"""Wake cue and mic-ignore patches in weixue_patches, against upstream handleAudioMessage
loaded from the vendored source and the cue clips in server/overlay."""
import asyncio
import re
import time
import types
import wave

import pytest
import yaml

import weixue_patches
from tests.conftest import LOGGER, OVERLAY_DIR, SERVER_DIR
from tests.test_conversation_timing import REMOVE_PUNCTUATION, _upstream_function

CUE_ONE = {"file": "config/assets/wake_cues/hmm.wav", "text": "Hmm?"}
CUES = [CUE_ONE]


@pytest.fixture(autouse=True)
def fast_cue(monkeypatch):
    monkeypatch.setattr(weixue_patches, "CUE_DELAY_S", 0.05)


class Conn:
    def __init__(self, cues=CUES, tts=True):
        self.config = {"wakeup_words": ["你好小智", "Jarvis"], "wake_cues": cues}
        self.client_listen_mode = "auto"
        self.client_have_voice = True
        self.client_is_speaking = False
        self.client_abort = True
        self.last_activity_time = 0.0
        self.sentence_id = "old"
        self.resets = 0
        self.tts = object() if tts else None
        self.vad_last_voice_time = 0.0

    def reset_audio_states(self):
        self.resets += 1


class Upstream:
    def __init__(self):
        self.calls = []

    async def handle(self, conn, msg_json):
        self.calls.append(msg_json)


def _wrapped(played):
    upstream = Upstream()

    async def play(conn, cue):
        played.append(cue)

    wrapper = weixue_patches.acknowledge_wake(Upstream.handle, REMOVE_PUNCTUATION, play)
    return upstream, wrapper


@pytest.mark.parametrize("text", ["Jarvis", "jarvis.", "你好小智"])
async def test_wake_detect_plays_a_cue_and_keeps_the_audio(text):
    played = []
    upstream, wrapper = _wrapped(played)
    conn = Conn()
    await wrapper(upstream, conn, {"type": "listen", "state": "detect", "text": text, "mode": "realtime"})
    await conn.weixue_wake_cue
    assert upstream.calls == []
    assert played == CUES
    assert conn.resets == 0
    assert conn.client_have_voice is True
    assert conn.client_listen_mode == "realtime"
    assert time.time() * 1000 - conn.last_activity_time < 1000


async def test_wake_start_opens_the_live_window():
    played = []
    upstream, wrapper = _wrapped(played)
    conn = Conn()
    conn.weixue_live_voice = True
    await wrapper(upstream, conn, {"state": "detect", "text": "Jarvis"})
    assert conn.weixue_live_voice is False
    assert getattr(conn, "weixue_live_since", None) is None
    await wrapper(upstream, conn, {"state": "start", "mode": "realtime"})
    assert time.time() - conn.weixue_live_since < 1


class FakeVad:
    def __init__(self, voice):
        self.voice = voice

    def is_vad(self, conn, _frame):
        conn.client_voice_stop = True
        return self.voice


@pytest.mark.parametrize(
    "elapsed, voice, stop, live_voice",
    [
        (0.1, True, False, False),
        (0.5, True, False, True),
        (0.5, False, False, False),
        (1.0, False, True, False),
        (1.0, True, True, True),
    ],
)
def test_vad_hold_after_a_wake(elapsed, voice, stop, live_voice):
    is_vad = weixue_patches.hold_wake_utterance(FakeVad.is_vad)
    conn = Conn()
    conn.weixue_live_voice = False
    conn.weixue_live_since = time.time() - elapsed
    assert is_vad(FakeVad(voice), conn, b"") is voice
    assert conn.client_voice_stop is stop
    assert conn.weixue_live_voice is live_voice


def test_vad_without_a_wake_is_untouched():
    is_vad = weixue_patches.hold_wake_utterance(FakeVad.is_vad)
    conn = Conn()
    assert is_vad(FakeVad(True), conn, b"") is True
    assert conn.client_voice_stop is True
    assert not hasattr(conn, "weixue_live_voice")


