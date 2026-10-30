// Optimizer page: the song stage, the options, the goal switch
// and the hand-off to the score search (opt-score.js) or the event search (opt-event.js).
// Every number on the page comes from the server; this file only lays out the choices.
import { byId, esc, fmt, dec, mmss, toast, getJSON, postJSON, AC, wirePopovers, normCard, loadRanks, when } from "/opt-util.js";
import { loadRanks as loadGradeRanks } from "/hb-grade.js";
import { showScoreIdle, runScore, cancelScore } from "/opt-score.js";
import { showEventIdle, runEvent, cancelEvent, HL, isHighlight, highlightTile, highlightPool, hlPct } from "/opt-event.js";
import { initTargets } from "/opt-targets.js";
import { openSongPicker } from "/hb-songpick.js";

/* ------------------------------------------------------------------ state */
const DEFAULTS = {
  goal: "score", evgoal: "event_points", song: "", difficulty: "EXPERT", cards: "asis", boards: "develop", sort: "avg",
  boosts: 3, holopass: true, pool: "all", platform: "mobile", top: 5, evtop: 5,
  lmember: "", loutfit: "", nooverlap: false, maxpoints: false, infmats: false,
  alevel: "account", abloom: "account", aspt: "account", keephigher: false, maxresets: "-1", resetgain: "0.01", auto: false, exact: false,
  allinvited: false, norefund: false, branches: "GBRY", targets: [], lvmode: "expert", lvlo: 25, lvhi: 30,
  require: [], exclude: [],
};
const KEEP = Object.keys(DEFAULTS).filter(k => !["require", "exclude"].includes(k));
const S = { ...DEFAULTS };
try { Object.assign(S, JSON.parse(localStorage.getItem("hb.optimizer") || "{}")); } catch (_) { /* private mode */ }
for (const k of ["require", "exclude", "targets"]) if (!Array.isArray(S[k])) S[k] = [];
// a stored "maxed-account" choice maps to "maxed": both are the same on-demand search on the maxed basis
if (S.cards === "maxed-account") S.cards = "maxed";
// boards planned by default; "Optimize Holomem Boards" off = the account's boards as they are
if (S.boards !== "current") S.boards = "develop";
{ // deep links: /optimizer?goal=event_points&song=m0342&difficulty=EXPERT&boosts=3&holopass=1
  const q = new URLSearchParams(location.search);
  if (["score", "event_points", "event_song"].includes(q.get("goal"))) S.goal = q.get("goal");
  if (S.goal !== "score") S.evgoal = S.goal;
  if (q.has("song")) S.song = q.get("song") || "";
  if (q.has("difficulty")) S.difficulty = String(q.get("difficulty")).toUpperCase();
  if (q.has("boosts")) S.boosts = Math.max(1, Math.min(10, +q.get("boosts") || 3));
  if (q.has("holopass")) S.holopass = q.get("holopass") !== "0";
}
fixHighlight();
// Highlight (the event's multi Live pick, opt-event.js) is an Event song and ranks by Event
// Points only: the Score goal drops it (back to Every song), Event Song Score gives way to
// Event Points. Returns true when it changed the goal
function fixHighlight() {
  if (!isHighlight(S.song)) return false;
  S.song = HL;
  if (S.goal === "score") { S.song = ""; return false; }
  if (S.goal === "event_song") { S.goal = S.evgoal = "event_points"; return true; }
  return false;
}
function save() {
  try { localStorage.setItem("hb.optimizer", JSON.stringify(Object.fromEntries(KEEP.map(k => [k, S[k]])))); } catch (_) { /* private */ }
  const q = new URLSearchParams();
  q.set("goal", S.goal);
  if (S.song) q.set("song", S.song);
  if (S.difficulty !== "EXPERT") q.set("difficulty", S.difficulty);
  if (S.goal !== "score") { q.set("boosts", S.boosts); if (S.goal === "event_points") q.set("holopass", S.holopass ? "1" : "0"); }
  history.replaceState(null, "", "/optimizer?" + q.toString());
}
const isEvent = () => S.goal !== "score";
const isHL = () => isEvent() && isHighlight(S.song);
const setProv = html => { const p = byId("prov"); if (p) p.innerHTML = html; };

