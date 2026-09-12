"""Builds the product demo video: narration, screen recording, subtitles, mux.

Everything here is build-time tooling. Nothing in the shipped daemon imports it,
which is why it may read a developer's ``.env`` and shell out to ffmpeg - two
things the product itself deliberately does not do.

The shape of the thing: narration is synthesised per beat *before* the take and
never played during it. The driver treats each beat's audio length as a floor,
does its work, and writes down when the beat actually started and ended. The
render pass then lays each beat's audio at its measured start. A slow Tally call
becomes a pause, never a narrator talking over a spinner.
"""
