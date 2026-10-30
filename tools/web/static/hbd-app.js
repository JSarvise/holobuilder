// Holomem Board page: loads the account's boards and draws the roster, one board, and the
// account-wide summaries. The Optimizer plans a team's boards; this page shows the boards as
// they are.
//
// Sources (all read-only):
//   GET  /api/board       geometry, node glyphs + player-level gates, which boards are captured
//   POST /api/board-doc   every holomem's board as it is (board-export/v0): nodes with cost,
//                         state, unlock path and value; sockets with the placed card and the
//                         nodes its Connect covers; the Connect catalogue; points
//   GET  /api/resources   the Cube/Hexa wallet and Hologold
//   GET  /api/collection  owned cards (team picker, socket card names)
//   GET  /api/songs       song picker
import {
  byId, esc, fmt, plural, D, BRANCH, BRANCH_ORDER, WHO, effectText, effectShort, nodeKind,
  isGreenAll, groupingOf, songBonusType, matName, wallet, genOf, nameOf, getJSON, postJSON, toast,
  parseWhen, ago, goldHeld, RESET_GOLD, matSort, boosted, fmtValue, connectView, ico, branchMark,
} from "/hbd-util.js";
import {
  drawBoard, markSelected, viewSets, socketCardFn, initPanZoom, zoomBy, fit, focusConnect, reveal,
} from "/hbd-draw.js";
import { connectStripHTML, connectInfoHTML } from "/hbd-connect.js";

const V = { chr: null, mode: "now", sort: "unspent", find: "", pinned: null, svg: null };
const LS = "hbd.view";
try { const s = JSON.parse(localStorage.getItem(LS) || "{}"); if (s.chr) V.chr = s.chr; if (s.sort) V.sort = s.sort; } catch (_) { /* private mode */ }
const remember = () => { try { localStorage.setItem(LS, JSON.stringify({ chr: V.chr, sort: V.sort })); } catch (_) { /* ignore */ } };

const icon = chr => `<img class="hbd-face" src="/chricon/${esc(chr)}" alt="" loading="lazy" onerror="this.style.visibility='hidden'">`;
const pts = c => c.points || {};

/* ------------------------------------------------------------------ the character to draw */
// With the plan shown, the plan doc's copy of the holomem (it carries plan + recommended
// sockets); else the board as it is.
function shownChar(chr) {
  if (V.mode === "plan" && D.plan) {
    const p = (D.plan.characters || []).find(c => c.chr === chr);
    if (p && p.plan) return p;
  }
  return D.chars.get(chr);
}
const planChar = chr => D.plan ? (D.plan.characters || []).find(c => c.chr === chr && c.plan) : null;
const planDelta = chr => {
  const p = planChar(chr);
  return p ? { buy: (p.plan.to_buy || []).length, sell: (p.plan.to_sell || []).length } : null;
};

/* ------------------------------------------------------------------ hero */
function hero() {
  const chars = [...D.chars.values()];
  const earned = chars.reduce((a, c) => a + (pts(c).granted || 0), 0);
  const spent = chars.reduce((a, c) => a + (pts(c).spent_on_owned || 0), 0);
  const unspent = chars.reduce((a, c) => a + (pts(c).unspent || 0), 0);
  const idle = chars.filter(c => (pts(c).unspent || 0) > 0).length;
  const touched = chars.filter(c => (c.owned_count || 0) > 1).length;
  const gold = goldHeld();
  // stat tiles (hb.css): no box of their own, split by a 1 px rule; the official item thumb where one exists
  const stat = (v, label, note, cls, art) => `<div class="stat-tile${cls ? " " + cls : ""}">${art || ""}<div><span class="lbl">${label}</span><span class="n">${v}</span>${note ? `<small>${note}</small>` : ""}</div></div>`;
  byId("stats").innerHTML =
    stat(fmt(earned), "Holomem Board Pt earned", "", "", `<img class="si" src="/ui/items/img_resource_thumb_skilltreepoint-cmn.webp" alt="" onerror="this.remove()">`) +
    stat(fmt(spent), "Spent on nodes") +
    stat(fmt(unspent), "Unspent", `on ${plural(idle, "holomem")}`, unspent ? "hot" : "") +
    stat(fmt(touched), "Boards with nodes") +
    (gold != null ? stat(fmt(gold), "Hologold", `covers ${fmt(Math.floor(gold / RESET_GOLD))} node resets`, "", `<img class="si" src="/itemicon/item-gold" alt="" onerror="this.remove()">`) : "");
  const parts = [];
  const d = parseWhen(D.res && D.res.inventory_captured_at);
  if (d) parts.push(`<span title="${esc(D.res.captured_at || "")}">Last refresh ${esc(window.HBFmt ? window.HBFmt.date(d) : d.toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }))} · ${esc(ago(d))}</span>`);
  if (D.board.player_level != null) parts.push(`<span>Player level ${fmt(D.board.player_level)}</span>`);
  parts.push(`<span>${fmt(D.chars.size)} holomems · ${fmt(Object.keys(D.board.models || {}).length)} board layouts</span>`);
  byId("chips").innerHTML = parts.join("");
}

