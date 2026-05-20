#!/usr/bin/env python3
"""
demo_gestures.py — Standalone voice-command demo for G1 robot.

Runs entirely on the robot's onboard computer. No AGX, no Ollama, no LLM.

Usage:
    python3 demo_gestures.py [network_interface]
    python3 demo_gestures.py eth0

Wake word : "Hey Jarvis"
Commands  : "handshake"    → shake hand + speak
            "move forward" → walk forward + speak
            "move backward"→ walk backward + speak

Requirements:
    pip install openwakeword faster-whisper sounddevice numpy
    unitree_sdk2py must be installed
"""

import json
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import queue

import numpy as np

# ── Config ────────────────────────────────────────────────────────────────────
NETWORK_INTERFACE = sys.argv[1] if len(sys.argv) > 1 else "eth0"
MULTICAST_GROUP   = "239.168.123.161"
MULTICAST_PORT    = 5555
OWW_CHUNK         = 1280        # OpenWakeWord: 80 ms at 16 kHz
SAMPLE_RATE       = 16000
WW_THRESHOLD      = 0.5
WW_KEY            = "hey_jarvis"
RECORD_SECONDS    = 4.0
MOVE_DURATION     = 2.0

API_SET_MODE      = 1008        # Unitree voice service: mic control


def get_local_ip(interface: str) -> str:
    try:
        out = subprocess.check_output(["ip", "-4", "addr", "show", interface], text=True)
        for line in out.splitlines():
            if "inet " in line:
                return line.strip().split()[1].split("/")[0]
    except Exception:
        pass
    return "127.0.0.1"


LOCAL_IP = get_local_ip(NETWORK_INTERFACE)
print(f"[DEMO] Interface={NETWORK_INTERFACE}, local_ip={LOCAL_IP}")

# ── DDS / SDK init ────────────────────────────────────────────────────────────
from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient
from unitree_sdk2py.g1.arm.g1_arm_action_client import G1ArmActionClient, action_map
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
from unitree_sdk2py.rpc.client import Client

print(f"[DEMO] Initializing DDS on {NETWORK_INTERFACE} ...")
ChannelFactoryInitialize(0, NETWORK_INTERFACE)
time.sleep(0.5)

loco = LocoClient()
loco.SetTimeout(10.0)
loco.Init()

arm = G1ArmActionClient()
arm.SetTimeout(10.0)
arm.Init()

audio_client = AudioClient()
audio_client.Init()
audio_client.SetVolume(100)

# ── Mic activation ────────────────────────────────────────────────────────────
voice_client = Client("voice", False)
voice_client.SetTimeout(5.0)
voice_client._SetApiVerson("1.0.0.0")
voice_client._RegistApi(API_SET_MODE, 0)

def mic_set_mode(mode: int):
    code, _ = voice_client._Call(API_SET_MODE, json.dumps({"mode": mode}))
    return code

print("[DEMO] Activating microphone ...")
code = mic_set_mode(1)   # 1 = active
if code != 0:
    print(f"[DEMO] Warning: mic activation returned code={code}")
else:
    print("[DEMO] Microphone active.")

def _cleanup(sig=None, frame=None):
    print("\n[DEMO] Shutting down — deactivating mic ...")
    mic_set_mode(2)   # 2 = idle
    sys.exit(0)

signal.signal(signal.SIGINT,  _cleanup)
signal.signal(signal.SIGTERM, _cleanup)

# ── Multicast mic receiver ────────────────────────────────────────────────────
_audio_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=200)

