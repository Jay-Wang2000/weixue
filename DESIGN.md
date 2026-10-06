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

**数据流**：唤醒 → 设备推流 → 后端 VAD/ASR → LLM（函数调用 → MCP 工具 → ha-mcp）→ TTS → 设备播放；每轮结束后端查 ha-mcp `/api/policy/pending`，有待批则推送到设备表情中央。

## Features（对应 tasks.yml）

| ID | 功能 | 验收 |
|---|---|---|
| feat-toolchain | 项目内 venv 装 ESP-IDF（小智要求版本）+ esptool；备份现有 Flash | `idf.py --version` 可用；`flash_backup.bin` 存在 |
| feat-firmware-base | 编译并烧录小智 `esp32-s3-touch-amoled-1.75c` build，OTA_URL 指向自建后端 | 「你好小智」唤醒，设备连上自建后端 |
| feat-server | Docker Compose 跑 xiaozhi-esp32-server（含 OTA、WebSocket、MCP 接入点） | 设备注册成功，能完成一轮对话（先用默认 provider） |
| feat-stt-google | 新增 Google Cloud STT provider：en-US 主 + zh-CN 备选的多语言识别 | 中英各 10 句，转写正确率人工验收 |
| feat-tts-google | 新增 Google Cloud TTS provider（Chirp 3 HD cmn-CN / en-US，流式） | 中英回复可播放，首字延迟 <1.5 s |
| feat-llm | 新增 Anthropic provider（tool use → MCP）+ 系统提示词（家庭语境，英主中辅） | 控灯、查温度、闲聊各通过；中文提问得中文回答 |
| feat-ha-mcp | 后端 MCP 接入点连 ha-mcp；为语音路径调策略；轮换 secret path | 「开客厅灯」免审批执行；不可逆动作被拒 |
| feat-approvals | 每轮查 `/api/policy/pending`，待批推到设备；设备上批准（API 待核实） | 一条 held 调用能在设备上批准并执行 |
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
- **ADR-8 代码托管**：fork `78/xiaozhi-esp32` 与 `xinnan-tech/xiaozhi-esp32-server` 到个人 GitHub（Jay-Wang2000，`github.com-personal` SSH 别名），改动在 fork 上并推送。Flash 先备份再覆盖。
- **ADR-9 设备上批准**：ha-mcp 已有批准机制，优先走 HA 事件总线：监听 `ha_mcp_approval_requested {token, tool_name, args, expires_at}`，用 admin 令牌发 `ha_mcp_approval_response {token, decision, pin}`（面板开启 event decisions 并设 PIN），结果由 `ha_mcp_approval_result` 回报。备选 HTTP：`GET /api/policy/pending`、`POST /api/policy/approve|deny {token}`（挂在 secret path 下，无额外鉴权，故只能由后端持有）。ESP32 只负责显示和点击，批准动作在后端。注意：调用默认阻塞 60 s 后返回 `USER_APPROVAL_REQUIRED`，token 5 分钟内有效，重调一次即消费；队列在内存，重启即丢；不记录批准人。调研：`research/reports/weixue-q7-hamcp-approval.md`。
- **ADR-5 UI**：程序化眼睛而非精灵图/Lottie（无 466 px 素材、性能、烧屏）。

## Testing Strategy

- 后端 provider：pytest，mock Google API + 一次真实冒烟（短音频）。
- 固件：构建通过 + 手动验收清单（唤醒/打断/四态/手势），每项录 10 次。
- 端到端：固定 20 句中英命令脚本，记录识别、延迟、HA 动作结果。

## Open Questions

- 无（2026-10-06）。
