// The simple panel: pick a test, it runs, and you watch it. One screen, no setup.
// 3D view + live readout + two charts (angles, contact force).
import { get, post, authUrl } from '../api.js';
import { store } from '../store.js';
import { h, icon, clear, toast, toastError, fmtNum, cssVar, modal, copyText, pathField, nativePick } from '../ui.js';
import { TimeChart } from '../chart.js';

const SIGS = ['source.charger.i', 'branch.f1.i', 'node.v5_servo.v', 'act.servo1.i_supply', 'act.servo1.pulse_us',
  'joint.servo_axis.deg', 'joint.rocker_axis.deg', 'probe.press.force_n', 'act.servo1.torque',
  'act.servo1.target_deg', 'mcu.rail_v', 'mcu.powered', 'mcu.resets', 'node.vin5.v', 'node.usb5v.v'];

export function createPanel(ctx) {
  const el = h('section', { class: 'screen panel' });
  const D = {};
  let runId = null, meta = null, sig = {}, dur = 0, tNow = 0, playing = false, lastFrame = 0;
  let main3d = null, angleChart = null, forceChart = null;
  let pendingJob = null, selected = null;

  // layout: left nav (collapsible) | centre (3D view or PCB page) | right sidebar (tests, tools, live state)
  //         with the play bar and the two charts fixed under the centre.
  let navCollapsed = false;
  try { navCollapsed = localStorage.getItem('emcosim.panel.nav') === 'collapsed'; } catch { /* storage off */ }
  const navItem = (id, ic, label) => h('button', { class: 'pn-nav-item', 'data-tab': id, title: label, onclick: () => setTab(id) },
    icon(ic), h('span', { class: 'pn-nav-label' }, label));
  el.append(
    D.nav = h('nav', { class: 'pn-nav', 'aria-label': '页面' },
      D.navToggle = h('button', { class: 'pn-nav-toggle', onclick: () => setNav(!navCollapsed) }, icon('chevron')),
      h('div', { class: 'pn-nav-brand' }, h('span', { class: 'pn-logo' }, icon('logo')),
        h('span', { class: 'pn-nav-label' }, h('strong', {}, 'SimWorld'), h('span', { class: 'muted small' }, '电子 + 物理仿真'))),
      D.tabSim = navItem('sim', 'cube', '仿真'),
      D.tabPcb = navItem('pcb', 'board', 'PCB 元件'),
      D.projBox = h('div', { class: 'pn-proj' })),
    h('div', { class: 'pn-main' },
      D.welcome = h('div', { class: 'pn-welcome', hidden: true }),
      D.banner = h('div', { class: 'pn-banner', hidden: true }),
      D.sim = h('div', { class: 'pn-sim' },
        D.view = h('div', { class: 'pn-view' }, h('span', { class: 'pn-tag' }, '打印件、PCB 与外壳同一场景'),
          D.viewEmpty = h('div', { class: 'pn-empty-over' }, icon('cube'), h('div', {}, '还没有运行结果'),
            h('div', { class: 'muted small' }, '在右边选一个测试，跑完会在这里回放 3D 动画')))),
      D.pcb = h('div', { class: 'pn-pcb', hidden: true },
        h('div', { class: 'pn-circuit-wrap' },
          h('div', { class: 'pn-legend' }, h('strong', {}, '电路原理（跟着仿真动）'),
            h('span', { class: 'muted' }, '线越粗电流越大，变红表示电压掉得太低')),
          D.circuit = h('div', { class: 'pn-circuit' })),
        D.check = h('div', { class: 'pn-check' }),
        D.parts = h('div', { class: 'pn-parts-wrap' }))),
    h('div', { class: 'pn-bottom' },
      D.play = h('div', { class: 'pn-play' },
        D.playBtn = h('button', { class: 'btn icon', 'aria-label': '播放', onclick: () => togglePlay() }, '▶'),
        D.slider = h('input', { type: 'range', min: 0, max: 1000, value: 0, 'aria-label': '时间' }),
        D.time = h('span', { class: 'mono pn-time' }, '0.00 s')),
      h('div', { class: 'pn-charts' },
        h('div', { class: 'pn-chart' }, h('div', { class: 'pn-legend' },
          h('span', { style: { color: 'var(--s2)' } }, '按压臂角度'), h('span', { style: { color: 'var(--s1)' } }, '翘板角度'),
          h('span', { class: 'muted' }, '角度 / °')), D.angleCv = h('canvas'), h('div', { class: 'pn-chart-empty' }, '运行测试后显示角度曲线')),
        h('div', { class: 'pn-chart' }, h('div', { class: 'pn-legend' },
          h('span', { style: { color: 'var(--s3)' } }, '按压接触力'), D.limitLab = h('span', { style: { color: 'var(--bad)' } }, ''),
          h('span', { class: 'muted' }, '力 / N')), D.forceCv = h('canvas'), h('div', { class: 'pn-chart-empty' }, '运行测试后显示接触力曲线')))),
    h('aside', { class: 'pn-side', 'aria-label': '测试和状态' },
      h('div', { class: 'pn-side-sec' },
        h('div', { class: 'pn-side-title' }, '测试'),
        D.tests = h('div', { class: 'pn-tests', role: 'group', 'aria-label': '选择测试' })),
      h('div', { class: 'pn-side-sec' },
        h('div', { class: 'pn-side-title' }, '状态'),
        D.readout = h('div', { class: 'pn-readout' })),
      h('div', { class: 'pn-side-sec pn-tools' },
        h('button', { class: 'btn', onclick: () => showAiSetup(), title: '让你的 Claude Code 或 Codex 来改模型、改板子、跑测试' }, icon('link'), '连接 AI'),
        h('button', { class: 'btn', onclick: () => ctx.setAdvanced(true), title: '参数表、扫参、打印、设置' }, icon('sliders'), '高级功能')),
      h('div', { class: 'pn-foot muted' }, '电机、电源、开关参数是估计值，没有用实物测过；结果用来比较方案，不当作精确数值。')),
  );
  function setNav(collapsed) {
    navCollapsed = collapsed;
    el.classList.toggle('nav-collapsed', collapsed);
    D.navToggle.title = collapsed ? '展开菜单' : '收起菜单';
    D.navToggle.setAttribute('aria-label', D.navToggle.title);
    try { localStorage.setItem('emcosim.panel.nav', collapsed ? 'collapsed' : 'open'); } catch { /* storage off */ }
  }
  setNav(navCollapsed);
  D.tabSim.classList.add('active');
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
    if (!store.app?.project) { D.tests.append(h('div', { class: 'pn-side-empty' }, '打开工程后，这里会列出它的测试。')); return; }
    if (!scenarios().length) { D.tests.append(h('div', { class: 'pn-side-empty' }, '这个工程还没有测试。可以点下面的“连接 AI”，让你的 AI 帮你加。')); return; }
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
      D.tests.append(h('button', { class: 'btn', disabled: !!pendingJob, onclick: () => run(selected), title: '用当前参数重新跑一次' },
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
    if (j.status === 'done' && j.result?.run_id) { pendingJob = null; knownRuns?.add(j.result.run_id); store.loadRuns().catch(() => {}); openRun(j.result.run_id); }
    else if (j.status === 'failed') { pendingJob = null; setBanner('bad', `仿真失败：${j.error}`); }
    else if (j.status === 'cancelled') { pendingJob = null; setBanner('', ''); }
    renderTests();
  });
  store.on('project', () => { if (!el.hidden) { renderTests(); renderProjBox(); } });
  let knownRuns = null;
  store.on('runs', () => {
    const ids = store.runs.map((r) => r.run_id);
    if (knownRuns && !pendingJob) {
      // a run this page did not start: the AI ran it through MCP. Show it.
      const fresh = store.runs.find((r) => !knownRuns.has(r.run_id) && !String(r.run_id).includes('-sweep'));
      if (fresh && !el.hidden) {
        selected = fresh.scenario;
        openRun(fresh.run_id).then(() => {
          const title = store.project?.scenario_info?.[fresh.scenario]?.title || fresh.scenario;
          if (D.banner.className.includes('bad') || D.banner.className.includes('ok')) {
            D.banner.textContent = `AI 刚跑了一次「${title}」。` + D.banner.textContent;
          }
        });
      }
    }
    knownRuns = new Set(ids);
    if (!el.hidden) renderTests();
  });
  store.on('files_changed', () => {
    boardCheck = null;
    if (tab === 'pcb') renderCheck();
    toast('模型或板子文件刚被改过（可能是 AI 改的）。重新运行测试就能看到效果。', { timeout: 6000 });
  });

  // ------------------------------------------------------------ connect an AI agent (MCP)
  async function showAiSetup() {
    let s;
    try { s = await get('ai_setup'); } catch (e) { toastError(e); return; }
    const block = (title, text, hint) => h('div', { class: 'pn-ai-block' },
      h('div', { class: 'pn-ai-title' }, h('strong', {}, title),
        h('button', { class: 'btn sm', type: 'button', onclick: () => copyText(text) }, '复制')),
      h('pre', { class: 'mono' }, text), hint ? h('div', { class: 'muted small' }, hint) : null);
    modal({
      title: '连接你的 AI（Claude Code 或 Codex）',
      width: '720px',
      body: h('div', { class: 'pn-ai' },
        h('p', {}, '复制下面这段话，发给你电脑上的 Claude Code 或 Codex，它会自己配置好并连上。',
          '连上以后，它可以改 3D 模型、改 PCB、跑 KiCad 检查和仿真测试、查嘉立创元件，跑的每次测试都会出现在这个页面上。'),
        h('div', { class: 'pn-ai-block' },
          h('pre', { class: 'pn-ai-prompt' }, s.prompt),
          h('button', { class: 'btn primary', type: 'button', onclick: () => copyText(s.prompt, '已复制，粘贴发给你的 AI 就行') }, '复制这段话')),
        h('details', { class: 'pn-ai-manual' }, h('summary', {}, '自己手动配置'),
          block('Claude Code：在终端里运行一次', s.claude_code),
          block('Codex：把这段加到配置文件里', s.codex, `配置文件位置：${s.codex_config}`)),
        h('p', { class: 'muted small' }, '这个页面不保存你的 AI 账号，AI 用的是你自己电脑上的 Claude Code 或 Codex。')),
      actions: [{ label: '关闭', value: true }],
    });
  }

  // ------------------------------------------------------------ KiCad board check
  let boardCheck = null, checking = false;
  async function runCheck() {
    checking = true; renderCheck();
    try { boardCheck = await post('board/check', {}); }
    catch (e) { boardCheck = { failed: e.message }; }
    checking = false; renderCheck();
  }
  function renderCheck() {
    const has = !!store.project?.data?.board?.kicad_pcb;
    clear(D.check);
    if (!has) return;
    const head = h('div', { class: 'pn-check-head' },
      h('strong', {}, '板子检查（KiCad DRC，按嘉立创工艺）'),
      h('button', { class: 'btn sm', disabled: checking, onclick: runCheck }, checking ? '检查中…' : boardCheck ? '重新检查' : '检查'));
    D.check.append(head);
    if (!boardCheck) return;
    if (boardCheck.failed) { D.check.append(h('div', { class: 'pn-banner bad' }, boardCheck.failed)); return; }
    const c = boardCheck;
    D.check.append(h('div', { class: 'pn-banner ' + (c.errors ? 'bad' : 'ok') },
      c.errors ? `${c.errors} 个错误，${c.warnings} 个提醒，${c.unconnected} 处没连上` :
        `没有错误（${c.warnings} 个提醒），${c.unconnected ? c.unconnected + ' 处没连上' : '所有网络都连上了'}`));
    const rows = c.items.filter((i) => i.severity === 'error').concat(c.items.filter((i) => i.severity !== 'error')).slice(0, 8);
    if (rows.length) {
      D.check.append(h('ul', { class: 'pn-check-list' }, rows.map((i) => h('li', { class: i.severity },
        h('span', { class: 'sev' }, i.severity === 'error' ? '错误' : '提醒'), ' ', i.description,
        i.where?.length ? h('span', { class: 'muted small' }, '  ' + i.where.map((w) => w.item).join('；')) : null))));
    }
  }

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
    if (tab === 'pcb') renderCircuit();
  }

  // ------------------------------------------------------------ live circuit
  function renderCircuit() {
    if (!D.circuit) return;
    if (!runId) { D.circuit.innerHTML = '<div class="pn-side-empty">先在右边选一个测试，跑完这里的电路会跟着仿真动。</div>'; return; }
    const bench = !!sig['node.usb5v.v'];
    const v = (n) => at(n, tNow);
    const vin = bench ? v('node.usb5v.v') : v('node.vin5.v'), vs = bench ? vin : v('node.v5_servo.v');
    const v33 = v('mcu.rail_v'), iIn = bench ? null : v('source.charger.i'), iServo = v('act.servo1.i_supply');
    const pulse = v('act.servo1.pulse_us'), powered = v('mcu.powered'), resets = v('mcu.resets') || 0;
    const arm = v('joint.servo_axis.deg') || 0, rocker = v('joint.rocker_axis.deg') || 0, f = v('probe.press.force_n') || 0;
    const ok = cssVar('--ok'), bad = cssVar('--bad'), ink = cssVar('--text'), dim = cssVar('--text-3'), line = cssVar('--line-strong');
    const wire = (volt, lowAt, amps) => `stroke="${volt !== null && volt < lowAt ? bad : ink}" stroke-width="${(1.5 + Math.min(Math.abs(amps || 0), 1.5) * 5).toFixed(1)}"`;
    const fmt = (x, d, u) => (x === null || x === undefined ? '–' : x.toFixed(d) + ' ' + u);
    const box = (x, y, w, hgt, title, sub, color = line) => `<rect x="${x}" y="${y}" width="${w}" height="${hgt}" rx="6" fill="var(--panel)" stroke="${color}" stroke-width="1.5"/>`
      + `<text x="${x + w / 2}" y="${y + 20}" text-anchor="middle" font-size="13" font-weight="600" fill="${ink}">${title}</text>`
      + `<text x="${x + w / 2}" y="${y + 37}" text-anchor="middle" font-size="11" fill="${dim}">${sub}</text>`;
    const val = (x, y, text, color = ink, anchor = 'middle') => `<text x="${x}" y="${y}" text-anchor="${anchor}" font-size="12" font-family="ui-monospace,monospace" fill="${color}">${text}</text>`;
    const mcuOk = powered !== 0;
    const on = rocker < 0;
    // servo horn and rocker drawn rotating with the simulated angles
    const horn = `<g transform="translate(905,58) rotate(${(-arm * 2).toFixed(1)})"><rect x="-4" y="-34" width="8" height="34" rx="3" fill="${cssVar('--s2')}"/></g>`;
    const rock = `<g transform="translate(905,165) rotate(${(rocker * 6).toFixed(1)})"><rect x="-30" y="-6" width="60" height="12" rx="4" fill="${on ? ok : cssVar('--s1')}"/></g>`;
    D.circuit.innerHTML = `<svg viewBox="0 0 1000 250" width="100%" role="img" aria-label="电路原理图">
      ${bench ? `<text x="10" y="245" font-size="11" fill="${bad}">这个测试模拟的是旧的开发板接法（舵机接在开发板 5V 上），不是 V0.5 板；图按 V0.5 画，数值仅供对照。</text>` : ''}
      ${box(10, 20, 110, 50, '充电头', '墙上插座 5V 2A')}
      <line x1="120" y1="45" x2="200" y2="45" ${wire(vin, 4.3, iIn)}/>${val(160, 36, 'USB-C 线', dim)}
      ${box(200, 20, 90, 50, 'D1 SS34', '防倒灌二极管')}
      <line x1="290" y1="45" x2="330" y2="45" ${wire(vin, 4.3, iIn)}/>
      <circle cx="335" cy="45" r="5" fill="${ink}"/>${val(325, 95, 'VIN5 ' + fmt(vin, 2, 'V'), vin !== null && vin < 4.3 ? bad : ink, 'end')}
      ${val(325, 112, iIn === null ? '' : '总电流 ' + fmt(iIn, 2, 'A'), dim, 'end')}
      <line x1="340" y1="45" x2="400" y2="45" ${wire(vs, 4.3, iServo)}/>
      ${box(400, 20, 100, 50, 'F1 保险丝', 'RUEF110 1.1A')}
      <line x1="500" y1="45" x2="620" y2="45" ${wire(vs, 4.3, iServo)}/>${val(560, 36, fmt(vs, 2, 'V'), vs !== null && vs < 4.3 ? bad : ink)}
      ${box(620, 20, 120, 50, 'J3 → 舵机', '电流 ' + fmt(iServo, 2, 'A'), iServo > 0.6 ? bad : line)}
      <line x1="740" y1="45" x2="860" y2="45" stroke="${dim}" stroke-dasharray="4 4"/>${val(800, 36, '齿轮 / 按压臂', dim)}
      <circle cx="905" cy="58" r="16" fill="${cssVar('--s1')}" opacity=".35"/>${horn}
      ${val(905, 100, '臂角 ' + arm.toFixed(1) + '°', dim)}
      <line x1="905" y1="108" x2="905" y2="150" stroke="${dim}" stroke-dasharray="4 4"/>${val(950, 135, '接触力 ' + f.toFixed(2) + ' N', f > 6 ? bad : dim)}
      ${rock}${val(905, 195, on ? '开关：开 ON' : '开关：关 OFF', on ? ok : cssVar('--s1'))}
      <line x1="335" y1="50" x2="335" y2="160" ${wire(vin, 4.3, 0.05)}/>
      <line x1="335" y1="160" x2="360" y2="160" ${wire(vin, 4.3, 0.05)}/>
      ${box(360, 135, 120, 50, 'U4 AMS1117', '5V → 3.3V')}
      <line x1="480" y1="160" x2="540" y2="160" ${wire(v33, 3.0, 0.05)}/>${val(510, 151, fmt(v33, 2, 'V'), v33 !== null && v33 < 3.0 ? bad : ink)}
      ${box(540, 135, 140, 50, 'U3 ESP32-C3', mcuOk ? (resets ? `运行中 · 已重启 ${resets} 次` : '运行中') : '掉电了', mcuOk ? (resets ? bad : ok) : bad)}
      <path d="M680 160 H690 Q700 160 700 150 V70" fill="none" stroke="${cssVar('--s7')}" stroke-width="2"/>
      ${val(760, 125, 'GPIO4 PWM ' + (pulse ? Math.round(pulse) + ' µs' : '–'), cssVar('--s7'))}
    </svg>`;
  }

  function renderReadout() {
    el.classList.toggle('no-run', !runId);
    if (!runId) {
      clear(D.readout).append(h('div', { class: 'pn-side-empty' }, store.app?.project ? '选一个测试，跑完这里会显示开关状态、角度、受力和供电电压。' : '打开工程并运行测试后，这里显示实时状态。'));
      return;
    }
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
    if (!store.app?.project) { D.tabSim.classList.toggle('active', t === 'sim'); D.tabPcb.classList.toggle('active', t === 'pcb'); return; }
    D.tabSim.classList.toggle('active', t === 'sim'); D.tabPcb.classList.toggle('active', t === 'pcb');
    D.sim.hidden = t !== 'sim'; D.pcb.hidden = t !== 'pcb';
    if (t === 'pcb') { renderPcb(); renderCircuit(); renderCheck(); }
    else main3d?.resize();
  }

  function openShop(url) {
    if (window.pywebview?.api?.open_external) window.pywebview.api.open_external(url);
    else window.open(url, '_blank', 'noopener');
  }
  const shopUrl = (c) => `https://so.szlcsc.com/global.html?k=${encodeURIComponent(c)}`;

  async function renderPcb() {
    if (!boards) {
      clear(D.parts).append(h('div', { class: 'muted' }, '读取中…'));
      try { boards = await get('boards'); } catch (e) { clear(D.parts).append(h('div', { class: 'pn-banner bad' }, e.message)); return; }
    }
    const b = boards[0];
    if (!b) { clear(D.parts).append(h('div', { class: 'pn-banner' }, '这个工程还没有元件清单（pcb/<板名>/parts.json）。')); return; }
    const total = b.parts.reduce((n, p) => n + (p.qty || 0), 0);
    const img = b.images?.[boardImg];
    clear(D.parts).append(
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
            h('thead', {}, h('tr', {}, ['位号', '元件', '用途', '数量', '焊接', '现在库存 / 单价', '立创料号'].map((x) => h('th', {}, x)))),
            h('tbody', {}, b.parts.map((p) => h('tr', {},
              h('td', { class: 'mono' }, p.refs.join(', ')),
              h('td', {}, h('div', {}, p.name), h('div', { class: 'muted small' }, p.manufacturer || '')),
              h('td', {}, p.role || ''),
              h('td', { class: 'num' }, String(p.qty)),
              h('td', {}, p.mount === 'THT' ? '插件' : '贴片', h('div', { class: 'muted small' }, p.jlc_library || '')),
              liveCell(p.lcsc),
              h('td', {}, h('a', { href: shopUrl(p.lcsc), class: 'mono', title: `在立创商城打开 ${p.lcsc}`,
                onclick: (e) => { e.preventDefault(); openShop(shopUrl(p.lcsc)); } }, p.lcsc, ' ↗'))))))),
      ),
      h('div', { class: 'muted small' }, `料号是 ${b.checked_at} 在嘉立创 EDA 和立创商城核对的；“现在库存”是打开这页时实时查的（数据来自开源的 PCB Parts MCP，MIT 许可）。点料号会打开立创商城对应的商品。整板也可以直接把 Gerber、BOM、坐标文件上传到嘉立创下单贴片。`),
    );
  }

  // live stock / price per LCSC number, fetched once per page load through the built-in parts lookup
  const live = new Map();
  function liveCell(code) {
    const td = h('td', { class: 'small' });
    const show = (r) => {
      clear(td);
      if (!r) td.append(h('span', { class: 'muted' }, '查询中…'));
      else if (r.failed) td.append(h('span', { class: 'muted', title: r.failed }, '查不到'));
      else {
        const price = r.prices?.[0]?.price ?? r.price;
        td.append(h('div', { class: r.stock > 0 ? '' : 'bad' }, r.stock > 0 ? `${r.stock.toLocaleString()} 个` : '缺货'),
          price != null ? h('div', { class: 'muted' }, `$${price} 起`) : null);
      }
    };
    if (!code) return td;
    if (!live.has(code)) {
      live.set(code, get(`part/${code}`).then((r) => r, (e) => ({ failed: e.message })));
    }
    show(null);
    live.get(code).then(show);
    return td;
  }

  // ------------------------------------------------------------ lifecycle
  // ------------------------------------------------------------ project: welcome page when none is open
  const baseName = (p) => String(p || '').split(/[\\/]/).filter(Boolean).pop() || '';
  function projectName() { return store.project?.data?.name || baseName(store.app?.project); }

  async function reloadProject() {
    store.seenRuns?.clear?.(); store.newRuns?.clear?.();
    await store.loadApp(); await store.loadProject(); await store.loadRuns();
    resetRun(); selected = null; boards = null; boardCheck = null;
    await showProject();
  }
  async function openProject(path, example = false) {
    try {
      const r = await post(example ? 'project/open_example' : 'project/open', { path });
      await reloadProject();
      if (r?.copied_to) toast(`示例已复制到 ${r.copied_to}，在副本上修改不会影响安装目录`, { timeout: 8000 });
    } catch (e) { toastError(e); }
  }
  async function openFolder() {
    const picked = await nativePick('folder');
    if (picked) return openProject(picked);
    const f = pathField({ label: '工程文件夹（里面有 project.yaml）', kind: 'folder', placeholder: 'D:\\robots\\my_robot' });
    const ok = await modal({ title: '打开工程', body: f.el, actions: [{ label: '取消', value: false },
      { label: '打开', kind: 'primary', handler: () => { if (!f.value) throw new Error('请填写路径'); return true; } }] });
    if (ok) openProject(f.value);
  }
  async function newProject() {
    const f = pathField({ label: '新工程放在哪个文件夹（不存在会自动创建）', kind: 'folder', placeholder: 'D:\\robots\\my_robot' });
    const name = h('input', { class: 'input', placeholder: '例如 my_robot' });
    const ok = await modal({ title: '新建工程',
      body: [f.el, h('label', { class: 'field' }, '工程名称', name),
        h('div', { class: 'hint' }, '新工程是空的。之后把 PCB 和 3D 模型交给你的 AI（点“连接 AI”），它会帮你导入、加测试。')],
      actions: [{ label: '取消', value: false }, { label: '创建', kind: 'primary', handler: async () => {
        if (!f.value) throw new Error('请填写文件夹');
        await post('project/new', { path: f.value, name: name.value.trim() || undefined });
        return true;
      } }] });
    if (ok) await reloadProject();
  }
  function resetRun() {
    stop(); runId = null; meta = null; sig = {}; dur = 0; tNow = 0;
    angleChart?.setSeries([]); forceChart?.setSeries([]); D.limitLab.textContent = '';
    main3d?.clear(); main3d?.render();
    setBanner('', ''); setTime(0);
  }

  function renderProjBox() {
    clear(D.projBox);
    const p = store.app?.project;
    if (!p) return;
    D.projBox.append(
      h('div', { class: 'pn-proj-label pn-nav-label' }, '当前工程'),
      h('button', { class: 'pn-nav-item pn-proj-btn', title: `${p}\n点这里打开别的工程`, onclick: openFolder },
        icon('folder'), h('span', { class: 'pn-nav-label pn-proj-name' }, projectName())));
  }

  function renderWelcome() {
    const app = store.app || {};
    const recent = (app.recent_projects || []).slice(0, 5);
    const ex = (app.examples || [])[0];
    const card = (ic, title, text, onclick, primary) => h('button', { class: 'pn-start' + (primary ? ' primary' : ''), onclick },
      h('span', { class: 'pn-start-ico' }, icon(ic)), h('span', { class: 'pn-start-txt' }, h('strong', {}, title), h('span', { class: 'muted small' }, text)));
    clear(D.welcome).append(h('div', { class: 'pn-welcome-in' },
      h('div', { class: 'pn-welcome-logo' }, icon('logo')),
      h('h1', {}, '欢迎使用 SimWorld'),
      h('p', { class: 'muted' }, '把 PCB 和 3D 模型放进来，在同一个场景里跑真实的电路和物理测试；再让你的 AI 改板子、改模型，直到测试通过。'),
      h('div', { class: 'pn-start-grid' },
        ex ? card('cube', '打开示例工程', '开关机器人：舵机按墙上开关，带 V0.5 控制板', () => openProject(ex, true), true) : null,
        card('folder', '打开工程', '选一个有 project.yaml 的文件夹', openFolder),
        card('plus', '新建空工程', '从零开始，让 AI 帮你导入 PCB 和模型', newProject)),
      recent.length ? h('div', { class: 'pn-recent' }, h('div', { class: 'pn-side-title' }, '最近打开'),
        recent.map((r) => h('button', { class: 'pn-recent-row', onclick: () => openProject(r) },
          icon('folder'), h('span', {}, baseName(r)), h('span', { class: 'muted small mono pn-recent-path' }, r)))) : null));
  }

  async function showProject() {
    const has = !!store.app?.project;
    el.classList.toggle('no-project', !has);
    D.welcome.hidden = has;
    D.sim.hidden = !has || tab !== 'sim'; D.pcb.hidden = !has || tab !== 'pcb';
    renderProjBox(); renderTests(); renderReadout();
    if (!has) { renderWelcome(); return; }
    if (tab === 'pcb') { renderPcb(); renderCircuit(); renderCheck(); }
    if (!selected) {
      const first = scenarios().find((s) => !s.needs_qemu || store.app?.qemu?.found);
      if (first) pick(first.name, false);
    }
    main3d?.resize();
  }

  return {
    el,
    async show() { await showProject(); },
    hide() { stop(); },
    openRun,
  };
}
