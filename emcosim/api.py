"""High-level operations shared by the CLI and the MCP server. Every function
takes a Project, edits project.yaml (or reads runs/) and returns plain data."""
from __future__ import annotations

import copy
import itertools
import shutil
import time
from pathlib import Path

import numpy as np

from . import firmware as fwmod
from .engine import Simulation, compute_metrics, evaluate
from .importers import kicad
from .project import Project, dump_yaml, get_path, pin_number
from .results import Recorder, RunStore, new_run_id


# ---------------------------------------------------------------- import ----

def import_pcb(project: Project, path, placement=None, dsn=None, bom=None):
    """KiCad .kicad_pcb (or EasyEDA Pro .epcb + .dsn via the stop-gap converter)."""
    path = Path(path)
    pcb_dir = project.root / 'pcb'
    pcb_dir.mkdir(exist_ok=True)
    note = None
    if path.suffix == '.epcb':
        from .importers import easyeda_pro
        if not dsn:
            raise ValueError('EasyEDA Pro .epcb needs the matching Specctra .dsn export (dsn=...)')
        target = pcb_dir / (path.stem + '.kicad_pcb')
        easyeda_pro.convert(path, dsn, target, bom)
        note = ('converted from EasyEDA Pro by emcosim (pads, tracks, values, outline only); '
                'prefer EasyEDA Pro "Export > KiCad"')
    elif path.suffix == '.kicad_pcb':
        target = pcb_dir / path.name
        if path.resolve() != target.resolve():
            shutil.copy(path, target)
    else:
        raise ValueError('expected a .kicad_pcb file (EasyEDA: export to KiCad first)')
    pcb = kicad.load_kicad_pcb(target)
    summary = kicad.public_summary(pcb)
    if note:
        summary['note'] = note
    summary_file = pcb_dir / (target.stem + '.import.yaml')
    summary_file.write_text(dump_yaml(summary), encoding='utf8')
    entry = {'file': str(target.relative_to(project.root)), 'summary': str(summary_file.relative_to(project.root))}
    if placement:
        entry['placement'] = placement
    elif project.data.get('pcb', {}).get('placement'):
        entry['placement'] = project.data['pcb']['placement']
    project.data['pcb'] = entry
    project.save()
    caps = {}
    for net in summary['nets']:
        if net.upper() in ('GND', 'GNDA', 'GNDD'):
            continue
        c, parts = kicad.capacitance_between(summary, net)
        if c:
            caps[net] = {'c_f': c, 'parts': parts}
    return {'pcb': entry, 'components': len(summary['components']), 'nets': list(summary['nets']),
            'bulk_capacitance_by_net': caps,
            'copper_ohm_by_net': {k: v['series_ohm'] for k, v in summary['copper'].items()},
            'note': note}


def import_model(project: Project, path, name=None, parent='world', pos=None, euler_deg=None,
                 units='mm', density=1240.0, mass_kg=None, collision='hull', rgba=None):
    """STL/STEP/OBJ part, or an MJCF (.xml) used as the mechanical base model."""
    path = Path(path)
    if path.suffix.lower() == '.xml':
        rel = Path(_relpath(path.resolve(), project.root))
        project.data.setdefault('mechanics', {})['mjcf'] = str(rel)
        project.save()
        import mujoco
        m = mujoco.MjModel.from_xml_path(str(path))
        return {'mjcf': str(rel), 'bodies': [m.body(i).name for i in range(m.nbody)],
                'joints': [m.joint(i).name for i in range(m.njnt)]}
    from .importers.mesh import import_mesh
    name = name or path.stem
    info = import_mesh(path, project.root / 'mechanics' / 'meshes', name, units=units, density=density,
                       collision=collision, mass_kg=mass_kg)
    info.update({'parent': parent, 'pos': pos or [0, 0, 0]})
    if euler_deg:
        info['euler_deg'] = euler_deg
    if rgba:
        info['rgba'] = rgba
    project.data.setdefault('mechanics', {}).setdefault('parts', {})[name] = info
    project.save()
    return {'part': name, **{k: info[k] for k in ('mass_kg', 'bounds_m', 'watertight', 'collision')}}


def _relpath(p, root):
    import os
    return os.path.relpath(p, root)


def import_firmware(project: Project, path, name='firmware', params=None):
    entry = fwmod.import_firmware(path, project.root / 'firmware', name)
    mcu = project.data.setdefault('mcu', {})
    fw = mcu.setdefault('firmware', {})
    fw.update({'flash': f"firmware/{entry['flash']}", 'size_bytes': entry['size_bytes']})
    has_params = 'fw_params' in entry['partitions']
    if has_params:
        fw.setdefault('params', {})
        if params:
            fw['params'].update(params)
    project.save()
    return {'firmware': fw, 'partitions': entry['partitions'], 'tunable_params':
            sorted(fwmod.FW_PARAMS_DEFAULTS) if has_params else []}


# -------------------------------------------------------------- annotate ----

