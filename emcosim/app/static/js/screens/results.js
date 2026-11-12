// 结果：运行列表、单次运行详情（3D 回放 + 曲线 + 检查 + 指标 + 串口），两次运行对比。
import { api, get, post, del } from '../api.js';
import { store } from '../store.js';
import { h, icon, clear, fmtNum, fmtValue, unitOf, toast, toastError, confirmDialog, seriesColor, popover, esc, debounce } from '../ui.js';
import { TimeChart } from '../chart.js';

const DEFAULT_SIGNALS = [/^mcu\.rail_v$/, /^node\..*\.v$/, /^act\..*\.i_motor$/, /^joint\..*\.deg$/, /^probe\..*force_n$/, /^mcu\.powered$/, /^act\..*\.pulse_us$/];
const SIG_KEY = 'emcosim.signals';

function loadPrefSignals() { try { return JSON.parse(localStorage.getItem(SIG_KEY) || 'null'); } catch (e) { return null; } }
function savePrefSignals(list) { try { localStorage.setItem(SIG_KEY, JSON.stringify(list)); } catch (e) { /* ignore */ } }

function defaultSignals(all) {
  const pref = loadPrefSignals();
  if (pref) { const keep = pref.filter((s) => all.includes(s)); if (keep.length) return keep; }
  return all.filter((s) => DEFAULT_SIGNALS.some((re) => re.test(s))).slice(0, 8);
}

function flatMetrics(m) {
  const out = [];
  for (const [k, v] of Object.entries(m || {})) {
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      const ent = Object.entries(v);
      if (!ent.length) out.push([k, '—']);
      for (const [kk, vv] of ent) out.push([`${k}.${kk}`, vv]);
    } else out.push([k, Array.isArray(v) ? v.join(', ') : v]);
  }
  return out;
}

function checkText(c) {
  const ops = { eq: '=', ne: '≠', lt: '<', gt: '>', le: '≤', ge: '≥' };
  return `${c.check} ${ops[c.op] || c.op} ${fmtValue(c.target)}`;
}

