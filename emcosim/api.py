"""High-level operations shared by the CLI and the MCP server. Every function
takes a Project, edits project.yaml (or reads runs/) and returns plain data."""
from __future__ import annotations

import copy
import itertools
import os
import re
import shutil
import sys
import time
from pathlib import Path

import numpy as np

from . import firmware as fwmod
from .engine import Simulation, compute_metrics, evaluate
from .importers import kicad
from .project import Project, dump_yaml, get_path, pin_number
from .results import Recorder, RunStore, new_run_id


def offscreen_renderer(model, height, width):
    """mujoco.Renderer, but on a Linux machine with no display and no MUJOCO_GL it raises at once:
    there MuJoCo falls back to GLFW, which hangs instead of failing when called off the main
    thread (the MCP tools run in worker threads)."""
    import mujoco
    if (sys.platform.startswith('linux') and not os.environ.get('MUJOCO_GL')
            and not os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY')):
        raise RuntimeError('no display for OpenGL: set MUJOCO_GL=egl or MUJOCO_GL=osmesa on a headless machine')
    return mujoco.Renderer(model, height=int(height), width=int(width))


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


DEFAULT_DENSITY = 1240.0       # kg/m3, about PLA; only used when neither mass nor density is given


def import_model(project: Project, path, name=None, parent='world', pos=None, euler_deg=None,
                 units='mm', density=None, mass_kg=None, collision='hull', rgba=None):
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
    warnings = []
    assumed_density = mass_kg is None and density is None
    if assumed_density:
        density = DEFAULT_DENSITY
    parts = project.data.setdefault('mechanics', {}).setdefault('parts', {})
    if name in parts:
        warnings.append(f'part {name!r} already existed and was replaced (old file {parts[name].get("source")})')
    if parent not in (None, 'world') and parent not in parts and not project.data['mechanics'].get('mjcf'):
        raise ValueError(f'parent {parent!r} is not a part of this project; parts: {sorted(parts) or "none"} '
                         '(use "world" for a part fixed in space)')
    info = import_mesh(path, project.root / 'mechanics' / 'meshes', name, units=units, density=density,
                       collision=collision, mass_kg=mass_kg)
    info.update({'parent': parent, 'pos': pos or [0, 0, 0]})
    if assumed_density:
        info['mass_assumed'] = True
        warnings.append(f'mass {info["mass_kg"] * 1000:.1f} g is ASSUMED: no mass_kg or density was given, so the '
                        f'solid volume was multiplied by {DEFAULT_DENSITY:.0f} kg/m3 (about PLA, 100 % infill). '
                        'Pass mass_kg (measured or from the datasheet) or density when it matters.')
    if euler_deg:
        info['euler_deg'] = euler_deg
    if rgba:
        info['rgba'] = rgba
    parts[name] = info
    project.save()
    out = {'part': name, **{k: info[k] for k in ('mass_kg', 'bounds_m', 'watertight', 'collision')},
           'units': {'pos': 'm', 'bounds_m': 'm', 'mesh': units}}
    if warnings:
        out['warnings'] = warnings
    return out


MODEL_UPLOAD_TYPES = ('.stl', '.step', '.stp', '.obj')


def model_summary(project: Project):
    """What the 建模 page lists: the mechanical model, its parts and joints, and the print parts."""
    mech = project.data.get('mechanics') or {}
    out = {'mjcf': mech.get('mjcf'), 'parts': [], 'bodies': [], 'joints': [], 'print_parts': [], 'error': None}
    labels = {}
    for key, v in (mech.get('part_labels') or {}).items():
        v = v if isinstance(v, dict) else {'label': v}
        labels[str(key)] = {'label': str(v.get('label') or key), 'print': v.get('print')}
    for name, part in (mech.get('parts') or {}).items():
        labels.setdefault(name, {'label': part.get('label') or name, 'print': part.get('print')})
    out['labels'] = labels
    for name, part in (mech.get('parts') or {}).items():
        out['parts'].append({'name': name, 'file': part.get('file'), 'mass_kg': part.get('mass_kg'),
                             'parent': part.get('parent', 'world')})
    for name, pp in ((project.data.get('printing') or {}).get('parts') or {}).items():
        f = pp.get('file')
        out['print_parts'].append({'name': name, 'label': pp.get('label') or name, 'file': f,
                                   'material': pp.get('material'), 'settings': pp.get('settings') or {},
                                   'exists': bool(f) and project.path(f).is_file()})
    out['has_model'] = bool(out['mjcf'] or out['parts'])
    from .checks import GEAR_DEFAULTS
    out['gear_defaults'] = GEAR_DEFAULTS
    from .strength.materials import MATERIALS
    out['materials'] = {k: {'strength_mpa': v['Xt'], 'layer_strength_mpa': v['Zt'], 'modulus_mpa': v['E_xy'],
                            'soften_c': v['T_soften_c']} for k, v in MATERIALS.items()}
    if not out['has_model']:
        return out
    try:
        import mujoco
        from .mechanics import Mechanics
        m = Mechanics(project.root, mech, None).model     # the PCB lives on its own page
    except Exception as e:  # a broken model is shown on the page, not raised
        out['error'] = str(e)
        return out
    types = {mujoco.mjtJoint.mjJNT_HINGE: 'hinge', mujoco.mjtJoint.mjJNT_SLIDE: 'slide',
             mujoco.mjtJoint.mjJNT_BALL: 'ball', mujoco.mjtJoint.mjJNT_FREE: 'free'}
    geoms = {}
    for g in range(m.ngeom):
        if m.geom_group[g] <= 2 and m.geom_rgba[g][3] > 0 and not is_pcb_geom(m.geom(g).name):
            b = int(m.geom_bodyid[g])
            geoms[b] = geoms.get(b, 0) + 1
    for b in range(1, m.nbody):
        if m.body(b).name == 'pcb':
            continue
        out['bodies'].append({'name': m.body(b).name, 'parent': m.body(int(m.body_parentid[b])).name or 'world',
                              'mass_kg': round(float(m.body_mass[b]), 6), 'geoms': geoms.get(b, 0)})
    cfg_joints = mech.get('joints') or {}
    for j in range(m.njnt):
        name = m.joint(j).name
        jt = types.get(int(m.jnt_type[j]), 'other')
        row = {'name': name, 'type': jt, 'body': m.body(int(m.jnt_bodyid[j])).name}
        if m.jnt_limited[j]:
            lo, hi = (float(x) for x in m.jnt_range[j])
            row['range'] = [round(np.degrees(lo), 2), round(np.degrees(hi), 2)] if jt == 'hinge' else [lo, hi]
        if name in cfg_joints and 'range_deg' in cfg_joints[name]:
            row['range'] = cfg_joints[name]['range_deg']
        out['joints'].append(row)
    return out


def is_pcb_geom(name):
    """PCB geometry: boards baked into the MJCF (pcb_visual_*) or added from the board summary (pcb_*)."""
    return (name or '').startswith('pcb_')


def model_preview(project: Project, out_dir):
    """scene.json / scene.bin / frames.bin of the model at its initial pose (one frame), in the
    same format as a run, so the page shows the model before anything has been simulated.
    Mechanical parts only: the PCB is shown on the PCB page, not here."""
    from .mechanics import Mechanics
    from .results import write_scene
    mech = Mechanics(project.root, project.data.get('mechanics') or {}, None)
    ids = mech.visual_bodies()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_scene(mech.model, ids, out_dir, skip=is_pcb_geom)
    pose = mech.poses(ids).astype(np.float32)
    with open(out_dir / 'frames.bin', 'wb') as f:
        f.write(np.array([1, len(ids)], dtype=np.int32).tobytes())
        f.write(np.zeros(1, dtype=np.float32).tobytes())
        f.write(pose.tobytes())
    return out_dir


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
                 range_mm=None, **extra):
    if type not in ('hinge', 'slide', 'ball', 'free'):
        raise ValueError(f"joint type {type!r}: use 'hinge' (rotates, range_deg) or 'slide' (moves, range_mm)")
    j = {'body': body, 'type': type}
    if axis is not None:
        j['axis'] = axis
    if pos is not None:
        j['pos'] = pos
    if type == 'slide' and range_deg is not None and range_mm is None:
        raise ValueError('a slide joint moves in a straight line: give its travel as range_mm '
                         '(millimetres), not range_deg')
    if type == 'hinge' and range_mm is not None:
        raise ValueError('a hinge joint rotates: give its range as range_deg')
    if range_deg is not None:
        j['range_deg'] = range_deg
    if range_mm is not None:
        j['range_mm'] = range_mm
    j.update({k: v for k, v in extra.items() if v is not None})
    project.data.setdefault('mechanics', {}).setdefault('joints', {})[name] = j
    project.save()
    from .mechanics import Mechanics
    mech = Mechanics(project.root, project.data['mechanics'], project.pcb())
    units = {'hinge': 'range in degrees; signals joint.<name>.deg and .dps',
             'slide': 'range in millimetres; signals joint.<name>.mm and .mm_s'}.get(type, '')
    return {'joint': name, 'joints_in_model': list(mech.joints), 'units': units}


