"""Running ffmpeg, and asking ffprobe how long something actually is.

Durations come from ffprobe rather than from what the TTS provider claimed,
because ffprobe measures the file ffmpeg is about to place. A provider's
reported length and the file's real length disagreeing by a tenth of a second
is enough to walk the subtitles off the end of a three minute video.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def require() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise RuntimeError(
                f"{tool} is not on PATH. Install ffmpeg - the demo build shells "
                "out to it for capture, concatenation and muxing."
            )


def run(args: list[str], what: str) -> None:
    log.info("ffmpeg: %s", what)
    result = subprocess.run(args, capture_output=True)
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", "replace").strip().splitlines()[-12:]
        raise RuntimeError(f"{what} failed:\n" + "\n".join(tail))


def duration(path: Path) -> float:
    """Seconds, as ffmpeg sees them."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe could not read {path.name}")
    payload = json.loads(result.stdout.decode("utf-8", "replace"))
    return round(float(payload["format"]["duration"]), 3)
