"""Offline calibration of the equivalent power model against ngspice.

The 1 ms co-simulation uses a resistive/capacitive network without ESR or
wiring inductance. Here the same network is written as a SPICE netlist WITH
those parasitics (capacitor ESR, source lead inductance, PCB trace inductance),
a load current step is applied at one node, and ngspice's transient (1 us step)
is compared with the equivalent model sampled the way the co-simulator uses
it. Two effective parameters are then fitted -- the target node's capacitance
and the source resistance -- so the cheap model reproduces the SPICE droop.

Requires the `ngspice` executable (Docker image includes it).
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from .electrical.power import PowerNetwork


def spice_netlist(power, node, i_step, t_step=1e-3, t_end=12e-3, defaults=None):
    d = {'esr_ohm': 0.03, 'source_l_h': 1.0e-6, 'branch_l_h': 30e-9}
    d.update(defaults or {})
    lines = ['* emcosim power network calibration', '.options method=gear reltol=1e-5']
    for name, s in power.get('sources', {}).items():
        lines += [f"V_{name} {name}_v 0 DC {s['v']}",
                  f"R_{name} {name}_v {name}_l {s.get('r_ohm', 0.1)}",
                  f"L_{name} {name}_l {s['node']} {s.get('l_h', d['source_l_h'])}"]
    for name, b in power.get('branches', {}).items():
        lines += [f"R_{name} {b['from']} {name}_m {b['r_ohm']}",
                  f"L_{name} {name}_m {b['to']} {b.get('l_h', d['branch_l_h'])}"]
    for name, n in power.get('nodes', {}).items():
        c = float(n.get('c_f') or 0)
        if c > 0:
            lines += [f'C_{name} {name}_c 0 {c}', f"R_esr_{name} {name} {name}_c {n.get('esr_ohm', d['esr_ohm'])}"]
        lines.append(f"R_leak_{name} {name} 0 {n.get('r_leak_ohm', 1e6)}")
    lines += [f'I_load {node} 0 PWL(0 0 {t_step} 0 {t_step + 1e-6} {i_step} {t_end} {i_step})',
              f'.tran 1u {t_end} 0 1u', '.control', 'run', f'wrdata out.txt v({node})', '.endc', '.end']
    return '\n'.join(lines) + '\n'


def run_ngspice(netlist):
    exe = shutil.which('ngspice')
    if not exe:
        raise RuntimeError('ngspice not found (apt install ngspice, or use the Docker image)')
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / 'cal.cir').write_text(netlist)
        subprocess.run([exe, '-b', 'cal.cir'], cwd=tmp, check=True, capture_output=True, timeout=120)
        data = np.loadtxt(Path(tmp) / 'out.txt')
    return data[:, 0], data[:, 1]


def equivalent_response(power, node, i_step, dt, t_step=1e-3, t_end=12e-3):
    net = PowerNetwork(power)
    net.initialise()
    t, v = [], []
    for k in range(int(round(t_end / dt)) + 1):
        tk = k * dt
        net.step(dt, {node: i_step if tk >= t_step else 0.0}, {})
        t.append(tk + dt)
        v.append(net.node_voltage(node))
    return np.array(t), np.array(v)


def calibrate(power, node, source, i_step=0.7, dt=0.000125, apply_to=None):
    """Fit node capacitance and source resistance; returns a report. If apply_to
    is a Project, the fitted values are written into project.yaml."""
    from scipy.optimize import least_squares
    netlist = spice_netlist(power, node, i_step)
    ts, vs = run_ngspice(netlist)
    t_eq, v0 = equivalent_response(power, node, i_step, dt)
    ref = np.interp(t_eq, ts, vs)
    err0 = float(np.max(np.abs(v0 - ref)))

    def model(x):
        p = _copy(power)
        p['nodes'][node]['c_f'] = float(power['nodes'][node].get('c_f') or 0) * x[0]
        p['sources'][source]['r_ohm'] = float(power['sources'][source].get('r_ohm', 0.1)) * x[1]
        return equivalent_response(p, node, i_step, dt)[1]

    fit = least_squares(lambda x: model(x) - ref, x0=[1.0, 1.0], bounds=([0.2, 0.5], [5.0, 2.0]))
    v1 = model(fit.x)
    err1 = float(np.max(np.abs(v1 - ref)))
    c_fit = float(power['nodes'][node].get('c_f') or 0) * fit.x[0]
    r_fit = float(power['sources'][source].get('r_ohm', 0.1)) * fit.x[1]
    report = {'node': node, 'source': source, 'i_step_a': i_step, 'dt_s': dt,
              'spice_final_v': float(vs[-1]), 'spice_min_v': float(vs.min()),
              'max_error_before_v': round(err0, 5), 'max_error_after_v': round(err1, 5),
              'fitted': {f'power.nodes.{node}.c_f': c_fit, f'power.sources.{source}.r_ohm': round(r_fit, 5)},
              'netlist': netlist}
    if apply_to is not None:
        for k, v in report['fitted'].items():
            apply_to.set(k, v)
        apply_to.set(f'power.calibration.{node}', {
            'method': 'ngspice transient, load step', 'i_step_a': i_step,
            'max_error_before_v': report['max_error_before_v'], 'max_error_after_v': report['max_error_after_v']})
    return report


def _copy(d):
    import copy
    return copy.deepcopy(d)
