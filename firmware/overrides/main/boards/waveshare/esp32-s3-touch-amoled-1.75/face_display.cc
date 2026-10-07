#include "face_display.h"

#include "application.h"
#include "assets/lang_config.h"
#include "lvgl_theme.h"
#include "monitor_panel.h"
#include "settings.h"

#include <esp_heap_caps.h>
#include <esp_log.h>
#include <esp_random.h>
#include <esp_timer.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <ctime>

#if WEIXUE_FACE_DIAG
#include <freertos/task.h>
#include <mbedtls/base64.h>
#include "lvgl_private.h"
#include <cstdio>
#endif

#define TAG "FaceDisplay"

namespace {

constexpr uint32_t kEyeColor = 0x00E0FF;
constexpr uint32_t kCaptionColor = 0x9FB4BC;
constexpr uint32_t kChromeColor = 0x6E8A92;
constexpr uint32_t kThinkTailColor = 0x3048FF;
constexpr uint32_t kErrorColor = 0xFF2A1A;
constexpr uint32_t kBedsideClockColor = 0x701000;
constexpr uint32_t kBedsideEyeColor = 0x5A0C00;
constexpr uint32_t kBedsideRingColor = 0x801400;

constexpr uint32_t kTickMs = 33;
constexpr uint32_t kRestTickMs = 100;
constexpr float kEyeSpacing = 80.0f;
constexpr float kFaceY = -20.0f;
constexpr int kCaptionWidth = 340;
constexpr int kCaptionY = 128;
constexpr float kShapeTau = 0.09f;
constexpr float kGazeTau = 0.22f;
constexpr int64_t kThinkTimeoutUs = 20LL * 1000 * 1000;
constexpr int64_t kShiftPeriodUs = 60LL * 1000 * 1000;
constexpr uint32_t kPanelReturnMs = 60000;
constexpr int8_t kShiftPath[8][2] = {{0, 0},  {1, 0},  {1, 1},   {0, 1},
                                     {-1, 1}, {-1, 0}, {-1, -1}, {0, -1}};
constexpr float kPi = 3.14159265f;

// Ring: 9 px is 2% of the 466 px diameter; the outer edge sits kRingInset inside the panel edge.
constexpr int kRingWidth = 9;
constexpr int kRingInset = 1;
constexpr int kRingPad = 2;
constexpr int64_t kErrorFlashUs = 1800LL * 1000;
constexpr int64_t kErrorFlashPeriodUs = 600LL * 1000;
constexpr float kListenPulseHz = 0.8f;
constexpr float kSpinDegPerS = 180.0f;
constexpr int64_t kSolidFrameUs = 100LL * 1000;
constexpr int64_t kSpinFrameUs = 66LL * 1000;
constexpr float kLevelAttackTau = 0.03f;
constexpr float kLevelReleaseTau = 0.15f;
constexpr float kBedsideRingOpa = 0.55f;

// Bedside page: seven-segment HH:MM clock above two closed-eye lines.
constexpr int64_t kBedsideResumeUs = 15LL * 1000 * 1000;
constexpr int kBedsideShiftScale = 6;
constexpr float kBedsideEyesY = 96.0f;
constexpr int kClockY = -26;
constexpr int kDigitW = 60;
constexpr int kDigitH = 112;
constexpr int kSegT = 11;
constexpr int kDigitGap = 18;
constexpr int kColonGap = 22;
constexpr int kColonDot = 12;
constexpr int kClockW = 4 * kDigitW + 2 * kDigitGap + 2 * kColonGap + kColonDot;
// Segments a-g as bits 0-6.
constexpr uint8_t kDigitSegments[10] = {0x3F, 0x06, 0x5B, 0x4F, 0x66, 0x6D, 0x7D, 0x07, 0x7F, 0x6F};
constexpr uint8_t kDashSegments = 0x40;
constexpr time_t kClockValidAfter = 1735689600;  // 2025-01-01

#if WEIXUE_FACE_DIAG
// One encoded screen dump handed from the LVGL task to the diag print task.
struct DiagDumpMsg {
    uint32_t id, w, h, lit;
    const char* mode;
    const char* mood;
    uint8_t* data;
    size_t size;
};
constexpr int kDiagQueueLen = 4;
constexpr size_t kDiagChunkBytes = 72;
constexpr uint32_t kDiagLineGapMs = 20;
#endif

float Rand01() { return esp_random() / 4294967295.0f; }
float RandRange(float lo, float hi) { return lo + (hi - lo) * Rand01(); }
int64_t RandUs(float lo_s, float hi_s) { return static_cast<int64_t>(RandRange(lo_s, hi_s) * 1e6f); }

// Seconds within a 1000 s window; the 3.3, 5.1 and 1.2 Hz waves complete whole cycles in it.
float WaveTime(int64_t now_us) { return (now_us % 1000000000LL) / 1e6f; }

float Approach(float cur, float target, float dt_s, float tau_s) {
    return cur + (target - cur) * (1.0f - expf(-dt_s / tau_s));
}

// Blink lid closure (0 open, 1 closed) at t seconds into one blink; negative once finished.
float BlinkCurve(float t, float close_s, float hold_s, float open_s) {
    if (t < close_s) return t / close_s;
    if (t < close_s + hold_s) return 1.0f;
    if (t < close_s + hold_s + open_s) return 1.0f - (t - close_s - hold_s) / open_s;
    return -1.0f;
}

}  // namespace

bool FaceDisplay::EyeBox::operator==(const EyeBox& o) const {
    return x == o.x && y == o.y && w == o.w && h == o.h && r == o.r && lid_left == o.lid_left &&
           lid_right == o.lid_right && cut == o.cut;
}

FaceDisplay::FaceDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel,
                         int width, int height, int offset_x, int offset_y, bool mirror_x,
                         bool mirror_y, bool swap_xy)
    : SpiLcdDisplay(panel_io, panel, width, height, offset_x, offset_y, mirror_x, mirror_y,
                    swap_xy),
      eye_color_(kEyeColor),
      bedside_schedule_(-1, -1) {
    auto dark = LvglThemeManager::GetInstance().GetTheme("dark");
    if (dark != nullptr) {
        current_theme_ = dark;
    }
    Settings settings("bedside", false);
    std::string start = settings.GetString("start", CONFIG_WEIXUE_BEDSIDE_START);
    std::string end = settings.GetString("end", CONFIG_WEIXUE_BEDSIDE_END);
    bedside_schedule_.SetWindow(BedsideSchedule::ParseClock(start.c_str()),
                                BedsideSchedule::ParseClock(end.c_str()));
    ESP_LOGI(TAG, "bedside window %s-%s (%d-%d min)", start.c_str(), end.c_str(),
             bedside_schedule_.start(), bedside_schedule_.end());
}

FaceDisplay::~FaceDisplay() {
    if (timer_ != nullptr) {
        lv_timer_delete(timer_);
    }
    heap_caps_free(ring_mask_);
#if WEIXUE_FACE_DIAG
    heap_caps_free(diag_buf_);
#endif
}

