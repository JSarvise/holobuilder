// Optimizer page, goals "Event Points" and "Event Song Score": the event search job
// (POST /api/event/run, SSE /events/<id>, POST /cancel/<id>; contract in tools/web/EVENT-API.md).
// Every number is the server's: Event Pt, live score, Event Bonus, the bound.
import { byId, esc, fmt, dec, sgn, gap, pct, info, AC, INK, thumb, getJSON, postJSON, toast, reveal, normCard, outfitText, stash, unstash, withState, loadStates, cardHook } from "/opt-util.js";
import { unitPanel, runPanel, timelinePanel, wireTimeline, boardPanel, matchLine, leaderColumn, wakeFigure, wirePlan, deckUnit } from "/opt-detail.js";
import { score } from "/hb-grade.js";
import { list as rankList } from "/hb-rank.js";
import { assumptionsOf, leaderOpts, withDistribution, exportFile } from "/opt-score.js";
import { progressCard } from "/opt-progress.js";

let live = null, run = null, open = new Set([0]);
let shownC = null;   // the rows laid out now (render), for a redraw after a refresh
let sel = 0, diffMode = true;   // the selected row on the right; the list outlines cards not in #1
const details = new Map();   // row key -> {fig, tl, doc, err…}: /api/row-detail on the event leaf (plain, refine:false)
let wallet = null;
let multiScore = "";   // the room's MULTI SCORE the player typed (one room, every row)
let ownScore = "";     // the player's own score in that Live (empty: the row's Expected Score)
let multiTimer = 0;
const STAGES = { build: "Loading your account", score_search: "Searching by score",
  event_search: "Searching by the event objective", bound: "Checking that nothing beats it",
  replan: "Re-planning the best teams' boards", refine: "Choosing the activation nodes for this song",
  ceiling: "Every team's best run (Max)", split: "Each team without FEVER", detail: "Reading the best teams' boards" };
const HL_STAGES = { build: "Loading your account and the Highlight pool",
  event_search: "Searching by the event objective on a sample of the pool", rescore: "Scoring the best teams on every song of the pool",
  refine: "Choosing the activation nodes per song", split: "Each team without FEVER, over the pool" };
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
// Multi Live: Event Song Score ranked by the MULTI SCORE contribution (score + 1.5 x fever),
// Event Points by the Event Pt of a multi Live (event_mode.py, multi_points)
const multiOn = S => (S.goal === "event_song" || S.goal === "event_points") && !!S.multi;
const optsKey = S => JSON.stringify([S.goal, S.song, S.difficulty, isHighlight(S.song) ? S.rlevel : 0, S.boosts, S.goal === "event_points" ? S.holopass : 0,
  S.platform, !!S.auto, eventAssumptions(S), eventFilters(S), S.chapter || "", multiOn(S)]);
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
// a song's chance in the draw (`event_mode.pool_weights`), when the run says
const drawPct = s => s.weight != null ? `${dec(s.weight * 100, 1)}%` : "";
function poolHTML(hl, from) {
  const pool = (hl && hl.pool) || [], skipped = (hl && hl.skipped) || [];
  if (!pool.length && !skipped.length) return "";
  const nEv = pool.filter(s => s.event_song).length;
  const draw = hl.event_share != null && nEv
    ? `<p class="o-cap">Draw: the ${nEv} event song${nEv === 1 ? "" : "s"} ${Math.round(hl.event_share * 100)}% of the time, the other ${pool.length - nEv} the rest, equally (an estimate until more draws are counted).</p>` : "";
  return `<details class="o-hlpool"><summary>Highlight pool: ${pool.length} song${pool.length === 1 ? "" : "s"}${nEv && hl.event_share != null ? ` · ${nEv} event song${nEv === 1 ? "" : "s"}` : ""}${skipped.length ? ` · ${skipped.length} skipped` : ""} <small>${esc(from)}</small></summary>
    ${draw}
    <ul>${pool.map(s => `<li><img src="/jacket/${encodeURIComponent(s.music_id)}" alt="" loading="lazy"><span><b>${esc(s.title)}</b><small>${esc(s.singer || "")}${hl.max_level && s.difficulty ? ` · ${esc(s.difficulty[0] + s.difficulty.slice(1).toLowerCase())} ${s.level ?? ""}` : ""}${s.event_song && hl.event_share != null ? ` · event song` : ""}${drawPct(s) ? ` · ${drawPct(s)}` : ""}</small></span></li>`).join("")}</ul>
    ${skipped.length ? `<p class="o-cap">Skipped (no chart ${hl.max_level ? "up to the level" : "at this difficulty"} on disk): ${skipped.map(s => esc(s.title || s.music_id)).join(", ")}.</p>` : ""}
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
    <h2>${isEP(S) ? "Most Event Pt per Live" : multiOn(S) ? "Most MULTI SCORE contribution" : "Best Event Song Score"} on this song</h2>
    <p>${isEP(S)
      ? `Event Bonus counts the five members, never the leader. This event’s holomems: ${mems.map(m => `<b>${esc(m.name)}</b> <span class="eb">${pct(m.permil)}</span>`).join(", ") || "none"}.`
      : songBonus.length ? `On this song, ${songBonus.map(m => `<b>${esc(m.card ? `${m.card.member} / ${m.card.costume_name}` : m.name || "")}</b> <span class="eb">${pct(m.permil)}</span>`).join(", ")} ${songBonus.length > 1 ? "lift the score when any one of them is among the five members. One is enough: more do not stack" : "lifts the score when it is one of the five members"}; the leader and the singers’ other cards do not count.`
      : "This song carries no Song Score Bonus in the current event: the best team is the best-scoring one."}
    Press Find best team; the search takes about 15 seconds. <a class="btn small" href="/event" style="margin-left:6px">Event details</a></p></section>`;
}

