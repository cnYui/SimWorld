"""Send printable parts to Bambu Studio (Bambu Lab's slicer).

Bambu Lab documents no public "import this model" API for Bambu Studio, so this
uses the two hand-offs Bambu Studio itself supports:

1. Start the slicer with the file as a command-line argument
   (`bambu-studio [file.3mf/file.stl ...]`, see the Command-Line-Usage page of the
   BambuStudio wiki). On Windows the executable is found through the registry
   (App Paths / uninstall entry) or the default install folder.
2. The `bambustudio://open?file=<url>` link MakerWorld uses for its "Open in
   Bambu Studio" button, pointing at a URL the app serves on 127.0.0.1. Used
   when the executable is not found but the URL handler is registered.

The parts are packed into one plain 3MF (core spec, millimetres) in the
orientation they were exported in. Recommended print settings are embedded as
per-object settings in Metadata/model_settings.config only when asked
(`with_settings=True`); that file follows the layout Bambu Studio writes, which
Bambu Lab does not document, so it is opt-in.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import numpy as np

SETTING_KEYS = {          # emcosim name -> Bambu Studio config key
    'layer_height': 'layer_height',
    'wall_loops': 'wall_loops',
    'top_shell_layers': 'top_shell_layers',
    'bottom_shell_layers': 'bottom_shell_layers',
    'sparse_infill_density': 'sparse_infill_density',
    'sparse_infill_pattern': 'sparse_infill_pattern',
    'enable_support': 'enable_support',
    'brim_type': 'brim_type',
}


def printable_parts(project):
    """Parts listed under printing.parts in project.yaml, with resolved paths."""
    out = []
    for name, p in ((project.data.get('printing') or {}).get('parts') or {}).items():
        f = (project.root / p['file']).resolve()
        out.append({'name': name, 'file': str(f), 'exists': f.exists(), 'material': p.get('material'),
                    'settings': p.get('settings') or {}, 'note': p.get('note'), 'label': p.get('label', name)})
    return out


# --------------------------------------------------------------------------- 3MF

def _load_mesh(path):
    import trimesh
    m = trimesh.load(str(path), force='mesh', process=True)
    return np.asarray(m.vertices, dtype=float), np.asarray(m.faces, dtype=np.int64)


def write_3mf(parts, out_path, with_settings=False, spacing_mm=8.0, app_name='emcosim'):
    """parts: [{'name', 'file', 'settings'?}] (STL/OBJ/3MF in millimetres).
    Objects are dropped to z=0 and laid out in a row so they do not overlap."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    objects, items = [], []
    x_cursor = 0.0
    for i, part in enumerate(parts, start=1):
        v, f = _load_mesh(part['file'])
        lo, hi = v.min(axis=0), v.max(axis=0)
        # Keep the exported orientation; only translate onto the plate.
        offset = np.array([x_cursor - lo[0], -(lo[1] + hi[1]) / 2, -lo[2]])
        x_cursor += (hi[0] - lo[0]) + spacing_mm
        verts = '\n'.join(f'<vertex x="{a:.9g}" y="{b:.9g}" z="{c:.9g}"/>' for a, b, c in v)
        tris = '\n'.join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in f)
        objects.append(f'<object id="{i}" name={quoteattr(part["name"])} type="model">'
                       f'<mesh><vertices>\n{verts}\n</vertices><triangles>\n{tris}\n</triangles></mesh></object>')
        items.append(f'<item objectid="{i}" transform="1 0 0 0 1 0 0 0 1 '
                     f'{offset[0]:.4f} {offset[1]:.4f} {offset[2]:.4f}" printable="1"/>')
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<model unit="millimeter" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
             f'<metadata name="Application">{escape(app_name)}</metadata>\n'
             f'<metadata name="CreationDate">{time.strftime("%Y-%m-%d")}</metadata>\n'
             '<resources>\n' + '\n'.join(objects) + '\n</resources>\n'
             '<build>\n' + '\n'.join(items) + '\n</build>\n</model>\n')
    content_types = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
                     '<Default Extension="config" ContentType="text/xml"/>'
                     '</Types>\n')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>\n')
    with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', content_types)
        z.writestr('_rels/.rels', rels)
        z.writestr('3D/3dmodel.model', model)
        if with_settings:
            z.writestr('Metadata/model_settings.config', _model_settings(parts))
    return {'file': str(out_path), 'objects': [p['name'] for p in parts], 'with_settings': with_settings}


