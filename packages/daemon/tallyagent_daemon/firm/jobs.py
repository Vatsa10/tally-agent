"""What one piece of the morning's work looks like.

A job is one recurring piece of a practice's work - reconcile GSTR-2B, read the
day's bills, close last month - run once per client. Jobs never post anything
themselves. They return outcomes, and the runner decides what happens to a
draft, so the question "may this go into the books without a person?" is
answered in exactly one place.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

DONE = "done"
QUEUED = "queued"
EXCEPTION = "exception"
KINDS = (DONE, QUEUED, EXCEPTION)


@dataclass(slots=True)
class Outcome:
    """One result of one job for one client, in words a partner can act on."""

    job: str
    kind: str
    title: str
    detail: str = ""
    #: Rupees that are lost, late or wrong if nobody acts. What the inbox sorts by.
    amount_at_risk: Decimal = Decimal("0")
    due: date | None = None
    ticket: str = ""
    #: Distinguishes two outcomes of the same job for the same client, so a
    #: re-run updates the existing inbox line instead of adding a second one.
    subject: str = ""
    client: str = ""
    company: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def key(self, run_day: date) -> str:
        raw = f"{run_day}|{self.client}|{self.job}|{self.subject or self.title}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]


class Job(Protocol):
    """A recurring piece of work, run per client."""

    name: str

    def due(self, client: Any, today: date) -> bool: ...

    async def run(self, ctx: Any, client: Any, today: date) -> list[Outcome]: ...


def previous_month(today: date) -> str:
    """The month a practice is closing on ``today``: last month, as YYYY-MM."""
    first = today.replace(day=1)
    year, month = (first.year, first.month - 1) if first.month > 1 else (first.year - 1, 12)
    return f"{year:04d}-{month:02d}"


@dataclass(slots=True)
class MonthEndJob:
    """The close pack for last month, in the first days of this one."""

    name: str = "month_end"
    window: tuple[int, int] = (1, 7)

    def due(self, client: Any, today: date) -> bool:
        return self.window[0] <= today.day <= self.window[1]

    async def run(self, ctx: Any, client: Any, today: date) -> list[Outcome]:
        from tallyagent_tools import close

        month = previous_month(today)
        result = await close.month_end_close(
            ctx, month, bank_ledger=getattr(client, "bank_ledger", "") or ""
        )
        data = result.data or {}
        outcomes = [
            Outcome(
                job=self.name,
                # "To fix" and "to check" both need a person; only the
                # informational ones are simply done.
                kind=DONE if finding["severity"] == "low" else EXCEPTION,
                title=finding["detail"],
                detail=finding["fix"],
                amount_at_risk=abs(Decimal(finding["amount"] or "0")),
                subject=f"{month}:{finding['kind']}:{finding['detail']}",
                data={"month": month},
            )
            for finding in data.get("findings", [])
        ]
        outcomes.append(
            Outcome(
                job=self.name,
                kind=DONE,
                title=f"Close pack for {month} written",
                detail=str(data.get("pack_path", "")),
                subject=f"{month}:pack",
            )
        )
        return outcomes


@dataclass(slots=True)
class Gstr1Job:
    """Last month's GSTR-1, built and checked well before the 11th."""

    name: str = "gstr1"
    window: tuple[int, int] = (1, 10)

    def due(self, client: Any, today: date) -> bool:
        return self.window[0] <= today.day <= self.window[1]

    async def run(self, ctx: Any, client: Any, today: date) -> list[Outcome]:
        from tallyagent_tools import gstr1

        month = previous_month(today)
        result = await gstr1.gstr1_export(ctx, month)
        data = result.data if isinstance(result.data, dict) else {}
        problems = data.get("problems") or []
        due = today.replace(day=11)
        if problems:
            return [
                Outcome(
                    job=self.name,
                    kind=EXCEPTION,
                    title=f"GSTR-1 {month}: {len(problems)} problem(s) before it can be filed",
                    detail="; ".join(str(p) for p in problems[:5]),
                    due=due,
                    subject=f"{month}:problems",
                )
            ]
        return [
            Outcome(
                job=self.name,
                kind=DONE,
                title=f"GSTR-1 {month} ready to upload",
                detail=result.message,
                due=due,
                subject=f"{month}:ready",
            )
        ]


def default_jobs() -> list[Job]:
    """The jobs every client gets unless told otherwise."""
    from tallyagent_daemon.firm.reconcile import Reconcile2BJob

    # Reconciliation first: it is the job with money and a statutory deadline
    # behind it, and the one most likely to queue drafts the others build on.
    return [Reconcile2BJob(), Gstr1Job(), MonthEndJob()]
