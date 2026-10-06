## 1. Summary verdict

**The architecture is coherent, but the specification is not ready for unconditional implementation sign-off.** Wave 2 has detailed provider briefs; the main blockers are the contracts between those providers, the voice loop, approvals, and hardware acceptance.

Resolve these before declaring M1 complete:

- Reconcile the wake word, STT locale, and deferred-fork instructions.
- Specify approval ownership, expiry, replay, and execution outcomes.
- Fix the LLM brief’s tool-index algorithm and timeout budget.
- Assign an integrated device test and an owner for follow-up listening and interruption during tool execution.

M2 is appropriately deferred, but its session, permission, and durable-task contracts need another specification pass before implementation.

This review assesses the supplied documents and their specified algorithms. It does not claim to have verified implementation, live APIs, or hardware. No files were modified.

## 2. Internal contradictions between DESIGN.md, tasks.yml, CLAUDE.md and the briefs

| Finding | Conflicting text | Consequence |
|---|---|---|
| **C1 — Two incompatible wake-word acceptance criteria** | [DESIGN.md:40](/Users/jwang14/Projects/weixue/DESIGN.md:40): “「你好小智」唤醒”. [tasks.yml:23](/Users/jwang14/Projects/weixue/tasks.yml:23): “只启用 wn9_jarvis_tts”. [firmware brief:143](/Users/jwang14/Projects/weixue/docs/briefs/feat-firmware-base.md:143): “Say ‘Jarvis’ ×10 → ≥9 wakes”. | A correct Jarvis-only build fails the design table. The brief already identifies this inconsistency at line 60; it remains unresolved. ADR-6’s allowance for the model’s actual phrase resolves “Hi Jarvis” versus “Jarvis”, but not the Chinese criterion. |
| **C2 — STT configuration specifies different Mandarin identifiers** | [DESIGN.md:42](/Users/jwang14/Projects/weixue/DESIGN.md:42) and [tasks.yml:31](/Users/jwang14/Projects/weixue/tasks.yml:31): “en-US 主 + zh-CN 备选”. [STT brief:13](/Users/jwang14/Projects/weixue/docs/briefs/feat-stt-google.md:13): `language_codes: ["en-US", "cmn-Hans-CN"]`, explicitly rejecting `zh-CN` for the selected v2 contract. | Implementers following the task description and brief produce different requests. Also, “primary + fallback” is not the same contract as passing both languages together and retrying with `["auto"]`. The separate TTS locale `cmn-CN` is not this error. |
| **C3 — Immediate forks versus an explicit deferral** | [DESIGN.md:61](/Users/jwang14/Projects/weixue/DESIGN.md:61): “fork … 到个人 GitHub … 改动在 fork 上并推送”. [tasks.yml:7](/Users/jwang14/Projects/weixue/tasks.yml:7): “fork 两个上游仓库到个人 GitHub”. [CLAUDE.md:22](/Users/jwang14/Projects/weixue/CLAUDE.md:22): “vendored locally for now; forks come with the remote”. The [firmware brief:7](/Users/jwang14/Projects/weixue/docs/briefs/feat-firmware-base.md:7) still starts with “Fork `78/xiaozhi-esp32`”. | CLAUDE.md supplies a clear temporary exception, but the design, completed toolchain task, and implementation instructions have not incorporated it. The local writable checkout and delivery mechanism remain ambiguous. |
| **C4 — LLM timeout acceptance permits the device to disconnect first** | [LLM brief:102](/Users/jwang14/Projects/weixue/docs/briefs/feat-llm.md:102): `timeout: 60`, `max_retries: 1`; [line 271](/Users/jwang14/Projects/weixue/docs/briefs/feat-llm.md:271): “a 60 s read timeout fails the turn in < 130 s”. [DESIGN.md:79](/Users/jwang14/Projects/weixue/DESIGN.md:79): firmware has a “120 s 信道静默超时”. | A test can pass at 125 seconds although the device has already timed out. SDK retries, provider fallback, and later tool rounds lack one shared deadline. |
| **C5 — Short acknowledgements are required but discarded or accepted as empty** | [DESIGN.md:51](/Users/jwang14/Projects/weixue/DESIGN.md:51): “漏水模拟 → 设备播报 → 「嗯」被识别”. [STT brief:35](/Users/jwang14/Projects/weixue/docs/briefs/feat-stt-google.md:35): realtime audio reaches recognition only above approximately 0.9 seconds. [line 145](/Users/jwang14/Projects/weixue/docs/briefs/feat-stt-google.md:145): a sub-second manual clip must return `""`. | A natural brief “嗯” can disappear before recognition, while the provider’s acceptance checklist still passes. The manual test also fails to distinguish valid short speech from silence. |
| **C6 — The specified LLM algorithm violates its documented consumer contract** | [LLM brief:26](/Users/jwang14/Projects/weixue/docs/briefs/feat-llm.md:26): the merger appends “exactly one placeholder” before indexing. [line 165](/Users/jwang14/Projects/weixue/docs/briefs/feat-llm.md:165) allocates ordinals for every encountered tool; [line 187](/Users/jwang14/Projects/weixue/docs/briefs/feat-llm.md:187) drops invalid tools without reclaiming their ordinal. | If tool 0 has invalid JSON and tool 1 is valid, the first emitted tool has index 1. The merger creates only element 0, then indexes element 1: `IndexError`. Existing tests cover a single dropped call and two valid calls, but not this combination. |

