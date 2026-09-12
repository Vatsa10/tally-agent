"""Voucher-creating tools.

Each builds a balanced draft from plain arguments, then hands it to
``base.submit``. GST ledger selection is derived from the party's state vs the
company's - never taken on trust from the caller, because the caller is often
a language model reading a blurry invoice.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from tallyagent_core import idempotency
from tallyagent_core.models import (
    GSTDetails,
    InventoryLine,
    Voucher,
    VoucherLine,
    VoucherType,
)
from tallyagent_core.models.voucher import PAISA
from tallyagent_tally.xml import builders, parsers
from tallyagent_tally.xml.quirks import from_tally_date
from tallyagent_tools.base import (
    PendingAction,
    ToolContext,
    ToolResult,
    submit,
)

# Ledger names follow the convention seeded in the demo company. A firm with a
# different chart configures these; they are never invented at runtime.
DEFAULT_GST_LEDGERS = {
    "output_cgst": "Output CGST",
    "output_sgst": "Output SGST",
    "output_igst": "Output IGST",
    "input_cgst": "Input CGST",
    "input_sgst": "Input SGST",
    "input_igst": "Input IGST",
}


def _split_tax(
    taxable_value: Decimal, rate: Decimal, interstate: bool
) -> tuple[Decimal, Decimal, Decimal]:
    """Return (cgst, sgst, igst). Intra-state halves are rounded so they sum to
    the total tax exactly - halving 18% of an odd amount otherwise loses a paisa.
    """
    total = (taxable_value * rate / Decimal("100")).quantize(PAISA)
    if interstate:
        return Decimal("0.00"), Decimal("0.00"), total
    half = (total / 2).quantize(PAISA)
    return half, total - half, Decimal("0.00")


async def _party_state(ctx: ToolContext, party_name: str) -> str | None:
    masters = await ctx.masters()
    for party in masters.parties:
        if party.name == party_name:
            return party.state_code
    return None


def _inventory_lines(
    items: list[dict[str, Any]], outward: bool
) -> list[InventoryLine]:
    """Turn plain item dicts into stock movements.

    Quantity is signed here, once: outward on a sale, inward on a purchase. The
    caller passes a positive quantity and says what kind of voucher it is,
    because asking a model to get a sign right is asking for a reversed entry.
    """
    lines: list[InventoryLine] = []
    for item in items:
        quantity = abs(Decimal(str(item["quantity"])))
        lines.append(
            InventoryLine(
                stock_item=str(item["stock_item"]),
                quantity=-quantity if outward else quantity,
                rate=Decimal(str(item["rate"])),
                unit=str(item.get("unit") or ""),
                godown=str(item.get("godown") or ""),
                amount_override=(
                    Decimal(str(item["amount"])) if item.get("amount") is not None else None
                ),
            )
        )
    return lines


async def _gst_voucher(
    ctx: ToolContext,
    *,
    voucher_type: VoucherType,
    party_name: str,
    taxable_value: Decimal,
    gst_rate: Decimal,
    revenue_ledger: str,
    voucher_date: date,
    reference: str,
    narration: str,
    place_of_supply: str | None,
    inventory: list[InventoryLine] | None = None,
) -> Voucher:
    state = place_of_supply or await _party_state(ctx, party_name)
    interstate = bool(state) and state != ctx.company.state_code
    cgst, sgst, igst = _split_tax(taxable_value, gst_rate, interstate)
    total = (taxable_value + cgst + sgst + igst).quantize(PAISA)

    is_sale = voucher_type is VoucherType.SALES
    prefix = "output" if is_sale else "input"
    # Sale: party debited, income credited. Purchase: the mirror image.
    sign = Decimal("1") if is_sale else Decimal("-1")

    lines = [
        # The invoice number rides on the party line as a bill reference. Our
        # parties have bill-wise tracking on, so without it Tally files the
        # whole amount "On Account" and outstanding-by-bill is impossible -
        # you get a party balance and no idea which invoice is unpaid.
        VoucherLine(
            ledger_name=party_name,
            amount=total * sign,
            bill_reference=reference or None,
        ),
        VoucherLine(ledger_name=revenue_ledger, amount=-taxable_value * sign),
    ]
    if igst:
        lines.append(
            VoucherLine(
                ledger_name=DEFAULT_GST_LEDGERS[f"{prefix}_igst"], amount=-igst * sign
            )
        )
    else:
        if cgst:
            lines.append(
                VoucherLine(
                    ledger_name=DEFAULT_GST_LEDGERS[f"{prefix}_cgst"],
                    amount=-cgst * sign,
                )
            )
        if sgst:
            lines.append(
                VoucherLine(
                    ledger_name=DEFAULT_GST_LEDGERS[f"{prefix}_sgst"],
                    amount=-sgst * sign,
                )
            )

    return Voucher(
        voucher_type=voucher_type,
        date=voucher_date,
        party_name=party_name,
        reference=reference,
        narration=narration,
        lines=lines,
        inventory=inventory or [],
        gst=GSTDetails(
            rate=gst_rate,
            taxable_value=taxable_value,
            cgst=cgst,
            sgst=sgst,
            igst=igst,
            place_of_supply=state,
        ),
    )


async def create_sales_voucher(
    ctx: ToolContext,
    party_name: str,
    taxable_value: Decimal | str,
    gst_rate: Decimal | str = "18",
    voucher_date: date | None = None,
    reference: str = "",
    narration: str = "",
    sales_ledger: str = "Sales - GST 18%",
    place_of_supply: str | None = None,
    items: list[dict[str, Any]] | None = None,
) -> ToolResult:
    """Raise a sales invoice against a customer.

    With ``items`` the voucher also moves stock, and the taxable value is
    computed from the goods rather than taken on trust - the two cannot then
    disagree.
    """
    inventory = _inventory_lines(items or [], outward=True)
    if inventory:
        taxable_value = sum((line.amount for line in inventory), Decimal("0"))

    voucher = await _gst_voucher(
        ctx,
        voucher_type=VoucherType.SALES,
        party_name=party_name,
        taxable_value=Decimal(str(taxable_value)),
        inventory=inventory,
        gst_rate=Decimal(str(gst_rate)),
        revenue_ledger=sales_ledger,
        voucher_date=voucher_date or date.today(),
        reference=reference,
        narration=narration,
        place_of_supply=place_of_supply,
    )
    summary = (
        f"Sales invoice {reference or '(no ref)'} to {party_name} for "
        f"{voucher.amount} on {voucher.date}"
    )
    return await submit(ctx, "create_sales_voucher", voucher, summary)


async def create_purchase_voucher(
    ctx: ToolContext,
    party_name: str,
    taxable_value: Decimal | str,
    gst_rate: Decimal | str = "18",
    voucher_date: date | None = None,
    reference: str = "",
    narration: str = "",
    purchase_ledger: str = "Purchase - GST 18%",
    place_of_supply: str | None = None,
    items: list[dict[str, Any]] | None = None,
) -> ToolResult:
    """Book a supplier bill, optionally bringing stock in."""
    inventory = _inventory_lines(items or [], outward=False)
    if inventory:
        taxable_value = sum((line.amount for line in inventory), Decimal("0"))

    voucher = await _gst_voucher(
        ctx,
        voucher_type=VoucherType.PURCHASE,
        party_name=party_name,
        taxable_value=Decimal(str(taxable_value)),
        inventory=inventory,
        gst_rate=Decimal(str(gst_rate)),
        revenue_ledger=purchase_ledger,
        voucher_date=voucher_date or date.today(),
        reference=reference,
        narration=narration,
        place_of_supply=place_of_supply,
    )
    summary = (
        f"Purchase bill {reference or '(no ref)'} from {party_name} for "
        f"{voucher.amount} on {voucher.date}"
    )
    return await submit(ctx, "create_purchase_voucher", voucher, summary)


async def create_payment(
    ctx: ToolContext,
    party_name: str,
    amount: Decimal | str,
    bank_ledger: str = "Bank - HDFC 1234",
    voucher_date: date | None = None,
    reference: str = "",
    narration: str = "",
) -> ToolResult:
    """Pay a supplier: debit the party, credit the bank."""
    value = Decimal(str(amount)).quantize(PAISA)
    voucher = Voucher(
        voucher_type=VoucherType.PAYMENT,
        date=voucher_date or date.today(),
        party_name=party_name,
        reference=reference,
        narration=narration,
        lines=[
            VoucherLine(
                ledger_name=party_name, amount=value, bill_reference=reference or None
            ),
            VoucherLine(ledger_name=bank_ledger, amount=-value),
        ],
    )
    summary = f"Payment of {value} to {party_name} from {bank_ledger}"
    return await submit(ctx, "create_payment", voucher, summary)


async def create_receipt(
    ctx: ToolContext,
    party_name: str,
    amount: Decimal | str,
    bank_ledger: str = "Bank - HDFC 1234",
    voucher_date: date | None = None,
    reference: str = "",
    narration: str = "",
) -> ToolResult:
    """Receive from a customer: debit the bank, credit the party."""
    value = Decimal(str(amount)).quantize(PAISA)
    voucher = Voucher(
        voucher_type=VoucherType.RECEIPT,
        date=voucher_date or date.today(),
        party_name=party_name,
        reference=reference,
        narration=narration,
        lines=[
            VoucherLine(ledger_name=bank_ledger, amount=value),
            VoucherLine(
                ledger_name=party_name, amount=-value, bill_reference=reference or None
            ),
        ],
    )
    summary = f"Receipt of {value} from {party_name} into {bank_ledger}"
    return await submit(ctx, "create_receipt", voucher, summary)


async def create_journal(
    ctx: ToolContext,
    lines: list[dict[str, object]],
    voucher_date: date | None = None,
    narration: str = "",
    reference: str = "",
) -> ToolResult:
    """Free-form journal. ``lines`` is [{"ledger": str, "amount": Decimal}],
    positive for debit, negative for credit.

    A line may carry ``"cost_centre"``; its category is resolved from the
    masters, because Tally rejects an allocation filed under the wrong one.
    """
    categories = await ctx._cost_centres()  # type: ignore[attr-defined]
    voucher = Voucher(
        voucher_type=VoucherType.JOURNAL,
        date=voucher_date or date.today(),
        narration=narration,
        reference=reference,
        lines=[
            VoucherLine(
                ledger_name=str(line["ledger"]),
                amount=Decimal(str(line["amount"])).quantize(PAISA),
                cost_centre=str(line["cost_centre"]) if line.get("cost_centre") else None,
                cost_category=categories.get(str(line.get("cost_centre") or "")),
            )
            for line in lines
        ],
    )
    summary = f"Journal for {voucher.amount} on {voucher.date}: {narration or 'no narration'}"
    return await submit(ctx, "create_journal", voucher, summary)


async def find_voucher(
    ctx: ToolContext,
    reference: str = "",
    party_name: str = "",
    voucher_number: str = "",
) -> ToolResult:
    """Find a voucher and, crucially, the REMOTEID needed to change it.

    Amending or deleting requires the identity tallyagent assigned when it
    posted the voucher. Tally's own MASTERID and GUID will not do: an amendment
    addressed by either silently creates a duplicate instead of changing
    anything.
    """
    rows = await ctx.backend.get_vouchers(company=ctx.company.name)  # type: ignore[attr-defined]

    # Tally reports its own GUID in REMOTEID, never the one we supplied, so the
    # amendable identity comes from our index joined on Tally's MASTERID.
    ours: dict[str, str] = {}
    if ctx.voucher_index is not None:
        ours = ctx.voucher_index.by_master_id()

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("MASTERID") or "")
        found = grouped.setdefault(
            key,
            {
                "remote_id": ours.get(str(row.get("MASTERID") or ""), ""),
                "master_id": str(row.get("MASTERID") or ""),
                "voucher_number": str(row.get("VOUCHERNUMBER") or ""),
                "type": str(row.get("VOUCHERTYPENAME") or ""),
                "date": str(from_tally_date(str(row.get("DATE") or "")) or ""),
                "party": str(row.get("PARTYLEDGERNAME") or ""),
                "reference": str(row.get("REFERENCE") or ""),
                "narration": str(row.get("NARRATION") or ""),
                "amount": Decimal("0"),
            },
        )
        amount = parsers.to_decimal(row.get("AMOUNT"))
        if amount > 0:
            found["amount"] += amount

    matches = [
        found
        for found in grouped.values()
        if (not reference or found["reference"] == reference)
        and (not party_name or found["party"] == party_name)
        and (not voucher_number or found["voucher_number"] == voucher_number)
    ]
    for found in matches:
        found["amount"] = format(found["amount"].quantize(PAISA), "f")

    if not matches:
        return ToolResult(message="No voucher matches that.", data=[])

    amendable = [m for m in matches if m["remote_id"]]
    note = ""
    if len(amendable) < len(matches):
        note = (
            f" {len(matches) - len(amendable)} of these were not posted by "
            "tallyagent and carry no REMOTEID, so they cannot be amended or "
            "deleted from here - change them in Tally."
        )
    return ToolResult(
        message=f"{len(matches)} voucher(s) found.{note}", data=matches
    )


async def alter_voucher(
    ctx: ToolContext,
    remote_id: str,
    lines: list[dict[str, object]],
    voucher_type: str = "Journal",
    voucher_date: date | None = None,
    narration: str = "",
    reference: str = "",
    party_name: str = "",
) -> ToolResult:
    """Amend a voucher tallyagent posted, addressed by its REMOTEID.

    Goes through validation and the approval queue like any other write - an
    amendment changes the books exactly as much as a new voucher does.
    """
    voucher = Voucher(
        voucher_type=VoucherType(voucher_type),
        date=voucher_date or date.today(),
        narration=narration,
        reference=reference,
        party_name=party_name or None,
        remote_id=remote_id,
        lines=[
            VoucherLine(
                ledger_name=str(line["ledger"]),
                amount=Decimal(str(line["amount"])).quantize(PAISA),
                bill_reference=str(line["bill_reference"])
                if line.get("bill_reference")
                else None,
            )
            for line in lines
        ],
    )
    summary = (
        f"Amend voucher {reference or remote_id[:16]} to {voucher.amount} "
        f"on {voucher.date}"
    )
    return await submit(
        ctx, "alter_voucher", voucher, summary, payload={"remote_id": remote_id}
    )


async def delete_voucher(
    ctx: ToolContext,
    remote_id: str,
    voucher_type: str = "Journal",
    voucher_date: date | None = None,
    reason: str = "",
) -> ToolResult:
    """Delete a voucher tallyagent posted.

    Queued for approval with the voucher's current ledger impact shown, so the
    approver sees what is about to disappear rather than an id.
    """
    ctx.live.require_write_scope(ctx.company.name)

    found = await find_voucher(ctx)
    target = next(
        (row for row in (found.data or []) if row["remote_id"] == remote_id), None
    )
    if target is None:
        return ToolResult(
            message=(
                f"No voucher posted by tallyagent has REMOTEID {remote_id!r}. "
                "Use find_voucher to list what can be deleted; anything entered "
                "directly in Tally has to be deleted there."
            )
        )

    when = voucher_date or _parse_iso(target["date"]) or date.today()
    kind = VoucherType(target["type"] or voucher_type)

    summary = (
        f"DELETE {kind.value.lower()} voucher {target['voucher_number']} "
        f"({target['reference'] or 'no reference'}) dated {target['date']} "
        f"for {target['amount']}"
        + (f" - {reason}" if reason else "")
    )

    pending = PendingAction(
        action_type="delete_voucher",
        company=ctx.company.name,
        summary=summary,
        idempotency_key=idempotency.make_key(
            ctx.company.name, _stand_in(kind, when, remote_id)
        ),
        raw_xml=builders.to_string(
            builders.build_voucher_delete_element(remote_id, kind, when)
        ),
        source=ctx.source,
        payload={
            "kind": "delete_voucher",
            "remote_id": remote_id,
            "master_id": target["master_id"],
            "voucher_type": kind.value,
            "date": when.isoformat(),
            "reason": reason,
            "deleting": target,
        },
    )

    if ctx.enqueue is None:
        return ToolResult(message=f"Awaiting approval: {summary}", pending=pending)
    ticket = await ctx.enqueue(pending)
    return ToolResult(
        message=f"Queued as {ticket}: {summary}",
        data={"ticket": ticket, "deleting": target},
        pending=pending,
    )


def _stand_in(kind: VoucherType, when: date, remote_id: str) -> Voucher:
    """A synthetic voucher so a deletion can have an idempotency key."""
    return Voucher(
        voucher_type=kind,
        date=when,
        reference=f"delete:{remote_id}",
        lines=[VoucherLine(ledger_name=f"delete:{remote_id}", amount=Decimal("0"))],
    )


def _parse_iso(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
