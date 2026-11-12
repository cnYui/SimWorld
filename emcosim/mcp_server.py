"""MCP server: lets an AI agent import, annotate, run, tweak and re-run.

    emcosim mcp --project examples/switch_robot      (stdio transport)

Tools edit project.yaml in place (so every change is a git diff) and write run
results under runs/.
"""
from __future__ import annotations

import json
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
        data = load_yaml_text(scenario_yaml)
        p = prj()
        (p.root / 'scenarios').mkdir(exist_ok=True)
        (p.root / 'scenarios' / f'{name}.yaml').write_text(dump_yaml(data), encoding='utf8')
        return {'scenario': name, 'scenarios': p.scenarios()}

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
