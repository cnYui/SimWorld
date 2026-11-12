// Small DOM helpers, icons, formatting, toasts and dialogs.

export function h(tag, attrs, ...children) {
  const el = tag === 'svg' || attrs?.svg ? document.createElementNS('http://www.w3.org/2000/svg', tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false || k === 'svg') continue;
    if (k === 'class') el.setAttribute('class', v);
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k === 'html') el.innerHTML = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'value' && 'value' in el) el.value = v;
    else if (k === 'checked' || k === 'disabled' || k === 'selected' || k === 'open') el[k] = !!v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) append(el, c);
    else el.append(c instanceof Node ? c : String(c));
  }
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
// clear() returns a helper whose append() accepts arrays and skips null/false like h()
export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return { el, append: (...c) => { append(el, c); return el; } };
}
export function add(el, ...c) { append(el, c); return el; }
export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ------------------------------------------------------------------ icons
// 24×24 stroke icons, drawn for this app.
const ICONS = {
  logo: '<path d="M4 7h4l2-3 4 16 2-6h4"/><circle cx="20" cy="14" r="1.2"/>',
  home: '<path d="M4 11l8-6 8 6"/><path d="M6 10v9h12v-9"/><path d="M10 19v-5h4v5"/>',
  overview: '<rect x="4" y="4" width="7" height="7" rx="1.2"/><rect x="13" y="4" width="7" height="4" rx="1.2"/><rect x="13" y="10" width="7" height="10" rx="1.2"/><rect x="4" y="13" width="7" height="7" rx="1.2"/>',
  sliders: '<path d="M5 5v14M12 5v14M19 5v14"/><rect x="3" y="13" width="4" height="3" rx="1"/><rect x="10" y="7" width="4" height="3" rx="1"/><rect x="17" y="11" width="4" height="3" rx="1"/>',
  scenario: '<path d="M6 3h9l4 4v14H6z"/><path d="M15 3v4h4"/><path d="M10 11l5 3-5 3z"/>',
  results: '<path d="M4 4v16h16"/><path d="M7 15l3-4 3 2 5-6"/>',
  sweep: '<rect x="4" y="4" width="16" height="16" rx="1.5"/><path d="M4 9.3h16M4 14.6h16M9.3 4v16M14.6 4v16"/><path d="M5.5 18.5l3-3" stroke-width="2.2"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2.8v2.4M12 18.8v2.4M2.8 12h2.4M18.8 12h2.4M5.5 5.5l1.7 1.7M16.8 16.8l1.7 1.7M5.5 18.5l1.7-1.7M16.8 7.2l1.7-1.7"/>',
  chat: '<path d="M4 5h16v11H9l-5 4z"/><path d="M8 9h8M8 12h5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  play: '<path d="M7 5l12 7-12 7z"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="1.5"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7"/>',
  trash: '<path d="M4 7h16M10 7V4h4v3M6 7l1 13h10l1-13"/>',
  refresh: '<path d="M19 8a8 8 0 1 0 1 6"/><path d="M20 3v5h-5"/>',
  search: '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5"/>',
  alert: '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17v.3"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.3"/>',
  folder: '<path d="M3 6.5h6l2 2h10v10.5H3z"/>',
  file: '<path d="M6 3h9l4 4v14H6z"/><path d="M15 3v4h4"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v6H4V6h6"/>',
  compare: '<path d="M8 4v16M16 4v16"/><path d="M4 8h4M4 14h4M16 10h4M16 17h4"/>',
  cube: '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M12 12l8-4.5M12 12v9M12 12L4 7.5"/>',
  chip: '<rect x="6" y="6" width="12" height="12" rx="1.5"/><path d="M9 3v3M12 3v3M15 3v3M9 18v3M12 18v3M15 18v3M3 9h3M3 12h3M3 15h3M18 9h3M18 12h3M18 15h3"/>',
  board: '<rect x="3" y="5" width="18" height="14" rx="1.5"/><circle cx="7.5" cy="9.5" r="1.3"/><path d="M11 9.5h6M7.5 11v4h9"/>',
  gear: '<circle cx="12" cy="12" r="3.2"/><path d="M12 3l1.6 2.6 3-.6.6 3L20 9.6 18.6 12 20 14.4l-2.8 1.6-.6 3-3-.6L12 21l-1.6-2.6-3 .6-.6-3L4 14.4 5.4 12 4 9.6l2.8-1.6.6-3 3 .6z"/>',
  bolt: '<path d="M13 3L5 14h6l-1 7 8-11h-6z"/>',
  motor: '<circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="2"/><path d="M12 5v2M12 17v2M5 12h2M17 12h2"/>',
  pin: '<path d="M8 4h8M10 4v6l-3 4h10l-3-4V4M12 14v6"/>',
  button: '<rect x="4" y="8" width="16" height="10" rx="2"/><path d="M9 8V6h6v2"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="1.5"/><path d="M16 8V4H4v12h4"/>',
  chevron: '<path d="M9 6l6 6-6 6"/>',
  down: '<path d="M6 9l6 6 6-6"/>',
  send: '<path d="M4 12l16-8-6 16-3-6z"/><path d="M11 14l9-10"/>',
  spark: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6"/>',
  undo: '<path d="M9 14L4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/>',
  save: '<path d="M5 4h11l3 3v13H5z"/><path d="M8 4v5h7V4M8 20v-6h8v6"/>',
  panel: '<rect x="3" y="4" width="18" height="16" rx="1.5"/><path d="M15 4v16"/>',
  moon: '<path d="M19 14.5A8 8 0 0 1 9.5 5a7 7 0 1 0 9.5 9.5z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
  import: '<path d="M12 4v11M7 10l5 5 5-5"/><path d="M4 17v3h16v-3"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  wave: '<path d="M3 12h3l2-6 4 12 3-9 2 3h4"/>',
  printer: '<path d="M7 9V4h10v5"/><rect x="3" y="9" width="18" height="8" rx="1.5"/><path d="M7 14h10v6H7z"/><path d="M17.5 11.5h.5"/>',
  user: '<circle cx="12" cy="8" r="3.5"/><path d="M5 20c1-4 4-5.5 7-5.5s6 1.5 7 5.5"/>',
};

