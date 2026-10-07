#ifndef IMU_GESTURES_H
#define IMU_GESTURES_H

#include <algorithm>
#include <cmath>
#include <cstdint>

// Pick-up and face-down/face-up detection from accelerometer samples (g) in the screen frame,
// +z out of the screen. Update() takes each sample with the time since the previous one
// (nominally kSamplePeriodMs).
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

    uint8_t Update(float x, float y, float z, int elapsed_ms = kSamplePeriodMs) {
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
            still_ms_ += elapsed_ms;
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
            orientation_ms_ = down ? orientation_ms_ + elapsed_ms : 0;
            if (orientation_ms_ >= kFaceDownMs) {
                face_down_ = true;
                orientation_ms_ = 0;
                events |= kFaceDown;
            }
        } else {
            orientation_ms_ = f[2] > kFaceUpZ ? orientation_ms_ + elapsed_ms : 0;
            if (orientation_ms_ >= kFaceUpMs) {
                face_down_ = false;
                orientation_ms_ = 0;
                events |= kFaceUp;
            }
        }
        return events;
    }

    // Drops the filter history and the rest, pick-up and orientation timing (after a sampling
    // gap); the face-down state is kept.
    void Reset() {
        pos_ = 0;
        filled_ = 0;
        still_ms_ = 0;
        armed_ = false;
        moving_samples_ = 0;
        orientation_ms_ = 0;
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

// Spacing of polled samples from the sensor's 24-bit sample counter (500 Hz, 2 ms per count).
class ImuSampleGate {
public:
    enum Result : uint8_t {
        kAccept,  // a new sample, *elapsed_ms after the last accepted one
        kResync,  // first sample, or after a gap: the series restarts here
        kSkip,    // the same or an early sample (catch-up read): not used
    };

    static constexpr uint32_t kMsPerCount = 2;
    // Fewer counts than this since the last accepted sample: not used, so the 5-sample median
    // spans at least 5 x 10 ms. More than kMaxCounts: a sampling gap.
    static constexpr uint32_t kMinCounts = 5;
    static constexpr uint32_t kMaxCounts = 50;

    Result Check(uint32_t timestamp, int* elapsed_ms) {
        uint32_t delta = (timestamp - last_) & 0xFFFFFF;
        *elapsed_ms = has_last_ ? static_cast<int>(delta * kMsPerCount) : 0;
        if (has_last_ && delta < kMinCounts) {
            return kSkip;
        }
        bool first = !has_last_;
        has_last_ = true;
        last_ = timestamp;
        return !first && delta <= kMaxCounts ? kAccept : kResync;
    }

    // After a failed read the next sample restarts the series.
    void Invalidate() { has_last_ = false; }

private:
    bool has_last_ = false;
    uint32_t last_ = 0;
};

// The sensor's TIMESTAMP registers are not latched: one read can straddle an increment and be
// off by 255 counts or more. A value is used once a later read agrees with it.
constexpr uint32_t kCounterAgreeCounts = 16;

inline bool CountersAgree(uint32_t earlier, uint32_t later) {
    return ((later - earlier) & 0xFFFFFF) <= kCounterAgreeCounts;
}

// The sample counter from the first read and up to two re-reads (read(&counter) returns false on a
// bus error). *reads is the total number of reads; false when no two reads agree.
template <typename ReadCounter>
bool AgreedCounter(uint32_t first, ReadCounter&& read, uint32_t* out, int* reads) {
    uint32_t second = 0;
    uint32_t third = 0;
    *reads = 2;
    if (!read(&second)) {
        return false;
    }
    if (CountersAgree(first, second)) {
        *out = first;
        return true;
    }
    *reads = 3;
    if (!read(&third)) {
        return false;
    }
    if (CountersAgree(second, third)) {
        *out = second;
        return true;
    }
    if (CountersAgree(first, third)) {
        *out = first;
        return true;
    }
    return false;
}

// One polled read through the gate into the detector, as the board's IMU task runs it.
struct ImuPollResult {
    enum Kind : uint8_t {
        kReadFailed,  // the gate is invalidated; the next sample restarts the series
        kSkipped,     // not used (ImuSampleGate::kSkip)
        kResynced,    // the detector was reset, then took the sample
        kAccepted,
    };
    Kind kind;
    uint8_t events;  // ImuGestureDetector::Event bits
    int elapsed_ms;  // from ImuSampleGate::Check
};

inline ImuPollResult ImuPoll(ImuGestureDetector& detector, ImuSampleGate& gate, bool read_ok,
                             uint32_t timestamp, float x, float y, float z) {
    if (!read_ok) {
        gate.Invalidate();
        return {ImuPollResult::kReadFailed, 0, 0};
    }
    int elapsed_ms = 0;
    ImuPollResult::Kind kind = ImuPollResult::kAccepted;
    switch (gate.Check(timestamp, &elapsed_ms)) {
        case ImuSampleGate::kSkip:
            return {ImuPollResult::kSkipped, 0, elapsed_ms};
        case ImuSampleGate::kResync:
            detector.Reset();
            kind = ImuPollResult::kResynced;
            break;
        case ImuSampleGate::kAccept:
            break;
    }
    return {kind, detector.Update(x, y, z, elapsed_ms), elapsed_ms};
}

#endif  // IMU_GESTURES_H
