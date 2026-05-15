"""Wake word detection using faster-whisper tiny.en model.

Replaces OpenWakeWord. Strategy:
  - Keep a 2-second rolling audio buffer
  - Energy gate: only run whisper when someone is speaking (avoids wasting GPU on silence)
  - Rate limit: at most one transcription run per 0.5s
  - Keyword check: case-insensitive substring match on transcript
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from typing import List

import numpy as np

SAMPLE_RATE = 16000
BUFFER_SECONDS = 2.0          # rolling window fed to whisper
RUN_INTERVAL = 0.5            # min seconds between whisper calls
MIN_BUFFER_SECONDS = 0.6      # don't run whisper until we have this much audio


class WhisperWakeWord:
    """Faster-whisper based wake word detector."""

    def __init__(
        self,
        keyword: str = "jarvis",
        device: str = "cuda",
        compute_type: str = "float16",
        model_size: str = "tiny.en",
    ):
        self.keyword = keyword.lower()
        # Build fuzzy pattern: matches "jarvis", "jarvis's", "hey jarvis", etc.
        self._pattern = re.compile(
            rf"\b{re.escape(self.keyword)}\w*", re.IGNORECASE
        )

        print(f"[WAKEWORD] Loading faster-whisper '{model_size}' on {device}...")
        from faster_whisper import WhisperModel
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self._last_run: float = 0.0
        print(f"[WAKEWORD] Ready — keyword='{self.keyword}'")

    # ── Public API ────────────────────────────────────────────────────────────

    def should_run(self, energy: float, energy_threshold: float) -> bool:
        """Return True when it's time to run a transcription check."""
        now = time.time()
        return (
            energy >= energy_threshold
            and now - self._last_run >= RUN_INTERVAL
        )

    def detect_sync(self, chunks: List[np.ndarray]) -> bool:
        """
        Transcribe buffered chunks and return True if keyword found.
        Runs on calling thread — use detect_async from an async context.
        """
        if not chunks:
            return False

        total_samples = sum(len(c) for c in chunks)
        if total_samples < int(MIN_BUFFER_SECONDS * SAMPLE_RATE):
            return False

        self._last_run = time.time()

        audio = _to_float32(chunks)
        try:
            segments, _ = self._model.transcribe(
                audio,
                beam_size=1,
                best_of=1,
                language="en",
                condition_on_previous_text=False,
                vad_filter=False,   # we gate externally via energy
            )
            text = " ".join(seg.text for seg in segments).strip()
        except Exception as e:
            print(f"[WAKEWORD] Transcription error: {e}")
            return False

        if text:
            print(f"[WAKEWORD] heard: '{text}'")

        return bool(self._pattern.search(text))

    async def detect_async(self, chunks: List[np.ndarray]) -> bool:
        """Non-blocking version — runs transcription in a thread."""
        return await asyncio.to_thread(self.detect_sync, chunks)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_float32(chunks: List[np.ndarray]) -> np.ndarray:
    """Concatenate int16 chunks and normalise to float32 [-1, 1]."""
    audio = np.concatenate(chunks).astype(np.float32)
    if audio.dtype == np.float32 and audio.max() > 1.0:
        audio /= 32768.0
    return audio


def make_rolling_buffer(block_size: int) -> deque:
    """Create a deque sized to hold BUFFER_SECONDS of audio."""
    maxchunks = int((BUFFER_SECONDS * SAMPLE_RATE) / block_size)
    return deque(maxlen=max(maxchunks, 1))
