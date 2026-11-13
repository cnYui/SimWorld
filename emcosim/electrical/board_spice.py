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
    'C82942': {'kind': 'ldo', 'name': 'ME6211C33M5G-N', 'v_out': 3.3, 'dropout_v': 0.2, 'i_gnd_a': 0.00006,
               'r_out_ohm': 0.05, 'pins': {'in': '1', 'out': '5', 'gnd': '2'},
               'note': 'behavioural LDO: min(3.3 V, Vin - 0.2 V), 60 uA ground current (datasheet: 120 mV '
                       'dropout at 100 mA, 0.2 V assumed for 200 mA)'},
    'C51118': {'kind': 'ldo', 'name': 'AP2112K-3.3', 'v_out': 3.3, 'dropout_v': 0.25, 'i_gnd_a': 0.000055,
               'r_out_ohm': 0.05, 'pins': {'in': '1', 'out': '5', 'gnd': '2'},
               'note': 'behavioural LDO: min(3.3 V, Vin - 0.25 V), 55 uA ground current (datasheet typical)'},
    'C5446': {'kind': 'ldo', 'name': 'XC6206P332MR', 'v_out': 3.3, 'dropout_v': 0.25, 'i_gnd_a': 0.000001,
              'r_out_ohm': 0.1, 'pins': {'in': '3', 'out': '2', 'gnd': '1'},
              'note': 'behavioural LDO: min(3.3 V, Vin - 0.25 V) (datasheet: 250 mV at 100 mA)'},
}


def _generic_ldo(c):
    """Model for a linear regulator from KiCad's Regulator_Linear library: output voltage from the part
    name or value, pins by name (VIN / VOUT / GND or VSS). Dropout and ground current are assumed."""
    if c.get('lib') != 'Regulator_Linear':
        return None
    names = {num: re.sub(r'[_{}~ ]', '', str(nm)).upper() for num, nm in (c.get('pin_names') or {}).items()}
    pick = lambda pat: next((n for n, nm in names.items() if re.fullmatch(pat, nm)), None)
    pins = {'in': pick(r'V?IN'), 'out': pick(r'V?OUT|VO'), 'gnd': pick(r'GND|VSS')}
    if not all(pins.values()):
        return None
    text = f"{c.get('part', '')} {c.get('value', '')}"
    m = re.search(r'(\d)\.(\d{1,2})\s*V?', text) or re.search(r'C(\d)(\d)[A-Z]', text) or re.search(r'-(\d)(\d)\b', text)
    if not m:
        return None
    v = float(f'{m.group(1)}.{m.group(2)}')
    return {'kind': 'ldo', 'name': c.get('part') or c.get('value'), 'v_out': v, 'dropout_v': 0.25,
            'i_gnd_a': 0.0001, 'r_out_ohm': 0.05, 'pins': pins,
            'note': f'generic LDO from the KiCad symbol: min({v} V, Vin - 0.25 V), 0.1 mA ground current '
                    '(dropout and ground current ASSUMED; give the datasheet values in circuit.models)'}
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


def build(netlist, loads, supply, t_end, lead_l_h=1e-6, models=None):
    """SPICE text for the board. netlist: eda.read_netlist() result. loads: {net: (t, i)} current
    drawn from that net to GND. supply: {'net', 'v', 'r_ohm'}. models: {ref or LCSC: model} from
    circuit.models, used before the built-in table."""
    pin_net = {pin: net for net, pins in netlist['nets'].items() for pin in pins}
    lines = ['* emcosim board circuit check (generated from the board netlist)', '.options method=gear reltol=1e-4']
    used, skipped = [], []
    models = models or {}
    for ref, c in sorted(netlist['components'].items()):
        pins = {p.split('-', 1)[1]: n for p, n in pin_net.items() if p.split('-', 1)[0] == ref}
        model = models.get(ref) or models.get(c.get('lcsc') or '') or MODELS.get(c.get('lcsc') or '') \
            or _generic_ldo(c)
        if model and model.get('kind') == 'ldo':
            model = {'dropout_v': 0.25, 'i_gnd_a': 0.0001, 'r_out_ohm': 0.05, 'note': 'from circuit.models', **model}
            missing = [k for k in ('in', 'out', 'gnd') if (model.get('pins') or {}).get(k) not in pins]
            if missing:
                skipped.append({'ref': ref, 'why': f'LDO model pins {missing} are not connected on the board'})
                continue
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
    from ..toolpaths import find_ngspice
    exe = find_ngspice()
    if not exe:
        raise RuntimeError('ngspice not found: run scripts/setup.sh or scripts\\setup.ps1, or install ngspice')
    vecs = ' '.join(f'v({_node(n)})' for n in probes)
    text = circuit + f'\n.control\nset wr_singlescale\nrun\nwrdata out.txt {vecs}\n.endc\n.end\n'
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / 'board.cir').write_text(text, encoding='utf-8')
        r = subprocess.run([exe, '-b', 'board.cir'], cwd=tmp, capture_output=True, text=True, timeout=timeout)
        out = Path(tmp) / 'out.txt'
        if not out.exists():
            errs = [ln for ln in (r.stdout + r.stderr).splitlines() if re.search(r'error|singular|fail|unknown', ln, re.I)]
            raise RuntimeError('ngspice could not solve the circuit: ' + ('; '.join(errs[-6:]) or
                                                                          (r.stdout + r.stderr)[-600:]))
        data = np.loadtxt(out, ndmin=2)
    return data[:, 0], {n: data[:, 1 + k] for k, n in enumerate(probes)}


