# Developer brief — feat-tts-google (revision 2)

Upstream read-only at `/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32-server` (commit `87c6df7`, 2026-09-29, "Merge pull request #3386 from xinnan-tech/0.9.7"). Server paths below are relative to `main/xiaozhi-server/` unless they start with `Dockerfile`/`.gitignore`/`docker-compose`. Wheel facts come from `google-cloud-texttospeech==2.37.0` installed into a scratch venv (resolved with `google-api-core 2.41.0`, `grpcio 1.84.0`); wheel paths are relative to `site-packages/google/`. Doc facts were fetched 2026-10-06. Work happens in the fork (`feat/tts-google`), never in the vendor copy.

## 1. Where the provider plugs in

| Concern | Location | What it does |
|---|---|---|
| Provider discovery | `core/utils/tts.py:33-41` | `os.path.exists(os.path.join('core','providers','tts', f'{class_name}.py'))` relative to **CWD**, then `importlib.import_module`, then `module.TTSProvider(*args, **kwargs)`. File name == `type` value. Run pytest and `app.py` from `main/xiaozhi-server/`, or you get `不支持的TTS类型: google_tts`. |
| Module init | `core/utils/modules_initialize.py:101-115` | `tts_config = config["TTS"][selected].copy()`; `setdefault("tts_timeout", config.get("tts_timeout", 15))`; `type` falls back to the section name; `delete_audio` (default `True`) becomes the `delete_audio_file` bool. |
| Lifecycle | `core/connection.py:606-613` (`_initialize_tts()` then `open_audio_channels(self)` scheduled on `conn.loop`), `core/connection.py:1640-1641` (`await self.tts.close()`) | One provider instance per connection; **the constructor runs at connection setup.** |
| Output sample rate | `core/connection.py:113` default 24000; `core/connection.py:242-246` `welcome_msg = config["xiaozhi"]` then `self.sample_rate = welcome_msg["audio_params"]["sample_rate"]`; `config.yaml:92-97` sets 24000 / 60 ms | `conn.sample_rate` is **config-only**. `helloHandle.py:44-49` copies the device's `audio_params` into `conn.audio_format` and `conn.welcome_msg` after L246 has run; grep of `core/handle` + `core/connection.py` finds no other writer. The comment at `connection.py:113` ("从客户端 hello 消息中动态更新") is misleading. |
| Timeout | `config.yaml:63` `tts_timeout: 15` | Pass as `timeout=` to both client calls. |

**Files to add / change in the fork**
1. `core/providers/tts/google_tts.py` — new provider (`type: google_tts`). Do not name it `google.py`.
2. `config.yaml` — `GoogleTTS:` section under `TTS:` (template in §5); `selected_module.TTS: GoogleTTS` in `data/.config.yaml` (gitignored: `.gitignore:156` `main/xiaozhi-server/data`).
3. `requirements.txt` — add `google-cloud-texttospeech==2.37.0` (`requirements.txt:15` pins `google-generativeai==0.8.5`; see pitfall 1).
4. Docker — see §7.
5. `tests/test_tts_google.py` — new mock + live pytest (see pitfall 13).

## 2. Base-class contract (`core/providers/tts/base.py`)

