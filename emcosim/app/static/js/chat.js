// Right-hand AI chat panel: conversations, streaming text, tool-call chips, stop, login banner.
import { get, post } from './api.js';
import { store } from './store.js';
import { h, icon, clear, toast, toastError } from './ui.js';
import { md } from './md.js';

const SUGGESTIONS = [
  '复现欠压重启并解释原因',
  '把舵机改接 J3 供电再跑一遍',
  '扫一下按压角 10–16° 看哪组最稳',
  '哪些参数是假设值？对结论影响最大的是哪个？',
];
const RUN_RE = /\b\d{8}-\d{6}-\d{3}-[\w.-]+/g;

export function createChat(root, wrap, ctx) {
  const convSel = h('select', { class: 'input sm', 'aria-label': '选择对话' });
  const banner = h('div', { class: 'chat-banner' });
  const msgs = h('div', { class: 'msgs', 'aria-live': 'polite' });
  const ta = h('textarea', { rows: 2, placeholder: '问点什么，或者让 AI 改参数、跑场景…（Enter 发送，Shift+Enter 换行）', 'aria-label': '给 AI 助手的消息' });
  const sendBtn = h('button', { class: 'btn primary sm' });
  const hintEl = h('span', { class: 'grow' });
  root.append(
    h('div', { class: 'chat-head' }, h('span', { class: 'title' }, icon('chat'), 'AI 助手'), h('span', { class: 'grow' }), convSel,
      h('button', { class: 'btn icon sm ghost', title: '新对话', 'aria-label': '新对话', onclick: () => newConv() }, icon('plus', 'sm')),
      h('button', { class: 'btn icon sm ghost', title: '收起（Ctrl+J）', 'aria-label': '收起对话面板', onclick: () => toggle(false) }, icon('panel', 'sm'))),
    banner, msgs,
    h('div', { class: 'composer' }, h('div', { class: 'box' }, ta, h('div', { class: 'bar' }, hintEl, sendBtn))));

  const convs = new Map();
  let cur = null;

  // ---------------------------------------------------------------- open / close
  function toggle(open) {
    const isOpen = !wrap.classList.contains('collapsed');
    const next = open === undefined ? !isOpen : open;
    wrap.classList.toggle('collapsed', !next);
    document.getElementById('railChat')?.classList.toggle('open', next);
    try { localStorage.setItem('emcosim.chatOpen', next ? '1' : '0'); } catch (e) { /* ignore */ }
    if (next) setTimeout(() => ta.focus(), 0);
    window.dispatchEvent(new Event('resize'));
  }
  let pref = null;
  try { pref = localStorage.getItem('emcosim.chatOpen'); } catch (e) { /* ignore */ }
  toggle(pref === null ? innerWidth >= 1280 : pref === '1');

  // ---------------------------------------------------------------- conversations
  async function loadList() {
    try {
      const list = await get('chat');
      for (const c of list) convs.set(c.id, c);
      if (!cur && list.length) cur = list[0].id;
    } catch (e) { /* backend without chat */ }
    renderConvSel();
    renderMsgs();
  }

  function renderConvSel() {
    const list = [...convs.values()].sort((a, b) => b.created - a.created);
    clear(convSel).append(list.length ? list.map((c) => h('option', { value: c.id, selected: c.id === cur }, (c.busy ? '● ' : '') + titleOf(c)))
      : h('option', { value: '' }, '新对话'));
    convSel.disabled = list.length < 2;
  }
  convSel.addEventListener('change', () => { cur = convSel.value; renderMsgs(); updateComposer(); });
  function titleOf(c) { const u = c.items.find((i) => i.role === 'user'); return u ? u.text.slice(0, 24) : '新对话'; }

  async function newConv() {
    const c = convs.get(cur);
    if (c && !c.items.length) { ta.focus(); return; }
    try {
      const n = await post('chat/new');
      convs.set(n.id, n); cur = n.id;
      renderConvSel(); renderMsgs(); updateComposer(); ta.focus();
    } catch (e) { toastError(e); }
  }

  // ---------------------------------------------------------------- send / stop
  async function send(text) {
    text = (text ?? ta.value).trim();
    if (!text) return;
    if (!store.app?.project) { toast('先打开一个工程', { kind: 'bad' }); return; }
    try {
      if (!cur || !convs.has(cur)) { const n = await post('chat/new'); convs.set(n.id, n); cur = n.id; renderConvSel(); }
      const c = convs.get(cur);
      if (c.busy) return;
      ta.value = ''; autosize();
      c.busy = true; updateComposer();
      await post(`chat/${cur}/send`, { text });
    } catch (e) {
      const c = convs.get(cur); if (c) c.busy = false;
      updateComposer();
      toastError(e);
    }
  }
  async function stop() {
    if (!cur) return;
    try { await post(`chat/${cur}/stop`); } catch (e) { toastError(e); }
  }

  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); send(); }
  });
  const autosize = () => { ta.style.height = 'auto'; ta.style.height = Math.min(200, ta.scrollHeight) + 'px'; };
  ta.addEventListener('input', autosize);
  sendBtn.addEventListener('click', () => { const c = convs.get(cur); c?.busy ? stop() : send(); });

  function updateComposer() {
    const c = convs.get(cur);
    const busy = !!c?.busy;
    sendBtn.replaceChildren(icon(busy ? 'stop' : 'send', 'sm'), busy ? '停止' : '发送');
    sendBtn.classList.toggle('primary', !busy);
    sendBtn.classList.toggle('danger', busy);
    sendBtn.setAttribute('aria-label', busy ? '停止生成' : '发送');
    const cs = store.chatStatus;
    hintEl.textContent = busy ? 'AI 正在处理…' : cs?.backend === 'api_key' ? `API Key · ${cs.api_key?.model || ''}` : '本机 Claude Code · 费用计入你的 Claude 套餐';
  }

  // ---------------------------------------------------------------- banner
  function renderBanner() {
    const cs = store.chatStatus;
    clear(banner);
    if (!cs || cs.error) return;
    const loginBtn = h('button', { class: 'btn sm primary', onclick: async () => {
      try { await post('chat/login'); toast('已在浏览器打开 Claude 登录页面，完成后会自动刷新', { timeout: 8000 }); store.loadChatStatus(); } catch (e) { toastError(e); }
    } }, '登录 Claude');
    if (!store.app?.project) banner.append(h('div', { class: 'banner info' }, icon('info', 'sm'), h('span', { class: 'grow' }, 'AI 助手需要先打开一个工程')));
    else if (cs.backend === 'api_key') {
      if (!cs.api_key.configured) banner.append(h('div', { class: 'banner' }, icon('alert', 'sm'), h('span', { class: 'grow' }, '还没有设置 API Key'), h('button', { class: 'btn sm', onclick: () => ctx.nav('settings') }, '去设置')));
    } else if (!cs.claude_code.found) {
      banner.append(h('div', { class: 'banner' }, icon('alert', 'sm'), h('span', { class: 'grow' }, '没有找到 Claude Code。安装后用你自己的 Claude 账号登录即可使用。'),
        h('button', { class: 'btn sm', onclick: () => window.open('https://claude.com/claude-code', '_blank') }, icon('external', 'sm'), '安装'),
        h('button', { class: 'btn sm ghost', onclick: () => ctx.nav('settings') }, '设置')));
    } else if (!cs.claude_code.logged_in) {
      banner.append(h('div', { class: 'banner' }, icon('alert', 'sm'), h('span', { class: 'grow' }, cs.claude_code.login_running ? '正在登录，请在浏览器里完成…' : 'Claude Code 还没登录'),
        cs.claude_code.login_running ? null : loginBtn));
    }
  }

  // ---------------------------------------------------------------- messages
  let streamEl = null;
  function nearBottom() { return msgs.scrollHeight - msgs.scrollTop - msgs.clientHeight < 80; }

  function renderMsgs() {
    const c = convs.get(cur);
    clear(msgs);
    streamEl = null;
    if (!c || !c.items.length) {
      msgs.append(h('div', { class: 'suggest' },
        h('div', { class: 'hello' }, 'AI 助手可以读工程、改参数、写场景、跑仿真和扫参，运行结果会出现在左边的“结果”里。试试：'),
        SUGGESTIONS.map((s) => h('button', { onclick: () => { ta.value = s; autosize(); ta.focus(); } }, icon('spark', 'sm'), s))));
      updateComposer();
      return;
    }
    for (const it of c.items) msgs.append(itemEl(it, c));
    msgs.scrollTop = msgs.scrollHeight;
    updateComposer();
  }

  function itemEl(it, c) {
    switch (it.role) {
      case 'user': return h('div', { class: 'msg-user' }, it.text);
      case 'assistant': {
        const e = h('div', { class: 'msg-ai', html: md(it.text) });
        if (!it.closed && c.busy && it === c.items.at(-1)) { e.classList.add('streaming'); streamEl = e; }
        linkRuns(e);
        return e;
      }
      case 'tool': return toolEl(it);
      case 'meta': {
        const parts = [];
        if (it.cost_usd != null) parts.push(`$${Number(it.cost_usd).toFixed(4)}`);
        if (it.duration_ms != null) parts.push(`${(it.duration_ms / 1000).toFixed(1)} s`);
        if (it.turns != null) parts.push(`${it.turns} 轮`);
        if (it.input_tokens != null) parts.push(`输入 ${it.input_tokens} tok`);
        if (it.output_tokens != null) parts.push(`输出 ${it.output_tokens} tok`);
        if (it.model) parts.push(it.model);
        return h('div', { class: 'msg-meta' }, parts.map((p) => h('span', {}, p)));
      }
      case 'notice': return h('div', { class: 'msg-notice' }, it.text);
      case 'error': return h('div', { class: 'msg-error', role: 'alert' }, it.text,
        /登录|login|auth/i.test(it.text) ? h('div', { style: { marginTop: '6px' } }, h('button', { class: 'btn sm', onclick: () => ctx.nav('settings') }, '打开设置')) : null);
      default: return h('div', { class: 'msg-notice' }, JSON.stringify(it));
    }
  }

  function argSummary(input) {
    if (!input || typeof input !== 'object') return '';
    return Object.entries(input).slice(0, 3).map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' ').slice(0, 90);
  }

  function toolEl(it) {
    const st = it.status || 'running';
    const runs = [...new Set(String(it.result || '').match(RUN_RE) || [])];
    return h('details', { class: `tool ${st}` },
      h('summary', {}, icon('gear', 'sm'), h('span', { class: 'nm' }, it.name), h('span', { class: 'arg' }, argSummary(it.input)),
        h('span', { class: 'st', title: st === 'running' ? '运行中' : st === 'ok' ? '成功' : '出错' }, st === 'running' ? h('span', { class: 'spin' }) : icon(st === 'ok' ? 'check' : 'x', 'sm'))),
      h('div', { class: 'tb' },
        h('div', { class: 'lbl' }, '输入'), h('pre', {}, JSON.stringify(it.input ?? {}, null, 2)),
        it.result !== undefined ? [h('div', { class: 'lbl' }, '结果'), h('pre', {}, it.result)] : null,
        runs.length ? h('div', { class: 'row wrap open-run' }, runs.slice(0, 3).map((r) => h('button', { class: 'btn sm', onclick: () => ctx.openRun(r) }, icon('results', 'sm'), `打开 ${r.slice(16)}`))) : null));
  }

  function linkRuns(e) {
    // turn run ids mentioned in text into links to the results screen
    const walker = document.createTreeWalker(e, NodeFilter.SHOW_TEXT);
    const nodes = [];
    const one = new RegExp(RUN_RE.source);
    while (walker.nextNode()) if (one.test(walker.currentNode.nodeValue)) nodes.push(walker.currentNode);
    for (const n of nodes) {
      const frag = document.createDocumentFragment();
      let last = 0;
      n.nodeValue.replace(RUN_RE, (m, off) => {
        frag.append(n.nodeValue.slice(last, off), h('a', { href: '#', onclick: (ev) => { ev.preventDefault(); ctx.openRun(m); } }, m));
        last = off + m.length;
      });
      frag.append(n.nodeValue.slice(last));
      n.replaceWith(frag);
    }
  }

  let raf = 0;
  function scheduleStream() {
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      const c = convs.get(cur);
      const last = c?.items.at(-1);
      if (!streamEl || !last || last.role !== 'assistant') { const stick = nearBottom(); renderMsgs(); if (!stick) return; return; }
      const stick = nearBottom();
      streamEl.innerHTML = md(last.text);
      if (stick) msgs.scrollTop = msgs.scrollHeight;
    });
  }

  // ---------------------------------------------------------------- events
  async function onEvent(d) {
    let c = convs.get(d.conversation);
    if (!c) {
      let fetched = true;
      try { c = await get(`chat/${d.conversation}`); } catch (e) { fetched = false; c = { id: d.conversation, items: [], busy: true, created: Date.now() / 1000 }; }
      if (convs.has(c.id)) c = convs.get(c.id);     // another event created it meanwhile
      convs.set(c.id, c);
      if (!cur || !convs.get(cur)?.items.length) cur = c.id;
      renderConvSel();
      if (fetched) { if (c.id === cur) renderMsgs(); return; }     // the fetched copy already includes this event
    }
    const items = c.items;
    const last = items.at(-1);
    let full = true;
    switch (d.type) {
      case 'user': items.push({ role: 'user', text: d.text }); c.busy = true; break;
      case 'delta':
        if (last && last.role === 'assistant' && !last.closed) { last.text += d.text; full = !streamEl; }
        else items.push({ role: 'assistant', text: d.text });
        break;
      case 'tool_use':
        if (last && last.role === 'assistant') last.closed = true;
        items.push({ role: 'tool', id: d.id, name: d.name, input: d.input, status: 'running' });
        break;
      case 'tool_result': {
        const t = [...items].reverse().find((i) => i.role === 'tool' && i.id === d.id);
        if (t) { t.status = d.ok ? 'ok' : 'error'; t.result = d.result; }
        break;
      }
      case 'notice': items.push({ role: 'notice', text: d.text }); break;
      case 'error': items.push({ role: 'error', text: d.text }); break;
      case 'meta': { const { conversation, type, ...rest } = d; items.push({ role: 'meta', ...rest }); break; }
      case 'done':
        c.busy = false;
        for (const i of items) if (i.role === 'assistant') i.closed = true;
        break;
    }
    if (d.type === 'user' || d.type === 'done') renderConvSel();
    if (c.id !== cur) return;
    if (!full) scheduleStream();
    else {
      const stick = nearBottom() || d.type === 'user';
      const top = msgs.scrollTop;
      renderMsgs();
      if (!stick) msgs.scrollTop = top;
    }
    if (wrap.classList.contains('collapsed') && d.type === 'done') toast('AI 助手回复了', { action: { label: '查看', fn: () => toggle(true) } });
  }

  store.on('chatStatus', () => { renderBanner(); updateComposer(); });
  store.on('app', renderBanner);
  loadList();
  renderBanner();

  return {
    el: root, onEvent, toggle,
    prefill(text) { ta.value = text; autosize(); ta.focus(); ta.setSelectionRange(text.length, text.length); },
  };
}
