"""The beat loop, and the script it drives - both without a desktop.

The whole script is rehearsed here against fakes. What that catches is the class
of mistake that would otherwise only appear mid-recording: a beat naming an
action that no longer exists, or an approval beat placed before anything has
been drafted.
"""

from __future__ import annotations

import pytest

from tallyagent_demo import script as script_mod
from tallyagent_demo.driver import Clock, DemoDriver
from tallyagent_demo.script import Beat, Chapter
from tallyagent_demo.timeline import BeatOverran


class FakeClock(Clock):
    """Time that only moves when something asks it to."""

    def __init__(self) -> None:
        super().__init__()
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def start(self) -> None:
        self.t = 0.0

    async def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        self.t += seconds


class FakeKeyboard:
    def __init__(self) -> None:
        self.typed: list[str] = []
        self.pressed: list[str] = []
        self.focused: list[str] = []

    def type(self, text: str, interval: float = 0.0) -> None:
        self.typed.append(text)

    def press(self, key: str) -> None:
        self.pressed.append(key)

    def focus(self, title_fragment: str) -> bool:
        self.focused.append(title_fragment)
        return True


class FakeSpotlight:
    def __init__(self) -> None:
        self.captions: list[str] = []
        self.points: list[tuple[int, int]] = []

    def announce(self, why: str) -> None:
        self.captions.append(why)

    def point(self, x: int, y: int, caption: str = "") -> None:
        self.points.append((x, y))
        if caption:
            self.captions.append(caption)


def _driver(audio: dict[str, float] | None = None) -> DemoDriver:
    return DemoDriver(
        audio_seconds=audio or {},
        clock=FakeClock(),
        keyboard=FakeKeyboard(),
        spotlight=FakeSpotlight(),
    )


# --- the script -------------------------------------------------------------


def test_the_demo_script_parses():
    demo = script_mod.load("demo/script.yaml")
    assert len(demo.chapters) >= 8
    assert len(demo.beats) >= 15


def test_every_beat_names_an_action_the_driver_actually_has():
    """A renamed method should fail here, not thirty seconds into a take."""
    demo = script_mod.load("demo/script.yaml")
    driver = _driver()
    missing = sorted({b.action for b in demo.beats if not hasattr(driver, b.action)})
    assert missing == []


def test_the_script_is_about_three_minutes():
    demo = script_mod.load("demo/script.yaml")
    assert 150 <= demo.estimated_seconds <= 200, demo.estimated_seconds


def test_every_beat_has_something_to_say():
    for beat in script_mod.load("demo/script.yaml").beats:
        assert beat.say.strip(), beat.id


def test_the_approval_beat_comes_after_the_beat_that_drafts_the_voucher():
    """Approving something that was never drafted is a video of a lie."""
    ids = [b.action for b in script_mod.load("demo/script.yaml").beats]
    assert ids.index("approve_pending") > ids.index("say_to_agent")


def test_two_beats_may_not_share_an_id():
    with pytest.raises(ValueError, match="duplicate beat id"):
        script_mod.DemoScript(
            chapters=[
                Chapter(
                    id="c",
                    beats=[Beat(id="same", say="one"), Beat(id="same", say="two")],
                )
            ]
        )


# --- the loop ---------------------------------------------------------------


async def test_a_quick_beat_waits_out_the_rest_of_its_line():
    driver = _driver(audio={"b1": 6.0})
    chapter = Chapter(id="c", beats=[Beat(id="b1", say="x", tail_pad=0.0)])

    timeline = await driver.run_chapter(chapter)

    assert driver.clock.slept == [6.0]  # type: ignore[attr-defined]
    assert timeline.spans[0].audio_seconds == 6.0
    assert timeline.spans[0].duration == 6.0