- Abstract `async def text_to_speak(self, text, output_file)` (L263-265). Base callers: `to_tts_stream` (L126-149) and `to_tts` (L196-261).
- **`to_tts(text)` return value depends on `delete_audio_file`** (L203-258): with `delete_audio_file=True` it calls `text_to_speak(text, None)` and decodes the returned bytes with `audio_bytes_to_data_stream(..., file_type=self.audio_file_type, is_opus=True, sample_rate=self.conn.sample_rate)` (L207-217) and returns `list[bytes]` of Opus frames; with `delete_audio_file=False` it calls `text_to_speak(text, tmp_file)` and **returns the file path** (L235-258). Both branches dereference `self.conn` (L215, and `to_tts_stream` at L147), so `to_tts` raises `AttributeError` before `open_audio_channels` unless a stub `conn` with `.sample_rate` is set, as `tests/test_tts.py:98-105` does. `to_tts` passes no `opus_encoder`, so `util.pcm_to_data_stream` (`core/utils/util.py:357-360`) builds a throw-away `opuslib_next.Encoder`.
- `audio_bytes_to_data_stream` (`util.py:327-343`) decodes non-p3 bytes with pydub `AudioSegment.from_file(BytesIO(...), format=file_type)`, i.e. it needs the `ffmpeg` binary. `audio_file_type = "wav"` matches Google `LINEAR16` ("Audio content returned as LINEAR16 also contains a WAV header", wheel `cloud/texttospeech_v1/types/cloud_tts.py:90-93`).
- Wake-word cache: `core/handle/helloHandle.py:144-152` runs `conn.tts.to_tts(result)` in a thread, then `opus_datas_to_wav_bytes(tts_result, sample_rate=conn.sample_rate)`, keyed by `getattr(conn.tts, "voice", "default")` (L149) — expose `self.voice`.
- `open_audio_channels(conn)` (L307-326) stores `self.conn`, creates `self.opus_encoder = OpusEncoderUtils(sample_rate=conn.sample_rate, channels=1, frame_size_ms=60)` only if the subclass has not set one (L311-314), starts `tts_text_priority_thread` and `_audio_play_priority_thread`.
- `handle_opus(opus_bytes)` (L119-121) puts `(SentenceType.MIDDLE, opus_data, None, getattr(self,'current_sentence_id',None))` on `tts_audio_queue` — one `bytes` frame per call.
- `_audio_play_priority_thread` (L414-472): accepts 3- or 4-tuples (L423-427), drops items while `conn.client_abort` (L433-436), reports per sentence unless `self.report_on_last` (L439-454; default `False` at L48), appends `bytes` items to the report buffer (L457-458), then `sendAudioMessage(conn, sentence_type, audio_datas, text, sentence_id)` (L461-465). Keep `report_on_last=False`: it is correct for one stream per segment (providers that set it `True` are single-stream-per-turn: `aliyun_stream.py:107`, `xunfei_stream.py:79`).
- Base `tts_text_priority_thread` (L371-387): skips messages when `conn.client_abort` (L375-377) and when `message.sentence_id != self.conn.sentence_id` (L378-380); on `FIRST` sets `current_sentence_id`, `tts_stop_request=False`, `processed_chars=0`, `tts_text_buff=[]`, `is_first_sentence=True`, `tts_audio_first_sentence=True` (L381-387).
- `_get_segment_text()` (L486-523) uses `first_sentence_punctuations` while `is_first_sentence` (L493-497) and clears the flag after the first split (L514-515); `_process_before_stop_play_files()` (L549-553) queues deferred files then `(LAST, [], None, current_sentence_id)`; `_apply_percentage_params` (L573-578) maps `TTS_PARAM_CONFIG` keys through `convert_percentage_to_range(percentage, min, max, base)` (`core/utils/tts.py:146-170`: -100..100 interpolated linearly from `base` to `min`/`max`, clamped).
- `close()` (L480-484) clears `_sentence_text_map` and closes `self.ws` if present.
- Encoder `core/utils/opus_encoder_utils.py`: ctor L16-50 (bitrate 24000, complexity 10, `SIGNAL_VOICE`); `reset_state()` L52-55 resets encoder and internal `self.buffer`; `encode_pcm_to_opus_stream(pcm, end_of_stream, callback)` L57-100 appends to `self.buffer`, emits whole frames, keeps the remainder (L89), and on `end_of_stream` zero-pads the tail and empties the buffer (L92-100) **without calling `reset_state()`**; `_convert_bytes_to_shorts` is `np.frombuffer(dtype=int16)` (L118-121) so an odd byte count raises `ValueError`; `_validate_pcm_data` only logs (L123-130).
- Message DTOs `core/providers/tts/dto/dto.py:5-43`: `SentenceType` FIRST/MIDDLE/LAST, `ContentType` TEXT/FILE/ACTION, `InterfaceType`, `TTSMessageDTO(sentence_id, sentence_type, content_type, content_detail=None, content_file=None)`.
- Producers: `connection.py:1062-1073` FIRST/ACTION with a fresh `sentence_id` (`conn.sentence_id` updated at L1065), `1213-1220` MIDDLE/TEXT deltas, `1232-1238` and `1380-1386` LAST/ACTION; `intentHandler.py:237-251` FIRST → `tts_one_sentence` → LAST.
- Send side (`core/handle/sendAudioHandle.py`): `AUDIO_FRAME_DURATION = 60` (L16); `sendAudioMessage` (L21-55) drops `sentence_id is not None and sentence_id != conn.sentence_id` (L22-24) — so a `None` id **bypasses** the filter; logs and clears `conn.tts.tts_audio_first_sentence` (L26-28); emits `sentence_start` on FIRST (L30-43); sends `tts stop` on LAST **only when `not conn.calling`** (L51-53; `conn.calling` initialised `False` at `connection.py:199`). `sendAudio` (L119-140) treats `isinstance(audios, bytes)` as a single packet (L134) and a list otherwise — call `handle_opus` with one 60 ms frame, never a list.

## 3. Abort semantics the stream loop must tolerate

`core/handle/abortHandle.py:9-20`: sets `conn.client_abort = True` (L13), `conn.clear_queues()` (L14) which drains `tts_text_queue`, `tts_audio_queue`, `report_queue` (`connection.py:1662-1681`), sends `{type: tts, state: stop}` itself (L16-18). `client_abort` is reset to `False` at the start of the next turn (`receiveAudioHandle.py:101`, `helloHandle.py:110`, `intentHandler.py:103,152`), so an in-flight `streaming_synthesize` loop from the old turn can observe `client_abort == False` again. **`self.conn.sentence_id != self.current_sentence_id` is the only reliable stop condition**; check both.

