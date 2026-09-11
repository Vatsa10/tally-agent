"""Seed the demo company with a few months of plausible activity.

Runs against the fake Tally, so it is safe by construction: it cannot touch a
real company. Use it to have something to look at in the UI.

    uv run python scripts/seed_demo_company.py
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

from tallyagent_core.idempotency import make_key
from tallyagent_core.models import Voucher, VoucherLine, VoucherType
from tallyagent_tally.backend import TallyBackend
from tallyagent_tally.client import TallyClient, TallyConfig
from tallyagent_tally.fake_server import seeded_demo

COMPANY = "Demo Traders Pvt Ltd"


def sale(day: date, reference: str, taxable: str) -> Voucher:
    value = Decimal(taxable)
    tax = (value * Decimal("0.09")).quantize(Decimal("0.01"))
    return Voucher(
        voucher_type=VoucherType.SALES,
        date=day,
        party_name="Acme Industries",
        reference=reference,
        narration=f"Sale {reference}",
        lines=[
            VoucherLine(ledger_name="Acme Industries", amount=value + tax * 2),
            VoucherLine(ledger_name="Sales - GST 18%", amount=-value),
            VoucherLine(ledger_name="Output CGST", amount=-tax),
            VoucherLine(ledger_name="Output SGST", amount=-tax),
        ],
    )


def purchase(day: date, reference: str, taxable: str) -> Voucher:
    value = Decimal(taxable)
    igst = (value * Decimal("0.18")).quantize(Decimal("0.01"))
    return Voucher(
        voucher_type=VoucherType.PURCHASE,
        date=day,
        party_name="Bharat Supplies",
        reference=reference,
        narration=f"Purchase {reference}",
        lines=[
            VoucherLine(ledger_name="Bharat Supplies", amount=-(value + igst)),
            VoucherLine(ledger_name="Purchase - GST 18%", amount=value),
            VoucherLine(ledger_name="Input IGST", amount=igst),
        ],
    )


def receipt(day: date, amount: str) -> Voucher:
    value = Decimal(amount)
    return Voucher(
        voucher_type=VoucherType.RECEIPT,
        date=day,
        party_name="Acme Industries",
        narration="NEFT ACME INDS",
        lines=[
            VoucherLine(ledger_name="Bank - HDFC 1234", amount=value),
            VoucherLine(ledger_name="Acme Industries", amount=-value),
        ],
    )


VOUCHERS = [
    sale(date(2026, 4, 5), "INV-001", "50000.00"),
    sale(date(2026, 5, 12), "INV-002", "35000.00"),
    sale(date(2026, 6, 15), "INV-003", "10000.00"),
    purchase(date(2026, 4, 10), "BS/2026/41", "5000.00"),
    purchase(date(2026, 5, 18), "BS/2026/42", "10000.00"),
    receipt(date(2026, 4, 20), "59000.00"),
    receipt(date(2026, 6, 2), "41300.00"),
]


async def seed() -> None:
    tally = seeded_demo(COMPANY)
    backend = TallyBackend(
        TallyClient(
            TallyConfig(host="127.0.0.1", company=COMPANY), transport=tally.transport
        )
    )
    for voucher in VOUCHERS:
        result = await backend.create_voucher(voucher, make_key(COMPANY, voucher))
        status = "ok" if result.ok else f"FAILED: {'; '.join(result.errors)}"
        print(f"{voucher.voucher_type.value:<9} {voucher.date} {voucher.amount:>12}  {status}")

    print("\nTrial balance:")
    for name, balance in sorted((await backend.trial_balance()).items()):
        if balance:
            print(f"  {name:<24}{balance:>14}")


if __name__ == "__main__":
    asyncio.run(seed())
