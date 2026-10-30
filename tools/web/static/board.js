// board.js — the Holomem Board editor.
//
// WHY THIS EXISTS, given the board is captured. The current state is
// CAPTURED, not typed (8,208 booleans), and nothing HAS to be typed — but that
// records what IS. This edits what COULD BE, which no capture can supply:
//
//   * PLANNING. Earned points can sit unspent; "where do they go" is a
//     question about a board that does not exist yet.
//   * COVERAGE. Characters the account has never developed appear in no
//     capture and have no board at all.
//
// So the source of every board is shown and never blurred: `captured` came
// from the game, `edited` was typed here, `empty` is a character with neither.
// A UI that rendered those identically would let an edit pass as an
// observation.
//
// THE RULE THAT DRIVES THE INTERACTION. Adjacency is the orthogonal
// 4-neighbour grid (the 152 slots form one component with degree histogram
// {1:31, 2:74, 3:25, 4:22}, and diagonals are NOT edges: under 8-neighbour the
// outer sockets stop being cut vertices, contradicting every observed board). A node cannot be bought without its
// path, which is worth 28 %: the 82 stat nodes cost 228 points in isolation
// and 293 connected. So clicking a node buys its whole path, and the cost the
// panel shows is the connected cost — the one the game will actually charge.

const ROOT = "S-001";
// The glyphs are 128x128 white-on-transparent PNGs at 43 % coverage, so they
// only read once a node is big enough to carry one. At CELL 26 the glyph is
// ~11 px and unreadable; 34 gives ~15 px inside a 10 px-radius node, and the
// widest model (x −10…10, y −14…9) still fits in ~680x780 inside a panel that
// scrolls.
const CELL = 34;
const PAD = 26;
const NODE_R = 10;
const SOCKET_R = 13;

import { el, svgEl } from "/dom.js";

const state = {
  data: null, // the /api/board payload
  chr: null, // selected character id
  selection: new Map(), // chr -> Set of node ids
  slots: new Map(), // chr -> { socket: card id }
  openSocket: null, // which socket's picker is showing
  filter: "", // card picker's text filter
  dirty: false,
};

// --- Connect sockets -------------------------------------------------------

/** `{ card id -> "chr/socket" }` over EVERY character, not just the open one.
 *
 * A card may occupy only one socket account-wide, so the picker cannot be
 * built from the character on screen — the card you want may be sitting on a
 * board you are not looking at, and the only honest thing to do is say where.
 * Mirrors `domain/skilltree.check_placements`, which is what a save is
 * validated against. */
function cardSeats() {
  const seats = new Map();
  for (const [chrId, slots] of state.slots) {
    for (const [socket, card] of Object.entries(slots)) {
      if (card) seats.set(card, `${chrId}/${socket}`);
    }
  }
  return seats;
}

/** Node id -> connect permil, for the character on screen.
 *
 * A node covered by two sockets takes the BEST of them: the shapes cannot
 * overlap between two sockets on the same board (shapes reach at most 3
 * cells and sockets sit 7 apart), so this max is a
 * guard against a future data change rather than a live case. */
function connectCover(chrId) {
  const cover = new Map();
  const cat = state.data.sockets;
  if (!cat) return cover;
  const model = state.data.members.find((m) => m.chr === chrId)?.model;
  const positions = state.data.models[model] || {};
  const byId = new Map(cat.cards.map((c) => [c.id, c]));
  const at = new Map();
  for (const [node, p] of Object.entries(positions)) at.set(`${p[0]},${p[1]}`, node);

  for (const [socket, cardId] of Object.entries(state.slots.get(chrId) || {})) {
    const card = byId.get(cardId);
    const origin = positions[socket];
    if (!card || !origin) continue;
    for (const [dx, dy] of cat.extents[card.area] || []) {
      const node = at.get(`${origin[0] + dx},${origin[1] + dy}`);
      if (!node) continue;
      cover.set(node, Math.max(cover.get(node) || 0, card.permil || 0));
    }
  }
  return cover;
}

/** The holomem behind a `chr-000NN`. Users do not know the ids. */
function holomemName(chrId) {
  return state.data.members.find((m) => m.chr === chrId)?.name || chrId;
}

/** The board geometry a socket picker needs, computed ONCE per open panel
 *  rather than per candidate row: the socket's own cell, and a cell -> node
 *  index. Same construction `connectCover` uses, so the picker and the drawn
 *  board can never disagree about what a shape reaches. */
