"""Opening a screen in the user's Tally, without a Tally."""

from __future__ import annotations

from datetime import date

import pytest

from tallyagent_tools import show
from tallyagent_tools.base import ToolContext


class FakeUi:
    def __init__(self, opens: bool = True) -> None:
        self.opened: list[str] = []
        self.rung: list[str] = []
        self.opens = opens

    async def go_to(self, report: str) -> bool:
        self.opened.append(report)
        return self.opens

    async def expect_screen(self, report: str) -> bool:
        return self.opens

    async def show_voucher(self, number: str, when: date):  # type: ignore[no-untyped-def]
        from tallyagent_agent.fallback.tally_ui import UiRun

        self.rung.append(number)
        return UiRun(task=f"show voucher {number}", completed=self.opens)


def _ctx() -> ToolContext:
    return ToolContext(backend=None, company=None)  # type: ignore[arg-type]


@pytest.fixture
def ui(monkeypatch):  # type: ignore[no-untyped-def]
    fake = FakeUi()
    monkeypatch.setattr(show, "build_ui", lambda ctx: fake)
    return fake


async def test_it_opens_the_report_by_its_tally_name(ui):  # type: ignore[no-untyped-def]
    result = await show.show_in_tally(_ctx(), "stock summary")

    assert ui.opened == ["Stock Summary"]
    assert result.data["opened"] is True


async def test_a_voucher_number_rings_the_row_in_the_day_book(ui):  # type: ignore[no-untyped-def]
    await show.show_in_tally(_ctx(), "day book", "12", date(2026, 6, 2))

    assert ui.rung == ["12"]


async def test_an_unknown_screen_is_not_typed_into_go_to(ui):  # type: ignore[no-untyped-def]
    """Go To matches "Delete Company" as happily as anything else."""
    result = await show.show_in_tally(_ctx(), "delete company")

    assert ui.opened == []
    assert "not one of them" in result.message


async def test_with_cursor_mode_off_it_says_so_rather_than_failing(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(show, "build_ui", lambda ctx: None)

    result = await show.show_in_tally(_ctx(), "day book")

    assert "/tier3 on" in result.message


async def test_cursor_mode_needs_the_tier_switch_not_just_a_fallback():
    class Tiers:
        class config:  # noqa: N801
            fallback_enabled = False

    ctx = _ctx()
    ctx.fallback = type("F", (), {"tiers": Tiers})()
    assert show.build_ui(ctx) is None
