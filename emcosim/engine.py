"""The co-simulation loop.

Shared virtual clock, exchange period 1 ms (mcu.step_ms):

  every 1 ms   MCU (QEMU, lock-step) <- input pins, power-good
               MCU -> output pins (GPIO levels, LEDC frequency/duty)
  every dt_m   (MuJoCo timestep, e.g. 0.125 ms, i.e. 8 sub-steps per exchange)
               actuators: pin signal + supply voltage + joint state -> current, torque
               power network: load currents -> node voltages (backward Euler)
               mechanics: torques -> MuJoCo step
  end of 1 ms  brown-out detector on the MCU rail -> power-good for the next exchange

The MCU therefore sees inputs with at most one exchange period of latency,
which is the only approximation the coupling adds.
"""
from __future__ import annotations

import math
import time

import numpy as np

from .electrical.motors import make_actuator
from .electrical.power import BrownoutDetector, PowerNetwork
from .mechanics import Mechanics
from .project import get_path, pin_number

RUNTIME_SETTABLE = ('power.sources.',)


class RunCancelled(Exception):
    """Raised inside Simulation.run when the caller's cancel event is set."""


class Inputs:
    """External inputs wired to MCU pins: buttons (active low/high)."""

    def __init__(self, cfg, bindings):
        self.cfg = cfg or {}
        self.pins = {}
        for b in bindings:
            if 'from' in b:
                self.pins[b['from']] = pin_number(b['pin'])
        self.pressed = {k: False for k in self.cfg}

    def mask(self):
        m = (1 << 40) - 1
        for name, pin in self.pins.items():
            c = self.cfg.get(name, {})
            active_low = c.get('active', 'low') == 'low'
            level = (not self.pressed.get(name)) if active_low else bool(self.pressed.get(name))
            if not level:
                m &= ~(1 << pin)
        return m


def _routes(bindings, actuators):
    routes = []
    for b in bindings:
        if 'to' not in b:
            continue
        target, _, role = str(b['to']).partition('.')
        if target not in actuators:
            raise KeyError(f"binding {b} points at unknown actuator {target!r}")
        routes.append((pin_number(b['pin']), actuators[target], role or 'signal'))
    return routes


def _pin_signal(out, pin):
    if pin in out['pwm']:
        freq, duty = out['pwm'][pin]
        return {'freq': freq, 'duty': duty}
    if out['enable'] >> pin & 1:
        return {'freq': 0.0, 'duty': float(out['out'] >> pin & 1)}
    return None


