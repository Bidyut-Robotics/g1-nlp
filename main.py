"""
main.py — Humanoid NLP Voice Pipeline
======================================
7-Phase conversational state machine:

  PHASE 1 — STANDBY: Wake-word detection
  PHASE 2 — COMMAND CAPTURE: Collect speech frames
  PHASE 3 — VALIDATE: Check for enough speech chunks
  PHASE 4 — ASR: Transcribe audio in a thread
  PHASE 5 — DIALOGUE: Run LLM + queue TTS sentences
  PHASE 6 — TTS PLAYBACK: Speak response, suppress echo
  PHASE 7 — CONVERSATION: Stay active, wait for follow-up
             → if CONVERSATION_TIMEOUT elapses → back to PHASE 1
"""

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


# ── Audio capture constants ───────────────────────────────────────────────────
WAKEWORD_SAMPLE_RATE = 16000
WAKEWORD_BLOCK_SIZE = 1280          # ~80 ms per chunk at 16kHz

# ── Command capture timing ────────────────────────────────────────────────────
MAX_COMMAND_SECONDS = 10.0          # Hard timeout: stop listening after this
MIN_COMMAND_SECONDS = 0.8           # Minimum before we check for silence
SILENCE_TIMEOUT_SECONDS = 0.8      # Silence after speech = end of utterance

# ── Energy / VAD thresholds ───────────────────────────────────────────────────
ENERGY_THRESHOLD = 0.010            # Below this → silence
SPEECH_START_THRESHOLD = 0.012     # Above this → speech started
MIN_SPEECH_CHUNKS = 2               # Minimum speech chunks to bother transcribing

# ── Pre-roll ──────────────────────────────────────────────────────────────────
PRE_ROLL_SECONDS = 1.5

# ── Conversational mode timeout (PHASE 7) ────────────────────────────────────
# Robot stays listening for follow-up questions for this long after last answer.
# After this elapses with no speech, it returns to PHASE 1 wake-word standby.
CONVERSATION_TIMEOUT = 30.0         # seconds idle before returning to standby

# ── Debug ─────────────────────────────────────────────────────────────────────
DEBUG_LOG_INTERVAL_SECONDS = 1.0
WAKEWORD_DEBUG_FLOOR = 0.20


