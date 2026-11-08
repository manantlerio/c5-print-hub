// Spin viewer for a job: its STL parts, or for a sliced-only job the G-code toolpath, in the filament colours.
// Drag turns it, pinch or wheel zooms, it spins slowly and stops while touched. Loaded only when a preview is tapped.
import * as THREE from './vendor/three-0.169.0/three.module.min.js';
import { OrbitControls } from './vendor/three-0.169.0/OrbitControls.js';
import { STLLoader } from './vendor/three-0.169.0/STLLoader.js';

const $ = id => document.getElementById(id);
const mb = n => (n / 1048576).toFixed(n < 10485760 ? 1 : 0);
const ord = n => n + (n % 10 === 2 && n % 100 !== 12 ? 'nd' : n % 10 === 3 && n % 100 !== 13 ? 'rd' : 'th');
let V = null;  // the open view

function colour(hex) {
  const c = new THREE.Color(/^#[0-9a-f]{6}$/i.test(hex || '') ? hex.slice(0, 7) : '#c8c8c8');
  const hsl = {};
  c.getHSL(hsl);
  if (hsl.l < 0.3) c.setHSL(hsl.h, hsl.s, 0.3);  // black filament shows as charcoal, bright enough to read its shape
  return c;
}

function status(text, frac) {
  $('v3d-load').style.display = text ? '' : 'none';
  $('v3d-msg').innerHTML = text || '';
  $('v3d-bar').parentNode.style.display = frac == null ? 'none' : '';
  $('v3d-bar').style.width = Math.round(Math.max(0, Math.min(1, frac || 0)) * 100) + '%';
}

async function fetchBytes(url, onBytes, signal) {
  const r = await fetch(url, { signal, credentials: 'same-origin' });
  if (r.status === 202) return r;
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    throw new Error(j.error || `HTTP ${r.status}`);
  }
  if (!r.body) { const b = await r.arrayBuffer(); onBytes(b.byteLength); return b; }
  const reader = r.body.getReader(), chunks = [];
  let n = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value); n += value.length; onBytes(n);
  }
  const out = new Uint8Array(n);
  let o = 0;
  for (const c of chunks) { out.set(c, o); o += c.length; }
  return out.buffer;
}

function frame(v) {
  v.group.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(v.group);
  if (box.isEmpty()) return;
  const s = box.getBoundingSphere(new THREE.Sphere()), r = Math.max(s.radius, 1);
  const vfov = THREE.MathUtils.degToRad(v.camera.fov);
  const hfov = 2 * Math.atan(Math.tan(vfov / 2) * v.camera.aspect);
  const dist = r / Math.sin(Math.min(vfov, hfov) / 2) * 1.05;
  v.camera.position.copy(s.center).add(new THREE.Vector3(0.45, 0.5, 1).normalize().multiplyScalar(dist));
  v.camera.near = r / 50; v.camera.far = dist + r * 20;
  v.camera.updateProjectionMatrix();
  v.controls.target.copy(s.center);
  v.controls.minDistance = r * 0.3; v.controls.maxDistance = r * 10;
  v.controls.update();
  v.controls.saveState();
}

function resize(v) {
  const w = v.host.clientWidth, h = v.host.clientHeight;
  v.renderer.setSize(w, h);
  v.camera.aspect = w / Math.max(1, h);
  v.camera.updateProjectionMatrix();
}

// One worker parses every part; if module workers are not supported the page parses them itself.
function parseInWorker(v, url, onBytes) {
  return new Promise((res, rej) => {
    if (v.worker === undefined) {
      try {
        v.worker = new Worker('/static/stl-worker.js', { type: 'module' });
        v.worker.onmessage = e => {
          const m = e.data, p = v.pending.get(m.id);
          if (!p) return;
          if (m.type === 'progress') return p.onBytes(m.loaded);
          v.pending.delete(m.id);
          m.error ? p.rej(new Error(m.error)) : p.res(m);
        };
        v.worker.onerror = e => {
          if (e.preventDefault) e.preventDefault();
          v.worker.terminate(); v.worker = null;
          for (const [id, p] of v.pending) { v.pending.delete(id); parseHere(v, p.url, p.onBytes).then(p.res, p.rej); }
        };
      } catch (e) { v.worker = null; }
    }
    if (!v.worker) return parseHere(v, url, onBytes).then(res, rej);
    const id = ++v.seq;
    v.pending.set(id, { res, rej, onBytes, url });
    v.worker.postMessage({ id, url });
  });
}

