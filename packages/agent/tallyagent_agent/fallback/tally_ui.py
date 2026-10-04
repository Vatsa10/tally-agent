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

#: Where each kind of step happens on Tally's screen: the label Tally draws
#: beside the field. The ring is moved there before anything is typed, so a
#: person watching sees the field being filled rather than a ring parked
#: somewhere else - which is what the first version showed.
ANCHORS: tuple[tuple[str, str], ...] = (
    ("choose the", "Voucher Creation"),
    ("open the date field", "Date"),
    ("the voucher date", "Change Voucher Date"),
    ("the account", "Account"),
    ("what it is being spent on", "Particulars"),
    ("who or what it came from", "Particulars"),
    ("the ledger", "Particulars"),
    ("debit side", "Particulars"),
    ("credit side", "Particulars"),
    ("the amount against it", "Amount"),
    ("the amount", "Amount"),
    ("the debit amount", "Amount"),
    ("the credit amount", "Amount"),
    ("the cost category", "Cost Category"),
    ("the cost centre", "Name of Cost Centre"),
    ("finish the allocation", "Cost Allocations"),
    ("allocate it on account", "Type of Ref"),
    ("accept the amount", "Amount"),
    ("finish the bill allocation", "Bill-wise Details"),
    ("no more lines", "Narration"),
    ("the narration", "Narration"),
    ("accept the voucher", "Narration"),
)


def anchor_for(what: str) -> str:
    for prefix, label in ANCHORS:
        if what.startswith(prefix):
            return label
    return ""

#: Tally writes the open screen's name in its own title area. Checking it is
#: what makes typing into the right field a fact rather than a hope.
SCREEN_CHECK_ATTEMPTS = 3


