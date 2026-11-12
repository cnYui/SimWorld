"""Measurements -> fitted model parameters -> project.yaml.

The user measures a few things on the real hardware with household tools
(USB current meter, multimeter, phone slow-motion video, kitchen scale; see
examples/switch_robot/measurements/HOWTO.md) and writes them into a
measurement sheet (measurements/template.yaml). This module fits the
parameters of the models the co-simulator actually runs:

  servo   r_ohm, ke_v_s_rad (with gear_ratio kept), efficiency, i_ctrl_a and,
          when the measured speed is slower than the motor allows, the
          position-loop speed limit max_speed_dps          (electrical/motors.py)
  supply  v, r_ohm, i_limit_a of one power.sources entry    (electrical/power.py)
  switch  peak_force_n (or peak_torque_nm) and stable_deg of a bistable
          element (mechanics.Bistable)

Every residual is evaluated by running the model classes themselves on a
small bench (Servo + PowerNetwork + a one-joint integrator, Bistable on a
stub joint, PowerNetwork DC operating points); closed-form relations are only
used as starting points. Parameters the measurements do not determine keep
their prior and are listed under `kept_prior` with the reason.

    fit_measurements(project, 'measurements/bench.yaml')              # report only
    fit_measurements(project, 'measurements/bench.yaml', apply=True)  # write project.yaml

compare_run() compares a stored run with a measured trace (CSV from a scope or
a logging multimeter) and returns the RMS error.
"""
from __future__ import annotations

import copy
import math
import time
from pathlib import Path

import numpy as np
import yaml

from .electrical.motors import Servo, merged
from .electrical.power import PowerNetwork
from .project import Project, get_path, set_path

G = 9.80665

# Output-side load of the servo during the unloaded move/sweep tests: the
# servo_axis joint of examples/switch_robot/mechanics/switch_robot.xml without
# the press arm (armature 3e-6 = reflected rotor, plus ~2e-7 for a plastic horn),
# its damping and friction loss. Override per sheet with servo.move.load_*.
BENCH_LOAD = {'inertia_kg_m2': 3.2e-6, 'damping_nms_per_rad': 1.5e-4, 'friction_nm': 7e-4}
BENCH_DT = 1.25e-4                     # same as the example's mechanics sub-step

# Default 1-sigma uncertainties used when a sheet gives a bare number.
DEFAULT_REL_UNC = 0.05


# ------------------------------------------------------------- utilities ----

def _mv(x, default_unc=None):
    """Read a measured quantity: {value, unc} | number | None -> (value, unc) or None."""
    if x is None:
        return None
    if isinstance(x, dict):
        v = x.get('value')
        if v is None:
            return None
        u = x.get('unc')
        v = float(v)
        u = float(u) if u is not None else (default_unc if default_unc is not None else abs(v) * DEFAULT_REL_UNC)
        return v, max(u, 1e-12)
    v = float(x)
    return v, max(default_unc if default_unc is not None else abs(v) * DEFAULT_REL_UNC, 1e-12)


def _sig(x, n=4):
    if x is None or not isinstance(x, (int, float)) or x == 0 or not math.isfinite(x):
        return x
    return float(f'{x:.{n}g}')


def load_measurements(path):
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding='utf8')) or {}
    if not isinstance(data, dict):
        raise ValueError(f'{path}: measurement sheet must be a YAML mapping')
    return data


# ------------------------------------------------------------ servo bench ----

def _servo(p, extra=None):
    cfg = dict(p)
    cfg.update({'joint': 'j', 'node': 'n', 'angle_limit_deg': 360.0, 'signal_timeout_s': 1e9})
    cfg.update(extra or {})
    return Servo('bench', cfg)


def _net(v_source, r_source, i_limit=None, c_f=1e-5):
    src = {'node': 'n', 'v': float(v_source), 'r_ohm': float(r_source)}
    if i_limit:
        src['i_limit_a'] = float(i_limit)
    net = PowerNetwork({'nodes': {'n': {'c_f': c_f}}, 'sources': {'s': src}})
    net.initialise()
    return net


def bench_stall(p, v_source, r_source=1e-3, i_limit=None, duration=0.05, dt=BENCH_DT):
    """Servo commanded far away with its horn blocked. Returns the steady supply
    current, motor current, output torque and supply-node voltage."""
    s = _servo(p)
    s.target = math.radians(60.0)
    s.last_valid = 0.0
    net = _net(v_source, r_source, i_limit)
    tau = 0.0
    for k in range(int(round(duration / dt))):
        i0, g = s.prepare(k * dt, 0.0, 0.0, net.node_voltage('n'), dt)
        net.step(dt, {'n': i0}, {}, {'n': g})
        tau = s.finish(net.node_voltage('n'))
    return {'i_supply_a': s.i_supply, 'i_motor_a': s.motor.i, 'torque_nm': abs(tau),
            'v_node_v': net.node_voltage('n')}


def _joint_step(q, qd, tau, load, dt):
    J, b, f = load['inertia_kg_m2'], load['damping_nms_per_rad'], load['friction_nm']
    qd = qd + (tau - b * qd) / J * dt
    imp = f * dt / J
    qd = 0.0 if abs(qd) <= imp else qd - math.copysign(imp, qd)
    return q + qd * dt, qd


def bench_move(p, angle_deg, v_source, r_source=1e-3, load=None, start_deg=1.0, end_deg=2.0,
               t_max=1.5, dt=BENCH_DT, trace=False):
    """Unloaded step of `angle_deg`, starting at rest at -angle/2. Returns the time
    from the first `start_deg` of motion to the first arrival within `end_deg`
    of the target -- what a slow-motion video measures."""
    load = {**BENCH_LOAD, **(load or {})}
    a = math.radians(angle_deg)
    q, qd = -a / 2, 0.0
    s = _servo(p)
    s.target = q
    s.last_valid = 0.0
    net = _net(v_source, r_source)
    # settle on the start position (also primes the rate limiter / gear model)
    for k in range(int(0.05 / dt)):
        i0, g = s.prepare(k * dt, q, qd, net.node_voltage('n'), dt)
        net.step(dt, {'n': i0}, {}, {'n': g})
        q, qd = _joint_step(q, qd, s.finish(net.node_voltage('n')), load, dt)
    q0 = q
    s.target = a / 2
    t_start = t_end = None
    prev_t, prev_q = 0.0, q
    tr = []
    for k in range(1, int(t_max / dt)):
        t = k * dt
        i0, g = s.prepare(t, q, qd, net.node_voltage('n'), dt)
        net.step(dt, {'n': i0}, {}, {'n': g})
        q, qd = _joint_step(q, qd, s.finish(net.node_voltage('n')), load, dt)
        if trace:
            tr.append((t, math.degrees(q), s.i_supply, net.node_voltage('n')))
        d0 = math.radians(start_deg)
        if t_start is None and q - q0 >= d0:
            t_start = prev_t + (q0 + d0 - prev_q) / (q - prev_q) * dt if q != prev_q else t
        d1 = a / 2 - math.radians(end_deg)
        if t_end is None and q >= d1:
            t_end = prev_t + (d1 - prev_q) / (q - prev_q) * dt if q != prev_q else t
            if not trace:
                break
        prev_t, prev_q = t, q
    out = {'time_s': (t_end - t_start) if (t_start is not None and t_end is not None) else float('inf')}
    if trace:
        out['trace'] = np.array(tr)
    return out


