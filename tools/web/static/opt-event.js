// Optimizer page, goals "Event Points" and "Event Song Score": the event search job
// (POST /api/event/run, SSE /events/<id>, POST /cancel/<id>; contract in tools/web/EVENT-API.md).
// Every number is the server's: Event Pt, live score, Event Bonus, the bound.
import { byId, esc, fmt, dec, sgn, pct, info, AC, INK, thumb, getJSON, postJSON, toast, reveal, normCard, outfitText, stash, unstash, withState, loadStates } from "/opt-util.js";
import { unitPanel, runPanel, timelinePanel, wireTimeline, boardPanel, matchLine, leaderColumn, wakeFigure, wirePlan } from "/opt-detail.js";
import { score } from "/hb-grade.js";
import { list as rankList } from "/hb-rank.js";
import { assumptionsOf, leaderOpts, withDistribution } from "/opt-score.js";
import { progressCard } from "/opt-progress.js";

let live = null, run = null, open = new Set([0]);
let sel = 0, diffMode = true;   // the selected row on the right; the list outlines cards not in #1
const details = new Map();   // row key -> {fig, tl, doc, err…}: /api/row-detail on the event leaf (plain, refine:false)
let wallet = null;
let multiScore = "";   // the room's MULTI SCORE the player typed (one room, every row)
let ownScore = "";     // the player's own score in that Live (empty: the row's Expected Score)
let multiTimer = 0;
const STAGES = { build: "Loading your account", score_search: "Searching by score",
  event_search: "Searching by the event objective", bound: "Checking that nothing beats it",
  replan: "Re-planning the best teams' boards", refine: "Choosing the activation nodes for this song",
  detail: "Reading the best teams' boards" };
const HL_STAGES = { build: "Loading your account and the Highlight pool",
  event_search: "Searching by Event Points on a sample of the pool", rescore: "Scoring the best teams on every song of the pool",
  refine: "Choosing the activation nodes per song" };
// the Score search's levers and pins on the account; Maxed account (also for
// Events) is its own basis: every card of the game at max level and bloom 5, complete boards,
// no account lever (null)
const isMaxed = S => S.cards === "maxed";
const eventAssumptions = S => isMaxed(S) ? null : assumptionsOf(S);
const MAXED_LEVERS = { level: "max", bloom: 5, sp_training: 4 };
function eventFilters(S) {
  const f = { ...leaderOpts(S) };
  if (S.require.length) f.require = S.require;
  if (S.exclude.length) f.exclude = S.exclude;
  return f;
}
const optsKey = S => JSON.stringify([S.goal, S.song, S.difficulty, S.boosts, S.goal === "event_points" ? S.holopass : 0,
  S.platform, !!S.auto, eventAssumptions(S), eventFilters(S)]);
const isEP = S => S.goal === "event_points";

/* ------------------------------------------------------------------ Highlight */
// The multi Live pick "Highlight": a random WHOLE song among the songs of the
// open chapter's Event Bonus holomems; its length bonus is a 170 s song's (hlPct). The
// server ranks each team by its MEAN score over the pool's charts (event_mode.py, Highlight);
// Event Points only. The game has no image for the tile: it composes it at runtime (#FFB31C,
// the white disc, the wave, the label), so the page composes it the same way from the
// official sprites (/ui/multi_lobby/…).
export const HL = "highlight";
export const isHighlight = song => String(song || "").trim().toLowerCase() === HL;
// the Highlight Bonus in percent: the song length formula (event_mode.length_permil_estimate)
// for a 170 s song, whatever song is drawn -- 54 at 1 boost, 50 at 2-3, 42 at 4+ (as the
// result screens show: 42 at 4 and 10, 50 at 3; event_mode.HIGHLIGHT_SECONDS)
export const hlPct = boosts => {
  const up = { 1: 300, 2: 200, 3: 200 }[boosts] || 0, num = 120 * 10 * (1000 + up), den = 29 * 1000;
  return Math.floor((num + den - 1) / den);
};
// a Highlight row's detail on the WHOLE pool (the selector's first option): one board and
// one slot order for every song, since the song is drawn after the team is set
// (`row_detail.pool_detail`)
const HL_POOL = "*pool";
export const highlightTile = () => `<span class="o-hl" aria-hidden="true"><i class="o-hl-wave"></i><b>Highlight</b><i class="o-hl-disc"></i></span>`;
// the pool as the server builds it (`event_mode.highlight_pool`): every song one of the event
// holomems sings (group songs included) with a chart at this difficulty. Read from /api/songs
// and /api/event before a run; a result's own `highlight.pool` replaces it
export function highlightPool(D) {
  const mems = ((D.event && D.event.live && D.event.live.holomems) || []).map(m => m.character_id).filter(Boolean);
  const want = new Set(mems);
  const songs = (D.songs || []).filter(s => (s.character_ids || []).some(id => want.has(id)))
    .sort((a, b) => a.music_id.localeCompare(b.music_id))
    .map(s => ({ music_id: s.music_id, title: s.title || s.music_id, singer: s.singer || "", notes: s.notes }));
  return { holomems: mems, pool: songs, skipped: [] };
}
// the pool, collapsed: titles, and the songs left out for want of a chart
const poolTitle = (hl, id) => ((hl && hl.pool || []).find(s => s.music_id === id) || {}).title || id || "";
function poolHTML(hl, from) {
  const pool = (hl && hl.pool) || [], skipped = (hl && hl.skipped) || [];
  if (!pool.length && !skipped.length) return "";
  return `<details class="o-hlpool"><summary>Highlight pool: ${pool.length} song${pool.length === 1 ? "" : "s"}${skipped.length ? ` · ${skipped.length} skipped` : ""} <small>${esc(from)}</small></summary>
    <ul>${pool.map(s => `<li><img src="/jacket/${encodeURIComponent(s.music_id)}" alt="" loading="lazy"><span><b>${esc(s.title)}</b><small>${esc(s.singer || "")}</small></span></li>`).join("")}</ul>
    ${skipped.length ? `<p class="o-cap">Skipped (no chart at this difficulty on disk): ${skipped.map(s => esc(s.title || s.music_id)).join(", ")}.</p>` : ""}
  </details>`;
}

