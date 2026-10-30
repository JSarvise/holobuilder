// Compare page: two to four hand-picked teams scored under ONE
// set of assumptions by POST /api/compare, laid out side by side or stacked, with every
// difference shown against team A. Nothing here scores anything: the server's numbers,
// this file's layout. The teams and the options live in localStorage ("hb.versus").
import { byId, esc, fmt, dec, sgn, mmss, toast, getJSON, postJSON, wirePopovers, normCard, info, reveal, LETTERS, MAX_TEAMS, loadRanks, thumb } from "/vs-util.js";
import { loadRanks as loadGradeRanks, score as padScore } from "/hb-grade.js";
import { openSongPicker as songPicker } from "/hb-songpick.js";
import { lineup, outfitLine, unitPanel, runPanel, timelinePanel, wireTimeline, boardPanel, glanceTable, gatesTable, skillsTable,
  teamHead, driverLine, covPeak, headline, headLabel, slotOrder } from "/vs-detail.js";

/* ------------------------------------------------------------------ state */
const emptyTeam = () => ({ cards: [null, null, null, null, null, null], ov: [null, null, null, null, null, null], from: null });
const DEFAULTS = {
  scope: "song", song: "", difficulty: "EXPERT", cards: "asis", boards: "develop", layout: "auto",
  alevel: "account", abloom: "account", maxpoints: false, platform: "mobile", auto: false, model: "finite",
  teams: [emptyTeam(), emptyTeam()],
};
const S = structuredClone(DEFAULTS);
try {
  const saved = JSON.parse(localStorage.getItem("hb.versus") || "{}");
  Object.assign(S, saved);
  if (!Array.isArray(S.teams) || S.teams.length < 2) S.teams = [emptyTeam(), emptyTeam()];
  S.teams = S.teams.slice(0, MAX_TEAMS).map(t => ({
    cards: Array.from({ length: 6 }, (_, i) => (t.cards && t.cards[i]) || null),
    ov: Array.from({ length: 6 }, (_, i) => (t.ov && t.ov[i]) || null), from: t.from || null }));
} catch (_) { /* a broken save is an empty page, never a crash */ }
{ // deep link: /versus?song=m0334&difficulty=EXPERT
  const q = new URLSearchParams(location.search);
  if (q.has("song")) { S.song = q.get("song") || ""; S.scope = S.song ? "song" : "across"; }
  if (q.has("difficulty")) S.difficulty = String(q.get("difficulty")).toUpperCase();
}
function save() {
  try { localStorage.setItem("hb.versus", JSON.stringify(S)); } catch (_) { /* private mode */ }
  const q = new URLSearchParams();
  if (S.scope === "song" && S.song) q.set("song", S.song);
  if (S.difficulty !== "EXPERT") q.set("difficulty", S.difficulty);
  history.replaceState(null, "", "/versus" + (q.toString() ? "?" + q : ""));
}
const basisOf = () => S.cards === "maxed" ? "maxed" : S.cards === "maxed-account" ? "maxed-account" : "account";
// "state" (As ranked): each team on the Optimizer's own leaf board, song scope only, on a two-stage basis
const stateOK = () => S.scope === "song" && basisOf() !== "maxed-account" && S.cards !== "whatif";
const boardOf = () => basisOf() === "maxed-account" ? "develop" : S.boards === "state" && !stateOK() ? "develop" : S.boards;
const tsBasis = () => basisOf() !== "account" ? "maxed" : (S.maxpoints ? "account-max" : "account");
const fromKey = () => JSON.stringify([S.scope === "song" ? S.song : "", S.difficulty, basisOf(), S.maxpoints]);

/* ------------------------------------------------------------------ data */
const D = { songs: [], cards: new Map(), wallet: null };
const song = () => D.songs.find(s => s.music_id === S.song) || null;
const card = id => D.cards.get(id) || null;

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
  const s = S.scope === "song" ? song() : null;
  const art = byId("jacketart"), dl = S.difficulty[0] + S.difficulty.slice(1).toLowerCase();
  if (s) {
    const src = `/jacket/${encodeURIComponent(s.music_id)}`;
    art.className = "";
    art.innerHTML = `<img id="jacketimg" alt="${esc(s.title)} jacket">`;
    const im = byId("jacketimg"); im.onload = () => tint(im); im.onerror = () => untint(); im.src = src;
    byId("blur").src = src; byId("blur").hidden = false;
    byId("eyebrow").textContent = "Compare teams on";
    byId("songtitle").textContent = s.title || s.music_id;
    byId("singer").textContent = s.singer || "";
    const chips = [`<span class="diff ${String(S.difficulty).toLowerCase()}">${s.difficulty_level != null ? `<b>${s.difficulty_level}</b>` : ""}${esc(S.difficulty.charAt(0) + S.difficulty.slice(1).toLowerCase())}</span>`];
    if (s.notes != null) chips.push(`<span><span class="num">${fmt(s.notes)}</span> notes</span>`);
    if (s.seconds != null) chips.push(`<span class="num">${mmss(s.seconds)}</span>`);
    byId("chips").innerHTML = chips.join("");
    document.title = `${s.title} · Compare`;
  } else {
    const pick = D.songs.slice(0, 4);
    art.className = "v-mosaic";
    art.innerHTML = pick.map(x => `<img src="/jacket/${encodeURIComponent(x.music_id)}" alt="">`).join("");
    byId("blur").hidden = true; untint();
    byId("eyebrow").textContent = "Compare teams";
    if (S.scope === "unit") {
      byId("songtitle").textContent = "Unit Score";
      byId("singer").textContent = "No song: the deck screen’s Unit Score";
      byId("chips").innerHTML = `<span>Overall Power × Score Bonus</span>`;
    } else {
      byId("songtitle").textContent = "Every song";
      byId("singer").textContent = `On average over every ${dl} chart`;
      byId("chips").innerHTML = `<span class="diff ${String(S.difficulty).toLowerCase()}">${esc(S.difficulty.charAt(0) + S.difficulty.slice(1).toLowerCase())}</span><span><span class="num">${fmt(D.songs.length)}</span> charts</span>`;
    }
    document.title = "Compare · hololive builder";
  }
}

