"""Where each line of narration goes, measured rather than assumed.

The recording is silent. The driver performs a beat, and the clock says how long
that actually took; this module turns those measurements into a plan for laying
the audio down afterwards.

That inversion is the whole design. Narration is never played during the take,
so it can never be talked over: if a Tally call runs long, the extra time
becomes silence between two sentences instead of the narrator describing
something that has not happened yet. Drift is not fought, it is impossible.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class BeatOverran(RuntimeError):
    """A beat took so much longer than its line that the video would sag."""


@dataclass(slots=True)
class BeatSpan:
    """One beat as it actually happened, in seconds from the take's epoch."""

    beat_id: str
    start: float
    end: float
    #: How long the narration for this beat runs.
    audio_seconds: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def silence_after(self) -> float:
        """Quiet between this line ending and the next one starting."""
        return max(0.0, self.duration - self.audio_seconds)


@dataclass(slots=True)
class Timeline:
    """The measured spans of one chapter, and the audio plan they imply."""

    chapter_id: str
    spans: list[BeatSpan] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.spans[-1].end if self.spans else 0.0

    def placements(self) -> list[tuple[str, float, float]]:
        """``(beat_id, start_seconds, audio_seconds)`` for the render pass.

        Audio starts when its beat started. Nothing is stretched or squeezed;
        the gaps carry the slack.
        """
        return [(s.beat_id, s.start, s.audio_seconds) for s in self.spans]

    def silence_plan(self) -> list[tuple[str, float]]:
        """``(kind, seconds)`` pairs: the concat list for narration.wav.

        Laying silence and audio end to end beats a twenty-input filter graph -
        the result is one wav you can listen to on its own and immediately hear
        whether the pacing is right.
        """
        plan: list[tuple[str, float]] = []
        cursor = 0.0
        for span in self.spans:
            gap = round(span.start - cursor, 3)
            if gap > 0.001:
                plan.append(("silence", gap))
            plan.append((span.beat_id, span.audio_seconds))
            cursor = span.start + span.audio_seconds
        tail = round(self.duration - cursor, 3)
        if tail > 0.001:
            plan.append(("silence", tail))
        return plan


def wait_for(
    audio_seconds: float,
    tail_pad: float,
    elapsed: float,
    max_overrun: float,
    beat_id: str = "",
) -> float:
    """How long to hold on this beat once its action has finished.

    Three cases, and the middle one is the reason this is a function rather
    than an inline ``max``:

    - the action finished early, which is normal: wait out the rest of the line;
    - the action ran over, which is fine: do not wait at all, and let the render
      pass insert the overrun as silence;
    - the action ran *absurdly* over: refuse, because a twelve second gap in a
      three minute video is worth re-recording one chapter for.
    """
    budget = audio_seconds + tail_pad
    if elapsed > budget + max_overrun:
        raise BeatOverran(
            f"beat {beat_id or '?'} took {elapsed:.1f}s against a {budget:.1f}s "
            f"line - more than the {max_overrun:.0f}s overrun allowed. "
            "Re-record this chapter, or give the beat a longer line."
        )
    return max(0.0, budget - elapsed)
