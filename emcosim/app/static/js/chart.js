// Canvas charts: time series (with playback cursor, hover readout, click-to-seek)
// and a small x/y chart for sweep results.
import { cssVar, fmtNum } from './ui.js';

export function niceTicks(lo, hi, count = 5) {
  if (!(hi > lo)) return [lo];
  const span = hi - lo;
  const step0 = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const f = step0 / mag;
  const step = (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-6; v += step) out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
  return out;
}

export function tickLabel(v) {
  const a = Math.abs(v);
  if (a === 0) return '0';
  if (a >= 1e4 || a < 1e-3) return v.toExponential(1).replace('.0e', 'e');
  return String(parseFloat(v.toPrecision(4)));
}

function lastIndexLE(t, x) {
  let lo = 0, hi = t.length - 1;
  if (!t.length || x < t[0]) return 0;
  while (lo < hi) { const m = (lo + hi + 1) >> 1; if (t[m] <= x) lo = m; else hi = m - 1; }
  return lo;
}

const FONT = '10px "Cascadia Mono", Consolas, ui-monospace, monospace';

class BaseChart {
  constructor(canvas) {
    this.cv = canvas;
    this.hover = null;
    this._raf = 0;
    this._ro = new ResizeObserver(() => this.draw());
    this._ro.observe(canvas);
    canvas.addEventListener('mousemove', (e) => { const r = canvas.getBoundingClientRect(); this.hover = { x: e.clientX - r.left, y: e.clientY - r.top }; this.draw(); });
    canvas.addEventListener('mouseleave', () => { this.hover = null; this.draw(); });
  }
  draw() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => { this._raf = 0; this.paint(); });
  }
  drawNow() { if (this._raf) { cancelAnimationFrame(this._raf); this._raf = 0; } this.paint(); }
  ctx() {
    const cv = this.cv, dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return null;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    const g = cv.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    return { g, w, h };
  }
  dispose() { this._ro.disconnect(); }
}