/* ------------------------------------------------------------------ options */
function setPressed(group, value) {
  group.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.v === String(value))));
}
function bindSeg(id, key, cast = v => v) {
  const g = byId(id);
  setPressed(g, S[key]);
  g.addEventListener("click", e => {
    const b = e.target.closest("button[data-v]"); if (!b || b.disabled) return;
    S[key] = cast(b.dataset.v); setPressed(g, b.dataset.v); changed(key);
  });
}
function applyOpts() {
  byId("assumerow").hidden = S.cards !== "whatif";
  const forced = basisOf() === "maxed-account";
  byId("boards").querySelectorAll("button").forEach(b => { b.disabled = (forced && b.dataset.v !== "develop") || (b.dataset.v === "state" && !stateOK()); });
  setPressed(byId("boards"), boardOf());
  byId("maxpoints").disabled = forced;
  const n = [S.cards === "whatif" && (S.alevel !== "account" || S.abloom !== "account"), S.maxpoints && !forced, S.platform !== "mobile", S.auto, S.model !== "finite"].filter(Boolean).length;
  byId("morecount").hidden = !n; byId("morecount").textContent = n;
}
async function changed(key) {
  save(); applyOpts();
  if (key === "difficulty") { await loadSongs(); }
  if (["song", "difficulty", "scope"].includes(key)) showSong();
  if (key === "layout" && last) { render(last); return; }
  drawTeams();
  markStale();
  if (!last && !busy) idle();
}
function wireMore() {
  for (const id of ["maxpoints", "auto"]) {
    byId(id).checked = !!S[id];
    byId(id).addEventListener("change", e => { S[id] = e.target.checked; changed(id); });
  }
  byId("alevel").value = S.alevel; byId("abloom").value = S.abloom;
  byId("alevel").onchange = e => { S.alevel = e.target.value; changed("alevel"); };
  byId("abloom").onchange = e => { S.abloom = e.target.value; changed("abloom"); };
  bindSeg("platform", "platform"); bindSeg("model", "model");
  byId("moreopts").onclick = () => byId("more").showModal();
}

