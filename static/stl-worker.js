// Fetches and parses STL parts off the page's main thread, so a 10+ MB part does not freeze the phone.
// Message in: {id, url}. Out: {id, type:"progress", loaded} while downloading, then {id, type:"done", pos, nor} or {id, type:"done", error}.
import { STLLoader } from './vendor/three-0.169.0/STLLoader.js';

const loader = new STLLoader();

async function fetchBytes(url, onBytes) {
  const r = await fetch(url, { credentials: 'same-origin' });
  if (!r.ok) throw new Error(`${url.split('/').pop()}: HTTP ${r.status}`);
  if (!r.body) { const b = await r.arrayBuffer(); onBytes(b.byteLength); return b; }
  const reader = r.body.getReader(), chunks = [];
  let n = 0, told = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value); n += value.length;
    if (n - told > 262144) { onBytes(n); told = n; }
  }
  onBytes(n);
  const out = new Uint8Array(n);
  let o = 0;
  for (const c of chunks) { out.set(c, o); o += c.length; }
  return out.buffer;
}

self.onmessage = async e => {
  const { id, url } = e.data;
  try {
    const buf = await fetchBytes(url, n => self.postMessage({ id, type: 'progress', loaded: n }));
    const g = loader.parse(buf);
    g.deleteAttribute('normal');  // some exporters write zero normals; work them out from the triangles
    g.computeVertexNormals();
    const pos = g.attributes.position.array, nor = g.attributes.normal.array;
    self.postMessage({ id, type: 'done', pos, nor }, [pos.buffer, nor.buffer]);
  } catch (err) {
    self.postMessage({ id, type: 'done', error: String((err && err.message) || err) });
  }
};
