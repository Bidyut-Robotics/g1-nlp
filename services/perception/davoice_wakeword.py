"""Wake word detection using DaVoice keyword_detection_lib.

99.2% accuracy, aarch64 native, external audio API.
Model: request "jarvis" or "hey jarvis" .onnx from info@davoice.io
License key: already in Python_WakeWordDetection/example/licensekey.txt
"""

from __future__ import annotations

import threading
import numpy as np


class DaVoiceWakeWord:
    """DaVoice keyword detector using external audio feed API."""

    def __init__(self, model_path: str, license_key: str, threshold: float = 0.9, buffer_cnt: int = 4):
        from keyword_detection import KeywordDetection

        self._detected = threading.Event()

        keyword_detection_models = [
            {
                "model_path": model_path,
                "callback_function": self._on_detection,
                "threshold": threshold,
                "buffer_cnt": buffer_cnt,
                "wait_time": 50,
            }
        ]

        self._model = KeywordDetection(keyword_models=keyword_detection_models)
        self._model.set_keyword_detection_license(license_key)
        self._model.start_keyword_detection_external_audio(enable_vad=False, buffer_ms=100)
        print(f"[WAKEWORD] DaVoice ready — model='{model_path}', threshold={threshold}")

    def _on_detection(self, params):
        phrase  = params.get("phrase", "")
        scores  = params.get("threshold_scores", [])
        print(f"[WAKEWORD] Detected: '{phrase}' scores={[s for s in scores if s]}")
        self._detected.set()

    def process_chunk(self, chunk: np.ndarray) -> bool:
        """Feed an int16 audio chunk. Returns True if wake word fired."""
        if self._model.is_listening:
            self._model.feed_audio_frame(chunk.astype(np.int16))
        if self._detected.is_set():
            self._detected.clear()
            return True
        return False

    def reset(self):
        self._detected.clear()
