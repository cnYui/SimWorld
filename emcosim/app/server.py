"""Local HTTP API + static UI for the emcosim app.

    emcosim app [--project DIR]            desktop window (pywebview)
    emcosim serve [--project DIR]          same UI in the browser

Binds to 127.0.0.1 only. Every request must carry the per-launch token (sent
as the X-Emcosim-Token header, or ?token= for EventSource/asset URLs), so other
local web pages cannot drive the app.

JSON API (all under /api):
  GET  app                       version, project root, recent projects, settings, qemu status
  POST project/open {path}       open a project directory
  POST project/new {path,name}   create and open an empty project
  POST project/open_example {path[,dest]}  copy a bundled example to Documents/emcosim and open it
  GET  project                   overview + full project.yaml data + scenario texts
  POST param {path,value}        set_param (persists to project.yaml)
  GET  scenario/<name>           {name, yaml}
  PUT  scenario/<name> {yaml}    write_scenario
  POST jobs/run {scenario, overrides}
  POST jobs/sweep {scenario, params, metrics, overrides}
  GET  jobs                      recent jobs
  POST jobs/<id>/cancel
  GET  runs                      run list
  GET  run/<id>                  meta (+ signal names)
  GET  run/<id>/signals?names=a,b&n=4000
  GET  run/<id>/scene.json|scene.bin|frames.bin|uart.log
  DELETE run/<id>
  POST import/pcb|model|firmware {path, ...}
  GET  model                     mechanical model summary (bodies, joints, imported and print parts)
  GET  model/scene.json|scene.bin|frames.bin   the model at its initial pose, for the 建模 page
  POST upload/model?name=x.stl   raw file body: saved under mechanics/uploads and imported as a part
  POST annotate/joint {...}, annotate/pin {...}
  GET  checks                    strength-checkable print parts + last results; POST jobs/strength {part, run_id?}
  POST checks/gears {run_id?}    servo output gear bending stress (Lewis)
  GET  checks/<part>/stress.bin  surface mesh + von Mises per vertex of the last strength check
  POST circuit/check {run_id?}   ngspice on the board netlist with a run's load currents
  GET  pcb                       PCB summary (components, nets, board, copper)
  GET  printing                  printable parts + whether Bambu Studio is installed
  POST printing/send {names, with_settings, open}  export a 3MF and open it in Bambu Studio
  GET  settings / POST settings {...} / POST settings/api_key {key}
  GET  chat/status, POST chat/login, POST chat/logout
  GET  chat, POST chat/new, GET chat/<id>, POST chat/<id>/send {text}, POST chat/<id>/stop
  GET  events?token=             Server-Sent Events: job, job_progress, chat, project_changed,
                                 scenarios_changed, runs_changed, auth_changed, auth_log
"""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import socket
import sys
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import __version__, api
from ..project import Project
from ..results import RunStore
from .chat import ChatManager
from .events import EventBus, ProjectWatcher
from .jobs import JobManager
from .settings import Settings

STATIC = Path(__file__).parent / 'static'
RUN_FILES = {'scene.json': 'application/json', 'scene.bin': 'application/octet-stream',
             'frames.bin': 'application/octet-stream', 'uart.log': 'text/plain; charset=utf-8'}


class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


