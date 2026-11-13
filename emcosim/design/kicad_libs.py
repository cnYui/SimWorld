"""KiCad's installed symbol and footprint libraries: find them, search them, read a symbol's pins."""
from __future__ import annotations

import functools
import glob
import os
import re

from pathlib import Path

from ..sexpr import Sym, child, children, parse


def share_dir():
    """KiCad's share/kicad directory (symbols/, footprints/), from kicad-cli's location."""
    for var in ('KICAD10_SYMBOL_DIR', 'KICAD9_SYMBOL_DIR', 'KICAD_SYMBOL_DIR'):
        if os.environ.get(var):
            return Path(os.environ[var]).parent
    from ..eda import find_cli
    cli = find_cli()
    cands = []
    if cli:
        b = Path(cli).resolve().parent
        cands += [b.parent / 'share' / 'kicad', b.parent / 'SharedSupport', b.parent.parent / 'SharedSupport']
    cands += [Path('/usr/share/kicad'), Path('/usr/local/share/kicad'),
              Path('/Applications/KiCad/KiCad.app/Contents/SharedSupport')]
    for c in cands:
        if (c / 'symbols').is_dir():
            return c
    raise FileNotFoundError('KiCad symbol libraries not found; install KiCad 9 or 10 or set KICAD_SYMBOL_DIR')


def symbol_dir():
    return share_dir() / 'symbols'


def footprint_dir():
    for var in ('KICAD10_FOOTPRINT_DIR', 'KICAD9_FOOTPRINT_DIR', 'KICAD_FOOTPRINT_DIR'):
        if os.environ.get(var):
            return Path(os.environ[var])
    return share_dir() / 'footprints'


@functools.lru_cache(maxsize=64)
def _library(lib):
    f = symbol_dir() / f'{lib}.kicad_sym'
    if not f.exists():
        d = symbol_dir() / f'{lib}.kicad_symdir'             # KiCad 10 can split a library per symbol
        if d.is_dir():
            return {s[1]: s for f2 in sorted(d.glob('*.kicad_sym'))
                    for s in children(parse(f2.read_text(encoding='utf-8')), 'symbol')}
        raise KeyError(f'no symbol library {lib!r} in {symbol_dir()}')
    tree = parse(f.read_text(encoding='utf-8'))
    return {s[1]: s for s in children(tree, 'symbol')}


def _split(lib_id):
    if ':' not in lib_id:
        raise ValueError(f'symbol {lib_id!r}: use Library:Name, e.g. Device:R (find one with symbol_search)')
    return lib_id.split(':', 1)


def load_symbol(lib_id):
    """The symbol node, with `extends` resolved, named lib_id (as schematics embed it)."""
    lib, name = _split(lib_id)
    syms = _library(lib)
    if name not in syms:
        close = [n for n in syms if name.lower() in n.lower()][:8]
        raise KeyError(f'no symbol {name!r} in library {lib}' + (f'; similar: {close}' if close else ''))
    node = syms[name]
    ext = child(node, 'extends')
    if ext is not None:
        parent = load_symbol(f'{lib}:{ext[1]}')
        props = {p[1]: p for p in children(node, 'property')}
        base_name = str(ext[1])
        out = [Sym('symbol'), lib_id]
        for c in parent[2:]:
            if isinstance(c, list) and c and c[0] == 'property' and c[1] in props:
                out.append(props.pop(c[1]))
            elif isinstance(c, list) and c and c[0] == 'symbol':     # sub-units: rename Parent_1_1 -> Name_1_1
                out.append([c[0], re.sub('^' + re.escape(base_name), name, str(c[1]))] + c[2:])
            else:
                out.append(c)
        out += list(props.values())
        return out
    return [node[0], lib_id] + node[2:]


def prop(sym, name):
    for p in children(sym, 'property'):
        if p[1] == name:
            return p[2]
    return None


