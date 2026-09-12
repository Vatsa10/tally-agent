"""Screen capture: where the frame is, what goes in it, and how it is recorded.

ffmpeg's ``gdigrab`` does the recording rather than a Python loop over ``mss``.
At this resolution mss hands back about eleven megabytes a frame through the
GIL, which lands somewhere between eight and fourteen frames a second - and,
worse, *variably*. The timeline maths assumes wall-clock time and video time run
together, and variable pacing is exactly what breaks that. gdigrab keeps its own
clock in C and duplicates dropped frames with correct timestamps, so the
assumption holds.

The capture region is fixed rather than fitted to the Tally window: a constant
rectangle means every retake frames identically, so chapters cut together
invisibly.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

#: 2560x1440 at (0,80) on the primary display: exactly 16:9, exactly a clean
#: 0.75 downscale to 1920x1080 - no resampling mush on Tally's hairline grid -
#: and the taskbar left out of frame.
REGION_LEFT = 0
REGION_TOP = 80
REGION_WIDTH = 2560
REGION_HEIGHT = 1440

FRAMERATE = 30

#: Where each window sits inside that rectangle. Set programmatically, because
#: "roughly where I dragged it last time" is how chapter cuts start to jump.
TALLY_RECT = (940, 120, 1600, 1000)
TERMINAL_RECT = (40, 120, 880, 1000)


@dataclass(frozen=True, slots=True)
class Region:
    left: int = REGION_LEFT
    top: int = REGION_TOP
    width: int = REGION_WIDTH
    height: int = REGION_HEIGHT

    @property
    def aspect(self) -> float:
        return self.width / self.height

    def contains(self, rect: tuple[int, int, int, int]) -> bool:
        x, y, w, h = rect
        return (
            x >= self.left
            and y >= self.top
            and x + w <= self.left + self.width
            and y + h <= self.top + self.height
        )


def check_framing(region: Region, windows: dict[str, tuple[int, int, int, int]]) -> None:
    """Refuse to record a window that is hanging off the edge of the frame.

    Finding this out afterwards costs a whole take; finding it out here costs a
    second.
    """
    if abs(region.aspect - 16 / 9) > 0.001:
        raise ValueError(
            f"capture region {region.width}x{region.height} is not 16:9, so the "
            "final video would be letterboxed or squashed"
        )
    for name, rect in windows.items():
        if not region.contains(rect):
            raise ValueError(
                f"the {name} window at {rect} is not fully inside the capture "
                f"region ({region.left},{region.top} {region.width}x{region.height}). "
                "Move it, or widen the region - do not record half a window."
            )


def record_args(
    output: Path, region: Region | None = None, framerate: int = FRAMERATE
) -> list[str]:
    """The capture command. No filters here on purpose.

    Scaling or drawing text during the take spends CPU that the recorder needs
    for frame pacing, and everything cosmetic can be done later from a better
    master. ``-draw_mouse`` is on because one whole chapter is about the cursor.
    """
    return [
        "ffmpeg", "-hide_banner", "-y",
        "-f", "gdigrab",
        "-framerate", str(framerate),
        "-draw_mouse", "1",
        "-offset_x", str((region or Region()).left),
        "-offset_y", str((region or Region()).top),
        "-video_size", f"{(region or Region()).width}x{(region or Region()).height}",
        "-i", "desktop",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "16",
        "-pix_fmt", "yuv420p",
        "-g", str(framerate * 2),
        str(output),
    ]


class Recorder:
    """Starts ffmpeg, stops it politely.

    Politely matters: killing ffmpeg leaves a file with no index. The container
    is Matroska rather than MP4 for the same reason - a half-written mkv still
    plays, a half-written mp4 is a brick.
    """

    def __init__(self, output: Path, region: Region | None = None) -> None:
        self.output = output
        self.region = region or Region()
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            record_args(self.output, self.region),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self, timeout: float = 10.0) -> None:
        if self.process is None:
            return
        try:
            if self.process.stdin is not None:
                # 'q' is ffmpeg's "finish the file and exit".
                self.process.stdin.write(b"q")
                self.process.stdin.flush()
            self.process.wait(timeout=timeout)
        except Exception:  # noqa: BLE001 - a stuck recorder must not hang the run
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except Exception:  # noqa: BLE001
                self.process.kill()
        finally:
            self.process = None


def place_windows(windows: dict[str, tuple[int, int, int, int]]) -> dict[str, bool]:
    """Move each named window to its rect. Windows only; a no-op elsewhere.

    Matched on a substring of the title, which is all that is available and all
    that is needed: there is one TallyPrime and one terminal on this desktop.
    """
    placed = dict.fromkeys(windows, False)
    try:
        import win32con  # type: ignore[import-not-found]
        import win32gui  # type: ignore[import-not-found]
    except ImportError:
        return placed

    def visit(handle: int, _extra: object) -> None:
        if not win32gui.IsWindowVisible(handle):
            return
        title = win32gui.GetWindowText(handle) or ""
        for name, (x, y, w, h) in windows.items():
            if placed[name] or name.lower() not in title.lower():
                continue
            win32gui.ShowWindow(handle, win32con.SW_RESTORE)
            win32gui.SetWindowPos(handle, 0, x, y, w, h, 0x0004)  # SWP_NOZORDER
            placed[name] = True

    win32gui.EnumWindows(visit, None)
    return placed
