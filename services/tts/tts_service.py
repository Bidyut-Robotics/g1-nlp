import os
from typing import AsyncGenerator
from piper.voice import PiperVoice
from core.interfaces import ITTSProvider

class PiperTTS(ITTSProvider):
    """
    On-device TTS using Piper.
    Extremely fast C++ engine with Python bindings.
    """
    def __init__(self, model_path: str = "models/en_US-lessac-medium.onnx"):
        # Piper expects the .json config file to be at model_path + ".json"
        self.model_path = model_path
        config_path = model_path + ".json"
        
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"Piper model not found at {model_path}. Please download it first.")
        if not os.path.exists(config_path):
            raise FileNotFoundError(
                f"Piper config not found at {config_path}. Please place the matching .json file next to the model."
            )
            
        self.voice = PiperVoice.load(model_path, config_path)

    async def speak(self, text: str) -> AsyncGenerator[bytes, None]:
        """
        Synthesizes speech and yields raw PCM bytes in chunks.
        Default sample rate is usually 22050Hz or 16000Hz depending on the model.
        """
        for audio_chunk in self.voice.synthesize(text):
            yield audio_chunk.audio_int16_bytes
            
    def get_sample_rate(self) -> int:
        return self.voice.config.sample_rate
