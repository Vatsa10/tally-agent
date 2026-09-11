"""Background jobs: nightly bank reconciliation, monthly GSTR-2B.

Both are *reporting* jobs. They read, classify and report; nothing scheduled is
ever allowed to post. An unattended process that writes to a client's books at
3am is not a feature.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

log = logging.getLogger(__name__)

TICK_SECONDS = 60


@dataclass(slots=True)
class JobRun:
    name: str
    at: datetime
    summary: str
    ok: bool = True


@dataclass(slots=True)
class Scheduler:
    wired: object
    history: list[JobRun] = field(default_factory=list)
    last_bank_reco: date | None = None
    last_gstr2b: tuple[int, int] | None = None

    @property
    def services(self):  # type: ignore[no-untyped-def]
        return self.wired.services  # type: ignore[attr-defined]

    def due_bank_reco(self, now: datetime) -> bool:
        """Nightly, after 22:00 local, once per day."""
        return now.hour >= 22 and self.last_bank_reco != now.date()

    def due_gstr2b(self, now: datetime) -> bool:
        """Monthly, from the 14th - GSTR-2B is generated on the 14th."""
        return now.day >= 14 and self.last_gstr2b != (now.year, now.month)

    async def run_bank_reco(self, now: datetime | None = None) -> JobRun:
        """Report on the bank ledger. Without a statement file there is nothing
        to match against, so this reports the period's movements for review."""
        now = now or datetime.now(UTC)
        from tallyagent_tools import reports

        try:
            result = await reports.day_book(
                self.services.tools,
                from_date=now.date() - timedelta(days=1),
                to_date=now.date(),
            )
            run = JobRun(
                "bank_reco",
                now,
                f"{len(result.data)} ledger line(s) in the last 24h awaiting "
                "reconciliation against a statement.",
            )
        except Exception as exc:  # noqa: BLE001 - a failed job must not kill the daemon
            log.exception("nightly bank reco failed")
            run = JobRun("bank_reco", now, f"failed: {exc}", ok=False)

        self.last_bank_reco = now.date()
        self.history.append(run)
        self.services.audit.append(
            "scheduled_job", actor="scheduler", job="bank_reco", summary=run.summary
        )
        return run

    async def run_gstr2b(self, now: datetime | None = None) -> JobRun:
        """Summarise the purchase register so a 2B comparison can be run."""
        now = now or datetime.now(UTC)
        from tallyagent_tools import reports

        try:
            result = await reports.gstr2_purchase_register(self.services.tools)
            run = JobRun(
                "gstr2b",
                now,
                f"{len(result.data)} purchase voucher(s) ready to compare "
                "against GSTR-2B. Upload the 2B JSON to run the comparison.",
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("monthly GSTR-2B job failed")
            run = JobRun("gstr2b", now, f"failed: {exc}", ok=False)

        self.last_gstr2b = (now.year, now.month)
        self.history.append(run)
        self.services.audit.append(
            "scheduled_job", actor="scheduler", job="gstr2b", summary=run.summary
        )
        return run

    async def tick(self, now: datetime | None = None) -> list[JobRun]:
        now = now or datetime.now(UTC)
        runs = []
        if self.due_bank_reco(now):
            runs.append(await self.run_bank_reco(now))
        if self.due_gstr2b(now):
            runs.append(await self.run_gstr2b(now))
        return runs

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            await asyncio.sleep(TICK_SECONDS)
