// Compare page: the per-team sections (lineup, Unit Score Details, run, timeline,
// Holomem Board plan) and the cross-team tables (at a glance, conditions, skills).
// Adapted from opt-detail.js. Every figure is a /api/compare (or /api/timeline,
// /api/board-doc, /api/resources) field; the only arithmetic here is layout and the
// ratio of two served numbers for a delta.
import { esc, fmt, dec, sgn, sgnN, info, songGrades, INK, thumb, place, byId, outfitText } from "/vs-util.js";
import { badge as rankBadge, score, rankOf, unitRows } from "/hb-grade.js";
import { render as renderTimeline } from "/hb-timeline.js";
// the game grades the Unit Score (IDENTITY.md §5); 72 px when one team fills the view, 48 px per column side by side
const unitBadge = (v, size) => { const g = rankOf(v, unitRows()); return g ? rankBadge(g, { size }) : ""; };

const GEN_EN = { "0期生": "Gen 0", "1期生": "Gen 1", "2期生": "Gen 2", "3期生": "Gen 3", "4期生": "Gen 4", "5期生": "Gen 5", "ゲーマーズ": "GAMERS", ID1: "ID Gen 1", ID2: "ID Gen 2", ID3: "ID Gen 3" };
export const genName = v => GEN_EN[v] || v;
// the deck screen's Unit Score: the Score Bonus split's own, else the panel's
export const deckUnit = row => row.score_bonus && row.score_bonus.unit_score != null ? row.score_bonus.unit_score : Math.ceil(row.panel.unit_score);
export const headline = (row, mode) => mode === "song" ? row.score.song_est : mode === "across" ? row.score.across_mean : deckUnit(row);
export const headLabel = mode => mode === "song" ? "Expected Score" : mode === "across" ? "Average score" : "Unit Score";
const pctVs = (v, ref) => (v == null || !ref) ? null : (v / ref - 1) * 100;
export function deltaPill(v, ref, digits = 2) {
  const p = pctVs(v, ref); if (p == null) return "";
  const cls = Math.abs(p) < 0.005 ? "eq" : p > 0 ? "up" : "down";
  return `<span class="v-dpill ${cls}">${Math.abs(p) < 0.005 ? "±0%" : sgn(p, digits)}</span>`;
}

/* ------------------------------------------------------------ the lineup: 6 equal cards */
export function slotOrder(row) {
  return [row.leader_id, ...(row.score.member_order || row.member_ids)];
}
export function lineup(t, ctx) {
  const ids = slotOrder(t.row), ordered = !!t.row.score.member_order;
  const ov = new Map((t.row.slots || []).filter(s => s.override).map(s => [s.card_id, s]));
  return `<div class="v-stage">${ids.map((id, k) => {
    const c = ctx.cardOf(id) || { id, member: id, rarity: 3 };
    const o = ov.get(id);
    return `<figure class="v-perf" style="--i:${k}">
      ${window.HBCard.face(c, { lead: k === 0, roleText: "Leader", w: ctx.side ? 240 : 400 })}
      <figcaption>
        <span class="slot">${k && ordered ? k : ""}</span>
        <b>${esc(c.costume_name || c.title || c.id)}</b>
        <span class="mb">${esc(c.member || "")}</span>
        <span class="xb">${c.level != null ? `Lv ${c.level}` : ""}${c.bloom ? ` · Bloom ${c.bloom}` : ""}${o ? ` <em class="v-ov">set by hand</em>` : ""}</span>
      </figcaption>
    </figure>`;
  }).join("")}</div>${ordered ? "" : `<p class="v-ordernote">Slot order not computed in this mode.</p>`}`;
}
export function outfitLine(t, ctx) {
  const L = ctx.cardOf(t.row.leader_id) || {};
  if (!L.costume_skill && !L.costume_name) return "";
  const gate = (t.row.gates || []).find(g => g.card_id === t.row.leader_id && g.slot_kind === "costume");
  return `<div class="v-outfit"><svg class="i s"><use href="#i-shirt"/></svg>
    <span>Leader’s Outfit Skill · <b>${esc(L.costume_name || "")}</b></span>${L.costume_skill || L.costume_text ? `<span aria-hidden="true">·</span><span class="fx">${esc(outfitText(L.costume_skill, L.costume_text))}</span>` : ""}
    ${gate ? `<span class="v-gate ${gate.fired ? "met" : "miss"}">${gate.fired ? "Condition met" : "Condition not met"} (${gate.actual_count}/${gate.threshold})</span>` : ""}</div>`;
}

/* ------------------------------------------------------------ Unit Score Details */
const POWER = [["member_parameter", "Member Parameter", "--t-member"], ["outfit", "Outfit Skill", "--t-leader"], ["board", "Holomem Board Bonus", "--t-board"],
  ["passive", "Passive Skill", "--t-passive"], ["memory", "Memory Bonus", "--t-memory"], ["power_up", "Member Power-Up Bonus", "--t-powerup"]];