/* ------------------------------------------------------------------ idle */
// the last answer comes back when the page is opened again (opt-util.stash)
let restored = false;
const remember = ctx => live && stash("event", ctx, { key: live.key, res: live.res, sel });
export function showEventIdle(ctx, keepLive) {
  if (!restored) {
    restored = true;
    const k = unstash("event", ctx);
    if (k) { live = { key: k.key, res: k.res, cards: ctx.D.cards }; sel = k.sel || 0; keepLive = true; }
  }
  const S = ctx.S;
  if (live && live.key === optsKey(S)) { render(ctx, live); return; }
  if (!keepLive) { open = new Set([0]); sel = 0; }
  const ev = ctx.D.event;
  if (!ev || ev.available === false) {
    ctx.out().innerHTML = `<section class="o-empty"><h2>No open event</h2><p>${esc((ev && ev.error) || "The event data is not on disk.")} Refresh your account to read the current event.</p></section>`;
    return;
  }
  const mems = (ev.live && ev.live.holomems) || [];
  if (isHighlight(S.song)) {
    const hl = highlightPool(ctx.D);
    ctx.out().innerHTML = `<section class="o-empty">
      <h2>Most Event Pt per Live on Highlight</h2>
      <p>Highlight plays a random whole song of this event’s holomems (${mems.map(m => `<b>${esc(m.name)}</b>`).join(", ") || "none"}), group songs included, and gives a <b>+${hlPct(S.boosts)}%</b> Highlight Bonus at ${S.boosts} boost${S.boosts === 1 ? "" : "s"} in place of the song length bonus (a 170 s song’s, whatever song is drawn).
      Each team is ranked by its mean score over the ${hl.pool.length} songs of the pool, by Event Points only (Event Song Score does not apply).
      Event Bonus counts the five members, never the leader. Press Find best team; the search scores every chart of the pool and takes one to a few minutes.
      <a class="btn small" href="/event" style="margin-left:6px">Event details</a></p>
      ${poolHTML(hl, "from the songs list; the search reads its own")}</section>`;
    return;
  }
  const songBonus = (ev.music || []).filter(m => m.music_id === S.song);
  ctx.out().innerHTML = `<section class="o-empty">
    <h2>${isEP(S) ? "Most Event Pt per Live" : "Best Event Song Score"} on this song</h2>
    <p>${isEP(S)
      ? `Event Bonus counts the five members, never the leader. This event’s holomems: ${mems.map(m => `<b>${esc(m.name)}</b> <span class="eb">${pct(m.permil)}</span>`).join(", ") || "none"}.`
      : songBonus.length ? `On this song, ${songBonus.map(m => `<b>${esc(m.card ? `${m.card.member} / ${m.card.costume_name}` : m.name || "")}</b> <span class="eb">${pct(m.permil)}</span>`).join(", ")} ${songBonus.length > 1 ? "lift the score when any one of them is among the five members. One is enough: more do not stack" : "lifts the score when it is one of the five members"}; the leader and the singers’ other cards do not count.`
      : "This song carries no Song Score Bonus in the current event: the best team is the best-scoring one."}
    Press Find best team; the search takes about 15 seconds. <a class="btn small" href="/event" style="margin-left:6px">Event details</a></p></section>`;
}

