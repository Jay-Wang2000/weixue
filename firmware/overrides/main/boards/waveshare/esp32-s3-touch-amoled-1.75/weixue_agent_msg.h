#ifndef WEIXUE_AGENT_MSG_H
#define WEIXUE_AGENT_MSG_H

#include <cJSON.h>

#include <cmath>
#include <cstdint>
#include <cstring>
#include <optional>
#include <string>
#include <vector>

// Agent wire contract v1 (docs/agent-protocol.md): parsing of server `agent` / `agent_sync`
// payloads and building of device `agent_response` / `played` notifications. No ESP-IDF types.
namespace weixue_agent {

constexpr int kVersion = 1;
constexpr size_t kMaxFrameBytes = 2048;
constexpr size_t kMaxNotificationBytes = 512;
constexpr size_t kMaxIdChars = 36;
constexpr size_t kMaxOptionIdChars = 32;
constexpr size_t kMaxTitleChars = 24;
constexpr size_t kMaxSummaryChars = 60;
constexpr size_t kMaxOptions = 4;
// Label width: Latin characters count 1, CJK characters 3 (12 Latin or 4 CJK).
constexpr size_t kMaxLabelWidth = 12;
constexpr size_t kMaxAnswerChars = 120;
constexpr size_t kMaxSyncIds = 32;

enum class State : uint8_t {
    kWorking,
    kNeedsInput,
    kNeedsApproval,
    kUnread,
    kDone,
    kFailed,
    kCancelled,
    kInterrupted,
    kStopped,
};
enum class Tier : uint8_t { kInterrupt, kWhenIdle, kSilent };
enum class Risk : uint8_t { kNone, kReversible, kIrreversible, kMoney };
enum class Playback : uint8_t { kNone, kPlayed, kCutOff, kUncertain };

inline const char* StateName(State s) {
    static const char* const kNames[] = {"working", "needs_input", "needs_approval",
                                         "unread",  "done",        "failed",
                                         "cancelled", "interrupted", "stopped"};
    return kNames[static_cast<int>(s)];
}
inline const char* TierName(Tier t) {
    static const char* const kNames[] = {"interrupt", "when_idle", "silent"};
    return kNames[static_cast<int>(t)];
}
inline bool IsTerminal(State s) {
    return s == State::kDone || s == State::kFailed || s == State::kCancelled ||
           s == State::kStopped;
}

struct Option {
    std::string id;
    std::string label;
};

// Fixture-only scripted answer; honoured only by WEIXUE_AGENT_LOOPBACK builds.
struct DebugReply {
    std::string kind;
    std::string option_id;
    std::string text;
};

struct Item {
    int64_t seq = 0;
    int64_t ts = 0;
    std::string chat_id;
    std::string task_id;
    State state = State::kWorking;
    std::string title;
    std::string summary;
    std::string lang = "en";
    bool has_progress = false;
    int progress_step = 0;
    int progress_total = -1;  // -1 = unknown
    std::string card_id;
    std::vector<Option> options;
    Risk risk = Risk::kNone;
    int timeout_s = -1;  // -1 = no timeout
    Tier tier = Tier::kWhenIdle;
    bool speaks = false;
    Playback playback = Playback::kNone;
    bool seen = false;
    std::optional<DebugReply> debug_reply;
};

struct Sync {
    int64_t seq = 0;
    std::vector<std::string> task_ids;
};

struct Message {
    enum class Kind : uint8_t { kItem, kSync } kind = Kind::kItem;
    Item item;
    Sync sync;
};

struct ParseResult {
    std::optional<Message> message;
    const char* error = nullptr;  // set when the payload is dropped
};

namespace detail {

inline size_t Utf8Len(unsigned char lead) {
    if (lead < 0x80) return 1;
    if ((lead >> 5) == 0x6) return 2;
    if ((lead >> 4) == 0xE) return 3;
    if ((lead >> 3) == 0x1E) return 4;
    return 1;
}

inline uint32_t DecodeAt(const std::string& s, size_t i, size_t len) {
    auto c = static_cast<unsigned char>(s[i]);
    if (len == 1) return c;
    uint32_t cp = c & (0xFF >> (len + 1));
    for (size_t k = 1; k < len; ++k) {
        cp = (cp << 6) | (static_cast<unsigned char>(s[i + k]) & 0x3F);
    }
    return cp;
}

// Truncates s to at most max_units, counting each code point by unit(cp); never splits a
// code point.
template <typename Unit>
inline std::string TruncateBy(const std::string& s, size_t max_units, Unit unit) {
    size_t used = 0;
    size_t i = 0;
    while (i < s.size()) {
        size_t len = Utf8Len(static_cast<unsigned char>(s[i]));
        if (i + len > s.size()) break;
        size_t u = unit(DecodeAt(s, i, len));
        if (used + u > max_units) break;
        used += u;
        i += len;
    }
    return s.substr(0, i);
}

inline bool IsAsciiId(const char* s, size_t max_chars) {
    size_t n = std::strlen(s);
    if (n == 0 || n > max_chars) return false;
    for (size_t i = 0; i < n; ++i) {
        auto c = static_cast<unsigned char>(s[i]);
        if (c <= 0x20 || c >= 0x7F || c == '"' || c == '\\' || c == '#' || c == '/' || c == '?') {
            return false;
        }
    }
    return true;
}

inline const char* String(const cJSON* obj, const char* key) {
    const cJSON* v = cJSON_GetObjectItemCaseSensitive(obj, key);
    return cJSON_IsString(v) ? v->valuestring : nullptr;
}

inline bool Integer(const cJSON* v, int64_t lo, int64_t hi, int64_t* out) {
    if (!cJSON_IsNumber(v)) return false;
    double d = v->valuedouble;
    if (std::isnan(d) || d != std::floor(d) || d < static_cast<double>(lo) ||
        d > static_cast<double>(hi)) {
        return false;
    }
    *out = static_cast<int64_t>(d);
    return true;
}

inline bool ParseState(const char* s, State* out) {
    for (int i = 0; i <= static_cast<int>(State::kStopped); ++i) {
        if (std::strcmp(s, StateName(static_cast<State>(i))) == 0) {
            *out = static_cast<State>(i);
            return true;
        }
    }
    return false;
}

inline bool ParseTier(const char* s, Tier* out) {
    for (int i = 0; i <= static_cast<int>(Tier::kSilent); ++i) {
        if (std::strcmp(s, TierName(static_cast<Tier>(i))) == 0) {
            *out = static_cast<Tier>(i);
            return true;
        }
    }
    return false;
}

constexpr int64_t kMaxSeq = 9007199254740991LL;  // 2^53 - 1, exact in a JSON double

}  // namespace detail

// Truncation to code points.
inline std::string TruncateChars(const std::string& s, size_t max_chars) {
    return detail::TruncateBy(s, max_chars, [](uint32_t) { return size_t{1}; });
}

// Truncation to display width: CJK and other wide code points (U+1100 and up) count 3.
inline std::string TruncateLabel(const std::string& s, size_t max_width = kMaxLabelWidth) {
    return detail::TruncateBy(s, max_width,
                              [](uint32_t cp) { return cp >= 0x1100 ? size_t{3} : size_t{1}; });
}

// The payload is an agent message (kind `agent` or `agent_sync`).
inline bool IsAgentPayload(const cJSON* payload) {
    const char* kind = cJSON_IsObject(payload) ? detail::String(payload, "kind") : nullptr;
    return kind != nullptr &&
           (std::strcmp(kind, "agent") == 0 || std::strcmp(kind, "agent_sync") == 0);
}

// Validates and normalises one payload. Unknown fields are ignored; an unknown kind or state,
// v other than 1, a bad id or a missing required field drops the payload.
inline ParseResult Parse(const cJSON* payload) {
    using namespace detail;
    ParseResult r;
    if (!cJSON_IsObject(payload)) {
        r.error = "payload is not an object";
        return r;
    }
    const char* kind = String(payload, "kind");
    if (kind == nullptr) {
        r.error = "no kind";
        return r;
    }
    int64_t v = 0;
    if (!Integer(cJSON_GetObjectItemCaseSensitive(payload, "v"), 1, 1000, &v)) {
        r.error = "bad v";
        return r;
    }
    if (v != kVersion) {
        r.error = "unsupported v";
        return r;
    }
    int64_t seq = 0;
    if (!Integer(cJSON_GetObjectItemCaseSensitive(payload, "seq"), 1, kMaxSeq, &seq)) {
        r.error = "bad seq";
        return r;
    }

    Message m;
    if (std::strcmp(kind, "agent_sync") == 0) {
        m.kind = Message::Kind::kSync;
        m.sync.seq = seq;
        const cJSON* ids = cJSON_GetObjectItemCaseSensitive(payload, "task_ids");
        if (!cJSON_IsArray(ids) || static_cast<size_t>(cJSON_GetArraySize(ids)) > kMaxSyncIds) {
            r.error = "bad task_ids";
            return r;
        }
        const cJSON* id = nullptr;
        cJSON_ArrayForEach(id, ids) {
            if (!cJSON_IsString(id) || !IsAsciiId(id->valuestring, kMaxIdChars)) {
                r.error = "bad task_ids";
                return r;
            }
            m.sync.task_ids.emplace_back(id->valuestring);
        }
        r.message = std::move(m);
        return r;
    }
    if (std::strcmp(kind, "agent") != 0) {
        r.error = "unknown kind";
        return r;
    }

    Item& it = m.item;
    it.seq = seq;
    int64_t ts = 0;
    if (Integer(cJSON_GetObjectItemCaseSensitive(payload, "ts"), 0, kMaxSeq, &ts)) {
        it.ts = ts;
    }
    const char* chat_id = String(payload, "chat_id");
    const char* task_id = String(payload, "task_id");
    if (chat_id == nullptr || !IsAsciiId(chat_id, kMaxIdChars) || task_id == nullptr ||
        !IsAsciiId(task_id, kMaxIdChars)) {
        r.error = "bad chat_id or task_id";
        return r;
    }
    it.chat_id = chat_id;
    it.task_id = task_id;
    const char* state = String(payload, "state");
    if (state == nullptr || !ParseState(state, &it.state)) {
        r.error = "unknown state";
        return r;
    }
    const char* summary = String(payload, "summary");
    if (summary == nullptr) {
        r.error = "no summary";
        return r;
    }
    it.summary = TruncateChars(summary, kMaxSummaryChars);
    if (const char* title = String(payload, "title")) {
        it.title = TruncateChars(title, kMaxTitleChars);
    }
    if (const char* lang = String(payload, "lang")) {
        if (std::strcmp(lang, "en") != 0 && std::strcmp(lang, "zh") != 0) {
            r.error = "bad lang";
            return r;
        }
        it.lang = lang;
    }
    const cJSON* progress = cJSON_GetObjectItemCaseSensitive(payload, "progress");
    if (cJSON_IsObject(progress)) {
        int64_t step = 0;
        int64_t total = 0;
        if (Integer(cJSON_GetObjectItemCaseSensitive(progress, "step"), 0, 10000, &step)) {
            it.has_progress = true;
            it.progress_step = static_cast<int>(step);
            if (Integer(cJSON_GetObjectItemCaseSensitive(progress, "total"), 1, 10000, &total)) {
                it.progress_total = static_cast<int>(total);
            }
        }
    }
    const cJSON* options = cJSON_GetObjectItemCaseSensitive(payload, "options");
    if (options != nullptr && !cJSON_IsNull(options)) {
        if (!cJSON_IsArray(options)) {
            r.error = "bad options";
            return r;
        }
        const cJSON* opt = nullptr;
        cJSON_ArrayForEach(opt, options) {
            if (it.options.size() >= kMaxOptions) break;
            const char* id = cJSON_IsObject(opt) ? String(opt, "id") : nullptr;
            const char* label = cJSON_IsObject(opt) ? String(opt, "label") : nullptr;
            if (id == nullptr || !IsAsciiId(id, kMaxOptionIdChars) || label == nullptr) {
                r.error = "bad option";
                return r;
            }
            it.options.push_back({id, TruncateLabel(label)});
        }
    }
    if (const char* card_id = String(payload, "card_id")) {
        if (!IsAsciiId(card_id, kMaxIdChars)) {
            r.error = "bad card_id";
            return r;
        }
        it.card_id = card_id;
    }
    if (!it.options.empty() && it.card_id.empty()) {
        r.error = "options without card_id";
        return r;
    }
    if (const char* risk = String(payload, "risk")) {
        if (std::strcmp(risk, "reversible") == 0) {
            it.risk = Risk::kReversible;
        } else if (std::strcmp(risk, "irreversible") == 0) {
            it.risk = Risk::kIrreversible;
        } else if (std::strcmp(risk, "money") == 0) {
            it.risk = Risk::kMoney;
        }
    }
    int64_t timeout = 0;
    if (Integer(cJSON_GetObjectItemCaseSensitive(payload, "timeout_s"), 0, 86400 * 7, &timeout)) {
        it.timeout_s = static_cast<int>(timeout);
    }
    if (const char* delivery = String(payload, "delivery")) {
        if (!ParseTier(delivery, &it.tier)) {
            r.error = "unknown delivery";
            return r;
        }
    }
    it.speaks = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(payload, "speaks"));
    it.seen = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(payload, "seen"));
    if (const char* playback = String(payload, "playback")) {
        if (std::strcmp(playback, "played") == 0) {
            it.playback = Playback::kPlayed;
        } else if (std::strcmp(playback, "cut_off") == 0) {
            it.playback = Playback::kCutOff;
        } else if (std::strcmp(playback, "uncertain") == 0) {
            it.playback = Playback::kUncertain;
        }
    }
    const cJSON* debug = cJSON_GetObjectItemCaseSensitive(payload, "debug_reply");
    if (cJSON_IsObject(debug)) {
        const char* dkind = String(debug, "kind");
        if (dkind != nullptr) {
            DebugReply d;
            d.kind = dkind;
            if (const char* o = String(debug, "option_id")) d.option_id = o;
            if (const char* t = String(debug, "text")) d.text = TruncateChars(t, kMaxAnswerChars);
            it.debug_reply = std::move(d);
        }
    }
    r.message = std::move(m);
    return r;
}

