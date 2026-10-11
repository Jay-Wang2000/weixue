"""Barge-in flag (config barge_in, VOICE_BARGE_IN): the weixue_patches gate over upstream startToChat
(loaded from the vendored source) and upstream handleAbortMessage, for both voice LLM paths, the wake
cue, physical interrupts and the turn_latency line."""
import ast
import asyncio
import importlib
import json
import time

import pytest

import weixue_patches
from core.providers.llm.claude_agent import claude_agent as ca
from tests.conftest import LOGGER, VENDOR_DIR

FIRST, MIDDLE, LAST = "FIRST", "MIDDLE", "LAST"
REPLY = "I have turned on the living room light."
DROPPED = "barge-in disabled: dropped utterance during playback"


def _upstream_start_to_chat(namespace):
    tree = ast.parse((VENDOR_DIR / "core" / "handle" / "receiveAudioHandle.py").read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "startToChat")
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(module, "receiveAudioHandle.py", "exec"), namespace)
    return namespace["startToChat"]


class Ws:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))


class Executor:
    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args):
        self.submitted.append(args)


class Stream:
    """The claude_agent provider's open reply stream (conn.weixue_agent_stream)."""

    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class Conn:
    def __init__(self, barge_in, provider):
        self.config = {"wakeup_words": ["Jarvis"], "wake_cues": []}
        if barge_in is not None:
            self.config["barge_in"] = barge_in
        self.provider = provider
        self.aborts = 0
        self.logger = LOGGER
        self.session_id = "s"
        self.sentence_id = "turn-1"
        self.client_abort = False
        self.client_is_speaking = False
        self.client_have_voice = False
        self.client_listen_mode = "realtime"
        self.close_after_chat = False
        self.need_bind = False
        self.max_output_size = 0
        self.headers = {"device-id": "7c:0c:5f:42:4b:fc"}
        self.introduced_speakers = set()
        self.websocket = Ws()
        self.executor = Executor()
        if provider == "claude_agent":
            self.weixue_agent_stream = Stream()

    def chat(self, text):
        raise AssertionError("runs on the executor")

    def clear_queues(self):
        pass

    def clearSpeakStatus(self):
        self.client_is_speaking = False

    @property
    def turn_aborted(self):
        """The provider's in-flight turn was stopped: its stream closed (claude_agent), or an abort set the
        client_abort upstream chat() stops on (anthropic)."""
        if self.provider == "claude_agent":
            return self.weixue_agent_stream.cancelled
        return self.aborts > 0


@pytest.fixture
def stack():
    """(guard over upstream startToChat, abort as the device's abort message runs it, stt texts sent)."""
    upstream_abort = importlib.import_module("core.handle.abortHandle").handleAbortMessage

    async def counted_abort(conn):
        conn.aborts += 1
        await upstream_abort(conn)

    abort = weixue_patches.count_aborts(counted_abort)
    stt = []

    async def send_stt_message(conn, text):
        stt.append(text)
        conn.client_is_speaking = True

    async def no_intent(conn, text):
        return False

    start = _upstream_start_to_chat({
        "json": json, "TAG": "core.handle.receiveAudioHandle", "handleAbortMessage": abort,
        "handle_user_intent": no_intent, "send_stt_message": send_stt_message,
        "check_device_output_limit": lambda *_a: False,
    })
    return weixue_patches.guard_playback_echo(start, LOGGER), abort, stt


class Vad:
    def __init__(self, voice):
        self.voice = voice

    def is_vad(self, conn, _frame):
        conn.client_have_voice = self.voice
        return self.voice


VAD = weixue_patches.hold_wake_utterance(Vad.is_vad)


def utterance_starts(conn):
    conn.client_have_voice = False
    VAD(Vad(True), conn, b"")


async def play_reply(conn, text=REPLY):
    """A reply sentence playing on the device: tts start, its caption and audio written."""
    async def send(c, kind, audios, caption, sentence_id=None):
        for packet in audios or ():
            await write(c, packet, {"sentence_id": c.sentence_id})

    async def do_send(c, packet, flow_control):
        return None

    write = weixue_patches.watch_transport(do_send)
    watch = weixue_patches.watch_sent_audio(send, FIRST, LAST)
    conn.client_is_speaking = True
    await watch(conn, FIRST, [b"a", b"b"], text)


def dropped():
    return [m for _, m in LOGGER.records if m.startswith(DROPPED)]


PROVIDERS = ["anthropic", "claude_agent"]


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("barge_in", [None, False])
async def test_off_drops_speech_during_a_reply_without_abort_or_turn(stack, provider, barge_in):
    guard, _, stt = stack
    conn = Conn(barge_in, provider)
    await play_reply(conn)
    utterance_starts(conn)
    assert await guard(conn, "Stop, what about the kitchen?") is None
    assert dropped() == [f"{DROPPED}: 'Stop, what about the kitchen?'"]
    assert not conn.turn_aborted and conn.client_is_speaking and not conn.websocket.sent
    assert conn.executor.submitted == [] and stt == []
    assert weixue_patches.event_generation(conn) == 0


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_on_speech_during_a_reply_aborts_it_and_starts_a_turn(stack, provider):
    guard, _, stt = stack
    conn = Conn(True, provider)
    await play_reply(conn)
    utterance_starts(conn)
    await guard(conn, "Stop, what about the kitchen?")
    assert conn.turn_aborted and {"type": "tts", "state": "stop", "session_id": "s"} in conn.websocket.sent
    assert stt == ["Stop, what about the kitchen?"] and conn.executor.submitted == [("Stop, what about the kitchen?",)]
    assert dropped() == []


