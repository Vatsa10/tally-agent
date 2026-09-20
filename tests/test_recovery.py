"""Putting Tally back without a person.

Three things end a real session: Tally crashes, Tally drops to its licence
screen, or Tally is up with no company open. All three happened repeatedly
while this was being built, and each one used to mean stopping to run a script.
"""

from __future__ import annotations

import httpx
import pytest

from tallyagent_core.errors import ConnectionError_
from tallyagent_tally.client import TallyClient, TallyConfig
from tallyagent_tally.recovery import Recovery


class FakeRecovery:
    """Stands in for the real repairer, counting how often it is asked."""

    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy
        self.calls = 0

    async def repair(self, force: bool = False):  # type: ignore[no-untyped-def]
        from tallyagent_tally.recovery import Repair

        self.calls += 1
        return Repair(healthy=self.healthy, actions=["restarted TallyPrime"])


def _dead_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    return httpx.MockTransport(handler)


def _flaky_transport(fail_times: int) -> httpx.MockTransport:
    state = {"failures": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["failures"] < fail_times:
            state["failures"] += 1
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, content=b"<ENVELOPE/>")

    return httpx.MockTransport(handler)


# --- the client's side ------------------------------------------------------


async def test_a_dropped_connection_is_repaired_once_and_retried():
    healer = FakeRecovery(healthy=True)
    client = TallyClient(
        TallyConfig(host="127.0.0.1"),
        transport=_flaky_transport(fail_times=1),
        recovery=healer,
    )

    assert await client.post(b"<ENVELOPE/>") == b"<ENVELOPE/>"
    assert healer.calls == 1


async def test_a_repair_that_does_not_help_raises_the_original_error():
    """Better to fail with the connection error than to hide it behind a retry."""
    healer = FakeRecovery(healthy=False)
    client = TallyClient(
        TallyConfig(host="127.0.0.1"), transport=_dead_transport(), recovery=healer
    )

    with pytest.raises(ConnectionError_):
        await client.post(b"<ENVELOPE/>")
    assert healer.calls == 1


async def test_the_retry_happens_once_and_never_becomes_a_loop():
    healer = FakeRecovery(healthy=True)
    client = TallyClient(
        TallyConfig(host="127.0.0.1"), transport=_dead_transport(), recovery=healer
    )

    with pytest.raises(ConnectionError_):
        await client.post(b"<ENVELOPE/>")
    assert healer.calls == 1, "a repaired-but-still-dead Tally must not spin"


async def test_without_a_repairer_nothing_is_restarted():
    """The fake and every test run this way: no process is ever touched."""
    client = TallyClient(TallyConfig(host="127.0.0.1"), transport=_dead_transport())

    with pytest.raises(ConnectionError_):
        await client.post(b"<ENVELOPE/>")


async def test_a_failing_repairer_does_not_mask_the_connection_error():
    class Broken:
        async def repair(self, force: bool = False):  # type: ignore[no-untyped-def]
            raise RuntimeError("pywin32 missing")

    client = TallyClient(
        TallyConfig(host="127.0.0.1"), transport=_dead_transport(), recovery=Broken()
    )

    with pytest.raises(ConnectionError_):
        await client.post(b"<ENVELOPE/>")


# --- the repairer's own decisions -------------------------------------------


class StubRecovery(Recovery):
    """Recovery with the desktop taken out: no process, no keystrokes."""

    def __init__(self, answers: list[bool], companies: list[list[str]], **kw) -> None:  # type: ignore[no-untyped-def]
        super().__init__(TallyConfig(host="127.0.0.1"), **kw)
        self._answers = answers
        self._companies = companies
        self.presses = 0

    async def port_answers(self) -> bool:
        return self._answers.pop(0) if self._answers else True

    async def companies(self) -> list[str]:
        return self._companies.pop(0) if self._companies else []


async def test_an_open_company_needs_no_repair_at_all():
    healer = StubRecovery(answers=[True], companies=[["TA-Demo Traders"]])

    result = await healer.repair()

    assert result.healthy
    assert result.actions == []
    assert result.describe() == "Tally was already answering"


async def test_a_port_that_answers_with_no_company_is_not_healthy(monkeypatch):
    """The worst case: reads succeed and come back empty, which looks like an
    empty company rather than like a broken session."""
    monkeypatch.setattr("tallyagent_tally.recovery.press_in_tally", lambda key: False)
    healer = StubRecovery(answers=[True], companies=[[], []])

    result = await healer.repair()

    assert not result.healthy
    assert "no company is open" in result.reason


async def test_the_licence_screen_is_cleared_and_then_rechecked(monkeypatch):
    pressed: list[str] = []
    monkeypatch.setattr(
        "tallyagent_tally.recovery.press_in_tally",
        lambda key: pressed.append(key) or True,
    )
    monkeypatch.setattr("tallyagent_tally.recovery.SETTLE_SECONDS", 0)
    healer = StubRecovery(answers=[True], companies=[[], ["TA-Demo Traders"]])

    result = await healer.repair()

    assert result.healthy
    assert pressed == ["t"]
    assert "cleared the licence screen" in result.actions


async def test_a_second_repair_straight_away_is_refused():
    """Otherwise a failing request repairs, fails, repairs, for ever."""
    healer = StubRecovery(answers=[True], companies=[["TA-Demo Traders"]])
    await healer.repair()

    again = await healer.repair()

    assert not again.healthy
    assert "already tried a moment ago" in again.reason


async def test_the_cooldown_can_be_overridden_when_a_person_asked():
    """`tallyagent doctor` is a person asking; it should not be told to wait."""
    healer = StubRecovery(
        answers=[True, True], companies=[["TA-Demo Traders"], ["TA-Demo Traders"]]
    )
    await healer.repair()

    again = await healer.repair(force=True)

    assert again.healthy


async def test_a_machine_without_tally_installed_says_so(monkeypatch):
    monkeypatch.setattr("tallyagent_tally.install.find_install", lambda: None)
    healer = StubRecovery(answers=[False], companies=[[]])

    result = await healer.repair()

    assert not result.healthy
    assert "does not appear to be installed" in result.reason
