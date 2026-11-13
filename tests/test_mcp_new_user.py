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


def _gl():
    try:
        import mujoco
        m = mujoco.MjModel.from_xml_string('<mujoco><worldbody><geom size=".1"/></worldbody></mujoco>')
        r = api.offscreen_renderer(m, 32, 32)
        r.close()
        return True
    except Exception:
        return False


HAVE_GL = _gl()


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
    assert out['mass_kg'] == 20 and (img or not HAVE_GL)
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
    if HAVE_GL:
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


# ------------------------------------------------------------------ circuit check on a generated design
from emcosim.toolpaths import find_ngspice as _find_ngspice

needs_ngspice = pytest.mark.skipif(_find_ngspice() is None, reason='ngspice not installed')
DESIGN = json.loads((_Path(__file__).parent / 'data' / 'rep_counter_design.json').read_text(encoding='utf-8'))


@needs_kicad
@needs_ngspice
def test_circuit_check_on_generated_schematic(empty):
    p, srv = empty
    out, _ = call_parts(srv, 'schematic_create', DESIGN)
    assert out['erc']['errors'] == 0
    call(srv, 'set_param', {'path': 'power', 'value': {
        'nodes': {'vbat': {'c_f': 1e-5, 'net': 'VBAT'}},
        'sources': {'lipo': {'node': 'vbat', 'v': 3.7, 'r_ohm': 0.3}},
        'regulators': {'ldo3v3': {'type': 'ldo', 'input': 'vbat', 'v_out': 3.3, 'dropout_v': 0.2,
                                  'c_out_f': 1e-5, 'i_q_a': 6e-5, 'net': '3V3'}}}})
    call(srv, 'set_param', {'path': 'mcu.supply', 'value': 'ldo3v3'})
    call(srv, 'write_scenario', {'name': 'idle', 'scenario_yaml': 'duration_s: 0.3\nmcu_backend: scripted\n'})
    call(srv, 'run_scenario', {'name': 'idle'})
    with pytest.raises(ToolError, match='circuit.loads is missing'):
        asyncio.run(srv.call_tool('circuit_check', {}))
    call(srv, 'set_param', {'path': 'circuit', 'value': {'loads': {'mcu': {'net': '3V3', 'a': 0.05}}}})
    res = call(srv, 'circuit_check')
    rows = {r['net']: r for r in res['compare']}
    assert res['trustworthy'], res['warnings']
    assert set(rows) == {'VBAT', '3V3'} and rows['3V3']['max_abs_diff_v'] < 0.1
    assert any(u['ref'] == 'U4' for u in res['parts_modelled'])
    # without the regulator model the 3V3 net floats: the check must say so instead of printing nonsense
    call(srv, 'set_param', {'path': 'circuit.models', 'value': {'U4': {'kind': 'ldo', 'v_out': 3.3,
                                                                      'pins': {'in': '9', 'out': '5', 'gnd': '2'}}}})
    res = call(srv, 'circuit_check')
    assert not res['trustworthy'] and any('3V3 has no DC path' in w for w in res['warnings'])


@needs_kicad
def test_grid_autoroute_connects_a_small_board(empty):
    p, srv = empty
    call_parts(srv, 'schematic_create', LED_BOARD)
    call_parts(srv, 'board_from_schematic', {'width_mm': 24, 'height_mm': 14})
    before = call(srv, 'board_drc')['unconnected']
    out, img = call_parts(srv, 'board_autoroute', {'engine': 'grid'})
    assert img and out['engine'] == 'grid' and not out['failed']
    assert out['drc']['unconnected'] < before and out['drc']['unconnected'] == 0


from emcosim.design.autoroute import find_freerouting as _find_fr


@needs_kicad
@pytest.mark.skipif(_find_fr() is None, reason='freerouting not installed (tools/freerouting or EMCOSIM_FREEROUTING)')
def test_freerouting_routes_a_small_board(empty):
    p, srv = empty
    call_parts(srv, 'schematic_create', LED_BOARD)
    call_parts(srv, 'board_from_schematic', {'width_mm': 24, 'height_mm': 14})
    out, img = call_parts(srv, 'board_autoroute', {'engine': 'freerouting', 'passes': 5})
    assert img and out['engine'] == 'freerouting' and out['tracks'] > 0
    assert out['drc']['unconnected'] == 0


