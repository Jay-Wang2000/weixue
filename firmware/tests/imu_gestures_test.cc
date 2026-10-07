// Host test for imu_gestures.h: firmware/tests/run.sh
#include "imu_gestures.h"

#include <cmath>
#include <cstdio>
#include <random>
#include <vector>

namespace {

using D = ImuGestureDetector;
using G = ImuSampleGate;
constexpr int kPeriod = D::kSamplePeriodMs;
// QMI8658 sample counter at 500 Hz: 2 ms per count.
constexpr int kSampleMs = 2;
int failures = 0;

struct Counts {
    int pick_up = 0;
    int face_down = 0;
    int face_up = 0;
};

class Sim {
public:
    explicit Sim(unsigned seed = 1) : rng_(seed) {}

    // Holds the orientation given as tilt from face-up (degrees) for ms, with Gaussian noise.
    void Hold(float tilt_deg, int ms, float noise_g = 0.005f) {
        for (int t = 0; t < ms; t += kPeriod) {
            Feed(Vec(tilt_deg), noise_g);
        }
        tilt_ = tilt_deg;
    }

    // Rotates from the current tilt to tilt_deg over ms.
    void Rotate(float tilt_deg, int ms, float noise_g = 0.005f) {
        int steps = ms / kPeriod;
        for (int i = 1; i <= steps; i++) {
            Feed(Vec(tilt_ + (tilt_deg - tilt_) * i / steps), noise_g);
        }
        tilt_ = tilt_deg;
    }

    // Straight vertical lift: extra +z acceleration for ms, then deceleration.
    void Lift(float accel_g, int ms) {
        auto base = Vec(tilt_);
        for (int t = 0; t < ms; t += kPeriod) {
            Feed({base[0], base[1], base[2] + accel_g}, 0.005f);
        }
        for (int t = 0; t < ms; t += kPeriod) {
            Feed({base[0], base[1], base[2] - accel_g}, 0.005f);
        }
    }

    // An impulse of width samples (a tap or a knock on the table).
    void Impulse(float g, int samples) {
        auto base = Vec(tilt_);
        for (int i = 0; i < samples; i++) {
            Feed({base[0] + g * 0.3f, base[1], base[2] + g}, 0.0f);
        }
    }

    // Gated mode: every sample carries the sensor sample counter and goes through ImuPoll, as in
    // the board's IMU task.
    void Gate() { gated_ = true; }

    // A stalled bus: no sample is read for ms.
    void Stall(int ms) { ts_ += ms / kSampleMs; }

    // n catch-up reads right after the last one: the sensor counter advances by at most one.
    void CatchUp(int n, float extra_z_g = 0.0f) {
        auto v = Vec(tilt_);
        for (int i = 0; i < n; i++) {
            ts_ += i % 2;
            Deliver({v[0], v[1], v[2] + extra_z_g}, 0.0f);
        }
    }

    // Polls late by one 10 ms tick, then on schedule again (10 ms later), as under CPU load.
    void Jitter(bool on) { jitter_ = on; }

    // A failed read at the next poll.
    void ReadFailure() {
        ts_ += kPeriod / kSampleMs;
        ImuPollResult r = ImuPoll(detector, gate, false, ts_ & 0xFFFFFF, 0.0f, 0.0f, 0.0f);
        read_failures += r.kind == ImuPollResult::kReadFailed ? 1 : 0;
    }

    Counts counts;
    D detector;
    G gate;
    int skipped = 0;
    int resyncs = 0;
    int read_failures = 0;

private:
    std::vector<float> Vec(float tilt_deg) {
        float r = tilt_deg * static_cast<float>(M_PI) / 180.0f;
        return {std::sin(r), 0.0f, std::cos(r)};
    }

    void Feed(std::vector<float> v, float noise_g) {
        if (jitter_) {
            ts_ += (jitter_phase_++ % 2 == 0 ? 15 : 5);
        } else {
            ts_ += kPeriod / kSampleMs;
        }
        Deliver(v, noise_g);
    }

