#include <string.h>
#include "esp_log.h"
#include "esp_partition.h"
#include "params.h"

static const char *TAG = "params";

static const fw_params_t DEFAULTS = {
    .magic = FW_PARAMS_MAGIC, .version = FW_PARAMS_VERSION, .size = sizeof(fw_params_t),
    .pin_servo = 18, .pin_button = 0, .idle_pwm_off = 0,
    .press_angle_deg = 17.0f, .ramp_deg_s = 90.0f,
    .pwm_center_us = 1500.0f, .pwm_us_per_deg = 11.111111f,
    .press_ms = 650, .retract_ms = 500, .debounce_ms = 30, .stall_guard_ms = 0,
};

void fw_params_load(fw_params_t *p)
{
    *p = DEFAULTS;
    const esp_partition_t *part = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, 0x40, "fw_params");
    fw_params_t stored;
    if (part && esp_partition_read(part, 0, &stored, sizeof(stored)) == ESP_OK &&
        stored.magic == FW_PARAMS_MAGIC && stored.version == FW_PARAMS_VERSION &&
        stored.size == sizeof(fw_params_t)) {
        *p = stored;
        ESP_LOGI(TAG, "loaded from flash");
    } else {
        ESP_LOGW(TAG, "no valid fw_params partition, using defaults");
    }
}
