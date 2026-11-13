// The 建模 page's 3D view: the model at rest, split into parts the user can pick, pull apart
// (exploded view), look at one at a time, and drag around. Moving parts here is only a view:
// nothing is written back to the model.
import * as THREE from 'three';
import { Replay3D } from './replay3d.js';

const HIGHLIGHT = new THREE.Color(0x2f7bf6);

/** 'arm_visual' -> 'arm', 'servo_lead_0_3' -> 'servo_lead', 'frame_visual_2' -> 'frame'. */
export function geomStem(name) {
  return String(name || '').replace(/_visual(_\d+)*$/, '').replace(/(_\d+)+$/, '') || 'part';
}

/** labels: { prefix: {label, print} }; the longest prefix that matches the stem wins. */
function lookup(labels, stem) {
  if (labels[stem]) return labels[stem];
  let best = null;
  for (const k of Object.keys(labels)) if (stem.startsWith(k + '_') && (!best || k.length > best.length)) best = k;
  return best ? labels[best] : null;
}

export class ModelView extends Replay3D {
  constructor(container, { onSelect } = {}) {
    super(container);
    this.parts = [];
    this.selected = null;
    this.isolated = null;
    this.explodeF = 0;
    this.onSelect = onSelect || (() => {});
    if (!this.ok) return;
    this._ray = new THREE.Raycaster();
    this._ndc = new THREE.Vector2();
    // capture on the container so a press on a part never reaches OrbitControls on the canvas
    this._down = (e) => this._pointerDown(e);
    this._move = (e) => this._pointerMove(e);
    this._up = (e) => this._pointerUp(e);
    this._hover = (e) => this._hoverMove(e);
    container.addEventListener('pointerdown', this._down, true);
    this.renderer.domElement.addEventListener('pointermove', this._hover);
  }

  async loadFrom(base, labels = {}) {
    const keep = this.parts.map((p) => [p.key, p.drag.clone()]);
    const ok = await super.loadFrom(base);
    if (!ok) return ok;
    this._index(labels);
    for (const [k, d] of keep) { const p = this.part(k); if (p) p.drag.copy(d); }
    if (this.selected && !this.part(this.selected)) this.selected = null;
    if (this.isolated && !this.part(this.isolated)) this.isolated = null;
    this._apply();
    this._paint();
    return ok;
  }

  clear() { super.clear(); this.parts = []; }

  /** Forget layout and selection (another project was opened). */
  forget() { this.selected = null; this.isolated = null; this.explodeF = 0; this.clear(); this._framed = false; }

  part(key) { return this.parts.find((p) => p.key === key) || null; }