**Approval instructions also need explicit reconciliation, although polling and events are not inherently contradictory.** [DESIGN.md:33](/Users/jwang14/Projects/weixue/DESIGN.md:33) requires checking pending approvals after each turn; [tasks.yml:63](/Users/jwang14/Projects/weixue/tasks.yml:63) specifies event subscription and displaying the approval result; [DESIGN.md:79](/Users/jwang14/Projects/weixue/DESIGN.md:79) additionally requires replaying the original tool call. The documents do not distinguish event delivery, reconciliation polling, permission granted, and action executed.

## 3. Missing requirements or acceptance criteria that will block implementation or testing

**M1 — Approval lifecycle and ownership.**  
[DESIGN.md:62](/Users/jwang14/Projects/weixue/DESIGN.md:62) documents expiring, single-consumption tokens and an in-memory queue; [tasks.yml:55](/Users/jwang14/Projects/weixue/tasks.yml:55) and [line 63](/Users/jwang14/Projects/weixue/tasks.yml:63) divide implementation across two tasks without a shared state machine.

Specify:

- Binding between token, original tool arguments, originating device/session, and expiry.
- Whether approvals originating in Claude Code may appear on the puck.
- Approve, deny, expire, reconnect, restart, duplicate-click, and replay outcomes.
- Whether an early approval completes the original waiting call or requires replay.
- Recovery when execution may have succeeded but its response was lost.

Acceptance must include these failure paths. Showing `ha_mcp_approval_result` is insufficient evidence that the HA action executed.

**M2 — Ownership of ADR-7’s conversation behaviour.**  
[DESIGN.md:60](/Users/jwang14/Projects/weixue/DESIGN.md:60) promises several seconds of follow-up listening and interruption; [tasks.yml:47](/Users/jwang14/Projects/weixue/tasks.yml:47) assigns “ChatGPT 式对话逻辑” to the LLM task. Its brief principally specifies a provider adapter and prompt.

Define the follow-up duration, start event, expiry behaviour, farewell handling, and microphone state. Assign server/firmware ownership. Include interruption while waiting for STT, the first LLM token, TTS’s first chunk, and a tool result—not just interruption during audible playback.

