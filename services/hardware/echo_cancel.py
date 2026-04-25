"""
PulseAudio WebRTC Acoustic Echo Cancellation setup.

Laptop mode only — G1 uses direct UDP multicast mic which bypasses PulseAudio.

On load, creates two virtual PA devices:
  echocancel        — mic with echo removed  (use this as sounddevice input)
  echocancel_sink   — speaker loopback       (PA uses this as AEC reference)

The WebRTC AEC algorithm is the same one used in Chrome/Firefox (Vocalis relies
on the browser's version; this gives the same algorithm in Python).
"""

import subprocess
import time
from typing import Optional


SOURCE_NAME = "echocancel"
SINK_NAME   = "echocancel_sink"


def _pactl(*args) -> tuple[int, str, str]:
    r = subprocess.run(["pactl", *args], capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _default_source() -> Optional[str]:
    code, out, _ = _pactl("get-default-source")
    if code == 0 and out and SOURCE_NAME not in out:
        return out
    # fallback: parse `pactl info`
    code, out, _ = _pactl("info")
    for line in out.splitlines():
        if "Default Source:" in line:
            name = line.split(":", 1)[1].strip()
            if SOURCE_NAME not in name:
                return name
    return None


def _default_sink() -> Optional[str]:
    code, out, _ = _pactl("get-default-sink")
    if code == 0 and out and SOURCE_NAME not in out:
        return out
    code, out, _ = _pactl("info")
    for line in out.splitlines():
        if "Default Sink:" in line:
            return line.split(":", 1)[1].strip()
    return None


def _is_loaded() -> bool:
    _, out, _ = _pactl("list", "short", "sources")
    return SOURCE_NAME in out


def setup() -> Optional[str]:
    """
    Load PulseAudio WebRTC AEC module if not already active.
    Returns the PA source name to use as mic device, or None on failure.
    Safe to call multiple times.
    """
    if _is_loaded():
        print(f"[AEC] WebRTC echo cancel already active ({SOURCE_NAME}).")
        return SOURCE_NAME

    source = _default_source()
    sink   = _default_sink()

    if not source:
        print("[AEC] Cannot detect default PulseAudio source. Is PulseAudio running?")
        return None
    if not sink:
        print("[AEC] Cannot detect default PulseAudio sink.")
        return None

    print(f"[AEC] Loading WebRTC echo cancel  source={source}  sink={sink}")
    code, mod_id, err = _pactl(
        "load-module", "module-echo-cancel",
        "aec_method=webrtc",
        f"source_master={source}",
        f"sink_master={sink}",
        f"source_name={SOURCE_NAME}",
        f"sink_name={SINK_NAME}",
        "rate=16000",
        "source_properties=device.description=EchoCancelMic",
        "sink_properties=device.description=EchoCancelSpeaker",
    )

    if code != 0:
        print(f"[AEC] Failed to load module-echo-cancel: {err}")
        print("[AEC] Install with:  sudo apt install pulseaudio pulseaudio-utils")
        print("[AEC] Falling back to default mic (no echo cancellation).")
        return None

    time.sleep(0.4)  # give PA time to initialise the new virtual devices

    # Make echocancel the default PA source so sounddevice picks it up
    # automatically when device=None, regardless of PortAudio backend.
    _pactl("set-default-source", SOURCE_NAME)

    print(f"[AEC] Ready. Default source set to '{SOURCE_NAME}' (module id={mod_id})")
    return SOURCE_NAME


def find_device_index(source_name: str) -> Optional[int]:
    """
    Return the sounddevice input device index whose name contains source_name.
    Returns None if not found (caller should fall back to default mic).
    """
    try:
        import sounddevice as sd
        for i, dev in enumerate(sd.query_devices()):
            if source_name.lower() in dev["name"].lower() and dev["max_input_channels"] > 0:
                return i
        return None
    except Exception as e:
        print(f"[AEC] sounddevice query failed: {e}")
        return None
