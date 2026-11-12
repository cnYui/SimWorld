"""Per-user settings (recent projects, chat backend, optional API key).

Stored in %APPDATA%\\emcosim\\settings.json on Windows and ~/.config/emcosim
elsewhere. An Anthropic API key, if the user enters one, is encrypted with
Windows DPAPI (bound to the Windows account) on Windows and stored in a file
readable only by the user elsewhere."""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
from pathlib import Path

DEFAULTS = {
    'recent_projects': [],
    'chat_backend': 'claude_code',       # 'claude_code' | 'api_key'
    'claude_path': '',                   # override for the claude executable
    'model': 'claude-opus-5-5',          # API-key mode only
    'effort': 'medium',
}


def config_dir() -> Path:
    if os.name == 'nt':
        base = Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming')
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    d = base / 'emcosim'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inp = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if encrypt else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError('DPAPI call failed')
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else config_dir() / 'settings.json'
        self._lock = threading.Lock()
        self.data = dict(DEFAULTS)
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding='utf8')))
            except (OSError, ValueError):
                pass

    def save(self):
        with self._lock:
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding='utf8')
            if os.name != 'nt':
                os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)

    def public(self):
        out = {k: v for k, v in self.data.items() if k != 'api_key_enc'}
        out['has_api_key'] = bool(self.data.get('api_key_enc'))
        return out

    def update(self, **kw):
        for k, v in kw.items():
            if k in DEFAULTS:
                self.data[k] = v
        self.save()

    # -- API key -------------------------------------------------------------
    def set_api_key(self, key: str | None):
        if not key:
            self.data.pop('api_key_enc', None)
        else:
            raw = key.strip().encode()
            if sys.platform == 'win32':
                self.data['api_key_enc'] = 'dpapi:' + base64.b64encode(_dpapi(raw, True)).decode()
            else:
                self.data['api_key_enc'] = 'plain:' + base64.b64encode(raw).decode()
        self.save()

    def api_key(self) -> str | None:
        enc = self.data.get('api_key_enc')
        if not enc:
            return os.environ.get('ANTHROPIC_API_KEY')
        kind, _, b64 = enc.partition(':')
        raw = base64.b64decode(b64)
        if kind == 'dpapi':
            raw = _dpapi(raw, False)
        return raw.decode()

    # -- recent projects -----------------------------------------------------
    def touch_project(self, root):
        root = str(Path(root).resolve())
        rec = [p for p in self.data.get('recent_projects', []) if p != root]
        self.data['recent_projects'] = [root] + rec[:9]
        self.save()
