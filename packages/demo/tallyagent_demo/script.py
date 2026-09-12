"""The demo script: chapters, beats, narration copy, and what happens on screen.

Shaped deliberately like ``tallyagent_channels.tui.script_runner.Scenario`` -
a list of steps with a note each - because the demo *is* a scenario that happens
to be filmed. A beat names an action on the driver rather than embedding
behaviour, so the copy can be rewritten without touching code, and the code can
be tested without reading the copy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

#: Pace used to estimate a beat before any audio exists. Murf at rate -4 lands
#: near this, and it is only ever an estimate: the real duration comes from
#: ffprobe once the wav is on disk.
WORDS_PER_SECOND = 2.6


class Voice(BaseModel):
    """Which Murf voice reads the script, pinned in the file rather than code."""

    voice_id: str = "en-IN-eashwar"
    style: str = "Narration"
    rate: int = -4
    pitch: int = 0
    sample_rate: int = 48000
    fmt: str = "WAV"

    def request(self, text: str) -> dict[str, Any]:
        """The Murf generate body. Also the cache key material - so every field
        that can change the audio has to be in here."""
        return {
            "voiceId": self.voice_id,
            "text": text,
            "style": self.style,
            "rate": self.rate,
            "pitch": self.pitch,
            "sampleRate": self.sample_rate,
            "format": self.fmt,
            "channelType": "MONO",
        }


class Beat(BaseModel):
    """One sentence of narration and the one thing on screen while it plays."""

    id: str = Field(min_length=1)
    say: str = Field(min_length=1)
    #: Name of a ``DemoDriver`` method. Validated against the driver in tests,
    #: so a renamed method fails CI rather than the recording session.
    action: str = "pause"
    args: dict[str, Any] = Field(default_factory=dict)
    #: Extra quiet after the line, for a beat that needs to breathe.
    tail_pad: float = 0.4
    #: Abort the chapter rather than ship a dead pause this long.
    max_overrun: float = 8.0

    @property
    def word_count(self) -> int:
        return len(self.say.split())

    @property
    def estimated_seconds(self) -> float:
        return round(self.word_count / WORDS_PER_SECOND, 2)


class Chapter(BaseModel):
    """The retake unit. One recording, one file, one chance to fluff it."""

    id: str = Field(min_length=1)
    title: str = ""
    beats: list[Beat] = Field(min_length=1)

    @property
    def estimated_seconds(self) -> float:
        return round(
            sum(b.estimated_seconds + b.tail_pad for b in self.beats), 2
        )


class DemoScript(BaseModel):
    title: str = "tallyagent"
    voice: Voice = Field(default_factory=Voice)
    #: Deepgram model, here rather than in code so switching it is a config edit
    #: and the ASR cache key changes with it.
    asr_model: str = "nova-3"
    company: str = "TA-Demo Traders"
    chapters: list[Chapter] = Field(min_length=1)

    @property
    def beats(self) -> list[Beat]:
        return [beat for chapter in self.chapters for beat in chapter.beats]

    @property
    def estimated_seconds(self) -> float:
        return round(sum(c.estimated_seconds for c in self.chapters), 2)

    def chapter(self, chapter_id: str) -> Chapter:
        for chapter in self.chapters:
            if chapter.id == chapter_id:
                return chapter
        known = ", ".join(c.id for c in self.chapters)
        raise KeyError(f"no chapter {chapter_id!r}; have: {known}")

    def model_post_init(self, _ctx: object) -> None:
        seen: set[str] = set()
        for beat in self.beats:
            if beat.id in seen:
                raise ValueError(
                    f"duplicate beat id {beat.id!r} - ids address cached audio, "
                    "so two beats sharing one would share a voice line"
                )
            seen.add(beat.id)


def load(path: str | Path = "demo/script.yaml") -> DemoScript:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return DemoScript.model_validate(raw)
