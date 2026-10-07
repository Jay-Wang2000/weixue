// Host test for weixue_clock.h: firmware/tests/run.sh
#include "weixue_clock.h"

#include <cstdio>
#include <cstdlib>
#include <ctime>

namespace {

int failures = 0;

void Expect(const char* name, long long got, long long want) {
    bool ok = got == want;
    std::printf("%s %s: %lld (want %lld)\n", ok ? "PASS" : "FAIL", name, got, want);
    if (!ok) {
        failures++;
    }
}

}  // namespace

int main() {
    using namespace weixue_clock;
    Expect("days to 2000-03-01", DaysFromCivil(2000, 3, 1), 11017);
    Expect("days to 1970-01-01", DaysFromCivil(1970, 1, 1), 0);

    time_t t = 0;
    Expect("parse Date", ParseHttpDate("Wed, 07 Oct 2026 21:30:05 GMT", &t), 1);
    Expect("parsed seconds", t, 1791408605LL);
    Expect("reject month", ParseHttpDate("Wed, 07 Foo 2026 21:30:05 GMT", &t), 0);
    Expect("reject zone", ParseHttpDate("Wed, 07 Oct 2026 21:30:05 PST", &t), 0);
    Expect("reject empty", ParseHttpDate("", &t), 0);
    Expect("reject trailing text", ParseHttpDate("Wed, 07 Oct 2026 21:30:05 GMTx", &t), 0);
    Expect("reject trailing space", ParseHttpDate("Wed, 07 Oct 2026 21:30:05 GMT ", &t), 0);
    Expect("reject 30 February", ParseHttpDate("Mon, 30 Feb 2026 10:00:00 GMT", &t), 0);
    Expect("reject 29 February 2027", ParseHttpDate("Mon, 29 Feb 2027 10:00:00 GMT", &t), 0);
    Expect("accept 29 February 2028", ParseHttpDate("Tue, 29 Feb 2028 10:00:00 GMT", &t), 1);
    Expect("reject 31 April", ParseHttpDate("Fri, 31 Apr 2026 10:00:00 GMT", &t), 0);
    Expect("reject negative hour", ParseHttpDate("Wed, 07 Oct 2026 -1:30:05 GMT", &t), 0);
    Expect("reject hour 24", ParseHttpDate("Wed, 07 Oct 2026 24:00:00 GMT", &t), 0);

    const char* date = "Wed, 07 Oct 2026 21:30:05 GMT";
    const time_t server = 1791408605;
    Expect("resync: no TZ", ResyncFromDate(false, date, server + 3600).apply, 0);
    Expect("resync: bad Date", ResyncFromDate(true, "garbage", server + 3600).apply, 0);
    Expect("resync: null Date", ResyncFromDate(true, nullptr, server + 3600).apply, 0);
    Expect("resync: 1 s fast", ResyncFromDate(true, date, server + 1).apply, 0);
    Expect("resync: 1 s slow", ResyncFromDate(true, date, server - 1).apply, 0);
    Expect("resync: in sync", ResyncFromDate(true, date, server).apply, 0);
    Resync fast = ResyncFromDate(true, date, server + kResyncMinS);
    Expect("resync: 2 s fast", fast.apply, 1);
    Expect("resync: 2 s fast drift", fast.drift_s, 2);
    Expect("resync: 2 s fast target", fast.server, server);
    Resync slow = ResyncFromDate(true, date, server - 3600);
    Expect("resync: 1 h slow", slow.apply, 1);
    Expect("resync: 1 h slow drift", slow.drift_s, -3600);

    setenv("TZ", "PST8PDT,M3.2.0,M11.1.0", 1);
    tzset();
    Expect("offset January (PST)", UtcOffsetMinutes(1768478400), -480);
    Expect("offset July (PDT)", UtcOffsetMinutes(1784116800), -420);
    // 2026-11-01 09:00 UTC is 01:00 PST, one hour after the fall-back transition.
    Expect("offset after fall-back", UtcOffsetMinutes(1793523600), -480);
    Expect("offset before fall-back", UtcOffsetMinutes(1793516400), -420);

    // Europe/Dublin: IST (+1) is standard time, GMT the negative-DST winter time.
    setenv("TZ", "IST-1GMT0,M10.5.0,M3.5.0/1", 1);
    tzset();
    Expect("Dublin January (GMT)", UtcOffsetMinutes(1768478400), 0);
    Expect("Dublin July (IST)", UtcOffsetMinutes(1784116800), 60);
    // 2026-10-25 01:00 UTC is 02:00 IST, the switch to GMT.
    Expect("Dublin before the October switch", UtcOffsetMinutes(1792889999), 60);
    Expect("Dublin after the October switch", UtcOffsetMinutes(1792890000), 0);

    std::printf("%s (%d failures)\n", failures == 0 ? "OK" : "FAILED", failures);
    return failures == 0 ? 0 : 1;
}
