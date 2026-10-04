"""Input tax credit: reconcile GSTR-2B against the books, and say what to do.

Every practice does this monthly and it is the work that costs money when it is
wrong: ITC claimed above what GSTR-2B shows draws a DRC-01C notice (Rule 88D),
and ITC never claimed is simply lost once the section 16(4) deadline passes.
Tools that list mismatches exist. What a CA's article spends the hours on is the
next step - *why* each one is a mismatch, what to do about it, who to chase -
and that is what this does.

Each portal and book invoice is matched on **supplier GSTIN plus invoice
number**, never on the invoice number alone: small suppliers number their bills
1, 2, 3, and two suppliers' "12" are different bills. Then each one gets a cause
in words, the rupees of credit at stake, and an action:

- **matched** - claim it; accept it in IMS.
- **supplier has not filed** - in the books, not in 2B. The credit is deferred
  until they file; a follow-up to the supplier is drafted, and the invoice goes
  on the deferred-ITC ledger so it is claimed the month it does appear.
- **bill not entered** - in 2B, not in the books. Either a bill nobody keyed in,
  or one that is not this client's.
- **ITC not available** - 2B itself says the credit is not available (place of
  supply, section 17(5), late filing). Do not claim it.
- **value or tax difference** - the two sides disagree beyond a rupee; the
  explanation says which side and by how much.
- **GSTIN mismatch** - the same invoice from the same supplier name, under a
  different GSTIN in the books. The ledger is wrong, and the credit will never
  match until it is fixed.

Nothing here posts and nothing here touches the GST portal. IMS actions are
*suggestions* with a reason; accepting or rejecting on the portal stays a
person's act, because under IMS doing nothing is itself a decision.
"""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

PAISA = Decimal("0.01")

#: A rupee either way is rounding, not a mismatch worth a person's time.
TOLERANCE = Decimal("1.00")

#: How far back in the books to look for a bill a supplier reported late.
LOOKBACK = timedelta(days=120)

MATCHED = "matched"
NOT_FILED = "supplier_not_filed"
NOT_IN_BOOKS = "bill_not_entered"
ITC_UNAVAILABLE = "itc_not_available"
VALUE_DIFF = "value_difference"
TAX_DIFF = "tax_difference"
GSTIN_MISMATCH = "gstin_mismatch"

CAUSE_WORDS = {
    MATCHED: "matched",
    NOT_FILED: "supplier has not filed",
    NOT_IN_BOOKS: "bill not entered in the books",
    ITC_UNAVAILABLE: "2B says ITC is not available",
    VALUE_DIFF: "taxable value differs",
    TAX_DIFF: "tax differs",
    GSTIN_MISMATCH: "supplier GSTIN differs between books and 2B",
}


def _money(raw: Any) -> Decimal:
    try:
        return Decimal(str(raw if raw not in (None, "") else 0)).quantize(PAISA)
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def _date(raw: Any) -> date | None:
    text = str(raw or "").strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def invoice_key(number: str) -> str:
    """An invoice number reduced to what identifies it.

    "INV/001", "inv-001" and "INV 1" are the same bill typed three ways; the
    leading zeros of the numeric part are not part of its identity either.
    """
    text = re.sub(r"[^A-Za-z0-9]", "", str(number or "")).upper()
    return re.sub(r"(?<![0-9])0+(?=[0-9])", "", text)


def _letters(name: str) -> str:
    return "".join(ch for ch in str(name or "").lower() if ch.isalnum())


@dataclass(slots=True)
class Invoice:
    """One inward supply, from either side."""

    supplier_gstin: str
    supplier: str
    number: str
    invoice_date: date | None
    taxable: Decimal
    igst: Decimal = Decimal("0.00")
    cgst: Decimal = Decimal("0.00")
    sgst: Decimal = Decimal("0.00")
    cess: Decimal = Decimal("0.00")
    total: Decimal = Decimal("0.00")
    #: Portal only: is the credit available, and if not, the portal's reason.
    itc_available: bool = True
    reason: str = ""
    #: Portal only: the period the supplier filed in, when it says.
    filed_period: str = ""
    voucher_number: str = ""

    @property
    def tax(self) -> Decimal:
        return (self.igst + self.cgst + self.sgst + self.cess).quantize(PAISA)

    @property
    def key(self) -> tuple[str, str]:
        return (self.supplier_gstin.upper().strip(), invoice_key(self.number))


