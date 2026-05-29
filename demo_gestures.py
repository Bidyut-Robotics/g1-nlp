#!/usr/bin/env python3
"""
demo_gestures.py — Standalone voice-command demo for G1 robot.

Runs entirely on the robot's onboard computer. No AGX, no Ollama, no LLM.

Usage:
    python3 demo_gestures.py [network_interface]
    python3 demo_gestures.py eth0

Wake word : "Alexa"
Commands  : "handshake"    → shake hand + speak
            "move forward" → walk forward + speak
            "move backward"→ walk backward + speak
            "wave"         → wave hand + speak

Requirements:
    pip install openwakeword numpy
    unitree_sdk2py must be installed
"""

#!/usr/bin/env python3
import os

# Fix for scikit-learn OpenMP TLS allocation error (commented out as it's from a different environment)
# os.environ['LD_PRELOAD'] = '/home/unitree/miniconda3/envs/demo/lib/python3.10/site-packages/scikit_learn.libs/libgomp-947d5fa1.so.1.0.0'

import json
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import queue
import torch
import numpy as np
# from transformers import MoonshineForConditionalGeneration, AutoProcessor

# ── Config ────────────────────────────────────────────────────────────────────
NETWORK_INTERFACE = sys.argv[1] if len(sys.argv) > 1 else "eth0"
MULTICAST_GROUP   = "239.168.123.161"
MULTICAST_PORT    = 5555
OWW_CHUNK         = 1280        # 80 ms at 16 kHz
SAMPLE_RATE       = 16000
WW_THRESHOLD      = 0.3      # from eval: optimal_threshold
WAKEWORD_MODEL    = "./hey_daksh.onnx"
MAX_RECORD_SECONDS = 8.0      # absolute cap for VAD-based recording
SPEECH_TIMEOUT_S  = 0.7       # silence after speech ends → stop recording
VAD_THRESHOLD     = 0.025     # normalized RMS energy threshold (above background ~0.012)
FUZZY_THRESHOLD   = 72        # rapidfuzz partial_ratio min score (0–100)
MOVE_DURATION     = 2.0

API_SET_MODE      = 1008        # Unitree voice service: mic control

LED_GREEN  = (0, 255, 0)
LED_BLUE   = (0, 0, 255)
LED_OFF    = (0, 0, 0)


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
from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_
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

previous_f3 = 0
is_active = False

def low_state_handler(msg):
    global previous_f3, is_active
    wireless_remote = msg.wireless_remote
    if len(wireless_remote) < 4:
        return

    f3_pressed = (wireless_remote[2] >> 7) & 1
    if f3_pressed and not previous_f3:
        is_active = not is_active
        state_str = "ACTIVE" if is_active else "PAUSED"
        print(f"\n[DEMO] Remote toggle: NLP code is now {state_str}")
        if is_active:
            print("[DEMO] Ready. Say 'Hey Daksh' to activate.")
    previous_f3 = f3_pressed

lowstate_subscriber = ChannelSubscriber("rt/lf/lowstate", LowState_)
lowstate_subscriber.Init(low_state_handler, 10)

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

def led(r: int, g: int, b: int):
    try:
        audio_client.LedControl(r, g, b)
    except Exception as e:
        print(f"[DEMO LED ERROR] {e}")

def _cleanup(sig=None, frame=None):
    print("\n[DEMO] Shutting down ...")
    led(*LED_OFF)
    mic_set_mode(2)   # 2 = idle
    sys.exit(0)

signal.signal(signal.SIGINT,  _cleanup)
signal.signal(signal.SIGTERM, _cleanup)

# ── Moonshine ASR (moonshine-streaming-medium) ───────────────────────────────
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor

_ms_device     = "cuda" if torch.cuda.is_available() else "cpu"
_ms_dtype      = torch.float16 if torch.cuda.is_available() else torch.float32

if os.path.exists("./moonshine-streaming-medium"):
    _MS_MODEL_PATH = "./moonshine-streaming-medium"
    _local_files_only = True
else:
    _MS_MODEL_PATH = "UsefulSensors/moonshine-streaming-medium"
    _local_files_only = False

print(f"[DEMO] Loading Moonshine streaming-medium ({_ms_device}) from {_MS_MODEL_PATH} ...")
_ms_model = AutoModelForSpeechSeq2Seq.from_pretrained(
    _MS_MODEL_PATH,
    dtype=_ms_dtype,
    local_files_only=_local_files_only,
    trust_remote_code=True,
).to(_ms_device)
_ms_proc = AutoProcessor.from_pretrained(
    _MS_MODEL_PATH,
    local_files_only=_local_files_only,
    trust_remote_code=True,
)
print("[DEMO] ASR ready.")

