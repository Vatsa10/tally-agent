"""Reconciliation: bank statement vs bank ledger, GSTR-2B vs purchase register.

Both are proposal engines. Neither posts anything: an unmatched bank line
becomes a *suggested* receipt or payment for a human to approve, and a 2B
mismatch becomes a classified row in a CSV. Auto-posting reconciliation
differences is how a set of books silently stops agreeing with the bank.
"""

from __future__ import annotations

import csv
import difflib
import io
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from tallyagent_core.models.voucher import PAISA
from tallyagent_tools.base import ToolContext, ToolResult

#: A cheque cleared on the 3rd for a voucher dated the 1st is the same payment.
DEFAULT_DATE_WINDOW_DAYS = 5
#: Narrations rarely match exactly ("NEFT ACME INDS" vs "Acme Industries").
NARRATION_THRESHOLD = 0.45


@dataclass(slots=True)
class StatementRow:
    """One line of a bank statement."""

    date: date
    narration: str
    amount: Decimal  # positive = credit into the account, negative = debit out
    reference: str = ""

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> StatementRow:
        raw_date = row.get("date")
        parsed = raw_date if isinstance(raw_date, date) else _parse_date(str(raw_date))
        if parsed is None:
            raise ValueError(f"unparseable statement date: {raw_date!r}")
        return cls(
            date=parsed,
            narration=str(row.get("narration") or row.get("description") or ""),
            amount=Decimal(str(row.get("amount"))).quantize(PAISA),
            reference=str(row.get("reference") or ""),
        )


def _parse_date(raw: str) -> date | None:
    from datetime import datetime

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _guess_party(narration: str, party_names: list[str]) -> str:
    """Guess which party a bank narration refers to.

    Bank narrations are abbreviated and padded with channel noise
    ("NEFT ACME INDUSTRIES ADV"), so whole-string similarity alone misses
    obvious matches. A party whose significant words all appear in the
    narration wins outright; otherwise fall back to fuzzy similarity.
    """
    haystack = narration.lower()
    for name in party_names:
        words = [w for w in name.lower().split() if len(w) >= 4]
        if words and all(word in haystack for word in words):
            return name
    best = max(party_names, key=lambda n: _similar(narration, n), default="")
    return best if best and _similar(narration, best) >= NARRATION_THRESHOLD else ""


@dataclass(slots=True)
class Match:
    statement: StatementRow
    voucher_number: str
    voucher_date: date | None
    score: float


@dataclass(slots=True)
class ReconciliationResult:
    matched: list[Match] = field(default_factory=list)
    unmatched_statement: list[StatementRow] = field(default_factory=list)
    unmatched_ledger: list[dict[str, str]] = field(default_factory=list)
    proposals: list[dict[str, str]] = field(default_factory=list)


async def bank_reco(
    ctx: ToolContext,
    statement_rows: list[dict[str, Any]],
    bank_ledger: str = "Bank - HDFC 1234",
    from_date: date | None = None,
    to_date: date | None = None,
    date_window_days: int = DEFAULT_DATE_WINDOW_DAYS,
) -> ToolResult:
    """Match statement lines to bank ledger entries.

    Matching is on amount first (exact, to the paisa), then a date window, then
    narration similarity to break ties. Amount is never fuzzy - two different
    payments of similar size on the same day must not be collapsed.
    """
    rows = [StatementRow.from_dict(row) for row in statement_rows]
    if not rows:
        return ToolResult(message="No statement rows supplied.", data={})

    from_date = from_date or min(r.date for r in rows) - timedelta(days=date_window_days)
    to_date = to_date or max(r.date for r in rows) + timedelta(days=date_window_days)
    entries = await ctx.backend.get_bank_ledger(
        bank_ledger, from_date, to_date, ctx.company.name
    )

    result = ReconciliationResult()
    remaining = list(entries)
    window = timedelta(days=date_window_days)

    for row in rows:
        candidates = [
            entry
            for entry in remaining
            if entry.amount.quantize(PAISA) == row.amount
            and entry.date is not None
            and abs(entry.date - row.date) <= window
        ]
        if not candidates:
            result.unmatched_statement.append(row)
            continue
        best = max(
            candidates,
            key=lambda e: _similar(row.narration, f"{e.particulars} {e.narration}"),
        )
        remaining.remove(best)
        result.matched.append(
            Match(
                statement=row,
                voucher_number=best.voucher_number,
                voucher_date=best.date,
                score=_similar(row.narration, f"{best.particulars} {best.narration}"),
            )
        )

    result.unmatched_ledger = [
        {
            "date": str(entry.date or ""),
            "voucher_number": entry.voucher_number,
            "amount": format(entry.amount, "f"),
            "narration": entry.narration,
        }
        for entry in remaining
    ]

    # Propose, never post. A credit in the bank is a receipt; a debit is a payment.
    masters = await ctx.masters()
    party_names = [p.name for p in masters.parties]
    for row in result.unmatched_statement:
        guess = _guess_party(row.narration, party_names)
        result.proposals.append(
            {
                "tool": "create_receipt" if row.amount > 0 else "create_payment",
                "date": row.date.isoformat(),
                "amount": format(abs(row.amount), "f"),
                "bank_ledger": bank_ledger,
                "narration": row.narration,
                "suggested_party": guess,
            }
        )

    return ToolResult(
        message=(
            f"Bank reco on {bank_ledger}: {len(result.matched)} matched, "
            f"{len(result.unmatched_statement)} statement line(s) not in books, "
            f"{len(result.unmatched_ledger)} book entry/entries not on the statement. "
            f"{len(result.proposals)} voucher(s) proposed - none posted."
        ),
        data={
            "matched": [
                {
                    "statement_date": m.statement.date.isoformat(),
                    "amount": format(m.statement.amount, "f"),
                    "voucher_number": m.voucher_number,
                    "narration_score": round(m.score, 2),
                }
                for m in result.matched
            ],
            "unmatched_statement": [
                {
                    "date": r.date.isoformat(),
                    "amount": format(r.amount, "f"),
                    "narration": r.narration,
                }
                for r in result.unmatched_statement
            ],
            "unmatched_ledger": result.unmatched_ledger,
            "proposals": result.proposals,
        },
    )


