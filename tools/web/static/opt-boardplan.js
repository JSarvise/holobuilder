// Optimizer: the Holomem Board plan of a team, drawn the way the community tool holodori.best
// shows it -- EVERY
// holomem whose board the plan changes (the team first, then the bench: green "to All" nodes
// lift every team), each as its TARGET board with drag / zoom (the Holomem Board page's own
// drawing, hbd-draw.js), the nodes to unlock and to reset marked.
//
// Data: the board-export doc of the plan -- POST /api/board-plan-account (the whole account,
// the exact plan under the search's board levers) or, without it, the team's six boards
// (/api/board-doc). Every node figure is the server's; this file only draws.
import { esc, fmt } from "/opt-util.js";
import { drawBoard, initPanZoom, zoomBy, fit, markSelected } from "/hbd-draw.js";
import { D as HBD, BRANCH, effectText } from "/hbd-util.js";
import { connectStripHTML, connectInfoHTML } from "/hbd-connect.js";

const picked = new Map();     // doc key -> the holomem shown
const SOCKS = new WeakMap();  // doc -> {now, then}: card id -> "Holomem S-00x", where it is and where the plan puts it
let glyphsP = null;           // /api/board node glyphs (icon + inner frame), once per page
const glyphs = () => (glyphsP = glyphsP || fetch("/api/board").then(r => r.json())
  .then(b => Object.fromEntries(Object.entries(b.nodes || {}).map(([id, n]) => [id, { icon: n.icon, inner: n.icon_inner }])))
  .catch(() => ({})));

// a board material by its game name: family (leader / card / allmember / content = the branch
// colour) and tier (1 Cube, 2 Hexa), as the Required Materials list names them
const MAT_FAM = { leader: "Red", card: "Blue", allmember: "Green", content: "Yellow" };
const matName = id => {
  const m = /^item-skill_tree_([a-z]+)-(\d)$/.exec(id || "");
  return m ? `${MAT_FAM[m[1]] || m[1]} ${m[2] === "1" ? "Cube" : m[2] === "2" ? "Hexa" : "tier " + m[2]}` : id;
};
// nodes to unlock / reset, and sockets whose card the plan changes (the server's `change`)
const counts = c => ({ buy: ((c.plan || {}).to_buy || []).length, sell: ((c.plan || {}).to_sell || []).length,
  sock: (c.sockets || []).filter(s => s.change).length });
const moved = k => k.buy + k.sell + k.sock;
const CHANGE = { add: "A card goes in", swap: "Another card goes in", remove: "The card comes out" };
const LEAVE_MS = 150;         // how long the side panel keeps a node/socket after the pointer leaves

/** The viewer's frame (filled by wireBoardPlan once in the DOM). */
export function boardPlanHTML(key, doc, loading) {
  if (!doc) return `<div class="o-bp o-bp-empty"><span class="o-sk" style="width:100%;height:${loading ? 420 : 60}px"></span></div>`;
  return `<div class="o-bp hbd" data-bp="${esc(key)}">
    <div class="o-bp-strip" role="tablist" aria-label="Holomems"></div>
    <div class="o-bp-body">
      <div class="hbd-viewport o-bp-view">
        <div class="hbd-canvas o-bp-canvas" tabindex="0" aria-label="Target board: scroll to zoom, drag to move, double-click to fit"></div>
        <div class="hbd-zoombar" role="toolbar" aria-label="Zoom">
          <button type="button" data-bpzoom="in" aria-label="Zoom in">+</button>
          <button type="button" data-bpzoom="out" aria-label="Zoom out">−</button>
          <button type="button" data-bpzoom="fit" aria-label="Fit"><span class="w">Fit</span></button>
        </div>
        <p class="hbd-howto">Drag to move · scroll to zoom</p>
      </div>
      <div class="hbd-sockets o-bp-socks"></div>
      <aside class="o-bp-side">
        <div class="o-bp-head"></div>
        <div class="o-bp-node"></div>
        <div class="o-bp-legend">
          <span><i class="lg on"></i>On the target board</span>
          <span><i class="lg buy"></i>To unlock</span>
          <span><i class="lg sell"></i>To reset</span>
          <span><i class="lg sock"></i>Socket: a card goes in</span>
          <span><i class="lg sockout"></i>Socket: the card comes out</span>
          <span><i class="lg con"></i>Boosted by a Connect card</span>
          <span><i class="lg both"></i>To unlock, boosted (both rings)</span>
        </div>
      </aside>
    </div>
  </div>`;
}