class G1MulticastStream:
    """
    Direct UDP Multicast listener for G1 Microphone.
    Bypasses PulseAudio entirely.
    """
    def __init__(self, queue, interface="eth0", local_ip="192.168.123.164"):
        self.queue = queue
        self.interface = interface
        self.local_ip = local_ip
        self.running = False
        self.thread = None
        self.multicast_group = "239.168.123.161"
        self.port = 5555
        self._voice_client = None

    def _activate_mic(self):
        """Send Voice Service API command to start mic streaming (mode=1)."""
        import json
        try:
            from unitree_sdk2py.rpc.client import Client
            API_SET_MODE = 1008
            vc = Client("voice", False)
            vc.SetTimeout(5.0)
            vc._SetApiVerson("1.0.0.0")
            vc._RegistApi(API_SET_MODE, 0)
            code, _ = vc._Call(API_SET_MODE, json.dumps({"mode": 1}))
            if code != 0:
                print(f"[AUDIO:G1-DIRECT] Warning: Could not enable mic mode (code={code}). Check PC1 voice service.")
            else:
                print("[AUDIO:G1-DIRECT] Mic streaming activated (mode=1).")
            self._voice_client = (vc, API_SET_MODE)
        except Exception as e:
            print(f"[AUDIO:G1-DIRECT] Warning: Failed to activate mic via voice API: {e}")

    def _deactivate_mic(self):
        """Reset mic to idle mode (mode=2)."""
        import json
        if self._voice_client:
            try:
                vc, API_SET_MODE = self._voice_client
                vc._Call(API_SET_MODE, json.dumps({"mode": 2}))
                print("[AUDIO:G1-DIRECT] Mic streaming deactivated (mode=2).")
            except Exception as e:
                print(f"[AUDIO:G1-DIRECT] Warning: Failed to deactivate mic: {e}")

    def _listen(self):
        import socket
        import struct

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        # Bind to the port
        sock.bind(('', self.port))

        # Join multicast group explicitly on the robot interface IP
        # This prevents it from accidentally joining on WiFi
        try:
            mreq = struct.pack("4s4s", 
                               socket.inet_aton(self.multicast_group), 
                               socket.inet_aton(self.local_ip))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except Exception as e:
            print(f"[AUDIO:G1-DIRECT] Warning: Failed to join multicast group on {self.local_ip}: {e}")
            # Fallback to general join
            mreq = struct.pack("4sl", socket.inet_aton(self.multicast_group), socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        sock.settimeout(1.0)
        print(f"[AUDIO:G1-DIRECT] Listening on {self.local_ip} -> {self.multicast_group}:{self.port}")

        while self.running:
            try:
                data, _ = sock.recvfrom(8192)
                if len(data) > 0:
                    # G1 sends 8 channels of int16 audio
                    raw_samples = np.frombuffer(data, dtype=np.int16)
                    
                    if len(raw_samples) % 8 == 0:
                        # Reshape to (N, 8) and take first channel
                        chunk = raw_samples.reshape(-1, 8)[:, 0].copy()
                        self.queue.put(chunk)
                    else:
                        # Fallback if packet is weirdly sized
                        self.queue.put(raw_samples.copy())
            except socket.timeout:
                continue
            except Exception as e:
                print(f"[AUDIO:G1-DIRECT ERROR] {e}")
                break
        sock.close()

    def start(self):
        self._activate_mic()
        self.running = True
        import threading
        self.thread = threading.Thread(target=self._listen, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join()
        self._deactivate_mic()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


class LiveAudioPipeline:
    """
    End-to-end microphone loop.
    Wake-word → capture → ASR → dialogue → TTS, with conversational follow-up.
    """

    def __init__(self):
        app_cfg_early = load_app_config()
        _env_debug = os.getenv("NLP_DEBUG")
        if _env_debug is not None:
            self.debug_enabled = _env_debug.lower() not in {"0", "false", "no"}
        else:
            self.debug_enabled = bool(app_cfg_early.get("nlp_debug", True))
        self.session_id = f"session_{int(time.time())}"

        # Thread-safe audio queue filled by the sounddevice callback
        self.audio_queue: "queue.Queue[np.ndarray]" = queue.Queue()

        # Async queue consumed by the TTS worker coroutine
        self.tts_sentence_queue: "asyncio.Queue[Optional[str]]" = asyncio.Queue()

        # Pre-roll: keeps last ~1.5s of chunks so we don't miss the first syllable
        self.preroll_chunks = deque(
            maxlen=max(1, int((PRE_ROLL_SECONDS * WAKEWORD_SAMPLE_RATE) / WAKEWORD_BLOCK_SIZE))
        )

        # Flag set while TTS is playing so the callback discards mic echo
        self.is_speaking: bool = False

        # ── Load configuration ────────────────────────────────────────────────
        app_cfg = load_app_config()
        ww_cfg = app_cfg.get("wake_word", {})

        hw_cfg = get_hardware_config()
        self.hardware_mode = hw_cfg["mode"]
        self.device = os.getenv("MIC_DEVICE") or hw_cfg["mic_device"]
        self.tts_player_extra_args: list = hw_cfg.get("tts_player_extra_args", [])
        print(f"[HARDWARE] mode={self.hardware_mode.upper()}, mic_device={self.device or 'system default'}")

        # ── Wake-word model ───────────────────────────────────────────────────
        self.wakeword_name = os.getenv("WAKEWORD_MODEL", ww_cfg.get("model", "hey_jarvis_v0.1"))
        self.wakeword_key = pathlib.Path(self.wakeword_name).stem.replace(" ", "_")

        try:
            if "/" in self.wakeword_name or self.wakeword_name.endswith(".onnx"):
                model_path = os.path.abspath(self.wakeword_name)
                print(f"[WAKEWORD] Loading custom model from path: {model_path}")
                if not os.path.exists(model_path):
                    raise FileNotFoundError(f"Wake-word model file not found at: {model_path}")
                self.wakeword_model = Model(wakeword_models=[model_path], inference_framework="onnx")
            else:
                print(f"[WAKEWORD] Initialized with standard name: '{self.wakeword_name}'")
                self.wakeword_model = Model(wakeword_models=[self.wakeword_name], inference_framework="onnx")
            print(f"[WAKEWORD] Available keys in model: {list(self.wakeword_model.models.keys())}")
        except Exception as e:
            print(f"\n[NLP ERROR] Failed to load wake-word model: {e}")
            sys.exit(0)

        self.wakeword_threshold = float(os.getenv("WAKEWORD_THRESHOLD", ww_cfg.get("threshold", "0.5")))
        self.wakeword_display = os.getenv("WAKEWORD_DISPLAY", ww_cfg.get("display_name", "Jarvis"))

        # ── TTS ───────────────────────────────────────────────────────────────
        self.tts_config = get_tts_config()
        hw_tts_player = hw_cfg.get("tts_player", self.tts_config.get("player", "aplay"))
        self.tts_player = os.getenv("TTS_PLAYER", hw_tts_player)

        # ── Core services ─────────────────────────────────────────────────────
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

    # ─────────────────────────────────────────────────────────────────────────
    # Utility helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _debug(self, message: str) -> None:
        if self.debug_enabled:
            print(f"[DEBUG] {message}")

    def _drain_audio_queue(self) -> int:
        """Discard all pending chunks in the audio queue. Returns count drained."""
        drained = 0
        while True:
            try:
                self.audio_queue.get_nowait()
                drained += 1
            except queue.Empty:
                return drained

    def _energy(self, chunk: np.ndarray) -> float:
        float_chunk = chunk.astype(np.float32) / 32768.0
        return float(np.sqrt(np.mean(np.square(float_chunk)) + 1e-12))

    # ─────────────────────────────────────────────────────────────────────────
    # Sounddevice callback — runs in a C thread, must be non-blocking
    # ─────────────────────────────────────────────────────────────────────────

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            print(f"[AUDIO] {status}", file=sys.stderr)

        chunk = np.squeeze(indata.copy())
        if chunk.ndim == 0:
            chunk = np.array([chunk], dtype=np.int16)
        if chunk.dtype != np.int16:
            chunk = chunk.astype(np.int16)

        # PHASE 6 echo suppression: discard mic input while TTS is playing
        if self.is_speaking:
            return

        self.audio_queue.put(chunk)

    # ─────────────────────────────────────────────────────────────────────────
    # TTS helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def _play_tts(self, text: str) -> None:
        """Synthesise and play a single string, setting is_speaking for echo suppression."""
        if not self.tts or not text.strip():
            print(f"[TTS:FALLBACK] {text}")
            return

        self._debug(f"TTS start: builtin={getattr(self.tts, 'is_builtin', False)}, chars={len(text)}")
        
        self.is_speaking = True
        try:
            # ── Built-in mode (Robot's own voice) ────────────────────────────
            if getattr(self.tts, "is_builtin", False):
                # Just iterate to trigger the API call, no bytes to play
                async for _ in self.tts.speak(text):
                    pass
            
            # ── Standard mode (Custom Jarvis voice via aplay) ────────────────
            else:
                aplay_cmd = [
                    self.tts_player, "-q",
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
                except Exception as exc:
                    player.kill()
                    raise exc

            self._debug("TTS playback complete")
        except Exception as exc:
            print(f"[TTS:ERROR] {exc}")
        finally:
            self.is_speaking = False

    async def _queue_tts_sentence(self, sentence: str) -> None:
        cleaned = sentence.strip()
        if cleaned:
            await self.tts_sentence_queue.put(cleaned)

    async def _tts_worker(self) -> None:
        """Background coroutine: pulls sentences from the queue and speaks them serially."""
        while True:
            sentence = await self.tts_sentence_queue.get()
            if sentence is None:
                self.tts_sentence_queue.task_done()
                return
            try:
                await self._play_tts(sentence)
            finally:
                self.tts_sentence_queue.task_done()

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 2 helper: reset per-turn state
    # ─────────────────────────────────────────────────────────────────────────

    def _reset_turn(self) -> dict:
        """Return a fresh turn state dict."""
        return {
            "command_chunks": [],
            "turn_start_time": time.time(),
            "speech_detected": False,
            "speech_chunk_count": 0,
            "silence_start_time": None,
            "last_debug_at": 0.0,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 4 + 5: Transcribe → Dialogue → queue TTS
    # ─────────────────────────────────────────────────────────────────────────

    async def _process_command(self, audio_chunks: list) -> Optional[str]:
        """
        PHASE 4 — ASR transcription (run in thread).
        PHASE 5 — Dialogue manager with sentence-streaming to TTS worker.
        PHASE 6 — Fallback: if nothing was streamed, play full response directly.
        Returns transcribed text, or None if ASR got noise/silence.
        """
        audio = np.concatenate(audio_chunks).astype(np.float32) / 32768.0
        duration = len(audio) / WAKEWORD_SAMPLE_RATE
        self._debug(f"ASR start: samples={len(audio)}, duration={duration:.2f}s")

        t0 = time.time()
        utterance = await asyncio.to_thread(self.asr.transcribe_sync, audio)
        self._debug(f"ASR done in {time.time() - t0:.2f}s")

        if not utterance.text.strip():
            self._debug("ASR returned empty — going back to listening")
            return None

        print(f"[USER] {utterance.text}")
        self._debug("Dialogue manager processing started")

        # STREAMING: pass sentence callback so TTS starts as each sentence arrives.
        # The _tts_worker coroutine runs concurrently and plays sentences immediately.
        streaming_tts = self.tts is not None
        tts_sentences_queued = 0

        async def _counted_queue(sentence: str) -> None:
            nonlocal tts_sentences_queued
            await self._queue_tts_sentence(sentence)
            tts_sentences_queued += 1

        state = await self.dialogue_manager.process_utterance(
            self.session_id,
            utterance,
            on_response_sentence=_counted_queue if streaming_tts else None,
        )
        self._debug(
            f"Dialogue done, actions={len(state['extracted_actions'])}, "
            f"streamed_sentences={tts_sentences_queued}"
        )

        if state["extracted_actions"]:
            self.ros_dispatcher.dispatch_many(state["extracted_actions"])

        response = state["response_text"].strip()
        if response:
            print(f"[{self.wakeword_display}] {response}")

        # Fallback: if sentence splitter produced nothing (e.g. very short/unusual
        # response without punctuation), play full text directly so audio always plays.
        if streaming_tts and tts_sentences_queued == 0 and response:
            self._debug("Streaming produced 0 sentences — falling back to direct play")
            await self._play_tts(response)

        return utterance.text


    # ─────────────────────────────────────────────────────────────────────────
    # Main loop
    # ─────────────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        print(f"\n[NLP MODULE] Wake word pipeline ready.")
        print(f"Say '{self.wakeword_display}' to activate. Press Ctrl+C to stop.")
        if self.debug_enabled:
            print("[DEBUG] Verbose pipeline logging is enabled.")

        # Start the TTS worker coroutine
        tts_worker_task = None
        if self.tts is not None:
            tts_worker_task = asyncio.create_task(self._tts_worker())

        try:
            # Resolve device: int index or string name
            parsed_device = None
            if self.device:
                try:
                    parsed_device = int(self.device)
                except (ValueError, TypeError):
                    parsed_device = self.device

            # ── Choose Capture Method ─────────────────────────────────────────────
            # If G1 mode, we use direct multicast to bypass PulseAudio
            if self.hardware_mode == "g1":
                 g1_cfg = load_app_config().get("g1", {})
                 local_ip = g1_cfg.get("local_ip", "192.168.123.164")
                 interface = g1_cfg.get("dds_interface", "eth0")
                 capture_context = G1MulticastStream(self.audio_queue, interface=interface, local_ip=local_ip)
            else:
                 capture_context = sd.InputStream(
                    samplerate=WAKEWORD_SAMPLE_RATE,
                    blocksize=WAKEWORD_BLOCK_SIZE,
                    device=parsed_device,
                    channels=1,
                    dtype="int16",
                    callback=self._audio_callback,
                )

            with capture_context:
                # ── State machine outer loop ──────────────────────────────────
                while True:

                    # ════════════════════════════════════════════════════════
                    # PHASE 1 — STANDBY: passive wake-word detection
                    # ════════════════════════════════════════════════════════
                    print("[NLP MODULE] Standby — waiting for wake word...")
                    last_debug_at = 0.0

                    while True:
                        chunk = await asyncio.to_thread(self.audio_queue.get)
                        now = time.time()
                        energy = self._energy(chunk)
                        self.preroll_chunks.append(chunk)

                        scores = self.wakeword_model.predict(chunk)
                        score = float(scores.get(
                            self.wakeword_key,
                            max(scores.values(), default=0.0)
                        ))

                        if score >= WAKEWORD_DEBUG_FLOOR:
                            self._debug(f"Wake score={score:.3f}, energy={energy:.4f}")
                        elif self.debug_enabled and now - last_debug_at >= DEBUG_LOG_INTERVAL_SECONDS:
                            self._debug(
                                f"Standby: score={score:.3f}, energy={energy:.4f}, "
                                f"queue={self.audio_queue.qsize()}"
                            )
                            last_debug_at = now

                        if score >= self.wakeword_threshold:
                            print(f"[{self.wakeword_display}] Wake word detected ({score:.3f}).")
                            break   # Exit PHASE 1

                    # ── Acknowledge + flush ───────────────────────────────────
                    ack = "Yes? How can I help you?"
                    print(f"[{self.wakeword_display}] {ack}")
                    await self._play_tts(ack)           # sets is_speaking during playback

                    # Drain any mic echo accumulated during TTS ack
                    drained = self._drain_audio_queue()
                    self._debug(f"Post-ack drain: cleared {drained} chunks")

                    # Reset wakeword internal sliding window
                    self.wakeword_model.reset()
                    self.preroll_chunks.clear()

                    # Track when the user last interacted (used for PHASE 7 timeout)
                    last_interaction_at = time.time()

                    # ════════════════════════════════════════════════════════
                    # PHASE 2–7: Conversational loop  (no wake word needed)
                    # ════════════════════════════════════════════════════════
                    in_conversation = True

                    while in_conversation:

                        # ── PHASE 2 — Reset per-turn state ───────────────────
                        turn = self._reset_turn()

                        # ── PHASE 7 check: has the session timed out? ─────────
                        if time.time() - last_interaction_at >= CONVERSATION_TIMEOUT:
                            print(
                                f"[NLP MODULE] {int(CONVERSATION_TIMEOUT)}s idle — "
                                f"returning to standby."
                            )
                            in_conversation = False
                            break

                        # ── PHASE 2 — Capture command ─────────────────────────
                        while True:
                            now = time.time()
                            elapsed = now - turn["turn_start_time"]

                            # Hard timeout
                            if elapsed >= MAX_COMMAND_SECONDS:
                                self._debug("Hard timeout reached")
                                break

                            # PHASE 7: session idle check inside capture
                            if time.time() - last_interaction_at >= CONVERSATION_TIMEOUT:
                                self._debug("Conversation timed out during capture")
                                in_conversation = False
                                break

                            try:
                                chunk = self.audio_queue.get(timeout=0.1)
                            except queue.Empty:
                                continue

                            energy = self._energy(chunk)
                            turn["command_chunks"].append(chunk)

                            if self.debug_enabled and now - turn["last_debug_at"] >= DEBUG_LOG_INTERVAL_SECONDS:
                                self._debug(
                                    f"Capturing: elapsed={elapsed:.2f}s, "
                                    f"energy={energy:.4f}, "
                                    f"chunks={len(turn['command_chunks'])}"
                                )
                                turn["last_debug_at"] = now

                            # VAD
                            if energy >= SPEECH_START_THRESHOLD:
                                turn["speech_detected"] = True
                                turn["speech_chunk_count"] += 1
                                turn["silence_start_time"] = None
                                self._debug(
                                    f"Speech: energy={energy:.4f}, "
                                    f"count={turn['speech_chunk_count']}"
                                )
                            elif turn["speech_detected"] and turn["silence_start_time"] is None:
                                turn["silence_start_time"] = now
                                self._debug("Silence timer started")

                            # Natural end of utterance (min duration + silence gap)
                            if (
                                elapsed >= MIN_COMMAND_SECONDS
                                and turn["silence_start_time"] is not None
                                and now - turn["silence_start_time"] >= SILENCE_TIMEOUT_SECONDS
                            ):
                                self._debug("Silence timeout — end of utterance")
                                break

                        # If session timed out mid-capture, exit conversation
                        if not in_conversation:
                            break

                        # ── PHASE 3 — Validate: enough speech? ───────────────
                        if turn["speech_chunk_count"] < MIN_SPEECH_CHUNKS:
                            # Silently loop — no annoying "I didn't catch that"
                            self._debug(
                                f"Not enough speech ({turn['speech_chunk_count']} chunks) "
                                f"— looping silently"
                            )
                            continue

                        # ── PHASE 4 + 5 — ASR + Dialogue ─────────────────────
                        try:
                            result = await self._process_command(turn["command_chunks"])
                        except Exception as exc:
                            print(f"[PIPELINE ERROR] {exc}")
                            # PHASE error handling: stay in conversation, do NOT go to standby
                            continue

                        if result is None:
                            # ASR returned empty — noise or whisper silence; keep listening
                            self._debug("Empty ASR result — continuing to listen")
                            continue

                        # ── Successful interaction — update last_interaction time ──
                        last_interaction_at = time.time()

                        # ── Check if user wants to end the conversation ───────
                        session_state = self.dialogue_manager.sessions.get(self.session_id, {})
                        if session_state.get("exit_intent", False):
                            print("[NLP MODULE] User ended conversation. Returning to standby.")
                            in_conversation = False
                            # Wait for TTS to finish saying goodbye
                            if self.tts is not None:
                                await self.tts_sentence_queue.join()
                            break

                        # ── PHASE 6 — Wait for TTS to finish ─────────────────
                        if self.tts is not None:
                            await self.tts_sentence_queue.join()

                        # Drain any mic echo picked up during playback
                        drained = self._drain_audio_queue()
                        if drained > 0:
                            self._debug(f"Post-TTS drain: cleared {drained} echo chunks")

                        # Reset wakeword model (won't be scored in conversation mode,
                        # but keeps it clean for when we return to PHASE 1)
                        self.wakeword_model.reset()

                    # End of conversational loop — clean up and go back to PHASE 1
                    self._drain_audio_queue()
                    self.wakeword_model.reset()
                    self.preroll_chunks.clear()

        finally:
            # Graceful shutdown: signal TTS worker to exit
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
