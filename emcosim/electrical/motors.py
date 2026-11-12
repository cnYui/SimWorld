"""Electrical models of actuators: a brushed DC motor (R, L, back-EMF, gearbox)
and a hobby position servo (pulse-width decoder + position loop + H-bridge +
the same DC motor). All quantities SI; joint-side angles in radians.

Average-value models: PWM switching inside the servo or driver is not resolved,
the drive is represented by its duty-weighted voltage. Good for the 0.1 ms ..
seconds range the co-simulator targets, not for EMI or ripple questions.
"""
from __future__ import annotations

import math

PRESETS = {
    # SG90-class micro servo. Chosen so that at 5.0 V the stall torque (0.1765 N*m)
    # and no-load speed (8.73 rad/s) equal simulation/parameters.json. Electrical
    # values are assumptions, not measurements: R gives ~0.71 A stall at 5 V.
    'sg90': {
        'r_ohm': 7.0, 'l_h': 3.0e-4, 'ke_v_s_rad': 0.00229, 'gear_ratio': 250.0,
        'efficiency': 0.43, 'i_ctrl_a': 0.006, 'v_logic_min': 3.0,
        'kp_per_rad': 15.0, 'kd_s_per_rad': 0.025, 'deadband_deg': 0.15,
        'pwm_center_us': 1500.0, 'us_per_deg': 11.111111, 'pulse_min_us': 500.0,
        'pulse_max_us': 2500.0, 'angle_limit_deg': 35.0, 'signal_timeout_s': 0.05,
        'freq_min_hz': 40.0, 'freq_max_hz': 500.0, 'direction': 1,
    },
    # signal_timeout_s: an analog servo stops driving about one frame after the
    # pulses stop. (simulation/bridge.py used 0.25 s as its virtual-driver policy.)
    # MG90S-class metal-gear micro servo (assumed: ~0.22 N*m stall at 5 V, ~1.0 A).
    'mg90s': {
        'r_ohm': 5.0, 'l_h': 3.0e-4, 'ke_v_s_rad': 0.00191, 'gear_ratio': 300.0,
        'efficiency': 0.38, 'i_ctrl_a': 0.006, 'v_logic_min': 3.0,
        'kp_per_rad': 15.0, 'kd_s_per_rad': 0.025, 'deadband_deg': 0.15,
        'pwm_center_us': 1500.0, 'us_per_deg': 11.111111, 'pulse_min_us': 500.0,
        'pulse_max_us': 2500.0, 'angle_limit_deg': 35.0, 'signal_timeout_s': 0.05,
        'freq_min_hz': 40.0, 'freq_max_hz': 500.0, 'direction': 1,
    },
    # Generic 6 V 130-size brushed motor with a 1:48 gearbox (assumed).
    'tt_motor': {
        'r_ohm': 3.0, 'l_h': 1.0e-3, 'ke_v_s_rad': 0.0045, 'gear_ratio': 48.0,
        'efficiency': 0.6, 'v_diode': 0.4,
    },
}


def merged(cfg):
    base = dict(PRESETS.get(cfg.get('preset', ''), {}))
    base.update({k: v for k, v in cfg.items() if k not in ('preset', 'type', 'joint', 'node')})
    return base


class DCMotor:
    """Brushed DC motor referred to the output shaft through a gearbox."""

    def __init__(self, p):
        self.R = float(p['r_ohm'])
        self.L = float(p['l_h'])
        self.ke = float(p['ke_v_s_rad'])
        self.N = float(p.get('gear_ratio', 1.0))
        self.eta = float(p.get('efficiency', 1.0))
        self.i = 0.0
        self.v_applied = 0.0

    def back_emf(self, omega_joint):
        return self.ke * self.N * omega_joint

    # Two-phase step so the supply node can be solved implicitly together with
    # the motor:  prepare() -> i_motor = i0 + v_motor / z ;  finish(v_motor).
    def prepare(self, omega_joint, dt):
        self._z = self.L / dt + self.R
        self._i0 = (self.L / dt * self.i - self.back_emf(omega_joint)) / self._z
        return self._i0, self._z

    def finish(self, v_motor, unidirectional=False):
        self.i = self._i0 + v_motor / self._z
        if unidirectional and self.i < 0:
            self.i = 0.0
        self.v_applied = v_motor
        return self.torque()

    def coast(self, omega_joint, dt):
        """Driver off: the current decays through the body diodes (approximated as
        an exponential decay to zero)."""
        self.i = self.i * math.exp(-dt * self.R / self.L) if self.L > 0 else 0.0
        self.v_applied = self.back_emf(omega_joint)
        return self.torque()

    def torque(self):
        return self.eta * self.N * self.ke * self.i