## 4. Streaming template: `core/providers/tts/minimax_httpstream.py`

Copy the shape; replace the HTTP body with gRPC.
- `open_audio_channels` (L88-94) copies `conn.sample_rate` into the request config after `super()`.
- `tts_text_priority_thread` (L96-129): FIRST resets `tts_stop_request/processed_chars/tts_text_buff/before_stop_play_files` (L101-106); TEXT appends and segments (L107-111); FILE → `_process_audio_file_stream(..., callback=handle_audio_file)` (L113-119); LAST → `_process_remaining_text_stream(True)` (L120-122). **It omits all five of the base thread's safeguards**: the `client_abort` skip, the `sentence_id` filter, `current_sentence_id`, `is_first_sentence=True`, `tts_audio_first_sentence=True`. Include all of them. Without `is_first_sentence=True` the comma-split first segment only happens on the connection's first turn; without `tts_audio_first_sentence=True` the per-turn first-audio log (`sendAudioHandle.py:26-28`) never fires again; without `current_sentence_id` frames carry `None` and bypass the stale filter.
- `to_tts_single_stream` (L148-174): `MarkdownCleaner.clean_markdown`, `correct_words` substitution, `asyncio.run(self.text_to_speak(text, is_last))`; the retry loop is cosmetic (one attempt).
- `text_to_speak` (L176-287): `self.pcm_buffer.clear()` then `(SentenceType.FIRST, [], text)` **before** the first audio chunk (L214-215); on error `(SentenceType.LAST, [], None)` (L211). Use 4-tuples with `self.current_sentence_id` instead.
- `close()` (L289-293): `super().close()` then `self.opus_encoder.close()`.
- `voice` key convention: minimax reads `private_voice`/`voice_id` (L28-31); `index_stream.py:22-26` reads `voice`/`private_voice`. `index_stream.py:31-34` creates its own 24 kHz encoder in `__init__` and overrides `audio_to_pcm_data_stream`/`audio_to_opus_data_stream` with `sample_rate=24000` (L182-194) — the fallback pattern for pitfall 2.

## 5. Google API facts

**Wheel `google-cloud-texttospeech==2.37.0`** (`METADATA:30` `Requires-Dist: protobuf<8.0.0,>=4.25.8`; base image `python:3.10-slim`, `Dockerfile-server-base:3`):
- Top-level exports from `google.cloud.texttospeech` (`texttospeech/__init__.py:46-90`): `TextToSpeechClient`, `TextToSpeechAsyncClient`, `AudioConfig`, `AudioEncoding`, `StreamingAudioConfig`, `StreamingSynthesisInput`, `StreamingSynthesizeConfig`, `StreamingSynthesizeRequest`, `SynthesisInput`, `VoiceSelectionParams`.
- `AudioEncoding` (`types/cloud_tts.py:81-127`): `LINEAR16 = 1`, `OGG_OPUS = 3`, `PCM = 7` (verified `int(AudioEncoding.PCM) == 7`).
- `StreamingAudioConfig` (`cloud_tts.py:917-948`) has **exactly three fields**: `audio_encoding` (Required; "Streaming supports PCM, ALAW, MULAW and OGG_OPUS"), `sample_rate_hertz` ("Optional. The synthesis sample rate (in hertz) for this audio." — no conversion guarantee stated), `speaking_rate` ("Optional. Input only. Speaking rate/speed, in the range [0.25, 2.0]"). **No `pitch`, no `volume_gain_db`, no `effects_profile_id`** on the streaming path. Sync `AudioConfig` has `audio_encoding, speaking_rate, pitch, volume_gain_db, sample_rate_hertz, effects_profile_id`.
- `StreamingSynthesizeConfig` (`cloud_tts.py:951-1006`): `voice` (1), `streaming_audio_config` (4), `custom_pronunciations` (5), `advanced_voice_options` (7).
- `StreamingSynthesisInput` (`cloud_tts.py:1008-1065`): oneof `input_source` = `text` (1), `markup` (5, "Markup for HD voices specifically"), `multi_speaker_markup` (7); plus optional `prompt` (6, controllable voices only). Pause tags need `markup=`, not `text=` (chirp3-hd page: "Note that pause tags will only work in the `markup` field, and not in the `text` field.").
- `StreamingSynthesizeRequest` (`cloud_tts.py:1067-1107`): oneof `streaming_config` | `input`. `StreamingSynthesizeResponse` (`cloud_tts.py:1109-1125`): single field `audio_content: bytes` ("headerless LINEAR16 audio with a sample rate of 24000", v1 RPC reference).
- `TextToSpeechClient.streaming_synthesize(self, requests: Optional[Iterator[StreamingSynthesizeRequest]] = None, *, retry, timeout, metadata) -> Iterable[StreamingSynthesizeResponse]` (`services/text_to_speech/client.py:975-982`); async twin at `async_client.py:547`.
- **The call blocks until the first response and raises API errors at the call site.** The return value is `google.api_core.grpc_helpers._StreamingResponseIterator` (subclass of `grpc.Call`, `api_core/grpc_helpers.py:83`); its `__init__` prefetches `next(self._wrapped)` (L87-99; `error_remapped_callable` L161-174 with `prefetch_first = True` default at L169). Observed empirically: an unauthenticated call raised `google.api_core.exceptions.PermissionDenied` from `client.streaming_synthesize(...)` itself, before iteration. It exposes `.cancel()` (L126-127, delegates to the gRPC call), `.code()`, `.details()`.
- **Credentials are resolved in the constructor**, not on first call: `transports/base.py:102-114` runs `google.auth.load_credentials_from_file(credentials_file, ...)` or `google.auth.default(...)` inside the transport `__init__`, which `TextToSpeechClient.__init__` invokes (`client.py:698`). Observed: `GOOGLE_APPLICATION_CREDENTIALS=/nonexistent.json; TextToSpeechClient()` raises `google.auth.exceptions.DefaultCredentialsError`.
- `TextToSpeechClient.from_service_account_file(filename: str, *args, **kwargs)` exists (`client.py:203`). There is **no public `close()`**; `__exit__` calls `self.transport.close()` (`client.py:1072-1080`), so `self.client.transport.close()` is the correct shutdown call (or use the client as a context manager; the docstring warns only if the transport is shared).
- `google.api_core.exceptions.InvalidArgument` and `Cancelled` exist (verified import).