/* ------------------------------------------------------------------ the team editor */
const filled = t => t.cards.every(Boolean);
function drawTeams() {
  const host = byId("teams");
  host.innerHTML = S.teams.map((t, k) => {
    const lead = card(t.cards[0]);
    const slots = t.cards.map((id, s) => {
      const c = id ? card(id) : null;
      const role = s === 0 ? "Leader" : `Member ${s}`;
      // an empty slot is drawn in the game card's own frame (5:7, the face's corner radii), under the same role row
      if (!id) return `<button class="v-slot empty" type="button" data-pick="${k}:${s}" aria-label="Team ${LETTERS[k]}, ${role}: choose a card">
        <span class="v-role">&nbsp;</span>
        <span class="v-ph"><i class="ico" style="--ico:url(/ui/grade/ui_999_ico_plus.png)" aria-hidden="true"></i></span>
        <span class="v-slotcap"><b>${role}</b><small>Choose a card</small></span></button>`;
      const face = c ? { ...c, level: t.ov[s] && t.ov[s].level != null ? t.ov[s].level : c.level, bloom: t.ov[s] && t.ov[s].bloom != null ? t.ov[s].bloom : c.bloom }
        : { id, member: id, rarity: 3 };
      return `<button class="v-slot" type="button" data-pick="${k}:${s}" aria-label="Team ${LETTERS[k]}, ${role}: ${esc(c ? c.member : id)}, change">
        ${window.HBCard.face(face, { lead: s === 0, roleText: "Leader", w: 240 })}
        <span class="v-slotcap"><b>${esc(c ? c.costume_name || c.title || id : id)}</b><small>${esc(c ? c.member : "Not in the catalogue")}${c && !c.owned ? " · not owned" : ""}</small>
        ${t.ov[s] ? `<em class="v-ov">Lv ${t.ov[s].level ?? "—"} · Bloom ${t.ov[s].bloom ?? "—"} set by hand</em>` : ""}</span>
      </button>`;
    }).join("");
    const missing = t.cards.filter(c => !c).length;
    const acts = [
      k ? `<button type="button" data-act="ref" data-k="${k}">Make reference</button>` : "",
      S.teams.length < MAX_TEAMS ? `<button type="button" data-act="dup" data-k="${k}">Duplicate</button>` : "",
      t.cards.some(Boolean) ? `<button type="button" data-act="clear" data-k="${k}">Empty every slot</button>` : "",
      S.teams.length > 2 ? `<button type="button" data-act="del" data-k="${k}">Remove team</button>` : "",
    ].filter(Boolean).join("");
    return `<article class="v-ted" style="--tc:var(--t${k})">
      <header>
        <span class="v-badge lg" style="--tc:var(--t${k})">${LETTERS[k]}</span>
        <div class="v-tedname"><b>Team ${LETTERS[k]}${k === 0 ? ` <span class="tag muted">Reference</span>` : ""}</b><small>${missing ? `${missing} slot${missing === 1 ? "" : "s"} to fill` : lead ? `${esc(lead.member)} leads` : ""}</small></div>
        ${acts ? `<details class="v-menu"><summary class="v-icon" aria-label="Team ${LETTERS[k]} actions" title="Team ${LETTERS[k]} actions"><span aria-hidden="true">···</span></summary><div class="v-menu-b">${acts}</div></details>` : ""}
      </header>
      <div class="v-slots">${slots}</div>
    </article>`;
  }).join("");
  byId("addteam").hidden = S.teams.length >= MAX_TEAMS;
  const ready = S.teams.every(filled) && (S.scope !== "song" || !!S.song);
  const hint = busy ? "" : !S.teams.every(filled) ? "Fill every slot of every team to compare." : S.scope === "song" && !S.song ? "Choose a song." : "";
  const play = byId("play");
  play.disabled = busy || !ready;
  // the one filled button of the view: Compare only once it can run; until then the start block's button is
  play.classList.toggle("primary", ready || busy);
  play.title = hint;
  byId("playhint").textContent = "";
  if (!last && !busy) startBlock();
}
function teamAction(act, k) {
  if (act === "ref") { const [t] = S.teams.splice(k, 1); S.teams.unshift(t); }
  else if (act === "dup" && S.teams.length < MAX_TEAMS) S.teams.splice(k + 1, 0, structuredClone(S.teams[k]));
  else if (act === "clear") S.teams[k] = emptyTeam();
  else if (act === "del" && S.teams.length > 2) S.teams.splice(k, 1);
  save(); drawTeams(); markStale();
}
async function fillBest() {
  const tsBasis = basisOf() !== "account" ? "maxed" : (S.maxpoints ? "account-max" : "account");
  const songQ = S.scope === "song" ? S.song : "";
  const btn = byId("fillbest"); btn.disabled = true;
  try {
    const doc = await getJSON(`/api/two-stage?song=${encodeURIComponent(songQ)}&basis=${tsBasis}`);
    const rows = doc.rows || [];
    if (!rows.length) { toast("The Optimizer has no best teams for this yet. Run it first."); return; }
    const n = Math.min(rows.length, Math.max(2, S.teams.length));
    S.teams = rows.slice(0, n).map(r => ({ cards: [r.leader, ...r.members], ov: [null, null, null, null, null, null],
      from: { key: fromKey(), score: r.best } }));
    save(); drawTeams(); markStale();
    toast(`Filled ${n} teams from the Optimizer's best teams (${doc.scope === "general" ? "every song" : "this song"}).`);
  } catch (e) { toast("The Optimizer's best teams could not be read: " + e.message); }
  finally { btn.disabled = false; }
}

