"""KiCad as the PCB kernel: design rule check, electrical rule check, netlists, 3D
export, import of other EDA formats and small edits, all through KiCad's own
kicad-cli and its Python module (pcbnew). emcosim never re-implements what KiCad
does; this module only finds KiCad, calls it and turns its reports into short
summaries an AI agent (or the panel) can act on.

KiCad is GPL and runs as a separate program, so emcosim stays under its own
license. Where KiCad is installed:

    Windows   C:\\Program Files\\KiCad\\<ver>\\bin\\kicad-cli.exe (+ python.exe next to it)
    macOS     /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli
    Linux     kicad-cli on PATH, `import pcbnew` in the system python

EMCOSIM_KICAD_CLI / EMCOSIM_KICAD_PYTHON override the search.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path


IMPORT_TYPES = {                      # file suffix -> pcbnew.PCB_IO_MGR plugin name
    '.epro': 'EASYEDAPRO', '.epro2': 'EASYEDAPRO', '.zip': 'EASYEDAPRO', '.json': 'EASYEDA',
    '.pcbdoc': 'ALTIUM_DESIGNER', '.brd': 'EAGLE', '.cpa': 'CADSTAR_PCB_ARCHIVE',
    '.pcb': 'PCAD', '.kicad_pcb': 'KICAD_SEXP', '.xml': 'IPC2581',
}


class KicadMissing(RuntimeError):
    pass


def _version_key(path):
    m = re.search(r'(\d+)\.(\d+)', str(path))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def find_cli():
    env = os.environ.get('EMCOSIM_KICAD_CLI')
    if env:
        return env
    found = shutil.which('kicad-cli')
    if found:
        return found
    cands = []
    if sys.platform == 'win32':
        for base in (os.environ.get('ProgramFiles', r'C:\Program Files'), os.environ.get('LOCALAPPDATA', '')):
            if base:
                cands += glob.glob(os.path.join(base, 'KiCad', '*', 'bin', 'kicad-cli.exe'))
                cands += glob.glob(os.path.join(base, 'Programs', 'KiCad', '*', 'bin', 'kicad-cli.exe'))
    elif sys.platform == 'darwin':
        cands += glob.glob('/Applications/KiCad*/KiCad.app/Contents/MacOS/kicad-cli')
    cands = [c for c in cands if os.path.exists(c)]
    return max(cands, key=_version_key) if cands else None


def find_python():
    env = os.environ.get('EMCOSIM_KICAD_PYTHON')
    if env:
        return env
    cli = find_cli()
    if cli:
        d = Path(cli).parent
        for name in (('python.exe',) if sys.platform == 'win32' else ('kpython',)):
            if (d / name).exists():
                return str(d / name)
        if sys.platform == 'darwin':
            py = glob.glob(str(d.parent / 'Frameworks/Python.framework/Versions/*/bin/python3'))
            if py:
                return py[0]
    for py in ('/usr/bin/python3', shutil.which('python3')):
        if not py or not os.path.isfile(py):
            continue
        try:
            if subprocess.run([py, '-c', 'import pcbnew'], capture_output=True, timeout=60).returncode == 0:
                return py
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def status():
    cli = find_cli()
    out = {'kicad_cli': cli, 'kicad_python': find_python(), 'version': None}
    if cli:
        try:
            r = subprocess.run([cli, 'version'], capture_output=True, text=True, timeout=60)
            out['version'] = (r.stdout.strip().splitlines() or [None])[-1]
        except (OSError, subprocess.SubprocessError) as e:
            out['error'] = f'kicad-cli did not run: {e}'
    if not cli:
        out['hint'] = ('KiCad was not found, so board_import, board_drc, board_erc, board_edit, schematic tools '
                       'and board_render cannot run. Install KiCad 9 or 10 (kicad.org/download; Windows: '
                       'winget install -e --id KiCad.KiCad) or set EMCOSIM_KICAD_CLI to kicad-cli.')
    return out


def _cli():
    cli = find_cli()
    if not cli:
        raise KicadMissing('KiCad was not found. Install KiCad 9 (kicad.org/download) or set EMCOSIM_KICAD_CLI '
                           'to the path of kicad-cli.')
    return cli


def _run(args, cwd=None, timeout=600):
    r = subprocess.run([_cli(), *map(str, args)], capture_output=True, text=True, cwd=cwd, timeout=timeout)
    if r.returncode not in (0, 5):     # 5: kicad-cli --exit-code-violations
        raise RuntimeError(f'kicad-cli {args[0]} {args[1]} failed ({r.returncode}): '
                           f'{(r.stderr or r.stdout).strip()[-800:]}')
    return r


def _summarize(report, limit, severity=None, types=None):
    counts, items, by_type = {}, [], {}
    for kind, rows in (('violation', report.get('violations', [])),
                       ('unconnected', report.get('unconnected_items', [])),
                       ('parity', report.get('schematic_parity', []))):
        for v in rows:
            sev = v.get('severity', 'error')
            counts[sev] = counts.get(sev, 0) + 1
            by_type[v.get('type')] = by_type.get(v.get('type'), 0) + 1
            if severity and sev != severity or types and v.get('type') not in types:
                continue
            items.append({'kind': kind, 'severity': sev, 'type': v.get('type'),
                          'description': v.get('description'),
                          'where': [{'item': i.get('description'), 'pos_mm': [i['pos']['x'], i['pos']['y']]}
                                    for i in v.get('items', []) if 'pos' in i][:3]})
    items.sort(key=lambda i: i['severity'] != 'error')
    counts['_by_type'] = by_type
    return counts, items[:limit]


def drc(pcb_path, limit=40, schematic_parity=False, severity=None, types=None):
    """Run KiCad's DRC on a board. Returns counts per severity and the first `limit` findings."""
    pcb_path = Path(pcb_path)
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / 'drc.json'
        args = ['pcb', 'drc', '--format', 'json', '--units', 'mm', '--severity-all', '-o', out]
        if schematic_parity:
            args.append('--schematic-parity')
        _run(args + [pcb_path])
        report = json.loads(out.read_text(encoding='utf-8'))
    counts, items = _summarize(report, limit, severity, types)
    return {'tool': 'drc', 'file': pcb_path.name, 'kicad_version': report.get('kicad_version'),
            'errors': counts.get('error', 0), 'warnings': counts.get('warning', 0),
            'unconnected': len(report.get('unconnected_items', [])), 'by_type': counts['_by_type'],
            'items': items, 'passed': counts.get('error', 0) == 0}


