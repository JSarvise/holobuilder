// hbuilder web — the Solve -> Ranking client.
//
// Vanilla ES module, no bundler, no framework. One EventSource per run;
// progress is the solver's real callback, never a fabricated animation.
//
// Rendering the ranking (all N teams the server returns, not just the first)
// lives in `ranking.js`; this file is the transport and the chrome.

import { renderRanking, separabilityNote, separabilityDetail, separabilityBand, num, metricOf } from "/ranking.js";
import { startRun } from "/run.js";
import { leaderOptions } from "/leader.js";
import { boardSection } from "/boardstate.js";
import { renderTimeline } from "/timeline.js";
// The song picker is a panel over the `#song` select: it writes the chosen
// music id into that select and fires its `change`, so everything below keeps
// reading one element and knows nothing about how the choice was made.
import { mountSongPicker } from "/songpicker.js";
// The park screen mounts itself when `#pwrap` is in the page; it is a
// separate task from the ranking and shares nothing with it but the tab.
import { mountPark } from "/park.js";
import { mountResources } from "/resources.js";
// The gacha tracker mounts the same way: lazy on its own <details>, fed by /api/gacha.
import { mountGacha } from "/gacha.js";
// The Compare view (two hand-picked teams, side by side). Same lazy-<details> contract.
import { mountCompare } from "/compare.js";
import { mountBoard } from "/board.js";
import { mountCollection } from "/collection.js";
// The board plan (visual board trees) is shown INLINE under a Solve result —
// one flow, teams + the board to build for them.
import { boardPlanSection, planDiffNote } from "/boardplan.js";
// Search constraints (require/leader/exclude/include-unowned) — a chip picker
// that surfaces the selectors the backend always had but the page hard-coded empty.
import { mountConstraints, getConstraints, constraintsSummary, mountPinSlots } from "/constraints.js";

// The Solve "basis" and the optimiser "scenario" are the same three collection
// states under different names; the mapping lives server-side in
// /api/boardplan (the page posts its basis, the server picks the scenario), so
// the page needs no copy of it.

const $ = (id) => document.getElementById(id);

const el = {
  solve: $("solve"), cancel: $("cancel"), top: $("top"), basis: $("basis"),
  estimate: $("estimate"), exact: $("exact"), across: $("across"),
  refresh: $("refresh"), boardmode: $("boardmode"),
  progress: $("progress"), fill: $("fill"), pstate: $("pstate"),
  pcount: $("pcount"), ptime: $("ptime"), pnote: $("pnote"),
  err: $("err"), result: $("result"), idle: $("idle"),
  score: $("score"), lower: $("lower"), gatecount: $("gatecount"),
  teamcount: $("teamcount"), elapsed: $("elapsed"), scanned: $("scanned"),
  approx: $("approx"), noise: $("noise"), board: $("board"),
  ranking: $("ranking"), rankhint: $("rankhint"),
  diagwrap: $("diagwrap"), diag: $("diag"), diagcaveat: $("diagcaveat"),
  assume: $("assume"),
  mode: $("mode"), modehint: $("modehint"), cardsmode: $("cardsmode"),
  assumerow: $("assumerow"), boardhint: $("boardhint"),
  topctrl: $("topctrl"), cstrwrap: $("cstrwrap"), refinewrap: $("refinewrap"),
};

// --- Step 1: what to build --------------------------------------------------
// "best" is the classic solve; "around" is the same solve with the locked-cards
// drawer opened; "acquire" answers the next-best-card question via POST /acquire.
const modeVal = () => (el.mode && el.mode.value) || "best";
const MODE_HINTS = {
  best: "",
  around: "Add the cards to lock (and/or the leader) below — the search fills the remaining slots.",
  acquire: "Ranks every card you do NOT own by the score of the best team it joins, against your best owned team. " +
    "Candidates are scored at MAX level/bloom (“what could it do once built”); your cards stay as chosen above. " +
    "Board applies as Current/Off; develop happens after you pick a target team.",
};
function applyMode() {
  const m = modeVal();
  if (el.modehint) el.modehint.textContent = MODE_HINTS[m] || "";
  // Acquire has no team count, no exhaustive/estimate, no refine.
  const acquire = m === "acquire";
  if (el.topctrl) el.topctrl.hidden = acquire;
  if (el.exact) el.exact.parentElement.hidden = acquire;
  if (el.estimate) el.estimate.hidden = acquire;
  if (el.refinewrap) el.refinewrap.hidden = acquire;
  if (el.cstrwrap) {
    el.cstrwrap.hidden = false;
    if (m === "around" || m === "acquire") el.cstrwrap.open = m === "around";
  }
  // in-panel pin slots, for the two modes that take pins.
  const pinwrap = $("pinwrap"), pinlead = $("pinlead");
  if (pinwrap) pinwrap.hidden = !(m === "around" || m === "acquire");
  if (pinlead) pinlead.textContent = m === "acquire"
    ? "Cards to build around (optional) — pick up to four owned cards to keep " +
      "in the team. Every card you don't own is tried and empty slots are " +
      "filled automatically, so this finds the best team your account can " +
      "have with ONE new card."
    : m === "around"
    ? "Pick the cards to lock into every team — the search fills the rest. " +
      "The drawer below also pins a leader or bans cards."
    : "";
  if (el.solve) el.solve.textContent = acquire ? "Find next best card" : "Solve";
}
if (el.mode) { el.mode.addEventListener("change", applyMode); applyMode(); }

// --- Step 2: the ONE cards axis --------------------------------------------
// #cardsmode drives the request's basis + the what-if row. "asis" resets the
// assume selects so a hidden stale what-if can never leak into a run.
// "maxed-account" (comparable with the community tool holodori.best) is a THIRD
// server basis: maxed cards on
// a fully maxed account (54 bench boards at 453, memories 37, power-up 11.58 %).
const MAXED_ACCOUNT_LABEL = "maxed account · 54 bench boards @453 · memories 37 · power-up 11.58 %";
const MAXED_ACCOUNT_REASON = "Cards = maxed account: the board is develop / full tree (453) by definition — a maxed account has every point.";
const cardsModeVal = () => (el.cardsmode && el.cardsmode.value) || "asis";
const maxedAccountOn = () => cardsModeVal() === "maxed-account";
function applyCardsMode() {
  const v = cardsModeVal();
  const basis = v === "maxed" ? "maxed" : v === "maxed-account" ? "maxed-account" : "account";
  if (el.basis && el.basis.value !== basis) {
    el.basis.value = basis;
    el.basis.dispatchEvent(new Event("change", { bubbles: true }));
  }
  if (el.assumerow) el.assumerow.hidden = v !== "whatif";
  if (v !== "whatif") {
    const lv = $("assume_level"), bl = $("assume_bloom");
    if (lv) lv.value = "account";
    if (bl) bl.value = "account";
  }
}
// On a user change the Board control follows (forced to develop / full tree
// on the maxed-account basis) and the header strip restates the scenario.
// Both read consts declared further down, so they run on events only; their
// own init calls (below) already read the cards mode.
if (el.cardsmode) {
  el.cardsmode.addEventListener("change", () => { applyCardsMode(); syncBoardMode(); applyMetaStrip(); });
  applyCardsMode();
}

// --- Step 2: the board hint, per option ------------------------------------
const BOARD_HINTS = {
  "develop-account": "Builds each team its best board from scratch (a respec plan) with each character's REAL earned points, places your Connect cards, then ranks.",
  "develop-max": "Same, but spends the flat per-character budget the slider chose (453 = the full tree, the “if I fully levelled everyone” ceiling).",
  "develop-leaders": "One best team PER LEADER, each with its board developed — breaks the one-leader monoculture. Slower.",
  "current": "Scores your captured board exactly as it is — no respec, no develop.",
  "off": "No board at all: the from-scratch question.",
};
function applyBoardHint() {
  if (el.boardhint) el.boardhint.textContent = BOARD_HINTS[boardModeVal()] || "";
}

// The unified Board control (index.html #boardmode): one axis for "which board
// does the ranking score against". `develop-account`/`develop-max` develop each
// team's best board (Engine-A) before ranking; `current` scores the captured
// board as-is; `off` scores bare. A single control, no duplication.
const boardModeVal = () => (el.boardmode && el.boardmode.value) || "develop-account";
// Whether the NON-developed scoring uses the captured board. ONLY "current" does.
// For a develop mode the initial solve must be board-OFF: it only picks the top-K
// CANDIDATES that then get their boards developed, and scoring those candidates
// against the current board would bias the pool toward teams that happen to own a
// good board today — hiding a team that's stronger once its board is built (the
// exact "no-board surfaces Suisei, develop surfaces Calliope" mismatch). "off"
// is board-off too; develop overwrites the transient board-off ranking anyway.
const boardWanted = () => boardModeVal() === "current";
// The develop budget for the auto-rerank: null = don't develop (off/current);
// "account" = each character's real points; "max" = the full level-50 tree.
// Pins against the two develop options that cannot honour them. "Every leader"
// GENERATES its teams from the whole pool and the swap-refine may swap a
// required card out or a banned one in (server.py says so in its warnings) —
// and picking a song switches both on by itself — so "Build around cards" with
// one card locked could return five teams, none holding it. A pin wins: the
// option it conflicts with is not sent.
const pinned = () => {
  const c = getConstraints();
  const members = c.require.length + c.exclude.length;
  return { any: members + c.leader.length > 0, members: members > 0 };
};
const PINS_NOTE = " Locked / banned cards are honoured: “every leader” and “refine” are skipped for this run, because they would ignore them.";
const developMode = () => {
  const v = boardModeVal();
  // a chosen leader needs every leader's own best team to filter from
  if (v.startsWith("develop") && leaderOptions().leader_member && !pinned().any) return "leaders";
  return v === "develop-account" ? "account"
    : v === "develop-max" ? "max"
    : v === "develop-leaders" ? (!pinned().any ? "leaders" : devBudget() === "real" ? "account" : "max")
    : null;
};
if (el.boardmode) {
  el.boardmode.addEventListener("change", applyBoardHint);
  applyBoardHint();
}

// --- the decomposed Board controls -> the hidden #boardmode value -----------
// #boardprimary (develop/current/off) + the leaders switch + the points budget
// compute the same five-valued #boardmode the request logic has always read,
// so nothing downstream changes. The slider exposes what the server always
// accepted: any flat per-character budget 0-453, not just "real" or 447.
const _bp = $("boardprimary"), _leaders = $("leaders"),
      _breal = $("budgetreal"), _bslider = $("budgetslider"),
      _bval = $("budgetval"), _devopts = $("devopts");
