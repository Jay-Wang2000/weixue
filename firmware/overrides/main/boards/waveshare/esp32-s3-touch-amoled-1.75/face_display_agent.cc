// FaceDisplay: agent states on the face, cards, management view page 1 and earcons
// (weixue-agent-ui-v1; decisions in agent_ui.h, widgets in agent_view.cc).
#include "face_display.h"

#include "agent_earcons.h"
#include "agent_loopback.h"
#include "agent_view.h"
#include "application.h"
#include "assets.h"
#include "lvgl_font.h"
#include "monitor_panel.h"
#include "weixue_agent.h"

#include <esp_log.h>
#include <esp_timer.h>

#include <algorithm>
#include <cstring>
#include <ctime>

#define TAG "FaceDisplay"

namespace {

using namespace agent_ui;
using weixue_agent::Response;

constexpr const char* kZhFontAsset = "font_noto_sans_common_20_4.bin";
constexpr int64_t kRenderGapMs = 250;
constexpr int64_t kWakePressUs = 400 * 1000;
constexpr int64_t kBedsideResumeAgentUs = 15LL * 1000 * 1000;
constexpr int64_t kHopUs = 300 * 1000;
constexpr int64_t kNodUs = 120 * 1000;
constexpr int32_t kBedsideDotY = 233 - 26 + 56 + 24;

int64_t NowMs() { return esp_timer_get_time() / 1000; }

bool TouchDown() {
    for (lv_indev_t* i = lv_indev_get_next(nullptr); i != nullptr; i = lv_indev_get_next(i)) {
        if (lv_indev_get_type(i) == LV_INDEV_TYPE_POINTER && lv_indev_get_state(i) == LV_INDEV_STATE_PRESSED) {
            return true;
        }
    }
    return false;
}

}  // namespace

void FaceDisplay::SetupAgent(lv_obj_t* screen) {
    AgentView::Callbacks cb;
    cb.complication_tap = [this]() {
        auto c = agent_.Face(NowMs());
        if (c.show) {
            AgentTapTask(c.task_id);
        }
    };
    cb.card_tap = [this](int index) { AgentCardTap(index); };
    cb.row_tap = [this](const std::string& task_id) { AgentTapTask(task_id); };
    cb.chats_toggle = [this]() { AgentChatsToggle(); };
    cb.chat_tap = [this](const std::string& chat_id) { AgentChatTap(chat_id); };
    cb.home_tap = [this]() { panel_->SetPage(1); };
    cb.wake_press = [this]() {
        return dimmed_.load() || esp_timer_get_time() - woke_us_.load() < kWakePressUs;
    };
    agent_view_ = std::make_unique<AgentView>(face_root_, screen, panel_->Page1(), width_, height_,
                                              std::move(cb));
    // z-order: face, chrome, card, ring, management view.
    lv_obj_move_to_index(agent_view_->card_root(), lv_obj_get_index(ring_obj_));

#if WEIXUE_AGENT_LOOPBACK
    // Loopback test build: no bedside page this boot (RAM hint), so fixture runs do not depend
    // on the hour.
    bedside_schedule_.SetHint(BedsideSchedule::Hint::kDay, -1, 0);
    ESP_LOGW(TAG, "agent ui: loopback build, bedside hint=day for this boot");
#endif
    auto& agent = WeixueAgent::GetInstance();
    agent.SetRenderAck(true);
    agent.Subscribe([this](const weixue_agent::Store& store) { OnAgentStore(store); });
}

void FaceDisplay::LoadZhFont() {
    if (zh_font_ != nullptr || agent_view_ == nullptr) {
        return;
    }
    void* ptr = nullptr;
    size_t size = 0;
    if (!Assets::GetInstance().GetAssetData(kZhFontAsset, ptr, size)) {
        return;
    }
    auto font = std::make_shared<LvglCBinFont>(ptr);
    if (font->font() == nullptr) {
        ESP_LOGW(TAG, "agent ui: %s is not a font; English-only sublabels", kZhFontAsset);
        return;
    }
    zh_font_ = font;
    agent_view_->SetZhFont(font->font());
    ESP_LOGI(TAG, "agent ui: 20 px CJK font loaded (%u bytes)", static_cast<unsigned>(size));
}

void FaceDisplay::SetPowerSaveMode(bool on) {
    SpiLcdDisplay::SetPowerSaveMode(on);
    dimmed_ = on;
    if (!on) {
        woke_us_ = esp_timer_get_time();
    }
}

