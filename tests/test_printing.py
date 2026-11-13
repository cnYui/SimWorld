"""Printable parts -> 3MF -> Bambu Studio hand-off."""
import os
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from emcosim import api, printing
from emcosim.project import Project

EXAMPLE = Path(__file__).resolve().parents[1] / 'examples' / 'switch_robot'
NS = {'m': 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}


def test_parts_listed_and_copied(tmp_path):
    parts = printing.printable_parts(Project(EXAMPLE))
    assert {'press_arm', 'frame', 'servo_cradle'} <= {p['name'] for p in parts}
    assert all(p['exists'] for p in parts)
    copy = Project(api.copy_project(EXAMPLE, tmp_path / 'sr')['root'])
    for p in printing.printable_parts(copy):
        assert p['exists'] and str(copy.root) in p['file']


def test_write_3mf_valid(tmp_path):
    parts = [p for p in printing.printable_parts(Project(EXAMPLE)) if p['name'] in ('press_arm', 'frame')]
    out = printing.write_3mf(parts, tmp_path / 'x.3mf', with_settings=True)
    with zipfile.ZipFile(out['file']) as z:
        names = z.namelist()
        assert {'[Content_Types].xml', '_rels/.rels', '3D/3dmodel.model', 'Metadata/model_settings.config'} <= set(names)
        model = ET.fromstring(z.read('3D/3dmodel.model'))
        cfg = ET.fromstring(z.read('Metadata/model_settings.config'))
    objs = model.findall('m:resources/m:object', NS)
    items = model.findall('m:build/m:item', NS)
    assert len(objs) == len(items) == 2
    for o in objs:
        tris = o.findall('m:mesh/m:triangles/m:triangle', NS)
        nv = len(o.findall('m:mesh/m:vertices/m:vertex', NS))
        assert tris and max(int(t.get(k)) for t in tris for k in ('v1', 'v2', 'v3')) < nv
    keys = {m.get('key'): m.get('value') for m in cfg.find('object').findall('metadata')}
    assert keys['layer_height'] == '0.16' and keys['wall_loops'] == '4'
    # objects sit on the plate (z >= 0 after the item translation)
    z = [float(it.get('transform').split()[-1]) for it in items]
    assert all(v >= 0 for v in z)


@pytest.mark.skipif(os.name == 'nt', reason='fake slicer is a POSIX script')
def test_send_opens_slicer(tmp_path, monkeypatch):
    log = tmp_path / 'argv'
    fake = tmp_path / 'bambu-studio'
    fake.write_text(f'#!/bin/sh\necho "$@" > {log}\n')
    fake.chmod(0o755)
    monkeypatch.setenv('EMCOSIM_BAMBU_STUDIO', str(fake))
    prj = Project(api.copy_project(EXAMPLE, tmp_path / 'sr')['root'])
    out = printing.send_parts(prj, ['press_arm'])
    assert out['opened']['method'] == 'executable'
    import time
    for _ in range(50):
        if log.exists():
            break
        time.sleep(0.05)
    argv = log.read_text().split()
    assert argv[0] == '--single-instance' and argv[-1].endswith('.3mf')


def test_no_slicer_error(tmp_path, monkeypatch):
    monkeypatch.setenv('EMCOSIM_BAMBU_STUDIO', '')
    monkeypatch.setattr(printing, 'find_bambu_studio', lambda: None)
    prj = Project(api.copy_project(EXAMPLE, tmp_path / 'sr')['root'])
    with pytest.raises(FileNotFoundError, match='Bambu Studio'):
        printing.send_parts(prj, ['press_arm'])
    assert list((prj.root / 'print').glob('*.3mf'))     # the 3MF is still exported


def test_all_parts_land_on_the_plate_without_overlap(tmp_path):
    parts = printing.printable_parts(Project(EXAMPLE))
    out = printing.write_3mf(parts, tmp_path / 'all.3mf')
    with zipfile.ZipFile(out['file']) as z:
        model = ET.fromstring(z.read('3D/3dmodel.model'))
    boxes = []
    for o, it in zip(model.findall('m:resources/m:object', NS), model.findall('m:build/m:item', NS)):
        t = [float(x) for x in it.get('transform').split()[-3:]]
        v = [(float(e.get('x')), float(e.get('y'))) for e in o.findall('m:mesh/m:vertices/m:vertex', NS)]
        xs, ys = [p[0] + t[0] for p in v], [p[1] + t[1] for p in v]
        boxes.append((min(xs), min(ys), max(xs), max(ys)))
    for x0, y0, x1, y1 in boxes:  # Bambu Studio's plate runs from (0, 0) to (256, 256)
        assert 0 <= x0 and 0 <= y0 and x1 <= 256 and y1 <= 256
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]


def test_exe_from_link_handler_command():
    assert printing._exe_from_command('"D:\\Bambu Studio\\bambu-studio.exe" "%1"') == 'D:\\Bambu Studio\\bambu-studio.exe'
    assert printing._exe_from_command('C:\\BS\\bambu-studio.exe %1') == 'C:\\BS\\bambu-studio.exe'
