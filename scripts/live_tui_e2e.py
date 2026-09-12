"""End to end through the real TUI, against the real TallyPrime.

    uv run python scripts/live_tui_e2e.py

Exits non-zero on the first thing that is not true. Requires a live Tally with
the demo company loaded - scripts/recover_tally.py gets there.

Textual's pilot drives the actual app - the same widgets, the same key
handling, the same approval flow a person gets - and the model is the scripted
mock so the run is deterministic. Everything below the model is real: real
tools, real validation, real approval queue, real XML, real Tally.
"""

from __future__ import annotations

import asyncio
import time

from tallyagent_channels.tui.app import TallyAgentTUI
from tallyagent_core.livemode import LiveMode
from tallyagent_daemon import config as config_mod
from tallyagent_daemon import wiring
from tallyagent_llm import mock
from tallyagent_llm.router import Router
from tallyagent_tools import cost_centres, stock

CO = "TA-Demo Traders"
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f" - {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)


LAST: dict[str, str] = {"text": ""}


def record_turns(app) -> None:
    """Keep what each turn said.

    Textual's Static does not hand its text back, so the transcript is read at
    the source instead: this is the same object the app renders from.
    """
    original = app.session.handle

    async def wrapped(text: str):
        turn = await original(text)
        LAST["text"] = " | ".join(
            f"{line.text} {line.detail}" for line in turn.lines
        )
        return turn

    app.session.handle = wrapped  # type: ignore[method-assign]


async def turn(app, pilot, prompt: str) -> None:
    """Type a prompt and let the turn finish, exactly as a person would."""
    app.query_one("#prompt").value = prompt
    await pilot.press("enter")
    await app.workers.wait_for_complete()
    await pilot.pause()


def transcript_text(app) -> str:
    return "\n".join(
        str(getattr(child, "renderable", ""))
        for child in app.query_one("#transcript").children
    )


async def main() -> int:
    cfg = config_mod.load("config/config.toml", "config/policy.toml")
    cfg.db_path = "./live.db"
    wired = wiring.build(cfg)
    services = wired.services
    ctx = services.tools

    # The model is scripted; every tool call below is the real one.
    services.router = Router(
        mock.MockProvider(
            script=[
                # 1. a stock-moving sale
                mock.call(
                    "create_sales_voucher",
                    party_name="Acme Industries",
                    taxable_value="0",
                    voucher_date="2026-06-02",
                    reference=f"TUI-E2E-{int(time.time())}",
                    narration="TUI end to end sale",
                    items=[{"stock_item": "Widget", "quantity": "2", "rate": "3200"}],
                ),
                mock.text("Drafted the sale; it is waiting for approval."),
                # 2. a report
                mock.call("trial_balance"),
                mock.text("Trial balance read."),
                # 3. stock on hand
                mock.call("stock_summary"),
                mock.text("Stock summary read."),
                # 4. outstanding
                mock.call("outstanding_receivables"),
                mock.text("Receivables read."),
                # 5. a refusal that should never reach Tally
                mock.call(
                    "create_journal",
                    lines=[
                        {
                            "ledger": "Round Off",
                            "amount": "100.00",
                            "cost_centre": "Baroda",
                        },
                        {"ledger": "Cash", "amount": "-100.00"},
                    ],
                    voucher_date="2026-06-01",
                ),
                mock.text("That cost centre does not exist."),
            ]
        )
    )

    live = LiveMode(enabled=True, edu=True, write_prefix="TA-")
    app = TallyAgentTUI(services, live, status_interval=3600)

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        record_turns(app)

        print("1. the app comes up against live Tally")
        status = app.query_one("#status").body
        check("status shows live EDU mode", "LIVE EDU" in status, status.split("\n")[0])
        check("status names the write scope", "TA-" in status)

        print("2. a stock-moving sale, drafted and queued")
        before_stock = await stock.on_hand(ctx)
        # The live queue carries leftovers from earlier runs, so this measures
        # what *this* turn added rather than what happens to be sitting there.
        before_tickets = {item.ticket for item in app.session.pending()}
        await turn(app, pilot, "raise a sale of 2 widgets to Acme at 3200")
        pending = [
            item
            for item in app.session.pending()
            if item.ticket not in before_tickets
        ]
        check("one action queued", len(pending) == 1, f"{len(pending)} new")
        if not pending:
            print("    (nothing queued; the rest of the run needs a draft)")
            return 1
        if pending:
            action = pending[0]
            check("the draft carries stock", bool(action.voucher.inventory))
            check(
                "the taxable value came from the goods",
                action.voucher.gst.taxable_value.normalize() == 6400,
                str(action.voucher.gst.taxable_value),
            )
            check("validation passed", action.validation.ok, action.validation.summary())

        print("3. approving through the TUI writes to Tally")
        ticket_id = pending[0].ticket
        await app._run_command(f"/approve {ticket_id}")
        await app.workers.wait_for_complete()
        await pilot.pause()
        ctx._stock_cache = None
        after_stock = await stock.on_hand(ctx)
        moved = after_stock.get("Widget", 0) - before_stock.get("Widget", 0)
        check("stock went down by 2", moved == -2, f"moved {moved}")
        check(
            "that ticket is no longer pending",
            ticket_id not in {i.ticket for i in app.session.pending()},
        )

        print("4. reports read back through the TUI")
        await turn(app, pilot, "trial balance please")
        await turn(app, pilot, "what stock do we hold?")
        await turn(app, pilot, "who owes us money?")
        check("five turns ran", app.session.stats.turns == 5, str(app.session.stats.turns))
        check("tools were actually called", app.session.stats.tool_calls >= 4,
              str(app.session.stats.tool_calls))

        print("5. an unknown cost centre never reaches Tally")
        before_tickets = {item.ticket for item in app.session.pending()}
        await turn(app, pilot, "book 100 rent to the Baroda branch")
        added = {i.ticket for i in app.session.pending()} - before_tickets
        check("nothing new was queued", not added, str(added))
        check(
            "the turn says why",
            "cost centre" in LAST["text"].lower(),
            LAST["text"][:200],
        )

    print("6. the books agree with themselves")
    balances = await ctx.backend.ledger_balances(company=CO)
    debits = sum(v for v in balances.values() if v > 0)
    credits = -sum(v for v in balances.values() if v < 0)
    check("trial balance balances", debits == credits, f"{debits} vs {credits}")

    summary = await stock.stock_summary(ctx)
    print("   ", summary.message)
    centres = await cost_centres.list_cost_centres(ctx)
    print("   ", centres.message)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("every check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
