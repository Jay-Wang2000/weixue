"""API schemas: pydantic request models and the normative JSON Schemas (`tools/export_schemas.py`)."""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

ID_BASE = "https://weixue.local/agent/v1/"
ULID = "[0-9A-HJKMNP-TV-Z]{26}"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Origin(Strict):
    kind: Optional[Literal["voice", "panel", "sim", "api", "retry"]] = None
    gateway_session_id: Optional[str] = Field(None, max_length=128)
    turn_seq: Optional[int] = Field(None, ge=0)


class MessageBody(Strict):
    device_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=4000)
    lang: Literal["en", "zh", "auto"] = "auto"
    origin: Optional[Origin] = None
    retry_of: Optional[str] = Field(None, pattern=f"^tsk_{ULID}$")


class Answer(Strict):
    interaction_id: str = Field(pattern=f"^int_{ULID}$")
    decision_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    option_ids: Optional[List[str]] = Field(None, min_length=1, max_length=4)
    text: Optional[str] = Field(None, min_length=1, max_length=500)
    via: Literal["panel", "voice", "api"]

    @model_validator(mode="after")
    def one_of(self) -> "Answer":
        if (self.option_ids is None) == (self.text is None):
            raise ValueError("exactly one of option_ids or text is required")
        if self.option_ids and any(len(o) > 32 for o in self.option_ids):
            raise ValueError("option ids are at most 32 characters")
        return self


class Decide(Strict):
    interaction_id: str = Field(pattern=f"^int_{ULID}$")
    decision_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    via: Literal["panel", "voice", "api"]


class Interrupt(Strict):
    decision_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    reason: Literal["voice", "panel", "api"]


class Ack(Strict):
    lease_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    outcome: Literal["accepted", "spoken", "played", "cut_off", "shown", "user_ack", "deferred", "dropped", "lost"]
    proactive_id: Optional[str] = Field(None, max_length=128)
    ttl_s: Optional[int] = Field(None, ge=10, le=3600)
    retry_after_ms: Optional[int] = Field(None, ge=0)
    detail: Optional[str] = Field(None, max_length=200)
    sentence_id: Optional[str] = Field(None, max_length=64)
    cut_at_ms: Optional[int] = Field(None, ge=0)
    reason: Optional[str] = Field(None, max_length=32)
    boot: Optional[int] = Field(None, ge=0)
    device_seq: Optional[int] = Field(None, ge=0)
    attempt: Optional[int] = Field(None, ge=1)


class DeviceActionResult(Strict):
    ok: bool
    result: Optional[str] = Field(None, max_length=4000)
    error: Optional[str] = Field(None, max_length=300)


class DeviceRef(Strict):
    device_id: str = Field(min_length=1, max_length=64)


class ChatCreate(Strict):
    device_id: str = Field(min_length=1, max_length=64)
    switch: bool = True


class ConnectionEvent(Strict):
    kind: Literal["open", "close", "busy", "idle"]
    gateway_session_id: Optional[str] = Field(None, max_length=128)
    detail: Optional[str] = Field(None, max_length=200)