def define_joint(project: Project, name, body, type='hinge', axis=None, pos=None, range_deg=None,
                 **extra):
    j = {'body': body, 'type': type}
    if axis is not None:
        j['axis'] = axis
    if pos is not None:
        j['pos'] = pos
    if range_deg is not None:
        j['range_deg'] = range_deg
    j.update({k: v for k, v in extra.items() if v is not None})
    project.data.setdefault('mechanics', {}).setdefault('joints', {})[name] = j
    project.save()
    from .mechanics import Mechanics
    mech = Mechanics(project.root, project.data['mechanics'], project.pcb())
    return {'joint': name, 'joints_in_model': list(mech.joints)}


def bind_pin(project: Project, pin, actuator=None, role=None, input=None, net=None,
             actuator_type=None, joint=None, supply_node=None, preset=None, active='low', **params):
    """Wire an MCU pin to an actuator input (pin -> actuator[.role]) or an external
    input device to an MCU pin (input -> pin). Creates the actuator/input if the
    type information is given. With a PCB, `net` is checked and the copper path
    from the MCU connector to the actuator connector is reported."""
    n = pin_number(pin)
    binding = {'pin': f'GPIO{n}'}
    if net:
        binding['net'] = net
    data = project.data
    if actuator:
        acts = data.setdefault('actuators', {})
        if actuator not in acts:
            if not (actuator_type and joint and supply_node):
                raise ValueError(f'actuator {actuator!r} does not exist; give actuator_type, joint and supply_node')
            acts[actuator] = {'type': actuator_type, 'joint': joint, 'node': supply_node}
            if preset:
                acts[actuator]['preset'] = preset
        acts[actuator].update(params)
        binding['to'] = f'{actuator}.{role}' if role else actuator
    elif input:
        ins = data.setdefault('inputs', {})
        ins.setdefault(input, {'type': 'button', 'active': active})
        binding['from'] = input
    else:
        raise ValueError('give actuator=... or input=...')
    report = {}
    pcb = project.pcb()
    if net and pcb:
        if net not in pcb['nets']:
            raise ValueError(f'net {net!r} is not on the PCB; nets: {list(pcb["nets"])}')
        report['net_pads'] = pcb['nets'][net]
        report['reaches'] = _net_neighbours(pcb, net)
    bindings = [b for b in data.setdefault('bindings', []) if pin_number(b['pin']) != n
                or ('to' in b) != ('to' in binding)]
    bindings.append(binding)
    data['bindings'] = bindings
    project.save()
    return {'binding': binding, **report}


def _net_neighbours(pcb, net):
    """Nets reachable through two-terminal parts (e.g. a series resistor)."""
    out = []
    for c in pcb['components']:
        nets = [x for x in c['pads'].values() if x]
        if net in nets and len(set(nets)) == 2:
            other = next(x for x in nets if x != net)
            out.append({'via': c['ref'], 'value': c.get('value'), 'net': other, 'pads': pcb['nets'].get(other)})
    return out


def set_param(project: Project, path, value):
    if path.startswith('firmware.'):
        path = 'mcu.firmware.params.' + path[len('firmware.'):]
        key = path.rsplit('.', 1)[1]
        if key not in fwmod.FW_PARAMS_DEFAULTS:
            raise KeyError(f'unknown firmware parameter {key!r}; known: {sorted(fwmod.FW_PARAMS_DEFAULTS)}')
    old = get_path(project.data, path, None)
    project.set(path, value)
    return {'path': path, 'old': old, 'new': value}


def get_param(project: Project, path=None):
    return project.data if not path else get_path(project.data, path)


# ------------------------------------------------------------------- run ----

def _mcu_factory(project):
    def make(data, scenario):
        from .mcu import QemuMcu, ScriptedMcu
        mcu = data.get('mcu', {})
        backend = scenario.get('mcu_backend', mcu.get('backend', 'qemu'))
        if backend == 'scripted':
            return ScriptedMcu(scenario.get('pin_script'))
        fw = mcu.get('firmware') or {}
        if not fw.get('flash'):
            raise ValueError('no firmware imported (import_firmware) and scenario does not use mcu_backend: scripted')
        return QemuMcu(project.path(fw['flash']), params=fw.get('params'), step_ms=mcu.get('step_ms', 1),
                       icount_shift=mcu.get('icount_shift', 3))
    return make


def run_scenario(project: Project, scenario, overrides=None, tag=None, frame_every_ms=10):
    sc = project.scenario(scenario) if isinstance(scenario, str) else scenario
    all_overrides = dict(sc.get('overrides') or {})
    all_overrides.update(overrides or {})
    data = project.resolved(_alias(all_overrides))
    run_id = new_run_id(sc['name'] + (f'-{tag}' if tag else ''))
    rec = Recorder(project.root / 'runs' / run_id)
    sim = Simulation(project, data, sc, _mcu_factory(project), frame_every_ms=frame_every_ms)
    info = sim.run(rec)
    t, cols = rec.columns()
    metrics = compute_metrics(t, cols, sim.mcu.events, sim.bod.events, sim.mech)
    checks = evaluate(sc.get('expect'), t, cols, metrics)
    passed = all(c['pass'] for c in checks) if checks else None
    meta = {'run_id': run_id, 'created': time.strftime('%Y-%m-%d %H:%M:%S'), 'scenario': sc,
            'overrides': all_overrides, 'project_fingerprint': project.fingerprint(data),
            'resolved_project': data, 'metrics': metrics, 'checks': checks, 'passed': passed,
            'log': info['log'], 'brownout_detector': sim.bod.events,
            'timing': {k: info[k] for k in ('wall_s', 'sim_s', 'substeps', 'dt_mech_s')},
            'mcu_backend': sim.mcu.backend}
    rec.finish(meta, sim.mcu.uart, sim.mcu.events)
    return summarize(meta)