async function parseHere(v, url, onBytes) {
  const g = new STLLoader().parse(await fetchBytes(url, onBytes, v.abort.signal));
  g.deleteAttribute('normal');
  g.computeVertexNormals();
  return { pos: g.attributes.position.array, nor: g.attributes.normal.array };
}

async function loadParts(v, job, info, cols) {
  const parts = info.parts, total = parts.reduce((s, p) => s + p.size, 0), got = parts.map(() => 0);
  let done = 0;
  const show = () => status(`Loading ${parts.length > 1 ? `part ${Math.min(done + 1, parts.length)} of ${parts.length}` : 'model'}`
    + ` - ${mb(got.reduce((a, b) => a + b, 0))} of ${mb(total)} MB`, got.reduce((a, b) => a + b, 0) / Math.max(1, total));
  show();
  await Promise.all(parts.map((p, i) =>
    parseInWorker(v, `/api/jobs/${job.id}/stl/${encodeURIComponent(p.file)}`, n => { got[i] = n; show(); }).then(m => {
      if (V !== v) return;
      const g = new THREE.BufferGeometry();
      g.setAttribute('position', new THREE.BufferAttribute(m.pos, 3));
      g.setAttribute('normal', new THREE.BufferAttribute(m.nor, 3));
      v.group.add(new THREE.Mesh(g, new THREE.MeshPhongMaterial({ color: colour(cols[p.tool - 1]), shininess: 40, specular: 0x262626 })));
      done++; got[i] = p.size; show();
      if (!v.touched) frame(v);
    })));
  if (V !== v) return;
  status('');
  $('v3d-note').textContent = `${parts.length} part${parts.length > 1 ? 's' : ''} as uploaded - drag to turn, pinch to zoom`;
}