export function icon(name, cls = '') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'i ' + cls);
  svg.setAttribute('aria-hidden', 'true');
  svg.innerHTML = ICONS[name] || ICONS.info;
  return svg;
}

// ------------------------------------------------------------------ formatting
export function fmtNum(v, digits = 4) {
  if (typeof v !== 'number' || !isFinite(v)) return v === null || v === undefined ? '—' : String(v);
  if (Number.isInteger(v)) return String(v);
  const a = Math.abs(v);
  if (a !== 0 && (a < 1e-3 || a >= 1e6)) return v.toExponential(3).replace(/\.?0+e/, 'e');
  return String(parseFloat(v.toPrecision(digits)));
}

const PREFIX = [[1e9, 'G'], [1e6, 'M'], [1e3, 'k'], [1, ''], [1e-3, 'm'], [1e-6, 'µ'], [1e-9, 'n'], [1e-12, 'p']];
export function fmtSI(v, unit = '') {
  if (typeof v !== 'number' || !isFinite(v)) return String(v ?? '—');
  if (v === 0) return '0 ' + unit;
  const a = Math.abs(v);
  for (const [f, p] of PREFIX) {
    if (a >= f * 0.9995) return `${parseFloat((v / f).toPrecision(4))} ${p}${unit}`;
  }
  return `${v.toExponential(3)} ${unit}`;
}

export function fmtValue(v) {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'number') return fmtNum(v);
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'string') return v;
  return JSON.stringify(v);
}