/* ------------------------------------------------------------------ search */
// Cancel answers at once: the page drops the search and goes back to idle; the server is
// told too and stops at its next check
// after a refresh: the details and the wallet are read again (opt-score.js forgetDetails)
export function forgetEventDetails() { details.clear(); wallet = null; }
// a refresh just ended: the selected team's details on screen are computed again
export function redrawEventDetails() {
  forgetEventDetails();
  const c = shownC;
  if (c && c.rows[sel] && c.ctx.out().querySelector(".o-sel > .o-team")) select(c, sel);
}
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
  // a stage announces its total before its first step: none done yet
  const done = e.total ? e.done ?? 0 : e.done;
  pc.update({ active: k >= 0 ? k : undefined, detail: detail || (e.total ? `${done} / ${e.total}` : ""),
    done, total: e.total });
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
  const split = multiOn(S) ? ["split"] : [];
  let order = hl ? ["build", "event_search", "rescore", ...(!A || A.boards !== "as_is" ? ["refine"] : []), ...split]
    : certify && !replan ? ["build", "score_search", "event_search", "bound"]
    : ["build", "event_search", ...(replan ? ["replan"] : !A || A.boards !== "as_is" ? ["refine"] : []),
      ...(S.goal === "event_song" ? ["ceiling"] : []), ...split, "detail"];
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
      // the chapter planned for, when it is not the running one (opt-app.js, S.chapter)
      ...(S.chapter ? { event: S.chapter } : {}),
      platform: S.platform, auto: !!S.auto, multi: multiOn(S),
      // Highlight: each pool song at its hardest chart up to the level
      ...(isHighlight(S.song) ? { max_level: S.rlevel } : {}) });
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
      live = { key, res: ev.payload, cards: ctx.D.cards }; details.clear(); wallet = null;  // a refresh since the last search changed the cubes and hexas the plan was made within
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
// Event Song Score sorted by Max (the Options' Sort by): every row reads its Max Event Song Score,
// the score if every Active fires, when the server served one for every row (one song, not Highlight)
const evMax = (S, rows) => !isEP(S) && S.evsort === "max" && !isHighlight(S.song)
  && rows.length > 0 && rows.every(r => typeof r.event_song_score_max === "number");
function headline(S, r, max = false) { return isEP(S) ? r.event_points : max ? r.event_song_score_max : (r.event_song_score ?? r.objective); }
const headFmt = (S, v) => isEP(S) ? `<span class="n o-num">${fmt(v)}</span>` : score(v);
function render(ctx, L) {
  const S = ctx.S, res = L.res, rows = (res.rows || []).slice(0, S.evtop);
  const byMax = evMax(S, rows);
  if (byMax) rows.sort((a, b) => b.event_song_score_max - a.event_song_score_max);
  if (!rows.length) { ctx.out().innerHTML = `<div class="o-error">The search returned no team.</div>`; return; }
  const c = shownC = { ctx, res, rows, S, byMax };
  loadStates(rows.flatMap(r => [r.leader.id, ...r.members.map(m => m.id)]), isMaxed(S) ? MAXED_LEVERS : eventAssumptions(S))
    .then(changed => { if (changed && live === L) render(ctx, L); });
  if (sel >= rows.length) sel = 0;
  open = new Set([sel]);
  const ep = isEP(S);
  ctx.out().innerHTML = `<div class="o-split">
    <aside class="dlg o-setlist" aria-label="Ranked teams">
      <div class="dlg-h"><h2>Search results</h2><span class="r">${rows.length} teams · ${ep ? "Event Pt per Live" : res.multi ? "MULTI SCORE contribution" : byMax ? "Event Song Score · Max" : "Event Song Score"}</span></div>
      <div class="o-listctl" role="group" aria-label="List view">
        <button class="chip" type="button" data-diffmode aria-pressed="${diffMode}" title="Outline the cards that differ from #1">Diff vs #1</button></div>
      <div class="o-list" id="o-list">${listHTML(c)}</div>
      <p class="o-runinfo">${esc([`Event search · ${res.basis || "account"}`, res.highlight ? `Highlight · ${res.highlight.event_share != null ? "draw-weighted " : ""}mean over ${(res.highlight.pool || []).length} songs` : "", `${fmt(res.pool)} cards in the pool`, res.elapsed_s != null ? `${res.elapsed_s.toFixed(1)} s` : ""].filter(Boolean).join(" · "))}${window.hbExportButton ? window.hbExportButton() : ""}</p>
    </aside>
    <section class="o-sel" aria-label="Selected team"><article class="o-team" data-open="true" id="team${sel}">${block(c, rows[sel], sel)}</article></section>
  </div>`;
  wakeFigure(ctx.out());
  ctx.out().onclick = e => {
    const rr = e.target.closest(".rk-row[data-i]"); if (rr) return select(c, +rr.dataset.i);
    if (e.target.closest("[data-diffmode]")) { diffMode = !diffMode; return relist(c); }
    const cp = e.target.closest("[data-copy]"); if (cp) return copy(c, +cp.dataset.copy);
    const ex = e.target.closest("[data-export]"); if (ex) return exportTeam(c, +ex.dataset.export, ex);
    const mc = e.target.closest("[data-multicalc]"); if (mc) return openMulti(c, +mc.dataset.multicalc);
  };
  reveal(ctx.out());
  after(c, rows[sel], sel);
  if (multiScore) fillMulti(c, rows[sel], sel);
}
/* the Multi Live tile's Event Pt: /api/event/points with the room's MULTI SCORE and the
   player's own score (the Event Pt of a played Live needs the score actually made, not
   this team's expected one; empty -> the expected). The server holds the formula, checked
   on real multi Lives. No room score -> a dash */
