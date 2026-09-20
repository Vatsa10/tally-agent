"""The month-end close pack: one dated run, one file, one list of decisions.

Closing a month in an Indian practice is not one job, it is the same six jobs
every month, done in a different order by whoever is free: tick the bank
statement, pull GSTR-2B and argue with it, chase the ledgers that have gone
negative, look at what is still owed, and write down what could not be fixed so
the partner can see it. Each of those already exists here as a tool. What did
not exist is the *month*: a single dated artefact that says what was checked,
what came out, and what a person still has to decide.

Nothing here posts anything. Every finding is either a fact read out of Tally
or a proposal that still has to go through the approval queue. The pack is
deliberately a file on disk as well as a chat answer, because what a practice
needs at the end of the month is something to attach to an email.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from calendar import monthrange
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from tallyagent_core.models.voucher import PAISA
from tallyagent_tools.base import ToolContext, ToolResult

log = logging.getLogger(__name__)

#: Ledgers that must never end a month on the wrong side. Cash cannot be
#: negative in reality - it means a payment was entered that the tin did not
#: have, and it is the commonest sign of a receipt that was never entered.
NEVER_NEGATIVE_GROUPS = ("Cash-in-Hand",)

#: A bank overdraft is legitimate; a bank below this without an OD facility is
#: worth a look. A knob rather than a rule, because whether it is normal is a
#: fact about the client, not about Tally.
BANK_ALERT = Decimal("0")

#: Receivables past this are the ones a partner asks about.
OVERDUE_DAYS = 90

SEVERITIES = ("high", "medium", "low")


@dataclass(slots=True)
class Finding:
    """One thing a person has to look at, in the words they would use."""

    kind: str
    severity: str
    detail: str
    amount: Decimal = Decimal("0")
    #: What to do about it. A finding nobody can act on is noise.
    fix: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "detail": self.detail,
            "amount": format(self.amount.quantize(PAISA), "f"),
            "fix": self.fix,
        }


@dataclass(slots=True)
class ClosePack:
    company: str
    month: str
    from_date: date
    to_date: date
    sections: dict[str, str] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    #: Extra files the pack produced, e.g. the GSTR-2B working. name -> content.
    attachments: dict[str, str] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        counted = Counter(f.severity for f in self.findings)
        return {severity: counted.get(severity, 0) for severity in SEVERITIES}

    def sorted_findings(self) -> list[Finding]:
        order = {severity: n for n, severity in enumerate(SEVERITIES)}
        return sorted(
            self.findings, key=lambda f: (order.get(f.severity, 9), -abs(f.amount))
        )

    def markdown(self) -> str:
        counts = self.counts
        lines = [
            f"# Month-end close - {self.company} - {self.month}",
            "",
            f"Period {self.from_date} to {self.to_date}. "
            f"{counts['high']} to fix, {counts['medium']} to check, "
            f"{counts['low']} for information.",
            "",
            "Nothing in this pack has been posted. Every proposal still goes "
            "through the approval queue.",
            "",
        ]
        for title, body in self.sections.items():
            lines += [f"## {title}", "", body.rstrip(), ""]

        lines += ["## What needs a person", ""]
        if not self.findings:
            lines += ["Nothing. The checks above all passed.", ""]
        else:
            lines += ["| | Finding | Amount | What to do |", "|---|---|---|---|"]
            for finding in self.sorted_findings():
                amount = (
                    format(finding.amount.quantize(PAISA), "f") if finding.amount else ""
                )
                lines.append(
                    f"| {finding.severity} | {finding.detail} | {amount} "
                    f"| {finding.fix} |"
                )
            lines.append("")
        return "\n".join(lines)


# --- the checks, all pure ----------------------------------------------------


def negative_cash(balances: dict[str, Decimal], groups: dict[str, str]) -> list[Finding]:
    """Cash that has gone below zero, and banks below their alert level."""
    findings = []
    for name, balance in sorted(balances.items()):
        group = groups.get(name, "")
        if group in NEVER_NEGATIVE_GROUPS and balance < 0:
            findings.append(
                Finding(
                    "negative_cash",
                    "high",
                    f"{name} is negative at month end",
                    balance,
                    "A payment was entered that the cash box could not cover - "
                    "usually a receipt that was never entered.",
                )
            )
        elif group == "Bank Accounts" and balance < BANK_ALERT:
            findings.append(
                Finding(
                    "negative_bank",
                    "medium",
                    f"{name} is overdrawn at month end",
                    balance,
                    "Confirm the OD facility, or find the missing credit.",
                )
            )
    return findings


def negative_stock(on_hand: dict[str, Decimal]) -> list[Finding]:
    """Items sold that the books never bought.

    Tally allows it, which is why every auditor asks about it: a negative
    quantity means the sale was entered and the purchase was not.
    """
    return [
        Finding(
            "negative_stock",
            "high",
            f"{item} is at {quantity} on hand",
            Decimal("0"),
            "The sale was entered before the purchase. Enter the purchase bill.",
        )
        for item, quantity in sorted(on_hand.items())
        if quantity < 0
    ]


def overdue_receivables(
    bills: list[dict[str, Any]], days: int = OVERDUE_DAYS
) -> list[Finding]:
    """Money owed for longer than anyone is comfortable with, by party."""
    by_party: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for bill in bills:
        if int(bill.get("overdue_days") or 0) >= days:
            by_party[str(bill.get("party", ""))] += Decimal(str(bill.get("amount", "0")))
    return [
        Finding(
            "overdue_receivable",
            "medium",
            f"{party} has been outstanding over {days} days",
            amount,
            "Chase it, or decide whether it is still collectable.",
        )
        for party, amount in sorted(by_party.items(), key=lambda kv: -kv[1])
        if amount > 0
    ]


def duplicate_vouchers(rows: list[dict[str, Any]]) -> list[Finding]:
    """The same party, the same amount, the same day, twice.

    This is what entering a bill from the email and again from the paper copy
    looks like in a day book. It is not proof - a client can genuinely pay
    twice in a day - so it is raised as a question, not a correction.

    A day book lists one row per ledger line, so the rows are folded back into
    vouchers first. Without that, a three-line sales invoice compares its own
    debit against its own two credits and reports itself three times.
    """
    # Tally numbers vouchers per type and restarts each year, so a Sales 14 and
    # a Journal 14 are different vouchers. Keyed on the number alone they merged
    # into one, and the pack reported a journal that did not exist.
    vouchers: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        number = str(row.get("number", ""))
        amount = Decimal(str(row.get("amount", "0")))
        voucher = vouchers.setdefault(
            (number, str(row.get("type", "")), str(row.get("date", ""))),
            {
                "date": str(row.get("date", "")),
                "party": str(row.get("party", "")),
                "type": str(row.get("type", "")),
                "total": Decimal("0"),
            },
        )
        # One side of a voucher is its value; summing both sides gives zero.
        if amount > 0:
            voucher["total"] += amount

    seen: dict[tuple[str, str, str, str], list[str]] = defaultdict(list)
    for (number, _type, _when), voucher in vouchers.items():
        key = (
            voucher["date"],
            voucher["party"],
            voucher["type"],
            format(voucher["total"].quantize(PAISA), "f"),
        )
        seen[key].append(number)

    findings = []
    for (when, party, kind, total), numbers in sorted(seen.items()):
        if len(numbers) > 1 and party and Decimal(total) > 0:
            findings.append(
                Finding(
                    "possible_duplicate",
                    "medium",
                    f"{kind} to {party} on {when} appears {len(numbers)} times "
                    f"(vouchers {', '.join(sorted(numbers))})",
                    Decimal(total),
                    "Check whether the same bill was entered twice.",
                )
            )
    return findings


def gst_findings(counts: dict[str, int], rows: list[dict[str, Any]]) -> list[Finding]:
    """What the 2B comparison means in money, not in row counts.

    Input tax credit is the whole reason a practice reconciles 2B at all, so
    the finding is the credit at risk, not "17 rows differ".
    """
    findings = []
    at_risk = sum(
        (
            Decimal(str(row.get("books_total", "0")))
            for row in rows
            if row.get("status") == "missing_in_2b"
        ),
        Decimal("0"),
    )
    if counts.get("missing_in_2b"):
        findings.append(
            Finding(
                "itc_at_risk",
                "high",
                f"{counts['missing_in_2b']} purchase(s) in the books are not in "
                "GSTR-2B",
                at_risk,
                "The supplier has not filed, or the GSTIN is wrong. The credit "
                "cannot be claimed until it appears.",
            )
        )
    if counts.get("missing_in_books"):
        findings.append(
            Finding(
                "missing_purchase",
                "medium",
                f"{counts['missing_in_books']} invoice(s) in GSTR-2B are not in "
                "the books",
                Decimal("0"),
                "Enter the bills, or confirm they belong to another period.",
            )
        )
    if counts.get("value_mismatch"):
        findings.append(
            Finding(
                "value_mismatch",
                "medium",
                f"{counts['value_mismatch']} invoice(s) differ in value between "
                "2B and the books",
                Decimal("0"),
                "Usually a rounding or a freight line. Fix the side that is wrong.",
            )
        )
    return findings


def bank_findings(data: dict[str, Any]) -> list[Finding]:
    """What is left after the statement and the ledger have been ticked off."""
    unmatched_statement = data.get("unmatched_statement") or []
    unmatched_ledger = data.get("unmatched_ledger") or []
    findings = []
    if unmatched_statement:
        findings.append(
            Finding(
                "bank_not_in_books",
                "high",
                f"{len(unmatched_statement)} statement line(s) have no voucher",
                sum(
                    (abs(Decimal(str(row["amount"]))) for row in unmatched_statement),
                    Decimal("0"),
                ),
                "Vouchers have been proposed for these. Approve or edit them.",
            )
        )
    if unmatched_ledger:
        findings.append(
            Finding(
                "books_not_on_statement",
                "medium",
                f"{len(unmatched_ledger)} book entry/entries are not on the statement",
                sum(
                    (abs(Decimal(str(row["amount"]))) for row in unmatched_ledger),
                    Decimal("0"),
                ),
                "Cheques not yet presented, or an entry against the wrong bank.",
            )
        )
    return findings


def month_dates(month: str) -> tuple[date, date]:
    """Turn 2026-06 into the first and the last of June."""
    year, _, number = month.partition("-")
    first = date(int(year), int(number), 1)
    return first, date(first.year, first.month, monthrange(first.year, first.month)[1])


def read_statement(path: Path) -> list[dict[str, Any]]:
    """Whatever the bank called its columns. Every bank names them differently,
    and ingest already knows the synonyms."""
    from tallyagent_tools import ingest

    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if path.suffix.lower() == ".csv":
        return ingest.parse_bank_statement_csv(text)
    return ingest.parse_bank_statement_text(text)


# --- the run -----------------------------------------------------------------


async def month_end_close(
    ctx: ToolContext,
    month: str,
    bank_statement: str = "",
    bank_ledger: str = "",
    gstr2b: str = "",
    out_dir: str = "reports/close",
) -> ToolResult:
    """Run every month-end check for one month and write the pack.

    The bank statement and the 2B are optional because they arrive on their own
    schedule, and a close that refuses to run without them is a close nobody
    runs on the 1st. What was skipped is said in the pack rather than left to
    be noticed.
    """
    from tallyagent_tools import reconcile, reports, stock

    from_date, to_date = month_dates(month)
    pack = ClosePack(
        company=ctx.company.name, month=month, from_date=from_date, to_date=to_date
    )

    balances, groups = await reports._balances(ctx)
    pack.findings += negative_cash(balances, groups)

    cash = await reports.cash_position(ctx)
    pack.sections["Cash and bank"] = cash.message

    on_hand = await stock.on_hand(ctx, as_on=to_date)
    pack.findings += negative_stock(on_hand)

    receivables = await reports.outstanding_receivables(ctx, as_on=to_date)
    pack.sections["Receivables"] = receivables.message
    pack.findings += overdue_receivables(receivables.data.get("bills") or [])

    book = await reports.day_book(ctx, from_date=from_date, to_date=to_date)
    rows = book.data or []
    pack.sections["Vouchers this month"] = (
        f"{len({str(r.get('number')) for r in rows})} voucher(s) posted between "
        f"{from_date} and {to_date}."
    )
    pack.findings += duplicate_vouchers(rows)

    if bank_statement:
        # Which bank the statement belongs to is optional; bank_reco keeps the
        # company's usual one as its default and this must not override it
        # with an empty string.
        which: dict[str, Any] = {"bank_ledger": bank_ledger} if bank_ledger else {}
        result = await reconcile.bank_reco(
            ctx,
            read_statement(Path(bank_statement)),
            from_date=from_date,
            to_date=to_date,
            **which,
        )
        pack.sections["Bank reconciliation"] = result.message
        data = result.data or {}
        pack.findings += bank_findings(data)
        if data.get("proposals"):
            pack.attachments[f"{month}-bank-proposals.json"] = json.dumps(
                data["proposals"], indent=2
            )
    else:
        pack.sections["Bank reconciliation"] = (
            "Not run - no bank statement was given, so the bank balance in this "
            "pack is the books' own and has not been checked against anything."
        )

    if gstr2b:
        with Path(gstr2b).open(encoding="utf-8") as handle:
            portal = json.load(handle)
        result = await reconcile.gstr2b_vs_purchase_register(
            ctx, portal, from_date=from_date, to_date=to_date
        )
        pack.sections["GSTR-2B"] = result.message
        data = result.data or {}
        pack.findings += gst_findings(data.get("counts") or {}, data.get("rows") or [])
        if data.get("csv"):
            pack.attachments[f"{month}-gstr2b.csv"] = data["csv"]
    else:
        pack.sections["GSTR-2B"] = (
            "Not run - no GSTR-2B JSON was given, so input credit for the month "
            "has not been checked against the portal."
        )

    written = write_pack(pack, Path(out_dir))
    counts = pack.counts
    return ToolResult(
        message=(
            f"Month-end close for {month}: {counts['high']} to fix, "
            f"{counts['medium']} to check. Pack written to {written}. "
            "Nothing was posted."
        ),
        data={
            "month": month,
            "counts": counts,
            "findings": [f.as_dict() for f in pack.sorted_findings()],
            "sections": pack.sections,
            "pack_path": str(written),
            "markdown": pack.markdown(),
        },
    )


def write_pack(pack: ClosePack, out_dir: Path) -> Path:
    """One folder per company per month, so a re-run overwrites rather than
    leaving two packs that disagree."""
    folder = out_dir / pack.company.replace("/", "-") / pack.month
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "close.md"
    path.write_text(pack.markdown(), encoding="utf-8")
    for name, content in pack.attachments.items():
        (folder / name).write_text(content, encoding="utf-8")
    return path


def as_csv(pack: ClosePack) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=["kind", "severity", "detail", "amount", "fix"]
    )
    writer.writeheader()
    writer.writerows(f.as_dict() for f in pack.sorted_findings())
    return buffer.getvalue()