**Docs** (`https://docs.cloud.google.com/text-to-speech/docs/chirp3-hd`, `https://docs.cloud.google.com/text-to-speech/docs/reference/rpc/google.cloud.texttospeech.v1`, fetched 2026-10-06):
- Voice name format `<locale>-<model>-<voice>`, example `en-US-Chirp3-HD-Kore`; 30 speakers: Achernar, Achird, Algenib, Algieba, Alnilam, Aoede, Autonoe, Callirrhoe, Charon, Despina, Enceladus, Erinome, Fenrir, Gacrux, Iapetus, Kore, Laomedeia, Leda, Orus, Pulcherrima, Puck, Rasalgethi, Sadachbia, Sadaltager, Schedar, Sulafat, Umbriel, Vindemiatrix, Zephyr, Zubenelgenubi. `cmn-CN` and `en-US` are both in the locale table and both have all 30 `*-Chirp3-HD-*` names on the voices page (skeptic-verified, grep count 30 each).
- "SSML tags are not currently supported for streaming requests." Pace 0.25x–2x. Custom pronunciations available for all locales except a list that does not include `cmn-cn` or `en-us`.
- The chirp3-hd page says nothing about `pitch` beyond the SSML `<prosody>` row ("Customizes the pitch, speaking rate, and volume"). **Whether sync `AudioConfig.pitch` is honoured for Chirp 3 HD is UNVERIFIED.** The verifiable fact is structural: the streaming request has no pitch or volume knob at all.
- `AudioConfig.sample_rate_hertz` (sync): "...the synthesizer will honor this request by converting to the desired sample rate (which might result in worse audio quality), unless the specified sample rate is not supported for the encoding chosen, in which case it will fail the request and return `google.rpc.Code.INVALID_ARGUMENT`." `AudioConfig.pitch`: "Optional. Input only. Speaking pitch, in the range [-20.0, 20.0]".
- Encodings: `PCM` "won't be wrapped in a WAV (or any other) header"; `OGG_OPUS` "Opus encoded audio wrapped in an ogg container"; `LINEAR16` includes a WAV header.
- Quotas, pricing and ADC search order: carried over from revision 1 (not disputed); re-fetch if you need exact numbers.

## 6. Class outline — `core/providers/tts/google_tts.py`

