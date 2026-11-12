"""Linear-elastic static FEA on quadratic tetrahedra (scikit-fem basis + own vectorised
assembly) with a transversely isotropic FDM material, perimeter shell / infill core
split, and per-mode failure evaluation in the material (layer) frame.

Units: mm, N, MPa. Heavy imports (skfem, scipy) happen inside the functions.
"""
from __future__ import annotations

import numpy as np

from .materials import PrintSettings, as_direction, material_frame, stiffness_tensor

_VOIGT = [(0, 0), (1, 1), (2, 2), (1, 2), (0, 2), (0, 1)]
MODES = ('in_plane', 'interlayer_normal', 'interlayer_shear', 'interlayer_combined')


def _voigt66(C):
    return np.array([[C[i, j, k, l] for (k, l) in _VOIGT] for (i, j) in _VOIGT])


# --------------------------------------------------------------------------- selectors

def make_selector(sel: dict):
    """Selector spec (part frame, mm) -> function f(x (3,n)) -> bool mask.

    {'box': {'min': [..], 'max': [..]}}
    {'cylinder': {'center': [..], 'axis': [..], 'radius': r, 'tol': 0.15, 'half_length': L}}
    {'plane': {'point': [..], 'normal': [..], 'tol': 0.05, 'box': {...optional}}}
    {'any': [sel, sel, ...]}
    """
    if 'any' in sel:
        fs = [make_selector(s) for s in sel['any']]
        return lambda x: np.any([f(x) for f in fs], axis=0)
    if 'box' in sel:
        lo = np.asarray(sel['box']['min'], float)[:, None]
        hi = np.asarray(sel['box']['max'], float)[:, None]
        return lambda x: np.all((x >= lo - 1e-9) & (x <= hi + 1e-9), axis=0)
    if 'cylinder' in sel:
        c = sel['cylinder']
        o = np.asarray(c['center'], float)[:, None]
        a = as_direction(c['axis'])[:, None]
        r, tol = float(c['radius']), float(c.get('tol', 0.15))
        hl = float(c.get('half_length', 1e9))

        def f(x):
            d = x - o
            ax = (d * a).sum(0)
            rad = np.linalg.norm(d - ax * a, axis=0)
            return (np.abs(rad - r) <= tol) & (np.abs(ax) <= hl)
        return f
    if 'plane' in sel:
        c = sel['plane']
        o = np.asarray(c['point'], float)[:, None]
        n = as_direction(c['normal'])[:, None]
        tol = float(c.get('tol', 0.05))
        fb = make_selector({'box': c['box']}) if 'box' in c else (lambda x: np.ones(x.shape[1], bool))
        return lambda x: (np.abs(((x - o) * n).sum(0)) <= tol) & fb(x)
    raise ValueError(f'unknown selector {sel}')


# --------------------------------------------------------------------------- surface / regions

class _Surface:
    """Boundary triangles with outward normals, sampled for distance queries."""

    def __init__(self, mesh, build_dir, density_per_mm2=60.0, seed=0):
        from scipy.spatial import cKDTree
        bf = mesh.boundary_facets()
        tri = mesh.facets[:, bf].T
        P = mesh.p.T
        a, b, c = P[tri[:, 0]], P[tri[:, 1]], P[tri[:, 2]]
        n = np.cross(b - a, c - a)
        area = 0.5 * np.linalg.norm(n, axis=1)
        n /= np.maximum(2 * area, 1e-30)[:, None]
        cen = (a + b + c) / 3
        ecen = P[mesh.t[:, mesh.f2t[0, bf]]].mean(axis=0)  # adjacent element centroid
        flip = ((cen - ecen) * n).sum(1) < 0
        n[flip] *= -1
        rng = np.random.default_rng(seed)
        cnt = np.maximum(1, np.ceil(area * density_per_mm2)).astype(int)
        fid = np.repeat(np.arange(len(tri)), cnt)
        u, v = rng.random(len(fid)), rng.random(len(fid))
        swap = u + v > 1
        u[swap], v[swap] = 1 - u[swap], 1 - v[swap]
        pts = a[fid] + u[:, None] * (b - a)[fid] + v[:, None] * (c - a)[fid]
        pts = np.vstack([pts, a, b, c])
        fid = np.concatenate([fid, np.arange(len(tri)), np.arange(len(tri)), np.arange(len(tri))])
        bd = as_direction(build_dir)
        self.is_tb = np.abs(n @ bd) > 0.7   # top/bottom skin (normal ~ build dir)
        tb = self.is_tb[fid]
        self.tree_tb = cKDTree(pts[tb]) if tb.any() else None
        self.tree_wall = cKDTree(pts[~tb]) if (~tb).any() else None
        self.tree_all = cKDTree(pts)
        self.normals, self.area, self.facets = n, area, bf

    def dist(self, x):
        """x (n,3) -> (distance to top/bottom skin, distance to side walls)."""
        inf = np.full(len(x), np.inf)
        d_tb = self.tree_tb.query(x)[0] if self.tree_tb is not None else inf
        d_w = self.tree_wall.query(x)[0] if self.tree_wall is not None else inf
        return d_tb, d_w