**M3 — Measurable integrated acceptance.**  
[DESIGN.md:42](/Users/jwang14/Projects/weixue/DESIGN.md:42) says transcription accuracy is manually accepted without a passing threshold. [DESIGN.md:43](/Users/jwang14/Projects/weixue/DESIGN.md:43) says first-word latency is below 1.5 seconds, while the [TTS brief:272](/Users/jwang14/Projects/weixue/docs/briefs/feat-tts-google.md:272) measures segment dispatch to first encoded audio, and [line 290](/Users/jwang14/Projects/weixue/docs/briefs/feat-tts-google.md:290) uses a median from one short text per language.

Specify a fixed corpus, transcription scoring, exact preservation of entities/numbers/negation, acoustic conditions, and latency endpoints. Measure provider latency separately from end-of-user-speech to audible device response. Assign one test using Google STT, Anthropic, Google TTS, and ha-mcp together.

**M4 — Cancellation must work without another cloud response.**  
The [TTS brief:192](/Users/jwang14/Projects/weixue/docs/briefs/feat-tts-google.md:192) blocks awaiting the first response before storing `_active_call`; cancellation is checked only after a response arrives. [Line 291](/Users/jwang14/Projects/weixue/docs/briefs/feat-tts-google.md:291) requires stopping synthesis “within one segment”, which has no useful time bound.

Define separate bounds for stopping device playback, abandoning cloud work, and allowing the next turn to progress. Test a stream stalled before its first chunk and between chunks. Sentence-ID filtering prevents stale playback; it does not free a blocked worker.

**M5 — Deployed configuration and combined dependency baseline.**  
The [LLM brief:296](/Users/jwang14/Projects/weixue/docs/briefs/feat-llm.md:296) still marks the server’s single-module versus manager-api mode unknown, although [tasks.yml:17](/Users/jwang14/Projects/weixue/tasks.yml:17) marks server setup done. This choice changes whether YAML provider configuration works.

Record the deployed mode, image build command, source revision, and configuration ownership. Require one Linux image resolving all three new SDKs together. STT’s [line 155](/Users/jwang14/Projects/weixue/docs/briefs/feat-stt-google.md:155) and TTS’s [line 303](/Users/jwang14/Projects/weixue/docs/briefs/feat-tts-google.md:303) explicitly leave dependency resolution checks outstanding.

**M6 — Device behaviour remains underspecified.**  
[DESIGN.md:48](/Users/jwang14/Projects/weixue/DESIGN.md:48) and [line 50](/Users/jwang14/Projects/weixue/DESIGN.md:50) do not define:

- Bedside timezone, exit time, clock source, or behaviour before time synchronization.
- Whether face-down mute stops microphone capture, transmission, playback, or all three; how it resumes.
- What alarm exists for “double-tap to stop”, and who creates it.
- False-positive limits for gestures, beyond ≥9/10 successful intentional attempts.

The [firmware brief:131](/Users/jwang14/Projects/weixue/docs/briefs/feat-firmware-base.md:131) also documents shutdown after five idle minutes while discharging. Decide whether that behaviour is acceptable for proactive notifications and bedside use.

**M7 — M2 lacks an executable task and permission contract.**  
[DESIGN.md:71](/Users/jwang14/Projects/weixue/DESIGN.md:71) names task operations and events but omits request schemas, authentication, idempotency, persistence, event ordering, and restart recovery. It specifies one client per device session, while the [backend report:83](/Users/jwang14/Projects/research/reports/weixue-agent-backend-v1.md:83) recommends one agent session per long task.

Resolve the relationship between voice sessions and task sessions. Specify how pending approval releases execution and later resumes it; an approval callback alone does not define durable suspension. Define event IDs and acknowledgements for the retry queue, including duplicate and stale delivery. Clarify whether SDK approval and ha-mcp approval are separate gates or one coordinated decision.

## 4. Risky decisions with concrete failure scenarios

