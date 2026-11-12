"""High-level strength studies: check a printed part, fillet (inside corner) study,
and loads taken from an emcosim run."""
from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

import numpy as np

from .materials import PrintSettings, as_direction, get_material


def corner_layer_flags(corner: dict, build_dir, print_settings: PrintSettings | None = None) -> dict:
    """Classify an inside corner against the layer planes.

    corner: {'edge_dir': [..], 'face_normals': [[..], [..]], 'r_mm': r (optional)}
    on_layer_boundary: one of the two faces is a layer plane (normal ~ build dir), so
    the corner root sits exactly where a new layer starts (a weld line at the stress peak).
    """
    ps = print_settings or PrintSettings()
    b = as_direction(build_dir)
    normals = [as_direction(n) for n in corner.get('face_normals', [])]
    on_boundary = any(abs(float(n @ b)) > 0.95 for n in normals)
    edge = as_direction(corner.get('edge_dir', (0, 0, 1)))
    edge_par = abs(float(edge @ b)) > 0.95
    r = corner.get('r_mm')
    out = {'on_layer_boundary': bool(on_boundary), 'edge_parallel_to_build': bool(edge_par)}
    if edge_par:
        out['radius_formed_by'] = 'toolpath in the layer plane'
        out['min_reliable_radius_mm'] = ps.min_inside_radius_mm
        if r is not None:
            out['radius_below_nozzle_limit'] = bool(r < ps.min_inside_radius_mm - 1e-9)
    else:
        out['radius_formed_by'] = 'layer staircase'
        out['min_reliable_radius_mm'] = 2 * ps.layer_height_mm
        if r is not None:
            out['layers_across_fillet'] = float(r / ps.layer_height_mm)
            out['radius_below_nozzle_limit'] = bool(r < 2 * ps.layer_height_mm - 1e-9)
    return out


def check_part(project, part_mesh, material, build_dir, fixtures, loads, print_settings=None,
               transform=None, unit_scale=1.0, mesh_size_mm=1.0, exclude_mm=1.0, singular_zones=(),
               probes=None, corners=None, cache_dir=None, keep_fields=False) -> dict:
    """Linear static FEA of a printed part with FDM anisotropy.

    project      project root (relative part_mesh / cache_dir resolve against it) or None
    part_mesh    STL/OBJ path; transform (4x4, applied after unit_scale) maps it to the part frame
    material     name in MATERIALS ('PLA', 'PETG', 'ABS', 'ASA', 'PA-CF') or a dict
    build_dir    build (layer stacking) direction in the part frame: 'z', '-x', [0, 1, 0], ...
    fixtures     [{'name', 'select': selector, 'dofs': 'xyz'}]   (see fea.make_selector)
    loads        [{'name', 'select': selector, 'force_n': [fx, fy, fz]}] or
                 [{'name', 'select', 'torque_nm': T, 'axis': 'x', 'center': [..]}]
    exclude_mm   nodes closer than this to a fixture/load patch are left out of the reported
                 minimum (idealised boundary conditions give local artefacts there)
    singular_zones  [{'name', 'p0', 'p1', 'r_mm'}] sharp re-entrant edges; reported separately
    corners      [{'name', 'edge_dir', 'face_normals'}] inside corners to flag against layers
    Returns a JSON-serialisable dict (plus '_fields' with numpy arrays if keep_fields).
    """
    from .fea import solve
    from .meshing import tet_mesh_from_surface
    root = Path(project) if project else Path('.')
    path = Path(part_mesh)
    if not path.is_absolute():
        path = root / path
    cdir = None
    if cache_dir:
        cdir = Path(cache_dir) if Path(cache_dir).is_absolute() else root / cache_dir
    mat = get_material(material)
    ps = print_settings or PrintSettings()
    t0 = time.time()
    p, t, minfo = tet_mesh_from_surface(path, transform, unit_scale, mesh_size_mm, cdir)
    t_mesh = time.time() - t0
    res = solve(p, t, mat, build_dir, fixtures, loads, ps, exclude_mm=exclude_mm,
                singular_zones=singular_zones, probes=probes, keep_fields=keep_fields)
    res['part'] = {'mesh': str(part_mesh), **minfo}
    res['timing_s']['mesh'] = round(t_mesh, 2)
    if corners:
        res['corners'] = {c.get('name', f'corner{i}'): corner_layer_flags(c, build_dir, ps)
                          for i, c in enumerate(corners)}
    return res