function devBudget() {
  // "real" -> the server uses each character's earned points; a number -> the
  // flat per-character cap the slider chose (453 = the full tree).
  if (!_breal || _breal.checked) return "real";
  return Number(_bslider && _bslider.value) || 0;
}
const _budgetrow = $("budgetrow");
function syncBoardMode() {
  if (!_bp || !el.boardmode) return;
  // Cards = maxed account forces develop / full tree: the "current" board and
  // "real points" are the ACCOUNT's, and this basis is not the account.
  // Disabled with the reason on the control (like every-leader does with the
  // budget), never silently changed back when the user leaves the basis.
  const forced = typeof maxedAccountOn === "function" && maxedAccountOn();
  if (forced) {
    if (_bp.value !== "develop") _bp.value = "develop";
    if (_breal && _breal.checked) _breal.checked = false;
    if (_bslider && Number(_bslider.value) !== 453) _bslider.value = "453";
  }
  _bp.disabled = forced;
  _bp.title = forced ? MAXED_ACCOUNT_REASON : "";
  if (_breal) _breal.disabled = forced;
  if (_budgetrow) _budgetrow.title = forced ? MAXED_ACCOUNT_REASON : "";
  const primary = _bp.value;
  const develop = primary === "develop";
  // The develop's internal knobs (every leader, refine, candidates) stay out of
  // sight and on their automatic defaults; the two board assumptions show
  // whenever the boards are being planned.
  if (_devopts) _devopts.hidden = true;
  for (const id of ["maxpointsrow", "infmatsrow"]) {
    const row = $(id);
    if (row) row.hidden = !develop || modeVal() === "acquire";
  }
  if (_bslider) _bslider.disabled = forced || !_breal || _breal.checked;
  if (_bval) {
    _bval.textContent = _breal && _breal.checked
      ? "real"
      : `${Number(_bslider.value)} pt${Number(_bslider.value) === 453 ? " (full tree)" : ""}`;
  }
  if (_leaders) _leaders.disabled = !develop;
  const value = !develop ? primary
    : (_leaders && _leaders.checked) ? "develop-leaders"
    : devBudget() === "real" ? "develop-account"
    : "develop-max";
  if (el.boardmode.value !== value) {
    el.boardmode.value = value;
    el.boardmode.dispatchEvent(new Event("change", { bubbles: true }));
  }
}
for (const control of [_bp, _leaders, _breal]) {
  if (control) control.addEventListener("change", syncBoardMode);
}
// "Assume max board points": every character at level 50, 361 points
// (tree_budget.py). It drives the flat budget the request already
// carries; off is each character's earned points.
const MAX_LEVEL_POINTS = 361;
const _maxpoints = $("maxpoints");
if (_maxpoints && _breal && _bslider) {
  _maxpoints.addEventListener("change", () => {
    _breal.checked = !_maxpoints.checked;
    _bslider.value = String(_maxpoints.checked ? MAX_LEVEL_POINTS : 453);
    syncBoardMode();
  });
}
// "Assume unlimited cubes and hexas": the develop spends the idle points of the
// characters off the team on green (server: `materials: "infinite"`).
const materialsVal = () => ($("infmats") && $("infmats").checked ? "infinite" : "mine");
if (_bslider) _bslider.addEventListener("input", syncBoardMode);
if (el.mode) el.mode.addEventListener("change", syncBoardMode);
syncBoardMode();

// Choosing a SONG switches on "every leader" + "refine" (once, and only if the
// user has not touched them): the stage-1 solve is song-agnostic, so without
// leader diversity the develop only re-scores the beam's monoculture and the
// song's own singer may never appear (e.g. a singer whose nodes are worth
// +20 % on her song, missing from a list of one other leader x5). Wide implies
// Refine: alone it does nothing.
{
  const refineBox = $("refine"), wideBox = $("refinewide"), songSel = $("song");
  const touched = new Set();
  for (const box of [_leaders, refineBox]) {
    if (box) box.addEventListener("change", () => touched.add(box.id));
  }
  if (wideBox && refineBox) {
    wideBox.addEventListener("change", () => {
      if (wideBox.checked && !refineBox.checked) {
        refineBox.checked = true;
        refineBox.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
  }
  if (songSel) {
    songSel.addEventListener("change", () => {
      if (!songSel.value) return;
      let moved = false;
      if (_leaders && !_leaders.disabled && !_leaders.checked && !touched.has("leaders")) {
        _leaders.checked = true; moved = true;
      }
      if (refineBox && !refineBox.checked && !touched.has("refine")) {
        refineBox.checked = true; moved = true;
      }
      if (moved) syncBoardMode();
    });
  }
}

// --- the account-facts strip (/api/meta) ------------------------------------
// Absent data renders as absent, never as zero — a meta fetch that fails just
// leaves the strip hidden.
(async () => {
  const strip = $("metastrip");
  if (!strip) return;
  try {
    const meta = await (await fetch("/api/meta")).json();
    if (meta.error) return;
    const bits = [];
    if (meta.owned != null && meta.catalogue != null) {
      bits.push(`${meta.owned}/${meta.catalogue} cards owned`);
    }
    if (meta.boards_with_nodes != null) {
      bits.push(`${meta.boards_with_nodes} boards with nodes`);
    }
    if (meta.memory_bonus_permil != null) {
      bits.push(`Memory bonus +${(meta.memory_bonus_permil / 10).toFixed(2)}%`);
    }
    if (meta.power_up_permyriad != null) {
      bits.push(`Member power-up +${(meta.power_up_permyriad / 100).toFixed(2)}%`);
    }
    if (meta.owned_arrangements != null) {
      bits.push(`owned pool = ${num(meta.owned_arrangements)} team arrangements`);
    }
    if (bits.length) {
      // kept in `data-own`: applyMetaStrip renders it, or the scenario line
      strip.dataset.own = "Your data: " + bits.join(" · ");
    }
  } catch (_) { /* strip stays hidden */ }
  applyMetaStrip();
})();

// The header line under the controls: the account's counters, or — on the
// maxed-account basis — the SCENARIO the numbers are computed on, so a maxed
// run never reads as "your data".
function applyMetaStrip() {
  const strip = $("metastrip");
  if (!strip) return;
  if (maxedAccountOn()) {
    strip.textContent = "Scenario: " + MAXED_ACCOUNT_LABEL +
      " · every card maxed · holodori-comparable (not your account)";
    strip.hidden = false;
  } else {
    // the account's own counters, kept in `data-own` by the /api/meta fetch
    strip.textContent = strip.dataset.own || "";
    strip.hidden = !strip.textContent;
  }
}

let job = null;      // current job id
let source = null;   // current EventSource
let t0 = 0;          // client-side clock, for end-to-end latency
let firstProgressAt = null;
let ticker = null;

// The what-if the ranking was solved with, so every per-row call (timeline,
// board plan) scores the SAME stats and cooldown model as the headline.
const whatIf = () => ({
  assume_level: ($("assume_level") && $("assume_level").value) || "account",
  assume_bloom: ($("assume_bloom") && $("assume_bloom").value) || "account",
  uptime_model: ($("uptime") && $("uptime").value) || "finite",
  // AUTO premise: the server sets BOTH halves (auto column + combo=0)
  // from this one flag -- see `server._auto_premise`. Never send half.
  auto: !!($("autorun") && $("autorun").checked),
  platform: ($("platform") && $("platform").value) || "mobile",
});

// One run at a time, honestly. `runToken` identifies the run the user last
// asked for: a develop that finishes for an OLDER run must not repaint the
// page. `developPending` keeps the page in the running state while the
// develop fetch is in flight — otherwise the SSE stream's `end` would say
// "done" and re-enable Solve minutes before the numbers were final.
let runToken = 0;
let developPending = false;
let developAbort = null;

function setRunning(on) {
  el.solve.disabled = on;
  el.cancel.disabled = !on;
  if (on) {
    ticker = setInterval(() => {
      el.ptime.textContent = ((performance.now() - t0) / 1000).toFixed(1) + "s";
    }, 100);
  } else {
    clearInterval(ticker);
    ticker = null;
  }
}

function fail(message) {
  el.err.textContent = message;
  el.err.hidden = false;
}

// --- start / cancel --------------------------------------------------------
// Solve and "Estimate cost" share one path — Estimate just sets `estimate:true`,
// which makes the server report the projected cost and stop before enumerating.
async function runSolve(estimateOnly = false) {
  el.err.hidden = true;
  el.result.hidden = true;
  // The invitation has been accepted; it does not come back on a second run.
  if (el.idle) el.idle.hidden = true;
  el.progress.hidden = false;
  el.pstate.textContent = estimateOnly ? "estimating" : "starting";
  el.pcount.textContent = "";
  el.pnote.textContent = "";
  el.fill.style.width = "";
  el.fill.classList.add("sweep");
  t0 = performance.now();
  firstProgressAt = null;
  runToken += 1;
  if (developAbort) { developAbort.abort(); developAbort = null; }
  developPending = false;
  setRunning(true);

  // The ONE-job path, flag-gated:
  // localStorage.newrun = "1". Search → develop → refine → rank on the server,
  // one stepper, `render()` exactly once with the final payload.
  if (modeVal() !== "acquire" && !estimateOnly && newRunEnabled()) {
    await runViaJob();
    return;
  }

  // Next best card: its own endpoint, the same event stream. Board maps to
  // Current/Off (a develop choice acts as Current — the hint says so).
  if (modeVal() === "acquire") {
    let acq;
    try {
      const response = await fetch("/acquire", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          board: boardModeVal() !== "off",
          song: ($("song") && $("song").value) || "",
          pool: ($("pool") && $("pool").value) || "all",
          uptime_model: ($("uptime") && $("uptime").value) || "finite",
          // AUTO premise: the server sets BOTH halves (auto column + combo=0)
          // from this one flag -- see `server._auto_premise`. Never send half.
          auto: !!($("autorun") && $("autorun").checked),
          platform: ($("platform") && $("platform").value) || "mobile",
          assume_level: ($("assume_level") && $("assume_level").value) || "account",
          assume_bloom: ($("assume_bloom") && $("assume_bloom").value) || "account",
          // acquire pins: locked cards / leader ride along; the
          // candidate may fill any UNpinned slot of any seed team.
          require: getConstraints().require || [],
          leader: getConstraints().leader || [],
        }),
      });
      // A non-JSON body here is an OLD server that predates /acquire (or a
      // 500): say WHAT came back instead of dying inside JSON.parse.
      const raw = await response.text();
      try { acq = JSON.parse(raw); } catch (_) {
        setRunning(false);
        fail(`the server answered HTTP ${response.status} with ` +
             `"${raw.slice(0, 80)}" — restart tools/web/server.py so it ` +
             `carries the /acquire route, then hard-reload this page.`);
        return;
      }
    } catch (error) {
      setRunning(false);
      fail("could not reach the local server: " + error);
      return;
    }
    job = acq.job_id;
    listen(job);
    return;
  }

  let started;
  try {
    const response = await fetch("/solve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        top: Number(el.top.value) || 1,
        // "account" or "maxed". The server treats anything else as
        // "account" rather than guessing, because the two are answers
        // to different questions.
        basis: (el.basis && el.basis.value) || "account",
        // Absent means TRUE on the server; sent explicitly so the
        // page and the server can never disagree about the default. In a
        // develop mode the board is re-scored after this solve anyway, so
        // this only decides the transient first-draw and the `off` case.
        board: boardWanted(),
        // The chosen chart. "" is song-agnostic, same as absent;
        // sent explicitly so the request records
        // what was asked. But the RESPONSE's `sorted_by` — never this value —
        // decides how the answer is drawn: an older server ignores this key
        // and answers song-agnostically, and the page must render that
        // answer as the song-agnostic run it actually is.
        song: ($("song") && $("song").value) || "",
        // Candidate-pool filter: "all" or "5star". Absent -> "all" server-side.
        pool: ($("pool") && $("pool").value) || "all",
        // --exact forces the exhaustive search over the auto-chosen beam;
        // --estimate reports the projected cost and stops without enumerating.
        exact: !!(el.exact && el.exact.checked),
        estimate: estimateOnly,
        // --across-songs: rank by the mean expected score over every chart (+wins);
        // ignores the single song above when on.
        across_songs: !!(el.across && el.across.checked),
        // ACTIVE-uptime model: "finite" (in-run — realistic, default) | "steady"
        // (deck screen). The server validates and falls back to config.json otherwise.
        uptime_model: ($("uptime") && $("uptime").value) || "finite",
        // AUTO premise: the server sets BOTH halves (auto column + combo=0)
        // from this one flag -- see `server._auto_premise`. Never send half.
        auto: !!($("autorun") && $("autorun").checked),
        // Platform: "mobile" (FLICK 1050, default) | "pc" (FLICK 1000). The server
        // validates and falls back to mobile otherwise.
        platform: ($("platform") && $("platform").value) || "mobile",
        // Level/bloom what-if: "account" (default, no override) | a number | "max".
        // Only the account basis honours them; the server validates.
        assume_level: ($("assume_level") && $("assume_level").value) || "account",
        assume_bloom: ($("assume_bloom") && $("assume_bloom").value) || "account",
        // Search constraints (require/leader/exclude/include_unowned). A bad id
        // or a conflict comes back as an error event from the job, not a crash.
        ...getConstraints(),
      }),
    });
    started = await response.json();
  } catch (error) {
    setRunning(false);
    fail("could not reach the local server: " + error);
    return;
  }
  job = started.job_id;
  // The server clamps `top` to 1..50. If it clamped, say so rather than
  // letting the box keep claiming a number that was not honoured.
  if (started.top && started.top !== (Number(el.top.value) || 1)) {
    el.top.value = started.top;
  }
  listen(job);
}

