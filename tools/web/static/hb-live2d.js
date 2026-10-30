// The leader's Live2D figure, the way the game's unit screen ("Build a Live Lineup") shows it:
// the model of the leader's OUTFIT playing its character's IDLE_01 loop with the idle
// expression, blinking, physics on, no pointer tracking.
//
// Files: /api/live2d/index says which cards have a converted figure (live2d_build.py);
// /live2d/core.js is Live2D's Cubism Core (proprietary, kept out of git, served locally);
// the renderer is untitled-pixi-live2d-engine on pixi.js v8. The page maps "pixi.js" with an
// import map (the engine imports that bare name). ONE canvas and one WebGL context per page:
// mounting a figure elsewhere moves the canvas and frees the previous model.
//
// The slot is drawn first with the card's own art (the fallback): no WebGL, no core, no model
// for that outfit, or a failed load all leave the art in place, so the page always reads.

const ENGINE_URL = "https://cdn.jsdelivr.net/npm/untitled-pixi-live2d-engine@1.4.0/dist/cubism.es.js";

let indexP = null, runtimeP = null;
const S = { app: null, model: null, card: null, host: null, token: 0, observer: null, crop: 0.5 };

/** The index of converted figures (cached for the page's life). */
export function ready() {
  if (!indexP) indexP = fetch("/api/live2d/index").then(r => r.ok ? r.json() : null).catch(() => null);
  return indexP;
}
/** The figure entry for a card (null when none was converted or the runtime is absent). */
export async function modelFor(cardId) {
  const ix = await ready();
  if (!ix || !ix.available) return null;
  const asset = ix.by_card && ix.by_card[cardId];
  return asset && ix.models[asset] ? { asset, ...ix.models[asset] } : null;
}

function webglOK() {
  try { const c = document.createElement("canvas"); return !!(c.getContext("webgl2") || c.getContext("webgl")); }
  catch (_) { return false; }
}
function loadScript(src) {
  return new Promise((res, rej) => {
    const s = document.createElement("script");
    s.src = src; s.onload = res; s.onerror = () => rej(new Error("could not load " + src));
    document.head.append(s);
  });
}
function runtime() {
  if (!runtimeP) runtimeP = (async () => {
    // the engine throws on import when the core is missing: the core first
    if (!window.Live2DCubismCore) await loadScript("/live2d/core.js");
    const PIXI = await import("pixi.js");
    const L2D = await import(ENGINE_URL);
    PIXI.extensions.add(L2D.Live2DPlugin);
    return { PIXI, L2D };
  })();
  return runtimeP;
}

async function app() {
  if (S.app) return S.app;
  const { PIXI } = await runtime();
  const a = new PIXI.Application();
  await a.init({ backgroundAlpha: 0, antialias: true, preference: "webgl",
    resolution: Math.min(2, window.devicePixelRatio || 1), autoDensity: true, width: 10, height: 10 });
  a.canvas.className = "l2d-canvas";
  S.app = a;
  document.addEventListener("visibilitychange", () => { if (document.hidden) a.ticker.stop(); else if (S.model && !reduced()) a.ticker.start(); });
  return a;
}
const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

function dropModel() {
  if (!S.model) return;
  try { S.model.destroy({ children: true, texture: true, textureSource: true }); } catch (_) { /* already gone */ }
  S.model = null;
}

// Framing: head to hips, as the unit screen crops it. `crop` = the share of the model's canvas
// height the slot shows (the game's models share a 3000 x 4500 canvas with the head in its upper
// third); tuned by eye on Noel / Soleil Kiss against the game's screen.
function fit(model, host, crop) {
  const w = host.clientWidth, h = host.clientHeight;
  if (!w || !h || !S.app) return;
  // Resizing a WebGL canvas CLEARS it, and the browser can paint that empty canvas before the
  // ticker's next frame: the flash after a search, when the panel is redrawn several times
  // around the same figure. So resize only when the size really changed, and draw at once
  // (end of this function).
  const r = S.app.renderer;
  if (r.width !== w || r.height !== h) r.resize(w, h);
  const mw = model.internalModel.width, mh = model.internalModel.height;
  const s = Math.max(h / (mh * crop), (w * 0.9) / mw);
  model.scale.set(s);
  model.anchor.set(0.5, 0);
  model.position.set(w / 2, -h * 0.07);
  S.app.render();
}

/** Show `cardId`'s figure in `host` (an element already holding the fallback art).
 *  Resolves true when the figure is live, false when the fallback stays. */
export async function mount(host, cardId, { crop = 0.45 } = {}) {
  // the same figure, the panel redrawn around it: move the canvas, keep the model
  if (S.model && S.card === cardId && S.app) {
    attach(host, crop);
    host.classList.add("l2d-live");
    return true;
  }
  const token = ++S.token;
  const info = await modelFor(cardId);
  if (!info || !webglOK()) return false;
  try {
    const a = await app();
    const { L2D } = await runtime();
    if (token !== S.token) return false;
    const model = await L2D.Live2DModel.from(info.model, { ticker: a.ticker, autoFocus: false, autoHitTest: false,
      idleMotionGroup: "Idle" });
    if (token !== S.token) { model.destroy({ children: true }); return false; }
    dropModel();
    S.model = model;
    S.card = cardId;
    a.stage.addChild(model);
    attach(host, crop);
    if (info.expression) { try { model.expression("idle"); } catch (_) { /* the expression is optional */ } }
    fit(model, host, crop);
    if (reduced()) { a.render(); a.ticker.stop(); }
    else { a.ticker.start(); try { model.motion("Idle"); } catch (_) { /* no idle: breath + blink only */ } }
    host.classList.add("l2d-live");
    return true;
  } catch (e) {
    console.warn("[live2d] figure unavailable:", e && e.message ? e.message : e);
    return false;
  }
}

function attach(host, crop) {
  S.crop = crop;
  if (S.host !== host || S.app.canvas.parentNode !== host) {
    if (S.observer) S.observer.disconnect();
    host.append(S.app.canvas);
    S.host = host;
    S.observer = new ResizeObserver(() => { if (S.model) fit(S.model, host, S.crop); });
    S.observer.observe(host);
  }
  fit(S.model, host, crop);
}

/** Stop drawing (the panel went away); the canvas and context stay for the next mount. */
export function release() {
  S.token++;
  dropModel();
  S.card = null;
  if (S.app) S.app.ticker.stop();
  if (S.host) S.host.classList.remove("l2d-live");
}
