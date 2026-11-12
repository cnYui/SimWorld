"""Measurement fitting: synthetic round trips (known parameters -> simulated
"measurements" -> fit -> recovered parameters) and trace comparison."""
import copy
import math
import shutil

import numpy as np
import pytest
import yaml
from conftest import EXAMPLE

from emcosim import fit
from emcosim.electrical.motors import merged
from emcosim.project import Project, dump_yaml

G = fit.G

TRUE_SERVO = {'r_ohm': 6.0, 'ke_v_s_rad': 0.62 / 250, 'efficiency': 0.5, 'i_ctrl_a': 0.007}
TRUE_FRICTION = 0.01
TRUE_SOURCE = {'v': 5.1, 'r_ohm': 0.5, 'i_limit_a': 0.9}
TRUE_SWITCH = {'stable_deg': 3.2, 'peak_force_n': 2.2}


def _m(v, unc):
    return {'value': float(v), 'unc': float(unc)}


def synthetic_sheet(p_true, friction, src, switch_true, lever_el=0.024):
    """Generate a measurement sheet by running the models with known parameters."""
    v, r, il = src['v'], src['r_ohm'], src['i_limit_a']
    st = fit.bench_stall(p_true, v, r, il)
    lever = 0.025
    p_load = {'friction_nm': friction}
    v_idle = v - r * p_true['i_ctrl_a']
    t_move = fit.bench_move(p_true, 90.0, v_idle, r, p_load)['time_s']
    v_low = 4.2 - r * p_true['i_ctrl_a']                 # second video on a lower supply
    t_low = fit.bench_move(p_true, 90.0, v_low, r, p_load)['time_s']
    i_sweep = fit.bench_sweep_current(p_true, 90.0, 1.0, v_idle, r, p_load)
    v10, i10 = fit.dc_point(v, r, il, {'r_load_ohm': 10.0})
    v33, i33 = fit.dc_point(v, r, il, {'r_load_ohm': 3.3})
    el = {'stable_deg': switch_true['stable_deg'], 'peak_force_n': switch_true['peak_force_n'], 'lever_m': lever_el}
    tau_sw = fit.bistable_peak(el)[0]
    press = 0.015
    return {
        'meta': {'example_data': False, 'notes': 'synthetic (test)'},
        'targets': {'actuator': 'servo1', 'source': 'usb', 'element': 'detent'},
        'servo': {
            'idle_current_a': _m(p_true['i_ctrl_a'], 0.002),
            'stall': {'current_a': _m(st['i_supply_a'], 0.01), 'v_servo_v': _m(st['v_node_v'], 0.02),
                      'torque': {'force_g': _m(st['torque_nm'] / (G * lever) * 1e3, 5),
                                 'lever_m': _m(lever, 0.0005)}},
            'moves': [{'angle_deg': _m(90, 1), 'time_s': _m(t_move, 0.005), 'fps': 240,
                       'v_servo_v': _m(v_idle, 0.02)},
                      {'angle_deg': _m(90, 1), 'time_s': _m(t_low, 0.005), 'fps': 240,
                       'v_servo_v': _m(v_low, 0.02)}],
            'sweep': {'current_a': _m(i_sweep, 0.01), 'angle_deg': 90, 'period_s': 1.0,
                      'v_servo_v': _m(v_idle, 0.02)},
        },
        'supply': {
            'open_circuit_v': _m(v, 0.02), 'current_limit_seen': True,
            'points': [
                {'label': 'r10', 'r_load_ohm': 10.0, 'current_a': _m(i10, 0.01), 'voltage_v': _m(v10, 0.02)},
                {'label': 'stall', 'current_a': _m(st['i_supply_a'], 0.01), 'voltage_v': _m(st['v_node_v'], 0.02)},
                {'label': 'r3.3', 'r_load_ohm': 3.3, 'current_a': _m(i33, 0.01), 'voltage_v': _m(v33, 0.02)},
            ]},
        'switch': {'force_peak_g': _m(tau_sw / (G * press) * 1e3, 2), 'press_lever_m': _m(press, 0.0005),
                   'travel_deg': _m(2 * switch_true['stable_deg'], 0.2)},
        'brownout': {'esp32_reset': False},
    }


def _true_params():
    p = merged({'preset': 'sg90'})
    p.update(TRUE_SERVO)
    return p


