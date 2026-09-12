"""Narration and transcription: request shape, caching, and the mismatch guard.

No network. respx stands in for Murf and Deepgram, which is how the rest of the
repo tests HTTP. The point of most of these is money: a cache that silently
misses turns every re-render into another bill.
"""

from __future__ import annotations

import os

import httpx
import pytest
import respx

from tallyagent_demo import asr as asr_mod
from tallyagent_demo import env, tts
from tallyagent_demo.asr import CachingASR, DeepgramASR, Word
from tallyagent_demo.cache import Cache
from tallyagent_demo.script import Voice
from tallyagent_demo.tts import CachingTTS, MurfTTS, SilentTTS


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("MURF_API_KEY", "murf-test-key")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "deepgram-test-key")


# --- .env -------------------------------------------------------------------


def test_env_parsing_handles_what_a_real_file_contains():
    parsed = env.parse(
        '# a comment\n\nexport MURF_API_KEY="abc 123"\n'
        "DEEPGRAM_API_KEY='xyz'\nBARE=value\nnot a pair\n"
    )
    assert parsed == {
        "MURF_API_KEY": "abc 123",
        "DEEPGRAM_API_KEY": "xyz",
        "BARE": "value",
    }


def test_a_real_environment_variable_is_never_overridden(tmp_path, monkeypatch):
    """Exporting a key in the shell, or in CI, has to win over a stale file."""
    monkeypatch.setenv("MURF_API_KEY", "from-the-shell")
    dotenv = tmp_path / ".env"
    dotenv.write_text("MURF_API_KEY=from-the-file\nOTHER=set-me\n")

    applied = env.load(dotenv)

    assert os.environ["MURF_API_KEY"] == "from-the-shell"
    assert applied == ["OTHER"]
    monkeypatch.delenv("OTHER", raising=False)


