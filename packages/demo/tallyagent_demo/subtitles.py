"""Word timings into burned-in captions.

Most demo videos are watched muted, so the subtitles are not an accessibility
afterthought - for a lot of viewers they are the narration. Which means they
have to break where a person would pause, not every forty characters.
"""

from __future__ import annotations

from dataclasses import dataclass

from tallyagent_demo.asr import Word

#: Two lines of this width is about as much as anyone reads without losing the
#: picture behind them.
MAX_LINE = 42
MAX_LINES = 2
#: A cue shorter than this flashes; longer than this and it outstays the audio.
MIN_CUE_SECONDS = 1.2
MAX_CUE_SECONDS = 6.0

#: Breaking a cue after one of these lands on a natural pause.
BREAK_AFTER = (".", "?", "!", ":", ";", ",")


@dataclass(slots=True)
class Cue:
    index: int
    start: float
    end: float
    text: str


def _wrap(words: list[str]) -> str:
    """Two lines at most, split as evenly as the words allow."""
    single = " ".join(words)
    if len(single) <= MAX_LINE:
        return single
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > MAX_LINE and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    # More than two lines would cover the screen; keep the last two together.
    return "\n".join(lines[:MAX_LINES]) if len(lines) <= MAX_LINES else "\n".join(
        [" ".join(lines[:-1]), lines[-1]]
    )


def group(words: list[Word], offset: float = 0.0) -> list[Cue]:
    """Words into cues, breaking at punctuation and at length.

    ``offset`` is where this beat's audio sits in the finished video, so the
    caller adds the beat placement once and every word inside is correct.
    """
    cues: list[Cue] = []
    pending: list[Word] = []

    def flush() -> None:
        if not pending:
            return
        start = offset + pending[0].start
        end = offset + pending[-1].end
        cues.append(
            Cue(
                index=len(cues) + 1,
                start=start,
                end=max(end, start + MIN_CUE_SECONDS),
                text=_wrap([w.text for w in pending]),
            )
        )
        pending.clear()

    for word in words:
        pending.append(word)
        line = " ".join(w.text for w in pending)
        ends_clause = word.text.rstrip().endswith(BREAK_AFTER)
        too_long = len(line) > MAX_LINE * MAX_LINES
        too_slow = (pending[-1].end - pending[0].start) > MAX_CUE_SECONDS
        if (ends_clause and len(line) > MAX_LINE // 2) or too_long or too_slow:
            flush()
    flush()
    return cues


def _stamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    milliseconds = int(round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def to_srt(cues: list[Cue]) -> str:
    """SubRip. Renumbered here, because cues arrive per beat and are concatenated."""
    blocks = []
    for number, cue in enumerate(cues, start=1):
        blocks.append(
            f"{number}\n{_stamp(cue.start)} --> {_stamp(cue.end)}\n{cue.text}\n"
        )
    return "\n".join(blocks)


def build(
    placements: list[tuple[str, float, float]],
    words_by_beat: dict[str, list[Word]],
) -> list[Cue]:
    """Every cue in the finished video, in order.

    ``placements`` is what the timeline measured: beat id, when its audio
    starts, how long it runs. Word offsets are relative to their own beat, so
    absolute time is just the sum - which is why the subtitles cannot drift even
    when a Tally call ran long.
    """
    cues: list[Cue] = []
    for beat_id, start, _seconds in placements:
        cues.extend(group(words_by_beat.get(beat_id, []), offset=start))
    return cues
