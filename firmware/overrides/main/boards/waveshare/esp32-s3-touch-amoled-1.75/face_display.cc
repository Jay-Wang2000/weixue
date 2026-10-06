#include "face_display.h"

#include "application.h"
#include "lvgl_theme.h"

#include <esp_log.h>
#include <esp_random.h>
#include <esp_timer.h>

#include <algorithm>
#include <cmath>
#include <cstring>

#if WEIXUE_FACE_DIAG
#include <esp_heap_caps.h>
#include <freertos/task.h>
#include <mbedtls/base64.h>
#include <cstdio>
#endif

#define TAG "FaceDisplay"

namespace {

constexpr uint32_t kEyeColor = 0x00E0FF;
constexpr uint32_t kCaptionColor = 0x9FB4BC;
constexpr uint32_t kChromeColor = 0x6E8A92;

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
constexpr int8_t kShiftPath[8][2] = {{0, 0},  {1, 0},  {1, 1},   {0, 1},
                                     {-1, 1}, {-1, 0}, {-1, -1}, {0, -1}};
constexpr float kPi = 3.14159265f;

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
                    swap_xy) {
    auto dark = LvglThemeManager::GetInstance().GetTheme("dark");
    if (dark != nullptr) {
        current_theme_ = dark;
    }
}

FaceDisplay::~FaceDisplay() {
    if (timer_ != nullptr) {
        lv_timer_delete(timer_);
    }
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

    StyleChrome();

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
    }
}

void FaceDisplay::SetEmotion(const char* emotion) {
    DisplayLockGuard lock(this);
    mood_ = MoodFromEmotion(emotion);
}

void FaceDisplay::SetChatMessage(const char* role, const char* content) {
    if (role != nullptr && strcmp(role, "assistant") == 0) {
        DisplayLockGuard lock(this);
        user_turn_ = false;
    }
    if (role != nullptr && strcmp(role, "user") == 0) {
        {
            DisplayLockGuard lock(this);
            if (content != nullptr && content[0] != '\0') {
                user_turn_ = true;
                user_turn_us_ = esp_timer_get_time();
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
        }
        last_state_ = state;
    }

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
        s.x += shift_x_;
        s.y += shift_y_;
        changed |= ApplyEye(i, s);
    }
    UpdateDots(now);

    bool resting = !changed && blink_start_us_ == 0 &&
                   (mode_ == Mode::kIdle || mode_ == Mode::kListening);
    lv_timer_set_period(timer_, resting ? kRestTickMs : kTickMs);

#if WEIXUE_FACE_DIAG
    DiagTick(now);
#endif
}

void FaceDisplay::TargetShapes(EyeShape out[2], bool voice) const {
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
    if (now >= next_gaze_us_) {
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
    if (mode_ == Mode::kThinking) {
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
#if WEIXUE_FACE_DIAG
    ESP_LOGI(TAG, "diag shift=%d,%d", shift_x_, shift_y_);
#endif
}

void FaceDisplay::UpdateDots(int64_t now) {
    static const int16_t kDotPos[3][2] = {{58, -96}, {82, -116}, {110, -138}};
    bool show = mode_ == Mode::kThinking;
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
        lv_obj_set_pos(dots_[i], width_ / 2 + kDotPos[i][0] - size / 2 + shift_x_,
                       height_ / 2 + kDotPos[i][1] - size / 2 + shift_y_);
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
    fill.color = lv_color_hex(kEyeColor);
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

#if WEIXUE_FACE_DIAG
void FaceDisplay::DiagRefrCb(lv_event_t* e) {
    auto* self = static_cast<FaceDisplay*>(lv_event_get_user_data(e));
    int64_t now = esp_timer_get_time();
    switch (lv_event_get_code(e)) {
        case LV_EVENT_REFR_START:
            self->diag_refr_start_us_ = now;
            self->diag_rendered_ = false;
            break;
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
    return 1u << (static_cast<int>(mode_) * 6 + static_cast<int>(shown_mood_));
}

void FaceDisplay::DiagTick(int64_t now) {
    if (now - diag_window_us_ >= 5LL * 1000 * 1000) {
        float secs = (now - diag_window_us_) / 1e6f;
        ESP_LOGI(TAG, "diag fps=%.1f frame_ms_avg=%.1f frame_ms_max=%.1f voice_flips=%lu mode=%s mood=%s",
                 diag_frames_ / secs,
                 diag_frames_ ? diag_frame_us_sum_ / 1000.0f / diag_frames_ : 0.0f,
                 diag_frame_us_max_ / 1000.0f, (unsigned long)diag_voice_flips_, ModeName(mode_),
                 MoodName(shown_mood_));
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
    DiagDumpMsg msg = {id, w, h, lit, ModeName(mode_), MoodName(shown_mood_), nullptr, runs * 4};
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
