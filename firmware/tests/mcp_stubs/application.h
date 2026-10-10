#pragma once
#include <cstdint>
#include <functional>
#include <string>

#define pdMS_TO_TICKS(ms) (ms)
inline void vTaskDelay(uint32_t) {}

// Host stub: Schedule runs the callback at once.
class Application {
public:
    static Application& GetInstance() {
        static Application instance;
        return instance;
    }
    void Schedule(std::function<void()> callback) { callback(); }
    void SendMcpMessage(const std::string&) {}
    void Reboot() {}
    bool UpgradeFirmware(const std::string&) { return false; }
};
