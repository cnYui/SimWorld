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
