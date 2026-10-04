"""Earned autonomy: hands-off posting that has to be earned, and is lost at once.

The goal is throughput - work done without a person touching it. The obstacle is
that a wrong entry in a client's books sits in Tally's edit log permanently, and
CAs will not hand an agent their clients' books on a promise. So autonomy is not
granted. It is earned, per client and per kind of action, on a measured record:

- A **partner switches it on** for a client, with two limits: how many clean
  approvals in a row an action type needs (``min_streak``), and an amount above
  which nothing posts unattended (``max_amount``). The system promotes only
  inside that grant. It never grants itself one - a system that promotes itself
  is not a control.
- An action type becomes **trusted** when the most recent ``min_streak``
  human decisions on it were all clean approvals: approved as drafted, posted
  successfully, no edit.
- It **loses trust on the first correction**. One rejection, one edit, one
  failed post, and it is back to waiting for a person until it earns the
  streak again.
- Even when trusted it posts only **up to the largest amount a person has
  approved for it** - the record says nothing about amounts nobody has checked.

Every post made this way carries "autonomy" and the record it rested on as its
approver, so the audit chain says exactly why nobody looked at it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine
from sqlmodel import Session, select

from tallyagent_approvals.db import ApprovalRow, AutonomyGrantRow

log = logging.getLogger(__name__)

#: How an approval made by this module is signed. Human decisions are
#: everything else, and only they count towards a streak.
AUTONOMY_ACTOR = "autonomy"

DEFAULT_STREAK = 20
DEFAULT_MAX = Decimal("50000")


@dataclass(frozen=True, slots=True)
class Grant:
    company: str
    granted_by: str
    granted_at: datetime
    min_streak: int
    max_amount: Decimal


@dataclass(slots=True)
class Record:
    """What people have decided about one kind of action for one client."""

    action_type: str
    streak: int = 0
    #: The most a person has approved, clean, within the current streak.
    ceiling: Decimal = Decimal("0")
    last_break: str = ""
    autonomous_posts: int = 0
    human_decisions: int = 0

    def trusted(self, grant: Grant | None) -> bool:
        return grant is not None and self.streak >= grant.min_streak

    def why_not(self, grant: Grant | None) -> str:
        if grant is None:
            return "a partner has not switched on earned autonomy for this client"
        if self.streak < grant.min_streak:
            last = f"; last broken by {self.last_break}" if self.last_break else ""
            return (
                f"needs {grant.min_streak} clean approvals in a row, has "
                f"{self.streak}{last}"
            )
        return ""


def is_autonomous(decided_by: str) -> bool:
    return (decided_by or "").startswith(AUTONOMY_ACTOR)


def records(rows: list[ApprovalRow]) -> dict[str, Record]:
    """Each action type's record, from the decisions in the queue.

    Walks newest first and stops counting the streak at the first thing that
    was not a clean human approval - so one correction anywhere in the recent
    run resets it, however long the run was before it.
    """
    decided = [r for r in rows if r.status in ("approved", "rejected", "failed")]
    decided.sort(key=lambda r: (r.decided_at or r.created_at), reverse=True)

    out: dict[str, Record] = {}
    broken: set[str] = set()
    for row in decided:
        record = out.setdefault(row.action_type, Record(row.action_type))
        if is_autonomous(row.decided_by):
            record.autonomous_posts += 1
            continue
        record.human_decisions += 1
        if row.action_type in broken:
            continue
        clean = row.status == "approved" and not row.decision_reason.startswith("edited")
        if clean:
            record.streak += 1
            record.ceiling = max(record.ceiling, Decimal(row.amount or "0"))
            continue
        broken.add(row.action_type)
        record.last_break = (
            f"a rejection by {row.decided_by}"
            if row.status == "rejected"
            else "an edit by " + row.decided_by
            if row.status == "approved"
            else "a failed post"
        )
    return out


class GrantStore:
    def __init__(self, engine: Engine, audit: Any = None) -> None:
        self.engine = engine
        self.audit = audit

    def grant(
        self,
        company: str,
        by: str,
        min_streak: int = DEFAULT_STREAK,
        max_amount: Decimal = DEFAULT_MAX,
    ) -> Grant:
        if min_streak < 5:
            raise ValueError(
                "a streak shorter than 5 is not a record, it is a coincidence"
            )
        self.revoke(company, by, quiet=True)
        with Session(self.engine) as session:
            row = AutonomyGrantRow(
                company=company,
                granted_by=by,
                min_streak=min_streak,
                max_amount=format(Decimal(max_amount), "f"),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            grant = _grant(row)
        self._note(
            "autonomy_granted",
            by,
            company=company,
            min_streak=min_streak,
            max_amount=format(grant.max_amount, "f"),
        )
        return grant

    def revoke(self, company: str, by: str, quiet: bool = False) -> bool:
        with Session(self.engine) as session:
            live = session.exec(
                select(AutonomyGrantRow).where(
                    AutonomyGrantRow.company == company,
                    AutonomyGrantRow.revoked_at == None,  # noqa: E711
                )
            ).all()
            for row in live:
                row.revoked_at = datetime.now(UTC)
                row.revoked_by = by
                session.add(row)
            session.commit()
        if live and not quiet:
            self._note("autonomy_revoked", by, company=company)
        return bool(live)

    def active(self, company: str) -> Grant | None:
        with Session(self.engine) as session:
            row = session.exec(
                select(AutonomyGrantRow)
                .where(
                    AutonomyGrantRow.company == company,
                    AutonomyGrantRow.revoked_at == None,  # noqa: E711
                )
                .order_by(AutonomyGrantRow.id.desc())  # type: ignore[union-attr]
            ).first()
        return _grant(row) if row is not None else None

    def _note(self, event: str, actor: str, **payload: Any) -> None:
        if self.audit is None:
            return
        try:
            self.audit.append(event, actor=actor, **payload)
        except Exception:  # noqa: BLE001 - never block a grant on the log
            log.exception("could not record %s", event)


def _grant(row: AutonomyGrantRow) -> Grant:
    return Grant(
        company=row.company,
        granted_by=row.granted_by,
        granted_at=row.granted_at,
        min_streak=row.min_streak,
        max_amount=Decimal(row.max_amount or "0"),
    )


def queue_rows(queue: Any, company: str) -> list[ApprovalRow]:
    with Session(queue.engine) as session:
        return list(session.exec(select(ApprovalRow).where(ApprovalRow.company == company)))


@dataclass(slots=True)
class Decision:
    ticket: str
    action_type: str
    amount: Decimal
    posted: bool
    reason: str
    voucher_number: str = ""
    errors: list[str] = field(default_factory=list)


async def decide(queue: Any, grants: GrantStore, company: str) -> list[Decision]:
    """Post what has earned it; say why for everything else.

    Records are recomputed before every ticket, so a post that fails in the
    middle of a run demotes its action type for the rest of that same run.
    """
    grant = grants.active(company)
    decisions: list[Decision] = []
    for item in queue.list("pending", company):
        record = records(queue_rows(queue, company)).get(
            item.action_type, Record(item.action_type)
        )
        amount = Decimal(item.amount or 0)
        reason = record.why_not(grant)
        if not reason and grant is not None and amount > grant.max_amount:
            reason = f"Rs {amount:,.2f} is over the Rs {grant.max_amount:,.2f} the partner allowed"
        if not reason and amount > record.ceiling:
            reason = (
                f"Rs {amount:,.2f} is more than any {item.action_type} a person has "
                f"approved (Rs {record.ceiling:,.2f})"
            )
        if not reason and item.validation is not None and not item.validation.ok:
            reason = "validation raised something a person should see"
        if reason:
            decisions.append(Decision(item.ticket, item.action_type, amount, False, reason))
            continue

        actor = f"{AUTONOMY_ACTOR} ({record.streak} clean approvals, granted by {grant.granted_by})"  # type: ignore[union-attr]
        result = await queue.approve(item.ticket, actor, "earned autonomy")
        decisions.append(
            Decision(
                item.ticket,
                item.action_type,
                amount,
                result.ok,
                "posted under earned autonomy" if result.ok else "the post failed",
                voucher_number=result.voucher_number,
                errors=list(result.errors),
            )
        )
    return decisions


@dataclass(slots=True)
class NoTouch:
    """How much of the posting happened without a person - the throughput number."""

    posted: int = 0
    by_autonomy: int = 0
    by_policy: int = 0

    @property
    def rate(self) -> float:
        return (self.by_autonomy + self.by_policy) / self.posted if self.posted else 0.0

    def describe(self) -> str:
        if not self.posted:
            return "nothing posted yet"
        return (
            f"{self.rate:.0%} no-touch: {self.by_autonomy + self.by_policy} of "
            f"{self.posted} posted without a person "
            f"({self.by_autonomy} earned, {self.by_policy} standing policy)"
        )


def no_touch(queue: Any, audit: Any, company: str, since: date | None = None) -> NoTouch:
    """Posts made without a person, against all posts, for one client."""
    metric = NoTouch()
    for row in queue_rows(queue, company):
        if row.status != "approved":
            continue
        when = (row.decided_at or row.created_at).date()
        if since and when < since:
            continue
        metric.posted += 1
        if is_autonomous(row.decided_by):
            metric.by_autonomy += 1
    if audit is not None:
        for entry in audit.entries():
            if entry.event != "auto_posted" or entry.payload.get("company") != company:
                continue
            if since and entry.at.date() < since:
                continue
            if entry.payload.get("ok", True):
                metric.posted += 1
                metric.by_policy += 1
    return metric