| Risk | Concrete failure scenario | Required control or evidence |
|---|---|---|
| **Backend holds administrator-equivalent authority** | [DESIGN.md:62](/Users/jwang14/Projects/weixue/DESIGN.md:62) gives the backend an admin event token, approval PIN, and a secret-path HTTP alternative without additional authentication. A compromised backend can approve its own HA requests. Keeping credentials off the ESP32 does not constrain that backend. | State this trust boundary explicitly. Isolate the approval capability from ordinary model/tool execution where feasible; exclude credentials from prompts and logs; authenticate decision requests. |
| **Voice policy changes also affect another client** | The [home-agent report:365](/Users/jwang14/Projects/research/reports/weixue-home-agent-v1.md:365) says Claude Code and the voice backend share one secret URL. Loosening rules for household voice use may also loosen Claude Code’s effective policy; rotating the URL may break its connection. | Define policy scope, approved entity/action bounds, all rotation consumers, and verification/rollback. Retain user confirmation for policy changes as required by [CLAUDE.md:8](/Users/jwang14/Projects/weixue/CLAUDE.md:8). |
| **Unauthenticated speech can trigger allowed actions** | Speaker recognition is explicitly excluded by [DESIGN.md:18](/Users/jwang14/Projects/weixue/DESIGN.md:18). A visitor or television can issue a valid wake word and an allowlisted command. Broad `switch` or `cover` rules can include consequential devices. | Treat physical audibility as the M1 input trust boundary. Specify entity-level exceptions and action/value bounds; domain membership alone is insufficient. |
| **Plaintext provisioning and audio transport** | The [firmware brief:43](/Users/jwang14/Projects/weixue/docs/briefs/feat-firmware-base.md:43) uses HTTP OTA; [line 104](/Users/jwang14/Projects/weixue/docs/briefs/feat-firmware-base.md:104) uses `ws://`. A hostile LAN peer could observe traffic or tamper with provisioning on an intercepted connection. [Line 107](/Users/jwang14/Projects/weixue/docs/briefs/feat-firmware-base.md:107) also documents automatic firmware offers. | Choose an explicit development-only network exception or authenticated encrypted transport. Define device authentication and which firmware sources may be installed. |
| **Silent truncation changes command meaning** | The [STT brief:106](/Users/jwang14/Projects/weixue/docs/briefs/feat-stt-google.md:106) says audio longer than 59 seconds is trimmed. A later qualification—“except the bedroom”, or “actually, don’t”—can be removed before the LLM acts. | Reject overlength commands with a repeat request, or use an explicitly designed segmented flow. Never silently execute a truncated command. |
| **M2 development jobs can escape HA policy enforcement** | [DESIGN.md:71](/Users/jwang14/Projects/weixue/DESIGN.md:71) grants repository development through `claude -p`. The [backend report:54](/Users/jwang14/Projects/research/reports/weixue-agent-backend-v1.md:54) warns that repository hooks/settings may run. Shell access or inherited credentials could reach HA directly, outside ha-mcp. | Define repository allowlists, isolated writable worktrees, permitted tools, environment/secret inheritance, hook/settings policy, and deployment approval. ha-mcp policy does not govern arbitrary shell operations. |
| **Privacy and spend have no operational limits** | The [STT brief:43](/Users/jwang14/Projects/weixue/docs/briefs/feat-stt-google.md:43) documents indefinite WAV retention when deletion is disabled. Its [configuration:68](/Users/jwang14/Projects/weixue/docs/briefs/feat-stt-google.md:68) chooses `us`; the design does not decide residency. Repeated webhook calls, false wakes, or M2 jobs can generate uncapped paid requests. | Specify audio/transcript retention and log redaction, chosen cloud locations, webhook authentication/rate limits, per-task budgets, concurrency limits, and a spend-triggered stop policy. The research’s budget recommendations are not yet requirements. |

There is also an immediate reliability risk already documented in the research: a second utterance can start another `chat()` while the first waits for a tool, and the old operation can continue afterward. See [backend report:36](/Users/jwang14/Projects/research/reports/weixue-agent-backend-v1.md:36). This affects ordinary M1 tool calls; postponing all concurrency work until M2 leaves ADR-7 incomplete.

