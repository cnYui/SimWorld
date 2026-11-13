"""MuJoCo layer: builds the model from an optional base MJCF plus the parts,
joints and PCB placement recorded in the project file, applies actuator torques
and passive mechanism elements, and reports joint/contact signals."""
from __future__ import annotations

import math
from pathlib import Path

import mujoco
import numpy as np

EMPTY_MJCF = '<mujoco model="emcosim"><compiler angle="radian"/><worldbody/></mujoco>'
JOINT_TYPES = {'hinge': mujoco.mjtJoint.mjJNT_HINGE, 'slide': mujoco.mjtJoint.mjJNT_SLIDE,
               'ball': mujoco.mjtJoint.mjJNT_BALL, 'free': mujoco.mjtJoint.mjJNT_FREE}


def _quat_from_euler_deg(e):
    if e is None:
        return [1, 0, 0, 0]
    q = np.zeros(4)
    mujoco.mju_euler2Quat(q, np.radians(np.asarray(e, float)), 'xyz')
    return q.tolist()


def build_spec(project_dir, cfg, pcb=None):
    project_dir = Path(project_dir)
    if cfg.get('mjcf'):
        spec = mujoco.MjSpec.from_file(str(project_dir / cfg['mjcf']))
    else:
        spec = mujoco.MjSpec.from_string(EMPTY_MJCF)
    if 'timestep_s' in cfg:
        spec.option.timestep = float(cfg['timestep_s'])

    def body(name):
        if name in (None, 'world'):
            return spec.worldbody
        b = spec.body(name)
        if b is None:
            raise KeyError(f'body {name!r} not found in the mechanics model')
        return b

    for name, part in (cfg.get('parts') or {}).items():
        parent = body(part.get('parent', 'world'))
        b = parent.add_body(name=name, pos=part.get('pos', [0, 0, 0]),
                            quat=_quat_from_euler_deg(part.get('euler_deg')))
        mesh_dir = project_dir / part.get('dir', 'mechanics/meshes')
        spec.add_mesh(name=f'{name}_visual', file=str((mesh_dir / part['file']).resolve()))
        rgba = part.get('rgba', [0.8, 0.8, 0.82, 1])
        b.add_geom(name=f'{name}_visual', type=mujoco.mjtGeom.mjGEOM_MESH, meshname=f'{name}_visual',
                   rgba=rgba, contype=0, conaffinity=0, group=1)
        if part.get('collision_file') and part.get('collision', 'hull') != 'none':
            spec.add_mesh(name=f'{name}_hull', file=str((mesh_dir / part['collision_file']).resolve()))
            b.add_geom(name=f'{name}_collision', type=mujoco.mjtGeom.mjGEOM_MESH, meshname=f'{name}_hull',
                       rgba=[0.5, 0.5, 0.5, 0], group=3)
        # A body without a joint is welded to its parent; mass only matters once
        # define_joint gives it a degree of freedom.
        b.explicitinertial = True
        b.mass = max(float(part.get('mass_kg', 1e-3)), 1e-6)
        b.ipos = part.get('com_m', [0, 0, 0])
        I = np.asarray(part.get('inertia_kg_m2', np.eye(3) * 1e-7))
        b.fullinertia = [I[0, 0], I[1, 1], I[2, 2], I[0, 1], I[0, 2], I[1, 2]]

    for name, j in (cfg.get('joints') or {}).items():
        existing = spec.joint(name)
        if existing is None:
            b = body(j['body'])
            existing = b.add_joint(name=name, type=JOINT_TYPES[j.get('type', 'hinge')],
                                   axis=j.get('axis', [0, 0, 1]), pos=j.get('pos', [0, 0, 0]))
        if 'range_deg' in j:
            r = j['range_deg']
            if j.get('type', 'hinge') == 'hinge':
                # the spec compiles angles in its own unit: degrees unless the MJCF says radian
                existing.range = list(r) if spec.compiler.degree else [math.radians(r[0]), math.radians(r[1])]
            else:
                existing.range = r
            existing.limited = mujoco.mjtLimited.mjLIMITED_TRUE
        if 'range_mm' in j:
            r = j['range_mm']
            existing.range = [r[0] * 1e-3, r[1] * 1e-3]
            existing.limited = mujoco.mjtLimited.mjLIMITED_TRUE
        if 'range' in j:
            existing.range = j['range']
            existing.limited = mujoco.mjtLimited.mjLIMITED_TRUE
        for key, attr in (('damping', 'damping'), ('armature', 'armature'), ('frictionloss', 'frictionloss'),
                          ('stiffness', 'stiffness'), ('springref', 'springref')):
            if key in j:
                _set_scalar(existing, attr, j[key])

    for name, pr in (cfg.get('probes') or {}).items():
        if pr.get('type') == 'imu':
            b = body(pr.get('body'))
            if b is spec.worldbody:
                raise ValueError(f'IMU probe {name}: give the body (part) the sensor is mounted on')
            b.add_site(name=f'{name}_imu', pos=pr.get('pos', [0, 0, 0]), quat=_quat_from_euler_deg(pr.get('euler_deg')),
                       size=[0.002, 0, 0], group=4)
            spec.add_sensor(name=f'{name}_acc', type=mujoco.mjtSensor.mjSENS_ACCELEROMETER,
                            objtype=mujoco.mjtObj.mjOBJ_SITE, objname=f'{name}_imu')
            spec.add_sensor(name=f'{name}_gyro', type=mujoco.mjtSensor.mjSENS_GYRO,
                            objtype=mujoco.mjtObj.mjOBJ_SITE, objname=f'{name}_imu')

    if pcb is not None and pcb.get('placement') and pcb.get('board'):
        _add_pcb(spec, body(pcb['placement'].get('parent', 'world')), pcb)
    return spec