async def test_a_beat_that_runs_long_is_not_padded_by_the_driver():
    """The render pass adds the silence; the driver must not add it twice."""

    class SlowDriver(DemoDriver):
        async def pause(self, **_: object) -> None:
            self.clock.t += 9.0  # type: ignore[attr-defined]

    driver = SlowDriver(
        audio_seconds={"b1": 3.0}, clock=FakeClock(), keyboard=FakeKeyboard()
    )
    timeline = await driver.run_chapter(
        Chapter(id="c", beats=[Beat(id="b1", say="x", tail_pad=0.0)])
    )

    assert driver.clock.slept == [0.0]  # type: ignore[attr-defined]
    assert timeline.spans[0].silence_after == 6.0


async def test_an_absurdly_slow_beat_stops_the_chapter():
    class StuckDriver(DemoDriver):
        async def pause(self, **_: object) -> None:
            self.clock.t += 60.0  # type: ignore[attr-defined]

    driver = StuckDriver(
        audio_seconds={"b1": 3.0}, clock=FakeClock(), keyboard=FakeKeyboard()
    )
    with pytest.raises(BeatOverran):
        await driver.run_chapter(Chapter(id="c", beats=[Beat(id="b1", say="x")]))


async def test_beats_are_laid_end_to_end_with_no_gaps_of_their_own():
    driver = _driver(audio={"b1": 2.0, "b2": 3.0})
    timeline = await driver.run_chapter(
        Chapter(
            id="c",
            beats=[
                Beat(id="b1", say="x", tail_pad=0.0),
                Beat(id="b2", say="y", tail_pad=0.0),
            ],
        )
    )
    assert [(s.start, s.end) for s in timeline.spans] == [(0.0, 2.0), (2.0, 5.0)]


async def test_a_beat_naming_a_missing_action_says_which_beat():
    driver = _driver()
    with pytest.raises(AttributeError, match="b9"):
        await driver.perform(Beat(id="b9", say="x", action="teleport"))


# --- individual actions -----------------------------------------------------


async def test_asking_the_agent_focuses_the_terminal_then_types_and_sends():
    driver = _driver()
    await driver.say_to_agent(text="trial balance please")

    keyboard = driver.keyboard
    assert keyboard.focused == ["tallyagent"]  # type: ignore[attr-defined]
    assert keyboard.typed == ["trial balance please"]  # type: ignore[attr-defined]
    assert keyboard.pressed == ["enter"]  # type: ignore[attr-defined]


async def test_a_slash_command_goes_in_the_same_way_a_person_would_type_it():
    driver = _driver()
    await driver.run_command(command="/reco bank statement.csv")
    assert driver.keyboard.typed == ["/reco bank statement.csv"]  # type: ignore[attr-defined]


async def test_the_tier_three_beat_points_at_tally_and_never_clicks():
    driver = _driver()
    await driver.tier3_cursor()

    spotlight = driver.spotlight
    assert driver.keyboard.focused == ["TallyPrime"]  # type: ignore[attr-defined]
    assert len(spotlight.points) == 3  # type: ignore[attr-defined]
    assert any("Alt+D" in c for c in spotlight.captions)  # type: ignore[attr-defined]
    assert not hasattr(spotlight, "clicked")


async def test_approving_with_nothing_queued_refuses_rather_than_filming_a_lie():
    class NoTickets(DemoDriver):
        def pending_ticket(self) -> str:
            return ""

    driver = NoTickets(audio_seconds={}, clock=FakeClock(), keyboard=FakeKeyboard())
    with pytest.raises(RuntimeError, match="would be a lie"):
        await driver.approve_pending()


async def test_showing_a_card_and_then_the_screen_hides_it_again():
    class FakeCards:
        def __init__(self) -> None:
            self.shown: list[str] = []
            self.hidden = 0

        def show(self, path) -> None:  # type: ignore[no-untyped-def]
            self.shown.append(path.name)

        def hide(self) -> None:
            self.hidden += 1

    screen = FakeCards()
    driver = DemoDriver(
        audio_seconds={}, clock=FakeClock(), keyboard=FakeKeyboard(), card_screen=screen
    )
    await driver.title_card()
    await driver.show_tally()

    assert screen.shown == ["title.png"]
    assert screen.hidden == 1