def _netlist_for(project, cfg):
    from .. import eda
    if cfg.get('netlist'):
        return eda.read_netlist(project.path(cfg['netlist'])), cfg['netlist']
    sch = (project.data.get('board') or {}).get('kicad_sch')
    if sch:
        with tempfile.TemporaryDirectory() as td:
            f = eda.export_netlist_xml(project.path(sch), Path(td) / 'net.xml')
            return eda.read_netlist(f), sch
    raise ValueError('circuit_check needs the board netlist: a schematic (schematic_create) or '
                     'circuit.netlist = path of an EDA netlist export')


def _defaults(project, cfg, nets):
    """Fill circuit.supply / compare from the power model when they are not given."""
    power = project.data.get('power') or {}
    out = dict(cfg)
    notes = []
    if not out.get('supply'):
        for name, src in (power.get('sources') or {}).items():
            node = (power.get('nodes') or {}).get(src.get('node'), {})
            if node.get('net') in nets:
                out['supply'] = {'net': node['net'], 'v': src.get('v', 5.0), 'r_ohm': src.get('r_ohm', 0.2)}
                notes.append(f'supply from power.sources.{name} on net {node["net"]}')
                break
    if not out.get('compare'):
        cmp = {}
        for name, node in (power.get('nodes') or {}).items():
            if node.get('net') in nets:
                cmp[node['net']] = f'node.{name}.v'
        mcu = project.data.get('mcu') or {}
        rnet = ((power.get('regulators') or {}).get(mcu.get('supply')) or {}).get('net')
        if rnet in nets:
            cmp[rnet] = 'mcu.rail_v'
        if cmp:
            out['compare'] = cmp
            notes.append(f'compare from power.nodes[*].net: {cmp}')
    example = ('circuit: {supply: {net: VBAT, v: 3.7, r_ohm: 0.3}, loads: {mcu: {net: 3V3, a: 0.05}}, '
               'compare: {VBAT: node.vbat.v, 3V3: mcu.rail_v}}')
    if not out.get('supply'):
        raise ValueError('circuit.supply is missing and no power.nodes.<n>.net matches a board net. '
                         f'Set it with set_param, e.g. {example}')
    if not out.get('compare'):
        raise ValueError('circuit.compare is missing: which board nets to compare with which run signals. '
                         f'e.g. {example}')
    if not out.get('loads'):
        raise ValueError(f'circuit.loads is missing: what draws current from which net. e.g. {example}')
    bad = [n for n in [out['supply']['net'], *out['compare'], *(ld['net'] for ld in out['loads'].values())]
           if n not in nets]
    if bad:
        raise ValueError(f'nets {bad} are not on the board; board nets: {sorted(nets)}')
    return out, notes


def _reachable(netlist, used, supply_net):
    """Nets with a DC path to the supply through modelled parts (R, fuse, diode, LDO)."""
    pin_net = {pin: net for net, pins in netlist['nets'].items() for pin in pins}
    edges = {}
    modelled = {u['ref'] for u in used if u.get('model') != 'capacitor'}
    for ref in modelled:
        nets = {n for p, n in pin_net.items() if p.split('-', 1)[0] == ref}
        for a in nets:
            edges.setdefault(a, set()).update(nets - {a})
    seen, todo = {supply_net, 'GND'}, [supply_net]
    while todo:
        n = todo.pop()
        for m in edges.get(n, ()):
            if m not in seen:
                seen.add(m)
                todo.append(m)
    return seen


def check(project, run_id, cfg=None, max_points=400):
    """Re-solve one run's power path with ngspice on the real board netlist and compare."""
    from ..results import RunStore
    cfg = cfg or project.data.get('circuit') or {}
    net, source = _netlist_for(project, cfg)
    cfg, notes = _defaults(project, cfg, net['nets'])
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
    circuit, used, skipped = build(net, loads, cfg['supply'], t_end, models=cfg.get('models'))
    probes = list(cfg.get('compare', {}))
    reach = _reachable(net, used, cfg['supply']['net'])
    warnings = []
    for n in sorted(set(probes) | set(loads)):
        if n not in reach:
            warnings.append(f'net {n} has no DC path to the supply ({cfg["supply"]["net"]}) through parts with a '
                            'circuit model, so its voltage is meaningless: give the regulator/switch between them '
                            'a model in circuit.models (see parts_not_modelled)')
    ts, vs = run(circuit, probes)
    rows = []
    vmax = float(cfg['supply']['v']) * 1.5 + 0.5
    for netname, signal in cfg.get('compare', {}).items():
        if signal not in names:
            warnings.append(f'compare: run {run_id} has no signal {signal}')
            continue
        cos = np.interp(ts, t, np.asarray(sig[signal]))
        d = vs[netname] - cos
        lo, hi = float(vs[netname].min()), float(vs[netname].max())
        if lo < -0.5 or hi > vmax:
            warnings.append(f'{netname} solved to {lo:.3g}..{hi:.3g} V, outside 0..{vmax:.3g} V: not a real voltage; '
                            'something on this net is not modelled or not connected')
        rows.append({'net': netname, 'cosim_signal': signal,
                     'spice_min_v': round(lo, 3), 'cosim_min_v': round(float(cos.min()), 3),
                     'max_abs_diff_v': round(float(np.abs(d).max()), 3),
                     'mean_diff_v': round(float(d.mean()), 3)})
    return {'run': run_id, 'solver': 'ngspice', 'netlist': str(source), 'compare': rows,
            'trustworthy': not warnings, 'warnings': warnings, 'settings_notes': notes,
            'parts_modelled': used, 'parts_not_modelled': skipped,
            'trace': {'t': ts[:: max(1, len(ts) // 300)].round(5).tolist(),
                      **{n: v[:: max(1, len(ts) // 300)].round(4).tolist() for n, v in vs.items()}}}