// --- the ONE-job path: POST /api/run --------------------------------------
const newRunEnabled = () => {
  try { return localStorage.getItem("newrun") === "1"; } catch (_) { return false; }
};
let activeRun = null;      // { jobId, cancel } while /api/run is in flight

function runBody() {
  const primary = boardModeVal();
  const board = primary === "current" ? "current" : primary === "off" ? "off" : "develop";
  const leadersOn = board === "develop" && primary === "develop-leaders" && !pinned().any;
  const budget = leadersOn ? "full" : (devBudget() === "real" ? "real" : devBudget());
  const refineBox = document.getElementById("refine");
  const wideBox = document.getElementById("refinewide");
  const refine = refineBox && refineBox.checked && !pinned().members
    ? (wideBox && wideBox.checked ? "wide" : true) : false;
  const dt = document.getElementById("developtop");
  return {
    top: Number(el.top.value) || 10,
    basis: (el.basis && el.basis.value) || "account",
    board, budget, leaders: leadersOn, refine,
    develop_top: dt ? (Number(dt.value) || undefined) : undefined,
    song: ($("song") && $("song").value) || "",
    pool: ($("pool") && $("pool").value) || "all",
    exact: !!(el.exact && el.exact.checked),
    across_songs: !!(el.across && el.across.checked),
    uptime_model: ($("uptime") && $("uptime").value) || "finite",
    // AUTO premise: the server sets BOTH halves (auto column + combo=0)
    // from this one flag -- see `server._auto_premise`. Never send half.
    auto: !!($("autorun") && $("autorun").checked),
    platform: ($("platform") && $("platform").value) || "mobile",
    assume_level: ($("assume_level") && $("assume_level").value) || "account",
    assume_bloom: ($("assume_bloom") && $("assume_bloom").value) || "account",
    seeds: "all",       // seed rosters unioned into the develop set (seed_rosters.py)
    ...getConstraints(),
  };
}

async function runViaJob() {
  const token = runToken;
  // the stepper lives inside #progress, under the classic line
  let host = document.getElementById("runstepper");
  if (!host) {
    host = document.createElement("div");
    host.id = "runstepper";
    el.progress.append(host);
  }
  host.hidden = false;
  el.pstate.textContent = "starting";
  el.fill.classList.add("sweep");
  // the previous result stays, greyed, until the NEW final result replaces it
  if (lastPayload) { el.result.hidden = false; el.result.classList.add("stale"); }
  const unstale = (msg) => { el.result.classList.remove("stale"); if (msg) el.pnote.textContent = msg; };
  activeRun = await startRun(runBody(), {
    onPlan: (plan) => {
      el.pnote.textContent = plan.map((s) => s.label).join(" → ");
    },
    onStage: (ev) => {
      if (token !== runToken) return;
      el.pstate.textContent = `${ev.stage} ${ev.state}`;
    },
    onProgress: (ev, best) => {
      if (token !== runToken) return;
      if (typeof ev.evaluated === "number") el.pcount.textContent = num(ev.evaluated) + " arrangements evaluated";
      else if (typeof ev.done === "number") el.pcount.textContent = `${ev.stage} ${ev.done}/${ev.total || "?"}`;
      if (best !== null && best !== undefined) el.pnote.textContent = `best so far (developed Unit Score): ${num(best)}`;
    },
    onFact: (ev) => {
      if (token !== runToken) return;
      if (ev.type === "board" && !developMode()) {
        el.board.hidden = false;
        el.board.className = "banner" + (ev.loaded ? (ev.edited ? " edited" : "") : "");
        el.board.textContent = (ev.loaded ? "Scored against your board. " : "Scored with no board. ") + (ev.lines || []).join(" · ");
      }
      if (ev.text) el.pstate.textContent = ev.text;
    },
    onResult: (payload) => {
      if (token !== runToken) return;
      el.pstate.textContent = "done";
      el.fill.classList.remove("sweep");
      el.fill.style.width = "100%";
      unstale();
      // ONE render, of the final payload. `_developed` (set by the server when a
      // develop ran) keeps render() from kicking off the legacy develop pass.
      render(payload);
      renderRunHeader(payload);
    },
    onCancelled: (ev, detail) => {
      if (token !== runToken) return;
      el.pstate.textContent = `cancelled at ${ev.stage}` + (detail ? ` (${detail})` : "");
      el.fill.classList.remove("sweep");
      unstale(`Cancelled at ${ev.stage}${detail ? " (" + detail + ")" : ""} — the previous result is back on screen.`);
    },
    onError: (ev) => {
      if (token !== runToken) return;
      const stage = ev.stage || "run";
      fail(stage === "develop"
        ? `Develop failed: ${ev.message} — rerun with Boards = as captured (nothing board-off is shown under a Develop label).`
        : `${stage} failed: ${ev.message}`);
      el.pstate.textContent = "failed";
      el.fill.classList.remove("sweep");
      unstale();
    },
    onEnd: () => {
      if (token !== runToken) return;
      activeRun = null;
      setRunning(false);
    },
  }, host);
  if (!activeRun) { setRunning(false); unstale(); }
}

// The run header: what ran, on what, how long; warnings as amber pills;
// server notices in yellow. Rendered once with the final payload.
function renderRunHeader(payload) {
  let head = document.getElementById("runheader");
  if (!head) {
    head = document.createElement("section");
    head.id = "runheader";
    head.className = "runheader";
    el.result.prepend(head);
  }
  head.replaceChildren();
  const run = payload.run || {};
  const dev = payload.developed || {};
  const opts = run.opts || {};
  const song = payload.song ? (payload.song.title || payload.song.music_id) : (opts.across ? "every chart (mean)" : "any song");
  const secs = run.elapsed_ms ? (run.elapsed_ms / 1000) : null;
  const dur = secs === null ? "" : (secs >= 60 ? ` · ${Math.floor(secs / 60)} m ${Math.round(secs % 60)} s` : ` · ${secs.toFixed(1)} s`);
  const p = document.createElement("p");
  p.className = "lede";
  const scenario = payload.scenario || (payload.certificate_basis && payload.certificate_basis.basis === "maxed-account" ? { label: MAXED_ACCOUNT_LABEL } : null);
  p.textContent =
    `Best teams for ${song} · ` +
    (scenario ? `${scenario.label || MAXED_ACCOUNT_LABEL} · ` : "") +
    (payload._developed ? `developed boards, ${dev.budget}` : (opts.board === "current" ? "boards as captured" : "boards off")) +
    (run.searched ? ` · ${num(run.searched)} searched` : "") +
    (run.developed ? `, ${run.developed} developed` : "") +
    (run.refined ? `, ${run.refined} refined` : "") + dur;
  head.append(p);
  const warns = payload.warnings || [];
  if (warns.length) {
    const row = document.createElement("div");
    row.className = "runwarn-row";
    for (const w of warns) {
      const pill = document.createElement("span");
      pill.className = "runwarn";
      pill.textContent = w;
      row.append(pill);
    }
    head.append(row);
  }
  for (const n of payload.notice || []) {
    const q = document.createElement("p");
    q.className = "banner";
    q.textContent = n;
    head.append(q);
  }
}

