"""A small two-layer grid router, so an AI can get from placed parts to connected copper without
another program. It is a maze router (A* on a grid, 45-degree moves, vias between F.Cu and B.Cu)
that routes one connection at a time, shortest first, and keeps every other net's copper at the
clearance. Good enough for small, sparse boards like a sensor or a counter; a dense board wants a
real autorouter (freerouting) or hand routing. Unroutable connections are reported, not forced.
"""
from __future__ import annotations

import heapq
import math
from pathlib import Path

import numpy as np

from ..sexpr import child, children, parse

LAYERS = ('F.Cu', 'B.Cu')


def _xy(n):
    return float(n[1]), float(n[2])


def _rot(x, y, deg):
    a = math.radians(deg)
    return x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)


def read_board(pcb_path):
    tree = parse(Path(pcb_path).read_text(encoding='utf-8'))
    pads, tracks, vias, edge, zones = [], [], [], [], []
    for fp in children(tree, 'footprint'):
        at = child(fp, 'at')
        fx, fy = _xy(at)
        frot = float(at[3]) if len(at) > 3 else 0.0
        ref = next((p[2] for p in children(fp, 'property') if p[1] == 'Reference'), '?')
        for pad in children(fp, 'pad'):
            pat, sz = child(pad, 'at'), child(pad, 'size')
            if pat is None or sz is None:
                continue
            px, py = _xy(pat)
            dx, dy = _rot(px, py, -frot)
            layers = ' '.join(str(x) for x in (child(pad, 'layers') or [])[1:])
            netn = child(pad, 'net')
            net = str(netn[-1]) if netn is not None else ''
            pads.append({'ref': ref, 'pad': str(pad[1]), 'x': fx + dx, 'y': fy + dy, 'w': float(sz[1]),
                         'h': float(sz[2]), 'ang': float(pat[3]) if len(pat) > 3 else frot,
                         'shape': str(pad[3]) if len(pad) > 3 else 'rect',
                         'layers': [i for i, L in enumerate(LAYERS) if L in layers or '*.Cu' in layers],
                         'net': net})
    for seg in children(tree, 'segment'):
        lay = str(child(seg, 'layer')[1])
        if lay in LAYERS:
            netn = child(seg, 'net')
            tracks.append({'a': _xy(child(seg, 'start')), 'b': _xy(child(seg, 'end')), 'w': float(child(seg, 'width')[1]),
                           'layer': LAYERS.index(lay), 'net': str(netn[-1]) if netn is not None else ''})
    for v in children(tree, 'via'):
        netn = child(v, 'net')
        vias.append({'p': _xy(child(v, 'at')), 'd': float(child(v, 'size')[1]),
                     'net': str(netn[-1]) if netn is not None else ''})
    for kind in ('gr_line', 'gr_rect', 'gr_arc', 'gr_poly'):
        for g in children(tree, kind):
            lay = child(g, 'layer')
            if lay is not None and str(lay[1]) == 'Edge.Cuts':
                for k in ('start', 'end', 'mid'):
                    c = child(g, k)
                    if c is not None:
                        edge.append(_xy(c))
                pts = child(g, 'pts')
                if pts is not None:
                    edge += [_xy(p) for p in children(pts, 'xy')]
    for z in children(tree, 'zone'):
        netn = child(z, 'net_name') or child(z, 'net')
        zones.append(str(netn[-1]) if netn is not None else '')
    if not edge:
        raise ValueError('the board has no outline (Edge.Cuts); add one with board_edit outline')
    return {'pads': pads, 'tracks': tracks, 'vias': vias, 'bbox': (min(x for x, _ in edge), min(y for _, y in edge),
                                                                    max(x for x, _ in edge), max(y for _, y in edge)),
            'zone_nets': sorted(set(zones) - {''})}