def _alias(overrides):
    out = {}
    for k, v in overrides.items():
        out['mcu.firmware.params.' + k[len('firmware.'):] if k.startswith('firmware.') else k] = v
    return out


def summarize(meta):
    m = meta['metrics']
    keep = {k: v for k, v in m.items() if not k.startswith('joint.') or k.endswith(('.max', '.min', '.final'))}
    return {'run_id': meta['run_id'], 'scenario': meta['scenario']['name'], 'passed': meta['passed'],
            'overrides': meta['overrides'], 'checks': meta['checks'], 'metrics': keep,
            'mcu_events': [f"{e['t']:.3f}s {e['event']} {e['detail']}".strip() for e in meta['mcu_events']][:40],
            'timing': meta['timing']}


def sweep(project: Project, scenario, params, metrics=None, extra_overrides=None):
    """params: {path: [values...]} -> full grid. Returns one row per run."""
    keys = list(params)
    rows = []
    for combo in itertools.product(*[params[k] for k in keys]):
        ov = dict(extra_overrides or {})
        ov.update(dict(zip(keys, combo)))
        s = run_scenario(project, scenario, ov, tag='sweep', frame_every_ms=50)
        row = {'run_id': s['run_id'], **dict(zip(keys, combo)), 'passed': s['passed']}
        for name in metrics or []:
            row[name] = s['metrics'].get(name)
        row['failed_checks'] = [c['check'] for c in s['checks'] if not c['pass']]
        rows.append(row)
    return {'scenario': scenario if isinstance(scenario, str) else scenario['name'], 'params': keys, 'rows': rows}


def get_results(project: Project, run_id=None, signals=None, t0=None, t1=None, max_points=500):
    store = RunStore(project.root)
    if run_id is None:
        runs = store.list()
        if not runs:
            return {'runs': []}
        run_id = runs[0]['run_id']
    if run_id == 'list':
        return {'runs': store.list()[:50]}
    meta = store.meta(run_id)
    out = summarize(meta)
    out['signals_available'] = store.signal_names(run_id)
    if signals:
        out['data'] = store.signals(run_id, signals, t0, t1, max_points)
    uart = (store.dir(run_id) / 'uart.log').read_text(encoding='utf8').splitlines()
    out['uart_tail'] = uart[-30:]
    return out


def render_frames(project: Project, run_id, times, width=640, height=480, camera=None, out_dir=None):
    """Re-render MuJoCo frames of a stored run at the given times (offscreen)."""
    import mujoco
    from .mechanics import Mechanics
    store = RunStore(project.root)
    meta = store.meta(run_id)
    data = meta['resolved_project']
    mech = Mechanics(project.root, data['mechanics'], project.pcb() if data.get('pcb') else None)
    qpos_file = store.dir(run_id) / 'qpos.npz'
    z = np.load(qpos_file)
    ft, q = z['t'], z['qpos']
    out_dir = Path(out_dir or store.dir(run_id) / 'frames')
    out_dir.mkdir(parents=True, exist_ok=True)
    r = mujoco.Renderer(mech.model, height=height, width=width)
    cam = mujoco.MjvCamera()
    mujoco.mjv_defaultFreeCamera(mech.model, cam)
    for k, v in (camera or {}).items():
        if k == 'lookat':
            cam.lookat[:] = v
        else:
            setattr(cam, k, v)
    paths = []
    from PIL import Image
    for t in times:
        i = int(np.argmin(np.abs(ft - t)))
        mech.data.qpos[:] = q[i]
        mujoco.mj_forward(mech.model, mech.data)
        r.update_scene(mech.data, camera=cam)
        p = out_dir / f'frame_{ft[i]:07.3f}s.png'
        Image.fromarray(r.render()).save(p)
        paths.append(str(p))
    r.close()
    return {'run_id': run_id, 'frames': paths}


def project_overview(project: Project):
    d = project.data
    return {'name': d.get('name'), 'root': str(project.root), 'scenarios': project.scenarios(),
            'pcb': d.get('pcb'), 'firmware': (d.get('mcu') or {}).get('firmware'),
            'mechanics': {k: (list(v) if isinstance(v, dict) else v) for k, v in (d.get('mechanics') or {}).items()},
            'actuators': d.get('actuators'), 'inputs': d.get('inputs'), 'bindings': d.get('bindings'),
            'power': d.get('power'), 'mcu': {k: v for k, v in (d.get('mcu') or {}).items() if k != 'firmware'},
            'recent_runs': RunStore(project.root).list()[:10]}


__all__ = [n for n in dir() if not n.startswith('_')] + ['copy']