class Simulation:
    def __init__(self, project, data, scenario, mcu_factory, frame_every_ms=10):
        self.project = project
        self.data = data
        self.scenario = scenario
        mcfg = data.get('mechanics', {})
        self.mech = Mechanics(project.root, mcfg, project.pcb() if data.get('pcb') else None)
        self.power = PowerNetwork(data.get('power', {}))
        self.actuators = {name: make_actuator(name, cfg) for name, cfg in (data.get('actuators') or {}).items()}
        self.inputs = Inputs(data.get('inputs'), data.get('bindings') or [])
        self.routes = _routes(data.get('bindings') or [], self.actuators)
        mcu = data.get('mcu', {})
        self.step_ms = float(mcu.get('step_ms', 1))
        self.mcu_supply = mcu.get('supply', 'external')
        self.mcu_i = mcu.get('current', {})
        self.bod = BrownoutDetector(mcu.get('brownout', {}))
        self.mcu = mcu_factory(data, scenario)
        self.frame_every = max(1, int(frame_every_ms / self.step_ms))
        self.events = sorted(scenario.get('events') or [], key=lambda e: e['t'])

    def _apply_event(self, e, log):
        if 'press' in e:
            self.inputs.pressed[e['press']] = True
            self._releases.append((e['t'] + float(e.get('hold_s', 0.1)), e['press']))
        elif 'release' in e:
            self.inputs.pressed[e['release']] = False
        elif 'set' in e:
            path = e['set']
            if not path.startswith(RUNTIME_SETTABLE):
                raise ValueError(f'{path} cannot change during a run (only {RUNTIME_SETTABLE}); '
                                 'use scenario overrides instead')
            _, _, name, field = path.split('.', 3)
            kw = {'v': 'v', 'enabled': 'enabled', 'r_ohm': 'r_ohm'}[field]
            self.power.set_source(name, **{kw: e['value']})
        log.append({'t': e['t'], 'event': 'scenario', 'detail': {k: v for k, v in e.items() if k != 't'}})

    def run(self, recorder, progress=None, cancel=None, progress_every_ms=20):
        """progress(fraction, t, signals) is called every progress_every_ms of
        simulated time; setting the threading.Event `cancel` stops the run."""
        try:
            return self._run(recorder, progress, cancel, progress_every_ms)
        finally:
            self.mcu.close()

    def _run(self, recorder, progress, cancel, progress_every_ms):
        duration = float(self.scenario.get('duration_s', 3.0))
        n = int(round(duration * 1000 / self.step_ms))
        dt_m = self.mech.dt
        sub = max(1, int(round(self.step_ms * 1e-3 / dt_m)))
        dt_m = self.step_ms * 1e-3 / sub
        self.mech.model.opt.timestep = dt_m
        self.power.initialise()
        good = self._mcu_rail_v() >= self.bod.v_release if self.mcu_supply != 'external' else True
        self.bod.good = good
        log = []
        self._releases = []
        ev_i = 0
        body_ids = self.mech.visual_bodies()
        recorder.begin(self, body_ids)
        wall0 = time.perf_counter()
        every = max(1, int(round(progress_every_ms / self.step_ms)))
        for k in range(n + 1):
            t = k * self.step_ms * 1e-3
            while ev_i < len(self.events) and self.events[ev_i]['t'] <= t + 1e-9:
                self._apply_event(self.events[ev_i], log)
                ev_i += 1
            for rt, name in list(self._releases):
                if rt <= t + 1e-9:
                    self.inputs.pressed[name] = False
                    self._releases.remove((rt, name))
            out = self.mcu.exchange(t, self.inputs.mask(), self.bod.good)
            for pin, act, role in self.routes:
                sig = _pin_signal(out, pin)
                if role == 'signal':
                    act.set_signal(t, sig)
                else:
                    act.set_signal(t, sig, role)
            v_min = math.inf
            peaks = {}
            for _ in range(sub):
                ts = self.mech.data.time
                loads, gs = {}, {}
                for name, act in self.actuators.items():
                    q, qd = self.mech.joint_state(act.joint)
                    i0, g = act.prepare(ts, q, qd, self.power.node_voltage(act.node), dt_m)
                    loads[act.node] = loads.get(act.node, 0.0) + i0
                    gs[act.node] = gs.get(act.node, 0.0) + g
                i_mcu = self.mcu_i.get('run_a', 0.05) if self.bod.good else self.mcu_i.get('reset_a', 0.02)
                reg_loads = {}
                if self.mcu_supply in self.power.regulators:
                    reg_loads[self.mcu_supply] = i_mcu
                elif self.mcu_supply in self.power.index:
                    loads[self.mcu_supply] = loads.get(self.mcu_supply, 0.0) + i_mcu
                self.power.step(dt_m, loads, reg_loads, gs)
                torques = {}
                for name, act in self.actuators.items():
                    tau = act.finish(self.power.node_voltage(act.node))
                    torques[act.joint] = torques.get(act.joint, 0.0) + tau
                    peaks[name] = max(peaks.get(name, 0.0), abs(act.motor.i))
                self.mech.step(torques)
                v_min = min(v_min, self._mcu_rail_v())
            if self.mcu_supply != 'external':
                was = self.bod.good
                self.bod.update(t, v_min if self.bod.good else self._mcu_rail_v())
                if was and not self.bod.good:
                    log.append({'t': t, 'event': 'brownout', 'v': round(v_min, 4)})
            sig = {'mcu.powered': float(self.bod.good), 'mcu.resets': float(self.mcu.resets),
                   'mcu.rail_v': self._mcu_rail_v() if self.mcu_supply != 'external' else 3.3}
            for pin, act, role in self.routes:
                s = _pin_signal(out, pin)
                sig[f'pin.GPIO{pin}.pulse_us'] = s['duty'] / s['freq'] * 1e6 if s and s['freq'] > 0 else 0.0
            sig.update(self.power.signals())
            for name, act in self.actuators.items():
                sig.update(act.signals())
                sig[f'act.{name}.i_motor_peak'] = peaks.get(name, 0.0)
            sig.update(self.mech.signals())
            frame = k % self.frame_every == 0
            recorder.sample(t, sig, self.mech.poses(body_ids) if frame else None,
                            self.mech.data.qpos.copy() if frame else None)
            if k % every == 0 or k == n:
                if cancel is not None and cancel.is_set():
                    raise RunCancelled()
                if progress is not None:
                    progress(k / n if n else 1.0, t, sig)
        wall = time.perf_counter() - wall0
        return {'log': log, 'wall_s': wall, 'sim_s': duration, 'substeps': sub, 'dt_mech_s': dt_m}

    def _mcu_rail_v(self):
        if self.mcu_supply == 'external':
            return 3.3
        return self.power.node_voltage(self.mcu_supply)