def _mic_thread():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    sock.bind(('', MULTICAST_PORT))

    try:
        mreq = struct.pack("4s4s", socket.inet_aton(MULTICAST_GROUP),
                           socket.inet_aton(LOCAL_IP))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except Exception as e:
        print(f"[DEMO] Multicast join failed on {LOCAL_IP}, trying INADDR_ANY: {e}")
        mreq = struct.pack("4sl", socket.inet_aton(MULTICAST_GROUP), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    sock.settimeout(1.0)
    print(f"[DEMO] Mic listening on {MULTICAST_GROUP}:{MULTICAST_PORT}")

    buf = np.array([], dtype=np.int16)
    while True:
        try:
            data, _ = sock.recvfrom(8192)
            packet = np.frombuffer(data, dtype=np.int16)
            buf = np.concatenate([buf, packet])
            while len(buf) >= OWW_CHUNK:
                chunk, buf = buf[:OWW_CHUNK].copy(), buf[OWW_CHUNK:]
                if _audio_q.full():
                    try:
                        _audio_q.get_nowait()
                    except queue.Empty:
                        pass
                _audio_q.put_nowait(chunk)
        except socket.timeout:
            continue
        except Exception as exc:
            print(f"[DEMO MIC ERROR] {exc}")
            break

threading.Thread(target=_mic_thread, daemon=True).start()
time.sleep(0.5)

# ── Wake word model ───────────────────────────────────────────────────────────
from openwakeword.model import Model as OWWModel
print("[DEMO] Loading OpenWakeWord (hey_jarvis) ...")
oww = OWWModel(wakeword_models=["hey_jarvis"], inference_framework="onnx")
print("[DEMO] Wake word ready.")

# ── ASR model ─────────────────────────────────────────────────────────────────
from faster_whisper import WhisperModel
print("[DEMO] Loading Whisper tiny (CPU) ...")
asr = WhisperModel("tiny.en", device="cpu", compute_type="int8")
print("[DEMO] ASR ready.")

# ── TTS ───────────────────────────────────────────────────────────────────────
def say(text: str, wait: float = 2.5):
    print(f"[DEMO] Say: {text}")
    ret = audio_client.TtsMaker(text, 1)
    if ret != 0:
        print(f"[DEMO TTS ERROR] TtsMaker returned {ret}")
    time.sleep(wait)

# ── Gestures ──────────────────────────────────────────────────────────────────
def do_handshake():
    arm.ExecuteAction(action_map.get("shake hand"))
    time.sleep(2.5)
    arm.ExecuteAction(action_map.get("release arm"))

def do_forward():
    loco.Move(0.3, 0, 0)
    time.sleep(MOVE_DURATION)
    loco.Move(0, 0, 0)

def do_backward():
    loco.Move(-0.3, 0, 0)
    time.sleep(MOVE_DURATION)
    loco.Move(0, 0, 0)

COMMANDS = {
    "handshake": (do_handshake, "Extend your hand for handshake"),
    "shake":     (do_handshake, "Extend your hand for handshake"),
    "forward":   (do_forward,   "Moving forward"),
    "backward":  (do_backward,  "Moving backward"),
}

def dispatch(transcript: str) -> bool:
    t = transcript.lower()
    for keyword, (fn, response) in COMMANDS.items():
        if keyword in t:
            say(response, wait=1.5)
            fn()
            return True
    return False

def drain_queue():
    while not _audio_q.empty():
        try:
            _audio_q.get_nowait()
        except queue.Empty:
            break

# ── Main loop ─────────────────────────────────────────────────────────────────
print("\n[DEMO] Ready. Say 'Hey Jarvis' to activate.\n")

while True:
    # PHASE 1: wait for wake word
    consec = 0
    _log_counter = 0
    while True:
        chunk = _audio_q.get()
        energy = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
        scores = oww.predict(chunk)
        score = float(scores.get(WW_KEY, max(scores.values(), default=0.0)))

        _log_counter += 1
        if score >= 0.1 or _log_counter % 25 == 0:  # always log if score notable, else every ~2s
            print(f"[STANDBY] score={score:.3f}  energy={energy:.1f}", flush=True)

        if score >= WW_THRESHOLD:
            consec += 1
        else:
            consec = 0
        if consec >= 2:
            print(f"[DEMO] Wake word! score={score:.3f}")
            oww.reset()
            break

    drain_queue()

    # PHASE 2: record command
    say("Yes?", wait=0.8)
    print(f"[DEMO] Listening for command ({RECORD_SECONDS}s) ...")
    frames = []
    deadline = time.time() + RECORD_SECONDS
    while time.time() < deadline:
        try:
            frames.append(_audio_q.get(timeout=0.1))
        except queue.Empty:
            continue

    if not frames:
        say("I didn't hear anything. Please try again.")
        continue

    audio_np = np.concatenate(frames).astype(np.float32) / 32768.0
    segments, _ = asr.transcribe(audio_np, language="en", beam_size=1)
    transcript = " ".join(seg.text for seg in segments).strip()
    print(f"[DEMO] Heard: '{transcript}'")

    if not dispatch(transcript):
        say("Sorry, I did not understand. Try saying: handshake, move forward, or move backward.")
