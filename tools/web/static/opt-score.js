// Optimizer page, goal "Score": the cached answer (GET /api/two-stage), the live search
// (POST /solve + SSE, then POST /api/optimize-rerank — the classic page's default path), and the
// results laid out as the Night Deck setlist (hb-rank.js, left) and the selected team (right).
// Scores are the server's.
import { byId, esc, fmt, sgn, info, thumb, getJSON, postJSON, toast, reveal, outfitText, NOTES, when, stash, unstash, withState, loadStates } from "/opt-util.js";
import { score } from "/hb-grade.js";
import { list as rankList } from "/hb-rank.js";
import { progressCard, countLine } from "/opt-progress.js";
import { unitPanel, runPanel, timelinePanel, wireTimeline, boardPanel, matchLine, deckUnit, leaderColumn, wakeFigure, wirePlan } from "/opt-detail.js";

let live = null;      // {key, rows, cards, info} — the last live answer
let cache = null;     // {key, doc, rows, cards}
let run = null;       // {abort, job, t0, timer}
let open = new Set([0]);   // always {sel}: the one team shown on the right
let sel = 0, diffMode = true, onePer = false;
const details = new Map();   // team key -> detail {dev, cur, tl, doc, err…}
let wallet = null;

const basisOf = S => S.cards === "maxed" ? "maxed" : "account";
// the server basis of a team's numbers (two_stage_build.BASES); null = a what-if the
// on-demand search does not cover (upgraded cards at a level other than Max, or a bloom)
const tsBasis = S => {
  if (basisOf(S) !== "account") return "maxed";
  if (S.cards === "whatif") {
    if (S.alevel !== "max" || S.abloom !== "account") return null;
    return S.maxpoints ? "account-levelmax-max" : "account-levelmax";
  }
  return S.maxpoints ? "account-max" : "account";
};
function optsKey(S) {
  return JSON.stringify([S.song, S.difficulty, S.cards, S.boards, S.maxpoints, S.infmats, S.pool, S.platform, S.auto, S.exact,
    S.cards === "whatif" ? [S.alevel, S.abloom, S.aspt, !!S.keephigher] : 0, S.maxresets, S.resetgain,
    S.allinvited, S.norefund, S.branches, JSON.stringify(S.targets), S.lvmode === "range" ? [S.lvlo, S.lvhi] : 0, S.lmember, S.loutfit, S.nooverlap, S.require, S.exclude]);
}
const teamKey = (leader, members) => leader + "|" + [...members].sort().join(",");
function common(S) {
  const b = {
    basis: basisOf(S), difficulty: S.difficulty, platform: S.platform, auto: !!S.auto, uptime_model: "finite",
    assume_level: S.cards === "whatif" ? S.alevel : "account", assume_bloom: S.cards === "whatif" ? S.abloom : "account",
    // the older paths' leader pin: the outfit chosen in the leader filter
    pool: S.pool, require: S.require, leader: S.lmember && S.loutfit ? [S.loutfit] : [], exclude: S.exclude,
  };
  return b;
}
export const leaderOpts = S => {
  const o = {};
  if (S.lmember) o.leader_member = S.lmember;
  if (S.lmember && S.loutfit) o.leader_card = S.loutfit;
  if (S.nooverlap) o.no_leader_overlap = true;
  return o;
};

/* ------------------------------------------------------------------ the answer kept across pages */
// the last live answer and the last on-demand search come back when the page is opened again
// (cards travel as the response's own subset and are merged onto the catalogue again)
let restored = false;
function remember(ctx) {
  const bare = r => r && { ...r, cards: undefined };
  stash("score", ctx, { live: bare(live), cache: cache && String(cache.url).startsWith("search:") ? bare(cache) : null, sel });
}
function restore(ctx) {
  restored = true;
  const k = unstash("score", ctx); if (!k) return;
  if (k.live) live = { ...k.live, cards: ctx.mergeCards(k.live.raw) };
  if (k.cache) cache = { ...k.cache, cards: ctx.D.cards };
  sel = k.sel || 0; open = new Set([sel]);
}

/* ------------------------------------------------------------------ idle: the cached answer */
export async function showScoreIdle(ctx, keepLive) {
  if (!restored) { restore(ctx); keepLive = true; }
  const S = ctx.S, key = optsKey(S);
  if (live && live.key === key) { render(ctx, live); return; }
  if (!keepLive) { sel = 0; open = new Set([0]); }
  // the optimiser ON DEMAND (POST /api/search) -- searched for this request, no cache --
  // for the account and maxed-account bases; otherwise the cached answer is read.
  // A search starts ONLY from Find best team -- never on load or on an option change
  if (assumptionsOf(S) || basisOf(S) === "maxed") { idleOnDemand(ctx, key); return; }
  const want = `/api/two-stage?song=${encodeURIComponent(S.song)}&basis=${tsBasis(S) || "account"}`;
  if (cache && cache.url === want) { render(ctx, cache); return; }
  ctx.out().innerHTML = `<div class="o-progress"><div class="row"><span>Reading the cached answer…</span></div></div>`;
  try {
    const doc = await getJSON(want);
    if (optsKey(ctx.S) !== key) return;               // the options moved on
    // the GLOBAL top N (rows may share a leader); older files: one row per leader
    const src = Array.isArray(doc.ranking) && doc.ranking.length ? doc.ranking : (doc.rows || []);
    const rows = src.map(r => ({
      key: teamKey(r.leader, r.members), source: "cached", route: "the cached search", api: "/api/two-stage",
      leader: r.leader, leaderName: r.leader_name, members: r.member_order || r.members, ordered: !!r.member_order,
      expected: r.best, kind: doc.scope === "general" ? "across" : "song", sings: r.sings, status: r.status, refinedLeaf: r.refined,
      p10: r.p10, p50: r.p50, p90: r.p90, max: r.song_max ?? null,
    }));
    cache = { url: want, doc, rows, cards: ctx.D.cards, cached: true };
    render(ctx, cache);
  } catch (e) {
    ctx.out().innerHTML = `<section class="o-empty"><h2>No cached answer</h2><p>${esc(e.message)}. Press Find best team to search.</p></section>`;
  }
}

