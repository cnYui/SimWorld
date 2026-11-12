// Token handling, JSON API calls and the Server-Sent Events stream.
const KEY = 'emcosim.token';
let token = null;

export function initToken() {
  const m = location.hash.match(/(?:^#|&)token=([^&]+)/);
  if (m) {
    token = decodeURIComponent(m[1]);
    try { sessionStorage.setItem(KEY, token); } catch (e) { /* private mode */ }
    history.replaceState(null, '', location.pathname + location.search);
  } else {
    try { token = sessionStorage.getItem(KEY); } catch (e) { token = null; }
  }
  return token;
}

export class ApiError extends Error {
  constructor(msg, status) { super(msg); this.status = status; }
}

export async function api(path, { method = 'GET', body, as } = {}) {
  const headers = { 'X-Emcosim-Token': token || '' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let r;
  try {
    r = await fetch('/api/' + path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
  } catch (e) {
    throw new ApiError('无法连接到 emcosim 后台（程序是否已关闭？）', 0);
  }
  if (r.ok && as === 'buffer') return r.arrayBuffer();
  if (r.ok && as === 'text') return r.text();
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) throw new ApiError((data && data.error) || `HTTP ${r.status}`, r.status);
  return data;
}

export const get = (p) => api(p);
export const post = (p, body = {}) => api(p, { method: 'POST', body });
export const put = (p, body = {}) => api(p, { method: 'PUT', body });
export const del = (p) => api(p, { method: 'DELETE' });

const KINDS = ['job', 'job_progress', 'chat', 'project_changed', 'project_opened', 'scenarios_changed',
  'runs_changed', 'auth_changed', 'auth_log'];

export function connectEvents(onEvent, onState) {
  const es = new EventSource('/api/events?token=' + encodeURIComponent(token || ''));
  for (const k of KINDS) {
    es.addEventListener(k, (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch (err) { return; }
      onEvent(k, msg.data, msg);
    });
  }
  es.onopen = () => onState('open');
  es.onerror = () => onState(es.readyState === EventSource.CLOSED ? 'closed' : 'reconnecting');
  return es;
}

/** URL for <img> and links: those can't send the token header, so it goes in the query. */
export function authUrl(path) {
  return '/api/' + path + (path.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token || '');
}
