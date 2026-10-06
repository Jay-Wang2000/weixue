import json
import os
import queue
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core import exceptions as gexc
from google.cloud.speech_v2.types import cloud_speech

from core.providers.asr import google_stt
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import ContentType, SentenceType
from tests.conftest import ASSETS_DIR, CREDENTIALS, LIVE, OVERLAY_DIR

PCM_ONE_SECOND = b"\x00\x01" * 16000


def _response(*results):
    return cloud_speech.RecognizeResponse(
        results=[
            cloud_speech.SpeechRecognitionResult(
                alternatives=[cloud_speech.SpeechRecognitionAlternative(transcript=text)],
                language_code=lang,
            )
            for text, lang in results
        ]
    )


class FakeTransport:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.transport = FakeTransport()

    def recognize(self, request, timeout=None, retry="unset"):
        self.calls.append({"request": request, "timeout": timeout, "retry": retry})
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_config(tmp_path, **overrides):
    config = {
        "type": "google_stt",
        "credentials_file": str(tmp_path / "missing-sa.json"),
        "use_adc": False,
        "project_id": "proj-test",
        "location": "us",
        "model": "chirp_3",
        "language_codes": ["en-US", "cmn-Hans-CN"],
        "enable_automatic_punctuation": True,
        "timeout": 7,
        "output_dir": str(tmp_path / "out"),
    }
    config.update(overrides)
    return config


def make_provider(tmp_path, monkeypatch=None, responses=None, **overrides):
    provider = google_stt.ASRProvider(make_config(tmp_path, **overrides), True)
    client = None
    if responses is not None:
        client = FakeClient(responses)
        monkeypatch.setattr(provider, "_get_client", lambda: client)
    return provider, client


def artifacts_for(pcm):
    return ASRProviderBase.AudioArtifacts(
        pcm_frames=[pcm], pcm_bytes=pcm, file_path=None, temp_path=None
    )


def test_init_performs_no_io(tmp_path):
    provider, _ = make_provider(tmp_path)
    assert isinstance(provider, ASRProviderBase)
    assert provider.interface_type == InterfaceType.NON_STREAM
    assert provider.requires_file() is False
    assert provider._client is None
    assert Path(provider.output_dir).is_dir()
    assert provider.delete_audio_file is True


def test_factory_creates_provider_from_overlay(tmp_path, monkeypatch):
    from core.utils.asr import create_instance

    monkeypatch.chdir(OVERLAY_DIR)
    provider = create_instance("google_stt", make_config(tmp_path), True)
    assert isinstance(provider, google_stt.ASRProvider)


async def test_request_shape(tmp_path, monkeypatch):
    provider, client = make_provider(
        tmp_path, monkeypatch, responses=[_response(("Hello there.", "en-US"))]
    )
    text, extra = await provider.speech_to_text([], "sid", artifacts_for(PCM_ONE_SECOND))

    assert (text, extra) == ("Hello there.", None)
    call = client.calls[0]
    request = call["request"]
    decoding = request.config.explicit_decoding_config
    assert request.recognizer == "projects/proj-test/locations/us/recognizers/_"
    assert decoding.encoding == cloud_speech.ExplicitDecodingConfig.AudioEncoding.LINEAR16
    assert decoding.sample_rate_hertz == 16000
    assert decoding.audio_channel_count == 1
    assert list(request.config.language_codes) == ["en-US", "cmn-Hans-CN"]
    assert request.config.model == "chirp_3"
    assert request.config.features.enable_automatic_punctuation is True
    assert request.content == PCM_ONE_SECOND
    assert call["timeout"] == 7.0
    assert call["retry"] is None


async def test_multi_result_join_and_language_log(tmp_path, monkeypatch, log_records):
    provider, _ = make_provider(
        tmp_path,
        monkeypatch,
        responses=[_response(("打开客厅的灯。", "cmn-Hans-CN"), ("谢谢。", "cmn-Hans-CN"))],
    )
    text, _ = await provider.speech_to_text([], "sid", artifacts_for(PCM_ONE_SECOND))
    assert text == "打开客厅的灯。谢谢。"
    assert isinstance(text, str)
    assert any(level == "info" and "cmn-Hans-CN" in msg for level, msg in log_records)


def test_extract_collapses_cjk_spacing():
    text, lang = google_stt.ASRProvider._extract(
        _response(("打 开 客 厅 的 灯 。", "cmn-Hans-CN"), ("turn on the lights", "en-US"))
    )
    assert text == "打开客厅的灯。 turn on the lights"
    assert lang == "en-US"


async def test_empty_response_returns_empty_string(tmp_path, monkeypatch):
    provider, _ = make_provider(tmp_path, monkeypatch, responses=[_response()])
    assert await provider.speech_to_text([], "sid", artifacts_for(PCM_ONE_SECOND)) == ("", None)


