"""Framing and ffmpeg arguments. Nothing here runs ffmpeg or touches a screen."""

from __future__ import annotations

from pathlib import Path

import pytest

from tallyagent_demo import capture, render

# --- framing ----------------------------------------------------------------


def test_the_capture_region_is_exactly_sixteen_by_nine():
    """Anything else means the finished video is letterboxed or squashed."""
    assert capture.Region().aspect == pytest.approx(16 / 9)


def test_the_region_downscales_to_1080p_without_resampling_artefacts():
    region = capture.Region()
    assert region.width / render.OUT_WIDTH == pytest.approx(4 / 3)
    assert region.height / render.OUT_HEIGHT == pytest.approx(4 / 3)


def test_the_default_window_rects_fit_in_the_frame():
    capture.check_framing(
        capture.Region(),
        {"Tally": capture.TALLY_RECT, "terminal": capture.TERMINAL_RECT},
    )


def test_a_window_hanging_off_the_edge_is_refused_by_name():
    with pytest.raises(ValueError) as excinfo:
        capture.check_framing(
            capture.Region(), {"TallyPrime": (2000, 120, 1600, 1000)}
        )
    message = str(excinfo.value)
    assert "TallyPrime" in message
    assert "do not record half a window" in message


def test_a_region_that_is_not_sixteen_by_nine_is_refused():
    with pytest.raises(ValueError, match="not 16:9"):
        capture.check_framing(capture.Region(width=2560, height=1600), {})


def test_the_two_windows_do_not_overlap():
    """They sit side by side; an overlap would hide the thing being demonstrated."""
    tally_x, _, _, _ = capture.TALLY_RECT
    term_x, _, term_w, _ = capture.TERMINAL_RECT
    assert term_x + term_w <= tally_x


# --- capture args -----------------------------------------------------------


def test_the_recorder_grabs_the_declared_region():
    args = capture.record_args(Path("take.mkv"))
    assert "gdigrab" in args
    assert args[args.index("-offset_y") + 1] == "80"
    assert args[args.index("-video_size") + 1] == "2560x1440"


def test_the_cursor_is_recorded_because_a_whole_chapter_is_about_it():
    args = capture.record_args(Path("take.mkv"))
    assert args[args.index("-draw_mouse") + 1] == "1"


def test_nothing_is_filtered_during_the_take():
    """Filtering steals CPU from frame pacing; it can all wait for the render."""
    args = capture.record_args(Path("take.mkv"))
    assert "-vf" not in args
    assert "-filter_complex" not in args


def test_the_master_is_matroska_so_a_killed_recorder_still_leaves_a_file():
    assert capture.record_args(Path("x/take.mkv"))[-1].endswith(".mkv")


# --- render args ------------------------------------------------------------


def test_a_windows_path_is_escaped_for_the_subtitles_filter():
    """Unescaped, ffmpeg reads D: as a filter argument and burns nothing."""
    assert render.ffmpeg_path("D:\\demo\\build\\demo.srt") == "D\\:/demo/build/demo.srt"


def test_the_final_pass_scales_burns_and_muxes():
    args = render.mux_args(
        Path("in.mkv"), Path("narration.wav"), Path("demo.srt"), Path("out.mp4")
    )
    chain = args[args.index("-filter_complex") + 1]
    assert "scale=1920:1080" in chain
    assert "subtitles=" in chain
    assert args[args.index("-map") + 1] == "[v]"
    assert "+faststart" in args


def test_the_video_is_not_truncated_to_the_narration():
    """If the voice ends early the picture plays on; -shortest would cut it."""
    args = render.mux_args(Path("in.mkv"), Path("a.wav"), None, Path("out.mp4"))
    assert "-shortest" not in args


def test_a_run_without_subtitles_still_scales_and_muxes():
    chain = render.mux_args(Path("in.mkv"), Path("a.wav"), None, Path("o.mp4"))
    assert "subtitles=" not in chain[chain.index("-filter_complex") + 1]


def test_the_concat_list_uses_forward_slashes():
    listing = render.concat_list([Path("d:\\build\\a.wav"), Path("d:\\build\\b.wav")])
    assert listing == "file 'd:/build/a.wav'\nfile 'd:/build/b.wav'\n"


def test_a_gap_becomes_a_real_silent_file():
    args = render.silence_args(1.25, Path("gap.wav"))
    assert "anullsrc=r=48000:cl=mono" in args
    assert args[args.index("-t") + 1] == "1.250"


def test_a_zero_length_gap_is_still_a_valid_file():
    """ffmpeg refuses -t 0; a hair of silence is harmless and keeps concat simple."""
    args = render.silence_args(0.0, Path("gap.wav"))
    assert float(args[args.index("-t") + 1]) > 0