/* ------------------------------------------------------------------ on demand: POST /api/search */
let od = null;        // {key, job, es} — the on-demand search in flight
// the account levers of the on-demand search (ondemand.Assumptions), or null for a
// basis that is not the user's account (every card maxed / a maxed account)
export function assumptionsOf(S) {
  if (basisOf(S) !== "account") return null;
  const whatif = S.cards === "whatif";
  const plan = S.boards === "develop";
  return {
    level: whatif ? S.alevel : "account", bloom: whatif ? S.abloom : "account", keep_higher: whatif && !!S.keephigher,
    sp_training: whatif ? S.aspt : "account",
    all_invited: plan && !!S.allinvited, no_refund: plan && !S.infmats && !!S.norefund,
    branches: plan ? S.branches : "GBRY", targets: plan ? S.targets : [],
    boards: plan ? "plan" : "as_is", max_points: plan && !!S.maxpoints, infinite: plan && !!S.infmats,
    max_resets: plan && !S.infmats && S.maxresets !== "-1" ? +S.maxresets : null,
    reset_min_gain: plan && !S.infmats ? +S.resetgain : 0,
  };
}
function odBody(S) {
  const a = assumptionsOf(S);
  // how the run is played: Platform and Auto must reach the on-demand search, or every
  // request is scored as a manual run on mobile
  const b = { song: S.song, basis: tsBasis(S) || "account", difficulty: S.difficulty, platform: S.platform,
    auto: !!S.auto, ...leaderOpts(S) };
  if (a) b.assumptions = a;
  if (!S.song && S.lvmode === "range") b.levels = [+S.lvlo, +S.lvhi];
  if (S.require.length) b.require = S.require;
  if (S.exclude.length) b.exclude = S.exclude;
  // Card pool "5★ only": the five members; the leader slot stays any rarity
  if (S.pool === "5star") b.pool = "5star";
  return b;
}
// idle with on-demand options: the last result when it is for these options, else a
// prompt; a search still running for OTHER options no longer applies and is cancelled
function idleOnDemand(ctx, key) {
  if (cache && cache.url === "search:" + key) { render(ctx, cache); return; }
  if (od && od.key === key) return;                 // already searching these options
  cancelOnDemand();
  ctx.out().innerHTML = `<section class="o-empty"><h2>Ready to search</h2><p>Set the options, then press <b>Find best team</b>. With a song the search takes about 15-40 seconds.</p></section>`;
}
function cancelOnDemand() {
  if (!od) return;
  if (od.es) od.es.close();
  if (od.job) fetch("/cancel/" + od.job, { method: "POST" }).catch(() => {});
  od = null;
}
async function searchNow(ctx, key) {
  const url = "search:" + key;
  if (cache && cache.url === url) { render(ctx, cache); return; }
  if (od && od.key === key) return;                 // already searching these options
  cancelOnDemand();
  const mine = od = { key, ctx, t0: performance.now() };
  newCard();
  // the third stage as the server runs it (`ondemand.Assumptions.replan` / `Engine.search`)
  const A = assumptionsOf(ctx.S);
  const replan = !!A && A.boards === "plan" && (!A.infinite || A.max_resets != null || A.branches !== "GBRY" || A.targets.length > 0);
  const asIs = !!A && A.boards === "as_is";
  const third = !ctx.S.song ? "Scoring the best teams on every song"
    : replan ? "Planning the best teams within your cubes and hexas"
    : asIs ? "Scoring the best teams on your boards as they are"
    : "Choosing the activation nodes for this song";
  const steps = ["Loading your account",
    ctx.S.song ? "Searching every leader and team" : "Searching every leader and team on a sample of songs", third];
  const kind = `search3:${ctx.S.song ? "song" : "general"}:${replan ? "materials" : asIs ? "asis" : "plain"}`;
  const show = (i, ev) => { if (od === mine) progress(ctx, kind, steps, i, "", ev); };
  show(0, null);
  let started;
  try { started = await postJSON("/api/search", odBody(ctx.S)); }
  catch (e) { if (od === mine) { od = null; endCard(); ctx.out().innerHTML = `<div class="o-error">The search could not start: ${esc(e.message)}</div>`; } return; }
  if (od !== mine) { fetch("/cancel/" + started.job_id, { method: "POST" }).catch(() => {}); return; }
  mine.job = started.job_id;
  const es = mine.es = new EventSource("/events/" + started.job_id);
  let doc = null, failed = null;
  es.onmessage = m => {
    let ev; try { ev = JSON.parse(m.data); } catch (_) { return; }
    // build (the account; instant when the engine is warm) -> search -> refine / materials
    if (ev.type === "stage" && ev.state === "active") show({ build: 0, search: 1, refine: 2 }[ev.stage] ?? 1, null);
    else if (ev.type === "progress") show(ev.stage === "refine" ? 2 : 1, ev);
    else if (ev.type === "result") doc = ev.payload;
    else if (ev.type === "error") failed = ev.message || "the search failed";
    else if (ev.type === "end") {
      es.close();
      if (od !== mine) return;
      od = null;
      endCard(!!doc);
      if (!doc) { ctx.out().innerHTML = `<div class="o-error">The search failed: ${esc(failed || "no result")}</div>`; return; }
      if (optsKey(ctx.S) !== key) return;
      // the board mode of THIS search: with "As they are" the rows are scored on the account's
      // boards (no plan to show, and "Now" would be the same team on the same boards); planned
      // boards (and every card maxed, whose boards are complete) carry a plan
      const A = assumptionsOf(ctx.S);
      const boards = A && A.boards === "as_is" ? "current" : "develop";
      const rows = (doc.ranking || []).map(r => ({
        key: teamKey(r.leader, r.members), source: "cached", od: true, route: "the search", api: "/api/search", boards,
        leader: r.leader, leaderName: r.leader_name, members: r.member_order || r.members, ordered: !!r.member_order,
        expected: r.best, kind: doc.scope === "general" ? "across" : "song", sings: r.sings, status: r.status, refinedLeaf: r.refined,
        p10: r.p10, p50: r.p50, p90: r.p90, max: r.song_max ?? null,
      }));
      cache = { url, doc, rows, cards: ctx.D.cards, cached: true };
      sel = 0; open = new Set([0]);
      remember(ctx);
      render(ctx, cache);
    }
  };
  es.onerror = () => { es.close(); if (od === mine && !doc) { od = null; endCard(); ctx.out().innerHTML = `<div class="o-error">Lost the search's event stream.</div>`; } };
}

