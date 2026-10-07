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
constexpr uint32_t kApproveColor = 0x22B5C4;
constexpr uint32_t kDenyColor = 0xB0585A;

constexpr uint32_t kRefreshMs = 1000;
constexpr uint32_t kRearmMs = 4000;
constexpr uint32_t kSlideMs = 220;
constexpr int64_t kPollFreshMs = 90000;
constexpr size_t kMaxReplyBytes = 240;

// Row centres relative to the screen centre; the round panel narrows towards the top and bottom.
constexpr int32_t kStatusY = -168;
constexpr int32_t kBackendY = -129;
constexpr int32_t kHeardY = -90;
constexpr int32_t kReplyY = -51;
constexpr int32_t kTasksY = -12;
constexpr int32_t kTextWidth = 270;
constexpr int32_t kCardY = 100;
constexpr int32_t kCardW = 300;
constexpr int32_t kCardH = 172;
constexpr int32_t kButtonW = 132;
constexpr int32_t kButtonH = 68;

const char* kCardId = "fixture-1";
const char* kDecisions[2] = {"approve", "deny"};

#if WEIXUE_FACE_DIAG
constexpr int64_t kReplayStartMs = 40000;
constexpr int64_t kReplayPeriodMs = 120000;
constexpr int64_t kDiagValuesMinMs = 5000;

// One replay step of `ms`: kSwipe drags from x0 to x1 on the centre line, kRelease lifts the
// pointer, 0 or 1 presses the centre of the Approve or Deny button.
struct ReplayStep {
    int64_t ms;
    int button;
    int32_t x0, x1;
};
constexpr int kSwipe = -1;
constexpr int kRelease = -2;
constexpr ReplayStep kReplay[] = {
    {300, kSwipe, 400, 66},  {4000, kRelease, 0, 0}, {120, 0, 0, 0},
    {8000, kRelease, 0, 0},  {120, 1, 0, 0},         {8000, kRelease, 0, 0},
    {300, kSwipe, 66, 400},
};
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

// Truncates s to at most max bytes without splitting a UTF-8 sequence.
void TruncateUtf8(std::string& s, size_t max) {
    if (s.size() <= max) {
        return;
    }
    size_t end = max;
    while (end > 0 && (static_cast<uint8_t>(s[end]) & 0xC0) == 0x80) {
        end--;
    }
    s.resize(end);
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

}  // namespace

