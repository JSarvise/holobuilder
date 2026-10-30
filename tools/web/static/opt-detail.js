// Optimizer page: the open team's detail sections. Unit Score Details, the run panel,
// the timeline and the Holomem Board plan. Every figure is a server field; the only
// arithmetic here is layout (shares of a bar, sums of server-listed node costs).
// `det.fig` is the row's ONE number set, from the route that rebuilds the row's own
// board (opt-score.js / opt-event.js fill it); `det.cur` is the same team on the boards
// as they are (/api/compare captured).
import { byId, esc, fmt, dec, sgn, info, INK, thumb, place, NOTES } from "/opt-util.js";
import { badge as rankBadge, score, rankOf, unitRows } from "/hb-grade.js";
import { render as renderTimeline } from "/hb-timeline.js";
import { mount as mountFigure } from "/hb-live2d.js";
import { boardPlanHTML, wireBoardPlan } from "/opt-boardplan.js";

/* ------------------------------------------------------------ the leader, as the unit screen shows it */
// The game's "Build a Live Lineup" screen draws the leader as a Live2D figure of her outfit on
// the left of the unit: name and unit tags over it, a Leader
// label, the Outfit Skill under it. The slot holds the card's own art first; hb-live2d.js
// replaces it with the figure when one was converted (the art stays as the fallback).
export function leaderColumn(L, { note = "", outfit = "" } = {}) {
  const chr = "chr-" + String(L.id || "").split("-")[1];
  const gens = ((window.HB_GENS && window.HB_GENS.by[chr]) || []).filter(g => g.reg !== null);
  const tags = gens.map(g => `<span class="o-lgen" style="--g1:${g.c};--g2:${g.c2}">${esc(g.n)}</span>`).join("");
  return `<aside class="o-lead" aria-label="Leader">
    <div class="o-fig" data-figure="${esc(L.id || "")}">
      <img class="o-fig-art" src="/cardart/vert/${encodeURIComponent(L.id || "")}?w=600" alt="" loading="lazy">
      <div class="o-lead-id"><div class="o-lgens">${tags}</div><b>${esc(L.member || L.id || "")}</b>
        <small>${esc(L.costume_name || "")}</small></div>
      <span class="o-lead-pill">Leader${note ? ` · ${esc(note)}` : ""}</span>
    </div>
    ${outfit}
  </aside>`;
}
/** Bring the figure up in a freshly drawn panel (no-op when the slot is gone or has none). */
export function wakeFigure(root) {
  const slot = root && root.querySelector(".o-fig[data-figure]");
  if (slot && slot.dataset.figure) mountFigure(slot, slot.dataset.figure);
}
// the ONE large grade of the view: the Unit Score, as the game grades it
const unitBadge = v => { const g = rankOf(v, unitRows()); return g ? rankBadge(g, { size: 72 }) : ""; };

/* ------------------------------------------------------------ the deck screen's figures */
const POWER = [["member_parameter", "Member Parameter", "--t-member"], ["outfit", "Outfit Skill", "--t-leader"], ["board", "Holomem Board Bonus", "--t-board"],
  ["passive", "Passive Skill", "--t-passive"], ["memory", "Memory Bonus", "--t-memory"], ["power_up", "Member Power-Up Bonus", "--t-powerup"]];
const BONUS_INK = { active: "--t-active", outfit: "--t-leader", passive: "--t-passive", board: "--t-board", special: "--t-special" };
// the deck screen's Unit Score: the Score Bonus split's own (score_bonus.unit_score), else the panel's
export const deckUnit = f => f && f.score_bonus && f.score_bonus.unit_score != null ? f.score_bonus.unit_score
  : f && f.panel && f.panel.unit_score != null ? Math.ceil(f.panel.unit_score) : null;

