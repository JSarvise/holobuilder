// Optimizer: secondary targets picked on the holomem's own board. A target is {chr, node, mode}: "buy" = bought when the plan can afford and
// reach it, "keep" = never bought, kept only when owned. The plan itself is the server's
// (board_plan_solver `targets`); this file only draws the board and edits the list.
//
// Geometry and node text are the server's: GET /api/board (glyphs, what each board owns)
// and POST /api/board-doc for the one holomem shown (positions, effect, value, cost).
// Links are the orthogonal 4-neighbour grid, as the Holomem Board page draws them.
import { byId, esc, fmt, toast, getJSON, postJSON, chrOf } from "/opt-util.js";
import { BRANCH, effectText } from "/hbd-util.js";

const NS = "http://www.w3.org/2000/svg";
const CELL = 34, PAD = 26, R1 = 11.5, R2 = 13.5, RS = 13;
const T = { S: null, onChange: null, names: new Map(), board: null, docs: new Map(),
  chr: "", mode: "buy", dirty: false, hover: null };

function el(tag, attrs, parent) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) n.setAttribute(k, v);
  if (parent) parent.append(n);
  return n;
}
const nameOf = chr => T.names.get(chr) || chr;
const modeOf = (chr, node) => (T.S.targets.find(t => t.chr === chr && t.node === node) || {}).mode || null;

async function docOf(chr) {
  if (!T.board) T.board = await getJSON("/api/board");
  if (!T.docs.has(chr)) {
    const c = (T.board.characters || {})[chr] || {};
    const doc = await postJSON("/api/board-doc", {
      board_state: { [chr]: { nodes: c.nodes || [], slots: c.slots || {} } }, cards: [], basis: "account" });
    T.docs.set(chr, (doc.characters || []).find(x => x.chr === chr) || null);
  }
  return T.docs.get(chr);
}

/** The chips under "Secondary targets" (and the dialog's own list). */
function paintLists() {
  const chip = (t, i) => `<span class="o-chip"><i class="o-tdot ${t.mode}"></i>${esc(nameOf(t.chr))} · ${esc(t.node)} · ${t.mode === "keep" ? "keep only" : "buy"}
    <button type="button" data-tgdel="${i}" aria-label="Remove target">×</button></span>`;
  byId("tglist").innerHTML = T.S.targets.map(chip).join("");
  byId("tgbmine").innerHTML = T.S.targets.length ? T.S.targets.map(chip).join("") : `<p class="o-muted">No targets yet.</p>`;
}

function info(ch, id) {
  const box = byId("tgbinfo");
  const n = ch && id && (ch.nodes || []).find(x => x.id === id);
  if (!n) {
    box.innerHTML = `<p class="o-muted">Point at a node to read it. Sockets (squares) hold Connect cards and cannot be targets.</p>`;
    return;
  }
  const b = BRANCH[n.branch] || {};
  const m = modeOf(ch.chr, n.id);
  const frozen = !T.S.branches.includes(n.branch);
  const pts = (n.cost || {}).points;
  box.innerHTML = `<p class="o-tgb-br" style="color:${b.hue}">${esc(b.name || n.branch)}</p>
    <p class="o-tgb-fx">${esc(effectText(n.effect, ch.name, n.value_text))}</p>
    <p class="o-muted">${esc(n.id)} · ${n.state === "owned" ? "owned now" : "not owned"}${pts != null ? ` · ${fmt(pts)} Holomem Board Pt` : ""}${
      n.unlock_points && n.state !== "owned" ? ` · ${fmt(n.unlock_points)} Pt with its path` : ""}</p>
    ${frozen ? `<p class="o-tgb-warn">This branch is switched off under "Branches the plan may change", so the plan leaves it as it is.</p>` : ""}
    ${m ? `<p class="o-tgb-on"><i class="o-tdot ${m}"></i>${m === "keep" ? "Keep only" : "Buy if possible"}</p>` : ""}`;
}

