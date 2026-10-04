"""Capture the data behind the film's two worked stories, from the product itself.

**A bill becomes a voucher.** The scanned bill, where each field sits on it
(found by OCR on the image, not drawn by hand), what was read, and the voucher
the agent drafted from it - the real queued ticket, with its validation.

**Debits and credits matched.** A June bank statement against the bank ledger
in Tally, and the supplier's GSTR-2B against the purchase register, run through
the same reconciliation tools the month-end close uses. Needs TallyPrime up;
without it the bill story is still refreshed and the last reco is kept.

    uv run python scripts/film_stories.py
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import shutil
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any

BILL = Path("tests/fixtures/bill_pile/bill-001.png")
TICKET = "APR-0010"
STATEMENT = Path("tests/fixtures/film_june_statement.csv")
GSTR2B = Path("tests/fixtures/live_gstr2b.json")
OUT = Path("film/data/stories.json")
JUNE = (date(2026, 6, 1), date(2026, 6, 30))
#: Statement lines with no voucher behind them yet: what the bank saw and the
#: books did not. The rest of the statement is the bank side of entries that
#: are in the books.
BANK_ONLY = [
    ("27/06/2026", "NEFT ACME INDUSTRIES PART PMT", "", "25000.00", "N0627"),
    ("30/06/2026", "BANK CHARGES QTR APR-JUN", "236.00", "", ""),
]


def _digits(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.lower())


def bill_story() -> dict[str, Any]:
    from tallyagent_agent.fallback.tally_ui import _ocr_boxes

    read = json.loads(BILL.with_suffix(".json").read_text(encoding="utf-8"))
    png = BILL.read_bytes()
    lines = _ocr_boxes(png)
    wanted = {
        "vendor": read["vendor"],
        "gstin": read["gstin"],
        "invoice_no": read["invoice_no"],
        "invoice_date": read["invoice_date"],
        "taxable_value": f"{read['taxable_value']:,.2f}",
        "igst": f"{read['igst']:,.2f}",
        "total": f"{read['total']:,.2f}",
    }
    dates = {read["invoice_date"], "/".join(reversed(read["invoice_date"].split("-")))}
    fields = []
    for name, value in wanted.items():
        keys = [_digits(v) for v in (dates if name == "invoice_date" else {value})]
        hits = [box for text, box in lines if any(k and k in _digits(text) for k in keys)]
        if not hits:
            print(f"  {name}: not found on the image", file=sys.stderr)
            continue
        # The value nearest the bottom right wins for amounts (the totals
        # block); the first for everything else.
        box = max(hits, key=lambda b: b[1]) if name in ("total",) else hits[0]
        fields.append({"field": name, "value": value, "box": [round(v) for v in box]})

    db = sqlite3.connect("data/demo.db")
    row = db.execute(
        "select summary, voucher_json, validation_json from approvals where ticket=?", (TICKET,)
    ).fetchone()
    if row is None:
        sys.exit(f"{TICKET} is not in data/demo.db")
    voucher = json.loads(row[1])
    checks = json.loads(row[2]).get("results", [])

    from PIL import Image

    size = Image.open(io.BytesIO(png)).size
    Path("film/data").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BILL, "film/data/bill.png")
    return {
        "image": "data/bill.png",
        "size": list(size),
        "fields": fields,
        "ticket": TICKET,
        "summary": row[0],
        "lines": [
            {
                "ledger": line["ledger_name"],
                "side": "Dr" if line["is_debit"] else "Cr",
                "amount": f"{abs(float(line['amount'])):,.2f}",
            }
            for line in voucher["lines"]
        ],
        "checks": [
            {"rule": c["rule"], "passed": c["passed"], "message": c["message"]} for c in checks
        ],
    }


async def reco_story() -> dict[str, Any] | None:
    from tallyagent_core import dotenv
    from tallyagent_daemon import clients, wiring
    from tallyagent_daemon import config as config_mod
    from tallyagent_tools import ingest, reconcile

    dotenv.load()
    base = config_mod.load("config/config.toml", "config/policy.toml")
    register = clients.load("config/clients.toml")
    wired = wiring.build(clients.apply(base, register.get("demo"), register.data_dir))
    ctx = wired.services.tools
    try:
        entries = await wired.backend.get_bank_ledger(
            "Bank - HDFC 1234", JUNE[0], JUNE[1], ctx.company.name
        )
    except Exception as exc:  # noqa: BLE001 - Tally down: keep the last reco
        print(f"Tally not reachable ({type(exc).__name__}); reco not refreshed", file=sys.stderr)
        return None

    if not STATEMENT.is_file():
        # The bank's side of what the books already hold, plus the lines only
        # the bank knows about. Written once, then a fixture like any other.
        buffer = io.StringIO()
        out = csv.writer(buffer, lineterminator="\n")
        out.writerow(["Date", "Narration", "Debit", "Credit", "Reference"])
        for e in entries:
            amount = float(e.amount)
            party = "" if e.particulars.startswith("Bank") else e.particulars
            side = "CR" if amount > 0 else "DR"
            narration = (e.narration or f"NEFT {side} {party}".strip()).upper()[:40]
            out.writerow([
                e.date.strftime("%d/%m/%Y"), narration,
                f"{-amount:.2f}" if amount < 0 else "", f"{amount:.2f}" if amount > 0 else "",
                f"V{e.voucher_number}",
            ])
        out.writerows(BANK_ONLY)
        STATEMENT.write_text(buffer.getvalue(), encoding="utf-8")

    rows = ingest.parse_bank_statement_csv(STATEMENT.read_text(encoding="utf-8"))
    bank = await reconcile.bank_reco(ctx, rows, from_date=JUNE[0], to_date=JUNE[1])
    gst = await reconcile.gstr2b_vs_purchase_register(
        ctx, json.loads(GSTR2B.read_text(encoding="utf-8")), from_date=JUNE[0], to_date=JUNE[1]
    )
    return {
        "bank": {"message": bank.message, **bank.data, "statement": rows},
        "gstr2b": {"message": gst.message, **(gst.data if isinstance(gst.data, dict) else {})},
    }


def main() -> None:
    previous = json.loads(OUT.read_text(encoding="utf-8")) if OUT.is_file() else {}
    story = {"bill": bill_story()}
    print(f"bill: {len(story['bill']['fields'])} fields located, {story['bill']['ticket']}")
    reco = asyncio.run(reco_story())
    if reco is None:
        reco = previous.get("reco")
    else:
        print("bank:", reco["bank"]["message"])
        print("2B:", reco["gstr2b"]["message"])
    story["reco"] = reco
    OUT.write_text(json.dumps(story, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
