import asyncio
import queue
import re
import threading
import time
from typing import List, Optional, Tuple

from google.cloud.speech_v2.types import cloud_speech

from config.logger import setup_logging
from core.providers.asr import google_stt
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType

TAG = __name__
logger = setup_logging()

# Utterances of at most 15 frames of 1920 bytes are not recognized.
MIN_UTTERANCE_BYTES = 1920 * 15
FRAME_S = 0.06
# A follow-on stream starts RESEND_FRAMES before the finished stream's reported end of utterance
# (without a reported end: before the frame on which the end was noticed).
RESEND_FRAMES = 2
# A stream whose reported end of utterance lies more than TRUNCATED_TAIL_S before the end of the
# audio it was given, with no follow-on stream after it, dropped the rest of the utterance.
TRUNCATED_TAIL_S = 0.6
STREAM_DEADLINE_S = google_stt.MAX_SYNC_AUDIO_BYTES / google_stt.BYTES_PER_SECOND + 15
# latest_short reports confidence 0.91 on every Mandarin final
# (docs/logs/feat-stt-streaming-accuracy-2026-10-07.md).
DEFAULT_ZH_FALLBACK_CONFIDENCE = 0.9
DEFAULT_REPEAT_REPLY = "Sorry, I didn't catch that. Please say it again.\n抱歉，我没听清，请再说一遍。"
# Problems after which the streamed text is incomplete: only a complete-buffer fallback transcript is used.
STREAM_PROBLEMS = ("no_stream", "stream_error", "timeout", "truncated", "wake_word_only")
# Streams that ended with no non-empty final on an utterance over MIN_UTTERANCE_BYTES: the buffered
# utterance is re-recognized with fallback_model; an empty or failed re-recognition is no turn.
NO_FINAL = "no_final"
_HAN = re.compile(r"[一-鿿]")
_NOT_WORD = re.compile(r"[\W_]+")
END_OF_UTTERANCE = cloud_speech.StreamingRecognizeResponse.SpeechEventType.END_OF_SINGLE_UTTERANCE


def _truthy(value) -> bool:
    return str(value).lower() in ("true", "1", "yes")


def is_chinese(text: str, language_code: Optional[str]) -> bool:
    code = (language_code or "").lower()
    return code.startswith(("cmn", "zh", "yue")) or bool(_HAN.search(text or ""))


def _no_first_response_prefetch(client):
    """Makes client.streaming_recognize() return its call before the first response arrives,
    so the call can be cancelled while it waits."""
    stub = getattr(getattr(client, "transport", None), "streaming_recognize", None)
    if stub is not None and getattr(stub, "_prefetch_first_result_", True):
        stub._prefetch_first_result_ = False


