"""KiCad (board), ngspice (circuit) and MuJoCo (model) kernels behind the MCP tools."""
import asyncio

import pytest

from emcosim import api, eda, model_edit
from emcosim.electrical import board_spice
from emcosim.mcp_server import build_server
from emcosim.project import Project
from emcosim.toolpaths import find_ngspice

from conftest import EXAMPLE

needs_kicad = pytest.mark.skipif(not eda.find_cli() or not eda.find_python(), reason='KiCad not installed')
needs_ngspice = pytest.mark.skipif(not find_ngspice(), reason='ngspice not installed')


@pytest.fixture
def prj(tmp_path):
    api.copy_project(EXAMPLE, tmp_path / 'p')
    return Project(tmp_path / 'p')


def test_mcp_lists_kernel_tools():
    tools = asyncio.run(build_server(EXAMPLE).list_tools())
    names = {t.name for t in tools}
    assert {'board_drc', 'board_edit', 'board_import', 'circuit_check', 'model_info', 'model_edit',
            'parts_search', 'part_info', 'parts_alternatives', 'board_parts_status'} <= names


def test_parts_lookup_reports_unreachable_server(monkeypatch):
    from emcosim import parts
    monkeypatch.setattr(parts, 'ENDPOINT', 'http://127.0.0.1:9/mcp')
    with pytest.raises(parts.PartsUnavailable, match='unavailable'):
        parts.call('jlc_get_part', {'lcsc': 'C6186'}, timeout=5)


def test_value_parsing():
    assert board_spice.parse_value('470uF') == pytest.approx(470e-6)
    assert board_spice.parse_value('5.1kΩ') == pytest.approx(5100)
    assert board_spice.parse_value('4K7') == pytest.approx(4700)
    assert board_spice.parse_value('100nF') == pytest.approx(1e-7)
    assert board_spice.parse_value('ESP32') is None


def test_easyeda_netlist_reader():
    n = eda.read_netlist(EXAMPLE / 'pcb/v05/source/carrier_v05.net')
    assert n['components']['U4']['lcsc'] == 'C6186'
    assert set(n['nets']['VIN5']) == {'C3-1', 'D1-1', 'F1-1', 'J1-1', 'U4-3'}
    assert 'U3-49' in n['nets']['GND']


@needs_kicad
def test_board_drc_and_edit(prj):
    board = prj.root / prj.data['board']['kicad_pcb']
    before = eda.drc(board)
    assert before['errors'] <= 1 and before['unconnected'] == 0      # C1/F1 courtyard overlap only
    res = eda.edit_pcb(board, [{'op': 'set_value', 'ref': 'R5', 'value': '330'},
                               {'op': 'set_field', 'ref': 'R5', 'field': 'LCSC', 'value': 'C23138'},
                               {'op': 'move', 'ref': 'C1', 'x_mm': 128.0, 'y_mm': 108.5}])
    assert res['applied'][0]['was'] and (board.parent / res['backup']).exists()
    text = board.read_text(encoding='utf-8')
    assert '"330"' in text and 'C23138' in text


@needs_ngspice
def test_circuit_check_against_cosim(prj):
    run = api.run_scenario(prj, 'toggle_scripted')
    res = board_spice.check(prj, run['run_id'])
    rows = {r['net']: r for r in res['compare']}
    assert set(rows) == {'VIN5', 'V5_SERVO', '3V3'}
    # same circuit, different detail (diode curve vs fixed drop): within 0.25 V
    assert all(r['max_abs_diff_v'] < 0.25 for r in rows.values())
    assert {'D1', 'U4', 'F1', 'C1'} <= {p['ref'] for p in res['parts_modelled']}


def test_model_edit_validates_and_backs_up(prj):
    names = {g['name'] for g in model_edit.info(prj, 'geom')['geom']}
    assert 'arm_collision_0' in names
    res = model_edit.edit(prj, [{'tag': 'geom', 'name': 'arm_collision_0', 'attr': 'friction',
                                 'value': [0.6, 0.005, 0.0001]}])
    path = model_edit.mjcf_path(prj)
    assert 'friction="0.6 0.005 0.0001"' in path.read_text(encoding='utf-8')
    assert (path.parent / res['backup']).exists()
    before = path.read_text(encoding='utf-8')
    with pytest.raises(ValueError, match='MuJoCo rejected'):
        model_edit.edit(prj, [{'tag': 'geom', 'name': 'arm_collision_0', 'attr': 'type', 'value': 'nonsense'}])
    assert path.read_text(encoding='utf-8') == before