export const POWER_LINES = POWER;

const BONUS_INK = { active: "--t-active", outfit: "--t-leader", passive: "--t-passive", board: "--t-board", special: "--t-special" };
function bonusTerm(row) {
  const SB = row.score_bonus, P = row.panel;
  const pooled = P && P.score_bonus_permil != null
    ? `<p class="v-cap v-pooled">Ranking model's pooled Score Bonus <span class="num">+${dec(P.score_bonus_permil / 10, 1)}%</span>${info("model_bonus")}</p>` : "";
  if (!SB || !SB.lines) return `<div class="v-term"><h4>Score Bonus</h4><p class="v-cap" style="margin-top:0"><span class="v-preview">Split not served</span></p>${pooled}</div>`;
  const tot = SB.lines.reduce((a, l) => a + Math.max(0, l.permil || 0), 0) || 1;
  return `<div class="v-term">
    <h4>Score Bonus <span class="num">+${dec(SB.score_bonus_permil / 10, 1)}%</span>${info("bonus")}</h4>
    <div class="v-sbar" role="img" aria-label="Score Bonus split">${SB.lines.map(l =>
      `<i style="--seg:var(${BONUS_INK[l.key] || "--ink-3"});width:${(Math.max(0, l.permil) / tot * 100).toFixed(2)}%" data-tip="${esc(l.label)}|+${dec(l.permil / 10, 1)}%"></i>`).join("")}</div>
    <ul class="v-legend">${SB.lines.map(l =>
      `<li style="--seg:var(${BONUS_INK[l.key] || "--ink-3"})"><i></i><span>${esc(l.label)}</span><span class="num">+${dec(l.permil / 10, 1)}%</span></li>`).join("")}</ul>
    ${(SB.unmodelled || []).length ? `<p class="v-cap">Not modelled: ${SB.unmodelled.map(esc).join(", ")}</p>` : ""}
    ${pooled}
  </div>`;
}
export function unitPanel(t, ctx) {
  const P = t.row.panel, us = deckUnit(t.row);
  const total = POWER.reduce((a, [k]) => a + (P[k] || 0), 0) || 1;
  const off = t.row.score.unit_score_board_off != null ? Math.ceil(t.row.score.unit_score_board_off) : null;
  const ref = ctx.ref && ctx.ref !== t ? deckUnit(ctx.ref.row) : null;
  return `<div class="v-card v-reveal">
    <div class="v-sh"><h3>Unit Score Details</h3>${info("unit")}${ref != null ? `<span class="right">vs A ${deltaPill(us, ref)}</span>` : ""}</div>
    <div class="v-ushead">
      ${unitBadge(us, ctx.side || ctx.n > 2 ? 48 : 72)}
      <div><p class="lab">Unit Score ${info("grade")}</p><p class="v-padnum" aria-label="${fmt(us)}">${score(us)}</p></div>
      ${off != null && ctx.board !== "off" ? `<div class="was">No Holomem Board<span class="num">${fmt(off)}</span></div>` : ""}
    </div>
    <div class="v-uscols">
      <div class="v-term">
        <h4>Overall Power <span class="num">${fmt(P.overall_power)}</span></h4>
        <div class="v-sbar" role="img" aria-label="Overall Power split">${POWER.map(([k, lab, c]) =>
          `<i style="--seg:var(${c});width:${((P[k] || 0) / total * 100).toFixed(2)}%" data-tip="${esc(lab)}|${fmt(P[k])}|${dec((P[k] || 0) / total * 100, 1)}% of Overall Power"></i>`).join("")}</div>
        <ul class="v-legend">${POWER.map(([k, lab, c]) =>
          `<li style="--seg:var(${c})"><i></i><span>${lab}</span><span class="num">${fmt(P[k])}</span></li>`).join("")}</ul>
      </div>
      ${bonusTerm(t.row)}
    </div>
    ${t.row.score.deck_power != null ? `<p class="v-cap">Scored on deck power <span class="num">${fmt(t.row.score.deck_power)}</span>${t.row.score.deck_power_basis === "server" ? " (the deck screen's Overall Power)" : ""}</p>` : ""}
  </div>`;
}