def bind_pin(project: Project, pin, actuator=None, role=None, input=None, net=None,
             actuator_type=None, joint=None, supply_node=None, preset=None, active='low', **params):
    """Wire an MCU pin to an actuator input (pin -> actuator[.role]) or an external
    input device to an MCU pin (input -> pin). Creates the actuator/input if the
    type information is given. With a PCB, `net` is checked and the copper path
    from the MCU connector to the actuator connector is reported."""
    n = pin_number(pin)
    warnings = esp32_pin_check(n, 'output' if actuator else 'input', input_type=params.get('input_type'),
                               active=active)
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
        itype = params.pop('input_type', None) or (ins.get(input) or {}).get('type') or 'button'
        if itype == 'button':
            ins[input] = {**(ins.get(input) or {}), 'type': 'button', 'active': active}
        elif itype == 'pot':
            cfg = {'type': 'pot', 'v_ref': params.pop('v_ref', 3.3)}
            if joint:
                cfg['joint'] = joint
                cfg['range_deg'] = params.pop('range_deg', None) or [0, 270]
            else:
                cfg['value'] = params.pop('value', 0.5)
            ins[input] = cfg
        else:
            raise ValueError(f"input_type {itype!r}: use 'button' (digital) or 'pot' (analog, ADC)")
        params.clear()
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
    elif net:
        design = _design_netlist(project)
        if design is not None:
            nets, parts = design
            if net not in nets:
                raise ValueError(f'net {net!r} is not in the schematic/board; nets: {sorted(nets)}')
            report['net_pins'] = nets[net]
            pull = _pull_up(nets, parts, net)
            if pull:
                report['pull_up'] = pull
                warnings = [w for w in warnings if 'pull-up' not in w]
    bindings = [b for b in data.setdefault('bindings', []) if pin_number(b['pin']) != n
                or ('to' in b) != ('to' in binding)]
    bindings.append(binding)
    data['bindings'] = bindings
    project.save()
    if warnings:
        report['warnings'] = warnings
    return {'binding': binding, **report}


