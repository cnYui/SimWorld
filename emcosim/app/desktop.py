"""Desktop shell: the app's local server in a native window.

On Windows the window is Microsoft Edge WebView2 (preinstalled on Windows 10/11;
the installer bootstraps it otherwise). pywebview provides the window and the
native file dialogs; everything else is the same page as `emcosim serve`."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

FILE_TYPES = {
    'pcb': ('KiCad PCB (*.kicad_pcb)', 'EasyEDA Pro (*.epcb)', 'All files (*.*)'),
    'dsn': ('Specctra DSN (*.dsn)', 'All files (*.*)'),
    'model': ('3D model (*.stl;*.step;*.stp;*.obj;*.xml)', 'All files (*.*)'),
    'firmware': ('ESP32 firmware (*.bin;flasher_args.json)', 'All files (*.*)'),
    'measurements': ('Measurements (*.yaml;*.yml;*.csv)', 'All files (*.*)'),
    'any': ('All files (*.*)',),
}


class JsApi:
    """Exposed to the page as window.pywebview.api."""

    def __init__(self):
        self.window = None

    def pick_file(self, kind='any'):
        import webview
        r = self.window.create_file_dialog(webview.FileDialog.OPEN, file_types=FILE_TYPES.get(kind, FILE_TYPES['any']))
        return r[0] if r else None

    def pick_folder(self):
        import webview
        r = self.window.create_file_dialog(webview.FileDialog.FOLDER)
        return r[0] if r else None

    def open_external(self, url):
        import webbrowser
        if str(url).startswith(('https://', 'http://')):
            webbrowser.open(url)
        return True

    def reveal(self, path):
        p = Path(path)
        if not p.exists():
            return False
        if os.name == 'nt':
            os.startfile(str(p if p.is_dir() else p.parent))  # noqa: S606 - user's own folder
        return True


def main(project_root=None):
    try:
        import webview
    except ImportError:
        print('pywebview is not installed; falling back to the browser (emcosim serve)', file=sys.stderr)
        from .server import serve
        return serve(project_root)
    from .. import __version__
    from .server import App, make_server

    app = App(project_root)
    httpd = make_server(app, '127.0.0.1', 0)
    threading.Thread(target=httpd.serve_forever, daemon=True, name='emcosim-http').start()
    url = f'http://127.0.0.1:{httpd.server_address[1]}/#token={app.token}'
    js = JsApi()
    win = webview.create_window(f'emcosim {__version__}', url, js_api=js, width=1440, height=900,
                                min_size=(1100, 680), background_color='#1b1f24', text_select=True)
    js.window = win
    icon = Path(__file__).parent / 'static' / 'icon.png'
    try:
        webview.start(private_mode=False, storage_path=str(_storage_dir()),
                      icon=str(icon) if icon.exists() else None)
    finally:
        httpd.shutdown()
    return 0


def _storage_dir():
    from .settings import config_dir
    d = config_dir() / 'webview'
    d.mkdir(parents=True, exist_ok=True)
    return d


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
