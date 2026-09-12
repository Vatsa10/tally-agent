"""Tier 3, made visible: the red cursor, the captions, and getting Tally up.

No window is opened here. The overlay is one sink among several, and every
decision - what is said, in what order, whether an action is sent at all - is
made above it, which is what these test.
"""

from __future__ import annotations

from tallyagent_agent.fallback import spotlight as spot
from tallyagent_agent.fallback import window as win
from tallyagent_agent.fallback.computer_use import ComputerUseFallback, FallbackStep
from tallyagent_agent.perception.screen import WindowBounds
from tallyagent_agent.tiers import TierConfig, TierRouter
from tallyagent_llm import mock
from tallyagent_llm.router import Router


class Recorder:
    """A sink that keeps the beats instead of drawing them."""

    def __init__(self) -> None:
        self.beats: list[spot.Beat] = []
        self.closed = False

    def send(self, beat: spot.Beat) -> None:
        self.beats.append(beat)

    def close(self) -> None:
        self.closed = True


def _spotlight(sink: Recorder) -> tuple[spot.Spotlight, list[str], list[tuple[int, int]]]:
    keys: list[str] = []
    clicks: list[tuple[int, int]] = []
    light = spot.Spotlight(
        sink=sink,
        do_key=keys.append,
        do_click=lambda x, y: clicks.append((x, y)),
        travel_seconds=0,
        sleep=lambda _seconds: None,
    )
    return light, keys, clicks


# --- captions ---------------------------------------------------------------


def test_a_dangerous_keystroke_is_spelled_out():
    assert spot.describe("key", "Alt+D") == "press Alt+D - delete the selected voucher"


def test_an_unknown_key_is_shown_as_typed_not_guessed_at():
    assert spot.describe("key", "ctrl+shift+k") == "press ctrl+shift+k"


def test_the_reason_rides_along_with_the_action():
    caption = spot.describe("key", "Alt+D", "removing the duplicate receipt")
    assert caption.endswith("(removing the duplicate receipt)")


def test_a_click_names_where_it_is_going():
    assert spot.describe("click", "410,265").startswith("click at 410,265")


# --- the wrapper ------------------------------------------------------------


def test_a_keystroke_is_narrated_then_sent():
    sink = Recorder()
    light, keys, _ = _spotlight(sink)
    light.announce("opening the day book")
    light.key("alt+d")

    assert keys == ["alt+d"], "the real keystroke still goes out"
    assert [b.kind for b in sink.beats] == ["key"]
    assert "delete the selected voucher" in sink.beats[0].caption
    assert "opening the day book" in sink.beats[0].caption


def test_the_ring_arrives_before_the_button_goes_down():
    """A click that is drawn after the fact tells you nothing you can stop."""
    sink = Recorder()
    light, _, clicks = _spotlight(sink)
    light.click(300, 200)

    assert [b.kind for b in sink.beats] == ["move", "click"]
    assert all((b.x, b.y) == (300, 200) for b in sink.beats)
    assert clicks == [(300, 200)]


def test_narration_failing_never_stops_the_action():
    """The overlay is cosmetic; an approved keystroke is not."""

    class Broken:
        def send(self, beat: spot.Beat) -> None:
            raise RuntimeError("no display")

        def close(self) -> None:
            pass

    keys: list[str] = []
    clicks: list[tuple[int, int]] = []
    light = spot.Spotlight(
        sink=Broken(),  # type: ignore[arg-type]
        do_key=keys.append,
        do_click=lambda x, y: clicks.append((x, y)),
        travel_seconds=0,
        sleep=lambda _s: None,
    )
    light.key("enter")
    light.click(10, 20)
    assert keys == ["enter"]
    assert clicks == [(10, 20)]


def test_the_log_sink_is_what_a_headless_machine_gets():
    sink = spot.LogSink()
    light = spot.Spotlight(
        sink=sink, do_key=lambda _k: None, travel_seconds=0, sleep=lambda _s: None
    )
    light.key("f9")
    assert [b.kind for b in sink.beats] == ["key"]


# --- getting Tally in front -------------------------------------------------


BOUNDS = WindowBounds(0, 0, 800, 600, "TallyPrime:9000")


