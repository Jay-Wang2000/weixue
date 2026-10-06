#ifndef QMI8658_H
#define QMI8658_H

#include <driver/i2c_master.h>
#include <esp_err.h>

#include <cstddef>
#include <cstdint>

// QMI8658A accelerometer with the on-chip Tap engine, polled over I2C (INT pins stay high-Z).
class Qmi8658 {
public:
    struct Accel {
        float x;
        float y;
        float z;
    };

    struct TapStatus {
        uint8_t status1;     // STATUS1 (0x2F)
        uint8_t tap_status;  // TAP_STATUS (0x59), 0 unless STATUS1.TAP is set
        uint8_t count() const { return tap_status & 0x03; }  // 1 single, 2 double
    };

    // Returns nullptr when no QMI8658 answers at 0x6B or 0x6A.
    static Qmi8658* Probe(i2c_master_bus_handle_t bus);
    ~Qmi8658();

    uint8_t address() const { return address_; }
    // Soft reset, accelerometer ±4 g at 500 Hz, Tap engine on.
    esp_err_t Initialize();
    esp_err_t ReadAccel(Accel& out);
    // Reads STATUS1 and, when its TAP bit is set, TAP_STATUS.
    esp_err_t ReadTap(TapStatus& out);

private:
    Qmi8658(i2c_master_dev_handle_t dev, uint8_t address) : dev_(dev), address_(address) {}

    esp_err_t Write(uint8_t reg, uint8_t value);
    esp_err_t Read(uint8_t reg, uint8_t* data, size_t len);
    esp_err_t Ctrl9(uint8_t command, const uint8_t cal[8]);

    i2c_master_dev_handle_t dev_;
    uint8_t address_;
};

#endif  // QMI8658_H
