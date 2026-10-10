#ifndef WEIXUE_DIAG_INPUT_H
#define WEIXUE_DIAG_INPUT_H

#include "face_diag.h"

#if WEIXUE_FACE_DIAG

#include <esp_log.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include <hal/usb_serial_jtag_ll.h>
#include <lvgl.h>

#include <cstdint>
#include <cstring>
#include <functional>
#include <string>

// Diag build only: an LVGL pointer that plays queued taps and swipes, read by LVGL like the
// touch panel. Strokes play one after another; each holds the pointer for press_ms (moving
// linearly from start to end) and then stays released for gap_ms.
class DiagPointer {
public:
    struct Stroke {
        int16_t x0, y0, x1, y1;
        uint16_t press_ms, gap_ms;
    };
    static constexpr int32_t kSize = 466;
    static constexpr uint16_t kTapMs = 120;
    static constexpr uint16_t kSwipeMs = 300;
    static constexpr uint16_t kGapMs = 400;
    static constexpr uint16_t kTourGapMs = 6000;

    // Call with the LVGL lock held. pressed_cb receives LV_EVENT_PRESSED with user_data.
    void Create(lv_display_t* display, lv_event_cb_t pressed_cb, void* user_data) {
        queue_ = xQueueCreate(16, sizeof(Stroke));
        indev_ = lv_indev_create();
        lv_indev_set_type(indev_, LV_INDEV_TYPE_POINTER);
        lv_indev_set_read_cb(indev_, ReadCb);
        lv_indev_set_user_data(indev_, this);
        lv_indev_set_display(indev_, display);
        if (pressed_cb != nullptr) {
            lv_indev_add_event_cb(indev_, pressed_cb, LV_EVENT_PRESSED, user_data);
        }
    }

    // Any task. False when a coordinate is off the panel or the queue is full.
    bool Tap(int32_t x, int32_t y) {
        if (x < 0 || y < 0 || x >= kSize || y >= kSize) {
            return false;
        }
        return Push({(int16_t)x, (int16_t)y, (int16_t)x, (int16_t)y, kTapMs, kGapMs});
    }

    // dir: left, right, up, down (the finger's direction). False for another word.
    bool Swipe(const std::string& dir, uint16_t gap_ms = kGapMs) {
        Stroke s;
        if (dir == "left") {
            s = {400, 233, 66, 233, kSwipeMs, gap_ms};
        } else if (dir == "right") {
            s = {66, 233, 400, 233, kSwipeMs, gap_ms};
        } else if (dir == "up") {
            s = {233, 380, 233, 120, kSwipeMs, gap_ms};
        } else if (dir == "down") {
            s = {233, 120, 233, 380, kSwipeMs, gap_ms};
        } else {
            return false;
        }
        return Push(s);
    }

    // Swipe left, up (page 2), down (page 1) and right, kTourGapMs apart.
    bool Tour() {
        return Swipe("left", kTourGapMs) && Swipe("up", kTourGapMs) && Swipe("down", kTourGapMs) &&
               Swipe("right");
    }

private:
    lv_indev_t* indev_ = nullptr;
    QueueHandle_t queue_ = nullptr;
    bool active_ = false;
    int64_t start_us_ = 0;
    Stroke stroke_ = {};
    lv_point_t point_ = {0, 0};

    bool Push(const Stroke& s) {
        return queue_ != nullptr && xQueueSend(queue_, &s, 0) == pdTRUE;
    }

    static void ReadCb(lv_indev_t* indev, lv_indev_data_t* data) {
        auto* self = static_cast<DiagPointer*>(lv_indev_get_user_data(indev));
        int64_t now = esp_timer_get_time();
        if (!self->active_ && xQueueReceive(self->queue_, &self->stroke_, 0) == pdTRUE) {
            self->active_ = true;
            self->start_us_ = now;
            const Stroke& s = self->stroke_;
            ESP_LOGI("DiagInput", "pointer (%d,%d)->(%d,%d) %u ms", s.x0, s.y0, s.x1, s.y1,
                     s.press_ms);
        }
        data->state = LV_INDEV_STATE_RELEASED;
        if (self->active_) {
            const Stroke& s = self->stroke_;
            int64_t t = (now - self->start_us_) / 1000;
            if (t < s.press_ms) {
                self->point_.x = s.x0 + (s.x1 - s.x0) * t / s.press_ms;
                self->point_.y = s.y0 + (s.y1 - s.y0) * t / s.press_ms;
                data->state = LV_INDEV_STATE_PRESSED;
            } else if (t >= s.press_ms + s.gap_ms) {
                self->active_ = false;
            }
        }
        data->point = self->point_;
    }
};

// Diag build only: reads newline-terminated lines from the USB Serial/JTAG receive FIFO (the
// console port) and hands each to a handler; logs `DiagConsole: ok <line>: <result>` or
// `DiagConsole: error <line>: <reason>`.
class DiagConsole {
public:
    // Returns true on success; *result is the reply text.
    using Handler = std::function<bool(const std::string& line, std::string* result)>;

    void Start(Handler handler) {
        handler_ = std::move(handler);
        xTaskCreate([](void* arg) { static_cast<DiagConsole*>(arg)->Run(); }, "diag_console",
                    6144, this, 3, nullptr);
        ESP_LOGI(kTag, "listening on USB Serial/JTAG");
    }

private:
    static constexpr const char* kTag = "DiagConsole";
    static constexpr size_t kMaxLine = 120;
    Handler handler_;

    void Run() {
        char line[kMaxLine + 1];
        size_t n = 0;
        bool overflow = false;
        while (true) {
            uint8_t chunk[64];
            uint32_t got = usb_serial_jtag_ll_read_rxfifo(chunk, sizeof(chunk));
            if (got == 0) {
                vTaskDelay(pdMS_TO_TICKS(20));
                continue;
            }
            for (uint32_t i = 0; i < got; i++) {
                char c = static_cast<char>(chunk[i]);
                if (c != '\n' && c != '\r') {
                    if (n < kMaxLine) {
                        line[n++] = c;
                    } else {
                        overflow = true;
                    }
                    continue;
                }
                line[n] = '\0';
                if (overflow) {
                    ESP_LOGW(kTag, "error: line longer than %u bytes", (unsigned)kMaxLine);
                } else if (n > 0) {
                    Dispatch(line);
                }
                n = 0;
                overflow = false;
            }
        }
    }

    void Dispatch(const char* line) {
        std::string result;
        if (handler_(line, &result)) {
            ESP_LOGI(kTag, "ok %s: %s", line, result.c_str());
        } else {
            ESP_LOGW(kTag, "error %s: %s", line, result.c_str());
        }
    }
};

#endif  // WEIXUE_FACE_DIAG

#endif  // WEIXUE_DIAG_INPUT_H
