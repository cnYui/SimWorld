"""A board from the schematic, laid out by KiCad's own pcbnew.

New board: every footprint of the schematic, nets on every pad, a rectangular outline and JLCPCB
two-layer rules; parts are packed into the outline (largest first) unless the caller gives positions.
Existing board: like KiCad's "Update PCB from Schematic": parts that stay keep their place and
tracks, new parts are packed into free space, removed parts go, values/fields/nets follow the
schematic.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .. import eda
from . import kicad_libs as libs

_BUILD = r'''
import json, os, sys, pcbnew
spec = json.loads(open(sys.argv[1], encoding='utf-8').read())
out = sys.argv[2]
mm, ToMM = pcbnew.FromMM, pcbnew.ToMM
V = lambda x, y: pcbnew.VECTOR2I(mm(x), mm(y))
exists = os.path.exists(out)
board = pcbnew.LoadBoard(out) if exists else pcbnew.BOARD()
nets = {}
def net(name):
    if name not in nets:
        n = board.FindNet(name)
        if n is None:
            n = pcbnew.NETINFO_ITEM(board, name); board.Add(n)
        nets[name] = n
    return nets[name]

def field(f, name):
    for m in ('GetFieldByName', 'GetField'):
        if hasattr(f, m):
            try:
                r = getattr(f, m)(name)
            except TypeError:
                continue
            if r is not None:
                return r
    return None

x0, y0, W, H = spec['outline']
want = {c['ref']: c for c in spec['components']}
have = {f.GetReference(): f for f in board.GetFootprints()}
report = {'kept': [], 'added': [], 'removed': [], 'footprint_changed': [], 'grew_board': None}
for ref, f in list(have.items()):
    if ref not in want:
        board.Remove(f); report['removed'].append(ref); del have[ref]
    elif str(f.GetFPID().GetUniStringLibId()) != want[ref]['footprint']:
        board.Remove(f); report['footprint_changed'].append(ref); del have[ref]
new = []
for ref, c in want.items():
    if ref in have:
        f = have[ref]; report['kept'].append(ref)
    else:
        lib, name = c['footprint'].split(':', 1)
        f = pcbnew.FootprintLoad(os.path.join(spec['fp_dir'], lib + '.pretty'), name)
        if f is None:
            raise SystemExit(json.dumps({'error': f'footprint {c["footprint"]} could not be loaded'}))
        f.SetFPID(pcbnew.LIB_ID(lib, name))
        f.SetReference(ref)
        board.Add(f); new.append(f); report['added'].append(ref)
    f.SetValue(c['value'])
    for k, v in (c.get('fields') or {}).items():
        f.SetField(k, v)
        fld = field(f, k)
        if fld is not None: fld.SetVisible(False)
    pins = c.get('pins') or {}
    for p in f.Pads():
        n = pins.get(p.GetNumber())
        if n: p.SetNet(net(n))
        else: p.SetNetCode(0)

# outline (replaced only on a new board or when asked)
if not exists or spec.get('reset_outline'):
    for d in list(board.GetDrawings()):
        if d.GetLayer() == pcbnew.Edge_Cuts: board.Remove(d)
    r = pcbnew.PCB_SHAPE(board); r.SetShape(pcbnew.SHAPE_T_RECT)
    r.SetStart(V(x0, y0)); r.SetEnd(V(x0 + W, y0 + H)); r.SetLayer(pcbnew.Edge_Cuts); r.SetWidth(mm(0.1))
    board.Add(r)

def bb(f):                      # body only: the reference text would make an 0402 15 mm wide
    try:
        return f.GetBoundingBox(False)
    except TypeError:
        return f.GetBoundingBox()
def size(f):
    b = bb(f)
    return ToMM(b.GetWidth()), ToMM(b.GetHeight())

placed = spec.get('positions') or {}
occupied = []
for f in board.GetFootprints():
    if f in new: continue
    b = bb(f)
    occupied.append((ToMM(b.GetLeft()), ToMM(b.GetTop()), ToMM(b.GetRight()), ToMM(b.GetBottom())))
gap = spec.get('gap_mm', 0.8)
def free(x, y, w, h):
    if x < x0 + 0.5 or y < y0 + 0.5 or x + w > x0 + W - 0.5 or y + h > y0 + H - 0.5:
        return False
    return all(x + w + gap <= a or b2 + gap <= x or y + h + gap <= c or d + gap <= y for a, c, b2, d in
               [(o[0], o[1], o[2], o[3]) for o in occupied])
todo = []
for f in new:
    ref = f.GetReference()
    if ref in placed:
        p = placed[ref]
        f.SetPosition(V(p[0], p[1]))
        if len(p) > 2: f.SetOrientationDegrees(p[2])
        b = bb(f); occupied.append((ToMM(b.GetLeft()), ToMM(b.GetTop()), ToMM(b.GetRight()), ToMM(b.GetBottom())))
    else:
        todo.append(f)
todo.sort(key=lambda f: -(size(f)[0] * size(f)[1]))
step = 0.5
for f in todo:
    w, h = size(f)
    b = bb(f); pos = f.GetPosition()
    ox, oy = ToMM(pos.x) - ToMM(b.GetLeft()), ToMM(pos.y) - ToMM(b.GetTop())
    spot = None
    y = y0 + 0.5
    while spot is None and y + h <= y0 + H:
        x = x0 + 0.5
        while x + w <= x0 + W:
            if free(x, y, w, h):
                spot = (x, y); break
            x += step
        y += step
    if spot is None:          # no room: put it below the outline and say so
        spot = (x0, y0 + H + 2 + sum(o[3] - o[1] + gap for o in occupied if o[1] > y0 + H))
        report.setdefault('outside_outline', []).append(f.GetReference())
    f.SetPosition(V(spot[0] + ox, spot[1] + oy))
    occupied.append((spot[0], spot[1], spot[0] + w, spot[1] + h))

pcbnew.SaveBoard(out, board)
pos = {f.GetReference(): [round(ToMM(f.GetPosition().x), 3), round(ToMM(f.GetPosition().y), 3),
                          f.GetOrientationDegrees()] for f in board.GetFootprints()}
report['positions'] = pos
report['footprints'] = len(pos)
report['nets'] = len(nets)
print(json.dumps(report))
'''


def from_schematic(sch_path, pcb_path, width_mm=None, height_mm=None, positions=None, origin=(100.0, 100.0),
                   reset_outline=False):
    sch_path, pcb_path = Path(sch_path), Path(pcb_path)
    nl = eda.netlist(sch_path)
    comps, problems = [], []
    pin_net = {}
    for net, pins in nl['nets'].items():
        if net.startswith('unconnected-') or net.startswith('Net-(') and len(pins) < 2:
            continue
        for p in pins:
            ref, num = p.split('.', 1)
            pin_net.setdefault(ref, {})[num] = net.lstrip('/')
    area = 0.0
    for c in nl['components']:
        if c['ref'].startswith('#'):
            continue
        fp = c.get('footprint')
        if not fp:
            problems.append(f'{c["ref"]} ({c.get("value")}) has no footprint: set it with '
                            f'schematic_edit(ref={c["ref"]!r}, field="Footprint", value="Lib:Name")')
            continue
        try:
            pads = set(libs.footprint_pads(fp))
        except (KeyError, ValueError) as e:
            problems.append(f'{c["ref"]}: {e}')
            continue
        used = set(pin_net.get(c['ref'], {}))
        missing = sorted(used - pads)
        if missing:
            problems.append(f'{c["ref"]}: footprint {fp} has no pad(s) {missing} for connected symbol pins; '
                            f'pads are {sorted(pads)}')
        fields = {k: v for k, v in (c.get('fields') or {}).items() if k.lower() in ('lcsc', 'mpn', 'manufacturer')}
        comps.append({'ref': c['ref'], 'value': c.get('value') or '', 'footprint': fp, 'fields': fields,
                      'pins': pin_net.get(c['ref'], {})})
        area += _fp_area(fp)
    if problems:
        raise ValueError('cannot lay out the board yet:\n- ' + '\n- '.join(problems))
    if not width_mm or not height_mm:
        side = max(10.0, (area * 2.6) ** 0.5)
        width_mm = width_mm or round(side * 1.6, 1)
        height_mm = height_mm or round(side / 1.6 * 1.0 + 4, 1)
    spec = {'components': comps, 'outline': [origin[0], origin[1], float(width_mm), float(height_mm)],
            'fp_dir': str(libs.footprint_dir()), 'positions': positions or {}, 'reset_outline': reset_outline}
    pcb_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as f:
        json.dump(spec, f)
    try:
        res = eda._pcbnew(_BUILD, f.name, pcb_path)
    finally:
        Path(f.name).unlink(missing_ok=True)
    if 'error' in res:
        raise ValueError(res['error'])
    pcb_path.with_suffix('.kicad_dru').write_text(eda.JLC_RULES, encoding='utf-8')
    pro = pcb_path.with_suffix('.kicad_pro')
    if not pro.exists():
        pro.write_text(json.dumps({'meta': {'filename': pro.name, 'version': 3}}, indent=2), encoding='utf-8')
    res['outline_mm'] = {'x': origin[0], 'y': origin[1], 'width': float(width_mm), 'height': float(height_mm)}
    return res


def _fp_area(fid):
    """Courtyard (else pad) bounding-box area of a library footprint, mm2, for sizing the outline."""
    from ..sexpr import child, children, parse
    tree = parse(libs.footprint_file(fid).read_text(encoding='utf-8'))
    xs, ys = [], []
    for g in tree[1:]:
        if not isinstance(g, list) or not g:
            continue
        layer = child(g, 'layer')
        if g[0] in ('fp_line', 'fp_rect') and layer is not None and 'CrtYd' in str(layer[1]):
            for k in ('start', 'end'):
                c = child(g, k)
                if c is not None:
                    xs.append(float(c[1])); ys.append(float(c[2]))
    if not xs:
        for p in children(tree, 'pad'):
            at, sz = child(p, 'at'), child(p, 'size')
            if at is not None and sz is not None:
                xs += [float(at[1]) - float(sz[1]) / 2, float(at[1]) + float(sz[1]) / 2]
                ys += [float(at[2]) - float(sz[2]) / 2, float(at[2]) + float(sz[2]) / 2]
    if not xs:
        return 4.0
    return (max(xs) - min(xs) + 0.8) * (max(ys) - min(ys) + 0.8)


__all__ = ['from_schematic']