class RecognitionStream:
    """One StreamingRecognize call fed from a queue on its own thread. send() and half_close()
    never block; finished is set when the response stream has ended. Times are monotonic
    seconds from the open."""

    def __init__(self, provider: "ASRProvider", frames: List[bytes], start_index: int = 0):
        self._queue: "queue.Queue[Optional[bytes]]" = queue.Queue()
        for frame in frames:
            self._queue.put(frame)
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._call = None
        self.opened_at = time.monotonic()
        self.start_index = start_index
        self.burst_frames = len(frames)
        self.frames = len(frames)
        self.bytes = sum(len(frame) for frame in frames)
        self.yielded = 0
        self.responses = 0
        self.first_interim_s: Optional[float] = None
        self.half_closed_s: Optional[float] = None
        self.ended_s: Optional[float] = None
        self.service_end = False
        self.utterance_end_s: Optional[float] = None
        self.empty_finals = 0
        self.cancelled = False
        self.half_closed = False
        self.finals: List[Tuple[str, Optional[str], float]] = []
        self.interims = 0
        self.error: Optional[BaseException] = None
        threading.Thread(
            target=self._run, args=(provider,), name="google-stt-stream", daemon=True
        ).start()

    @property
    def finished(self) -> bool:
        return self._done.is_set()

    def _since_open(self) -> float:
        return time.monotonic() - self.opened_at

    def send(self, pcm_frame: bytes):
        if not self.half_closed:
            self.frames += 1
            self.bytes += len(pcm_frame)
            self._queue.put(pcm_frame)

    def half_close(self):
        if not self.half_closed:
            self.half_closed = True
            self.half_closed_s = self._since_open()
            self._queue.put(None)

    def summary(self) -> str:
        """frames/bytes queued (burst = queued at open), audio requests sent, responses, finals
        (empty_finals: finals without a transcript), the service's end of utterance (audio
        offset), first interim / half-close / end in ms from the open, whether the service ended
        the stream before the half-close."""

        def ms(value):
            return "-" if value is None else str(round(value * 1000))

        return (
            f"burst={self.burst_frames} frames={self.frames} bytes={self.bytes} sent={self.yielded} "
            f"responses={self.responses} interims={self.interims} finals={len(self.finals)} "
            f"empty_finals={self.empty_finals} utterance_end_ms={ms(self.utterance_end_s)} "
            f"first_interim_ms={ms(self.first_interim_s)} half_close_ms={ms(self.half_closed_s)} "
            f"end_ms={ms(self.ended_s)} service_end={int(self.service_end)} cancelled={int(self.cancelled)}"
            + (f" error={type(self.error).__name__}" if self.error is not None else "")
        )

    def wait(self, timeout: float) -> bool:
        return self._done.wait(timeout)

    def cancel(self):
        with self._lock:
            if self._done.is_set():
                return
            self.cancelled = True
            call = self._call
        self.half_close()
        if call is not None:
            try:
                call.cancel()
            except Exception as e:
                logger.bind(tag=TAG).warning(f"Google STT stream cancel failed: {type(e).__name__}: {e}")

    def _requests(self, first: cloud_speech.StreamingRecognizeRequest):
        yield first
        while True:
            pcm = self._queue.get()
            if pcm is None:
                return
            self.yielded += 1
            yield cloud_speech.StreamingRecognizeRequest(audio=pcm)

    def _run(self, provider: "ASRProvider"):
        try:
            client = provider._get_client()
            _no_first_response_prefetch(client)
            call = client.streaming_recognize(
                requests=self._requests(provider._streaming_request()),
                timeout=STREAM_DEADLINE_S,
            )
            with self._lock:
                self._call = call
                cancelled = self.cancelled
            if cancelled:
                call.cancel()
            for response in call:
                self.responses += 1
                if response.speech_event_type == END_OF_UTTERANCE:
                    offset = response.speech_event_offset.total_seconds() if response.speech_event_offset else 0
                    self.utterance_end_s = offset if offset > 0 else None
                for result in response.results:
                    if result.is_final and not (result.alternatives and result.alternatives[0].transcript.strip()):
                        self.empty_finals += 1
                    if not result.alternatives:
                        continue
                    if not result.is_final:
                        self.interims += 1
                        if self.first_interim_s is None:
                            self.first_interim_s = self._since_open()
                        continue
                    alternative = result.alternatives[0]
                    self.finals.append(
                        (alternative.transcript.strip(), result.language_code or None, alternative.confidence)
                    )
        except Exception as e:
            if not self.cancelled:
                self.error = e
        finally:
            self.ended_s = self._since_open()
            self.service_end = not self.half_closed and not self.cancelled and self.error is None
            self._queue.put(None)
            self._done.set()


