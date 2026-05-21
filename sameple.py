#!/usr/bin/env python3

import queue
import re
import sys
import time

import numpy as np
import sounddevice as sd

from faster_whisper import WhisperModel
from rapidfuzz import fuzz

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------

SAMPLE_RATE = 16000
CHANNELS = 1

OWW_CHUNK = 1280
WW_THRESHOLD = 0.3
WW_KEY = "hey_jarvis"

RECORD_SECONDS = 3.0

# -------------------------------------------------------------------
# AUDIO QUEUE
# -------------------------------------------------------------------

audio_q = queue.Queue(maxsize=200)

def audio_callback(indata, frames, time_info, status):
    if status:
        print(status)

    audio = (indata[:, 0] * 32767).astype(np.int16)

    if audio_q.full():
        try:
            audio_q.get_nowait()
        except queue.Empty:
            pass

    audio_q.put(audio)

# -------------------------------------------------------------------
# OPENWAKEWORD
# -------------------------------------------------------------------

# Prevent Jetson/onnxruntime issues on some systems
import sys as _sys
import types as _types

if "onnxruntime" not in _sys.modules:
    _ort_stub = _types.ModuleType("onnxruntime")

    class _OrtInferenceSession:
        def __init__(self, *a, **kw):
            raise RuntimeError("onnxruntime disabled")

    _ort_stub.InferenceSession = _OrtInferenceSession
    _sys.modules["onnxruntime"] = _ort_stub

from openwakeword.model import Model as OWWModel

print("[INFO] Loading wake word model...")
oww = OWWModel(
    wakeword_models=["hey_jarvis"],
    inference_framework="tflite"
)

print("[INFO] Wake word ready.")

# -------------------------------------------------------------------
# WHISPER
# -------------------------------------------------------------------

print("[INFO] Loading Whisper model...")

model = WhisperModel(
    "distil-large-v3",
    device="cuda",
    compute_type="float16"
)

print("[INFO] Whisper ready.")

# -------------------------------------------------------------------
# COMMANDS
# -------------------------------------------------------------------

def cmd_handshake():
    print("🤝 HANDSHAKE")

def cmd_forward():
    print("⬆️ MOVE FORWARD")

def cmd_backward():
    print("⬇️ MOVE BACKWARD")

def cmd_wave():
    print("👋 WAVE HAND")

COMMANDS = {
    "handshake": cmd_handshake,
    "hand shake": cmd_handshake,
    "move forward": cmd_forward,
    "forward": cmd_forward,
    "move backward": cmd_backward,
    "backward": cmd_backward,
    "wave": cmd_wave,
    "wave hand": cmd_wave,
    "hello": cmd_wave,
    "hi": cmd_wave,
}

# -------------------------------------------------------------------
# FUZZY DISPATCH
# -------------------------------------------------------------------

def dispatch(transcript: str):

    t = re.sub(r"[^\x00-\x7F]+", "", transcript)
    t = re.sub(r"[.,!?'\"]", "", t).lower().strip()

    print(f"[CLEANED] {t}")

    if not t:
        return False

    best_score = 0
    best_command = None

    for keyword in COMMANDS.keys():

        score = fuzz.token_set_ratio(keyword, t)

        print(f"[MATCH] {keyword} <-> {t} = {score}")

        if score > best_score:
            best_score = score
            best_command = keyword

    print(f"[BEST] {best_command} ({best_score})")

    if best_score >= 72:

        print(f"[EXECUTE] {best_command}")

        COMMANDS[best_command]()

        return True

    return False

# -------------------------------------------------------------------
# HELPERS
# -------------------------------------------------------------------

def drain_queue():
    while not audio_q.empty():
        try:
            audio_q.get_nowait()
        except queue.Empty:
            break

# -------------------------------------------------------------------
# MAIN
# -------------------------------------------------------------------

print("\n===================================")
print("Say: 'Hey Jarvis'")
print("===================================\n")

stream = sd.InputStream(
    samplerate=SAMPLE_RATE,
    channels=CHANNELS,
    dtype=np.float32,
    blocksize=OWW_CHUNK,
    callback=audio_callback,
)

stream.start()

try:

    while True:

        # -----------------------------------------------------------
        # WAIT FOR WAKE WORD
        # -----------------------------------------------------------

        consec = 0

        while True:

            chunk = audio_q.get()

            scores = oww.predict(chunk)

            score = float(
                scores.get(
                    WW_KEY,
                    max(scores.values(), default=0.0)
                )
            )

            print(f"[WAKE SCORE] {score:.3f}", end="\r")

            if score >= WW_THRESHOLD:
                consec += 1
            else:
                consec = 0

            if consec >= 2:
                print("\n\n[WAKE WORD DETECTED]\n")
                oww.reset()
                break

        # -----------------------------------------------------------
        # RECORD COMMAND
        # -----------------------------------------------------------

        drain_queue()

        print("🎤 Listening...")

        frames = []

        deadline = time.time() + RECORD_SECONDS

        while time.time() < deadline:

            try:
                frames.append(audio_q.get(timeout=0.1))
            except queue.Empty:
                continue

        if not frames:
            print("[NO AUDIO]")
            continue

        # -----------------------------------------------------------
        # PREPROCESS
        # -----------------------------------------------------------

        audio_np = np.concatenate(frames).astype(np.float32)

        audio_np = audio_np / 32768.0

        audio_np = audio_np / max(
            np.abs(audio_np).max(),
            1e-6
        )

        # -----------------------------------------------------------
        # TRANSCRIBE
        # -----------------------------------------------------------

        print("🧠 Transcribing...")

        segments, info = model.transcribe(
            audio_np,
            language="en",
            beam_size=5,
            vad_filter=True,
            temperature=0.0
        )

        transcript = " ".join(
            segment.text for segment in segments
        ).strip()

        print(f"\n[TRANSCRIPT] {transcript}\n")

        # -----------------------------------------------------------
        # DISPATCH
        # -----------------------------------------------------------

        ok = dispatch(transcript)

        if not ok:
            print("❌ Command not understood\n")

except KeyboardInterrupt:

    print("\nBye 👋")

finally:

    stream.stop()
    stream.close()