/* ------------------------------------------------------------------ roster */
function roster() {
  const t = V.find.trim().toLowerCase();
  let list = [...D.chars.values()].filter(c => !t || `${c.name} ${genOf(c.chr).map(g => g.n).join(" ")}`.toLowerCase().includes(t));
  const by = {
    unspent: (a, b) => (pts(b).unspent || 0) - (pts(a).unspent || 0) || (pts(b).granted || 0) - (pts(a).granted || 0),
    spent: (a, b) => (pts(b).spent_on_owned || 0) - (pts(a).spent_on_owned || 0),
    name: (a, b) => a.name.localeCompare(b.name),
    plan: (a, b) => {
      const da = planDelta(a.chr), db = planDelta(b.chr);
      const ka = da ? da.buy + da.sell : -1, kb = db ? db.buy + db.sell : -1;
      return kb - ka || a.name.localeCompare(b.name);
    },
  }[V.sort];
  list.sort(by);
  byId("roster").innerHTML = list.map(c => {
    const p = pts(c), g = p.granted || 0, sp = p.spent_on_owned || 0, un = p.unspent || 0;
    const gen = genOf(c.chr)[0];
    const dl = planDelta(c.chr);
    const delta = dl && (dl.buy || dl.sell) ? `<span class="hbd-delta">${dl.buy ? `<i class="b">+${dl.buy}</i>` : ""}${dl.sell ? `<i class="s">−${dl.sell}</i>` : ""}</span>` : "";
    return `<li><button type="button" class="hbd-row" data-chr="${esc(c.chr)}" ${c.chr === V.chr ? 'aria-current="true"' : ""}>
      ${icon(c.chr)}
      <span class="nm"><b>${esc(c.name)}</b><small>${gen ? `<i style="--gc:${gen.c};--gc2:${gen.c2}"></i>${esc(gen.n)}` : ""}</small>
        <span class="bar" title="${fmt(sp)} of ${fmt(g)} Pt spent"><i style="width:${g ? (sp / g * 100).toFixed(1) : 0}%"></i></span></span>
      ${delta}
      <span class="un${un ? " hot" : ""}"><b class="num">${fmt(un)}</b><small>unspent</small></span>
    </button></li>`;
  }).join("") || `<li class="hbd-empty">No holomem matches “${esc(V.find)}”.</li>`;
}

/* ------------------------------------------------------------------ one board */
function stageHead(c, ch) {
  const p = pts(c);
  const src = (D.board.characters[c.chr] || {}).source;
  const srcText = { captured: "Read from the game at the last refresh", edited: "Edited by hand in the collection", empty: "No capture: shown empty" }[src] || "";
  const gens = genOf(c.chr).map(g => `<span class="hbd-gen" style="--gc:${g.c};--gc2:${g.c2}">${esc(g.n)}</span>`).join("");
  const plan = V.mode === "plan" && ch.plan;
  const ptsLine = plan
    ? `<div class="hbd-kv"><b class="num">${fmt(ch.plan.point_cost)}</b><small>of ${fmt(ch.plan.point_budget)} Pt used by the plan</small></div>
       <div class="hbd-kv buy"><b class="num">${fmt((ch.plan.to_buy || []).length)}</b><small>nodes to unlock</small></div>
       <div class="hbd-kv sell"><b class="num">${fmt((ch.plan.to_sell || []).length)}</b><small>nodes to reset</small></div>`
    : `<div class="hbd-kv"><b class="num">${fmt(p.granted)}</b><small>Pt earned</small></div>
       <div class="hbd-kv"><b class="num">${fmt(p.spent_on_owned)}</b><small>spent</small></div>
       <div class="hbd-kv${p.unspent ? " hot" : ""}"><b class="num">${fmt(p.unspent)}</b><small>unspent</small></div>`;
  byId("stagehead").innerHTML = `
    <div class="who">${icon(c.chr).replace("hbd-face", "hbd-face big")}
      <div><h3>${esc(c.name)}</h3><div class="hbd-gens">${gens}<span class="hbd-src" title="${esc(srcText)}">${esc(src === "captured" ? "From the game" : src === "edited" ? "Edited" : "No capture")}</span></div></div></div>
    <div class="hbd-kvs">${ptsLine}</div>`;
}