class Grid:
    def __init__(self, bbox, pitch):
        self.x0, self.y0 = bbox[0], bbox[1]
        self.p = pitch
        self.nx = int(math.ceil((bbox[2] - bbox[0]) / pitch)) + 1
        self.ny = int(math.ceil((bbox[3] - bbox[1]) / pitch)) + 1
        self.X = self.x0 + np.arange(self.nx) * pitch
        self.Y = self.y0 + np.arange(self.ny) * pitch
        # owner: net id that stamped the cell (-1 none); multi: stamped by two nets or by the edge
        self.owner = np.full((2, 2, self.ny, self.nx), -1, np.int32)     # [kind: track/via][layer]
        self.multi = np.zeros((2, 2, self.ny, self.nx), bool)

    def window(self, x0, y0, x1, y1):
        i0 = max(0, int((x0 - self.x0) / self.p) - 1)
        i1 = min(self.nx, int((x1 - self.x0) / self.p) + 2)
        j0 = max(0, int((y0 - self.y0) / self.p) - 1)
        j1 = min(self.ny, int((y1 - self.y0) / self.p) + 2)
        return i0, i1, j0, j1

    def stamp(self, kind, layer, net, i0, i1, j0, j1, mask):
        o = self.owner[kind, layer, j0:j1, i0:i1]
        mu = self.multi[kind, layer, j0:j1, i0:i1]
        mu |= mask & (o != -1) & (o != net)
        o[mask & (o == -1)] = net

    def free(self, kind, layer, net):
        o = self.owner[kind, layer]
        return ~self.multi[kind, layer] & ((o == -1) | (o == net))


def _pad_dist(pad, X, Y):
    """Distance from points to the pad copper (0 inside)."""
    dx, dy = X - pad['x'], Y - pad['y']
    a = math.radians(pad['ang'])
    u = dx * math.cos(a) - dy * math.sin(a)
    v = dx * math.sin(a) + dy * math.cos(a)
    if pad['shape'] == 'circle':
        return np.maximum(np.hypot(u, v) - pad['w'] / 2, 0)
    if pad['shape'] == 'oval':
        r = min(pad['w'], pad['h']) / 2
        hu, hv = max(pad['w'] / 2 - r, 0), max(pad['h'] / 2 - r, 0)
        return np.maximum(np.hypot(np.maximum(np.abs(u) - hu, 0), np.maximum(np.abs(v) - hv, 0)) - r, 0)
    return np.hypot(np.maximum(np.abs(u) - pad['w'] / 2, 0), np.maximum(np.abs(v) - pad['h'] / 2, 0))


def _seg_dist(a, b, X, Y):
    ax, ay = a
    bx, by = b
    vx, vy = bx - ax, by - ay
    L2 = vx * vx + vy * vy or 1e-12
    t = np.clip(((X - ax) * vx + (Y - ay) * vy) / L2, 0, 1)
    return np.hypot(X - (ax + t * vx), Y - (ay + t * vy))