// Main task: copies the store for the next LVGL tick.
void FaceDisplay::OnAgentStore(const weixue_agent::Store& store) {
    DisplayLockGuard lock(this);
    agent_pending_ = store.tasks();
    agent_pending_seq_ = store.max_seq();
    if (store.syncs() != agent_syncs_) {
        agent_syncs_ = store.syncs();
        agent_synced_out_.insert(agent_synced_out_.end(), store.synced_out().begin(), store.synced_out().end());
    }
    agent_dirty_ = true;
}

std::string FaceDisplay::LocalHhmm() const { return local_minute_ >= 0 ? clock_text_ : ""; }

void FaceDisplay::SendAgent(Response r, std::function<void(bool)> done, std::function<void(uint32_t)> queued) {
    Application::GetInstance().Schedule([this, r = std::move(r), done = std::move(done),
                                         queued = std::move(queued)]() mutable {
        auto callback = [this, done](bool sent) {
            if (done) {
                DisplayLockGuard lock(this);
                done(sent);
            }
        };
        uint32_t id = WeixueAgent::GetInstance().SendResponse(std::move(r), callback);
        DisplayLockGuard lock(this);
        if (id == 0) {
            if (done) done(false);
        } else if (queued) {
            queued(id);
        }
    });
}

// Drops the outbox entry of a decision send (main task: WeixueAgent::Cancel).
void FaceDisplay::CancelCardSend(uint32_t token) {
    for (auto it = card_sends_.begin(); it != card_sends_.end(); ++it) {
        if (it->first == token) {
            uint32_t id = it->second;
            card_sends_.erase(it);
            Application::GetInstance().Schedule([id]() { WeixueAgent::GetInstance().Cancel(id); });
            return;
        }
    }
}

void FaceDisplay::PlayEarcon(Earcon earcon, bool interrupt) {
    if (earcon == Earcon::kNone) {
        return;
    }
    if (bedside_ && !interrupt) {
        ESP_LOGI(TAG, "earcon=%s skipped: bedside", EarconName(earcon));
        return;
    }
    Application::GetInstance().Schedule([earcon]() {
        auto& app = Application::GetInstance();
        if (app.GetDeviceState() != kDeviceStateIdle || app.IsMicMuted()) {
            ESP_LOGI(TAG, "earcon=%s skipped: device busy or muted", EarconName(earcon));
            return;
        }
        app.PlaySound(agent_earcons::Get(EarconName(earcon)));
        ESP_LOGI(TAG, "earcon=%s", EarconName(earcon));
    });
}

// Store changes: arrival effects (§3.1, §3.2). At most one earcon per update: the most urgent.
void FaceDisplay::ApplyArrivals(const std::vector<Arrival>& arrivals, int64_t now_ms) {
    Earcon best = Earcon::kNone;
    bool best_interrupt = false;
    auto rank = [](Earcon e) {
        switch (e) {
            case Earcon::kNeedsYou: return 4;
            case Earcon::kError: return 3;
            case Earcon::kDone: return 2;
            default: return 0;
        }
    };
    int64_t now_us = now_ms * 1000;
    for (const Arrival& a : arrivals) {
        Earcon e = EarconFor(a);
        Flash f = FlashFor(a, now_ms);
        agent_.StartFlash(f);
        if (rank(e) > rank(best)) {
            best = e;
            best_interrupt = a.tier == Tier::kInterrupt;
        }
        if (a.tier == Tier::kInterrupt && (weixue_agent::IsOpenCard(a.state) || e != Earcon::kNone)) {
            bedside_resume_us_ = now_us + kBedsideResumeAgentUs;
        }
        if (a.tier != Tier::kSilent) {
            if (weixue_agent::IsOpenCard(a.state)) {
                next_blink_us_ = now_us;
            } else if (a.state == State::kUnread) {
                hop_until_us_ = now_us + kHopUs;
            }
        }
        ESP_LOGI(TAG, "agent ui: task=%s state=%s tier=%s flash=%d earcon=%s", a.task_id.c_str(),
                 weixue_agent::StateName(a.state), weixue_agent::TierName(a.tier), f.count,
                 EarconName(e));
    }
    PlayEarcon(best, best_interrupt);
}

void FaceDisplay::SetTransient(const std::string& title, const std::string& summary, uint32_t color,
                               int64_t until_ms) {
    transient_title_ = title;
    transient_summary_ = summary;
    transient_color_ = color;
    transient_until_ms_ = until_ms;
    agent_view_->SetTransient(title, summary, color);
}

