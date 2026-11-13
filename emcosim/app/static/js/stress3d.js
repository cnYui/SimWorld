// Stress-coloured 3D view of a printed part after a strength check (checks/<part>/stress.bin):
// header [n_verts, n_tris] uint32, vertices float32 (m), triangles uint32, von Mises float32 (MPa).
import * as THREE from 'three';
import { OrbitControls } from '../vendor/OrbitControls.js';
import { api } from './api.js';
import { cssVar } from './ui.js';

// blue -> cyan -> green -> yellow -> red
const STOPS = [[0, [0.17, 0.33, 0.85]], [0.25, [0.1, 0.7, 0.85]], [0.5, [0.2, 0.75, 0.3]], [0.75, [0.95, 0.8, 0.15]], [1, [0.85, 0.15, 0.12]]];
export function stressColor(x) {
  x = Math.max(0, Math.min(1, x));
  for (let i = 1; i < STOPS.length; i++) {
    const [b, cb] = STOPS[i];
    if (x <= b) {
      const [a, ca] = STOPS[i - 1], f = (x - a) / (b - a);
      return ca.map((c, k) => c + (cb[k] - c) * f);
    }
  }
  return STOPS[STOPS.length - 1][1];
}
export const stressGradient = `linear-gradient(90deg, ${STOPS.map(([x, c]) => `rgb(${c.map((v) => Math.round(v * 255)).join(',')}) ${x * 100}%`).join(', ')})`;

export class StressView {
  constructor(container) {
    this.container = container;
    this.ok = false;
    try { this.renderer = new THREE.WebGLRenderer({ antialias: true }); }
    catch (e) { this.error = '这台电脑的 WebGL 不可用，3D 应力图无法显示。'; return; }
    this.ok = true;
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    container.prepend(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(35, 1, 0.0005, 10);
    this.camera.up.set(0, 0, 1);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.addEventListener('change', () => this.render());
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 1.5));
    const sun = new THREE.DirectionalLight(0xffffff, 1.2);
    sun.position.set(-1, -2, 3);
    this.scene.add(sun);
    this.scene.background = new THREE.Color(cssVar('--view-bg') || '#e6eaef');
    this._ro = new ResizeObserver(() => this.resize());
    this._ro.observe(container);
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
    if (this.mesh) { this.scene.remove(this.mesh); this.mesh.geometry.dispose(); this.mesh.material.dispose(); this.mesh = null; }
    this.render();
  }

  /** scaleMax: the stress shown as full red (MPa); defaults to the part's peak. Returns {max}. */
  async load(part, scaleMax) {
    if (!this.ok) throw new Error(this.error);
    const buf = await api(`checks/${encodeURIComponent(part)}/stress.bin`, { as: 'buffer' });
    const [nv, nt] = new Uint32Array(buf, 0, 2);
    const pos = new Float32Array(buf, 8, nv * 3);
    const idx = new Uint32Array(buf, 8 + nv * 12, nt * 3);
    const val = new Float32Array(buf, 8 + nv * 12 + nt * 12, nv);
    let max = 0;
    for (const v of val) if (v > max) max = v;
    const top = scaleMax || max || 1;
    const col = new Float32Array(nv * 3);
    for (let i = 0; i < nv; i++) col.set(stressColor(val[i] / top), i * 3);
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    geo.setAttribute('color', new THREE.BufferAttribute(col, 3));
    geo.setIndex(new THREE.BufferAttribute(idx, 1));
    geo.computeVertexNormals();
    this.clear();
    this.mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.6, metalness: 0, side: THREE.DoubleSide }));
    this.scene.add(this.mesh);
    geo.computeBoundingSphere();
    const { center, radius } = geo.boundingSphere;
    this.controls.target.copy(center);
    this.camera.position.copy(center).add(new THREE.Vector3(radius * 2.2, -radius * 3.0, radius * 1.9));
    this.controls.update();
    this.resize();
    return { max };
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
