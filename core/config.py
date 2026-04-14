import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict


DEFAULT_CONFIG_PATH = Path(os.getenv("APP_CONFIG_PATH", "config/app_config.json"))
DEFAULT_CONFIG: Dict[str, Any] = {
    "llm": {
        "mode": "local",
        "model_name": "llama3.2",
        "base_url": "http://localhost:11434",
        "temperature": 0.2,
        "num_predict": 80,
        "num_ctx": 1024,
        "keep_alive": "30m",
    },
    "tts": {
        "mode": "local",
        "model_path": "models/en_US-lessac-medium.onnx",
        "player": "aplay",
    }
}


@lru_cache(maxsize=1)
def load_app_config(config_path: str | None = None) -> Dict[str, Any]:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        return DEFAULT_CONFIG.copy()

    with path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)

    config = DEFAULT_CONFIG.copy()
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            merged = config[key].copy()
            merged.update(value)
            config[key] = merged
        else:
            config[key] = value
    return config


def get_llm_config() -> Dict[str, Any]:
    config = load_app_config()
    return config.get("llm", {}).copy()


def get_tts_config() -> Dict[str, Any]:
    config = load_app_config()
    return config.get("tts", {}).copy()


def get_hardware_config() -> Dict[str, Any]:
    """
    Returns the resolved audio hardware settings based on the configured hardware_mode.

    In 'laptop' mode (default / quick dev):
      - mic_device: None  (sounddevice picks the system default)
      - tts_player_args: ["aplay", ...]  (plays through laptop speaker)

    In 'g1' mode (robot):
      - mic_device: 'g1_microphone'  (PulseAudio source created by g1_audio_driver.py)
      - tts_player_args: ["aplay", "-D", "alsa_output.g1_speaker", ...]
        (routes audio to the G1 speaker via the PulseAudio sink)

    Prerequisites for 'g1' mode:
      1. Run: python services/hardware/g1_audio_driver.py
      2. Verify PulseAudio sources: pactl list sources short
    """
    config = load_app_config()
    mode = os.getenv("HARDWARE_MODE", config.get("hardware_mode", "laptop")).lower()
    g1_cfg = config.get("g1", {})
    tts_cfg = config.get("tts", {})
    base_player = tts_cfg.get("player", "aplay")

    if mode == "g1":
        # In Docker/G1 mode, we use the system default audio device (None)
        # and let the PULSE_SINK/PULSE_SOURCE environment variables handle the routing.
        return {
            "mode": "g1",
            "mic_device": None, # Use system default
            "tts_player": base_player,
            "tts_player_extra_args": [],
        }

    # Default: laptop
    return {
        "mode": "laptop",
        "mic_device": None,
        "tts_player": base_player,
        "tts_player_extra_args": [],
    }
