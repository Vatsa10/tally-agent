"""Putting it together: narration, then the mux.

Two ffmpeg passes. The first lays the voice lines and the gaps between them end
to end into one wav - as a concat list rather than a twenty-input filter graph,
because the result is a file you can play on its own and immediately hear
whether the pacing works. The second scales the master, burns the subtitles and
marries the two.

Every function here returns an argument list rather than running anything, so
the commands can be asserted in a test without ffmpeg present.
"""

from __future__ import annotations

from pathlib import Path

#: The video everyone can actually play. 2560 wide is not a safe h264 level on
#: every device; 1920x1080 is.
OUT_WIDTH = 1920
OUT_HEIGHT = 1080

#: Sizes here are in ASS points against libass's default 288-line canvas, not
#: pixels: the filter scales them by 1080/288, so 18 became a 67 pixel caption
#: sitting across the middle of the screen. 9 lands near 34 pixels, which reads
#: on a phone without covering the thing being demonstrated.
SUBTITLE_STYLE = (
    "FontName=Segoe UI,FontSize=9,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&HC0000000,BorderStyle=3,Outline=1,Shadow=0,MarginV=14"
)


def ffmpeg_path(value: str) -> str:
    """A path ffmpeg's filter parser will accept.

    Windows paths are the problem: the subtitles filter treats ``\\`` as an
    escape and ``:`` as an argument separator, so ``D:\\demo`` silently becomes
    a filter with no file. Forward slashes plus an escaped drive colon is what
    it actually wants.
    """
    return str(value).replace("\\", "/").replace(":", "\\:")


def silence_args(seconds: float, out: Path, sample_rate: int = 48000) -> list[str]:
    """A gap, as a real file, so the concat demuxer can treat it like any line."""
    return [
        "ffmpeg", "-hide_banner", "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=r={sample_rate}:cl=mono",
        "-t", f"{max(seconds, 0.01):.3f}",
        "-c:a", "pcm_s16le",
        str(out),
    ]


def concat_list(paths: list[Path]) -> str:
    """A concat demuxer list, with absolute paths.

    Absolute because ffmpeg resolves each entry relative to the *list file*,
    not to the working directory - a relative entry lands at something like
    ``build/scratch/build/audio/line.wav`` and the render dies on the first
    one. Forward slashes for the same reason the quoting matters: these are
    Windows paths going into a parser that treats backslash as an escape.
    """
    return "".join(
        f"file '{str(p.resolve()).replace(chr(92), '/')}'\n" for p in paths
    )


def concat_args(list_file: Path, out: Path, audio: bool = False) -> list[str]:
    args = [
        "ffmpeg", "-hide_banner", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
    ]
    args += ["-c:a", "pcm_s16le"] if audio else ["-c", "copy"]
    args.append(str(out))
    return args


def mux_args(
    video: Path,
    narration: Path,
    subtitles: Path | None,
    out: Path,
    width: int = OUT_WIDTH,
    height: int = OUT_HEIGHT,
) -> list[str]:
    """The final pass: scale, burn captions, attach the voice.

    ``-shortest`` is deliberately absent. The video is the master; if the
    narration ends first the picture should still play out, and if the narration
    somehow runs longer that is a bug worth hearing rather than truncating.
    """
    chain = f"scale={width}:{height}:flags=lanczos"
    if subtitles is not None:
        chain += f",subtitles='{ffmpeg_path(subtitles)}':force_style='{SUBTITLE_STYLE}'"

    return [
        "ffmpeg", "-hide_banner", "-y",
        "-i", str(video),
        "-i", str(narration),
        "-filter_complex", f"[0:v]{chain}[v]",
        "-map", "[v]",
        "-map", "1:a",
        "-c:v", "libx264",
        "-preset", "slow",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-profile:v", "high",
        "-level", "4.1",
        "-c:a", "aac",
        "-b:a", "160k",
        "-ar", "48000",
        "-movflags", "+faststart",
        str(out),
    ]


def card_args(image: Path, seconds: float, out: Path, framerate: int = 30) -> list[str]:
    """A still image as a silent clip, so title cards concat like any chapter."""
    return [
        "ffmpeg", "-hide_banner", "-y",
        "-loop", "1",
        "-framerate", str(framerate),
        "-i", str(image),
        "-t", f"{seconds:.3f}",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "16",
        "-pix_fmt", "yuv420p",
        str(out),
    ]
