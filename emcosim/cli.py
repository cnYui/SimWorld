"""Command line: emcosim <command> [--project DIR] ..."""
from __future__ import annotations

import argparse
import json
import sys

import yaml

from . import __version__, api
from .project import Project


def _value(text):
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return text


def _print(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def main(argv=None):
    ap = argparse.ArgumentParser(prog='emcosim', description='ESP32 + power + MuJoCo co-simulation')
    ap.add_argument('--version', action='version', version=__version__)
    ap.add_argument('--project', '-p', default='.', help='project directory (default: .)')
    sub = ap.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('new', help='create an empty project')
    s.add_argument('name', nargs='?')
    s = sub.add_parser('import-pcb')
    s.add_argument('path')
    s.add_argument('--dsn')
    s.add_argument('--bom')
    s = sub.add_parser('import-model')
    s.add_argument('path')
    s.add_argument('--name')
    s.add_argument('--parent', default='world')
    s.add_argument('--pos', type=float, nargs=3)
    s.add_argument('--euler-deg', type=float, nargs=3)
    s.add_argument('--units', default='mm')
    s.add_argument('--density', type=float, default=1240.0)
    s.add_argument('--mass-kg', type=float)
    s.add_argument('--collision', default='hull', choices=['hull', 'none'])
    s = sub.add_parser('import-firmware')
    s.add_argument('path')
    s.add_argument('--name', default='firmware')
    s = sub.add_parser('define-joint')
    s.add_argument('name')
    s.add_argument('--body', required=True)
    s.add_argument('--type', default='hinge')
    s.add_argument('--axis', type=float, nargs=3)
    s.add_argument('--pos', type=float, nargs=3)
    s.add_argument('--range-deg', type=float, nargs=2)
    s = sub.add_parser('bind-pin')
    s.add_argument('pin')
    s.add_argument('--actuator')
    s.add_argument('--role')
    s.add_argument('--input')
    s.add_argument('--net')
    s.add_argument('--type', dest='actuator_type')
    s.add_argument('--joint')
    s.add_argument('--supply-node')
    s.add_argument('--preset')
    s = sub.add_parser('set', help='set a parameter: emcosim set power.sources.adapter.r_ohm 0.5')
    s.add_argument('path')
    s.add_argument('value')
    s = sub.add_parser('get')
    s.add_argument('path', nargs='?')
    s = sub.add_parser('run', help='run a scenario')
    s.add_argument('scenario')
    s.add_argument('--set', nargs=2, action='append', metavar=('PATH', 'VALUE'), default=[],
                   help='temporary override for this run')
    s = sub.add_parser('sweep')
    s.add_argument('scenario')
    s.add_argument('--param', nargs='+', action='append', required=True, metavar='PATH VALUES',
                   help='--param actuators.servo1.node usb5v v5_servo')
    s.add_argument('--metric', action='append', default=[])
    s = sub.add_parser('results')
    s.add_argument('run_id', nargs='?')
    s.add_argument('--signals', nargs='*')
    s = sub.add_parser('render')
    s.add_argument('run_id')
    s.add_argument('times', type=float, nargs='+')
    s = sub.add_parser('calibrate', help='fit the power model against ngspice')
    s.add_argument('node')
    s.add_argument('source')
    s.add_argument('--i-step', type=float, default=0.7)
    s.add_argument('--apply', action='store_true')
    s = sub.add_parser('view', help='open the web viewer')
    s.add_argument('--port', type=int, default=8765)
    s.add_argument('--host', default='127.0.0.1')
    sub.add_parser('mcp', help='run the MCP server on stdio')

    a = ap.parse_args(argv)
    if a.cmd == 'new':
        p = Project.create(a.project, a.name)
        return _print({'created': str(p.file)})
    if a.cmd == 'mcp':
        from .mcp_server import main as mcp_main
        return mcp_main(a.project)
    if a.cmd == 'view':
        from .viewer.server import serve
        return serve(Project(a.project).root, a.port, a.host)
    p = Project(a.project)
    if a.cmd == 'import-pcb':
        return _print(api.import_pcb(p, a.path, dsn=a.dsn, bom=a.bom))
    if a.cmd == 'import-model':
        return _print(api.import_model(p, a.path, name=a.name, parent=a.parent, pos=a.pos, euler_deg=a.euler_deg,
                                       units=a.units, density=a.density, mass_kg=a.mass_kg, collision=a.collision))
    if a.cmd == 'import-firmware':
        return _print(api.import_firmware(p, a.path, a.name))
    if a.cmd == 'define-joint':
        return _print(api.define_joint(p, a.name, a.body, type=a.type, axis=a.axis, pos=a.pos, range_deg=a.range_deg))
    if a.cmd == 'bind-pin':
        return _print(api.bind_pin(p, a.pin, actuator=a.actuator, role=a.role, input=a.input, net=a.net,
                                   actuator_type=a.actuator_type, joint=a.joint, supply_node=a.supply_node,
                                   preset=a.preset))
    if a.cmd == 'set':
        return _print(api.set_param(p, a.path, _value(a.value)))
    if a.cmd == 'get':
        return _print(api.get_param(p, a.path))
    if a.cmd == 'run':
        s = api.run_scenario(p, a.scenario, {k: _value(v) for k, v in a.set})
        _print(s)
        return 0 if s['passed'] in (True, None) else 1
    if a.cmd == 'sweep':
        params = {x[0]: [_value(v) for v in x[1:]] for x in a.param}
        return _print(api.sweep(p, a.scenario, params, metrics=a.metric))
    if a.cmd == 'results':
        return _print(api.get_results(p, a.run_id, a.signals))
    if a.cmd == 'render':
        return _print(api.render_frames(p, a.run_id, a.times))
    if a.cmd == 'calibrate':
        from . import calibrate
        r = calibrate.calibrate(p.data['power'], a.node, a.source, a.i_step, apply_to=p if a.apply else None)
        return _print(r)
    return 0


if __name__ == '__main__':
    sys.exit(main())
