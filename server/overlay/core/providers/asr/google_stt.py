import os
import re
import time
import uuid
import asyncio
from typing import List, Optional, Sequence, Tuple

import google.auth
from google.api_core.client_options import ClientOptions
from google.api_core.exceptions import InvalidArgument
from google.auth.exceptions import DefaultCredentialsError
from google.cloud.speech_v2 import SpeechClient
from google.cloud.speech_v2.types import cloud_speech
from google.oauth2 import service_account

from config.logger import setup_logging
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType
from core.handle.sendAudioHandle import send_tts_message
from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO

TAG = __name__
logger = setup_logging()

SAMPLE_RATE_HZ = 16000
BYTES_PER_SECOND = SAMPLE_RATE_HZ * 2
MAX_SYNC_AUDIO_BYTES = 59 * BYTES_PER_SECOND  # Recognize accepts at most 60 s
DEFAULT_LANGUAGE_CODES = ("en-US", "cmn-Hans-CN")
AUTO_LANGUAGE_CODES = ("auto",)
DEFAULT_TOO_LONG_REPLY = (
    "Sorry, that was too long for me. Please say it again, a bit shorter.\n"
    "抱歉，这段话太长了，请说短一点再试一次。"
)
_LANGUAGE_REJECTION = re.compile(r"^Bad language code|^The language .* is not supported by the model")
_CJK = r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]"
_CJK_INNER_SPACE = re.compile(rf"(?<={_CJK})\s+(?={_CJK})")


def is_language_rejection(error: InvalidArgument) -> bool:
    """True when Recognize rejected the language_codes list, by field violation or message."""
    for detail in getattr(error, "details", None) or []:
        for violation in getattr(detail, "field_violations", None) or []:
            if str(getattr(violation, "field", "")).split(".")[-1] == "language_codes":
                return True
    return bool(_LANGUAGE_REJECTION.search(str(getattr(error, "message", "") or "")))


def cap_buffered_audio(frames: List[bytes]) -> None:
    """Drops the frames after the one that takes frames past MAX_SYNC_AUDIO_BYTES; the
    remaining buffer still exceeds the limit, so its voice stop is rejected as too long."""
    total = 0
    for i, frame in enumerate(frames):
        total += len(frame)
        if total > MAX_SYNC_AUDIO_BYTES:
            del frames[i + 1 :]
            return


