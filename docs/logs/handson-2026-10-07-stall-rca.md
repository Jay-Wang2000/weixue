# "Tell me a story" stall: root cause (2026-10-07)

## Summary
The story turn was blocked inside the Anthropic HTTP call. The first attempt hung for the full 60 s client timeout, and the SDK then retried once without logging it. The abort at 10:41:32 released nothing: the retry fired on its own about 4.4 s later. Its reply was then dropped from audio but still written to history.

## Timeline (Dublin)
| Time | Event |
|---|---|
| 10:40:35.719 | STT "Tell me a story." chat() starts and the first HTTP send blocks |
| 10:40:40.52 | A connection tears down (unrelated, see below) |
| 10:40:55 | Face goes thinking→speaking on the device's 20 s timer (serial 282→288, 42067→62067 ms) |
| 10:41:32.175 | Abort: flags set, `tts stop` sent |
| ~10:41:35.7–35.8 | 60 s timeout fires; SDK sleeps 0.375–0.5 s and retries |
| 10:41:36.24 | New STT turn resets `client_abort` and `sentence_id` |
| 10:41:36.624 | Story retry and time request both created at the API |
| 10:41:38–39 | Time turn speaks a reply that answers both messages; story retry ends `tool_use` (`direct_answer`); its text is dropped from TTS but kept in history |

## Root cause
**Confirmed**
- The turn blocked at `self.client.messages.stream` (overlay `anthropic.py:300`), below `_base_client.py:1303`.
- The SDK retried once without logging it.
- Not involved: tools, MCP, memory (`nomem`), A's teardown, and the abort.

**Most likely, not proven**
- The first attempt was a 60 s timeout before any headers came back (connect, TLS or first byte).
- A retryable status with `retry-after` of about 60 s would give the same timeline, because the SDK honours it with no cap (`_base_client.py:~832-838`).

**Unexplained**
- Why the first attempt stalled (dead keep-alive socket, hotspot/NAT, or an API-side stall).
- Why two independent HTTP/1.1 requests were created in the same millisecond.

## Evidence
- **Config.** `server/data/.config.yaml:56-57` sets `timeout: 60` and `max_retries: 1`. `anthropic.py:87-88` turns the 60 into a float that applies to each operation separately. The container's md5 hashes match the local files.
- **Retry path.**
  - Timeout becomes `APITimeoutError` at `_base_client.py:1308-1310`.
  - It is retryable at `:907-908`.
  - The retry sleep is at `:842-849`.
  - Its `log.info` at `:1358` is dropped by level: `ANTHROPIC_LOG` is unset, so the logger stays at root WARNING (`_utils/_logs.py:17-25`).
  - The overlay's give-up line (`anthropic.py:276-277`) and fallback line (`:273`) never appear.
- **Request IDs.** These decode as UUIDv7 (version 7 on all 14 checked). Every other turn's ID lands 0.24–0.44 s after chat() starts. The story ID `req_011Cfnf5ztuFq9Du7uN95iPU` lands about 60.9 s after.
- **Snapshot.** The story request has 4363 input tokens and the time request 4374. So the story request is the 10:40:35 dialogue re-sent (`connection.py:1131-1137`).
- **No tool log.** The `direct_answer` branch (`connection.py:1270-1299`) never calls `handle_llm_function_call`, so there is no `执行工具` line (`unified_tool_manager.py:95`).
- **Stale history.**
  - `google_tts.py:104` drops the text because its `sentence_id` is stale, but `connection.py:1298-1299` still writes it to history.
  - The next turn's input jumps 4374→4649 (+275). The time reply alone would add about 125, so the token counts support the stale entry. This is loose, because thinking tokens are counted in output but not stored.
- **A's teardown ruled out.**
  - `close()` (`connection.py:1543-1660`) never touches `self.llm`.
  - Each connection has its own `ServerMCPExecutor` (`unified_tool_handler.py:32`), and cleanup at `:228-242` is per connection.
  - The same teardown pattern at 10:39:51 (server 59-62) was harmless.
  - The device tag does not tell A from B. The teardown is attributed to A because B kept working afterwards.

## Why it unblocked at abort
It did not. `abortHandle.py:12-19` only sets a flag, clears queues and sends `tts stop`. `client_abort` is checked only between stream events (`connection.py:1157`), and `receiveAudioHandle.py:101` cleared it before the retry's first event. The release lines up with timeout plus retry delay, 4.4 s after the abort.

## Recommended fix: fix-turn-isolation (ADR-11)
Correct the task description: "first-byte stall + silent SDK retry; late direct_answer persisted to history", replacing the teardown hypothesis at handson-2026-10-07.md:42.

- **(a) Turn-id guard.** Drop events and skip the history write when the captured `sentence_id` no longer matches `self.sentence_id`. `chat()` is vendored code, so this needs a new wrapper in `weixue_patches.py`, or a vendored edit once the fork exists.
- **(b) Wall-clock deadline.** Enforce the 90 s deadline around the generator. SDK timeout and retry settings cannot bound the turn on their own: per-read timeouts reset while bytes trickle in, and the fallback model at `anthropic.py:279-281` gets its own retries. Set SDK `max_retries=0`, retry in the overlay within the budget, and send the device a final state when the deadline passes.
- **(c) Retry logging.** Set the `anthropic` and `httpx2` loggers to INFO and bridge them to loguru.
- **Config change (needs Jay's approval).** Switch to a dict timeout (`anthropic.py:79-86`), for example connect 5 s and read about 20 s. Ship it with this task.

fix-wave3-server and fix-server-review-2 need no change.

## Regression test
- **Unit: stall then success.** The first `messages.stream()` blocks past the time-to-first-event bound and the second succeeds. Assert the overlay retry is logged, the deadline holds, and a final state is sent.
- **Unit: second turn during a stall.** Start a second `chat()` while the first is blocked. Assert the first turn's `direct_answer` is neither queued nor added to `dialogue`, and the second request carries one pending user message.
- **End to end (home LAN).** Delay api.anthropic.com for about 70 s and say "Tell me a story". Expect a timeout cue within budget and no stale story in history. Save serial and server logs under `docs/logs/`.

Produced by the `story-stall-rca` workflow (4 investigators, synthesis, 2 skeptics, neither refuted), 2026-10-07.