/** Fill the viewer inside `root` from `doc` (`team` = the six cards' characters, shown first). */
export async function wireBoardPlan(root, key, doc, team) {
  const el = root && root.querySelector(`.o-bp[data-bp="${CSS.escape(key)}"]`);
  if (!el || !doc || !doc.characters) return;
  const teamSet = new Set(team || doc.team_chrs || []);
  const byChr = new Map(doc.characters.map(c => [c.chr, c]));
  if (!SOCKS.has(doc)) {
    const now = {}, then = {};
    for (const c of doc.characters) for (const s of c.sockets || []) {
      if (s.placed_card) now[s.placed_card] = `${c.name} ${s.socket}`;
      if (s.recommended_card) then[s.recommended_card] = `${c.name} ${s.socket}`;
    }
    SOCKS.set(doc, { now, then });
  }
  const changed = doc.characters.filter(c => moved(counts(c)) > 0);
  const order = [
    ...[...teamSet].map(chr => byChr.get(chr)).filter(Boolean),
    ...changed.filter(c => !teamSet.has(c.chr)).sort((a, b) => {
      const ka = counts(a), kb = counts(b);
      return moved(kb) - moved(ka) || a.name.localeCompare(b.name);
    }),
  ];
  if (!order.length) { el.innerHTML = `<p class="o-cap">The plan changes no Holomem Board.</p>`; return; }
  let cur = picked.get(key);
  if (!order.some(c => c.chr === cur)) cur = order[0].chr;
  const strip = el.querySelector(".o-bp-strip");
  const benchN = order.length - [...teamSet].filter(c => byChr.has(c)).length;
  strip.innerHTML = order.map((c, k) => {
    const n = counts(c);
    const sep = k === [...teamSet].filter(x => byChr.has(x)).length && benchN ? `<span class="o-bp-sep">Bench · ${benchN}</span>` : "";
    return `${sep}<button type="button" class="o-bp-chip${teamSet.has(c.chr) ? " team" : ""}" role="tab" data-bpchr="${esc(c.chr)}" aria-selected="${c.chr === cur}">
      <img src="/chricon/${esc(c.chr)}" alt="" onerror="this.style.visibility='hidden'"><b>${esc(c.name)}</b>
      <span class="n">${n.buy ? `<em class="b">+${n.buy}</em>` : ""}${n.sell ? `<em class="s">−${n.sell}</em>` : ""}${n.sock ? `<em class="k" title="${n.sock} socket${n.sock === 1 ? "" : "s"} change${n.sock === 1 ? "s" : ""} card">⇄${n.sock}</em>` : ""}${!moved(n) ? `<em>no change</em>` : ""}</span></button>`;
  }).join("");
  const g = await glyphs();
  const canvas = el.querySelector(".o-bp-canvas");
  initPanZoom(canvas, () => {});
  const show = chr => {
    cur = chr; picked.set(key, chr);
    strip.querySelectorAll("[data-bpchr]").forEach(b => b.setAttribute("aria-selected", String(b.dataset.bpchr === chr)));
    const ch = byChr.get(chr);
    HBD.doc = doc;                                    // the Connect catalogue hbd-util reads
    const values = {};
    for (const n of ch.nodes || []) values[n.id] = { value: n.value ?? null, unit: n.value_unit || null };
    // the side panel redraws only when the pointer reaches ANOTHER node or socket (mouseover
    // fires on every child it crosses), and clears a moment after it leaves, so crossing the
    // gap between two sockets does not flash the empty text (no flicker when hovering fast)
    let showing = null, clear = 0;
    const hover = id => {
      clearTimeout(clear);
      if (id === showing) return;
      showing = id;
      nodeInfo(el, ch, id, SOCKS.get(doc));
    };
    const leave = () => { clearTimeout(clear); clear = setTimeout(() => hover(null), LEAVE_MS); };
    const svg = drawBoard(canvas, ch, {
      mode: "plan", glyphs: g, values,
      onPick: (id, click) => { hover(id); if (click) markSelected(svg, id); },
    });
    canvas.onmouseleave = leave;
    const socks = el.querySelector(".o-bp-socks");
    socks.innerHTML = connectStripHTML(ch, "plan");
    socks.onmouseover = e => { const t = e.target.closest("[data-socket]"); if (t) hover(t.dataset.socket); };
    socks.onmouseleave = leave;
    head(el, ch, teamSet.has(chr));
    nodeInfo(el, ch, null);
  };
  strip.onclick = e => { const b = e.target.closest("[data-bpchr]"); if (b) show(b.dataset.bpchr); };
  el.querySelector(".hbd-zoombar").onclick = e => {
    const b = e.target.closest("[data-bpzoom]"); if (!b) return;
    ({ in: () => zoomBy(1.3), out: () => zoomBy(1 / 1.3), fit: () => fit() })[b.dataset.bpzoom]();
  };
  show(cur);
}