function socketGeometry(chrId, socket) {
  const model = state.data.members.find((m) => m.chr === chrId)?.model;
  const positions = state.data.models[model] || {};
  const origin = positions[socket];
  if (!origin) return null;
  const at = new Map();
  for (const [node, p] of Object.entries(positions)) at.set(`${p[0]},${p[1]}`, node);
  return { origin, at, bought: state.selection.get(chrId) || new Set() };
}

/** What a candidate card would ACTUALLY cover here: how many nodes its shape
 *  reaches, and how many of those you have bought.
 *
 *  This is the number the choice turns on. A
 *  x3.00 over three nodes you do not own is worth nothing, and a x2.10 over
 *  eight you do own is worth a great deal -- ranking by the multiplier alone
 *  actively misleads. `_connect_multipliers` in `build_board.py` does the same
 *  join server-side; this is the same geometry, shown before you commit. */
function coverageAt(geom, card) {
  if (!geom) return null;
  const extents = state.data.sockets?.extents?.[card.area] || [];
  let total = 0;
  let yours = 0;
  for (const [dx, dy] of extents) {
    const node = geom.at.get(`${geom.origin[0] + dx},${geom.origin[1] + dy}`);
    if (!node) continue;
    total += 1;
    if (geom.bought.has(node)) yours += 1;
  }
  return { total, yours };
}

// --- geometry --------------------------------------------------------------

function adjacency(positions) {
  const byCell = new Map();
  for (const [node, [x, y]] of Object.entries(positions)) byCell.set(`${x},${y}`, node);
  const out = new Map();
  for (const [node, [x, y]] of Object.entries(positions)) {
    const near = [];
    for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      const other = byCell.get(`${x + dx},${y + dy}`);
      if (other) near.push(other);
    }
    out.set(node, near);
  }
  return out;
}

// The cheapest path from `target` back to any already-selected node, so a
// click buys what the game would make you buy. BFS over the model graph with
// selected nodes at zero cost — plain Dijkstra would be overkill and the
// costs are tiny integers, so this is a uniform-cost search on a small graph.
function pathToSelected(target, adj, selected, nodes) {
  if (selected.has(target)) return [];
  const best = new Map([[target, 0]]);
  const prev = new Map();
  const queue = [[0, target]];
  let goal = null;
  while (queue.length) {
    queue.sort((a, b) => a[0] - b[0]);
    const [cost, node] = queue.shift();
    if (cost > (best.get(node) ?? Infinity)) continue;
    if (selected.has(node) || node === ROOT) { goal = node; break; }
    for (const other of adj.get(node) || []) {
      const step = selected.has(other) ? 0 : (nodes[other]?.cost ?? 0);
      const next = cost + step;
      if (next < (best.get(other) ?? Infinity)) {
        best.set(other, next);
        prev.set(other, node);
        queue.push([next, other]);
      }
    }
  }
  if (goal === null) return null;
  const path = [];
  for (let n = goal; n !== undefined; n = prev.get(n)) {
    if (!selected.has(n)) path.push(n);
    if (n === target) break;
  }
  return path;
}

// Selected nodes that still reach ROOT through other selected nodes.
function reachable(selected, adj) {
  if (!selected.has(ROOT)) return new Set();
  const seen = new Set([ROOT]);
  const queue = [ROOT];
  while (queue.length) {
    for (const other of adj.get(queue.pop()) || []) {
      if (selected.has(other) && !seen.has(other)) { seen.add(other); queue.push(other); }
    }
  }
  return seen;
}

// --- the verdict panel -----------------------------------------------------

function verdict(chrId) {
  const { nodes, models, members, player_level } = state.data;
  const model = members.find((m) => m.chr === chrId)?.model;
  const positions = models[model] || {};
  const adj = adjacency(positions);
  const selected = state.selection.get(chrId) || new Set();

  let spent = 0;
  for (const node of selected) spent += nodes[node]?.cost ?? 0;

  const connected = reachable(selected, adj);
  const orphans = [...selected].filter((n) => !connected.has(n));
  const locked = player_level == null
    ? []
    : [...selected].filter((n) => (nodes[n]?.req_level ?? 0) > player_level);

  return { spent, orphans, locked, adj, positions, selected };
}

// --- rendering -------------------------------------------------------------