// unit guess from a parameter / signal name
export function unitOf(name) {
  const k = String(name).split('.').pop();
  if (/_ohm$/.test(k)) return 'Ω';
  if (/(^|_)c_f$|_f$/.test(k) && !/_hz$/.test(k)) return 'F';
  if (/_a$/.test(k) || /^i(_|$)/.test(k) || /(^|\.)i_\w+$/.test(name)) return 'A';
  if (/(^|_)v$|_v$|^v_/.test(k) || k === 'v') return 'V';
  if (/_h$/.test(k)) return 'H';
  if (/_hz$/.test(k)) return 'Hz';
  if (/_deg$|^deg$/.test(k) || k === 'deg') return '°';
  if (/_deg_s$|^dps$/.test(k)) return '°/s';
  if (/_ms$/.test(k)) return 'ms';
  if (/_us$/.test(k)) return 'µs';
  if (/_s$/.test(k)) return 's';
  if (/_n$/.test(k)) return 'N';
  if (/_m$/.test(k)) return 'm';
  if (/_mm$|^mm$/.test(k)) return 'mm';
  if (/_kg$/.test(k)) return 'kg';
  return '';
}

export function relTime(ts) {
  const d = (Date.now() / 1000 - ts);
  if (d < 60) return '刚刚';
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false });
}

export function parseRunTime(created) {
  // "2026-11-12 09:56:53"
  const t = Date.parse(String(created).replace(' ', 'T'));
  return isNaN(t) ? null : t / 1000;
}

// parse a typed value using the type of the previous value as a hint
export function parseTyped(text, hint) {
  const s = String(text).trim();
  if (typeof hint === 'number') {
    if (s === '') throw new Error('需要一个数值');
    const n = Number(s.replace(/[，,_\s]/g, ''));
    if (!isFinite(n)) throw new Error(`“${s}” 不是数值`);
    return n;
  }
  if (typeof hint === 'boolean') {
    if (/^(true|1|yes|是|on)$/i.test(s)) return true;
    if (/^(false|0|no|否|off)$/i.test(s)) return false;
    throw new Error('需要 true 或 false');
  }
  if (Array.isArray(hint) || (hint && typeof hint === 'object')) {
    try { return JSON.parse(s); } catch (e) { throw new Error('需要 JSON，例如 [1, 0, 0]'); }
  }
  if (hint === undefined || hint === null) return parseLoose(s);
  return s;
}

