"""Wake word detection using sherpa-onnx streaming keyword spotter.

Purpose-built keyword spotting — not a transcription model.
Free, no account, ARM-native, ~3.3M parameter model.

Model download:
  wget https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2
  tar xf sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2
"""

from __future__ import annotations

import numpy as np

SAMPLE_RATE = 16000


class SherpaWakeWord:
    """Streaming keyword spotter using sherpa-onnx."""

    def __init__(self, model_dir: str, keyword: str = "jarvis", num_threads: int = 2):
        import sherpa_onnx
        import os

        tokens   = os.path.join(model_dir, "tokens.txt")
        encoder  = os.path.join(model_dir, "encoder-epoch-12-avg-2-chunk-16-left-64.onnx")
        decoder  = os.path.join(model_dir, "decoder-epoch-12-avg-2-chunk-16-left-64.onnx")
        joiner   = os.path.join(model_dir, "joiner-epoch-12-avg-2-chunk-16-left-64.onnx")

        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=tokens,
            encoder=encoder,
            decoder=decoder,
            joiner=joiner,
            keywords_file=self._write_keywords_file(keyword, model_dir),
            num_threads=num_threads,
            provider="cpu",
        )
        self._stream = self._spotter.create_stream()
        print(f"[WAKEWORD] sherpa-onnx ready — keyword='{keyword}'")

    def _write_keywords_file(self, keyword: str, model_dir: str) -> str:
        import os
        path = os.path.join(model_dir, "_active_keyword.txt")
        with open(path, "w") as f:
            f.write(keyword + "\n")
        return path

    def process_chunk(self, chunk: np.ndarray) -> bool:
        """Feed a raw int16 chunk. Returns True when keyword detected."""
        audio = chunk.astype(np.float32) / 32768.0
        self._stream.accept_waveform(SAMPLE_RATE, audio)
        self._spotter.decode_stream(self._stream)
        result = self._spotter.get_result(self._stream)
        if result:
            print(f"[WAKEWORD] detected: '{result}'")
            # Reset stream for next detection
            self._stream = self._spotter.create_stream()
            return True
        return False

    def reset(self):
        self._stream = self._spotter.create_stream()
