"""STL / STEP / OBJ import: normalise units to metres, write an OBJ next to the
project, and compute mass properties so the part can become a MuJoCo body."""
from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np

UNITS = {'m': 1.0, 'mm': 1e-3, 'cm': 1e-2, 'in': 0.0254}


def load_mesh(path, units='mm'):
    import trimesh
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in ('.step', '.stp'):
        try:
            import cascadio
        except ImportError as e:  # pragma: no cover
            raise RuntimeError('STEP import needs the "cascadio" package (pip install cascadio)') from e
        with tempfile.TemporaryDirectory() as tmp:
            glb = Path(tmp) / 'part.glb'
            cascadio.step_to_glb(str(path), str(glb), tol_linear=0.02, tol_angular=0.3)
            mesh = trimesh.load(glb, force='mesh')
        scale = 1.0                 # cascadio already writes metres
    else:
        mesh = trimesh.load(path, force='mesh')
        scale = UNITS[units]
    mesh.merge_vertices()
    if scale != 1.0:
        mesh.apply_scale(scale)
    return mesh


def import_mesh(src, out_dir, name, units='mm', density=1240.0, collision='hull',
                mass_kg=None):
    """Returns the part description stored in the project file. Geometry stays in
    the source frame (no re-centring), so CAD assemblies keep their placement."""
    import trimesh
    mesh = load_mesh(src, units)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    visual = out_dir / f'{name}.obj'
    mesh.export(visual)
    info = {'file': visual.name, 'source': Path(src).name, 'units': units,
            'bounds_m': np.round(mesh.bounds, 6).tolist(), 'faces': int(len(mesh.faces)),
            'watertight': bool(mesh.is_watertight), 'collision': collision}
    if collision == 'hull':
        hull = mesh.convex_hull
        hull.export(out_dir / f'{name}_hull.obj')
        info['collision_file'] = f'{name}_hull.obj'
    solid = mesh if mesh.is_watertight else mesh.convex_hull
    if not mesh.is_watertight:
        info['mass_note'] = 'mesh not closed: mass properties from convex hull (over-estimate)'
    volume = float(abs(solid.volume))
    mass = float(mass_kg) if mass_kg is not None else volume * density
    scale = mass / volume if volume > 0 else 0.0
    info.update({'volume_m3': volume, 'density_kg_m3': density if mass_kg is None else None,
                 'mass_kg': mass,
                 'com_m': np.round(solid.center_mass, 7).tolist(),
                 'inertia_kg_m2': np.round(solid.moment_inertia * scale, 12).tolist()})
    if isinstance(solid, trimesh.Trimesh) and mass <= 0:
        info['mass_kg'] = 1e-4
    return info
