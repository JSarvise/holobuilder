/* The song picker the Optimizer and Compare share. One search box that predicts as you
   type -- songs, talents and groups -- plus song-list levers like those of the community
   tool holodori.best (sort by release / title / length / difficulty with a direction
   toggle, Original / Cover). Picking a talent or a group turns it into a
   chip that filters the grid (a song matches when its cast holds that talent, or any
   member of that group, per hb-gens.js); picking a song suggestion picks the song.

   openSongPicker(dialog, {songs, current, specials, sub, sorts, onPick})
     songs    /api/songs rows (title, singer, characters, release, seconds, difficulty_level, category)
     current  the music_id (or special key) shown as current
     specials [{key, title, sub, icon, words, art}] tiles shown before the songs (In general, Unit
              Score, the event's Highlight); `art` (trusted HTML) replaces the icon square
     sub      (song) -> HTML line under the title (default: singer · level)
     sorts    extra sorts [{key, label, cmp}] offered first (e.g. the event's length bonus)
     onPick   (music_id | special key) -> void; the dialog closes itself first */

const QUOTE = String.fromCharCode(34), APOS = String.fromCharCode(39);
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", [QUOTE]: "&quot;", [APOS]: "&#39;" };
const ESC_RE = new RegExp("[&<>" + QUOTE + APOS + "]", "g");
const esc = s => String(s ?? "").replace(ESC_RE, c => ESC[c]);
const KEY = "hb.songpick";
const SORTS = [
  { key: "release", label: "Release", cmp: (a, b) => (a.release || 0) - (b.release || 0) || a.music_id.localeCompare(b.music_id), desc: true },
  { key: "title", label: "Title", cmp: (a, b) => (a.title || a.music_id).localeCompare(b.title || b.music_id, "en", { sensitivity: "base" }) },
  { key: "length", label: "Length", cmp: (a, b) => (a.seconds || 0) - (b.seconds || 0) },
  { key: "level", label: "Level", cmp: (a, b) => (a.difficulty_level || 0) - (b.difficulty_level || 0), desc: true },
];

function load() { try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch { return {}; } }
function save(v) { try { localStorage.setItem(KEY, JSON.stringify(v)); } catch { /* private window */ } }

function groupsOf(chr) { return ((window.HB_GENS || {}).by || {})[chr] || []; }

