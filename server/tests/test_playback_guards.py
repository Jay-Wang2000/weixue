"""weixue_patches playback record and echo guard, and proactive delivery outcomes driven
through the real Google TTS provider threads and upstream sendAudioMessage."""
import asyncio
import sys
import threading
import time
import types

import pytest
from google.api_core import exceptions as gexc
from google.cloud import texttospeech

import weixue_patches
import weixue_proactive as wp
from tests.conftest import LOGGER, TEST_CONFIG
from tests.test_conversation_timing import REMOVE_PUNCTUATION, stub_handler_modules, stub_listen_and_http

FIRST, MIDDLE, LAST = "FIRST", "MIDDLE", "LAST"
DEVICE = "7c:0c:5f:42:4b:fc"


class Conn:
    def __init__(self):
        self.sentence_id = "turn-1"
        self.client_abort = False
        self.client_is_speaking = False
        self.client_have_voice = False
        self.config = {"wakeup_words": ["Jarvis"], "wake_cues": []}


def _watch():
    sent = []

    async def send(conn, kind, audios, text, sentence_id=None):
        sent.append((kind, sentence_id))

    return sent, weixue_patches.watch_sent_audio(send, FIRST, LAST)


async def _play(watch, conn, text, sid=None, packets=3, last=True):
    sid = sid or conn.sentence_id
    await watch(conn, FIRST, [], text, sid)
    for _ in range(packets):
        await watch(conn, MIDDLE, b"opus", None, sid)
    if last:
        await watch(conn, LAST, [], None, sid)


# Playback record

async def test_record_counts_audio_and_marks_the_end():
    sent, watch = _watch()
    conn = Conn()
    record = weixue_patches.track_playback(conn, "turn-1")
    await _play(watch, conn, "The light is on.")
    assert (record.frames, record.done.is_set()) == (3, True)
    assert [k for k, _ in sent] == [FIRST, MIDDLE, MIDDLE, MIDDLE, LAST]
    weixue_patches.untrack_playback(conn, "turn-1")
    assert conn.weixue_playback == {}


async def test_record_of_a_failed_synthesis_has_no_audio():
    _, watch = _watch()
    conn = Conn()
    record = weixue_patches.track_playback(conn, "turn-1")
    await _play(watch, conn, "Water detected.", packets=0)
    assert (record.frames, record.done.is_set()) == (0, True)


async def test_record_of_a_segment_without_audio_is_failed():
    _, watch = _watch()
    conn = Conn()
    record = weixue_patches.track_playback(conn, "turn-1")
    await _play(watch, conn, "Water detected under the sink.", last=False)
    await _play(watch, conn, "Shall I close the valve?", packets=0)
    assert (record.frames, record.done.is_set(), record.failed) == (3, True, True)


async def test_record_of_complete_segments_is_not_failed():
    _, watch = _watch()
    conn = Conn()
    record = weixue_patches.track_playback(conn, "turn-1")
    await _play(watch, conn, "Water detected under the sink.", last=False)
    await _play(watch, conn, "Shall I close the valve?")
    assert (record.frames, record.failed) == (6, False)


def test_synthesis_failure_marks_only_the_tracked_sentence():
    conn = Conn()
    record = weixue_patches.track_playback(conn, "turn-1")
    weixue_patches.note_synthesis_failure(conn, "an-older-turn")
    assert record.failed is False
    weixue_patches.note_synthesis_failure(conn, "turn-1")
    assert record.failed is True
    weixue_patches.note_synthesis_failure(Conn(), "turn-1")


async def test_record_ignores_aborted_and_stale_audio():
    _, watch = _watch()
    conn = Conn()
    record = weixue_patches.track_playback(conn, "turn-1")
    await watch(conn, MIDDLE, b"opus", None, "an-older-turn")
    conn.client_abort = True
    await _play(watch, conn, "Hello.")
    assert (record.frames, record.done.is_set()) == (0, False)


# Echo guard

def _onset(conn, delay=0.0):
    conn.weixue_voice_onset = (time.monotonic() + delay, conn.sentence_id, conn.client_is_speaking)


async def _played(texts, speaking=True):
    _, watch = _watch()
    conn = Conn()
    conn.client_is_speaking = speaking
    for text in texts:
        await _play(watch, conn, text, last=False)
    return conn, watch


