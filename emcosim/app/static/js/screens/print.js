// 打印：列出工程里的打印件（printing.parts），发送到 Bambu Studio 或导出 3MF。
import { get, post } from '../api.js';
import { store } from '../store.js';
import { h, icon, clear, fmtValue, toast, toastError, copyText } from '../ui.js';

const SETTING_NAMES = {
  layer_height: '层高', initial_layer_print_height: '首层层高', wall_loops: '墙层数', top_shell_layers: '顶层数', bottom_shell_layers: '底层数',
  sparse_infill_density: '填充密度', sparse_infill_pattern: '填充图案', nozzle_diameter: '喷嘴', enable_support: '支撑', support_type: '支撑类型',
  brim_type: '裙边', filament_type: '耗材', print_speed: '速度',
};
const unitFor = (k, v) => (/height|diameter/.test(k) && typeof v === 'number' ? ' mm' : '');

export function createPrint(ctx) {
  const statusLine = h('div', { class: 'row wrap' });
  const grid = h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: '12px' } });
  const result = h('div');
  const withSettings = h('input', { type: 'checkbox' });
  const selInfo = h('span', { class: 'muted' });
  const sendBtn = h('button', { class: 'btn primary', onclick: () => send(true) }, icon('send', 'sm'), '发送到 Bambu Studio');
  const exportBtn = h('button', { class: 'btn', onclick: () => send(false) }, icon('import', 'sm'), '只导出 3MF');
  const el = h('section', { class: 'screen', 'aria-label': '打印' },
    h('div', { class: 'screen-head' }, h('h1', {}, '打印'), h('span', { class: 'sub' }, '把机械零件发到拓竹 Bambu Studio 切片'), h('span', { class: 'grow' }),
      h('button', { class: 'btn icon ghost', title: '刷新', 'aria-label': '刷新', onclick: () => load() }, icon('refresh'))),
    h('div', { class: 'screen-body' }, h('div', { class: 'pad col', style: { gap: '12px', maxWidth: '1300px' } },
      h('div', { class: 'card' }, h('div', { class: 'body col' },
        statusLine,
        h('div', { class: 'row wrap' },
          h('button', { class: 'btn sm', onclick: () => setAll(true) }, '全选'),
          h('button', { class: 'btn sm ghost', onclick: () => setAll(false) }, '全不选'),
          selInfo, h('span', { class: 'grow' }),
          h('label', { class: 'check', title: '把每个零件的推荐层高、墙层数、填充写进 3MF 的对象设置里；Bambu Studio 版本不同可能不认' }, withSettings, '附带推荐打印参数（实验性）'),
          exportBtn, sendBtn),
        result)),
      grid)));

  let data = null;
  const selected = new Set();

  async function load() {
    try {
      data = await get('printing');
    } catch (e) {
      data = null;
      clear(grid);
      clear(statusLine).append(h('div', { class: 'banner bad grow' }, icon('alert', 'sm'), h('span', { class: 'grow' }, e.status === 404 ? '后台还没有打印接口（需要更新 emcosim）' : e.message)));
      return;
    }
    if (!selected.size) for (const p of data.parts || []) if (p.exists !== false) selected.add(p.name);
    render();
  }

  function setAll(on) {
    selected.clear();
    if (on) for (const p of data?.parts || []) if (p.exists !== false) selected.add(p.name);
    render();
  }

  function render() {
    if (!data) return;
    const b = data.bambu || {};
    clear(statusLine).append(
      h('span', { class: 'led ' + (b.found ? 'ok' : 'warn') }),
      b.found ? h('span', {}, 'Bambu Studio 已找到 ', h('span', { class: 'mono dim' }, b.path || (b.url_handler ? '（通过 bambustudio:// 链接打开）' : '')))
        : h('span', {}, 'Bambu Studio 没有找到', b.url_handler ? '（但可以用 bambustudio:// 链接打开）' : '，可以先“只导出 3MF”，再手动拖进切片软件'));
    const parts = data.parts || [];
    selInfo.textContent = `已选 ${selected.size} / ${parts.length} 个零件`;
    sendBtn.disabled = exportBtn.disabled = !selected.size;
    clear(grid).append(parts.length ? parts.map(card) : h('div', { class: 'empty' }, 'project.yaml 里还没有 printing.parts'));
  }

  function card(p) {
    const missing = p.exists === false;
    const cb = h('input', { type: 'checkbox', checked: selected.has(p.name), disabled: missing, 'aria-label': `选择 ${p.label || p.name}`,
      onchange: () => { cb.checked ? selected.add(p.name) : selected.delete(p.name); render(); } });
    const settings = Object.entries(p.settings || {});
    return h('div', { class: 'card', style: selected.has(p.name) ? { borderColor: 'var(--accent)' } : (missing ? { opacity: 0.65 } : {}) },
      h('header', {}, h('label', { class: 'check' }, cb, h('h2', {}, p.label || p.name)),
        p.material ? h('span', { class: 'chip accent', style: { marginLeft: 'auto' } }, p.material) : null),
      h('div', { class: 'body col', style: { gap: '6px' } },
        h('div', { class: 'mono dim', style: { fontSize: '11.5px', overflowWrap: 'anywhere' }, title: p.file }, p.name, ' · ', (p.file || '').split(/[\\/]/).pop()),
        missing ? h('div', { class: 'note' }, icon('alert', 'sm'), '找不到模型文件：', h('span', { class: 'mono' }, p.file)) : null,
        settings.length ? h('div', { class: 'kv' }, settings.map(([k, v]) => [
          h('span', { class: 'k', title: k }, SETTING_NAMES[k] || k), h('span', { class: 'v' }, fmtValue(v) + unitFor(k, v))])) : h('div', { class: 'hint' }, '没有推荐参数'),
        p.note ? h('div', { class: 'note info' }, icon('info', 'sm'), p.note) : null,
        h('a', { class: 'hint', href: '#', onclick: (e) => { e.preventDefault(); ctx.nav('params', `printing.parts.${p.name}`); } }, '在参数页修改')));
  }

  async function send(open) {
    if (!selected.size) return;
    clear(result).append(h('div', { class: 'hint' }, open ? '正在导出并打开 Bambu Studio…' : '正在导出 3MF…'));
    try {
      const all = (data?.parts || []).filter((p) => p.exists !== false).length;
      const body = { with_settings: withSettings.checked, open };
      if (selected.size !== all) body.names = [...selected];
      const r = await post('printing/send', body);
      const how = r.opened?.method === 'executable' ? '已用 Bambu Studio 打开' : r.opened?.method === 'url_scheme' ? '已通过 bambustudio:// 链接打开' : open ? '已导出' : '已导出';
      clear(result).append(h('div', { class: 'banner info' }, icon('check', 'sm'),
        h('span', { class: 'grow' }, `${how}：${Array.isArray(r.objects) ? r.objects.length : r.objects ?? ''} 个零件${r.with_settings ? '（附带推荐参数）' : ''}  `, h('span', { class: 'mono' }, r.file)),
        h('button', { class: 'btn sm', onclick: () => copyText(r.file) }, icon('copy', 'sm'), '复制路径')));
      toast(open ? how : `3MF 已导出：${r.file}`, { timeout: 6000 });
    } catch (e) {
      clear(result).append(h('div', { class: 'banner bad' }, icon('alert', 'sm'), h('span', { class: 'grow', style: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }, html: linkify(e.message) })));
    }
  }

  function linkify(text) {
    const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    return esc(text).replace(/https?:\/\/[^\s<]+/g, (u) => `<a href="${u}" target="_blank" rel="noopener">${u}</a>`);
  }

  store.on('project', () => { if (!el.hidden) load(); });
  return { el, show: load };
}
