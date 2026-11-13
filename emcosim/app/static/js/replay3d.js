// 3D replay of a stored run (scene.json / scene.bin / frames.bin), adapted from
// the standalone viewer. Renders on demand so an idle results screen costs nothing.
import * as THREE from 'three';
import { OrbitControls } from '../vendor/OrbitControls.js';
import { api } from './api.js';
import { cssVar } from './ui.js';

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

export class Replay3D {
  constructor(container) {
    this.container = container;
    this.frames = null;
    this.bodies = [];
    this.ok = false;
    try {
      this.renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: false });
    } catch (e) {
      this.error = '这台电脑的 WebGL 不可用，3D 回放无法显示（曲线和日志不受影响）。';
      return;
    }
    this.ok = true;
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    container.prepend(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(35, 1, 0.001, 50);
    this.camera.up.set(0, 0, 1);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.addEventListener('change', () => this.render());
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 1.6));
    const sun = new THREE.DirectionalLight(0xffffff, 1.6);
    sun.position.set(-1, -2, 3);
    this.scene.add(sun);
    this.applyTheme();
    this._ro = new ResizeObserver(() => this.resize());
    this._ro.observe(container);
  }

  applyTheme() {
    if (!this.ok) return;
    this.scene.background = new THREE.Color(cssVar('--view-bg') || '#e6eaef');
    this.render();
  }

  resize() {
    if (!this.ok) return;
    const r = this.container.getBoundingClientRect();
    if (!r.width || !r.height) return;
    this.renderer.setSize(r.width, r.height, false);
    this.camera.aspect = r.width / r.height;
    this.camera.updateProjectionMatrix();
    this.render();
  }

  render() {
    if (!this.ok || this._raf) return;
    this._raf = requestAnimationFrame(() => { this._raf = 0; this.renderer.render(this.scene, this.camera); });
  }

  clear() {
    for (const b of this.bodies) {
      this.scene.remove(b);
      b.traverse((o) => { if (o.isMesh) { o.material.dispose(); } });
    }
    for (const geo of this._geos || []) geo.dispose();
    this.bodies = [];
    this._geos = [];
    this.frames = null;
  }

  load(runId) { return this.loadFrom(`run/${runId}`); }

  /** base: an API path that serves scene.json, scene.bin and frames.bin (a run, or `model`). */
  async loadFrom(base) {
    if (!this.ok) throw new Error(this.error);
    const token = (this._token = {});
    const [desc, blob, fr] = await Promise.all([
      api(`${base}/scene.json`),
      api(`${base}/scene.bin`, { as: 'buffer' }),
      api(`${base}/frames.bin`, { as: 'buffer' }),
    ]);
    if (token !== this._token) return false;     // superseded by a newer load
    this.clear();
    this._desc = desc;
    this.bodies = desc.bodies.map((name) => { const o = new THREE.Group(); o.name = name; this.scene.add(o); return o; });
    const cache = new Map();
    for (const g of desc.geoms) {
      const key = g.type === 'mesh' ? `m${g.mesh.v_off}` : null;
      let geo = key && cache.get(key);
      if (!geo) { geo = geometryFor(g, blob); if (!geo) continue; this._geos.push(geo); if (key) cache.set(key, geo); }
      const mat = new THREE.MeshStandardMaterial({
        color: new THREE.Color(g.rgba[0], g.rgba[1], g.rgba[2]), transparent: g.rgba[3] < 1, opacity: g.rgba[3],
        roughness: 0.7, metalness: 0.05, side: THREE.DoubleSide,
      });
      const mesh = new THREE.Mesh(geo, mat);
      mesh.position.fromArray(g.pos);
      mesh.quaternion.copy(quat(g.quat));
      this.bodies[g.body].add(mesh);
    }
    const head = new Int32Array(fr, 0, 2);
    const n = head[0], nb = head[1];
    this.frames = { n, nb, t: new Float32Array(fr, 8, n), pose: new Float32Array(fr, 8 + 4 * n, n * nb * 7) };
    this.setTime(0);
    if (!this._framed) this.frame(desc);
    this.resize();
    return true;
  }

  frame(desc) {
    const box = new THREE.Box3();
    this.bodies.forEach((b, i) => { if (desc.bodies[i] !== 'world' && b.children.length) box.expandByObject(b); });
    if (box.isEmpty()) box.setFromObject(this.scene);
    const c = box.getCenter(new THREE.Vector3()), size = box.getSize(new THREE.Vector3()).length() || 0.1;
    this.controls.target.copy(c);
    this.camera.position.copy(c).add(new THREE.Vector3(size * 0.6, -size * 0.9, size * 0.5));
    this.controls.update();
    this._framed = true;
  }

  /** Close-up from the side, on the moving parts (press arm + rocker). */
  sideView(dir = [-1, 0, 0.05], zoom = 1.05) {
    const desc = this._desc;
    if (!desc) return;
    const box = new THREE.Box3();
    this.bodies.forEach((b, i) => { if (/press_arm|rocker/i.test(desc.bodies[i]) && b.children.length) box.expandByObject(b); });
    if (box.isEmpty()) this.bodies.forEach((b, i) => { if (desc.bodies[i] !== 'world' && b.children.length) box.expandByObject(b); });
    const c = box.getCenter(new THREE.Vector3()), size = box.getSize(new THREE.Vector3()).length() || 0.05;
    this.controls.target.copy(c);
    this.camera.position.copy(c).add(new THREE.Vector3(...dir).normalize().multiplyScalar(size * zoom));
    this.controls.update();
    this._framed = true;
    this.render();
  }

  resetView() { this._framed = false; if (this._desc) this.frame(this._desc); this.render(); }

  get duration() { const f = this.frames; return f ? f.t[f.n - 1] : 0; }

  setTime(t) {
    const f = this.frames;
    if (!f) return;
    let lo = 0, hi = f.n - 1;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (f.t[m] <= t) lo = m; else hi = m - 1; }
    for (let b = 0; b < f.nb; b++) {
      const o = (lo * f.nb + b) * 7, p = f.pose;
      this.bodies[b].position.set(p[o], p[o + 1], p[o + 2]);
      this.bodies[b].quaternion.set(p[o + 4], p[o + 5], p[o + 6], p[o + 3]);
    }
    this.render();
  }

  dispose() {
    if (!this.ok) return;
    this.clear();
    this._ro.disconnect();
    this.controls.dispose();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}