// the player's own score in a Live: the row's score, or in Multi Live (FEVER) its score without
// the fever weight (the row is ranked on the weighted one)
const ownOf = r => Math.round(r.song_est_solo ?? r.score);
async function fillMulti(c, r, i) {
  // the row's tile and, while it is open, the dialog's result: both carry data-multi-out
  const outs = document.querySelectorAll(`[data-multi-out="${i}"]`);
  if (!outs.length || !r) return;
  const put = v => outs.forEach(o => { o.textContent = v; });
  const room = parseInt(String(multiScore).replace(/\D/g, ""), 10);
  const own = parseInt(String(ownScore).replace(/\D/g, ""), 10) || ownOf(r);
  document.querySelectorAll(`[data-multi-sub="${i}"]`).forEach(s => {
    s.textContent = room ? `room ${fmt(room)} · you ${fmt(own)}` : "type the room's MULTI SCORE";
  });
  if (!room) { put("—"); return; }
  const asked = multiScore + "|" + ownScore;
  try {
    // the event shown: its kind sets the room term's divisor (`marathon.multi_divisor`)
    const d = await postJSON("/api/event/points", { score: own, live_permil: r.live_permil,
      event: (c.ctx.D.event && c.ctx.D.event.event && c.ctx.D.event.event.key) || undefined,
      multi_score: room, boosts: c.S.boosts, length_permil: c.res.length_permil, holopass: !!c.S.holopass });
    if (asked === multiScore + "|" + ownScore) put(fmt(d.event_points));
  } catch (e) { put("—"); toast(e.message); }
}
/* the Multi Live tile in the row's figures: its last Event Pt (or a dash) and what it was
   counted from; a click opens the calculator */
function multiTile(r, i) {
  return `<button type="button" class="stat-tile o-mtile" data-multicalc="${i}" aria-haspopup="dialog">
      <div><span class="lbl">Multi Live Event Pt</span><span class="n o-num" data-multi-out="${i}">—</span>
        <small data-multi-sub="${i}">type the room's MULTI SCORE</small></div>
      <span class="o-mtile-go"><svg class="i s"><use href="#i-chev"/></svg></span></button>`;
}
/* the calculator: the room's MULTI SCORE + the player's score in that Live (empty: this team's
   Expected Score) = the Event Pt, counted by the server with this team's Event Bonus, the Live
   Boosts, HoloPass and the length bonus; the result shows here and in the row's tile */