# --------------------------------------------------------------------------- fillet study

def fillet_study(material, build_dir, radii=None, print_settings=None, load_n=10.0, geometry=None,
                 convergence=True, convergence_radii=(0.0, 1.0), keep_fields=False) -> dict:
    """Stress concentration at a printed inside corner vs fillet radius.

    Built-in L-bracket (gmsh OCC), frame: wall x in [-W, 0], z in [0, H], fixed at z = 0;
    arm x in [0, L], z in [H-h, H], extruded y in [0, b]; tip load +z on the face x = L
    (puts the inside corner at (0, H-h) in tension). Corner edge is along y; the two
    corner faces are the arm underside (normal -z) and the wall face (normal +x).

    Kt = max principal stress near the corner / (6 P L / (b h^2)). Solid section is
    used for the stiffness (Kt is geometric); interlayer strength and layer-notch
    factors still come from print_settings. Default radii: nozzle/2, 0.4, 1, 2, 3 mm.
    The sharp corner (r = 0) is only used in the convergence check, to show that its
    peak keeps growing with mesh refinement (a singularity, not a stress).
    """
    from .fea import solve
    from .meshing import bracket_mesh
    mat = get_material(material)
    ps = print_settings or PrintSettings()
    ps_solid = dataclasses.replace(ps, walls=999, top_bottom_layers=99999, infill_pct=100.0)
    g = {'L': 20.0, 'h': 4.0, 'b': 4.0, 'W': 6.0, 'H': 16.0, 'h_global': 1.0}
    g.update(geometry or {})
    if radii is None:
        radii = sorted({round(ps.min_inside_radius_mm, 3), 0.4, 1.0, 2.0, 3.0})
    sig_nom = 6 * load_n * g['L'] / (g['b'] * g['h'] ** 2)
    bd = as_direction(build_dir)
    corner_geo = {'edge_dir': (0, 1, 0), 'face_normals': [(0, 0, -1), (1, 0, 0)]}

    def run(r, h_corner=None):
        p, t, corner = bracket_mesh(r, L=g['L'], h=g['h'], b=g['b'], W=g['W'], H=g['H'],
                                    h_global=g['h_global'], h_corner=h_corner)
        fixtures = [{'name': 'wall_base', 'select': {'plane': {'point': [0, 0, 0], 'normal': 'z'}}}]
        loads = [{'name': 'tip', 'select': {'plane': {'point': [g['L'], 0, 0], 'normal': 'x'}},
                  'force_n': [0, 0, load_n]}]
        res = solve(p, t, mat, bd, fixtures, loads, ps_solid, exclude_mm=1.0, keep_fields=True)
        F = res['_fields']
        X = F['X']
        cx, cz = corner['center_xz']
        zc = g['H'] - g['h']
        dist = np.hypot(X[:, 0] - cx, X[:, 2] - cz)
        zone = (dist <= r + 1.0) & (X[:, 0] >= -1.0) & (X[:, 2] <= zc + 1.0) & (X[:, 2] >= zc - r - 1.0)
        sig = F['sig'][zone]
        w, v = np.linalg.eigh(sig)
        s1 = w[:, -1]
        k = int(np.argmax(s1))
        d1 = v[k, :, -1]
        modes = {}
        for name, arr in F['sf'].items():
            a = arr[zone]
            kk = int(np.argmin(a))
            modes[name] = {'min_sf': float(a[kk]), 'at_mm': X[zone][kk].round(3).tolist()}
        flags = corner_layer_flags({**corner_geo, 'r_mm': r}, bd, ps)
        across = abs(float(d1 @ bd))
        flags['peak_stress_across_layers'] = bool(across > 0.7)
        flags['worst_combination'] = bool(flags['on_layer_boundary'] and across > 0.7)
        out = {'r_mm': r, 'h_corner_mm': corner['h_corner'], 'n_tets': res['mesh']['n_tets'],
               'sigma_nom_mpa': sig_nom, 'sigma1_peak_mpa': float(s1[k]), 'Kt': float(s1[k] / sig_nom),
               'peak_at_mm': X[zone][k].round(3).tolist(), 'peak_dir': d1.round(3).tolist(),
               'cos_peak_dir_build': across, 'modes': modes, 'flags': flags,
               'tip_deflection_mm': res['max_deflection_mm'], 'time_s': res['timing_s']['total']}
        if keep_fields:
            out['_fields'] = F
        return out

    t0 = time.time()
    rows = [run(r) for r in radii]
    conv = []
    if convergence:
        for r in convergence_radii:
            levels = [0.4, 0.2, 0.1, 0.05] if r == 0 else [r / 2, r / 4, r / 8]
            for hc in levels:
                o = run(r, max(hc, 0.03))
                conv.append({'r_mm': r, 'h_corner_mm': o['h_corner_mm'], 'n_tets': o['n_tets'],
                             'Kt': o['Kt']})
    return {'material': mat['name'], 'build_dir': bd.round(4).tolist(), 'geometry_mm': g, 'load_n': load_n,
            'print_settings': ps.describe(mat), 'sigma_nom_mpa': sig_nom, 'rows': rows,
            'convergence': conv, 'time_s': round(time.time() - t0, 1)}