/* ------------------------------------------------------------------ search */
// Cancel answers at once: the page drops the search and goes back to idle; the server is
// told too and stops at its next check
export function cancelEvent() {
  if (!run) return;
  if (run.job) postJSON(`/cancel/${encodeURIComponent(run.job)}`, {}).catch(e => toast(e.message));
  const ctx = run.ctx;
  if (run.es) run.es.close();
  run.finish();
  toast("Search cancelled.");
  showEventIdle(ctx);
}
// this search's progress card (opt-progress.js), made once, then only updated
let pc = null;
function progress(ctx, order, stage, detail, ev) {
  if (!pc || !pc.el.isConnected) {
    if (pc) pc.stop();
    const hl = isHighlight(ctx.S.song);
    pc = progressCard("event:" + (hl ? "hl:" : "") + order.join(","), order.map(s => (hl && HL_STAGES[s]) || STAGES[s] || s), { best: false });
    ctx.out().replaceChildren(pc.el);
  }
  const k = order.indexOf(stage);
  const e = ev || {};
  pc.update({ active: k >= 0 ? k : undefined, detail: detail || (e.total ? `${e.done} / ${e.total}` : ""),
    done: e.done, total: e.total });
}
let lastCtx = null;
// "Prove it": the same search again with the bound's exact check (POST /api/event/run certify)
document.addEventListener("click", e => {
  if (e.target.closest("[data-certify]") && lastCtx && !run) runEvent(lastCtx, { certify: true });
});
export async function runEvent(ctx, { certify = false } = {}) {
  lastCtx = ctx;
  const S = ctx.S, key = optsKey(S);
  const song = ctx.song();
  if (song && (ctx.D.evsongs || []).some(s => s.music_id === S.song && s.chart_available === false)) { toast("This song has no chart at that difficulty."); return; }
  ctx.setBusy(true);
  const mine = run = { t0: performance.now(), ctx };
  if (pc) pc.stop();
  pc = null;
  const A = eventAssumptions(S);
  const replan = !!A && A.boards === "plan" && (!A.infinite || A.max_resets != null || A.branches !== "GBRY" || A.targets.length > 0);
  // Highlight: no re-plan, no certification; the search on a sample, its best re-scored (event_mode.py)
  const hl = isHighlight(S.song);
  let order = hl ? ["build", "event_search", "rescore", ...(!A || A.boards !== "as_is" ? ["refine"] : [])]
    : certify && !replan ? ["build", "score_search", "event_search", "bound"]
    : ["build", "event_search", ...(replan ? ["replan"] : !A || A.boards !== "as_is" ? ["refine"] : []), "detail"];
  progress(ctx, order, "build", "");
  // the end of this search, once (an answer, an error, or Cancel): a stale stream is ignored
  const done = (answered = false) => {
    if (run !== mine) return false;
    if (pc) { if (answered) pc.done(); else pc.stop(); }
    pc = null; run = null; ctx.setBusy(false);
    return true;
  };
  mine.finish = () => done(false);
  let started;
  try {
    started = await postJSON("/api/event/run", { objective: S.goal, song: S.song, difficulty: S.difficulty,
      boosts: S.boosts, holopass: !!S.holopass, top: S.evtop, certify, basis: isMaxed(S) ? "maxed" : "account",
      ...(A ? { assumptions: A } : {}), ...eventFilters(S),
      platform: S.platform, auto: !!S.auto });
  } catch (e) { ctx.out().innerHTML = `<div class="o-error">The event search could not start: ${esc(e.message)}</div>`; done(); return; }
  if (run !== mine) { postJSON(`/cancel/${encodeURIComponent(started.job_id)}`, {}).catch(() => {}); return; }
  run.job = started.job_id;
  // the stages this search runs (the server's plan lists every stage it knows)
  const es = mine.es = new EventSource(`/events/${encodeURIComponent(run.job)}`);
  let finished = false;
  es.onmessage = m => {
    if (run !== mine) { es.close(); return; }                // cancelled from the page
    let ev; try { ev = JSON.parse(m.data); } catch (_) { return; }
    if (ev.type === "stage" || ev.type === "progress") progress(ctx, order, ev.stage, ev.evaluated ? `${fmt(ev.evaluated)} teams scored` : "", ev);
    else if (ev.type === "result") {
      finished = true;
      done(true);
      live = { key, res: ev.payload, cards: ctx.D.cards }; details.clear();
      open = new Set([0]); sel = 0; remember(ctx); render(ctx, live);
    } else if (ev.type === "error") { finished = true; ctx.out().innerHTML = `<div class="o-error">The event search failed: ${esc(ev.message || "")}</div>`; }
    else if (ev.type === "cancelled") { finished = true; toast("Search cancelled."); showEventIdle(ctx); }
    else if (ev.type === "end") { es.close(); if (!finished) ctx.out().innerHTML = `<div class="o-error">The search stopped without a result.</div>`; done(); }
  };
  es.onerror = () => { if (!finished && run === mine) { es.close(); ctx.out().innerHTML = `<div class="o-error">Lost the connection to the server.</div>`; done(); } };
}

/* ------------------------------------------------------------------ results */
// The same layout as the Score view (opt-score.js): the ranked teams on the left (hb-rank.js),
// the selected team on the right with the same tiles, card strip, outfit and detail panels.
function headline(S, r) { return isEP(S) ? r.event_points : (r.event_song_score ?? r.objective); }
const headFmt = (S, v) => isEP(S) ? `<span class="n o-num">${fmt(v)}</span>` : score(v);
function render(ctx, L) {
  const S = ctx.S, res = L.res, rows = (res.rows || []).slice(0, S.evtop);
  if (!rows.length) { ctx.out().innerHTML = `<div class="o-error">The search returned no team.</div>`; return; }
  const c = { ctx, res, rows, S };
  loadStates(rows.flatMap(r => [r.leader.id, ...r.members.map(m => m.id)]), isMaxed(S) ? MAXED_LEVERS : eventAssumptions(S))
    .then(changed => { if (changed && live === L) render(ctx, L); });
  if (sel >= rows.length) sel = 0;
  open = new Set([sel]);
  const ep = isEP(S);
  ctx.out().innerHTML = `<div class="o-split">
    <aside class="dlg o-setlist" aria-label="Ranked teams">
      <div class="dlg-h"><h2>Search results</h2><span class="r">${rows.length} teams · ${ep ? "Event Pt per Live" : "Event Song Score"}</span></div>
      <div class="o-listctl" role="group" aria-label="List view">
        <button class="chip" type="button" data-diffmode aria-pressed="${diffMode}" title="Outline the cards that differ from #1">Diff vs #1</button></div>
      <div class="o-list" id="o-list">${listHTML(c)}</div>
      <p class="o-runinfo">${esc([`Event search · ${res.basis || "account"}`, res.highlight ? `Highlight · mean over ${(res.highlight.pool || []).length} songs` : "", `${fmt(res.pool)} cards in the pool`, res.elapsed_s != null ? `${res.elapsed_s.toFixed(1)} s` : ""].filter(Boolean).join(" · "))}</p>
    </aside>
    <section class="o-sel" aria-label="Selected team"><article class="o-team" data-open="true" id="team${sel}">${block(c, rows[sel], sel)}</article></section>
  </div>`;
  wakeFigure(ctx.out());
  ctx.out().onclick = e => {
    const rr = e.target.closest(".rk-row[data-i]"); if (rr) return select(c, +rr.dataset.i);
    if (e.target.closest("[data-diffmode]")) { diffMode = !diffMode; return relist(c); }
    const cp = e.target.closest("[data-copy]"); if (cp) return copy(c, +cp.dataset.copy);
  };
  ctx.out().oninput = e => {
    const m = e.target.closest("[data-multi]"); if (!m) return;
    if (m.dataset.own != null) ownScore = m.value; else multiScore = m.value;
    clearTimeout(multiTimer);
    multiTimer = setTimeout(() => fillMulti(c, rows[+m.dataset.multi], +m.dataset.multi), 250);
  };
  reveal(ctx.out());
  after(c, rows[sel], sel);
  if (multiScore) fillMulti(c, rows[sel], sel);
}
/* the Multi Live tile's Event Pt: /api/event/points with the room's MULTI SCORE and the
   player's own score (the Event Pt of a played Live needs the score actually made, not
   this team's expected one; empty -> the expected). The server holds the formula, checked
   on real multi Lives. No room score -> a dash */