/* ------------------------------------------------------------ the run panel */
function lawChart(law, marks) {
  const pm = law.pmf; if (!pm || !pm.mass || !pm.mass.length) return "";
  const n = pm.mass.length, lo = pm.start, hi = pm.start + n * pm.width, peak = Math.max(...pm.mass) || 1;
  const X = v => (v - lo) / (hi - lo) * 100;
  const bars = pm.mass.map((m, k) => `<i style="left:${(k / n * 100).toFixed(3)}%;width:${(100 / n).toFixed(3)}%;height:${Math.max(1, m / peak * 100).toFixed(1)}%" data-tip="${fmt(lo + k * pm.width)} – ${fmt(lo + (k + 1) * pm.width)}|${dec(m * 100, 2)}%|of runs"></i>`).join("");
  const mk = marks.filter(([, v]) => v != null && v >= lo && v <= hi).map(([lab, v, cls]) =>
    `<span class="mk ${cls}" style="left:${X(v).toFixed(2)}%" data-tip="${lab}|${fmt(v)}"><span>${lab}</span></span>`).join("");
  return `<div class="v-law" role="img" aria-label="Run score distribution, ${fmt(law.p1)} to ${fmt(law.p99)} from the 1st to the 99th percentile">
      <div class="bars">${bars}${mk}</div>
      <div class="ax"><span class="num">${fmt(lo)}</span>${pm.below ? `<span>${dec(pm.below * 100, 1)}% below</span>` : ""}<span class="num">${fmt(hi)}</span></div>
    </div>`;
}
export function runPanel(t, ctx) {
  const s = t.row.score, mode = ctx.mode;
  if (mode === "unit") {
    return `<div class="v-card v-reveal"><div class="v-sh"><h3>Run</h3>${info("unitscore")}</div>
      <p class="v-cap" style="margin-top:0">No song chosen: the teams are compared by Unit Score. Pick a song to see expected and max scores, and the timeline.</p></div>`;
  }
  const exp = headline(t.row, mode);
  const pts = mode === "song"
    ? [["Expected", exp, "e"], ["Max", s.song_max, ""], ["No board", ctx.board === "off" ? null : s.song_off, "n"]].filter(p => p[1] != null)
    : [["Average", exp, "e"]];
  const wi = t.row.develop_whatif;
  const wiv = wi ? (wi.song_est ?? wi.across_mean) : null;
  const wins = mode === "across" && t.diff ? t.row.score.wins : null;
  const refWins = mode === "across" && t.diff && t.refRow ? t.refRow.score.wins : null;
  const law = t.row.distribution && t.row.distribution.available !== false ? t.row.distribution : null;
  const lawState = law ? (law.verified ? `<span class="v-tag ok" title="The law's mean reproduces the Expected Score exactly">Verified</span>`
    : law.verified === false ? `<span class="v-tag warn">Not verified</span>` : "") : "";
  let body;
  if (law) {
    const q = [["1%", law.p1], ["5%", law.p5], ["Median", law.p50], ["95%", law.p95], ["99%", law.p99]];
    body = `${lawChart(law, [["Expected", exp, "e"]])}
      <div class="v-stats v-qs">${q.map(([lab, v]) => `<div title="${lab === "Median" ? "Half the runs score less" : `${lab} of runs score less`}"><span>${lab}</span><b>${score(v)}</b></div>`).join("")}</div>
      <div class="v-stats v-s${pts.length + 1}">${pts.map(([lab, v]) => `<div><span>${lab}</span><b>${score(v, 7)}</b></div>`).join("")}<div title="Standard deviation of the run score"><span>Spread (sd)</span><b>±${fmt(law.sd)}</b></div></div>`;
  } else {
    const lo = Math.min(...pts.map(p => p[1])), hi = Math.max(...pts.map(p => p[1]));
    const span = hi - lo || 1, X = v => 4 + (v - lo) / span * 92;
    body = `${pts.length > 1 ? `<div class="v-range" role="img" aria-label="Expected, max and no board on one axis">
      <span class="ln"></span>
      ${s.song_max != null ? `<span class="span" style="left:${X(exp)}%;width:${X(s.song_max) - X(exp)}%"></span>` : ""}
      ${pts.map(([lab, v, cls]) => `<span class="mk ${cls}" style="left:${X(v)}%" data-tip="${lab}|${fmt(v)}"><span>${lab}</span><i></i></span>`).join("")}
    </div>` : ""}
    <div class="v-stats">${pts.map(([lab, v]) => `<div><span>${lab}</span><b>${score(v)}</b></div>`).join("")}</div>`;
  }
  return `<div class="v-card v-reveal">
    <div class="v-sh"><h3>Score distribution</h3>${info("run")}<span class="right">${lawState}</span></div>
    ${body}
    ${mode === "song" && s.song_max != null ? `<p class="v-cap">Max is <span class="num">${sgn((s.song_max / exp - 1) * 100)}</span> over the expected score${ctx.board === "off" ? "" : `; the Holomem Boards add <span class="num">${sgn((exp / s.song_off - 1) * 100)}</span>`}.</p>` : ""}
    ${wins != null ? `<p class="v-cap">Beats team A on <span class="num">${wins}</span> of ${ctx.chartCount} charts, loses on <span class="num">${refWins}</span>${t.row.score.ties ? `, ties ${t.row.score.ties}` : ""}.</p>` : ""}
    ${wiv != null && Math.round(wiv) !== Math.round(exp) ? `<p class="v-cap">Re-planned for this team alone: <span class="num">${fmt(wiv)}</span> (${sgn(wi.delta_pct, 2)}) ${info("shared")}</p>` : ""}
    ${matchLine(t)}
  </div>`;
}
// with As ranked: the Optimizer's leaf figure (/api/row-detail) next to /api/compare's on that board
export function matchLine(t) {
  const leaf = t.leaf; if (!leaf || leaf.song_est == null || !t.row.score || t.row.score.song_est == null) return "";
  const v = t.row.score.song_est;
  if (Math.round(leaf.song_est) === Math.round(v)) return `<p class="v-match ok"><svg class="i s"><use href="#i-check"/></svg><span>One number: the Optimizer and this page both give <span class="num">${fmt(v)}</span> on this board.</span>${info("match")}</p>`;
  return `<p class="v-match off"><svg class="i s"><use href="#i-alert"/></svg><span>The Optimizer gives <span class="num">${fmt(leaf.song_est)}</span>; this page on the same board gives <span class="num">${fmt(v)}</span> (<b>${sgn((v / leaf.song_est - 1) * 100, 2)}</b>).</span>${info("match")}</p>`;
}
// with As ranked a team's plan is read off its board doc: the doc's to_buy / to_sell / point cost
// and budget per holomem; materials = the sum of the server's node costs over the nodes to unlock
export function planFromDoc(doc) {
  if (!doc || !doc.characters) return null;
  const materials = {};
  const per_character = doc.characters.filter(c => c.plan).map(c => {
    const nodes = new Map((c.nodes || []).map(n => [n.id, n]));
    for (const id of c.plan.to_buy || []) {
      const cost = (nodes.get(id) || {}).cost || {};
      for (const [k, v] of Object.entries(cost)) if (k !== "points") materials[k] = (materials[k] || 0) + v;
    }
    return { chr: c.chr, name: c.name, buy: c.plan.to_buy || [], sell: c.plan.to_sell || [], current_owned: c.owned_count,
      points_used: c.plan.point_cost, points_max: c.plan.point_budget,
      sockets: Object.entries(c.plan.slots || {}).map(([slot, card_id]) => ({ slot, card_id })) };
  });
  return { per_character, materials };
}

