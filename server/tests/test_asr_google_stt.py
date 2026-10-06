import os
import wave
from pathlib import Path

import pytest
from google.api_core import exceptions as gexc
from google.cloud.speech_v2.types import cloud_speech

from core.providers.asr import google_stt
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType
from tests.conftest import ASSETS_DIR, OVERLAY_DIR

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


async def test_audio_over_sync_limit_is_trimmed(tmp_path, monkeypatch):
    provider, client = make_provider(tmp_path, monkeypatch, responses=[_response(("long", "en-US"))])
    pcm = b"\x00" * (google_stt.MAX_SYNC_AUDIO_BYTES + 32000)
    await provider.speech_to_text([], "sid", artifacts_for(pcm))
    assert len(client.calls[0]["request"].content) == google_stt.MAX_SYNC_AUDIO_BYTES


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


@pytest.mark.skipif(os.environ.get("RUN_LIVE_TESTS") != "1", reason="RUN_LIVE_TESTS=1 not set")
async def test_live_recognize_english_and_mandarin(tmp_path, log_records):
    credentials_file = os.environ.get("GOOGLE_STT_CREDENTIALS_FILE") or os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS"
    )
    if not credentials_file:
        pytest.skip("GOOGLE_STT_CREDENTIALS_FILE not set")
    provider, _ = make_provider(
        tmp_path,
        credentials_file=credentials_file,
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
