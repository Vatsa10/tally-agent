"""The agent loop: plan, call tools, validate, queue or execute, respond.

Bounded by construction. A step limit, a per-step trace, and no path from a
model tool call to a Tally write that skips the tools layer's validate-then-queue
gate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from tallyagent_agent.context import ContextBuilder, tool_result_message
from tallyagent_llm.provider import Completion, Image, Message
from tallyagent_llm.router import Router
from tallyagent_tools import registry
from tallyagent_tools.base import ToolContext, ToolResult

log = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 12


@dataclass(slots=True)
class Step:
    """One model turn plus whatever tools it invoked."""

    index: int
    tool: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    output_hash: str = ""
    output_summary: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    egress_bytes: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "tool": self.tool,
            "arguments": self.arguments,
            "output_hash": self.output_hash,
            "output_summary": self.output_summary,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "latency_ms": self.latency_ms,
            "egress_bytes": self.egress_bytes,
            "error": self.error,
        }


@dataclass(slots=True)
class AgentResult:
    text: str
    steps: list[Step] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    tickets: list[str] = field(default_factory=list)
    hit_step_limit: bool = False

    @property
    def tools_used(self) -> list[str]:
        return [s.tool for s in self.steps if s.tool]

    @property
    def total_tokens(self) -> int:
        return sum(s.prompt_tokens + s.completion_tokens for s in self.steps)

    @property
    def total_egress_bytes(self) -> int:
        return sum(s.egress_bytes for s in self.steps)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, default=str, sort_keys=True).encode()).hexdigest()[:16]


class Agent:
    def __init__(
        self,
        router: Router,
        tools: ToolContext,
        context: ContextBuilder,
        max_steps: int = DEFAULT_MAX_STEPS,
        read_only: bool = False,
    ) -> None:
        self.router = router
        self.tools = tools
        self.context = context
        self.max_steps = max_steps
        # read_only hides every write tool - used by the MCP read profile and
        # by any channel that must not be able to propose a mutation.
        self.read_only = read_only
        self.history: list[Message] = []

    def tool_schemas(self) -> list[dict[str, Any]]:
        tools = [t for t in registry.TOOLS if not (self.read_only and t.mutating)]
        return [t.openai_schema() for t in tools]

    async def run(
        self, user_text: str, images: list[Image] | None = None
    ) -> AgentResult:
        messages = self.context.build(user_text, self.history, images)
        schemas = self.tool_schemas()
        steps: list[Step] = []
        tickets: list[str] = []

        for index in range(1, self.max_steps + 1):
            started = time.perf_counter()
            completion: Completion = await self.router.complete(messages, schemas)
            latency = int((time.perf_counter() - started) * 1000)

            if not completion.wants_tools:
                steps.append(
                    Step(
                        index=index,
                        output_summary=completion.text[:200],
                        prompt_tokens=completion.usage.prompt_tokens,
                        completion_tokens=completion.usage.completion_tokens,
                        latency_ms=latency,
                        egress_bytes=completion.raw_bytes_sent,
                    )
                )
                messages.append(Message(role="assistant", content=completion.text))
                self.history = messages[1:]
                return AgentResult(completion.text, steps, messages, tickets)

            messages.append(
                Message(
                    role="assistant",
                    content=completion.text,
                    tool_calls=completion.tool_calls,
                )
            )

            for call in completion.tool_calls:
                step = Step(
                    index=index,
                    tool=call.name,
                    arguments=call.arguments,
                    prompt_tokens=completion.usage.prompt_tokens,
                    completion_tokens=completion.usage.completion_tokens,
                    latency_ms=latency,
                    egress_bytes=completion.raw_bytes_sent,
                )
                result = await self._call_tool(call.name, call.arguments, step)
                steps.append(step)
                if isinstance(result, ToolResult) and isinstance(result.data, dict):
                    ticket = result.data.get("ticket")
                    if ticket:
                        tickets.append(str(ticket))
                messages.append(
                    tool_result_message(call.id, call.name, result)
                )

        # Out of steps. Say so rather than pretending the answer is complete.
        text = (
            f"I stopped after {self.max_steps} steps without finishing. "
            f"Tools tried: {', '.join(s.tool for s in steps if s.tool) or 'none'}. "
            "Ask me something narrower, or tell me which of those to pursue."
        )
        self.history = messages[1:]
        return AgentResult(text, steps, messages, tickets, hit_step_limit=True)

    async def _call_tool(
        self, name: str, arguments: dict[str, Any], step: Step
    ) -> ToolResult:
        """Invoke a tool, turning every failure into a message the model can act on.

        A raised exception here would end the conversation; what the model
        actually needs is to be told what went wrong so it can correct itself.
        """
        if self.read_only:
            try:
                if registry.get(name).mutating:
                    step.error = "blocked: read-only"
                    return ToolResult(
                        message=f"{name} is a write tool and this session is read-only."
                    )
            except KeyError:
                pass
        try:
            result = await registry.call(self.tools, name, arguments)
        except KeyError as exc:
            step.error = str(exc)
            return ToolResult(message=f"No such tool: {exc}")
        except TypeError as exc:
            step.error = str(exc)
            return ToolResult(
                message=f"{name} was called with the wrong arguments: {exc}"
            )
        except Exception as exc:  # noqa: BLE001 - a tool failure must not kill the turn
            log.exception("tool %s failed", name)
            step.error = f"{type(exc).__name__}: {exc}"
            return ToolResult(message=f"{name} failed: {type(exc).__name__}: {exc}")

        step.output_hash = _hash(result.data if result.data is not None else result.message)
        step.output_summary = result.message[:200]
        return result
