#ifndef WEIXUE_AGENT_UI_H
#define WEIXUE_AGENT_UI_H

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <string>
#include <utility>
#include <vector>

#include "weixue_agent_store.h"

// Agent states on the face, the cards and the management view (weixue-agent-ui-v1 §3-§6):
// what to show for the task store, free of LVGL and ESP-IDF types. Times are device ms.
namespace agent_ui {

using weixue_agent::Option;
using weixue_agent::Risk;
using weixue_agent::State;
using weixue_agent::Task;
using weixue_agent::Tier;

namespace color {
constexpr uint32_t kEye = 0x00E0FF;
constexpr uint32_t kAttention = 0xFFB020;
constexpr uint32_t kAttentionText = 0xC8963C;
constexpr uint32_t kDone = 0x46D27A;
constexpr uint32_t kFail = 0xC05A50;
constexpr uint32_t kApprove = 0x22B5C4;
constexpr uint32_t kDeny = 0xB0585A;
constexpr uint32_t kText = 0xE8F4F7;
constexpr uint32_t kValue = 0x9FB4BC;
constexpr uint32_t kLabel = 0x6E8A92;
constexpr uint32_t kSub = 0x4E6268;
constexpr uint32_t kBorder = 0x2E6670;
constexpr uint32_t kTrack = 0x0F3A40;
constexpr uint32_t kScrollTrack = 0x1E3A40;
}  // namespace color

constexpr int64_t kIdleBeforeMiniMs = 1500;
constexpr int64_t kWhenIdleTouchGapMs = 5000;
constexpr int64_t kSendSlowMs = 4000;
constexpr int64_t kSendHardTimeoutMs = 15000;
constexpr int64_t kCardTapGuardMs = 400;
constexpr int64_t kSwitchTimeoutMs = 3000;
constexpr int64_t kOutcomeMs = 900;
constexpr int64_t kCardIdleLaterMs = 60000;
constexpr int64_t kHintMs = 10000;
constexpr int64_t kMoodMs = 3000;
constexpr int64_t kStaleUpdateMs = 120000;
constexpr int64_t kMainDimMs = 5 * 60000;
constexpr int64_t kUnreadFaceMs = 30 * 60000;
constexpr int64_t kStoppedFaceMs = 900;
constexpr int64_t kFlashPhaseMs = 300;
constexpr int64_t kFadeMs = 600;
constexpr int64_t kTransientMs = 6000;
constexpr int64_t kFastEarsMs = 30000;
constexpr size_t kMaxChats = 20;

enum class Glyph : uint8_t { kNone, kWorking, kQuestion, kApproval, kDone, kFailed };

struct Span {
    std::string text;
    uint32_t color;
};
using Line = std::vector<Span>;

inline std::string Join(const Line& line) {
    std::string out;
    for (const auto& s : line) out += s.text;
    return out;
}

// Small-face pose; kFull = the normal face (no complication).
enum class Pose : uint8_t { kFull, kMini, kAttentive, kConfused, kHappy, kSad };

inline const char* PoseName(Pose p) {
    static const char* const kNames[] = {"full", "mini", "attentive", "confused", "happy", "sad"};
    return kNames[static_cast<int>(p)];
}

struct Complication {
    bool show = false;
    std::string task_id;
    Glyph glyph = Glyph::kNone;
    float progress = -1;  // working: 0..1, -1 = unknown
    std::string main;
    uint32_t main_color = color::kText;
    Line status;
    int more = 0;
    std::string hint;
    Pose pose = Pose::kFull;
};

// Ring arc in clock degrees (0 = 12 o'clock, clockwise).
struct Arc {
    int16_t start = 0;
    int16_t span = 0;
    uint32_t color = 0;
    uint8_t opa = 0;
    bool rounded = true;
    bool operator==(const Arc& o) const {
        return start == o.start && span == o.span && color == o.color && opa == o.opa &&
               rounded == o.rounded;
    }
};

struct RingHints {
    static constexpr int kMax = 5;
    int count = 0;
    Arc arcs[kMax];
    const char* kind = "off";
    void Add(const Arc& a) {
        if (count < kMax) arcs[count++] = a;
    }
};

// Geometry of the ring cues (weixue-agent-ui-v1 §2.4).
constexpr int kProgressStart = 150, kProgressSpan = 60;
constexpr int kAskStart = 25, kAskSpan = 90;
constexpr int kEarRightStart = 25, kEarLeftStart = 295, kEarSpan = 40;
constexpr int kDotDeg = 0;

// Clock degrees to LVGL degrees (0 = 3 o'clock, clockwise), 0-359.
inline int ClockToLvDeg(int clock_deg) { return ((clock_deg - 90) % 360 + 360) % 360; }

// Ring rotation of the static cues for pixel-shift step x (the 60 s burn-in ring): +-2 degrees.
inline int CueRotation(int shift_x) { return 2 * shift_x; }

struct Row {
    std::string task_id;
    Glyph glyph = Glyph::kNone;
    float progress = -1;
    std::string title;
    Line status;
};

struct ChatRow {
    std::string chat_id;
    std::string title;
    Glyph glyph = Glyph::kNone;
    float progress = -1;
    std::string chip;  // state word, "" for an idle chat
    uint32_t chip_color = color::kValue;
    std::string age;   // "now", "2 min", "3 h", "2 d"
    bool current = false;
};

enum class CardKind : uint8_t { kNone, kApproval, kQuestion };
enum class CardPhase : uint8_t { kOpen, kSending, kNotSent, kOutcome };

struct CardView {
    CardKind kind = CardKind::kNone;
    CardPhase phase = CardPhase::kOpen;
    std::string task_id;
    std::string card_id;
    std::string body;
    Line context;
    std::vector<Option> options;
    bool tap_only = false;  // irreversible or money: voice cannot approve
    Line footer;
    int pressed = -1;  // control index highlighted while sending
    Glyph outcome_glyph = Glyph::kNone;
    std::string outcome;
    uint32_t outcome_color = color::kText;
};

// Effects of a store update for the face: one per task whose state changed.
struct Arrival {
    std::string task_id;
    State state;
    Tier tier;
    bool speaks;
};

enum class Earcon : uint8_t { kNone, kNeedsYou, kApproved, kDenied, kDone, kError, kChatSwitch };

inline const char* EarconName(Earcon e) {
    static const char* const kNames[] = {"none", "needs_you", "approved", "denied",
                                         "done", "error",     "chat_switch"};
    return kNames[static_cast<int>(e)];
}

// Flash of the ears in a state colour: count blinks of kFlashPhaseMs on / off.
struct Flash {
    int count = 0;
    uint32_t color = 0;
    int64_t start_ms = 0;
    bool fade_to_dot = false;
};

// Earcon and flash for an arrival (§3.2): silent items neither sound nor flash.
inline Earcon EarconFor(const Arrival& a) {
    if (a.tier == Tier::kSilent) return Earcon::kNone;
    switch (a.state) {
        case State::kNeedsApproval:
            return Earcon::kNeedsYou;
        case State::kNeedsInput:
            return a.speaks ? Earcon::kNone : Earcon::kNeedsYou;
        case State::kUnread:
            return a.speaks ? Earcon::kNone : Earcon::kDone;
        case State::kFailed:
        case State::kCancelled:
            return a.tier == Tier::kInterrupt ? Earcon::kError : Earcon::kNone;
        default:
            return Earcon::kNone;
    }
}

inline Flash FlashFor(const Arrival& a, int64_t now_ms) {
    Flash f;
    if (a.tier == Tier::kSilent) return f;
    f.start_ms = now_ms;
    switch (a.state) {
        case State::kNeedsApproval:
        case State::kNeedsInput:
            f.count = a.tier == Tier::kInterrupt ? 3 : 2;
            f.color = color::kAttention;
            break;
        case State::kUnread:
            f.count = 2;
            f.color = color::kDone;
            f.fade_to_dot = true;
            break;
        case State::kFailed:
        case State::kCancelled:
            f.count = 1;
            f.color = color::kFail;
            f.fade_to_dot = true;
            break;
        default:
            break;
    }
    return f;
}

// "4:58"; "0:00" at or after the deadline.
inline std::string FormatRemaining(int64_t remaining_ms) {
    int64_t s = remaining_ms <= 0 ? 0 : (remaining_ms + 999) / 1000;
    char buf[32];
    snprintf(buf, sizeof(buf), "%lld:%02lld", static_cast<long long>(s / 60),
             static_cast<long long>(s % 60));
    return buf;
}

// The countdown shown for remaining_ms: whole minutes (rounded up) until the last 10 s, then
// every second; the value changes at most once a minute before that.
inline std::string CountdownText(int64_t remaining_ms) {
    if (remaining_ms > 10000) {
        int64_t minutes = (remaining_ms + 59999) / 60000;
        return FormatRemaining(minutes * 60000);
    }
    return FormatRemaining(remaining_ms);
}

// "now", "4 min", "3 h", "2 d".
inline std::string FormatAge(int64_t age_ms) {
    char buf[32];
    int64_t s = std::max<int64_t>(0, age_ms / 1000);
    if (s < 60) return "now";
    if (s < 3600) {
        snprintf(buf, sizeof(buf), "%lld min", static_cast<long long>(s / 60));
    } else if (s < 86400) {
        snprintf(buf, sizeof(buf), "%lld h", static_cast<long long>(s / 3600));
    } else {
        snprintf(buf, sizeof(buf), "%lld d", static_cast<long long>(s / 86400));
    }
    return buf;
}

// Strips the label recolour marker so server text cannot change colours.
inline std::string Plain(const std::string& s) {
    std::string out = s;
    std::replace(out.begin(), out.end(), '#', ' ');
    return out;
}

// What the device knows about the moment, for card and complication decisions.
struct Context {
    bool voice_idle = true;   // device idle (no session)
    int64_t idle_since_ms = 0;
    int64_t last_touch_ms = -1000000;
    bool touch_down = false;  // a finger is on the panel now
    bool panel_shown = false;
    bool bedside = false;
    bool dimmed = false;
};

class Model {
public:
    struct Info {
        std::string task_id;
        int64_t seq = 0;
        State state = State::kWorking;
        int64_t state_since_ms = 0;  // first receipt of this state
        int64_t first_ms = 0;        // first receipt of the task
        int64_t updated_ms = 0;      // last receipt of any item
        int step = -1;
        std::string hhmm;            // local time of the state's first receipt
        std::string decided;         // "approve"|"deny"|"answer" sent for card_id
        std::string decided_card;
    };