/* ------------------------------------------------------------ row vs detail */
const cap = t => String(t).charAt(0).toUpperCase() + String(t).slice(1);
// the row's headline next to its detail's, with the route names; equal = one number set
export function matchLine(T, det) {
  const f = det.fig; if (!f || f.value == null || T.expected == null) return "";
  const tip = esc(`row: ${T.api || T.route} · detail: ${f.api || f.route}`);
  if (Math.round(f.value) === Math.round(T.expected)) {
    return `<p class="o-match ok" title="${tip}"><svg class="i s"><use href="#i-check"/></svg><span>The detail below matches the row: <span class="num">${fmt(f.value)}</span> on the row's own board.</span>${info("match")}</p>`;
  }
  const why = T.source === "cached" && f.row_matches === false ? " The cached search was computed from older data; the detail is today's figure for this team."
    : T.infmats ? " This row was developed with unlimited materials; the detail rebuilds its board on your real bench." : "";
  return `<p class="o-match off" title="${tip}"><svg class="i s"><use href="#i-alert"/></svg><span>${esc(cap(T.route))} ranked this team at <span class="num">${fmt(T.expected)}</span>; the detail (${esc(f.route)}) gives <span class="num">${fmt(f.value)}</span> (<b>${sgn((f.value / T.expected - 1) * 100, 2)}</b>).${why}</span>${info("match")}</p>`;
}