def _shell_mask(surface, ps: PrintSettings, x):
    d_tb, d_w = surface.dist(x)
    return (d_tb <= ps.top_bottom_thickness_mm + 1e-9) | (d_w <= ps.wall_thickness_mm + 1e-9), d_tb, d_w


# --------------------------------------------------------------------------- solver

def _linsolve(Kc, fc, basis, I, direct_max_dofs=20000):
    """Direct solve for small systems; smoothed-aggregation AMG + CG (pyamg, with the
    rigid-body modes as near-nullspace) for larger ones when pyamg is installed."""
    from scipy.sparse.linalg import spsolve
    n = Kc.shape[0]
    try:
        import pyamg
    except ImportError:
        pyamg = None
    if pyamg is None or n <= direct_max_dofs:
        return spsolve(Kc.tocsc(), fc), 'direct (SuperLU)'
    X = np.asarray(basis.doflocs)  # (3, N)
    comp = np.zeros(basis.N, int)
    for c, idx in enumerate(basis.split_indices()):
        comp[idx] = c
    Bm = np.zeros((basis.N, 6))
    for c in range(3):
        Bm[comp == c, c] = 1.0
    x, y, z = X
    # rotations about x, y, z: (0,-z,y), (z,0,-x), (-y,x,0)
    Bm[comp == 1, 3] = -z[comp == 1]
    Bm[comp == 2, 3] = y[comp == 2]
    Bm[comp == 0, 4] = z[comp == 0]
    Bm[comp == 2, 4] = -x[comp == 2]
    Bm[comp == 0, 5] = -y[comp == 0]
    Bm[comp == 1, 5] = x[comp == 1]
    ml = pyamg.smoothed_aggregation_solver(Kc.tocsr(), B=Bm[I], symmetry='symmetric', strength='symmetric',
                                           max_coarse=500)
    res = []
    sol = ml.solve(fc, tol=1e-10, accel='cg', maxiter=300, residuals=res)
    rel = res[-1] / max(res[0], 1e-300)
    if not rel < 1e-6:  # poorly constrained / ill-conditioned: fall back to the direct solver
        return spsolve(Kc.tocsc(), fc), f'direct (SuperLU; AMG stalled at rel.res {rel:.1e})'
    return sol, f'AMG-CG (pyamg, {len(res) - 1} it, rel.res {rel:.1e})'


