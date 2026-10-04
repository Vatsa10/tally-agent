"""The morning run: every client, every job that is due, one list at the end.

A practice's day starts with the same question for thirty clients: what changed,
what is wrong, what is waiting for me. This walks the client register, checks
each client's Tally can be worked on, runs the jobs that are due, and puts every
outcome into the firm inbox.

Two rules shape it:

- **One client never stops the others.** A Tally that is off, a VPN that has
  dropped, a job that throws - each becomes a line in the report, and the run
  moves to the next client. A morning run that dies on client four is a
  morning run nobody trusts.
- **Jobs do not decide what posts.** They draft and report. Whether a draft
  goes into the books without a person is decided by the autonomy policy the
  runner is given, in one place, the same way for every job.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from tallyagent_daemon.firm.inbox import Inbox
from tallyagent_daemon.firm.jobs import DONE, EXCEPTION, QUEUED, Job, Outcome, default_jobs

log = logging.getLogger(__name__)

#: A client whose Tally takes longer than this to answer everything is reported
#: as stuck rather than holding up the rest of the practice.
CLIENT_TIMEOUT_SECONDS = 600


@dataclass(slots=True)
class ClientReport:
    slug: str
    company: str
    healthy: bool = True
    reason: str = ""
    outcomes: list[Outcome] = field(default_factory=list)
    seconds: float = 0.0

    def count(self, kind: str) -> int:
        return sum(1 for o in self.outcomes if o.kind == kind)

    @property
    def at_risk(self) -> Decimal:
        return sum((o.amount_at_risk for o in self.outcomes), Decimal("0"))


@dataclass(slots=True)
class RunReport:
    run_id: str
    day: date
    clients: list[ClientReport] = field(default_factory=list)

    @property
    def outcomes(self) -> list[Outcome]:
        return [o for c in self.clients for o in c.outcomes]

    def count(self, kind: str) -> int:
        return sum(c.count(kind) for c in self.clients)

    @property
    def at_risk(self) -> Decimal:
        return sum((c.at_risk for c in self.clients), Decimal("0"))

    def summary(self) -> str:
        down = [c.slug for c in self.clients if not c.healthy]
        return (
            f"{len(self.clients)} client(s): {self.count(DONE)} done, "
            f"{self.count(QUEUED)} waiting for approval, "
            f"{self.count(EXCEPTION)} need a person"
            + (f", Rs {self.at_risk:,.2f} at risk" if self.at_risk else "")
            + (f". Could not work on: {', '.join(down)}" if down else "")
            + "."
        )


#: Builds everything needed to work on one client: its Tally connection, its
#: database, its tools. ``tallyagent_daemon.wiring.build`` behind
#: ``clients.apply`` in production; a fake Tally per client in tests.
Wire = Callable[[Any], Any]


class FirmRunner:
    def __init__(
        self,
        register: Any,
        wire: Wire,
        inbox: Inbox,
        jobs: list[Job] | None = None,
        autonomy: Any = None,
        timeout: float = CLIENT_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.register = register
        self.wire = wire
        self.inbox = inbox
        self.jobs = list(jobs) if jobs is not None else default_jobs()
        self.autonomy = autonomy
        self.timeout = timeout
        self.clock = clock

    async def run(
        self,
        today: date,
        only: list[str] | None = None,
        force: bool = False,
        run_id: str = "",
    ) -> RunReport:
        """Run every due job for every client (or ``only`` these).

        ``force`` runs every job whether or not it is due - for a partner who
        wants the numbers now rather than on the day the calendar says.
        """
        run_id = run_id or f"run-{today.isoformat()}-{int(self.clock() * 1000) % 10**8}"
        report = RunReport(run_id=run_id, day=today)
        for client in self.register.clients:
            if only and client.slug not in only:
                continue
            started = self.clock()
            try:
                result = await asyncio.wait_for(
                    self._client(client, today, force), timeout=self.timeout
                )
            except TimeoutError:
                result = ClientReport(
                    slug=client.slug,
                    company=client.company,
                    healthy=False,
                    reason=f"stopped after {self.timeout:.0f}s without finishing",
                )
            except Exception as exc:  # noqa: BLE001 - one client never stops the run
                log.exception("client %s failed", client.slug)
                result = ClientReport(
                    slug=client.slug,
                    company=client.company,
                    healthy=False,
                    reason=f"{type(exc).__name__}: {exc}",
                )
            result.seconds = round(self.clock() - started, 2)
            if not result.healthy:
                result.outcomes.append(
                    Outcome(
                        job="health",
                        kind=EXCEPTION,
                        title=f"Could not work on {client.company}",
                        detail=result.reason,
                        subject="health",
                    )
                )
            for outcome in result.outcomes:
                outcome.client = client.slug
                outcome.company = client.company
            report.clients.append(result)
        self.inbox.record(run_id, today, report.outcomes)
        return report

    async def _client(self, client: Any, today: date, force: bool) -> ClientReport:
        report = ClientReport(slug=client.slug, company=client.company)
        wired = self.wire(client)
        reason = await health(wired, client)
        if reason:
            report.healthy = False
            report.reason = reason
            return report

        report.outcomes.extend(await compliance(wired, client))
        ctx = wired.services.tools
        for job in self.jobs:
            if not (force or job.due(client, today)):
                continue
            try:
                outcomes = await job.run(ctx, client, today)
            except Exception as exc:  # noqa: BLE001 - one job never stops the others
                log.exception("job %s failed for %s", job.name, client.slug)
                outcomes = [
                    Outcome(
                        job=job.name,
                        kind=EXCEPTION,
                        title=f"{job.name} could not run",
                        detail=f"{type(exc).__name__}: {exc}",
                        subject="failed",
                    )
                ]
            report.outcomes.extend(outcomes)

        if self.autonomy is not None:
            report.outcomes.extend(await self.autonomy.apply(wired, client, today))
        return report


async def health(wired: Any, client: Any) -> str:
    """Why this client cannot be worked on right now, or "" if it can."""
    backend = wired.backend
    try:
        companies = await backend.list_companies()
    except Exception as exc:  # noqa: BLE001 - unreachable is a reason, not a crash
        return f"Tally is not answering at {client.host}:{client.port} ({exc})"
    if client.company not in companies:
        loaded = ", ".join(companies) or "nothing"
        return f"{client.company} is not loaded in Tally (open: {loaded})"
    return ""


async def compliance(wired: Any, client: Any) -> list[Outcome]:
    """Things about the books themselves a partner must know before trusting them.

    Today, one: Rule 3(1) of the Companies (Accounts) Rules requires an audit
    trail that cannot be disabled, since April 2023, and Rule 11(g) makes the
    auditor report on it. In Tally that is the edit log. A company client
    without it is a qualified audit report waiting to happen - and anything the
    agent posts into those books would be posted without one.
    """
    if not getattr(client, "needs_edit_log", False):
        return []
    try:
        on = await wired.backend.edit_log_on(client.company)
    except Exception as exc:  # noqa: BLE001 - an unanswered check is not a crash
        log.info("could not read the edit log flag for %s: %s", client.slug, exc)
        return []
    if on is True:
        return []
    state = "is off" if on is False else "could not be confirmed"
    return [
        Outcome(
            job="compliance",
            kind=EXCEPTION,
            title=f"Tally's edit log {state} for {client.company}",
            detail=(
                "Companies Act Rule 3(1) requires an audit trail that cannot be "
                "switched off, and the auditor reports on it under Rule 11(g). "
                "Use a TallyPrime release with Edit Log and keep it on for this "
                "company before its books are audited."
            ),
            subject="edit-log",
        )
    ]
