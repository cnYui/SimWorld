"""Strength check: cantilever vs beam theory, FDM anisotropy, print settings.
Skipped when the optional FEA dependencies (pip install "emcosim[strength]") are missing."""
import dataclasses

import numpy as np
import pytest

from emcosim import strength
from emcosim.strength import PrintSettings, SOLID, get_material

ok, missing = strength.available()
needs_fea = pytest.mark.skipif(not ok, reason=f'optional strength deps missing: {missing}')

L, B, H, P = 40.0, 4.0, 6.0, 10.0
ROOT = [{'name': 'root', 'select': {'plane': {'point': [0, 0, 0], 'normal': 'x'}}}]
TIP = [{'name': 'tip', 'select': {'plane': {'point': [L, 0, 0], 'normal': 'x'}}, 'force_n': [0, 0, -P]}]


def isotropic(name='PLA'):
    m = get_material(name)
    m.update(E_z=m['E_xy'], nu_xz=m['nu_xy'], G_xz=m['E_xy'] / 2 / (1 + m['nu_xy']))
    return m


@pytest.fixture(scope='module')
def beam_mesh():
    from emcosim.strength.meshing import box_mesh
    return box_mesh(L, B, H, 1.0)


def test_import_without_fea_deps_is_cheap():
    # the public API and material table never need the heavy deps at import time
    assert 'PLA' in strength.MATERIALS and 'PETG' in strength.MATERIALS
    for m in strength.MATERIALS.values():
        assert 0.3 <= m['Zt'] / m['Xt'] <= 0.6
        assert 'assum' in m['note'] and m['source']
    C = strength.stiffness_tensor(get_material('PLA'), 'z')
    assert np.allclose(C, C.transpose(2, 3, 0, 1))


@needs_fea
def test_cantilever_matches_beam_theory(beam_mesh):
    from emcosim.strength.fea import solve
    p, t = beam_mesh
    mat = isotropic()
    r = solve(p, t, mat, 'z', ROOT, TIP, SOLID, probes={'tip': [L, B / 2, H / 2]}, keep_fields=True)
    E, nu = mat['E_xy'], mat['nu_xy']
    I = B * H ** 3 / 12
    d_bend = P * L ** 3 / (3 * E * I)
    d_shear = P * L / (5 / 6 * E / 2 / (1 + nu) * B * H)
    d = abs(r['probes']['tip']['u_mm'][2])
    assert d == pytest.approx(d_bend + d_shear, rel=0.10)
    # bending stress on the top fibre one beam depth away from the clamp (St-Venant)
    F = r['_fields']
    x0 = H
    m = (np.abs(F['X'][:, 0] - x0) < 0.3) & (F['X'][:, 2] > H - 1e-6)
    s_fe = F['sig'][m, 0, 0].mean()
    s_an = 6 * P * (L - x0) / (B * H ** 2)
    assert s_fe == pytest.approx(s_an, rel=0.10)
    # root stress extrapolated linearly from two sections vs 6PL/(bh^2)
    x1 = 2 * H
    m1 = (np.abs(F['X'][:, 0] - x1) < 0.3) & (F['X'][:, 2] > H - 1e-6)
    s1 = F['sig'][m1, 0, 0].mean()
    s_root = s_fe + (s_fe - s1) * x0 / (x1 - x0)
    assert s_root == pytest.approx(6 * P * L / (B * H ** 2), rel=0.10)


@needs_fea
def test_anisotropy_flat_vs_upright(beam_mesh):
    """Bending stress runs along x. Build along z (flat): stress in the layer plane.
    Build along x (upright): the same stress pulls the layers apart."""
    from emcosim.strength.fea import solve
    p, t = beam_mesh
    flat = solve(p, t, get_material('PLA'), 'z', ROOT, TIP, SOLID)
    upright = solve(p, t, get_material('PLA'), 'x', ROOT, TIP, SOLID)
    sf_f = flat['modes']['interlayer_normal']['min_sf']
    sf_u = upright['modes']['interlayer_normal']['min_sf']
    assert sf_u < 0.3 * sf_f
    # in the upright case interlayer tension governs, flat it is in-plane or shear
    assert upright['governing_mode'] in ('interlayer_normal', 'interlayer_combined')
    assert flat['governing_mode'] != 'interlayer_normal'
    # the upright interlayer SF is about Zt / sigma_bending (exclusion zone removes the clamp)
    s_max = 6 * P * (L - 1.0) / (B * H ** 2)
    assert sf_u == pytest.approx(get_material('PLA')['Zt'] / s_max, rel=0.2)


@needs_fea
def test_print_settings_change_results(beam_mesh):
    from emcosim.strength.fea import solve
    p, t = beam_mesh
    base = PrintSettings(walls=2, infill_pct=20)
    sparse = solve(p, t, get_material('PETG'), 'z', ROOT, TIP, base, probes={'tip': [L, B / 2, H / 2]})
    solid = solve(p, t, get_material('PETG'), 'z', ROOT, TIP, SOLID, probes={'tip': [L, B / 2, H / 2]})
    assert sparse['probes']['tip']['|u|_mm'] > solid['probes']['tip']['|u|_mm']
    up_thin = solve(p, t, get_material('PETG'), 'x', ROOT, TIP, dataclasses.replace(SOLID, layer_height_mm=0.12))
    up_thick = solve(p, t, get_material('PETG'), 'x', ROOT, TIP, dataclasses.replace(SOLID, layer_height_mm=0.28))
    assert up_thin['modes']['interlayer_normal']['min_sf'] > up_thick['modes']['interlayer_normal']['min_sf']


def test_print_setting_relations():
    ps = PrintSettings()
    pla = get_material('PLA')
    assert ps.interlayer_factor(pla) == pytest.approx(1.0)
    assert ps.layer_notch_factor == pytest.approx(1.0)
    assert PrintSettings(layer_height_mm=0.28).interlayer_factor(pla) < 1.0
    assert PrintSettings(layer_height_mm=0.28).layer_notch_factor > 1.0
    assert not PrintSettings(layer_height_mm=0.28, nozzle_mm=0.2).printable
    assert PrintSettings(nozzle_mm=0.6).min_inside_radius_mm == pytest.approx(0.3)
    e, s = PrintSettings(infill_pct=20).infill_factors()
    assert 0 < e < 1 and 0 < s < 1
    flags = strength.corner_layer_flags({'edge_dir': 'y', 'face_normals': [[0, 0, -1], [1, 0, 0]]}, 'x')
    assert flags['on_layer_boundary']
    flags = strength.corner_layer_flags({'edge_dir': 'y', 'face_normals': [[0, 0, -1], [1, 0, 0]]}, 'y')
    assert not flags['on_layer_boundary'] and flags['edge_parallel_to_build']
