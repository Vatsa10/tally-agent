"""Stage 15.2: stock — masters, movements, validation and reports."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tallyagent_approvals.audit import AuditLog
from tallyagent_approvals.db import make_engine
from tallyagent_approvals.queue import ApprovalQueue
from tallyagent_core.models import (
    GSTDetails,
    InventoryLine,
    StockItem,
    Voucher,
    VoucherLine,
    VoucherType,
)
from tallyagent_core.policy import Policy
from tallyagent_core.validation import ValidationContext, validate
from tallyagent_tally.xml import builders, parsers
from tallyagent_tally.xml.quirks import tally_quantity, tally_rate
from tallyagent_tools import stock, vouchers
from tallyagent_tools.base import ToolContext
from tallyagent_tools.executor import build_executor

ITEMS = [
    {"name": "Widget", "group": "Finished Goods", "unit": "Pcs",
     "hsn": "8471", "gst_rate": "18", "opening_quantity": "10", "opening_rate": "2000"},
    {"name": "Gadget", "group": "Finished Goods", "unit": "Pcs", "hsn": "8472"},
]


@pytest.fixture
def engine():
    return make_engine(":memory:")


@pytest.fixture
def queue(engine):
    return ApprovalQueue(engine, AuditLog(engine))


@pytest.fixture
def ctx(queue, backend, company):
    tools = ToolContext(
        backend=backend, company=company, policy=Policy.default(), enqueue=queue.enqueue
    )
    queue.executor = build_executor(tools)
    return tools


async def seed_items(ctx, queue) -> None:
    result = await stock.create_stock_masters(
        ctx, items=ITEMS, units=["Pcs"], groups=["Finished Goods"],
        godowns=["Main Location"],
    )
    await queue.approve(result.data["ticket"], "test")
    ctx._stock_cache = None


# --- model ------------------------------------------------------------------


def test_amount_is_derived_from_quantity_and_rate():
    line = InventoryLine(stock_item="Widget", quantity=Decimal("-20"), rate=Decimal("2500"))
    assert line.amount == Decimal("50000.00")
    assert not line.is_inward
    assert not line.overrides_value


def test_an_overridden_amount_is_flagged():
    line = InventoryLine(
        stock_item="Widget",
        quantity=Decimal("-20"),
        rate=Decimal("2500"),
        amount_override=Decimal("49999"),
    )
    assert line.amount == Decimal("49999.00")
    assert line.overrides_value, "a stated total that disagrees must be visible"


def test_stock_changes_the_voucher_fingerprint():
    """Otherwise an amended quantity looks like the same voucher to duplicate
    detection, and the correction is refused."""
    base = Voucher(
        voucher_type=VoucherType.SALES,
        date=date(2026, 6, 1),
        lines=[
            VoucherLine(ledger_name="Acme Industries", amount=Decimal("100")),
            VoucherLine(ledger_name="Sales - GST 18%", amount=Decimal("-100")),
        ],
        inventory=[
            InventoryLine(stock_item="Widget", quantity=Decimal("-1"), rate=Decimal("100"))
        ],
    )
    changed = base.model_copy(
        update={
            "inventory": [
                InventoryLine(
                    stock_item="Widget", quantity=Decimal("-2"), rate=Decimal("50")
                )
            ]
        }
    )
    assert base.fingerprint() != changed.fingerprint()


def test_a_voucher_without_stock_is_unchanged():
    """Inventory must not alter what an accounting-only company posts."""
    voucher = Voucher(
        voucher_type=VoucherType.JOURNAL,
        date=date(2026, 6, 1),
        lines=[
            VoucherLine(ledger_name="Cash", amount=Decimal("100")),
            VoucherLine(ledger_name="Round Off", amount=Decimal("-100")),
        ],
    )
    assert not voucher.has_inventory
    assert voucher.inventory_value == Decimal("0")
    element = builders.build_voucher_element(voucher)
    assert element.find("INVENTORYALLOCATIONS.LIST") is None


def test_a_stock_item_rejects_an_unnotified_gst_rate():
    with pytest.raises(ValueError, match="not a notified rate"):
        StockItem(name="Widget", gst_rate=Decimal("17"))


# --- XML --------------------------------------------------------------------


def test_rate_and_quantity_carry_their_unit():
    assert tally_rate(Decimal("2500.00"), "Pcs") == "2500.00/Pcs"
    assert tally_rate(Decimal("2500.00"), "") == "2500.00"
    assert tally_quantity(Decimal("-20"), "Pcs") == "20 Pcs"


def test_outward_stock_is_positive_in_tallys_convention():
    """The mirror of a ledger line, and the same trap."""
    voucher = Voucher(
        voucher_type=VoucherType.SALES,
        date=date(2026, 6, 1),
        lines=[
            VoucherLine(ledger_name="Acme Industries", amount=Decimal("50000")),
            VoucherLine(ledger_name="Sales - GST 18%", amount=Decimal("-50000")),
        ],
        inventory=[
            InventoryLine(
                stock_item="Widget", quantity=Decimal("-20"), rate=Decimal("2500"),
                unit="Pcs", godown="Main Location",
            )
        ],
    )
    entry = builders.build_voucher_element(voucher).find(".//INVENTORYALLOCATIONS.LIST")
    assert entry.findtext("STOCKITEMNAME") == "Widget"
    assert entry.findtext("ISDEEMEDPOSITIVE") == "No"
    assert entry.findtext("AMOUNT") == "50000.00"
    assert entry.findtext("ACTUALQTY") == "20 Pcs"
    assert entry.find("BATCHALLOCATIONS.LIST").findtext("GODOWNNAME") == "Main Location"


def test_inward_stock_is_negative_in_tallys_convention():
    voucher = Voucher(
        voucher_type=VoucherType.PURCHASE,
        date=date(2026, 6, 1),
        lines=[
            VoucherLine(ledger_name="Bharat Supplies", amount=Decimal("-50000")),
            VoucherLine(ledger_name="Purchase - GST 18%", amount=Decimal("50000")),
        ],
        inventory=[
            InventoryLine(
                stock_item="Widget", quantity=Decimal("20"), rate=Decimal("2500"), unit="Pcs"
            )
        ],
    )
    entry = builders.build_voucher_element(voucher).find(".//INVENTORYALLOCATIONS.LIST")
    assert entry.findtext("ISDEEMEDPOSITIVE") == "Yes"
    assert entry.findtext("AMOUNT") == "-50000.00"


def test_stock_rows_are_parsed_out_of_a_voucher_collection():
    xml = (
        b"<ENVELOPE><BODY><DATA><COLLECTION>"
        b'<VOUCHER VCHTYPE="Sales"><DATE>20260601</DATE>'
        b"<VOUCHERTYPENAME>Sales</VOUCHERTYPENAME><VOUCHERNUMBER>1</VOUCHERNUMBER>"
        b"<ALLINVENTORYENTRIES.LIST><STOCKITEMNAME>Widget</STOCKITEMNAME>"
        b"<ACTUALQTY>20 Pcs</ACTUALQTY><RATE>2500/Pcs</RATE>"
        b"<AMOUNT>50000.00</AMOUNT>"
        b"<BATCHALLOCATIONS.LIST><GODOWNNAME>Main Location</GODOWNNAME>"
        b"</BATCHALLOCATIONS.LIST></ALLINVENTORYENTRIES.LIST>"
        b"</VOUCHER></COLLECTION></DATA></BODY></ENVELOPE>"
    )
    rows = parsers.parse_stock_rows(xml)
    assert len(rows) == 1
    assert rows[0]["STOCKITEMNAME"] == "Widget"
    assert rows[0]["GODOWNNAME"] == "Main Location"
    assert rows[0]["VOUCHERTYPENAME"] == "Sales"


def test_the_cmpinfo_counter_is_not_mistaken_for_a_voucher():
    xml = (
        b"<ENVELOPE><BODY><DESC><CMPINFO><VOUCHER>3</VOUCHER></CMPINFO></DESC>"
        b"<DATA><COLLECTION></COLLECTION></DATA></BODY></ENVELOPE>"
    )
    assert parsers.parse_stock_rows(xml) == []


# --- validation -------------------------------------------------------------


def _sale(company, quantity: str, rate: str, taxable: str) -> tuple:
    taxable_value = Decimal(taxable)
    tax = (taxable_value * Decimal("0.18")).quantize(Decimal("0.01"))
    half = (tax / 2).quantize(Decimal("0.01"))
    voucher = Voucher(
        voucher_type=VoucherType.SALES,
        date=date(2026, 6, 1),
        party_name="Acme Industries",
        lines=[
            VoucherLine(ledger_name="Acme Industries", amount=taxable_value + tax),
            VoucherLine(ledger_name="Sales - GST 18%", amount=-taxable_value),
            VoucherLine(ledger_name="Output CGST", amount=-half),
            VoucherLine(ledger_name="Output SGST", amount=-(tax - half)),
        ],
        gst=GSTDetails(
            rate=Decimal("18"), taxable_value=taxable_value,
            cgst=half, sgst=tax - half,
            place_of_supply="27",
        ),
        inventory=[
            InventoryLine(
                stock_item="Widget", quantity=Decimal(quantity), rate=Decimal(rate)
            )
        ],
    )
    ctx = ValidationContext(
        company=company,
        known_ledgers={
            "Acme Industries", "Sales - GST 18%", "Output CGST", "Output SGST",
        },
        known_stock_items={"Widget"},
        stock_on_hand={"Widget": Decimal("100")},
    )
    return voucher, ctx


def test_a_consistent_stock_voucher_passes(company):
    voucher, ctx = _sale(company, "-20", "2500", "50000")
    report = validate(voucher, ctx)
    assert report.ok, report.summary()


def test_stock_value_must_match_what_is_billed(company):
    """The classic trading error: 20 boxes leave, the invoice bills for 200."""
    voucher, ctx = _sale(company, "-20", "2500", "500000")
    failure = next(
        r for r in validate(voucher, ctx).failures if r.rule == "inventory_matches_value"
    )
    assert "50000.00" in failure.message and "500000.00" in failure.message


def test_an_unknown_stock_item_blocks_and_suggests(company):
    voucher, ctx = _sale(company, "-20", "2500", "50000")
    ctx.known_stock_items = {"Widgets"}
    failure = next(
        r for r in validate(voucher, ctx).failures if r.rule == "stock_items_exist"
    )
    assert "Widgets" in failure.details["missing"]["Widget"]


def test_negative_stock_warns_by_default(company):
    voucher, ctx = _sale(company, "-200", "2500", "500000")
    ctx.stock_on_hand = {"Widget": Decimal("5")}
    warning = next(
        r for r in validate(voucher, ctx).warnings if r.rule == "stock_not_negative"
    )
    assert "takes stock negative" in warning.message
    assert "purchase" in warning.message


def test_negative_stock_can_be_made_an_error(company):
    voucher, ctx = _sale(company, "-200", "2500", "500000")
    ctx.stock_on_hand = {"Widget": Decimal("5")}
    ctx.allow_negative_stock = False
    assert any(r.rule == "stock_not_negative" for r in validate(voucher, ctx).failures)


def test_the_stock_rules_are_inert_without_inventory(company):
    voucher = Voucher(
        voucher_type=VoucherType.JOURNAL,
        date=date(2026, 6, 1),
        lines=[
            VoucherLine(ledger_name="Cash", amount=Decimal("100")),
            VoucherLine(ledger_name="Round Off", amount=Decimal("-100")),
        ],
    )
    ctx = ValidationContext(company=company, known_ledgers={"Cash", "Round Off"})
    report = validate(voucher, ctx)
    assert report.ok, report.summary()


# --- masters and movements --------------------------------------------------


async def test_stock_masters_are_created_as_one_approval(ctx, queue, fake_tally):
    result = await stock.create_stock_masters(
        ctx, items=ITEMS, units=["Pcs"], groups=["Finished Goods"]
    )
    assert "Queued as APR-0001" in result.message
    assert result.data["items"] == 2
    assert fake_tally.stock_items == {}, "nothing before approval"

    write = await queue.approve("APR-0001", "test")
    assert write.ok, write.errors
    assert set(fake_tally.stock_items) == {"Widget", "Gadget"}
    assert fake_tally.stock_items["Widget"].opening_quantity == Decimal("10")


async def test_seeding_the_same_masters_twice_is_a_no_op(ctx, queue):
    await seed_items(ctx, queue)
    again = await stock.create_stock_masters(ctx, items=ITEMS)
    assert "already exist" in again.message


async def test_a_sale_moves_stock_and_derives_the_value(ctx, queue, fake_tally):
    await seed_items(ctx, queue)
    result = await vouchers.create_sales_voucher(
        ctx,
        party_name="Acme Industries",
        taxable_value="0",
        gst_rate="18",
        voucher_date=date(2026, 6, 15),
        reference="INV-S1",
        items=[{"stock_item": "Widget", "quantity": "20", "rate": "2500", "unit": "Pcs"}],
    )
    assert result.ok, result.message
    voucher = result.pending.voucher
    # Quantity is signed by the tool, from the voucher type.
    assert voucher.inventory[0].quantity == Decimal("-20")
    assert voucher.gst.taxable_value == Decimal("50000.00")
    assert voucher.amount == Decimal("59000.00")

    await queue.approve(result.data["ticket"], "test")
    assert fake_tally.vouchers[0].stock[0].quantity == Decimal("-20")


async def test_a_purchase_brings_stock_in(ctx, queue, fake_tally):
    await seed_items(ctx, queue)
    result = await vouchers.create_purchase_voucher(
        ctx,
        party_name="Bharat Supplies",
        taxable_value="0",
        voucher_date=date(2026, 6, 10),
        reference="BILL-1",
        items=[{"stock_item": "Widget", "quantity": "30", "rate": "2000", "unit": "Pcs"}],
    )
    assert result.pending.voucher.inventory[0].quantity == Decimal("30")
    await queue.approve(result.data["ticket"], "test")
    assert fake_tally.vouchers[0].stock[0].quantity == Decimal("30")


async def test_a_sale_of_an_unknown_item_never_reaches_tally(ctx, queue, fake_tally):
    await seed_items(ctx, queue)
    result = await vouchers.create_sales_voucher(
        ctx,
        party_name="Acme Industries",
        taxable_value="0",
        voucher_date=date(2026, 6, 15),
        items=[{"stock_item": "Doohickey", "quantity": "1", "rate": "10"}],
    )
    assert not result.ok
    assert any(r.rule == "stock_items_exist" for r in result.validation.failures)
    assert fake_tally.vouchers == []


# --- reports ----------------------------------------------------------------


async def test_stock_summary_is_opening_plus_movements(ctx, queue, fake_tally):
    await seed_items(ctx, queue)

    purchase = await vouchers.create_purchase_voucher(
        ctx, party_name="Bharat Supplies", taxable_value="0",
        voucher_date=date(2026, 6, 1), reference="BILL-1",
        items=[{"stock_item": "Widget", "quantity": "30", "rate": "2000", "unit": "Pcs"}],
    )
    await queue.approve(purchase.data["ticket"], "test")

    sale = await vouchers.create_sales_voucher(
        ctx, party_name="Acme Industries", taxable_value="0",
        voucher_date=date(2026, 6, 2), reference="INV-S1",
        items=[{"stock_item": "Widget", "quantity": "5", "rate": "2500", "unit": "Pcs"}],
    )
    await queue.approve(sale.data["ticket"], "test")

    summary = await stock.stock_summary(ctx)
    widget = next(row for row in summary.data["rows"] if row["item"] == "Widget")
    # 10 opening + 30 in - 5 out
    assert Decimal(widget["quantity"]) == Decimal("35")
    # Weighted average of 10 @ 2000 and 30 @ 2000
    assert Decimal(widget["rate"]) == Decimal("2000.00")
    assert Decimal(widget["value"]) == Decimal("70000.00")
    assert summary.data["negative"] == []


async def test_stock_summary_flags_a_negative_item(ctx, queue, fake_tally):
    await seed_items(ctx, queue)
    sale = await vouchers.create_sales_voucher(
        ctx, party_name="Acme Industries", taxable_value="0",
        voucher_date=date(2026, 6, 2), reference="INV-S2",
        items=[{"stock_item": "Widget", "quantity": "50", "rate": "2500", "unit": "Pcs"}],
    )
    await queue.approve(sale.data["ticket"], "test")

    summary = await stock.stock_summary(ctx)
    assert summary.data["negative"] == ["Widget"]
    assert "Negative stock on: Widget" in summary.message


async def test_stock_ledger_runs_a_balance(ctx, queue, fake_tally):
    await seed_items(ctx, queue)
    purchase = await vouchers.create_purchase_voucher(
        ctx, party_name="Bharat Supplies", taxable_value="0",
        voucher_date=date(2026, 6, 1), reference="BILL-2",
        items=[{"stock_item": "Widget", "quantity": "30", "rate": "2000", "unit": "Pcs"}],
    )
    await queue.approve(purchase.data["ticket"], "test")

    ledger = await stock.stock_ledger(ctx, "Widget")
    assert len(ledger.data) == 1
    assert ledger.data[0]["in"] == "30"
    assert Decimal(ledger.data[0]["balance"]) == Decimal("40")
    assert "40 Pcs on hand" in ledger.message


async def test_stock_ledger_suggests_on_a_typo(ctx, queue):
    await seed_items(ctx, queue)
    result = await stock.stock_ledger(ctx, "Widgt")
    assert "No stock item named" in result.message
    assert "Widget" in result.message


async def test_list_stock_items_reports_openings(ctx, queue):
    await seed_items(ctx, queue)
    result = await stock.list_stock_items(ctx)
    widget = next(row for row in result.data if row["name"] == "Widget")
    assert widget["unit"] == "Pcs"
    assert widget["hsn"] == "8471"
    assert Decimal(widget["opening_quantity"]) == Decimal("10")


# --- registry ---------------------------------------------------------------


def test_the_stock_tools_are_registered():
    from tallyagent_tools import registry

    names = {tool.name for tool in registry.TOOLS}
    assert {"stock_summary", "stock_ledger", "list_stock_items"} <= names
    assert registry.get("create_stock_masters").mutating
    assert not registry.get("stock_summary").mutating


def test_tool_names_are_unique():
    """A duplicate silently shadows one definition and the model gets a schema
    that does not match the function it reaches."""
    from tallyagent_tools import registry

    names = [tool.name for tool in registry.TOOLS]
    assert len(names) == len(set(names))
