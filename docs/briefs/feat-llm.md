# feat-llm developer brief (rev 2) — Anthropic Claude provider for xiaozhi-esp32-server

Vendored upstream re-read at `/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32-server` (HEAD `87c6df7`). Upstream paths below are relative to `main/xiaozhi-server/` unless they start with `Dockerfile`/`main/`. Nothing was modified. Docs cited were fetched 2026-10-06; the bundled `claude-api` skill (Claude Code 2.1.289) is cited as "skill: <file>". Section 8 lists every change against rev 1.

## 0. What to build

A provider module `core/providers/llm/anthropic/anthropic.py` exposing `class LLMProvider(LLMProviderBase)` that (a) converts the server's OpenAI-shaped dialogue + function list into Messages API `system`/`messages`/`tools`, (b) streams with `client.messages.stream(...)`, (c) yields exactly the token / tool-call shapes `ConnectionHandler.chat()` consumes, (d) is model-aware so `claude-sonnet-5-5` (default) and `claude-haiku-4-5-20251001` (fallback) both work without 400s, and (e) never emits `thinking` blocks (decision in §2.4). Plus `requirements.txt` + image rebuild, a `config.yaml` LLM block, a weixue-owned prompt template, and pytest coverage with an injected fake client.

## 1. Upstream contract (from source)

### 1.1 Provider base and loading
- `core/providers/llm/base.py` L7-33: `response(self, session_id, dialogue)` is abstract, a sync generator of `str`; `response_no_stream(system_prompt, user_prompt, **kwargs)` (L13-22) builds `[{"role":"system"},{"role":"user"}]` and calls `self.response("", dialogue, **kwargs)` — so `response` must accept `**kwargs`. `response_with_functions(self, session_id, dialogue, functions=None)` (L24-33) default yields `(token, None)`.
- `core/utils/llm.py` L17: `os.path.exists(os.path.join('core','providers','llm',class_name,f'{class_name}.py'))` is **relative to the process CWD**. The provider loads only when the app (and pytest) run with cwd = `main/xiaozhi-server`. In Docker this holds: `Dockerfile-server-base` L25 `WORKDIR /opt/xiaozhi-esp32-server`, `Dockerfile-server` L4 `COPY main/xiaozhi-server .`. Directory and file must both be named `anthropic`; precedent for a module named like the SDK it imports: `core/providers/llm/openai/openai.py`.
- `core/utils/util.py` L133-136 `check_model_key(modelType, modelKey)` does `"你" in modelKey` → **TypeError if `api_key` is None**. Call it only when `api_key` is a str.
- `core/utils/modules_initialize.py` L36-48: `selected_module.LLM` names a key under `LLM:`; `type` defaults to the key name.
- **Config source** (`config/config_loader.py` L35-46): `config.yaml` is merged with `data/.config.yaml` only when `custom_config["manager-api"]["url"]` is absent (L42-43); if set, the whole config comes from manager-api and the YAML `LLM:` block is ignored — then §7's manager-api SQL registration is **required**, not deferred. `config/settings.py` L17-21 raises `FileNotFoundError` if `data/.config.yaml` does not exist.
- `response_no_stream` is also called by `core/providers/memory/mem_local_short/mem_local_short.py` L178-183 with `max_tokens=2000, temperature=0.2` kwargs when memory is enabled: `response()` must accept arbitrary `**kwargs`, honour `max_tokens`, **drop `temperature`** (400 on Sonnet 5.5), and not log at INFO per call.

### 1.2 How `chat()` consumes the provider (`core/connection.py`)
- L1079-1093: `MAX_DEPTH = 5`; at max depth a `[系统提示]` user message is appended and `functions` stays `None`.
- L1098-1107: in `function_call` mode `functions = list(self.func_handler.get_functions())`, and **only at depth 0** `DIRECT_ANSWER_TOOL` is appended (L1104-1107). So the `tools` array differs between depth 0 and depth > 0.
- **Exception scope, corrected**: the `try` at L1111-1147 wraps only the *call* `self.llm.response_with_functions(...)` / `self.llm.response(...)` (L1129-1144). Both are generator functions, so no provider code runs there; L1145-1147 logs `LLM 处理出错` and `return None` silently (no TTS). The only things it can catch are memory-query errors (L1116-1120). **Every provider exception surfaces during iteration** (L1156 onward) and is caught at L1221-1239: logs `LLM stream processing error`, speaks `get_system_error_response(config)`, ends the turn. Anything you want logged with request params or `_request_id` must be logged inside the generator before re-raising; fallback-to-Haiku must happen inside the generator before the first yield.
- L1156-1173: for each yielded item in function mode `content, tools_call = response`; text is appended to `content_arguments`; non-empty `tools_call` sets `tool_call_flag = True` and calls `self._merge_tool_calls(tool_calls_list, tools_call)`. Non-function mode: `content = response` (str).
- L1175-1197: `direct_answer` arguments are read **incrementally** via `_extract_direct_answer_response` (L1772-1803: tries `json.loads`, falls back to string extraction of `"response": "`, tolerant of partial/truncated JSON) and streamed to TTS with a 5-char safety buffer.
- L1210-1220: text chunks are pushed to TTS **only while `tool_call_flag` is False**. A text block emitted before the first `tool_use` block is therefore spoken; after a tool block, nothing is.
- **`_merge_tool_calls` L1826-1852 expects attribute objects**: `getattr(tool_call,"index",None)`, `tool_call.id`, `tool_call.function.name`, `tool_call.function.arguments`. `index` is the list position. L1843-1844 appends **exactly one** placeholder when `tool_index >= len(tool_calls_list)`; L1847-1852 then indexes `tool_calls_list[tool_index]`. Precedent for the yield shape: `core/providers/llm/gemini/gemini.py` L183-194 `SimpleNamespace(id=..., type="function", function=SimpleNamespace(name=..., arguments=json_str))`.
- L1245-1312: after the stream, `direct_answer` calls are split out (L1272-1273), spoken/stored (L1275-1299), and if no real tool remains `chat()` returns (L1301-1310). `tool_calls_list = real_tool_calls` (L1312).
- L1327-1345: for each **real** tool, `tool_input = json.loads(tool_call_data.get("arguments") or "{}")` at **L1335 is outside any try** — the `try/except` at L1352-1368 wraps only `future.result(timeout=...)`, and the streaming `try` ended at L1239. A truncated/invalid `arguments` string for a real tool raises an **uncaught `JSONDecodeError` out of `chat()`**. The provider must guarantee parseable concatenated `arguments` for every real tool it yields (§3.3).
- `_handle_function_result` L1397-1503: `RESPONSE/NOTFOUND/ERROR` are spoken unless `text in streamed_text` (L1407-1413) and stored as assistant text; `REQLLM` → `Message(role="assistant", tool_calls=[{id, function:{arguments,name}, type, index}])` (L1470-1486), then one `Message(role="tool", tool_call_id, content)` per tool **only if `result.result` is non-empty** (L1488-1501; L1491-1495 synthesize `str(uuid.uuid4())` when `id` is None), then `self.chat(None, depth+1)` (L1503). Text streamed before the tool calls is stored as its own assistant message (L1319-1324) and is not deduped on the REQLLM path.
- Few-shot injection L690-729 adds `is_temporary` messages with ids `fewshot_da_001` (L694) and `fewshot_exit_001` (L711), each followed by its `role="tool"` result — the examples are complete.
- Client abort: L1157-1158 `if self.client_abort: break` → `GeneratorExit` inside the provider generator.