// --- next-best-card result -------------------------------------------------
// Its own renderer: the answer is a list of CARDS (with the team each would
// join), not a list of teams, so the teams table would be the wrong shape.
function renderAcquire(payload) {
  const rows = payload.rows || [];
  el.result.hidden = false;
  // The teams-table legend describes a table this mode does not draw.
  const lede = $("ranklede"), key = $("rankkey");
  if (lede) lede.hidden = true;
  if (key) key.hidden = true;
  el.score.textContent = num(payload.baseline_score);
  el.lower.hidden = true;
  el.teamcount.textContent = `${rows.length} candidate cards`;
  el.gatecount.textContent = "";
  el.scanned.textContent = "";
  el.elapsed.textContent = (payload.elapsed_ms / 1000).toFixed(1) + "s";
  el.rankhint.textContent = payload.song
    ? `gain on ${payload.song} (expected run score)`
    : "gain in Unit Score (song-agnostic)";
  const say = document.querySelector(".scorebar .say");
  if (say) say.textContent =
    "Baseline: your best OWNED team under these options. Each candidate is " +
    "scored at MAX level/bloom — what it could do once built.";
  el.approx.hidden = true;
  el.board.hidden = true;
  if (el.diagwrap) el.diagwrap.hidden = true;

  el.ranking.textContent = "";
  const faces = payload.cards || {};
  const list = document.createElement("ol");
  list.className = "acq-list";
  rows.forEach((row) => {
    const li = document.createElement("li");
    li.className = "acq-row";
    const img = document.createElement("img");
    img.className = "acq-face";
    img.loading = "lazy";
    img.src = "/cardart/thumb/" + row.card + "?w=96";
    img.onerror = () => { img.remove(); };
    const body = document.createElement("div");
    body.className = "acq-body";
    const name = document.createElement("b");
    const face = faces[row.card] || {};
    name.textContent = `${row.member} — ${face.name || face.title || row.card}`;
    const gain = document.createElement("span");
    gain.className = "acq-gain" + (row.gain > 0 ? " up" : "");
    gain.textContent = (row.gain > 0 ? "+" : "") + num(row.gain) +
      ` (${row.gain_pct > 0 ? "+" : ""}${row.gain_pct}%)`;
    const where = document.createElement("span");
    where.className = "acq-where";
    where.textContent = row.gain > 0
      ? `→ ${num(row.score)} in ${row.leader_member}'s team`
      : "does not beat your current best team";
    body.append(name, gain, where);
    li.append(img, body);
    list.append(li);
  });
  el.ranking.append(list);
}

el.solve.addEventListener("click", () => runSolve(false));
if (el.estimate) el.estimate.addEventListener("click", () => runSolve(true));

el.cancel.addEventListener("click", async () => {
  if (activeRun) {
    el.pstate.textContent = "cancelling after the current team";
    activeRun.cancel();
    return;
  }
  if (developAbort) {
    // the develop pass is a plain fetch: aborting it leaves the board-off
    // ranking on screen, clearly labelled as such by finishDevelop().
    el.pstate.textContent = "cancelling develop";
    developAbort.abort();
    return;
  }
  if (!job) return;
  el.pstate.textContent = "cancelling";
  await fetch("/cancel/" + job, { method: "POST" });
});

// --- the SSE stream --------------------------------------------------------
function listen(jobId) {
  source = new EventSource("/events/" + jobId);
  source.onmessage = (message) => {
    const event = JSON.parse(message.data);
    switch (event.type) {
      case "status":
        el.pstate.textContent = event.message;
        break;

      // WHICH BOARD was scored. With two sources this matters: an EDITED
      // board in state/collection.json supersedes the capture, so a stale
      // edit would silently answer a different question. The account's
      // actual board and a hypothetical one must never be confused, and a
      // ranking that does not say which it used cannot honour that.
      case "board":
        // In a develop mode this stage-1 banner ("captured board…") is both
        // redundant and misleading — the ranking is re-scored on each team's
        // DEVELOPED board, and the headline already names that. Hide it.
        if (developMode()) { el.board.hidden = true; break; }
        el.board.hidden = false;
        // Which of the three this is decides the colour, and only one of them
        // is a warning: NO board loaded means every board term scored zero,
        // which changes the answer. A board that loaded -- captured or edited
        // -- is a statement of fact and stays neutral.
        // ...unless the user CHOSE no board: then it is a fact, not a warning.
        const choseOff = boardModeVal() === "off";
        el.board.className = "banner"
          + (event.loaded ? (event.edited ? " edited" : "") : (choseOff ? "" : " warn"));
        el.board.textContent = event.loaded
          ? (event.edited
              ? "Scored against your EDITED board from state/collection.json, " +
                "which supersedes the captured one. "
              : "Scored against your captured board. ") +
            (event.lines || []).join(" · ")
          : (choseOff
              ? "Scored with NO board, as you chose — every board term is zero by design. "
              : "No board loaded — every board term scores as zero. ") +
            (event.lines || []).join(" · ");
        break;

      case "projection":
        if (event.solver === "acquire-sweep") {
          el.pstate.textContent = "trying candidates";
          el.pnote.textContent =
            `${num(event.set_count)} unowned candidate cards × every seed ` +
            `team's six roles (${num(event.arrangement_count)} evaluations)`;
          break;
        }
        el.pstate.textContent =
          event.solver === "heuristic" ? "beam search" : "exact search";
        // The projection is the EXHAUSTIVE cost. Saying so matters: it is
        // 4.2 billion arrangements against the beam's ~228 thousand, and a
        // number must not be quoted without what it was measured over.
        el.pnote.textContent =
          `pool ${event.pool_size} cards (${event.owned_count} owned) · ` +
          `the exhaustive search over this pool is ` +
          `${num(event.arrangement_count)} arrangements` +
          (event.projected_seconds
            ? `, projected ${num(event.projected_seconds)}s`
            : "");
        break;

      // --estimate: the projection is the whole answer; the server stopped
      // before enumerating. `arrangement_count`/`projected_seconds` are the
      // EXHAUSTIVE cost; `solver` says what Solve will ACTUALLY run — the beam
      // (fast, approximate) when that exhaustive cost is over the threshold, or
      // the exhaustive search itself when it is small (or Exhaustive is ticked).
      case "estimate": {
        el.pstate.textContent = "estimate only";
        el.fill.classList.remove("sweep");
        el.fill.style.width = "0";
        const secs = event.projected_seconds;
        const time = secs
          ? ` (~${num(secs)}s` +
            (secs > 3600 ? ` ≈ ${(secs / 3600).toFixed(1)} h` : "") + ")"
          : "";
        el.pnote.textContent = event.solver === "heuristic"
          ? `Estimate only — not solved. The EXHAUSTIVE search over this pool is ` +
            `${num(event.arrangement_count)} arrangements${time} — over the ` +
            `threshold, so Solve runs the fast beam search instead (approximate, ` +
            `seconds). Tick Exhaustive to force the full search; press Solve to run.`
          : `Estimate only — not solved. Solve will run the EXHAUSTIVE search: ` +
            `${num(event.arrangement_count)} arrangements${time}. Press Solve to run it.`;
        break;
      }

      case "progress": {
        if (firstProgressAt === null) {
          firstProgressAt = performance.now() - t0;
          el.pnote.textContent +=
            ` · first progress at ${firstProgressAt.toFixed(0)} ms`;
        }
        el.pcount.textContent = num(event.evaluated) +
          (modeVal() === "acquire" ? " candidates tried" : " arrangements evaluated");
        if (event.complete) {
          el.fill.classList.remove("sweep");
          el.fill.style.width = "100%";
          el.pstate.textContent = "scoring";
        }
        break;
      }

      case "result":
        el.pstate.textContent = "done";
        render(event.payload);
        break;

      case "cancelled":
        el.pstate.textContent = "cancelled";
        el.fill.classList.remove("sweep");
        el.fill.style.width = "0";
        el.pnote.textContent =
          `cancelled after ${num(event.elapsed_ms)} ms — the solver stopped, ` +
          `it was not left running`;
        break;

      case "error":
        fail(event.message);
        el.pstate.textContent = "failed";
        el.fill.classList.remove("sweep");
        break;

      case "end":
        source.close();
        source = null;
        job = null;
        // Not done yet if the develop pass is running: it re-scores every team
        // on its developed board and re-renders — saying "done" here would let
        // the page change under the user minutes later.
        if (developPending) {
          el.pstate.textContent = "developing boards";
          el.fill.classList.add("sweep");
        } else {
          setRunning(false);
        }
        break;
    }
  };
  source.onerror = () => {
    if (source) { source.close(); source = null; }
    if (!developPending) setRunning(false);
  };
}

// --- the beam's own diagnostics -------------------------------------------
// Measured over 134 real misses: not one of the seven
// failure labels has lift above 1, and three fire MORE often on correct
// answers (`beam_truncation` 0.49x, `open_condition_rescue` 0.38x,
// `seed_disagreement` 0.48x). So they are shown, folded away, as what the
// beam did — never as a warning that the answer is wrong, which is a claim
// the measurement does not support. The APPROXIMATE banner above is a
// different thing and stays loud: that one IS true of every beam result.
function renderDiagnostics(search) {
  if (!search || !search.approximate) {
    el.diagwrap.hidden = true;
    return;
  }
  el.diagwrap.hidden = false;
  el.diagcaveat.textContent =
    "These are diagnostics, not warnings. Checked over " +
    "134 real misses from a deliberately weakened beam: not one of the seven " +
    "labels has lift above 1, and three fire more often on correct answers " +
    "(beam_truncation 0.49×, open_condition_rescue 0.38×, seed_disagreement " +
    "0.48×). 130 of the 134 misses carried a label set identical to some " +
    "successful run's. Read them as a record of what the search did; do not " +
    "read a label as evidence that this answer is wrong.";

  const counts = new Map();
  for (const observation of search.observations || []) {
    counts.set(observation.mode, (counts.get(observation.mode) || 0) + 1);
  }
  const lines = [];
  for (const mode of search.failure_modes || []) {
    lines.push(`[${mode}] x${counts.get(mode) || 0}`);
    const first = (search.observations || []).find((o) => o.mode === mode);
    if (first && first.detail) lines.push("    e.g. " + first.detail);
  }
  el.diag.textContent = lines.join("\n");
}

