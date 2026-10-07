# weixue — Design (plan spec v0.1, 2026-10-06)

> 状态：v0.2，2026-10-06 用户已回答开放问题，冻结。调研依据：`~/Projects/research/reports/weixue-home-agent-v1.md`（§11.4–11.5 为已定决策）。

## Overview

**问题**：把微雪 ESP32-S3-Touch-AMOLED-1.75C 做成家庭语音 agent 的前端：唤醒词唤醒、中英对话、通过 MCP 控制 Home Assistant、圆屏上有会表情的脸。

**目标**
- 板子跑小智固件（1.75C build，设备端 AEC），连接自建后端。
- 后端：xiaozhi-esp32-server（Docker），STT/TTS 全部走 Google Cloud，LLM 用 Anthropic Claude（新 provider）。
- HA 接口：ha-mcp（现有组件），策略按语音路径迭代；待批审批在设备上显示并批准。
- UI：Cozmo 式程序化眼睛 + 语音状态环 + 床头模式 + 左滑监控面板。
- IMU 手势：拿起亮屏、翻面静音、双击止闹。

**非目标**
- 不用 HA Assist 管线、不做 ESPHome 版本。
- 不做自定义唤醒词训练、不做说话人识别。
- 第二台 ESP32 不在范围。

## Architecture

```
[1.75C 板子: 小智固件]
  ESP-SR 唤醒(Hi Jarvis, ADR-6)+AEC → Opus/WebSocket → [xiaozhi-esp32-server @ Mac(Docker)]
  LVGL 脸/状态环/面板 ← llm emotion / 状态消息          ├ ASR: Google Cloud STT (新 provider)
  QMI8658 手势 → 设备侧 MCP 工具                          ├ LLM: Anthropic Claude (新 provider, claude-sonnet-5-5 默认, haiku-4-5 低延迟备选)
                                                          ├ TTS: Google Cloud TTS Chirp 3 HD (新 provider)
                                                          └ MCP 接入点 → ha-mcp @ HA Green:9584
[HA 自动化] → webhook → 后端 → 设备播报 / 发起对话（主动编排）
```

**数据流**：唤醒 → 设备推流 → 后端 VAD/STT → LLM → MCP → ha-mcp → TTS。held 调用须在 MCP 30 s 超时前返回可关联的 token。后端在有 pending 时每 2 s 轮询 `/api/policy/pending` 更新审批状态，并在每轮结束及重连后对账（决定 18 = HTTP 路线，事件总线留作后续推送）。只有绑定到本设备原始调用的审批可在设备上裁决；审批通过不等于执行完成：后端按原调用结果决定是否重放原工具与原参数，并显示成功、失败或结果未知。

## Features（对应 tasks.yml）

| ID | 功能 | 验收 |
|---|---|---|
| feat-toolchain | 项目内 venv 装 ESP-IDF（小智要求版本）+ esptool；备份现有 Flash | `idf.py --version` 可用；`backup/flash-<timestamp>.bin` 存在 |
| feat-firmware-base | 编译并烧录自定义 1.75C build：设备端 AEC、唯一 WakeNet 模型 wn9_jarvis_tts、CONFIG_RECEIVE_CUSTOM_MESSAGE=y；OTA_URL 指向自建后端 | “Jarvis” 10 次唤醒成功 ≥9；启动日志仅该一个 WakeNet 模型；连接自建后端并完成一轮对话；实机确认合法 custom 消息可达、未知消息不影响语音；保存实机日志 |
| feat-server | Docker Compose 跑 xiaozhi-esp32-server（含 OTA、WebSocket、MCP 接入点） | 设备注册成功，能完成一轮对话（先用默认 provider） |
| feat-stt-google | Google Cloud STT v2：chirp_3，language_codes [en-US, cmn-Hans-CN]；超长输入明确拒绝而非静默截断 | 固定中英各 10 句，每种语言语义转写正确 ≥9/10；控制句的目标、数值、否定词必须正确；另测短应答（嗯/yes/no）、空音频、超长输入 |
| feat-tts-google | 新增 Google Cloud TTS provider（Chirp 3 HD cmn-CN / en-US，流式） | 中英回复可播放，首字延迟 <1.5 s |
| feat-llm | 新增 Anthropic provider（tool use → MCP）+ 系统提示词（家庭语境，英主中辅） | 控灯、查温度、闲聊各通过；中文提问得中文回答 |
| feat-ha-mcp | 后端 MCP 接入点连 ha-mcp；为语音路径调策略；轮换 secret path | 「开客厅灯」免审批执行；不可逆动作被拒 |
| feat-approvals | pending 期间每 2 s 查 `/api/policy/pending`，待批推到设备审批卡；设备决定经 HTTP `/api/policy/approve|deny`（既有 secret path）；只裁决能证明属于本设备的 token | 一条 held 调用能在设备上批准并执行；他人发起的 held 调用只读显示 |
| feat-ui-face | LVGL 程序化眼睛（闲/听/想/说 + 情绪），替换默认 emoji 显示 | 四态可辨，眨眼/视线漂移，待机 ≤15% 亮像素 |
| feat-ui-ring-bedside | 语音状态环；夜间床头模式 | 听态有环；22:00 后暗红时钟页 |
| feat-ui-panel | 左滑监控面板：待批审批、后端连接、电量、最近一句转写 | 左滑/右滑切换，点击批准 |
| feat-imu | QMI8658 驱动 + 拿起亮屏、翻面静音、双击止闹 | 三个手势各 10 次触发 ≥9 次 |
| feat-proactive | 后端 webhook：HA 自动化可让设备播报或发起对话 | 漏水模拟 → 设备播报 → 「嗯」被识别 |