function drawTree(host, chrId) {
  host.replaceChildren();
  const { nodes, branches } = state.data;
  const v = verdict(chrId);
  const cover = connectCover(chrId);
  const entries = Object.entries(v.positions);
  if (!entries.length) { host.append(el("p", "note", "no geometry for this character")); return; }

  const xs = entries.map(([, p]) => p[0]);
  const ys = entries.map(([, p]) => p[1]);
  const minX = Math.min(...xs), minY = Math.min(...ys);
  const w = (Math.max(...xs) - minX) * CELL + PAD * 2;
  const h = (Math.max(...ys) - minY) * CELL + PAD * 2;
  const px = (x) => (x - minX) * CELL + PAD;
  // NO FLIP. `models` already stores SCREEN-like coordinates: y grows
  // DOWNWARD. On model-002 the R (Leader) branch runs y −14…−1 (median −8)
  // and G runs 1…9 (median 7), and the game draws Leader at the TOP. So the
  // smallest y is the top row and mapping it straight through is correct; a
  // flip would put the whole red branch at the bottom, mirroring the board.
  const py = (y) => (y - minY) * CELL + PAD;

  const svg = svgEl("svg", { viewBox: `0 0 ${w} ${h}`, width: w, height: h, class: "tree" });

  const drawn = new Set();
  for (const [node, near] of v.adj) {
    for (const other of near) {
      const key = node < other ? `${node}|${other}` : `${other}|${node}`;
      if (drawn.has(key)) continue;
      drawn.add(key);
      const both = v.selected.has(node) && v.selected.has(other);
      svg.append(svgEl("line", {
        x1: px(v.positions[node][0]), y1: py(v.positions[node][1]),
        x2: px(v.positions[other][0]), y2: py(v.positions[other][1]),
        class: "link" + (both ? " on" : ""),
      }));
    }
  }

  for (const [node, pos] of entries) {
    const meta = nodes[node] || {};
    const hue = branches[meta.br]?.hue || "#888";
    const on = v.selected.has(node);
    const orphan = v.orphans.includes(node);
    const locked = v.locked.includes(node);

    const g = svgEl("g", { class: "node" + (on ? " on" : ""), "data-node": node });
    const cx = px(pos[0]);
    const cy = py(pos[1]);
    const socket = node.startsWith("S-");
    const r = socket ? SOCKET_R : NODE_R;

    g.append(svgEl("circle", {
      cx, cy, r,
      fill: on ? hue : "#161a24",
      // Both from the game's own palette: AlertRed, and Blk for locked — the game draws
      // locked board tiles grey. They must not collide with a branch hue (e.g. #ff5f6e is
      // the R hue, and #FFA22B is a shade off Y's #FFB028).
      stroke: orphan ? "#FF3B3B" : locked ? "#62628E" : hue,
      "stroke-width": orphan || locked ? 2.5 : 1.4,
      opacity: on ? 1 : 0.55,
    }));

    // A node inside a placed card's shape gets a halo, so the shape is
    // visible on the board rather than described in a panel. The socket the
    // card sits in gets a filled ring instead — the two must not look alike,
    // because one is the cause and the others are the effect.
    const permil = cover.get(node);
    if (permil) {
      g.append(svgEl("circle", {
        cx, cy, r: r + 4.5,
        fill: "none",
        // HiPink, from the game's palette. It has to be a colour NO branch
        // uses: the obvious amber #FFB028 IS the Y hue, so a halo in it would
        // be invisible on the 31 Y nodes that already draw that ring.
        stroke: "#FF3BC8",
        "stroke-width": 1.8,
        opacity: 0.9,
      }));
    }
    if (socket && (state.slots.get(chrId) || {})[node]) {
      g.append(svgEl("circle", {
        cx, cy, r: r + 3,
        fill: "none", stroke: "#28C3FF", "stroke-width": 2.4,
      }));
    }

    // The game's own glyph, from dumps/skill_icons/. Sockets have none (they
    // grant nothing), and a node whose icon was never extracted gets none
    // either — the server sends "" rather than a substitute, because a wrong
    // symbol on a real node is worse than a bare one.
    if (meta.icon) {
      const side = r * 1.45;
      // Unbought nodes keep the glyph but dim it, so the board still reads as
      // a plan rather than as a wall of identical circles.
      const dim = on ? 1 : 0.4;
      // A "level up" node's own asset is a hollow FRAME; the granted skill's
      // glyph goes INSIDE it, or the node reads as an empty box (R-031). The
      // inner glyph is drawn first, then the frame over it — the same order
      // build_explorer.py composites in. On the 128x128 frame its
      // enclosed hole spans 0.117..0.883 on both axes and the explorer fits
      // the inner glyph to 86 % of that, i.e. 0.659 of the frame side.
      if (meta.icon_inner) {
        const innerSide = side * 0.659;
        g.append(svgEl("image", {
          href: `/icon/${meta.icon_inner}.png`,
          x: cx - innerSide / 2, y: cy - innerSide / 2,
          width: innerSide, height: innerSide,
          opacity: dim, class: "glyph",
        }));
      }
      g.append(svgEl("image", {
        href: `/icon/${meta.icon}.png`,
        x: cx - side / 2, y: cy - side / 2, width: side, height: side,
        opacity: dim, class: "glyph",
      }));
    }
    const title = svgEl("title", {});
    title.textContent =
      `${node} — ${branches[meta.br]?.name || meta.br} · ${meta.cost} pt` +
      (meta.req_level ? ` · account level ${meta.req_level}` : "") +
      (meta.d ? `\n${meta.d}` : "") +
      (orphan ? "\n⚠ no path to S-001" : "") +
      (locked ? "\n⚠ above your account level" : "") +
      (cover.get(node)
        ? `\nConnect: effect ×${(1 + cover.get(node) / 1000).toFixed(2)}`
        : "");
    g.append(title);
    // A socket is the one node you do something ELSE with, so it opens the
    // card picker instead of toggling. Buying and selling it stays available
    // there — folding both into a click would make "buy" and "place a card"
    // the same gesture, and they are not the same decision.
    g.addEventListener("click", () =>
      socket ? openSocket(chrId, node) : toggle(chrId, node));
    svg.append(g);
  }

  // The board is a viewport onto a 1,874 x 850 plane, not a block that sets
  // the page's height: rendered at full size, half of it would be off the
  // bottom of the window. The SVG rides on a stage that pans and zooms and
  // the tree opens centred, which is how the game presents it.
  const stage = el("div", "bstage");
  stage.append(svg);
  host.append(stage);
  installPanZoom(host, stage, w, h);
}

