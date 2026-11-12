"""Project = one directory with project.yaml (+ pcb/, mechanics/, firmware/,
scenarios/, runs/). Everything a run depends on is text in project.yaml or a
file it references, so a design change is a reviewable diff."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
from pathlib import Path

import yaml

_SAVE_LOCK = threading.RLock()

PROJECT_FILE = 'project.yaml'
SCHEMA_VERSION = '0.1'


class _Dumper(yaml.SafeDumper):
    pass


def _repr_float(dumper, value):
    text = repr(float(value))
    if 'e' in text and '.' not in text.split('e')[0]:
        text = text.replace('e', '.0e', 1)       # YAML 1.1 needs a dot to read 1e-05 as float
    if text in ('inf', '-inf', 'nan'):
        text = {'inf': '.inf', '-inf': '-.inf', 'nan': '.nan'}[text]
    return dumper.represent_scalar('tag:yaml.org,2002:float', text)


_Dumper.add_representer(float, _repr_float)


def dump_yaml(data):
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)


def load_yaml(path):
    return yaml.safe_load(Path(path).read_text(encoding='utf8')) or {}


def _split(path):
    if isinstance(path, (list, tuple)):
        return list(path)
    return [int(p) if re.fullmatch(r'\d+', p) else p for p in str(path).split('.')]


def get_path(data, path, default=KeyError):
    cur = data
    for key in _split(path):
        try:
            cur = cur[key]
        except (KeyError, IndexError, TypeError):
            if default is KeyError:
                raise KeyError(f'no parameter {path!r}') from None
            return default
    return cur


def set_path(data, path, value, create=True):
    keys = _split(path)
    cur = data
    for k, nxt in zip(keys[:-1], keys[1:]):
        if isinstance(cur, list):
            cur = cur[k]
            continue
        if k not in cur or cur[k] is None:
            if not create:
                raise KeyError(f'no parameter {path!r}')
            cur[k] = [] if isinstance(nxt, int) else {}
        cur = cur[k]
    last = keys[-1]
    if isinstance(cur, list):
        cur[last] = value
    else:
        cur[last] = value
    return data


def pin_number(pin):
    if isinstance(pin, int):
        return pin
    m = re.fullmatch(r'(?:GPIO|IO|G)?(\d+)', str(pin).strip(), re.I)
    if not m:
        raise ValueError(f'cannot read pin {pin!r}; use e.g. GPIO18')
    return int(m.group(1))


class Project:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.file = self.root / PROJECT_FILE
        if not self.file.exists():
            raise FileNotFoundError(f'{self.file} not found (create one with `emcosim new`)')
        self.data = load_yaml(self.file)

    @classmethod
    def create(cls, root, name=None):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        f = root / PROJECT_FILE
        if not f.exists():
            data = {
                'emcosim': SCHEMA_VERSION,
                'name': name or root.name,
                'mcu': {'type': 'esp32', 'backend': 'qemu', 'step_ms': 1, 'icount_shift': 3,
                        'supply': 'external', 'current': {'run_a': 0.05, 'reset_a': 0.02},
                        'brownout': {'v_trip': 2.43, 'v_release': 2.53}},
                'power': {'nodes': {}, 'sources': {}, 'branches': {}, 'regulators': {}},
                'mechanics': {'timestep_s': 0.0005, 'parts': {}, 'joints': {}, 'elements': {}, 'probes': {}},
                'actuators': {}, 'inputs': {}, 'bindings': [],
            }
            f.write_text(dump_yaml(data), encoding='utf8')
            for sub in ('scenarios', 'mechanics/meshes', 'pcb', 'firmware'):
                (root / sub).mkdir(parents=True, exist_ok=True)
            (root / '.gitignore').write_text('runs/\nprint/*.3mf\n')
        return cls(root)

    def save(self):
        # Atomic: the app, the MCP server and the user's editor may all touch this file.
        tmp = self.file.with_name(self.file.name + '.tmp')
        with _SAVE_LOCK:
            tmp.write_text(dump_yaml(self.data), encoding='utf8')
            os.replace(tmp, self.file)

    def get(self, path, default=KeyError):
        return get_path(self.data, path, default)

    def set(self, path, value):
        with _SAVE_LOCK:
            set_path(self.data, path, value)
            self.save()

    def path(self, rel):
        return (self.root / rel).resolve()

    def pcb(self):
        pcb = self.data.get('pcb')
        if not pcb or not pcb.get('summary'):
            return None
        out = load_yaml(self.path(pcb['summary']))
        out['placement'] = pcb.get('placement')
        return out

    def scenarios(self):
        d = self.root / 'scenarios'
        return sorted(p.stem for p in d.glob('*.yaml')) if d.exists() else []

    def scenario(self, name):
        p = self.root / 'scenarios' / f'{name}.yaml'
        if not p.exists():
            raise FileNotFoundError(f'scenario {name!r} not found; have {self.scenarios()}')
        s = load_yaml(p)
        s.setdefault('name', name)
        return s

    def resolved(self, overrides=None):
        """Deep copy of the project data with dotted-path overrides applied."""
        data = copy.deepcopy(self.data)
        for k, v in (overrides or {}).items():
            set_path(data, k, v)
        return data

    def fingerprint(self, data=None):
        blob = json.dumps(data if data is not None else self.data, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:12]