// --- render ----------------------------------------------------------------
function render(payload) {
  if (payload && payload.mode === "acquire") { renderAcquire(payload); return; }
  { // a run header belongs to a /api/run payload only; a legacy render drops it
    const rh = document.getElementById("runheader");
    if (rh && !(payload && payload.run)) rh.remove();
  }
  { // the acquire renderer hides the teams-table legend; a solve restores it.
    const lede = $("ranklede"), key = $("rankkey");
    if (lede) lede.hidden = false;
    if (key) key.hidden = false;
  }
  const teams = payload.teams || [];
  if (!teams.length) { fail("the solver returned no team"); return; }
  // the second sort order: the ceiling (every active fires) when chosen
  if ($("sortby") && $("sortby").value === "max" && teams.every((t) => typeof t.song_max === "number")) {
    teams.sort((a, b) => b.song_max - a.song_max);
    teams.forEach((t, i) => { t.rank = i + 1; });
  }
  const best = teams[0];

  // ⛔ ONE accessor, chosen by the payload's own `sorted_by`, feeds the big
  // number, every row, the vs-#1 deltas, the %, the bars and the noise band.
  // A table sorted by one number while it displays another is an easy
  // failure to ship; routing every score-quantity read through `metric.read`
  // makes the match structural, not something each call site remembers. A
  // payload without `sorted_by` is a song-agnostic run.
  const metric = metricOf(payload);
  const songMode = metric.field !== "score";
  const unreadable = teams.filter((t) => typeof metric.read(t) !== "number").length;
  if (unreadable) {
    // A payload that claims one ordering but cannot show that field is a
    // contract break; drawing anything would put the wrong number on screen.
    fail(
      `the payload claims sorted_by="${metric.field}" but ${unreadable} ` +
      `team(s) carry no such number — refusing to draw a table whose order ` +
      `and figures would disagree`
    );
    return;
  }

  el.score.textContent = num(metric.read(best), 1);
  el.lower.hidden = !best.is_lower_bound;

  // Which quantity the big number IS, said in words beside it. Written
  // per-run from the payload so it can never describe a different run than
  // the one on screen; in song-agnostic mode the element is REMOVED, because
  // index.html's static copy already describes that mode and a stale
  // per-song claim over an agnostic number would be exactly the confusion
  // this line exists to prevent.
  let qual = document.getElementById("scorequal");
  if (songMode) {
    if (!qual) {
      qual = document.createElement("p");
      qual.id = "scorequal";
      qual.className = "banner";
      const big = el.score.closest(".big");
      if (big && big.parentNode) big.parentNode.insertBefore(qual, big.nextSibling);
      else el.result.prepend(qual);
    }
    const across = payload.across;
    if (across) {
      const bestWins = best.wins || 0;
      const devNote = payload.developed
        ? ` <span class="scoremax-lab">· each team's board developed ` +
          `(${payload.developed.budget}); wins counted among these teams</span>`
        : "";
      qual.innerHTML =
        `<b>Mean per-song score</b> over ${across.chart_count} charts — expected ` +
        `procs.${devNote} #1 tops <b>${bestWins}</b> of them (the "wins" count per ` +
        `row; a team can lead by mean yet win few).`;
    } else {
      // Concise: the big number is the estimate (expected procs); the ceiling (every
      // active fires, best on overlap) is fetched for #1 and appended when it lands.
      const song = payload.song || {};
      const songId = ($("song") && $("song").value) || song.music_id || "";
      const name = song.title || song.music_id || "the song";
      // When boards were developed the context is named, so the number reads as
      // "this team's score once its board is built", not against some other board.
      const devNote = payload.developed
        ? ` <span class="scoremax-lab">· each team's board developed ` +
          `(${payload.developed.budget})</span>`
        : "";
      qual.innerHTML =
        `<b>Per-song score for ${name} (${song.difficulty || "EXPERT"})</b> — ` +
        `estimated, expected procs.${devNote} ` +
        `<span id="scoremax" class="scoremax"></span>`;
      // Developed render already carries the ceiling per team (song_max) — use it
      // directly rather than a second /api/timeline round-trip.
      if (payload._developed && typeof best.song_max === "number") {
        const s = document.getElementById("scoremax");
        if (s) s.innerHTML =
          `· <span class="scoremax-lab">max if all actives fire</span> ` +
          `<b>${num(best.song_max)}</b>`;
      }
      const t0 = (payload.teams || [])[0];
      if (!payload._developed && t0 && songId) {
        fetch("/api/timeline", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            song: songId,
            cards: [t0.leader_id, ...(t0.member_order || t0.member_ids || [])],
            basis: el.basis ? el.basis.value : "account",
            board: boardWanted(),
          }),
        }).then((r) => r.json()).then((d) => {
          const s = document.getElementById("scoremax");
          if (s && d && d.max_score) {
            s.innerHTML = `· <span class="scoremax-lab">max if all actives fire</span> ` +
              `<b>${num(Math.round(d.max_score))}</b>`;
          }
        }).catch(() => {});
      }
    }
  } else if (qual) {
    qual.remove();
  }

  const fired = best.gates.filter((g) => g.fired).length;
  el.teamcount.textContent =
    `${teams.length} team${teams.length === 1 ? "" : "s"} ranked`;
  el.gatecount.textContent =
    `#1 met ${fired} of ${best.gates.length} skill conditions`;
  el.gatecount.title =
    "A skill condition (a “gate”) is something a card's skill demands of the " +
    "finished team. Unmet, that skill pays nothing.";
  el.elapsed.textContent = `solve ${num(payload.elapsed_ms)} ms`;
  const search = payload.search;
  el.scanned.textContent = search
    ? `${num(search.arrangements_scored)} arrangements scored (${search.mode})`
    : `${num(payload.pool && payload.pool.arrangement_count)} arrangements`;

  // The approximation banner is not optional chrome. The CLI prints it to
  // BOTH stdout and stderr precisely so a ranking a reader scrolls straight
  // to carries its own caveat; the same reasoning applies on screen. It does
  // NOT recite the failure labels as if they meant the answer is suspect —
  // that claim is refuted, and they live in the diagnostics panel with the
  // number that refutes them.
  if (search && search.approximate) {
    el.approx.hidden = false;
    el.approx.className = "banner warn";
    // One line; the numbers and the "true optimum not guaranteed" caveat move to
    // the tooltip. The band below already shows the whole spread sits inside the
    // RNG noise, so the beam-vs-exact distinction rarely changes the answer.
    el.approx.textContent =
      `Beam search (fast, not exhaustive) — tick “Exhaustive” to search all teams.`;
    el.approx.title =
      `beam_width=${search.beam_width} branch=${search.branch} seeds=${search.seeds}; ` +
      `${num(search.arrangements_scored)} arrangements scored against an exhaustive ` +
      `${num(search.exhaustive_arrangement_count)}. The true optimum may not be in ` +
      `this list, and no ordering below is proof it is — but the spread below is ` +
      `inside the active-skill RNG band, so an exact search rarely reorders anything ` +
      `you could observe in play.`;
  } else {
    el.approx.hidden = true;
    el.approx.title = "";
  }

  const summary = renderRanking(el.ranking, payload);
  // In song mode the spread is NAMED, because "points" of two different
  // quantities on two different scales is how a reader mixes them up.
  el.rankhint.textContent = summary
    ? `— ${summary.count} teams, best to worst, ` +
      `${num(summary.spread, 1)} points` +
      (songMode ? ` of ${summary.label}` : "") +
      ` between first and last`
    : "";
  // The band is drawn ABOVE the sentence that explains it, and the sentence
  // stays: the graphic is the glance, the prose is the evidence, and the
  // second is not traded for the first.
  // ⚠ Both take the SAME metric the table is sorted by: a noise band computed
  // over `score` under a table ordered by `song_score` would quietly describe
  // a different ranking than the one on screen.
  el.noise.textContent = separabilityNote(teams, metric);
  el.noise.title = separabilityDetail();
  el.noise.hidden = teams.length < 2;
  const oldBand = document.getElementById("noiseband");
  if (oldBand) oldBand.remove();
  const band = separabilityBand(teams, metric);
  if (band) {
    band.id = "noiseband";
    el.noise.parentNode.insertBefore(band, el.noise);
  }

  renderDiagnostics(search);

  el.assume.textContent = [
    ...(payload.assumptions || []),
    "",
    ...(payload.approximation || []),
  ].join("\n");

  el.result.hidden = false;
  lastPayload = payload;
  // In a develop mode the ranking above is the transient board-off order; kick
  // off the board-develop pass, which re-scores every team on its OWN developed
  // board and RE-RENDERS in the developed order. Skip the timeline/board-plan
  // draws on this first pass — they'd only be redrawn a moment later with the
  // developed numbers. `off`/`current` (developMode() null) draw straight away.
  if (developMode() && !payload._developed) {
    developPending = true;
    if (el.ranking) el.ranking.classList.add("stale");
    const token = runToken;
    developAndRerank(payload, token).finally(() => finishDevelop(payload, token));
    return;
  }
  if (el.ranking) el.ranking.classList.remove("stale");
  drawTimeline();
  drawBoardPlan();
}

// The develop pass is over (developed, failed, or aborted): the run is done
// NOW, not when the SSE stream closed. Whatever is on screen is labelled by
// its own banner (developed order / board-off order with the reason).
function finishDevelop(payload, token) {
  if (token !== runToken) return;   // an older run's develop; the page belongs to a newer one
  developPending = false;
  developAbort = null;
  if (el.ranking) el.ranking.classList.remove("stale");
  if (payload && !payload._developed) {
    // still the board-off ranking (develop failed/aborted): draw its extras now
    drawTimeline(); drawBoardPlan();
  }
  if (!source) {
    el.pstate.textContent = payload && payload._developed ? "done" : "done (board-off)";
    el.fill.classList.remove("sweep");
    setRunning(false);
  }
}

