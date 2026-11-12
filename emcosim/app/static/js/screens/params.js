// 参数：project.yaml 中所有可编辑参数，行内修改即写回（POST /api/param）。
import { post } from '../api.js';
import { store, getPath } from '../store.js';
import { h, icon, clear, fmtSI, fmtValue, unitOf, parseTyped, sameValue, toast, toastError, copyText, debounce, confirmDialog } from '../ui.js';

const SECTIONS = ['power', 'actuators', 'mechanics', 'mcu', 'inputs', 'printing'];
const READONLY = new Set(['mechanics.mjcf', 'mcu.firmware.flash', 'mcu.firmware.size_bytes', 'mcu.type']);
const SKIP = (path) => path === 'mcu.firmware.params' || path.startsWith('mechanics.probes.');

export function createParams(ctx) {
  const search = h('input', { class: 'input', placeholder: '搜索路径、数值或备注…', style: { width: '260px' }, 'aria-label': '搜索参数' });
  const onlyChanged = h('button', { class: 'btn', 'aria-pressed': 'false' }, '只看已修改');
  const changedInfo = h('span', { class: 'sub' });
  const revertAll = h('button', { class: 'btn', hidden: true }, icon('undo'), '全部还原');
  const tree = h('div', { class: 'pane-body ptree' });
  const tableBox = h('div', { class: 'screen-body', style: { background: 'var(--panel)' } });
  const el = h('section', { class: 'screen', 'aria-label': '参数' },
    h('div', { class: 'screen-head' }, h('h1', {}, '参数'), changedInfo, h('span', { class: 'grow' }),
      h('label', { class: 'search' }, icon('search', 'sm'), search), onlyChanged, revertAll),
    h('div', { class: 'params-wrap', style: { flex: 1, minHeight: 0 } },
      h('div', { class: 'pane' }, h('div', { class: 'pane-head' }, h('span', { class: 'title' }, '分组')), tree),
      tableBox));

  let filterChanged = false;
  let rows = [];
  const status = {};     // path -> 'saving' | 'err'

  onlyChanged.addEventListener('click', () => {
    filterChanged = !filterChanged;
    onlyChanged.classList.toggle('on', filterChanged);
    onlyChanged.setAttribute('aria-pressed', String(filterChanged));
    render();
  });
  search.addEventListener('input', debounce(() => render(), 120));
  search.addEventListener('keydown', (e) => { if (e.key === 'Escape') { search.value = ''; render(); } });
  revertAll.addEventListener('click', async () => {
    const ch = rows.filter((r) => r.changed && r.base !== undefined);
    if (!ch.length) return;
    if (!(await confirmDialog('全部还原', `把 ${ch.length} 个已修改的参数改回本次打开工程时的值？`, '还原'))) return;
    for (const r of ch) await save(r, r.base, false);
    toast(`已还原 ${ch.length} 个参数`);
  });

  function collect() {
    const p = store.project;
    if (!p) return [];
    const d = p.data || {};
    const out = [];
    const walk = (obj, path) => {
      for (const [k, v] of Object.entries(obj || {})) {
        if (k === 'note') continue;
        const pp = `${path}.${k}`;
        if (SKIP(pp)) continue;
        if (v && typeof v === 'object' && !Array.isArray(v)) walk(v, pp);
        else out.push({ path: pp, value: v, note: obj.note });
      }
    };
    for (const sec of SECTIONS) if (d[sec]) walk(d[sec], sec);
    // actuators: preset values that can be overridden per actuator
    for (const [name, a] of Object.entries(d.actuators || {})) {
      const pre = p.presets?.[a.preset];
      if (!pre) continue;
      for (const [k, v] of Object.entries(pre)) {
        if (k in a) continue;
        out.push({ path: `actuators.${name}.${k}`, value: v, inherited: `预设 ${a.preset}` });
      }
    }
    // firmware params: defaults unless the project overrides them
    const fwp = d.mcu?.firmware?.params || {};
    for (const [k, v] of Object.entries(p.firmware_params || {})) {
      if (k in fwp) continue;
      out.push({ path: `mcu.firmware.params.${k}`, value: v, inherited: '固件默认' });
    }
    for (const r of out) {
      r.base = getPath(store.baseline, r.path);
      r.changed = !r.inherited && !sameValue(r.base, r.value);
      r.group = groupOf(r.path);
      r.readonly = READONLY.has(r.path) || /\.type$/.test(r.path);
    }
    out.sort((a, b) => (order(a.group) - order(b.group)) || (a.group < b.group ? -1 : a.group > b.group ? 1 : 0));
    return out;
  }

  function order(g) { const i = SECTIONS.indexOf(g.split('.')[0]); return i * 10 + (g.startsWith('mcu.firmware') ? 5 : 0); }

  function groupOf(path) {
    const s = path.split('.');
    if (path.startsWith('mcu.firmware.params.')) return 'mcu.firmware.params';
    if (s[0] === 'mcu') return 'mcu';
    if (s[0] === 'printing' && s[1] === 'parts') return s.length >= 4 ? `printing.parts.${s[2]}` : 'printing.parts';
    if (s[0] === 'actuators' || s[0] === 'inputs') return `${s[0]}.${s[1]}`;
    if (s[0] === 'power' || s[0] === 'mechanics') return s.length > 3 ? `${s[0]}.${s[1]}.${s[2]}` : `${s[0]}.${s[1]}`.replace(/\.[^.]*$/, s.length > 2 ? `.${s[1]}` : '');
    return s[0];
  }

  const GROUP_NAMES = {
    power: '电源', 'power.nodes': '节点', 'power.sources': '电源', 'power.branches': '支路', 'power.regulators': '稳压器',
    actuators: '执行器', mechanics: '机械', 'mechanics.elements': '机械元件', 'mechanics.joints': '关节', 'mechanics.initial_deg': '初始角度',
    'mechanics.parts': '零件', mcu: 'MCU', 'mcu.firmware.params': '固件参数', inputs: '输入', printing: '3D 打印', 'printing.parts': '打印件',
  };
  function groupLabel(g) {
    const s = g.split('.');
    if (GROUP_NAMES[g]) return GROUP_NAMES[g];
    if (s.length === 3) return `${GROUP_NAMES[s[0] + '.' + s[1]] || s[1]} · ${s[2]}`;
    if (s.length === 2) return `${GROUP_NAMES[s[0]] || s[0]} · ${s[1]}`;
    return g;
  }

  function editorFor(r) {
    const d = store.project.data;
    const nodes = Object.keys(d.power?.nodes || {});
    const regs = Object.keys(d.power?.regulators || {});
    let options = null;
    if (/^(actuators\.[^.]+|power\.sources\.[^.]+)\.node$/.test(r.path) || /^power\.branches\.[^.]+\.(from|to)$/.test(r.path) || /^power\.regulators\.[^.]+\.input$/.test(r.path)) options = nodes;
    else if (r.path === 'mcu.supply') options = [...nodes, ...regs];
    else if (/^actuators\.[^.]+\.preset$/.test(r.path)) options = Object.keys(store.project.presets || {});
    else if (/^actuators\.[^.]+\.joint$/.test(r.path) || /^mechanics\.elements\.[^.]+\.joint$/.test(r.path)) options = Object.keys(d.mechanics?.joints || {});
    else if (/^inputs\.[^.]+\.active$/.test(r.path)) options = ['low', 'high'];
    else if (/^mcu\.backend$/.test(r.path)) options = ['qemu', 'scripted'];
    else if (typeof r.value === 'boolean') options = [true, false];
    if (options) {
      const opts = options.map(String);
      if (!opts.includes(String(r.value))) opts.unshift(String(r.value));
      const sel = h('select', { class: 'input', 'data-path': r.path, disabled: r.readonly, 'aria-label': r.path },
        opts.map((o) => h('option', { value: o, selected: o === String(r.value) }, o)));
      sel.addEventListener('change', () => {
        const v = typeof r.value === 'boolean' ? sel.value === 'true' : sel.value;
        save(r, v);
      });
      return sel;
    }
    const shown = Array.isArray(r.value) || (r.value && typeof r.value === 'object') ? JSON.stringify(r.value) : fmtValue(r.value);
    const inp = h('input', { class: 'input' + (r.inherited ? ' inherited' : ''), value: r.value === null ? '' : String(Array.isArray(r.value) ? JSON.stringify(r.value) : r.value), 'data-path': r.path,
      readonly: r.readonly, spellcheck: 'false', 'aria-label': r.path, title: r.inherited ? `${r.inherited}：${shown}（修改后写入工程覆盖）` : shown });
    inp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); commit(); inp.blur(); }
      if (e.key === 'Escape') { inp.value = String(Array.isArray(r.value) ? JSON.stringify(r.value) : r.value ?? ''); inp.blur(); }
    });
    inp.addEventListener('blur', commit);
    function commit() {
      if (r.readonly) return;
      const cur = String(Array.isArray(r.value) ? JSON.stringify(r.value) : r.value ?? '');
      if (inp.value.trim() === cur.trim() || inp.value === inp.dataset.sent) return;
      inp.dataset.sent = inp.value;      // Enter + blur must not submit twice
      let v;
      try { v = parseTyped(inp.value, r.value); } catch (e) { toastError(new Error(`${r.path}：${e.message}`)); inp.value = cur; return; }
      save(r, v);
    }
    return inp;
  }

  // one write at a time: the backend rewrites the whole project.yaml per request
  let chain = Promise.resolve();
  function save(r, v, notify = true) {
    const p = chain.then(() => doSave(r, v, notify));
    chain = p.catch(() => {});
    return p;
  }
  async function doSave(r, v, notify) {
    status[r.path] = 'saving';
    markRow(r.path);
    try {
      const res = await post('param', { path: r.path, value: v });
      setLocal(res.path || r.path, v);
      delete status[r.path];
      if (notify) toast(`${r.path} = ${fmtValue(v)}（已写入 project.yaml）`, { timeout: 2500 });
      render();
    } catch (e) {
      status[r.path] = 'err';
      markRow(r.path);
      toastError(e);
    }
  }

  function setLocal(path, v) {
    const parts = path.split('.');
    let o = store.project.data;
    for (const k of parts.slice(0, -1)) o = (o[k] ||= {});
    o[parts.at(-1)] = v;
  }

  function markRow(path) {
    const tr = tableBox.querySelector(`tr[data-path="${CSS.escape(path)}"]`);
    if (!tr) return;
    tr.classList.toggle('saving', status[path] === 'saving');
    tr.classList.toggle('err', status[path] === 'err');
  }

  function render() {
    if (!store.project) { clear(tableBox).append(h('div', { class: 'empty' }, '还没有打开工程')); clear(tree); return; }
    const focusPath = document.activeElement?.dataset?.path;
    const selStart = document.activeElement?.selectionStart;
    rows = collect();
    const q = search.value.trim().toLowerCase();
    const shown = rows.filter((r) => (!filterChanged || r.changed) &&
      (!q || r.path.toLowerCase().includes(q) || String(fmtValue(r.value)).toLowerCase().includes(q) || (r.note || '').toLowerCase().includes(q) || (r.inherited || '').includes(q)));
    const nChanged = rows.filter((r) => r.changed).length;
    changedInfo.textContent = `${rows.length} 项${nChanged ? ` · 已修改 ${nChanged} 项` : ''}`;
    revertAll.hidden = !nChanged;
    // tree
    const groups = [...new Set(rows.map((r) => r.group))];
    clear(tree).append(groups.map((g) => {
      const n = rows.filter((r) => r.group === g).length, c = rows.filter((r) => r.group === g && r.changed).length;
      return h('button', { class: 'grp' + (g.split('.').length > 1 ? ' l2' : ''), onclick: () => {
        const tr = tableBox.querySelector(`tr.gh[data-group="${CSS.escape(g)}"]`);
        if (tr) tableBox.scrollTop = tr.offsetTop;
        else { search.value = ''; filterChanged = false; onlyChanged.classList.remove('on'); render(); tableBox.querySelector(`tr.gh[data-group="${CSS.escape(g)}"]`)?.scrollIntoView(); }
      } }, groupLabel(g), h('span', { class: 'c' }, c ? h('span', { class: 'warn-t' }, `${c}/`) : null, n));
    }));
    // table
    const tbody = h('tbody');
    let lastGroup = null;
    for (const r of shown) {
      if (r.group !== lastGroup) {
        lastGroup = r.group;
        tbody.append(h('tr', { class: 'gh', 'data-group': r.group }, h('td', { colspan: 4 }, groupLabel(r.group), h('span', { class: 'mono dim', style: { marginLeft: '8px', fontWeight: 400 } }, r.group))));
      }
      const unit = unitOf(r.path);
      const pre = r.path.slice(0, r.path.lastIndexOf('.') + 1), leaf = r.path.slice(pre.length);
      const info = [];
      if (r.changed) info.push(h('span', { class: 'orig' }, `原值 ${fmtValue(r.base)} `),
        r.base !== undefined ? h('button', { class: 'btn sm ghost', onclick: () => save(r, r.base), title: '还原为打开工程时的值' }, icon('undo', 'sm'), '还原') : null);
      if (r.inherited) info.push(h('span', { class: 'dim' }, r.inherited));
      if (typeof r.value === 'number' && (unit === 'F' || unit === 'H' || (unit === 'A' && Math.abs(r.value) < 0.1)) && r.value !== 0) info.push(h('span', { class: 'dim mono' }, ` = ${fmtSI(r.value, unit)}`));
      if (r.note && r.path.split('.').length && rowIsFirstOfNote(r, shown)) {
        info.push(h('div', { class: 'note-inline', title: r.note }, /assum|假设/i.test(r.note) ? h('span', { class: 'assume' }, '假设') : null, r.note));
      }
      tbody.append(h('tr', { 'data-path': r.path, class: (r.changed ? 'changed ' : '') + (status[r.path] || '') },
        h('td', { class: 'path' }, h('span', { class: 'pre' }, pre), h('span', {}, leaf),
          h('button', { class: 'btn icon sm ghost copy', title: '复制路径（AI 对话里用同样的路径）', 'aria-label': `复制 ${r.path}`, onclick: () => copyText(r.path) }, icon('copy', 'sm'))),
        h('td', { class: 'val' }, editorFor(r)),
        h('td', { class: 'unit' }, unit),
        h('td', { class: 'info' }, info)));
    }
    clear(tableBox).append(shown.length ? h('table', { class: 'ptable' }, h('colgroup', {}, h('col', { class: 'c-path' }), h('col', { class: 'c-val' }), h('col', { class: 'c-unit' }), h('col', { class: 'c-info' })), tbody)
      : h('div', { class: 'empty' }, filterChanged ? '没有已修改的参数' : '没有匹配的参数'));
    if (focusPath) {
      const f = tableBox.querySelector(`[data-path="${CSS.escape(focusPath)}"]`);
      if (f) { f.focus(); try { if (selStart != null) f.setSelectionRange(selStart, selStart); } catch (e) { /* select */ } }
    }
  }

  function rowIsFirstOfNote(r, shown) {
    const parent = r.path.slice(0, r.path.lastIndexOf('.'));
    const first = shown.find((x) => x.note === r.note && x.path.slice(0, x.path.lastIndexOf('.')) === parent);
    return first === r;
  }

  let stale = true;
  store.on('project', () => { if (!el.hidden) render(); else stale = true; });
  return {
    el,
    show(arg) {
      if (arg) { search.value = arg; filterChanged = false; onlyChanged.classList.remove('on'); }
      render(); stale = false;
      if (arg) setTimeout(() => tableBox.querySelector(`tr[data-path^="${CSS.escape(arg)}"] .input`)?.focus(), 30);
    },
  };
}