def test_hinge_range_is_degrees_without_mjcf(empty):
    """Parts-only projects compile with angle="radian"; range_deg [0, 270] must stay 270 degrees."""
    import math
    p, srv = empty
    call_parts(srv, 'add_shape', {'name': 'knob', 'shape': 'cylinder', 'size_mm': [14, 7], 'mass_kg': 0.002})
    call(srv, 'define_joint', {'name': 'turn', 'body': 'knob', 'axis': [0, 0, 1], 'range_deg': [0, 270],
                               'damping': 0.0005})
    from emcosim.mechanics import Mechanics
    m = Mechanics(p.root, Project(p.root).data['mechanics'])
    assert m.model.jnt_range[0][1] == pytest.approx(math.radians(270))
    m.set_drive('turn', lambda t: (math.radians(108), 0.0))       # passive damping must not stall it
    for _ in range(3000):
        m.step({})
    assert math.degrees(m.data.qpos[m.joints['turn'][0]]) == pytest.approx(108, abs=1)


@needs_kicad
def test_bind_pin_finds_the_pull_up_on_the_schematic(empty):
    _, srv = empty
    call_parts(srv, 'schematic_create', DESIGN)
    out = call(srv, 'bind_pin', {'pin': 'GPIO37', 'input': 'export_btn', 'net': 'BTN_EXPORT'})
    assert out['pull_up'].startswith('R4') and 'warnings' not in out
    with pytest.raises(ToolError, match='not in the schematic'):
        asyncio.run(srv.call_tool('bind_pin', {'pin': 'GPIO39', 'input': 'mode_btn', 'net': 'NOPE'}))


# ------------------------------------------------------------------ LCSC numbers against the design
from emcosim.parts_check import check_part

USB16 = {'model': 'TYPE-C 16PIN 2MD(073)', 'package': 'SMD', 'category': 'Connectors',
         'specs': {'Number of Contacts': '16P', 'Connector Type': 'Type-C'}}
R47K = {'model': '0402WGF4701TCE', 'package': '0402', 'category': 'Resistors', 'specs': {'Resistance': '4.7kΩ'}}
C10U_0603 = {'model': 'CL10A106KP8NNNC', 'package': '0603', 'category': 'Capacitors', 'specs': {'Capacitance': '10uF'}}
ME6211 = {'model': 'ME6211C33M5G-N', 'package': 'SOT-23-5', 'category': 'Power Management', 'specs': {}}
ESP32 = {'model': 'ESP32-PICO-V3-02', 'package': 'QFN-48-EP(7x7)', 'category': 'RF', 'specs': {}}


@pytest.mark.parametrize('ref, value, fp, info, pads, problem', [
    ('J1', 'USB-C', 'Connector_USB:USB_C_Receptacle_GCT_USB4125-xx-x_6P_TopMnt_Horizontal', USB16,
     ['A5', 'A9', 'A12', 'B5', 'B9', 'B12', 'SH'], '16-pin part'),
    ('R1', '5.1k', 'Resistor_SMD:R_0402_1005Metric', R47K, None, 'value 4.7k'),
    ('C3', '10uF', 'Capacitor_SMD:C_0402_1005Metric', C10U_0603, None, 'package 0603'),
    ('U3', 'TP4054', 'Package_TO_SOT_SMD:SOT-23-5', ME6211, None, 'part is ME6211'),
    ('U1', 'ESP32-PICO-V3-02', 'Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm', ESP32, None, '48 pins'),
])
def test_lcsc_mismatches_are_caught(ref, value, fp, info, pads, problem):
    r = check_part(ref, value, fp, info, pads)
    assert r['status'] == 'mismatch' and any(problem in x for x in r['problems']), r