def route(pcb_path, width_mm=0.25, clearance_mm=0.2, via_mm=0.6, drill_mm=0.3, pitch_mm=0.2, nets=None,
          skip_nets=None, edge_mm=0.4, max_expand=400000):
    """Plan tracks and vias for the unrouted connections. Returns ops for eda.edit_pcb and a report."""
    b = read_board(pcb_path)
    skip = set(skip_nets if skip_nets is not None else b['zone_nets'])
    g = Grid(b['bbox'], pitch_mm)
    names = sorted({p['net'] for p in b['pads'] if p['net']} | {t['net'] for t in b['tracks'] if t['net']})
    nid = {n: k for k, n in enumerate(names)}
    rt = clearance_mm + width_mm / 2          # how far a track centre stays from other copper
    rv = clearance_mm + via_mm / 2            # how far a via centre stays from other copper
    half = {0: rt, 1: rv}

    def stamp_shape(net, layers, dist_fn, ext, grow=0.0):
        n = nid.get(net, -2 - len(nid))            # unnamed copper blocks everyone
        for kind in (0, 1):
            r = half[kind] + grow
            i0, i1, j0, j1 = g.window(ext[0] - r, ext[1] - r, ext[2] + r, ext[3] + r)
            if i0 >= i1 or j0 >= j1:
                continue
            X, Y = np.meshgrid(g.X[i0:i1], g.Y[j0:j1])
            mask = dist_fn(X, Y) <= r
            for L in layers:
                g.stamp(kind, L, n, i0, i1, j0, j1, mask)

    pad_cells, pad_center = {}, {}
    for p in b['pads']:
        ext = (p['x'] - max(p['w'], p['h']), p['y'] - max(p['w'], p['h']),
               p['x'] + max(p['w'], p['h']), p['y'] + max(p['w'], p['h']))
        stamp_shape(p['net'], p['layers'], lambda X, Y, p=p: _pad_dist(p, X, Y), ext)
        i0, i1, j0, j1 = g.window(*ext)
        X, Y = np.meshgrid(g.X[i0:i1], g.Y[j0:j1])
        dist = _pad_dist(p, X, Y)
        inside = dist <= 0
        if not inside.any():                                   # pad smaller than the grid: nearest cell
            jj, ii = np.unravel_index(np.argmin(dist), X.shape)
            inside[jj, ii] = True
        js, is_ = np.nonzero(inside)
        pad_cells[(p['ref'], p['pad'])] = [(L, j0 + j, i0 + i) for L in p['layers'] for j, i in zip(js, is_)]
        for c in pad_cells[(p['ref'], p['pad'])]:
            pad_center[c] = (p['x'], p['y'])
    for t in b['tracks']:
        ext = (min(t['a'][0], t['b'][0]), min(t['a'][1], t['b'][1]), max(t['a'][0], t['b'][0]), max(t['a'][1], t['b'][1]))
        stamp_shape(t['net'], [t['layer']], lambda X, Y, t=t: _seg_dist(t['a'], t['b'], X, Y), ext, t['w'] / 2)
    for v in b['vias']:
        ext = (v['p'][0], v['p'][1], v['p'][0], v['p'][1])
        stamp_shape(v['net'], [0, 1], lambda X, Y, v=v: np.hypot(X - v['p'][0], Y - v['p'][1]), ext, v['d'] / 2)
    # board edge: nothing within edge_mm of it or outside it
    for kind in (0, 1):
        m = half[kind] - clearance_mm + edge_mm
        bx0, by0, bx1, by1 = b['bbox']
        X, Y = np.meshgrid(g.X, g.Y)
        out = (X < bx0 + m) | (X > bx1 - m) | (Y < by0 + m) | (Y > by1 - m)
        g.multi[kind, :, out] = True

    # connections: per net, a tree grown pad by pad (nearest pad first), shortest nets first
    by_net = {}
    for p in b['pads']:
        if p['net'] and not p['net'].startswith('unconnected-') and p['net'] not in skip:
            if nets is None or p['net'] in nets:
                by_net.setdefault(p['net'], []).append(p)
    order = sorted(by_net, key=lambda n: _mst_len(by_net[n]))
    ops, done, failed = [], [], []
    step = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0), (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414),
            (-1, -1, 1.414)]
    via_cost = max(6.0, via_mm / pitch_mm * 3)
    for net in order:
        n = nid[net]
        free_t = [g.free(0, L, n) for L in (0, 1)]
        free_v = g.free(1, 0, n) & g.free(1, 1, n)
        pads = by_net[net]
        tree = set(pad_cells[(pads[0]['ref'], pads[0]['pad'])])
        left = pads[1:]
        while left:
            # nearest remaining pad to the pads already connected
            tree_xy = [(g.X[i], g.Y[j]) for _, j, i in list(tree)[:: max(1, len(tree) // 64)]]
            left.sort(key=lambda p: min(math.hypot(p['x'] - x, p['y'] - y) for x, y in tree_xy))
            tgt_pad = left.pop(0)
            targets = set(pad_cells[(tgt_pad['ref'], tgt_pad['pad'])])
            path = _astar(tree, targets, free_t, free_v, step, via_cost, (tgt_pad['x'], tgt_pad['y']), g, max_expand)
            if path is None:
                failed.append({'net': net, 'to': f"{tgt_pad['ref']}.{tgt_pad['pad']}"})
                continue
            new_ops = _path_ops(path, net, g, width_mm, via_mm, drill_mm, pad_center)
            ops += new_ops
            done.append({'net': net, 'to': f"{tgt_pad['ref']}.{tgt_pad['pad']}", 'cells': len(path)})
            tree |= set(path) | targets
            # the new copper now keeps other nets away
            for op in new_ops:
                if op['op'] == 'add_track':
                    (ax, ay), (bx, by) = op['points']
                    L = LAYERS.index(op['layer'])
                    stamp_shape(net, [L], lambda X, Y, a=(ax, ay), c=(bx, by): _seg_dist(a, c, X, Y),
                                (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)), width_mm / 2)
                else:
                    stamp_shape(net, [0, 1], lambda X, Y, op=op: np.hypot(X - op['x_mm'], Y - op['y_mm']),
                                (op['x_mm'], op['y_mm'], op['x_mm'], op['y_mm']), via_mm / 2)
    return ops, {'routed': len(done), 'failed': failed, 'skipped_nets': sorted(skip), 'grid_mm': pitch_mm,
                 'width_mm': width_mm, 'clearance_mm': clearance_mm,
                 'tracks': sum(1 for o in ops if o['op'] == 'add_track'), 'vias': sum(1 for o in ops if o['op'] == 'add_via')}


def _mst_len(pads):
    if len(pads) < 2:
        return 0.0
    left, tot = pads[1:], 0.0
    tree = [pads[0]]
    while left:
        d, k = min((min(math.hypot(p['x'] - q['x'], p['y'] - q['y']) for q in tree), k) for k, p in enumerate(left))
        tot += d
        tree.append(left.pop(k))
    return tot


def _astar(sources, targets, free_t, free_v, step, via_cost, goal_xy, g, max_expand):
    gx, gy = goal_xy
    INF = float('inf')
    dist = {}
    heap = []
    for s in sources:
        dist[s] = 0.0
        L, j, i = s
        heapq.heappush(heap, (math.hypot(g.X[i] - gx, g.Y[j] - gy) / g.p, 0.0, s, None))
    prev = {}
    expanded = 0
    while heap:
        f, d, cur, par = heapq.heappop(heap)
        if d > dist.get(cur, INF):
            continue
        if cur not in prev:
            prev[cur] = par
        if cur in targets:
            path = [cur]
            while prev.get(path[-1]) is not None:
                path.append(prev[path[-1]])
            return path[::-1]
        expanded += 1
        if expanded > max_expand:
            return None
        L, j, i = cur
        moves = [((L, j + dj, i + di), c) for di, dj, c in step]
        if free_v[j, i]:
            moves.append(((1 - L, j, i), via_cost))
        for nxt, c in moves:
            L2, j2, i2 = nxt
            if not (0 <= j2 < g.ny and 0 <= i2 < g.nx):
                continue
            if nxt not in targets and not free_t[L2][j2, i2]:
                continue
            if c > 1.2 and L2 == L and not (free_t[L][j, i2] and free_t[L][j2, i]):   # no corner cutting
                continue
            nd = d + c
            if nd < dist.get(nxt, INF):
                dist[nxt] = nd
                h = math.hypot(g.X[i2] - gx, g.Y[j2] - gy) / g.p
                heapq.heappush(heap, (nd + h, nd, nxt, cur))
    return None


def _path_ops(path, net, g, width, via, drill, pad_center=None):
    ops = []
    seg = [path[0]]
    pad_center = pad_center or {}
    # each end that sits on a pad gets a stub to the pad centre, so the track lands in the copper
    for end in (path[0], path[-1]):
        if end in pad_center:
            cx, cy = pad_center[end]
            ex, ey = float(g.X[end[2]]), float(g.Y[end[1]])
            if math.hypot(cx - ex, cy - ey) > 1e-3:
                ops.append({'op': 'add_track', 'net': net, 'layer': LAYERS[end[0]], 'width_mm': width,
                            'points': [[round(ex, 4), round(ey, 4)], [round(cx, 4), round(cy, 4)]]})

    def flush(seg):
        if len(seg) < 2:
            return
        pts = [seg[0]]
        for k in range(1, len(seg) - 1):           # drop points in the middle of straight runs
            a, b, c = pts[-1], seg[k], seg[k + 1]
            if (b[1] - a[1]) * (c[2] - b[2]) != (c[1] - b[1]) * (b[2] - a[2]):
                pts.append(b)
        pts.append(seg[-1])
        for a, b in zip(pts, pts[1:]):
            ops.append({'op': 'add_track', 'net': net, 'layer': LAYERS[a[0]], 'width_mm': width,
                        'points': [[round(float(g.X[a[2]]), 4), round(float(g.Y[a[1]]), 4)],
                                   [round(float(g.X[b[2]]), 4), round(float(g.Y[b[1]]), 4)]]})
    for c in path[1:]:
        if c[0] != seg[-1][0]:
            flush(seg)
            ops.append({'op': 'add_via', 'net': net, 'x_mm': round(float(g.X[c[2]]), 4),
                        'y_mm': round(float(g.Y[c[1]]), 4), 'size_mm': via, 'drill_mm': drill})
            seg = [c]
        else:
            seg.append(c)
    flush(seg)
    return ops


__all__ = ['route', 'read_board']
