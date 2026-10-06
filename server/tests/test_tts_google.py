"""Google Cloud TTS provider (server/overlay/core/providers/tts/google_tts.py).

Mocked by default; RUN_LIVE_TESTS=1 adds one real smoke test using the service
account at GOOGLE_APPLICATION_CREDENTIALS (default: ha-agent google-cloud-ha.json).
"""
import asyncio
import io
import math
import os
import queue
import struct
import threading
import time
import wave
from types import SimpleNamespace

import pytest
from google.api_core import exceptions as gexc
from google.cloud import texttospeech

from tests.conftest import CREDENTIALS, LIVE, TEST_CONFIG

from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
from core.utils.tts import create_instance

try:
    from core.utils.opus_encoder_utils import OpusEncoderUtils

    OpusEncoderUtils(24000, 1, 60)
    HAVE_OPUS = True
except Exception:
    HAVE_OPUS = False

REAL_CLIENT = texttospeech.TextToSpeechClient
SAMPLE_RATE = 24000
FRAME_BYTES = SAMPLE_RATE * 60 // 1000 * 2


def _pcm(seconds, freq=440.0):
    n = int(SAMPLE_RATE * seconds)
    return b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE)))
        for i in range(n)
    )


def _wav(pcm):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


class FakeCall:
    def __init__(self, chunks, fail_mid=None):
        self._chunks = list(chunks)
        self._fail_mid = fail_mid
        self.cancelled = False
        self._i = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.cancelled:
            raise gexc.Cancelled("cancelled")
        if self._i >= len(self._chunks):
            raise StopIteration
        if self._fail_mid is not None and self._i == self._fail_mid:
            raise self._fail_mid_exc()
        chunk = self._chunks[self._i]
        self._i += 1
        return SimpleNamespace(audio_content=chunk)

    def _fail_mid_exc(self):
        return gexc.InternalServerError("stream broke")

    def cancel(self):
        self.cancelled = True


class FakeClient:
    """Stands in for texttospeech.TextToSpeechClient."""

    instances = []
    chunks = [_pcm(0.05), _pcm(0.07), b"\x01\x02\x03"]  # last chunk is odd-length
    call_error = None
    stale_after_chunk = None  # index after which the test flips conn.sentence_id

    def __init__(self, *args, **kwargs):
        self.sync_calls = []
        self.stream_calls = []
        self.calls = []
        self.transport = SimpleNamespace(closed=False)
        self.transport.close = lambda: setattr(self.transport, "closed", True)
        FakeClient.instances.append(self)

    @classmethod
    def from_service_account_file(cls, filename, *args, **kwargs):
        inst = cls()
        inst.credentials_path = filename
        return inst

    def synthesize_speech(self, input, voice, audio_config, timeout=None):
        self.sync_calls.append((input.text, voice.name, voice.language_code, audio_config))
        return SimpleNamespace(audio_content=_wav(_pcm(0.2)))

    def streaming_synthesize(self, requests, timeout=None):
        reqs = list(requests)
        config = reqs[0].streaming_config
        text = reqs[1].input.text
        self.stream_calls.append(
            {
                "text": text,
                "voice": config.voice.name,
                "language": config.voice.language_code,
                "encoding": config.streaming_audio_config.audio_encoding,
                "sample_rate": config.streaming_audio_config.sample_rate_hertz,
                "speaking_rate": config.streaming_audio_config.speaking_rate,
                "timeout": timeout,
            }
        )
        if FakeClient.call_error is not None:
            raise FakeClient.call_error
        call = FakeCall(FakeClient.chunks)
        self.calls.append(call)
        return call


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    FakeClient.instances = []
    FakeClient.call_error = None
    FakeClient.chunks = [_pcm(0.05), _pcm(0.07), b"\x01\x02\x03"]
    monkeypatch.setattr(texttospeech, "TextToSpeechClient", FakeClient)
    yield FakeClient


def _config(**overrides):
    cfg = dict(TEST_CONFIG["TTS"]["GoogleTTS"])
    cfg.update(overrides)
    return cfg


