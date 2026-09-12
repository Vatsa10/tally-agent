"""Word timings, read back off the audio that was actually produced.

Murf reports word durations of its own, but taking them on trust means a
mispronunciation never gets noticed: if the engine reads "GSTR-2B" as one
mangled word, the subtitles still claim the script's version and drift from
what is being said. Transcribing the rendered audio independently and comparing
it to the script catches that, and gives the caption timings at the same time.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from tallyagent_demo import env
from tallyagent_demo.cache import Cache, file_key

DEEPGRAM_LISTEN = "https://api.deepgram.com/v1/listen"


@dataclass(slots=True)
class Word:
    text: str
    start: float
    end: float


class ASR(Protocol):
    async def words(self, audio: Path, model: str) -> list[Word]: ...


def normalise(text: str) -> list[str]:
    """Words, stripped to what two transcriptions can fairly be compared on."""
    lowered = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return lowered.split()


#: How different a transcript may be before it is worth a human's attention.
#: Compared letter by letter rather than word by word, because the differences
#: that do not matter are nearly all one letter inside a word - "sale" heard as
#: "sail", "centre" as "center" - while the ones that do matter are whole
#: phrases coming back as something else. Failing on the former would mean
#: crying wolf every run until the check gets switched off, which is the real
#: failure mode here.
SIMILARITY_FLOOR = 0.85


def compare(script_text: str, heard: list[Word]) -> str:
    """Empty when the audio says roughly what the script says; a complaint if not.

    What this is actually guarding against is a line the voice garbled badly
    enough that the burned-in captions would describe something other than what
    a viewer hears. That shows up as a low similarity over the whole line, not
    as one word spelled differently, so the comparison is a ratio rather than an
    equality.
    """
    expected = normalise(script_text)
    actual = normalise(" ".join(w.text for w in heard))
    if not expected:
        return ""
    if not actual:
        return "the audio transcribed to nothing - listen to this line before shipping it"

    ratio = difflib.SequenceMatcher(None, " ".join(expected), " ".join(actual)).ratio()
    if ratio >= SIMILARITY_FLOOR:
        return ""

    differences = [
        f"{want!r} -> {got!r}"
        for want, got in zip(expected, actual, strict=False)
        if want != got
    ][:4]
    return (
        f"the audio only matches the script {ratio:.0%} "
        f"({', '.join(differences) or 'length differs'}). Listen to it, and if "
        "the voice really is wrong, rewrite the phrase phonetically in "
        "demo/script.yaml rather than editing the audio."
    )


class NullASR:
    """Offline stand-in: evenly spaced words across the audio's length.

    Wrong in detail, right in shape - enough to lay out subtitles and prove the
    whole pipeline without a network call.
    """

    def __init__(self, texts: dict[str, str], durations: dict[str, float]) -> None:
        self.texts = texts
        self.durations = durations

    async def words(self, audio: Path, model: str) -> list[Word]:
        text = self.texts.get(audio.stem, "")
        seconds = self.durations.get(audio.stem, 0.0)
        tokens = text.split()
        if not tokens or seconds <= 0:
            return []
        step = seconds / len(tokens)
        return [
            Word(text=token, start=round(i * step, 3), end=round((i + 1) * step, 3))
            for i, token in enumerate(tokens)
        ]


def parse_response(payload: dict[str, Any]) -> list[Word]:
    channels = payload.get("results", {}).get("channels", [])
    if not channels:
        return []
    alternatives = channels[0].get("alternatives", [])
    if not alternatives:
        return []
    return [
        Word(
            text=str(word.get("punctuated_word") or word.get("word") or ""),
            start=float(word.get("start") or 0.0),
            end=float(word.get("end") or 0.0),
        )
        for word in alternatives[0].get("words", [])
    ]


class DeepgramASR:
    #: Bumped whenever the request changes shape. Without it, a cached result
    #: from the old parameters survives the change that was meant to fix it.
    REQUEST_VERSION = "2"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def words(self, audio: Path, model: str) -> list[Word]:
        api_key = env.require("DEEPGRAM_API_KEY", "subtitle timing")
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=180)
        response = await self._client.post(
            DEEPGRAM_LISTEN,
            params={
                "model": model,
                "punctuate": "true",
                # Not smart_format. It rewrites "thirty two hundred" as "3200",
                # which reads worse as a caption under a voice saying the words,
                # and makes the script comparison flag a formatting choice as
                # though the narrator had misspoken.
                "smart_format": "false",
            },
            headers={
                "Authorization": f"Token {api_key}",
                "Content-Type": "audio/wav",
            },
            content=audio.read_bytes(),
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Deepgram refused {audio.name} ({response.status_code}): "
                f"{response.text[:300]}"
            )
        return parse_response(response.json())

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


class CachingASR:
    """Keyed on the audio's own bytes, so re-running costs nothing."""

    def __init__(self, inner: ASR, cache: Cache) -> None:
        self.inner = inner
        self.cache = cache
        self.transcribed: list[str] = []

    async def words(self, audio: Path, model: str) -> list[Word]:
        version = getattr(self.inner, "REQUEST_VERSION", "1")
        key = file_key(audio, salt=f"{model}|{version}")
        if self.cache.path(key, ".json").is_file():
            self.cache.hits += 1
            return [Word(**row) for row in self.cache.get_json(key, ".json")]

        self.cache.misses += 1
        words = await self.inner.words(audio, model)
        self.cache.put_json(
            key, ".json", [{"text": w.text, "start": w.start, "end": w.end} for w in words]
        )
        self.transcribed.append(audio.name)
        return words
