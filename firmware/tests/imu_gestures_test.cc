// Host test for ImuGestureDetector: firmware/tests/run.sh
#include "imu_gestures.h"

#include <cmath>
#include <cstdio>
#include <random>
#include <vector>

namespace {

using D = ImuGestureDetector;
constexpr int kPeriod = D::kSamplePeriodMs;
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

    Counts counts;
    D detector;

private:
    std::vector<float> Vec(float tilt_deg) {
        float r = tilt_deg * static_cast<float>(M_PI) / 180.0f;
        return {std::sin(r), 0.0f, std::cos(r)};
    }

    void Feed(std::vector<float> v, float noise_g) {
        std::normal_distribution<float> n(0.0f, noise_g > 0 ? noise_g : 1e-9f);
        uint8_t e = detector.Update(v[0] + n(rng_), v[1] + n(rng_), v[2] + n(rng_));
        counts.pick_up += (e & D::kPickUp) ? 1 : 0;
        counts.face_down += (e & D::kFaceDown) ? 1 : 0;
        counts.face_up += (e & D::kFaceUp) ? 1 : 0;
    }

    std::mt19937 rng_;
    float tilt_ = 0.0f;
};

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