# --------------------------------------------------------------------------- loads from a run

def loads_from_run(project, run_id=None, scenario='stall_brownout', probe='press', actuator='servo1',
                   supply_v=5.0, pick='newest') -> dict:
    """Worst-case loads of one emcosim run (runs/<id>/run.json + signals.npz).

    run_id None -> a run of `scenario`: pick='newest', or pick='worst' (highest contact
    force peak among the stored runs of that scenario). Returns the contact force peak of
    probe.<probe>.force_n, the peak |act.<actuator>.torque| seen in the run, and
    the actuator's stall torque at supply_v from its electrical preset
    (V/R * ke * gear ratio * efficiency), which bounds what the servo can push.
    """
    from ..results import RunStore
    store = RunStore(project)
    if run_id is None:
        runs = [r for r in store.list() if scenario is None or r['scenario'] == scenario]
        if not runs:
            raise FileNotFoundError(f'no runs of scenario {scenario!r} under {store.root}; '
                                    f'run `emcosim run {scenario}` first')
        if pick == 'worst':
            def peak(rid):
                z = np.load(store.dir(rid) / 'signals.npz')
                k = f'probe.{probe}.force_n'
                return float(np.nanmax(z[k])) if k in z.files else -1.0
            run_id = max((r['run_id'] for r in runs), key=peak)
        else:
            run_id = runs[0]['run_id']
    meta = store.meta(run_id)
    z = np.load(store.dir(run_id) / 'signals.npz')
    t = z['t']
    out = {'run_id': run_id, 'scenario': meta.get('scenario', {}).get('name'),
           'overrides': meta.get('overrides')}
    fkey = f'probe.{probe}.force_n'
    if fkey in z.files:
        f = z[fkey]
        k = int(np.nanargmax(f))
        out.update(press_force_peak_n=float(f[k]), press_force_peak_t_s=float(t[k]),
                   press_force_p99_n=float(np.nanpercentile(f, 99)))
    tkey = f'act.{actuator}.torque'
    if tkey in z.files:
        tq = np.abs(z[tkey])
        k = int(np.nanargmax(tq))
        out.update(servo_torque_peak_nm=float(tq[k]), servo_torque_peak_t_s=float(t[k]))
    act = (meta.get('resolved_project', {}).get('actuators') or {}).get(actuator)
    if act:
        from ..electrical.motors import merged
        p = merged(act)
        if all(k in p for k in ('r_ohm', 'ke_v_s_rad')):
            stall = supply_v / p['r_ohm'] * p['ke_v_s_rad'] * p.get('gear_ratio', 1.0) * p.get('efficiency', 1.0)
            out.update(servo_preset=act.get('preset'), servo_stall_torque_nm=float(stall),
                       stall_supply_v=supply_v)
    return out


def to_json(obj, path=None):
    """Drop private numpy fields and dump."""
    def clean(o):
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items() if not str(k).startswith('_')}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, (np.floating, np.integer)):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
        return o
    s = json.dumps(clean(obj), indent=1, ensure_ascii=False)
    if path:
        Path(path).write_text(s, encoding='utf8')
    return s