/* ------------------------------------------------------------------ the card picker */
const CP = { k: 0, s: 0, own: "owned", elem: "", rar: "" };
function openCardPicker(k, s) {
  Object.assign(CP, { k, s });
  byId("ctitle").textContent = `Team ${LETTERS[k]} · ${s === 0 ? "Leader" : "Member " + s}`;
  byId("cq").value = "";
  setPressed(byId("cown"), CP.own); setPressed(byId("celem"), CP.elem); setPressed(byId("crar"), CP.rar);
  drawCardGrid(); drawCardFoot();
  byId("cardpick").showModal(); byId("cq").focus();
}
function drawCardGrid() {
  const t = S.teams[CP.k], q = byId("cq").value.trim().toLowerCase();
  // members must be distinct holomems; the leader is a separate slot and may also be a member
  const otherMembers = CP.s === 0 ? new Set() : new Set(t.cards.filter((id, i) => id && i > 0 && i !== CP.s).map(id => (card(id) || {}).member));
  const list = [...D.cards.values()].filter(c => (CP.own === "all" || c.owned) && (!CP.elem || c.type === CP.elem) && (!CP.rar || c.rarity === +CP.rar)
    && (!q || `${c.member} ${c.costume_name || ""} ${c.title || ""}`.toLowerCase().includes(q)))
    .sort((a, b) => b.rarity - a.rarity || String(a.member).localeCompare(String(b.member)));
  byId("ccount").textContent = `${list.length} card${list.length === 1 ? "" : "s"}`;
  byId("cgrid").innerHTML = list.slice(0, 240).map(c => {
    const taken = otherMembers.has(c.member);
    const cur = t.cards[CP.s] === c.id;
    return `<button class="v-ctile" type="button" data-card="${esc(c.id)}" ${taken ? "disabled" : ""} ${cur ? 'aria-current="true"' : ""}
        title="${taken ? esc(c.member) + " is already a member of this team" : esc(c.member + " / " + (c.costume_name || ""))}">
      ${window.HBCard.face(c, { role: false, w: 200 })}
      <b>${esc(c.costume_name || c.title || c.id)}</b><span>${esc(c.member)}${c.owned ? "" : " · not owned"}</span></button>`;
  }).join("") || `<p class="v-noresult">No card matches. Clear a filter or show every card.</p>`;
}
function drawCardFoot() {
  const t = S.teams[CP.k], id = t.cards[CP.s], ov = t.ov[CP.s] || {};
  byId("cfoot").hidden = !id;
  if (!id) return;
  byId("clevel").value = ov.level == null ? "" : String(ov.level);
  byId("cbloom").value = ov.bloom == null ? "" : String(ov.bloom);
}
function setOverride() {
  const t = S.teams[CP.k];
  const lv = byId("clevel").value, bl = byId("cbloom").value;
  t.ov[CP.s] = lv === "" && bl === "" ? null : { level: lv === "" ? null : +lv, bloom: bl === "" ? null : +bl };
  t.from = null; save(); drawTeams(); markStale();
}
function wireCardPicker() {
  byId("cq").oninput = drawCardGrid;
  for (const [id, key] of [["cown", "own"], ["celem", "elem"], ["crar", "rar"]]) {
    byId(id).addEventListener("click", e => { const b = e.target.closest("button[data-v]"); if (!b) return; CP[key] = b.dataset.v; setPressed(byId(id), CP[key]); drawCardGrid(); });
  }
  byId("cgrid").addEventListener("click", e => {
    const b = e.target.closest("[data-card]"); if (!b || b.disabled) return;
    const t = S.teams[CP.k];
    if (t.cards[CP.s] !== b.dataset.card) { t.cards[CP.s] = b.dataset.card; t.ov[CP.s] = null; t.from = null; }
    save(); drawTeams(); markStale();
    // move on to the next empty slot of this team, or close
    const next = t.cards.findIndex(c => !c);
    if (next >= 0) { CP.s = next; byId("ctitle").textContent = `Team ${LETTERS[CP.k]} · ${next === 0 ? "Leader" : "Member " + next}`; byId("cq").value = ""; drawCardGrid(); drawCardFoot(); byId("cq").focus(); }
    else byId("cardpick").close();
  });
  byId("clevel").onchange = setOverride; byId("cbloom").onchange = setOverride;
  byId("cremove").onclick = () => { const t = S.teams[CP.k]; t.cards[CP.s] = null; t.ov[CP.s] = null; t.from = null; save(); drawTeams(); markStale(); byId("cardpick").close(); };
}

/* ------------------------------------------------------------------ song picker */
function openSongPicker() {
  // the shared picker (hb-songpick.js): the Optimizer's search, sorts and filters
  songPicker(byId("picker"), {
    songs: D.songs, current: S.scope === "song" ? S.song : S.scope === "across" ? "@across" : "@unit",
    specials: [
      { key: "@across", title: "In general", sub: "Every song, on average", icon: "i-globe", words: "every song in general average" },
      { key: "@unit", title: "Unit Score", sub: "No song: the deck screen", icon: "i-gauge", words: "unit score deck screen" }],
    sub: s => `<span>${esc(s.singer)} · ${esc(S.difficulty)} ${s.difficulty_level ?? ""}</span>`,
    onPick: key => {
      if (key === "@across") S.scope = "across"; else if (key === "@unit") S.scope = "unit"; else { S.scope = "song"; S.song = key; }
      changed("scope");
    },
  });
}