  _index(labels) {
    const byKey = new Map();
    this.scene.updateMatrixWorld(true);
    this.bodies.forEach((body) => {
      for (const mesh of body.children) {
        if (!mesh.isMesh) continue;
        const stem = geomStem(mesh.userData.geom);
        const info = lookup(labels, stem);
        const label = info?.label || stem;
        let p = byKey.get(label);
        if (!p) {
          p = { key: label, label, print: info?.print || null, stems: new Set(), meshes: [], bodies: new Set(),
            fixed: true, color: null, drag: new THREE.Vector3(), explode: new THREE.Vector3(), home: new THREE.Vector3() };
          byKey.set(label, p);
        }
        p.stems.add(stem);
        p.meshes.push(mesh);
        p.bodies.add(body.name);
        if (body.name !== 'world') p.fixed = false;
        if (!p.color || mesh.material.opacity > 0.5) p.color = '#' + mesh.material.color.getHexString();
        mesh.userData.home = mesh.position.clone();
        mesh.userData.part = p;
      }
    });
    this.parts = [...byKey.values()];
    // explode directions: from the centre of the moving assembly out through each part's centre
    const all = new THREE.Box3();
    for (const p of this.parts) {
      const b = new THREE.Box3();
      for (const m of p.meshes) b.expandByObject(m);
      p.box = b;
      b.getCenter(p.home);
      if (!p.fixed) all.union(b);
    }
    if (all.isEmpty()) for (const p of this.parts) all.union(p.box);
    const c = all.getCenter(new THREE.Vector3());
    const r = all.getSize(new THREE.Vector3()).length() / 2 || 0.05;
    this._radius = r;
    // Parts close together (one sub-assembly) are spread among themselves: sideways away from
    // their local centre, and stacked along the sub-assembly's thinnest direction, so a part
    // nested in another comes out in front of it. Sub-assemblies also move apart a little.
    const moving = this.parts.filter((p) => !p.fixed);
    const D = 2 * r;
    for (const p of moving) {
      const near = moving.filter((q) => q.home.distanceTo(p.home) < 0.2 * D);
      const L = new THREE.Vector3();
      const nb = new THREE.Box3();
      for (const q of near) { L.add(q.home); nb.union(q.box); }
      L.divideScalar(near.length);
      const ext = nb.getSize(new THREE.Vector3());
      const axis = new THREE.Vector3(...[0, 1, 2].map((k) => (k === [ext.x, ext.y, ext.z].indexOf(Math.min(ext.x, ext.y, ext.z)) ? 1 : 0)));
      const order = [...near].sort((x, y) => x.home.dot(axis) - y.home.dot(axis) || x.key.localeCompare(y.key));
      const k = order.indexOf(p) - (order.length - 1) / 2;
      const local = p.home.clone().sub(L);
      const lateral = local.clone().sub(axis.clone().multiplyScalar(local.dot(axis)));
      p.explode.copy(p.home).sub(c).multiplyScalar(0.5)
        .add(lateral.multiplyScalar(2.0))
        .add(axis.multiplyScalar(k * nb.getSize(new THREE.Vector3()).length() * 0.3));
    }
  }

  /** f: 0 = assembled, 1 = fully pulled apart. */
  explode(f) { this.explodeF = Math.max(0, f); this._apply(); }

  _apply() {
    const q = new THREE.Quaternion();
    for (const p of this.parts) {
      const w = p.explode.clone().multiplyScalar(this.explodeF).add(p.drag);
      for (const m of p.meshes) {
        m.parent.getWorldQuaternion(q);
        m.position.copy(m.userData.home).add(w.clone().applyQuaternion(q.invert()));
        // pulled apart, the fixed scenery (a wall) would hide the parts pushed behind it
        m.visible = this.isolated ? this.isolated === p.key : !(p.fixed && this.explodeF > 0.02);
      }
    }
    this.render();
  }

  _paint() {
    for (const p of this.parts) {
      const on = p.key === this.selected;
      for (const m of p.meshes) { m.material.emissive.copy(on ? HIGHLIGHT : new THREE.Color(0)); m.material.emissiveIntensity = on ? 0.55 : 0; }
    }
    this.render();
  }

  select(key, { silent } = {}) {
    this.selected = key && this.part(key) ? key : null;
    this._paint();
    if (!silent) this.onSelect(this.selected);
  }

  /** Show one part alone (null shows everything again) and frame it. */
  isolate(key) {
    this.isolated = key && this.part(key) ? key : null;
    this._apply();
    if (this.isolated) { this.select(this.isolated); this.focus(this.isolated); }
    else this.fitAll();
  }

  focus(key) {
    const p = this.part(key);
    if (!p) return;
    const b = new THREE.Box3();
    for (const m of p.meshes) b.expandByObject(m);
    const c = b.getCenter(new THREE.Vector3()), size = b.getSize(new THREE.Vector3()).length() || 0.02;
    const dir = this.camera.position.clone().sub(this.controls.target).normalize();
    this.controls.target.copy(c);
    this.camera.position.copy(c).add(dir.multiplyScalar(size * 1.9));
    this.controls.update();
    this.render();
  }