/* ------------------------------------------------------------------ the live search */
// Cancel answers at once: the page drops the search and shows what it showed before; the
// server is told too and stops at its next check (between batches of teams)
export function cancelScore() {
  if (od) {
    const ctx = od.ctx;
    cancelOnDemand();
    endCard();
    const pw = ctx.out().querySelector(":scope > .o-progwrap"); if (pw) pw.remove();
    toast("Search cancelled.");
    if (!ctx.out().querySelector(".o-split")) showScoreIdle(ctx, true);
    return;
  }
  if (!run) return;
  if (run.job) fetch("/cancel/" + run.job, { method: "POST" }).catch(() => {});
  if (run.abort) run.abort.abort();
  run.cancelled = true;
  if (run.es) run.es.close();
  if (run.reject) run.reject(new Error("cancelled"));
}
// this search's progress card (opt-progress.js): made once per search, then only updated
let pc = null;
function newCard() { if (pc) pc.stop(); pc = null; }
function endCard(answered) { if (pc) { if (answered) pc.done(); else pc.stop(); } pc = null; }
function progress(ctx, kind, steps, active, detail, ev) {
  const out = ctx.out();
  let wrap = out.querySelector(":scope > .o-progwrap");
  if (!pc || !wrap || !wrap.contains(pc.el)) {
    if (pc) pc.stop();
    if (!out.querySelector(".o-split")) out.innerHTML = "";        // keep an answer on screen below
    if (!wrap) { wrap = document.createElement("div"); wrap.className = "o-progwrap"; out.prepend(wrap); }
    pc = progressCard(kind, steps);
    wrap.replaceChildren(pc.el);
  }
  const e = ev || {}, shown = (live && live.key === optsKey(ctx.S) ? live : cache);
  const cached = out.querySelector(".o-split") && shown && shown.rows && shown.rows[0] ? shown.rows[0].expected : null;
  const upd = { active, detail: countLine(e) || detail, done: e.done, total: e.total };
  if (e.best_so_far && e.best_so_far.value != null) upd.best = e.best_so_far.value;
  if (cached != null) upd.note = "the answer below stays until this one finishes";
  pc.update(upd);
}
function solveSSE(ctx, body, onProgress) {
  return new Promise(async (resolve, reject) => {
    let started;
    try { started = await postJSON("/solve", body); } catch (e) { reject(e); return; }
    run.job = started.job_id;
    const es = run.es = new EventSource("/events/" + started.job_id);
    run.reject = reject;
    let payload = null, failed = null;
    es.onmessage = m => {
      let ev; try { ev = JSON.parse(m.data); } catch (_) { return; }
      if (ev.type === "progress" && typeof ev.evaluated === "number") onProgress(`${fmt(ev.evaluated)} arrangements scored`, ev);
      else if (ev.type === "song_progress") onProgress(`${ev.scored} / ${ev.total} teams scored on the chart`, ev);
      else if (ev.type === "status" && ev.message) onProgress(ev.message, ev);
      else if (ev.type === "result") payload = ev.payload;
      else if (ev.type === "error") failed = ev.message || "the search failed";
      else if (ev.type === "cancelled") failed = "cancelled";
      else if (ev.type === "end") { es.close(); run.job = null; if (payload) resolve(payload); else reject(new Error(failed || "the search ended without a result")); }
    };
    es.onerror = () => { es.close(); if (!payload) reject(new Error(failed || "lost the event stream")); };
  });
}

export async function runScore(ctx) {
  // the on-demand search covers these: "Find best team" simply searches again now
  if (assumptionsOf(ctx.S) || basisOf(ctx.S) === "maxed") { cache = null; searchNow(ctx, optsKey(ctx.S)); return; }
  const S = ctx.S, key = optsKey(S), song = S.song;
  const pins = S.require.length + S.exclude.length > 0;
  const pinMembers = S.require.length + S.exclude.length > 0;
  const develop = S.boards === "develop";
  // with a song, every leader's own best team is generated (as the classic page does with a song);
  // a pin turns it off, because that path generates from the whole pool and would ignore the pin
  const leaders = develop && !pins && (!!song || !!S.lmember);
  const refine = develop && !!song && !pinMembers;
  const steps = !develop ? ["Search", "Score on the chart"]
    : leaders ? ["Every leader's best team, Holomem Boards planned" + (refine ? ", members refined" : "")]
    : ["Search", "Plan each team's Holomem Boards" + (song ? "" : ", average over every chart")];
  run = { t0: performance.now(), abort: new AbortController() };
  newCard();
  const kind = "live:" + steps.join("|") + (song ? ":song" : ":general");
  ctx.setBusy(true);
  run.detail = leaders ? "usually under a minute with a song" : "";
  progress(ctx, kind, steps, 0, run.detail, null);
  const warnings = [];
  if (pins && develop && song) warnings.push("Locked or banned cards: the every-leader search and the refine are skipped, because they would ignore them.");
  if (!develop && (S.lmember || S.nooverlap)) warnings.push("Leader options apply to the From scratch search only.");
  try {
    let doc, solved = null;
    if (!leaders) {
      solved = await solveSSE(ctx, {
        ...common(S), top: develop ? 25 : Math.max(S.top, 5), board: !develop, song, across_songs: !song, exact: !!S.exact,
      }, (d, ev) => { run.detail = d; progress(ctx, kind, steps, 0, d, ev); });
    }
    if (develop) {
      run.detail = leaders ? "usually under a minute with a song" : "";
      progress(ctx, kind, steps, leaders ? 0 : 1, run.detail, null);
      const body = { ...common(S), ...leaderOpts(S), seeds: "all", materials: S.infmats ? "infinite" : "mine" };
      if (song) body.song = song; else body.across = true;
      if (leaders) body.leader_diverse = true;
      else body.teams = (solved.teams || []).map(t => [t.leader_id, ...(t.member_ids || [])]).filter(t => t.length === 6);
      if (refine) body.refine = true;
      if (S.maxpoints) body.budget = 361;
      doc = await postJSON("/api/optimize-rerank", body, run.abort.signal);
      if (doc.warnings) warnings.push(...doc.warnings);
    }
    const res = develop ? fromRerank(ctx, doc) : fromSolve(ctx, solved);
    res.key = key; res.warnings = warnings;
    res.elapsed = (performance.now() - run.t0) / 1000;
    live = res; sel = 0; open = new Set([0]);
    endCard(true);
    remember(ctx);
    render(ctx, live);
  } catch (e) {
    const pw = ctx.out().querySelector(":scope > .o-progwrap");
    if (run && run.cancelled) { if (pw) pw.remove(); toast("Search cancelled."); if (!ctx.out().querySelector(".o-split")) showScoreIdle(ctx, true); }
    else if (pw && ctx.out().querySelector(".o-split")) pw.innerHTML = `<div class="o-error">The search failed: ${esc(e.message)}. The answer below is the one shown before the search.</div>`;
    else ctx.out().innerHTML = `<div class="o-error">The search failed: ${esc(e.message)}</div>`;
  } finally {
    endCard(); run = null; ctx.setBusy(false);
  }
}

