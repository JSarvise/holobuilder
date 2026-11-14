// Optimizer page, goal "Rating": the Holomem Rating (the game's per-holomem high-score
// ranking). Your rating, rank and borders on the chosen holomem, the songs that count for her,
// and the searches (POST /api/rating/run + SSE): a team per song, or one team for all of them.
// Every number comes from the server (rating_mode.py, hbuilder.domain.rating).
import { byId, esc, fmt, toast, getJSON, postJSON, thumb, when, info, NOTES, cardHook, stash, unstash } from "/opt-util.js";
import { score } from "/hb-grade.js";
import { progressCard } from "/opt-progress.js";
import { assumptionsOf, renderTeams } from "/opt-score.js";

NOTES.rating = "A holomem's rating adds up your three best songs of hers. Each song counts once, at its best difficulty, "
  + "and is worth its highest score divided by 5,000, rounded up. A play counts for the holomem who leads the team, and only on a song she sings "
  + "(solo or unit songs; all-member songs do not count). The ranking is weekly and closes on Monday at 5:00.";
NOTES.ratingmax = "Max: the team's run if every Active skill fires. A high-score ranking keeps your best run, so the teams are ranked by the best they can do. "
  + "Expected ranks them by an average run instead.";
NOTES.ratinggain = "What this song adds to the holomem's rating if you reach the predicted score: only your three best songs count, "
  + "and a record only ever rises.";

let list = null;          // GET /api/rating: every holomem, your rating and rank on each
let status = null;        // GET /api/rating/status for {holomem, level, owned}
let statusKey = "";
let picked = null;        // the songs chosen by hand (music ids), or null for the best estimated
let result = null;        // {key, doc}
let live = null;          // {key, job, es, ctx, songs: Map}
let pc = null;            // the progress card
let selSong = null;       // the song whose teams are laid out below the list (a music id)
let teamSel = 0;         // one team for every song: which of the served teams is shown

// the last answer comes back when the page is opened again (opt-util.stash, per browser tab, as the
// Score and Event goals keep theirs); a refresh of the account drops it
let restored = false;
function remember(ctx) {
  if (result) stash("rating", ctx, { result, selSong, teamSel, picked: picked ? [...picked] : null });
}
function restore(ctx) {
  restored = true;
  const k = unstash("rating", ctx);
  if (!k || !k.result) return;
  result = k.result; selSong = k.selSong || null; teamSel = k.teamSel || 0;
  picked = Array.isArray(k.picked) ? new Set(k.picked) : null;
}

const LEVELS = [20, 22, 24, 25, 26, 27, 28, 29, 30, 31, 32];
const statusQuery = S => `holomem=${encodeURIComponent(S.rholomem)}&max_level=${S.rlevel}&owned=${S.rowned ? 1 : 0}`;
const runKey = S => JSON.stringify([S.rholomem, S.rlevel, S.rowned, S.rmode, S.rboard, S.rsort, S.rsongs, S.rteams,
  picked ? [...picked].sort() : null, S.cards, S.boards, S.maxpoints, S.infmats, S.platform, S.auto, S.pool,
  S.cards === "whatif" ? [S.alevel, S.abloom, S.aspt, !!S.keephigher] : 0, S.maxresets, S.resetgain, S.allinvited,
  S.norefund, S.branches, JSON.stringify(S.targets), S.loutfit, S.nooverlap, S.require, S.exclude]);

export async function loadRatingList(force) {
  if (list && !force) return list;
  try { list = await getJSON("/api/rating"); } catch (e) { list = { holomems: [], error: e.message }; }
  return list;
}
export const ratingHolomem = id => ((list && list.holomems) || []).find(h => h.chr === id) || null;
export const levelOptions = () => LEVELS;

async function loadStatus(S) {
  const q = statusQuery(S);
  if (status && statusKey === q) return status;
  statusKey = q;
  try { status = await getJSON("/api/rating/status?" + q); } catch (e) { status = { error: e.message }; }
  if (statusKey !== q) return null;
  return status;
}

