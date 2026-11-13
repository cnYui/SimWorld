"""In-process publish/subscribe used to push updates to the UI over SSE."""
from __future__ import annotations

import queue
import threading
import time


class EventBus:
    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=2000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, kind: str, data=None):
        msg = {'kind': kind, 'time': time.time(), 'data': data}
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:          # a stalled client must not block the app
                pass


class ProjectWatcher(threading.Thread):
    """Notices edits made outside the app's own API (the AI agent's MCP server
    runs in a separate process): project.yaml, scenarios/ and runs/."""

    def __init__(self, bus: EventBus, get_root, interval=0.7):
        super().__init__(daemon=True, name='emcosim-watcher')
        self.bus, self.get_root, self.interval = bus, get_root, interval
        self._stop = threading.Event()
        self._last = None

    def snapshot(self, root):
        def mtime(p):
            try:
                return p.stat().st_mtime_ns
            except OSError:
                return 0
        sc = root / 'scenarios'
        runs = root / 'runs'
        # the files an agent edits through MCP besides project.yaml: the MuJoCo model and the board
        extra = tuple(mtime(f) for f in sorted(root.glob('mechanics/*.xml')) + sorted(root.glob('pcb/*/*.kicad_pcb'))
                      + sorted(root.glob('pcb/*/*.kicad_sch')))
        return (str(root), mtime(root / 'project.yaml'),
                tuple(sorted((p.name, mtime(p)) for p in sc.glob('*.yaml'))) if sc.exists() else (),
                tuple(sorted(p.name for p in runs.iterdir() if (p / 'run.json').exists())) if runs.exists() else (),
                extra,
                tuple(sorted((f.parent.name, mtime(f)) for f in (runs / '_checks').glob('*/result.json'))))

    def run(self):
        while not self._stop.wait(self.interval):
            root = self.get_root()
            if root is None:
                continue
            try:
                snap = self.snapshot(root)
            except OSError:
                continue
            last, self._last = self._last, snap
            if last is None or last[0] != snap[0]:
                continue
            if last[1] != snap[1]:
                self.bus.publish('project_changed')
            if last[2] != snap[2]:
                self.bus.publish('scenarios_changed')
            if last[3] != snap[3]:
                new = sorted(set(snap[3]) - set(last[3]))
                self.bus.publish('runs_changed', {'new': new})
            if last[4] != snap[4]:
                self.bus.publish('files_changed')
            if last[5] != snap[5]:
                self.bus.publish('checks_changed')

    def stop(self):
        self._stop.set()
