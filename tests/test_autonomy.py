"""Earned autonomy: hands-off posting that has to be earned, and is lost at once.

The record is built from the decisions people actually made. What is being
protected: nothing posts unattended without a partner's grant, a streak that a
person has not built, or above an amount a person has approved; and one
correction takes trust away immediately.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from tallyagent_approvals import autonomy as rules
from tallyagent_approvals.audit import AuditLog
from tallyagent_approvals.db import ApprovalRow, make_engine

T0 = datetime(2026, 6, 1, 10, tzinfo=UTC)


def row(n: int, status: str = "approved", by: str = "R. Mehta", reason: str = "",
        amount: str = "1000", action: str = "create_receipt") -> ApprovalRow:
    return ApprovalRow(
        ticket=f"APR-{n:04d}",
        company="Sharma Textiles",
        action_type=action,
        status=status,
        amount=amount,
        decided_by=by,
        decision_reason=reason,
        decided_at=T0 + timedelta(minutes=n),
    )


def grant(streak: int = 5, max_amount: str = "50000") -> rules.Grant:
    return rules.Grant("Sharma Textiles", "R. Mehta", T0, streak, Decimal(max_amount))


# --- the record ---------------------------------------------------------------


def test_clean_approvals_build_a_streak():
    record = rules.records([row(i) for i in range(6)])["create_receipt"]

    assert record.streak == 6
    assert record.trusted(grant(streak=5))


def test_one_rejection_resets_the_streak_however_long_it_was():
    history = [row(i) for i in range(30)] + [row(31, status="rejected")]

    record = rules.records(history)["create_receipt"]

    assert record.streak == 0
    assert "rejection by R. Mehta" in record.last_break


def test_an_edit_counts_as_a_correction():
    """Approved, but somebody had to change it first - that is not clean."""
    history = [row(i) for i in range(10)] + [row(11, reason="edited: wrong ledger")]

    record = rules.records(history)["create_receipt"]

    assert record.streak == 0
    assert "edit" in record.last_break


def test_a_failed_post_counts_as_a_correction():
    history = [row(i) for i in range(10)] + [row(11, status="failed")]

    assert rules.records(history)["create_receipt"].streak == 0


def test_the_streak_counts_only_since_the_last_correction():
    history = [row(1, status="rejected")] + [row(i) for i in range(2, 9)]

    assert rules.records(history)["create_receipt"].streak == 7


def test_what_autonomy_posted_itself_does_not_count_towards_its_own_record():
    """Otherwise it would vouch for itself."""
    history = [row(i) for i in range(3)] + [
        row(i, by="autonomy (3 clean approvals, granted by R. Mehta)") for i in range(4, 20)
    ]

    record = rules.records(history)["create_receipt"]

    assert record.streak == 3
    assert record.autonomous_posts == 16


def test_each_kind_of_action_has_its_own_record():
    history = [row(i) for i in range(6)] + [row(7, status="rejected", action="create_payment")]

    records = rules.records(history)

    assert records["create_receipt"].streak == 6
    assert records["create_payment"].streak == 0


def test_the_ceiling_is_the_largest_amount_a_person_approved_in_the_streak():
    history = [row(1, amount="900000", status="rejected")] + [
        row(i, amount=str(1000 * i)) for i in range(2, 8)
    ]

    assert rules.records(history)["create_receipt"].ceiling == Decimal("7000")


def test_without_a_grant_nothing_is_trusted_whatever_the_record():
    record = rules.records([row(i) for i in range(100)])["create_receipt"]

    assert not record.trusted(None)
    assert "partner has not switched on" in record.why_not(None)


def test_the_reason_says_how_far_off_it_is():
    record = rules.records([row(i) for i in range(3)])["create_receipt"]

    assert "needs 5 clean approvals in a row, has 3" in record.why_not(grant(streak=5))


# --- the grant ----------------------------------------------------------------


@pytest.fixture
def engine():
    return make_engine(":memory:")


def test_a_grant_is_recorded_with_the_partner_and_the_limits(engine):
    audit = AuditLog(engine)
    store = rules.GrantStore(engine, audit)

    granted = store.grant(
        "Sharma Textiles", by="R. Mehta", min_streak=10, max_amount=Decimal("25000")
    )

    assert store.active("Sharma Textiles") == granted
    entry = next(e for e in audit.entries() if e.event == "autonomy_granted")
    assert entry.actor == "R. Mehta"
    assert entry.payload["min_streak"] == 10


def test_a_streak_too_short_to_mean_anything_is_refused(engine):
    with pytest.raises(ValueError, match="coincidence"):
        rules.GrantStore(engine).grant("Sharma Textiles", by="R. Mehta", min_streak=2)


def test_revoking_ends_it(engine):
    store = rules.GrantStore(engine)
    store.grant("Sharma Textiles", by="R. Mehta")

    assert store.revoke("Sharma Textiles", by="R. Mehta")
    assert store.active("Sharma Textiles") is None


def test_a_new_grant_replaces_the_old_one(engine):
    store = rules.GrantStore(engine)
    store.grant("Sharma Textiles", by="R. Mehta", min_streak=20)
    store.grant("Sharma Textiles", by="S. Iyer", min_streak=10)

    assert store.active("Sharma Textiles").granted_by == "S. Iyer"


# --- the metric ---------------------------------------------------------------


def test_no_touch_counts_both_earned_and_standing_policy(engine):
    from sqlmodel import Session

    from tallyagent_approvals.queue import ApprovalQueue

    audit = AuditLog(engine)
    queue = ApprovalQueue(engine, audit)
    with Session(engine) as session:
        for r in [row(1), row(2, by="autonomy (20 clean, granted by R. Mehta)"),
                  row(3, by="autonomy (21 clean, granted by R. Mehta)")]:
            session.add(r)
        session.commit()
    audit.append("auto_posted", actor="policy (asked by Nikhil)",
                 company="Sharma Textiles", ok=True)

    metric = rules.no_touch(queue, audit, "Sharma Textiles")

    assert (metric.posted, metric.by_autonomy, metric.by_policy) == (4, 2, 1)
    assert metric.rate == pytest.approx(0.75)
    assert "75% no-touch" in metric.describe()


# --- end to end: the morning run posts what has earned it ---------------------


async def test_the_morning_run_posts_what_has_earned_it_and_queues_the_rest(tmp_path):
    from tallyagent_daemon import clients as clients_mod
    from tallyagent_daemon import config as config_mod
    from tallyagent_daemon import wiring
    from tallyagent_daemon.firm.autonomy import EarnedAutonomy
    from tallyagent_daemon.firm.inbox import Inbox
    from tallyagent_daemon.firm.runner import FirmRunner
    from tallyagent_tally.fake_server import seeded_demo
    from tallyagent_tools import vouchers

    register = clients_mod.parse(
        {
            "storage": {"data_dir": str(tmp_path / "data")},
            "client": [{"slug": "sharma", "company": "Sharma Textiles"}],
        }
    )
    base = config_mod.from_dict({"storage": {"firm_db_path": str(tmp_path / "firm.db")}})
    tally = seeded_demo("Sharma Textiles")
    wired = wiring.build(
        clients_mod.apply(base, register.clients[0], register.data_dir),
        transport=tally.transport,
    )
    services = wired.services

    async def receipt(amount: str, ref: str) -> str:
        result = await vouchers.create_receipt(
            services.tools, party_name="Acme Industries", amount=Decimal(amount),
            voucher_date=date(2026, 6, 2), bank_ledger="Bank - HDFC 1234",
        )
        return str(result.data["ticket"])

    # A partner builds the record by hand: five clean approvals.
    for i in range(5):
        await services.queue.approve(await receipt(f"{1000 + i}", f"R{i}"), "R. Mehta")
    services.autonomy.grant("Sharma Textiles", by="R. Mehta", min_streak=5)

    small = await receipt("900", "SMALL")
    above_record = await receipt("4000", "ABOVE-RECORD")
    above_limit = await receipt("90000", "ABOVE-LIMIT")
    posted_before = len(tally.vouchers)

    runner = FirmRunner(register, lambda c: wired, Inbox(make_engine(str(tmp_path / "firm.db"))),
                        jobs=[], autonomy=EarnedAutonomy())
    report = await runner.run(date(2026, 7, 3))

    titles = [o.title for o in report.outcomes]
    assert services.queue.get(small).status == "approved"
    assert services.queue.get(small).decided_by.startswith("autonomy (5 clean approvals")
    assert services.queue.get(above_record).status == "pending"
    assert services.queue.get(above_limit).status == "pending"
    assert len(tally.vouchers) == posted_before + 1
    assert any("under earned autonomy" in t for t in titles)

    def why(ticket: str) -> str:
        return next(o for o in report.outcomes if o.ticket == ticket).detail

    assert "more than any create_receipt a person has approved" in why(above_record)
    assert "the partner allowed" in why(above_limit)
    assert any("no-touch" in t for t in titles)