def parse_2b(document: dict[str, Any]) -> tuple[str, list[Invoice]]:
    """The return period and the B2B invoices in a GSTR-2B download.

    Reads the portal's own GSTR-2B shape - invoice date in ``dt``, tax in
    ``igst``/``cgst``/``sgst``/``cess``, availability in ``itcavl`` - and also
    the GSTR-1 style (``idt``, ``iamt``/``camt``/``samt``) some tools export.
    The first version read only the second, so a real portal file came back
    with every tax as zero and every date blank.
    """
    data = document.get("data", document)
    period = str(data.get("rtnprd") or document.get("rtnprd") or "")
    docdata = data.get("docdata") or document.get("docdata") or {}
    invoices: list[Invoice] = []
    for supplier in docdata.get("b2b") or []:
        gstin = str(supplier.get("ctin") or "").strip()
        name = str(supplier.get("trdnm") or "").strip()
        filed = str(supplier.get("supprd") or "")
        for inv in supplier.get("inv") or []:
            items = inv.get("items") or inv.get("itms") or []
            taxable = igst = cgst = sgst = cess = Decimal("0.00")
            for item in items:
                detail = item.get("itm_det", item)
                taxable += _money(detail.get("txval"))
                igst += _money(detail.get("igst", detail.get("iamt")))
                cgst += _money(detail.get("cgst", detail.get("camt")))
                sgst += _money(detail.get("sgst", detail.get("samt")))
                cess += _money(detail.get("cess", detail.get("csamt")))
            invoices.append(
                Invoice(
                    supplier_gstin=gstin,
                    supplier=name,
                    number=str(inv.get("inum") or ""),
                    invoice_date=_date(inv.get("dt") or inv.get("idt")),
                    taxable=taxable,
                    igst=igst,
                    cgst=cgst,
                    sgst=sgst,
                    cess=cess,
                    total=_money(inv.get("val") or (taxable + igst + cgst + sgst + cess)),
                    itc_available=str(inv.get("itcavl") or "Y").upper() != "N",
                    reason=str(inv.get("rsn") or ""),
                    filed_period=filed,
                )
            )
    return period, invoices


def book_invoices(register_rows: list[dict[str, Any]], gstins: dict[str, str]) -> list[Invoice]:
    """The purchase register as invoices, with each party's GSTIN looked up.

    ``gstins`` maps a party ledger to its GSTIN, from the masters. A party
    with no GSTIN in the books can still match on its name below.
    """
    out = []
    for row in register_rows:
        party = str(row.get("party") or "")
        out.append(
            Invoice(
                supplier_gstin=gstins.get(party, ""),
                supplier=party,
                number=str(row.get("reference") or row.get("voucher_number") or ""),
                invoice_date=_date(row.get("date")),
                taxable=_money(row.get("taxable_value")),
                igst=_money(row.get("igst")),
                cgst=_money(row.get("cgst")),
                sgst=_money(row.get("sgst")),
                total=_money(row.get("total")),
                voucher_number=str(row.get("voucher_number") or ""),
            )
        )
    return out


@dataclass(slots=True)
class Finding:
    cause: str
    supplier: str
    supplier_gstin: str
    number: str
    invoice_date: date | None
    portal: Invoice | None
    books: Invoice | None
    #: Credit that cannot be claimed this month because of this finding.
    itc_at_risk: Decimal
    explanation: str
    action: str
    ims: str = ""
    ims_reason: str = ""

    def as_row(self) -> dict[str, str]:
        return {
            "cause": CAUSE_WORDS.get(self.cause, self.cause),
            "supplier": self.supplier,
            "supplier_gstin": self.supplier_gstin,
            "invoice_no": self.number,
            "invoice_date": self.invoice_date.isoformat() if self.invoice_date else "",
            "taxable_2b": format(self.portal.taxable, "f") if self.portal else "",
            "tax_2b": format(self.portal.tax, "f") if self.portal else "",
            "taxable_books": format(self.books.taxable, "f") if self.books else "",
            "tax_books": format(self.books.tax, "f") if self.books else "",
            "itc_at_risk": format(self.itc_at_risk, "f"),
            "explanation": self.explanation,
            "action": self.action,
            "ims": self.ims,
        }


