"""
main.py — Humanoid NLP Voice Pipeline
======================================
7-Phase conversational state machine with Silero VAD barge-in:

  PHASE 1 — STANDBY: Wake-word detection
  PHASE 2 — COMMAND CAPTURE: Collect speech frames (Silero VAD)
  PHASE 3 — VALIDATE: Check for enough speech chunks
  PHASE 4 — ASR: Transcribe audio in a thread
  PHASE 5 — DIALOGUE: Run LLM + queue TTS sentences
  PHASE 6 — TTS PLAYBACK: Speak response
             → BARGE-IN: Silero VAD detects user speech → stop TTS, capture user
  PHASE 7 — CONVERSATION: Stay active, wait for follow-up
             → if CONVERSATION_TIMEOUT elapses → back to PHASE 1

Key improvements:
  - Silero VAD replaces energy-ratio barge-in (reliable, ~160ms detection)
  - Non-blocking async capture loop (no event-loop starvation)
  - Ack TTS fire-and-forget with proper synchronisation via is_speaking flag
  - Post-barge-in, captured preroll + live frames feed directly into PHASE 2
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
import torch
from services.perception.whisper_wakeword import WhisperWakeWord, make_rolling_buffer

from core.config import get_tts_config, get_hardware_config, load_app_config
from core.factory import ServiceFactory
from core.schemas import ActionType
from services.actuation.ros_topic_dispatcher import ROSTopicDispatcher
from services.gesture.gesture_service import GestureService
from services.reasoning.dialogue_manager import DialogueManager


# ── Audio capture constants ───────────────────────────────────────────────────
WAKEWORD_SAMPLE_RATE = 16000
WAKEWORD_BLOCK_SIZE = 1280          # ~80 ms per chunk at 16kHz

# ── Command capture timing ────────────────────────────────────────────────────
MAX_COMMAND_SECONDS = 10.0          # Hard timeout: stop listening after this
MIN_COMMAND_SECONDS = 0.4           # Minimum before we check for silence
SILENCE_TIMEOUT_SECONDS = 0.6       # Silence after speech = end of utterance

# ── VAD / energy thresholds ───────────────────────────────────────────────────
ENERGY_THRESHOLD = 0.010            # Below this → silence
SPEECH_START_THRESHOLD = 0.012      # Above this → speech started
MIN_SPEECH_CHUNKS = 3               # Minimum speech chunks before ASR (~240ms of actual speech)

# Silero VAD thresholds
VAD_SPEECH_THRESHOLD = 0.5          # Probability threshold for "is speech"
# Absolute mic energy floor for barge-in. Chunks below this are discarded before Silero.
# Laptop room noise ≈ 0.008-0.012; normal speech at 50cm ≈ 0.03-0.15.
# Raise this if TTS echo triggers false barge-in; lower if your voice isn't detected.
BARGE_IN_MIN_MIC_ENERGY = 0.025     # absolute floor — never trigger below this
BARGE_IN_SNR_RATIO      = 2.5       # must exceed noise_floor × this to trigger
NOISE_FLOOR_ALPHA       = 0.005     # EMA speed for noise floor update (slow)

# ── Barge-in grace period ─────────────────────────────────────────────────────
# Ignore barge-in for this many seconds after each TTS sentence starts.
# Short enough to allow barge-in mid-sentence; long enough to skip initial speaker burst.
BARGE_IN_GRACE_SECONDS = 0.3

# ── Pre-roll ──────────────────────────────────────────────────────────────────
PRE_ROLL_SECONDS = 1.5

# ── Conversational mode timeout (PHASE 7) ────────────────────────────────────
CONVERSATION_TIMEOUT = 30.0         # seconds idle before returning to standby

# ── Barge-in preroll ─────────────────────────────────────────────────────────
BARGE_IN_PREROLL_CHUNKS = 40        # ~3.2 s at 80 ms/chunk

# ── Post-TTS echo decay ───────────────────────────────────────────────────────
# After TTS stops, the speaker physically rings down and the soundcard buffer
# still drains for ~200-400ms. All mic audio during this window is discarded
# so it can't be captured as user speech and fed to ASR.
ECHO_DECAY_SECONDS = 0.5

# ── Debug ─────────────────────────────────────────────────────────────────────
DEBUG_LOG_INTERVAL_SECONDS = 1.0
WAKEWORD_DEBUG_FLOOR = 0.20


class G1MulticastStream:
    """
    Direct UDP Multicast listener for G1 Microphone.
    Bypasses PulseAudio entirely.

    extra_queues: optional list of additional queue.Queue objects that receive
    every chunk (used for barge-in VAD fan-out).
    """
    def __init__(self, queue, interface="eth0", local_ip="192.168.123.164",
                 extra_queues=None):
        self.queue = queue
        self.extra_queues = extra_queues or []
        self.interface = interface
        self.local_ip = local_ip
        self.running = False
        self.thread = None
        self.multicast_group = "239.168.123.161"
        self.port = 5555
        self._voice_client = None

    def _activate_mic(self):
        """Mic is activated by robot_agent at startup via DDS — nothing to do here."""
        print("[AUDIO:G1-DIRECT] Mic activated by robot_agent (DDS).")

    def _deactivate_mic(self):
        pass

    def _listen(self):
        import socket
        import struct

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('', self.port))

        try:
            mreq = struct.pack("4s4s",
                               socket.inet_aton(self.multicast_group),
                               socket.inet_aton(self.local_ip))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except Exception as e:
            print(f"[AUDIO:G1-DIRECT] Warning: Failed to join multicast group on {self.local_ip}: {e}")
            mreq = struct.pack("4sl", socket.inet_aton(self.multicast_group), socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        sock.settimeout(1.0)
        print(f"[AUDIO:G1-DIRECT] Listening on {self.local_ip} -> {self.multicast_group}:{self.port}")

        while self.running:
            try:
                data, _ = sock.recvfrom(8192)
                if len(data) > 0:
                    # G1 sends mono 16kHz int16 PCM (5120 bytes = 2560 samples = 160ms)
                    chunk = np.frombuffer(data, dtype=np.int16).copy()
                    try:
                        self.queue.put_nowait(chunk)
                    except queue.Full:
                        pass  # drop oldest-path chunk; never block the mic thread
                    # Fan out to extra consumers (e.g. barge-in VAD queue)
                    for eq in self.extra_queues:
                        try:
                            eq.put_nowait(chunk)
                        except queue.Full:
                            pass  # drop if consumer is slow; never block the mic thread
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
    Wake-word → capture → ASR → dialogue → TTS, with Silero VAD-based
    barge-in interruption during TTS playback.
    """

    def __init__(self):
        app_cfg_early = load_app_config()
        _env_debug = os.getenv("NLP_DEBUG")
        if _env_debug is not None:
            self.debug_enabled = _env_debug.lower() not in {"0", "false", "no"}
        else:
            self.debug_enabled = bool(app_cfg_early.get("nlp_debug", True))
        self.session_id = f"session_{int(time.time())}"

        # ── Audio queues ──────────────────────────────────────────────────────
        # Main pipeline queue (wakeword + command capture)
        self.audio_queue: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=200)
        # Barge-in VAD queue — receives ALL chunks regardless of is_speaking
        self._barge_in_queue: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=100)

        # Async queue consumed by the TTS worker coroutine
        self.tts_sentence_queue: "asyncio.Queue[Optional[str]]" = asyncio.Queue()

        # Pre-roll: keeps last ~1.5s of chunks so we don't miss the first syllable
        self.preroll_chunks = deque(
            maxlen=max(1, int((PRE_ROLL_SECONDS * WAKEWORD_SAMPLE_RATE) / WAKEWORD_BLOCK_SIZE))
        )

        # ── TTS state ─────────────────────────────────────────────────────────
        # Flag set while TTS is playing (used for echo suppression + barge-in gating)
        self.is_speaking: bool = False
        # Timestamp set when TTS finishes — used to enforce ECHO_DECAY_SECONDS window
        self._tts_stop_time: float = 0.0
        # Reference to the current TTS worker task (cancelled on barge-in)
        self._tts_worker_task: Optional[asyncio.Task] = None
        # aplay subprocess reference for laptop mode (killed on barge-in)
        self._tts_player_process: Optional[subprocess.Popen] = None

        # ── Barge-in ──────────────────────────────────────────────────────────
        # Set by _barge_in_task when sustained interruption is confirmed
        self.interrupt_event: asyncio.Event = asyncio.Event()
        # Timestamp of last wake-word detection — used for latency profiling
        self._wake_time: float = 0.0
        # Rolling buffer of mic chunks captured during TTS — contains the
        # user's barge-in question so it isn't lost when we interrupt.
        self._barge_in_preroll: deque = deque(maxlen=BARGE_IN_PREROLL_CHUNKS)
        # RMS energy of the last TTS audio chunk sent to the speaker.
        # Used by the energy gate in _barge_in_task to reject echo.
        self._tts_output_energy: float = 0.0
        # Barge-in is suppressed until this timestamp — set at TTS start
        # to give the speaker time to ramp up without false-triggering.
        self._barge_in_suppress_until: float = 0.0
        # Flag set by _handle_barge_in so PHASE 2 knows to skip ack-drain logic
        self._barge_in_active: bool = False
        # Adaptive noise floor — updated from mic energy during silence.
        # Barge-in threshold = max(noise_floor × SNR_RATIO, BARGE_IN_MIN_MIC_ENERGY)
        self._noise_floor: float = 0.020

        # ── Load configuration ────────────────────────────────────────────────
        app_cfg = load_app_config()
        ww_cfg = app_cfg.get("wake_word", {})

        hw_cfg = get_hardware_config()
        self.hardware_mode = hw_cfg["mode"]
        self.device = os.getenv("MIC_DEVICE") or hw_cfg["mic_device"]
        self.tts_player_extra_args: list = hw_cfg.get("tts_player_extra_args", [])

        # ── PulseAudio WebRTC Echo Cancellation (laptop mode only) ────────────
        # G1 uses direct UDP multicast mic — PulseAudio not in the path.
        if self.hardware_mode == "laptop" and not self.device:
            laptop_cfg = app_cfg.get("laptop", {})
            if laptop_cfg.get("echo_cancel", False):
                from services.hardware.echo_cancel import setup
                ec_source = setup()
                if ec_source:
                    # echocancel is now the PA default source.
                    # Leave self.device = None so sounddevice uses the PA default,
                    # which is now the AEC-filtered mic regardless of PortAudio backend.
                    print(f"[AEC] Using default PA source '{ec_source}' (device=None)")

        print(f"[HARDWARE] mode={self.hardware_mode.upper()}, mic_device={self.device or 'system default'}")

        # ── Wake-word model (faster-whisper) ─────────────────────────────────
        self.wakeword_display = os.getenv("WAKEWORD_DISPLAY", ww_cfg.get("display_name", "Jarvis"))
        _keyword = self.wakeword_display.lower()
        _asr_cfg = app_cfg.get("asr", {})

        try:
            self.wakeword_detector = WhisperWakeWord(
                keyword=_keyword,
                device=_asr_cfg.get("device", "cuda"),
                compute_type=_asr_cfg.get("compute_type", "float16"),
                model_size="tiny.en",
            )
        except Exception as e:
            print(f"\n[NLP ERROR] Failed to load wake-word model: {e}")
            sys.exit(0)

        self._ww_buffer = make_rolling_buffer(WAKEWORD_BLOCK_SIZE)

        # ── Silero VAD ────────────────────────────────────────────────────────
        print("[VAD] Loading Silero VAD...")
        try:
            self.vad_model, _vad_utils = torch.hub.load(
                repo_or_dir='snakers4/silero-vad',
                model='silero_vad',
                trust_repo=True,
            )
            self.vad_model.eval()
            # Silero VAD requires exactly 512 samples at 16kHz per inference
            self._vad_window_samples = 512
            print("[VAD] Silero VAD ready.")
        except Exception as e:
            print(f"[VAD ERROR] Failed to load Silero VAD: {e}")
            print("[VAD] Falling back to energy-only detection.")
            self.vad_model = None
            self._vad_window_samples = 512

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

        # ── Gesture service ───────────────────────────────────────────────────
        # On G1: uses LocoClient over DDS (already initialized by TTS above).
        # On laptop: stub mode — gestures are printed, not executed.
        try:
            if self.hardware_mode == "g1":
                g1_cfg = load_app_config().get("g1", {})
                gestures_enabled = g1_cfg.get("gestures_enabled", True)
                self.gesture_service = GestureService(
                    interface=g1_cfg.get("dds_interface", "eth0"),
                    enabled=gestures_enabled,
                )
                print(f"[GESTURE] gestures_enabled={gestures_enabled}")
            else:
                self.gesture_service = GestureService(enabled=False)
        except Exception as exc:
            print(f"[GESTURE] Disabled: {exc}")
            self.gesture_service = None

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

    def _drain_barge_in_queue(self) -> None:
        """Discard all pending chunks in the barge-in queue."""
        while True:
            try:
                self._barge_in_queue.get_nowait()
            except queue.Empty:
                return

    def _energy(self, chunk: np.ndarray) -> float:
        float_chunk = chunk.astype(np.float32) / 32768.0
        return float(np.sqrt(np.mean(np.square(float_chunk)) + 1e-12))

    def _vad_is_speech(self, chunk: np.ndarray, threshold: float = VAD_SPEECH_THRESHOLD) -> bool:
        """
        Run Silero VAD on a chunk and return True if human speech is detected.
        Silero VAD requires exactly 512 samples at 16kHz — we take the first 512
        samples of each incoming chunk (which is typically 1280 or 2560 samples).
        """
        if self.vad_model is None:
            # Fallback to energy-only
            return self._energy(chunk) >= SPEECH_START_THRESHOLD

        if len(chunk) < self._vad_window_samples:
            return False

        try:
            samples = chunk[:self._vad_window_samples].astype(np.float32) / 32768.0
            tensor = torch.from_numpy(samples)
            with torch.no_grad():
                prob = self.vad_model(tensor, 16000).item()
            return prob >= threshold
        except Exception as e:
            self._debug(f"[VAD] Error: {e} — falling back to energy")
            return self._energy(chunk) >= SPEECH_START_THRESHOLD

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

        # Fan out to barge-in queue unconditionally.
        try:
            self._barge_in_queue.put_nowait(chunk)
        except queue.Full:
            pass

        # Always put into audio_queue so the mic is never starved and
        # preroll_chunks stay warm during TTS. Echo suppression is
        # handled at the PHASE 2 consumer side (skip while is_speaking).
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
            # ── Built-in / DDS mode (G1DirectTTS) ────────────────────────────
            if getattr(self.tts, "is_builtin", False):
                async for _audio_bytes in self.tts.speak(text):
                    # Yield to event loop so barge-in detector can run
                    await asyncio.sleep(0)

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
                self._tts_player_process = player

                try:
                    async for audio_bytes in self.tts.speak(text):
                        if player.stdin:
                            player.stdin.write(audio_bytes)
                        # Yield to event loop so barge-in detector can run
                        await asyncio.sleep(0)
                    if player.stdin:
                        player.stdin.close()
                    # Non-blocking wait — keeps event loop free for barge-in
                    await asyncio.to_thread(lambda: player.wait(timeout=10))
                except BaseException:
                    # Catches CancelledError (barge-in) and regular exceptions.
                    # Kill subprocess so audio stops immediately.
                    player.kill()
                    raise
                finally:
                    self._tts_player_process = None

            self._debug("TTS playback complete")
        except asyncio.CancelledError:
            self._debug("TTS cancelled by barge-in")
            raise
        except Exception as exc:
            print(f"[TTS:ERROR] {exc}")
        finally:
            self.is_speaking = False
            self._tts_stop_time = time.time()  # marks when speaker goes silent
            self._tts_output_energy = 0.0      # reset gate so post-TTS silence isn't gated

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
            except asyncio.CancelledError:
                raise  # finally runs next and calls task_done() exactly once
            finally:
                self.tts_sentence_queue.task_done()

    # ─────────────────────────────────────────────────────────────────────────
    # Barge-in detection — Silero VAD
    # ─────────────────────────────────────────────────────────────────────────

    async def _barge_in_task(self) -> None:
        """
        Continuous background task: fires barge-in when mic energy exceeds
        BARGE_IN_MIN_MIC_ENERGY during TTS playback.

        Pure energy check (no Silero) — same approach as Vocalis.
        Latency: one chunk duration (~80ms) after user starts speaking.
        If the robot starts interrupting itself, raise BARGE_IN_MIN_MIC_ENERGY.
        """
        consecutive_above = 0  # require 2 chunks in a row to avoid single-spike false triggers
        while True:
            try:
                chunk = self._barge_in_queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.005)
                continue

            mic_energy = self._energy(chunk)

            if not self.is_speaking:
                consecutive_above = 0
                # Update noise floor during silence (slow EMA)
                self._noise_floor = (
                    (1 - NOISE_FLOOR_ALPHA) * self._noise_floor
                    + NOISE_FLOOR_ALPHA * mic_energy
                )
                continue

            # Grace period: skip first BARGE_IN_GRACE_SECONDS of each response
            if time.time() < self._barge_in_suppress_until:
                consecutive_above = 0
                continue

            # Always buffer into preroll so speech start is captured before gate fires
            self._barge_in_preroll.append(chunk)

            # Dynamic threshold adapts to ambient noise level
            dynamic_threshold = max(
                self._noise_floor * BARGE_IN_SNR_RATIO,
                BARGE_IN_MIN_MIC_ENERGY,
            )
            if mic_energy >= dynamic_threshold:
                # Silero confirmation: energy gate passed, now verify it's actual
                # human speech (not HVAC, motors, or distant background noise).
                # Falls back to energy-only if Silero model failed to load.
                if self._vad_is_speech(chunk):
                    consecutive_above += 1
                else:
                    consecutive_above = 0
            else:
                consecutive_above = 0

            if consecutive_above >= 2 and not self.interrupt_event.is_set():
                self._debug(
                    f"[BARGE-IN] Energy trigger mic={mic_energy:.3f} "
                    f"threshold={dynamic_threshold:.3f} (floor={self._noise_floor:.3f})"
                )
                print("[BARGE-IN] Interrupting TTS")
                self.interrupt_event.set()
                consecutive_above = 0

    # ─────────────────────────────────────────────────────────────────────────
    # TTS phase: wait for completion or barge-in
    # ─────────────────────────────────────────────────────────────────────────

    async def _wait_for_tts_or_interrupt(self) -> bool:
        """
        Wait for the TTS sentence queue to drain OR for a barge-in interrupt.
        Returns True if interrupted, False if TTS completed normally.
        NOTE: interrupt_event is NOT cleared here — it may have been set during
        LLM generation (while sentence 1 was already playing). Clearing it is
        done at the START of each turn in _reset_turn/interrupt_event.clear().
        """
        # Set grace period ONCE for the whole response, not per sentence.
        self._barge_in_suppress_until = time.time() + BARGE_IN_GRACE_SECONDS

        join_task = asyncio.create_task(self.tts_sentence_queue.join())
        interrupt_task = asyncio.create_task(self.interrupt_event.wait())

        done, pending = await asyncio.wait(
            [join_task, interrupt_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for t in pending:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

        return interrupt_task in done

    async def _handle_barge_in(self) -> None:
        """
        Interrupt controller: kills TTS immediately, flushes queues, and
        re-seeds audio_queue so PHASE 2 can capture the user's question.
        """
        print("[BARGE-IN] Stopping TTS — listening for new command…")
        self._barge_in_active = True

        # 1. Kill aplay subprocess immediately (laptop mode)
        if self._tts_player_process:
            self._tts_player_process.kill()
            self._tts_player_process = None

        # 2. Cancel TTS worker task
        if self._tts_worker_task and not self._tts_worker_task.done():
            self._tts_worker_task.cancel()
            try:
                await self._tts_worker_task
            except asyncio.CancelledError:
                pass

        # 3. Ensure flags are clean
        self.is_speaking = False

        # 4. Flush remaining sentences from the TTS queue
        while not self.tts_sentence_queue.empty():
            try:
                self.tts_sentence_queue.get_nowait()
                self.tts_sentence_queue.task_done()
            except asyncio.QueueEmpty:
                break

        # 5. Short settle — G1 mic/speaker are physically separated so echo is minimal.
        await asyncio.sleep(0.1)

        # 6. Drain echo queue
        self._drain_barge_in_queue()

        # 7. Prepend barge-in preroll to audio_queue (preserves question start).
        # Cap live frames to the most recent ~1s — old frames are TTS echo, not speech.
        preroll_count = len(self._barge_in_preroll)
        current_frames = []
        while True:
            try:
                current_frames.append(self.audio_queue.get_nowait())
            except queue.Empty:
                break
        current_frames = current_frames[-12:]  # keep last ~1s only
        for chunk in self._barge_in_preroll:
            self.audio_queue.put(chunk)
        for chunk in current_frames:
            self.audio_queue.put(chunk)
        self._barge_in_preroll.clear()
        self._debug(
            f"[BARGE-IN] Prepended {preroll_count} preroll + kept {len(current_frames)} live frames"
        )

        # 8. Clear interrupt flag
        self.interrupt_event.clear()

        # 9. Clear conversation history so LLM answers the new question fresh
        self.dialogue_manager.reset_session(self.session_id)

        # 10. Restart TTS worker so it's ready for the next turn
        self._tts_worker_task = asyncio.create_task(self._tts_worker())

        print("[BARGE-IN] Ready — capturing your question…")

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
            "waiting_for_ack": False,
            "from_barge_in": False,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 4 + 5: Transcribe → Dialogue → queue TTS
    # ─────────────────────────────────────────────────────────────────────────

    async def _process_command(self, audio_chunks: list) -> Optional[str]:
        """
        PHASE 4 — ASR transcription (run in thread).
        PHASE 5 — Dialogue manager with sentence-streaming to TTS worker.
        """
        audio = np.concatenate(audio_chunks).astype(np.float32) / 32768.0
        duration = len(audio) / WAKEWORD_SAMPLE_RATE
        self._debug(f"ASR start: samples={len(audio)}, duration={duration:.2f}s")

        # Reject captures that are mostly silence/echo — avoids hallucination
        audio_rms = float(np.sqrt(np.mean(audio ** 2)))
        if audio_rms < 0.010:
            self._debug(f"Audio RMS {audio_rms:.4f} too low — skipping ASR")
            return None

        t0 = time.time()
        print(f"[LATENCY] wake→asr_start={t0 - self._wake_time:.3f}s")
        utterance = await asyncio.to_thread(self.asr.transcribe_sync, audio)
        print(f"[LATENCY] wake→asr_done={time.time() - self._wake_time:.3f}s")
        self._debug(f"ASR done in {time.time() - t0:.2f}s")

        if not utterance.text.strip():
            self._debug("ASR returned empty — going back to listening")
            return None

        print(f"[USER] {utterance.text}")
        self._debug("Dialogue manager processing started")

        streaming_tts = self.tts is not None
        tts_sentences_queued = 0

        async def _counted_queue(sentence: str) -> None:
            nonlocal tts_sentences_queued
            if tts_sentences_queued == 0:
                print(f"[LATENCY] wake→tts_first_sentence={time.time() - self._wake_time:.3f}s")
            await self._queue_tts_sentence(sentence)
            tts_sentences_queued += 1

        # TODO: parallelize intent+entity when DialogueManager exposes them
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

        # Fire gestures concurrently with TTS so the robot moves while speaking
        if self.gesture_service:
            for action in state["extracted_actions"]:
                if action.action_type == ActionType.GESTURE:
                    gesture_name = action.params.get("gesture_name")
                    if gesture_name:
                        asyncio.create_task(self.gesture_service.execute(gesture_name))
                        self._debug(f"Gesture task fired: {gesture_name}")

        response = state["response_text"].strip()
        if response:
            print(f"[{self.wakeword_display}] {response}")

        # Fallback: if sentence splitter produced nothing, play full text directly
        if streaming_tts and tts_sentences_queued == 0 and response:
            self._debug("Streaming produced 0 sentences — falling back to direct play")
            await self._play_tts(response)

        return utterance.text

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 2 inner capture loop — non-blocking async
    # ─────────────────────────────────────────────────────────────────────────

    async def _capture_command(
        self, turn: dict, last_interaction_at_ref: list
    ) -> bool:
        """
        Run the PHASE 2 capture loop. Returns True if capture completed normally,
        False if session timed out. Modifies turn dict in place.
        last_interaction_at_ref is a [float] list for pass-by-reference.
        """
        echo_drained = False

        while True:
            now = time.time()
            elapsed = now - turn["turn_start_time"]

            # Hard timeout
            if elapsed >= MAX_COMMAND_SECONDS:
                self._debug("Hard timeout reached")
                return True

            # PHASE 7: session idle check
            if now - last_interaction_at_ref[0] >= CONVERSATION_TIMEOUT:
                self._debug("Conversation timed out during capture")
                return False

            # Non-blocking queue read
            try:
                chunk = self.audio_queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.01)
                continue

            # ── Echo suppression: skip while TTS is playing ───────────────────
            if self.is_speaking:
                if self.debug_enabled and now - turn["last_debug_at"] >= DEBUG_LOG_INTERVAL_SECONDS:
                    self._debug("PHASE 2: waiting for TTS to finish…")
                    turn["last_debug_at"] = now
                continue

            # ── Post-TTS echo decay window ─────────────────────────────────────
            # Discard audio for ECHO_DECAY_SECONDS after TTS stops, regardless of
            # whether is_speaking was observed True in this turn. This catches the
            # direct-play fallback path where TTS finishes before _capture_command
            # even starts. Barge-in path skips this — it already settled.
            if not self._barge_in_active and self._tts_stop_time > 0:
                time_since_tts = now - self._tts_stop_time
                if time_since_tts < ECHO_DECAY_SECONDS:
                    if self.debug_enabled and now - turn["last_debug_at"] >= DEBUG_LOG_INTERVAL_SECONDS:
                        self._debug(
                            f"Echo decay: {time_since_tts:.2f}/{ECHO_DECAY_SECONDS}s — discarding"
                        )
                        turn["last_debug_at"] = now
                    continue
                # Decay window just expired — flush any echo still in the queue
                if not echo_drained:
                    self._drain_audio_queue()
                    self._drain_barge_in_queue()
                    echo_drained = True
                    turn["turn_start_time"] = time.time()
                    self._debug("Post-TTS echo decay complete — capture starting fresh")
                    continue

            # Clear the barge-in flag on first valid chunk so the next turn
            # is treated as normal. Stamp turn so PHASE 3 can relax the
            # min-speech check for short commands like "stop"/"wait".
            if self._barge_in_active:
                self._barge_in_active = False
                turn["from_barge_in"] = True
                echo_drained = True  # barge-in handler already settled

            # ── Process the chunk ─────────────────────────────────────────────
            energy = self._energy(chunk)
            turn["command_chunks"].append(chunk)

            if self.debug_enabled and now - turn["last_debug_at"] >= DEBUG_LOG_INTERVAL_SECONDS:
                self._debug(
                    f"Capturing: elapsed={elapsed:.2f}s, energy={energy:.4f}, "
                    f"chunks={len(turn['command_chunks'])}"
                )
                turn["last_debug_at"] = now

            # ── VAD: use Silero if available, else energy ─────────────────────
            # For speed during capture we use energy; Silero ran in barge-in path
            is_speech = energy >= SPEECH_START_THRESHOLD

            if is_speech:
                turn["speech_detected"] = True
                turn["speech_chunk_count"] += 1
                turn["silence_start_time"] = None
            elif turn["speech_detected"] and turn["silence_start_time"] is None:
                turn["silence_start_time"] = now
                self._debug("Silence timer started")

            # End of utterance: min duration + silence gap
            if (
                elapsed >= MIN_COMMAND_SECONDS
                and turn["silence_start_time"] is not None
                and now - turn["silence_start_time"] >= SILENCE_TIMEOUT_SECONDS
            ):
                self._debug("Silence timeout — end of utterance")
                return True

    # ─────────────────────────────────────────────────────────────────────────
    # Main loop
    # ─────────────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        print(f"\n[NLP MODULE] Wake word pipeline ready.")
        print(f"Say '{self.wakeword_display}' to activate. Press Ctrl+C to stop.")
        if self.debug_enabled:
            print("[DEBUG] Verbose pipeline logging is enabled.")

        # Start background tasks
        if self.tts is not None:
            self._tts_worker_task = asyncio.create_task(self._tts_worker())

        barge_in_bg_task = asyncio.create_task(self._barge_in_task())

        try:
            # Resolve device: int index or string name
            parsed_device = None
            if self.device:
                try:
                    parsed_device = int(self.device)
                except (ValueError, TypeError):
                    parsed_device = self.device

            # ── Choose Capture Method ─────────────────────────────────────────
            if self.hardware_mode == "g1":
                g1_cfg = load_app_config().get("g1", {})
                local_ip = g1_cfg.get("local_ip", "192.168.123.164")
                interface = g1_cfg.get("dds_interface", "eth0")
                capture_context = G1MulticastStream(
                    self.audio_queue,
                    interface=interface,
                    local_ip=local_ip,
                    extra_queues=[self._barge_in_queue],
                )
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

                    self._ww_buffer.clear()
                    while True:
                        try:
                            chunk = self.audio_queue.get_nowait()
                        except queue.Empty:
                            await asyncio.sleep(0.01)
                            continue

                        now = time.time()
                        energy = self._energy(chunk)
                        self.preroll_chunks.append(chunk)
                        self._ww_buffer.append(chunk)

                        if self.debug_enabled and now - last_debug_at >= DEBUG_LOG_INTERVAL_SECONDS:
                            self._debug(
                                f"Standby: energy={energy:.4f}, "
                                f"queue={self.audio_queue.qsize()}"
                            )
                            last_debug_at = now

                        if self.wakeword_detector.should_run(energy):
                            # Drain any backlogged chunks into buffer before transcribing
                            # so whisper always sees the most recent audio, not stale frames
                            while True:
                                try:
                                    _c = self.audio_queue.get_nowait()
                                    self._ww_buffer.append(_c)
                                    self.preroll_chunks.append(_c)
                                except queue.Empty:
                                    break

                            detected = await self.wakeword_detector.detect_async(
                                list(self._ww_buffer)
                            )
                            if detected:
                                self._wake_time = time.time()
                                print(f"[{self.wakeword_display}] Wake word detected.")
                                break

                    self.preroll_chunks.clear()

                    # ── Sniff for inline question (500ms window) ──────────────
                    # If user said "Hey Jarvis, what time is it?" in one breath,
                    # the question audio arrives immediately after wake detection.
                    # Collect it now; if speech is present skip the ack so PHASE 2
                    # can process it without the ack TTS blocking the queue.
                    _sniff_chunks = []
                    _inline_speech = False
                    _sniff_end = time.time() + 0.5
                    while time.time() < _sniff_end:
                        try:
                            _c = self.audio_queue.get_nowait()
                            _sniff_chunks.append(_c)
                            if self._energy(_c) > SPEECH_START_THRESHOLD:
                                _inline_speech = True
                        except queue.Empty:
                            await asyncio.sleep(0.01)

                    # Put chunks back so PHASE 2 captures them
                    for _c in _sniff_chunks:
                        self.audio_queue.put_nowait(_c)

                    # ── Acknowledge only when no inline question detected ──────
                    if not _inline_speech:
                        ack = "Yes? How can I help you?"
                        print(f"[{self.wakeword_display}] {ack}")
                        if self.tts is not None:
                            self.is_speaking = True
                            self._barge_in_suppress_until = time.time() + BARGE_IN_GRACE_SECONDS
                            asyncio.create_task(self._play_tts(ack))
                    else:
                        print(f"[{self.wakeword_display}] Inline question detected — skipping ack.")

                    last_interaction_at = [time.time()]  # List for pass-by-ref

                    # ════════════════════════════════════════════════════════
                    # PHASE 2–7: Conversational loop (no wake word needed)
                    # ════════════════════════════════════════════════════════
                    in_conversation = True

                    while in_conversation:

                        # ── PHASE 2 — Reset per-turn state ───────────────────
                        turn = self._reset_turn()
                        self.interrupt_event.clear()

                        # ── PHASE 7 check ─────────────────────────────────────
                        if time.time() - last_interaction_at[0] >= CONVERSATION_TIMEOUT:
                            print(
                                f"[NLP MODULE] {int(CONVERSATION_TIMEOUT)}s idle — "
                                f"returning to standby."
                            )
                            in_conversation = False
                            break

                        # ── PHASE 2 — Capture command (async helper) ─────────
                        continued = await self._capture_command(turn, last_interaction_at)
                        if not continued:
                            in_conversation = False
                            break

                        # ── PHASE 3 — Validate: enough speech? ───────────────
                        # After barge-in, short commands ("stop", "wait") may
                        # produce only 1-2 chunks — skip the floor check so they
                        # reach ASR. Normal turns still require MIN_SPEECH_CHUNKS.
                        if not turn["from_barge_in"] and turn["speech_chunk_count"] < MIN_SPEECH_CHUNKS:
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
                            continue

                        if result is None:
                            self._debug("Empty ASR result — continuing to listen")
                            continue

                        last_interaction_at[0] = time.time()

                        # ── Check if user wants to end conversation ───────────
                        session_state = self.dialogue_manager.sessions.get(self.session_id, {})
                        if session_state.get("exit_intent", False):
                            print("[NLP MODULE] User ended conversation. Returning to standby.")
                            in_conversation = False
                            if self.tts is not None:
                                await self.tts_sentence_queue.join()
                            break

                        # ── PHASE 6 — Wait for TTS OR barge-in ───────────────
                        if self.tts is not None:
                            interrupted = await self._wait_for_tts_or_interrupt()
                            if interrupted:
                                await self._handle_barge_in()
                                # _handle_barge_in reset state; go straight to PHASE 2
                                continue

                        pass  # no wakeword state to reset

                    # End of conversational loop — cleanup before PHASE 1
                    self._drain_audio_queue()
                    self._drain_barge_in_queue()
                    self.preroll_chunks.clear()
                    self._barge_in_active = False

        finally:
            # Graceful shutdown
            barge_in_bg_task.cancel()
            try:
                await barge_in_bg_task
            except asyncio.CancelledError:
                pass

            if self._tts_worker_task is not None and not self._tts_worker_task.done():
                await self.tts_sentence_queue.put(None)
                try:
                    await self._tts_worker_task
                except asyncio.CancelledError:
                    pass


async def run_interaction_loop():
    pipeline = LiveAudioPipeline()
    await pipeline.run()


if __name__ == "__main__":
    _ollama_proc = None
    try:
        already_up = subprocess.call(
            ["pgrep", "-x", "ollama"], stdout=subprocess.DEVNULL
        ) == 0
        if already_up:
            print("[OLLAMA] Already running — skipping start.")
        else:
            _ollama_proc = subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(f"[OLLAMA] Started (pid={_ollama_proc.pid})")
        time.sleep(2)  # give ollama a moment to be ready
        asyncio.run(run_interaction_loop())
    except KeyboardInterrupt:
        print("\n[NLP MODULE] Shutting down...")
    except Exception as exc:
        print(f"\n[NLP ERROR] {exc}")
    finally:
        if _ollama_proc and _ollama_proc.poll() is None:
            _ollama_proc.terminate()
            _ollama_proc.wait(timeout=5)
            print("[OLLAMA] Stopped.")