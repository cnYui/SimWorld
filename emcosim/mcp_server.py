"""MCP server: lets an AI agent import, annotate, run, tweak and re-run.

    emcosim mcp --project examples/switch_robot      (stdio transport)

Tools edit project.yaml in place (so every change is a git diff) and write run
results under runs/.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

try:                                    # mcp >= 2
    from mcp.server.mcpserver import Image, MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:                     # mcp 1.x
    from mcp.server.fastmcp import FastMCP, Image
    from mcp.server.fastmcp.exceptions import ToolError

from . import api, calibrate as cal
from .project import Project, dump_yaml
from .toolpaths import find_ngspice


def build_server(project_root=None, link=None):
    """project_root: project used when no SimWorld page is running (the page's open project wins
    when one is). link: app.link.AppLink, read from the live file when None."""
    from .app.link import AppLink
    state = {'root': Path(project_root).resolve() if project_root else None}
    mcp = FastMCP('emcosim', instructions=(
        'SimWorld: electronics + physics co-simulation. The tools act on the project open in the '
        "user's SimWorld browser page: current_project shows it; open_project / open_example / "
        'new_project switch the page to another project. Workflow: get_project -> '
        'import_pcb / import_model / import_firmware -> define_joint / bind_pin -> '
        'run_scenario -> get_results -> set_param or sweep -> run_scenario again. '
        'Parameters are dotted paths into project.yaml (e.g. power.sources.adapter.r_ohm, '
        'actuators.servo1.preset, mechanics.elements.rocker_detent.peak_force_n, '
        'firmware.press_angle_deg). All model values marked "assumed" in project.yaml are '
        'not measurements.'))

    tool = _reporting_tool(mcp)

    def page():
        return link if link is not None else AppLink()

    def page_root():
        """Project open in the running page: Path, None (page has none), or False (no page)."""
        try:
            r = page().call('GET', 'app', timeout=3).get('project')
        except AppLink.Unavailable:
            return False
        return Path(r) if r else None

    def prj():
        r = page_root()
        root = r if r else state['root']
        if root is None:
            raise ValueError('No project is open. Use open_example, open_project or new_project first '
                             '(list_examples shows the bundled examples).')
        state['root'] = root
        return Project(root)

    def switch(method, body):
        """Open a project in the page when it runs, otherwise only here."""
        try:
            res = page().call('POST', method, body, timeout=120)
            state['root'] = Path(res['root'])
            return {**res, 'page': 'switched'}
        except AppLink.Unavailable:
            return None

    @tool()
    def current_project() -> dict:
        """Which project the tools act on, and whether the user's SimWorld page is connected."""
        r = page_root()
        root = r if r else state['root']
        out = {'project': str(root) if root else None, 'page_connected': r is not False,
               'page_url': page().page_url if r is not False else None}
        if root:
            p = Project(root)
            out['name'] = p.data.get('name') or root.name
            out['scenarios'] = p.scenarios()
        else:
            out['hint'] = 'No project open: open_example, open_project(path) or new_project(path).'
        return out

    @tool()
    def list_examples() -> list:
        """Example projects bundled with SimWorld (open one with open_example)."""
        from .app.server import _bundled_examples
        return [{'name': p.name, 'path': str(p)} for p in _bundled_examples()]

    @tool()
    def open_example(name: str = 'switch_robot', dest: str | None = None) -> dict:
        """Copy a bundled example to Documents/emcosim (or dest) and open it in the page."""
        from .app.server import _bundled_examples, _documents_dir
        ex = {p.name: p for p in _bundled_examples()}
        if name not in ex:
            raise ValueError(f'no example {name!r}; examples: {sorted(ex)}')
        res = switch('project/open_example', {'path': str(ex[name]), **({'dest': dest} if dest else {})})
        if res:
            return res
        out = api.copy_project(ex[name], Path(dest) if dest else _documents_dir() / name)
        state['root'] = Path(out['root'])
        return {'root': out['root'], 'page': 'not running (start it with: emcosim serve)'}

    @tool()
    def open_project(path: str) -> dict:
        """Open a project folder (one that has project.yaml) and show it in the page."""
        root = Path(path).expanduser().resolve()
        if not (root / 'project.yaml').exists():
            raise ValueError(f'{root} has no project.yaml')
        res = switch('project/open', {'path': str(root)})
        if res:
            return res
        state['root'] = root
        return {'root': str(root), 'page': 'not running (start it with: emcosim serve)'}

    @tool()
    def new_project(path: str, name: str | None = None) -> dict:
        """Create an empty project in a folder (created if missing) and open it in the page.
        Then import_pcb / board_import, import_model and add scenarios."""
        res = switch('project/new', {'path': path, **({'name': name} if name else {})})
        if res:
            return res
        p = Project.create(path, name)
        state['root'] = p.root
        return {'root': str(p.root), 'page': 'not running (start it with: emcosim serve)'}

    @tool()
    def get_project(path: str | None = None) -> dict:
        """Overview of the project (scenarios, PCB, firmware, joints, actuators, bindings, power,
        recent runs), or the value at one dotted parameter path."""
        p = prj()
        return api.project_overview(p) if not path else {'path': path, 'value': api.get_param(p, path)}

    @tool()
    def import_pcb(path: str, dsn: str | None = None, bom: str | None = None,
                   placement: dict | None = None) -> dict:
        """Import a KiCad .kicad_pcb (EasyEDA: export to KiCad first; an EasyEDA Pro .epcb plus its
        Specctra .dsn also works through a limited converter). Returns nets, bulk capacitance per
        net and copper resistance per net."""
        return api.import_pcb(prj(), path, placement=placement, dsn=dsn, bom=bom)

    @tool()
    def import_model(path: str, name: str | None = None, parent: str = 'world',
                     pos: list[float] | None = None, euler_deg: list[float] | None = None,
                     units: str = 'mm', density: float | None = None, mass_kg: float | None = None,
                     collision: str = 'hull') -> dict:
        """Import an STL/STEP/OBJ part as a body under `parent` (a .xml MJCF instead becomes the base
        mechanical model). pos is in metres relative to the parent, euler_deg in degrees (xyz); `units`
        is the unit of the mesh file. mass_kg or density (kg/m3) sets the mass; with neither, the mass is
        estimated from the solid volume and flagged as assumed. Importing a name that exists replaces it.
        collision: 'hull' (convex hull) or 'none'. To make simple shapes without a file use add_shape."""
        return api.import_model(prj(), path, name=name, parent=parent, pos=pos, euler_deg=euler_deg,
                                units=units, density=density, mass_kg=mass_kg, collision=collision)

    @tool()
    def import_firmware(path: str, name: str = 'firmware') -> dict:
        """Import ESP32 firmware: an ESP-IDF build directory (with flasher_args.json) or a merged
        flash .bin. Lists the tunable parameters stored in its fw_params partition."""
        return api.import_firmware(prj(), path, name=name)

    @tool()
    def define_joint(name: str, body: str, type: str = 'hinge', axis: list[float] | None = None,
                     pos: list[float] | None = None, range_deg: list[float] | None = None,
                     range_mm: list[float] | None = None, damping: float | None = None,
                     armature: float | None = None, stiffness: float | None = None,
                     frictionloss: float | None = None) -> dict:
        """Give a part a degree of freedom relative to its parent (or annotate an existing MJCF joint).
        type 'hinge' rotates about `axis` (range_deg, degrees; stiffness N*m/rad, damping N*m*s/rad);
        'slide' moves along `axis` (range_mm, millimetres; stiffness N/m, damping N*s/m). pos is the
        joint origin in the body frame, metres. A part without a joint is welded to its parent."""
        return api.define_joint(prj(), name, body, type=type, axis=axis, pos=pos, range_deg=range_deg,
                                range_mm=range_mm, damping=damping, armature=armature, stiffness=stiffness,
                                frictionloss=frictionloss)

    @tool()
    def bind_pin(pin: str, actuator: str | None = None, role: str | None = None,
                 input: str | None = None, net: str | None = None, actuator_type: str | None = None,
                 joint: str | None = None, supply_node: str | None = None, preset: str | None = None,
                 active: str = 'low', input_type: str | None = None, knob_range_deg: list[float] | None = None,
                 value: float | None = None) -> dict:
        """Wire an ESP32 pin.
        Output: pin -> actuator (servo signal, or dc_motor role in_a/in_b); creating the actuator needs
        actuator_type ('servo'|'dc_motor'), joint, supply_node and optionally preset ('sg90', 'mg90s',
        'tt_motor').
        Input: input='<name>' wires an input device to the pin. input_type 'button' (default; active
        'low'|'high', pressed by scenario events) or 'pot' (potentiometer on an ADC pin, GPIO32-39:
        follows `joint`'s angle over knob_range_deg [min, max], or a fixed `value` 0..1 that scenario
        events can set with {t, set: inputs.<name>.value, value}).
        `net` is checked against the PCB. Unusable pins are refused; risky ones come back as warnings.
        Note: QEMU firmware sees buttons only; analog inputs and IMUs reach the behavior backend."""
        extra = {k: v for k, v in (('input_type', input_type), ('range_deg', knob_range_deg), ('value', value))
                 if v is not None}
        return api.bind_pin(prj(), pin, actuator=actuator, role=role, input=input, net=net,
                            actuator_type=actuator_type, joint=joint, supply_node=supply_node,
                            preset=preset, active=active, **extra)

    @tool()
    def define_probe(name: str, type: str, body: str | None = None, pos: list[float] | None = None,
                     euler_deg: list[float] | None = None, geoms_a: str | None = None,
                     geoms_b: str | None = None) -> dict:
        """Add a measurement to the mechanical model.
        type 'imu': accelerometer + gyroscope mounted on `body` at pos (metres, body frame) with
        orientation euler_deg; signals probe.<name>.ax/ay/az (m/s2, gravity included like a real
        accelerometer), gx/gy/gz (deg/s), a_g (|a| in g); metrics a_g.peak/min; the behavior backend
        reads it with io.imu(name).
        type 'contact': force between geoms whose names start with geoms_a and geoms_b (part names work);
        signal probe.<name>.force_n, metric force_n.peak."""
        p = prj()
        if type == 'imu':
            if not body:
                raise ValueError('an IMU probe needs body (the part it is mounted on)')
            cfg = {'type': 'imu', 'body': body, **({'pos': pos} if pos else {}),
                   **({'euler_deg': euler_deg} if euler_deg else {})}
        elif type == 'contact':
            if not (geoms_a and geoms_b):
                raise ValueError('a contact probe needs geoms_a and geoms_b (geom name prefixes, e.g. part names)')
            cfg = {'type': 'contact', 'geoms_a': geoms_a, 'geoms_b': geoms_b}
        else:
            raise ValueError("probe type: 'imu' or 'contact'")
        probes = p.data.setdefault('mechanics', {}).setdefault('probes', {})
        old = probes.get(name)
        probes[name] = cfg
        from .mechanics import Mechanics
        try:
            mech = Mechanics(p.root, p.data['mechanics'], p.pcb())
        except Exception as e:
            if old is None:
                probes.pop(name)
            else:
                probes[name] = old
            raise ValueError(f'MuJoCo rejected the probe, nothing saved: {e}') from e
        if type == 'contact':
            pr = mech.probes[name]
            if not pr.a or not pr.b:
                probes.pop(name) if old is None else probes.__setitem__(name, old)
                names = sorted({mech.model.geom(i).name for i in range(mech.model.ngeom) if mech.model.geom(i).name})
                raise ValueError(f'no geoms start with {geoms_a if not pr.a else geoms_b!r}; geoms: {names}')
        p.save()
        sig = ['ax', 'ay', 'az', 'gx', 'gy', 'gz', 'a_g'] if type == 'imu' else ['force_n']
        return {'probe': name, **cfg, 'signals': [f'probe.{name}.{k}' for k in sig]}

    @tool()
    def write_behavior(name: str, code: str) -> dict:
        """Save a Python model of the firmware as behavior/<name>.py and check that it loads. It defines
        setup(io) (optional, at each boot) and loop(io, t) (every 1 ms). io: io.read(pin) 0/1,
        io.adc(pin) 0..4095, io.volts(pin), io.imu(name) -> {ax, ay, az, gx, gy, gz, a_g}, io.write(pin, 0/1),
        io.pwm(pin, duty, freq), io.servo(pin, pulse_us), io.log(text), io.event(name, **detail),
        io.export(rows, name='export.csv'), io.state (dict, cleared at boot), io.t, io.params.
        Run it with a scenario that has mcu_backend: behavior and behavior_script: behavior/<name>.py.
        Use it for logic QEMU cannot run yet (ADC, IMU over I2C, radio); then port it to the firmware."""
        from .mcu.behavior import check_script
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
            raise ValueError('name: letters, digits and _ only')
        p = prj()
        d = p.root / 'behavior'
        d.mkdir(exist_ok=True)
        f = d / f'{name}.py'
        old = f.read_text(encoding='utf-8') if f.exists() else None
        f.write_text(code, encoding='utf-8')
        try:
            out = check_script(f)
        except Exception:
            if old is None:
                f.unlink()
            else:
                f.write_text(old, encoding='utf-8')
            raise
        return {**out, 'scenario_keys': {'mcu_backend': 'behavior', 'behavior_script': f'behavior/{name}.py'}}

    @tool()
    def set_param(path: str, value: Any) -> dict:
        """Set one parameter (dotted path into project.yaml; 'firmware.X' means the firmware's
        fw_params field X). Persists to project.yaml. Returns old and new value."""
        return api.set_param(prj(), path, value)

    @tool()
    def write_scenario(name: str, scenario_yaml: str) -> dict:
        """Create or replace scenarios/<name>.yaml. Keys (times in seconds):
        description, duration_s, overrides {dotted.path: value},
        events: [{t, press: <input>, hold_s} | {t, release: <input>} |
                 {t, set: power.sources.<name>.v|r_ohm|enabled, value} | {t, set: inputs.<pot>.value, value}],
        motions: [{joint, profile: reps|sine|hold, amplitude_mm|amplitude_deg, period_s, count, start_s,
                   rest_s, offset_mm|offset_deg}]  (moves a joint along a path, e.g. a barbell doing reps),
        expect: [{metric: <name>, <op>: value} | {event: <name>, <op>: count} |
                 {signal: <name>, <op>: value, at: t | window: [t0, t1], stat: max|min|mean|final}]
                 with <op> one of eq ne lt le gt ge,
        mcu_backend: qemu (default, real firmware) | scripted (pin_script, no firmware) |
                     behavior (a Python model of the firmware, see behavior_script),
        pin_script: [{t, pin: GPIO18, pulse_us, freq} | {t, pin, duty, freq} | {t, pin, level: 0|1} |
                     {t, pin, off: true}]  (scripted backend; it does not read inputs),
        behavior_script: path to a .py with setup(io) and loop(io, t) (behavior backend).
        Signal names come from get_results(run_id).signals_available."""
        return api.write_scenario(prj(), name, load_yaml_text(scenario_yaml))

    @tool()
    def run_scenario(name: str, overrides: dict | None = None) -> dict:
        """Run a scenario (firmware in QEMU, power + motor models, MuJoCo, 1 ms lock-step).
        overrides: temporary {path: value} for this run only. Returns checks, metrics, MCU log."""
        return api.run_scenario(prj(), name, overrides)

    @tool()
    def sweep(scenario: str, params: dict[str, list], metrics: list[str] | None = None,
              overrides: dict | None = None) -> dict:
        """Run the scenario for every combination of params ({path: [values]}); returns one row per
        run with pass/fail and the requested metrics (e.g. mcu.resets, node.v5_servo.min_v)."""
        return api.sweep(prj(), scenario, params, metrics=metrics, extra_overrides=overrides)

    @tool()
    def strength_check(part: str, run_id: str | None = None) -> dict:
        """FEA strength check of a printed part (printing.parts.<part>.strength config) with the loads
        of a run (newest if omitted): contact-force peak and servo stall torque. Uses the part's
        material and print settings. Returns the minimum safety factor per failure mode (in-plane,
        between layers); the page's 仿真 tab shows the result with a stress-coloured 3D view.
        Takes ~20-60 s."""
        from . import checks
        return checks.strength_check(prj(), part, run_id)

    @tool()
    def gear_check(run_id: str | None = None) -> dict:
        """Bending stress in each servo's output gear tooth (Lewis) for the peak torque of a run and
        the stall torque, with safety factors. Gear geometry defaults are assumed per servo model;
        override with actuators.<name>.gear.{module_mm, teeth, face_width_mm, allow_mpa}."""
        from . import checks
        return checks.gear_check(prj(), run_id)

    @tool()
    def get_results(run_id: str | None = None, signals: list[str] | None = None,
                    t0: float | None = None, t1: float | None = None, max_points: int = 300) -> dict:
        """Summary of a run (latest if run_id is omitted; 'list' lists runs). With `signals`,
        returns those time series (decimated to max_points)."""
        return api.get_results(prj(), run_id, signals, t0, t1, max_points)

    @tool()
    def render_frames(run_id: str, times: list[float], width: int = 640, height: int = 480,
                      azimuth: float | None = None, elevation: float | None = None,
                      distance: float | None = None, target: str | None = None) -> list:
        """Render MuJoCo images of a run at the given times (max 6), on a floor. target: a body name to
        aim the camera at (e.g. the part you are checking); distance in metres. For the model without a
        run use render_model. Needs an OpenGL backend (MUJOCO_GL=egl or osmesa on headless machines)."""
        from . import modeling
        from .results import RunStore
        p = prj()
        store = RunStore(p.root)
        import numpy as np
        z = np.load(store.dir(run_id) / 'qpos.npz')
        frames = []
        for t in times[:6]:
            i = int(np.argmin(np.abs(z['t'] - t)))
            out = modeling.render(p, store.dir(run_id) / 'frames' / f'frame_{z["t"][i]:07.3f}s.png', width, height,
                                  target=target, azimuth=azimuth, elevation=elevation, distance=distance,
                                  qpos=z['qpos'][i])
            frames.append(out['png'])
        meta = {'run_id': run_id, 'frames': frames, 'camera': out['camera']}
        return [json.dumps(meta)] + [Image(path=f) for f in frames]

    @tool()
    def calibrate_power(node: str, source: str, i_step_a: float = 0.7, apply: bool = False) -> dict:
        """Offline ngspice calibration: compares the equivalent power model with a SPICE transient
        that includes capacitor ESR and lead inductance, fits the node capacitance and source
        resistance; apply=True writes them to project.yaml."""
        p = prj()
        r = cal.calibrate(p.data['power'], node, source, i_step=i_step_a, apply_to=p if apply else None)
        r.pop('netlist', None)
        return r

    @tool()
    def fit_measurements(path: str, apply: bool = False) -> dict:
        """Fit servo (R, Ke, gear efficiency, idle current, friction, speed limit, backlash/stiffness),
        supply (v, r_ohm, i_limit_a) and bistable switch (peak_force_n, stable_deg) parameters from a
        measurement sheet (measurements/template.yaml). Returns fitted values ± 1σ, residuals before/after,
        changes old→new, and parameters kept at their prior (with the reason). apply=True writes
        project.yaml."""
        return api.fit_measurements(prj(), path, apply=apply)

    @tool()
    def compare_trace(trace_csv: str, signal: str, run_id: str | None = None, column: str | None = None,
                      time_unit: str = 's', t_offset: float = 0.0, align: bool = True,
                      scale: float = 1.0) -> dict:
        """RMS/max/mean error of a run's signal against a measured CSV trace; align searches the
        time offset."""
        return api.compare_trace(prj(), trace_csv, signal, run_id=run_id, column=column, time_unit=time_unit,
                                 t_offset=t_offset, align=align, scale=scale)

    @tool()
    def send_to_bambu_studio(parts: list[str] | None = None, with_settings: bool = False,
                             open_app: bool = True) -> dict:
        """Export printable parts (printing.parts in project.yaml, e.g. press_arm, frame,
        servo_cradle) into one 3MF and open it in Bambu Studio on this computer. with_settings
        embeds the suggested per-part print settings (experimental)."""
        from . import printing
        return printing.send_parts(prj(), parts or None, with_settings, open_app=open_app)

    # ------------------------------------------------------------- board (KiCad kernel)
    MISSING_BOARD = {
        'kicad_pcb': 'This project has no PCB yet (board.kicad_pcb). Create one from the schematic with '
                     'board_from_schematic, or bring an existing board in with board_import.',
        'kicad_sch': 'This project has no schematic yet (board.kicad_sch). Create it with schematic_create '
                     '(components + nets), or pass schematic=<file.kicad_sch> to board_import. Importing only '
                     'a .kicad_pcb does not bring a schematic.',
    }

    def board_file(key='kicad_pcb'):
        p = prj()
        b = p.data.get('board') or {}
        if not b.get(key):
            raise ValueError(MISSING_BOARD.get(key, f'project.yaml has no board.{key}'))
        f = p.root / b[key]
        if not f.exists():
            raise FileNotFoundError(f)
        return f

    def board_parts(p):
        """{ref: {value, lcsc, footprint}} from the schematic, else the board file, else the netlist."""
        from . import eda
        from .importers.kicad import load_kicad_pcb
        b = p.data.get('board') or {}
        if b.get('kicad_sch') and (p.root / b['kicad_sch']).exists():
            nl = eda.netlist(p.root / b['kicad_sch'])
            out = {}
            for c in nl['components']:
                f = c.get('fields') or {}
                lcsc = next((v for k, v in f.items() if k.lower() in ('lcsc', 'lcsc part', 'jlcpcb part')), None)
                out[c['ref']] = {'value': c.get('value'), 'footprint': c.get('footprint'), 'lcsc': lcsc}
            return out, 'schematic', nl['nets']
        if b.get('kicad_pcb') and (p.root / b['kicad_pcb']).exists():
            pcb = load_kicad_pcb(p.root / b['kicad_pcb'])
            out = {c['ref']: {'value': c['value'], 'footprint': c['footprint'], 'lcsc': c.get('lcsc')}
                   for c in pcb['components']}
            if b.get('netlist'):                # LCSC numbers the board file does not carry
                try:
                    for ref, c in eda.read_netlist(p.path(b['netlist']))['components'].items():
                        if ref in out and not out[ref]['lcsc'] and c.get('lcsc'):
                            out[ref]['lcsc'] = c['lcsc']
                except (OSError, ValueError):
                    pass
            return out, 'board', pcb['nets']
        if b.get('netlist'):
            nl = eda.read_netlist(p.path(b['netlist']))
            return ({r: {'value': c.get('value'), 'footprint': c.get('footprint'), 'lcsc': c.get('lcsc')}
                     for r, c in nl['components'].items()}, 'netlist', nl['nets'])
        raise ValueError('This project has no schematic, board or netlist yet: start with schematic_create '
                         '(or board_import for an existing board).')

    @tool()
    def board_status() -> dict:
        """Which board files the project uses and whether KiCad and ngspice are available."""
        import shutil
        from . import eda
        return {'board': prj().data.get('board'), 'kicad': eda.status(), 'ngspice': find_ngspice()}

    @tool()
    def board_import(path: str, netlist: str | None = None, name: str | None = None,
                     schematic: str | None = None) -> dict:
        """Bring an existing board in through KiCad's importers: .kicad_pcb as is, or EasyEDA Pro .epro
        (export it from 嘉立创EDA: File > Export > project), Altium .PcbDoc, Eagle .brd. `netlist` is that
        EDA's netlist export (EasyEDA: Protel 2.0 .net) and fills in pad nets the importer misses;
        `schematic` is the board's KiCad .kicad_sch (enables board_erc). Writes pcb/<name>/<name>.kicad_pcb
        plus JLCPCB DRC rules. To design a board from nothing use schematic_create and board_from_schematic."""
        from . import eda
        import shutil
        p = prj()
        root = p.root
        src = Path(path).expanduser().resolve()
        if not src.exists():
            raise FileNotFoundError(src)
        name = name or re.sub(r'[^A-Za-z0-9_-]+', '_', src.stem).strip('_').lower()
        out_dir = root / 'pcb' / name
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / f'{name}.kicad_pcb'
        if src.suffix.lower() == '.kicad_pcb':
            shutil.copy2(src, dst)
            res = {'file': str(dst)}
        else:
            res = eda.import_board(src, dst, netlist=netlist)
        entry = {'kicad_pcb': dst.relative_to(root).as_posix()}
        if netlist:
            nl = Path(netlist).expanduser().resolve()
            entry['netlist'] = nl.relative_to(root).as_posix() if nl.is_relative_to(root) else str(nl)
        if schematic:
            sch = Path(schematic).expanduser().resolve()
            if not sch.exists():
                raise FileNotFoundError(sch)
            shutil.copy2(sch, out_dir / f'{name}.kicad_sch')
            entry['kicad_sch'] = (out_dir / f'{name}.kicad_sch').relative_to(root).as_posix()
        p.data['board'] = {**(p.data.get('board') or {}), **entry}
        p.save()
        return {**res, 'board': p.data['board'], 'drc': eda.drc(dst, limit=15)}

    @tool()
    def board_drc(limit: int = 40, severity: str | None = None, types: list[str] | None = None) -> dict:
        """KiCad design rule check of the project's board (JLCPCB two-layer limits). Returns counts,
        by_type (count per finding type) and findings with positions in mm, errors first. severity
        ('error'|'warning') and types (e.g. ['clearance', 'unconnected_items']) filter the list.
        board_render(show_drc=true) draws them on the board."""
        from . import eda
        return eda.drc(board_file(), limit=limit, severity=severity, types=types)

    # ------------------------------------------------------- design from nothing (KiCad libraries)
    @tool()
    def symbol_search(query: str, limit: int = 20) -> list:
        """Search KiCad's installed symbol libraries (words must all match name, description or
        keywords), e.g. "ESP32-PICO", "LDO 3.3", "LSM6DS3", "USB C receptacle", "potentiometer".
        Returns Library:Name ids for schematic_create, with the default footprint if the symbol has one."""
        from .design import kicad_libs
        hits = kicad_libs.search_symbols(query, limit)
        return {'symbols': hits, **({} if hits else {'hint': 'no match: try fewer or shorter words, '
                                                             'e.g. the part family ("LSM6DS3", "AMS1117")'})}

    @tool()
    def symbol_info(symbol: str) -> dict:
        """Pins of a library symbol (number, name, electrical type) and its default footprint, so nets
        can be written as REF.PIN. symbol: Library:Name from symbol_search."""
        from .design import kicad_libs
        return kicad_libs.symbol_info(symbol)

    @tool()
    def footprint_search(query: str, limit: int = 20) -> dict:
        """Search KiCad's footprint libraries by words in Library:Name, e.g. "R_0402", "SOT-23-5",
        "QFN-48 7x7", "USB_C 6P", "JST_SH 1x02". Returns ids and the pads of the first match."""
        from .design import kicad_libs
        ids = kicad_libs.search_footprints(query, limit)
        out = {'footprints': ids}
        if ids:
            out['pads_of_first'] = kicad_libs.footprint_pads(ids[0])
        return out

    def _design_dir(p, name=None):
        b = p.data.get('board') or {}
        cur = b.get('kicad_sch') or b.get('kicad_pcb')
        if cur and not name:
            return (p.root / cur).parent, Path(cur).stem
        name = re.sub(r'[^A-Za-z0-9_-]+', '_', name or p.data.get('name') or 'board').strip('_').lower() or 'board'
        if not re.search(r'[a-z]', name):
            name = 'board'
        return p.root / 'pcb' / name, name

    @tool()
    def schematic_create(components: list[dict], nets: dict[str, list[str]], name: str | None = None) -> list:
        """Write the project's KiCad schematic from parts and nets (replaces an existing one; then run
        board_from_schematic to bring the board in line).
        components: [{"ref": "U1", "symbol": "MCU_Espressif:ESP32-PICO-V3-02", "value": "...",
                      "footprint": "Lib:Name" (needed when the symbol has none), "lcsc": "C908392",
                      "fields": {...}}]
        nets: {"3V3": ["U1.3", "C3.1", ...], "GND": [...]}: pins as REF.PIN (pin number, or a unique pin
        name). Pins in no net get no-connect flags; power nets without a source get PWR_FLAG.
        Runs KiCad ERC, checks KiCad's own netlist against `nets`, and returns a picture."""
        from . import eda
        from .design import schematic
        from .design.render import schematic_png
        p = prj()
        d, stem = _design_dir(p, name)
        sch = d / f'{stem}.kicad_sch'
        report = schematic.write(sch, components, nets, project_name=stem, title=p.data.get('name'))
        p.data['board'] = {**(p.data.get('board') or {}), 'kicad_sch': sch.relative_to(p.root).as_posix()}
        p.data['board'].pop('netlist', None)
        p.save()
        erc = eda.erc(sch, limit=25)
        got = eda.netlist(sch)['nets']
        want = report.pop('_nets')
        mismatch = [{'net': n, 'asked': pins, 'kicad': sorted(got.get(n, []))}
                    for n, pins in want.items() if pins != sorted(got.get(n, []))]
        img = schematic_png(sch, d / 'schematic.png')
        out = {'schematic': str(sch), **report, 'erc': erc,
               'netlist_check': 'KiCad netlist matches nets' if not mismatch else {'differs': mismatch},
               'image': img['png'],
               'next': 'board_from_schematic (then board_render, board_edit, board_drc)'}
        return [json.dumps(out, ensure_ascii=False), Image(path=img['png'])]

    @tool()
    def schematic_render(focus: str | None = None, width_px: int = 1800) -> list:
        """Picture of the project's schematic (symbols with pin numbers and names, wires, net labels,
        no-connect flags). focus: a reference such as "U1" to zoom on it."""
        from .design.render import schematic_png
        f = board_file('kicad_sch')
        img = schematic_png(f, f.parent / (f'schematic_{focus}.png' if focus else 'schematic.png'),
                            width_px=width_px, focus=focus)
        return [json.dumps(img), Image(path=img['png'])]

    @tool()
    def board_from_schematic(width_mm: float | None = None, height_mm: float | None = None,
                             positions: dict[str, list[float]] | None = None,
                             reset_outline: bool = False) -> list:
        """Create the PCB from the schematic, or update it like KiCad's "Update PCB from Schematic"
        (parts that stay keep their place and tracks; new parts are packed into free space).
        width_mm/height_mm: board outline (default: sized from the parts; the outline is only replaced on
        a new board or with reset_outline). positions: {"U1": [x_mm, y_mm, rot_deg]} to place parts
        (board origin is at x=100, y=100 mm). Writes JLCPCB rules, runs DRC, returns a picture."""
        from . import eda
        from .design import board as dboard
        from .design.render import board_png
        p = prj()
        sch = board_file('kicad_sch')
        pcb = sch.with_suffix('.kicad_pcb')
        res = dboard.from_schematic(sch, pcb, width_mm, height_mm, positions, reset_outline=reset_outline)
        p.data['board'] = {**(p.data.get('board') or {}), 'kicad_pcb': pcb.relative_to(p.root).as_posix(),
                           'rules': 'JLCPCB 2-layer (kicad_dru)'}
        p.save()
        drc = eda.drc(pcb, limit=12)
        img = board_png(pcb, pcb.parent / 'board_top.png',
                        drc_items=[i for i in drc['items'] if i['type'] != 'unconnected_items'])
        res.pop('positions', None)
        out = {'board': str(pcb), **res, 'drc': drc, 'image': img['png'],
               'next': ('Place parts with board_edit move (look at board_render), then connect the nets: '
                        'board_autoroute or board_edit add_track/add_via, and a GND zone with add_zone.')}
        return [json.dumps(out, ensure_ascii=False), Image(path=img['png'])]

    @tool()
    def board_autoroute(engine: str = 'auto', passes: int = 20, nets: list[str] | None = None,
                        width_mm: float = 0.25, clearance_mm: float = 0.2) -> list:
        """Route the board's unconnected nets. engine 'freerouting' (installed under tools/freerouting or
        EMCOSIM_FREEROUTING; uses the board's net classes, `passes` optimisation passes, ~10-60 s), 'grid'
        (built-in two-layer maze router for small sparse boards; width_mm/clearance_mm; nets limits it;
        nets that have a zone are left to the zone), or 'auto' (freerouting if installed). Existing
        tracks are kept. Place parts first (board_edit move, board_render); add a GND zone with
        board_edit add_zone before routing. Returns what was routed, DRC and a picture; the previous board
        is kept as a .bak file."""
        from . import eda
        from .design import autoroute
        from .design.render import board_png
        f = board_file()
        kw = {'width_mm': width_mm, 'clearance_mm': clearance_mm, **({'nets': nets} if nets else {})}
        res = autoroute.autoroute(f, engine=engine, passes=passes, **kw)
        drc = eda.drc(f, limit=12)
        img = board_png(f, f.parent / 'board_both.png', side='both',
                        drc_items=[i for i in drc['items'] if i['type'] != 'unconnected_items'])
        out = {**res, 'drc': drc, 'image': img['png']}
        if drc['unconnected']:
            out['next'] = (f"{drc['unconnected']} connections are still open: move crowded parts apart "
                           '(board_edit move), enlarge the outline, raise passes, or add tracks by hand '
                           '(board_edit add_track / add_via), then run board_autoroute again')
        return [json.dumps(out, ensure_ascii=False), Image(path=img['png'])]

    @tool()
    def board_render(view: str = '2d', side: str = 'top', show_drc: bool = True, focus: str | None = None,
                     width_px: int = 1400) -> list:
        """Picture of the board. view '2d': copper, pads, tracks, vias, zones and outline, with
        reference designators and (show_drc) numbered red circles at DRC findings, listed in drc_marked;
        side top|bottom|both; focus a reference ("U1") or [x0, y0, x1, y1] mm to zoom. The result gives
        origin_mm and mm_per_px to turn pixels into board coordinates. view '3d': KiCad's 3D render."""
        from . import eda
        from .design.render import board_3d_png, board_png
        f = board_file()
        if view == '3d':
            img = board_3d_png(f, f.parent / f'board_3d_{side}.png', side=side, width_px=width_px,
                               height_px=int(width_px * 0.6))
        else:
            items = []
            if show_drc:
                items = [i for i in eda.drc(f, limit=60)['items'] if i['type'] != 'unconnected_items']
            img = board_png(f, f.parent / f'board_{side}.png', width_px=width_px, side=side, drc_items=items,
                            focus=focus)
        return [json.dumps(img, ensure_ascii=False), Image(path=img['png'])]

    @tool()
    def board_erc(limit: int = 40) -> dict:
        """KiCad electrical rule check of the project's schematic (board.kicad_sch)."""
        from . import eda
        return eda.erc(board_file('kicad_sch'), limit=limit)

    @tool()
    def board_netlist() -> dict:
        """Components (ref, value, footprint, LCSC part) and nets of the design. Source, in this order:
        the schematic (board.kicad_sch), the board file itself (so board_edit changes show up), or the
        imported netlist file. Values that differ between the board and an imported netlist are listed."""
        from . import eda
        p = prj()
        comps, source, nets = board_parts(p)
        out = {'source': source, 'components': comps, 'nets': nets}
        b = p.data.get('board') or {}
        if source == 'board' and b.get('netlist'):
            try:
                ext = eda.read_netlist(p.path(b['netlist']))['components']
                diff = [{'ref': r, 'board': c['value'], 'netlist': ext[r].get('value')}
                        for r, c in comps.items() if r in ext and str(ext[r].get('value')) != str(c['value'])]
                if diff:
                    out['netlist_differs'] = diff
                    out['note'] = ('The board was edited after the netlist file was exported; the values above '
                                   "are the board's. circuit_check reads circuit.netlist.")
            except (OSError, ValueError) as e:
                out['netlist_error'] = str(e)
        return out

    @tool()
    def board_edit(ops: list[dict]) -> dict:
        """Edit the board with KiCad and re-run DRC. Coordinates in mm (board_render shows them). ops,
        applied in order (any failure leaves the board unchanged):
        {"op": "move", "ref": "C1", "x_mm": 130, "y_mm": 108, "rot_deg": 90} |
        {"op": "flip", "ref": "SW1"} (other side) |
        {"op": "set_value", "ref": "R5", "value": "220"} |
        {"op": "set_field", "ref": "R5", "field": "LCSC", "value": "C22962"} |
        {"op": "add_track", "net": "3V3", "points": [[x, y], [x, y], ...], "width_mm": 0.3, "layer": "F.Cu"} |
        {"op": "add_via", "net": "GND", "x_mm": 120, "y_mm": 110, "size_mm": 0.6, "drill_mm": 0.3} |
        {"op": "delete_tracks", "net": "VBAT", "layer": "F.Cu"|"B.Cu"|"via"} or {"op": "delete_tracks", "all": true} |
        {"op": "add_zone", "net": "GND", "layer": "B.Cu", "outline": [[x, y], ...]} (default: whole board) |
        {"op": "delete_zones", "net": "GND"} or {"all": true} |
        {"op": "outline", "x_mm": 100, "y_mm": 100, "width_mm": 44, "height_mm": 22} |
        {"op": "track_width", "net": "V5_SERVO", "width_mm": 1.0} |
        {"op": "netclass_width", "netclass": "Default", "width_mm": 0.3} | {"op": "clearance", "mm": 0.15}.
        Zones are refilled. The previous board is kept as <file>.bak-<time>."""
        from . import eda
        f = board_file()
        res = eda.edit_pcb(f, ops)
        return {**res, 'drc': eda.drc(f, limit=15)}

    @tool()
    def schematic_edit(ref: str, field: str, value: str) -> dict:
        """Set a symbol field in the KiCad schematic (Value, LCSC, Footprint, Sim.Device, ...)."""
        from . import eda
        return eda.edit_schematic(board_file('kicad_sch'), ref, field, value)

    @tool()
    def board_export_3d(fmt: str = 'step') -> dict:
        """Export the board with components as 3D (step, glb, wrl, stl) next to the board file."""
        from . import eda
        f = board_file()
        return eda.export_3d(f, f.with_suffix('.' + fmt), fmt)

    @tool()
    def circuit_check(run_id: str | None = None) -> dict:
        """Solve the board's power path with ngspice, using the real board netlist (the schematic, or
        circuit.netlist) with every R and C, and simple models for diodes, fuses and linear regulators
        (built-in table, any KiCad Regulator_Linear symbol, or circuit.models), driven by the currents of
        a run (latest if omitted); compares node voltages with the co-simulation.
        Settings (set_param 'circuit'): supply {net, v, r_ohm}; loads {name: {net, a | signal}};
        compare {board_net: run_signal}; models {REF or LCSC: {kind: ldo, v_out, dropout_v, i_gnd_a,
        pins: {in, out, gnd}} | {kind: resistor, r_ohm}}. supply and compare default from power.nodes.*.net.
        trustworthy=false with warnings when a compared net has no modelled path to the supply."""
        from .electrical import board_spice
        p = prj()
        if not run_id:
            from .results import RunStore
            runs = RunStore(p.root).list()
            if not runs:
                raise ValueError('no runs yet; run_scenario first')
            run_id = runs[0]['run_id']
        res = board_spice.check(p, run_id)
        res.pop('trace', None)
        return res

    # ------------------------------------------------- parts (JLCPCB / LCSC, built in)
    @tool()
    def parts_search(query: str, limit: int = 10, filters: dict | None = None) -> dict:
        """Search JLCPCB's in-stock parts in plain words ("10k 0603 1%", "LDO 3.3V SOT-223 1A",
        "Schottky 40V 3A SMA"). filters: package, manufacturer, library_type ("basic" /
        "preferred" / "extended"), min_stock, spec_filters, sort_by ... Data: PCB Parts MCP
        (MIT, pcbparts.dev); stock/price here are the daily snapshot, use part_info for live."""
        from . import parts
        return parts.search(query, limit, **(filters or {}))

    @tool()
    def part_info(lcsc: str | None = None, mpn: str | None = None) -> dict:
        """Live JLCPCB/LCSC details for one part by LCSC number (C6186) or manufacturer part number:
        stock, price tiers, library type (assembly fee), package, datasheet, attributes."""
        from . import parts
        return parts.part(lcsc, mpn)

    @tool()
    def parts_alternatives(lcsc: str, limit: int = 10, filters: dict | None = None) -> dict:
        """In-stock replacements for a part, checked for compatibility, cheapest assembly first.
        filters: same_package, library_type, min_stock."""
        from . import parts
        return parts.alternatives(lcsc, limit, **(filters or {}))

    @tool()
    def board_parts_status() -> dict:
        """For every part of the design that has an LCSC number (schematic, board or netlist): live stock,
        price and library type, and which ones are out of stock or extended (assembly fee). Parts with
        no LCSC number are listed under no_lcsc."""
        from . import parts
        comps, source, _ = board_parts(prj())
        by_lcsc, missing = {}, []
        for ref, c in comps.items():
            if c.get('lcsc'):
                by_lcsc.setdefault(c['lcsc'], []).append(ref)
            else:
                missing.append(ref)
        rows = []
        for code, refs in sorted(by_lcsc.items()):
            try:
                info = parts.part(code)
                rows.append({'lcsc': code, 'refs': refs, 'mpn': info.get('model') or info.get('mpn'),
                             'stock': info.get('stock'), 'library_type': info.get('library_type'),
                             'price': info.get('price') or info.get('prices')})
            except Exception as e:
                rows.append({'lcsc': code, 'refs': refs, 'error': str(e)})
        return {'source': source, 'parts': rows, 'no_lcsc': sorted(missing),
                'out_of_stock': [r['lcsc'] for r in rows if r.get('stock') == 0],
                'extended': [r['lcsc'] for r in rows if r.get('library_type') == 'extended'],
                'credit': parts.CREDIT}

    # ---------------------------------------------------------------- mechanics (MuJoCo)
    @tool()
    def model_info(tag: str | None = None) -> dict:
        """The mechanical model as MuJoCo compiles it: bodies (world position, mass, world bounds),
        joints (type, axis, range in deg or mm), geoms, sites, and the part records (mass source:
        given, density or assumed). tag: body | joint | geom | site | part. Works for MJCF models,
        imported/shape parts, or both. See it with render_model."""
        from . import modeling
        p = prj()
        out = modeling.model_summary(p, tag)
        if (p.data.get('mechanics') or {}).get('mjcf'):
            from . import model_edit
            out['mjcf_elements'] = model_edit.info(p, tag if tag in model_edit.TAGS else None)
        return out

    @tool()
    def model_edit(ops: list[dict]) -> dict:
        """Change attributes of named elements of the MJCF base model, e.g.
        [{"tag": "geom", "name": "arm_collision_0", "attr": "friction", "value": [0.6, 0.005, 0.0001]},
         {"tag": "body", "name": "press_arm", "attr": "pos", "value": [0, 0.01, 0.02]}].
        value null removes the attribute. MuJoCo must still compile the model, otherwise nothing is
        saved. The previous file is kept as <file>.bak-<time>. Parts made with add_shape/import_model
        are changed with part_edit / part_delete instead."""
        from . import model_edit as me
        p = prj()
        if not (p.data.get('mechanics') or {}).get('mjcf'):
            raise ValueError('this project has no MJCF base model; its parts come from add_shape / import_model: '
                             'change them with part_edit (pos, euler_deg, parent, mass_kg, rgba) or part_delete, '
                             'and joints with define_joint')
        return me.edit(p, ops)

    @tool()
    def add_shape(name: str, shape: str, size_mm: list[float], parent: str = 'world',
                  pos: list[float] | None = None, euler_deg: list[float] | None = None,
                  mass_kg: float | None = None, density: float | None = None,
                  rgba: list[float] | None = None) -> list:
        """Make a part from a simple shape, centred on its own origin, with no CAD file:
        box [x, y, z] | rounded_box [x, y, z, corner_r] | cylinder [d, h] | tube [d_out, d_in, h] |
        sphere [d] | capsule [d, h]; sizes in mm, cylinder/tube/capsule axis along z. pos (metres) and
        euler_deg place it relative to `parent` (a part, or world). mass_kg or density sets the mass;
        otherwise it is estimated as solid PLA and flagged. Returns a picture of the model with the new
        part highlighted. Collisions use the convex hull (a tube collides as a full cylinder)."""
        from . import modeling
        p = prj()
        out = modeling.add_shape(p, name, shape, size_mm, parent=parent, pos=pos, euler_deg=euler_deg,
                                 mass_kg=mass_kg, density=density, rgba=rgba)
        img = modeling.render(p, p.root / 'mechanics' / 'render.png', target=None, highlight=[name])
        out['image'] = img['png']
        return [json.dumps(out, ensure_ascii=False), Image(path=img['png'])]

    @tool()
    def part_edit(name: str, pos: list[float] | None = None, euler_deg: list[float] | None = None,
                  parent: str | None = None, mass_kg: float | None = None, rgba: list[float] | None = None,
                  label: str | None = None) -> dict:
        """Move, re-parent, re-weigh or recolour a part (pos in metres relative to its parent,
        euler_deg in degrees, mass in kg). MuJoCo must still compile, otherwise nothing is saved."""
        from . import modeling
        return modeling.part_edit(prj(), name, pos=pos, euler_deg=euler_deg, parent=parent, mass_kg=mass_kg,
                                  rgba=rgba, label=label)

    @tool()
    def part_delete(name: str, cascade: bool = False) -> dict:
        """Delete a part and the joints, probes and actuators that depend on it. Parts mounted on it
        must be deleted first, or pass cascade=true to delete them too."""
        from . import modeling
        return modeling.part_delete(prj(), name, cascade=cascade)

    @tool()
    def render_model(target: str | None = None, highlight: list[str] | None = None,
                     azimuth: float | None = None, elevation: float | None = None,
                     distance: float | None = None, width: int = 900, height: int = 600) -> list:
        """Picture of the mechanical model as it is now (no run needed), on a floor. target: body to
        aim at (default: the whole model); highlight: bodies drawn in orange; azimuth/elevation in
        degrees, distance in metres. Returns the camera used so the next shot can be adjusted."""
        from . import modeling
        p = prj()
        img = modeling.render(p, p.root / 'mechanics' / 'render.png', width, height, target=target,
                              azimuth=azimuth, elevation=elevation, distance=distance, highlight=highlight)
        return [json.dumps(img), Image(path=img['png'])]

    return mcp


