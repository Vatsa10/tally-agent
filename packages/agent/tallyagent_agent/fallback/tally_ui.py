"""Operating TallyPrime's own screens, visibly, one narrated step at a time.

Everything else in this product writes over XML, which is faster and safer and
which a bookkeeper cannot watch. That matters more than it sounds. The people
who buy this live inside Tally's screens all day; an integration that claims to
have posted something is a claim, and the market has been sold shallow claims
before - file drops called integrations, connectors that break on the next Tally
update. Watching the work happen in their own Tally, at a speed they can follow,
with the keystroke named before it is pressed, is the proof.

So this is not a fallback for when XML fails. It is a *mode*: a way to do work
where a person can see every step, refuse any of them, and recognise the screens
being used as the ones they use.

Three rules, none of them negotiable:

- **Navigate by name, never by menu letter.** ``k`` is Day Book at the Gateway
  and something else on every other screen. Go To takes a report's full name
  from anywhere.
- **Nothing is typed into a field until the screen is confirmed.** Tally's own
  title bar says which screen is open; if it is not the expected one, the step
  stops rather than typing an amount into whatever happens to have focus.
- **Every mutating step goes through the approval hook**, the same one the XML
  path uses. Visible is not the same as permitted.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

log = logging.getLogger(__name__)

#: How long Tally needs to draw a screen before the next key means anything.
SETTLE = 1.2

#: Tally writes the open screen's name in its own title area. Checking it is
#: what makes typing into the right field a fact rather than a hope.
SCREEN_CHECK_ATTEMPTS = 3


@dataclass(slots=True)
class UiStep:
    """One thing done to Tally's interface, in words a person can refuse."""

    what: str
    keys: list[str] = field(default_factory=list)
    text: str = ""
    mutating: bool = False
    done: bool = False
    refused: bool = False

    def describe(self) -> str:
        if self.text:
            return f'{self.what}: type "{self.text}"'
        return f"{self.what}: {' then '.join(self.keys) or 'nothing'}"


@dataclass(slots=True)
class UiRun:
    task: str
    steps: list[UiStep] = field(default_factory=list)
    completed: bool = False
    stopped: str = ""

    @property
    def performed(self) -> list[UiStep]:
        return [s for s in self.steps if s.done]

    def report(self) -> str:
        if self.completed:
            return f"{self.task}: {len(self.performed)} step(s), finished."
        return f"{self.task}: stopped after {len(self.performed)} step(s) - {self.stopped}"


#: Asks a person about one mutating step. Returns True to let it happen.
Approve = Callable[[UiStep], Awaitable[bool]]