void FaceDisplay::OpenAgentCard(const std::string& task_id) {
    int64_t now = NowMs();
    if (!agent_.OpenCard(task_id, now)) {
        return;
    }
    if (panel_->IsShown()) {
        ShowPanel(false);
    }
    agent_view_->SetCard(agent_.Card(now));
    agent_view_->ShowCard(true);
    ESP_LOGI(TAG, "agent ui: card open task=%s", task_id.c_str());
#if WEIXUE_FACE_DIAG
    diag_dump_due_us_ = esp_timer_get_time() + 800 * 1000;
#endif
}

void FaceDisplay::CloseAgentCard() {
    lv_obj_remove_flag(face_root_, LV_OBJ_FLAG_HIDDEN);
    agent_view_->ShowCard(false);
    ESP_LOGI(TAG, "agent ui: card closed");
}

void FaceDisplay::AgentCardTap(int index) {
    int64_t now = NowMs();
    if (index < 0) {
        if (!agent_.CardTapGuarded(now)) {
            agent_.Later();
            CloseAgentCard();
        }
        return;
    }
    auto d = agent_.Tap(index, now);
    agent_view_->SetCard(agent_.Card(now));
    if (d.refreshed) {
        ESP_LOGI(TAG, "agent ui: card changed under the tap, refreshed task=%s", agent_.card_task().c_str());
    }
    if (!d.send) {
        return;
    }
    Response r;
    r.kind = d.kind;
    r.chat_id = d.chat_id;
    r.task_id = d.task_id;
    r.card_id = d.card_id;
    r.option_id = d.option_id;
    std::string card_id = d.card_id;
    uint32_t token = d.token;
    SendAgent(std::move(r), [this, card_id, token](bool recorded) {
        int64_t t = NowMs();
        Earcon e = agent_.OnDecisionSent(card_id, token, recorded, t);
        card_sends_.erase(std::remove_if(card_sends_.begin(), card_sends_.end(),
                                         [token](const auto& s) { return s.first == token; }),
                          card_sends_.end());
        if (agent_.card_open()) {
            agent_view_->SetCard(agent_.Card(t));
        }
        PlayEarcon(e, false);
    }, [this, token](uint32_t id) {
        if (agent_.SendPending(token)) {
            card_sends_.push_back({token, id});
        } else {
            WeixueAgent::GetInstance().Cancel(id);
        }
    });
}

void FaceDisplay::AgentTapTask(const std::string& task_id) {
    const auto* t = agent_.Find(task_id);
    if (t == nullptr) {
        return;
    }
    switch (t->item.state) {
        case State::kNeedsApproval:
        case State::kNeedsInput:
            OpenAgentCard(task_id);
            break;
        case State::kUnread:
        case State::kFailed:
        case State::kCancelled:
        case State::kStopped:
            agent_.MarkRead(task_id);
            Application::GetInstance().Schedule([task_id]() { WeixueAgent::GetInstance().AckSeen(task_id); });
            agent_drawn_ms_ = 0;
            break;
        case State::kWorking:
        case State::kInterrupted:
            if (!panel_->IsShown()) {
                ShowPanel(true);
            }
            break;
        case State::kDone:
            break;
    }
}

void FaceDisplay::AgentChatsToggle() {
    bool open = !agent_view_->ChatsOpen();
    chats_focus_ = 0;
    if (open) {
        auto chats = agent_.Chats(NowMs());
        for (size_t i = 0; i < chats.size(); i++) {
            if (chats[i].current) chats_focus_ = static_cast<int>(i);
        }
    }
    agent_view_->SetHeader(agent_.CurrentTitle(), open);
    agent_drawn_ms_ = 0;
}

void FaceDisplay::AgentChatsScroll(int delta) {
    int n = static_cast<int>(agent_.Chats(NowMs()).size());
    chats_focus_ = std::clamp(chats_focus_ + delta, 0, std::max(0, n - 1));
    agent_drawn_ms_ = 0;
}