```python
import os, re, queue, asyncio, traceback
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

class TTSProvider(TTSProviderBase):
    # Only speaking_rate is tunable on the streaming path (StreamingAudioConfig has no pitch/volume).
    TTS_PARAM_CONFIG = [("ttsRate", "speaking_rate", 0.25, 2.0, 1.0, lambda v: round(float(v), 2))]

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.interface_type = InterfaceType.SINGLE_STREAM
        self.audio_file_type = "wav"
        self.speaker = config.get("speaker", "Charon")
        self.voice_en = config.get("voice_en") or f"en-US-Chirp3-HD-{self.speaker}"
        self.voice_zh = config.get("voice_zh") or f"cmn-CN-Chirp3-HD-{self.speaker}"
        self.default_language = config.get("default_language", "en")
        self.voice = config.get("private_voice") or (self.voice_zh if self.default_language == "zh" else self.voice_en)
        self.speaking_rate = float(config.get("speaking_rate") or 1.0)
        self._apply_percentage_params(config)          # ttsRate (-100..100) overrides speaking_rate
        self.sample_rate = 24000                        # replaced by conn.sample_rate in open_audio_channels
        self.credentials_file = config.get("credentials_file")
        self._client = None                              # lazy: constructor runs at connection setup
        self.pcm_buffer = bytearray()
        self._active_call = None

    @property
    def client(self):
        if self._client is None:
            if self.credentials_file:
                path = os.path.join(get_project_dir(), self.credentials_file)
                if not os.path.exists(path):
                    raise FileNotFoundError(f"Google TTS credentials_file not found: {self.credentials_file}")
                self._client = texttospeech.TextToSpeechClient.from_service_account_file(path)
            else:
                self._client = texttospeech.TextToSpeechClient()   # ADC; raises DefaultCredentialsError here if none
        return self._client

    def _select_voice(self, text):
        if _CJK.search(text): return self.voice_zh, "cmn-CN"
        if re.search(r"[A-Za-z]", text): return self.voice_en, "en-US"
        return (self.voice_zh, "cmn-CN") if self.default_language == "zh" else (self.voice_en, "en-US")

    async def open_audio_channels(self, conn):
        await super().open_audio_channels(conn)
        self.sample_rate = conn.sample_rate

    async def close(self):
        await super().close()
        if getattr(self, "opus_encoder", None): self.opus_encoder.close()
        if self._client is not None:
            try: self._client.transport.close()
            except Exception: pass

    def tts_text_priority_thread(self):
        while not self.conn.stop_event.is_set():
            try:
                message = self.tts_text_queue.get(timeout=1)
                if self.conn.client_abort: continue
                if message.sentence_id != self.conn.sentence_id: continue
                if message.sentence_type == SentenceType.FIRST:
                    self.current_sentence_id = message.sentence_id
                    self.tts_stop_request = False
                    self.processed_chars = 0
                    self.tts_text_buff = []
                    self.before_stop_play_files.clear()
                    self.is_first_sentence = True
                    self.tts_audio_first_sentence = True
                    self.opus_encoder.reset_state()      # drop an aborted segment's tail
                elif message.content_type == ContentType.TEXT:
                    self.tts_text_buff.append(message.content_detail)
                    seg = self._get_segment_text()
                    if seg: self.to_tts_single_stream(seg)
                elif message.content_type == ContentType.FILE:
                    if message.content_file and os.path.exists(message.content_file):
                        self._process_audio_file_stream(message.content_file,
                            callback=lambda a: self.handle_audio_file(a, message.content_detail))
                if message.sentence_type == SentenceType.LAST:
                    self._process_remaining_text_stream(True)
            except queue.Empty:
                continue
            except Exception as e:
                logger.bind(tag=TAG).error(f"处理TTS文本失败: {e} {traceback.format_exc()}")

    # _process_remaining_text_stream / to_tts_single_stream: mirror minimax_httpstream.py:131-174,
    # calling self._stream_segment(text, is_last) instead of asyncio.run(text_to_speak).

    def _stream_segment(self, text, is_last):
        voice_name, lang = self._select_voice(text)
        cfg = texttospeech.StreamingSynthesizeRequest(streaming_config=texttospeech.StreamingSynthesizeConfig(
            voice=texttospeech.VoiceSelectionParams(name=voice_name, language_code=lang),
            streaming_audio_config=texttospeech.StreamingAudioConfig(
                audio_encoding=texttospeech.AudioEncoding.PCM,
                sample_rate_hertz=self.sample_rate, speaking_rate=self.speaking_rate)))
        def gen():
            yield cfg
            yield texttospeech.StreamingSynthesizeRequest(input=texttospeech.StreamingSynthesisInput(text=text))
        enc = self.opus_encoder
        frame_bytes = enc.sample_rate * enc.channels * enc.frame_size_ms // 1000 * 2
        sid = self.current_sentence_id
        def stale(): return self.conn.client_abort or self.conn.sentence_id != sid
        self.pcm_buffer.clear()
        self.tts_audio_queue.put((SentenceType.FIRST, [], text, sid))   # subtitle before any audio
        try:
            call = self.client.streaming_synthesize(gen(), timeout=self.tts_timeout)  # blocks until first chunk
            self._active_call = call
            for resp in call:
                if stale():
                    call.cancel(); return
                self.pcm_buffer.extend(resp.audio_content)
                while len(self.pcm_buffer) >= frame_bytes:
                    frame = bytes(self.pcm_buffer[:frame_bytes]); del self.pcm_buffer[:frame_bytes]
                    enc.encode_pcm_to_opus_stream(frame, end_of_stream=False, callback=self.handle_opus)
            if len(self.pcm_buffer) % 2: self.pcm_buffer.pop()        # int16 guard
            if self.pcm_buffer:
                enc.encode_pcm_to_opus_stream(bytes(self.pcm_buffer), end_of_stream=True, callback=self.handle_opus)
                self.pcm_buffer.clear()
            if is_last: self._process_before_stop_play_files()
        except gexc.Cancelled:
            return                                                       # stale sentence: emit nothing
        except gexc.InvalidArgument as e:
            logger.bind(tag=TAG).error(f"Google TTS rejected request (check sample_rate_hertz/voice): {e}")
            self.tts_audio_queue.put((SentenceType.LAST, [], None, sid))
        except Exception as e:
            logger.bind(tag=TAG).error(f"Google TTS stream failed: {e}")
            self.tts_audio_queue.put((SentenceType.LAST, [], None, sid))
        finally:
            self._active_call = None

    async def text_to_speak(self, text, output_file):
        voice_name, lang = self._select_voice(text)
        resp = self.client.synthesize_speech(
            input=texttospeech.SynthesisInput(text=text),
            voice=texttospeech.VoiceSelectionParams(name=voice_name, language_code=lang),
            audio_config=texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.LINEAR16,
                                                   sample_rate_hertz=self.sample_rate, speaking_rate=self.speaking_rate),
            timeout=self.tts_timeout)
        if output_file:
            with open(output_file, "wb") as f: f.write(resp.audio_content)
        else:
            return resp.audio_content
```

