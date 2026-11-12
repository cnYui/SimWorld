"""Local web viewer: `emcosim view --project DIR` then open http://127.0.0.1:8765.
Serves the static page and the files of runs/<id>/ (read-only, localhost only)."""
from __future__ import annotations

import json
import re
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..results import RunStore

STATIC = Path(__file__).parent / 'static'
RUN_FILES = {'scene.json': 'application/json', 'scene.bin': 'application/octet-stream',
             'frames.bin': 'application/octet-stream', 'uart.log': 'text/plain; charset=utf-8'}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, store=None, **kw):
        self.store = store
        super().__init__(*args, directory=str(STATIC), **kw)

    def log_message(self, *a):
        pass

    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, default=str).encode(), 'application/json', code)

    def do_GET(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split('/') if p]
        try:
            if parts[:2] == ['api', 'runs'] and len(parts) == 2:
                return self._json(self.store.list())
            if len(parts) >= 3 and parts[:2] == ['api', 'run']:
                run_id = parts[2]
                if not re.fullmatch(r'[\w.\-]+', run_id):
                    return self._json({'error': 'bad run id'}, 400)
                d = self.store.dir(run_id)
                if len(parts) == 3:
                    meta = self.store.meta(run_id)
                    meta.pop('resolved_project', None)
                    meta['signals'] = self.store.signal_names(run_id)
                    return self._json(meta)
                if parts[3] == 'signals':
                    q = parse_qs(url.query)
                    names = q.get('names', [''])[0].split(',') if q.get('names') else None
                    return self._json(self.store.signals(run_id, names, max_points=int(q.get('n', ['4000'])[0])))
                if parts[3] in RUN_FILES and (d / parts[3]).exists():
                    return self._send((d / parts[3]).read_bytes(), RUN_FILES[parts[3]])
                return self._json({'error': 'not found'}, 404)
        except (FileNotFoundError, KeyError) as e:
            return self._json({'error': str(e)}, 404)
        return super().do_GET()


def serve(project_root, port=8765, host='127.0.0.1'):
    store = RunStore(project_root)
    httpd = ThreadingHTTPServer((host, port), partial(Handler, store=store))
    print(f'emcosim viewer: http://{host}:{port}  (project {project_root})', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