def erc(sch_path, limit=40):
    """Run KiCad's ERC on a schematic."""
    sch_path = Path(sch_path)
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / 'erc.json'
        _run(['sch', 'erc', '--format', 'json', '--units', 'mm', '--severity-all', '-o', out, sch_path])
        report = json.loads(out.read_text(encoding='utf-8'))
    rows = [v for sheet in report.get('sheets', []) for v in sheet.get('violations', [])]
    counts, items = _summarize({'violations': rows}, limit)
    return {'tool': 'erc', 'file': sch_path.name, 'kicad_version': report.get('kicad_version'),
            'errors': counts.get('error', 0), 'warnings': counts.get('warning', 0), 'items': items,
            'passed': counts.get('error', 0) == 0}


def netlist(sch_path):
    """Components (ref, value, footprint, fields such as LCSC) and nets from a schematic."""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / 'net.xml'
        _run(['sch', 'export', 'netlist', '--format', 'kicadxml', '-o', out, sch_path])
        root = ET.parse(out).getroot()
    comps = []
    for c in root.iter('comp'):
        fields = {f.get('name'): (f.text or '') for f in c.iter('field')}
        comps.append({'ref': c.get('ref'), 'value': c.findtext('value'), 'footprint': c.findtext('footprint'),
                      'fields': {k: v for k, v in fields.items() if v and k not in ('Reference', 'Value',
                                                                                     'Footprint')}})
    nets = {}
    for n in root.iter('net'):
        nets[n.get('name')] = [f'{p.get("ref")}.{p.get("pin")}' for p in n.iter('node')]
    return {'components': comps, 'nets': nets}


def export_netlist_xml(sch_path, out_path):
    """KiCad XML netlist of a schematic (read_netlist reads it, with library parts and pin names)."""
    _run(['sch', 'export', 'netlist', '--format', 'kicadxml', '-o', Path(out_path), Path(sch_path)])
    return Path(out_path)