    // Replaces the task list with the store's; returns one Arrival per task whose state (or
    // card) is new. hhmm is the local time ("" when the clock is not set).
    std::vector<Arrival> SetTasks(const std::vector<Task>& tasks, int64_t now_ms,
                                  const std::string& hhmm) {
        std::vector<Arrival> out;
        std::vector<Info> next;
        for (const Task& t : tasks) {
            const Info* old = FindInfo(t.item.task_id);
            Info info = old ? *old : Info();
            bool fresh = old == nullptr;
            info.task_id = t.item.task_id;
            if (fresh) info.first_ms = now_ms;
            bool changed_state = fresh || info.state != t.item.state;
            bool new_card = !fresh && !t.item.card_id.empty() &&
                            t.item.card_id != CardOf(old->task_id) && weixue_agent::IsOpenCard(t.item.state);
            if (fresh || t.item.seq != info.seq) info.updated_ms = now_ms;
            if (changed_state || new_card) {
                info.state_since_ms = now_ms;
                info.hhmm = hhmm;
                if (!t.seen) out.push_back({t.item.task_id, t.item.state, t.item.tier, t.item.speaks});
            }
            if (info.decided_card != t.item.card_id || !weixue_agent::IsOpenCard(t.item.state)) {
                info.decided.clear();
                info.decided_card.clear();
            }
            info.step = t.item.has_progress ? t.item.progress_step : -1;
            info.seq = t.item.seq;
            info.state = t.item.state;
            next.push_back(std::move(info));
        }
        infos_ = std::move(next);
        tasks_ = tasks;
        PruneDecisions();
        RememberChats();
        for (const Arrival& a : out) {
            if (a.state == State::kUnread || a.state == State::kFailed ||
                a.state == State::kCancelled || a.state == State::kStopped) {
                read_.erase(std::remove(read_.begin(), read_.end(), a.task_id), read_.end());
            }
            if (weixue_agent::IsOpenCard(a.state) && a.tier != Tier::kSilent) {
                const Task* t = Find(a.task_id);
                if (t != nullptr) {
                    DropPending(t->item.card_id);
                    pending_open_.push_back({t->item.card_id, a.tier});
                }
            }
        }
        if (new_chat_pending_) {
            for (const Task& t : tasks) {
                if (std::find(chats_before_new_.begin(), chats_before_new_.end(), t.item.chat_id) ==
                    chats_before_new_.end()) {
                    current_chat_ = t.item.chat_id;
                    new_chat_pending_ = false;
                    break;
                }
            }
        }
        if (current_chat_.empty() && !new_chat_pending_) {
            const Task* newest = nullptr;
            for (const Task& t : tasks) {
                if (newest == nullptr || t.item.seq > newest->item.seq) newest = &t;
            }
            if (newest != nullptr) current_chat_ = newest->item.chat_id;
        }
        return out;
    }