/* ------------------------------------------------------------------ header */
// the stage's line under the title: the account's rating, rank and the next border
export function ratingHeader(S) {
  const h = ratingHolomem(S.rholomem) || {};
  const st = status && status.holomem === S.rholomem ? status : null;
  const rating = st ? st.rating : h.rating;
  const rank = st ? st.rank : h.rank;
  const nb = st && st.next_border;
  return [
    rating != null ? `<span>Your rating <span class="num">${fmt(rating)}</span></span>` : "",
    `<span>${rank ? `Rank <span class="num">#${fmt(rank)}</span>` : list && list.ranking_read_at ? "Not ranked yet" : "Rank not read yet"}</span>`,
    nb ? `<span>Top ${fmt(nb.rank)} needs <span class="num">${fmt(nb.rating)}</span>${nb.gap ? ` (<span class="num">+${fmt(nb.gap)}</span>)` : ""}</span>` : "",
  ].filter(Boolean).join("");
}

/* ------------------------------------------------------------------ the holomem picker */
export async function openHolomemPicker(dlg, S, onPick) {
  const doc = await loadRatingList();
  const rows = [...(doc.holomems || [])].sort((a, b) => (b.rating - a.rating) || a.name.localeCompare(b.name));
  dlg.innerHTML = `<div class="dlg-h"><h2>Choose a holomem</h2><span class="r">Your rating on each holomem's ranking</span>
      <button class="o-x" type="button" data-close aria-label="Close"><svg class="i"><use href="#i-x"/></svg></button></div>
    <div class="o-rt-pick-b">
      <input class="o-rt-find" type="search" placeholder="Search a holomem" aria-label="Search a holomem">
      <div class="o-rt-grid" role="listbox">${rows.map(h => `<button type="button" role="option" class="o-rt-hm" data-chr="${esc(h.chr)}" aria-selected="${h.chr === S.rholomem}" data-name="${esc(h.name.toLowerCase())}">
        <img src="/chricon/${esc(h.chr)}" alt="" loading="lazy" onerror="this.style.visibility='hidden'">
        <span class="nm">${esc(h.name)}</span>
        <span class="num rt">${fmt(h.rating)}</span>
        <span class="rk">${h.rank ? `#${fmt(h.rank)}` : "—"}</span></button>`).join("")}</div>
    </div>`;
  const find = dlg.querySelector(".o-rt-find");
  find.oninput = () => {
    const q = find.value.trim().toLowerCase();
    dlg.querySelectorAll(".o-rt-hm").forEach(b => { b.hidden = !!q && !b.dataset.name.includes(q); });
  };
  dlg.onclick = e => {
    const b = e.target.closest(".o-rt-hm");
    if (b) { dlg.close(); onPick(b.dataset.chr); return; }
    if (e.target.closest("[data-close]")) dlg.close();
  };
  dlg.showModal();
  find.focus();
}

/* ------------------------------------------------------------------ idle */
export async function showRatingIdle(ctx, keepLive) {
  const S = ctx.S;
  if (!restored) { restore(ctx); keepLive = true; }
  if (!keepLive) { selSong = null; teamSel = 0; }
  if (!S.rholomem) {
    ctx.out().innerHTML = `<section class="o-empty"><h2>Choose a holomem</h2><p>Pick whose ranking you are climbing: she leads every team, since a play counts for the leader. ${info("rating")}</p></section>`;
    return;
  }
  if (live && live.key !== runKey(S)) cancelRating(true);
  const st = await loadStatus(S);
  if (!st || st.holomem !== S.rholomem && !st.error) return;
  if (ctx.onStatus) ctx.onStatus();
  render(ctx);
}

/* ------------------------------------------------------------------ run */
function body(S) {
  const a = assumptionsOf(S);
  const b = {
    holomem: S.rholomem, mode: S.rmode, max_level: S.rlevel, owned_only: !!S.rowned,
    objective: S.rsort === "max" ? "max" : "expected", one_board: S.rboard !== "per", teams: S.rteams,
    basis: S.cards === "maxed" ? "maxed" : "account", platform: S.platform, auto: !!S.auto,
  };
  if (picked && picked.size) b.songs = [...picked];
  else b.song_count = S.rsongs === "all" ? 20 : +S.rsongs;
  if (a) b.assumptions = a;
  if (S.loutfit) b.leader_card = S.loutfit;
  if (S.nooverlap) b.no_leader_overlap = true;
  if (S.require.length) b.require = S.require;
  if (S.exclude.length) b.exclude = S.exclude;
  if (S.pool === "5star") b.pool = "5star";
  return b;
}
// the songs a run with these options searches, as the page shows them before it starts
function chosenSongs(S) {
  const songs = (status && status.songs) || [];
  if (picked && picked.size) return songs.filter(s => picked.has(s.music_id));
  const n = S.rsongs === "all" ? songs.length : +S.rsongs;
  return songs.slice(0, S.rmode === "one" ? Math.min(n, 8) : n);
}

