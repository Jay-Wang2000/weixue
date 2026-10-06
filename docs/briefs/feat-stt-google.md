# feat-stt-google — Developer brief (rev. 2): Google Cloud STT v2 provider for xiaozhi-esp32-server

Vendor baseline: `/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32-server` at `587c6df77b0220ddbd8fc804f984f83f69f3225e5`. Paths below are relative to `main/xiaozhi-server/` unless absolute. SDK facts come from the `google_cloud_speech-2.40.0-py3-none-any.whl` wheel (downloaded 2026-10-06 into a scratch Python 3.10.16 venv), google-auth 2.60.0 and google-api-core 2.41.0. Doc pages fetched 2026-10-06. Nothing in the vendor tree was modified.

Changes from rev. 1 are marked **[rev]**.

## 1. Decision summary

| Choice | Value | Why |
|---|---|---|
| API | Speech-to-Text **v2**, `google.cloud.speech_v2` | v2 adds `language_codes[]`, per-location recognizers (`locations/{location}/recognizers/_`) and `AutoDetectDecodingConfig` ([migration](https://docs.cloud.google.com/speech-to-text/v2/docs/migration)) |
| Model / location | `chirp_3`, `location: us`, endpoint `us-speech.googleapis.com` | chirp_3 page lists `us (multi-region) GA`, `eu (multi-region) GA`; supports `Recognize`, `StreamingRecognize`, `BatchRecognize` ([chirp_3](https://docs.cloud.google.com/speech-to-text/v2/docs/chirp_3-model)). **[rev]** Whether `global` also serves chirp_3 is UNVERIFIED: Google's own sync sample uses `locations/global` + `model="chirp_3"` with a default-endpoint `SpeechClient()` ([sync-recognize](https://docs.cloud.google.com/speech-to-text/v2/docs/sync-recognize)), contradicting the region table. Keep `us`; do not describe `global` as unsupported; the live smoke decides. |
| Languages | `language_codes: ["en-US", "cmn-Hans-CN"]`, fallback `["auto"]` | **[rev]** Parsed the 1564-row table at [supported-languages](https://docs.cloud.google.com/speech-to-text/v2/docs/speech-to-text-supported-languages): `cmn-Hans-CN` rows are exactly chirp_3 (`us`, `eu`), chirp and chirp_2 (`us-central1`, `europe-west4`, `asia-southeast1`). No `long`/`short`/`telephony` row for Mandarin; **no `zh-CN` row anywhere in v2**. `en-US` has chirp_3 (`us`,`eu`), `long`/`short`/`telephony`/`telephony_short` (`global`,`us`,`eu`), chirp/chirp_2/chirp_telephony (single regions). chirp_3 page: `language_codes=["auto"]` or "condition it on specific locales you expect, for example: `["en-US", "fr-FR"]`". en-US + cmn-Hans-CN together is within that wording but has no documented example → confirm in the smoke; auto-retry once with `["auto"]` on `InvalidArgument`. |
| Call style | Synchronous `Recognize`, `InterfaceType.NON_STREAM` (`core/providers/asr/dto/dto.py:5-9`) | Sync limit "60 seconds, 10 MB" ([sync-recognize](https://docs.cloud.google.com/speech-to-text/v2/docs/sync-recognize); [quotas](https://docs.cloud.google.com/speech-to-text/v2/docs/quotas)). Two upstream trigger paths feed one `speech_to_text` call (§2). |
| Audio | `ExplicitDecodingConfig(encoding=LINEAR16, sample_rate_hertz=16000, audio_channel_count=1)` on `artifacts.pcm_bytes` | Server decodes Opus→PCM at entry with `opuslib_next.Decoder(16000, 1)` and `decode(packet, 960)` (`core/connection.py:556`, `:547`), for both WebSocket (`connection.py:380-383`) and MQTT-gateway (`connection.py:375-378`, `385-409`) clients. RPC ref: `sample_rate_hertz` valid 8000-48000, 16000 "optimal"; `audio_channel_count` "effectively REQUIRED" ([rpc ref](https://docs.cloud.google.com/speech-to-text/v2/docs/reference/rpc/google.cloud.speech.v2)). Exact match to the chirp_3 page's streaming sample, which also passes `ClientOptions(api_endpoint=f"{region}-speech.googleapis.com", quota_project_id=project_id)` and `RecognitionFeatures(enable_automatic_punctuation=True)`. |
| Auth | `credentials_file` (service-account JSON) by default; ADC **opt-in** via `use_adc: true` **[rev]** | `service_account.Credentials.from_service_account_file(path)`; `.project_id` property exists (google-auth 2.60.0 `google/oauth2/service_account.py:283`; [reference](https://googleapis.dev/python/google-auth/latest/reference/google.oauth2.service_account.html)). `google.auth.default()` returns `(credentials, project_id)` and "Project ID may be None" ([reference](https://googleapis.dev/python/google-auth/latest/reference/google.auth.html); `google/auth/_default.py:593`, `:748`). ADC is opt-in because mock-mode pytest constructs the real provider and calls it (§2 Tests). |
| Package | `google-cloud-speech==2.40.0` **[rev]** | 2.41.0 requires `protobuf>=6.33.5,<8`; vendored `requirements.txt:15` pins `google-generativeai==0.8.5` → `google-ai-generativelanguage==0.6.15` → `protobuf<6`. Verified in a Python 3.10.16 venv: `pip install --dry-run google-cloud-speech==2.41.0 google-generativeai==0.8.5` → `ResolutionImpossible`; with `2.40.0` → `Would install google-cloud-speech-2.40.0 protobuf-5.29.6 grpcio-1.84.0 google-api-core-2.33.0 google-auth-2.60.0`. 2.40.0 METADATA: `Requires-Python >=3.10`, `protobuf<8.0.0,>=4.25.8`, `google-api-core[grpc]<3,>=2.17.1`, `google-auth<3,>=2.14.1,!=2.24.0,!=2.25.0`, `grpcio<2,>=1.59.0`, `proto-plus<2,>=1.22.3`. PyPI: 2.40.0 uploaded 2026-06-03 ([PyPI JSON](https://pypi.org/pypi/google-cloud-speech/json)). Base image is `python:3.10-slim` (`Dockerfile-server-base:3`). |

## 2. Upstream contract (from source)

**Factory** — `core/utils/asr.py:16-24`: `create_instance(class_name, *args, **kwargs)` checks `os.path.exists(os.path.join('core','providers','asr', f'{class_name}.py'))` **relative to CWD** (line 18), imports `core.providers.asr.<class_name>` and instantiates `.ASRProvider(*args, **kwargs)`; otherwise raises `不支持的ASR类型`. So: file `core/providers/asr/google_stt.py`, class `ASRProvider`, config `type: google_stt`, and the server/pytest/smoke must run with CWD = `main/xiaozhi-server`.

**Constructor** — `core/utils/modules_initialize.py:118-131`: `create_instance(asr_type, config["ASR"][name], delete_audio_bool)`. Signature: `def __init__(self, config: dict, delete_audio_file: bool)`. **[rev]** Follow `doubao.py:84-85` (calls `super().__init__()`), not `openai.py:14-22` (does not, and overrides `requires_file()` to `True` at `openai.py:24-25`). Set `self.interface_type = InterfaceType.NON_STREAM`, `self.output_dir`, `self.delete_audio_file`, `os.makedirs(self.output_dir, exist_ok=True)`.

**Three instantiation sites [rev]** — (a) server start: `initialize_modules(init_asr=True)` → `initialize_asr` (`modules_initialize.py:94-96`), held as `self._asr` (`core/websocket_server.py:58`) and passed to `ConnectionHandler` (`websocket_server.py:121-129`); (b) per connection: `connection.py:757-772` builds a **new instance** for every non-`LOCAL` provider via `initialize_asr(self.config)`; (c) hot config reload rebuilds it when `check_asr_update` says the selected ASR changed (`websocket_server.py:173-197`, `core/utils/util.py:448-460`). A key-file or network failure in `__init__` therefore crashes **server startup**, not just one connection. Keep `__init__` free of I/O; build the gRPC client lazily.

**Abstract method** — `core/providers/asr/base.py:322-336`:
```
async def speech_to_text(self, opus_data: List[bytes], session_id: str,
                         artifacts: Optional[AudioArtifacts] = None) -> Tuple[Optional[str], Optional[str]]
```
`opus_data` is PCM despite the name. Use `artifacts.pcm_bytes` (`base.py:290-296`).

**Two trigger paths [rev]** —
- auto/realtime listen mode: `base.receive_audio` (`base.py:65-81`) appends frames, and on `client_voice_stop` joins them and calls `handle_voice_stop(conn, [pcm_bytes])` **only if `len(pcm_bytes) > 1920*15`** (28 800 B ≈ 0.9 s) — a single joined blob (`base.py:75-80`).
- manual listen mode: `base.receive_audio` only appends (`base.py:62-64`); the trigger is `listen stop` in `core/handle/textHandler/listenMessageHandler.py:38-54`, which calls `conn.asr.handle_voice_stop(conn, conn.asr_audio.copy())` — a **list of 1920-byte frames, any non-empty length, no minimum**. (`listenMessageHandler.py:44-46` calls `conn.asr._send_stop_request()` for STREAM providers instead.)
- `speech_to_text_wrapper` (`base.py:268-301`) joins the list; when the joined PCM is empty it passes `artifacts=None` (`base.py:288-289`). The provider must return `("", None)` early on `artifacts is None` (pattern `openai.py:30-31`, `doubao.py:240-241`) and tolerate sub-second clips.

**Return value [rev]** — the wrapper discards the provider's second element: `text, _ = await self.speech_to_text(...)`; `return text, file_path` (`base.py:298-301`). Returning `artifacts.file_path` vs `None` has no effect; return `(text, None)` for simplicity. Exceptions escaping are logged and become `(None, None)` (`base.py:302-307`).

**Event loop** — frames are queued on `conn.asr_audio_queue` (`connection.py:159-160`, `:383`) and drained by `asr_text_priority_thread`, which schedules `handleAudioMessage` onto `conn.loop` (`base.py:43-58`). `speech_to_text` therefore runs **on the connection's asyncio loop**; wrap the sync client in `asyncio.to_thread` (precedent `fun_local.py:84-91`) or use `SpeechAsyncClient`.

**File hooks / output_dir [rev]** — keep `requires_file()`/`prefers_temp_file()` at the base default `False` (`base.py:232-238`). `output_dir` is mandatory **solely** because the wrapper calls `shutil.disk_usage(self.output_dir)` (`base.py:276-278`, CWD-relative); `ensure_directories` reads `provider.get("output_dir", "")` and skips when empty (`config/config_loader.py:122-124`). A WAV **is** written per utterance via `save_audio_to_file` whenever `delete_audio_file` is `False`, regardless of `requires_file()` (`base.py:283-286`), and is then never deleted (`base.py:310-317`). `config.yaml:59` sets `delete_audio: true`, so the default path writes nothing; with `delete_audio: false` expect one WAV per utterance in `output_dir`.

**close() [rev]** — `connection.py:1642-1643` awaits `self.asr.close()` on every connection teardown; base default is a no-op (`base.py:216-217`). Decision: client is **per instance** (instances are already per connection), built lazily; `close()` calls `self._client.transport.close()` if it was built (`SpeechClient.__exit__` does the same: wheel `client.py:3962-3970`). Do not share a module-level client, or `close()` must not close it.

**Result flow** — `handle_voice_stop` (`base.py:84-175`) `json.dumps` dict results; `startToChat` only unwraps JSON when both `speaker` and `content` exist (`core/handle/receiveAudioHandle.py:50-66`); `conn.chat` puts the query verbatim into the dialogue (`connection.py:1055-1066`). **Return a plain `str`**; log `result.language_code` at INFO.

**Config selection** — `config.yaml:234-237` `selected_module.ASR`; sections under `ASR:` from `config.yaml:358`; `type:` per section (`OpenaiASR` → `type: openai`, `config.yaml:504-516`). Runtime config = `merge_configs(config.yaml, data/.config.yaml)` (`config_loader.py:35-45`). **[rev]** `data/.config.yaml` must exist (`config/settings.py:17-21`) **and be a non-empty YAML mapping**: `read_config` returns `yaml.safe_load(...)` (`config_loader.py:20-23`), which is `None` for an empty file, and `custom_config.get("manager-api", {})` at `config_loader.py:42` would raise `AttributeError`. If `manager-api.url` is set, config comes from the Java API (`config_loader.py:42-43`; `config.yaml:5`) and the local `ASR.GoogleSTT` section is ignored — this brief assumes local-config mode. Credentials go in `data/.config.yaml`, never `config.yaml` (`config.yaml:1-5`).

**Dependencies / Docker [rev]** — `requirements.txt` has no Speech package (`pydub==0.25.1` at line 12, `google-generativeai==0.8.5` at line 15). Python deps are installed in **`Dockerfile-server-base`** (`:28-32`; base `python:3.10-slim` at `:3`; `ffmpeg` at `:7`). `Dockerfile-server` (repo root, `:1-7`) is `FROM ghcr.io/xinnan-tech/xiaozhi-esp32-server:server-base`, `COPY main/xiaozhi-server .`, `CMD ["python", "app.py"]` — build context is the repo root. `main/xiaozhi-server/docker-compose.yml:6` pulls `ghcr.nju.edu.cn/xinnan-tech/xiaozhi-esp32-server:server_latest` (a mirror registry); `:11-12` is the `environment:` block; `:20` bind-mounts `./data:/opt/xiaozhi-esp32-server/data`; `:22` bind-mounts `./models/SenseVoiceSmall/model.pt` (if absent on the host, Docker creates an empty directory there). The vendored Gemini LLM provider imports `google.generativeai` (`core/providers/llm/gemini/gemini.py:6-7`), which is why protobuf 5.x must survive.

**Tests** — `tests/test_stt.py:36-48` builds the provider via `create_instance(ptype, cfg, True)` from `selected_module.ASR`; `test_asr_speech_to_text_wrapper_returns_text` (`:92-128`) feeds `[b"\x00"*1024]` in mock mode (`:109-111`) and only requires no exception. **[rev] Billing hazard:** `google` is **not** in `_MOCKED_MODULES`/`_FORCE_SHIM_MODULES` (`tests/conftest.py:211-235`; `LIVE_API_TESTS` at `:207`), so mock mode calls the real provider; if ADC were the fallback and `gcloud auth application-default login` credentials exist on the Mac, "mock" pytest would issue a real, billed `Recognize`. Hence ADC opt-in, and the unit tests monkeypatch `_get_client`/`_recognize`. `conftest.py:66-71` honours `PYTEST_CONFIG_FILE` for an isolated config; `conftest.py:92-116` `sys.exit(2)`s if `data/.config.yaml` has `manager-api.url`+`secret`; `conftest.py:120` merges like production. `tests/core/` exists with `__init__.py`; `pyproject.toml:1-4` sets `asyncio_mode = "auto"`, `testpaths = ["tests"]`, `python_files = ["test_*.py"]`.

**Upstream live mode is not acceptance evidence [rev]** — `_find_test_audio()` (`test_stt.py:51-62`) picks the largest WAV > 300 KB in `config/assets/` and feeds the raw file bytes, RIFF header included, as PCM (`:107-108`), inside `asyncio.wait_for(..., timeout=10.0)` (`:114-117`, shorter than this provider's `timeout: 15`). Measured asset formats (Python `wave`): `bind_code.wav` 44.1 kHz stereo 2.35 s; `bind_not_found.wav`, `max_output_size.wav`, `wakeup_words.wav` 24 kHz mono (6.89 / 6.51 / 10.89 s); only `wakeup_words_short.wav` is 16 kHz mono 16-bit (1.56 s, 49 964 B). So upstream live mode would send 24 kHz audio labelled 16 kHz. None of the assets contains English speech.

## 3. Changes to make

1. **New** `core/providers/asr/google_stt.py` (§4).
2. **Edit** `config.yaml`, under `ASR:`:
```yaml
  GoogleSTT:
    # Google Cloud Speech-to-Text v2 (chirp_3). Service-account JSON path, or use_adc: true for Application Default Credentials.
    type: google_stt
    credentials_file: data/google-sa.json
    use_adc: false
    project_id: ""                 # empty -> from the credentials; fail fast if still unknown
    location: us                   # chirp_3 documented GA: us | eu
    model: chirp_3
    language_codes: ["en-US", "cmn-Hans-CN"]   # or ["auto"]
    enable_automatic_punctuation: true
    timeout: 15
    output_dir: tmp/
```
3. **Edit** `requirements.txt`: `google-cloud-speech==2.40.0`.
4. **Local override** `data/.config.yaml` (non-empty mapping, no `manager-api.url`): `selected_module.ASR: GoogleSTT`, `ASR.GoogleSTT.credentials_file`. Key copied from `~/Projects/ha-agent/.secrets/` (0600). `data/`, `backup/` stay out of git. For Docker, the path is the in-container one (`data/google-sa.json` under `/opt/xiaozhi-esp32-server`, via the `docker-compose.yml:20` mount); if ADC is used instead, set `GOOGLE_APPLICATION_CREDENTIALS=/opt/xiaozhi-esp32-server/data/<sa>.json` in the `environment:` block (`docker-compose.yml:11-12`), not the host path.
5. **Docker**: derived image — `FROM ghcr.io/xinnan-tech/xiaozhi-esp32-server:server-base`, `RUN pip install --no-cache-dir google-cloud-speech==2.40.0`, `COPY main/xiaozhi-server .`, `CMD ["python", "app.py"]`; switch the compose service from `image:` to `build:` (context = repo root). Or rebuild `Dockerfile-server-base` with the edited `requirements.txt`.
6. **Tests** `tests/core/test_asr_google_stt.py`, client monkeypatched (`_get_client` returns a stub whose `recognize` records the request): LINEAR16/16000/1, recognizer path `projects/<p>/locations/us/recognizers/_`, `language_codes` passthrough, `features.enable_automatic_punctuation`, `timeout=` forwarded, multi-result join, empty response → `""`, `artifacts is None` → `("", None)`, `GoogleAPICallError` → `("", None)`, `InvalidArgument` → one retry with `["auto"]`, `close()` without a built client is a no-op. One `RUN_LIVE_API_TESTS=1` smoke using **own 16 kHz mono clips** (one English, one Mandarin) or `config/assets/wakeup_words_short.wav` with the 44-byte header stripped; other assets need resampling (`pydub==0.25.1`, `requirements.txt:12`; ffmpeg in the base image).

## 4. Class skeleton (names only)

```
core/providers/asr/google_stt.py
  TAG = __name__; logger = setup_logging()
  class ASRProvider(ASRProviderBase):
      def __init__(self, config: dict, delete_audio_file: bool)
          # super().__init__(); interface_type=NON_STREAM; credentials_file, use_adc, project_id, location,
          # model, language_codes, enable_automatic_punctuation, timeout, output_dir; _client=None; _creds=None
          # no I/O here; os.makedirs(output_dir, exist_ok=True) only
      def _load_credentials(self) -> Tuple[Optional[Credentials], Optional[str]]
          # credentials_file -> service_account.Credentials.from_service_account_file(path); project = creds.project_id
          # use_adc -> google.auth.default() -> (creds, project)   # project may be None
          # explicit project_id wins; if still None -> raise ValueError before building the recognizer path
      def _get_client(self) -> SpeechClient
          # lazy; SpeechClient(credentials=creds, client_options=ClientOptions(
          #     api_endpoint=f"{location}-speech.googleapis.com", quota_project_id=project_id))
          # omit api_endpoint when location == "global"
      def _recognizer_path(self) -> str          # f"projects/{project_id}/locations/{location}/recognizers/_"
      def _build_config(self, language_codes) -> cloud_speech.RecognitionConfig
      def _recognize(self, pcm_bytes: bytes, language_codes) -> cloud_speech.RecognizeResponse
          # sync; client.recognize(request=..., timeout=self.timeout, retry=None); run via asyncio.to_thread
      @staticmethod
      def _extract(response) -> Tuple[str, Optional[str]]
          # " ".join(r.alternatives[0].transcript for r in results if r.alternatives), results[-1].language_code
      async def speech_to_text(self, opus_data, session_id, artifacts=None) -> Tuple[Optional[str], Optional[str]]
          # artifacts None -> ("", None); len(pcm) > 59*32000 -> trim; InvalidArgument -> retry once with ["auto"]
          # GoogleAPICallError / DefaultCredentialsError / ValueError -> log, ("", None)
      async def close(self)                      # if self._client: self._client.transport.close()
```

SDK names (wheel 2.40.0, `google/cloud/speech_v2/`): `SpeechClient.__init__(*, credentials=None, transport=None, client_options=None, client_info=...)` (`services/speech/client.py:680-687`); `SpeechClient.from_service_account_file(filename, *args, **kwargs)` (`client.py:210`; async `async_client.py:135`); `SpeechClient.recognize(request=None, *, recognizer=None, config=None, config_mask=None, content=None, uri=None, retry=gapic_v1.method.DEFAULT, timeout=gapic_v1.method.DEFAULT, metadata=())` (`client.py:1629-1641`; same on `SpeechAsyncClient`, `async_client.py:1104-1116`); `OptionalRetry = Union[retries.Retry, gapic_v1.method._MethodDefault, None]` (`client.py:53-55`) so `retry=None` is valid. Types (`types/cloud_speech.py`): `ExplicitDecodingConfig` (`:807`), `ExplicitDecodingConfig.AudioEncoding.LINEAR16 = 1` (`:871`), `RecognitionFeatures.enable_automatic_punctuation` (`:938`, `:1024`), `RecognitionConfig` (`:1214`), `RecognizeRequest` (`:1332`), `SpeechRecognitionResult.language_code` (`:1567`, `:1608`), `RecognizeResponse` (`:1614`). Exceptions: `google.api_core.exceptions.GoogleAPICallError` (`exceptions.py:109`), `InvalidArgument` (`:271`), `PermissionDenied` (`:308`), `DeadlineExceeded` (`:437`), `RetryError` (`:74`); `google.auth.exceptions.DefaultCredentialsError` (`exceptions.py:44`). Wrap both `_get_client` and `_recognize` — whether `DefaultCredentialsError` fires at client construction or first RPC is UNVERIFIED.

## 5. Pitfalls

- **Language code**: no `zh-CN` in v2; use `cmn-Hans-CN`. `long`/`short`/`telephony` have no Mandarin row at all.
- **Model/region coupling**: documented GA for chirp_3 is `us`/`eu`; `global` is UNVERIFIED either way (§1). chirp_2 regions are "Private GA" ([chirp_2](https://docs.cloud.google.com/speech-to-text/v2/docs/chirp_2-model)) — not a drop-in fallback. The [multiple-languages](https://docs.cloud.google.com/speech-to-text/v2/docs/multiple-languages) page is Preview, scoped to `long`/`short`/`telephony` in `global`/`us`/`eu`, max 3 codes — it does **not** govern chirp_3; chirp_3's max list length is undocumented.
- **Endpoint**: `f"{location}-speech.googleapis.com"` must match `locations/{location}` in the recognizer path.
- **Blocking the loop**: `speech_to_text` runs on `conn.loop`; never call the sync client directly.
- **Timeout/retry**: pass `timeout=self.timeout` and `retry=None` (or a short `Retry`) so the gapic default policy cannot stall the connection loop beyond one utterance.
- **Startup crash surface**: `__init__` runs at server start, per connection, and on hot reload (§2). No I/O there.
- **Short/empty audio**: manual mode delivers any non-empty frame list; `artifacts` may be `None`.
- **Sync limits**: 60 s / 10 MB; at 32 kB/s the 60 s cap binds first — guard `len(pcm_bytes) > 59*32000`.
- **Dict returns leak JSON to the LLM** — return `str`.
- **`get_current_artifacts()`** reads `self._current_artifacts`, which nothing in base.py sets (`base.py:229-230`) — don't call it.
- **`performance_tester_asr.py:71`** calls `speech_to_text_wrapper` with 3 args and feeds whole WAV bytes (`tests/test_stt.py:11-13`); not acceptance evidence. Same for upstream live `test_stt.py` (§2).
- **Mock-mode pytest** constructs and calls the real provider: no network in `__init__`, no implicit ADC, monkeypatch the client in unit tests.
- **CWD coupling**: `asr.py:18`, `config_loader.py:122-124`, `base.py:276` are all CWD-relative — run from `main/xiaozhi-server`.
- **Docker**: deps live in the base image; the upstream `server_latest` image will `ImportError`. `data/.config.yaml` must be a non-empty mapping.
- **Secrets**: never log key contents; file 0600; never commit `data/`.
- **Pricing** ([pricing](https://cloud.google.com/speech-to-text/pricing), fetched 2026-10-06): V2 "Recognition" SKU 3099-B70F-0949 Standard `$0.016`/min (0–500k), `$0.01`, `$0.008`, `$0.004` by tier; "Speech Recognition (with data logging)" SKU 67F5-A183-E319: 0–60 min/month `$0.00`, then `$0.016` (discount tiers `$0.0144`/`$0.0128`); "without data logging" SKU 60AE-2FE3-C3D8 `$0.024`; dynamic batch `$0.003`; "Each request is rounded up to the nearest increment of 1 seconds". The "Standard¹ models include" footnote lists `default, command_and_search, latest_short, latest_long, phone_call, video, chirp (Speech-to-Text V2 only)` — chirp_3's SKU is UNVERIFIED until the cost check.

## 6. Streaming (deferred)

A `STREAM` provider overrides `receive_audio` (pattern `doubao_stream.py:18-21`, `:69`), the base skips the VAD-stop trigger (`base.py:75`), and it **must implement `_send_stop_request()`** (`listenMessageHandler.py:44-46`). Google v2: first `StreamingRecognizeRequest(recognizer=..., streaming_config=StreamingRecognitionConfig(config=..., streaming_features=StreamingRecognitionFeatures(interim_results=...)))`, then `StreamingRecognizeRequest(audio=chunk)`; per-message 25 KB and "a stream can remain open for up to 5 minutes" are on the [quotas](https://docs.cloud.google.com/speech-to-text/v2/docs/quotas) page; responses carry `speech_event_type` and `results[].is_final`/`stability`/`language_code` ([rpc ref](https://docs.cloud.google.com/speech-to-text/v2/docs/reference/rpc/google.cloud.speech.v2)). Not needed for feat-stt-google acceptance.

## 7. Acceptance checklist

- [ ] `core/providers/asr/google_stt.py`; class `ASRProvider(ASRProviderBase)`; `__init__(self, config: dict, delete_audio_file: bool)` calls `super().__init__()`; `interface_type == InterfaceType.NON_STREAM`; `output_dir` set; `requires_file()` is the base default.
- [ ] From CWD `main/xiaozhi-server`: `create_instance("google_stt", cfg, True)` succeeds with a non-existent `credentials_file` and `use_adc: false` (no I/O in `__init__`).
- [ ] `config.yaml` has `ASR.GoogleSTT` with the §3 keys; `data/.config.yaml` is a non-empty mapping without `manager-api.url`, selects `GoogleSTT`, supplies the key path; `git status` shows no `data/` or key file.
- [ ] `requirements.txt` pins `google-cloud-speech==2.40.0`. In a Python 3.10 venv: `pip install --dry-run -r requirements.txt` resolves (on macOS exclude the `vosk==0.3.45` line, which has no macOS wheel — the remaining set resolved to `protobuf-5.29.6`, `google-api-core-2.30.3`, `torch-2.2.2`; run the unmodified file inside `python:3.10-slim`), and `python -c 'from google.cloud.speech_v2 import SpeechClient'` works.
- [ ] Unit tests (mocked client) pass: LINEAR16/16000/1, recognizer path, `language_codes` passthrough, punctuation feature, `timeout`/`retry` forwarded, multi-result join, empty → `""`, `artifacts is None` → `("", None)`, `GoogleAPICallError` → `("", None)`, `InvalidArgument` → `["auto"]` retry, `close()` safe; `speech_to_text` uses `to_thread` or the async client.
- [ ] `pytest tests/test_stt.py` passes in mock mode with `selected_module.ASR: GoogleSTT` and **no ADC credentials consulted** (no Cloud Console usage after the run).
- [ ] Live smoke (`RUN_LIVE_API_TESTS=1`, own 16 kHz mono clips, header stripped): one en-US and one Mandarin clip return correct text with `["en-US","cmn-Hans-CN"]`, `model=chirp_3`, `location=us`; log shows `language_code` `en-US`/`cmn-Hans-CN`. If `InvalidArgument`, record it and confirm `["auto"]` works.
- [ ] Manual-mode path: a sub-second `listen stop` (frame list) returns `""` without exceptions in the server log.
- [ ] End-to-end on the device: 10 English + 10 Mandarin utterances, transcripts manually judged (DESIGN feat-stt-google); STT→LLM text in the log is plain text, not JSON.
- [ ] Docker: derived image builds; container starts (startup `initialize_asr` succeeds); device completes a round with Google STT; key mounted from `data/`.
- [ ] Cost check: Cloud Console shows Speech-to-Text v2 usage billed to the expected project and SKU after the smoke run.

## UNVERIFIED
- Whether `locations/global` serves `chirp_3`: the chirp_3 page lists only us/eu GA and the languages table has chirp_3 rows only under us/eu, but the official sync-recognize sample uses `locations/global` with `model="chirp_3"`. Keep `us`; the live smoke decides.
- Whether `language_codes=["en-US", "cmn-Hans-CN"]` is accepted together on chirp_3 (only the `["en-US", "fr-FR"]` example is documented) and chirp_3's maximum list length. Smoke tries it first and falls back to `["auto"]`.
- Which pricing SKU chirp_3 bills under: the Standard footnote names `chirp (Speech-to-Text V2 only)` but not chirp_2/chirp_3; resolved only by the Cloud Console cost check.
- Whether `DefaultCredentialsError` fires at `SpeechClient(...)` construction or on the first RPC when no credentials are available; wrap both `_get_client` and `_recognize`.
- Full unmodified `requirements.txt` resolution with google-cloud-speech==2.40.0 on Linux/python:3.10-slim: verified on macOS only with the `vosk==0.3.45` line excluded (no macOS arm64 wheel exists; `jieba` needs sdist builds). Run the unmodified file inside the base image before rebuilding Docker.
- Live behaviour of the `InvalidArgument` -> `["auto"]` retry and of sub-second manual-mode clips against the real API (only the code path is specified; no live call was made).
- Whether the device firmware uses auto or manual listen mode by default (determines which trigger path dominates); not checked in the xiaozhi-esp32 vendor tree for this revision.