    const std::vector<Task>& tasks() const { return tasks_; }

    const Task* Find(const std::string& task_id) const {
        for (const auto& t : tasks_) {
            if (t.item.task_id == task_id) return &t;
        }
        return nullptr;
    }

    // Local read mark (tap): the result leaves the face at once.
    void MarkRead(const std::string& task_id) {
        if (std::find(read_.begin(), read_.end(), task_id) == read_.end()) read_.push_back(task_id);
    }

    bool IsRead(const Task& t) const {
        return t.seen || std::find(read_.begin(), read_.end(), t.item.task_id) != read_.end();
    }

    // Whether the card of t was decided on this device and awaits the server's next item.
    bool IsDecided(const Task& t) const {
        const Info* i = FindInfo(t.item.task_id);
        return i != nullptr && !i->decided.empty() && i->decided_card == t.item.card_id;
    }

    bool IsExpired(const Task& t, int64_t now_ms) const {
        return t.item.state == State::kNeedsApproval && t.deadline_ms >= 0 && now_ms >= t.deadline_ms;
    }

    // Face and list priority (approval > input > failed > unread > working, newest first);
    // 0 = not shown.
    int Rank(const Task& t, int64_t now_ms, bool face) const {
        const Info* info = FindInfo(t.item.task_id);
        int64_t since = info ? now_ms - info->state_since_ms : 0;
        switch (t.item.state) {
            case State::kNeedsApproval:
                if (IsDecided(t)) return face ? 0 : 1;
                if (IsExpired(t, now_ms)) return face ? 0 : 3;
                return 6;
            case State::kNeedsInput:
                if (IsDecided(t)) return face ? 0 : 1;
                return 5;
            case State::kFailed:
            case State::kCancelled:
                return IsRead(t) ? 0 : 4;
            case State::kStopped:
                if (IsRead(t)) return 0;
                return face && since >= kStoppedFaceMs ? 0 : 4;
            case State::kUnread:
                if (IsRead(t)) return 0;
                return face && since >= kUnreadFaceMs ? 0 : 3;
            case State::kWorking:
            case State::kInterrupted:
                return 2;
            case State::kDone:
                return 0;
        }
        return 0;
    }

    // Displayable tasks, best first.
    std::vector<const Task*> Ranked(int64_t now_ms, bool face) const {
        std::vector<const Task*> out;
        for (const auto& t : tasks_) {
            if (Rank(t, now_ms, face) > 0) out.push_back(&t);
        }
        std::stable_sort(out.begin(), out.end(), [&](const Task* a, const Task* b) {
            int ra = Rank(*a, now_ms, face), rb = Rank(*b, now_ms, face);
            if (ra != rb) return ra > rb;
            return a->item.seq > b->item.seq;
        });
        return out;
    }

    static float Progress(const Task& t) {
        if (!t.item.has_progress || t.item.progress_total <= 0) return -1;
        return std::clamp(static_cast<float>(t.item.progress_step) / t.item.progress_total, 0.0f, 1.0f);
    }

    static std::string TitleOf(const Task& t) {
        return Plain(t.item.title.empty() ? t.item.summary : t.item.title);
    }

    static Glyph GlyphFor(State s) {
        switch (s) {
            case State::kNeedsApproval: return Glyph::kApproval;
            case State::kNeedsInput: return Glyph::kQuestion;
            case State::kUnread:
            case State::kDone: return Glyph::kDone;
            case State::kFailed:
            case State::kCancelled:
            case State::kStopped: return Glyph::kFailed;
            case State::kWorking:
            case State::kInterrupted: return Glyph::kWorking;
        }
        return Glyph::kNone;
    }

    // "Step 2 of 5 · 4 min" / "Working · 4 min", plus " · 3 min ago" after kStaleUpdateMs
    // without an update.
    Line WorkingStatus(const Task& t, int64_t now_ms) const {
        const Info* info = FindInfo(t.item.task_id);
        Line line;
        std::string head;
        if (t.item.state == State::kInterrupted) {
            head = "Interrupted";
        } else if (t.item.has_progress && t.item.progress_total > 0) {
            head = "Step " + std::to_string(t.item.progress_step) + " of " +
                   std::to_string(t.item.progress_total);
        } else {
            head = "Working";
        }
        line.push_back({head, color::kLabel});
        if (info != nullptr) {
            int64_t minutes = (now_ms - info->first_ms) / 60000;
            if (minutes >= 1) line.push_back({" · " + std::to_string(minutes) + " min", color::kLabel});
            int64_t quiet = now_ms - info->updated_ms;
            if (quiet >= kStaleUpdateMs) {
                line.push_back({" · " + std::to_string(quiet / 60000) + " min ago", color::kLabel});
            }
        }
        return line;
    }