def _transcribe(audio_np: np.ndarray) -> str:
    inputs = _ms_proc(audio_np, return_tensors="pt", sampling_rate=SAMPLE_RATE)
    # Cast float tensors to model dtype; keep attention_mask as int
    inputs = {
        k: v.to(device=_ms_device, dtype=_ms_dtype if v.is_floating_point() else v.dtype)
        for k, v in inputs.items()
    }
    duration       = len(audio_np) / SAMPLE_RATE
    max_new_tokens = max(int(duration * 6.5), 16)
    generated_ids  = _ms_model.generate(**inputs, max_new_tokens=max_new_tokens)
    return _ms_proc.decode(generated_ids[0], skip_special_tokens=True).strip()
# ── Energy VAD (no extra deps — robust enough for controlled robotics env) ────
def _vad_prob(chunk: np.ndarray) -> float:
    return float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2))) / 32768.0

# ── Multicast mic receiver (for OWW wake word only) ──────────────────────────
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

# ── Wake word model (livekit-wakeword) ────────────────────────────────────────
from livekit.wakeword import WakeWordModel

if not os.path.exists(WAKEWORD_MODEL):
    raise FileNotFoundError(
        f"Wake word model not found: {WAKEWORD_MODEL}\n"
        "Expected: hey_daksh.onnx in the same directory as this script."
    )

print(f"[DEMO] Loading livekit wake word model: {WAKEWORD_MODEL} ...")
_ww_model = WakeWordModel(models=[WAKEWORD_MODEL])
# Rolling 2-second buffer — livekit-wakeword is stateless, needs full window each call
_ww_buffer = np.zeros(SAMPLE_RATE * 2, dtype=np.float32)
print("[DEMO] Wake word ready.")

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

def do_hand_wave():
    arm.ExecuteAction(action_map.get("high wave"))
    time.sleep(3.0)
    arm.ExecuteAction(action_map.get("release arm"))

COMMANDS = {
    "handshake":          (do_handshake, "Extend your hand for handshake"),
    "shake hand":          (do_handshake, "Extend your hand for handshake"),
    "hand shake":         (do_handshake, "Extend your hand for handshake"),
    "handshake":         (do_handshake, "Extend your hand for handshake"),
    "shake":              (do_handshake, "Extend your hand for handshake"),
    "give hand shake":    (do_handshake, "Extend your hand for handshake"),
    "give handshake":     (do_handshake, "Extend your hand for handshake"),
    "give me hand shake": (do_handshake, "Extend your hand for handshake"),
    "give me handshake":  (do_handshake, "Extend your hand for handshake"),
    "forward":            (do_forward,   "Moving forward"),
    "move forward":       (do_forward,   "Moving forward"),
    "backward":           (do_backward,  "Moving backward"),
    "move backward":      (do_backward,  "Moving backward"),
    "wave hand":          (do_hand_wave, "Sure, waving hand!"),
    "hand wave":          (do_hand_wave, "Sure, waving hand!"),
    "wave":               (do_hand_wave, "Sure, waving hand!"),
    "hi":                 (do_hand_wave, "Hello! Nice to meet you."),
    "hello":              (do_hand_wave, "Hello! Nice to meet you."),
    "bye":                (do_hand_wave, "Goodbye! See you soon."),
    "good bye":           (do_hand_wave, "Goodbye! See you soon."),
    "goodbye":            (do_hand_wave, "Goodbye! See you soon."),
}

from rapidfuzz import process as _fuzz_process, fuzz as _fuzz

def _word_match(keyword: str, transcript: str) -> bool:
    # Match whole words only — prevents "hi" matching inside "chicken", "this", etc.
    return bool(re.search(r'\b' + re.escape(keyword) + r'\b', transcript))