def bench_sweep_current(p, angle_deg, period_s, v_source, r_source=1e-3, load=None, cycles=1,
                        dt=BENCH_DT):
    """Mean supply current while the unloaded servo sweeps back and forth between
    +/-angle/2 every half period (what a USB meter shows during a sweep test).
    Starts settled at -angle/2, which is where every full cycle starts too."""
    load = {**BENCH_LOAD, **(load or {})}
    a = math.radians(angle_deg) / 2
    q, qd = -a, 0.0
    s = _servo(p)
    s.last_valid = 0.0
    s.target = -a
    net = _net(v_source, r_source)
    for k in range(int(0.02 / dt)):                 # settle (rate limiter, gear model)
        i0, g = s.prepare(k * dt, q, qd, net.node_voltage('n'), dt)
        net.step(dt, {'n': i0}, {}, {'n': g})
        q, qd = _joint_step(q, qd, s.finish(net.node_voltage('n')), load, dt)
    n = int(round(cycles * period_s / dt))
    acc = 0.0
    for k in range(n):
        t = k * dt
        s.target = a if (t % period_s) < period_s / 2 else -a
        i0, g = s.prepare(t, q, qd, net.node_voltage('n'), dt)
        net.step(dt, {'n': i0}, {}, {'n': g})
        q, qd = _joint_step(q, qd, s.finish(net.node_voltage('n')), load, dt)
        acc += s.i_supply
    return acc / max(n, 1)


# -------------------------------------------------------------- servo fit ----

def _servo_with(p0, x, free):
    p = dict(p0)
    N = float(p.get('gear_ratio', 1.0))
    for name, val in zip(free, x):
        if name == 'R':
            p['r_ohm'] = float(val)
        elif name == 'KeN':
            p['ke_v_s_rad'] = float(val) / N
        elif name == 'eta':
            p['efficiency'] = float(val)
        elif name == 'vmax':
            p['max_speed_dps'] = float(val)
        elif name == 'fric':
            p['_friction_nm'] = float(val)
    return p


def _load(p, extra):
    load = dict(extra or {})
    if p.get('_friction_nm') is not None:
        load['friction_nm'] = p['_friction_nm']
    return load


def _servo_predictions(p, sm, supply_r):
    """Model predictions for each servo measurement present in the sheet."""
    out = {}
    if sm.get('idle'):
        out['idle_current_a'] = float(p['i_ctrl_a'])
    st = sm.get('stall')
    if st:
        b = bench_stall(p, st['v'][0])
        out['stall_current_a'] = b['i_supply_a']
        if st.get('torque'):
            out['stall_torque_nm'] = b['torque_nm']
    for mv in sm.get('moves') or []:
        out[mv['key']] = bench_move(p, mv['angle'][0], mv['v'][0], supply_r, _load(p, mv['load']),
                                    mv['start_deg'], mv['end_deg'])['time_s']
    sw = sm.get('sweep')
    if sw:
        out['sweep_current_a'] = bench_sweep_current(p, sw['angle'], sw['period'], sw['v'], supply_r,
                                                     _load(p, sw['load']))
    return out


def _servo_sheet(sheet):
    """Normalise the servo part of a measurement sheet."""
    s = sheet.get('servo') or {}
    out = {}
    idle = _mv(s.get('idle_current_a'), 0.003)
    if idle:
        out['idle'] = idle
    st = s.get('stall') or {}
    i = _mv(st.get('current_a'), 0.02)
    v = _mv(st.get('v_servo_v'), 0.05)
    if i and v:
        d = {'i': i, 'v': v}
        tq = st.get('torque') or {}
        f = _mv(tq.get('force_g'))
        lever = _mv(tq.get('lever_m'), 0.001)
        if f and lever:
            tau = f[0] * 1e-3 * G * lever[0]
            rel = math.hypot(f[1] / f[0], lever[1] / lever[0])
            d['torque'] = (tau, tau * rel)
        out['stall'] = d
    raw = s.get('move')
    raw = ([raw] if isinstance(raw, dict) else list(raw or [])) + list(s.get('moves') or [])
    moves = []
    for mv in raw:
        fps = float(mv.get('fps') or 240)
        t = _mv(mv.get('time_s'), 1.5 / fps)          # +/- about 1.5 frames by default
        if not t:
            continue
        load = {k: float(mv[f'load_{k}']) for k in BENCH_LOAD if mv.get(f'load_{k}') is not None}
        v = _mv(mv.get('v_servo_v'), 0.05) or (5.0, 0.1)
        moves.append({'t': t, 'angle': _mv(mv.get('angle_deg'), 1.0) or (90.0, 1.0), 'v': v, 'load': load,
                      'start_deg': float(mv.get('start_threshold_deg', 1.0)),
                      'end_deg': float(mv.get('end_threshold_deg', 2.0)),
                      'key': f'move_time_s@{v[0]:.2f}V' + (f'#{len(moves) + 1}' if len(raw) > 1 else '')})
    if moves:
        out['moves'] = moves
    sw = s.get('sweep') or {}
    isw = _mv(sw.get('current_a'), 0.02)
    if isw and sw.get('period_s'):
        out['sweep'] = {'i': isw, 'angle': float(sw.get('angle_deg', 90.0)), 'period': float(sw['period_s']),
                        'v': float((_mv(sw.get('v_servo_v')) or (5.0, 0))[0]),
                        'load': {k: float(sw[f'load_{k}']) for k in BENCH_LOAD if sw.get(f'load_{k}') is not None}}
    return out


