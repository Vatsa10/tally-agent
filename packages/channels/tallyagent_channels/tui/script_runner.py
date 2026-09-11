"""The non-interactive twin of the TUI.

Runs the same ``Session`` against a YAML scenario, so the control surface a
human uses is the one CI exercises. A scenario is a list of turns; each turn is
what the user types plus optional assertions about what came back.

Assertions live in the scenario rather than only in the test, because the same
file runs against the live Tally (``scripts/live_e2e.py``) where a pytest
assertion would not be watching.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from tallyagent_channels.services import Services
from tallyagent_channels.tui.session import Session, Turn
from tallyagent_core.livemode import LiveMode

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Step:
    say: str
    expect: list[str] = field(default_factory=list)
    expect_not: list[str] = field(default_factory=list)
    approve: bool = True
    note: str = ""
    optional: bool = False


@dataclass(slots=True)
class Scenario:
    name: str
    description: str = ""
    steps: list[Step] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> Scenario:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Scenario:
        return cls(
            name=str(data.get("name") or "scenario"),
            description=str(data.get("description") or ""),
            steps=[
                Step(
                    say=str(raw["say"]),
                    expect=[str(e) for e in (raw.get("expect") or [])],
                    expect_not=[str(e) for e in (raw.get("expect_not") or [])],
                    approve=bool(raw.get("approve", True)),
                    note=str(raw.get("note") or ""),
                    optional=bool(raw.get("optional", False)),
                )
                for raw in (data.get("steps") or [])
            ],
        )


@dataclass(slots=True)
class StepResult:
    step: Step
    turn: Turn
    missing: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.forbidden

    @property
    def transcript(self) -> str:
        return self.turn.text


@dataclass(slots=True)
class RunResult:
    scenario: Scenario
    steps: list[StepResult] = field(default_factory=list)
    started: datetime = field(default_factory=lambda: datetime.now(UTC))
    summary: str = ""

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.steps if not result.step.optional)

    @property
    def failures(self) -> list[StepResult]:
        return [r for r in self.steps if not r.ok and not r.step.optional]

    def report(self) -> str:
        """A markdown report - what was said, what came back, what was asserted."""
        lines = [
            f"# {self.scenario.name}",
            "",
            self.scenario.description,
            "",
            f"Run: {self.started.isoformat()}",
            f"Result: {'PASS' if self.ok else 'FAIL'} "
            f"({len(self.steps) - len(self.failures)}/{len(self.steps)} steps)",
            "",
        ]
        for index, result in enumerate(self.steps, start=1):
            mark = "ok" if result.ok else ("skip" if result.step.optional else "FAIL")
            lines += [f"## {index}. [{mark}] {result.step.say}", ""]
            if result.step.note:
                lines += [f"> {result.step.note}", ""]
            lines += ["```", result.transcript.strip() or "(no output)", "```", ""]
            if result.missing:
                lines += [f"- expected but not found: {result.missing}"]
            if result.forbidden:
                lines += [f"- present but forbidden: {result.forbidden}"]
            if result.missing or result.forbidden:
                lines.append("")
        lines += ["## Session", "", self.summary, ""]
        return "\n".join(lines)


async def run_scenario(
    services: Services,
    scenario: Scenario,
    live: LiveMode | None = None,
    auto_approve: bool = True,
    actor: str = "script",
    on_step: Any = None,
) -> RunResult:
    """Execute every step. Never stops early: a later step's failure is
    information too, and the report is more useful complete."""
    session = Session(
        services,
        live=live,
        conversation="script",
        actor=actor,
        auto_approve=auto_approve,
    )
    result = RunResult(scenario=scenario)

    for step in scenario.steps:
        session.auto_approve = auto_approve and step.approve
        turn = await session.handle(step.say)
        haystack = turn.text
        step_result = StepResult(
            step=step,
            turn=turn,
            missing=[e for e in step.expect if e.lower() not in haystack.lower()],
            forbidden=[e for e in step.expect_not if e.lower() in haystack.lower()],
        )
        result.steps.append(step_result)
        if on_step is not None:
            on_step(step_result)
        if not step_result.ok:
            log.warning(
                "step %r missing=%s forbidden=%s",
                step.say,
                step_result.missing,
                step_result.forbidden,
            )

    result.summary = session.stats.summary(services)
    return result
