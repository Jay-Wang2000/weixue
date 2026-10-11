import os
import re
import time
import queue
import threading
import traceback

from google.cloud import texttospeech
from google.api_core import exceptions as gexc

import weixue_patches
from config.logger import setup_logging
from config.config_loader import get_project_dir
from core.utils import textUtils
from core.utils.tts import MarkdownCleaner
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import SentenceType, ContentType, InterfaceType

TAG = __name__
logger = setup_logging()

_CJK = re.compile(r"[一-鿿㐀-䶿　-〿＀-￯]")
_LATIN = re.compile(r"[A-Za-z]")
# Google Text-to-Speech accepts at most 5,000 bytes of input per request.
_MAX_REQUEST_BYTES = 5000
_REQUEST_BREAKS = (
    re.compile(r"[。？！；?!;\n]|[.…](?=\s)"),
    re.compile(r"[，,、：:]"),
    re.compile(r"\s"),
)
# Streamed text with no sentence end is sent once it exceeds this many characters.
_MAX_PENDING_CHARS = 250
_ENGLISH_SENTENCE_END = re.compile(r"[.…]+[\"'”’)\]]*(?=\s)")
_LAST_WORD = re.compile(r"(\S+)$")
_ABBREVIATIONS = frozenset(
    "mr mrs ms dr prof sr jr st mt ft vs etc approx e.g i.e a.m p.m u.s u.k inc ltd "
    "jan feb mar apr jun jul aug sep sept oct nov dec fig".split()
)
_CANCEL_POLL_S = 0.05
# First segment of a reply (first_clause): cut at the earliest clause or sentence mark whose prefix holds
# FIRST_CLAUSE_WORDS English words or FIRST_CLAUSE_HAN Chinese characters; without one, once the text holds
# FIRST_CUT_WORDS words (cut at the last word boundary) or FIRST_CUT_HAN characters.
FIRST_CLAUSE_WORDS = 2
FIRST_CLAUSE_HAN = 4
FIRST_CUT_WORDS = 8
FIRST_CUT_HAN = 12
_CLAUSE_MARKS = frozenset("，,、：:；;。.!?！？…—")
_MARK_RUN = frozenset(".!?…。！？")
_CLOSERS = frozenset("\"'”’)]）」』")
_DIGIT_MARKS = frozenset(".,:")
_HAN = re.compile(r"[\u3400-\u9fff]")
_WORD = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)*")
_WORD_THEN_SPACE = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)*(?=\s)")


def _english_sentence_end(text):
    """Index just past the last English sentence end in text that is followed by whitespace,
    skipping abbreviations and single-letter initials; 0 when there is none."""
    end = 0
    for match in _ENGLISH_SENTENCE_END.finditer(text):
        word = _LAST_WORD.search(text[: match.start()])
        token = word.group(1).lstrip("([\"'“‘").lower() if word else ""
        if token in _ABBREVIATIONS or (len(token) == 1 and token.isalpha()):
            continue
        end = match.end()
    return end


def _abbreviation_before(text, i):
    word = _LAST_WORD.search(text[:i])
    token = word.group(1).lstrip("([\"'“‘").lower() if word else ""
    return token in _ABBREVIATIONS or (len(token) == 1 and token.isalpha())


def _holds(prefix, words, han):
    return len(_WORD.findall(prefix)) >= words or len(_HAN.findall(prefix)) >= han


def first_clause_cut(text):
    """Index just past the first segment of a reply in text (the first_clause rule), 0 when it is not
    complete yet. A `.` counts when whitespace, a closer or the end of the text follows it and it does
    not end an abbreviation or initial; `.`, `,` and `:` after a digit never count before a non-digit
    follows."""
    for i, ch in enumerate(text):
        if ch not in _CLAUSE_MARKS:
            continue
        prev = text[i - 1] if i else ""
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if ch in _DIGIT_MARKS and prev.isdigit() and (nxt == "" or nxt.isdigit()):
            continue
        if ch == "." and ((nxt and not nxt.isspace() and nxt not in _CLOSERS and nxt != ".")
                          or _abbreviation_before(text, i)):
            continue
        end = i + 1
        while end < len(text) and (text[end] in _MARK_RUN or text[end] in _CLOSERS):
            end += 1
        if _holds(text[:end], FIRST_CLAUSE_WORDS, FIRST_CLAUSE_HAN):
            return end
    if len(_HAN.findall(text)) >= FIRST_CUT_HAN:
        return len(text)
    if len(_WORD_THEN_SPACE.findall(text)) >= FIRST_CUT_WORDS:
        return _word_cut(text, len(text))
    return 0