// --- pan and zoom -----------------------------------------------------------
// Deliberately hand-rolled and small: a transform on one wrapper, pointer
// events for the drag, wheel for the zoom. The alternative is a library, and
// this project's whole constraint is not adding one for 60 lines of work.
//
// The transform lives on the STAGE, never on the <svg>'s viewBox: the node
// geometry, the branch hues and the socket halos are all measured, and a
// viewBox rewrite would resample them. Scaling a wrapper moves the picture
// without touching a single drawn coordinate.
const ZOOM_MIN = 0.35;
const ZOOM_MAX = 2.6;

function installPanZoom(host, stage, w, h) {
  const view = { x: 0, y: 0, k: 1 };

  const apply = () => {
    stage.style.transform =
      `translate(${view.x}px, ${view.y}px) scale(${view.k})`;
  };

  // Fit the whole board, then centre it. `min(1, ...)` because a board smaller
  // than the viewport should sit at its true size rather than be blown up.
  function fit() {
    const box = host.getBoundingClientRect();
    if (!box.width || !box.height) return;
    view.k = Math.min(1, Math.min(box.width / w, box.height / h) * 0.94);
    view.x = (box.width - w * view.k) / 2;
    view.y = (box.height - h * view.k) / 2;
    apply();
  }

  // Zoom about a point, so the node under the cursor stays under the cursor.
  function zoomAt(clientX, clientY, factor) {
    const box = host.getBoundingClientRect();
    const px = clientX - box.left, py = clientY - box.top;
    const k = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, view.k * factor));
    if (k === view.k) return;
    view.x = px - ((px - view.x) / view.k) * k;
    view.y = py - ((py - view.y) / view.k) * k;
    view.k = k;
    apply();
  }

  host.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    zoomAt(ev.clientX, ev.clientY, ev.deltaY < 0 ? 1.12 : 1 / 1.12);
  }, { passive: false });

  let drag = null;
  // ⚠ Set by a drag, cleared on the next turn of the event loop. The `click`
  // that follows a `pointerup` is dispatched before that timeout runs, so this
  // suppresses exactly the click the drag produced and nothing else.
  //
  // A `{once:true}` capture listener would be wrong: nothing consumes a
  // one-shot listener until SOME click arrives, so a drag followed a minute
  // later by a deliberate click on a node would silently eat that click. Buying a node buys its
  // whole path, so "the first click after a pan does nothing" is a real cost.
  let swallow = false;
  host.addEventListener("click", (ev) => {
    if (!swallow) return;
    ev.stopPropagation();
    ev.preventDefault();
  }, true);

  host.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    drag = { x: ev.clientX, y: ev.clientY, ox: view.x, oy: view.y, moved: 0 };
    host.setPointerCapture(ev.pointerId);
    host.classList.add("grabbing");
  });
  host.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
    drag.moved = Math.max(drag.moved, Math.abs(dx) + Math.abs(dy));
    view.x = drag.ox + dx;
    view.y = drag.oy + dy;
    apply();
  });
  const endDrag = (ev) => {
    if (!drag) return;
    // A drag that crossed the board must not also BUY the node it finished
    // over. Buying a node buys its whole path, so a stray purchase here is not
    // a cosmetic slip -- it is points spent. 4px of slop keeps a real click
    // (which always jitters a little) working.
    const moved = drag.moved;
    drag = null;
    host.classList.remove("grabbing");
    if (host.hasPointerCapture?.(ev.pointerId)) host.releasePointerCapture(ev.pointerId);
    if (moved > 4) {
      swallow = true;
      setTimeout(() => { swallow = false; }, 0);
    }
  };
  host.addEventListener("pointerup", endDrag);
  host.addEventListener("pointercancel", endDrag);
  host.addEventListener("dblclick", fit);

  const controls = el("div", "bzoom");
  const btn = (label, title, fn) => {
    const b = el("button", "ghost sm", label);
    b.type = "button";
    b.title = title;
    b.addEventListener("click", (ev) => { ev.stopPropagation(); fn(); });
    return b;
  };
  const centre = () => {
    const box = host.getBoundingClientRect();
    return [box.left + box.width / 2, box.top + box.height / 2];
  };
  controls.append(
    btn("−", "Zoom out", () => zoomAt(...centre(), 1 / 1.25)),
    btn("+", "Zoom in", () => zoomAt(...centre(), 1.25)),
    btn("Fit", "Fit the whole board (or double-click it)", fit)
  );
  host.append(controls);

  fit();
  // ⚠ The board is drawn by `mountBoard` at page load, while `#bwrap` is still
  // `hidden` — so this first `fit()` measures a 0x0 box, bails, and leaves the
  // stage untransformed at top-left, which is invisible in the code: the
  // function is called, it just has nothing to measure. A ResizeObserver fixes
  // that because it fires when the pane actually GETS a size — on the first
  // reveal, and again on every window resize.
  let seen = 0;
  const ro = new ResizeObserver(() => {
    const box = host.getBoundingClientRect();
    if (!box.width || !box.height) return;
    // Re-fit on the first real measurement and on genuine resizes only —
    // not on the reflow this page's own zooming causes.
    if (Math.abs(box.width - seen) < 1) return;
    seen = box.width;
    fit();
  });
  ro.observe(host);
}

