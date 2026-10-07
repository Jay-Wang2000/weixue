#ifndef WEIXUE_SESSION_H
#define WEIXUE_SESSION_H

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <utility>

// Audio-session decisions of the application, free of ESP-IDF types.
namespace weixue_session {

// Mute invariant: no microphone audio is sent, no reply audio is decoded, listening does not
// start and `tts start` is refused while muted; tts callbacks scheduled before a mute are dropped.
class MuteFence {
public:
    // Captured when a tts callback is scheduled.
    uint32_t generation() const { return generation_.load(); }
    // Invalidates every generation captured before it.
    void OnMute() { generation_.fetch_add(1); }

    bool AcceptTtsStart(bool muted, uint32_t captured) const {
        return !muted && captured == generation();
    }
    bool AcceptTtsStop(uint32_t captured) const { return captured == generation(); }
    static bool DecodeIncomingAudio(bool speaking, bool muted) { return speaking && !muted; }
    static bool SendMicAudio(bool muted) { return !muted; }
    static bool StartListening(bool muted) { return !muted; }

private:
    std::atomic<uint32_t> generation_{0};
};

struct MuteActions {
    bool abort_speaking;
    bool close_transport;
    bool to_idle;
};

// What a mute does to the audio channel. The transport is closed whenever a protocol exists:
// a channel past its receive timeout reports not opened but keeps its transport.
inline MuteActions ActionsOnMute(bool has_protocol, bool channel_opened, bool speaking,
                                 bool in_session) {
    return {has_protocol && channel_opened && speaking, has_protocol, in_session};
}

// Packets captured after the wake word while the audio channel opens: at most N, oldest dropped,
// drained in capture order.
template <typename Packet, size_t N>
class WakeAudioHold {
public:
    void Push(Packet packet) {
        if (packets_.size() >= N) {
            packets_.pop_front();
        }
        packets_.push_back(std::move(packet));
    }

    template <typename Send>
    size_t Drain(Send&& send) {
        size_t count = packets_.size();
        while (!packets_.empty()) {
            send(std::move(packets_.front()));
            packets_.pop_front();
        }
        return count;
    }

    void Clear() { packets_.clear(); }
    size_t size() const { return packets_.size(); }

private:
    std::deque<Packet> packets_;
};

struct PollAge {
    int64_t age_ms;            // -1 before the first poll finished
    bool polled_since_resume;  // a poll finished after polling last resumed
};

// Age of the last proactive poll result, counted from that poll or from the last resume of
// polling after a pause (session, mute), whichever is later. Times in microseconds, 0 = never.
inline PollAge PollAgeAt(int64_t now_us, int64_t done_us, int64_t resumed_us) {
    if (done_us == 0) {
        return {-1, false};
    }
    int64_t since_us = done_us > resumed_us ? done_us : resumed_us;
    return {(now_us - since_us) / 1000, done_us >= resumed_us};
}

}  // namespace weixue_session

#endif  // WEIXUE_SESSION_H