FaceDisplay::Mood FaceDisplay::MoodFromEmotion(const char* emotion) {
    static const struct {
        const char* name;
        Mood mood;
    } kTable[] = {
        {"happy", Mood::kHappy},         {"funny", Mood::kHappy},
        {"laughing", Mood::kHappy},      {"loving", Mood::kHappy},
        {"relaxed", Mood::kHappy},       {"silly", Mood::kHappy},
        {"winking", Mood::kHappy},       {"cool", Mood::kHappy},
        {"delicious", Mood::kHappy},     {"kissy", Mood::kHappy},
        {"confident", Mood::kHappy},     {"sad", Mood::kSad},
        {"crying", Mood::kSad},          {"angry", Mood::kSad},
        {"surprised", Mood::kSurprised}, {"shocked", Mood::kSurprised},
        {"sleepy", Mood::kSleepy},       {"confused", Mood::kConfused},
        {"thinking", Mood::kConfused},   {"embarrassed", Mood::kConfused},
        {"warning", Mood::kConfused},    {"cancel", Mood::kConfused},
        {"cloud_off", Mood::kConfused},
    };
    if (emotion != nullptr) {
        for (const auto& entry : kTable) {
            if (strcmp(emotion, entry.name) == 0) {
                return entry.mood;
            }
        }
    }
    return Mood::kNeutral;
}

const char* FaceDisplay::ModeName(Mode mode) {
    switch (mode) {
        case Mode::kIdle: return "idle";
        case Mode::kListening: return "listening";
        case Mode::kThinking: return "thinking";
        case Mode::kSpeaking: return "speaking";
    }
    return "?";
}

const char* FaceDisplay::MoodName(Mood mood) {
    switch (mood) {
        case Mood::kNeutral: return "neutral";
        case Mood::kHappy: return "happy";
        case Mood::kSad: return "sad";
        case Mood::kSurprised: return "surprised";
        case Mood::kSleepy: return "sleepy";
        case Mood::kConfused: return "confused";
    }
    return "?";
}