/* ------------------------------------------------------------ timeline */
export function covPeak(t) {
  if (!t.tl) return 0;
  const TL = t.tl.timeline; let peak = 0;
  for (let x = 0; x <= TL.song_seconds; x += .2)
    peak = Math.max(peak, TL.lanes.reduce((a, ln) => a + ln.active.reduce((b, w) => b + (x >= w.start && x < w.end ? w.probability_base : 0), 0), 0));
  return peak;
}
export function timelinePanel(t, ctx) {
  if (ctx.mode !== "song") return "";
  if (!t.tl) return t.tlErr
    ? `<div class="v-sect v-pending"><span class="v-preview">Unavailable</span>Timeline: ${esc(t.tlErr)}</div>`
    : `<div class="v-sect v-pending"><span class="v-sk" style="width:100%;height:120px"></span></div>`;
  // drawn by the shared component (hb-timeline.js) once in the DOM: wireTimeline
  return `<div class="v-sect v-reveal">
    <div class="v-sh"><h3>Skill uptime</h3>${info("timeline")}</div>
    <div class="v-hbtl" data-tl="${t.idx}"></div>
  </div>`;
}
export function wireTimeline(root, t, ctx) {
  const el = root.querySelector(`.v-hbtl[data-tl="${t.idx}"]`); if (!el || !t.tl) return;
  const nm = id => { const c = ctx.cardOf(id); return c ? `${c.member || ""}${c.costume_name ? " · " + c.costume_name : ""}` : id; };
  const L = ctx.cardOf(t.row.leader_id);
  renderTimeline(el, t.tl.timeline, {
    name: nm,
    leader: L ? { name: L.member || L.id, text: L.costume_name ? `Leader · ${L.costume_name}` : "Leader" } : undefined,
  });
}

