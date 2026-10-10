#ifndef WEIXUE_MONITOR_PANEL_H
#define WEIXUE_MONITOR_PANEL_H

#include "face_diag.h"
#include "face_state.h"

#include <lvgl.h>

#include <cstdint>
#include <functional>
#include <string>

// Management view reached by swiping left from the face (weixue-agent-ui-v1 §4.9): page 1 holds
// the current chat and the agent tasks (filled by AgentView through Page1()), page 2 the backend,
// Wi-Fi, battery and microphone state, the last transcript and reply, and Next up (the most urgent
// card, opened full-screen). Page dots at 3 o'clock. All methods run with the display lock held.
class MonitorPanel {
public:
    // on_cover(true) runs once the view covers the screen, on_cover(false) before it uncovers;
    // on_open_next opens the Next up card.
    MonitorPanel(lv_obj_t* screen, int32_t width, int32_t height, const lv_font_t* icon_font,
                 std::function<void(bool)> on_cover, std::function<void()> on_open_next);

    // Showing always starts on page 1.
    void Show(bool show);
    bool IsShown() const { return shown_; }
    lv_obj_t* Page1() const { return page1_; }
    void SetPage(int page);
    int page() const { return page_; }
    // Next up row: empty title shows "Nothing needs you".
    void SetNextUp(const std::string& title, const std::string& state, uint32_t state_color);

    void SetHeard(const char* text);
    // reply: Application::GetReplyCount() when the text arrived; a new value starts a new reply.
    void AddReply(const char* text, uint32_t reply);
    void SetShift(int32_t x, int32_t y);
    // Re-derives line heights from the inherited text font after a theme or font change.
    void ApplyFonts();

private:
    int32_t width_;
    const lv_font_t* icon_font_;
    std::function<void(bool)> on_cover_;
    std::function<void()> on_open_next_;
    bool shown_ = false;
    int page_ = 0;

    lv_obj_t* root_ = nullptr;
    lv_obj_t* page1_ = nullptr;
    lv_obj_t* content_ = nullptr;
    lv_obj_t* dots_[2] = {nullptr, nullptr};
    lv_obj_t* next_title_ = nullptr;
    lv_obj_t* next_state_ = nullptr;
    lv_obj_t* next_open_ = nullptr;
    lv_obj_t* wifi_icon_ = nullptr;
    lv_obj_t* wifi_value_ = nullptr;
    lv_obj_t* battery_icon_ = nullptr;
    lv_obj_t* battery_value_ = nullptr;
    lv_obj_t* mic_icon_ = nullptr;
    lv_obj_t* backend_value_ = nullptr;
    lv_obj_t* heard_text_ = nullptr;
    lv_obj_t* reply_text_ = nullptr;
    lv_timer_t* refresh_timer_ = nullptr;

    static constexpr size_t kMaxReplyBytes = 240;
    ReplyText reply_{kMaxReplyBytes};

    lv_obj_t* Row(int32_t y);
    lv_obj_t* Label(lv_obj_t* parent, const char* text, uint32_t color, const lv_font_t* font);
    void BuildNextUp();
    void Refresh();
    void SetText(lv_obj_t* label, const char* text);

    static void SlideCb(void* var, int32_t value);
    static void SlideDoneCb(lv_anim_t* anim);
    static void RefreshCb(lv_timer_t* timer);
    static void OpenCb(lv_event_t* e);

#if WEIXUE_FACE_DIAG
    char diag_values_[96] = "";
    int64_t diag_values_us_ = 0;
    const char* diag_backend_ = nullptr;
    static void DiagBackendCb(lv_timer_t* timer);
#endif
};

#endif  // WEIXUE_MONITOR_PANEL_H