export async function runRating(ctx) {
  const S = ctx.S;
  if (!S.rholomem) { toast("Choose a holomem first."); return; }
  await loadStatus(S);
  const songs = chosenSongs(S);
  if (!songs.length) { toast("No song of hers can be searched with these options."); return; }
  cancelRating(true);
  result = null;
  const key = runKey(S);
  const mine = live = { key, ctx, songs: new Map() };
  ctx.setBusy(true);
  const steps = S.rmode === "one"
    ? [`Searching one team for ${songs.length} songs`]
    : songs.map(s => `Searching ${s.title}`);
  if (pc) pc.stop();
  pc = progressCard(`rating:${S.rmode}:${songs.length}`, steps, { best: false });
  render(ctx);
  let started;
  try { started = await postJSON("/api/rating/run", body(S)); }
  catch (e) { if (live === mine) { live = null; ctx.setBusy(false); endCard(); toast("The search could not start: " + e.message); render(ctx); } return; }
  if (live !== mine) { fetch("/cancel/" + started.job_id, { method: "POST" }).catch(() => {}); return; }
  mine.job = started.job_id;
  const order = new Map(songs.map((s, i) => [s.music_id, i]));
  const es = mine.es = new EventSource("/events/" + started.job_id);
  let doc = null, failed = null;
  es.onmessage = m => {
    let ev; try { ev = JSON.parse(m.data); } catch (_) { return; }
    if (live !== mine) return;
    if (ev.type === "progress" && pc) {
      const i = ev.song != null ? order.get(ev.song) ?? 0 : 0;
      pc.update({ active: S.rmode === "one" ? 0 : i, detail: S.rmode === "one" ? (ev.round ? `round ${ev.round}` : ev.sub === "materials" ? "planning within your materials" : "") : `song ${i + 1} of ${songs.length}`,
        // a time left only from a count within the step (the one team's plans per song); the
        // songs ARE the steps, so their count says nothing about the step running
        ...(S.rmode === "one" && ev.sub === "materials" ? { done: ev.done, total: ev.total } : {}) });
    } else if (ev.type === "song") {
      mine.songs.set(ev.song.music_id, ev.song);
      render(ctx);
    } else if (ev.type === "result") doc = ev.payload;
    else if (ev.type === "error") failed = ev.message || "the search failed";
    else if (ev.type === "end") {
      es.close();
      live = null;
      ctx.setBusy(false);
      endCard(!!doc);
      if (doc) { result = { key, doc }; selSong = null; teamSel = 0; }
      if (doc) remember(ctx);
      else toast("The search failed: " + (failed || "no result"), 6000);
      render(ctx);
      if (doc && failed == null && (doc.materials_failed || []).length) toast(doc.materials_failed[0], 6000);
    }
  };
  es.onerror = () => { es.close(); if (live === mine && !doc) { live = null; ctx.setBusy(false); endCard(); toast("Lost the search's event stream."); render(ctx); } };
}
function endCard(answered) { if (pc) { if (answered) pc.done(); else pc.stop(); } pc = null; }
export function cancelRating(quiet) {
  if (!live) return;
  if (live.es) live.es.close();
  if (live.job) fetch("/cancel/" + live.job, { method: "POST" }).catch(() => {});
  const ctx = live.ctx;
  live = null;
  ctx.setBusy(false);
  endCard();
  if (!quiet) { toast("Search cancelled."); render(ctx); }
}

/* ------------------------------------------------------------------ rendering */
const jacket = id => `<img class="o-rt-jk" src="/jacket/${encodeURIComponent(id)}" alt="" loading="lazy" onerror="this.style.visibility='hidden'">`;
const diffBadge = (d, lv) => d ? `<span class="diff ${esc(String(d).toLowerCase())}">${lv != null ? `<b>${lv}</b>` : ""}${esc(d[0] + d.slice(1).toLowerCase())}</span>` : "";
const gainChip = g => g > 0 ? `<span class="o-rt-gain up num">+${fmt(g)}</span>` : `<span class="o-rt-gain num">±0</span>`;
const mini = (ids, cardOf) => `<span class="o-rt-team">${ids.map((id, k) => {
  const c = cardOf(id) || {};
  return `<img src="${thumb(id)}" alt="${esc(c.member || id)}" title="${esc((k ? "" : "Leader · ") + (c.member || id) + (c.costume_name ? " / " + c.costume_name : ""))}"${k ? "" : ` class="lead"`} loading="lazy">`;
}).join("")}</span>`;