function draw() {
  const host = byId("tgbcanvas");
  const chr = T.chr;
  host.innerHTML = `<p class="o-muted" style="padding:24px">Loading ${esc(nameOf(chr))}'s board…</p>`;
  docOf(chr).then(ch => {
    if (chr !== T.chr) return;
    if (!ch || !(ch.nodes || []).length) {
      host.innerHTML = `<p class="o-muted" style="padding:24px">No board for ${esc(nameOf(chr))}.</p>`;
      info(null); return;
    }
    render(host, ch);
  }).catch(e => { host.innerHTML = `<div class="error" style="margin:24px">The board could not be loaded. ${esc(e.message)}</div>`; });
}

function render(host, ch) {
  const nodes = (ch.nodes || []).filter(n => n.branch !== "S");
  const sockets = ch.sockets || [];
  const all = [...nodes, ...sockets.map(s => ({ ...s, id: s.socket, branch: "S" }))];
  const xs = all.map(n => n.pos[0]), ys = all.map(n => n.pos[1]);
  const minX = Math.min(...xs), minY = Math.min(...ys);
  const W = (Math.max(...xs) - minX) * CELL + PAD * 2, H = (Math.max(...ys) - minY) * CELL + PAD * 2;
  const px = x => (x - minX) * CELL + PAD, py = y => (y - minY) * CELL + PAD;
  const owned = new Set(nodes.filter(n => n.state === "owned").map(n => n.id));
  const glyphs = (T.board && T.board.nodes) || {};

  const svg = el("svg", { class: "o-tgb-svg", viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `${ch.name}'s Holomem Board`, preserveAspectRatio: "xMidYMid meet" });
  const defs = el("defs", {}, svg);
  for (const [b, v] of Object.entries(BRANCH)) {
    const g = el("linearGradient", { id: `tgb-g-${b}`, x1: 0, y1: 0, x2: 1, y2: 1 }, defs);
    el("stop", { offset: "0", "stop-color": v.g1 }, g);
    el("stop", { offset: "1", "stop-color": v.g2 }, g);
  }
  const at = new Map(all.map(n => [`${n.pos[0]},${n.pos[1]}`, n]));
  const links = el("g", { class: "lk" }, svg);
  for (const n of all) for (const [dx, dy] of [[1, 0], [0, 1]]) {
    const m = at.get(`${n.pos[0] + dx},${n.pos[1] + dy}`);
    if (!m) continue;
    const on = (owned.has(n.id) || n.branch === "S") && (owned.has(m.id) || m.branch === "S");
    const br = n.branch === "S" ? m.branch : n.branch;
    el("line", { x1: px(n.pos[0]), y1: py(n.pos[1]), x2: px(m.pos[0]), y2: py(m.pos[1]),
      class: on ? "on" : "", stroke: on ? BRANCH[br].hue : null }, links);
  }
  for (const s of sockets) {
    const cx = px(s.pos[0]), cy = py(s.pos[1]);
    el("rect", { x: cx - RS, y: cy - RS, width: RS * 2, height: RS * 2, rx: 5, class: "sk" }, svg);
  }
  const layer = el("g", {}, svg);
  for (const n of nodes) {
    const cx = px(n.pos[0]), cy = py(n.pos[1]), r = n.star === 2 ? R2 : R1;
    const on = owned.has(n.id), m = modeOf(ch.chr, n.id), frozen = !T.S.branches.includes(n.branch);
    const g = el("g", { class: `nd${on ? " on" : ""}${frozen ? " off" : ""}${m ? " " + m : ""}`, "data-node": n.id, tabindex: "0",
      role: "button", "aria-label": `${n.id}${m ? ", " + m : ""}` }, layer);
    el("circle", { cx, cy, r: r + 5, class: "hit" }, g);
    el("circle", { cx, cy, r, class: "disc", fill: on ? `url(#tgb-g-${n.branch})` : "#171A26",
      stroke: on ? "rgb(255 255 255 / .55)" : BRANCH[n.branch].hue, "stroke-opacity": on ? 1 : 0.6 }, g);
    const gl = glyphs[n.id];
    const icon = (gl && gl.icon) || n.effect_icon;
    if (icon) {
      const s = r * 1.42;
      el("image", { href: `/icon/${icon}.png`, x: cx - s / 2, y: cy - s / 2, width: s, height: s, class: "gly" }, g);
      if (gl && gl.icon_inner) {
        const t = s * 0.62;
        el("image", { href: `/icon/${gl.icon_inner}.png`, x: cx - t / 2, y: cy - t / 2, width: t, height: t, class: "gly" }, g);
      }
    }
    if (m === "buy") el("circle", { cx, cy, r: r + 4, class: "ring-buy" }, g);
    if (m === "keep") {
      el("circle", { cx, cy, r: r + 4, class: "ring-keep" }, g);
      el("circle", { cx, cy, r: r + 7, class: "ring-keep" }, g);
    }
  }
  host.replaceChildren(svg);
  const pick = e => e.target.closest && e.target.closest("[data-node]");
  svg.addEventListener("mouseover", e => { const t = pick(e); if (t) { T.hover = t.dataset.node; info(ch, T.hover); } });
  svg.addEventListener("focusin", e => { const t = pick(e); if (t) info(ch, t.dataset.node); });
  svg.addEventListener("click", e => { const t = pick(e); if (t) toggle(ch, t.dataset.node); });
  svg.addEventListener("keydown", e => {
    const t = pick(e);
    if (t && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggle(ch, t.dataset.node); }
  });
  info(ch, T.hover && nodes.some(n => n.id === T.hover) ? T.hover : null);
}

