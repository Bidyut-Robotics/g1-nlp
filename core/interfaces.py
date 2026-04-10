from abc import ABC, abstractmethod
from typing import AsyncGenerator, Optional
from .schemas import Utterance, NLPActionPayload

class IASRProvider(ABC):
    """Interface for Automatic Speech Recognition providers."""
    
    @abstractmethod
    async def transcribe(self, audio_data: bytes) -> Utterance:
        """Transcribe audio data to text."""
        pass

class ILLMProvider(ABC):
    """Interface for Large Language Model providers."""
    
    @abstractmethod
    async def generate_response(self, prompt: str, context: Optional[dict] = None) -> AsyncGenerator[str, None]:
        """Generate a streaming response from the LLM."""
        pass

    @abstractmethod
    async def extract_actions(self, text: str) -> list[NLPActionPayload]:
        """Extract structured actions from the dialogue."""
        pass

class ITTSProvider(ABC):
    """Interface for Text-to-Speech providers."""
    
    @abstractmethod
    async def speak(self, text: str) -> AsyncGenerator[bytes, None]:
        """Stream audio bytes for the given text."""
        pass
