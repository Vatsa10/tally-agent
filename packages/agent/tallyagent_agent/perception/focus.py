"""Where Tally's input cursor is, and whether the screen has changed.

Two fast, pixel-level answers the vision model is too slow and too imprecise to
give.

**The active field.** TallyPrime paints the field holding the cursor in a
saturated yellow (about #F8E0A8) - distinct from the paler band it puts behind
the current row (#F8E8C0). Measured on frames of a recorded payment, that
colour boxed the field being typed into every time, in a few milliseconds,
where the vision model's own box landed on the field one time in four. So the
ring that shows a person where the agent is working follows this.

**Change.** A vision read costs seconds, so it is spent once per new screen.
A small greyscale thumbnail of the window is compared frame to frame; a
difference means Tally redrew, and two frames alike in a row mean it has
finished redrawing and is worth reading.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

import numpy as np

#: Tally's active-field yellow, as a tolerance band around #F8E0A8.
ACTIVE_RGB_MIN = (240, 214, 148)
ACTIVE_RGB_MAX = (255, 231, 180)

#: An input field is a single line. Anything taller is a panel or a band.
MAX_FIELD_HEIGHT = 70
MIN_FIELD_PIXELS = 120

#: Thumbnail size for change detection: small enough to compare instantly,
#: large enough that a field changing colour shows up.
THUMB = (96, 60)


def _array(image: Any) -> np.ndarray:
    """A PNG's bytes, a PIL image or an array, as an RGB array."""
    if isinstance(image, np.ndarray):
        return image
    if isinstance(image, (bytes, bytearray)):
        from PIL import Image as PilImage

        image = PilImage.open(io.BytesIO(image))
    return np.asarray(image.convert("RGB"))


def active_field(image: Any) -> tuple[int, int, int, int] | None:
    """The box of the field Tally has the cursor in, or None if none is lit.

    Finds the pixels in Tally's active-field yellow and takes the largest
    connected patch that is shaped like an input field - a line high, not a
    panel. Connected on a coarse grid, which is plenty for boxes this size and
    keeps the whole thing to a few milliseconds.
    """
    rgb = _array(image).astype(np.int16)
    lo, hi = np.array(ACTIVE_RGB_MIN), np.array(ACTIVE_RGB_MAX)
    mask = np.all((rgb >= lo) & (rgb <= hi), axis=-1)
    if mask.sum() < MIN_FIELD_PIXELS:
        return None

    cell = 4
    h, w = mask.shape
    grid = mask[: h - h % cell, : w - w % cell].reshape(h // cell, cell, w // cell, cell)
    coarse = grid.sum(axis=(1, 3)) >= (cell * cell) // 2
    original = coarse

    # The text typed into a field splits its yellow into pieces between the
    # glyphs - measured, the first version boxed only the part of a date field
    # to the left of the cursor. Bridge horizontal gaps of up to BRIDGE cells
    # before grouping, then tighten each box back to the real yellow.
    bridge = 12
    spread = coarse.copy()
    for shift in range(1, bridge + 1):
        spread[:, shift:] |= coarse[:, :-shift]
    coarse = spread

    seen = np.zeros_like(coarse, dtype=bool)
    best: tuple[int, tuple[int, int, int, int]] | None = None
    rows, cols = coarse.shape
    for r0, c0 in zip(*np.nonzero(coarse), strict=False):
        if seen[r0, c0]:
            continue
        stack = [(r0, c0)]
        seen[r0, c0] = True
        size, rmin, rmax, cmin, cmax = 0, r0, r0, c0, c0
        while stack:
            r, c = stack.pop()
            size += 1
            rmin, rmax, cmin, cmax = min(rmin, r), max(rmax, r), min(cmin, c), max(cmax, c)
            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols and coarse[nr, nc] and not seen[nr, nc]:
                    seen[nr, nc] = True
                    stack.append((nr, nc))
        real = np.nonzero(original[rmin : rmax + 1, cmin : cmax + 1])
        if len(real[0]) == 0:
            continue
        cmin, cmax = cmin + int(real[1].min()), cmin + int(real[1].max())
        box = (int(cmin * cell), int(rmin * cell), int((cmax + 1) * cell), int((rmax + 1) * cell))
        if box[3] - box[1] > MAX_FIELD_HEIGHT:
            continue
        if best is None or size > best[0]:
            best = (size, box)
    return best[1] if best else None


def centre(box: tuple[int, int, int, int]) -> tuple[int, int]:
    return (box[0] + box[2]) // 2, (box[1] + box[3]) // 2


def signature(image: Any) -> np.ndarray:
    """A tiny greyscale picture of the window, for telling frames apart."""
    from PIL import Image as PilImage

    rgb = _array(image)
    grey = PilImage.fromarray(rgb.astype(np.uint8)).convert("L").resize(THUMB)
    return np.asarray(grey, dtype=np.int16)


def differs(a: np.ndarray, b: np.ndarray, threshold: float = 1.5) -> bool:
    """Has anything visible changed between two signatures?"""
    return float(np.abs(a - b).mean()) > threshold


@dataclass
class ChangeWatcher:
    """Says when Tally has drawn something new and finished drawing it."""

    last: np.ndarray | None = None
    settled_on: np.ndarray | None = None

    def observe(self, image: Any) -> str:
        """'changing', 'settled' (a new screen, done drawing) or 'same'."""
        sig = signature(image)
        previous, self.last = self.last, sig
        if previous is None or differs(previous, sig):
            return "changing"
        if self.settled_on is None or differs(self.settled_on, sig):
            self.settled_on = sig
            return "settled"
        return "same"
