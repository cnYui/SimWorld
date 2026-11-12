"""Stop-gap converter: EasyEDA Pro (.epcb + Specctra .dsn + BOM .csv) -> .kicad_pcb.

The intended route is EasyEDA Pro's own "Export -> KiCad". This converter exists
so that a board drawn in EasyEDA Pro can be brought in when that export is not
available. It keeps what the co-simulator uses: component placement and values,
pad nets and positions, tracks (layer/width), the board outline and the
non-plated holes. Footprint graphics, pad shapes, zones and silkscreen are NOT
converted; pads are written as round through-hole pads.
"""
from __future__ import annotations

import csv
import json
import re
import uuid
from pathlib import Path

from ..sexpr import Sym, children, dumps, parse

MIL = 0.0254


def _records(epcb_text):
    for line in epcb_text.splitlines():
        line = line.strip().rstrip('|')
        if '||' not in line:
            continue
        head, _, body = line.partition('||')
        try:
            yield json.loads(head), json.loads(body) if body else {}
        except json.JSONDecodeError:
            continue


def _dsn_pins(dsn_text):
    tree = parse(dsn_text)
    pins = {}
    lib = [c for c in tree[1:] if isinstance(c, list) and c and c[0] == 'library']
    for image in children(lib[0], 'image') if lib else []:
        for pin in children(image, 'pin'):
            # (pin <padstack> <pin_id> x y) or (pin <padstack> (rotate a) <pin_id> x y)
            atoms = [a for a in pin[1:] if not isinstance(a, list)]
            if len(atoms) >= 4:
                pins[str(atoms[1])] = (float(atoms[2]), float(atoms[3]))
    return pins


def _bom_values(bom_path):
    values = {}
    if not bom_path:
        return values
    text = Path(bom_path).read_text(encoding='utf-8-sig')
    for row in csv.DictReader(text.splitlines()):
        value = (row.get('Value') or '').strip() or (row.get('Comment') or '').strip()
        for ref in re.split(r'[,\s]+', row.get('Designator', '')):
            if ref:
                values[ref] = {'value': value, 'footprint': row.get('Footprint', ''),
                               'mpn': row.get('Manufacturer Part', '')}
    return values