@pytest.mark.parametrize("barge_in, dropped_as_echo", [(False, True), (True, False)])
async def test_echo_during_a_reply_with_either_value(stack, barge_in, dropped_as_echo):
    guard, _, stt = stack
    conn = Conn(barge_in, "claude_agent")
    await play_reply(conn)
    utterance_starts(conn)
    assert await guard(conn, "turned on the living room light") is None
    assert stt == [] and not conn.turn_aborted
    logged = [m for _, m in LOGGER.records if "living room" in m]
    if dropped_as_echo:
        assert logged == [f"{DROPPED}: 'turned on the living room light' (echo of {REPLY!r})"]
    else:
        assert logged == [f"playback echo ignored: 'turned on the living room light' matches {REPLY!r}"]


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_off_utterance_that_outlasts_the_reply_is_still_dropped(stack, provider):
    guard, _, stt = stack
    conn = Conn(False, provider)
    await play_reply(conn)
    utterance_starts(conn)
    conn.clearSpeakStatus()
    assert await guard(conn, "the light") is None
    assert len(dropped()) == 1 and stt == [] and conn.executor.submitted == []


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_off_speech_after_playback_is_a_normal_turn_and_echo_is_only_logged(stack, provider):
    guard, _, stt = stack
    conn = Conn(False, provider)
    await play_reply(conn)
    conn.clearSpeakStatus()
    utterance_starts(conn)
    await guard(conn, "turned on the living room light")
    assert stt == ["turned on the living room light"] and conn.executor.submitted == [("turned on the living room light",)]
    assert not conn.turn_aborted and dropped() == []
    assert any(m.startswith("playback echo kept (barge-in disabled)") for _, m in LOGGER.records)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("event", ["abort", "wake"])
async def test_off_physical_interrupt_then_speech_is_a_normal_turn(stack, provider, event):
    guard, abort, stt = stack
    conn = Conn(False, provider)
    await play_reply(conn)
    utterance_starts(conn)
    if event == "abort":
        await abort(conn)
        assert conn.turn_aborted and not conn.client_is_speaking
    else:
        weixue_patches.note_event(conn)
        conn.clearSpeakStatus()
    conn.client_abort = False
    await guard(conn, "What time is it?")
    assert stt == ["What time is it?"] and conn.executor.submitted == [("What time is it?",)]
    assert dropped() == []


@pytest.mark.parametrize("barge_in", [False, True])
async def test_speech_during_the_wake_cue_is_the_request_with_either_value(stack, barge_in):
    guard, _, stt = stack
    events = []

    async def audio_to_data(_path):
        return [b"cue"]

    async def send_tts_message(conn, state, text=None):
        events.append(state)
        if state == "stop":
            conn.clearSpeakStatus()

    async def send_audio_message(conn, kind, audios, text, sentence_id=None):
        events.append(kind)
        if kind == FIRST:
            utterance_starts(conn)
            starts.set()
            await release.wait()

    starts, release = asyncio.Event(), asyncio.Event()
    play = weixue_patches.wake_cue_player(audio_to_data, send_tts_message, send_audio_message, FIRST, LAST, LOGGER)
    conn = Conn(barge_in, "claude_agent")
    conn.tts = object()
    previous = weixue_patches.CUE_DELAY_S
    weixue_patches.CUE_DELAY_S = 0.01
    try:
        cue = asyncio.create_task(play(conn, {"file": "x.wav", "text": "Yes?"}))
        await asyncio.wait_for(starts.wait(), 2)
        assert conn.client_is_speaking and conn.weixue_cue_speaking == conn.sentence_id
        assert conn.weixue_onset_playback is None
        await guard(conn, "What time is it?")
        release.set()
        await cue
    finally:
        weixue_patches.CUE_DELAY_S = previous
    assert {"type": "tts", "state": "stop", "session_id": "s"} in conn.websocket.sent
    assert stt == ["What time is it?"] and conn.executor.submitted == [("What time is it?",)]
    assert dropped() == []


def test_onset_records_playback_only_for_a_reply():
    conn = Conn(False, "anthropic")
    utterance_starts(conn)
    assert conn.weixue_onset_playback is None
    conn.client_is_speaking = True
    utterance_starts(conn)
    assert conn.weixue_onset_playback == 0 and weixue_patches.drops_utterance(conn)
    weixue_patches.note_event(conn)
    assert not weixue_patches.drops_utterance(conn)
    conn.config["barge_in"] = True
    utterance_starts(conn)
    assert weixue_patches.heard_during_playback(conn) and not weixue_patches.drops_utterance(conn)


@pytest.mark.parametrize("barge_in, shown", [(None, "barge_in=0"), (False, "barge_in=0"), (True, "barge_in=1")])
def test_turn_latency_line_shows_the_flag(barge_in, shown):
    conn = Conn(barge_in, "claude_agent")
    conn.weixue_speech_end = time.monotonic()
    line = ca.LLMProvider._metrics(conn, "turn-1").line()
    assert line.startswith("turn_latency ") and line.endswith(f" {shown}")