/* ------------------------------------------------------------ Unit Score Details */
// what the boards add on THIS song only: the yellow song bonus per singer
// against its 10 % cap, and the red singer-conditional terms (they need the leader to sing)
function songBonusHTML(sg) {
  if (!sg) return "";
  const pct = v => `${dec(v / 10, 1)}%`;
  const kind = { solo: "solo", group: "unit", all: "whole roster" }[sg.scope] || sg.scope;
  const by = (sg.yellow_by_singer || []).filter(x => x.permil > 0);
  const capped = sg.yellow_uncapped_permil > sg.yellow_cap_permil;
  const yellow = sg.yellow_permil > 0
    ? `<b class="num">+${pct(sg.yellow_permil)}</b> song score bonus${capped ? ` (the boards give ${pct(sg.yellow_uncapped_permil)}; the game counts ${pct(sg.yellow_cap_permil)})` : sg.scope === "solo" ? "" : ` of ${pct(sg.yellow_cap_permil)} possible`}`
      + (by.length ? `<br><span class="o-cap">${by.map(x => `${esc(x.holomem)} ${pct(x.permil)}`).join(" · ")}</span>` : "")
    : `none: no singer's board has yellow nodes of this song's type`;
  const red = sg.red_support_permil || sg.red_power
    ? `<b class="num">+${pct(sg.red_support_permil)}</b> Score Support${sg.red_power ? `, <b class="num">+${fmt(sg.red_power)}</b> Overall Power` : ""}`
    : sg.leader_sings ? `the leader sings, but her board has no singer reds bought`
    : `none: the leader does not sing this song (singer reds pay only on the leader)`;
  return `<div class="o-songbonus"><h5>On this song <span class="o-cap">(${esc(kind)}: ${esc((sg.singers || []).join(", "))})</span></h5>
    <p><i class="y"></i><span>Yellow</span><span>${yellow}</span></p>
    <p><i class="r"></i><span>Red</span><span>${red}</span></p></div>`;
}
function bonusTerm(f) {
  const SB = f.score_bonus, P = f.panel;
  NOTES.bonus_row = NOTES.bonus + (P && P.score_bonus_permil != null
    ? `<br><br>${NOTES.model_bonus} For this team: <b>+${dec(P.score_bonus_permil / 10, 1)}%</b>.` : "");
  const pooled = songBonusHTML(SB && SB.song);
  if (!SB || !SB.lines) return `<div class="o-term"><h4>Score Bonus</h4>
    <p class="o-cap" style="margin-top:0"><span class="o-preview">Split not served for this row</span></p>${pooled}</div>`;
  const tot = SB.lines.reduce((a, l) => a + Math.max(0, l.permil || 0), 0) || 1;
  return `<div class="o-term">
    <h4><span>Score Bonus ${info("bonus_row")}</span><span class="num">+${dec(SB.score_bonus_permil / 10, 1)}%</span></h4>
    <div class="o-sbar" role="img" aria-label="Score Bonus split">${SB.lines.map(l =>
      `<i style="--seg:var(${BONUS_INK[l.key] || "--ink-3"});width:${(Math.max(0, l.permil) / tot * 100).toFixed(2)}%" data-tip="${esc(l.label)}|+${dec(l.permil / 10, 1)}%"></i>`).join("")}</div>
    <ul class="o-legend">${SB.lines.map(l =>
      `<li style="--seg:var(${BONUS_INK[l.key] || "--ink-3"})"><i></i><span>${esc(l.label)}</span><span class="num">+${dec(l.permil / 10, 1)}%</span></li>`).join("")}</ul>
    ${(SB.unmodelled || []).length ? `<p class="o-cap">Not modelled: ${SB.unmodelled.map(esc).join(", ")}</p>` : ""}
    ${pooled}
  </div>`;
}
export function unitPanel(T, det) {
  if (det.err) return `<div class="o-card o-pending o-reveal"><span class="o-preview">Unavailable</span>${esc(det.err)}</div>`;
  const f = det.fig;
  if (!f) return `<div class="o-card o-reveal"><div class="o-sh"><h3>Unit Score Details</h3></div>
    <div class="o-ushead"><span class="o-sk" style="width:64px;height:64px;border-radius:50%"></span><span class="o-sk" style="width:260px;height:44px"></span></div>
    <p class="o-cap">Scoring this team on its own board…</p></div>`;
  const P = f.panel || {};
  const us = deckUnit(f);
  const total = POWER.reduce((a, [k]) => a + (P[k] || 0), 0) || 1;
  const was = det.cur && T.boards !== "current" ? deckUnit(det.cur) : null;
  return `<div class="o-card o-reveal">
    <div class="o-sh"><h3>Unit Score Details</h3>${info("unit")}</div>
    <div class="o-ushead">
      ${us != null ? unitBadge(us) : ""}
      <div><p class="lab">Unit Score ${info("grade")}</p><p class="o-padnum" aria-label="${fmt(us)}">${score(us)}</p></div>
      ${was != null ? `<div class="was">Boards as they are<span class="num">${fmt(was)}</span></div>` : ""}
    </div>
    <div class="o-uscols">
      <div class="o-term">
        <h4>Overall Power <span class="num">${fmt(P.overall_power)}</span></h4>
        <div class="o-sbar" role="img" aria-label="Overall Power split">${POWER.map(([k, lab, c]) =>
          `<i style="--seg:var(${c});width:${((P[k] || 0) / total * 100).toFixed(2)}%" data-tip="${esc(lab)}|${fmt(P[k])}|${dec((P[k] || 0) / total * 100, 1)}% of Overall Power"></i>`).join("")}</div>
        <ul class="o-legend">${POWER.map(([k, lab, c]) =>
          `<li style="--seg:var(${c})"><i></i><span>${lab}</span><span class="num">${fmt(P[k])}</span></li>`).join("")}</ul>
      </div>
      ${bonusTerm(f)}
    </div>
    ${f.deck_power != null ? `<p class="o-cap" title="${esc(f.api || "")}">Scored on deck power <span class="num">${fmt(f.deck_power)}</span>${f.deck_power_basis === "server" ? " (the deck screen's Overall Power)" : f.deck_power_basis ? ` (${esc(f.deck_power_basis)})` : ""}.</p>` : ""}
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
  return `<div class="o-law" role="img" aria-label="Run score distribution, ${fmt(law.p1)} to ${fmt(law.p99)} from the 1st to the 99th percentile">
      <div class="bars">${bars}${mk}</div>
      <div class="ax"><span class="num">${fmt(lo)}</span>${pm.below ? `<span>${dec(pm.below * 100, 1)}% of runs below</span>` : ""}<span class="num">${fmt(hi)}</span></div>
    </div>`;
}
export function runPanel(T, det) {
  const f = det.fig || {};
  // the detail's own Expected Score when it has one (a mismatch with the row is named above, by matchLine)
  const exp = f.value ?? T.expected, max = f.song_max ?? T.max ?? null;
  const now = det.cur && T.boards !== "current" ? det.cur.value : null;
  const pts = [["Expected", exp, "e"], ["Max", max, ""], ["Now", now, "n"]].filter(p => p[1] != null);
  if (!pts.length) return `<div class="o-card o-pending o-reveal"><span class="o-preview">Not served</span>No run figures for this team.</div>`;
  const law = f.distribution && f.distribution.available !== false ? f.distribution : null;
  const lawState = law
    ? (law.verified ? `<span class="o-tag ok" title="The law's mean reproduces the detail's Expected Score exactly">Verified</span>`
      : law.verified === false ? `<span class="o-tag warn" title="The law's mean is not the detail's figure">Not verified</span>` : "")
    : det.distLoading ? `<span class="o-preview">Computing…</span>`
    : f.distribution && f.distribution.reason ? `<span class="o-preview" title="${esc(f.distribution.reason)}">Distribution unavailable</span>` : "";
  const cov = T.cover || {};
  const stats = [
    cov.active_cover_pct != null ? [`Active coverage`, `${dec(cov.active_cover_pct, 1)}%`, "Share of the song with at least one Active Skill window open"] : null,
    cov.active_overlap_loss_pct != null ? [`Overlap loss`, `${dec(cov.active_overlap_loss_pct, 1)}%`, "Active time lost to windows that overlap"] : null,
    cov.active_dead_start_s != null ? [`First Active at`, `${dec(cov.active_dead_start_s, 1)} s`, "Seconds before the first Active Skill window opens"] : null,
  ].filter(Boolean);
  let body;
  if (law) {
    const q = [["1%", law.p1], ["5%", law.p5], ["Median", law.p50], ["95%", law.p95], ["99%", law.p99]];
    body = `${lawChart(law, [["Expected", f.value ?? exp, "e"], ["Now", now, "n"]])}
      <div class="o-stats o-qs">${q.map(([lab, v]) => `<div title="${lab === "Median" ? "Half the runs score less" : `${lab} of runs score less`}"><span>${lab}</span><b>${fmt(v)}</b></div>`).join("")}</div>
      <div class="o-stats o-s${pts.length + 1}">${pts.map(([lab, v]) => `<div><span>${lab}</span><b>${score(v, 7)}</b></div>`).join("")}<div title="Standard deviation of the run score"><span>Spread (sd)</span><b>±${fmt(law.sd)}</b></div></div>
      <p class="o-cap">${esc(law.premise || "")}. ${law.exact === false ? `Binned to ${fmt(law.bin_width)}-point steps over ${fmt(law.support)} outcomes.` : `Exact over ${fmt(law.support)} outcomes.`}${law.random_windows != null ? ` ${law.random_windows} Active windows roll.` : ""}</p>`;
  } else {
    const lo = Math.min(...pts.map(p => p[1])), hi = Math.max(...pts.map(p => p[1]));
    const span = hi - lo || 1, X = v => 4 + (v - lo) / span * 92;
    body = `<div class="o-range" role="img" aria-label="Expected, max and now on one axis">
      <span class="ln"></span>
      ${max != null ? `<span class="span" style="left:${X(exp)}%;width:${X(max) - X(exp)}%"></span>` : ""}
      ${pts.map(([lab, v, cls]) => `<span class="mk ${cls}" style="left:${X(v)}%" data-tip="${lab}|${fmt(v)}"><span>${lab}</span><i></i></span>`).join("")}
    </div>
    <div class="o-stats">${pts.map(([lab, v]) => `<div><span>${lab}</span><b>${score(v)}</b></div>`).join("")}</div>
    ${T.kind === "song" && (!det.fig || det.distLoading) ? `<p class="o-cap"><span class="o-sk" style="width:100%;height:60px"></span></p>` : ""}`;
  }
  return `<div class="o-card o-reveal">
    <div class="o-sh"><h3>Score distribution</h3>${info("run")}<span class="right">${lawState}</span></div>
    ${body}
    ${max != null && exp ? `<p class="o-cap">Max is <span class="num">${sgn((max / exp - 1) * 100)}</span> over the expected score: the headroom if every Active Skill fires.</p>` : ""}
    ${now != null && exp ? `<p class="o-cap">With your Holomem Boards as they are this team scores <span class="num">${fmt(now)}</span> (${sgn((now / exp - 1) * 100)}).</p>` : ""}
    ${stats.length ? `<div class="o-stats">${stats.map(([a, b, t]) => `<div title="${esc(t)}"><span>${a}</span><b>${b}</b></div>`).join("")}</div>` : ""}
  </div>`;
}

/* ------------------------------------------------------------ a plan read off the board doc */
// For a row with a board_state but no board_plan_summary (a cached or event row): per
// holomem, the doc's own to_buy / to_sell / point cost and budget; materials = the sum of
// the server's node costs over the nodes to unlock.
export function planFromDoc(doc) {
  if (!doc || !doc.characters) return null;
  // what the buys cost, and what the resets give back (a reset refunds the node's materials)
  const materials = {}, refund = {};
  const add = (into, nodes, ids) => {
    for (const id of ids || []) {
      const cost = (nodes.get(id) || {}).cost || {};
      for (const [k, v] of Object.entries(cost)) if (k !== "points") into[k] = (into[k] || 0) + v;
    }
  };
  const per_character = doc.characters.filter(c => c.plan).map(c => {
    const nodes = new Map((c.nodes || []).map(n => [n.id, n]));
    add(materials, nodes, c.plan.to_buy);
    add(refund, nodes, c.plan.to_sell);
    return { chr: c.chr, name: c.name, buy: c.plan.to_buy || [], sell: c.plan.to_sell || [], current_owned: c.owned_count,
      points_used: c.plan.point_cost, points_max: c.plan.point_budget,
      sockets: Object.entries(c.plan.slots || {}).map(([slot, card_id]) => ({ slot, card_id })) };
  });
  return { per_character, materials, refund, from_doc: true };
}

/* ------------------------------------------------------------ timeline */
export function timelinePanel(T, det, cardOf) {
  if (!det.tl) return det.tlErr
    ? `<div class="o-sect o-pending"><span class="o-preview">Unavailable</span>Timeline: ${esc(det.tlErr)}</div>`
    : `<div class="o-sect o-pending"><span class="o-sk" style="width:100%;height:120px"></span></div>`;
  // drawn by the shared component (hb-timeline.js) once the block is in the DOM: wireTimeline
  return `<div class="o-sect o-reveal">
    <div class="o-sh"><h3>Skill uptime</h3>${info("timeline")}</div>
    <div class="o-hbtl" data-tl="${esc(T.key)}" data-leader="${esc(T.leader || "")}"></div>
  </div>`;
}
export function wireTimeline(root, det, cardOf) {
  const el = root.querySelector(".o-hbtl"); if (!el || !det.tl) return;
  const nm = id => { const c = cardOf(id); return c ? `${c.member || ""}${c.costume_name ? " · " + c.costume_name : ""}` : id; };
  const L = cardOf(el.dataset.leader) || null;
  renderTimeline(el, det.tl.timeline, {
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
// the bench's root sockets the plan chose together with the team's: a card
// is used once account-wide, so a card the team takes leaves its bench socket for another
function benchSocketsHTML(f, doc, cardOf) {
  const list = (f && f.bench_sockets) || [];
  if (!list.length) return "";
  const nameOf = chr => ((doc && doc.characters) || []).find(c => c.chr === chr)?.name || chr;
  const rows = [...list].sort((a, b) => b.value - a.value).map(p => {
    const c = cardOf(p.card);
    return `<li><span>${esc(p.holomem || nameOf(p.chr))}</span><span>${esc(c ? `${c.member} / ${c.costume_name || c.id}` : p.card)}</span><span class="num">×${dec(p.multiplier, 2)}</span></li>`;
  }).join("");
  return `<details class="o-benchsock"><summary>Bench Connect cards: ${list.length} root sockets (S-001) <small>chosen with the team's, each card used once</small></summary><ul>${rows}</ul></details>`;
}
export function boardPanel(T, det, wallet, isOpen, cardOf) {
  // the WHOLE account's plan (/api/board-plan-account) when served, else the team's six boards
  const doc = det.acct || det.doc;
  const plan = det.acct ? planFromDoc(det.acct) : (T.plan || planFromDoc(det.doc));
  if (!plan && !doc && !det.docErr && !det.acctLoading && (det.fig || {}).board_state) return `<div class="o-sect o-pending"><span class="o-sk" style="width:100%;height:60px"></span></div>`;
  if (!plan || !plan.per_character) return `<div class="o-sect o-pending"><span class="o-preview">Not served</span>No Holomem Board plan for this team.</div>`;
  const unit0 = det.fig && det.fig.unit_off != null ? Math.ceil(det.fig.unit_off) : null;
  const unitCur = deckUnit(det.cur), unit1 = deckUnit(det.fig);
  const changedN = plan.per_character.filter(pc => (pc.buy || []).length || (pc.sell || []).length).length;
  // a maxed account: every holomem off the team is scored with a COMPLETE board (the full
  // 453 Pt tree, level-2 Connect cards), so there is nothing to plan for them and only the
  // leader and the 5 members appear
  const scope = det.unlimited ? (det.acct
      ? `A maxed account, against your boards now: the nodes that add score and the paths to them (no mini-game, reward, EXP or LIFE nodes), level-2 Connect cards. <b class="num">${fmt(changedN)}</b> boards to change, the team first, then the bench.`
      : det.acctLoading ? "Every holomem's scoring nodes against your boards…" : "The team's boards. Every other holomem is counted with all of its scoring nodes and level-2 Connect cards.")
    : det.acct ? `Every holomem the plan changes: <b class="num">${fmt(changedN)}</b> boards, the team first, then the bench (green nodes lift every team).`
    : det.acctLoading ? "Planning every holomem's board…" : det.acctErr ? `The team's six boards (the whole-account plan is unavailable: ${esc(det.acctErr)}).` : "The team's six boards.";
  // materials: the server's sums (board_plan_summary.materials) against the wallet (/api/resources)
  // the plan spends what the resets refund too (the planner's wallet counts them), so a material is
  // short only when the buys exceed the held amount PLUS the refunds -- not on the buys alone
  // (that would flag a plan that fits as "short")
  // a maxed account has no wallet: its boards are complete and nothing is checked against yours
  // (shown on a maxed account too, as what it would take from your boards now, but never a warning)
  const need = plan.materials || {}, back = det.noRefund ? {} : (plan.refund || {});
  const names = new Map(((wallet && wallet.board_materials) || []).map(m => [m.item_id, m]));
  const ORDER = ["leader", "card", "allmember", "content"];
  const items = Object.keys(need).sort((a, b) => {
    const ka = ORDER.findIndex(k => a.includes(`_${k}-`)), kb = ORDER.findIndex(k => b.includes(`_${k}-`));
    return ka - kb || a.localeCompare(b);
  });
  const mats = items.map(id => {
    const m = names.get(id), held = m ? m.wallet : null, refunded = back[id] || 0;
    const have = held == null ? null : held + refunded, short = have != null && need[id] > have;
    return { id, name: m ? m.name : id, need: need[id], have, held, refunded, short };
  });
  const shortList = mats.filter(m => m.short);
  const resetsN = plan.per_character.reduce((a, pc) => a + (pc.sell || []).length, 0);
  const goldItem = ((wallet && wallet.currencies) || []).find(c => c.item_id === "item-gold");
  const gold = goldItem ? goldItem.held : null, cost = resetsN * 1000;
  const uplift = unit1 != null && unitCur != null
    ? `<span class="o-uplift">Unit Score <span class="num">${fmt(unitCur)}</span> → <span class="num to">${fmt(unit1)}</span> <span class="gain">${sgn((unit1 / unitCur - 1) * 100)}</span></span>`
    : unit1 != null && unit0 ? `<span class="o-uplift">Unit Score with no board <span class="num">${fmt(unit0)}</span> → <span class="num to">${fmt(unit1)}</span></span>` : "";
  return `<details class="o-sect o-plan o-reveal" data-plan="${T.key}" ${isOpen ? "open" : ""}>
    <summary><h3>Holomem Board plan</h3>${info("plan")}${uplift}
      <svg class="i s go" aria-hidden="true"><use href="#i-chev"/></svg></summary>
    <p class="o-cap o-bp-scope">${scope}</p>
    ${boardPlanHTML(T.key, doc, !!det.acctLoading)}
    ${mats.length ? `<div class="o-mats">
      <h4>Required Materials <small>${Object.keys(back).length ? "needed / held + back from resets" : "needed / held"}</small>${info("materials")}</h4>
      <div class="o-matrow">${mats.map(m => `<div class="o-mat${m.short ? " short" : ""}"><img src="/itemicon/${esc(m.id)}" alt="" onerror="this.style.visibility='hidden'"><div><b>${esc(m.name)}</b>
        <span><span class="need">${fmt(m.need)}</span> / <span class="have">${m.have == null ? "—" : fmt(m.have)}</span>${m.refunded ? `<small class="o-back">${fmt(m.held)} + ${fmt(m.refunded)} back</small>` : ""}</span></div></div>`).join("")}</div>
      ${shortList.length && !det.unlimited ? `<p class="o-warn"><svg class="i s"><use href="#i-alert"/></svg>Not enough required materials.<small>${shortList.map(m => `${esc(m.name)} short by ${fmt(m.need - m.have)}`).join(" · ")}</small></p>` : ""}
    </div>` : ""}
    ${det.unlimited && !resetsN ? "" : `<p class="o-reset${gold != null && cost > gold ? " short" : ""}"><img src="/itemicon/item-gold" alt="" onerror="this.style.visibility='hidden'">Resetting ${resetsN} node${resetsN === 1 ? "" : "s"}: <span class="num need">${fmt(cost)}</span> Hologold${gold != null ? ` · you hold <span class="num">${fmt(gold)}</span>` : ""}${info("reset")}</p>`}
    ${resetPriceHTML(det, resetsN)}
    ${benchSocketsHTML(det.fig, doc, cardOf)}
  </details>`;
}

