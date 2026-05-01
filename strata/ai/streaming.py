"""Token streaming for filename + bucket suggestions.

``document_intelligence`` reuses Azure OpenAI for text generation on Markdown
excerpts (Document Intelligence layout APIs expect raw documents; the finance
worker uses MinerU + ``finance_extract`` for full runs). A dedicated DI-based
layout path can replace this branch later without touching route handlers.
"""
from __future__ import annotations

import json
import os
from urllib.parse import quote

import httpx


OLLAMA_URL = (os.environ.get("OLLAMA_URL") or "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL") or "gemma4:e4b"
AZURE_OPENAI_ENDPOINT = (os.environ.get("AZURE_OPENAI_ENDPOINT") or "").rstrip("/")
AZURE_OPENAI_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY") or ""
AZURE_OPENAI_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT") or ""
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION") or "2024-10-21"


async def stream_azure_openai(prompt: str, *, max_tokens: int = 60, temp: float = 0.3):
    """Async generator yielding token chunks from Azure OpenAI as they arrive."""
    if not (AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY and AZURE_OPENAI_DEPLOYMENT):
        raise RuntimeError("Azure OpenAI is not configured")
    url = (
        f"{AZURE_OPENAI_ENDPOINT}/openai/deployments/"
        f"{quote(AZURE_OPENAI_DEPLOYMENT, safe='')}/chat/completions"
    )
    payload = {
        "stream": True,
        "temperature": temp,
        "max_tokens": max_tokens,
        "top_p": 0.9,
        "messages": [
            {"role": "system", "content": "Follow the user's output format rules exactly."},
            {"role": "user", "content": prompt},
        ],
    }
    async with httpx.AsyncClient(timeout=120) as c:
        async with c.stream(
            "POST",
            url,
            params={"api-version": AZURE_OPENAI_API_VERSION},
            headers={"api-key": AZURE_OPENAI_API_KEY, "Content-Type": "application/json"},
            json=payload,
        ) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    return
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                tok = ((obj.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
                if tok:
                    yield tok


async def stream_ollama(prompt: str, *, max_tokens: int = 60, temp: float = 0.3):
    """Async generator yielding token chunks from a local or self-hosted Ollama endpoint."""
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": True,
        "options": {"temperature": temp, "num_predict": max_tokens, "top_p": 0.9},
    }
    async with httpx.AsyncClient(timeout=120) as c:
        async with c.stream("POST", f"{OLLAMA_URL}/api/generate", json=payload) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                tok = obj.get("response") or ""
                if tok:
                    yield tok
                if obj.get("done"):
                    return


async def stream_document_intelligence(prompt: str, *, max_tokens: int = 60, temp: float = 0.3):
    """Placeholder: DI layout models need raw bytes; naming uses OpenAI on excerpts."""
    async for tok in stream_azure_openai(prompt, max_tokens=max_tokens, temp=temp):
        yield tok


async def stream_model(prompt: str, *, max_tokens: int = 60, temp: float = 0.3, provider: str):
    """Dispatch by normalized provider id (``azure_openai`` | ``ollama`` | ``document_intelligence``)."""
    if provider == "ollama":
        async for tok in stream_ollama(prompt, max_tokens=max_tokens, temp=temp):
            yield tok
        return
    if provider == "document_intelligence":
        async for tok in stream_document_intelligence(prompt, max_tokens=max_tokens, temp=temp):
            yield tok
        return
    if provider in ("azure", "azure_openai", "azure_openai_chat"):
        async for tok in stream_azure_openai(prompt, max_tokens=max_tokens, temp=temp):
            yield tok
        return
    raise RuntimeError(f"Unsupported provider: {provider}")