void FaceDisplay::AgentChatTap(const std::string& chat_id) {
    int64_t now = NowMs();
    if (!chat_id.empty() && chat_id == agent_.current_chat()) {
        AgentChatsToggle();
        return;
    }
    ShowPanel(false);
    switching_ = Switching();
    switching_.active = true;
    switching_.new_chat = chat_id.empty();
    switching_.chat_id = chat_id;
    switching_.title = chat_id.empty() ? "New chat" : agent_.ChatTitle(chat_id);
    switching_.start_ms = now;
    SetTransient(switching_.title, "Switching...", color::kValue, now + kSwitchTimeoutMs + kOutcomeMs);
    Response r;
    r.kind = chat_id.empty() ? "new_chat" : "switch_chat";
    r.chat_id = chat_id;
    std::string target = chat_id;
    SendAgent(std::move(r), [this, target](bool sent) {
        if (!switching_.active || switching_.chat_id != target) {
            return;
        }
        int64_t t = NowMs();
        switching_.active = false;
        if (!sent) {
            SetTransient(switching_.title, "Couldn't switch", color::kFail, t + kTransientMs / 2);
            return;
        }
        std::string summary = "Ready. What's up?";
        if (target.empty()) {
            agent_.StartNewChat();
        } else {
            agent_.SetCurrentChat(target);
            const weixue_agent::Task* last = nullptr;
            for (const auto& task : agent_.tasks()) {
                if (task.item.chat_id == target && (last == nullptr || task.item.seq > last->item.seq)) {
                    last = &task;
                }
            }
            summary = last != nullptr ? "Last: " + last->item.summary : "";
        }
        SetTransient(switching_.title, summary, color::kValue, t + kTransientMs);
        next_blink_us_ = esp_timer_get_time();
        PlayEarcon(Earcon::kChatSwitch, false);
        switch_send_id_ = 0;
        ESP_LOGI(TAG, "agent ui: chat=%s current", target.empty() ? "(new)" : target.c_str());
    }, [this](uint32_t id) { switch_send_id_ = id; });
}

// Called from Tick with the display lock held.
void FaceDisplay::UpdateAgent(int64_t now_us, DeviceState state) {
    if (agent_view_ == nullptr) {
        return;
    }
    int64_t now = now_us / 1000;
    if (agent_dirty_) {
        agent_dirty_ = false;
        auto arrivals = agent_.SetTasks(agent_pending_, now, LocalHhmm(), agent_synced_out_);
        agent_synced_out_.clear();
        ApplyArrivals(arrivals, now);
        int64_t upto = agent_pending_seq_;
        Application::GetInstance().Schedule([upto]() { WeixueAgent::GetInstance().OnRendered(upto); });
        agent_drawn_ms_ = 0;
    }

    Context ctx;
    ctx.voice_idle = state == kDeviceStateIdle;
    ctx.idle_since_ms = idle_since_us_ / 1000;
    ctx.last_touch_ms = now - static_cast<int64_t>(lv_display_get_inactive_time(display_));
    ctx.touch_down = TouchDown();
    ctx.panel_shown = panel_->IsShown();
    ctx.bedside = bedside_;
    ctx.dimmed = dimmed_;
    std::string open = agent_.WantAutoOpen(ctx, now);
    if (!open.empty()) {
        if (bedside_) {
            bedside_resume_us_ = now_us + kBedsideResumeAgentUs;
        }
        OpenAgentCard(open);
    }

    bool expired = false;
    for (uint32_t token : agent_.ExpireSends(now)) {
        ESP_LOGW(TAG, "agent ui: decision not resolved within %lld ms, cancelled",
                 static_cast<long long>(kSendHardTimeoutMs));
        CancelCardSend(token);
        expired = true;
    }
    if (agent_.card_open()) {
        auto tick = agent_.TickCard(now);
        if (tick.close) {
            CloseAgentCard();
        } else if (tick.changed || expired || now - agent_drawn_ms_ >= kRenderGapMs) {
            agent_view_->SetCard(agent_.Card(now));
        }
    } else if (agent_view_->CardShown()) {
        CloseAgentCard();
    }
    if (agent_view_->CardCovering() && !lv_obj_has_flag(face_root_, LV_OBJ_FLAG_HIDDEN)) {
        lv_obj_add_flag(face_root_, LV_OBJ_FLAG_HIDDEN);
    }

    if (switching_.active && now - switching_.start_ms >= kSwitchTimeoutMs) {
        switching_.active = false;
        SetTransient(switching_.title, "Couldn't switch", color::kFail, now + kTransientMs / 2);
        if (switch_send_id_ != 0) {
            uint32_t id = switch_send_id_;
            switch_send_id_ = 0;
            Application::GetInstance().Schedule([id]() { WeixueAgent::GetInstance().Cancel(id); });
        }
    }
    if (!transient_title_.empty() && now >= transient_until_ms_) {
        SetTransient("", "", 0, 0);
    }
    if (state != kDeviceStateIdle && !transient_title_.empty() && !switching_.active) {
        SetTransient("", "", 0, 0);
    }

    if (now - agent_drawn_ms_ >= kRenderGapMs) {
        agent_drawn_ms_ = now;
        RenderAgent(now);
    }
    agent_view_->Animate(now);
    agent_view_->SetBlink(blink_);
}

