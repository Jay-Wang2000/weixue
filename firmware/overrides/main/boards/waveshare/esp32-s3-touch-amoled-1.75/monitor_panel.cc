#include "monitor_panel.h"

#include "application.h"
#include "board.h"
#include "display.h"

#include <esp_log.h>
#include <esp_timer.h>
#include <material_symbols.h>
#include <wifi_manager.h>

#include <cstdio>
#include <cstring>

#define TAG "MonitorPanel"

LV_FONT_DECLARE(font_noto_sans_basic_20_4);

namespace {

constexpr uint32_t kLabelColor = 0x6E8A92;
constexpr uint32_t kSubColor = 0x4E6268;
constexpr uint32_t kValueColor = 0x9FB4BC;
constexpr uint32_t kOkColor = 0x22B5C4;
constexpr uint32_t kWarnColor = 0xC8963C;
constexpr uint32_t kBadColor = 0xC05A50;
constexpr uint32_t kCardColor = 0x2E6670;
constexpr uint32_t kDotOnColor = 0x9FB4BC;

constexpr uint32_t kRefreshMs = 1000;
constexpr uint32_t kSlideMs = 220;
constexpr int64_t kPollFreshMs = 90000;

// Row centres relative to the screen centre; the round panel narrows towards the top and bottom.
constexpr int32_t kStatusY = -168;
constexpr int32_t kBackendY = -129;
constexpr int32_t kHeardY = -90;
constexpr int32_t kReplyY = -51;
constexpr int32_t kTextWidth = 270;
constexpr int32_t kNextY = 60;
constexpr int32_t kNextW = 320;
constexpr int32_t kNextH = 120;
constexpr int32_t kButtonW = 132;
constexpr int32_t kButtonH = 68;
// Page dots at 3 o'clock, screen pixels.
constexpr int32_t kDotX = 440;
constexpr int32_t kDotY[2] = {223, 243};

#if WEIXUE_FACE_DIAG
constexpr int64_t kDiagValuesMinMs = 5000;
#endif

const lv_font_t* EnFont() { return &font_noto_sans_basic_20_4; }

void Plain(lv_obj_t* obj) {
    lv_obj_remove_style_all(obj);
    lv_obj_remove_flag(obj, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_remove_flag(obj, LV_OBJ_FLAG_CLICKABLE);
}

void Flex(lv_obj_t* obj, lv_flex_flow_t flow, int32_t gap) {
    lv_obj_set_size(obj, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(obj, flow);
    lv_obj_set_flex_align(obj, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(obj, gap, 0);
    lv_obj_set_style_pad_row(obj, 0, 0);
}

const char* BatteryIcon(int level, bool charging) {
    if (charging) {
        return MATERIAL_SYMBOLS_BATTERY_ANDROID_FRAME_BOLT;
    }
    static const char* const kLevels[] = {
        MATERIAL_SYMBOLS_BATTERY_ANDROID_0,       MATERIAL_SYMBOLS_BATTERY_ANDROID_FRAME_1,
        MATERIAL_SYMBOLS_BATTERY_ANDROID_FRAME_2, MATERIAL_SYMBOLS_BATTERY_ANDROID_FRAME_3,
        MATERIAL_SYMBOLS_BATTERY_ANDROID_FRAME_4, MATERIAL_SYMBOLS_BATTERY_ANDROID_FRAME_5,
        MATERIAL_SYMBOLS_BATTERY_ANDROID_FRAME_6, MATERIAL_SYMBOLS_BATTERY_ANDROID_FRAME_FULL,
    };
    int i = level <= 0 ? 0 : level >= 100 ? 7 : 1 + level * 6 / 100;
    return kLevels[i];
}

// Backend row text and colour from the application's backend status.
const char* BackendLabel(const Application::BackendStatus& backend, bool muted, uint32_t* color) {
    switch (BackendRowFor(backend.channel_open, muted, backend.poll_status, backend.poll_age_ms,
                          backend.polled_since_resume, kPollFreshMs)) {
        case BackendRow::kSession:
            *color = kOkColor;
            return "session";
        case BackendRow::kPaused:
            *color = kWarnColor;
            return "paused";
        case BackendRow::kChecking:
            *color = kSubColor;
            return "checking";
        case BackendRow::kOnline:
            *color = kOkColor;
            return "online";
        case BackendRow::kOffline:
            break;
    }
    *color = kBadColor;
    return "offline";
}

}  // namespace

MonitorPanel::MonitorPanel(lv_obj_t* screen, int32_t width, int32_t height,
                           const lv_font_t* icon_font, std::function<void(bool)> on_cover,
                           std::function<void()> on_open_next)
    : width_(width), icon_font_(icon_font), on_cover_(std::move(on_cover)),
      on_open_next_(std::move(on_open_next)) {
    root_ = lv_obj_create(screen);
    lv_obj_remove_style_all(root_);
    lv_obj_remove_flag(root_, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_size(root_, width, height);
    lv_obj_set_style_bg_color(root_, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(root_, LV_OPA_COVER, 0);
    lv_obj_set_pos(root_, width, 0);
    lv_obj_add_flag(root_, LV_OBJ_FLAG_HIDDEN);

    page1_ = lv_obj_create(root_);
    Plain(page1_);
    lv_obj_set_size(page1_, width, height);

    content_ = lv_obj_create(root_);
    Plain(content_);
    lv_obj_set_size(content_, width, height);
    lv_obj_add_flag(content_, LV_OBJ_FLAG_HIDDEN);

    for (int i = 0; i < 2; i++) {
        dots_[i] = lv_obj_create(root_);
        Plain(dots_[i]);
        lv_obj_set_style_radius(dots_[i], LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_opa(dots_[i], LV_OPA_COVER, 0);
    }

    lv_obj_t* status = Row(kStatusY);
    lv_obj_set_style_pad_column(status, 12, 0);
    lv_obj_t* group = lv_obj_create(status);
    Plain(group);
    Flex(group, LV_FLEX_FLOW_ROW, 4);
    wifi_icon_ = Label(group, MATERIAL_SYMBOLS_WIFI_OFF, kLabelColor, icon_font_);
    wifi_value_ = Label(group, "--", kValueColor, EnFont());
    group = lv_obj_create(status);
    Plain(group);
    Flex(group, LV_FLEX_FLOW_ROW, 4);
    battery_icon_ = Label(group, MATERIAL_SYMBOLS_BATTERY_ANDROID_0, kLabelColor, icon_font_);
    battery_value_ = Label(group, "--", kValueColor, EnFont());
    mic_icon_ = Label(status, MATERIAL_SYMBOLS_MIC, kSubColor, icon_font_);

    lv_obj_t* row = Row(kBackendY);
    Label(row, "后端", kLabelColor, nullptr);
    Label(row, "Backend", kSubColor, EnFont());
    backend_value_ = Label(row, "checking", kSubColor, EnFont());

    row = Row(kHeardY);
    Label(row, "听到", kLabelColor, nullptr);
    Label(row, "Heard", kSubColor, EnFont());
    heard_text_ = Label(row, "-", kValueColor, nullptr);

    row = Row(kReplyY);
    Label(row, "回复", kLabelColor, nullptr);
    Label(row, "Reply", kSubColor, EnFont());
    reply_text_ = Label(row, "-", kValueColor, nullptr);

    for (lv_obj_t* text : {heard_text_, reply_text_}) {
        lv_label_set_long_mode(text, LV_LABEL_LONG_MODE_DOTS);
        lv_obj_set_width(text, kTextWidth);
    }

    BuildNextUp();
    ApplyFonts();
    SetPage(0);

    refresh_timer_ = lv_timer_create(RefreshCb, kRefreshMs, this);
    lv_timer_pause(refresh_timer_);
#if WEIXUE_FACE_DIAG
    lv_timer_create(DiagBackendCb, kRefreshMs, this);
#endif
}

lv_obj_t* MonitorPanel::Row(int32_t y) {
    lv_obj_t* row = lv_obj_create(content_);
    Plain(row);
    Flex(row, LV_FLEX_FLOW_ROW, 6);
    lv_obj_align(row, LV_ALIGN_CENTER, 0, y);
    return row;
}

lv_obj_t* MonitorPanel::Label(lv_obj_t* parent, const char* text, uint32_t color,
                              const lv_font_t* font) {
    lv_obj_t* label = lv_label_create(parent);
    lv_label_set_text(label, text);
    lv_obj_set_style_text_color(label, lv_color_hex(color), 0);
    if (font != nullptr) {
        lv_obj_set_style_text_font(label, font, 0);
    }
    return label;
}

void MonitorPanel::BuildNextUp() {
    lv_obj_t* box = lv_obj_create(content_);
    Plain(box);
    lv_obj_set_size(box, kNextW, kNextH);
    lv_obj_align(box, LV_ALIGN_CENTER, 0, kNextY);
    lv_obj_set_style_radius(box, 40, 0);
    lv_obj_set_style_border_width(box, 2, 0);
    lv_obj_set_style_border_color(box, lv_color_hex(kCardColor), 0);

    lv_obj_t* header = lv_obj_create(box);
    Plain(header);
    Flex(header, LV_FLEX_FLOW_ROW, 6);
    lv_obj_align(header, LV_ALIGN_TOP_MID, 0, 8);
    Label(header, "Next up", kLabelColor, EnFont());
    next_state_ = Label(header, "", kWarnColor, EnFont());

    next_title_ = Label(box, "Nothing needs you", kSubColor, EnFont());
    lv_label_set_long_mode(next_title_, LV_LABEL_LONG_MODE_DOTS);
    lv_obj_set_width(next_title_, kNextW - kButtonW - 56);
    lv_obj_align(next_title_, LV_ALIGN_LEFT_MID, 28, 14);

    next_open_ = lv_obj_create(box);
    lv_obj_remove_style_all(next_open_);
    lv_obj_remove_flag(next_open_, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_size(next_open_, kButtonW, kButtonH);
    lv_obj_align(next_open_, LV_ALIGN_RIGHT_MID, -18, 14);
    lv_obj_set_style_radius(next_open_, 20, 0);
    lv_obj_set_style_border_width(next_open_, 2, 0);
    lv_obj_set_style_border_color(next_open_, lv_color_hex(kOkColor), 0);
    lv_obj_set_style_bg_color(next_open_, lv_color_hex(kOkColor), 0);
    lv_obj_set_style_bg_opa(next_open_, LV_OPA_TRANSP, 0);
    lv_obj_set_style_bg_opa(next_open_, LV_OPA_40, LV_STATE_PRESSED);
    lv_obj_t* open = Label(next_open_, "Open", kOkColor, nullptr);
    lv_obj_center(open);
    lv_obj_add_event_cb(next_open_, OpenCb, LV_EVENT_CLICKED, this);
    lv_obj_add_flag(next_open_, LV_OBJ_FLAG_HIDDEN);
}

void MonitorPanel::SetNextUp(const std::string& title, const std::string& state, uint32_t state_color) {
    bool has = !title.empty();
    if (has == !lv_obj_has_flag(next_open_, LV_OBJ_FLAG_HIDDEN) &&
        strcmp(lv_label_get_text(next_title_), has ? title.c_str() : "Nothing needs you") == 0 &&
        strcmp(lv_label_get_text(next_state_), has ? state.c_str() : "") == 0) {
        return;
    }
    SetText(next_title_, has ? title.c_str() : "Nothing needs you");
    lv_obj_set_style_text_color(next_title_, lv_color_hex(has ? kValueColor : kSubColor), 0);
    SetText(next_state_, has ? state.c_str() : "");
    lv_obj_set_style_text_color(next_state_, lv_color_hex(state_color), 0);
    if (has) {
        lv_obj_remove_flag(next_open_, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(next_open_, LV_OBJ_FLAG_HIDDEN);
    }
}

void MonitorPanel::OpenCb(lv_event_t* e) {
    auto* self = static_cast<MonitorPanel*>(lv_event_get_user_data(e));
    lv_indev_t* indev = lv_indev_active();
    if (indev != nullptr && lv_indev_get_gesture_dir(indev) != LV_DIR_NONE) {
        return;
    }
    if (self->on_open_next_) {
        self->on_open_next_();
    }
}

void MonitorPanel::SetPage(int page) {
    page_ = page == 1 ? 1 : 0;
    if (page_ == 0) {
        lv_obj_remove_flag(page1_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(content_, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(page1_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(content_, LV_OBJ_FLAG_HIDDEN);
    }
    for (int i = 0; i < 2; i++) {
        int32_t r = i == page_ ? 4 : 3;
        lv_obj_set_size(dots_[i], 2 * r, 2 * r);
        lv_obj_set_pos(dots_[i], kDotX - r, kDotY[i] - r);
        lv_obj_set_style_bg_color(dots_[i], lv_color_hex(i == page_ ? kDotOnColor : kSubColor), 0);
    }
    ESP_LOGI(TAG, "page=%d", page_ + 1);
}

void MonitorPanel::ApplyFonts() {
    for (lv_obj_t* text : {heard_text_, reply_text_}) {
        lv_obj_set_height(text, lv_font_get_line_height(lv_obj_get_style_text_font(text, LV_PART_MAIN)));
    }
}

void MonitorPanel::Show(bool show) {
    if (show == shown_) {
        return;
    }
    shown_ = show;
    lv_anim_delete(this, SlideCb);
    if (show) {
        SetPage(0);
        lv_obj_remove_flag(root_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_move_foreground(root_);
        Refresh();
        lv_timer_resume(refresh_timer_);
    } else {
        on_cover_(false);
        lv_timer_pause(refresh_timer_);
    }
    lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_var(&a, this);
    lv_anim_set_exec_cb(&a, SlideCb);
    lv_anim_set_values(&a, lv_obj_get_x(root_), show ? 0 : width_);
    lv_anim_set_duration(&a, kSlideMs);
    lv_anim_set_path_cb(&a, lv_anim_path_ease_out);
    lv_anim_set_completed_cb(&a, SlideDoneCb);
    lv_anim_start(&a);
    ESP_LOGI(TAG, "page=%s", show ? "panel" : "face");
}

void MonitorPanel::SlideCb(void* var, int32_t value) {
    lv_obj_set_x(static_cast<MonitorPanel*>(var)->root_, value);
}

void MonitorPanel::SlideDoneCb(lv_anim_t* anim) {
    auto* self = static_cast<MonitorPanel*>(anim->var);
    if (self->shown_) {
        self->on_cover_(true);
    } else {
        lv_obj_add_flag(self->root_, LV_OBJ_FLAG_HIDDEN);
    }
}

void MonitorPanel::SetText(lv_obj_t* label, const char* text) {
    if (strcmp(lv_label_get_text(label), text) != 0) {
        lv_label_set_text(label, text);
    }
}

void MonitorPanel::SetHeard(const char* text) {
    if (text == nullptr || text[0] == '\0') {
        return;
    }
    SetText(heard_text_, text);
    reply_.Clear();
    SetText(reply_text_, "-");
#if WEIXUE_FACE_DIAG
    ESP_LOGI(TAG, "diag heard=\"%s\" reply=\"\"", text);
#endif
}

void MonitorPanel::AddReply(const char* text, uint32_t reply) {
    if (!reply_.Add(text, reply)) {
        return;
    }
    SetText(reply_text_, reply_.text().c_str());
#if WEIXUE_FACE_DIAG
    ESP_LOGI(TAG, "diag reply=\"%s\" (reply %lu)", reply_.text().c_str(), (unsigned long)reply);
#endif
}

void MonitorPanel::SetShift(int32_t x, int32_t y) {
    for (lv_obj_t* obj : {content_, dots_[0], dots_[1]}) {
        lv_obj_set_style_translate_x(obj, x, 0);
        lv_obj_set_style_translate_y(obj, y, 0);
    }
}

void MonitorPanel::RefreshCb(lv_timer_t* timer) {
    static_cast<MonitorPanel*>(lv_timer_get_user_data(timer))->Refresh();
}

void MonitorPanel::Refresh() {
    char buf[32];
    auto& wifi = WifiManager::GetInstance();
    if (wifi.IsConnected()) {
        int rssi = wifi.GetRssi();
        SetText(wifi_icon_, rssi >= -65   ? MATERIAL_SYMBOLS_WIFI
                            : rssi >= -75 ? MATERIAL_SYMBOLS_WIFI_2_BAR
                                          : MATERIAL_SYMBOLS_WIFI_1_BAR);
        snprintf(buf, sizeof(buf), "%d dBm", rssi);
        SetText(wifi_value_, buf);
    } else {
        SetText(wifi_icon_, MATERIAL_SYMBOLS_WIFI_OFF);
        SetText(wifi_value_, "--");
    }

    int level = 0;
    bool charging = false, discharging = false;
    if (Board::GetInstance().GetBatteryLevel(level, charging, discharging)) {
        SetText(battery_icon_, BatteryIcon(level, charging));
        snprintf(buf, sizeof(buf), "%d%% %s", level, discharging ? "BAT" : "USB");
        SetText(battery_value_, buf);
    }

    auto& app = Application::GetInstance();
    bool muted = app.IsMicMuted();
    SetText(mic_icon_, muted ? MATERIAL_SYMBOLS_MIC_OFF : MATERIAL_SYMBOLS_MIC);
    lv_obj_set_style_text_color(mic_icon_, lv_color_hex(muted ? kWarnColor : kSubColor), 0);

    uint32_t color;
    const char* text = BackendLabel(app.GetBackendStatus(), muted, &color);
    SetText(backend_value_, text);
    lv_obj_set_style_text_color(backend_value_, lv_color_hex(color), 0);

#if WEIXUE_FACE_DIAG
    char values[96];
    snprintf(values, sizeof(values), "wifi=%s battery=%s mic=%s backend=%s",
             lv_label_get_text(wifi_value_), lv_label_get_text(battery_value_), muted ? "muted" : "on",
             text);
    int64_t now = esp_timer_get_time();
    if (strcmp(values, diag_values_) != 0 && now - diag_values_us_ >= kDiagValuesMinMs * 1000) {
        strcpy(diag_values_, values);
        diag_values_us_ = now;
        ESP_LOGI(TAG, "diag values %s", values);
    }
#endif
}

#if WEIXUE_FACE_DIAG
void MonitorPanel::DiagBackendCb(lv_timer_t* timer) {
    auto* self = static_cast<MonitorPanel*>(lv_timer_get_user_data(timer));
    auto& app = Application::GetInstance();
    auto backend = app.GetBackendStatus();
    uint32_t color;
    const char* text = BackendLabel(backend, app.IsMicMuted(), &color);
    if (text != self->diag_backend_) {
        self->diag_backend_ = text;
        ESP_LOGI(TAG, "diag backend=%s poll_status=%d poll_age_ms=%lld", text, backend.poll_status,
                 (long long)backend.poll_age_ms);
    }
}
#endif