/* ------------------------------------------------------------------ data */
const D = { meta: null, songs: [], evsongs: null, event: null, cards: new Map(), wallet: null };
export const CTX = {
  S, D, out: () => byId("out"),
  song: () => D.songs.find(s => s.music_id === S.song) || null,
  card: id => D.cards.get(id) || null,
  mergeCards(doc) {
    // response card objects carry the level/bloom of THAT basis (maxed answers show maxed faces)
    const m = new Map(D.cards);
    for (const [id, c] of Object.entries(doc || {})) {
      const cat = D.cards.get(id) || {};
      // the catalogue's Outfit Skill keeps its condition; a response copy may carry it resolved
      m.set(id, normCard({ ...cat, ...c, id, costume_skill: cat.costume_skill || c.costume_skill }));
    }
    return m;
  },
  setBusy, rerenderIdle: () => showIdle(),
};

/* ------------------------------------------------------------------ hero */
function tint(img) {
  try {
    const c = document.createElement("canvas"); c.width = c.height = 32;
    const x = c.getContext("2d", { willReadFrequently: true }); x.drawImage(img, 0, 0, 32, 32);
    const d = x.getImageData(0, 0, 32, 32).data;
    const bins = Array.from({ length: 12 }, () => ({ w: 0, r: 0, g: 0, b: 0 }));
    const hue = (r, g, b) => { const mx = Math.max(r, g, b), mn = Math.min(r, g, b), dd = mx - mn; if (!dd) return 0;
      let h = mx === r ? ((g - b) / dd) % 6 : mx === g ? (b - r) / dd + 2 : (r - g) / dd + 4; h *= 60; return h < 0 ? h + 360 : h; };
    for (let i = 0; i < d.length; i += 4) {
      const r = d[i] / 255, g = d[i + 1] / 255, b = d[i + 2] / 255;
      const mx = Math.max(r, g, b), mn = Math.min(r, g, b), sat = mx ? (mx - mn) / mx : 0;
      if (mx < .12) continue;
      const w = sat * sat * mx, k = Math.floor(hue(r, g, b) / 30) % 12;
      Object.assign(bins[k], { w: bins[k].w + w, r: bins[k].r + r * w, g: bins[k].g + g * w, b: bins[k].b + b * w });
    }
    const best = bins.reduce((a, b) => b.w > a.w ? b : a);
    if (best.w < 2) return untint();
    const r = best.r / best.w, g = best.g / best.w, b = best.b / best.w;
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), dd = mx - mn, l = (mx + mn) / 2;
    const s = dd ? dd / (1 - Math.abs(2 * l - 1)) : 0;
    const root = document.documentElement.style;
    root.setProperty("--h", Math.round(hue(r, g, b)));
    root.setProperty("--s", Math.round(Math.min(.78, Math.max(.32, s)) * 100) + "%");
  } catch (_) { untint(); }
}
function untint() { const r = document.documentElement.style; r.setProperty("--h", 232); r.setProperty("--s", "42%"); }