def spice_netlist(sch_path):
    """SPICE netlist of a schematic, as KiCad writes it for ngspice (uses Sim.* fields)."""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / 'net.cir'
        _run(['sch', 'export', 'netlist', '--format', 'spice', '-o', out, sch_path])
        return out.read_text(encoding='utf-8')


def export_3d(pcb_path, out_path, fmt=None):
    """Board with components as a 3D model (glb, step, wrl, stl), for the 3D view or a CAD tool."""
    out_path = Path(out_path)
    fmt = fmt or out_path.suffix.lstrip('.').lower()
    _run(['pcb', 'export', fmt, '-o', out_path.resolve(), Path(pcb_path).resolve()], cwd=Path(pcb_path).resolve().parent)
    return {'file': str(out_path), 'bytes': out_path.stat().st_size}


# --------------------------------------------------------------------- pcbnew

def _pcbnew(script, *args, timeout=600):
    py = find_python()
    if not py:
        raise KicadMissing("KiCad's Python (pcbnew) was not found; set EMCOSIM_KICAD_PYTHON.")
    with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False, encoding='utf-8') as f:
        f.write(script)
    try:
        r = subprocess.run([py, f.name, *map(str, args)], capture_output=True, text=True, timeout=timeout)
    finally:
        os.unlink(f.name)
    tail = [ln for ln in r.stdout.splitlines() if ln.startswith('{')]
    err = [ln for ln in (r.stderr or '').splitlines() if ln.startswith('{"error"')]
    if err:                                  # the script stopped with SystemExit(json.dumps({'error': ...}))
        return json.loads(err[-1])
    if r.returncode != 0 or not tail:
        raise RuntimeError(f'KiCad python failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-800:]}')
    return json.loads(tail[-1])


_IMPORT = r'''
import json, sys, pcbnew
src, dst, kind = sys.argv[1], sys.argv[2], sys.argv[3]
board = pcbnew.PCB_IO_MGR.Load(getattr(pcbnew.PCB_IO_MGR, kind), src)
board.SetFileName(dst)
# KiCad 9's EasyEDA Pro importer brings SMD pads in as plated through-hole pads with a 1 um
# drill on all copper layers. Turn them back into surface-mount pads so DRC is meaningful.
fixed = 0
for fp in board.GetFootprints():
    for pad in fp.Pads():
        d = pad.GetDrillSize()
        if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH and max(d.x, d.y) < pcbnew.FromMM(0.05):
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            ls = pcbnew.LSET()
            for layer in ((pcbnew.B_Cu, pcbnew.B_Paste, pcbnew.B_Mask) if fp.IsFlipped()
                          else (pcbnew.F_Cu, pcbnew.F_Paste, pcbnew.F_Mask)):
                ls.AddLayer(layer)
            pad.SetDrillSize(pcbnew.VECTOR2I(0, 0))
            pad.SetLayerSet(ls)
            fixed += 1
        elif (pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH and not pad.GetNetname()
              and min(pad.GetSizeX(), pad.GetSizeY()) <= min(d.x, d.y)):
            pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)      # mounting hole without a copper ring
            fixed += 1
rules = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
# Pads the importer left without a net get it from the source EDA's own netlist, if given.
netmap, netted = rules.get('pins', {}), 0
for fp in board.GetFootprints():
    for pad in fp.Pads():
        name = netmap.get(f'{fp.GetReference()}-{pad.GetNumber()}')
        if name and not pad.GetNetname():
            net = board.FindNet(name)
            if net is None:
                net = pcbnew.NETINFO_ITEM(board, name)
                board.Add(net)
            pad.SetNet(net)
            netted += 1
# Values and supplier part numbers the importer left empty, also from the netlist.
for fp in board.GetFootprints():
    c = rules.get('parts', {}).get(fp.GetReference())
    if not c:
        continue
    if not fp.GetValue() and c.get('value'):
        fp.SetValue(c['value'])
        fp.Value().SetVisible(False)              # data only; keep the silkscreen as drawn
    for field, key in (('LCSC', 'lcsc'), ('MPN', 'mpn'), ('Manufacturer', 'manufacturer')):
        cur = None
        for m in ('GetFieldByName', 'GetField'):          # KiCad 9 / KiCad 10
            if cur is None and hasattr(fp, m):
                try:
                    cur = getattr(fp, m)(field)
                except TypeError:
                    pass
        if c.get(key) and not (cur is not None and cur.GetText()):
            fp.SetField(field, c[key])
            for m in ('GetFieldByName', 'GetField'):
                try:
                    getattr(fp, m)(field).SetVisible(False)
                    break
                except (AttributeError, TypeError):
                    continue
ds = board.GetDesignSettings()
if 'clearance_mm' in rules:
    ds.m_NetSettings.GetDefaultNetclass().SetClearance(pcbnew.FromMM(rules['clearance_mm']))
    ds.m_MinClearance = pcbnew.FromMM(rules['clearance_mm'])
pcbnew.PCB_IO_MGR.Save(pcbnew.PCB_IO_MGR.KICAD_SEXP, dst, board)
print(json.dumps({'footprints': len(board.GetFootprints()), 'nets': board.GetNetCount() - 1,
                  'tracks': len(board.GetTracks()), 'pads_fixed': fixed, 'pads_netted': netted}))
'''


