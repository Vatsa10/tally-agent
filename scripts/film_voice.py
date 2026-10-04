"""Narrate the film, and fix every scene's length to its line.

    uv run python scripts/film_voice.py            # Murf, cached per line
    uv run python scripts/film_voice.py --offline  # silence of the right length

The film is drawn to a timeline, not filmed in real time, so there is no drift
to fight: each scene lasts as long as its narration plus a breath (never less
than the scene's own minimum), the narration track is laid end to end on the
same boundaries, and ``film/data/timing.json`` tells the page where every scene
starts. Re-voicing one line re-times one scene and nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from tallyagent_demo import env
from tallyagent_demo.build import Paths, audio_cache
from tallyagent_demo.ffmpeg import duration
from tallyagent_demo.script import Voice
from tallyagent_demo.tts import CachingTTS, MurfTTS, SilentTTS, silence_wav

NARRATION = Path("film/narration.json")
TIMING = Path("film/data/timing.json")
BUILD = Path("film/build")

#: Silence before a scene's line and after it, so a cut never clips a word.
LEAD_IN = 0.45
TAIL = 0.9


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    env.load()

    spec = json.loads(NARRATION.read_text(encoding="utf-8"))
    clips_file = Path("film/data/clips.json")
    clips = json.loads(clips_file.read_text(encoding="utf-8")) if clips_file.is_file() else {}
    voice = Voice(**{k: v for k, v in spec["voice"].items() if k in Voice.model_fields})
    paths = Paths()
    raw = SilentTTS(paths.scratch) if args.offline else MurfTTS(paths.scratch)
    speaker = CachingTTS(raw, audio_cache(paths))

    BUILD.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    scenes = []
    cursor = 0.0
    for scene in spec["scenes"]:
        clip = clips.get(scene.get("clip", "")) if scene.get("clip") else None
        if scene.get("clip") and not (clip and clip.get("ok")):
            # A clip that did not record cleanly is left out rather than shown
            # failing: the film shows what the product does, not a retake.
            print(f"  {scene['id']:<10} skipped - clip {scene['clip']!r} not recorded cleanly")
            continue
        spoken = await speaker.speak(scene["say"], voice)
        seconds = duration(spoken.path) or spoken.seconds
        length = max(float(scene.get("min", 0)), LEAD_IN + seconds + TAIL)
        entry = {
            "id": scene["id"], "start": round(cursor, 3), "duration": round(length, 3),
            "say": scene["say"], "voice_at": round(cursor + LEAD_IN, 3),
            "voice_seconds": round(seconds, 3),
        }
        # What the frame draws around the scene: the time of day in the
        # firm's story, the one-line payoff, an act's title.
        entry.update({k: scene[k] for k in ("clock", "payoff", "act", "title") if k in scene})
        if clip:
            speed = float(scene.get("speed", 1.0))
            # The footage sets the floor: the scene lasts as long as the clip
            # takes at its speed, plus the frame's own entrance and exit.
            length = max(length, float(clip["seconds"]) / speed + 1.4)
            entry.update(duration=round(length, 3), clip=scene["clip"], speed=speed,
                         clip_at=round(cursor + 0.6, 3), clip_seconds=clip["seconds"],
                         events=clip.get("events", []))
        scenes.append(entry)
        lead = BUILD / f"lead-{scene['id']}.wav"
        lead.write_bytes(silence_wav(LEAD_IN))
        tail = BUILD / f"tail-{scene['id']}.wav"
        tail.write_bytes(silence_wav(max(0.05, length - LEAD_IN - seconds)))
        parts += [lead, spoken.path, tail]
        cursor += length
        print(f"  {scene['id']:<10} {length:5.1f}s  (voice {seconds:4.1f}s)")

    listing = BUILD / "narration.txt"
    listing.write_text(
        "".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8"
    )
    out = BUILD / "narration.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-ar", "48000", "-ac", "1", str(out)],
        check=True,
    )
    TIMING.parent.mkdir(parents=True, exist_ok=True)
    TIMING.write_text(json.dumps({"total": round(cursor, 3), "scenes": scenes}, indent=2),
                      encoding="utf-8")
    print(f"  total {cursor:.1f}s; {len(speaker.spoken)} line(s) newly voiced -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