@pytest.mark.parametrize(
    "heard",
    [
        "Okay, I've turned on the living room light.",
        "turned on the living room light",
        "I turned on the living room lights",
        '{"speaker": "", "content": "turned on the living room light"}',
    ],
)
async def test_echo_of_the_playing_reply_is_recognised(heard):
    conn, _ = await _played(["Okay, I've turned on the living room light."])
    _onset(conn)
    assert weixue_patches.echo_of_playback(conn, heard) == "Okay, I've turned on the living room light."


async def test_chinese_echo_is_recognised():
    conn, _ = await _played(["好的，客厅的灯已经打开了。"])
    _onset(conn)
    assert weixue_patches.echo_of_playback(conn, "客厅的灯已经打开") is not None


@pytest.mark.parametrize("heard", ["Yes.", "No.", "Stop.", "What time is it?", "嗯", "Turn off the kitchen light."])
async def test_real_answers_during_playback_are_kept(heard):
    conn, _ = await _played(["Yes, I've turned on the living room light. Shall I dim it?"])
    _onset(conn)
    assert weixue_patches.echo_of_playback(conn, heard) is None


@pytest.mark.parametrize("cue, heard", [("Yes?", "Yes."), ("Hmm?", "Hmm."), ("Mm-hm?", "Mm-hm.")])
async def test_wake_cue_captions_are_not_echo_candidates(cue, heard):
    _, watch = _watch()
    conn = Conn()
    conn.client_is_speaking = True
    conn.sentence_id = conn.weixue_cue_sentence = "cue-1"
    await watch(conn, FIRST, [b"cue"], cue, "cue-1")
    await watch(conn, LAST, [], None, "cue-1")
    _onset(conn)
    assert weixue_patches.echo_of_playback(conn, heard) is None
    assert "weixue_spoken" not in conn.__dict__ or not conn.weixue_spoken


async def test_matching_speech_that_starts_after_playback_is_kept(monkeypatch):
    monkeypatch.setattr(weixue_patches, "ECHO_TAIL_S", 0.05)
    _, watch = _watch()
    conn = Conn()
    conn.client_is_speaking = True
    await _play(watch, conn, "Shall I close the valve?")
    conn.client_is_speaking = False
    await asyncio.sleep(0.1)
    _onset(conn)
    assert weixue_patches.echo_of_playback(conn, "Shall I close the valve?") is None


async def test_speech_that_starts_within_the_tail_matches():
    _, watch = _watch()
    conn = Conn()
    conn.client_is_speaking = True
    await _play(watch, conn, "Shall I close the valve?")
    conn.client_is_speaking = False
    _onset(conn)
    assert weixue_patches.echo_of_playback(conn, "close the valve") is not None


async def test_no_onset_or_empty_text_never_matches():
    conn, _ = await _played(["Hello there."])
    assert weixue_patches.echo_of_playback(conn, "Hello there.") is None
    _onset(conn)
    assert weixue_patches.echo_of_playback(conn, "?!") is None


async def test_sentences_not_yet_started_at_onset_are_ignored():
    _, watch = _watch()
    conn = Conn()
    conn.client_is_speaking = True
    _onset(conn, delay=-0.01)
    await _play(watch, conn, "Here is the weather for today.", last=False)
    assert weixue_patches.echo_of_playback(conn, "the weather for today") is None


class FakeVad:
    def __init__(self, voice):
        self.voice = voice

    def is_vad(self, conn, _frame):
        conn.client_have_voice = self.voice
        return self.voice


def test_vad_wrapper_records_the_utterance_onset():
    is_vad = weixue_patches.hold_wake_utterance(FakeVad.is_vad)
    conn = Conn()
    conn.client_is_speaking = True
    is_vad(FakeVad(False), conn, b"")
    assert not hasattr(conn, "weixue_voice_onset")
    is_vad(FakeVad(True), conn, b"")
    started, sid, holding = conn.weixue_voice_onset
    assert sid == "turn-1" and holding is True and abs(time.monotonic() - started) < 1
    conn.sentence_id = "turn-2"
    is_vad(FakeVad(True), conn, b"")
    assert conn.weixue_voice_onset[1] == "turn-1"


async def test_guard_drops_echo_before_barge_in_and_passes_speech():
    calls = []

    async def start_to_chat(conn, text):
        calls.append(text)

    guard = weixue_patches.guard_playback_echo(start_to_chat, LOGGER)
    conn, _ = await _played(["The kitchen light is now off."])
    conn.weixue_wake_cue = asyncio.create_task(asyncio.sleep(10))
    _onset(conn)
    assert await guard(conn, "the kitchen light is now off") is None
    assert calls == [] and not conn.weixue_wake_cue.done()
    assert any("playback echo ignored" in m for _, m in LOGGER.records)
    await guard(conn, "Yes.")
    await asyncio.sleep(0)
    assert calls == ["Yes."] and conn.weixue_wake_cue.cancelled()