Design choice: one `streaming_synthesize` call per text segment (minimax/index_stream granularity). Per-turn long-lived session is Future Work. The sync client blocks only `tts_priority_thread`; never call it on `conn.loop`.

### Config template (`config.yaml`, under `TTS:`)
```yaml
  GoogleTTS:
    type: google_tts
    output_dir: tmp/
    # Credentials: GOOGLE_APPLICATION_CREDENTIALS (ADC) or credentials_file relative to the server dir.
    # credentials_file: data/google-sa.json
    speaker: Charon            # Chirp 3 HD speaker, used for both locales
    # voice_en: en-US-Chirp3-HD-Charon
    # voice_zh: cmn-CN-Chirp3-HD-Charon
    default_language: en       # voice for text with neither CJK nor Latin letters
    speaking_rate: 1.0         # 0.25-2.0; the only streaming-path knob
    # ttsRate: 0               # optional -100..100 percentage mapped onto 0.25..2.0 around 1.0; overrides speaking_rate
```
`credentials_file` resolves against `get_project_dir()` (the loader does the same for `data/.config.yaml`, `config/config_loader.py:35-36`). Leave `xiaozhi.audio_params.sample_rate: 24000` (`config.yaml:95`).

### Language → voice mapping
| Segment content | Voice | language_code |
|---|---|---|
| any CJK ideograph / CJK punctuation / full-width form | `cmn-CN-Chirp3-HD-<speaker>` | `cmn-CN` |
| no CJK, ASCII letters | `en-US-Chirp3-HD-<speaker>` | `en-US` |
| neither | `default_language` voice | — |
Same speaker across locales (verified: 30 names in both locales). Whether Chirp 3 HD reads embedded English inside a `cmn-CN` request well is UNVERIFIED; check in the smoke test. Voice-name letter case (`en-US` vs `en-us`) handling by the API is UNVERIFIED; the docs' format example is `en-US-Chirp3-HD-Kore`.

## 7. Docker / deployment

- `Dockerfile-server-base:1-32`: `FROM python:3.10-slim` (L3), `apt-get install libopus0 ffmpeg locales` (L7 — ffmpeg for pydub and libopus are present), `COPY main/xiaozhi-server/requirements.txt .` (L28), `pip install -r requirements.txt` (L31-32). No `USER` instruction in either Dockerfile, so the container runs as the base image's default user (root for `python:3.10-slim` — UNVERIFIED by doc fetch).
- `Dockerfile-server:1-7`: `FROM ghcr.io/xinnan-tech/xiaozhi-esp32-server:server-base`, `COPY main/xiaozhi-server .`, `CMD ["python", "app.py"]`. Prod image contains only upstream's baked requirements. Either rebuild server-base from the fork's `requirements.txt`, or add `RUN pip install --no-cache-dir google-cloud-texttospeech==2.37.0` to `Dockerfile-server` before the `COPY`.
- `main/xiaozhi-server/docker-compose.yml:1-22`: `image: ghcr.nju.edu.cn/xinnan-tech/xiaozhi-esp32-server:server_latest` (L6), `environment: [TZ=UTC]` (L11-12), ports L13-17, `volumes:` L18 with `./data:/opt/xiaozhi-esp32-server/data` (L20). **No `build:` key** in `docker-compose.yml` or `docker-compose_all.yml` (grep), so `docker compose build` builds nothing. Add `build: {context: ../.., dockerfile: Dockerfile-server}` (context must be the repo root because of `COPY main/xiaozhi-server .`) or run `docker build -f Dockerfile-server .` from the repo root.
- Add to `environment:`: `- GOOGLE_APPLICATION_CREDENTIALS=/opt/xiaozhi-esp32-server/data/google-sa.json`; file 0600 on the host under `main/xiaozhi-server/data/` (gitignored, `.gitignore:156`).