def _add_pcb(spec, parent, pcb):
    """Board slab plus one box per component, purely visual."""
    pl, board = pcb['placement'], pcb['board']
    t = board.get('thickness_mm', 1.6) * 1e-3
    b = parent.add_body(name='pcb', pos=pl.get('pos', [0, 0, 0]), quat=_quat_from_euler_deg(pl.get('euler_deg')))
    w, h = board['width_mm'] * 1e-3, board['height_mm'] * 1e-3
    x0, y0 = board['x_min'] * 1e-3, board['y_min'] * 1e-3
    b.add_geom(name='pcb_board', type=mujoco.mjtGeom.mjGEOM_BOX, size=[w / 2, h / 2, t / 2],
               pos=[x0 + w / 2, -(y0 + h / 2), -t / 2], rgba=[0.05, 0.42, 0.20, 1], contype=0, conaffinity=0, group=1)
    heights = pl.get('component_height_mm', {})
    for c in pcb.get('components', []):
        if c['ref'].startswith('H'):
            continue
        hz = heights.get(c['ref'][0], 3.0) * 1e-3
        b.add_geom(name=f"pcb_{c['ref']}", type=mujoco.mjtGeom.mjGEOM_BOX, size=[0.0022, 0.0022, hz / 2],
                   pos=[c['x_mm'] * 1e-3, -c['y_mm'] * 1e-3, hz / 2], rgba=[0.15, 0.15, 0.17, 1],
                   contype=0, conaffinity=0, group=1)


def _set_scalar(obj, attr, value):
    """Joint damping/stiffness are scalars in MuJoCo 3 and 3-vectors (polynomial
    coefficients) in MuJoCo 4; set the linear term in both."""
    try:
        setattr(obj, attr, value)
    except TypeError:
        cur = list(getattr(obj, attr))
        cur[0] = value
        setattr(obj, attr, cur)