export class TimeChart extends BaseChart {
  /** opts: onSeek(t), compact, xRange [t0,t1], unit */
  constructor(canvas, opts = {}) {
    super(canvas);
    this.opts = opts;
    this.series = [];
    this.cursor = null;
    let dragging = false;
    const seek = (e) => {
      if (!this.opts.onSeek || !this._map) return;
      const r = canvas.getBoundingClientRect();
      this.opts.onSeek(this._map.inv(e.clientX - r.left));
    };
    canvas.addEventListener('mousedown', (e) => { if (e.button === 0) { dragging = true; seek(e); } });
    window.addEventListener('mouseup', () => { dragging = false; });
    canvas.addEventListener('mousemove', (e) => { if (dragging) seek(e); });
  }
  setSeries(series) { this.series = series; this.draw(); }
  setCursor(t) { this.cursor = t; this.draw(); }
  valueAt(i, t) {
    const s = this.series[i];
    if (!s || !s.t?.length) return null;
    return s.y[lastIndexLE(s.t, t)];
  }
  paint() {
    const c = this.ctx();
    if (!c) return;
    const { g, w, h } = c;
    const compact = this.opts.compact;
    const L = compact ? 38 : 46, R = 8, T = 6, B = compact ? 14 : 16;
    let t0 = Infinity, t1 = -Infinity, lo = Infinity, hi = -Infinity;
    for (const s of this.series) {
      const n = s.t?.length || 0;
      if (!n) continue;
      t0 = Math.min(t0, s.t[0]); t1 = Math.max(t1, s.t[n - 1]);
      for (let i = 0; i < n; i++) { const v = s.y[i]; if (v < lo) lo = v; if (v > hi) hi = v; }
    }
    if (this.opts.xRange) [t0, t1] = this.opts.xRange;
    const gridC = cssVar('--chart-grid'), axisC = cssVar('--chart-axis'), textC = cssVar('--text-3'), ink = cssVar('--text');
    g.font = FONT;
    if (!isFinite(lo)) {
      g.fillStyle = textC; g.fillText('无数据', L, T + 14);
      this._map = null;
      return;
    }
    if (!(t1 > t0)) t1 = t0 + 1;
    if (hi - lo < 1e-9) { const d = Math.max(Math.abs(hi) * 0.05, 0.5); hi += d; lo -= d; }
    const pad = (hi - lo) * 0.06; lo -= pad; hi += pad;
    const pw = w - L - R, ph = h - T - B;
    const X = (v) => L + (v - t0) / (t1 - t0) * pw;
    const Y = (v) => T + (hi - v) / (hi - lo) * ph;
    this._map = { inv: (px) => Math.max(t0, Math.min(t1, t0 + (px - L) / pw * (t1 - t0))) };
    // grid + y labels
    g.lineWidth = 1; g.textBaseline = 'middle'; g.textAlign = 'right';
    for (const v of niceTicks(lo, hi, compact ? 3 : 4)) {
      const y = Math.round(Y(v)) + 0.5;
      g.strokeStyle = gridC; g.beginPath(); g.moveTo(L, y); g.lineTo(w - R, y); g.stroke();
      g.fillStyle = textC; g.fillText(tickLabel(v), L - 5, y);
    }
    // x labels
    g.textAlign = 'center'; g.textBaseline = 'alphabetic';
    for (const v of niceTicks(t0, t1, Math.max(2, Math.floor(pw / 70)))) {
      const lab = tickLabel(v) + 's', tw = g.measureText(lab).width;
      const x = Math.min(Math.max(X(v), L + tw / 2), w - tw / 2 - 1);
      g.fillStyle = textC; g.fillText(lab, x, h - 3);
    }
    g.strokeStyle = axisC; g.beginPath(); g.moveTo(L + 0.5, T); g.lineTo(L + 0.5, T + ph); g.lineTo(w - R, T + ph + 0.5); g.stroke();
    // series
    g.save();
    g.beginPath(); g.rect(L, T - 1, pw, ph + 2); g.clip();
    g.lineJoin = 'round';
    for (const s of this.series) {
      const n = s.t?.length || 0;
      if (!n) continue;
      g.strokeStyle = s.color; g.lineWidth = s.width || 1.5;
      g.setLineDash(s.dash || []);
      g.beginPath();
      for (let i = 0; i < n; i++) { const x = X(s.t[i]), y = Y(s.y[i]); i ? g.lineTo(x, y) : g.moveTo(x, y); }
      g.stroke();
    }
    g.setLineDash([]);
    g.restore();
    // playback cursor
    if (this.cursor !== null && this.cursor !== undefined && this.cursor >= t0 && this.cursor <= t1) {
      const x = Math.round(X(this.cursor)) + 0.5;
      g.strokeStyle = cssVar('--accent'); g.lineWidth = 1;
      g.beginPath(); g.moveTo(x, T); g.lineTo(x, T + ph); g.stroke();
    }
    // hover readout
    const hv = this.hover;
    if (hv && hv.x >= L && hv.x <= w - R && !compact) {
      const t = this._map.inv(hv.x);
      const x = Math.round(X(t)) + 0.5;
      g.strokeStyle = textC; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(x, T); g.lineTo(x, T + ph); g.stroke(); g.setLineDash([]);
      const rows = [`t = ${t.toFixed(3)} s`];
      const cols = [null];
      this.series.forEach((s, i) => {
        if (!s.t?.length) return;
        const v = s.y[lastIndexLE(s.t, t)];
        g.fillStyle = s.color; g.strokeStyle = cssVar('--panel'); g.lineWidth = 2;
        g.beginPath(); g.arc(x, Y(v), 4, 0, Math.PI * 2); g.fill(); g.stroke();
        rows.push(`${s.label ? s.label + '  ' : ''}${fmtNum(v)}${this.opts.unit ? ' ' + this.opts.unit : ''}`);
        cols.push(s.color);
      });
      g.font = FONT;
      const tw = Math.max(...rows.map((r) => g.measureText(r).width)) + 22;
      const th = rows.length * 14 + 8;
      let bx = x + 10; if (bx + tw > w - 2) bx = x - 10 - tw;
      const by = T + 2;
      g.fillStyle = cssVar('--panel'); g.strokeStyle = cssVar('--line-strong'); g.lineWidth = 1;
      g.beginPath(); g.roundRect ? g.roundRect(bx, by, tw, th, 4) : g.rect(bx, by, tw, th); g.fill(); g.stroke();
      g.textAlign = 'left'; g.textBaseline = 'middle';
      rows.forEach((r, i) => {
        const yy = by + 11 + i * 14;
        if (cols[i]) { g.fillStyle = cols[i]; g.fillRect(bx + 7, yy - 1.5, 8, 3); }
        g.fillStyle = i ? ink : textC; g.fillText(r, bx + (cols[i] ? 19 : 7), yy);
      });
    }
  }
}

