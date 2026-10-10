# feat-agent-service S0/S1 gate (brief rev 4 §13), 2026-10-10

Measured on this Mac, 2026-10-10 21:55–22:20 IST, home LAN (HA reached directly, no tunnel), personal API key from `server/.env`. Script: `server/agent-service/tools/spike_s1.py` (anthropic 1.11.0, mcp 2.3.0, Python 3.12.9); raw rows and request bodies stayed in the session scratchpad. Only read-only HA tools ran, plus one held call on a non-existent entity that was denied. Times are client-side from the request call. Per (config, question): 1 warm-up + 5 measured runs; p90 interpolated as in `latency-spike-2026-10-07.md`. **First output** = first `text_delta` or first `tool_use`/`server_tool_use` block start; **first text** = first `text_delta`.

## Verdict

Items 1–4 (the decision-40 fallback trigger) pass; the in-process loop stays. Items 5 and 6 pass. No `pause_turn` occurred in 20 web tries (reported, as the brief allows); the runner's resume of a paused turn is covered by the unit tests' fake streaming API.

## 1. First token, real ha-mcp tool list (Sonnet 5.5, effort low, quick turn)

ha-mcp 8.6.0 lists 77 tools (270 KB). `count_tokens` with the system prompt and one user message:

| Tool set | Tools in request | Input tokens |
|---|---|---|
| all 77 HA tools loaded (no tool search) | 86 | 102,461 (stub in the 2026-10-07 spike: 91,447) |
| hot 7 loaded, 70 deferred, bm25 tool search, app + web tools, memory | 87 | 18,468 |
| all 77 deferred, bm25 tool search | 87 | 10,402 |
| hot 7 + app + web tools only | 16 | 18,111 |

First output / first text, median (p90), ms, quick-turn tool set **without** the memory tool (see finding A):

| Config | Q1 "What time is it in Dublin?" | Q2 "Is the living room light on?" | Q2 tools | Searches per turn |
|---|---|---|---|---|
| **hot 7 + tool search (production), rerun after finding B** | **604 (663) / 604 (663)** | **685 (876) / 2412 (2556)** | one `ha_search` every run | 0 |
| hot 7 + tool search, first run | 690 (870) / 690 (870) | 689 (756) / 2746 (2950) | `ha_search`, executed twice (finding B) | 0 |
| all deferred + tool search | 677 (742) / 677 (742) | 775 (910) / 3278 (4266) | search, then `ha_eval_template` | 1 |
| hot 7 only, no search | 656 (679) / 656 (679) | 802 (1033) / 2654 (2900) | `ha_search` | — |

Q2's first text includes one real HA read and a second request (first tool call at ~690 ms). Cache reads covered the whole prefix after the warm-up (17,074 of 17,076 tokens).

The other two rows also executed each HA call twice (finding B); their first-output times are unaffected, their Q2 first text includes one extra HA read.

**Finding B (tool runner, anthropic 1.11.0):** with `run_tools_eagerly=False`, the body's `append_messages()` clears the runner's cached `generate_tool_call_response()`, so after the loop body the runner ran every tool call a second time (`_beta_runner.py` `__run__`). For HA writes that is a double execution. The actor therefore runs the runner with `run_tools_eagerly=True` and defers every `tool_use` at its `content_block_start`: nothing runs while the reply streams (a cancelled stream ran nothing), the calls run once in `generate_tool_call_response()`, and the runner reuses their recorded results after `append_messages()`. Unit test `test_free_light_call_runs_inside_the_budget` / `test_e2e_guard_refuses_free_writes_except_the_test_entity` assert one ha-mcp call per tool use.

**Finding A (brief §11 UNVERIFIED, memory tool):** with `memory_20250818` declared, Sonnet viewed `/memories` before answering in every run, including Q1: first text 2081 ms (Q1) and 2644 ms (Q2) median. Decision: the memory tool is declared on task-model requests only (a pure function of the model, brief §6); each turn's context note carries a memory index (path and first line of each file), and a quick turn that must save or read more escalates with `start_task`.

## 2. Cancel and steer on Fable 5.1 (`prefix_mismatch_behavior: "error"`)