    void Deliver(std::vector<float> v, float noise_g) {
        std::normal_distribution<float> n(0.0f, noise_g > 0 ? noise_g : 1e-9f);
        float x = v[0] + n(rng_), y = v[1] + n(rng_), z = v[2] + n(rng_);
        uint8_t e = 0;
        if (gated_) {
            ImuPollResult r = ImuPoll(detector, gate, true, ts_ & 0xFFFFFF, x, y, z);
            skipped += r.kind == ImuPollResult::kSkipped ? 1 : 0;
            resyncs += r.kind == ImuPollResult::kResynced ? 1 : 0;
            e = r.events;
        } else {
            e = detector.Update(x, y, z);
        }
        counts.pick_up += (e & D::kPickUp) ? 1 : 0;
        counts.face_down += (e & D::kFaceDown) ? 1 : 0;
        counts.face_up += (e & D::kFaceUp) ? 1 : 0;
    }

    std::mt19937 rng_;
    float tilt_ = 0.0f;
    bool gated_ = false;
    bool jitter_ = false;
    unsigned jitter_phase_ = 0;
    uint32_t ts_ = 0xFFFF00;  // wraps past 0xFFFFFF in the first second
};

void ExpectInt(const char* name, int got, int want) {
    bool ok = got == want;
    std::printf("%s %s: %d (want %d)\n", ok ? "PASS" : "FAIL", name, got, want);
    if (!ok) {
        failures++;
    }
}

void TestGate() {
    G g;
    int elapsed = 0;
    struct Step {
        uint32_t ts;
        G::Result want;
        int elapsed;
    };
    const Step steps[] = {
        {0xFFFFF8, G::kResync, 0},   // first sample
        {0x000002, G::kAccept, 20},  // counter wrap
        {0x000011, G::kAccept, 30},  // one tick late
        {0x000011, G::kSkip, 0},     // same sample read again
        {0x000013, G::kSkip, 4},     // catch-up read
        {0x000016, G::kAccept, 10},  // measured from the last accepted sample
        {0x000048, G::kAccept, 100}, // 50 counts: still the same series
        {0x00007B, G::kResync, 102}, // 51 counts: a gap
        {0x000085, G::kAccept, 20},
    };
    bool ok = true;
    for (const Step& s : steps) {
        G::Result r = g.Check(s.ts, &elapsed);
        if (r != s.want || elapsed != s.elapsed) {
            std::printf("FAIL gate ts=0x%06X: result %d elapsed %d (want %d elapsed %d)\n", s.ts, r,
                        elapsed, s.want, s.elapsed);
            ok = false;
        }
    }
    g.Invalidate();
    if (g.Check(0x00008F, &elapsed) != G::kResync || elapsed != 0) {
        std::printf("FAIL gate: sample after a failed read must restart the series\n");
        ok = false;
    }
    std::printf("%s sample gate sequence\n", ok ? "PASS" : "FAIL");
    if (!ok) {
        failures++;
    }
}

void TestPoll() {
    using R = ImuPollResult;
    D d;
    G g;
    bool ok = true;
    auto check = [&ok](const char* what, const R& r, R::Kind kind, int elapsed) {
        if (r.kind != kind || r.elapsed_ms != elapsed) {
            std::printf("FAIL poll %s: kind %d elapsed %d (want %d elapsed %d)\n", what, r.kind,
                        r.elapsed_ms, kind, elapsed);
            ok = false;
        }
    };
    uint32_t ts = 1000;
    check("first sample", ImuPoll(d, g, true, ts, 0, 0, 1), R::kResynced, 0);
    for (int i = 0; i < 200; i++) {
        ts += kPeriod / kSampleMs;
        check("on schedule", ImuPoll(d, g, true, ts, 0, 0, 1), R::kAccepted, kPeriod);
    }
    if (!d.armed()) {
        std::printf("FAIL poll: detector not armed after 4 s at rest\n");
        ok = false;
    }
    check("catch-up read", ImuPoll(d, g, true, ts + 1, 0, 0, 1), R::kSkipped, kSampleMs);
    check("read failure", ImuPoll(d, g, false, 0, 0, 0, 0), R::kReadFailed, 0);
    if (!d.armed()) {
        std::printf("FAIL poll: a read failure alone must not reset the detector\n");
        ok = false;
    }
    ts += kPeriod / kSampleMs;
    check("after a read failure", ImuPoll(d, g, true, ts, 0, 0, 1), R::kResynced, 0);
    if (d.armed()) {
        std::printf("FAIL poll: a resync must reset the detector before the sample\n");
        ok = false;
    }
    for (int i = 0; i < 200; i++) {
        ts += kPeriod / kSampleMs;
        ImuPoll(d, g, true, ts, 0, 0, 1);
    }
    ts += G::kMaxCounts + 1;
    check("sampling gap", ImuPoll(d, g, true, ts, 0, 0, 1), R::kResynced,
          static_cast<int>((G::kMaxCounts + 1) * kSampleMs));
    if (d.armed()) {
        std::printf("FAIL poll: a sampling gap must reset the detector\n");
        ok = false;
    }
    std::printf("%s poll step sequence\n", ok ? "PASS" : "FAIL");
    if (!ok) {
        failures++;
    }
}

void TestAgreedCounter() {
    struct Case {
        const char* name;
        uint32_t first;
        std::vector<uint32_t> rereads;  // returned in order; a read past the end fails
        bool ok;
        uint32_t want;
        int reads;
    };
    const Case cases[] = {
        {"first read confirmed", 0x000200, {0x000201}, true, 0x000200, 2},
        {"counter wrap", 0xFFFFFF, {0x000001}, true, 0xFFFFFF, 2},
        // L read before an increment, M after: 0x0001FF -> 0x0002FF.
        {"torn first read", 0x0002FF, {0x000201, 0x000202}, true, 0x000201, 3},
        {"torn re-read", 0x000200, {0x0002FF, 0x000201}, true, 0x000200, 3},
        {"torn across M/H", 0x01FFFF + 0x10000, {0x020001, 0x020002}, true, 0x020001, 3},
        {"no two reads agree", 0x000100, {0x000300, 0x000500}, false, 0, 3},
        {"bus error on the re-read", 0x000100, {}, false, 0, 2},
    };
    bool ok = true;
    for (const Case& c : cases) {
        size_t next = 0;
        auto read = [&c, &next](uint32_t* counter) {
            if (next >= c.rereads.size()) {
                return false;
            }
            *counter = c.rereads[next++];
            return true;
        };
        uint32_t out = 0;
        int reads = 0;
        bool agreed = AgreedCounter(c.first, read, &out, &reads);
        if (agreed != c.ok || (agreed && out != c.want) || reads != c.reads) {
            std::printf("FAIL counter %s: agreed %d value 0x%06X reads %d\n", c.name, agreed, out,
                        reads);
            ok = false;
        }
    }
    std::printf("%s timestamp counter re-reads\n", ok ? "PASS" : "FAIL");
    if (!ok) {
        failures++;
    }
}

void Expect(const char* name, const Counts& got, const Counts& want) {
    bool ok = got.pick_up == want.pick_up && got.face_down == want.face_down &&
              got.face_up == want.face_up;
    std::printf("%s %s: pick_up=%d face_down=%d face_up=%d (want %d/%d/%d)\n", ok ? "PASS" : "FAIL",
                name, got.pick_up, got.face_down, got.face_up, want.pick_up, want.face_down,
                want.face_up);
    if (!ok) {
        failures++;
    }
}

}  // namespace