// Device → server notification `notifications/weixue/agent_response`.
struct Response {
    std::string kind;  // approve|deny|answer|cancel|ack_seen|switch_chat|new_chat|shown
    std::string chat_id;
    std::string task_id;
    std::string card_id;
    std::string option_id;
    std::string text;
    int64_t ack_upto = -1;  // shown only
    std::string via = "tap";
};

inline bool IsResponseKind(const std::string& kind) {
    static const char* const kKinds[] = {"approve",  "deny",        "answer",   "cancel",
                                         "ack_seen", "switch_chat", "new_chat", "shown"};
    for (const char* k : kKinds) {
        if (kind == k) return true;
    }
    return false;
}

namespace detail {

inline std::string Print(cJSON* root) {
    char* s = cJSON_PrintUnformatted(root);
    std::string out = s ? s : "";
    cJSON_free(s);
    cJSON_Delete(root);
    return out;
}

inline cJSON* Notification(const char* method, uint32_t boot, uint32_t seq, cJSON** params) {
    cJSON* root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "jsonrpc", "2.0");
    cJSON_AddStringToObject(root, "method", method);
    *params = cJSON_AddObjectToObject(root, "params");
    cJSON_AddNumberToObject(*params, "v", kVersion);
    cJSON_AddNumberToObject(*params, "boot", boot);
    cJSON_AddNumberToObject(*params, "seq", seq);
    return root;
}

