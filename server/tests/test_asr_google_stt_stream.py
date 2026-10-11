import asyncio
import os
import threading
import time
import wave
from types import SimpleNamespace

import pytest
from google.api_core import exceptions as gexc
from google.cloud.speech_v2.types import cloud_speech

from core.providers.asr import google_stt, google_stt_stream
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType
from tests.conftest import ASSETS_DIR, CREDENTIALS, LIVE, OVERLAY_DIR
from tests.test_asr_google_stt import ListeningConn, _drain, _response, make_conn

FRAME = b"\x01\x00" * 960  # 60 ms at 16 kHz
SILENCE = b"\x00\x00" * 960
Streaming = cloud_speech.StreamingRecognizeResponse


def _result(text, lang="en-US", confidence=0.98, final=True):
    return Streaming(
        results=[
            cloud_speech.StreamingRecognitionResult(
                alternatives=[cloud_speech.SpeechRecognitionAlternative(transcript=text, confidence=confidence)],
                is_final=final,
                language_code=lang,
            )
        ]
    )


class FakeCall:
    def __init__(self, behaviour, requests):
        self.config = None
        self.audio = []
        self.half_closed_at = None
        self.cancelled = threading.Event()
        self._responses = behaviour(self, iter(requests))

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._responses)

    def cancel(self):
        self.cancelled.set()


class FakeClient:
    def __init__(self, *behaviours, recognize=()):
        self.behaviours = list(behaviours)
        self.calls = []
        self.timeouts = []
        self.recognize_outcomes = list(recognize)
        self.recognize_calls = []
        self.transport = SimpleNamespace(streaming_recognize=SimpleNamespace(), close=lambda: None)

    def streaming_recognize(self, requests, timeout=None):
        call = FakeCall(self.behaviours.pop(0), requests)
        self.calls.append(call)
        self.timeouts.append(timeout)
        return call

    def recognize(self, request, timeout=None, retry="unset"):
        self.recognize_calls.append(request)
        outcome = self.recognize_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def final_after_half_close(text, lang="en-US", confidence=0.98, interims=1, delay=0.0):
    def run(call, requests):
        call.config = next(requests)
        for request in requests:
            call.audio.append(request.audio)
            if len(call.audio) == 2 and interims:
                yield _result(text[:4], lang, 0.0, final=False)
        call.half_closed_at = time.monotonic()
        if delay and call.cancelled.wait(delay):
            raise gexc.Cancelled("cancelled")
        yield _result(text, lang, confidence)

    return run


def ends_after(frames, text, lang="en-US", confidence=0.98):
    """The service's own end of utterance after `frames` audio requests."""

    def run(call, requests):
        call.config = next(requests)
        for request in requests:
            call.audio.append(request.audio)
            if len(call.audio) >= frames:
                break
        yield Streaming(speech_event_type=Streaming.SpeechEventType.END_OF_SINGLE_UTTERANCE)
        yield _result(text, lang, confidence)

    return run


def fails_after(frames, error):
    def run(call, requests):
        call.config = next(requests)
        for request in requests:
            call.audio.append(request.audio)
            if len(call.audio) >= frames:
                raise error
        yield from ()

    return run


def hangs():
    def run(call, requests):
        call.config = next(requests)
        for request in requests:
            call.audio.append(request.audio)
        call.half_closed_at = time.monotonic()
        call.cancelled.wait(10)
        raise gexc.Cancelled("cancelled")
        yield

    return run


def make_config(tmp_path, **overrides):
    config = {
        "type": "google_stt_stream",
        "credentials_file": str(tmp_path / "missing-sa.json"),
        "use_adc": False,
        "project_id": "proj-test",
        "location": "us",
        "model": "latest_short",
        "language_codes": ["en-US", "cmn-Hans-CN"],
        "enable_automatic_punctuation": True,
        "interim_results": True,
        "final_timeout": 2,
        "fallback_model": "chirp_3",
        "fallback_timeout": 7,
        "output_dir": str(tmp_path / "out"),
    }
    config.update(overrides)
    return config