int main() {
    TestGate();
    TestPoll();
    TestAgreedCounter();
    {
        Sim s;
        s.Gate();
        s.Hold(0, 5 * 60 * 1000);
        Expect("gated idle face-up 5 min", s.counts, {0, 0, 0});
        ExpectInt("gated idle resyncs (first sample only)", s.resyncs, 1);
        ExpectInt("gated idle skipped", s.skipped, 0);
    }
    {
        // A knock read three times by catch-up reads after a stall passes a 5-sample median.
        Sim s;
        s.Gate();
        s.Hold(0, 3000);
        s.Stall(200);
        s.CatchUp(3, 1.5f);
        s.Hold(0, 2000);
        Expect("knock repeated by catch-up reads after a stall", s.counts, {0, 0, 0});
        ExpectInt("catch-up reads skipped", s.skipped, 2);
    }
    {
        Sim s;
        s.Hold(0, 3000);
        s.Stall(200);
        s.CatchUp(3, 1.5f);
        s.Hold(0, 2000);
        Expect("same knock without the gate (control)", s.counts, {1, 0, 0});
    }
    {
        // Face-down needs 800 ms of continuous sampling; a stall restarts the count.
        Sim s;
        s.Gate();
        s.Hold(0, 2000);
        s.Rotate(180, 600);
        s.Hold(180, 500);
        s.Stall(300);
        s.CatchUp(4);
        s.Hold(180, 600);
        Counts before = s.counts;
        s.Hold(180, 400);
        Expect("face-down not within 800 ms after a stall", before, {1, 0, 0});
        Expect("face-down 800 ms after the stall", s.counts, {1, 1, 0});
    }
    {
        // Tick jitter (30 ms / 10 ms polls) keeps one series and the measured timing.
        Sim s;
        s.Gate();
        s.Jitter(true);
        s.Hold(0, 60 * 1000);
        Expect("gated jitter idle 1 min", s.counts, {0, 0, 0});
        ExpectInt("gated jitter resyncs (first sample only)", s.resyncs, 1);
        s.Rotate(30, 300);
        s.Hold(30, 1000, 0.03f);
        Expect("gated jitter pick-up", s.counts, {1, 0, 0});
        s.Rotate(180, 600);
        s.Hold(180, 600);
        Counts early = s.counts;
        s.Hold(180, 600);
        Expect("gated jitter face-down not before 800 ms", early, {1, 0, 0});
        Expect("gated jitter face-down after 800 ms", s.counts, {1, 1, 0});
    }
    {
        Sim s;
        s.Gate();
        s.Hold(0, 3000);
        s.ReadFailure();
        s.Hold(0, 1200);
        Expect("read failure is not a pick-up", s.counts, {0, 0, 0});
        ExpectInt("read failures", s.read_failures, 1);
        ExpectInt("read failure resyncs", s.resyncs, 2);
        s.Rotate(30, 300);
        s.Hold(30, 1000, 0.03f);
        Expect("pick-up after the rest is re-established", s.counts, {1, 0, 0});
    }
    {
        Sim s;
        s.Hold(0, 5 * 60 * 1000);
        Expect("idle face-up 5 min", s.counts, {0, 0, 0});
    }
    {
        Sim s;
        s.Hold(0, 3000);
        s.Rotate(35, 400);
        s.Hold(35, 2000, 0.03f);
        Expect("pick-up with tilt, then held", s.counts, {1, 0, 0});
    }
    {
        Sim s;
        s.Hold(0, 3000);
        s.Lift(0.3f, 200);
        s.Hold(0, 500, 0.03f);
        Expect("straight lift", s.counts, {1, 0, 0});
    }
    {
        Sim s;
        s.Hold(0, 3000);
        s.Impulse(1.5f, 2);
        s.Hold(0, 200);
        s.Impulse(1.5f, 2);
        s.Hold(0, 2000);
        Expect("double tap on table", s.counts, {0, 0, 0});
    }
    {
        Sim s;
        s.Hold(0, 3000);
        for (int i = 0; i < 10; i++) {
            s.Rotate(30, 300);
            s.Hold(30, 1000, 0.03f);
            s.Rotate(0, 300);
            s.Hold(0, 2000);
        }
        Expect("10 pick-ups, put down between", s.counts, {10, 0, 0});
    }
    {
        Sim s;
        s.Hold(0, 2000);
        s.Rotate(180, 600);
        s.Hold(180, 600);
        Counts early = s.counts;
        s.Hold(180, 600);
        Expect("face-down not before 800 ms", early, {1, 0, 0});
        Expect("face-down after 800 ms", s.counts, {1, 1, 0});
        s.Rotate(0, 600);
        s.Hold(0, 1000);
        // Lifting the device after it rested face-down is a pick-up as well.
        Expect("face-up", s.counts, {2, 1, 1});
    }
    {
        Sim s;
        s.Hold(0, 2000);
        for (int i = 0; i < 10; i++) {
            s.Rotate(180, 600);
            s.Hold(180, 2000);
            s.Rotate(0, 600);
            s.Hold(0, 2000);
        }
        Expect("10 face-down/face-up cycles", s.counts, {20, 10, 10});
    }
    {
        Sim s;
        s.Hold(0, 2000);
        s.Rotate(135, 500);
        s.Hold(135, 3000);
        Expect("tilted 135 deg is not face-down", s.counts, {1, 0, 0});
    }
    std::printf("%s (%d failures)\n", failures == 0 ? "OK" : "FAILED", failures);
    return failures == 0 ? 0 : 1;
}