// Lines lit like a solid from their vertex normals: a key light and a fill fixed to the camera, both faces alike.
function litLines(c) {
  return new THREE.ShaderMaterial({
    uniforms: { col: { value: c } },
    vertexShader: `varying vec3 vN;
      void main() { vN = normalMatrix * normal; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
    fragmentShader: `uniform vec3 col; varying vec3 vN;
      void main() {
        vec3 n = normalize(vN);
        float key = abs(dot(n, normalize(vec3(0.35, 0.55, 0.75)))), fill = abs(dot(n, normalize(vec3(-0.6, -0.25, 0.45))));
        gl_FragColor = vec4(col * (0.28 + 0.95 * key * key + 0.2 * fill), 1.0);
        #include <colorspace_fragment>
      }`,
  });
}

async function loadToolpath(v, job, cols) {
  let buf;
  for (;;) {
    const r = await fetchBytes(`/api/jobs/${job.id}/toolpath`, n => status(`Loading the toolpath - ${mb(n)} MB`, null), v.abort.signal);
    if (V !== v) return;
    if (!(r instanceof Response)) { buf = r; break; }
    const j = await r.json();
    status(`Building the 3D toolpath from the G-code (first time only) - ${Math.round((j.progress || 0) * 100)}%`, j.progress || 0);
    await new Promise(ok => setTimeout(ok, 1500));
    if (V !== v) return;
  }
  const dv = new DataView(buf);
  const magic = dv.getUint32(0, true), lit = magic === 0x00325054;  // TP2 has vertex normals, TP1 is flat
  if (!lit && magic !== 0x00315054) throw new Error('not a toolpath file');
  const n = dv.getUint32(4, true), head = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 8, n)));
  let off = 8 + n;
  if (!head.segments) return status('No 3D model for this job: the G-code has no extrusion moves to show.', null);
  for (const [tool, count] of head.tools) {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(new Uint16Array(buf, off, count * 6), 3));
    off += count * 12;
    if (lit) {
      g.setAttribute('normal', new THREE.BufferAttribute(new Int8Array(buf, off, count * 6), 3, true));
      off += count * 6;
    }
    const line = new THREE.LineSegments(g, lit ? litLines(colour(cols[tool - 1]))
      : new THREE.LineBasicMaterial({ color: colour(cols[tool - 1]) }));
    line.scale.setScalar(head.scale);
    line.position.set(head.origin[0], head.origin[1], head.origin[2]);
    v.group.add(line);
  }
  frame(v);
  status('');
  $('v3d-note').textContent = `Toolpath from the G-code, ${head.hidden} hidden` +
    (head.stride > 1 ? `, every ${ord(head.stride)} of ${head.layers} layers` : '') + ' - drag to turn, pinch to zoom';
}

export async function open(job, cols) {
  close();
  const host = $('v3d-canvas');
  $('v3d-title').textContent = job.name;
  $('v3d-note').textContent = '';
  $('v3d').classList.add('open');
  status('Loading', null);
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x4a505a);
  host.appendChild(renderer.domElement);
  const scene = new THREE.Scene(), camera = new THREE.PerspectiveCamera(40, 1, 0.1, 5000);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x3a3e45, 1.7));
  const sun = new THREE.DirectionalLight(0xffffff, 1.8);
  sun.position.set(0.6, 1, 0.8);
  camera.add(sun); scene.add(camera);
  const group = new THREE.Group();
  group.rotation.x = -Math.PI / 2;  // printer Z up
  scene.add(group);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true; controls.dampingFactor = 0.12;
  controls.autoRotate = true; controls.autoRotateSpeed = 1.5;
  const v = V = { host, renderer, scene, camera, controls, group, touched: false, abort: new AbortController(),
                  worker: undefined, pending: new Map(), seq: 0 };
  // A touch stops the spin; it picks up again 3.5 s after the last finger lifts.
  controls.addEventListener('start', () => { v.touched = true; controls.autoRotate = false; clearTimeout(v.spin); });
  controls.addEventListener('end', () => { clearTimeout(v.spin); v.spin = setTimeout(() => { controls.autoRotate = true; }, 3500); });
  v.onResize = () => resize(v);
  window.addEventListener('resize', v.onResize);
  resize(v);
  const tick = () => { if (V !== v) return; v.raf = requestAnimationFrame(tick); controls.update(); renderer.render(scene, camera); };
  tick();
  try {
    const r = await fetch(`/api/jobs/${job.id}/model`, { signal: v.abort.signal, credentials: 'same-origin' });
    const info = await r.json();
    if (!r.ok) throw new Error(info.error || `HTTP ${r.status}`);
    if (V !== v) return;
    if (info.kind === 'stl') await loadParts(v, job, info, cols);
    else if (info.kind === 'toolpath') await loadToolpath(v, job, cols);
    else status(`No 3D model for this job.<br><img class="preview" style="margin-top:12px" src="/api/jobs/${job.id}/preview.png" alt="">`, null);
  } catch (e) {
    if (V === v && e.name !== 'AbortError') status(`Could not show this job in 3D: ${String(e.message || e).replace(/[<&]/g, '')}`, null);
  }
}

export function reset() {
  if (V) V.controls.reset();
}

export function close() {
  if (!V) return;
  const v = V;
  V = null;
  cancelAnimationFrame(v.raf);
  clearTimeout(v.spin);
  v.abort.abort();
  if (v.worker) v.worker.terminate();
  window.removeEventListener('resize', v.onResize);
  v.controls.dispose();
  v.group.traverse(o => { if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose(); });
  v.renderer.dispose();
  v.renderer.forceContextLoss();
  v.renderer.domElement.remove();
  $('v3d').classList.remove('open');
}
