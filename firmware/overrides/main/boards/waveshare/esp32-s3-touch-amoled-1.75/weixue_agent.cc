#include "weixue_agent.h"

#include <esp_log.h>
#include <esp_random.h>

#include <algorithm>
#include <cstring>
#include <utility>

#include "agent_loopback.h"
#include "application.h"
#include "board.h"
#include "settings.h"

#define TAG "WeixueAgent"

namespace {

using namespace weixue_agent;

constexpr size_t kOutboxSize = 8;
// Retry delays after a send that did not reach the transport; dropped after the last.
constexpr int64_t kRetryDelayUs[] = {1000000, 3000000, 9000000};
constexpr int kMaxAttempts = 1 + sizeof(kRetryDelayUs) / sizeof(kRetryDelayUs[0]);
constexpr int64_t kShownDelayUs = 200000;
constexpr int64_t kLoopbackDelayUs = 2000000;
constexpr int64_t kTickUs = 100000;
constexpr int kPollApplyWaitMs = 1000;

int64_t NowUs() { return esp_timer_get_time(); }

bool NeedsCard(const std::string& kind) {
    return kind == "approve" || kind == "deny" || kind == "answer";
}

}  // namespace

void WeixueAgent::Start() {
    if (tick_timer_ != nullptr) {
        return;
    }
    boot_ = esp_random();
    poll_applied_ = xSemaphoreCreateBinary();
    const esp_timer_create_args_t args = {
        .callback =
            [](void* arg) {
                auto* self = static_cast<WeixueAgent*>(arg);
                Application::GetInstance().Schedule([self]() { self->Tick(); });
            },
        .arg = this,
        .dispatch_method = ESP_TIMER_TASK,
        .name = "agent_tick",
        .skip_unhandled_events = true,
    };
    esp_timer_create(&args, &tick_timer_);
#if WEIXUE_AGENT_LOOPBACK
    // Loopback test build: speaker silent for this boot; the stored volume is kept for the next.
    auto* codec = Board::GetInstance().GetAudioCodec();
    int stored = codec->output_volume();
    codec->SetOutputVolume(0);
    Settings("audio", true).SetInt("output_volume", stored);
    ESP_LOGW(TAG, "agent: loopback build, answers debug_reply after 2 s; output volume 0 (stored %d kept)",
             stored);
#endif
    ESP_LOGI(TAG, "agent: protocol v%d, boot %lu", kVersion, static_cast<unsigned long>(boot_));
}

void WeixueAgent::OnFrame(const cJSON* payload, size_t frame_bytes) {
    if (frame_bytes > kMaxFrameBytes) {
        ESP_LOGW(TAG, "agent: dropped frame of %u bytes (limit %u)", static_cast<unsigned>(frame_bytes),
                 static_cast<unsigned>(kMaxFrameBytes));
        return;
    }
    auto parsed = Parse(payload);
    if (!parsed.message) {
        ESP_LOGW(TAG, "agent: dropped frame (%s)", parsed.error);
        return;
    }
    ScheduleApply(std::move(*parsed.message), "push");
}

bool WeixueAgent::OnPollBody(const cJSON* body) {
    const cJSON* items = cJSON_GetObjectItemCaseSensitive(body, "agent");
    bool scheduled = false;
    if (cJSON_IsArray(items)) {
        const cJSON* entry = nullptr;
        cJSON_ArrayForEach(entry, items) {
            char* printed = cJSON_PrintUnformatted(entry);
            size_t bytes = printed ? strlen(printed) : 0;
            cJSON_free(printed);
            if (bytes > kMaxFrameBytes) {
                ESP_LOGW(TAG, "agent: dropped poll item of %u bytes", static_cast<unsigned>(bytes));
                continue;
            }
            auto parsed = Parse(entry);
            if (!parsed.message) {
                ESP_LOGW(TAG, "agent: dropped poll item (%s)", parsed.error);
                continue;
            }
            ScheduleApply(std::move(*parsed.message), "poll");
            scheduled = true;
        }
    }
    // The next poll's X-Weixue-Agent-Seq must cover what the main task accepted from this body.
    if (scheduled && poll_applied_ != nullptr) {
        xSemaphoreTake(poll_applied_, 0);
        Application::GetInstance().Schedule([this]() { xSemaphoreGive(poll_applied_); });
        if (xSemaphoreTake(poll_applied_, pdMS_TO_TICKS(kPollApplyWaitMs)) != pdTRUE) {
            ESP_LOGW(TAG, "agent: poll items not applied within %d ms", kPollApplyWaitMs);
        }
    }
    const cJSON* speak = cJSON_GetObjectItemCaseSensitive(body, "speak");
    return !cJSON_IsFalse(speak);
}

void WeixueAgent::ScheduleApply(Message message, const char* via) {
    Application::GetInstance().Schedule(
        [this, message = std::move(message), via]() { Apply(message, via); });
}

