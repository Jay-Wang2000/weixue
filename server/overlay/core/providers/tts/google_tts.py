import os
import re
import time
import queue
import traceback

from google.cloud import texttospeech
from google.api_core import exceptions as gexc

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
# Google Text-to-Speech caps one request at 5,000 bytes of input.
_MAX_REQUEST_BYTES = 4500
_SPLIT_PUNCT = re.compile(r"(?<=[。？！；：?!;:,，])")


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
        self._apply_percentage_params(config)
        self.sample_rate = 24000
        self.credentials_file = config.get("credentials_file")
        self._client = None
        self.pcm_buffer = bytearray()
        self._active_call = None
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
        if len(text.encode("utf-8")) <= _MAX_REQUEST_BYTES:
            return [text] if text else []
        chunks, current = [], ""
        for piece in _SPLIT_PUNCT.split(text):
            if current and len((current + piece).encode("utf-8")) > _MAX_REQUEST_BYTES:
                chunks.append(current)
                current = ""
            current += piece
        if current:
            chunks.append(current)
        return [c for c in chunks if c]

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

    def _stream_segment(self, text, is_last, emit_first=True):
        """Stream one request; returns False when the sentence went stale."""
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
        try:
            call = self.client.streaming_synthesize(
                self._streaming_requests(text), timeout=self.tts_timeout
            )
            self._active_call = call
            for response in call:
                if stale():
                    call.cancel()
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
        except gexc.Cancelled:
            return False
        except gexc.InvalidArgument as e:
            logger.bind(tag=TAG).error(
                f"Google TTS rejected request (voice/sample_rate_hertz): {e}"
            )
        except Exception as e:
            logger.bind(tag=TAG).error(f"Google TTS stream failed: {type(e).__name__}: {e}")
        finally:
            self._active_call = None
            self.pcm_buffer.clear()
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
