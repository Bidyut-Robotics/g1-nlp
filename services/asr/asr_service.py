import time
import asyncio
import numpy as np
from core.interfaces import IASRProvider
from core.schemas import Utterance

# Minimum ratio of ASCII/Latin chars for a transcription to be considered valid English.
# Whisper on unclear audio hallucinates non-Latin scripts — this catches that fast.
_MIN_ASCII_RATIO = 0.80


def _is_valid_english(text: str) -> bool:
    if not text:
        return False
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return (ascii_chars / len(text)) >= _MIN_ASCII_RATIO


class FasterWhisperASR(IASRProvider):
    """
    On-device ASR using openai-whisper (PyTorch backend).
    English-only: language is forced to "en" so Whisper skips language detection,
    which eliminates the 15-20s hallucination delay on unclear audio.
    """

    def __init__(
        self,
        model_size: str = "medium",
        device: str = "cpu",
        compute_type: str = "float32",
    ):
        self.device = device

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
                language="en",
                condition_on_previous_text=False,
                fp16=(self.device == "cuda"),
            )
            text = result["text"].strip()
            return text, "en", 1.0
        else:
            segments, info = self._model.transcribe(
                audio_data,
                beam_size=1,
                best_of=1,
                language="en",
                condition_on_previous_text=False,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=250),
            )
            text = " ".join(seg.text.strip() for seg in segments)
            return text, "en", info.language_probability

    def transcribe_sync(self, audio_data: np.ndarray) -> Utterance:
        text, lang, confidence = self._transcribe_raw(audio_data)

        if not _is_valid_english(text):
            print(f"[ASR] Rejected hallucination: '{text[:60]}'")
            text = ""

        return Utterance(
            text=text,
            language="English",
            confidence=confidence,
            timestamp=time.time(),
            id=f"asr_{int(time.time() * 1000)}",
        )

    async def transcribe(self, audio_data: np.ndarray) -> Utterance:
        return await asyncio.to_thread(self.transcribe_sync, audio_data)

class ParakeetASR(IASRProvider):
    """
    On-device ASR using NeMo Parakeet TDT.
    Recommended for AGX Thor / any Jetson with JetPack 6 + CUDA 12.
    """

    def __init__(self, device: str = "cuda", model_name: str = "nvidia/parakeet-tdt-0.6b-v2"):
        import logging
        import nemo.collections.asr as nemo_asr

        self._device = device
        logging.getLogger("nemo_logger").setLevel(logging.ERROR)

        print(f"[ASR] Loading {model_name} via NeMo...")
        self._model = nemo_asr.models.ASRModel.from_pretrained(model_name=model_name)
        self._model.eval()
        if device == "cuda":
            self._model = self._model.cuda()

        print("[ASR] Parakeet ready.")
        self._prewarm()

    def _prewarm(self):
        try:
            self._model.transcribe([np.zeros(8000, dtype=np.float32)], batch_size=1)
            print("[ASR] Pre-warm complete.")
        except Exception as e:
            print(f"[ASR] Pre-warm skipped: {e}")

    def transcribe_sync(self, audio_data: np.ndarray) -> Utterance:
        output = self._model.transcribe([audio_data], batch_size=1)
        text = (output[0].text if hasattr(output[0], "text") else str(output[0])).strip()
        if not _is_valid_english(text):
            print(f"[ASR] Rejected hallucination: '{text[:60]}'")
            text = ""
        return Utterance(
            text=text,
            language="English",
            confidence=1.0,
            timestamp=time.time(),
            id=f"asr_{int(time.time() * 1000)}",
        )

    async def transcribe(self, audio_data: np.ndarray) -> Utterance:
        return await asyncio.to_thread(self.transcribe_sync, audio_data)
