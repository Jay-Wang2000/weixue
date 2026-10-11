#ifndef WEIXUE_AUDIO_TUNE_H
#define WEIXUE_AUDIO_TUNE_H

#include <atomic>

// Runtime WakeNet threshold request (diag console), applied by the AFE processing task.
namespace weixue_audio_tune {

inline constexpr float kWakeNetThresholdMin = 0.4f;
inline constexpr float kWakeNetThresholdMax = 0.9999f;

// 0 = the model's own threshold.
inline std::atomic<float> wakenet_threshold{0.0f};
inline std::atomic<bool> wakenet_threshold_dirty{false};

inline bool RequestWakeNetThreshold(float threshold) {
    if (threshold != 0.0f &&
        (threshold < kWakeNetThresholdMin || threshold > kWakeNetThresholdMax)) {
        return false;
    }
    wakenet_threshold.store(threshold);
    wakenet_threshold_dirty.store(true);
    return true;
}

}  // namespace weixue_audio_tune

#endif  // WEIXUE_AUDIO_TUNE_H
