"""Deterministic mock provider.

Used by the whole test suite and by ``tallyagent chat`` when no API key is set,
so nothing in this build ever needs to send real bookkeeping data to a vendor.
It is not a stub: it answers with real tool calls, driven either by a script or
by a small set of rules over the user's text, so the agent loop under test is
the same loop that runs in production.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tallyagent_llm.provider import Completion, Message, ToolCall, Usage

Responder = Callable[[list[Message]], Completion]


def text(body: str) -> Completion:
    return Completion(
        text=body, usage=Usage(prompt_tokens=100, completion_tokens=20), provider="mock"
    )


def call(name: str, **arguments: Any) -> Completion:
    return Completion(
        tool_calls=[ToolCall(id=f"call_{name}", name=name, arguments=arguments)],
        usage=Usage(prompt_tokens=100, completion_tokens=20),
        provider="mock",
    )


_AMOUNT = re.compile(r"(?:rs\.?|₹|inr)?\s*([\d,]+(?:\.\d{1,2})?)", re.I)


def _amount(prompt: str) -> str:
    match = _AMOUNT.search(prompt)
    return match.group(1).replace(",", "") if match else "0"


def default_rules(messages: list[Message]) -> Completion:
    """A small deterministic policy over the last user message.

    Enough to exercise read paths, a write path and a plain answer without a
    network call. Anything it does not recognise gets an honest refusal rather
    than an invented figure.
    """
    last_user = next(
        (m.content for m in reversed(messages) if m.role == "user"), ""
    ).lower()
    # A tool has already answered: summarise rather than calling again.
    if messages and messages[-1].role == "tool":
        return text(messages[-1].content)

    if "outstanding" in last_user or "owes" in last_user or "receivable" in last_user:
        return call("outstanding_receivables")
    if "payable" in last_user or "we owe" in last_user:
        return call("outstanding_payables")
    if "cash" in last_user or "bank balance" in last_user:
        return call("cash_position")
    if "trial balance" in last_user:
        return call("trial_balance")
    if "top debtor" in last_user:
        return call("top_debtors")
    if "day book" in last_user or "transactions" in last_user:
        return call("day_book")
    if "ledger" in last_user and ("list" in last_user or "which" in last_user):
        return call("list_ledgers")
    return text(
        "I can look up balances, outstandings and the day book, or draft a "
        "voucher for approval. I will not state a figure I have not pulled "
        "from Tally."
    )


@dataclass
class MockProvider:
    """Either replays a script in order, or falls back to the rules above."""

    name: str = "mock"
    model: str = "mock-deterministic"
    script: list[Completion] = field(default_factory=list)
    responder: Responder = default_rules
    calls: list[list[Message]] = field(default_factory=list)
    tools_seen: list[list[dict[str, Any]]] = field(default_factory=list)

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> Completion:
        self.calls.append(list(messages))
        self.tools_seen.append(list(tools or []))
        completion = (
            self.script.pop(0) if self.script else self.responder(messages)
        )
        completion.provider = self.name
        completion.model = self.model
        # Approximate egress so the loop's accounting is exercised too.
        completion.raw_bytes_sent = sum(
            len(m.content.encode("utf-8")) for m in messages
        )
        return completion