## 8. Pitfalls

1. **Dependency conflict (blocking).** `requirements.txt:15` pins `google-generativeai==0.8.5`, whose `google-ai-generativelanguage 0.6.15` pins `protobuf<6.0.0dev` (skeptic-verified on PyPI). `google-cloud-texttospeech==2.38.0` requires `protobuf>=6.33.5`; **2.37.0** requires `protobuf<8.0.0,>=4.25.8` (`METADATA:30`) so pip can resolve to 5.x. In a clean venv 2.37.0 alone resolves to protobuf 7.36.2; the combined resolution with upstream's `requirements.txt` is the acceptance step below.
2. **Output sample rate.** Streaming `sample_rate_hertz` is "Optional" with no conversion guarantee; the response doc says 24000. Request `sample_rate_hertz=conn.sample_rate` (config-only, `connection.py:246`), measure bytes/sec in the smoke test. Fallback if the device ever runs at 16 kHz: own 24 kHz `OpusEncoderUtils` in `__init__` plus the `index_stream.py:182-194` overrides.
3. **Encodings.** Streaming: `PCM` (7). Non-stream `text_to_speak`: `LINEAR16` (WAV header for pydub). `OGG_OPUS` is Ogg-containerised, not raw frames. Catch `google.api_core.exceptions.InvalidArgument` (sample rate unsupported for encoding) rather than looping in `to_tts`'s 5x retry.
4. **FIRST ordering and 4-tuples.** Queue `(FIRST, [], text, sid)` before the first frame; always include `current_sentence_id`, since `None` bypasses `sendAudioHandle.py:23`.
5. **Interrupt.** Check `client_abort` and `conn.sentence_id != sid` per chunk; on stale call `.cancel()` on the `_StreamingResponseIterator`, swallow `google.api_core.exceptions.Cancelled`, and do not emit LAST for the stale id (abort already sent `tts stop`, `abortHandle.py:16-18`). `client_abort` alone is not reliable (§3).
6. **Error path emits LAST** with the current sid so the device gets `tts stop` (`sendAudioHandle.py:51-53`, only when `not conn.calling`).
7. **First-chunk blocking.** `streaming_synthesize()` returns only after the first response (`grpc_helpers.py:87-99`); errors such as `PermissionDenied`/`InvalidArgument` surface at the call, so wrap the call itself, not just the loop. Measure first-audio latency from segment dispatch to the first `handle_opus`.
8. **No pitch/volume on streaming.** `StreamingAudioConfig` has only `audio_encoding/sample_rate_hertz/speaking_rate`; do not register `ttsPitch` or `ttsVolume` in `TTS_PARAM_CONFIG`. Pitch support for Chirp 3 HD on the sync path is UNVERIFIED.
9. **Request size.** 5,000 bytes per request (carried over); guard long `tts_one_sentence`/FILE text by splitting on `self.punctuations` when `len(text.encode()) > 4500`.
10. **Quota.** 200 Chirp 3 requests/min/project (carried over); per-segment streaming means one request per sentence. Whether a streaming session counts as one request is UNVERIFIED.
11. **Credentials at construction.** `TextToSpeechClient()` resolves ADC in `__init__` (§5). Eager construction inside `TTSProvider.__init__` fails the whole connection (`connection.py:606-609`) and makes the three construction tests in `tests/test_tts.py:52-81` fail in mock mode. Construct lazily (outline above) and log a clear "credentials not found" line; never log file contents.
12. **Billing on CJK.** Whether Chirp 3 HD bills CJK as one character each is UNVERIFIED.
13. **Tests.** `tests/conftest.py:211-225` does not shim `google.cloud`; `_build_provider()` (`test_tts.py:37-49`) reads `selected_module.TTS` and the `TTS.<name>` block from `config.yaml` + `data/.config.yaml` (`conftest.py:65-80`, `155`), so the existing suite needs a `GoogleTTS` block with `type: google_tts` in `data/.config.yaml` (or `PYTEST_CONFIG_FILE`). `test_tts_text_to_speak_writes_file` (`test_tts.py:84-127`) stubs `conn` with `sample_rate=16000, audio_format="pcm"` while the provider's `self.sample_rate` stays 24000 — harmless for the file-exists assertion. In `tests/test_tts_google.py`, `monkeypatch` `texttospeech.TextToSpeechClient` **before** `create_instance` with a fake exposing `synthesize_speech` (returns `.audio_content` = small WAV) and `streaming_synthesize` (returns an iterator with `.cancel()` yielding `.audio_content` PCM chunks, including one odd-length chunk); live runs via `RUN_LIVE_API_TESTS=1` (`conftest.py:13-16`, `207`).
14. **Encoder state across segments.** `encode_pcm_to_opus_stream(..., end_of_stream=True)` empties the buffer but does not `reset_state()` (`opus_encoder_utils.py:52-55, 92-100`); `self.pcm_buffer.clear()` does not touch the encoder's `self.buffer`. Call `self.opus_encoder.reset_state()` on FIRST. Drop/pad an odd trailing byte before the flush (`np.frombuffer(dtype=int16)`, L118-121).
15. **Frame contract.** One 60 ms `bytes` frame per `handle_opus` call (`sendAudioHandle.py:16, 134`); never pass a list for MIDDLE.
16. **CWD.** `create_instance` tests a CWD-relative path (`core/utils/tts.py:35`); run from `main/xiaozhi-server/`.
17. **Docker.** Without the package in the image, `create_instance` raises at import on the first device connection (§7).