async def test_cue_caption_is_not_kept_through_the_wrapped_sender():
    sent, watch = _watch()
    events = []

    async def audio_to_data(_path):
        return [b"cue"]

    async def send_tts_message(conn, state, text=None):
        events.append(state)

    monkey_delay = weixue_patches.CUE_DELAY_S
    weixue_patches.CUE_DELAY_S = 0.01
    try:
        play = weixue_patches.wake_cue_player(audio_to_data, send_tts_message, watch, FIRST, LAST, LOGGER)
        conn = Conn()
        conn.tts = object()
        await play(conn, {"file": "x.wav", "text": "Hmm?"})
    finally:
        weixue_patches.CUE_DELAY_S = monkey_delay
    assert not conn.__dict__.get("weixue_spoken")
    assert conn.weixue_cue_sentence == conn.sentence_id
    assert [k for k, _ in sent] == [FIRST, LAST]


async def test_apply_wraps_playback_hooks_once(monkeypatch):
    connection = types.ModuleType("core.connection")

    class ConnectionHandler:
        _clean_response_garbage = staticmethod(lambda text: text)
        _extract_direct_answer_response = staticmethod(lambda arguments: "")

        async def handle_connection(self, ws):
            return None

    connection.ConnectionHandler = ConnectionHandler
    monkeypatch.setitem(sys.modules, "core.connection", connection)
    stub_handler_modules(monkeypatch)
    stub_listen_and_http(monkeypatch)
    send_audio = sys.modules["core.handle.sendAudioHandle"]
    tts_base = sys.modules["core.providers.tts.base"]
    asr_base = sys.modules["core.providers.asr.base"]
    server = sys.modules["core.http_server"].SimpleHttpServer
    upstream_start, upstream_init = asr_base.startToChat, server.__init__
    proactive = wp.Proactive("t")
    monkeypatch.setattr(wp, "INSTANCE", proactive)

    weixue_patches.apply()
    weixue_patches.apply()

    assert send_audio.sendAudioMessage.weixue_playback and send_audio.sendAudioMessage.weixue_wrapped is None
    assert tts_base.sendAudioMessage.weixue_playback and tts_base.sendAudioMessage.weixue_wrapped is None
    assert asr_base.startToChat.weixue_wrapped is upstream_start
    assert server.__init__.weixue_wrapped is upstream_init
    server({"server": {"auth_key": "k", "auth": {"enabled": True, "allowed_devices": [DEVICE]}}})
    assert proactive.auth.device({"device-id": DEVICE}) == DEVICE


# Proactive delivery through the real TTS pipeline

class Ws:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


class PipelineConn:
    def __init__(self, loop, tts):
        self.loop = loop
        self.tts = tts
        self.sample_rate = 24000
        self.audio_format = "opus"
        self.stop_event = threading.Event()
        self.client_abort = False
        self.client_is_speaking = False
        self.client_have_voice = False
        self.sentence_id = None
        self.session_id = "s1"
        self.headers = {"device-id": DEVICE}
        self.device_id = DEVICE
        self.max_output_size = 0
        self.config = {}
        self.calling = False
        self.close_after_chat = False
        self.conn_from_mqtt_gateway = False
        self.logger = LOGGER
        self.websocket = Ws()
        self.dialogue = types.SimpleNamespace(dialogue=[])
        self.dialogue.put = self.dialogue.dialogue.append

    def clearSpeakStatus(self):
        self.client_is_speaking = False

    async def close(self):
        self.stop_event.set()


@pytest.fixture
async def pipeline(monkeypatch):
    pytest.importorskip("opuslib_next")
    from tests.test_tts_google import HAVE_OPUS, FakeClient, _pcm

    if not HAVE_OPUS:
        pytest.skip("libopus not available")
    import core.handle.sendAudioHandle as send_audio
    import core.providers.tts.base as tts_base
    from core.providers.tts.dto.dto import SentenceType
    from core.utils.opus_encoder_utils import OpusEncoderUtils
    from core.utils.tts import create_instance

    FakeClient.instances = []
    FakeClient.call_error = None
    FakeClient.fail_mid = None
    FakeClient.chunks = [_pcm(0.12), _pcm(0.12)]
    monkeypatch.setattr(texttospeech, "TextToSpeechClient", FakeClient)
    monkeypatch.setattr(
        tts_base,
        "sendAudioMessage",
        weixue_patches.watch_sent_audio(send_audio.sendAudioMessage, SentenceType.FIRST, SentenceType.LAST),
    )
    provider = create_instance("google_tts", dict(TEST_CONFIG["TTS"]["GoogleTTS"]), True)
    conn = PipelineConn(asyncio.get_running_loop(), provider)
    provider.conn = conn
    provider.sample_rate = conn.sample_rate
    provider.opus_encoder = OpusEncoderUtils(conn.sample_rate, 1, 60)
    threads = [
        threading.Thread(target=provider.tts_text_priority_thread, daemon=True),
        threading.Thread(target=provider._audio_play_priority_thread, daemon=True),
    ]
    for t in threads:
        t.start()
    yield conn, FakeClient
    conn.stop_event.set()
    for t in threads:
        t.join(timeout=3)