MonitorPanel::MonitorPanel(lv_obj_t* screen, int32_t width, int32_t height,
                           const lv_font_t* icon_font, std::function<void(bool)> on_cover)
    : width_(width), icon_font_(icon_font), on_cover_(std::move(on_cover)) {
    root_ = lv_obj_create(screen);
    lv_obj_remove_style_all(root_);
    lv_obj_remove_flag(root_, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_size(root_, width, height);
    lv_obj_set_style_bg_color(root_, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(root_, LV_OPA_COVER, 0);
    lv_obj_set_pos(root_, width, 0);
    lv_obj_add_flag(root_, LV_OBJ_FLAG_HIDDEN);

    content_ = lv_obj_create(root_);
    Plain(content_);
    lv_obj_set_size(content_, width, height);

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

    row = Row(kTasksY);
    Label(row, "任务", kLabelColor, nullptr);
    Label(row, "Tasks", kSubColor, EnFont());
    Chip(row, "日落", "Sunset 2/3");
    Chip(row, nullptr, "+1");

    BuildCard();
    ApplyFonts();

    refresh_timer_ = lv_timer_create(RefreshCb, kRefreshMs, this);
    lv_timer_pause(refresh_timer_);
    rearm_timer_ = lv_timer_create(RearmCb, kRearmMs, this);
    lv_timer_pause(rearm_timer_);
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

lv_obj_t* MonitorPanel::Chip(lv_obj_t* parent, const char* zh, const char* en) {
    lv_obj_t* chip = lv_obj_create(parent);
    Plain(chip);
    Flex(chip, LV_FLEX_FLOW_ROW, 4);
    lv_obj_set_style_radius(chip, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_border_width(chip, 1, 0);
    lv_obj_set_style_border_color(chip, lv_color_hex(kCardColor), 0);
    lv_obj_set_style_pad_hor(chip, 12, 0);
    lv_obj_set_style_pad_ver(chip, 2, 0);
    if (zh != nullptr) {
        Label(chip, zh, kValueColor, nullptr);
    }
    Label(chip, en, kValueColor, EnFont());
    return chip;
}

lv_obj_t* MonitorPanel::Button(lv_obj_t* parent, const char* zh, const char* en, uint32_t color) {
    lv_obj_t* button = lv_obj_create(parent);
    lv_obj_remove_style_all(button);
    lv_obj_remove_flag(button, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_size(button, kButtonW, kButtonH);
    lv_obj_set_flex_flow(button, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(button, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_radius(button, 20, 0);
    lv_obj_set_style_border_width(button, 2, 0);
    lv_obj_set_style_border_color(button, lv_color_hex(color), 0);
    lv_obj_set_style_bg_color(button, lv_color_hex(color), 0);
    lv_obj_set_style_bg_opa(button, LV_OPA_TRANSP, 0);
    lv_obj_set_style_bg_opa(button, LV_OPA_40, LV_STATE_PRESSED);
    Label(button, zh, color, nullptr);
    Label(button, en, color, EnFont());
    lv_obj_add_event_cb(button, ButtonCb, LV_EVENT_CLICKED, this);
    return button;
}

void MonitorPanel::BuildCard() {
    card_ = lv_obj_create(content_);
    Plain(card_);
    lv_obj_set_size(card_, kCardW, kCardH);
    lv_obj_align(card_, LV_ALIGN_CENTER, 0, kCardY);
    lv_obj_set_style_radius(card_, 40, 0);
    lv_obj_set_style_border_width(card_, 2, 0);
    lv_obj_set_style_border_color(card_, lv_color_hex(kCardColor), 0);

    lv_obj_t* header = lv_obj_create(card_);
    Plain(header);
    Flex(header, LV_FLEX_FLOW_ROW, 6);
    lv_obj_align(header, LV_ALIGN_TOP_MID, 0, 6);
    Label(header, "待批", kLabelColor, nullptr);
    Label(header, "Approval", kSubColor, EnFont());
    card_status_ = Label(header, "fixture", kSubColor, EnFont());

    lv_obj_t* text = lv_obj_create(card_);
    Plain(text);
    Flex(text, LV_FLEX_FLOW_ROW, 6);
    lv_obj_align(text, LV_ALIGN_TOP_MID, 0, 42);
    Label(text, "开客厅灯", kValueColor, nullptr);
    Label(text, "Living room on", kSubColor, EnFont());

    buttons_[0] = Button(card_, "批准", "Approve", kApproveColor);
    buttons_[1] = Button(card_, "拒绝", "Deny", kDenyColor);
    lv_obj_align(buttons_[0], LV_ALIGN_BOTTOM_MID, -(kButtonW / 2 + 6), -10);
    lv_obj_align(buttons_[1], LV_ALIGN_BOTTOM_MID, kButtonW / 2 + 6, -10);
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
    reply_.clear();
    SetText(reply_text_, "-");
}

void MonitorPanel::AddReply(const char* text) {
    if (text == nullptr || text[0] == '\0') {
        return;
    }
    if (!reply_.empty()) {
        reply_ += ' ';
    }
    reply_ += text;
    TruncateUtf8(reply_, kMaxReplyBytes);
    SetText(reply_text_, reply_.c_str());
}

void MonitorPanel::SetShift(int32_t x, int32_t y) {
    lv_obj_set_style_translate_x(content_, x, 0);
    lv_obj_set_style_translate_y(content_, y, 0);
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

    auto backend = app.GetBackendStatus();
    const char* text;
    uint32_t color;
    bool reachable = backend.poll_status == 200 || backend.poll_status == 204;
    if (backend.channel_open) {
        text = "session";
        color = kOkColor;
    } else if (muted) {
        text = "paused";
        color = kWarnColor;
    } else if (backend.poll_age_ms < 0) {
        text = "checking";
        color = kSubColor;
    } else if (reachable && backend.poll_age_ms < kPollFreshMs) {
        text = "online";
        color = kOkColor;
    } else {
        text = "offline";
        color = kBadColor;
    }
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

void MonitorPanel::SetCardState(CardState state) {
    card_state_ = state;
    char buf[32];
    const char* text = buf;
    uint32_t color = kSubColor;
    switch (state) {
        case CardState::kPending:
            text = "fixture";
            break;
        case CardState::kSending:
            text = "sending";
            color = kValueColor;
            break;
        case CardState::kSent:
            snprintf(buf, sizeof(buf), "%s sent", kDecisions[decision_]);
            color = kOkColor;
            break;
        case CardState::kFailed:
            snprintf(buf, sizeof(buf), "%s not sent", kDecisions[decision_]);
            color = kBadColor;
            break;
    }
    SetText(card_status_, text);
    lv_obj_set_style_text_color(card_status_, lv_color_hex(color), 0);
}

void MonitorPanel::ButtonCb(lv_event_t* e) {
    auto* self = static_cast<MonitorPanel*>(lv_event_get_user_data(e));
    lv_indev_t* indev = lv_indev_active();
    if (indev != nullptr && lv_indev_get_gesture_dir(indev) != LV_DIR_NONE) {
        return;
    }
    lv_obj_t* target = lv_event_get_target_obj(e);
    self->OnDecision(target == self->buttons_[0] ? 0 : 1);
}

void MonitorPanel::OnDecision(int button) {
    if (card_state_ == CardState::kSending) {
        return;
    }
    uint32_t seq = ++event_seq_;
    char payload[256];
    snprintf(payload, sizeof(payload),
             "{\"jsonrpc\":\"2.0\",\"method\":\"notifications/weixue/panel\",\"params\":"
             "{\"event\":\"approval_decision\",\"card_id\":\"%s\",\"decision\":\"%s\","
             "\"fixture\":true,\"seq\":%lu}}",
             kCardId, kDecisions[button], (unsigned long)seq);
    ESP_LOGI(TAG, "card %s %s (seq %lu)", kCardId, kDecisions[button], (unsigned long)seq);
    decision_ = button;
    SetCardState(CardState::kSending);
    lv_timer_pause(rearm_timer_);
    Application::GetInstance().SendDeviceEvent(payload, [this](bool sent) {
        DisplayLockGuard lock(Board::GetInstance().GetDisplay());
        SetCardState(sent ? CardState::kSent : CardState::kFailed);
        lv_timer_reset(rearm_timer_);
        lv_timer_resume(rearm_timer_);
    });
}

void MonitorPanel::RearmCb(lv_timer_t* timer) {
    auto* self = static_cast<MonitorPanel*>(lv_timer_get_user_data(timer));
    lv_timer_pause(timer);
    self->SetCardState(CardState::kPending);
}

#if WEIXUE_FACE_DIAG
void MonitorPanel::StartTouchReplay() {
    replay_start_us_ = esp_timer_get_time() + kReplayStartMs * 1000;
    replay_indev_ = lv_indev_create();
    lv_indev_set_type(replay_indev_, LV_INDEV_TYPE_POINTER);
    lv_indev_set_read_cb(replay_indev_, ReplayReadCb);
    lv_indev_set_user_data(replay_indev_, this);
    lv_indev_set_display(replay_indev_, lv_obj_get_display(root_));
}

void MonitorPanel::ReplayReadCb(lv_indev_t* indev, lv_indev_data_t* data) {
    static lv_point_t last = {0, 0};
    auto* self = static_cast<MonitorPanel*>(lv_indev_get_user_data(indev));
    data->point = last;
    data->state = LV_INDEV_STATE_RELEASED;
    int64_t t = (esp_timer_get_time() - self->replay_start_us_) / 1000;
    if (t < 0) {
        return;
    }
    t %= kReplayPeriodMs;
    const int32_t mid = lv_obj_get_height(self->root_) / 2;
    for (const ReplayStep& step : kReplay) {
        if (t >= step.ms) {
            t -= step.ms;
            continue;
        }
        if (step.button == kSwipe) {
            data->point.x = step.x0 + (step.x1 - step.x0) * t / step.ms;
            data->point.y = mid;
            data->state = LV_INDEV_STATE_PRESSED;
        } else if (step.button >= 0) {
            lv_area_t area;
            lv_obj_get_coords(self->buttons_[step.button], &area);
            data->point.x = (area.x1 + area.x2) / 2;
            data->point.y = (area.y1 + area.y2) / 2;
            data->state = LV_INDEV_STATE_PRESSED;
        }
        last = data->point;
        return;
    }
}
#endif