# --- GSTR-2B ----------------------------------------------------------------

def _money(value: Decimal) -> str:
    return format(Decimal(value).quantize(PAISA), "f")


async def gstr2b_vs_purchase_register(
    ctx: ToolContext,
    gstr2b_json: dict[str, Any],
    from_date: date | None = None,
    to_date: date | None = None,
) -> ToolResult:
    """Classify every invoice as matched / missing in books / missing in 2B /
    value mismatch, and emit a CSV a CA can work through.

    The matching itself lives in ``tallyagent_tools.itc``: on supplier GSTIN
    plus invoice number, reading the portal's real GSTR-2B shape. The first
    version matched on the invoice number alone - two suppliers' bill "12"
    collided - and read only GSTR-1 style tax keys, so a real portal download
    came back with every tax as zero. The rows keep their old shape so the TUI
    and the close pack read them unchanged, with the cause and the action
    added.
    """
    from tallyagent_tools import itc, reports

    period, portal = itc.parse_2b(gstr2b_json)
    # Without dates, the window comes from the 2B file itself: its own month,
    # plus the months before it that a late-reported bill can come from. The
    # first version fell back to the register's default - today - and saw no
    # books at all, so every 2B invoice read as "not entered".
    own = itc.period_dates(period)
    if own and from_date is None and to_date is None:
        from_date, to_date = own[0] - itc.LOOKBACK, own[1]
    register = await reports.gstr2_purchase_register(
        ctx, from_date=from_date, to_date=to_date
    )
    masters = await ctx.masters()
    gstins = {p.name: (p.gstin or "") for p in masters.parties}
    result = itc.reconcile(period, portal, itc.book_invoices(register.data or [], gstins))

    status_of = {
        itc.MATCHED: "matched",
        itc.NOT_IN_BOOKS: "missing_in_books",
        itc.NOT_FILED: "missing_in_2b",
        itc.VALUE_DIFF: "value_mismatch",
        itc.TAX_DIFF: "value_mismatch",
        itc.GSTIN_MISMATCH: "gstin_mismatch",
        itc.ITC_UNAVAILABLE: "itc_not_available",
    }
    classified: list[dict[str, str]] = []
    counts: dict[str, int] = defaultdict(int)
    for finding in result.findings:
        status = status_of[finding.cause]
        counts[status] += 1
        portal_total = finding.portal.total if finding.portal else Decimal("0")
        books_total = finding.books.total if finding.books else Decimal("0")
        classified.append(
            {
                "status": status,
                "supplier": finding.supplier,
                "supplier_gstin": finding.supplier_gstin,
                "invoice_no": finding.number,
                "invoice_date": finding.invoice_date.isoformat()
                if finding.invoice_date
                else "",
                "gstr2b_total": _money(portal_total),
                "books_total": _money(books_total),
                "difference": _money(portal_total - books_total),
                "itc_at_risk": _money(finding.itc_at_risk),
                "cause": itc.CAUSE_WORDS[finding.cause],
                "action": finding.action,
            }
        )

    buffer = io.StringIO()
    if classified:
        writer = csv.DictWriter(buffer, fieldnames=list(classified[0].keys()))
        writer.writeheader()
        writer.writerows(classified)

    summary = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in sorted(counts.items()))
    return ToolResult(
        message=(
            f"GSTR-2B reconciliation - {summary or 'nothing to compare'}. "
            f"Claimable Rs {result.claimable:,.2f}, at risk Rs {result.at_risk:,.2f}."
        ),
        data={
            "rows": classified,
            "counts": dict(counts),
            "csv": buffer.getvalue(),
            "claimable": format(result.claimable, "f"),
            "at_risk": format(result.at_risk, "f"),
        },
    )
