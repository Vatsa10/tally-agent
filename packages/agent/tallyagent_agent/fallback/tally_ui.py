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
    #: The screen this step expects to be typing into. Tally opens sub-screens
    #: of its own accord, and a step that names the one it wants is the only
    #: way to tell the expected one from a surprise.
    on_screen: str = "Payment"
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
            if not self.performed:
                return f"{self.task}: done, on screen in Tally."
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

            async def sleep(seconds: float) -> None:
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
        # A Go To list left open from a previous step swallows the next Alt+G,
        # and then the report name is typed into a palette that is already
        # searching. Close it first, and only it.
        if self._palette_open():
            self.keyboard.press("escape")
            await self._sleep(0.6)
        # The first Alt+G after Tally is raised is sometimes swallowed, and the
        # report name then gets typed into whatever report is open. Confirm the
        # palette is up before typing, and ask again if it is not.
        for attempt in (1, 2):
            self.keyboard.press("alt+g")
            await self._sleep(0.8)
            if self._palette_open():
                break
            log.info("Go To did not open (attempt %d)", attempt)
        else:
            return False
        self.keyboard.type(report)
        await self._sleep(0.5)
        self.keyboard.press("enter")
        await self._sleep(SETTLE)
        # Go To leaves its report list sitting over the report it just opened.
        # Escape closes the list, not the report - but only press it when the
        # list is actually there, or it backs out of the report instead.
        if self._palette_open():
            self.keyboard.press("escape")
            await self._sleep(0.6)
        return True

    def _palette_open(self) -> bool:
        """Is Tally's Go To list up? It is what Alt+G is supposed to produce."""
        return "listofreports" in self._screen_name().lower().replace(" ", "")

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
        from_ledger: str,
        expense_ledger: str,
        amount: Decimal,
        when: date,
        narration: str = "",
        cost_centre: str = "",
        cost_category: str = "",
    ) -> UiRun:
        """Key a payment voucher through Tally's own screen.

        Deliberately the simplest voucher there is - paid out of one account,
        against one expense - because the point of this mode is that a person
        can follow it, and a GST sales invoice keyed through the UI is a wall
        of fields nobody can watch meaningfully. Anything more elaborate
        belongs on the XML path, which is what it is for.

        Go To is asked for "Create Voucher", not for "Payment": typing a
        voucher type into Go To matches the first report that happens to start
        the same way - "Payment" selects *Payment Advice* - so the screen is
        opened generically and F5 chooses the type once we are on it.

        Each field is its own step, so an approver refusing halfway leaves a
        half-filled voucher that Tally has not accepted, rather than a posted
        one that has to be found and deleted.
        """
        run = UiRun(task=f"payment {amount} to {expense_ledger}")
        if not await self.go_to("Create Voucher"):
            run.stopped = "could not bring Tally to the front"
            return run
        if not await self.expect_screen("Voucher Creation"):
            run.stopped = (
                "Tally did not open the voucher creation screen, so nothing was typed"
            )
            return run

        # Whatever voucher type was last used is the one Tally opens on.
        await self.step(UiStep("choose the Payment voucher type", keys=["f5"]))
        if not await self.expect_screen("Payment"):
            run.stopped = "Tally did not switch to a Payment voucher; nothing was typed"
            return run

        steps = [
            UiStep("open the date field", keys=["f2"]),
            UiStep(
                "the voucher date",
                text=when.strftime("%d-%m-%Y"),
                keys=["enter"],
                on_screen="Change Voucher Date",
            ),
            UiStep("the account to pay from", text=from_ledger, keys=["enter"]),
            UiStep("what it is being spent on", text=expense_ledger, keys=["enter"]),
            UiStep("the amount", text=f"{amount}", keys=["enter"]),
        ]
        tail = [
            # An empty particulars line is how Tally is told the entries are
            # done; it is also what moves the cursor to the narration. Typed
            # before this, a narration goes into a ledger name field and Tally
            # offers to create a ledger called "June rent".
            UiStep("no more lines", keys=["enter"]),
            UiStep("the narration", text=narration),
            UiStep("accept the voucher", keys=["ctrl+a"], mutating=True),
        ]

        run.steps = steps
        for step in steps:
            # Tally opens sub-screens of its own accord: a ledger with cost
            # centres on it pops a Cost Allocation screen mid-voucher, and the
            # next field then goes into *that*. Left unchecked, a narration was
            # typed into a cost centre name and Ctrl+A accepted the sub-screen.
            # So every field re-confirms it is still the Payment voucher.
            if step is not steps[0] and not await self.expect_screen(step.on_screen):
                run.stopped = (
                    "Tally opened another screen mid-voucher "
                    f"({self._screen_name()[:80]!r}), so the rest was not typed"
                )
                self.keyboard.press("escape")
                return run
            if not await self.step(step):
                run.stopped = (
                    f"refused at {step.describe()}"
                    if step.refused
                    else f"could not perform {step.describe()}"
                )
                # Back out so Tally is not left holding a half-typed voucher.
                self.keyboard.press("escape")
                return run

        # Most Indian companies switch cost centres on for their expense
        # ledgers, and Tally then interrupts the voucher with an allocation
        # screen. Allocating the whole amount to one centre is the case worth
        # automating; anything split belongs on the XML path.
        if await self.expect_screen("Cost Allocations"):
            if not cost_centre:
                run.stopped = (
                    f"{expense_ledger} is allocated to cost centres, and none "
                    "was given, so the voucher was left unposted"
                )
                self.keyboard.press("escape")
                return run
            allocation = []
            # A company with cost categories asks for the category before the
            # centre, and the centre name typed into that field matches
            # nothing. Which it is cannot be read off the screen - the category
            # list only appears once the field is typed into - so the caller,
            # which has the cost centre masters, says.
            if cost_category:
                allocation.append(
                    UiStep(
                        "the cost category",
                        text=cost_category,
                        keys=["enter"],
                        on_screen="Cost Allocations",
                    )
                )
            allocation += [
                UiStep(
                    "the cost centre",
                    text=cost_centre,
                    keys=["enter"],
                    on_screen="Cost Allocations",
                ),
                UiStep(
                    "the amount against it",
                    text=f"{amount}",
                    keys=["enter"],
                    on_screen="Cost Allocations",
                ),
                # Tally offers a second allocation row; an empty one closes the
                # screen and hands the voucher back.
                UiStep(
                    "finish the allocation",
                    keys=["enter"],
                    on_screen="Cost Allocations",
                ),
            ]
            run.steps.extend(allocation)
            for step in allocation:
                if not await self.step(step):
                    run.stopped = f"could not perform {step.describe()}"
                    self.keyboard.press("escape")
                    return run

        run.steps.extend(tail)
        for step in tail:
            if not await self.expect_screen(step.on_screen):
                run.stopped = (
                    "Tally opened another screen mid-voucher "
                    f"({self._screen_name()[:80]!r}), so the rest was not typed"
                )
                self.keyboard.press("escape")
                return run
            if not await self.step(step):
                run.stopped = (
                    f"refused at {step.describe()}"
                    if step.refused
                    else f"could not perform {step.describe()}"
                )
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