## Technical Decisions

- **ADR-1 固件**：小智（官方 1.75C 支持、AEC、中文唤醒、MIT）。否决 ESPHome（无 AEC、无中文唤醒）、自研 ESP-IDF（工作量）。
- **ADR-2 HA 接口**：ha-mcp 策略引擎；内置 `mcp_server` 无策略，不作备选。
- **ADR-3 语音服务**：Google Cloud STT+TTS（中英、凭据已就绪、零维护）；自托管留作后续。
- **ADR-4 后端宿主**：开发期 Mac（Docker Compose）；后期迁到家里 Ubuntu 主机，配置与数据卷保持可迁移。
- **ADR-6 唤醒词**：单一英文唤醒词 `Hi Jarvis`，用 ESP-SR 现成 WakeNet 模型（`wn9_jarvis_tts`，sdkconfig `CONFIG_SR_WN_WN9_JARVIS_TTS=y`；若乐鑫模型实际短语为 "Jarvis" 则以模型为准）。不用 MultiNet 自定义短语、不走付费定制、不加第二个唤醒词。调研：`research/reports/weixue-q3-wakeword.md`。
- **ADR-7 对话逻辑**：仿 ChatGPT 语音：英语为主、中文为辅；STT 多语言识别，LLM 用用户所用语言回答，TTS 随之切换声音；回答结束后保持数秒免唤醒追问；支持打断（设备端 AEC）。
- **ADR-8 代码托管**：按 CLAUDE.md 的 remote-deferred 例外工作：共享 `weixue-vendor` 只作只读基线，改动在每任务的本地 `feat/*` 分支完成并记录基线提交；review/test 通过后本地 `--no-ff` 合并。个人 remote 建立后创建两个上游 fork，切换到一任务一提交一 PR；不直接推送 main。Flash 覆盖前保存带时间戳和校验值的备份。
- **ADR-9 设备审批（2026-10-07 修订，决定 18 = B）**：后端持有 HA 凭据；设备只发送与当前审批卡关联的决定。ha-mcp 的同步挂起在 xiaozhi-server 内走不通（接入点执行器写死 30 s；进度通知两跳丢弃；拉长等待撞固件 120 s 信道静默），故**时限放在我们自己的层，不改 ha-mcp 策略的 `wait_seconds`**。唯一契约：每个 HA 调用自设约 8 s 预算，超时即向 LLM 返回「待批准 / 结果待定」（超时只说明结果未到，不证明 HA 在等审批），同时保持对 ha-mcp 的原始请求继续等待（最长至 `wait_seconds`）；裁决经 ha-mcp 的 HTTP `/api/policy/approve|deny`（既有 secret path）；有 pending 时每 2 s 轮询 `/api/policy/pending` 做关联与对账；事件总线仅作日后可选的推送优化。记录 token、原始工具及参数、来源设备/会话、到期时间、状态；只有能证明属于本服务原始调用的 token 可在设备上裁决，歧义匹配只读；拒绝/过期/未知 token 不得执行；重复决定不得重复执行；从 pending 列表消失不等于已执行；结果未知时不得盲目重试。实现归属：agent 路径由 feat-agent-service 实现（服务内进程内适配器，经 HTTP 直连 ha-mcp）；feat-llm-claude-agent 接管语音 LLM 路径之前，现有 mcp-bridge 在旧语音路径上保持现有行为。验收覆盖早到批准、延迟批准、拒绝、过期、重复点击、断连和 HA/后端重启。调研：`research/reports/weixue-q7-hamcp-approval.md`。
- **ADR-5 UI**：程序化眼睛而非精灵图/Lottie（无 466 px 素材、性能、烧屏）。

