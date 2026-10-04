"""The agent's eyes on TallyPrime: a vision model for what the screen is, Tally's
own highlight for where the cursor is.

The focus tests run against frames from a real recording of the agent keying a
payment, where the right answer was measured by hand.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from tallyagent_agent.fallback.tally_ui import TallyUi, UiStep
from tallyagent_agent.perception import focus
from tallyagent_agent.perception.vision import ScreenState, VisionScreen, parse

# --- reading the model's reply -------------------------------------------------


def test_a_screen_reply_is_read_into_a_state():
    state = parse(
        '```json\n{"screen": "Accounting Voucher Creation", "voucher_type": "Payment", '
        '"dialog": "Cost Allocations for : Bank Charges", "active_field": "Name of Cost Centre", '
        '"active_box": [500, 220, 640, 236], "waiting_for": "a cost centre", "error": ""}\n```',
        scale=0.5,
    )

    assert state.screen == "Accounting Voucher Creation"
    assert state.dialog.startswith("Cost Allocations")
    assert state.active_box == (1000, 440, 1280, 472), "back in window pixels"


def test_an_unreadable_reply_is_an_empty_state_which_matches_nothing():
    """The safe direction: a state nobody could read is never 'the expected screen'."""
    state = parse("I'm not sure what this is.")

    assert state == ScreenState()
    assert not state.mentions("Payment")


def test_a_popup_counts_as_the_screen_being_shown():
    state = ScreenState(screen="Accounting Voucher Creation", dialog="Bill-wise Details for Zenith")

    assert state.mentions("Bill-wise Details")
    assert state.mentions("Voucher Creation")
    assert not state.mentions("Day Book")


def test_a_state_reads_as_a_sentence():
    state = ScreenState(dialog="Cost Allocations", active_field="Name of Cost Centre",
                        waiting_for="a cost centre for Bank Charges")

    assert state.describe() == (
        "Cost Allocations - cursor in Name of Cost Centre - a cost centre for Bank Charges"
    )


class CountingRouter:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, max_tokens=0):  # type: ignore[no-untyped-def]
        from tallyagent_llm.provider import Completion

        self.calls += 1
        assert messages[0].images, "the screenshot is sent"
        return Completion(text='{"screen": "Day Book"}')


def _png(colour=(240, 240, 240), size=(64, 40)) -> bytes:
    import io

    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


async def test_the_same_picture_is_read_once():
    """A read costs seconds; the same screen twice should not cost it twice."""
    router = CountingRouter()
    eyes = VisionScreen(router)

    first = await eyes.read(_png())
    second = await eyes.read(_png())

    assert first.screen == second.screen == "Day Book"
    assert router.calls == 1


async def test_an_empty_reply_is_asked_again_and_never_cached():
    """Measured live: some reads come back empty. Ask once more; cache only answers."""
    class Flaky:
        def __init__(self) -> None:
            self.replies = ["", '{"screen": "Gateway of Tally"}']

        async def complete(self, messages, max_tokens=0):  # type: ignore[no-untyped-def]
            from tallyagent_llm.provider import Completion

            return Completion(text=self.replies.pop(0) if self.replies else "")

    eyes = VisionScreen(Flaky())
    assert (await eyes.read(_png())).screen == "Gateway of Tally"

    blank = VisionScreen(CountingRouter())
    blank.router.complete = Flaky().complete  # type: ignore[method-assign]
    other = _png((10, 10, 10))
    blank.router.complete.__self__.replies = ["", ""]  # type: ignore[attr-defined]
    assert (await blank.read(other)).screen == ""
    assert not blank._cache, "an empty read is not remembered as the answer"


async def test_a_failing_model_is_an_unknown_screen_not_a_crash():
    class Broken:
        async def complete(self, *a, **k):  # type: ignore[no-untyped-def]
            raise RuntimeError("provider down")

    state = await VisionScreen(Broken()).read(_png())

    assert state.screen == ""


# --- where the cursor is: Tally's own highlight ---------------------------------

FRAMES = Path("film/build")
#: Measured by hand on frames of a recorded payment: (frame second) -> the box
#: of the field Tally had the cursor in.
TRUTH = {30: (1090, 872, 1195, 895), 50: (2114, 360, 2285, 381), 95: (11, 474, 439, 495)}


@pytest.mark.skipif(not (FRAMES / "f-payment-50.png").exists(),
                    reason="recorded frames are local, not committed")
@pytest.mark.parametrize(("second", "truth"), list(TRUTH.items()))
def test_the_highlight_finds_the_field_on_real_frames(second, truth):
    box = focus.active_field(Image.open(FRAMES / f"f-payment-{second}.png"))

    assert box is not None
    found, expected = focus.centre(box), focus.centre(truth)
    assert abs(found[0] - expected[0]) <= 12 and abs(found[1] - expected[1]) <= 6


def test_the_highlight_on_a_synthetic_screen():
    """Tally paints the field with the cursor #F8E0A8; the row behind it paler."""
    screen = np.full((400, 800, 3), 252, dtype=np.uint8)
    screen[100:124, :] = (248, 232, 192)            # the current row's band
    screen[100:124, 500:700] = (248, 224, 168)      # the field with the cursor
    screen[106:118, 560:600] = (20, 20, 20)         # text typed into it

    box = focus.active_field(screen)

    assert box is not None
    assert 495 <= box[0] <= 505 and 695 <= box[2] <= 704, "the whole field, across its text"