function showSong() {
  const song = CTX.song();
  const art = byId("jacketart");
  const diff = S.difficulty.charAt(0) + S.difficulty.slice(1).toLowerCase();
  if (isHL()) {
    // no jacket of its own: the tile the game composes (opt-event.js highlightTile), its orange
    art.className = "";
    art.innerHTML = highlightTile();
    const root = document.documentElement.style;
    root.setProperty("--h", 41); root.setProperty("--s", "78%");
    byId("songtitle").innerHTML = `Highlight <span class="tag">Event</span>`;
    // the pool before a run is read from the songs list (the search reads its own, the same rule)
    const hp = highlightPool(D);
    const tip = hp.pool.map(s => s.title).join(" · ");
    byId("singer").innerHTML = [
      `<span class="diff ${S.difficulty.toLowerCase()}">${esc(diff)}</span>`,
      `<span title="${esc(tip)}"><span class="num">${fmt(hp.pool.length)}</span> songs of the event holomems</span>`,
      `<span>Highlight Bonus <span class="num">+${hlPct(S.boosts)}%</span> instead of the song length bonus</span>`].join("");
    document.title = "Highlight · Optimizer";
    return;
  }
  if (song) {
    const src = `/jacket/${encodeURIComponent(song.music_id)}`;
    art.className = "";
    art.innerHTML = `<img id="jacketimg" alt="${esc(song.title)} jacket">`;
    const im = byId("jacketimg"); im.onload = () => tint(im); im.onerror = () => untint(); im.src = src;
    const evTag = isEvent() && isEventSong(song.music_id) ? ` <span class="tag">Event song</span>` : "";
    byId("songtitle").innerHTML = esc(song.title || song.music_id) + evTag;
    // one line under the title: singer, the chart's level, notes and length
    const bits = [song.singer ? esc(song.singer) : "",
      `<span class="diff ${S.difficulty.toLowerCase()}">${song.difficulty_level != null ? `<b>${song.difficulty_level}</b>` : ""}${esc(diff)}</span>`,
      song.notes != null ? `<span class="num">${fmt(song.notes)}</span> notes` : "",
      song.seconds != null ? `<span class="num">${mmss(song.seconds)}</span>` : ""];
    const ev = evSong(song.music_id);
    if (isEvent() && ev && ev.length_permil != null) bits.push(`Song length bonus <span class="num">+${dec((ev.length_permil - 1000) / 10, 0)}%</span> (estimate)`);
    byId("singer").innerHTML = bits.filter(Boolean).map(x => `<span>${x}</span>`).join("");
    document.title = `${song.title} · Optimizer`;
  } else {
    // "in general": no single song, so no one jacket; the game's own music sprite stands in
    art.className = "o-every";
    art.innerHTML = `<i class="ico" style="--ico:url(/ui/music/ui_999_ico_music_l.png)" aria-hidden="true"></i>`;
    untint();
    byId("songtitle").textContent = "Every song";
    byId("singer").innerHTML = S.goal === "score" && S.lvmode === "range"
      ? `<span>Average over every chart of level ${S.lvlo}–${S.lvhi}, any difficulty</span>`
      : `<span>Average over every ${esc(diff)} chart</span><span><span class="num">${fmt(D.songs.length)}</span> charts</span>`;
    document.title = "Optimizer · hololive builder";
  }
}
const evSong = id => (D.evsongs || []).find(s => s.music_id === id) || null;
const isEventSong = id => !!(D.event && (D.event.event_songs || []).includes(id));

