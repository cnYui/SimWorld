"""Autorouting: freerouting (Specctra DSN out, SES back in, through KiCad's own pcbnew) when it is
installed, else the built-in grid router (emcosim.design.router)."""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .. import eda
from ..toolpaths import tools_dir


def find_freerouting():
    """[command...] to run freerouting, or None. EMCOSIM_FREEROUTING may point at the CLI or a jar."""
    env = os.environ.get('EMCOSIM_FREEROUTING')
    cands = [env] if env else []
    t = tools_dir() / 'freerouting'
    # the jar first: the native freerouting-cli 2.5.0 build crashes on Windows (GraalVM serialization)
    cands += sorted(glob.glob(str(t / '*.jar')), reverse=True)
    cands += [str(t / 'cli' / 'freerouting-cli.exe'), str(t / 'cli' / 'freerouting-cli'),
              str(t / 'freerouting-cli.exe'), str(t / 'freerouting-cli')]
    for c in cands:
        if c and Path(c).is_file():
            if c.endswith('.jar'):
                java = shutil.which('java')
                if java:
                    return [java, '-jar', c]
                continue
            return [c]
    found = shutil.which('freerouting-cli') or shutil.which('freerouting')
    return [found] if found else None


_EXPORT = r'''
import json, sys, pcbnew
board = pcbnew.LoadBoard(sys.argv[1])
ok = pcbnew.ExportSpecctraDSN(board, sys.argv[2])
print(json.dumps({'ok': bool(ok)}))
'''

_IMPORT = r'''
import json, sys, pcbnew
board = pcbnew.LoadBoard(sys.argv[1])
before = len([t for t in board.GetTracks()])
ok = pcbnew.ImportSpecctraSES(board, sys.argv[2])
if not ok:
    raise SystemExit(json.dumps({'error': 'KiCad could not read the routed session from freerouting'}))
if any(True for _ in board.Zones()):
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
pcbnew.SaveBoard(sys.argv[1], board)
tr = [t for t in board.GetTracks()]
print(json.dumps({'tracks_before': before, 'tracks': sum(1 for t in tr if t.Type() == pcbnew.PCB_TRACE_T),
                  'vias': sum(1 for t in tr if t.Type() == pcbnew.PCB_VIA_T)}))
'''


def freerouting(pcb_path, passes=20, timeout=600):
    cmd = find_freerouting()
    if not cmd:
        raise FileNotFoundError('freerouting not found: put freerouting-cli in tools/freerouting/cli or set '
                                'EMCOSIM_FREEROUTING (github.com/freerouting/freerouting/releases)')
    pcb_path = Path(pcb_path).resolve()
    with tempfile.TemporaryDirectory() as td:
        dsn, ses = Path(td) / 'board.dsn', Path(td) / 'board.ses'
        r = eda._pcbnew(_EXPORT, pcb_path, dsn)
        if not r.get('ok') or not dsn.exists():
            raise RuntimeError('KiCad could not export the board for freerouting (Specctra DSN)')
        t0 = time.time()
        p = subprocess.run(cmd + ['-de', str(dsn), '-do', str(ses), '-mp', str(int(passes))],
                           capture_output=True, text=True, timeout=timeout, cwd=td)
        if not ses.exists():
            tail = '\n'.join((p.stdout + p.stderr).splitlines()[-12:])
            raise RuntimeError(f'freerouting did not produce a result (exit {p.returncode}):\n{tail}')
        log = [ln for ln in (p.stdout + p.stderr).splitlines()
               if 'stage completed' in ln or 'Auto-routing stage' in ln][-4:]
        log = [ln.split('] ', 1)[-1] for ln in log]
        res = eda._pcbnew(_IMPORT, pcb_path, ses)
        if 'error' in res:
            raise RuntimeError(res['error'])
        return {'engine': 'freerouting', 'seconds': round(time.time() - t0, 1), **res, 'log': log}


def grid(pcb_path, **kw):
    from .router import route
    ops, rep = route(pcb_path, **kw)
    if ops:
        eda.edit_pcb(pcb_path, ops)
    return {'engine': 'grid', **rep}


def autoroute(pcb_path, engine='auto', passes=20, **grid_kw):
    pcb_path = Path(pcb_path)
    backup = pcb_path.with_name(f'{pcb_path.name}.bak-{time.strftime("%Y%m%d-%H%M%S")}')
    shutil.copy2(pcb_path, backup)
    try:
        if engine == 'freerouting' or (engine == 'auto' and find_freerouting()):
            out = freerouting(pcb_path, passes=passes)
        elif engine in ('grid', 'auto'):
            out = grid(pcb_path, **grid_kw)
        else:
            raise ValueError("engine: 'auto', 'freerouting' or 'grid'")
    except Exception:
        shutil.copy2(backup, pcb_path)
        backup.unlink(missing_ok=True)
        raise
    out['backup'] = backup.name
    return out


__all__ = ['autoroute', 'find_freerouting', 'freerouting', 'grid']