// Develop every ranked team's best board (fast Engine-A, /api/optimize-rerank)
// and RE-RANK by the developed-board score, then re-render. In song mode the
// re-rank is by the PER-SONG score on the chosen chart (the question the user
// asked); song-agnostic mode re-ranks by the developed Unit Score. This is the
// default flow — a team the board-off solve placed lower can rise once its
// board is built, which is exactly why developing before ranking is the point.
async function developAndRerank(payload, token) {
  const mode = developMode();
  if (!mode || payload._developed) return;
  const superseded = () => token !== undefined && token !== runToken;
  // "leaders" IGNORES the solved teams: the server generates the best team per
  // leader (one beam, per_leader_best) and develops those — so the solve's
  // monoculture top-N is replaced by a leader-diverse ranking.
  const isLeaders = mode === "leaders";
  const teams = (payload.teams || [])
    .map((t) => [t.leader_id, ...(t.member_ids || [])].filter(Boolean))
    .filter((ids) => ids.length === 6);
  if (!isLeaders && !teams.length) return;   // finishDevelop draws the extras
  const song = ($("song") && $("song").value) || "";
  // Across-songs: develop, then rank by the MEAN per-song score over EVERY chart
  // against the developed board (not Unit Score). Mutually exclusive with a song.
  const across = !!(el.across && el.across.checked) && !song;
  const body = { basis: el.basis ? el.basis.value : "account", teams };
  if (song) body.song = song;
  if (across) body.across = true;
  // Swap-refine: hill-climb each shown team's members by the target metric —
  // per-song score in song mode, a sampled across mean in across mode, the
  // developed Unit Score in any-song mode. Opt-in: it adds ~40 s (song) to
  // ~2-3 min (wide) on top of the develop.
  const refineBox = document.getElementById("refine");
  const wideBox = document.getElementById("refinewide");
  const skipped = (boardModeVal() === "develop-leaders" && pinned().any)
    || (refineBox && refineBox.checked && pinned().members);
  if (refineBox && refineBox.checked && !pinned().members) {
    body.refine = wideBox && wideBox.checked ? "wide" : true;
  }
  // every-leader honours the board-points choice too: earned points (no
  // budget sent) or the flat max-points budget.
  if (isLeaders) { body.leader_diverse = true; if (devBudget() !== "real") body.budget = devBudget(); }
  else if (mode === "max") body.budget = devBudget() === "real" ? 453 : devBudget();
  // Seed rosters (server: seed_rosters.py): known-strong rosters are UNIONED
  // with the candidates and developed/ranked the same way, so a roster the
  // beam never generates cannot hide. Always on from the UI; the
  // seeds respect the same Ban / Must-include / Leader pins as the search.
  body.seeds = "all";
  Object.assign(body, getConstraints());
  // Send the SAME level/bloom what-if the solve used, or the develop re-scores the
  // maxed-solved teams on their real (often level-1) stats — the numbers then come
  // out BELOW the board-off ranking. Mirrors the /solve body exactly.
  body.assume_level = ($("assume_level") && $("assume_level").value) || "account";
  body.assume_bloom = ($("assume_bloom") && $("assume_bloom").value) || "account";
  // Same score model (in-run vs deck-screen) the solve used, or the develop
  // re-scores with a different uptime than the ranking above it.
  body.uptime_model = ($("uptime") && $("uptime").value) || "finite";
  body.auto = !!($("autorun") && $("autorun").checked);
  // Same platform (flick weight) the solve used, for the same reason.
  body.platform = ($("platform") && $("platform").value) || "mobile";
  body.materials = materialsVal();
  Object.assign(body, leaderOptions());

  // A banner above the (board-off) ranking so the reorder is not a silent pause.
  let status = document.getElementById("developstatus");
  if (!status) {
    status = document.createElement("p");
    status.id = "developstatus";
    status.className = "banner";
    if (el.ranking && el.ranking.parentNode) {
      el.ranking.parentNode.insertBefore(status, el.ranking);
    }
  }
  status.textContent = isLeaders
    ? `Building the best team for EVERY leader and developing each ` +
      `${song ? "on this chart" : across ? "across all songs" : ""}… (slow — one team per leader)`
    : `Developing each team's board from scratch ` +
      `(${mode === "max"
          ? (devBudget() === 453 || devBudget() === "real"
              ? "full tree" : `${devBudget()} pt budget`)
          : "my points"}) ` +
      `and re-ranking${song ? " on this chart" : across ? " across all songs" : ""}…`;
  if (skipped) status.textContent += PINS_NOTE;

  const key = (ids) => [...ids].sort().join("|");
  try {
    developAbort = new AbortController();
    const res = await fetch("/api/optimize-rerank", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body), signal: developAbort.signal,
    });
    if (superseded()) return;   // a newer run owns the page now
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try { const e = await res.json(); if (e.error) msg = e.error; } catch (_) {}
      status.className = "banner warn";
      status.textContent = `Board develop failed (${msg}) — showing the board-off ranking (boards NOT developed).`;
      return;   // finishDevelop draws the extras
    }
    const doc = await res.json();
    if (superseded()) return;
    const rows = doc.ranking || [];
    const songField = doc.sorted_by === "song_est";
    const acrossField = doc.sorted_by === "across_song";
    // leader_diverse: the rows are teams the solve never showed, so REBUILD the
    // ranking from them (they carry gates/rosters/scores). Display the user's
    // chosen count, best first.
    // Merge the faces for cards the solve never displayed (leader_diverse
    // teams, refined-in members), or they render as "(unknown card)".
    if (doc.cards) payload.cards = Object.assign({}, payload.cards || {}, doc.cards);
    // certificate provenance of the DEVELOPED response
    if (doc.model_version) payload.model_version = doc.model_version;
    if ("inputs_hash" in doc) payload.inputs_hash = doc.inputs_hash;
    if (doc.certificate_basis) payload.certificate_basis = doc.certificate_basis;
    if (doc.objective) payload.objective = doc.objective;
    // a developed row as a ranking team (leader_diverse rows and seeded rows
    // are teams the solve never showed, so they are rebuilt from the row)
    const rowToTeam = (r, i) => {
      const t = {
        rank: i + 1, leader_id: r.leader_id, member_ids: r.member_ids || [],
        member_order: r.member_order, gates: r.gates || [],
        board_state: r.board_state, wins: r.wins,
        developed_points: r.points, developed_gain: r.gain_pct, is_lower_bound: false,
        refined: r.refined, provenance: r.provenance, certificate: r.certificate,
        board_plan_summary: r.board_plan_summary,
      };
      whatifFields(t, r);
      if (songField) {
        t.song_score = r.song_est; t.song_max = r.song_max;
        t.developed_off = r.song_off;
        if (typeof r.song_est_develop === "number") t.song_score_develop = r.song_est_develop;
        t.active_cover_pct = r.active_cover_pct;
        t.active_overlap_loss_pct = r.active_overlap_loss_pct;
        t.active_dead_start_s = r.active_dead_start_s;
      }
      else if (acrossField) {
        t.across_score = r.across_score;
        if (typeof r.across_score_develop === "number") t.across_score_develop = r.across_score_develop;
      }
      else { t.score = r.optimised_us; t.developed_off = r.board_off_us; }
      return t;
    };
    // The plan-cache objective's labelled what-if: the
    // per-team greedy re-develop's number and board ride on the row beside
    // the objective's; absent on the legacy `objective_leaf: "develop"` rows.
    function whatifFields(t, r) {
      for (const k of ["objective_leaf", "develop_whatif", "board_state_develop",
                       "board_plan_summary_develop", "points_develop"]) {
        if (r[k] !== undefined) t[k] = r[k];
      }
    }
    if (isLeaders) {
      const displayTop = Math.max(1, Number(el.top && el.top.value) || 10);
      payload.teams = rows.slice(0, displayTop).map(rowToTeam);
      payload._developed = true;
      payload.developed = { mode: "leaders", budget: "every leader · "
        + (devBudget() === "real" ? "earned points" : `${devBudget()} pt per character`)
        + (materialsVal() === "infinite" ? " · unlimited cubes/hexas" : "") };
      status.remove();
      render(payload);
      return;
    }
    const byRoster = new Map(rows.map((r) => [key(r.cards), r]));
    const claimed = new Set();
    // A swap-refined row no longer matches the roster the solve posted; the
    // leader is fixed by the refine, so it is claimed by leader instead and the
    // team's members are REPLACED by the refined ones (otherwise a refined
    // team would silently keep its board-off numbers and the refine be lost).
    const refinedByLeader = new Map();
    for (const r of rows) {
      if (Array.isArray(r.refined) && r.refined.length && r.leader_id) {
        refinedByLeader.set(r.leader_id, r);
      }
    }
    for (const t of payload.teams) {
      let r = byRoster.get(key([t.leader_id, ...(t.member_ids || [])]));
      if (!r && refinedByLeader.has(t.leader_id)) {
        r = refinedByLeader.get(t.leader_id);
        refinedByLeader.delete(t.leader_id);
        t.member_ids = r.member_ids || t.member_ids;
        if (r.gates) t.gates = r.gates;
        t.refined = r.refined;
      }
      if (!r) continue;
      claimed.add(r);
      if (r.provenance) t.provenance = r.provenance;
      whatifFields(t, r);
      if (songField) {
        t.song_score = r.song_est;      // the metric the table reads in song mode
        t.song_max = r.song_max;        // the "all actives fire" ceiling per team
        t.developed_off = r.song_off;
        if (typeof r.song_est_develop === "number") t.song_score_develop = r.song_est_develop;
        t.active_cover_pct = r.active_cover_pct;
        t.active_overlap_loss_pct = r.active_overlap_loss_pct;
        t.active_dead_start_s = r.active_dead_start_s;
        if (r.member_order) t.member_order = r.member_order;
      } else if (acrossField) {
        t.across_score = r.across_score; // the metric across-songs mode reads (mean)
        t.wins = r.wins;                 // charts topped among these developed teams
        if (typeof r.across_score_develop === "number") t.across_score_develop = r.across_score_develop;
      } else {
        t.score = r.optimised_us;       // the metric in song-agnostic mode
        t.developed_off = r.board_off_us;
      }
      t.board_state = r.board_state;     // drives each team's developed timeline
      t.developed_gain = r.gain_pct;
      t.developed_points = r.points;
      t.board_plan_summary = r.board_plan_summary;   // the row's board section
      // the developed row's certificate supersedes the stage-1 one: same
      // hash, but the basis (flat/real budget) is the developed one.
      if (r.certificate) t.certificate = r.certificate;
    }
    // Seeded rows the solve never posted: developed and ranked like the rest,
    // so they join the list (tagged "seeded" by their provenance).
    for (const r of rows) {
      if (!claimed.has(r) && r.provenance) payload.teams.push(rowToTeam(r, payload.teams.length));
    }
    const read = songField ? (t) => t.song_score
      : acrossField ? (t) => t.across_score : (t) => t.score;
    payload.teams.sort((a, b) => read(b) - read(a));
    payload.teams.forEach((t, i) => { t.rank = i + 1; });
    payload._developed = true;
    payload.developed = {
      mode, budget: mode === "max"
        ? (devBudget() === 453 || devBudget() === "real"
            ? "full tree, from scratch"
            : `${devBudget()} pt per character, from scratch`)
        : "my points, from scratch",
    };
    if (skipped) payload.developed.budget += " · locks honoured, every-leader / refine skipped";
    status.remove();
    render(payload);   // re-render in the developed order (guarded: _developed)
  } catch (error) {
    if (superseded()) return;
    status.className = "banner warn";
    status.textContent = error && error.name === "AbortError"
      ? "Board develop cancelled — showing the board-off ranking (boards NOT developed)."
      : `Board develop failed to load (${error}) — showing the board-off ranking.`;
  }
}

// --- the board plan under a result -----------------------------------------
//
// The "Board plan" shown inline under the ranking, so a solve answers "who to
// field" AND "what board to build for them" in one place. Built on demand for the
// exact team via /api/board-plan-fast (Engine A, ~seconds) or /api/boardplan
// (tree_optimise, ~90s); a failure is a first-class state with the reason, never
// a broken-looking blank.
const note = (text) => {
  const p = document.createElement("p");
  p.className = "note";
  p.textContent = text;
  return p;
};

