import time
import asyncio
import numpy as np
from core.interfaces import IASRProvider
from core.schemas import Utterance


class FasterWhisperASR(IASRProvider):
    """
    On-device ASR using openai-whisper (PyTorch backend).
    Uses torch.cuda directly — works on AGX Thor where CTranslate2 doesn't.
    Falls back to faster-whisper if openai-whisper is not installed.
    """

    def __init__(
        self,
        model_size: str = "medium",
        device: str = "cpu",
        compute_type: str = "float32",
    ):
        self.device = device
        self.language_map = {
            "en": "English",
            "hi": "Hindi",
            "te": "Telugu",
            "ta": "Tamil",
            "kn": "Kannada",
        }

        print(f"[ASR] Loading Whisper '{model_size}' on {device}...")
        try:
            import whisper
            self._model = whisper.load_model(model_size, device=device)
            self._backend = "whisper"
            print(f"[ASR] openai-whisper loaded (torch backend).")
        except ImportError:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
            self._backend = "faster-whisper"
            print(f"[ASR] faster-whisper loaded.")

        self._prewarm()

    def _prewarm(self):
        try:
            silent = np.zeros(8000, dtype=np.float32)
            self._transcribe_raw(silent)
            print("[ASR] Pre-warm complete.")
        except Exception as e:
            print(f"[ASR] Pre-warm skipped: {e}")

    def _transcribe_raw(self, audio_data: np.ndarray) -> tuple[str, str, float]:
        """Returns (text, language, confidence)."""
        if self._backend == "whisper":
            import whisper
            result = self._model.transcribe(
                audio_data,
                beam_size=1,
                language=None,
                condition_on_previous_text=False,
                fp16=(self.device == "cuda"),
            )
            text = result["text"].strip()
            lang = result.get("language", "en")
            return text, lang, 1.0
        else:
            segments, info = self._model.transcribe(
                audio_data,
                beam_size=1,
                best_of=1,
                language=None,
                condition_on_previous_text=False,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=250),
            )
            text = " ".join(seg.text.strip() for seg in segments)
            return text, info.language, info.language_probability

    def transcribe_sync(self, audio_data: np.ndarray) -> Utterance:
        text, lang, confidence = self._transcribe_raw(audio_data)
        return Utterance(
            text=text,
            language=self.language_map.get(lang, lang),
            confidence=confidence,
            timestamp=time.time(),
            id=f"asr_{int(time.time() * 1000)}",
        )

    async def transcribe(self, audio_data: np.ndarray) -> Utterance:
        return await asyncio.to_thread(self.transcribe_sync, audio_data)
