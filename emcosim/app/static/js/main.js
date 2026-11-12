// App shell: auth, navigation rail, router, status bar, SSE dispatch.
import { initToken, connectEvents } from './api.js';
import { store } from './store.js';
import { h, icon, clear, toast, toastError } from './ui.js';
import { createStart } from './screens/start.js';
import { createPanel } from './screens/panel.js';
import { createOverview } from './screens/overview.js';
import { createParams } from './screens/params.js';
import { createScenarios } from './screens/scenarios.js';
import { createResults } from './screens/results.js';
import { createSweep } from './screens/sweep.js';
import { createSettings } from './screens/settings.js';
import { createPrint } from './screens/print.js';
import { createChat } from './chat.js';

const SCREENS = [
  { id: 'panel', title: '仿真面板', icon: 'results', make: createPanel, simple: true },
  { id: 'start', title: '开始', icon: 'home', make: createStart },
  { id: 'overview', title: '工程总览', icon: 'overview', make: createOverview, needsProject: true },
  { id: 'params', title: '参数', icon: 'sliders', make: createParams, needsProject: true },
  { id: 'scenarios', title: '场景', icon: 'scenario', make: createScenarios, needsProject: true },
  { id: 'results', title: '结果', icon: 'results', make: createResults, needsProject: true },
  { id: 'sweep', title: '扫参', icon: 'sweep', make: createSweep, needsProject: true },
  { id: 'print', title: '打印', icon: 'printer', make: createPrint, needsProject: true },
  { id: 'settings', title: '设置', icon: 'settings', make: createSettings, bottom: true },
];

const main = document.getElementById('main');
const rail = document.getElementById('rail');
const statusbar = document.getElementById('statusbar');
const instances = {};
let current = null;

function fatal(title, text) {
  const f = document.getElementById('fatal');
  f.hidden = false;
  clear(f).append(h('div', { class: 'box' }, h('h1', {}, title), h('p', { class: 'muted' }, text)));
}

// ------------------------------------------------------------------ router
export function nav(id, arg) {
  const hash = '#/' + id + (arg ? '/' + encodeURIComponent(arg) : '');
  if (location.hash !== hash) history.pushState(null, '', hash);
  show(id, arg);
}

function show(id, arg) {
  let def = SCREENS.find((s) => s.id === id) || SCREENS[0];
  if (def.needsProject && !store.app?.project) def = SCREENS[0];
  if (current && current !== def.id) instances[current]?.hide?.();
  let inst = instances[def.id];
  if (!inst) {
    inst = instances[def.id] = def.make(ctx);
    inst.el.hidden = true;
    main.append(inst.el);
  }
  for (const [k, v] of Object.entries(instances)) v.el.hidden = k !== def.id;
  current = def.id;
  inst.show?.(arg);
  for (const b of rail.querySelectorAll('button[data-id]')) {
    b.classList.toggle('active', b.dataset.id === def.id);
    if (b.dataset.id === def.id) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  }
  document.title = `${def.title} · emcosim`;
}

