#ifndef FACE_STATE_H
#define FACE_STATE_H

#include <cstddef>
#include <cstdint>
#include <string>

// Face and monitor panel decisions, free of LVGL and ESP-IDF types.

// Thinking: a user transcript arrived and its turn has neither spoken nor ended. A turn ends on
// its first assistant sentence, an error, idle, or an accepted `tts stop`.
class ThinkingTurn {
public:
    // tts_stops: Application::GetTtsStopCount() at the transcript.
    void OnTranscript(uint32_t tts_stops) {
        active_ = true;
        stops_ = tts_stops;
    }
    void End() { active_ = false; }
    bool Thinking(uint32_t tts_stops) const { return active_ && tts_stops == stops_; }

private:
    bool active_ = false;
    uint32_t stops_ = 0;
};

// Cuts s to at most max bytes without splitting a UTF-8 sequence.
inline void TruncateUtf8(std::string& s, size_t max) {
    if (s.size() <= max) {
        return;
    }
    size_t end = max;
    while (end > 0 && (static_cast<uint8_t>(s[end]) & 0xC0) == 0x80) {
        end--;
    }
    s.resize(end);
}

// The sentences of one reply, space-separated and capped at max_bytes.
class ReplyText {
public:
    explicit ReplyText(size_t max_bytes) : max_bytes_(max_bytes) {}

    // reply: Application::GetReplyCount() when the sentence arrived; a new value starts a new
    // reply. Returns false for an empty sentence.
    bool Add(const char* sentence, uint32_t reply) {
        if (sentence == nullptr || sentence[0] == '\0') {
            return false;
        }
        if (reply != reply_) {
            reply_ = reply;
            text_.clear();
        }
        if (!text_.empty()) {
            text_ += ' ';
        }
        text_ += sentence;
        TruncateUtf8(text_, max_bytes_);
        return true;
    }
    void Clear() { text_.clear(); }
    const std::string& text() const { return text_; }

private:
    size_t max_bytes_;
    uint32_t reply_ = 0;
    std::string text_;
};

enum class BackendRow : uint8_t { kSession, kPaused, kChecking, kOnline, kOffline };

// Panel backend row from the audio channel, mute and the last proactive poll (HTTP status, 0 =
// no response; age as Application::BackendStatus). A failed poll from before a pause shows
// checking until a poll finishes after the resume.
inline BackendRow BackendRowFor(bool channel_open, bool muted, int poll_status, int64_t poll_age_ms,
                                bool polled_since_resume, int64_t fresh_ms) {
    if (channel_open) {
        return BackendRow::kSession;
    }
    if (muted) {
        return BackendRow::kPaused;
    }
    if (poll_age_ms < 0) {
        return BackendRow::kChecking;
    }
    bool reachable = poll_status == 200 || poll_status == 204;
    if (reachable && poll_age_ms < fresh_ms) {
        return BackendRow::kOnline;
    }
    if (!reachable && !polled_since_resume) {
        return BackendRow::kChecking;
    }
    return BackendRow::kOffline;
}

#endif  // FACE_STATE_H