/* ------------------------------------------------------------------ the options bar */
// One line under the header: the search basis as a summary (opens More options), the cached
// answer's provenance (opt-score.js fills #prov), and when the account was read.
const LABEL = {
  cards: { asis: "My cards", whatif: "Upgraded cards", maxed: "Maxed account" },
  boards: { develop: "Boards planned", current: "Boards as they are" },
  sort: { avg: "by Expected", max: "by Max" },
  evgoal: { event_points: "Event Points", event_song: "Event Song Score" },
};
function summary() {
  if (isEvent()) return [LABEL.evgoal[S.goal], `Live Boost ${S.boosts}`, S.goal === "event_points" ? `HoloPass ${S.holopass ? "on" : "off"}` : "",
    LABEL.cards[S.cards], S.cards === "maxed" ? "" : LABEL.boards[S.boards]].filter(Boolean).join(" · ");
  return [LABEL.cards[S.cards], LABEL.boards[S.boards], LABEL.sort[S.sort]].join(" · ");
}
function renderFresh() {
  const M = D.meta;
  const prov = byId("prov") ? byId("prov").innerHTML : "";
  const n = moreCount();
  byId("fresh").innerHTML = `
    <span class="o-sum" title="The options of the next search: change them with Options">${esc(summary())}</span>
    <span class="o-prov" id="prov">${prov}</span>
    <span class="when">${!M || M.error ? `Account unavailable${M && M.error ? `: ${esc(M.error)}` : ""}` : `Account read <span class="num">${esc(when(M.snapshot_mtime))}</span>`}</span>
    <button class="btn small" type="button" id="refresh"${REFRESHING ? " disabled" : ""}><svg class="i s"><use href="#i-refresh"/></svg>${REFRESHING ? "Refreshing…" : "Refresh"}</button>`;
  byId("refresh").onclick = refresh;
  const cnt = byId("optcount");
  cnt.hidden = !n; cnt.textContent = n ? `+${n}` : ""; cnt.title = n ? `${n} more option${n > 1 ? "s" : ""} set` : "";
  byId("moreopts").onclick = () => byId("more").showModal();
  // the account's own figures live in the More options sheet
  byId("acct").innerHTML = !M || M.error ? `<div><dt>Account</dt><dd>unavailable</dd></div>` : [
    M.owned != null ? ["Cards", `${M.owned} / ${M.catalogue}`] : null,
    M.boards_with_nodes != null ? ["Holomem Boards", M.boards_with_nodes] : null,
    M.memory_bonus_permil != null ? ["Memory Bonus", `+${dec(M.memory_bonus_permil / 10, 1)}%`] : null,
    M.power_up_permyriad != null ? ["Member Power-Up Bonus", `+${dec(M.power_up_permyriad / 100, 2)}%`] : null,
    ["Read", when(M.snapshot_mtime)],
  ].filter(Boolean).map(([k, v]) => `<div><dt>${k}</dt><dd class="num">${esc(String(v))}</dd></div>`).join("");
  const sc = byId("scenario");
  // Score and Event alike: both goals offer the maxed account, so the notice follows S.cards
  // whichever goal is shown
  sc.hidden = S.cards !== "maxed";
  sc.textContent = "Maxed account: every card in the game at max level and bloom, every holomem at max level with its Holomem Board complete (453 Pt); your cubes, hexas and Hologold are not used."
    + (isEvent() ? " Every event card counts at bloom 5." : "");
}
// a refresh in flight: the button stays "Refreshing…" across re-renders and across a
// page reload (the server says so in /api/meta), so a second one is never started
let REFRESHING = false;
async function refresh() {
  REFRESHING = true; renderFresh();
  toast("Refreshing your account: cards, boards, songs. This takes a few minutes.");
  try {
    const r = await postJSON("/api/refresh", {});
    // the game refuses the account (DATE_CHANGED) until the game itself is opened once on the new
    // game day; the project never does that daily login for you
    toast(r.returncode === 0 ? "Refresh finished."
      : r.returncode === 3 ? "hololive Dreams is in maintenance: nothing was refreshed. Refresh again once it ends."
      : r.returncode === 4 ? "This PC does not have the game's build constants yet: run refresh.cmd once (it finds them in your game install). Nothing was refreshed."
      : /DATE_CHANGED/.test(r.stdout || "") ? "The game refused your account for today: open hololive Dreams once, then refresh again."
      : `Refresh exited ${r.returncode}: partial (see the server log).`);
    D.meta = await getJSON("/api/meta");
  } catch (e) {
    toast("Refresh failed: " + e.message);
    if (/already running/.test(e.message)) return watchRefresh();
  }
  REFRESHING = false; renderFresh();
}
// a refresh started before this page loaded: wait for the server to finish it
async function watchRefresh() {
  REFRESHING = true; renderFresh();
  for (;;) {
    await new Promise(ok => setTimeout(ok, 10000));
    const m = await getJSON("/api/meta").catch(() => null);
    if (m && !m.refresh_running) { D.meta = m; break; }
  }
  REFRESHING = false; renderFresh(); toast("Refresh finished.");
}