def _provider(**overrides):
    return create_instance("google_tts", _config(**overrides), True)


def _conn(sentence_id="sid-1"):
    return SimpleNamespace(
        sample_rate=SAMPLE_RATE,
        audio_format="opus",
        stop_event=threading.Event(),
        client_abort=False,
        sentence_id=sentence_id,
        headers={},
        max_output_size=0,
    )


def _attach(provider, conn):
    provider.conn = conn
    provider.sample_rate = conn.sample_rate
    provider.opus_encoder = OpusEncoderUtils(SAMPLE_RATE, 1, 60)
    provider.current_sentence_id = conn.sentence_id


def _drain(q):
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            return items


# --------------------------------------------------------------------------- construction


def test_create_instance_is_lazy_about_credentials():
    provider = _provider()
    assert isinstance(provider, TTSProviderBase)
    assert FakeClient.instances == []
    assert provider.voice == "en-US-Chirp3-HD-Charon"
    assert provider.audio_file_type == "wav"


def test_voice_mapping_and_defaults():
    p = _provider(speaker="Kore")
    assert p._select_voice("你好，世界") == ("cmn-CN-Chirp3-HD-Kore", "cmn-CN")
    assert p._select_voice("Hello there") == ("en-US-Chirp3-HD-Kore", "en-US")
    assert p._select_voice("123 456") == ("en-US-Chirp3-HD-Kore", "en-US")
    assert p._select_voice("Hi，你好") == ("cmn-CN-Chirp3-HD-Kore", "cmn-CN")
    p_zh = _provider(default_language="zh")
    assert p_zh._select_voice("123") == ("cmn-CN-Chirp3-HD-Charon", "cmn-CN")
    assert p_zh.voice == "cmn-CN-Chirp3-HD-Charon"
    p_custom = _provider(voice_en="en-US-Chirp3-HD-Puck", voice_zh="cmn-CN-Chirp3-HD-Leda")
    assert p_custom._select_voice("ok")[0] == "en-US-Chirp3-HD-Puck"
    assert p_custom._select_voice("好")[0] == "cmn-CN-Chirp3-HD-Leda"


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({}, 1.0),
        ({"speaking_rate": 1.3}, 1.3),
        ({"ttsRate": 100}, 2.0),
        ({"ttsRate": -100}, 0.25),
        ({"ttsRate": 0}, 1.0),
        ({"ttsRate": 50, "speaking_rate": 1.7}, 1.5),
    ],
)
def test_speaking_rate(overrides, expected):
    assert _provider(**overrides).speaking_rate == pytest.approx(expected)


def test_missing_credentials_file_is_reported(tmp_path):
    p = _provider(credentials_file=str(tmp_path / "nope.json"))
    with pytest.raises(FileNotFoundError, match="credentials_file not found"):
        p.client


def test_credentials_file_is_used(tmp_path):
    sa = tmp_path / "sa.json"
    sa.write_text("{}")
    p = _provider(credentials_file=str(sa))
    assert p.client.credentials_path == str(sa)
    assert p.client is p.client


def test_split_request_respects_byte_limit():
    long_zh = "这是一个很长的句子，" * 300
    chunks = _provider()._split_request(long_zh)
    assert len(chunks) > 1
    assert "".join(chunks) == long_zh
    assert all(len(c.encode("utf-8")) <= 4500 for c in chunks)
    assert _provider()._split_request("short") == ["short"]
    assert _provider()._split_request("") == []


# --------------------------------------------------------------------------- sync path


def test_text_to_speak_returns_wav_bytes_and_writes_file(tmp_path):
    p = _provider()
    p.conn = _conn()
    data = asyncio.run(p.text_to_speak("Hello", None))
    assert data[:4] == b"RIFF"
    out = tmp_path / "out.wav"
    assert asyncio.run(p.text_to_speak("你好", str(out))) is None
    assert out.read_bytes()[:4] == b"RIFF"
    texts = [c[0] for c in p.client.sync_calls]
    voices = [c[1] for c in p.client.sync_calls]
    assert texts == ["Hello", "你好"]
    assert voices == ["en-US-Chirp3-HD-Charon", "cmn-CN-Chirp3-HD-Charon"]
    assert p.client.sync_calls[0][3].audio_encoding == texttospeech.AudioEncoding.LINEAR16
    assert p.client.sync_calls[0][3].sample_rate_hertz == SAMPLE_RATE


