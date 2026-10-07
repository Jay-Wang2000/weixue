// Host test for BedsideSchedule and RingState: firmware/tests/run.sh
#include "bedside_schedule.h"
#include "voice_ring.h"

#include <cstdio>
#include <cstring>

namespace {

int failures = 0;

void Check(bool ok, const char* what) {
    if (!ok) {
        failures++;
        printf("FAIL %s\n", what);
    }
}

int Min(int h, int m) { return h * 60 + m; }

void TestParse() {
    Check(BedsideSchedule::ParseClock("22:00") == Min(22, 0), "parse 22:00");
    Check(BedsideSchedule::ParseClock("07:05") == Min(7, 5), "parse 07:05");
    Check(BedsideSchedule::ParseClock("0:00") == 0, "parse 0:00");
    Check(BedsideSchedule::ParseClock("24:00") == -1, "reject 24:00");
    Check(BedsideSchedule::ParseClock("7:60") == -1, "reject 7:60");
    Check(BedsideSchedule::ParseClock("") == -1, "reject empty");
    Check(BedsideSchedule::ParseClock("22:00x") == -1, "reject trailing text");
    Check(BedsideSchedule::ParseClock(nullptr) == -1, "reject null");
}

void TestWindow() {
    BedsideSchedule wrap(Min(22, 0), Min(7, 0));
    Check(!wrap.InWindow(Min(21, 59)), "21:59 is day");
    Check(wrap.InWindow(Min(22, 0)), "22:00 is night");
    Check(wrap.InWindow(Min(0, 0)), "00:00 is night");
    Check(wrap.InWindow(Min(6, 59)), "06:59 is night");
    Check(!wrap.InWindow(Min(7, 0)), "07:00 is day");
    Check(!wrap.InWindow(-1), "no clock is day");

    BedsideSchedule same_day(Min(1, 0), Min(5, 30));
    Check(same_day.InWindow(Min(3, 0)) && !same_day.InWindow(Min(5, 30)) &&
              !same_day.InWindow(Min(0, 59)),
          "window within one day");

    BedsideSchedule off(Min(7, 0), Min(7, 0));
    Check(!off.InWindow(Min(7, 0)) && !off.InWindow(Min(23, 0)), "equal start and end disables");
    BedsideSchedule bad(-1, Min(7, 0));
    Check(!bad.InWindow(Min(23, 0)), "malformed start disables");
}

void TestWindowText() {
    char text[24];
    BedsideSchedule(Min(22, 0), Min(7, 5)).WindowText(text, sizeof(text));
    Check(strcmp(text, "22:00-07:05") == 0, "window text 22:00-07:05");
    BedsideSchedule(Min(7, 0), Min(7, 0)).WindowText(text, sizeof(text));
    Check(strcmp(text, "off") == 0, "equal start and end is off");
    BedsideSchedule(-1, Min(7, 0)).WindowText(text, sizeof(text));
    Check(strcmp(text, "off") == 0, "malformed start is off");
    BedsideSchedule(Min(22, 0), -1).WindowText(text, sizeof(text));
    Check(strcmp(text, "off") == 0, "malformed end is off");
}

void TestHints() {
    BedsideSchedule s(Min(22, 0), Min(7, 0));
    int64_t t = 1000;
    // Night hint at 20:30 lasts until 07:00 (630 min), then the window applies again.
    s.SetHint(BedsideSchedule::Hint::kNight, Min(20, 30), t);
    Check(s.IsNight(Min(20, 30), t), "night hint at 20:30");
    Check(s.IsNight(Min(6, 59), t + 629 * 60), "night hint holds to 06:59");
    Check(!s.IsNight(Min(7, 0), t + 630 * 60), "night hint ends at 07:00");
    Check(s.hint() == BedsideSchedule::Hint::kAuto, "night hint cleared");

    // Day hint at 23:00 lasts until the next 22:00 (23 h).
    s.SetHint(BedsideSchedule::Hint::kDay, Min(23, 0), t);
    Check(!s.IsNight(Min(23, 0), t), "day hint at 23:00");
    Check(!s.IsNight(Min(21, 59), t + (23 * 60 - 1) * 60), "day hint holds to 21:59");
    Check(s.IsNight(Min(22, 0), t + 23 * 60 * 60), "day hint ends at 22:00");

    // A hint set exactly on its boundary lasts a full day.
    s.SetHint(BedsideSchedule::Hint::kNight, Min(7, 0), t);
    Check(s.IsNight(Min(12, 0), t + 5 * 3600), "night hint at 07:00 lasts past noon");

    // Auto clears a hint at once.
    s.SetHint(BedsideSchedule::Hint::kAuto, Min(12, 0), t);
    Check(!s.IsNight(Min(12, 0), t), "auto at noon is day");

    // Without a clock a night hint lasts until changed.
    BedsideSchedule n(Min(22, 0), Min(7, 0));
    n.SetHint(BedsideSchedule::Hint::kNight, -1, t);
    Check(n.IsNight(-1, t + 3 * 86400), "night hint without clock persists");
    n.SetHint(BedsideSchedule::Hint::kDay, -1, t);
    Check(!n.IsNight(-1, t), "day hint without clock");

    // With the window disabled, the night hint still applies and does not expire by time.
    BedsideSchedule off(-1, -1);
    off.SetHint(BedsideSchedule::Hint::kNight, Min(23, 0), t);
    Check(off.IsNight(Min(9, 0), t + 10 * 3600), "night hint with window disabled");
}

void TestRingTiles() {
    RingState off;
    for (int k = 0; k < RingState::kTiles; k++) {
        Check(off.TileView(k) == RingState(), "off ring has empty tiles");
    }

    RingState solid;
    solid.style = RingState::Style::kSolid;
    solid.opa = 128;
    solid.width = 9;
    solid.color = 0x00E0FF;
    for (int k = 0; k < RingState::kTiles; k++) {
        Check(solid.TileView(k) == solid, "solid ring covers every tile");
    }
    RingState zero = solid;
    zero.opa = 0;
    Check(zero.TileView(3) == RingState(), "transparent ring has empty tiles");

    // A spin with head at 90 covers [-30, 90): tiles 0-4 (0-90) and 18-19 (324-360).
    RingState spin;
    spin.style = RingState::Style::kSpin;
    spin.opa = 255;
    spin.width = 9;
    spin.head_deg = 90;
    int covered = 0;
    for (int k = 0; k < RingState::kTiles; k++) {
        bool on = spin.TileView(k) != RingState();
        int a0 = k * RingState::kTileDeg;
        bool expect = a0 < 90 || a0 + RingState::kTileDeg > 360 - (RingState::kSpinSpanDeg - 90);
        Check(on == expect, "spin tile coverage");
        covered += on;
    }
    Check(covered == 7, "spin covers 7 tiles");

    // Moving the head changes the view of covered tiles only.
    RingState moved = spin;
    moved.head_deg = 100;
    Check(moved.TileView(10) == spin.TileView(10), "uncovered tile unchanged");
    Check(moved.TileView(2) != spin.TileView(2), "covered tile changes");

    // Spin steps fade from the head.
    int start;
    uint8_t opa0, opa_last, mix0, mix_last;
    spin.SpinStep(0, start, opa0, mix0);
    Check(start == 80 && opa0 == 255 && mix0 == 0, "spin head step");
    spin.SpinStep(RingState::kSpinSteps - 1, start, opa_last, mix_last);
    Check(start == RingState::Wrap(90 - RingState::kSpinSpanDeg) && opa_last < 10 && mix_last == 255,
          "spin tail step");
}

void TestSectorBox() {
    // Tile 0 (0-18 deg) of a 232 px ring around (233, 233).
    RingState::Box b = RingState::SectorBox(233, 233, 232, 223, 0, 18, 0);
    Check(b.x1 >= 233 + 210 && b.x2 == 233 + 232 && b.y1 == 233 && b.y2 <= 233 + 72 && b.y2 >= 233 + 71,
          "tile 0 box");
    // A sector across the 90 deg axis reaches the bottom of the ring.
    RingState::Box c = RingState::SectorBox(233, 233, 232, 223, 81, 18, 2);
    Check(c.y2 == 233 + 232 + 2, "sector across 90 deg");
    // A sector across 0 deg (350-8) reaches the right edge.
    RingState::Box d = RingState::SectorBox(233, 233, 232, 223, 350, 18, 0);
    Check(d.x2 == 233 + 232 && d.y1 < 233 && d.y2 > 233, "sector across 0 deg");
}

}  // namespace

int main() {
    TestParse();
    TestWindow();
    TestWindowText();
    TestHints();
    TestRingTiles();
    TestSectorBox();
    if (failures != 0) {
        printf("ring_bedside_test: %d failure(s)\n", failures);
        return 1;
    }
    printf("ring_bedside_test: all passed\n");
    return 0;
}