/* ------------------------------------------------------------------ the comparison */
let busy = false, last = null, runAbort = null;
const openPlans = new Set([0]);
function optsKey() {
  return JSON.stringify([S.scope, S.song, S.difficulty, S.cards, boardOf(), S.alevel, S.abloom, S.maxpoints, S.platform, S.auto, S.model,
    S.teams.map(t => [t.cards, t.ov])]);
}
function markStale() {
  const el = byId("stale"); if (!el) return;
  el.hidden = !last || last.key === optsKey();
}
function pairBody(a, b, la, lb) {
  const st = boardOf() === "state";
  const body = {
    teams: [[a, la], [b, lb]].map(([t, leaf]) => st && leaf ? { cards: t.cards, board_state: leaf.board_state, member_order: leaf.member_order }
      : { cards: t.cards, overrides: t.ov }), basis: basisOf(), board: boardOf(),
    difficulty: S.difficulty, platform: S.platform, uptime_model: S.model, auto: !!S.auto,
  };
  if (S.scope === "song") { body.song = S.song; body.distribution = true; } else if (S.scope === "across") body.across = true;
  if (S.cards === "whatif") { body.assume_level = S.alevel; body.assume_bloom = S.abloom; }
  if (S.maxpoints && basisOf() !== "maxed-account" && boardOf() === "develop") body.budget = 361;
  return body;
}
function setBusy(on) {
  busy = on;
  byId("playlabel").textContent = on ? "Comparing…" : "Compare teams";
  drawTeams();
}
async function run() {
  if (busy) return;
  if (!S.teams.every(filled)) { toast("Fill every slot of every team first."); return; }
  if (S.scope === "song" && !S.song) { toast("Choose a song first."); openSongPicker(); return; }
  const key = optsKey(), n = S.teams.length;
  const snap = structuredClone(S.teams);
  byId("start").innerHTML = "";
  byId("fillbest").hidden = false;
  runAbort = new AbortController();
  setBusy(true);
  const t0 = performance.now();
  const prog = k => {
    byId("out").innerHTML = `<div class="v-progress"><div class="row"><span><b>Scoring team A against team ${LETTERS[k]}</b>
      <span class="num" style="color:var(--ink-3)">pair ${k} of ${n - 1}${boardOf() === "develop" ? " · Holomem Boards planned" : ""}</span></span>
      <button class="btn small" type="button" data-cancel>Cancel</button></div>
      <div class="bar"><i style="width:${Math.round((k - 1) / (n - 1) * 100)}%"></i></div></div>`;
  };
  try {
    // As ranked: each team's board and slot order from the Optimizer's own leaf, read first
    const leaves = [];
    if (boardOf() === "state") {
      if (snap.some(t => t.ov.some(Boolean))) throw new Error("As ranked scores your cards as they are: clear the level / bloom set by hand, or pick another Holomem Board option");
      for (let k = 0; k < n; k++) {
        byId("out").innerHTML = `<div class="v-progress"><div class="row"><span><b>Reading team ${LETTERS[k]}'s board from the Optimizer</b>
          <span class="num" style="color:var(--ink-3)">${k + 1} of ${n}</span></span>
          <button class="btn small" type="button" data-cancel>Cancel</button></div><div class="bar sweep"><i style="width:30%"></i></div></div>`;
        leaves.push(await postJSON("/api/row-detail", { song: S.song, difficulty: S.difficulty, basis: tsBasis(), cards: snap[k].cards }, runAbort.signal));
      }
    }
    const docs = [];
    for (let k = 1; k < n; k++) {
      prog(k);
      docs.push(await postJSON("/api/compare", pairBody(snap[0], snap[k], leaves[0], leaves[k]), runAbort.signal));
    }
    const R = assemble(docs, snap, key);
    R.teams.forEach((t, k) => { if (leaves[k]) t.leaf = leaves[k]; });
    R.elapsed = (performance.now() - t0) / 1000;
    last = R; openPlans.clear(); openPlans.add(0);
    render(R);
    byId("results").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
    details(R);
  } catch (e) {
    if (e.name === "AbortError") { toast("Comparison cancelled."); last ? render(last) : idle(); }
    else { console.error(e); byId("out").innerHTML = `<div class="v-error">The comparison failed: ${esc(e.message)}</div>`; }
  } finally { runAbort = null; setBusy(false); markStale(); }
}
function assemble(docs, snap, key) {
  const d0 = docs[0];
  const cards = new Map(D.cards);
  for (const doc of docs) for (const [id, c] of Object.entries(doc.cards || {})) {
    const cat = D.cards.get(id) || {};
    cards.set(id, normCard({ ...cat, ...c, id, costume_skill: cat.costume_skill || c.costume_skill }));
  }
  const teams = [{ idx: 0, label: "A", row: d0.teams[0], diff: null }];
  docs.forEach((doc, i) => teams.push({ idx: i + 1, label: LETTERS[i + 1], row: doc.teams[1], diff: doc.diff, refRow: doc.teams[0] }));
  const warns = [];
  const h0 = headline(d0.teams[0], d0.mode);
  docs.forEach((doc, i) => {
    const h = headline(doc.teams[0], doc.mode);
    if (Math.round(h) !== Math.round(h0)) warns.push(`Team A scored ${fmt(h)} in the pair with team ${LETTERS[i + 1]} but ${fmt(h0)} with team B: the pairs did not share one basis.`);
  });
  const cached = snap.map(t => t.from && t.from.key === fromKey() ? t.from.score : null);
  return { key, docs, d0, mode: d0.mode, board: d0.assumptions.board, teams, cards, warns, cached, scope: S.scope };
}
function ctxOf(R) {
  const n = R.teams.length;
  const side = S.layout === "side" || (S.layout === "auto" && n === 2);
  return { mode: R.mode, board: R.board, n, side, ref: R.teams[0], cardOf: id => R.cards.get(id) || null, wallet: D.wallet, walletErr: D.walletErr,
    song: R.d0.song, chartCount: R.d0.chart_count, covMax: Math.max(0, ...R.teams.map(covPeak)) };
}
function assumptionsLine(R) {
  const a = R.d0.assumptions;
  const cards = { account: "My cards", maxed: "Every card, maxed", "maxed-account": "Maxed account" }[a.basis] || a.basis;
  const bits = [R.mode === "song" ? `${R.d0.difficulty} chart` : R.mode === "across" ? `every ${R.d0.difficulty} chart (${R.d0.chart_count})` : "Unit Score",
    cards + (a.assume_level != null ? ` at level ${a.assume_level}` : "") + (a.assume_bloom != null ? `, bloom ${a.assume_bloom}` : ""),
    a.board === "state" ? "Holomem Boards as the Optimizer ranked each team" : a.board === "develop" ? `Holomem Boards planned (${a.budget === "real" ? "your Holomem Board Pt" : a.budget + " Pt each"})` : a.board === "captured" ? "Holomem Boards as they are" : "No Holomem Board",
    a.platform === "pc" ? "PC" : "Mobile", a.uptime_model === "steady" ? "deck-screen model" : "in-live model"];
  return bits.join(" · ");
}
function render(R) {
  const ctx = ctxOf(R);
  const T = R.teams;
  const plan = t => boardPanel(t, { ...ctx, planOpen: openPlans.has(ctx.side ? 0 : t.idx) });   // side by side: the plans open together
  const sideRow = (cls, f) => { const cells = T.map(t => f(t)); return cells.some(Boolean) ? `<div class="v-cols ${cls}" style="--n:${T.length}">${T.map((t, k) => `<div class="v-cell" style="--tc:var(--t${t.idx})">${cells[k] || ""}</div>`).join("")}</div>` : ""; };
  const blocks = ctx.side
    ? sideRow("head", t => teamHead(t, ctx) + lineup(t, ctx) + outfitLine(t, ctx) + driverLine(t)) +
      sideRow("", t => unitPanel(t, ctx)) + sideRow("", t => runPanel(t, ctx)) +
      sideRow("", t => timelinePanel(t, ctx)) + sideRow("", t => plan(t))
    : T.map(t => `<article class="v-team" style="--tc:var(--t${t.idx})">${teamHead(t, ctx)}${lineup(t, ctx)}${outfitLine(t, ctx)}${driverLine(t)}
        <div class="v-sect"><div class="v-duo">${unitPanel(t, ctx)}${runPanel(t, ctx)}</div></div>${timelinePanel(t, ctx)}${plan(t)}</article>`).join("");
  const obj = R.d0.objective || {};
  byId("out").innerHTML = `
    <div class="v-reshead">
      <div><h2>${headLabel(R.mode)}, ${T.length} teams</h2><p>${esc(assumptionsLine(R))} ${info("route")}</p></div>
      <div class="v-restools">
        <div class="seg" id="layout" role="group" aria-label="Layout">
          <button type="button" data-v="side" aria-pressed="${ctx.side}">Side by side</button>
          <button type="button" data-v="stack" aria-pressed="${!ctx.side}">Stacked</button>
        </div>
        <button class="btn small" type="button" data-copy><svg class="i s"><use href="#i-copy"/></svg>Copy as text</button>
      </div>
    </div>
    <p class="v-stale" id="stale" hidden><svg class="i s"><use href="#i-alert"/></svg>The teams or options changed since this comparison. Press Compare teams to score them again.</p>
    ${R.warns.length ? `<p class="v-warnline">${R.warns.map(esc).join("<br>")}</p>` : ""}
    <section class="v-block v-reveal">${glanceTable(R.mode, R.board, T, R.cached)}</section>
    <div class="v-layout ${ctx.side ? "side" : "stack"}">${blocks}</div>
    <section class="v-block v-reveal"><div class="v-sh"><h3>Conditions</h3>${info("gates")}</div>${gatesTable(T, ctx.cardOf)}</section>
    <details class="v-block v-fold"><summary><h3>Per-skill contribution</h3>${info("skills")}<svg class="i s go" aria-hidden="true"><use href="#i-chev"/></svg></summary>${skillsTable(T, ctx.cardOf)}</details>
    <p class="v-runinfo" title="${esc(`${R.d0.model_version || ""} · inputs ${String(R.d0.inputs_hash || "").slice(0, 12)}`)}">Scored in ${dec(R.elapsed || 0, 1)} s · ${T.length - 1} pair${T.length > 2 ? "s" : ""}${obj.leaf === "plan_cache" ? " · one Holomem Board plan shared by every team" : ""}</p>`;
  markStale();
  const out = byId("out");
  reveal(out);
  T.forEach(t => wireTimeline(out, t, ctx));
  out.querySelectorAll("details.v-plan").forEach(d => d.addEventListener("toggle", () => {
    const k = ctx.side ? 0 : +d.dataset.plan;
    if (d.open) openPlans.add(k); else openPlans.delete(k);
    if (ctx.side) out.querySelectorAll("details.v-plan").forEach(x => { if (x.open !== d.open) x.open = d.open; });
  }));
}
function repaint(R) {
  if (last !== R) return;
  const keepFold = [...byId("out").querySelectorAll("details.v-pr, details.v-fold")].map(d => d.open);
  const y = scrollY;
  render(R);
  byId("out").querySelectorAll(".v-reveal").forEach(x => x.classList.add("in"));
  byId("out").querySelectorAll("details.v-pr, details.v-fold").forEach((d, k) => { if (keepFold[k]) d.open = true; });
  scrollTo(0, y);
}
/* the per-team extras: the wallet, each team's timeline (song) and board-doc (planned boards) */
async function details(R) {
  const a = R.d0.assumptions, jobs = [];
  const add = p => jobs.push(p);
  // the wallet first and alone: read at the same moment as the board docs it once failed transiently
  if (!D.wallet && (R.board === "develop" || R.board === "state")) {
    try { const w = await getJSON("/api/resources"); if (w.available === false) D.walletErr = w.reason || "unavailable"; else { D.wallet = w; D.walletErr = null; } }
    catch (e) { D.walletErr = e.message; }
  }
  for (const t of R.teams) {
    const cards = t.row.cards;
    if (R.mode === "song") {
      add(postJSON("/api/timeline", { song: R.d0.song, cards, board: R.board !== "off", board_state: R.board === "develop" || R.board === "state" ? t.row.board_state : undefined,
        member_order: t.row.score.member_order || undefined,
        basis: a.basis, difficulty: R.d0.difficulty, platform: a.platform, uptime_model: a.uptime_model, auto: !!S.auto,
        assume_level: a.assume_level ?? undefined, assume_bloom: a.assume_bloom ?? undefined })
        .then(d => { if (d.available === false) t.tlErr = d.reason || "not available"; else t.tl = d; })
        .catch(e => { t.tlErr = e.message; }));
    }
    if ((R.board === "develop" || R.board === "state") && t.row.board_state && Object.keys(t.row.board_state).length) {
      add(postJSON("/api/board-doc", { board_state: t.row.board_state, cards, basis: a.basis, budget: a.budget === "real" ? undefined : a.budget })
        .then(d => { t.doc = d; }).catch(e => { t.docErr = e.message; }));
    }
  }
  await Promise.allSettled(jobs);
  repaint(R);
}
/* The empty state: the Optimizer's best teams, #1 and #2 already chosen. One press fills the
   teams and runs the comparison; hand-picking stays in the slots above. */
