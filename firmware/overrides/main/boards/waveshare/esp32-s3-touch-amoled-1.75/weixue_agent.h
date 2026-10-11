#ifndef WEIXUE_AGENT_H
#define WEIXUE_AGENT_H

#include <cJSON.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

#include <atomic>
#include <cstdint>
#include <deque>
#include <functional>
#include <string>
#include <vector>

#include "weixue_agent_msg.h"
#include "weixue_agent_outbox.h"
#include "weixue_agent_store.h"

// Agent messages on the device (docs/agent-protocol.md): `agent` frames from the WebSocket and
// the proactive poll go into the task store; user responses, `shown` and `played` acks go to the
// server through Application::SendDeviceEvent from a RAM outbox, responses until the server's
// `agent_ack` receipt arrives.
class WeixueAgent {
public:
    static WeixueAgent& GetInstance() {
        static WeixueAgent instance;
        return instance;
    }
    WeixueAgent(const WeixueAgent&) = delete;
    WeixueAgent& operator=(const WeixueAgent&) = delete;

    using Listener = std::function<void(const weixue_agent::Store&)>;

    // Main task, once, after the audio service started.
    void Start();

    // Network task: a `custom` payload of kind agent/agent_sync/agent_ack/agent_chats; frame_bytes
    // is its JSON size.
    void OnFrame(const cJSON* payload, size_t frame_bytes);
    // Poll task: the `agent` array of a 200 poll body; returns once the main task applied it (at
    // most kPollApplyWaitMs). Returns the body's `speak` (true when absent).
    bool OnPollBody(const cJSON* body);
    // Highest seq the store applied since boot (any task): the X-Weixue-Agent-Seq header.
    int64_t AppliedSeq() const { return applied_seq_.load(); }

    // Main task: tts frames.
    void OnTtsStart(const std::string& sentence_id, bool ack);
    void OnTtsRefused(const std::string& sentence_id, bool ack);
    void OnTtsStop(const std::string& sentence_id);
    // Main task: playback of the tracked sentence stopped early; reason is the wire reason
    // (wake|tap|imu|mute|closed|superseded). "closed" after its `tts stop` waits for the drain.
    void OnPlaybackCut(const char* reason);

    // Main task: UI.
    void Subscribe(Listener listener);
    const weixue_agent::Store& store() const { return store_; }
    // Queues a response; done(ok) runs on the main task once its receipt arrived (ok: the server
    // recorded it) or the outbox gave up (false). Returns its outbox id, 0 when refused.
    uint32_t SendResponse(weixue_agent::Response response, std::function<void(bool)> done = nullptr);
    // Drops a queued response that is not in flight; its done callback never runs.
    void Cancel(uint32_t id);
    // User ack of a task (tap on its card or complication).
    bool AckSeen(const std::string& task_id);
    // With render acks on, `shown` waits for OnRendered (the UI drew the store) instead of the
    // apply.
    void SetRenderAck(bool on) { render_ack_ = on; }
    void OnRendered(int64_t upto);

private:
    WeixueAgent() = default;

    struct LoopbackReply {
        int64_t due_us;
        weixue_agent::Response response;
    };

    weixue_agent::Store store_;
    std::vector<Listener> listeners_;
    std::atomic<int64_t> applied_seq_{0};
    uint32_t boot_ = 0;
    uint32_t next_seq_ = 1;
    bool render_ack_ = false;

    weixue_agent::Outbox outbox_;
    std::vector<LoopbackReply> loopback_;

    std::string sentence_;
    bool sentence_stop_ = false;
    uint64_t sentence_start_ms_ = 0;

    esp_timer_handle_t tick_timer_ = nullptr;
    SemaphoreHandle_t poll_applied_ = nullptr;
    bool ticking_ = false;

    void ScheduleApply(weixue_agent::Message message, const char* via);
    void Apply(const weixue_agent::Message& message, const char* via);
    void Notify();
    void QueueShown(int64_t upto);
    void OnReceipt(const weixue_agent::Receipt& receipt);
    uint32_t Enqueue(std::string payload, std::string label, uint32_t seq, bool receipt,
                     std::function<void(bool)> done = nullptr);
    void FinishPlayback(bool complete, const char* reason);
    void Tick();
    void SendNext();
    void EnsureTicking();
};

#endif  // WEIXUE_AGENT_H