def fit_servo(sheet, p_prior, supply_r=1e-3, friction_prior=None):
    """Fit servo parameters. p_prior: merged actuator parameters (preset + overrides).
    supply_r: resistance of the source feeding the servo during the move/sweep
    tests (their voltage is read at the servo connector while idle).
    friction_prior: output-joint friction loss [N*m] of the project (MuJoCo
    frictionloss of the servo joint); fitted from the sweep current when given."""
    from scipy.optimize import least_squares

    sm = _servo_sheet(sheet)
    s_raw = sheet.get('servo') or {}
    p0 = dict(p_prior)
    N = float(p0.get('gear_ratio', 1.0))
    ken0 = float(p0['ke_v_s_rad']) * N
    out = {'fitted': {}, 'sigma': {}, 'kept_prior': {}, 'residuals': [], 'notes': []}
    if not sm and not s_raw.get('backlash_deg') and not s_raw.get('compliance'):
        out['notes'].append('no servo measurements in the sheet')
        return out

    p = dict(p0)
    fric0 = float(friction_prior if friction_prior is not None else BENCH_LOAD['friction_nm'])
    p['_friction_nm'] = fric0
    p0 = dict(p)
    if sm.get('idle'):
        p['i_ctrl_a'] = sm['idle'][0]
        out['fitted']['i_ctrl_a'] = sm['idle'][0]
        out['sigma']['i_ctrl_a'] = sm['idle'][1]
    else:
        out['kept_prior']['i_ctrl_a'] = 'no idle (holding, unloaded) current measured'

    st, moves = sm.get('stall'), sm.get('moves') or []
    mv = moves[0] if moves else None
    free, x0, lo, hi = [], [], [], []
    if st:
        free.append('R')
        x0.append(max(st['v'][0] / max(st['i'][0] - p['i_ctrl_a'], 1e-3), 0.6))
        lo.append(0.5)
        hi.append(100.0)
    else:
        out['kept_prior']['r_ohm'] = 'no stall current at a known servo voltage'
    if mv:
        free.append('KeN')
        x0.append(ken0)
        lo.append(0.02)
        hi.append(10.0)
    else:
        out['kept_prior']['ke_v_s_rad'] = 'no speed measurement (move time) -> back-EMF constant unknown'
    if st and st.get('torque'):
        free.append('eta')
        r0 = x0[0]
        i_m = st['v'][0] / r0
        ken_guess = x0[free.index('KeN')] if 'KeN' in free else ken0
        x0.append(min(max(st['torque'][0] / (ken_guess * i_m), 0.06), 0.99))
        lo.append(0.05)
        hi.append(1.0)
    else:
        out['kept_prior']['efficiency'] = ('no stall torque measured (lever + scale) -> gear efficiency '
                                           'cannot be separated from the torque constant')
    if sm.get('sweep'):
        free.append('fric')
        x0.append(max(fric0, 1e-4))
        lo.append(0.0)
        hi.append(0.1)
    else:
        out['kept_prior']['friction_nm'] = 'no sweep (moving, unloaded) current measured'
    # backlash and output compliance are read directly (model definitions in motors.Transmission)
    bl = _mv(s_raw.get('backlash_deg'), 0.5)
    if bl:
        out['fitted']['backlash_deg'] = max(bl[0], 0.0)
        p['backlash_deg'] = max(bl[0], 0.0)
        out['sigma']['backlash_deg'] = bl[1]
    cp = s_raw.get('compliance') or {}
    cf, cl, cd = _mv(cp.get('force_g')), _mv(cp.get('lever_m'), 0.001), _mv(cp.get('deflection_deg'), 0.5)
    if cf and cl and cd and cd[0] > 0:
        tau = cf[0] * 1e-3 * G * cl[0]
        k = tau / math.radians(cd[0])
        out['fitted']['output_stiffness_nm_per_rad'] = k
        p['output_stiffness_nm_per_rad'] = k
        out['sigma']['output_stiffness_nm_per_rad'] = k * math.sqrt((cf[1] / cf[0]) ** 2 + (cl[1] / cl[0]) ** 2
                                                                    + (cd[1] / cd[0]) ** 2)
    if bl or (cf and cl and cd):
        out['notes'].append('backlash/compliance enabled: move the servo joint armature (reflected rotor '
                            'inertia) into actuators.<servo>.gear_inertia_kg_m2 so it is not counted twice')
    out['kept_prior']['gear_ratio'] = ('not observable from the terminals; only ke*gear_ratio matters '
                                       f'(ke_v_s_rad is fitted with gear_ratio={N:g} kept)')

    meas = {}
    if st:
        meas['stall_current_a'] = st['i']
        if st.get('torque'):
            meas['stall_torque_nm'] = st['torque']
    for m_ in moves:
        meas[m_['key']] = m_['t']
    if sm.get('sweep'):
        meas['sweep_current_a'] = sm['sweep']['i']

    def resid(x, free_):
        pp = _servo_with(p, x, free_)
        pred = _servo_predictions(pp, {k: v for k, v in sm.items() if k in ('stall', 'moves', 'sweep')}, supply_r)
        r = []
        for k, (val, unc) in meas.items():
            y = pred.get(k, float('inf'))
            y = min(y, 1e3) if math.isfinite(y) else 1e3     # e.g. the move never arrives
            r.append((y - val) / unc)
        return np.array(r)

    def solve(free_, x0_, lo_, hi_):
        if not free_:
            return np.array([]), None
        x0_ = np.clip(x0_, np.array(lo_) * 1.0001, np.array(hi_) * 0.9999)
        sol = least_squares(resid, x0_, args=(free_,), bounds=(lo_, hi_), x_scale=np.maximum(np.abs(x0_), 1e-3),
                            diff_step=1e-3, xtol=1e-8, ftol=1e-10)
        return sol.x, sol

    x, sol = solve(free, x0, lo, hi)
    mode = 'motor-limited'
    if 'eta' in free and 'KeN' in free:
        eta_fit = x[free.index('eta')]
        if eta_fit > 0.97:
            out['notes'].append('the move is faster than the measured stall torque allows at <=100 % gear '
                                'efficiency: check the torque lever/scale reading and the video timing')
        # Alternative hypothesis: the servo electronics limit the speed (slow-move or
        # digital servo), so the move time says nothing about the motor's back-EMF.
        # Then efficiency and torque constant are not separable: keep the efficiency
        # prior, take the torque constant from the stall torque, fit max_speed_dps.
        free2 = [n for n in free if n != 'eta'] + ['vmax']
        i_m = st['v'][0] / x[0]
        ken_t = st['torque'][0] / (float(p['efficiency']) * i_m)
        a, t = mv['angle'][0], mv['t'][0]
        x0_2, lo_2, hi_2 = [], [], []
        for n in free2:
            if n == 'KeN':
                x0_2.append(min(max(ken_t, 0.021), 9.9)); lo_2.append(0.02); hi_2.append(10.0)
            elif n == 'vmax':
                x0_2.append(0.8 * a / t); lo_2.append(10.0); hi_2.append(5000.0)
            else:
                j = free.index(n)
                x0_2.append(x[j]); lo_2.append(lo[j]); hi_2.append(hi[j])
        x2, sol2 = solve(free2, x0_2, lo_2, hi_2)
        chi1, chi2 = float(sol.fun @ sol.fun), float(sol2.fun @ sol2.fun)
        implausible = eta_fit < 0.2
        if chi2 < chi1 - 1.0 or (implausible and chi2 <= chi1 + 1.0):
            mode = 'speed-limited'
            x, free, sol = x2, free2, sol2
            out['kept_prior']['efficiency'] = ('speed-limited servo: torque constant and efficiency are not '
                                               'separable, efficiency kept, torque constant from stall torque')
            out['notes'].append('the move is slower than the motor allows: fitted a position-loop speed limit '
                                f'(max_speed_dps); motor-limited alternative had chi2={chi1:.3g} '
                                f'(gear efficiency {eta_fit:.2f}) vs {chi2:.3g}')
        elif chi2 <= chi1 + 1.0:
            out['notes'].append('a speed-limited servo (max_speed_dps) explains the data equally well '
                                f'(chi2 {chi2:.3g} vs {chi1:.3g}); kept the motor-limited fit. A second move video '
                                'at a clearly different supply voltage separates the two (a motor-limited '
                                'servo slows down in proportion, a speed-limited one does not)')
    out['mode'] = mode

    p_fit = _servo_with(p, x, free)
    names = {'R': 'r_ohm', 'KeN': 'ke_v_s_rad', 'eta': 'efficiency', 'vmax': 'max_speed_dps',
             'fric': 'friction_nm'}
    cov = None
    if sol is not None and len(sol.fun) >= len(free):
        try:
            Jm = sol.jac
            cov = np.linalg.inv(Jm.T @ Jm)
        except np.linalg.LinAlgError:
            cov = None
    for k, name in enumerate(free):
        key = names[name]
        val = float(x[k]) / (N if name == 'KeN' else 1.0)
        out['fitted'][key] = val
        if cov is not None and np.isfinite(cov[k, k]):
            out['sigma'][key] = float(math.sqrt(max(cov[k, k], 0.0))) / (N if name == 'KeN' else 1.0)

    before = _servo_predictions(p0, sm, supply_r)
    after = _servo_predictions(p_fit, sm, supply_r)
    meas_all = dict(meas)
    if sm.get('idle'):
        meas_all['idle_current_a'] = sm['idle']
    units = {'idle_current_a': 'A', 'stall_current_a': 'A', 'stall_torque_nm': 'N*m', 'move_time_s': 's',
             'sweep_current_a': 'A'}
    for k, (val, unc) in meas_all.items():
        out['residuals'].append({'quantity': f'servo.{k}', 'unit': units[k.split('@')[0]], 'measured': _sig(val),
                                 'unc': _sig(unc), 'model_before': _sig(before.get(k)),
                                 'model_after': _sig(after.get(k)),
                                 'z_after': _sig((after.get(k, float('nan')) - val) / unc, 3),
                                 'used_in_fit': True})
    if sm.get('sweep'):
        out['notes'].append('friction_nm (no-load losses, from the sweep current) is the MuJoCo frictionloss '
                            'of the servo joint; it lumps motor brush/bearing and gear drag at the output')
    out['derived'] = {
        'ke_times_gear_v_s_per_rad': _sig(float(p_fit['ke_v_s_rad']) * N),
        'kt_output_nm_per_a': _sig(float(p_fit['ke_v_s_rad']) * N * float(p_fit['efficiency'])),
        'no_load_speed_dps_at_5v': _sig(math.degrees(5.0 / (float(p_fit['ke_v_s_rad']) * N))),
        'stall_torque_nm_at_5v': _sig(float(p_fit['efficiency']) * float(p_fit['ke_v_s_rad']) * N * 5.0
                                      / float(p_fit['r_ohm'])),
        'stall_current_a_at_5v': _sig(5.0 / float(p_fit['r_ohm']) + float(p_fit['i_ctrl_a'])),
    }
    out['params'] = {k: v for k, v in p_fit.items() if not k.startswith('_')}
    return out


