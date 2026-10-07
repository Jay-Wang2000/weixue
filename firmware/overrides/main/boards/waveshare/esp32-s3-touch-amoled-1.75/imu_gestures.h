#ifndef IMU_GESTURES_H
#define IMU_GESTURES_H

#include <algorithm>
#include <cmath>
#include <cstdint>

// Pick-up and face-down/face-up detection from accelerometer samples (g) in the screen frame,
// +z out of the screen. Update() expects one sample every kSamplePeriodMs.
class ImuGestureDetector {
public:
    enum Event : uint8_t {
        kNone = 0,
        kPickUp = 1 << 0,
        kFaceDown = 1 << 1,
        kFaceUp = 1 << 2,
    };

    static constexpr int kSamplePeriodMs = 20;
    // A 5-sample median per axis removes impulses up to 2 samples wide (taps, knocks).
    static constexpr int kMedianTaps = 5;
    // Rest: the filtered vector stays within kStillToleranceG of one value for kRestArmMs.
    static constexpr float kStillToleranceG = 0.04f;
    static constexpr int kRestArmMs = 1000;
    // Pick-up: after rest, the filtered vector leaves the rest vector by more than
    // kPickUpDeltaG for kPickUpSamples consecutive samples.
    static constexpr float kPickUpDeltaG = 0.12f;
    static constexpr int kPickUpSamples = 3;
    // Face-down: z below kFaceDownZ with |a| within kGravityToleranceG of 1 g for kFaceDownMs.
    // Face-up: z above kFaceUpZ for kFaceUpMs.
    static constexpr float kFaceDownZ = -0.8f;
    static constexpr float kFaceUpZ = -0.5f;
    static constexpr float kGravityToleranceG = 0.2f;
    static constexpr int kFaceDownMs = 800;
    static constexpr int kFaceUpMs = 300;

    uint8_t Update(float x, float y, float z) {
        hist_[0][pos_] = x;
        hist_[1][pos_] = y;
        hist_[2][pos_] = z;
        pos_ = (pos_ + 1) % kMedianTaps;
        if (filled_ < kMedianTaps) {
            filled_++;
            if (filled_ < kMedianTaps) {
                return kNone;
            }
        }
        float f[3];
        for (int axis = 0; axis < 3; axis++) {
            f[axis] = Median(hist_[axis]);
        }

        uint8_t events = kNone;

        if (Distance(f, anchor_) > kStillToleranceG) {
            Copy(f, anchor_);
            still_ms_ = 0;
        } else if (still_ms_ < kRestArmMs) {
            still_ms_ += kSamplePeriodMs;
        }
        if (still_ms_ >= kRestArmMs) {
            armed_ = true;
            Copy(anchor_, rest_);
            moving_samples_ = 0;
        } else if (armed_) {
            if (Distance(f, rest_) > kPickUpDeltaG) {
                if (++moving_samples_ >= kPickUpSamples) {
                    armed_ = false;
                    moving_samples_ = 0;
                    events |= kPickUp;
                }
            } else {
                moving_samples_ = 0;
            }
        }

        float magnitude = std::sqrt(f[0] * f[0] + f[1] * f[1] + f[2] * f[2]);
        if (!face_down_) {
            bool down = f[2] < kFaceDownZ && std::fabs(magnitude - 1.0f) < kGravityToleranceG;
            orientation_ms_ = down ? orientation_ms_ + kSamplePeriodMs : 0;
            if (orientation_ms_ >= kFaceDownMs) {
                face_down_ = true;
                orientation_ms_ = 0;
                events |= kFaceDown;
            }
        } else {
            orientation_ms_ = f[2] > kFaceUpZ ? orientation_ms_ + kSamplePeriodMs : 0;
            if (orientation_ms_ >= kFaceUpMs) {
                face_down_ = false;
                orientation_ms_ = 0;
                events |= kFaceUp;
            }
        }
        return events;
    }

    bool face_down() const { return face_down_; }
    bool armed() const { return armed_; }

private:
    static float Median(const float* v) {
        float s[kMedianTaps];
        std::copy(v, v + kMedianTaps, s);
        std::nth_element(s, s + kMedianTaps / 2, s + kMedianTaps);
        return s[kMedianTaps / 2];
    }
    static float Distance(const float* a, const float* b) {
        float dx = a[0] - b[0], dy = a[1] - b[1], dz = a[2] - b[2];
        return std::sqrt(dx * dx + dy * dy + dz * dz);
    }
    static void Copy(const float* from, float* to) { std::copy(from, from + 3, to); }

    float hist_[3][kMedianTaps] = {};
    int pos_ = 0;
    int filled_ = 0;
    float anchor_[3] = {};
    float rest_[3] = {};
    int still_ms_ = 0;
    bool armed_ = false;
    int moving_samples_ = 0;
    bool face_down_ = false;
    int orientation_ms_ = 0;
};

#endif  // IMU_GESTURES_H