function legend() {
  const plan = V.mode === "plan";
  byId("legend").innerHTML = `
    <span><i class="hbd-lg on"></i>Unlocked${plan ? " after the plan" : ""}</span>
    <span><i class="hbd-lg off"></i>Not unlocked</span>
    ${plan ? `<span><i class="hbd-lg buy"></i>To unlock</span><span><i class="hbd-lg sell"></i>To reset</span>` : ""}
    <span><i class="hbd-lg con"></i>Boosted by a Connect</span>
    <span><i class="hbd-lg gated"></i>Needs a higher player level</span>`;
}

function socketCardName(id) {
  const c = ((D.coll && D.coll.cards) || []).find(x => x.id === id);
  return c ? { member: c.member, title: c.title || c.costume_name || "" } : null;
}
// The Connect strip under the board: one card per socket, hover to see its range (hbd-connect.js).
function socketsPanel(c, ch) {
  byId("sockets").innerHTML = connectStripHTML(ch, V.mode);
}

// The base value and, under a Connect, the boosted one: the figure the game shows.
function valueBlock(ch, n, CV) {
  const base = ((D.chars.get(ch.chr) || {}).nodes || []).find(x => x.id === n.id) || {};
  if (base.value == null || !base.value_unit) return { html: "", shown: null };
  const permil = CV.permil.get(n.id) || 0;
  const b = fmtValue(base.value, base.value_unit);
  if (!permil) return { html: `<div class="hbd-val"><b class="num">${esc(b)}</b><small>Value of this node</small></div>`, shown: b };
  const u = fmtValue(boosted(base.value, permil), base.value_unit);
  const src = (CV.src.get(n.id) || []).map(sk => {
    const c = CV.bySocket.get(sk);
    const nm = socketCardName(c.card);
    return `${esc(nm ? nm.member : c.card)}'s card on ${esc(sk)} (+${fmt(c.info.permil / 10)}%)`;
  }).join(" and ");
  return {
    html: `<div class="hbd-val"><b class="num">${esc(b)}</b><span class="arrow">→</span><b class="num up">${esc(u)}</b>
      <small>Boosted by ${src}. The game rounds the boosted value up.</small></div>`,
    shown: u, up: true,
  };
}

function socketInfo(ch, id) {
  return connectInfoHTML(ch, id, V.mode);
}

