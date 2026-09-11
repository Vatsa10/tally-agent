"""Provider-agnostic model interface.

Nothing above this layer knows which vendor is answering. Swapping
``[model] provider`` in config.toml between deepseek, anthropic, openai and mock
must be the only change required.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class Image:
    """An image to send with a message. ``data`` is raw bytes, not base64 -
    encoding is each provider's business."""

    data: bytes
    media_type: str = "image/jpeg"


@dataclass(slots=True)
class Message:
    role: str  # system | user | assistant | tool
    content: str = ""
    images: list[Image] = field(default_factory=list)
    #: assistant messages may carry tool calls; tool messages carry a call id
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    @classmethod
    def from_json(cls, id_: str, name: str, raw: str) -> ToolCall:
        """Models emit arguments as a JSON string, and sometimes as an invalid
        one. A broken call becomes an empty-argument call so the loop can tell
        the model what went wrong instead of crashing."""
        try:
            arguments = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            arguments = {"__parse_error__": raw}
        return cls(id=id_, name=name, arguments=arguments)


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(slots=True)
class Completion:
    """One model turn: either text, tool calls, or both."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    provider: str = ""
    raw_bytes_sent: int = 0

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class Provider(Protocol):
    name: str
    model: str

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> Completion: ...