/* ------------------------------------------------------------ Holomem Board plan */
const BR = { R: ["Leader Skill", "--br-r"], B: ["Member Skill", "--br-b"], G: ["Overall Boost", "--br-g"], Y: ["Song Bonus", "--br-y"], S: ["Connect", "--ink-2"] };
const effText = (e, name) => String(e || "").replace(/\[character\]/g, name).replace(/\s*\[value(\/10)?\]%?/g, "").replace(/^Grants /, "").replace(/\.$/, "");
function groups(ids, nodeById, name) {
  const by = {};
  for (const id of ids) {
    const n = nodeById.get(id) || { id, branch: id[0], effect: id };
    const g = (by[n.branch] = by[n.branch] || {});
    const k = n.branch === "S" ? "Connect slot" : effText(n.effect, name);
    g[k] = g[k] || { n: 0, pt: 0 }; g[k].n++; g[k].pt += (n.cost && n.cost.points) || 0;
  }
  return by;
}
export function boardPanel(t, ctx) {
  if (ctx.board !== "develop" && ctx.board !== "state") return "";
  const plan = t.row.board_plan_summary || planFromDoc(t.doc);
  if (!plan && ctx.board === "state" && !t.docErr) return `<div class="v-sect v-pending"><span class="v-sk" style="width:100%;height:60px"></span></div>`;
  if (!plan || !plan.per_character) return `<div class="v-sect v-pending"><span class="v-preview">Not served</span>No Holomem Board plan for this team.</div>`;
  const wallet = ctx.wallet;
  const docBy = new Map(((t.doc && t.doc.characters) || []).map(c => [c.chr, c]));
  const order = ["R", "B", "G", "Y", "S"];
  const rows = plan.per_character.map(pc => {
    const doc = docBy.get(pc.chr);
    const nodeById = new Map(((doc && doc.nodes) || []).map(n => [n.id, n]));
    const buy = pc.buy || [], sell = pc.sell || [];
    const g = groups(buy, nodeById, pc.name), sg = groups(sell, nodeById, pc.name);
    const pct = pc.points_max ? pc.points_used / pc.points_max * 100 : 0;
    const branches = order.filter(b => g[b]).map(b => `<div class="v-branch" style="--br:var(${BR[b][1]})">
        <h5><i></i>${BR[b][0]} <span class="num">${Object.values(g[b]).reduce((a, v) => a + v.n, 0)} nodes</span></h5>
        <ul>${Object.entries(g[b]).map(([k, v]) => `<li><span>${esc(k)}${v.n > 1 ? ` ×${v.n}` : ""}</span><span class="num">${doc ? v.pt + " Pt" : ""}</span></li>`).join("")}</ul></div>`).join("");
    const resets = order.filter(b => sg[b]).map(b => `<div class="v-branch reset" style="--br:var(${BR[b][1]})"><h5><i></i>To reset · ${BR[b][0]}</h5>
        <ul>${Object.entries(sg[b]).map(([k, v]) => `<li><span>${esc(k)}${v.n > 1 ? ` ×${v.n}` : ""}</span><span></span></li>`).join("")}</ul></div>`).join("");
    const sockets = (pc.sockets || []).filter(s => s.card_id);
    return `<details class="v-pr">
      <summary>
        <span class="hm"><img src="/chricon/${esc(pc.chr)}" alt="" onerror="this.style.visibility='hidden'"><b>${esc(pc.name)}</b></span>
        <span class="c dim">${pc.current_owned ?? "—"}</span>
        <span class="c">${buy.length}</span>
        <span class="c ${sell.length ? "" : "dim"}">${sell.length}</span>
        <span class="v-pts"><span class="bar"><i style="width:${pct.toFixed(1)}%"></i></span><span class="num">${pc.points_used ?? "—"}/${pc.points_max ?? "—"}</span></span>
        <svg class="i s go" aria-hidden="true"><use href="#i-chev"/></svg>
      </summary>
      <div class="v-nodes">${branches || `<p class="v-cap" style="grid-column:1/-1">Nothing to unlock.</p>`}${resets}
        ${sockets.length ? `<p class="v-cap" style="grid-column:1/-1">Connect: ${sockets.map(s => { const c = ctx.cardOf(s.card_id); return `${esc(s.slot)} → ${esc(c ? `${c.member} / ${c.costume_name || ""}` : s.card_id)}`; }).join(" · ")}</p>` : ""}
        ${!doc ? `<p class="v-cap" style="grid-column:1/-1">${t.docErr ? "Node names unavailable: " + esc(t.docErr) : "Loading node names…"}</p>` : ""}</div>
    </details>`;
  }).join("");
  const need = plan.materials || {};
  const names = new Map(((wallet && wallet.board_materials) || []).map(m => [m.item_id, m]));
  const ORDER = ["leader", "card", "allmember", "content"];
  const items = Object.keys(need).sort((a, b) => {
    const ka = ORDER.findIndex(k => a.includes(`_${k}-`)), kb = ORDER.findIndex(k => b.includes(`_${k}-`));
    return ka - kb || a.localeCompare(b);
  });
  const mats = items.map(id => {
    const m = names.get(id), have = m ? m.wallet : null, short = have != null && need[id] > have;
    return { id, name: m ? m.name : id, need: need[id], have, short };
  });
  const shortList = mats.filter(m => m.short);
  const resetsN = plan.per_character.reduce((a, pc) => a + (pc.sell || []).length, 0);
  const ptsTotal = t.row.points ?? (plan.per_character.every(pc => pc.points_used != null) ? plan.per_character.reduce((a, pc) => a + pc.points_used, 0) : null);
  const goldItem = ((wallet && wallet.currencies) || []).find(c => c.item_id === "item-gold");
  const gold = goldItem ? goldItem.held : null, cost = resetsN * 1000;
  const us0 = t.row.board_off_us ?? t.row.score.unit_score_board_off, us1 = t.row.optimised_us ?? deckUnit(t.row);
  const uplift = us0 != null && us1 != null
    ? `<span class="v-uplift">Unit Score with no board <span class="num">${fmt(us0)}</span> → <span class="num to">${fmt(us1)}</span></span>` : "";
  return `<details class="v-sect v-plan v-reveal" data-plan="${t.idx}" ${ctx.planOpen ? "open" : ""}>
    <summary><h3>Holomem Board plan</h3>${info("plan")}${ctx.side ? "" : uplift}
      <span class="v-plansum num">${ptsTotal != null ? `${fmt(ptsTotal)} Pt` : ""}${resetsN ? `${ptsTotal != null ? " · " : ""}${resetsN} to reset` : ""}</span>
      <svg class="i s go" aria-hidden="true"><use href="#i-chev"/></svg></summary>
    <div class="v-ptable">
      <div class="v-prh"><span>Holomem</span><span>Unlocked now</span><span>To unlock</span><span>To reset</span><span>Holomem Board Pt</span><span></span></div>
      ${rows}
    </div>
    ${mats.length ? `<div class="v-mats">
      <h4>Required Materials <small>needed / held</small>${info("materials")}</h4>
      <div class="v-matrow">${mats.map(m => `<div class="v-mat${m.short ? " short" : ""}"><img src="/itemicon/${esc(m.id)}" alt="" onerror="this.style.visibility='hidden'"><div><b>${esc(m.name)}</b>
        <span><span class="need">${fmt(m.need)}</span> / <span class="have">${m.have == null ? "—" : fmt(m.have)}</span></span></div></div>`).join("")}</div>
      ${!wallet && ctx.walletErr ? `<p class="v-cap">Your materials could not be read (<span title="${esc(ctx.walletErr)}">details</span>), so nothing is compared against them.</p>` : ""}
      ${shortList.length ? `<p class="v-warn"><svg class="i s"><use href="#i-alert"/></svg>Not enough required materials.<small>${shortList.map(m => `${esc(m.name)} short by ${fmt(m.need - m.have)}`).join(" · ")}</small></p>` : ""}
    </div>` : ""}
    <p class="v-reset${gold != null && cost > gold ? " short" : ""}"><img src="/itemicon/item-gold" alt="" onerror="this.style.visibility='hidden'">Resetting ${resetsN} node${resetsN === 1 ? "" : "s"}: <span class="num need">${fmt(cost)}</span> Hologold${gold != null ? ` · you hold <span class="num">${fmt(gold)}</span>` : ""}${info("reset")}</p>
  </details>`;
}

