// compare.js — the "Compare" view: pick TWO teams by
// hand, set the assumptions ONCE, and read both teams scored side by side by
// POST /api/compare (tools/web/compare_view.py) with the differences explained.
//
// Nothing here scores anything: every number is the server's, computed by the
// same machinery the Solve page uses (same card basis, same board develop, same
// per-song scorer, same certificate guard). This module draws two pickers, one
// set of controls, and the answer. The two rosters + the assumptions live in
// localStorage so a reload does not lose an hour of clicking.
import { el } from "/dom.js";
import { cardTile } from "/ranking.js";
import { boardSection } from "/boardstate.js";
import { certPill, certFold } from "/certpill.js";
import { renderTimeline } from "/timeline.js";

const $ = (id) => document.getElementById(id);
const STORE_KEY = "hbuilder.compare";
const SLOT_LABELS = ["Leader", "Member 1", "Member 2", "Member 3", "Member 4", "Member 5"];
const LABELS = ["A", "B"];
// Cards = "maxed account" (comparable with the community tool holodori.best) forces the Board control:
const MA_REASON = "Cards = maxed account: the board is develop / full tree (453) by definition — a maxed account has every point.";

const num = (n, d = 0) => (typeof n === "number" && Number.isFinite(n)
  ? n.toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: d }) : "—");
const signed = (n, d = 0) => (typeof n === "number" && Number.isFinite(n)
  ? (n > 0 ? "+" : "") + num(n, d) : "—");
const pctText = (p) => (typeof p === "number" && Number.isFinite(p) ? signed(p, 2) + " %" : "—");

// --- state -------------------------------------------------------------------
function emptyTeam() { return { cards: [null, null, null, null, null, null], overrides: [null, null, null, null, null, null] }; }

const state = {
  teams: [emptyTeam(), emptyTeam()],
  assume: {
    cardsmode: "asis",          // asis | whatif | maxed | maxed-account
    assume_level: "account", assume_bloom: "account",
    board: "develop",           // captured | develop | off
    budgetkind: "real",         // real | full | custom
    budget: 453,
    songmode: "song",           // song | across | unit
    song: "",
    platform: "mobile", uptime_model: "finite",
  },
};

function save() {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch (_) { /* private mode */ }
}
function load() {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (!raw) return;
    const saved = JSON.parse(raw);
    if (Array.isArray(saved.teams) && saved.teams.length === 2) {
      state.teams = saved.teams.map((t) => ({
        cards: Array.from({ length: 6 }, (_, i) => (t.cards && t.cards[i]) || null),
        overrides: Array.from({ length: 6 }, (_, i) => (t.overrides && t.overrides[i]) || null),
      }));
    }
    if (saved.assume && typeof saved.assume === "object") Object.assign(state.assume, saved.assume);
  } catch (_) { /* a broken save is an empty page, never a crash */ }
}

// --- catalogue ------------------------------------------------------------------
let CATALOGUE = [];      // /api/collection cards (id, member, rarity, type, generations, costume_name, title, owned, level, bloom)
let BY_ID = new Map();
let SONGS = [];          // /api/songs songs (music_id, title, singer)

async function loadCatalogue() {
  try {
    const r = await fetch("/api/collection");
    if (!r.ok) return;
    const doc = await r.json();
    CATALOGUE = (doc.cards || []).map((c) => ({ ...c, owned: !!c.owned }));
    BY_ID = new Map(CATALOGUE.map((c) => [c.id, c]));
  } catch (_) { CATALOGUE = []; }
}
async function loadSongs() {
  try {
    const r = await fetch("/api/songs");
    if (!r.ok) return;
    const doc = await r.json();
    SONGS = (doc.songs || []).map((s) => ({
      id: String(s.music_id || ""), title: String(s.title || s.music_id || ""),
      singer: String(s.singer || ""),
    }));
  } catch (_) { SONGS = []; }
}

const cardLabel = (c) => (c ? (c.member || c.id) + (c.title ? ` — ${c.title}` : c.costume_name ? ` — ${c.costume_name}` : "") : "");
const rarityRank = (r) => { const n = parseInt(r, 10); return Number.isFinite(n) ? n : 0; };

// --- the card picker dialog ---------------------------------------------------------
// One <dialog id="xpick"> for all twelve slots; `openPicker(team, slot)` fills it.
let pickTarget = null;
const pick = { dlg: null, search: null, type: null, rarity: null, owned: null, list: null, count: null, title: null };