def pins(sym):
    """[{number, name, type, unit, x, y, angle, length}] in symbol coordinates (mm, y up)."""
    out = []
    for sub in children(sym, 'symbol'):
        m = re.search(r'_(\d+)_(\d+)$', str(sub[1]))
        unit = int(m.group(1)) if m else 0
        for p in children(sub, 'pin'):
            at = child(p, 'at')
            name = child(p, 'name')
            num = child(p, 'number')
            ln = child(p, 'length')
            hidden = any(isinstance(c, Sym) and c == 'hide' for c in p) or \
                (child(p, 'hide') is not None and str(child(p, 'hide')[1]) == 'yes')
            out.append({'number': str(num[1]) if num else '', 'name': str(name[1]) if name else '',
                        'type': str(p[1]), 'unit': unit, 'x': float(at[1]), 'y': float(at[2]),
                        'angle': float(at[3]) if len(at) > 3 else 0.0,
                        'length': float(ln[1]) if ln else 2.54, 'hidden': hidden})
    return out


def symbol_info(lib_id):
    s = load_symbol(lib_id)
    ps = pins(s)
    return {'symbol': lib_id, 'description': prop(s, 'Description') or prop(s, 'ki_description'),
            'keywords': prop(s, 'ki_keywords'), 'footprint': prop(s, 'Footprint') or None,
            'footprint_filters': prop(s, 'ki_fp_filters'), 'datasheet': prop(s, 'Datasheet'),
            'units': sorted({p['unit'] for p in ps if p['unit']}) or [1],
            'pins': [{k: p[k] for k in ('number', 'name', 'type', 'unit')} for p in
                     sorted(ps, key=lambda p: _natural(p['number']))]}


def _natural(s):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', str(s))]


@functools.lru_cache(maxsize=1)
def _symbol_index():
    rows = []
    for f in sorted(symbol_dir().glob('*.kicad_sym')):
        text = f.read_text(encoding='utf-8', errors='replace')
        for m in re.finditer(r'\n\t\(symbol "([^"]+)"(.*?)(?=\n\t\(symbol "|\n\)\s*$)', text, re.S):
            name, body = m.group(1), m.group(2)
            desc = re.search(r'\(property "Description" "([^"]*)"', body)
            kw = re.search(r'\(property "ki_keywords" "([^"]*)"', body)
            fp = re.search(r'\(property "Footprint" "([^"]*)"', body)
            rows.append((f'{f.stem}:{name}', desc.group(1) if desc else '', kw.group(1) if kw else '',
                         fp.group(1) if fp else ''))
    return rows


def search_symbols(query, limit=20):
    words = [w.lower() for w in query.split() if w]
    scored = []
    for lib_id, desc, kw, fp in _symbol_index():
        hay = f'{lib_id} {desc} {kw}'.lower()
        if all(w in hay for w in words):
            name = lib_id.split(':', 1)[1].lower()
            score = (0 if any(w == name for w in words) else 1 if any(name.startswith(w) for w in words) else 2,
                     len(lib_id))
            scored.append((score, {'symbol': lib_id, 'description': desc, 'keywords': kw, 'footprint': fp or None}))
    scored.sort(key=lambda r: r[0])
    return [r for _, r in scored[:limit]]


def search_footprints(query, limit=20):
    words = [w.lower() for w in query.split() if w]
    out = []
    for lib in sorted(footprint_dir().glob('*.pretty')):
        for f in lib.glob('*.kicad_mod'):
            fid = f'{lib.stem}:{f.stem}'
            if all(w in fid.lower() for w in words):
                out.append(fid)
    out.sort(key=len)
    return out[:limit]


def footprint_file(fid):
    if ':' not in str(fid):
        raise ValueError(f'footprint {fid!r}: use Library:Name, e.g. Resistor_SMD:R_0402_1005Metric')
    lib, name = str(fid).split(':', 1)
    f = footprint_dir() / f'{lib}.pretty' / f'{name}.kicad_mod'
    if not f.exists():
        close = [Path(p).stem for p in glob.glob(str(footprint_dir() / f'{lib}.pretty' / f'*{name[:6]}*'))][:8]
        raise KeyError(f'footprint {fid} not found' + (f'; similar in {lib}: {close}' if close else
                                                       ' (find one with footprint_search)'))
    return f


def footprint_pads(fid):
    tree = parse(footprint_file(fid).read_text(encoding='utf-8'))
    return sorted({str(p[1]) for p in children(tree, 'pad') if str(p[1])}, key=_natural)


__all__ = ['share_dir', 'symbol_dir', 'footprint_dir', 'load_symbol', 'pins', 'prop', 'symbol_info',
           'search_symbols', 'search_footprints', 'footprint_file', 'footprint_pads']