def make_provider(tmp_path, monkeypatch, client=None, **overrides):
    provider = google_stt_stream.ASRProvider(make_config(tmp_path, **overrides), True)
    if client is not None:
        monkeypatch.setattr(google_stt.ASRProvider, "_get_client", lambda self: client)
    return provider


def make_listening_conn(provider, mode="realtime"):
    return ListeningConn(
        **vars(make_conn()), client_listen_mode=mode, asr_audio=[], client_have_voice=False,
        client_voice_stop=False, asr=provider, resets=0, voiceprint_provider=None,
    )


@pytest.fixture
def started(monkeypatch):
    texts = []

    async def start_to_chat(_conn, text):
        texts.append(text)

    monkeypatch.setattr("core.providers.asr.base.startToChat", start_to_chat)
    return texts


def wait_for(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, "condition not reached"
        time.sleep(0.005)


def call_of(client, index=0):
    wait_for(lambda: len(client.calls) > index)
    return client.calls[index]


async def speak(provider, conn, voiced=20, idle=0):
    """`idle` silent frames, then `voiced` frames the VAD hears as voice."""
    for _ in range(idle):
        await provider.receive_audio(conn, SILENCE, False)
    conn.client_have_voice = True
    for _ in range(voiced):
        await provider.receive_audio(conn, FRAME, True)


async def stop(provider, conn):
    conn.client_voice_stop = True
    await provider.receive_audio(conn, SILENCE, False)


def stt_lines(log_records):
    return [msg for level, msg in log_records if level == "info" and msg.startswith("stt_final_latency_ms=")]


def test_init_and_factory(tmp_path, monkeypatch):
    provider = make_provider(tmp_path, monkeypatch)
    assert isinstance(provider, google_stt.ASRProvider)
    assert provider.interface_type == InterfaceType.STREAM
    assert (provider.model, provider.fallback_model) == ("latest_short", "chirp_3")
    assert provider.timeout == 7 and provider.final_timeout == 2
    assert provider.zh_fallback_confidence == google_stt_stream.DEFAULT_ZH_FALLBACK_CONFIDENCE
    assert provider._client is None

    from core.utils.asr import create_instance

    monkeypatch.chdir(OVERLAY_DIR)
    assert isinstance(create_instance("google_stt_stream", make_config(tmp_path), True), google_stt_stream.ASRProvider)


def test_is_chinese():
    assert google_stt_stream.is_chinese("打开客厅的灯。", "cmn-hans-cn")
    assert google_stt_stream.is_chinese("打开客厅的灯。", None)
    assert google_stt_stream.is_chinese("ok", "cmn-Hans-CN")
    assert not google_stt_stream.is_chinese("Turn on the lights.", "en-US")
    assert not google_stt_stream.is_chinese("", None)


async def test_streams_from_first_voiced_frame_and_half_closes_at_voice_stop(tmp_path, monkeypatch, started, log_records):
    client = FakeClient(final_after_half_close("Turn on the living room lights."))
    provider = make_provider(tmp_path, monkeypatch, client)
    conn = make_listening_conn(provider)

    await speak(provider, conn, voiced=0, idle=15)
    assert client.calls == []
    await speak(provider, conn, voiced=20)
    call = call_of(client)
    wait_for(lambda: len(call.audio) == 10 + 20)
    assert call.half_closed_at is None

    stopped = time.monotonic()
    await stop(provider, conn)

    assert started == ["Turn on the living room lights."]
    assert call.half_closed_at >= stopped
    assert call.audio == [SILENCE] * 10 + [FRAME] * 20 + [SILENCE]
    assert conn.resets == 1 and conn.asr_audio == []
    assert client.timeouts == [google_stt_stream.STREAM_DEADLINE_S]
    assert getattr(client.transport.streaming_recognize, "_prefetch_first_result_") is False
    (line,) = stt_lines(log_records)
    assert "model=latest_short" in line and "fallback=-" in line and "interims=1" in line
    assert "language=en-US" in line and "confidence=0.980" in line
    assert client.recognize_calls == []


async def test_stream_config_request(tmp_path, monkeypatch, started):
    client = FakeClient(final_after_half_close("Hello there."))
    provider = make_provider(tmp_path, monkeypatch, client)
    conn = make_listening_conn(provider)
    await speak(provider, conn)
    await stop(provider, conn)

    request = client.calls[0].config
    config = request.streaming_config.config
    assert request.recognizer == "projects/proj-test/locations/us/recognizers/_"
    assert config.model == "latest_short"
    assert list(config.language_codes) == ["en-US", "cmn-Hans-CN"]
    assert config.explicit_decoding_config.encoding == cloud_speech.ExplicitDecodingConfig.AudioEncoding.LINEAR16
    assert config.explicit_decoding_config.sample_rate_hertz == 16000
    assert config.features.enable_automatic_punctuation is True
    assert request.streaming_config.streaming_features.interim_results is True


async def test_interim_results_are_configurable(tmp_path, monkeypatch, started):
    client = FakeClient(final_after_half_close("Hi there.", interims=0))
    provider = make_provider(tmp_path, monkeypatch, client, interim_results=False)
    conn = make_listening_conn(provider)
    await speak(provider, conn)
    await stop(provider, conn)
    assert client.calls[0].config.streaming_config.streaming_features.interim_results is False


async def test_latency_is_measured_from_voice_stop(tmp_path, monkeypatch, started, log_records):
    client = FakeClient(final_after_half_close("What time is it?", delay=0.15))
    provider = make_provider(tmp_path, monkeypatch, client)
    conn = make_listening_conn(provider)
    await speak(provider, conn)
    await stop(provider, conn)
    (line,) = stt_lines(log_records)
    latency = int(line.split()[0].split("=")[1])
    assert 150 <= latency < 1000


async def test_chinese_final_below_threshold_is_re_recognized_with_chirp_3(tmp_path, monkeypatch, started, log_records):
    client = FakeClient(
        final_after_half_close("打开客厅的等。", "cmn-hans-cn", 0.91),
        recognize=[_response(("打 开 客 厅 的 灯 。", "cmn-Hans-CN"))],
    )
    provider = make_provider(tmp_path, monkeypatch, client, zh_fallback_confidence=0.95)
    conn = make_listening_conn(provider)
    await speak(provider, conn)
    await stop(provider, conn)

    assert started == ["打开客厅的灯。"]
    (request,) = client.recognize_calls
    assert request.config.model == "chirp_3"
    assert request.content == FRAME * 20 + SILENCE
    (line,) = stt_lines(log_records)
    assert "model=chirp_3" in line and "fallback=zh_low_confidence" in line and "language=cmn-Hans-CN" in line


async def test_chinese_final_at_default_threshold_keeps_latest_short(tmp_path, monkeypatch, started, log_records):
    client = FakeClient(final_after_half_close("打开客厅的灯。", "cmn-hans-cn", 0.91))
    provider = make_provider(tmp_path, monkeypatch, client)
    conn = make_listening_conn(provider)
    await speak(provider, conn)
    await stop(provider, conn)
    assert started == ["打开客厅的灯。"] and client.recognize_calls == []
    assert "model=latest_short" in stt_lines(log_records)[0]


async def test_english_final_below_threshold_is_not_re_recognized(tmp_path, monkeypatch, started):
    client = FakeClient(final_after_half_close("Turn on the lights.", "en-US", 0.3))
    provider = make_provider(tmp_path, monkeypatch, client, zh_fallback_confidence=0.95)
    conn = make_listening_conn(provider)
    await speak(provider, conn)
    await stop(provider, conn)
    assert started == ["Turn on the lights."] and client.recognize_calls == []


@pytest.mark.parametrize("outcome", [_response(), gexc.ServiceUnavailable("down")])
async def test_failed_or_empty_chinese_fallback_keeps_the_streamed_text(tmp_path, monkeypatch, started, outcome):
    client = FakeClient(final_after_half_close("打开客厅的灯。", "cmn-hans-cn", 0.91), recognize=[outcome])
    provider = make_provider(tmp_path, monkeypatch, client, zh_fallback_confidence=0.95)
    conn = make_listening_conn(provider)
    await speak(provider, conn)
    await stop(provider, conn)
    assert started == ["打开客厅的灯。"] and len(client.recognize_calls) == 1


async def test_stream_error_falls_back_to_batch(tmp_path, monkeypatch, started, log_records):
    client = FakeClient(
        fails_after(3, gexc.ServiceUnavailable("stream broke")),
        recognize=[_response(("Lock the front door.", "en-US"))],
    )
    provider = make_provider(tmp_path, monkeypatch, client)
    conn = make_listening_conn(provider)
    await speak(provider, conn, voiced=5)
    wait_for(lambda: provider._streams and provider._streams[0].finished)
    await speak(provider, conn, voiced=15)
    await stop(provider, conn)

    assert len(client.calls) == 1
    assert started == ["Lock the front door."]
    assert client.recognize_calls[0].content == FRAME * 20 + SILENCE
    (line,) = stt_lines(log_records)
    assert "model=chirp_3" in line and "fallback=stream_error" in line
    assert any(level == "error" and "stream broke" in msg for level, msg in log_records)


def spoken_text(conn):
    items = _drain(conn.tts.tts_text_queue)
    return "".join(i.content_detail for i in items if i.content_type.name == "TEXT")


async def test_stream_error_without_fallback_rejects_the_utterance(tmp_path, monkeypatch, started, log_records):
    client = FakeClient(fails_after(1, gexc.ServiceUnavailable("stream broke")))
    provider = make_provider(tmp_path, monkeypatch, client, fallback_model="")
    conn = make_listening_conn(provider)
    await speak(provider, conn)
    await stop(provider, conn)
    assert started == [] and client.recognize_calls == []
    line = stt_lines(log_records)[0]
    assert "fallback=stream_error" in line and "rejected=fallback_failed" in line
    assert spoken_text(conn) == google_stt_stream.DEFAULT_REPEAT_REPLY


def finals_then_fails(text, error):
    """One final for the first words, then the stream breaks before the rest of the utterance."""

    def run(call, requests):
        call.config = next(requests)
        for request in requests:
            call.audio.append(request.audio)
            if len(call.audio) == 3:
                yield _result(text)
            if len(call.audio) >= 5:
                raise error

    return run


def finals_then_hangs(text):
    def run(call, requests):
        call.config = next(requests)
        for request in requests:
            call.audio.append(request.audio)
            if len(call.audio) == 3:
                yield _result(text)
        call.cancelled.wait(10)
        raise gexc.Cancelled("cancelled")

    return run


@pytest.mark.parametrize("behaviour, problem", [
    (finals_then_fails("Turn on", gexc.ServiceUnavailable("stream broke")), "stream_error"),
    (finals_then_hangs("Turn on"), "timeout"),
])
async def test_partial_finals_with_a_failed_fallback_never_become_a_turn(
        tmp_path, monkeypatch, started, log_records, behaviour, problem):
    client = FakeClient(behaviour, recognize=[gexc.ServiceUnavailable("batch down")])
    provider = make_provider(tmp_path, monkeypatch, client, final_timeout=0.2)
    conn = make_listening_conn(provider)
    await speak(provider, conn, voiced=4)
    wait_for(lambda: call_of(client).audio and len(provider._streams[0].finals) == 1)
    await speak(provider, conn, voiced=16)
    await stop(provider, conn)

    assert started == [] and len(client.recognize_calls) == 1
    line = stt_lines(log_records)[0]
    assert f"fallback={problem}" in line and "model=-" in line and "rejected=fallback_failed" in line
    assert spoken_text(conn) == google_stt_stream.DEFAULT_REPEAT_REPLY


async def test_partial_finals_take_the_complete_fallback_transcript(tmp_path, monkeypatch, started, log_records):
    client = FakeClient(
        finals_then_fails("Turn on", gexc.ServiceUnavailable("stream broke")),
        recognize=[_response(("Turn on the porch light, no, the hall light.", "en-US"))],
    )
    provider = make_provider(tmp_path, monkeypatch, client)
    conn = make_listening_conn(provider)
    await speak(provider, conn, voiced=20)
    await stop(provider, conn)
    assert started == ["Turn on the porch light, no, the hall light."]
    assert "rejected" not in stt_lines(log_records)[0]
    assert spoken_text(conn) == ""


async def test_repeat_reply_is_configurable(tmp_path, monkeypatch, started):
    client = FakeClient(fails_after(1, gexc.ServiceUnavailable("stream broke")), recognize=[gexc.DeadlineExceeded("slow")])
    provider = make_provider(tmp_path, monkeypatch, client, repeat_reply="Once more?")
    conn = make_listening_conn(provider)
    await speak(provider, conn)
    await stop(provider, conn)
    assert started == [] and spoken_text(conn) == "Once more?"


async def test_final_timeout_cancels_the_stream_and_falls_back(tmp_path, monkeypatch, started, log_records):
    client = FakeClient(hangs(), recognize=[_response(("Will it rain tomorrow?", "en-US"))])
    provider = make_provider(tmp_path, monkeypatch, client, final_timeout=0.2)
    conn = make_listening_conn(provider)
    await speak(provider, conn)
    began = time.monotonic()
    await stop(provider, conn)

    assert started == ["Will it rain tomorrow?"]
    assert 0.2 <= time.monotonic() - began < 2
    wait_for(call_of(client).cancelled.is_set)
    assert "fallback=timeout" in stt_lines(log_records)[0]


async def test_service_end_of_utterance_opens_a_follow_on_stream(tmp_path, monkeypatch, started, log_records):
    client = FakeClient(
        ends_after(10, "Turn on the kitchen light."),
        final_after_half_close("And close the blinds."),
    )
    provider = make_provider(tmp_path, monkeypatch, client)
    conn = make_listening_conn(provider)
    await speak(provider, conn, voiced=10)
    wait_for(lambda: provider._streams[0].finished)
    for _ in range(5):
        await provider.receive_audio(conn, SILENCE, False)
    assert len(client.calls) == 1
    await speak(provider, conn, voiced=8)
    await stop(provider, conn)

    assert started == ["Turn on the kitchen light. And close the blinds."]
    first, second = client.calls
    assert len(first.audio) == 10
    assert second.audio == [FRAME] * google_stt_stream.RESEND_FRAMES + [SILENCE] * 5 + [FRAME] * 8 + [SILENCE]
    assert "streams=2" in stt_lines(log_records)[0]


async def test_short_utterance_is_not_recognized(tmp_path, monkeypatch, started, log_records):
    client = FakeClient(final_after_half_close("Hm.", delay=5))
    provider = make_provider(tmp_path, monkeypatch, client)
    conn = make_listening_conn(provider)
    await speak(provider, conn, voiced=10)
    await stop(provider, conn)
    assert started == [] and stt_lines(log_records) == []
    assert conn.resets == 1
    wait_for(call_of(client).cancelled.is_set)


async def test_audio_reset_mid_utterance_drops_the_stream(tmp_path, monkeypatch, started):
    client = FakeClient(hangs(), final_after_half_close("Second try."))
    provider = make_provider(tmp_path, monkeypatch, client)
    conn = make_listening_conn(provider)
    await speak(provider, conn, voiced=8)
    conn.reset_audio_states()
    await speak(provider, conn, voiced=20)
    wait_for(call_of(client).cancelled.is_set)
    await stop(provider, conn)
    assert started == ["Second try."]
    assert len(client.calls) == 2


async def test_overlong_utterance_drops_the_stream_and_speaks_the_reply(tmp_path, monkeypatch, started):
    client = FakeClient(hangs())
    provider = make_provider(tmp_path, monkeypatch, client)
    conn = make_listening_conn(provider)
    await speak(provider, conn, voiced=62 * 1000 // 60)
    wait_for(call_of(client).cancelled.is_set)
    await stop(provider, conn)

    assert started == [] and len(client.calls) == 1 and client.recognize_calls == []
    items = _drain(conn.tts.tts_text_queue)
    spoken = "".join(i.content_detail for i in items if i.content_type.name == "TEXT")
    assert spoken == google_stt.DEFAULT_TOO_LONG_REPLY


async def test_manual_mode_finishes_on_listen_stop(tmp_path, monkeypatch, started):
    client = FakeClient(final_after_half_close("Manual words."))
    provider = make_provider(tmp_path, monkeypatch, client)
    conn = make_listening_conn(provider, mode="manual")
    for _ in range(20):
        await provider.receive_audio(conn, FRAME, True)
    conn.client_voice_stop = True
    await provider.receive_audio(conn, FRAME, True)
    assert started == []
    await provider._send_stop_request()
    assert started == ["Manual words."]
    assert len(client.calls[0].audio) == 21


async def test_open_failure_falls_back_to_batch(tmp_path, monkeypatch, started, log_records):
    client = FakeClient(recognize=[_response(("Fallback words.", "en-US"))])
    calls = {"n": 0}

    def get_client(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("no project")
        return client

    monkeypatch.setattr(google_stt.ASRProvider, "_get_client", get_client)
    provider = google_stt_stream.ASRProvider(make_config(tmp_path), True)
    conn = make_listening_conn(provider)
    await speak(provider, conn)
    await stop(provider, conn)
    assert started == ["Fallback words."]
    assert "fallback=stream_error" in stt_lines(log_records)[0]


async def test_close_cancels_the_open_stream(tmp_path, monkeypatch):
    client = FakeClient(hangs())
    provider = make_provider(tmp_path, monkeypatch, client)
    conn = make_listening_conn(provider)
    await speak(provider, conn, voiced=5)
    await provider.close()
    wait_for(call_of(client).cancelled.is_set)
    assert provider._streams == []


def _pcm_from_wav(path):
    with wave.open(str(path)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        return wav.readframes(wav.getnframes())


@pytest.mark.skipif(not LIVE, reason="RUN_LIVE_TESTS=1 not set")
@pytest.mark.skipif(not CREDENTIALS, reason="GOOGLE_SA_FILE not set")
@pytest.mark.parametrize(
    "clip, expected",
    [
        ("en-US_turn_on_the_living_room_lights.wav", ("living room", "light")),
        ("cmn-Hans-CN_turn_on_the_living_room_lights.wav", ("客厅", "灯")),
    ],
)
async def test_live_streaming_english_and_mandarin(tmp_path, monkeypatch, started, log_records, clip, expected):
    provider = google_stt_stream.ASRProvider(make_config(tmp_path, credentials_file=CREDENTIALS, project_id=""), True)
    conn = make_listening_conn(provider)
    pcm = _pcm_from_wav(ASSETS_DIR / clip) + b"\x00" * len(FRAME) * 4
    conn.client_have_voice = True
    try:
        for i in range(0, len(pcm), len(FRAME)):
            await provider.receive_audio(conn, pcm[i : i + len(FRAME)].ljust(len(FRAME), b"\x00"), True)
            await asyncio.sleep(0.06)
        await stop(provider, conn)
    finally:
        await provider.close()
    errors = [msg for level, msg in log_records if level == "error"]
    assert not errors, errors
    print(f"\nlive {clip}: {started!r} | {stt_lines(log_records)}")
    assert len(started) == 1 and all(word in started[0].lower() for word in expected)
    assert "model=latest_short" in stt_lines(log_records)[0]