# ------------------------------------------------------------- supply fit ----

def dc_point(v, r, i_limit, load):
    """DC operating point of a source (PowerNetwork) with a resistor
    {'r_load_ohm'} or constant-current {'current_a'} load. Returns (v_node, i)."""
    src = {'node': 'n', 'v': v, 'r_ohm': r}
    if i_limit:
        src['i_limit_a'] = i_limit
    net = PowerNetwork({'nodes': {'n': {'c_f': 0.0}}, 'sources': {'s': src}})
    i_load = float(load.get('current_a') or 0.0)
    g = 1.0 / float(load['r_load_ohm']) if load.get('r_load_ohm') else 0.0
    for _ in range(4):
        net.step(1.0, {'n': i_load}, {}, {'n': g} if g else None)
    vn = net.node_voltage('n')
    return vn, net.source_current('s')


def _supply_points(sheet):
    s = sheet.get('supply') or {}
    pts = []
    oc = _mv(s.get('open_circuit_v'), 0.02)
    if oc:
        pts.append({'label': 'open circuit', 'load': {'current_a': 0.0}, 'v': oc, 'i': None})
    for k, p in enumerate(s.get('points') or []):
        v = _mv(p.get('voltage_v'), 0.03)
        i = _mv(p.get('current_a'), 0.02)
        if p.get('r_load_ohm'):
            load = {'r_load_ohm': float(p['r_load_ohm'])}
        elif i:
            load = {'current_a': i[0]}
            i = None                    # used as the load, not as an observation
        else:
            continue
        if v is None and i is None:
            continue
        pts.append({'label': p.get('label') or f'point {k + 1}', 'load': load, 'v': v, 'i': i})
    return pts