## Milestone M2 — agent 化（2026-10-07 Jay 决定：M1 收尾后立即开始；调研：`research/reports/weixue-agent-backend-v1.md`，UI 设计稿 `research/reports/weixue-agent-ui-v1.md`）

xiaozhi-server 的对话循环是深度 ≤5、每工具 30 s、同步阻塞、180 s 无声断连的受限 loop，做不了分钟级多步任务。M2 把它退化为语音网关，新增独立 agent 服务并补齐设备协议与 UI。

| ID | 功能 | 验收 |
|---|---|---|
| feat-agent-service | Claude Agent SDK 服务（每个 chat 一个 SDK session，会话索引持久化；直接调用 ha-mcp（办公室模式下与 mcp-bridge 一样加入 wg 命名空间，不设中继）；暴露全部 ha-mcp 工具，安全决策只在 ha-mcp policy；ADR-9 的 pending/异步审批由本服务内的进程内适配器实现（经 HTTP 直连 ha-mcp），不改 ha-mcp wait_seconds；SDK WebSearch/WebFetch 启用；`permission_mode=default` + `can_use_tool` 只管非 HA 工具）；任务 API `POST /tasks`、SSE 事件 `progress|needs_input|needs_approval|done|failed|cancelled`、`answer|approve|cancel`；`dispatch_dev_job` 不在 M2（另立后续任务 feat-dev-dispatch） | 「把客厅灯做成日落自动化」跨 ≥3 工具、>30 s，期间设备可被唤醒问天气且不并发 `chat()`，完成后设备收到 done |
| feat-llm-claude-agent | xiaozhi provider `type: claude_agent`：短问直答，长任务建 task 后立即应答并发 LAST；文本流 + 侧信道事件；长任务不依赖连接保活（结果经 WHEN_IDLE 投递）；MCP 接入点执行器透传超时 | 5 min 无语音任务在设备断连后仍完成，结果经 WHEN_IDLE 投递到达 |
| feat-protocol-agent-msg | 服务器→设备 `agent` 消息 `{task_id,state,summary,progress,options,scheduling}`（先借 `custom`，固件开 `CONFIG_RECEIVE_CUSTOM_MESSAGE`）；后端通知重投队列 | 设备忙时的通知回 Idle 后 10 s 内送达 |
| feat-agent-monitor | 后端监控台（Jay 2026-10-07）：agent 服务的事件库（SQLite，与任务库同源）+ Web UI——已执行动作时间线（工具调用/参数摘要/结果、审批与裁决、谁/何时）、token 与费用（按会话/任务/天，来自 SDK usage）、家庭状态快照（经 ha-mcp 只读工具缓存的关键实体）、连接（设备 WS 会话、agent 服务与 ha-mcp 直连、bridge/endpoint/隧道健康）、MCP 状态（ha-mcp 可达、工具数、pending 审批（含非本服务归属的）、策略版本）、chats 列表、错误；只读 UI，不执行动作；仅绑定 127.0.0.1（决定 30）；后端迁到家中 Ubuntu 后经 Tailscale 访问，不公网暴露 | 一次长任务后监控台显示其全部工具调用与 token；拔掉设备/断隧道后 30 s 内连接与 MCP 状态变红；迁到 Ubuntu 后手机经 Tailscale 可打开（Mac 期间本机打开） |
| feat-ui-sessions | 会话切换（Jay 2026-10-07，决定 27）：在现有横滑进入的单一管理视图内选择 chat，不设独立会话页、不设上滑列表——每个 chat 显示标题（首句/自动摘要）、最近活动时间、状态芯片（进行中/需批准/有未读）、当前会话高亮；点按切换、「新对话」入口；语音同义：「new chat」「switch to <title>」「back to the last chat」；切换时服务端 `resume` 对应 SDK session，设备 UI 在 1 s 内显示所选会话的最近一轮摘要；列表来自 agent 服务的会话索引（持久化，重启后仍在） | 5 个 chat 间来回切换 10 次，每次语音追问落在正确会话；重启服务后列表与当前会话不变 |
| feat-ui-agent-states | 新状态：后台工作中 / 需澄清 / 需批准 / 有未读结果；澄清与批准为中央整屏卡；语音非选项答案经 `response` 回传；任务芯片；任务列表、chats 切换与 HA 状态/审批同在现有横滑的单一管理视图内（决定 27，无左滑任务列表）（参考 Muse 头像状态行 / Goals 页 / 批准卡） | 四态可辨，卡片按钮与语音都能应答（语音能否批准待 brief §14 Q1） |

依赖：feat-llm、feat-ui-face、feat-ui-panel、feat-approvals。结果交付三档借 Gemini Live 的 `INTERRUPT | WHEN_IDLE | SILENT`。