class ASRProvider(ASRProviderBase):
    """Google Cloud Speech-to-Text v2, synchronous Recognize on 16 kHz mono LINEAR16."""

    def __init__(self, config: dict, delete_audio_file: bool):
        super().__init__()
        self.interface_type = InterfaceType.NON_STREAM
        self.credentials_file = config.get("credentials_file") or os.environ.get(
            "GOOGLE_APPLICATION_CREDENTIALS"
        )
        self.use_adc = str(config.get("use_adc", False)).lower() in ("true", "1", "yes")
        self.project_id = config.get("project_id") or None
        self.location = config.get("location") or "us"
        self.model = config.get("model") or "chirp_3"
        self.language_codes = list(config.get("language_codes") or DEFAULT_LANGUAGE_CODES)
        self.enable_automatic_punctuation = str(
            config.get("enable_automatic_punctuation", True)
        ).lower() in ("true", "1", "yes")
        self.timeout = float(config.get("timeout", 15))
        self.output_dir = config.get("output_dir") or "tmp/"
        self.too_long_reply = config.get("too_long_reply") or DEFAULT_TOO_LONG_REPLY
        self.delete_audio_file = delete_audio_file
        self._client: Optional[SpeechClient] = None
        os.makedirs(self.output_dir, exist_ok=True)

    def _load_credentials(self):
        if self.credentials_file:
            creds = service_account.Credentials.from_service_account_file(
                self.credentials_file
            )
            return creds, creds.project_id
        if self.use_adc:
            return google.auth.default()
        raise DefaultCredentialsError(
            "GoogleSTT: set credentials_file (or GOOGLE_APPLICATION_CREDENTIALS) or use_adc: true"
        )

    def _get_client(self) -> SpeechClient:
        if self._client is None:
            creds, project_id = self._load_credentials()
            self.project_id = self.project_id or project_id
            if not self.project_id:
                raise ValueError("GoogleSTT: project_id unknown; set it in the config")
            options = ClientOptions()
            if self.location != "global":
                options.api_endpoint = f"{self.location}-speech.googleapis.com"
            self._client = SpeechClient(credentials=creds, client_options=options)
        return self._client

    def _recognizer_path(self) -> str:
        return f"projects/{self.project_id}/locations/{self.location}/recognizers/_"

    def _build_config(
        self, language_codes: Sequence[str], model: Optional[str] = None
    ) -> cloud_speech.RecognitionConfig:
        return cloud_speech.RecognitionConfig(
            explicit_decoding_config=cloud_speech.ExplicitDecodingConfig(
                encoding=cloud_speech.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=SAMPLE_RATE_HZ,
                audio_channel_count=1,
            ),
            language_codes=list(language_codes),
            model=model or self.model,
            features=cloud_speech.RecognitionFeatures(
                enable_automatic_punctuation=self.enable_automatic_punctuation
            ),
        )

    def _recognize(
        self,
        pcm_bytes: bytes,
        language_codes: Sequence[str],
        timeout: float,
        model: Optional[str] = None,
    ) -> cloud_speech.RecognizeResponse:
        client = self._get_client()
        request = cloud_speech.RecognizeRequest(
            recognizer=self._recognizer_path(),
            config=self._build_config(language_codes, model),
            content=pcm_bytes,
        )
        return client.recognize(request=request, timeout=timeout, retry=None)

    @staticmethod
    def _extract(response: cloud_speech.RecognizeResponse) -> Tuple[str, Optional[str]]:
        transcripts = []
        language_code = None
        for result in response.results:
            if not result.alternatives:
                continue
            transcripts.append(result.alternatives[0].transcript.strip())
            language_code = result.language_code or language_code
        text = " ".join(t for t in transcripts if t)
        return _CJK_INNER_SPACE.sub("", text), language_code

    async def speech_to_text(
        self, opus_data: List[bytes], session_id: str, artifacts=None
    ) -> Tuple[Optional[str], Optional[str]]:
        if artifacts is None or not artifacts.pcm_bytes:
            return "", None
        pcm_bytes = artifacts.pcm_bytes
        if len(pcm_bytes) > MAX_SYNC_AUDIO_BYTES:
            self._log_too_long(len(pcm_bytes))
            return "", None
        start_time = time.monotonic()
        try:
            try:
                response = await asyncio.to_thread(
                    self._recognize, pcm_bytes, self.language_codes, self.timeout
                )
            except InvalidArgument as e:
                remaining = self.timeout - (time.monotonic() - start_time)
                if (
                    list(self.language_codes) == list(AUTO_LANGUAGE_CODES)
                    or not is_language_rejection(e)
                    or remaining <= 0
                ):
                    raise
                logger.bind(tag=TAG).warning(
                    f"language_codes={self.language_codes} 被拒绝（{e.message}），改用 auto 重试"
                )
                response = await asyncio.to_thread(
                    self._recognize, pcm_bytes, AUTO_LANGUAGE_CODES, remaining
                )
        except Exception as e:
            logger.bind(tag=TAG).error(f"语音识别失败: {e}")
            return "", None
        text, language_code = self._extract(response)
        logger.bind(tag=TAG).info(
            f"语音识别耗时: {time.monotonic() - start_time:.3f}s | 语言: {language_code} | 结果: {text}"
        )
        return text, None

    @staticmethod
    def _log_too_long(num_bytes: int):
        logger.bind(tag=TAG).warning(
            f"音频 {num_bytes / BYTES_PER_SECOND:.1f}s 超过同步识别上限 "
            f"{MAX_SYNC_AUDIO_BYTES // BYTES_PER_SECOND}s，已拒绝"
        )

    async def receive_audio(self, conn, pcm_frame, audio_have_voice):
        await super().receive_audio(conn, pcm_frame, audio_have_voice)
        cap_buffered_audio(conn.asr_audio)

    async def handle_voice_stop(self, conn, asr_audio_task: List[bytes]):
        """Utterances over the Recognize limit are not transcribed; the device speaks too_long_reply."""
        num_bytes = sum(len(chunk) for chunk in asr_audio_task)
        if num_bytes <= MAX_SYNC_AUDIO_BYTES:
            await super().handle_voice_stop(conn, asr_audio_task)
            return
        self._log_too_long(num_bytes)
        try:
            await self._speak(conn, self.too_long_reply)
        except Exception as e:
            logger.bind(tag=TAG).error(f"超长提示播报失败: {e}")

    @staticmethod
    async def _speak(conn, text: str):
        conn.client_abort = False
        sentence_id = conn.sentence_id = uuid.uuid4().hex
        await send_tts_message(conn, "start")
        conn.client_is_speaking = True
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=sentence_id,
                sentence_type=SentenceType.FIRST,
                content_type=ContentType.ACTION,
            )
        )
        conn.tts.tts_one_sentence(
            conn, ContentType.TEXT, content_detail=text, sentence_id=sentence_id
        )
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=sentence_id,
                sentence_type=SentenceType.LAST,
                content_type=ContentType.ACTION,
            )
        )

    async def close(self):
        client, self._client = self._client, None
        if client is not None:
            client.transport.close()