@pytest.mark.parametrize('ref, value, fp, info', [
    ('R2', '4.7k', 'Resistor_SMD:R_0402_1005Metric', R47K),
    ('C1', '10uF', 'Capacitor_SMD:C_0603_1608Metric', C10U_0603),
    ('U4', 'ME6211C33M5G-N', 'Package_TO_SOT_SMD:SOT-23-5', ME6211),
    ('U1', 'ESP32-PICO-V3-02', 'Package_DFN_QFN:QFN-48-1EP_7x7mm_P0.5mm_EP5.3x5.3mm', ESP32),
])
def test_lcsc_matches_pass(ref, value, fp, info):
    assert check_part(ref, value, fp, info)['status'] == 'ok'


@needs_kicad
def test_board_parts_status_reports_a_wrong_lcsc(empty, monkeypatch):
    from emcosim import parts
    p, srv = empty
    bad = {**LED_BOARD, 'components': [dict(c) for c in LED_BOARD['components']]}
    bad['components'][1]['lcsc'] = 'C25900'                     # a 4.7 k on the 330 ohm resistor
    call_parts(srv, 'schematic_create', bad)
    monkeypatch.setattr(parts, 'part', lambda code, mpn=None: {**R47K, 'lcsc': code, 'stock': 100,
                                                                 'library_type': 'basic'})
    out = call(srv, 'board_parts_status')
    assert out['mismatches'] and out['mismatches'][0]['ref'] == 'R1'
    assert any('value 4.7k' in x for x in out['mismatches'][0]['problems'])


def test_fixtures_stay_off_the_model_page(empty, tmp_path):
    """A barbell is what the counter is tested on, not part of it: simulated, but the 建模 page
    shows only the product parts (the strap on the bar stays a product part)."""
    p, srv = empty
    call_parts(srv, 'add_shape', {'name': 'bar', 'shape': 'cylinder', 'size_mm': [28, 1200], 'pos': [0, 0, 1.0],
                                  'euler_deg': [0, 90, 0], 'mass_kg': 20, 'role': 'fixture'})
    call_parts(srv, 'add_shape', {'name': 'case', 'shape': 'box', 'size_mm': [48, 26, 14], 'parent': 'bar',
                                  'pos': [-0.021, 0, -0.35], 'euler_deg': [0, -90, 0], 'mass_kg': 0.03})
    call_parts(srv, 'add_shape', {'name': 'plate', 'shape': 'cylinder', 'size_mm': [450, 50], 'parent': 'bar',
                                  'pos': [0, 0, 0.5], 'mass_kg': 20})
    with pytest.raises(ToolError, match='role'):
        asyncio.run(srv.call_tool('add_shape', {'name': 'x', 'shape': 'box', 'size_mm': [1, 1, 1], 'role': 'jig'}))
    out = call(srv, 'part_edit', {'name': 'plate', 'role': 'fixture'})
    assert out['changed']['role'] == {'old': 'product', 'new': 'fixture'}
    call(srv, 'define_joint', {'name': 'bar_lift', 'body': 'bar', 'type': 'slide', 'axis': [1, 0, 0],
                               'range_mm': [-600, 600]})
    prj = Project(p.root)
    m = api.model_summary(prj)
    assert [b['name'] for b in m['bodies']] == ['case'] and [x['name'] for x in m['parts']] == ['case']
    assert {f['name'] for f in m['fixtures']} == {'bar', 'plate'} and m['fixture_joints'] == ['bar_lift']
    assert m['joints'] == []
    api.model_preview(prj, tmp_path / 'pv')
    desc = json.loads((tmp_path / 'pv' / 'scene.json').read_text())
    assert {g['name'] for g in desc['geoms']} == {'case_visual'}
    info = call(srv, 'model_info', {'tag': 'part'})
    assert {x['name']: x['role'] for x in info['part']} == {'bar': 'fixture', 'case': 'product', 'plate': 'fixture'}
    call(srv, 'part_edit', {'name': 'plate', 'role': 'product'})
    assert 'role' not in Project(p.root).data['mechanics']['parts']['plate']


def test_design_rules_and_skill_install(empty, tmp_path):
    from emcosim.skill import install_skill
    _, srv = empty
    res = asyncio.run(srv.call_tool('design_rules', {}))
    text = json.dumps(res, ensure_ascii=False, default=str)
    assert 'fixture' in text and '建模' in text and 'PCB 元件' in text
    out = install_skill(tmp_path / 'skills')
    assert out.read_text(encoding='utf8').startswith('---\nname: simworld\n')