## 9. Acceptance checklist

- [ ] In the project venv, `pip install -r requirements.txt` (with `google-cloud-texttospeech==2.37.0` added) resolves; `python -c "from google.cloud import texttospeech; print(texttospeech.StreamingAudioConfig, texttospeech.TextToSpeechClient.streaming_synthesize)"` succeeds; `pip show protobuf` is 5.x.
- [ ] From `main/xiaozhi-server/`, `create_instance("google_tts", cfg, True)` returns a `TTSProviderBase` subclass **without credentials present** (lazy client); existing `tests/test_tts.py` construction tests pass with `selected_module.TTS: GoogleTTS`.
- [ ] Mock: `text_to_speak(text, None)` returns bytes starting with `RIFF`; with `provider.conn = SimpleNamespace(sample_rate=24000, ...)` (as `test_tts.py:99-105`) and `delete_audio_file=True`, `to_tts("你好")` returns a non-empty `list[bytes]`.
- [ ] Mock: FIRST/MIDDLE/LAST on `tts_text_queue` yields `(FIRST, [], text, sid)` → N×`(MIDDLE, bytes, None, sid)` → `(LAST, [], None, sid)` on `tts_audio_queue`; CJK segment selects `cmn-CN-Chirp3-HD-*`, Latin selects `en-US-Chirp3-HD-*`; a raised `InvalidArgument` yields a trailing LAST; setting `conn.sentence_id` to a new value mid-stream calls `.cancel()` and emits no further frames and no LAST for the old sid; an odd-length final chunk does not raise.
- [ ] Live smoke (`RUN_LIVE_API_TESTS=1`, one short text per language): audio plays on the 1.75C; PCM byte rate ≈ 48,000 B/s; first-audio latency per segment logged, median < 1.5 s (DESIGN.md).
- [ ] Interrupting mid-reply stops synthesis within one segment; no stale audio after the next turn starts.
- [ ] Mixed-language reply switches voice between segments with the same speaker.
- [ ] `docker build -f Dockerfile-server .` from the repo root (or compose with the added `build:` block) produces an image where `python -c "import google.cloud.texttospeech"` works; compose `environment:` sets `GOOGLE_APPLICATION_CREDENTIALS=/opt/xiaozhi-esp32-server/data/<file>.json`; the file is 0600 under `data/`; `git status` shows nothing under `data/`.
- [ ] `config.yaml` documents the `GoogleTTS` section (streaming-path knob is `speaking_rate`/`ttsRate` only); `.claude/status/spec.md` records the per-segment-session choice and the per-turn session as Future Work.

## UNVERIFIED
- Whether sync AudioConfig.pitch is honoured (or rejected) for Chirp 3 HD voices — no statement found on the chirp3-hd page or the v1 RPC reference.
- Whether the API accepts lowercase locale in voice names (en-us-Chirp3-HD-*) — the fetched page's format example is uppercase en-US; case handling not tested.
- Whether Chirp 3 HD reads embedded English words inside a cmn-CN request acceptably — needs the live smoke test.
- Whether a streaming session counts as one request against Chirp3RequestsPerMinutePerProject.
- Whether Chirp 3 HD bills CJK characters as one character each.
- Default user of the python:3.10-slim base image (no USER instruction in either Dockerfile is verified; root default not confirmed by doc fetch).
- Combined pip resolution of upstream requirements.txt plus google-cloud-texttospeech==2.37.0 (only the clean-venv install of 2.37.0 was tested; protobuf<6 pin of google-ai-generativelanguage 0.6.15 is skeptic-reported, not re-fetched here).
- Quota, pricing and ADC search-order numbers are carried over from revision 1 and were not re-fetched in this revision.