function toggle(chrId, node) {
  const v = verdict(chrId);
  const selected = new Set(v.selected);
  if (selected.has(node)) {
    // Removing a node can strand everything behind it. Drop the node, then
    // drop whatever no longer reaches the root — silently leaving orphans
    // would let the panel show a cost the game would never charge.
    selected.delete(node);
    const keep = reachable(selected, v.adj);
    for (const other of [...selected]) if (!keep.has(other)) selected.delete(other);
  } else {
    // Buying a node buys its path. This is the 228-vs-293 rule made
    // interactive rather than explained in a footnote.
    const path = pathToSelected(node, v.adj, selected, state.data.nodes);
    if (path === null) return;
    for (const step of path) selected.add(step);
    selected.add(node);
    selected.add(ROOT);
  }
  state.selection.set(chrId, selected);
  state.dirty = true;
  render();
}

function openSocket(chrId, socket) {
  state.openSocket = state.openSocket === socket ? null : socket;
  state.filter = "";
  render();
}

function placeCard(chrId, socket, cardId) {
  const slots = { ...(state.slots.get(chrId) || {}) };
  if (cardId === null) delete slots[socket];
  else {
    // One socket per card, account-wide. Rather than refuse, MOVE it: the
    // user asking for a card that is elsewhere is asking to move it, and
    // making them go and clear the other board first would be busywork.
    for (const [otherChr, otherSlots] of state.slots) {
      for (const [otherSocket, card] of Object.entries(otherSlots)) {
        if (card !== cardId) continue;
        if (otherChr === chrId) delete slots[otherSocket];
        else {
          const copy = { ...otherSlots };
          delete copy[otherSocket];
          state.slots.set(otherChr, copy);
        }
      }
    }
    slots[socket] = cardId;
  }
  state.slots.set(chrId, slots);
  state.dirty = true;
  render();
}

/** The key to the board.
 *
 *  Every hue here comes from `/api/board`'s `branches`, which is the GAME'S
 *  own palette — not retyped, so it cannot drift from
 *  what `drawTree` actually paints. The four state markers below are the
 *  literals `drawTree` uses, and they are game colours too: AlertRed for an
 *  orphan, `Blk` for locked (the game draws locked tiles grey), HiPink for a
 *  Connect halo and `Blu1st` for the socket holding the card.
 *
 *  The `when` text is the game's own rule for each branch and is the single
 *  most load-bearing line on this screen: a Leader-branch node pays only
 *  while that member leads, and nothing else on the page says so. */