def fit_supply(sheet, prior):
    """Fit v, r_ohm and (when a point shows it) i_limit_a of one source."""
    from scipy.optimize import least_squares

    pts = _supply_points(sheet)
    out = {'fitted': {}, 'sigma': {}, 'kept_prior': {}, 'residuals': [], 'notes': []}
    s = sheet.get('supply') or {}
    if len(pts) < 2:
        if pts or s:
            out['notes'].append('supply: need the open-circuit voltage and at least one loaded point')
        return out
    v_prior, r_prior = float(prior.get('v', 5.0)), float(prior.get('r_ohm', 0.1))
    il_prior = float(prior['i_limit_a']) if prior.get('i_limit_a') else None

    def resid(x, limited):
        v, r = x[0], x[1]
        il = x[2] if limited else None
        res = []
        for p in pts:
            vn, i = dc_point(v, r, il, p['load'])
            if p['v']:
                res.append((vn - p['v'][0]) / p['v'][1])
            if p['i']:
                res.append((i - p['i'][0]) / p['i'][1])
        return np.array(res)

    oc = next((p['v'][0] for p in pts if p['label'] == 'open circuit'), v_prior)
    sol0 = least_squares(resid, [oc, max(r_prior, 0.01)], args=(False,), bounds=([0.5, 1e-4], [30.0, 50.0]))
    chi0 = float(sol0.fun @ sol0.fun)
    # largest current the source delivered at any point (no-limit fit)
    i_obs = [dc_point(sol0.x[0], sol0.x[1], None, p['load'])[1] for p in pts]
    i_max = max(i_obs)
    best, limited = sol0, False
    nobs = len(sol0.fun)
    if nobs >= 3:
        cands = []
        for il0 in sorted(set([0.5 * i_max, 0.8 * i_max] + [x for x in i_obs if x > 0.05])):
            try:
                cands.append(least_squares(resid, [oc, max(sol0.x[1] * 0.7, 1e-3), max(il0, 0.02)], args=(True,),
                                           bounds=([0.5, 1e-4, 0.01], [30.0, 50.0, 20.0])))
            except ValueError:
                pass
        if cands:
            sol1 = min(cands, key=lambda z: float(z.fun @ z.fun))
            chi1 = float(sol1.fun @ sol1.fun)
            n_lim = sum(1 for p in pts if dc_point(*sol1.x, p['load'])[1] >= sol1.x[2] * 0.999)
            if chi0 > max(4.0, nobs) and chi1 < 0.5 * chi0 and n_lim >= 1:
                best, limited = sol1, True
    seen = s.get('current_limit_seen')
    names = ['v', 'r_ohm'] + (['i_limit_a'] if limited else [])
    try:
        cov = np.linalg.inv(best.jac.T @ best.jac)
    except np.linalg.LinAlgError:
        cov = None
    for k, n in enumerate(names):
        out['fitted'][n] = float(best.x[k])
        if cov is not None and np.isfinite(cov[k, k]):
            out['sigma'][n] = float(math.sqrt(max(cov[k, k], 0.0)))
    if not limited:
        i_del = max(dc_point(best.x[0], best.x[1], None, p['load'])[1] for p in pts)
        if il_prior is not None and il_prior < i_del:
            out['fitted']['i_limit_a'] = i_del
            out['notes'].append(f'the source delivered {i_del:.3f} A without limiting, above the prior limit '
                                f'{il_prior} A: i_limit_a raised to that value, which is only a lower bound')
        else:
            out['kept_prior']['i_limit_a'] = (f'no point reached a current limit (max {i_del:.3f} A delivered); '
                                              'the limit, if any, is above that')
        if seen is True:
            out['notes'].append('sheet says a current limit was seen, but the points fit a plain resistive source; '
                                'add a point with the voltage/current while limiting')
    elif seen is False:
        out['notes'].append('the points fit a current-limited source although the sheet says no limit was seen')
    x = best.x
    x_before = [v_prior, r_prior]
    for p in pts:
        vb, ib = dc_point(x_before[0], x_before[1], il_prior, p['load'])
        va, ia = dc_point(x[0], x[1], x[2] if limited else out['fitted'].get('i_limit_a', il_prior), p['load'])
        if p['v']:
            out['residuals'].append({'quantity': f"supply.{p['label']}.v", 'unit': 'V', 'measured': _sig(p['v'][0]),
                                     'unc': _sig(p['v'][1]), 'model_before': _sig(vb), 'model_after': _sig(va),
                                     'z_after': _sig((va - p['v'][0]) / p['v'][1], 3), 'used_in_fit': True})
        if p['i']:
            out['residuals'].append({'quantity': f"supply.{p['label']}.i", 'unit': 'A', 'measured': _sig(p['i'][0]),
                                     'unc': _sig(p['i'][1]), 'model_before': _sig(ib), 'model_after': _sig(ia),
                                     'z_after': _sig((ia - p['i'][0]) / p['i'][1], 3), 'used_in_fit': True})
    out['limited'] = limited
    return out


# ------------------------------------------------------------- switch fit ----

class _StubData:
    def __init__(self, q):
        self.qpos = np.array([q])
        self.qfrc_applied = np.zeros(1)


def bistable_torque(cfg, q):
    """Torque of mechanics.Bistable at joint angle q [rad] (the model class itself)."""
    from .mechanics import Bistable
    b = Bistable(None, {'j': (0, 0, 0)}, {**cfg, 'joint': 'j'})
    d = _StubData(q)
    b.apply(d)
    return float(d.qfrc_applied[0])


def bistable_peak(cfg):
    """(peak torque [N*m], angle of the peak measured from the rest position [deg])."""
    q0 = math.radians(float(cfg['stable_deg']))
    qs = np.linspace(0.0, q0, 2001)
    tau = np.array([bistable_torque(cfg, q) for q in qs])
    k = int(np.argmax(tau))
    return float(tau[k]), math.degrees(q0 - qs[k])


