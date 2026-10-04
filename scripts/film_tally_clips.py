"""Record the agent working inside the real TallyPrime, for the film.

    uv run python scripts/film_tally_clips.py            # every clip
    uv run python scripts/film_tally_clips.py lands      # just one

The rest of the film is drawn from captured output; these clips are the part
that has to be seen happening - Tally's own screens, changing because the agent
changed them. Each clip is a real screen recording of the Tally window while
the agent drives it, with the red cursor ring showing where it is and what it
is about to press.

This takes over the keyboard and mouse for a few minutes. Nobody should be at
the machine while it runs: the cursor refuses to type into anything that is not
Tally, so a person using the desk stops the clip rather than receiving its
keystrokes - but the clip is then wasted.

Writes ``film/clips/<name>.mkv`` and ``film/data/clips.json``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from tallyagent_agent.fallback import spotlight as spotlight_mod
from tallyagent_agent.fallback import window as window_mod
from tallyagent_agent.fallback.tally_ui import (
    DesktopKeyboard,
    TallyUi,
    _row_position,
    grab_window,
    locate_on_screen,
)
from tallyagent_agent.perception.vision import VisionScreen
from tallyagent_core import dotenv
from tallyagent_daemon import clients, wiring
from tallyagent_daemon import config as config_mod
from tallyagent_demo import capture
from tallyagent_tools import vouchers

CLIPS = Path("film/clips")
MANIFEST = Path("film/data/clips.json")
DAY = date(2026, 6, 2)
#: The partner whose approval the film shows - checked by PIN like any approval.
PARTNER, PARTNER_PIN = "R. Mehta", "4821"


async def hold(seconds: float) -> None:
    await asyncio.sleep(seconds)


#: Every step the agent narrates, with when it happened inside the current
#: take - the film shows the agent's checklist ticking in time with the footage.
EVENTS: list[dict[str, Any]] = []
CURRENT: dict[str, Any] = {"started": 0.0}


def record_event(kind: str, text: str) -> None:
    if CURRENT["started"] and text:
        EVENTS.append({"t": round(time.monotonic() - CURRENT["started"], 2),
                       "kind": kind, "text": text})


class Take:
    """One recording of exactly the Tally window, started before the action.

    The region is Tally's own window, so the frame is Tally edge to edge with
    no desktop beside it. The real mouse arrow is left out: the ring is the
    cursor, and two pointers on screen read as one too many.
    """

    def __init__(self, name: str) -> None:
        from tallyagent_agent.perception.screen import find_tally_window

        self.name = name
        self.path = CLIPS / f"{name}.mkv"
        bounds = find_tally_window()
        region = None
        if bounds is not None:
            region = capture.Region(
                left=max(0, bounds.left), top=max(0, bounds.top),
                width=bounds.width - bounds.width % 2, height=bounds.height - bounds.height % 2,
            )
        self.recorder = capture.Recorder(self.path, region=region, draw_mouse=False)
        self.started = 0.0

    def __enter__(self) -> Take:
        EVENTS.clear()
        self.recorder.start()
        time.sleep(1.2)  # ffmpeg's first frames, before anything moves
        self.started = time.monotonic()
        CURRENT["started"] = self.started - 1.2
        return self

    def __exit__(self, *exc: object) -> None:
        time.sleep(1.0)
        self.recorder.stop()
        CURRENT["started"] = 0.0
        print(f"  recorded {self.path} ({time.monotonic() - self.started:.1f}s)")


async def clip_lands(ui: TallyUi, light: Any, wired: Any, stamp: int) -> str:
    """A sale drafted, approved by a partner, and seen arriving in the Day Book."""
    services = wired.services
    result = await vouchers.create_sales_voucher(
        services.tools,
        party_name="Acme Industries",
        items=[{"stock_item": "Widget", "quantity": "2", "rate": "3200", "unit": "Pcs"}],
        voucher_date=DAY,
        reference=f"LIVE-{stamp}",
    )
    ticket = str((result.data or {}).get("ticket") or "")
    with Take("lands"):
        await ui.go_to("Day Book")
        light.point(*(locate_on_screen("Particulars") or _row_position()),
                    caption="Tally's own Day Book, before the approval")
        await hold(2.5)
        light.announce("")
        partner = services.people.authorise("approve", PARTNER, PARTNER_PIN)
        light.announce(f"{partner.name} approves {ticket} - posting over XML")
        write = await services.queue.approve(ticket, partner.name)
        await hold(1.5)
        await ui.go_to("Day Book")
        light.point(*(locate_on_screen("Acme Industries") or _row_position()),
                    caption=f"{ticket}: the new voucher, posted by Tally")
        await hold(4)
        light.announce("")
    return f"{ticket} -> {write.voucher_number or write.errors}"


async def clip_payment(ui: TallyUi, light: Any) -> str:
    """A payment keyed field by field through Tally's own screens."""
    with Take("payment"):
        run = await ui.enter_payment(
            from_ledger="Cash",
            expense_ledger="Bank Charges",
            amount=Decimal("150.00"),
            when=DAY,
            narration="keyed by the agent on camera",
            cost_centre="Ahmedabad",
            cost_category="Branches",
        )
        await hold(2.5)
    return run.report()