function nodeInfo(id) {
  const ch = shownChar(V.chr);
  if (!ch) return;
  const host = byId("nodeinfo");
  if (!id) {
    // resting state: this board by branch, one line each (the compact key); a node or Connect replaces it
    const V0 = viewSets(ch, V.mode);
    const row = (b, on, all, scope) => `<li title="${esc(WHO[b])}">${branchMark(b)}<span><b>${esc(BRANCH[b].name)}</b><small>${scope}</small></span>${all != null ? `<em class="num">${on}<small>/${all}</small></em>` : ""}</li>`;
    const SCOPE = { R: "While she leads", B: "Her own cards", G: "Every team", Y: "Songs she sings", S: "Boosts the nodes in range" };
    const sock = (ch.sockets || []);
    const sockOn = sock.filter(s => V0.final.has(s.socket) || s.socket === "S-001").length;
    host.innerHTML = `<div class="hbd-rest"><h4>${esc(ch.name)}'s board${V.mode === "plan" ? " with the plan" : ""}</h4>
      <ul class="hbd-key">${BRANCH_ORDER.map(b => {
        const mine = (ch.nodes || []).filter(n => n.branch === b);
        return row(b, mine.filter(n => V0.final.has(n.id)).length, mine.length, SCOPE[b]);
      }).join("")}${row("S", sockOn, sock.length, SCOPE.S)}</ul>
      <p class="hbd-ro-hint">Hover a node or a Connect to read it here; click to keep it.</p></div>`;
    return;
  }
  const V2 = viewSets(ch, V.mode);
  const CV = connectView(ch, socketCardFn(ch, V.mode));
  if (id.startsWith("S-")) { host.innerHTML = socketInfo(ch, id, CV); return; }
  const n = (ch.nodes || []).find(x => x.id === id);
  if (!n) return;
  const b = BRANCH[n.branch];
  const g = (D.board.nodes || {})[id] || {};
  const lvl = g.req_level;
  const gated = lvl != null && D.board.player_level != null && lvl > D.board.player_level;
  const cost = Object.entries(n.cost || {}).filter(([k]) => k !== "points").sort((a, c) => matSort(a[0], c[0]))
    .map(([k, v]) => `<span class="mat"><img src="/itemicon/${esc(k)}" alt="" onerror="this.style.visibility='hidden'"><b class="num">${fmt(v)}</b><small>${esc(matName(k))}</small></span>`).join("");
  let state;
  if (V2.buy.has(id)) state = `<p class="st buy">${ico("plus")}The plan unlocks this node.</p>`;
  else if (V2.sell.has(id)) state = `<p class="st sell">${ico("x")}The plan resets this node: ${fmt(RESET_GOLD)} Hologold, its Pt and materials come back.</p>`;
  else if (V2.final.has(id)) state = `<p class="st on">${ico("check")}Unlocked.</p>`;
  else if (n.state === "buyable" && !gated) state = `<p class="st">Can be unlocked now.</p>`;
  else {
    const path = (n.unlock_path || []).filter(x => !V2.final.has(x));
    state = `<p class="st">Not unlocked.${path.length > 1 ? ` Reached through ${plural(path.length, "node")}, ${fmt(n.unlock_points)} Pt in all.` : ""}</p>`;
  }
  const kind = nodeKind(n);
  const note = kind === "reward" ? `<p class="dim">Rewards only: no effect on score.</p>` : kind === "life" ? `<p class="dim">LIFE only: no effect on score.</p>` : "";
  const gl = g.icon ? `<span class="gl" style="--g1:${b.g1};--g2:${b.g2}"><img src="/icon/${esc(g.icon)}.png" alt="">${g.icon_inner ? `<img class="in" src="/icon/${esc(g.icon_inner)}.png" alt="">` : ""}</span>` : "";
  const val = valueBlock(ch, n, CV);
  // the game's text with the number filled; a boosted number in the connect colour
  const MARK = String.fromCharCode(1);
  const text = esc(effectText(n.effect, ch.name, val.shown ? MARK : null))
    .replace(MARK, val.up ? `<span class="up">${esc(val.shown)}</span>` : esc(val.shown || ""));
  host.innerHTML = `<div class="hbd-ni" style="--bh:${b.ink}">
    <div class="top">${gl}<div><span class="tag">${esc(b.name)}${n.star === 2 ? " · large node" : ""}</span><h4>${esc(effectShort(n.effect, ch.name, val.shown))}</h4></div></div>
    <p class="eff">${text}</p>
    ${val.html}
    <p class="who">${esc(WHO[n.branch])}</p>${note}
    <div class="cost"><span class="mat pt"><b class="num">${fmt((n.cost || {}).points)}</b><small>Holomem Board Pt</small></span>${cost}</div>
    ${lvl ? `<p class="lvl${gated ? " bad" : ""}">${ico("lock")}Player level ${fmt(lvl)} or higher${gated ? ` · you are ${fmt(D.board.player_level)}` : ""}</p>` : ""}
    ${state}
    <p class="id">${esc(id)}</p></div>`;
}

function stage() {
  const c = D.chars.get(V.chr);
  if (!c) return;
  const ch = shownChar(V.chr);
  stageHead(c, ch);
  legend();
  const glyphs = {}, req = {}, values = {};
  for (const [id, n] of Object.entries(D.board.nodes || {})) { glyphs[id] = { icon: n.icon, inner: n.icon_inner }; req[id] = n.req_level; }
  for (const n of c.nodes || []) values[n.id] = { value: n.value ?? null, unit: n.value_unit || null };
  V.svg = drawBoard(byId("canvas"), ch, {
    mode: V.mode, glyphs, reqLevel: req, playerLevel: D.board.player_level, values,
    onPick: (id, click) => {
      if (click) { pin(V.pinned === id ? null : id, id); return; }
      if (!V.pinned) { nodeInfo(id); focusConnect(V.svg, id); }
    },
  });
  byId("canvas").onmouseleave = () => { if (!V.pinned) { nodeInfo(null); focusConnect(V.svg, null); } };
  V.pinned = null;
  nodeInfo(null);
  socketsPanel(c, ch);
}