def fit_switch(sheet, prior):
    sw = sheet.get('switch') or {}
    out = {'fitted': {}, 'sigma': {}, 'kept_prior': {}, 'residuals': [], 'notes': []}
    if not sw:
        return out
    lever_el = float(prior.get('lever_m') or 0.0)
    trials = sw.get('force_peak_g_trials')
    if trials:
        arr = np.array([float(x) for x in trials])
        f_g = (float(arr.mean()), float(max(arr.std(ddof=1) / math.sqrt(len(arr)) if len(arr) > 1 else 0.0,
                                            float(sw.get('scale_resolution_g', 1.0)))))
    else:
        f_g = _mv(sw.get('force_peak_g'), float(sw.get('scale_resolution_g', 2.0)))
    lever = _mv(sw.get('press_lever_m'), 0.001)
    travel = None
    if sw.get('travel_deg') is not None:
        travel = _mv(sw.get('travel_deg'), 0.5)
    elif sw.get('travel_mm') is not None and sw.get('travel_lever_m') is not None:
        tm = _mv(sw.get('travel_mm'), 0.2)
        tl = _mv(sw.get('travel_lever_m'), 0.001)
        ang = math.degrees(2 * math.atan(tm[0] * 1e-3 / 2 / tl[0]))
        travel = (ang, ang * math.hypot(tm[1] / tm[0], tl[1] / tl[0]))
    new = dict(prior)
    if travel:
        new['stable_deg'] = travel[0] / 2
        out['fitted']['stable_deg'] = travel[0] / 2
        out['sigma']['stable_deg'] = travel[1] / 2
    else:
        out['kept_prior']['stable_deg'] = 'no rocker travel measured'
    if f_g and lever:
        tau = f_g[0] * 1e-3 * G * lever[0]
        rel = math.hypot(f_g[1] / f_g[0], lever[1] / lever[0])
        # scale the element so the model's own peak (Bistable) equals the measured peak torque
        tmp = dict(new)
        if prior.get('peak_torque_nm') is not None or not lever_el:
            tmp['peak_torque_nm'] = 1.0
            unit_peak = bistable_peak(tmp)[0]
            out['fitted']['peak_torque_nm'] = tau / unit_peak
            out['sigma']['peak_torque_nm'] = tau / unit_peak * rel
        else:
            tmp['peak_force_n'] = 1.0
            unit_peak = bistable_peak(tmp)[0]
            out['fitted']['peak_force_n'] = tau / unit_peak
            out['sigma']['peak_force_n'] = tau / unit_peak * rel
        new.update({k: v for k, v in out['fitted'].items() if k.startswith('peak_')})
        before_tau = bistable_peak(prior)[0]
        after_tau = bistable_peak(new)[0]
        out['residuals'].append({'quantity': 'switch.peak_torque_nm', 'unit': 'N*m', 'measured': _sig(tau),
                                 'unc': _sig(tau * rel), 'model_before': _sig(before_tau),
                                 'model_after': _sig(after_tau),
                                 'z_after': _sig((after_tau - tau) / (tau * rel), 3), 'used_in_fit': True})
        out['notes'].append(f'measured peak torque {tau * 1e3:.1f} mN*m at the pivot '
                            f'({f_g[0]:.0f} g at {lever[0] * 1e3:.1f} mm)')
    else:
        out['kept_prior']['peak_force_n'] = 'no peak force with its lever distance measured'
    pk = _mv(sw.get('peak_at_deg_from_rest'), 0.5)
    if pk:
        _, ang_b = bistable_peak(prior)
        _, ang_a = bistable_peak(new)
        out['residuals'].append({'quantity': 'switch.peak_at_deg_from_rest', 'unit': 'deg', 'measured': _sig(pk[0]),
                                 'unc': _sig(pk[1]), 'model_before': _sig(ang_b), 'model_after': _sig(ang_a),
                                 'z_after': _sig((ang_a - pk[0]) / pk[1], 3), 'used_in_fit': False})
        out['notes'].append('the quartic bistable has a fixed shape (peak at 42 % of the travel to the centre); '
                            'the peak position is a check, not a fitted parameter')
    out['params'] = new
    return out


# ------------------------------------------------------ brown-out check ----

def brownout_check(data, actuator, sheet, duration=0.3, dt=BENCH_DT):
    """Steady servo stall on its supply node with the project's power network and
    MCU load: does the MCU rail fall below the brown-out trip level?"""
    bo = sheet.get('brownout') or {}
    if not bo or bo.get('esp32_reset') is None:
        return None
    acfg = (data.get('actuators') or {}).get(actuator)
    if not acfg:
        return None
    node = bo.get('servo_node') or acfg['node']
    net = PowerNetwork(data.get('power', {}))
    net.initialise()
    s = Servo('bench', {**merged(acfg), 'joint': 'j', 'node': node, 'angle_limit_deg': 360.0,
                        'signal_timeout_s': 1e9})
    s.target, s.last_valid = math.radians(60.0), 0.0
    mcu = data.get('mcu', {})
    supply = mcu.get('supply', 'external')
    i_mcu = (mcu.get('current') or {}).get('run_a', 0.05)
    v_trip = float((mcu.get('brownout') or {}).get('v_trip', 2.43))
    v_min = math.inf
    for k in range(int(duration / dt)):
        i0, g = s.prepare(k * dt, 0.0, 0.0, net.node_voltage(node), dt)
        loads, regs = {node: i0}, {}
        if supply in net.regulators:
            regs[supply] = i_mcu
        elif supply in net.index:
            loads[supply] = loads.get(supply, 0.0) + i_mcu
        net.step(dt, loads, regs, {node: g})
        s.finish(net.node_voltage(node))
        if supply != 'external':
            v_min = min(v_min, net.node_voltage(supply))
    if supply == 'external':
        return {'check': 'brownout', 'note': 'MCU supply is external in the project; nothing to compare'}
    predicted = v_min < v_trip
    observed = bool(bo['esp32_reset'])
    return {'check': 'brownout under servo stall', 'servo_node': node, 'mcu_rail': supply,
            'model_min_rail_v': _sig(v_min), 'v_trip': v_trip, 'model_resets': predicted,
            'observed_reset': observed, 'consistent': predicted == observed,
            'note': ('steady-stall check with the fitted values; a mismatch points at transients the '
                     'average model does not have (start-up inrush, servo PWM, meter averaging) or at '
                     'wiring that differs from project.yaml') if predicted != observed else ''}


# ------------------------------------------------------------ main entry ----

def _targets(sheet, data):
    t = sheet.get('targets') or {}
    acts = data.get('actuators') or {}
    srcs = (data.get('power') or {}).get('sources') or {}
    els = (data.get('mechanics') or {}).get('elements') or {}
    actuator = t.get('actuator') or next((k for k, v in acts.items() if v.get('type', 'servo') == 'servo'), None)
    source = t.get('source')
    if source is None and actuator in acts:
        node = acts[actuator]['node']
        source = next((k for k, v in srcs.items() if v['node'] == node), None)
    source = source or next(iter(srcs), None)
    element = t.get('element') or next((k for k, v in els.items() if v.get('type') == 'bistable'), None)
    return actuator, source, element


