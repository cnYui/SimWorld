import os
import shutil
from pathlib import Path

import pytest

PLATFORM = Path(__file__).resolve().parents[1]
REPO = PLATFORM.parent
EXAMPLE = PLATFORM / 'examples' / 'switch_robot'


def qemu_available():
    exe = os.environ.get('EMCOSIM_QEMU')
    return bool(exe and Path(exe).exists())


needs_qemu = pytest.mark.skipif(not qemu_available(), reason='patched QEMU not configured (EMCOSIM_QEMU)')
needs_ngspice = pytest.mark.skipif(shutil.which('ngspice') is None, reason='ngspice not installed')