@dataclass(slots=True)
class Reconciliation:
    period: str
    findings: list[Finding] = field(default_factory=list)

    def by(self, cause: str) -> list[Finding]:
        return [f for f in self.findings if f.cause == cause]

    @property
    def claimable(self) -> Decimal:
        return sum((f.portal.tax for f in self.by(MATCHED) if f.portal), Decimal("0.00"))

    @property
    def at_risk(self) -> Decimal:
        return sum((f.itc_at_risk for f in self.findings), Decimal("0.00"))

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for finding in self.findings:
            out[finding.cause] += 1
        return dict(out)

    def summary(self) -> str:
        counts = self.counts()
        parts = [f"{n} {CAUSE_WORDS.get(c, c)}" for c, n in sorted(counts.items())]
        return (
            f"GSTR-2B {self.period}: {', '.join(parts) or 'nothing to compare'}. "
            f"Claimable Rs {self.claimable:,.2f}; at risk Rs {self.at_risk:,.2f}."
        )

    def csv(self) -> str:
        buffer = io.StringIO()
        rows = [f.as_row() for f in self.findings]
        if rows:
            writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return buffer.getvalue()


def period_dates(period: str) -> tuple[date, date] | None:
    """MMYYYY, as the first and last day of that month."""
    if len(period) != 6 or not period.isdigit():
        return None
    first = date(int(period[2:]), int(period[:2]), 1)
    following = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return first, following - timedelta(days=1)


def reconcile(period: str, portal: list[Invoice], books: list[Invoice]) -> Reconciliation:
    """Match, classify, and say what to do about every invoice on either side.

    ``books`` may reach back several months, because a supplier can report a
    bill late and 2B then carries an old invoice date. But a bill from another
    month that is still unmatched is that month's business - it was reported as
    unfiled in its own reconciliation and sits on the deferred ledger - so it is
    not reported again here, where it would count the same credit twice.
    """
    result = Reconciliation(period=period)
    own = period_dates(period)

    by_key: dict[tuple[str, str], Invoice] = {}
    by_name: dict[tuple[str, str], Invoice] = {}
    for entry in books:
        if entry.supplier_gstin:
            by_key[entry.key] = entry
        by_name[(_letters(entry.supplier), invoice_key(entry.number))] = entry
    used: set[int] = set()

    for inv in portal:
        bill: Invoice | None = by_key.get(inv.key)
        gstin_differs = False
        if bill is None:
            # The same invoice from a supplier with the same name, under a
            # different (or no) GSTIN in the books.
            candidate = by_name.get((_letters(inv.supplier), invoice_key(inv.number)))
            if candidate is not None and id(candidate) not in used:
                bill = candidate
                gstin_differs = bool(candidate.supplier_gstin) and (
                    candidate.supplier_gstin.upper() != inv.supplier_gstin.upper()
                )
        if bill is not None:
            used.add(id(bill))
        result.findings.append(_classify(inv, bill, gstin_differs))

    for entry in books:
        if id(entry) in used:
            continue
        if own and entry.invoice_date and not (own[0] <= entry.invoice_date <= own[1]):
            continue
        result.findings.append(
            Finding(
                cause=NOT_FILED,
                supplier=entry.supplier,
                supplier_gstin=entry.supplier_gstin,
                number=entry.number,
                invoice_date=entry.invoice_date,
                portal=None,
                books=entry,
                itc_at_risk=entry.tax,
                explanation=(
                    f"In the books (voucher {entry.voucher_number or '?'}) but not in "
                    f"GSTR-2B for {period}: the supplier has not reported it, or "
                    "reported it under another GSTIN or number."
                ),
                action=(
                    "Do not claim it this month. Ask the supplier to file it in GSTR-1 "
                    "or IFF; it goes on the deferred-ITC list and is claimable the "
                    "month it appears."
                ),
            )
        )

    order = {ITC_UNAVAILABLE: 0, GSTIN_MISMATCH: 1, NOT_FILED: 2, TAX_DIFF: 3,
             VALUE_DIFF: 4, NOT_IN_BOOKS: 5, MATCHED: 9}
    result.findings.sort(key=lambda f: (order.get(f.cause, 8), -f.itc_at_risk))
    return result