@dataclass(slots=True)
class UiStep:
    """One thing done to Tally's interface, in words a person can refuse."""

    what: str
    keys: list[str] = field(default_factory=list)
    text: str = ""
    #: The label beside this step's field on Tally's screen; derived from
    #: ``what`` when not given.
    anchor: str = ""
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
        locate: Callable[[str], tuple[int, int] | None] | None = None,
        on_event: Callable[[str, str], None] | None = None,
        pace: float = 1.0,
        vision: Any = None,
        grab: Callable[[], tuple[bytes, Any] | None] | None = None,
    ) -> None:
        self.keyboard = keyboard
        self.spotlight = spotlight
        self.approve = approve
        self.title = title
        self._screen_name = screen_name or _screen_from_title
        # Finding a field means reading the real screen, so it is never implied:
        # callers driving the desktop pass ``locate_on_screen``; tests and
        # anything without a ring pass nothing.
        self._locate = locate
        self._on_event = on_event
        #: Multiplies every pause. Below 1 for a recording, where the waits
        #: that let Tally draw are kept and the rest is dead air.
        self.pace = pace
        #: The agent's eyes: a vision model that says what screen is up and
        #: what is in front of it (``perception.vision.VisionScreen``), and a
        #: way to grab the window (png bytes, bounds). Both optional - without
        #: them the driver works exactly as before, on header text alone.
        self.vision = vision
        self._grab = grab
        self._watcher: Any = None
        self._seeing: Any = None
        self.last_seen: Any = None
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
        self._event("say", words)
        log.info("tally ui: %s", words)

    def _event(self, kind: str, text: str) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(kind, text)
        except Exception:  # noqa: BLE001 - an observer never stops the work
            log.debug("event observer failed", exc_info=True)

    async def _wait(self, seconds: float) -> None:
        await self._sleep(seconds * self.pace)

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
            await self._wait(0.6)
        # Alt+G does nothing inside voucher entry. go_to is never called in the
        # middle of a voucher this driver is keying, so a voucher screen here is
        # the blank one Tally opens after an accept - and Escape leaves that.
        if "vouchercreation" in self._screen_name().lower().replace(" ", ""):
            self.keyboard.press("escape")
            await self._wait(0.8)
        # The first Alt+G after Tally is raised is sometimes swallowed, and the
        # report name then gets typed into whatever report is open. Confirm the
        # palette is up before typing, and ask again if it is not.
        for attempt in (1, 2):
            self.keyboard.press("alt+g")
            # Poll rather than check once: a Tally that has just been restored
            # or is busy can take two seconds to draw the list, and pressing
            # Alt+G again while it is drawing *closes* it.
            if await self._palette_appears():
                break
            log.info("Go To did not open (attempt %d)", attempt)
        else:
            return False
        self.keyboard.type(report)
        await self._wait(0.5)
        self.keyboard.press("enter")
        await self._wait(SETTLE)
        # Go To leaves its report list sitting over the report it just opened.
        # Escape closes the list, not the report - but only press it when the
        # list is actually there, or it backs out of the report instead.
        if self._palette_open():
            self.keyboard.press("escape")
            await self._wait(0.6)
        return True

    async def _palette_appears(self, polls: int = 5, every: float = 0.5) -> bool:
        for _ in range(polls):
            await self._sleep(every)
            if self._palette_open():
                return True
        return False

    def _palette_open(self) -> bool:
        """Is Tally's Go To list up? It is what Alt+G is supposed to produce."""
        return "listofreports" in self._screen_name().lower().replace(" ", "")

    async def probe_screen(self, expected: str) -> bool:
        """Has an optional popup opened? One look, no retries, no vision.

        Bill-wise and Cost Allocation screens appear only for some ledgers.
        Asking expect_screen whether they were up cost every voucher without
        them three header reads and a vision read - 36 seconds of a recorded
        payment spent confirming that nothing had happened.
        """
        await self._wait(0.5)
        wanted = expected.lower().replace(" ", "")
        return wanted in self._screen_name().lower().replace(" ", "")

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
            await self._wait(0.6)
        # Header text says no. Before refusing, look at the whole screen: the
        # vision model reads popups and sub-screens the header strip does not
        # show, and if it is not the expected screen it can at least say what
        # is there - "Bill-wise Details for Zenith Exports" rather than "an
        # unexpected screen".
        state = await self.look()
        if state is not None and state.mentions(expected):
            return True
        log.warning(
            "expected %r, Tally is showing %r",
            expected,
            state.describe() if state is not None else self._screen_name(),
        )
        return False

    async def step(self, step: UiStep) -> bool:
        """Narrate, ask if it changes anything, then do it."""
        self.say(step.describe())
        self._point_at(step)
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
            await self._wait(0.35)
        await self._wait(SETTLE if step.mutating else 0.4)
        step.done = True
        self._event("done", step.describe())
        # The caption belongs to this step. Left in place, the next screen
        # carried the last one's words - "the new voucher, posted" over a
        # payment that had not been typed yet.
        if self.spotlight is not None:
            self.spotlight.announce("")
        return True

    def _point_at(self, step: UiStep) -> None:
        """Move the ring to the field this step is about to fill.

        Tally's own highlight first: the field holding the cursor is painted in
        a distinct yellow, found in milliseconds and exact. Reading the screen
        for the field's label is the fallback for screens with no highlight.
        """
        if self.spotlight is None:
            return
        frame = self._frame()
        if frame is not None:
            self._notice(frame[0])
            from tallyagent_agent.perception.focus import active_field, centre

            try:
                box = active_field(frame[0])
            except Exception:  # noqa: BLE001 - fall back to the label
                box = None
            if box is not None:
                x, y = centre(box)
                bounds = frame[1]
                self.spotlight.point(bounds.left + x, bounds.top + y, caption=step.describe())
                return
        if self._locate is None:
            return
        label = step.anchor or anchor_for(step.what)
        if not label:
            return
        try:
            where = self._locate(label)
        except Exception:  # noqa: BLE001 - the keys still go in; only the ring misses
            where = None
        if where is not None:
            self.spotlight.point(*where, caption=step.describe())

    # --- the work ------------------------------------------------------------

    async def show_voucher(self, number: str, when: date) -> UiRun:
        """Open the Day Book and put the ring on a voucher. Changes nothing.

        The everyday use of this mode: "show me what you posted", answered by
        Tally itself rather than by us repeating our own record back.
        """
        run = UiRun(task=f"show voucher {number}")
        if not await self.go_to("Day Book"):
            run.stopped = "could not open the screen through Go To"
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
        fields = [
            UiStep("the account to pay from", text=from_ledger, keys=["enter"]),
            UiStep("what it is being spent on", text=expense_ledger, keys=["enter"]),
            UiStep("the amount", text=f"{amount}", keys=["enter"]),
        ]
        return await self._enter_voucher(
            run,
            type_key="f5",
            type_name="Payment",
            fields=fields,
            when=when,
            amount=amount,
            allocated_ledger=expense_ledger,
            narration=narration,
            cost_centre=cost_centre,
            cost_category=cost_category,
        )

    async def enter_receipt(
        self,
        into_ledger: str,
        from_ledger: str,
        amount: Decimal,
        when: date,
        narration: str = "",
        cost_centre: str = "",
        cost_category: str = "",
    ) -> UiRun:
        """Key a receipt voucher: the mirror image of a payment.

        Tally's Receipt screen asks first for the account the money landed in
        and then for the party or income ledger it came from, so the fields go
        in that order. An income ledger with cost centres on it interrupts the
        voucher exactly the way an expense ledger does on a payment, and is
        handled the same way.
        """
        run = UiRun(task=f"receipt {amount} from {from_ledger}")
        fields = [
            UiStep(
                "the account received into",
                text=into_ledger,
                keys=["enter"],
                on_screen="Receipt",
            ),
            UiStep(
                "who or what it came from",
                text=from_ledger,
                keys=["enter"],
                on_screen="Receipt",
            ),
            UiStep("the amount", text=f"{amount}", keys=["enter"], on_screen="Receipt"),
        ]
        return await self._enter_voucher(
            run,
            type_key="f6",
            type_name="Receipt",
            fields=fields,
            when=when,
            amount=amount,
            allocated_ledger=from_ledger,
            narration=narration,
            cost_centre=cost_centre,
            cost_category=cost_category,
        )

    async def enter_journal(
        self,
        debit_ledger: str,
        credit_ledger: str,
        amount: Decimal,
        when: date,
        narration: str = "",
    ) -> UiRun:
        """Key a two-line journal: one ledger debited, one credited.

        A journal line in Tally starts by asking whether it is a debit or a
        credit - "By" or "To" in the old wording, Dr or Cr on the screen - and
        only then for the ledger and the amount. Each of those is its own
        narrated step, so a person watching sees the side chosen before the
        ledger is named. Two lines is the limit for the same reason a payment
        has one expense: past that nobody can follow it, and the XML path is
        the right tool.

        No cost centre is taken. A journal ledger that pops an allocation
        screen between the lines is caught by the per-field screen check and
        the voucher is left unposted, which is the safe reading of a screen
        nobody planned for.
        """
        run = UiRun(task=f"journal {amount} from {credit_ledger} to {debit_ledger}")
        fields = [
            UiStep("debit side", text="Dr", keys=["enter"], on_screen="Journal"),
            UiStep(
                "the ledger debited",
                text=debit_ledger,
                keys=["enter"],
                on_screen="Journal",
            ),
            UiStep(
                "the debit amount",
                text=f"{amount}",
                keys=["enter"],
                on_screen="Journal",
            ),
            UiStep("credit side", text="Cr", keys=["enter"], on_screen="Journal"),
            UiStep(
                "the ledger credited",
                text=credit_ledger,
                keys=["enter"],
                on_screen="Journal",
            ),
            UiStep(
                "the credit amount",
                text=f"{amount}",
                keys=["enter"],
                on_screen="Journal",
            ),
        ]
        return await self._enter_voucher(
            run,
            type_key="f7",
            type_name="Journal",
            fields=fields,
            when=when,
            amount=amount,
            # Only an allocation screen opened by the last line keyed survives
            # to the post-field check - one opened by the debit line is caught
            # by the screen guard before "Cr" is typed - so the ledger to name
            # in the refusal is the credit one.
            allocated_ledger=credit_ledger,
            narration=narration,
        )

    async def _enter_voucher(
        self,
        run: UiRun,
        *,
        type_key: str,
        type_name: str,
        fields: list[UiStep],
        when: date,
        amount: Decimal,
        allocated_ledger: str,
        narration: str,
        cost_centre: str = "",
        cost_category: str = "",
    ) -> UiRun:
        """The part of keying a voucher that does not depend on its type.

        Payment, receipt and journal differ only in the F-key that picks the
        type and the fields in between. The screen checks, the date sub-screen,
        the cost allocation and the approval-gated accept are the same, and a
        safety rule fixed in one copy and not another is the kind of drift that
        puts an amount into the wrong field. So there is one copy.
        """
        if not await self.go_to("Create Voucher"):
            run.stopped = "could not open the screen through Go To"
            return run
        if not await self.expect_screen("Voucher Creation"):
            run.stopped = (
                "Tally did not open the voucher creation screen, so nothing was typed"
            )
            return run

        # Whatever voucher type was last used is the one Tally opens on.
        await self.step(UiStep(f"choose the {type_name} voucher type", keys=[type_key]))
        if not await self.expect_screen(type_name):
            run.stopped = (
                f"Tally did not switch to a {type_name} voucher; nothing was typed"
            )
            return run

        steps = [
            UiStep("open the date field", keys=["f2"], on_screen=type_name),
            UiStep(
                "the voucher date",
                text=when.strftime("%d-%m-%Y"),
                keys=["enter"],
                on_screen="Change Voucher Date",
            ),
            *fields,
        ]
        tail = [
            # An empty particulars line is how Tally is told the entries are
            # done; it is also what moves the cursor to the narration. Typed
            # before this, a narration goes into a ledger name field and Tally
            # offers to create a ledger called "June rent".
            UiStep("no more lines", keys=["enter"], on_screen=type_name),
            UiStep("the narration", text=narration, on_screen=type_name),
            UiStep(
                "accept the voucher",
                keys=["ctrl+a"],
                mutating=True,
                on_screen=type_name,
            ),
        ]

        run.steps = steps
        for step in steps:
            # Tally opens sub-screens of its own accord: a ledger with cost
            # centres on it pops a Cost Allocation screen mid-voucher, and the
            # next field then goes into *that*. Left unchecked, a narration was
            # typed into a cost centre name and Ctrl+A accepted the sub-screen.
            # So every field re-confirms it is still the voucher it started as.
            if step is not steps[0] and not await self.expect_screen(step.on_screen):
                return self._interrupted(run)
            if not await self._perform(run, step):
                return run

        # Most Indian companies switch cost centres on for their expense and
        # income ledgers, and Tally then interrupts the voucher with an
        # allocation screen. Allocating the whole amount to one centre is the
        # case worth automating; anything split belongs on the XML path.
        if await self.probe_screen("Cost Allocations"):
            if not cost_centre:
                run.stopped = (
                    f"{allocated_ledger} is allocated to cost centres, and none "
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

        # A party ledger with bill-wise details on - the default for debtors
        # and creditors - stops the voucher to ask which bill the amount is
        # against. "On Account" is the honest answer for a keyed entry: it does
        # not guess which of the party's bills this settles, and the partner can
        # match it against a bill afterwards. Choosing a particular bill belongs
        # to whoever knows which one it was.
        if await self.probe_screen("Bill-wise Details"):
            billwise = [
                UiStep(
                    "allocate it on account",
                    text="On Account",
                    keys=["enter"],
                    on_screen="Bill-wise Details",
                ),
                # Tally fills in the full amount; accepting it is one Enter.
                UiStep(
                    "accept the amount",
                    keys=["enter"],
                    on_screen="Bill-wise Details",
                ),
            ]
            run.steps.extend(billwise)
            for step in billwise:
                if not await self._perform(run, step):
                    return run
            # Some releases ask for a further reference row; an empty one
            # closes the screen. Only pressed if the screen is still there.
            if await self.probe_screen("Bill-wise Details"):
                close = UiStep(
                    "finish the bill allocation",
                    keys=["enter"],
                    on_screen="Bill-wise Details",
                )
                run.steps.append(close)
                if not await self._perform(run, close):
                    return run

        run.steps.extend(tail)
        for step in tail:
            if not await self.expect_screen(step.on_screen):
                return self._interrupted(run)
            if not await self._perform(run, step):
                return run

        run.completed = True
        # Accepting a voucher opens the next blank one of the same type, and
        # Go To does nothing from inside voucher entry - measured, a receipt
        # keyed straight after a payment could not even open its screen.
        # The blank voucher has nothing in it, so Escape leaves it unasked.
        await self._wait(0.6)
        if await self.probe_screen("Voucher Creation"):
            self.keyboard.press("escape")
            await self._wait(0.6)
        return run

    def _frame(self) -> tuple[bytes, Any] | None:
        if self._grab is None:
            return None
        try:
            return self._grab()
        except Exception:  # noqa: BLE001 - no picture, no pointer; keys still go
            return None

    def _notice(self, png: bytes) -> None:
        """When Tally has drawn a new screen, have it read - without waiting.

        A vision read takes seconds; a keystroke takes milliseconds. So the
        read runs alongside the work, and what it saw is reported when it is
        ready. Nothing waits on it unless something has gone wrong.
        """
        if self.vision is None:
            return
        import asyncio

        from tallyagent_agent.perception.focus import ChangeWatcher

        if self._watcher is None:
            self._watcher = ChangeWatcher()
        if self._watcher.observe(png) == "same":
            return
        if self._seeing is not None and not self._seeing.done():
            return

        async def look() -> None:
            state = await self.vision.read(png)
            self.last_seen = state
            if state.describe():
                self._event("see", state.describe())

        try:
            self._seeing = asyncio.get_running_loop().create_task(look())
        except RuntimeError:
            self._seeing = None

    async def look(self) -> Any:
        """Read the screen now, and wait for the answer."""
        if self.vision is None:
            return None
        frame = self._frame()
        if frame is None:
            return None
        state = await self.vision.read(frame[0])
        self.last_seen = state
        if state.describe():
            self._event("see", state.describe())
        return state

    def _interrupted(self, run: UiRun) -> UiRun:
        """Stop on a screen nobody asked for, and back out of it."""
        seen = self.last_seen.describe() if self.last_seen is not None else ""
        run.stopped = (
            "Tally opened another screen mid-voucher "
            f"({seen or self._screen_name()[:80]!r}), so the rest was not typed"
        )
        self.keyboard.press("escape")
        return run

    async def _perform(self, run: UiRun, step: UiStep) -> bool:
        """Do one voucher step; on refusal or failure, say so and back out.

        Escaping leaves Tally without a half-typed voucher, rather than one a
        person has to notice and clear by hand.
        """
        if await self.step(step):
            return True
        run.stopped = (
            f"refused at {step.describe()}"
            if step.refused
            else f"could not perform {step.describe()}"
        )
        self.keyboard.press("escape")
        return False


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


def _ocr_boxes(png: bytes) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Every line of text on the image, with its box: (text, (x0, y0, x1, y1))."""
    global _OCR
    import io  # noqa: PLC0415

    import numpy  # noqa: PLC0415
    from PIL import Image as PilImage  # noqa: PLC0415

    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-untyped]  # noqa: PLC0415

        _OCR = RapidOCR()
    image = PilImage.open(io.BytesIO(png)).convert("RGB")
    # Read at half size. Tally's labels are large enough to survive it, and a
    # full 2560x1600 window took four seconds a read - most of every step.
    scale = 0.5 if image.width > 1600 else 1.0
    if scale != 1.0:
        image = image.resize((int(image.width * scale), int(image.height * scale)))
    found, _ = _OCR(numpy.array(image))
    out = []
    for box, text, _score in found or []:
        xs = [point[0] / scale for point in box]
        ys = [point[1] / scale for point in box]
        out.append((str(text), (min(xs), min(ys), max(xs), max(ys))))
    return out


def _squash(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def grab_window() -> tuple[bytes, Any] | None:
    """The Tally window's own pixels (PrintWindow) and where it is on screen."""
    from tallyagent_agent.perception.screen import capture, find_tally_window

    bounds = find_tally_window()
    if bounds is None:
        return None
    return capture(bounds), bounds


def locate_on_screen(label: str) -> tuple[int, int] | None:
    """Where ``label`` is drawn in the Tally window, in screen coordinates.

    Reads the window through PrintWindow, so it works whether or not Tally is
    in front. The topmost match wins - Tally's field labels sit above the
    rows of figures that might repeat the same word.
    """
    from tallyagent_agent.perception.screen import capture, find_tally_window

    bounds = find_tally_window()
    if bounds is None:
        return None
    wanted = _squash(label)
    matches = [
        box for text, box in _ocr_boxes(capture(bounds)) if wanted and wanted in _squash(text)
    ]
    if not matches:
        return None
    x0, y0, x1, y1 = min(matches, key=lambda b: (b[1], b[0]))
    return int(bounds.left + (x0 + x1) / 2), int(bounds.top + (y0 + y1) / 2)


_HEADER_CACHE: dict[str, str] = {}


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
        import hashlib  # noqa: PLC0415
        import io  # noqa: PLC0415

        from PIL import Image as PilImage  # noqa: PLC0415

        from tallyagent_agent.perception.screen import capture, find_tally_window

        bounds = find_tally_window()
        if bounds is None:
            return ""
        png = capture(bounds)
        image = PilImage.open(io.BytesIO(png)).convert("RGB")
        header = image.crop((0, 0, image.width, max(1, image.height // 5)))
        # The header is the same pixels for most of a voucher; OCR is a second
        # or two. Read it again only when it has changed.
        key = hashlib.sha1(header.tobytes()).hexdigest()
        if _HEADER_CACHE.get("key") == key:
            return _HEADER_CACHE["text"]
        buffer = io.BytesIO()
        header.save(buffer, format="PNG")
        text = " ".join(_ocr_lines(buffer.getvalue()))
        _HEADER_CACHE.update(key=key, text=text)
        return text
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
