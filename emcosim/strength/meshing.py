"""Tetrahedral meshing for the strength check (heavy deps imported lazily).

STL/OBJ parts: fTetWild via the `pytetwild` wheel. It works from the surface
triangles with an envelope tolerance and a generalized winding number, so it
copes with the sliver triangles that Blender booleans leave (gmsh's STL
re-parametrisation failed on the project's press arm with "overlapping
facets") and with surfaces that are not watertight. If the input is not
watertight, a warning is returned; the inside/outside decision then relies on
the winding number and small holes are closed over. Results are cached on disk.

Parametric corner geometry: gmsh OpenCASCADE (exact fillet arcs + local size field).
"""
from __future__ import annotations

import glob
import hashlib
import importlib.util
import os
from pathlib import Path

import numpy as np

_MEM_CACHE: dict = {}


_DLL_DIRS = {}


def _ftetwild():
    """Return the compiled fTetWild wrapper module of `pytetwild`.

    pytetwild >= 0.3 imports pyvista in its package __init__ although only the
    compiled extension is needed; load the extension directly to avoid that.
    """
    spec = importlib.util.find_spec('pytetwild')
    if spec is None or not spec.submodule_search_locations:
        raise ImportError('pytetwild is not installed: pip install "emcosim[strength]"')
    d = list(spec.submodule_search_locations)[0]
    libs = [f for f in glob.glob(os.path.join(d, 'PyfTetWildWrapper*')) if f.endswith(('.so', '.pyd'))]
    if not libs:
        raise ImportError(f'pytetwild extension not found in {d}')
    # Windows wheels are built with delvewheel: the bundled DLLs sit in ../pytetwild.libs and the
    # package __init__ we skip is what normally adds that folder to the DLL search path.
    dll_dir = os.path.normpath(os.path.join(d, os.pardir, 'pytetwild.libs'))
    if os.name == 'nt' and os.path.isdir(dll_dir) and dll_dir not in _DLL_DIRS:
        _DLL_DIRS[dll_dir] = os.add_dll_directory(dll_dir)
    s2 = importlib.util.spec_from_file_location('PyfTetWildWrapper', libs[0])
    mod = importlib.util.module_from_spec(s2)
    s2.loader.exec_module(mod)
    return mod


def load_surface(path, transform=None, unit_scale=1.0):
    """Read STL/OBJ with trimesh, scale to mm and apply a 4x4 transform (part frame)."""
    import trimesh
    m = trimesh.load(str(path), force='mesh')
    m.merge_vertices()
    if unit_scale != 1.0:
        m.apply_scale(unit_scale)
    if transform is not None:
        m.apply_transform(np.asarray(transform, dtype=float))
    return m


def tet_mesh_from_surface(path, transform=None, unit_scale=1.0, edge_mm=1.0, cache_dir=None,
                          envelope_rel=1e-3):
    """Tetrahedralize a surface mesh. Returns (points (N,3) mm, tets (M,4), info dict)."""
    path = Path(path)
    key_src = f'{path.resolve()}|{path.stat().st_mtime_ns}|{np.asarray(transform).tolist() if transform is not None else None}|{unit_scale}|{edge_mm}|{envelope_rel}|v1'
    key = hashlib.sha1(key_src.encode()).hexdigest()[:16]
    if key in _MEM_CACHE:
        return _MEM_CACHE[key]
    cache_file = Path(cache_dir) / f'tet_{path.stem}_{key}.npz' if cache_dir else None
    surf = load_surface(path, transform, unit_scale)
    info = {'source': str(path), 'surface_faces': int(len(surf.faces)),
            'watertight': bool(surf.is_watertight), 'warnings': []}
    if not surf.is_watertight:
        info['warnings'].append('surface is not watertight: fTetWild closes it via the winding number; '
                                'check the volume below against the CAD volume')
    if cache_file is not None and cache_file.exists():
        z = np.load(cache_file)
        p, t = z['p'], z['t']
        info['cached'] = True
    else:
        W = _ftetwild()
        # args: vertices, faces, optimize, skip_simplify, edge_length_fac, edge_length_abs,
        # epsilon (relative envelope), stop_energy, coarsen, threads, opt iters, loglevel,
        # quiet, vtk_ordering, disable_filtering, background sizing field (none)
        out = W.tetrahedralize_mesh(surf.vertices.astype(np.float64), surf.faces.astype(np.uint32),
                                    True, False, 0.05, float(edge_mm), float(envelope_rel), 10.0, True,
                                    max(1, (os.cpu_count() or 2)), 80, 6, True, False, False,
                                    np.zeros((0, 3)), np.zeros((0, 4), np.int32), np.zeros(0))
        p, t = np.asarray(out[0], dtype=np.float64), np.asarray(out[1], dtype=np.int64)
        info['cached'] = False
        if cache_file is not None:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(cache_file, p=p, t=t)
    p, t = _fix_orientation(p, t)
    info.update(n_points=int(len(p)), n_tets=int(len(t)), volume_mm3=float(_volumes(p, t).sum()),
                surface_volume_mm3=float(surf.volume) if surf.is_watertight else None,
                bounds=[p.min(0).round(3).tolist(), p.max(0).round(3).tolist()])
    res = (p, t, info)
    _MEM_CACHE[key] = res
    return res