void FaceDisplay::SetupUI() {
    if (face_root_ != nullptr) {
        return;
    }
    SpiLcdDisplay::SetupUI();

    DisplayLockGuard lock(this);
    lv_obj_t* screen = lv_screen_active();

    lv_label_set_text(emoji_label_, "");
    lv_obj_add_flag(emoji_box_, LV_OBJ_FLAG_HIDDEN);

    face_root_ = lv_obj_create(screen);
    lv_obj_remove_style_all(face_root_);
    lv_obj_set_size(face_root_, width_, height_);
    lv_obj_remove_flag(face_root_, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_remove_flag(face_root_, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_move_to_index(face_root_, 1);

    for (int i = 0; i < 2; i++) {
        eyes_[i] = lv_obj_create(face_root_);
        lv_obj_remove_style_all(eyes_[i]);
        lv_obj_remove_flag(eyes_[i], LV_OBJ_FLAG_CLICKABLE);
        lv_obj_remove_flag(eyes_[i], LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_event_cb(eyes_[i], EyeDrawCb, LV_EVENT_DRAW_MAIN, this);
    }
    for (int i = 0; i < 3; i++) {
        dots_[i] = lv_obj_create(face_root_);
        lv_obj_remove_style_all(dots_[i]);
        lv_obj_remove_flag(dots_[i], LV_OBJ_FLAG_CLICKABLE);
        lv_obj_remove_flag(dots_[i], LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_style_radius(dots_[i], LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_color(dots_[i], lv_color_hex(kEyeColor), 0);
        lv_obj_set_style_bg_opa(dots_[i], LV_OPA_TRANSP, 0);
        lv_obj_set_size(dots_[i], 10 + 4 * i, 10 + 4 * i);
        lv_obj_add_flag(dots_[i], LV_OBJ_FLAG_HIDDEN);
    }

    clock_ = lv_obj_create(face_root_);
    lv_obj_remove_style_all(clock_);
    lv_obj_remove_flag(clock_, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_remove_flag(clock_, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_size(clock_, kClockW, kDigitH);
    lv_obj_add_event_cb(clock_, ClockDrawCb, LV_EVENT_DRAW_MAIN, this);
    lv_obj_add_flag(clock_, LV_OBJ_FLAG_HIDDEN);

    ring_obj_ = lv_obj_create(screen);
    lv_obj_remove_style_all(ring_obj_);
    lv_obj_set_size(ring_obj_, width_, height_);
    lv_obj_remove_flag(ring_obj_, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_remove_flag(ring_obj_, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_event_cb(ring_obj_, RingDrawCb, LV_EVENT_DRAW_MAIN, this);
    BuildRingMask();

    StyleChrome();

    auto* theme = static_cast<LvglTheme*>(current_theme_);
    panel_ = new MonitorPanel(screen, width_, height_, theme->icon_font()->font(), [this](bool covered) {
        if (covered) {
            lv_obj_add_flag(face_root_, LV_OBJ_FLAG_HIDDEN);
        } else {
            lv_obj_remove_flag(face_root_, LV_OBJ_FLAG_HIDDEN);
        }
    });
    lv_obj_add_event_cb(screen, GestureCb, LV_EVENT_GESTURE, this);
#if WEIXUE_FACE_DIAG
    panel_->StartTouchReplay();
#endif

    EyeShape initial[2];
    TargetShapes(initial, false);
    cur_[0] = initial[0];
    cur_[1] = initial[1];

    int64_t now = esp_timer_get_time();
    last_tick_us_ = now;
    next_blink_us_ = now + RandUs(3.0f, 6.0f);
    next_gaze_us_ = now + RandUs(1.8f, 5.5f);
    last_shift_us_ = now;
    timer_ = lv_timer_create(TickCb, kTickMs, this);

#if WEIXUE_FACE_DIAG
    diag_window_us_ = now;
    diag_queue_ = xQueueCreate(kDiagQueueLen, sizeof(DiagDumpMsg));
    xTaskCreate(DiagPrintTask, "face_diag", 4096, this, 1, nullptr);
    lv_display_add_event_cb(display_, DiagRefrCb, LV_EVENT_REFR_START, this);
    lv_display_add_event_cb(display_, DiagRefrCb, LV_EVENT_RENDER_START, this);
    lv_display_add_event_cb(display_, DiagRefrCb, LV_EVENT_REFR_READY, this);
#endif
}

void FaceDisplay::StyleChrome() {
    lv_obj_set_style_bg_image_src(container_, nullptr, 0);
    lv_obj_set_style_bg_color(container_, lv_color_black(), 0);
    lv_obj_set_style_bg_color(lv_screen_active(), lv_color_black(), 0);
    // The pixel shift moves bars 1 px past the screen edge; that must not make it scroll.
    lv_obj_remove_flag(lv_screen_active(), LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_scrollbar_mode(lv_screen_active(), LV_SCROLLBAR_MODE_OFF);

    lv_obj_t* chrome[] = {network_label_, mute_label_, battery_label_, status_label_,
                          notification_label_};
    for (lv_obj_t* obj : chrome) {
        lv_obj_set_style_text_color(obj, lv_color_hex(kChromeColor), 0);
    }

    lv_obj_set_style_bg_opa(bottom_bar_, LV_OPA_TRANSP, 0);
    lv_obj_set_width(bottom_bar_, kCaptionWidth);
    lv_obj_align(bottom_bar_, LV_ALIGN_CENTER, 0, kCaptionY);
    lv_obj_set_width(chat_message_label_, kCaptionWidth - 2 * lv_obj_get_style_pad_left(bottom_bar_, LV_PART_MAIN));
    lv_obj_set_style_text_color(chat_message_label_, lv_color_hex(kCaptionColor), 0);
}

void FaceDisplay::SetTheme(Theme* theme) {
    auto dark = LvglThemeManager::GetInstance().GetTheme("dark");
    SpiLcdDisplay::SetTheme(dark != nullptr ? dark : theme);
    if (face_root_ != nullptr) {
        DisplayLockGuard lock(this);
        StyleChrome();
        panel_->ApplyFonts();
    }
}

void FaceDisplay::SetStatus(const char* status) {
    SpiLcdDisplay::SetStatus(status);
    if (status != nullptr && strcmp(status, Lang::Strings::ERROR) == 0) {
        DisplayLockGuard lock(this);
        error_until_us_ = esp_timer_get_time() + kErrorFlashUs;
    }
}

bool FaceDisplay::SetBedsideHint(const char* hint) {
    BedsideSchedule::Hint h;
    if (hint != nullptr && strcmp(hint, "night") == 0) {
        h = BedsideSchedule::Hint::kNight;
    } else if (hint != nullptr && strcmp(hint, "day") == 0) {
        h = BedsideSchedule::Hint::kDay;
    } else if (hint != nullptr && strcmp(hint, "auto") == 0) {
        h = BedsideSchedule::Hint::kAuto;
    } else {
        return false;
    }
    DisplayLockGuard lock(this);
    bedside_schedule_.SetHint(h, local_minute_, esp_timer_get_time() / 1000000);
    ESP_LOGI(TAG, "bedside hint=%s local=%s", hint, clock_text_);
    return true;
}

bool FaceDisplay::SetBedsideWindow(const char* start, const char* end) {
    int s = BedsideSchedule::ParseClock(start), e = BedsideSchedule::ParseClock(end);
    if (s < 0 || e < 0) {
        return false;
    }
    {
        Settings settings("bedside", true);
        settings.SetString("start", start);
        settings.SetString("end", end);
    }
    DisplayLockGuard lock(this);
    bedside_schedule_.SetWindow(s, e);
    ESP_LOGI(TAG, "bedside window %s-%s local=%s", start, end, clock_text_);
    return true;
}

void FaceDisplay::OnTouch() {
    touch_us_ = esp_timer_get_time();
}

std::string FaceDisplay::BedsideStateJson() {
    DisplayLockGuard lock(this);
    static const char* kHints[] = {"auto", "night", "day"};
    char buf[200];
    char window[24];
    bedside_schedule_.WindowText(window, sizeof(window));
    bool night = bedside_schedule_.IsNight(local_minute_, esp_timer_get_time() / 1000000);
    char local[10] = "null";
    if (local_minute_ >= 0) {
        snprintf(local, sizeof(local), "\"%s\"", clock_text_);
    }
    snprintf(buf, sizeof(buf),
             "{\"night\":%s,\"showing\":%s,\"hint\":\"%s\",\"window\":\"%s\",\"local_time\":%s}",
             night ? "true" : "false", bedside_ ? "true" : "false",
             kHints[static_cast<int>(bedside_schedule_.hint())], window, local);
    return buf;
}

void FaceDisplay::GestureCb(lv_event_t* e) {
    auto* self = static_cast<FaceDisplay*>(lv_event_get_user_data(e));
    lv_dir_t dir = lv_indev_get_gesture_dir(lv_indev_active());
    if (dir == LV_DIR_LEFT) {
        self->ShowPanel(true);
    } else if (dir == LV_DIR_RIGHT) {
        self->ShowPanel(false);
    }
}

void FaceDisplay::ShowPanel(bool show) {
    if (show == panel_->IsShown()) {
        return;
    }
    panel_->Show(show);
#if WEIXUE_FACE_DIAG
    if (show) {
        diag_dump_due_us_ = esp_timer_get_time() + 800 * 1000;
    }
#endif
}

void FaceDisplay::SetEmotion(const char* emotion) {
    DisplayLockGuard lock(this);
    mood_ = MoodFromEmotion(emotion);
}

void FaceDisplay::SetChatMessage(const char* role, const char* content) {
    if (role != nullptr && strcmp(role, "assistant") == 0) {
        DisplayLockGuard lock(this);
        user_turn_ = false;
        if (panel_ != nullptr) {
            panel_->AddReply(content);
        }
    }
    if (role != nullptr && strcmp(role, "user") == 0) {
        {
            DisplayLockGuard lock(this);
            if (content != nullptr && content[0] != '\0') {
                user_turn_ = true;
                user_turn_us_ = esp_timer_get_time();
            }
            if (panel_ != nullptr) {
                panel_->SetHeard(content);
            }
        }
        SpiLcdDisplay::SetChatMessage(role, "");
        return;
    }
    SpiLcdDisplay::SetChatMessage(role, content);
}

void FaceDisplay::TickCb(lv_timer_t* timer) {
    static_cast<FaceDisplay*>(lv_timer_get_user_data(timer))->Tick();
}

// Thinking: from the user text (stt) until the first assistant sentence, in listening or speaking.
FaceDisplay::Mode FaceDisplay::ModeFromState(DeviceState state, int64_t now_us) const {
    bool thinking = user_turn_ && now_us - user_turn_us_ < kThinkTimeoutUs;
    switch (state) {
        case kDeviceStateConnecting:
            return Mode::kListening;
        case kDeviceStateListening:
            return thinking ? Mode::kThinking : Mode::kListening;
        case kDeviceStateSpeaking:
            return thinking ? Mode::kThinking : Mode::kSpeaking;
        case kDeviceStateNotifying:
            return Mode::kSpeaking;
        default:
            return Mode::kIdle;
    }
}

void FaceDisplay::Tick() {
    int64_t now = esp_timer_get_time();
    float dt = std::clamp((now - last_tick_us_) / 1e6f, 0.001f, 0.1f);
    last_tick_us_ = now;

    auto& app = Application::GetInstance();
    DeviceState state = app.GetDeviceState();
    if (state != last_state_) {
        if (state == kDeviceStateIdle) {
            user_turn_ = false;
        } else if (last_state_ == kDeviceStateIdle) {
            ShowPanel(false);
        }
        last_state_ = state;
    }
    if (panel_->IsShown() && lv_display_get_inactive_time(display_) > kPanelReturnMs) {
        ShowPanel(false);
    }

    UpdateClock(now);
    UpdateBedside(now, state);

    Mode mode = ModeFromState(state, now);
    if (mode != mode_ || mood_ != shown_mood_) {
        mode_ = mode;
        shown_mood_ = mood_;
        next_gaze_us_ = now;
        ESP_LOGI(TAG, "mode=%s mood=%s", ModeName(mode_), MoodName(shown_mood_));
#if WEIXUE_FACE_DIAG
        diag_pose_since_us_ = now;
        if ((diag_dumped_poses_ & DiagPoseBit()) == 0) {
            diag_dump_due_us_ = now + 800 * 1000;
        }
#endif
    }

    UpdateGaze(now, dt);
    UpdateBlink(now);
    UpdateShift(now);

    bool voice = app.IsVoiceDetected();
#if WEIXUE_FACE_DIAG
    if (voice != diag_voice_) {
        diag_voice_ = voice;
        diag_voice_flips_++;
    }
#endif
    EyeShape target[2];
    TargetShapes(target, mode_ == Mode::kListening && voice);

    float t = WaveTime(now);
    float talk = mode_ == Mode::kSpeaking
                     ? 0.5f + 0.25f * sinf(2 * kPi * 3.3f * t) + 0.25f * sinf(2 * kPi * 5.1f * t + 1.3f)
                     : 0.0f;
    bool changed = false;
    for (int i = 0; i < 2; i++) {
        EyeShape& c = cur_[i];
        const EyeShape& g = target[i];
        c.x = Approach(c.x, g.x, dt, kShapeTau);
        c.y = Approach(c.y, g.y, dt, kShapeTau);
        c.w = Approach(c.w, g.w, dt, kShapeTau);
        c.h = Approach(c.h, g.h, dt, kShapeTau);
        c.r = Approach(c.r, g.r, dt, kShapeTau);
        c.lid_out = Approach(c.lid_out, g.lid_out, dt, kShapeTau);
        c.lid_in = Approach(c.lid_in, g.lid_in, dt, kShapeTau);
        c.cut = Approach(c.cut, g.cut, dt, kShapeTau);

        EyeShape s = c;
        float persp = 1.0f + 0.0025f * gaze_x_ * (i == 0 ? -1.0f : 1.0f);
        s.x += gaze_x_;
        s.y += gaze_y_;
        s.w *= persp;
        s.h *= persp * (1.0f - 0.08f * talk);
        s.y += s.h * 0.12f * blink_;
        s.h = std::max(6.0f, s.h * (1.0f - blink_));
        s.w *= 1.0f + 0.05f * blink_;
        s.x += ShiftX();
        s.y += ShiftY();
        changed |= ApplyEye(i, s);
    }
    UpdateDots(now);
    bool ring_on = UpdateRing(now, dt, state);

    bool resting = !changed && !ring_on && blink_start_us_ == 0 &&
                   (mode_ == Mode::kIdle || mode_ == Mode::kListening);
    lv_timer_set_period(timer_, resting ? kRestTickMs : kTickMs);

#if WEIXUE_FACE_DIAG
    DiagTick(now);
#endif
}

void FaceDisplay::TargetShapes(EyeShape out[2], bool voice) const {
    if (bedside_) {
        for (int i = 0; i < 2; i++) {
            out[i] = {(i == 0 ? -1.0f : 1.0f) * kEyeSpacing, kBedsideEyesY, 84, 6, 3, 0, 0, 0};
        }
        return;
    }
    EyeShape base = {0, 0, 100, 116, 30, 0, 0, 0};
    switch (mode_) {
        case Mode::kIdle:
            break;
        case Mode::kListening:
            base.w = 112;
            base.h = voice ? 156 : 146;
            base.r = 36;
            break;
        case Mode::kThinking:
            base.w = 108;
            base.h = 54;
            base.r = 22;
            base.y = -24;
            break;
        case Mode::kSpeaking:
            base.w = 106;
            base.h = 122;
            base.r = 34;
            break;
    }
    for (int i = 0; i < 2; i++) {
        out[i] = base;
        out[i].x = (i == 0 ? -1.0f : 1.0f) * kEyeSpacing;
        out[i].y += kFaceY;
    }
    if (mode_ == Mode::kThinking) {
        return;
    }
    switch (shown_mood_) {
        case Mood::kNeutral:
            break;
        case Mood::kHappy:
            for (int i = 0; i < 2; i++) {
                out[i].cut = 0.5f;
                out[i].y -= 4;
            }
            break;
        case Mood::kSad:
            for (int i = 0; i < 2; i++) {
                out[i].lid_out = 0.42f;
                out[i].lid_in = 0.0f;
                out[i].h *= 0.92f;
                out[i].y += 10;
            }
            break;
        case Mood::kSurprised:
            for (int i = 0; i < 2; i++) {
                out[i].w *= 1.08f;
                out[i].h *= 1.22f;
                out[i].r = std::min(out[i].w, out[i].h) * 0.45f;
            }
            break;
        case Mood::kSleepy:
            for (int i = 0; i < 2; i++) {
                out[i].lid_out = 0.55f;
                out[i].lid_in = 0.55f;
                out[i].y += 14;
            }
            break;
        case Mood::kConfused:
            out[0].h *= 0.7f;
            out[0].lid_in = 0.3f;
            out[1].h *= 1.08f;
            out[1].y -= 6;
            break;
    }
}

void FaceDisplay::UpdateGaze(int64_t now, float dt) {
    if (bedside_) {
        gaze_tx_ = 0;
        gaze_ty_ = 0;
        next_gaze_us_ = now;
    } else if (now >= next_gaze_us_) {
        switch (mode_) {
            case Mode::kIdle:
                if (Rand01() < 0.35f) {
                    gaze_tx_ = 0;
                    gaze_ty_ = 0;
                } else {
                    gaze_tx_ = RandRange(-22, 22);
                    gaze_ty_ = RandRange(-12, 10);
                }
                next_gaze_us_ = now + RandUs(1.8f, 5.5f);
                break;
            case Mode::kListening:
                gaze_tx_ = RandRange(-4, 4);
                gaze_ty_ = RandRange(-3, 3);
                next_gaze_us_ = now + RandUs(1.5f, 3.5f);
                break;
            case Mode::kThinking:
                gaze_tx_ = 18 + RandRange(-3, 3);
                gaze_ty_ = -10 + RandRange(-2, 2);
                next_gaze_us_ = now + RandUs(0.8f, 1.6f);
                break;
            case Mode::kSpeaking:
                gaze_tx_ = RandRange(-8, 8);
                gaze_ty_ = RandRange(-5, 4);
                next_gaze_us_ = now + RandUs(1.2f, 3.0f);
                break;
        }
#if WEIXUE_FACE_DIAG
        ESP_LOGI(TAG, "diag gaze=%d,%d mode=%s", (int)gaze_tx_, (int)gaze_ty_, ModeName(mode_));
#endif
    }
    gaze_x_ = Approach(gaze_x_, gaze_tx_, dt, kGazeTau);
    gaze_y_ = Approach(gaze_y_, gaze_ty_, dt, kGazeTau);
}

void FaceDisplay::UpdateBlink(int64_t now) {
    if (mode_ == Mode::kThinking || bedside_) {
        blink_ = 0;
        blink_start_us_ = 0;
        return;
    }
    if (blink_start_us_ == 0) {
        if (now < next_blink_us_) {
            return;
        }
        blink_start_us_ = now;
        blink_double_ = Rand01() < 0.18f;
#if WEIXUE_FACE_DIAG
        ESP_LOGI(TAG, "diag blink double=%d mode=%s", blink_double_, ModeName(mode_));
#endif
    }
    float scale = shown_mood_ == Mood::kSleepy ? 1.6f : 1.0f;
    float close_s = 0.07f * scale, hold_s = 0.04f * scale, open_s = 0.12f * scale;
    float gap_s = 0.08f * scale;
    float one = close_s + hold_s + open_s;
    float t = (now - blink_start_us_) / 1e6f;
    float amount;
    if (blink_double_ && t >= one + gap_s) {
        amount = BlinkCurve(t - one - gap_s, close_s, hold_s, open_s);
    } else if (blink_double_ && t >= one) {
        amount = 0;
    } else {
        amount = BlinkCurve(t, close_s, hold_s, open_s);
    }
    if (amount < 0) {
        blink_ = 0;
        blink_start_us_ = 0;
        next_blink_us_ = now + RandUs(3.0f, 6.0f);
        return;
    }
    blink_ = amount;
}

void FaceDisplay::UpdateShift(int64_t now) {
    if (now - last_shift_us_ < kShiftPeriodUs) {
        return;
    }
    last_shift_us_ = now;
    shift_idx_ = (shift_idx_ + 1) % 8;
    shift_x_ = kShiftPath[shift_idx_][0];
    shift_y_ = kShiftPath[shift_idx_][1];
    lv_obj_t* chrome[] = {top_bar_, status_bar_, bottom_bar_};
    for (lv_obj_t* obj : chrome) {
        lv_obj_set_style_translate_x(obj, shift_x_, 0);
        lv_obj_set_style_translate_y(obj, shift_y_, 0);
    }
    if (bedside_) {
        PlaceClock();
    }
    panel_->SetShift(shift_x_, shift_y_);
#if WEIXUE_FACE_DIAG
    ESP_LOGI(TAG, "diag shift=%d,%d", shift_x_, shift_y_);
#endif
}

void FaceDisplay::UpdateDots(int64_t now) {
    static const int16_t kDotPos[3][2] = {{58, -96}, {82, -116}, {110, -138}};
    bool show = mode_ == Mode::kThinking && !bedside_;
    float t = WaveTime(now);
    for (int i = 0; i < 3; i++) {
        if (!show) {
            if (!lv_obj_has_flag(dots_[i], LV_OBJ_FLAG_HIDDEN)) {
                lv_obj_add_flag(dots_[i], LV_OBJ_FLAG_HIDDEN);
                dot_opa_[i] = 0;
            }
            continue;
        }
        int size = 10 + 4 * i;
        lv_obj_set_pos(dots_[i], width_ / 2 + kDotPos[i][0] - size / 2 + ShiftX(),
                       height_ / 2 + kDotPos[i][1] - size / 2 + ShiftY());
        float wave = 0.5f + 0.5f * sinf(2 * kPi * 1.2f * t - i * 0.9f);
        uint8_t opa = static_cast<uint8_t>((60 + static_cast<int>(195 * wave)) & 0xF0);
        if (opa != dot_opa_[i]) {
            dot_opa_[i] = opa;
            lv_obj_set_style_bg_opa(dots_[i], opa, 0);
        }
        lv_obj_remove_flag(dots_[i], LV_OBJ_FLAG_HIDDEN);
    }
}

bool FaceDisplay::ApplyEye(int i, const EyeShape& s) {
    EyeBox b;
    b.w = std::max<int32_t>(2, lroundf(s.w));
    b.h = std::max<int32_t>(2, lroundf(s.h));
    b.x = lroundf(width_ / 2.0f + s.x - b.w / 2.0f);
    b.y = lroundf(height_ / 2.0f + s.y - b.h / 2.0f);
    b.r = std::min<int32_t>(lroundf(s.r), std::min(b.w, b.h) / 2);
    float lid_left = i == 0 ? s.lid_out : s.lid_in;
    float lid_right = i == 0 ? s.lid_in : s.lid_out;
    b.lid_left = lroundf(lid_left * b.h);
    b.lid_right = lroundf(lid_right * b.h);
    b.cut = lroundf(s.cut * b.h);

    if (b == box_[i]) {
        return false;
    }
    bool moved = b.x != box_[i].x || b.y != box_[i].y || b.w != box_[i].w || b.h != box_[i].h;
    box_[i] = b;
    if (moved) {
        lv_obj_set_pos(eyes_[i], b.x, b.y);
        lv_obj_set_size(eyes_[i], b.w, b.h);
    }
    lv_obj_invalidate(eyes_[i]);
    return true;
}

void FaceDisplay::EyeDrawCb(lv_event_t* e) {
    auto* self = static_cast<FaceDisplay*>(lv_event_get_user_data(e));
    lv_obj_t* obj = lv_event_get_target_obj(e);
    self->DrawEye(lv_event_get_layer(e), obj, obj == self->eyes_[0] ? 0 : 1);
}

void FaceDisplay::DrawEye(lv_layer_t* layer, lv_obj_t* obj, int i) const {
    const EyeBox& b = box_[i];
    lv_area_t area;
    lv_obj_get_coords(obj, &area);

    lv_draw_fill_dsc_t fill;
    lv_draw_fill_dsc_init(&fill);
    fill.color = lv_color_hex(eye_color_);
    fill.opa = LV_OPA_COVER;
    fill.radius = b.r;
    lv_draw_fill(layer, &fill, &area);

    // Lid: a black band down to the lower lid edge plus one triangle for the slant.
    int32_t lid_flat = std::min(b.lid_left, b.lid_right);
    if (lid_flat > 0) {
        lv_draw_fill_dsc_t band;
        lv_draw_fill_dsc_init(&band);
        band.color = lv_color_black();
        band.opa = LV_OPA_COVER;
        lv_area_t band_area = {area.x1, area.y1, area.x2, area.y1 + lid_flat - 1};
        lv_draw_fill(layer, &band, &band_area);
    }
    if (b.lid_left != b.lid_right) {
        lv_draw_triangle_dsc_t tri;
        lv_draw_triangle_dsc_init(&tri);
        tri.color = lv_color_black();
        tri.opa = LV_OPA_COVER;
        lv_value_precise_t x1 = area.x1, x2 = area.x2 + 1;
        lv_value_precise_t top = area.y1 + lid_flat - 1;
        tri.p[0] = {x1, top};
        tri.p[1] = {x2, top};
        tri.p[2] = b.lid_left > b.lid_right
                       ? lv_point_precise_t{x1, static_cast<lv_value_precise_t>(area.y1 + b.lid_left)}
                       : lv_point_precise_t{x2, static_cast<lv_value_precise_t>(area.y1 + b.lid_right)};
        lv_draw_triangle(layer, &tri);
    }

    if (b.cut > 0) {
        int32_t d = b.w * 18 / 10;
        int32_t cx = (area.x1 + area.x2) / 2;
        lv_area_t arch = {cx - d / 2, area.y2 + 1 - b.cut, cx - d / 2 + d - 1, area.y2 + d - b.cut};
        fill.color = lv_color_black();
        fill.radius = LV_RADIUS_CIRCLE;
        lv_draw_fill(layer, &fill, &arch);
    }
}

int FaceDisplay::ShiftX() const { return shift_x_ * (bedside_ ? kBedsideShiftScale : 1); }
int FaceDisplay::ShiftY() const { return shift_y_ * (bedside_ ? kBedsideShiftScale : 1); }

void FaceDisplay::UpdateClock(int64_t now) {
    if (clock_checked_us_ != 0 && now - clock_checked_us_ < 1000000) {
        return;
    }
    clock_checked_us_ = now;
    time_t t = time(nullptr);
    int minute = -1;
    char text[6] = "--:--";
    if (t >= kClockValidAfter) {
        struct tm tm_now;
        localtime_r(&t, &tm_now);
        minute = tm_now.tm_hour * 60 + tm_now.tm_min;
        snprintf(text, sizeof(text), "%02d:%02d", tm_now.tm_hour, tm_now.tm_min);
    }
    local_minute_ = minute;
    if (strcmp(text, clock_text_) != 0) {
        memcpy(clock_text_, text, sizeof(clock_text_));
        if (bedside_) {
            lv_obj_invalidate(clock_);
        }
    }
}

// Night and no interaction (touch, or a session other than a non-urgent announcement without a
// wake word or user transcript) within kBedsideResumeUs shows the bedside page.
void FaceDisplay::UpdateBedside(int64_t now, DeviceState state) {
    int64_t touch = touch_us_;
    bool touched = touch != touch_seen_us_;
    touch_seen_us_ = touch;
    bool quiet_session = Application::GetInstance().GetSessionOrigin() ==
                         Application::SessionOrigin::kProactive;
    bool active = state != kDeviceStateIdle && !quiet_session;
    if (touched || active) {
        bedside_resume_us_ = now + kBedsideResumeUs;
    }
    bool night = bedside_schedule_.IsNight(local_minute_, now / 1000000);
    bool want = night && now >= bedside_resume_us_;
    if (want != bedside_) {
        SetBedside(want, !night ? "day" : touched ? "touch" : active ? "session" : "night");
    }
}

void FaceDisplay::SetBedside(bool on, const char* reason) {
    bedside_ = on;
    eye_color_ = on ? kBedsideEyeColor : kEyeColor;
    lv_obj_t* chrome[] = {top_bar_, status_bar_, bottom_bar_};
    for (lv_obj_t* obj : chrome) {
        lv_obj_set_style_opa(obj, on ? LV_OPA_TRANSP : LV_OPA_COVER, 0);
    }
    if (on) {
        PlaceClock();
        lv_obj_remove_flag(clock_, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(clock_, LV_OBJ_FLAG_HIDDEN);
    }
    for (lv_obj_t* eye : eyes_) {
        lv_obj_invalidate(eye);
    }
    for (RingState& tile : ring_tiles_) {
        tile = RingState();
    }
    lv_obj_invalidate(ring_obj_);
    next_gaze_us_ = esp_timer_get_time();
    ESP_LOGI(TAG, "bedside=%s reason=%s local=%s hint=%d", on ? "on" : "off", reason, clock_text_,
             static_cast<int>(bedside_schedule_.hint()));
#if WEIXUE_FACE_DIAG
    diag_pose_since_us_ = esp_timer_get_time();
    if ((diag_dumped_poses_ & DiagPoseBit()) == 0) {
        diag_dump_due_us_ = diag_pose_since_us_ + 800 * 1000;
    }
#endif
}

void FaceDisplay::PlaceClock() {
    lv_obj_set_pos(clock_, (width_ - kClockW) / 2 + ShiftX(),
                   height_ / 2 + kClockY - kDigitH / 2 + ShiftY());
}

void FaceDisplay::ClockDrawCb(lv_event_t* e) {
    auto* self = static_cast<FaceDisplay*>(lv_event_get_user_data(e));
    self->DrawClock(lv_event_get_layer(e), lv_event_get_target_obj(e));
}

void FaceDisplay::DrawClock(lv_layer_t* layer, lv_obj_t* obj) const {
    lv_area_t area;
    lv_obj_get_coords(obj, &area);
    lv_draw_fill_dsc_t fill;
    lv_draw_fill_dsc_init(&fill);
    fill.color = lv_color_hex(kBedsideClockColor);
    fill.opa = LV_OPA_COVER;
    fill.radius = kSegT / 2;

    auto rect = [&](int32_t x, int32_t y, int32_t w, int32_t h) {
        lv_area_t a = {area.x1 + x, area.y1 + y, area.x1 + x + w - 1, area.y1 + y + h - 1};
        lv_draw_fill(layer, &fill, &a);
    };
    const int32_t half = (kDigitH + kSegT) / 2;
    auto digit = [&](int32_t x, uint8_t seg) {
        if (seg & 0x01) rect(x, 0, kDigitW, kSegT);                         // a
        if (seg & 0x02) rect(x + kDigitW - kSegT, 0, kSegT, half);          // b
        if (seg & 0x04) rect(x + kDigitW - kSegT, half - kSegT, kSegT, kDigitH - half + kSegT);  // c
        if (seg & 0x08) rect(x, kDigitH - kSegT, kDigitW, kSegT);           // d
        if (seg & 0x10) rect(x, half - kSegT, kSegT, kDigitH - half + kSegT);  // e
        if (seg & 0x20) rect(x, 0, kSegT, half);                            // f
        if (seg & 0x40) rect(x, (kDigitH - kSegT) / 2, kDigitW, kSegT);     // g
    };
    const int32_t xs[4] = {0, kDigitW + kDigitGap, kClockW - 2 * kDigitW - kDigitGap,
                           kClockW - kDigitW};
    const int chars[4] = {0, 1, 3, 4};
    for (int i = 0; i < 4; i++) {
        char c = clock_text_[chars[i]];
        digit(xs[i], c >= '0' && c <= '9' ? kDigitSegments[c - '0'] : kDashSegments);
    }
    int32_t colon_x = 2 * kDigitW + kDigitGap + kColonGap;
    fill.radius = LV_RADIUS_CIRCLE;
    rect(colon_x, kDigitH * 3 / 10 - kColonDot / 2, kColonDot, kColonDot);
    rect(colon_x, kDigitH * 7 / 10 - kColonDot / 2, kColonDot, kColonDot);
}

// Returns whether the ring is showing anything.
bool FaceDisplay::UpdateRing(int64_t now, float dt, DeviceState state) {
    RingState s;
    s.width = kRingWidth;
    s.color = kEyeColor;
    float t = WaveTime(now);
    float level_target = Application::GetInstance().GetAudioService().GetOutputLevel() / 255.0f;
    out_level_ = Approach(out_level_, level_target, dt,
                          level_target > out_level_ ? kLevelAttackTau : kLevelReleaseTau);

    bool error = now < error_until_us_ || state == kDeviceStateFatalError;
    if (error) {
        s.style = RingState::Style::kSolid;
        s.color = kErrorColor;
        int64_t since = now - (error_until_us_ - kErrorFlashUs);
        s.opa = since % kErrorFlashPeriodUs < kErrorFlashPeriodUs / 2 ? LV_OPA_COVER : 0;
    } else {
        switch (mode_) {
            case Mode::kIdle:
                break;
            case Mode::kListening: {
                float wave = 0.5f + 0.5f * sinf(2 * kPi * kListenPulseHz * t);
                s.style = RingState::Style::kSolid;
                s.opa = static_cast<uint8_t>(64 + 191 * wave) & 0xE0;
                break;
            }
            case Mode::kThinking:
                s.style = RingState::Style::kSpin;
                s.opa = LV_OPA_COVER;
                s.tail_color = kThinkTailColor;
                s.head_deg = static_cast<int16_t>(
                    RingState::Wrap(static_cast<int>(fmodf(t * kSpinDegPerS, 360.0f)) & ~1));
                break;
            case Mode::kSpeaking:
                s.style = RingState::Style::kSolid;
                s.opa = static_cast<uint8_t>(48 + 207 * out_level_) & 0xE0;
                break;
        }
    }
    if (bedside_ && s.style != RingState::Style::kOff) {
        s.color = s.tail_color = error ? kErrorColor : kBedsideRingColor;
        s.opa = static_cast<uint8_t>(s.opa * kBedsideRingOpa) & 0xF0;
    }

    // Solid rings repaint at most every kSolidFrameUs, a spin every kSpinFrameUs; a new style
    // or colour repaints at once.
    bool restyled = s.style != ring_.style || s.color != ring_.color;
    int64_t gap = s.style == RingState::Style::kSpin ? kSpinFrameUs : kSolidFrameUs;
    bool repaint = restyled || now - ring_painted_us_ >= gap;
    if (repaint) {
        ring_ = s;
        ring_painted_us_ = now;
    }
    const char* kind = error                                    ? "error"
                       : s.style == RingState::Style::kOff      ? "off"
                       : s.style == RingState::Style::kSpin     ? "spin"
                       : mode_ == Mode::kListening              ? "pulse"
                                                                : "level";
    if (strcmp(kind, ring_kind_) != 0) {
        ring_kind_ = kind;
        ESP_LOGI(TAG, "ring=%s bedside=%d", kind, bedside_);
#if WEIXUE_FACE_DIAG
        if (error && (diag_dumped_poses_ & DiagPoseBit()) == 0) {
            diag_dump_due_us_ = now + 800 * 1000;
        }
#endif
    }
    if (!repaint) {
        return s.style != RingState::Style::kOff;
    }
    const int cx = width_ / 2, cy = height_ / 2;
    const int r_out = width_ / 2 - kRingInset;
    for (int k = 0; k < RingState::kTiles; k++) {
        RingState view = s.TileView(k);
        if (view == ring_tiles_[k]) {
            continue;
        }
        int r_in = r_out - std::max(view.width, ring_tiles_[k].width);
        ring_tiles_[k] = view;
        RingState::Box b = RingState::SectorBox(cx, cy, r_out, r_in, k * RingState::kTileDeg,
                                                RingState::kTileDeg, kRingPad);
        lv_area_t a = {b.x1, b.y1, b.x2, b.y2};
        lv_obj_invalidate_area(ring_obj_, &a);
    }
    return s.style != RingState::Style::kOff;
}

// A8 coverage mask of the solid ring.
void FaceDisplay::BuildRingMask() {
    const int w = width_, h = height_;
    ring_mask_ = static_cast<uint8_t*>(heap_caps_calloc(1, w * h, MALLOC_CAP_SPIRAM));
    if (ring_mask_ == nullptr) {
        ESP_LOGW(TAG, "ring mask: no memory, drawing arcs");
        return;
    }
    const float cx = w / 2, cy = h / 2;
    const float r_out = w / 2 - kRingInset, r_in = r_out - kRingWidth;
    const float skip2 = (r_in - 1) * (r_in - 1), out2 = (r_out + 1) * (r_out + 1);
    for (int y = 0; y < h; y++) {
        for (int x = 0; x < w; x++) {
            float dx = x - cx, dy = y - cy, d2 = dx * dx + dy * dy;
            if (d2 < skip2 || d2 > out2) {
                continue;
            }
            float d = sqrtf(d2);
            float cov = std::clamp(r_out + 0.5f - d, 0.0f, 1.0f) * std::clamp(d - r_in + 0.5f, 0.0f, 1.0f);
            ring_mask_[y * w + x] = static_cast<uint8_t>(lroundf(cov * 255));
        }
    }
    ring_img_.header.magic = LV_IMAGE_HEADER_MAGIC;
    ring_img_.header.cf = LV_COLOR_FORMAT_A8;
    ring_img_.header.w = w;
    ring_img_.header.h = h;
    ring_img_.header.stride = w;
    ring_img_.data_size = w * h;
    ring_img_.data = ring_mask_;
}

void FaceDisplay::RingDrawCb(lv_event_t* e) {
    auto* self = static_cast<FaceDisplay*>(lv_event_get_user_data(e));
    self->DrawRing(lv_event_get_layer(e));
}

void FaceDisplay::DrawRing(lv_layer_t* layer) const {
    const RingState& s = ring_;
    if (s.style == RingState::Style::kOff || s.opa == 0) {
        return;
    }
    const int cx = width_ / 2, cy = height_ / 2;
    const int r_out = width_ / 2 - kRingInset;
    lv_draw_arc_dsc_t arc;
    lv_draw_arc_dsc_init(&arc);
    arc.center = {cx, cy};
    arc.radius = r_out;
    arc.width = s.width;
    arc.rounded = 0;
    const lv_area_t clip = layer->_clip_area;
    auto dist2 = [&](int32_t x, int32_t y) {
        return int64_t(x - cx) * (x - cx) + int64_t(y - cy) * (y - cy);
    };
    int32_t nx = std::clamp<int32_t>(cx, clip.x1, clip.x2), ny = std::clamp<int32_t>(cy, clip.y1, clip.y2);
    int64_t far2 = std::max({dist2(clip.x1, clip.y1), dist2(clip.x2, clip.y1), dist2(clip.x1, clip.y2),
                             dist2(clip.x2, clip.y2)});
    const int r_in = r_out - s.width - 1;
    if (far2 < int64_t(r_in) * r_in || dist2(nx, ny) > int64_t(r_out + 1) * (r_out + 1)) {
        return;
    }
    if (s.style == RingState::Style::kSolid && ring_mask_ != nullptr) {
        lv_draw_image_dsc_t img;
        lv_draw_image_dsc_init(&img);
        img.src = &ring_img_;
        img.recolor = lv_color_hex(s.color);
        img.recolor_opa = LV_OPA_COVER;
        img.opa = s.opa;
        lv_area_t coords = {0, 0, width_ - 1, height_ - 1};
        lv_draw_image(layer, &img, &coords);
        return;
    }
    if (s.style == RingState::Style::kSolid) {
        arc.color = lv_color_hex(s.color);
        arc.opa = s.opa;
        arc.start_angle = 0;
        arc.end_angle = 360;
        lv_draw_arc(layer, &arc);
        return;
    }
    constexpr int step_deg = RingState::kSpinSpanDeg / RingState::kSpinSteps;
    for (int i = 0; i < RingState::kSpinSteps; i++) {
        int start;
        uint8_t opa, mix;
        s.SpinStep(i, start, opa, mix);
        if (opa <= LV_OPA_MIN) {
            continue;
        }
        RingState::Box b = RingState::SectorBox(cx, cy, r_out, r_out - s.width, start, step_deg, 1);
        lv_area_t step_clip = {std::max(b.x1, clip.x1), std::max(b.y1, clip.y1),
                               std::min(b.x2, clip.x2), std::min(b.y2, clip.y2)};
        if (step_clip.x1 > step_clip.x2 || step_clip.y1 > step_clip.y2) {
            continue;
        }
        // Each step's draw task covers only its own sector box.
        lv_area_t clip_ori = layer->_clip_area;
        layer->_clip_area = step_clip;
        arc.color = lv_color_mix(lv_color_hex(s.tail_color), lv_color_hex(s.color), mix);
        arc.opa = opa;
        arc.start_angle = start;
        arc.end_angle = start + step_deg;
        lv_draw_arc(layer, &arc);
        layer->_clip_area = clip_ori;
    }
}

#if WEIXUE_FACE_DIAG
void FaceDisplay::DiagRefrCb(lv_event_t* e) {
    auto* self = static_cast<FaceDisplay*>(lv_event_get_user_data(e));
    int64_t now = esp_timer_get_time();
    switch (lv_event_get_code(e)) {
        case LV_EVENT_REFR_START: {
            self->diag_refr_start_us_ = now;
            self->diag_rendered_ = false;
            lv_display_t* disp = self->display_;
            const int64_t screen = int64_t(self->width_) * self->height_;
            for (uint32_t i = 0; i < disp->inv_p; i++) {
                int64_t size = lv_area_get_size(&disp->inv_areas[i]);
                self->diag_inv_areas_++;
                self->diag_inv_px_ += size;
                if (size >= screen) {
                    self->diag_inv_full_++;
                }
            }
            break;
        }
        case LV_EVENT_RENDER_START:
            self->diag_rendered_ = true;
            break;
        case LV_EVENT_REFR_READY:
            if (self->diag_rendered_) {
                int64_t frame = now - self->diag_refr_start_us_;
                self->diag_frames_++;
                self->diag_frame_us_sum_ += frame;
                self->diag_frame_us_max_ = std::max(self->diag_frame_us_max_, frame);
            }
            break;
        default:
            break;
    }
}

uint32_t FaceDisplay::DiagPoseBit() const {
    if (bedside_) {
        return 1u << 31;
    }
    if (ring_.color == kErrorColor) {
        return 1u << 30;
    }
    return 1u << (static_cast<int>(mode_) * 6 + static_cast<int>(shown_mood_));
}

void FaceDisplay::DiagTick(int64_t now) {
    if (now - diag_window_us_ >= 5LL * 1000 * 1000) {
        float secs = (now - diag_window_us_) / 1e6f;
        ESP_LOGI(TAG, "diag fps=%.1f frame_ms_avg=%.1f frame_ms_max=%.1f voice_flips=%lu mode=%s mood=%s "
                 "inv_areas=%.1f inv_kpx=%.1f inv_full=%lu ring=%s",
                 diag_frames_ / secs,
                 diag_frames_ ? diag_frame_us_sum_ / 1000.0f / diag_frames_ : 0.0f,
                 diag_frame_us_max_ / 1000.0f, (unsigned long)diag_voice_flips_, ModeName(mode_),
                 MoodName(shown_mood_),
                 diag_frames_ ? static_cast<float>(diag_inv_areas_) / diag_frames_ : 0.0f,
                 diag_frames_ ? diag_inv_px_ / 1000.0f / diag_frames_ : 0.0f,
                 (unsigned long)diag_inv_full_, ring_kind_);
        diag_inv_areas_ = 0;
        diag_inv_px_ = 0;
        diag_inv_full_ = 0;
        diag_window_us_ = now;
        diag_voice_flips_ = 0;
        diag_frames_ = 0;
        diag_frame_us_sum_ = 0;
        diag_frame_us_max_ = 0;
    }
    if (mode_ == Mode::kIdle && diag_dump_due_us_ == 0 &&
        now - diag_last_dump_us_ >= 60LL * 1000 * 1000 &&
        now - diag_pose_since_us_ >= 5LL * 1000 * 1000) {
        diag_dump_due_us_ = now;
    }
    if (diag_dump_due_us_ != 0 && now >= diag_dump_due_us_ &&
        now - diag_last_dump_us_ >= 2LL * 1000 * 1000) {
        diag_dump_due_us_ = 0;
        diag_last_dump_us_ = now;
        diag_dumped_poses_ |= DiagPoseBit();
        DiagDump();
    }
}

// Encodes the screen as run-length pairs (uint16 count, uint16 RGB565) for DiagPrintTask.
void FaceDisplay::DiagDump() {
    const uint32_t w = width_, h = height_;
    const uint32_t stride = lv_draw_buf_width_to_stride(w, LV_COLOR_FORMAT_RGB565);
    const uint32_t size = stride * h;
    if (diag_buf_ == nullptr) {
        diag_buf_ = static_cast<uint16_t*>(heap_caps_aligned_alloc(64, size, MALLOC_CAP_SPIRAM));
        if (diag_buf_ == nullptr) {
            ESP_LOGE(TAG, "diag dump: no memory");
            return;
        }
    }
    int64_t start = esp_timer_get_time();
    lv_draw_buf_t buf;
    if (lv_draw_buf_init(&buf, w, h, LV_COLOR_FORMAT_RGB565, stride, diag_buf_, size) != LV_RESULT_OK ||
        lv_snapshot_take_to_draw_buf(lv_screen_active(), LV_COLOR_FORMAT_RGB565, &buf) != LV_RESULT_OK) {
        ESP_LOGE(TAG, "diag dump: snapshot failed");
        return;
    }
    auto pixel = [&](uint32_t i) {
        return reinterpret_cast<const uint16_t*>(buf.data + (i / w) * stride)[i % w];
    };

    const uint32_t n = w * h;
    const int32_t rr = (w / 2) * (w / 2);
    uint32_t lit = 0, circle = 0, runs = 0;
    for (uint32_t i = 0; i < n; i++) {
        int32_t dx = static_cast<int32_t>(i % w) - static_cast<int32_t>(w / 2);
        int32_t dy = static_cast<int32_t>(i / w) - static_cast<int32_t>(h / 2);
        if (dx * dx + dy * dy <= rr) {
            circle++;
        }
        uint16_t px = pixel(i);
        if (px != 0) {
            lit++;
        }
    }
    for (uint32_t i = 0, run = 0; i < n; i++) {
        run++;
        if (i + 1 == n || pixel(i + 1) != pixel(i) || run == 0xFFFF) {
            runs++;
            run = 0;
        }
    }

    uint32_t id = ++diag_dump_id_;
    const char* mode = panel_->IsShown() ? "panel" : ModeName(mode_);
    DiagDumpMsg msg = {id, w, h, lit, mode, MoodName(shown_mood_), nullptr, runs * 4};
    msg.data = static_cast<uint8_t*>(heap_caps_malloc(msg.size, MALLOC_CAP_SPIRAM));
    if (msg.data != nullptr) {
        uint8_t* out = msg.data;
        for (uint32_t i = 0, run = 0; i < n; i++) {
            run++;
            uint16_t color = pixel(i);
            if (i + 1 == n || pixel(i + 1) != color || run == 0xFFFF) {
                *out++ = run & 0xFF;
                *out++ = run >> 8;
                *out++ = color & 0xFF;
                *out++ = color >> 8;
                run = 0;
            }
        }
    }
    ESP_LOGI(TAG, "diag lit=%.2f%% screen %.2f%% circle mode=%s mood=%s dump=%lu snap_ms=%.1f",
             100.0f * lit / n, 100.0f * lit / circle, msg.mode, msg.mood, (unsigned long)id,
             (esp_timer_get_time() - start) / 1000.0f);
    if (msg.data == nullptr || xQueueSend(diag_queue_, &msg, 0) != pdTRUE) {
        ESP_LOGW(TAG, "diag dump=%lu dropped", (unsigned long)id);
        heap_caps_free(msg.data);
    }
}

// Prints queued dumps as FACEDUMP lines (base64 of kDiagChunkBytes each), one per
// kDiagLineGapMs, without the display lock.
void FaceDisplay::DiagPrintTask(void* arg) {
    auto* self = static_cast<FaceDisplay*>(arg);
    DiagDumpMsg msg;
    unsigned char line[100];
    while (true) {
        if (xQueueReceive(self->diag_queue_, &msg, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        printf("FACEDUMP %lu begin %lu %lu %s %s\n", (unsigned long)msg.id, (unsigned long)msg.w,
               (unsigned long)msg.h, msg.mode, msg.mood);
        uint32_t seq = 0;
        for (size_t at = 0; at < msg.size; at += kDiagChunkBytes) {
            size_t out = 0;
            mbedtls_base64_encode(line, sizeof(line), &out, msg.data + at,
                                  std::min(kDiagChunkBytes, msg.size - at));
            line[out] = '\0';
            printf("FACEDUMP %lu %lu %s\n", (unsigned long)msg.id, (unsigned long)seq++, line);
            vTaskDelay(pdMS_TO_TICKS(kDiagLineGapMs));
        }
        printf("FACEDUMP %lu end %lu\n", (unsigned long)msg.id, (unsigned long)msg.lit);
        heap_caps_free(msg.data);
    }
}
#endif