/* ------------------------------------------------------------------ options */
function setPressed(group, value) {
  group.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.v === String(value))));
}
function bindSeg(id, key, cast = v => v) {
  const g = byId(id);
  setPressed(g, key === "holopass" ? (S[key] ? "1" : "0") : S[key]);
  g.addEventListener("click", e => {
    const b = e.target.closest("button[data-v]"); if (!b) return;
    S[key] = cast(b.dataset.v); setPressed(g, b.dataset.v); changed(key);
  });
}
function applyGoal() {
  // Highlight ranks by Event Points only: Event Song Score is off while it is the song
  const evs = byId("evgoal").querySelector('[data-v="event_song"]');
  evs.disabled = isHL();
  evs.title = isHL() ? "Highlight ranks by Event Points only" : "";
  document.querySelectorAll("[data-goal]").forEach(el => {
    const g = el.dataset.goal;
    el.hidden = !(g === S.goal || (g === "event" && isEvent()));
  });
  byId("assumerow").hidden = S.cards !== "whatif";
  // every card maxed: the boards are complete by definition (nothing to choose)
  byId("boardgroup").hidden = S.cards === "maxed";
  byId("resetrow").hidden = !(S.boards === "develop" && !S.infmats);
  byId("resetgainrow").hidden = byId("resetrow").hidden;
  byId("planrows").hidden = S.boards !== "develop";
  byId("refundrow").hidden = !(S.boards === "develop" && !S.infmats);
  byId("lvgroup").hidden = !!S.song || S.goal !== "score";
  byId("lvrow").hidden = S.lvmode !== "range";
  byId("boostx").textContent = `×${AC[S.boosts - 1]} Event Pt`;
  setPressed(byId("goal"), isEvent() ? "event" : "score");
  if (isEvent()) setPressed(byId("evgoal"), S.goal);
}
// options set beyond the three the summary names (Cards, Boards, Sort)
function moreCount() {
  const shared = [S.lmember, S.nooverlap, S.require.length, S.exclude.length,
    S.boards === "develop" && (S.maxpoints || S.infmats || S.allinvited || S.targets.length || S.branches !== "GBRY" || S.maxresets !== "-1" || S.resetgain !== "0.01")];
  if (isEvent()) return [...shared, S.platform !== "mobile", S.auto].filter(Boolean).length;
  return [...shared, S.pool !== "all", S.platform !== "mobile", S.auto, S.exact].filter(Boolean).length;
}
async function changed(key) {
  if (key === "pool") prunePins();
  save(); applyGoal();
  if (key === "difficulty") await loadSongs();
  if (key === "goal" || key === "boosts" || key === "difficulty") {
    if (isEvent()) await loadEventSongs();
    if (isEvent() && !S.song) S.song = defaultEventSong();
  }
  if (["goal", "song", "difficulty", "cards", "boards", "maxpoints", "infmats", "maxresets", "resetgain", "alevel", "abloom", "aspt", "keephigher", "allinvited", "norefund", "branches", "targets", "lvmode", "lvlo", "lvhi", "boosts", "holopass"].includes(key)) { setProv(""); showSong(); renderFresh(); showIdle(); }
  else if (key === "sort" || key === "top") { renderFresh(); showIdle(true); }
  else if (isEvent()) { renderFresh(); showIdle(); }   // a leader or pinned-card change: the event answer shown no longer applies
  else renderFresh();
}

