"""Anthropic provider. A genuinely different wire format, so not a subclass.

The system prompt is a top-level field rather than a message, tool results are
user-turn content blocks, and tool schemas use ``input_schema``. The registry
already produces ``input_schema`` shapes, so tools pass through unchanged.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from tallyagent_core.errors import NotConfiguredError, TallyAgentError
from tallyagent_llm.provider import Completion, Message, ToolCall, Usage

DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
DEFAULT_MODEL = "claude-sonnet-5"
API_VERSION = "2023-06-01"


def to_anthropic(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Return (system prompt, messages). System turns are hoisted out."""
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    for message in messages:
        if message.role == "system":
            system_parts.append(message.content)
            continue

        if message.role == "tool":
            # A tool result is a user turn carrying a tool_result block.
            block = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id,
                "content": message.content,
            }
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue

        content: list[dict[str, Any]] = []
        if message.content:
            content.append({"type": "text", "text": message.content})
        for image in message.images:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image.media_type,
                        "data": base64.b64encode(image.data).decode("ascii"),
                    },
                }
            )
        for call in message.tool_calls:
            content.append(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                }
            )
        out.append({"role": message.role, "content": content or [{"type": "text", "text": ""}]})

    return "\n\n".join(system_parts), out


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise NotConfiguredError("ANTHROPIC_API_KEY is not set.")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> Completion:
        system, converted = to_anthropic(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": converted,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools

        payload = json.dumps(body)
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self._transport
        ) as http:
            response = await http.post(
                f"{self.base_url}/messages",
                content=payload,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": API_VERSION,
                    "Content-Type": "application/json",
                },
            )
        if response.status_code != 200:
            raise TallyAgentError(
                f"Anthropic returned HTTP {response.status_code}: {response.text[:300]}"
            )

        data = response.json()
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(
                    ToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        arguments=block.get("input") or {},
                    )
                )
        usage = data.get("usage") or {}
        return Completion(
            text="".join(text_parts),
            tool_calls=calls,
            usage=Usage(
                prompt_tokens=int(usage.get("input_tokens", 0)),
                completion_tokens=int(usage.get("output_tokens", 0)),
                cached_tokens=int(usage.get("cache_read_input_tokens", 0)),
            ),
            model=data.get("model", self.model),
            provider=self.name,
            raw_bytes_sent=len(payload.encode("utf-8")),
        )