const START = { key: null, rows: null, pick: [], err: null };
const ready = () => S.teams.every(filled) && (S.scope !== "song" || !!S.song);
async function loadStart() {
  const key = JSON.stringify([fromKey(), S.scope]);
  if (START.key === key && START.rows) return;
  START.key = key; START.rows = null; START.err = null;
  try {
    const doc = await getJSON(`/api/two-stage?song=${encodeURIComponent(S.scope === "song" ? S.song : "")}&basis=${tsBasis()}`);
    if (START.key !== key) return;
    START.rows = (doc.rows || []).filter(r => r.leader && Array.isArray(r.members) && r.members.length === 5 && r.best != null)
      .sort((x, y) => y.best - x.best).slice(0, 6);
  } catch (e) { START.rows = []; START.err = e.message; }
  START.pick = START.rows.length >= 2 ? [0, 1] : [];
}
function startBlock() {
  const s = S.scope === "song" ? song() : null;
  const on = s ? esc(s.title || s.music_id) : S.scope === "unit" ? "the Unit Score" : "every song";
  const rows = START.rows;
  let body;
  if (rows == null) body = `<p class="v-start-l">Reading the Optimizer's best teams…</p>`;
  else if (!rows.length) body = `<p class="v-start-l">The Optimizer has no best teams for ${on} yet${START.err ? " (they could not be read)" : ""}. Run it on the Optimizer page, or choose each card in the slots above.</p>`;
  else {
    body = `<ol class="v-cands">${rows.map((r, i) => {
      const k = START.pick.indexOf(i);
      const lead = card(r.leader) || {};
      const [mem, title] = String(r.leader_name || "").split(" / ");
      return `<li><button type="button" class="v-cand" data-cand="${i}" aria-pressed="${k >= 0}" style="--tc:var(--t${Math.max(0, k)})">
        <span class="v-cand-k" aria-hidden="true">${k >= 0 ? LETTERS[k] : ""}</span>
        <span class="v-cand-rk num">#${i + 1}</span>
        <span class="v-cand-cards">${[r.leader, ...r.members].map((id, j) => `<img${j ? "" : ' class="ld"'} src="${thumb(id)}" alt="" loading="lazy" onerror="this.style.visibility='hidden'">`).join("")}</span>
        <span class="v-cand-nm"><b>${esc(mem || lead.member || r.leader)}</b><small>${esc(title || lead.costume_name || "")}</small></span>
        <span class="v-cand-sc">${padScore(r.best)}</span>
      </button></li>`;
    }).join("")}</ol>`;
  }
  const label = START.pick.length >= 2 ? `Compare ${START.pick.map(i => `#${i + 1}`).join(" vs ")}` : "Choose two teams";
  // with every slot empty the start block leads the page (above the slots); once a card is picked it moves below them
  const top = S.teams.every(t => !t.cards.some(Boolean));
  byId(top ? "out" : "start").innerHTML = "";
  byId("fillbest").hidden = top;   // the start block IS this action while every slot is empty
  byId(top ? "start" : "out").innerHTML = `<section class="v-start${top ? " top" : ""}" aria-labelledby="starth">
    <div class="v-start-h"><div><h2 id="starth">Start from the best teams</h2>
      <p>The Optimizer's best team for each leader on ${on}, by expected score. Choose two to four.</p></div>
      ${rows && rows.length ? `<button class="btn btn-lg${ready() ? "" : " primary"}" type="button" id="startgo"${START.pick.length < 2 ? " disabled" : ""}>${label}</button>` : ""}</div>
    ${body}
  </section>`;
}
function idle() {
  startBlock();
  loadStart().then(() => { if (!last && !busy) startBlock(); });
}
function startPick(i) {
  const k = START.pick.indexOf(i);
  if (k >= 0) START.pick.splice(k, 1);
  else if (START.pick.length < MAX_TEAMS) START.pick.push(i);
  else { toast(`At most ${MAX_TEAMS} teams.`); return; }
  START.pick.sort((x, y) => x - y);
  startBlock();
}
function startGo() {
  if (START.pick.length < 2 || !START.rows) return;
  S.teams = START.pick.map(i => {
    const r = START.rows[i];
    return { cards: [r.leader, ...r.members], ov: [null, null, null, null, null, null], from: { key: fromKey(), score: r.best } };
  });
  save(); drawTeams(); markStale();
  run();
}
function asText(R) {
  const ctx = ctxOf(R), name = id => { const c = ctx.cardOf(id) || {}; return `${c.costume_name || id} (${c.member || ""})`; };
  const s = song();
  const lines = [`Compare · ${R.mode === "song" ? (s ? s.title : R.d0.song) : R.mode === "across" ? "every song" : "Unit Score"} · ${assumptionsLine(R)}`];
  const h0 = headline(R.teams[0].row, R.mode);
  for (const t of R.teams) {
    const v = headline(t.row, R.mode);
    lines.push(`Team ${t.label}: ${fmt(v)}${t.idx ? ` (${sgn((v / h0 - 1) * 100, 2)} vs A)` : " (reference)"}`);
    const ids = slotOrder(t.row);
    lines.push(`  Leader: ${name(ids[0])}`);
    ids.slice(1).forEach((id, k) => lines.push(`  ${k + 1}. ${name(id)}`));
    if (t.diff) lines.push(`  vs A: ${t.diff.sentence}`);
  }
  lines.push(`${R.d0.model_version || ""} · inputs ${String(R.d0.inputs_hash || "").slice(0, 12)}`);
  return lines.join("\n");
}