function drawLegend() {
  const host = document.getElementById("blegend");
  if (!host) return;
  host.replaceChildren();
  const branches = state.data?.branches || {};

  const swatch = (stroke, fill, label, hint) => {
    const item = el("span", "lg-item");
    const dot = el("i", "lg-dot");
    dot.style.borderColor = stroke;
    dot.style.background = fill || "transparent";
    item.append(dot, el("b", null, label));
    if (hint) item.append(el("span", "lg-hint", hint));
    return item;
  };

  const row1 = el("div", "lg-row");
  row1.append(el("span", "lg-lab", "Branches"));
  for (const [key, b] of Object.entries(branches)) {
    // A branch's own hue, filled — which is exactly how a BOUGHT node draws.
    row1.append(swatch(b.hue, b.hue, `${b.name}`, b.when));
  }
  host.append(row1);

  const row2 = el("div", "lg-row");
  row2.append(el("span", "lg-lab", "Nodes"));
  const anyHue = branches.G?.hue || "#3BC799";
  row2.append(
    swatch(anyHue, anyHue, "Bought", "you own it and it is scoring"),
    swatch(anyHue, null, "Not bought", "clicking buys it AND its whole path"),
    swatch("#62628E", null, "Locked", `above your account level${
      state.data?.player_level ? ` (${state.data.player_level})` : ""}`),
    swatch("#FF3B3B", null, "Stranded", "no path back to the root, so it pays nothing"),
    swatch("#FF3BC8", null, "Boosted", "inside a Connect card's shape"),
    swatch("#28C3FF", null, "Connect socket", "holding the card doing the boosting"),
  );
  host.append(row2);
}