def test_the_highlight_as_printwindow_paints_it():
    """The live capture is a shade lighter than a recording; both are the field."""
    screen = np.full((400, 800, 3), 252, dtype=np.uint8)
    screen[100:124, :] = (248, 232, 192)
    screen[100:124, 500:700] = (254, 232, 175)

    box = focus.active_field(screen)

    assert box is not None and 495 <= box[0] <= 505


def test_no_highlight_means_no_answer_rather_than_a_guess():
    assert focus.active_field(np.full((200, 300, 3), 250, dtype=np.uint8)) is None


def test_a_yellow_panel_is_not_mistaken_for_a_field():
    screen = np.full((600, 800, 3), 250, dtype=np.uint8)
    screen[50:400, 100:400] = (248, 224, 168)

    assert focus.active_field(screen) is None


def test_the_watcher_says_when_a_new_screen_has_finished_drawing():
    watch = focus.ChangeWatcher()
    a, b = np.full((80, 120, 3), 240, np.uint8), np.full((80, 120, 3), 30, np.uint8)

    assert watch.observe(a) == "changing"
    assert watch.observe(a) == "settled"
    assert watch.observe(a) == "same"
    assert watch.observe(b) == "changing"
    assert watch.observe(b) == "settled"


# --- the driver uses both --------------------------------------------------------


class Bounds:
    left, top = 100, 50


class Ring:
    def __init__(self) -> None:
        self.points: list[tuple[int, int]] = []
        self.said: list[str] = []

    def announce(self, words: str) -> None:
        self.said.append(words)

    def point(self, x: int, y: int, caption: str = "") -> None:
        self.points.append((x, y))


class Keys:
    def __init__(self) -> None:
        self.typed: list[str] = []
        self.pressed: list[str] = []

    def type(self, text: str, interval: float = 0.0) -> None:
        self.typed.append(text)

    def press(self, key: str) -> None:
        self.pressed.append(key)

    def focus(self, title: str) -> bool:
        return True


async def _nosleep(seconds: float) -> None:
    return None


def _frame_with_field() -> bytes:
    import io

    screen = np.full((400, 800, 3), 252, dtype=np.uint8)
    screen[100:124, 500:700] = (248, 224, 168)
    buffer = io.BytesIO()
    Image.fromarray(screen).save(buffer, format="PNG")
    return buffer.getvalue()


async def test_the_ring_follows_tallys_own_highlight():
    ring = Ring()
    ui = TallyUi(keyboard=Keys(), spotlight=ring, sleep=_nosleep, screen_name=lambda: "x",
                 grab=lambda: (_frame_with_field(), Bounds()))

    await ui.step(UiStep("the amount", text="150.00"))

    x, y = ring.points[0]
    assert 100 + 595 <= x <= 100 + 605 and 50 + 108 <= y <= 50 + 116


async def test_the_screen_is_read_alongside_the_work_without_holding_it_up():
    events: list[tuple[str, str]] = []

    class Slow:
        async def read(self, png: bytes) -> ScreenState:
            await asyncio.sleep(0.05)
            return ScreenState(screen="Accounting Voucher Creation", active_field="Amount")

    keys = Keys()
    ui = TallyUi(keyboard=keys, spotlight=Ring(), sleep=_nosleep, screen_name=lambda: "x",
                 grab=lambda: (_frame_with_field(), Bounds()), vision=Slow(),
                 on_event=lambda k, t: events.append((k, t)))

    await ui.step(UiStep("the amount", text="150.00"))
    assert keys.typed == ["150.00"], "typed without waiting for the read"
    await asyncio.sleep(0.1)

    assert ("see", "Accounting Voucher Creation - cursor in Amount") in events


async def test_the_safety_check_asks_the_eyes_before_refusing():
    """Header text alone missed popups; the vision read sees them."""
    class Sees:
        async def read(self, png: bytes) -> ScreenState:
            return ScreenState(screen="Accounting Voucher Creation", dialog="Bill-wise Details")

    ui = TallyUi(keyboard=Keys(), sleep=_nosleep, screen_name=lambda: "Accounting Voucher",
                 grab=lambda: (_frame_with_field(), Bounds()), vision=Sees())

    assert await ui.expect_screen("Bill-wise Details") is True
    assert await ui.expect_screen("Day Book") is False


async def test_a_refusal_says_what_the_agent_saw():
    class Sees:
        async def read(self, png: bytes) -> ScreenState:
            return ScreenState(screen="Ledger Creation", waiting_for="a new ledger's name")

    keys = Keys()
    ui = TallyUi(keyboard=keys, sleep=_nosleep, screen_name=lambda: "Ledger Creation",
                 grab=lambda: (_frame_with_field(), Bounds()), vision=Sees())

    await ui.expect_screen("Payment")
    from tallyagent_agent.fallback.tally_ui import UiRun

    run = ui._interrupted(UiRun(task="payment"))

    assert "Ledger Creation - a new ledger's name" in run.stopped
    assert keys.pressed == ["escape"]