def _word_cut(text, limit):
    """Index at which to cut text of more than limit characters: after the last whitespace
    within the first limit characters, else at limit."""
    space = max(text.rfind(" ", 0, limit), text.rfind("\n", 0, limit))
    return space + 1 if space > 0 else limit


def _request_cut(text, limit):
    """Index of the longest prefix of text within limit UTF-8 bytes that ends at the latest
    sentence end, else clause mark, else whitespace, else at a character boundary."""
    fit = len(text.encode("utf-8")[:limit].decode("utf-8", "ignore"))
    head = text[:fit]
    for pattern in _REQUEST_BREAKS:
        ends = [m.end() for m in pattern.finditer(head)]
        if ends and ends[-1] > 0:
            return ends[-1]
    return fit


def _no_first_response_prefetch(client):
    """Makes client.streaming_synthesize() return its call before the first response arrives,
    so the call can be cancelled while it waits."""
    stub = getattr(getattr(client, "transport", None), "streaming_synthesize", None)
    if stub is not None and getattr(stub, "_prefetch_first_result_", True):
        stub._prefetch_first_result_ = False


class _CancelWatch:
    """Cancels a streaming call from a helper thread as soon as stale() holds, whether or not
    a response is pending; cancelled records that the cancellation was ours."""

    def __init__(self, call, stale, interval=_CANCEL_POLL_S):
        self._call = call
        self._stale = stale
        self._interval = interval
        self._done = threading.Event()
        self._lock = threading.Lock()
        self.cancelled = False
        threading.Thread(target=self._run, name="google-tts-cancel", daemon=True).start()

    def _run(self):
        while not self._done.wait(self._interval):
            if self._stale():
                self.cancel()
                return

    def cancel(self):
        with self._lock:
            if self._done.is_set():
                return
            self.cancelled = True
            self._done.set()
        try:
            self._call.cancel()
        except Exception as e:
            logger.bind(tag=TAG).warning(f"Google TTS cancel failed: {type(e).__name__}: {e}")

    def stop(self):
        with self._lock:
            self._done.set()


