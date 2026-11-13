// The simple panel: pick a test, it runs, and you watch it. One screen, no setup.
// 3D view + live readout + two charts (angles, contact force).
import { get, post, upload, authUrl } from '../api.js';
import { store } from '../store.js';
import { h, icon, clear, toast, toastError, fmtNum, cssVar, modal, copyText, pathField, nativePick } from '../ui.js';
import { TimeChart } from '../chart.js';
import { powerCards, printCards, mechCards } from './pfields.js';

const SIGS = ['source.charger.i', 'branch.f1.i', 'node.v5_servo.v', 'act.servo1.i_supply', 'act.servo1.pulse_us',
  'joint.servo_axis.deg', 'joint.rocker_axis.deg', 'probe.press.force_n', 'act.servo1.torque',
  'act.servo1.target_deg', 'mcu.rail_v', 'mcu.powered', 'mcu.resets', 'node.vin5.v', 'node.usb5v.v'];

export function createPanel(ctx) {
  const el = h('section', { class: 'screen panel' });
  const D = {};
  let runId = null, meta = null, sig = {}, dur = 0, tNow = 0, playing = false, lastFrame = 0;
  let main3d = null, angleChart = null, forceChart = null;
  let pendingJob = null, selected = null;

  // layout: left nav (collapsible) | centre (3D view, PCB page or 建模 page) | right sidebar (tests, tools, live state)
  //         with the play bar and the two charts fixed under the centre.
  let navCollapsed = false;
  try { navCollapsed = localStorage.getItem('emcosim.panel.nav') === 'collapsed'; } catch { /* storage off */ }
  const navItemBtn = (ic, label, onclick) => h('button', { class: 'pn-nav-item', title: label, onclick }, icon(ic), h('span', { class: 'pn-nav-label' }, label));
  const navItem = (id, ic, label) => h('button', { class: 'pn-nav-item', 'data-tab': id, title: label, onclick: () => setTab(id) },
    icon(ic), h('span', { class: 'pn-nav-label' }, label));
  el.append(
    D.nav = h('nav', { class: 'pn-nav', 'aria-label': '页面' },
      D.navToggle = h('button', { class: 'pn-nav-toggle', onclick: () => setNav(!navCollapsed) }, icon('chevron')),
      h('div', { class: 'pn-nav-brand' }, h('strong', {}, 'SimWorld'), h('span', { class: 'muted small' }, '电子 + 物理仿真')),
      D.tabPcb = navItem('pcb', 'board', 'PCB 元件'),
      D.tabModel = navItem('model', 'shapes', '建模'),
      D.tabSim = navItem('sim', 'cube', '仿真'),
      D.projBox = h('div', { class: 'pn-proj' }),
      h('div', { class: 'pn-nav-bottom' }, navItemBtn('settings', '设置', () => showSettings()))),
    h('div', { class: 'pn-main' },
      D.welcome = h('div', { class: 'pn-welcome', hidden: true }),
      D.banner = h('div', { class: 'pn-banner', hidden: true }),
      D.sim = h('div', { class: 'pn-sim' },
        D.simModes = h('div', { class: 'pn-tabs pn-sim-modes', role: 'tablist' },
          ...[['replay', '运动回放'], ['stress', '强度和应力'], ['history', '历史结果']].map(([m, l]) =>
            h('button', { 'data-mode': m, role: 'tab', onclick: () => setSimMode(m) }, l))),
        D.view = h('div', { class: 'pn-view' }, h('span', { class: 'pn-tag' }, '打印件、PCB 与外壳同一场景'),
          D.viewEmpty = h('div', { class: 'pn-empty-over' }, icon('cube'), D.viewEmptyTitle = h('div', {}, '还没有运行结果'),
            D.viewEmptyText = h('div', { class: 'muted small' }, '在右边选一个测试，跑完会在这里回放 3D 动画'))),
        D.stress = h('div', { class: 'pn-stress', hidden: true },
          D.stressView = h('div', { class: 'pn-view pn-stress-view' }, D.stressTag = h('span', { class: 'pn-tag' }, '3D 参考图：颜色是应力'),
            D.stressLegend = h('div', { class: 'pn-stress-legend', hidden: true })),
          D.stressInfo = h('div', { class: 'pn-stress-info' })),
        D.history = h('div', { class: 'pn-history', hidden: true })),
      D.pcb = h('div', { class: 'pn-pcb', hidden: true },
        D.parts = h('div', { class: 'pn-parts-wrap' }),
        D.check = h('div', { class: 'pn-check' }),
        D.spice = h('div', { class: 'pn-check' }),
        h('div', { class: 'pn-sec' }, h('div', { class: 'pn-sec-head' }, h('strong', {}, '电源和单片机参数'),
          h('span', { class: 'muted small' }, '改完按回车保存，下次仿真就按新数值算')),
          D.power = h('div', { class: 'pf-grid' })),
        D.wiring = h('div', { class: 'pn-sec' }),
        h('div', { class: 'pn-circuit-wrap' },
          h('div', { class: 'pn-legend' }, h('strong', {}, '电路原理（跟着仿真动）'),
            h('span', { class: 'muted' }, '线越粗电流越大，变红表示电压掉得太低')),
          D.circuit = h('div', { class: 'pn-circuit' }))),
      D.model = h('div', { class: 'pn-model', hidden: true })),
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
  queueMicrotask(() => setSimMode('replay'));
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
  store.on('project', () => { if (!el.hidden) { renderTests(); renderProjBox(); renderSimEmpty(); if (tab === 'model') renderModel(); if (tab === 'pcb') { renderPower(); renderWiring(); } } });
  let knownRuns = null;
  store.on('runs', () => {
    const ids = store.runs.map((r) => r.run_id);
    if (knownRuns && !pendingJob) {
      // a run this page did not start: the AI ran it through MCP. Show it.
      const fresh = store.runs.find((r) => !knownRuns.has(r.run_id) && !String(r.run_id).includes('-sweep'));
      if (fresh && !el.hidden) {
        selected = fresh.scenario;
        setTab('sim'); setSimMode('replay');
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
    spice = null;
    if (tab === 'pcb') { renderCheck(); renderSpice(); }
    if (tab === 'model') renderModel();
    toast('模型或板子文件刚被改过（可能是 AI 改的）。重新运行测试就能看到效果。', { timeout: 6000 });
  });

  // ------------------------------------------------------------ connect an AI agent (MCP)
  async function showAiSetup() {
    let s;
    try { s = await get('ai_setup'); } catch (e) { toastError(e); return; }
    const card = (name, text, hint) => h('div', { class: 'pn-ai-card' },
      h('strong', {}, name), h('span', { class: 'muted small' }, hint),
      h('button', { class: 'btn primary', type: 'button', onclick: () => copyText(text, `已复制，粘贴发给你的 ${name} 就行`) }, icon('copy'), `复制给 ${name}`));
    modal({
      title: '连接你的 AI',
      width: '620px',
      body: h('div', { class: 'pn-ai' },
        h('p', {}, '用哪个就点哪个按钮，把复制的话发给它，它会自己配置好并连上。',
          '连上以后，你可以直接让它打开或新建工程、改 3D 模型、改 PCB、跑 KiCad 检查和仿真测试、查嘉立创元件，结果都会出现在这个页面上。'),
        h('div', { class: 'pn-ai-cards' },
          card('Claude Code', s.prompt_claude_code, '它会在终端里运行一条命令'),
          card('Codex', s.prompt_codex, '它会改一下自己的配置文件')),
        h('p', { class: 'muted small' }, '这个页面不保存你的 AI 账号，AI 用的是你自己电脑上的 Claude Code 或 Codex。')),
      actions: [{ label: '关闭', value: true }],
    });
  }

  // ------------------------------------------------------------ settings (appearance, about)
  function showSettings() {
    let theme = 'system';
    try { theme = localStorage.getItem('emcosim.theme') || 'system'; } catch { /* storage off */ }
    const app = store.app || {};
    const seg = h('div', { class: 'seg-ctl', role: 'group', 'aria-label': '主题' });
    const drawSeg = () => clear(seg).append(...[['system', '跟随系统'], ['light', '浅色'], ['dark', '深色']].map(([v, l]) =>
      h('button', { class: theme === v ? 'on' : '', 'aria-pressed': String(theme === v), onclick: () => {
        theme = v;
        try { if (v === 'system') localStorage.removeItem('emcosim.theme'); else localStorage.setItem('emcosim.theme', v); } catch { /* storage off */ }
        if (v === 'system') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = v;
        store.emit('theme', v); drawSeg();
      } }, l)));
    drawSeg();
    const q = app.qemu || {};
    modal({
      title: '设置', width: '560px',
      body: h('div', { class: 'pn-settings' },
        h('div', { class: 'pn-set-row' }, h('span', { class: 'k' }, '外观'), seg),
        h('div', { class: 'pn-set-row' }, h('span', { class: 'k' }, '版本'), h('span', { class: 'mono' }, app.version || '—')),
        h('div', { class: 'pn-set-row' }, h('span', { class: 'k' }, '当前工程'), h('span', { class: 'mono small' }, app.project || '未打开')),
        h('div', { class: 'pn-set-row' }, h('span', { class: 'k' }, '固件仿真（QEMU）'), h('span', { class: 'small' }, q.found ? h('span', { class: 'mono' }, q.path) : '没找到，运行 scripts\\setup 安装')),
        h('div', { class: 'pn-set-row' }, h('span', { class: 'k' }, '许可'), h('span', { class: 'small' }, 'Apache-2.0；QEMU、KiCad 为 GPL，作为独立程序运行'))),
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

  // ------------------------------------------------------------ PCB page: everything about the circuit
  function renderPcbPage() { renderPcb(); renderCheck(); renderSpice(); renderPower(); renderWiring(); renderCircuit(); }

  // ngspice on the board netlist, with the servo / MCU currents of the latest simulation
  let spice = null, spiceBusy = false;
  async function runSpice() {
    spiceBusy = true; renderSpice();
    try { spice = await post('circuit/check', runId ? { run_id: runId } : {}); }
    catch (e) { spice = { failed: e.message }; }
    spiceBusy = false; renderSpice();
  }
  function renderSpice() {
    clear(D.spice);
    if (!store.project?.data?.circuit?.netlist) return;
    D.spice.append(h('div', { class: 'pn-check-head' },
      h('div', {}, h('strong', {}, '电路检查（ngspice）'),
        h('div', { class: 'muted small' }, '用板子上真实的电阻、电容、二极管、稳压和保险丝，按仿真里的舵机和单片机电流重新算一遍电压')),
      h('button', { class: 'btn sm', disabled: spiceBusy, onclick: runSpice }, spiceBusy ? '计算中…' : spice ? '重新检查' : '检查')));
    if (!spice) return;
    if (spice.failed) { D.spice.append(h('div', { class: 'pn-banner bad' }, spice.failed)); return; }
    const title = store.project?.scenario_info?.[spice.scenario]?.title || spice.scenario || spice.run;
    const worst = Math.max(0, ...spice.compare.map((r) => r.max_abs_diff_v));
    D.spice.append(
      h('div', { class: 'pn-banner ' + (worst < 0.2 ? 'ok' : 'bad') },
        worst < 0.2 ? `和仿真一致：各点电压最多差 ${worst.toFixed(2)} V（按「${title}」的电流算）`
          : `和仿真差得比较多：最多差 ${worst.toFixed(2)} V（按「${title}」的电流算），电源参数可能需要调整`),
      h('table', { class: 'pn-parts' },
        h('thead', {}, h('tr', {}, ['网络', 'ngspice 最低电压', '仿真最低电压', '最大差值'].map((x) => h('th', {}, x)))),
        h('tbody', {}, spice.compare.map((r) => h('tr', {},
          h('td', { class: 'mono' }, r.net), h('td', { class: 'num' }, `${r.spice_min_v.toFixed(2)} V`),
          h('td', { class: 'num' }, `${r.cosim_min_v.toFixed(2)} V`), h('td', { class: 'num' }, `${r.max_abs_diff_v.toFixed(2)} V`))))),
      spice.parts_not_modelled?.length ? h('div', { class: 'muted small' },
        `算进去了 ${spice.parts_modelled.length} 个元件；没算的：${spice.parts_not_modelled.map((x) => x.ref || x).slice(0, 12).join('、')}${spice.parts_not_modelled.length > 12 ? ' 等' : ''}`) : null);
  }

  function renderPower() {
    clear(D.power);
    const cards = powerCards();
    if (!cards.length) D.power.append(h('div', { class: 'pn-side-empty' }, '这个工程还没有电源网络。导入 PCB 后可以让 AI 帮你按板子建好。'));
    else D.power.append(...cards);
  }

  // which MCU pin drives what: firmware, bindings, inputs
  function renderWiring() {
    clear(D.wiring);
    const d = store.project?.data || {};
    const mcu = d.mcu || {}, binds = d.bindings || [];
    D.wiring.append(h('div', { class: 'pn-sec-head' }, h('strong', {}, '单片机和接线'),
      h('span', { class: 'muted small' }, `${(mcu.type || '—').toUpperCase()} · 固件 ${mcu.firmware?.flash ? baseName(mcu.firmware.flash) : '还没有'} · ${store.app?.qemu?.found ? '可以跑真实固件' : '没装 QEMU，只能跑快速测试'}`)));
    if (!binds.length) { D.wiring.append(h('div', { class: 'pn-side-empty' }, '还没有接线。可以让 AI 按原理图把引脚连到舵机、按键上。')); return; }
    const who = (name) => d.actuators?.[name] ? `${name}（${d.actuators[name].preset || d.actuators[name].type}）` : d.inputs?.[name] ? `${name}（按键）` : name;
    D.wiring.append(h('table', { class: 'pn-parts' },
      h('thead', {}, h('tr', {}, ['引脚', '方向', '连到', '网络'].map((x) => h('th', {}, x)))),
      h('tbody', {}, binds.map((b) => h('tr', {},
        h('td', { class: 'mono' }, b.pin), h('td', {}, b.to ? '输出 →' : '← 输入'),
        h('td', {}, who(b.to || b.from)), h('td', { class: 'mono muted' }, b.net || '–'))))));
  }

  // ------------------------------------------------------------ 仿真 page modes: replay | strength & stress | history
  let simMode = 'replay';
  function setSimMode(m) {
    simMode = m;
    for (const b of D.simModes.children) { const on = b.dataset.mode === m; b.classList.toggle('active', on); b.setAttribute('aria-selected', String(on)); }
    D.view.hidden = m !== 'replay'; D.stress.hidden = m !== 'stress'; D.history.hidden = m !== 'history';
    el.classList.toggle('sim-other', m !== 'replay');
    if (m === 'stress') loadChecks();
    else if (m === 'history') renderHistory();
    else main3d?.resize();
  }

  const MODE_ZH = { in_plane: '层内断裂', interlayer_normal: '层与层被拉开', interlayer_shear: '层与层错开', interlayer_combined: '层间综合' };
  const sfClass = (sf) => (sf >= 3 ? 'ok' : sf >= 1.5 ? 'warn' : 'bad');
  const sfText = (sf) => (sf >= 3 ? '够结实' : sf >= 1.5 ? '余量不多' : '可能会坏');
  let checks = null, gears = null, stressJob = null, stress3d = null, stressPart = null;

  async function loadChecks() {
    try { checks = await get('checks'); } catch (e) { checks = { failed: e.message }; }
    try { gears = runId || store.runs.length ? await post('checks/gears', runId ? { run_id: runId } : {}) : null; }
    catch (e) { gears = { failed: e.message }; }
    renderChecks();
  }

  async function startStrength(part) {
    try {
      const job = await post('jobs/strength', { part, ...(runId ? { run_id: runId } : {}) });
      stressJob = { id: job.id, part, progress: 0 };
    } catch (e) { toastError(e); }
    renderChecks();
  }
  store.on('job_progress', (d) => { if (stressJob && d.id === stressJob.id) { stressJob.progress = d.progress; renderChecks(); } });
  store.on('job', (j) => {
    if (!stressJob || j.id !== stressJob.id) return;
    if (j.status === 'done') { stressPart = stressJob.part; stressJob = null; loadChecks(); }
    else if (j.status === 'failed' || j.status === 'cancelled') { stressJob = null; toast(`强度检测没算完：${j.error || '已取消'}`, { kind: 'bad', timeout: 8000 }); renderChecks(); }
  });
  // results written by the AI through MCP (or another tab): show them
  store.on('checks_changed', () => {
    if (el.hidden || stressJob) return;
    setTab('sim'); setSimMode('stress');
  });

  function renderChecks() {
    clear(D.stressInfo);
    if (!checks) { D.stressInfo.append(h('div', { class: 'muted' }, '读取中…')); return; }
    if (checks.failed) { D.stressInfo.append(h('div', { class: 'pn-banner bad' }, checks.failed)); return; }
    const parts = checks.strength_parts || [];
    const title = (sc) => store.project?.scenario_info?.[sc]?.title || sc;
    D.stressInfo.append(h('div', { class: 'muted small' },
      runId ? `按当前回放的这次仿真的受力来算（${title(meta?.scenario?.name)}）。` : '按最近一次仿真的受力来算。',
      '安全系数 = 材料能承受的 ÷ 实际受到的，3 倍以上算够结实。'));
    if (!parts.length) {
      D.stressInfo.append(h('div', { class: 'pn-side-empty' }, '这个工程还没有设置要检测强度的打印件。可以让 AI 给打印件设好夹持位置和受力位置（strength 配置）。'));
    }
    for (const sp of parts) {
      const r = checks.strength?.[sp.name];
      const busy = stressJob?.part === sp.name;
      const card = h('div', { class: 'pn-ck' },
        h('div', { class: 'pn-ck-head' },
          h('div', {}, h('strong', {}, `${sp.label} 强度`), h('div', { class: 'muted small' }, `${sp.material || '—'}，按“建模”页里的打印设置算`)),
          h('button', { class: 'btn sm' + (r ? '' : ' primary'), disabled: !!stressJob, onclick: () => startStrength(sp.name) },
            busy ? `计算中 ${Math.round((stressJob.progress || 0) * 100)}%…` : r ? '重新检测' : '开始检测')));
      if (r) {
        card.append(h('div', { class: 'pn-ck-big ' + sfClass(r.min_sf) }, h('span', { class: 'n' }, `${r.min_sf}`), h('span', {}, ` 倍安全系数 · ${sfText(r.min_sf)}`)),
          h('table', { class: 'pn-parts' }, h('tbody', {}, r.cases.map((c) => h('tr', {},
            h('td', {}, c.label, h('div', { class: 'muted small' }, `受力 ${c.force_n} N，最先坏在：${MODE_ZH[c.governing] || c.governing}`)),
            h('td', { class: 'num ' + sfClass(c.min_sf) }, `${c.min_sf} 倍`),
            h('td', { class: 'num muted small' }, `最大应力 ${c.max_von_mises_mpa} MPa`, h('br'), `变形 ${c.tip_deflection_mm ?? c.max_deflection_mm} mm`))))),
          h('div', { class: 'muted small' }, `${r.material}，层高 ${r.print_settings.layer_height_mm} mm，${r.print_settings.walls} 层墙，填充 ${r.print_settings.infill_pct}%。用的是「${title(r.scenario)}」的受力。${r.assumptions}`),
          h('button', { class: 'btn sm', onclick: () => showStress(sp.name) }, icon('cube'), '看 3D 应力图'));
      } else if (!busy) card.append(h('div', { class: 'muted small' }, '有限元计算，大约半分钟到一分钟。'));
      D.stressInfo.append(card);
    }
    // gears
    if (gears?.failed) D.stressInfo.append(h('div', { class: 'pn-banner bad' }, gears.failed));
    for (const g of gears?.gears || []) {
      const worst = Math.min(...g.cases.map((c) => c.sf ?? 99));
      D.stressInfo.append(h('div', { class: 'pn-ck' },
        h('div', { class: 'pn-ck-head' }, h('div', {}, h('strong', {}, `${g.actuator} 舵机齿轮应力`),
          h('div', { class: 'muted small' }, `${(g.preset || '').toUpperCase()} 输出齿轮，${g.gear.material}`))),
        h('div', { class: 'pn-ck-big ' + sfClass(worst) }, h('span', { class: 'n' }, `${worst}`), h('span', {}, ` 倍安全系数 · ${sfText(worst)}`)),
        h('table', { class: 'pn-parts' }, h('tbody', {}, g.cases.map((c) => h('tr', {},
          h('td', {}, c.label, h('div', { class: 'muted small' }, `力矩 ${c.torque_nm} N·m，齿上的力 ${c.tooth_force_n} N`)),
          h('td', { class: 'num ' + sfClass(c.sf) }, `${c.sf} 倍`),
          h('td', { class: 'num muted small' }, `齿根应力 ${c.stress_mpa} MPa`, h('br'), `许用 ${g.gear.allow_mpa} MPa`))))),
        h('div', { class: 'muted small' }, `模数 ${g.gear.module_mm} mm、${g.gear.teeth} 齿、齿宽 ${g.gear.face_width_mm} mm${g.assumed ? '，都是这类舵机的典型值（估计，没拆实物量过），可以在“建模”页改' : ''}。${gears.method}。`)));
    }
    if (!stressPart) stressPart = parts.find((sp) => checks.strength?.[sp.name])?.name || null;
    if (stressPart && checks.strength?.[stressPart]) showStress(stressPart);
    else { stress3d?.clear(); D.stressLegend.hidden = true; D.stressTag.textContent = '3D 参考图：检测后显示应力颜色'; }
  }

  async function showStress(part) {
    stressPart = part;
    const r = checks?.strength?.[part];
    if (!r) return;
    if (!stress3d) {
      const { StressView, stressGradient } = await import('../stress3d.js');
      stress3d = new StressView(D.stressView);
      D.stressGrad = stressGradient;
      if (!stress3d.ok) { D.stressView.append(h('div', { class: 'empty' }, stress3d.error)); return; }
    }
    try {
      const { max } = await stress3d.load(part);
      const caseLabel = r.cases.find((c) => c.case === r.stress_view.case)?.label || '';
      D.stressTag.textContent = `${r.label}：${caseLabel}的应力（拖动旋转）`;
      D.stressLegend.hidden = false;
      clear(D.stressLegend).append(h('span', { class: 'mono' }, '0'), h('i', { style: { background: D.stressGrad } }),
        h('span', { class: 'mono' }, `${max.toFixed(1)} MPa`), h('span', { class: 'muted' }, `（材料强度约 ${r.stress_view.material_strength_mpa} MPa）`));
    } catch (e) { toastError(e); }
  }

  // ------------------------------------------------------------ history: past runs, compare two
  const pickedRuns = new Set();
  const METRICS = [['probe.press.force_n.peak', '最大按压力', 'N', 2], ['node.v5_servo.min_v', '舵机最低电压', 'V', 2],
    ['node.vin5.min_v', '输入最低电压', 'V', 2], ['mcu.min_rail_v', '单片机最低电压', 'V', 2], ['mcu.resets', '单片机重启', '次', 0],
    ['act.servo1.peak_current_a', '舵机最大电流', 'A', 2], ['joint.servo_axis.deg.max', '按压臂最大角度', '°', 1]];
  const metaCache = new Map();
  const runMeta = (id) => { if (!metaCache.has(id)) metaCache.set(id, get(`run/${id}`)); return metaCache.get(id); };

  async function renderHistory() {
    clear(D.history);
    const runs = store.runs.filter((r) => !String(r.run_id).includes('-sweep'));
    const title = (sc) => store.project?.scenario_info?.[sc]?.title || sc;
    if (!runs.length) { D.history.append(h('div', { class: 'pn-side-empty' }, '还没有仿真结果。在右边选一个测试跑一次。')); return; }
    D.history.append(h('div', { class: 'muted small' }, '勾选两次结果可以对比；点“回放”回到 3D 动画。'),
      h('table', { class: 'pn-parts' },
        h('thead', {}, h('tr', {}, ['', '时间', '测试', '结果', '临时改的参数', ''].map((x) => h('th', {}, x)))),
        h('tbody', {}, runs.slice(0, 40).map((r) => h('tr', { class: r.run_id === runId ? 'cur' : '' },
          h('td', {}, h('input', { type: 'checkbox', checked: pickedRuns.has(r.run_id), 'aria-label': '选择对比', onchange: (e) => {
            if (e.target.checked) { pickedRuns.add(r.run_id); if (pickedRuns.size > 2) pickedRuns.delete(pickedRuns.values().next().value); } else pickedRuns.delete(r.run_id);
            renderHistory(); } })),
          h('td', { class: 'mono small' }, (r.created || '').slice(5, 16)),
          h('td', {}, title(r.scenario)),
          h('td', { class: r.passed ? 'ok' : 'bad' }, r.passed ? '通过' : '没通过'),
          h('td', { class: 'mono small muted' }, Object.entries(r.overrides || {}).map(([k, v]) => `${k}=${v}`).join('，') || '–'),
          h('td', {}, h('button', { class: 'btn sm', onclick: () => { selected = r.scenario; renderTests(); setSimMode('replay'); openRun(r.run_id); } }, '回放')))))));
    if (pickedRuns.size === 2) {
      const [a, b] = [...pickedRuns];
      const box = h('div', { class: 'pn-ck' }, h('strong', {}, '对比'));
      D.history.append(box);
      try {
        const [ma, mb] = await Promise.all([runMeta(a), runMeta(b)]);
        const v = (m, k) => m.metrics?.[k];
        box.append(h('table', { class: 'pn-parts' },
          h('thead', {}, h('tr', {}, h('th', {}, ''), h('th', {}, `${title(ma.scenario?.name)} ${(ma.created || '').slice(11, 16)}`), h('th', {}, `${title(mb.scenario?.name)} ${(mb.created || '').slice(11, 16)}`), h('th', {}, '差'))),
          h('tbody', {},
            h('tr', {}, h('td', {}, '结果'), h('td', {}, ma.passed ? '通过' : '没通过'), h('td', {}, mb.passed ? '通过' : '没通过'), h('td', {}, '')),
            METRICS.filter(([k]) => v(ma, k) !== undefined || v(mb, k) !== undefined).map(([k, l, u, d]) => {
              const x = v(ma, k), y = v(mb, k);
              const f = (n) => (typeof n === 'number' ? `${n.toFixed(d)} ${u}` : '–');
              return h('tr', {}, h('td', {}, l), h('td', { class: 'num' }, f(x)), h('td', { class: 'num' }, f(y)),
                h('td', { class: 'num muted' }, typeof x === 'number' && typeof y === 'number' ? `${(y - x >= 0 ? '+' : '')}${(y - x).toFixed(d)} ${u}` : ''));
            }))));
      } catch (e) { box.append(h('div', { class: 'pn-banner bad' }, e.message)); }
    }
  }
  store.on('runs', () => { if (!el.hidden && tab === 'sim' && simMode === 'history') renderHistory(); });

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
    D.tabSim.classList.toggle('active', t === 'sim'); D.tabPcb.classList.toggle('active', t === 'pcb');
    D.tabModel.classList.toggle('active', t === 'model');
    el.classList.toggle('tab-model', t === 'model');
    el.classList.toggle('tab-pcb', t === 'pcb');
    el.classList.toggle('tab-sim', t === 'sim');
    if (!store.app?.project) return;
    D.sim.hidden = t !== 'sim'; D.pcb.hidden = t !== 'pcb'; D.model.hidden = t !== 'model';
    if (t === 'pcb') renderPcbPage();
    else if (t === 'model') renderModel();
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

  // ------------------------------------------------------------ 建模 page: the 3D model before any simulation
  let model = null, model3d = null, modelBusy = false;
  const JOINT_KIND = { hinge: '转动', slide: '滑动', ball: '球铰', free: '自由' };
  const filePicker = h('input', { type: 'file', accept: '.stl,.step,.stp,.obj', multiple: true, hidden: true,
    onchange: () => { const files = [...filePicker.files]; filePicker.value = ''; uploadModels(files); } });
  el.append(filePicker);

  async function uploadModels(files) {
    if (!files.length || modelBusy) return;
    modelBusy = true; renderModelHead();
    const done = [];
    try {
      for (const f of files) {
        toast(`正在导入 ${f.name}…`, { timeout: 3000 });
        const r = await upload(`upload/model?name=${encodeURIComponent(f.name)}`, f);
        done.push(r.part);
      }
      toast(`已导入 ${done.join('、')}。零件放在原点，没有关节；要让它动起来，可以让 AI 帮你加关节和测试。`, { timeout: 8000 });
    } catch (e) { toastError(e); }
    modelBusy = false;
    if (model3d) model3d._framed = false;     // bring the new parts into view
    try { await store.loadProject(); } catch { /* shown by renderModel */ }
    renderModel();
  }

  function modelActions(primary) {
    return [
      h('button', { class: 'btn' + (primary ? ' primary' : ''), disabled: modelBusy, onclick: () => filePicker.click(),
        title: '支持 STL、STEP、OBJ；单位按毫米' }, icon('import'), modelBusy ? '导入中…' : '导入 3D 模型'),
      h('button', { class: 'btn', onclick: () => showAiSetup(), title: '连上你的 AI，让它按你的描述建模、加关节' }, icon('link'), '让 AI 帮你建模'),
    ];
  }

  let modelHead = null;
  function renderModelHead() {
    if (!modelHead || !model) return;
    const n = model.bodies.length, j = model.joints.length;
    clear(modelHead).append(
      h('div', {}, h('strong', {}, '3D 模型'),
        h('span', { class: 'muted' }, `  ${n} 个零件，${j ? j + ' 个活动关节' : '没有活动关节'}`),
        model.mjcf ? h('div', { class: 'muted small mono' }, model.mjcf) : null),
      h('div', { class: 'pn-model-acts' }, ...modelActions(false),
        h('button', { class: 'btn', title: '重置视角', onclick: () => model3d?.resetView() }, icon('refresh'))));
  }

  async function renderModel() {
    if (!store.app?.project) return;
    try { model = await get('model'); } catch (e) { clear(D.model).append(h('div', { class: 'pn-banner bad' }, e.message)); return; }
    if (tab !== 'model') return;
    if (!model.has_model) {
      clear(D.model).append(h('div', { class: 'pn-model-empty' },
        h('span', { class: 'pn-start-ico' }, icon('shapes')),
        h('h2', {}, '从建模开始'),
        h('p', { class: 'muted' }, '这个工程还没有 3D 模型。把零件的 STL / STEP 文件导进来，或者连上你的 AI，跟它描述想做的东西，让它来建模。',
          '模型好了以后再放 PCB 元件、加测试，“仿真”页就能跑了。'),
        h('div', { class: 'pn-model-acts' }, ...modelActions(true)),
        model.print_parts.length ? printList() : null));
      return;
    }
    const view = D.modelView ||= h('div', { class: 'pn-view pn-model-view' }, h('span', { class: 'pn-tag' }, '初始姿态，拖动可旋转、滚轮缩放'));
    clear(D.model).append(
      modelHead = h('div', { class: 'pn-pcb-head pn-model-head' }),
      h('div', { class: 'pn-model-body' }, view, h('div', { class: 'pn-model-info' },
        model.error ? h('div', { class: 'pn-banner bad' }, `模型编译失败：${model.error}`) : null,
        h('div', { class: 'pn-side-title' }, '零件'),
        h('table', { class: 'pn-parts' },
          h('thead', {}, h('tr', {}, ['名称', '挂在', '质量'].map((x) => h('th', {}, x)))),
          h('tbody', {}, model.bodies.map((b) => h('tr', {},
            h('td', { class: 'mono' }, b.name), h('td', { class: 'mono muted' }, b.parent),
            h('td', { class: 'num' }, b.mass_kg ? `${fmtNum(b.mass_kg * 1000, 3)} g` : '–'))))),
        h('div', { class: 'pn-side-title' }, '活动关节'),
        model.joints.length ? h('table', { class: 'pn-parts' },
          h('thead', {}, h('tr', {}, ['名称', '方式', '零件', '范围'].map((x) => h('th', {}, x)))),
          h('tbody', {}, model.joints.map((j) => h('tr', {},
            h('td', { class: 'mono' }, j.name), h('td', {}, JOINT_KIND[j.type] || j.type), h('td', { class: 'mono muted' }, j.body),
            h('td', { class: 'num' }, j.range ? (j.type === 'hinge' ? `${j.range[0]}° ~ ${j.range[1]}°` : `${fmtNum(j.range[0] * 1000, 3)} ~ ${fmtNum(j.range[1] * 1000, 3)} mm`) : '不限')))))
          : h('div', { class: 'pn-side-empty' }, '还没有活动关节，零件都是固定的。可以让 AI 帮你加转轴或滑轨。'))),
      h('div', { class: 'muted small' }, '这里显示的是模型本身，还没有跑仿真。改尺寸、加关节、换材料可以直接跟你的 AI 说，改完这里会自动刷新。'),
      D.modelMat = h('div', { class: 'pn-sec' }),
      D.modelMech = h('div', { class: 'pn-sec' }));
    renderModelParams();
    renderModelHead();
    if (!model3d) {
      const { Replay3D } = await import('../replay3d.js');
      model3d = new Replay3D(view);
      if (!model3d.ok) { view.append(h('div', { class: 'empty' }, model3d.error)); return; }
    }
    if (model.error || !model3d.ok) return;
    try { await model3d.loadFrom('model'); } catch (e) { toastError(e); }
    model3d.resize();
  }

  // materials, print settings and mechanism parameters, edited in place
  function renderModelParams() {
    if (!D.modelMat || !model) return;
    const pc = printCards(model.materials);
    clear(D.modelMat).append(h('div', { class: 'pn-sec-head' }, h('strong', {}, '材料和打印'),
      h('span', { class: 'muted small' }, '每个打印件用什么料、怎么打；改完按回车保存，强度检测会按这里的设置算'),
      pc.length ? h('span', { class: 'pn-model-acts', style: { marginLeft: 'auto' } },
        h('button', { class: 'btn sm', onclick: () => sendPrint(false), title: '导出一个 3MF 文件，可以拖进任何切片软件' }, icon('import'), '导出 3MF'),
        h('button', { class: 'btn sm primary', onclick: () => sendPrint(true) }, icon('send'), '发到 Bambu Studio')) : null));
    D.modelMat.hidden = !pc.length;
    if (pc.length) D.modelMat.append(h('div', { class: 'pf-grid' }, ...pc));
    const mc = mechCards(store.project?.presets, model.gear_defaults);
    clear(D.modelMech).append(h('div', { class: 'pn-sec-head' }, h('strong', {}, '机构参数'),
      h('span', { class: 'muted small' }, '电机型号、齿轮、开关手感、开始角度')), h('div', { class: 'pf-grid' }, ...mc));
    D.modelMech.hidden = !mc.length;
  }

  async function sendPrint(open) {
    toast(open ? '正在导出并打开 Bambu Studio…' : '正在导出 3MF…', { timeout: 3000 });
    try {
      const r = await post('printing/send', { open, with_settings: true });
      const how = r.opened?.method === 'executable' ? '已用 Bambu Studio 打开' : r.opened?.method === 'url_scheme' ? '已通过 bambustudio:// 链接打开' : '3MF 已导出';
      toast(`${how}：${r.file}`, { timeout: 9000, action: { label: '复制路径', fn: () => copyText(r.file) } });
    } catch (e) { toastError(e); }
  }

  function printList() {
    return h('div', {},
      h('div', { class: 'pn-side-title' }, '打印件'),
      h('table', { class: 'pn-parts' },
        h('thead', {}, h('tr', {}, ['零件', '材料', '建议打印设置'].map((x) => h('th', {}, x)))),
        h('tbody', {}, model.print_parts.map((p) => h('tr', {},
          h('td', {}, h('div', {}, p.label),
            h('div', { class: 'mono small ' + (p.exists ? 'muted' : 'bad') }, p.exists ? baseName(p.file) : `缺文件 ${p.file}`)),
          h('td', {}, p.material || '–'),
          h('td', { class: 'small muted' }, [p.settings.layer_height && `层高 ${p.settings.layer_height} mm`,
            p.settings.wall_loops && `${p.settings.wall_loops} 层墙`, p.settings.sparse_infill_density && `填充 ${p.settings.sparse_infill_density}`]
            .filter(Boolean).join('，') || '–'))))));
  }

  // 仿真 page placeholder: what to do next depends on whether the project has tests yet
  function renderSimEmpty() {
    const none = store.app?.project && !scenarios().length;
    D.viewEmptyTitle.textContent = none ? '还没有仿真' : '还没有运行结果';
    D.viewEmptyText.textContent = none ? '这个工程还没有测试。先在“建模”和“PCB 元件”里把模型和板子放好，再让 AI 加测试。'
      : '在右边选一个测试，跑完会在这里回放 3D 动画';
  }

  // ------------------------------------------------------------ lifecycle
  // ------------------------------------------------------------ project: welcome page when none is open
  const baseName = (p) => String(p || '').split(/[\\/]/).filter(Boolean).pop() || '';
  function projectName() { return store.project?.data?.name || baseName(store.app?.project); }

  let shownRoot;
  async function reloadProject() {
    store.seenRuns?.clear?.(); store.newRuns?.clear?.();
    await store.loadApp(); await store.loadProject(); await store.loadRuns();
    await switched();
  }
  async function switched() {
    resetRun(); selected = null; boards = null; boardCheck = null; knownRuns = null; model = null;
    checks = null; gears = null; stressPart = null; pickedRuns.clear(); metaCache.clear(); stress3d?.clear();
    if (simMode !== 'replay') setSimMode('replay');
    model3d?.clear(); model3d && (model3d._framed = false);
    await showProject();
  }
  // opened from elsewhere: the AI through MCP (open_project / open_example / new_project) or another tab
  store.on('project_opened', () => { if ((store.app?.project || null) !== shownRoot) switched(); });
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
    const dir = store.app?.projects_dir || '';
    const sep = dir.includes('\\') ? '\\' : '/';
    const name = h('input', { class: 'input', placeholder: '例如 小台灯', value: '我的工程' });
    const where = h('div', { class: 'hint mono' });
    const f = pathField({ label: '放在别的文件夹（可不填）', kind: 'folder', placeholder: '不填就放在上面的位置' });
    const showWhere = () => { where.textContent = '保存到 ' + (f.value || (dir + sep + (name.value.trim() || '我的工程'))); };
    name.addEventListener('input', showWhere); f.el.addEventListener('input', showWhere); showWhere();
    const more = h('details', { class: 'pn-new-more' }, h('summary', { class: 'small muted' }, '换个保存位置'), f.el);
    setTimeout(() => { name.focus(); name.select(); }, 50);
    const ok = await modal({ title: '新建工程',
      body: [h('label', { class: 'field' }, '工程名称', name), where, more,
        h('div', { class: 'hint' }, '新工程是空白的，会先打开“建模”页：导入 3D 模型或让 AI 帮你建模，再放 PCB 元件、加测试。')],
      actions: [{ label: '取消', value: false }, { label: '创建', kind: 'primary', handler: async () => {
        const n = name.value.trim();
        if (!n) throw new Error('请填写工程名称');
        await post('project/new', f.value ? { path: f.value, name: n } : { name: n });
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
    D.projBox.append(
      p ? h('div', { class: 'pn-proj-label pn-nav-label' }, '当前工程') : null,
      p ? h('div', { class: 'pn-proj-cur', title: p }, icon('folder'), h('span', { class: 'pn-nav-label pn-proj-name' }, projectName())) : null,
      navItemBtn('plus', '新建工程', () => newProject()),
      navItemBtn('import', '打开工程', () => openFolder()));
  }

  function renderWelcome() {
    const app = store.app || {};
    const recent = (app.recent_projects || []).slice(0, 5);
    const ex = (app.examples || [])[0];
    const card = (ic, title, text, onclick, primary) => h('button', { class: 'pn-start' + (primary ? ' primary' : ''), onclick },
      h('span', { class: 'pn-start-ico' }, icon(ic)), h('span', { class: 'pn-start-txt' }, h('strong', {}, title), h('span', { class: 'muted small' }, text)));
    clear(D.welcome).append(h('div', { class: 'pn-welcome-in' },
      h('h1', {}, '欢迎使用 SimWorld'),
      h('p', { class: 'muted' }, '把 PCB 和 3D 模型放进来，在同一个场景里跑真实的电路和物理测试；再让你的 AI 改板子、改模型，直到测试通过。'),
      h('div', { class: 'pn-start-grid' },
        ex ? card('cube', '打开示例工程', '开关机器人：舵机按墙上开关，带 V0.5 控制板', () => openProject(ex, true), true) : null,
        card('folder', '打开工程', '选一个有 project.yaml 的文件夹', openFolder),
        card('plus', '新建空工程', '从零开始，让 AI 帮你导入 PCB 和模型', newProject)),
      h('div', { class: 'pn-welcome-ai' }, icon('link'), h('span', {},
        '也可以先点右下角的“连接 AI”，连上后直接跟你的 AI 说“打开示例工程”或“打开 D:\\xxx 这个工程”，这个页面会跟着切过去。')),
      recent.length ? h('div', { class: 'pn-recent' }, h('div', { class: 'pn-side-title' }, '最近打开'),
        recent.map((r) => h('button', { class: 'pn-recent-row', onclick: () => openProject(r) },
          icon('folder'), h('span', {}, baseName(r)), h('span', { class: 'muted small mono pn-recent-path' }, r)))) : null));
  }

  let defaultedRoot;
  async function showProject() {
    const has = !!store.app?.project;
    shownRoot = store.app?.project || null;
    // a project that has nothing to simulate yet opens on 建模, one with tests on 仿真
    if (has && shownRoot !== defaultedRoot) { defaultedRoot = shownRoot; tab = scenarios().length ? 'sim' : 'model'; }
    el.classList.toggle('no-project', !has);
    D.welcome.hidden = has;
    setTab(tab);
    D.sim.hidden = !has || tab !== 'sim'; D.pcb.hidden = !has || tab !== 'pcb'; D.model.hidden = !has || tab !== 'model';
    renderProjBox(); renderTests(); renderReadout(); renderSimEmpty();
    if (!has) { renderWelcome(); return; }
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