function fromHash() {
  const m = location.hash.match(/^#\/([\w-]+)(?:\/(.+))?/);
  if (m && (advanced || m[1] === 'panel')) show(m[1], m[2] ? decodeURIComponent(m[2]) : undefined);
  else show(advanced ? (store.app?.project ? 'overview' : 'start') : 'panel');
}

// Simple mode (default): only the panel. Advanced mode shows the navigation rail
// with parameters, sweeps, printing and settings.
let advanced = false;
try { advanced = localStorage.getItem('emcosim.advanced') === '1'; } catch (e) { /* ignore */ }
function setAdvanced(on) {
  advanced = !!on;
  try { localStorage.setItem('emcosim.advanced', advanced ? '1' : '0'); } catch (e) { /* ignore */ }
  document.getElementById('app').classList.toggle('simple', !advanced);
  renderStatus();
  nav(advanced ? (store.app?.project ? 'overview' : 'start') : 'panel');
}
window.addEventListener('popstate', fromHash);

const ctx = {
  nav,
  openRun: (runId) => nav('results', runId),
  compareRuns: (a, b) => nav('results', `${a}..${b}`),
  toggleChat: (open) => chat.toggle(open),
  askChat: (text) => { chat.toggle(true); chat.prefill(text); },
  currentScreen: () => current,
  setAdvanced: (on) => setAdvanced(on),
};

// ------------------------------------------------------------------ rail
function buildRail() {
  rail.append(h('div', { class: 'logo', title: 'emcosim' }, icon('logo')));
  rail.append(h('button', { class: 'rail-simple', 'aria-label': '回到简单面板', title: '回到简单面板', onclick: () => setAdvanced(false) }, '简'));
  const tip = h('div', { class: 'rail-tip', hidden: true });
  document.body.append(tip);
  const mk = (s) => {
    const b = h('button', {
      'data-id': s.id, 'aria-label': s.title, onclick: () => nav(s.id),
      onmouseenter: () => { const r = b.getBoundingClientRect(); tip.textContent = s.title; tip.hidden = false; tip.style.left = r.right + 8 + 'px'; tip.style.top = r.top + 10 + 'px'; },
      onmouseleave: () => { tip.hidden = true; },
    }, icon(s.icon));
    return b;
  };
  SCREENS.filter((s) => !s.bottom && !s.simple).forEach((s) => rail.append(mk(s)));
  rail.append(h('div', { class: 'spacer' }));
  const chatBtn = h('button', {
    'aria-label': 'AI 助手（Ctrl+J）', id: 'railChat', onclick: () => chat.toggle(),
    onmouseenter: () => { const r = chatBtn.getBoundingClientRect(); tip.textContent = 'AI 助手  Ctrl+J'; tip.hidden = false; tip.style.left = r.right + 8 + 'px'; tip.style.top = r.top + 10 + 'px'; },
    onmouseleave: () => { tip.hidden = true; },
  }, icon('chat'));
  rail.append(chatBtn);
  SCREENS.filter((s) => s.bottom).forEach((s) => rail.append(mk(s)));
}

function updateRailBadges() {
  const b = rail.querySelector('button[data-id=results]');
  if (!b) return;
  b.querySelector('.dot')?.remove();
  if (store.newRuns.size) b.append(h('span', { class: 'dot' }));
  const s = rail.querySelector('button[data-id=scenarios]');
  s.querySelector('.dot')?.remove();
  if (store.runningJob()) s.append(h('span', { class: 'dot' }));
}

// ------------------------------------------------------------------ status bar
let sseState = 'connecting';
function renderStatus() {
  const app = store.app || {};
  const q = app.qemu || {};
  const cs = store.chatStatus;
  const job = store.runningJob();
  const queued = [...store.jobs.values()].filter((j) => j.status === 'queued').length;
  let chatTxt = '检测中…', chatLed = '';
  if (cs && !cs.error) {
    if (cs.backend === 'api_key') { chatTxt = cs.api_key.configured ? `API Key · ${cs.api_key.model}` : 'API Key 未设置'; chatLed = cs.api_key.configured ? 'ok' : 'warn'; }
    else if (!cs.claude_code.found) { chatTxt = 'Claude Code 未安装'; chatLed = 'warn'; }
    else if (!cs.claude_code.logged_in) { chatTxt = 'Claude Code 未登录'; chatLed = 'warn'; }
    else { chatTxt = 'Claude Code 已登录'; chatLed = 'ok'; }
  }
  clear(statusbar).append(
    h('span', { class: 'seg grow', title: app.project || '' }, icon('folder', 'sm'),
      h('span', { class: 'path mono' }, app.project || '未打开工程')),
    job ? h('button', { class: 'seg', onclick: () => nav('scenarios'), title: '查看任务队列' },
      h('span', { class: 'led busy' }), `${job.kind === 'sweep' ? '扫参' : '运行'} ${job.args.scenario}`,
      h('span', { class: 'mini-progress' }, h('i', { style: { width: `${Math.round(job.progress * 100)}%` } })),
      `${Math.round(job.progress * 100)}%`, queued ? ` · 排队 ${queued}` : '') : null,
    !advanced ? null : h('button', { class: 'seg', onclick: () => nav('start'), title: q.found ? q.path : (q.error || '') },
      h('span', { class: 'led ' + (q.found ? 'ok' : 'warn') }), q.found ? 'QEMU 就绪' : 'QEMU 未找到'),
    advanced ? h('button', { class: 'seg', onclick: () => nav('settings') }, h('span', { class: 'led ' + chatLed }), chatTxt) : null,
    h('span', { class: 'seg', title: '与后台的实时连接' },
      h('span', { class: 'led ' + (sseState === 'open' ? 'ok' : sseState === 'closed' ? 'bad' : 'warn') }),
      sseState === 'open' ? '已连接' : sseState === 'closed' ? '已断开' : '重连中'),
    h('span', { class: 'seg dim' }, `v${app.version || ''}`),
  );
}

// ------------------------------------------------------------------ events
const toastedRuns = new Set();
function onEvent(kind, data) {
  switch (kind) {
    case 'job': {
      const prev = store.jobs.get(data.id);
      const keepLive = prev?.live;
      store.jobs.set(data.id, { ...data, live: keepLive });
      store.emit('job', data);
      if (prev?.status !== data.status) jobStatusToast(data);
      break;
    }
    case 'job_progress': {
      const j = store.jobs.get(data.id);
      if (j) { j.progress = data.progress; j.t = data.t; j.live = data.live; }
      let buf = store.live.get(data.id);
      if (!buf) store.live.set(data.id, buf = { t: [], sig: {} });
      const last = buf.t[buf.t.length - 1];
      if (last !== undefined && data.t < last) { buf.t = []; buf.sig = {}; }   // next sweep point restarts time
      buf.t.push(data.t);
      for (const [k, v] of Object.entries(data.live || {})) (buf.sig[k] ||= []).push(v);
      for (const k of Object.keys(buf.sig)) if (buf.sig[k].length < buf.t.length) buf.sig[k].push(buf.sig[k].at(-1));
      store.emit('job_progress', data);
      break;
    }
    case 'chat': chat.onEvent(data); break;
    case 'project_changed': store.loadProject().catch(toastError); break;
    case 'scenarios_changed': store.loadProject().then(() => store.emit('scenarios')).catch(toastError); break;
    case 'project_opened':
      store.seenRuns.clear(); store.newRuns.clear();
      store.loadApp().then(() => store.loadProject()).then(() => store.loadRuns()).catch(toastError);
      break;
    case 'runs_changed':
      store.loadRuns().then(() => {
        const fresh = (data?.new || []).filter((id) => !toastedRuns.has(id));
        if (fresh.length) {
          const id = fresh.at(-1);
          toastedRuns.add(id);
          if (!id.includes('-sweep')) {
            toast(`有新的运行结果：${id}`, { action: { label: '打开最新结果', fn: () => ctx.openRun(id) }, timeout: 9000 });
          }
        }
      }).catch(toastError);
      break;
    case 'auth_changed': store.loadChatStatus(); break;
    case 'auth_log': store.emit('auth_log', data?.line ?? ''); break;
  }
  renderStatus();
  updateRailBadges();
}

function jobStatusToast(j) {
  const name = `${j.kind === 'sweep' ? '扫参' : '运行'} ${j.args.scenario}`;
  if (j.status === 'done' && j.kind === 'run' && j.result?.run_id) {
    toastedRuns.add(j.result.run_id);
    const ok = j.result.passed;
    toast(`${name} 完成：${ok === true ? '全部断言通过' : ok === false ? '有断言未通过' : '已完成'}`,
      { action: { label: '打开最新结果', fn: () => ctx.openRun(j.result.run_id) }, timeout: 10000 });
  } else if (j.status === 'done' && j.kind === 'sweep') {
    toast(`${name} 完成，共 ${j.rows.length} 组`, { action: { label: '查看', fn: () => nav('sweep', j.id) }, timeout: 10000 });
  } else if (j.status === 'failed') {
    toast(`${name} 失败：${j.error}`, { kind: 'bad', timeout: 10000 });
  } else if (j.status === 'cancelled') {
    toast(`${name} 已取消`);
  }
}

// ------------------------------------------------------------------ chat panel + splitter
let chat;
function setupSplitter() {
  const sp = document.getElementById('chatSplitter');
  const root = document.documentElement;
  try { const w = localStorage.getItem('emcosim.chatW'); if (w) root.style.setProperty('--chat-w', w + 'px'); } catch (e) { /* ignore */ }
  const setW = (w) => {
    w = Math.max(280, Math.min(w, Math.round(innerWidth * 0.6)));
    root.style.setProperty('--chat-w', w + 'px');
    try { localStorage.setItem('emcosim.chatW', w); } catch (e) { /* ignore */ }
  };
  sp.addEventListener('mousedown', (e) => {
    e.preventDefault();
    sp.classList.add('drag');
    const move = (ev) => setW(innerWidth - ev.clientX - 2);
    const up = () => { sp.classList.remove('drag'); removeEventListener('mousemove', move); removeEventListener('mouseup', up); };
    addEventListener('mousemove', move); addEventListener('mouseup', up);
  });
  sp.addEventListener('keydown', (e) => {
    const cur = document.getElementById('chat').offsetWidth;
    if (e.key === 'ArrowLeft') setW(cur + 20);
    if (e.key === 'ArrowRight') setW(cur - 20);
  });
}

document.addEventListener('keydown', (e) => {
  if (advanced && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'j') { e.preventDefault(); chat.toggle(); }
});

// ------------------------------------------------------------------ boot
async function boot() {
  const token = initToken();
  if (!token) {
    fatal('缺少访问令牌', '请从 emcosim 桌面程序打开本界面，或使用 `emcosim serve` 在终端里打印的完整链接（链接末尾带 #token=…）。为了防止其他网页操控本程序，没有令牌时界面不会连接后台。');
    return;
  }
  try {
    await store.loadApp();
  } catch (e) {
    if (e.status === 401) {
      fatal('访问令牌无效', '这个链接里的令牌和当前运行的 emcosim 不匹配（程序可能已经重启）。请重新从 emcosim 打开界面，或复制终端里最新打印的链接。');
      try { sessionStorage.removeItem('emcosim.token'); } catch (err) { /* ignore */ }
    } else fatal('无法连接 emcosim 后台', e.message);
    return;
  }
  document.getElementById('app').classList.toggle('simple', !advanced);
  buildRail();
  setupSplitter();
  chat = createChat(document.getElementById('chat'), document.getElementById('chatWrap'), ctx);
  store.on('chatStatus', renderStatus);
  store.on('app', renderStatus);
  store.on('runs', updateRailBadges);
  renderStatus();
  await Promise.all([
    store.loadProject().catch(toastError),
    store.loadJobs().catch(() => {}),
    store.loadChatStatus(),
  ]);
  await store.loadRuns().catch(() => {});
  fromHash();
  let opened = false;
  connectEvents(onEvent, (st) => {
    const was = sseState;
    sseState = st;
    renderStatus();
    if (st === 'open' && opened && was !== 'open') {
      // we may have missed events while disconnected
      store.loadApp().then(() => store.loadProject()).then(() => store.loadRuns()).catch(() => {});
      store.loadJobs().catch(() => {});
    }
    if (st === 'open') opened = true;
  });
  window.__emcosim = { store, onEvent, nav, chat };   // for automated UI tests
}

boot();