/* the More options sheet */
function wireMore() {
  byId("optboards").checked = S.boards === "develop";
  byId("optboards").addEventListener("change", e => { S.boards = e.target.checked ? "develop" : "current"; changed("boards"); });
  const checks = { nooverlap: "nooverlap", maxpoints: "maxpoints", infmats: "infmats", keephigher: "keephigher", allinvited: "allinvited", norefund: "norefund", auto: "auto", exact: "exact" };
  for (const [id, key] of Object.entries(checks)) {
    byId(id).checked = !!S[key];
    byId(id).addEventListener("change", e => { S[key] = e.target.checked; changed(key); });
  }
  byId("alevel").value = S.alevel; byId("abloom").value = S.abloom;
  byId("alevel").onchange = e => { S.alevel = e.target.value; changed("alevel"); };
  byId("maxresets").value = S.maxresets;
  byId("maxresets").onchange = e => { S.maxresets = e.target.value; changed("maxresets"); };
  byId("resetgain").value = S.resetgain;
  byId("resetgain").onchange = e => { S.resetgain = e.target.value; changed("resetgain"); };
  byId("abloom").onchange = e => { S.abloom = e.target.value; changed("abloom"); };
  byId("aspt").value = S.aspt;
  byId("aspt").onchange = e => { S.aspt = e.target.value; changed("aspt"); };
  // branches the plan may change: at least one stays on
  const paintBranches = () => byId("branches").querySelectorAll("[data-b]").forEach(b => b.setAttribute("aria-pressed", S.branches.includes(b.dataset.b)));
  paintBranches();
  byId("branches").addEventListener("click", e => {
    const b = e.target.closest("[data-b]"); if (!b) return;
    const has = S.branches.includes(b.dataset.b);
    const next = "GBRY".split("").filter(x => x === b.dataset.b ? !has : S.branches.includes(x)).join("");
    if (!next) { toast("Keep at least one branch."); return; }
    S.branches = next; paintBranches(); changed("branches");
  });
  bindSeg("lvmode", "lvmode");
  for (const id of ["lvlo", "lvhi"]) {
    byId(id).value = S[id];
    byId(id).onchange = e => {
      const v = Math.max(1, Math.min(40, Math.round(+e.target.value || 1)));
      S[id] = v; e.target.value = v;
      if (S.lvlo > S.lvhi) { S.lvlo = S.lvhi = v; byId("lvlo").value = byId("lvhi").value = v; }
      changed(id);
    };
  }
  initTargets(S, D.cards.values(), () => changed("targets"));
  bindSeg("pool", "pool"); bindSeg("platform", "platform");
  bindSeg("top", "top", Number); bindSeg("evtop", "evtop", Number);
}
const pinDraw = {};
// Card pool switched to 5★ only: a member pinned below 5★ can no longer be in the team
function prunePins() {
  if (S.pool !== "5star") { if (pinDraw.require) pinDraw.require(); return; }
  const drop = S.require.filter(id => String((D.cards.get(id) || {}).rarity) !== "5");
  if (drop.length) {
    S.require = S.require.filter(id => !drop.includes(id));
    toast(`Card pool is 5★ only: unpinned ${drop.map(id => (D.cards.get(id) || {}).member || id).join(", ")} from Must include.`);
  }
  if (pinDraw.require) pinDraw.require();
}
function fillLeaders() {
  const owned = [...D.cards.values()].filter(c => c.owned);
  const members = [...new Set(owned.map(c => c.member))].sort((a, b) => a.localeCompare(b));
  byId("lmember").insertAdjacentHTML("beforeend", members.map(m => `<option>${esc(m)}</option>`).join(""));
  const outfits = () => {
    const o = byId("loutfit");
    o.innerHTML = `<option value="">The best outfit she has</option>` + owned.filter(c => c.member === S.lmember)
      .map(c => `<option value="${esc(c.id)}">${esc(c.costume_name || c.id)} · ${c.rarity}★</option>`).join("");
    o.disabled = !S.lmember; o.value = S.loutfit || "";
  };
  byId("lmember").value = S.lmember || ""; if (byId("lmember").value !== (S.lmember || "")) S.lmember = "";
  outfits();
  byId("lmember").onchange = e => { S.lmember = e.target.value; S.loutfit = ""; outfits(); changed("lmember"); };
  byId("loutfit").onchange = e => { S.loutfit = e.target.value; changed("loutfit"); };
  // the card pins: Must include / Ban (ids of owned cards). The leader is chosen above
  // (holomem + outfit), so there is no separate "Leader card" pin
  const label = c => `${c.member} / ${c.costume_name || c.id} (${c.rarity}★)`;
  const byLabel = new Map(owned.map(c => [label(c), c.id]));
  byId("cardlist").innerHTML = owned.map(c => `<option value="${esc(label(c))}">`).join("");
  // Must include = a MEMBER: with Card pool "5★ only" it offers 5★ cards only; the leader pin
  // and the ban list keep every card (outfits are not in the pool filter)
  byId("cardlist").insertAdjacentHTML("afterend", `<datalist id="cardlist5">${owned.filter(c => String(c.rarity) === "5").map(c => `<option value="${esc(label(c))}">`).join("")}</datalist>`);
  const memberOK = id => S.pool !== "5star" || String((D.cards.get(id) || {}).rarity) === "5";
  for (const key of ["require", "exclude"]) {
    const box = byId("pin-" + key);
    const draw = () => {
      const list = key === "require" && S.pool === "5star" ? "cardlist5" : "cardlist";
      box.innerHTML = S[key].map(id => { const c = D.cards.get(id); return `<span class="o-chip">${esc(c ? label(c) : id)}<button type="button" data-unpin="${esc(id)}" aria-label="Remove">×</button></span>`; }).join("")
        + `<input list="${list}" placeholder="Type a holomem or outfit, pick a card${list === "cardlist5" ? " (5★ only)" : ""}" aria-label="Add a card">`;
      box.querySelector("input").addEventListener("change", e => {
        const id = byLabel.get(e.target.value);
        if (id && !S[key].includes(id) && (key !== "require" || memberOK(id))) { S[key] = [...S[key], id]; changed(key); draw(); }
        else e.target.value = "";
      });
    };
    pinDraw[key] = draw;
    box.addEventListener("click", e => { const b = e.target.closest("[data-unpin]"); if (b) { S[key] = S[key].filter(x => x !== b.dataset.unpin); changed(key); draw(); } });
    draw();
  }
}