/* ------------------------------------------------------------------ events */
document.addEventListener("click", e => {
  if (e.target.closest("[data-cancel]")) { if (runAbort) runAbort.abort(); return; }
  const cd = e.target.closest("[data-cand]");
  if (cd) { startPick(+cd.dataset.cand); return; }
  if (e.target.closest("#startgo")) { startGo(); return; }
  const pk = e.target.closest("[data-pick]");
  if (pk) { const [k, s] = pk.dataset.pick.split(":").map(Number); openCardPicker(k, s); return; }
  const act = e.target.closest("[data-act]");
  document.querySelectorAll("details.v-menu[open]").forEach(d => { if (!d.contains(e.target)) d.open = false; });
  if (act) { act.closest("details")?.removeAttribute("open"); teamAction(act.dataset.act, +act.dataset.k); return; }
  const lay = e.target.closest("#layout button[data-v]");
  if (lay) { S.layout = lay.dataset.v; changed("layout"); return; }
  if (e.target.closest("[data-copy]") && last) {
    navigator.clipboard?.writeText(asText(last)).then(() => toast("Comparison copied"), () => toast("Couldn't copy"));
    return;
  }
  const c = e.target.closest("[data-close]"); if (c) { c.closest("dialog").close(); return; }
});
// a click on the backdrop closes a dialog -- only when the press began there too: a press inside
// that ends elsewhere (a dropdown opening under the pointer) is attributed to the dialog itself
// (otherwise opening the leader dropdown in "More options" closes the dialog)
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
  if (S.song && !D.songs.some(s => s.music_id === S.song)) { toast(`No ${S.difficulty} chart for ${S.song}.`); S.song = ""; }
}
async function init() {
  wirePopovers();
  const [meta, coll] = await Promise.all([
    getJSON("/api/meta").catch(() => ({})),
    getJSON("/api/collection").catch(() => ({ cards: [] })),
    loadRanks(), loadGradeRanks(),
  ]);
  for (const c of coll.cards || []) D.cards.set(c.id, normCard(c));
  const diffs = (meta.difficulties && meta.difficulties.available) || ["EASY", "NORMAL", "HARD", "EXPERT"];
  if (!diffs.includes(S.difficulty)) S.difficulty = "EXPERT";
  byId("difficulty").innerHTML = diffs.map(d => `<button type="button" data-v="${d}" aria-pressed="false">${d[0] + d.slice(1).toLowerCase()}</button>`).join("");
  bindSeg("difficulty", "difficulty"); bindSeg("cards", "cards"); bindSeg("boards", "boards");
  wireMore(); wireCardPicker();
  byId("jacket").onclick = openSongPicker; byId("change").onclick = openSongPicker; byId("play").onclick = run;
  byId("addteam").onclick = () => { if (S.teams.length < MAX_TEAMS) { S.teams.push(emptyTeam()); save(); drawTeams(); markStale(); } };
  byId("fillbest").onclick = fillBest;
  await loadSongs();
  if (S.scope === "song" && !S.song && D.songs.length) S.song = D.songs[0].music_id;
  save(); applyOpts(); showSong(); drawTeams(); idle();
}
init();