# ---------------------------------------------------------------------------
# metrics and expectations

OPS = {'eq': lambda a, b: a == b, 'ne': lambda a, b: a != b, 'lt': lambda a, b: a < b,
       'le': lambda a, b: a <= b, 'gt': lambda a, b: a > b, 'ge': lambda a, b: a >= b}


def compute_metrics(t, cols, mcu_events, log, mech):
    m = {}
    m['mcu.resets'] = int(cols['mcu.resets'][-1]) if 'mcu.resets' in cols else 0
    m['mcu.brownouts'] = sum(1 for e in log if e.get('event') == 'brownout')
    m['mcu.min_rail_v'] = float(np.min(cols['mcu.rail_v']))
    boots = [rest for _, name, rest in mcu_events if name == 'boot']
    m['mcu.boots'] = len(boots)
    m['mcu.boot_reasons'] = [b.split('reason=')[-1] for b in boots]
    for key, vals in cols.items():
        if key.startswith('node.') and key.endswith('.v'):
            m[f'{key[:-2]}.min_v'] = float(np.min(vals))
        elif key.endswith('.i_motor_peak'):
            m[key.replace('.i_motor_peak', '.peak_current_a')] = float(np.max(vals))
        elif key.endswith('.i_supply'):
            m[key.replace('.i_supply', '.mean_supply_a')] = float(np.mean(vals))
        elif key.startswith('joint.') and (key.endswith('.deg') or key.endswith('.mm')):
            m[f'{key}.max'] = float(np.max(vals))
            m[f'{key}.min'] = float(np.min(vals))
            m[f'{key}.final'] = float(vals[-1])
        elif key.startswith('probe.'):
            m[f'{key}.peak'] = float(np.max(vals))
    for key in cols:
        if key.startswith('act.') and key.endswith('.torque'):
            name = key.split('.')[1]
            stall = cols.get(f'act.{name}.drive')
            if stall is not None:
                m[f'act.{name}.saturated_s'] = float(np.sum(np.abs(stall) >= 0.999) * (t[1] - t[0]))
    counts = {}
    for _, name, _ in mcu_events:
        counts[name] = counts.get(name, 0) + 1
    m['events'] = counts
    m['mech.warnings'] = dict(mech.warnings)
    return m


def evaluate(expect, t, cols, metrics):
    results = []
    for item in expect or []:
        op = next((k for k in OPS if k in item), None)
        target = item.get(op)
        if 'metric' in item:
            key = item['metric']
            value = metrics[key] if key in metrics else get_path(metrics, key, None)
            label = item['metric']
        elif 'event' in item:
            value = metrics['events'].get(item['event'], 0)
            label = f"count(EVT {item['event']})"
        elif 'signal' in item:
            y = cols.get(item['signal'])
            if y is None:
                results.append({**item, 'pass': False, 'error': f"unknown signal {item['signal']}"})
                continue
            if 'at' in item:
                value = float(np.interp(item['at'], t, y))
                label = f"{item['signal']}@{item['at']}s"
            else:
                w0, w1 = item.get('window', [t[0], t[-1]])
                sel = (t >= w0) & (t <= w1)
                stat = item.get('stat', 'max')
                value = float({'max': np.max, 'min': np.min, 'mean': np.mean,
                               'final': lambda a: a[-1]}[stat](y[sel]))
                label = f"{stat}({item['signal']}, {w0}..{w1}s)"
        else:
            results.append({**item, 'pass': False, 'error': 'expectation needs metric/event/signal'})
            continue
        ok = value is not None and op is not None and OPS[op](value, target)
        results.append({'check': label, 'op': op, 'target': target,
                        'value': value if not isinstance(value, float) else round(value, 5),
                        'pass': bool(ok), 'note': item.get('note', '')})
    return results