@pytest.mark.skipif(not HAVE_OPUS, reason="libopus not available")
def test_to_tts_returns_opus_frames():
    p = _provider()
    p.conn = _conn()
    frames = p.to_tts("你好")
    assert isinstance(frames, list) and frames
    assert all(isinstance(f, bytes) and f for f in frames)


# --------------------------------------------------------------------------- streaming path


def _run_turn(provider, conn, texts, sid=None):
    sid = sid or conn.sentence_id
    put = provider.tts_text_queue.put
    put(TTSMessageDTO(sid, SentenceType.FIRST, ContentType.ACTION))
    for t in texts:
        put(TTSMessageDTO(sid, SentenceType.MIDDLE, ContentType.TEXT, content_detail=t))
    put(TTSMessageDTO(sid, SentenceType.LAST, ContentType.ACTION))


def _collect_until_last(provider, conn, timeout=5.0):
    items = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            item = provider.tts_audio_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        items.append(item)
        if item[0] == SentenceType.LAST:
            break
    return items


@pytest.fixture
def running(fake_client):
    provider = _provider()
    conn = _conn()
    _attach(provider, conn)
    thread = threading.Thread(target=provider.tts_text_priority_thread, daemon=True)
    thread.start()
    yield provider, conn
    conn.stop_event.set()
    thread.join(timeout=3)


pytestmark_opus = pytest.mark.skipif(not HAVE_OPUS, reason="libopus not available")


@pytestmark_opus
def test_stream_turn_emits_first_middle_last_with_sentence_id(running):
    provider, conn = running
    _run_turn(provider, conn, ["你好，", "今天天气不错。", "The light ", "is on."])
    items = _collect_until_last(provider, conn)
    kinds = [i[0] for i in items]
    assert kinds[0] == SentenceType.FIRST
    assert kinds[-1] == SentenceType.LAST
    assert all(len(i) == 4 and i[3] == conn.sentence_id for i in items)
    firsts = [i for i in items if i[0] == SentenceType.FIRST]
    assert [f[2] for f in firsts] == ["你好", "今天天气不错", "The light is on"]
    middles = [i for i in items if i[0] == SentenceType.MIDDLE]
    assert middles and all(isinstance(m[1], bytes) and m[1] for m in middles)
    pcm_total = sum(len(c) for c in FakeClient.chunks) - 1  # odd byte dropped
    per_segment = math.ceil(pcm_total / FRAME_BYTES)
    assert len(middles) == per_segment * len(firsts)
    calls = provider.client.stream_calls
    assert [c["language"] for c in calls] == ["cmn-CN", "cmn-CN", "en-US"]
    assert [c["voice"] for c in calls] == [
        "cmn-CN-Chirp3-HD-Charon",
        "cmn-CN-Chirp3-HD-Charon",
        "en-US-Chirp3-HD-Charon",
    ]
    assert all(c["encoding"] == texttospeech.AudioEncoding.PCM for c in calls)
    assert all(c["sample_rate"] == SAMPLE_RATE for c in calls)
    assert all(c["timeout"] == provider.tts_timeout for c in calls)
    assert provider.last_first_audio_latency is not None


@pytestmark_opus
def test_stream_call_error_still_emits_last(running):
    provider, conn = running
    FakeClient.call_error = gexc.InvalidArgument("unsupported sample rate")
    _run_turn(provider, conn, ["Hello world."])
    items = _collect_until_last(provider, conn)
    kinds = [i[0] for i in items]
    assert kinds == [SentenceType.FIRST, SentenceType.LAST]
    assert items[-1][3] == conn.sentence_id


