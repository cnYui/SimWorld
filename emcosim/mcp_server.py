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
except ImportError:                     # mcp 1.x
    from mcp.server.fastmcp import FastMCP, Image

from . import api, calibrate as cal
from .project import Project, dump_yaml


def build_server(project_root):
    root = Path(project_root).resolve()
    mcp = FastMCP('emcosim', instructions=(
        'Electronics + physics co-simulation of an ESP32 device. Workflow: get_project -> '
        'import_pcb / import_model / import_firmware -> define_joint / bind_pin -> '
        'run_scenario -> get_results -> set_param or sweep -> run_scenario again. '
        'Parameters are dotted paths into project.yaml (e.g. power.sources.adapter.r_ohm, '
        'actuators.servo1.preset, mechanics.elements.rocker_detent.peak_force_n, '
        'firmware.press_angle_deg). All model values marked "assumed" in project.yaml are '
        'not measurements.'))

    def prj():
        return Project(root)

    @mcp.tool()
    def get_project(path: str | None = None) -> dict:
        """Overview of the project (scenarios, PCB, firmware, joints, actuators, bindings, power,
        recent runs), or the value at one dotted parameter path."""
        p = prj()
        return api.project_overview(p) if not path else {'path': path, 'value': api.get_param(p, path)}

    @mcp.tool()
    def import_pcb(path: str, dsn: str | None = None, bom: str | None = None,
                   placement: dict | None = None) -> dict:
        """Import a KiCad .kicad_pcb (EasyEDA: export to KiCad first; an EasyEDA Pro .epcb plus its
        Specctra .dsn also works through a limited converter). Returns nets, bulk capacitance per
        net and copper resistance per net."""
        return api.import_pcb(prj(), path, placement=placement, dsn=dsn, bom=bom)

    @mcp.tool()
    def import_model(path: str, name: str | None = None, parent: str = 'world',
                     pos: list[float] | None = None, euler_deg: list[float] | None = None,
                     units: str = 'mm', density: float = 1240.0, mass_kg: float | None = None,
                     collision: str = 'hull') -> dict:
        """Import an STL/STEP/OBJ part as a body under `parent` (a .xml MJCF instead becomes the base
        mechanical model). collision: 'hull' (convex hull) or 'none'."""
        return api.import_model(prj(), path, name=name, parent=parent, pos=pos, euler_deg=euler_deg,
                                units=units, density=density, mass_kg=mass_kg, collision=collision)

    @mcp.tool()
    def import_firmware(path: str, name: str = 'firmware') -> dict:
        """Import ESP32 firmware: an ESP-IDF build directory (with flasher_args.json) or a merged
        flash .bin. Lists the tunable parameters stored in its fw_params partition."""
        return api.import_firmware(prj(), path, name=name)

    @mcp.tool()
    def define_joint(name: str, body: str, type: str = 'hinge', axis: list[float] | None = None,
                     pos: list[float] | None = None, range_deg: list[float] | None = None,
                     damping: float | None = None, armature: float | None = None,
                     stiffness: float | None = None, frictionloss: float | None = None) -> dict:
        """Create a joint on a body (or annotate an existing joint of the MJCF with limits/damping)."""
        return api.define_joint(prj(), name, body, type=type, axis=axis, pos=pos, range_deg=range_deg,
                                damping=damping, armature=armature, stiffness=stiffness,
                                frictionloss=frictionloss)

    @mcp.tool()
    def bind_pin(pin: str, actuator: str | None = None, role: str | None = None,
                 input: str | None = None, net: str | None = None, actuator_type: str | None = None,
                 joint: str | None = None, supply_node: str | None = None, preset: str | None = None,
                 active: str = 'low') -> dict:
        """Wire an ESP32 pin. Output: pin -> actuator (servo signal, or dc_motor role in_a/in_b);
        creating the actuator needs actuator_type ('servo'|'dc_motor'), joint, supply_node and
        optionally preset ('sg90', 'mg90s', 'tt_motor'). Input: input='button1' wires a button
        to the pin. `net` is checked against the PCB and the copper path is reported."""
        return api.bind_pin(prj(), pin, actuator=actuator, role=role, input=input, net=net,
                            actuator_type=actuator_type, joint=joint, supply_node=supply_node,
                            preset=preset, active=active)

    @mcp.tool()
    def set_param(path: str, value: Any) -> dict:
        """Set one parameter (dotted path into project.yaml; 'firmware.X' means the firmware's
        fw_params field X). Persists to project.yaml. Returns old and new value."""
        return api.set_param(prj(), path, value)

    @mcp.tool()
    def write_scenario(name: str, scenario_yaml: str) -> dict:
        """Create or replace scenarios/<name>.yaml. Keys: description, duration_s, overrides
        {path: value}, events [{t, press: button1, hold_s} | {t, set: power.sources.X.v, value}],
        expect [{metric|event|signal, eq|lt|gt|le|ge|ne: value, at|window|stat}],
        mcu_backend: scripted with pin_script for firmware-free runs."""
        return api.write_scenario(prj(), name, load_yaml_text(scenario_yaml))

    @mcp.tool()
    def run_scenario(name: str, overrides: dict | None = None) -> dict:
        """Run a scenario (firmware in QEMU, power + motor models, MuJoCo, 1 ms lock-step).
        overrides: temporary {path: value} for this run only. Returns checks, metrics, MCU log."""
        return api.run_scenario(prj(), name, overrides)

    @mcp.tool()
    def sweep(scenario: str, params: dict[str, list], metrics: list[str] | None = None,
              overrides: dict | None = None) -> dict:
        """Run the scenario for every combination of params ({path: [values]}); returns one row per
        run with pass/fail and the requested metrics (e.g. mcu.resets, node.v5_servo.min_v)."""
        return api.sweep(prj(), scenario, params, metrics=metrics, extra_overrides=overrides)

    @mcp.tool()
    def get_results(run_id: str | None = None, signals: list[str] | None = None,
                    t0: float | None = None, t1: float | None = None, max_points: int = 300) -> dict:
        """Summary of a run (latest if run_id is omitted; 'list' lists runs). With `signals`,
        returns those time series (decimated to max_points)."""
        return api.get_results(prj(), run_id, signals, t0, t1, max_points)

    @mcp.tool()
    def render_frames(run_id: str, times: list[float], width: int = 640, height: int = 480,
                      azimuth: float | None = None, elevation: float | None = None,
                      distance: float | None = None) -> list:
        """Render MuJoCo images of a run at the given times (max 6). Needs an OpenGL backend
        (MUJOCO_GL=egl or osmesa on headless machines)."""
        cam = {k: v for k, v in (('azimuth', azimuth), ('elevation', elevation), ('distance', distance))
               if v is not None}
        out = api.render_frames(prj(), run_id, times[:6], width, height, camera=cam)
        return [json.dumps(out)] + [Image(path=p) for p in out['frames']]

    @mcp.tool()
    def calibrate_power(node: str, source: str, i_step_a: float = 0.7, apply: bool = False) -> dict:
        """Offline ngspice calibration: compares the equivalent power model with a SPICE transient
        that includes capacitor ESR and lead inductance, fits the node capacitance and source
        resistance; apply=True writes them to project.yaml."""
        p = prj()
        r = cal.calibrate(p.data['power'], node, source, i_step=i_step_a, apply_to=p if apply else None)
        r.pop('netlist', None)
        return r

    @mcp.tool()
    def fit_measurements(path: str, apply: bool = False) -> dict:
        """Fit servo (R, Ke, gear efficiency, idle current, friction, speed limit, backlash/stiffness),
        supply (v, r_ohm, i_limit_a) and bistable switch (peak_force_n, stable_deg) parameters from a
        measurement sheet (measurements/template.yaml). Returns fitted values ± 1σ, residuals before/after,
        changes old→new, and parameters kept at their prior (with the reason). apply=True writes
        project.yaml."""
        return api.fit_measurements(prj(), path, apply=apply)

    @mcp.tool()
    def compare_trace(trace_csv: str, signal: str, run_id: str | None = None, column: str | None = None,
                      time_unit: str = 's', t_offset: float = 0.0, align: bool = True,
                      scale: float = 1.0) -> dict:
        """RMS/max/mean error of a run's signal against a measured CSV trace; align searches the
        time offset."""
        return api.compare_trace(prj(), trace_csv, signal, run_id=run_id, column=column, time_unit=time_unit,
                                 t_offset=t_offset, align=align, scale=scale)

    @mcp.tool()
    def send_to_bambu_studio(parts: list[str] | None = None, with_settings: bool = False,
                             open_app: bool = True) -> dict:
        """Export printable parts (printing.parts in project.yaml, e.g. press_arm, frame,
        servo_cradle) into one 3MF and open it in Bambu Studio on this computer. with_settings
        embeds the suggested per-part print settings (experimental)."""
        from . import printing
        return printing.send_parts(prj(), parts or None, with_settings, open_app=open_app)

    # ------------------------------------------------------------- board (KiCad kernel)
    def board_file(key='kicad_pcb'):
        b = prj().data.get('board') or {}
        if not b.get(key):
            raise ValueError(f'project.yaml has no board.{key}; use board_import first')
        return root / b[key]

    @mcp.tool()
    def board_status() -> dict:
        """Which board files the project uses and whether KiCad and ngspice are available."""
        import shutil
        from . import eda
        return {'board': prj().data.get('board'), 'kicad': eda.status(), 'ngspice': shutil.which('ngspice')}

    @mcp.tool()
    def board_import(path: str, netlist: str | None = None, name: str | None = None) -> dict:
        """Bring a board in through KiCad's importers: .kicad_pcb as is, or EasyEDA Pro .epro (export it
        from 嘉立创EDA: File > Export > project), Altium .PcbDoc, Eagle .brd. `netlist` is that EDA's
        netlist export (EasyEDA: Protel 2.0 .net) and fills in pad nets the importer misses. Writes
        pcb/<name>/<name>.kicad_pcb plus JLCPCB DRC rules and records it as board.kicad_pcb."""
        from . import eda
        p = prj()
        src = Path(path).expanduser().resolve()
        name = name or re.sub(r'[^A-Za-z0-9_-]+', '_', src.stem).strip('_').lower()
        out_dir = root / 'pcb' / name
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / f'{name}.kicad_pcb'
        if src.suffix.lower() == '.kicad_pcb':
            import shutil
            shutil.copy2(src, dst)
            res = {'file': str(dst)}
        else:
            res = eda.import_board(src, dst, netlist=netlist)
        entry = {'kicad_pcb': dst.relative_to(root).as_posix()}
        if netlist:
            nl = Path(netlist).expanduser().resolve()
            entry['netlist'] = nl.relative_to(root).as_posix() if nl.is_relative_to(root) else str(nl)
        p.data['board'] = {**(p.data.get('board') or {}), **entry}
        p.save()
        return {**res, 'board': p.data['board'], 'drc': eda.drc(dst, limit=15)}

    @mcp.tool()
    def board_drc(limit: int = 40) -> dict:
        """KiCad design rule check of the project's board (JLCPCB limits when imported with them).
        Returns counts and the first findings with positions in mm."""
        from . import eda
        return eda.drc(board_file(), limit=limit)

    @mcp.tool()
    def board_erc(limit: int = 40) -> dict:
        """KiCad electrical rule check of the project's schematic (board.kicad_sch)."""
        from . import eda
        return eda.erc(board_file('kicad_sch'), limit=limit)

    @mcp.tool()
    def board_netlist() -> dict:
        """Components (ref, value, LCSC part, manufacturer part) and nets of the board."""
        from . import eda
        b = prj().data.get('board') or {}
        if b.get('kicad_sch'):
            return eda.netlist(root / b['kicad_sch'])
        if b.get('netlist'):
            return eda.read_netlist(root / b['netlist'])
        raise ValueError('board has neither kicad_sch nor netlist')

    @mcp.tool()
    def board_edit(ops: list[dict]) -> dict:
        """Edit the board with KiCad and re-run DRC. ops, applied in order:
        {"op": "move", "ref": "C1", "x_mm": 130, "y_mm": 108, "rot_deg": 90} |
        {"op": "set_value", "ref": "R5", "value": "220"} |
        {"op": "set_field", "ref": "R5", "field": "LCSC", "value": "C22962"} |
        {"op": "track_width", "net": "V5_SERVO", "width_mm": 1.0} |
        {"op": "netclass_width", "netclass": "Default", "width_mm": 0.3} |
        {"op": "clearance", "mm": 0.15}.
        The previous board is kept as <file>.bak-<time>."""
        from . import eda
        f = board_file()
        res = eda.edit_pcb(f, ops)
        return {**res, 'drc': eda.drc(f, limit=15)}

    @mcp.tool()
    def schematic_edit(ref: str, field: str, value: str) -> dict:
        """Set a symbol field in the KiCad schematic (Value, LCSC, Footprint, Sim.Device, ...)."""
        from . import eda
        return eda.edit_schematic(board_file('kicad_sch'), ref, field, value)

    @mcp.tool()
    def board_export_3d(fmt: str = 'step') -> dict:
        """Export the board with components as 3D (step, glb, wrl, stl) next to the board file."""
        from . import eda
        f = board_file()
        return eda.export_3d(f, f.with_suffix('.' + fmt), fmt)

    @mcp.tool()
    def circuit_check(run_id: str | None = None) -> dict:
        """Solve the board's power path with ngspice, using the real board netlist (every R and C, the
        diode, LDO and fuse with simple models) and the servo / MCU currents of a run, then compare
        node voltages with the co-simulation. Settings: circuit.* in project.yaml."""
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
    @mcp.tool()
    def parts_search(query: str, limit: int = 10, filters: dict | None = None) -> dict:
        """Search JLCPCB's in-stock parts in plain words ("10k 0603 1%", "LDO 3.3V SOT-223 1A",
        "Schottky 40V 3A SMA"). filters: package, manufacturer, library_type ("basic" /
        "preferred" / "extended"), min_stock, spec_filters, sort_by ... Data: PCB Parts MCP
        (MIT, pcbparts.dev); stock/price here are the daily snapshot, use part_info for live."""
        from . import parts
        return parts.search(query, limit, **(filters or {}))

    @mcp.tool()
    def part_info(lcsc: str | None = None, mpn: str | None = None) -> dict:
        """Live JLCPCB/LCSC details for one part by LCSC number (C6186) or manufacturer part number:
        stock, price tiers, library type (assembly fee), package, datasheet, attributes."""
        from . import parts
        return parts.part(lcsc, mpn)

    @mcp.tool()
    def parts_alternatives(lcsc: str, limit: int = 10, filters: dict | None = None) -> dict:
        """In-stock replacements for a part, checked for compatibility, cheapest assembly first.
        filters: same_package, library_type, min_stock."""
        from . import parts
        return parts.alternatives(lcsc, limit, **(filters or {}))

    @mcp.tool()
    def board_parts_status() -> dict:
        """For every part on the board (netlist LCSC numbers): live stock, price and library type,
        and which ones are out of stock or extended (assembly fee)."""
        from . import eda, parts
        b = prj().data.get('board') or {}
        if not b.get('netlist'):
            raise ValueError('board has no netlist')
        comps = eda.read_netlist(root / b['netlist'])['components']
        by_lcsc = {}
        for ref, c in comps.items():
            if c.get('lcsc'):
                by_lcsc.setdefault(c['lcsc'], []).append(ref)
        rows = []
        for code, refs in sorted(by_lcsc.items()):
            try:
                info = parts.part(code)
                rows.append({'lcsc': code, 'refs': refs, 'mpn': info.get('mpn') or info.get('model'),
                             'stock': info.get('stock'), 'library_type': info.get('library_type'),
                             'price': info.get('price') or info.get('prices')})
            except Exception as e:
                rows.append({'lcsc': code, 'refs': refs, 'error': str(e)})
        return {'parts': rows, 'source': parts.CREDIT}

    # ---------------------------------------------------------------- mechanics (MuJoCo)
    @mcp.tool()
    def model_info(tag: str | None = None) -> dict:
        """Named bodies, geoms, joints, sites and meshes of the MuJoCo model with sizes, positions,
        masses, friction and joint ranges. tag limits it to one kind."""
        from . import model_edit
        return model_edit.info(prj(), tag)

    @mcp.tool()
    def model_edit(ops: list[dict]) -> dict:
        """Change attributes of named MJCF elements, e.g.
        [{"tag": "geom", "name": "arm_collision_0", "attr": "friction", "value": [0.6, 0.005, 0.0001]},
         {"tag": "body", "name": "press_arm", "attr": "pos", "value": [0, 0.01, 0.02]}].
        value null removes the attribute. MuJoCo must still compile the model, otherwise nothing is
        saved. The previous file is kept as <file>.bak-<time>. Run a scenario afterwards."""
        from . import model_edit as me
        return me.edit(prj(), ops)

    return mcp


def load_yaml_text(text):
    import yaml
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError('scenario must be a YAML mapping')
    return data


def main(project_root):
    build_server(project_root).run()


__all__ = ['build_server', 'main']