def _design_netlist(project):
    """({net: ['REF.PIN', ...]}, {ref: value}) of the schematic, else the board; None without either."""
    b = project.data.get('board') or {}
    try:
        if b.get('kicad_sch'):
            from . import eda
            nl = eda.netlist(project.path(b['kicad_sch']))
            return nl['nets'], {c['ref']: c.get('value') for c in nl['components']}
        if b.get('kicad_pcb'):
            from .importers.kicad import load_kicad_pcb
            pcb = load_kicad_pcb(project.path(b['kicad_pcb']))
            return pcb['nets'], {c['ref']: c.get('value') for c in pcb['components']}
    except Exception:          # no KiCad: the binding still works, only the check is skipped
        return None
    return None


def _pull_up(nets, parts, net):
    """A resistor between `net` and a supply-looking net, e.g. 'R4 10k to 3V3'."""
    supply = re.compile(r'^\+?(3V3|3\.3V|VCC|VDD|VBAT|5V|VBUS|3V)', re.I)
    pin_net = {p: n for n, pins in nets.items() for p in pins}
    for p in nets.get(net, []):
        ref = p.split('.')[0]
        if not re.match(r'R\d', ref):
            continue
        other = [n for q, n in pin_net.items() if q.split('.')[0] == ref and n != net]
        if other and supply.match(other[0]):
            return f'{ref} {parts.get(ref) or ""} to {other[0]}'.replace('  ', ' ')
    return None


ESP32_INPUT_ONLY = range(34, 40)
ESP32_FLASH = range(6, 12)
ESP32_ADC1 = range(32, 40)
ESP32_ADC2 = (0, 2, 4, 12, 13, 14, 15, 25, 26, 27)
ESP32_STRAP = (0, 2, 5, 12, 15)


