import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';

const $ = (id) => document.getElementById(id);
const DEFAULT_SIGNALS = [/^node\..*\.v$/, /act\..*\.i_motor$/, /joint\..*\.deg$/, /probe\..*force_n$/, /^mcu\.powered$/, /pulse_us$/];
const state = { run: null, frames: null, t: 0, playing: false, signals: {}, chosen: new Set(), charts: [] };

// ---------------------------------------------------------------- 3D view --
const view = $('view');
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(window.devicePixelRatio);
view.appendChild(renderer.domElement);
const scene = new THREE.Scene();
const dark = matchMedia('(prefers-color-scheme: dark)').matches;
scene.background = new THREE.Color(dark ? 0x1b1f24 : 0xe9edf1);
const camera = new THREE.PerspectiveCamera(35, 1, 0.001, 50);
camera.up.set(0, 0, 1);
const controls = new OrbitControls(camera, renderer.domElement);
scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 1.6));
const sun = new THREE.DirectionalLight(0xffffff, 1.6);
sun.position.set(-1, -2, 3);
scene.add(sun);
let bodies = [];

function resize() {
  const r = view.getBoundingClientRect();
  renderer.setSize(r.width, r.height, false);
  camera.aspect = r.width / Math.max(r.height, 1);
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(view);

function quat(q) { return new THREE.Quaternion(q[1], q[2], q[3], q[0]); }

function geometryFor(g, blob) {
  const s = g.size;
  switch (g.type) {
    case 'box': return new THREE.BoxGeometry(2 * s[0], 2 * s[1], 2 * s[2]);
    case 'sphere': return new THREE.SphereGeometry(s[0], 24, 16);
    case 'ellipsoid': { const geo = new THREE.SphereGeometry(1, 24, 16); geo.scale(s[0], s[1], s[2]); return geo; }
    case 'cylinder': { const geo = new THREE.CylinderGeometry(s[0], s[0], 2 * s[1], 24); geo.rotateX(Math.PI / 2); return geo; }
    case 'capsule': { const geo = new THREE.CapsuleGeometry(s[0], 2 * s[1], 8, 16); geo.rotateX(Math.PI / 2); return geo; }
    case 'plane': return new THREE.PlaneGeometry(2 * (s[0] || 1), 2 * (s[1] || 1));
    case 'mesh': {
      const m = g.mesh;
      const geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(blob, m.v_off, m.v_n * 3), 3));
      geo.setIndex(new THREE.BufferAttribute(new Uint32Array(blob, m.f_off, m.f_n * 3), 1));
      geo.computeVertexNormals();
      return geo;
    }
  }
  return null;
}

async function loadScene(runId) {
  for (const b of bodies) scene.remove(b);
  bodies = [];
  const [desc, blob] = await Promise.all([
    fetch(`/api/run/${runId}/scene.json`).then((r) => r.json()),
    fetch(`/api/run/${runId}/scene.bin`).then((r) => r.arrayBuffer()),
  ]);
  bodies = desc.bodies.map((name) => { const o = new THREE.Group(); o.name = name; scene.add(o); return o; });
  const cache = new Map();
  for (const g of desc.geoms) {
    const key = g.type === 'mesh' ? `m${g.mesh.v_off}` : null;
    let geo = key && cache.get(key);
    if (!geo) { geo = geometryFor(g, blob); if (key) cache.set(key, geo); }
    if (!geo) continue;
    const mat = new THREE.MeshStandardMaterial({ color: new THREE.Color(g.rgba[0], g.rgba[1], g.rgba[2]),
      transparent: g.rgba[3] < 1, opacity: g.rgba[3], roughness: 0.7, metalness: 0.05, side: THREE.DoubleSide });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.position.fromArray(g.pos);
    mesh.quaternion.copy(quat(g.quat));
    bodies[g.body].add(mesh);
  }
  const fr = await fetch(`/api/run/${runId}/frames.bin`).then((r) => r.arrayBuffer());
  const head = new Int32Array(fr, 0, 2);
  const n = head[0], nb = head[1];
  state.frames = { n, nb, t: new Float32Array(fr, 8, n), pose: new Float32Array(fr, 8 + 4 * n, n * nb * 7) };
  applyFrame(0);
  // frame the moving/mounted parts, not the world geometry (walls, floors)
  const box = new THREE.Box3();
  bodies.forEach((b, i) => { if (desc.bodies[i] !== 'world' && b.children.length) box.expandByObject(b); });
  if (box.isEmpty()) box.setFromObject(scene);
  const c = box.getCenter(new THREE.Vector3()), size = box.getSize(new THREE.Vector3()).length();
  controls.target.copy(c);
  camera.position.copy(c).add(new THREE.Vector3(size * 0.6, -size * 0.9, size * 0.5));
  controls.update();
}

