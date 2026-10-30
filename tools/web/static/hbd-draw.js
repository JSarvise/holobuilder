// Holomem Board page: one holomem's board drawn the way the game lays it out, in a view
// that zooms and pans.
//
// Geometry is the server's: each node's `pos` from /api/board-doc (board-export/v0,
// screen-like [x, y], Leader branch on top), links are the orthogonal 4-neighbour grid
// (as the game's SetAdjacentNode builds it; diagonals are not edges). Node colours are the
// game's branch gradients, glyphs are the game's own skill icons (/icon/), resolved by
// /api/board (frame + inner glyph for level-up nodes).
//
// Connect: a node inside a placed card's shape gets the game's connect marks (SkillTreeCell:
// a Blu2nd #5FDCF0 glow ring and the ConnectArrow badge); hovering a socket draws its whole
// shape and dims the rest. Values are server fields; the boost is hbd-util's `boosted`.
import { BRANCH, CONNECT_HUE, esc, connectView, boosted, fmtValue } from "/hbd-util.js";

const NS = "http://www.w3.org/2000/svg";
const CELL = 34, PAD = 26;
const R1 = 11.5, R2 = 13.5, RS = 15;
export const ZOOM_MIN = 0.6, ZOOM_MAX = 7;
const LABEL_ZOOM = 1.55;   // value labels appear from this zoom on

function el(tag, attrs, parent) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) n.setAttribute(k, v);
  if (parent) parent.append(n);
  return n;
}

/** The state of every node for a view.
 *  now:  final = owned now.
 *  plan: final = plan.optimised_nodes; buy = plan.to_buy; sell = plan.to_sell. */
export function viewSets(ch, mode) {
  const owned = new Set((ch.nodes || []).filter(n => n.state === "owned").map(n => n.id));
  for (const s of ch.sockets || []) if (s.socket === "S-001") owned.add("S-001");
  const plan = mode === "plan" && ch.plan ? ch.plan : null;
  return {
    owned,
    final: plan ? new Set(plan.optimised_nodes || []) : owned,
    buy: new Set(plan ? plan.to_buy || [] : []),
    sell: new Set(plan ? plan.to_sell || [] : []),
    plan,
  };
}

/** The card a socket holds in the view: the plan's recommendation (on an unlocked socket)
 *  with the plan shown, else the card placed now. */
export function socketCardFn(ch, mode) {
  const V = viewSets(ch, mode);
  return s => {
    if (!V.plan) return s.placed_card || null;
    const on = V.final.has(s.socket) || s.socket === "S-001";
    return on ? s.recommended_card || null : null;
  };
}

/* ------------------------------------------------------------------ pan + zoom */
// The view is the svg's viewBox, so the board stays vector-sharp at any zoom. The
// viewBox keeps the host's aspect ratio, so client px <-> board units is one factor.
// Kept across holomems (every board shares the same layout), refit on demand.
const PZ = { box: null, view: null, host: null, svg: null, onZoom: null };

function fitBox(host) {
  const b = PZ.box, r = host.getBoundingClientRect();
  const ar = (r.height || 1) / (r.width || 1);
  let w = b.w, h = b.w * ar;
  if (h < b.h) { h = b.h; w = b.h / ar; }
  return { x: b.x - (w - b.w) / 2, y: b.y - (h - b.h) / 2, w, h, fitW: w };
}
const zoomOf = () => (PZ.view ? PZ.view.fitW / PZ.view.w : 1);
function apply() {
  if (!PZ.svg || !PZ.view) return;
  const v = PZ.view;
  PZ.svg.setAttribute("viewBox", `${v.x} ${v.y} ${v.w} ${v.h}`);
  const z = zoomOf();
  PZ.svg.classList.toggle("labels", z >= LABEL_ZOOM);
  if (PZ.onZoom) PZ.onZoom(z);
}
function clampPan() {
  const v = PZ.view, b = PZ.box;
  // keep at least a quarter of the board inside the view
  const mx = v.w * 0.75, my = v.h * 0.75;
  v.x = Math.min(Math.max(v.x, b.x - mx), b.x + b.w - v.w + mx);
  v.y = Math.min(Math.max(v.y, b.y - my), b.y + b.h - v.h + my);
}
/** Zoom by `f` around the client point (cx, cy) — the cursor — or the centre. */
export function zoomBy(f, cx, cy) {
  if (!PZ.view) return;
  const v = PZ.view, r = PZ.host.getBoundingClientRect();
  const z = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, zoomOf() * f));
  const nf = (v.fitW / z) / v.w;   // new width / old width
  const px = cx == null ? 0.5 : (cx - r.left) / r.width, py = cy == null ? 0.5 : (cy - r.top) / r.height;
  const ax = v.x + px * v.w, ay = v.y + py * v.h;
  v.w *= nf; v.h *= nf;
  v.x = ax - px * v.w; v.y = ay - py * v.h;
  clampPan(); apply();
}
export function panBy(dxPx, dyPx) {
  if (!PZ.view) return;
  const r = PZ.host.getBoundingClientRect();
  PZ.view.x -= dxPx * PZ.view.w / r.width;
  PZ.view.y -= dyPx * PZ.view.h / r.height;
  clampPan(); apply();
}
export function fit() {
  if (!PZ.box || !PZ.host) return;
  PZ.view = fitBox(PZ.host);
  apply();
}
/** Centre the view on board point (x, y) without changing the zoom. */
function centreOn(x, y) {
  if (!PZ.view) return;
  PZ.view.x = x - PZ.view.w / 2; PZ.view.y = y - PZ.view.h / 2;
  clampPan(); apply();
}