function buildPicker() {
  const dlg = $("xpick");
  if (!dlg) return;
  pick.dlg = dlg;
  const inner = el("div", "dlg-in xpick-in");
  const x = el("button", "dlg-x", "×");
  x.type = "button"; x.setAttribute("aria-label", "Close");
  x.addEventListener("click", () => dlg.close());
  const head = el("div", "xpick-head");
  pick.title = el("h3", null, "Pick a card");
  head.append(pick.title);
  const bar = el("div", "xpick-bar");
  pick.search = document.createElement("input");
  pick.search.type = "search"; pick.search.placeholder = "search name, outfit, id…";
  pick.search.className = "xpick-search";
  pick.type = document.createElement("select");
  for (const [v, t] of [["", "any type"], ["Cute", "Cute"], ["Cool", "Cool"], ["Pure", "Pure"], ["Elegant", "Elegant"], ["Sexy", "Sexy"]]) {
    const o = document.createElement("option"); o.value = v; o.textContent = t; pick.type.append(o);
  }
  pick.rarity = document.createElement("select");
  for (const [v, t] of [["", "any rarity"], ["5", "5★"], ["4", "4★"], ["3", "3★"]]) {
    const o = document.createElement("option"); o.value = v; o.textContent = t; pick.rarity.append(o);
  }
  const ownedLab = document.createElement("label");
  ownedLab.className = "xpick-owned";
  pick.owned = document.createElement("input");
  pick.owned.type = "checkbox"; pick.owned.checked = true;
  ownedLab.append(pick.owned, document.createTextNode(" owned only"));
  pick.count = el("span", "xpick-count", "");
  bar.append(pick.search, pick.type, pick.rarity, ownedLab, pick.count);
  pick.list = el("div", "xpick-list");
  inner.append(x, head, bar, pick.list);
  dlg.append(inner);
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  for (const ctl of [pick.search, pick.type, pick.rarity, pick.owned]) {
    ctl.addEventListener("input", renderPickList);
    ctl.addEventListener("change", renderPickList);
  }
  // the type list is whatever the catalogue actually holds
  refreshTypeOptions();
}

function refreshTypeOptions() {
  if (!pick.type) return;
  const types = [...new Set(CATALOGUE.map((c) => c.type).filter(Boolean))].sort();
  if (!types.length) return;
  pick.type.textContent = "";
  const any = document.createElement("option"); any.value = ""; any.textContent = "any type"; pick.type.append(any);
  for (const t of types) { const o = document.createElement("option"); o.value = t; o.textContent = t; pick.type.append(o); }
}

