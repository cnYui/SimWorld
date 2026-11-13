"""What a new user's AI meets through MCP on an empty project: errors it can act on, warnings for
assumed values, and the design tools (schematic, board, shapes, sensors) it needs to get from nothing
to a passing simulation."""
import asyncio
import json

import pytest

from emcosim import api
from emcosim.app.link import AppLink
from emcosim.mcp_server import ToolError, build_server
from emcosim.project import Project


@pytest.fixture
def empty(tmp_path):
    p = Project.create(tmp_path / 'p', 'empty')
    return p, build_server(p.root, link=AppLink({}))


def call(srv, name, args=None):
    res = asyncio.run(srv.call_tool(name, args or {}))
    if isinstance(res, tuple):
        res = res[1] if isinstance(res[1], dict) else res[0]
    if isinstance(res, dict):
        return res.get('result', res) if set(res) == {'result'} else res
    sc = getattr(res, 'structured_content', None) or getattr(res, 'structuredContent', None)
    if sc:
        return sc.get('result', sc) if set(sc) == {'result'} else sc
    return json.loads(''.join(getattr(c, 'text', '') for c in getattr(res, 'content', res)))


@pytest.mark.parametrize('tool, args, reason', [
    ('board_drc', {}, 'no PCB yet'),
    ('board_erc', {}, 'no schematic yet'),
    ('board_netlist', {}, 'no schematic, board or netlist'),
    ('run_scenario', {'name': 'missing'}, 'missing'),
    ('import_model', {'path': 'nope.stl'}, 'nope.stl'),
])
def test_errors_reach_the_ai_with_their_reason(empty, tool, args, reason):
    _, srv = empty
    with pytest.raises(ToolError, match=reason):
        asyncio.run(srv.call_tool(tool, args))


def test_board_status_without_kicad_explains(empty, monkeypatch):
    from emcosim import eda
    monkeypatch.setattr(eda, 'find_cli', lambda: None)
    monkeypatch.setattr(eda.shutil, 'which', lambda *_: None)
    out = call(empty[1], 'board_status')
    assert out['kicad']['kicad_cli'] is None and 'Install KiCad' in out['kicad']['hint']


def test_assumed_mass_and_replace_are_reported(empty, tmp_path):
    import trimesh
    p, srv = empty
    stl = tmp_path / 'box.stl'
    trimesh.creation.box([10, 10, 10]).export(stl)
    out = call(srv, 'import_model', {'path': str(stl), 'name': 'box'})
    assert any('ASSUMED' in w for w in out['warnings'])
    assert Project(p.root).data['mechanics']['parts']['box']['mass_assumed']
    out = call(srv, 'import_model', {'path': str(stl), 'name': 'box', 'mass_kg': 0.01})
    assert any('replaced' in w for w in out['warnings']) and not any('ASSUMED' in w for w in out['warnings'])
    with pytest.raises(ToolError, match='parent'):
        asyncio.run(srv.call_tool('import_model', {'path': str(stl), 'name': 'b2', 'parent': 'nope'}))


def test_slide_joint_takes_millimetres(empty, tmp_path):
    import trimesh
    p, srv = empty
    stl = tmp_path / 'box.stl'
    trimesh.creation.box([10, 10, 10]).export(stl)
    call(srv, 'import_model', {'path': str(stl), 'name': 'box', 'mass_kg': 0.01})
    with pytest.raises(ToolError, match='range_mm'):
        asyncio.run(srv.call_tool('define_joint', {'name': 'j', 'body': 'box', 'type': 'slide',
                                                   'range_deg': [0, 1]}))
    out = call(srv, 'define_joint', {'name': 'j', 'body': 'box', 'type': 'slide', 'axis': [0, 0, 1],
                                     'range_mm': [-5, 5]})
    assert 'millimetres' in out['units']
    from emcosim.mechanics import Mechanics
    m = Mechanics(p.root, Project(p.root).data['mechanics']).model
    assert list(m.jnt_range[0]) == pytest.approx([-0.005, 0.005])


def test_esp32_pin_checks(empty):
    _, srv = empty
    out = call(srv, 'bind_pin', {'pin': 'GPIO37', 'input': 'export_btn'})
    assert any('pull-up' in w for w in out['warnings'])
    with pytest.raises(ToolError, match='flash'):
        asyncio.run(srv.call_tool('bind_pin', {'pin': 'GPIO7', 'input': 'x'}))


# ------------------------------------------------------------------ schematic and board from nothing
from emcosim import eda as _eda

needs_kicad = pytest.mark.skipif(not _eda.find_cli() or not _eda.find_python(), reason='KiCad not installed')

LED_BOARD = {
    'components': [
        {'ref': 'J1', 'symbol': 'Connector_Generic:Conn_01x02', 'value': '5V in',
         'footprint': 'Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical'},
        {'ref': 'R1', 'symbol': 'Device:R', 'value': '330', 'footprint': 'Resistor_SMD:R_0603_1608Metric',
         'lcsc': 'C23138'},
        {'ref': 'D1', 'symbol': 'Device:LED', 'value': 'red', 'footprint': 'LED_SMD:LED_0603_1608Metric'},
    ],
    'nets': {'VIN': ['J1.1', 'R1.1'], 'LED_A': ['R1.2', 'D1.A'], 'GND': ['D1.K', 'J1.2']},
}