def esp32_pin_check(n, direction, input_type=None, active='low'):
    """Errors for pins that cannot work, warnings for ones that need care (ESP32 / ESP32-PICO)."""
    if n in ESP32_FLASH:
        raise ValueError(f'GPIO{n} is wired to the SPI flash on ESP32 modules and cannot be used')
    if n > 39 or n in (20, 24, 28, 29, 30, 31):
        raise ValueError(f'GPIO{n} does not exist on the ESP32')
    if direction == 'output' and n in ESP32_INPUT_ONLY:
        raise ValueError(f'GPIO{n} is input-only on the ESP32 (GPIO34-39); pick an output-capable pin')
    w = []
    if input_type == 'pot':
        if n in ESP32_ADC2:
            w.append(f'GPIO{n} is on ADC2, which cannot be read while Wi-Fi or Bluetooth is on; '
                     'prefer ADC1 (GPIO32-39)')
        elif n not in ESP32_ADC1:
            w.append(f'GPIO{n} has no ADC; an analog input needs GPIO32-39 (ADC1)')
    elif direction == 'input' and n in ESP32_INPUT_ONLY and active == 'low':
        w.append(f'GPIO{n} (34-39) has no internal pull-up: an active-low button needs an external '
                 'pull-up resistor (e.g. 10 k to 3V3) on the board')
    if n in ESP32_STRAP:
        w.append(f'GPIO{n} is a boot strapping pin; make sure what is wired to it does not hold it at the '
                 'wrong level during reset')
    return w


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
        if backend == 'behavior':
            from .mcu.behavior import BehaviorMcu
            script = scenario.get('behavior_script') or (mcu.get('behavior') or {}).get('script')
            if not script:
                raise ValueError('mcu_backend: behavior needs behavior_script (write one with write_behavior)')
            return BehaviorMcu(project.path(script), params=(mcu.get('behavior') or {}).get('params'))
        if backend != 'qemu':
            raise ValueError(f'mcu_backend {backend!r}: use qemu (real firmware), scripted or behavior')
        fw = mcu.get('firmware') or {}
        if not fw.get('flash'):
            raise ValueError('no firmware imported (import_firmware) and scenario does not use mcu_backend: scripted')
        return QemuMcu(project.path(fw['flash']), params=fw.get('params'), step_ms=mcu.get('step_ms', 1),
                       icount_shift=mcu.get('icount_shift', 3))
    return make


def run_scenario(project: Project, scenario, overrides=None, tag=None, frame_every_ms=10,
                 progress=None, cancel=None):
    """progress(fraction, t, signals) and cancel (threading.Event) are optional
    hooks used by the desktop app; a cancelled run leaves no directory behind."""
    sc = project.scenario(scenario) if isinstance(scenario, str) else scenario
    all_overrides = dict(sc.get('overrides') or {})
    all_overrides.update(overrides or {})
    data = project.resolved(_alias(all_overrides))
    run_id = new_run_id(sc['name'] + (f'-{tag}' if tag else ''))
    rec = Recorder(project.root / 'runs' / run_id)
    try:
        sim = Simulation(project, data, sc, _mcu_factory(project), frame_every_ms=frame_every_ms)
        info = sim.run(rec, progress=progress, cancel=cancel)
    except BaseException:
        shutil.rmtree(rec.dir, ignore_errors=True)
        raise
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
    files = getattr(sim.mcu, 'files', None) or {}
    meta['files'] = sorted(files)
    rec.finish(meta, sim.mcu.uart, sim.mcu.events)
    for name, text in files.items():
        (rec.dir / name).write_text(text, encoding='utf-8')
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
            'scenario_events': [f"{e['t']:.3f}s {e.get('detail')}" for e in meta.get('log', [])
                                if e.get('event') == 'scenario'][:20],
            'files': meta.get('files') or [],
            'timing': meta['timing']}


def sweep(project: Project, scenario, params, metrics=None, extra_overrides=None, progress=None,
          cancel=None, on_row=None):
    """params: {path: [values...]} -> full grid. Returns one row per run."""
    keys = list(params)
    rows = []
    combos = list(itertools.product(*[params[k] for k in keys]))
    for i, combo in enumerate(combos):
        ov = dict(extra_overrides or {})
        ov.update(dict(zip(keys, combo)))
        sub = None
        if progress is not None:
            sub = (lambda f, t, sig, i=i: progress((i + f) / len(combos), t, sig))
        s = run_scenario(project, scenario, ov, tag='sweep', frame_every_ms=50, progress=sub, cancel=cancel)
        row = {'run_id': s['run_id'], **dict(zip(keys, combo)), 'passed': s['passed']}
        for name in metrics or []:
            row[name] = s['metrics'].get(name)
        row['failed_checks'] = [c['check'] for c in s['checks'] if not c['pass']]
        rows.append(row)
        if on_row is not None:
            on_row(row)
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
    r = offscreen_renderer(mech.model, height, width)
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