/** The picker. Says what a card would DO here, not just what it is. */
function drawSocketPanel(chrId) {
  const host = document.getElementById("bsocket");
  const socket = state.openSocket;
  const cat = state.data.sockets;
  host.replaceChildren();
  if (!socket || !cat || !cat.cards.length) { host.hidden = true; return; }
  host.hidden = false;

  const bought = (state.selection.get(chrId) || new Set()).has(socket);
  const current = (state.slots.get(chrId) || {})[socket] || null;
  const seats = cardSeats();

  const head = el("div", "brow");
  head.append(el("b", null, `${socket} — Connect socket`));
  if (socket !== ROOT) {
    head.append(el("span", bought ? "chint" : "cstatus dirty",
      bought ? "bought" : "not bought — buy it before a card counts"));
    const buy = el("button", "ghost", bought ? "sell socket" : "buy socket");
    buy.addEventListener("click", () => { toggle(chrId, socket); });
    head.append(buy);
  } else {
    head.append(el("span", "chint", "the free root — always yours"));
  }
  const close = el("button", "ghost", "close");
  close.addEventListener("click", () => { state.openSocket = null; render(); });
  head.append(close);
  host.append(head);

  if (current) {
    const card = cat.cards.find((c) => c.id === current);
    const cover = card ? coverageAt(socketGeometry(chrId, socket), card) : null;
    const row = el("div", "brow");
    row.append(el("span", "chint",
      card
        ? `Holding ${card.member} ${card.rarity} — ` +
          `×${(1 + card.permil / 1000).toFixed(2)}` +
          (cover
            ? ` over ${cover.total} node${cover.total === 1 ? "" : "s"}, ` +
              `${cover.yours} of them bought`
            : ` — ${card.area}`)
        : `Holding ${current} — this card has no Connect effect, so it grants nothing`));
    const clear = el("button", "ghost", "remove");
    clear.addEventListener("click", () => placeCard(chrId, socket, null));
    row.append(clear);
    host.append(row);
  }

  const search = el("input");
  search.type = "search";
  search.placeholder = "filter by holomem, rarity or shape";
  search.value = state.filter;
  search.addEventListener("input", () => {
    state.filter = search.value;
    drawSocketPanel(chrId);      // redraw only the list; the tree has not moved
  });
  host.append(search);

  const geom = socketGeometry(chrId, socket);

  const needle = state.filter.trim().toLowerCase();
  const shown = cat.cards
    .filter((c) =>
      !needle ||
      `${c.member} ${c.rarity} ${c.area}`.toLowerCase().includes(needle))
    // Two keys, and neither invents a "value". First: does this card reach ANY
    // node you have bought? A card covering none is worth exactly zero here,
    // today, and sorting purely by multiplier floats those to the top — the
    // strongest cards in the catalogue may cover 3 nodes none of which are
    // bought, so the honest-looking ordering would be the misleading one.
    // Second, within each group: the bigger multiplier. Trading a smaller
    // multiplier over more of your nodes against a larger one over fewer is a
    // judgement the reader makes from the coverage on each row; the sort does
    // not make it for them.
    .slice()
    .sort((a, b) => {
      const ca = coverageAt(geom, a);
      const cb = coverageAt(geom, b);
      const ua = ca && ca.yours ? 1 : 0;
      const ub = cb && cb.yours ? 1 : 0;
      if (ua !== ub) return ub - ua;
      return (b.permil || 0) - (a.permil || 0);
    });

  // The one thing a multiplier alone does not tell you, said once rather than
  // relied upon being inferred from 60 rows.
  host.append(el("p", "note",
    "A Connect card multiplies the nodes its shape reaches — so a big " +
    "multiplier over nodes you have not bought is worth nothing. Each row " +
    "says how many of the nodes it covers are yours."));

  const list = el("div", "bcards");
  for (const card of shown.slice(0, 60)) {
    const seat = seats.get(card.id);
    const here = seat === `${chrId}/${socket}`;
    const row = el("button", "bcard" + (here ? " on" : ""));
    row.append(el("b", null, `${card.member} ${card.rarity}`));
    row.append(el("span", "mono", `×${(1 + card.permil / 1000).toFixed(2)}`));
    const cov = coverageAt(geom, card);
    row.append(el("span", cov && cov.yours ? "chint" : "chint dim",
      cov
        ? `covers ${cov.total} node${cov.total === 1 ? "" : "s"} · ` +
          `${cov.yours} yours`
        : card.area));
    // Why the multiplier is what it is. `card_default: "max"` holds for STATS
    // but not for Connect — assuming Bloom 5 overshoots the game's own
    // +28.73 pp by +16.31 % (tests/test_board.py pins it), and socket cards are
    // often below Bloom 5. So an unstated Bloom shows the FLOOR and says so,
    // rather than quietly showing a ceiling.
    if (card.basis !== "manual") {
      row.append(el("span", "term-basis",
        `Bloom not entered — floor ×${(1 + card.permil / 1000).toFixed(2)}, ` +
        `×${(1 + card.permil_at_max / 1000).toFixed(2)} at Bloom 5`));
    } else if (card.level < 2) {
      row.append(el("span", "term-basis",
        `Bloom ${card.bloom} — ×${(1 + card.permil_at_max / 1000).toFixed(2)} at Bloom 5`));
    }
    // Not `in use: chr-00023/S-003`: users do not know the chr ids, and a card
    // is MOVED rather than refused, so the row has to say who loses it.
    if (seat && !here) {
      const [otherChr, otherSocket] = seat.split("/");
      row.append(el("span", "cstatus dirty",
        otherChr === chrId
          ? `moves from ${otherSocket}`
          : `takes it from ${holomemName(otherChr)} · ${otherSocket}`));
    }
    row.addEventListener("click", () => placeCard(chrId, socket, here ? null : card.id));
    list.append(row);
  }
  if (shown.length > 60) {
    list.append(el("p", "note", `${shown.length - 60} more — narrow the filter`));
  }
  host.append(list);
}

function render() {
  const host = document.getElementById("btree");
  const info = document.getElementById("binfo");
  if (!host || !state.chr) return;
  drawTree(host, state.chr);
  drawLegend();
  drawSocketPanel(state.chr);

  const v = verdict(state.chr);
  const entry = state.data.characters[state.chr] || {};
  // The REAL budget: what this character has EARNED, from
  // `cost_of(unlocked) + unspent-from-User/Get`. Falls back to the 453 of a
  // full board only when there is no capture for this character, and says which it is showing, because spending
  // against a ceiling you do not have is the one thing this panel must not
  // quietly encourage.
  const earned = entry.budget;
  const total = earned ?? state.data.total_points;
  info.replaceChildren();

  const line = el("div", "brow");
  line.append(el("b", "mono", `${v.spent} / ${total} pt`));
  if (earned != null) {
    const left = earned - v.spent;
    line.append(el("span", left < 0 ? "cstatus err" : "chint",
      left < 0 ? `${-left} over your budget` : `${left} unspent`));
  } else {
    line.append(el("span", "term-basis", "no budget — full board shown"));
  }
  line.append(el("span", "chint", `${v.selected.size} of 154 nodes`));
  line.append(el("span", "term-basis", entry.source));
  info.append(line);

  if (v.orphans.length) {
    info.append(el("p", "cstatus err",
      `${v.orphans.length} node(s) have no path to ${ROOT} — a node cannot be bought without its path`));
  }
  if (v.locked.length) {
    info.append(el("p", "cstatus dirty",
      `${v.locked.length} node(s) need a higher account level than ${state.data.player_level}`));
  }
  if (state.dirty) {
    info.append(el("p", "cstatus dirty", "edited — not saved yet"));
  }
  // Only a genuinely unresolvable glyph is reported. NOT the "addition"
  // family: `build_explorer.py` established that the game ships no tree icon
  // for those and never will — such a node draws the icon of the skill it
  // GRANTS, which is why `icon_sk` is the correct answer there rather than a
  // fallback. Telling the user to re-run an extractor for an asset that does
  // not exist would be a wild goose chase.
  if (state.data.missing_icons?.length) {
    info.append(el("p", "note",
      `${state.data.missing_icons.length} node glyph(s) could not be resolved: ` +
      state.data.missing_icons.join(", ")));
  }
}

