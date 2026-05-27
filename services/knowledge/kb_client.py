"""Knowledge base client — queries g1_nlp_dashboard for relevant document chunks."""

import asyncio
import re
from typing import List

import aiohttp

from core.config import load_app_config


def _load_cfg() -> dict:
    return load_app_config().get("knowledge_base", {})


# Queries that never need KB lookup — greetings, acks, simple commands
_SKIP_PATTERNS = re.compile(
    r"^(hi|hello|hey|good morning|good afternoon|good evening|"
    r"thank(s| you( very much)?)?|bye|goodbye|ok(ay)?|"
    r"yes|no|stop|cancel|got it|alright|"
    r"what time|what date|what day|who are you|what are you|"
    r"what can you do|where are you|move forward|move backward|"
    r"shake hand|handshake)[.!?]?$",
    re.IGNORECASE,
)


def _clean_query(text: str) -> str:
    """
    Keep only the first sentence of the ASR transcript for KB lookup.
    Whisper often appends hallucinated repetitions after the real utterance;
    the first sentence is almost always the genuine question.
    """
    # Split on sentence-ending punctuation, take the first non-empty chunk
    sentences = re.split(r"[.!?]", text)
    first = sentences[0].strip() if sentences else text
    return first if first else text


class KBClient:
    """Async HTTP client for the RAG knowledge base dashboard."""

    def __init__(self):
        cfg = _load_cfg()
        self.enabled: bool = cfg.get("enabled", False)
        self.base_url: str = cfg.get("url", "http://localhost:8000").rstrip("/")
        self.top_k: int = cfg.get("top_k", 3)
        self.timeout_s: float = cfg.get("timeout_ms", 500) / 1000.0

    async def retrieve(self, query: str, top_k: int = None) -> List[str]:
        """
        Return text chunks relevant to `query` from the knowledge base.
        Returns [] on timeout, connection error, or if disabled.
        Never raises — the robot must continue even if KB is down.
        """
        if not self.enabled:
            return []

        # Skip KB for trivial utterances that will never need document context
        clean = _clean_query(query)
        if _SKIP_PATTERNS.match(clean.strip()):
            return []

        url = f"{self.base_url}/api/v1/retrieve/"
        payload = {"query": clean, "top_k": top_k or self.top_k}

        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout_s)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        print(f"[KB] HTTP {resp.status} — skipping")
                        return []
                    data = await resp.json()
                    chunks = [r["text"] for r in data.get("results", []) if r.get("text")]
                    if chunks:
                        print(f"[KB] {len(chunks)} chunks retrieved for: {query[:50]}")
                    return chunks

        except asyncio.TimeoutError:
            print(f"[KB] Timeout ({self.timeout_s}s) — skipping knowledge base")
            return []
        except Exception as e:
            print(f"[KB] Unreachable ({e.__class__.__name__}) — skipping knowledge base")
            return []


# Global instance (created once at import time)
kb_client = KBClient()