export class XYChart extends BaseChart {
  /** series: [{label,color,points:[{x,y,row}]}]; categories: [labels] when x is categorical */
  constructor(canvas, opts = {}) {
    super(canvas);
    this.opts = opts;
    this.series = [];
    this.categories = null;
    canvas.addEventListener('click', () => { const p = this._near; if (p && this.opts.onPick) this.opts.onPick(p.row); });
  }
  set(series, { categories = null, xLabel = '', yLabel = '' } = {}) {
    Object.assign(this, { series, categories, xLabel, yLabel });
    this.draw();
  }
  paint() {
    const c = this.ctx();
    if (!c) return;
    const { g, w, h } = c;
    const L = 56, R = 14, T = 12, B = 34;
    const textC = cssVar('--text-3'), ink = cssVar('--text'), gridC = cssVar('--chart-grid'), axisC = cssVar('--chart-axis');
    g.font = FONT;
    const pts = this.series.flatMap((s) => s.points.filter((p) => typeof p.y === 'number' && isFinite(p.y)));
    if (!pts.length) { g.fillStyle = textC; g.fillText('等待数据…', L, T + 20); this._near = null; return; }
    let x0 = Math.min(...pts.map((p) => p.x)), x1 = Math.max(...pts.map((p) => p.x));
    let lo = Math.min(...pts.map((p) => p.y)), hi = Math.max(...pts.map((p) => p.y));
    if (this.categories) { x0 = -0.5; x1 = this.categories.length - 0.5; }
    if (!(x1 > x0)) { x0 -= 1; x1 += 1; }
    if (hi - lo < 1e-12) { const d = Math.max(Math.abs(hi) * 0.1, 0.5); hi += d; lo -= d; }
    const pad = (hi - lo) * 0.1; lo -= pad; hi += pad;
    if (!this.categories) { const px = (x1 - x0) * 0.05; x0 -= px; x1 += px; }
    const pw = w - L - R, ph = h - T - B;
    const X = (v) => L + (v - x0) / (x1 - x0) * pw, Y = (v) => T + (hi - v) / (hi - lo) * ph;
    g.lineWidth = 1; g.textAlign = 'right'; g.textBaseline = 'middle';
    for (const v of niceTicks(lo, hi, 5)) {
      const y = Math.round(Y(v)) + 0.5;
      g.strokeStyle = gridC; g.beginPath(); g.moveTo(L, y); g.lineTo(w - R, y); g.stroke();
      g.fillStyle = textC; g.fillText(tickLabel(v), L - 6, y);
    }
    g.textAlign = 'center'; g.textBaseline = 'alphabetic';
    const xt = this.categories ? this.categories.map((_, i) => i) : niceTicks(x0, x1, Math.max(2, Math.floor(pw / 80)));
    for (const v of xt) g.fillText(this.categories ? String(this.categories[v]) : tickLabel(v), X(v), T + ph + 15);
    g.fillStyle = textC;
    g.fillText(this.xLabel || '', L + pw / 2, h - 3);
    g.strokeStyle = axisC; g.beginPath(); g.moveTo(L + 0.5, T); g.lineTo(L + 0.5, T + ph); g.lineTo(w - R, T + ph + 0.5); g.stroke();
    const surface = cssVar('--panel');
    let near = null, nd = 18 * 18;
    for (const s of this.series) {
      const ps = s.points.filter((p) => typeof p.y === 'number' && isFinite(p.y)).sort((a, b) => a.x - b.x);
      g.strokeStyle = s.color; g.lineWidth = 2; g.beginPath();
      ps.forEach((p, i) => (i ? g.lineTo(X(p.x), Y(p.y)) : g.moveTo(X(p.x), Y(p.y))));
      g.stroke();
      for (const p of ps) {
        const px = X(p.x), py = Y(p.y);
        g.fillStyle = p.failed ? cssVar('--bad') : s.color; g.strokeStyle = surface; g.lineWidth = 2;
        g.beginPath(); g.arc(px, py, 4.5, 0, Math.PI * 2); g.fill(); g.stroke();
        if (this.hover) {
          const d = (this.hover.x - px) ** 2 + (this.hover.y - py) ** 2;
          if (d < nd) { nd = d; near = { ...p, px, py, s }; }
        }
      }
    }
    this._near = near;
    this.cv.style.cursor = near ? 'pointer' : 'default';
    if (near) {
      g.strokeStyle = ink; g.lineWidth = 1.5; g.beginPath(); g.arc(near.px, near.py, 7, 0, Math.PI * 2); g.stroke();
      const lines = [near.label || '', `${this.yLabel}: ${fmtNum(near.y)}`, near.failed ? '未通过 · 点击打开' : '点击打开这次运行'].filter(Boolean);
      const tw = Math.max(...lines.map((l) => g.measureText(l).width)) + 14, th = lines.length * 14 + 8;
      let bx = near.px + 10, by = near.py - th - 6;
      if (bx + tw > w) bx = near.px - tw - 10;
      if (by < 0) by = near.py + 10;
      g.fillStyle = surface; g.strokeStyle = cssVar('--line-strong');
      g.beginPath(); g.roundRect ? g.roundRect(bx, by, tw, th, 4) : g.rect(bx, by, tw, th); g.fill(); g.stroke();
      g.textAlign = 'left'; g.textBaseline = 'middle';
      lines.forEach((l, i) => { g.fillStyle = i === 1 ? ink : textC; g.fillText(l, bx + 7, by + 11 + i * 14); });
    }
  }
}