def dispatch(transcript: str) -> bool:
    t = re.sub(r"[^\x00-\x7F]+", "", transcript)
    t = re.sub(r"[.,!?'\"]", "", t).lower().strip()
    if not t:
        return False

    # Exact: keyword as whole word(s) in transcript
    for keyword, (fn, response) in COMMANDS.items():
        if _word_match(keyword, t):
            print(f"[DEMO] Exact match: '{t}' → '{keyword}'")
            say(response, wait=1.5)
            fn()
            return True

    # Partial: transcript is a whole-word prefix of a keyword
    # e.g. "hand" → "handshake" (min 4 chars to avoid noise)
    if len(t) >= 4:
        for keyword, (fn, response) in COMMANDS.items():
            if keyword.startswith(t) or t in keyword.split():
                print(f"[DEMO] Partial match: '{t}' ⊆ '{keyword}'")
                say(response, wait=1.5)
                fn()
                return True

    # Fuzzy: catch ASR typos ("foreward" → "forward", "handschake" → "handshake")
    # result = _fuzz_process.extractOne(t, list(COMMANDS.keys()), scorer=_fuzz.WRatio)
    result = _fuzz_process.extractOne(t, list(COMMANDS.keys()), scorer=_fuzz.ratio)
    if result and result[1] >= FUZZY_THRESHOLD:
        keyword, score = result[0], result[1]
        fn, response = COMMANDS[keyword]
        print(f"[DEMO] Fuzzy match: '{t}' → '{keyword}' ({score}%)")
        say(response, wait=1.5)
        fn()
        return True

    print(f"[DEMO] No match for: '{t}'")
    return False

def drain_queue():
    while not _audio_q.empty():
        try:
            _audio_q.get_nowait()
        except queue.Empty:
            break

# ── Main loop ─────────────────────────────────────────────────────────────────
print("\n[DEMO] Initialized. Press F3 on the remote controller to START/STOP NLP listening.\n")

while True:
    if not is_active:
        drain_queue()
        time.sleep(0.1)
        continue

    # PHASE 1: wait for wake word
    consec = 0
    _log_counter = 0
    wake_word_detected = False
    while is_active:
        try:
            chunk = _audio_q.get(timeout=0.1)
        except queue.Empty:
            continue

        # Slide rolling buffer and append new chunk (converted to float32)
        chunk_f32 = chunk.astype(np.float32) / 32768.0
        _ww_buffer = np.roll(_ww_buffer, -len(chunk_f32))
        _ww_buffer[-len(chunk_f32):] = chunk_f32

        scores = _ww_model.predict(_ww_buffer)
        score = max(scores.values()) if scores else 0.0
        energy = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))

        _log_counter += 1
        if score >= 0.1 or _log_counter % 25 == 0:
            print(f"[STANDBY] score={score:.3f}  energy={energy:.1f}", flush=True)

        if score >= WW_THRESHOLD:
            consec += 1
        else:
            consec = 0
        if consec >= 1:
            print(f"[DEMO] Wake word! score={score:.3f}")
            # Reset rolling buffer so stale audio doesn't re-trigger
            _ww_buffer[:] = 0.0
            wake_word_detected = True
            break
            
    if not wake_word_detected:
        drain_queue()
        continue

    drain_queue()

    # PHASE 2: respond immediately, then record command
    say("Yes?", wait=0.5)
    drain_queue()   # flush TTS echo before listening

    _silence_limit = max(1, round(SPEECH_TIMEOUT_S * SAMPLE_RATE / OWW_CHUNK))
    frames = []
    speech_started = False
    speech_consec  = 0      # consecutive above-threshold chunks to confirm real speech vs spike
    silence_chunks = 0
    deadline = time.time() + MAX_RECORD_SECONDS

    print(f"[DEMO] Listening for command (VAD, max {MAX_RECORD_SECONDS}s) ...")
    while time.time() < deadline and is_active:
        try:
            chunk = _audio_q.get(timeout=0.1)
        except queue.Empty:
            continue
        frames.append(chunk)
        if _vad_prob(chunk) >= VAD_THRESHOLD:
            speech_consec += 1
            if speech_consec >= 3:   # ~240 ms sustained above threshold = real speech
                speech_started = True
            silence_chunks = 0
        else:
            speech_consec = 0
            if speech_started:
                silence_chunks += 1
                if silence_chunks >= _silence_limit:
                    print(f"[DEMO] VAD: speech ended ({len(frames)} chunks)")
                    break

    if not frames:
        transcript = ""
    else:
        audio_np = np.concatenate(frames).astype(np.float32) / 32768.0
        transcript = _transcribe(audio_np)

    print(f"[DEMO] Heard: '{transcript}'")

    if not transcript:
        say("I didn't hear anything. Please try again.")
        continue

    if not dispatch(transcript):
        say("Sorry, I did not understand. Try again.")