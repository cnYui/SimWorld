#pragma once
#include <stdint.h>

/* Tunables stored in the "fw_params" partition (offset 0x110000). All fields
 * little-endian. If the magic or version does not match, defaults are used.
 * Keep in sync with platform/emcosim/firmware.py (FW_PARAMS_LAYOUT). */
#define FW_PARAMS_MAGIC   0x50524d53u   /* "SMRP" */
#define FW_PARAMS_VERSION 1

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint16_t version;
    uint16_t size;              /* sizeof(fw_params_t) */
    uint8_t  pin_servo;         /* LEDC output, default GPIO18 */
    uint8_t  pin_button;        /* active-low button, default GPIO0 (BOOT) */
    uint8_t  idle_pwm_off;      /* 1: stop pulses when idle (servo un-powered) */
    uint8_t  reserved0;
    float    press_angle_deg;   /* arm swing for one press, sign chosen by ON/OFF */
    float    ramp_deg_s;        /* commanded angle slew rate */
    float    pwm_center_us;
    float    pwm_us_per_deg;
    uint32_t press_ms;          /* hold time at the press angle */
    uint32_t retract_ms;        /* time allowed to return to centre */
    uint32_t debounce_ms;
    uint32_t stall_guard_ms;    /* 0 = off; otherwise max time at press angle per press */
} fw_params_t;

void fw_params_load(fw_params_t *p);