function fromRerank(ctx, doc) {
  const kind = doc.sorted_by === "song_est" ? "song" : doc.sorted_by === "across_song" ? "across" : "unit";
  const rows = (doc.ranking || []).map(r => ({
    key: teamKey(r.leader_id, r.member_ids), source: "live", boards: "develop", route: "the live search", api: "/api/optimize-rerank",
    leader: r.leader_id, members: r.member_order || r.member_ids, ordered: !!r.member_order, orderShare: r.member_order_share ?? null, kind,
    infmats: !!ctx.S.infmats, deck_power: r.deck_power ?? null,
    expected: kind === "song" ? r.song_est : kind === "across" ? r.across_score : r.optimised_us,
    max: kind === "song" ? r.song_max : null, off: kind === "song" ? r.song_off : r.board_off_us,
    board_state: r.board_state, plan: r.board_plan_summary, cert: r.certificate, refined: r.refined, wins: r.wins,
    cover: { active_cover_pct: r.active_cover_pct, active_overlap_loss_pct: r.active_overlap_loss_pct, active_dead_start_s: r.active_dead_start_s },
  }));
  return { rows, raw: doc.cards, cards: ctx.mergeCards(doc.cards), info: { searched: doc.ranking ? doc.ranking.length : 0, objective: doc.objective, kind,
    basis: doc.certificate_basis, members_first: doc.members_first } };
}
function fromSolve(ctx, payload) {
  const kind = payload.sorted_by === "song_score" ? "song" : payload.sorted_by === "across_song" ? "across" : "unit";
  const rows = (payload.teams || []).map(t => ({
    key: teamKey(t.leader_id, t.member_ids), source: "live", boards: "current", route: "the live search", api: "/solve",
    leader: t.leader_id, members: t.member_order || t.member_ids, ordered: !!t.member_order, orderShare: t.member_order_share ?? null, kind,
    expected: kind === "song" ? t.song_score : kind === "across" ? (t.across_score ?? t.score) : t.score,
    max: t.song_max ?? null, cert: t.certificate, wins: t.wins, cover: {},
  }));
  return { rows, raw: payload.cards, cards: ctx.mergeCards(payload.cards), info: { searched: (payload.search || {}).evaluated, kind } };
}