async function fillMulti(c, r, i) {
  const out = document.querySelector(`[data-multi-out="${i}"]`);
  if (!out || !r) return;
  const room = parseInt(String(multiScore).replace(/\D/g, ""), 10);
  if (!room) { out.textContent = "—"; return; }
  const own = parseInt(String(ownScore).replace(/\D/g, ""), 10) || Math.round(r.score);
  const asked = multiScore + "|" + ownScore;
  try {
    // the event shown: its kind sets the room term's divisor (`marathon.multi_divisor`)
    const d = await postJSON("/api/event/points", { score: own, live_permil: r.live_permil,
      event: (c.ctx.D.event && c.ctx.D.event.event && c.ctx.D.event.event.key) || undefined,
      multi_score: room, boosts: c.S.boosts, length_permil: c.res.length_permil, holopass: !!c.S.holopass });
    if (asked === multiScore + "|" + ownScore) out.textContent = fmt(d.event_points);
  } catch (e) { out.textContent = "—"; toast(e.message); }
}
function listHTML({ S, rows }) {
  return rankList(rows.map(r => ({ score: headline(S, r), max: null,
    leader: { id: r.leader.id, name: r.leader.member, outfit: r.leader.costume_name || "" },
    cards: [r.leader.id, ...r.members.map(m => m.id)] })),
  { selected: sel, diff: diffMode, thumb: id => thumb(id), format: v => headFmt(S, v) });
}
function relist(c) {
  const l = byId("o-list"); if (l) l.innerHTML = listHTML(c);
  c.ctx.out().querySelectorAll("[data-diffmode]").forEach(b => b.setAttribute("aria-pressed", diffMode));
}
function select(c, i) {
  if (!c.rows[i]) return;
  sel = i; open = new Set([i]);
  remember(c.ctx);
  const el = c.ctx.out().querySelector(".o-sel > .o-team"); if (!el) return;
  el.id = "team" + i; el.innerHTML = block(c, c.rows[i], i);
  wakeFigure(el);
  c.ctx.out().querySelectorAll(".rk-row[data-i]").forEach(b => b.setAttribute("aria-selected", +b.dataset.i === i));
  if (el.getBoundingClientRect().top < 0) el.scrollIntoView({ block: "start", behavior: "smooth" });
  reveal(el);
  after(c, c.rows[i], i);
  if (multiScore) fillMulti(c, c.rows[i], i);
}
function copy({ ctx, S, rows }, i) {
  const r = rows[i], song = ctx.song ? ctx.song() : null;
  const name = m => `${m.costume_name || m.id} (${m.member || ""})`;
  const order = r.member_order && r.member_order.length === 5 ? r.member_order.map(id => r.members.find(m => m.id === id)).filter(Boolean) : r.members;
  const txt = [`${isHighlight(S.song) ? "Highlight" : song ? song.title : S.song} ·${isEP(S) ? "Event Pt" : "Event Song Score"} team #${i + 1} · ${fmt(headline(S, r))}`,
    `Leader: ${name(r.leader)}`, ...order.map((m, k) => `${k + 1}. ${name(m)}`)].join("\n");
  navigator.clipboard?.writeText(txt).then(() => toast("Team copied"), () => toast("Couldn't copy"));
}
function perf(c, k, S, lead, slot) {
  const card = withState(normCard(c));
  const bonus = isEP(S) ? card.live_permil : card.song_permil;
  const tag = lead ? `<span class="xb">Leader · no ${isEP(S) ? "Event Bonus" : "Song Score Bonus"}</span>`
    : `<span class="xb">${bonus ? `<span class="eb">${pct(bonus)}</span> ${isEP(S) ? "EB" : "Song Score Bonus"}` : `Lv ${card.level ?? "?"}${card.bloom ? ` · Bloom ${card.bloom}` : ""}`}</span>`;
  return `<figure class="o-perf" style="--i:${k}">
    ${window.HBCard.face(card, { lead, roleText: "Leader", w: 400 })}
    <figcaption><span class="slot" aria-label="${slot ? "Slot " + slot : ""}">${slot || ""}</span><b>${esc(card.costume_name || card.id)}</b><span class="mb">${esc(card.member)}</span>${tag}</figcaption>
  </figure>`;
}
const keyOf = r => r.leader.id + "|" + r.members.map(m => m.id).sort().join(",");
function block({ ctx, S, res, rows }, r, i) {
  const ep = isEP(S), v = headline(S, r);
  const det = details.get(keyOf(r)) || {};
  const delta = rows.length < 2 ? "" : i === 0
    ? `${sgn((v / headline(S, rows[1]) - 1) * 100)} over #2`
    : `${sgn((v / headline(S, rows[0]) - 1) * 100)} vs #1`;
  // Highlight (res.highlight): one score is the MEAN over the pool, L is a 170 s song's; the
  // detail below is the team on ONE pool song, chosen in the row (as In general on Score)
  const hl = res.highlight || null, nPool = hl ? (hl.pool || []).length : 0;
  const onPool = !!hl && det.song === HL_POOL;
  const lenPct = res.length_permil != null ? `+${Math.round((res.length_permil - 1000) / 10)}%` : null;
  const unit = r.score_bonus && r.score_bonus.unit_score != null ? r.score_bonus.unit_score : r.unit_score;
  const tile = (lbl, val, small) => `<div class="stat-tile"><div><span class="lbl">${lbl}</span>${val}<small>${small || ""}</small></div></div>`;
  const side = [tile("Live score", score(r.score), hl ? `mean over ${nPool} song${nPool === 1 ? "" : "s"}` : "this team's board"),
    tile(ep ? "Event Bonus" : "Song Score Bonus", `<span class="n o-num eb">${pct(ep ? r.live_permil : r.song_permil)}</span>`, ep ? "the five members" : "one card is enough; no stacking")];
  if (r.song_max != null) side.push(tile(`Max ${info("max")}`, score(r.song_max), "every Active fires"));
  if (unit != null) side.push(tile(`Unit Score ${info("grade")}`, score(unit), "this team's board"));
  if (ep && lenPct) side.push(hl ? tile("Highlight Bonus", `<span class="n o-num">${lenPct}</span>`, "in place of the song length bonus")
    : tile("Song length bonus", `<span class="n o-num">${lenPct}</span>`, "estimate"));
  // Multi Live: the player types the room's MULTI SCORE and their own score (empty: this
  // team's Expected Score), the server answers the Event Pt. Its own block under the tiles,
  // every field labelled (as a tile it was unclear and its text was cut off)
  const multi = ep && res.length_permil != null ? `<div class="o-mcalc" role="group" aria-labelledby="mcalc${i}">
      <h4 id="mcalc${i}">Multi Live Event Pt calculator</h4>
      <label><span>Room MULTI SCORE</span><input class="hbc-in" inputmode="numeric" autocomplete="off" placeholder="e.g. 9,283,806" data-multi="${i}" value="${esc(multiScore)}"></label>
      <span class="o-mcalc-op" aria-hidden="true">+</span>
      <label><span>Your score in that Live</span><input class="hbc-in" inputmode="numeric" autocomplete="off" placeholder="${esc(fmt(Math.round(r.score)))}" data-multi="${i}" data-own="" value="${esc(ownScore)}"></label>
      <span class="o-mcalc-op" aria-hidden="true">=</span>
      <div class="o-mcalc-out"><span>Event Pt</span><b class="o-num" data-multi-out="${i}">—</b></div>
      <p>MULTI SCORE is the room's total at the top of the multi result screen. Leave your score empty to use this team's expected score (${esc(fmt(Math.round(r.score)))}). Counted with this team's Event Bonus (${pct(r.live_permil)}), ${S.boosts} Live Boost${S.boosts > 1 ? "s" : ""}${S.holopass ? ", HoloPass" : ", no HoloPass"} and the ${hl ? "Highlight Bonus" : "song length bonus"} (${lenPct || "—"}).</p>
    </div>` : "";
  const L = { ...(ctx.D.cards.get(r.leader.id) || {}), ...r.leader };
  const head = `<div class="o-thead">
      <div class="o-who"><span class="rk-n">${i + 1}</span><h2>${esc(L.member || r.leader.id)} <small>leads${L.costume_name ? " · " + esc(L.costume_name) : ""}</small></h2></div>
      <div class="o-tact"><button class="btn small" type="button" data-copy="${i}"><svg class="i s"><use href="#i-copy"/></svg>Copy team</button></div>
    </div>`;
  const tiles = `<div class="o-tiles">
      <div class="stat-tile prime"><div><span class="lbl">${ep ? `Event Pt per Live · ${S.boosts} Live Boost${S.boosts > 1 ? "s" : ""} (×${AC[S.boosts - 1]})${S.holopass ? " · HoloPass" : ""}` : "Event Song Score"} ${info(ep ? "ev_points" : "ev_song")}</span>${headFmt(S, v)}<small>${delta}</small></div></div>
      <div class="o-sidetiles">${side.join("")}</div>
    </div>`;
  // slot order: the row's member_order (or its detail's), never the server's listing order
  const byCard = new Map(r.members.map(m => [m.id, m]));
  const order = (det.fig && det.fig.member_order) || r.member_order;
  const ordered = Array.isArray(order) && order.length === 5 && order.every(id => byCard.has(id));
  const members = ordered ? order.map(id => byCard.get(id)) : r.members;
  // the leader is the figure on the left (leaderColumn); the strip holds the five members
  const cards = `<div class="o-stage team-strip o-members">${members.map((m, k) => perf(m, k + 1, S, false, ordered ? k + 1 : "")).join("")}</div>
    ${hl ? `<p class="o-ordernote">${ordered ? (onPool ? `One slot order for the whole pool: the best single order over its ${nPool} songs.` : `Slot order on ${esc(poolTitle(hl, det.song))}; it varies by song.`) : det.fig || det.err ? "Slot order not served for this team." : onPool ? "Choosing one board and one slot order for the whole pool (a few seconds)…" : "Reading the slot order…"}</p>`
    : ordered ? "" : `<p class="o-ordernote">${det.fig || det.err ? "Slot order not served for this team." : "Reading the slot order…"}</p>`}`;
  const outfit = L.costume_name ? `<div class="o-outfit"><svg class="i s"><use href="#i-shirt"/></svg><span>Outfit Skill · <b>${esc(L.costume_name)}</b></span>
    ${L.costume_skill || L.costume_text ? `<span aria-hidden="true">·</span><span class="fx">${esc(outfitText(L.costume_skill, L.costume_text))}</span>` : ""}</div>` : "";
  const cert = i === 0 ? (hl ? `<div class="o-cert warn"><svg class="i s"><use href="#i-info"/></svg><span>The best team the search found, not proven best: the bound check is per song and does not run on Highlight.</span></div>` : certBlock(res, ep)) : "";
  const notes = i === 0 && !hl && (res.notes || []).length ? `<ul class="o-notes">${res.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>` : "";
  const pool = i === 0 && hl ? poolHTML(hl, `at ${res.difficulty || S.difficulty}, read by the search`) : "";
  const src = r.source === "replanned" ? `<p class="o-cap">Boards re-planned under your board options.</p>`
    : r.source && r.source !== "event_search" ? `<p class="o-cap">Found by the ${r.source === "bound" ? "bound check" : "score search"}.</p>` : "";
  const T = hl ? { ...detailRow(r), expected: det.fig ? det.fig.value : null, max: det.fig ? det.fig.song_max ?? null : null } : detailRow(r);
  const shown = { ...det, fig: det.fig || null };
  const detErr = r.detail_error ? `<p class="o-cap">The search could not re-develop this row: ${esc(r.detail_error)}</p>` : "";
  // Highlight: the team on one song of the pool, its board rebuilt for that song
  const perSong = hl ? `<div class="o-sect o-persong">
      <h3><label for="hlsong${i}">On</label> <select id="hlsong${i}" data-hlsong="${i}"><option value="${HL_POOL}"${onPool ? " selected" : ""}>The whole pool</option>${(hl.pool || []).map(s => `<option value="${esc(s.music_id)}"${s.music_id === det.song ? " selected" : ""}>${esc(s.title || s.music_id)}</option>`).join("")}</select></h3>
      ${onPool ? poolSummary(r, det, nPool)
      : `<p>This team on one song of the Highlight pool, its Holomem Board rebuilt for that song. The figures above are the mean over ${nPool} songs, each on its own board and slot order.</p>`}</div>` : "";
  const plan = (eventAssumptions(S) || {}).boards === "plan" ? boardPanel(T, shown, wallet, i === 0, id => ctx.D.cards.get(id) || null) : "";
  const panels = onPool
    ? `<div class="o-sect"><div class="o-duo">${unitPanel(T, shown)}${poolSongs(hl, det)}</div>${detErr}</div>` + plan
    : `<div class="o-sect"><div class="o-duo">${unitPanel(T, shown)}${runPanel(T, shown)}</div>${detErr}</div>` +
      timelinePanel(T, shown, id => ctx.D.cards.get(id) || r.members.find(m => m.id === id) || null) + plan;
  const body = hl ? `<section class="o-onsong">${perSong}${panels}</section>` : panels;
  const unitBlock = `<div class="o-unit">${leaderColumn(L, { outfit, note: ep ? "no Event Bonus" : "no Song Score Bonus" })}
    <div class="o-unit-main">${tiles}${multi}${hl ? "" : matchLine(T, det)}${cards}</div></div>`;
  return head + unitBlock + (cert || notes || src || pool ? `<div class="o-sect o-reveal">${cert}${src}${pool}${notes}</div>` : "") + body;
}
/* the whole pool's line under the selector: the mean at one board and one order against the
   search's mean (each song on its own board and order), and its Event Pt per Live */