async def clip_receipt(ui: TallyUi, light: Any) -> str:
    """A receipt from a party with bill-wise details on: the screen Tally adds."""
    with Take("receipt"):
        run = await ui.enter_receipt(
            into_ledger="Cash",
            from_ledger="Zenith Exports",
            amount=Decimal("250.00"),
            when=DAY,
            narration="receipt keyed by the agent on camera",
        )
        await hold(2.5)
    return run.report()


async def clip_stock(ui: TallyUi, light: Any) -> str:
    with Take("stock"):
        await ui.go_to("Stock Summary")
        light.point(*(locate_on_screen("Widget") or _row_position()),
                    caption="Tally's own Stock Summary - two Widgets fewer")
        await hold(4)
        light.announce("")
    return "shown"


async def main() -> int:
    dotenv.load()
    capture.make_dpi_aware()
    wanted = set(sys.argv[1:]) or {"lands", "payment", "receipt", "stock"}

    bounds, reason = window_mod.ensure_visible()
    if bounds is None:
        print(reason, file=sys.stderr)
        return 1
    capture.place_windows({"TallyPrime": capture.TALLY_RECT})
    time.sleep(1.5)

    base = config_mod.load("config/config.toml", "config/policy.toml")
    register = clients.load("config/clients.toml")
    wired = wiring.build(clients.apply(base, register.get("demo"), register.data_dir))

    light = spotlight_mod.build(show_cursor=True)
    # The ring hops field to field while keys go in; the typing is the hold.
    light.travel_seconds, light.hold_seconds = 0.25, 0.15

    async def partner_approves(step: Any) -> bool:
        # The keyed voucher is accepted only after the same partner check as
        # any approval; the caption puts that on screen before Ctrl+A.
        partner = wired.services.people.authorise("approve", PARTNER, PARTNER_PIN)
        light.announce(f"{partner.name} approves: {step.describe()}")
        await hold(1.8)
        return True

    ui = TallyUi(
        keyboard=DesktopKeyboard(interval=0.035),
        spotlight=light,
        approve=partner_approves,
        locate=locate_on_screen,
        on_event=record_event,
        pace=0.6,
        vision=VisionScreen(wired.services.router),
        grab=grab_window,
    )
    manifest: dict[str, Any] = (
        json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.is_file() else {}
    )
    stamp = int(time.time()) % 100000
    steps = {
        "lands": lambda: clip_lands(ui, light, wired, stamp),
        "payment": lambda: clip_payment(ui, light),
        "receipt": lambda: clip_receipt(ui, light),
        "stock": lambda: clip_stock(ui, light),
    }
    try:
        for name, run in steps.items():
            if name not in wanted:
                continue
            print(f"[{name}]")
            try:
                outcome = await run()
                ok = "stopped" not in outcome and "refused" not in outcome
            except Exception as exc:  # noqa: BLE001 - one bad clip, keep the others
                outcome, ok = f"{type(exc).__name__}: {exc}", False
            path = CLIPS / f"{name}.mkv"
            manifest[name] = {
                "file": path.as_posix(),
                "ok": ok,
                "outcome": outcome,
                "seconds": capture_seconds(path),
                "events": list(EVENTS),
            }
            print(f"  {'ok' if ok else 'NOT OK'}: {outcome}")
            # One Escape leaves a voucher screen; more than that can reach the
            # Gateway's "Quit?" prompt on camera. The next clip opens its screen
            # by name through Go To, which works from wherever this leaves it.
            if DesktopKeyboard().focus():
                DesktopKeyboard().press("escape")
                await hold(0.8)
    finally:
        light.close()
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return 0 if all(v["ok"] for k, v in manifest.items() if k in wanted) else 1


def capture_seconds(path: Path) -> float:
    from tallyagent_demo.ffmpeg import duration

    try:
        return round(duration(path), 2)
    except Exception:  # noqa: BLE001
        return 0.0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
