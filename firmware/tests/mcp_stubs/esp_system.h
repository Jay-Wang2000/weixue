#pragma once
#include <cstdio>
#include <cstdlib>

[[noreturn]] inline void esp_system_abort(const char* details) {
    fprintf(stderr, "abort: %s\n", details);
    abort();
}