def test_a_window_already_up_is_just_raised():
    calls: list[str] = []
    bounds, reason = win.ensure_visible(
        locate=lambda: BOUNDS,
        launch=lambda: calls.append("launch") or True,
        raise_window=lambda _b: calls.append("raise") or True,
        sleep=lambda _s: None,
    )
    assert (bounds, reason) == (BOUNDS, "")
    assert calls == ["raise"], "nothing is started when Tally is already there"


def test_tally_is_started_when_it_is_not_running():
    seen: list[str] = []
    windows = iter([None, None, BOUNDS])

    bounds, reason = win.ensure_visible(
        locate=lambda: next(windows, BOUNDS),
        launch=lambda: seen.append("launch") or True,
        raise_window=lambda _b: True,
        sleep=lambda _s: None,
    )
    assert seen == ["launch"]
    assert bounds == BOUNDS
    assert reason == ""


def test_nothing_is_sent_when_tally_cannot_be_brought_to_the_front():
    """Keystrokes land on whatever has focus, so this has to be fatal."""
    bounds, reason = win.ensure_visible(
        locate=lambda: BOUNDS,
        launch=lambda: True,
        raise_window=lambda _b: False,
        sleep=lambda _s: None,
    )
    assert bounds is None
    assert "would go to whatever is in front" in reason


def test_a_missing_installation_is_reported_not_retried_forever():
    bounds, reason = win.ensure_visible(
        locate=lambda: None,
        launch=lambda: False,
        raise_window=lambda _b: True,
        sleep=lambda _s: None,
    )
    assert bounds is None
    assert "not installed" in reason


# --- the two together -------------------------------------------------------


def _fallback(sink: Recorder, approve=None, ensure=None):  # type: ignore[no-untyped-def]
    router = Router(
        mock.MockProvider(
            script=[
                mock.text('{"action": "key", "key": "f9", "why": "open a purchase"}'),
                mock.text('{"action": "done", "why": "finished"}'),
            ]
        )
    )
    light, keys, _ = _spotlight(sink)
    fallback = ComputerUseFallback(
        router,
        TierRouter(TierConfig(fallback_enabled=True, fallback_max_steps=4)),
        approve=approve,
        locate=lambda: BOUNDS,
        grab=lambda _b: b"png",
        do_key=lambda k: keys.append(k),
        do_click=lambda x, y: None,
        spotlight=light,
        ensure_visible=ensure,
    )
    return fallback, keys


async def test_the_fallback_narrates_every_step_it_takes():
    async def allow(_step: FallbackStep) -> bool:
        return True

    sink = Recorder()
    fallback, keys = _fallback(sink, approve=allow)
    session = await fallback.run("open a purchase voucher")

    assert session.completed
    assert keys == ["f9"], "the keystroke went out once"
    assert [b.kind for b in sink.beats] == ["key"]
    assert "purchase" in sink.beats[0].caption


async def test_a_rejected_step_is_never_narrated_as_done():
    """What is drawn on screen must match what actually happened."""

    async def refuse(_step: FallbackStep) -> bool:
        return False

    sink = Recorder()
    router = Router(
        mock.MockProvider(
            script=[mock.text('{"action": "key", "key": "enter", "why": "accept"}')]
        )
    )
    light, keys, _ = _spotlight(sink)
    fallback = ComputerUseFallback(
        router,
        TierRouter(TierConfig(fallback_enabled=True)),
        approve=refuse,
        locate=lambda: BOUNDS,
        grab=lambda _b: b"png",
        do_key=keys.append,
        spotlight=light,
    )
    session = await fallback.run("save the voucher")

    assert not session.completed
    assert keys == [], "a rejected keystroke is not sent"
    assert sink.beats == [], "and it is not drawn either"


async def test_the_run_stops_when_tally_cannot_be_fronted():
    async def allow(_step: FallbackStep) -> bool:
        return True

    sink = Recorder()
    fallback, keys = _fallback(
        sink,
        approve=allow,
        ensure=lambda: (None, "TallyPrime is open but could not be raised"),
    )
    session = await fallback.run("anything at all")

    assert not session.completed
    assert "could not be raised" in session.stopped_reason
    assert keys == []