DEFS: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": ID_BASE + "defs.schema.json",
    "$defs": {
        "TaskId": {"type": "string", "pattern": f"^tsk_{ULID}$"},
        "ChatId": {"type": "string", "pattern": f"^cht_{ULID}$"},
        "TurnId": {"type": "string", "pattern": f"^trn_{ULID}$"},
        "InteractionId": {"type": "string", "pattern": f"^int_{ULID}$"},
        "DeliveryId": {"type": "string", "pattern": f"^dlv_{ULID}$"},
        "DeviceId": {"type": "string", "minLength": 1, "maxLength": 64},
        "TraceId": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "Time": {"type": "string", "format": "date-time"},
        "Lang": {"enum": ["en", "zh"]},
        "Tier": {"enum": ["INTERRUPT", "WHEN_IDLE", "SILENT"]},
        "TaskState": {"enum": ["progress", "needs_input", "needs_approval", "done", "failed", "cancelled"]},
        "TurnState": {"enum": ["running", "completed", "interrupted", "failed"]},
        "Outcome": {"enum": ["ok", "partial", "denied", "expired", "unknown", "none"]},
        "Progress": {"type": "object", "additionalProperties": False, "required": ["phase"],
                     "properties": {"step": {"type": ["integer", "null"]}, "label": {"type": "string", "maxLength": 40},
                                    "tool": {"type": ["string", "null"], "maxLength": 128},
                                    "phase": {"enum": ["thinking", "tool", "writing", "retrying", "waiting"]}}},
        "SideEffect": {"type": "object", "additionalProperties": False, "required": ["tool", "status"],
                       "properties": {"tool": {"type": "string", "maxLength": 128},
                                      "target": {"type": ["string", "null"], "maxLength": 128},
                                      "status": {"enum": ["confirmed", "failed", "unknown", "not_run"]}}},
        "Error": {"type": "object", "additionalProperties": False, "required": ["code", "message"],
                  "properties": {"code": {"enum": ["service_restart", "api_error", "rate_limit", "auth", "billing",
                                                   "refusal", "max_turns", "max_budget", "sdk_error", "agent_process",
                                                   "interrupt_timeout", "timeout", "busy", "internal"]},
                                 "message": {"type": "string", "maxLength": 300}}},
        "Option": {"type": "object", "additionalProperties": False, "required": ["id", "label"],
                   "properties": {"id": {"type": "string", "maxLength": 32}, "label": {"type": "string", "maxLength": 24}}},
        "Interaction": {
            "type": "object", "additionalProperties": False,
            "required": ["interaction_id", "task_id", "chat_id", "kind", "source", "state", "created_at"],
            "properties": {
                "interaction_id": {"$ref": "#/$defs/InteractionId"}, "task_id": {"$ref": "#/$defs/TaskId"},
                "chat_id": {"$ref": "#/$defs/ChatId"},
                "turn_id": {"anyOf": [{"$ref": "#/$defs/TurnId"}, {"type": "null"}]},
                "kind": {"enum": ["input", "approval"]}, "source": {"enum": ["sdk", "ha_mcp", "text"]},
                "state": {"enum": ["pending", "answered", "approved", "denied", "expired", "cancelled", "uncorrelated"]},
                "question": {"type": "object", "additionalProperties": False, "required": ["text"],
                             "properties": {"text": {"type": "string", "maxLength": 120},
                                            "header": {"type": "string", "maxLength": 12},
                                            "options": {"type": "array", "maxItems": 4, "items": {"$ref": "#/$defs/Option"}},
                                            "multi_select": {"type": "boolean", "default": False}}},
                "action": {"type": "object", "additionalProperties": False, "required": ["text", "tool"],
                           "properties": {"text": {"type": "string", "maxLength": 60},
                                          "detail": {"type": "string", "maxLength": 120},
                                          "tool": {"type": "string", "maxLength": 128},
                                          "risk": {"enum": ["low", "medium", "high", None]},
                                          "reversible": {"type": "boolean"}}},
                "voice_can_approve": {"type": "boolean"},
                "created_at": {"$ref": "#/$defs/Time"},
                "expires_at": {"anyOf": [{"$ref": "#/$defs/Time"}, {"type": "null"}]},
                "decision": {"type": ["object", "null"]}, "via": {"enum": ["panel", "voice", "api", "timeout", None]},
            },
            "oneOf": [{"required": ["question"]}, {"required": ["action"]}],
        },
    },
}

EVENT: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": ID_BASE + "event.schema.json",
    "type": "object", "additionalProperties": False,
    "required": ["v", "seq", "chat_id", "trace_id", "ts", "type", "data"],
    "properties": {
        "v": {"const": 1}, "seq": {"type": "integer", "minimum": 1},
        "chat_id": {"$ref": "defs.schema.json#/$defs/ChatId"},
        "task_id": {"anyOf": [{"$ref": "defs.schema.json#/$defs/TaskId"}, {"type": "null"}]},
        "turn_id": {"anyOf": [{"$ref": "defs.schema.json#/$defs/TurnId"}, {"type": "null"}]},
        "trace_id": {"$ref": "defs.schema.json#/$defs/TraceId"}, "ts": {"$ref": "defs.schema.json#/$defs/Time"},
        "type": {"enum": ["accepted", "started", "text", "reply_end", "progress", "interrupted", "needs_input",
                          "needs_approval", "resolved", "done", "failed", "cancelled", "device_action"]},
        "data": {"type": "object"},
    },
}