// Keep one node or socket open (click), or let go (null; the hovered one stays shown).
function pin(id, fallback) {
  V.pinned = id;
  markSelected(V.svg, id);
  focusConnect(V.svg, id);
  nodeInfo(id || fallback || null);
}

export function show(chr, mode, scroll) {
  if (!D.chars.has(chr)) return;
  V.chr = chr;
  if (mode) setMode(mode, true);
  remember();
  roster(); stage();
  if (scroll) byId("h-boards").scrollIntoView({ behavior: "smooth", block: "start" });
}
function setMode(mode, quiet) {
  V.mode = mode === "plan" && D.plan ? "plan" : "now";
  if (!quiet) stage();
}

/* ------------------------------------------------------------------ across the account */
function idlePanel() {
  const chars = [...D.chars.values()].filter(c => (pts(c).unspent || 0) > 0)
    .sort((a, b) => pts(b).unspent - pts(a).unspent);
  const total = chars.reduce((a, c) => a + pts(c).unspent, 0);
  if (!chars.length) { byId("idle").innerHTML = `<p class="hbd-empty">Every holomem has spent all of her Pt.</p>`; return; }
  byId("idle").innerHTML = `<p class="hbd-lede"><b class="num">${fmt(total)}</b> Pt sit unspent on ${plural(chars.length, "holomem")}.
    Overall Boost to All helps every team from any board, so these points are never wasted there.</p>
    <div class="hbd-idlegrid">${chars.map(c => {
      const left = (c.nodes || []).filter(n => isGreenAll(n) && n.state !== "owned").length;
      return `<button type="button" class="hbd-idlec" data-chr="${esc(c.chr)}" title="Show ${esc(c.name)}'s board">${icon(c.chr)}
        <span><b>${esc(c.name)}</b><small>${left ? `${left} Overall Boost to All left` : "Overall Boost to All complete"}</small></span>
        <em class="num">${fmt(pts(c).unspent)}</em></button>`;
    }).join("")}</div>`;
}

// For one holomem: the nodes still needed to reach every Overall Boost to All node (union of
// the server's unlock paths) and what they cost, summed from the server's per-node costs.
function greenLeft(c) {
  const byIdN = new Map((c.nodes || []).map(n => [n.id, n]));
  const need = new Set();
  for (const n of c.nodes || []) {
    if (!isGreenAll(n) || n.state === "owned") continue;
    for (const p of n.unlock_path || [n.id]) { const m = byIdN.get(p); if (m && m.state !== "owned") need.add(p); }
  }
  const cost = { points: 0 };
  for (const id of need) for (const [k, v] of Object.entries(byIdN.get(id).cost || {})) cost[k] = (cost[k] || 0) + v;
  return { nodes: need.size, cost };
}
function greenPanel() {
  const chars = [...D.chars.values()];
  let own = 0, all = 0;
  const rows = chars.map(c => {
    const g = (c.nodes || []).filter(isGreenAll);
    const o = g.filter(n => n.state === "owned").length;
    own += o; all += g.length;
    return { c, o, n: g.length, left: greenLeft(c) };
  });
  const tot = {};
  for (const r of rows) for (const [k, v] of Object.entries(r.left.cost)) tot[k] = (tot[k] || 0) + v;
  const mats = Object.keys(tot).filter(k => k !== "points").sort(matSort).map(k => {
    const have = wallet(k), short = have != null && tot[k] > have;
    return `<span class="mat${short ? " short" : ""}"><img src="/itemicon/${esc(k)}" alt="" onerror="this.style.visibility='hidden'"><b class="num">${fmt(tot[k])}</b><small>${esc(matName(k))} · you hold ${fmt(have)}</small></span>`;
  }).join("");
  rows.sort((a, b) => b.o / (b.n || 1) - a.o / (a.n || 1) || a.c.name.localeCompare(b.c.name));
  byId("greens").innerHTML = `<p class="hbd-lede"><b class="num">${fmt(own)}</b> of ${fmt(all)} unlocked across ${plural(chars.length, "board")}.
    These help every card of every team, whoever owns the board, and they stack with no cap.</p>
    <div class="hbd-need"><small>To finish them on every board</small><div class="cost"><span class="mat pt"><b class="num">${fmt(tot.points)}</b><small>Holomem Board Pt</small></span>${mats}</div></div>
    <div class="hbd-gbars">${rows.map(r => `<button type="button" data-chr="${esc(r.c.chr)}" title="${esc(r.c.name)}: ${r.o} of ${r.n}${r.left.nodes ? ` · ${r.left.cost.points} Pt to finish` : ""}">
      <span>${esc(r.c.name)}</span><span class="bar"><i style="width:${r.n ? (r.o / r.n * 100).toFixed(1) : 0}%"></i></span><b class="num">${r.o}/${r.n}</b></button>`).join("")}</div>`;
}

