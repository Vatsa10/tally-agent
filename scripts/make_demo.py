"""Build the product demo video.

    uv run python scripts/make_demo.py all --offline     # rehearse, no API spend
    uv run python scripts/make_demo.py voices            # list the Murf voices
    uv run python scripts/make_demo.py tts               # narrate (costs money)
    uv run python scripts/make_demo.py asr               # word timings
    uv run python scripts/make_demo.py preflight         # is the desk ready?
    uv run python scripts/make_demo.py record --chapter ch2-draft
    uv run python scripts/make_demo.py subtitles render
    uv run python scripts/make_demo.py cleanup           # undo the demo vouchers

Stages are separate on purpose. The expensive ones touch a network or a live
TallyPrime; being able to re-run only the cheap ones after a change is what
makes iterating on a video bearable. ``--offline`` swaps the network for local
stand-ins and produces a real, watchable, subtitled mp4 for nothing, which is
where the pacing should be settled before a single word is paid for.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from tallyagent_demo import build as build_mod
from tallyagent_demo import capture, cards, env, ffmpeg
from tallyagent_demo import driver as driver_mod
from tallyagent_demo import script as script_mod
from tallyagent_demo.asr import CachingASR, DeepgramASR, NullASR
from tallyagent_demo.tts import CachingTTS, MurfTTS, SilentTTS

SCRIPT = "demo/script.yaml"
OUT = Path("demo/out/tallyagent-demo.mp4")


def _speaker(offline: bool, paths: build_mod.Paths):  # type: ignore[no-untyped-def]
    raw = SilentTTS(paths.scratch) if offline else MurfTTS(paths.scratch)
    return CachingTTS(raw, build_mod.audio_cache(paths))


def _listener(offline: bool, demo, paths):  # type: ignore[no-untyped-def]
    if offline:
        durations = {
            beat.id: beat.estimated_seconds for beat in demo.beats
        }
        return NullASR(
            texts={beat.id: beat.say for beat in demo.beats}, durations=durations
        )
    return CachingASR(DeepgramASR(), build_mod.word_cache(paths))


async def stage_voices() -> int:
    """The Murf voice library, so the script can pin a real id, not a guess."""
    speaker = MurfTTS(Path("demo/build/scratch"))
    for voice in await speaker.voices():
        locale = str(voice.get("locale") or voice.get("language") or "")
        if "IN" not in locale.upper():
            continue
        print(
            f"  {voice.get('voiceId') or voice.get('voice_id')}  "
            f"{voice.get('displayName') or voice.get('name')}  "
            f"{locale}  styles={voice.get('availableStyles') or voice.get('styles')}"
        )
    await speaker.aclose()
    return 0


async def stage_tts(demo, paths, offline: bool) -> dict[str, float]:  # type: ignore[no-untyped-def]
    speaker = _speaker(offline, paths)
    lengths = await build_mod.narrate(demo, speaker, paths)
    spoken = getattr(speaker, "spoken", [])
    print(f"  narrated {len(lengths)} beat(s); {len(spoken)} new line(s) synthesised")
    print(f"  total narration: {sum(lengths.values()):.1f}s")
    return lengths


async def stage_asr(demo, paths, offline: bool, strict: bool) -> None:  # type: ignore[no-untyped-def]
    words = await build_mod.transcribe(demo, _listener(offline, demo, paths), paths, strict)
    print(f"  word timings for {len(words)} beat(s)")


def stage_preflight(demo) -> int:  # type: ignore[no-untyped-def]
    """Is the desk ready to record? Fail here, not thirty seconds into a take."""
    from tallyagent_agent.fallback import window as window_mod

    ffmpeg.require()
    print("  ffmpeg and ffprobe: found")

    bounds, reason = window_mod.ensure_visible()
    if bounds is None:
        print(f"  TallyPrime: {reason}", file=sys.stderr)
        return 1
    print(f"  TallyPrime: up at {bounds.left},{bounds.top} {bounds.width}x{bounds.height}")

    placed = capture.place_windows(
        {"TallyPrime": capture.TALLY_RECT, demo.title: capture.TERMINAL_RECT}
    )
    for name, ok in placed.items():
        print(f"  window {name}: {'placed' if ok else 'NOT FOUND'}")

    capture.check_framing(
        capture.Region(),
        {"TallyPrime": capture.TALLY_RECT, "terminal": capture.TERMINAL_RECT},
    )
    print("  framing: both windows inside the 2560x1440 capture region")

    if not placed.get(demo.title):
        print(
            f"\n  Open the TUI in its own console window titled {demo.title!r} first:\n"
            f"    start {demo.title} cmd /k uv run tallyagent tui",
            file=sys.stderr,
        )
        return 1
    return 0


async def stage_record(demo, paths, chapters: list[str], show_cursor: bool) -> int:  # type: ignore[no-untyped-def]
    lengths = {}
    durations_file = paths.root / "durations.json"
    if durations_file.is_file():
        import json

        lengths = json.loads(durations_file.read_text())
    else:
        print("  no durations.json - run the tts stage first", file=sys.stderr)
        return 1

    spotlight = driver_mod.build_spotlight(show_cursor)
    screen = driver_mod.build_cards()
    driver = driver_mod.DemoDriver(
        audio_seconds=lengths,
        spotlight=spotlight,
        card_screen=screen,
        terminal_title=demo.title,
    )

    try:
        for chapter_id in chapters:
            chapter = demo.chapter(chapter_id)
            take_dir = paths.takes / chapter_id
            take_dir.mkdir(parents=True, exist_ok=True)
            number = len(list(take_dir.glob("*.mkv"))) + 1
            output = take_dir / f"{number:03d}.mkv"

            recorder = capture.Recorder(output)
            print(f"  recording {chapter_id} -> {output.name}")
            recorder.start()
            driver.clock.start()
            try:
                timeline = await driver.run_chapter(chapter)
            finally:
                recorder.stop()
            build_mod.save_timeline(timeline, output.with_suffix(".timeline.json"))
            print(f"    {timeline.duration:.1f}s, {len(timeline.spans)} beat(s)")
    finally:
        screen.close()
        spotlight.close()
    return 0


def _selected_takes(demo, paths) -> list[Path]:  # type: ignore[no-untyped-def]
    """The newest take of each chapter, in script order."""
    takes: list[Path] = []
    for chapter in demo.chapters:
        found = sorted((paths.takes / chapter.id).glob("*.mkv"))
        if found:
            takes.append(found[-1])
    return takes


def stage_subtitles(demo, paths) -> int:  # type: ignore[no-untyped-def]
    import json

    words_file = paths.words / "all.json"
    if not words_file.is_file():
        print("  no word timings - run the asr stage first", file=sys.stderr)
        return 1
    from tallyagent_demo.asr import Word

    raw = json.loads(words_file.read_text())
    words = {beat: [Word(**w) for w in rows] for beat, rows in raw.items()}

    timelines = [
        build_mod.load_timeline(take.with_suffix(".timeline.json"))
        for take in _selected_takes(demo, paths)
        if take.with_suffix(".timeline.json").is_file()
    ]
    if not timelines:
        print("  no takes recorded yet", file=sys.stderr)
        return 1
    build_mod.caption(timelines, words, paths)
    return 0


def stage_render(demo, paths) -> int:  # type: ignore[no-untyped-def]
    takes = _selected_takes(demo, paths)
    if not takes:
        print("  no takes recorded yet", file=sys.stderr)
        return 1
    timelines = [
        build_mod.load_timeline(t.with_suffix(".timeline.json"))
        for t in takes
        if t.with_suffix(".timeline.json").is_file()
    ]
    narration = build_mod.narration_track(timelines, paths)
    master = build_mod.stitch(takes, paths)
    srt = paths.root / "demo.srt"
    out = build_mod.finish(master, narration, srt if srt.is_file() else None, OUT)
    print(f"  {out}  ({ffmpeg.duration(out):.1f}s)")
    return 0


async def stage_cleanup(demo) -> int:  # type: ignore[no-untyped-def]
    """Delete what the demo posted, by the REMOTEID it was posted with.

    The books end where they started, and the deletion is itself a proof that
    amend and delete work over XML.
    """
    from datetime import date

    from tallyagent_core.models import VoucherType
    from tallyagent_daemon import config as config_mod
    from tallyagent_daemon import wiring

    config = config_mod.load("config/config.toml", "config/policy.toml")
    config.db_path = "./live.db"
    wired = wiring.build(config)
    rows = await wired.backend.get_vouchers(company=demo.company)

    removed = 0
    for row in rows:
        remote = str(row.get("REMOTEID") or "")
        reference = str(row.get("REFERENCE") or "")
        if not reference.startswith("DEMO-") or not remote:
            continue
        result = await wired.backend.delete_voucher(
            remote,
            VoucherType(str(row.get("VOUCHERTYPENAME"))),
            date(2026, 6, 2),
            f"demo-cleanup-{remote}",
            demo.company,
            master_id=str(row.get("MASTERID") or ""),
        )
        print(f"  {reference}: {'deleted' if result.ok else result.errors}")
        removed += int(result.ok)
    print(f"  {removed} demo voucher(s) removed")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stages",
        nargs="+",
        choices=[
            "all", "voices", "cards", "tts", "asr", "preflight",
            "record", "subtitles", "render", "cleanup",
        ],
    )
    parser.add_argument("--script", default=SCRIPT)
    parser.add_argument("--chapter", action="append", default=[])
    parser.add_argument(
        "--offline",
        action="store_true",
        help="local stand-ins for Murf and Deepgram; no network, no spend",
    )
    parser.add_argument(
        "--loose",
        action="store_true",
        help="warn instead of failing when the audio and the script disagree",
    )
    parser.add_argument("--no-cursor", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    applied = env.load()
    if applied:
        logging.info("read %s from .env", ", ".join(applied))

    demo = script_mod.load(args.script)
    paths = build_mod.Paths()
    paths.ensure()

    stages = args.stages
    if "all" in stages:
        stages = ["cards", "tts", "asr", "record", "subtitles", "render"]

    for stage in stages:
        print(f"[{stage}]")
        if stage == "voices":
            await stage_voices()
        elif stage == "cards":
            for name, path in cards.build_all(Path("demo/assets")).items():
                print(f"  {name}: {path}")
        elif stage == "tts":
            await stage_tts(demo, paths, args.offline)
        elif stage == "asr":
            await stage_asr(demo, paths, args.offline, strict=not args.loose)
        elif stage == "preflight":
            if stage_preflight(demo) != 0:
                return 1
        elif stage == "record":
            chapters = args.chapter or [c.id for c in demo.chapters]
            if await stage_record(demo, paths, chapters, not args.no_cursor) != 0:
                return 1
        elif stage == "subtitles":
            if stage_subtitles(demo, paths) != 0:
                return 1
        elif stage == "render":
            if stage_render(demo, paths) != 0:
                return 1
        elif stage == "cleanup":
            await stage_cleanup(demo)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