MESSAGE_RESPONSE: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": ID_BASE + "message-response.schema.json",
    "type": "object", "required": ["chat_id", "task_id", "turn_id", "routed", "duplicate"],
    "properties": {
        "chat_id": {"$ref": "defs.schema.json#/$defs/ChatId"},
        "task_id": {"anyOf": [{"$ref": "defs.schema.json#/$defs/TaskId"}, {"type": "null"}]},
        "turn_id": {"anyOf": [{"$ref": "defs.schema.json#/$defs/TurnId"}, {"type": "null"}]},
        "routed": {"enum": ["new_task", "steer", "continue", "answer", "approval", "cancel"]},
        "duplicate": {"type": "boolean"},
    },
}

TASK: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": ID_BASE + "task.schema.json",
    "type": "object",
    "required": ["task_id", "chat_id", "device_id", "state", "lang", "pending", "side_effects", "turn_ids",
                 "created_at", "updated_at", "trace_id", "retry_allowed"],
    "properties": {
        "task_id": {"$ref": "defs.schema.json#/$defs/TaskId"}, "chat_id": {"$ref": "defs.schema.json#/$defs/ChatId"},
        "device_id": {"$ref": "defs.schema.json#/$defs/DeviceId"}, "state": {"$ref": "defs.schema.json#/$defs/TaskState"},
        "title": {"type": ["string", "null"], "maxLength": 32}, "summary": {"type": ["string", "null"], "maxLength": 60},
        "lang": {"$ref": "defs.schema.json#/$defs/Lang"},
        "progress": {"anyOf": [{"$ref": "defs.schema.json#/$defs/Progress"}, {"type": "null"}]},
        "pending": {"type": "array", "items": {"$ref": "defs.schema.json#/$defs/Interaction"}},
        "outcome": {"anyOf": [{"$ref": "defs.schema.json#/$defs/Outcome"}, {"type": "null"}]},
        "error": {"anyOf": [{"$ref": "defs.schema.json#/$defs/Error"}, {"type": "null"}]},
        "side_effects": {"type": "array", "items": {"$ref": "defs.schema.json#/$defs/SideEffect"}},
        "turn_ids": {"type": "array", "items": {"$ref": "defs.schema.json#/$defs/TurnId"}},
        "current_turn_id": {"anyOf": [{"$ref": "defs.schema.json#/$defs/TurnId"}, {"type": "null"}]},
        "created_at": {"$ref": "defs.schema.json#/$defs/Time"}, "updated_at": {"$ref": "defs.schema.json#/$defs/Time"},
        "finished_at": {"anyOf": [{"$ref": "defs.schema.json#/$defs/Time"}, {"type": "null"}]},
        "trace_id": {"$ref": "defs.schema.json#/$defs/TraceId"},
        "retry_of": {"anyOf": [{"$ref": "defs.schema.json#/$defs/TaskId"}, {"type": "null"}]},
        "retry_allowed": {"type": "boolean"},
    },
}

REQUEST_MODELS = {
    "message.schema.json": MessageBody, "answer.schema.json": Answer, "approve.schema.json": Decide,
    "deny.schema.json": Decide, "interrupt.schema.json": Interrupt, "ack.schema.json": Ack,
    "chat-create.schema.json": ChatCreate, "device-ref.schema.json": DeviceRef,
    "connection.schema.json": ConnectionEvent, "device-action-result.schema.json": DeviceActionResult,
}


def all_schemas() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {"defs.schema.json": DEFS, "event.schema.json": EVENT,
                                      "message-response.schema.json": MESSAGE_RESPONSE, "task.schema.json": TASK}
    for name, model in REQUEST_MODELS.items():
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = ID_BASE + name
        out[name] = schema
    return out
