"""The title and closing cards, drawn rather than designed.

Two typographic stills. Generating them with Pillow keeps them in version
control as code, re-rendered whenever the wording changes, and means no
binary asset has to be edited by hand to fix a typo.

They are shown full screen during their beat, so a chapter stays one continuous
recording and one timeline - no separate clip to splice and no second place for
the timing to go wrong.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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


class FullScreen:
    """Shows a card over everything, for exactly as long as its line runs.

    Tk again, and for the same reason as the Tier 3 overlay: it is in the
    standard library, and a demo build should not add a dependency to draw a
    rectangle. Failure to display is logged and ignored - a missing card is a
    cosmetic problem, and it must not take the recording down with it.
    """

    def __init__(self) -> None:
        self._root: Any = None
        self._label: Any = None
        self._image: Any = None

    def show(self, path: Path) -> None:
        try:
            import tkinter as tk

            if self._root is None:
                self._root = tk.Tk()
                self._root.attributes("-fullscreen", True)
                self._root.configure(bg=PAPER)
                self._label = tk.Label(self._root, bd=0, bg=PAPER)
                self._label.pack(expand=True, fill="both")

            from PIL import Image, ImageTk

            screen = (self._root.winfo_screenwidth(), self._root.winfo_screenheight())
            picture = Image.open(path).resize(screen, Image.LANCZOS)
            self._image = ImageTk.PhotoImage(picture)
            self._label.configure(image=self._image)
            self._root.deiconify()
            self._root.lift()
            self._root.update()
        except Exception:  # noqa: BLE001 - a card is never worth a failed take
            import logging

            logging.getLogger(__name__).warning("could not show the card %s", path.name)

    def hide(self) -> None:
        try:
            if self._root is not None:
                self._root.withdraw()
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