def _explain(exc):
    """One line the AI can act on: exception type, message and, for a missing file, which file."""
    if isinstance(exc, FileNotFoundError) and getattr(exc, 'filename', None):
        return f'FileNotFoundError: file not found: {exc.filename}'
    if isinstance(exc, KeyError) and exc.args:
        return f'KeyError: {exc.args[0]}'
    msg = str(exc).strip() or repr(exc)
    return f'{type(exc).__name__}: {msg}'


def _reporting_tool(mcp):
    """mcp.tool(), but an exception reaches the AI with its reason. The MCP SDK replaces the text of
    any exception that is not a ToolError with "Error executing tool X", which leaves an agent
    nothing to fix; re-raising as ToolError keeps the message (the full traceback stays in the log)."""
    import functools
    import inspect
    import logging
    log = logging.getLogger('emcosim.mcp')

    def tool(*args, **kwargs):
        register = mcp.tool(*args, **kwargs)

        def deco(fn):
            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def wrapped(*a, **kw):
                    try:
                        return await fn(*a, **kw)
                    except ToolError:
                        raise
                    except Exception as e:
                        log.exception('tool %s failed', fn.__name__)
                        raise ToolError(_explain(e)) from e
            else:
                @functools.wraps(fn)
                def wrapped(*a, **kw):
                    try:
                        return fn(*a, **kw)
                    except ToolError:
                        raise
                    except Exception as e:
                        log.exception('tool %s failed', fn.__name__)
                        raise ToolError(_explain(e)) from e
            return register(wrapped)
        return deco
    return tool


def load_yaml_text(text):
    import yaml
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError('scenario must be a YAML mapping')
    return data


def main(project_root):
    build_server(project_root).run()


__all__ = ['build_server', 'main']