### 1.3 Dialogue wire format (`core/utils/dialogue.py`)
- `Message` L7-22 has `role, content, uniq_id, tool_calls, tool_call_id, is_temporary` — **no field for thinking blocks or signatures**. `getMessages` L34-48 emits `{"role":"assistant","tool_calls":[...]}` (no `content`), `{"role":"tool","tool_call_id","content"}` (L41-43 synthesize `str(uuid.uuid4())` when `tool_call_id` is None → an id that matches no `tool_use`), or `{"role","content"}`.
- L102-148: single `system` message first; `{{current_time}}` replaced with `HH:MM` on every call (L110-112); memory regex-filled into `<memory>` (L116-121); `<speakers_info>` appended.
- `_ensure_tool_calls_complete` L64-92 appends dummy `role="tool"` results `{"status": "interrupted", "message": "动作已取消/被打断"}` for dangling `tool_calls` **at the end of the list it is given**. It is applied **separately** to the few-shot segment (L150-155) and to the real history (L157-161). Dummies can therefore land (a) after the last few-shot message, before the first real user message, and (b) after the newest real user message. Both positions violate the Anthropic "immediately after" rule (§2.2).

### 1.4 Tools exposed to the LLM
- `core/providers/tools/unified_tool_handler.py` L120-122 → `ToolManager.get_function_descriptions()`; `unified_tool_manager.py` L49-60 returns OpenAI-format dicts; `execute_tool` looks up by exact name (L79-84).
- MCP endpoint: `mcp_endpoint_client.py` L36-48 keeps only `type/properties/required` from `inputSchema`; `mcp_endpoint_executor.py` L15-65 returns `ActionResponse(action=Action.REQLLM, result=str(result))` (L60) unless the result JSON carries `action`.
- User text may arrive as JSON `{"speaker": ..., "content": ...}` on a speaker's first appearance (`core/handle/receiveAudioHandle.py` L50-63) — the prompt's language rule must key on `content`, not the wrapper.

### 1.5 Config, Docker, tests
- `config.yaml`: `selected_module` L233-252; `LLM:` L613-744; `prompt_template` L220; `tool_call_timeout: 30` L65. `requirements.txt` L14 `openai==2.8.1`, L17 `httpx==0.28.1`, no `anthropic`.
- `Dockerfile-server-base` L3 `FROM python:3.10-slim`; L14-15 pins pip to `mirrors.aliyun.com`; L27-32 installs `requirements.txt`. `Dockerfile-server` L2 `FROM ghcr.io/xinnan-tech/xiaozhi-esp32-server:server-base`. **`docker-compose.yml` lives at `main/xiaozhi-server/docker-compose.yml`** (not the repo root): L6 `image: ghcr.nju.edu.cn/xinnan-tech/xiaozhi-esp32-server:server_latest`, L20 mounts `./data:/opt/xiaozhi-esp32-server/data`. Adding a dependency means rebuilding `server-base` from the fork, retagging or changing the `FROM` in `Dockerfile-server`, and pointing compose L6 at the fork's image — otherwise `import anthropic` fails at provider load.
- Mirror check (curl, 2026-10-06): `mirrors.aliyun.com/pypi/simple/anthropic/` lists `anthropic-1.11.0-py3-none-any.whl` and `.tar.gz`, so no `--index-url` override is needed today.
- Local dev: `/opt/homebrew/bin/python3.10` … `python3.14` exist and `uv` is installed; `python3` defaults to 3.14.3. Use `uv venv --python 3.10` to match the image.
- `tests/test_llm.py` L38-66: skips when `has_real_key(cfg, "api_key")` is False; `tests/conftest.py` L184-200 `has_real_key` returns False when the key is **absent** (`val is None → continue`). So for the live smoke, `api_key` must be present in `data/.config.yaml`, not left to `ANTHROPIC_API_KEY`.
- `conftest.py` L211-225 lists `"anthropic"` in `_MOCKED_MODULES`; L302-305 shims it only when `_try_import_real` (L237-246) fails. L275-284 `_ShimModule.__getattr__` returns a `_RecordingShim` **instance**, so with the shim active `except anthropic.RateLimitError:` raises `TypeError` when an exception reaches that clause. **Tests require the real `anthropic` package in the venv.** L329-354 patch `httpx.Client/AsyncClient`; the SDK uses `httpx2` (§2.1) so this never intercepts Anthropic calls; `httpx2.alias_httpx()` would have to run before conftest's own `import httpx` at L331, so it is not usable from conftest — inject a fake client instead.

## 2. Anthropic API facts (fetched)

### 2.1 SDK (https://platform.claude.com/docs/en/api/sdks/python.md ; https://pypi.org/pypi/anthropic/json)
- `anthropic==1.11.0`, Python ≥ 3.10; deps `anyio<5,>=4.1`, `docstring-parser`, `httpx2<3,>=2.0`, `jiter`, `pydantic<3`, `sniffio`, `typing-extensions>=4.14`. `httpx2` is separate from `httpx`, so no conflict with `httpx==0.28.1`.
- Client: `Anthropic(api_key=..., base_url=..., timeout=float | httpx2.Timeout, max_retries=2)`. Default timeout 10 min → `APITimeoutError`. Retries: 2 by default on connection errors, 408, 409, 429, ≥500; **"requests that time out are retried twice by default"** — so `timeout=60` with default retries can block a voice turn ≈180 s. `http_client` must be an `httpx2` client ("Passing a client from the separate `httpx` package raises a `TypeError`"). "Tracing and mocking tools that patch `httpx` itself … do not see the SDK's requests by default."
- Errors: `BadRequestError` 400, `AuthenticationError` 401, `PermissionDeniedError` 403, `NotFoundError` 404, `ConflictError` 409, `UnprocessableEntityError` 422, `RateLimitError` 429, `InternalServerError` ≥500, `APIConnectionError`, base `APIStatusError` (`.status_code`). Catch most-specific first (skill: `shared/error-codes.md` chain).
- `anthropic.Timeout` is the `httpx2`-based re-export; use it instead of importing `httpx2`, or declare `httpx2>=2.0` if you import it (skill: `python/claude-api/sdk-upgrade.md` Step 2 "httpx2 / httpx" bullet and Step 3).
- Streaming helper: `with client.messages.stream(**params) as stream: for event in stream` … `stream.get_final_message()`; `_request_id` on responses. Mid-stream SSE `error` events (e.g. `overloaded_error`, streaming.md § Error events) are raised from the iterator by `anthropic/_streaming.py` via `self._client._make_status_error(err_msg, body=body, response=self.response)` (fetched from the SDK repo); the concrete subclass for an HTTP-200 stream is UNVERIFIED — catch `anthropic.APIStatusError` then `anthropic.APIError`.