/* ------------------------------------------------------------ the cross-team tables */
// rows of the "at a glance" table: [label, value-of(team), digits, note, kind, show, neutral].
// Score rows print through HBGrade.score (8 digits, dimmed zeros) like the columns below: all
// scores in a view padded or none (IDENTITY §2). Power, bonus and Pt rows stay plain numbers.
const SC = v => score(v);
export function glanceRows(mode, board, teams) {
  const R = [];
  const has = f => teams.some(t => f(t) != null);
  const S = t => t.row.score, P = t => t.row.panel;
  if (mode === "song") {
    R.push(["Expected Score", t => S(t).song_est, 0, "expected", "head", SC]);
    R.push(["Max", t => S(t).song_max, 0, "max", "", SC]);
    if (board !== "off") R.push(["No Holomem Board", t => S(t).song_off, 0, "off", "", SC]);
  } else if (mode === "across") {
    R.push(["Average score", t => S(t).across_mean, 0, "across", "head", SC]);
  }
  R.push(["Unit Score", t => deckUnit(t.row), 0, mode === "unit" ? "unitscore" : null, mode === "unit" ? "head" : "", SC]);
  if (mode !== "song" && board !== "off") R.push(["Unit Score, no Holomem Board", t => S(t).unit_score_board_off, 0, "off", "", SC]);
  if (has(t => t.row.develop_whatif)) R.push(["Re-planned for this team", t => t.row.develop_whatif ? (t.row.develop_whatif.song_est ?? t.row.develop_whatif.across_mean) : null, 0, "shared", "dim", SC]);
  R.push(["Overall Power", t => P(t).overall_power, 0, null, "sub"]);
  for (const [k, lab] of POWER) R.push([lab, t => P(t)[k], 0, null, "line"]);
  R.push(["Score Bonus", t => t.row.score_bonus ? t.row.score_bonus.score_bonus_permil : null, 1, "bonus", "sub", v => `+${dec(v / 10, 1)}%`]);
  for (const [k, lab] of [["active", "Active Skill"], ["outfit", "Outfit Skill"], ["passive", "Passive Skill"], ["board", "Holomem Board Bonus"], ["special", "Special Skill"]]) {
    R.push([lab, t => { const l = t.row.score_bonus && t.row.score_bonus.lines.find(x => x.key === k); return l ? l.permil : null; }, 1, null, "line", v => `+${dec(v / 10, 1)}%`]);
  }
  R.push(["Score Bonus, ranking model (pooled)", t => P(t).score_bonus_permil, 1, "model_bonus", "dim", v => `+${dec(v / 10, 1)}%`]);
  if (has(t => t.leaf)) R.push(["The Optimizer's figure", t => t.leaf ? t.leaf.song_est : null, 0, "match", "dim", SC]);
  if (has(t => t.row.points)) R.push(["Holomem Board Pt spent", t => t.row.points, 0, null, "", null, true]);
  return R;
}
export function glanceTable(mode, board, teams, cached) {
  const rows = glanceRows(mode, board, teams);
  const ref = teams[0];
  const body = rows.map(([lab, get, , note, kind, show, neutral]) => {
    const vals = teams.map(get);
    const nums = vals.filter(v => v != null);
    const best = nums.length > 1 && !neutral ? Math.max(...nums) : null;
    const allEq = nums.length > 1 && nums.every(v => Math.round(v) === Math.round(nums[0]));
    return `<tr class="${kind || ""}"><th scope="row">${lab}${note ? " " + `<button class="v-info" type="button" data-note="${note}" aria-label="About this" aria-expanded="false">i</button>` : ""}</th>${vals.map((v, k) => {
      const txt = v == null ? "—" : show ? show(v) : fmt(v);
      const d = k && v != null && get(ref) != null && !neutral ? deltaPill(v, get(ref), kind === "head" ? 2 : 1) : "";
      return `<td class="${best != null && !allEq && Math.round(v) === Math.round(best) ? "best" : ""}"><span class="num">${txt}</span>${d}</td>`;
    }).join("")}</tr>`;
  }).join("");
  const cachedRow = cached.some(c => c != null)
    ? `<tr class="dim"><th scope="row">Optimizer's search ${info("cached")}</th>${cached.map(c => `<td><span class="num">${c == null ? "—" : score(c)}</span></td>`).join("")}</tr>` : "";
  return `<table class="v-glance" style="--n:${teams.length}">
    <thead><tr><th></th>${teams.map(t => `<th scope="col"><span class="v-badge" style="--tc:var(--t${t.idx})">${t.label}</span>${t.idx ? `<small>vs A</small>` : `<small>reference</small>`}</th>`).join("")}</tr></thead>
    <tbody>${body}${cachedRow}</tbody></table>`;
}

