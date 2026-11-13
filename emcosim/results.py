"""Run storage: runs/<run_id>/ holds
  run.json        scenario, overrides, project fingerprint, summary, checks
  signals.npz     1 ms samples of every signal
  uart.log        ESP32 serial console with virtual-time stamps
  scene.json/.bin geometry for the web viewer, frames.bin body poses
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import mujoco
import numpy as np

GEOM_TYPES = {int(mujoco.mjtGeom.mjGEOM_PLANE): 'plane', int(mujoco.mjtGeom.mjGEOM_SPHERE): 'sphere',
              int(mujoco.mjtGeom.mjGEOM_CAPSULE): 'capsule', int(mujoco.mjtGeom.mjGEOM_ELLIPSOID): 'ellipsoid',
              int(mujoco.mjtGeom.mjGEOM_CYLINDER): 'cylinder', int(mujoco.mjtGeom.mjGEOM_BOX): 'box',
              int(mujoco.mjtGeom.mjGEOM_MESH): 'mesh'}


def new_run_id(scenario_name):
    return time.strftime('%Y%m%d-%H%M%S') + f'-{int(time.time() * 1000) % 1000:03d}-{scenario_name}'


class Recorder:
    def __init__(self, run_dir):
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.t = []
        self.rows = []
        self.keys = None
        self.frames = []
        self.frame_t = []
        self.qpos = []

    def begin(self, sim, body_ids):
        write_scene(sim.mech.model, body_ids, self.dir)

    def sample(self, t, signals, poses, qpos=None):
        if self.keys is None:
            self.keys = list(signals)
        self.t.append(t)
        self.rows.append([signals.get(k, np.nan) for k in self.keys])
        if poses is not None:
            self.frames.append(poses.astype(np.float32))
            self.frame_t.append(t)
            self.qpos.append(qpos)

    def columns(self):
        arr = np.asarray(self.rows, dtype=np.float64)
        return np.asarray(self.t), {k: arr[:, i] for i, k in enumerate(self.keys)}

    def finish(self, meta, uart, mcu_events):
        t, cols = self.columns()
        np.savez_compressed(self.dir / 'signals.npz', t=t, **{k.replace('/', '_'): v for k, v in cols.items()})
        if self.frames:
            frames = np.stack(self.frames)
            header = np.array([len(self.frames), frames.shape[1]], dtype=np.int32)
            with open(self.dir / 'frames.bin', 'wb') as f:
                f.write(header.tobytes())
                f.write(np.asarray(self.frame_t, dtype=np.float32).tobytes())
                f.write(frames.tobytes())
            np.savez_compressed(self.dir / 'qpos.npz', t=np.asarray(self.frame_t), qpos=np.stack(self.qpos))
        (self.dir / 'uart.log').write_text(''.join(f'[{ts:9.3f} s] {line}\n' for ts, line in uart),
                                          encoding='utf8')
        meta['mcu_events'] = [{'t': ts, 'event': n, 'detail': r} for ts, n, r in mcu_events]
        (self.dir / 'run.json').write_text(json.dumps(meta, indent=2, ensure_ascii=False, default=_json_default),
                                           encoding='utf8')


def _json_default(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


def write_scene(model, body_ids, out_dir, skip=None):
    """Visual geometry for the viewer: one entry per visible geom, in its body frame.
    skip(geom_name) -> True leaves a geom out (the 建模 page hides the PCB)."""
    index = {b: i for i, b in enumerate(body_ids)}
    geoms, blobs, offset = [], [], 0
    mesh_ref = {}
    for g in range(model.ngeom):
        b = int(model.geom_bodyid[g])
        if b not in index or model.geom_group[g] > 2 or model.geom_rgba[g][3] <= 0:
            continue
        if skip and skip(model.geom(g).name):
            continue
        gt = GEOM_TYPES.get(int(model.geom_type[g]))
        if gt is None:
            continue
        entry = {'body': index[b], 'type': gt, 'size': model.geom_size[g].round(6).tolist(),
                 'pos': model.geom_pos[g].round(7).tolist(), 'quat': model.geom_quat[g].round(7).tolist(),
                 'rgba': model.geom_rgba[g].round(3).tolist(), 'name': model.geom(g).name}
        if gt == 'mesh':
            mid = int(model.geom_dataid[g])
            if mid not in mesh_ref:
                va, vn = model.mesh_vertadr[mid], model.mesh_vertnum[mid]
                fa, fn = model.mesh_faceadr[mid], model.mesh_facenum[mid]
                v = model.mesh_vert[va:va + vn].astype(np.float32).tobytes()
                f = model.mesh_face[fa:fa + fn].astype(np.uint32).tobytes()
                mesh_ref[mid] = {'v_off': offset, 'v_n': int(vn), 'f_off': offset + len(v), 'f_n': int(fn)}
                blobs += [v, f]
                offset += len(v) + len(f)
            entry['mesh'] = mesh_ref[mid]
        geoms.append(entry)
    out_dir = Path(out_dir)
    (out_dir / 'scene.bin').write_bytes(b''.join(blobs))
    bodies = [model.body(b).name for b in body_ids]
    (out_dir / 'scene.json').write_text(json.dumps({'bodies': bodies, 'geoms': geoms}), encoding='utf8')


class RunStore:
    def __init__(self, project_root):
        self.root = Path(project_root) / 'runs'

    def list(self):
        if not self.root.exists():
            return []
        out = []
        for d in sorted(self.root.iterdir(), reverse=True):
            f = d / 'run.json'
            if f.exists():
                meta = json.loads(f.read_text(encoding='utf8'))
                out.append({'run_id': d.name, 'scenario': meta.get('scenario', {}).get('name'),
                            'passed': meta.get('passed'), 'overrides': meta.get('overrides'),
                            'created': meta.get('created')})
        return out

    def meta(self, run_id):
        return json.loads((self.root / run_id / 'run.json').read_text(encoding='utf8'))

    def signals(self, run_id, names=None, t0=None, t1=None, max_points=2000):
        z = np.load(self.root / run_id / 'signals.npz')
        t = z['t']
        keys = [k for k in z.files if k != 't']
        if names:
            missing = [n for n in names if n not in keys]
            if missing:
                raise KeyError(f'unknown signals {missing}; available: {keys}')
            keys = names
        sel = np.ones_like(t, dtype=bool)
        if t0 is not None:
            sel &= t >= t0
        if t1 is not None:
            sel &= t <= t1
        idx = np.nonzero(sel)[0]
        if len(idx) > max_points:
            idx = idx[np.linspace(0, len(idx) - 1, max_points).astype(int)]
        return {'t': t[idx].round(4).tolist(), **{k: z[k][idx].round(6).tolist() for k in keys}}

    def signal_names(self, run_id):
        z = np.load(self.root / run_id / 'signals.npz')
        return [k for k in z.files if k != 't']

    def dir(self, run_id):
        d = self.root / run_id
        if not d.exists():
            raise FileNotFoundError(f'run {run_id} not found')
        return d