_OCR: Any = None


def _ocr_lines(png: bytes) -> list[str]:
    """Read the words out of a PNG with the local OCR engine."""
    global _OCR
    import numpy  # noqa: PLC0415
    from PIL import Image as PilImage  # noqa: PLC0415

    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-untyped]  # noqa: PLC0415

        _OCR = RapidOCR()
    import io  # noqa: PLC0415

    image = PilImage.open(io.BytesIO(png)).convert("RGB")
    found, _ = _OCR(numpy.array(image))
    return [line[1] for line in (found or [])]


def _screen_from_title() -> str:
    """What Tally says it is showing, read off Tally's own header.

    The OS title bar says ``TallyPrime:9000`` whatever is open - the report name
    lives in the header Tally draws itself, so the only way to know which screen
    is up is to look at the pixels. The window renders itself through
    PrintWindow, so this works even when Tally is not in front, and the top
    fifth is cropped off before OCR because the rest of the screen is rows of
    numbers that cost a second each and never contain the answer.

    Anything that goes wrong returns "" - an unknown screen is treated as the
    wrong screen, which is the direction that refuses to type.
    """
    try:
        import io  # noqa: PLC0415

        from PIL import Image as PilImage  # noqa: PLC0415

        from tallyagent_agent.perception.screen import capture, find_tally_window

        bounds = find_tally_window()
        if bounds is None:
            return ""
        png = capture(bounds)
        image = PilImage.open(io.BytesIO(png)).convert("RGB")
        header = image.crop((0, 0, image.width, max(1, image.height // 5)))
        buffer = io.BytesIO()
        header.save(buffer, format="PNG")
        return " ".join(_ocr_lines(buffer.getvalue()))
    except Exception as exc:  # noqa: BLE001 - unknown screen is the wrong screen
        log.debug("could not read Tally's screen name: %s", exc)
        return ""


class DesktopKeyboard:
    """The real keyboard, and the window it is allowed to type into.

    ``focus`` is not a convenience: every key here goes to whatever has focus,
    so a step that cannot prove Tally is in front must not press anything.
    """

    def __init__(self, interval: float = 0.02) -> None:
        self.interval = interval

    def _pyautogui(self) -> Any:
        import pyautogui  # type: ignore[import-untyped]  # noqa: PLC0415

        pyautogui.FAILSAFE = False
        return pyautogui

    def type(self, text: str, interval: float | None = None) -> None:
        self._pyautogui().write(
            text, interval=self.interval if interval is None else interval
        )

    def press(self, key: str) -> None:
        pyautogui = self._pyautogui()
        if "+" in key:
            pyautogui.hotkey(*[part.strip() for part in key.split("+")])
        else:
            pyautogui.press(key)

    def focus(self, title: str = "TallyPrime") -> bool:
        from tallyagent_agent.fallback.window import ensure_visible  # noqa: PLC0415

        bounds, reason = ensure_visible()
        if bounds is None:
            log.warning("cannot type into Tally: %s", reason)
            return False
        return True
