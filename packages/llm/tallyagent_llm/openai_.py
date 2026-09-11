"""OpenAI provider. Same wire format as DeepSeek, different host and pricing."""

from __future__ import annotations

from typing import Any

import httpx

from tallyagent_core.errors import NotConfiguredError
from tallyagent_llm.deepseek import DeepSeekProvider
from tallyagent_llm.provider import Completion, Message

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


class OpenAIProvider(DeepSeekProvider):
    """Reuses the DeepSeek transport because the API is byte-compatible.

    Inheriting rather than copying means a fix to tool-call parsing lands in
    both; the only differences are the default host, model and the name used
    for cost accounting.
    """

    name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise NotConfiguredError("OPENAI_API_KEY is not set.")
        super().__init__(api_key, model, base_url, timeout, transport)

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> Completion:
        completion = await super().complete(messages, tools, max_tokens, temperature)
        completion.provider = self.name
        return completion
