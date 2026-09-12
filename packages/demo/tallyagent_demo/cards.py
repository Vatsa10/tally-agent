"""The title and closing cards, drawn rather than designed.

Two typographic stills. Generating them with Pillow keeps them in version
control as code, re-rendered whenever the wording changes, and means no
binary asset has to be edited by hand to fix a typo.

They are shown full screen during their beat, so a chapter stays one continuous
recording and one timeline - no separate clip to splice and no second place for
the timing to go wrong.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

WIDTH = 2560
HEIGHT = 1440

INK = "#f4f4f5"
DIM = "#8b8b93"
ACCENT = "#ff2d2d"
PAPER = "#0b0b0d"


def _font(size: int, bold: bool = False) -> Any:
    from PIL import ImageFont

    for name in (("segoeuib.ttf", "segoeui.ttf") if bold else ("segoeui.ttf",)):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def draw(
    path: Path,
    title: str,
    subtitle: str = "",
    footer: str = "",
    width: int = WIDTH,
    height: int = HEIGHT,
) -> Path:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), PAPER)
    pen = ImageDraw.Draw(image)

    left = int(width * 0.12)
    baseline = int(height * 0.42)

    # A red rule, the same red as the Tier 3 cursor - the one visual thread
    # running through the whole video.
    pen.rectangle([left, baseline - 28, left + 96, baseline - 20], fill=ACCENT)

    pen.text((left, baseline), title, font=_font(int(height * 0.11), bold=True), fill=INK)
    if subtitle:
        pen.text(
            (left, baseline + int(height * 0.14)),
            subtitle,
            font=_font(int(height * 0.035)),
            fill=DIM,
        )
    if footer:
        pen.text(
            (left, height - int(height * 0.12)),
            footer,
            font=_font(int(height * 0.025)),
            fill=DIM,
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def build_all(root: Path) -> dict[str, Path]:
    """Both cards. Wording lives here, beside the drawing that renders it."""
    return {
        "title": draw(
            root / "title.png",
            "tallyagent",
            "Agentic bookkeeping for TallyPrime",
            "Nothing posts without a person approving it.",
        ),
        "closing": draw(
            root / "closing.png",
            "Your people approve.",
            "The typing stops.",
            "tallyagent - TallyPrime, operated by an agent that shows its work.",
        ),
    }


class Stage:
    """The two Tk surfaces the recording needs, over one root.

    A backdrop that sits *behind* everything for the whole take, and a card that
    comes in *front* for a few seconds at each end. One Tk root because two in
    a process is a source of hangs, and because they are the same concern: what
    the camera sees that is not an application window.

    The backdrop matters more than it sounds. Without it the frame shows
    whatever is on the desktop in the gaps - a bookmarks bar along the top, an
    editor down the seam between the two windows - and the video stops looking
    like a product and starts looking like somebody's screen.
    """

    def __init__(self, region: tuple[int, int, int, int] | None = None) -> None:
        self.region = region
        self._root: Any = None
        self._backdrop: Any = None
        self._card: Any = None
        self._label: Any = None
        self._image: Any = None

    def _ensure(self) -> bool:
        if self._root is not None:
            return True
        try:
            import tkinter as tk

            self._root = tk.Tk()
            self._root.withdraw()

            self._backdrop = tk.Toplevel(self._root)
            self._backdrop.overrideredirect(True)
            self._backdrop.configure(bg=PAPER)
            if self.region:
                left, top, width, height = self.region
                self._backdrop.geometry(f"{width}x{height}+{left}+{top}")
            self._backdrop.withdraw()

            self._card = tk.Toplevel(self._root)
            self._card.overrideredirect(True)
            self._card.attributes("-topmost", True)
            self._card.configure(bg=PAPER)
            self._label = tk.Label(self._card, bd=0, bg=PAPER)
            self._label.pack(expand=True, fill="both")
            self._card.withdraw()
            return True
        except Exception:  # noqa: BLE001 - a bare screen is not worth a failed take
            log.warning("could not open the demo stage; recording without it")
            self._root = None
            return False

    def backdrop(self) -> None:
        """Put the dark rectangle up, behind the application windows."""
        if not self._ensure():
            return
        try:
            self._backdrop.deiconify()
            self._backdrop.lower()
            self._root.update()
        except Exception:  # noqa: BLE001
            pass

    def show(self, path: Path) -> None:
        if not self._ensure():
            return
        try:
            from PIL import Image, ImageTk

            left, top, width, height = self.region or (
                0, 0, self._root.winfo_screenwidth(), self._root.winfo_screenheight()
            )
            picture = Image.open(path).resize((width, height), Image.LANCZOS)
            self._image = ImageTk.PhotoImage(picture)
            self._label.configure(image=self._image)
            self._card.geometry(f"{width}x{height}+{left}+{top}")
            self._card.deiconify()
            self._card.lift()
            self._root.update()
        except Exception:  # noqa: BLE001
            log.warning("could not show the card %s", path.name)

    def pump(self) -> None:
        """Give Tk a slice. Cheap, and the difference between a black backdrop
        and a white rectangle saying "not responding"."""
        try:
            if self._root is not None:
                self._root.update()
        except Exception:  # noqa: BLE001
            pass

    def hide(self) -> None:
        try:
            if self._card is not None:
                self._card.withdraw()
                self._root.update()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        try:
            if self._root is not None:
                self._root.destroy()
        except Exception:  # noqa: BLE001
            pass
        self._root = None
