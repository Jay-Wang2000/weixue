#pragma once

struct esp_app_desc_t {
    char version[32];
};

inline const esp_app_desc_t* esp_app_get_description() {
    static const esp_app_desc_t desc = {"host"};
    return &desc;
}
