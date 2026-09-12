"""What happens on screen, beat by beat.

The TUI here is a *real* one in a *real* terminal, driven by typed keystrokes.
The tempting alternative - Textual's headless pilot, which
``scripts/live_tui_e2e.py`` uses - renders to nothing, and a video of nothing is
not a video. So the driver types, exactly as a person would, and what the camera
sees is what a user would see.

Each action is a plain async method named in ``demo/script.yaml``. The mapping
is checked by a test, so renaming a method fails CI rather than the recording
session.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tallyagent_demo import capture, cards
from tallyagent_demo.script import Beat, Chapter
from tallyagent_demo.timeline import BeatSpan, Timeline, wait_for

log = logging.getLogger(__name__)

#: Typing speed. Fast enough not to bore, slow enough to read along with.
KEY_INTERVAL = 0.045

#: After Enter, how long to let a turn finish before giving up on it. A live
#: Tally report can take a few seconds; anything past this is a stuck call.
TURN_TIMEOUT = 45.0


class Keyboard:
    """Typing and window focus, isolated so the tests can watch without a desktop."""

    def type(self, text: str, interval: float = KEY_INTERVAL) -> None:
        import pyautogui  # type: ignore[import-not-found]

        pyautogui.write(text, interval=interval)

    def press(self, key: str) -> None:
        import pyautogui  # type: ignore[import-not-found]

        if "+" in key:
            pyautogui.hotkey(*[part.strip() for part in key.split("+")])
        else:
            pyautogui.press(key)

    def focus(self, title_fragment: str) -> bool:
        try:
            import win32con  # type: ignore[import-not-found]
            import win32gui  # type: ignore[import-not-found]
        except ImportError:
            return False

        found: list[int] = []

        def visit(handle: int, _extra: object) -> None:
            if not found and win32gui.IsWindowVisible(handle):
                title = win32gui.GetWindowText(handle) or ""
                if title_fragment.lower() in title.lower():
                    found.append(handle)

        win32gui.EnumWindows(visit, None)
        if not found:
            return False
        # The same stubborn raise Tier 3 uses: a background process is not
        # allowed to take the foreground, and typing into the wrong window is
        # the one failure worth refusing over.
        from tallyagent_agent.fallback.window import raise_window

        if win32gui.GetWindowPlacement(found[0])[1] == win32con.SW_SHOWMINIMIZED:
            win32gui.ShowWindow(found[0], win32con.SW_RESTORE)
        return bool(raise_window(found[0], title_fragment))


@dataclass(slots=True)
class Clock:
    """Monotonic time and sleeping, both injectable so a test can run instantly."""

    epoch: float = 0.0
    #: Called every tick while sleeping. The Tk backdrop is not running a main
    #: loop of its own, and a window that goes unpumped for six seconds is a
    #: window Windows paints white and labels "not responding" - on camera.
    pump: Any = None

    def now(self) -> float:
        return time.monotonic() - self.epoch

    def start(self) -> None:
        self.epoch = time.monotonic()

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self.pump is None:
            await asyncio.sleep(seconds)
            return
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
            self.pump()


@dataclass
class DemoDriver:
    """Performs beats. Owns nothing about timing except asking the clock."""

    audio_seconds: dict[str, float]
    clock: Clock = field(default_factory=Clock)
    keyboard: Keyboard = field(default_factory=Keyboard)
    card_screen: Any = None
    spotlight: Any = None
    tally_title: str = "TallyPrime"
    terminal_title: str = "tallyagent"
    #: Substituted into any beat text containing ``{run}``. Every take needs a
    #: fresh invoice reference, because duplicate detection is working and will
    #: correctly refuse the same sale twice - which is the right behaviour and
    #: the wrong take.
    run_id: str = ""
    assets: Path = Path("demo/assets")
    #: Every keystroke and window switch, for the run report.
    log: list[str] = field(default_factory=list)

    # --- actions ------------------------------------------------------------

    async def pause(self, **_: Any) -> None:
        """Hold on whatever is on screen. The wait is the caller's job."""
        self.log.append("pause")

    async def title_card(self, **_: Any) -> None:
        self._card("title.png")

    async def closing_card(self, **_: Any) -> None:
        self._card("closing.png")

    async def show_tally(self, **_: Any) -> None:
        self._hide_card()
        self.log.append(f"focus {self.tally_title}")
        self.keyboard.focus(self.tally_title)

    async def open_tui(self, **_: Any) -> None:
        self._hide_card()
        self.log.append(f"focus {self.terminal_title}")
        self.keyboard.focus(self.terminal_title)

    async def say_to_agent(self, text: str = "", **_: Any) -> None:
        """Type a question into the TUI and press Enter, as a person would."""
        self._hide_card()
        line = text.replace("{run}", self.run_id or "1")
        self.keyboard.focus(self.terminal_title)
        self.log.append(f"type {line!r}")
        self.keyboard.type(line)
        await self.clock.sleep(0.3)
        self.keyboard.press("enter")

    def _run_text(self, text: str) -> str:
        return text.replace("{run}", self.run_id or "1")

    async def run_command(self, command: str = "", **_: Any) -> None:
        await self.say_to_agent(text=command)

    async def show_draft(self, **_: Any) -> None:
        """The draft is already on screen; this beat only holds the frame."""
        self.log.append("show draft")

    async def show_refusal(self, **_: Any) -> None:
        self.log.append("show refusal")

    async def show_approvals(self, **_: Any) -> None:
        await self.say_to_agent(text="/approvals")

    async def show_ticket(self, **_: Any) -> None:
        """The queue panel is on screen; hold on the diff."""
        self.log.append("show ticket")

    async def approve_pending(self, ticket: str = "", **_: Any) -> None:
        """Approve the oldest pending ticket - the one the camera just showed."""
        chosen = ticket or self.pending_ticket()
        if not chosen:
            raise RuntimeError(
                "nothing is waiting for approval, so the approval beat would be "
                "a lie. Re-record from the chapter that drafts the voucher."
            )
        await self.say_to_agent(text=f"/approve {chosen}")

    async def show_day_book(self, **_: Any) -> None:
        """Raise Tally and point at where the new voucher landed."""
        self._hide_card()
        self.keyboard.focus(self.tally_title)
        self.log.append("point at the day book")
        if self.spotlight is not None:
            left, top, width, _height = capture.TALLY_RECT
            self.spotlight.announce("the voucher that was just approved")
            self.spotlight.point(left + width // 2, top + 320)

    async def tier3_cursor(self, **_: Any) -> None:
        """The red cursor over the real Tally window. Points, never clicks."""
        self._hide_card()
        self.keyboard.focus(self.tally_title)
        if self.spotlight is None:
            self.log.append("tier3 (no spotlight)")
            return
        left, top, width, height = capture.TALLY_RECT
        self.spotlight.announce("reading the screen before deciding anything")
        self.spotlight.point(left + width // 2, top + 80)
        self.spotlight.announce("this is the voucher it would act on")
        self.spotlight.point(left + 200, top + height // 2)
        self.spotlight.point(
            left + width // 2,
            top + height // 2,
            "press Alt+D - delete the selected voucher",
        )
        self.log.append("tier3 cursor")

    # --- helpers ------------------------------------------------------------

    def pending_ticket(self) -> str:
        """The ticket just drafted - the newest one waiting, not the oldest.

        A queue left over from a rehearsal would otherwise have the approval
        beat approving something the camera never showed.
        """
        try:
            from tallyagent_daemon import config as config_mod
            from tallyagent_daemon import wiring
        except ImportError:  # pragma: no cover - the daemon is always present
            return ""
        # Whatever database the TUI on screen is using - no override. Reading a
        # different one would hand the approval beat a ticket number that the
        # visible queue has never heard of.
        config = config_mod.load("config/config.toml", "config/policy.toml")
        wired = wiring.build(config)
        waiting = wired.services.queue.list("pending", config.company.name)
        return waiting[-1].ticket if waiting else ""

    def _card(self, name: str) -> None:
        if self.card_screen is None:
            self.log.append(f"card {name} (not shown)")
            return
        self.log.append(f"card {name}")
        self.card_screen.show(self.assets / name)

    def _hide_card(self) -> None:
        if self.card_screen is not None:
            self.card_screen.hide()

    # --- the loop ------------------------------------------------------------

    async def perform(self, beat: Beat) -> None:
        action = getattr(self, beat.action, None)
        if action is None:
            raise AttributeError(
                f"beat {beat.id} names action {beat.action!r}, which the driver "
                "does not have"
            )
        await action(**beat.args)

    async def run_chapter(self, chapter: Chapter) -> Timeline:
        """Perform every beat, measuring what each one actually took.

        The measurement is the point: the audio is laid down afterwards against
        these spans, so a beat that ran long becomes a pause rather than a
        narrator talking over a spinner.
        """
        timeline = Timeline(chapter_id=chapter.id)
        for beat in chapter.beats:
            audio = self.audio_seconds.get(beat.id, beat.estimated_seconds)
            started = self.clock.now()
            await self.perform(beat)
            elapsed = self.clock.now() - started
            await self.clock.sleep(
                wait_for(audio, beat.tail_pad, elapsed, beat.max_overrun, beat.id)
            )
            timeline.spans.append(
                BeatSpan(
                    beat_id=beat.id,
                    start=round(started, 3),
                    end=round(self.clock.now(), 3),
                    audio_seconds=audio,
                )
            )
        return timeline


def launch_terminal(command: str, title: str = "tallyagent") -> subprocess.Popen[bytes]:
    """Open a real console window running the TUI, and give it a known title.

    A new console rather than this one: the recorder needs a window it can place
    at a fixed rectangle, and the build's own output must not scroll past on
    camera.
    """
    return subprocess.Popen(
        ["cmd", "/c", "start", title, "cmd", "/k", command],
        shell=False,
    )


def build_spotlight(show_cursor: bool = True) -> Any:
    from tallyagent_agent.fallback import spotlight

    return spotlight.build(show_cursor)


def build_stage() -> Any:
    """Backdrop plus cards, sized to the capture region."""
    region = capture.Region()
    return cards.Stage((region.left, region.top, region.width, region.height))