/* ------------------------------------------------------------------ rendering */
function sorted(ctx, res) {
  const rows = [...res.rows];
  if (ctx.S.sort === "max" && rows.every(r => typeof r.max === "number")) rows.sort((a, b) => b.max - a.max);
  return rows.slice(0, res.cached ? Math.max(ctx.S.top, 20) : Math.max(ctx.S.top, 5));
}
function render(ctx, res) {
  const rows = sorted(ctx, res);
  const cardOf = id => withState(res.cards.get(id) || ctx.D.cards.get(id) || null);
  const S = ctx.S;
  // the cards at the levels the search assumed (a maxed basis: every card maxed)
  const levers = basisOf(S) === "maxed" ? { level: "max", bloom: 5, sp_training: 4 } : assumptionsOf(S);
  loadStates(rows.flatMap(T => [T.leader, ...T.members]), levers).then(changed => { if (changed && (cache === res || live === res)) render(ctx, res); });
  if (!rows.length) {
    ctx.out().innerHTML = res.cached
      ? `<section class="o-empty"><h2>No cached answer for this yet</h2><p>${cacheLine(res.doc)} Press Find best team to search now.</p></section>`
      : `<div class="o-error">The search returned no team.</div>`;
    return;
  }
  setProv(res);
  const ctxT = { ctx, res, rows, cardOf };
  if (sel >= rows.length) { sel = 0; open = new Set([0]); }
  ctx.out().innerHTML = `<div class="o-split">
    <aside class="dlg o-setlist" aria-label="Ranked teams">
      <div class="dlg-h"><h2>${res.cached ? "Setlist of best teams" : "Search results"}</h2><span class="r">${rows.length} teams · ${res.cached ? (res.doc.scope === "general" ? "average over every chart" : "expected score on this chart") : "this search"}</span></div>
      <div class="o-listctl" role="group" aria-label="List view">
        <button class="chip" type="button" data-diffmode aria-pressed="${diffMode}" title="Outline the cards that differ from #1">Diff vs #1</button>
        <button class="chip" type="button" data-oneper aria-pressed="${onePer}" title="Show only each leader card's best team">One per leader</button></div>
      <div class="o-list" id="o-list">${listHTML(ctxT)}</div>
      ${(info => info ? `<p class="o-runinfo">${info}</p>` : "")(runLine(res, S))}
    </aside>
    <section class="o-sel" aria-label="Selected team"><article class="o-team" data-open="true" id="team${sel}">${block(ctxT, rows[sel], sel)}</article></section>
  </div>`;
  ctx.out().onclick = e => {
    const r = e.target.closest(".rk-row[data-i]"); if (r) return select(ctxT, +r.dataset.i);
    if (e.target.closest("[data-diffmode]")) { diffMode = !diffMode; return relist(ctxT); }
    if (e.target.closest("[data-oneper]")) { onePer = !onePer; return relist(ctxT); }
    const tg = e.target.closest("[data-toggle]"); if (tg) return select(ctxT, +tg.dataset.toggle);
    const cp = e.target.closest("[data-copy]"); if (cp) return copy(ctxT, +cp.dataset.copy);
  };
  wakeFigure(ctx.out());
  after(ctxT, rows[sel], sel);
}
function listHTML({ rows, cardOf }) {
  return rankList(rows.map(T => {
    const L = cardOf(T.leader) || {}, [nm, of] = String(T.leaderName || "").split(" / ");
    return { score: T.expected, p10: T.p10 ?? null, p50: T.p50 ?? null, p90: T.p90 ?? null, max: T.max ?? null,
      leader: { id: T.leader, name: L.member || nm || T.leader, outfit: L.costume_name || of || "" },
      cards: [T.leader, ...T.members], sings: T.sings };
  }), { selected: sel, diff: diffMode, onePerLeader: onePer, thumb: id => thumb(id) });
}
function relist(ctxT) {
  const l = byId("o-list"); if (l) l.innerHTML = listHTML(ctxT);
  ctxT.ctx.out().querySelectorAll("[data-diffmode]").forEach(b => b.setAttribute("aria-pressed", diffMode));
  ctxT.ctx.out().querySelectorAll("[data-oneper]").forEach(b => b.setAttribute("aria-pressed", onePer));
}
function select(ctxT, i) {
  if (!ctxT.rows[i]) return;
  sel = i; open = new Set([i]);
  if (ctxT.res === live || ctxT.res === cache) remember(ctxT.ctx);
  const el = ctxT.ctx.out().querySelector(".o-sel > .o-team"); if (!el) return;
  el.id = "team" + i; el.innerHTML = block(ctxT, ctxT.rows[i], i);
  wakeFigure(el);
  ctxT.ctx.out().querySelectorAll(".rk-row[data-i]").forEach(b => b.setAttribute("aria-selected", +b.dataset.i === i));
  if (el.getBoundingClientRect().top < 0) el.scrollIntoView({ block: "start", behavior: "smooth" });
  after(ctxT, ctxT.rows[i], i);
}
// one line: the cache state + how many teams it scored exactly + when; everything else in the popover
function setProv(res) {
  const el = byId("prov"); if (!el) return;
  if (!res.cached) { el.innerHTML = ""; return; }
  const d = res.doc, st = d.on_demand ? ["fresh", "Searched now"] : d.state === "fresh" ? ["fresh", "Up to date"] : d.state === "missing" ? ["missing", "Not computed yet"] : ["stale", "Older data"];
  NOTES.prov = `${NOTES.cached || ""}<br><br>${cacheLine(d)}`;
  const n = (d.exact_evals && d.legal_sets ? `${fmt(d.exact_evals)} of ${fmt(d.legal_sets)} teams scored exactly` : "Cached search")
    + (d.on_demand && d.seconds != null ? ` in ${Number(d.seconds).toFixed(1)} s` : "");
  el.innerHTML = `<span class="o-state ${st[0]}">${st[1]}</span><span class="o-provtxt">${n}${d.written ? ` · ${esc(when(d.written))}` : ""}</span>${info("prov")}`;
}
function cacheLine(doc) {
  const bits = [];
  if (doc.objective) bits.push(doc.objective);
  if (doc.basis) bits.push(doc.basis);
  if (doc.exact_evals && doc.legal_sets) bits.push(`${fmt(doc.exact_evals)} teams scored exactly out of ${fmt(doc.legal_sets)}`);
  if (doc.written) bits.push(`computed ${when(doc.written)}`);
  return esc(bits.join(" · ")) + (bits.length ? "." : "");
}
function runLine(res, S) {
  if (res.cached) return "";
  const k = res.info.kind;
  const what = k === "song" ? "expected score on this chart" : k === "across" ? "average score over every chart" : "Unit Score";
  const bits = [`Live search · ranked by ${what}`, `${res.rows.length} teams scored`, `${res.elapsed.toFixed(1)} s`];
  if (res.rows[0] && res.rows[0].cert && res.rows[0].cert.status === "EXACT") bits.push("#1 certified EXACT");
  return esc(bits.join(" · ")) + (res.warnings && res.warnings.length ? `<br><span class="w">${res.warnings.map(esc).join("<br>")}</span>` : "");
}
const lineup = (T, cardOf) => [{ c: cardOf(T.leader) || { id: T.leader, member: T.leader }, slot: 0, lead: true },
  ...T.members.map((id, k) => ({ c: cardOf(id) || { id, member: id }, slot: T.ordered ? k + 1 : 0, lead: false }))];
const scoreLabel = T => T.od ? "Expected Score" : T.source === "cached" ? "Score (cached search)" : T.boards === "current" ? "Score" : T.kind === "song" ? "Expected Score" : T.kind === "across" ? "Average score" : "Unit Score";
const scoreNote = T => T.source === "cached" ? "cached" : T.boards === "current" ? "expected_cur" : T.kind === "song" ? "expected" : T.kind === "across" ? "across" : "unitscore";

