#include "qmi8658.h"

#include <esp_log.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <rom/ets_sys.h>

#define TAG "Qmi8658"

namespace {

constexpr uint8_t kWhoAmI = 0x00;
constexpr uint8_t kCtrl1 = 0x02;
constexpr uint8_t kCtrl2 = 0x03;
constexpr uint8_t kCtrl5 = 0x06;
constexpr uint8_t kCtrl7 = 0x08;
constexpr uint8_t kCtrl8 = 0x09;
constexpr uint8_t kCtrl9 = 0x0A;
constexpr uint8_t kCal1L = 0x0B;
constexpr uint8_t kStatusInt = 0x2D;
constexpr uint8_t kStatus1 = 0x2F;
constexpr uint8_t kAxL = 0x35;
constexpr uint8_t kResetResult = 0x4D;
constexpr uint8_t kTapStatus = 0x59;
constexpr uint8_t kReset = 0x60;

constexpr uint8_t kWhoAmIValue = 0x05;
constexpr uint8_t kCmdAck = 0x00;
constexpr uint8_t kCmdConfigureTap = 0x0C;

// CTRL1: address auto-increment, little-endian, INT1/INT2 high-Z.
constexpr uint8_t kCtrl1Value = 0x40;
// CTRL2: ±4 g, 500 Hz (accelerometer only).
constexpr uint8_t kCtrl2Value = 0x14;
constexpr float kLsbPerG = 8192.0f;
// CTRL8: CTRL9 handshake on STATUSINT.bit7; bit0 enables the Tap engine.
constexpr uint8_t kCtrl8Handshake = 0x80;
constexpr uint8_t kCtrl8TapEnable = 0x01;

// Tap engine parameters in 500 Hz samples (QMI8658A datasheet Table 35 examples).
constexpr uint8_t kTapPeakWindow = 20;
constexpr uint8_t kTapPriority = 0;
constexpr uint16_t kTapWindow = 50;
constexpr uint16_t kDoubleTapWindow = 250;
constexpr uint8_t kTapAlpha = 8;            // 0.0625, 1/128 units
constexpr uint8_t kTapGamma = 32;           // 0.25, 1/128 units
constexpr uint16_t kTapPeakMagThr = 0x0320;  // 0.8 g^2, 1/1024 units
constexpr uint16_t kTapUdmThr = 0x0190;      // 0.4 g^2, 1/1024 units

constexpr int kTimeoutMs = 50;
constexpr uint8_t kAddresses[] = {0x6B, 0x6A};

}  // namespace

Qmi8658* Qmi8658::Probe(i2c_master_bus_handle_t bus) {
    for (uint8_t address : kAddresses) {
        if (i2c_master_probe(bus, address, kTimeoutMs) != ESP_OK) {
            continue;
        }
        i2c_device_config_t cfg = {
            .dev_addr_length = I2C_ADDR_BIT_LEN_7,
            .device_address = address,
            .scl_speed_hz = 400 * 1000,
            .scl_wait_us = 0,
            .flags = {.disable_ack_check = 0},
        };
        i2c_master_dev_handle_t dev = nullptr;
        if (i2c_master_bus_add_device(bus, &cfg, &dev) != ESP_OK) {
            continue;
        }
        uint8_t reg = kWhoAmI;
        uint8_t who = 0;
        if (i2c_master_transmit_receive(dev, &reg, 1, &who, 1, kTimeoutMs) == ESP_OK &&
            who == kWhoAmIValue) {
            ESP_LOGI(TAG, "QMI8658 found at 0x%02X", address);
            return new Qmi8658(dev, address);
        }
        ESP_LOGW(TAG, "Device at 0x%02X is not a QMI8658 (WHO_AM_I 0x%02X)", address, who);
        i2c_master_bus_rm_device(dev);
    }
    ESP_LOGW(TAG, "QMI8658 not found");
    return nullptr;
}

Qmi8658::~Qmi8658() { i2c_master_bus_rm_device(dev_); }

esp_err_t Qmi8658::Write(uint8_t reg, uint8_t value) {
    uint8_t buf[2] = {reg, value};
    return i2c_master_transmit(dev_, buf, sizeof(buf), kTimeoutMs);
}

esp_err_t Qmi8658::Read(uint8_t reg, uint8_t* data, size_t len) {
    return i2c_master_transmit_receive(dev_, &reg, 1, data, len, kTimeoutMs);
}

