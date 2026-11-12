"""FDM material table, print settings and the transversely isotropic stiffness.

Units throughout the strength package: mm, N, MPa (N/mm^2), N*mm.

Every number in MATERIALS is an ASSUMPTION: a typical mid-range value for
solid (100 % infill) FDM specimens printed at 0.2 mm layers with a 0.4 mm
nozzle, collected from manufacturer data sheets and the literature cited in
`source`. None of it was measured for this project. Real values scatter by
+-30 % or more with printer, filament brand, colour, moisture and settings.

Material frame: axes 1, 2 lie in the layer plane, axis 3 is the build (Z)
direction. In-plane raster anisotropy (0/90/+-45 deg) is ignored: the layer
plane is treated as isotropic, which is reasonable for perimeters + crossed
infill but not for fibre-filled filaments (PA-CF), see its note.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

import numpy as np

_COMMON_SOURCES = (
    'Assumed typical values. Sources consulted for the ranges: Prusament PLA / PETG and Bambu Lab '
    'PLA Basic / PETG HF / PA6-CF technical data sheets (XY vs Z tensile bars); Ahn et al., Rapid '
    'Prototyping J. 8 (2002) 248 (ABS anisotropy); Gordelier et al., Rapid Prototyping J. 25 (2019) '
    '953 (PLA review); Kiendl & Gao, Composites B 180 (2020) 107562 (transversely isotropic PLA '
    'model); Aliheidari et al., Polymer Testing 60 (2017) 94 (interlayer fracture vs temperature).')

MATERIALS: dict[str, dict] = {
    'PLA': {
        'E_xy': 3300.0, 'E_z': 2900.0, 'nu_xy': 0.35, 'nu_xz': 0.35, 'G_xz': 1100.0,
        'Xt': 50.0, 'Xc': 65.0, 'Zt': 25.0, 'S_il': 20.0,
        'T_nozzle_ref_c': 210.0, 'T_soften_c': 55.0,
        'note': ('assumed; interlayer tensile Zt = 50 % of in-plane. PLA creeps noticeably under '
                 'sustained load even at room temperature and softens near its glass transition '
                 '(~55-60 C): a part in a hot car or near a heater can sag at a fraction of the '
                 'stresses below. Use SF >= 3-4 for sustained/repeated loads.'),
        'source': _COMMON_SOURCES,
    },
    'PETG': {
        'E_xy': 2000.0, 'E_z': 1800.0, 'nu_xy': 0.38, 'nu_xz': 0.38, 'G_xz': 700.0,
        'Xt': 45.0, 'Xc': 55.0, 'Zt': 25.0, 'S_il': 18.0,
        'T_nozzle_ref_c': 240.0, 'T_soften_c': 75.0,
        'note': ('assumed; interlayer tensile ~55 % of in-plane (PETG bonds layers well). More '
                 'ductile and less creep-prone than PLA at room temperature; softens ~75-80 C.'),
        'source': _COMMON_SOURCES,
    },
    'ABS': {
        'E_xy': 2100.0, 'E_z': 1800.0, 'nu_xy': 0.35, 'nu_xz': 0.35, 'G_xz': 700.0,
        'Xt': 32.0, 'Xc': 45.0, 'Zt': 14.0, 'S_il': 12.0,
        'T_nozzle_ref_c': 245.0, 'T_soften_c': 95.0,
        'note': 'assumed; interlayer ~45 % of in-plane, very sensitive to enclosure temperature/drafts.',
        'source': _COMMON_SOURCES,
    },
    'ASA': {
        'E_xy': 2000.0, 'E_z': 1750.0, 'nu_xy': 0.35, 'nu_xz': 0.35, 'G_xz': 680.0,
        'Xt': 33.0, 'Xc': 45.0, 'Zt': 15.0, 'S_il': 12.0,
        'T_nozzle_ref_c': 250.0, 'T_soften_c': 95.0,
        'note': 'assumed; similar to ABS, interlayer ~45 % of in-plane.',
        'source': _COMMON_SOURCES,
    },
    'PA-CF': {
        'E_xy': 6000.0, 'E_z': 2500.0, 'nu_xy': 0.35, 'nu_xz': 0.35, 'G_xz': 900.0,
        'Xt': 75.0, 'Xc': 70.0, 'Zt': 25.0, 'S_il': 22.0,
        'T_nozzle_ref_c': 280.0, 'T_soften_c': 150.0,
        'note': ('assumed, dry state; interlayer ~33 % of in-plane. Fibres align with the raster, so '
                 'the in-plane values are really direction dependent (treated isotropic here). '
                 'Moisture uptake can cut stiffness/strength by 30-50 %.'),
        'source': _COMMON_SOURCES,
    },
}


def get_material(material) -> dict:
    """Name (case-insensitive) or dict with the same keys -> material dict (copy)."""
    if isinstance(material, dict):
        m = dict(material)
        m.setdefault('name', 'custom')
        return m
    key = str(material).upper().replace('_', '-')
    if key not in MATERIALS:
        raise KeyError(f'unknown material {material!r}; known: {sorted(MATERIALS)}')
    return {'name': key, **MATERIALS[key]}


# --------------------------------------------------------------------------- print settings

# Gibson-Ashby style exponents (E ~ rho^n, strength ~ rho^m) for the infill core.
# ASSUMPTION: grid/lines/triangles/cubic are treated as stretch-dominated (n = m = 1,
# a rule of mixtures on the solid fraction), gyroid/honeycomb as partly bending-
# dominated. Literature fits for printed lattices give n ~ 1..2.
INFILL_EXPONENTS = {
    'grid': (1.0, 1.0), 'rectilinear': (1.0, 1.0), 'lines': (1.1, 1.1), 'triangles': (1.0, 1.0),
    'cubic': (1.1, 1.1), 'gyroid': (1.6, 1.5), 'honeycomb': (1.3, 1.3), 'concentric': (1.0, 1.0),
}


@dataclass
class PrintSettings:
    """FDM slicer settings that enter the strength check.

    Reference state of the MATERIALS table: 0.2 mm layer, 0.4 mm nozzle, solid.
    All relations below are simple documented assumptions, not fitted to tests.
    """
    layer_height_mm: float = 0.2
    nozzle_mm: float = 0.4
    line_width_mm: float | None = None      # None -> 1.125 x nozzle (common slicer default)
    walls: int = 3                           # perimeters
    top_bottom_layers: int = 4
    infill_pct: float = 20.0
    infill_pattern: str = 'grid'
    nozzle_temp_c: float | None = None       # None -> material reference temperature

    # assumption knobs (documented in describe())
    ref_layer_height_mm: float = 0.2
    ref_ratio: float = 0.5                   # layer height / nozzle of the table data
    interlayer_slope: float = 0.6            # strength change per unit (ratio - ref_ratio)
    temp_slope_per_c: float = 0.005          # +0.5 %/C nozzle temperature for interlayer strength
    notch_depth_frac: float = 0.12           # layer valley depth / layer height
    notch_root_radius_mm: float = 0.05       # valley root radius (fixed)
    notch_sensitivity: float = 0.3           # static notch sensitivity q

    @property
    def line_width(self) -> float:
        return float(self.line_width_mm or 1.125 * self.nozzle_mm)

    @property
    def wall_thickness_mm(self) -> float:
        return self.walls * self.line_width

    @property
    def top_bottom_thickness_mm(self) -> float:
        return self.top_bottom_layers * self.layer_height_mm

    @property
    def min_inside_radius_mm(self) -> float:
        """Smallest inside-corner radius that is reproduced on purpose (~ nozzle / 2)."""
        return self.nozzle_mm / 2

    @property
    def ratio(self) -> float:
        return self.layer_height_mm / self.nozzle_mm

    @property
    def printable(self) -> bool:
        """Layer height above ~80 % of the nozzle diameter does not bond reliably."""
        return self.ratio <= 0.8

    def interlayer_factor(self, mat: dict) -> float:
        """Multiplier on Zt and S_il relative to the table (0.2 mm / 0.4 mm / ref temp).

        ASSUMPTION: thinner layers relative to the nozzle are squished more and get a
        wider weld, so k_geom = 1 + slope*(ref_ratio - h/d), clipped to [0.5, 1.15];
        hotter nozzle improves the weld, k_T = 1 + 0.005/C*(T - T_ref) clipped to
        [0.75, 1.10]. Trends after e.g. Chacon et al. Mater. Des. 124 (2017) 143 and
        Aliheidari et al. 2017; magnitudes are guesses.
        """
        k_geom = min(1.15, max(0.5, 1.0 + self.interlayer_slope * (self.ref_ratio - self.ratio)))
        k_t = 1.0
        if self.nozzle_temp_c is not None:
            k_t = min(1.10, max(0.75, 1.0 + self.temp_slope_per_c *
                                (self.nozzle_temp_c - mat.get('T_nozzle_ref_c', self.nozzle_temp_c))))
        return k_geom * k_t

    def _kf(self, h):
        kt = 1.0 + 2.0 * math.sqrt(self.notch_depth_frac * h / self.notch_root_radius_mm)
        return 1.0 + self.notch_sensitivity * (kt - 1.0)

    @property
    def layer_notch_factor(self) -> float:
        """Surface stress concentration of the layer valleys, relative to the reference
        layer height (the table already contains the 0.2 mm notch). Applies to stress
        normal to the layers on side walls. Peterson shallow notch Kt = 1 + 2 sqrt(d/rho)
        with d = 0.12 h, rho = 0.05 mm, and static notch sensitivity q = 0.3 (ASSUMPTION).
        """
        return self._kf(self.layer_height_mm) / self._kf(self.ref_layer_height_mm)

    def infill_factors(self) -> tuple[float, float]:
        """(stiffness factor, strength factor) of the infill core vs. solid."""
        rho = max(0.0, min(1.0, self.infill_pct / 100.0))
        if rho >= 0.99:
            return 1.0, 1.0
        n, m = INFILL_EXPONENTS.get(self.infill_pattern.lower(), (1.2, 1.2))
        return max(rho ** n, 1e-3), max(rho ** m, 1e-3)

    def describe(self, mat: dict | None = None) -> dict:
        d = asdict(self)
        d.update(line_width=self.line_width, wall_thickness_mm=self.wall_thickness_mm,
                 top_bottom_thickness_mm=self.top_bottom_thickness_mm,
                 min_inside_radius_mm=self.min_inside_radius_mm, printable=self.printable,
                 layer_notch_factor=self.layer_notch_factor,
                 infill_stiffness_factor=self.infill_factors()[0],
                 infill_strength_factor=self.infill_factors()[1])
        if mat is not None:
            d['interlayer_factor'] = self.interlayer_factor(mat)
        return d


SOLID = PrintSettings(walls=99, top_bottom_layers=999, infill_pct=100.0)


# --------------------------------------------------------------------------- stiffness

_AXES = {'x': (1, 0, 0), 'y': (0, 1, 0), 'z': (0, 0, 1)}


def as_direction(d) -> np.ndarray:
    """'x', '-z', 'Y' or a 3-vector -> unit vector."""
    if isinstance(d, str):
        s = d.strip().lower()
        sign = -1.0 if s.startswith('-') else 1.0
        v = sign * np.array(_AXES[s.lstrip('+-')], dtype=float)
    else:
        v = np.asarray(d, dtype=float)
    n = np.linalg.norm(v)
    if n == 0:
        raise ValueError('build direction must be non-zero')
    return v / n


def material_frame(build_dir) -> np.ndarray:
    """3x3 rotation R whose columns are the material axes (1, 2 in-plane, 3 = build)."""
    e3 = as_direction(build_dir)
    a = np.array([1.0, 0, 0]) if abs(e3[0]) < 0.9 else np.array([0, 1.0, 0])
    e1 = np.cross(a, e3)
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(e3, e1)
    return np.column_stack([e1, e2, e3])


def local_stiffness_voigt(mat: dict) -> np.ndarray:
    """6x6 stiffness (Voigt order 11, 22, 33, 23, 13, 12; engineering shear) in material axes."""
    exy, ez = mat['E_xy'], mat['E_z']
    nxy, nxz = mat['nu_xy'], mat['nu_xz']
    gxz = mat['G_xz']
    gxy = exy / (2 * (1 + nxy))
    S = np.zeros((6, 6))
    S[0, 0] = S[1, 1] = 1 / exy
    S[2, 2] = 1 / ez
    S[0, 1] = S[1, 0] = -nxy / exy
    S[0, 2] = S[2, 0] = S[1, 2] = S[2, 1] = -nxz / exy
    S[3, 3] = S[4, 4] = 1 / gxz
    S[5, 5] = 1 / gxy
    return np.linalg.inv(S)


_VOIGT = [(0, 0), (1, 1), (2, 2), (1, 2), (0, 2), (0, 1)]


def stiffness_tensor(mat: dict, build_dir) -> np.ndarray:
    """4th-order stiffness C_ijkl (3,3,3,3) in the part frame."""
    Cv = local_stiffness_voigt(mat)
    C = np.zeros((3, 3, 3, 3))
    for I, (i, j) in enumerate(_VOIGT):
        for J, (k, l) in enumerate(_VOIGT):
            for (a, b) in {(i, j), (j, i)}:
                for (c, d) in {(k, l), (l, k)}:
                    C[a, b, c, d] = Cv[I, J]
    R = material_frame(build_dir)
    return np.einsum('ip,jq,kr,ls,pqrs->ijkl', R, R, R, R, C)