class Mechanics:
    def __init__(self, project_dir, cfg, pcb=None):
        self.cfg = cfg
        self.spec = build_spec(project_dir, cfg, pcb)
        self.warnings = {}
        mujoco.set_mju_user_warning(lambda m: self.warnings.__setitem__(m, self.warnings.get(m, 0) + 1))
        self.model = self.spec.compile()
        self.data = mujoco.MjData(self.model)
        m = self.model
        self.joints = {}
        for i in range(m.njnt):
            name = m.joint(i).name
            if name:
                self.joints[name] = (int(m.jnt_qposadr[i]), int(m.jnt_dofadr[i]), int(m.jnt_type[i]))
        for name, deg in (cfg.get('initial_deg') or {}).items():
            qa, _, _ = self.joints[name]
            self.data.qpos[qa] = math.radians(deg)
        self.elements = [Bistable(m, self.joints, e) for e in (cfg.get('elements') or {}).values()
                         if e.get('type') == 'bistable']
        self.probes = {name: ContactProbe(m, p) for name, p in (cfg.get('probes') or {}).items()
                       if p.get('type', 'contact') == 'contact'}
        self.imus = {}
        for name, p in (cfg.get('probes') or {}).items():
            if p.get('type') == 'imu':
                acc = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_SENSOR, f'{name}_acc')
                gyr = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_SENSOR, f'{name}_gyro')
                self.imus[name] = (int(m.sensor_adr[acc]), int(m.sensor_adr[gyr]), p.get('noise_g', 0.0))
        self.drives = {}
        self.extra_torque = {}
        mujoco.mj_forward(m, self.data)

    @property
    def dt(self):
        return float(self.model.opt.timestep)

    def joint_state(self, name):
        qa, da, _ = self.joints[name]
        return float(self.data.qpos[qa]), float(self.data.qvel[da])

    def set_drive(self, joint, target, hz=15.0, zeta=1.0):
        """Make a joint follow target(t) -> (q, qd) (m or rad) with a stiff, critically damped
        servo plus gravity/bias compensation: a stand-in for the person or machine moving it."""
        if joint not in self.joints:
            raise KeyError(f'motion: no joint {joint!r}; joints: {sorted(self.joints)}')
        self.drives[joint] = (target, 2 * math.pi * hz, zeta)

    def step(self, torques):
        d = self.data
        d.qfrc_applied[:] = 0
        for name, tau in torques.items():
            d.qfrc_applied[self.joints[name][1]] += tau
        for name, (target, w, zeta) in self.drives.items():
            qa, da, _ = self.joints[name]
            q_t, qd_t = target(float(d.time))
            M = float(self.model.dof_M0[da]) or 1e-6         # joint-space inertia at qpos0
            d.qfrc_applied[da] += M * (w * w * (q_t - d.qpos[qa]) + 2 * zeta * w * (qd_t - d.qvel[da])) \
                + float(d.qfrc_bias[da]) - float(d.qfrc_passive[da])   # cancel the joint's own spring/damping
        for e in self.elements:
            e.apply(d)
        mujoco.mj_step(self.model, d)
        for p in self.probes.values():
            p.update(self.model, d)
        if not np.isfinite(d.qpos).all():
            raise RuntimeError('mechanics diverged (non-finite state)')

    def signals(self):
        out = {}
        for name, (qa, da, jt) in self.joints.items():
            if jt == mujoco.mjtJoint.mjJNT_HINGE:
                out[f'joint.{name}.deg'] = math.degrees(self.data.qpos[qa])
                out[f'joint.{name}.dps'] = math.degrees(self.data.qvel[da])
            elif jt == mujoco.mjtJoint.mjJNT_SLIDE:
                out[f'joint.{name}.mm'] = self.data.qpos[qa] * 1e3
                out[f'joint.{name}.mm_s'] = self.data.qvel[da] * 1e3
        for name, p in self.probes.items():
            out[f'probe.{name}.force_n'] = p.force
        for name, v in self.imu_values().items():
            for k, x in v.items():
                out[f'probe.{name}.{k}'] = x
        return out

    def imu_values(self):
        sd = self.data.sensordata
        out = {}
        for name, (a, g, _) in self.imus.items():
            ax, ay, az = (float(x) for x in sd[a:a + 3])
            gx, gy, gz = (math.degrees(float(x)) for x in sd[g:g + 3])
            out[name] = {'ax': ax, 'ay': ay, 'az': az, 'gx': gx, 'gy': gy, 'gz': gz,
                         'a_g': math.sqrt(ax * ax + ay * ay + az * az) / 9.80665}
        return out

    # -- viewer support --------------------------------------------------------
    def visual_bodies(self):
        m = self.model
        ids = sorted({int(m.geom_bodyid[g]) for g in range(m.ngeom) if m.geom_group[g] <= 2 and m.geom_rgba[g][3] > 0})
        return ids

    def poses(self, body_ids):
        return np.concatenate([self.data.xpos[body_ids], self.data.xquat[body_ids]], axis=1)


class Bistable:
    """Two stable positions at +/-stable_deg with a quartic potential; the peak
    restoring torque between them is peak_torque_nm (or peak_force_n*lever_m)."""

    def __init__(self, model, joints, cfg):
        self.dof = joints[cfg['joint']][1]
        self.qadr = joints[cfg['joint']][0]
        self.q0 = math.radians(float(cfg['stable_deg']))
        peak = cfg.get('peak_torque_nm')
        if peak is None:
            peak = float(cfg['peak_force_n']) * float(cfg['lever_m'])
        self.k = float(peak) * 3 * math.sqrt(3) / (2 * self.q0)

    def apply(self, d):
        q = float(d.qpos[self.qadr])
        d.qfrc_applied[self.dof] += self.k * q * (1 - (q / self.q0) ** 2)


class ContactProbe:
    """Sum of contact normal+friction force magnitude between two geom groups,
    selected by name prefix."""

    def __init__(self, model, cfg):
        a, b = cfg['geoms_a'], cfg['geoms_b']
        names = [model.geom(i).name for i in range(model.ngeom)]
        self.a = {i for i, n in enumerate(names) if n.startswith(a)}
        self.b = {i for i, n in enumerate(names) if n.startswith(b)}
        self.force = 0.0
        self.peak = 0.0
        self._f = np.zeros(6)

    def update(self, model, d):
        total = 0.0
        for i in range(d.ncon):
            c = d.contact[i]
            g1, g2 = int(c.geom1), int(c.geom2)
            if (g1 in self.a and g2 in self.b) or (g1 in self.b and g2 in self.a):
                mujoco.mj_contactForce(model, d, i, self._f)
                total += float(np.linalg.norm(self._f[:3]))
        self.force = total
        self.peak = max(self.peak, total)
