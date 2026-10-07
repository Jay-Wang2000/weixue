# Latency spike — text first-token, direct API vs Agent SDK (2026-10-07)

Measured on this Mac, 2026-10-07 ~17:00–17:38 IST, personal API key. Questions: Q1 "What time is it in Dublin?", Q2 "Is the living room light on?". Per (path, model, question): 1 warm-up + 5 measured runs; median and p90 over the 5 (p90 linearly interpolated, so with n=5 it sits between the 4th and 5th values). Times are client-side, from the request call to the event.

- **First output**: first `text_delta`, or the first `tool_use` block start, whichever comes first.
- **First text**: first `text_delta`. When the model calls a tool first, this includes the tool round trip and a second API call.
- **Input / cache read**: tokens per API request (an SDK turn with one tool call makes two requests).

## Results

| Path | Model | Q | First output med / p90 (ms) | First text med / p90 (ms) | Input tok | Cache read tok | Notes |
|---|---|---|---|---|---|---|---|
| A direct, no tools | claude-sonnet-5-5 | Q1 | 892 / 1753 | 892 / 1753 | 40 | 0 | effort low, adaptive thinking (default). 1 of 5 runs used all 60 tokens on thinking and returned no text (n=4 for text) |
| A direct, no tools | claude-sonnet-5-5 | Q2 | **715** / 743 | **715** / 743 | 38 | 0 | effort low, adaptive |
| A′ direct, no tools | claude-sonnet-5-5 | Q1 | 721 / 765 | 721 / 765 | 39 | 0 | `thinking: between_tools` (thinking off), effort low |
| A′ direct, no tools | claude-sonnet-5-5 | Q2 | 837 / 1607 | 837 / 1607 | 37 | 0 | same; one 1975 ms outlier |
| B direct, no tools | claude-fable-5-1 | Q1 | 2224 / 4463 | 2224 / 4463 | 40 | 0 | effort low, `thinking` omitted (always on). One 5.7 s outlier |
| B direct, no tools | claude-fable-5-1 | Q2 | 2452 / 6352 | 2452 / 6352 | 38 | 0 | same. One 8.8 s outlier |
| A-7 direct + 7 voice tools | claude-sonnet-5-5 | Q1 | 793 / 1963 | 793 / 1963 | 8,685 | 8,577 | decomposition run; 7 stub tools ≈ 24 KB (= today's voice path) |
| A-7 direct + 7 voice tools | claude-sonnet-5-5 | Q2 | 836 / 853 | 2222 / 3678 | 8,683 | 8,577 | 1 `ha_get_state` call every run |
| A-77 direct + 77 tools | claude-sonnet-5-5 | Q1 | 2782 / 3848 | 2782 / 3848 | 91,447 | 91,339 | decomposition run; same 77 stub schemas as C, no SDK |
| A-77 direct + 77 tools | claude-sonnet-5-5 | Q2 | 2369 / 2837 | 5040 / 6744 | 91,445 | 91,339 | 1 `ha_get_state` call every run |
| C Agent SDK, warm client | claude-fable-5-1 | Q1 | 4137 / 5240 | 9734 / 18600 | ~94.5k | ~94k | tools called in 3/5 runs (`ha_get_template`, `WebSearch`, `ha_get_entity`) |
| C Agent SDK, warm client | claude-fable-5-1 | Q2 | 4145 / 6417 | 9510 / 12309 | ~95.5k | ~95k | 1 `ha_get_state` call every run |
| C Agent SDK, warm client | claude-sonnet-5-5 | Q1 | 2475 / 4521 | 4349 / 8184 | ~94.5k | ~94k | tools called in 3/5 runs (`ha_get_template`, `ha_search_entity`, `ha_search_state`) |
| C Agent SDK, warm client | claude-sonnet-5-5 | Q2 | 2431 / 3792 | 5330 / 6302 | ~96k | ~96k | 1 `ha_get_state` call every run |
| C0 SDK cold start | claude-fable-5-1 | — | 926 ms | — | — | — | `ClaudeSDKClient(...)` + `connect()` (CLI spawn → ready, stub MCP connected) |
| C0 SDK cold start | claude-sonnet-5-5 | — | 846 ms | — | — | — | same |

First SDK request of a fresh prefix (cache write ~93k tokens) took 4.6 s to the first token on Fable 5.1 (warm-up, excluded above).

## Setup

- **HA MCP**: `HA_MCP_URL` (LAN address) was unreachable from the host (connect refused within 3 ms; HA is reachable only through the mcp-bridge container's WireGuard tunnel). Path C therefore used a **local stdio stub MCP server**: 77 dummy `ha_*` tools, 264 KB of `tools/list` JSON (ha-mcp 8.6.0 lists 77 tools, 270 KB, per `docs/ha-mcp.md`). `tools/call` returns a fixed JSON state (`light.living_room_ceiling_lights` = off). The real endpoint is HTTP and its schemas differ in content, so its token count will differ from ~91k.
- **Path C options**: `ClaudeSDKClient` with `include_partial_messages=True`, one client per model reused across all 12 turns (one SDK session, so history grows ~0.5k tokens per turn), `effort="low"`, `system_prompt` = the same one-sentence string as A/B, `tools=["WebSearch"]` (no other built-ins), `allowed_tools=["WebSearch","mcp__ha"]`, `permission_mode="dontAsk"`, `strict_mcp_config=True`, `setting_sources=[]`, an isolated `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `max_turns=3`. The init message reported 78 tools (77 MCP + WebSearch): the CLI loaded all MCP schemas up front, with no deferred tool search.
- **max_tokens**: 60 for A/B. For C and the A-7/A-77 runs it was 300: at 60 the SDK turn failed ("response exceeded the 60 output token maximum") once tool calls were in play.
- **Fable 5.1 settings**: `thinking` omitted (only `adaptive` is accepted; `disabled` and `budget_tokens` return a 400). Effort via `output_config.effort="low"`. No sampling params. With the default `display: "omitted"` the first stream event arrived only when text started (TTFB = first text). No server-side `fallbacks` were sent in this spike; production Fable code should send them.
- **Sonnet 5.5 settings**: adaptive (default) or `between_tools`, effort low. `disabled` would return a 400.
- **Not touched**: the live Docker stack and the device. No audio.
- **Spend**: about $3.3 in total: SDK Fable session $1.95 (of which $1.16 was the first-turn cache write), SDK Sonnet $0.49, SDK smoke run $0.29, A-77 ≈ $0.45, everything else < $0.10. Warm per-turn cost: Fable ≈ $0.03 (no tool) / $0.056 (one tool call); Sonnet ≈ $0.02 / $0.04.

## Versions

- Python 3.13.11 (venv `~/Projects/weixue-vendor/spikes/latency/.venv`), macOS 26.6.2
- `anthropic` 1.11.0 (`httpx2` 2.13.1)
- `claude-agent-sdk` 0.2.164, bundled Claude Code CLI 2.1.292
- `mcp` 2.3.0 (installed but unused; the stub speaks JSON-RPC over stdio directly)
- Scripts: `bench_direct.py` (A/A′/B), `bench_direct_tools.py` (A-7/A-77), `bench_sdk.py` (C/C0), `stub_mcp.py`; raw rows in `results_direct.jsonl` and `results_sdk.jsonl` in the same directory

## Conclusion (budget: agent text first-token ≤ 800 ms median; direct path as the floor)

1. Floor: with no tools, direct Sonnet 5.5 reaches the first token in 0.72–0.89 s median (both thinking modes), right at the 800 ms budget. Direct Fable 5.1 takes 2.2–2.5 s with thinking it cannot turn off, so Fable misses the budget on every path.
2. The Agent SDK path misses by 2–5×. First output takes 2.4–2.5 s on Sonnet and 4.1 s on Fable. First text takes 4.3–5.3 s (Sonnet) and 9.5–9.7 s (Fable), because the agent also calls tools for Q1 in 3/5 runs.
3. Most of that cost is the tool surface, not the SDK harness. The direct API with the same 77 schemas (91k tokens, cache hit) needs 2.4–2.8 s, about the same as the SDK. The 7 voice-path tools (8.7k tokens) need 0.79–0.84 s. Cold start is 0.85–0.93 s, paid once per client.
4. Per the brief's red line this triggers the hybrid path. Quick asks go through direct streaming Sonnet 5.5 with the small voice tool set (≈ 0.8 s to the first output, ≈ 2.2 s to the first text after one HA read). Tasks go through the SDK, where Fable 5.1 at 4–10 s is acceptable for long work only.
5. Open: (a) re-run C against the real ha-mcp endpoint from the home LAN or the bridge network, since its token count may not match the stub's ~91k; (b) test SDK tool search/deferred MCP loading, which might cut the 91k-token prefix; (c) measure end-to-end speech-end → first TTS audio (budget ≤ 1.5 s) on the hybrid quick path.
