"""Captions: where they break, and that they cannot drift."""

from __future__ import annotations

from tallyagent_demo import subtitles
from tallyagent_demo.asr import Word


def _words(text: str, start: float = 0.0, step: float = 0.4) -> list[Word]:
    return [
        Word(text=token, start=round(start + i * step, 3), end=round(start + (i + 1) * step, 3))
        for i, token in enumerate(text.split())
    ]


def test_a_short_line_is_one_cue_on_one_line():
    cues = subtitles.group(_words("A sale, in plain words."))
    assert len(cues) == 1
    assert "\n" not in cues[0].text
    assert cues[0].text == "A sale, in plain words."


def test_a_cue_breaks_at_the_full_stop_not_mid_clause():
    text = (
        "There is no Baroda cost centre in this company. There is a Vadodara, "
        "which is the same city under its other name."
    )
    cues = subtitles.group(_words(text))
    assert len(cues) > 1
    first = cues[0].text.replace("\n", " ")
    assert first.endswith("company."), first


def test_no_cue_is_more_than_two_lines():
    long_text = " ".join(["reconciliation"] * 30)
    for cue in subtitles.group(_words(long_text)):
        assert cue.text.count("\n") <= 1


def test_a_flashed_cue_is_held_long_enough_to_read():
    cues = subtitles.group([Word("Right.", start=0.0, end=0.2)])
    assert cues[0].end - cues[0].start >= subtitles.MIN_CUE_SECONDS


def test_word_times_are_offset_by_where_the_beat_sits():
    """This is the property that stops subtitles drifting when Tally is slow."""
    cues = subtitles.build(
        placements=[("b01", 0.0, 2.0), ("b02", 30.0, 2.0)],
        words_by_beat={
            "b01": _words("first line here."),
            "b02": _words("second line here."),
        },
    )
    assert cues[0].start == 0.0
    assert cues[1].start == 30.0, "beat two's words are late by exactly its placement"


def test_a_beat_with_no_words_contributes_nothing():
    cues = subtitles.build([("b01", 0.0, 2.0)], {})
    assert cues == []


def test_srt_is_renumbered_and_stamped():
    srt = subtitles.to_srt(
        [
            subtitles.Cue(index=7, start=0.0, end=2.5, text="One."),
            subtitles.Cue(index=9, start=61.25, end=63.0, text="Two."),
        ]
    )
    assert srt.startswith("1\n00:00:00,000 --> 00:00:02,500\nOne.\n")
    assert "2\n00:01:01,250 --> 00:01:03,000\nTwo." in srt


def test_a_negative_stamp_never_reaches_the_file():
    srt = subtitles.to_srt([subtitles.Cue(1, start=-0.5, end=1.0, text="x")])
    assert "00:00:00,000" in srt
