"""Checks shown on the 仿真 page after a run: strength of printed parts (FEA) and the
bending stress in the servo's output gear. Both take their loads from a stored run.

Strength: a print part opts in with printing.parts.<name>.strength: <config yaml>
(see examples/switch_robot/strength/press_arm.yaml). Two load cases are solved:
the contact-force peak of the run at the load face, and the servo stall torque
reacted at the same face (what the servo can push at most). Material and slicer
settings come from printing.parts.<name> (material, settings), so changing them on
the 建模 page changes the answer.

Gears: Lewis bending stress of the output gear tooth, sigma = Ft / (b * m * Y),
Ft = T / r, for the peak torque of the run and the stall torque. Gear geometry and
allowable stress are typical values per servo model (assumed, not measured) and can
be overridden with actuators.<name>.gear.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from .project import Project, load_yaml

RESULTS_DIR = Path('runs') / '_checks'      # runs/ is git-ignored; no run.json, so not listed as a run


# --------------------------------------------------------------------------- strength

def strength_parts(project: Project) -> list[dict]:
    out = []
    for name, pp in ((project.data.get('printing') or {}).get('parts') or {}).items():
        if pp.get('strength'):
            out.append({'name': name, 'label': pp.get('label') or name, 'material': pp.get('material'),
                        'config': pp['strength']})
    return out


def _print_settings(pp):
    from .strength import PrintSettings
    s = pp.get('settings') or {}
    kw = {}
    if 'layer_height' in s:
        kw['layer_height_mm'] = float(s['layer_height'])
    if 'wall_loops' in s:
        kw['walls'] = int(s['wall_loops'])
    if 'sparse_infill_density' in s:
        kw['infill_pct'] = float(str(s['sparse_infill_density']).rstrip('%'))
    if 'top_shell_layers' in s:
        kw['top_bottom_layers'] = int(s['top_shell_layers'])
    return PrintSettings(**kw)


def _boundary_faces(t):
    """Faces of a tet mesh that belong to one tet only, oriented outwards."""
    f = np.concatenate([t[:, [0, 2, 1]], t[:, [0, 1, 3]], t[:, [0, 3, 2]], t[:, [1, 2, 3]]])
    key = np.sort(f, axis=1)
    _, idx, cnt = np.unique(key, axis=0, return_index=True, return_counts=True)
    return f[idx[cnt == 1]]


def strength_check(project: Project, part: str, run_id=None, progress=None) -> dict:
    from .strength import check_part, loads_from_run
    pp = ((project.data.get('printing') or {}).get('parts') or {}).get(part)
    if not pp or not pp.get('strength'):
        raise ValueError(f'打印件 {part} 没有强度检测设置（printing.parts.{part}.strength）')
    cfg = load_yaml(project.path(pp['strength']))
    material = pp.get('material') or 'PLA'
    build = cfg.get('build_dir') or next(iter((cfg.get('orientations') or {'z': ['z']}).values()))[0]
    ps = _print_settings(pp)
    run_cfg = cfg.get('run') or {}
    kw = dict(probe=run_cfg.get('probe', 'press'), actuator=run_cfg.get('actuator', 'servo1'), pick='newest')
    try:   # the run shown on the page, else the newest run of the configured scenario, else the newest run
        loads = loads_from_run(project.root, run_id=run_id, scenario=None if run_id else run_cfg.get('scenario'), **kw)
    except FileNotFoundError:
        loads = loads_from_run(project.root, run_id=None, scenario=None, **kw)
    fixtures = cfg.get('fixtures') or cfg.get('fixtures_horn')
    cases = []
    if 'press_force_peak_n' in loads:
        cases.append(('press', '按压时（仿真里的最大接触力）', loads['press_force_peak_n']))
    if 'servo_stall_torque_nm' in loads and cfg.get('tip_lever_mm'):
        cases.append(('stall', '舵机堵转（舵机能推出的最大力）', loads['servo_stall_torque_nm'] * 1000.0 / cfg['tip_lever_mm']))
    if not cases:
        raise ValueError('这次仿真里没有接触力或舵机力矩，算不了强度')
    common = dict(print_settings=ps, transform=cfg.get('transform'), unit_scale=cfg.get('unit_scale', 1.0),
                  mesh_size_mm=cfg.get('mesh_size_mm', 1.0), exclude_mm=cfg.get('exclude_mm', 1.0),
                  singular_zones=cfg.get('singular_zones') or (), probes=cfg.get('probes'),
                  corners=cfg.get('corners'), cache_dir=str(Path(pp['strength']).parent / '.cache'))
    out_cases, worst = [], None
    for i, (key, label, force) in enumerate(cases):
        if progress:
            progress(i / len(cases))
        ld = [{'name': key, 'select': cfg['tip_face'], 'force_n': [0, 0, float(force)]}]
        r = check_part(project.root, cfg['part_mesh'], material, build, fixtures, ld, keep_fields=True, **common)
        fields = r.pop('_fields')
        modes = {m: {'min_sf': v['min_sf'], 'at_mm': v.get('at_mm')} for m, v in r['modes'].items()}
        sf = min(v['min_sf'] for v in modes.values())
        row = {'case': key, 'label': label, 'force_n': round(float(force), 3), 'min_sf': round(float(sf), 2),
               'governing': r['governing_mode'], 'modes': modes,
               'max_von_mises_mpa': round(float(r['max_von_mises_mpa']), 2),
               'max_deflection_mm': round(float(r['max_deflection_mm']), 4),
               'tip_deflection_mm': round(float(r['probes']['tip']['|u|_mm']), 4) if r.get('probes', {}).get('tip') else None,
               'mesh': r.get('mesh'), 'timing_s': r.get('timing_s')}
        out_cases.append(row)
        if worst is None or sf < worst[0]:
            worst = (sf, key, fields)
    # surface of the worst case, coloured by von Mises stress, for the 3D reference view
    from .strength.meshing import tet_mesh_from_surface
    p, t, _ = tet_mesh_from_surface(project.root / cfg['part_mesh'], cfg.get('transform'), cfg.get('unit_scale', 1.0),
                                    cfg.get('mesh_size_mm', 1.0), project.root / Path(pp['strength']).parent / '.cache')
    fields = worst[2]
    X, vm = np.asarray(fields['X']), np.asarray(fields['vm'])
    # fields are on the solver's nodes (P2 adds mid-edge nodes); take the corner nodes by nearest point
    from scipy.spatial import cKDTree
    _, k = cKDTree(X).query(p)
    v_node = vm[k]
    faces = _boundary_faces(np.asarray(t))
    used = np.unique(faces)
    remap = -np.ones(len(p), dtype=np.int64)
    remap[used] = np.arange(len(used))
    out_dir = project.root / RESULTS_DIR / part
    out_dir.mkdir(parents=True, exist_ok=True)
    verts = (np.asarray(p)[used] / 1000.0).astype(np.float32)      # mm -> m, part frame
    tri = remap[faces].astype(np.uint32)
    vals = v_node[used].astype(np.float32)
    with open(out_dir / 'stress.bin', 'wb') as f:
        f.write(np.array([len(verts), len(tri)], dtype=np.uint32).tobytes())
        f.write(verts.tobytes()); f.write(tri.tobytes()); f.write(vals.tobytes())
    from .strength.materials import get_material
    mat = get_material(material)
    res = {'part': part, 'label': pp.get('label') or part, 'material': material, 'build_dir': build,
           'print_settings': {'layer_height_mm': ps.layer_height_mm, 'walls': ps.walls, 'infill_pct': ps.infill_pct},
           'run_id': loads['run_id'], 'scenario': loads.get('scenario'), 'loads': loads, 'cases': out_cases,
           'min_sf': round(float(worst[0]), 2), 'worst_case': worst[1],
           'stress_view': {'file': 'stress.bin', 'case': worst[1], 'vm_max_mpa': round(float(vals.max()), 2),
                           'material_strength_mpa': mat['Xt']},
           'assumptions': '材料强度是文献典型值；舵机夹持按螺丝孔完全固定；结果只用来比较方案。'}
    (out_dir / 'result.json').write_text(json.dumps(res, ensure_ascii=False, default=float), encoding='utf8')
    if progress:
        progress(1.0)
    return res


def last_results(project: Project) -> dict:
    out = {}
    d = project.root / RESULTS_DIR
    if d.is_dir():
        for f in d.glob('*/result.json'):
            out[f.parent.name] = json.loads(f.read_text(encoding='utf8'))
    return out


# --------------------------------------------------------------------------- gears

# Output-stage gear of common hobby servos. Typical/assumed values: tooth geometry from
# teardown photos of the class, allowable bending stress for the gear material with a
# static safety allowance. Override per project with actuators.<name>.gear.
GEAR_DEFAULTS = {
    'sg90': {'module_mm': 0.5, 'teeth': 22, 'face_width_mm': 4.0, 'material': 'POM（塑料）',
             'allow_mpa': 55.0},
    'mg90s': {'module_mm': 0.5, 'teeth': 22, 'face_width_mm': 4.0, 'material': '铜合金（金属齿轮）',
              'allow_mpa': 150.0},
    'tt_motor': {'module_mm': 0.5, 'teeth': 30, 'face_width_mm': 4.0, 'material': '塑料', 'allow_mpa': 35.0},
}
# Lewis form factor, 20 deg full-depth involute (Shigley, table 14-2)
_LEWIS = [(12, 0.245), (13, 0.261), (14, 0.277), (15, 0.290), (16, 0.296), (17, 0.303), (18, 0.309),
          (19, 0.314), (20, 0.322), (21, 0.328), (22, 0.331), (24, 0.337), (26, 0.346), (28, 0.353),
          (30, 0.359), (34, 0.371), (38, 0.384), (43, 0.397), (50, 0.409), (60, 0.422), (75, 0.435),
          (100, 0.447), (150, 0.460), (300, 0.472), (400, 0.480)]


def lewis_y(teeth):
    n = [a for a, _ in _LEWIS]
    y = [b for _, b in _LEWIS]
    return float(np.interp(teeth, n, y))


def lewis_stress_mpa(torque_nm, module_mm, teeth, face_width_mm):
    r_m = module_mm * teeth / 2 / 1000.0
    ft = abs(torque_nm) / r_m
    return ft / (face_width_mm * module_mm * lewis_y(teeth)), ft


def gear_check(project: Project, run_id=None) -> dict:
    from .electrical.motors import merged
    from .results import RunStore
    store = RunStore(project.root)
    runs = [r for r in store.list() if '-sweep' not in r['run_id']]
    if run_id is None:
        if not runs:
            raise ValueError('还没有仿真结果，先跑一个测试')
        run_id = runs[0]['run_id']
    meta = store.meta(run_id)
    z = np.load(store.dir(run_id) / 'signals.npz')
    rows = []
    for name, act in (project.data.get('actuators') or {}).items():
        preset = act.get('preset')
        g = {**GEAR_DEFAULTS.get(preset, {}), **(act.get('gear') or {})}
        if not g.get('module_mm'):
            continue
        p = merged(act)
        key = f'act.{name}.torque'
        peak = float(np.nanmax(np.abs(z[key]))) if key in z.files else None
        stall = None
        if all(k in p for k in ('r_ohm', 'ke_v_s_rad')):
            v = 5.0
            stall = v / p['r_ohm'] * p['ke_v_s_rad'] * p.get('gear_ratio', 1.0) * p.get('efficiency', 1.0)
        cases = []
        for label, tq in (('仿真里的最大力矩', peak), ('堵转力矩（5 V）', stall)):
            if tq is None:
                continue
            s, ft = lewis_stress_mpa(tq, g['module_mm'], g['teeth'], g['face_width_mm'])
            cases.append({'label': label, 'torque_nm': round(tq, 4), 'tooth_force_n': round(ft, 2),
                          'stress_mpa': round(s, 1), 'sf': round(g['allow_mpa'] / s, 2) if s > 0 else None})
        rows.append({'actuator': name, 'preset': preset, 'gear': g, 'lewis_y': round(lewis_y(g['teeth']), 3),
                     'cases': cases, 'assumed': 'gear' not in act})
    return {'run_id': run_id, 'scenario': meta.get('scenario', {}).get('name'), 'gears': rows,
            'method': 'Lewis 齿根弯曲应力 σ = Ft / (b·m·Y)，Ft = T / r；没算动载和磨损',
            'assumptions': '齿轮模数、齿数、齿宽和许用应力是这类舵机的典型值（估计，没拆实物量过）'}
