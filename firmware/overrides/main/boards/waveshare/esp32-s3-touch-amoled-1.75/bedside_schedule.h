#ifndef BEDSIDE_SCHEDULE_H
#define BEDSIDE_SCHEDULE_H

#include <cstddef>
#include <cstdint>
#include <cstdio>

// Night window and server hint for the bedside page. Times are local minutes of the day
// (0-1439); -1 means the clock is not set.
class BedsideSchedule {
public:
    enum class Hint : uint8_t { kAuto, kNight, kDay };

    static constexpr int kMinutesPerDay = 24 * 60;

    // Parses "HH:MM"; returns -1 when malformed.
    static int ParseClock(const char* text) {
        int h = -1, m = -1;
        char tail = 0;
        if (text == nullptr || sscanf(text, "%d:%d%c", &h, &m, &tail) != 2 || h < 0 || h > 23 ||
            m < 0 || m > 59) {
            return -1;
        }
        return h * 60 + m;
    }

    // start == end, or either negative, disables the window.
    BedsideSchedule(int start_minute, int end_minute) : start_(start_minute), end_(end_minute) {}

    // Changing the window clears the hint.
    void SetWindow(int start_minute, int end_minute) {
        start_ = start_minute;
        end_ = end_minute;
        hint_ = Hint::kAuto;
        hint_expire_s_ = -1;
    }

    int start() const { return start_; }
    int end() const { return end_; }
    Hint hint() const { return hint_; }

    bool WindowEnabled() const { return start_ >= 0 && end_ >= 0 && start_ != end_; }

    // "HH:MM-HH:MM", or "off" when the window is disabled.
    void WindowText(char* out, size_t size) const {
        if (!WindowEnabled()) {
            snprintf(out, size, "off");
            return;
        }
        unsigned s = start_, e = end_;
        snprintf(out, size, "%02u:%02u-%02u:%02u", s / 60 % 24, s % 60, e / 60 % 24, e % 60);
    }

    bool InWindow(int minute) const {
        if (minute < 0 || !WindowEnabled()) {
            return false;
        }
        return start_ < end_ ? minute >= start_ && minute < end_ : minute >= start_ || minute < end_;
    }

    // A night hint lasts until the next end boundary, a day hint until the next start boundary;
    // without a clock either lasts until the next SetHint.
    void SetHint(Hint hint, int minute, int64_t now_s) {
        hint_ = hint;
        hint_expire_s_ = -1;
        if (hint == Hint::kAuto || minute < 0 || !WindowEnabled()) {
            return;
        }
        int boundary = hint == Hint::kNight ? end_ : start_;
        int ahead = (boundary - minute + kMinutesPerDay) % kMinutesPerDay;
        if (ahead == 0) {
            ahead = kMinutesPerDay;
        }
        hint_expire_s_ = now_s + int64_t(ahead) * 60;
    }

    bool IsNight(int minute, int64_t now_s) {
        if (hint_ != Hint::kAuto && hint_expire_s_ >= 0 && now_s >= hint_expire_s_) {
            hint_ = Hint::kAuto;
            hint_expire_s_ = -1;
        }
        switch (hint_) {
            case Hint::kNight: return true;
            case Hint::kDay: return false;
            case Hint::kAuto: break;
        }
        return InWindow(minute);
    }

private:
    int start_;
    int end_;
    Hint hint_ = Hint::kAuto;
    int64_t hint_expire_s_ = -1;
};

#endif  // BEDSIDE_SCHEDULE_H