@pytestmark_opus
def test_stale_sentence_cancels_and_stays_silent(running):
    provider, conn = running
    original_next = FakeCall.__next__

    def gated_next(self):
        if self._i == 1:
            conn.sentence_id = "sid-2"  # a new turn started mid-stream
        return original_next(self)

    FakeCall.__next__ = gated_next
    try:
        _run_turn(provider, conn, ["Hello world."])
        time.sleep(0.5)
        items = _drain(provider.tts_audio_queue)
    finally:
        FakeCall.__next__ = original_next
    kinds = [i[0] for i in items]
    assert SentenceType.LAST not in kinds
    assert provider.client.calls[0].cancelled is True
    assert len([k for k in kinds if k == SentenceType.MIDDLE]) <= math.ceil(len(FakeClient.chunks[0]) / FRAME_BYTES)


@pytestmark_opus
def test_messages_for_other_sentence_are_ignored(running):
    provider, conn = running
    _run_turn(provider, conn, ["Ignored."], sid="old-sid")
    time.sleep(0.3)
    assert _drain(provider.tts_audio_queue) == []
    assert FakeClient.instances == []


def test_close_releases_transport():
    p = _provider()
    p.conn = _conn()
    asyncio.run(p.text_to_speak("hi", None))
    client = p.client
    asyncio.run(p.close())
    assert client.transport.closed is True
    assert p._client is None


# --------------------------------------------------------------------------- live smoke


@pytest.mark.skipif(not LIVE, reason="RUN_LIVE_TESTS=1 required")
@pytest.mark.skipif(not HAVE_OPUS, reason="libopus not available")
def test_live_smoke_first_audio_latency(tmp_path, monkeypatch):
    monkeypatch.setattr(texttospeech, "TextToSpeechClient", REAL_CLIENT)
    assert os.path.exists(CREDENTIALS), f"credentials not found at {CREDENTIALS}"
    provider = _provider(credentials_file=CREDENTIALS)
    conn = _conn()
    _attach(provider, conn)
    out_dir = os.environ.get("WEIXUE_SMOKE_OUT", str(tmp_path))
    os.makedirs(out_dir, exist_ok=True)

    results = []
    for lang, text in [("zh", "你好，我是你的家庭助手。"), ("en", "Hello, the living room light is on.")]:
        pcm = bytearray()

        class Buf(bytearray):
            def extend(self, data):
                pcm.extend(data)
                super().extend(data)

        provider.pcm_buffer = Buf()
        ok = provider._stream_segment(text, is_last=True)
        assert ok is True
        items = _drain(provider.tts_audio_queue)
        kinds = [i[0] for i in items]
        assert kinds[0] == SentenceType.FIRST and kinds[-1] == SentenceType.LAST
        frames = [i[1] for i in items if i[0] == SentenceType.MIDDLE]
        assert frames, f"no audio for {lang}"
        latency = provider.last_first_audio_latency
        assert latency is not None

        wav = asyncio.run(provider.text_to_speak(text, None))
        with wave.open(io.BytesIO(wav)) as w:
            sync_rate = w.getframerate()
            sync_bytes = w.getnframes() * w.getsampwidth()
        with open(os.path.join(out_dir, f"tts-smoke-{lang}.wav"), "wb") as f:
            f.write(wav)
        stream_seconds = len(pcm) / (SAMPLE_RATE * 2)
        results.append(
            dict(lang=lang, first_audio_s=round(latency, 3), frames=len(frames),
                 stream_pcm_bytes=len(pcm), stream_seconds=round(stream_seconds, 2),
                 sync_rate=sync_rate, sync_bytes=sync_bytes,
                 ratio=round(len(pcm) / sync_bytes, 3))
        )
        assert sync_rate == SAMPLE_RATE
        assert 0.8 < stream_seconds < 6.0, "streamed PCM duration implausible at 24 kHz"
        assert 0.5 < len(pcm) / sync_bytes < 1.6, "streaming PCM length deviates from sync LINEAR16"

    print("\nLIVE SMOKE RESULTS:")
    for r in results:
        print(" ", r)
    assert max(r["first_audio_s"] for r in results) < 1.5
    asyncio.run(provider.close())
