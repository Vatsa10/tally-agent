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
import os
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

        exact: list[int] = []
        partial: list[int] = []

        def visit(handle: int, _extra: object) -> None:
            if not win32gui.IsWindowVisible(handle):
                return
            title = win32gui.GetWindowText(handle) or ""
            if title == title_fragment:
                exact.append(handle)
            elif title_fragment.lower() in title.lower():
                partial.append(handle)

        win32gui.EnumWindows(visit, None)
        # Exact first. "tallyagent" as a fragment also matches an editor or a
        # shell with the project open, and the take then types a demo script
        # into somebody's terminal.
        found = exact or partial
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
    #: The database the TUI on screen is writing to. Watched, never written.
    db_path: str = "tallyagent.db"
    #: Every keystroke and window switch, for the run report.
    log: list[str] = field(default_factory=list)
    #: Whether a Tally report is open because *we* opened it. Escape is only
    #: safe when something is open to back out of: at the Gateway Tally reads it
    #: as "Quit ?", and a take that films a quit dialog is a take wasted.
    _report_open: bool = False

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

    async def say_to_agent(self, text: str = "", wait: bool = True, **_: Any) -> None:
        """Type a question into the TUI and press Enter, as a person would.

        Then wait for the answer to actually arrive. Without this the narration
        describes a trial balance while the screen still shows a spinner, which
        is the single thing that made earlier takes look faked: the words were
        right and the screen was half a sentence behind them.
        """
        self._hide_card()
        line = text.replace("{run}", self.run_id or "1")
        self._require_focus(self.terminal_title)
        before = self._audit_rows()
        self.log.append(f"type {line!r}")
        self.keyboard.type(line)
        await self.clock.sleep(0.3)
        self.keyboard.press("enter")
        if wait:
            await self.wait_for_turn(before)

    def _require_focus(self, title: str, attempts: int = 3) -> None:
        """Bring a window to the front, or refuse to type at all.

        Windows does not always grant the foreground to a background process,
        and pyautogui types wherever focus happens to be. A take that loses the
        race types its script into whatever the person was doing - which is
        what happened, into a live shell. Failing the chapter is cheap; that is
        not.
        """
        for _ in range(attempts):
            if self.keyboard.focus(title):
                return
            time.sleep(0.6)
        raise RuntimeError(
            f"refusing to type: {title!r} would not come to the front, and the "
            "keystrokes would land in whatever window did."
        )

    def _audit_rows(self) -> int:
        """How many rows the audit log holds right now.

        The TUI runs in its own process, so there is no object to ask. What
        there is, is the log it writes to as it works - one row per tool call,
        approval and egress - and a count of those is enough to tell "still
        working" from "finished" without parsing a console.
        """
        import sqlite3

        # Three tables, because they move at different moments: egress rows
        # while the model is being called, audit rows as tools run, approvals
        # as tickets land. Watching only the audit log made a folder of bills
        # look finished while it was still reading the second scan.
        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as db:
                return sum(
                    int(db.execute(f"select count(*) from {table}").fetchone()[0])
                    for table in ("audit_log", "egress", "approvals")
                )
        except Exception:  # noqa: BLE001 - no log is not a reason to stop filming
            return -1

    async def wait_for_turn(self, before: int, quiet: float = 1.4) -> None:
        """Hold until the agent stops doing things, or the timeout gives up.

        "Stops doing things" rather than "has done something": a turn is
        several tool calls, and the first one landing does not mean the answer
        is on screen. So it waits for the count to go quiet, and a beat whose
        command writes nothing at all - /approvals, /help - simply waits out
        the quiet period and moves on.
        """
        if before < 0:
            await self.clock.sleep(1.0)
            return
        deadline = self.clock.now() + TURN_TIMEOUT
        last_change = self.clock.now()
        seen = before
        while self.clock.now() < deadline:
            await self.clock.sleep(0.3)
            now = self._audit_rows()
            if now != seen:
                seen = now
                last_change = self.clock.now()
            elif self.clock.now() - last_change >= quiet:
                break
        self.log.append(f"turn settled after {seen - before} audit row(s)")

    def _run_text(self, text: str) -> str:
        return text.replace("{run}", self.run_id or "1")

    async def run_command(self, command: str = "", wait: bool = True, **_: Any) -> None:
        await self.say_to_agent(text=command, wait=wait)

    async def wait_for_agent(self, quiet: float = 6.0, **_: Any) -> None:
        """Hold until the work started by an earlier beat has finished.

        For the long ones - a folder of bills is a minute of character
        recognition - where waiting inside the beat that starts it would put a
        minute of silence on camera. The narration runs over the work instead,
        and this is where the next chapter waits for it to be done, so nothing
        is typed into a busy terminal.
        """
        await self.wait_for_turn(self._audit_rows() - 1, quiet=quiet)

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

    async def in_tally(
        self,
        report: str = "",
        caption: str = "",
        point_y: int = 150,
        then: str = "",
        **_: Any,
    ) -> None:
        """Open one of Tally's own reports, through Tally's own Go To search.

        Not the menu accelerators. A bare "k" means Day Book at the Gateway and
        something else entirely on any other screen, and a driver that presses
        letters hopefully ends up three menus deep with half a ledger name typed
        into a filter - which is exactly what happened. ``Alt+G`` opens the Go To
        box from anywhere, the report is named in full, and Enter takes it. One
        path, no assumptions about what is currently on screen.

        This is also why the demo can verify itself at all: Tally showing the
        figure is evidence in a way that the agent reporting it is not.
        """
        self._hide_card()
        if not self.keyboard.focus(self.tally_title):
            self.log.append(f"tally {report!r}: could not raise the window")
            return
        if report:
            self.keyboard.press("alt+g")
            await self.clock.sleep(0.8)
            self.keyboard.type(report)
            await self.clock.sleep(0.6)
            self.keyboard.press("enter")
            self._report_open = True
            await self.clock.sleep(1.6)
        if then:
            # A view switch inside a report that is already open, so the key
            # means what the report's own panel says it means.
            self.keyboard.press(then)
            await self.clock.sleep(1.0)
        self.log.append(f"tally shows {caption or report}")
        if self.spotlight is not None and caption:
            left, top, width, _height = capture.TALLY_RECT
            self.spotlight.announce(caption)
            self.spotlight.point(left + width // 2, top + point_y)

    async def show_day_book(self, **_: Any) -> None:
        """Tally's Day Book, with the voucher that was just approved in it."""
        # Near the top of the report: Tally lists the day's vouchers from the
        # first row down, and a ring in the empty middle of the screen points at
        # nothing.
        await self.in_tally(
            report="Day Book",
            caption="the voucher that was just approved",
            point_y=150,
        )

    async def show_stock_summary(self, **_: Any) -> None:
        """Tally's own Stock Summary - the same quantity, from the other side."""
        # Tally opens this at group level and the view keys did not reliably
        # expand it, so the narration talks about what is actually on screen -
        # the stock value - rather than a per-item quantity that is one keypress
        # away and might not be there.
        await self.in_tally(
            report="Stock Summary",
            caption="Tally's own stock value",
            point_y=200,
        )

    async def close_report(self, **_: Any) -> None:
        """Back out of a report *we* opened, and only then.

        Escape at the Gateway is "Quit ?", so this tracks whether anything is
        open rather than pressing hopefully. Pressing it blind is exactly how an
        earlier take filmed Tally asking whether to shut down.
        """
        if not self._report_open:
            self.log.append("nothing open; not pressing escape")
            return
        self.keyboard.focus(self.tally_title)
        self.keyboard.press("escape")
        self._report_open = False
        await self.clock.sleep(0.6)
        self.log.append("back to the gateway")

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


class Terminal:
    """The TUI, in its own console window, opened and closed by the build.

    A separate console rather than this one, for three reasons: the recorder
    needs a window it can place at a fixed rectangle, the build's own output
    must not scroll past on camera, and the window has to carry a known title so
    keystrokes can be aimed at it.

    It is opened fresh for every take. A reused console still holds the previous
    take's transcript, and the first thing the camera saw in one recording was
    the *last* recording's egress log scrolled up the screen.
    """

    #: The TUI needs to reach Tally, list companies and draw itself before a
    #: keystroke means anything.
    STARTUP_SECONDS = 26

    def __init__(self, title: str = "tallyagent", build_dir: Path = Path("demo/build")) -> None:
        self.title = title
        self.build_dir = build_dir
        self.process: subprocess.Popen[bytes] | None = None

    def script(self) -> Path:
        """A .cmd wrapper: the title, a cleared screen, and the model key.

        The key is read out of ``.env`` here rather than exported globally,
        because the product deliberately reads secrets from the environment and
        nothing else - this bridges the developer's file into the one process
        that needs it, for as long as it runs.
        """
        from tallyagent_demo import env as env_mod

        key = os.environ.get("DEEPSEEK_API_KEY", "") or env_mod.parse(
            Path(".env").read_text(encoding="utf-8") if Path(".env").is_file() else ""
        ).get("DEEPSEEK_API_KEY", "")

        self.build_dir.mkdir(parents=True, exist_ok=True)
        path = self.build_dir / "run_tui.cmd"
        path.write_text(
            "@echo off\r\n"
            f"title {self.title}\r\n"
            "cls\r\n"
            f"cd /d {Path.cwd()}\r\n"
            + (f"set DEEPSEEK_API_KEY={key}\r\n" if key else "")
            + "uv run tallyagent tui\r\n",
            encoding="utf-8",
        )
        return path

    def start(self) -> bool:
        import time as _time

        self.stop()
        self.process = subprocess.Popen(
            [str(self.script())],
            creationflags=0x00000010,  # CREATE_NEW_CONSOLE
        )
        waited = 0.0
        while waited < self.STARTUP_SECONDS:
            _time.sleep(2)
            waited += 2
            if _window(self.title) is not None:
                _time.sleep(4)  # let it finish drawing before anything is typed
                return True
        return False

    def stop(self) -> None:
        """Close the window politely, so the next take starts from nothing."""
        handle = _window(self.title)
        if handle is None:
            return
        try:
            import win32con  # type: ignore[import-not-found]
            import win32gui  # type: ignore[import-not-found]

            win32gui.PostMessage(handle, win32con.WM_CLOSE, 0, 0)
            import time as _time

            _time.sleep(2)
        except Exception:  # noqa: BLE001 - a lingering console is not fatal
            pass
        self.process = None


def _window(title: str) -> int | None:
    try:
        import win32gui  # type: ignore[import-not-found]
    except ImportError:
        return None

    found: list[int] = []

    def visit(handle: int, _extra: object) -> None:
        if not found and win32gui.IsWindowVisible(handle):
            if (win32gui.GetWindowText(handle) or "") == title:
                found.append(handle)

    win32gui.EnumWindows(visit, None)
    return found[0] if found else None


def build_spotlight(show_cursor: bool = True) -> Any:
    from tallyagent_agent.fallback import spotlight

    return spotlight.build(show_cursor)


def build_stage() -> Any:
    """Backdrop plus cards, sized to the capture region."""
    region = capture.Region()
    return cards.Stage((region.left, region.top, region.width, region.height))