def _message(p, **body):
    _, created = p.submit(wp.parse_request({"message": "Water detected under the sink.", **body}), "ha")
    return p.messages[created["id"]]


async def test_synthesis_failure_is_not_reported_as_spoken(pipeline):
    conn, client = pipeline
    client.call_error = gexc.InvalidArgument("unsupported sample rate")
    p = wp.Proactive("t", speaker=wp.speak, auth=wp.DeviceAuth(True, [DEVICE]))
    item = _message(p)
    outcome = await asyncio.wait_for(wp.speak(conn, item, close_after=False), 10)
    assert outcome == "no_audio"
    assert not any(isinstance(m, bytes) for m in conn.websocket.sent)


async def test_failed_synthesis_requeues_and_a_good_one_delivers(pipeline):
    conn, client = pipeline
    client.call_error = gexc.InvalidArgument("unsupported sample rate")
    p = wp.Proactive("t", speaker=wp.speak, auth=wp.DeviceAuth(True, [DEVICE]))
    item = _message(p)
    await asyncio.wait_for(p.claim(conn, item), 10)
    assert (item.status, item.attempts) == ("queued", 1)
    client.call_error = None
    await asyncio.wait_for(p.claim(conn, item), 10)
    assert (item.status, item.attempts) == ("delivered", 2)
    assert sum(isinstance(m, bytes) for m in conn.websocket.sent) > 0


async def test_session_lost_mid_audio_is_partial(pipeline, monkeypatch):
    conn, _ = pipeline
    real_send = conn.websocket.send

    async def send(data):
        await real_send(data)
        if isinstance(data, bytes):
            conn.stop_event.set()

    conn.websocket.send = send
    p = wp.Proactive("t", speaker=wp.speak, auth=wp.DeviceAuth(True, [DEVICE]))
    item = _message(p, urgent=True)
    await asyncio.wait_for(p.claim(conn, item), 10)
    assert item.status == "partial" and not p.queue


async def test_synthesis_failing_mid_stream_is_partial(pipeline):
    from tests.test_tts_google import _pcm

    conn, client = pipeline
    client.chunks = [_pcm(0.24), _pcm(0.24)]
    client.fail_mid = 1
    p = wp.Proactive("t", speaker=wp.speak, auth=wp.DeviceAuth(True, [DEVICE]))
    item = _message(p)
    await asyncio.wait_for(p.claim(conn, item), 10)
    assert sum(isinstance(m, bytes) for m in conn.websocket.sent) > 0
    assert (item.status, item.error, item.attempts) == ("partial", "synthesis failed after audio started", 1)
    assert not p.queue


async def test_later_request_chunk_failing_is_partial(pipeline, monkeypatch):
    import core.providers.tts.google_tts as google_tts

    conn, client = pipeline
    monkeypatch.setattr(google_tts, "_MAX_REQUEST_BYTES", 40)
    texts = []
    stream = client.streaming_synthesize

    def second_fails(self, requests, timeout=None):
        reqs = list(requests)
        texts.append(reqs[1].input.text)
        if len(texts) > 1:
            raise gexc.ServiceUnavailable("backend unavailable")
        return stream(self, iter(reqs), timeout)

    monkeypatch.setattr(client, "streaming_synthesize", second_fails)
    p = wp.Proactive("t", speaker=wp.speak, auth=wp.DeviceAuth(True, [DEVICE]))
    item = _message(p, message="Water detected under the kitchen sink, shall I turn off the main water valve?")
    await asyncio.wait_for(p.claim(conn, item), 10)
    assert len(texts) == 2
    assert sum(isinstance(m, bytes) for m in conn.websocket.sent) > 0
    assert (item.status, item.error) == ("partial", "synthesis failed after audio started")