void WeixueAgent::Apply(const Message& message, const char* via) {
    int64_t now_ms = NowUs() / 1000;
    if (message.kind == Message::Kind::kSync) {
        size_t dropped = store_.Sync(message.sync.task_ids, message.sync.seq);
        ESP_LOGI(TAG, "agent: sync seq=%lld listed=%u dropped=%u tasks=%u via=%s",
                 static_cast<long long>(message.sync.seq),
                 static_cast<unsigned>(message.sync.task_ids.size()), static_cast<unsigned>(dropped),
                 static_cast<unsigned>(store_.tasks().size()), via);
    } else {
        const Item& item = message.item;
        ApplyResult result = store_.Apply(item, now_ms);
        ESP_LOGI(TAG, "agent: chat=%s task=%s state=%s seq=%lld tier=%s speaks=%d result=%s via=%s",
                 item.chat_id.c_str(), item.task_id.c_str(), StateName(item.state),
                 static_cast<long long>(item.seq), TierName(item.tier), item.speaks,
                 ApplyResultName(result), via);
        if (result != ApplyResult::kApplied && result != ApplyResult::kDuplicate) {
            return;
        }
#if WEIXUE_AGENT_LOOPBACK
        if (result == ApplyResult::kApplied && item.debug_reply) {
            Response r;
            r.kind = item.debug_reply->kind;
            r.chat_id = item.chat_id;
            r.task_id = item.task_id;
            if (NeedsCard(r.kind)) {
                r.card_id = item.card_id;
            }
            r.option_id = item.debug_reply->option_id;
            r.text = item.debug_reply->text;
            r.via = "loopback";
            loopback_.push_back({NowUs() + kLoopbackDelayUs, std::move(r)});
            EnsureTicking();
        }
#endif
    }
    int64_t seen = applied_seq_.load();
    int64_t seq = store_.max_seq();
    while (seq > seen && !applied_seq_.compare_exchange_weak(seen, seq)) {
    }
    QueueShown();
    Notify();
}

void WeixueAgent::Notify() {
    for (auto& listener : listeners_) {
        listener(store_);
    }
}

void WeixueAgent::Subscribe(Listener listener) { listeners_.push_back(std::move(listener)); }

// `shown` goes only over an open audio channel; an idle device acks through its next poll.
void WeixueAgent::QueueShown() {
    if (!Application::GetInstance().GetBackendStatus().channel_open || store_.max_seq() == 0) {
        return;
    }
    Response r;
    r.kind = "shown";
    r.ack_upto = store_.max_seq();
    r.via = "tap";
    for (size_t i = in_flight_ ? 1 : 0; i < outbox_.size(); ++i) {
        if (outbox_[i].shown) {
            outbox_[i].payload = BuildResponse(boot_, next_seq_++, r);
            outbox_[i].label = "shown upto " + std::to_string(r.ack_upto);
            return;
        }
    }
    Enqueue(BuildResponse(boot_, next_seq_++, r), "shown upto " + std::to_string(r.ack_upto));
    outbox_.back().shown = true;
    outbox_.back().due_us = NowUs() + kShownDelayUs;
}

bool WeixueAgent::SendResponse(Response response) {
    if (!IsResponseKind(response.kind) || response.kind == "shown") {
        ESP_LOGW(TAG, "agent: response kind %s refused", response.kind.c_str());
        return false;
    }
    if (NeedsCard(response.kind) && response.card_id.empty()) {
        ESP_LOGW(TAG, "agent: %s without card_id refused", response.kind.c_str());
        return false;
    }
    uint32_t seq = next_seq_++;
    std::string payload = BuildResponse(boot_, seq, response);
    if (payload.empty()) {
        return false;
    }
    ESP_LOGI(TAG, "agent response: kind=%s chat=%s task=%s card=%s option=%s via=%s seq=%lu",
             response.kind.c_str(), response.chat_id.c_str(), response.task_id.c_str(),
             response.card_id.c_str(), response.option_id.c_str(), response.via.c_str(),
             static_cast<unsigned long>(seq));
    Enqueue(std::move(payload), response.kind + " " + response.task_id);
    return true;
}

bool WeixueAgent::AckSeen(const std::string& task_id) {
    const Task* task = store_.Find(task_id);
    if (task == nullptr) {
        return false;
    }
    Response r;
    r.kind = "ack_seen";
    r.chat_id = task->item.chat_id;
    r.task_id = task_id;
    store_.MarkSeen(task_id);
    Notify();
    return SendResponse(std::move(r));
}

void WeixueAgent::Enqueue(std::string payload, std::string label) {
    if (outbox_.size() >= kOutboxSize) {
        size_t victim = in_flight_ ? 1 : 0;
        ESP_LOGW(TAG, "agent outbox full, dropped: %s", outbox_[victim].label.c_str());
        outbox_.erase(outbox_.begin() + victim);
    }
    Outgoing out;
    out.payload = std::move(payload);
    out.label = std::move(label);
    out.due_us = NowUs();
    outbox_.push_back(std::move(out));
    EnsureTicking();
    SendNext();
}

