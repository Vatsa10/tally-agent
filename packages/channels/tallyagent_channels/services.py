"""What every channel needs, wired once by the daemon.

Channels (web, WhatsApp, MCP) differ in transport and in who is speaking. They
must not differ in what they are allowed to do, so they all receive the same
object and none of them constructs a backend of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tallyagent_agent.context import ContextBuilder
from tallyagent_agent.loop import Agent
from tallyagent_agent.memory import Memory
from tallyagent_approvals.audit import AuditLog
from tallyagent_approvals.queue import ApprovalQueue
from tallyagent_core.models import Company
from tallyagent_llm.router import Router
from tallyagent_tools.base import ToolContext


@dataclass(slots=True)
class Services:
    company: Company
    tools: ToolContext
    queue: ApprovalQueue
    audit: AuditLog
    router: Router
    memory: Memory | None = None
    max_steps: int = 12
    #: One Agent per conversation key (a browser session, a phone number), so
    #: two people talking at once do not share a history.
    _agents: dict[str, Agent] = field(default_factory=dict)

    def agent(self, conversation: str = "default", read_only: bool = False) -> Agent:
        existing = self._agents.get(conversation)
        if existing is not None:
            return existing
        builder = ContextBuilder(
            company=self.company,
            memory_summary=self.memory.summary() if self.memory else "",
        )
        agent = Agent(
            self.router,
            self.tools,
            builder,
            max_steps=self.max_steps,
            read_only=read_only,
        )
        self._agents[conversation] = agent
        return agent

    def reset(self, conversation: str = "default") -> None:
        self._agents.pop(conversation, None)

    def refresh_aliases(self) -> None:
        """Pull approver-taught aliases into the tool context.

        Called after an edit-and-approve so the next draft already knows the
        correction, without restarting the daemon.
        """
        self.tools.ledger_aliases = self.queue.learned_aliases(self.company.name)

    def status(self) -> dict[str, Any]:
        pending = self.queue.list("pending", self.company.name)
        return {
            "company": self.company.name,
            "pending": len(pending),
            "provider": self.router.name,
            "model": self.router.model,
            "tokens": self.router.total_tokens,
            "cost_usd": format(self.router.total_cost, "f"),
            "audit": str(self.audit.verify()),
        }