function groupPanel() {
  const by = new Map();
  for (const c of D.chars.values()) {
    for (const n of c.nodes || []) {
      const grp = groupingOf(n);
      if (!grp) continue;
      const e = by.get(grp) || new Map();
      const r = e.get(c.chr) || { c, own: 0, n: 0 };
      r.n++; if (n.state === "owned") r.own++;
      e.set(c.chr, r); by.set(grp, e);
    }
  }
  const groups = [...by.entries()].map(([g, m]) => ({ g, rows: [...m.values()], own: [...m.values()].reduce((a, r) => a + r.own, 0) }))
    .sort((a, b) => b.own - a.own || a.g.localeCompare(b.g));
  byId("groupings").innerHTML = `<p class="hbd-lede">Each holomem's board has three nodes that raise All Stats for her own group. The game caps the total a card can receive from them at <b class="num">900</b>, so after two or three full chains in one group more add nothing.</p>
    <div class="hbd-grps">${groups.map(x => {
      const gen = (Object.values((window.HB_GENS || {}).by || {}).flat().find(g => g.n === x.g)) || {};
      return `<div class="hbd-grp" style="--gc:${gen.c || "var(--ink-3)"};--gc2:${gen.c2 || "var(--blk)"}"><header><i></i><b>${esc(x.g)}</b><small>${plural(x.own, "node")} unlocked</small></header>
        <div>${x.rows.sort((a, b) => b.own - a.own).map(r => `<button type="button" data-chr="${esc(r.c.chr)}" class="pip${r.own ? "" : " none"}" title="${esc(r.c.name)}: ${r.own} of ${r.n}">${icon(r.c.chr)}<span>${"●".repeat(r.own)}${"○".repeat(Math.max(0, r.n - r.own))}</span></button>`).join("")}</div></div>`;
    }).join("")}</div>`;
}

function yellowPanel() {
  const rows = [...D.chars.values()].map(c => {
    const k = { solo: [0, 0], group: [0, 0], any: [0, 0] };
    for (const n of c.nodes || []) { const t = songBonusType(n); if (t) { k[t][1]++; if (n.state === "owned") k[t][0]++; } }
    return { c, k, own: k.solo[0] + k.group[0] + k.any[0] };
  }).filter(r => r.own).sort((a, b) => b.own - a.own);
  byId("yellows").innerHTML = `<p class="hbd-lede">A singer's own board raises the score of the songs she sings, in any team, whether she is in it or not. The game caps it at <b class="num">+10%</b> per song, summed over the song's singers.</p>
    ${rows.length ? `<div class="hbd-ylist">${rows.map(r => `<button type="button" data-chr="${esc(r.c.chr)}">${icon(r.c.chr)}<b>${esc(r.c.name)}</b>
      <span>${["solo", "group", "any"].filter(t => r.k[t][1]).map(t => `<em title="${t === "any" ? "Songs by the whole cast" : t === "solo" ? "Solo songs" : "Unit songs"}">${t === "any" ? "All songs" : t === "solo" ? "Solo" : "Unit"} <b class="num">${r.k[t][0]}/${r.k[t][1]}</b></em>`).join("")}</span></button>`).join("")}</div>`
    : `<p class="hbd-empty">No holomem has a per-song score bonus node yet.</p>`}`;
}

