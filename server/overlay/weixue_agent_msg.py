"""Agent wire contract v1.1 (docs/agent-protocol.md): builders and validators of the server → device
`agent` / `agent_sync` / `agent_ack` payloads and of the device → server `agent_response` /
`played` notifications, the limits, and the agent-service delivery → wire mapping (the only
mapper)."""
import json
import re
import uuid
from urllib.parse import quote
from datetime import datetime, timezone

VERSION = 1
KIND_ITEM = "agent"
KIND_SYNC = "agent_sync"
KIND_ACK = "agent_ack"
STATES = (
    "working", "needs_input", "needs_approval", "unread", "done",
    "failed", "cancelled", "interrupted", "stopped",
)
TERMINAL = ("done", "failed", "cancelled", "stopped")
TIERS = ("interrupt", "when_idle", "silent")
RISKS = ("reversible", "irreversible", "money")
PLAYBACK = ("played", "cut_off", "uncertain")
LANGS = ("en", "zh")
RESPONSE_KINDS = ("approve", "deny", "answer", "cancel", "ack_seen", "switch_chat", "new_chat", "shown")
CARD_KINDS = ("approve", "deny", "answer")
VIA = ("tap", "loopback")
RECEIPT_RESULTS = ("accepted", "duplicate", "stale")
CUT_REASONS = ("wake", "tap", "imu", "mute", "closed", "superseded")

MAX_ITEM_BYTES = 1024
DEVICE_DROP_BYTES = 2048
MAX_POLL_BYTES = 4096
MAX_POLL_ITEMS = 8
# Poll bodies the gateway builds: with the response headers they fit one TCP segment (device MSS
# 1440 B).
POLL_BODY_BYTES = 1200
POLL_BODY_ENTRIES = 2
MAX_NOTIFICATION_BYTES = 512
MAX_ID_CHARS = 36
MAX_OPTION_ID_CHARS = 32
MAX_TITLE_CHARS = 24
MAX_SUMMARY_CHARS = 60
MAX_OPTIONS = 4
MAX_LABEL_WIDTH = 12
MAX_ANSWER_CHARS = 120
MAX_SYNC_IDS = 32
MAX_TIMEOUT_S = 7 * 86400
MAX_SEQ = 2 ** 53 - 1

_ID = re.compile(r"^[!$-.0-9:->@-\[\]-~]+$")  # printable ASCII except space, '"', '#', '/', '?' and '\'
_CJK = re.compile(r"[㐀-鿿]")


class WireError(ValueError):
    pass


def truncate_chars(text, limit):
    return text[:limit]


def truncate_label(text, width=MAX_LABEL_WIDTH):
    """Display-width truncation: code points from U+1100 count 3, others 1."""
    used, out = 0, []
    for ch in text:
        w = 3 if ord(ch) >= 0x1100 else 1
        if used + w > width:
            break
        used += w
        out.append(ch)
    return "".join(out)


def is_id(value, limit=MAX_ID_CHARS):
    return isinstance(value, str) and 0 < len(value) <= limit and bool(_ID.match(value))