function tiles(st) {
  const nb = st.next_border;
  const sum = (st.top || []).reduce((a, t) => a + t.rating, 0);
  return `<div class="o-tiles o-rt-tiles">
    <div class="stat-tile prime"><div><span class="lbl">Holomem Rating ${info("rating")}</span><span class="n">${fmt(st.rating)}</span><small>${esc(st.name)} · your best 3 songs added</small></div></div>
    <div class="o-sidetiles">
      <div class="stat-tile"><div><span class="lbl">Rank</span><span class="n">${st.rank ? "#" + fmt(st.rank) : "—"}</span><small>${st.rank ? "on her weekly ranking" : st.ranking_read_at ? "not in the ranking yet" : "not read yet"}</small></div></div>
      <div class="stat-tile"><div><span class="lbl">Next border</span><span class="n">${nb ? fmt(nb.rating) : "—"}</span><small>${nb ? `Top ${fmt(nb.rank)} · ${nb.gap ? `+${fmt(nb.gap)} to go` : "reached"}` : (st.borders || []).length ? "you are in the top tier" : "borders not read yet"}</small></div></div>
      <div class="stat-tile"><div><span class="lbl">Account total</span><span class="n">${st.account_total != null ? fmt(st.account_total) : "—"}</span><small>every holomem's rating added</small></div></div>
    </div></div>`;
}
function borderLine(st) {
  const bs = st.borders || [];
  const bits = [];
  if (bs.length) bits.push("Borders " + bs.map(b => `top ${fmt(b.rank)} <span class="num">${fmt(b.rating)}</span>`).join(" · "));
  if (st.ranking_note) bits.push(esc(st.ranking_note));
  else if (st.ranking_read_at) bits.push(`ranking read ${esc(when(st.ranking_read_at))}`);
  if (st.captured_at) bits.push(`records read ${esc(when(st.captured_at))}`);
  return bits.length ? `<p class="o-rt-note">${bits.join(" · ")}</p>` : "";
}