    Complication Face(int64_t now_ms) const {
        Complication c;
        auto ranked = Ranked(now_ms, true);
        if (ranked.empty()) return c;
        const Task& t = *ranked.front();
        const Info* info = FindInfo(t.item.task_id);
        int64_t since = info ? now_ms - info->state_since_ms : 0;
        c.show = true;
        c.task_id = t.item.task_id;
        c.more = static_cast<int>(ranked.size()) - 1;
        c.glyph = GlyphFor(t.item.state);
        c.progress = Progress(t);
        c.main = TitleOf(t);
        c.pose = Pose::kMini;
        switch (t.item.state) {
            case State::kNeedsApproval:
                c.main = "Needs your OK";
                c.main_color = color::kAttention;
                c.status = {{TitleOf(t), color::kValue}};
                if (t.deadline_ms >= 0) {
                    c.status.push_back({" · " + CountdownText(t.deadline_ms - now_ms), color::kLabel});
                }
                c.pose = Pose::kAttentive;
                if (since < kHintMs) c.hint = "tap, or say \"show me\"";
                break;
            case State::kNeedsInput:
                c.main = "Question";
                c.main_color = color::kAttention;
                c.status = {{TitleOf(t), color::kValue}, {" · tap or say \"show me\"", color::kLabel}};
                c.pose = Pose::kConfused;
                break;
            case State::kUnread:
                c.status = {{"Done", color::kDone}, {" · tap to hear", color::kLabel}};
                if (since < kMoodMs) c.pose = Pose::kHappy;
                break;
            case State::kFailed:
                c.status = {{"Failed", color::kFail}, {" · tap for why", color::kLabel}};
                if (since < kMoodMs) c.pose = Pose::kSad;
                break;
            case State::kCancelled:
                c.status = {{"Cancelled", color::kFail}, {" · tap for why", color::kLabel}};
                if (since < kMoodMs) c.pose = Pose::kSad;
                break;
            case State::kStopped:
                c.status = {{"Stopped", color::kLabel}};
                break;
            case State::kWorking:
            case State::kInterrupted:
                c.status = WorkingStatus(t, now_ms);
                break;
            case State::kDone:
                break;
        }
        if (c.main_color == color::kText && info != nullptr && now_ms - info->updated_ms >= kMainDimMs) {
            c.main_color = color::kValue;
        }
        return c;
    }

    // Ring cues while no voice state owns the ring (§2.4): flash > ears > ask > progress (bottom,
    // coexists with ears or ask) > dot.
    RingHints Ring(int64_t now_ms, int rotate_deg) const {
        RingHints h;
        auto ranked = Ranked(now_ms, true);
        const Task* top = ranked.empty() ? nullptr : ranked.front();
        const Task* working = nullptr;
        for (const Task* t : ranked) {
            if (t->item.state == State::kWorking || t->item.state == State::kInterrupted) {
                working = t;
                break;
            }
        }
        auto ears = [&](uint32_t col, uint8_t opa) {
            h.Add({static_cast<int16_t>(kEarRightStart + rotate_deg), kEarSpan, col, opa, true});
            h.Add({static_cast<int16_t>(kEarLeftStart + rotate_deg), kEarSpan, col, opa, true});
        };
        int64_t ft = now_ms - flash_.start_ms;
        int64_t flash_len = flash_.count * 2 * kFlashPhaseMs;
        if (flash_.count > 0 && ft >= 0 && ft < flash_len) {
            if ((ft / kFlashPhaseMs) % 2 == 0) ears(flash_.color, 255);
            h.kind = "flash";
            return h;
        }
        bool top_ears = top != nullptr && top->item.state == State::kNeedsApproval;
        bool top_ask = top != nullptr && top->item.state == State::kNeedsInput;
        if (top_ears) {
            int64_t remaining = top->deadline_ms >= 0 ? top->deadline_ms - now_ms : INT64_MAX;
            float hz = remaining <= kFastEarsMs ? 1.0f : 0.5f;
            ears(color::kAttention, Breathe(now_ms, hz, 96, 224));
            h.kind = "ears";
        } else if (top_ask) {
            h.Add({static_cast<int16_t>(kAskStart + rotate_deg), kAskSpan, color::kAttention, 191, true});
            h.kind = "ask";
        }
        if (working != nullptr) {
            float p = Progress(*working);
            int fill = p < 0 ? kProgressSpan : static_cast<int>(kProgressSpan * p + 0.5f);
            int16_t start = static_cast<int16_t>(kProgressStart + rotate_deg);
            h.Add({start, kProgressSpan, color::kTrack, 255, false});
            uint8_t opa = top_ears ? 217 : Breathe(now_ms, 0.25f, 102, 217);
            if (fill > 0) h.Add({start, static_cast<int16_t>(fill), color::kEye, opa, false});
            if (!top_ears && !top_ask) h.kind = "progress";
        }
        if (!top_ears && !top_ask) {
            uint32_t dot = 0;
            for (const Task* t : ranked) {
                if (t->item.state == State::kFailed || t->item.state == State::kCancelled) {
                    dot = color::kFail;
                    break;
                }
                if (t->item.state == State::kUnread && dot == 0) dot = color::kDone;
            }
            if (dot == 0) dot = UnreadDot(now_ms);
            uint8_t opa = 255;
            if (flash_.count > 0 && flash_.fade_to_dot && ft >= flash_len && ft < flash_len + kFadeMs) {
                opa = static_cast<uint8_t>(255 * (ft - flash_len) / kFadeMs) & 0xE0;
            }
            if (dot != 0 && opa > 0) {
                h.Add({static_cast<int16_t>(kDotDeg + rotate_deg), 1, dot, opa, true});
                if (working == nullptr) h.kind = "dot";
            }
        }
        return h;
    }

    // Unread results that have left the face keep the dot.
    uint32_t UnreadDot(int64_t now_ms) const {
        for (const auto& t : tasks_) {
            if (t.item.state == State::kUnread && !IsRead(t) && Rank(t, now_ms, false) > 0) {
                return color::kDone;
            }
        }
        return 0;
    }

    // Opacity breathing at hz between lo and hi (triangle wave), quantised to multiples of 32.
    static uint8_t Breathe(int64_t now_ms, float hz, int lo, int hi) {
        int64_t period = static_cast<int64_t>(1000.0f / hz);
        int64_t ph = now_ms % period;
        float tri = ph < period / 2 ? static_cast<float>(ph) / (period / 2)
                                    : static_cast<float>(period - ph) / (period / 2);
        int v = lo + static_cast<int>((hi - lo) * tri);
        return static_cast<uint8_t>(std::clamp(v, 0, 255) & 0xE0);
    }

    void StartFlash(const Flash& f) {
        if (f.count > 0) flash_ = f;
    }
    const Flash& flash() const { return flash_; }

