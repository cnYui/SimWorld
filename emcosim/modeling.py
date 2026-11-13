"""Mechanical modelling for an AI that has no CAD program: simple shapes as parts, editing and
deleting parts, a readable summary of the compiled model, and pictures of it at any time.

Parts are the same records import_model writes (mechanics.parts in project.yaml, a mesh under
mechanics/meshes), so the 建模 page, printing and the simulation treat a shape like any imported part.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from . import api
from .project import Project

SHAPES = {
    'box': '[x, y, z] edge lengths',
    'cylinder': '[diameter, height], axis along z',
    'sphere': '[diameter]',
    'capsule': '[diameter, height of the straight part], axis along z',
    'tube': '[outer diameter, inner diameter, height], axis along z (collision fills the hole: '
            'MuJoCo collides with the convex hull)',
    'rounded_box': '[x, y, z, corner radius]',
}


def _mesh(shape, size):
    import trimesh
    s = [float(v) for v in size]
    if shape == 'box':
        _need(shape, s, 3)
        return trimesh.creation.box(s)
    if shape == 'cylinder':
        _need(shape, s, 2)
        return trimesh.creation.cylinder(radius=s[0] / 2, height=s[1], sections=64)
    if shape == 'sphere':
        _need(shape, s, 1)
        return trimesh.creation.icosphere(subdivisions=3, radius=s[0] / 2)
    if shape == 'capsule':
        _need(shape, s, 2)
        return trimesh.creation.capsule(height=s[1], radius=s[0] / 2, count=[32, 32])
    if shape == 'tube':
        _need(shape, s, 3)
        if s[1] >= s[0]:
            raise ValueError('tube: inner diameter must be smaller than the outer one')
        return trimesh.creation.annulus(r_min=s[1] / 2, r_max=s[0] / 2, height=s[2], sections=64)
    if shape == 'rounded_box':
        _need(shape, s, 4)
        x, y, z, r = s
        r = min(r, x / 2 - 1e-3, y / 2 - 1e-3)
        prof = []
        for cx, cy, a0 in ((x / 2 - r, y / 2 - r, 0), (-x / 2 + r, y / 2 - r, 90), (-x / 2 + r, -y / 2 + r, 180),
                           (x / 2 - r, -y / 2 + r, 270)):
            for k in range(9):
                a = math.radians(a0 + k * 90 / 8)
                prof.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        # convex outline: a fan of triangles from the centre, extruded (no shapely needed)
        verts = np.array([(0.0, 0.0)] + prof)
        n = len(prof)
        faces = np.array([[0, 1 + k, 1 + (k + 1) % n] for k in range(n)])
        m = trimesh.creation.extrude_triangulation(verts, faces, z)
        m.apply_translation([0, 0, -z / 2])
        m.merge_vertices()
        m.fix_normals()
        return m
    raise ValueError(f'shape {shape!r}: use one of {", ".join(f"{k} {v}" for k, v in SHAPES.items())}')


def _need(shape, s, n):
    if len(s) != n or any(v <= 0 for v in s):
        raise ValueError(f'{shape} size_mm needs {n} positive numbers: {SHAPES[shape]}')


def add_shape(project: Project, name, shape, size_mm, parent='world', pos=None, euler_deg=None, mass_kg=None,
              density=None, rgba=None, collision='hull'):
    m = _mesh(shape, size_mm)
    d = project.root / 'mechanics' / 'shapes'
    d.mkdir(parents=True, exist_ok=True)
    stl = d / f'{name}.stl'
    m.export(stl)
    out = api.import_model(project, stl, name=name, parent=parent, pos=pos, euler_deg=euler_deg, units='mm',
                           density=density, mass_kg=mass_kg, collision=collision, rgba=rgba)
    project.data['mechanics']['parts'][name]['shape'] = {'type': shape, 'size_mm': [float(v) for v in size_mm]}
    project.save()
    return {**out, 'shape': shape, 'size_mm': size_mm}


def _check_compiles(project):
    from .mechanics import Mechanics
    Mechanics(project.root, project.data['mechanics'], project.pcb())


def part_edit(project: Project, name, **changes):
    parts = (project.data.get('mechanics') or {}).get('parts') or {}
    if name not in parts:
        raise ValueError(f'no part {name!r}; parts: {sorted(parts) or "none"}')
    before = dict(parts[name])
    done = {}
    for k in ('pos', 'euler_deg', 'parent', 'mass_kg', 'rgba', 'label'):
        v = changes.get(k)
        if v is None:
            continue
        if k == 'parent' and v not in ('world',) and v not in parts:
            raise ValueError(f'parent {v!r} is not a part; parts: {sorted(parts)}')
        if k == 'parent' and _descends(parts, v, name):
            raise ValueError(f'{v} is inside {name}; a part cannot be its own ancestor')
        done[k] = {'old': before.get(k), 'new': v}
        parts[name][k] = v
        if k == 'mass_kg':
            parts[name].pop('mass_assumed', None)
            parts[name]['density_kg_m3'] = None
            if parts[name].get('inertia_kg_m2') is not None and before.get('mass_kg'):
                scale = float(v) / float(before['mass_kg'])
                parts[name]['inertia_kg_m2'] = (np.asarray(parts[name]['inertia_kg_m2']) * scale).tolist()
    if not done:
        raise ValueError('nothing to change: give pos, euler_deg, parent, mass_kg, rgba or label')
    try:
        _check_compiles(project)
    except Exception as e:
        parts[name] = before
        raise ValueError(f'MuJoCo rejected the change, nothing saved: {e}') from e
    project.save()
    return {'part': name, 'changed': done, 'units': {'pos': 'm', 'euler_deg': 'deg', 'mass_kg': 'kg'}}


def _descends(parts, node, ancestor):
    seen = set()
    while node and node != 'world' and node not in seen:
        if node == ancestor:
            return True
        seen.add(node)
        node = (parts.get(node) or {}).get('parent')
    return False


def part_delete(project: Project, name, cascade=False):
    mech = project.data.get('mechanics') or {}
    parts = mech.get('parts') or {}
    if name not in parts:
        raise ValueError(f'no part {name!r}; parts: {sorted(parts) or "none"}')
    kids = [k for k, v in parts.items() if v.get('parent') == name]
    if kids and not cascade:
        raise ValueError(f'{name} carries {kids}; delete those first or pass cascade=true')
    gone = []

    def drop(n):
        for k in [k for k, v in parts.items() if v.get('parent') == n]:
            drop(k)
        parts.pop(n, None)
        gone.append(n)
    drop(name)
    joints = mech.get('joints') or {}
    lost_j = [j for j, v in joints.items() if v.get('body') in gone]
    for j in lost_j:
        joints.pop(j)
    probes = mech.get('probes') or {}
    lost_p = [p for p, v in probes.items() if any(str(v.get(k, '')).startswith(tuple(gone)) for k in
                                                  ('geoms_a', 'geoms_b', 'body'))]
    for p in lost_p:
        probes.pop(p)
    acts = project.data.get('actuators') or {}
    lost_a = [a for a, v in acts.items() if v.get('joint') in lost_j]
    for a in lost_a:
        acts.pop(a)
    project.save()
    return {'deleted_parts': gone, 'deleted_joints': lost_j, 'deleted_probes': lost_p, 'deleted_actuators': lost_a,
            'note': 'mesh files are kept under mechanics/meshes'}


def model_summary(project: Project, tag=None):
    """Bodies, geoms, joints and sites of the compiled model (any project: MJCF, parts or both), in
    world coordinates at the initial pose, plus the part records with their mass source."""
    import mujoco
    from .mechanics import Mechanics
    mech = Mechanics(project.root, project.data.get('mechanics') or {}, project.pcb())
    m, d = mech.model, mech.data
    out = {}

    def want(t):
        return tag in (None, t)
    if want('body'):
        rows = []
        for b in range(1, m.nbody):
            geoms = [g for g in range(m.ngeom) if m.geom_bodyid[g] == b]
            lo, hi = _world_bounds(m, d, geoms)
            rows.append({'name': m.body(b).name, 'parent': m.body(int(m.body_parentid[b])).name or 'world',
                         'pos_world_m': np.round(d.xpos[b], 5).tolist(), 'mass_kg': round(float(m.body_mass[b]), 6),
                         'bounds_world_m': [np.round(lo, 5).tolist(), np.round(hi, 5).tolist()] if lo is not None else None,
                         'free': any(m.jnt_bodyid[j] == b for j in range(m.njnt))})
        out['body'] = rows
    if want('joint'):
        rows = []
        for j in range(m.njnt):
            jt = int(m.jnt_type[j])
            unit = {int(mujoco.mjtJoint.mjJNT_HINGE): 'deg', int(mujoco.mjtJoint.mjJNT_SLIDE): 'mm'}.get(jt)
            k = 180 / math.pi if unit == 'deg' else 1000.0 if unit == 'mm' else 1.0
            rng = (np.asarray(m.jnt_range[j]) * k).round(4).tolist() if m.jnt_limited[j] else None
            rows.append({'name': m.joint(j).name, 'type': ['free', 'ball', 'slide', 'hinge'][jt],
                         'body': m.body(int(m.jnt_bodyid[j])).name, 'axis': np.round(m.jnt_axis[j], 4).tolist(),
                         'range': rng, 'unit': unit})
        out['joint'] = rows
    if want('geom'):
        out['geom'] = [{'name': m.geom(g).name, 'body': m.body(int(m.geom_bodyid[g])).name,
                        'type': mujoco.mjtGeom(int(m.geom_type[g])).name.replace('mjGEOM_', '').lower(),
                        'collides': bool(m.geom_contype[g] or m.geom_conaffinity[g]),
                        'friction': np.round(m.geom_friction[g], 4).tolist()}
                       for g in range(m.ngeom) if m.geom(g).name]
    if want('site'):
        out['site'] = [{'name': m.site(s).name, 'body': m.body(int(m.site_bodyid[s])).name,
                        'pos_world_m': np.round(d.site_xpos[s], 5).tolist()} for s in range(m.nsite)]
    parts = (project.data.get('mechanics') or {}).get('parts') or {}
    if parts and want('part'):
        out['part'] = [{'name': n, 'parent': v.get('parent', 'world'), 'pos_m': v.get('pos'),
                        'euler_deg': v.get('euler_deg'), 'mass_kg': v.get('mass_kg'),
                        'mass_source': 'assumed (default density)' if v.get('mass_assumed') else
                        ('given' if v.get('density_kg_m3') is None else f'density {v.get("density_kg_m3")} kg/m3'),
                        'shape': v.get('shape'), 'source': v.get('source'), 'bounds_m': v.get('bounds_m')}
                       for n, v in parts.items()]
    out['units'] = 'metres, kg, degrees for hinge joints, mm for slide joints'
    out['model'] = 'MJCF + parts' if (project.data.get('mechanics') or {}).get('mjcf') else 'parts'
    return out


def _world_bounds(m, d, geoms):
    pts = []
    for g in geoms:
        if m.geom_rgba[g][3] == 0 and m.geom_group[g] == 3:
            continue
        if int(m.geom_type[g]) == 0:            # plane: unbounded
            continue
        aabb = np.asarray(m.geom_aabb[g]) if hasattr(m, 'geom_aabb') else None
        if aabb is None or not np.any(aabb[3:]):
            r = float(m.geom_rbound[g])
            c = d.geom_xpos[g]
            pts += [c - r, c + r]
            continue
        c, h = aabb[:3], aabb[3:]
        R = np.asarray(d.geom_xmat[g]).reshape(3, 3)
        for sx in (-1, 1):
            for sy in (-1, 1):
                for sz in (-1, 1):
                    pts.append(d.geom_xpos[g] + R @ (c + h * np.array([sx, sy, sz])))
    if not pts:
        return None, None
    a = np.asarray(pts)
    return a.min(axis=0), a.max(axis=0)


def render(project: Project, out_png, width=900, height=600, target=None, azimuth=None, elevation=None,
           distance=None, qpos=None, highlight=None, floor=True):
    """Image of the model (initial pose, or qpos). target: a body name to look at (default: whole
    model). highlight: body names drawn in orange. Returns the camera used, for follow-up shots."""
    import mujoco
    from PIL import Image as PILImage

    from .mechanics import build_spec
    mech_cfg = project.data.get('mechanics') or {}
    spec = build_spec(project.root, mech_cfg, project.pcb())
    m0 = spec.compile()
    d0 = mujoco.MjData(m0)
    if qpos is not None:
        d0.qpos[:] = qpos
    mujoco.mj_forward(m0, d0)
    lo, hi = _world_bounds(m0, d0, range(m0.ngeom))
    if lo is None:
        raise ValueError('the model has nothing to draw yet: add parts with add_shape or import_model')
    if floor:
        spec.worldbody.add_geom(type=mujoco.mjtGeom.mjGEOM_PLANE, size=[0, 0, 0.05],
                                pos=[(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2] - 0.002],
                                rgba=[0.32, 0.34, 0.38, 1], contype=0, conaffinity=0, group=2)
        spec.worldbody.add_light(pos=[(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, hi[2] + 1.5], dir=[0, 0, -1],
                                 diffuse=[0.6, 0.6, 0.6])
    spec.visual.global_.offwidth = max(int(width), 640)
    spec.visual.global_.offheight = max(int(height), 480)
    m = spec.compile()
    d = mujoco.MjData(m)
    if qpos is not None:
        d.qpos[:] = qpos
    mujoco.mj_forward(m, d)
    for b in highlight or []:
        bid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, b)
        if bid < 0:
            raise ValueError(f'no body {b!r} to highlight')
        for g in range(m.ngeom):
            if m.geom_bodyid[g] == bid and m.geom_rgba[g][3] > 0:
                m.geom_rgba[g] = [1.0, 0.55, 0.1, 1]
    cam = mujoco.MjvCamera()
    mujoco.mjv_defaultFreeCamera(m, cam)
    if target:
        bid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, target)
        if bid < 0:
            names = [m.body(i).name for i in range(1, m.nbody)]
            raise ValueError(f'no body {target!r}; bodies: {names}')
        geoms = [g for g in range(m.ngeom) if m.geom_bodyid[g] == bid]
        tlo, thi = _world_bounds(m, d, geoms)
        center = (tlo + thi) / 2 if tlo is not None else d.xpos[bid]
        span = float(np.max(thi - tlo)) if tlo is not None else 0.05
    else:
        center, span = (lo + hi) / 2, float(np.max(hi - lo))
    cam.lookat[:] = center
    cam.distance = distance if distance is not None else max(span * 2.2, 0.02)
    cam.azimuth = azimuth if azimuth is not None else 135
    cam.elevation = elevation if elevation is not None else -25
    r = mujoco.Renderer(m, height=int(height), width=int(width))
    r.update_scene(d, camera=cam)
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    PILImage.fromarray(r.render()).save(out_png)
    r.close()
    return {'png': str(out_png), 'camera': {'lookat_m': np.round(cam.lookat, 4).tolist(),
                                            'distance_m': round(float(cam.distance), 4),
                                            'azimuth': cam.azimuth, 'elevation': cam.elevation},
            'model_bounds_m': [np.round(lo, 4).tolist(), np.round(hi, 4).tolist()]}


__all__ = ['add_shape', 'part_edit', 'part_delete', 'model_summary', 'render', 'SHAPES']
