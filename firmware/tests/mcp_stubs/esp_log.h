#pragma once
#include <cstdio>
#include <string>
#include <vector>

// Host stub: every log line is kept as "<level> <tag>: <message>".
inline std::vector<std::string>& HostLogLines() {
    static std::vector<std::string> lines;
    return lines;
}

#define HOST_LOG(level, tag, fmt, ...)                                         \
    do {                                                                       \
        char host_log_buf[512];                                                \
        snprintf(host_log_buf, sizeof(host_log_buf), fmt, ##__VA_ARGS__);      \
        HostLogLines().push_back(std::string(level " ") + tag + ": " + host_log_buf); \
    } while (0)
#define ESP_LOGE(tag, fmt, ...) HOST_LOG("E", tag, fmt, ##__VA_ARGS__)
#define ESP_LOGW(tag, fmt, ...) HOST_LOG("W", tag, fmt, ##__VA_ARGS__)
#define ESP_LOGI(tag, fmt, ...) HOST_LOG("I", tag, fmt, ##__VA_ARGS__)
#define ESP_LOGD(tag, fmt, ...) HOST_LOG("D", tag, fmt, ##__VA_ARGS__)