const gateKey = g => [g.card_id, g.slot_kind, g.dimension, g.value, g.threshold].join("|");
const KIND = { costume: "Outfit Skill", PASSIVE: "Passive Skill", ACTIVE: "Active Skill", SPECIAL: "Special Skill" };
export function gatesTable(teams, cardOf) {
  const all = new Map();
  teams.forEach((t, k) => (t.row.gates || []).forEach(g => {
    const key = gateKey(g);
    if (!all.has(key)) all.set(key, { g, per: Array(teams.length).fill(null) });
    all.get(key).per[k] = g;
  }));
  if (!all.size) return `<p class="v-cap">No team carries a skill with a member condition.</p>`;
  const rows = [...all.values()].sort((a, b) => a.g.card_id.localeCompare(b.g.card_id));
  return `<table class="v-gates"><thead><tr><th>Card</th><th>Skill</th><th>Needs</th>${teams.map(t => `<th><span class="v-badge" style="--tc:var(--t${t.idx})">${t.label}</span></th>`).join("")}</tr></thead><tbody>
    ${rows.map(({ g, per }) => {
      const c = cardOf(g.card_id) || { id: g.card_id, member: g.card_id };
      const need = `${g.threshold}+ ${esc(g.dimension === "generation" ? genName(g.value) : g.value)} members`;
      return `<tr><td><span class="v-cardref"><img src="${thumb(c.id)}" alt=""><span><b>${esc(c.member)}</b><small>${esc(c.costume_name || "")}</small></span></span></td>
        <td>${KIND[g.slot_kind] || esc(g.slot_kind)}</td><td>${need}</td>
        ${per.map(p => `<td>${p == null ? `<span class="v-gate none">Not in team</span>` : `<span class="v-gate ${p.fired ? "met" : "miss"}">${p.fired ? "Met" : "Not met"} <span class="num">${p.actual_count}/${g.threshold}</span></span>`}</td>`).join("")}</tr>`;
    }).join("")}</tbody></table>`;
}