    // Management view rows: the best max rows; *more = how many are left out.
    std::vector<Row> Rows(int64_t now_ms, size_t max, int* more) const {
        auto ranked = Ranked(now_ms, false);
        std::vector<Row> out;
        for (size_t i = 0; i < ranked.size() && i < max; ++i) {
            const Task& t = *ranked[i];
            const Info* info = FindInfo(t.item.task_id);
            Row r;
            r.task_id = t.item.task_id;
            r.glyph = GlyphFor(t.item.state);
            r.progress = Progress(t);
            r.title = TitleOf(t);
            std::string at = info && !info->hhmm.empty() ? " · " + info->hhmm : "";
            switch (t.item.state) {
                case State::kNeedsApproval:
                    if (IsDecided(t)) {
                        r.glyph = Glyph::kWorking;
                        r.status = {{info->decided == "approve" ? "Approved" : "Denied", color::kValue},
                                    {" · sent", color::kLabel}};
                    } else if (IsExpired(t, now_ms)) {
                        r.glyph = Glyph::kFailed;
                        r.status = {{"Timed out", color::kFail}};
                    } else {
                        r.status = {{"Needs your OK", color::kAttentionText}};
                        if (t.deadline_ms >= 0) {
                            r.status.push_back({" · " + CountdownText(t.deadline_ms - now_ms), color::kLabel});
                        }
                    }
                    break;
                case State::kNeedsInput:
                    if (IsDecided(t)) {
                        r.glyph = Glyph::kWorking;
                        r.status = {{"Answered", color::kValue}, {" · sent", color::kLabel}};
                    } else {
                        r.status = {{"Question", color::kAttentionText}, {at, color::kLabel}};
                    }
                    break;
                case State::kUnread:
                    r.status = {{"Done · unread", color::kDone}, {at, color::kLabel}};
                    break;
                case State::kFailed:
                    r.status = {{"Failed", color::kFail}, {at, color::kLabel}};
                    break;
                case State::kCancelled:
                    r.status = {{"Cancelled", color::kFail}, {at, color::kLabel}};
                    break;
                case State::kStopped:
                    r.status = {{"Stopped", color::kLabel}, {at, color::kLabel}};
                    break;
                case State::kWorking:
                case State::kInterrupted: {
                    std::string word = t.item.state == State::kInterrupted ? "Interrupted" : "Working";
                    r.status = {{word, color::kValue}};
                    if (t.item.has_progress && t.item.progress_total > 0) {
                        r.status.push_back({" · step " + std::to_string(t.item.progress_step) + " of " +
                                                std::to_string(t.item.progress_total),
                                            color::kLabel});
                    }
                    break;
                }
                case State::kDone:
                    break;
            }
            if (!current_chat_.empty() && t.item.chat_id != current_chat_) {
                r.status.push_back({" · " + ChatTitle(t.item.chat_id), color::kSub});
            }
            out.push_back(std::move(r));
        }
        if (more != nullptr) *more = static_cast<int>(ranked.size()) - static_cast<int>(out.size());
        return out;
    }

    // Highest-ranked open card that is not decided or expired, for the panel's Next up.
    const Task* NextUp(int64_t now_ms) const {
        for (const Task* t : Ranked(now_ms, true)) {
            if (weixue_agent::IsOpenCard(t->item.state)) return t;
        }
        return nullptr;
    }

    // Chats

    struct ChatInfo {
        std::string chat_id;
        std::string title;
        int64_t last_ms = 0;
        int64_t title_seq = 0;
    };

    const std::string& current_chat() const { return current_chat_; }
    bool new_chat_pending() const { return new_chat_pending_; }

    std::string ChatTitle(const std::string& chat_id) const {
        for (const auto& c : chats_) {
            if (c.chat_id == chat_id) return c.title;
        }
        return chat_id;
    }

    // Header text of the current chat.
    std::string CurrentTitle() const {
        if (new_chat_pending_ || current_chat_.empty()) return "New chat";
        return ChatTitle(current_chat_);
    }

    void SetCurrentChat(const std::string& chat_id) {
        current_chat_ = chat_id;
        new_chat_pending_ = false;
    }

    // A new chat was requested: the next unknown chat id becomes current.
    void StartNewChat() {
        chats_before_new_.clear();
        for (const auto& c : chats_) chats_before_new_.push_back(c.chat_id);
        new_chat_pending_ = true;
        current_chat_.clear();
    }

    // The current chat first, then the others by last activity, newest first (New chat is the
    // view's fixed entry above the list).
    std::vector<ChatRow> Chats(int64_t now_ms) const {
        std::vector<ChatInfo> sorted = chats_;
        std::stable_sort(sorted.begin(), sorted.end(), [&](const ChatInfo& a, const ChatInfo& b) {
            bool ca = a.chat_id == current_chat_, cb = b.chat_id == current_chat_;
            if (ca != cb) return ca;
            return a.last_ms > b.last_ms;
        });
        std::vector<ChatRow> out;
        for (const auto& c : sorted) {
            ChatRow r;
            r.chat_id = c.chat_id;
            r.title = c.title;
            r.age = FormatAge(now_ms - c.last_ms);
            r.current = c.chat_id == current_chat_;
            const Task* best = nullptr;
            for (const auto& t : tasks_) {
                if (t.item.chat_id != c.chat_id || Rank(t, now_ms, false) == 0) continue;
                if (best == nullptr || Rank(t, now_ms, false) > Rank(*best, now_ms, false)) best = &t;
            }
            if (best != nullptr) {
                r.glyph = GlyphFor(best->item.state);
                r.progress = Progress(*best);
                switch (best->item.state) {
                    case State::kNeedsApproval:
                        r.chip = "Waiting for you";
                        r.chip_color = color::kAttentionText;
                        break;
                    case State::kNeedsInput:
                        r.chip = "Question";
                        r.chip_color = color::kAttentionText;
                        break;
                    case State::kUnread:
                        r.chip = "Unread";
                        r.chip_color = color::kDone;
                        break;
                    case State::kFailed:
                    case State::kCancelled:
                    case State::kStopped:
                        r.chip = best->item.state == State::kFailed ? "Failed" : "Stopped";
                        r.chip_color = color::kFail;
                        break;
                    case State::kWorking:
                    case State::kInterrupted:
                        r.chip = "Working";
                        if (best->item.has_progress && best->item.progress_total > 0) {
                            r.chip += " " + std::to_string(best->item.progress_step) + "/" +
                                      std::to_string(best->item.progress_total);
                        }
                        r.chip_color = color::kValue;
                        break;
                    case State::kDone:
                        break;
                }
            }
            out.push_back(std::move(r));
        }
        return out;
    }

