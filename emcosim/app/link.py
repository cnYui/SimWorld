"""Link between the browser app and MCP servers started by an AI agent.

The app server writes <config dir>/live.json (port and token) while it runs. An MCP
server started by Claude Code or Codex reads it, so the agent works on the project
that is open in the page and can open, create or switch projects in that page.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .settings import config_dir


def _file():
    return config_dir() / 'live.json'


def write(host, port, token):
    data = {'url': f'http://{host}:{port}', 'token': token, 'pid': os.getpid()}
    _file().write_text(json.dumps(data), encoding='utf-8')
    return data


def remove():
    try:
        d = json.loads(_file().read_text(encoding='utf-8'))
        if d.get('pid') == os.getpid():
            _file().unlink()
    except (OSError, ValueError):
        pass


def read():
    try:
        return json.loads(_file().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


class AppLink:
    """Client for the running app's HTTP API. None-safe: call() raises AppUnavailable."""

    class Unavailable(RuntimeError):
        pass

    def __init__(self, info=None):
        self.info = info if info is not None else read()

    @property
    def page_url(self):
        return f"{self.info['url']}/#token={self.info['token']}" if self.info else None

    def call(self, method, path, body=None, timeout=10):
        if not self.info:
            raise self.Unavailable('SimWorld page is not running')
        req = urllib.request.Request(f"{self.info['url']}/api/{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={'X-Emcosim-Token': self.info['token'], 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b'null')
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get('error')
            except Exception:
                msg = None
            raise RuntimeError(msg or f'app answered {e.code}') from None
        except (urllib.error.URLError, OSError) as e:
            raise self.Unavailable(f'SimWorld page is not reachable: {e}') from None