class ASRProvider(google_stt.ASRProvider):
    """Google Cloud Speech-to-Text v2 StreamingRecognize: audio is streamed from the first voiced
    frame, the stream is half-closed when the VAD ends the utterance, and the final transcript is
    taken from the stream. A stream error or timeout, a stream whose end of utterance cut the
    utterance short (truncated), a wake segment with live voice heard as only the wake word
    (wake_word_only), streams that ended with no non-empty final (no_final), and a Chinese final
    below zh_fallback_confidence, are re-recognized with fallback_model batch Recognize on the
    buffered utterance. After a stream error, timeout, truncation or wake_word_only only that
    re-recognition is used: when it fails the utterance is rejected (no turn) and the device
    speaks repeat_reply."""

    def __init__(self, config: dict, delete_audio_file: bool):
        super().__init__({**config, "timeout": config.get("fallback_timeout", 15)}, delete_audio_file)
        self.interface_type = InterfaceType.STREAM
        self.model = config.get("model") or "latest_short"
        self.interim_results = _truthy(config.get("interim_results", True))
        self.final_timeout = float(config.get("final_timeout", 2))
        self.fallback_model = config.get("fallback_model", "chirp_3") or None
        self.zh_fallback_confidence = float(
            config.get("zh_fallback_confidence", DEFAULT_ZH_FALLBACK_CONFIDENCE)
        )
        self.repeat_reply = config.get("repeat_reply") or DEFAULT_REPEAT_REPLY
        self._rejected: Optional[str] = None
        self._conn = None
        self._client_lock = threading.Lock()
        self._streams: List[RecognitionStream] = []
        self._buffered = 0
        self._resume_index: Optional[int] = None
        self._pending: Optional[List[RecognitionStream]] = None
        self._voice_stop_at: Optional[float] = None
        self.weixue_last_stt: Optional[dict] = None

    def _get_client(self):
        with self._client_lock:
            return super()._get_client()

    def _streaming_request(self) -> cloud_speech.StreamingRecognizeRequest:
        return cloud_speech.StreamingRecognizeRequest(
            recognizer=self._recognizer_path(),
            streaming_config=cloud_speech.StreamingRecognitionConfig(
                config=self._build_config(self.language_codes, self.model),
                streaming_features=cloud_speech.StreamingRecognitionFeatures(
                    interim_results=self.interim_results
                ),
            ),
        )

    def _drop_streams(self, reason: str):
        if self._streams:
            logger.bind(tag=TAG).info(f"Google STT streams dropped: {reason} streams={len(self._streams)}")
        for stream in self._streams:
            stream.cancel()
        self._streams = []
        self._resume_index = None

    async def receive_audio(self, conn, pcm_frame, audio_have_voice):
        """A stream opens on the first voiced frame with the buffered pre-roll. When the service
        ends a stream at its own end of utterance while the VAD still hears the utterance, the
        next voiced frame opens a follow-on stream; finals of all streams form the transcript."""
        self._conn = conn
        await ASRProviderBase.receive_audio(self, conn, pcm_frame, audio_have_voice)
        google_stt.cap_buffered_audio(conn.asr_audio)
        if self._streams and len(conn.asr_audio) != self._buffered + 1:
            self._drop_streams("audio_reset")
        self._buffered = len(conn.asr_audio)
        over_limit = sum(len(frame) for frame in conn.asr_audio) > google_stt.MAX_SYNC_AUDIO_BYTES
        if over_limit:
            self._drop_streams("over_limit")
        active = self._streams[-1] if self._streams else None
        streaming = active is not None and not active.finished
        if streaming:
            active.send(pcm_frame)
        if conn.client_voice_stop and conn.client_listen_mode != "manual":
            await self._finish(conn)
            return
        if streaming or over_limit:
            return
        if active is not None and self._resume_index is None:
            self._resume_index = self._follow_on_index(active, len(conn.asr_audio) - 1)
        if audio_have_voice and (active is None or active.error is None):
            start = self._resume_index or 0
            self._streams.append(RecognitionStream(self, list(conn.asr_audio[start:]), start))
            self._resume_index = None

    @staticmethod
    def _follow_on_index(stream: RecognitionStream, noticed_index: int) -> int:
        """Buffer index a follow-on stream starts from: RESEND_FRAMES before the frame holding the
        finished stream's reported end of utterance, else before the frame it was noticed on."""
        if stream.utterance_end_s is not None:
            end_index = stream.start_index + int(stream.utterance_end_s / FRAME_S)
            return max(min(end_index, noticed_index) - RESEND_FRAMES, stream.start_index, 0)
        return max(noticed_index - RESEND_FRAMES, 0)

    async def _send_stop_request(self):
        if self._conn is not None:
            await self._finish(self._conn)

    async def _finish(self, conn):
        frames = list(conn.asr_audio)
        streams, self._streams = self._streams, []
        conn.reset_audio_states()
        self._buffered = 0
        self._resume_index = None
        self._voice_stop_at = time.monotonic()
        for stream in streams:
            stream.half_close()
        try:
            if sum(len(frame) for frame in frames) > MIN_UTTERANCE_BYTES:
                self._pending = streams
                self._rejected = None
                await self.handle_voice_stop(conn, frames)
                if self._rejected is not None:
                    await self._ask_to_repeat(conn)
        finally:
            self._pending = None
            self._rejected = None
            for stream in streams:
                stream.cancel()

    async def _ask_to_repeat(self, conn):
        try:
            await self._speak(conn, self.repeat_reply)
        except Exception as e:
            logger.bind(tag=TAG).error(f"repeat reply failed: {type(e).__name__}: {e}")

    async def speech_to_text(
        self, opus_data: List[bytes], session_id: str, artifacts=None
    ) -> Tuple[Optional[str], Optional[str]]:
        streams, self._pending = self._pending or [], None
        stopped_at = self._voice_stop_at or time.monotonic()
        if artifacts is None or not artifacts.pcm_bytes:
            return "", None
        if streams and not streams[-1].finished:
            await asyncio.to_thread(streams[-1].wait, self.final_timeout)
        text, language, confidence, interims, problem = self._collect(streams)
        no_final = None
        if problem is None and self._truncated(streams[-1]):
            problem = "truncated"
        elif problem is None and text and self._lost_after_wake_word(text):
            problem = "wake_word_only"
        elif problem is None and not text and len(artifacts.pcm_bytes) > MIN_UTTERANCE_BYTES:
            problem, no_final = NO_FINAL, self._no_final_reason(streams)
        model = self.model
        if problem is None and text and is_chinese(text, language) and confidence < self.zh_fallback_confidence:
            problem = "zh_low_confidence"
        recognized = None
        if problem is not None and self.fallback_model and self._dropped_anyway():
            logger.bind(tag=TAG).info(f"{self.fallback_model} fallback ({problem}) skipped: utterance during playback is dropped")
        elif problem is not None and self.fallback_model:
            recognized = await self._fallback(artifacts.pcm_bytes, problem)
            if recognized is not None and (recognized[0] or problem != "zh_low_confidence"):
                text, language = recognized
                model = self.fallback_model
        rejected = problem in STREAM_PROBLEMS and recognized is None
        if rejected:
            text, model = "", "-"
            self._rejected = problem
        latency_ms = round((time.monotonic() - stopped_at) * 1000)
        if self._conn is not None:
            self._conn.weixue_stt = {"at": time.monotonic(), "latency_ms": latency_ms, "language": language}
        self.weixue_last_stt = {"model": model, "fallback": problem, "no_final": no_final, "streams": len(streams)}
        for index, stream in enumerate(streams):
            logger.bind(tag=TAG).info(f"stt_stream {index} {stream.summary()}")
        shown = "-" if confidence is None else f"{confidence:.3f}"
        logger.bind(tag=TAG).info(
            f"stt_final_latency_ms={latency_ms} model={model} language={language} "
            f"confidence={shown} fallback={problem or '-'} streams={len(streams)} interims={interims}"
            + (f" no_final={no_final}" if no_final else "")
            + (" rejected=fallback_failed" if rejected else "")
        )
        return text, None

    def _dropped_anyway(self) -> bool:
        """With barge-in off, the utterance started during a reply and gets no turn
        (weixue_patches.drops_utterance)."""
        import weixue_patches

        return self._conn is not None and weixue_patches.drops_utterance(self._conn)

    def _lost_after_wake_word(self, text: str) -> bool:
        """The transcript of a wake segment that held live voice (conn.weixue_segment_kind
        `wake`) is only a configured wake word: the request after it was not recognized."""
        conn = self._conn
        if getattr(conn, "weixue_segment_kind", None) != "wake":
            return False
        words = {_NOT_WORD.sub("", w).casefold() for w in (getattr(conn, "config", None) or {}).get("wakeup_words") or []}
        heard = _NOT_WORD.sub("", text).casefold()
        return bool(heard) and heard in words

    @staticmethod
    def _truncated(stream: RecognitionStream) -> bool:
        """The service ended the utterance more than TRUNCATED_TAIL_S before the end of the
        audio the stream was given."""
        end = stream.utterance_end_s
        return end is not None and stream.frames * FRAME_S - end > TRUNCATED_TAIL_S

    @staticmethod
    def _no_final_reason(streams: List[RecognitionStream]) -> str:
        """no_response: no stream returned a response; no_speech: responses but no interim;
        empty_final: interims but no non-empty final. `_service_end` is appended when a stream
        ended before its half-close."""
        if not any(stream.responses for stream in streams):
            reason = "no_response"
        elif not any(stream.interims for stream in streams):
            reason = "no_speech"
        else:
            reason = "empty_final"
        return reason + ("_service_end" if any(stream.service_end for stream in streams) else "")

    @staticmethod
    def _collect(streams: List[RecognitionStream]):
        """(text, language, lowest final confidence or None, interims, problem); problem names why
        the streamed result cannot be used: no_stream, stream_error, timeout."""
        texts, language, confidence, interims, problem = [], None, None, 0, None
        if not streams:
            problem = "no_stream"
        for stream in streams:
            interims += stream.interims
            if stream.error is not None:
                logger.bind(tag=TAG).error(
                    f"Google STT stream failed: {type(stream.error).__name__}: {stream.error}"
                )
                problem = problem or "stream_error"
            elif not stream.finished:
                problem = problem or "timeout"
            for transcript, code, value in list(stream.finals):
                if transcript:
                    texts.append(transcript)
                    language = code or language
                    confidence = value if confidence is None else min(confidence, value)
        text = google_stt._CJK_INNER_SPACE.sub("", " ".join(texts))
        return text, language, confidence, interims, problem

    async def _fallback(self, pcm_bytes: bytes, problem: str) -> Optional[Tuple[str, Optional[str]]]:
        if len(pcm_bytes) > google_stt.MAX_SYNC_AUDIO_BYTES:
            return None
        try:
            response = await asyncio.to_thread(
                self._recognize, pcm_bytes, self.language_codes, self.timeout, self.fallback_model
            )
        except Exception as e:
            logger.bind(tag=TAG).error(f"{self.fallback_model} fallback ({problem}) failed: {e}")
            return None
        return self._extract(response)

    async def close(self):
        self._drop_streams("close")
        await super().close()