void FaceDisplay::RenderAgent(int64_t now) {
    Complication c = agent_.Face(now);
    bool idle_long = mode_ == Mode::kIdle && esp_timer_get_time() - idle_since_us_ >= kIdleBeforeMiniMs * 1000;
    bool mini = c.show && idle_long && !bedside_ && transient_title_.empty() && !agent_view_->CardShown();
    Pose pose = mini ? c.pose : Pose::kFull;
    if (pose != pose_) {
        pose_ = pose;
        ESP_LOGI(TAG, "agent ui: pose=%s top=%s", PoseName(pose_), c.task_id.c_str());
#if WEIXUE_FACE_DIAG
        if (pose_ != Pose::kFull) {
            diag_dump_due_us_ = esp_timer_get_time() + 800 * 1000;
        }
#endif
    }
    agent_view_->SetComplication(c, mini, now);
    if (c.show && c.task_id == last_top_) {
        const auto* t = agent_.Find(c.task_id);
        int step = t != nullptr && t->item.has_progress ? t->item.progress_step : -1;
        if (step != last_step_ && last_step_ >= 0) {
            nod_until_us_ = esp_timer_get_time() + kNodUs;
        }
        last_step_ = step;
    } else {
        last_top_ = c.show ? c.task_id : "";
        last_step_ = -1;
    }

    bool pending = agent_.HasPendingOpen() || agent_.UnreadDot(now) != 0;
    agent_view_->SetBedsideDot(bedside_ && pending, kBedsideDotY + ShiftY());

    if (panel_->IsShown()) {
        if (agent_view_->ChatsOpen()) {
            agent_view_->SetChats(agent_.Chats(now), chats_focus_);
        } else {
            agent_view_->SetHeader(agent_.CurrentTitle(), false);
            int more = 0;
            agent_view_->SetRows(agent_.Rows(now, 3, &more), more);
        }
        const auto* next = agent_.NextUp(now);
        if (next != nullptr) {
            bool approval = next->item.state == State::kNeedsApproval;
            panel_->SetNextUp(Model::TitleOf(*next), approval ? "Needs your OK" : "Question", color::kAttentionText);
        } else {
            panel_->SetNextUp("", "", color::kAttentionText);
        }
    }
}

// Ring cues while the voice states leave the ring free; false when there are none.
bool FaceDisplay::AgentRing(int64_t now_ms, RingState& s, const char** kind) {
    if (agent_view_ == nullptr || bedside_) {
        return false;
    }
    RingHints h = agent_.Ring(now_ms, CueRotation(shift_x_));
    if (h.count == 0) {
        return false;
    }
    s.style = RingState::Style::kArcs;
    s.opa = LV_OPA_COVER;
    for (int i = 0; i < h.count; i++) {
        const Arc& a = h.arcs[i];
        s.AddArc({static_cast<int16_t>(ClockToLvDeg(a.start)), a.span, a.color, a.opa, a.rounded});
    }
    *kind = h.kind;
    return s.n_arcs > 0;
}

// Small-face targets (§4.1-4.3): 60x70 eyes above the complication, shaped by the pose.
void FaceDisplay::AgentPose(EyeShape out[2]) const {
    constexpr float kMiniSpacing = 48.0f;
    constexpr float kMiniY = -90.0f;
    int64_t now = esp_timer_get_time();
    float dy = (now < hop_until_us_ ? -8.0f : 0.0f) + (now < nod_until_us_ ? 4.0f : 0.0f);
    for (int i = 0; i < 2; i++) {
        out[i] = {(i == 0 ? -1.0f : 1.0f) * kMiniSpacing, kMiniY + dy, 60, 70, 20, 0, 0, 0};
    }
    switch (pose_) {
        case Pose::kAttentive:
            for (int i = 0; i < 2; i++) {
                out[i].w = 62;
                out[i].h = 66;
                out[i].r = 10;
                out[i].y = -92 + dy;
            }
            break;
        case Pose::kHappy:
            for (int i = 0; i < 2; i++) {
                out[i].cut = 0.5f;
                out[i].y -= 6;
            }
            break;
        case Pose::kSad:
            for (int i = 0; i < 2; i++) {
                out[i].lid_out = 0.42f;
                out[i].h *= 0.92f;
                out[i].y += 6;
            }
            break;
        case Pose::kConfused:
            out[0].h *= 0.7f;
            out[0].lid_in = 0.3f;
            out[1].h *= 1.08f;
            out[1].y -= 4;
            break;
        case Pose::kMini:
        case Pose::kFull:
            break;
    }
}
