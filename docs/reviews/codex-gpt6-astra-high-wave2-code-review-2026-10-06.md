## 1. Summary verdict

**Fix-first.** The provider implementations and firmware build safeguards are useful, but the merged stack still has reproducible streaming failures and incomplete turn isolation.

Top five issues:

1. **Dropped Anthropic calls leave tool-index gaps**, causing `IndexError` and losing otherwise valid calls.
2. **An interrupted tool round can resume inside a newer turn**, modifying its dialogue and producing stale responses.
3. **ADR-11’s 90-second deadline is unimplemented**; retries alone can exceed the device’s 120-second channel timeout.
4. **TTS cancellation waits for another cloud response**, blocking synthesis for the next turn.
5. **The reproducible deployment exposes unauthenticated WebSocket/OTA services on all host interfaces**, contrary to the intended physical-audibility trust boundary.

Reviewed Wave 1/2 source at `430014e`; subsequent main changes observed during review did not alter the scoped implementation. No files or services were changed. Python AST and shell syntax checks passed. In-memory reproductions confirmed the tool-index, TTS cancellation/chunking, whitespace-boundary and STT fallback findings below. Full pytest, cloud calls and hardware tests were not rerun. Docker metadata inspection was unavailable because the sandbox denied socket access.

## 2. Correctness bugs

### C1 — P1: Tool indexes count discarded calls

**Location:** [anthropic.py:308](/Users/jwang14/Projects/weixue/server/overlay/core/providers/llm/anthropic/anthropic.py:308), [anthropic.py:330](/Users/jwang14/Projects/weixue/server/overlay/core/providers/llm/anthropic/anthropic.py:330).

An ordinal is allocated at every `tool_use` start. Invalid JSON subsequently discards the call without reclaiming its ordinal. The pinned upstream [merger:1843](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32-server/main/xiaozhi-server/core/connection.py:1843) appends only one placeholder before indexing.

Reproduced using the actual provider method and upstream merger:

| Sequence | Emitted indexes before failure | Result |
|---|---:|---|
| Invalid → valid | `1` | `IndexError` |
| Valid → invalid → valid | `0, 2` | `IndexError` |
| Invalid → `direct_answer` | `1` | `IndexError` |

Upstream catches the stream error and returns before dispatching the collected tools.

**Fix:** Allocate contiguous output indexes only when emitting a call. Track API block indexes separately. Reserve `direct_answer`’s output index at its first emission and reuse it. This is already required by the [amended brief:305](/Users/jwang14/Projects/weixue/docs/briefs/feat-llm.md:305).

### C2 — P1: Old tool work can enter a newer turn

**Location:** Integration inherited through [weixue_main.py:7](/Users/jwang14/Projects/weixue/server/overlay/weixue_main.py:7); upstream [receiveAudioHandle.py:101](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32-server/main/xiaozhi-server/core/handle/receiveAudioHandle.py:101), [connection.py:1351](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32-server/main/xiaozhi-server/core/connection.py:1351).

A second utterance resets `client_abort` and submits another `chat()`. The previous chat may still be waiting on a tool future. On completion, its [result handler:1397](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32-server/main/xiaozhi-server/core/connection.py:1397) uses shared dialogue and `self.sentence_id`; recursive chat also adopts the current shared ID at [connection.py:1075](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32-server/main/xiaozhi-server/core/connection.py:1075).

**Failure:** Interrupt a slow control request with another question. The old action can finish, append history to the new turn, and speak under its sentence ID. Audio filtering alone cannot prevent this.

**Fix:** Give every turn an immutable identity and cancellation state; serialize or explicitly coordinate chat execution. Check identity before dispatch, result handling, history mutation and recursive LLM calls. Already-dispatched actions need an “outcome unknown” state, not automatic retry.

### C3 — P1: TTS abort cannot interrupt a stalled stream

**Location:** [google_tts.py:216](/Users/jwang14/Projects/weixue/server/overlay/core/providers/tts/google_tts.py:216).

`stale()` is checked only after the iterator yields. `_active_call` is assigned after `streaming_synthesize()` returns and is never used by an abort hook. Upstream [abortHandle.py:13](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32-server/main/xiaozhi-server/core/handle/abortHandle.py:13) clears queues and sends `tts stop`, but does not cancel the RPC.

**Confirmed:** With stalled fake calls, the actual `_stream_segment()` stayed blocked after abort both before the first response and between responses. Cancellation occurred only after releasing another response.

