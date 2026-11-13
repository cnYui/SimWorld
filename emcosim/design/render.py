"""Pictures of the schematic and the board for an AI (and the page) that cannot open KiCad.

Both are drawn straight from the KiCad files with Pillow, so they need nothing beyond the project's
own dependencies and always show the current file. The board view can mark DRC findings with
numbered red circles; the 3D board view uses `kicad-cli pcb render`.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..sexpr import child, children, parse

# -------------------------------------------------------------------------- shared helpers


def _font(px):
    for name in ('arial.ttf', 'DejaVuSans.ttf', 'msyh.ttc', 'Arial.ttf'):
        try:
            return ImageFont.truetype(name, max(8, int(px)))
        except OSError:
            continue
    return ImageFont.load_default()


def _xy(n):
    return float(n[1]), float(n[2])


def _rot(x, y, deg):
    a = math.radians(deg)
    return x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)


class _Canvas:
    def __init__(self, bbox, width_px, bg, margin_mm=2.0):
        x0, y0, x1, y1 = bbox
        self.x0, self.y0 = x0 - margin_mm, y0 - margin_mm
        w, h = (x1 - x0) + 2 * margin_mm, (y1 - y0) + 2 * margin_mm
        self.s = width_px / max(w, 1e-3)
        self.img = Image.new('RGB', (int(width_px), max(1, int(h * self.s))), bg)
        self.d = ImageDraw.Draw(self.img)

    def p(self, x, y):
        return ((x - self.x0) * self.s, (y - self.y0) * self.s)

    def w(self, mm):
        return max(1, int(round(mm * self.s)))


# -------------------------------------------------------------------------- board

COLORS = {'F.Cu': (200, 52, 52), 'B.Cu': (60, 110, 220), 'pad': (212, 175, 55), 'pad_b': (120, 150, 210),
          'edge': (230, 220, 60), 'via': (230, 230, 230), 'zone_F.Cu': (120, 40, 40), 'zone_B.Cu': (40, 60, 120),
          'text': (235, 235, 235), 'drc': (255, 40, 40), 'bg': (18, 22, 28), 'court': (90, 90, 90)}


def _pad_polygon(cx, cy, w, h, ang):
    pts = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
    return [(cx + x, cy + y) for x, y in (_rot(px, py, -ang) for px, py in pts)]


def board_png(pcb_path, out_png, width_px=1400, side='top', drc_items=None, focus=None):
    """2D board drawing. side: top | bottom | both. drc_items: board_drc items to mark.
    focus: a reference (e.g. 'U1') or [x0, y0, x1, y1] in mm to zoom on."""
    tree = parse(Path(pcb_path).read_text(encoding='utf-8'))
    edges, pads, tracks, vias, zones, refs = [], [], [], [], [], []
    for kind in ('gr_line', 'gr_rect', 'gr_arc', 'gr_circle', 'gr_poly'):
        for g in children(tree, kind):
            lay = child(g, 'layer')
            if lay is None or str(lay[1]) != 'Edge.Cuts':
                continue
            if kind == 'gr_line':
                edges.append(('line', [_xy(child(g, 'start')), _xy(child(g, 'end'))]))
            elif kind == 'gr_rect':
                (a, b), (c, d) = _xy(child(g, 'start')), _xy(child(g, 'end'))
                edges.append(('line', [(a, b), (c, b), (c, d), (a, d), (a, b)]))
            elif kind == 'gr_arc':
                edges.append(('line', [_xy(child(g, 'start')), _xy(child(g, 'mid')), _xy(child(g, 'end'))]))
            elif kind == 'gr_circle':
                (cx, cy), (ex, ey) = _xy(child(g, 'center')), _xy(child(g, 'end'))
                r = math.hypot(ex - cx, ey - cy)
                edges.append(('line', [(cx + r * math.cos(t / 24 * 2 * math.pi), cy + r * math.sin(t / 24 * 2 * math.pi))
                                       for t in range(25)]))
            else:
                pts = [_xy(p) for p in children(child(g, 'pts'), 'xy')]
                edges.append(('line', pts + pts[:1]))
    for fp in children(tree, 'footprint'):
        at = child(fp, 'at')
        fx, fy = _xy(at)
        frot = float(at[3]) if len(at) > 3 else 0.0
        bottom = 'B.' in str(child(fp, 'layer')[1])
        ref = next((p[2] for p in children(fp, 'property') if p[1] == 'Reference'), '?')
        xs, ys = [], []
        for pad in children(fp, 'pad'):
            pat, sz = child(pad, 'at'), child(pad, 'size')
            if pat is None or sz is None:
                continue
            px, py = _xy(pat)
            dx, dy = _rot(px, py, -frot)
            ang = float(pat[3]) if len(pat) > 3 else frot
            layers = ' '.join(str(x) for x in (child(pad, 'layers') or [])[1:])
            shape = str(pad[3]) if len(pad) > 3 else 'rect'
            netn = child(pad, 'net')
            pads.append({'x': fx + dx, 'y': fy + dy, 'w': float(sz[1]), 'h': float(sz[2]), 'ang': ang,
                         'shape': shape, 'th': str(pad[2]) == 'thru_hole',
                         'top': 'F.Cu' in layers or '*.Cu' in layers, 'bot': 'B.Cu' in layers or '*.Cu' in layers,
                         'net': str(netn[-1]) if netn is not None else ''})
            xs.append(fx + dx); ys.append(fy + dy)
        if xs:
            refs.append((ref, (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, bottom, (min(xs), min(ys), max(xs), max(ys))))
    for seg in children(tree, 'segment') + children(tree, 'arc'):
        lay = str(child(seg, 'layer')[1])
        pts = [_xy(child(seg, 'start'))] + ([_xy(child(seg, 'mid'))] if child(seg, 'mid') else []) + [_xy(child(seg, 'end'))]
        tracks.append((lay, pts, float(child(seg, 'width')[1])))
    for v in children(tree, 'via'):
        vias.append((_xy(child(v, 'at')), float(child(v, 'size')[1])))
    for z in children(tree, 'zone'):
        for fpoly in children(z, 'filled_polygon'):
            lay = str(child(fpoly, 'layer')[1])
            zones.append((lay, [_xy(p) for p in children(child(fpoly, 'pts'), 'xy')]))

    pts = [p for _, line in edges for p in line] + [(p['x'], p['y']) for p in pads]
    if not pts:
        raise ValueError('the board has no outline and no parts to draw')
    bbox = (min(x for x, _ in pts), min(y for _, y in pts), max(x for x, _ in pts), max(y for _, y in pts))
    if focus:
        if isinstance(focus, str):
            hit = [r for r in refs if r[0] == focus]
            if not hit:
                raise ValueError(f'no part {focus} on the board')
            b = hit[0][4]
            bbox = (b[0] - 4, b[1] - 4, b[2] + 4, b[3] + 4)
        else:
            bbox = tuple(float(v) for v in focus)
    cv = _Canvas(bbox, width_px, COLORS['bg'])
    d = cv.d
    show_top, show_bot = side in ('top', 'both'), side in ('bottom', 'both')
    order = (['B.Cu'] if show_bot else []) + (['F.Cu'] if show_top else [])
    for lay in order:
        for zl, poly in zones:
            if zl == lay and len(poly) > 2:
                d.polygon([cv.p(*q) for q in poly], fill=COLORS['zone_' + lay])
        for tl, line, w in tracks:
            if tl == lay:
                d.line([cv.p(*q) for q in line], fill=COLORS[lay], width=cv.w(w), joint='curve')
                for q in (line[0], line[-1]):
                    r = w / 2 * cv.s
                    x, y = cv.p(*q)
                    d.ellipse([x - r, y - r, x + r, y + r], fill=COLORS[lay])
    for p in pads:
        if not ((p['top'] and show_top) or (p['bot'] and show_bot)):
            continue
        col = COLORS['pad'] if (p['top'] and show_top) else COLORS['pad_b']
        if p['shape'] in ('circle', 'oval') and abs(p['w'] - p['h']) < 1e-6 or p['shape'] == 'circle':
            x, y = cv.p(p['x'], p['y']); r = p['w'] / 2 * cv.s
            d.ellipse([x - r, y - r, x + r, y + r], fill=col)
        else:
            d.polygon([cv.p(*q) for q in _pad_polygon(p['x'], p['y'], p['w'], p['h'], p['ang'])], fill=col)
        if p['th']:
            x, y = cv.p(p['x'], p['y']); r = min(p['w'], p['h']) * 0.25 * cv.s
            d.ellipse([x - r, y - r, x + r, y + r], fill=COLORS['bg'])
    for (x, y), size in vias:
        cx, cy = cv.p(x, y); r = size / 2 * cv.s
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=COLORS['via'])
        d.ellipse([cx - r / 2, cy - r / 2, cx + r / 2, cy + r / 2], fill=COLORS['bg'])
    for _, line in edges:
        d.line([cv.p(*q) for q in line], fill=COLORS['edge'], width=max(2, cv.w(0.15)))
    f = _font(max(10, min(22, 1.1 * cv.s)))
    for ref, x, y, bottom, _ in refs:
        if (bottom and show_bot) or (not bottom and show_top):
            d.text(cv.p(x, y), ref, fill=COLORS['text'], font=f, anchor='mm', stroke_width=2, stroke_fill=COLORS['bg'])
    legend = []
    for k, item in enumerate(drc_items or [], 1):
        where = [w for w in item.get('where', []) if w.get('pos_mm')]
        if not where:
            continue
        x, y = where[0]['pos_mm']
        cx, cy = cv.p(x, y); r = max(10, 0.8 * cv.s)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=COLORS['drc'], width=3)
        d.text((cx + r, cy - r), str(k), fill=COLORS['drc'], font=f, stroke_width=2, stroke_fill=COLORS['bg'])
        legend.append({'n': k, 'type': item.get('type'), 'severity': item.get('severity'),
                       'description': item.get('description'), 'pos_mm': [x, y]})
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cv.img.save(out_png)
    return {'png': str(out_png), 'size_px': list(cv.img.size), 'mm_per_px': round(1 / cv.s, 4),
            'origin_mm': [round(cv.x0, 3), round(cv.y0, 3)], 'drc_marked': legend}


def board_3d_png(pcb_path, out_png, side='top', width_px=1400, height_px=900, rotate=None):
    from ..eda import _run
    args = ['pcb', 'render', '-o', Path(out_png).resolve(), '--width', str(width_px), '--height', str(height_px),
            '--side', side, '--background', 'opaque', '--quality', 'basic']
    if rotate:
        args += ['--rotate', ','.join(str(v) for v in rotate)]
    _run(args + [Path(pcb_path).resolve()], cwd=Path(pcb_path).resolve().parent, timeout=300)
    return {'png': str(out_png), 'size_px': [width_px, height_px]}


# -------------------------------------------------------------------------- schematic

SCH = {'bg': (250, 249, 244), 'body': (132, 0, 0), 'fill': (255, 255, 194), 'pin': (132, 0, 0),
       'wire': (0, 132, 0), 'label': (0, 0, 0), 'glabel': (160, 80, 0), 'nc': (0, 0, 200), 'text': (0, 100, 100)}


def schematic_png(sch_path, out_png, width_px=1800, focus=None):
    """Schematic drawing: symbols (body, pins with numbers), wires, labels, no-connect flags."""
    tree = parse(Path(sch_path).read_text(encoding='utf-8'))
    libsyms = {s[1]: s for s in children(child(tree, 'lib_symbols') or ['lib_symbols'], 'symbol')}
    insts = children(tree, 'symbol')

    def tf(inst):
        at = child(inst, 'at')
        X, Y = _xy(at)
        rot = float(at[3]) if len(at) > 3 else 0.0
        mir = child(inst, 'mirror')

        def f(x, y):
            if mir is not None and str(mir[1]) == 'y':
                x = -x
            if mir is not None and str(mir[1]) == 'x':
                y = -y
            rx, ry = _rot(x, y, rot)
            return X + rx, Y - ry
        return f

    prims, texts, pts = [], [], []
    for inst in insts:
        lib = libsyms.get(child(inst, 'lib_id')[1])
        if lib is None:
            continue
        unit = int(child(inst, 'unit')[1]) if child(inst, 'unit') else 1
        f = tf(inst)
        for sub in children(lib, 'symbol'):
            tail = str(sub[1]).rsplit('_', 2)
            u = int(tail[-2]) if len(tail) == 3 and tail[-2].isdigit() else 0
            if u not in (0, unit):
                continue
            for g in sub[2:]:
                if not isinstance(g, list) or not g:
                    continue
                if g[0] == 'rectangle':
                    (a, b), (c, e) = _xy(child(g, 'start')), _xy(child(g, 'end'))
                    poly = [f(a, b), f(c, b), f(c, e), f(a, e)]
                    fill = child(g, 'fill')
                    prims.append(('poly', poly, fill is not None and 'background' in str(fill)))
                elif g[0] == 'polyline':
                    prims.append(('line', [f(*_xy(p)) for p in children(child(g, 'pts'), 'xy')], False))
                elif g[0] == 'circle':
                    cx, cy = f(*_xy(child(g, 'center')))
                    prims.append(('circle', (cx, cy, float(child(g, 'radius')[1])), False))
                elif g[0] == 'arc':
                    prims.append(('line', [f(*_xy(child(g, k))) for k in ('start', 'mid', 'end')], False))
                elif g[0] == 'pin':
                    at = child(g, 'at'); x, y = _xy(at); ang = float(at[3]) if len(at) > 3 else 0
                    ln = float(child(g, 'length')[1]) if child(g, 'length') else 2.54
                    ex, ey = x + ln * math.cos(math.radians(ang)), y + ln * math.sin(math.radians(ang))
                    a, b = f(x, y), f(ex, ey)
                    prims.append(('pin', [a, b], False))
                    num = child(g, 'number'); name = child(g, 'name')
                    texts.append(('pnum', ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), str(num[1]) if num else ''))
                    if name and str(name[1]) not in ('', '~'):
                        texts.append(('pname', b, str(name[1]).replace('{', '').replace('}', ''), a))
        for pr in children(inst, 'property'):
            if pr[1] in ('Reference', 'Value') and not any(
                    isinstance(c, list) and c and c[0] == 'hide' and str(c[1]) == 'yes' for c in pr[2:]) and \
                    not any(isinstance(c, list) and c[0] == 'effects' and child(c, 'hide') for c in pr[2:]):
                texts.append(('prop', _xy(child(pr, 'at')), str(pr[2])))
    for w in children(tree, 'wire'):
        prims.append(('wire', [_xy(p) for p in children(child(w, 'pts'), 'xy')], False))
    for kind in ('label', 'global_label', 'hierarchical_label'):
        for lb in children(tree, kind):
            at = child(lb, 'at')
            texts.append((kind, _xy(at), str(lb[1]), float(at[3]) if len(at) > 3 else 0))
    for nc in children(tree, 'no_connect'):
        prims.append(('nc', _xy(child(nc, 'at')), False))
    for j in children(tree, 'junction'):
        prims.append(('junction', _xy(child(j, 'at')), False))

    for p in prims:
        if p[0] in ('poly', 'line', 'pin', 'wire'):
            pts += list(p[1])
        elif p[0] == 'circle':
            pts.append(p[1][:2])
        else:
            pts.append(p[1])
    pts += [t[1] for t in texts]
    if not pts:
        raise ValueError('the schematic is empty')
    bbox = (min(x for x, _ in pts), min(y for _, y in pts), max(x for x, _ in pts), max(y for _, y in pts))
    if focus:
        if isinstance(focus, str):
            inst = next((i for i in insts if any(pr[1] == 'Reference' and pr[2] == focus
                                                 for pr in children(i, 'property'))), None)
            if inst is None:
                raise ValueError(f'no symbol {focus} in the schematic')
            X, Y = _xy(child(inst, 'at'))
            bbox = (X - 40, Y - 40, X + 40, Y + 40)
        else:
            bbox = tuple(float(v) for v in focus)
    cv = _Canvas(bbox, width_px, SCH['bg'], margin_mm=8)
    d = cv.d
    lw = max(1, cv.w(0.18))
    for kind, data, filled in prims:
        if kind == 'poly':
            d.polygon([cv.p(*q) for q in data], fill=SCH['fill'] if filled else None, outline=SCH['body'])
            d.line([cv.p(*q) for q in data + data[:1]], fill=SCH['body'], width=lw)
        elif kind == 'line':
            d.line([cv.p(*q) for q in data], fill=SCH['body'], width=lw)
        elif kind == 'circle':
            x, y = cv.p(data[0], data[1]); r = data[2] * cv.s
            d.ellipse([x - r, y - r, x + r, y + r], outline=SCH['body'], width=lw)
        elif kind == 'pin':
            d.line([cv.p(*q) for q in data], fill=SCH['pin'], width=lw)
        elif kind == 'wire':
            d.line([cv.p(*q) for q in data], fill=SCH['wire'], width=lw)
        elif kind == 'nc':
            x, y = cv.p(*data); r = 0.6 * cv.s
            d.line([x - r, y - r, x + r, y + r], fill=SCH['nc'], width=lw)
            d.line([x - r, y + r, x + r, y - r], fill=SCH['nc'], width=lw)
        elif kind == 'junction':
            x, y = cv.p(*data); r = 0.45 * cv.s
            d.ellipse([x - r, y - r, x + r, y + r], fill=SCH['wire'])
    small, normal = _font(1.0 * cv.s), _font(1.27 * cv.s)
    for t in texts:
        kind, (x, y), s = t[0], t[1], t[2]
        if kind == 'pnum':
            d.text(cv.p(x, y - 0.4), s, fill=SCH['pin'], font=small, anchor='mb')
        elif kind == 'pname':
            a = t[3]
            dx = 1 if t[1][0] >= a[0] else -1
            if abs(t[1][0] - a[0]) < 1e-6:
                d.text(cv.p(x, y + (0.6 if t[1][1] > a[1] else -0.6)), s, fill=SCH['text'], font=small,
                       anchor='mt' if t[1][1] > a[1] else 'mb')
            else:
                d.text(cv.p(x + 0.6 * dx, y), s, fill=SCH['text'], font=small, anchor='lm' if dx > 0 else 'rm')
        elif kind == 'prop':
            d.text(cv.p(x, y), s, fill=SCH['body'], font=normal, anchor='lm')
        else:
            ang = t[3]
            col = SCH['glabel'] if kind == 'global_label' else SCH['label']
            anchor = {0: 'lm', 180: 'rm', 90: 'ld', 270: 'lt'}.get(int(ang) % 360, 'lm')
            off = {0: (0.4, 0), 180: (-0.4, 0), 90: (0, -0.4), 270: (0, 0.4)}.get(int(ang) % 360, (0, 0))
            d.text(cv.p(x + off[0], y + off[1]), s, fill=col, font=small, anchor=anchor)
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cv.img.save(out_png)
    return {'png': str(out_png), 'size_px': list(cv.img.size), 'mm_per_px': round(1 / cv.s, 4)}


__all__ = ['board_png', 'board_3d_png', 'schematic_png']
