"""Desktop app backend: HTTP API, job queue, example copy, settings, chat parsing."""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from emcosim import api
from emcosim.app.server import App, make_server
from emcosim.app.settings import Settings

EXAMPLE = Path(__file__).resolve().parents[1] / 'examples' / 'switch_robot'


@pytest.fixture()
def project(tmp_path):
    return Path(api.copy_project(EXAMPLE, tmp_path / 'sr')['root'])


@pytest.fixture()
def client(project, tmp_path):
    app = App(project, Settings(tmp_path / 'settings.json'))
    httpd = make_server(app)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def call(method, path, body=None, token=app.token):
        req = urllib.request.Request(f'http://127.0.0.1:{port}/api/{path}', method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={'X-Emcosim-Token': token, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    call.app = app
    call.app_port = port
    yield call
    httpd.shutdown()


def wait_job(app, job_id, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = app.jobs.jobs[job_id]
        if j.status in ('done', 'failed', 'cancelled'):
            return j
        time.sleep(0.05)
    raise TimeoutError(job_id)


def test_copy_project_is_self_contained(project):
    xml = (project / 'mechanics' / 'switch_robot.xml').read_text()
    assert 'meshdir="assets"' in xml
    assert (project / 'mechanics' / 'assets' / 'arm_v06.obj').exists()
    assert not (project / 'runs').exists()


def test_api_requires_token(client):
    code, _ = client('GET', 'app', token='wrong')
    assert code == 401
    code, info = client('GET', 'app')
    assert code == 200 and info['project']


def _raw(client, method, path):
    port = client.app_port
    req = urllib.request.Request(f'http://127.0.0.1:{port}/api/{path}', method=method,
                                 headers={'X-Emcosim-Token': client.app.token})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def test_model_page_summary_and_preview(client):
    code, m = client('GET', 'model')
    assert code == 200 and m['has_model'] and not m['error']
    names = {b['name'] for b in m['bodies']}
    assert 'press_arm' in names and 'rocker' not in names            # the wall switch is a test fixture
    assert {'wall', 'rocker'} <= {f['name'] for f in m['fixtures']} and 'rocker_axis' in m['fixture_joints']
    servo = next(j for j in m['joints'] if j['name'] == 'servo_axis')
    assert servo['type'] == 'hinge' and servo['range'] == [-40, 40]
    assert any(p['label'] == '按压臂' and p['exists'] for p in m['print_parts'])
    assert m['materials']['PETG']['layer_strength_mpa'] > 0
    assert m['labels']['arm'] == {'label': '按压臂', 'print': 'press_arm'} and m['labels']['plate']['label'] == '开关面板'
    desc = json.loads(_raw(client, 'GET', 'model/scene.json'))
    assert desc['geoms'] and not any(g['name'].startswith(('pcb_', 'wall', 'rocker', 'plate')) for g in desc['geoms'])
    frames = _raw(client, 'GET', 'model/frames.bin')
    n, nb = (int.from_bytes(frames[i:i + 4], 'little') for i in (0, 4))
    assert n == 1 and nb == len(desc['bodies']) and len(frames) == 8 + 4 + nb * 7 * 4


def test_new_project_upload_model(client, tmp_path):
    code, _ = client('POST', 'project/new', {'path': str(tmp_path / 'blank'), 'name': 'blank'})
    assert code == 200
    code, m = client('GET', 'model')
    assert code == 200 and not m['has_model']
    import trimesh
    stl = trimesh.creation.box(extents=[20, 10, 5]).export(file_type='stl')
    port = client.app_port
    req = urllib.request.Request(f'http://127.0.0.1:{port}/api/upload/model?name=my%20box.stl', method='POST', data=stl,
                                 headers={'X-Emcosim-Token': client.app.token, 'Content-Type': 'application/octet-stream'})
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.loads(r.read())
    assert res['part'] == 'my_box' and res['mass_kg'] > 0
    assert (tmp_path / 'blank' / 'mechanics' / 'uploads' / 'my_box.stl').exists()
    code, m = client('GET', 'model')
    assert m['has_model'] and [b['name'] for b in m['bodies']] == ['my_box'] and not m['joints']
    assert json.loads(_raw(client, 'GET', 'model/scene.json'))['bodies'] == ['my_box']
    req = urllib.request.Request(f'http://127.0.0.1:{port}/api/upload/model?name=x.exe', method='POST', data=b'x',
                                 headers={'X-Emcosim-Token': client.app.token})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=30)
    assert e.value.code == 400


def test_project_param_and_scenario_roundtrip(client, project):
    code, prj = client('GET', 'project')
    assert code == 200 and 'servo_sweep_scripted' in prj['scenario_texts']
    code, r = client('POST', 'param', {'path': 'actuators.servo1.node', 'value': 'vin5'})
    assert code == 200 and r['old'] == 'v5_servo'
    assert 'node: vin5' in (project / 'project.yaml').read_text()
    code, r = client('PUT', 'scenario/quick', {'yaml': 'duration_s: 0.2\nmcu_backend: scripted\n'})
    assert code == 200 and 'quick' in r['scenarios']
    code, r = client('PUT', 'scenario/bad%20name', {'yaml': 'x: 1'})
    assert code == 400


def test_run_job_progress_and_cancel(client):
    app = client.app
    seen = []
    sub = app.bus.subscribe()
    code, job = client('POST', 'jobs/run', {'scenario': 'servo_sweep_scripted'})
    assert code == 200
    j = wait_job(app, job['id'])
    assert j.status == 'done', j.error
    while not sub.empty():
        seen.append(sub.get()['kind'])
    assert 'job_progress' in seen
    code, runs = client('GET', 'runs')
    assert runs[0]['run_id'] == j.result['run_id']
    code, sig = client('GET', f"run/{j.result['run_id']}/signals?names=mcu.powered&n=50")
    assert len(sig['t']) <= 50

    # a longer scripted scenario, cancelled while running: leaves no run directory
    client('PUT', 'scenario/long', {'yaml': 'duration_s: 30\nmcu_backend: scripted\n'})
    code, job = client('POST', 'jobs/run', {'scenario': 'long'})
    while app.jobs.jobs[job['id']].status != 'running':
        time.sleep(0.02)
    client('POST', f"jobs/{job['id']}/cancel")
    assert wait_job(app, job['id']).status == 'cancelled'
    code, runs2 = client('GET', 'runs')
    assert len(runs2) == len(runs)


def test_circuit_check_uses_latest_run(client):
    from emcosim.toolpaths import find_ngspice
    code, r = client('POST', 'circuit/check', {})
    assert code == 409 and '仿真' in r['error']          # no run yet
    if not find_ngspice():
        pytest.skip('ngspice not installed')
    code, job = client('POST', 'jobs/run', {'scenario': 'servo_sweep_scripted'})
    assert wait_job(client.app, job['id']).status == 'done'
    code, r = client('POST', 'circuit/check', {})
    assert code == 200 and r['scenario'] == 'servo_sweep_scripted'
    assert {row['net'] for row in r['compare']} >= {'VIN5', '3V3'}


def test_checks_gears_and_strength(client, project):
    code, c = client('GET', 'checks')
    assert code == 200 and [x['name'] for x in c['strength_parts']] == ['press_arm'] and c['strength'] == {}
    code, job = client('POST', 'jobs/run', {'scenario': 'servo_sweep_scripted'})
    assert wait_job(client.app, job['id']).status == 'done'
    code, g = client('POST', 'checks/gears', {})
    assert code == 200
    sg = g['gears'][0]
    assert sg['preset'] == 'sg90' and sg['assumed'] and len(sg['cases']) == 2
    stall = sg['cases'][1]
    assert stall['sf'] < sg['cases'][0]['sf'] and abs(stall['stress_mpa'] * stall['sf'] - sg['gear']['allow_mpa']) < 0.5
    try:
        import skfem  # noqa: F401
        from emcosim.strength.meshing import _ftetwild
        _ftetwild()
    except ImportError:
        pytest.skip('strength extra not installed')
    code, job = client('POST', 'jobs/strength', {'part': 'press_arm'})
    assert code == 200
    j = wait_job(client.app, job['id'], timeout=300)
    assert j.status == 'done', j.error
    r = j.result
    assert {c['case'] for c in r['cases']} == {'press', 'stall'} and r['min_sf'] > 1
    raw = _raw(client, 'GET', 'checks/press_arm/stress.bin')
    nv, nt = (int.from_bytes(raw[i:i + 4], 'little') for i in (0, 4))
    assert len(raw) == 8 + nv * 12 + nt * 12 + nv * 4 and nt > 100
    code, c = client('GET', 'checks')
    assert c['strength']['press_arm']['min_sf'] == r['min_sf']


def test_sweep_job_rows(client):
    code, job = client('POST', 'jobs/sweep', {'scenario': 'servo_sweep_scripted',
                                              'params': {'actuators.servo1.node': ['v5_servo', 'vin5']},
                                              'metrics': ['node.vin5.min_v']})
    j = wait_job(client.app, job['id'])
    assert j.status == 'done', j.error
    assert [r['actuators.servo1.node'] for r in j.rows] == ['v5_servo', 'vin5']


@pytest.mark.skipif(sys.platform == 'win32', reason='DPAPI path tested on Windows CI only via the app')
def test_settings_api_key_roundtrip(tmp_path):
    s = Settings(tmp_path / 's.json')
    s.set_api_key('sk-ant-test')
    assert Settings(tmp_path / 's.json').api_key() == 'sk-ant-test'
    assert 'sk-ant-test' not in json.dumps(s.public())


@pytest.mark.skipif(os.name == 'nt', reason='fake CLI is a POSIX script')
def test_claude_code_stream_parsing(client, tmp_path):
    """A stand-in for the claude CLI that prints the stream-json events of a turn
    with one tool call: the chat transcript must show the tool call, its result
    and the streamed reply, and pass the session id on the next turn."""
    log = tmp_path / 'argv.log'
    fake = tmp_path / 'claude'
    events = [
        {'type': 'system', 'subtype': 'init', 'session_id': 's', 'mcp_servers': [{'name': 'emcosim', 'status': 'connected'}]},
        {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 't1', 'name': 'mcp__emcosim__get_project', 'input': {'path': 'x'}}]}},
        {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': [{'type': 'text', 'text': '{"value": 1}'}]}]}},
        {'type': 'stream_event', 'event': {'type': 'message_start'}},
        {'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': '值是 '}}},
        {'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': '1'}}},
        {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': '值是 1'}]}},
        {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': '值是 1', 'total_cost_usd': 0.01, 'num_turns': 2},
    ]
    fake.write_text('#!' + sys.executable + '\nimport sys, json\n'
                    f'open({str(log)!r}, "a").write(json.dumps(sys.argv[1:]) + "\\n")\n'
                    'if sys.argv[1:3] == ["auth", "status"]:\n    print(json.dumps({"loggedIn": True})); sys.exit()\n'
                    'if sys.argv[1:] == ["--version"]:\n    print("9.9.9 (Claude Code)"); sys.exit()\n'
                    'sys.stdin.read()\n'
                    f'for e in {events!r}:\n    print(json.dumps(e), flush=True)\n', encoding='utf8')
    fake.chmod(0o755)
    app = client.app
    app.settings.update(claude_path=str(fake), chat_backend='claude_code')
    code, st = client('GET', 'chat/status')
    assert st['claude_code']['logged_in'] and st['claude_code']['version'].startswith('9.9.9')
    code, conv = client('POST', 'chat/new')
    for turn in range(2):
        code, _ = client('POST', f"chat/{conv['id']}/send", {'text': '查一下'})
        assert code == 200
        t0 = time.time()
        while app.chat.convs[conv['id']].busy and time.time() - t0 < 20:
            time.sleep(0.05)
    items = app.chat.get(conv['id'])['items']
    roles = [i['role'] for i in items]
    assert roles[:4] == ['user', 'tool', 'assistant', 'meta']
    assert items[1]['name'] == 'get_project' and items[1]['status'] == 'ok'
    assert items[2]['text'] == '值是 1'
    calls = [json.loads(line) for line in log.read_text().splitlines() if '-p' in line]
    assert '--session-id' in calls[0] and '--resume' in calls[1]
    assert calls[0][calls[0].index('--session-id') + 1] == calls[1][calls[1].index('--resume') + 1]
