#ifndef WEIXUE_MONITOR_PANEL_H
#define WEIXUE_MONITOR_PANEL_H

#include "face_diag.h"

#include <lvgl.h>

#include <cstdint>
#include <functional>
#include <string>

// Full-screen monitor page reached by swiping left from the face: backend, Wi-Fi, battery and
// microphone state, the last transcript and reply, task chips and one approval card. Task chips
// and the approval card show fixture data; the card's buttons send `notifications/weixue/panel`
// to the server. All methods run with the display lock held.
class MonitorPanel {
public:
    // on_cover(true) runs once the page covers the screen, on_cover(false) before it uncovers.
    MonitorPanel(lv_obj_t* screen, int32_t width, int32_t height, const lv_font_t* icon_font,
                 std::function<void(bool)> on_cover);

    void Show(bool show);
    bool IsShown() const { return shown_; }

    void SetHeard(const char* text);
    void AddReply(const char* text);
    void SetShift(int32_t x, int32_t y);
    // Re-derives line heights from the inherited text font after a theme or font change.
    void ApplyFonts();

#if WEIXUE_FACE_DIAG
    // Replays swipe left, Approve tap, Deny tap and swipe right through a virtual pointer every
    // kReplayPeriodMs, starting kReplayStartMs after the call.
    void StartTouchReplay();
#endif

private:
    enum class CardState : uint8_t { kPending, kSending, kSent, kFailed };

    int32_t width_;
    const lv_font_t* icon_font_;
    std::function<void(bool)> on_cover_;
    bool shown_ = false;

    lv_obj_t* root_ = nullptr;
    lv_obj_t* content_ = nullptr;
    lv_obj_t* wifi_icon_ = nullptr;
    lv_obj_t* wifi_value_ = nullptr;
    lv_obj_t* battery_icon_ = nullptr;
    lv_obj_t* battery_value_ = nullptr;
    lv_obj_t* mic_icon_ = nullptr;
    lv_obj_t* backend_value_ = nullptr;
    lv_obj_t* heard_text_ = nullptr;
    lv_obj_t* reply_text_ = nullptr;
    lv_obj_t* card_ = nullptr;
    lv_obj_t* card_status_ = nullptr;
    lv_obj_t* buttons_[2] = {nullptr, nullptr};
    lv_timer_t* refresh_timer_ = nullptr;
    lv_timer_t* rearm_timer_ = nullptr;

    std::string reply_;
    CardState card_state_ = CardState::kPending;
    uint32_t event_seq_ = 0;
    int decision_ = 0;

    lv_obj_t* Row(int32_t y);
    lv_obj_t* Label(lv_obj_t* parent, const char* text, uint32_t color, const lv_font_t* font);
    lv_obj_t* Chip(lv_obj_t* parent, const char* zh, const char* en);
    lv_obj_t* Button(lv_obj_t* parent, const char* zh, const char* en, uint32_t color);
    void BuildCard();
    void Refresh();
    void SetText(lv_obj_t* label, const char* text);
    void SetCardState(CardState state);
    void OnDecision(int button);

    static void SlideCb(void* var, int32_t value);
    static void SlideDoneCb(lv_anim_t* anim);
    static void RefreshCb(lv_timer_t* timer);
    static void RearmCb(lv_timer_t* timer);
    static void ButtonCb(lv_event_t* e);

#if WEIXUE_FACE_DIAG
    lv_indev_t* replay_indev_ = nullptr;
    int64_t replay_start_us_ = 0;
    char diag_values_[96] = "";
    int64_t diag_values_us_ = 0;
    static void ReplayReadCb(lv_indev_t* indev, lv_indev_data_t* data);
#endif
};

#endif  // WEIXUE_MONITOR_PANEL_H