// The board plan is computed FOR THE TEAM THIS SOLVE WON (leader + 5), not a
// precompute's own best team — the winner depends on the board toggle / basis,
// so a generic precompute could show a plan for a different leader. It POSTs
// the exact team to /api/boardplan, which runs `tree_optimise --for-team`. That
// is a beam search (~1-2 min), so it is a deliberate button, not an auto-run on
// every solve — the ranking stays instant and the plan is opt-in per team.
// One team's board plan — TWO tiers, both opt-in per team (winner or any
// runner-up), both rendering the same tree into one output box:
//   FAST (default): /api/board-plan-fast — Engine A, in-process greedy, ~2s, on
//     the EXACT deck-screen Unit Score the ranking uses. Shows the developed board.
//   FULL: /api/boardplan — tree_optimise beam, ~90s, adds respec refunds, cube/
//     hexa affordability, socket-card picks and per-song tuning.
function boardPlanButton(team) {
  const host = document.createElement("section");
  host.className = "resultplan";
  const cards = [team.leader_id, ...(team.member_ids || [])].filter(Boolean);
  if (cards.length !== 6) return host;

  const out = document.createElement("div");
  const row = document.createElement("div");
  row.className = "planbtn-row";

  // PRIMARY: the correct plan (tree_optimise) — a knapsack over node POINTS and the
  // cube/hexa currencies, so it never proposes a board you cannot afford. Two
  // toggles set the resources it may spend.
  const fullBtn = document.createElement("button");
  fullBtn.type = "button";
  fullBtn.className = "planbtn";
  fullBtn.textContent = "Board to build (~25s, optimal)";
  const mkSel = (title, opts) => {
    const s = document.createElement("select");
    s.className = "planmode";
    s.title = title;
    opts.forEach(([v, l]) => {
      const o = document.createElement("option");
      o.value = v; o.textContent = l; s.append(o);
    });
    return s;
  };
  // Character level -> node points; materials -> cubes/hexas. The two axes.
  const levelSel = mkSel(
    "Character level sets the node points each holomem can spend",
    [["current", "My levels"], ["max", "Max level (50)"]]);
  const resSel = mkSel(
    "The cube/hexa materials the plan may spend",
    [["inventory", "My materials"], ["infinite", "Infinite resources"]]);

  // SECONDARY: the fast Engine-A estimate — POINTS ONLY, ignores cube/hexa costs,
  // so it can list more than you can build. A quick preview, not the buy list.
  const fastBtn = document.createElement("button");
  fastBtn.type = "button";
  fastBtn.className = "planbtn ghost";
  fastBtn.textContent = "Quick estimate (points only, ~8s)";

  const hint = note(
    "Board to build: the real plan, now PROVABLY OPTIMAL — a HiGHS solver picks the " +
    "best possible nodes for this team's boards under your POINTS (character level) AND " +
    "your cube/hexa MATERIALS, so every node is affordable, plus the Connect-card picks. " +
    "~25s (develops the team's boards AND the account-global GREEN on your other holomems), and it beats the old ~90s beam. Quick estimate: a fast preview (~8s) " +
    "that counts POINTS ONLY for the nodes (ignores cube/hexa costs), so it can propose " +
    "more than you can actually build — use Board to build for the buy list. It places " +
    "Connect cards across every board, including the free root socket of the ~50 bench " +
    "holomems whose green is account-global — a cheap stat gain you own already."
  );
  row.append(fullBtn, levelSel, resSel, fastBtn);
  host.append(row, hint, out);

  const basisOf = () => (el.basis ? el.basis.value : "account");
  const songOf = () => ($("song") && $("song").value) || "";
  const fail = (msg) => { out.textContent = ""; out.append(note(msg)); };
  const renderDoc = async (doc) => {
    out.textContent = "";
    if (doc.plan_included === false) {
      out.append(note(
        "The planner produced no board for this team, so nothing is shown " +
        "rather than a wrong plan. Check the server log for the reason."
      ));
      return;
    }
    // Under a developed row the plan is a DIFF against the board the row was
    // scored on — one board per row, anything else labelled against it.
    if (team.board_state) {
      const diff = planDiffNote(doc, team.board_state);
      if (diff) out.append(diff);
    }
    out.append(boardPlanSection(doc));
    // Optimising FOR A SONG: regenerate the timeline against THIS optimised board
    // (its cooldown/activation nodes tighten the active windows) and show the
    // board-aware song score the timeline scores. Absent song / non-solver plan skips.
    const song = songOf();
    if (song && doc.optimised_board_state) {
      const tl = document.createElement("div");
      tl.className = "plan-tl";
      tl.append(note("Regenerating the timeline for this optimised board…"));
      out.append(tl);
      // Fold the plan's recommended Connect cards INTO the board before scoring, so
      // the song score reflects the FULL plan (nodes AND sockets) — the sockets
      // amplify stat/score nodes and are worth ~20% here, so an empty-slot score
      // materially understates what the plan actually builds.
      const scored = JSON.parse(JSON.stringify(doc.optimised_board_state));
      for (const p of (doc.sockets_optimised && doc.sockets_optimised.placements) || []) {
        if (scored[p.chr]) {
          scored[p.chr].slots = scored[p.chr].slots || {};
          scored[p.chr].slots[p.socket] = p.card;
        }
      }
      try {
        const r = await fetch("/api/timeline", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            cards, song, basis: basisOf(), board_state: scored,
          }),
        });
        const data = await r.json();
        tl.textContent = "";
        if (data.available) {
          const head = document.createElement("div");
          head.className = "plan-score";
          head.innerHTML =
            `<span class="plan-score-lab">Song score with this board</span>` +
            `<span class="plan-score-val">${num(Math.round(data.total_score))}</span>` +
            (data.max_score
              ? `<span class="plan-score-max">max if all actives fire ` +
                `<b>${num(Math.round(data.max_score))}</b></span>`
              : "") +
            `<span class="plan-score-sub">the full plan (nodes + Connect cards): estimated ` +
            `expected active uptime vs the ceiling if every active triggered — the windows ` +
            `below reflect the board's cooldown nodes</span>`;
          tl.append(head);
          // renderTimeline replaces its host's content, so give it its own box
          // rather than the one holding the score header.
          const tlbox = document.createElement("div");
          tl.append(tlbox);
          renderTimeline(tlbox, data, (lastPayload && lastPayload.cards) || {});
        } else {
          tl.append(note("Board-aware timeline unavailable: " + (data.reason || "")));
        }
      } catch (error) {
        tl.textContent = "";
        tl.append(note("Board-aware timeline failed to load: " + error));
      }
    }
  };

  fastBtn.addEventListener("click", async () => {
    fastBtn.disabled = true;
    const label0 = "Quick estimate (points only, ~8s)";
    fastBtn.textContent = "Estimating (points only)…";
    try {
      const r = await fetch("/api/board-plan-fast", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // the develop's own flat budget (453 or the slider) when one was chosen,
        // so the plan matches the row's board instead of the real point grant
        body: JSON.stringify({ cards, basis: basisOf(),
          ...(developMode() && developMode() !== "account" && devBudget() !== "real"
              ? { budget: devBudget() } : {}) }),
      });
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        try { const e = await r.json(); if (e.error) msg = e.error; } catch (_) {}
        fail(`Board plan failed: ${msg}`);
        fastBtn.textContent = label0;
        return;
      }
      await renderDoc(await r.json());
      fastBtn.textContent = "Estimate (points only) ✓";
    } catch (error) {
      fail(`Board estimate failed to load: ${error}`);
      fastBtn.textContent = label0;
    } finally {
      fastBtn.disabled = false;
    }
  });

  fullBtn.addEventListener("click", async () => {
    fullBtn.disabled = true;
    fullBtn.textContent = "Optimising (full)…";
    const basis = basisOf();
    const song = songOf();
    // Honest elapsed/remaining against a measured typical time — the beam runs as
    // a subprocess with no live progress channel, so the bar eases to 92% and
    // holds until the real result lands. A per-song run is slower.
    const EST = song ? 22 : 15;
    const start = performance.now();
    const prog = document.createElement("div");
    prog.className = "planprog";
    const plabel = document.createElement("div");
    plabel.className = "planprog-label";
    const barWrap = document.createElement("div");
    barWrap.className = "planprog-bar";
    const fill = document.createElement("div");
    fill.className = "planprog-fill";
    barWrap.append(fill);
    prog.append(plabel, barWrap);
    out.textContent = "";
    out.append(prog);
    const tick = () => {
      const elapsed = (performance.now() - start) / 1000;
      fill.style.width = Math.min(92, (elapsed / EST) * 100).toFixed(1) + "%";
      const left = EST - elapsed;
      plabel.textContent =
        `Optimising… ${Math.floor(elapsed)}s elapsed` +
        (left > 1 ? ` · ~${Math.ceil(left)}s left` : " · almost there…") +
        ` (usually ~${EST}s)`;
    };
    tick();
    const timer = setInterval(tick, 500);
    try {
      const response = await fetch("/api/boardplan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          cards, basis, song, engine: "solver",
          char_level: levelSel.value,
          resources: resSel.value,
        }),
      });
      if (!response.ok) {
        let msg = `HTTP ${response.status}`;
        try { const e = await response.json(); if (e.error) msg = e.error; } catch (_) {}
        fail(`Board plan failed: ${msg}`);
        fullBtn.textContent = "Board to build (~25s, optimal)";
        return;
      }
      await renderDoc(await response.json());
      fullBtn.textContent = "Board to build ✓";
    } catch (error) {
      fail(`Board plan failed to load: ${error}`);
      fullBtn.textContent = "Board to build (~90s)";
    } finally {
      clearInterval(timer);
      fullBtn.disabled = false;
    }
  });

  return host;
}

// Attach a per-team board-plan button to every ranked team's block, so the plan
// can be built for the winner OR any runner-up (one per team, not just #1). It
// stays a button, not an auto-run: five plans would be 5-10 min of beam search.
function drawBoardPlan() {
  if (!lastPayload || !(lastPayload.teams || []).length) return;
  const rankRoot = document.getElementById("ranking");
  const blocks = rankRoot ? rankRoot.querySelectorAll("details.rank") : [];
  const runOpts = (lastPayload.run && lastPayload.run.opts) || null;
  lastPayload.teams.forEach((team, i) => {
    const block = blocks[i];
    if (!block) return;
    const body = block.querySelector(".rbody") || block;
    // The developed board this row was scored on, per character —
    // eager counts from `board_plan_summary`, the grid on expand.
    if (team.board_plan_summary) {
      body.append(boardSection(team, {
        cards: lastPayload.cards || {},
        basis: runOpts ? runOpts.basis : ((el.basis && el.basis.value) || "account"),
        budget: runOpts ? runOpts.budget : (developMode() && developMode() !== "account" ? devBudget() : "real"),
        song: ($("song") && $("song").value) || "",
      }));
    }
    body.append(boardPlanButton(team));
  });
}