# JLCPCB 2-layer capability as KiCad custom rules (jlcpcb.com/capabilities/pcb-capabilities);
# EasyEDA Pro's "JLCPCB Capability (Two Layers Board)" rule set uses the same numbers.
JLC_RULES = """(version 1)
(rule "JLC copper clearance"
  (constraint clearance (min 0.102mm)))
(rule "JLC pad clearance"
  (constraint clearance (min 0.152mm))
  (condition "A.Type == 'Pad' || B.Type == 'Pad'"))
(rule "JLC via hole"
  (constraint hole_size (min 0.3mm))
  (condition "A.Type == 'Via'"))
(rule "JLC hole to hole"
  (constraint hole_to_hole (min 0.254mm)))
(rule "JLC board edge"
  (constraint edge_clearance (min 0.3mm)))
"""


def read_netlist(path):
    """Components and nets from a netlist file: Protel 2.0 (.net, EasyEDA / Altium export) or
    KiCad XML (.xml). Nets map to 'REF-PIN' strings."""
    path = Path(path)
    text = path.read_text(encoding='utf-8', errors='replace')
    if text.lstrip().startswith('<'):
        root = ET.fromstring(text)
        libparts = {}
        for lp in root.iter('libpart'):
            libparts[(lp.get('lib'), lp.get('part'))] = {pn.get('num'): pn.get('name') for pn in lp.iter('pin')}
        comps = {}
        for c in root.iter('comp'):
            fields = {f.get('name'): f.text for f in c.iter('field') if f.text}
            lcsc = next((v for k, v in fields.items()
                         if k.lower() in ('lcsc', 'lcsc part', 'jlcpcb part', 'supplier part')), None)
            src = c.find('libsource')
            lib, part = (src.get('lib'), src.get('part')) if src is not None else (None, None)
            comps[c.get('ref')] = {'value': c.findtext('value'), 'footprint': c.findtext('footprint'),
                                   **fields, **({'lcsc': lcsc} if lcsc else {}),
                                   **({'lib': lib, 'part': part, 'pin_names': libparts.get((lib, part), {})}
                                      if lib else {})}
        nets = {n.get('name'): [f'{p.get("ref")}-{p.get("pin")}' for p in n.iter('node')] for n in root.iter('net')}
        return {'components': comps, 'nets': nets}
    if not text.startswith('PROTEL NETLIST'):
        raise ValueError(f'{path.name}: expected a Protel 2.0 or KiCad XML netlist')
    comps, nets = {}, {}
    for block in re.findall(r'\[\n(.*?)\n\]', text, re.S):
        lines = block.split('\n')
        kv = {}
        for k in range(0, len(lines) - 1):
            if lines[k] in ('DESIGNATOR', 'FOOTPRINT', 'PARTTYPE', 'Name', 'Value', 'Supplier Part',
                            'Manufacturer', 'Manufacturer Part') and lines[k] not in kv:
                kv[lines[k]] = lines[k + 1].strip()
        ref = kv.get('DESIGNATOR')
        if ref:
            comps[ref] = {'value': kv.get('Value') or kv.get('Name'), 'footprint': kv.get('FOOTPRINT'),
                          'mpn': kv.get('Manufacturer Part') or kv.get('PARTTYPE'),
                          'manufacturer': kv.get('Manufacturer'), 'lcsc': kv.get('Supplier Part')}
    for block in re.findall(r'\(\n(.*?)\n\)', text, re.S):
        lines = [ln.strip() for ln in block.split('\n') if ln.strip()]
        if lines:
            nets[lines[0]] = [ln.split()[0] for ln in lines[1:]]
    return {'components': comps, 'nets': nets}