/* ------------------------------------------------------------------ song picker */
function openPicker() {
  // the shared picker (hb-songpick.js): predictive search over songs, talents and groups,
  // holodori.best-style sorts; event goals open on the songs ranked by the length bonus, best first
  const sorts = [];
  if (isEvent() && D.evsongs) {
    const rank = new Map(D.evsongs.map((s, i) => [s.music_id, i]));
    sorts.push({ key: "event", label: "Length bonus", cmp: (a, b) => (rank.get(a.music_id) ?? 1e9) - (rank.get(b.music_id) ?? 1e9) });
  }
  openSongPicker(byId("picker"), {
    songs: D.songs, current: S.song || (isEvent() ? null : ""), sorts,
    // the event's Highlight pick (Event Points only), the first tile on an event goal
    specials: isEvent() ? [{ key: HL, title: "Highlight", sub: `Random of ${highlightPool(D).pool.length} event songs · +${hlPct(S.boosts)}%`, art: highlightTile(), words: "highlight pickup random multi live event holomems" }]
      : [{ key: "", title: "In general", sub: "Every song, on average", icon: "i-globe", words: "every song in general average" }],
    sub: s => {
      const ev = isEvent() ? evSong(s.music_id) : null;
      return ev && ev.length_permil != null
        ? `<span class="evs">+${dec((ev.length_permil - 1000) / 10, 0)}% length${isEventSong(s.music_id) ? " · event song" : ""}</span>`
        : `<span>${esc(s.singer)} · ${esc(S.difficulty)} ${s.difficulty_level ?? ""}</span>`;
    },
    onPick: key => {
      S.song = key;
      if (fixHighlight()) { toast("Highlight ranks by Event Points only: switched to Event Points."); changed("goal"); return; }
      changed("song");
    },
  });
}

/* ------------------------------------------------------------------ run / idle */
let busy = false;
function setBusy(on) {
  busy = on;
  byId("play").disabled = on;
  byId("playlabel").textContent = on ? "Searching…" : "Find best team";
}
function showIdle(keepLive) {
  if (busy) return;
  if (isEvent()) showEventIdle(CTX, keepLive); else showScoreIdle(CTX, keepLive);
}
async function play() {
  if (busy) return;
  if (isEvent()) {
    if (!S.song) { toast("Choose a song first."); openPicker(); return; }
    runEvent(CTX);
  } else runScore(CTX);
  byId("out").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
}
document.addEventListener("click", e => {
  if (e.target.closest("[data-cancel]")) { if (isEvent()) cancelEvent(); else cancelScore(); return; }
  const c = e.target.closest("[data-close]"); if (c) { c.closest("dialog").close(); return; }
});
// a click on the backdrop closes a dialog -- only when the press began there too: a press inside
// that ends elsewhere (a dropdown opening under the pointer) would otherwise be attributed to the
// backdrop and close "More options" from its leader dropdown
document.querySelectorAll("dialog").forEach(d => {
  let fromBackdrop = false;
  d.addEventListener("pointerdown", e => { fromBackdrop = e.target === d; });
  d.addEventListener("click", e => { if (e.target === d && fromBackdrop) d.close(); });
});