function perf({ c, slot, lead }, k) {
  return `<figure class="o-perf" style="--i:${k}">
    ${window.HBCard.face(c, { lead, roleText: "Leader", w: 400 })}
    <figcaption>
      <span class="slot" aria-label="${slot ? "Slot " + slot : ""}">${slot || ""}</span>
      <b>${esc(c.costume_name || c.title || c.id)}</b>
      <span class="mb">${esc(c.member || "")}</span>
      <span class="xb">${c.level != null ? `Lv ${c.level}` : ""}${c.bloom ? ` · Bloom ${c.bloom}` : ""}</span>
    </figcaption>
  </figure>`;
}
function block(ctxT, T, i) {
  const { ctx, rows, cardOf } = ctxT;
  const det = details.get(T.source + ":" + T.key) || {};
  const f = det.fig || null;
  const L = cardOf(T.leader) || {};
  const general = T.source === "cached" && T.kind === "across";   // boards (and slot order) rebuilt per song
  const delta = rows.length < 2 ? "" : i === 0
    ? `${sgn((T.expected / rows[1].expected - 1) * 100)} over #2`
    : `${sgn((T.expected / rows[0].expected - 1) * 100)} vs #1`;
  const now = det.cur && T.boards !== "current" ? det.cur.value : null;
  const max = general ? null : (f && f.song_max != null ? f.song_max : T.max);
  const pend = w => `<span class="o-sk" style="width:${w}px;height:22px;margin-top:6px"></span>`;
  const dash = `<span class="n">—</span>`;
  // a general row's Unit Score / Overall Power are the boards-as-they-are figures (song-free);
  // a song row's are its own board's, the same basis as its Expected Score
  const src = general ? det.cur : f;
  const srcErr = general ? det.curErr : det.err;
  const us = deckUnit(src), op = src && src.panel ? src.panel.overall_power : null;
  const side = [];
  if (max != null) side.push(`<div class="stat-tile"><div><span class="lbl">Max ${info("max")}</span>${score(max)}<small>every Active fires</small></div></div>`);
  if (T.boards !== "current" && T.kind !== "unit") side.push(`<div class="stat-tile"><div><span class="lbl">Now ${info("now")}</span>${now != null ? score(now) : det.curErr ? dash : pend(150)}<small>${general ? "average, boards as they are" : "boards as they are"}</small></div></div>`);
  if (T.kind !== "unit") side.push(`<div class="stat-tile"><div><span class="lbl">Unit Score ${info("grade")}</span>${us != null ? score(us) : srcErr ? dash : pend(110)}<small>${general ? "boards as they are" : "this team's board"}</small></div></div>`);
  side.push(`<div class="stat-tile"><div><span class="lbl">Overall Power</span>${op != null ? `<span class="n o-num">${fmt(op)}</span>` : srcErr ? dash : pend(110)}<small>${general ? "boards as they are" : "this team's board"}</small></div></div>`);
  // the run spread on one line under the hero score (served on the top cached rows)
  const spread = T.p10 != null && T.p90 != null ? `p10 ${fmt(T.p10)} · p50 ${fmt(T.p50)} · p90 ${fmt(T.p90)}` : "";
  // the expected score is never graded: the one grade of the view is the Unit Score below
  const head = `<div class="o-thead">
      <div class="o-who"><span class="rk-n">${i + 1}</span><h2>${esc(L.member || T.leader)} <small>leads${L.costume_name ? " · " + esc(L.costume_name) : ""}</small></h2></div>
      <div class="o-tact">
        ${T.cert && T.cert.status === "EXACT" ? `<span class="o-tag" style="color:var(--good);border-color:currentColor">EXACT</span>` : ""}
        <button class="btn small" type="button" data-copy="${i}"><svg class="i s"><use href="#i-copy"/></svg>Copy team</button>
      </div>
    </div>`;
  const tiles = `<div class="o-tiles">
      <div class="stat-tile prime"><div><span class="lbl">${scoreLabel(T)} ${info(scoreNote(T))}</span>${score(T.expected)}<small>${spread}${spread && delta ? " · " : ""}${delta}</small></div></div>
      <div class="o-sidetiles">${side.join("")}</div>
    </div>`;
  // the slot order is the detail's (the row's own board); a general row has none: it differs per song
  const order = f && f.member_order && f.member_order.length === 5 ? f.member_order : T.ordered ? T.members : null;
  const shown = { ...T, members: order || T.members, ordered: !!order };
  const songName = id => { const s = ctx.D.songs.find(x => x.music_id === id); return s ? s.title : id; };
  const orderNote = general
    ? (order ? `Slot order on ${esc(songName(det.song))}; it varies by song.` : "Slot order varies by song.")
    : !order ? (det.fig || det.err ? "Slot order not served for this team." : "Reading the slot order…")
    : T.orderShare != null && !f ? `Slot order best on ${Math.round(T.orderShare * 100)}% of the charts.` : "";
  // the leader is the figure on the left (leaderColumn); the strip holds the five members
  const cards = `<div class="o-stage team-strip o-members">${lineup(shown, cardOf).slice(1).map(perf).join("")}</div>
    ${orderNote ? `<p class="o-ordernote">${orderNote}</p>` : ""}
    ${T.refined && T.refined.length ? `<p class="o-ordernote">Refined: ${T.refined.map(r => `${esc((cardOf(r.out) || {}).member || r.out)} → ${esc((cardOf(r.in) || {}).member || r.in)}`).join(" · ")}</p>` : ""}`;
  const outfit = L.costume_skill || L.costume_name ? `<div class="o-outfit"><svg class="i s"><use href="#i-shirt"/></svg>
      <span>Outfit Skill · <b>${esc(L.costume_name || "")}</b></span>${L.costume_skill || L.costume_text ? `<span aria-hidden="true">·</span><span class="fx">${esc(outfitText(L.costume_skill, L.costume_text))}</span>` : ""}</div>` : "";
  // a general row: the detail is the team on ONE song, chosen here, its board rebuilt for that song
  const perSong = general ? `<div class="o-sect o-persong">
      <h3><label for="persong${i}">On</label> <select id="persong${i}" data-persong="${i}">${ctx.D.songs.map(s => `<option value="${esc(s.music_id)}"${s.music_id === det.song ? " selected" : ""}>${esc(s.title || s.music_id)}</option>`).join("")}</select></h3>
      <p>This team on one song, its Holomem Board rebuilt for that song. The figures above are the average over every chart.</p></div>` : "";
  const T2 = { ...T, kind: general ? "song" : T.kind, expected: general ? (f ? f.value : null) : T.expected, max };
  // a general row's "Now" is an average over every chart: it never enters the per-song panels
  const dS = general ? { ...det, cur: null, curErr: null } : det;
  const body = `<div class="o-sect"><div class="o-duo">${unitPanel(T2, dS)}${runPanel(T2, dS)}</div></div>` +
    ((ctx.S.song && T.kind !== "across") || general ? timelinePanel(T2, dS, cardOf) : "") +
    (T.boards === "current" ? "" : boardPanel(T2, dS, wallet, i === 0, cardOf));
  const unitBlock = `<div class="o-unit">${leaderColumn({ ...L, id: T.leader }, { outfit })}
    <div class="o-unit-main">${tiles}${general ? "" : matchLine(T, det)}${cards}</div></div>`;
  return head + unitBlock + (general ? `<section class="o-onsong">${perSong}${body}</section>` : body);
}

