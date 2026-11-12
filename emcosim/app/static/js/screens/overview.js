// 工程总览：PCB、电源网络、机械、固件、执行器、输入、引脚绑定和假设备注。
import { post } from '../api.js';
import { store } from '../store.js';
import { h, icon, clear, fmtSI, fmtNum, fmtValue, modal, pathField, toast, toastError } from '../ui.js';

const SVGNS = 'http://www.w3.org/2000/svg';
const s = (tag, attrs = {}, ...kids) => h(tag, { ...attrs, svg: true }, ...kids);

export function createOverview(ctx) {
  const body = h('div', { class: 'screen-body' });
  const sub = h('span', { class: 'sub mono' });
  const el = h('section', { class: 'screen', 'aria-label': '工程总览' },
    h('div', { class: 'screen-head' }, h('h1', {}, '工程总览'), sub, h('span', { class: 'grow' }),
      h('button', { class: 'btn', onclick: () => importDialog() }, icon('import'), '导入…'),
      h('button', { class: 'btn icon ghost', title: '刷新', 'aria-label': '刷新', onclick: () => refresh(true) }, icon('refresh'))),
    body);

  const goParam = (path) => ctx.nav('params', path);

  async function refresh(force) {
    try {
      if (force) await store.loadProject();
      await store.loadPcb(force);
      render();
    } catch (e) { toastError(e); }
  }

  function render() {
    const p = store.project;
    if (!p) { clear(body).append(h('div', { class: 'empty' }, '还没有打开工程')); return; }
    sub.textContent = p.root;
    const d = p.data || {};
    clear(body).append(h('div', { class: 'ov-grid' },
      powerCard(d),
      pcbCard(store.pcb, d),
      mcuCard(d, p),
      mechCard(d),
      actuatorCard(d, p),
      notesCard(d),
    ));
  }

  // ---------------------------------------------------------------- power
  function powerCard(d) {
    const pw = d.power || {};
    const nodes = pw.nodes || {}, sources = pw.sources || {}, branches = pw.branches || {}, regs = pw.regulators || {};
    const depth = {};
    for (const src of Object.values(sources)) if (src.node) depth[src.node] = 0;
    let changed = true, guard = 0;
    while (changed && guard++ < 20) {
      changed = false;
      for (const b of Object.values(branches)) {
        if (depth[b.from] !== undefined && (depth[b.to] === undefined || depth[b.to] > depth[b.from] + 1)) { depth[b.to] = depth[b.from] + 1; changed = true; }
      }
      for (const [name, r] of Object.entries(regs)) {
        if (depth[r.input] !== undefined && (depth[name] === undefined || depth[name] > depth[r.input] + 1)) { depth[name] = depth[r.input] + 1; changed = true; }
      }
    }
    for (const n of Object.keys(nodes)) if (depth[n] === undefined) depth[n] = 0;
    const items = [];   // {id, kind, col, title, sub, lines, note, path, parents}
    for (const [k, v] of Object.entries(sources)) items.push({ id: 's:' + k, kind: 'source', col: 0, title: k, tag: '电源',
      lines: [`${fmtNum(v.v)} V · 内阻 ${fmtNum(v.r_ohm)} Ω`, v.i_limit_a ? `限流 ${fmtNum(v.i_limit_a)} A` : '无限流'], note: v.note, path: `power.sources.${k}`, parents: [] });
    for (const [k, v] of Object.entries(nodes)) items.push({ id: 'n:' + k, kind: 'node', col: 1 + 2 * depth[k], title: k, tag: '节点',
      lines: [`C ${fmtSI(v.c_f, 'F')}`, v.net ? `网络 ${v.net}` : '（不在 PCB 上）'], note: v.note, path: `power.nodes.${k}`,
      parents: Object.entries(sources).filter(([, sv]) => sv.node === k).map(([sk]) => 's:' + sk)
        .concat(Object.entries(branches).filter(([, b]) => b.to === k).map(([bk]) => 'b:' + bk)) });
    for (const [k, v] of Object.entries(branches)) items.push({ id: 'b:' + k, kind: 'branch', col: 2 + 2 * (depth[v.from] ?? 0), title: k, tag: '支路',
      lines: [`R ${fmtNum(v.r_ohm)} Ω`, `${v.from} → ${v.to}`], note: v.note, path: `power.branches.${k}`, parents: ['n:' + v.from] });
    for (const [k, v] of Object.entries(regs)) items.push({ id: 'n:' + k, kind: 'reg', col: 2 + 2 * (depth[v.input] ?? 0), title: k, tag: (v.type || 'reg').toUpperCase(),
      lines: [`${fmtNum(v.v_out)} V · 压差 ${fmtNum(v.dropout_v)} V`, `C_out ${fmtSI(v.c_out_f, 'F')} · Iq ${fmtSI(v.i_q_a, 'A')}`], note: v.note, path: `power.regulators.${k}`, parents: ['n:' + v.input] });
    const maxCol = Math.max(0, ...items.map((i) => i.col));
    for (const [k, a] of Object.entries(d.actuators || {})) if (a.node) items.push({ id: 'l:' + k, kind: 'load', col: maxCol + 1, title: k, tag: a.type === 'servo' ? '舵机' : a.type === 'dc_motor' ? '直流电机' : (a.type || '负载'),
      lines: [a.preset ? `预设 ${a.preset}` : (a.type || ''), `供电 ${a.node}`], note: a.note, path: `actuators.${k}`, parents: ['n:' + a.node] });
    const mcu = d.mcu || {};
    if (mcu.supply) items.push({ id: 'l:mcu', kind: 'load', col: maxCol + 1, title: (mcu.type || 'MCU').toUpperCase(), tag: 'MCU',
      lines: [`运行 ${fmtSI(mcu.current?.run_a, 'A')} / 复位 ${fmtSI(mcu.current?.reset_a, 'A')}`, mcu.brownout ? `欠压 ${fmtNum(mcu.brownout.v_trip)} V 复位` : ''], note: mcu.brownout?.note, path: 'mcu', parents: ['n:' + mcu.supply] });
    if (!items.length) return h('div', { class: 'card span12' }, h('header', {}, h('h2', {}, icon('bolt'), '电源网络')), h('div', { class: 'empty' }, '还没有定义电源网络'));

    const W = 168, H = 62, GX = 38, GY = 14;
    const cols = [];
    items.forEach((it) => (cols[it.col] ||= []).push(it));
    const pos = {};
    let maxRows = 0;
    cols.forEach((list = [], c) => {
      list.sort((a, b) => avgRow(a) - avgRow(b));
      list.forEach((it, r) => { pos[it.id] = { x: c * (W + GX), y: r * (H + GY), r }; });
      maxRows = Math.max(maxRows, list.length);
    });
    function avgRow(it) { const rs = it.parents.map((p) => pos[p]?.r).filter((v) => v !== undefined); return rs.length ? rs.reduce((a, b) => a + b, 0) / rs.length : 0; }
    // center short columns vertically
    const totalH = maxRows * (H + GY) - GY;
    cols.forEach((list = []) => { const hh = list.length * (H + GY) - GY; const off = (totalH - hh) / 2; list.forEach((it) => { pos[it.id].y += off; }); });
    const totalW = cols.length * (W + GX) - GX;
    const svg = s('svg', { class: 'power-svg', viewBox: `-2 -2 ${totalW + 4} ${totalH + 4}`, style: `max-width:${totalW + 4}px`, role: 'img', 'aria-label': '电源网络示意图' });
    for (const it of items) for (const p of it.parents) {
      const a = pos[p], b = pos[it.id];
      if (!a || !b) continue;
      const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2, mx = (x1 + x2) / 2;
      svg.append(s('path', { class: 'wire', d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}` }));
    }
    for (const it of items) {
      const p = pos[it.id];
      const g = s('g', { class: `box ${it.kind}${it.note ? ' has-note' : ''}`, transform: `translate(${p.x},${p.y})`, style: 'cursor:pointer', tabindex: '0', role: 'button',
        'aria-label': `${it.tag} ${it.title}，在参数页查看` });
      g.addEventListener('click', () => goParam(it.path));
      g.addEventListener('keydown', (e) => { if (e.key === 'Enter') goParam(it.path); });
      g.append(s('title', {}, `${it.tag} ${it.title}\n${it.path}${it.note ? '\n备注：' + it.note : ''}`));
      g.append(s('rect', { width: W, height: H, rx: 5 }));
      g.append(s('text', { class: 'k', x: 9, y: 14 }, it.tag));
      g.append(s('text', { class: 't', x: 9, y: 29 }, it.title));
      it.lines.forEach((l, i) => l && g.append(s('text', { class: 'v', x: 9, y: 43 + i * 13 }, l)));
      if (it.note) g.append(s('circle', { class: 'note-dot', cx: W - 9, cy: 9, r: 3.2 }));
      svg.append(g);
    }
    return h('div', { class: 'card span12' },
      h('header', {}, h('h2', {}, icon('bolt'), '电源网络'),
        h('span', { class: 'meta' }, `${Object.keys(sources).length} 个电源 · ${Object.keys(nodes).length} 个节点 · ${Object.keys(branches).length} 条支路 · ${Object.keys(regs).length} 个稳压器 · 橙点 = 有备注/假设 · 点击方块查看参数`)),
      h('div', { class: 'body', style: { overflowX: 'auto' } }, svg));
  }

  // ---------------------------------------------------------------- PCB
  function pcbCard(pcb, d) {
    const card = h('div', { class: 'card span7' });
    const head = h('header', {}, h('h2', {}, icon('board'), 'PCB'));
    card.append(head);
    if (pcb === undefined) { card.append(h('div', { class: 'empty' }, '加载中…')); return card; }
    if (!pcb || !pcb.components) {
      card.append(h('div', { class: 'empty' }, '还没有导入 PCB', h('div', { style: { marginTop: '8px' } },
        h('button', { class: 'btn', onclick: () => importDialog('pcb') }, icon('import'), '导入 KiCad PCB…'))));
      return card;
    }
    head.append(h('span', { class: 'meta mono' }, pcb.source || d.pcb?.file || ''));
    const b = pcb.board || {};
    const comps = pcb.components || [];
    const nets = pcb.nets || {};
    const bw = b.width_mm || 50, bh = b.height_mm || 30, x0 = b.x_min ?? 0, y0 = b.y_min ?? 0;
    const padm = 3;
    const svg = s('svg', { class: 'board-svg', viewBox: `${-padm} ${-padm} ${bw + 2 * padm} ${bh + 2 * padm}`, role: 'img', 'aria-label': 'PCB 元件布局' });
    svg.append(s('rect', { class: 'outline', x: 0, y: 0, width: bw, height: bh, rx: 0.8 }));
    // KiCad import stores y upwards (negative below the top edge); flip to screen coordinates
    const yTop = y0 + bh;
    const compEls = {};
    for (const c of comps) {
      const n = Object.keys(c.pads || {}).length;
      const ww = Math.max(3.2, Math.min(9, 1.6 + n * 1.25)), hh = 3.2;
      const rot = c.rot_deg || 0;
      const cx = c.x_mm - x0, cy = yTop - c.y_mm;
      const g = s('g', { class: 'comp', transform: `translate(${cx},${cy}) rotate(${-rot})` });
      g.append(s('title', {}, `${c.ref}  ${c.value || ''}\n${c.footprint || ''}\n${Object.entries(c.pads || {}).map(([p, nn]) => `${p}: ${nn}`).join('\n')}`));
      g.append(s('rect', { x: -ww / 2, y: -hh / 2, width: ww, height: hh, rx: 0.4 }));
      g.append(s('text', { x: 0, y: 0, transform: `rotate(${rot})` }, c.ref));
      compEls[c.ref] = g;
      svg.append(g);
    }
    const hl = (net, on) => { for (const pin of nets[net] || []) compEls[pin.split('.')[0]]?.classList.toggle('hl', on); };
    const powerNets = new Set(Object.values(d.power?.nodes || {}).map((n) => n.net).filter(Boolean));
    const bindNets = new Set((d.bindings || []).map((x) => x.net).filter(Boolean));
    const netChips = Object.entries(nets).map(([n, pins]) => {
      const chip = h('span', { class: 'chip mono' + (powerNets.has(n) ? ' warn' : bindNets.has(n) ? ' accent' : ''), tabindex: '0',
        title: `${n}: ${pins.join(', ')}${pcb.copper?.[n] ? `\n铜皮 ${fmtNum(pcb.copper[n].length_mm)} mm，串联 ${fmtNum(pcb.copper[n].series_ohm)} Ω` : ''}` }, `${n} `, h('span', { class: 'dim' }, pins.length));
      chip.addEventListener('mouseenter', () => hl(n, true)); chip.addEventListener('mouseleave', () => hl(n, false));
      chip.addEventListener('focus', () => hl(n, true)); chip.addEventListener('blur', () => hl(n, false));
      return chip;
    });
    card.append(h('div', { class: 'body' },
      h('div', { class: 'stat-row' },
        stat(comps.length, '元件'), stat(Object.keys(nets).length, '网络'),
        stat(`${fmtNum(bw)}×${fmtNum(bh)}`, '板框 mm'), stat(fmtNum(b.thickness_mm ?? 1.6), '板厚 mm')),
      h('div', { style: { display: 'grid', gridTemplateColumns: 'minmax(0, 1.2fr) minmax(0, 1fr)', gap: '12px', alignItems: 'start' } },
        svg,
        h('div', {},
          h('div', { class: 'section-title' }, '网络（悬停高亮元件）'),
          h('div', { class: 'netlist' }, netChips),
          h('div', { class: 'hint', style: { marginTop: '8px' } }, h('span', { class: 'chip warn', style: { height: '16px' } }, '橙'), ' 电源网络节点 ', h('span', { class: 'chip accent', style: { height: '16px' } }, '蓝'), ' 有引脚绑定'))),
      h('details', { style: { marginTop: '10px' } }, h('summary', { class: 'muted', style: { cursor: 'pointer' } }, `元件列表（${comps.length}）`),
        h('div', { style: { maxHeight: '260px', overflow: 'auto', marginTop: '6px' } },
          h('table', { class: 'tbl' }, h('thead', {}, h('tr', {}, h('th', {}, '位号'), h('th', {}, '值'), h('th', {}, '封装'), h('th', { class: 'num' }, 'x mm'), h('th', { class: 'num' }, 'y mm'))),
            h('tbody', {}, comps.map((c) => h('tr', { onmouseenter: () => compEls[c.ref]?.classList.add('hl'), onmouseleave: () => compEls[c.ref]?.classList.remove('hl') },
              h('td', { class: 'mono' }, c.ref), h('td', {}, c.value || ''), h('td', { class: 'mono dim', style: { fontSize: '11px' } }, (c.footprint || '').replace(/^[^:]*:/, '')),
              h('td', { class: 'num' }, fmtNum(c.x_mm)), h('td', { class: 'num' }, fmtNum(c.y_mm))))))))));
    return card;
  }

  // ---------------------------------------------------------------- MCU + firmware
  function mcuCard(d, p) {
    const mcu = d.mcu || {};
    const fw = mcu.firmware || {};
    const defs = p.firmware_params || {};
    const ovr = fw.params || {};
    return h('div', { class: 'card span5' },
      h('header', {}, h('h2', {}, icon('chip'), 'MCU 与固件'), h('span', { class: 'meta' }, (mcu.type || '').toUpperCase())),
      h('div', { class: 'body' },
        h('div', { class: 'kv' },
          kv('后端', mcu.backend === 'qemu' ? 'QEMU（真实固件）' : mcu.backend),
          kv('锁步', `${fmtNum(mcu.step_ms)} ms · icount ${fmtNum(mcu.icount_shift)}`),
          kv('供电', mcu.supply),
          kv('电流', `运行 ${fmtSI(mcu.current?.run_a, 'A')} / 复位 ${fmtSI(mcu.current?.reset_a, 'A')}`),
          kv('欠压检测', mcu.brownout ? `< ${fmtNum(mcu.brownout.v_trip)} V 复位，> ${fmtNum(mcu.brownout.v_release)} V 释放` : '—'),
          kv('固件', fw.flash ? `${fw.flash}（${fmtSI(fw.size_bytes, 'B')}）` : '未导入')),
        h('div', { class: 'section-title', style: { marginTop: '12px' } }, '固件参数（fw_params 分区，改了不用重新编译）'),
        Object.keys(defs).length ? h('table', { class: 'tbl' },
          h('thead', {}, h('tr', {}, h('th', {}, '参数'), h('th', { class: 'num' }, '默认'), h('th', { class: 'num' }, '工程值'))),
          h('tbody', {}, Object.entries(defs).map(([k, v]) => h('tr', { class: 'click', onclick: () => goParam(`mcu.firmware.params.${k}`) },
            h('td', { class: 'mono' }, k), h('td', { class: 'num dim' }, fmtValue(v)),
            h('td', { class: 'num' }, k in ovr ? h('b', {}, fmtValue(ovr[k])) : h('span', { class: 'dim' }, '默认'))))))
          : h('div', { class: 'hint' }, '固件没有 fw_params 分区'),
        !fw.flash ? h('button', { class: 'btn', style: { marginTop: '8px' }, onclick: () => importDialog('firmware') }, icon('import'), '导入固件…') : null));
  }

  // ---------------------------------------------------------------- mechanics
  function mechCard(d) {
    const m = d.mechanics || {};
    const joints = m.joints || {}, els = m.elements || {}, probes = m.probes || {}, parts = m.parts || {};
    return h('div', { class: 'card span6' },
      h('header', {}, h('h2', {}, icon('cube'), '机械'), h('span', { class: 'meta mono' }, m.mjcf || '')),
      h('div', { class: 'body' },
        h('div', { class: 'stat-row' }, stat(Object.keys(joints).length, '关节'), stat(Object.keys(parts).length, '导入零件'),
          stat(Object.keys(els).length, '元件'), stat(Object.keys(probes).length, '力探针'), stat(fmtSI(m.timestep_s, 's'), '步长')),
        h('div', { class: 'section-title' }, '关节'),
        Object.keys(joints).length ? h('table', { class: 'tbl' },
          h('thead', {}, h('tr', {}, h('th', {}, '名称'), h('th', {}, '刚体'), h('th', {}, '类型'), h('th', {}, '轴'), h('th', { class: 'num' }, '范围 °'), h('th', { class: 'num' }, '初始 °'))),
          h('tbody', {}, Object.entries(joints).map(([k, j]) => h('tr', {},
            h('td', { class: 'mono' }, k), h('td', { class: 'mono' }, j.body), h('td', {}, j.type === 'hinge' ? '转动' : j.type === 'slide' ? '滑动' : j.type),
            h('td', { class: 'mono' }, JSON.stringify(j.axis || [])), h('td', { class: 'num' }, j.range_deg ? j.range_deg.map((x) => fmtNum(x)).join(' … ') : '—'),
            h('td', { class: 'num' }, m.initial_deg?.[k] !== undefined ? fmtNum(m.initial_deg[k]) : '—')))))
          : h('div', { class: 'hint' }, '没有标注关节'),
        Object.keys(els).length ? [h('div', { class: 'section-title' }, '元件'),
          Object.entries(els).map(([k, e]) => h('div', { class: 'bind', style: { flexWrap: 'wrap', cursor: 'pointer' }, onclick: () => goParam(`mechanics.elements.${k}`) },
            h('b', { class: 'mono' }, k), h('span', { class: 'chip' }, e.type === 'bistable' ? '双稳态翘板' : e.type),
            h('span', { class: 'mono dim' }, Object.entries(e).filter(([kk]) => !['type', 'note'].includes(kk)).map(([kk, vv]) => `${kk}=${fmtValue(vv)}`).join('  ')),
            e.note ? h('div', { class: 'note', style: { width: '100%' } }, icon('alert', 'sm'), e.note) : null))] : null,
        Object.keys(probes).length ? [h('div', { class: 'section-title' }, '力探针'),
          Object.entries(probes).map(([k, pr]) => h('div', { class: 'bind' }, h('b', { class: 'mono' }, k), h('span', { class: 'dim' }, pr.type),
            h('span', { class: 'mono' }, `${pr.geoms_a}`), h('span', { class: 'arrow' }, '↔'), h('span', { class: 'mono' }, `${pr.geoms_b}`)))] : null,
        Object.keys(parts).length ? [h('div', { class: 'section-title' }, '导入零件'),
          Object.entries(parts).map(([k, pt]) => h('div', { class: 'bind', style: { flexWrap: 'wrap' } }, h('b', { class: 'mono' }, k),
            h('span', { class: 'mono dim' }, Object.entries(pt).filter(([kk]) => kk !== 'note').map(([kk, vv]) => `${kk}=${fmtValue(vv)}`).join('  ')),
            pt.note ? h('div', { class: 'note', style: { width: '100%' } }, icon('alert', 'sm'), pt.note) : null))] : null));
  }

  // ---------------------------------------------------------------- actuators / inputs / bindings
  function actuatorCard(d, p) {
    const acts = d.actuators || {}, inputs = d.inputs || {}, binds = d.bindings || [];
    return h('div', { class: 'card span6' },
      h('header', {}, h('h2', {}, icon('motor'), '执行器 · 输入 · 引脚绑定')),
      h('div', { class: 'body' },
        h('div', { class: 'section-title' }, '执行器'),
        Object.keys(acts).length ? Object.entries(acts).map(([k, a]) => {
          const pre = p.presets?.[a.preset] || {};
          const eff = { ...pre, ...Object.fromEntries(Object.entries(a).filter(([kk]) => !['type', 'joint', 'node', 'preset', 'note'].includes(kk))) };
          return h('div', { style: { padding: '6px 0', borderBottom: '1px dashed var(--line)', cursor: 'pointer' }, onclick: () => goParam(`actuators.${k}`) },
            h('div', { class: 'row wrap' }, h('b', { class: 'mono' }, k), h('span', { class: 'chip' }, a.type === 'servo' ? '舵机' : a.type),
              a.preset ? h('span', { class: 'chip accent' }, `预设 ${a.preset}`) : null,
              h('span', { class: 'dim' }, '关节'), h('span', { class: 'mono' }, a.joint || '—'),
              h('span', { class: 'dim' }, '供电'), h('span', { class: 'mono' }, a.node || '—')),
            Object.keys(eff).length ? h('div', { class: 'mono dim', style: { fontSize: '11.5px', marginTop: '3px' } },
              ['r_ohm', 'gear_ratio', 'efficiency', 'ke_v_s_rad', 'angle_limit_deg'].filter((x) => x in eff).map((x) => `${x}=${fmtValue(eff[x])}`).join('  ')) : null,
            a.note ? h('div', { class: 'note', style: { marginTop: '4px' } }, icon('alert', 'sm'), a.note) : null);
        }) : h('div', { class: 'hint' }, '没有执行器'),
        h('div', { class: 'section-title' }, '输入'),
        Object.keys(inputs).length ? Object.entries(inputs).map(([k, i]) => h('div', { class: 'bind' }, icon('button', 'sm'), h('b', { class: 'mono' }, k),
          h('span', { class: 'dim' }, i.type === 'button' ? '按键' : i.type), i.active ? h('span', { class: 'chip' }, i.active === 'low' ? '低电平有效' : '高电平有效') : null))
          : h('div', { class: 'hint' }, '没有输入'),
        h('div', { class: 'section-title' }, '引脚绑定'),
        binds.length ? binds.map((b) => h('div', { class: 'bind' }, icon('pin', 'sm'),
          b.from ? [h('span', { class: 'mono' }, b.from), h('span', { class: 'arrow' }, '→'), h('b', { class: 'mono' }, b.pin)]
            : [h('b', { class: 'mono' }, b.pin), h('span', { class: 'arrow' }, '→'), h('span', { class: 'mono' }, b.to || '—')],
          b.net ? h('span', { class: 'chip mono accent' }, `网络 ${b.net}`) : null))
          : h('div', { class: 'hint' }, '还没有绑定引脚')));
  }

  // ---------------------------------------------------------------- notes
  function notesCard(d) {
    const notes = [];
    const walk = (o, path) => {
      if (!o || typeof o !== 'object' || Array.isArray(o)) return;
      for (const [k, v] of Object.entries(o)) {
        if (k === 'note' && typeof v === 'string') notes.push({ path: path || '(root)', text: v });
        else walk(v, path ? `${path}.${k}` : k);
      }
    };
    walk(d, '');
    const assumed = (t) => /assum|假设|估|guess/i.test(t);
    const nA = notes.filter((n) => assumed(n.text)).length;
    return h('div', { class: 'card span12' },
      h('header', {}, h('h2', {}, icon('alert'), '备注与假设'), h('span', { class: 'meta' }, `${notes.length} 条备注，其中 ${nA} 条标为假设值（未经实物标定）`)),
      h('div', { class: 'body' },
        notes.length ? h('div', { class: 'notes-list' }, notes.map((n) => h('div', { class: 'n' + (assumed(n.text) ? ' assumed' : '') },
          h('a', { class: 'p', href: '#', onclick: (e) => { e.preventDefault(); goParam(n.path); } }, n.path), h('span', {}, n.text))))
          : h('div', { class: 'hint' }, 'project.yaml 里没有 note'),
        h('div', { class: 'hint', style: { marginTop: '8px' } }, '本平台的电机、电源、开关参数默认值都是按公开规格估的假设值；仿真结论只能说明“在这些参数下会怎样”。')));
  }

  // ---------------------------------------------------------------- import
  async function importDialog(kind = 'pcb') {
    const sel = h('select', { class: 'input' },
      h('option', { value: 'pcb', selected: kind === 'pcb' }, 'PCB（KiCad .kicad_pcb，或 EasyEDA .epcb + .dsn）'),
      h('option', { value: 'model', selected: kind === 'model' }, '3D 模型（STL / OBJ / STEP / MJCF .xml）'),
      h('option', { value: 'firmware', selected: kind === 'firmware' }, 'ESP32 固件（ESP-IDF build 目录或合并 .bin）'));
    const fields = h('div', { class: 'col' });
    let f = {};
    const build = () => {
      const k = sel.value;
      f = { path: pathField({ label: '文件路径', kind: k === 'firmware' ? 'firmware' : k, placeholder: k === 'pcb' ? 'D:\\pcb\\board.kicad_pcb' : k === 'model' ? 'D:\\cad\\arm.stl' : 'D:\\fw\\build' }) };
      clear(fields).append(f.path.el);
      if (k === 'pcb') { f.dsn = pathField({ label: 'Specctra .dsn（仅 EasyEDA .epcb 需要）', kind: 'dsn' }); fields.append(f.dsn.el); }
      if (k === 'model') {
        f.name = h('input', { class: 'input', placeholder: '零件名，例如 press_arm' });
        f.parent = h('input', { class: 'input mono', value: 'world' });
        f.units = h('select', { class: 'input' }, ['mm', 'cm', 'm', 'in'].map((u) => h('option', { value: u }, u)));
        f.mass = h('input', { class: 'input mono', placeholder: '留空按密度 1240 kg/m³（PLA）估算' });
        fields.append(h('div', { class: 'row' }, h('label', { class: 'field grow' }, '名称', f.name), h('label', { class: 'field grow' }, '父刚体', f.parent), h('label', { class: 'field' }, '单位', f.units)),
          h('label', { class: 'field' }, '质量 kg（可选）', f.mass));
      }
      if (k === 'firmware') { f.name = h('input', { class: 'input', value: 'firmware' }); fields.append(h('label', { class: 'field' }, '名称', f.name)); }
    };
    sel.addEventListener('change', build);
    build();
    await modal({
      title: '导入', body: [h('label', { class: 'field' }, '类型', sel), fields],
      actions: [{ label: '取消', value: null }, {
        label: '导入', kind: 'primary', handler: async () => {
          const k = sel.value;
          if (!f.path.value) throw new Error('请填写文件路径');
          const b = { path: f.path.value };
          if (k === 'pcb' && f.dsn.value) b.dsn = f.dsn.value;
          if (k === 'model') { if (f.name.value.trim()) b.name = f.name.value.trim(); b.parent = f.parent.value.trim() || 'world'; b.units = f.units.value; if (f.mass.value.trim()) b.mass_kg = Number(f.mass.value); }
          if (k === 'firmware') b.name = f.name.value.trim() || 'firmware';
          await post(`import/${k}`, b);
          toast('导入完成');
          await store.loadProject();
          await store.loadPcb(true);
          render();
          return true;
        },
      }],
    });
  }

  store.on('project', () => { if (!el.hidden) refresh(false); else stale = true; });
  let stale = false;
  return {
    el,
    show() { refresh(stale); stale = false; },
  };
}

function stat(n, l) { return h('div', { class: 'stat' }, h('div', { class: 'n' }, n), h('div', { class: 'l' }, l)); }
function kv(k, v) { return [h('span', { class: 'k' }, k), h('span', { class: 'v' }, v ?? '—')]; }