class TallyUi:
    """Drives Tally's screens with a visible cursor and a running commentary."""

    def __init__(
        self,
        keyboard: Any,
        spotlight: Any = None,
        approve: Approve | None = None,
        sleep: Any = None,
        screen_name: Callable[[], str] | None = None,
        title: str = "TallyPrime",
    ) -> None:
        self.keyboard = keyboard
        self.spotlight = spotlight
        self.approve = approve
        self.title = title
        self._screen_name = screen_name or _screen_from_title
        if sleep is None:
            import asyncio

            async def sleep(seconds: float) -> None:  # type: ignore[misc]
                await asyncio.sleep(seconds)

        self._sleep = sleep

    # --- the primitives -----------------------------------------------------

    def say(self, words: str) -> None:
        """Put the reason on screen, beside the ring."""
        if self.spotlight is not None:
            self.spotlight.announce(words)
        log.info("tally ui: %s", words)

    async def go_to(self, report: str) -> bool:
        """Open a screen by name. Works from wherever Tally happens to be."""
        self.say(f"opening {report}")
        if not self.keyboard.focus(self.title):
            return False
        self.keyboard.press("alt+g")
        await self._sleep(0.8)
        self.keyboard.type(report)
        await self._sleep(0.5)
        self.keyboard.press("enter")
        await self._sleep(SETTLE)
        return True

    async def expect_screen(self, expected: str) -> bool:
        """Is the screen we asked for the screen that is open?

        The whole safety of typing into Tally rests on this. Without it a
        mistyped Go To leaves the next amount going into a filter box, or worse
        into a voucher nobody meant to touch.
        """
        wanted = expected.lower().replace(" ", "")
        for _ in range(SCREEN_CHECK_ATTEMPTS):
            current = self._screen_name().lower().replace(" ", "")
            if wanted in current:
                return True
            await self._sleep(0.6)
        log.warning("expected %r, Tally is showing %r", expected, self._screen_name())
        return False

    async def step(self, step: UiStep) -> bool:
        """Narrate, ask if it changes anything, then do it."""
        self.say(step.describe())
        if step.mutating:
            if self.approve is None:
                step.refused = True
                return False
            if not await self.approve(step):
                step.refused = True
                return False

        if not self.keyboard.focus(self.title):
            return False
        if step.text:
            self.keyboard.type(step.text)
        for key in step.keys:
            self.keyboard.press(key)
            await self._sleep(0.35)
        await self._sleep(SETTLE if step.mutating else 0.4)
        step.done = True
        return True

    # --- the work ------------------------------------------------------------

    async def show_voucher(self, number: str, when: date) -> UiRun:
        """Open the Day Book and put the ring on a voucher. Changes nothing.

        The everyday use of this mode: "show me what you posted", answered by
        Tally itself rather than by us repeating our own record back.
        """
        run = UiRun(task=f"show voucher {number}")
        if not await self.go_to("Day Book"):
            run.stopped = "could not bring Tally to the front"
            return run
        if not await self.expect_screen("Day Book"):
            run.stopped = "Tally did not open the Day Book"
            return run

        self.say(f"voucher {number} dated {when:%d-%b-%Y}")
        if self.spotlight is not None:
            self.spotlight.point(*_row_position())
        run.completed = True
        return run

    async def enter_payment(
        self,
        ledger: str,
        amount: Decimal,
        when: date,
        narration: str = "",
    ) -> UiRun:
        """Key a payment voucher through Tally's own screen.

        Deliberately the simplest voucher there is - one ledger, one amount -
        because the point of this mode is that a person can follow it, and a
        GST sales invoice keyed through the UI is a wall of fields nobody can
        watch meaningfully. Anything more elaborate belongs on the XML path,
        which is what it is for.

        Each field is its own step, so an approver refusing halfway leaves a
        half-filled voucher that Tally has not accepted, rather than a posted
        one that has to be found and deleted.
        """
        run = UiRun(task=f"payment {amount} to {ledger}")
        if not await self.go_to("Payment"):
            run.stopped = "could not bring Tally to the front"
            return run
        if not await self.expect_screen("Payment"):
            run.stopped = (
                "Tally did not open the Payment voucher screen, so nothing was typed"
            )
            return run

        steps = [
            UiStep("set the date", keys=["f2"], mutating=False),
            UiStep("the voucher date", text=when.strftime("%d-%m-%Y"), keys=["enter"]),
            UiStep("the account to pay from", text=ledger, keys=["enter"]),
            UiStep("the amount", text=f"{amount}", keys=["enter"]),
        ]
        if narration:
            steps.append(UiStep("the narration", text=narration, keys=["enter"]))
        steps.append(
            UiStep("accept the voucher", keys=["ctrl+a"], mutating=True)
        )

        run.steps = steps
        for step in steps:
            if not await self.step(step):
                run.stopped = (
                    f"refused at {step.describe()}"
                    if step.refused
                    else f"could not perform {step.describe()}"
                )
                # Back out so Tally is not left holding a half-typed voucher.
                self.keyboard.press("escape")
                return run

        run.completed = True
        return run


def _row_position() -> tuple[int, int]:
    """Where the first rows of a Tally report sit on screen.

    Tally lists from the top down, so a ring in the middle of the window points
    at nothing - which is what the first version did.
    """
    try:
        from tallyagent_agent.perception.screen import find_tally_window

        bounds = find_tally_window()
        if bounds is None:
            return (0, 0)
        return (bounds.left + bounds.width // 2, bounds.top + 220)
    except Exception:  # noqa: BLE001
        return (0, 0)


def _screen_from_title() -> str:
    """What Tally says it is showing, read off the window.

    Tally puts the open report's name in its own header rather than the OS
    title bar, so this reads the window text it does expose and falls back to
    an empty string - which fails the check, which is the safe direction.
    """
    try:
        import win32gui  # type: ignore[import-not-found]

        from tallyagent_agent.perception.screen import find_tally_window

        bounds = find_tally_window()
        if bounds is None:
            return ""
        handle = win32gui.FindWindow(None, bounds.title)
        return win32gui.GetWindowText(handle) or "" if handle else ""
    except Exception:  # noqa: BLE001 - unknown screen is treated as wrong screen
        return ""
