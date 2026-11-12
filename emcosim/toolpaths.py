"""Where emcosim finds its external programs (QEMU, ngspice).

scripts/setup.sh and scripts/setup.ps1 put them under <repo>/tools/ so that a fresh
clone works without touching PATH. Environment variables and PATH still win.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

EXE = '.exe' if sys.platform == 'win32' else ''


def tools_dir():
    env = os.environ.get('EMCOSIM_TOOLS')
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / 'tools'


def _first(cands):
    for c in cands:
        if c.exists():
            return str(c)
    return None


def find_ngspice():
    env = os.environ.get('EMCOSIM_NGSPICE')
    if env:
        return env
    found = shutil.which('ngspice')
    if found:
        return found
    t = tools_dir() / 'ngspice'
    # Windows build: Spice64/bin/ngspice_con.exe is the console program (-b batch mode)
    return _first([t / 'Spice64' / 'bin' / 'ngspice_con.exe', t / 'bin' / ('ngspice_con' + EXE),
                   t / 'bin' / ('ngspice' + EXE), t / 'Spice64' / 'bin' / 'ngspice.exe'])


def local_qemu():
    t = tools_dir() / 'qemu'
    return _first([t / ('qemu-system-xtensa' + EXE), t / 'bin' / ('qemu-system-xtensa' + EXE)])
