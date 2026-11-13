"""Write a KiCad schematic from a component list and a net list.

Every symbol comes from KiCad's own libraries, so pin numbers, names and electrical types are the real
ones and KiCad's ERC can check the result. Nets are drawn the way a tidy hand-made schematic often is:
a short wire from each pin to a net label, no long wires to route. Pins that are in no net get a
no-connect flag (and are listed, so a forgotten power pin does not go unnoticed); nets that carry
power-input pins but no power source get a PWR_FLAG, as KiCad expects for supplies that come from a
connector or a regulator's passive pin.
"""
from __future__ import annotations

import re
import uuid as _uuid
from pathlib import Path

from ..sexpr import Sym, dumps
from . import kicad_libs as libs

GRID = 2.54
STUB = 2.54                     # wire from a pin to its label
FONT = [Sym('effects'), [Sym('font'), [Sym('size'), 1.27, 1.27]]]
PAPERS = [('A4', 297, 210), ('A3', 420, 297), ('A2', 594, 420), ('A1', 841, 594), ('A0', 1189, 841)]
SCH_VERSION = 20250114          # KiCad 9 format; KiCad 10 reads it


def _uuid4():
    return str(_uuid.uuid4())


def _snap(v, g=GRID):
    return round(v / g) * g


def _effects(hide=False, justify=None):
    e = [Sym('effects'), [Sym('font'), [Sym('size'), 1.27, 1.27]]]
    if justify:
        e.append([Sym('justify')] + [Sym(j) for j in justify.split()])
    out = [e]
    if hide:
        out.insert(0, [Sym('hide'), Sym('yes')])
    return out


def _prop(name, value, x, y, hide=False, justify=None, angle=0):
    return [Sym('property'), name, str(value), [Sym('at'), round(x, 4), round(y, 4), angle]] + \
        _effects(hide, justify)


def _glabel(net, x, y, angle):
    """Global label, so the net keeps its plain name in the netlist, the board and the simulation."""
    just = 'left' if angle in (0, 90) else 'right'
    return [Sym('global_label'), net, [Sym('shape'), Sym('passive')], [Sym('at'), round(x, 4), round(y, 4), angle],
            [Sym('fields_autoplaced'), Sym('yes')]] + _effects(justify=just) + [
        [Sym('uuid'), _uuid4()],
        [Sym('property'), 'Intersheetrefs', '${INTERSHEET_REFS}', [Sym('at'), round(x, 4), round(y, 4), 0],
         [Sym('effects'), [Sym('font'), [Sym('size'), 1.27, 1.27]], [Sym('hide'), Sym('yes')]]]]


def lib_tables(directory, symbols, footprints):
    """sym-lib-table / fp-lib-table next to the schematic, pointing at KiCad's installed libraries, so
    kicad-cli finds them even when KiCad was never opened on this machine."""
    directory = Path(directory)
    sl = sorted({s.split(':', 1)[0] for s in symbols if ':' in s})
    fl = sorted({f.split(':', 1)[0] for f in footprints if f and ':' in f})
    sd, fd = libs.symbol_dir(), libs.footprint_dir()
    rows = [f'  (lib (name "{n}")(type "KiCad")(uri "{(sd / (n + ".kicad_sym")).as_posix()}")(options "")(descr ""))'
            for n in sl]
    nl = chr(10)
    (directory / 'sym-lib-table').write_text(f'(sym_lib_table{nl}  (version 7){nl}' + nl.join(rows) + f'{nl}){nl}',
                                             encoding='utf-8')
    rows = [f'  (lib (name "{n}")(type "KiCad")(uri "{(fd / (n + ".pretty")).as_posix()}")(options "")(descr ""))'
            for n in fl]
    (directory / 'fp-lib-table').write_text(f'(fp_lib_table{nl}  (version 7){nl}' + nl.join(rows) + f'{nl}){nl}',
                                            encoding='utf-8')


