"""GSTR-1 as the JSON the GST portal and the offline tool accept.

A CA files GSTR-1 every month, and the slow part is not the totals but the
upload being rejected for a malformed invoice number or a GSTIN typo. So the
file is validated against the rules the portal applies before it is written,
and a month with problems produces a list of them instead of a file that the
portal would bounce three days before the due date.

The builder and the checks are pure functions over the per-voucher rows that
reports.gstr1_data already produces; only gstr1_export talks to Tally.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from tallyagent_core.models.voucher import PAISA
from tallyagent_core.validation import gstin as gstin_rules
from tallyagent_tools.base import ToolContext, ToolResult

#: The rates GST actually levies. A computed rate is snapped to one of these,
#: because a voucher's tax is rounded and 900.004 / 5000 is still 18%.
STANDARD_RATES = (
    Decimal("0"),
    Decimal("0.1"),
    Decimal("0.25"),
    Decimal("1"),
    Decimal("1.5"),
    Decimal("3"),
    Decimal("5"),
    Decimal("6"),
    Decimal("7.5"),
    Decimal("12"),
    Decimal("18"),
    Decimal("28"),
)

#: The portal allows letters, digits, '/' and '-' in an invoice number, at most
#: 16 characters. Tally allows far more, which is why this is checked here.
_INVOICE_NUMBER = re.compile(r"^[A-Za-z0-9/-]{1,16}$")
TOLERANCE = Decimal("1")


@dataclass(slots=True)
class Invoice:
    """One outward supply, normalised from a day-book row."""

    number: str
    date: str
    party: str
    ctin: str
    pos: str
    txval: Decimal
    iamt: Decimal
    camt: Decimal
    samt: Decimal
    total: Decimal
    hsn: str = ""
    rate: Decimal = field(default=Decimal("0"))

    @property
    def tax(self) -> Decimal:
        return self.iamt + self.camt + self.samt


def _dec(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def _num(value: Decimal) -> float:
    """The portal wants numbers, not strings, to the paisa."""
    return float(value.quantize(PAISA))


def _rate(txval: Decimal, tax: Decimal) -> Decimal:
    """The standard rate nearest to what the voucher actually charged."""
    if txval == 0:
        return Decimal("0")
    return min(STANDARD_RATES, key=lambda r: abs(txval * r / 100 - tax))


def _portal_date(iso: str) -> str:
    """Tally rows carry 2026-06-15; the portal reads 15-06-2026."""
    year, month, day = iso.split("-")
    return f"{day}-{month}-{year}"


def invoices(
    rows: list[dict[str, Any]], company_gstin: str
) -> list[Invoice]:
    """Normalise rows into invoices, deciding each one's place of supply.

    A registered buyer's place of supply is the state in its GSTIN. An
    unregistered buyer's is its stated state when the row has one, and
    otherwise the supplier's own state when the voucher charged CGST+SGST -
    which is the only reading of such a voucher that does not contradict it.
    """
    home = gstin_rules.state_code(company_gstin) if company_gstin else ""
    out: list[Invoice] = []
    for row in rows:
        ctin = str(row.get("gstin") or "").strip().upper()
        iamt, camt, samt = _dec(row.get("igst")), _dec(row.get("cgst")), _dec(row.get("sgst"))
        pos = str(row.get("pos") or "").strip()
        if ctin:
            pos = gstin_rules.state_code(ctin)
        elif not pos:
            pos = home if iamt == 0 else ""
        txval = _dec(row.get("taxable_value"))
        invoice = Invoice(
            number=str(row.get("reference") or row.get("voucher_number") or "").strip(),
            date=str(row.get("date") or ""),
            party=str(row.get("party") or ""),
            ctin=ctin,
            pos=pos,
            txval=txval,
            iamt=iamt,
            camt=camt,
            samt=samt,
            total=_dec(row.get("total")),
            hsn=str(row.get("hsn") or "").strip(),
        )
        invoice.rate = (
            _dec(row["rate"]) if row.get("rate") not in (None, "") else _rate(txval, invoice.tax)
        )
        out.append(invoice)
    return out


def validate(rows: list[dict[str, Any]], company_gstin: str) -> list[str]:
    """Everything the portal would reject, as sentences a CA can act on."""
    problems: list[str] = []
    if not gstin_rules.is_valid(company_gstin):
        problems.append(
            f"The company GSTIN {company_gstin or '(blank)'} is not a valid GSTIN; "
            "set it on the company before filing."
        )
    home = gstin_rules.state_code(company_gstin) if company_gstin else ""
    for inv in invoices(rows, company_gstin):
        label = f"Invoice {inv.number or '(no number)'} to {inv.party or '(no party)'}"
        if inv.ctin and not gstin_rules.is_valid(inv.ctin):
            problems.append(
                f"{label}: buyer GSTIN {inv.ctin} fails the format or checksum."
            )
        if not _INVOICE_NUMBER.match(inv.number):
            problems.append(
                f"{label}: the invoice number must be 1-16 letters, digits, "
                "'/' or '-'."
            )
        expected = inv.txval * inv.rate / 100
        if abs(expected - inv.tax) > TOLERANCE:
            problems.append(
                f"{label}: tax {inv.tax} is not {inv.rate}% of {inv.txval} "
                f"(expected {expected.quantize(PAISA)})."
            )
        if not inv.pos:
            problems.append(f"{label}: no place of supply - give the buyer a state.")
        elif inv.pos == home and inv.iamt != 0:
            problems.append(
                f"{label}: an intra-state supply must charge CGST+SGST, not IGST."
            )
        elif inv.pos != home and (inv.camt != 0 or inv.samt != 0):
            problems.append(
                f"{label}: an inter-state supply must charge IGST, not CGST+SGST."
            )
    return problems


def build_gstr1(
    rows: list[dict[str, Any]], company_gstin: str, period: str
) -> dict[str, Any]:
    """The GSTR-1 JSON for one return period (MMYYYY).

    Registered buyers become b2b invoices grouped by their GSTIN. Everyone
    else is aggregated into b2cs by place of supply, rate and supply type,
    because the portal takes small B2C supplies as totals, not invoices.
    """
    home = gstin_rules.state_code(company_gstin)
    b2b: dict[str, list[dict[str, Any]]] = defaultdict(list)
    b2cs: dict[tuple[str, Decimal, str], list[Decimal]] = defaultdict(
        lambda: [Decimal("0")] * 4
    )
    hsn: dict[tuple[str, Decimal], list[Decimal]] = defaultdict(
        lambda: [Decimal("0")] * 5
    )
    found = invoices(rows, company_gstin)
    for inv in found:
        if inv.ctin:
            b2b[inv.ctin].append(
                {
                    "inum": inv.number,
                    "idt": _portal_date(inv.date),
                    "val": _num(inv.total),
                    "pos": inv.pos,
                    "rchrg": "N",
                    "inv_typ": "R",
                    "itms": [
                        {
                            "num": 1,
                            "itm_det": {
                                "rt": _num(inv.rate),
                                "txval": _num(inv.txval),
                                "iamt": _num(inv.iamt),
                                "camt": _num(inv.camt),
                                "samt": _num(inv.samt),
                                "csamt": 0.0,
                            },
                        }
                    ],
                }
            )
        else:
            kind = "INTRA" if inv.pos == home else "INTER"
            bucket = b2cs[(inv.pos, inv.rate, kind)]
            for i, amount in enumerate((inv.txval, inv.iamt, inv.camt, inv.samt)):
                bucket[i] += amount
        if inv.hsn:
            line = hsn[(inv.hsn, inv.rate)]
            for i, amount in enumerate((inv.txval, inv.iamt, inv.camt, inv.samt, inv.total)):
                line[i] += amount

    payload: dict[str, Any] = {
        "gstin": company_gstin,
        "fp": period,
        "b2b": [
            {"ctin": ctin, "inv": sorted(invs, key=lambda i: i["inum"])}
            for ctin, invs in sorted(b2b.items())
        ],
        "b2cs": [
            {
                "sply_ty": kind,
                "pos": pos,
                "typ": "OE",
                "rt": _num(rate),
                "txval": _num(t[0]),
                "iamt": _num(t[1]),
                "camt": _num(t[2]),
                "samt": _num(t[3]),
                "csamt": 0.0,
            }
            for (pos, rate, kind), t in sorted(b2cs.items())
        ],
    }
    # A partial HSN summary is worse than none: the portal checks it against
    # the invoices, so it is only included when every supply carries a code.
    if found and all(inv.hsn for inv in found):
        payload["hsn"] = {
            "data": [
                {
                    "num": n,
                    "hsn_sc": code,
                    "rt": _num(rate),
                    "txval": _num(t[0]),
                    "iamt": _num(t[1]),
                    "camt": _num(t[2]),
                    "samt": _num(t[3]),
                    "csamt": 0.0,
                    "val": _num(t[4]),
                }
                for n, ((code, rate), t) in enumerate(sorted(hsn.items()), start=1)
            ]
        }
    return payload


def summary_markdown(
    company: str, month: str, payload: dict[str, Any] | None, problems: list[str]
) -> str:
    """The page a CA reads before uploading: what is in the file, or why not."""
    lines = [f"# GSTR-1 {month} - {company}", ""]
    if payload is not None:
        invoice_count = sum(len(p["inv"]) for p in payload["b2b"])
        by_rate: dict[float, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for party in payload["b2b"]:
            for inv in party["inv"]:
                det = inv["itms"][0]["itm_det"]
                by_rate[det["rt"]][0] += det["txval"]
                by_rate[det["rt"]][1] += det["iamt"] + det["camt"] + det["samt"]
        for row in payload["b2cs"]:
            by_rate[row["rt"]][0] += row["txval"]
            by_rate[row["rt"]][1] += row["iamt"] + row["camt"] + row["samt"]
        lines += [
            f"- B2B: {invoice_count} invoice(s) to {len(payload['b2b'])} registered buyer(s)",
            f"- B2CS: {len(payload['b2cs'])} summary row(s)",
            f"- HSN summary: {'included' if 'hsn' in payload else 'not available'}",
            "",
            "| Rate | Taxable value | Tax |",
            "|---:|---:|---:|",
        ]
        lines += [
            f"| {rate:g}% | {txval:.2f} | {tax:.2f} |"
            for rate, (txval, tax) in sorted(by_rate.items())
        ]
        lines.append("")
    lines.append("## Problems")
    lines += [f"- {p}" for p in problems] or ["- None. The JSON is ready to upload."]
    if problems:
        lines += ["", "No JSON was written: fix these in Tally and run it again."]
    return "\n".join(lines) + "\n"


async def gstr1_export(
    ctx: ToolContext, month: str, out_dir: str = "reports/gstr1"
) -> ToolResult:
    """Write the month's GSTR-1 JSON and its summary, or the reasons not to.

    Reads only. The buyer GSTINs come from the ledger masters because the day
    book does not carry them, and a sale without one is a B2C supply.
    """
    from tallyagent_tools import reports
    from tallyagent_tools.close import month_dates

    from_date, to_date = month_dates(month)
    book = await reports.gstr1_data(ctx, from_date=from_date, to_date=to_date)
    masters = await ctx.masters()
    by_name = {p.name: p for p in masters.parties}
    rows = []
    for row in book.data:
        party = by_name.get(row["party"])
        rows.append(
            {
                **row,
                "gstin": (party.gstin or "") if party else "",
                "pos": (party.state_code or "") if party else "",
            }
        )

    company_gstin = (ctx.company.gstin or "").strip().upper()
    problems = validate(rows, company_gstin)
    payload = None if problems else build_gstr1(
        rows, company_gstin, f"{to_date.month:02d}{to_date.year}"
    )

    folder = Path(out_dir) / ctx.company.name.replace("/", "-")
    folder.mkdir(parents=True, exist_ok=True)
    json_path = folder / f"{month}.json"
    if payload is not None:
        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    elif json_path.exists():
        # A stale file from an earlier clean run must not be uploaded by mistake.
        json_path.unlink()
    summary_path = folder / f"{month}.md"
    summary_path.write_text(
        summary_markdown(ctx.company.name, month, payload, problems), encoding="utf-8"
    )

    if problems:
        message = (
            f"GSTR-1 {month}: {len(problems)} problem(s), no JSON written. "
            f"See {summary_path}."
        )
    else:
        message = (
            f"GSTR-1 {month}: {len(rows)} supply voucher(s) written to {json_path}."
        )
    return ToolResult(
        message=message,
        data={
            "json": payload,
            "problems": problems,
            "json_path": str(json_path) if payload is not None else "",
            "summary_path": str(summary_path),
        },
    )