def import_board(src, dst, rules='jlc', netlist=None):
    """Convert another EDA's board (EasyEDA Pro .epro, Altium .PcbDoc, Eagle .brd, ...) to .kicad_pcb
    with KiCad's importers. rules='jlc' also writes JLCPCB's 2-layer limits as KiCad DRC rules;
    netlist (that EDA's own netlist export) fills in pad nets the importer missed."""
    src, dst = Path(src), Path(dst)
    kind = IMPORT_TYPES.get(src.suffix.lower())
    if not kind:
        raise ValueError(f'no KiCad importer for {src.suffix}; supported: {sorted(IMPORT_TYPES)}')
    opts = {'clearance_mm': 0.102} if rules == 'jlc' else {}
    if netlist:
        nl = read_netlist(netlist)
        opts['pins'] = {pin: net for net, pins in nl['nets'].items() for pin in pins}
        opts['parts'] = nl['components']
    res = _pcbnew(_IMPORT, src, dst, kind, json.dumps(opts))
    if rules == 'jlc':
        dst.with_suffix('.kicad_dru').write_text(JLC_RULES, encoding='utf-8')
    if not res['footprints']:
        raise ValueError(f'KiCad read {src.name} but found no components. For EasyEDA Pro, export the project '
                         f'from the editor (File > Export > EasyEDA Pro project, .epro) rather than zipping its folder.')
    return {'file': str(dst), **res}


