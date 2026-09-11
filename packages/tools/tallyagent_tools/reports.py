"""Read-only reporting tools.

Every number an answer contains comes from one of these. The agent is
instructed never to state a figure it did not get from a tool call.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from tallyagent_core.models.voucher import PAISA
from tallyagent_tally.xml import parsers
from tallyagent_tally.xml.quirks import from_tally_date
from tallyagent_tools.base import ToolContext, ToolResult

AGEING_BUCKETS = ("not due", "0-30", "31-60", "61-90", "90+")


async def _balances(ctx: ToolContext) -> tuple[dict[str, Decimal], dict[str, str]]:
    """Closing balance and group per ledger.

    Derived from opening balances plus postings - Tally's report exports return
    nothing and the obvious TDL hangs it. See docs/DECISIONS.md D-107/D-110.
    """
    masters = await ctx.masters()
    groups = {ledger.name: ledger.parent for ledger in masters.ledgers}
    balances = await ctx.backend.ledger_balances(company=ctx.company.name)  # type: ignore[attr-defined]
    return balances, groups


async def trial_balance(ctx: ToolContext, group: str = "") -> ToolResult:
    """Closing balance per ledger. Positive is a debit balance."""
    balances, groups = await _balances(ctx)
    rows = [
        {
            "ledger": name,
            "group": groups.get(name, ""),
            "balance": format(balance.quantize(PAISA), "f"),
        }
        for name, balance in sorted(balances.items())
        if not group or groups.get(name) == group
    ]
    total = sum((Decimal(row["balance"]) for row in rows), Decimal("0"))
    debits = sum(
        (Decimal(r["balance"]) for r in rows if Decimal(r["balance"]) > 0), Decimal("0")
    )
    credits = -sum(
        (Decimal(r["balance"]) for r in rows if Decimal(r["balance"]) < 0), Decimal("0")
    )

    # Tally shows an unmatched opening as "Difference in Opening Balances"
    # rather than calling the books broken, because that is what it is: a
    # ledger was opened without its contra, not a posting that went astray.
    openings = await _opening_total(ctx)
    difference = total.quantize(PAISA)
    if difference == 0:
        note = ""
    elif difference == openings.quantize(PAISA) and openings != 0:
        note = (
            f", difference in opening balances {difference} "
            "(a ledger was opened without its contra - post a capital or "
            "opening-balance entry to clear it)"
        )
    else:
        note = f", NET {difference} - the postings do not balance"

    return ToolResult(
        message=(
            f"Trial balance: {len(rows)} ledger(s), debits {debits}, "
            f"credits {credits}{note}"
        ),
        data={
            "rows": rows,
            "net": format(difference, "f"),
            "debits": format(debits, "f"),
            "credits": format(credits, "f"),
            "balanced": difference == 0,
            "opening_difference": format(
                difference if difference == openings.quantize(PAISA) else Decimal("0"),
                "f",
            ),
        },
    )


async def _opening_total(ctx: ToolContext) -> Decimal:
    masters = await ctx.masters()
    return sum((lg.opening_balance for lg in masters.ledgers), Decimal("0"))


async def _outstanding(
    ctx: ToolContext, receivable: bool, as_on: date | None, party: str
) -> ToolResult:
    bills = await ctx.backend.get_outstanding(
        receivable=receivable, company=ctx.company.name, as_on=as_on
    )
    if party:
        bills = [b for b in bills if b.party_name == party]

    buckets: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    by_party: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for bill in bills:
        buckets[bill.ageing_bucket()] += bill.amount
        by_party[bill.party_name] += bill.amount

    total = sum(by_party.values(), Decimal("0")).quantize(PAISA)
    side = "receivable" if receivable else "payable"
    return ToolResult(
        message=f"Total {side}: {total} across {len(by_party)} part(y/ies).",
        data={
            "total": format(total, "f"),
            "by_party": {k: format(v, "f") for k, v in sorted(by_party.items())},
            "ageing": {
                bucket: format(buckets[bucket], "f")
                for bucket in AGEING_BUCKETS
                if buckets[bucket]
            },
            "bills": [
                {
                    "party": b.party_name,
                    "reference": b.bill_reference,
                    "amount": format(b.amount, "f"),
                    "overdue_days": b.overdue_days,
                    "bucket": b.ageing_bucket(),
                }
                for b in bills
            ],
        },
    )


async def outstanding_receivables(
    ctx: ToolContext, as_on: date | None = None, party: str = ""
) -> ToolResult:
    """Who owes us, with ageing buckets."""
    return await _outstanding(ctx, True, as_on, party)


async def outstanding_payables(
    ctx: ToolContext, as_on: date | None = None, party: str = ""
) -> ToolResult:
    """Who we owe, with ageing buckets."""
    return await _outstanding(ctx, False, as_on, party)


async def cash_position(ctx: ToolContext) -> ToolResult:
    """Balances of every Bank Accounts and Cash-in-Hand ledger."""
    balances, groups = await _balances(ctx)
    accounts = {
        name: balance
        for name, balance in balances.items()
        if groups.get(name) in ("Bank Accounts", "Cash-in-Hand")
    }
    total = sum(accounts.values(), Decimal("0")).quantize(PAISA)
    return ToolResult(
        message=f"Cash and bank: {total} across {len(accounts)} account(s).",
        data={
            "total": format(total, "f"),
            "accounts": {k: format(v, "f") for k, v in sorted(accounts.items())},
        },
    )


async def top_debtors(ctx: ToolContext, limit: int = 5) -> ToolResult:
    """Largest receivable balances, biggest first."""
    result = await outstanding_receivables(ctx)
    by_party = result.data["by_party"]
    ranked = sorted(by_party.items(), key=lambda kv: Decimal(kv[1]), reverse=True)[:limit]
    return ToolResult(
        message="Top debtors: "
        + (", ".join(f"{name} {amount}" for name, amount in ranked) or "none"),
        data=[{"party": name, "amount": amount} for name, amount in ranked],
    )


async def day_book(
    ctx: ToolContext,
    from_date: date | None = None,
    to_date: date | None = None,
) -> ToolResult:
    """Every voucher in a date range, one row per ledger line."""
    to_date = to_date or date.today()
    from_date = from_date or to_date - timedelta(days=30)
    rows = await ctx.backend.get_report(
        "Day Book", company=ctx.company.name, from_date=from_date, to_date=to_date
    )
    entries = [
        {
            "date": str(from_tally_date(str(row.get("DATE") or "")) or ""),
            "type": str(row.get("VOUCHERTYPENAME") or ""),
            "number": str(row.get("VOUCHERNUMBER") or ""),
            "ledger": str(row.get("LEDGERNAME") or ""),
            "party": str(row.get("PARTYLEDGERNAME") or ""),
            "amount": format(parsers.to_decimal(row.get("AMOUNT")), "f"),
            "narration": str(row.get("NARRATION") or ""),
            "reference": str(row.get("REFERENCE") or ""),
        }
        for row in rows
    ]
    return ToolResult(
        message=f"{len(entries)} day book line(s) from {from_date} to {to_date}.",
        data=entries,
    )


async def party_transactions(
    ctx: ToolContext, party_name: str, limit: int = 10
) -> ToolResult:
    """The last N transactions with one party."""
    period = ctx.company.period
    book = await day_book(
        ctx,
        from_date=period.start if period else None,
        to_date=period.end if period else None,
    )
    rows = [
        row
        for row in book.data
        if party_name in (row["party"], row["ledger"])
    ]
    rows.sort(key=lambda r: r["date"], reverse=True)
    return ToolResult(
        message=f"{min(len(rows), limit)} recent transaction(s) with {party_name}.",
        data=rows[:limit],
    )


def _gst_rows(entries: list[dict[str, str]], tax_prefix: str) -> list[dict[str, str]]:
    """Fold day-book lines into one row per voucher with its tax split."""
    grouped: dict[str, dict[str, str]] = {}
    for entry in entries:
        number = entry["number"]
        row = grouped.setdefault(
            number,
            {
                "voucher_number": number,
                "date": entry["date"],
                "party": entry["party"],
                "reference": entry.get("reference", ""),
                "taxable_value": "0",
                "cgst": "0",
                "sgst": "0",
                "igst": "0",
                "total": "0",
            },
        )
        amount = Decimal(entry["amount"])
        ledger = entry["ledger"]
        if ledger == f"{tax_prefix} CGST":
            row["cgst"] = format(-amount, "f")
        elif ledger == f"{tax_prefix} SGST":
            row["sgst"] = format(-amount, "f")
        elif ledger == f"{tax_prefix} IGST":
            row["igst"] = format(-amount, "f")
        elif ledger == entry["party"]:
            row["total"] = format(abs(amount), "f")
        else:
            row["taxable_value"] = format(abs(amount), "f")
    return list(grouped.values())


async def gstr1_data(
    ctx: ToolContext, from_date: date | None = None, to_date: date | None = None
) -> ToolResult:
    """Outward supplies for a period, in GSTR-1 shape (B2B rows)."""
    book = await day_book(ctx, from_date=from_date, to_date=to_date)
    sales = [row for row in book.data if row["type"] == "Sales"]
    rows = _gst_rows(sales, "Output")
    total = sum((Decimal(r["total"]) for r in rows), Decimal("0"))
    return ToolResult(
        message=f"GSTR-1: {len(rows)} outward supply voucher(s), total {total}.",
        data=rows,
    )


async def gstr2_purchase_register(
    ctx: ToolContext, from_date: date | None = None, to_date: date | None = None
) -> ToolResult:
    """Inward supplies for a period - the book side of a GSTR-2B reconciliation."""
    book = await day_book(ctx, from_date=from_date, to_date=to_date)
    purchases = [row for row in book.data if row["type"] == "Purchase"]
    rows = _gst_rows(purchases, "Input")
    total = sum((Decimal(r["total"]) for r in rows), Decimal("0"))
    return ToolResult(
        message=f"Purchase register: {len(rows)} voucher(s), total {total}.",
        data=rows,
    )