- **Stream cancel**: 5 runs, task cancelled at the first text delta, then the stream and the runner generator closed explicitly (`close_runner`): **1 ms median, 1 ms p90**, HTTP response closed in every run. Cancelling only the reading task leaves the runner generator suspended with the response open until garbage collection (one run raised `generator didn't stop after athrow()`), so the actor always closes both.
- **Boundary steer**: after `ha_get_state`, user message `[tool_result, text "Actually, also tell me what time it is."]` plus a separate system note → **200**, no `input_transformations`; the reply answered both.
- **Cancel then tail rewrite**: a second turn sent and cancelled after ~40 characters; the uncommitted tail rewritten once into the same user content plus a correction, then a system `[note: your previous reply was cut off; the user heard: "…"]` → **200**, no `input_transformations`, the reply continued from the heard text.

## 3. Web tools and `pause_turn`

`web_search_20260209` / `web_fetch_20260209` (`max_uses` 5) were accepted by both Fable 5.1 and Sonnet 5.5 (no 400), so both models share one server-tool set. 20 multi-search turns (1 Fable, 19 Sonnet; 7–14 server tool uses each, dynamic filtering via `code_execution`): stop reasons `tool_use`/`end_turn` only, **no `pause_turn` in 20 tries**. One run hit `max_uses`.

## 4. One ADR-9 held HA call end to end (through `HaGateway`, live policy v14 unchanged)

`ha_call_service {domain: input_boolean, service: toggle, entity_id: input_boolean.weixue_s1_probe}` (held by the policy's domain rule; the entity does not exist, so an approval could not change anything):

| Step | Result |
|---|---|
| Call | `PENDING_APPROVAL` returned at **8001 ms**; row `pending` |
| Correlation | owned on the first `/api/policy/pending` poll (exact tool + args, one candidate); `expires_at` from ha-mcp (10 min TTL) |
| Deny via `/api/policy/deny` | original request returned `USER_DENIED` 25 ms later; row `denied`; nothing left pending |

The model-driven path (card, `resolved`, event turn, `done{denied}`) is end-to-end scenario 4.

## 5. Model switch in one conversation

Sonnet quick turn (one `ha_search`) → second message: Sonnet spoke "On it, I'll check today's history and draft it." and called `start_task` + `ha_get_history` → effort-only `high` message, model set to Fable 5.1 on the same runner → Fable answered → third message on Sonnet again (effort-only `low` before it). All requests 200. Sonnet's request after the Fable turn reported `model_binding_mismatch` drops for Fable's two thinking blocks (expected); the Fable request after Sonnet's turn reported none. First Fable request: cache creation 20,501 tokens (per-model cache); the next Sonnet request read 17,795 cached tokens.

## 6. Append-only transcript after an interrupt

The claude-api skill's `prefix_diff.py` over every request body: steer conversation 3 pairs, switch conversation 4 pairs → **0 mismatches, 0 chain breaks**; the cancelled-tail rewrite reports "tail replaced … after the last replayed thinking block — nothing the API checks has changed"; the switch pairs report only `model_switch`.

## Items 7–10

- **7, 8** (cancel/steer races, append-only over a long session): unit suites `test_steer`, `test_cancel`, `test_transcript`, `test_modes` over a fake streaming Messages API at the HTTP layer (the real SDK runner runs unchanged), and the end-to-end run (`feat-agent-service-e2e-2026-10-10.log`).
- **9, basics** (scratch container `agent-smoke` from the built image on the compose network, bind mount under `server/data/`, removed afterwards): the arm64 image runs as uid 1000 and writes the bind mount; WAL on the bind mount survives `docker kill -s KILL` (`integrity_check` ok, journal `wal`, rows intact); RSS 102 MiB after a live turn (`mem_limit` 512m); one live quick turn 2.6 s to the answer with a cold cache (1st request cache write 17,082 tokens). Memory: "Please remember that the bins go out on Thursday" → Sonnet said "Sure, saving that now." (1.5 s), `start_task`, Fable viewed `/memories` and created `bins_day.md`; after a container restart a new chat's "When do the bins go out?" was answered from the per-turn memory index on Sonnet (first text 1.13 s). Four concurrent writers: unit test `test_memory`. Not run: whether ha-mcp executes an approved held call after the client's request dropped (it needs an approval of a real held call; deferred to Jay's approval test).
- **10** (rev 3 items 3–6): HTTP deny route verified live in item 4; approve, the policy surface review and fault tests are in the unit suites with the fake ha-mcp; the physical audio gate stays with the M1/M2 audio sessions.

## Spend

Not metered per request in this script; estimated from token counts at list prices: about $5–7, most of it the 20 web turns and the Fable cancel runs (Fable spent 17–20 s thinking before the first story text at effort high).