function render(ctx) {
  const S = ctx.S, out = ctx.out();
  const st = status;
  if (!st) { out.innerHTML = `<section class="o-empty"><h2>Reading your rating…</h2></section>`; return; }
  if (st.error) { out.innerHTML = `<div class="o-error">Your Holomem Rating is unavailable: ${esc(st.error)}</div>`; return; }
  const cardOf = id => ctx.card(id);
  const key = runKey(S);
  const doc = result && result.key === key ? result.doc : null;
  const running = live && live.key === key ? live : null;
  if (doc && !selSong) selSong = firstSong(doc);
  const noRecords = !st.have_records
    ? `<div class="o-error">Your records are not on this PC yet: press Refresh (it reads your highest scores, the songs you own and your rank).</div>` : "";
  const head = `${noRecords}${tiles(st)}${borderLine(st)}`;
  const prog = running && pc ? `<div class="o-progwrap" data-rt-prog></div>` : "";
  const body = S.rmode === "one" && doc ? oneTeam(ctx, doc, cardOf) : songTable(ctx, st, doc, running, cardOf);
  const detail = doc && selSong ? `<section class="o-rt-detail" aria-label="Teams on the chosen song">${detailHead(ctx, doc)}<div id="o-rt-detail"></div></section>` : "";
  out.innerHTML = `<section class="o-rt">${head}${prog}${doc && S.rmode !== "one" ? summary(doc.predicted, st) : ""}${body}${skippedList(st)}${detail}</section>`;
  const pw = out.querySelector("[data-rt-prog]"); if (pw && pc) pw.replaceChildren(pc.el);
  out.onclick = e => {
    const pick = e.target.closest("[data-pick]");
    if (pick) { togglePick(ctx, pick.dataset.pick); return; }
    if (e.target.closest("[data-pickreset]")) { picked = null; render(ctx); return; }
    const tm = e.target.closest("[data-rtteam]");
    if (tm) { teamSel = +tm.dataset.rtteam; remember(ctx); render(ctx); return; }
    const sg = e.target.closest("[data-rtsong]");
    if (sg && !e.target.closest("label, input")) {
      selSong = sg.dataset.rtsong; remember(ctx); render(ctx);
      const d = out.querySelector(".o-rt-detail");
      if (d) d.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
    }
  };
  if (detail) showDetail(ctx, doc);
}
// the song whose teams open first: the best rating the search found
function firstSong(doc) {
  if (doc.mode === "one") {
    const T = (doc.rows || [])[0];
    return T ? ([...T.charts].sort((a, b) => b.rating - a.rating)[0] || {}).music_id || null : null;
  }
  const done = (doc.songs || []).filter(s => (s.rows || []).length);
  return done.length ? [...done].sort((a, b) => b.rating - a.rating)[0].music_id : null;
}
function chosenEntry(doc) {
  if (doc.mode === "one") {
    const T = (doc.rows || [])[Math.min(teamSel, (doc.rows || []).length - 1)];
    const c = T && T.charts.find(x => x.music_id === selSong);
    return T && c ? { T, c } : null;
  }
  const e = (doc.songs || []).find(x => x.music_id === selSong);
  return e && (e.rows || []).length ? { e } : null;
}
function detailHead(ctx, doc) {
  const got = chosenEntry(doc);
  if (!got) return "";
  const s = got.e || got.c;
  const what = doc.mode === "one"
    ? (doc.one_board ? "The teams found, on this song; pick one to see it above. The board shown is planned for this song; the one setup for every song can differ a little." : "The teams found, on this song, each on the board planned for it; pick one to see it above.")
    : `The best teams the search found for this song, each on its own Holomem Boards.`;
  return `<div class="o-rt-dhead">${jacket(s.music_id)}<div><h2>${esc(s.title || s.music_id)} ${diffBadge(s.difficulty, s.level)}</h2><p>${esc(what)}</p></div></div>`;
}
// the chosen song's teams, as the Score goal lays out a search: the setlist and the selected
// team with its Unit Score, Skill uptime and Holomem Board plan (/api/row-detail rebuilds the
// row's own board, under the options the search ran with)
function showDetail(ctx, doc) {
  const got = chosenEntry(doc);
  const el = document.getElementById("o-rt-detail");
  if (!got || !el) return;
  const byMax = ctx.S.rsort === "max";
  let ranking, difficulty, music, title, seconds, basis;
  if (got.e) {
    const e = got.e;
    ({ music_id: music, difficulty, title, seconds, basis } = e);
    ranking = e.rows.map(t => ({ leader: t.leader, leader_name: t.leader_name, members: t.members, member_order: t.member_order,
      best: t.expected, song_max: t.max, refined: t.refined }));
  } else {
    const { T, c } = got;
    ({ music_id: music, difficulty, title } = c);
    basis = doc.basis; seconds = doc.seconds;
    ranking = doc.rows.map(t => {
      const tc = t.charts.find(x => x.music_id === c.music_id) || {};
      return { leader: t.leader, leader_name: t.leader_name, members: t.members, best: tc.expected, song_max: tc.max, refined: false };
    });
  }
  const S = { ...ctx.S, goal: "score", song: music, difficulty, sort: byMax ? "max" : "avg", top: Math.max(3, ranking.length), lvmode: "expert" };
  const sctx = { ...ctx, S, out: () => document.getElementById("o-rt-detail"), song: () => ({ music_id: music, title }) };
  renderTeams(sctx, {
    scope: "song", on_demand: true, state: "fresh", seconds,
    variant: doc.mode === "one" ? `one:${doc.one_board ? "board" : "per-song"}` : "each",
    objective: `${byMax ? "Max (every Active fires)" : "expected score"} on this song's ${difficulty} chart, her leading`,
    basis,
  }, ranking, got.e ? {} : {
    // a team picked in the setlist is the one the songs above are rated with
    selected: teamSel,
    onSelect: i => { if (i !== teamSel) { teamSel = i; remember(ctx); paintOne(ctx, doc); } },
  });
}
function paintOne(ctx, doc) {
  const box = document.getElementById("o-rt-onebox");
  if (box) box.innerHTML = oneTeamInner(ctx, doc, id => ctx.card(id));
}
function togglePick(ctx, id) {
  const S = ctx.S;
  if (!picked) picked = new Set(chosenSongs(S).map(s => s.music_id));
  if (picked.has(id)) picked.delete(id); else picked.add(id);
  if (S.rmode === "one" && picked.size > 8) { picked.delete(id); toast("One team is searched on at most 8 songs."); }
  render(ctx);
}

