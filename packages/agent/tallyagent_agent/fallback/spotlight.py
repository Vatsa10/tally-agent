"""Show what Tier 3 is doing, while it does it.

Tier 3 drives TallyPrime's keyboard and mouse. Until now that happened at
machine speed with no sign on screen of what was about to be pressed, which is
an uncomfortable thing to watch a program do to your accounts.

This draws a red ring where the pointer is going, moves it there slowly enough
to follow, flashes on click, and captions every keystroke in plain words -
"Alt+D - delete the selected voucher". Nothing here decides anything; it only
narrates what the fallback has already chosen and had approved.

Built on tkinter, which is in the standard library, so watching the robot work
costs no new dependency. The overlay is click-through and always on top; if any
of that is unavailable the narration degrades to log lines and the automation
carries on, because a missing overlay must never block an approved action.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)

#: How long the pointer takes to travel to its target, in seconds. Fast enough
#: not to be tedious, slow enough that a person can see where it went.
TRAVEL_SECONDS = 0.45

#: How long a caption stays up after the action.
CAPTION_SECONDS = 1.2

#: Plain-words for the keystrokes Tally answers to. An unknown key is shown
#: as-is rather than guessed at.
KEY_MEANINGS = {
    "alt+d": "delete the selected voucher",
    "alt+c": "create a master from here",
    "alt+x": "cancel the voucher",
    "alt+f1": "shut the company",
    "ctrl+a": "accept and save",
    "ctrl+enter": "open the highlighted line",
    "ctrl+q": "quit without saving",
    "enter": "accept this field",
    "escape": "go back",
    "esc": "go back",
    "f1": "help",
    "f2": "change the date",
    "f4": "contra",
    "f5": "payment",
    "f6": "receipt",
    "f7": "journal",
    "f8": "sales",
    "f9": "purchase",
    "f11": "features",
    "f12": "configure",
    "space": "select this row",
    "tab": "next field",
    "y": "confirm - yes",
    "n": "decline - no",
}


def describe(action: str, detail: str, why: str = "") -> str:
    """One line a person can read before it happens."""
    if action == "click":
        head = f"click at {detail}"
    else:
        meaning = KEY_MEANINGS.get(detail.strip().lower())
        head = f"press {detail}" + (f" - {meaning}" if meaning else "")
    return f"{head}  ({why})" if why else head


@dataclass(slots=True)
class Beat:
    """One thing to show. Kind is 'move', 'click', 'key' or 'stop'."""

    kind: str
    caption: str = ""
    x: int = 0
    y: int = 0


class Sink(Protocol):
    """Where beats go. The overlay is one; the tests use a list."""

    def send(self, beat: Beat) -> None: ...

    def close(self) -> None: ...


class LogSink:
    """Narration with no window - what a headless or Tk-less machine gets."""

    def __init__(self) -> None:
        self.beats: list[Beat] = []

    def send(self, beat: Beat) -> None:
        self.beats.append(beat)
        if beat.kind != "stop":
            log.info("tier3: %s", beat.caption or beat.kind)

    def close(self) -> None:
        pass


class OverlaySink:
    """A red ring and a caption, drawn over everything else.

    Tk insists on owning its own thread, so the overlay runs in one and beats
    arrive through a queue. Every Tk call is wrapped: a failure here downgrades
    to logging rather than interrupting an approved action.
    """

    def __init__(self, ring_px: int = 46) -> None:
        self.ring_px = ring_px
        self._queue: queue.Queue[Beat] = queue.Queue()
        self._ready = threading.Event()
        self._failed = False
        self._thread = threading.Thread(
            target=self._run, name="tier3-spotlight", daemon=True
        )
        self._thread.start()
        # Not waiting forever: if Tk cannot start we narrate to the log instead.
        self._ready.wait(timeout=3.0)

    # --- producer side ------------------------------------------------------

    def send(self, beat: Beat) -> None:
        if self._failed:
            log.info("tier3: %s", beat.caption or beat.kind)
            return
        self._queue.put(beat)

    def close(self) -> None:
        self._queue.put(Beat(kind="stop"))
        self._thread.join(timeout=2.0)

    # --- Tk thread ----------------------------------------------------------

    def _run(self) -> None:
        try:
            import tkinter as tk
        except Exception:  # noqa: BLE001 - no Tk is a downgrade, not an error
            self._failed = True
            self._ready.set()
            return

        try:
            root = tk.Tk()
            root.withdraw()
            ring = self._make_window(tk, root, self.ring_px, self.ring_px)
            canvas = tk.Canvas(
                ring, width=self.ring_px, height=self.ring_px,
                highlightthickness=0, bg="black",
            )
            canvas.pack()
            pad = 3
            canvas.create_oval(
                pad, pad, self.ring_px - pad, self.ring_px - pad,
                outline="#ff2d2d", width=4,
            )
            canvas.create_line(
                self.ring_px // 2, 0, self.ring_px // 2, self.ring_px,
                fill="#ff2d2d", width=1,
            )
            canvas.create_line(
                0, self.ring_px // 2, self.ring_px, self.ring_px // 2,
                fill="#ff2d2d", width=1,
            )

            caption = self._make_window(tk, root, 10, 10)
            label = tk.Label(
                caption,
                text="",
                bg="#12060a",
                fg="#ff6b6b",
                font=("Consolas", 12, "bold"),
                padx=10,
                pady=5,
            )
            label.pack()
        except Exception:  # noqa: BLE001
            self._failed = True
            self._ready.set()
            return

        self._ready.set()
        self._pump(root, ring, canvas, label, caption)

    def _make_window(self, tk: Any, root: Any, width: int, height: int) -> Any:
        window = tk.Toplevel(root)
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        # Black is painted as transparent, so only the red survives.
        try:
            window.attributes("-transparentcolor", "black")
        except Exception:  # noqa: BLE001 - not every platform has it
            window.attributes("-alpha", 0.85)
        window.geometry(f"{width}x{height}+0+0")
        window.withdraw()
        self._click_through(window)
        return window

    def _click_through(self, window: Any) -> None:
        """Let clicks pass to Tally, not to our overlay.

        Without this the ring would swallow the very click it is drawing.
        """
        try:
            import win32con  # type: ignore[import-not-found]
            import win32gui  # type: ignore[import-not-found]

            window.update_idletasks()
            handle = int(window.winfo_id())
            styles = win32gui.GetWindowLong(handle, win32con.GWL_EXSTYLE)
            # Only add TRANSPARENT. Setting WS_EX_LAYERED again clears the
            # colour key that ``-transparentcolor`` installed, and the whole
            # overlay then renders as nothing at all - which is how this was
            # invisible the first time.
            win32gui.SetWindowLong(
                handle, win32con.GWL_EXSTYLE, styles | win32con.WS_EX_TRANSPARENT
            )
        except Exception:  # noqa: BLE001 - cosmetic; never worth failing over
            pass

    def _pump(self, root: Any, ring: Any, canvas: Any, label: Any, caption: Any) -> None:
        while True:
            try:
                beat = self._queue.get(timeout=0.05)
            except queue.Empty:
                try:
                    root.update()
                except Exception:  # noqa: BLE001 - window closed under us
                    return
                continue

            if beat.kind == "stop":
                try:
                    root.destroy()
                except Exception:  # noqa: BLE001
                    pass
                return

            try:
                if beat.caption:
                    label.configure(text=beat.caption)
                    # Shrink to fit the new text. Without this the window keeps
                    # whatever size it was first given and the caption is a
                    # 10x10 speck.
                    caption.geometry("")
                    caption.update_idletasks()
                    width = caption.winfo_reqwidth()
                    # Under the ring when there is room, above it otherwise.
                    top = beat.y + self.ring_px
                    if top + 40 > root.winfo_screenheight():
                        top = beat.y - 40
                    caption.geometry(f"+{max(0, beat.x - width // 2)}+{max(0, top)}")
                    caption.deiconify()

                if beat.kind in ("move", "click"):
                    ring.geometry(
                        f"+{beat.x - self.ring_px // 2}+{beat.y - self.ring_px // 2}"
                    )
                    ring.deiconify()
                    if beat.kind == "click":
                        # A visible thump on the ring. root.after would only
                        # schedule this; inside our own loop, sleeping is right.
                        import time as _time

                        for width_px in (10, 4):
                            canvas.itemconfigure("all", width=width_px)
                            root.update()
                            _time.sleep(0.06)
                root.update()
            except Exception:  # noqa: BLE001
                return


class Spotlight:
    """Wraps the fallback's key and click functions so the work is visible.

    The wrapped callables are what ``ComputerUseFallback`` already takes, so
    nothing about the approval gate changes: this only narrates actions that
    have already been decided and approved.
    """

    def __init__(
        self,
        sink: Sink | None = None,
        do_key: Any = None,
        do_click: Any = None,
        travel_seconds: float = TRAVEL_SECONDS,
        sleep: Any = None,
    ) -> None:
        from tallyagent_agent.fallback import computer_use

        self.sink = sink if sink is not None else LogSink()
        self._key = do_key or computer_use.press
        self._click = do_click or computer_use.click
        self.travel_seconds = travel_seconds
        if sleep is None:
            import time

            sleep = time.sleep
        self._sleep = sleep
        self.why = ""

    def announce(self, why: str) -> None:
        """The reason the next action is being taken, shown alongside it."""
        self.why = why

    def _show(self, beat: Beat) -> None:
        """Narrate, and never let narration be the thing that fails.

        The action below has already been approved by a person. A broken
        display is not a reason to withhold it - that would turn a cosmetic
        fault into a half-finished voucher.
        """
        try:
            self.sink.send(beat)
        except Exception:  # noqa: BLE001
            log.warning("tier3 narration failed; continuing: %s", beat.caption)

    def point(self, x: int, y: int, caption: str = "") -> None:
        """Show where an action would land, without taking it.

        Used to preview a click while an approver is deciding, and by the demo,
        which must be able to show the narration without touching the books.
        """
        self._show(
            Beat(kind="move", caption=caption or describe("click", f"{x},{y}", self.why),
                 x=x, y=y)
        )
        self._glide(x, y)
        self._sleep(CAPTION_SECONDS)

    def key(self, key: str) -> None:
        self._show(Beat(kind="key", caption=describe("key", key, self.why)))
        self._key(key)
        self._sleep(CAPTION_SECONDS)

    def click(self, x: int, y: int) -> None:
        caption = describe("click", f"{x},{y}", self.why)
        # Move first, so the ring arrives before the button goes down.
        self._show(Beat(kind="move", caption=caption, x=x, y=y))
        self._glide(x, y)
        self._show(Beat(kind="click", caption=caption, x=x, y=y))
        self._click(x, y)
        self._sleep(CAPTION_SECONDS)

    def _glide(self, x: int, y: int) -> None:
        """Walk the real pointer over, rather than teleporting it."""
        try:
            import pyautogui  # type: ignore[import-not-found]

            pyautogui.moveTo(x, y, duration=self.travel_seconds)
        except Exception:  # noqa: BLE001 - the click itself still happens
            self._sleep(self.travel_seconds)

    def close(self) -> None:
        self.sink.close()


def build(show_cursor: bool = True) -> Spotlight:
    """A Spotlight with a window when one is wanted, log lines otherwise."""
    return Spotlight(sink=OverlaySink() if show_cursor else LogSink())