def _volumes(p, t):
    a, b, c, d = (p[t[:, i]] for i in range(4))
    return np.einsum('ij,ij->i', np.cross(b - a, c - a), d - a) / 6.0


def _fix_orientation(p, t):
    t = np.array(t, dtype=np.int64)
    v = _volumes(p, t)
    neg = v < 0
    t[neg, 0], t[neg, 1] = t[neg, 1].copy(), t[neg, 0].copy()
    keep = np.abs(v) > 1e-12
    return p, t[keep]


# --------------------------------------------------------------------------- parametric bracket

def bracket_mesh(r_mm, L=20.0, h=4.0, b=4.0, W=6.0, H=16.0, h_global=1.0, h_corner=None):
    """L-bracket in the x-z plane extruded along y (0..b).

    Wall: x in [-W, 0], z in [0, H] (fixed at z = 0). Arm: x in [0, L], z in [H-h, H].
    The inside corner is at (x, z) = (0, H-h), filleted with radius r (0 = sharp).
    Returns (points, tets, corner dict).
    """
    import gmsh
    zc = H - h
    if h_corner is None:
        h_corner = max(min(r_mm / 4, 0.25), 0.04) if r_mm > 0 else 0.1
    own = not gmsh.isInitialized()
    if own:
        gmsh.initialize()
    gmsh.option.setNumber('General.Terminal', 0)
    gmsh.model.add(f'bracket_r{r_mm}')
    occ = gmsh.model.occ
    P = lambda x, z: occ.addPoint(x, 0, z)
    p0, p1 = P(-W, 0), P(0, 0)
    pts_arc = []
    if r_mm > 0:
        a0, cc, a1 = P(0, zc - r_mm), P(r_mm, zc - r_mm), P(r_mm, zc)
        pts_arc = [a0, cc, a1]
    else:
        a0 = a1 = P(0, zc)
    p4, p5, p6 = P(L, zc), P(L, H), P(-W, H)
    lines = [occ.addLine(p0, p1), occ.addLine(p1, a0)]
    corner_curves = []
    if r_mm > 0:
        arc = occ.addCircleArc(a0, pts_arc[1], a1)
        lines.append(arc)
        corner_curves.append(arc)
    lines += [occ.addLine(a1, p4), occ.addLine(p4, p5), occ.addLine(p5, p6), occ.addLine(p6, p0)]
    s = occ.addPlaneSurface([occ.addCurveLoop(lines)])
    occ.extrude([(2, s)], 0, b, 0)
    occ.synchronize()
    # size field: distance to the corner line (the arc surface, or the sharp edge)
    if r_mm > 0:
        targets = [e[1] for e in gmsh.model.getEntities(2)
                   if _bbox_near(gmsh.model.getBoundingBox(2, e[1]), r_mm, zc)]
        fdist = gmsh.model.mesh.field.add('Distance')
        gmsh.model.mesh.field.setNumbers(fdist, 'SurfacesList', targets)
    else:
        targets = [e[1] for e in gmsh.model.getEntities(1)
                   if _edge_is_corner(gmsh.model.getBoundingBox(1, e[1]), zc)]
        fdist = gmsh.model.mesh.field.add('Distance')
        gmsh.model.mesh.field.setNumbers(fdist, 'CurvesList', targets)
        gmsh.model.mesh.field.setNumber(fdist, 'Sampling', 50)
    fth = gmsh.model.mesh.field.add('Threshold')
    gmsh.model.mesh.field.setNumber(fth, 'InField', fdist)
    gmsh.model.mesh.field.setNumber(fth, 'SizeMin', h_corner)
    gmsh.model.mesh.field.setNumber(fth, 'SizeMax', h_global)
    gmsh.model.mesh.field.setNumber(fth, 'DistMin', max(r_mm * 0.5, 2 * h_corner))
    gmsh.model.mesh.field.setNumber(fth, 'DistMax', max(r_mm * 0.5, 2 * h_corner) + 4 * h_global)
    gmsh.model.mesh.field.setAsBackgroundMesh(fth)
    gmsh.option.setNumber('Mesh.MeshSizeExtendFromBoundary', 0)
    gmsh.option.setNumber('Mesh.MeshSizeFromPoints', 0)
    gmsh.option.setNumber('Mesh.MeshSizeFromCurvature', 0)
    gmsh.option.setNumber('Mesh.MeshSizeMax', h_global)
    gmsh.model.mesh.generate(3)
    p, t = _gmsh_tets()
    gmsh.model.remove()
    if own:
        gmsh.finalize()
    p, t = _fix_orientation(p, t)
    corner = {'point_xz': (0.0, zc), 'r_mm': r_mm, 'edge_dir': (0, 1, 0),
              'face_normals': [(0, 0, -1), (1, 0, 0)],  # arm underside, wall face
              'h_corner': h_corner, 'h_global': h_global,
              'center_xz': (r_mm, zc - r_mm) if r_mm > 0 else (0.0, zc)}
    return p, t, corner