async def test_only_the_first_listen_start_after_a_wake_keeps_the_audio():
    played = []
    upstream, wrapper = _wrapped(played)
    conn = Conn()
    start = {"type": "listen", "state": "start", "mode": "realtime"}
    await wrapper(upstream, conn, {"state": "detect", "text": "Jarvis"})
    conn.client_listen_mode = "auto"
    await wrapper(upstream, conn, start)
    assert upstream.calls == []
    assert conn.client_listen_mode == "realtime"
    await wrapper(upstream, conn, start)
    assert upstream.calls == [start]


async def test_cue_is_picked_from_the_configured_list():
    cues = [{"file": f"c{i}.wav", "text": str(i)} for i in range(3)]
    played = []
    upstream, wrapper = _wrapped(played)
    for _ in range(60):
        conn = Conn(cues=cues)
        await wrapper(upstream, conn, {"state": "detect", "text": "Jarvis"})
        await conn.weixue_wake_cue
    assert {c["text"] for c in played} == {"0", "1", "2"}


@pytest.mark.parametrize(
    "msg, cues",
    [
        ({"state": "detect", "text": "Jarvis"}, []),
        ({"state": "detect", "text": "Jarvis, what time is it?"}, CUES),
        ({"state": "detect", "text": "weixue:proactive"}, CUES),
        ({"state": "start", "mode": "realtime"}, CUES),
    ],
)
async def test_other_listen_messages_reach_upstream(msg, cues):
    played = []
    upstream, wrapper = _wrapped(played)
    await wrapper(upstream, Conn(cues=cues), msg)
    assert upstream.calls == [msg]
    assert played == []


class Recorder:
    def __init__(self):
        self.events = []

    async def audio_to_data(self, path):
        self.events.append(("load", path))
        return [b"p1", b"p2"]

    async def send_tts_message(self, conn, state, text=None):
        self.events.append(("tts", state, conn.client_is_speaking, time.monotonic()))

    async def send_audio_message(self, conn, kind, packets, text, sentence_id):
        self.events.append(("audio", kind, packets, text, sentence_id == conn.sentence_id, time.monotonic()))


def _player(rec):
    return weixue_patches.wake_cue_player(
        rec.audio_to_data, rec.send_tts_message, rec.send_audio_message, "FIRST", "LAST", LOGGER
    )


async def test_player_sends_start_then_audio_after_the_decoder_reset(log_records):
    rec = Recorder()
    conn = Conn()
    await _player(rec)(conn, CUES[0])
    load, start, first, last = rec.events
    assert load == ("load", CUES[0]["file"])
    assert start[:3] == ("tts", "start", False)
    assert first[:5] == ("audio", "FIRST", [b"p1", b"p2"], "Hmm?", True)
    assert last[:5] == ("audio", "LAST", [], None, True)
    assert first[5] - start[3] >= weixue_patches.DECODER_RESET_S * 0.9
    assert conn.client_is_speaking is True
    assert conn.client_abort is False
    assert conn.sentence_id != "old"
    assert any("wake cue: Hmm?" in message for _, message in log_records)


async def test_player_skips_the_cue_after_live_voice(log_records):
    rec = Recorder()
    conn = Conn()
    conn.weixue_live_voice = True
    await _player(rec)(conn, CUE_ONE)
    assert rec.events == []
    assert conn.sentence_id == "old"
    assert any("user speaking" in message for _, message in log_records)


async def test_player_skips_when_tts_is_not_ready(monkeypatch, log_records):
    monkeypatch.setattr(weixue_patches, "TTS_READY_S", 0.1)
    rec = Recorder()
    await _player(rec)(Conn(tts=False), CUES[0])
    assert rec.events == []
    assert any("TTS not ready" in message for _, message in log_records)


