"""The tier router.

Tier 1 (API) does the work. Tier 2 (screen perception) only observes, and only
when switched on. Tier 3 (computer use) is off by default and never routine.

The routing decision is deliberately dull: if a tool exists for the task, it is
Tier 1, full stop. Tier 3 exists so that the absence of a tool is visible and
recorded - every fallback emits an event naming the tool that should have
existed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import IntEnum

from tallyagent_core.errors import PolicyError
from tallyagent_tools import registry

log = logging.getLogger(__name__)


class Tier(IntEnum):
    API = 1
    PERCEIVE = 2
    FALLBACK = 3


@dataclass(slots=True)
class TierConfig:
    perception_enabled: bool = False
    perception_interval_seconds: int = 30
    fallback_enabled: bool = False
    fallback_max_steps: int = 10
    #: Draw a red ring where Tier 3 is about to click and caption every
    #: keystroke, so a person can see what is being done to their books
    #: as it happens. On whenever the fallback is.
    show_cursor: bool = True


@dataclass(slots=True)
class Routing:
    tier: Tier
    reason: str
    tool: str = ""


@dataclass(slots=True)
class FallbackEvent:
    """Emitted whenever Tier 3 is used. The point of this record is that the
    team builds a proper tool and the event stops appearing."""

    task: str
    reason: str
    suggested_tool: str = ""


@dataclass(slots=True)
class TierRouter:
    config: TierConfig = field(default_factory=TierConfig)
    fallback_events: list[FallbackEvent] = field(default_factory=list)

    def route(self, task: str, tool_name: str = "") -> Routing:
        """Decide which tier handles a task."""
        if tool_name and tool_name in registry.BY_NAME:
            return Routing(Tier.API, "a typed tool exists for this", tool_name)
        if not tool_name:
            return Routing(Tier.API, "no specific tool named; the agent will choose")

        if not self.config.fallback_enabled:
            raise PolicyError(
                f"no tool named {tool_name!r} and the Tier 3 computer-use fallback "
                "is disabled. Enable [tiers] fallback_enabled in config.toml only "
                "if you accept a bounded, recorded, approval-gated UI session - "
                "or better, build the missing tool."
            )
        event = FallbackEvent(
            task=task,
            reason=f"no Tier 1 tool named {tool_name!r}",
            suggested_tool=tool_name,
        )
        self.fallback_events.append(event)
        log.warning("fallback_used: %s (%s)", event.task, event.reason)
        return Routing(Tier.FALLBACK, event.reason, tool_name)

    def perception_allowed(self) -> bool:
        return self.config.perception_enabled

    def require_perception(self) -> None:
        if not self.config.perception_enabled:
            raise PolicyError(
                "Tier 2 screen perception is disabled. Set [tiers] "
                "perception_enabled = true in config.toml to allow tallyagent to "
                "capture the active Tally window."
            )

    def require_fallback(self) -> None:
        if not self.config.fallback_enabled:
            raise PolicyError(
                "Tier 3 computer-use fallback is disabled. It is off by default "
                "and must be enabled explicitly in config.toml."
            )
