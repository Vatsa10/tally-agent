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


# --- captions say what was written, not what was heard -----------------------


def test_captions_use_the_script_and_the_audios_timing():
    """Transcription hears "TallyPrime" as "Dolly Prime". Fine for timing,
    humiliating burned into the picture."""
    heard = [Word("Dolly", 0.0, 0.4), Word("Prime", 0.4, 0.9), Word("runs.", 0.9, 1.4)]

    cues = subtitles.build(
        placements=[("b01", 10.0, 1.4)],
        words_by_beat={"b01": heard},
        texts_by_beat={"b01": "TallyPrime runs."},
    )

    assert "TallyPrime runs." in cues[0].text
    assert "Dolly" not in cues[0].text
    assert cues[0].start == 10.0, "and it is still timed by what was said"


def test_alignment_survives_a_transcript_that_splits_words():
    script = "X M L is the interface"
    heard = [Word(w, i * 0.5, i * 0.5 + 0.5) for i, w in enumerate("xml is the interface".split())]

    timed = subtitles.align(script, heard)

    assert [w.text for w in timed] == script.split()
    assert timed[0].start == 0.0
    assert timed[-1].end == heard[-1].end


def test_alignment_is_one_to_one_when_the_counts_agree():
    heard = [Word("one", 0.0, 0.5), Word("two", 0.5, 1.0)]
    timed = subtitles.align("ONE TWO", heard)
    assert [(w.text, w.start) for w in timed] == [("ONE", 0.0), ("TWO", 0.5)]


def test_a_beat_with_no_audio_yields_no_caption():
    assert subtitles.align("anything", []) == []
    assert subtitles.align("", [Word("x", 0, 1)]) == []