async def test_player_waits_for_tts():
    rec = Recorder()
    conn = Conn(tts=False)

    async def tts_ready():
        await asyncio.sleep(0.15)
        conn.tts = object()

    await asyncio.gather(_player(rec)(conn, CUES[0]), tts_ready())
    assert [e[1] for e in rec.events] == [CUES[0]["file"], "start", "FIRST", "LAST"]


class AudioConn:
    def __init__(self):
        self.vad = types.SimpleNamespace(is_vad=lambda _conn, _frame: True)
        self.asr = self
        self.client_aec = False
        self.received = []

    async def receive_audio(self, _conn, frame, have_voice):
        self.received.append((frame, have_voice))


async def _no_voice(_conn, _have_voice):
    return None


UPSTREAM_HANDLE_AUDIO = _upstream_function(
    "core/handle/receiveAudioHandle.py",
    "handleAudioMessage",
    {"asyncio": asyncio, "no_voice_close_connect": _no_voice, "handleAbortMessage": _no_voice},
)
UPSTREAM_RESUME = _upstream_function(
    "core/handle/receiveAudioHandle.py", "resume_vad_detection", {"asyncio": asyncio}
)
UPSTREAM_HANDLE_AUDIO.__globals__["resume_vad_detection"] = UPSTREAM_RESUME


async def test_upstream_drops_audio_after_a_detect():
    conn = AudioConn()
    conn.just_woken_up = True
    await UPSTREAM_HANDLE_AUDIO(conn, b"frame")
    conn.vad_resume_task.cancel()
    assert conn.received == []


async def test_patched_connection_keeps_audio_after_a_detect():
    class PatchedConn(AudioConn):
        just_woken_up = property(weixue_patches.never_just_woken_up, lambda _conn, _value: None)

    conn = PatchedConn()
    conn.just_woken_up = True
    await UPSTREAM_HANDLE_AUDIO(conn, b"frame")
    assert conn.received == [(b"frame", True)]


def test_configured_cue_clips_are_short_mono_wavs():
    text = (SERVER_DIR / "config" / "config.template.yaml").read_text(encoding="utf-8")
    template = yaml.safe_load(re.sub(r"@@[A-Z_?]+@@", "x", text))
    cues = template["wake_cues"]
    assert len(cues) == 3
    assert template["enable_greeting"] is False
    for cue in cues:
        assert cue["text"]
        with wave.open(str(OVERLAY_DIR / cue["file"]), "rb") as clip:
            seconds = clip.getnframes() / clip.getframerate()
            assert clip.getnchannels() == 1
            assert 0.2 < seconds < 1.0


CHECK = weixue_patches.ignore_bare_wake_word(REMOVE_PUNCTUATION, LOGGER)


@pytest.mark.parametrize("text", ["Huh?", "Hmm?", "hm.", "Mm-hm?", "Yes?"])
async def test_cue_echo_right_after_a_cue_is_consumed(text, log_records):
    conn = Conn(cues=[{"file": "x.wav", "text": "Mm-hm?"}])
    conn.weixue_cue_started = time.monotonic() - 2
    assert await CHECK(conn, text) is True
    assert any("wake cue echo ignored" in message for _, message in log_records)


@pytest.mark.parametrize("text", ["Yes, turn on the lights.", "What time is it?", "", "?"])
async def test_other_transcripts_after_a_cue_reach_the_llm(text):
    conn = Conn()
    conn.weixue_cue_started = time.monotonic() - 1
    assert await CHECK(conn, text) is False


async def test_cue_words_long_after_the_cue_or_without_one_reach_the_llm():
    conn = Conn()
    assert await CHECK(conn, "Yes.") is False
    conn.weixue_cue_started = time.monotonic() - weixue_patches.CUE_ECHO_S - 1
    assert await CHECK(conn, "Yes.") is False


async def test_player_marks_the_cue_start():
    rec = Recorder()
    conn = Conn()
    before = time.monotonic()
    await _player(rec)(conn, CUES[0])
    assert before <= conn.weixue_cue_started <= time.monotonic()
