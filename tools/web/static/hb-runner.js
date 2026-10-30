// The search's progress bar with a holomem's 3D chibi running along it.
// One random SD outfit per search, from the game's own models (tools/recon/proxy/chibi_build.py,
// GET /api/chibi/index, /chibi/models/<id>.glb) and its own run motion baked into the .glb.
// three.js loads only when a search starts; anything missing leaves a plain bar.
//
//   const r = runner();            // a detached element: r.el
//   slot.appendChild(r.el);        // moving it between re-renders keeps the WebGL context
//   r.set(0.42);                   // progress 0..1 (the chibi eases towards it), or
//   r.follow(p);                   // a pace(): the bar reads p.frac() every frame
//   r.stop();                      // frees the GPU side when the search ends
const THREE_URL = "three";        // optimizer.html's importmap: the same module the loader imports
const LOADER_URL = "https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/loaders/GLTFLoader.js";
const H = 150;                     // canvas height, px: the chibi stands ~100 px tall, plus
                                   // hats, ears and the run's bounce above the head

let index = null;                  // the /api/chibi/index answer (fetched once)
let lib = null;                    // {THREE, GLTFLoader}
const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

async function libs() {
  if (!lib) {
    const [THREE, { GLTFLoader }] = await Promise.all([import(THREE_URL), import(LOADER_URL)]);
    lib = { THREE, GLTFLoader };
  }
  return lib;
}
// ------------------------------------------------------------------ pace: ETA and bar estimate
// The searches report stages, not a total of work, so the bar and the ETA are ESTIMATES from
// how long each stage took in this viewer's last searches of the same kind (localStorage,
// a per-viewer convenience: without it every stage is assumed to take DEFAULT_S).
const PACE_KEY = "hb.pace", KEEP = 5, DEFAULT_S = 12;
function history() { try { return JSON.parse(localStorage.getItem(PACE_KEY) || "{}"); } catch (_) { return {}; } }
export function pace(kind, n) {
  const h = (history()[kind] || []);
  const med = i => { const v = h.map(r => r[i]).filter(x => x > 0).sort((a, b) => a - b); return v.length ? v[v.length >> 1] : DEFAULT_S; };
  const expect = Array.from({ length: n }, (_, i) => med(i));
  const t0 = performance.now(), took = [];
  let cur = 0, at = 0, top = 0;
  const now = () => (performance.now() - t0) / 1000;
  const p = {
    kind, expect,
    step(i) { const t = now(); while (cur < i && cur < n) { took[cur] = t - at; cur += 1; at = t; } },
    // seconds still to go: the current stage's expected rest (never under a fifth of it while
    // it overruns) plus every later stage's expected time
    eta() {
      const inStep = now() - at, e = expect[cur] || DEFAULT_S;
      let rest = Math.max(e - inStep, e * 0.2 * Math.exp(-(inStep - e) / e));
      for (let i = cur + 1; i < n; i += 1) rest += expect[i];
      return rest;
    },
    // never backwards, never 100 % before the answer
    frac() { const t = now(); top = Math.max(top, Math.min(0.97, t / (t + p.eta()))); return top; },
    elapsed: now,
    done() {
      p.step(n);
      try {
        const all = history();
        all[kind] = [...(all[kind] || []), took.slice(0, n)].slice(-KEEP);
        localStorage.setItem(PACE_KEY, JSON.stringify(all));
      } catch (_) { /* private mode: the defaults stay */ }
    },
  };
  return p;
}