export function parseLoose(s) {
  s = String(s).trim();
  if (s === '') return '';
  if (/^(true|false)$/i.test(s)) return s.toLowerCase() === 'true';
  if (/^[-+]?(\d+\.?\d*|\.\d+)(e[-+]?\d+)?$/i.test(s)) return Number(s);
  if (/^[[{]/.test(s)) { try { return JSON.parse(s); } catch (e) { /* plain string */ } }
  return s;
}

export function sameValue(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

// ------------------------------------------------------------------ toasts
export function toast(msg, { kind = '', action, timeout = 4500 } = {}) {
  const box = document.getElementById('toasts');
  const el = h('div', { class: 'toast ' + kind, role: kind === 'bad' ? 'alert' : 'status' }, h('span', {}, msg));
  const close = () => el.remove();
  if (action) el.append(h('button', { onclick: () => { close(); action.fn(); } }, action.label));
  el.append(h('button', { class: 'x', 'aria-label': '关闭', onclick: close }, icon('x', 'sm')));
  box.append(el);
  while (box.children.length > 4) box.firstChild.remove();
  if (timeout) setTimeout(close, timeout);
  return el;
}
export const toastError = (e) => toast(e?.message || String(e), { kind: 'bad', timeout: 7000 });

// ------------------------------------------------------------------ dialogs
export function modal({ title, body, actions = [], width }) {
  return new Promise((resolve) => {
    const dlg = h('dialog', { class: 'modal', style: width ? { width } : undefined });
    const done = (v) => { dlg.close(); dlg.remove(); resolve(v); };
    const footer = h('footer', {});
    for (const a of actions) {
      footer.append(h('button', {
        class: 'btn ' + (a.kind || ''), type: 'button',
        onclick: async () => {
          if (a.handler) {
            try { const r = await a.handler(); if (r === false) return; done(r === undefined ? a.value : r); } catch (e) { toastError(e); }
          } else done(a.value);
        },
      }, a.label));
    }
    dlg.append(h('header', {}, title), h('div', { class: 'body' }, body), footer);
    dlg.addEventListener('cancel', (e) => { e.preventDefault(); done(null); });
    document.body.append(dlg);
    dlg.showModal();
    const first = dlg.querySelector('.body input, .body textarea, .body select');
    (first || footer.lastChild)?.focus();
  });
}

export function confirmDialog(title, text, okLabel = '确定', danger = false) {
  return modal({
    title, body: h('div', {}, text),
    actions: [{ label: '取消', value: false }, { label: okLabel, value: true, kind: danger ? 'danger' : 'primary' }],
  });
}

// path input with native picker when running inside the desktop shell
export function pathField({ label, value = '', kind = 'folder', placeholder = '' }) {
  const input = h('input', { class: 'input mono grow', value, placeholder, spellcheck: 'false' });
  const row = h('div', { class: 'row' }, input);
  const pw = window.pywebview?.api;
  if (pw && (kind === 'folder' ? pw.pick_folder : pw.pick_file)) {
    row.append(h('button', {
      class: 'btn', type: 'button', onclick: async () => {
        try {
          const p = kind === 'folder' ? await pw.pick_folder() : await pw.pick_file(kind);
          if (p) input.value = Array.isArray(p) ? p[0] : p;
        } catch (e) { toastError(e); }
      },
    }, icon('folder'), '浏览…'));
  }
  const el = h('label', { class: 'field' }, label, row);
  return { el, input, get value() { return input.value.trim(); } };
}

export async function nativePick(kind) {
  const pw = window.pywebview?.api;
  if (!pw) return undefined;
  try {
    const p = kind === 'folder' ? await pw.pick_folder() : await pw.pick_file(kind);
    return Array.isArray(p) ? p[0] : p;
  } catch (e) { return undefined; }
}

export function copyText(text, message) {
  const done = () => toast(message || `已复制：${text}`, { timeout: 2500 });
  if (navigator.clipboard?.writeText) navigator.clipboard.writeText(text).then(done, () => fallback());
  else fallback();
  function fallback() {
    const ta = h('textarea', { style: { position: 'fixed', opacity: 0 } }, text);
    document.body.append(ta); ta.select();
    try { document.execCommand('copy'); done(); } catch (e) { /* ignore */ }
    ta.remove();
  }
}

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
export const SERIES = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8'];
export function seriesColor(i) { return cssVar(SERIES[i % SERIES.length]); }

// popover anchored under an element; closes on outside click / Escape
export function popover(anchor, content, { onClose } = {}) {
  const pop = h('div', { class: 'popover' }, content);
  document.body.append(pop);
  const r = anchor.getBoundingClientRect();
  const w = pop.offsetWidth;
  pop.style.left = Math.max(8, Math.min(r.left, innerWidth - w - 8)) + 'px';
  pop.style.top = Math.min(r.bottom + 4, innerHeight - pop.offsetHeight - 8) + 'px';
  const close = () => {
    pop.remove();
    document.removeEventListener('mousedown', outside, true);
    document.removeEventListener('keydown', key, true);
    onClose?.();
  };
  const outside = (e) => { if (!pop.contains(e.target) && !anchor.contains(e.target)) close(); };
  const key = (e) => { if (e.key === 'Escape') { close(); anchor.focus(); } };
  setTimeout(() => {
    document.addEventListener('mousedown', outside, true);
    document.addEventListener('keydown', key, true);
  });
  return { el: pop, close };
}
