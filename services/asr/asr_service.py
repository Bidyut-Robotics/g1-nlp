import time
import asyncio
import numpy as np
from faster_whisper import WhisperModel
from core.interfaces import IASRProvider
from core.schemas import Utterance


class FasterWhisperASR(IASRProvider):
    """
    On-device ASR using Faster-Whisper.
    - Dev (CPU laptop): 'tiny' model, int8 → ~300ms per utterance
    - Jetson Orin: 'base' model, device='cuda', compute_type='int8_float16'
    Supports English + Hindi auto-detection (and Telugu, Tamil, Kannada).
    """

    def __init__(
        self,
        model_size: str = "tiny",
        device: str = "cpu",
        compute_type: str = "int8",
    ):
        print(f"[ASR] Loading Faster-Whisper '{model_size}' on {device}/{compute_type}...")
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self.language_map = {
            "en": "English",
            "hi": "Hindi",
            "te": "Telugu",
            "ta": "Tamil",
            "kn": "Kannada",
        }
        # Pre-warm: run a silent inference so the first real call has no cold-start delay
        self._prewarm()

    def _prewarm(self):
        """Run a silent dummy transcription to warm up the ONNX/CTranslate2 engine."""
        try:
            silent = np.zeros(8000, dtype=np.float32)  # 0.5s of silence at 16kHz
            list(self.model.transcribe(silent, beam_size=1, best_of=1)[0])
            print("[ASR] Pre-warm complete.")
        except Exception as e:
            print(f"[ASR] Pre-warm skipped: {e}")

    def transcribe_sync(self, audio_data: np.ndarray) -> Utterance:
        """
        Synchronous transcription — called via asyncio.to_thread() from the pipeline.
        audio_data: float32 numpy array at 16kHz, range [-1, 1].
        Supports English + Hindi auto-detection (language=None lets Whisper detect).
        """
        segments, info = self.model.transcribe(
            audio_data,
            beam_size=1,
            best_of=1,
            language=None,             # Auto-detects English vs Hindi (and others)
            condition_on_previous_text=False,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=250),
        )

        full_text = " ".join(seg.text.strip() for seg in segments)

        return Utterance(
            text=full_text.strip(),
            language=self.language_map.get(info.language, info.language),
            confidence=info.language_probability,
            timestamp=time.time(),
            id=f"asr_{int(time.time() * 1000)}",
        )

    async def transcribe(self, audio_data: np.ndarray) -> Utterance:
        """
        Async wrapper — runs transcribe_sync in a thread pool.
        Use this when calling from async code directly.
        """
        return await asyncio.to_thread(self.transcribe_sync, audio_data)