def _project(tmp_path):
    prj = Project.create(tmp_path / 'p')
    d = prj.data
    d['power'] = {'nodes': {'n5': {'c_f': 1e-4}},
                  'sources': {'usb': {'node': 'n5', 'v': 5.0, 'r_ohm': 0.3, 'i_limit_a': 0.5}},
                  'branches': {}, 'regulators': {}}
    d['mechanics'] = {'elements': {'detent': {'type': 'bistable', 'joint': 'rocker', 'stable_deg': 3.5,
                                              'peak_force_n': 3.0, 'lever_m': 0.024}}}
    d['actuators'] = {'servo1': {'type': 'servo', 'joint': 'servo_axis', 'node': 'n5', 'preset': 'sg90'}}
    prj.save()
    return prj


def test_round_trip_recovers_parameters(tmp_path):
    sheet = synthetic_sheet(_true_params(), TRUE_FRICTION, TRUE_SOURCE, TRUE_SWITCH)
    sheet_file = tmp_path / 'm.yaml'
    sheet_file.write_text(dump_yaml(sheet), encoding='utf8')
    prj = _project(tmp_path)
    r = fit.fit_measurements(prj, sheet_file, apply=True)
    f = r['fitted']
    rel = lambda a, b: abs(a - b) / abs(b)
    assert rel(f['actuators.servo1.r_ohm'], TRUE_SERVO['r_ohm']) < 0.02
    assert rel(f['actuators.servo1.ke_v_s_rad'], TRUE_SERVO['ke_v_s_rad']) < 0.03
    assert rel(f['actuators.servo1.efficiency'], TRUE_SERVO['efficiency']) < 0.03
    assert rel(f['actuators.servo1.i_ctrl_a'], TRUE_SERVO['i_ctrl_a']) < 0.01
    assert rel(f['mechanics.joints.servo_axis.frictionloss'], TRUE_FRICTION) < 0.1
    assert rel(f['power.sources.usb.v'], TRUE_SOURCE['v']) < 0.01
    assert rel(f['power.sources.usb.r_ohm'], TRUE_SOURCE['r_ohm']) < 0.05
    assert rel(f['power.sources.usb.i_limit_a'], TRUE_SOURCE['i_limit_a']) < 0.02
    assert rel(f['mechanics.elements.detent.stable_deg'], TRUE_SWITCH['stable_deg']) < 0.01
    assert rel(f['mechanics.elements.detent.peak_force_n'], TRUE_SWITCH['peak_force_n']) < 0.01
    assert r['servo_mode'] == 'motor-limited'
    assert 'actuators.servo1.gear_ratio' in r['kept_prior']
    assert all(abs(x['z_after']) < 0.5 for x in r['residuals'] if x['used_in_fit']), r['residuals']
    paths = {c['path']: c for c in r['changes']}
    assert paths['actuators.servo1.r_ohm']['old'] == 7.0            # preset value shown as old
    assert paths['power.sources.usb.r_ohm']['old'] == 0.3
    assert r['checks'] and r['checks'][0]['check'].startswith('brownout')
    # apply wrote the new values and a record
    saved = Project(prj.root)
    assert r['applied']
    for c in r['changes']:
        assert saved.get(c['path']) == c['new']
    assert saved.get('calibration.measurements.file').endswith('m.yaml')
    assert saved.get('calibration.measurements.example_data') is False


def test_speed_limited_servo_is_detected():
    p_true = _true_params()
    p_true['max_speed_dps'] = 250.0
    sheet = synthetic_sheet(p_true, TRUE_FRICTION, TRUE_SOURCE, TRUE_SWITCH)
    r = fit.fit_servo(sheet, merged({'preset': 'sg90', 'efficiency': 0.5}), supply_r=TRUE_SOURCE['r_ohm'],
                      friction_prior=0.0007)
    assert r['mode'] == 'speed-limited'
    assert abs(r['fitted']['max_speed_dps'] - 250.0) / 250.0 < 0.05
    assert abs(r['fitted']['r_ohm'] - 6.0) / 6.0 < 0.02
    assert 'efficiency' in r['kept_prior']


