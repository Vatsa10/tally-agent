"""Downscale a screenshot to characters, so a Tier 3 step can be judged in a
terminal.

The approver has to answer "is the agent about to press Enter on the right
screen?" without leaving the terminal. A braille-cell rendering carries enough
structure - where the panels are, where the cursor block sits, roughly what the
text density looks like - to answer that, which a filename and a keystroke do
not.

Pillow is optional: without it this degrades to a one-line description rather
than blocking the approval.
"""

from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)

#: Braille cells pack 2x4 pixels into one character, which is four times the
#: vertical resolution of block characters at the same cell count.
BRAILLE_BASE = 0x2800
_DOT_BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))

#: Darker than this (0-255) counts as ink. Tally is dark text on light, so the
#: default picks out text and borders.
DEFAULT_THRESHOLD = 128


def render(
    png: bytes, width: int = 60, threshold: int = DEFAULT_THRESHOLD
) -> str:
    """Render PNG bytes as braille text, ``width`` characters across."""
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        return (
            f"[screenshot: {len(png)} bytes - install the desktop extra "
            "(uv sync --extra desktop) to preview it here]"
        )

    try:
        image = Image.open(io.BytesIO(png)).convert("L")
    except Exception as exc:  # noqa: BLE001 - a preview must never break approval
        log.debug("preview failed: %s", exc)
        return f"[screenshot: {len(png)} bytes - could not be rendered: {exc}]"

    # Each character is 2 pixels wide and 4 tall, and terminal cells are about
    # twice as tall as they are wide, so the aspect correction is 2/4 * 2 = 1.
    pixel_width = max(2, width * 2)
    scale = pixel_width / image.width
    pixel_height = max(4, int(image.height * scale))
    pixel_height -= pixel_height % 4
    image = image.resize((pixel_width, max(4, pixel_height)))

    pixels = image.load()
    lines: list[str] = []
    for top in range(0, image.height, 4):
        row: list[str] = []
        for left in range(0, image.width, 2):
            bits = 0
            for dy in range(4):
                for dx in range(2):
                    x, y = left + dx, top + dy
                    if x >= image.width or y >= image.height:
                        continue
                    if pixels[x, y] < threshold:
                        bits |= _DOT_BITS[dy][dx]
            row.append(chr(BRAILLE_BASE + bits))
        lines.append("".join(row))
    return "\n".join(lines)


def describe_step(step: object, png: bytes = b"", width: int = 60) -> str:
    """The whole panel an approver reads before allowing a Tier 3 keystroke."""
    action = getattr(step, "action", "?")
    detail = getattr(step, "detail", "")
    why = getattr(step, "why", "")
    index = getattr(step, "index", "?")
    shot = png or getattr(step, "screenshot", b"")

    header = (
        f"Tier 3 step {index}: {action} {detail!r}"
        + (f"  -  {why}" if why else "")
    )
    warning = (
        "This step can change data in Tally. Approve it only if the screen "
        "below is the one you expect."
    )
    body = render(shot, width=width) if shot else "[no screenshot]"
    return f"{header}\n{warning}\n\n{body}"