async def test_no_artifacts_short_circuits(tmp_path, monkeypatch):
    provider, client = make_provider(tmp_path, monkeypatch, responses=[])
    assert await provider.speech_to_text([], "sid", None) == ("", None)
    assert await provider.speech_to_text([], "sid", artifacts_for(b"")) == ("", None)
    assert client.calls == []


async def test_api_error_returns_empty(tmp_path, monkeypatch, log_records):
    provider, _ = make_provider(
        tmp_path, monkeypatch, responses=[gexc.PermissionDenied("api disabled")]
    )
    assert await provider.speech_to_text([], "sid", artifacts_for(PCM_ONE_SECOND)) == ("", None)
    assert any(level == "error" for level, _ in log_records)


async def test_invalid_argument_retries_with_auto(tmp_path, monkeypatch):
    provider, client = make_provider(
        tmp_path,
        monkeypatch,
        responses=[gexc.InvalidArgument("bad language"), _response(("ok", "en-US"))],
    )
    text, _ = await provider.speech_to_text([], "sid", artifacts_for(PCM_ONE_SECOND))
    assert text == "ok"
    assert len(client.calls) == 2
    assert list(client.calls[0]["request"].config.language_codes) == ["en-US", "cmn-Hans-CN"]
    assert list(client.calls[1]["request"].config.language_codes) == ["auto"]


async def test_invalid_argument_with_auto_does_not_loop(tmp_path, monkeypatch):
    provider, client = make_provider(
        tmp_path, monkeypatch, responses=[gexc.InvalidArgument("bad")], language_codes=["auto"]
    )
    assert await provider.speech_to_text([], "sid", artifacts_for(PCM_ONE_SECOND)) == ("", None)
    assert len(client.calls) == 1


OVERLONG_PCM = b"\x00" * (google_stt.MAX_SYNC_AUDIO_BYTES + 2)


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))


class FakeTTS:
    tts_one_sentence = TTSProviderBase.tts_one_sentence

    def __init__(self):
        self.tts_text_queue = queue.Queue()


def make_conn():
    return SimpleNamespace(
        session_id="sid",
        sentence_id="previous",
        client_abort=True,
        client_is_speaking=False,
        websocket=FakeWebSocket(),
        tts=FakeTTS(),
        config={},
    )


def _drain(q):
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


async def test_audio_over_sync_limit_is_rejected_not_trimmed(tmp_path, monkeypatch, log_records):
    provider, client = make_provider(tmp_path, monkeypatch, responses=[_response(("long", "en-US"))])
    assert await provider.speech_to_text([], "sid", artifacts_for(OVERLONG_PCM)) == ("", None)
    assert client.calls == []
    assert any(level == "warning" and "59s" in msg for level, msg in log_records)


async def test_audio_at_sync_limit_is_recognized(tmp_path, monkeypatch):
    provider, client = make_provider(tmp_path, monkeypatch, responses=[_response(("ok", "en-US"))])
    pcm = b"\x00" * google_stt.MAX_SYNC_AUDIO_BYTES
    assert await provider.speech_to_text([], "sid", artifacts_for(pcm)) == ("ok", None)
    assert client.calls[0]["request"].content == pcm


async def test_voice_stop_over_limit_speaks_too_long_reply(tmp_path, monkeypatch, log_records):
    provider, client = make_provider(tmp_path, monkeypatch, responses=[])
    delegated = []
    monkeypatch.setattr(
        ASRProviderBase, "handle_voice_stop", lambda self, conn, task: delegated.append(task)
    )
    conn = make_conn()
    await provider.handle_voice_stop(conn, [OVERLONG_PCM[:1000], OVERLONG_PCM[1000:]])

    assert delegated == [] and client.calls == []
    assert conn.websocket.sent == [{"type": "tts", "state": "start", "session_id": "sid"}]
    assert conn.client_is_speaking is True and conn.client_abort is False
    items = _drain(conn.tts.tts_text_queue)
    assert {item.sentence_id for item in items} == {conn.sentence_id} != {"previous"}
    assert items[0].sentence_type == SentenceType.FIRST
    assert items[-1].sentence_type == SentenceType.LAST
    spoken = "".join(i.content_detail for i in items if i.content_type == ContentType.TEXT)
    assert spoken == google_stt.DEFAULT_TOO_LONG_REPLY
    assert any(level == "warning" and "59s" in msg for level, msg in log_records)


async def test_voice_stop_too_long_reply_is_configurable(tmp_path, monkeypatch):
    provider, _ = make_provider(tmp_path, too_long_reply="太长了，请简短地再说一遍。")
    conn = make_conn()
    await provider.handle_voice_stop(conn, [OVERLONG_PCM])
    items = _drain(conn.tts.tts_text_queue)
    spoken = "".join(i.content_detail for i in items if i.content_type == ContentType.TEXT)
    assert spoken == "太长了，请简短地再说一遍。"