    // Cards

    // Card to open without a tap (§3.2), or "" for none: INTERRUPT once no finger is down and
    // the panel was untouched for kCardTapGuardMs, WHEN_IDLE once the device is idle and
    // untouched and the face is showing.
    std::string WantAutoOpen(const Context& ctx, int64_t now_ms) {
        pending_open_.erase(
            std::remove_if(pending_open_.begin(), pending_open_.end(),
                           [&](const PendingOpen& p) { return CardTaskId(p.card_id, now_ms).empty(); }),
            pending_open_.end());
        if (card_.kind != CardKind::kNone || ctx.touch_down) return "";
        if (now_ms - ctx.last_touch_ms >= kCardTapGuardMs) {
            for (const auto& p : pending_open_) {
                if (p.tier == Tier::kInterrupt) return CardTaskId(p.card_id, now_ms);
            }
        }
        if (ctx.panel_shown || ctx.bedside || ctx.dimmed || !ctx.voice_idle ||
            now_ms - ctx.idle_since_ms < kIdleBeforeMiniMs ||
            now_ms - ctx.last_touch_ms < kWhenIdleTouchGapMs) {
            return "";
        }
        for (const auto& p : pending_open_) {
            if (p.tier != Tier::kInterrupt) return CardTaskId(p.card_id, now_ms);
        }
        return "";
    }

    bool HasInterruptPending(int64_t now_ms) const {
        for (const auto& p : pending_open_) {
            if (p.tier == Tier::kInterrupt && !CardTaskId(p.card_id, now_ms).empty()) return true;
        }
        return false;
    }

    // Whether an undecided open card is waiting for a WHEN_IDLE open (bedside dot).
    bool HasPendingOpen() const { return !pending_open_.empty(); }

    // Opens the card of task_id; false when it has no open, undecided, unexpired card.
    bool OpenCard(const std::string& task_id, int64_t now_ms) {
        const Task* t = Find(task_id);
        if (t == nullptr || !weixue_agent::IsOpenCard(t->item.state) || IsDecided(*t) ||
            IsExpired(*t, now_ms) || t->item.card_id.empty()) {
            return false;
        }
        card_ = CardState();
        Snapshot(*t, now_ms);
        card_.touched_ms = now_ms;
        return true;
    }

    bool card_open() const { return card_.kind != CardKind::kNone; }
    const std::string& card_task() const { return card_.task_id; }

    // Touches within kCardTapGuardMs of the card's content appearing are ignored.
    bool CardTapGuarded(int64_t now_ms) const { return now_ms - card_.opened_ms < kCardTapGuardMs; }

    // A touch or a voice turn on the card restarts its 60 s idle Later.
    void TouchCard(int64_t now_ms) { card_.touched_ms = now_ms; }

    // Card content for rendering; the options are the ones captured when the card opened.
    CardView Card(int64_t now_ms) const {
        CardView v;
        if (card_.kind == CardKind::kNone) return v;
        const DecisionRecord* rec = FindDecision(card_.card_id);
        v.kind = card_.kind;
        v.phase = card_.phase;
        if (v.phase == CardPhase::kOpen && rec != nullptr) {
            v.phase = rec->phase == SendPhase::kNotSent ? CardPhase::kNotSent : CardPhase::kSending;
        }
        v.task_id = card_.task_id;
        v.card_id = card_.card_id;
        v.pressed = rec != nullptr ? rec->pressed : -1;
        v.options = card_.options;
        for (auto& o : v.options) o.label = Plain(o.label);
        const Task* t = Find(card_.task_id);
        if (t != nullptr) {
            v.body = Plain(t->item.summary);
            std::string title = TitleOf(*t);
            std::string chat = !current_chat_.empty() && t->item.chat_id != current_chat_
                                   ? ChatTitle(t->item.chat_id) + " · "
                                   : "";
            if (card_.kind == CardKind::kApproval) {
                v.tap_only = t->item.risk == Risk::kIrreversible || t->item.risk == Risk::kMoney;
                const char* risk = t->item.risk == Risk::kIrreversible ? "irreversible"
                                   : t->item.risk == Risk::kMoney      ? "spends money"
                                   : t->item.risk == Risk::kReversible ? "reversible"
                                                                       : nullptr;
                v.context = {{chat + title, color::kValue}};
                if (risk != nullptr) {
                    v.context.push_back({" · ", color::kValue});
                    v.context.push_back({risk, v.tap_only ? color::kFail : color::kValue});
                }
            } else {
                std::string step;
                if (t->item.has_progress && t->item.progress_total > 0) {
                    step = " · step " + std::to_string(t->item.progress_step) + " of " +
                           std::to_string(t->item.progress_total);
                }
                v.context = {{chat + title + step, color::kLabel}};
            }
        }
        switch (v.phase) {
            case CardPhase::kOpen:
                if (!card_.heard.empty() && now_ms - card_.heard_ms < 3000) {
                    v.footer = {{Plain(card_.heard), color::kValue}};
                } else if (card_.kind == CardKind::kApproval) {
                    v.footer = {{v.tap_only ? "Tap Approve, or say \"deny\"" : "Say \"approve\" or \"deny\"",
                                 color::kLabel}};
                    if (t != nullptr && t->deadline_ms >= 0) {
                        v.footer.push_back({" · " + CountdownText(t->deadline_ms - now_ms), color::kAttentionText});
                    }
                } else {
                    v.footer = {{"Or just say it", color::kSub}};
                }
                break;
            case CardPhase::kSending:
                v.footer = {{SendingWord(rec->kind) +
                                 (now_ms - rec->started_ms < kSendSlowMs ? " · sending" : " · still sending"),
                             color::kValue}};
                break;
            case CardPhase::kNotSent:
                v.footer = {{"Not sent · tap to retry", color::kFail}};
                break;
            case CardPhase::kOutcome:
                v.outcome = card_.outcome;
                v.outcome_color = card_.outcome_color;
                v.outcome_glyph = card_.outcome_glyph;
                break;
        }
        return v;
    }

