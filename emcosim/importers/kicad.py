"""Read a KiCad 6/7/8 .kicad_pcb and extract what the co-simulator needs:
components with values, the netlist, board outline, and copper resistance of
each net (from track length/width), which the power model uses.

Nothing here edits the PCB; the result is a plain dict that is stored in the
project as YAML so it can be diffed.
"""
from __future__ import annotations

import heapq
import math
import re
from pathlib import Path

from ..sexpr import child, children, parse

COPPER_RHO = 1.72e-8          # ohm*m at 20 C
DEFAULT_CU_THICKNESS = 35e-6  # 1 oz
_SI = {'p': 1e-12, 'n': 1e-9, 'u': 1e-6, 'µ': 1e-6, 'μ': 1e-6, 'm': 1e-3,
       'k': 1e3, 'K': 1e3, 'M': 1e6, 'G': 1e9}


def parse_value(text):
    """'470uF' -> 4.7e-4, '100kΩ' -> 1e5, '4k7' -> 4700, '0R22' -> 0.22. None if not numeric."""
    if not text:
        return None
    s = str(text).strip().replace('Ω', '').replace('ohm', '').replace('Ohm', '')
    s = re.sub(r'[FHfh]$', '', s).strip()
    m = re.fullmatch(r'(\d+)([pnuµμmkKMGR])(\d+)', s)
    if m:
        a, unit, b = m.groups()
        return float(f'{a}.{b}') * (1.0 if unit == 'R' else _SI[unit])
    m = re.fullmatch(r'([0-9]*\.?[0-9]+(?:[eE][-+]?\d+)?)\s*([pnuµμmkKMG]?)', s)
    if not m:
        return None
    return float(m.group(1)) * _SI.get(m.group(2), 1.0)


def _xy(node):
    return float(node[1]), float(node[2])


def _rot(x, y, deg):
    a = math.radians(deg)
    return x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)


def _prop(fp, name):
    for p in children(fp, 'property'):          # KiCad 7/8
        if len(p) > 2 and p[1] == name:
            return p[2]
    for t in children(fp, 'fp_text'):           # KiCad 6
        if len(t) > 2 and str(t[1]).lower() == name.lower():
            return t[2]
    return None