def call_parts(srv, name, args):
    """Tools that return [json, image]: the JSON part and whether an image came along."""
    res = asyncio.run(srv.call_tool(name, args))
    content = res[0] if isinstance(res, tuple) else getattr(res, 'content', res)
    text = next(c.text for c in content if getattr(c, 'type', '') == 'text')
    return json.loads(text), any(getattr(c, 'type', '') == 'image' for c in content)


@needs_kicad
def test_schematic_to_board_through_mcp(empty):
    p, srv = empty
    out, img = call_parts(srv, 'schematic_create', LED_BOARD)
    assert img and out['erc']['errors'] == 0 and out['netlist_check'].startswith('KiCad netlist matches')
    assert Project(p.root).data['board']['kicad_sch'].endswith('.kicad_sch')
    out, img = call_parts(srv, 'board_from_schematic', {'width_mm': 20, 'height_mm': 12})
    assert img and out['footprints'] == 3 and not out.get('outside_outline')
    nets = call(srv, 'board_netlist')
    assert nets['source'] == 'schematic' and nets['components']['R1']['lcsc'] == 'C23138'
    pads = {(q['ref'], q['pad']): q for q in __import__('emcosim.importers.kicad', fromlist=['x'])
            .load_kicad_pcb(p.root / Project(p.root).data['board']['kicad_pcb'])['_pads']}
    a, b = pads[('R1', '2')], pads[('D1', '2')]
    before = call(srv, 'board_drc')['unconnected']
    res = call(srv, 'board_edit', {'ops': [{'op': 'add_track', 'net': 'LED_A', 'width_mm': 0.3,
                                            'points': [[a['x'], a['y']], [b['x'], b['y']]]},
                                           {'op': 'add_zone', 'net': 'GND', 'layer': 'B.Cu'}]})
    assert res['drc']['unconnected'] < before
    with pytest.raises(ToolError, match='no net'):
        asyncio.run(srv.call_tool('board_edit', {'ops': [{'op': 'add_track', 'net': 'NOPE', 'points': [[0, 0], [1, 1]]}]}))
    out, img = call_parts(srv, 'board_render', {})
    assert img and out['mm_per_px'] > 0
    out, img = call_parts(srv, 'schematic_render', {'focus': 'R1'})
    assert img


@needs_kicad
def test_schematic_create_reports_bad_pins(empty):
    bad = {**LED_BOARD, 'nets': {**LED_BOARD['nets'], 'X': ['R1.7', 'Q9.1']}}
    with pytest.raises(ToolError, match='R1 has no pin'):
        asyncio.run(empty[1].call_tool('schematic_create', bad))


@needs_kicad
def test_symbol_and_footprint_search(empty):
    _, srv = empty
    hits = call(srv, 'symbol_search', {'query': 'ESP32-PICO-V3-02'})['symbols']
    assert any(h['symbol'] == 'MCU_Espressif:ESP32-PICO-V3-02' for h in hits)
    info = call(srv, 'symbol_info', {'symbol': 'MCU_Espressif:ESP32-PICO-V3-02'})
    assert {'number': '5', 'name': 'SENSOR_VP', 'type': 'input', 'unit': 0} in info['pins']
    assert 'SOT-23-5' in ' '.join(call(srv, 'footprint_search', {'query': 'SOT-23-5'})['footprints'])


# ------------------------------------------------------------------ modelling without CAD files
def test_shapes_edit_delete_and_render(empty):
    p, srv = empty
    out, img = call_parts(srv, 'add_shape', {'name': 'bar', 'shape': 'cylinder', 'size_mm': [28, 1200],
                                             'pos': [0, 0, 1.0], 'euler_deg': [0, 90, 0], 'mass_kg': 20})
    assert img and out['mass_kg'] == 20
    out, _ = call_parts(srv, 'add_shape', {'name': 'case', 'shape': 'rounded_box', 'size_mm': [48, 26, 14, 4],
                                           'parent': 'bar', 'pos': [-0.021, 0, -0.35], 'euler_deg': [0, -90, 0]})
    assert any('ASSUMED' in w for w in out['warnings'])
    call_parts(srv, 'add_shape', {'name': 'knob', 'shape': 'cylinder', 'size_mm': [14, 7], 'parent': 'case',
                                  'pos': [0, 0, 0.0105], 'mass_kg': 0.002})
    with pytest.raises(ToolError, match='size_mm needs 2'):
        asyncio.run(srv.call_tool('add_shape', {'name': 'x', 'shape': 'cylinder', 'size_mm': [1]}))
    info = call(srv, 'model_info', {'tag': 'body'})
    case = next(b for b in info['body'] if b['name'] == 'case')
    assert case['bounds_world_m'][0][2] == pytest.approx(1.014, abs=1e-3)      # sits on the bar
    call(srv, 'part_edit', {'name': 'case', 'mass_kg': 0.035})
    assert Project(p.root).data['mechanics']['parts']['case']['mass_kg'] == 0.035
    with pytest.raises(ToolError, match='part_edit'):
        asyncio.run(srv.call_tool('model_edit', {'ops': [{'tag': 'body', 'name': 'case', 'attr': 'pos', 'value': [0, 0, 0]}]}))
    out, img = call_parts(srv, 'render_model', {'target': 'case', 'highlight': ['knob']})
    assert img and out['camera']['distance_m'] < 0.5
    with pytest.raises(ToolError, match='carries'):
        asyncio.run(srv.call_tool('part_delete', {'name': 'case'}))
    out = call(srv, 'part_delete', {'name': 'case', 'cascade': True})
    assert set(out['deleted_parts']) == {'case', 'knob'}


