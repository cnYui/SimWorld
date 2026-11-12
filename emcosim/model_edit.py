"""Read and edit the mechanical model (MuJoCo MJCF) by element name.

MuJoCo is the physics kernel; this module only gives an AI agent a safe way to
change it: look up bodies, geoms and joints with their sizes, positions and
masses, set attributes on named elements, check that MuJoCo still compiles the
model, and keep a backup so the change can be undone.
"""
from __future__ import annotations

import shutil
import time
import xml.etree.ElementTree as ET
from pathlib import Path

TAGS = ('body', 'geom', 'joint', 'site', 'mesh', 'material', 'default')
KEYS = ('pos', 'quat', 'euler', 'axisangle', 'size', 'fromto', 'type', 'mesh', 'mass', 'density', 'friction',
        'range', 'axis', 'damping', 'stiffness', 'armature', 'frictionloss', 'contype', 'conaffinity',
        'material', 'rgba', 'scale', 'file', 'solref', 'solimp')


def mjcf_path(project):
    rel = (project.data.get('mechanics') or {}).get('mjcf')
    if not rel:
        raise ValueError('project has no mechanics.mjcf')
    return project.root / rel


def info(project, tag=None):
    """Named elements of the model with their main attributes."""
    path = mjcf_path(project)
    root = ET.parse(path).getroot()
    out = {}
    for t in ([tag] if tag else TAGS):
        rows = []
        for el in root.iter(t):
            name = el.get('name') or el.get('class') if t == 'default' else el.get('name')
            if not name:
                continue
            rows.append({'name': name, **{k: el.get(k) for k in KEYS if el.get(k) is not None}})
        if rows:
            out[t] = rows
    return {'file': str(path.relative_to(project.root)), **out}


def _compile(path):
    import mujoco
    mujoco.MjModel.from_xml_path(str(path))


def edit(project, ops):
    """ops: [{'tag': 'geom', 'name': 'arm_collision', 'attr': 'friction', 'value': '0.6 0.005 0.0001'}].
    value None removes the attribute. The model must still compile, otherwise nothing changes."""
    path = mjcf_path(project)
    text = path.read_text(encoding='utf-8')
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    tree = ET.ElementTree(ET.fromstring(text, parser=parser))
    root = tree.getroot()
    done = []
    for op in ops:
        tag, name, attr = op.get('tag'), op['name'], op['attr']
        hits = [el for el in root.iter(tag or '*') if el.get('name') == name]
        if not hits:
            raise ValueError(f'no {tag or "element"} named {name!r}')
        if len(hits) > 1 and not tag:
            raise ValueError(f'{name!r} names several elements ({sorted({h.tag for h in hits})}); give tag')
        el = hits[0]
        was = el.get(attr)
        value = op.get('value')
        if value is None:
            el.attrib.pop(attr, None)
        else:
            el.set(attr, ' '.join(str(v) for v in value) if isinstance(value, (list, tuple)) else str(value))
        done.append({'tag': el.tag, 'name': name, 'attr': attr, 'value': el.get(attr), 'was': was})
    backup = path.with_name(f'{path.name}.bak-{time.strftime("%Y%m%d-%H%M%S")}')
    shutil.copy2(path, backup)
    prolog = text[:text.find('<' + root.tag)]          # comments before the root element
    path.write_text(prolog + ET.tostring(root, encoding='unicode') + '\n', encoding='utf-8')
    try:
        _compile(path)
    except Exception as e:
        shutil.copy2(backup, path)
        backup.unlink()
        raise ValueError(f'MuJoCo rejected the change, nothing was saved: {e}') from None
    return {'applied': done, 'backup': backup.name}
