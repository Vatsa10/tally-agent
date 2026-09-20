"""Draw a pile of purchase bills to work on.

    uv run python scripts/make_bill_pile.py --count 40 --out demo/bills

Real bills are photographs of paper from small vendors, so these are drawn
rather than exported: slightly different layouts, a couple with a vendor that is
not in the chart of accounts, and one that is deliberately unreadable. A pile
that is uniformly clean proves nothing about a product whose whole claim is
handling the eight out of a hundred that are not.

Each bill gets a ``.json`` beside it holding what a correct extraction would
be, so the batch can be exercised - and demonstrated - without paying for a
vision call per document.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import date
from decimal import Decimal
from pathlib import Path

#: Vendors that exist in the demo company, and two that do not.
KNOWN = ["Bharat Supplies"]
UNKNOWN = ["Sharma Trading Co", "Gupta Transport"]

ITEMS = [
    ("Widget", 2500),
    ("Gadget", 1200),
    ("Packing material", 450),
    ("Freight inward", 800),
    ("Printing and stationery", 320),
]

INK = "#1a1a1a"
PAPER = "#fbfbf7"
RULE = "#c9c9c2"


def _font(size: int, bold: bool = False):  # type: ignore[no-untyped-def]
    from PIL import ImageFont

    for name in (("arialbd.ttf",) if bold else ("arial.ttf", "segoeui.ttf")):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def draw_bill(
    path: Path, vendor: str, number: str, when: date, item: str, qty: int, rate: int
) -> dict[str, object]:
    from PIL import Image, ImageDraw

    taxable = Decimal(qty * rate)
    # Every demo vendor is in Maharashtra against a Gujarat company, so the tax
    # is integrated - the same split the agent has to work out for itself.
    igst = (taxable * Decimal("0.18")).quantize(Decimal("0.01"))
    total = taxable + igst

    image = Image.new("RGB", (1000, 1300), PAPER)
    pen = ImageDraw.Draw(image)

    pen.text((60, 50), vendor, font=_font(40, bold=True), fill=INK)
    pen.text((60, 105), "27AAPFU0939F1ZV  |  Maharashtra", font=_font(20), fill=INK)
    pen.line([(60, 150), (940, 150)], fill=RULE, width=2)

    pen.text((60, 185), "TAX INVOICE", font=_font(26, bold=True), fill=INK)
    pen.text((60, 230), f"Invoice no.  {number}", font=_font(22), fill=INK)
    pen.text((60, 262), f"Date         {when.strftime('%d-%m-%Y')}", font=_font(22), fill=INK)
    pen.text((60, 294), "Buyer        TA-Demo Traders", font=_font(22), fill=INK)

    pen.line([(60, 360), (940, 360)], fill=RULE, width=2)
    pen.text((60, 380), "Description", font=_font(22, bold=True), fill=INK)
    pen.text((560, 380), "Qty", font=_font(22, bold=True), fill=INK)
    pen.text((660, 380), "Rate", font=_font(22, bold=True), fill=INK)
    pen.text((820, 380), "Amount", font=_font(22, bold=True), fill=INK)
    pen.line([(60, 415), (940, 415)], fill=RULE, width=1)

    pen.text((60, 440), item, font=_font(22), fill=INK)
    pen.text((560, 440), str(qty), font=_font(22), fill=INK)
    pen.text((660, 440), f"{rate:,.2f}", font=_font(22), fill=INK)
    pen.text((820, 440), f"{taxable:,.2f}", font=_font(22), fill=INK)

    pen.line([(560, 520), (940, 520)], fill=RULE, width=1)
    pen.text((600, 540), "Taxable", font=_font(22), fill=INK)
    pen.text((820, 540), f"{taxable:,.2f}", font=_font(22), fill=INK)
    pen.text((600, 575), "IGST 18%", font=_font(22), fill=INK)
    pen.text((820, 575), f"{igst:,.2f}", font=_font(22), fill=INK)
    pen.line([(560, 610), (940, 610)], fill=RULE, width=2)
    pen.text((600, 630), "Total", font=_font(24, bold=True), fill=INK)
    pen.text((820, 630), f"{total:,.2f}", font=_font(24, bold=True), fill=INK)

    pen.text((60, 1180), "E. & O.E.   Subject to Mumbai jurisdiction", font=_font(18), fill=RULE)

    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)

    return {
        "vendor": vendor,
        "gstin": "27AAPFU0939F1ZV",
        "invoice_no": number,
        "invoice_date": when.isoformat(),
        "line_items": [{"description": item, "amount": float(taxable)}],
        "taxable_value": float(taxable),
        "cgst": 0,
        "sgst": 0,
        "igst": float(igst),
        "total": float(total),
        "place_of_supply": "27",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument("--out", default="demo/bills")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--strangers",
        type=int,
        default=3,
        help="How many bills come from a vendor with no ledger.",
    )
    args = parser.parse_args()

    random.seed(args.seed)
    out = Path(args.out)
    for existing in out.glob("*"):
        existing.unlink()

    #: EDU-legal dates. Tally Educational refuses anything else.
    days = [date(2026, 6, 1), date(2026, 6, 2)]

    for index in range(1, args.count + 1):
        stranger = index > args.count - args.strangers
        vendor = random.choice(UNKNOWN) if stranger else random.choice(KNOWN)
        item, rate = random.choice(ITEMS)
        extraction = draw_bill(
            out / f"bill-{index:03d}.png",
            vendor=vendor,
            number=f"{'ST' if stranger else 'BS'}/2026/{index:03d}",
            when=random.choice(days),
            item=item,
            qty=random.randint(1, 6),
            rate=rate,
        )
        (out / f"bill-{index:03d}.json").write_text(
            json.dumps(extraction, indent=2), encoding="utf-8"
        )

    # One that cannot be read at all, because a real pile always has one.
    (out / f"bill-{args.count + 1:03d}.png").write_bytes(b"\x89PNG\r\n\x1a\ntorn")

    print(f"  {args.count} bill(s) in {out}")
    print(f"  {args.strangers} from a vendor with no ledger, 1 unreadable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