export function createResults(ctx) {
  // ---------------------------------------------------------------- list pane
  const filterSel = h('select', { class: 'input sm grow', 'aria-label': '按场景筛选' });
  const cmpBtn = h('button', { class: 'btn sm', disabled: true, title: '勾选两次运行后对比' }, icon('compare', 'sm'), '对比');
  const listBody = h('div', { class: 'pane-body' });
  const newBanner = h('div');
  const right = h('div', { style: { minWidth: 0, minHeight: 0, display: 'flex', flexDirection: 'column' } });
  const el = h('section', { class: 'screen', 'aria-label': '结果' },
    h('div', { class: 'screen-head' }, h('h1', {}, '结果'), h('span', { class: 'sub' }, '每次运行保存在 runs/<id>/'), h('span', { class: 'grow' }),
      h('button', { class: 'btn icon ghost', title: '刷新', 'aria-label': '刷新', onclick: () => store.loadRuns().catch(toastError) }, icon('refresh'))),
    h('div', { class: 'res-wrap', style: { flex: 1, minHeight: 0 } },
      h('div', { class: 'pane' }, h('div', { class: 'pane-head' }, filterSel, cmpBtn), newBanner, listBody),
      right));

  const picked = [];     // run ids checked for comparison
  let mode = null;       // 'run' | 'compare'
  let runId = null, cmpIds = null;
  let newMarks = new Set();

  filterSel.addEventListener('change', renderList);
  cmpBtn.addEventListener('click', () => { if (picked.length === 2) ctx.compareRuns(picked[0], picked[1]); });

  function renderList() {
    const runs = store.runs || [];
    const scen = [...new Set(runs.map((r) => r.scenario))].sort();
    const fv = filterSel.value;
    clear(filterSel).append(h('option', { value: '' }, `全部场景（${runs.length}）`), scen.map((s) => h('option', { value: s, selected: s === fv }, s)));
    const shown = runs.filter((r) => !filterSel.value || r.scenario === filterSel.value);
    cmpBtn.disabled = picked.length !== 2;
    cmpBtn.textContent = '';
    cmpBtn.append(icon('compare', 'sm'), picked.length ? `对比 ${picked.length}/2` : '对比');
    clear(listBody).append(shown.length ? shown.map(runItem) : h('div', { class: 'empty' }, runs.length ? '没有匹配的运行' : '还没有运行结果：去“场景”运行一个'));
    const latest = runs[0];
    clear(newBanner);
    if (latest && newMarks.has(latest.run_id) && latest.run_id !== runId) {
      newBanner.append(h('div', { class: 'banner info', style: { margin: '6px', borderRadius: '6px' } }, icon('info', 'sm'), h('span', { class: 'grow' }, '有新的运行结果'),
        h('button', { class: 'btn sm primary', onclick: () => ctx.openRun(latest.run_id) }, '打开最新结果')));
    }
  }

  function runItem(r) {
    const cb = h('input', { type: 'checkbox', checked: picked.includes(r.run_id), 'aria-label': `选择 ${r.run_id} 用于对比`,
      onclick: (e) => e.stopPropagation(),
      onchange: () => {
        const i = picked.indexOf(r.run_id);
        if (cb.checked && i < 0) { picked.push(r.run_id); if (picked.length > 2) picked.shift(); }
        if (!cb.checked && i >= 0) picked.splice(i, 1);
        renderList();
      } });
    const ov = Object.entries(r.overrides || {});
    const sel = (mode === 'run' && r.run_id === runId) || (mode === 'compare' && cmpIds?.includes(r.run_id));
    const time = (r.created || '').slice(5, 16);
    const sweep = /-sweep$/.test(r.run_id) || /-sweep-/.test(r.run_id);
    return h('div', { class: 'run-item' + (sel ? ' sel' : '') + (newMarks.has(r.run_id) ? ' new' : ''), role: 'button', tabindex: '0', 'data-run': r.run_id,
      onclick: () => ctx.openRun(r.run_id), onkeydown: (e) => { if (e.key === 'Enter') ctx.openRun(r.run_id); } },
      cb,
      h('div', { style: { minWidth: 0 } },
        h('div', { class: 'name mono' }, r.scenario),
        h('div', { class: 'sub' }, r.passed === true ? h('span', { class: 'chip ok' }, icon('check'), '通过') : r.passed === false ? h('span', { class: 'chip bad' }, icon('x'), '未通过') : h('span', { class: 'chip' }, '无断言'),
          sweep ? h('span', { class: 'chip' }, '扫参') : null, h('span', { class: 'mono' }, time)),
        ov.length ? h('div', { class: 'ovs', title: ov.map(([k, v]) => `${k} = ${fmtValue(v)}`).join('\n') }, ov.map(([k, v]) => `${k.split('.').slice(-2).join('.')}=${fmtValue(v)}`).join('  ')) : null),
      h('button', { class: 'btn icon sm ghost del', title: '删除这次运行', 'aria-label': `删除 ${r.run_id}`, onclick: (e) => { e.stopPropagation(); removeRun(r.run_id); } }, icon('trash', 'sm')));
  }

  async function removeRun(id) {
    if (!(await confirmDialog('删除运行结果', `删除 runs/${id}？这个操作不能撤销。`, '删除', true))) return;
    try {
      await del(`run/${id}`);
      const i = picked.indexOf(id); if (i >= 0) picked.splice(i, 1);
      if (runId === id) { runId = null; mode = null; history.replaceState(null, '', '#/results'); renderEmpty(); }
      await store.loadRuns();
      toast('已删除');
    } catch (e) { toastError(e); }
  }

  function renderEmpty() {
    clear(right).append(h('div', { class: 'empty', style: { margin: 'auto' } },
      h('div', { style: { marginBottom: '8px' } }, '从左侧选择一次运行查看 3D 回放、曲线和串口日志；勾选两次运行可以对比。'),
      store.runs?.[0] ? h('button', { class: 'btn primary', onclick: () => ctx.openRun(store.runs[0].run_id) }, '打开最新结果') : null));
  }

  // ---------------------------------------------------------------- run detail
  const D = {};   // persistent detail elements
  let replay = null, playing = false, speed = 0.5, tNow = 0, dur = 0, charts = [], sigCache = {}, chosen = [], meta = null, uartLines = [];

  function buildDetail() {
    D.head = h('div', { class: 'rd-head' });
    D.view = h('div', { class: 'view3d' }, h('div', { class: 'overlay' }));
    D.tabs = h('div', { class: 'tabs', role: 'tablist' });
    D.tabpane = h('div', { class: 'tabpane' });
    D.play = h('button', { class: 'btn icon', 'aria-label': '播放', title: '播放/暂停（空格）', onclick: togglePlay }, icon('play'));
    D.speed = h('select', { class: 'input', 'aria-label': '播放速度' }, ['0.1', '0.25', '0.5', '1', '2'].map((v) => h('option', { value: v, selected: v === '0.5' }, `${v}×`)));
    D.speed.addEventListener('change', () => { speed = Number(D.speed.value); });
    D.slider = h('input', { type: 'range', min: 0, max: 1000, value: 0, 'aria-label': '时间' });
    D.slider.addEventListener('input', () => setTime(D.slider.value / 1000 * dur));
    D.time = h('span', { class: 'time' }, '0.000 s');
    D.sigBtn = h('button', { class: 'btn sm', onclick: () => openPicker(D.sigBtn) }, icon('wave', 'sm'), '选择信号');
    D.sigInfo = h('span', { class: 'hint' });
    D.charts = h('div', { class: 'charts' });
    D.root = h('div', { class: 'rd' }, D.head,
      h('div', { class: 'rd-body' },
        h('div', { class: 'rd-top' }, D.view, h('div', { class: 'rd-side' }, D.tabs, D.tabpane)),
        h('div', { class: 'player' }, D.play, D.speed, D.slider, D.time,
          h('button', { class: 'btn icon sm ghost', title: '重置视角', 'aria-label': '重置 3D 视角', onclick: () => replay?.resetView() }, icon('cube', 'sm'))),
        h('div', { class: 'sig-bar' }, D.sigBtn, D.sigInfo, h('span', { class: 'grow' }), h('span', { class: 'hint' }, '点击或拖动曲线可以跳到对应时刻')),
        D.charts));
  }

  async function openRun(id) {
    stopPlay();
    mode = 'run'; runId = id; cmpIds = null;
    newMarks.delete(id); store.newRuns.delete(id);
    renderList();
    if (!D.root) buildDetail();
    clear(right).append(D.root);
    clear(D.head).append(h('div', { class: 'hint' }, '加载中…'));
    let m;
    try { m = await get(`run/${id}`); } catch (e) { clear(D.head).append(h('div', { class: 'banner bad' }, icon('alert'), `打不开 ${id}：${e.message}`)); return; }
    if (runId !== id) return;
    meta = m;
    sigCache = {};
    dur = m.scenario?.duration_s || m.timing?.sim_s || 0;
    renderHead(m);
    renderTabs('checks');
    chosen = defaultSignals(m.signals || []);
    D.sigInfo.textContent = `${chosen.length} / ${(m.signals || []).length} 个信号`;
    setTime(0);
    // 3D
    if (!replay) {
      const { Replay3D } = await import('../replay3d.js');
      replay = new Replay3D(D.view);
    }
    const msg = D.view.querySelector('.msg'); if (msg) msg.remove();
    D.view.querySelector('.overlay').textContent = '';
    const loads = [loadSignals(chosen).then(() => renderCharts())];
    if (replay.ok) {
      loads.push(replay.load(id).then((ok) => { if (ok && runId === id) { dur = replay.duration || dur; setTime(tNow); } })
        .catch((e) => D.view.append(h('div', { class: 'msg' }, `3D 回放不可用：${e.message}`))));
    } else D.view.append(h('div', { class: 'msg' }, replay.error));
    loads.push(loadUart(id));
    await Promise.all(loads);
  }

  async function loadUart(id) {
    try {
      const txt = await api(`run/${id}/uart.log`, { as: 'text' });
      if (runId !== id) return;
      uartLines = txt.split('\n').filter((l) => l.length).map((l) => {
        const m = l.match(/^\[\s*([\d.]+)\s*s\]\s?(.*)$/);
        return m ? { t: Number(m[1]), text: m[2] } : { t: null, text: l };
      });
    } catch (e) { uartLines = []; }
    if (D.activeTab === 'uart') renderTabs('uart');
    else renderTabs(D.activeTab);
  }

  function renderHead(m) {
    const ov = Object.entries(m.overrides || {});
    const sc = m.scenario || {};
    const wall = m.timing?.wall_s;
    clear(D.head).append(
      h('div', { class: 'l1' },
        h('h2', { class: 'mono' }, sc.name || m.run_id),
        m.passed === true ? h('span', { class: 'chip ok' }, icon('check'), '全部通过') : m.passed === false ? h('span', { class: 'chip bad' }, icon('x'), '有断言未通过') : h('span', { class: 'chip' }, '无断言'),
        h('span', { class: 'chip' }, m.mcu_backend === 'qemu' ? '固件（QEMU）' : '脚本 MCU'),
        h('span', { class: 'id' }, m.run_id),
        h('span', { class: 'dim' }, `${m.created} · 仿真 ${fmtNum(m.timing?.sim_s)} s${wall ? ` · 耗时 ${fmtNum(wall, 3)} s` : ''}`),
        h('span', { class: 'grow' }),
        h('button', { class: 'btn sm', onclick: () => rerun(m) }, icon('refresh', 'sm'), '再跑一次'),
        h('button', { class: 'btn sm', onclick: () => ctx.askChat(`分析运行 ${m.run_id}（场景 ${sc.name}）：哪些断言没过、原因是什么、该改哪个参数？`) }, icon('chat', 'sm'), '让 AI 分析'),
        h('button', { class: 'btn icon sm ghost', title: '删除', 'aria-label': '删除这次运行', onclick: () => removeRun(m.run_id) }, icon('trash', 'sm'))),
      sc.description ? h('div', { class: 'muted' }, sc.description) : null,
      ov.length ? h('div', { class: 'row wrap' }, h('span', { class: 'dim' }, '覆盖参数'), ov.map(([k, v]) => h('span', { class: 'chip mono', title: k }, `${k} = ${fmtValue(v)}`))) : null);
  }

  async function rerun(m) {
    try {
      const job = await post('jobs/run', { scenario: m.scenario.name, overrides: m.overrides || {} });
      store.jobs.set(job.id, job);
      toast(`已加入队列：${m.scenario.name}`, { action: { label: '查看进度', fn: () => ctx.nav('scenarios') } });
    } catch (e) { toastError(e); }
  }

  function renderTabs(which = 'checks') {
    if (!meta) return;
    D.activeTab = which;
    const checks = meta.checks || [];
    const nFail = checks.filter((c) => !c.pass).length;
    const evts = (meta.mcu_events || []).length + (meta.log || []).length + (meta.brownout_detector || []).length;
    const tabs = [['checks', '检查', nFail ? `${nFail} 未过` : checks.length], ['metrics', '指标', null], ['uart', 'ESP32 串口', uartLines.length || null], ['events', '事件', evts]];
    clear(D.tabs).append(tabs.map(([k, label, n]) => h('button', { class: k === which ? 'on' : '', role: 'tab', 'aria-selected': String(k === which), onclick: () => renderTabs(k) },
      label, n !== null && n !== undefined ? h('span', { class: 'count' + (k === 'checks' && nFail ? ' bad-t' : '') }, n) : null)));
    clear(D.tabpane);
    if (which === 'checks') {
      D.tabpane.append(checks.length ? h('div', { class: 'checks' }, checks.map((c) => h('div', { class: 'ck' },
        h('span', { class: c.pass ? 'ok-t' : 'bad-t' }, icon(c.pass ? 'check' : 'x')),
        h('div', {}, h('div', { class: 'expr' }, checkText(c), '  →  ', h('b', {}, fmtValue(c.value))), c.note ? h('div', { class: 'nt' }, c.note) : null)))) : h('div', { class: 'empty' }, '这个场景没有 expect 断言'));
    } else if (which === 'metrics') {
      D.tabpane.append(h('table', { class: 'tbl' }, h('thead', {}, h('tr', {}, h('th', {}, '指标'), h('th', { class: 'num' }, '值'))),
        h('tbody', {}, flatMetrics(meta.metrics).map(([k, v]) => h('tr', {}, h('td', { class: 'mono' }, k), h('td', { class: 'num' }, typeof v === 'number' ? fmtNum(v) : String(v), ' ', h('span', { class: 'dim' }, typeof v === 'number' ? unitFromMetric(k) : '')))))));
    } else if (which === 'uart') {
      renderUart();
    } else if (which === 'events') {
      const rows = [
        ...(meta.mcu_events || []).map((e) => ({ t: e.t, src: '固件', ev: e.event, d: e.detail })),
        ...(meta.log || []).map((e) => ({ t: e.t, src: '场景', ev: e.event, d: typeof e.detail === 'object' ? JSON.stringify(e.detail) : e.detail })),
        ...(meta.brownout_detector || []).map((e) => ({ t: e.t, src: '欠压检测', ev: e.event || 'brownout', d: e.detail ? JSON.stringify(e.detail) : JSON.stringify(e) })),
      ].sort((a, b) => a.t - b.t);
      D.tabpane.append(rows.length ? h('table', { class: 'tbl' }, h('thead', {}, h('tr', {}, h('th', { class: 'num' }, 't s'), h('th', {}, '来源'), h('th', {}, '事件'), h('th', {}, '详情'))),
        h('tbody', {}, rows.map((r) => h('tr', { class: 'click', onclick: () => setTime(r.t) }, h('td', { class: 'num' }, fmtNum(r.t)), h('td', {}, r.src),
          h('td', { class: 'mono ' + (/brown|reset/i.test(r.ev) ? 'bad-t' : '') }, r.ev), h('td', { class: 'mono' }, r.d || ''))))) : h('div', { class: 'empty' }, '没有事件'));
    }
  }

  function unitFromMetric(k) {
    if (/_v$|\.min_v$|rail_v/.test(k)) return 'V';
    if (/_a$/.test(k)) return 'A';
    if (/deg/.test(k)) return '°';
    if (/force_n/.test(k)) return 'N';
    if (/_s$/.test(k)) return 's';
    return '';
  }

  let uartQuery = '', uartImportant = false;
  function renderUart() {
    const q = h('input', { class: 'input sm grow', placeholder: '搜索串口日志…', value: uartQuery, 'aria-label': '搜索串口日志' });
    const imp = h('button', { class: 'btn sm' + (uartImportant ? ' on' : ''), 'aria-pressed': String(uartImportant), title: '只显示 EVT / 启动 / 欠压 / 错误' }, '只看关键行');
    const box = h('div', { class: 'uart' });
    const draw = () => {
      const qq = uartQuery.toLowerCase();
      const re = qq ? new RegExp(qq.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi') : null;
      const lines = uartLines.filter((l) => (!qq || l.text.toLowerCase().includes(qq)) && (!uartImportant || cls(l.text)));
      box.innerHTML = lines.length ? lines.map((l) => {
        const c = cls(l.text);
        let t = esc(l.text);
        if (re) t = t.replace(new RegExp(esc(qq).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), (s) => `<mark>${s}</mark>`);
        return `<div class="ln ${c}" data-t="${l.t ?? ''}"><span class="ts">${l.t !== null ? l.t.toFixed(3).padStart(8) : ''}</span>  ${t}</div>`;
      }).join('') : `<div class="empty">${uartLines.length ? '没有匹配的行' : '没有串口输出（scripted 场景没有固件）'}</div>`;
    };
    q.addEventListener('input', debounce(() => { uartQuery = q.value; draw(); }, 100));
    imp.addEventListener('click', () => { uartImportant = !uartImportant; imp.classList.toggle('on', uartImportant); imp.setAttribute('aria-pressed', String(uartImportant)); draw(); });
    box.addEventListener('click', (e) => { const ln = e.target.closest('.ln'); if (ln?.dataset.t) setTime(Number(ln.dataset.t)); });
    D.tabpane.append(h('div', { class: 'uart-tools' }, q, imp), box);
    draw();
  }
  function cls(text) {
    if (/brownout|Brownout|BROWNOUT|rst:0x(?!1 )|abort|panic|Guru|E \(\d+\)/.test(text)) return 'bad';
    if (/^EVT\b/.test(text)) return /boot/.test(text) ? 'evt boot' : 'evt';
    if (/^ets |rst:0x1 /.test(text)) return 'boot';
    if (/^W \(\d+\)/.test(text)) return 'warn';
    return '';
  }

  async function loadSignals(names, id = runId, cache = sigCache) {
    const want = names.filter((n) => !(n in cache));
    if (!want.length) return;
    const d = await get(`run/${id}/signals?names=${encodeURIComponent(want.join(','))}&n=3000`);
    for (const n of want) if (d[n]) cache[n] = { t: d.t, y: d[n] };
  }

  function renderCharts() {
    charts.forEach((c) => c.chart.dispose());
    charts = [];
    clear(D.charts);
    if (!chosen.length) { D.charts.append(h('div', { class: 'empty' }, '点“选择信号”添加曲线')); return; }
    for (const name of chosen) {
      const s = sigCache[name];
      if (!s) continue;
      const cv = h('canvas', { 'aria-label': `${name} 曲线` });
      const val = h('span', { class: 'val' });
      const unit = unitOf(name.replace(/\.(i_motor|i_supply|i_in|i|i_motor_peak)$/, '.x_a').replace(/\.v$/, '.x_v').replace(/rail_v$/, 'x_v').replace(/\.deg$/, '.x_deg').replace(/\.dps$/, '.x_deg_s').replace(/force_n$/, 'x_n').replace(/pulse_us$/, 'x_us').replace(/\.mm$/, '.x_mm'));
      D.charts.append(h('div', { class: 'chart-card' },
        h('header', {}, h('span', { class: 'nm', title: name }, name), val, h('span', { class: 'dim' }, unit),
          h('button', { class: 'btn icon sm ghost x', 'aria-label': `移除 ${name}`, onclick: () => { chosen = chosen.filter((c) => c !== name); savePrefSignals(chosen); renderCharts(); } }, icon('x', 'sm'))),
        cv));
      const chart = new TimeChart(cv, { onSeek: (t) => setTime(t), unit, xRange: dur ? [0, dur] : null });
      chart.setSeries([{ t: s.t, y: s.y, color: seriesColor(0) }]);
      charts.push({ chart, name, val });
    }
    D.sigInfo.textContent = `${chosen.length} / ${(meta?.signals || []).length} 个信号`;
    updateCursor();
  }

  function openPicker(anchor, all = meta?.signals || [], onChange) {
    const search = h('input', { class: 'input sm grow', placeholder: '筛选信号…', 'aria-label': '筛选信号' });
    const body = h('div', { class: 'pk-body' });
    const draw = () => {
      const q = search.value.trim().toLowerCase();
      const groups = {};
      for (const s of all) if (!q || s.toLowerCase().includes(q)) (groups[s.split('.')[0]] ||= []).push(s);
      clear(body).append(Object.entries(groups).map(([g, list]) => [h('div', { class: 'pk-grp' }, g),
        list.map((s) => h('label', {}, h('input', { type: 'checkbox', checked: chosen.includes(s), onchange: async (e) => {
          chosen = e.target.checked ? [...chosen, s] : chosen.filter((c) => c !== s);
          savePrefSignals(chosen);
          if (onChange) onChange(); else { try { await loadSignals(chosen); } catch (err) { toastError(err); } renderCharts(); }
        } }), s))]));
    };
    search.addEventListener('input', draw);
    draw();
    const pop = popover(anchor, h('div', { class: 'picker', style: { display: 'flex', flexDirection: 'column', maxHeight: '60vh' } },
      h('div', { class: 'pk-head' }, search,
        h('button', { class: 'btn sm', onclick: async () => { chosen = all.filter((s) => DEFAULT_SIGNALS.some((re) => re.test(s))).slice(0, 8); savePrefSignals(null); draw(); if (onChange) onChange(); else { await loadSignals(chosen); renderCharts(); } } }, '默认')),
      body));
    search.focus();
    return pop;
  }

  // ---------------------------------------------------------------- playback
  function setTime(t) {
    tNow = Math.max(0, Math.min(t, dur || 0));
    if (D.slider) D.slider.value = dur ? Math.round(tNow / dur * 1000) : 0;
    if (D.time) D.time.textContent = `${tNow.toFixed(3)} / ${fmtNum(dur)} s`;
    replay?.setTime(tNow);
    updateCursor();
  }
  function updateCursor() {
    for (const c of charts) {
      c.chart.setCursor(tNow);
      const v = c.chart.valueAt(0, tNow);
      c.val.textContent = v === null || v === undefined ? '' : fmtNum(v);
    }
  }
  function togglePlay() { playing ? stopPlay() : startPlay(); }
  let last = 0;
  function startPlay() {
    if (!dur) return;
    if (tNow >= dur) setTime(0);
    playing = true; last = performance.now();
    D.play.replaceChildren(icon('pause')); D.play.setAttribute('aria-label', '暂停');
    requestAnimationFrame(tick);
  }
  function stopPlay() {
    playing = false;
    if (D.play) { D.play.replaceChildren(icon('play')); D.play.setAttribute('aria-label', '播放'); }
  }
  function tick(now) {
    if (!playing) return;
    const dt = (now - last) / 1000; last = now;
    setTime(tNow + dt * speed);
    if (tNow >= dur) { stopPlay(); return; }
    requestAnimationFrame(tick);
  }
  el.addEventListener('keydown', (e) => {
    if (mode !== 'run' || e.target.closest('input, textarea, select, button')) return;
    if (e.key === ' ') { e.preventDefault(); togglePlay(); }
    if (e.key === 'ArrowRight') setTime(tNow + 0.01);
    if (e.key === 'ArrowLeft') setTime(tNow - 0.01);
  });

  // ---------------------------------------------------------------- compare
  let cmp = null;
  async function openCompare(a, b) {
    stopPlay();
    mode = 'compare'; cmpIds = [a, b]; runId = null;
    picked.length = 0; picked.push(a, b);
    renderList();
    clear(right).append(h('div', { class: 'empty' }, '加载中…'));
    let ma, mb;
    try { [ma, mb] = await Promise.all([get(`run/${a}`), get(`run/${b}`)]); } catch (e) { clear(right).append(h('div', { class: 'banner bad', style: { margin: '12px' } }, e.message)); return; }
    if (mode !== 'compare' || cmpIds[0] !== a) return;
    cmp = { a: ma, b: mb, cacheA: {}, cacheB: {} };
    const common = (ma.signals || []).filter((s) => (mb.signals || []).includes(s));
    chosen = defaultSignals(common);
    const chartsBox = h('div', { class: 'charts' });
    const sigBtn = h('button', { class: 'btn sm' }, icon('wave', 'sm'), '选择信号');
    const cA = seriesColor(0), cB = seriesColor(1);
    const label = (m) => `${m.scenario?.name} · ${m.run_id.slice(9, 15).replace(/(\d\d)(\d\d)(\d\d)/, '$1:$2:$3')}`;
    const ovDiff = [...new Set([...Object.keys(ma.overrides || {}), ...Object.keys(mb.overrides || {})])];
    const mA = Object.fromEntries(flatMetrics(ma.metrics)), mB = Object.fromEntries(flatMetrics(mb.metrics));
    const mkeys = [...new Set([...Object.keys(mA), ...Object.keys(mB)])];
    const checksKeys = [...new Set([...(ma.checks || []).map((c) => c.check), ...(mb.checks || []).map((c) => c.check)])];
    const findCk = (m, k) => (m.checks || []).find((c) => c.check === k);
    const runCard = (m, color, tag) => h('div', { class: 'card' },
      h('header', {}, h('span', { class: 'legend' }, h('span', { class: 'sw', style: { background: color, height: '4px', width: '18px' } })), h('h2', {}, `${tag} · ${m.scenario?.name}`),
        m.passed === true ? h('span', { class: 'chip ok' }, '通过') : m.passed === false ? h('span', { class: 'chip bad' }, '未通过') : null,
        h('button', { class: 'btn sm ghost', style: { marginLeft: 'auto' }, onclick: () => ctx.openRun(m.run_id) }, '打开')),
      h('div', { class: 'body' }, h('div', { class: 'mono dim' }, m.run_id),
        Object.keys(m.overrides || {}).length ? h('div', { class: 'row wrap', style: { marginTop: '6px' } }, Object.entries(m.overrides).map(([k, v]) => h('span', { class: 'chip mono', title: `${k} = ${fmtValue(v)}` }, `${k} = ${fmtValue(v)}`))) : h('div', { class: 'hint', style: { marginTop: '4px' } }, '无覆盖参数')));
    clear(right).append(h('div', { class: 'rd' },
      h('div', { class: 'rd-head' }, h('div', { class: 'l1' }, h('h2', {}, '对比两次运行'), h('span', { class: 'grow' }),
        h('button', { class: 'btn sm', onclick: () => ctx.askChat(`对比运行 ${a} 和 ${b}，差别在哪里，原因是什么？`) }, icon('chat', 'sm'), '让 AI 解读'),
        h('button', { class: 'btn sm ghost', onclick: () => { picked.length = 0; mode = null; cmpIds = null; ctx.nav('results'); } }, icon('x', 'sm'), '退出对比'))),
      h('div', { class: 'rd-body' },
        h('div', { class: 'cmp-head' }, runCard(ma, cA, 'A'), runCard(mb, cB, 'B')),
        ovDiff.length ? h('div', { style: { padding: '0 12px 10px' } }, h('div', { class: 'card' }, h('header', {}, h('h2', {}, '参数差异')),
          h('div', { class: 'body flush' }, h('table', { class: 'tbl' }, h('thead', {}, h('tr', {}, h('th', {}, '参数'), h('th', { class: 'num' }, 'A'), h('th', { class: 'num' }, 'B'))),
            h('tbody', {}, ovDiff.map((k) => h('tr', {}, h('td', { class: 'mono' }, k), h('td', { class: 'num' }, fmtValue(ma.overrides?.[k] ?? '（工程值）')), h('td', { class: 'num' }, fmtValue(mb.overrides?.[k] ?? '（工程值）'))))))))) : null,
        h('div', { class: 'sig-bar' }, sigBtn, h('span', { class: 'legend' }, h('span', {}, h('span', { class: 'sw', style: { background: cA } }), 'A ', label(ma)), h('span', {}, h('span', { class: 'sw', style: { background: cB } }), 'B ', label(mb))),
          h('span', { class: 'grow' }), h('span', { class: 'hint' }, `${common.length} 个共同信号`)),
        chartsBox,
        h('div', { style: { padding: '0 12px 12px', display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))', gap: '10px' } },
          h('div', { class: 'card' }, h('header', {}, h('h2', {}, '指标对比'), h('span', { class: 'meta' }, 'Δ = B − A')),
            h('div', { class: 'body flush', style: { maxHeight: '420px', overflow: 'auto' } }, h('table', { class: 'tbl' },
              h('thead', {}, h('tr', {}, h('th', {}, '指标'), h('th', { class: 'num' }, 'A'), h('th', { class: 'num' }, 'B'), h('th', { class: 'num' }, 'Δ'), h('th', { class: 'num' }, 'Δ%'))),
              h('tbody', {}, mkeys.map((k) => {
                const va = mA[k], vb = mB[k];
                const num = typeof va === 'number' && typeof vb === 'number';
                const d = num ? vb - va : null;
                const pct = num && va !== 0 ? d / Math.abs(va) * 100 : null;
                const same = num ? Math.abs(d) < 1e-12 : String(va) === String(vb);
                return h('tr', { style: same ? { opacity: 0.6 } : {} }, h('td', { class: 'mono' }, k),
                  h('td', { class: 'num' }, typeof va === 'number' ? fmtNum(va) : String(va ?? '—')), h('td', { class: 'num' }, typeof vb === 'number' ? fmtNum(vb) : String(vb ?? '—')),
                  h('td', { class: 'num' }, num && !same ? (d > 0 ? '+' : '') + fmtNum(d) : ''), h('td', { class: 'num' }, pct !== null && !same && isFinite(pct) ? `${pct > 0 ? '+' : ''}${pct.toFixed(1)}%` : ''));
              }))))),
          h('div', { class: 'card' }, h('header', {}, h('h2', {}, '断言对比')),
            h('div', { class: 'body flush' }, checksKeys.length ? h('table', { class: 'tbl' },
              h('thead', {}, h('tr', {}, h('th', {}, '断言'), h('th', { class: 'num' }, 'A'), h('th', { class: 'num' }, 'B'))),
              h('tbody', {}, checksKeys.map((k) => {
                const ca = findCk(ma, k), cb = findCk(mb, k);
                const cell = (c) => c ? h('span', { class: c.pass ? 'ok-t' : 'bad-t' }, icon(c.pass ? 'check' : 'x', 'sm'), ' ', fmtValue(c.value)) : h('span', { class: 'dim' }, '—');
                return h('tr', {}, h('td', { class: 'mono' }, checkText(ca || cb)), h('td', { class: 'num' }, cell(ca)), h('td', { class: 'num' }, cell(cb)));
              }))) : h('div', { class: 'empty' }, '没有断言')))))));
    const drawCharts = async () => {
      try { await Promise.all([loadSignals(chosen, a, cmp.cacheA), loadSignals(chosen, b, cmp.cacheB)]); } catch (e) { toastError(e); }
      charts.forEach((c) => c.chart.dispose()); charts = [];
      clear(chartsBox);
      const durA = ma.scenario?.duration_s || 0, durB = mb.scenario?.duration_s || 0;
      for (const n of chosen) {
        const sa = cmp.cacheA[n], sb = cmp.cacheB[n];
        if (!sa && !sb) continue;
        const cv = h('canvas', { 'aria-label': `${n} 对比曲线` });
        chartsBox.append(h('div', { class: 'chart-card' }, h('header', {}, h('span', { class: 'nm' }, n)), cv));
        const chart = new TimeChart(cv, { xRange: [0, Math.max(durA, durB) || 1] });
        chart.setSeries([sa && { t: sa.t, y: sa.y, color: cA, label: 'A' }, sb && { t: sb.t, y: sb.y, color: cB, label: 'B' }].filter(Boolean));
        charts.push({ chart, name: n, val: h('span') });
      }
      if (!chosen.length) chartsBox.append(h('div', { class: 'empty' }, '点“选择信号”添加曲线'));
    };
    sigBtn.addEventListener('click', () => openPicker(sigBtn, common, drawCharts));
    drawCharts();
  }

  // ---------------------------------------------------------------- wiring
  store.on('runs', () => {
    for (const id of store.newRuns) newMarks.add(id);
    if (!el.hidden) { store.newRuns.clear(); renderList(); }
    // the open run may have been deleted elsewhere (e.g. by the AI)
    if (mode === 'run' && runId && !store.runs.some((r) => r.run_id === runId)) { runId = null; mode = null; renderEmpty(); }
  });
  const themeMq = matchMedia('(prefers-color-scheme: dark)');
  const retheme = () => { replay?.applyTheme(); charts.forEach((c) => c.chart.draw()); };
  themeMq.addEventListener?.('change', retheme);
  store.on('theme', retheme);

  return {
    el,
    show(arg) {
      for (const id of store.newRuns) newMarks.add(id);
      store.newRuns.clear();
      renderList();
      if (arg && arg.includes('..')) { const [a, b] = arg.split('..'); if (!(mode === 'compare' && cmpIds?.[0] === a && cmpIds?.[1] === b)) openCompare(a, b); }
      else if (arg) { if (!(mode === 'run' && runId === arg)) openRun(arg); }
      else if (!mode) renderEmpty();
      else if (mode === 'run' && runId) history.replaceState(null, '', `#/results/${encodeURIComponent(runId)}`);
      replay?.resize();
    },
    hide() { stopPlay(); },
  };
}
