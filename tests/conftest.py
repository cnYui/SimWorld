import os
import shutil
from pathlib import Path

import pytest

from emcosim.toolpaths import find_ngspice

PLATFORM = Path(__file__).resolve().parents[1]
EXAMPLE = PLATFORM / 'examples' / 'switch_robot'


def qemu_available():
    try:
        from emcosim.mcu.qemu import find_qemu
        exe, _ = find_qemu()
    except RuntimeError:
        return False
    return bool(exe and Path(exe).exists())


needs_qemu = pytest.mark.skipif(not qemu_available(), reason='patched QEMU not configured (EMCOSIM_QEMU)')
needs_ngspice = pytest.mark.skipif(find_ngspice() is None, reason='ngspice not installed')


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path_factory, monkeypatch):
    """Keep tests away from the real settings and from a SimWorld page running on this machine."""
    d = tmp_path_factory.mktemp('cfg')
    monkeypatch.setenv('APPDATA', str(d))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(d))