void WeixueAgent::SendNext() {
    while (!in_flight_ && !outbox_.empty()) {
        Outgoing& front = outbox_.front();
        if (NowUs() < front.due_us) {
            return;
        }
        if (front.shown && !Application::GetInstance().GetBackendStatus().channel_open) {
            outbox_.pop_front();
            continue;
        }
        in_flight_ = true;
        front.attempts++;
        Application::GetInstance().SendDeviceEvent(front.payload, [this](bool sent) {
            in_flight_ = false;
            if (outbox_.empty()) {
                return;
            }
            Outgoing& done = outbox_.front();
            if (sent) {
                ESP_LOGI(TAG, "agent ack sent: %s (attempt %d)", done.label.c_str(), done.attempts);
                outbox_.pop_front();
            } else if (done.attempts >= kMaxAttempts) {
                ESP_LOGW(TAG, "agent ack dropped after %d attempts: %s", done.attempts,
                         done.label.c_str());
                outbox_.pop_front();
            } else {
                done.due_us = NowUs() + kRetryDelayUs[done.attempts - 1];
                ESP_LOGW(TAG, "agent ack not sent (attempt %d), retry in %lld ms: %s", done.attempts,
                         static_cast<long long>(kRetryDelayUs[done.attempts - 1] / 1000),
                         done.label.c_str());
            }
            EnsureTicking();
        });
    }
}

// Playback acks of `ack:true` sentences.

void WeixueAgent::OnTtsStart(const std::string& sentence_id, bool ack) {
    if (!sentence_.empty() && sentence_id != sentence_) {
        FinishPlayback(false, "superseded");
    }
    if (!ack || sentence_id.empty() || sentence_id == sentence_) {
        return;
    }
    sentence_ = sentence_id;
    sentence_stop_ = false;
    sentence_start_ms_ = Application::GetInstance().GetAudioService().GetOutputMs();
    ESP_LOGI(TAG, "agent: tracking playback of sentence %s", sentence_.c_str());
    EnsureTicking();
}

void WeixueAgent::OnTtsRefused(const std::string& sentence_id, bool ack) {
    if (!ack || sentence_id.empty()) {
        return;
    }
    if (!sentence_.empty() && sentence_ != sentence_id) {
        FinishPlayback(false, "superseded");
    }
    sentence_ = sentence_id;
    sentence_start_ms_ = Application::GetInstance().GetAudioService().GetOutputMs();
    FinishPlayback(false, "mute");
}

void WeixueAgent::OnTtsStop(const std::string& sentence_id) {
    if (!sentence_.empty() && (sentence_id.empty() || sentence_id == sentence_)) {
        sentence_stop_ = true;
        EnsureTicking();
    }
}

void WeixueAgent::OnPlaybackCut(const char* reason) {
    if (sentence_.empty()) {
        return;
    }
    if (sentence_stop_ && strcmp(reason, "closed") == 0) {
        return;
    }
    FinishPlayback(false, reason);
}

void WeixueAgent::FinishPlayback(bool complete, const char* reason) {
    uint64_t played_ms = Application::GetInstance().GetAudioService().GetOutputMs() - sentence_start_ms_;
    std::string payload = BuildPlayed(boot_, next_seq_++, sentence_, complete,
                                      static_cast<uint32_t>(played_ms), reason);
    ESP_LOGI(TAG, "agent played: sentence=%s complete=%d played_ms=%llu reason=%s", sentence_.c_str(),
             complete, static_cast<unsigned long long>(played_ms), complete ? "-" : reason);
    std::string label = std::string("played ") + (complete ? "complete " : "cut ") + sentence_;
    sentence_.clear();
    sentence_stop_ = false;
    if (!payload.empty()) {
        Enqueue(std::move(payload), std::move(label));
    }
}

// Timer

void WeixueAgent::EnsureTicking() {
    if (!ticking_ && tick_timer_ != nullptr) {
        ticking_ = true;
        esp_timer_start_periodic(tick_timer_, kTickUs);
    }
}

void WeixueAgent::Tick() {
    if (!sentence_.empty() && sentence_stop_ &&
        Application::GetInstance().GetAudioService().IsPlaybackIdle()) {
        FinishPlayback(true, nullptr);
    }
    int64_t now = NowUs();
    for (size_t i = 0; i < loopback_.size();) {
        if (now < loopback_[i].due_us) {
            ++i;
            continue;
        }
        Response r = std::move(loopback_[i].response);
        loopback_.erase(loopback_.begin() + i);
        if (r.kind == "ack_seen") {
            store_.MarkSeen(r.task_id);
            Notify();
        }
        SendResponse(std::move(r));
    }
    SendNext();
    bool busy = !outbox_.empty() || !loopback_.empty() || (!sentence_.empty() && sentence_stop_);
    if (!busy && ticking_) {
        ticking_ = false;
        esp_timer_stop(tick_timer_);
    }
}