  /** Frame whatever is visible (the moving assembly when everything is shown). */
  fitAll() {
    const box = new THREE.Box3();
    for (const p of this.parts) if (!p.fixed && (!this.isolated || this.isolated === p.key)) for (const m of p.meshes) box.expandByObject(m);
    if (box.isEmpty()) return this.resetView();
    const c = box.getCenter(new THREE.Vector3()), size = box.getSize(new THREE.Vector3()).length() || 0.1;
    const dir = this.camera.position.clone().sub(this.controls.target).normalize();
    const half = THREE.MathUtils.degToRad(this.camera.fov / 2);
    const fit = Math.min(half, Math.atan(Math.tan(half) * this.camera.aspect));
    this.controls.target.copy(c);
    this.camera.position.copy(c).add(dir.multiplyScalar((size / 2) / Math.sin(fit) * 1.05));
    this.controls.update();
    this.render();
  }

  /** Put every part back where the model has it. */
  resetLayout() {
    for (const p of this.parts) p.drag.set(0, 0, 0);
    this.explodeF = 0;
    this.isolated = null;
    this._apply();
  }

  get moved() { return this.explodeF > 0 || this.parts.some((p) => p.drag.lengthSq() > 0); }

  _pick(e) {
    const r = this.renderer.domElement.getBoundingClientRect();
    this._ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    this._ray.setFromCamera(this._ndc, this.camera);
    const meshes = [];
    for (const p of this.parts) for (const m of p.meshes) if (m.visible) meshes.push(m);
    // the wall and other fixed scenery only count when nothing else is under the pointer
    const hits = this._ray.intersectObjects(meshes, false);
    return hits.find((h) => !h.object.userData.part.fixed) || hits[0] || null;
  }

  _pointerDown(e) {
    if (e.button !== 0 || e.target !== this.renderer.domElement) return;
    const hit = this._pick(e);
    this._click = { x: e.clientX, y: e.clientY, hit };
    window.addEventListener('pointerup', this._up);
    if (!hit || hit.object.userData.part.fixed) return;       // empty space or scenery: let the camera orbit
    e.stopPropagation();
    e.preventDefault();
    const p = hit.object.userData.part;
    const n = this.camera.getWorldDirection(new THREE.Vector3());
    this._drag = { part: p, plane: new THREE.Plane().setFromNormalAndCoplanarPoint(n, hit.point), start: hit.point.clone(), from: p.drag.clone() };
    if (this.selected !== p.key) this.select(p.key);
    this.renderer.domElement.style.cursor = 'grabbing';
    window.addEventListener('pointermove', this._move);
  }

  _pointerMove(e) {
    const d = this._drag;
    if (!d) return;
    const r = this.renderer.domElement.getBoundingClientRect();
    this._ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    this._ray.setFromCamera(this._ndc, this.camera);
    const pt = this._ray.ray.intersectPlane(d.plane, new THREE.Vector3());
    if (!pt) return;
    d.part.drag.copy(d.from).add(pt.sub(d.start));
    d.moved = true;
    this._apply();
  }

  _pointerUp(e) {
    window.removeEventListener('pointermove', this._move);
    window.removeEventListener('pointerup', this._up);
    const c = this._click, d = this._drag;
    this._drag = null;
    this._click = null;
    this.renderer.domElement.style.cursor = '';
    if (d?.moved) { this.onSelect(this.selected, { dragged: true }); return; }
    if (!c || Math.hypot(e.clientX - c.x, e.clientY - c.y) > 4) return;   // that was an orbit
    const p = c.hit?.object.userData.part;
    if (p && !d) this.select(this.selected === p.key ? null : p.key);       // click on scenery toggles it
    else if (!p) this.select(null);
  }

  _hoverMove(e) {
    if (this._drag || e.buttons || this._hoverRaf) return;
    this._hoverRaf = requestAnimationFrame(() => {
      this._hoverRaf = 0;
      const hit = this._pick(e);
      this.renderer.domElement.style.cursor = hit && !hit.object.userData.part.fixed ? 'grab' : '';
      this.renderer.domElement.title = hit ? hit.object.userData.part.label : '';
    });
  }

  dispose() {
    if (!this.ok) return;
    this.container.removeEventListener('pointerdown', this._down, true);
    this.renderer.domElement.removeEventListener('pointermove', this._hover);
    super.dispose();
  }
}
