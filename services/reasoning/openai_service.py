import httpx
import json
import os
import re
from typing import AsyncGenerator, Optional, List
from core.interfaces import ILLMProvider
from core.schemas import NLPActionPayload, ActionType


class EnterpriseChatGPT(ILLMProvider):
    """
    Enterprise ChatGPT provider — connects to the client's custom OpenAI-compatible endpoint.

    Configuration (via env vars or app_config.json enterprise section):
        ENTERPRISE_API_KEY   — required, the client's API key
        ENTERPRISE_API_BASE  — base URL (default: https://api.openai.com/v1)
        ENTERPRISE_MODEL     — model name (default: gpt-4o)

    Notes:
        - This stub is ready to plug in the client endpoint once details are shared.
        - Falls back gracefully with an error message if the endpoint is unreachable.
        - Supports the same [ACTION: ...] tag format as the other providers.
        - RAG/knowledge injection is supported via inject_knowledge().
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_name: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 200,
    ):
        self.api_key = api_key or os.getenv("ENTERPRISE_API_KEY")
        self.base_url = (
            base_url
            or os.getenv("ENTERPRISE_API_BASE", "https://api.openai.com/v1")
        ).rstrip("/")
        self.model_name = model_name or os.getenv("ENTERPRISE_MODEL", "gpt-4o")
        self.temperature = temperature
        self.max_tokens = max_tokens

        self.system_prompt = (
            "You are Jarvis, the Nueroid Office Humanoid assistant. "
            "You have access to company knowledge. Answer accurately and concisely. "
            "Always reply in the SAME LANGUAGE the user spoke (English or Hindi). "
            "Keep responses to 1–3 sentences unless detail is explicitly requested. "
            "Never disclose internal server IPs, employee passwords, or confidential HR data."
        )
        self.knowledge_context = ""

        if not self.api_key:
            print("[ENTERPRISE LLM] WARNING: ENTERPRISE_API_KEY is not set. Responses will fail.")
        else:
            print(f"[ENTERPRISE LLM] Ready — model={self.model_name}, base={self.base_url}")

    def inject_knowledge(self, knowledge_snippet: str):
        """Called by RAG systems to inject proprietary documents into the session context."""
        self.knowledge_context = f"\n\n[Company Knowledge]\n{knowledge_snippet}"

    def _build_system(self) -> str:
        return self.system_prompt + self.knowledge_context

    def _blocked(self, prompt: str) -> bool:
        """Simple guardrail — blocks requests for secrets/credentials."""
        blocked_keywords = ["password", "root access", "hidden file", "private key", "secret"]
        return any(word in prompt.lower() for word in blocked_keywords)

    async def generate_response(self, prompt: str, context: Optional[dict] = None) -> AsyncGenerator[str, None]:
        """Streams a response from the enterprise ChatGPT endpoint."""

        if self._blocked(prompt):
            yield "I'm sorry, I cannot process that request due to security policy."
            return

        if not self.api_key:
            yield "Enterprise ChatGPT is not configured yet. Please set ENTERPRISE_API_KEY."
            return

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        messages = [{"role": "system", "content": self._build_system()}]

        # Inject conversation history if provided
        if context and "history" in context:
            messages.extend(context["history"][-4:])  # Last 4 messages for context

        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model_name,
            "messages": messages,
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
                        yield f"[Enterprise LLM Error {response.status_code}] {body.decode()[:200]}"
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

        except httpx.ConnectError:
            yield "I could not reach the enterprise knowledge server. Please check your network."
        except Exception as e:
            yield f"[Enterprise LLM Error] {e}"

    async def extract_actions(self, text: str) -> List[NLPActionPayload]:
        """Extract [ACTION: TYPE {...}] tags from the LLM response."""
        actions = []
        pattern = re.compile(r"\[ACTION:\s*(\w+)\s*(\{.*?\})\]")
        for match in pattern.finditer(text):
            a_type_str = match.group(1).lower()
            params_str = match.group(2)
            try:
                params = json.loads(params_str.replace("'", '"'))
                a_type = next((t for t in ActionType if t.value == a_type_str), ActionType.SPEAK)
                actions.append(NLPActionPayload(action_type=a_type, params=params, confidence=1.0))
            except (json.JSONDecodeError, StopIteration):
                continue
        return actions