    // Live transcript or heard text for the card footer.
    void CardHeard(const std::string& text, int64_t now_ms) {
        card_.heard = text;
        card_.heard_ms = now_ms;
        card_.touched_ms = now_ms;
    }

    struct Decision {
        bool send = false;
        bool refreshed = false;  // the task changed since the card opened: new content, nothing sent
        uint32_t token = 0;      // matches OnDecisionSent and ExpireSends to this send
        std::string kind;        // approve|deny|answer
        std::string task_id, chat_id, card_id, option_id;
    };

    // Tap on control index (approval: 0 approve, 1 deny; question: option index). A card holds
    // one decision: once queued or sent, other controls do nothing; after Not sent, a tap on the
    // same control resends that decision.
    Decision Tap(int index, int64_t now_ms) {
        Decision d;
        if (card_.kind == CardKind::kNone || card_.phase == CardPhase::kOutcome || CardTapGuarded(now_ms)) {
            return d;
        }
        card_.touched_ms = now_ms;
        const Task* t = Find(card_.task_id);
        if (t == nullptr) return d;
        if (!SameCard(*t)) {
            if (weixue_agent::IsOpenCard(t->item.state) && !t->item.card_id.empty() && !IsDecided(*t) &&
                FindDecision(t->item.card_id) == nullptr) {
                Snapshot(*t, now_ms);
            }
            d.refreshed = true;
            return d;
        }
        DecisionRecord* rec = FindDecision(card_.card_id);
        if (rec != nullptr) {
            if (rec->phase != SendPhase::kNotSent || index != rec->pressed) return d;
        } else {
            DecisionRecord fresh;
            if (card_.kind == CardKind::kApproval) {
                if (index != 0 && index != 1) return d;
                fresh.kind = index == 0 ? "approve" : "deny";
                for (const auto& o : card_.options) {
                    if (o.id == fresh.kind) fresh.option_id = o.id;
                }
            } else {
                if (index < 0 || index >= static_cast<int>(card_.options.size())) return d;
                fresh.kind = "answer";
                fresh.option_id = card_.options[index].id;
            }
            fresh.task_id = t->item.task_id;
            fresh.chat_id = t->item.chat_id;
            fresh.card_id = card_.card_id;
            fresh.pressed = index;
            decisions_.push_back(std::move(fresh));
            rec = &decisions_.back();
        }
        rec->phase = SendPhase::kQueued;
        rec->token = next_token_++;
        rec->started_ms = now_ms;
        d.send = true;
        d.token = rec->token;
        d.kind = rec->kind;
        d.task_id = rec->task_id;
        d.chat_id = rec->chat_id;
        d.card_id = rec->card_id;
        d.option_id = rec->option_id;
        return d;
    }

    // Whether the send of token is still waiting for the transport.
    bool SendPending(uint32_t token) const {
        for (const auto& r : decisions_) {
            if (r.token == token) return r.phase == SendPhase::kQueued;
        }
        return false;
    }

    // Transport outcome of the send (card_id, token) from Tap; a callback of an older send or of
    // another card changes nothing. Returns the earcon to play.
    Earcon OnDecisionSent(const std::string& card_id, uint32_t token, bool sent, int64_t now_ms) {
        DecisionRecord* rec = nullptr;
        for (auto& r : decisions_) {
            if (r.card_id == card_id && r.token == token && r.phase == SendPhase::kQueued) rec = &r;
        }
        if (rec == nullptr) return Earcon::kNone;
        if (!sent) {
            rec->phase = SendPhase::kNotSent;
            return Earcon::kNone;
        }
        rec->phase = SendPhase::kSent;
        const Task* t = Find(rec->task_id);
        if (t != nullptr && t->item.card_id == rec->card_id) {
            for (auto& i : infos_) {
                if (i.task_id == rec->task_id) {
                    i.decided = rec->kind;
                    i.decided_card = rec->card_id;
                }
            }
        }
        bool shown = card_.kind != CardKind::kNone && card_.card_id == rec->card_id;
        Earcon e = Earcon::kNone;
        if (rec->kind == "approve") {
            if (shown) SetOutcome("Approved", color::kApprove, Glyph::kDone, now_ms);
            e = Earcon::kApproved;
        } else if (rec->kind == "deny") {
            if (shown) SetOutcome("Denied", color::kDeny, Glyph::kFailed, now_ms);
            e = Earcon::kDenied;
        } else if (shown) {
            SetOutcome("Got it", color::kText, Glyph::kDone, now_ms);
        }
        return e;
    }

    // Sends still queued kSendHardTimeoutMs after the tap become Not sent; returns their tokens
    // (to drop from the outbox).
    std::vector<uint32_t> ExpireSends(int64_t now_ms) {
        std::vector<uint32_t> out;
        for (auto& r : decisions_) {
            if (r.phase == SendPhase::kQueued && now_ms - r.started_ms >= kSendHardTimeoutMs) {
                r.phase = SendPhase::kNotSent;
                out.push_back(r.token);
            }
        }
        return out;
    }

    // Right swipe or 60 s idle: the card closes, the item stays pending on the face. A decision
    // in flight keeps its record.
    void Later() {
        if (!card_.card_id.empty()) DropPending(card_.card_id);
        card_ = CardState();
    }

    struct CardTick {
        bool close = false;    // slide the card out now
        bool changed = false;  // content changed (re-render)
    };