def write_scenario(project: Project, name, scenario):
    """Create or replace scenarios/<name>.yaml from a mapping or YAML text."""
    import re
    import yaml
    if not re.fullmatch(r'[\w\-]+', name or ''):
        raise ValueError('scenario names use letters, digits, _ and - only')
    data = yaml.safe_load(scenario) if isinstance(scenario, str) else scenario
    if not isinstance(data, dict):
        raise ValueError('scenario must be a YAML mapping')
    (project.root / 'scenarios').mkdir(exist_ok=True)
    # Keep the user's text (and its comments) as written; only mappings are re-dumped.
    text = scenario if isinstance(scenario, str) else dump_yaml(data)
    (project.root / 'scenarios' / f'{name}.yaml').write_text(text, encoding='utf8')
    return {'scenario': name, 'scenarios': project.scenarios()}


def fit_measurements(project: Project, path, apply=False, allow_example=False):
    """Fit servo / supply / switch parameters from a measurement sheet
    (examples/switch_robot/measurements/template.yaml)."""
    from . import fit
    r = fit.fit_measurements(project, path, apply=apply, allow_example=allow_example)
    r['text'] = fit.format_report(r)
    return r


def compare_trace(project: Project, trace_csv, signal, run_id=None, column=None, time_column=None,
                  time_unit='s', t_offset=0.0, align=False, max_lag_s=0.5, window=None, scale=1.0):
    """Error of a run's signal against a measured CSV trace (scope / logging meter)."""
    from . import fit
    return fit.compare_run(project, run_id, trace_csv, signal, column=column, time_column=time_column,
                           time_unit=time_unit, t_offset=t_offset, align=align, max_lag_s=max_lag_s,
                           window=window, scale=scale)


def copy_project(src, dst):
    """Copy a project (without runs/) to dst and make it self-contained: a base
    MJCF whose meshdir points outside the project (the switch_robot example reuses
    ../../../../simulation/assets) gets the referenced meshes copied next to it."""
    import os
    import xml.etree.ElementTree as ET
    src, dst = Path(src).resolve(), Path(dst).resolve()
    if (dst / 'project.yaml').exists():
        return {'root': str(dst), 'copied': False}
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns('runs', '__pycache__', '*.pyc'))
    prj = Project(dst)
    mjcf = (prj.data.get('mechanics') or {}).get('mjcf')
    if mjcf:
        f_src, f_dst = src / mjcf, dst / mjcf
        tree = ET.parse(f_src)
        comp = tree.getroot().find('compiler')
        mdir = comp.get('meshdir') if comp is not None else None
        if mdir:
            mesh_src = (f_src.parent / mdir).resolve()
            if src not in mesh_src.parents and mesh_src != src:
                out = f_dst.parent / 'assets'
                out.mkdir(exist_ok=True)
                for m in tree.getroot().iter('mesh'):
                    if m.get('file'):
                        shutil.copy2(mesh_src / m.get('file'), out / Path(m.get('file')).name)
                comp.set('meshdir', 'assets')
                tree.write(f_dst, encoding='unicode')
    # Printable parts referenced outside the project (../../../mechanical/*.stl).
    parts = ((prj.data.get('printing') or {}).get('parts') or {})
    changed = False
    for name, part in parts.items():
        f = (src / part['file']).resolve()
        if f.exists() and src not in f.parents:
            (dst / 'print' / 'parts').mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst / 'print' / 'parts' / f.name)
            part['file'] = f'print/parts/{f.name}'
            changed = True
    if changed:
        prj.save()
    return {'root': str(dst), 'copied': True}


def delete_run(project: Project, run_id):
    d = RunStore(project.root).dir(run_id)
    shutil.rmtree(d)
    return {'deleted': run_id}


def project_overview(project: Project):
    d = project.data
    return {'name': d.get('name'), 'root': str(project.root), 'scenarios': project.scenarios(),
            'pcb': d.get('pcb'), 'firmware': (d.get('mcu') or {}).get('firmware'),
            'mechanics': {k: (list(v) if isinstance(v, dict) else v) for k, v in (d.get('mechanics') or {}).items()},
            'actuators': d.get('actuators'), 'inputs': d.get('inputs'), 'bindings': d.get('bindings'),
            'power': d.get('power'), 'mcu': {k: v for k, v in (d.get('mcu') or {}).items() if k != 'firmware'},
            'recent_runs': RunStore(project.root).list()[:10]}


__all__ = [n for n in dir() if not n.startswith('_')] + ['copy']