def _classify(inv: Invoice, bill: Invoice | None, gstin_differs: bool) -> Finding:
    def finding(cause: str, at_risk: Decimal, explanation: str, action: str,
                ims: str, ims_reason: str) -> Finding:
        return Finding(
            cause=cause,
            supplier=inv.supplier,
            supplier_gstin=inv.supplier_gstin,
            number=inv.number,
            invoice_date=inv.invoice_date,
            portal=inv,
            books=bill,
            itc_at_risk=at_risk,
            explanation=explanation,
            action=action,
            ims=ims,
            ims_reason=ims_reason,
        )

    if not inv.itc_available:
        reason = f" ({inv.reason})" if inv.reason else ""
        return finding(
            ITC_UNAVAILABLE, inv.tax,
            f"GSTR-2B marks the credit on this invoice as not available{reason}.",
            "Do not claim it. If it was claimed earlier, reverse it.",
            "accept", "the supply is genuine; only the credit is barred",
        )

    if bill is None:
        return finding(
            NOT_IN_BOOKS, Decimal("0.00"),
            f"{inv.supplier} reported this in GSTR-2B, but there is no purchase "
            "with this number in the books.",
            "Find the bill and enter it, or confirm it is not this client's.",
            "pending", "until the bill is found; rejecting a genuine bill loses the credit",
        )

    if gstin_differs:
        return finding(
            GSTIN_MISMATCH, inv.tax,
            f"The books have {bill.supplier} under GSTIN {bill.supplier_gstin}; "
            f"GSTR-2B has {inv.supplier_gstin}.",
            "Correct the GSTIN on the supplier ledger; until then this will never match.",
            "accept", "the invoice is genuine; the books are what needs fixing",
        )

    tax_gap = inv.tax - bill.tax
    value_gap = inv.taxable - bill.taxable
    if abs(value_gap) > TOLERANCE:
        return finding(
            VALUE_DIFF, max(Decimal("0.00"), bill.tax - inv.tax),
            f"Taxable value is Rs {inv.taxable:,.2f} in 2B and Rs {bill.taxable:,.2f} "
            f"in the books ({'+' if value_gap > 0 else ''}{value_gap:,.2f}).",
            "Check the bill: a freight or discount line, or a typing error, on one side.",
            "pending", "until the value is agreed with the supplier",
        )
    if abs(tax_gap) > TOLERANCE:
        return finding(
            TAX_DIFF, max(Decimal("0.00"), bill.tax - inv.tax),
            f"Tax is Rs {inv.tax:,.2f} in 2B and Rs {bill.tax:,.2f} in the books - a "
            "different rate, or IGST against CGST+SGST.",
            "Claim no more than 2B shows; correct whichever side has the wrong rate.",
            "pending", "until the rate is agreed",
        )
    return finding(
        MATCHED, Decimal("0.00"),
        "Matches the books.",
        "Claim it.",
        "accept", "matches the books",
    )


# --- what to send, and what to do in IMS -------------------------------------


def followups(result: Reconciliation, firm: str = "", client: str = "") -> dict[str, str]:
    """A message per supplier who has not filed, ready to send.

    Polite, specific and short: the invoices by number, date and tax, and the
    one thing being asked for. Chasing suppliers is the most tedious part of
    the month and the part most often left undone.
    """
    by_supplier: dict[tuple[str, str], list[Finding]] = defaultdict(list)
    for finding in result.by(NOT_FILED):
        by_supplier[(finding.supplier, finding.supplier_gstin)].append(finding)

    messages: dict[str, str] = {}
    for (supplier, gstin), items in sorted(by_supplier.items()):
        total = sum((f.itc_at_risk for f in items), Decimal("0.00"))
        lines = [
            f"Dear {supplier},",
            "",
            f"The following invoice(s) you raised on {client or 'us'} do not appear in "
            f"our GSTR-2B for {_period_words(result.period)}"
            + (f" against your GSTIN {gstin}" if gstin else "")
            + ":",
            "",
        ]
        for f in items:
            when = f.invoice_date.strftime("%d-%m-%Y") if f.invoice_date else "?"
            lines.append(f"  - {f.number} dated {when}, GST Rs {f.itc_at_risk:,.2f}")
        lines += [
            "",
            f"That is Rs {total:,.2f} of input tax credit we cannot claim until they "
            "are reported. Please include them in your next GSTR-1 / IFF, or let us "
            "know if any were reported under a different number or GSTIN.",
            "",
            "Regards,",
            firm or "Accounts",
        ]
        messages[supplier] = "\n".join(lines)
    return messages


def ims_actions(result: Reconciliation) -> list[dict[str, str]]:
    """What to do with each 2B invoice in IMS, and why. Suggestions only."""
    return [
        {
            "supplier_gstin": f.supplier_gstin,
            "supplier": f.supplier,
            "invoice_no": f.number,
            "suggestion": f.ims,
            "reason": f.ims_reason,
        }
        for f in result.findings
        if f.portal is not None and f.ims
    ]


def _period_words(period: str) -> str:
    if len(period) == 6 and period.isdigit():
        return datetime.strptime(period, "%m%Y").strftime("%B %Y")
    return period


def section_16_4_deadline(invoice_date: date) -> date:
    """The last day credit on this invoice can be claimed: 30 November after
    the end of its financial year."""
    fy_end_year = invoice_date.year + (1 if invoice_date.month >= 4 else 0)
    return date(fy_end_year, 11, 30)