function renderPickList() {
  if (!pick.list) return;
  const q = (pick.search.value || "").trim().toLowerCase();
  const type = pick.type.value;
  const rar = pick.rarity.value;
  const ownedOnly = pick.owned.checked;
  const taken = pickTarget ? new Set(state.teams[pickTarget.team].cards.filter((id, i) => id && i !== pickTarget.slot)) : new Set();
  const hits = CATALOGUE.filter((c) => {
    if (ownedOnly && !c.owned) return false;
    if (type && c.type !== type) return false;
    if (rar && String(rarityRank(c.rarity)) !== rar) return false;
    if (q) {
      const hay = `${c.member || ""} ${c.title || ""} ${c.costume_name || ""} ${c.id} ${(c.generations || []).join(" ")}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  }).sort((a, b) => rarityRank(b.rarity) - rarityRank(a.rarity) || String(a.member).localeCompare(String(b.member)));
  pick.count.textContent = `${hits.length} card${hits.length === 1 ? "" : "s"}`;
  pick.list.textContent = "";
  for (const c of hits.slice(0, 120)) {
    const row = el("button", "xpick-row" + (c.owned ? "" : " unowned") + (taken.has(c.id) ? " taken" : ""));
    row.type = "button";
    const art = document.createElement("img");
    art.className = "xpick-art"; art.loading = "lazy"; art.alt = "";
    art.src = "/cardart/thumb/" + encodeURIComponent(c.id) + "?w=96";
    art.addEventListener("error", () => art.remove());
    const txt = el("div", "xpick-txt");
    txt.append(el("b", null, c.member || c.id));
    txt.append(el("span", "xpick-sub", [c.title || c.costume_name || "", `${rarityRank(c.rarity) || "?"}★`, c.type || "",
      c.owned ? (c.level != null ? `Lv ${c.level}` : "") : "unowned",
      c.owned && c.bloom != null ? `bloom ${c.bloom}` : ""].filter(Boolean).join(" · ")));
    row.append(art, txt);
    row.title = taken.has(c.id) ? "already in this team (the leader may also be a member)" : c.id;
    row.addEventListener("click", () => {
      if (!pickTarget) return;
      state.teams[pickTarget.team].cards[pickTarget.slot] = c.id;
      save();
      renderTeams();
      pick.dlg.close();
    });
    pick.list.append(row);
  }
  if (!hits.length) pick.list.append(el("p", "note", "No card matches — clear a filter or untick “owned only”."));
}

function openPicker(team, slot) {
  if (!pick.dlg) return;
  pickTarget = { team, slot };
  pick.title.textContent = `Team ${LABELS[team]} — ${SLOT_LABELS[slot]}`;
  pick.search.value = "";
  renderPickList();
  pick.dlg.showModal();
  pick.search.focus();
}

// --- the two team editors ------------------------------------------------------------
let teamsHost = null;

function slotRow(teamIdx, slotIdx) {
  const t = state.teams[teamIdx];
  const id = t.cards[slotIdx];
  const c = id ? BY_ID.get(id) : null;
  const row = el("div", "xslot" + (slotIdx === 0 ? " lead" : ""));
  row.append(el("span", "xslot-role", SLOT_LABELS[slotIdx]));
  const face = el("button", "xslot-card" + (id ? "" : " empty"));
  face.type = "button";
  if (id) {
    const art = document.createElement("img");
    art.className = "xslot-art"; art.alt = ""; art.loading = "lazy";
    art.src = "/cardart/thumb/" + encodeURIComponent(id) + "?w=96";
    art.addEventListener("error", () => art.remove());
    const txt = el("span", "xslot-txt");
    txt.append(el("b", null, c ? (c.member || id) : id));
    txt.append(el("small", null, c ? [c.title || c.costume_name || "", `${rarityRank(c.rarity) || "?"}★`, c.owned ? "" : "unowned"].filter(Boolean).join(" · ") : "not in the catalogue"));
    face.append(art, txt);
    if (c && !c.owned) face.classList.add("unowned");
  } else {
    face.append(el("span", "xslot-txt", "pick a card…"));
  }
  face.title = "click to pick a card";
  face.addEventListener("click", () => openPicker(teamIdx, slotIdx));
  row.append(face);

  // per-card override: level + bloom, blank = the basis' own value
  const ov = t.overrides[slotIdx] || {};
  const ovBox = el("span", "xslot-ov");
  const lvl = document.createElement("input");
  lvl.type = "number"; lvl.min = "1"; lvl.max = "80"; lvl.placeholder = "Lv";
  lvl.className = "xslot-lvl"; lvl.title = "override this card's level (blank = as the basis)";
  if (ov.level != null) lvl.value = String(ov.level);
  const bl = document.createElement("select");
  bl.className = "xslot-bloom"; bl.title = "override this card's bloom (blank = as the basis)";
  for (const [v, txt] of [["", "bloom"], ["0", "b0"], ["1", "b1"], ["2", "b2"], ["3", "b3"], ["4", "b4"], ["5", "b5"], ["6", "b6"]]) {
    const o = document.createElement("option"); o.value = v; o.textContent = txt; bl.append(o);
  }
  if (ov.bloom != null) bl.value = String(ov.bloom);
  const commit = () => {
    const level = lvl.value === "" ? null : Math.max(1, Math.min(80, parseInt(lvl.value, 10) || 1));
    const bloom = bl.value === "" ? null : parseInt(bl.value, 10);
    t.overrides[slotIdx] = level == null && bloom == null ? null : { level, bloom };
    row.classList.toggle("overridden", t.overrides[slotIdx] != null);
    save();
  };
  lvl.addEventListener("change", commit);
  bl.addEventListener("change", commit);
  ovBox.append(lvl, bl);
  row.append(ovBox);
  row.classList.toggle("overridden", t.overrides[slotIdx] != null);

  const clear = el("button", "xslot-x", "×");
  clear.type = "button"; clear.title = "clear this slot";
  clear.addEventListener("click", () => { t.cards[slotIdx] = null; t.overrides[slotIdx] = null; save(); renderTeams(); });
  row.append(clear);
  return row;
}

function renderTeams() {
  if (!teamsHost) return;
  teamsHost.textContent = "";
  state.teams.forEach((t, idx) => {
    const box = el("section", "xteam");
    const head = el("div", "xteam-head");
    head.append(el("h3", null, `Team ${LABELS[idx]}`));
    const tools = el("div", "xteam-tools");
    const copy = el("button", "ghost sm", idx === 0 ? "copy → B" : "copy → A");
    copy.type = "button"; copy.title = "copy this roster (and overrides) into the other team";
    copy.addEventListener("click", () => {
      const o = 1 - idx;
      state.teams[o] = { cards: [...t.cards], overrides: t.overrides.map((x) => (x ? { ...x } : null)) };
      save(); renderTeams();
    });
    const clr = el("button", "ghost sm", "clear");
    clr.type = "button";
    clr.addEventListener("click", () => { state.teams[idx] = emptyTeam(); save(); renderTeams(); });
    tools.append(copy, clr);
    if (idx === 0) {
      const swap = el("button", "ghost sm", "swap A ⇄ B");
      swap.type = "button";
      swap.addEventListener("click", () => { state.teams.reverse(); save(); renderTeams(); });
      tools.append(swap);
    }
    head.append(tools);
    box.append(head);
    for (let s = 0; s < 6; s++) box.append(slotRow(idx, s));
    box.append(el("p", "note xteam-note", "Level / bloom boxes override ONE card; blank keeps the basis chosen below. The leader may also be one of the five members."));
    teamsHost.append(box);
  });
}

// --- the assumptions ---------------------------------------------------------------
let assumeHost = null;
const ctl = {};

function option(select, value, text, selected) {
  const o = document.createElement("option");
  o.value = value; o.textContent = text; if (selected) o.selected = true;
  select.append(o);
}

function seg(name, values, current, onChange) {
  const wrap = el("div", "seg");
  for (const [v, t] of values) {
    const b = el("button", v === current ? "on" : "", t);
    b.type = "button"; b.dataset.val = v;
    b.addEventListener("click", () => {
      for (const x of wrap.querySelectorAll("button")) x.classList.toggle("on", x === b);
      onChange(v);
    });
    wrap.append(b);
  }
  return wrap;
}

function ctrl(labelText, ...children) {
  const box = el("div", "ctrl");
  box.append(el("span", "ctrl-lab", labelText));
  for (const c of children) box.append(c);
  return box;
}

function renderAssumptions() {
  if (!assumeHost) return;
  assumeHost.textContent = "";
  const a = state.assume;

  // cards basis
  ctl.cardsmode = document.createElement("select"); ctl.cardsmode.className = "planmode";
  option(ctl.cardsmode, "asis", "My cards, as they are", a.cardsmode === "asis");
  option(ctl.cardsmode, "whatif", "My cards, at a level/bloom I choose…", a.cardsmode === "whatif");
  option(ctl.cardsmode, "maxed", "Every card in the game, maxed", a.cardsmode === "maxed");
  option(ctl.cardsmode, "maxed-account", "Every card maxed + maxed account (all boards 453, memories 37, power-up 11.58 %)", a.cardsmode === "maxed-account");
  ctl.cardsmode.lastChild.title = "holodori-comparable: 54 boards fully developed at 453 with level-2 connect cards, memories 37, power-up 11.58 %";
  const assumeRow = el("div", "assume-row");
  ctl.assume_level = document.createElement("select");
  option(ctl.assume_level, "account", "real", a.assume_level === "account");
  for (const v of ["20", "30", "40", "50", "60", "70", "80"]) option(ctl.assume_level, v, v, a.assume_level === v);
  option(ctl.assume_level, "max", "Max", a.assume_level === "max");
  ctl.assume_bloom = document.createElement("select");
  option(ctl.assume_bloom, "account", "real", a.assume_bloom === "account");
  for (const v of ["0", "1", "2", "3", "4", "5", "6"]) option(ctl.assume_bloom, v, v, a.assume_bloom === v);
  option(ctl.assume_bloom, "max", "Max", a.assume_bloom === "max");
  const l1 = document.createElement("label"); l1.append(document.createTextNode("level "), ctl.assume_level);
  const l2 = document.createElement("label"); l2.append(document.createTextNode("bloom "), ctl.assume_bloom);
  assumeRow.append(l1, l2);
  const syncCards = () => {
    assumeRow.hidden = ctl.cardsmode.value !== "whatif";
    // maxed account => board forced to develop / full tree (a maxed account
    // has every point; "captured" and "real points" describe the user's own account)
    const forced = ctl.cardsmode.value === "maxed-account";
    if (ctl.board) {
      if (forced) { a.board = "develop"; ctl.board.value = "develop"; a.budgetkind = "full"; }
      ctl.board.disabled = forced;
      ctl.board.title = forced ? MA_REASON : "";
      if (typeof syncBoardUI === "function") syncBoardUI();
    }
  };
  ctl.cardsmode.addEventListener("change", () => { a.cardsmode = ctl.cardsmode.value; syncCards(); save(); });
  ctl.assume_level.addEventListener("change", () => { a.assume_level = ctl.assume_level.value; save(); });
  ctl.assume_bloom.addEventListener("change", () => { a.assume_bloom = ctl.assume_bloom.value; save(); });
  syncCards();
  assumeHost.append(ctrl("Cards", ctl.cardsmode, assumeRow,
    el("p", "ctrl-hint", "Applied to all twelve slots; a per-card level/bloom box on a slot overrides it for that card only.")));

  // board
  ctl.board = document.createElement("select"); ctl.board.className = "planmode";
  option(ctl.board, "develop", "Develop each team's best board", a.board === "develop");
  option(ctl.board, "captured", "Use my current board, as captured", a.board === "captured");
  option(ctl.board, "off", "No board at all", a.board === "off");
  const budgetRow = el("div", "dev-row xbudget");
  ctl.budgetkind = seg("budget", [["real", "my real points"], ["full", "full tree (453)"], ["custom", "custom"]], a.budgetkind, (v) => {
    a.budgetkind = v; ctl.budget.hidden = v !== "custom"; save();
  });
  ctl.budget = document.createElement("input");
  ctl.budget.type = "number"; ctl.budget.min = "0"; ctl.budget.max = "453"; ctl.budget.value = String(a.budget);
  ctl.budget.className = "dev-num"; ctl.budget.hidden = a.budgetkind !== "custom";
  ctl.budget.addEventListener("change", () => { a.budget = Math.max(0, Math.min(453, parseInt(ctl.budget.value, 10) || 0)); ctl.budget.value = String(a.budget); save(); });
  budgetRow.append(ctl.budgetkind, ctl.budget);
  const syncBoard = () => { budgetRow.hidden = ctl.board.value !== "develop"; };
  var syncBoardUI = () => {
    syncBoard();
    const forced = ctl.cardsmode && ctl.cardsmode.value === "maxed-account";
    for (const b of ctl.budgetkind.querySelectorAll("button")) {
      b.disabled = forced;
      b.classList.toggle("on", forced ? b.dataset.val === "full" : b.dataset.val === a.budgetkind);
    }
    ctl.budget.hidden = forced || a.budgetkind !== "custom";
    budgetRow.title = forced ? MA_REASON : "";
  };
  ctl.board.addEventListener("change", () => { a.board = ctl.board.value; syncBoard(); save(); });
  syncBoardUI();
  syncCards();
  assumeHost.append(ctrl("Board", ctl.board, budgetRow,
    el("p", "ctrl-hint", "Develop = each team gets its own Engine-A board (real sockets, bench GREENs). Captured = board.json as it is. Off = bare.")));

  // song
  ctl.songmode = document.createElement("select"); ctl.songmode.className = "planmode";
  option(ctl.songmode, "song", "One chart (per-song score)", a.songmode === "song");
  option(ctl.songmode, "across", "Across all charts (mean + wins)", a.songmode === "across");
  option(ctl.songmode, "unit", "Song-agnostic Unit Score", a.songmode === "unit");
  const songRow = el("div", "xsong-row");
  ctl.songsearch = document.createElement("input");
  ctl.songsearch.type = "search"; ctl.songsearch.placeholder = "filter songs…"; ctl.songsearch.className = "xsong-search";
  ctl.song = document.createElement("select"); ctl.song.className = "xsong-sel";
  const fillSongs = () => {
    const q = (ctl.songsearch.value || "").trim().toLowerCase();
    ctl.song.textContent = "";
    option(ctl.song, "", SONGS.length ? "— choose a song —" : "no chart library on disk", !a.song);
    for (const s of SONGS) {
      if (q && !`${s.title} ${s.id} ${s.singer}`.toLowerCase().includes(q)) continue;
      option(ctl.song, s.id, `${s.title}${s.singer ? " · " + s.singer : ""} (${s.id})`, s.id === a.song);
    }
    if (a.song && ctl.song.value !== a.song) {
      // the chosen song is filtered out: keep it selectable so the choice is not silently lost
      const s = SONGS.find((x) => x.id === a.song);
      option(ctl.song, a.song, s ? `${s.title} (${s.id})` : a.song, true);
    }
  };
  fillSongs();
  ctl.songsearch.addEventListener("input", fillSongs);
  ctl.song.addEventListener("change", () => { a.song = ctl.song.value; save(); });
  songRow.append(ctl.songsearch, ctl.song);
  const syncSong = () => { songRow.hidden = ctl.songmode.value !== "song"; };
  ctl.songmode.addEventListener("change", () => { a.songmode = ctl.songmode.value; syncSong(); save(); });
  syncSong();
  assumeHost.append(ctrl("Song", ctl.songmode, songRow));

  // platform + score model
  assumeHost.append(ctrl("Platform", seg("platform", [["mobile", "Mobile"], ["pc", "PC"]], a.platform, (v) => { a.platform = v; save(); }),
    el("p", "ctrl-hint", "Flick notes weigh 1050 on mobile and 1000 on PC (Steam).")));
  assumeHost.append(ctrl("Score model", seg("uptime", [["finite", "In-run (real)"], ["steady", "Deck screen"]], a.uptime_model, (v) => { a.uptime_model = v; save(); }),
    el("p", "ctrl-hint", "In-run = what a live actually pays out (cooldown windows). Deck screen reproduces the in-game panel number.")));
}

// --- the request ---------------------------------------------------------------------
function requestBody() {
  const a = state.assume;
  const body = {
    teams: state.teams.map((t) => ({ cards: t.cards.map((c) => c || ""), overrides: t.overrides })),
    basis: a.cardsmode === "maxed" ? "maxed" : a.cardsmode === "maxed-account" ? "maxed-account" : "account",
    board: a.cardsmode === "maxed-account" ? "develop" : a.board,
    platform: a.platform,
    uptime_model: a.uptime_model,
  };
  if (a.cardsmode === "whatif") {
    body.assume_level = a.assume_level;
    body.assume_bloom = a.assume_bloom;
  }
  if (a.cardsmode === "maxed-account") {
    body.budget = 453;      // forced: the full tree on a maxed account
  } else if (a.board === "develop") {
    body.budget = a.budgetkind === "real" ? null : a.budgetkind === "full" ? 453 : a.budget;
  }
  if (a.songmode === "song") body.song = a.song;
  else if (a.songmode === "across") body.across = true;
  return body;
}

function validateLocally() {
  for (let i = 0; i < 2; i++) {
    const missing = state.teams[i].cards.filter((c) => !c).length;
    if (missing) return `Team ${LABELS[i]} still has ${missing} empty slot${missing === 1 ? "" : "s"}.`;
  }
  if (state.assume.songmode === "song" && !state.assume.song) return "Choose a song, or switch the Song control to across / Unit Score.";
  return null;
}

// --- results ----------------------------------------------------------------------------
let resultHost = null;
let lastDoc = null;

function table(headers, rows, cls) {
  const t = el("table", "grid " + (cls || ""));
  const thead = el("thead"); const htr = el("tr");
  for (const h of headers) htr.append(el("th", null, h));
  thead.append(htr);
  const tbody = el("tbody");
  for (const cells of rows) {
    const tr = el("tr");
    for (const [c, v] of cells) {
      const td = el("td", c);
      if (v instanceof Node) td.append(v); else td.textContent = v;
      tr.append(td);
    }
    tbody.append(tr);
  }
  t.append(thead, tbody);
  const wrap = el("div", "gridwrap");
  wrap.append(t);
  return wrap;
}

const modeLabel = (doc) => (doc.mode === "song" ? `per-song score on ${doc.song}`
  : doc.mode === "across" ? `mean per-song score across ${doc.chart_count} charts` : "Unit Score (deck screen)");

// The headline IS the score under the chosen board option; the sub-line names
// that option and then gives two REFERENCE points (the same team with no board
// at all, and the ceiling if every ACTIVE fired) so "board off" can never be
// misread as the headline's basis.
function boardWord(doc) {
  const a = doc.assumptions || {};
  if (a.board === "develop") return `with developed board (${a.budget === "real" || a.budget == null ? "real points" : a.budget + " pt flat"})`;
  if (a.board === "captured") return "with your captured board";
  return "with NO board";
}
// Under the plan-cache objective (doc.objective.leaf === "plan_cache") the
// headline is the team on the FIXED plan (the v2 objective, the number the
// certificates are about) and the per-team greedy re-develop is named as the
// what-if beside it — the comparator says which number is which.
function whatifWord(team, doc) {
  const wi = team.develop_whatif;
  if (!wi || !doc.objective || doc.objective.leaf !== "plan_cache") return "";
  const v = typeof wi.song_est === "number" ? wi.song_est : typeof wi.across_mean === "number" ? wi.across_mean : null;
  if (v === null) return "";
  const pct = typeof wi.delta_pct === "number" ? wi.delta_pct : 0;
  return ` · objective = fixed plan (plan-cache leaf); greedy re-develop what-if ${num(v)} (${pct >= 0 ? "+" : ""}${pct.toFixed(2)} %)`;
}
function headlineOf(team, doc) {
  const s = team.score;
  const bw = boardWord(doc);
  const ww = whatifWord(team, doc);
  if (doc.mode === "song") return { main: s.song_est, sub: `${bw} · reference: no board ${num(s.song_off)} · ceiling if every ACTIVE fires ${num(s.song_max)}${ww}` };
  if (doc.mode === "across") return { main: s.across_mean, sub: `${bw} · wins ${s.wins} of ${doc.chart_count} charts (ties ${s.ties}) · Unit Score ${num(s.unit_score)}${ww}` };
  return { main: s.unit_score, sub: `${bw} · reference: no board ${num(s.unit_score_board_off)}` };
}

const nameOf = (doc, id) => (doc.cards && doc.cards[id] ? doc.cards[id].member : id);

function deltaCell(d, digits, unit) {
  if (!d || d.delta == null) return el("span", "k", "—");
  const span = el("span", d.delta > 0 ? "x-up" : d.delta < 0 ? "x-down" : "k");
  span.textContent = signed(d.delta, digits) + (unit || "") + (d.pct != null && unit !== " pp" ? ` (${pctText(d.pct)})` : "");
  return span;
}

function gatePlain(g) {
  const noun = g.dimension === "generation" ? `${g.value} members`
    : g.dimension === "type" ? `${g.value} members` : `${g.value} (${g.dimension})`;
  return `needs ${g.threshold}+ ${noun}`;
}

function verdict(v) {
  if (!v) return el("span", "k", "— not carried");
  const s = el("span", v.fired ? "fired" : "unfired", (v.fired ? "met" : "NOT MET") + ` (${v.actual_count})`);
  return s;
}

function rosterBlock(team, doc) {
  const roster = el("div", "roster");
  const leadBox = el("div", "rgroup lead");
  leadBox.append(el("h4", null, "Leader"));
  const lg = el("div", "team");
  lg.append(cardTile(doc.cards[team.leader_id], { lead: true }, false));
  leadBox.append(lg);
  const memBox = el("div", "rgroup mem");
  const ordered = doc.mode === "song" && Array.isArray(team.score.member_order) ? team.score.member_order : null;
  memBox.append(el("h4", null, ordered ? "Members — in slot order" : "Members"));
  const mg = el("div", "team");
  (ordered || team.member_ids).forEach((id, i) => {
    const tile = cardTile(doc.cards[id], null, false);
    if (ordered) tile.prepend(el("div", "slotno", `slot ${i + 1}`));
    const slot = team.slots.find((s) => s.card_id === id && s.override);
    if (slot) tile.append(el("div", "x-ovtag", `override: Lv ${slot.level ?? "—"} · bloom ${slot.bloom ?? "—"}`));
    mg.append(tile);
  });
  memBox.append(mg);
  roster.append(leadBox, memBox);
  return roster;
}

function renderResult(doc) {
  lastDoc = doc;
  resultHost.textContent = "";
  const [A, B] = doc.teams;
  const hA = headlineOf(A, doc), hB = headlineOf(B, doc);

  // headline
  const head = el("section", "x-head");
  head.append(el("p", "lede sm", `Both teams scored under the SAME assumptions — ${modeLabel(doc)} · cards ${doc.assumptions.basis}`
    + (doc.assumptions.scenario ? ` (${doc.assumptions.scenario.label || "maxed account"})` : "")
    + (doc.assumptions.assume_level != null ? ` @ level ${doc.assumptions.assume_level}` : "")
    + (doc.assumptions.assume_bloom != null ? ` bloom ${doc.assumptions.assume_bloom}` : "")
    + ` · board ${doc.assumptions.board}${doc.assumptions.board === "develop" ? ` (${doc.assumptions.budget === "real" ? "real points" : doc.assumptions.budget + " pt flat"})` : ""}`
    + ` · ${doc.assumptions.platform} · ${doc.assumptions.uptime_model}`));
  const grid = el("div", "x-headgrid");
  for (const [team, h] of [[A, hA], [B, hB]]) {
    const card = el("div", "x-headcard");
    const top = el("div", "x-headtop");
    top.append(el("span", "x-headlab", `Team ${team.label}`));
    const pill = certPill(team.certificate);
    if (pill) top.append(pill);
    card.append(top);
    card.append(el("div", "x-big mono", num(h.main)));
    card.append(el("div", "x-sub", h.sub));
    card.append(el("div", "x-sub", `${team.leader_member} leads · ${team.members.join(", ")}`));
    grid.append(card);
  }
  const d = doc.diff.score;
  const dcard = el("div", "x-headcard x-delta");
  dcard.append(el("span", "x-headlab", "B − A"));
  const big = el("div", "x-big mono " + (d.delta > 0 ? "x-up" : d.delta < 0 ? "x-down" : ""));
  big.textContent = signed(d.delta) + (d.pct != null ? ` (${pctText(d.pct)})` : "");
  dcard.append(big);
  dcard.append(el("div", "x-sub x-driver", "Biggest driver: " + doc.diff.sentence));
  if (doc.diff.drivers && doc.diff.drivers.length > 1) {
    const labels = Object.fromEntries((doc.panel_lines || []).map((l) => [l.key, l.label]));
    dcard.append(el("div", "x-sub", "then: " + doc.diff.drivers.slice(1, 4).map((x) => `${labels[x.line] || x.line} (${x.weight > 0 ? "B" : "A"})`).join(", ")));
  }
  grid.append(dcard);
  head.append(grid);
  const tools = el("div", "x-tools");
  const copy = el("button", "ghost sm", "Copy as text");
  copy.type = "button";
  copy.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(asText(doc)); copy.textContent = "Copied ✓"; }
    catch (_) { copy.textContent = "Copy failed"; }
    setTimeout(() => { copy.textContent = "Copy as text"; }, 2000);
  });
  tools.append(copy);
  if (doc.inputs_hash) tools.append(el("span", "note mono", `${doc.model_version} · inputs ${String(doc.inputs_hash).slice(0, 12)}…`));
  head.append(tools);
  resultHost.append(head);

  // rosters
  for (const team of [A, B]) {
    const sec = el("section", "x-roster");
    sec.append(el("h3", null, `Team ${team.label}`));
    sec.append(rosterBlock(team, doc));
    resultHost.append(sec);
  }

  // panel lines
  const panel = el("section", "x-panel");
  panel.append(el("h3", null, "Deck-screen lines"));
  panel.append(el("p", "note", "Member Parameter = the five members' summed stats · Passive = every PASSIVE stat_up · Outfit = the leader's costume stat effect · Board = the board's Member-Parameter half. Our score bonus is ONE pooled rate (E[max] over the ACTIVE bucket × (1 + B)); the game's four lines are not separable from it."));
  const rows = [];
  for (const line of doc.panel_lines || []) {
    const dd = doc.diff.panel[line.key];
    const digits = line.kind === "power" ? 0 : line.kind === "permil" ? 1 : 2;
    const unit = line.kind === "rate" ? " pp" : line.kind === "permil" ? " ‰" : "";
    rows.push([
      ["k", line.label],
      ["v mono", num(A.panel[line.key], digits) + (line.kind === "rate" ? " %" : line.kind === "permil" ? " ‰" : "")],
      ["v mono", num(B.panel[line.key], digits) + (line.kind === "rate" ? " %" : line.kind === "permil" ? " ‰" : "")],
      ["v mono", deltaCell(dd, digits, unit)],
    ]);
  }
  if (doc.mode === "song") {
    rows.push([["k", "Per-song score, reference with NO board"], ["v mono", num(A.score.song_off)], ["v mono", num(B.score.song_off)], ["v mono", deltaCell(doc.diff.song_off, 0)]]);
    rows.push([["k", "Per-song score (max, every ACTIVE fires)"], ["v mono", num(A.score.song_max)], ["v mono", num(B.score.song_max)], ["v mono", deltaCell(doc.diff.song_max, 0)]]);
  }
  if (A.points != null || B.points != null) {
    rows.push([["k", "Board points spent"], ["v mono", num(A.points)], ["v mono", num(B.points)], ["v mono", deltaCell(doc.diff.board_points, 0)]]);
  }
  panel.append(table(["line", "A", "B", "Δ (B − A)"], rows, "x-grid4"));
  resultHost.append(panel);

  // gates
  const gates = el("section", "x-gates");
  gates.append(el("h3", null, "Conditions (gates)"));
  if (!doc.diff.gates.length) gates.append(el("p", "note", "Neither team carries a gated skill."));
  else gates.append(table(["card", "slot", "what the skill needs", "A", "B"],
    doc.diff.gates.map((g) => [
      ["k", nameOf(doc, g.card_id)], ["k", g.slot_kind], ["", gatePlain(g)],
      ["v", verdict(g.a)], ["v", verdict(g.b)],
    ]), "x-grid5"));
  resultHost.append(gates);

  // per-skill contributions
  const skills = el("section", "x-skills");
  skills.append(el("h3", null, "Per-skill contribution (weighted rate)"));
  skills.append(el("p", "note", "Each row is that skill's own magnitude × occupancy × weight — what it would contribute alone, before the pooled max. Rows do not sum to the pool."));
  const fmtRate = (v) => (v == null ? "—" : `${Number(v.weighted_rate).toFixed(4)}${v.state !== "applied" ? " · " + v.state : ""}`);
  const srows = doc.diff.skills
    .sort((x, y) => Math.abs(((y.b || {}).weighted_rate || 0) - ((y.a || {}).weighted_rate || 0)) - Math.abs(((x.b || {}).weighted_rate || 0) - ((x.a || {}).weighted_rate || 0)))
    .map((r) => {
      const ra = r.a ? r.a.weighted_rate || 0 : null, rb = r.b ? r.b.weighted_rate || 0 : null;
      const delta = ra == null || rb == null ? null : rb - ra;
      const dspan = el("span", delta > 0 ? "x-up" : delta < 0 ? "x-down" : "k");
      dspan.textContent = delta == null ? (ra == null ? "only in B" : "only in A") : signed(delta, 4);
      return [["k", nameOf(doc, r.card_id)], ["k", r.slot_kind], ["", `${r.family} / ${r.effect_type}`],
        ["v mono", fmtRate(r.a)], ["v mono", fmtRate(r.b)], ["v mono", dspan]];
    });
  skills.append(table(["card", "slot", "skill", "A", "B", "Δ"], srows, "x-grid6"));
  resultHost.append(skills);

  // developed boards
  if (doc.assumptions.board === "develop") {
    const ctx = { cards: doc.cards, basis: doc.assumptions.basis, budget: doc.assumptions.budget, song: doc.song };
    for (const team of [A, B]) {
      const sec = el("section", "x-board");
      const pc = doc.objective && doc.objective.leaf === "plan_cache";
      sec.append(el("h3", null, `Team ${team.label} — ${pc ? "fixed-plan board (objective)" : "developed board"}`));
      sec.append(boardSection(team, ctx));
      resultHost.append(sec);
    }
  }

  // timelines (song mode)
  if (doc.mode === "song") {
    for (const team of [A, B]) {
      const sec = el("section", "x-tl");
      sec.append(el("h3", null, `Team ${team.label} — when each member is live`));
      const host = el("div");
      sec.append(host);
      resultHost.append(sec);
      fetchTimeline(team, doc).then((data) => renderTimeline(host, data, doc.cards))
        .catch((e) => { host.textContent = `timeline failed: ${e.message || e}`; });
    }
  }

  // certificates
  for (const team of [A, B]) {
    const fold = certFold(team.certificate);
    if (fold) { fold.querySelector("summary").textContent = `Team ${team.label} — ` + fold.querySelector("summary").textContent; resultHost.append(fold); }
  }
}

async function fetchTimeline(team, doc) {
  const a = doc.assumptions;
  const body = {
    song: doc.song, cards: [team.leader_id, ...team.member_ids],
    basis: a.basis, platform: a.platform, uptime_model: a.uptime_model,
    board: a.board !== "off",
  };
  if (a.assume_level != null) body.assume_level = a.assume_level;
  if (a.assume_bloom != null) body.assume_bloom = a.assume_bloom;
  if (a.board === "develop" && team.board_state && Object.keys(team.board_state).length) body.board_state = team.board_state;
  const r = await fetch("/api/timeline", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return r.json();
}

function asText(doc) {
  const [A, B] = doc.teams;
  const lines = [];
  lines.push(`hbuilder compare — ${modeLabel(doc)}`);
  const a = doc.assumptions;
  lines.push(`assumptions: cards ${a.basis}${a.assume_level != null ? ` level ${a.assume_level}` : ""}${a.assume_bloom != null ? ` bloom ${a.assume_bloom}` : ""} · board ${a.board}${a.board === "develop" ? ` (${a.budget})` : ""} · ${a.platform} · ${a.uptime_model} · ${doc.model_version} inputs ${doc.inputs_hash || "n/a"}`);
  for (const t of [A, B]) {
    const h = headlineOf(t, doc);
    lines.push(`Team ${t.label}: ${num(h.main)}  [${t.certificate && t.certificate.status}]  ${h.sub}`);
    lines.push(`  leader ${t.leader_id} (${t.leader_member}); members ${t.member_ids.join(", ")}`);
    for (const s of t.slots) if (s.override) lines.push(`  override ${s.card_id}: level ${s.level} bloom ${s.bloom}`);
  }
  const d = doc.diff.score;
  lines.push(`B - A: ${signed(d.delta)} (${pctText(d.pct)}) — ${doc.diff.sentence}`);
  lines.push("panel lines (A | B | delta):");
  for (const line of doc.panel_lines || []) {
    const dd = doc.diff.panel[line.key];
    lines.push(`  ${line.label}: ${num(A.panel[line.key], 2)} | ${num(B.panel[line.key], 2)} | ${signed(dd && dd.delta, 2)}`);
  }
  lines.push("gates:");
  for (const g of doc.diff.gates) {
    const v = (x) => (x ? (x.fired ? "met" : "NOT MET") + `(${x.actual_count})` : "—");
    lines.push(`  ${nameOf(doc, g.card_id)} ${g.slot_kind} ${gatePlain(g)}: A ${v(g.a)} | B ${v(g.b)}`);
  }
  return lines.join("\n");
}

// --- mount -------------------------------------------------------------------------------
async function runCompare(status) {
  const why = validateLocally();
  if (why) { status.textContent = why; status.className = "note x-status warn"; return; }
  status.textContent = "Comparing… (a develop takes a few seconds)";
  status.className = "note x-status";
  try {
    const r = await fetch("/api/compare", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(requestBody()) });
    const raw = await r.text();
    let doc;
    try { doc = JSON.parse(raw); } catch (_) { throw new Error(`HTTP ${r.status}: ${raw.slice(0, 200)}`); }
    if (!r.ok) throw new Error(doc.error || `HTTP ${r.status}`);
    status.textContent = "";
    renderResult(doc);
  } catch (e) {
    status.textContent = `Compare failed: ${e.message || e}`;
    status.className = "note x-status warn";
  }
}

export function mountCompare() {
  const wrap = $("xwrap");
  const body = $("xbody");
  if (!wrap || !body) return;
  load();
  let ever = false;
  wrap.addEventListener("toggle", async function () {
    if (!this.open || ever) return;
    ever = true;
    body.textContent = "";
    const head = el("header", "page-head");
    head.append(el("h1", null, "Compare teams"));
    head.append(el("p", null, "Two rosters, one set of assumptions, both scored by the same solver — and the difference explained line by line."));
    body.append(head);
    await Promise.all([loadCatalogue(), loadSongs()]);
    buildPicker();
    teamsHost = el("div", "x-teams");
    body.append(teamsHost);
    renderTeams();
    assumeHost = el("div", "x-assume controls");
    body.append(assumeHost);
    renderAssumptions();
    const go = el("div", "go-row x-go");
    const btn = el("button", "primary", "Compare");
    btn.type = "button";
    const status = el("p", "note x-status", "");
    go.append(btn, status);
    body.append(go);
    resultHost = el("div", "x-result");
    body.append(resultHost);
    btn.addEventListener("click", () => runCompare(status));
  });
}
