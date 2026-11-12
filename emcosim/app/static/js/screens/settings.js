// 设置：AI 助手后端（本机 Claude Code / API Key）、登录状态、外观。
import { post } from '../api.js';
import { store } from '../store.js';
import { h, icon, clear, toast, toastError, confirmDialog } from '../ui.js';

const MODELS = ['claude-opus-5-5'];
const EFFORTS = [['low', '低（更快更省）'], ['medium', '中（默认）'], ['high', '高'], ['max', '最高']];

const logLines = [];

export function createSettings(ctx) {
  const body = h('div', { class: 'screen-body' });
  const el = h('section', { class: 'screen', 'aria-label': '设置' },
    h('div', { class: 'screen-head' }, h('h1', {}, '设置'), h('span', { class: 'grow' }),
      h('button', { class: 'btn icon ghost', title: '重新检测', 'aria-label': '重新检测', onclick: () => refresh() }, icon('refresh'))),
    body);

  let busy = false;
  const logEl = h('pre', { class: 'log mono', hidden: true });

  async function refresh() {
    await Promise.all([store.loadChatStatus(), store.loadApp()]);
  }

  async function saveSettings(patch) {
    try {
      await post('settings', patch);
      await refresh();
      toast('设置已保存', { timeout: 1600 });
    } catch (e) { toastError(e); }
  }

  async function login(console = false) {
    busy = true; render();
    logLines.length = 0;
    try {
      const r = await post('chat/login', { console });
      if (r.running) toast('登录窗口已经打开了，请在浏览器里完成登录');
      else toast('已在浏览器打开 Claude 登录页面，完成后这里会自动刷新', { timeout: 8000 });
    } catch (e) { toastError(e); }
    busy = false; render();
  }

  async function logout() {
    if (!(await confirmDialog('退出登录', '退出本机 Claude Code 的登录？这会影响这台电脑上所有使用 Claude Code 的程序。', '退出登录', true))) return;
    try { await post('chat/logout'); await store.loadChatStatus(); toast('已退出登录'); } catch (e) { toastError(e); }
  }

  function render() {
    const cs = store.chatStatus;
    const s = store.app?.settings || {};
    const backend = cs?.backend || s.chat_backend || 'claude_code';
    const cc = cs?.claude_code || {};
    const theme = (() => { try { return localStorage.getItem('emcosim.theme') || 'system'; } catch (e) { return 'system'; } })();

    const pick = (val, title, desc) => h('label', { class: backend === val ? 'on' : '' },
      h('input', { type: 'radio', name: 'backend', value: val, checked: backend === val, onchange: () => saveSettings({ chat_backend: val }) }),
      h('div', {}, h('div', { class: 't' }, title), h('div', { class: 'd' }, desc)));

    const keyInput = h('input', { class: 'input mono grow', type: 'password', placeholder: s.has_api_key ? '已保存（输入新的 Key 可替换）' : 'sk-ant-…', autocomplete: 'off', 'aria-label': 'Anthropic API Key' });
    const modelInput = h('input', { class: 'input mono', list: 'models', value: s.model || 'claude-opus-5-5', style: { width: '220px' }, 'aria-label': '模型' });
    modelInput.addEventListener('change', () => saveSettings({ model: modelInput.value.trim() }));
    const effortSel = h('select', { class: 'input', 'aria-label': '思考强度' }, EFFORTS.map(([v, l]) => h('option', { value: v, selected: v === (s.effort || 'medium') }, l)));
    effortSel.addEventListener('change', () => saveSettings({ effort: effortSel.value }));
    const pathInput = h('input', { class: 'input mono grow', value: s.claude_path || '', placeholder: '留空自动查找（PATH、%USERPROFILE%\\.local\\bin、npm 全局目录）', 'aria-label': 'claude 可执行文件路径' });
    pathInput.addEventListener('change', () => saveSettings({ claude_path: pathInput.value.trim() }));

    logEl.hidden = !logLines.length;
    logEl.textContent = logLines.join('\n');

    clear(body).append(h('div', { class: 'set-wrap' },
      h('div', { class: 'card' },
        h('header', {}, h('h2', {}, icon('sun'), '外观')),
        h('div', { class: 'body row' }, h('span', { class: 'muted' }, '主题'),
          h('div', { class: 'seg-ctl', role: 'group', 'aria-label': '主题' },
            [['system', '跟随系统'], ['light', '浅色'], ['dark', '深色']].map(([v, l]) => h('button', { class: theme === v ? 'on' : '', 'aria-pressed': String(theme === v), onclick: () => setTheme(v) }, l))))),
      h('div', { class: 'card' },
        h('header', {}, h('h2', {}, icon('info'), '关于')),
        h('div', { class: 'body kv' },
          h('span', { class: 'k' }, '版本'), h('span', { class: 'v' }, store.app?.version || '—'),
          h('span', { class: 'k' }, '工程'), h('span', { class: 'v' }, store.app?.project || '—'),
          h('span', { class: 'k' }, 'QEMU'), h('span', { class: 'v' }, store.app?.qemu?.found ? store.app.qemu.path : (store.app?.qemu?.error || '未找到')),
          h('span', { class: 'k' }, '许可'), h('span', { class: 'v' }, 'Apache-2.0；QEMU 部分为 GPL，独立进程')))));
  }

  function setTheme(v) {
    try { if (v === 'system') localStorage.removeItem('emcosim.theme'); else localStorage.setItem('emcosim.theme', v); } catch (e) { /* ignore */ }
    if (v === 'system') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = v;
    store.emit('theme', v);
    render();
  }

  store.on('chatStatus', () => { if (!el.hidden) render(); });
  store.on('app', () => { if (!el.hidden) render(); });
  store.on('auth_log', (line) => { logLines.push(line); if (logLines.length > 200) logLines.shift(); logEl.hidden = false; logEl.textContent = logLines.join('\n'); logEl.scrollTop = logEl.scrollHeight; });

  return { el, show() { render(); store.loadChatStatus(); } };
}
