"""The partner's morning: every client's outcomes in one list.

Stored in the firm database, not a client's. Ranked by what is at stake - an
exception before a queued draft before something already done, then rupees at
risk, then the nearest deadline - because a partner with forty clients reads
the top of the list and not the bottom.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Engine
from sqlmodel import Session, select

from tallyagent_approvals.db import InboxRow
from tallyagent_daemon.firm.jobs import DONE, EXCEPTION, QUEUED, Outcome

#: Exceptions first: they are the only thing nobody else will do.
RANK = {EXCEPTION: 0, QUEUED: 1, DONE: 2}


@dataclass(frozen=True, slots=True)
class Item:
    id: int
    client: str
    company: str
    job: str
    kind: str
    title: str
    detail: str
    amount_at_risk: Decimal
    due: str
    ticket: str
    resolved: bool
    run_id: str

    def line(self) -> str:
        money = f"  Rs {self.amount_at_risk:,.2f}" if self.amount_at_risk else ""
        due = f"  due {self.due}" if self.due else ""
        return f"[{self.kind:<9}] {self.client:<12} {self.title}{money}{due}"


class Inbox:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def record(self, run_id: str, run_day: date, outcomes: list[Outcome]) -> int:
        """Add or update outcomes. Returns how many lines are new.

        Keyed on day, client, job and subject: pressing "run" twice in a
        morning refreshes the same lines rather than doubling the list. A line
        somebody already resolved stays resolved.
        """
        added = 0
        now = datetime.now(UTC)
        fresh = {outcome.key(run_day) for outcome in outcomes}
        reported = {(outcome.client, outcome.job) for outcome in outcomes}
        with Session(self.engine) as session:
            # A job that has reported again for a client speaks for that
            # client's current state: its earlier lines it no longer reports
            # are over - the 2B that "has not been downloaded" was downloaded.
            # Jobs that did not run this time keep their lines.
            for row in session.exec(
                select(InboxRow).where(InboxRow.resolved == False)  # noqa: E712
            ).all():
                if (row.client, row.job) in reported and row.key not in fresh:
                    row.resolved = True
                    row.resolved_by = "superseded by a later run"
                    row.updated_at = now
                    session.add(row)
            for outcome in outcomes:
                key = outcome.key(run_day)
                row = session.exec(select(InboxRow).where(InboxRow.key == key)).first()
                if row is None:
                    row = InboxRow(key=key, run_id=run_id, client=outcome.client, job=outcome.job)
                    added += 1
                row.run_id = run_id
                row.company = outcome.company
                row.kind = outcome.kind
                row.title = outcome.title
                row.detail = outcome.detail
                row.amount_at_risk = format(outcome.amount_at_risk, "f")
                row.due = outcome.due.isoformat() if outcome.due else ""
                row.ticket = outcome.ticket
                row.updated_at = now
                session.add(row)
            session.commit()
        return added

    def open(self, client: str = "", limit: int = 200) -> list[Item]:
        """Everything not yet resolved, worst first."""
        with Session(self.engine) as session:
            query = select(InboxRow).where(InboxRow.resolved == False)  # noqa: E712
            if client:
                query = query.where(InboxRow.client == client)
            rows = list(session.exec(query).all())
        items = [_item(row) for row in rows]
        items.sort(
            key=lambda i: (RANK.get(i.kind, 9), -i.amount_at_risk, i.due or "9999", i.client)
        )
        return items[:limit]

    def resolve(self, item_id: int, by: str) -> bool:
        with Session(self.engine) as session:
            row = session.get(InboxRow, item_id)
            if row is None:
                return False
            row.resolved = True
            row.resolved_by = by
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.commit()
        return True

    def for_run(self, run_id: str) -> list[Item]:
        with Session(self.engine) as session:
            rows = list(session.exec(select(InboxRow).where(InboxRow.run_id == run_id)).all())
        return [_item(row) for row in rows]


def _item(row: InboxRow) -> Item:
    return Item(
        id=int(row.id or 0),
        client=row.client,
        company=row.company,
        job=row.job,
        kind=row.kind,
        title=row.title,
        detail=row.detail,
        amount_at_risk=Decimal(row.amount_at_risk or "0"),
        due=row.due,
        ticket=row.ticket,
        resolved=row.resolved,
        run_id=row.run_id,
    )


__all__ = ["DONE", "EXCEPTION", "QUEUED", "Inbox", "Item"]