/** Wire wheel / drag / pinch / double-click / keys on `host` once. `onZoom(z)` hears
 *  every zoom change (the page shows the percentage). */
export function initPanZoom(host, onZoom) {
  PZ.host = host; PZ.onZoom = onZoom;
  host.addEventListener("wheel", e => {
    e.preventDefault();
    // trackpad pinch arrives as ctrl+wheel with small deltas; a mouse notch is ~100
    const k = e.ctrlKey ? 0.01 : 0.0018;
    zoomBy(Math.exp(-e.deltaY * k), e.clientX, e.clientY);
  }, { passive: false });

  const pts = new Map();
  let drag = null, pinch = null;
  host.addEventListener("pointerdown", e => {
    if (e.button !== 0 && e.pointerType === "mouse") return;
    if (e.target.closest(".hbd-zoombar")) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pts.size === 1) drag = { x: e.clientX, y: e.clientY, moved: false, id: e.pointerId };
    if (pts.size === 2) {
      const [a, b] = [...pts.values()];
      pinch = { d: Math.hypot(a.x - b.x, a.y - b.y) };
      drag = null;
    }
  });
  host.addEventListener("pointermove", e => {
    if (!pts.has(e.pointerId)) return;
    const prev = pts.get(e.pointerId);
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pinch && pts.size === 2) {
      const [a, b] = [...pts.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      zoomBy(d / (pinch.d || d), (a.x + b.x) / 2, (a.y + b.y) / 2);
      panBy(e.clientX - prev.x, e.clientY - prev.y);
      pinch.d = d;
      return;
    }
    if (!drag) return;
    if (!drag.moved && Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 4) return;
    if (!drag.moved) { drag.moved = true; host.setPointerCapture(e.pointerId); host.classList.add("grabbing"); }
    panBy(e.clientX - prev.x, e.clientY - prev.y);
  });
  const end = e => {
    pts.delete(e.pointerId);
    if (pts.size < 2) pinch = null;
    if (drag && drag.id === e.pointerId) {
      if (drag.moved) {
        host.classList.remove("grabbing");
        // the click that ends a drag must not pick a node
        host.addEventListener("click", ev => { ev.stopPropagation(); ev.preventDefault(); }, { capture: true, once: true });
      }
      drag = null;
    }
  };
  host.addEventListener("pointerup", end);
  host.addEventListener("pointercancel", end);
  host.addEventListener("dblclick", e => { e.preventDefault(); fit(); });
  host.addEventListener("keydown", e => {
    const step = 60;
    const k = {
      "+": () => zoomBy(1.25), "=": () => zoomBy(1.25), "-": () => zoomBy(0.8), "_": () => zoomBy(0.8),
      "0": () => fit(), ArrowLeft: () => panBy(step, 0), ArrowRight: () => panBy(-step, 0),
      ArrowUp: () => panBy(0, step), ArrowDown: () => panBy(0, -step),
    }[e.key];
    if (k && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); k(); }
  });
  new ResizeObserver(() => {
    if (!PZ.view || !PZ.box) return;
    const r = host.getBoundingClientRect();
    if (!r.width || !r.height) return;
    // keep the zoom and the centre, take the new aspect
    const z = zoomOf(), cx = PZ.view.x + PZ.view.w / 2, cy = PZ.view.y + PZ.view.h / 2;
    const f = fitBox(host);
    PZ.view = { ...f, w: f.fitW / z, h: (f.fitW / z) * r.height / r.width };
    PZ.view.x = cx - PZ.view.w / 2; PZ.view.y = cy - PZ.view.h / 2;
    clampPan(); apply();
  }).observe(host);
}

