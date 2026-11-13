// Small editable parameter cards for the simple panel pages (PCB 元件 / 建模 / 仿真).
// Each row edits one project.yaml path in friendly units; changes are saved on blur / Enter
// through POST /api/param, the same call the advanced 参数 page uses.
import { post } from '../api.js';
import { store, getPath } from '../store.js';
import { h, fmtNum, toast, toastError } from '../ui.js';

const ASSUMED = /assum|假设|估计|not measured/i;

/** rows: [{ path, label, unit, scale = 1, options, hint }]; rows whose value is missing are skipped. */
export function paramCard(title, rows, { sub, note } = {}) {
  const data = store.project?.data || {};
  const live = rows.filter((r) => getPath(data, r.path) !== undefined);
  if (!live.length) return null;
  const parentNote = (path) => getPath(data, path.split('.').slice(0, -1).concat('note').join('.'));
  return h('div', { class: 'pf-card' },
    h('div', { class: 'pf-head' }, h('strong', {}, title), sub ? h('span', { class: 'muted small' }, sub) : null),
    h('div', { class: 'pf-rows' }, live.map((r) => row(r, getPath(data, r.path), parentNote(r.path)))),
    note ? h('div', { class: 'muted small' }, note) : null);
}

function row(r, value, pnote) {
  const scale = r.scale || 1;
  const assumed = typeof pnote === 'string' && ASSUMED.test(pnote);
  let input;
  if (r.options) {
    input = h('select', { class: 'input pf-in' }, r.options.map(([v, l]) => h('option', { value: String(v), selected: String(v) === String(value) }, l)));
    input.addEventListener('change', () => save(r, r.options.find(([v]) => String(v) === input.value)[0], input));
  } else if (typeof value === 'number') {
    input = h('input', { class: 'input pf-in mono', value: fmtNum(value / scale, 4), inputmode: 'decimal', 'aria-label': r.label });
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') input.blur(); if (e.key === 'Escape') { input.value = fmtNum(value / scale, 4); input.blur(); } });
    input.addEventListener('change', () => {
      const n = Number(String(input.value).replace(/[，,_\s]/g, ''));
      if (!isFinite(n)) { toast(`“${input.value}” 不是数值`, { kind: 'bad' }); input.value = fmtNum(value / scale, 4); return; }
      save(r, n * scale, input);
    });
  } else {
    input = h('input', { class: 'input pf-in', value: value ?? '', 'aria-label': r.label });
    input.addEventListener('change', () => save(r, input.value, input));
  }
  return h('label', { class: 'pf-row', title: (r.hint ? r.hint + '\n' : '') + r.path + (typeof pnote === 'string' ? '\n' + pnote : '') },
    h('span', { class: 'pf-k' }, r.label, assumed ? h('span', { class: 'pf-tag' }, '估计值') : null),
    h('span', { class: 'pf-v' }, input, r.unit ? h('span', { class: 'pf-u muted' }, r.unit) : null));
}

async function save(r, value, input) {
  input.classList.add('saving');
  try {
    await post('param', { path: r.path, value });
    input.classList.remove('saving');
    input.classList.add('saved');
    setTimeout(() => input.classList.remove('saved'), 1200);
  } catch (e) { input.classList.remove('saving'); toastError(e); }
}

// ------------------------------------------------------------ rows per page

const SOURCE_KEYS = [['v', '电压', 'V'], ['r_ohm', '内阻 + 线阻', 'Ω'], ['i_limit_a', '最大电流', 'A']];
const NODE_KEYS = [['c_f', '电容', 'µF', 1e-6]];
const BRANCH_KEYS = [['r_ohm', '电阻', 'Ω']];
const REG_KEYS = [['v_out', '输出电压', 'V'], ['dropout_v', '压差', 'V'], ['c_out_f', '输出电容', 'µF', 1e-6], ['i_q_a', '静态电流', 'mA', 1e-3]];

function keyed(base, keys) {
  return keys.map(([k, label, unit, scale]) => ({ path: `${base}.${k}`, label, unit, scale }));
}

/** Power network cards (PCB 元件 page): sources, decoupling nodes, branches, regulators, MCU draw. */
export function powerCards() {
  const d = store.project?.data || {};
  const pw = d.power || {};
  const cards = [];
  for (const name of Object.keys(pw.sources || {})) cards.push(paramCard(`电源 ${name}`, keyed(`power.sources.${name}`, SOURCE_KEYS), { sub: '供电输入' }));
  for (const name of Object.keys(pw.regulators || {})) cards.push(paramCard(`稳压 ${name}`, keyed(`power.regulators.${name}`, REG_KEYS), { sub: pw.regulators[name].type === 'ldo' ? '线性稳压（LDO）' : '' }));
  for (const name of Object.keys(pw.branches || {})) cards.push(paramCard(`支路 ${name}`, keyed(`power.branches.${name}`, BRANCH_KEYS), { sub: `${pw.branches[name].from} → ${pw.branches[name].to}` }));
  const nodes = Object.keys(pw.nodes || {});
  if (nodes.length) {
    cards.push(paramCard('网络上的电容', nodes.map((n) => ({ path: `power.nodes.${n}.c_f`, label: n, unit: 'µF', scale: 1e-6 })), { sub: '储能电容越大，舵机启动时电压掉得越少' }));
  }
  cards.push(paramCard('单片机', [
    { path: 'mcu.current.run_a', label: '工作电流', unit: 'mA', scale: 1e-3 },
    { path: 'mcu.current.reset_a', label: '复位时电流', unit: 'mA', scale: 1e-3 },
    { path: 'mcu.brownout.v_trip', label: '欠压复位电压', unit: 'V' },
    { path: 'mcu.brownout.v_release', label: '恢复电压', unit: 'V' },
  ], { sub: (d.mcu?.type || '').toUpperCase() }));
  return cards.filter(Boolean);
}