/* ------------------------------------------------------------------ boot */
async function load() {
  try {
    const [board, res, coll, songs] = await Promise.all([
      getJSON("/api/board"), getJSON("/api/resources").catch(() => null),
      getJSON("/api/collection").catch(() => null), getJSON("/api/songs").catch(() => null),
    ]);
    if (!board.available) throw new Error(board.reason || "The board data is not on disk.");
    D.board = board; D.res = res && res.available !== false ? res : null; D.coll = coll;
    D.songs = (songs && songs.songs) || [];
    const state = {};
    for (const [chr, c] of Object.entries(board.characters || {})) state[chr] = { nodes: c.nodes || [], slots: c.slots || {} };
    D.doc = await postJSON("/api/board-doc", { board_state: state, cards: [], basis: "account" });
    for (const c of D.doc.characters || []) D.chars.set(c.chr, c);
  } catch (e) {
    byId("chips").innerHTML = "";
    byId("body").innerHTML = `<div class="error" style="margin-top:32px"><b>The boards could not be loaded.</b> ${esc(e.message)}</div>`;
    return;
  }
  hero();
  if (!D.chars.has(V.chr)) {
    const first = [...D.chars.values()].sort((a, b) => (pts(b).spent_on_owned || 0) - (pts(a).spent_on_owned || 0))[0];
    V.chr = first && first.chr;
  }
  const q = new URLSearchParams(location.search).get("chr");
  if (q && D.chars.has(q)) V.chr = q;
  byId("sort").querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.v === V.sort)));
  if (V.sort === "plan") V.sort = "unspent";
  roster(); stage();
  idlePanel(); greenPanel(); groupPanel(); yellowPanel();
}

byId("roster").addEventListener("click", e => { const b = e.target.closest("[data-chr]"); if (b) show(b.dataset.chr); });
byId("find").addEventListener("input", e => { V.find = e.target.value; roster(); });
byId("sort").addEventListener("click", e => {
  const b = e.target.closest("button[data-v]"); if (!b || b.disabled) return;
  V.sort = b.dataset.v; remember();
  byId("sort").querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
  roster();
});
document.querySelector(".hbd-acct").addEventListener("click", e => {
  const b = e.target.closest("[data-chr]"); if (b) show(b.dataset.chr, "now", true);
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && V.pinned) pin(null);
});
// the Connect strip and the range list drive the board too
byId("sockets").addEventListener("mouseover", e => {
  const t = e.target.closest("[data-socket]");
  if (!t || V.pinned) return;
  nodeInfo(t.dataset.socket); focusConnect(V.svg, t.dataset.socket);
});
byId("sockets").addEventListener("mouseleave", () => { if (!V.pinned) { nodeInfo(null); focusConnect(V.svg, null); } });
byId("sockets").addEventListener("click", e => {
  const t = e.target.closest("[data-socket]"); if (!t) return;
  pin(V.pinned === t.dataset.socket ? null : t.dataset.socket); reveal(V.svg, t.dataset.socket);
});
byId("nodeinfo").addEventListener("click", e => {
  const t = e.target.closest("[data-goto]"); if (!t) return;
  pin(t.dataset.goto); reveal(V.svg, t.dataset.goto);
});
// zoom: wheel / pinch / drag / double-click / keys live in hbd-draw.js; the bar and full screen here
initPanZoom(byId("canvas"), z => { byId("zoomlvl").textContent = `${Math.round(z * 100)}%`; });
document.querySelector(".hbd-zoombar").addEventListener("click", e => {
  const b = e.target.closest("[data-zoom]"); if (!b) return;
  const k = b.dataset.zoom;
  if (k === "in") zoomBy(1.3);
  else if (k === "out") zoomBy(1 / 1.3);
  else if (k === "fit") fit();
  else if (k === "full") toggleFull();
});
function toggleFull() {
  const vp = byId("viewport");
  if (document.fullscreenElement) document.exitFullscreen();
  else if (vp.requestFullscreen) vp.requestFullscreen().then(() => byId("canvas").focus()).catch(() => toast("Full screen is not available here."));
}
byId("canvas").addEventListener("keydown", e => { if ((e.key === "f" || e.key === "F") && !e.ctrlKey && !e.metaKey) toggleFull(); });
document.querySelectorAll("dialog").forEach(d => d.addEventListener("click", e => {
  if (e.target === d || e.target.closest("[data-close]")) d.close();
}));
window.addEventListener("error", e => toast(`Page error: ${e.message}`));
load();