/* what the row's resets buy: the same team re-planned with fewer resets, each plan scored like the row */
function resetPriceHTML(det, resetsN) {
  if (det.rp) {
    const pts = det.rp.points || [], own = pts.find(p => p.label === "This plan");
    const rows = pts.map(p => {
      const d = own && p.score != null && p !== own ? (p.score / own.score - 1) * 100 : null;
      return `<tr${p === own ? ' class="own"' : ""}><th>${esc(p.label)}</th>
        <td class="num">${p.resets == null ? "—" : fmt(p.resets)}</td><td class="num">${p.hologold == null ? "—" : fmt(p.hologold)}</td>
        <td class="num">${p.score == null ? esc(p.reason || "—") : fmt(Math.round(p.score))}</td>
        <td class="num ${d != null && d < 0 ? "loss" : ""}">${d == null ? "" : sgn(d)}</td></tr>`;
    }).join("");
    return `<div class="o-rprice"><h4>What the resets buy${info("reset_price")}</h4>
      <table><thead><tr><th>Plan</th><th>Resets</th><th>Hologold</th><th>Expected Score</th><th>vs this plan</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }
  if (det.rpLoading) return `<p class="o-rprice-wait"><span class="o-sk" style="width:14px;height:14px"></span>Re-planning this team with fewer resets (three plans, about a minute)…</p>`;
  if (det.rpErr) return `<p class="o-warn">Couldn't price the resets: ${esc(det.rpErr)}</p>`;
  if (det.rpBody && resetsN > 0) return `<button type="button" class="btn o-rpbtn" data-rp>What do these ${fmt(resetsN)} resets buy?</button>`;
  return "";
}

/** Draw the plan viewer of a freshly painted team block (after boardPanel's HTML is in). */
export function wirePlan(root, T, det, team) {
  const doc = det.acct || det.doc;
  if (doc) wireBoardPlan(root, T.key, doc, team);
}
