import httpx
import json
import re
import os
import asyncio
from typing import AsyncGenerator, Optional, List
from core.interfaces import ILLMProvider
from core.schemas import NLPActionPayload, ActionType

# ─── Shared system prompt (both providers) ───────────────────────────────────
_SYSTEM_PROMPT = (
    "You are Jarvis, a friendly and efficient humanoid robot assistant deployed in a corporate office. "
    "Always reply in the SAME LANGUAGE the user spoke in. "
    "If the user speaks Hindi, reply in Hindi (Devanagari script or Roman transliteration, whichever is more natural). "
    "If the user speaks English, reply in English. "
    "Keep answers concise: 1–3 short sentences unless the user clearly asks for more detail. "
    "Never mention internal system tags, tool names, or JSON structures in your spoken response."
)

# ─── Action extraction (shared between both providers) ────────────────────────
_ACTION_PATTERN = re.compile(r"\[ACTION:\s*(\w+)\s*(\{.*?\})\]")


def _extract_actions_from_text(text: str) -> List[NLPActionPayload]:
    actions = []
    for match in _ACTION_PATTERN.finditer(text):
        a_type_str = match.group(1).lower()
        params_str = match.group(2)
        try:
            params = json.loads(params_str.replace("'", '"'))
            a_type = next((t for t in ActionType if t.value == a_type_str), ActionType.SPEAK)
            actions.append(NLPActionPayload(action_type=a_type, params=params, confidence=1.0))
        except (json.JSONDecodeError, StopIteration):
            continue
    return actions


# ─── Groq ─────────────────────────────────────────────────────────────────────

class GroqLLM(ILLMProvider):
    """
    Cloud-based LLM using Groq API — ultra-fast inference (~10–100 tok/s, free tier).
    Default model: llama-3.1-8b-instant (fastest available on Groq as of 2025).
    Other options: llama3-8b-8192, llama-3.3-70b-versatile, gemma2-9b-it
    """

    def __init__(
        self,
        model_name: str = "llama-3.1-8b-instant",
        api_key: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 150,
    ):
        self.model_name = model_name
        self.api_key = api_key or os.getenv("GROQ_API_KEY")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.base_url = "https://api.groq.com/openai/v1"

        if not self.api_key:
            raise ValueError("GROQ_API_KEY not set. Set the environment variable or pass api_key.")
        print(f"[LLM] GroqLLM ready — model={self.model_name}")

    async def generate_response(self, prompt: str, context: Optional[dict] = None) -> AsyncGenerator[str, None]:
        """Streams a conversational response from Groq."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                ) as response:
                    if response.status_code != 200:
                        body = await response.aread()
                        yield f"[LLM Error {response.status_code}] {body.decode()[:200]}"
                        return
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:]
                            if data == "[DONE]":
                                break
                            try:
                                delta = json.loads(data)["choices"][0].get("delta", {})
                                if "content" in delta:
                                    yield delta["content"]
                            except (json.JSONDecodeError, KeyError, IndexError):
                                continue
        except Exception as e:
            yield f"[LLM Error] {e}"

    async def extract_actions(self, text: str) -> List[NLPActionPayload]:
        return _extract_actions_from_text(text)


# ─── Ollama (local / Jetson) ──────────────────────────────────────────────────

class OllamaLLM(ILLMProvider):
    """
    On-device LLM using Ollama.
    Dev (CPU): llama3.2:1b  — ~2–4 tok/s, faster than phi3 (3.8B)
    Jetson:    llama3.2:1b or phi3:mini with GPU offloading
    """

    def __init__(
        self,
        model_name: str = "llama3.2:1b",
        base_url: str = "http://localhost:11434",
        temperature: float = 0.1,
        num_predict: int = 50,
        num_ctx: int = 512,
        keep_alive: str = "30m",
    ):
        self.model_name = model_name
        self.base_url = base_url
        self.temperature = temperature
        self.num_predict = num_predict
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive
        print(f"[LLM] OllamaLLM ready — model={self.model_name} @ {self.base_url}")

    async def generate_response(self, prompt: str, context: Optional[dict] = None) -> AsyncGenerator[str, None]:
        """Streams a conversational response from Ollama."""
        url = f"{self.base_url}/api/generate"
        payload = {
            "model": self.model_name,
            "system": _SYSTEM_PROMPT,
            "prompt": prompt,
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.num_predict,
                "num_ctx": self.num_ctx,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream("POST", url, json=payload) as response:
                    if response.status_code != 200:
                        yield f"[LLM Error {response.status_code}] Ollama unavailable"
                        return
                    async for line in response.aiter_lines():
                        if line:
                            try:
                                chunk = json.loads(line)
                                yield chunk.get("response", "")
                                if chunk.get("done"):
                                    break
                            except json.JSONDecodeError:
                                continue
        except Exception as e:
            yield f"[LLM Error] Ollama: {e}"

    async def extract_actions(self, text: str) -> List[NLPActionPayload]:
        return _extract_actions_from_text(text)
