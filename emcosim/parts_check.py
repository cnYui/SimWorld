"""Does an LCSC number fit the part it is attached to?

A wrong LCSC number is easy to write (a 16-pin USB-C on a 6-pin footprint, a 4.7 k on a 5.1 k resistor,
an 0603 capacitor on an 0402 footprint) and nothing downstream notices until the boards arrive. This
compares the live JLCPCB/LCSC record with the schematic: package size or family against the footprint,
pin count against the footprint's pads, and R/C/L values against the schematic value. A mismatch is a
problem; something that cannot be compared is left as unchecked, never guessed.
"""
from __future__ import annotations

import re

SIZE = re.compile(r'(?<!\d)(01005|0201|0402|0603|0805|1008|1206|1210|1812|2010|2512)(?!\d)')
FAMILY = re.compile(r'(SOT-?23|SOT-?223|SOT-?89|SOD-?123F?|SOD-?323|SOD-?523|SMA|SMB|SMC|QFN|DFN|LGA|BGA|QFP|LQFP|TQFP|'
                    r'SOIC|SOP|SSOP|TSSOP|MSOP|ESOP|TO-?220|TO-?252|TO-?263|DIP|SIP)[-_]?(\d+)?', re.I)
PINS = re.compile(r'(\d+)\s*(?:PIN|P\b|POS|CONTACT)', re.I)


def _family(text):
    """('SOT23', 5) style family + pin count from a footprint or package name."""
    m = FAMILY.search(text or '')
    if not m:
        return None
    fam = m.group(1).upper().replace('-', '')
    n = int(m.group(2)) if m.group(2) else None
    if fam == 'SOT23' and n is None:
        n = 3
    return fam, n


def _value(text):
    from .electrical.board_spice import parse_value
    if text is None:
        return None
    t = str(text).split()[0] if str(text).split() else ''
    t = t.replace('Ω', '').replace('ohm', '').replace('µ', 'u').replace('μ', 'u')
    t = re.sub(r'(?<=\d)[Hh]$', '', t)
    return parse_value(t)


def _spec(specs, *names):
    for k, v in (specs or {}).items():
        if any(n.lower() in k.lower() for n in names):
            return v
    return None


def check_part(ref, sch_value, footprint, info, footprint_pads=None):
    """{'status': ok|mismatch|unchecked, 'problems': [...], 'checked': [...]} for one reference."""
    problems, checked = [], []
    pkg = str(info.get('package') or '')
    fp_name = str(footprint or '').split(':', 1)[-1]
    # package size (passives) or family + pins (ICs)
    s_fp, s_pkg = SIZE.search(fp_name), SIZE.search(pkg)
    if s_fp and s_pkg:
        checked.append('size')
        if s_fp.group(1) != s_pkg.group(1):
            problems.append(f'package {s_pkg.group(1)} but the footprint is {s_fp.group(1)} ({fp_name})')
    else:
        f_fp, f_pkg = _family(fp_name), _family(pkg)
        if f_fp and f_pkg:
            checked.append('package')
            if f_fp[0] != f_pkg[0] and not {f_fp[0], f_pkg[0]} <= {'SOP', 'SOIC'}:
                problems.append(f'package {pkg} but the footprint is {fp_name}')
            elif f_fp[1] and f_pkg[1] and f_fp[1] != f_pkg[1]:
                problems.append(f'{f_pkg[1]} pins ({pkg}) but the footprint has {f_fp[1]} ({fp_name})')
    # pin count of connectors and the like, from the part record against the footprint's pads
    if footprint_pads:
        signal = [p for p in footprint_pads if p and not re.fullmatch(r'MP|SH|S\d*|EP|0', p, re.I)]
        spec_pins = _spec(info.get('specs'), 'Number of Contacts', 'Number of Pins', 'Number of Positions', 'Pin Count')
        m = PINS.search(str(spec_pins or '')) or re.fullmatch(r'\s*(\d+)\s*', str(spec_pins or '')) \
            or PINS.search(str(info.get('model') or ''))
        if m and str(info.get('category') or '').lower().startswith('connector'):
            n = int(m.group(1))
            checked.append('pins')
            if n != len(signal) and n != len(footprint_pads):
                problems.append(f'{n}-pin part but the footprint has {len(signal)} signal pads ({fp_name})')
    # value of resistors, capacitors, inductors
    kind = re.sub(r'\d+$', '', ref).upper()
    spec_name = {'R': ('Resistance',), 'C': ('Capacitance',), 'L': ('Inductance',)}.get(kind)
    if spec_name:
        want, got = _value(sch_value), _value(_spec(info.get('specs'), *spec_name))
        if want is not None and got is not None:
            checked.append('value')
            if abs(want - got) > 0.011 * max(abs(want), abs(got)):
                problems.append(f'value {_spec(info.get("specs"), *spec_name)} but the schematic says {sch_value}')
    elif sch_value and info.get('model'):
        a = re.sub(r'[^A-Z0-9]', '', str(sch_value).upper())
        b = re.sub(r'[^A-Z0-9]', '', str(info['model']).upper())
        if len(a) >= 4 and len(b) >= 4 and re.search(r'\d', a):
            checked.append('name')
            if not (a in b or b in a or a[:6] == b[:6]):
                problems.append(f'part is {info["model"]} but the schematic value is {sch_value}')
    return {'status': 'mismatch' if problems else ('ok' if checked else 'unchecked'),
            'problems': problems, 'checked': checked}


__all__ = ['check_part']
