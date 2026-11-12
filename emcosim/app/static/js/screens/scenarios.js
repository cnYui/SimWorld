// 场景：场景列表、YAML 编辑器、运行（可带临时覆盖）、任务队列和实时曲线。
import { post, put, del } from '../api.js';
import { store, paramPaths } from '../store.js';
import { h, icon, clear, fmtValue, parseLoose, toast, toastError, modal, confirmDialog, seriesColor, relTime } from '../ui.js';
import { TimeChart } from '../chart.js';

const TEMPLATE = `description: 新场景
duration_s: 2.0
# mcu_backend: scripted     # 不跑固件，用 pin_script 直接给引脚波形
# overrides:                # 只对这个场景生效的参数
#   power.sources.adapter.r_ohm: 0.5
events:
  - {t: 0.8, press: button1, hold_s: 0.1}
expect:
  - {metric: mcu.resets, eq: 0}
`;

export function createScenarios(ctx) {
  const list = h('div', { class: 'pane-body' });
  const nameEl = h('span', { class: 'mono', style: { fontWeight: 600 } });
  const badge = h('span');
  const dirtyEl = h('span', { class: 'chip warn', hidden: true }, '未保存');
  const saveBtn = h('button', { class: 'btn sm', onclick: () => save() }, icon('save', 'sm'), '保存');
  const revertBtn = h('button', { class: 'btn sm ghost', onclick: () => load(cur, true) }, '还原');
  const delBtn = h('button', { class: 'btn icon sm ghost', title: '删除场景', 'aria-label': '删除场景', onclick: () => remove() }, icon('trash', 'sm'));
  const gutter = h('div', { class: 'gutter', 'aria-hidden': 'true' });
  const ta = h('textarea', { spellcheck: 'false', 'aria-label': '场景 YAML', wrap: 'off' });
  const desc = h('div', { class: 'hint', style: { padding: '6px 10px', borderTop: '1px solid var(--line)' } });
  const runPanel = h('div', { class: 'card' });
  const jobsPanel = h('div', { class: 'card' });
  const livePanel = h('div', { class: 'card' });
  const el = h('section', { class: 'screen', 'aria-label': '场景' },
    h('div', { class: 'screen-head' }, h('h1', {}, '场景'), h('span', { class: 'sub' }, '编辑场景文件（scenarios/*.yaml）并运行')),
    h('div', { class: 'sc-wrap', style: { flex: 1, minHeight: 0 } },
      h('div', { class: 'pane' }, h('div', { class: 'pane-head' }, h('span', { class: 'title grow' }, '场景'),
        h('button', { class: 'btn sm', onclick: () => create() }, icon('plus', 'sm'), '新建')), list),
      h('div', { class: 'editor-wrap' },
        h('div', { class: 'editor-bar' }, nameEl, badge, dirtyEl, h('span', { class: 'grow' }), revertBtn, delBtn, saveBtn),
        h('div', { class: 'code-editor' }, gutter, ta), desc),
      h('div', { class: 'sc-side' }, runPanel, livePanel, jobsPanel)));

  let cur = null, saved = '', overrides = [], liveJob = null, charts = [];

  // ---------------------------------------------------------------- editor
  const lines = () => { const n = ta.value.split('\n').length; gutter.textContent = Array.from({ length: n }, (_, i) => i + 1).join('\n'); gutter.scrollTop = ta.scrollTop; };
  ta.addEventListener('input', () => { lines(); markDirty(); });
  ta.addEventListener('scroll', () => { gutter.scrollTop = ta.scrollTop; });
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Tab' && !e.shiftKey) { e.preventDefault(); document.execCommand('insertText', false, '  '); }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); save(); }
    if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey) {
      // keep indentation (and continue "- " list items)
      const before = ta.value.slice(0, ta.selectionStart).split('\n').pop();
      const ind = (before.match(/^\s*/) || [''])[0];
      if (ind) { e.preventDefault(); document.execCommand('insertText', false, '\n' + ind); }
    }
  });
  const dirty = () => cur && ta.value !== saved;
  function markDirty() { dirtyEl.hidden = !dirty(); saveBtn.classList.toggle('primary', !!dirty()); renderRun(); }

  function isScripted(text) { return /^\s*mcu_backend:\s*scripted/m.test(text || ''); }
  function describe(text) { const m = (text || '').match(/^description:\s*(.+)$/m); return m ? m[1].replace(/^['"]|['"]$/g, '') : ''; }
  function duration(text) { const m = (text || '').match(/^duration_s:\s*([\d.]+)/m); return m ? Number(m[1]) : null; }

  function renderList() {
    const p = store.project;
    const texts = p?.scenario_texts || {};
    clear(list).append((p?.scenarios || []).map((n) => h('button', { class: 'list-item' + (n === cur ? ' sel' : ''), onclick: () => select(n) },
      h('div', { class: 'l1' }, h('span', { class: 'name mono' }, n), isScripted(texts[n]) ? h('span', { class: 'chip' }, '脚本') : h('span', { class: 'chip accent' }, '固件')),
      h('div', { class: 'l2' }, describe(texts[n]) || '—'))));
    if (!p?.scenarios?.length) list.append(h('div', { class: 'empty' }, '还没有场景'));
  }

  async function select(n) {
    if (n === cur) return;
    if (dirty() && !(await confirmDialog('放弃修改？', `场景 ${cur} 有未保存的修改。`, '放弃修改', true))) return;
    load(n, true);
  }

  function load(n, force) {
    const texts = store.project?.scenario_texts || {};
    if (!n || !(n in texts)) { cur = null; saved = ''; ta.value = ''; ta.disabled = true; nameEl.textContent = '未选择场景'; clear(badge); desc.textContent = ''; lines(); markDirty(); renderList(); return; }
    if (!force && dirty()) return;      // keep the user's edits when the file changes underneath
    cur = n; saved = texts[n];
    ta.disabled = false;
    ta.value = saved;
    nameEl.textContent = `${n}.yaml`;
    clear(badge).append(isScripted(saved) ? h('span', { class: 'chip' }, '脚本 MCU（不需要 QEMU）') : h('span', { class: 'chip accent' }, '固件在 QEMU 中运行'));
    desc.textContent = '保存时文件会按 YAML 重新排版（注释不保留）。Ctrl+S 保存，Tab 缩进两格。';
    lines(); markDirty(); renderList(); renderRun();
  }

  async function save() {
    if (!cur) return;
    try {
      await put(`scenario/${encodeURIComponent(cur)}`, { yaml: ta.value });
      await store.loadProject();
      saved = store.project.scenario_texts?.[cur] ?? ta.value;
      ta.value = saved; lines(); markDirty();
      toast(`已保存 ${cur}.yaml`, { timeout: 2000 });
    } catch (e) { toastError(e); }
  }

  async function create() {
    const name = h('input', { class: 'input mono', placeholder: '例如 stall_9n_v5servo' });
    const ok = await modal({
      title: '新建场景', body: [h('label', { class: 'field' }, '场景名（字母、数字、_ 和 -）', name)],
      actions: [{ label: '取消', value: false }, {
        label: '创建', kind: 'primary', handler: async () => {
          const n = name.value.trim();
          if (!/^[\w-]+$/.test(n)) throw new Error('场景名只能用字母、数字、_ 和 -');
          if (store.project.scenarios.includes(n)) throw new Error('已有同名场景');
          await put(`scenario/${n}`, { yaml: TEMPLATE });
          await store.loadProject();
          return n;
        },
      }],
    });
    if (ok) load(ok, true);
  }

  async function remove() {
    if (!cur || !(await confirmDialog('删除场景', `删除 scenarios/${cur}.yaml？这个操作不能撤销。`, '删除', true))) return;
    try { await del(`scenario/${encodeURIComponent(cur)}`); const n = cur; cur = null; await store.loadProject(); load(store.project.scenarios[0], true); toast(`已删除 ${n}`); } catch (e) { toastError(e); }
  }

  // ---------------------------------------------------------------- run
  function renderRun() {
    const qemu = store.app?.qemu?.found;
    const scripted = isScripted(ta.value);
    const paths = paramPaths(store.project?.data || {}).map((p) => p.path)
      .concat(Object.keys(store.project?.firmware_params || {}).map((k) => `firmware.${k}`));
    const dl = h('datalist', { id: 'sc-param-paths' }, paths.map((p) => h('option', { value: p })));
    const rowsEl = h('div');
    overrides.forEach((o, i) => {
      const pi = h('input', { class: 'input mono sm', list: 'sc-param-paths', value: o.path, placeholder: '参数路径', 'aria-label': '覆盖参数路径' });
      const vi = h('input', { class: 'input mono sm', value: o.value, placeholder: '值', 'aria-label': '覆盖值' });
      pi.addEventListener('input', () => { o.path = pi.value.trim(); });
      vi.addEventListener('input', () => { o.value = vi.value; });
      rowsEl.append(h('div', { class: 'ov-row' }, pi, vi,
        h('button', { class: 'btn icon sm ghost', 'aria-label': '删除这一行', onclick: () => { overrides.splice(i, 1); renderRun(); } }, icon('x', 'sm'))));
    });
    clear(runPanel).append(
      h('header', {}, h('h2', {}, icon('play'), '运行'), h('span', { class: 'meta mono' }, cur || '')),
      h('div', { class: 'body col' },
        !scripted && !qemu && cur ? h('div', { class: 'note' }, icon('alert', 'sm'), '这个场景运行真实固件，需要 QEMU，但没有找到。可以先运行 scripted 场景。') : null,
        h('div', {}, h('div', { class: 'row', style: { marginBottom: '6px' } }, h('span', { class: 'muted grow' }, '临时覆盖（只对这次运行生效，不改 project.yaml）'),
          h('button', { class: 'btn sm ghost', onclick: () => { overrides.push({ path: '', value: '' }); renderRun(); runPanel.querySelector('.ov-row:last-child input')?.focus(); } }, icon('plus', 'sm'), '添加')),
        rowsEl, dl, !overrides.length ? h('div', { class: 'hint' }, '例如 actuators.servo1.node = v5_servo') : null),
        h('div', { class: 'row', style: { justifyContent: 'flex-end' } },
          h('button', { class: 'btn primary', disabled: !cur, onclick: run }, icon('play', 'sm'), dirty() ? '保存并运行' : '运行'),
          h('button', { class: 'btn', disabled: !cur, title: '让 AI 助手解释这个场景', onclick: () => ctx.askChat(`解释一下场景 ${cur} 在测什么，结果应该怎么看。`) }, icon('chat', 'sm'), '问 AI'))));
  }

  async function run() {
    if (!cur) return;
    if (dirty()) { await save(); if (dirty()) return; }
    const ov = {};
    for (const o of overrides) if (o.path) ov[o.path] = parseLoose(o.value);
    try {
      const job = await post('jobs/run', { scenario: cur, overrides: ov });
      store.jobs.set(job.id, job);
      liveJob = job.id;
      renderJobs(); renderLive();
      toast(`已加入队列：${cur}`, { timeout: 2000 });
    } catch (e) { toastError(e); }
  }

  // ---------------------------------------------------------------- jobs
  function renderJobs() {
    const jobs = [...store.jobs.values()].sort((a, b) => b.created - a.created).slice(0, 20);
    clear(jobsPanel).append(h('header', {}, h('h2', {}, icon('clock'), '任务队列'), h('span', { class: 'meta' }, `${jobs.filter((j) => j.status === 'running' || j.status === 'queued').length} 个进行中`)),
      h('div', { class: 'body flush', style: { maxHeight: '360px', overflow: 'auto' } },
        jobs.length ? jobs.map(jobRow) : h('div', { class: 'empty' }, '还没有任务')));
  }

  function jobRow(j) {
    const st = { queued: ['排队', ''], running: ['运行中', 'accent'], done: ['完成', 'ok'], failed: ['失败', 'bad'], cancelled: ['已取消', ''] }[j.status] || [j.status, ''];
    const pct = Math.round((j.progress || 0) * 100);
    const ov = Object.entries(j.args.overrides || {});
    const res = j.result;
    return h('div', { class: 'job', style: { cursor: 'pointer', background: liveJob === j.id ? 'var(--accent-weak)' : '' }, 'data-job': j.id, onclick: () => { liveJob = j.id; renderJobs(); renderLive(); } },
      h('div', { class: 'l1' }, h('span', { class: 'chip' }, j.kind === 'sweep' ? '扫参' : '运行'), h('span', { class: 'name mono' }, j.args.scenario),
        res && j.kind === 'run' ? h('span', { class: 'chip ' + (res.passed ? 'ok' : 'bad') }, res.passed ? '通过' : '未通过') : null,
        h('span', { class: 'chip ' + st[1] }, st[0])),
      h('div', { class: 'progress ' + j.status }, h('i', { style: { width: `${j.status === 'done' ? 100 : pct}%` } })),
      j.error ? h('div', { class: 'err' }, j.error) : null,
      h('div', { class: 'l3' },
        h('span', { class: 'mono' }, j.status === 'running' ? `t = ${(j.t || 0).toFixed(2)} s · ${pct}%` : relTime(j.created)),
        ov.length ? h('span', { class: 'mono', style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }, title: ov.map(([k, v]) => `${k}=${fmtValue(v)}`).join('\n') }, ov.map(([k, v]) => `${k.split('.').slice(-2).join('.')}=${fmtValue(v)}`).join(' ')) : h('span', { class: 'grow' }),
        j.kind === 'sweep' ? h('span', {}, `${j.rows?.length || 0} 组`) : null,
        (j.status === 'queued' || j.status === 'running') ? h('button', { class: 'btn sm danger', onclick: (e) => { e.stopPropagation(); cancel(j); } }, icon('stop', 'sm'), '取消') : null,
        j.status === 'done' && j.kind === 'run' && res?.run_id ? h('button', { class: 'btn sm', onclick: (e) => { e.stopPropagation(); ctx.openRun(res.run_id); } }, '打开结果') : null,
        j.kind === 'sweep' ? h('button', { class: 'btn sm', onclick: (e) => { e.stopPropagation(); ctx.nav('sweep', j.id); } }, '查看') : null));
  }

  async function cancel(j) {
    try { const r = await post(`jobs/${j.id}/cancel`); store.jobs.set(j.id, { ...store.jobs.get(j.id), ...r }); renderJobs(); } catch (e) { toastError(e); }
  }

  // ---------------------------------------------------------------- live charts
  const GROUPS = [
    { title: '电压', unit: 'V', match: (k) => k === 'mcu.rail_v' || /^node\..+\.v$/.test(k) },
    { title: '电机电流', unit: 'A', match: (k) => /^act\..+\.i_motor$/.test(k) },
    { title: '关节角度', unit: '°', match: (k) => /^joint\..+\.deg$/.test(k) },
  ];
  function renderLive() {
    charts.forEach((c) => c.chart.dispose());
    charts = [];
    if (!liveJob || !store.jobs.has(liveJob)) liveJob = store.runningJob()?.id || null;
    const j = liveJob && store.jobs.get(liveJob);
    const buf = j && store.live.get(j.id);
    clear(livePanel).append(h('header', {}, h('h2', {}, icon('wave'), '实时曲线'), h('span', { class: 'meta mono' }, j ? `${j.id} · ${j.args.scenario}` : '')));
    if (!j || !buf || !buf.t.length) {
      livePanel.append(h('div', { class: 'empty' }, j ? (j.status === 'queued' ? '排队中…' : j.status === 'running' ? '等待数据…' : '这个任务没有实时数据（只记录本次打开界面后运行的任务）') : '运行一个场景后，这里会实时显示供电电压、电机电流和关节角度'));
      return;
    }
    const dur = duration(store.project?.scenario_texts?.[j.args.scenario]);
    const box = h('div', { class: 'body live-charts' });
    livePanel.append(box);
    for (const g of GROUPS) {
      const names = Object.keys(buf.sig).filter(g.match);
      if (!names.length) continue;
      const cv = h('canvas');
      const legend = h('span', { class: 'legend' }, names.map((n, i) => h('span', {}, h('span', { class: 'sw', style: { background: seriesColor(i) } }), n.replace(/^(node|act|joint)\./, '').replace(/\.(v|i_motor|deg)$/, ''))));
      box.append(h('div', { class: 'lc' }, h('header', {}, h('span', {}, `${g.title} ${g.unit}`), legend), cv));
      const chart = new TimeChart(cv, { compact: true, xRange: dur ? [0, dur] : null });
      charts.push({ chart, names });
    }
    updateLive();
  }
  function updateLive() {
    const j = liveJob && store.jobs.get(liveJob);
    const buf = j && store.live.get(j.id);
    if (!buf) return;
    for (const { chart, names } of charts) chart.setSeries(names.map((n, i) => ({ t: buf.t, y: buf.sig[n] || [], color: seriesColor(i), label: n })));
  }

  // ---------------------------------------------------------------- events
  store.on('project', () => {
    if (el.hidden) return;
    renderList();
    if (cur && !dirty()) load(cur, true);
  });
  store.on('scenarios', () => { if (!el.hidden) { renderList(); if (cur && !dirty()) load(cur, true); } });
  store.on('job', (j) => {
    if (el.hidden) return;
    if (j.status === 'running' && (!liveJob || store.jobs.get(liveJob)?.status !== 'running')) { liveJob = j.id; renderLive(); }
    renderJobs();
  });
  let lastJobsRender = 0;
  store.on('job_progress', (d) => {
    if (el.hidden) return;
    if (d.id !== liveJob) { liveJob = d.id; renderLive(); }
    else if (!charts.length) renderLive();
    else updateLive();
    const now = performance.now();
    if (now - lastJobsRender > 250) { lastJobsRender = now; renderJobs(); }
  });

  return {
    el,
    show(arg) {
      renderList();
      const names = store.project?.scenarios || [];
      if (arg && names.includes(arg)) load(arg, true);
      else if (!cur || !names.includes(cur)) load(names[0], true);
      else if (!dirty()) load(cur, true);
      renderRun(); renderJobs(); renderLive();
    },
  };
}