def _bbox_near(bb, r, zc):
    x0, y0, z0, x1, y1, z1 = bb
    return x0 >= -1e-6 and x1 <= r + 1e-6 and z0 >= zc - r - 1e-6 and z1 <= zc + 1e-6 and (x1 - x0) > 1e-6 \
        and (z1 - z0) > 1e-6


def _edge_is_corner(bb, zc):
    x0, y0, z0, x1, y1, z1 = bb
    return abs(x0) < 1e-6 and abs(x1) < 1e-6 and abs(z0 - zc) < 1e-6 and abs(z1 - zc) < 1e-6


def _gmsh_tets():
    import gmsh
    tags, coords, _ = gmsh.model.mesh.getNodes()
    p = coords.reshape(-1, 3)
    idx = np.full(int(tags.max()) + 1, -1, dtype=np.int64)
    idx[tags.astype(np.int64)] = np.arange(len(tags))
    et, _, enodes = gmsh.model.mesh.getElements(3)
    k = list(et).index(4)
    t = idx[np.asarray(enodes[k], dtype=np.int64).reshape(-1, 4)]
    used = np.unique(t)
    remap = np.full(len(p), -1, dtype=np.int64)
    remap[used] = np.arange(len(used))
    return p[used], remap[t]


def box_mesh(lx, ly, lz, h=1.0):
    """Box [0,lx]x[0,ly]x[0,lz] tet mesh via gmsh (used by tests)."""
    import gmsh
    own = not gmsh.isInitialized()
    if own:
        gmsh.initialize()
    gmsh.option.setNumber('General.Terminal', 0)
    gmsh.model.add('box')
    gmsh.model.occ.addBox(0, 0, 0, lx, ly, lz)
    gmsh.model.occ.synchronize()
    gmsh.option.setNumber('Mesh.MeshSizeMax', h)
    gmsh.option.setNumber('Mesh.MeshSizeMin', h / 2)
    gmsh.model.mesh.generate(3)
    p, t = _gmsh_tets()
    gmsh.model.remove()
    if own:
        gmsh.finalize()
    return _fix_orientation(p, t)