# ------------------------------------------------------------------ sensors, motion, behaviour model
from pathlib import Path as _Path

REP_COUNTER = (_Path(__file__).parent / 'data' / 'rep_counter_behavior.py')


def test_barbell_reps_counted_by_behaviour_model(empty):
    p, srv = empty
    call_parts(srv, 'add_shape', {'name': 'bar', 'shape': 'cylinder', 'size_mm': [28, 1200], 'pos': [0, 0, 1.0],
                                  'euler_deg': [0, 90, 0], 'mass_kg': 20})
    call_parts(srv, 'add_shape', {'name': 'case', 'shape': 'box', 'size_mm': [48, 26, 14], 'parent': 'bar',
                                  'pos': [-0.021, 0, -0.35], 'euler_deg': [0, -90, 0], 'mass_kg': 0.03})
    call(srv, 'define_joint', {'name': 'bar_lift', 'body': 'bar', 'type': 'slide', 'axis': [1, 0, 0],
                               'range_mm': [-600, 600]})     # bar frame is rotated: its x is world z
    out = call(srv, 'define_probe', {'name': 'imu', 'type': 'imu', 'body': 'case'})
    assert 'probe.imu.az' in out['signals']
    call(srv, 'bind_pin', {'pin': 'GPIO36', 'input': 'weight_knob', 'input_type': 'pot', 'value': 0.3})
    call(srv, 'bind_pin', {'pin': 'GPIO37', 'input': 'export_btn'})
    call(srv, 'bind_pin', {'pin': 'GPIO39', 'input': 'mode_btn'})
    call(srv, 'set_param', {'path': 'power.nodes.vbat', 'value': {'c_f': 1e-5}})
    call(srv, 'set_param', {'path': 'power.sources.lipo', 'value': {'node': 'vbat', 'v': 3.7, 'r_ohm': 0.3}})
    with pytest.raises(ToolError, match='loop'):
        asyncio.run(srv.call_tool('write_behavior', {'name': 'bad', 'code': 'x = 1\n'}))
    call(srv, 'write_behavior', {'name': 'counter', 'code': REP_COUNTER.read_text(encoding='utf-8')})
    call(srv, 'write_scenario', {'name': 'bench5', 'scenario_yaml': (
        'duration_s: 13\nmcu_backend: behavior\nbehavior_script: behavior/counter.py\n'
        'motions:\n  - {joint: bar_lift, profile: reps, amplitude_mm: 400, period_s: 1.6, count: 5, start_s: 0.5}\n'
        'events:\n  - {t: 0.2, press: mode_btn, hold_s: 0.1}\n  - {t: 12.0, press: export_btn, hold_s: 0.2}\n'
        'expect:\n  - {event: rep, eq: 5}\n  - {event: set, eq: 1}\n  - {event: export, eq: 1}\n')})
    res = call(srv, 'run_scenario', {'name': 'bench5'})
    assert res['passed'], res['checks']
    assert res['files'] == ['session.csv']
    run_dir = p.root / 'runs' / res['run_id']
    csv_text = (run_dir / 'session.csv').read_text(encoding='utf-8')
    assert 'press,5,60.0' in csv_text                       # mode pressed once; 0.3 x 200 kg
    assert res['metrics']['probe.imu.a_g.peak'] > 1.1
    data = call(srv, 'get_results', {'signals': ['joint.bar_lift.mm', 'input.weight_knob.v'], 'max_points': 50})
    assert min(data['data']['joint.bar_lift.mm']) == pytest.approx(-400, abs=15)


def test_behaviour_errors_point_at_the_line(empty):
    p, srv = empty
    call(srv, 'write_behavior', {'name': 'oops', 'code': 'def loop(io, t):\n    io.imu("nope")\n'})
    call(srv, 'write_scenario', {'name': 's', 'scenario_yaml': 'duration_s: 0.1\nmcu_backend: behavior\n'
                                                               'behavior_script: behavior/oops.py\n'})
    with pytest.raises(ToolError, match=r"no IMU probe 'nope'.*line 2"):
        asyncio.run(srv.call_tool('run_scenario', {'name': 's'}))
