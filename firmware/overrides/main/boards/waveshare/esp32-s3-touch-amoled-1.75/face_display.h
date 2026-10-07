#ifndef WEIXUE_FACE_DISPLAY_H
#define WEIXUE_FACE_DISPLAY_H

#include "bedside_schedule.h"
#include "device_state.h"
#include "face_diag.h"
#include "lcd_display.h"
#include "voice_ring.h"

#include <atomic>
#include <cstdint>
#include <string>

#if WEIXUE_FACE_DIAG
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#endif

// Procedural two-eye face drawn with LVGL draw primitives in place of the emoji box.
// The pose follows the device state (idle, listening, thinking, speaking) and the
// last emotion; chat text stays available as a caption below the eyes. A ring at the
// screen edge shows the voice state; at night the idle screen is a dim red clock page.
class FaceDisplay : public SpiLcdDisplay {
public:
    FaceDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel, int width,
                int height, int offset_x, int offset_y, bool mirror_x, bool mirror_y,
                bool swap_xy);
    ~FaceDisplay();

    void SetupUI() override;
    void SetEmotion(const char* emotion) override;
    void SetChatMessage(const char* role, const char* content) override;
    void SetTheme(Theme* theme) override;
    void SetStatus(const char* status) override;

    // Server night hint ("night", "day", "auto"); any task. False for an unknown hint.
    bool SetBedsideHint(const char* hint);
    // Local "HH:MM" window, stored in NVS; any task. False when either time is malformed.
    bool SetBedsideWindow(const char* start, const char* end);
    // A touch while the panel is on: leaves the bedside page for kBedsideResumeUs.
    void OnTouch();
    // {"night":..,"showing":..,"hint":..,"window":"HH:MM-HH:MM","local_time":"HH:MM"|null}
    std::string BedsideStateJson();

    enum class Mode : uint8_t { kIdle, kListening, kThinking, kSpeaking };
    enum class Mood : uint8_t { kNeutral, kHappy, kSad, kSurprised, kSleepy, kConfused };

    static Mood MoodFromEmotion(const char* emotion);
    static const char* ModeName(Mode mode);
    static const char* MoodName(Mood mood);

private:
    // Eye geometry in pixels relative to the screen centre. lid_out / lid_in are the
    // fractions of the height covered from the top at the outer / inner edge; cut is the
    // fraction removed from the bottom by a circular arch.
    struct EyeShape {
        float x, y, w, h, r, lid_out, lid_in, cut;
    };
    struct EyeBox {
        int32_t x, y, w, h, r, lid_left, lid_right, cut;
        bool operator==(const EyeBox& o) const;
    };

    lv_obj_t* face_root_ = nullptr;
    lv_obj_t* eyes_[2] = {nullptr, nullptr};
    lv_obj_t* dots_[3] = {nullptr, nullptr, nullptr};
    lv_obj_t* clock_ = nullptr;
    lv_obj_t* ring_obj_ = nullptr;
    lv_timer_t* timer_ = nullptr;

    Mode mode_ = Mode::kIdle;
    Mood mood_ = Mood::kNeutral;
    Mood shown_mood_ = Mood::kNeutral;
    DeviceState last_state_ = kDeviceStateUnknown;
    bool user_turn_ = false;
    int64_t user_turn_us_ = 0;

    EyeShape cur_[2] = {};
    EyeBox box_[2] = {};
    uint8_t dot_opa_[3] = {0, 0, 0};
    int64_t last_tick_us_ = 0;

    float gaze_x_ = 0, gaze_y_ = 0, gaze_tx_ = 0, gaze_ty_ = 0;
    int64_t next_gaze_us_ = 0;

    float blink_ = 0;
    bool blink_double_ = false;
    int64_t blink_start_us_ = 0;
    int64_t next_blink_us_ = 0;

    int shift_idx_ = 0;
    int shift_x_ = 0, shift_y_ = 0;
    int64_t last_shift_us_ = 0;

    uint32_t eye_color_;
    uint8_t* ring_mask_ = nullptr;
    lv_image_dsc_t ring_img_ = {};
    RingState ring_;
    RingState ring_tiles_[RingState::kTiles];
    int64_t error_until_us_ = 0;
    const char* ring_kind_ = "off";
    int64_t ring_painted_us_ = 0;
    float out_level_ = 0;

    BedsideSchedule bedside_schedule_;
    bool bedside_ = false;
    int64_t bedside_resume_us_ = 0;
    std::atomic<int64_t> touch_us_{0};
    int64_t touch_seen_us_ = 0;
    int64_t clock_checked_us_ = 0;
    int local_minute_ = -1;
    char clock_text_[6] = "--:--";

    static void TickCb(lv_timer_t* timer);
    static void EyeDrawCb(lv_event_t* e);
    static void RingDrawCb(lv_event_t* e);
    static void ClockDrawCb(lv_event_t* e);

    void Tick();
    Mode ModeFromState(DeviceState state, int64_t now_us) const;
    void TargetShapes(EyeShape out[2], bool voice) const;
    void UpdateGaze(int64_t now_us, float dt_s);
    void UpdateBlink(int64_t now_us);
    void UpdateShift(int64_t now_us);
    void UpdateDots(int64_t now_us);
    void UpdateClock(int64_t now_us);
    void UpdateBedside(int64_t now_us, DeviceState state);
    void SetBedside(bool on, const char* reason);
    bool UpdateRing(int64_t now_us, float dt_s, DeviceState state);
    void BuildRingMask();
    void DrawRing(lv_layer_t* layer) const;
    void DrawClock(lv_layer_t* layer, lv_obj_t* obj) const;
    void PlaceClock();
    int ShiftX() const;
    int ShiftY() const;
    bool ApplyEye(int i, const EyeShape& s);
    void DrawEye(lv_layer_t* layer, lv_obj_t* obj, int i) const;
    void StyleChrome();

#if WEIXUE_FACE_DIAG
    int64_t diag_window_us_ = 0;
    int64_t diag_refr_start_us_ = 0;
    bool diag_rendered_ = false;
    uint32_t diag_frames_ = 0;
    int64_t diag_frame_us_sum_ = 0;
    int64_t diag_frame_us_max_ = 0;
    int64_t diag_dump_due_us_ = 0;
    int64_t diag_last_dump_us_ = 0;
    uint32_t diag_dump_id_ = 0;
    uint16_t* diag_buf_ = nullptr;
    uint32_t diag_dumped_poses_ = 0;
    int64_t diag_pose_since_us_ = 0;
    bool diag_voice_ = false;
    uint32_t diag_voice_flips_ = 0;
    uint32_t diag_inv_areas_ = 0;
    int64_t diag_inv_px_ = 0;
    uint32_t diag_inv_full_ = 0;
    QueueHandle_t diag_queue_ = nullptr;

    static void DiagRefrCb(lv_event_t* e);
    static void DiagPrintTask(void* arg);
    uint32_t DiagPoseBit() const;
    void DiagTick(int64_t now_us);
    void DiagDump();
#endif
};

#endif  // WEIXUE_FACE_DISPLAY_H
