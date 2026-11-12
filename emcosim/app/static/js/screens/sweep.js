// 扫参：选场景、1–2 个参数和取值、要收集的指标 → POST /api/jobs/sweep；表格和曲线随结果逐行填充。
import { get, post } from '../api.js';
import { store, paramPaths, getPath } from '../store.js';
import { h, icon, clear, add, fmtNum, fmtValue, parseLoose, toast, toastError, seriesColor } from '../ui.js';
import { XYChart } from '../chart.js';

const PREFERRED = [/^mcu\.resets$/, /^mcu\.min_rail_v$/, /min_v$/, /peak_current_a$/, /force_n\.peak$/, /saturated_s$/, /rocker_axis\.deg\.final$/];

export function createSweep(ctx) {
  const form = h('div', { class: 'sw-form' });
  const mainBox = h('div', { class: 'sw-main' });
  const el = h('section', { class: 'screen', 'aria-label': '扫参' },
    h('div', { class: 'screen-head' }, h('h1', {}, '扫参'), h('span', { class: 'sub' }, '对一个场景批量试不同参数，每组都是一次独立运行（只用临时覆盖，不改 project.yaml）')),
    h('div', { class: 'sw-wrap', style: { flex: 1, minHeight: 0 } }, form, mainBox));

  const st = { scenario: null, params: [{ path: '', values: '' }], metrics: new Set(), extraMetric: '', metricNames: [], jobId: null, yMetric: null };
  let chart = null;

  // ---------------------------------------------------------------- form
  async function loadMetricNames() {
    const run = (store.runs || []).find((r) => r.scenario === st.scenario);
    st.metricNames = [];
    if (run) {
      try {
        const m = await get(`run/${run.run_id}`);
        for (const [k, v] of Object.entries(m.metrics || {})) if (typeof v === 'number') st.metricNames.push(k);
      } catch (e) { /* no metrics yet */ }
    }
    if (!st.metrics.size) st.metricNames.filter((n) => PREFERRED.some((re) => re.test(n))).slice(0, 5).forEach((n) => st.metrics.add(n));
    renderForm();
  }

  function suggestions(path) {
    const d = store.project?.data || {};
    if (/\.node$|\.from$|\.to$|\.input$/.test(path)) return Object.keys(d.power?.nodes || {}).join(', ');
    if (/\.preset$/.test(path)) return Object.keys(store.project?.presets || {}).join(', ');
    let v = getPath(d, path.startsWith('firmware.') ? 'mcu.firmware.params.' + path.slice(9) : path);
    if (v === undefined && path.startsWith('firmware.')) v = store.project?.firmware_params?.[path.slice(9)];
    if (v === undefined && /^mcu\.firmware\.params\./.test(path)) v = store.project?.firmware_params?.[path.split('.').pop()];
    if (typeof v === 'number') { const a = v; return [a * 0.5, a * 0.75, a, a * 1.5, a * 2].map((x) => fmtNum(x, 3)).join(', '); }
    return '';
  }

  function renderForm() {
    const p = store.project;
    if (!p) { clear(form).append(h('div', { class: 'empty' }, '还没有打开工程')); return; }
    if (!st.scenario || !p.scenarios.includes(st.scenario)) st.scenario = p.scenarios.includes('servo_sweep_scripted') ? 'servo_sweep_scripted' : p.scenarios[0];
    const paths = paramPaths(p.data).map((x) => x.path).filter((x) => /^(power|actuators|mechanics|mcu)\./.test(x))
      .concat(Object.keys(p.firmware_params || {}).map((k) => `firmware.${k}`));
    const sel = h('select', { class: 'input', 'aria-label': '场景' }, p.scenarios.map((s) => h('option', { value: s, selected: s === st.scenario }, s)));
    sel.addEventListener('change', () => { st.scenario = sel.value; st.metrics.clear(); loadMetricNames(); });
    const scripted = /mcu_backend:\s*scripted/.test(p.scenario_texts?.[st.scenario] || '');
    const countEl = h('span', { class: 'muted grow' });
    const updCount = () => { countEl.textContent = `共 ${st.params.reduce((n, q) => n * Math.max(1, splitVals(q.values).length), 1)} 组运行`; };
    updCount();

    const paramEls = st.params.map((q, i) => {
      const pi = h('input', { class: 'input mono', list: 'sw-paths', value: q.path, placeholder: '参数路径，例如 power.sources.usb.i_limit_a', 'aria-label': `参数 ${i + 1} 路径` });
      const vi = h('input', { class: 'input mono', value: q.values, placeholder: '取值，用逗号分隔', 'aria-label': `参数 ${i + 1} 取值` });
      const hint = h('div', { class: 'hint' });
      const upd = () => { const s = suggestions(q.path); clear(hint).append(s ? [h('span', {}, '建议：'), h('a', { href: '#', onclick: (e) => { e.preventDefault(); q.values = s; vi.value = s; renderForm(); } }, s)] : null); };
      pi.addEventListener('change', () => { q.path = pi.value.trim(); upd(); });
      pi.addEventListener('input', () => { q.path = pi.value.trim(); });
      vi.addEventListener('input', () => { q.values = vi.value; updCount(); });
      // range helper
      const a = h('input', { class: 'input sm mono', placeholder: '起', style: { width: '64px' }, 'aria-label': '起始值' });
      const b = h('input', { class: 'input sm mono', placeholder: '止', style: { width: '64px' }, 'aria-label': '结束值' });
      const s = h('input', { class: 'input sm mono', placeholder: '步长', style: { width: '64px' }, 'aria-label': '步长' });
      const gen = h('button', { class: 'btn sm', onclick: () => {
        const x0 = Number(a.value), x1 = Number(b.value), dx = Math.abs(Number(s.value));
        if (!isFinite(x0) || !isFinite(x1) || !(dx > 0)) return toast('起、止、步长需要是数值', { kind: 'bad' });
        const out = []; const dir = x1 >= x0 ? 1 : -1;
        for (let x = x0; dir > 0 ? x <= x1 + dx * 1e-9 : x >= x1 - dx * 1e-9; x += dir * dx) { out.push(fmtNum(x, 6)); if (out.length > 60) break; }
        q.values = out.join(', '); renderForm();
      } }, '生成');
      upd();
      return h('div', { class: 'sw-param' },
        h('div', { class: 'row' }, h('b', { class: 'grow' }, `参数 ${i + 1}`), st.params.length > 1 ? h('button', { class: 'btn icon sm ghost', 'aria-label': '删除参数', onclick: () => { st.params.splice(i, 1); renderForm(); } }, icon('x', 'sm')) : null),
        pi, vi, hint, h('div', { class: 'row' }, h('span', { class: 'hint', style: { whiteSpace: 'nowrap' } }, '范围'), a, b, s, gen));
    });

    const metricBox = h('div', { class: 'metric-pick' });
    const allMetrics = [...new Set([...st.metricNames, ...st.metrics])];
    add(metricBox, allMetrics.length ? allMetrics.map((n) => h('label', {}, h('input', { type: 'checkbox', checked: st.metrics.has(n), onchange: (e) => { e.target.checked ? st.metrics.add(n) : st.metrics.delete(n); } }), n))
      : h('div', { class: 'hint' }, '先运行一次这个场景，才能列出它的指标；也可以在下面手动输入'));
    const extra = h('input', { class: 'input mono sm grow', placeholder: '其他指标名，回车添加', 'aria-label': '添加指标' });
    extra.addEventListener('keydown', (e) => { if (e.key === 'Enter' && extra.value.trim()) { st.metrics.add(extra.value.trim()); renderForm(); } });

    clear(form).append(
      h('label', { class: 'field' }, '场景', sel),
      !scripted && !store.app?.qemu?.found ? h('div', { class: 'note' }, icon('alert', 'sm'), '这个场景需要 QEMU，但没有找到') : null,
      scripted ? h('div', { class: 'hint' }, 'scripted 场景不跑固件，每组约 1–2 秒') : h('div', { class: 'hint' }, '固件场景每组约 5 秒（QEMU）'),
      paramEls,
      st.params.length < 2 ? h('button', { class: 'btn sm', onclick: () => { st.params.push({ path: '', values: '' }); renderForm(); } }, icon('plus', 'sm'), '再加一个参数（二维网格）') : null,
      h('datalist', { id: 'sw-paths' }, paths.map((x) => h('option', { value: x }))),
      h('div', { class: 'field' }, h('span', {}, '收集的指标'), metricBox, h('div', { class: 'row' }, extra)),
      h('div', { class: 'row' }, countEl,
        h('button', { class: 'btn primary', onclick: start }, icon('play', 'sm'), '开始扫参')));
  }

  function splitVals(s) { return String(s || '').split(/[,，;；\s]+/).map((x) => x.trim()).filter(Boolean); }

  async function start() {
    const params = {};
    for (const q of st.params) {
      if (!q.path) continue;
      const vals = splitVals(q.values).map(parseLoose);
      if (!vals.length) return toast(`${q.path} 还没有取值`, { kind: 'bad' });
      params[q.path] = vals;
    }
    if (!Object.keys(params).length) return toast('至少要一个参数路径', { kind: 'bad' });
    try {
      const job = await post('jobs/sweep', { scenario: st.scenario, params, metrics: [...st.metrics] });
      store.jobs.set(job.id, job);
      st.jobId = job.id;
      st.yMetric = null;
      history.replaceState(null, '', `#/sweep/${job.id}`);
      renderMain();
    } catch (e) { toastError(e); }
  }

  // ---------------------------------------------------------------- results
  function renderMain() {
    const sweeps = [...store.jobs.values()].filter((j) => j.kind === 'sweep').sort((a, b) => b.created - a.created);
    if (!st.jobId || !store.jobs.has(st.jobId)) st.jobId = sweeps[0]?.id || null;
    const j = st.jobId && store.jobs.get(st.jobId);
    chart?.dispose(); chart = null;
    if (!j) { clear(mainBox).append(h('div', { class: 'empty' }, '左边设置好参数后点“开始扫参”。结果会一行一行填进表格，并画出指标随参数变化的曲线。'), exampleCard()); return; }
    const keys = Object.keys(j.args.params || {});
    const metrics = j.args.metrics || [];
    if (!st.yMetric || !metrics.includes(st.yMetric) || !st.yPicked) {
      // default to the metric that varies most across the rows so far
      let best = metrics[0] || null, spread = -1;
      for (const m of metrics) {
        const v = j.rows.map((r) => r[m]).filter((x) => typeof x === 'number');
        if (v.length < 2) continue;
        const sp = (Math.max(...v) - Math.min(...v)) / (Math.abs(Math.max(...v)) || 1);
        if (sp > spread) { spread = sp; best = m; }
      }
      st.yMetric = best;
    }
    const total = Object.values(j.args.params).reduce((n, v) => n * v.length, 1);
    const jobSel = h('select', { class: 'input sm', 'aria-label': '选择扫参任务' }, sweeps.map((x) => h('option', { value: x.id, selected: x.id === j.id }, `${x.id} · ${x.args.scenario} · ${Object.keys(x.args.params).join(' × ')}`)));
    jobSel.addEventListener('change', () => { st.jobId = jobSel.value; st.yMetric = null; st.yPicked = false; renderMain(); });
    const ySel = h('select', { class: 'input sm', 'aria-label': '纵轴指标' }, metrics.length ? metrics.map((m) => h('option', { value: m, selected: m === st.yMetric }, m)) : h('option', { value: '' }, '（没有选指标）'));
    ySel.addEventListener('change', () => { st.yMetric = ySel.value; st.yPicked = true; drawChart(j); });
    const statusChip = { queued: ['排队', ''], running: ['运行中', 'accent'], done: ['完成', 'ok'], failed: ['失败', 'bad'], cancelled: ['已取消', ''] }[j.status] || [j.status, ''];
    const cv = h('canvas', { 'aria-label': '指标随参数变化' });
    clear(mainBox).append(
      h('div', { class: 'card' },
        h('header', {}, h('h2', {}, icon('sweep'), '扫参任务'), jobSel, h('span', { class: 'chip ' + statusChip[1] }, statusChip[0]),
          h('span', { class: 'meta' }, `${j.rows.length} / ${total} 组`),
          (j.status === 'running' || j.status === 'queued') ? h('button', { class: 'btn sm danger', onclick: async () => { try { await post(`jobs/${j.id}/cancel`); } catch (e) { toastError(e); } } }, icon('stop', 'sm'), '取消') : null),
        h('div', { class: 'body' }, h('div', { class: 'progress ' + j.status }, h('i', { style: { width: `${Math.round((j.status === 'done' ? 1 : j.progress || 0) * 100)}%` } })),
          j.error ? h('div', { class: 'banner bad', style: { marginTop: '8px' } }, j.error) : null)),
      h('div', { class: 'card xy' },
        h('header', {}, h('h2', {}, '指标 vs ', h('span', { class: 'mono' }, keys[0] || '')), h('span', { class: 'grow' }), h('span', { class: 'hint' }, '纵轴'), ySel),
        h('div', { class: 'body' }, cv, keys.length > 1 ? h('div', { class: 'legend', style: { marginTop: '6px' }, id: 'sw-legend' }) : null)),
      h('div', { class: 'card' },
        h('header', {}, h('h2', {}, '结果表'), h('span', { class: 'meta' }, '点击一行打开这次运行')),
        h('div', { class: 'body flush', style: { overflow: 'auto', maxHeight: '480px' } },
          h('table', { class: 'tbl' },
            h('thead', {}, h('tr', {}, h('th', {}, '#'), keys.map((k) => h('th', { class: 'mono' }, k.split('.').slice(-2).join('.'))), h('th', {}, '断言'),
              metrics.map((m) => h('th', { class: 'num mono', title: m }, m.split('.').slice(-2).join('.'))), h('th', {}, '未过的断言'))),
            h('tbody', {}, j.rows.map((r, i) => h('tr', { class: 'click', onclick: () => ctx.openRun(r.run_id), title: r.run_id },
              h('td', { class: 'num' }, i + 1), keys.map((k) => h('td', { class: 'mono' }, fmtValue(r[k]))),
              h('td', {}, r.passed === true ? h('span', { class: 'chip ok' }, '通过') : r.passed === false ? h('span', { class: 'chip bad' }, '未通过') : h('span', { class: 'chip' }, '—')),
              metrics.map((m) => h('td', { class: 'num' }, typeof r[m] === 'number' ? fmtNum(r[m]) : fmtValue(r[m]))),
              h('td', { class: 'mono dim', style: { fontSize: '11px' } }, (r.failed_checks || []).join(', ')))),
              j.status === 'running' || j.status === 'queued' ? h('tr', {}, h('td', { colspan: keys.length + metrics.length + 3, class: 'dim' }, j.status === 'queued' ? '排队中…' : `正在运行第 ${j.rows.length + 1} 组…`)) : null)))));
    chart = new XYChart(cv, { onPick: (row) => ctx.openRun(row.run_id) });
    drawChart(j);
  }

  function drawChart(j) {
    if (!chart) return;
    const keys = Object.keys(j.args.params || {});
    const xs = j.args.params[keys[0]] || [];
    const numeric = xs.every((v) => typeof v === 'number');
    const y = st.yMetric;
    const groups = keys.length > 1 ? j.args.params[keys[1]] : [null];
    const series = groups.map((g, gi) => ({
      label: g === null ? y : `${keys[1]} = ${fmtValue(g)}`,
      color: seriesColor(gi),
      points: j.rows.filter((r) => g === null || JSON.stringify(r[keys[1]]) === JSON.stringify(g)).map((r) => ({
        x: numeric ? r[keys[0]] : xs.findIndex((v) => JSON.stringify(v) === JSON.stringify(r[keys[0]])),
        y: typeof r[y] === 'number' ? r[y] : (typeof r[y] === 'boolean' ? Number(r[y]) : NaN),
        row: r, failed: r.passed === false,
        label: `${keys.map((k) => `${k.split('.').pop()}=${fmtValue(r[k])}`).join(', ')}`,
      })),
    }));
    chart.set(series, { categories: numeric ? null : xs.map(fmtValue), xLabel: keys[0], yLabel: y || '' });
    const lg = mainBox.querySelector('#sw-legend');
    if (lg) clear(lg).append(series.map((s) => h('span', {}, h('span', { class: 'sw', style: { background: s.color } }), s.label)), h('span', { class: 'dim' }, '红点 = 断言未通过'));
  }

  function exampleCard() {
    return h('div', { class: 'card' }, h('header', {}, h('h2', {}, icon('info'), '例子')), h('div', { class: 'body' },
      h('ul', { style: { margin: 0, paddingLeft: '18px', lineHeight: 1.8 } },
        [['actuators.servo1.node', 'usb5v, v5_servo', '舵机接哪路电'],
          ['power.sources.usb.i_limit_a', '0.5, 0.9, 1.5', 'USB 口限流'],
          ['mechanics.elements.rocker_detent.peak_force_n', '3, 5, 7, 9', '开关越来越硬'],
          ['firmware.press_angle_deg', '10, 12, 14, 16', '固件按压角（不用重新编译）']].map(([pp, vv, dd]) => h('li', {},
          h('a', { href: '#', class: 'mono', onclick: (e) => { e.preventDefault(); st.params = [{ path: pp, values: vv }]; renderForm(); } }, pp), `：${vv} — ${dd}`)))));
  }

  store.on('job', (j) => {
    if (el.hidden || j.kind !== 'sweep') return;
    if (j.id === st.jobId || !st.jobId) renderMain();
  });
  let lastP = 0;
  store.on('job_progress', (d) => {
    if (el.hidden || d.id !== st.jobId) return;
    const now = performance.now();
    if (now - lastP < 300) return;
    lastP = now;
    const bar = mainBox.querySelector('.progress > i');
    if (bar) bar.style.width = `${Math.round(d.progress * 100)}%`;
  });
  store.on('project', () => { if (!el.hidden) renderForm(); });

  return {
    el,
    show(arg) {
      if (arg && store.jobs.has(arg)) st.jobId = arg;
      if (!st.scenario) renderForm();
      loadMetricNames();
      renderMain();
    },
  };
}
