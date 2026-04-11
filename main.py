import asyncio
import os
import pathlib
import queue
import subprocess
import sys
import time
from collections import deque
from typing import Optional

import numpy as np
import sounddevice as sd
from openwakeword.model import Model

from core.config import get_tts_config, get_hardware_config, load_app_config
from core.factory import ServiceFactory
from services.actuation.ros_topic_dispatcher import ROSTopicDispatcher
from services.reasoning.dialogue_manager import DialogueManager


WAKEWORD_SAMPLE_RATE = 16000
WAKEWORD_BLOCK_SIZE = 1280
MAX_COMMAND_SECONDS = 8.0
MIN_COMMAND_SECONDS = 0.8       # Reduced from 1.2s → saves ~400ms per turn
SILENCE_TIMEOUT_SECONDS = 0.6  # Reduced from 1.0s → saves ~400ms per turn
ENERGY_THRESHOLD = 0.015
SPEECH_START_THRESHOLD = 0.02
MIN_SPEECH_CHUNKS = 4           # Reduced from 5 to match shorter min window
PRE_ROLL_SECONDS = 1.5
DEBUG_LOG_INTERVAL_SECONDS = 1.0
WAKEWORD_DEBUG_FLOOR = 0.20
FOLLOW_UP_TIMEOUT_SECONDS = 180.0

# Display name for the wake word (always shows as "Jarvis" to the user)
WAKEWORD_DISPLAY_NAME = "Jarvis"


