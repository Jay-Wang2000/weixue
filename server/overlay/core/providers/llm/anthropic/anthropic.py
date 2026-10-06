import json
import re
from types import SimpleNamespace

import anthropic

from config.logger import setup_logging
from core.providers.llm.base import LLMProviderBase

TAG = __name__
logger = setup_logging()

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TIMEOUT = 60
DEFAULT_MAX_RETRIES = 1
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
THINKING_MODES = ("between_tools", "adaptive")
TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
DIRECT_ANSWER = "direct_answer"
MISSING_RESULT = "no result returned"
TRUNCATED_TEXT = "Sorry, my reply got cut off. Could you say that again?"
REFUSAL_TEXT = "Sorry, I can't help with that one."


def _tool_item(index, tool_id, name, arguments):
    return SimpleNamespace(
        index=index,
        id=tool_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _has_tool_blocks(messages):
    for m in messages:
        for b in m["content"]:
            if b["type"] in ("tool_use", "tool_result"):
                return True
    return False


class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.model_name = config.get("model_name") or DEFAULT_MODEL
        self.fallback_model_name = config.get("fallback_model_name") or None
        api_key = config.get("api_key")
        self.api_key = api_key if isinstance(api_key, str) and api_key.strip() else None
        if self.api_key is None:
            logger.bind(tag=TAG).warning("LLM api_key 未配置，使用 ANTHROPIC_API_KEY 环境变量")
        elif "你" in self.api_key:
            logger.bind(tag=TAG).error("配置错误: LLM 的 API key 未设置")
        self.base_url = config.get("base_url") or None
        self.max_tokens = int(config.get("max_tokens") or DEFAULT_MAX_TOKENS)

        effort = str(config.get("effort") or "low").lower()
        if effort not in EFFORT_LEVELS:
            logger.bind(tag=TAG).warning(f"effort={effort} 无效，使用 low")
            effort = "low"
        thinking = str(config.get("thinking") or "adaptive").lower()
        if thinking not in THINKING_MODES:
            logger.bind(tag=TAG).warning(f"thinking={thinking} 无效，使用 adaptive")
            thinking = "adaptive"
        if thinking == "between_tools" and effort in ("xhigh", "max"):
            logger.bind(tag=TAG).warning(f"thinking=between_tools 下 effort={effort} 降为 high")
            effort = "high"
        self.effort = effort
        self.thinking = thinking

        self.client = anthropic.Anthropic(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self._timeout(config.get("timeout")),
            max_retries=int(config.get("max_retries", DEFAULT_MAX_RETRIES)),
        )
        self._last_tools = None

    @staticmethod
    def _timeout(value):
        if isinstance(value, dict):
            return anthropic.Timeout(
                pool=value.get("pool", 2.0),
                connect=value.get("connect", 3.0),
                write=value.get("write", 5.0),
                read=value.get("read", float(DEFAULT_TIMEOUT)),
            )
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
        return float(DEFAULT_TIMEOUT)

    # ------------------------------------------------------------------ public

    def response(self, session_id, dialogue, **kwargs):
        system, rest = self._split_system(dialogue)
        messages = self._convert_dialogue(rest)
        for text, _ in self._stream(system, messages, None, kwargs.get("max_tokens")):
            if text:
                yield text

    def response_with_functions(self, session_id, dialogue, functions=None, **kwargs):
        system, rest = self._split_system(dialogue)
        messages = self._convert_dialogue(rest)
        tools = self._convert_tools(functions)
        yield from self._stream(system, messages, tools, kwargs.get("max_tokens"))

    # -------------------------------------------------------------- conversion

    @staticmethod
    def _split_system(dialogue):
        parts, rest = [], []
        for m in dialogue:
            if m.get("role") == "system":
                content = m.get("content")
                if isinstance(content, str) and content.strip():
                    parts.append(content)
            else:
                rest.append(m)
        return ("\n\n".join(parts) or None), rest

    @staticmethod
    def _convert_dialogue(dialogue):
        """OpenAI-shaped history -> Messages API messages.

        Every tool_result is placed directly after the assistant turn that
        issued its tool_use; results with no matching tool_use are dropped,
        tool_use blocks with no result get an is_error result.
        """
        flat = []
        tool_use_ids = set()
        results_by_id = {}
        for m in dialogue:
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                blocks = []
                for n, tc in enumerate(m["tool_calls"]):
                    fn = tc.get("function") or {}
                    tool_id = tc.get("id") or f"toolu_missing_{len(tool_use_ids)}_{n}"
                    raw = fn.get("arguments") or "{}"
                    try:
                        args = json.loads(raw)
                    except (ValueError, TypeError):
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    blocks.append({"type": "tool_use", "id": tool_id, "name": fn.get("name"), "input": args})
                    tool_use_ids.add(tool_id)
                flat.append(("assistant", blocks))
            elif role == "tool":
                tool_id = m.get("tool_call_id")
                if tool_id not in tool_use_ids:
                    logger.bind(tag=TAG).warning("丢弃无对应 tool_use 的 tool_result")
                elif tool_id in results_by_id:
                    logger.bind(tag=TAG).warning("丢弃重复的 tool_result")
                else:
                    block = {"type": "tool_result", "tool_use_id": tool_id}
                    content = m.get("content")
                    if content is not None and str(content) != "":
                        block["content"] = str(content)
                    results_by_id[tool_id] = block
                flat.append(("user", []))
            elif role in ("user", "assistant"):
                content = m.get("content")
                text = content if isinstance(content, str) else ("" if content is None else str(content))
                flat.append((role, [{"type": "text", "text": text}] if text.strip() else []))

        def results_for(ids):
            out = []
            for tool_id in ids:
                block = results_by_id.get(tool_id)
                if block is None:
                    block = {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "is_error": True,
                        "content": MISSING_RESULT,
                    }
                out.append(block)
            return out

        final = []
        pending = []

        def push(role, blocks):
            if final and final[-1]["role"] == role:
                final[-1]["content"].extend(blocks)
            else:
                final.append({"role": role, "content": list(blocks)})

        for role, blocks in flat:
            if pending:
                if role == "user":
                    blocks = results_for(pending) + blocks
                else:
                    push("user", results_for(pending))
                pending = []
            if not blocks:
                continue
            push(role, blocks)
            if role == "assistant":
                pending = [b["id"] for b in final[-1]["content"] if b["type"] == "tool_use"]
        if pending:
            push("user", results_for(pending))

        if not final:
            return final
        if final[0]["role"] != "user":
            logger.bind(tag=TAG).warning("对话首条不是 user，插入占位")
            final.insert(0, {"role": "user", "content": [{"type": "text", "text": "[conversation start]"}]})
        if final[-1]["role"] != "user":
            logger.bind(tag=TAG).warning("对话末条不是 user，模型将拒绝该请求")
        return final

    def _convert_tools(self, functions):
        tools = []
        for f in functions or []:
            fn = f.get("function") or f
            name = fn.get("name")
            if not isinstance(name, str) or not TOOL_NAME_RE.match(name):
                logger.bind(tag=TAG).warning(f"跳过名称不合规的工具: {name!r}")
                continue
            schema = dict(fn.get("parameters") or {})
            schema["type"] = "object"
            schema.setdefault("properties", {})
            tool = {"name": name, "description": fn.get("description") or "", "input_schema": schema}
            if name == DIRECT_ANSWER and self.base_url is None:
                tool["eager_input_streaming"] = True
            tools.append(tool)
        if not tools:
            return None
        self._last_tools = tools
        return tools

    def _request_params(self, model, system, messages, tools, max_tokens=None):
        params = {
            "model": model,
            "max_tokens": int(max_tokens or self.max_tokens),
            "messages": messages,
        }
        if system:
            params["system"] = system
        if tools:
            params["tools"] = tools
            params["tool_choice"] = {"type": "auto"}
        elif self._last_tools and _has_tool_blocks(messages):
            params["tools"] = self._last_tools
            params["tool_choice"] = {"type": "none"}
        if model.startswith("claude-haiku-4-5"):
            return params
        params["output_config"] = {"effort": self.effort}
        if model.startswith("claude-sonnet-5-5") and self.thinking == "between_tools":
            params["thinking"] = {"type": "between_tools"}
        return params

    # --------------------------------------------------------------- streaming

    def _stream(self, system, messages, tools, max_tokens=None):
        params = self._request_params(self.model_name, system, messages, tools, max_tokens)
        yielded = False
        try:
            for item in self._stream_once(params):
                yielded = True
                yield item
            return
        except anthropic.BadRequestError as e:
            self._log_bad_request(e, params)
            raise
        except anthropic.APIStatusError as e:
            retryable = e.status_code in (200, 408, 409, 429) or e.status_code >= 500
            fallback = self.fallback_model_name
            if yielded or not retryable or not fallback or fallback == params["model"]:
                logger.bind(tag=TAG).error(f"LLM 请求失败 status={e.status_code} request_id={e.request_id}")
                raise
            logger.bind(tag=TAG).warning(
                f"LLM status={e.status_code}，切换到备用模型 {fallback} request_id={e.request_id}"
            )
        except anthropic.APIConnectionError as e:
            logger.bind(tag=TAG).error(f"LLM 连接失败: {type(e).__name__}")
            raise
        params = self._request_params(self.fallback_model_name, system, messages, tools, max_tokens)
        try:
            yield from self._stream_once(params)
        except anthropic.BadRequestError as e:
            self._log_bad_request(e, params)
            raise

    @staticmethod
    def _log_bad_request(e, params):
        roles = [m["role"] for m in params["messages"][-2:]]
        logger.bind(tag=TAG).error(
            f"LLM 400: {e.message} request_id={e.request_id} model={params['model']} "
            f"keys={sorted(params)} messages={len(params['messages'])} last_roles={roles}"
        )

    def _stream_once(self, params):
        ordinal_by_block = {}
        pending = {}
        dropped = []
        text_yielded = False
        tool_yielded = False
        with self.client.messages.stream(**params) as stream:
            try:
                for event in stream:
                    etype = getattr(event, "type", None)
                    if etype == "content_block_start":
                        cb = event.content_block
                        if cb.type != "tool_use":
                            continue
                        ordinal = len(ordinal_by_block)
                        ordinal_by_block[event.index] = ordinal
                        if cb.name == DIRECT_ANSWER:
                            tool_yielded = True
                            yield None, [_tool_item(ordinal, cb.id, cb.name, "")]
                        else:
                            pending[event.index] = {"id": cb.id, "name": cb.name, "args": ""}
                    elif etype == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            if delta.text:
                                text_yielded = True
                                yield delta.text, None
                        elif delta.type == "input_json_delta":
                            if not delta.partial_json or event.index not in ordinal_by_block:
                                continue
                            if event.index in pending:
                                pending[event.index]["args"] += delta.partial_json
                            else:
                                yield None, [
                                    _tool_item(ordinal_by_block[event.index], None, None, delta.partial_json)
                                ]
                    elif etype == "content_block_stop" and event.index in pending:
                        p = pending.pop(event.index)
                        args = p["args"] or "{}"
                        try:
                            json.loads(args)
                        except ValueError:
                            logger.bind(tag=TAG).warning(
                                f"工具 {p['name']} 的参数不是完整 JSON（{len(args)} 字符），已丢弃"
                            )
                            dropped.append(p["name"])
                            continue
                        tool_yielded = True
                        yield None, [_tool_item(ordinal_by_block[event.index], p["id"], p["name"], args)]
            except ValueError as e:
                logger.bind(tag=TAG).warning(f"SDK 无法解析流式工具参数: {e}")
                return
            final = stream.get_final_message()
            request_id = getattr(stream, "request_id", None)

        for p in pending.values():
            dropped.append(p["name"])
        usage = getattr(final, "usage", None)
        stop_reason = getattr(final, "stop_reason", None)
        logger.bind(tag=TAG).info(
            f"Token 消耗：输入 {getattr(usage, 'input_tokens', '未知')}，"
            f"输出 {getattr(usage, 'output_tokens', '未知')}，"
            f"缓存读取 {getattr(usage, 'cache_read_input_tokens', 0) or 0}；"
            f"stop_reason={stop_reason} request_id={request_id}"
        )
        if stop_reason in ("max_tokens", "model_context_window_exceeded"):
            logger.bind(tag=TAG).warning(f"LLM 输出被截断 stop_reason={stop_reason} dropped={dropped}")
            if dropped and not tool_yielded:
                yield TRUNCATED_TEXT, None
        elif stop_reason == "refusal":
            details = getattr(final, "stop_details", None)
            logger.bind(tag=TAG).warning(f"LLM 拒绝回答 category={getattr(details, 'category', None)}")
            if not tool_yielded and not text_yielded:
                yield REFUSAL_TEXT, None
