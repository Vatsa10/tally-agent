"""Inventory tools: stock masters, stock movements, stock reports.

A trading company's vouchers move two things at once - value and goods. The
accounting side already worked; this is the goods.

Like the trial balance, the stock summary is **derived** rather than asked for:
TallyPrime 1.1.7.1's report exports come back empty, so quantity on hand is
opening plus movements, computed from the same voucher collection everything
else reads (docs/DECISIONS.md D-107).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from lxml import etree

from tallyagent_core import idempotency
from tallyagent_core.models import (
    Godown,
    StockGroup,
    StockItem,
    Unit,
    Voucher,
    VoucherLine,
    VoucherType,
)
from tallyagent_core.models.inventory import QUANTITY
from tallyagent_core.models.voucher import PAISA
from tallyagent_tally.xml import builders, parsers, quirks
from tallyagent_tally.xml.quirks import from_tally_date
from tallyagent_tools.base import PendingAction, ToolContext, ToolResult

log = logging.getLogger(__name__)

#: Tally ships this godown with every inventory-enabled company.
DEFAULT_GODOWN = "Main Location"

#: A TDL collection is the only way to read stock masters, same as ledgers.
STOCK_ITEM_TDL = (
    '<COLLECTION NAME="TAStockItems" ISMODIFY="No">'
    "<TYPE>StockItem</TYPE>"
    "<FETCH>NAME,PARENT,BASEUNITS,HSNCODE,OPENINGBALANCE,OPENINGRATE,"
    "OPENINGVALUE,MASTERID</FETCH>"
    "</COLLECTION>"
)
UNIT_TDL = (
    '<COLLECTION NAME="TAUnits" ISMODIFY="No">'
    "<TYPE>Unit</TYPE><FETCH>NAME,FORMALNAME,DECIMALPLACES</FETCH>"
    "</COLLECTION>"
)
GODOWN_TDL = (
    '<COLLECTION NAME="TAGodowns" ISMODIFY="No">'
    "<TYPE>Godown</TYPE><FETCH>NAME,PARENT</FETCH>"
    "</COLLECTION>"
)


def format_quantity(value: Decimal) -> str:
    """Render a quantity without trailing zeros - or scientific notation.

    ``Decimal("40").normalize()`` is ``4E+1``, which is not a quantity anyone
    wants to read on a stock report.
    """
    quantized = value.quantize(QUANTITY)
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _quantity(raw: object) -> Decimal:
    """Tally writes quantities as ``20 Nos``; the unit is not part of the number."""
    text = str(raw or "").strip()
    if not text:
        return Decimal("0")
    head = text.split()[0].replace(",", "")
    try:
        return Decimal(head)
    except Exception:  # noqa: BLE001 - a report must not die on one odd cell
        return Decimal("0")


def _rate(raw: object) -> Decimal:
    """And rates as ``2500.00/Nos``."""
    text = str(raw or "").strip()
    if not text:
        return Decimal("0")
    head = text.split("/")[0].replace(",", "").strip()
    try:
        return Decimal(head)
    except Exception:  # noqa: BLE001
        return Decimal("0")


# --- masters ----------------------------------------------------------------


async def list_stock_items(ctx: ToolContext, refresh: bool = False) -> ToolResult:
    """The stock item masters, with their opening stock."""
    items = await stock_masters(ctx)
    return ToolResult(
        message=f"{len(items)} stock item(s).",
        data=[
            {
                "name": item.name,
                "group": item.parent,
                "unit": item.base_units,
                "hsn": item.hsn_code or "",
                "opening_quantity": format(item.opening_quantity, "f"),
                "opening_rate": format(item.opening_rate, "f"),
            }
            for item in items
        ],
    )


async def stock_masters(ctx: ToolContext) -> list[StockItem]:
    """Stock items as models. Cached per turn alongside the ledger masters."""
    cached = getattr(ctx, "_stock_cache", None)
    if cached is not None:
        return cached

    rows = await ctx.backend.client.export_collection(  # type: ignore[attr-defined]
        "TAStockItems", "STOCKITEM", company=ctx.company.name, tdl=STOCK_ITEM_TDL
    )
    items = [
        StockItem(
            name=str(row.get("NAME") or row.get("@NAME") or "").strip(),
            parent=str(row.get("PARENT") or "").strip(),
            base_units=str(row.get("BASEUNITS") or "").strip() or "Nos",
            hsn_code=str(row.get("HSNCODE") or "").strip() or None,
            opening_quantity=_quantity(row.get("OPENINGBALANCE")),
            opening_rate=_rate(row.get("OPENINGRATE")),
            master_id=str(row.get("MASTERID") or "").strip() or None,
        )
        for row in rows
        if (row.get("NAME") or row.get("@NAME"))
    ]
    ctx._stock_cache = items  # type: ignore[attr-defined]
    return items


async def create_stock_masters(
    ctx: ToolContext,
    items: list[dict[str, Any]] | None = None,
    units: list[str] | None = None,
    groups: list[str] | None = None,
    godowns: list[str] | None = None,
) -> ToolResult:
    """Create stock masters as one batched approval.

    Units and groups go in the same envelope as the items that reference them:
    Tally resolves masters within a single import, so an item can name a unit
    created two elements earlier.
    """
    ctx.live.require_write_scope(ctx.company.name)

    wanted_units = {str(name) for name in (units or [])} | {
        str(spec.get("unit") or "Nos") for spec in (items or [])
    }
    reserved = sorted(wanted_units & quirks.RESERVED_UNIT_NAMES)
    if reserved:
        return ToolResult(
            message=(
                f"TallyPrime will not accept the unit symbol(s) {', '.join(reserved)}: "
                "the name is taken internally but unusable, so an item based on it is "
                "rejected as 'does not exist'. Use another symbol, such as Pcs."
            )
        )

    existing_items = {item.name for item in await stock_masters(ctx)}
    existing_units = await _existing(ctx, "TAUnits", "UNIT", UNIT_TDL)
    existing_godowns = await _existing(ctx, "TAGodowns", "GODOWN", GODOWN_TDL)

    new_units = [
        Unit(name=name) for name in (units or []) if name not in existing_units
    ]
    new_groups = [StockGroup(name=name) for name in (groups or [])]
    new_godowns = [
        Godown(name=name) for name in (godowns or []) if name not in existing_godowns
    ]
    new_items = [
        StockItem(
            name=str(spec["name"]),
            parent=str(spec.get("group") or ""),
            base_units=str(spec.get("unit") or "Nos"),
            hsn_code=str(spec.get("hsn") or "") or None,
            gst_rate=(
                Decimal(str(spec["gst_rate"])) if spec.get("gst_rate") is not None else None
            ),
            opening_quantity=Decimal(str(spec.get("opening_quantity") or "0")),
            opening_rate=Decimal(str(spec.get("opening_rate") or "0")),
        )
        for spec in (items or [])
        if str(spec.get("name")) not in existing_items
    ]

    if not (new_units or new_groups or new_godowns or new_items):
        return ToolResult(message="Those stock masters already exist; nothing to do.")

    elements = (
        [builders.build_unit_element(u) for u in new_units]
        + [builders.build_stock_group_element(g) for g in new_groups]
        + [builders.build_godown_element(g) for g in new_godowns]
        + [builders.build_stock_item_element(i) for i in new_items]
    )
    combined = etree.Element("MASTERS")
    for element in elements:
        combined.append(element)

    summary = (
        f"Create {len(new_items)} stock item(s), {len(new_units)} unit(s), "
        f"{len(new_groups)} group(s), {len(new_godowns)} godown(s)"
    )
    listing = "\n".join(
        [f"  unit    {u.name}" for u in new_units]
        + [f"  group   {g.name}" for g in new_groups]
        + [f"  godown  {g.name}" for g in new_godowns]
        + [
            f"  item    {i.name} ({i.base_units}"
            + (f", opening {i.opening_quantity} @ {i.opening_rate}" if i.opening_quantity else "")
            + ")"
            for i in new_items
        ]
    )

    stand_in = Voucher(
        voucher_type=VoucherType.JOURNAL,
        date=ctx.company.period.start if ctx.company.period else date(2026, 4, 1),
        reference=f"stock_masters:{','.join(sorted(i.name for i in new_items))[:60]}",
        lines=[VoucherLine(ledger_name="stock_masters", amount=Decimal("0"))],
    )
    pending = PendingAction(
        action_type="create_stock_masters",
        company=ctx.company.name,
        summary=summary,
        idempotency_key=idempotency.make_key(ctx.company.name, stand_in),
        raw_xml=builders.to_string(combined),
        source=ctx.source,
        payload={
            "kind": "stock_masters",
            "units": [u.model_dump(mode="json") for u in new_units],
            "groups": [g.model_dump(mode="json") for g in new_groups],
            "godowns": [g.model_dump(mode="json") for g in new_godowns],
            "items": [i.model_dump(mode="json") for i in new_items],
        },
    )

    if ctx.enqueue is None:
        return ToolResult(message=f"{summary}\n{listing}", pending=pending)
    ticket = await ctx.enqueue(pending)
    return ToolResult(
        message=f"Queued as {ticket}: {summary}\n{listing}",
        data={"ticket": ticket, "items": len(new_items)},
        pending=pending,
    )


async def _existing(ctx: ToolContext, name: str, tag: str, tdl: str) -> set[str]:
    try:
        rows = await ctx.backend.client.export_collection(  # type: ignore[attr-defined]
            name, tag, company=ctx.company.name, tdl=tdl
        )
    except Exception:  # noqa: BLE001 - a company without inventory has none
        return set()
    return {
        str(row.get("NAME") or row.get("@NAME") or "").strip()
        for row in rows
        if (row.get("NAME") or row.get("@NAME"))
    }


async def execute_stock_masters(ctx: ToolContext, pending: PendingAction):  # type: ignore[no-untyped-def]
    """Perform a queued stock-master batch. Called by the one executor."""
    from tallyagent_backends.accounting_backend import WriteResult

    payload = pending.payload

    # Tally resolves a master reference against what is *already in the
    # company*, not against the rest of the envelope: a stock item naming a unit
    # created two elements earlier still fails with "Unit 'Nos' does not exist!".
    # So the batch goes out in dependency order, one request per wave.
    waves = [
        [builders.build_unit_element(Unit.model_validate(u)) for u in payload.get("units", [])],
        [
            builders.build_stock_group_element(StockGroup.model_validate(g))
            for g in payload.get("groups", [])
        ]
        + [
            builders.build_godown_element(Godown.model_validate(g))
            for g in payload.get("godowns", [])
        ],
        [
            builders.build_stock_item_element(StockItem.model_validate(i))
            for i in payload.get("items", [])
        ],
    ]
    waves = [wave for wave in waves if wave]
    if not waves:
        return WriteResult(ok=True, idempotency_key=pending.idempotency_key)

    errors: list[str] = []
    sent: list[str] = []
    master_id = ""
    for wave in waves:
        result, raw = await ctx.backend.client.import_elements(  # type: ignore[attr-defined]
            wave, "All Masters", company=ctx.company.name
        )
        sent.append(raw)
        errors.extend(result.messages)
        master_id = result.last_master_id or master_id
        if result.messages:
            # A later wave depends on this one, so stop rather than pile up
            # failures that all say the same thing.
            break

    ctx._stock_cache = None  # type: ignore[attr-defined]
    return WriteResult(
        ok=not errors,
        master_id=master_id,
        idempotency_key=pending.idempotency_key,
        errors=errors,
        raw_request="\n".join(sent),
    )


# --- movements and reports --------------------------------------------------


@dataclass(slots=True)
class Movement:
    stock_item: str
    quantity: Decimal
    value: Decimal
    when: date | None
    voucher_type: str
    voucher_number: str
    party: str


async def stock_movements(
    ctx: ToolContext,
    from_date: date | None = None,
    to_date: date | None = None,
) -> list[Movement]:
    """Every stock movement in a period, one per inventory line."""
    rows = await ctx.backend.stock_rows(  # type: ignore[attr-defined]
        company=ctx.company.name, from_date=from_date, to_date=to_date
    )
    return [
        Movement(
            stock_item=str(row.get("STOCKITEMNAME") or ""),
            quantity=_quantity(row.get("ACTUALQTY")),
            value=parsers.to_decimal(row.get("AMOUNT")),
            when=from_tally_date(str(row.get("DATE") or "")),
            voucher_type=str(row.get("VOUCHERTYPENAME") or ""),
            voucher_number=str(row.get("VOUCHERNUMBER") or ""),
            party=str(row.get("PARTYLEDGERNAME") or ""),
        )
        for row in rows
        if row.get("STOCKITEMNAME")
    ]


async def on_hand(ctx: ToolContext, as_on: date | None = None) -> dict[str, Decimal]:
    """Quantity on hand per item: opening plus movements."""
    quantities: dict[str, Decimal] = {
        item.name: item.opening_quantity for item in await stock_masters(ctx)
    }
    for movement in await stock_movements(ctx, to_date=as_on):
        quantities[movement.stock_item] = (
            quantities.get(movement.stock_item, Decimal("0")) + movement.quantity
        )
    return quantities


async def stock_summary(ctx: ToolContext, as_on: date | None = None) -> ToolResult:
    """Quantity and value on hand, per item.

    Value is at weighted average cost of what came in, which is what Tally's
    default valuation does. An item with stock but no inward movement is valued
    at its opening rate.
    """
    items = {item.name: item for item in await stock_masters(ctx)}
    quantities: dict[str, Decimal] = {
        name: item.opening_quantity for name, item in items.items()
    }
    inward_qty: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    inward_value: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))

    for name, item in items.items():
        if item.opening_quantity:
            inward_qty[name] += item.opening_quantity
            inward_value[name] += item.opening_value

    for movement in await stock_movements(ctx, to_date=as_on):
        quantities[movement.stock_item] = (
            quantities.get(movement.stock_item, Decimal("0")) + movement.quantity
        )
        if movement.quantity > 0:
            inward_qty[movement.stock_item] += movement.quantity
            inward_value[movement.stock_item] += abs(movement.value)

    rows: list[dict[str, str]] = []
    total = Decimal("0")
    for name in sorted(quantities):
        quantity = quantities[name].quantize(QUANTITY)
        bought = inward_qty.get(name, Decimal("0"))
        rate = (
            (inward_value[name] / bought).quantize(PAISA)
            if bought
            else (items[name].opening_rate if name in items else Decimal("0"))
        )
        value = (quantity * rate).quantize(PAISA)
        total += value
        rows.append(
            {
                "item": name,
                "unit": items[name].base_units if name in items else "",
                "quantity": format_quantity(quantity),
                "rate": format(rate, "f"),
                "value": format(value, "f"),
            }
        )

    negative = [row["item"] for row in rows if Decimal(row["quantity"]) < 0]
    note = (
        f" Negative stock on: {', '.join(negative)} - usually a purchase that "
        "has not been entered."
        if negative
        else ""
    )
    return ToolResult(
        message=f"Stock on hand: {len(rows)} item(s), value {total}.{note}",
        data={"rows": rows, "total_value": format(total, "f"), "negative": negative},
    )


async def stock_ledger(
    ctx: ToolContext,
    stock_item: str,
    from_date: date | None = None,
    to_date: date | None = None,
) -> ToolResult:
    """Every movement of one item, with a running balance."""
    items = {item.name: item for item in await stock_masters(ctx)}
    if stock_item not in items:
        import difflib

        suggestions = difflib.get_close_matches(stock_item, sorted(items), n=3, cutoff=0.5)
        return ToolResult(
            message=(
                f"No stock item named {stock_item!r}."
                + (f" Closest: {', '.join(suggestions)}." if suggestions else "")
            ),
            data=[],
        )

    running = items[stock_item].opening_quantity
    movements = [
        movement
        for movement in await stock_movements(ctx, from_date, to_date)
        if movement.stock_item == stock_item
    ]
    movements.sort(key=lambda m: (m.when or date.min, m.voucher_number))

    rows = []
    for movement in movements:
        running += movement.quantity
        rows.append(
            {
                "date": str(movement.when or ""),
                "type": movement.voucher_type,
                "voucher": movement.voucher_number,
                "party": movement.party,
                "in": format_quantity(movement.quantity) if movement.quantity > 0 else "",
                "out": format_quantity(abs(movement.quantity))
                if movement.quantity < 0
                else "",
                "balance": format_quantity(running),
            }
        )
    return ToolResult(
        message=(
            f"{stock_item}: {len(rows)} movement(s), "
            f"{format_quantity(running)} {items[stock_item].base_units} on hand."
        ),
        data=rows,
    )