class LiveAudioPipeline:
    """
    End-to-end microphone loop:
    wake word -> capture command -> ASR -> dialogue -> ROS action -> TTS.
    """

    def __init__(self):
        self.debug_enabled = os.getenv("NLP_DEBUG", "1").lower() not in {"0", "false", "no"}
        self.session_id = f"session_{int(time.time())}"
        self.audio_queue: "queue.Queue[np.ndarray]" = queue.Queue()
        self.tts_sentence_queue: "asyncio.Queue[Optional[str]]" = asyncio.Queue()
        self.preroll_chunks = deque(
            maxlen=max(1, int((PRE_ROLL_SECONDS * WAKEWORD_SAMPLE_RATE) / WAKEWORD_BLOCK_SIZE))
        )

        # Load configuration
        app_cfg = load_app_config()
        ww_cfg = app_cfg.get("wake_word", {})

        # ── Hardware mode (laptop vs g1) ───────────────────────────────────────
        hw_cfg = get_hardware_config()
        self.hardware_mode = hw_cfg["mode"]
        # MIC: None = sounddevice default (laptop), or PulseAudio source name (g1)
        self.device = os.getenv("MIC_DEVICE") or hw_cfg["mic_device"]
        # TTS: extra args forwarded to aplay so we can target g1_speaker sink
        self.tts_player_extra_args: list = hw_cfg.get("tts_player_extra_args", [])
        print(f"[HARDWARE] mode={self.hardware_mode.upper()}, mic_device={self.device or 'system default'}")

        self.wakeword_name = os.getenv("WAKEWORD_MODEL", ww_cfg.get("model", "hey_jarvis_v0.1"))
        self.wakeword_key = pathlib.Path(self.wakeword_name).stem.replace(" ", "_")
        
        # ── Initialize Wake Word engine ────────────────────────────────────────
        try:
            # If it's a path, ensure it exists
            if "/" in self.wakeword_name or self.wakeword_name.endswith(".onnx"):
                model_path = os.path.abspath(self.wakeword_name)
                print(f"[WAKEWORD] Loading custom model from path: {model_path}")
                if not os.path.exists(model_path):
                    raise FileNotFoundError(f"Wake-word model file not found at: {model_path}")
                self.wakeword_model = Model(wakeword_models=[model_path], inference_framework="onnx")
            else:
                print(f"[WAKEWORD] Initialized with standard name: '{self.wakeword_name}'")
                self.wakeword_model = Model(wakeword_models=[self.wakeword_name], inference_framework="onnx")
        except Exception as e:
            print(f"\n[NLP ERROR] Failed to load wake-word model: {e}")
            sys.exit(0) # Exit cleanly to avoid container loop thrashing

        self.wakeword_threshold = float(os.getenv("WAKEWORD_THRESHOLD", ww_cfg.get("threshold", "0.5")))
        self.wakeword_display = os.getenv("WAKEWORD_DISPLAY", ww_cfg.get("display_name", WAKEWORD_DISPLAY_NAME))
        self.tts_config = get_tts_config()
        hw_tts_player = hw_cfg.get("tts_player", self.tts_config.get("player", "aplay"))
        self.tts_player = os.getenv("TTS_PLAYER", hw_tts_player)

        self.asr = ServiceFactory.get_asr_provider()
        self.llm = ServiceFactory.get_llm_provider()
        self.dialogue_manager = DialogueManager(self.llm)
        self.ros_dispatcher = ROSTopicDispatcher(
            topic_name=os.getenv("ROS_ACTION_TOPIC", "/humanoid/nlp/actions")
        )

        self.tts = None
        self.tts_sample_rate = None
        try:
            self.tts = ServiceFactory.get_tts_provider()
            self.tts_sample_rate = self.tts.get_sample_rate()
            model_path = getattr(self.tts, "model_path", "unknown")
            print(
                f"[TTS] Enabled: model={model_path}, sample_rate={self.tts_sample_rate}, "
                f"player={self.tts_player}"
            )
        except Exception as exc:
            configured_model = os.getenv(
                "TTS_MODEL_PATH",
                self.tts_config.get("model_path", "models/en_US-lessac-medium.onnx"),
            )
            print(f"[TTS] Disabled: {exc}")
            print(f"[TTS] Expected model path: {configured_model}")
            print(f"[TTS] Expected config path: {configured_model}.json")
            print(f"[TTS] Player command: {self.tts_player}")

    def _debug(self, message: str) -> None:
        if self.debug_enabled:
            print(f"[DEBUG] {message}")

    def _drain_audio_queue(self) -> int:
        drained = 0
        while True:
            try:
                self.audio_queue.get_nowait()
                drained += 1
            except queue.Empty:
                return drained

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            print(f"[AUDIO] {status}", file=sys.stderr)

        chunk = np.squeeze(indata.copy())
        if chunk.ndim == 0:
            chunk = np.array([chunk], dtype=np.int16)
        if chunk.dtype != np.int16:
            chunk = chunk.astype(np.int16)

        self.audio_queue.put(chunk)

    def _energy(self, chunk: np.ndarray) -> float:
        float_chunk = chunk.astype(np.float32) / 32768.0
        return float(np.sqrt(np.mean(np.square(float_chunk)) + 1e-12))

    async def _play_tts(self, text: str) -> None:
        if not self.tts or not text.strip():
            print(f"[TTS:FALLBACK] {text}")
            return

        self._debug(f"TTS start: sample_rate={self.tts_sample_rate}, chars={len(text)}")
        # Build aplay command: base args + optional extra args (e.g. -D g1_speaker)
        aplay_cmd = [
            self.tts_player,
            "-q",
            "-t", "raw",
            "-f", "S16_LE",
            "-r", str(self.tts_sample_rate),
            "-c", "1",
        ] + self.tts_player_extra_args
        player = subprocess.Popen(aplay_cmd, stdin=subprocess.PIPE)

        try:
            async for audio_bytes in self.tts.speak(text):
                if player.stdin:
                    player.stdin.write(audio_bytes)
            if player.stdin:
                player.stdin.close()
            player.wait(timeout=10)
            self._debug("TTS playback complete")
        except Exception as exc:
            print(f"[TTS:ERROR] {exc}")
            player.kill()

    async def _acknowledge_wake_word(self) -> None:
        response = "Yes? How can I help you?"
        print(f"[{self.wakeword_display}] {response}")
        await self._play_tts(response)

    async def _ask_follow_up(self) -> None:
        response = "Is there anything else I can do for you?"
        print(f"[{self.wakeword_display}] {response}")
        await self._play_tts(response)

    async def _queue_tts_sentence(self, sentence: str) -> None:
        cleaned = sentence.strip()
        if cleaned:
            await self.tts_sentence_queue.put(cleaned)

    async def _tts_worker(self) -> None:
        while True:
            sentence = await self.tts_sentence_queue.get()
            if sentence is None:
                self.tts_sentence_queue.task_done()
                return
            try:
                await self._play_tts(sentence)
            finally:
                self.tts_sentence_queue.task_done()

    async def _transcribe_command(self, audio_chunks: list[np.ndarray]) -> Optional[str]:
        if not audio_chunks:
            return None

        audio = np.concatenate(audio_chunks).astype(np.float32) / 32768.0
        duration_seconds = len(audio) / WAKEWORD_SAMPLE_RATE
        self._debug(f"ASR start: samples={len(audio)}, duration={duration_seconds:.2f}s")

        # Run ASR in a thread so the event loop stays free (TTS can keep playing)
        t_asr_start = time.time()
        utterance = await asyncio.to_thread(self.asr.transcribe_sync, audio)
        self._debug(f"ASR done in {time.time() - t_asr_start:.2f}s")

        if not utterance.text.strip():
            self._debug("ASR returned empty text")
            return None

        print(f"[USER] {utterance.text}")
        self._debug(f"ASR metadata: language={utterance.language}, confidence={utterance.confidence:.3f}")

        # ── [WEEK 2 HOOK: FACIAL RECOGNITION] ─────────────────────────────────
        # person_id = await asyncio.to_thread(self.face_recognizer.identify)
        # if person_id:
        #     self.dialogue_manager.update_context(self.session_id, {"recognized_person_id": person_id})
        # ──────────────────────────────────────────────────────────────────────

        self._debug("Dialogue manager processing started")
        streaming_tts_enabled = self.tts is not None
        state = await self.dialogue_manager.process_utterance(
            self.session_id,
            utterance,
            on_response_sentence=self._queue_tts_sentence if streaming_tts_enabled else None,
        )
        self._debug(f"Dialogue manager actions={len(state['extracted_actions'])}")

        if state["extracted_actions"]:
            self._debug("Dispatching extracted actions to ROS topic")
            self.ros_dispatcher.dispatch_many(state["extracted_actions"])

        response = state["response_text"].strip()
        if response:
            print(f"[{self.wakeword_display}] {response}")
            if not streaming_tts_enabled:
                await self._play_tts(response)

        return utterance.text

    async def run(self) -> None:
        print(f"\n[NLP MODULE] Wake word pipeline ready.")
        print(f"Say '{self.wakeword_display}' followed by your request. Press Ctrl+C to stop.")
        if self.debug_enabled:
            print("[DEBUG] Verbose pipeline logging is enabled.")

        tts_worker_task = None
        if self.tts is not None:
            tts_worker_task = asyncio.create_task(self._tts_worker())

        try:
            parsed_device = None
            if self.device:
                try:
                    parsed_device = int(self.device)
                except ValueError:
                    parsed_device = self.device

            with sd.InputStream(
                samplerate=WAKEWORD_SAMPLE_RATE,
                blocksize=WAKEWORD_BLOCK_SIZE,
                device=parsed_device,
                channels=1,
                dtype="int16",
                callback=self._audio_callback,
            ):
                listening_for_command = False
                follow_up_mode = False
                command_chunks: list[np.ndarray] = []
                command_started_at = 0.0
                speech_started = False
                speech_chunk_count = 0
                silence_started_at: Optional[float] = None
                last_debug_log_at = 0.0

                while True:
                    chunk = await asyncio.to_thread(self.audio_queue.get)
                    now = time.time()
                    self.preroll_chunks.append(chunk)
                    energy = self._energy(chunk)

                    if not listening_for_command:
                        scores = self.wakeword_model.predict(chunk)
                        score = float(scores.get(self.wakeword_key, max(scores.values(), default=0.0)))
                        if score >= WAKEWORD_DEBUG_FLOOR:
                            self._debug(f"Wake score={score:.3f}, energy={energy:.4f}")
                        elif self.debug_enabled and now - last_debug_log_at >= DEBUG_LOG_INTERVAL_SECONDS:
                            self._debug(
                                f"Standing by: wake_score={score:.3f}, energy={energy:.4f}, "
                                f"queue={self.audio_queue.qsize()}"
                            )
                            last_debug_log_at = now
                        if score >= self.wakeword_threshold:
                            print(f"[{self.wakeword_display}] Wake word detected ({score:.3f}).")
                            await self._acknowledge_wake_word()
                            drained = self._drain_audio_queue()
                            self._debug(f"Wake word accepted, cleared {drained} stale audio chunks")
                            await asyncio.sleep(0.1)
                            listening_for_command = True
                            speech_started = False
                            speech_chunk_count = 0
                            silence_started_at = None
                            command_started_at = time.time()
                            command_chunks = []
                            follow_up_mode = False
                            print(f"[{self.wakeword_display}] Listening for your command...")
                        continue

                    command_chunks.append(chunk)
                    if self.debug_enabled and now - last_debug_log_at >= DEBUG_LOG_INTERVAL_SECONDS:
                        self._debug(
                            f"Capturing command: elapsed={now - command_started_at:.2f}s, "
                            f"energy={energy:.4f}, chunks={len(command_chunks)}"
                        )
                        last_debug_log_at = now

                    if energy >= ENERGY_THRESHOLD:
                        if energy >= SPEECH_START_THRESHOLD:
                            speech_started = True
                            speech_chunk_count += 1
                            silence_started_at = None
                            self._debug(
                                f"Speech detected: energy={energy:.4f}, speech_chunks={speech_chunk_count}"
                            )
                    elif speech_started and silence_started_at is None:
                        silence_started_at = now
                        self._debug("Silence timer started")

                    elapsed = now - command_started_at
                    has_min_audio = elapsed >= MIN_COMMAND_SECONDS
                    timed_out = elapsed >= MAX_COMMAND_SECONDS
                    follow_up_timed_out = follow_up_mode and elapsed >= FOLLOW_UP_TIMEOUT_SECONDS
                    silence_complete = (
                        silence_started_at is not None and now - silence_started_at >= SILENCE_TIMEOUT_SECONDS
                    )

                    if not (timed_out or follow_up_timed_out or (has_min_audio and silence_complete)):
                        continue

                    listening_for_command = False
                    try:
                        self._debug(
                            f"Command capture finished: elapsed={elapsed:.2f}s, "
                            f"timed_out={timed_out}, silence_complete={silence_complete}, "
                            f"speech_chunks={speech_chunk_count}, follow_up_mode={follow_up_mode}"
                        )
                        if follow_up_timed_out:
                            self._debug("Follow-up window expired, returning to wake-word standby")
                        elif speech_chunk_count < MIN_SPEECH_CHUNKS:
                            if follow_up_mode:
                                self._debug("Ignoring short/noisy follow-up input and continuing to wait")
                                listening_for_command = True
                                command_started_at = time.time()
                                command_chunks = []
                                speech_started = False
                                speech_chunk_count = 0
                                silence_started_at = None
                                continue
                            print(f"[{self.wakeword_display}] I didn't catch that. Please try again.")
                            await self._play_tts("I didn't catch that. Please try again.")
                        else:
                            transcribed_text = await self._transcribe_command(command_chunks)
                            if transcribed_text:
                                # Ask follow-up ONLY the first time (not every answer)
                                if not follow_up_mode:
                                    await self._ask_follow_up()
                                drained = self._drain_audio_queue()
                                self._debug(f"Follow-up mode active, cleared {drained} stale audio chunks")
                                await asyncio.sleep(0.1)
                                listening_for_command = True
                                follow_up_mode = True
                                command_started_at = time.time()
                                command_chunks = []
                                speech_started = False
                                speech_chunk_count = 0
                                silence_started_at = None
                                last_debug_log_at = 0.0
                                if self.tts is not None:
                                    await self.tts_sentence_queue.join()
                                continue
                            elif follow_up_mode:
                                # Empty transcription (e.g. mic picked up TTS echo) —
                                # stay silent and keep waiting, do NOT fall back to standby
                                self._debug("Empty transcription in follow-up mode, staying in follow-up")
                                drained = self._drain_audio_queue()
                                listening_for_command = True
                                command_started_at = time.time()
                                command_chunks = []
                                speech_started = False
                                speech_chunk_count = 0
                                silence_started_at = None
                                continue

                        if self.tts is not None:
                            await self.tts_sentence_queue.join()
                    except Exception as exc:
                        print(f"[PIPELINE ERROR] {exc}")
                    finally:
                        command_chunks = []
                        speech_started = False
                        speech_chunk_count = 0
                        silence_started_at = None
                        follow_up_mode = False
                        self.preroll_chunks.clear()
                        last_debug_log_at = 0.0
                        print("[NLP MODULE] Standing by for wake word...")
        finally:
            if tts_worker_task is not None:
                await self.tts_sentence_queue.put(None)
                await tts_worker_task


async def run_interaction_loop():
    pipeline = LiveAudioPipeline()
    await pipeline.run()


if __name__ == "__main__":
    try:
        asyncio.run(run_interaction_loop())
    except KeyboardInterrupt:
        print("\n[NLP MODULE] Shutting down...")
    except Exception as exc:
        print(f"\n[NLP ERROR] {exc}")