function mark(text, q) {
  const s = String(text ?? "");
  const at = q ? s.toLowerCase().indexOf(q) : -1;
  return at < 0 ? esc(s) : esc(s.slice(0, at)) + "<mark>" + esc(s.slice(at, at + q.length)) + "</mark>" + esc(s.slice(at + q.length));
}
// 0 starts with, 1 a word starts with, 2 anywhere, -1 no
function rank(text, q) {
  const l = String(text ?? "").toLowerCase();
  if (l.startsWith(q)) return 0;
  const at = l.indexOf(q);
  return at < 0 ? -1 : /[\s\-(/·:,!?.~]/.test(l[at - 1] || "") ? 1 : 2;
}
// the game's own day (JST): the launch songs read 2023-01-01, as the game dates them
const fmtDate = ms => ms ? new Date(ms + 9 * 3600e3).toISOString().slice(0, 10) : "";
const mmss = s => s ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : "";

export function openSongPicker(dialog, opt) {
  const songs = opt.songs || [];
  const specials = opt.specials || [];
  const sorts = [...(opt.sorts || []), ...SORTS];
  const pref = load();
  // a page's own ranking (the event's length bonus) opens first, best first; otherwise
  // the last sort used, newest first the first time
  const own = (opt.sorts || []).length > 0;
  const st = {
    q: "", chips: [], cat: pref.cat || "",
    sort: own ? sorts[0].key : (SORTS.some(s => s.key === pref.sort) ? pref.sort : "release"),
    desc: own ? false : (pref.desc ?? true),
  };

  // the talents and groups the songs' casts hold, for the suggestions
  const talents = new Map(), groups = new Map();
  for (const s of songs) for (const c of s.characters || []) {
    if (!talents.has(c.id)) talents.set(c.id, { kind: "talent", id: c.id, label: c.name, n: 0 });
    talents.get(c.id).n++;
    for (const g of groupsOf(c.id)) {
      if (!groups.has(g.n)) groups.set(g.n, { kind: "group", id: g.n, label: g.n, colour: g.c, songs: new Set() });
      groups.get(g.n).songs.add(s.music_id);
    }
  }
  for (const g of groups.values()) g.n = g.songs.size;

  dialog.classList.add("sp-dialog");
  dialog.innerHTML = `
    <div class="sp-head">
      <svg class="i sp-glass" aria-hidden="true"><use href="#i-search"/></svg>
      <div class="sp-field">
        <div class="sp-chips"></div>
        <input class="sp-q" type="search" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="sp-sug"
          placeholder="Search a song, a talent or a group" aria-label="Search songs, talents or groups" autocomplete="off" spellcheck="false">
      </div>
      <button class="sp-x" type="button" aria-label="Close"><svg class="i"><use href="#i-x"/></svg></button>
      <ul class="sp-sug" id="sp-sug" role="listbox" hidden></ul>
    </div>
    <div class="sp-bar">
      <div class="seg sp-sort" role="group" aria-label="Sort by">${sorts.map(s => `<button type="button" data-sp-sort="${s.key}">${esc(s.label)}</button>`).join("")}</div>
      <button class="sp-dir" type="button"></button>
      <div class="seg sp-cat" role="group" aria-label="Category">
        <button type="button" data-sp-cat="">All</button><button type="button" data-sp-cat="original">Original</button><button type="button" data-sp-cat="cover">Cover</button>
      </div>
      <span class="sp-count"></span>
    </div>
    <div class="sp-grid"></div>`;
  const $ = s => dialog.querySelector(s);
  const q = $(".sp-q"), sug = $(".sp-sug"), grid = $(".sp-grid");
  let shown = [], active = -1;

  const textOf = s => `${s.title} ${s.singer} ${(s.characters || []).map(c => c.name).join(" ")}`.toLowerCase();
  function filtered() {
    const t = st.q.trim().toLowerCase();
    let list = songs.filter(s => (!st.cat || s.category === st.cat)
      && st.chips.every(c => c.kind === "talent" ? (s.character_ids || []).includes(c.id) : groups.get(c.id)?.songs.has(s.music_id))
      && (!t || textOf(s).includes(t)));
    const so = sorts.find(s => s.key === st.sort) || sorts[0];
    list = [...list].sort(so.cmp);
    if (st.desc) list.reverse();
    return list;
  }

  function drawBar() {
    dialog.querySelectorAll(".sp-sort button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.spSort === st.sort)));
    dialog.querySelectorAll(".sp-cat button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.spCat === st.cat)));
    const so = sorts.find(s => s.key === st.sort) || sorts[0];
    const dir = $(".sp-dir");
    const words = { release: ["Oldest first", "Newest first"], title: ["A → Z", "Z → A"], length: ["Shortest first", "Longest first"], level: ["Easiest first", "Hardest first"] }[so.key]
      || ["Best first", "Worst first"];
    dir.textContent = words[st.desc ? 1 : 0];
    dir.title = "Reverse the order";
    $(".sp-chips").innerHTML = st.chips.map((c, k) => `<span class="sp-chip ${c.kind}"${c.colour ? ` style="--gc:${esc(c.colour)}"` : ""}>
      <small>${c.kind === "talent" ? "Talent" : "Group"}</small>${esc(c.label)}<button type="button" data-sp-unchip="${k}" aria-label="Remove ${esc(c.label)}">×</button></span>`).join("");
  }

  function sub(s) {
    if (opt.sub) return opt.sub(s);
    return `<span>${esc(s.singer)}${s.difficulty_level != null ? ` · Lv ${s.difficulty_level}` : ""}</span>`;
  }
  function meta(s) {
    if (st.sort === "release" && s.release) return `<em class="sp-meta">${fmtDate(s.release)}</em>`;
    if (st.sort === "length" && s.seconds) return `<em class="sp-meta">${mmss(s.seconds)}</em>`;
    return "";
  }
  function drawGrid() {
    const t = st.q.trim().toLowerCase();
    const list = filtered();
    const sp = st.chips.length || st.cat ? [] : specials.filter(x => !t || `${x.title} ${x.words || ""}`.toLowerCase().includes(t));
    grid.innerHTML = (sp.map(x => `<button class="sp-tile" type="button" data-sp-pick="${esc(x.key)}" ${x.key === opt.current ? 'aria-current="true"' : ""}>
        ${x.art ? `<span class="sp-art">${x.art}</span>` : `<span class="sp-all"><svg class="i"><use href="#${esc(x.icon || "i-globe")}"/></svg></span>`}<b>${esc(x.title)}</b><span>${esc(x.sub || "")}</span></button>`).join("")
      + list.map(s => `<button class="sp-tile" type="button" data-sp-pick="${esc(s.music_id)}" ${s.music_id === opt.current ? 'aria-current="true"' : ""}>
        <span class="sp-art"><img src="/jacket/${encodeURIComponent(s.music_id)}" alt="" loading="lazy">${s.category === "cover" ? '<i class="sp-cov">Cover</i>' : ""}${meta(s)}</span>
        <b>${mark(s.title || s.music_id, t)}</b>${sub(s)}</button>`).join(""))
      || `<p class="sp-none">No song matches${st.q ? ` “${esc(st.q)}”` : ""}${st.chips.length ? ` with ${st.chips.map(c => esc(c.label)).join(" and ")}` : ""}${st.cat ? ` (${st.cat} only)` : ""}.</p>`;
    $(".sp-count").textContent = `${list.length} of ${songs.length} songs`;
  }

  // the predictions: talents and groups first (they become filters), then songs
  function drawSug() {
    const t = st.q.trim().toLowerCase();
    if (!t) { sug.hidden = true; q.setAttribute("aria-expanded", "false"); shown = []; active = -1; return; }
    const pickBest = (items, text, n) => items.map(x => ({ x, r: rank(text(x), t) })).filter(o => o.r >= 0)
      .sort((a, b) => a.r - b.r || (b.x.n || 0) - (a.x.n || 0)).slice(0, n).map(o => o.x);
    const taken = new Set(st.chips.map(c => c.kind + c.id));
    const tal = pickBest([...talents.values()].filter(x => !taken.has("talent" + x.id)), x => x.label, 4);
    const grp = pickBest([...groups.values()].filter(x => !taken.has("group" + x.id)), x => x.label, 3);
    const sng = pickBest(songs.map(s => ({ kind: "song", id: s.music_id, label: s.title || s.music_id, singer: s.singer })), x => x.label, 6);
    shown = [...tal, ...grp, ...sng];
    if (active >= shown.length) active = shown.length - 1;
    sug.innerHTML = shown.map((x, k) => {
      const tag = x.kind === "talent" ? `<small>Talent · ${x.n} song${x.n === 1 ? "" : "s"}</small>`
        : x.kind === "group" ? `<small>Group · ${x.n} song${x.n === 1 ? "" : "s"}</small>`
        : `<small>${esc(x.singer || "Song")}</small>`;
      const lead = x.kind === "song" ? `<img src="/jacket/${encodeURIComponent(x.id)}" alt="" loading="lazy">`
        : x.kind === "group" ? `<i class="sp-dot" style="--gc:${esc(x.colour || "#fff")}"></i>`
        : `<i class="sp-dot talent"></i>`;
      return `<li role="option" id="sp-o${k}" data-k="${k}" class="${k === active ? "on" : ""}">${lead}<span>${mark(x.label, t)}</span>${tag}</li>`;
    }).join("");
    const none = !shown.length;
    sug.hidden = none;
    q.setAttribute("aria-expanded", String(!none));
    q.setAttribute("aria-activedescendant", active >= 0 ? `sp-o${active}` : "");
  }

  function choose(x) {
    if (!x) return;
    if (x.kind === "song") { finish(x.id); return; }
    st.chips.push({ kind: x.kind, id: x.id, label: x.label, colour: x.colour });
    st.q = ""; q.value = ""; active = -1;
    drawBar(); drawSug(); drawGrid(); q.focus();
  }
  function finish(key) {
    dialog.close();
    opt.onPick(key);
  }
  function remember() { if (SORTS.some(s => s.key === st.sort)) save({ sort: st.sort, desc: st.desc, cat: st.cat }); else save({ ...load(), cat: st.cat }); }

  q.addEventListener("input", () => { st.q = q.value; active = -1; drawSug(); drawGrid(); });
  q.addEventListener("keydown", e => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (!shown.length) return;
      e.preventDefault();
      active = (active + (e.key === "ArrowDown" ? 1 : -1) + shown.length) % shown.length;
      drawSug();
      sug.querySelector("li.on")?.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (active >= 0) choose(shown[active]);
      else { const list = filtered(); if (list.length === 1) finish(list[0].music_id); else { sug.hidden = true; } }
    } else if (e.key === "Backspace" && !q.value && st.chips.length) {
      st.chips.pop(); drawBar(); drawGrid();
    } else if (e.key === "Escape" && !sug.hidden) {
      e.preventDefault(); sug.hidden = true; active = -1;
    }
  });
  q.addEventListener("blur", () => setTimeout(() => { if (document.activeElement !== q) sug.hidden = true; }, 120));
  q.addEventListener("focus", drawSug);
  sug.addEventListener("mousedown", e => e.preventDefault());
  sug.addEventListener("click", e => { const li = e.target.closest("li[data-k]"); if (li) choose(shown[+li.dataset.k]); });
  dialog.onclick = e => {
    if (e.target === dialog || e.target.closest(".sp-x")) { dialog.close(); return; }
    const so = e.target.closest("[data-sp-sort]");
    if (so) {
      const def = sorts.find(s => s.key === so.dataset.spSort);
      if (st.sort === def.key) st.desc = !st.desc; else { st.sort = def.key; st.desc = !!def.desc; }
      remember(); drawBar(); drawGrid(); return;
    }
    if (e.target.closest(".sp-dir")) { st.desc = !st.desc; remember(); drawBar(); drawGrid(); return; }
    const ca = e.target.closest("[data-sp-cat]");
    if (ca) { st.cat = ca.dataset.spCat; remember(); drawBar(); drawGrid(); return; }
    const un = e.target.closest("[data-sp-unchip]");
    if (un) { st.chips.splice(+un.dataset.spUnchip, 1); drawBar(); drawGrid(); q.focus(); return; }
    const pk = e.target.closest("[data-sp-pick]");
    if (pk) finish(pk.dataset.spPick);
  };

  drawBar(); drawGrid();
  dialog.showModal();
  q.focus();
  const cur = grid.querySelector('[aria-current="true"]');
  if (cur) cur.scrollIntoView({ block: "center" });
}