function head(el, ch, onTeam) {
  const p = ch.plan || {}, n = counts(ch);
  const pts = p.point_cost != null ? `${fmt(p.point_cost)} / ${p.point_budget != null ? fmt(p.point_budget) : "—"} Pt` : "";
  el.querySelector(".o-bp-head").innerHTML = `<div class="who"><img src="/chricon/${esc(ch.chr)}" alt="" onerror="this.style.visibility='hidden'">
      <div><b>${esc(ch.name)}</b><small>${onTeam ? "In the team" : "Bench · its green lifts every team"}</small></div></div>
    <dl><div><dt>Unlocked now</dt><dd class="num">${fmt(ch.owned_count)}</dd></div>
      <div><dt>To unlock</dt><dd class="num b">${fmt(n.buy)}</dd></div>
      <div><dt>To reset</dt><dd class="num s">${fmt(n.sell)}</dd></div>
      <div><dt>Sockets to change</dt><dd class="num k">${fmt(n.sock)}</dd></div>
      ${pts ? `<div><dt>Holomem Board Pt</dt><dd class="num">${pts}</dd></div>` : ""}</dl>`;
}

function cardLine(name, member) {
  return name ? `${esc(name)}${member ? ` <small>· ${esc(member)}</small>` : ""}` : `<i>empty</i>`;
}

function socketInfo(box, ch, s, at) {
  const p = ch.plan || {};
  const open = (p.optimised_nodes || []).includes(s.socket) || s.socket === "S-001" || s.placed_card;
  const now = cardLine(s.placed_card_name || s.placed_card, s.placed_card_member);
  const then = cardLine(s.recommended_card_name || s.recommended_card, s.recommended_card_member);
  // where the cards come from and go to: a card the plan takes out is always used elsewhere
  // (one the plan uses nowhere stays where it is), so say where
  const notes = [];
  if (s.change && s.placed_card) {
    const to = (at || {}).then?.[s.placed_card];
    notes.push(`${esc(s.placed_card_name || s.placed_card)} moves to ${to ? esc(to) : "another socket"}.`);
  }
  if (s.change && s.recommended_card) {
    const from = (at || {}).now?.[s.recommended_card];
    notes.push(`${esc(s.recommended_card_name || s.recommended_card)} comes ${from ? `from ${esc(from)}` : "from your collection (not socketed now)"}.`);
  }
  box.innerHTML = `<p class="br">Socket ${esc(s.socket)}</p>
    <dl class="o-bp-sock"><div><dt>Now</dt><dd>${now}</dd></div><div><dt>Plan</dt><dd>${then}</dd></div></dl>
    <p class="st ${s.change === "remove" ? "s" : s.change ? "b" : ""}">${s.change ? CHANGE[s.change] : open ? "Unchanged" : "Locked"}</p>
    ${notes.length ? `<p class="o-cap">${notes.join(" ")}</p>` : ""}
    ${connectInfoHTML(ch, s.socket, "plan")}`;
}

function nodeInfo(el, ch, id, at) {
  const box = el.querySelector(".o-bp-node");
  const sock = id && (ch.sockets || []).find(x => x.socket === id);
  if (sock) { socketInfo(box, ch, sock, at); return; }
  const n = id && (ch.nodes || []).find(x => x.id === id);
  if (!n) {
    box.innerHTML = `<p class="o-cap">Point at a node to read it. Click a holomem above to see her board.</p>`;
    return;
  }
  const p = ch.plan || {};
  const st = (p.to_buy || []).includes(id) ? ["b", "To unlock"] : (p.to_sell || []).includes(id) ? ["s", "To reset"]
    : (p.optimised_nodes || []).includes(id) ? ["", "Kept"] : ["", "Not in the plan"];
  const b = BRANCH[n.branch] || {};
  const cost = Object.entries(n.cost || {}).filter(([k]) => k !== "points").map(([k, v]) => `${fmt(v)} ${esc(matName(k))}`).join(" · ");
  box.innerHTML = `<p class="br" style="color:${b.hue || "inherit"}">${esc(b.name || n.branch)}</p>
    <p class="fx">${esc(effectText(n.effect, ch.name, n.boosted_text || n.value_text))}</p>
    <p class="st ${st[0]}">${st[1]}</p>
    <p class="o-cap">${esc(n.id)}${(n.cost || {}).points != null ? ` · ${fmt(n.cost.points)} Pt` : ""}${cost ? ` · ${cost}` : ""}</p>`;
}
