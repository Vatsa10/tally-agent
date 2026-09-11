"""Stage 3: vouchers, masters, reports, reconciliation, ingest, registry."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tallyagent_core.models import Company, Voucher, VoucherLine, VoucherType
from tallyagent_core.policy import Policy
from tallyagent_tools import ingest, masters, reconcile, registry, reports, vouchers
from tallyagent_tools.base import PendingAction, ToolContext

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def ctx(backend, company) -> ToolContext:
    return ToolContext(backend=backend, company=company, policy=Policy.default())


@pytest.fixture
def queue() -> list[PendingAction]:
    return []


@pytest.fixture
def queued_ctx(backend, company, queue) -> ToolContext:
    async def enqueue(action: PendingAction) -> str:
        queue.append(action)
        return f"APR-{len(queue):04d}"

    return ToolContext(
        backend=backend, company=company, policy=Policy.default(), enqueue=enqueue
    )


# --- voucher tools ----------------------------------------------------------


async def test_sales_voucher_is_validated_and_queued_not_posted(
    queued_ctx, queue, fake_tally
):
    result = await vouchers.create_sales_voucher(
        queued_ctx,
        party_name="Acme Industries",
        taxable_value="10000.00",
        gst_rate="18",
        voucher_date=date(2026, 6, 15),
        reference="INV-001",
    )
    assert result.ok
    assert "Queued for approval as APR-0001" in result.message
    assert fake_tally.vouchers == [], "nothing may reach Tally before approval"

    action = queue[0]
    assert action.action_type == "create_sales_voucher"
    assert action.voucher.amount == Decimal("11800.00")
    assert action.validation.ok
    assert "<VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>" in action.raw_xml

    impact = action.ledger_impact()
    assert {"ledger": "Acme Industries", "debit": "11800.00", "credit": ""} in impact
    assert {"ledger": "Output CGST", "debit": "", "credit": "900.00"} in impact


async def test_intrastate_sale_splits_cgst_sgst(queued_ctx, queue):
    await vouchers.create_sales_voucher(
        queued_ctx, "Acme Industries", "10000.00", "18", date(2026, 6, 15)
    )
    gst = queue[0].voucher.gst
    assert (gst.cgst, gst.sgst, gst.igst) == (
        Decimal("900.00"),
        Decimal("900.00"),
        Decimal("0.00"),
    )


async def test_interstate_purchase_uses_igst_from_the_party_state(queued_ctx, queue):
    # Bharat Supplies is in Karnataka (29); the company is Maharashtra (27).
    await vouchers.create_purchase_voucher(
        queued_ctx, "Bharat Supplies", "5000.00", "18", date(2026, 6, 5)
    )
    voucher = queue[0].voucher
    assert voucher.gst.igst == Decimal("900.00")
    assert voucher.gst.cgst == Decimal("0.00")
    assert [ln.ledger_name for ln in voucher.lines] == [
        "Bharat Supplies",
        "Purchase - GST 18%",
        "Input IGST",
    ]
    # Purchase mirrors a sale: supplier credited, purchases debited.
    assert voucher.lines[0].amount == Decimal("-5900.00")
    assert voucher.lines[1].amount == Decimal("5000.00")


async def test_odd_tax_halves_still_balance_to_the_paisa(queued_ctx, queue):
    await vouchers.create_sales_voucher(
        queued_ctx, "Acme Industries", "1000.05", "18", date(2026, 6, 15)
    )
    voucher = queue[0].voucher
    assert voucher.is_balanced
    assert voucher.gst.cgst + voucher.gst.sgst == Decimal("180.01")


async def test_validation_failure_blocks_the_queue(queued_ctx, queue):
    result = await vouchers.create_sales_voucher(
        queued_ctx,
        party_name="Nobody Ltd",
        taxable_value="1000.00",
        voucher_date=date(2026, 6, 15),
    )
    assert not result.ok
    assert "Not queued" in result.message
    assert queue == []
    assert any(r.rule == "ledgers_exist" for r in result.validation.failures)


async def test_locked_period_blocks_the_queue(queued_ctx, queue):
    result = await vouchers.create_sales_voucher(
        queued_ctx, "Acme Industries", "1000.00", "18", date(2026, 3, 15)
    )
    assert not result.ok
    assert queue == []


async def test_policy_auto_approve_posts_directly(backend, company, fake_tally):
    policy = Policy.from_dict(
        {"actions": {"create_receipt": {"mode": "auto_below_amount", "max_amount": 5000}}}
    )
    ctx = ToolContext(backend=backend, company=company, policy=policy)
    result = await vouchers.create_receipt(
        ctx, "Acme Industries", "1000.00", voucher_date=date(2026, 6, 20)
    )
    assert result.ok
    assert result.write.ok
    assert "automatically under policy" in result.message
    assert len(fake_tally.vouchers) == 1


async def test_policy_limit_is_respected(backend, company, fake_tally):
    policy = Policy.from_dict(
        {"actions": {"create_receipt": {"mode": "auto_below_amount", "max_amount": 5000}}}
    )
    ctx = ToolContext(backend=backend, company=company, policy=policy)
    result = await vouchers.create_receipt(
        ctx, "Acme Industries", "9000.00", voucher_date=date(2026, 6, 20)
    )
    assert result.pending is not None
    assert fake_tally.vouchers == []


async def test_unbalanced_journal_is_refused(queued_ctx, queue):
    result = await vouchers.create_journal(
        queued_ctx,
        lines=[
            {"ledger": "Cash", "amount": "100"},
            {"ledger": "Bank - HDFC 1234", "amount": "-90"},
        ],
        voucher_date=date(2026, 6, 15),
    )
    assert not result.ok
    assert any(r.rule == "balanced" for r in result.validation.failures)
    assert queue == []


async def test_duplicate_invoice_is_caught_against_posted_vouchers(
    queued_ctx, queue, backend, sales_voucher
):
    from tallyagent_core.idempotency import make_key

    await backend.create_voucher(sales_voucher, make_key("Demo", sales_voucher))
    result = await vouchers.create_sales_voucher(
        queued_ctx,
        "Acme Industries",
        "10000.00",
        "18",
        date(2026, 6, 16),
        reference="INV-001",
    )
    assert not result.ok
    assert any(r.rule == "not_duplicate" for r in result.validation.failures)


# --- master tools -----------------------------------------------------------


async def test_resolve_ledger_alias_exact_alias_and_suggestion(ctx):
    exact = await masters.resolve_ledger_alias(ctx, "Acme Industries")
    assert exact.data["how"] == "exact"

    ctx.ledger_aliases["acme"] = "Acme Industries"
    aliased = await masters.resolve_ledger_alias(ctx, "acme")
    assert aliased.data == {"resolved": "Acme Industries", "how": "alias"}

    fuzzy = await masters.resolve_ledger_alias(ctx, "Acme Industires")
    assert fuzzy.data["resolved"] is None, "a near match must never be applied silently"
    assert "Acme Industries" in fuzzy.data["suggestions"]


async def test_create_ledger_is_queued_and_then_executes(queued_ctx, queue, fake_tally):
    result = await masters.create_ledger(queued_ctx, "Rent", "Indirect Expenses")
    assert "Queued for approval" in result.message
    assert "Rent" not in fake_tally.ledgers

    write = await masters._execute_master(queued_ctx, queue[0])
    assert write.ok
    assert "Rent" in fake_tally.ledgers


async def test_create_party_rejects_a_bad_gstin(ctx):
    result = await masters.create_party(ctx, "Zed Ltd", gstin="27AAPFU0939F1ZX")
    assert "not a valid GSTIN" in result.message


async def test_creating_an_existing_ledger_is_a_no_op(ctx):
    result = await masters.create_ledger(ctx, "Cash", "Cash-in-Hand")
    assert "already exists" in result.message


async def test_list_ledgers_filters_by_group(ctx):
    result = await masters.list_ledgers(ctx, group="Duties & Taxes")
    assert {row["name"] for row in result.data} == {
        "Output CGST", "Output SGST", "Output IGST",
        "Input CGST", "Input SGST", "Input IGST",
    }


# --- reports ----------------------------------------------------------------


async def test_reports_reflect_posted_vouchers(ctx, backend, sales_voucher):
    from tallyagent_core.idempotency import make_key

    await backend.create_voucher(sales_voucher, make_key("Demo", sales_voucher))

    tb = await reports.trial_balance(ctx)
    rows = {row["ledger"]: row["balance"] for row in tb.data["rows"]}
    assert rows["Acme Industries"] == "11800.00"

    cash = await reports.cash_position(ctx)
    assert cash.data["accounts"]["Bank - HDFC 1234"] == "250000.00"
    assert cash.data["total"] == "265000.00"

    receivables = await reports.outstanding_receivables(ctx, as_on=date(2026, 8, 1))
    assert receivables.data["by_party"]["Acme Industries"] == "11800.00"
    assert receivables.data["ageing"]["31-60"] == "11800.00"

    top = await reports.top_debtors(ctx)
    assert top.data[0]["party"] == "Acme Industries"

    book = await reports.day_book(ctx, date(2026, 6, 1), date(2026, 6, 30))
    assert len(book.data) == 4
    assert {row["type"] for row in book.data} == {"Sales"}

    party = await reports.party_transactions(ctx, "Acme Industries")
    assert party.data and all(
        row["party"] == "Acme Industries" or row["ledger"] == "Acme Industries"
        for row in party.data
    )


async def test_gstr1_folds_lines_into_one_row_per_voucher(ctx, backend, sales_voucher):
    from tallyagent_core.idempotency import make_key

    await backend.create_voucher(sales_voucher, make_key("Demo", sales_voucher))
    result = await reports.gstr1_data(ctx, date(2026, 6, 1), date(2026, 6, 30))
    assert len(result.data) == 1
    row = result.data[0]
    assert row["taxable_value"] == "10000.00"
    assert row["cgst"] == "900.00"
    assert row["sgst"] == "900.00"
    assert row["total"] == "11800.00"


# --- bank reconciliation ----------------------------------------------------


async def _post_june_bank_activity(ctx, backend):
    """Two bank movements the statement fixture should match."""
    from tallyagent_core.idempotency import make_key

    receipt = Voucher(
        voucher_type=VoucherType.RECEIPT,
        date=date(2026, 6, 15),
        party_name="Acme Industries",
        narration="Acme Industries NEFT",
        lines=[
            VoucherLine(ledger_name="Bank - HDFC 1234", amount=Decimal("11800.00")),
            VoucherLine(ledger_name="Acme Industries", amount=Decimal("-11800.00")),
        ],
    )
    payment = Voucher(
        voucher_type=VoucherType.PAYMENT,
        date=date(2026, 6, 21),
        party_name="Bharat Supplies",
        narration="Bharat Supplies RTGS",
        lines=[
            VoucherLine(ledger_name="Bharat Supplies", amount=Decimal("5900.00")),
            VoucherLine(ledger_name="Bank - HDFC 1234", amount=Decimal("-5900.00")),
        ],
    )
    for voucher in (receipt, payment):
        await backend.create_voucher(voucher, make_key("Demo", voucher))


async def test_bank_reco_classifies_and_proposes_without_posting(ctx, backend, fake_tally):
    await _post_june_bank_activity(ctx, backend)
    rows = ingest.parse_bank_statement_csv(
        (FIXTURES / "bank_statement.csv").read_text()
    )
    assert len(rows) == 4

    before = len(fake_tally.vouchers)
    result = await reconcile.bank_reco(ctx, rows, "Bank - HDFC 1234")
    assert len(fake_tally.vouchers) == before, "reconciliation must never post"

    data = result.data
    matched = {row["amount"] for row in data["matched"]}
    assert matched == {"11800.00", "-5900.00"}

    unmatched = {row["amount"] for row in data["unmatched_statement"]}
    assert unmatched == {"-118.00", "50000.00"}

    proposals = {p["amount"]: p for p in data["proposals"]}
    assert proposals["50000.00"]["tool"] == "create_receipt"
    assert proposals["50000.00"]["suggested_party"] == "Acme Industries"
    assert proposals["118.00"]["tool"] == "create_payment"


async def test_bank_reco_matches_within_the_date_window_only(ctx, backend):
    await _post_june_bank_activity(ctx, backend)
    far = [{"date": "2026-06-30", "narration": "NEFT ACME", "amount": "11800.00"}]
    result = await reconcile.bank_reco(ctx, far, "Bank - HDFC 1234", date_window_days=2)
    assert result.data["matched"] == []
    assert len(result.data["unmatched_statement"]) == 1


async def test_bank_reco_never_fuzzy_matches_amounts(ctx, backend):
    await _post_june_bank_activity(ctx, backend)
    close = [{"date": "2026-06-15", "narration": "NEFT ACME", "amount": "11800.01"}]
    result = await reconcile.bank_reco(ctx, close, "Bank - HDFC 1234")
    assert result.data["matched"] == []


# --- GSTR-2B reconciliation -------------------------------------------------


async def test_gstr2b_classification(ctx, backend):
    from tallyagent_core.idempotency import make_key

    # Books: BS/2026/41 matching, BS/2026/42 at the wrong value, BS/2026/99 not in 2B.
    # BS/2026/43 exists only in 2B.
    booked = [
        ("BS/2026/41", Decimal("5000.00"), Decimal("900.00")),
        ("BS/2026/42", Decimal("9000.00"), Decimal("1620.00")),
        ("BS/2026/99", Decimal("1000.00"), Decimal("180.00")),
    ]
    for reference, taxable, igst in booked:
        voucher = Voucher(
            voucher_type=VoucherType.PURCHASE,
            date=date(2026, 6, 10),
            party_name="Bharat Supplies",
            reference=reference,
            lines=[
                VoucherLine(ledger_name="Bharat Supplies", amount=-(taxable + igst)),
                VoucherLine(ledger_name="Purchase - GST 18%", amount=taxable),
                VoucherLine(ledger_name="Input IGST", amount=igst),
            ],
        )
        await backend.create_voucher(voucher, make_key("Demo", voucher))

    gstr2b = json.loads((FIXTURES / "gstr2b.json").read_text())
    result = await reconcile.gstr2b_vs_purchase_register(
        ctx, gstr2b, date(2026, 6, 1), date(2026, 6, 30)
    )

    by_invoice = {row["invoice_no"]: row for row in result.data["rows"]}
    assert by_invoice["BS/2026/41"]["status"] == "matched"
    assert by_invoice["BS/2026/42"]["status"] == "value_mismatch"
    assert by_invoice["BS/2026/42"]["difference"] == "1180.00"
    assert by_invoice["BS/2026/43"]["status"] == "missing_in_books"
    assert by_invoice["BS/2026/99"]["status"] == "missing_in_2b"

    assert result.data["counts"] == {
        "matched": 1,
        "value_mismatch": 1,
        "missing_in_books": 1,
        "missing_in_2b": 1,
    }
    csv_text = result.data["csv"]
    assert csv_text.startswith("status,supplier,")
    assert csv_text.count("\n") == 5  # header + 4 rows


def test_invoice_number_normalisation_absorbs_the_usual_noise():
    assert reconcile._normalise_invoice_no(" bs-2026/41 ") == "BS2026/41"
    assert reconcile._normalise_invoice_no("0041") == "41"


# --- ingest -----------------------------------------------------------------


def test_invoice_extraction_parses_typed_fields():
    extract = ingest.parse_invoice_json(
        (FIXTURES / "invoice_extraction.json").read_text()
    )
    assert extract.vendor == "Bharat Supplies"
    assert extract.invoice_date == date(2026, 6, 5)
    assert extract.taxable_value == Decimal("5000.00")
    assert extract.gst_rate == Decimal("18")
    assert extract.place_of_supply == "29"
    assert extract.warnings == []


def test_invoice_extraction_survives_a_fenced_chatty_reply():
    extract = ingest.parse_invoice_json(
        'Here you go:\n```json\n{"vendor": "X Ltd", "taxable_value": "1,000.00", '
        '"igst": 180, "total": 1180, "invoice_no": "A1"}\n```\nHope that helps!'
    )
    assert extract.vendor == "X Ltd"
    assert extract.taxable_value == Decimal("1000.00")
    assert extract.gst_rate == Decimal("18")


def test_invoice_extraction_flags_a_bad_gstin_and_a_wrong_total():
    extract = ingest.parse_invoice_json(
        {"vendor": "X", "gstin": "27AAPFU0939F1ZX", "taxable_value": 100,
         "igst": 18, "total": 200, "invoice_no": "A1"}
    )
    assert any("checksum" in w for w in extract.warnings)
    assert any("does not equal" in w for w in extract.warnings)


def test_unparseable_extraction_warns_instead_of_raising():
    extract = ingest.parse_invoice_json("the invoice is unreadable")
    assert extract.warnings and "could not parse" in extract.warnings[0]


async def test_invoice_to_draft_queues_a_purchase(queued_ctx, queue, fake_tally):
    result = await ingest.invoice_image_to_draft(
        queued_ctx, (FIXTURES / "invoice_extraction.json").read_text()
    )
    assert "Queued for approval" in result.message
    assert fake_tally.vouchers == []
    voucher = queue[0].voucher
    assert voucher.voucher_type.value == "Purchase"
    assert voucher.party_name == "Bharat Supplies"
    assert voucher.amount == Decimal("5900.00")
    assert voucher.gst.igst == Decimal("900.00")


async def test_invoice_from_an_unknown_vendor_stops_with_suggestions(queued_ctx, queue):
    result = await ingest.invoice_image_to_draft(
        queued_ctx,
        {"vendor": "Bharat Supplys", "taxable_value": 100, "igst": 18,
         "total": 118, "invoice_no": "A1", "invoice_date": "2026-06-05"},
    )
    assert "is not a ledger" in result.message
    assert "Bharat Supplies" in result.data["suggestions"]
    assert queue == []


def test_bank_statement_text_parsing():
    rows = ingest.parse_bank_statement_text(
        "15-06-2026  NEFT ACME INDS        11,800.00 CR\n"
        "20-06-2026  RTGS BHARAT            5,900.00 DR\n"
        "garbage line that should be skipped\n"
    )
    assert [r["amount"] for r in rows] == ["11800.00", "-5900.00"]


async def test_bank_statement_format_autodetection(ctx):
    csv_result = await ingest.bank_statement_to_rows(
        ctx, (FIXTURES / "bank_statement.csv").read_text()
    )
    assert len(csv_result.data) == 4
    text_result = await ingest.bank_statement_to_rows(
        ctx, "15-06-2026  NEFT ACME  11,800.00 CR\n"
    )
    assert len(text_result.data) == 1


# --- registry ---------------------------------------------------------------


def test_registry_exposes_every_tool_with_a_schema():
    assert len(registry.TOOLS) >= 20
    for tool in registry.TOOLS:
        schema = tool.schema()
        assert schema["description"], f"{tool.name} has no description"
        assert schema["input_schema"]["type"] == "object"
        assert tool.openai_schema()["function"]["name"] == tool.name


def test_read_and_write_tools_are_separable():
    reads = {s["name"] for s in registry.schemas(mutating=False)}
    writes = {s["name"] for s in registry.schemas(mutating=True)}
    assert "trial_balance" in reads
    assert "create_sales_voucher" in writes
    assert not reads & writes


def test_unknown_tool_names_fail_helpfully():
    with pytest.raises(KeyError, match="Available:"):
        registry.get("drop_all_vouchers")


async def test_registry_call_coerces_iso_dates(queued_ctx, queue):
    result = await registry.call(
        queued_ctx,
        "create_sales_voucher",
        {
            "party_name": "Acme Industries",
            "taxable_value": "10000.00",
            "gst_rate": "18",
            "voucher_date": "2026-06-15",
            "reference": "INV-777",
        },
    )
    assert result.ok
    assert queue[0].voucher.date == date(2026, 6, 15)


async def test_registry_call_drops_empty_optional_dates(ctx):
    result = await registry.call(ctx, "day_book", {"from_date": "", "to_date": ""})
    assert result.ok


async def test_tool_context_caches_masters(ctx, fake_tally):
    await ctx.masters()
    exports = len(fake_tally.requests)
    await ctx.masters()
    assert len(fake_tally.requests) == exports, "masters must be cached per turn"
    await ctx.masters(refresh=True)
    assert len(fake_tally.requests) > exports


async def test_company_without_a_period_still_validates(backend):
    ctx = ToolContext(
        backend=backend, company=Company(name="No Period Co", state_code="27")
    )
    result = await vouchers.create_receipt(
        ctx, "Acme Industries", "100.00", voucher_date=date(2026, 6, 20)
    )
    # period_open degrades to a warning, so the draft still reaches a human.
    assert result.validation.ok
    assert any(r.rule == "period_open" for r in result.validation.warnings)
