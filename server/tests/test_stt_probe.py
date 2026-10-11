"""Segment probe, wake-only drop, capture and session counters (weixue_stt_probe, weixue_patches)."""
import json
import math
import re
import time
import wave

import pytest

import weixue_patches
import weixue_stt_probe
from tests.conftest import LOGGER
from tests.test_conversation_timing import REMOVE_PUNCTUATION
from tests.test_wake_cue import CUES, Conn, Upstream

FRAME = b"\x10\x00" * 960


class Provider:
    def __init__(self, text="What time is it?"):
        self.text = text
        self.stt_calls = 0

    async def handle_voice_stop(self, conn, asr_audio_task):
        await self.speech_to_text_wrapper(asr_audio_task, "sid")

    async def speech_to_text_wrapper(self, pcm_data, session_id):
        self.stt_calls += 1
        return self.text, None

    async def receive_audio(self, conn, pcm_frame, audio_have_voice):
        conn.asr_audio.append(pcm_frame)


@pytest.fixture
def wrapped():
    class Wrapped(Provider):
        pass

    Wrapped.handle_voice_stop = weixue_patches.note_speech_end(Provider.handle_voice_stop, LOGGER)
    Wrapped.speech_to_text_wrapper = weixue_patches.keep_transcript(Provider.speech_to_text_wrapper)
    Wrapped.receive_audio = weixue_patches.note_frames(Provider.receive_audio)
    return Wrapped


def segment_lines(log_records):
    return [msg for level, msg in log_records if level == "info" and msg.startswith("stt_segment ")]


async def wake(conn):
    async def play(*_args, **_kwargs):
        return None

    handler = weixue_patches.acknowledge_wake(Upstream.handle, REMOVE_PUNCTUATION, play)
    await handler(Upstream(), conn, {"state": "detect", "text": "Jarvis"})
    await handler(Upstream(), conn, {"state": "start", "mode": "realtime"})


async def feed(provider, conn, frames, gap=0.0):
    for frame in frames:
        if gap:
            time.sleep(gap)
        await provider.receive_audio(conn, frame, True)


@pytest.mark.parametrize("live_voice, kind, stt_calls", [(False, "wake_only", 0), (True, "wake", 1)])
async def test_first_segment_after_a_wake(wrapped, log_records, live_voice, kind, stt_calls):
    conn = Conn(cues=CUES)
    conn.asr_audio = []
    provider = wrapped()
    await wake(conn)
    conn.weixue_live_voice = live_voice
    await feed(provider, conn, [FRAME] * 20)
    await provider.handle_voice_stop(conn, list(conn.asr_audio))
    assert provider.stt_calls == stt_calls
    (line,) = segment_lines(log_records)
    assert f"kind={kind} frames=20 seconds=1.2 burst=20" in line
    dropped = any("wake-only segment dropped" in msg for _level, msg in log_records)
    assert dropped is (kind == "wake_only")
    assert conn.weixue_wake_segment is False


async def test_later_segments_are_follow_ups(wrapped, log_records):
    conn = Conn(cues=CUES)
    conn.asr_audio = []
    provider = wrapped()
    await wake(conn)
    await provider.handle_voice_stop(conn, [FRAME] * 20)
    await provider.handle_voice_stop(conn, [b"".join([FRAME] * 20)])
    assert provider.stt_calls == 1
    assert [line.split()[1] for line in segment_lines(log_records)] == ["kind=wake_only", "kind=follow"]
    assert conn.weixue_segments == 2


async def test_segment_line_reports_level_and_leading_burst(wrapped, log_records):
    conn = Conn(cues=CUES)
    conn.asr_audio = []
    provider = wrapped()
    await feed(provider, conn, [FRAME] * 5)
    await feed(provider, conn, [FRAME] * 3, gap=0.03)
    await provider.handle_voice_stop(conn, [b"".join(conn.asr_audio)])
    (line,) = segment_lines(log_records)
    peak = round(20 * math.log10(16 / 32768), 1)
    assert f"kind=follow frames=8 seconds=0.48 burst=5 peak_dbfs={peak} rms_dbfs={peak}" in line
    assert conn.weixue_frames_in == 8 and conn.weixue_bytes_in == 8 * 1920


def test_level_of_silence_and_empty_audio():
    assert weixue_stt_probe.level_dbfs(b"\x00\x00" * 10) == (-math.inf, -math.inf)
    assert weixue_stt_probe.level_dbfs(b"") == (-math.inf, -math.inf)
    peak, rms = weixue_stt_probe.level_dbfs(b"\xff\x7f\x01\x80")
    assert round(peak, 2) == 0.0 and round(rms, 2) == 0.0