function poolSummary(r, det, n) {
  const f = det.fig && det.fig.pool;
  if (det.err) return `<p>Unavailable: ${esc(det.err)}</p>`;
  if (!f) return `<p>One Holomem Board and one slot order for all ${n} songs: the song is drawn after the team is set. Choosing them…</p>`;
  const d = (f.song_est / r.score - 1) * 100;
  return `<p>One Holomem Board and one slot order for all ${n} songs, since the song is drawn after the team is set.
    Mean score <b class="num">${fmt(Math.round(f.song_est))}</b>${det.fig.ep != null ? ` · <b class="num">${fmt(det.fig.ep)}</b> Event Pt per Live` : ""},
    <span class="num">${sgn(d)}</span> against the ranking's ${fmt(Math.round(r.score))}, which gives every song its own board and order.
    Board: ${esc(f.board_from)}.</p>`;
}
/* the whole pool's card, beside the Unit Score one (a song table would be too
   tall): the score on each pool song at the one board and order, a bar per song sorted low to
   high (each song is drawn 1 time in N), its quantiles, the mean and the spread across songs;
   the per-song list folds away. The in-song Active roll is not in it: that law is per song. */
function poolSongs(hl, det) {
  const f = det.fig && det.fig.pool;
  if (!f) return `<div class="o-card o-pending o-reveal"><span class="o-sk" style="width:100%;height:120px"></span></div>`;
  const rows = [...f.per_song].sort((a, b) => a.score - b.score), n = rows.length;
  const v = rows.map(s => s.score), lo = v[0], hi = v[n - 1], mean = f.song_est;
  const at = q => v[Math.min(n - 1, Math.max(0, Math.round(q * (n - 1))))];
  const sd = Math.sqrt(v.reduce((a, x) => a + (x - mean) ** 2, 0) / n);
  const own = f.own_order_mean ? (mean / f.own_order_mean - 1) * 100 : 0;
  // bars from 12 % (the lowest song) to 100 % (the highest), the mean as the Expected mark
  const H = x => 12 + (hi > lo ? (x - lo) / (hi - lo) : 1) * 88;
  const bars = rows.map((s, k) => `<i style="left:${(k / n * 100).toFixed(3)}%;width:${(100 / n).toFixed(3)}%;height:${H(s.score).toFixed(1)}%" data-tip="${esc(poolTitle(hl, s.music_id))}|${fmt(Math.round(s.score))}|${s.score >= mean ? "+" : ""}${dec((s.score / mean - 1) * 100, 1)}% vs the mean"></i>`).join("");
  const q = [["Worst", lo], ["25%", at(0.25)], ["Median", at(0.5)], ["75%", at(0.75)], ["Best", hi]];
  const list = [...rows].reverse().map(s => {
    const loss = s.own_order_best > 0 ? (s.score / s.own_order_best - 1) * 100 : 0;
    return `<tr><td>${esc(poolTitle(hl, s.music_id))}</td><td class="num">${fmt(Math.round(s.score))}</td><td class="num">${Math.abs(loss) < 0.005 ? "same" : sgn(-loss)}</td></tr>`;
  }).join("");
  return `<div class="o-card o-reveal o-poolsongs">
    <div class="o-sh"><h3>Score by song drawn</h3><span class="right"><span class="o-preview">each song 1 in ${n}</span></span></div>
    <div class="o-law" role="img" aria-label="Score on each of the ${n} pool songs, ${fmt(Math.round(lo))} to ${fmt(Math.round(hi))}">
      <div class="bars">${bars}</div>
      <div class="ax"><span>${esc(poolTitle(hl, rows[0].music_id))}</span><span>${esc(poolTitle(hl, rows[n - 1].music_id))}</span></div>
    </div>
    <div class="o-stats o-qs">${q.map(([lab, x]) => `<div><span>${lab}</span><b>${fmt(Math.round(x))}</b></div>`).join("")}</div>
    <div class="o-stats o-s3"><div><span>Expected</span><b>${score(mean, 7)}</b></div><div title="Standard deviation of the score across the pool's songs"><span>Spread (sd)</span><b>±${fmt(Math.round(sd))}</b></div><div title="What each song's own best slot order would add on the same board"><span>Own order per song</span><b>${sgn(-own)}</b></div></div>
    <details class="o-poolall"><summary>Every song</summary>
      <table><thead><tr><th>Song</th><th>Score</th><th>Own order</th></tr></thead><tbody>${list}</tbody></table></details>
  </div>`;
}
// the row as the detail sections read it: the event search's live score is the Expected Score
const detailRow = r => ({ key: keyOf(r), source: "event", route: "the event search", api: "/api/event/run", kind: "song", boards: "develop",
  expected: r.score, max: r.song_max ?? null, plan: null });

