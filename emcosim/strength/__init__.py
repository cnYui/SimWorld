"""Strength check for 3D-printed (FDM) parts: linear-elastic static FEA with a
transversely isotropic material, perimeter-shell / infill-core split, per-mode
failure check against layer orientation, fillet study and loads from runs.

Optional dependencies (pip install "emcosim[strength]"): scikit-fem, gmsh,
pytetwild (fTetWild), matplotlib for plots. They are imported lazily, so
`import emcosim.strength` works without them; `available()` reports what is missing.

Public API
    check_part(project, part_mesh, material, build_dir, fixtures, loads, print_settings=..., ...)
    fillet_study(material, build_dir, radii=None, print_settings=..., ...)
    loads_from_run(project, run_id=None, scenario='stall_brownout')
    PrintSettings, MATERIALS, get_material, corner_layer_flags, available
"""
from __future__ import annotations

import importlib.util

from .materials import MATERIALS, SOLID, PrintSettings, get_material, material_frame, stiffness_tensor
from .study import check_part, corner_layer_flags, fillet_study, loads_from_run, to_json

OPTIONAL = {'skfem': 'scikit-fem', 'gmsh': 'gmsh', 'pytetwild': 'pytetwild', 'trimesh': 'trimesh'}


def available() -> tuple[bool, list[str]]:
    """(all FEA deps importable, list of missing pip package names)."""
    missing = [pkg for mod, pkg in OPTIONAL.items() if importlib.util.find_spec(mod) is None]
    return (not missing, missing)


__all__ = ['check_part', 'fillet_study', 'loads_from_run', 'PrintSettings', 'MATERIALS', 'SOLID',
           'get_material', 'material_frame', 'stiffness_tensor', 'corner_layer_flags', 'available',
           'to_json']