_EDIT = r'''
import json, sys, pcbnew
path, ops = sys.argv[1], json.loads(open(sys.argv[2], encoding='utf-8').read())
board = pcbnew.LoadBoard(path)
mm = pcbnew.FromMM
done = []
def field(f, name):                      # KiCad 9: GetFieldByName; KiCad 10: GetField(name)
    for m in ('GetFieldByName', 'GetField'):
        if hasattr(f, m):
            try:
                r = getattr(f, m)(name)
            except TypeError:
                continue
            if r is not None:
                return r
    return next((x for x in f.GetFields() if x.GetName() == name), None)
LAYERS = {'F.Cu': pcbnew.F_Cu, 'B.Cu': pcbnew.B_Cu}
refill = False
def net(name):
    n = board.FindNet(name)
    if n is None:
        names = sorted(str(x) for x in board.GetNetsByName().keys() if str(x) and not str(x).startswith('unconnected-'))
        raise SystemExit(json.dumps({'error': f'no net {name!r} on the board; nets: {names[:60]}'}))
    return n
def fp(ref):
    f = board.FindFootprintByReference(ref)
    if f is None:
        raise SystemExit(json.dumps({'error': f'no footprint {ref}'}))
    return f
for op in ops:
    k = op['op']
    if k == 'move':
        f = fp(op['ref']); old = f.GetPosition()
        f.SetPosition(pcbnew.VECTOR2I(mm(op['x_mm']), mm(op['y_mm'])))
        if 'rot_deg' in op: f.SetOrientationDegrees(op['rot_deg'])
        done.append({**op, 'was': [pcbnew.ToMM(old.x), pcbnew.ToMM(old.y)]})
    elif k == 'set_value':
        f = fp(op['ref']); done.append({**op, 'was': f.GetValue()}); f.SetValue(op['value'])
    elif k == 'set_field':
        f = fp(op['ref']); fld = field(f, op['field']); was = fld.GetText() if fld is not None else None
        f.SetField(op['field'], op['value']); done.append({**op, 'was': was})
        if was is None:
            fld = field(f, op['field'])
            if fld is not None: fld.SetVisible(False)
    elif k == 'track_width':
        n = 0
        for t in board.GetTracks():
            if t.GetNetname() == op['net'] and t.Type() == pcbnew.PCB_TRACE_T:
                t.SetWidth(mm(op['width_mm'])); n += 1
        done.append({**op, 'tracks': n})
    elif k == 'netclass_width':
        nc = board.GetDesignSettings().m_NetSettings.GetNetClassByName(op['netclass'])
        nc.SetTrackWidth(mm(op['width_mm'])); done.append(op)
    elif k == 'clearance':
        board.GetDesignSettings().m_MinClearance = mm(op['mm']); done.append(op)
    elif k == 'add_track':
        pts = op.get('points') or []
        if len(pts) < 2:
            raise SystemExit(json.dumps({'error': 'add_track needs points: [[x_mm, y_mm], [x_mm, y_mm], ...]'}))
        layer = LAYERS.get(op.get('layer', 'F.Cu'))
        if layer is None:
            raise SystemExit(json.dumps({'error': f"layer {op.get('layer')!r}: use F.Cu or B.Cu"}))
        n = net(op['net'])
        for a, b in zip(pts, pts[1:]):
            t = pcbnew.PCB_TRACK(board)
            t.SetStart(pcbnew.VECTOR2I(mm(a[0]), mm(a[1]))); t.SetEnd(pcbnew.VECTOR2I(mm(b[0]), mm(b[1])))
            t.SetWidth(mm(op.get('width_mm', 0.25))); t.SetLayer(layer); t.SetNet(n)
            board.Add(t)
        done.append({**op, 'segments': len(pts) - 1})
    elif k == 'add_via':
        v = pcbnew.PCB_VIA(board)
        v.SetPosition(pcbnew.VECTOR2I(mm(op['x_mm']), mm(op['y_mm'])))
        v.SetWidth(mm(op.get('size_mm', 0.6))); v.SetDrill(mm(op.get('drill_mm', 0.3)))
        v.SetNet(net(op['net'])); board.Add(v); done.append(op)
    elif k == 'delete_tracks':
        sel = [t for t in board.GetTracks()
               if (op.get('all') or t.GetNetname() == op.get('net'))
               and (not op.get('layer') or op.get('layer') == 'via' and t.Type() == pcbnew.PCB_VIA_T
                    or t.GetLayerName() == op.get('layer'))]
        if not op.get('all') and not op.get('net'):
            raise SystemExit(json.dumps({'error': 'delete_tracks needs net (or all: true)'}))
        for t in sel:
            board.Remove(t)
        done.append({**op, 'deleted': len(sel)})
    elif k == 'add_zone':
        layer = LAYERS.get(op.get('layer', 'B.Cu'))
        z = pcbnew.ZONE(board)
        z.SetLayer(layer); z.SetNet(net(op['net']))
        pts = op.get('outline')
        if not pts:
            bb = board.GetBoardEdgesBoundingBox()
            m = mm(op.get('inset_mm', 0.3))
            pts = [[pcbnew.ToMM(bb.GetLeft() + m), pcbnew.ToMM(bb.GetTop() + m)],
                   [pcbnew.ToMM(bb.GetRight() - m), pcbnew.ToMM(bb.GetTop() + m)],
                   [pcbnew.ToMM(bb.GetRight() - m), pcbnew.ToMM(bb.GetBottom() - m)],
                   [pcbnew.ToMM(bb.GetLeft() + m), pcbnew.ToMM(bb.GetBottom() - m)]]
        ol = z.Outline(); ol.NewOutline()
        for p in pts:
            ol.Append(mm(p[0]), mm(p[1]))
        if 'clearance_mm' in op:
            z.SetLocalClearance(mm(op['clearance_mm']))
        board.Add(z); done.append({**op, 'outline': pts}); refill = True
    elif k == 'delete_zones':
        zs = [z for z in board.Zones() if op.get('all') or z.GetNetname() == op.get('net')]
        for z in zs:
            board.Remove(z)
        done.append({**op, 'deleted': len(zs)})
    elif k == 'outline':
        for d in list(board.GetDrawings()):
            if d.GetLayer() == pcbnew.Edge_Cuts: board.Remove(d)
        r = pcbnew.PCB_SHAPE(board); r.SetShape(pcbnew.SHAPE_T_RECT)
        x, y = op['x_mm'], op['y_mm']
        r.SetStart(pcbnew.VECTOR2I(mm(x), mm(y)))
        r.SetEnd(pcbnew.VECTOR2I(mm(x + op['width_mm']), mm(y + op['height_mm'])))
        r.SetLayer(pcbnew.Edge_Cuts); r.SetWidth(mm(0.1)); board.Add(r); done.append(op)
    elif k == 'flip':
        f = fp(op['ref'])
        if hasattr(pcbnew, 'FLIP_DIRECTION_LEFT_RIGHT'):
            f.Flip(f.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        else:
            f.Flip(f.GetPosition(), False)
        done.append({**op, 'side': 'bottom' if f.IsFlipped() else 'top'})
    else:
        raise SystemExit(json.dumps({'error': f'unknown op {k}'}))
if refill or any(True for _ in board.Zones()):
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
pcbnew.SaveBoard(path, board)
print(json.dumps({'applied': done}))
'''