async def test_voice_stop_within_limit_delegates_to_base(tmp_path, monkeypatch):
    provider, _ = make_provider(tmp_path)
    delegated = []

    async def base_handle_voice_stop(self, conn, task):
        delegated.append(task)

    monkeypatch.setattr(ASRProviderBase, "handle_voice_stop", base_handle_voice_stop)
    conn = make_conn()
    await provider.handle_voice_stop(conn, [PCM_ONE_SECOND])
    assert delegated == [[PCM_ONE_SECOND]]
    assert conn.websocket.sent == [] and conn.tts.tts_text_queue.empty()


async def test_missing_credentials_file_fails_soft(tmp_path, log_records):
    provider, _ = make_provider(tmp_path)
    assert await provider.speech_to_text([], "sid", artifacts_for(PCM_ONE_SECOND)) == ("", None)
    assert provider._client is None
    assert any(level == "error" for level, _ in log_records)


def test_client_options_follow_location(tmp_path, monkeypatch):
    captured = {}

    class FakeSpeechClient:
        def __init__(self, credentials=None, client_options=None):
            captured["credentials"] = credentials
            captured["client_options"] = client_options

    monkeypatch.setattr(google_stt, "SpeechClient", FakeSpeechClient)

    provider, _ = make_provider(tmp_path, project_id="")
    monkeypatch.setattr(provider, "_load_credentials", lambda: ("creds", "proj-from-key"))
    provider._get_client()
    assert captured["credentials"] == "creds"
    assert captured["client_options"].api_endpoint == "us-speech.googleapis.com"
    assert provider._recognizer_path() == "projects/proj-from-key/locations/us/recognizers/_"

    provider, _ = make_provider(tmp_path, location="global")
    monkeypatch.setattr(provider, "_load_credentials", lambda: ("creds", None))
    provider._get_client()
    assert captured["client_options"].api_endpoint is None
    assert provider._recognizer_path() == "projects/proj-test/locations/global/recognizers/_"


def test_unknown_project_id_raises(tmp_path, monkeypatch):
    provider, _ = make_provider(tmp_path, project_id="")
    monkeypatch.setattr(provider, "_load_credentials", lambda: ("creds", None))
    with pytest.raises(ValueError):
        provider._get_client()


async def test_wrapper_path_writes_no_file(tmp_path, monkeypatch):
    provider, client = make_provider(tmp_path, monkeypatch, responses=[_response(("hi", "en-US"))])
    text, file_path = await provider.speech_to_text_wrapper([PCM_ONE_SECOND[:1920]], "sid")
    assert (text, file_path) == ("hi", None)
    assert list(Path(provider.output_dir).iterdir()) == []
    assert len(client.calls[0]["request"].content) == 1920


async def test_close_is_safe_and_closes_transport(tmp_path, monkeypatch):
    provider, _ = make_provider(tmp_path)
    await provider.close()

    provider, client = make_provider(tmp_path, monkeypatch, responses=[_response()])
    await provider.speech_to_text([], "sid", artifacts_for(PCM_ONE_SECOND))
    provider._client = client
    await provider.close()
    assert client.transport.closed is True
    assert provider._client is None


def _pcm_from_wav(path):
    with wave.open(str(path)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        return wav.readframes(wav.getnframes())


@pytest.mark.skipif(not LIVE, reason="RUN_LIVE_TESTS=1 not set")
@pytest.mark.skipif(not CREDENTIALS, reason="GOOGLE_SA_FILE not set")
async def test_live_recognize_english_and_mandarin(tmp_path, log_records):
    provider, _ = make_provider(
        tmp_path,
        credentials_file=CREDENTIALS,
        project_id=os.environ.get("GOOGLE_STT_PROJECT_ID", ""),
        location=os.environ.get("GOOGLE_STT_LOCATION", "us"),
    )
    try:
        en = _pcm_from_wav(ASSETS_DIR / "en-US_turn_on_the_living_room_lights.wav")
        zh = _pcm_from_wav(ASSETS_DIR / "cmn-Hans-CN_turn_on_the_living_room_lights.wav")
        en_text, _ = await provider.speech_to_text([], "live-en", artifacts_for(en))
        zh_text, _ = await provider.speech_to_text([], "live-zh", artifacts_for(zh))
    finally:
        await provider.close()

    errors = [msg for level, msg in log_records if level == "error"]
    assert not errors, errors
    print(f"\nlive en: {en_text!r}\nlive zh: {zh_text!r}")
    assert "living room" in en_text.lower() and "light" in en_text.lower()
    assert "客厅" in zh_text and "灯" in zh_text
    assert any("en-US" in msg for level, msg in log_records if level == "info")
    assert any("cmn-Hans-CN" in msg for level, msg in log_records if level == "info")
