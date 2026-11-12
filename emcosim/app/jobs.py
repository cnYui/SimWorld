"""Background queue for scenario runs and sweeps. One worker: each run already
uses a QEMU process plus the solver, and runs must not share project state."""
from __future__ import annotations

import itertools
import queue
import threading
import time
import traceback

from .. import api
from ..engine import RunCancelled

LIVE_PREFIXES = ('mcu.powered', 'mcu.rail_v', 'node.', 'act.', 'joint.', 'probe.', 'pin.')


def _live(sig):
    return {k: round(float(v), 5) for k, v in sig.items()
            if k.startswith(LIVE_PREFIXES) and not k.endswith('.dps')}


class Job:
    _ids = itertools.count(1)

    def __init__(self, kind, args):
        self.id = f'job{next(self._ids)}'
        self.kind, self.args = kind, args
        self.status = 'queued'
        self.progress = 0.0
        self.t = 0.0
        self.live = {}
        self.result = None
        self.error = None
        self.rows = []
        self.created = time.time()
        self.started = self.finished = None
        self.cancel = threading.Event()

    def public(self, live=False):
        d = {'id': self.id, 'kind': self.kind, 'args': self.args, 'status': self.status,
             'progress': round(self.progress, 4), 't': round(self.t, 4), 'error': self.error,
             'result': self.result, 'rows': self.rows, 'created': self.created,
             'started': self.started, 'finished': self.finished}
        if live:
            d['live'] = self.live
        return d


class JobManager:
    def __init__(self, bus, get_project):
        self.bus, self.get_project = bus, get_project
        self.jobs: dict[str, Job] = {}
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._worker, daemon=True, name='emcosim-jobs').start()

    def submit(self, kind, **args):
        if kind not in ('run', 'sweep'):
            raise ValueError(kind)
        job = Job(kind, args)
        self.jobs[job.id] = job
        self.q.put(job)
        self.bus.publish('job', job.public())
        return job.public()

    def cancel(self, job_id):
        job = self.jobs[job_id]
        job.cancel.set()
        if job.status == 'queued':
            job.status = 'cancelled'
            self.bus.publish('job', job.public())
        return job.public()

    def list(self):
        return [j.public() for j in sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)][:50]

    def _worker(self):
        while True:
            job = self.q.get()
            if job.status == 'cancelled':
                continue
            job.status, job.started = 'running', time.time()
            self.bus.publish('job', job.public())
            last = [0.0]

            def progress(frac, t, sig):
                job.progress, job.t = frac, t
                now = time.monotonic()
                if now - last[0] >= 0.1 or frac >= 1:
                    last[0] = now
                    job.live = _live(sig)
                    self.bus.publish('job_progress', {'id': job.id, 'progress': round(frac, 4),
                                                      't': round(t, 4), 'live': job.live})

            try:
                project = self.get_project()
                a = job.args
                if job.kind == 'run':
                    job.result = api.run_scenario(project, a['scenario'], a.get('overrides') or None,
                                                  progress=progress, cancel=job.cancel)
                else:
                    def on_row(row):
                        job.rows.append(row)
                        self.bus.publish('job', job.public())
                    job.result = api.sweep(project, a['scenario'], a['params'], metrics=a.get('metrics'),
                                           extra_overrides=a.get('overrides'), progress=progress,
                                           cancel=job.cancel, on_row=on_row)
                job.status, job.progress = 'done', 1.0
            except RunCancelled:
                job.status = 'cancelled'
            except Exception as e:      # reported to the UI, the worker keeps going
                job.status = 'failed'
                job.error = f'{type(e).__name__}: {e}'
                job.trace = traceback.format_exc()
            job.finished = time.time()
            self.bus.publish('job', job.public())