/* ------------------------------------------------------------------ draw */
/** Draw `ch` into `host` (the svg goes in `host`, the pan/zoom surface is its parent).
 *  opts: {mode, glyphs (node id -> {icon, inner}), reqLevel (id -> level), playerLevel,
 *  values (node id -> {value, unit}), onPick(id, click)}. Returns the svg. */
export function drawBoard(host, ch, opts) {
  opts = opts || {};
  host.replaceChildren();
  const nodes = (ch.nodes || []).filter(n => n.branch !== "S");
  const sockets = ch.sockets || [];
  if (!nodes.length) {
    host.innerHTML = `<p class="hbd-empty">No board geometry for ${esc(ch.name || ch.chr)}.</p>`;
    PZ.svg = null;
    return null;
  }
  const V = viewSets(ch, opts.mode);
  const cardOf = socketCardFn(ch, opts.mode);
  const CV = connectView(ch, cardOf);
  const all = [...nodes, ...sockets.map(s => ({ ...s, id: s.socket, branch: "S" }))];
  const xs = all.map(n => n.pos[0]), ys = all.map(n => n.pos[1]);
  const minX = Math.min(...xs), minY = Math.min(...ys);
  const W = (Math.max(...xs) - minX) * CELL + PAD * 2;
  const H = (Math.max(...ys) - minY) * CELL + PAD * 2;
  const px = x => (x - minX) * CELL + PAD, py = y => (y - minY) * CELL + PAD;

  const svg = el("svg", { class: "hbd-svg", role: "img", "aria-label": `${ch.name}'s Holomem Board`,
    preserveAspectRatio: "xMidYMid meet" });
  const defs = el("defs", {}, svg);
  for (const [b, v] of Object.entries(BRANCH)) {
    const g = el("linearGradient", { id: `hbd-g-${b}`, x1: 0, y1: 0, x2: 1, y2: 1 }, defs);
    el("stop", { offset: "0", "stop-color": v.g1 }, g);
    el("stop", { offset: "1", "stop-color": v.g2 }, g);
  }
  const glow = el("filter", { id: "hbd-glow", x: "-50%", y: "-50%", width: "200%", height: "200%" }, defs);
  el("feGaussianBlur", { stdDeviation: "3.2", result: "b" }, glow);
  const fm = el("feMerge", {}, glow);
  el("feMergeNode", { in: "b" }, fm); el("feMergeNode", { in: "SourceGraphic" }, fm);
  // the game's ConnectArrow: an up chevron on a Blu2nd disc
  const arrow = el("symbol", { id: "hbd-arrow", viewBox: "0 0 12 12" }, defs);
  el("circle", { cx: 6, cy: 6, r: 5.6, fill: CONNECT_HUE, stroke: "#fff", "stroke-width": 0.9 }, arrow);
  el("path", { d: "M3.4 6.9L6 4.3l2.6 2.6M6 4.6v4", fill: "none", stroke: "#0C1A2A", "stroke-width": 1.5,
    "stroke-linecap": "round", "stroke-linejoin": "round" }, arrow);

  // connect ranges (the whole shape per socket), under everything; shown on socket focus
  const ranges = el("g", { class: "hbd-ranges" }, svg);
  for (const [sock, cells] of CV.cells) {
    const g = el("g", { class: "hbd-range", "data-range": sock }, ranges);
    for (const [x, y] of cells) el("rect", { x: px(x) - CELL / 2 + 2, y: py(y) - CELL / 2 + 2, width: CELL - 4, height: CELL - 4, rx: 9 }, g);
  }

  // links, under the nodes; lit when both ends are on the board shown
  const at = new Map(all.map(n => [`${n.pos[0]},${n.pos[1]}`, n]));
  const links = el("g", { class: "hbd-links" }, svg);
  for (const n of all) {
    for (const [dx, dy] of [[1, 0], [0, 1]]) {
      const m = at.get(`${n.pos[0] + dx},${n.pos[1] + dy}`);
      if (!m) continue;
      const on = V.final.has(n.id) && V.final.has(m.id);
      const br = n.branch === "S" ? m.branch : n.branch;
      el("line", { x1: px(n.pos[0]), y1: py(n.pos[1]), x2: px(m.pos[0]), y2: py(m.pos[1]),
        class: on ? "on" : "", stroke: on ? BRANCH[br].hue : null }, links);
    }
  }

  const layer = el("g", { class: "hbd-nodes" }, svg);
  const glyphs = opts.glyphs || {};
  const values = opts.values || {};
  for (const n of nodes) {
    const cx = px(n.pos[0]), cy = py(n.pos[1]);
    const r = n.star === 2 ? R2 : R1;
    const on = V.final.has(n.id), buy = V.buy.has(n.id), sell = V.sell.has(n.id);
    const lvl = (opts.reqLevel || {})[n.id];
    const gated = !on && opts.playerLevel != null && lvl != null && lvl > opts.playerLevel;
    const permil = CV.permil.get(n.id) || 0;
    const src = CV.src.get(n.id) || [];
    const g = el("g", { class: `hbd-n${on ? " on" : ""}${gated ? " gated" : ""}${sell ? " sell" : ""}${permil ? " boost" : ""}`,
      "data-node": n.id, "data-src": src.join(" ") || null, tabindex: "-1" }, layer);
    // two concentric rings, so both read at a glance (one ring would hide the other): Connect
    // hugs the disc, as the game draws it; unlock/reset sits outside
    // each ring is laid on a dark band so it reads on any branch colour (cyan on a blue disc did not)
    if (permil) { el("circle", { cx, cy, r: r + 2.4, class: "ring-gap" }, g); el("circle", { cx, cy, r: r + 2.4, class: "ring-connect" }, g); }
    if (on) el("circle", { cx, cy, r: r + 2.5, fill: BRANCH[n.branch].hue, opacity: 0.22, filter: "url(#hbd-glow)" }, g);
    el("circle", { cx, cy, r, class: "disc", fill: on ? `url(#hbd-g-${n.branch})` : "#171A26",
      stroke: on ? "rgb(255 255 255 / .55)" : BRANCH[n.branch].hue, "stroke-width": on ? 1.2 : 1.4,
      "stroke-opacity": on ? 1 : 0.55 }, g);
    const gl = glyphs[n.id] || (n.effect_icon ? { icon: n.effect_icon, inner: "" } : null);
    if (gl && gl.icon) {
      const s = r * 1.42;
      el("image", { href: `/icon/${gl.icon}.png`, x: cx - s / 2, y: cy - s / 2, width: s, height: s,
        class: "glyph" }, g);
      if (gl.inner) {
        const t = s * 0.62;
        el("image", { href: `/icon/${gl.inner}.png`, x: cx - t / 2, y: cy - t / 2, width: t, height: t, class: "glyph" }, g);
      }
    }
    if (buy || sell) el("circle", { cx, cy, r: r + 5.2, class: "ring-gap" }, g);
    if (buy) el("circle", { cx, cy, r: r + 5.2, class: "ring-buy" }, g);
    if (sell) el("circle", { cx, cy, r: r + 5.2, class: "ring-sell" }, g);
    if (permil) el("use", { href: "#hbd-arrow", x: cx + r * 0.42, y: cy + r * 0.42, width: 8.5, height: 8.5, class: "arrow" }, g);
    // the value, as the game prints it; boosted in the connect colour (shown when zoomed in)
    const v = values[n.id];
    if (v && v.value != null && v.unit) {
      const shown = fmtValue(boosted(v.value, permil), v.unit);
      el("text", { x: cx, y: cy + r + 7.2, class: `vl${permil ? " up" : ""}` }, g).textContent = shown;
    }
  }

  for (const s of sockets) {
    const cx = px(s.pos[0]), cy = py(s.pos[1]);
    const on = V.final.has(s.socket) || s.socket === "S-001";
    const card = cardOf(s);
    // what the plan does to the socket: the server's `change` (add / swap / remove) when it
    // compared the account's card with the plan's, else a card that differs from the placed one
    const change = V.plan ? (s.change || (card && card !== s.placed_card ? (s.placed_card ? "swap" : "add") : null)) : null;
    const g = el("g", { class: `hbd-s${on ? " on" : ""}${card ? " has" : ""}${change ? ` chg-${change}` : ""}`, "data-socket": s.socket, tabindex: "-1" }, layer);
    // a socket is a rounded square in the game (SkillTreeConnectCell)
    el("rect", { x: cx - RS, y: cy - RS, width: RS * 2, height: RS * 2, rx: 6, class: "frame" }, g);
    if (card) {
      const clip = `hbd-c-${s.socket}-${ch.chr}`;
      const cp = el("clipPath", { id: clip }, defs);
      el("rect", { x: cx - RS + 2.5, y: cy - RS + 2.5, width: (RS - 2.5) * 2, height: (RS - 2.5) * 2, rx: 4.5 }, cp);
      el("image", { href: `/cardart/thumb/${encodeURIComponent(card)}?w=96`, x: cx - RS + 2.5, y: cy - RS + 2.5,
        width: (RS - 2.5) * 2, height: (RS - 2.5) * 2, "clip-path": `url(#${clip})`, preserveAspectRatio: "xMidYMid slice" }, g);
      const c = CV.bySocket.get(s.socket);
      if (c) el("text", { x: cx, y: cy + RS + 8, class: "sl" }, g).textContent = `+${c.info.permil / 10}%`;
    } else {
      // an open socket is the game's own empty-socket sprite (ui_710_ico_socket); a locked one stays a dashed frame
      if (on) el("image", { href: "/ui/board/ui_710_ico_socket.png", x: cx - RS - 1, y: cy - RS - 1, width: RS * 2 + 2, height: RS * 2 + 2, class: "sockimg" }, g);
    }
    if (change) el("rect", { x: cx - RS - 4, y: cy - RS - 4, width: RS * 2 + 8, height: RS * 2 + 8, rx: 9,
      class: change === "remove" ? "ring-sell" : "ring-buy" }, g);
  }

  host.append(svg);
  // the view: same box as the last board keeps the zoom and position; a new box refits
  const box = { x: 0, y: 0, w: W, h: H };
  const same = PZ.box && PZ.box.w === W && PZ.box.h === H;
  PZ.box = box; PZ.svg = svg;
  if (!same || !PZ.view) PZ.view = fitBox(PZ.host || host);
  apply();

  if (opts.onPick) {
    svg.addEventListener("mouseover", e => {
      const t = e.target.closest("[data-node],[data-socket]");
      if (t) opts.onPick(t.dataset.node || t.dataset.socket, false);
    });
    svg.addEventListener("click", e => {
      const t = e.target.closest("[data-node],[data-socket]");
      if (t) opts.onPick(t.dataset.node || t.dataset.socket, true);
    });
  }
  svg.__board = { px, py, CV };
  return svg;
}