- **ADR-11 长任务的对话模型（2026-10-07 重写，决定 36）**：不设独立的 90 s 期限；语音轮按正常时序结束（回答后 8 s 无语音即收尾），长任务在同一个 Claude 会话中作为进行中的轮次继续，结果完成后按投递档位报回；用户随时唤醒追问进度或改方向——该消息打断进行中的轮次（同 Claude Code 的 interrupt），agent 从当前状态继续；对设备信道约 120 s 的静默超时，用“先说一句简短确认”保证，而非计时器。已派发的动作结果未知时不得报为已取消或自动重试。
- **ADR-12 电源策略**：上游固件 PowerSaveTimer 在空闲 300 s 后经 PMIC 关机，即使 USB 供电也会关——这是设备反复“从 USB 消失”的原因。决定：USB 供电时禁用自动关机；空闲 60 s 调暗、300 s **关闭屏幕**（AMOLED 全黑，MCU 与唤醒词检测保持运行），唤醒词、拿起（IMU）或触摸点亮；电池供电时保留空闲关机，时长可配。热修复 fix-power-usb 负责去掉关机；300 s 关屏若未随热修复实现，由 feat-ui-ring-bedside 补。
- **ADR-10 agent 层**：Claude Agent SDK 自托管；Managed Agents 留作不碰 HA 的 side project 后备；不采用 LangGraph/Pydantic AI/Pipecat。2026-10-07 Jay 决定：模型用 `claude-fable-5-1`（成本不是约束，响应性与稳定性优先，保留 fallback）；多会话——设备上可切换 chat，每个 chat 对应一个 SDK session（原生 resume/history），5 min 无语音任务的连接断开可接受，任务与会话状态在 agent 服务侧持久化；记忆——以 SDK 自带机制（CLAUDE.md/settings 加载、项目记忆目录）为主，应用层只补会话索引；鉴权：SDK 文档（code.claude.com/docs/en/agent-sdk/overview）明确 SDK 构建的 agent 不得使用 claude.ai 登录，须用 API key；且本机 `claude` 登录是 Yahoo 企业账号——故 agent 服务用 Jay 的个人 ANTHROPIC_API_KEY，唯一来源 `server/.env`（决定 16），只以该服务专属的环境变量注入容器。SDK 事实：session 持久化为 `~/.claude/projects/<cwd>/*.jsonl`，`resume`/`continue_conversation`/`fork_session`；CLAUDE.md、`.claude/rules/` 与项目记忆目录按 `setting_sources` 加载；`model`/`fallback_model`/`max_turns`/`max_budget_usd` 可设；进程内 `@tool` + `create_sdk_mcp_server`，外部 MCP 经 `mcp_servers`，`can_use_tool` 作审批门。待实现时核实：流式模式下的中途打断（interrupt）与 partial message 事件。
- feat-firmware-base 必须在最终 sdkconfig 验证 `CONFIG_RECEIVE_CUSTOM_MESSAGE=y`，并在实机确认合法 custom 消息可达、未知消息不影响语音。feat-ui-panel 用 fixture 数据预留任务芯片和审批卡并输出 UI 决定；真实审批执行由 feat-approvals 验收。

## Testing Strategy

- 后端 provider：pytest，mock Google API + 一次真实冒烟（短音频）。
- 固件：构建通过 + 手动验收清单（唤醒/打断/四态/手势），每项录 10 次。
- 端到端：`feat-voice-integration` 验收门——同一固件、同一 Docker 镜像，Google STT + Anthropic + Google TTS + ha-mcp 一起完成固定中英各 10 句；记录版本、配置模式、转写、回复语言、HA 结果、语音结束到设备首个可听音频的延迟，以及 TTS 分段提交到首帧的独立延迟；加入短应答、打断、服务超时、网络恢复、审批异常场景；实机证据按 CLAUDE.md 保存。

## Open Questions

来自 2026-10-06 的外部评审（`docs/reviews/codex-gpt6-astra-high-spec-review-2026-10-06.md`）：

- M1 冻结前待定：免唤醒追问的时长、起点、麦克风状态与归属（服务器/固件）；工具等待期间的打断策略（现有 `abort` 打不断工具且会并发第二个 `chat()`）；静音/床头/电池 5 分钟关机的语义；OTA/WS 明文传输与设备鉴权的开发期例外；音频/转写保留、云区域、费用上限与 webhook 鉴权限流；语音输入无说话人识别——以“物理可听”为 M1 信任边界，策略按实体级而非域级收紧。
- M2 排程前待定：语音会话与任务会话的关系；任务 API/event schema、持久化、幂等、重启恢复；SDK 审批与 ha-mcp 审批是双门还是一次决定；开发子进程的仓库、工具、凭据、hooks 与部署权限边界。