def encode(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def size(payload):
    return len(encode(payload).encode())


def frame(payload):
    """The WebSocket text frame carrying payload on the custom channel."""
    return encode({"type": "custom", "payload": payload})


def _int(value, name, lo, hi):
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise WireError(f"{name} must be an integer from {lo} to {hi}")
    return value


def _str(value, name):
    if not isinstance(value, str):
        raise WireError(f"{name} must be a string")
    return value


def build_item(fields, seq, ts):
    """A validated `agent` payload from wire-named fields (unknown fields are refused);
    texts are truncated to the UI limits. Raises WireError."""
    if not isinstance(fields, dict):
        raise WireError("item must be an object")
    allowed = {
        "chat_id", "task_id", "state", "title", "summary", "lang", "progress", "card_id", "options",
        "risk", "timeout_s", "delivery", "speaks", "playback", "seen", "debug_reply",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise WireError(f"unknown fields: {sorted(unknown)}")
    out = {"kind": KIND_ITEM, "v": VERSION, "seq": _int(seq, "seq", 1, MAX_SEQ), "ts": int(ts)}
    for key in ("chat_id", "task_id"):
        if not is_id(fields.get(key)):
            raise WireError(f"{key} must be 1-{MAX_ID_CHARS} printable ASCII characters")
        out[key] = fields[key]
    state = fields.get("state")
    if state not in STATES:
        raise WireError(f"state must be one of {list(STATES)}")
    out["state"] = state
    if fields.get("title") is not None:
        out["title"] = truncate_chars(_str(fields["title"], "title"), MAX_TITLE_CHARS)
    summary = _str(fields.get("summary"), "summary")
    out["summary"] = truncate_chars(summary, MAX_SUMMARY_CHARS)
    lang = fields.get("lang") or ("zh" if _CJK.search(summary) else "en")
    if lang not in LANGS:
        raise WireError(f"lang must be one of {list(LANGS)}")
    out["lang"] = lang
    progress = fields.get("progress")
    if progress is not None:
        if state != "working" or not isinstance(progress, dict):
            raise WireError("progress is an object on working items only")
        step = _int(progress.get("step"), "progress.step", 0, 10000)
        total = progress.get("total")
        if total is not None:
            total = _int(total, "progress.total", 1, 10000)
        out["progress"] = {"step": step, "total": total}
    options = fields.get("options")
    if options is not None:
        if not isinstance(options, list) or not 1 <= len(options) <= MAX_OPTIONS:
            raise WireError(f"options must be a list of 1-{MAX_OPTIONS}")
        norm = []
        for opt in options:
            if not isinstance(opt, dict) or not is_id(opt.get("id"), MAX_OPTION_ID_CHARS):
                raise WireError("option id must be 1-32 printable ASCII characters")
            norm.append({"id": opt["id"], "label": truncate_label(_str(opt.get("label"), "option label"))})
        out["options"] = norm
    card_id = fields.get("card_id")
    if card_id is not None:
        if not is_id(card_id):
            raise WireError("card_id must be 1-36 printable ASCII characters")
        out["card_id"] = card_id
    if options is not None and card_id is None:
        raise WireError("card_id is required with options")
    risk = fields.get("risk")
    if risk is not None:
        if state != "needs_approval" or risk not in RISKS:
            raise WireError(f"risk is one of {list(RISKS)} on needs_approval items only")
        out["risk"] = risk
    if "timeout_s" in fields:
        timeout = fields["timeout_s"]
        out["timeout_s"] = None if timeout is None else _int(timeout, "timeout_s", 0, MAX_TIMEOUT_S)
    delivery = fields.get("delivery", "when_idle")
    if delivery not in TIERS:
        raise WireError(f"delivery must be one of {list(TIERS)}")
    out["delivery"] = delivery
    speaks = fields.get("speaks", False)
    if not isinstance(speaks, bool):
        raise WireError("speaks must be a boolean")
    out["speaks"] = speaks and delivery != "silent"
    playback = fields.get("playback")
    if playback is not None:
        if playback not in PLAYBACK or state not in ("unread", "failed"):
            raise WireError(f"playback is one of {list(PLAYBACK)} on unread or failed items only")
        out["playback"] = playback
    seen = fields.get("seen", False)
    if not isinstance(seen, bool):
        raise WireError("seen must be a boolean")
    if state in TERMINAL or state == "unread":
        out["seen"] = seen
    debug = fields.get("debug_reply")
    if debug is not None:
        if not isinstance(debug, dict) or debug.get("kind") not in RESPONSE_KINDS:
            raise WireError("debug_reply.kind must be a response kind")
        out["debug_reply"] = {k: debug[k] for k in ("kind", "option_id", "text") if isinstance(debug.get(k), str)}
    if size(out) > MAX_ITEM_BYTES:
        raise WireError(f"item exceeds {MAX_ITEM_BYTES} bytes")
    return out


def build_sync(seq, task_ids):
    ids = list(task_ids)
    if len(ids) > MAX_SYNC_IDS or not all(is_id(t) for t in ids):
        raise WireError("bad task_ids")
    return {"kind": KIND_SYNC, "v": VERSION, "seq": _int(seq, "seq", 1, MAX_SEQ), "task_ids": ids}


def build_receipt(boot, seq, response, result, card_id=None, decided=None):
    """`agent_ack`: the gateway's receipt of the device's response (boot, seq). result is
    accepted, duplicate (the card already has a decision: decided) or stale (not the task's
    current card)."""
    if result not in RECEIPT_RESULTS:
        raise WireError(f"result must be one of {list(RECEIPT_RESULTS)}")
    payload = {"kind": KIND_ACK, "v": VERSION, "boot": boot, "seq": seq, "response": response, "result": result}
    if card_id is not None:
        payload["card_id"] = card_id
    if decided is not None:
        payload["decided"] = decided
    return payload


def _common(params, max_bytes=MAX_NOTIFICATION_BYTES):
    if not isinstance(params, dict):
        raise WireError("params must be an object")
    if len(json.dumps(params, ensure_ascii=False).encode()) > max_bytes:
        raise WireError(f"notification exceeds {max_bytes} bytes")
    if params.get("v") != VERSION:
        raise WireError("unsupported v")
    return {
        "boot": _int(params.get("boot"), "boot", 0, 2 ** 32 - 1),
        "seq": _int(params.get("seq"), "seq", 0, 2 ** 32 - 1),
    }


def parse_response(params):
    """Validated `notifications/weixue/agent_response` params. Raises WireError."""
    out = _common(params)
    kind = params.get("kind")
    if kind not in RESPONSE_KINDS:
        raise WireError(f"kind must be one of {list(RESPONSE_KINDS)}")
    out["kind"] = kind
    for key in ("chat_id", "task_id", "card_id"):
        value = params.get(key)
        if value is not None:
            if not is_id(value):
                raise WireError(f"bad {key}")
            out[key] = value
    if kind in CARD_KINDS and "card_id" not in out:
        raise WireError(f"{kind} needs card_id")
    option = params.get("option_id")
    if option is not None:
        if not is_id(option, MAX_OPTION_ID_CHARS):
            raise WireError("bad option_id")
        out["option_id"] = option
    text = params.get("text")
    if text is not None:
        if kind != "answer" or not isinstance(text, str) or len(text) > MAX_ANSWER_CHARS:
            raise WireError(f"text is a string of at most {MAX_ANSWER_CHARS} characters on answer only")
        out["text"] = text
    if kind == "answer" and "option_id" not in out and "text" not in out:
        raise WireError("answer needs option_id or text")
    if kind == "shown":
        out["ack_upto"] = _int(params.get("ack_upto"), "ack_upto", 0, MAX_SEQ)
    via = params.get("via", "tap")
    if via not in VIA:
        raise WireError(f"via must be one of {list(VIA)}")
    out["via"] = via
    return out


def parse_played(params):
    """Validated `notifications/weixue/played` params. Raises WireError."""
    out = _common(params)
    sentence = params.get("sentence_id")
    if not isinstance(sentence, str) or not 0 < len(sentence) <= 64:
        raise WireError("bad sentence_id")
    out["sentence_id"] = sentence
    complete = params.get("complete")
    if not isinstance(complete, bool):
        raise WireError("complete must be a boolean")
    out["complete"] = complete
    if not complete:
        out["cut_at_ms"] = _int(params.get("cut_at_ms"), "cut_at_ms", 0, 3_600_000)
        reason = params.get("reason")
        if reason not in CUT_REASONS:
            raise WireError(f"reason must be one of {list(CUT_REASONS)}")
        out["reason"] = reason
    return out


def decision_id(device, boot, seq):
    """First-wins decision id of a device response (agent-service section 5)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"weixue:{device}:{boot}:{seq}"))


# agent-service Delivery → wire

_TASK_STATE = {"progress": "working"}
_TIER = {"INTERRUPT": "interrupt", "WHEN_IDLE": "when_idle", "SILENT": "silent"}
_APPROVAL_LABELS = {"en": ("Approve", "Deny"), "zh": ("批准", "拒绝")}


def _timeout_s(expires_at, now):
    if not expires_at:
        return None
    try:
        expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int(expires.timestamp() - now))


def from_delivery(offer, now=None):
    """(wire fields, speech text or None) of an agent-service delivery offer of kind `agent`."""
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    payload = offer.get("payload") or {}
    state = _TASK_STATE.get(payload.get("state"), payload.get("state"))
    if state == "done" and payload.get("unread", True):
        state = "unread"
    lang = payload.get("lang") if payload.get("lang") in LANGS else None
    fields = {
        "chat_id": offer.get("chat_id") or payload.get("chat_id"),
        "task_id": offer.get("task_id") or payload.get("task_id"),
        "state": state,
        "title": payload.get("title"),
        "summary": payload.get("summary") or "",
        "delivery": _TIER.get(offer.get("tier"), "silent"),
        "speaks": bool(offer.get("speak")) and offer.get("tier") != "SILENT",
    }
    if lang:
        fields["lang"] = lang
    progress = payload.get("progress")
    if state == "working" and isinstance(progress, dict) and isinstance(progress.get("step"), int):
        fields["progress"] = {"step": progress["step"], "total": progress.get("total")}
    interaction = payload.get("interaction") or {}
    if state in ("needs_input", "needs_approval") and interaction.get("interaction_id"):
        fields["card_id"] = interaction["interaction_id"]
        fields["timeout_s"] = _timeout_s(interaction.get("expires_at"), now)
        if state == "needs_approval":
            approve, deny = _APPROVAL_LABELS[lang or "en"]
            fields["options"] = [{"id": "approve", "label": approve}, {"id": "deny", "label": deny}]
            action = interaction.get("action") or {}
            fields["risk"] = "reversible" if action.get("reversible") else "irreversible"
        else:
            options = (interaction.get("question") or {}).get("options") or []
            if options:
                fields["options"] = [{"id": o["id"], "label": o["label"]} for o in options[:MAX_OPTIONS]]
    if state in ("unread", "failed") and payload.get("playback") in PLAYBACK:
        fields["playback"] = payload["playback"]
    if state in TERMINAL or state == "unread":
        fields["seen"] = bool(payload.get("seen", False))
    return fields, offer.get("speak") if fields["speaks"] else None


def response_route(device, response, lang="en"):
    """(method, path, body) of the agent-service route for a device response, or None when the
    response has no route (shown, or ack_seen without a chat). Raises WireError on a bad chat_id."""
    kind = response["kind"]
    chat = response.get("chat_id")
    if chat is not None:
        if not is_id(chat):
            raise WireError("bad chat_id")
        chat = quote(chat, safe="")
    decision = decision_id(device, response["boot"], response["seq"])
    if kind in ("approve", "deny") and chat:
        return "POST", f"/v1/chats/{chat}/{kind}", {
            "interaction_id": response["card_id"], "decision_id": decision, "via": "panel"}
    if kind == "answer" and chat:
        body = {"interaction_id": response["card_id"], "decision_id": decision, "via": "panel"}
        if "option_id" in response:
            body["option_ids"] = [response["option_id"]]
        else:
            body["text"] = response["text"]
        return "POST", f"/v1/chats/{chat}/answer", body
    if kind == "cancel" and chat:
        return "POST", f"/v1/chats/{chat}/interrupt", {"decision_id": decision, "reason": "panel"}
    if kind == "switch_chat" and chat:
        return "POST", f"/v1/chats/{chat}/switch", {"device_id": device}
    if kind == "new_chat":
        return "POST", "/v1/chats", {"device_id": device}
    if kind == "ack_seen" and chat:
        return "POST", f"/v1/chats/{chat}/read", {"device_id": device}
    return None