## 5. Sequencing problems in the task dependency graph

The listed graph is acyclic. Its problems are acceptance prerequisites and missing ownership.

1. **LLM acceptance creates a practical dependency cycle.**  
   `feat-ha-mcp` depends on `feat-llm` at [tasks.yml:56](/Users/jwang14/Projects/weixue/tasks.yml:56), but the LLM task’s [acceptance:257](/Users/jwang14/Projects/weixue/docs/briefs/feat-llm.md:257) requires device speech through MCP execution and spoken confirmation. For the specified HA examples, that requires the later integration. Split provider acceptance using a controlled MCP fixture from real-HA acceptance owned by a subsequent integration task.

2. **Provider tasks can become test-ready before hardware is ready.**  
   STT, TTS, and LLM depend only on `feat-server` ([tasks.yml:32](/Users/jwang14/Projects/weixue/tasks.yml:32), [line 40](/Users/jwang14/Projects/weixue/tasks.yml:40), [line 48](/Users/jwang14/Projects/weixue/tasks.yml:48)), but their briefs require device tests. Keep implementation parallel; add `feat-firmware-base` as a hardware-test prerequisite and serialize flashing/testing on the single puck.

3. **Proactive conversation omits its recognition prerequisite.**  
   [tasks.yml:104](/Users/jwang14/Projects/weixue/tasks.yml:104) depends only on TTS and firmware. Its acceptance includes recognizing “嗯”, requiring STT plus the short-response/listening changes. If “发起对话” includes a conversational answer, LLM integration is also a prerequisite.

4. **Panel and approval tasks have overlapping completion criteria.**  
   [DESIGN.md:49](/Users/jwang14/Projects/weixue/DESIGN.md:49) requires clicking to approve, while `feat-approvals` depends on the panel at [tasks.yml:64](/Users/jwang14/Projects/weixue/tasks.yml:64). Make panel acceptance use fixture cards and emitted UI decisions; make the later approvals task own live authorization, replay, and HA-result verification.

5. **No task owns the final M1 combination.**  
   [DESIGN.md:86](/Users/jwang14/Projects/weixue/DESIGN.md:86) promises a fixed end-to-end script, but `tasks.yml` has no integration gate. Individual branches can pass with different providers, configurations, and images. Add a gate after firmware, all providers, and HA integration, with approval and proactive scenarios added after those features land.

6. **M2 preparation is specified but missing from task-level acceptance.**  
   [DESIGN.md:80](/Users/jwang14/Projects/weixue/DESIGN.md:80) requires `CONFIG_RECEIVE_CUSTOM_MESSAGE` and a panel task-chip reservation. Neither appears in the firmware brief’s [build assertions:138](/Users/jwang14/Projects/weixue/docs/briefs/feat-firmware-base.md:138) or the panel’s [task description:87](/Users/jwang14/Projects/weixue/tasks.yml:87). Add these now. Deferring the four M2 task entries themselves is consistent with the stated plan; their absence is not a current graph defect.

## 6. Specific wording fixes

The following are proposed replacements, not applied edits.

**F1 — Replace the firmware feature row at [DESIGN.md:40](/Users/jwang14/Projects/weixue/DESIGN.md:40):**

```markdown
| feat-firmware-base | 编译并烧录自定义 1.75C build，启用设备端 AEC、唯一 WakeNet 模型 wn9_jarvis_tts 和 CONFIG_RECEIVE_CUSTOM_MESSAGE；OTA_URL 指向自建后端 | “Jarvis” 10 次唤醒成功 ≥9 次；启动日志仅有该 WakeNet 模型；连接自建后端并完成一轮对话；保存实机日志 |
```

**F2 — Replace the STT description at [tasks.yml:31](/Users/jwang14/Projects/weixue/tasks.yml:31):**