**Failure:** Playback stops, but the single TTS worker cannot service the next turn until the old RPC responds or times out.

**Fix:** Expose cancellation independently of response iteration, including during first-response prefetch. Cancel on abort/close and fence the worker by turn identity. Also distinguish unsolicited `Cancelled` errors from deliberate aborts: [line 250](/Users/jwang14/Projects/weixue/server/overlay/core/providers/tts/google_tts.py:250) currently suppresses the terminal marker in both cases.

### C4 — P2: TTS splitting does not enforce its byte limit

**Location:** [google_tts.py:170](/Users/jwang14/Projects/weixue/server/overlay/core/providers/tts/google_tts.py:170).

The splitter moves whole punctuation-delimited pieces between chunks but never subdivides an oversized piece. ASCII periods are absent from its delimiters.

**Confirmed outputs:**

- 5,001 unpunctuated ASCII characters → one 5,001-byte request.
- 1,700 Chinese characters → one 5,100-byte request.
- `"Hello world. "` repeated 500 times → one 6,500-byte request.

These exceed Google’s documented [5,000-byte content limit](https://docs.cloud.google.com/text-to-speech/quotas).

Separately, the inherited [segmenter’s punctuation lists:85](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32-server/main/xiaozhi-server/core/providers/tts/base.py:85) omit English periods. Ordinary declarative English replies can wait until LAST before synthesis begins.

**Fix:** Add a UTF-8-safe hard split after preferred sentence/word boundaries. Implement English sentence segmentation with a bounded buffer, accounting for decimals and abbreviations.

### C5 — P2: Whitespace repair remains dependent on stream boundaries

**Location:** [weixue_patches.py:13](/Users/jwang14/Projects/weixue/server/overlay/weixue_patches.py:13).

The wrapper restores whitespace only when the cleaned and original final characters match. Upstream’s garbage cleaner can mistake legitimate quotes/apostrophes at an intermediate boundary for JSON delimiters, stripping adjacent whitespace too. The consumer then advances `_da_sent` past the removed characters.

**Confirmed with the actual extractor, patched cleaner and consumer cursor logic:** Different splits of `Say 'on' now please.` produce `Sayon' now please.` or `Say 'onnow please.`

Whitespace-only chunks are **not independently an end-to-end defect**: upstream leaves `_da_sent` unchanged when cleaning returns empty, allowing a later chunk to recover them.

**Fix:** Decode the `response` string incrementally and preserve its contents; remove JSON framing at the parser boundary. Do not classify punctuation in arbitrary decoded slices as framing.

### C6 — P2: STT fallback changes language policy for unrelated errors

**Location:** [google_stt.py:138](/Users/jwang14/Projects/weixue/server/overlay/core/providers/asr/google_stt.py:138).

Every `InvalidArgument` retries with `["auto"]`. This includes invalid audio configuration, recognizer/model errors and unsupported features—not just a rejected language combination.

**Confirmed:** An injected `InvalidArgument("unsupported sample rate")` caused a second recognition attempt with `["auto"]`.

**Fix:** Restrict fallback to an explicitly identified language-combination rejection; otherwise report the original failure. Keep both attempts inside the turn deadline.

**Overlong handling is substantially fixed:** [lines 129–131](/Users/jwang14/Projects/weixue/server/overlay/core/providers/asr/google_stt.py:129) reject rather than trim, and [lines 163–197](/Users/jwang14/Projects/weixue/server/overlay/core/providers/asr/google_stt.py:163) intercept the upstream PCM voice-stop path and enqueue a spoken rejection. Remaining gaps are the English-only default response and unbounded accumulation before voice-stop; the limit currently protects the recognition request, not the receive buffer.

### C7 — P1: Flash’s backup check can accept an unrelated device backup

**Location:** [flash.sh:55](/Users/jwang14/Projects/weixue/firmware/flash.sh:55), [flash.sh:65](/Users/jwang14/Projects/weixue/firmware/flash.sh:65).

If no MAC-tagged backup matches, flashing proceeds with any full-sized untagged backup. Its checksum establishes file integrity, not that it belongs to the connected device. Moreover, `shasum -c` validates filenames contained in the sidecar without asserting that it names the selected `.bin`.

**Failure:** A backup from another board satisfies the guard before overwriting the only recoverable image of this board.

**Fix:** Require matching device identity and validate the selected binary’s digest explicitly. Migrate historical untagged backups through a separate verification step.

Neither flash nor backup acquires the [documented device lock](/Users/jwang14/Projects/weixue/docs/METHODOLOGY.md:20). Put locking inside the entry points so concurrent testers cannot interleave serial operations.

### C8 — P2: Setup destroys the previous configuration before validation

**Location:** [setup.sh:41](/Users/jwang14/Projects/weixue/server/scripts/setup.sh:41).

Shell redirection truncates `data/.config.yaml` before `render_config.py` executes. An invalid `TZ_OFFSET`, missing template variable or interrupted process leaves an empty/partial configuration. Permissions are tightened only after successful rendering.

**Failure:** A failed setup leaves the currently running process potentially usable from cached config, but its next restart cannot load configuration. A later image-build failure also leaves newly published config beside the old image.

**Fix:** Render to a mode-0600 temporary file, validate YAML and required settings, build successfully, then publish atomically with rollback and a readiness check.

### C9 — P2: Firmware provenance checks are incomplete

**Location:** [build.sh:63](/Users/jwang14/Projects/weixue/firmware/build.sh:63), [build.sh:68](/Users/jwang14/Projects/weixue/firmware/build.sh:68), [flash.sh:23](/Users/jwang14/Projects/weixue/firmware/flash.sh:23).

The manifest verifies artifacts, but does not bind them to the current patch set, project revision or ESP-IDF revision. Flash compares only the board config against the current checkout.

**Failure:** Switch branches or change firmware patches while retaining the scratch build and unchanged board JSON; flash accepts the previous branch’s image. Managed-component lock drift is merely a warning, and `env.sh` does not verify the IDF pin.

**Fix:** Include source/patch/lock/toolchain identities in build provenance and reject mismatches before flashing. Fail on unintended dependency-lock drift. No firmware source patches are currently tracked, so that build path lacks merged execution evidence.

### C10 — P2: The retained Wave 2 launcher is unsafe to reuse

**Location:** [start-wave2.sh:11](/Users/jwang14/Projects/weixue/scripts/orchestrator/start-wave2.sh:11), [start-wave2.sh:25](/Users/jwang14/Projects/weixue/scripts/orchestrator/start-wave2.sh:25).

It requires associative arrays unsupported by macOS’s stock Bash 3.2. Its backgrounded `cd && nohup …` list also leaves the PID-writing shell in the original directory, while the next line reads the PID from the worktree.

More seriously, it uses `--dangerously-skip-permissions`, contradicting [METHODOLOGY.md:8](/Users/jwang14/Projects/weixue/docs/METHODOLOGY.md:8), and retains obsolete implementation instructions.

**Fix:** Remove or explicitly retire this historical launcher; use the documented permission-scoped workflow.

## 3. Security and secrets handling

**S1 — P1: Unauthenticated network entry remains enabled.**  
[config.template.yaml:5](/Users/jwang14/Projects/weixue/server/config/config.template.yaml:5) disables authentication; [docker-compose.yml:18](/Users/jwang14/Projects/weixue/server/docker-compose.yml:18) publishes 8000/8003 on all interfaces. A reachable network client can submit requests that consume cloud services and use whatever tools its session receives. This bypasses the proposed physical-audibility boundary even before HA integration.

[docs/server.md:17](/Users/jwang14/Projects/weixue/docs/server.md:17) promises firmware-base will enable authentication, but the reproducible configuration still disables it. Plain HTTP/WS also remains the default.

**Required:** Resolve and implement the development network exception or authenticated transport before broadening tool access. Do not treat a supplied MAC header alone as proof of device identity.

**Credential storage has good foundations, but incomplete creation safeguards.**

- The Google key is mounted read-only at [compose:23](/Users/jwang14/Projects/weixue/server/docker-compose.yml:23); secrets and runtime data are gitignored. Documentation now sources keys from an environment file instead of showing key-bearing command lines.
- `setup.sh` does not verify `.env` or service-account permissions. Its post-write `chmod` leaves a first-creation exposure window.
- [backup_flash.sh:19](/Users/jwang14/Projects/weixue/scripts/backup_flash.sh:19) sets no restrictive umask. Full flash backups contain retained NVS/Wi-Fi data and should be private from creation.
- Both principal services disable seccomp at [compose:13](/Users/jwang14/Projects/weixue/server/docker-compose.yml:13) and [compose:31](/Users/jwang14/Projects/weixue/server/docker-compose.yml:31), without a documented necessity.

**Logs retain household content and endpoint credentials.**  
STT logs transcripts at [google_stt.py:152](/Users/jwang14/Projects/weixue/server/overlay/core/providers/asr/google_stt.py:152); TTS logs spoken text at [google_tts.py:212](/Users/jwang14/Projects/weixue/server/overlay/core/providers/tts/google_tts.py:212). Exception messages are logged without central redaction. The MCP bearer URL is deliberately extracted from container logs by [setup.sh:35](/Users/jwang14/Projects/weixue/server/scripts/setup.sh:35).

Upstream file logging retains logs for [30 days](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32-server/main/xiaozhi-server/config/logger.py:105); Compose specifies no Docker log rotation. Define retention and redact credential-bearing URLs before persistence.

**No credential values were found by the tracked-text pattern scan.** Test keys inspected were placeholders, and live credentials are environment-gated. This is not a comprehensive secret-history audit.

## 4. Reliability and operations

**O1 — P1: The 90-second absolute deadline exists only in the spec.**  
[config.template.yaml:47](/Users/jwang14/Projects/weixue/server/config/config.template.yaml:47) supplies a 60-second timeout and one SDK retry. [anthropic.py:256](/Users/jwang14/Projects/weixue/server/overlay/core/providers/llm/anthropic/anthropic.py:256) may then start a fallback request, while subsequent tool rounds receive fresh budgets.

Two timed-out attempts already approach/exceed 120 seconds with backoff. A stream delivering occasional data can outlive a read timeout indefinitely. The firmware’s [channel timeout is 120 seconds](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32/main/protocols/protocol.cc:108).

Implement one monotonic deadline shared by STT, LLM retries/fallback, tools and terminal delivery. Expiry must stop scheduling and fence late results. Heartbeats alone would not implement [ADR-11](/Users/jwang14/Projects/weixue/DESIGN.md:78).

**Restart policy is not recovery validation.**  
`unless-stopped` restarts exited containers; it does not detect a wedged worker or unusable provider. The server and MCP endpoint have no health checks, and `depends_on` supplies ordering rather than readiness. [setup.sh:45](/Users/jwang14/Projects/weixue/server/scripts/setup.sh:45) reports endpoints after detached startup without checking a successful OTA/WS round. Add readiness checks and test endpoint restart, backend restart and credential failures.

**Pinning is partial—not absent.**

- Base, MCP, web, MySQL and Redis images have digest defaults. Overlay SDK versions are exact. The Dockerfile syntax directive resolves the earlier missing-frontend declaration; `COPY --exclude` requires [Dockerfile frontend 1.19 or later](https://docs.docker.com/reference/dockerfile/).
- [Dockerfile:1](/Users/jwang14/Projects/weixue/server/Dockerfile:1) still follows floating frontend `:1`; transitive Python dependencies are not locked, and installation does not run `pip check`.
- Source validation exists only in `setup.sh`; direct Compose builds bypass it and may label the result `unset`. `git status --porcelain` also excludes ignored files, while Docker copies the upstream directory with only `__pycache__` excluded. Ignored runtime configuration/logs could enter an image if the vendor checkout were used operationally.
- [setup.sh:13](/Users/jwang14/Projects/weixue/server/scripts/setup.sh:13) downloads `model.pt` from mutable `master`, checking only file size. Pin its revision and digest.

**Ubuntu migration needs a real bootstrap test.**  
The volume layout is portable, but [setup.sh:39](/Users/jwang14/Projects/weixue/server/scripts/setup.sh:39) assumes the host user can chmod a container-created key. The referenced MCP [Dockerfile](/Users/jwang14/Projects/weixue-vendor/mcp-endpoint-server/Dockerfile:14) has no non-root user: on rootful Linux, a freshly created bind-mounted key can be root-owned and this command fails. Confirm the shipped image user and establish ownership explicitly.

The docs correctly identify firmware address changes and preservation of the MCP key. Also record minimum Compose/BuildKit versions and validate image architecture on the destination. Actual pinned-image metadata was not accessible during this review.

## 5. Test quality

**Meaningful coverage exists.**

- STT tests inspect real protobuf request fields, timeout/retry arguments, credential laziness and overlength rejection.
- TTS tests exercise the upstream queue/encoder path, sentence IDs, PCM framing and stale-response filtering.
- LLM tests exercise dialogue conversion, tool-result ordering, fallback suppression after output and generator closure.
- The whitespace tests extract the actual upstream cleaner rather than maintaining a second implementation.

**Important gaps and misleading assertions:**

| Area | Assessment |
|---|---|
| Tool indexes | [test_llm_anthropic.py:144](/Users/jwang14/Projects/weixue/server/tests/test_llm_anthropic.py:144) copies the merger. It matches this baseline, but misses all three already-specified dropped-call combinations. Load the actual consumer. |
| Cancellation | [test_tts_google.py:355](/Users/jwang14/Projects/weixue/server/tests/test_tts_google.py:355) changes sentence ID while another response arrives. It cannot detect cancellation blocked awaiting that response. |
| Byte splitting | [test_tts_google.py:235](/Users/jwang14/Projects/weixue/server/tests/test_tts_google.py:235) uses frequent Chinese commas, avoiding oversized individual pieces and English periods. |
| Whitespace | [test_weixue_patches.py:29](/Users/jwang14/Projects/weixue/server/tests/test_weixue_patches.py:29) joins cleaned chunks without the real `_da_sent` cursor. It can both miss real corruption and suggest false positives. |
| Short STT | [test_asr_google_stt.py:322](/Users/jwang14/Projects/weixue/server/tests/test_asr_google_stt.py:322) calls the wrapper directly. It bypasses upstream’s [28,800-byte realtime admission gate](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32-server/main/xiaozhi-server/core/providers/asr/base.py:79). |
| STT errors | [test_asr_google_stt.py:168](/Users/jwang14/Projects/weixue/server/tests/test_asr_google_stt.py:168) proves unconditional fallback, not the required language-specific policy. |
| Live LLM | [test_llm_anthropic_live.py:145](/Users/jwang14/Projects/weixue/server/tests/test_llm_anthropic_live.py:145) fabricates a successful light result. It validates API history compatibility, not MCP/HA execution. |
| Latency | [test_tts_google.py:402](/Users/jwang14/Projects/weixue/server/tests/test_tts_google.py:402) measures provider dispatch to encoded audio, not speech-end to audible device output. |

The most circular assertions are tests that merely bless chosen constants or implementation policy—particularly [adaptive-thinking omission:364](/Users/jwang14/Projects/weixue/server/tests/test_llm_anthropic.py:364) and [CJK-cleaner equality:46](/Users/jwang14/Projects/weixue/server/tests/test_weixue_patches.py:46). They are regression checks, not independent correctness evidence. Likewise, receiving RIFF bytes from a fake that always returns RIFF validates plumbing, not Google’s audio contract.

Missing integration coverage includes deadline expiry, concurrent turns, stalled cloud responses, restart/network recovery, rejection playback through actual audio workers, shell failure injection, and fresh Linux bootstrap.

## 6. Spec drift and earlier-review status

| Earlier finding | Current status |
|---|---|
| **C1/F1: wake-word contradiction** | Corrected in [DESIGN:40](/Users/jwang14/Projects/weixue/DESIGN.md:40) and build assertions. **Acceptance open:** [firmware docs:99](/Users/jwang14/Projects/weixue/docs/firmware.md:99) explicitly say 9/10 is not demonstrated. |
| **C2/F2: Mandarin locale and truncation** | Locale and overlong rejection corrected. Google’s [Chirp 3 documentation](https://docs.cloud.google.com/speech-to-text/docs/models/chirp-3) supports the selected identifiers. Conditional fallback remains wrong; short-speech acceptance remains unproved. |
| **C3/F3: fork deferral** | ADR-8 and tasks reconciled. The [firmware brief:7](/Users/jwang14/Projects/weixue/docs/briefs/feat-firmware-base.md:7) still instructs immediate forking. |
| **M1/F4: approval lifecycle** | Substantially specified in ADR-9: ownership, expiry, replay, duplicate decisions and unknown outcomes. Implementation belongs to later tasks; this is specification closure, not runtime verification. |
| **C6/F5: tool indexes** | Amendment added, implementation unchanged. C1 above reproduces the predicted failure. |
| **C4/F6: deadline** | ADR-11 and brief amendment added, implementation absent. |
| **C5/F7: short replies** | Still open. The [STT brief:145](/Users/jwang14/Projects/weixue/docs/briefs/feat-stt-google.md:145) still accepts empty output for sub-second manual speech; realtime admission remains inherited. |
| **M3/F8: integrated acceptance** | Corpus, scoring and latency endpoints now specified; [integration task:113](/Users/jwang14/Projects/weixue/tasks.yml:113) exists but is pending. Its dependencies do not include approvals/proactive tasks although its stated scenarios include their behavior. |
| **F9: proactive prerequisites** | STT and LLM dependencies added. |
| **F10: custom-message preparation** | Build assertion implemented. [On-device custom-message test:91](/Users/jwang14/Projects/weixue/docs/firmware.md:91) explicitly remains unrun. |
| **M4: cancellation** | Still open; C3 confirms the original concern. |
| **M5: deployment baseline** | Local YAML configuration, combined overlay requirements and image build route are documented. Fully reproducible dependencies and clean Linux validation remain incomplete. |
| **F11 / remaining design questions** | Questions are now recorded, not resolved: transport/authentication, retention, cloud location/spend policy, interruption semantics, bedside/mute behavior and M2 durability/permissions. |

Other material drift:

- [tasks.yml:25](/Users/jwang14/Projects/weixue/tasks.yml:25) still describes firmware-base as blocked awaiting connection, despite recorded flashing and voice rounds. Replace the stale blocker with the actual outstanding acceptance items.
- Provider tasks marked `done` do not establish the full [DESIGN acceptance rows](/Users/jwang14/Projects/weixue/DESIGN.md:42): fixed bilingual corpus, control-value/negation accuracy, real HA control and sensor queries remain pending integration.
- ADR-7’s short follow-up window is not implemented in this snapshot. [Recorded acceptance:103](/Users/jwang14/Projects/weixue/docs/firmware.md:103) observed approximately 120 seconds followed by a goodbye, and Chinese canned wake replies remain. The queued timing fix correctly acknowledges this.
- The STT brief still contains the superseded instruction to trim at [line 106](/Users/jwang14/Projects/weixue/docs/briefs/feat-stt-google.md:106). Remove conflicting old instructions rather than relying solely on later amendments.

## 7. Concrete patch list, ordered by value

1. **`server/overlay/core/providers/llm/anthropic/anthropic.py`** — Allocate tool indexes on emission; add the three dropped-call regressions against the actual upstream merger.
2. **`server/overlay/weixue_patches.py` / connection integration** — Introduce immutable turn identity and prevent stale tool results from mutating or speaking in newer turns.
3. **Connection integration and all providers** — Enforce ADR-11’s shared monotonic deadline, including retries, fallback, tools and terminal delivery.
4. **`server/config/config.template.yaml`, `server/docker-compose.yml`, `docs/server.md`** — Implement the approved authentication/network boundary and remove the false promise that firmware-base already enables it.
5. **`server/overlay/core/providers/tts/google_tts.py`** — Cancel RPCs independently of response arrival and unblock the next turn promptly.
6. **`firmware/flash.sh`, `scripts/backup_flash.sh`** — Require device-bound backups, verify the selected file’s digest, acquire the device lock and create backups privately.
7. **`server/scripts/setup.sh`** — Render privately and atomically; validate/build before publishing config; check readiness and preserve rollback.
8. **`server/overlay/core/providers/tts/google_tts.py`** — Enforce UTF-8 byte limits and bounded English sentence segmentation.
9. **`server/overlay/weixue_patches.py`** — Replace chunk-level garbage stripping with framing-aware `direct_answer` decoding.
10. **`server/overlay/core/providers/asr/google_stt.py`** — Restrict language fallback, bound receive accumulation and make rejection language follow the agreed bilingual policy.
11. **`server/Dockerfile`, `server/scripts/setup.sh`, dependency locks** — Lock transitive dependencies/model artifacts, run `pip check`, exclude runtime files and make source validation unavoidable.
12. **`firmware/build.sh`, `firmware/flash.sh`, `scripts/env.sh`** — Bind artifacts to source patches, dependency lock and verified toolchain identity.
13. **`server/docker-compose.yml`, logging configuration** — Add readiness probes, bounded logs and validated Linux volume ownership; remove unnecessary seccomp exceptions.
14. **`scripts/orchestrator/start-wave2.sh`** — Retire the obsolete permission-bypassing launcher.
15. **`server/tests/`, `tasks.yml`, `docs/briefs/`, verification docs** — Add failure-path integration tests and replace stale completion/blocker claims with traceable acceptance evidence.