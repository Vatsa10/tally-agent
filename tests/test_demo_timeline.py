"""The sync logic: what happens when the screen and the narration disagree.

This is the test that earns its keep. Everything else in the demo build is
plumbing that fails loudly; this is the part that could fail quietly, by
shipping a video where the voice describes something that is not on screen yet.
"""

from __future__ import annotations

import pytest

from tallyagent_demo.timeline import BeatOverran, BeatSpan, Timeline, wait_for

# --- waiting ----------------------------------------------------------------


def test_an_action_that_finishes_early_waits_out_its_line():
    # 6s of narration, the screen was done in 2: hold for the remaining 4.4.
    assert wait_for(audio_seconds=6.0, tail_pad=0.4, elapsed=2.0, max_overrun=8) == 4.4


def test_an_action_that_runs_long_does_not_wait_at_all():
    """The overrun becomes silence later, not a narrator talking over a spinner."""
    assert wait_for(audio_seconds=3.0, tail_pad=0.4, elapsed=9.0, max_overrun=8) == 0.0


def test_an_absurd_overrun_is_refused_rather_than_filmed():
    with pytest.raises(BeatOverran) as excinfo:
        wait_for(
            audio_seconds=3.0, tail_pad=0.4, elapsed=30.0, max_overrun=8, beat_id="b12"
        )
    message = str(excinfo.value)
    assert "b12" in message
    assert "Re-record this chapter" in message


def test_the_boundary_is_inclusive_so_a_beat_exactly_at_the_limit_survives():
    assert wait_for(audio_seconds=2.0, tail_pad=0.0, elapsed=10.0, max_overrun=8) == 0.0


# --- placement --------------------------------------------------------------


def _timeline() -> Timeline:
    return Timeline(
        chapter_id="ch2",
        spans=[
            # Line fits comfortably: 4s of audio inside a 5s beat.
            BeatSpan("b03", start=0.0, end=5.0, audio_seconds=4.0),
            # Tally was slow: 3s of audio inside a 10s beat.
            BeatSpan("b04", start=5.0, end=15.0, audio_seconds=3.0),
            BeatSpan("b05", start=15.0, end=21.0, audio_seconds=6.0),
        ],
    )


def test_each_line_starts_when_its_beat_started():
    assert _timeline().placements() == [
        ("b03", 0.0, 4.0),
        ("b04", 5.0, 3.0),
        ("b05", 15.0, 6.0),
    ]


def test_the_slack_is_reported_per_beat():
    spans = _timeline().spans
    assert spans[0].silence_after == 1.0
    assert spans[1].silence_after == 7.0, "a slow Tally call becomes a pause"


def test_the_narration_plan_alternates_silence_and_lines():
    assert _timeline().silence_plan() == [
        ("b03", 4.0),
        ("silence", 1.0),
        ("b04", 3.0),
        ("silence", 7.0),
        ("b05", 6.0),
    ]


def test_the_plan_sums_to_the_video_length():
    """If these disagree the audio drifts, so assert it rather than hope."""
    timeline = _timeline()
    assert sum(seconds for _, seconds in timeline.silence_plan()) == timeline.duration


def test_a_gap_before_the_first_beat_is_carried_as_leading_silence():
    timeline = Timeline(
        chapter_id="ch1",
        spans=[BeatSpan("b01", start=1.5, end=6.5, audio_seconds=5.0)],
    )
    assert timeline.silence_plan() == [("silence", 1.5), ("b01", 5.0)]


def test_a_tail_after_the_last_line_is_carried_too():
    timeline = Timeline(
        chapter_id="ch1",
        spans=[BeatSpan("b01", start=0.0, end=8.0, audio_seconds=5.0)],
    )
    assert timeline.silence_plan() == [("b01", 5.0), ("silence", 3.0)]


def test_an_empty_chapter_has_no_length_and_no_plan():
    empty = Timeline(chapter_id="ch0")
    assert empty.duration == 0.0
    assert empty.silence_plan() == []
