"""Stage 1: core models, validation rules, idempotency, policy."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tallyagent_core import idempotency
from tallyagent_core.models import (
    Company,
    GSTDetails,
    Period,
    Voucher,
    VoucherLine,
    VoucherType,
)
from tallyagent_core.policy import ApprovalMode, Policy
from tallyagent_core.validation import (
    PostedVoucher,
    Severity,
    ValidationContext,
    validate,
)
from tallyagent_core.validation import gstin as gstin_mod

FY = Period(
    start=date(2026, 4, 1), end=date(2027, 3, 31), locked_before=date(2026, 4, 1)
)
COMPANY = Company(
    name="Demo Traders Pvt Ltd",
    state_code="27",
    gstin="27AAPFU0939F1ZV",
    period=FY,
)
LEDGERS = {
    "Acme Industries",
    "Sales - GST 18%",
    "Output CGST",
    "Output SGST",
    "Output IGST",
    "Bank - HDFC 1234",
}


def ctx(**kw: object) -> ValidationContext:
    base: dict[str, object] = {
        "company": COMPANY,
        "known_ledgers": set(LEDGERS),
        "party_state_codes": {"Acme Industries": "27"},
    }
    base.update(kw)
    return ValidationContext(**base)  # type: ignore[arg-type]


def sales_voucher(**kw: object) -> Voucher:
    """Intra-state sale: 10,000 taxable + 9% CGST + 9% SGST = 11,800."""
    defaults: dict[str, object] = {
        "voucher_type": VoucherType.SALES,
        "date": date(2026, 6, 15),
        "party_name": "Acme Industries",
        "reference": "INV-001",
        "lines": [
            VoucherLine(ledger_name="Acme Industries", amount=Decimal("11800.00")),
            VoucherLine(ledger_name="Sales - GST 18%", amount=Decimal("-10000.00")),
            VoucherLine(ledger_name="Output CGST", amount=Decimal("-900.00")),
            VoucherLine(ledger_name="Output SGST", amount=Decimal("-900.00")),
        ],
        "gst": GSTDetails(
            rate=Decimal("18"),
            taxable_value=Decimal("10000.00"),
            cgst=Decimal("900.00"),
            sgst=Decimal("900.00"),
            place_of_supply="27",
        ),
    }
    defaults.update(kw)
    return Voucher(**defaults)  # type: ignore[arg-type]


# --- models -----------------------------------------------------------------


def test_voucher_balances_and_totals():
    v = sales_voucher()
    assert v.total_debit == Decimal("11800.00")
    assert v.total_credit == Decimal("11800.00")
    assert v.is_balanced
    assert v.amount == Decimal("11800.00")


def test_fingerprint_is_line_order_independent():
    v1 = sales_voucher()
    v2 = sales_voucher(lines=list(reversed(v1.lines)))
    assert v1.fingerprint() == v2.fingerprint()


def test_fingerprint_changes_with_amount():
    v1 = sales_voucher()
    lines = [ln.model_copy() for ln in v1.lines]
    lines[0] = lines[0].model_copy(update={"amount": Decimal("11801.00")})
    assert sales_voucher(lines=lines).fingerprint() != v1.fingerprint()


def test_unknown_gst_rate_rejected_at_the_model():
    with pytest.raises(ValueError, match="not in"):
        GSTDetails(rate=Decimal("17"))


def test_unknown_state_code_rejected():
    with pytest.raises(ValueError, match="unknown GST state code"):
        Company(name="X", state_code="55")


def test_period_lock_and_containment():
    assert FY.contains(date(2026, 6, 15))
    assert not FY.contains(date(2027, 4, 1))
    assert FY.is_locked(date(2026, 3, 31))


# --- GSTIN ------------------------------------------------------------------


@pytest.mark.parametrize("good", ["27AAPFU0939F1ZV", "29AAGCB7383J1Z4"])
def test_gstin_accepts_valid(good):
    assert gstin_mod.is_valid(good)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "27AAPFU0939F1Z",          # 14 chars
        "27AAPFU0939F1ZX",         # wrong check digit
        "27AAPFU0939F1AV",         # 14th char must be Z
        "9927AAPFU0939F1ZV",       # too long
    ],
)
def test_gstin_rejects_bad(bad):
    assert not gstin_mod.is_valid(bad)


def test_check_digit_round_trips():
    assert gstin_mod.check_digit("27AAPFU0939F1Z") == "V"
    assert gstin_mod.state_code("27AAPFU0939F1ZV") == "27"


# --- validation rules -------------------------------------------------------


def test_clean_sales_voucher_passes_everything():
    report = validate(sales_voucher(), ctx())
    assert report.ok, report.summary()
    assert not report.warnings
    assert not report.blocks_enqueue


def test_unknown_ledger_blocks_and_suggests():
    v = sales_voucher(
        lines=[
            VoucherLine(ledger_name="Acme Industires", amount=Decimal("11800.00")),
            VoucherLine(ledger_name="Sales - GST 18%", amount=Decimal("-11800.00")),
        ],
        gst=None,
    )
    report = validate(v, ctx())
    assert not report.ok
    failure = next(r for r in report.failures if r.rule == "ledgers_exist")
    assert "Acme Industries" in failure.details["missing"]["Acme Industires"]


def test_learned_alias_resolves_without_fuzzy_guessing():
    v = sales_voucher(
        lines=[
            VoucherLine(ledger_name="acme", amount=Decimal("100.00")),
            VoucherLine(ledger_name="Sales - GST 18%", amount=Decimal("-100.00")),
        ],
        gst=None,
    )
    report = validate(v, ctx(ledger_aliases={"acme": "Acme Industries"}))
    assert report.ok, report.summary()


def test_unbalanced_voucher_blocks_to_the_paisa():
    v = sales_voucher(
        lines=[
            VoucherLine(ledger_name="Acme Industries", amount=Decimal("11800.01")),
            VoucherLine(ledger_name="Sales - GST 18%", amount=Decimal("-11800.00")),
        ],
        gst=None,
    )
    report = validate(v, ctx())
    assert [r.rule for r in report.failures] == ["balanced"]


def test_interstate_supply_must_use_igst():
    v = sales_voucher(
        gst=GSTDetails(
            rate=Decimal("18"),
            taxable_value=Decimal("10000.00"),
            cgst=Decimal("900.00"),
            sgst=Decimal("900.00"),
            place_of_supply="29",
        )
    )
    report = validate(v, ctx())
    failure = next(r for r in report.failures if r.rule == "gst_split_correct")
    assert "must use IGST" in failure.message


def test_intrastate_supply_must_not_use_igst():
    v = sales_voucher(
        lines=[
            VoucherLine(ledger_name="Acme Industries", amount=Decimal("11800.00")),
            VoucherLine(ledger_name="Sales - GST 18%", amount=Decimal("-10000.00")),
            VoucherLine(ledger_name="Output IGST", amount=Decimal("-1800.00")),
        ],
        gst=GSTDetails(
            rate=Decimal("18"),
            taxable_value=Decimal("10000.00"),
            igst=Decimal("1800.00"),
            place_of_supply="27",
        ),
    )
    failure = next(
        r for r in validate(v, ctx()).failures if r.rule == "gst_split_correct"
    )
    assert "must use CGST+SGST" in failure.message


def test_tax_amount_must_match_the_rate():
    v = sales_voucher(
        gst=GSTDetails(
            rate=Decimal("18"),
            taxable_value=Decimal("10000.00"),
            cgst=Decimal("800.00"),
            sgst=Decimal("800.00"),
            place_of_supply="27",
        )
    )
    failure = next(
        r for r in validate(v, ctx()).failures if r.rule == "gst_split_correct"
    )
    assert "does not match" in failure.message


def test_unknown_place_of_supply_warns_but_does_not_block():
    v = sales_voucher(
        party_name="Walk-in Customer",
        gst=GSTDetails(
            rate=Decimal("18"),
            taxable_value=Decimal("10000.00"),
            cgst=Decimal("900.00"),
            sgst=Decimal("900.00"),
        ),
    )
    report = validate(v, ctx(known_ledgers=set(LEDGERS)))
    warning = next(r for r in report.warnings if r.rule == "gst_split_correct")
    assert warning.severity is Severity.WARNING
    assert report.ok


def test_bad_company_gstin_blocks():
    bad_company = COMPANY.model_copy(update={"gstin": "27AAPFU0939F1ZX"})
    report = validate(sales_voucher(), ctx(company=bad_company))
    assert any(r.rule == "gstin_valid" for r in report.failures)


def test_locked_period_blocks():
    report = validate(sales_voucher(date=date(2026, 3, 15)), ctx())
    failure = next(r for r in report.failures if r.rule == "period_open")
    assert "closed period" in failure.message


def test_date_outside_fy_blocks():
    report = validate(sales_voucher(date=date(2027, 6, 1)), ctx())
    failure = next(r for r in report.failures if r.rule == "period_open")
    assert "outside the financial year" in failure.message


def test_duplicate_detection_inside_window():
    posted = PostedVoucher(
        party_name="Acme Industries",
        reference="INV-001",
        amount=Decimal("11800.00"),
        date=date(2026, 6, 10),
        voucher_number="S/26-27/14",
    )
    report = validate(sales_voucher(), ctx(recent_vouchers=[posted]))
    failure = next(r for r in report.failures if r.rule == "not_duplicate")
    assert "S/26-27/14" in failure.message


def test_same_invoice_outside_window_is_not_a_duplicate():
    posted = PostedVoucher(
        party_name="Acme Industries",
        reference="INV-001",
        amount=Decimal("11800.00"),
        date=date(2026, 1, 10),
        voucher_number="S/25-26/99",
    )
    assert validate(sales_voucher(), ctx(recent_vouchers=[posted])).ok


def test_a_rule_that_raises_becomes_a_failure_not_a_skip():
    def exploding_rule(_v, _c):
        raise RuntimeError("boom")

    report = validate(sales_voucher(), ctx(), rules=[exploding_rule])
    assert not report.ok
    assert "raised RuntimeError" in report.failures[0].message


def test_override_requires_a_reason_and_unblocks_enqueue():
    report = validate(sales_voucher(date=date(2026, 3, 15)), ctx())
    assert report.blocks_enqueue
    with pytest.raises(ValueError, match="reason"):
        report.override("ca@firm.in", "   ")
    report.override("ca@firm.in", "client confirmed the period is reopened")
    assert not report.blocks_enqueue
    assert report.overridden_by == "ca@firm.in"


# --- idempotency ------------------------------------------------------------


def test_idempotency_key_is_stable_and_company_scoped():
    v = sales_voucher()
    assert idempotency.make_key("Demo", v) == idempotency.make_key("Demo", v)
    assert idempotency.make_key("Demo", v) != idempotency.make_key("Other", v)
    assert idempotency.make_key("Demo", v, salt="2") != idempotency.make_key("Demo", v)


def test_replay_of_a_succeeded_write_is_a_no_op():
    store = idempotency.InMemoryIdempotencyStore()
    key = idempotency.make_key("Demo", sales_voucher())

    first = idempotency.check(store, key, "Demo")
    assert first.should_execute
    idempotency.record_success(store, key, {"voucher_number": "S/1"})

    second = idempotency.check(store, key, "Demo")
    assert not second.should_execute
    assert second.existing.result == {"voucher_number": "S/1"}


def test_in_flight_write_blocks_a_concurrent_duplicate():
    store = idempotency.InMemoryIdempotencyStore()
    key = idempotency.make_key("Demo", sales_voucher())
    idempotency.check(store, key, "Demo")
    assert not idempotency.check(store, key, "Demo").should_execute


def test_failed_write_may_be_retried():
    store = idempotency.InMemoryIdempotencyStore()
    key = idempotency.make_key("Demo", sales_voucher())
    idempotency.check(store, key, "Demo")
    idempotency.record_failure(store, key, "Tally restarted")
    assert idempotency.check(store, key, "Demo").should_execute


# --- policy -----------------------------------------------------------------


def test_unknown_action_defaults_to_manual():
    assert Policy.default().requires_approval("create_sales_voucher", Decimal("1"))


def test_policy_loads_from_the_example_file(tmp_path):
    policy = Policy.load("config/policy.example.toml")
    assert policy.for_action("create_sales_voucher").mode is ApprovalMode.MANUAL
    receipt = policy.for_action("create_receipt")
    assert receipt.mode is ApprovalMode.AUTO_BELOW_AMOUNT
    assert receipt.max_amount == Decimal("5000.00")
    assert not policy.requires_approval("create_receipt", Decimal("4999"))
    assert policy.requires_approval("create_receipt", Decimal("5000"))
    assert policy.allow_validation_override


def test_auto_below_amount_without_a_limit_fails_closed():
    policy = Policy.from_dict(
        {"actions": {"create_receipt": {"mode": "auto_below_amount"}}}
    )
    assert policy.requires_approval("create_receipt", Decimal("1"))


# --- duplicates without an invoice reference --------------------------------


def _receipt(when: date, amount: str = "5000.00"):
    """A receipt, which in real life usually carries no invoice reference."""
    return Voucher(
        voucher_type=VoucherType.RECEIPT,
        date=when,
        party_name="Acme Industries",
        lines=[
            VoucherLine(ledger_name="Bank - HDFC 1234", amount=Decimal(amount)),
            VoucherLine(ledger_name="Acme Industries", amount=-Decimal(amount)),
        ],
    )


def test_an_unreferenced_receipt_can_still_be_a_duplicate():
    """Found live: matching on reference alone let the same payment post twice,
    because receipts and payments usually have no reference at all."""
    posted = PostedVoucher(
        party_name="Acme Industries",
        reference="",
        amount=Decimal("5000.00"),
        date=date(2026, 6, 1),
        voucher_number="R/1",
        voucher_type="Receipt",
    )
    report = validate(_receipt(date(2026, 6, 1)), ctx(recent_vouchers=[posted]))
    failure = next(r for r in report.failures if r.rule == "not_duplicate")
    assert "R/1" in failure.message
    assert "neither carries an invoice reference" in failure.message


def test_an_unreferenced_receipt_a_week_later_is_not_a_duplicate():
    """The fallback window is deliberately tight: a second payment of the same
    amount to the same party is normal, just not on the same day."""
    posted = PostedVoucher(
        party_name="Acme Industries",
        reference="",
        amount=Decimal("5000.00"),
        date=date(2026, 6, 1),
        voucher_number="R/1",
        voucher_type="Receipt",
    )
    assert validate(_receipt(date(2026, 6, 8)), ctx(recent_vouchers=[posted])).ok


def test_a_different_voucher_type_is_not_a_duplicate():
    posted = PostedVoucher(
        party_name="Acme Industries",
        reference="",
        amount=Decimal("5000.00"),
        date=date(2026, 6, 1),
        voucher_number="P/1",
        voucher_type="Payment",
    )
    assert validate(_receipt(date(2026, 6, 1)), ctx(recent_vouchers=[posted])).ok


def test_a_referenced_voucher_still_matches_on_its_reference():
    """The tight window must not weaken the referenced case, which matches
    across a month."""
    posted = PostedVoucher(
        party_name="Acme Industries",
        reference="INV-001",
        amount=Decimal("11800.00"),
        date=date(2026, 6, 1),
        voucher_number="S/1",
        voucher_type="Sales",
    )
    report = validate(sales_voucher(date=date(2026, 6, 20)), ctx(recent_vouchers=[posted]))
    assert any(r.rule == "not_duplicate" for r in report.failures)