### 2.2 Messages API shapes
- https://platform.claude.com/docs/en/api/messages.md: `system` is string or text-block array; text block `minLength: 1`; tool def `name` `^[a-zA-Z0-9_-]{1,128}$`, `description`, `input_schema`, optional `strict`, **`eager_input_streaming`**, `cache_control`; `tool_result` fields `type, tool_use_id (^[a-zA-Z0-9_-]+$), content (optional, string or blocks), is_error, cache_control`; `tool_use.id` pattern `^[a-zA-Z0-9_-]+$` (so `fewshot_da_001`, `uuid4().hex` and hyphenated `str(uuid4())` are all valid — P13 resolved); `tool_choice` `auto|any|tool|none`, all with optional `disable_parallel_tool_use`; `thinking` types `enabled|disabled|between_tools|adaptive` (`between_tools` has no other field); `output_config.effort` `low|medium|high|xhigh|max`.
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls.md (Note "Important formatting requirements"): "Tool result blocks must immediately follow their corresponding tool use blocks in the message history. You cannot include any messages between the assistant's tool use message and the user's tool result message." "In the user message containing tool results, the tool_result blocks must come FIRST in the content array. Any text must come AFTER all tool results." Error text: "tool_use ids were found without tool_result blocks immediately after". `is_error: true` for tool failures; an empty `tool_result` (no `content`) is a documented valid shape ("Example of empty tool result").
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use.md § Execution semantics: "return one `tool_result` for each `tool_use` block, all together in the next user message … put every `tool_result` block before any text content in that message"; § Troubleshooting: "Correct: all tool results together in a single user message" / "Wrong: separate user messages reduce parallel tool use"; "If you choose not to run a particular call … still return a `tool_result` for it with `is_error: true`". § Disable parallel tool use: `tool_choice: {"type":"auto","disable_parallel_tool_use":true}` → at most one call per response.
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools.md: `any`/`tool` return 400 on Claude Sonnet 5.5 (and Opus 5.5, Fable 5.1); "`none` is also supported"; "changes to the `tool_choice` parameter will invalidate cached message blocks. Tool definitions and system prompts remain cached"; § Model responses with tools: "Claude often comments on what it's doing or responds naturally to the user before calling tools"; `strict: true` requires `additionalProperties: false` + `required`.
- https://platform.claude.com/docs/en/build-with-claude/streaming.md: event order `message_start` → per block `content_block_start`/`content_block_delta`/`content_block_stop` (each with `index` = position in final `content`) → `message_delta` (`stop_reason`, **cumulative** `usage`) → `message_stop`; `ping` and `error` events. § Input JSON delta: "Current models only support emitting one complete key and value property from `input` at a time" — the first `partial_json` may be `""`. § Thinking delta: "When `display: "omitted"` … The thinking block opens, receives a `thinking_delta` with an empty `thinking` string and then a single `signature_delta`, and closes." § Streaming request with tool use: "Enable it per tool with `eager_input_streaming`."
- https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons.md: on `max_tokens` with a trailing `tool_use` block the input is incomplete — "retry the request with a higher `max_tokens`"; `model_context_window_exceeded` "Returned when the response fills the model's context window. Treat the response as truncated" (no beta header on Sonnet 4.5+); `refusal` is HTTP 200 with `stop_details` (`null` for other stop reasons). The documented 400 for tool blocks in history without a matching tool definition is for **server** tools ("but no `web_search` tool was provided"); client-tool behaviour is UNVERIFIED.

### 2.3 Models and Sonnet 5.5 rules
- https://platform.claude.com/docs/en/about-claude/models/overview.md: Claude Sonnet 5.5 `claude-sonnet-5-5`, 1M context, 128K output, thinking "Adaptive", default effort `high`. Claude Haiku 4.5 `claude-haiku-4-5-20251001` (alias `claude-haiku-4-5`), 200K context, 64K output, thinking "Extended", default effort **"Not supported"**.
- https://platform.claude.com/docs/en/build-with-claude/thinking.md per-model table: Claude Sonnet 5.5 — no field / `adaptive` → adaptive thinking; `enabled`+`budget_tokens` → 400; `between_tools` → "Up-front thinking off at `high` effort or below"; `disabled` → 400. Claude Haiku 4.5 — no field → thinking off; `adaptive` → 400; `between_tools` → 400; `disabled` → off. Claude Opus 5.5 / Opus 5 / Sonnet 5: `between_tools` → 400. https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting.md: "Only Claude Sonnet 5.5 accepts `"between_tools"`"; error text `"thinking.type.between_tools" is not supported for this model.`; § Cache hits drop: keep thinking config and effort constant across requests sharing a conversation.
- https://platform.claude.com/docs/en/models/sonnet-5-5/migration-guide.md: `between_tools` accepted at `low`/`medium`/`high`, 400 at `xhigh`/`max`; "takes no other field: `display`, `budget_tokens`, or `block_binding` sent with it returns a 400"; "effort can't change mid-conversation" with it (per-message effort 400); Claude Sonnet 4.5 and Haiku 4.5 accept only `disabled`/`enabled`; non-default `temperature`/`top_p`/`top_k` → 400 (L1194); notes between tool calls "longer than a sentence or two" return as progress-update `thinking` blocks, "Shorter remarks stay `text`" (L870); server-side fallback `fallbacks: "default"` is beta, Claude API only, and a `between_tools` request falling back to Sonnet 5 runs with `disabled` (L248, L884); Claude 4.5+ also stop with `model_context_window_exceeded` (L1239); refusal `stop_details.category` ∈ {cyber, bio, frontier_llm, reasoning_extraction, general_harms}.
- https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5-5.md: at `low` effort "it skips thinking on most simple requests"; thinking counts toward `max_tokens`; with `between_tools` "remove any instruction that tells the model not to think"; read responses by block type; tolerant tool-call handling (wrong letter case → accept unambiguous match or `is_error` naming the exact tool); "Never put user text inside a `tool_result` block".

