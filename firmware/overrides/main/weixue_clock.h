#ifndef WEIXUE_CLOCK_H
#define WEIXUE_CLOCK_H

#include <cstdio>
#include <cstring>
#include <ctime>

// Clock helpers for the UTC system clock: the OTA response's POSIX TZ gives local time, and the
// backend's HTTP Date header resyncs the clock.
namespace weixue_clock {

// Days since 1970-01-01 of a proleptic Gregorian date (month 1-12).
inline long DaysFromCivil(int y, int m, int d) {
    y -= m <= 2;
    const long era = (y >= 0 ? y : y - 399) / 400;
    const long yoe = y - era * 400;
    const long doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
    const long doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return era * 146097 + doe - 719468;
}

inline int DaysInMonth(int y, int m) {
    static const int kDays[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    bool leap = (y % 4 == 0 && y % 100 != 0) || y % 400 == 0;
    return m == 2 && leap ? 29 : kDays[m - 1];
}

// Parses an IMF-fixdate ("Wed, 07 Oct 2026 21:30:00 GMT", nothing after GMT) to seconds since
// the epoch.
inline bool ParseHttpDate(const char* text, time_t* out) {
    static const char kMonths[] = "JanFebMarAprMayJunJulAugSepOctNovDec";
    char month[4] = {};
    int day = 0, year = 0, hour = 0, minute = 0, second = 0, end = 0;
    if (text == nullptr ||
        sscanf(text, "%*3s, %2d %3s %4d %2d:%2d:%2d GMT%n", &day, month, &year, &hour, &minute,
               &second, &end) != 6 ||
        end == 0 || text[end] != '\0') {
        return false;
    }
    const char* found = strstr(kMonths, month);
    if (strlen(month) != 3 || found == nullptr || (found - kMonths) % 3 != 0 || year < 1970 ||
        hour < 0 || hour > 23 || minute < 0 || minute > 59 || second < 0 || second > 60) {
        return false;
    }
    const int mon = static_cast<int>(found - kMonths) / 3 + 1;
    if (day < 1 || day > DaysInMonth(year, mon)) {
        return false;
    }
    *out = static_cast<time_t>(DaysFromCivil(year, mon, day)) * 86400 + hour * 3600 +
           minute * 60 + second;
    return true;
}

// The UTC clock is set from a backend Date header when it is off by at least this much.
constexpr long long kResyncMinS = 2;

struct Resync {
    bool apply;
    time_t server;       // the Date header, valid when apply
    long long drift_s;   // now minus the Date header
};

// Whether a backend Date header resets the UTC clock. Only with a POSIX TZ (tz_set): without
// one the clock holds local time.
inline Resync ResyncFromDate(bool tz_set, const char* date, time_t now) {
    Resync r = {false, 0, 0};
    if (!tz_set || !ParseHttpDate(date, &r.server)) {
        return r;
    }
    r.drift_s = static_cast<long long>(now) - static_cast<long long>(r.server);
    r.apply = r.drift_s >= kResyncMinS || r.drift_s <= -kResyncMinS;
    return r;
}

// Local time minus UTC at t, in minutes, under the current TZ.
inline int UtcOffsetMinutes(time_t t) {
    struct tm local;
    localtime_r(&t, &local);
    const long long as_utc =
        static_cast<long long>(DaysFromCivil(local.tm_year + 1900, local.tm_mon + 1, local.tm_mday)) *
            86400 +
        local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec;
    return static_cast<int>((as_utc - static_cast<long long>(t)) / 60);
}

}  // namespace weixue_clock

#endif  // WEIXUE_CLOCK_H
