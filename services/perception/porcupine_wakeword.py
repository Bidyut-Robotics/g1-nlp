"""Wake word detection using Porcupine (Picovoice).

Built-in "jarvis" keyword — ~1ms per frame on ARM, no GPU needed.
Requires a free access key from https://console.picovoice.ai/
"""

from __future__ import annotations

import numpy as np
from typing import List

PORCUPINE_FRAME_SAMPLES = 512   # Porcupine requires exactly 512 samples at 16kHz


class PorcupineWakeWord:
    """Porcupine-based wake word detector."""

    def __init__(self, access_key: str, keyword: str = "jarvis"):
        import pvporcupine
        self._porcupine = pvporcupine.create(
            access_key=access_key,
            keywords=[keyword],
        )
        self._leftover = np.array([], dtype=np.int16)
        print(f"[WAKEWORD] Porcupine ready — keyword='{keyword}', "
              f"frame={PORCUPINE_FRAME_SAMPLES} samples")

    def process_chunk(self, chunk: np.ndarray) -> bool:
        """
        Feed a raw int16 audio chunk. Returns True the moment the wake word fires.
        Handles arbitrary chunk sizes by buffering internally.
        """
        samples = chunk.astype(np.int16).flatten()
        buf = np.concatenate([self._leftover, samples])

        detected = False
        offset = 0
        while offset + PORCUPINE_FRAME_SAMPLES <= len(buf):
            frame = buf[offset: offset + PORCUPINE_FRAME_SAMPLES]
            result = self._porcupine.process(frame)
            if result >= 0:
                detected = True
                break
            offset += PORCUPINE_FRAME_SAMPLES

        self._leftover = buf[offset:] if not detected else np.array([], dtype=np.int16)
        return detected

    def reset(self):
        self._leftover = np.array([], dtype=np.int16)

    def delete(self):
        self._porcupine.delete()