function toggle(ch, node) {
  if (!T.S.branches.includes(node[0])) {
    toast("That branch is switched off under \"Branches the plan may change\".");
    return;
  }
  const cur = modeOf(ch.chr, node);
  const rest = T.S.targets.filter(t => !(t.chr === ch.chr && t.node === node));
  T.S.targets = cur === T.mode ? rest : [...rest, { chr: ch.chr, node, mode: T.mode }];
  T.dirty = true; T.hover = node;
  paintLists();
  render(byId("tgbcanvas"), ch);
  const back = byId("tgbcanvas").querySelector(`[data-node="${node}"]`);
  if (back && document.activeElement && document.activeElement.closest && document.activeElement.closest("#tgbcanvas")) back.focus();
}

/** Wire the target list and the board dialog. `S.targets` is edited in place;
 *  `onChange()` runs once when the dialog closes with changes (or a chip is removed). */
export function initTargets(S, cards, onChange) {
  T.S = S; T.onChange = onChange;
  for (const c of cards) if (c.owned && !T.names.has(chrOf(c.id))) T.names.set(chrOf(c.id), c.member);
  const sorted = [...T.names].sort((a, b) => a[1].localeCompare(b[1]));
  byId("tgbchr").innerHTML = sorted.map(([chr, m]) => `<option value="${esc(chr)}">${esc(m)}</option>`).join("");
  paintLists();
  const dlg = byId("tgboard");
  byId("tgopen").onclick = () => {
    if (!T.chr) T.chr = (S.targets[S.targets.length - 1] || {}).chr || (sorted[0] || [])[0] || "";
    byId("tgbchr").value = T.chr;
    T.dirty = false;
    dlg.showModal();
    draw();
  };
  byId("tgbchr").onchange = e => { T.chr = e.target.value; T.hover = null; draw(); };
  const seg = byId("tgbmode");
  seg.addEventListener("click", e => {
    const b = e.target.closest("[data-v]"); if (!b) return;
    T.mode = b.dataset.v;
    seg.querySelectorAll("[data-v]").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
  });
  dlg.addEventListener("close", () => { if (T.dirty) { T.dirty = false; onChange(); } });
  const del = e => {
    const b = e.target.closest("[data-tgdel]"); if (!b) return;
    const gone = S.targets[+b.dataset.tgdel];
    S.targets = S.targets.filter((_t, i) => i !== +b.dataset.tgdel);
    paintLists();
    if (dlg.open) { T.dirty = true; if (gone && gone.chr === T.chr) draw(); }
    else onChange();
  };
  byId("tglist").addEventListener("click", del);
  byId("tgbmine").addEventListener("click", del);
}
