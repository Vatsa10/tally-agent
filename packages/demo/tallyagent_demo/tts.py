"""Narration: text in, a wav and its exact length out.

Three implementations behind one protocol. ``MurfTTS`` is the real one.
``SilentTTS`` renders silence of a plausible length so the whole pipeline - the
recording, the subtitles, the mux - can be rehearsed end to end for nothing,
which is what makes it affordable to get the pacing right before paying for a
single spoken word. ``CachingTTS`` wraps either and is why re-recording video
costs no API calls at all.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from tallyagent_demo import env
from tallyagent_demo.cache import Cache, key_for
from tallyagent_demo.script import WORDS_PER_SECOND, Voice

MURF_GENERATE = "https://api.murf.ai/v1/speech/generate"
MURF_VOICES = "https://api.murf.ai/v1/speech/voices"


@dataclass(slots=True)
class Spoken:
    """One rendered line."""

    path: Path
    seconds: float
    #: Whatever the provider said about it, kept for provenance.
    detail: dict[str, Any]


class TTS(Protocol):
    async def speak(self, text: str, voice: Voice) -> Spoken: ...


def silence_wav(seconds: float, sample_rate: int = 48000) -> bytes:
    """A real, playable, silent wav. Not a stub - ffmpeg has to accept it."""
    frames = max(1, int(seconds * sample_rate))
    data = b"\x00\x00" * frames
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    return header + data


class SilentTTS:
    """Offline stand-in: silence as long as the line would take to read.

    Lets `make_demo.py all --offline` produce a genuinely watchable, subtitled
    video with correct timing and zero spend.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    async def speak(self, text: str, voice: Voice) -> Spoken:
        seconds = round(max(1.0, len(text.split()) / WORDS_PER_SECOND), 2)
        key = key_for({"silent": True, **voice.request(text)})
        path = self.root / f"{key}.wav"
        path.write_bytes(silence_wav(seconds, voice.sample_rate))
        return Spoken(path=path, seconds=seconds, detail={"provider": "silent"})


class MurfTTS:
    """Murf's generate endpoint: one POST for the metadata, one GET for the wav."""

    def __init__(self, root: Path, client: httpx.AsyncClient | None = None) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._client = client

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=120)
        return self._client

    async def speak(self, text: str, voice: Voice) -> Spoken:
        api_key = env.require("MURF_API_KEY", "narration")
        client = await self._http()
        body = voice.request(text)

        response = await client.post(
            MURF_GENERATE, json=body, headers={"api-key": api_key}
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Murf refused this line ({response.status_code}): "
                f"{response.text[:300]}"
            )
        payload = response.json()
        url = payload.get("audioFile")
        if not url:
            raise RuntimeError(f"Murf returned no audio for: {text[:60]!r}")

        audio = await client.get(url)
        audio.raise_for_status()
        key = key_for(body)
        path = self.root / f"{key}.wav"
        path.write_bytes(audio.content)
        return Spoken(
            path=path,
            seconds=float(payload.get("audioLengthInSeconds") or 0.0),
            detail=payload,
        )

    async def voices(self) -> list[dict[str, Any]]:
        """The voice library, so the script can pin a real id rather than a guess."""
        client = await self._http()
        response = await client.get(
            MURF_VOICES, headers={"api-key": env.require("MURF_API_KEY", "voice listing")}
        )
        response.raise_for_status()
        listed = response.json()
        if isinstance(listed, dict):
            found = listed.get("voices") or listed.get("data") or []
            return list(found)
        return list(listed)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


class CachingTTS:
    """Speak only what has not been spoken before.

    Keyed on every field that can change the bytes, so editing one sentence
    re-synthesises one sentence and re-recording the video synthesises nothing.
    """

    def __init__(self, inner: TTS, cache: Cache, tag: str = "") -> None:
        self.inner = inner
        self.cache = cache
        # Which provider made these. Without it, a rehearsal's silence and a
        # paid-for voice line share a cache key, and the first real build
        # happily ships three minutes of silence.
        self.tag = tag or type(inner).__name__
        #: Lines that actually went to the provider this run.
        self.spoken: list[str] = []

    async def speak(self, text: str, voice: Voice) -> Spoken:
        key = key_for({"provider": self.tag, **voice.request(text)})
        path = self.cache.path(key, ".wav")
        if path.is_file():
            self.cache.hits += 1
            detail = self.cache.sidecar(key)
            return Spoken(
                path=path,
                seconds=float(detail.get("audioLengthInSeconds") or 0.0),
                detail=detail,
            )

        self.cache.misses += 1
        spoken = await self.inner.speak(text, voice)
        data = spoken.path.read_bytes()
        stored = self.cache.put(
            key, ".wav", data, sidecar={**spoken.detail, "text": text}
        )
        if spoken.path != stored and spoken.path.is_file():
            spoken.path.unlink()
        self.spoken.append(text)
        return Spoken(path=stored, seconds=spoken.seconds, detail=spoken.detail)