def parse_pin_ref(s):
    m = re.fullmatch(r'\s*([A-Za-z#_][A-Za-z0-9#_]*?)[.\-:](\S+)\s*', str(s))
    if not m:
        raise ValueError(f'pin {s!r}: write it as REF.PIN, e.g. U1.5 or C3.1')
    return m.group(1), m.group(2)


def _bbox(sym_pins, sym):
    xs, ys = [0.0], [0.0]
    for p in sym_pins:
        xs.append(p['x']); ys.append(p['y'])
    for sub in sym[2:]:
        if isinstance(sub, list) and sub and sub[0] == 'symbol':
            for g in sub[2:]:
                if isinstance(g, list) and g and g[0] in ('rectangle',):
                    for k in ('start', 'end'):
                        for c in g[1:]:
                            if isinstance(c, list) and c and c[0] == k:
                                xs.append(float(c[1])); ys.append(float(c[2]))
    return min(xs), min(ys), max(xs), max(ys)


def _outward(angle):
    """Unit vector from a pin's connection point away from the symbol, in schematic coords (y down)."""
    a = int(round(angle)) % 360
    return {0: (-1, 0), 180: (1, 0), 90: (0, 1), 270: (0, -1)}.get(a, (-1, 0))


def build(components, nets, project_name='project', title=None):
    """components: [{ref, symbol: 'Lib:Name', value?, footprint?, lcsc?, fields?}]
    nets: {name: ['U1.5', 'C1.1', ...]}. Returns (schematic text, report)."""
    if not components:
        raise ValueError('components is empty')
    refs = {}
    for c in components:
        ref = c.get('ref')
        if not ref or not re.fullmatch(r'[A-Za-z#][A-Za-z0-9_#]*\d+', ref):
            raise ValueError(f'component {c!r} needs a reference like R1, U2 or SW3')
        if ref in refs:
            raise ValueError(f'reference {ref} is used twice')
        if not c.get('symbol'):
            raise ValueError(f'{ref}: give symbol as Library:Name (symbol_search finds one)')
        refs[ref] = c

    lib_syms, placed = {}, {}
    for ref, c in refs.items():
        if c['symbol'] not in lib_syms:
            lib_syms[c['symbol']] = libs.load_symbol(c['symbol'])
        sym = lib_syms[c['symbol']]
        ps = [p for p in libs.pins(sym) if p['unit'] in (0, 1)]
        placed[ref] = {'sym': sym, 'pins': ps}

    # which pin is on which net, with checks the AI can act on
    pin_net, problems = {}, []
    for net, members in (nets or {}).items():
        if not re.fullmatch(r'[^\s{}"]+', str(net)):
            raise ValueError(f'net name {net!r}: no spaces or quotes')
        for m in members:
            ref, num = parse_pin_ref(m)
            if ref not in placed:
                problems.append(f'{net}: {m} names a component that is not in components')
                continue
            nums = {p['number'] for p in placed[ref]['pins']}
            if num not in nums:
                by_name = [p['number'] for p in placed[ref]['pins'] if p['name'] == num]
                if len(by_name) == 1:
                    num = by_name[0]
                else:
                    problems.append(f'{net}: {ref} has no pin {num!r} (pins: '
                                    f'{", ".join(p["number"] + ("=" + p["name"] if p["name"] else "") for p in placed[ref]["pins"])})')
                    continue
            if (ref, num) in pin_net and pin_net[(ref, num)] != net:
                problems.append(f'{ref}.{num} is in two nets: {pin_net[(ref, num)]} and {net}')
            pin_net[(ref, num)] = net
    if problems:
        raise ValueError('net list problems:\n- ' + '\n- '.join(problems))

    # power flags: nets with power inputs but nothing that drives them
    pwr_nets = []
    for net in nets:
        types = [p['type'] for (r, n), nn in pin_net.items() if nn == net
                 for p in placed[r]['pins'] if p['number'] == n]
        if 'power_in' in types and 'power_out' not in types:
            pwr_nets.append(net)

    # layout: rows of symbols, widest first, label room around each
    order = sorted(placed, key=lambda r: (re.sub(r'\d+', '', r) not in ('U', 'J'), r))
    boxes = {}
    for ref in order:
        x0, y0, x1, y1 = _bbox(placed[ref]['pins'], placed[ref]['sym'])
        boxes[ref] = (x0, y0, x1, y1)
    margin_x, label_room = 25.4, 17.78
    x, y, row_h, max_w = margin_x, 30.48, 0.0, 0.0
    area = sum((b[2] - b[0] + 2 * label_room) * (b[3] - b[1] + 2 * label_room) for b in boxes.values())
    widest = max(b[2] - b[0] + 2 * label_room for b in boxes.values())
    page_w = min(PAPERS[-1][1], max(PAPERS[0][1] - 2 * margin_x, widest, (area * 1.6) ** 0.5) + 2 * margin_x)
    pos = {}
    for ref in order:
        x0, y0, x1, y1 = boxes[ref]
        w, h = x1 - x0 + 2 * label_room, y1 - y0 + 2 * label_room
        if x + w > page_w - margin_x and x > margin_x:
            x, y = margin_x, y + row_h + 7.62
            row_h = 0.0
        ox, oy = _snap(x + label_room - x0), _snap(y + label_room + y1)     # symbol y is up
        pos[ref] = (ox, oy)
        x += w
        row_h = max(row_h, h)
        max_w = max(max_w, x)
    need_w, need_h = max_w + margin_x, y + row_h + 30
    paper = next(((n, w, h) for n, w, h in PAPERS if w >= need_w and h >= need_h), PAPERS[-1])

    root_uuid = _uuid4()
    items = []
    for ref in order:
        c, sym = refs[ref], placed[ref]['sym']
        ox, oy = pos[ref]
        x0, y0, x1, y1 = boxes[ref]
        fp = c.get('footprint') or libs.prop(sym, 'Footprint') or ''
        value = c.get('value') or libs.prop(sym, 'Value') or c['symbol'].split(':')[1]
        node = [Sym('symbol'), [Sym('lib_id'), c['symbol']], [Sym('at'), ox, oy, 0], [Sym('unit'), 1],
                [Sym('exclude_from_sim'), Sym('no')], [Sym('in_bom'), Sym('yes')], [Sym('on_board'), Sym('yes')],
                [Sym('dnp'), Sym('no')], [Sym('uuid'), _uuid4()],
                _prop('Reference', ref, ox + x1 + 1.27, oy - y1 - 1.27, justify='left'),
                _prop('Value', value, ox + x1 + 1.27, oy - y0 + 2.54, justify='left'),
                _prop('Footprint', fp, ox, oy, hide=True),
                _prop('Datasheet', libs.prop(sym, 'Datasheet') or '', ox, oy, hide=True)]
        if c.get('lcsc'):
            node.append(_prop('LCSC', c['lcsc'], ox, oy, hide=True))
        for k, v in (c.get('fields') or {}).items():
            node.append(_prop(k, v, ox, oy, hide=True))
        for p in placed[ref]['pins']:
            node.append([Sym('pin'), p['number'], [Sym('uuid'), _uuid4()]])
        node.append([Sym('instances'), [Sym('project'), project_name,
                                        [Sym('path'), f'/{root_uuid}', [Sym('reference'), ref], [Sym('unit'), 1]]]])
        items.append(node)
        for p in placed[ref]['pins']:
            if p['hidden']:
                continue
            px, py = ox + p['x'], oy - p['y']
            net = pin_net.get((ref, p['number']))
            if net is None:
                if p['type'] != 'no_connect':
                    items.append([Sym('no_connect'), [Sym('at'), round(px, 4), round(py, 4)], [Sym('uuid'), _uuid4()]])
                continue
            dx, dy = _outward(p['angle'])
            lx, ly = px + dx * STUB, py + dy * STUB
            items.append([Sym('wire'), [Sym('pts'), [Sym('xy'), round(px, 4), round(py, 4)],
                                        [Sym('xy'), round(lx, 4), round(ly, 4)]],
                          [Sym('stroke'), [Sym('width'), 0], [Sym('type'), Sym('default')]], [Sym('uuid'), _uuid4()]])
            angle = {(-1, 0): 180, (1, 0): 0, (0, -1): 90, (0, 1): 270}[(dx, dy)]
            items.append(_glabel(net, lx, ly, angle))

    # PWR_FLAGs in a row at the bottom
    flag_sym = libs.load_symbol('power:PWR_FLAG') if pwr_nets else None
    fx, fy = 25.4, _snap(need_h - 12.7)
    for k, net in enumerate(pwr_nets):
        ref = f'#FLG{k + 1:02d}'
        node = [Sym('symbol'), [Sym('lib_id'), 'power:PWR_FLAG'], [Sym('at'), fx, fy, 0], [Sym('unit'), 1],
                [Sym('exclude_from_sim'), Sym('no')], [Sym('in_bom'), Sym('yes')], [Sym('on_board'), Sym('yes')],
                [Sym('dnp'), Sym('no')], [Sym('uuid'), _uuid4()],
                _prop('Reference', ref, fx, fy - 3.81, hide=True), _prop('Value', 'PWR_FLAG', fx, fy - 3.81),
                _prop('Footprint', '', fx, fy, hide=True), _prop('Datasheet', '', fx, fy, hide=True),
                [Sym('pin'), '1', [Sym('uuid'), _uuid4()]],
                [Sym('instances'), [Sym('project'), project_name,
                                    [Sym('path'), f'/{root_uuid}', [Sym('reference'), ref], [Sym('unit'), 1]]]]]
        items.append(node)
        items.append([Sym('wire'), [Sym('pts'), [Sym('xy'), fx, fy], [Sym('xy'), fx, fy + STUB]],
                      [Sym('stroke'), [Sym('width'), 0], [Sym('type'), Sym('default')]], [Sym('uuid'), _uuid4()]])
        items.append(_glabel(net, fx, fy + STUB, 270))
        fx += 15.24
    if flag_sym is not None:
        lib_syms['power:PWR_FLAG'] = flag_sym

    tree = [Sym('kicad_sch'), [Sym('version'), SCH_VERSION], [Sym('generator'), 'emcosim'],
            [Sym('generator_version'), '0.3'], [Sym('uuid'), root_uuid], [Sym('paper'), paper[0]],
            [Sym('title_block'), [Sym('title'), title or project_name], [Sym('comment'), 1,
                                                                      'Generated by SimWorld (emcosim) from a net list']],
            [Sym('lib_symbols')] + list(lib_syms.values())] + items + \
        [[Sym('sheet_instances'), [Sym('path'), '/', [Sym('page'), '1']]]]

    unused_power = [f'{r}.{p["number"]} {p["name"]}' for r in order for p in placed[r]['pins']
                    if (r, p['number']) not in pin_net and p['type'] in ('power_in', 'power_out')]
    resolved = {}
    for (r, n), net in pin_net.items():
        resolved.setdefault(net, []).append(f'{r}.{n}')
    report = {'_nets': {k: sorted(v) for k, v in resolved.items()},
              'components': len(refs), 'nets': len(nets or {}), 'paper': paper[0],
              'pwr_flags': pwr_nets, 'no_connect_pins': sum(1 for r in order for p in placed[r]['pins']
                                                            if (r, p['number']) not in pin_net),
              'unconnected_power_pins': unused_power,
              'footprints': {r: (refs[r].get('footprint') or libs.prop(placed[r]['sym'], 'Footprint') or None)
                             for r in order}}
    return dumps(tree) + '\n', report


def write(path, components, nets, project_name='project', title=None):
    text, report = build(components, nets, project_name, title)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    lib_tables(path.parent, [c['symbol'] for c in components] + ['power:PWR_FLAG'],
               [f for f in report['footprints'].values() if f])
    pro = path.with_suffix('.kicad_pro')          # KiCad reads the project's lib tables only with one
    if not pro.exists():
        import json
        pro.write_text(json.dumps({'meta': {'filename': pro.name, 'version': 3}}, indent=2), encoding='utf-8')
    return report


__all__ = ['build', 'write', 'parse_pin_ref']
