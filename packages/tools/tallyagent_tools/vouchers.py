"""Voucher-creating tools.

Each builds a balanced draft from plain arguments, then hands it to
``base.submit``. GST ledger selection is derived from the party's state vs the
company's - never taken on trust from the caller, because the caller is often
a language model reading a blurry invoice.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from tallyagent_core.models import (
    GSTDetails,
    Voucher,
    VoucherLine,
    VoucherType,
)
from tallyagent_core.models.voucher import PAISA
from tallyagent_tools.base import ToolContext, ToolResult, submit

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
        VoucherLine(ledger_name=party_name, amount=total * sign),
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
) -> ToolResult:
    """Raise a sales invoice against a customer."""
    voucher = await _gst_voucher(
        ctx,
        voucher_type=VoucherType.SALES,
        party_name=party_name,
        taxable_value=Decimal(str(taxable_value)),
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
) -> ToolResult:
    """Book a supplier bill."""
    voucher = await _gst_voucher(
        ctx,
        voucher_type=VoucherType.PURCHASE,
        party_name=party_name,
        taxable_value=Decimal(str(taxable_value)),
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
            VoucherLine(ledger_name=party_name, amount=value),
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
            VoucherLine(ledger_name=party_name, amount=-value),
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
    positive for debit, negative for credit."""
    voucher = Voucher(
        voucher_type=VoucherType.JOURNAL,
        date=voucher_date or date.today(),
        narration=narration,
        reference=reference,
        lines=[
            VoucherLine(
                ledger_name=str(line["ledger"]),
                amount=Decimal(str(line["amount"])).quantize(PAISA),
            )
            for line in lines
        ],
    )
    summary = f"Journal for {voucher.amount} on {voucher.date}: {narration or 'no narration'}"
    return await submit(ctx, "create_journal", voucher, summary)


async def alter_voucher(
    ctx: ToolContext,
    master_id: str,
    lines: list[dict[str, object]],
    voucher_type: str = "Journal",
    voucher_date: date | None = None,
    narration: str = "",
) -> ToolResult:
    """Amend an existing voucher, matched on MASTERID."""
    voucher = Voucher(
        voucher_type=VoucherType(voucher_type),
        date=voucher_date or date.today(),
        narration=narration,
        master_id=master_id,
        lines=[
            VoucherLine(
                ledger_name=str(line["ledger"]),
                amount=Decimal(str(line["amount"])).quantize(PAISA),
            )
            for line in lines
        ],
    )
    summary = f"Amend voucher {master_id} to {voucher.amount} on {voucher.date}"
    return await submit(
        ctx, "alter_voucher", voucher, summary, payload={"master_id": master_id}
    )
