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
# Frames received before a finished stream was noticed that its follow-on stream resends.
RESEND_FRAMES = 2
STREAM_DEADLINE_S = google_stt.MAX_SYNC_AUDIO_BYTES / google_stt.BYTES_PER_SECOND + 15
# latest_short reports confidence 0.91 on every Mandarin final
# (docs/logs/feat-stt-streaming-accuracy-2026-10-07.md).
DEFAULT_ZH_FALLBACK_CONFIDENCE = 0.9
_HAN = re.compile(r"[一-鿿]")


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
    never block; finished is set when the response stream has ended."""

    def __init__(self, provider: "ASRProvider", frames: List[bytes]):
        self._queue: "queue.Queue[Optional[bytes]]" = queue.Queue()
        for frame in frames:
            self._queue.put(frame)
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._call = None
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

    def send(self, pcm_frame: bytes):
        if not self.half_closed:
            self._queue.put(pcm_frame)

    def half_close(self):
        if not self.half_closed:
            self.half_closed = True
            self._queue.put(None)

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
                for result in response.results:
                    if not result.alternatives:
                        continue
                    if not result.is_final:
                        self.interims += 1
                        continue
                    alternative = result.alternatives[0]
                    self.finals.append(
                        (alternative.transcript.strip(), result.language_code or None, alternative.confidence)
                    )
        except Exception as e:
            if not self.cancelled:
                self.error = e
        finally:
            self._queue.put(None)
            self._done.set()


class ASRProvider(google_stt.ASRProvider):
    """Google Cloud Speech-to-Text v2 StreamingRecognize: audio is streamed from the first voiced
    frame, the stream is half-closed when the VAD ends the utterance, and the final transcript is
    taken from the stream. A stream error or timeout, and a Chinese final below
    zh_fallback_confidence, are re-recognized with fallback_model batch Recognize on the buffered
    utterance."""

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
        self._conn = None
        self._client_lock = threading.Lock()
        self._streams: List[RecognitionStream] = []
        self._buffered = 0
        self._resume_index: Optional[int] = None
        self._pending: Optional[List[RecognitionStream]] = None
        self._voice_stop_at: Optional[float] = None

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

    def _drop_streams(self):
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
            self._drop_streams()
        self._buffered = len(conn.asr_audio)
        over_limit = sum(len(frame) for frame in conn.asr_audio) > google_stt.MAX_SYNC_AUDIO_BYTES
        if over_limit:
            self._drop_streams()
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
            self._resume_index = max(len(conn.asr_audio) - 1 - RESEND_FRAMES, 0)
        if audio_have_voice and (active is None or active.error is None):
            frames = conn.asr_audio[self._resume_index or 0 :]
            self._streams.append(RecognitionStream(self, list(frames)))
            self._resume_index = None

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
                await self.handle_voice_stop(conn, frames)
        finally:
            self._pending = None
            for stream in streams:
                stream.cancel()

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
        model = self.model
        if problem is None and text and is_chinese(text, language) and confidence < self.zh_fallback_confidence:
            problem = "zh_low_confidence"
        if problem is not None and self.fallback_model:
            recognized = await self._fallback(artifacts.pcm_bytes, problem)
            if recognized is not None and (recognized[0] or problem != "zh_low_confidence"):
                text, language = recognized
                model = self.fallback_model
        latency_ms = round((time.monotonic() - stopped_at) * 1000)
        shown = "-" if confidence is None else f"{confidence:.3f}"
        logger.bind(tag=TAG).info(
            f"stt_final_latency_ms={latency_ms} model={model} language={language} "
            f"confidence={shown} fallback={problem or '-'} streams={len(streams)} interims={interims}"
        )
        return text, None

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
        self._drop_streams()
        await super().close()