export function skillsTable(teams, cardOf) {
  const key = r => [r.card_id, r.slot_kind, r.family, r.effect_type].join("|");
  const all = new Map();
  teams.forEach((t, k) => (t.row.contributions || []).forEach(r => {
    const kk = key(r);
    if (!all.has(kk)) all.set(kk, { r, per: Array(teams.length).fill(null) });
    all.get(kk).per[k] = r;
  }));
  const spread = x => { const v = x.per.map(p => p ? p.weighted_rate || 0 : 0); return Math.max(...v) - Math.min(...v); };
  const rows = [...all.values()].sort((a, b) => spread(b) - spread(a));
  return `<table class="v-gates v-skills"><thead><tr><th>Card</th><th>Skill</th><th>Effect</th>${teams.map(t => `<th><span class="v-badge" style="--tc:var(--t${t.idx})">${t.label}</span></th>`).join("")}</tr></thead><tbody>
    ${rows.map(({ r, per }) => {
      const c = cardOf(r.card_id) || { id: r.card_id, member: r.card_id };
      return `<tr><td><span class="v-cardref"><img src="${thumb(c.id)}" alt=""><span><b>${esc(c.member)}</b><small>${esc(c.costume_name || "")}</small></span></span></td>
        <td>${KIND[r.slot_kind] || esc(r.slot_kind)}</td><td class="mono">${esc(r.family)} · ${esc(r.effect_type)}</td>
        ${per.map(p => `<td class="num">${p == null ? `<span class="v-gate none">—</span>` : `${dec(p.weighted_rate || 0, 4)}${p.state !== "applied" ? ` <small>${esc(p.state)}</small>` : ""}`}</td>`).join("")}</tr>`;
    }).join("")}</tbody></table>`;
}

/* ------------------------------------------------------------ one team's head (score + delta) */
export function teamHead(t, ctx) {
  const v = headline(t.row, ctx.mode), ref = ctx.ref ? headline(ctx.ref.row, ctx.mode) : null;
  const d = t.idx && ref ? (v / ref - 1) * 100 : null;
  const cert = t.row.certificate || {};
  return `<div class="v-thead">
    <span class="v-badge lg" style="--tc:var(--t${t.idx})">${t.label}</span>
    <div class="v-kv"><p class="k">${headLabel(ctx.mode)} ${info(ctx.mode === "song" ? "expected" : ctx.mode === "across" ? "across" : "unitscore")}</p>
      <div class="v-scoreline"><p class="v-score">${score(v)}</p></div></div>
    ${d != null ? `<div class="v-vs"><b class="${Math.abs(d) < 0.005 ? "" : d > 0 ? "up" : "down"}">${sgn(d, 2)}</b><span class="num">${sgnN(v - ref)}</span><small>vs A</small></div>`
      : t.idx === 0 && ctx.n > 1 ? `<div class="v-vs ref"><small>Reference</small></div>` : ""}
    <div class="v-tact">
      <span class="v-tag" title="${esc(cert.status === "EXACT" ? "Certified exact" : "Scored, not certified as the best team")}">${cert.status === "EXACT" ? "EXACT" : "Not certified"}</span>
    </div>
  </div>`;
}
export const driverLine = t => t.diff ? `<p class="v-driver"><span>Biggest difference vs A</span> ${esc(driverText(t.diff.sentence, t.label))} ${info("driver")}</p>` : "";
// the server names the two teams of a pair A and B; on this page B is this team's own letter
const SRV_LINE = { "Member Parameter": "Member Parameter", Passive: "Passive Skill", Outfit: "Outfit Skill", Board: "Holomem Board Bonus",
  Memory: "Memory Bonus", "Power-Up": "Member Power-Up Bonus" };
const APOS = String.fromCharCode(39);
function driverText(s, label) {
  let x = String(s || "").replace(/\bB\b/g, label).replace(/ACTIVE\/score-bonus pool/g, "Score Bonus").replace(/\bpp\b/g, "points")
    .replace(/board score rate/g, "Holomem Board score rate").replace(/outfit gate/g, "Outfit Skill condition");
  for (const [k, v] of Object.entries(SRV_LINE)) x = x.split(APOS + "s " + k + " +").join(APOS + "s " + v + " +");
  return x;
}