export function runner() {
  const el = document.createElement("div");
  el.className = "hb-run";
  el.innerHTML = `<div class="hb-run-stage"></div><div class="pg-bar"><i style="width:0%"></i></div>`;
  const stage = el.firstElementChild, fill = el.querySelector(".pg-bar > i");
  let target = 0, shown = 0, alive = true, raf = 0, last = performance.now(), follow = null;
  let three = null;                // {renderer, scene, cam, actor, mixer, clock, span}
  const api = {
    el, who: null,
    set(frac) { target = Math.max(0, Math.min(1, +frac || 0)); if (reduced()) shown = target; paint(); },
    follow(p) { follow = p; },
    get alive() { return alive; },
    stop() { alive = false; cancelAnimationFrame(raf); if (three) { three.renderer.dispose(); three.renderer.forceContextLoss(); three = null; } },
  };
  function paint() { fill.style.width = (shown * 100).toFixed(1) + "%"; }
  let away = 0;                    // frames spent out of the page: the search's view is gone
  function frame() {
    if (!alive) return;
    raf = requestAnimationFrame(frame);
    away = el.isConnected ? 0 : away + 1;
    if (away > 90) { api.stop(); return; }
    // time-based easing: the same glide at any frame rate, no lurch when a stage ends
    const t = performance.now(), dt = Math.min(0.1, (t - last) / 1000);
    last = t;
    if (follow) target = Math.max(target, follow.frac());
    shown += (target - shown) * (1 - Math.exp(-dt / 0.45));
    if (Math.abs(target - shown) < 1e-4) shown = target;
    paint();
    if (!three || !el.isConnected) return;
    const w = stage.clientWidth;
    if (w && three.w !== w) { three.renderer.setSize(w, H); three.cam.left = 0; three.cam.right = w; three.cam.updateProjectionMatrix(); three.w = w; }
    three.actor.position.x = 30 + shown * Math.max(0, w - 60);
    if (three.mixer) three.mixer.update(three.clock.getDelta());
    three.renderer.render(three.scene, three.cam);
  }
  raf = requestAnimationFrame(frame);
  (async () => {
    try {
      if (!index) index = fetch("/api/chibi/index").then(r => r.json());
      const [{ THREE, GLTFLoader }, idx] = await Promise.all([libs(), index]);
      const ids = Object.keys(idx.outfits || {});
      if (!ids.length || !alive) return;
      const want = new URLSearchParams(location.search).get("chibi");      // ?chibi=<SdCostume id>: a fixed runner, for checking one
      const id = want && idx.outfits[want] ? want : ids[Math.floor(Math.random() * ids.length)], o = idx.outfits[id];
      api.who = { id, ...o };
      const gltf = await new GLTFLoader().loadAsync(o.model);
      if (!alive) return;
      const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
      renderer.outputColorSpace = THREE.SRGBColorSpace;
      renderer.setPixelRatio(Math.min(2, devicePixelRatio || 1));
      stage.appendChild(renderer.domElement);
      const scene = new THREE.Scene();
      // pixels as world units: x along the bar, y up; the chibi (~1.25 m) scaled to ~100 px
      const cam = new THREE.OrthographicCamera(0, 100, H, 0, -1000, 1000);
      cam.position.set(0, 0, 500);
      const actor = new THREE.Group();
      gltf.scene.traverse(m => {
        if (!m.isMesh) return;
        const src = m.material;
        m.material = new THREE.MeshBasicMaterial({ map: src.map, alphaTest: src.alphaTest || 0, side: src.side });
        m.frustumCulled = false;
      });
      gltf.scene.scale.setScalar(80);
      gltf.scene.rotation.y = Math.PI / 2 - 0.35;     // running to the right, turned a little to the viewer
      actor.add(gltf.scene);
      actor.position.y = 4;
      scene.add(actor);
      let mixer = null;
      if (gltf.animations && gltf.animations.length) {
        mixer = new THREE.AnimationMixer(gltf.scene);
        mixer.clipAction(gltf.animations[0]).play();
        if (reduced()) mixer.timeScale = 0;
      }
      three = { renderer, scene, cam, actor, mixer, clock: new THREE.Clock(), w: 0 };
      el.classList.add("live");
      el.dispatchEvent(new CustomEvent("runner", { detail: api.who, bubbles: true }));
    } catch (e) {
      console.warn("[runner] no chibi:", e);   // the plain bar stays
    }
  })();
  return api;
}

if (typeof window !== "undefined") window.HBRunner = { runner, pace };