function summary(pred, st) {
  if (!pred) return "";
  const title = id => ((st.songs || []).find(s => s.music_id === id) || {}).title || id;
  return `<div class="o-rt-sum">
    <div class="o-rt-arrow"><span class="lbl">If you reach these scores</span>
      <span class="n">${fmt(pred.now)}</span><svg class="i"><use href="#i-chev"/></svg><span class="n hi">${fmt(pred.after)}</span>${gainChip(pred.gain)}</div>
    <ol class="o-rt-top">${(pred.top || []).map(t => `<li class="${t.new ? "new" : ""}">${jacket(t.music_id)}<span>${esc(title(t.music_id))}</span><span class="num">${fmt(t.rating)}</span>${t.new ? `<span class="tag">new</span>` : `<span class="tag muted">kept</span>`}</li>`).join("")}</ol>
  </div>`;
}

function songTable(ctx, st, doc, running, cardOf) {
  const S = ctx.S;
  const chosen = new Set(chosenSongs(S).map(s => s.music_id));
  const res = new Map();
  if (doc) for (const s of doc.songs || []) res.set(s.music_id, s);
  if (running) for (const [k, v] of running.songs) res.set(k, v);
  const byMax = S.rsort === "max";
  const songs = st.songs || [];
  const top3 = new Set((st.top || []).map(t => t.music_id));
  const hdr = `<div class="dlg-h"><h2>Songs that count for ${esc(st.name)}</h2>
    <span class="r">${fmt(songs.length)} songs · hardest chart up to level ${S.rlevel >= 40 ? "any" : S.rlevel}${picked ? ` · <button class="o-link" type="button" data-pickreset>use the best estimated</button>` : ""}</span></div>`;
  if (!songs.length) return `<div class="dlg o-rt-songs">${hdr}<p class="o-rt-empty">No song of hers is owned, charted and within your level. Raise the level or allow every song in Options.</p></div>`;
  const rows = songs.map((s, i) => {
    const r = res.get(s.music_id);
    const cur = s.current;
    const on = chosen.has(s.music_id);
    const best = r && r.rows && r.rows[0];
    const val = best ? (byMax ? best.max : best.expected) : null;
    const has = !!(r && r.rows && r.rows.length);
    const isSel = has && selSong === s.music_id;
    return `<div class="o-rt-row${on ? " on" : ""}${top3.has(s.music_id) ? " top3" : ""}${has ? " pick" : ""}"${has ? ` data-rtsong="${esc(s.music_id)}" aria-selected="${isSel}" title="Show this song's teams below"` : ""}>
      <label class="o-rt-chk" title="${on ? "Searched" : "Not searched"}"><input type="checkbox" data-pick="${esc(s.music_id)}"${on ? " checked" : ""}${running ? " disabled" : ""}></label>
      ${jacket(s.music_id)}
      <div class="o-rt-title"><b>${esc(s.title)}</b><span>${diffBadge(s.difficulty, s.level)}<span class="tag muted">${s.singer_type === "solo" ? "Solo" : "Unit"}</span>${top3.has(s.music_id) ? `<span class="tag muted">in your top 3</span>` : ""}</span></div>
      <div class="o-rt-cell"><span class="lbl">Your record</span>${cur ? `<span class="num o-rt-r">${fmt(cur.rating)}</span><small>${fmt(cur.score)} · ${esc(cur.difficulty[0] + cur.difficulty.slice(1).toLowerCase())}</small>` : `<span class="num o-rt-r dim">—</span><small>not played with her leading</small>`}</div>
      <div class="o-rt-cell"><span class="lbl">${byMax ? "Max" : "Expected"} ${info("ratingmax")}</span>${best ? `<span class="num">${score(val)}</span><small>${byMax ? `Expected ${fmt(best.expected)}` : `Max ${fmt(best.max)}`}</small>` : running && on ? `<span class="o-sk" style="width:140px;height:22px"></span>` : `<span class="dim">${on ? "press Find best teams" : "not searched"}</span>`}</div>
      <div class="o-rt-cell"><span class="lbl">Rating ${info("ratinggain")}</span>${r ? `<span class="o-rt-to"><span class="num o-rt-r">${fmt(r.rating)}</span>${gainChip(r.gain)}</span>` : `<span class="dim">—</span>`}</div>
      <div class="o-rt-cell o-rt-teamcell">${best ? mini([best.leader, ...best.members], cardOf) : ""}${has ? `<span class="o-rt-see">${isSel ? "Shown below" : `${r.rows.length} team${r.rows.length > 1 ? "s" : ""}`}<svg class="i s"><use href="#i-chev"/></svg></span>` : ""}</div>
    </div>`;
  }).join("");
  return `<div class="dlg o-rt-songs">${hdr}<div class="o-rt-list">${rows}</div></div>`;
}