def test_a_missing_key_names_itself_and_where_to_put_it(monkeypatch):
    monkeypatch.delenv("MURF_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        env.require("MURF_API_KEY", "narration")
    assert "MURF_API_KEY" in str(excinfo.value)
    assert ".env" in str(excinfo.value)


# --- Murf -------------------------------------------------------------------


def _murf_routes(wav: bytes = b"RIFFfake") -> respx.MockRouter:
    router = respx.mock(assert_all_called=False)
    router.post(tts.MURF_GENERATE).mock(
        return_value=httpx.Response(
            200,
            json={
                "audioFile": "https://cdn.murf.test/line.wav",
                "audioLengthInSeconds": 7.25,
            },
        )
    )
    router.get("https://cdn.murf.test/line.wav").mock(
        return_value=httpx.Response(200, content=wav)
    )
    return router


async def test_murf_is_asked_in_the_shape_its_api_expects(tmp_path, keys):
    with _murf_routes() as router:
        spoken = await MurfTTS(tmp_path).speak("A sale, in plain words.", Voice())

        # respx forgets its calls on exit, so read them inside the block.
        request = router.calls[0].request
        assert request.headers["api-key"] == "murf-test-key"
        body = request.read().decode().replace(" ", "")
        assert '"voiceId":"en-IN-eashwar"' in body
        assert '"format":"WAV"' in body

    assert spoken.seconds == 7.25
    assert spoken.path.read_bytes() == b"RIFFfake"


async def test_murf_refusing_says_so_rather_than_writing_an_empty_wav(tmp_path, keys):
    with respx.mock as router:
        router.post(tts.MURF_GENERATE).mock(
            return_value=httpx.Response(401, text="bad key")
        )
        with pytest.raises(RuntimeError) as excinfo:
            await MurfTTS(tmp_path).speak("hello", Voice())
    assert "401" in str(excinfo.value)


async def test_speaking_the_same_line_twice_costs_one_call(tmp_path, keys):
    cache = Cache(tmp_path / "audio")
    speaker = CachingTTS(MurfTTS(tmp_path / "raw"), cache)

    with _murf_routes() as router:
        await speaker.speak("Reports come through the same tools.", Voice())
        await speaker.speak("Reports come through the same tools.", Voice())
        assert len(router.calls) == 2, "one generate, one download - then nothing"

    assert speaker.spoken == ["Reports come through the same tools."]


async def test_editing_one_line_re_synthesises_only_that_line(tmp_path, keys):
    cache = Cache(tmp_path / "audio")
    speaker = CachingTTS(MurfTTS(tmp_path / "raw"), cache)

    with _murf_routes():
        await speaker.speak("first line", Voice())
        await speaker.speak("second line", Voice())
        await speaker.speak("first line", Voice())
        await speaker.speak("second line, edited", Voice())

    assert speaker.spoken == ["first line", "second line", "second line, edited"]


async def test_changing_the_voice_invalidates_the_cache(tmp_path, keys):
    """A new voice is new audio, however identical the words."""
    cache = Cache(tmp_path / "audio")
    speaker = CachingTTS(MurfTTS(tmp_path / "raw"), cache)

    with _murf_routes():
        await speaker.speak("same words", Voice(voice_id="en-IN-eashwar"))
        await speaker.speak("same words", Voice(voice_id="en-IN-arohi"))

    assert len(speaker.spoken) == 2


async def test_a_rehearsal_and_a_paid_line_never_share_a_cache_entry(tmp_path, keys):
    """Otherwise the first real build silently ships the rehearsal's silence."""
    cache = Cache(tmp_path / "audio")
    rehearsal = CachingTTS(SilentTTS(tmp_path / "raw"), cache)
    await rehearsal.speak("same words", Voice())

    real = CachingTTS(MurfTTS(tmp_path / "raw"), cache)
    with _murf_routes():
        spoken = await real.speak("same words", Voice())

    assert real.spoken == ["same words"], "the silence was not mistaken for a voice"
    assert spoken.path.read_bytes() == b"RIFFfake"


async def test_the_offline_voice_produces_a_real_wav_of_plausible_length(tmp_path):
    spoken = await SilentTTS(tmp_path).speak(" ".join(["word"] * 26), Voice())
    assert spoken.seconds == 10.0
    assert spoken.path.read_bytes().startswith(b"RIFF")


# --- Deepgram ---------------------------------------------------------------


DEEPGRAM_BODY = {
    "results": {
        "channels": [
            {
                "alternatives": [
                    {
                        "words": [
                            {"word": "a", "punctuated_word": "A", "start": 0.0, "end": 0.2},
                            {"word": "sale", "punctuated_word": "sale,", "start": 0.2, "end": 0.6},
                        ]
                    }
                ]
            }
        ]
    }
}


async def test_deepgram_is_called_with_the_token_and_the_chosen_model(tmp_path, keys):
    audio = tmp_path / "line.wav"
    audio.write_bytes(b"RIFFfake")

    with respx.mock as router:
        route = router.post(asr_mod.DEEPGRAM_LISTEN).mock(
            return_value=httpx.Response(200, json=DEEPGRAM_BODY)
        )
        words = await DeepgramASR().words(audio, model="nova-3")

        request = route.calls[0].request
        assert request.headers["Authorization"] == "Token deepgram-test-key"
        assert "model=nova-3" in str(request.url)
        assert "smart_format=false" in str(request.url), (
            "smart_format rewrites spoken numbers as digits, which makes the "
            "captions disagree with the voice"
        )

    assert [w.text for w in words] == ["A", "sale,"]


async def test_transcribing_the_same_audio_twice_costs_one_call(tmp_path, keys):
    audio = tmp_path / "line.wav"
    audio.write_bytes(b"RIFFfake")
    listener = CachingASR(DeepgramASR(), Cache(tmp_path / "words"))

    with respx.mock as router:
        router.post(asr_mod.DEEPGRAM_LISTEN).mock(
            return_value=httpx.Response(200, json=DEEPGRAM_BODY)
        )
        await listener.words(audio, model="nova-3")
        await listener.words(audio, model="nova-3")
        assert len(router.calls) == 1

    assert listener.transcribed == ["line.wav"]


def test_audio_that_matches_the_script_raises_no_complaint():
    heard = [Word("G-S-T-R", 0, 1), Word("two", 1, 2), Word("B.", 2, 3)]
    assert asr_mod.compare("G-S-T-R two B", heard) == ""


def test_a_transcription_quirk_is_not_treated_as_a_mispronunciation():
    """Transcription hears "sale" as "sail" constantly. Failing on that cries wolf."""
    heard = [
        Word("A", 0, 1),
        Word("sail,", 1, 2),
        Word("in", 2, 3),
        Word("plain", 3, 4),
        Word("words.", 4, 5),
    ]
    assert asr_mod.compare("A sale, in plain words.", heard) == ""


def test_a_badly_garbled_line_is_flagged_with_what_was_heard():
    heard = [Word("porridge", 0, 1), Word("elephant", 1, 2), Word("seventeen", 2, 3)]
    complaint = asr_mod.compare("trial balance please", heard)
    assert "%" in complaint
    assert "demo/script.yaml" in complaint
    assert "editing the audio" in complaint


def test_silence_where_a_line_should_be_is_always_worth_hearing_about():
    assert "transcribed to nothing" in asr_mod.compare("anything at all", [])


def test_a_whole_clause_going_missing_is_caught():
    heard = [Word("trial", 0, 1)]
    assert asr_mod.compare("trial balance please, and outstanding too", heard) != ""


async def test_the_offline_listener_spreads_words_across_the_audio(tmp_path):
    audio = tmp_path / "b01.wav"
    audio.write_bytes(b"RIFF")
    listener = asr_mod.NullASR(texts={"b01": "one two three four"}, durations={"b01": 4.0})

    words = await listener.words(audio, model="none")

    assert [w.text for w in words] == ["one", "two", "three", "four"]
    assert words[0].start == 0.0
    assert words[-1].end == 4.0