def _model_settings(parts):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<config>']
    for i, part in enumerate(parts, start=1):
        lines.append(f'  <object id="{i}">')
        lines.append(f'    <metadata key="name" value={quoteattr(part["name"])}/>')
        for k, v in (part.get('settings') or {}).items():
            if k in SETTING_KEYS:
                val = ('1' if v else '0') if isinstance(v, bool) else str(v)
                lines.append(f'    <metadata key="{SETTING_KEYS[k]}" value={quoteattr(val)}/>')
        lines.append(f'    <part id="{i}" subtype="normal_part">')
        lines.append(f'      <metadata key="name" value={quoteattr(part["name"])}/>')
        lines.append('    </part>')
        lines.append('  </object>')
    lines.append('</config>')
    return '\n'.join(lines) + '\n'


# --------------------------------------------------------------------------- Bambu Studio

def find_bambu_studio():
    env = os.environ.get('EMCOSIM_BAMBU_STUDIO')
    if env and Path(env).exists():
        return env
    if sys.platform == 'win32':
        import winreg
        keys = [(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\bambu-studio.exe', ''),
                (winreg.HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\bambu-studio.exe', '')]
        for hive, sub, val in keys:
            try:
                with winreg.OpenKey(hive, sub) as k:
                    p = winreg.QueryValueEx(k, val)[0]
                    if p and Path(p).exists():
                        return p
            except OSError:
                pass
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for root in (r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                         r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'):
                try:
                    with winreg.OpenKey(hive, root) as k:
                        for i in range(winreg.QueryInfoKey(k)[0]):
                            try:
                                with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sk:
                                    name = winreg.QueryValueEx(sk, 'DisplayName')[0]
                                    if 'bambu studio' not in str(name).lower():
                                        continue
                                    loc = winreg.QueryValueEx(sk, 'InstallLocation')[0]
                                    cand = Path(loc) / 'bambu-studio.exe'
                                    if cand.exists():
                                        return str(cand)
                            except OSError:
                                continue
                except OSError:
                    pass
        for base in (os.environ.get('ProgramFiles', r'C:\Program Files'),
                     os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
                     os.environ.get('LOCALAPPDATA', '')):
            cand = Path(base) / 'Bambu Studio' / 'bambu-studio.exe'
            if base and cand.exists():
                return str(cand)
        return None
    if sys.platform == 'darwin':
        app = Path('/Applications/BambuStudio.app')
        return str(app) if app.exists() else None
    return shutil.which('bambu-studio') or shutil.which('BambuStudio')


def url_handler_registered():
    if sys.platform != 'win32':
        return False
    import winreg
    try:
        winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, 'bambustudio').Close()
        return True
    except OSError:
        return False


def open_in_bambu_studio(path, download_url=None):
    """Open a 3MF/STL in Bambu Studio. Returns {'method': ...}."""
    path = str(Path(path).resolve())
    exe = find_bambu_studio()
    if exe:
        if sys.platform == 'darwin':
            subprocess.Popen(['open', '-a', exe, path])
        else:
            flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
            subprocess.Popen([exe, path], cwd=str(Path(exe).parent), creationflags=flags,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {'method': 'executable', 'exe': exe, 'file': path}
    if download_url and url_handler_registered():
        from urllib.parse import quote
        os.startfile('bambustudio://open?file=' + quote(download_url, safe=''))  # noqa: S606
        return {'method': 'url_scheme', 'file': path}
    raise FileNotFoundError('没有找到 Bambu Studio。请先安装（https://bambulab.com/download/studio），'
                            f'或者手动把 {path} 拖进 Bambu Studio。')


def send_parts(project, names=None, with_settings=False, out_dir=None, download_url_for=None, open_app=True):
    parts = [p for p in printable_parts(project) if names is None or p['name'] in names]
    if not parts:
        raise ValueError('没有可打印的零件（project.yaml 的 printing.parts）')
    missing = [p['name'] for p in parts if not p['exists']]
    if missing:
        raise FileNotFoundError(f'找不到这些零件的网格文件: {missing}')
    out_dir = Path(out_dir or project.root / 'print')
    stamp = time.strftime('%Y%m%d-%H%M%S')
    label = parts[0]['name'] if len(parts) == 1 else f'{len(parts)}parts'
    out = write_3mf(parts, out_dir / f'{project.data.get("name", "project")}-{label}-{stamp}.3mf', with_settings)
    if open_app:
        url = download_url_for(Path(out['file'])) if download_url_for else None
        out['opened'] = open_in_bambu_studio(out['file'], url)
    return out