inline void AddIfSet(cJSON* obj, const char* key, const std::string& value) {
    if (!value.empty()) cJSON_AddStringToObject(obj, key, value.c_str());
}

}  // namespace detail

// The JSON-RPC notification for r, or "" when r is invalid or exceeds kMaxNotificationBytes
// even with its text dropped.
inline std::string BuildResponse(uint32_t boot, uint32_t seq, const Response& r) {
    if (!IsResponseKind(r.kind)) return "";
    std::string text = TruncateChars(r.text, kMaxAnswerChars);
    for (int attempt = 0; attempt < 2; ++attempt) {
        cJSON* params = nullptr;
        cJSON* root = detail::Notification("notifications/weixue/agent_response", boot, seq, &params);
        cJSON_AddStringToObject(params, "kind", r.kind.c_str());
        detail::AddIfSet(params, "chat_id", r.chat_id);
        detail::AddIfSet(params, "task_id", r.task_id);
        detail::AddIfSet(params, "card_id", r.card_id);
        detail::AddIfSet(params, "option_id", r.option_id);
        detail::AddIfSet(params, "text", text);
        if (r.ack_upto >= 0) cJSON_AddNumberToObject(params, "ack_upto", static_cast<double>(r.ack_upto));
        cJSON_AddStringToObject(params, "via", r.via.c_str());
        std::string out = detail::Print(root);
        if (out.size() <= kMaxNotificationBytes) return out;
        text.clear();
    }
    return "";
}

// `notifications/weixue/played`: complete, or cut at cut_at_ms for reason.
inline std::string BuildPlayed(uint32_t boot, uint32_t seq, const std::string& sentence_id,
                               bool complete, uint32_t cut_at_ms, const char* reason) {
    if (sentence_id.empty() || sentence_id.size() > 64) return "";
    cJSON* params = nullptr;
    cJSON* root = detail::Notification("notifications/weixue/played", boot, seq, &params);
    cJSON_AddStringToObject(params, "sentence_id", sentence_id.c_str());
    cJSON_AddBoolToObject(params, "complete", complete);
    if (!complete) {
        cJSON_AddNumberToObject(params, "cut_at_ms", cut_at_ms);
        cJSON_AddStringToObject(params, "reason", reason ? reason : "closed");
    }
    std::string out = detail::Print(root);
    return out.size() <= kMaxNotificationBytes ? out : "";
}

}  // namespace weixue_agent

#endif  // WEIXUE_AGENT_MSG_H
