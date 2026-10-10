#pragma once
#include <cstddef>

inline int mbedtls_base64_encode(unsigned char*, size_t, size_t* olen, const unsigned char*, size_t) {
    *olen = 0;
    return 0;
}