class TTSProvider(TTSProviderBase):
    # StreamingAudioConfig exposes speaking_rate only (no pitch, no volume).
    TTS_PARAM_CONFIG = [
        ("ttsRate", "speaking_rate", 0.25, 2.0, 1.0, lambda v: round(float(v), 2)),
    ]

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.interface_type = InterfaceType.SINGLE_STREAM
        self.audio_file_type = "wav"
        self.speaker = config.get("speaker") or "Charon"
        self.voice_en = config.get("voice_en") or f"en-US-Chirp3-HD-{self.speaker}"
        self.voice_zh = config.get("voice_zh") or f"cmn-CN-Chirp3-HD-{self.speaker}"
        self.default_language = (config.get("default_language") or "en").lower()
        self.voice = config.get("private_voice") or (
            self.voice_zh if self.default_language == "zh" else self.voice_en
        )
        self.speaking_rate = float(config.get("speaking_rate") or 1.0)
        self.first_clause = str(config.get("first_clause", True)).lower() not in ("false", "0", "no", "off")
        self._apply_percentage_params(config)
        self.sample_rate = 24000
        self.credentials_file = config.get("credentials_file")
        self._client = None
        self.pcm_buffer = bytearray()
        self._active_watch = None
        self.last_first_audio_latency = None

    @property
    def client(self):
        if self._client is None:
            if self.credentials_file:
                path = self.credentials_file
                if not os.path.isabs(path):
                    path = os.path.join(get_project_dir(), path)
                if not os.path.exists(path):
                    raise FileNotFoundError(
                        f"Google TTS credentials_file not found: {self.credentials_file}"
                    )
                self._client = texttospeech.TextToSpeechClient.from_service_account_file(path)
            else:
                self._client = texttospeech.TextToSpeechClient()
        return self._client

    def _select_voice(self, text):
        if _CJK.search(text):
            return self.voice_zh, "cmn-CN"
        if _LATIN.search(text):
            return self.voice_en, "en-US"
        if self.default_language == "zh":
            return self.voice_zh, "cmn-CN"
        return self.voice_en, "en-US"

    def _voice_params(self, text):
        name, lang = self._select_voice(text)
        return texttospeech.VoiceSelectionParams(name=name, language_code=lang)

    async def open_audio_channels(self, conn):
        await super().open_audio_channels(conn)
        self.sample_rate = conn.sample_rate

    async def close(self):
        watch = self._active_watch
        if watch is not None:
            watch.cancel()
        await super().close()
        encoder = getattr(self, "opus_encoder", None)
        if encoder is not None and hasattr(encoder, "close"):
            encoder.close()
        if self._client is not None:
            try:
                self._client.transport.close()
            except Exception:
                pass
            self._client = None

    def tts_text_priority_thread(self):
        while not self.conn.stop_event.is_set():
            try:
                message = self.tts_text_queue.get(timeout=1)
                if self.conn.client_abort:
                    continue
                if message.sentence_id != self.conn.sentence_id:
                    continue
                if message.sentence_type == SentenceType.FIRST:
                    self.current_sentence_id = message.sentence_id
                    self.tts_stop_request = False
                    self.processed_chars = 0
                    self.tts_text_buff = []
                    self.before_stop_play_files.clear()
                    self.is_first_sentence = True
                    self.tts_audio_first_sentence = True
                    self.pcm_buffer.clear()
                    self.opus_encoder.reset_state()
                elif message.content_type == ContentType.TEXT:
                    self.tts_text_buff.append(message.content_detail)
                    segment_text = self._get_segment_text()
                    if segment_text:
                        self.to_tts_single_stream(segment_text)
                elif message.content_type == ContentType.FILE:
                    if message.content_file and os.path.exists(message.content_file):
                        self._process_audio_file_stream(
                            message.content_file,
                            callback=lambda audio: self.handle_audio_file(
                                audio, message.content_detail
                            ),
                        )
                if message.sentence_type == SentenceType.LAST:
                    self._process_remaining_text_stream(True)
            except queue.Empty:
                continue
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"处理TTS文本失败: {e}, 类型: {type(e).__name__}, 堆栈: {traceback.format_exc()}"
                )

    def _process_remaining_text_stream(self, is_last=False):
        full_text = "".join(self.tts_text_buff)
        remaining_text = full_text[self.processed_chars:]
        if remaining_text:
            segment_text = textUtils.get_string_no_punctuation_or_emoji(remaining_text)
            if segment_text:
                self.to_tts_single_stream(segment_text, is_last)
                self.processed_chars += len(full_text)
                return
        if is_last:
            self._process_before_stop_play_files()

    def _get_segment_text(self):
        if self.first_clause and self.is_first_sentence:
            current = "".join(self.tts_text_buff)[self.processed_chars :]
            cut = first_clause_cut(current)
            if not cut:
                return None
            self.processed_chars += cut
            self.is_first_sentence = False
            return textUtils.get_string_no_punctuation_or_emoji(current[:cut])
        segment = super()._get_segment_text()
        if segment is not None:
            return segment
        current = "".join(self.tts_text_buff)[self.processed_chars :]
        cut = _english_sentence_end(current)
        if not cut and len(current) > _MAX_PENDING_CHARS:
            cut = _word_cut(current, _MAX_PENDING_CHARS)
        if not cut:
            return None
        self.processed_chars += cut
        self.is_first_sentence = False
        return textUtils.get_string_no_punctuation_or_emoji(current[:cut])

    def to_tts_single_stream(self, text, is_last=False):
        original_text = text
        try:
            text = MarkdownCleaner.clean_markdown(text)
            if self._correct_words_pattern:
                text = self._correct_words_pattern.sub(
                    lambda m: self.correct_words[m.group(0)], text
                )
            chunks = self._split_request(text)
            if not chunks:
                if is_last:
                    self._process_before_stop_play_files()
                return
            for i, chunk in enumerate(chunks):
                last = is_last and i == len(chunks) - 1
                if not self._stream_segment(chunk, last, emit_first=(i == 0)):
                    return
        except Exception as e:
            logger.bind(tag=TAG).error(f"Failed to generate TTS stream: {original_text} {e}")

    @staticmethod
    def _split_request(text):
        chunks = []
        while len(text.encode("utf-8")) > _MAX_REQUEST_BYTES:
            cut = _request_cut(text, _MAX_REQUEST_BYTES)
            chunks.append(text[:cut])
            text = text[cut:]
        if text:
            chunks.append(text)
        return chunks

    def _streaming_requests(self, text):
        yield texttospeech.StreamingSynthesizeRequest(
            streaming_config=texttospeech.StreamingSynthesizeConfig(
                voice=self._voice_params(text),
                streaming_audio_config=texttospeech.StreamingAudioConfig(
                    audio_encoding=texttospeech.AudioEncoding.PCM,
                    sample_rate_hertz=self.sample_rate,
                    speaking_rate=self.speaking_rate,
                ),
            )
        )
        yield texttospeech.StreamingSynthesizeRequest(
            input=texttospeech.StreamingSynthesisInput(text=text)
        )

    def _start_stream(self, text):
        client = self.client
        _no_first_response_prefetch(client)
        return client.streaming_synthesize(self._streaming_requests(text), timeout=self.tts_timeout)

    def _stream_segment(self, text, is_last, emit_first=True):
        """Stream one request; returns False when the sentence went stale (abort or newer turn)."""
        encoder = self.opus_encoder
        frame_bytes = encoder.sample_rate * encoder.channels * encoder.frame_size_ms // 1000 * 2
        sid = self.current_sentence_id
        started = time.monotonic()
        first_frame_at = []

        def on_opus(opus_data):
            if not first_frame_at:
                first_frame_at.append(time.monotonic())
                self.last_first_audio_latency = first_frame_at[0] - started
                logger.bind(tag=TAG).info(
                    f"Google TTS first audio after {self.last_first_audio_latency:.3f}s: {text}"
                )
            self.handle_opus(opus_data)

        def stale():
            return self.conn.client_abort or self.conn.sentence_id != sid

        self.pcm_buffer.clear()
        if emit_first:
            self.tts_audio_queue.put((SentenceType.FIRST, [], text, sid))
        watch = None
        try:
            call = self._start_stream(text)
            watch = self._active_watch = _CancelWatch(call, stale)
            for response in call:
                if stale():
                    watch.cancel()
                    logger.bind(tag=TAG).info(f"Google TTS stream cancelled (abort or newer turn): {text}")
                    return False
                self.pcm_buffer.extend(response.audio_content)
                while len(self.pcm_buffer) >= frame_bytes:
                    frame = bytes(self.pcm_buffer[:frame_bytes])
                    del self.pcm_buffer[:frame_bytes]
                    encoder.encode_pcm_to_opus_stream(frame, end_of_stream=False, callback=on_opus)
            if stale():
                return False
            if len(self.pcm_buffer) % 2:
                self.pcm_buffer.pop()
            if self.pcm_buffer:
                encoder.encode_pcm_to_opus_stream(
                    bytes(self.pcm_buffer), end_of_stream=True, callback=on_opus
                )
                self.pcm_buffer.clear()
            if not first_frame_at:
                logger.bind(tag=TAG).warning(f"Google TTS returned no audio: {text}")
            if is_last:
                self._process_before_stop_play_files()
            return True
        except gexc.Cancelled as e:
            if (watch is not None and watch.cancelled) or stale():
                logger.bind(tag=TAG).info(f"Google TTS stream cancelled (abort or newer turn): {text}")
                return False
            logger.bind(tag=TAG).error(f"Google TTS stream cancelled by the service: {e}")
        except gexc.InvalidArgument as e:
            logger.bind(tag=TAG).error(
                f"Google TTS rejected request (voice/sample_rate_hertz): {e}"
            )
        except Exception as e:
            logger.bind(tag=TAG).error(f"Google TTS stream failed: {type(e).__name__}: {e}")
        finally:
            if watch is not None:
                watch.stop()
            self._active_watch = None
            self.pcm_buffer.clear()
        weixue_patches.note_synthesis_failure(self.conn, sid)
        if is_last:
            self.tts_audio_queue.put((SentenceType.LAST, [], None, sid))
        return True

    async def text_to_speak(self, text, output_file):
        response = self.client.synthesize_speech(
            input=texttospeech.SynthesisInput(text=text),
            voice=self._voice_params(text),
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.LINEAR16,
                sample_rate_hertz=self.sample_rate,
                speaking_rate=self.speaking_rate,
            ),
            timeout=self.tts_timeout,
        )
        if output_file:
            with open(output_file, "wb") as f:
                f.write(response.audio_content)
            return None
        return response.audio_content