function openMulti(c, i) {
  const dlg = byId("multidlg"), r = c.rows[i];
  if (!dlg || !r) return;
  const { S, res } = c, hl = !!res.highlight;
  const L = { ...(c.ctx.D.cards.get(r.leader.id) || {}), ...r.leader };
  const exp = fmt(ownOf(r));
  const lenPct = res.length_permil != null ? `+${Math.round((res.length_permil - 1000) / 10)}%` : "—";
  const terms = [`Event Bonus <b>${pct(r.live_permil)}</b>`, `${S.boosts} Live Boost${S.boosts > 1 ? "s" : ""} <b>×${AC[S.boosts - 1]}</b>`,
    S.holopass ? "HoloPass" : "no HoloPass", `${hl ? "Highlight Bonus" : "Song length bonus"} <b>${lenPct}</b>`];
  dlg.innerHTML = `<div class="dlg-h"><h2 id="mdlgtitle">Multi Live Event Pt</h2>
      <span class="r">Team #${i + 1} · ${esc(L.member || r.leader.id)} leads</span>
      <button class="o-x" type="button" data-close aria-label="Close"><svg class="i"><use href="#i-x"/></svg></button></div>
    <div class="dlg-b o-mdlg-b">
      <div class="o-mdlg-eq">
        <label><span class="lbl">Room MULTI SCORE</span><input class="hbc-in" inputmode="numeric" autocomplete="off" placeholder="e.g. 9,283,806" data-dmulti value="${esc(multiScore)}"></label>
        <span class="o-mdlg-op" aria-hidden="true">+</span>
        <label><span class="lbl">Your score in that Live</span><input class="hbc-in" inputmode="numeric" autocomplete="off" placeholder="${esc(exp)}" data-dmulti data-own value="${esc(ownScore)}"></label>
      </div>
      <div class="o-mdlg-out"><span class="lbl">Event Pt</span><b class="o-num" data-multi-out="${i}">—</b></div>
      <div class="o-mdlg-terms">${terms.map(t => `<span class="tag muted">${t}</span>`).join("")}</div>
      <p class="o-mdlg-help">MULTI SCORE is the room's total at the top of the multi result screen. Leave your score empty to use this team's expected score (${esc(exp)}).</p>
    </div>`;
  dlg.oninput = e => {
    const m = e.target.closest("[data-dmulti]"); if (!m) return;
    if (m.dataset.own != null) ownScore = m.value; else multiScore = m.value;
    clearTimeout(multiTimer);
    multiTimer = setTimeout(() => fillMulti(c, r, i), 250);
  };
  // a click on the backdrop (the dialog element itself, outside its content) closes it too
  dlg.onclick = e => { if (e.target === dlg || e.target.closest("[data-close]")) dlg.close(); };
  dlg.showModal();
  const first = dlg.querySelector("[data-dmulti]");
  first.focus(); first.select();
  fillMulti(c, r, i);
}
function listHTML({ S, rows, byMax }) {
  return rankList(rows.map(r => ({ score: headline(S, r, byMax), max: null,
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
/* the row as a .hbteam file: the detail's own request (the song open in a Highlight row) */
function exportTeam({ ctx, S, rows, byMax }, i, btn) {
  const r = rows[i], song = ctx.song ? ctx.song() : null, det = details.get(keyOf(r));
  if (!det || !det.body) return toast("Wait for the team's details to load, then export");
  const shown = { song: isHighlight(S.song) ? `Highlight (detail on ${det.song})` : song ? song.title : S.song,
    mode: isEP(S) ? "Event Pt" : multiOn(S) ? "MULTI SCORE contribution" : byMax ? "Event Song Score (Max)" : "Event Song Score", rank: i + 1, headline: headline(S, r, byMax),
    score: r.score ?? null, boosts: S.boosts, holopass: !!S.holopass };
  return exportFile(det.body, shown, null, btn);
}
function copy({ ctx, S, rows, byMax }, i) {
  const r = rows[i], song = ctx.song ? ctx.song() : null;
  const name = m => `${m.costume_name || m.id} (${m.member || ""})`;
  const order = r.member_order && r.member_order.length === 5 ? r.member_order.map(id => r.members.find(m => m.id === id)).filter(Boolean) : r.members;
  const txt = [`${isHighlight(S.song) ? "Highlight" : song ? song.title : S.song} ·${isEP(S) ? "Event Pt" : byMax ? "Event Song Score (Max)" : "Event Song Score"} team #${i + 1} · ${fmt(headline(S, r, byMax))}`,
    `Leader: ${name(r.leader)}`, ...order.map((m, k) => `${k + 1}. ${name(m)}`)].join("\n");
  navigator.clipboard?.writeText(txt).then(() => toast("Team copied"), () => toast("Couldn't copy"));
}
function perf(c, k, S, lead, slot) {
  const card = withState(normCard(c));
  const bonus = isEP(S) ? card.live_permil : card.song_permil;
  const tag = lead ? `<span class="xb">Leader · no ${isEP(S) ? "Event Bonus" : "Song Score Bonus"}</span>`
    : `<span class="xb">${bonus ? `<span class="eb">${pct(bonus)}</span> ${isEP(S) ? "EB" : "Song Score Bonus"}` : `Lv ${card.level ?? "?"}${card.bloom ? ` · Bloom ${card.bloom}` : ""}`}</span>`;
  return `<figure class="o-perf" style="--i:${k}" ${cardHook(card)}>
    ${window.HBCard.face(card, { lead, roleText: "Leader", w: 400 })}
    <figcaption><span class="slot" aria-label="${slot ? "Slot " + slot : ""}">${slot || ""}</span><b>${esc(card.costume_name || card.id)}</b><span class="mb">${esc(card.member)}</span>${tag}</figcaption>
  </figure>`;
}
const keyOf = r => r.leader.id + "|" + r.members.map(m => m.id).sort().join(",");
function block({ ctx, S, res, rows, byMax }, r, i) {
  const ep = isEP(S), v = headline(S, r, byMax);
  const det = details.get(keyOf(r)) || {};
  const delta = rows.length < 2 ? "" : i === 0
    ? `${gap((v / headline(S, rows[1], byMax) - 1) * 100)} over #2`
    : `${gap((v / headline(S, rows[0], byMax) - 1) * 100)} vs #1`;
  // Highlight (res.highlight): one score is the MEAN over the pool, L is a 170 s song's; the
  // detail below is the team on ONE pool song, chosen in the row (as In general on Score)
  const hl = res.highlight || null, nPool = hl ? (hl.pool || []).length : 0;
  const onPool = !!hl && det.song === HL_POOL;
  const lenPct = res.length_permil != null ? `+${Math.round((res.length_permil - 1000) / 10)}%` : null;
  // a row past the ones the job developed gets these from its own detail (the same board)
  const own = !hl && det.fig ? det.fig : null;
  const unit = r.score_bonus && r.score_bonus.unit_score != null ? r.score_bonus.unit_score : r.unit_score
    ?? (own && own.score_bonus && own.score_bonus.unit_score != null ? own.score_bonus.unit_score : own && own.panel ? own.panel.unit_score : null);
  const rmax = r.song_max ?? (own ? own.song_max : null);
  const tile = (lbl, val, small) => `<div class="stat-tile"><div><span class="lbl">${lbl}</span>${val}<small>${small || ""}</small></div></div>`;
  const pend = `<span class="o-sk" style="width:150px;height:26px;margin-top:6px"></span>`;
  const meanOf = hl ? `${hl.event_share != null ? "draw-weighted " : ""}mean over ${nPool} song${nPool === 1 ? "" : "s"}` : "";
  // a Multi Live row's own score at its order and board (`row_split`; a row past the served ones
  // from its detail), a Highlight row's as the mean over the pool
  const solo = r.song_est_solo ?? (!hl && det.fig ? det.fig.solo : null);
  const soloTile = () => tile("without FEVER", solo != null ? score(solo) : det.err || hl ? `<span class="n">—</span>` : `<span class="o-sk" style="width:150px;height:26px;margin-top:6px"></span>`, meanOf || "this team's own score");
  const mp = res.multi_points;
  const contrib = r.multi_score ?? (res.multi && !hl && det.fig ? det.fig.value : null);
  const fever = contrib != null && solo != null ? contrib - solo : null;
  const side = res.multi
    // a MULTI SCORE contribution: no song bonus (the event record's only)
    ? [soloTile(),
      // what the fever adds to the room (1.5 x the fever section's score): the contribution less
      // the team's own score (no song bonus: the event record's only)
      tile("FEVER bonus", fever != null ? score(fever) : det.err || hl ? `<span class="n">—</span>` : pend, hl ? meanOf : "1.5 × your fever section")]
    : mp
    // Event Points in Multi Live: the row's score is weighted by the fever; its contribution and
    // its own score are what a player reads
    ? [tile(`MULTI SCORE contribution ${info("multi")}`, r.multi_score != null ? score(r.multi_score) : `<span class="n">—</span>`, meanOf || "your part of the room's MULTI SCORE"),
      soloTile(),
      tile("Event Bonus", `<span class="n o-num eb">${pct(r.live_permil)}</span>`, "the five members")]
    : [tile("Live score", score(r.score), meanOf || "this team's board"),
      tile(ep ? "Event Bonus" : "Song Score Bonus", `<span class="n o-num eb">${pct(ep ? r.live_permil : r.song_permil)}</span>`, ep ? "the five members" : "one card is enough; no stacking")];
  // Max and Unit Score keep their slots while the row's detail fills them in (a skeleton), so the
  // grid does not move; a Highlight row's pool has no single run, so no Max
  const none = (lbl, why) => `<div class="stat-tile none"><div><span class="lbl">${lbl}</span><span class="n">—</span><small>${why}</small></div></div>`;
  // Event Pt with a length bonus: the Multi Live tile under the big Event Pt, which opens the
  // calculator (the view keeps Score's height); on a Highlight pool, with Event Pt, the Max a
  // pool cannot have is left out so the side figures fit two rows
  const multiOn = ep && res.length_permil != null;
  if (!(hl && multiOn) && !mp) side.push(rmax != null ? tile(`Max ${info("max")}`, score(rmax), hl ? `${meanOf}, all fire` : "every Active fires")
    : hl ? none(`Max ${info("max")}`, "a pool has no single run") : tile(`Max ${info("max")}`, det.err ? `<span class="n">—</span>` : pend, "every Active fires"));
  // a Highlight row has no Unit Score of its own (each pool song on its own board): the one of
  // the board its detail shows, once that is in
  const hlUnit = hl && unit == null && det.fig ? deckUnit(det.fig) : null;
  side.push(unit != null ? tile(`Unit Score ${info("grade")}`, score(unit), "this team's board")
    : hlUnit != null ? tile(`Unit Score ${info("grade")}`, score(hlUnit), onPool ? "the pool's one board" : "this song's board")
    : tile(`Unit Score ${info("grade")}`, det.err || (hl && det.fig) ? `<span class="n">—</span>` : pend, "this team's board"));
  // Event Points in Multi Live: the room the Event Pt are read with
  if (ep && lenPct) side.push(hl ? tile("Highlight Bonus", `<span class="n o-num">${lenPct}</span>`, "in place of the song length bonus")
    : tile("Song length bonus", `<span class="n o-num">${lenPct}</span>`, "estimate"));
  const L = { ...(ctx.D.cards.get(r.leader.id) || {}), ...r.leader };
  const head = `<div class="o-thead">
      <div class="o-who"><span class="rk-n">${i + 1}</span><h2>${esc(L.member || r.leader.id)} <small>leads${L.costume_name ? " · " + esc(L.costume_name) : ""}</small></h2></div>
      <div class="o-tact"><button class="btn small" type="button" data-copy="${i}"><svg class="i s"><use href="#i-copy"/></svg>Copy team</button>
        <button class="btn small" type="button" data-export="${i}" title="Save this team as a .hbteam file: the row, its numbers and the account data its score uses, so it can be replayed and checked"><svg class="i s"><use href="#i-down"/></svg>Export</button></div>
    </div>`;
  const tiles = `<div class="o-tiles">
      <div class="o-primecol"><div class="stat-tile prime"><div><span class="lbl">${ep ? `Event Pt per Live${mp ? " · solo" : ""} · ${S.boosts} Live Boost${S.boosts > 1 ? "s" : ""} (×${AC[S.boosts - 1]})${S.holopass ? " · HoloPass" : ""}` : res.multi ? (byMax ? "MULTI SCORE contribution · Max" : "MULTI SCORE contribution") : byMax ? "Event Song Score · Max" : "Event Song Score"} ${info(ep ? "ev_points" : res.multi ? "multi" : byMax ? "max" : "ev_song")}</span>${headFmt(S, v)}<small>${byMax ? `every Active fires · Expected ${fmt(r.event_song_score)}${delta ? " · " + delta : ""}` : delta}</small></div></div>
        ${multiOn ? multiTile(r, i) : ""}</div>
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
  const pool = i === 0 && hl ? poolHTML(hl, `${res.max_level ? `each at its hardest chart up to level ${res.max_level}` : `at ${res.difficulty || S.difficulty}`}, read by the search`) : "";
  const src = r.source === "replanned" ? `<p class="o-cap">Boards re-planned under your board options.</p>`
    : r.source && r.source !== "event_search" ? `<p class="o-cap">Found by the ${r.source === "bound" ? "bound check" : "score search"}.</p>` : "";
  const T = hl ? { ...detailRow(r), expected: det.fig ? det.fig.value : null, max: det.fig ? det.fig.song_max ?? null : null } : detailRow(r);
  const shown = { ...det, fig: det.fig || null };
  const detErr = r.detail_error ? `<p class="o-cap">The search could not re-develop this row: ${esc(r.detail_error)}</p>` : "";
  // Highlight: the team on one song of the pool, its board rebuilt for that song
  const perSong = hl ? `<div class="o-sect o-persong">
      <h3><label for="hlsong${i}">On</label> <select id="hlsong${i}" data-hlsong="${i}"><option value="${HL_POOL}"${onPool ? " selected" : ""}>The whole pool</option>${(hl.pool || []).map(s => `<option value="${esc(s.music_id)}"${s.music_id === det.song ? " selected" : ""}>${esc(s.title || s.music_id)}</option>`).join("")}</select></h3>
      ${onPool ? poolSummary(r, det, nPool)
      : `<p>This team on one song of the Highlight pool, its Holomem Board rebuilt for that song. The figures above are the ${hl.event_share != null ? "draw-weighted " : ""}mean over ${nPool} songs, each on its own board and slot order.</p>`}</div>` : "";
  const plan = (eventAssumptions(S) || {}).boards === "plan" ? boardPanel(T, shown, wallet, i === 0, id => ctx.D.cards.get(id) || null) : "";
  const panels = onPool
    ? `<div class="o-sect"><div class="o-duo">${unitPanel(T, shown)}${poolSongs(hl, det)}</div>${detErr}</div>` + plan
    : `<div class="o-sect"><div class="o-duo">${unitPanel(T, shown)}${runPanel(T, shown)}</div>${detErr}</div>` +
      timelinePanel(T, shown, id => ctx.D.cards.get(id) || r.members.find(m => m.id === id) || null) + plan;
  const body = hl ? `<section class="o-onsong">${perSong}${panels}</section>` : panels;
  const unitBlock = `<div class="o-unit">${leaderColumn(L, { outfit, note: ep ? "no Event Bonus" : "no Song Score Bonus" })}
    <div class="o-unit-main">${tiles}${hl ? "" : matchLine(T, det)}${cards}</div></div>`;
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
    ${f.song_est_solo != null ? `Mean MULTI SCORE contribution <b class="num">${fmt(Math.round(f.multi_score ?? f.song_est))}</b> (without FEVER <b class="num">${fmt(Math.round(f.song_est_solo))}</b>)${det.fig.ep != null ? ` · <b class="num">${fmt(det.fig.ep)}</b> Event Pt per Live` : ""}`
      : `Mean score <b class="num">${fmt(Math.round(f.song_est))}</b>${det.fig.ep != null ? ` · <b class="num">${fmt(det.fig.ep)}</b> Event Pt per Live` : ""}`},
    <span class="num">${sgn(d)}</span> against the ranking's ${fmt(Math.round(r.score))}, which gives every song its own board and order.
    Board: ${esc(f.board_from)}.</p>`;
}
/* the whole pool's card, beside the Unit Score one (a song table would be too
   tall): the score on each pool song at the one board and order, a bar per song sorted low to
   high, each as wide as its chance in the draw (the event songs come up more often), its quantiles, the mean and the spread across songs;
   the per-song list folds away. The in-song Active roll is not in it: that law is per song. */
function poolSongs(hl, det) {
  const f = det.fig && det.fig.pool;
  if (!f) return `<div class="o-card o-pending o-reveal"><span class="o-sk" style="width:100%;height:120px"></span></div>`;
  const rows = [...f.per_song].sort((a, b) => a.score - b.score), n = rows.length;
  // each song's chance in the draw (`row_detail.pool_detail`'s weights; 1 in n without them)
  const w = rows.map(s => s.weight ?? 1 / n), weighted = rows.some(s => s.weight != null && Math.abs(s.weight - 1 / n) > 1e-9);
  const v = rows.map(s => s.score), lo = v[0], hi = v[n - 1], mean = f.song_est;
  // the draw's quantiles: the first song whose cumulative chance reaches q
  const cum = []; w.reduce((a, x, k) => (cum[k] = a + x), 0);
  const at = q => { const k = cum.findIndex(c => c >= q - 1e-9); return v[k < 0 ? n - 1 : k]; };
  const sd = Math.sqrt(v.reduce((a, x, k) => a + w[k] * (x - mean) ** 2, 0));
  const own = f.own_order_mean ? (mean / f.own_order_mean - 1) * 100 : 0;
  // bars from 12 % (the lowest song) to 100 % (the highest), each as wide as its chance
  const H = x => 12 + (hi > lo ? (x - lo) / (hi - lo) : 1) * 88;
  const left = k => (k ? cum[k - 1] : 0) * 100;
  const bars = rows.map((s, k) => `<i style="left:${left(k).toFixed(3)}%;width:${(w[k] * 100).toFixed(3)}%;height:${H(s.score).toFixed(1)}%" data-tip="${esc(poolTitle(hl, s.music_id))}${weighted ? ` (${dec(w[k] * 100, 1)}% of draws)` : ""}|${fmt(Math.round(s.score))}|${s.score >= mean ? "+" : ""}${dec((s.score / mean - 1) * 100, 1)}% vs the mean"></i>`).join("");
  const q = [["Worst", lo], ["25%", at(0.25)], ["Median", at(0.5)], ["75%", at(0.75)], ["Best", hi]];
  const list = [...rows].reverse().map(s => {
    const loss = s.own_order_best > 0 ? (s.score / s.own_order_best - 1) * 100 : 0;
    return `<tr><td>${esc(poolTitle(hl, s.music_id))}</td>${weighted ? `<td class="num">${dec((s.weight ?? 1 / n) * 100, 1)}%</td>` : ""}<td class="num">${fmt(Math.round(s.score))}</td><td class="num">${Math.abs(loss) < 0.005 ? "same" : sgn(-loss)}</td></tr>`;
  }).join("");
  return `<div class="o-card o-reveal o-poolsongs">
    <div class="o-sh"><h3>Score by song drawn</h3><span class="right"><span class="o-preview">${weighted ? "event songs drawn more often" : `each song 1 in ${n}`}</span></span></div>
    <div class="o-law" role="img" aria-label="Score on each of the ${n} pool songs, ${fmt(Math.round(lo))} to ${fmt(Math.round(hi))}">
      <div class="bars">${bars}</div>
      <div class="ax"><span>${esc(poolTitle(hl, rows[0].music_id))}</span><span>${esc(poolTitle(hl, rows[n - 1].music_id))}</span></div>
    </div>
    <div class="o-stats o-qs">${q.map(([lab, x]) => `<div><span>${lab}</span><b>${fmt(Math.round(x))}</b></div>`).join("")}</div>
    <div class="o-stats o-s3"><div><span>Expected</span><b>${score(mean, 7)}</b></div><div title="Standard deviation of the score across the pool's songs"><span>Spread (sd)</span><b>±${fmt(Math.round(sd))}</b></div><div title="What each song's own best slot order would add on the same board"><span>Own order per song</span><b>${sgn(-own)}</b></div></div>
    <details class="o-poolall"><summary>Every song</summary>
      <table><thead><tr><th>Song</th>${weighted ? "<th>Drawn</th>" : ""}<th>Score</th><th>Own order</th></tr></thead><tbody>${list}</tbody></table></details>
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
  el.innerHTML = block(c, r, i);
  wakeFigure(el);
  const p = el.querySelector("details.o-plan"); if (p) p.open = keepPlan;
  el.querySelectorAll(".o-reveal").forEach(x => x.classList.add("in"));
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
    // the songs the draw favours (the run's own weights: `event_mode.pool_weights`)
    const poolEvent = hl && c.res.highlight.event_share != null ? (c.res.highlight.pool || []).filter(s => s.event_song).map(s => s.music_id) : [];
    // a pool song's own chart (the search's pick under the level), the nominal Expert for the pool
    const own = hl && !onPool ? (c.res.highlight.pool || []).find(s => s.music_id === song) : null;
    const body = { song: onPool ? poolIds[0] : song, difficulty: (own && own.difficulty) || S.difficulty, basis: isMaxed(S) ? "maxed" : "account", cards,
      platform: S.platform, auto: !!S.auto, multi: multiOn(S),   // the search's play, or the detail is not the row's
      ...(c.res.multi_points ? { fever_weight: c.res.multi_points.fever_weight } : {}),
      refine: !!r.refined, assumptions: eventAssumptions(S) || undefined, member_order: hl ? undefined : r.member_order || undefined };
    // the whole pool: one board and one slot order for every song (`row_detail.pool_detail`)
    const poolBody = { ...body, refine: undefined, pool: poolIds, ...(poolEvent.length ? { pool_event: poolEvent } : {}),
      ...(c.res.max_level ? { max_level: c.res.max_level } : {}) };
    det.body = onPool ? poolBody : body;
    const d = await postJSON("/api/row-detail", onPool ? poolBody : { ...body, timeline: true, distribution: false });
    if (stale()) return;
    det.fig = { route: onPool ? "one board and one slot order for the whole pool" : "the team's own board", api: "/api/row-detail", value: d.song_est, song_max: d.song_max, member_order: d.member_order, deck_power: d.deck_power,
      solo: d.song_est_solo ?? null,
      deck_power_basis: d.deck_power_basis, panel: d.panel, score_bonus: d.score_bonus || r.score_bonus, distribution: d.distribution || null,
      board_state: d.board_state || r.board_state, bench_slots: d.bench_slots || r.bench_slots || null, pool: onPool ? d : null };
    if (onPool) {
      // its Event Pt per Live: the server's formula on the pool mean (as the Multi Live box)
      // in Multi Live (FEVER) the pool mean is weighted by the fever: a solo Live's points are on
      // the score without it
      const mp = c.res.multi_points;
      await postJSON("/api/event/points", { score: Math.round(mp && d.song_est_solo != null ? d.song_est_solo : d.song_est), live_permil: r.live_permil,
        event: (c.ctx.D.event && c.ctx.D.event.event && c.ctx.D.event.event.key) || undefined,
        boosts: S.boosts, length_permil: c.res.length_permil, holopass: !!S.holopass })
        .then(x => { det.fig.ep = x.event_points; }).catch(() => {});
    } else if (d.timeline) det.tl = { timeline: d.timeline }; else det.tlErr = "not served";
    repaint(c, r, i);
    const bs = det.fig.board_state;
    if (bs && Object.keys(bs).length) {
      await postJSON("/api/board-doc", isMaxed(S) ? { board_state: bs, cards, basis: "maxed", budget: "max", complete: true }
        : { board_state: bs, cards, basis: "account" })
        .then(x => { if (!stale()) det.doc = x; }).catch(e => { if (!stale()) det.docErr = e.message; });
      // the whole account's plan when the boards are planned (see opt-score.js boardDoc)
      const A = eventAssumptions(S);
      if (A && A.boards === "plan" && !det.acct) {
        det.acctLoading = true; repaint(c, r, i);
        await postJSON("/api/board-plan-account", { cards, song: onPool ? (d.plan_song || undefined) : song, song_pool: onPool ? (d.plan_pool || undefined) : undefined, assumptions: A, board_state: bs, bench_slots: det.fig.bench_slots })
          // a plan still running for the previous pool song no longer applies
          .then(x => { if (!stale()) det.acct = x; }).catch(e => { if (!stale()) det.acctErr = e.message; });
        if (!stale()) det.acctLoading = false;
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
