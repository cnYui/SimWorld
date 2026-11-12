/*
 * Minimal switch-robot firmware: one active-low button toggles the light.
 * Each press swings the arm to +/-press_angle (OFF / ON side), holds, and
 * returns to centre. The servo gets 50 Hz pulses from LEDC high-speed
 * channel 0. Behaviour mirrors simulation/controller.py so the old MuJoCo
 * results remain comparable.
 *
 * Log lines starting with "EVT " are machine-readable and are picked up by
 * the co-simulator (platform/emcosim).
 */
#include <math.h>
#include <stdbool.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "driver/ledc.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "params.h"

static const char *TAG = "robot";
static fw_params_t P;
static RTC_NOINIT_ATTR uint32_t s_boot_count;
static RTC_NOINIT_ATTR uint32_t s_boot_magic;

#define LEDC_RES       LEDC_TIMER_14_BIT
#define LEDC_PERIOD_US 20000.0f

static void servo_write_us(float us)
{
    if (us < 500) us = 500;
    if (us > 2500) us = 2500;
    uint32_t duty = (uint32_t)lroundf(us / LEDC_PERIOD_US * (1 << 14));
    ledc_set_duty(LEDC_HIGH_SPEED_MODE, LEDC_CHANNEL_0, duty);
    ledc_update_duty(LEDC_HIGH_SPEED_MODE, LEDC_CHANNEL_0);
}

static void servo_init(void)
{
    ledc_timer_config_t t = {
        .speed_mode = LEDC_HIGH_SPEED_MODE, .timer_num = LEDC_TIMER_0,
        .duty_resolution = LEDC_RES, .freq_hz = 50, .clk_cfg = LEDC_USE_APB_CLK,
    };
    ESP_ERROR_CHECK(ledc_timer_config(&t));
    ledc_channel_config_t c = {
        .gpio_num = P.pin_servo, .speed_mode = LEDC_HIGH_SPEED_MODE,
        .channel = LEDC_CHANNEL_0, .timer_sel = LEDC_TIMER_0,
        .duty = 0, .hpoint = 0,
    };
    ESP_ERROR_CHECK(ledc_channel_config(&c));
    servo_write_us(P.pwm_center_us);
}

static void servo_idle(void)
{
    if (P.idle_pwm_off) {
        ledc_stop(LEDC_HIGH_SPEED_MODE, LEDC_CHANNEL_0, 0);
    } else {
        servo_write_us(P.pwm_center_us);
    }
}

static const char *reason_str(esp_reset_reason_t r)
{
    switch (r) {
    case ESP_RST_POWERON: return "POWERON";
    case ESP_RST_BROWNOUT: return "BROWNOUT";
    case ESP_RST_SW: return "SW";
    case ESP_RST_PANIC: return "PANIC";
    case ESP_RST_INT_WDT: case ESP_RST_TASK_WDT: case ESP_RST_WDT: return "WDT";
    default: return "OTHER";
    }
}

typedef enum { IDLE, PRESS, HOLD, RETRACT } phase_t;

void app_main(void)
{
    if (s_boot_magic != 0xB007C0DE) { s_boot_magic = 0xB007C0DE; s_boot_count = 0; }
    s_boot_count++;
    esp_reset_reason_t why = esp_reset_reason();
    fw_params_load(&P);
    printf("EVT boot count=%lu reason=%s\n", (unsigned long)s_boot_count, reason_str(why));
    ESP_LOGI(TAG, "servo GPIO%d, button GPIO%d, press %.1f deg, hold %lu ms",
             P.pin_servo, P.pin_button, P.press_angle_deg, (unsigned long)P.press_ms);

    gpio_config_t btn = {
        .pin_bit_mask = 1ULL << P.pin_button, .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&btn);
    servo_init();
    printf("EVT ready\n");

    bool light_on = false;          /* what we believe; there is no sensor */
    phase_t phase = IDLE;
    float angle = 0, goal = 0;
    int64_t phase_t0 = 0, last = esp_timer_get_time();
    int stable_level = 1, debounce_level = 1;
    int64_t debounce_t0 = 0;

    for (;;) {
        vTaskDelay(1);
        int64_t now = esp_timer_get_time();
        float dt = (now - last) / 1e6f;
        last = now;

        /* debounce: level must be stable for debounce_ms */
        int lvl = gpio_get_level(P.pin_button);
        if (lvl != debounce_level) { debounce_level = lvl; debounce_t0 = now; }
        if (debounce_level != stable_level && now - debounce_t0 >= P.debounce_ms * 1000LL) {
            stable_level = debounce_level;
            if (stable_level == 0 && phase == IDLE) {
                light_on = !light_on;
                goal = (light_on ? -1.0f : 1.0f) * P.press_angle_deg;
                phase = PRESS;
                phase_t0 = now;
                if (P.idle_pwm_off) servo_init();
                printf("EVT press target=%s angle=%.1f\n", light_on ? "ON" : "OFF", goal);
            } else if (stable_level == 0) {
                printf("EVT busy_ignored\n");
            }
        }

        int64_t in_phase = (now - phase_t0) / 1000;
        switch (phase) {
        case PRESS:
            if (fabsf(angle - goal) < 0.01f) { phase = HOLD; phase_t0 = now; }
            break;
        case HOLD: {
            uint32_t limit = P.press_ms;
            if (P.stall_guard_ms && P.stall_guard_ms < limit) limit = P.stall_guard_ms;
            if (in_phase >= limit) {
                phase = RETRACT; phase_t0 = now; goal = 0;
                if (limit < P.press_ms) printf("EVT stall_guard\n");
            }
            break;
        }
        case RETRACT:
            if (in_phase >= P.retract_ms && fabsf(angle) < 0.01f) {
                phase = IDLE;
                servo_idle();
                printf("EVT done light=%s (unconfirmed)\n", light_on ? "ON" : "OFF");
            }
            break;
        default:
            break;
        }
        if (phase != IDLE) {
            float step = P.ramp_deg_s * dt;
            float e = goal - angle;
            angle += e > step ? step : (e < -step ? -step : e);
            servo_write_us(P.pwm_center_us + angle * P.pwm_us_per_deg);
        }
    }
}
