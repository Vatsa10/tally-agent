"""Show the work in Tally itself, with the red cursor, while the chat answers.

Every other read tool answers out of our own copy of Tally's data. This one
answers by opening the screen in the user's Tally and ringing the row, which is
the difference between being told a voucher exists and watching it sit in the
Day Book. It changes nothing - navigation and a ring, no keys that can write.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from tallyagent_tools.base import ToolContext, ToolResult

log = logging.getLogger(__name__)

#: Screens worth opening from chat. Restricted on purpose: Go To will happily
#: match "Delete Company", and a typo should not be able to reach it.
REPORTS = {
    "day book": "Day Book",
    "trial balance": "Trial Balance",
    "balance sheet": "Balance Sheet",
    "profit and loss": "Profit & Loss A/c",
    "stock summary": "Stock Summary",
    "outstanding": "Bills Receivable",
    "ledger": "Ledger Vouchers",
}


def build_ui(ctx: ToolContext) -> Any:
    """The cursor-first driver, or None when Tier 3 is off."""
    fallback = ctx.fallback
    if fallback is None:
        return None
    tiers = getattr(fallback, "tiers", None)
    if tiers is not None and not tiers.config.fallback_enabled:
        return None
    from tallyagent_agent.fallback.tally_ui import DesktopKeyboard, TallyUi

    return TallyUi(
        keyboard=DesktopKeyboard(),
        spotlight=getattr(fallback, "spotlight", None),
    )


async def show_in_tally(
    ctx: ToolContext,
    report: str,
    voucher_number: str = "",
    voucher_date: date | None = None,
) -> ToolResult:
    """Open a report in the user's own TallyPrime and point at it."""
    wanted = REPORTS.get(report.strip().lower())
    if wanted is None:
        return ToolResult(
            message=(
                f"I can open {', '.join(sorted(REPORTS))} in Tally; "
                f"{report!r} is not one of them."
            )
        )

    ui = build_ui(ctx)
    if ui is None:
        return ToolResult(
            message=(
                "Cursor mode is off, so I cannot drive Tally's screens. "
                "Turn it on with /tier3 on."
            )
        )

    if voucher_number and wanted == "Day Book":
        run = await ui.show_voucher(voucher_number, voucher_date or date.today())
        return ToolResult(
            message=run.report(),
            data={"screen": wanted, "opened": run.completed},
        )

    opened = await ui.go_to(wanted) and await ui.expect_screen(wanted)
    return ToolResult(
        message=(
            f"{wanted} is open in Tally."
            if opened
            else f"I could not get Tally to open {wanted}."
        ),
        data={"screen": wanted, "opened": opened},
    )