function applyFrame(t) {
  const f = state.frames;
  if (!f) return;
  let i = 0;
  while (i < f.n - 1 && f.t[i + 1] <= t) i++;
  for (let b = 0; b < f.nb; b++) {
    const o = (i * f.nb + b) * 7, p = f.pose;
    bodies[b].position.set(p[o], p[o + 1], p[o + 2]);
    bodies[b].quaternion.set(p[o + 4], p[o + 5], p[o + 6], p[o + 3]);
  }
}

// ---------------------------------------------------------------- charts ---
function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function drawChart(c) {
  const cv = c.canvas, dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  cv.width = w * dpr; cv.height = h * dpr;
  const g = cv.getContext('2d');
  g.scale(dpr, dpr);
  const t = state.signals.t, y = state.signals[c.name];
  if (!t || !y) return;
  let lo = Infinity, hi = -Infinity;
  for (const v of y) { if (v < lo) lo = v; if (v > hi) hi = v; }
  if (hi - lo < 1e-9) { hi += 0.5; lo -= 0.5; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const t0 = t[0], t1 = t[t.length - 1];
  const X = (v) => 34 + (v - t0) / (t1 - t0) * (w - 40), Y = (v) => 4 + (hi - v) / (hi - lo) * (h - 18);
  g.strokeStyle = css('--line'); g.lineWidth = 1;
  g.beginPath(); g.moveTo(34, 4); g.lineTo(34, h - 14); g.lineTo(w - 6, h - 14); g.stroke();
  g.fillStyle = css('--muted'); g.font = '10px system-ui';
  g.fillText(hi.toPrecision(3), 0, 12); g.fillText(lo.toPrecision(3), 0, h - 16);
  g.fillText(`${t0.toFixed(1)}s`, 34, h - 3); g.fillText(`${t1.toFixed(1)}s`, w - 34, h - 3);
  g.strokeStyle = css('--accent'); g.lineWidth = 1.4; g.beginPath();
  for (let i = 0; i < t.length; i++) { const px = X(t[i]), py = Y(y[i]); i ? g.lineTo(px, py) : g.moveTo(px, py); }
  g.stroke();
  const cx = X(state.t);
  g.strokeStyle = css('--bad'); g.lineWidth = 1; g.beginPath(); g.moveTo(cx, 4); g.lineTo(cx, h - 14); g.stroke();
  let i = 0; while (i < t.length - 1 && t[i + 1] <= state.t) i++;
  c.value.textContent = y[i].toPrecision(4);
}

function rebuildCharts() {
  const box = $('charts');
  box.innerHTML = '';
  state.charts = [];
  for (const name of state.chosen) {
    const el = document.createElement('div');
    el.className = 'chart';
    el.innerHTML = `<header><span>${name}</span><span></span></header><canvas></canvas>`;
    box.appendChild(el);
    state.charts.push({ name, canvas: el.querySelector('canvas'), value: el.querySelector('header span:last-child') });
  }
  if (!state.chosen.size) box.innerHTML = '<div class="empty">在左侧勾选信号</div>';
  state.charts.forEach(drawChart);
}

async function loadSignals(runId, names) {
  const want = names.filter((n) => !(n in state.signals));
  if (want.length) {
    const d = await fetch(`/api/run/${runId}/signals?names=${encodeURIComponent(want.join(','))}`).then((r) => r.json());
    Object.assign(state.signals, d);
  }
}

// ---------------------------------------------------------------- page -----
function fmt(v) { return typeof v === 'number' ? (Number.isInteger(v) ? v : v.toPrecision(4)) : JSON.stringify(v); }

async function openRun(runId) {
  state.signals = {};
  const meta = await fetch(`/api/run/${runId}`).then((r) => r.json());
  state.run = meta;
  $('desc').textContent = (meta.scenario.description || '') + (Object.keys(meta.overrides || {}).length ? `  覆盖: ${JSON.stringify(meta.overrides)}` : '');
  $('checks').innerHTML = (meta.checks || []).map((c) =>
    `<div class="check"><b class="${c.pass ? 'ok' : 'bad'}">${c.pass ? '✓' : '✗'}</b><span>${c.check} ${c.op} ${fmt(c.target)} → <b>${fmt(c.value)}</b>${c.note ? `<br><span style="color:var(--muted)">${c.note}</span>` : ''}</span></div>`).join('') || '<span style="color:var(--muted)">无断言</span>';
  const m = meta.metrics || {};
  $('metrics').innerHTML = Object.entries(m).filter(([k, v]) => typeof v === 'number').map(([k, v]) => `<span title="${k}">${k}</span><span>${fmt(v)}</span>`).join('');
  $('uart').textContent = await fetch(`/api/run/${runId}/uart.log`).then((r) => r.text());
  state.chosen = new Set(meta.signals.filter((s) => DEFAULT_SIGNALS.some((re) => re.test(s))).slice(0, 10));
  $('pick').innerHTML = meta.signals.map((s) => `<label><input type="checkbox" value="${s}" ${state.chosen.has(s) ? 'checked' : ''}> ${s}</label>`).join('');
  $('pick').querySelectorAll('input').forEach((cb) => cb.addEventListener('change', async () => {
    cb.checked ? state.chosen.add(cb.value) : state.chosen.delete(cb.value);
    await loadSignals(runId, [...state.chosen]);
    rebuildCharts();
  }));
  await Promise.all([loadScene(runId), loadSignals(runId, ['t', ...state.chosen].filter((x) => x !== 't'))]);
  if (!state.signals.t) Object.assign(state.signals, await fetch(`/api/run/${runId}/signals?names=mcu.powered`).then((r) => r.json()));
  setTime(0);
  rebuildCharts();
}

function duration() { const f = state.frames; return f ? f.t[f.n - 1] : 0; }

function setTime(t) {
  state.t = Math.max(0, Math.min(t, duration()));
  $('time').textContent = `${state.t.toFixed(3)} s`;
  $('slider').value = duration() ? Math.round(state.t / duration() * 1000) : 0;
  applyFrame(state.t);
  state.charts.forEach(drawChart);
}

$('slider').addEventListener('input', (e) => setTime(e.target.value / 1000 * duration()));
$('play').addEventListener('click', () => { state.playing = !state.playing; $('play').textContent = state.playing ? '暂停' : '播放'; if (state.t >= duration()) setTime(0); });
$('runs').addEventListener('change', (e) => openRun(e.target.value));

let last = performance.now();
function loop(now) {
  const dt = (now - last) / 1000; last = now;
  if (state.playing) {
    setTime(state.t + dt * parseFloat($('speed').value));
    if (state.t >= duration()) { state.playing = false; $('play').textContent = '播放'; }
  }
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(loop);
}
requestAnimationFrame(loop);

const runs = await fetch('/api/runs').then((r) => r.json());
$('runs').innerHTML = runs.map((r) => `<option value="${r.run_id}">${r.passed === true ? '✓' : r.passed === false ? '✗' : '·'} ${r.run_id}</option>`).join('');
const pre = new URLSearchParams(location.search).get('run');
if (runs.length) openRun(pre || runs[0].run_id);
else $('charts').innerHTML = '<div class="empty">还没有运行结果：先执行 emcosim run &lt;scenario&gt;</div>';