def test_underdetermined_parameters_keep_prior():
    sheet = {'servo': {'stall': {'current_a': _m(0.6, 0.02), 'v_servo_v': _m(4.7, 0.03)}}}
    r = fit.fit_servo(sheet, merged({'preset': 'sg90'}))
    assert set(r['fitted']) == {'r_ohm'}
    for k in ('ke_v_s_rad', 'efficiency', 'i_ctrl_a', 'friction_nm', 'gear_ratio'):
        assert k in r['kept_prior']
    assert abs(r['fitted']['r_ohm'] - 4.7 / (0.6 - 0.006)) < 0.01
    # supply without a limited point: i_limit is a lower bound only
    sup = {'supply': {'open_circuit_v': _m(5.0, 0.01),
                      'points': [{'label': 'r10', 'r_load_ohm': 10.0, 'voltage_v': _m(4.8, 0.01)}]}}
    s = fit.fit_supply(sup, {'node': 'n', 'v': 5.0, 'r_ohm': 0.1})
    assert abs(s['fitted']['r_ohm'] - 10 * 0.2 / 4.8) < 0.02 and 'i_limit_a' in s['kept_prior']


def test_example_sheet_is_not_applied(tmp_path):
    src = EXAMPLE / 'measurements' / 'example_sg90_usb.yaml'
    sheet = yaml.safe_load(src.read_text(encoding='utf8'))
    assert sheet['meta']['example_data'] is True
    # supply + switch only (fast); the full example is exercised by the round trip above
    small = {k: sheet[k] for k in ('meta', 'targets', 'supply', 'switch')}
    f = tmp_path / 'ex.yaml'
    f.write_text(dump_yaml(small), encoding='utf8')
    prj = _project(tmp_path)
    prj.data['mechanics']['elements']['rocker_detent'] = prj.data['mechanics']['elements'].pop('detent')
    prj.save()
    before = copy.deepcopy(prj.data)
    r = fit.fit_measurements(prj, f)
    assert r['example_data'] and r['changes'] and not r['applied']
    assert Project(prj.root).data == before          # report only: nothing written
    with pytest.raises(ValueError):
        fit.fit_measurements(prj, f, apply=True)


def test_compare_trace_alignment_and_rms():
    t = np.arange(0, 2, 1e-3)
    y = 5 - 0.5 * (np.tanh((t - 0.8) / 0.02) + 1) / 2
    rng = np.random.default_rng(1)
    tm = np.arange(0.1, 1.9, 2e-3)
    ym = 5 - 0.5 * (np.tanh((tm - 0.85) / 0.02) + 1) / 2 + rng.normal(0, 0.01, len(tm))
    raw = fit.compare_trace(t, y, tm, ym)
    al = fit.compare_trace(t, y, tm, ym, align=True, max_lag_s=0.2)
    assert raw['rms'] > 0.05
    assert abs(al['lag_s'] + 0.05) < 0.003
    assert abs(al['rms'] - 0.01) < 0.003


def test_compare_run_against_csv(tmp_path):
    from emcosim import api
    prj = Project(EXAMPLE)
    s = api.run_scenario(prj, 'servo_sweep_scripted')
    run_dir = EXAMPLE / 'runs' / s['run_id']
    try:
        z = np.load(run_dir / 'signals.npz')
        t, v = z['t'], z['joint.servo_axis.deg']
        csv = tmp_path / 'trace.csv'
        sel = slice(0, len(t), 4)
        lines = ['time_ms;angle'] + [f'{a * 1e3:.3f};{b + 0.2:.5f}' for a, b in zip(t[sel], v[sel])]
        csv.write_text('\n'.join(lines), encoding='utf8')
        out = fit.compare_run(prj, s['run_id'], csv, 'joint.servo_axis.deg', column='angle', time_unit='ms')
        assert abs(out['rms'] - 0.2) < 1e-3 and abs(out['mean'] + 0.2) < 1e-3
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def test_backlash_and_compliance_read_directly():
    sheet = {'servo': {'backlash_deg': _m(2.0, 0.5),
                       'compliance': {'force_g': _m(300, 5), 'lever_m': _m(0.02, 0.001),
                                      'deflection_deg': _m(3.0, 0.5)}}}
    r = fit.fit_servo(sheet, merged({'preset': 'sg90'}))
    assert r['fitted']['backlash_deg'] == 2.0
    k = 0.3 * G * 0.02 / math.radians(3.0)
    assert abs(r['fitted']['output_stiffness_nm_per_rad'] - k) < 1e-9