class App:
    """State shared by all request handlers."""

    def __init__(self, project_root=None, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.bus = EventBus()
        self.token = os.environ.get('EMCOSIM_APP_TOKEN') or secrets.token_urlsafe(24)
        self._root = None
        self._lock = threading.RLock()
        self.downloads = {}
        self.jobs = JobManager(self.bus, self.project)
        self.chat = ChatManager(self.bus, self.settings, lambda: self._root)
        self.watcher = ProjectWatcher(self.bus, lambda: self._root)
        self.watcher.start()
        if project_root:
            self.open_project(project_root)
        elif self.settings.data.get('recent_projects'):
            for p in self.settings.data['recent_projects']:
                if (Path(p) / 'project.yaml').exists():
                    self.open_project(p)
                    break

    # -- project -----------------------------------------------------------
    def open_project(self, root):
        root = Path(root).expanduser().resolve()
        if not (root / 'project.yaml').exists():
            raise ApiError(f'{root} 里没有 project.yaml')
        with self._lock:
            self._root = root
        self.settings.touch_project(root)
        self.bus.publish('project_opened', {'root': str(root)})
        return {'root': str(root)}

    def model_preview(self, prj: Project):
        """Preview of the mechanical model, rebuilt when the project file or a model file changes."""
        files = [prj.root / 'project.yaml']
        for sub in ('mechanics', 'pcb'):
            d = prj.root / sub
            if d.is_dir():
                files += [f for f in d.rglob('*') if f.is_file() and f.suffix.lower() in
                          ('.xml', '.obj', '.stl', '.msh', '.yaml', '.png')]
        sig = (str(prj.root), max((f.stat().st_mtime_ns for f in files), default=0), len(files))
        with self._lock:
            if getattr(self, '_preview', None) and self._preview[0] == sig and self._preview[1].exists():
                return self._preview[1]
            import tempfile
            out = Path(tempfile.mkdtemp(prefix='simworld-model-'))
            api.model_preview(prj, out)
            old = getattr(self, '_preview', None)
            self._preview = (sig, out)
        if old:
            shutil.rmtree(old[1], ignore_errors=True)
        return out

    def project(self) -> Project:
        if self._root is None:
            raise ApiError('还没有打开工程', 409)
        return Project(self._root)

    @property
    def root(self):
        return self._root


def _qemu_status():
    try:
        from ..mcu.qemu import find_qemu
        exe, roms = find_qemu()
        return {'found': True, 'path': exe, 'roms': roms}
    except RuntimeError as e:
        return {'found': False, 'error': str(e)}


def _design_board(prj):
    """The PCB page's parts list and pictures for a board designed in SimWorld (schematic_create /
    board_from_schematic / board_import), when there is no hand-made parts.json."""
    b = prj.data.get('board') or {}
    src = b.get('kicad_sch') or b.get('kicad_pcb')
    if not src:
        return None
    d = (prj.root / src).parent
    if not d.is_relative_to(prj.root / 'pcb'):
        return None
    images = {}
    try:
        from ..design import render
        for key, rel, fn in (('schematic', b.get('kicad_sch'), render.schematic_png),
                             ('board', b.get('kicad_pcb'), render.board_png)):
            if not rel or not (prj.root / rel).exists():
                continue
            png = d / f'page_{key}.png'
            if not png.exists() or png.stat().st_mtime < (prj.root / rel).stat().st_mtime:
                fn(prj.root / rel, png, 1400)
            images[key] = png.name
    except Exception:          # pictures are a convenience; the parts list still shows
        pass
    parts = {}
    try:
        if b.get('kicad_sch'):
            from .. import eda
            nl = eda.netlist(prj.root / b['kicad_sch'])
            rows = [(c['ref'], c.get('value') or '', c.get('footprint') or '',
                     next((v for k, v in (c.get('fields') or {}).items() if k.lower() == 'lcsc'), None))
                    for c in nl['components'] if not c['ref'].startswith('#')]
        else:
            from ..importers.kicad import load_kicad_pcb
            rows = [(c['ref'], c.get('value') or '', c.get('footprint') or '', c.get('lcsc'))
                    for c in load_kicad_pcb(prj.root / b['kicad_pcb'])['components']]
    except Exception:
        rows = []
    for ref, value, fp, lcsc in rows:
        key = (value, fp, lcsc)
        e = parts.setdefault(key, {'refs': [], 'name': value, 'manufacturer': fp.split(':', 1)[-1], 'role': '',
                                    'qty': 0, 'lcsc': lcsc, 'jlc_library': '',
                                    'mount': 'THT' if re.search(r'THT|PinHeader|PinSocket', fp)
                                    else 'SMD'})
        e['refs'].append(ref)
        e['qty'] += 1
    return {'id': d.name, 'board': d.name, 'source': 'design', 'images': images,
            'parts': sorted(parts.values(), key=lambda e: e['refs'][0]), 'checked_at': None}


def _scenario_info(project, texts):
    """Title / description / whether QEMU is needed, for the simple panel."""
    import yaml
    out = {}
    for name, text in texts.items():
        try:
            d = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            d = {}
        backend = d.get('mcu_backend') or (project.data.get('mcu') or {}).get('backend') or 'qemu'
        out[name] = {'title': d.get('title') or name, 'description': d.get('description') or '',
                     'needs_qemu': backend == 'qemu', 'duration_s': d.get('duration_s')}
    return out


def _scenario_texts(project):
    out = {}
    d = project.root / 'scenarios'
    for name in project.scenarios():
        try:
            out[name] = (d / f'{name}.yaml').read_text(encoding='utf8')
        except OSError:
            pass
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = 'emcosim'
    protocol_version = 'HTTP/1.1'

    def __init__(self, *args, app: App = None, **kw):
        self.app = app
        super().__init__(*args, **kw)

    def log_message(self, *a):
        pass

    # -- plumbing ----------------------------------------------------------
    def _send(self, body: bytes, ctype, code=200, extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, default=str, ensure_ascii=False).encode('utf8'),
                   'application/json; charset=utf-8', code)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode('utf8'))
        except ValueError:
            raise ApiError('请求体不是合法 JSON')

    def _authorized(self, query):
        tok = self.headers.get('X-Emcosim-Token') or (query.get('token') or [''])[0]
        return secrets.compare_digest(tok, self.app.token)

    def do_GET(self):
        self._dispatch('GET')

    def do_HEAD(self):
        self._dispatch('GET')

    def do_POST(self):
        self._dispatch('POST')

    def do_PUT(self):
        self._dispatch('PUT')

    def do_DELETE(self):
        self._dispatch('DELETE')

    def _dispatch(self, method):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        parts = [unquote(p) for p in url.path.split('/') if p]
        if len(parts) >= 2 and parts[0] == 'dl' and method == 'GET':
            # one-time download links handed to Bambu Studio (bambustudio://open?file=...)
            f = self.app.downloads.pop(parts[1], None)
            if not f or not Path(f).is_file():
                return self._json({'error': 'not found'}, 404)
            return self._send(Path(f).read_bytes(), 'application/vnd.ms-package.3dmanufacturing-3dmodel+xml')
        if not parts or parts[0] != 'api':
            if method != 'GET':
                return self._json({'error': 'not found'}, 404)
            return self._static(url.path)
        # The page itself is served without a token (the desktop shell and
        # `emcosim serve` put the token in the URL fragment); every API call needs it.
        if not self._authorized(q):
            return self._json({'error': 'unauthorized'}, 401)
        try:
            if parts[1:] == ['events']:
                return self._events()
            out = self._route(method, parts[1:], q)
            if out is not None:
                self._json(out)
        except ApiError as e:
            self._json({'error': str(e)}, e.code)
        except (FileNotFoundError, KeyError) as e:
            self._json({'error': str(e).strip("'\"")}, 404)
        except (ValueError, RuntimeError, TypeError) as e:
            self._json({'error': f'{e}'}, 400)

    def _upload(self, p, q):
        """POST /api/upload/model?name=part.stl with the file as the raw body: saved under
        mechanics/uploads/ and imported as a part (same as import_model)."""
        if p != ['model']:
            raise ApiError('not found', 404)
        prj = self.app.project()
        name = Path((q.get('name') or [''])[0]).name
        suffix = Path(name).suffix.lower()
        if suffix not in api.MODEL_UPLOAD_TYPES:
            raise ApiError('只支持 STL、STEP、OBJ 文件')
        n = int(self.headers.get('Content-Length') or 0)
        if not n or n > 300 * 1024 * 1024:
            raise ApiError('文件是空的或超过 300 MB')
        stem = re.sub(r'[^\w\-]+', '_', Path(name).stem).strip('_') or 'part'
        part = stem if stem[0].isalpha() else 'part_' + stem
        taken = set((prj.data.get('mechanics') or {}).get('parts') or {})
        base, i = part, 2
        while part in taken:
            part, i = f'{base}_{i}', i + 1
        dest = prj.root / 'mechanics' / 'uploads' / f'{part}{suffix}'
        dest.parent.mkdir(parents=True, exist_ok=True)
        remaining = n
        with open(dest, 'wb') as f:
            while remaining:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        units = (q.get('units') or ['mm'])[0]
        return api.import_model(prj, dest, name=part, units=units if units in ('mm', 'cm', 'm', 'in') else 'mm')

    def _static(self, path):
        rel = path.lstrip('/') or 'index.html'
        f = (STATIC / rel).resolve()
        if not str(f).startswith(str(STATIC.resolve())) or not f.is_file():
            f = STATIC / 'index.html'
        ctype = mimetypes.guess_type(f.name)[0] or 'application/octet-stream'
        if f.suffix == '.js':
            ctype = 'text/javascript'
        if ctype.startswith('text/') or ctype in ('application/json', 'text/javascript'):
            ctype += '; charset=utf-8'
        self._send(f.read_bytes(), ctype)

    def _events(self):
        sub = self.app.bus.subscribe()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        try:
            self.wfile.write(b'retry: 1500\n\n')
            self.wfile.flush()
            while True:
                try:
                    msg = sub.get(timeout=15)
                    data = json.dumps(msg, default=str, ensure_ascii=False)
                    self.wfile.write(f'event: {msg["kind"]}\ndata: {data}\n\n'.encode('utf8'))
                except queue.Empty:
                    self.wfile.write(b': ping\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            self.app.bus.unsubscribe(sub)
            self.close_connection = True

    # -- routes ------------------------------------------------------------
    def _route(self, method, p, q):
        app = self.app
        head = p[0] if p else ''
        if head == 'upload' and method == 'POST':
            return self._upload(p[1:], q)
        b = self._body() if method in ('POST', 'PUT') else {}

        if head == 'app' and method == 'GET':
            return {'version': __version__, 'project': str(app.root) if app.root else None,
                    'recent_projects': [r for r in app.settings.data.get('recent_projects', [])
                                        if (Path(r) / 'project.yaml').exists()],
                    'settings': app.settings.public(), 'qemu': _qemu_status(),
                    'examples': [str(x) for x in _bundled_examples()],
                    'projects_dir': str(_documents_dir())}

        if head == 'project':
            if p[1:] == ['open'] and method == 'POST':
                return app.open_project(b['path'])
            if p[1:] == ['open_example'] and method == 'POST':
                # Examples ship read-only (Program Files); work on a copy in Documents.
                src = Path(b['path'])
                dst = Path(b.get('dest') or _documents_dir() / src.name)
                return {**app.open_project(api.copy_project(src, dst)['root']), 'copied_to': str(dst)}
            if p[1:] == ['new'] and method == 'POST':
                # {path} as before, or just {name}: a new folder under Documents/SimWorld
                path = b.get('path')
                if not path:
                    name = (b.get('name') or '').strip()
                    stem = re.sub(r'[\\/:*?"<>|\s]+', '_', name).strip('._') or 'project'
                    path, i = _documents_dir() / stem, 2
                    while path.exists():
                        path, i = _documents_dir() / f'{stem}_{i}', i + 1
                elif (Path(path) / 'project.yaml').exists():
                    raise ApiError(f'{path} 里已经有一个工程了，换个文件夹或者直接打开它')
                prj = Project.create(path, b.get('name'))
                return app.open_project(prj.root)
            if len(p) == 1 and method == 'GET':
                prj = app.project()
                ov = api.project_overview(prj)
                ov['data'] = prj.data
                ov['scenario_texts'] = _scenario_texts(prj)
                ov['scenario_info'] = _scenario_info(prj, ov['scenario_texts'])
                ov['firmware_params'] = _fw_param_defaults()
                ov['presets'] = _motor_presets()
                return ov

        if head == 'param' and method == 'POST':
            return api.set_param(app.project(), b['path'], b['value'])

        if head == 'scenario' and len(p) == 2:
            prj = app.project()
            if method == 'GET':
                return {'name': p[1], 'yaml': (prj.root / 'scenarios' / f'{p[1]}.yaml').read_text(encoding='utf8')}
            if method == 'PUT':
                return api.write_scenario(prj, p[1], b['yaml'])
            if method == 'DELETE':
                (prj.root / 'scenarios' / f'{p[1]}.yaml').unlink()
                return {'deleted': p[1]}

        if head == 'jobs':
            if p[1:] == ['run'] and method == 'POST':
                app.project()
                return app.jobs.submit('run', scenario=b['scenario'], overrides=b.get('overrides') or {})
            if p[1:] == ['strength'] and method == 'POST':
                from .. import checks
                names = [x['name'] for x in checks.strength_parts(app.project())]
                if b.get('part') not in names:
                    raise ApiError(f"没有这个可检测的打印件：{b.get('part')}")
                try:
                    import skfem  # noqa: F401
                    from ..strength.meshing import _ftetwild
                    _ftetwild()
                except ImportError as e:
                    raise ApiError(f'强度检测要的有限元库还没装好（{e}）。运行 scripts\\setup 后再试。', 409)
                return app.jobs.submit('strength', part=b['part'], run_id=b.get('run_id'))
            if p[1:] == ['sweep'] and method == 'POST':
                app.project()
                if not b.get('params'):
                    raise ApiError('扫参至少要一个参数')
                return app.jobs.submit('sweep', scenario=b['scenario'], params=b['params'],
                                       metrics=b.get('metrics') or [], overrides=b.get('overrides') or {})
            if len(p) == 1 and method == 'GET':
                return app.jobs.list()
            if len(p) == 3 and p[2] == 'cancel' and method == 'POST':
                return app.jobs.cancel(p[1])
            if len(p) == 2 and method == 'GET':
                return app.jobs.jobs[p[1]].public(live=True)

        if head == 'runs' and method == 'GET':
            return RunStore(app.project().root).list()

        if head == 'run' and len(p) >= 2:
            run_id = p[1]
            if not re.fullmatch(r'[\w.\-]+', run_id):
                raise ApiError('bad run id')
            store = RunStore(app.project().root)
            if method == 'DELETE' and len(p) == 2:
                return api.delete_run(app.project(), run_id)
            d = store.dir(run_id)
            if len(p) == 2:
                meta = store.meta(run_id)
                meta.pop('resolved_project', None)
                meta['signals'] = store.signal_names(run_id)
                return meta
            if p[2] == 'signals':
                names = q.get('names', [''])[0].split(',') if q.get('names') else None
                t0 = float(q['t0'][0]) if q.get('t0') else None
                t1 = float(q['t1'][0]) if q.get('t1') else None
                return store.signals(run_id, [n for n in names or [] if n] or None, t0, t1,
                                     max_points=int(q.get('n', ['4000'])[0]))
            if p[2] == 'resolved':
                return store.meta(run_id).get('resolved_project')
            if p[2] in RUN_FILES and (d / p[2]).exists():
                self._send((d / p[2]).read_bytes(), RUN_FILES[p[2]])
                return None
            raise ApiError('not found', 404)

        if head == 'import' and method == 'POST' and len(p) == 2:
            prj = app.project()
            kind = p[1]
            if kind == 'pcb':
                return api.import_pcb(prj, b['path'], dsn=b.get('dsn'), bom=b.get('bom'),
                                      placement=b.get('placement'))
            if kind == 'model':
                return api.import_model(prj, b['path'], name=b.get('name'), parent=b.get('parent', 'world'),
                                        pos=b.get('pos'), euler_deg=b.get('euler_deg'), units=b.get('units', 'mm'),
                                        density=float(b.get('density', 1240.0)), mass_kg=b.get('mass_kg'),
                                        collision=b.get('collision', 'hull'))
            if kind == 'firmware':
                return api.import_firmware(prj, b['path'], b.get('name', 'firmware'))

        if head == 'annotate' and method == 'POST' and len(p) == 2:
            prj = app.project()
            if p[1] == 'joint':
                b = dict(b)
                return api.define_joint(prj, b.pop('name'), b.pop('body'), **b)
            if p[1] == 'pin':
                b = dict(b)
                return api.bind_pin(prj, b.pop('pin'), **b)

        if head == 'model' and method == 'GET':
            prj = app.project()
            if len(p) == 1:
                return api.model_summary(prj)
            if len(p) == 2 and p[1] in ('scene.json', 'scene.bin', 'frames.bin'):
                d = app.model_preview(prj)
                self._send((d / p[1]).read_bytes(), RUN_FILES[p[1]])
                return None
            raise ApiError('not found', 404)

        if head == 'boards' and method == 'GET':
            # Purchasable parts per board: pcb/<board>/parts.json (+ images next to it).
            root = app.project().root / 'pcb'
            if len(p) == 1:
                out = []
                for f in sorted(root.glob('*/parts.json')):
                    d = json.loads(f.read_text(encoding='utf8'))
                    d['id'] = f.parent.name
                    out.append(d)
                if not out:
                    d = _design_board(app.project())
                    if d:
                        out.append(d)
                return out
            if len(p) == 3 and re.fullmatch(r'[\w\-]+', p[1]) and re.fullmatch(r'[\w\-]+\.(png|jpg|jpeg|svg)', p[2]):
                f = root / p[1] / p[2]
                if not f.is_file():
                    raise ApiError('not found', 404)
                self._send(f.read_bytes(), mimetypes.guess_type(f.name)[0] or 'application/octet-stream')
                return None
            raise ApiError('not found', 404)

        if head == 'part' and len(p) == 2 and method == 'GET':
            # live stock / price for the PCB page, through the built-in parts lookup
            from .. import parts
            if not re.fullmatch(r'C\d{1,10}', p[1]):
                raise ApiError('bad LCSC number')
            try:
                return parts.part(p[1])
            except parts.PartsUnavailable as e:
                raise ApiError(str(e), 503)

        if head == 'ai_setup' and method == 'GET':
            return _ai_setup(app.root)
        if head == 'mcp_guide' and method == 'GET':
            return _mcp_guide()

        if head == 'board' and p[1:] == ['check'] and method == 'POST':
            from .. import eda
            prj = app.project()
            board = (prj.data.get('board') or {}).get('kicad_pcb')
            if not board:
                raise ApiError('这个工程还没有 KiCad 板子文件（board.kicad_pcb）')
            if not eda.find_cli():
                raise ApiError('没有找到 KiCad。装好 KiCad 9 后再点检查（kicad.org/download）。', 409)
            return eda.drc(prj.root / board, limit=60)

        if head == 'checks':
            # 仿真 page: strength of printed parts (FEA, as a job) and servo gear stress
            from .. import checks
            prj = app.project()
            if len(p) == 1 and method == 'GET':
                return {'strength_parts': checks.strength_parts(prj), 'strength': checks.last_results(prj)}
            if p[1:] == ['gears'] and method == 'POST':
                return checks.gear_check(prj, b.get('run_id'))
            if len(p) == 3 and p[2] == 'stress.bin' and re.fullmatch(r'[\w\-]+', p[1]):
                f = prj.root / checks.RESULTS_DIR / p[1] / 'stress.bin'
                if not f.is_file():
                    raise ApiError('not found', 404)
                self._send(f.read_bytes(), 'application/octet-stream')
                return None
            raise ApiError('not found', 404)

        if head == 'circuit' and p[1:] == ['check'] and method == 'POST':
            # ngspice on the real board netlist, driven by the currents of a run (latest by default)
            from ..electrical import board_spice
            from ..toolpaths import find_ngspice
            prj = app.project()
            if not (prj.data.get('circuit') or {}).get('netlist'):
                raise ApiError('这个工程还没有设置电路检查（project.yaml 里的 circuit.netlist）')
            run_id = b.get('run_id')
            if not run_id:
                runs = [r for r in RunStore(prj.root).list() if '-sweep' not in r['run_id']]
                if not runs:
                    raise ApiError('还没有仿真结果。电路检查要用一次仿真里的舵机和单片机电流，先在“仿真”页跑一个测试。', 409)
                run_id = runs[0]['run_id']
            if not find_ngspice():
                raise ApiError('没有找到 ngspice。运行 scripts\\setup 安装后再试。', 409)
            res = board_spice.check(prj, run_id)
            res['scenario'] = RunStore(prj.root).meta(run_id).get('scenario', {}).get('name')
            return res

        if head == 'pcb' and method == 'GET':
            prj = app.project()
            pcb = prj.pcb()
            if not pcb:
                return {}
            return pcb

        if head == 'printing':
            from .. import printing
            prj = app.project()
            if len(p) == 1 and method == 'GET':
                return {'parts': printing.printable_parts(prj),
                        'bambu': {'found': bool(printing.find_bambu_studio()), 'path': printing.find_bambu_studio(),
                                  'url_handler': printing.url_handler_registered()}}
            if p[1:] == ['send'] and method == 'POST':
                port = self.server.server_address[1]

                def url_for(path):
                    key = secrets.token_urlsafe(16)
                    app.downloads[key] = path
                    return f'http://127.0.0.1:{port}/dl/{key}/{path.name}'
                return printing.send_parts(prj, b.get('names') or None, bool(b.get('with_settings')),
                                           download_url_for=url_for, open_app=b.get('open', True))

        if head == 'settings':
            if len(p) == 1 and method == 'GET':
                return app.settings.public()
            if len(p) == 1 and method == 'POST':
                app.settings.update(**b)
                return app.settings.public()
            if p[1:] == ['api_key'] and method == 'POST':
                app.settings.set_api_key(b.get('key'))
                return app.settings.public()

        if head == 'chat':
            c = app.chat
            if p[1:] == ['status']:
                return c.status()
            if p[1:] == ['login'] and method == 'POST':
                return c.login(console=bool(b.get('console')))
            if p[1:] == ['logout'] and method == 'POST':
                return c.logout()
            if len(p) == 1 and method == 'GET':
                return c.list()
            if p[1:] == ['new'] and method == 'POST':
                return c.new()
            if len(p) == 2 and method == 'GET':
                return c.get(p[1])
            if len(p) == 3 and p[2] == 'send' and method == 'POST':
                text = (b.get('text') or '').strip()
                if not text:
                    raise ApiError('消息是空的')
                return c.send(p[1], text)
            if len(p) == 3 and p[2] == 'stop' and method == 'POST':
                return c.stop(p[1])

        raise ApiError(f'unknown endpoint {method} /api/{"/".join(p)}', 404)


def _fw_param_defaults():
    try:
        from ..firmware import FW_PARAMS_DEFAULTS
        return FW_PARAMS_DEFAULTS
    except ImportError:
        return {}


def _motor_presets():
    try:
        from ..electrical.motors import PRESETS
        return PRESETS
    except ImportError:
        return {}


def _documents_dir():
    docs = Path.home() / 'Documents'
    d = (docs if docs.exists() else Path.home()) / 'SimWorld'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _bundled_examples():
    """Example projects shipped next to the package (source tree or installer)."""
    here = Path(__file__).resolve()
    cands = [here.parents[2] / 'examples', here.parents[1] / 'examples']
    import sys
    if getattr(sys, 'frozen', False):
        cands.insert(0, Path(sys.executable).parent / 'examples')
    out = []
    for c in cands:
        if c.is_dir():
            out += [d for d in sorted(c.iterdir()) if (d / 'project.yaml').exists()]
    return out


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind a port that is already
    # in use, and requests then go to whichever process Windows picks.
    allow_reuse_address = sys.platform != 'win32'

    def server_bind(self):
        if sys.platform == 'win32' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(app: App, host='127.0.0.1', port=0):
    handler = partial(Handler, app=app)
    try:
        return _Server((host, port), handler)
    except OSError:
        if not port:
            raise
        print(f'port {port} is in use, picking a free one', file=sys.stderr)
        return _Server((host, 0), handler)


_GUIDE = {}


def _mcp_guide():
    """The 'MCP 使用说明' page: every tool of the MCP server in plain words, plus Markdown to copy."""
    if 'g' not in _GUIDE:
        from ..mcp_docs import GROUPS, guide_markdown, mcp_guide
        try:
            import asyncio
            from ..mcp_server import build_server
            from .link import AppLink
            tools = [(t.name, t.description) for t in asyncio.run(build_server(None, link=AppLink({})).list_tools())]
        except Exception:      # MCP package missing: the written guide still lists the tools
            tools = [(t, '') for _, _, _, rows in GROUPS for t, _, _ in rows]
        sections = mcp_guide(tools)
        _GUIDE['g'] = {'sections': sections, 'markdown': guide_markdown(sections), 'count': len(tools)}
    return {**_GUIDE['g'], 'setup': _ai_setup()}


def _ai_setup(root=None):
    """How to connect Claude Code / Codex to the MCP server, with this machine's paths. The MCP
    server follows the project open in the page (app/link.py), so no project is baked in."""
    if getattr(sys, 'frozen', False):
        exe = Path(sys.executable).with_name('emcosim.exe' if sys.platform == 'win32' else 'emcosim')
        cmd = [str(exe)]
    else:
        cmd = [sys.executable, '-m', 'emcosim']
    args = [*cmd[1:], 'mcp']
    q = (lambda x: f'"{x}"' if ' ' in x or '\\' in x else x)
    claude = 'claude mcp add emcosim -- ' + ' '.join(q(x) for x in [cmd[0], *args])
    toml_list = ', '.join(json.dumps(a) for a in args)
    codex = f'[mcp_servers.emcosim]\ncommand = {json.dumps(cmd[0])}\nargs = [{toml_list}]'
    claude = claude.replace('claude mcp add emcosim', 'claude mcp add -s user emcosim', 1)
    codex_cfg = str(Path.home() / '.codex' / 'config.toml')
    skill = lambda target: ' '.join(q(x) for x in [*cmd, 'skill', target])  # noqa: E731
    about = (
        '说明：你操作的就是我浏览器里开着的 SimWorld 页面。我可以直接跟你说“打开示例工程”“打开 D:\\xxx 这个工程”'
        '“新建一个工程”，你用 open_example、open_project、new_project，页面会跟着切过去。'
        '工程打开后，你可以改 3D 模型（model_edit）、用 KiCad 检查和修改 PCB（board_drc、board_edit）、'
        '跑电路检查（circuit_check）和仿真测试（run_scenario），结果会出现在页面上，还能查嘉立创元件（parts_search、part_info）。'
        '设计前先读 SimWorld 的规则（skill 或 design_rules 工具），改动前先告诉我要改什么。')
    after = ('配好后：如果你现在就能调用 emcosim 的工具，调用 current_project，告诉我连上了、页面里现在打开的是哪个工程；'
             '如果要重启会话才生效，告诉我重启后说“检查 SimWorld”。\n\n')
    prompt_claude = (
        '请帮我把本机的 SimWorld 电子+物理仿真平台接到你这里（MCP 服务，stdio，服务名 emcosim）。\n\n'
        '在终端运行下面这条命令（已经装过 emcosim 的话先运行 claude mcp remove -s user emcosim）：\n'
        f'{claude}\n\n'
        '再运行下面这条命令，装上 SimWorld 的使用规则（skill：三个页面各管什么、产品零件和测试夹具怎么分）：\n'
        f'{skill("claude")}\n\n' + after + about)
    prompt_codex = (
        '请帮我把本机的 SimWorld 电子+物理仿真平台接到你这里（MCP 服务，stdio，服务名 emcosim）。\n\n'
        f'打开 {codex_cfg}（没有就新建），加入下面这段；已有 [mcp_servers.emcosim] 就整段替换：\n'
        f'{codex}\n\n'
        '再运行下面这条命令，装上 SimWorld 的使用规则（skill：三个页面各管什么、产品零件和测试夹具怎么分）；'
        '如果你不读 ~/.codex/skills，就在开始设计前调用 emcosim 的 design_rules 工具读同样的规则：\n'
        f'{skill("codex")}\n\n' + after + about)
    return {'claude_code': claude, 'codex': codex, 'skill_claude_code': skill('claude'), 'skill_codex': skill('codex'),
            'project': str(root) if root else None, 'codex_config': codex_cfg,
            'prompt_claude_code': prompt_claude, 'prompt_codex': prompt_codex}


def serve(project_root=None, port=8765, host='127.0.0.1', open_browser=True):
    import webbrowser
    app = App(project_root)
    httpd = make_server(app, host, port)
    url = f'http://{host}:{httpd.server_address[1]}/#token={app.token}'
    print(f'SimWorld: {url}', flush=True)
    from . import link
    try:
        link.write(host if host not in ('0.0.0.0', '::') else '127.0.0.1', httpd.server_address[1], app.token)
    except OSError:
        pass
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        link.remove()