def fit_measurements(project, measurements, apply=False, allow_example=False):
    """Fit model parameters from a measurement sheet.

    project: Project or project directory. measurements: path to the sheet
    (relative paths are resolved against the project directory first).
    Returns fitted values with 1-sigma uncertainties, residuals before/after,
    the project.yaml paths that would change (old -> new), parameters kept at
    their prior (and why), and consistency checks. apply=True writes the changes
    with Project.set_path/save and records them under calibration.measurements."""
    if not isinstance(project, Project):
        project = Project(project)
    mpath = Path(measurements)
    if not mpath.is_absolute() and (project.root / mpath).exists():
        mpath = project.root / mpath
    sheet = load_measurements(mpath)
    data = project.data
    example = bool((sheet.get('meta') or {}).get('example_data'))
    actuator, source, element = _targets(sheet, data)
    report = {'measurements': str(mpath), 'example_data': example,
              'targets': {'actuator': actuator, 'source': source, 'element': element},
              'fitted': {}, 'sigma': {}, 'changes': [], 'kept_prior': {}, 'residuals': [], 'checks': [],
              'notes': [], 'applied': False}
    if example:
        report['notes'].append('this sheet is labelled example_data: the numbers are invented, not measurements')
    new_data = copy.deepcopy(data)
    old_eff_extra = {}

    def record(prefix, part):
        for k, v in part.get('fitted', {}).items():
            report['fitted'][f'{prefix}.{k}'] = _sig(v, 5)
        for k, v in part.get('sigma', {}).items():
            report['sigma'][f'{prefix}.{k}'] = _sig(v, 2)
        for k, v in part.get('kept_prior', {}).items():
            report['kept_prior'][f'{prefix}.{k}'] = v
        report['residuals'] += part.get('residuals', [])
        report['notes'] += part.get('notes', [])

    # supply first: its resistance is used when the servo move voltage was read at the source
    if source and sheet.get('supply'):
        sp = (data['power']['sources'] or {})[source]
        part = fit_supply(sheet, sp)
        record(f'power.sources.{source}', part)
        for k, v in part['fitted'].items():
            set_path(new_data, f'power.sources.{source}.{k}', _sig(v, 4))
    if actuator and sheet.get('servo'):
        acfg = data['actuators'][actuator]
        prior = merged(acfg)
        r_src = 1e-3
        if source:
            r_src = float(get_path(new_data, f'power.sources.{source}.r_ohm', 0.1))
        joint = acfg['joint']
        fric_path = f'mechanics.joints.{joint}.frictionloss'
        fric_old = get_path(data, fric_path, None)
        if fric_old is None:
            fric_old = _joint_attr(project, joint, 'frictionloss')
        part = fit_servo(sheet, prior, supply_r=r_src, friction_prior=fric_old)
        if 'friction_nm' in part['fitted']:
            fv, fs = part['fitted'].pop('friction_nm'), part['sigma'].pop('friction_nm', None)
            report['fitted'][fric_path] = _sig(fv, 5)
            if fs is not None:
                report['sigma'][fric_path] = _sig(fs, 2)
            set_path(new_data, fric_path, _sig(fv, 4))
            old_eff_extra[fric_path] = fric_old
        elif 'friction_nm' in part['kept_prior']:
            report['kept_prior'][fric_path] = part['kept_prior'].pop('friction_nm')
        record(f'actuators.{actuator}', part)
        if part.get('derived'):
            report['derived'] = part['derived']
        report['servo_mode'] = part.get('mode')
        for k, v in part['fitted'].items():
            set_path(new_data, f'actuators.{actuator}.{k}', _sig(v, 4))
    if element and sheet.get('switch'):
        el = data['mechanics']['elements'][element]
        part = fit_switch(sheet, el)
        record(f'mechanics.elements.{element}', part)
        for k, v in part['fitted'].items():
            set_path(new_data, f'mechanics.elements.{element}.{k}', _sig(v, 4))
        if 'stable_deg' in part['fitted']:
            joint = el['joint']
            init = get_path(data, f'mechanics.initial_deg.{joint}', None)
            sd = _sig(part['fitted']['stable_deg'], 4)
            if init is not None and abs(abs(float(init)) - float(el['stable_deg'])) < 1e-9:
                set_path(new_data, f'mechanics.initial_deg.{joint}', math.copysign(sd, float(init)))
            rng = get_path(data, f'mechanics.joints.{joint}.range_deg', None)
            if rng is not None and sd >= max(abs(float(r)) for r in rng):
                report['notes'].append(f'stable_deg {sd} is at or beyond the joint range {rng} of {joint}: '
                                       'the end stops in the MJCF/project need to move too')

    # changes old -> new (old = effective value, including presets)
    old_eff = dict(old_eff_extra)
    if actuator:
        for k, v in merged(data['actuators'][actuator]).items():
            old_eff[f'actuators.{actuator}.{k}'] = v
    for path in _leaf_diff(data, new_data):
        old = get_path(data, path, None)
        if old is None:
            old = old_eff.get(path)
        new = get_path(new_data, path)
        if old is not None and isinstance(old, (int, float)) and isinstance(new, (int, float)) \
                and abs(new - old) <= 1e-6 * max(abs(old), 1e-12):
            continue
        rel = (new - old) / old if isinstance(old, (int, float)) and old else None
        report['changes'].append({'path': path, 'old': old, 'new': new,
                                  'change_pct': _sig(rel * 100, 3) if rel is not None else None})

    chk = brownout_check(new_data, actuator, sheet) if actuator else None
    if chk:
        report['checks'].append(chk)
    if apply:
        if example and not allow_example:
            raise ValueError('refusing to apply a sheet labelled example_data (invented values); '
                             'pass allow_example=True to override')
        for c in report['changes']:
            set_path(project.data, c['path'], c['new'])
        try:
            rel = str(mpath.resolve().relative_to(project.root))
        except ValueError:
            rel = str(mpath)
        set_path(project.data, 'calibration.measurements', {
            'file': rel, 'applied': time.strftime('%Y-%m-%d %H:%M:%S'), 'example_data': example,
            'changes': {c['path']: {'old': c['old'], 'new': c['new']} for c in report['changes']}})
        project.save()
        report['applied'] = True
    return report


def _joint_attr(project, joint, attr):
    """Effective value of a MuJoCo joint attribute (from the MJCF when project.yaml
    does not set it), e.g. the servo joint's frictionloss."""
    try:
        from .mechanics import Mechanics
        m = Mechanics(project.root, project.data.get('mechanics', {}), None).model
        v = getattr(m.joint(joint), attr)
        return float(np.ravel(v)[0])
    except Exception:              # noqa: BLE001 -- no model available: fall back to the bench default
        return BENCH_LOAD['friction_nm'] if attr == 'frictionloss' else None


def _leaf_diff(a, b, prefix=''):
    out = []
    if isinstance(b, dict):
        for k, v in b.items():
            p = f'{prefix}.{k}' if prefix else str(k)
            av = a.get(k) if isinstance(a, dict) else None
            if isinstance(v, dict):
                out += _leaf_diff(av if isinstance(av, dict) else {}, v, p)
            elif av != v:
                out.append(p)
    return out


