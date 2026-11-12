"""Circuit check with ngspice, built from the board's real netlist.

The co-simulation runs a small equivalent power network every millisecond
(electrical/power.py). This module checks it against the board as drawn: every
resistor and capacitor of the netlist goes into a SPICE circuit with its value,
the parts that matter for power get a model (diode, LDO, fuse), and the loads
seen in a simulation run (servo current, MCU current) are played back as current
sources. ngspice then solves the circuit and the node voltages are compared with
what the co-simulation computed.

Models here are simple and stated as such (MODELS below): a Schottky diode
fitted to typical datasheet values, a behavioural LDO with dropout and ground
current, a fuse as its cold resistance. Parts without a model are left out and
listed in the report, so nothing is silently assumed.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np

# LCSC part -> model. 'kind' picks the SPICE template below; params are typical datasheet values.
MODELS = {
    'C8678': {'kind': 'diode', 'name': 'SS34', 'model': 'D(Is=5u Rs=0.03 N=1.1 Cjo=250p BV=40)',
              'pins': ('2', '1'), 'note': 'SS34 Schottky, ~0.4 V at 1 A (typical curve, not a vendor model)'},
    'C6186': {'kind': 'ldo', 'name': 'AMS1117-3.3', 'v_out': 3.3, 'dropout_v': 1.1, 'i_gnd_a': 0.005,
              'r_out_ohm': 0.02, 'pins': {'in': '3', 'out': '2', 'gnd': '1'},
              'note': 'behavioural LDO: min(3.3 V, Vin - 1.1 V), 5 mA ground current (datasheet typical)'},
    'C5395': {'kind': 'resistor', 'name': 'RUEF110', 'r_ohm': 0.15,
              'note': 'polyfuse at its cold resistance (assumed, datasheet range 0.10-0.18 ohm)'},
}
ESR = {'electrolytic': 0.08, 'ceramic': 0.005}

_SI = {'p': 1e-12, 'n': 1e-9, 'u': 1e-6, 'µ': 1e-6, 'm': 1e-3, 'k': 1e3, 'K': 1e3, 'M': 1e6, 'R': 1, 'Ω': 1}


def parse_value(text):
    """'470uF' -> 4.7e-4, '5.1kΩ' -> 5100, '4K7' -> 4700, '100nF' -> 1e-7. None if not a number."""
    if not text:
        return None
    t = str(text).strip().replace('Ω', '').replace('F', '').replace(' ', '')
    m = re.match(r'^(\d+(?:\.\d+)?)([pnuµmkKMR]?)(\d*)$', t)
    if not m:
        return None
    num, unit, frac = m.groups()
    val = float(num + ('.' + frac if frac else ''))
    return val * _SI.get(unit, 1)


def _node(net):
    return 'n_' + re.sub(r'[^A-Za-z0-9_]', '_', net) if net != 'GND' else '0'


def build(netlist, loads, supply, t_end, lead_l_h=1e-6):
    """SPICE text for the board. netlist: eda.read_netlist() result. loads: {net: (t, i)} current
    drawn from that net to GND. supply: {'net', 'v', 'r_ohm'}."""
    pin_net = {pin: net for net, pins in netlist['nets'].items() for pin in pins}
    lines = ['* emcosim board circuit check (generated from the board netlist)', '.options method=gear reltol=1e-4']
    used, skipped = [], []
    for ref, c in sorted(netlist['components'].items()):
        pins = {p.split('-', 1)[1]: n for p, n in pin_net.items() if p.split('-', 1)[0] == ref}
        model = MODELS.get(c.get('lcsc') or '')
        kind = model['kind'] if model else {'R': 'resistor', 'C': 'capacitor'}.get(re.sub(r'\d', '', ref)[:1])
        if kind in ('resistor', 'capacitor') and len(pins) == 2 and set(pins) == {'1', '2'}:
            a, b = _node(pins['1']), _node(pins['2'])
            if a == b:
                continue
            if kind == 'resistor':
                r = model['r_ohm'] if model else parse_value(c.get('value'))
                if r is None:
                    skipped.append({'ref': ref, 'why': f'value {c.get("value")!r} not understood'})
                    continue
                lines.append(f'R{ref} {a} {b} {r:g}')
            else:
                cap = parse_value(c.get('value'))
                if cap is None:
                    skipped.append({'ref': ref, 'why': f'value {c.get("value")!r} not understood'})
                    continue
                esr = ESR['electrolytic'] if cap >= 47e-6 else ESR['ceramic']
                lines += [f'C{ref} {a} c_{ref} {cap:g}', f'Resr{ref} c_{ref} {b} {esr:g}']
            used.append({'ref': ref, 'value': c.get('value'), 'model': model['note'] if model else kind})
        elif kind == 'diode':
            an, ca = (pins.get(p) for p in model['pins'])
            lines += [f'D{ref} {_node(an)} {_node(ca)} {model["name"]}', f'.model {model["name"]} {model["model"]}']
            used.append({'ref': ref, 'value': c.get('value'), 'model': model['note']})
        elif kind == 'ldo':
            p = {k: _node(pins[v]) for k, v in model['pins'].items()}
            lines += [f'B{ref}o {ref}_int {p["gnd"]} V = max(min({model["v_out"]}, V({p["in"]},{p["gnd"]}) '
                      f'- {model["dropout_v"]}), 0)',
                      f'R{ref}o {ref}_int {ref}_s {model["r_out_ohm"]}',
                      f'V{ref}s {ref}_s {p["out"]} 0',
                      f'B{ref}i {p["in"]} {p["gnd"]} I = I(V{ref}s) + {model["i_gnd_a"]}']
            used.append({'ref': ref, 'value': c.get('value'), 'model': model['note']})
        else:
            skipped.append({'ref': ref, 'why': 'no circuit model (connector, switch, module or IC); its '
                                               'current, if any, comes from the run as a load'})
    s = supply
    lines += [f'Vsupply sup_v 0 DC {s["v"]}', f'Rsupply sup_v sup_l {s.get("r_ohm", 0.2)}',
              f'Lsupply sup_l {_node(s["net"])} {s.get("l_h", lead_l_h)}']
    for k, (net, (t, i)) in enumerate(loads.items()):
        pts = ' '.join(f'{tt:.6g} {ii:.6g}' for tt, ii in zip(t, i))
        lines.append(f'Iload{k} {_node(net)} 0 PWL({pts})')
    for net in netlist['nets']:
        if net != 'GND':
            lines.append(f'Rleak_{_node(net)} {_node(net)} 0 1e7')
    lines.append(f'.tran 20u {t_end:g} 0 20u')
    return '\n'.join(lines), used, skipped


def run(circuit, probes, timeout=300):
    exe = shutil.which('ngspice')
    if not exe:
        raise RuntimeError('ngspice not found (Windows: bundled with the installer; Linux: apt install ngspice)')
    vecs = ' '.join(f'v({_node(n)})' for n in probes)
    text = circuit + f'\n.control\nset wr_singlescale\nrun\nwrdata out.txt {vecs}\n.endc\n.end\n'
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / 'board.cir').write_text(text, encoding='utf-8')
        r = subprocess.run([exe, '-b', 'board.cir'], cwd=tmp, capture_output=True, text=True, timeout=timeout)
        out = Path(tmp) / 'out.txt'
        if not out.exists():
            raise RuntimeError('ngspice failed: ' + (r.stdout + r.stderr)[-1200:])
        data = np.loadtxt(out, ndmin=2)
    return data[:, 0], {n: data[:, 1 + k] for k, n in enumerate(probes)}


def check(project, run_id, cfg=None, max_points=400):
    """Re-solve one run's power path with ngspice on the real board netlist and compare."""
    from .. import eda
    from ..results import RunStore
    cfg = cfg or project.data.get('circuit') or {}
    if not cfg.get('netlist'):
        raise ValueError('project.yaml has no circuit.netlist (path of the board netlist export)')
    net = eda.read_netlist(project.root / cfg['netlist'])
    res = RunStore(project.root)
    names = res.signal_names(run_id)
    want = [ld['signal'] for ld in cfg.get('loads', {}).values() if ld.get('signal') in names]
    want += [sig for sig in cfg.get('compare', {}).values() if sig in names]
    sig = res.signals(run_id, sorted(set(want)), max_points=100000)
    t = np.asarray(sig['t'])
    t_end = float(t[-1])
    # resample loads to <= max_points breakpoints (PWL); the run is sampled every 1 ms
    idx = np.unique(np.linspace(0, len(t) - 1, min(len(t), max_points)).astype(int))
    loads = {}
    for name, ld in cfg.get('loads', {}).items():
        if ld.get('signal') in names:
            loads[ld['net']] = (t[idx], np.asarray(sig[ld['signal']])[idx])
        elif 'a' in ld:
            loads[ld['net']] = (np.array([0.0, t_end]), np.array([ld['a'], ld['a']]))
    circuit, used, skipped = build(net, loads, cfg['supply'], t_end)
    probes = list(cfg.get('compare', {}))
    ts, vs = run(circuit, probes)
    rows = []
    for netname, signal in cfg.get('compare', {}).items():
        if signal not in names:
            continue
        cos = np.interp(ts, t, np.asarray(sig[signal]))
        d = vs[netname] - cos
        rows.append({'net': netname, 'cosim_signal': signal,
                     'spice_min_v': round(float(vs[netname].min()), 3), 'cosim_min_v': round(float(cos.min()), 3),
                     'max_abs_diff_v': round(float(np.abs(d).max()), 3),
                     'mean_diff_v': round(float(d.mean()), 3)})
    return {'run': run_id, 'solver': 'ngspice', 'netlist': cfg['netlist'], 'compare': rows,
            'parts_modelled': used, 'parts_not_modelled': skipped,
            'trace': {'t': ts[:: max(1, len(ts) // 300)].round(5).tolist(),
                      **{n: v[:: max(1, len(ts) // 300)].round(4).tolist() for n, v in vs.items()}}}
