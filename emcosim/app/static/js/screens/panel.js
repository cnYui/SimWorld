// The simple panel: pick a test, it runs, and you watch it. One screen, no setup.
// 3D view + live readout + two charts (angles, contact force).
import { get, post, authUrl } from '../api.js';
import { store } from '../store.js';
import { h, icon, clear, toast, toastError, fmtNum, cssVar } from '../ui.js';
import { TimeChart } from '../chart.js';

const SIGS = ['joint.servo_axis.deg', 'joint.rocker_axis.deg', 'probe.press.force_n', 'act.servo1.torque',
  'act.servo1.target_deg', 'mcu.rail_v', 'mcu.powered', 'mcu.resets', 'node.vin5.v', 'node.usb5v.v'];

export function createPanel(ctx) {
  const el = h('section', { class: 'screen panel' });
  const D = {};
  let runId = null, meta = null, sig = {}, dur = 0, tNow = 0, playing = false, lastFrame = 0;
  let main3d = null, angleChart = null, forceChart = null;
  let pendingJob = null, selected = null;

  el.append(
    h('header', { class: 'pn-head' },
      h('div', { class: 'pn-title' }, h('strong', {}, '开关机器人仿真'), D.sub = h('span', { class: 'muted' }, '')),
      h('div', { class: 'pn-tabs', role: 'tablist' },
        D.tabSim = h('button', { role: 'tab', class: 'active', onclick: () => setTab('sim') }, '仿真'),
        D.tabPcb = h('button', { role: 'tab', onclick: () => setTab('pcb') }, 'PCB 元件')),
      D.tests = h('div', { class: 'pn-tests', role: 'group', 'aria-label': '选择测试' }),
      h('div', { class: 'pn-tools' },
        h('button', { class: 'btn ghost', onclick: () => ctx.setAdvanced(true), title: '参数表、扫参、打印、设置' }, '高级功能'))),
    D.pcb = h('div', { class: 'pn-pcb', hidden: true }),
    D.sim = h('div', { class: 'pn-sim' },
    D.banner = h('div', { class: 'pn-banner', hidden: true }),
    h('div', { class: 'pn-body' },
      D.view = h('div', { class: 'pn-view' }, h('span', { class: 'pn-tag' }, '打印件、PCB 与外壳同一场景')),
      D.readout = h('div', { class: 'pn-readout' })),
    h('div', { class: 'pn-play' },
      D.playBtn = h('button', { class: 'btn icon', 'aria-label': '播放', onclick: () => togglePlay() }, '▶'),
      D.slider = h('input', { type: 'range', min: 0, max: 1000, value: 0, 'aria-label': '时间' }),
      D.time = h('span', { class: 'mono pn-time' }, '0.00 s')),
    h('div', { class: 'pn-charts' },
      h('div', { class: 'pn-chart' }, h('div', { class: 'pn-legend' },
        h('span', { style: { color: 'var(--s2)' } }, '按压臂角度'), h('span', { style: { color: 'var(--s1)' } }, '翘板角度'),
        h('span', { class: 'muted' }, '角度 / °')), D.angleCv = h('canvas')),
      h('div', { class: 'pn-chart' }, h('div', { class: 'pn-legend' },
        h('span', { style: { color: 'var(--s3)' } }, '按压接触力'), D.limitLab = h('span', { style: { color: 'var(--bad)' } }, ''),
        h('span', { class: 'muted' }, '力 / N')), D.forceCv = h('canvas')))),
    h('footer', { class: 'pn-foot muted' }, '电机、电源、开关参数是估计值，没有用实物测过；结果用来比较方案，不当作精确数值。'),
  );
  D.slider.addEventListener('input', () => { stop(); setTime(D.slider.value / 1000 * dur); });

  // ------------------------------------------------------------ tests row
  function scenarios() {
    const info = store.project?.scenario_info || {};
    return Object.entries(info).map(([name, s]) => ({ name, ...s }))
      .sort((a, b) => (a.needs_qemu - b.needs_qemu) || (b.name.includes('toggle') - a.name.includes('toggle'))
        || a.title.localeCompare(b.title, 'zh'));
  }

  function latestRun(name) { return store.runs.find((r) => r.scenario === name && !String(r.run_id).includes('-sweep')); }

  function renderTests() {
    const qemu = !!store.app?.qemu?.found;
    clear(D.tests);
    for (const s of scenarios()) {
      const blocked = s.needs_qemu && !qemu;
      const busy = pendingJob && pendingJob.scenario === s.name;
      const b = h('button', {
        class: 'pn-test' + (selected === s.name ? ' active' : '') + (blocked ? ' blocked' : ''),
        title: blocked ? '这个测试要跑真实固件，需要安装版里带的 QEMU。' : (s.description || s.title),
        onclick: () => pick(s.name, blocked),
      }, s.title, busy ? h('span', { class: 'pn-pct' }, `${Math.round((busy.progress || 0) * 100)}%`) : null);
      D.tests.append(b);
    }
    if (selected) {
      const r = latestRun(selected);
      D.tests.append(h('button', { class: 'btn ghost sm', disabled: !!pendingJob, onclick: () => run(selected), title: '用当前参数重新跑一次' },
        r ? '重新运行' : '运行'));
    }
  }

  async function pick(name, blocked) {
    if (blocked) { toast('这个测试要跑真实固件，需要安装版里带的 QEMU。先试试标着“快速”的测试。', { timeout: 6000 }); return; }
    selected = name;
    renderTests();
    const r = latestRun(name);
    if (r) await openRun(r.run_id); else await run(name);
  }

  async function run(name) {
    if (pendingJob) return;
    try {
      const job = await post('jobs/run', { scenario: name });
      store.jobs.set(job.id, job);
      pendingJob = { id: job.id, scenario: name, progress: 0 };
      setBanner('busy', `正在仿真：${store.project.scenario_info[name]?.title || name}…`);
      renderTests();
    } catch (e) { toastError(e); }
  }

  store.on('job_progress', (d) => { if (pendingJob && d.id === pendingJob.id) { pendingJob.progress = d.progress; renderTests(); } });
  store.on('job', (j) => {
    if (!pendingJob || j.id !== pendingJob.id) return;
    if (j.status === 'done' && j.result?.run_id) { pendingJob = null; store.loadRuns().catch(() => {}); openRun(j.result.run_id); }
    else if (j.status === 'failed') { pendingJob = null; setBanner('bad', `仿真失败：${j.error}`); }
    else if (j.status === 'cancelled') { pendingJob = null; setBanner('', ''); }
    renderTests();
  });
  store.on('project', () => { if (!el.hidden) renderTests(); });
  store.on('runs', () => { if (!el.hidden) renderTests(); });

  // ------------------------------------------------------------ load a run
  async function openRun(id) {
    stop();
    runId = id;
    try {
      meta = await get(`run/${id}`);
      const want = SIGS.filter((n) => (meta.signals || []).includes(n));
      sig = await get(`run/${id}/signals?names=${encodeURIComponent(want.join(','))}&n=4000`);
    } catch (e) { setBanner('bad', `打不开结果：${e.message}`); return; }
    if (runId !== id) return;
    dur = sig.t?.length ? sig.t[sig.t.length - 1] : (meta.timing?.sim_s || 0);
    summarize();
    drawCharts();
    await ensure3d();
    const loads = [];
    if (main3d?.ok) loads.push(main3d.load(id));
    await Promise.allSettled(loads);
    setTime(0);
    play();
  }

  function summarize() {
    const checks = meta.checks || [];
    const failed = checks.filter((c) => !c.pass);
    const name = typeof meta.scenario === 'string' ? meta.scenario : (selected || '');
    const title = store.project?.scenario_info?.[name]?.title || meta.scenario?.title || name;
    D.sub.textContent = title;
    if (!checks.length) setBanner('', '');
    else if (!failed.length) setBanner('ok', `测试通过：${checks.map((c) => c.note).filter(Boolean).join('，') || '全部检查都满足'}`);
    else setBanner('bad', `没通过：${failed.map(explain).join('；')}`);
  }

  function explain(c) {
    const v = typeof c.value === 'number' ? fmtNum(c.value, 3) : c.value;
    if (c.check?.startsWith('probe.press.force_n')) return `按压力峰值 ${v} N，超过 ${c.target} N 的限值`;
    if (c.check === 'mcu.resets') return `单片机重启了 ${v} 次（供电被拉低）`;
    if (c.note) return `${c.note}（实际 ${v}）`;
    return `${c.check} = ${v}`;
  }

  function setBanner(kind, text) {
    D.banner.hidden = !text;
    D.banner.className = 'pn-banner ' + kind;
    D.banner.textContent = text;
  }

  // ------------------------------------------------------------ charts
  function forceLimit() {
    const c = (meta?.checks || []).find((x) => x.check?.startsWith('probe.press.force_n') && x.op === 'lt');
    return c ? c.target : null;
  }

  function drawCharts() {
    angleChart ||= new TimeChart(D.angleCv, { onSeek: (t) => { stop(); setTime(t); }, unit: '°' });
    forceChart ||= new TimeChart(D.forceCv, { onSeek: (t) => { stop(); setTime(t); }, unit: 'N' });
    const t = sig.t || [];
    const xr = dur ? [0, dur] : null;
    angleChart.opts.xRange = xr; forceChart.opts.xRange = xr;
    angleChart.setSeries([
      { t, y: sig['joint.servo_axis.deg'] || [], color: cssVar('--s2'), label: '按压臂', width: 2 },
      { t, y: sig['joint.rocker_axis.deg'] || [], color: cssVar('--s1'), label: '翘板', width: 2 },
    ]);
    const lim = forceLimit();
    const fs = [{ t, y: sig['probe.press.force_n'] || [], color: cssVar('--s3'), label: '接触力', width: 1.5 }];
    if (lim !== null && dur) fs.push({ t: [0, dur], y: [lim, lim], color: cssVar('--bad'), dash: [5, 4], label: '限值', width: 1 });
    forceChart.setSeries(fs);
    D.limitLab.textContent = lim !== null ? `${lim} N 测试限值` : '';
  }

  // ------------------------------------------------------------ 3D
  async function ensure3d() {
    if (main3d) return;
    const { Replay3D } = await import('../replay3d.js');
    main3d = new Replay3D(D.view);
    if (!main3d.ok) D.view.append(h('div', { class: 'empty' }, main3d.error));
  }

  // ------------------------------------------------------------ time + readout
  function at(name, t) {
    const ts = sig.t, ys = sig[name];
    if (!ts?.length || !ys) return null;
    let lo = 0, hi = ts.length - 1;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (ts[m] <= t) lo = m; else hi = m - 1; }
    return ys[lo];
  }

  function setTime(t) {
    tNow = Math.max(0, Math.min(t, dur || 0));
    main3d?.setTime(tNow);
    angleChart?.setCursor(tNow); forceChart?.setCursor(tNow);
    D.slider.value = dur ? Math.round(tNow / dur * 1000) : 0;
    D.time.textContent = `${tNow.toFixed(2)} s / ${dur.toFixed(2)} s`;
    renderReadout();
  }

  function renderReadout() {
    if (!runId) { clear(D.readout).append(h('div', { class: 'muted' }, '在上面选一个测试，会自动开始仿真并回放。')); return; }
    const rocker = at('joint.rocker_axis.deg', tNow), arm = at('joint.servo_axis.deg', tNow);
    const f = at('probe.press.force_n', tNow), tq = at('act.servo1.torque', tNow), tgt = at('act.servo1.target_deg', tNow);
    const v5 = sig['node.usb5v.v'] ? at('node.usb5v.v', tNow) : at('node.vin5.v', tNow);
    const rail = at('mcu.rail_v', tNow), powered = at('mcu.powered', tNow), resets = at('mcu.resets', tNow);
    const on = rocker !== null && rocker < 0;
    const lim = forceLimit();
    const n = (v, d = 2) => (v === null || v === undefined ? '–' : (v >= 0 ? '+' : '') + v.toFixed(d));
    clear(D.readout).append(
      h('div', { class: 'pn-state ' + (on ? 'on' : 'off') }, on ? '状态 开 ON' : '状态 关 OFF', h('span', { class: 'mono' }, `t=${tNow.toFixed(2)} s`)),
      h('div', { class: 'pn-kv' },
        kv('按压臂', `${n(arm, 1)}°`), kv('翘板', `${n(rocker, 2)}°`),
        kv('接触力', `${f === null ? '–' : f.toFixed(2)} N`, lim !== null && f > lim ? 'bad' : ''),
        kv('舵机力矩', `${n(tq, 3)} N·m`),
        kv('动作', tgt === null ? '–' : Math.abs(tgt) > 1 ? '按压' : '退回'),
        v5 !== null ? kv('5V 电源', `${v5.toFixed(2)} V`, v5 < 4.3 ? 'bad' : '') : null,
        rail !== null ? kv('单片机供电', `${rail.toFixed(2)} V`, powered === 0 ? 'bad' : '') : null),
      powered === 0 ? h('div', { class: 'pn-warn' }, '单片机掉电了：舵机电流把供电电压拉了下去。') :
        resets > 0 ? h('div', { class: 'pn-warn' }, `单片机已经重启 ${resets} 次。`) : null,
    );
  }
  const kv = (k, v, cls = '') => h('div', { class: 'kv ' + cls }, h('span', { class: 'k' }, k), h('span', { class: 'v mono' }, v));

  function play() {
    if (!dur) return;
    if (tNow >= dur - 1e-6) setTime(0);
    playing = true; D.playBtn.textContent = '❚❚'; D.playBtn.setAttribute('aria-label', '暂停');
    lastFrame = performance.now();
    const step = (now) => {
      if (!playing) return;
      const dt = (now - lastFrame) / 1000; lastFrame = now;
      setTime(tNow + dt * 0.5);                 // half speed: the press takes ~0.3 s
      if (tNow >= dur) { stop(); return; }
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  function stop() { playing = false; D.playBtn.textContent = '▶'; D.playBtn.setAttribute('aria-label', '播放'); }
  function togglePlay() { playing ? stop() : play(); }

  // ------------------------------------------------------------ PCB parts page
  let tab = 'sim', boards = null, boardImg = 'board';
  function setTab(t) {
    tab = t;
    D.tabSim.classList.toggle('active', t === 'sim'); D.tabPcb.classList.toggle('active', t === 'pcb');
    D.sim.hidden = t !== 'sim'; D.tests.hidden = t !== 'sim'; D.pcb.hidden = t !== 'pcb';
    if (t === 'pcb') { stop(); renderPcb(); } else main3d?.resize();
  }

  function openShop(url) {
    if (window.pywebview?.api?.open_external) window.pywebview.api.open_external(url);
    else window.open(url, '_blank', 'noopener');
  }
  const shopUrl = (c) => `https://so.szlcsc.com/global.html?k=${encodeURIComponent(c)}`;

  async function renderPcb() {
    if (!boards) {
      clear(D.pcb).append(h('div', { class: 'muted' }, '读取中…'));
      try { boards = await get('boards'); } catch (e) { clear(D.pcb).append(h('div', { class: 'pn-banner bad' }, e.message)); return; }
    }
    const b = boards[0];
    if (!b) { clear(D.pcb).append(h('div', { class: 'pn-banner' }, '这个工程还没有元件清单（pcb/<板名>/parts.json）。')); return; }
    const total = b.parts.reduce((n, p) => n + (p.qty || 0), 0);
    const img = b.images?.[boardImg];
    clear(D.pcb).append(
      h('div', { class: 'pn-pcb-head' },
        h('div', {}, h('strong', {}, b.board), h('span', { class: 'muted' }, `  ${b.parts.length} 种元件，共 ${total} 个`)),
        h('div', { class: 'pn-tabs small' },
          ...['board', 'schematic'].filter((k) => b.images?.[k]).map((k) => h('button', {
            class: boardImg === k ? 'active' : '', onclick: () => { boardImg = k; renderPcb(); },
          }, k === 'board' ? 'PCB 图' : '原理图')))),
      h('div', { class: 'pn-pcb-body' },
        h('div', { class: 'pn-pcb-img' }, img ? h('img', { src: authUrl(`boards/${b.id}/${img}`), alt: boardImg === 'board' ? 'PCB 图' : '原理图' }) : null),
        h('div', { class: 'pn-pcb-table' },
          h('table', { class: 'pn-parts' },
            h('thead', {}, h('tr', {}, ['位号', '元件', '用途', '数量', '焊接', '立创料号'].map((x) => h('th', {}, x)))),
            h('tbody', {}, b.parts.map((p) => h('tr', {},
              h('td', { class: 'mono' }, p.refs.join(', ')),
              h('td', {}, h('div', {}, p.name), h('div', { class: 'muted small' }, p.manufacturer || '')),
              h('td', {}, p.role || ''),
              h('td', { class: 'num' }, String(p.qty)),
              h('td', {}, p.mount === 'THT' ? '插件' : '贴片', h('div', { class: 'muted small' }, p.jlc_library || '')),
              h('td', {}, h('a', { href: shopUrl(p.lcsc), class: 'mono', title: `在立创商城打开 ${p.lcsc}`,
                onclick: (e) => { e.preventDefault(); openShop(shopUrl(p.lcsc)); } }, p.lcsc, ' ↗'))))))),
      ),
      h('div', { class: 'muted small' }, `料号和库存是 ${b.checked_at} 在嘉立创 EDA 和立创商城核对的；点料号会打开立创商城对应的商品。整板也可以直接把 Gerber、BOM、坐标文件上传到嘉立创下单贴片。`),
    );
  }

  // ------------------------------------------------------------ lifecycle
  async function ensureProject() {
    if (store.app?.project) return true;
    const ex = store.app?.examples?.[0];
    if (!ex) return false;
    try {
      await post('project/open_example', { path: ex });
      await store.loadApp(); await store.loadProject(); await store.loadRuns();
      return true;
    } catch (e) { toastError(e); return false; }
  }

  return {
    el,
    async show() {
      if (!(await ensureProject())) { setBanner('bad', '没有找到示例工程，请在“高级功能”里打开一个工程。'); return; }
      renderTests();
      if (!selected) {
        const first = scenarios().find((s) => !s.needs_qemu || store.app?.qemu?.found);
        if (first) pick(first.name, false);
      } else renderReadout();
      main3d?.resize();
    },
    hide() { stop(); },
    openRun,
  };
}
