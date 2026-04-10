import os
import numpy as np
import onnxruntime
import requests
from typing import Optional

class VADDetector:
    """
    Voice Activity Detection using Silero VAD (ONNX).
    Optimized for low-latency on-device execution.
    """
    def __init__(self, model_path: str = "models/silero_vad.onnx", threshold: float = 0.5, sampling_rate: int = 16000):
        self.model_path = model_path
        self.threshold = threshold
        self.sampling_rate = sampling_rate
        
        if not os.path.exists(self.model_path):
            self._download_model()
            
        # Initialize ONNX Runtime session
        self.session = onnxruntime.InferenceSession(self.model_path)
        self.reset_states()

    def _download_model(self):
        os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
        # Silero VAD v4 ONNX model
        url = "https://github.com/snakers4/silero-vad/raw/master/files/silero_vad.onnx"
        print(f"Downloading Silero VAD model to {self.model_path}...")
        response = requests.get(url)
        with open(self.model_path, "wb") as f:
            f.write(response.content)

    def reset_states(self):
        """Reset the internal GRU states of the VAD model."""
        self._h = np.zeros((2, 1, 64)).astype('float32')
        self._c = np.zeros((2, 1, 64)).astype('float32')

    def is_speech(self, audio_chunk: np.ndarray) -> bool:
        """
        Detects if speech is present in the given audio chunk.
        Expects 16kHz, 16-bit mono audio.
        Input audio_chunk should ideally be 512 samples long.
        """
        if len(audio_chunk.shape) == 1:
            audio_chunk = np.expand_dims(audio_chunk, 0)
        
        # Prepare inputs for the ONNX model
        ort_inputs = {
            'input': audio_chunk.astype('float32'),
            'h': self._h,
            'c': self._c,
            'sr': np.array([self.sampling_rate], dtype='int64')
        }
        
        # Perform inference
        out, h, c = self.session.run(None, ort_inputs)
        self._h, self._c = h, c
        
        return out[0][0] > self.threshold

class AudioBuffer:
    """A simple circular buffer for handling incoming audio streams."""
    def __init__(self, max_size: int = 16000 * 30): # 30 seconds of 16kHz audio
        self.max_size = max_size
        self.buffer = np.zeros(max_size, dtype='float32')
        self.size = 0

    def add(self, data: np.ndarray):
        num_samples = len(data)
        if self.size + num_samples > self.max_size:
            # Shift buffer left
            overflow = (self.size + num_samples) - self.max_size
            self.buffer[:-overflow] = self.buffer[overflow:]
            self.size = self.max_size - num_samples
            
        self.buffer[self.size:self.size + num_samples] = data
        self.size += num_samples

    def get_latest(self, num_samples: int) -> np.ndarray:
        start = max(0, self.size - num_samples)
        return self.buffer[start:self.size]

    def clear(self):
        self.size = 0