### 2.4 Thinking blocks — the decision (new)
Facts:
- thinking.md § Preserving thinking blocks: "**Required:** within a tool-use turn, pass thinking blocks back … Within the latest assistant message, the sequence of consecutive `thinking` blocks must match what the model generated … you can't rearrange, edit, or partially drop them." "Modified thinking blocks are rejected with a 400 error."
- https://platform.claude.com/docs/en/build-with-claude/preserved-thinking.md: valid changes include "Remove `thinking` blocks from the start of the history, from the end, or all of them"; "If it removes all of them, nothing fails and the model loses its earlier reasoning"; what fails is a **gap**. The prefix check (system, tools, every earlier message unchanged) is enforced by default for accounts created on/after 2026-08-31 and applies to **replayed** blocks; error text `Invalid signature in thinking block. The block is bound to a different conversation`. `block_binding.prefix_mismatch_behavior: drop_block` needs beta `thinking-binding-controls-2026-08-01` and, on Sonnet 5.5, adaptive thinking only (troubleshooting doc).
- thinking.md § Thinking with tool use: "Mid-turn conflicts degrade gracefully … the API may strip thinking blocks that would create an invalid turn structure, or disable thinking when the conversation history is incompatible".
- thinking.md § Progress updates: each progress update "sits immediately before the `tool_use` … block it introduces" and has its own `signature`; under `between_tools` it carries summary text.
- Upstream constraint: `Message` has no thinking field (dialogue.py L7-22); upstream re-renders `system` every request (`{{current_time}}`, L110-112) and changes `tools` by depth (connection.py L1104-1107) — both are prefix edits.

Decision (v1): **the converter never emits `thinking` blocks and never stores signatures.** Rationale: dropping all is a documented valid edit; dropping some is the failure mode; and because the prefix check applies only to replayed blocks, upstream's per-request `system`/`tools` changes become harmless. Cost: with `between_tools` there is no up-front reasoning to lose, only the progress note between tool calls; longer notes arrive as `thinking` and are lost, short ones arrive as `text`. In `_stream`, ignore `content_block_start` of type `thinking`/`redacted_thinking`, `thinking_delta` and `signature_delta`. Acceptance #13 verifies the second request of a tool round-trip succeeds with no thinking blocks.

Option B (deferred, §7): cache `final.content` per request keyed by its `tool_use` ids and replay it verbatim in `_convert_dialogue`; this also requires freezing the rendered `system` string and the `tools` list within a top-level turn (always include `direct_answer`, snapshot `system` at depth 0). Only worth it if measured quality demands it.

## 3. Changes, file by file

### 3.1 `requirements.txt`
Add `anthropic==1.11.0`. Rebuild `server-base` from `Dockerfile-server-base` (the layer that installs requirements); either tag the local build `ghcr.io/xinnan-tech/xiaozhi-esp32-server:server-base` or change `Dockerfile-server` L2; point `main/xiaozhi-server/docker-compose.yml` L6 at the fork's `server_latest`. Coordinate with feat-server. Do not import `httpx2` directly; if you must, add `httpx2>=2.0`.

### 3.2 `config.yaml` (and weixue `data/.config.yaml` override)
```yaml
LLM:
  AnthropicLLM:
    type: anthropic
    api_key: 你的anthropic api key      # placeholder in config.yaml; real key in data/.config.yaml (required for live tests)
    model_name: claude-sonnet-5-5
    fallback_model_name: claude-haiku-4-5-20251001   # optional
    max_tokens: 1024                 # raise to 2048+ if thinking: adaptive
    effort: low                      # low|medium|high (clamped to high when thinking=between_tools)
    thinking: between_tools          # between_tools|adaptive ; ignored for non-Sonnet-5.5 models
    timeout: 60                      # seconds
    max_retries: 1                   # SDK retries timeouts too; 0 or 1 for voice
    # base_url: optional
selected_module:
  LLM: AnthropicLLM
  Intent: function_call
```
Do **not** expose `temperature/top_p/top_k/frequency_penalty` for this type. Keep `thinking` + `effort` constant for the lifetime of a conversation (cache + `between_tools` rule). If `data/.config.yaml` sets `manager-api.url`, this block is ignored (§1.1) — register the provider in manager-api instead.

### 3.3 New `core/providers/llm/anthropic/anthropic.py`
```python
class LLMProvider(LLMProviderBase):
    def __init__(self, config: dict): ...
    def response(self, session_id, dialogue, **kwargs) -> Iterator[str]
    def response_with_functions(self, session_id, dialogue, functions=None, **kwargs) -> Iterator[tuple[str | None, list | None]]
    @staticmethod
    def _split_system(dialogue) -> tuple[str | None, list[dict]]
    @staticmethod
    def _convert_dialogue(dialogue) -> list[dict]
    @staticmethod
    def _convert_tools(functions) -> list[dict] | None
    def _request_params(self, model, system, messages, tools, max_tokens=None) -> dict
    def _stream(self, params, tools) -> Iterator[tuple[str | None, list | None]]
```
No `__init__.py` (upstream provider dirs have none).

**`__init__`**: `self.model_name = config.get("model_name") or "claude-sonnet-5-5"`; `self.fallback_model_name = config.get("fallback_model_name")`; `self.api_key = config.get("api_key") or None`; if `isinstance(self.api_key, str)`: `check_model_key("LLM", self.api_key)` and log like openai.py L69-71; `self.max_tokens = int(config.get("max_tokens") or 1024)`; `self.effort` ∈ {low, medium, high, xhigh, max} (default `low`); `self.thinking` ∈ {between_tools, adaptive} (default `between_tools`); if `thinking == between_tools` and `effort in (xhigh, max)` → clamp to `high` with a warning. Timeout: number → seconds; dict → `anthropic.Timeout(connect=..., read=..., write=..., pool=...)` mirroring openai.py L31-45; default 60. `max_retries = int(config.get("max_retries", 1))`. `self.client = anthropic.Anthropic(api_key=self.api_key, base_url=config.get("base_url") or None, timeout=..., max_retries=...)`. `self._last_tools = None` (for the MAX_DEPTH path).

**`_split_system`**: pop every `role == "system"` entry; join `content` with `\n\n` → `system` string (skip if empty).