/* ------------------------------------------------------------------ loading */
async function loadSongs() {
  try {
    const doc = await getJSON(`/api/songs?difficulty=${encodeURIComponent(S.difficulty)}`);
    D.songs = (doc.songs || []).filter(s => s.notes != null || s.difficulty_level != null);
  } catch (e) { D.songs = []; toast("Songs unavailable: " + e.message); }
  if (S.song && !isHighlight(S.song) && !D.songs.some(s => s.music_id === S.song)) { toast(`No ${S.difficulty} chart for ${S.song}.`); S.song = ""; }
}
async function loadEventSongs() {
  try { D.evsongs = (await getJSON(`/api/event/songs?boosts=${S.boosts}&difficulty=${encodeURIComponent(S.difficulty)}`)).songs || []; }
  catch (_) { D.evsongs = []; }
}
const defaultEventSong = () => ((D.evsongs || []).find(s => s.chart_available !== false && s.length_permil != null) || {}).music_id || "";

async function init() {
  wirePopovers();
  // difficulty segment from /api/meta's list
  const [meta, coll] = await Promise.all([
    getJSON("/api/meta").catch(e => ({ error: e.message })),
    getJSON("/api/collection").catch(() => ({ cards: [] })),
    loadRanks(), loadGradeRanks(),
  ]);
  D.meta = meta;
  for (const c of coll.cards || []) D.cards.set(c.id, normCard(c));
  const diffs = (meta.difficulties && meta.difficulties.available) || ["EASY", "NORMAL", "HARD", "EXPERT"];
  if (!diffs.includes(S.difficulty)) S.difficulty = "EXPERT";
  byId("difficulty").innerHTML = diffs.map(d => `<button type="button" data-v="${d}" aria-pressed="false">${d[0] + d.slice(1).toLowerCase()}</button>`).join("");
  byId("boosts").innerHTML = AC.map((_, i) => `<option value="${i + 1}">${i + 1}</option>`).join("");
  byId("boosts").value = String(S.boosts);
  byId("boosts").onchange = e => { S.boosts = +e.target.value; changed("boosts"); };
  // Goal: Score or Event; which event figure ranks the teams is chosen in More options
  byId("goal").addEventListener("click", e => {
    const b = e.target.closest("button[data-v]"); if (!b) return;
    const g = b.dataset.v === "event" ? (S.evgoal || "event_points") : "score";
    if (g !== S.goal) {
      const hadHL = isHL();
      S.goal = g; fixHighlight();
      if (hadHL && g === "score") toast("Highlight is an Event song: the Score goal searches every song.");
      changed("goal");
    }
  });
  byId("evgoal").addEventListener("click", e => {
    const b = e.target.closest("button[data-v]"); if (!b || b.dataset.v === S.goal) return;
    if (b.dataset.v === "event_song" && isHL()) { toast("Highlight ranks by Event Points only: choose a song for Event Song Score."); return; }
    S.goal = S.evgoal = b.dataset.v; changed("goal");
  });
  bindSeg("difficulty", "difficulty");
  bindSeg("cards", "cards"); bindSeg("sort", "sort");
  bindSeg("holopass", "holopass", v => v === "1");
  wireMore(); fillLeaders(); prunePins();
  byId("jacket").onclick = openPicker; byId("change").onclick = openPicker; byId("play").onclick = play;
  await loadSongs();
  try { D.event = await getJSON("/api/event"); } catch (_) { D.event = null; }
  if (isEvent()) { await loadEventSongs(); if (!S.song) S.song = defaultEventSong(); }
  save(); applyGoal(); showSong(); renderFresh(); showIdle();
  if (meta.refresh_running) watchRefresh();
}
init();