/** Mark one node as the selected one (a ring), clearing the last. */
export function markSelected(svg, id) {
  if (!svg) return;
  svg.querySelectorAll(".sel").forEach(n => n.classList.remove("sel"));
  const t = id && svg.querySelector(`[data-node="${id}"],[data-socket="${id}"]`);
  if (t) t.classList.add("sel");
}

/** Focus a Connect: show `socket`'s whole range and dim everything outside it; or, for a
 *  boosted node, light the socket(s) that boost it. `null` clears. */
export function focusConnect(svg, id) {
  if (!svg) return;
  svg.classList.remove("focus");
  svg.querySelectorAll(".lit").forEach(n => n.classList.remove("lit"));
  if (!id) return;
  const CV = svg.__board && svg.__board.CV;
  if (!CV) return;
  let sockets = [];
  if (id.startsWith("S-")) sockets = CV.bySocket.has(id) ? [id] : [];
  else sockets = CV.src.get(id) || [];
  if (!sockets.length) return;
  svg.classList.add("focus");
  for (const s of sockets) {
    const r = svg.querySelector(`[data-range="${s}"]`); if (r) r.classList.add("lit");
    const k = svg.querySelector(`[data-socket="${s}"]`); if (k) k.classList.add("lit");
    for (const n of CV.bySocket.get(s).covers) {
      const t = svg.querySelector(`[data-node="${n}"]`); if (t) t.classList.add("lit");
    }
  }
}

/** Bring a node or socket into view (centre it) when it is outside the visible box. */
export function reveal(svg, id) {
  if (!svg || !PZ.view || !svg.__board) return;
  const t = svg.querySelector(`[data-node="${id}"],[data-socket="${id}"]`);
  if (!t) return;
  const b = t.getBBox(), v = PZ.view;
  const x = b.x + b.width / 2, y = b.y + b.height / 2;
  if (x < v.x || x > v.x + v.w || y < v.y || y > v.y + v.h) centreOn(x, y);
}
export const currentZoom = () => zoomOf();