esp_err_t Qmi8658::Ctrl9(uint8_t command, const uint8_t cal[8]) {
    uint8_t buf[9] = {kCal1L};
    for (int i = 0; i < 8; i++) {
        buf[i + 1] = cal[i];
    }
    esp_err_t err = i2c_master_transmit(dev_, buf, sizeof(buf), kTimeoutMs);
    if (err == ESP_OK) {
        err = Write(kCtrl9, command);
    }
    if (err != ESP_OK) {
        return err;
    }
    uint8_t status = 0;
    for (int i = 0; i < 100; i++) {
        err = Read(kStatusInt, &status, 1);
        if (err == ESP_OK && (status & 0x80)) {
            break;
        }
        ets_delay_us(1000);
    }
    if (!(status & 0x80)) {
        ESP_LOGE(TAG, "CTRL9 command 0x%02X not done", command);
        return ESP_ERR_TIMEOUT;
    }
    return Write(kCtrl9, kCmdAck);
}

esp_err_t Qmi8658::Initialize() {
    esp_err_t err = Write(kReset, 0xB0);
    if (err != ESP_OK) {
        return err;
    }
    vTaskDelay(pdMS_TO_TICKS(20));
    uint8_t reset_result = 0;
    uint8_t who = 0;
    if ((err = Read(kResetResult, &reset_result, 1)) != ESP_OK ||
        (err = Read(kWhoAmI, &who, 1)) != ESP_OK) {
        return err;
    }
    if (who != kWhoAmIValue) {
        ESP_LOGE(TAG, "WHO_AM_I 0x%02X after reset", who);
        return ESP_ERR_INVALID_RESPONSE;
    }
    ESP_LOGI(TAG, "Reset result 0x%02X", reset_result);

    const uint8_t tap1[8] = {kTapPeakWindow,
                             kTapPriority,
                             static_cast<uint8_t>(kTapWindow & 0xFF),
                             static_cast<uint8_t>(kTapWindow >> 8),
                             static_cast<uint8_t>(kDoubleTapWindow & 0xFF),
                             static_cast<uint8_t>(kDoubleTapWindow >> 8),
                             0x00,
                             0x01};
    const uint8_t tap2[8] = {kTapAlpha,
                             kTapGamma,
                             static_cast<uint8_t>(kTapPeakMagThr & 0xFF),
                             static_cast<uint8_t>(kTapPeakMagThr >> 8),
                             static_cast<uint8_t>(kTapUdmThr & 0xFF),
                             static_cast<uint8_t>(kTapUdmThr >> 8),
                             0x00,
                             0x02};

    // Tap parameters are accepted only while the sensors are disabled (CTRL7 = 0).
    if ((err = Write(kCtrl1, kCtrl1Value)) != ESP_OK ||
        (err = Write(kCtrl7, 0x00)) != ESP_OK ||
        (err = Write(kCtrl2, kCtrl2Value)) != ESP_OK ||
        (err = Write(kCtrl5, 0x00)) != ESP_OK ||
        (err = Write(kCtrl8, kCtrl8Handshake)) != ESP_OK ||
        (err = Ctrl9(kCmdConfigureTap, tap1)) != ESP_OK ||
        (err = Ctrl9(kCmdConfigureTap, tap2)) != ESP_OK ||
        (err = Write(kCtrl8, kCtrl8Handshake | kCtrl8TapEnable)) != ESP_OK ||
        (err = Write(kCtrl7, 0x01)) != ESP_OK) {
        ESP_LOGE(TAG, "Configuration failed: %s", esp_err_to_name(err));
        return err;
    }
    vTaskDelay(pdMS_TO_TICKS(20));
    return ESP_OK;
}

esp_err_t Qmi8658::ReadAccel(Accel& out) {
    uint8_t raw[6];
    esp_err_t err = Read(kAxL, raw, sizeof(raw));
    if (err != ESP_OK) {
        return err;
    }
    out.x = static_cast<int16_t>(raw[0] | (raw[1] << 8)) / kLsbPerG;
    out.y = static_cast<int16_t>(raw[2] | (raw[3] << 8)) / kLsbPerG;
    out.z = static_cast<int16_t>(raw[4] | (raw[5] << 8)) / kLsbPerG;
    return ESP_OK;
}

esp_err_t Qmi8658::ReadTap(TapStatus& out) {
    out.status1 = 0;
    out.tap_status = 0;
    esp_err_t err = Read(kStatus1, &out.status1, 1);
    if (err != ESP_OK || !(out.status1 & 0x02)) {
        return err;
    }
    return Read(kTapStatus, &out.tap_status, 1);
}
