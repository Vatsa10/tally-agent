"""Driving Tally's own screens, without a Tally or a screen.

Everything that decides whether a keystroke is sent - which screen is open,
whether a person approved it, what happens when they refuse halfway - is tested
here. What is not tested is whether Tally accepts the keys, which is what the
live rehearsal is for.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from tallyagent_agent.fallback.tally_ui import TallyUi, UiStep


class FakeKeyboard:
    def __init__(self, focusable: bool = True) -> None:
        self.typed: list[str] = []
        self.pressed: list[str] = []
        self.focused: list[str] = []
        self.focusable = focusable

    def type(self, text: str, interval: float = 0.0) -> None:
        self.typed.append(text)

    def press(self, key: str) -> None:
        self.pressed.append(key)

    def focus(self, title: str) -> bool:
        self.focused.append(title)
        return self.focusable


class FakeSpotlight:
    def __init__(self) -> None:
        self.said: list[str] = []
        self.points: list[tuple[int, int]] = []

    def announce(self, words: str) -> None:
        self.said.append(words)

    def point(self, x: int, y: int, caption: str = "") -> None:
        self.points.append((x, y))


async def _nosleep(seconds: float) -> None:
    return None


def _ui(screen: str = "Payment", focusable: bool = True, approve=None):  # type: ignore[no-untyped-def]
    keyboard = FakeKeyboard(focusable=focusable)
    spotlight = FakeSpotlight()
    ui = TallyUi(
        keyboard=keyboard,
        spotlight=spotlight,
        approve=approve,
        sleep=_nosleep,
        screen_name=lambda: screen,
    )
    return ui, keyboard, spotlight


async def yes(step: UiStep) -> bool:
    return True


async def no(step: UiStep) -> bool:
    return False


# --- navigating -------------------------------------------------------------


async def test_screens_are_opened_by_name_not_by_menu_letter():
    """A menu letter means different things on different screens."""
    ui, keyboard, _ = _ui(screen="Day Book")

    await ui.go_to("Day Book")

    assert keyboard.typed == ["Day Book"]
    assert keyboard.pressed == ["alt+g", "enter"]


async def test_nothing_is_attempted_when_tally_cannot_be_raised():
    ui, keyboard, _ = _ui(focusable=False)

    assert await ui.go_to("Day Book") is False
    assert keyboard.typed == []
    assert keyboard.pressed == []


# --- knowing which screen is open -------------------------------------------


async def test_the_open_screen_is_checked_before_anything_is_typed():
    ui, _, _ = _ui(screen="Payment Voucher Creation")
    assert await ui.expect_screen("Payment") is True


async def test_a_different_screen_is_not_typed_into():
    """Without this an amount goes into whatever has focus."""
    ui, _, _ = _ui(screen="Gateway of Tally")
    assert await ui.expect_screen("Payment") is False


async def test_an_unreadable_screen_counts_as_the_wrong_screen():
    ui, _, _ = _ui(screen="")
    assert await ui.expect_screen("Day Book") is False


# --- entering a voucher -----------------------------------------------------


async def test_a_payment_is_keyed_field_by_field_and_accepted():
    ui, keyboard, _ = _ui(screen="Payment", approve=yes)

    run = await ui.enter_payment(
        ledger="Bank - HDFC 1234",
        amount=Decimal("2500.00"),
        when=date(2026, 6, 1),
        narration="June rent",
    )

    assert run.completed, run.stopped
    assert keyboard.typed == [
        "Payment",
        "01-06-2026",
        "Bank - HDFC 1234",
        "2500.00",
        "June rent",
    ]
    assert keyboard.pressed[-1] == "ctrl+a", "accepted last, and only last"


async def test_the_wrong_screen_stops_it_before_a_single_field():
    ui, keyboard, _ = _ui(screen="Gateway of Tally", approve=yes)

    run = await ui.enter_payment("Cash", Decimal("100"), date(2026, 6, 1))

    assert not run.completed
    assert "nothing was typed" in run.stopped
    assert keyboard.typed == ["Payment"], "only the Go To, no voucher fields"


async def test_refusing_the_accept_leaves_the_voucher_unposted():
    """Refusing halfway must leave a half-typed voucher Tally never took, not a
    posted one somebody has to find and delete."""
    ui, keyboard, _ = _ui(screen="Payment", approve=no)

    run = await ui.enter_payment("Cash", Decimal("100"), date(2026, 6, 1))

    assert not run.completed
    assert "refused" in run.stopped
    assert "ctrl+a" not in keyboard.pressed
    assert keyboard.pressed[-1] == "escape", "and it backs out of the screen"


async def test_without_an_approver_nothing_is_ever_accepted():
    """Visible is not the same as permitted."""
    ui, keyboard, _ = _ui(screen="Payment", approve=None)

    run = await ui.enter_payment("Cash", Decimal("100"), date(2026, 6, 1))

    assert not run.completed
    assert "ctrl+a" not in keyboard.pressed


async def test_only_the_accept_asks_for_approval():
    """Typing a date is not a change; accepting the voucher is."""
    asked: list[str] = []

    async def record(step: UiStep) -> bool:
        asked.append(step.what)
        return True

    ui, _, _ = _ui(screen="Payment", approve=record)
    await ui.enter_payment("Cash", Decimal("100"), date(2026, 6, 1))

    assert asked == ["accept the voucher"]


async def test_every_step_is_narrated_before_it_happens():
    ui, _, spotlight = _ui(screen="Payment", approve=yes)

    await ui.enter_payment("Cash", Decimal("100"), date(2026, 6, 1))

    assert any("opening Payment" in said for said in spotlight.said)
    assert any("accept the voucher" in said for said in spotlight.said)


async def test_a_step_reads_as_a_sentence_a_person_can_refuse():
    assert UiStep("the amount", text="2500.00").describe() == (
        'the amount: type "2500.00"'
    )
    assert UiStep("accept the voucher", keys=["ctrl+a"]).describe() == (
        "accept the voucher: ctrl+a"
    )


# --- showing without touching ------------------------------------------------


async def test_showing_a_voucher_changes_nothing():
    ui, keyboard, spotlight = _ui(screen="Day Book")

    run = await ui.show_voucher("12", date(2026, 6, 2))

    assert run.completed
    assert keyboard.pressed == ["alt+g", "enter"], "navigation only"
    assert spotlight.points, "and it points at the rows"


async def test_the_report_says_where_it_stopped():
    ui, _, _ = _ui(screen="Gateway of Tally", approve=yes)
    run = await ui.enter_payment("Cash", Decimal("100"), date(2026, 6, 1))

    assert "stopped after 0 step(s)" in run.report()