**`_convert_dialogue`** (core of the task):
1. `user` str content → `{"role":"user","content":[{"type":"text","text":content}]}`; skip empty/whitespace (text `minLength: 1`).
2. `assistant` with `tool_calls` → assistant message whose content is `[{"type":"tool_use","id":tc["id"],"name":tc["function"]["name"],"input":json.loads(tc["function"]["arguments"] or "{}")}]`; `JSONDecodeError` → `{}`. If the previous emitted message is an assistant text message, append the blocks to it (one assistant turn; this covers upstream's text-then-tool_calls split at L1324 + L1486). Never emit `thinking` blocks (§2.4).
3. `assistant` str content → text block; skip empty.
4. `tool` → `{"type":"tool_result","tool_use_id":m["tool_call_id"],"content":str(content)}`; omit `content` when empty (documented valid shape). Consecutive `tool` messages merge into **one** user message directly after their assistant `tool_use` message.
5. **Repair pass (mandatory)** — index every `tool_use` id emitted so far, then:
   - **Relocate**: any `tool_result` whose `tool_use_id` is known but which is not in the user message immediately following that `tool_use` is moved there (this is where upstream's end-of-segment dummies land — from the few-shot segment *and* from the real history, dialogue.py L150-161). Keep its content; do **not** add `is_error` (the content already says `interrupted`).
   - **Synthesize**: for a `tool_use` id with no `tool_result` anywhere, insert `{"type":"tool_result","tool_use_id":id,"is_error":true,"content":"no result returned"}` immediately after — `is_error: true` only for genuinely missing results.
   - **Drop orphans**: a `tool_result` whose `tool_use_id` matches no preceding `tool_use` (dialogue.py L41-43 / connection.py L1491-1495 synthesize fresh uuids) is dropped with a warning (or flattened to a user text block if its content is useful).
   - **Dedupe**: a second `tool_result` for the same id is dropped.
   - Within a user message, `tool_result` blocks come before text blocks.
6. First message must be `user` (log a warning and insert a minimal user text otherwise); last message must be `user` (Sonnet 5.5 rejects assistant prefill; `chat()` always ends with a user message or tool results — assert/log).

**`_convert_tools`**: for each `f`: `fn = f["function"]`; `{"name": fn["name"], "description": fn.get("description") or "", "input_schema": fn.get("parameters") or {"type":"object","properties":{}}}`; force `input_schema["type"] = "object"`. Validate `name` against `^[a-zA-Z0-9_-]{1,128}$`; skip with a warning on failure (do not rename: `ToolManager.execute_tool` is exact-match, unified_tool_manager.py L79-84). For `direct_answer` only, add `"eager_input_streaming": True` (§4 P14). Return `None` for an empty list. Remember the result in `self._last_tools`.

**`_request_params(model, system, messages, tools, max_tokens=None)`**: `{"model", "max_tokens", "messages"}` + `system` if any + `tools` + `tool_choice: {"type":"auto"}` when tools present. Model policy:
- `model.startswith("claude-sonnet-5-5")`: `output_config: {"effort": self.effort}`; `thinking: {"type":"between_tools"}` only when `self.thinking == "between_tools"` (for `adaptive`, omit the field — equivalent to the default); never `display`/`budget_tokens`/`block_binding`.
- `model.startswith("claude-haiku-4-5")`: send **neither** `thinking` nor `output_config` (effort "Not supported"; omitted thinking = off).
- any other id (`claude-opus-5-5`, `claude-sonnet-5`, …): omit `thinking` (adaptive default), keep `output_config.effort`. Never send `between_tools` to them (400).
- Never send `temperature/top_p/top_k`, even when `response_no_stream` kwargs carry them.
- MAX_DEPTH path: if `tools is None` and the converted history contains any `tool_use`/`tool_result` block and `self._last_tools` is set, send `tools=self._last_tools` with `tool_choice: {"type":"none"}` (behaviour without `tools` is UNVERIFIED for client tools; `none` is supported on Sonnet 5.5). Note `tool_choice` changes invalidate the cached *message* prefix only.
- Optional `cache_control: {"type":"ephemeral"}` on the last tool definition; measure `usage.cache_read_input_tokens` first (P12).

**`_stream(params, tools)`** (sync generator used by both public methods):
```python
with self.client.messages.stream(**params) as stream:
    ordinal_by_block = {}      # API block index -> tool ordinal (0-based, counting tool_use blocks only)
    pending = {}               # block index -> {"id","name","args": str} for real tools (buffered)
    for event in stream:
        t = event.type
        if t == "content_block_start":
            cb = event.content_block
            if cb.type == "tool_use":
                ordinal = len(ordinal_by_block); ordinal_by_block[event.index] = ordinal
                if cb.name == "direct_answer":
                    yield None, [SimpleNamespace(index=ordinal, id=cb.id, type="function",
                                  function=SimpleNamespace(name=cb.name, arguments=""))]
                else:
                    pending[event.index] = {"id": cb.id, "name": cb.name, "args": ""}
            # thinking / redacted_thinking: ignore
        elif t == "content_block_delta":
            d = event.delta
            if d.type == "text_delta" and d.text:
                yield d.text, None
            elif d.type == "input_json_delta" and d.partial_json:
                if event.index in pending:
                    pending[event.index]["args"] += d.partial_json
                else:   # direct_answer: stream live for incremental TTS
                    yield None, [SimpleNamespace(index=ordinal_by_block[event.index], id=None, type="function",
                                  function=SimpleNamespace(name=None, arguments=d.partial_json))]
            # thinking_delta / signature_delta: ignore
        elif t == "content_block_stop" and event.index in pending:
            p = pending.pop(event.index); args = p["args"] or "{}"
            try: json.loads(args)
            except ValueError:
                log warning (name, len(args), self._request_id if known); dropped[p["name"]] = True; continue
            yield None, [SimpleNamespace(index=ordinal_by_block[event.index], id=p["id"], type="function",
                          function=SimpleNamespace(name=p["name"], arguments=args))]
        # ping, message_start, message_delta, message_stop: ignore here
    final = stream.get_final_message()
    log final.usage.input_tokens/output_tokens/cache_read_input_tokens, final.stop_reason, final._request_id
    if final.stop_reason in ("max_tokens", "model_context_window_exceeded"): log warning; if a real tool was dropped and nothing was yielded as a tool, yield a short spoken warning text
    if final.stop_reason == "refusal": log final.stop_details (category); yield a short spoken fallback text
```
Why buffer real tools: `_merge_tool_calls` string-concatenates `arguments` and `json.loads` at connection.py L1335 is uncaught, so a real tool's arguments must be parseable by stream end; buffering until `content_block_stop` lets the provider validate and drop a truncated call instead of crashing `chat()`. `direct_answer` is exempt because `chat()` filters it out before L1335 (L1272-1273) and reads it with a tolerant extractor (L1772-1803) — and it needs live deltas for incremental TTS (L1175-1197). The ordinal (not the API block index) is the `index` because a `thinking` or `text` block routinely occupies block 0 (P1).

Spoken fallback texts are yielded as plain text; they are only spoken if no tool item was yielded earlier in the stream (connection.py L1210-1220), which is the case for a lone truncated/refused call.

`response()` = `for text, _ in self._stream(params_without_tools, None): if text: yield text` (ignore tool items). `response_with_functions()` = `yield from self._stream(params_with_tools, tools)`. The `with … as stream` context closes the HTTP stream on `GeneratorExit` (client abort, L1157-1158), matching openai.py's `finally: stream.close()`.

**Fallback model and error handling** (inside the generator, before the first yield): wrap the whole `_stream` body; on `anthropic.RateLimitError`, `anthropic.InternalServerError`, or `anthropic.APIStatusError` with `status_code == 529`, or an `APIStatusError`/`APIError` raised by the iterator for a mid-stream `error` event, **if nothing has been yielded yet** and `fallback_model_name` is set → rebuild params for the fallback model (model policy changes: Haiku gets no `thinking`/`output_config`) and retry once. Catch `anthropic.BadRequestError` separately: log `e.message`, `e.status_code`, the request param **keys**, message count and the last two roles (never the key or content), then re-raise — 400s here are history-shape bugs (P3) or a Sonnet-5.5-only param on another model. Catch order: `BadRequestError` → `RateLimitError` → `InternalServerError` → `APIStatusError` → `APIConnectionError`/`APITimeoutError` → re-raise everything else. `chat()` logs and speaks the system error only for exceptions raised during iteration (L1221-1239).

### 3.4 Prompt template (weixue-owned) — §5.

### 3.5 Tests `tests/test_llm_anthropic.py` (new; default mock mode, no network)
Run from `main/xiaozhi-server` with a 3.10 venv that has `anthropic` installed (the conftest shim breaks `except anthropic.X`; `create_instance` is cwd-relative). Monkeypatch `anthropic.Anthropic` (or a module-level client factory) with a fake whose `messages.stream(**params)` records `params` and returns a context manager iterating scripted `SimpleNamespace` events and exposing `get_final_message()`. Cases:
1. Dialogue conversion: system → `system`; `tool_calls` → `tool_use` with parsed `input`; `role: tool` → `tool_result` grouped into one user message right after; empty content skipped; end-of-segment "interrupted" dummy relocated from **both** the few-shot segment and the real history; orphan `tool_result` (uuid id with no `tool_use`) dropped; duplicate dropped; synthesized result carries `is_error: true`, relocated dummy does not; few-shot ids unchanged; no `thinking` blocks ever emitted.
2. Tool conversion: `DIRECT_ANSWER_TOOL` and an MCP-shaped function → `name/description/input_schema`; `eager_input_streaming` only on `direct_answer`; invalid name skipped; empty list → no `tools` key.
3. Param policy: Sonnet config → `thinking={"type":"between_tools"}`, `output_config={"effort":"low"}`, `tool_choice={"type":"auto"}`, no `temperature`, no `display`/`budget_tokens`/`block_binding`; `effort: xhigh` + `between_tools` clamps to `high`; Haiku → no `thinking`/`output_config`; `claude-opus-5-5` → no `thinking`, has `output_config`; `response("", dialogue, temperature=0.2, max_tokens=2000)` forwards `max_tokens` and drops `temperature`; MAX_DEPTH (no functions, history has tool blocks) → `tools=_last_tools` + `tool_choice none`.
4. Streaming protocol: events `[thinking block idx0 (empty delta + signature), text block idx1 "Sure,", tool_use idx2 id=toolu_1 name=light_on, input_json_delta '{"entity', '_id":"light.living"}', content_block_stop idx2, message_delta stop_reason=tool_use]` → yields `("Sure,", None)` then one tool item with `index == 0` (not 2) and complete arguments; feeding the yields through a copy of `_merge_tool_calls` produces `[{"id":"toolu_1","name":"light_on","arguments":'{"entity_id":"light.living"}'}]` without `IndexError`, and `json.loads` succeeds. Same script with `direct_answer` → arguments arrive as multiple partial deltas.
5. Truncation: tool_use block whose deltas stop mid-JSON and `stop_reason == "max_tokens"` → no tool item yielded for it, no exception escapes, a warning text is yielded.
6. `response_no_stream("sys","hi")` returns the joined text; `response()` ignores tool events.
7. Fallback: first `stream()` raises `anthropic.RateLimitError` (construct with a fake `httpx2`-style response or subclass stub) → second call uses `fallback_model_name` without `thinking`/`output_config`; an error after the first yield does **not** retry.
Live smoke (DESIGN "一次真实冒烟"): `RUN_LIVE_API_TESTS=1 pytest tests/test_llm.py` with `selected_module.LLM: AnthropicLLM` and a real `api_key` present in `data/.config.yaml`, cwd `main/xiaozhi-server`. Also run one tool round-trip (acceptance #5/#13) and record first-token latency for `between_tools` vs `adaptive` at `low` (#21).

## 4. Pitfalls (ranked)
- **P1 Tool-call `index` must be the tool ordinal, not the API block index.** With a `thinking` or `text` block at index 0, passing block index 1 (or 2) into `_merge_tool_calls` appends exactly one placeholder (L1843-1844), then `tool_calls_list[tool_index]` at L1847-1852 raises `IndexError`; it is caught at L1221 ("LLM stream processing error"), the generic system error is spoken, `chat()` returns, and **the tool is never dispatched**. Unit test #4 covers it.
- **P2 Yield objects, not dicts** (`_merge_tool_calls` attribute access, L1836-1852; gemini.py L183-194 precedent).
- **P3 Misplaced / dangling / orphan tool results → 400.** Upstream appends "interrupted" dummies at the end of *each* segment (dialogue.py L150-161) and synthesizes uuids that match no `tool_use` (L41-43, connection.py L1491-1495). The repair pass (relocate, synthesize, drop orphans, dedupe, results-before-text) is mandatory.
- **P4 Model-specific 400s.** `between_tools` only on `claude-sonnet-5-5*` (400 on Opus 5.5, Opus 5, Sonnet 5, Haiku 4.5); with it no `display`/`budget_tokens`/`block_binding`, effort ≤ `high`, no mid-conversation effort change. Haiku 4.5: no `output_config.effort`, no `adaptive`. Sonnet 5.5: no non-default `temperature/top_p/top_k`, no `thinking: disabled`, no `budget_tokens`, no `tool_choice any/tool`, no assistant-last message.
- **P5 Truncated tool input is an uncaught crash.** `json.loads` at connection.py L1335 is outside every `try`. Buffer real tools until `content_block_stop`, validate, drop + warn on failure; handle `stop_reason` `max_tokens` and `model_context_window_exceeded`. Thinking counts toward `max_tokens`.
- **P6 Thinking blocks**: never emit them, never store signatures (§2.4); ignore `thinking`/`redacted_thinking` block events. Progress notes longer than a sentence or two are lost under `between_tools`.
- **P7 Exceptions are only handled during iteration** (connection.py L1111-1147 vs L1221-1239). Log request context inside the generator; fall back before the first yield.
- **P8 Timeout × retries**: SDK retries timeouts; `timeout 60` × (1 + `max_retries`) wall-clock. Use `max_retries` 0 or 1 for the streaming client; the provider's own fallback covers 429/5xx/529.
- **P9 MCP tool names** must match `^[a-zA-Z0-9_-]{1,128}$`; ha-mcp names UNVERIFIED. v1: skip + warn, surface in the feat-ha-mcp handoff. Letter-case mismatches (`Light_On`) can be matched case-insensitively when unambiguous against the tool list passed in.
- **P10 Empty text blocks** (`minLength: 1`): skip `""` content and never produce a text block for `tool_calls`-only assistant messages.
- **P11 Spoken text before tool calls**: text emitted before the first `tool_use` is spoken (L1210-1220) and the post-tool answer is spoken again at depth+1 (REQLLM path has no dedupe). Prompt rule: do not narrate before a tool call (§5); otherwise accept the double utterance.
- **P12 Prompt caching**: render order tools → system → messages; `system` changes every minute (`{{current_time}}`) and `tools`/`tool_choice` differ by depth. Only the tools prefix is cacheable; measure before tuning. Keep `thinking`/`effort` constant.
- **P13 (resolved)** tool ids `fewshot_da_001`, `uuid4().hex`, hyphenated `str(uuid4())` all match `^[a-zA-Z0-9_-]+$`; no re-id needed.
- **P14 `direct_answer` latency**: without `eager_input_streaming` the whole `response` string arrives in one burst ("one complete key and value property … at a time"), so incremental TTS (L1175-1197) gets nothing early. Set `eager_input_streaming: true` on `direct_answer` (schema field, no beta header). With it the API no longer validates/coerces that input; upstream's extractor already tolerates partial JSON. A custom `base_url` proxy may reject the field (skill: `shared/tool-use-concepts.md` § Eager input streaming) — drop it when `base_url` is set unless verified.
- **P15 Empty MCP results**: `_handle_function_result` stores a `role=tool` message only when `result.result` is non-empty (L1488-1501); an MCP tool returning `""` leaves a dangling call that upstream back-fills with the "interrupted" dummy next request, so the model will say the action was cancelled. feat-ha-mcp handoff: results must be non-empty.
- **P16 `check_model_key(None)` TypeError** when `api_key` is omitted (util.py L134).
- **P17 conftest does not sandbox you**: the `httpx` patch does not see `httpx2`; the shim breaks `except anthropic.X` if the package is missing. Install `anthropic`, inject a fake client.
- **P18 Config source**: with `manager-api.url` set, the YAML `LLM:` block is ignored (config_loader.py L42-43).
- **P19 Logging**: never log the config dict, the key, or message content; log `final._request_id`, `usage`, `stop_reason`, `stop_details.category`.
- **P20 Speaker JSON** user text (`receiveAudioHandle.py` L50-63): language rule follows `content`.

## 5. System prompt: English-primary / Chinese-secondary (ADR-7)
**Mechanism**: do not edit upstream `agent-base-prompt.txt`. Ship `data/weixue-agent-prompt.txt` (the `./data` volume is mounted, compose L20) and set `prompt_template: data/weixue-agent-prompt.txt` plus the household identity in `prompt:` in `data/.config.yaml`. `PromptManager._load_base_template` loads `config["prompt_template"]` with a cwd-relative `os.path.exists` (prompt_manager.py L79-81, L96) and renders Jinja2 variables `base_prompt, current_time ("{{current_time}}" passthrough), today_date, today_weekday, lunar_date, local_address, weather_info, emojiList, device_id, client_ip, dynamic_context, language (TTS.<selected>.language or "中文", L277-283), **kwargs` (L249-290). Keep the literal `{{current_time}}` and an empty `<memory></memory>` block.

**Changes vs upstream template**:
- Replace `<output_language_directive>` with: "Reply in the language the user spoke in this turn. English is the household default: use it when the turn is ambiguous (a name, a number, a single word) or mixes languages. If the user speaks Chinese, reply in Simplified Chinese. If the user turn is a JSON object with `speaker` and `content`, judge the language from `content`. Never switch language mid-reply; do not translate or echo the user's words. Tool results may be English or JSON; still answer in the user's language." Do not use `{{language}}`. Cross-task note for feat-tts-google: the LLM emits no language tag; TTS picks the voice from the reply script.
- Keep `<core_rules>`; make rule 4's farewell list bilingual ("bye", "good night", "再见", "晚安").
- Keep `<tts_format_constraints>` (no Markdown, no emoji unless `emoji_enabled`, no stage directions). Add: one or two short sentences for commands and status questions; offer to continue for long content.
- Add: "When you call a tool, do not say anything first; speak only after the result." (P11.) Do **not** add "do not think/reason" instructions (`between_tools` guidance).
- Rewrite `<tool_and_knowledge>`: device control and state → Home Assistant MCP tools; never claim an action happened without a tool result; if a result says approval is required/pending or denied (ha-mcp `USER_APPROVAL_REQUIRED`, ADR-9), say so in one sentence; small talk → `direct_answer` (injected at depth 0, demonstrated by few-shot L690-708). Drop music/news lines unless those plugins are enabled.
- Drop `<safety_compliance>`; keep `<speaker_recognition>` only if voiceprint is configured.

**Prompt acceptance** (DESIGN feat-llm row): "Turn on the living room light" → tool call → English confirmation; "客厅几度？" → query tool → Chinese answer with the value; "tell me a joke" → direct English reply, no real tool; "讲个笑话" → Chinese reply.

## 6. Acceptance checklist
1. `requirements.txt` has `anthropic==1.11.0`; `server-base` rebuilt from the fork, `Dockerfile-server` FROM / compose L6 point at it; container logs `初始化组件: llm成功 AnthropicLLM` (modules_initialize.py L48).
2. `config.yaml` gains `AnthropicLLM` (`type: anthropic`); `data/.config.yaml` selects it and `Intent: function_call`; no `temperature/top_p` keys; `manager-api.url` unset (or provider registered in manager-api).
3. `pytest tests/test_llm_anthropic.py` passes from `main/xiaozhi-server` in a Python 3.10 venv with `anthropic` installed, no network; `RUN_LIVE_API_TESTS=1 pytest tests/test_llm.py` passes once with a real `api_key` present in `data/.config.yaml`.
4. Streaming: first spoken text arrives from `text_delta` (or `direct_answer` deltas) before `message_stop`; `usage`, `stop_reason`, `_request_id` logged per request.
5. Tool path end-to-end: device utterance → `function_name=... function_id=toolu_... function_arguments={...}` (connection.py L1329-1331) → MCP executes → `REQLLM` → second request carries `tool_result` → spoken confirmation; no `tool_use ids were found without tool_result` 400 in logs.
6. Parallel tools: two tool calls yield ordinals 0 and 1, both executed (L1327-1345), results in one user message.
7. Interrupt test: abort mid-tool-call, speak again → next request succeeds (repair pass; no 400). Repeat with the abort during the first turn after startup (few-shot segment dummy).
8. `direct_answer` works: reply streamed incrementally from the `response` argument (multiple `input_json_delta` events observed); no "工具 direct_answer 不存在".
9. Max-depth: after 5 rounds the `[系统提示]` message is sent with `tool_choice none` (or without tools) and the model answers in text (L1082-1093); no 400.
10. Model swap: `model_name: claude-haiku-4-5-20251001` works (no `thinking`/`output_config`); a forced 429/5xx with fallback configured switches model once, before any output.
11. Language: 10 English + 10 Chinese utterances → reply language matches ≥ 9/10 each; no mid-reply switch; no Markdown/emoji when `emoji_enabled` is false.
12. No secret in logs or git: grep logs for the key prefix; `data/.config.yaml` untracked.
13. Thinking blocks: the second request of a tool round-trip contains no `thinking` blocks and returns 200 (drop-all tolerance confirmed on this account); log any `input_transformations` or 400 mentioning `signature`.
14. No pre-tool narration: in 10 device commands, the model speaks only after the tool result (≤ 1 double utterance); otherwise tighten the prompt rule.
15. Unit: text/thinking block before `tool_use` → ordinal 0, no `IndexError`.
16. Unit: truncated real-tool input → no exception escapes the generator, call dropped, warning yielded.
17. Unit: orphan `tool_result` dropped; dummies relocated from both segments; synthesized result has `is_error: true`, relocated dummy does not.
18. Unit: param assertions per model (P4), `temperature` kwarg dropped, `between_tools` + `xhigh` clamped.
19. Config: `max_retries` ≤ 1 on the streaming client; a 60 s read timeout fails the turn in < 130 s.
20. Measure first-token latency for `thinking: between_tools` vs `adaptive` at `effort: low` over 10 utterances each; pick the default from the data.

## 7. Deferred / out of scope (hand off)
- Replaying thinking blocks (Option B, §2.4): requires freezing `system` and `tools` within a turn and caching `final.content` per request.
- manager-api registration (`ai_model_provider`/`ai_model_config` rows, e.g. `main/manager-api/src/main/resources/db/changelog/202504082211.sql` L19, `202504112044.sql` L23) — required only if feat-server sets `manager-api.url` / uses `docker-compose_all.yml` (UNVERIFIED which).
- MCP tool-name sanitation + alias map (P9) once ha-mcp names are known; `strict: true` (needs `additionalProperties: false` added to MCP schemas); `disable_parallel_tool_use` if parallel HA actions prove risky.
- Server-side fallback (`fallbacks: "default"`, beta, Claude API only; interacts with `between_tools`); prompt-caching breakpoints; per-message effort (beta, adaptive only).

## 8. Revision log (rev 1 → rev 2)
- C21: single-user-message rule now cited to parallel-tool-use.md (and skill SKILL.md "Parallel tool use"); handle-tool-calls.md kept for immediacy/ordering/`is_error`/error text.
- P1: failure mode corrected to `IndexError` → generic system error, no dispatch.
- C6/P3: repair pass handles dummies from both segments, orphan results, and `is_error` only for synthesized results.
- §3.3 params: `between_tools` only for `claude-sonnet-5-5*`; other 5.x omit `thinking`; Haiku gets neither; clamp effort.
- §3.3 stream: real-tool arguments buffered and validated (L1335 is uncaught); `direct_answer` streams live with `eager_input_streaming`; `model_context_window_exceeded` and `stop_details` handled.
- §1.2: exception scope corrected (L1111-1147 wraps only the call).
- New §2.4 thinking-block decision; new P5–P8, P11, P14, P15, P18, P20; P13 resolved; acceptance #13–#20 added.
- Resolved UNVERIFIED: Aliyun mirror carries 1.11.0; python3.10 + uv available locally; `docker-compose.yml` path.

## UNVERIFIED
- Exact exception subclass the Python SDK raises for a mid-stream SSE `error` event (e.g. overloaded_error) on an HTTP-200 stream — _streaming.py calls client._make_status_error(...); treat as APIStatusError/APIError and test the fallback path live.
- Behaviour when the history contains client `tool_use`/`tool_result` blocks but the request sends no `tools` (MAX_DEPTH path): docs document a 400 only for server tools. Mitigation (send last-known tools with tool_choice none) is a design choice, not a verified requirement.
- Whether dropping ALL thinking blocks within a tool-use turn is tolerated on Sonnet 5.5 for this account: documented as a valid edit in preserved-thinking.md, but 'Required: within a tool-use turn, pass thinking blocks back' in thinking.md — confirm with acceptance #13 live.
- Jay's Anthropic account creation date (prefix-check enforcement applies to accounts created on/after 2026-08-31) — check Console. Irrelevant under the v1 no-replay decision, relevant for Option B.
- ha-mcp tool names' compliance with ^[a-zA-Z0-9_-]{1,128}$ and whether ha-mcp ever returns an empty result string (feat-ha-mcp).
- Whether feat-server targets docker-compose.yml (single module, YAML config) or docker-compose_all.yml with manager-api (which would make the manager-api SQL registration mandatory).
- Whether a custom `base_url` proxy (if ever configured) accepts the `eager_input_streaming` tool field — skill warning only; the fine-grained-tool-streaming doc page was not fetched.
- First-token latency difference between `thinking: between_tools` and adaptive at `effort: low` for voice turns — measure (acceptance #20).
- The parallel-tool-use.md statement is that splitting results 'reduces parallel tool use'; the stronger skill wording 'silently trains Claude to stop making parallel calls' is the skill's, not the doc's.
- PyPI JSON listed the latest version as 1.11.0 but its 'recent releases' ordering looked odd (0.125.0 first); the 1.11.0 release exists on PyPI and on the Aliyun mirror, upload date not checked.


## Review amendment (2026-10-06, external spec review F5/F6)

**Streaming tool-index contract:** track API block identity separately from the index exposed to `_merge_tool_calls`. Allocate an output index only when a tool is first emitted; emitted tools use contiguous indices from zero. A real tool is emitted only after its complete arguments parse. A dropped tool consumes no output index. `direct_answer` reserves an index on first emission and reuses it for every delta. Text/thinking blocks consume no tool index. Acceptance (owned by feat-voice-integration): invalid first tool then valid tool; valid/invalid/valid; dropped real tool then streamed `direct_answer` — no `IndexError`, dropped call never executed.

**Turn deadline:** see DESIGN ADR-11 (90 s absolute per foreground turn, shared by retries/fallback/tool rounds).
