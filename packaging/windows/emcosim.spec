# PyInstaller spec: one folder with two executables sharing the same libraries.
#   emcosim-app.exe  desktop window (no console)
#   emcosim.exe      command line + MCP server (used by Claude Code from the app's chat)
# Build from platform/:  pyinstaller packaging/windows/emcosim.spec --noconfirm
# Expects build/stage/examples (from stage.py) and optionally build/stage/qemu.
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parents[1]          # platform/
STAGE = ROOT / 'build' / 'stage'

datas, binaries, hidden = [], [], []
for pkg in ('mujoco', 'webview', 'trimesh', 'mcp', 'anthropic', 'cascadio', 'emcosim', 'pydantic',
            'clr_loader', 'pythonnet'):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hidden += h
    except Exception as e:                # optional packages
        print('skip', pkg, e)
hidden += collect_submodules('scipy.sparse') + collect_submodules('yaml')

extra = []
if (STAGE / 'examples').exists():
    extra.append((str(STAGE / 'examples'), 'examples'))
if (STAGE / 'qemu').exists():
    extra.append((str(STAGE / 'qemu'), 'qemu'))
if (STAGE / 'licenses').exists():
    extra.append((str(STAGE / 'licenses'), 'licenses'))


def analysis(script):
    return Analysis([str(ROOT / 'packaging' / 'windows' / script)], pathex=[str(ROOT)], binaries=binaries,
                    datas=datas, hiddenimports=hidden, excludes=['tkinter', 'matplotlib', 'IPython', 'pytest'],
                    noarchive=False)


a_app = analysis('entry_app.py')
a_cli = analysis('entry_cli.py')
icon = ROOT / 'packaging' / 'windows' / 'emcosim.ico'
icon = str(icon) if icon.exists() else None

exe_app = EXE(PYZ(a_app.pure), a_app.scripts, [], exclude_binaries=True, name='emcosim-app', console=False,
              icon=icon, version=None)
exe_cli = EXE(PYZ(a_cli.pure), a_cli.scripts, [], exclude_binaries=True, name='emcosim', console=True, icon=icon)

coll = COLLECT(exe_app, a_app.binaries, a_app.datas, exe_cli, a_cli.binaries, a_cli.datas, *[], name='emcosim',
               upx=False)

# Data folders next to the executables (examples/, qemu/, licenses/), not inside _internal,
# so that find_qemu() and the "open example" screen find them at <install>/<name>.
import shutil
dist = Path(DISTPATH) / 'emcosim'
for src, name in extra:
    tgt = dist / name
    if tgt.exists():
        shutil.rmtree(tgt)
    shutil.copytree(src, tgt)
