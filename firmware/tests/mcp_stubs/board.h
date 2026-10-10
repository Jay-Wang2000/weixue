#pragma once
#include <cstdint>
#include <string>

class AudioCodec {
public:
    void SetOutputVolume(int volume) { volume_ = volume; }
    int volume_ = 0;
};

class Backlight {
public:
    void SetBrightness(uint8_t brightness, bool) { brightness_ = brightness; }
    uint8_t brightness_ = 0;
};

class Camera {
public:
    void SetExplainUrl(const std::string&, const std::string&) {}
};

class Display {};

// Host stub with a codec and a backlight, no camera or display.
class Board {
public:
    static Board& GetInstance() {
        static Board instance;
        return instance;
    }
    std::string GetDeviceStatusJson() { return "{\"audio_speaker\":{\"volume\":50}}"; }
    std::string GetSystemInfoJson() { return "{}"; }
    AudioCodec* GetAudioCodec() { return &codec_; }
    Backlight* GetBacklight() { return &backlight_; }
    Camera* GetCamera() { return nullptr; }
    Display* GetDisplay() { return nullptr; }
    AudioCodec codec_;
    Backlight backlight_;
};