function repaint(ctxT, T, i) {
  const el = byId("team" + i);
  if (!el || !open.has(i) || ctxT.rows[i] !== T) return;
  const planOpen = el.querySelector("details.o-plan");
  const keepPlan = planOpen ? planOpen.open : i === 0;
  const openRows = [...el.querySelectorAll("details.o-pr")].map(d => d.open);
  el.innerHTML = block(ctxT, T, i);
  wakeFigure(el);
  const p = el.querySelector("details.o-plan"); if (p) p.open = keepPlan;
  el.querySelectorAll("details.o-pr").forEach((d, k) => { if (openRows[k]) d.open = true; });
  el.querySelectorAll(".o-reveal").forEach(x => x.classList.add("in"));
  wireTimeline(el, details.get(T.source + ":" + T.key) || {}, ctxT.cardOf);
  wirePlan(el, T, details.get(T.source + ":" + T.key) || {}, [T.leader, ...T.members].map(id => "chr-" + String(id).split("-")[1]));
  const rp = el.querySelector("[data-rp]");
  if (rp) rp.onclick = () => resetPrice(ctxT, T, i);
  const ps = el.querySelector("[data-persong]");
  if (ps) ps.onchange = () => { const det = details.get(T.source + ":" + T.key); if (det) { det.song = ps.value; perSongDetail(ctxT, T, i, det); } };
}

/* what the row's resets buy: the same team re-planned with fewer (/api/row-detail "reset_price") */
async function resetPrice(ctxT, T, i) {
  const det = details.get(T.source + ":" + T.key);
  if (!det || !det.rpBody || det.rpLoading) return;
  det.rpLoading = true; det.rpErr = null; repaint(ctxT, T, i);
  try {
    const d = await postJSON("/api/row-detail", { ...det.rpBody, reset_price: true });
    det.rp = d.reset_price || { points: [] };
  } catch (e) { det.rpErr = e.message; }
  det.rpLoading = false; repaint(ctxT, T, i);
}

/* one detail's figures from a /api/compare team row */
function fromCompare(d, route, api = "/api/compare") {
  const t = d.teams[0], s = t.score || {};
  return { route, api, value: s.song_est ?? s.across_mean ?? s.unit_score, song_max: s.song_max ?? null, member_order: s.member_order || null,
    deck_power: s.deck_power ?? null, deck_power_basis: s.deck_power_basis, panel: t.panel, score_bonus: t.score_bonus,
    distribution: t.distribution || null, board_state: t.board_state, unit_off: s.unit_score_board_off };
}
/* one detail's figures from /api/row-detail */
function fromRowDetail(d) {
  return { route: "the team's own board", api: "/api/row-detail", value: d.song_est, song_max: d.song_max, member_order: d.member_order, deck_power: d.deck_power,
    deck_power_basis: d.deck_power_basis, panel: d.panel, score_bonus: d.score_bonus, distribution: d.distribution || null,
    board_state: d.board_state, bench_sockets: d.bench_sockets || null, bench_slots: d.bench_slots || null, row_matches: d.row_matches, row_best: d.row_best, unit_off: null };
}
function baseOf(S) {
  const b = { basis: basisOf(S), song: S.song, across: !S.song, platform: S.platform, uptime_model: "finite", auto: !!S.auto,
    assume_level: S.cards === "whatif" ? S.alevel : null, assume_bloom: S.cards === "whatif" ? S.abloom : null, difficulty: S.difficulty };
  if (S.maxpoints) b.budget = 361;
  return b;
}
function boardDoc(ctxT, T, i, det, bs, cards) {
  if (!bs || !Object.keys(bs).length) return Promise.resolve();
  const S = ctxT.ctx.S;
  // the whole account's plan (every holomem's target board), as holodori.best shows it: the team's
  // six boards are this row's own; it takes seconds, so it fills in after the rest
  const A = assumptionsOf(S);
  if (A && A.boards === "plan" && !det.acct && !det.acctLoading) {
    det.acctLoading = true;
    postJSON("/api/board-plan-account", { cards, song: S.song || null, assumptions: A, board_state: bs, bench_slots: (det.fig || {}).bench_slots || null })
      .then(d => { det.acct = d; det.acctErr = null; }).catch(e => { det.acctErr = e.message; })
      .finally(() => { det.acctLoading = false; repaint(ctxT, T, i); });
  }
  // a maxed account: EVERY holomem's target is its complete board (the team's sockets its row's,
  // the bench's the exact assignment the score used), shown against the account's REAL boards --
  // what the user would have to unlock; measured against the full 453 Pt
  if (basisOf(S) === "maxed") {
    det.acctLoading = true;
    return postJSON("/api/board-doc", { board_state: bs, cards, basis: "maxed", budget: 453, complete: true })
      .then(d => { det.acct = det.doc = d; det.acctErr = det.docErr = null; })
      .catch(e => { det.acctErr = det.docErr = e.message; })
      .finally(() => { det.acctLoading = false; });
  }
  return postJSON("/api/board-doc", { board_state: bs, cards, basis: basisOf(S), budget: baseOf(S).budget })
    .then(d => { det.doc = d; det.docErr = null; }).catch(e => { det.docErr = e.message; });
}

/* the row's run distribution, asked on its own after the figures (it takes ~5 s more, so
   the figures are not held back for it) */
export async function withDistribution(det, body, paint) {
  if (!det.fig) return;
  det.distLoading = true; paint();
  try { det.fig.distribution = (await postJSON("/api/row-detail", { ...body, distribution: true })).distribution || null; }
  catch (e) { det.fig.distribution = { available: false, reason: e.message }; }
  det.distLoading = false;
}