def format_report(report):
    """Plain-text summary of a fit_measurements() report."""
    lines = [f"measurements: {report['measurements']}" + ('  [EXAMPLE DATA, invented]' if report['example_data']
                                                           else '')]
    lines.append('changes (old -> new):')
    for c in report['changes']:
        sig = report['sigma'].get(c['path'])
        lines.append(f"  {c['path']}: {c['old']} -> {c['new']}" + (f" (+/- {sig})" if sig else '')
                     + (f"  [{c['change_pct']:+g} %]" if c.get('change_pct') is not None else ''))
    lines.append('kept at prior:')
    for k, v in report['kept_prior'].items():
        lines.append(f'  {k}: {v}')
    lines.append('residuals (measured | model before | model after | z after):')
    for r in report['residuals']:
        lines.append(f"  {r['quantity']} [{r['unit']}]: {r['measured']} +/- {r['unc']} | {r['model_before']} | "
                     f"{r['model_after']} | {r['z_after']}" + ('' if r['used_in_fit'] else '  (check only)'))
    for c in report['checks']:
        lines.append(f"check {c.get('check')}: {c}")
    for n in report['notes']:
        lines.append(f'note: {n}')
    return '\n'.join(lines)


# ---------------------------------------------------------- trace compare ----

def read_trace_csv(path, column=None, time_column=None, time_unit='s'):
    """Read (t [s], y) from a CSV exported by a scope or logging multimeter.
    Header row required; delimiter , ; or tab; '#' lines are comments. The time
    column defaults to the first column, the value column to the second."""
    text = [ln for ln in Path(path).read_text(encoding='utf8', errors='replace').splitlines()
            if ln.strip() and not ln.lstrip().startswith('#')]
    if not text:
        raise ValueError(f'{path}: empty trace')
    delim = max([',', ';', '\t'], key=lambda d: text[0].count(d))
    header = [h.strip().strip('"') for h in text[0].split(delim)]
    lower = [h.lower() for h in header]

    def col(name, default):
        if name is None:
            return default
        if name.lower() in lower:
            return lower.index(name.lower())
        raise KeyError(f'column {name!r} not in {header}')
    it = col(time_column, 0)
    iy = col(column, 1)
    t, y = [], []
    for ln in text[1:]:
        parts = ln.split(delim)
        try:
            t.append(float(parts[it]))
            y.append(float(parts[iy]))
        except (ValueError, IndexError):
            continue
    scale = {'s': 1.0, 'ms': 1e-3, 'us': 1e-6}[time_unit]
    return np.asarray(t) * scale, np.asarray(y)


def compare_trace(t_sim, y_sim, t_meas, y_meas, t_offset=0.0, align=False, max_lag_s=0.5, window=None):
    """RMS / max / mean error of a simulated signal against a measured trace.
    t_offset is added to the measured time axis; align=True additionally searches
    the lag in +/-max_lag_s that minimises the RMS (trigger points of a scope and
    a simulation rarely coincide). window=[t0, t1] in simulation time."""
    t_sim, y_sim = np.asarray(t_sim, float), np.asarray(y_sim, float)
    t_meas, y_meas = np.asarray(t_meas, float), np.asarray(y_meas, float)

    def stats(lag):
        tm = t_meas + t_offset + lag
        sel = (tm >= t_sim[0]) & (tm <= t_sim[-1])
        if window is not None:
            sel &= (tm >= window[0]) & (tm <= window[1])
        if sel.sum() < 2:
            return None
        e = np.interp(tm[sel], t_sim, y_sim) - y_meas[sel]
        return {'rms': float(np.sqrt(np.mean(e ** 2))), 'max_abs': float(np.max(np.abs(e))),
                'mean': float(np.mean(e)), 'n': int(sel.sum()),
                'overlap_s': [float(tm[sel][0]), float(tm[sel][-1])]}
    lag = 0.0
    if align:
        dtm = float(np.median(np.diff(t_meas))) if len(t_meas) > 1 else 1e-3
        step = max(dtm, float(np.median(np.diff(t_sim))) if len(t_sim) > 1 else dtm)
        best = None
        for lg in np.arange(-max_lag_s, max_lag_s + step / 2, step):
            s = stats(lg)
            if s and s['n'] >= max(10, len(t_meas) // 4) and (best is None or s['rms'] < best[1]['rms']):
                best = (lg, s)
        if best is None:
            raise ValueError('no overlap between the measured and simulated traces')
        # refine around the best grid point
        lo, hi = best[0] - step, best[0] + step
        for lg in np.linspace(lo, hi, 41):
            s = stats(lg)
            if s and s['n'] >= best[1]['n'] * 0.9 and s['rms'] < best[1]['rms']:
                best = (lg, s)
        lag, out = best
    else:
        out = stats(0.0)
        if out is None:
            raise ValueError('no overlap between the measured and simulated traces '
                             '(check t_offset and the time unit)')
    out['lag_s'] = float(lag)
    out['t_offset_s'] = float(t_offset)
    rng = float(np.ptp(y_meas)) if len(y_meas) else 0.0
    out['rms_pct_of_range'] = 100 * out['rms'] / rng if rng > 0 else None
    return {k: (_sig(v, 5) if isinstance(v, float) else v) for k, v in out.items()}


def compare_run(project, run_id, trace_csv, signal, column=None, time_column=None, time_unit='s',
                t_offset=0.0, align=False, max_lag_s=0.5, window=None, scale=1.0):
    """Compare signal `signal` (e.g. node.usb5v.v) of a stored run with a
    measured CSV trace. scale multiplies the measured values (probe ratio,
    mV -> V). Returns RMS error and friends; see compare_trace."""
    from .results import RunStore
    if not isinstance(project, Project):
        project = Project(project)
    store = RunStore(project.root)
    if run_id is None:
        runs = store.list()
        if not runs:
            raise FileNotFoundError('no runs')
        run_id = runs[0]['run_id']
    z = np.load(store.dir(run_id) / 'signals.npz')
    if signal not in z.files:
        raise KeyError(f'unknown signal {signal!r}; available: {[k for k in z.files if k != "t"]}')
    p = Path(trace_csv)
    if not p.is_absolute() and (project.root / p).exists():
        p = project.root / p
    tm, ym = read_trace_csv(p, column, time_column, time_unit)
    out = compare_trace(z['t'], z[signal], tm, ym * scale, t_offset=t_offset, align=align,
                        max_lag_s=max_lag_s, window=window)
    out.update({'run_id': run_id, 'signal': signal, 'trace': str(p)})
    return out


__all__ = ['fit_measurements', 'format_report', 'fit_servo', 'fit_supply', 'fit_switch', 'brownout_check',
           'bench_stall', 'bench_move', 'bench_sweep_current', 'dc_point', 'bistable_peak', 'bistable_torque',
           'compare_trace', 'compare_run', 'read_trace_csv', 'load_measurements']
