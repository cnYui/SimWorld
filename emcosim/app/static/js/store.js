// Shared app state + a tiny event emitter. Screens subscribe to the parts they show.
import { get } from './api.js';

const listeners = {};

export const store = {
  app: null,           // GET /api/app
  project: null,       // GET /api/project (overview + data + scenario texts)
  baseline: null,      // project data when the project was opened in this session
  runs: [],
  jobs: new Map(),     // id -> job (public dict)
  live: new Map(),     // job id -> {t:[], sig:{name:[]}} live samples from job_progress
  chatStatus: null,
  pcb: undefined,
  seenRuns: new Set(),
  newRuns: new Set(),

  on(ev, fn) { (listeners[ev] ||= new Set()).add(fn); return () => listeners[ev].delete(fn); },
  emit(ev, ...a) { for (const fn of listeners[ev] || []) { try { fn(...a); } catch (e) { console.error(e); } } },

  async loadApp() {
    this.app = await get('app');
    this.emit('app', this.app);
    return this.app;
  },

  async loadProject() {
    if (!this.app?.project) { this.project = null; this.emit('project', null); return null; }
    try {
      const p = await get('project');
      const rootChanged = !this.project || this.project.root !== p.root;
      this.project = p;
      if (rootChanged || !this.baseline) {
        this.baseline = structuredClone(p.data);
        this.pcb = undefined;
      }
      this.emit('project', p);
    } catch (e) {
      if (e.status === 409) { this.project = null; this.emit('project', null); } else throw e;
    }
    return this.project;
  },

  async loadPcb(force = false) {
    if (this.pcb !== undefined && !force) return this.pcb;
    try { this.pcb = await get('pcb'); } catch (e) { this.pcb = null; }
    return this.pcb;
  },

  async loadRuns() {
    if (!this.project) { this.runs = []; this.emit('runs', []); return []; }
    const runs = await get('runs');
    if (!this.seenRuns.size) runs.forEach((r) => this.seenRuns.add(r.run_id));
    else for (const r of runs) if (!this.seenRuns.has(r.run_id)) { this.seenRuns.add(r.run_id); this.newRuns.add(r.run_id); }
    this.runs = runs;
    this.emit('runs', runs);
    return runs;
  },

  async loadJobs() {
    const jobs = await get('jobs');
    this.jobs = new Map(jobs.map((j) => [j.id, j]));
    this.emit('jobs');
  },

  async loadChatStatus() {
    try { this.chatStatus = await get('chat/status'); } catch (e) { this.chatStatus = { error: e.message }; }
    this.emit('chatStatus', this.chatStatus);
    return this.chatStatus;
  },

  runningJob() {
    return [...this.jobs.values()].find((j) => j.status === 'running') || null;
  },
};

// every editable leaf path in the project data, with value + optional note
export function paramPaths(data) {
  const out = [];
  const walk = (obj, path) => {
    for (const [k, v] of Object.entries(obj || {})) {
      if (k === 'note') continue;
      const p = path ? `${path}.${k}` : k;
      if (v && typeof v === 'object' && !Array.isArray(v)) walk(v, p);
      else out.push({ path: p, value: v, note: obj.note });
    }
  };
  walk(data, '');
  return out;
}

export function getPath(obj, path) {
  return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
}