EDIT_OPS = ('move', 'set_value', 'set_field', 'track_width', 'netclass_width', 'clearance', 'add_track',
            'add_via', 'delete_tracks', 'add_zone', 'delete_zones', 'outline', 'flip')


def edit_pcb(pcb_path, ops):
    """Apply small edits with KiCad's own board model and save. A copy of the previous board is kept
    next to it (<name>.kicad_pcb.bak-<time>) so every change can be undone."""
    pcb_path = Path(pcb_path)
    for op in ops:
        if op.get('op') not in EDIT_OPS:
            raise ValueError(f'unknown op {op.get("op")!r}; supported: {EDIT_OPS}')
    backup = pcb_path.with_name(f'{pcb_path.name}.bak-{time.strftime("%Y%m%d-%H%M%S")}')
    shutil.copy2(pcb_path, backup)
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as f:
        json.dump(ops, f)
    try:
        res = _pcbnew(_EDIT, pcb_path, f.name)
    except Exception:
        shutil.copy2(backup, pcb_path)
        backup.unlink(missing_ok=True)
        raise
    finally:
        os.unlink(f.name)
    if 'error' in res:
        shutil.copy2(backup, pcb_path)
        backup.unlink(missing_ok=True)
        raise ValueError(res['error'] + ' (nothing was changed)')
    return {**res, 'backup': backup.name}


def _q(text):
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _block_start(text, pos):
    """Index of the '(' that opens the innermost '(symbol (lib_id' block containing pos."""
    depth = 0
    for k in range(pos, -1, -1):
        c = text[k]
        if c == ')':
            depth += 1
        elif c == '(':
            if depth == 0 and text.startswith('(symbol', k) and '(lib_id' in text[k:k + 200]:
                return k
            depth = max(depth - 1, 0) if depth else depth
    return -1


def _block_end(text, start):
    depth, k, n = 0, start, len(text)
    while k < n:
        c = text[k]
        if c == '"':
            k += 1
            while text[k] != '"':
                k += 2 if text[k] == '\\' else 1
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return k + 1
        k += 1
    return -1


def edit_schematic(sch_path, ref, field, value):
    """Set a symbol field (Value, LCSC, Sim.Device, ...) in a .kicad_sch. KiCad has no schematic
    Python API yet, so this edits that one property in the file text and leaves the rest untouched."""
    sch_path = Path(sch_path)
    text = sch_path.read_text(encoding='utf-8')
    m = re.search(r'\(property\s+"Reference"\s+' + re.escape(_q(ref)), text)
    if not m:
        raise ValueError(f'no symbol {ref} in {sch_path.name}')
    start = _block_start(text, m.start() - 1)
    end = _block_end(text, start)
    block = text[start:end]
    pm = re.search(r'(\(property\s+' + re.escape(_q(field)) + r'\s+)("(?:[^"\\]|\\.)*")', block)
    if pm:
        was = pm.group(2)[1:-1]
        block = block[:pm.start(2)] + _q(value) + block[pm.end(2):]
    else:
        was = None
        rm = re.search(r'\(property\s+"Reference"[^\n]*\n(\s*)', block)
        at = re.search(r'\(at [^)]*\)', block[rm.start():])
        indent = rm.group(1) if rm else '\t\t'
        line = (f'(property {_q(field)} {_q(value)} {at.group(0) if at else "(at 0 0 0)"} '
                f'(effects (font (size 1.27 1.27)) (hide yes)))\n{indent}')
        block = block[:rm.start()] + line + block[rm.start():]
    backup = sch_path.with_name(f'{sch_path.name}.bak-{time.strftime("%Y%m%d-%H%M%S")}')
    shutil.copy2(sch_path, backup)
    sch_path.write_text(text[:start] + block + text[end:], encoding='utf-8')
    return {'ref': ref, 'field': field, 'value': value, 'was': was, 'backup': backup.name}
