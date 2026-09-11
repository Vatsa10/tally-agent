"""DeepSeek provider - the default. OpenAI-compatible /chat/completions.

Message ordering is deliberately prefix-stable: system prompt, then tool
schemas, then the conversation in order, with nothing mutable in front. DeepSeek
bills cached prefix tokens at a fraction of the price, and a prompt that
reshuffles its own preamble each turn never gets a cache hit.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from tallyagent_core.errors import NotConfiguredError, TallyAgentError
from tallyagent_llm.provider import Completion, Message, ToolCall, Usage

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"


def to_openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Translate to the OpenAI wire shape, shared with the OpenAI provider."""
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": message.content,
                }
            )
            continue

        if message.images:
            content: Any = [{"type": "text", "text": message.content}]
            for image in message.images:
                encoded = base64.b64encode(image.data).decode("ascii")
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{image.media_type};base64,{encoded}"
                        },
                    }
                )
        else:
            content = message.content

        payload: dict[str, Any] = {"role": message.role, "content": content}
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                }
                for call in message.tool_calls
            ]
        out.append(payload)
    return out


def parse_openai_response(data: dict[str, Any], provider: str) -> Completion:
    choices = data.get("choices") or []
    if not choices:
        raise TallyAgentError(f"{provider} returned no choices: {data}")
    message = choices[0].get("message") or {}
    calls = [
        ToolCall.from_json(
            call.get("id", ""),
            call.get("function", {}).get("name", ""),
            call.get("function", {}).get("arguments", ""),
        )
        for call in (message.get("tool_calls") or [])
    ]
    usage_raw = data.get("usage") or {}
    details = usage_raw.get("prompt_tokens_details") or {}
    return Completion(
        text=message.get("content") or "",
        tool_calls=calls,
        usage=Usage(
            prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
            completion_tokens=int(usage_raw.get("completion_tokens", 0)),
            cached_tokens=int(
                usage_raw.get("prompt_cache_hit_tokens")
                or details.get("cached_tokens")
                or 0
            ),
        ),
        model=data.get("model", ""),
        provider=provider,
    )


class DeepSeekProvider:
    name = "deepseek"

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise NotConfiguredError(
                "DEEPSEEK_API_KEY is not set. Export it, put it in the OS keyring, "
                "or set [model] provider = \"mock\" to run without a model."
            )
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
        body: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        payload = json.dumps(body)
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self._transport
        ) as http:
            response = await http.post(
                f"{self.base_url}/chat/completions",
                content=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        if response.status_code != 200:
            raise TallyAgentError(
                f"DeepSeek returned HTTP {response.status_code}: {response.text[:300]}"
            )
        completion = parse_openai_response(response.json(), self.name)
        completion.raw_bytes_sent = len(payload.encode("utf-8"))
        return completion