function oneTeam(ctx, doc, cardOf) {
  if (!(doc.rows || []).length) return `<div class="o-error">The search returned no team.</div>`;
  return `<div id="o-rt-onebox">${oneTeamInner(ctx, doc, cardOf)}</div>`;
}
function oneTeamInner(ctx, doc, cardOf) {
  const rows = doc.rows || [];
  const sel = Math.min(teamSel, rows.length - 1);
  const T = rows[sel];
  const byMax = doc.ranked_by === "max";
  // the strip holds the five members; the leader is named in the title (and drawn in the team detail
  // below, which holds the page's one Live2D figure)
  const L = cardOf(T.leader) || { id: T.leader, member: T.leader };
  const faces = T.members.map(id => {
    const c = cardOf(id) || { id, member: id };
    return `<figure class="o-perf" ${cardHook(c)}>${window.HBCard.face(c, { w: 400 })}
      <figcaption><b>${esc(c.costume_name || c.id)}</b><span class="mb">${esc(c.member || "")}</span></figcaption></figure>`;
  }).join("");
  const charts = T.charts.map(c => `<div class="o-rt-row on pick" data-rtsong="${esc(c.music_id)}" aria-selected="${selSong === c.music_id}" title="Show this team on this song below">
      <span></span>${jacket(c.music_id)}
      <div class="o-rt-title"><b>${esc(c.title || c.music_id)}</b><span>${diffBadge(c.difficulty, c.level)}</span></div>
      <div class="o-rt-cell"><span class="lbl">Your record</span><span class="num o-rt-r${c.current_rating ? "" : " dim"}">${c.current_rating ? fmt(c.current_rating) : "—"}</span></div>
      <div class="o-rt-cell"><span class="lbl">${byMax ? "Max" : "Expected"} ${info("ratingmax")}</span><span class="num">${score(byMax ? c.max : c.expected)}</span><small>${byMax ? `Expected ${fmt(c.expected)}` : `Max ${fmt(c.max)}`}</small></div>
      <div class="o-rt-cell"><span class="lbl">Rating</span><span class="o-rt-to"><span class="num o-rt-r">${fmt(c.rating)}</span>${gainChip(Math.max(0, c.rating - c.current_rating))}</span></div>
      <div class="o-rt-cell o-rt-teamcell"><span class="o-rt-see">${selSong === c.music_id ? "Shown below" : "Details"}<svg class="i s"><use href="#i-chev"/></svg></span></div>
    </div>`).join("");
  const boards = doc.one_board ? "one Holomem Board setup for every song" : "each song on its own Holomem Boards";
  return `${summary(T.predicted, status)}
    <div class="dlg o-rt-songs"><div class="dlg-h"><h2>${rows.length > 1 ? `Team #${sel + 1} of ${rows.length}` : "One team"} for ${fmt(T.charts.length)} songs <small class="o-rt-lead">${esc(L.member || T.leader)} leads${L.costume_name ? " · " + esc(L.costume_name) : ""}</small></h2><span class="r">${esc(boards)} · ranked by ${byMax ? "Max" : "Expected"}${rows.length > 1 ? " · the others in the list below" : ""}</span></div>
      <div class="o-rt-one"><div class="team-strip o-members">${faces}</div></div>
      <div class="o-rt-list">${charts}</div></div>`;
}

function skippedList(st) {
  const sk = st.skipped || [];
  if (!sk.length) return "";
  return `<details class="o-rt-skip"><summary>${fmt(sk.length)} more song${sk.length > 1 ? "s" : ""} of hers not searched</summary>
    <ul>${sk.map(s => `<li>${jacket(s.music_id)}<span>${esc(s.title)}</span><span class="dim">${esc(s.why)}</span>${s.current ? `<span class="num">${fmt(s.current.rating)}</span>` : ""}</li>`).join("")}</ul></details>`;
}