// --- the per-song picture ---------------------------------------------------
//
// The select is filled from the chart library rather than hardcoded, so a song the
// library does not carry cannot be offered and one it gains needs no edit here.
//
// The timeline is drawn for the TOP team only, and says so. Drawing it for
// every rank would put five lanes x N teams on screen; the question it answers
// -- "when is each member actually live on this chart" -- is about the team you
// are going to field.
let lastPayload = null;
// One timeline PER ranked team, inside its own rank block — the "when is each
// member live on this chart" picture for the winner AND every runner-up. In song
// mode the arrangement is part of the answer (cue i fires slot i's SPECIAL),
// so each team draws in its own `member_order`. /api/timeline
// is a fast chart read (not the board plan's beam), so blocks render LAZILY on
// first expand: rank 1 (open) draws at once, the rest when opened. Song mode only.
function drawTimeline() {
  const song = ($("song") && $("song").value) || "";
  if (!song || !lastPayload || !(lastPayload.teams || []).length) return;
  const rankRoot = document.getElementById("ranking");
  const blocks = rankRoot ? rankRoot.querySelectorAll("details.rank") : [];
  lastPayload.teams.forEach((team, i) => {
    const block = blocks[i];
    if (!block) return;
    const body = block.querySelector(".rbody") || block;
    const host = document.createElement("section");
    host.className = "tl";
    body.append(host);
    let drawn = false;
    const render = async () => {
      if (drawn) return;
      drawn = true;
      host.textContent = "Reading the chart…";
      const members = team.member_order || team.member_ids;
      try {
        const data = await fetch("/api/timeline", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            song,
            cards: [team.leader_id, ...members],
            basis: el.basis ? el.basis.value : "account",
            board: boardWanted(),
            // When boards were developed, draw the timeline against the SAME
            // developed board it was scored on — its Act.CD/Act.Prob nodes
            // tighten the active windows — so the headline and the timeline agree.
            board_state: team.board_state || undefined,
            ...whatIf(),
          }),
        }).then((r) => r.json());
        renderTimeline(host, data, lastPayload.cards || {});
      } catch (error) {
        host.textContent = `Timeline failed: ${error}`;
      }
    };
    if (block.open) render();
    block.addEventListener("toggle", () => { if (block.open) render(); });
  });
}

async function loadSongs() {
  const select = $("song");
  if (!select) return;
  try {
    const data = await (await fetch("/api/songs")).json();
    if (!data.available) {
      // First-class absence, said out loud rather than left as an empty list
      // that reads as "this game has no songs".
      const option = document.createElement("option");
      option.disabled = true;
      option.textContent = data.reason || "no chart library on disk";
      select.appendChild(option);
      return;
    }
    for (const song of data.songs) {
      const option = document.createElement("option");
      option.value = song.music_id;
      // The level and the note count are what make one chart a different
      // question from another, so they travel with the name instead of being
      // something you have to look up elsewhere.
      option.textContent =
        `${song.title} — Lv${song.difficulty_level}, ${song.notes} notes`;
      if (!song.titled) option.textContent += " (no title on file)";
      select.appendChild(option);
    }
  } catch (error) {
    const option = document.createElement("option");
    option.disabled = true;
    option.textContent = `could not load songs: ${error}`;
    select.appendChild(option);
  }
}

loadSongs();
{
  const select = $("song");
  if (select) select.addEventListener("change", drawTimeline);
}
mountSongPicker();

// The collection and board editors are separate panels with their own state;
// mounting them here keeps `app.js` the single entry point the page loads.
// --- views ------------------------------------------------------------------
// One screen at a time. The two editors stay <details>; setting `.open` is what
// still fires their existing lazy-load on `toggle`, so neither module needed a
// change. The choice is remembered because reloading after a save should not
// throw you back to a screen you were not on.
const VIEWS = {
  solve: document.getElementById("view-solve"),
  collection: document.getElementById("cwrap"),
  board: document.getElementById("bwrap"),
  // The park screen. `#pwrap` may not exist yet on an older page; a missing
  // entry is filtered below rather than left as a `null` that `showView` would
  // dereference on the first tab click.
  park: document.getElementById("pwrap"),
  resources: document.getElementById("rwrap"),
  // The gacha tracker. Same lazy-<details> contract as the two above.
  gacha: document.getElementById("gwrap"),
  compare: document.getElementById("xwrap"),
};
for (const [key, node] of Object.entries(VIEWS)) {
  if (!node) delete VIEWS[key];
}

function showView(name) {
  if (!VIEWS[name]) name = "solve";
  for (const [key, node] of Object.entries(VIEWS)) {
    const active = key === name;
    node.hidden = !active;
    // `open` drives the lazy-load; only touch it on the <details> panels.
    if (node.tagName === "DETAILS" && active) node.open = true;
  }
  for (const tab of document.querySelectorAll("#views .vtab")) {
    tab.classList.toggle("on", tab.dataset.view === name);
  }
  try { localStorage.setItem("hbuilder.view", name); } catch { /* private mode */ }
}

for (const tab of document.querySelectorAll("#views .vtab")) {
  tab.addEventListener("click", () => showView(tab.dataset.view));
}
let startView = "solve";
try { startView = localStorage.getItem("hbuilder.view") || "solve"; } catch { /* ignore */ }

// ⚠ ORDER. `showView` sets `open` on the active <details>, and that is what
// fires the lazy-load its module listens for. Calling it BEFORE the mounts
// registered those listeners leaves the panel open and empty, with nothing in the
// console -- the event has already gone by. Mount first, switch second.
mountCollection();
mountBoard();
mountPark();
mountResources();
mountGacha();
mountCompare();
// The Solve's search constraints — fetches the card list and renders the picker.
// A live badge on the disclosure shows how many are set; refreshed on toggle and
// before each solve so it tracks the state without a per-keystroke listener.
mountConstraints(document.getElementById("constraints"));
mountPinSlots(document.getElementById("pinslots"));
const cstrCount = document.getElementById("cstr-count");
function refreshConstraintCount() {
  if (cstrCount) cstrCount.textContent = constraintsSummary() ? `· ${constraintsSummary()}` : "";
}
const cstrDetails = document.querySelector(".ctrl-constraints");
if (cstrDetails) cstrDetails.addEventListener("toggle", refreshConstraintCount);
showView(startView);


// -- Refresh account ---------------------------------------------------------
// The one control on this page that writes. It shells out to refresh_all.py,
// which owns the data-authority rules (owned.json never written, cards.json
// append-only) -- see `server._refresh`. The whole stdout is shown because the
// DIFF is the product: "what moved" is the answer, not "done".
if (el.refresh) {
  el.refresh.addEventListener("click", async () => {
    const button = el.refresh;
    const label = button.textContent;
    button.disabled = true;
    button.textContent = "Refreshing…";
    // Not a spinner: the chain logs in, reads User/Get, rebuilds six artefacts
    // and can take minutes. A control that looks idle for that long reads as
    // broken.
    const note = document.getElementById("err");
    if (note) {
      note.hidden = false;
      note.textContent =
        "Refreshing: login, User/Get, card params, board state, then the "
        + "derived files. This can take a few minutes.";
    }
    try {
      const response = await fetch("/api/refresh", { method: "POST" });
      // ⛔ Read TEXT first, then parse. `response.json()` on a non-JSON body
      // throws a SyntaxError naming a column number, which tells the reader
      // nothing about what the server actually said -- and what it said is the
      // whole diagnosis: "unexpected keyword at line 1 column 1" says nothing
      // about what came back.
      const raw = await response.text();
      let data;
      try {
        data = JSON.parse(raw);
      } catch (parseError) {
        if (note) {
          note.hidden = false;
          // A 404 on this route has exactly ONE cause worth naming: the route
          // exists in server.py, so a server that does not have it is a server
          // started before the route was added. Python does not reload an
          // edited module. Saying that beats printing "not found".
          note.textContent = response.status === 404
            ? "This server is OLDER than this page: /api/refresh returned 404, "
              + "and Python does not reload an edited module.\n\n"
              + "Close the hololive-builder window, open it again, then reload "
              + "here with Ctrl+F5."
            : "The server replied with something that is not JSON (HTTP "
              + response.status + "). Raw reply:\n\n" + raw.slice(0, 2000);
        }
        return;
      }
      if (note) {
        note.hidden = false;
        // The full log already went to the shell, line by line, as it
        // happened. Repeating a few hundred lines here buries the page in
        // something the operator is already looking at, so this shows the
        // SUMMARY block and the lines that actually moved something -- and
        // says where the rest is.
        const stdout = data.stdout || "";
        const summaryAt = stdout.lastIndexOf("SUMMARY");
        const summary = summaryAt === -1 ? stdout : stdout.slice(summaryAt);
        const changed = stdout
          .split("\n")
          .filter((line) => line.trim().startsWith("~ ") && !/no change/.test(line))
          .map((line) => line.trim());
        const head = data.returncode === 0
          ? "Refresh finished.\n"
          : data.returncode === 3
          ? "hololive Dreams is in maintenance: nothing was refreshed. Refresh again once it ends.\n"
          : data.returncode === 4
          ? "This PC does not have the game's build constants yet: run refresh.cmd once (it finds them in your game install). Nothing was refreshed.\n"
          : "Refresh exited " + data.returncode
            + " -- partial. The failed step is in the summary below.\n";
        // The gacha sync rides along with the refresh (otherwise pulls sit
        // untracked until the Gacha tab's own button is pressed). One line says what it found; a possible history gap
        // (the wire serves only the newest 100 draws) is called out loudly.
        let gachaLine = "";
        if (data.gacha) {
          const g = data.gacha;
          if (g.error) gachaLine = "\nGacha: " + g.error + "\n";
          else if (g.summary) {
            gachaLine = "\nGacha: +" + g.summary.pulls + " pulls, +"
              + g.summary.banners + " banners synced.\n"
              + (g.summary.possible_gap
                ? "⚠ POSSIBLE GAP: the game only serves the newest 100 draws and "
                  + "everything it sent is newer than the store — pulls made "
                  + "between syncs may be lost for good.\n"
                : "");
          } else gachaLine = "\nGacha sync exited " + g.returncode + " — see the shell window.\n";
        }
        note.textContent =
          head
          + (changed.length
            ? "\nWhat moved:\n" + changed.join("\n") + "\n"
            : "\nNothing moved.\n")
          + gachaLine
          + "\n" + summary.trim()
          + "\n\n(the full log is in the shell window, as it happened)"
          + (data.stderr ? "\n\n" + data.stderr : "");
      }
    } catch (error) {
      if (note) {
        note.hidden = false;
        note.textContent = "Refresh failed: " + error;
      }
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  });
}
