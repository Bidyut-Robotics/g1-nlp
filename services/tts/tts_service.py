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


class G1BuiltinTTS(ITTSProvider):
    """
    Standard G1 Robot TTS using the onboard TtsMaker service.
    Bypasses PulseAudio/PCM streaming for maximum stability.
    """
    def __init__(self, interface: str = "eth0", speaker_id: int = 1):
        self.interface = interface
        self.speaker_id = speaker_id
        self.is_builtin = True
        self._client = None
        
        # Initialize DDS immediately on startup
        try:
            # CIRCULAR IMPORT FIX: Import root before submodules
            import unitree_sdk2py
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize
            from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
            
            print(f"[TTS:G1] Initializing built-in TTS on {self.interface}...")
            ChannelFactoryInitialize(0, self.interface)
            self._client = AudioClient()
            self._client.Init()
            # Note: TtsMaker doesn't always need a long sleep after Init(), 
            # but we assume the factory handles the handshake.
            self._client.SetVolume(100)
            print("[TTS:G1] DDS initialised successfully.")
        except Exception as e:
            print(f"[TTS:G1 ERROR] Initialization failed: {e}")

    async def speak(self, text: str) -> AsyncGenerator[bytes, None]:
        """
        Triggers the robot's built-in TTS via TtsMaker.
        """
        if self._client:
            print(f"[TTS:G1] Speaking (TtsMaker): {text}")
            ret = self._client.TtsMaker(text, self.speaker_id)
            if ret != 0:
                 print(f"[TTS:G1 ERROR] TtsMaker returned code {ret}. Interface: {self.interface}")
        else:
            print(f"[TTS:G1 ERROR] AudioClient not available for: {text}")
        
        # Generator must yield at least once to be valid in main loops
        if False: yield b""

    def get_sample_rate(self) -> int:
        return 16000 # Default for G1 built-in

class G1DirectTTS(ITTSProvider):
    """
    High-performance TTS for G1.
    Uses Piper for voice generation (Jarvis), then sends PCM 
    directly to Robot DDS PlayStream with 5.0x boost.
    Bypasses PulseAudio entirely.
    """
    def __init__(self, model_path: str = "models/en_US-lessac-medium.onnx", interface: str = "eth0"):
        self.interface = interface
        self.piper = PiperTTS(model_path)
        self._client = None
        self.stream_id = f"jarvis_{int(os.getpid())}"
        
        try:
            import unitree_sdk2py
            from unitree_sdk2py.core.channel import ChannelFactoryInitialize
            from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
            
            print(f"[TTS:G1-DIRECT] Initializing DDS PlayStream on {self.interface}...")
            ChannelFactoryInitialize(0, self.interface)
            self._client = AudioClient()
            self._client.Init()
            self._client.SetVolume(100)
            print("[TTS:G1-DIRECT] DDS initialised successfully.")
        except Exception as e:
            print(f"[TTS:G1-DIRECT ERROR] Initialization failed: {e}")

    async def speak(self, text: str) -> AsyncGenerator[bytes, None]:
        import numpy as np
        
        if not self._client:
            print(f"[TTS:G1-DIRECT ERROR] AudioClient not available for: {text}")
            async for chunk in self.piper.speak(text):
                yield chunk
            return

        print(f"[TTS:G1-DIRECT] Streaming to robot: '{text}'")
        
        target_fs = 16000
        source_fs = self.piper.get_sample_rate()
        boost_factor = 5.0

        async for pcm_chunk in self.piper.speak(text):
            # Convert bytes to numpy int16
            samples = np.frombuffer(pcm_chunk, dtype=np.int16)
            
            # Resample from Piper rate to 16000Hz
            if source_fs != target_fs:
                num_samples_out = int(len(samples) * target_fs / source_fs)
                samples = np.interp(
                    np.linspace(0, len(samples), num_samples_out, endpoint=False),
                    np.arange(len(samples)),
                    samples
                ).astype(np.int16)
            
            # Apply volume boost
            boosted = np.clip(samples.astype(np.float32) * boost_factor, -32768, 32767).astype(np.int16)
            
            # Send to hardware
            try:
                # App name "jarvis_brain", stream_id, bytes
                self._client.PlayStream("jarvis_brain", self.stream_id, boosted.tobytes())
            except Exception as e:
                print(f"[TTS:G1-DIRECT ERROR] PlayStream failed: {e}")
            
            # Still yield for downstream logging/sync
            yield pcm_chunk
            
    def get_sample_rate(self) -> int:
        return 16000