// --- persistence -----------------------------------------------------------

async function save() {
  const status = document.getElementById("bstatus");
  status.textContent = "saving…";
  status.className = "cstatus";

  // Read-modify-write the WHOLE collection document: the board editor owns
  // `characters` and must not clobber `cards` or `player`, which the other
  // editor owns. One write sink, one document.
  const current = await (await fetch("/api/collection")).json();
  const document_ = current.document || {};
  const characters = { ...(document_.characters || {}) };
  for (const [chrId, selected] of state.selection) {
    // The EDITED slots, not the captured ones: copying
    // `state.data.characters[chrId].slots` straight back would silently
    // discard every placement.
    const slots = state.slots.get(chrId) || {};
    if (!selected.size && !Object.keys(slots).length) {
      // An untouched character is OMITTED, not written as an empty board.
      // Writing all 54 would mark the ones that appear in no capture as
      // "edited", which destroys the captured-vs-edited distinction the whole
      // panel rests on — and would claim the user had decided something
      // about characters they have never opened.
      delete characters[chrId];
      continue;
    }
    characters[chrId] = { nodes: [...selected].sort(), slots };
  }
  if (Object.keys(characters).length) document_.characters = characters;
  else delete document_.characters;

  const response = await fetch("/api/collection", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(document_),
  });
  const body = await response.json();
  if (!response.ok) {
    status.textContent = body.error || "save failed";
    status.className = "cstatus err";
    return;
  }
  state.dirty = false;
  status.textContent = `saved to ${body.path || "state/collection.json"} — Solve now scores this board`;
  status.className = "cstatus ok";
  render();
}

// --- mount -----------------------------------------------------------------

export async function mountBoard() {
  const wrap = document.getElementById("bwrap");
  if (!wrap) return;
  let data;
  try {
    data = await (await fetch("/api/board")).json();
  } catch {
    return;
  }
  if (!data.available) {
    document.getElementById("binfo").append(el("p", "note", data.reason || "unavailable"));
    return;
  }
  state.data = data;

  for (const [chrId, entry] of Object.entries(data.characters)) {
    const nodes = new Set(entry.nodes || []);
    // ROOT is always present. It costs 0 and carries no unlock requirement
    // (the free root at (0,0) on every model), so the capture only lists it
    // on the few boards that explicitly bought it. Without this, every other
    // captured board reads as fully disconnected, because nothing reaches a
    // root that is not selected.
    if (nodes.size) nodes.add(ROOT);
    state.selection.set(chrId, nodes);
    state.slots.set(chrId, { ...(entry.slots || {}) });
  }

  const picker = document.getElementById("bchr");
  for (const member of data.members) {
    const entry = data.characters[member.chr] || {};
    const spent = (entry.nodes || []).reduce((a, n) => a + (data.nodes[n]?.cost ?? 0), 0);
    const idle = entry.budget == null ? null : entry.budget - spent;
    const option = el("option", null,
      `${member.name} — ${spent} pt` +
      (idle ? `, ${idle} idle` : "") +
      (entry.source === "empty" ? " (no board)" : ""));
    option.value = member.chr;
    picker.append(option);
  }
  picker.addEventListener("change", () => {
    state.chr = picker.value;
    // The open picker belongs to the character it was opened on. Carrying it
    // across a switch makes the next click on that socket CLOSE it instead of
    // opening it, which reads as the panel being broken.
    state.openSocket = null;
    state.filter = "";
    render();
  });
  state.chr = data.members[0]?.chr || null;
  picker.value = state.chr;

  document.getElementById("bsave").addEventListener("click", save);
  render();
}
