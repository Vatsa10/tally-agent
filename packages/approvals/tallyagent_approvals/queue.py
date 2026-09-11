"""The approval queue.

Nothing mutates Tally except by passing through here, and every transition is
written to the audit log before the caller hears about it.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine
from sqlmodel import Session, select

from tallyagent_approvals.audit import AuditLog
from tallyagent_approvals.db import ApprovalRow, MemoryRow
from tallyagent_backends.accounting_backend import WriteResult
from tallyagent_core.models import Voucher
from tallyagent_core.validation.report import (
    RuleResult,
    Severity,
    ValidationReport,
)
from tallyagent_tools.base import PendingAction

#: Performs the actual write for an approved action. Injected by the daemon so
#: the queue never imports a backend.
Executor = Callable[[PendingAction], Awaitable[WriteResult]]


def _money(value: Decimal) -> str:
    """Every figure an approver sees is quantised to the paisa."""
    from tallyagent_core.models.voucher import PAISA

    return format(value.quantize(PAISA), "f")


def _dump_validation(report: ValidationReport | None) -> str:
    if report is None:
        return "null"
    return json.dumps(
        {
            "results": [
                {
                    "rule": r.rule,
                    "passed": r.passed,
                    "message": r.message,
                    "severity": r.severity.value,
                    "details": r.details,
                }
                for r in report.results
            ],
            "overridden_by": report.overridden_by,
            "override_reason": report.override_reason,
        },
        default=str,
    )


def _load_validation(raw: str) -> ValidationReport | None:
    data = json.loads(raw)
    if data is None:
        return None
    report = ValidationReport(
        overridden_by=data.get("overridden_by"),
        override_reason=data.get("override_reason"),
    )
    for item in data["results"]:
        report.add(
            RuleResult(
                rule=item["rule"],
                passed=item["passed"],
                message=item["message"],
                severity=Severity(item["severity"]),
                details=item.get("details", {}),
            )
        )
    return report


@dataclass(slots=True)
class ApprovalStats:
    action_type: str
    total: int = 0
    approved: int = 0
    rejected: int = 0
    edited: int = 0

    @property
    def approval_rate(self) -> float:
        decided = self.approved + self.rejected
        return self.approved / decided if decided else 0.0

    @property
    def recommendation(self) -> str:
        """Advice only. Promotion to auto-approve stays a human edit to
        policy.toml - a system that promotes itself is not a control."""
        if self.total < 20:
            return "not enough history to judge"
        if self.edited:
            return "keep manual: approvers are still correcting these"
        if self.approval_rate >= 0.98:
            return "candidate for auto or auto_below_amount in policy.toml"
        return "keep manual"


@dataclass(slots=True)
class ApprovalItem:
    ticket: str
    company: str
    action_type: str
    status: str
    summary: str
    amount: Decimal
    source: str
    created_at: datetime
    idempotency_key: str = ""
    raw_xml: str = ""
    voucher: Voucher | None = None
    validation: ValidationReport | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    decided_by: str = ""
    decision_reason: str = ""
    result: dict[str, Any] | None = None

    def to_pending(self) -> PendingAction:
        return PendingAction(
            action_type=self.action_type,
            company=self.company,
            summary=self.summary,
            voucher=self.voucher,
            idempotency_key=self.idempotency_key,
            validation=self.validation,
            raw_xml=self.raw_xml,
            source=self.source,
            payload=self.payload,
        )

    def ledger_impact(self) -> list[dict[str, str]]:
        return self.to_pending().ledger_impact()

    def diff(self) -> dict[str, Any]:
        """Everything an approver needs on one screen."""
        validation = self.validation
        return {
            "ticket": self.ticket,
            "status": self.status,
            "summary": self.summary,
            "company": self.company,
            "action_type": self.action_type,
            "source": self.source,
            "amount": _money(self.amount),
            "created_at": self.created_at.isoformat(),
            "ledger_impact": self.ledger_impact(),
            "totals": {
                "debit": _money(self.voucher.total_debit) if self.voucher else "0.00",
                "credit": _money(self.voucher.total_credit) if self.voucher else "0.00",
            },
            "validation": {
                "ok": validation.ok if validation else True,
                "summary": validation.summary() if validation else "no rules apply",
                "results": [
                    {
                        "rule": r.rule,
                        "passed": r.passed,
                        "severity": r.severity.value,
                        "message": r.message,
                    }
                    for r in (validation.results if validation else [])
                ],
            },
            "raw_xml": self.raw_xml,
        }


class ApprovalQueue:
    def __init__(
        self,
        engine: Engine,
        audit: AuditLog | None = None,
        executor: Executor | None = None,
    ) -> None:
        self.engine = engine
        self.audit = audit or AuditLog(engine)
        self.executor = executor

    # --- enqueue ------------------------------------------------------------

    def next_ticket(self) -> str:
        with Session(self.engine) as session:
            count = len(list(session.exec(select(ApprovalRow))))
        return f"APR-{count + 1:04d}"

    async def enqueue(self, action: PendingAction) -> str:
        """Add a validated action to the queue. Returns its ticket."""
        if action.validation is not None and action.validation.blocks_enqueue:
            raise ValueError(
                "refusing to queue an action whose validation failed and was not "
                f"overridden: {action.validation.summary()}"
            )
        ticket = self.next_ticket()
        row = ApprovalRow(
            ticket=ticket,
            company=action.company,
            action_type=action.action_type,
            summary=action.summary,
            amount=format(action.amount, "f"),
            source=action.source,
            idempotency_key=action.idempotency_key,
            raw_xml=action.raw_xml,
            voucher_json=(
                action.voucher.model_dump_json() if action.voucher else "null"
            ),
            validation_json=_dump_validation(action.validation),
            payload_json=json.dumps(action.payload, default=str),
        )
        with Session(self.engine) as session:
            session.add(row)
            session.commit()
        self.audit.append(
            "action_queued",
            actor=action.source,
            ticket=ticket,
            action_type=action.action_type,
            amount=format(action.amount, "f"),
            summary=action.summary,
            idempotency_key=action.idempotency_key,
        )
        return ticket

    # --- reads --------------------------------------------------------------

    def _to_item(self, row: ApprovalRow) -> ApprovalItem:
        voucher_json = json.loads(row.voucher_json)
        return ApprovalItem(
            ticket=row.ticket,
            company=row.company,
            action_type=row.action_type,
            status=row.status,
            summary=row.summary,
            amount=Decimal(row.amount),
            source=row.source,
            created_at=row.created_at,
            idempotency_key=row.idempotency_key,
            raw_xml=row.raw_xml,
            voucher=Voucher.model_validate(voucher_json) if voucher_json else None,
            validation=_load_validation(row.validation_json),
            payload=json.loads(row.payload_json),
            decided_by=row.decided_by,
            decision_reason=row.decision_reason,
            result=json.loads(row.result_json),
        )

    def get(self, ticket: str) -> ApprovalItem:
        with Session(self.engine) as session:
            row = session.exec(
                select(ApprovalRow).where(ApprovalRow.ticket == ticket)
            ).first()
        if row is None:
            raise KeyError(f"no approval ticket {ticket!r}")
        return self._to_item(row)

    def list(self, status: str = "pending", company: str = "") -> list[ApprovalItem]:
        with Session(self.engine) as session:
            statement = select(ApprovalRow)
            if status:
                statement = statement.where(ApprovalRow.status == status)
            if company:
                statement = statement.where(ApprovalRow.company == company)
            rows = list(session.exec(statement.order_by(ApprovalRow.id)))  # type: ignore[arg-type]
        return [self._to_item(row) for row in rows]

    def stats(self, company: str = "") -> list[ApprovalStats]:
        """Per-action-type history, to inform a policy promotion decision."""
        by_type: dict[str, ApprovalStats] = {}
        with Session(self.engine) as session:
            statement = select(ApprovalRow)
            if company:
                statement = statement.where(ApprovalRow.company == company)
            rows = list(session.exec(statement))
        for row in rows:
            stat = by_type.setdefault(row.action_type, ApprovalStats(row.action_type))
            stat.total += 1
            if row.status == "approved":
                stat.approved += 1
                if row.decision_reason.startswith("edited"):
                    stat.edited += 1
            elif row.status == "rejected":
                stat.rejected += 1
        return sorted(by_type.values(), key=lambda s: s.action_type)

    # --- decisions ----------------------------------------------------------

    def _finish(
        self,
        ticket: str,
        status: str,
        actor: str,
        reason: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        with Session(self.engine) as session:
            row = session.exec(
                select(ApprovalRow).where(ApprovalRow.ticket == ticket)
            ).first()
            if row is None:
                raise KeyError(f"no approval ticket {ticket!r}")
            row.status = status
            row.decided_at = datetime.now(UTC)
            row.decided_by = actor
            row.decision_reason = reason
            row.result_json = json.dumps(result, default=str)
            session.add(row)
            session.commit()

    async def approve(self, ticket: str, actor: str, reason: str = "") -> WriteResult:
        """Approve and perform the write."""
        item = self.get(ticket)
        if item.status != "pending":
            raise ValueError(f"{ticket} is already {item.status}")
        if self.executor is None:
            raise RuntimeError(
                "this ApprovalQueue has no executor; wire one before approving"
            )

        self.audit.append(
            "action_approved",
            actor=actor,
            ticket=ticket,
            action_type=item.action_type,
            amount=format(item.amount, "f"),
            reason=reason,
        )
        result = await self.executor(item.to_pending())
        self._finish(
            ticket,
            "approved" if result.ok else "failed",
            actor,
            reason or "approved",
            {
                "ok": result.ok,
                "voucher_number": result.voucher_number,
                "master_id": result.master_id,
                "replayed": result.replayed,
                "errors": result.errors,
            },
        )
        self.audit.append(
            "action_executed" if result.ok else "action_failed",
            actor=actor,
            ticket=ticket,
            voucher_number=result.voucher_number,
            master_id=result.master_id,
            replayed=result.replayed,
            errors=result.errors,
        )
        return result

    def reject(self, ticket: str, actor: str, reason: str) -> ApprovalItem:
        """Reject. A reason is mandatory: an unexplained rejection teaches
        nobody anything and cannot be audited."""
        if not reason.strip():
            raise ValueError("a rejection must carry a reason")
        item = self.get(ticket)
        if item.status != "pending":
            raise ValueError(f"{ticket} is already {item.status}")
        self._finish(ticket, "rejected", actor, reason)
        self.audit.append(
            "action_rejected",
            actor=actor,
            ticket=ticket,
            action_type=item.action_type,
            reason=reason,
        )
        return self.get(ticket)

    async def edit_and_approve(
        self,
        ticket: str,
        actor: str,
        voucher: Voucher,
        reason: str = "",
    ) -> WriteResult:
        """Replace the draft with a corrected one, then approve it.

        Ledger substitutions made by the approver are recorded as learned
        aliases - the correction a human makes once should not need making twice.
        """
        item = self.get(ticket)
        if item.status != "pending":
            raise ValueError(f"{ticket} is already {item.status}")

        original = item.voucher
        if original is not None:
            self._learn_corrections(item.company, original, voucher)

        # A different voucher is a different write, so it gets a new key.
        from tallyagent_core.idempotency import make_key
        from tallyagent_tally.xml import builders

        new_key = make_key(item.company, voucher)
        with Session(self.engine) as session:
            row = session.exec(
                select(ApprovalRow).where(ApprovalRow.ticket == ticket)
            ).first()
            assert row is not None
            row.voucher_json = voucher.model_dump_json()
            row.amount = format(voucher.amount, "f")
            row.idempotency_key = new_key
            row.raw_xml = builders.to_string(builders.build_voucher_element(voucher))
            session.add(row)
            session.commit()

        self.audit.append(
            "action_edited",
            actor=actor,
            ticket=ticket,
            before=original.model_dump(mode="json") if original else None,
            after=voucher.model_dump(mode="json"),
            reason=reason,
        )
        return await self.approve(ticket, actor, reason=f"edited: {reason}".strip())

    def _learn_corrections(
        self, company: str, before: Voucher, after: Voucher
    ) -> None:
        """Record ledger swaps as aliases, positionally matched."""
        with Session(self.engine) as session:
            for old, new in zip(before.lines, after.lines, strict=False):
                if old.ledger_name == new.ledger_name:
                    continue
                existing = session.exec(
                    select(MemoryRow)
                    .where(MemoryRow.company == company)
                    .where(MemoryRow.kind == "ledger_alias")
                    .where(MemoryRow.key == old.ledger_name)
                ).first()
                if existing is None:
                    existing = MemoryRow(
                        company=company, kind="ledger_alias", key=old.ledger_name
                    )
                existing.value = new.ledger_name
                existing.hits += 1
                existing.updated_at = datetime.now(UTC)
                session.add(existing)
            session.commit()

    # --- memory -------------------------------------------------------------

    def learned_aliases(self, company: str) -> dict[str, str]:
        with Session(self.engine) as session:
            rows = list(
                session.exec(
                    select(MemoryRow)
                    .where(MemoryRow.company == company)
                    .where(MemoryRow.kind == "ledger_alias")
                )
            )
        return {row.key: row.value for row in rows}