def solve(p, t, mat, build_dir, fixtures, loads, print_settings=None, exclude_mm=1.0,
          singular_zones=(), probes=None, keep_fields=False):
    """Run one static case. p (N,3) mm, t (M,4). Returns a result dict."""
    import time
    from scipy.sparse import coo_matrix
    from scipy.spatial import cKDTree
    from skfem import (Basis, ElementTetP2, ElementVector, FacetBasis, LinearForm, MeshTet,
                       asm, condense)
    from skfem.helpers import dot

    t0 = time.time()
    ps = print_settings or PrintSettings()
    bd = as_direction(build_dir)
    R = material_frame(bd)
    mesh = MeshTet(np.ascontiguousarray(p.T), np.ascontiguousarray(t.T))
    e = ElementVector(ElementTetP2())
    basis = Basis(mesh, e, intorder=2)
    surf = _Surface(mesh, bd)

    # stiffness field (perimeter shell = solid, infill core = scaled)
    e_core, s_core = ps.infill_factors()
    xq = np.asarray(basis.global_coordinates())  # (3, nel, nq)
    nel, nq = xq.shape[1], xq.shape[2]
    shell_q, _, _ = _shell_mask(surf, ps, xq.reshape(3, -1).T)
    sfield = np.where(shell_q, 1.0, e_core).reshape(nel, nq)
    Cg = stiffness_tensor(mat, bd)
    C6 = _voigt66(Cg)

    # vectorised assembly K_e = sum_q B^T (s C) B w
    nb = basis.Nbfun
    B = np.empty((nel, nq, 6, nb))
    for i in range(nb):
        G = basis.basis[i][0].grad  # (3,3,nel,nq): G[a,b] = d u_a / d x_b
        B[:, :, 0, i] = G[0, 0]
        B[:, :, 1, i] = G[1, 1]
        B[:, :, 2, i] = G[2, 2]
        B[:, :, 3, i] = G[1, 2] + G[2, 1]
        B[:, :, 4, i] = G[0, 2] + G[2, 0]
        B[:, :, 5, i] = G[0, 1] + G[1, 0]
    W = basis.dx * sfield  # (nel, nq) includes |detJ| * weights
    CB = np.einsum('ab,eqbj->eqaj', C6, B)
    Ke = np.einsum('eqai,eqaj,eq->eij', B, CB, W)
    dofs = basis.element_dofs  # (nb, nel)
    rows = np.repeat(dofs.T, nb, axis=1).ravel()
    cols = np.tile(dofs.T, (1, nb)).ravel()
    K = coo_matrix((Ke.ravel(), (rows, cols)), shape=(basis.N, basis.N)).tocsr()
    del B, CB, Ke

    # fixtures
    D = []
    bc_nodes = []
    fixture_info = []
    for fx in fixtures:
        facets = mesh.facets_satisfying(make_selector(fx['select']), boundaries_only=True)
        if len(facets) == 0:
            raise ValueError(f"fixture {fx.get('name')} selects no boundary facets")
        comps = fx.get('dofs', 'xyz')
        dv = basis.get_dofs(facets)
        names = [f'u^{"xyz".index(c) + 1}' for c in comps]
        for kind in (dv.nodal, dv.facet, dv.edge, dv.interior):
            for k, v in kind.items():
                if k in names:
                    D.append(np.asarray(v))
        bc_nodes.append(np.unique(mesh.facets[:, facets]))
        fixture_info.append({'name': fx.get('name'), 'facets': int(len(facets))})
    D = np.unique(np.concatenate(D))

    # loads
    f = np.zeros(basis.N)
    load_info = []
    for ld in loads:
        facets = mesh.facets_satisfying(make_selector(ld['select']), boundaries_only=True)
        if len(facets) == 0:
            raise ValueError(f"load {ld.get('name')} selects no boundary facets")
        fb = FacetBasis(mesh, e, facets=facets, intorder=3)
        area = float(fb.dx.sum())
        if 'force_n' in ld:
            tr = np.asarray(ld['force_n'], float) / area

            @LinearForm
            def lf(v, w, tr=tr):
                return tr[0] * v[0] + tr[1] * v[1] + tr[2] * v[2]
            info = {'force_n': list(map(float, ld['force_n']))}
        elif 'torque_nm' in ld:
            ax = as_direction(ld.get('axis', 'x'))
            c0 = np.asarray(ld.get('center', (0, 0, 0)), float)
            xq_f = np.asarray(fb.global_coordinates())
            r = xq_f - c0[:, None, None]
            rperp = r - np.einsum('i,inq->nq', ax, r)[None] * ax[:, None, None]
            J = float((np.einsum('inq,inq->nq', rperp, rperp) * fb.dx).sum())
            k = float(ld['torque_nm']) * 1000.0 / J  # N*mm

            @LinearForm
            def lf(v, w, ax=ax, c0=c0, k=k):
                x = w.x
                rr = np.array([x[i] - c0[i] for i in range(3)])
                ax_r = np.einsum('i,i...->...', ax, rr)
                rp = rr - ax_r[None] * ax[:, None, None]
                td = np.array([ax[1] * rp[2] - ax[2] * rp[1], ax[2] * rp[0] - ax[0] * rp[2],
                               ax[0] * rp[1] - ax[1] * rp[0]])
                return k * (td[0] * v[0] + td[1] * v[1] + td[2] * v[2])
            info = {'torque_nm': float(ld['torque_nm']), 'axis': ax.tolist()}
        else:
            raise ValueError('load needs force_n or torque_nm')
        f += asm(lf, fb)
        bc_nodes.append(np.unique(mesh.facets[:, facets]))
        load_info.append({'name': ld.get('name'), 'facets': int(len(facets)), 'area_mm2': area, **info})

    t_asm = time.time() - t0
    u = np.zeros(basis.N)
    Kc, fc, _, I = condense(K, f, D=D)
    u[I], solver = _linsolve(Kc, fc, basis, I)
    t_solve = time.time() - t0 - t_asm

    # ---- recovery at the 10 P2 nodes of each element, averaged per node
    Xref = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], float)
    pairs = [(0, 1), (1, 2), (0, 2), (0, 3), (1, 3), (2, 3)]
    Xr = np.vstack([Xref] + [(Xref[a] + Xref[b]) / 2 for a, b in pairs]).T
    rb = Basis(mesh, e, quadrature=(Xr, np.ones(Xr.shape[1])))
    xg = np.asarray(rb.global_coordinates())  # (3, nel, 10)
    assert np.allclose(xg[:, :, :4], mesh.p[:, mesh.t].transpose(0, 2, 1), atol=1e-8), 'unexpected reference ordering'
    ui = rb.interpolate(u)
    G = ui.grad  # (3,3,nel,10)
    eps = 0.5 * (G + G.transpose(1, 0, 2, 3))
    # node keys: vertices, then unique edges
    tt = mesh.t.T
    ekey = np.sort(np.stack([tt[:, [a, b]] for a, b in pairs], axis=1), axis=2)  # (nel,6,2)
    flat = ekey.reshape(-1, 2)
    uniq, inv = np.unique(flat, axis=0, return_inverse=True)
    keys = np.hstack([tt, mesh.p.shape[1] + inv.reshape(-1, 6)])  # (nel,10)
    nn = mesh.p.shape[1] + len(uniq)
    kf = keys.ravel()
    cnt = np.bincount(kf, minlength=nn).astype(float)
    E6 = np.stack([eps[i, j] for (i, j) in _VOIGT], axis=-1).reshape(-1, 6)  # tensor strains
    acc = np.zeros((nn, 6))
    np.add.at(acc, kf, E6)
    epsn = acc / cnt[:, None]
    X = np.zeros((nn, 3))
    X[kf] = xg.transpose(1, 2, 0).reshape(-1, 3)
    U = np.zeros((nn, 3))
    np.add.at(U, kf, np.asarray(ui).transpose(1, 2, 0).reshape(-1, 3))
    U /= cnt[:, None]

    # stress at nodes
    shell_n, d_tb, d_w = _shell_mask(surf, ps, X)
    s_n = np.where(shell_n, 1.0, e_core)
    eps_t = np.zeros((nn, 3, 3))
    for I_, (i, j) in enumerate(_VOIGT):
        eps_t[:, i, j] = epsn[:, I_]
        eps_t[:, j, i] = epsn[:, I_]
    sig = np.einsum('ijkl,nkl->nij', Cg, eps_t) * s_n[:, None, None]
    sl = np.einsum('ip,nij,jq->npq', R, sig, R)  # material frame

    # strengths at nodes
    k_il = ps.interlayer_factor(mat)
    sf_scale = np.where(shell_n, 1.0, s_core)
    Xt, Xc = mat['Xt'] * sf_scale, mat['Xc'] * sf_scale
    Zt, S = mat['Zt'] * k_il * sf_scale, mat['S_il'] * k_il * sf_scale
    on_wall_surface = d_w < 0.05
    notch = np.where(on_wall_surface, ps.layer_notch_factor, 1.0)

    a11, a22, a12 = sl[:, 0, 0], sl[:, 1, 1], sl[:, 0, 1]
    m = 0.5 * (a11 + a22)
    rr = np.sqrt((0.5 * (a11 - a22)) ** 2 + a12 ** 2)
    p1, p2 = m + rr, m - rr
    big = 1e9
    sf_inp = np.minimum(np.where(p1 > 0, Xt / np.maximum(p1, 1e-12), big),
                        np.where(p2 < 0, Xc / np.maximum(-p2, 1e-12), big))
    s33 = sl[:, 2, 2] * notch
    tau = np.sqrt(sl[:, 0, 2] ** 2 + sl[:, 1, 2] ** 2)
    sf_norm = np.where(s33 > 0, Zt / np.maximum(s33, 1e-12), big)
    sf_shear = S / np.maximum(tau, 1e-12)
    comb = np.sqrt((np.maximum(s33, 0) / Zt) ** 2 + (tau / S) ** 2)
    sf_comb = 1.0 / np.maximum(comb, 1e-12)
    sfs = {'in_plane': sf_inp, 'interlayer_normal': sf_norm, 'interlayer_shear': sf_shear,
           'interlayer_combined': sf_comb}

    # exclusion zones
    excl = np.zeros(nn, bool)
    bcn = np.unique(np.concatenate(bc_nodes)) if bc_nodes else np.zeros(0, int)
    if exclude_mm > 0 and len(bcn):
        excl |= cKDTree(mesh.p[:, bcn].T).query(X)[0] < exclude_mm
    zone_masks = {}
    for z in singular_zones:
        p0, p1_ = np.asarray(z['p0'], float), np.asarray(z['p1'], float)
        d = p1_ - p0
        tpar = np.clip(((X - p0) @ d) / (d @ d), 0, 1)
        dist = np.linalg.norm(X - (p0 + tpar[:, None] * d), axis=1)
        mz = dist < float(z.get('r_mm', 1.0))
        zone_masks[z.get('name', f'zone{len(zone_masks)}')] = mz
        excl |= mz

    vm = np.sqrt(0.5 * ((sig[:, 0, 0] - sig[:, 1, 1]) ** 2 + (sig[:, 1, 1] - sig[:, 2, 2]) ** 2 +
                        (sig[:, 2, 2] - sig[:, 0, 0]) ** 2) + 3 * (sig[:, 0, 1] ** 2 + sig[:, 1, 2] ** 2 +
                                                                  sig[:, 0, 2] ** 2))
    umag = np.linalg.norm(U, axis=1)

    def at(mask, arr):
        idx = np.nonzero(mask)[0]
        k = idx[np.argmin(arr[idx])]
        return k

    res_modes = {}
    for name, arr in sfs.items():
        k = at(~excl, arr)
        kr = int(np.argmin(arr))
        res_modes[name] = {
            'min_sf': float(min(arr[k], big)), 'at_mm': X[k].round(3).tolist(),
            'region': 'shell' if shell_n[k] else 'core',
            'stress_material_frame_mpa': {'s11': float(sl[k, 0, 0]), 's22': float(sl[k, 1, 1]),
                                          's33': float(sl[k, 2, 2]), 's13': float(sl[k, 0, 2]),
                                          's23': float(sl[k, 1, 2]), 's12': float(sl[k, 0, 1])},
            'raw_min_sf_incl_excluded': float(arr[kr]), 'raw_at_mm': X[kr].round(3).tolist(),
        }
    for zname, mz in zone_masks.items():
        if mz.any():
            for name, arr in sfs.items():
                k = at(mz, arr)
                res_modes[name].setdefault('singular_zones', {})[zname] = {
                    'min_sf_mesh_dependent': float(arr[k]), 'at_mm': X[k].round(3).tolist()}

    gov = min(res_modes, key=lambda n: res_modes[n]['min_sf'])
    out = {
        'material': mat.get('name'), 'build_dir': bd.round(4).tolist(),
        'print_settings': ps.describe(mat),
        'mesh': {'n_points': int(mesh.p.shape[1]), 'n_tets': int(mesh.t.shape[1]), 'dofs': int(basis.N),
                 'shell_fraction_of_volume': float((basis.dx * shell_q.reshape(nel, nq)).sum() / basis.dx.sum()),
                 'volume_mm3': float(basis.dx.sum())},
        'fixtures': fixture_info, 'loads': load_info,
        'max_deflection_mm': float(umag[~excl].max() if (~excl).any() else umag.max()),
        'max_deflection_at_mm': X[int(np.argmax(umag))].round(3).tolist(),
        'max_von_mises_mpa': float(vm[~excl].max()), 'max_von_mises_at_mm': X[at(~excl, -vm)].round(3).tolist(),
        'modes': res_modes, 'governing_mode': gov, 'min_sf': res_modes[gov]['min_sf'],
        'exclude_mm': exclude_mm, 'solver': solver, 'timing_s': {'assemble': round(t_asm, 2), 'solve': round(t_solve, 2),
                                              'total': round(time.time() - t0, 2)},
    }
    if probes:
        tree = cKDTree(X)
        out['probes'] = {}
        for name, pt in probes.items():
            _, k = tree.query(np.asarray(pt, float))
            out['probes'][name] = {'u_mm': U[k].round(5).tolist(), '|u|_mm': float(umag[k]),
                                   'node_mm': X[k].round(3).tolist()}
    if keep_fields:
        out['_fields'] = {'X': X, 'U': U, 'sig': sig, 'sig_mat': sl, 'vm': vm, 'excluded': excl,
                          'shell': shell_n, 'sf': sfs, 'R': R}
    return out