    CardTick TickCard(int64_t now_ms) {
        CardTick r;
        if (card_.kind == CardKind::kNone) return r;
        if (card_.phase == CardPhase::kOutcome) {
            if (now_ms - card_.phase_ms >= kOutcomeMs) {
                card_ = CardState();
                r.close = true;
            }
            return r;
        }
        const DecisionRecord* rec = FindDecision(card_.card_id);
        if (rec != nullptr && rec->phase != SendPhase::kNotSent) return r;
        const Task* t = Find(card_.task_id);
        bool gone = t == nullptr || t->item.card_id != card_.card_id ||
                    !weixue_agent::IsOpenCard(t->item.state);
        if (gone) {
            SetOutcome("Answered", color::kLabel, Glyph::kNone, now_ms);
            r.changed = true;
        } else if (IsExpired(*t, now_ms)) {
            SetOutcome("Timed out", color::kFail, Glyph::kFailed, now_ms);
            r.changed = true;
        } else if (rec == nullptr && !SameCard(*t)) {
            Snapshot(*t, now_ms);
            r.changed = true;
        } else if (now_ms - card_.touched_ms >= kCardIdleLaterMs) {
            Later();
            r.close = true;
        }
        return r;
    }

private:
    struct PendingOpen {
        std::string card_id;
        Tier tier;
    };
    struct CardState {
        CardKind kind = CardKind::kNone;
        CardPhase phase = CardPhase::kOpen;  // kOpen or kOutcome; sends live in decisions_
        std::string task_id, card_id, outcome, heard;
        std::vector<Option> options;
        uint32_t outcome_color = color::kText;
        Glyph outcome_glyph = Glyph::kNone;
        int64_t opened_ms = 0, touched_ms = 0, phase_ms = 0, heard_ms = 0;
    };
    enum class SendPhase : uint8_t { kQueued, kSent, kNotSent };
    // One decision per card_id, kept until its card is no longer open on any task (and, while
    // queued, until the transport resolves it).
    struct DecisionRecord {
        std::string task_id, chat_id, card_id, kind, option_id;
        int pressed = -1;
        uint32_t token = 0;
        int64_t started_ms = 0;
        SendPhase phase = SendPhase::kQueued;
    };

    std::vector<Task> tasks_;
    std::vector<Info> infos_;
    std::vector<std::string> read_;
    std::vector<PendingOpen> pending_open_;
    std::vector<ChatInfo> chats_;
    std::vector<std::string> chats_before_new_;
    std::vector<DecisionRecord> decisions_;
    uint32_t next_token_ = 1;
    std::string current_chat_;
    bool new_chat_pending_ = false;
    Flash flash_;
    CardState card_;

    const Info* FindInfo(const std::string& task_id) const {
        for (const auto& i : infos_) {
            if (i.task_id == task_id) return &i;
        }
        return nullptr;
    }

    std::string CardOf(const std::string& task_id) const {
        const Task* t = Find(task_id);
        return t ? t->item.card_id : "";
    }

    DecisionRecord* FindDecision(const std::string& card_id) {
        for (auto& r : decisions_) {
            if (r.card_id == card_id) return &r;
        }
        return nullptr;
    }
    const DecisionRecord* FindDecision(const std::string& card_id) const {
        return const_cast<Model*>(this)->FindDecision(card_id);
    }

    void PruneDecisions() {
        decisions_.erase(std::remove_if(decisions_.begin(), decisions_.end(),
                                        [&](const DecisionRecord& r) {
                                            if (r.phase == SendPhase::kQueued) return false;
                                            for (const auto& t : tasks_) {
                                                if (t.item.card_id == r.card_id &&
                                                    weixue_agent::IsOpenCard(t.item.state)) {
                                                    return false;
                                                }
                                            }
                                            return true;
                                        }),
                         decisions_.end());
    }

    // Captures the card of t (id, kind, options) as the content the card shows and taps send.
    void Snapshot(const Task& t, int64_t now_ms) {
        DropPending(t.item.card_id);
        card_.kind = t.item.state == State::kNeedsApproval ? CardKind::kApproval : CardKind::kQuestion;
        card_.phase = CardPhase::kOpen;
        card_.task_id = t.item.task_id;
        card_.card_id = t.item.card_id;
        card_.options = t.item.options;
        card_.opened_ms = now_ms;
    }

    // Whether t still carries the card captured at open, with the same options.
    bool SameCard(const Task& t) const {
        if (!weixue_agent::IsOpenCard(t.item.state) || t.item.card_id != card_.card_id) return false;
        CardKind kind = t.item.state == State::kNeedsApproval ? CardKind::kApproval : CardKind::kQuestion;
        if (kind != card_.kind || t.item.options.size() != card_.options.size()) return false;
        for (size_t i = 0; i < card_.options.size(); i++) {
            if (t.item.options[i].id != card_.options[i].id || t.item.options[i].label != card_.options[i].label) {
                return false;
            }
        }
        return true;
    }

    // The task whose current, unexpired card is card_id with no decision on this device, or "".
    std::string CardTaskId(const std::string& card_id, int64_t now_ms) const {
        for (const auto& t : tasks_) {
            if (t.item.card_id == card_id && weixue_agent::IsOpenCard(t.item.state) && !IsDecided(t) &&
                !IsExpired(t, now_ms) && FindDecision(card_id) == nullptr) {
                return t.item.task_id;
            }
        }
        return "";
    }

    void DropPending(const std::string& card_id) {
        pending_open_.erase(std::remove_if(pending_open_.begin(), pending_open_.end(),
                                           [&](const PendingOpen& p) { return p.card_id == card_id; }),
                            pending_open_.end());
    }

    void SetOutcome(const char* text, uint32_t col, Glyph glyph, int64_t now_ms) {
        card_.phase = CardPhase::kOutcome;
        card_.phase_ms = now_ms;
        card_.outcome = text;
        card_.outcome_color = col;
        card_.outcome_glyph = glyph;
    }

    static std::string SendingWord(const std::string& decision) {
        if (decision == "approve") return "Approved";
        if (decision == "deny") return "Denied";
        return "Sent";
    }

    // Chat cache (at most kMaxChats): title of the chat's newest task, last update time.
    void RememberChats() {
        for (const auto& t : tasks_) {
            const Info* info = FindInfo(t.item.task_id);
            int64_t updated = info ? info->updated_ms : 0;
            ChatInfo* c = nullptr;
            for (auto& existing : chats_) {
                if (existing.chat_id == t.item.chat_id) c = &existing;
            }
            if (c == nullptr) {
                if (chats_.size() >= kMaxChats) {
                    chats_.erase(std::min_element(chats_.begin(), chats_.end(),
                                                  [](const ChatInfo& a, const ChatInfo& b) {
                                                      return a.last_ms < b.last_ms;
                                                  }));
                }
                chats_.push_back({t.item.chat_id, TitleOf(t), updated, t.item.seq});
                continue;
            }
            if (t.item.seq >= c->title_seq) {
                c->title = TitleOf(t);
                c->title_seq = t.item.seq;
            }
            c->last_ms = std::max(c->last_ms, updated);
        }
    }
};

}  // namespace agent_ui

#endif  // WEIXUE_AGENT_UI_H