def test_leading_burst():
    assert weixue_stt_probe.leading_burst([]) == 0
    assert weixue_stt_probe.leading_burst([0.0]) == 1
    assert weixue_stt_probe.leading_burst([0.0, 0.001, 0.002, 0.06, 0.061]) == 3


async def test_capture_writes_the_segment_and_the_transcript(wrapped, monkeypatch, tmp_path, log_records):
    monkeypatch.setenv(weixue_stt_probe.CAPTURE_ENV, "1")
    monkeypatch.setenv(weixue_stt_probe.CAPTURE_DIR_ENV, str(tmp_path / "captures"))
    conn = Conn(cues=CUES)
    conn.asr_audio = []
    provider = wrapped()
    provider.weixue_last_stt = {"model": "chirp_3", "fallback": "no_final"}
    await feed(provider, conn, [FRAME] * 20)
    await provider.handle_voice_stop(conn, list(conn.asr_audio))

    (wav_path,) = sorted((tmp_path / "captures").glob("*.wav"))
    assert re.fullmatch(r"\d{8}T\d{6}\.\d{3}Z-\d{4}", wav_path.stem)
    with wave.open(str(wav_path)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert wav.readframes(wav.getnframes()) == FRAME * 20
    meta = json.loads(wav_path.with_suffix(".json").read_text())
    assert meta["kind"] == "follow" and meta["frames"] == 20 and meta["provider"] == "test_stt_probe"
    assert len(meta["arrival_offsets_s"]) == 20 and meta["arrival_offsets_s"][0] == 0
    assert meta["result"] == {"text": "What time is it?", "model": "chirp_3", "fallback": "no_final"}
    assert f"capture={wav_path.stem}" in segment_lines(log_records)[0]


async def test_capture_is_off_without_the_env(wrapped, monkeypatch, tmp_path, log_records):
    monkeypatch.delenv(weixue_stt_probe.CAPTURE_ENV, raising=False)
    monkeypatch.setenv(weixue_stt_probe.CAPTURE_DIR_ENV, str(tmp_path / "captures"))
    conn = Conn(cues=CUES)
    conn.asr_audio = []
    provider = wrapped()
    await provider.handle_voice_stop(conn, [FRAME] * 20)
    assert not (tmp_path / "captures").exists()
    assert "capture=" not in segment_lines(log_records)[0]


def test_capture_keeps_the_newest_pairs(monkeypatch, tmp_path):
    monkeypatch.setenv(weixue_stt_probe.CAPTURE_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(weixue_stt_probe.CAPTURE_MAX_ENV, "2")
    names = [weixue_stt_probe.write_capture(FRAME, {"n": i}) for i in range(4)]
    assert sorted(p.stem for p in tmp_path.glob("*.wav")) == names[2:]
    assert sorted(p.stem for p in tmp_path.glob("*.json")) == names[2:]


async def test_listen_start_without_a_detect_is_logged(log_records):
    upstream = Upstream()
    handler = weixue_patches.note_listen(Upstream.handle, LOGGER)
    conn = Conn()
    conn.weixue_frames_in = 7
    await handler(upstream, conn, {"state": "detect", "text": "Jarvis"})
    await handler(upstream, conn, {"state": "start", "mode": "realtime"})
    await handler(upstream, conn, {"state": "start", "mode": "realtime"})
    await handler(upstream, conn, {"state": "stop"})
    assert len(upstream.calls) == 4
    assert (conn.weixue_detects, conn.weixue_listen_starts) == (1, 2)
    (line,) = [msg for _level, msg in log_records if msg.startswith("listen start without a wake detect")]
    assert line.endswith("mode=realtime start=2 frames_before=7")


async def test_session_end_logs_the_audio_counts(log_records):
    class Proactive:
        auth = type("Auth", (), {"device": staticmethod(lambda headers: None)})()

    class Session:
        session_id = "sess-counts"

    async def handle(self, ws):
        self.weixue_packets_in, self.weixue_frames_in, self.weixue_bytes_in, self.weixue_segments = 42, 40, 76800, 2
        self.weixue_detects, self.weixue_listen_starts = 1, 1

    ws = type("Ws", (), {"request": type("Req", (), {"headers": {}})()})()
    await weixue_patches.register_sessions(handle, Proactive(), LOGGER)(Session(), ws)
    assert "audio session end packets=42 frames=40 bytes=76800 segments=2 detects=1 listen_starts=1" in [
        msg for _level, msg in log_records
    ]


async def test_audio_messages_are_counted_before_routing():
    seen = []

    class Connection:
        async def _route_message(self, message):
            seen.append(message)

    route = weixue_patches.count_packets(Connection._route_message)
    conn = Connection()
    for message in (b"\x01", "{}", bytearray(b"\x02"), b"\x03"):
        await route(conn, message)
    assert conn.weixue_packets_in == 3 and len(seen) == 4