def load_kicad_pcb(path, cu_thickness=DEFAULT_CU_THICKNESS):
    path = Path(path)
    tree = parse(path.read_text(encoding='utf8'))
    if tree[0] != 'kicad_pcb':
        raise ValueError(f'{path} is not a .kicad_pcb file')
    nets = {}
    for n in children(tree, 'net'):
        nets[int(n[1])] = str(n[2]) if len(n) > 2 else ''

    def net_name(node):
        nn = child(node, 'net')
        if nn is None:
            return ''
        if len(nn) > 2:
            return str(nn[2])
        try:
            return nets.get(int(nn[1]), '')
        except ValueError:          # KiCad 9 can store the name only
            return str(nn[1])

    components, pads = [], []
    for fp in children(tree, 'footprint') + children(tree, 'module'):
        at = child(fp, 'at')
        fx, fy = _xy(at)
        frot = float(at[3]) if len(at) > 3 else 0.0
        layer = child(fp, 'layer')
        ref = _prop(fp, 'Reference') or '?'
        value = _prop(fp, 'Value') or ''
        comp = {'ref': ref, 'value': value, 'footprint': str(fp[1]),
                'x_mm': round(fx, 4), 'y_mm': round(fy, 4), 'rot_deg': frot,
                'side': 'bottom' if layer is not None and 'B.' in str(layer[1]) else 'top',
                'pads': {}}
        lcsc = _prop(fp, 'LCSC') or _prop(fp, 'LCSC Part') or _prop(fp, 'JLCPCB Part')
        if lcsc:
            comp['lcsc'] = str(lcsc)
        parsed = parse_value(value)
        if parsed is not None:
            comp['value_si'] = parsed
        for pad in children(fp, 'pad'):
            pat = child(pad, 'at')
            px, py = _xy(pat)
            dx, dy = _rot(px, py, -frot)       # KiCad rotates clockwise in screen coords
            name = str(pad[1])
            size = child(pad, 'size')
            n = net_name(pad)
            comp['pads'][name] = n
            pads.append({'ref': ref, 'pad': name, 'net': n, 'x': fx + dx, 'y': fy + dy,
                         'size': float(size[1]) if size else 1.0})
        components.append(comp)

    segments = []
    for seg in children(tree, 'segment'):
        (x1, y1), (x2, y2) = _xy(child(seg, 'start')), _xy(child(seg, 'end'))
        w = float(child(seg, 'width')[1])
        segments.append({'net': net_name(seg), 'layer': str(child(seg, 'layer')[1]),
                         'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2, 'w': w})
    vias = [{'net': net_name(v), 'x': _xy(child(v, 'at'))[0], 'y': _xy(child(v, 'at'))[1]}
            for v in children(tree, 'via')]

    outline = []
    for kind in ('gr_line', 'gr_rect', 'gr_arc', 'gr_poly'):
        for g in children(tree, kind):
            layer = child(g, 'layer')
            if layer is None or str(layer[1]) != 'Edge.Cuts':
                continue
            if kind == 'gr_poly':
                pts = child(g, 'pts')
                outline += [_xy(p) for p in children(pts, 'xy')]
            else:
                for key in ('start', 'end', 'mid'):
                    c = child(g, key)
                    if c is not None:
                        outline.append(_xy(c))
    if outline:
        xs, ys = [p[0] for p in outline], [p[1] for p in outline]
        board = {'x_min': min(xs), 'y_min': min(ys), 'width_mm': max(xs) - min(xs),
                 'height_mm': max(ys) - min(ys)}
    else:
        board = None
    thickness = child(child(tree, 'general') or ['general'], 'thickness')
    if board is not None:
        board['thickness_mm'] = float(thickness[1]) if thickness else 1.6

    netlist = {}
    for p in pads:
        if p['net']:
            netlist.setdefault(p['net'], []).append(f"{p['ref']}.{p['pad']}")
    for k in netlist:
        netlist[k].sort()

    return {
        'source': path.name,
        'board': board,
        'components': components,
        'nets': dict(sorted(netlist.items())),
        'copper': copper_summary(segments, vias, pads, cu_thickness),
        '_pads': pads,
        '_segments': segments,
    }


def copper_summary(segments, vias, pads, cu_thickness=DEFAULT_CU_THICKNESS):
    """Per net: total track length, resistance of the whole track set, and a pad-to-pad
    resistance table for the power-relevant pads (connectors, fuses, capacitors)."""
    by_net = {}
    for s in segments:
        if not s['net']:
            continue
        L = math.hypot(s['x2'] - s['x1'], s['y2'] - s['y1']) * 1e-3
        r = COPPER_RHO * L / (s['w'] * 1e-3 * cu_thickness)
        d = by_net.setdefault(s['net'], {'length_mm': 0.0, 'min_width_mm': 1e9, 'series_ohm': 0.0})
        d['length_mm'] += L * 1e3
        d['min_width_mm'] = min(d['min_width_mm'], s['w'])
        d['series_ohm'] += r
    for net, d in by_net.items():
        d['length_mm'] = round(d['length_mm'], 3)
        d['series_ohm'] = round(d['series_ohm'], 6)
        terminals = [p for p in pads if p['net'] == net]
        d['pad_to_pad_ohm'] = _pad_resistances(net, segments, vias, terminals, cu_thickness)
    return by_net


def _pad_resistances(net, segments, vias, terminals, cu_thickness):
    """Shortest-resistance path between every pair of pads on a net (tracks + vias)."""
    nodes = {}

    def node(x, y, tol=0.05):
        key = (round(x / tol), round(y / tol))
        return nodes.setdefault(key, len(nodes))
    graph = {}

    def edge(a, b, r):
        graph.setdefault(a, []).append((b, r))
        graph.setdefault(b, []).append((a, r))
    segs = [s for s in segments if s['net'] == net]
    for s in segs:
        L = math.hypot(s['x2'] - s['x1'], s['y2'] - s['y1']) * 1e-3
        edge(node(s['x1'], s['y1']), node(s['x2'], s['y2']),
             COPPER_RHO * L / (s['w'] * 1e-3 * cu_thickness))
    for v in vias:
        if v['net'] == net:
            node(v['x'], v['y'])        # same xy on both layers -> same node
    # attach pads to any track end inside the pad (through-hole pads join both layers)
    term_nodes = {}
    for p in terminals:
        pn = ('pad', p['ref'], p['pad'])
        nid = nodes.setdefault(pn, len(nodes))
        term_nodes[f"{p['ref']}.{p['pad']}"] = nid
        for s in segs:
            for (x, y) in ((s['x1'], s['y1']), (s['x2'], s['y2'])):
                if math.hypot(x - p['x'], y - p['y']) <= max(p['size'] / 2, 0.3):
                    edge(nid, node(x, y), 0.0)
    names = sorted(term_nodes)
    out = {}
    for i, a in enumerate(names):
        dist = _dijkstra(graph, term_nodes[a])
        for b in names[i + 1:]:
            r = dist.get(term_nodes[b])
            if r is not None:
                out[f'{a}-{b}'] = round(r, 6)
    return out


def _dijkstra(graph, src):
    dist = {src: 0.0}
    pq = [(0.0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, math.inf):
            continue
        for v, w in graph.get(u, []):
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                heapq.heappush(pq, (nd, v))
    return dist


def capacitance_between(pcb, net_a, net_b='GND'):
    """Sum of capacitor values connected between two nets (parallel caps)."""
    total, parts = 0.0, []
    for c in pcb['components']:
        if not c['ref'].upper().startswith('C') or 'value_si' not in c:
            continue
        nets = set(c['pads'].values())
        if nets == {net_a, net_b}:
            total += c['value_si']
            parts.append(c['ref'])
    return total, parts


def path_resistance(pcb, net, pad_a, pad_b):
    table = pcb['copper'].get(net, {}).get('pad_to_pad_ohm', {})
    key, rkey = f'{pad_a}-{pad_b}', f'{pad_b}-{pad_a}'
    return table.get(key, table.get(rkey))


def public_summary(pcb):
    """What gets written into the project file (drops raw geometry)."""
    return {k: v for k, v in pcb.items() if not k.startswith('_')}
