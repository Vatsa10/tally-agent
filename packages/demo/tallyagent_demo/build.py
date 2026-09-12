"""The stages, as plain functions: narrate, transcribe, caption, assemble.

Each one reads what the previous wrote and writes something you can inspect, so
any of them can be re-run alone. That matters more than elegance here: the
expensive parts are the ones that touch a network or a live TallyPrime, and
being able to redo only the cheap stage after a change is what makes iterating
on a video bearable.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from tallyagent_demo import asr as asr_mod
from tallyagent_demo import cache as cache_mod
from tallyagent_demo import ffmpeg, render, subtitles
from tallyagent_demo.asr import ASR, Word
from tallyagent_demo.script import DemoScript
from tallyagent_demo.timeline import BeatSpan, Timeline
from tallyagent_demo.tts import TTS

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Paths:
    root: Path = Path("demo/build")

    @property
    def audio(self) -> Path:
        return self.root / "audio"

    @property
    def words(self) -> Path:
        return self.root / "words"

    @property
    def takes(self) -> Path:
        return self.root / "takes"

    @property
    def scratch(self) -> Path:
        return self.root / "scratch"

    def ensure(self) -> None:
        for directory in (self.root, self.audio, self.words, self.takes, self.scratch):
            directory.mkdir(parents=True, exist_ok=True)


async def narrate(demo: DemoScript, speaker: TTS, paths: Paths) -> dict[str, float]:
    """A wav per beat. Returns beat id -> its true length, from ffprobe.

    ffprobe rather than the provider's own figure, because ffprobe measures the
    file that will actually be laid into the timeline. A tenth of a second of
    disagreement is enough to walk the captions off the end of the video.
    """
    paths.ensure()
    lengths: dict[str, float] = {}
    for beat in demo.beats:
        spoken = await speaker.speak(beat.say, demo.voice)
        target = paths.audio / f"{beat.id}.wav"
        if spoken.path.resolve() != target.resolve():
            target.write_bytes(spoken.path.read_bytes())
        lengths[beat.id] = ffmpeg.duration(target)
        log.info("narrated %s (%.2fs)", beat.id, lengths[beat.id])

    (paths.root / "durations.json").write_text(json.dumps(lengths, indent=2))
    return lengths


async def transcribe(
    demo: DemoScript, listener: ASR, paths: Paths, strict: bool = True
) -> dict[str, list[Word]]:
    """Word timings per beat, and a check that the voice said what was written.

    The check is the reason this stage is worth its API call even though Murf
    reports word durations of its own: a mispronunciation shows up as a
    mismatch here rather than as subtitles that quietly drift out of step.
    """
    paths.ensure()
    heard: dict[str, list[Word]] = {}
    complaints: list[str] = []
    for beat in demo.beats:
        audio = paths.audio / f"{beat.id}.wav"
        if not audio.is_file():
            continue
        words = await listener.words(audio, demo.asr_model)
        heard[beat.id] = words
        problem = asr_mod.compare(beat.say, words)
        if problem:
            complaints.append(f"{beat.id}: {problem}")

    if complaints and strict:
        raise RuntimeError(
            "the narration does not match the script:\n  " + "\n  ".join(complaints)
        )
    for complaint in complaints:
        log.warning("narration mismatch - %s", complaint)

    (paths.words / "all.json").write_text(
        json.dumps(
            {
                beat: [{"text": w.text, "start": w.start, "end": w.end} for w in words]
                for beat, words in heard.items()
            },
            indent=2,
        )
    )
    return heard


def caption(
    timelines: list[Timeline], words: dict[str, list[Word]], paths: Paths
) -> Path:
    """One SRT for the whole video, with each chapter shifted to where it sits."""
    placements: list[tuple[str, float, float]] = []
    offset = 0.0
    for timeline in timelines:
        for beat_id, start, seconds in timeline.placements():
            placements.append((beat_id, offset + start, seconds))
        offset += timeline.duration

    cues = subtitles.build(placements, words)
    target = paths.root / "demo.srt"
    target.write_text(subtitles.to_srt(cues), encoding="utf-8")
    log.info("%d subtitle cue(s) -> %s", len(cues), target)
    return target


def narration_track(timelines: list[Timeline], paths: Paths) -> Path:
    """One wav: every line at its measured start, silence in between.

    Assembled as a concat list rather than a twenty-input filter graph, because
    the result is a file you can play on its own and hear whether the pacing is
    right before spending a minute on the final encode.
    """
    paths.ensure()
    pieces: list[Path] = []
    for timeline in timelines:
        for kind, seconds in timeline.silence_plan():
            if kind == "silence":
                gap = paths.scratch / f"gap-{len(pieces):03d}.wav"
                ffmpeg.run(render.silence_args(seconds, gap), f"{seconds:.2f}s of silence")
                pieces.append(gap)
            else:
                pieces.append(paths.audio / f"{kind}.wav")

    listing = paths.scratch / "narration.txt"
    listing.write_text(render.concat_list(pieces), encoding="utf-8")
    target = paths.audio / "narration.wav"
    ffmpeg.run(render.concat_args(listing, target, audio=True), "narration track")
    return target


def stitch(takes: list[Path], paths: Paths) -> Path:
    """The selected chapter recordings, end to end, without re-encoding."""
    listing = paths.scratch / "video.txt"
    listing.write_text(render.concat_list(takes), encoding="utf-8")
    target = paths.scratch / "master.mkv"
    ffmpeg.run(render.concat_args(listing, target), "chapter concat")
    return target


def finish(video: Path, narration: Path, srt: Path | None, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(render.mux_args(video, narration, srt, out), "final render")
    return out


def save_timeline(timeline: Timeline, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "chapter": timeline.chapter_id,
                "spans": [
                    {
                        "beat_id": s.beat_id,
                        "start": s.start,
                        "end": s.end,
                        "audio_seconds": s.audio_seconds,
                    }
                    for s in timeline.spans
                ],
            },
            indent=2,
        )
    )


def load_timeline(path: Path) -> Timeline:
    payload = json.loads(path.read_text())
    return Timeline(
        chapter_id=payload["chapter"],
        spans=[BeatSpan(**span) for span in payload["spans"]],
    )


def audio_cache(paths: Paths) -> cache_mod.Cache:
    return cache_mod.Cache(paths.root / "tts-cache")


def word_cache(paths: Paths) -> cache_mod.Cache:
    return cache_mod.Cache(paths.root / "asr-cache")
