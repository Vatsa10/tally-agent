"""Put the demo company in the state the video's narration describes.

Filming against live books means the books have to be true. Two things drift
between takes: stock goes negative because every rehearsal sells another two
Widgets, and the approval queue fills with tickets nobody decided. Both make the
recording say something false - a stock summary flashing a negative-stock
warning while the narrator talks about reading Tally live is exactly the sort of
detail that loses a room.

    uv run python scripts/demo_prep.py

Idempotent: run it before every take.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

from tallyagent_daemon import config as config_mod
from tallyagent_daemon import wiring
from tallyagent_tools import stock, vouchers
from tallyagent_tools.executor import execute

#: Enough headroom that a demo can sell two Widgets several takes running.
TARGET_ON_HAND = Decimal("40")
RESTOCK_DATE = date(2026, 6, 1)  # an EDU-legal date


async def main() -> int:
    config = config_mod.load("config/config.toml", "config/policy.toml")
    wired = wiring.build(config)
    ctx = wired.services.tools
    company = config.company.name

    queue = wired.services.queue
    waiting = queue.list("pending", company)
    for item in waiting:
        queue.reject(item.ticket, "demo-prep", "cleared before recording")
    print(f"  queue: {len(waiting)} undecided ticket(s) cleared")

    on_hand = await stock.on_hand(ctx)
    print("  stock on hand:", {k: str(v) for k, v in on_hand.items()})

    short = {
        item: TARGET_ON_HAND - held
        for item, held in on_hand.items()
        if held < TARGET_ON_HAND
    }
    if not short:
        print("  nothing to restock")
        return 0

    result = await vouchers.create_purchase_voucher(
        ctx,
        party_name="Bharat Supplies",
        taxable_value="0",
        voucher_date=RESTOCK_DATE,
        reference=f"RESTOCK-{date.today().isoformat()}",
        narration="restock before filming",
        items=[
            {"stock_item": item, "quantity": str(qty), "rate": "2500"}
            for item, qty in short.items()
        ],
    )
    if result.pending is None:
        print(f"  restock not queued: {result.message}")
        return 1

    write = await execute(ctx, result.pending)
    print(f"  restocked {', '.join(f'{k} +{v}' for k, v in short.items())}: ok={write.ok}")

    ctx._stock_cache = None  # type: ignore[attr-defined]
    summary = await stock.stock_summary(ctx)
    print(" ", summary.message)
    return 0 if write.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