class Servo:
    """Hobby servo: decodes pulse width, runs a P+D position loop on the joint
    angle (its potentiometer), drives the motor from its supply node."""

    kind = 'servo'

    def __init__(self, name, cfg):
        self.name = name
        self.p = merged(cfg)
        self.joint = cfg['joint']
        self.node = cfg['node']
        self.motor = DCMotor(self.p)
        self.target = 0.0
        self.last_valid = -1e9
        self.pulse_us = 0.0
        self.duty_cmd = 0.0
        self.i_supply = 0.0
        self.active = False

    def set_signal(self, t, sig):
        """sig: dict(freq=Hz, duty=0..1) or None (pin low / undriven)."""
        p = self.p
        if sig and p['freq_min_hz'] <= sig['freq'] <= p['freq_max_hz']:
            pulse = sig['duty'] / sig['freq'] * 1e6
            if p['pulse_min_us'] <= pulse <= p['pulse_max_us']:
                self.pulse_us = pulse
                deg = (pulse - p['pwm_center_us']) / p['us_per_deg']
                lim = p['angle_limit_deg']
                self.target = math.radians(max(-lim, min(lim, deg))) * p.get('direction', 1)
                self.last_valid = t
                return
        self.pulse_us = 0.0

    def prepare(self, t, q, qd, v_prev, dt):
        """Returns the supply current as a Norton equivalent: i = i0 + g * v_supply."""
        p = self.p
        have_signal = t - self.last_valid <= p['signal_timeout_s']
        self.powered = v_prev >= p['v_logic_min']
        self.active = have_signal and self.powered
        self._qd, self._dt = qd, dt
        if not self.active:
            self.duty_cmd = 0.0
            return (p['i_ctrl_a'] if self.powered else 0.0), 0.0
        err = self.target - q
        dead = math.radians(p['deadband_deg'])
        err = math.copysign(max(0.0, abs(err) - dead), err)
        u = max(-1.0, min(1.0, p['kp_per_rad'] * err - p['kd_s_per_rad'] * qd))
        self.duty_cmd = u
        i0, z = self.motor.prepare(qd, dt)
        return u * i0 + p['i_ctrl_a'], u * u / z

    def finish(self, v_supply):
        p = self.p
        if self.active:
            tau = self.motor.finish(self.duty_cmd * v_supply)
            self.i_supply = self.duty_cmd * self.motor.i + p['i_ctrl_a']
        else:
            tau = self.motor.coast(self._qd, self._dt)
            self.i_supply = p['i_ctrl_a'] if self.powered else 0.0
        return tau

    def signals(self):
        return {f'act.{self.name}.i_motor': self.motor.i, f'act.{self.name}.i_supply': self.i_supply,
                f'act.{self.name}.torque': self.motor.torque(),
                f'act.{self.name}.target_deg': math.degrees(self.target),
                f'act.{self.name}.pulse_us': self.pulse_us, f'act.{self.name}.drive': self.duty_cmd,
                f'act.{self.name}.active': float(self.active)}


class DCMotorActuator:
    """DC motor behind a low-side switch (one pin, unidirectional, flyback diode)
    or an H-bridge (pins in_a / in_b)."""

    kind = 'dc_motor'

    def __init__(self, name, cfg):
        self.name = name
        self.p = merged(cfg)
        self.joint = cfg['joint']
        self.node = cfg['node']
        self.driver = cfg.get('driver', 'low_side')
        self.motor = DCMotor(self.p)
        self.duty = {'in_a': 0.0, 'in_b': 0.0}
        self.i_supply = 0.0

    def set_signal(self, t, sig, role='in_a'):
        if sig is None:
            self.duty[role] = 0.0
        elif sig.get('freq', 0) == 0:
            self.duty[role] = float(sig['duty'] >= 0.5)
        else:
            self.duty[role] = float(sig['duty'])

    def _drive(self):
        if self.driver == 'h_bridge':
            return self.duty['in_a'] - self.duty['in_b']
        return self.duty['in_a']

    def prepare(self, t, q, qd, v_prev, dt):
        d = self._drive()
        i0, z = self.motor.prepare(qd, dt)
        if self.driver != 'h_bridge' and d < 1.0 and self.motor.i > 0:
            # low side switch off part of the period: flyback diode voltage
            i0 -= (1 - d) * self.p.get('v_diode', 0.4) / z
        self._d = d
        return d * i0, d * d / z

    def finish(self, v_supply):
        d = self._d
        v_m = d * v_supply
        if self.driver != 'h_bridge' and d < 1.0 and self.motor.i > 0:
            v_m -= (1 - d) * self.p.get('v_diode', 0.4)
        tau = self.motor.finish(v_m, unidirectional=self.driver != 'h_bridge')
        self.i_supply = d * self.motor.i
        return tau

    def signals(self):
        return {f'act.{self.name}.i_motor': self.motor.i, f'act.{self.name}.i_supply': self.i_supply,
                f'act.{self.name}.torque': self.motor.torque(),
                f'act.{self.name}.duty': self.duty['in_a'] - self.duty['in_b']}


def make_actuator(name, cfg):
    kind = cfg.get('type', 'servo')
    if kind == 'servo':
        return Servo(name, cfg)
    if kind == 'dc_motor':
        return DCMotorActuator(name, cfg)
    raise ValueError(f'unknown actuator type {kind!r}')
