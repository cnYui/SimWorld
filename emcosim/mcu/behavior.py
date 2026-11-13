"""A Python stand-in for the firmware, for logic the patched QEMU cannot exercise yet.

QEMU runs the real ESP32 image but models only GPIO and LEDC: no ADC, no I2C peripherals, no radio.
A device that counts repetitions from an IMU, reads a weight knob on the ADC and exports data needs
those, so this backend runs a small Python model of the firmware against the co-simulation instead:
the same 1 ms exchange, the same pins, brown-out resets, plus the IMU probes and analog inputs of the
mechanical/electrical model. Write the algorithm here first, then port it to the real firmware.

The script defines setup(io) (optional, run at every boot) and loop(io, t) (every 1 ms). io offers:

    io.t                         time in s
    io.read(pin) -> 0/1          digital input level (buttons; pull-ups as configured)
    io.adc(pin)  -> 0..4095      12-bit ADC of an analog input (pot) on that pin (3.3 V full scale)
    io.volts(pin) -> V           the same input in volts
    io.imu(name) -> {ax, ay, az, gx, gy, gz, a_g}   IMU probe: m/s2 in the sensor frame (gravity
                                 included, as a real accelerometer), deg/s, |a| in g
    io.write(pin, level)         digital output
    io.pwm(pin, duty, freq=1000) / io.servo(pin, pulse_us, freq=50) / io.off(pin)
    io.log(text)                 UART line (get_results uart_tail)
    io.event(name, **detail)     counted per name in metrics.events, testable with expect: {event: name}
    io.export(rows, name='export.csv')   rows: list of dicts (CSV) or text; saved with the run
    io.state                     dict kept between loop calls (cleared at each boot)
"""
from __future__ import annotations

import csv
import importlib.util
import io as _io
import traceback
from pathlib import Path

from ..project import pin_number

ADC_FULL_SCALE_V = 3.3


class BehaviorError(RuntimeError):
    pass


class _IO:
    def __init__(self, mcu):
        self._m = mcu
        self.t = 0.0
        self.state = {}
        self._mask = (1 << 40) - 1
        self.out = {}

    def read(self, pin):
        return (self._mask >> pin_number(pin)) & 1

    def volts(self, pin):
        a = self._m.sense.get('analog', {})
        n = pin_number(pin)
        if n not in a:
            raise KeyError(f'no analog input on GPIO{n}; analog inputs: {sorted(f"GPIO{k}" for k in a) or "none"} '
                           '(bind_pin input_type="pot")')
        return a[n]

    def adc(self, pin):
        v = self.volts(pin)
        return int(round(max(0.0, min(1.0, v / ADC_FULL_SCALE_V)) * 4095))

    def imu(self, name):
        s = self._m.sense.get('imu', {})
        if name not in s:
            raise KeyError(f'no IMU probe {name!r}; IMU probes: {sorted(s) or "none"} (define_probe type="imu")')
        return dict(s[name])

    def write(self, pin, level):
        self.out[pin_number(pin)] = (0.0, 1.0 if level else 0.0)

    def pwm(self, pin, duty, freq=1000.0):
        self.out[pin_number(pin)] = (float(freq), float(duty))

    def servo(self, pin, pulse_us, freq=50.0):
        self.out[pin_number(pin)] = (float(freq), float(pulse_us) * 1e-6 * float(freq))

    def off(self, pin):
        self.out.pop(pin_number(pin), None)

    def log(self, text):
        self._m.uart.append((round(self.t, 4), str(text)))

    def event(self, name, **detail):
        d = ' '.join(f'{k}={v}' for k, v in detail.items())
        self._m.events.append((round(self.t, 4), str(name), d))

    def export(self, rows, name='export.csv'):
        if not isinstance(rows, str):
            rows = list(rows)
            buf = _io.StringIO()
            if rows:
                keys = list(dict.fromkeys(k for r in rows for k in r))
                w = csv.DictWriter(buf, fieldnames=keys)
                w.writeheader()
                w.writerows(rows)
            rows = buf.getvalue()
        safe = Path(str(name)).name
        self._m.files[safe] = rows
        self.event('export', file=safe, bytes=len(rows.encode('utf-8')))


class BehaviorMcu:
    backend = 'behavior'

    def __init__(self, script_path, params=None):
        script_path = Path(script_path)
        if not script_path.exists():
            raise FileNotFoundError(script_path)
        spec = importlib.util.spec_from_file_location(f'emcosim_behavior_{abs(hash(str(script_path)))}', script_path)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            raise BehaviorError(f'{script_path.name} does not load: {_where(e, script_path)}') from e
        if not callable(getattr(mod, 'loop', None)):
            raise BehaviorError(f'{script_path.name} needs a function loop(io, t)')
        self.mod, self.path = mod, script_path
        self.params = dict(params or {})
        self.uart, self.events, self.files = [], [], {}
        self.resets, self.boots = 0, 0
        self.sense = {}
        self.io = _IO(self)
        self.io.params = self.params
        self._on = False

    def set_sensors(self, sense):
        self.sense = sense

    def exchange(self, t, inputs_mask, power_good):
        io = self.io
        io.t = t
        if not power_good:
            if self._on:
                self.resets += 1
                self.events.append((round(t, 4), 'reset', 'brownout'))
            self._on = False
            io.out.clear()
            return {'out': 0, 'enable': 0, 'pwm': {}}
        io._mask = inputs_mask
        if not self._on:
            self._on = True
            self.boots += 1
            io.state = {}
            io.out.clear()
            self.events.append((round(t, 4), 'boot', f'count={self.boots} reason={"POWERON" if self.boots == 1 else "BROWNOUT"}'))
            if callable(getattr(self.mod, 'setup', None)):
                self._call(self.mod.setup, io)
        self._call(self.mod.loop, io, t)
        pwm = {p: v for p, v in io.out.items() if v[0] > 0}
        out = sum(1 << p for p, v in io.out.items() if v[0] == 0 and v[1] >= 0.5)
        en = sum(1 << p for p in io.out)
        return {'out': out, 'enable': en, 'pwm': pwm}

    def _call(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:
            raise BehaviorError(f'{self.path.name} failed at t={self.io.t:.3f} s: {_where(e, self.path)}') from e

    def close(self):
        pass


def _where(e, path):
    tb = [f for f in traceback.extract_tb(e.__traceback__) if Path(f.filename).name == Path(path).name]
    at = f' (line {tb[-1].lineno}: {tb[-1].line})' if tb else ''
    msg = e.args[0] if isinstance(e, KeyError) and e.args else e
    return f'{type(e).__name__}: {msg}{at}'


def check_script(path):
    """Load the script once and report what it defines, so a broken script is caught when written."""
    m = BehaviorMcu(path)
    return {'script': str(path), 'setup': callable(getattr(m.mod, 'setup', None)), 'loop': True}


__all__ = ['BehaviorMcu', 'BehaviorError', 'check_script']
