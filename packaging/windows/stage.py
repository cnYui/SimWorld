"""Prepare build/stage/ for the Windows package: a self-contained copy of the
example project (meshes copied in, no runs/) and the license texts.

    python packaging/windows/stage.py [--qemu DIR]
"""
import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from emcosim.api import copy_project  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--qemu', help='folder with qemu-system-xtensa.exe, its DLLs and share/qemu ROMs')
    a = ap.parse_args()
    stage = ROOT / 'build' / 'stage'
    if stage.exists():
        shutil.rmtree(stage)
    (stage / 'examples').mkdir(parents=True)
    for ex in sorted((ROOT / 'examples').iterdir()):
        if (ex / 'project.yaml').exists():
            copy_project(ex, stage / 'examples' / ex.name)
            for junk in ('scripts', '.gitignore'):
                p = stage / 'examples' / ex.name / junk
                if p.is_dir():
                    shutil.rmtree(p)
    lic = stage / 'licenses'
    lic.mkdir()
    shutil.copy(ROOT / 'LICENSE', lic / 'emcosim-LICENSE.txt')
    shutil.copy(ROOT / 'NOTICE', lic / 'NOTICE.txt')
    shutil.copy(ROOT / 'qemu' / 'README.md', lic / 'QEMU-README.md')
    shutil.copy(ROOT / 'qemu' / '0001-esp32-cosim-gpio-ledc-lockstep.patch', lic / 'qemu-cosim.patch')
    shutil.copy(ROOT / 'emcosim' / 'app' / 'static' / 'vendor' / 'three.LICENSE', lic / 'three.js-LICENSE.txt')
    if a.qemu:
        shutil.copytree(a.qemu, stage / 'qemu')
    print('staged', stage)


if __name__ == '__main__':
    main()
