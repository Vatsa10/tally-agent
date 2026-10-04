"""Rehearse the cursor-first mode against the Tally that is actually running.

    uv run python scripts/tally_ui_rehearse.py show 12
    uv run python scripts/tally_ui_rehearse.py screen
    uv run python scripts/tally_ui_rehearse.py receipt --ledger Bank --party Sharma
    uv run python scripts/tally_ui_rehearse.py journal --debit Depreciation --credit Furniture

``show`` changes nothing - it opens the Day Book and rings a row. ``screen``
just prints what the screen check reads off Tally's own header, which is the
part that has to be right before anything is ever typed. ``pay``, ``receipt``
and ``journal`` key a voucher and ask at the terminal before Ctrl+A.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date
from decimal import Decimal

from tallyagent_agent.fallback import spotlight as spotlight_mod
from tallyagent_agent.fallback.tally_ui import DesktopKeyboard, TallyUi, _screen_from_title


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("what", choices=["show", "screen", "pay", "receipt", "journal"])
    parser.add_argument("number", nargs="?", default="1")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--ledger", default="Cash")
    parser.add_argument("--expense", default="Rent")
    parser.add_argument("--party", default="Sundry Debtor")
    parser.add_argument("--debit", default="Depreciation")
    parser.add_argument("--credit", default="Furniture")
    parser.add_argument("--cost-centre", default="")
    parser.add_argument("--cost-category", default="")
    parser.add_argument("--amount", default="100.00")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.what == "screen":
        print(f"screen reads as: {_screen_from_title()!r}")
        return 0

    async def ask(step) -> bool:  # type: ignore[no-untyped-def]
        answer = input(f"{step.describe()}  [y/N] ").strip().lower()
        return answer == "y"

    light = spotlight_mod.build()
    ui = TallyUi(keyboard=DesktopKeyboard(), spotlight=light, approve=ask)
    try:
        if args.what == "pay":
            run = await ui.enter_payment(
                from_ledger=args.ledger,
                expense_ledger=args.expense,
                amount=Decimal(args.amount),
                when=date.fromisoformat(args.date),
                narration="cursor mode rehearsal",
                cost_centre=args.cost_centre,
                cost_category=args.cost_category,
            )
        elif args.what == "receipt":
            run = await ui.enter_receipt(
                into_ledger=args.ledger,
                from_ledger=args.party,
                amount=Decimal(args.amount),
                when=date.fromisoformat(args.date),
                narration="cursor mode rehearsal",
                cost_centre=args.cost_centre,
                cost_category=args.cost_category,
            )
        elif args.what == "journal":
            run = await ui.enter_journal(
                debit_ledger=args.debit,
                credit_ledger=args.credit,
                amount=Decimal(args.amount),
                when=date.fromisoformat(args.date),
                narration="cursor mode rehearsal",
            )
        else:
            run = await ui.show_voucher(args.number, date.fromisoformat(args.date))
    finally:
        light.close()
    print(run.report())
    print(f"screen seen: {_screen_from_title()!r}")
    return 0 if run.completed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