/* a general (across) cached row on ONE song: /api/row-detail for that song */
async function perSongDetail(ctxT, T, i, det) {
  const S = ctxT.ctx.S, cards = [T.leader, ...T.members], song = det.song;
  Object.assign(det, { fig: null, tl: null, tlErr: null, doc: null, docErr: null, err: null });
  repaint(ctxT, T, i);
  try {
    // an on-demand row carries the search's levers, as the song rows do, so an "In general"
    // search's detail does not fall back to boards and cards as they are
    const body = { song, difficulty: S.difficulty, basis: tsBasis(S) || "account", cards,
      ...(T.od ? { refine: !!T.refinedLeaf, ...(assumptionsOf(S) ? { assumptions: assumptionsOf(S) } : {}) } : {}) };
    const d = await postJSON("/api/row-detail", { ...body, timeline: true });
    if (det.song !== song) return;
    det.fig = fromRowDetail(d); det.fig.song = song;
    det.tl = d.timeline ? { timeline: d.timeline } : null;
    if (!d.timeline) det.tlErr = "not served";
    repaint(ctxT, T, i);
    await boardDoc(ctxT, T, i, det, d.board_state, cards);
    if (det.song === song) await withDistribution(det, body, () => repaint(ctxT, T, i));
  } catch (e) { if (det.song === song) det.err = e.message; }
  if (det.song === song) repaint(ctxT, T, i);
}

/* the detail fetches for an open team — every figure from the route that rebuilds the ROW's board:
   a cached row -> /api/row-detail (its own two-stage leaf); a live developed row -> /api/compare
   board "state" with the row's board_state and slot order; a /solve row (boards as they are) ->
   /api/compare captured. "Now" is always /api/compare captured. */
async function after(ctxT, T, i) {
  const { ctx, cardOf } = ctxT;
  const el = byId("team" + i);
  reveal(el);
  const S = ctx.S;
  let det = details.get(T.source + ":" + T.key);
  // a cached failure is not an answer: opening the row again retries (see opt-event.js after)
  if (det && det.optsKey === optsKey(S) && !det.err) { repaint(ctxT, T, i); return; }
  det = { optsKey: optsKey(S), unlimited: basisOf(S) === "maxed" }; details.set(T.source + ":" + T.key, det);
  const cards = [T.leader, ...T.members];
  const base = baseOf(S);
  const cmp = (board, extra = {}) => postJSON("/api/compare", { ...base, board, ...extra });
  const jobs = [];
  if (!wallet) jobs.push(getJSON("/api/resources").then(w => { wallet = w; }).catch(() => { wallet = {}; }));
  const song = S.song && T.kind === "song";
  if (T.boards !== "current" && T.kind !== "unit") {
    // a general row: base.across is set (no song), so this is the average over every chart
    jobs.push(cmp("captured", { teams: [{ cards }, { cards }] }).then(d => { det.cur = fromCompare(d, "your boards as they are"); })
      .catch(e => { det.curErr = e.message; }).then(() => repaint(ctxT, T, i)));
  }
  if (T.source === "cached" && T.kind === "across") {
    det.song = (ctx.D.songs[0] || {}).music_id;
    if (det.song) jobs.push(perSongDetail(ctxT, T, i, det));
  } else if (T.source === "cached") {
    jobs.push((async () => {
      try {
        const body = { song: S.song, difficulty: S.difficulty, basis: tsBasis(S) || "account", cards,
          member_order: T.ordered ? T.members : undefined,
          ...(T.od ? { refine: !!T.refinedLeaf, ...(assumptionsOf(S) ? { assumptions: assumptionsOf(S) } : {}) } : {}) };
        const d = await postJSON("/api/row-detail", { ...body, timeline: true });
        det.fig = fromRowDetail(d);
        // a row planned within the wallet can show what its resets buy (a few plans more: on request)
        const A = body.assumptions;
        if (A && A.boards === "plan" && !A.infinite && d.plan_cost && d.plan_cost.resets > 0) det.rpBody = body;
        det.noRefund = !!(A && A.no_refund);
        if (d.timeline) det.tl = { timeline: d.timeline }; else det.tlErr = "not served";
        repaint(ctxT, T, i);
        await boardDoc(ctxT, T, i, det, d.board_state, cards);
        await withDistribution(det, body, () => repaint(ctxT, T, i));
      } catch (e) { det.err = e.message; }
      repaint(ctxT, T, i);
    })());
  } else {
    // a live row: its own board (state) when it carries one, else the boards as they are
    const own = T.boards === "current" || !T.board_state;
    const team = own ? { cards } : { cards, board_state: T.board_state, member_order: T.ordered ? T.members : undefined };
    const pFig = cmp(own ? "captured" : "state", { teams: [team, team], distribution: !!song })
      .then(d => { det.fig = fromCompare(d, own ? (T.boards === "current" ? "your boards as they are" : "the team scorer") : "the team's own board", own ? "/api/compare" : "/api/compare (board: state)"); })
      .catch(e => { det.err = e.message; });
    jobs.push(pFig.then(() => repaint(ctxT, T, i)));
    jobs.push((async () => {
      await pFig;
      const order = det.fig && det.fig.member_order;
      const tasks = [];
      if (song) {
        tasks.push(postJSON("/api/timeline", { song: S.song, cards, board_state: own ? undefined : T.board_state, member_order: order || undefined,
          basis: basisOf(S), difficulty: S.difficulty, platform: S.platform, uptime_model: "finite", auto: !!S.auto,
          assume_level: base.assume_level, assume_bloom: base.assume_bloom })
          .then(d => { if (d.available === false) det.tlErr = d.reason || "not available"; else det.tl = d; })
          .catch(e => { det.tlErr = e.message; }));
      }
      if (T.boards !== "current") tasks.push(boardDoc(ctxT, T, i, det, T.board_state, cards));
      await Promise.all(tasks);
      repaint(ctxT, T, i);
    })());
  }
  await Promise.all(jobs);
}

function copy({ ctx, rows, cardOf }, i) {
  const T = rows[i], song = ctx.song();
  const name = id => { const c = cardOf(id) || {}; return `${c.costume_name || id} (${c.member || ""})`; };
  const txt = [`${song ? song.title : "Every song"} · team #${i + 1} · ${fmt(T.expected)}`, `Leader: ${name(T.leader)}`,
    ...T.members.map((id, k) => `${T.ordered ? k + 1 + ". " : "· "}${name(id)}`)].join("\n");
  navigator.clipboard?.writeText(txt).then(() => toast("Team copied"), () => toast("Couldn't copy"));
}