def convert(epcb_path, dsn_path, out_path, bom_path=None):
    epcb_text = Path(epcb_path).read_text(encoding='utf8')
    pins = _dsn_pins(Path(dsn_path).read_text(encoding='utf8'))
    bom = _bom_values(bom_path)

    comps, designators, pad_nets, lines, holes, outline = {}, {}, {}, [], [], []
    for head, body in _records(epcb_text):
        t = head.get('type')
        if t == 'COMPONENT':
            comps[head['id']] = body
        elif t == 'ATTR' and body.get('key') == 'Designator':
            designators[body.get('parentId')] = body.get('value')
        elif t == 'PAD_NET':
            _, comp, num, elem = json.loads(head['id'])
            pad_nets[(comp, num)] = (elem, body.get('padNet', ''))
        elif t == 'LINE' and body.get('layerId') in (1, 2):
            lines.append(body)
        elif t == 'PAD' and body.get('plated') is False and body.get('hole'):
            holes.append(body)
        elif t == 'POLY' and body.get('layerId') == 11:
            path = [p for p in body.get('path', []) if isinstance(p, (int, float))]
            outline = list(zip(path[0::2], path[1::2]))

    net_names = sorted({n for _, n in pad_nets.values() if n} | {l.get('netName') for l in lines if l.get('netName')})
    net_id = {n: i + 1 for i, n in enumerate(net_names)}

    def xy(x, y):
        return [round(x * MIL, 4), round(-y * MIL, 4)]   # EasyEDA y is up, KiCad y is down

    def net(n):
        return [Sym('net'), net_id[n], n] if n else [Sym('net'), 0, '']

    pcb = [Sym('kicad_pcb'), [Sym('version'), 20240108], [Sym('generator'), 'emcosim_easyeda_pro'],
           [Sym('general'), [Sym('thickness'), 1.6]],
           [Sym('layers'), [0, 'F.Cu', Sym('signal')], [31, 'B.Cu', Sym('signal')],
            [44, 'Edge.Cuts', Sym('user')]],
           [Sym('net'), 0, '']] + [[Sym('net'), i, n] for n, i in net_id.items()]

    for cid, body in comps.items():
        ref = designators.get(cid, cid[:6])
        cx, cy = body['x'], body['y']
        info = bom.get(ref, {})
        device = json.loads(body.get('attrs', {}).get('DeviceName', '{}') or '{}').get('name', '')
        fp = [Sym('footprint'), f"easyeda:{info.get('footprint') or device or ref}",
              [Sym('layer'), 'B.Cu' if body.get('layerId') == 2 else 'F.Cu'],
              [Sym('uuid'), str(uuid.uuid5(uuid.NAMESPACE_URL, cid))],
              [Sym('at')] + xy(cx, cy),
              [Sym('property'), 'Reference', ref, [Sym('at'), 0, 0]],
              [Sym('property'), 'Value', info.get('value') or device, [Sym('at'), 0, 0]]]
        if info.get('mpn'):
            fp.append([Sym('property'), 'MPN', info['mpn'], [Sym('at'), 0, 0]])
        for (comp, num), (elem, n) in sorted(pad_nets.items(), key=lambda kv: kv[0][1]):
            if comp != cid:
                continue
            # The Specctra export drops the first 'e' of "<component id><pad element id>".
            full = cid + elem
            pin = pins.get(full.replace('e', '', 1)) or pins.get(full) or pins.get(cid + elem.lstrip('e'))
            if pin is None:
                continue
            px, py = xy(pin[0] - cx, pin[1] - cy)
            fp.append([Sym('pad'), num, Sym('thru_hole'), Sym('circle'), [Sym('at'), px, py],
                       [Sym('size'), 1.8, 1.8], [Sym('drill'), 1.0], [Sym('layers'), '*.Cu', '*.Mask'],
                       net(n)])
        pcb.append(fp)

    for i, h in enumerate(holes):
        d = round(h['hole']['width'] * MIL, 3)
        pcb.append([Sym('footprint'), 'easyeda:NPTH', [Sym('layer'), 'F.Cu'],
                    [Sym('at')] + xy(h['centerX'], h['centerY']),
                    [Sym('property'), 'Reference', f'H{i + 1}', [Sym('at'), 0, 0]],
                    [Sym('property'), 'Value', 'MountingHole', [Sym('at'), 0, 0]],
                    [Sym('pad'), '', Sym('np_thru_hole'), Sym('circle'), [Sym('at'), 0, 0],
                     [Sym('size'), d, d], [Sym('drill'), d], [Sym('layers'), '*.Cu', '*.Mask']]])

    for l in lines:
        pcb.append([Sym('segment'), [Sym('start')] + xy(l['startX'], l['startY']),
                    [Sym('end')] + xy(l['endX'], l['endY']), [Sym('width'), round(l['width'] * MIL, 4)],
                    [Sym('layer'), 'F.Cu' if l['layerId'] == 1 else 'B.Cu'], net(l.get('netName', ''))])

    for a, b in zip(outline, outline[1:]):
        if a != b:
            pcb.append([Sym('gr_line'), [Sym('start')] + xy(*a), [Sym('end')] + xy(*b),
                        [Sym('stroke'), [Sym('width'), 0.1], [Sym('type'), Sym('solid')]],
                        [Sym('layer'), 'Edge.Cuts']])
    Path(out_path).write_text(dumps(pcb) + '\n', encoding='utf8')
    return {'components': len(comps), 'tracks': len(lines), 'nets': len(net_id), 'holes': len(holes)}
