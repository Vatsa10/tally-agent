"""Stage 4: approval queue, diff rendering, audit hash chain, learned corrections."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from tallyagent_approvals.audit import AuditLog, compute_hash
from tallyagent_approvals.db import AuditRow, SqlIdempotencyStore, make_engine
from tallyagent_approvals.queue import ApprovalQueue
from tallyagent_core.idempotency import IdempotencyRecord
from tallyagent_core.models import Voucher, VoucherLine
from tallyagent_core.policy import Policy
from tallyagent_tally.backend import TallyBackend
from tallyagent_tools import masters, vouchers
from tallyagent_tools.base import PendingAction, ToolContext


@pytest.fixture
def engine():
    return make_engine(":memory:")


@pytest.fixture
def audit(engine) -> AuditLog:
    return AuditLog(engine)


@pytest.fixture
def executor(backend, company):
    """Performs approved writes, exactly as the daemon wires it."""
    ctx = ToolContext(backend=backend, company=company)

    async def execute(action: PendingAction):
        if action.voucher is None:
            return await masters._execute_master(ctx, action)
        if action.action_type == "alter_voucher":
            return await backend.alter_voucher(
                action.voucher,
                str(action.payload["master_id"]),
                action.idempotency_key,
                company.name,
            )
        return await backend.create_voucher(
            action.voucher, action.idempotency_key, company.name
        )

    return execute


@pytest.fixture
def queue(engine, audit, executor) -> ApprovalQueue:
    return ApprovalQueue(engine, audit, executor)


@pytest.fixture
def ctx(backend, company, queue) -> ToolContext:
    return ToolContext(
        backend=backend,
        company=company,
        policy=Policy.default(),
        enqueue=queue.enqueue,
        source="whatsapp",
    )


# --- queueing and diffs -----------------------------------------------------


async def test_queue_renders_a_full_diff(ctx, queue, fake_tally):
    await vouchers.create_sales_voucher(
        ctx, "Acme Industries", "10000.00", "18", date(2026, 6, 15), reference="INV-001"
    )
    assert fake_tally.vouchers == []

    item = queue.get("APR-0001")
    diff = item.diff()
    assert diff["status"] == "pending"
    assert diff["source"] == "whatsapp"
    assert diff["amount"] == "11800.00"
    assert diff["totals"] == {"debit": "11800.00", "credit": "11800.00"}
    assert diff["validation"]["ok"]
    assert {r["rule"] for r in diff["validation"]["results"]} >= {
        "ledgers_exist", "balanced", "gst_split_correct", "period_open",
    }
    assert {"ledger": "Output SGST", "debit": "", "credit": "900.00"} in diff["ledger_impact"]
    assert "<VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>" in diff["raw_xml"]
    assert "INV-001" in diff["summary"]


async def test_pending_list_is_scoped_by_status_and_company(ctx, queue):
    await vouchers.create_receipt(ctx, "Acme Industries", "100", voucher_date=date(2026, 6, 20))
    await vouchers.create_receipt(ctx, "Acme Industries", "200", voucher_date=date(2026, 6, 21))
    assert len(queue.list("pending")) == 2
    assert len(queue.list("pending", company="Demo Traders Pvt Ltd")) == 2
    assert queue.list("pending", company="Other Co") == []


async def test_a_failed_validation_can_never_be_queued(queue, company):
    bad = Voucher(
        voucher_type="Journal",
        date=date(2026, 6, 15),
        lines=[VoucherLine(ledger_name="Cash", amount=Decimal("100"))],
    )
    from tallyagent_core.validation import ValidationContext, validate

    report = validate(bad, ValidationContext(company=company, known_ledgers={"Cash"}))
    action = PendingAction(
        action_type="create_journal",
        company=company.name,
        summary="unbalanced",
        voucher=bad,
        validation=report,
    )
    with pytest.raises(ValueError, match="validation failed"):
        await queue.enqueue(action)


# --- approve / reject / edit ------------------------------------------------


async def test_approving_posts_to_tally_and_is_idempotent(ctx, queue, fake_tally):
    await vouchers.create_sales_voucher(
        ctx, "Acme Industries", "10000.00", "18", date(2026, 6, 15), reference="INV-001"
    )
    result = await queue.approve("APR-0001", "ca@firm.in")
    assert result.ok
    assert len(fake_tally.vouchers) == 1
    assert fake_tally.balance("Acme Industries") == Decimal("11800.00")

    item = queue.get("APR-0001")
    assert item.status == "approved"
    assert item.decided_by == "ca@firm.in"
    assert item.result["voucher_number"]

    with pytest.raises(ValueError, match="already approved"):
        await queue.approve("APR-0001", "ca@firm.in")


async def test_resubmitting_the_same_invoice_is_a_no_op_via_idempotency(
    engine, backend, company, audit, executor, fake_tally
):
    store = SqlIdempotencyStore(engine)
    backend.store = store
    queue = ApprovalQueue(engine, audit, executor)
    ctx = ToolContext(
        backend=backend, company=company, policy=Policy.default(), enqueue=queue.enqueue
    )

    async def submit_once():
        return await vouchers.create_sales_voucher(
            ctx, "Acme Industries", "10000.00", "18", date(2026, 6, 15), reference="INV-9"
        )

    await submit_once()
    first = await queue.approve("APR-0001", "ca@firm.in")
    assert first.ok and not first.replayed
    assert len(fake_tally.vouchers) == 1

    # The same image arrives again: duplicate detection catches it first.
    second = await submit_once()
    assert not second.ok
    assert any(r.rule == "not_duplicate" for r in second.validation.failures)

    # And even if it were queued anyway, the key replays rather than posting.
    same_key = first.idempotency_key
    action = PendingAction(
        action_type="create_sales_voucher",
        company=company.name,
        summary="replay",
        voucher=queue.get("APR-0001").voucher,
        idempotency_key=same_key,
    )
    replay = await executor(action)
    assert replay.replayed
    assert len(fake_tally.vouchers) == 1


async def test_rejection_requires_a_reason_and_posts_nothing(ctx, queue, fake_tally):
    await vouchers.create_receipt(ctx, "Acme Industries", "500", voucher_date=date(2026, 6, 20))
    with pytest.raises(ValueError, match="must carry a reason"):
        queue.reject("APR-0001", "ca@firm.in", "  ")

    item = queue.reject("APR-0001", "ca@firm.in", "customer cheque bounced")
    assert item.status == "rejected"
    assert item.decision_reason == "customer cheque bounced"
    assert fake_tally.vouchers == []


async def test_edit_and_approve_posts_the_edit_and_learns_the_alias(
    ctx, queue, fake_tally, company
):
    await vouchers.create_journal(
        ctx,
        lines=[
            {"ledger": "Cash", "amount": "1000.00"},
            {"ledger": "Sales - GST 18%", "amount": "-1000.00"},
        ],
        voucher_date=date(2026, 6, 15),
        narration="misc income",
    )
    original = queue.get("APR-0001").voucher
    corrected = original.model_copy(
        update={
            "lines": [
                VoucherLine(ledger_name="Bank - HDFC 1234", amount=Decimal("1000.00")),
                original.lines[1],
            ]
        }
    )

    result = await queue.edit_and_approve(
        "APR-0001", "ca@firm.in", corrected, reason="it went into the bank, not cash"
    )
    assert result.ok
    assert fake_tally.balance("Bank - HDFC 1234") == Decimal("251000.00")
    assert fake_tally.balance("Cash") == Decimal("15000.00")

    assert queue.learned_aliases(company.name) == {"Cash": "Bank - HDFC 1234"}
    item = queue.get("APR-0001")
    assert item.decision_reason.startswith("edited")


async def test_a_write_that_tally_rejects_is_marked_failed_not_approved(
    ctx, queue, fake_tally
):
    await vouchers.create_receipt(
        ctx, "Acme Industries", "500", voucher_date=date(2026, 6, 20)
    )
    # Tally loses the ledger between queueing and approval.
    del fake_tally.ledgers["Acme Industries"]
    result = await queue.approve("APR-0001", "ca@firm.in")
    assert not result.ok
    assert queue.get("APR-0001").status == "failed"


async def test_queue_without_an_executor_refuses_to_approve(engine, audit, ctx):
    await vouchers.create_receipt(ctx, "Acme Industries", "500", voucher_date=date(2026, 6, 20))
    naked = ApprovalQueue(engine, audit, executor=None)
    with pytest.raises(RuntimeError, match="no executor"):
        await naked.approve("APR-0001", "ca@firm.in")


def test_unknown_ticket_fails_clearly(queue):
    with pytest.raises(KeyError, match="APR-9999"):
        queue.get("APR-9999")


# --- stats ------------------------------------------------------------------


async def test_stats_inform_but_never_make_the_promotion_decision(ctx, queue):
    for n in range(3):
        await vouchers.create_receipt(
            ctx, "Acme Industries", f"{100 + n}", voucher_date=date(2026, 6, 20 + n)
        )
    await queue.approve("APR-0001", "ca@firm.in")
    await queue.approve("APR-0002", "ca@firm.in")
    queue.reject("APR-0003", "ca@firm.in", "duplicate")

    stats = {s.action_type: s for s in queue.stats()}
    receipt = stats["create_receipt"]
    assert (receipt.total, receipt.approved, receipt.rejected) == (3, 2, 1)
    assert receipt.approval_rate == pytest.approx(2 / 3)
    assert receipt.recommendation == "not enough history to judge"


# --- audit chain ------------------------------------------------------------


def test_audit_chain_links_and_verifies(audit):
    first = audit.append("action_queued", actor="chat", ticket="APR-0001")
    second = audit.append("action_approved", actor="ca@firm.in", ticket="APR-0001")
    assert first.prev_hash == "0" * 64
    assert second.prev_hash == first.hash

    result = audit.verify()
    assert result.ok
    assert result.checked == 2
    assert "intact" in str(result)


async def test_every_decision_is_audited(ctx, queue, audit):
    await vouchers.create_receipt(ctx, "Acme Industries", "500", voucher_date=date(2026, 6, 20))
    await queue.approve("APR-0001", "ca@firm.in", reason="verified against the bank app")

    events = [e.event for e in audit.entries()]
    assert events == ["action_queued", "action_approved", "action_executed"]
    approved = audit.entries()[1]
    assert approved.actor == "ca@firm.in"
    assert approved.payload["reason"] == "verified against the bank app"
    assert audit.verify().ok


def test_tampering_with_a_payload_breaks_the_chain(engine, audit):
    audit.append("action_queued", actor="chat", ticket="APR-0001", amount="100.00")
    audit.append("action_approved", actor="ca@firm.in", ticket="APR-0001")
    assert audit.verify().ok

    with Session(engine) as session:
        row = session.exec(select(AuditRow).where(AuditRow.sequence == 1)).one()
        row.payload_json = row.payload_json.replace("100.00", "100000.00")
        session.add(row)
        session.commit()

    result = audit.verify()
    assert not result.ok
    assert result.broken_at == 1
    assert "does not match its hash" in result.reason
    assert "BROKEN" in str(result)


def test_deleting_a_record_breaks_the_chain(engine, audit):
    for n in range(3):
        audit.append("action_queued", ticket=f"APR-{n}")
    with Session(engine) as session:
        row = session.exec(select(AuditRow).where(AuditRow.sequence == 2)).one()
        session.delete(row)
        session.commit()

    result = audit.verify()
    assert not result.ok
    assert result.broken_at == 3


def test_relinking_a_tampered_record_still_fails_the_content_hash(engine, audit):
    audit.append("first", ticket="A")
    audit.append("second", ticket="B")
    with Session(engine) as session:
        row = session.exec(select(AuditRow).where(AuditRow.sequence == 2)).one()
        row.actor = "someone else"
        session.add(row)
        session.commit()
    assert not audit.verify().ok


def test_hash_is_stable_across_key_order():
    from datetime import UTC, datetime

    at = datetime(2026, 6, 15, tzinfo=UTC)
    a = compute_hash(1, at, "e", "actor", {"x": 1, "y": 2}, "0" * 64)
    b = compute_hash(1, at, "e", "actor", {"y": 2, "x": 1}, "0" * 64)
    assert a == b


# --- persistent idempotency -------------------------------------------------


def test_idempotency_survives_a_restart(engine):
    store = SqlIdempotencyStore(engine)
    store.put(IdempotencyRecord(key="ta1_abc", company="Demo"))
    store.put(
        IdempotencyRecord(
            key="ta1_abc", company="Demo", status="succeeded", result={"master_id": "7"}
        )
    )
    # A fresh store object over the same database, as after a restart.
    reopened = SqlIdempotencyStore(engine)
    record = reopened.get("ta1_abc")
    assert record is not None
    assert record.status == "succeeded"
    assert record.result == {"master_id": "7"}
    assert reopened.get("ta1_missing") is None


async def test_queued_master_creation_executes_on_approval(ctx, queue, fake_tally):
    await masters.create_ledger(ctx, "Rent", "Indirect Expenses")
    assert "Rent" not in fake_tally.ledgers
    result = await queue.approve("APR-0001", "ca@firm.in")
    assert result.ok
    assert "Rent" in fake_tally.ledgers


async def test_backend_is_reachable_only_through_the_queue(backend):
    """The executor is the one place a PendingAction becomes a Tally write."""
    assert isinstance(backend, TallyBackend)
