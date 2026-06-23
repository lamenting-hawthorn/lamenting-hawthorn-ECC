"""LLM client for Mo Memory.

Supports DeepSeek API (default) with fallback to OpenAI-compatible endpoints.

Usage:
    from src.llm_client import LLMClient
    client = LLMClient()
    response = client.chat("What is the capital of France?")
"""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TEMPERATURE = 0.7


class LLMClient:
    """Simple HTTP client for DeepSeek / OpenAI-compatible chat APIs."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> None:
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        self.base_url = base_url or os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL)
        self.model = model or os.environ.get("LLM_MODEL", DEFAULT_MODEL)
        self.max_tokens = max_tokens
        self.temperature = temperature

    def chat(
        self,
        user_message: str,
        system_prompt: str | None = None,
        retrieved_context: str = "",
    ) -> str:
        """Send a chat completion request and return the assistant's text."""
        if not self.api_key:
            raise RuntimeError(
                "LLM_API_KEY not set. Configure DeepSeek API key to use real LLM."
            )

        messages: list[dict[str, str]] = []

        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        if retrieved_context:
            messages.append({
                "role": "system",
                "content": f"Retrieved context:\n{retrieved_context}",
            })

        messages.append({"role": "user", "content": user_message})

        url = f"{self.base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

        try:
            resp = httpx.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"LLM API error: {exc.response.status_code} {exc.response.text}") from exc
        except Exception as exc:
            raise RuntimeError(f"LLM call failed: {exc}") from exc

    def chat_with_memory(
        self,
        user_message: str,
        retrieved_context: str = "",
    ) -> str:
        """Call the LLM with the Mo Memory system prompt + retrieved context."""
        system = (
            "You are Mo Memory, a permission-safe assistant with durable memory.\n"
            "Use retrieved context if relevant.\n"
            "Never claim memory unless it appears in retrieved context.\n"
            "Be concise and accurate."
        )
        return self.chat(user_message, system_prompt=system, retrieved_context=retrieved_context)
