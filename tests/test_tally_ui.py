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


def _ui(screen: str = "Payment Voucher Creation", focusable: bool = True, approve=None):  # type: ignore[no-untyped-def]
    keyboard = FakeKeyboard(focusable=focusable)
    spotlight = FakeSpotlight()

    def screen_name() -> str:
        # F2 opens Tally's date sub-screen; the fake says so, because the code
        # under test has to tell that one apart from an unexpected sub-screen.
        if keyboard.pressed and keyboard.pressed[-1] == "f2":
            return "Change Voucher Date"
        if keyboard.pressed and keyboard.pressed[-1] == "alt+g":
            return "List of Reports"
        return screen

    ui = TallyUi(
        keyboard=keyboard,
        spotlight=spotlight,
        approve=approve,
        sleep=_nosleep,
        screen_name=screen_name,
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
    ui, keyboard, _ = _ui(screen="Payment Voucher Creation", approve=yes)

    run = await ui.enter_payment(
        from_ledger="Bank - HDFC 1234",
        expense_ledger="Rent",
        amount=Decimal("2500.00"),
        when=date(2026, 6, 1),
        narration="June rent",
    )

    assert run.completed, run.stopped
    assert keyboard.typed == [
        "Create Voucher",
        "01-06-2026",
        "Bank - HDFC 1234",
        "Rent",
        "2500.00",
        "June rent",
    ]
    assert keyboard.pressed[-1] == "ctrl+a", "accepted last, and only last"


async def test_the_wrong_screen_stops_it_before_a_single_field():
    ui, keyboard, _ = _ui(screen="Gateway of Tally", approve=yes)

    run = await ui.enter_payment("Cash", "Rent", Decimal("100"), date(2026, 6, 1))

    assert not run.completed
    assert "nothing was typed" in run.stopped
    assert keyboard.typed == ["Create Voucher"], "only the Go To, no fields"


async def test_refusing_the_accept_leaves_the_voucher_unposted():
    """Refusing halfway must leave a half-typed voucher Tally never took, not a
    posted one somebody has to find and delete."""
    ui, keyboard, _ = _ui(screen="Payment Voucher Creation", approve=no)

    run = await ui.enter_payment("Cash", "Rent", Decimal("100"), date(2026, 6, 1))

    assert not run.completed
    assert "refused" in run.stopped
    assert "ctrl+a" not in keyboard.pressed
    assert keyboard.pressed[-1] == "escape", "and it backs out of the screen"


async def test_without_an_approver_nothing_is_ever_accepted():
    """Visible is not the same as permitted."""
    ui, keyboard, _ = _ui(screen="Payment Voucher Creation", approve=None)

    run = await ui.enter_payment("Cash", "Rent", Decimal("100"), date(2026, 6, 1))

    assert not run.completed
    assert "ctrl+a" not in keyboard.pressed


async def test_only_the_accept_asks_for_approval():
    """Typing a date is not a change; accepting the voucher is."""
    asked: list[str] = []

    async def record(step: UiStep) -> bool:
        asked.append(step.what)
        return True

    ui, _, _ = _ui(screen="Payment Voucher Creation", approve=record)
    await ui.enter_payment("Cash", "Rent", Decimal("100"), date(2026, 6, 1))

    assert asked == ["accept the voucher"]


async def test_every_step_is_narrated_before_it_happens():
    ui, _, spotlight = _ui(screen="Payment Voucher Creation", approve=yes)

    await ui.enter_payment("Cash", "Rent", Decimal("100"), date(2026, 6, 1))

    assert any("opening Create Voucher" in said for said in spotlight.said)
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
    run = await ui.enter_payment("Cash", "Rent", Decimal("100"), date(2026, 6, 1))

    assert "stopped after 0 step(s)" in run.report()


async def test_a_sub_screen_tally_opened_by_itself_stops_the_voucher():
    """A ledger with cost centres on it pops a Cost Allocation screen, and the
    next field would be typed into that instead."""
    keyboard = FakeKeyboard()

    def screen_name() -> str:
        # Everything is normal until the date field, and then Tally is
        # somewhere nobody asked for.
        if keyboard.pressed and keyboard.pressed[-1] == "alt+g":
            return "List of Reports"
        if "f2" in keyboard.pressed:
            return "Cost Centre Creation"
        return "Payment Voucher Creation"

    ui = TallyUi(
        keyboard=keyboard,
        approve=yes,
        sleep=_nosleep,
        screen_name=screen_name,
    )

    run = await ui.enter_payment("Cash", "Rent", Decimal("100"), date(2026, 6, 1))

    assert not run.completed
    assert "another screen mid-voucher" in run.stopped
    assert "ctrl+a" not in keyboard.pressed


def _with_allocation(voucher: str = "Payment Voucher Creation"):  # type: ignore[no-untyped-def]
    """A Tally that interrupts the voucher with a cost allocation screen once
    the amount has been keyed, the way a cost-centre ledger does."""
    keyboard = FakeKeyboard()

    def screen_name() -> str:
        if keyboard.pressed and keyboard.pressed[-1] == "f2":
            return "Change Voucher Date"
        if keyboard.pressed and keyboard.pressed[-1] == "alt+g":
            return "List of Reports"
        if len(keyboard.typed) >= 5 and "Admin" not in keyboard.typed:
            return "Cost Allocations"
        return voucher

    return keyboard, screen_name


async def test_a_ledger_with_cost_centres_is_allocated_then_accepted():
    keyboard, screen_name = _with_allocation()
    ui = TallyUi(
        keyboard=keyboard, approve=yes, sleep=_nosleep, screen_name=screen_name
    )

    run = await ui.enter_payment(
        "Cash", "Bank Charges", Decimal("150"), date(2026, 6, 2), cost_centre="Admin"
    )

    assert run.completed, run.stopped
    assert "Admin" in keyboard.typed
    assert keyboard.pressed[-1] == "ctrl+a"


async def test_without_a_cost_centre_the_voucher_is_left_unposted():
    keyboard, screen_name = _with_allocation()
    ui = TallyUi(
        keyboard=keyboard, approve=yes, sleep=_nosleep, screen_name=screen_name
    )

    run = await ui.enter_payment(
        "Cash", "Bank Charges", Decimal("150"), date(2026, 6, 2)
    )

    assert not run.completed
    assert "cost centres" in run.stopped
    assert "ctrl+a" not in keyboard.pressed


# --- receipts and journals --------------------------------------------------


async def test_a_receipt_is_keyed_into_the_bank_then_from_the_party():
    ui, keyboard, _ = _ui(screen="Receipt Voucher Creation", approve=yes)

    run = await ui.enter_receipt(
        into_ledger="Bank - HDFC 1234",
        from_ledger="Sharma Traders",
        amount=Decimal("11800.00"),
        when=date(2026, 6, 3),
        narration="against bill 42",
    )

    assert run.completed, run.stopped
    assert "f6" in keyboard.pressed and "f5" not in keyboard.pressed
    assert keyboard.typed == [
        "Create Voucher",
        "03-06-2026",
        "Bank - HDFC 1234",
        "Sharma Traders",
        "11800.00",
        "against bill 42",
    ]
    assert keyboard.pressed[-1] == "ctrl+a"


async def test_a_journal_names_each_side_before_its_ledger_and_amount():
    ui, keyboard, _ = _ui(screen="Journal Voucher Creation", approve=yes)

    run = await ui.enter_journal(
        debit_ledger="Depreciation",
        credit_ledger="Furniture",
        amount=Decimal("5000.00"),
        when=date(2026, 6, 30),
        narration="June depreciation",
    )

    assert run.completed, run.stopped
    assert "f7" in keyboard.pressed
    assert keyboard.typed == [
        "Create Voucher",
        "30-06-2026",
        "Dr",
        "Depreciation",
        "5000.00",
        "Cr",
        "Furniture",
        "5000.00",
        "June depreciation",
    ]
    assert keyboard.pressed[-1] == "ctrl+a"


async def test_a_receipt_on_the_wrong_voucher_type_types_no_field():
    """F6 that did not take leaves Tally on whatever type it was on."""
    ui, keyboard, _ = _ui(screen="Payment Voucher Creation", approve=yes)

    run = await ui.enter_receipt("Bank", "Sharma Traders", Decimal("100"), date(2026, 6, 1))

    assert not run.completed
    assert "Receipt" in run.stopped and "nothing was typed" in run.stopped
    assert keyboard.typed == ["Create Voucher"]


async def test_a_journal_on_the_wrong_voucher_type_types_no_field():
    ui, keyboard, _ = _ui(screen="Receipt Voucher Creation", approve=yes)

    run = await ui.enter_journal("Depreciation", "Furniture", Decimal("100"), date(2026, 6, 1))

    assert not run.completed
    assert "Journal" in run.stopped
    assert keyboard.typed == ["Create Voucher"]


async def test_a_receipt_into_a_cost_centre_ledger_is_allocated_then_accepted():
    keyboard, screen_name = _with_allocation("Receipt Voucher Creation")
    ui = TallyUi(keyboard=keyboard, approve=yes, sleep=_nosleep, screen_name=screen_name)

    run = await ui.enter_receipt(
        "Bank", "Consulting Income", Decimal("900"), date(2026, 6, 2), cost_centre="Admin"
    )

    assert run.completed, run.stopped
    assert keyboard.typed[-2:] == ["Admin", "900"], "the centre, then the whole amount"
    assert keyboard.pressed[-1] == "ctrl+a"


async def test_a_receipt_without_its_cost_centre_is_left_unposted():
    keyboard, screen_name = _with_allocation("Receipt Voucher Creation")
    ui = TallyUi(keyboard=keyboard, approve=yes, sleep=_nosleep, screen_name=screen_name)

    run = await ui.enter_receipt("Bank", "Consulting Income", Decimal("900"), date(2026, 6, 2))

    assert not run.completed
    assert "Consulting Income is allocated to cost centres" in run.stopped
    assert "ctrl+a" not in keyboard.pressed


async def test_refusing_a_receipt_or_journal_leaves_ctrl_a_unpressed():
    for screen, enter in (
        ("Receipt Voucher Creation", "enter_receipt"),
        ("Journal Voucher Creation", "enter_journal"),
    ):
        ui, keyboard, _ = _ui(screen=screen, approve=no)

        run = await getattr(ui, enter)("Bank", "Sharma", Decimal("100"), date(2026, 6, 1))

        assert not run.completed
        assert "refused" in run.stopped
        assert "ctrl+a" not in keyboard.pressed
        assert keyboard.pressed[-1] == "escape"


async def test_every_receipt_and_journal_step_is_narrated():
    for screen, enter in (
        ("Receipt Voucher Creation", "enter_receipt"),
        ("Journal Voucher Creation", "enter_journal"),
    ):
        ui, _, spotlight = _ui(screen=screen, approve=yes)

        run = await getattr(ui, enter)("Bank", "Sharma", Decimal("100"), date(2026, 6, 1))

        assert run.completed, run.stopped
        for step in run.steps:
            assert step.describe() in spotlight.said, step.what


def _allocating_after(typed_count: int, voucher: str):  # type: ignore[no-untyped-def]
    """A Tally that opens a cost allocation screen once ``typed_count`` texts
    have gone in - the point at which a cost-centre ledger's amount lands."""
    keyboard = FakeKeyboard()

    def screen_name() -> str:
        if keyboard.pressed and keyboard.pressed[-1] == "f2":
            return "Change Voucher Date"
        if keyboard.pressed and keyboard.pressed[-1] == "alt+g":
            return "List of Reports"
        if len(keyboard.typed) >= typed_count:
            return "Cost Allocations for Furniture"
        return voucher

    ui = TallyUi(keyboard=keyboard, approve=yes, sleep=_nosleep, screen_name=screen_name)
    return ui, keyboard


async def test_a_journal_credit_ledger_with_cost_centres_is_named_when_it_stops():
    # Create Voucher, date, Dr, Depreciation, 100, Cr, Furniture, 100
    ui, keyboard = _allocating_after(8, "Journal Voucher Creation")

    run = await ui.enter_journal("Depreciation", "Furniture", Decimal("100"), date(2026, 6, 1))

    assert not run.completed
    assert run.stopped.startswith("Furniture is allocated to cost centres"), run.stopped
    assert "ctrl+a" not in keyboard.pressed


async def test_a_journal_debit_ledger_with_cost_centres_stops_before_the_credit_line():
    # Create Voucher, date, Dr, Depreciation, 100 - then the allocation screen
    ui, keyboard = _allocating_after(5, "Journal Voucher Creation")

    run = await ui.enter_journal("Depreciation", "Furniture", Decimal("100"), date(2026, 6, 1))

    assert not run.completed
    assert "another screen" in run.stopped
    assert "Cr" not in keyboard.typed and "Furniture" not in keyboard.typed
    assert "ctrl+a" not in keyboard.pressed
    assert keyboard.pressed[-1] == "escape"