/* the detail for an open row: /api/row-detail on the event leaf (plain: refine false), the
   same leaf the event search scored the row on; the Score Bonus split comes with the row */
function repaint(c, r, i) {
  const el = byId("team" + i);
  if (!el || !open.has(i) || !live || live.res !== c.res) return;
  const planOpen = el.querySelector("details.o-plan");
  const keepPlan = planOpen ? planOpen.open : i === 0;
  // the detail repaints the row: a player typing in the Multi Live box keeps it
  const typing = document.activeElement && el.contains(document.activeElement) && document.activeElement.matches("[data-multi]");
  el.innerHTML = block(c, r, i);
  wakeFigure(el);
  const p = el.querySelector("details.o-plan"); if (p) p.open = keepPlan;
  el.querySelectorAll(".o-reveal").forEach(x => x.classList.add("in"));
  const box = typing && el.querySelector(document.activeElement.dataset.own != null ? "[data-own]" : "[data-multi]:not([data-own])");
  if (box) { box.focus(); box.setSelectionRange(box.value.length, box.value.length); }
  if (multiScore) fillMulti(c, r, i);
  const cardOf = id => c.ctx.D.cards.get(id) || r.members.find(m => m.id === id) || null;
  wireTimeline(el, details.get(keyOf(r)) || {}, cardOf);
  wirePlan(el, detailRow(r), details.get(keyOf(r)) || {}, [r.leader, ...r.members].map(m => "chr-" + String(m.id).split("-")[1]));
  const hs = el.querySelector("[data-hlsong]");
  if (hs) hs.onchange = () => {
    const det = details.get(keyOf(r));
    if (!det) return;
    Object.assign(det, { song: hs.value, fig: null, tl: null, tlErr: null, doc: null, docErr: null, acct: null, acctErr: null, err: null });
    repaint(c, r, i);
    songDetail(c, r, i, det);
  };
}
async function after(c, r, i) {
  const key = keyOf(r);
  // a cached answer is reused; a cached FAILURE is not (a request cut as a long search ends
  // -- "NetworkError" -- would otherwise leave the row "Unavailable" for good): opening the
  // row again retries it
  if (details.has(key) && !details.get(key).err) { repaint(c, r, i); return; }
  const det = {}; details.set(key, det);
  if (!wallet) getJSON("/api/resources").then(w => { wallet = w; repaint(c, r, i); }).catch(() => { wallet = {}; });
  // Highlight: no single song -- the detail opens on the WHOLE pool (one board, one order);
  // the row's selector shows the team on any one pool song instead
  if (c.res.highlight) det.song = HL_POOL;
  await songDetail(c, r, i, det);
}
/* the row's figures on one song: the search's song, or (Highlight) the pool song det.song */
async function songDetail(c, r, i, det) {
  const S = c.S, cards = [r.leader.id, ...r.members.map(m => m.id)];
  const hl = !!c.res.highlight, song = hl ? det.song : S.song;
  if (!song) { det.err = "no song to show"; repaint(c, r, i); return; }
  const stale = () => hl && det.song !== song;
  try {
    // the figures first; the run distribution (~5 s more) after them
    // a Highlight row's member_order is the search's, scored over the pool: the song's own is read
    const onPool = hl && song === HL_POOL, poolIds = hl ? (c.res.highlight.pool || []).map(s => s.music_id) : [];
    const body = { song: onPool ? poolIds[0] : song, difficulty: S.difficulty, basis: isMaxed(S) ? "maxed" : "account", cards,
      refine: !!r.refined, assumptions: eventAssumptions(S) || undefined, member_order: hl ? undefined : r.member_order || undefined };
    // the whole pool: one board and one slot order for every song (`row_detail.pool_detail`)
    const d = await postJSON("/api/row-detail", onPool ? { ...body, refine: undefined, pool: poolIds } : { ...body, timeline: true, distribution: false });
    if (stale()) return;
    det.fig = { route: onPool ? "one board and one slot order for the whole pool" : "the team's own board", api: "/api/row-detail", value: d.song_est, song_max: d.song_max, member_order: d.member_order, deck_power: d.deck_power,
      deck_power_basis: d.deck_power_basis, panel: d.panel, score_bonus: d.score_bonus || r.score_bonus, distribution: d.distribution || null,
      board_state: d.board_state || r.board_state, bench_slots: d.bench_slots || r.bench_slots || null, pool: onPool ? d : null };
    if (onPool) {
      // its Event Pt per Live: the server's formula on the pool mean (as the Multi Live box)
      await postJSON("/api/event/points", { score: Math.round(d.song_est), live_permil: r.live_permil,
        event: (c.ctx.D.event && c.ctx.D.event.event && c.ctx.D.event.event.key) || undefined,
        boosts: S.boosts, length_permil: c.res.length_permil, holopass: !!S.holopass })
        .then(x => { det.fig.ep = x.event_points; }).catch(() => {});
    } else if (d.timeline) det.tl = { timeline: d.timeline }; else det.tlErr = "not served";
    repaint(c, r, i);
    const bs = det.fig.board_state;
    if (bs && Object.keys(bs).length) {
      await postJSON("/api/board-doc", isMaxed(S) ? { board_state: bs, cards, basis: "maxed", budget: 453, complete: true }
        : { board_state: bs, cards, basis: "account" })
        .then(x => { det.doc = x; }).catch(e => { det.docErr = e.message; });
      // the whole account's plan when the boards are planned (see opt-score.js boardDoc)
      const A = eventAssumptions(S);
      if (A && A.boards === "plan" && !det.acct) {
        det.acctLoading = true; repaint(c, r, i);
        await postJSON("/api/board-plan-account", { cards, song: onPool ? (d.plan_song || undefined) : song, song_pool: onPool ? (d.plan_pool || undefined) : undefined, assumptions: A, board_state: bs, bench_slots: det.fig.bench_slots })
          .then(x => { det.acct = x; }).catch(e => { det.acctErr = e.message; });
        det.acctLoading = false;
      }
    }
    if (stale()) return;
    if (!onPool) await withDistribution(det, body, () => repaint(c, r, i));
  } catch (e) { if (!stale()) det.err = e.message; }
  if (!stale()) repaint(c, r, i);
}
function certBlock(res, ep) {
  const b = res.bound; if (!b) return "";
  const label = ep ? "Event Bonus" : "Song Score Bonus";
  const thr = b.threshold ?? b.e_min ?? b.p_min;
  return b.certified
    ? `<div class="o-cert"><svg class="i s"><use href="#i-check"/></svg><span><b>Checked.</b> A team that beats this needs more than ${pct(Math.max(0, thr))} ${label}. ${b.sets_above ? `All ${fmt(b.sets_above)} such member sets were scored exactly under ${(b.leaders_checked || []).length} leaders, and none beats it.` : "No member set reaches that."} This holds if no team scores above ${fmt(b.s_max)} and if the best leader is among the ones tried.</span></div>`
    : b.requested === false
    ? `<div class="o-cert warn"><svg class="i s"><use href="#i-info"/></svg><span>The best team the search found, not yet proven best. The proof scores every member set that could still beat it (about half a minute).</span> <button class="btn small" type="button" data-certify>Prove it</button></div>`
    : `<div class="o-cert warn"><svg class="i s"><use href="#i-info"/></svg><span>Not proven best: ${b.overflow ? `more than ${fmt(b.certify_limit)} member sets could still beat it, too many to check exactly` : "the bound check did not finish"}. This is the best team the search found.</span></div>`;
}