```yaml
    description: 新增 Google Cloud STT v2 provider（chirp_3；language_codes 为 [en-US, cmn-Hans-CN]）。语言组合经真实冒烟确认；仅在确认错误由该组合引起时允许一次 [auto] 回退，并记录结果。禁止静默截断超长语音。pytest 使用假客户端；实机验收等待 feat-firmware-base。
```

Replace the corresponding [DESIGN.md:42](/Users/jwang14/Projects/weixue/DESIGN.md:42) row with:

```markdown
| feat-stt-google | Google Cloud STT v2：chirp_3，en-US + cmn-Hans-CN；超长输入明确拒绝 | 固定中英各 10 句，每种语言语义转写正确 ≥9/10；所有控制句的目标、数值和否定词必须正确；另测短应答、空音频和超长输入 |
```

**F3 — Replace ADR-8 at [DESIGN.md:61](/Users/jwang14/Projects/weixue/DESIGN.md:61):**

```markdown
- **ADR-8 代码托管**：当前按 CLAUDE.md 的 remote-deferred 例外工作：共享 weixue-vendor 仅作只读基线，改动在每任务独立的本地 feat/* 工作分支中完成；记录可复现的基线提交与改动。用户批准且 review/test 通过后按临时规则本地合并。个人 remote 建立后创建两个上游 fork，并切换到个人 remote 上的一任务一提交一 PR；不向上游或 main 直接推送功能改动。Flash 覆盖前保存不可覆盖的时间戳备份及校验值。
```

In [tasks.yml:7](/Users/jwang14/Projects/weixue/tasks.yml:7), replace only:

```text
fork 两个上游仓库到个人 GitHub 并作为 submodule/vendor 引入。
```

with:

```text
记录两个上游仓库的本地只读基线与工作副本方式；个人 fork 随 remote 建立后补。
```

**F4 — Replace the data-flow paragraph at [DESIGN.md:33](/Users/jwang14/Projects/weixue/DESIGN.md:33):**

```markdown
**数据流**：唤醒 → 设备推流 → 后端 VAD/STT → LLM → MCP → ha-mcp → TTS。held 调用须在 MCP 的 30 s 超时前返回可关联的结果。后端以 approval-requested 事件实时更新审批状态，并在每轮结束及重连后查询 pending 作对账。只有绑定到本设备原始调用的审批可在设备上裁决；审批通过不等于执行完成。后端根据原调用结果决定是否重放原工具及原参数，并显示执行成功、失败或结果未知。
```

Replace the legacy [DESIGN.md:62](/Users/jwang14/Projects/weixue/DESIGN.md:62) and revised [line 79](/Users/jwang14/Projects/weixue/DESIGN.md:79) ADR entries with one normative ADR:

```markdown
- **ADR-9 设备审批（M1 生效）**：后端持有 HA 凭据；设备仅发送与当前审批卡关联的决定。策略 wait_seconds 设为 5–10 s，策略变更须用户确认。记录 token、原始工具及参数、来源设备/会话、到期时间和状态。事件总线为主路径，HTTP 为后端备选；两条路径共享去重和状态校验。拒绝、过期或未知 token 不得执行；重复决定不得重复执行。若原等待调用已执行则不得重放；若返回 USER_APPROVAL_REQUIRED，则批准后重放原调用以消费 token。执行结果未知时不得盲目重试。验收覆盖早到批准、延迟批准、拒绝、过期、重复点击、断连和 HA/后端重启。
```

**F5 — Replace the tool-index allocation rule and streaming outline at [LLM brief:155](/Users/jwang14/Projects/weixue/docs/briefs/feat-llm.md:155), through its explanatory paragraph at line 196:**

```markdown
**Streaming tool-index contract:** Track API block identity separately from the index exposed to `_merge_tool_calls`. Allocate an output index only when a tool is first emitted; emitted tools must use contiguous indices starting at zero. A real tool is emitted only after its complete arguments parse successfully. A dropped tool consumes no output index. `direct_answer` reserves an index on its first emitted event and reuses it for every delta. Text/thinking blocks consume no tool index.

Acceptance must feed the actual consumer with: invalid first tool followed by a valid tool; valid/invalid/valid tools; and a dropped real tool followed by streamed `direct_answer`. No case may raise `IndexError` or execute the dropped call.
```

**F6 — Replace timeout acceptance item 19 at [LLM brief:271](/Users/jwang14/Projects/weixue/docs/briefs/feat-llm.md:271):**

```markdown
19. M1 uses a proposed 90 s absolute deadline per foreground voice turn, shared by SDK retries, provider fallback and tool rounds. Individual request timeouts do not reset it. On expiry, stop scheduling new work, prevent late results from entering a newer turn, and send a terminal device state before the documented 120 s channel timeout. Test no-first-token, stalled-stream, fallback and tool-wait cases. An action already dispatched may have an unknown outcome and must not be reported as cancelled or automatically retried.
```

**F7 — Replace the manual short-audio acceptance at [STT brief:145](/Users/jwang14/Projects/weixue/docs/briefs/feat-stt-google.md:145):**

```markdown
- [ ] Short-input acceptance distinguishes silence from speech: empty/silent input causes no action; valid sub-second “嗯”, “yes” and “no” clips reach recognition in the selected listen mode. Device tests achieve ≥9/10 correct acknowledgements per phrase without padding the recording to bypass the VAD threshold.
```

**F8 — Replace the final testing bullet at [DESIGN.md:86](/Users/jwang14/Projects/weixue/DESIGN.md:86):**

```markdown
- 端到端：新增 feat-voice-integration 验收门，使用同一固件、同一 Docker 镜像及 Google STT + Anthropic + Google TTS + ha-mcp 完成固定中英各 10 句；记录版本、配置模式、转写、回复语言、HA 结果、语音结束到设备首个可听音频的延迟，以及 TTS 分段提交到首帧的独立延迟。加入短应答、打断、服务超时、网络恢复与审批异常场景。硬件测试等待 feat-firmware-base，并按 CLAUDE.md 保存实机证据。
```

**F9 — Replace the proactive dependency at [tasks.yml:104](/Users/jwang14/Projects/weixue/tasks.yml:104):**

```yaml
    depends_on: [feat-tts-google, feat-stt-google, feat-llm, feat-firmware-base]
```

Replace its description at line 103 with:

```yaml
    description: 后端提供经鉴权、限流和去重的 webhook，支持 HA 自动化触发播报或发起对话；明确设备忙碌、离线、静音时的交付规则；拥有短应答监听窗口及其端到端验收。
```

**F10 — Replace the M2 preparation sentence at [DESIGN.md:80](/Users/jwang14/Projects/weixue/DESIGN.md:80):**

```markdown
- feat-firmware-base 必须在最终 sdkconfig 验证 CONFIG_RECEIVE_CUSTOM_MESSAGE=y，并在实机确认合法 custom 消息可达、未知消息不影响语音；将该项加入固件 brief 验收。feat-ui-panel 使用 fixture 数据预留任务芯片和审批卡，输出关联的 UI 决定；真实审批执行由 feat-approvals 验收。
```

**F11 — Replace “无” at [DESIGN.md:90](/Users/jwang14/Projects/weixue/DESIGN.md:90):**

```markdown
- M1 冻结前待定：审批归属与重放状态机；免唤醒追问时长；工具等待期间的打断策略；静音/床头/电池关机语义；网络鉴权与传输边界；数据保留、云区域及费用上限；统一端到端验收负责人。
- M2 排程前待定：语音会话与任务会话关系；任务/API/event schema、持久化与恢复；SDK 与 ha-mcp 双层审批映射；开发子进程的仓库、工具、凭据、hooks 和部署权限边界。
```