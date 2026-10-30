// The song picker: a panel over the `#song` select.
//
// The song choice is a click that opens a panel, with a search by name and
// filters such as difficulty or singing member. The integration surface is deliberately ONE ELEMENT wide: whatever
// the panel looks like, it writes a music id into `#song` and fires that
// select's `change`. Nothing else on the page learns that a panel exists, and
// the select stays the single place the chosen song lives.
//
// Mounting is DEFERRED until /api/songs actually answers with songs. Until
// then — and forever, if the chart library is absent or the fetch fails — the
// native <select> stays visible and keeps working, with app.js's own reason
// line in it. Enhancement, not replacement: a picker that broke the page's
// only song control on failure would be worse than no picker.

const $ = (id) => document.getElementById(id);

// The pinned first row: choosing it clears the song. It is rendered by the
// same code path as a real song (branching on the empty id), so the keyboard
// cursor treats it as row 0 rather than a special case with its own bugs.
const ANY = { id: "" };

// A song object may arrive WITHOUT the filter fields: the backend gains
// `characters` / `singer_type` / `category` / `seconds` in a parallel change,
// and `filters_available:false` is a real state (main_Music.json absent).
// Every access below goes through this normaliser so a missing field is an
// empty value, never a TypeError that takes the panel down.
function shape(raw) {
  return {
    id: String(raw.music_id || ""),
    title: String(raw.title || raw.music_id || ""),
    titled: raw.titled !== false,
    level: raw.difficulty_level == null ? null : Number(raw.difficulty_level),
    notes: raw.notes == null ? null : Number(raw.notes),
    seconds: raw.seconds == null ? null : Number(raw.seconds),
    singer: String(raw.singer || ""),
    singerType: String(raw.singer_type || ""),
    category: String(raw.category || ""),
    characters: Array.isArray(raw.characters)
      ? raw.characters.map((c) => ({
          id: String((c && c.id) || ""),
          name: String((c && c.name) || (c && c.id) || ""),
        }))
      : [],
  };
}

// 140 s -> "2:20". Round the TOTAL first: rounding the remainder alone turns
// 119.7 s into "1:60".
function mmss(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  const total = Math.round(seconds);
  return Math.floor(total / 60) + ":" + String(total % 60).padStart(2, "0");
}

// The one line that travels with a title everywhere (trigger and rows): the
// numbers that make one chart a different question from another.
function metaLine(song) {
  const parts = [];
  if (song.level != null) parts.push("Lv" + song.level);
  if (song.notes != null) parts.push(song.notes + " notes");
  const dur = mmss(song.seconds);
  if (dur) parts.push(dur);
  if (song.singer) parts.push(song.singer);
  return parts.join(" · ");
}

export function mountSongPicker() {
  const songSelect = $("song");
  const dlg = $("songdlg");
  // No select or no dialog markup: nothing to enhance. The native control (if
  // present) keeps working; this is a quiet no-op by design, not an error.
  if (!songSelect || !dlg) return;

  const ui = {
    search: $("sp-search"),
    member: $("sp-member"), stype: $("sp-stype"),
    cat: $("sp-cat"), level: $("sp-level"),
    clear: $("sp-clear"), nofilters: $("sp-nofilters"),
    count: $("sp-count"), list: $("sp-list"), close: $("sp-close"),
  };

  let songs = [];      // every song the server offered, shaped
  let filtered = [ANY]; // ANY + the songs the current search/filters keep
  let active = 0;       // keyboard cursor: index into `filtered`
  const selectedMembers = new Set(); // chr ids toggled in the member face row
  let trigger = null;   // the button that stands where the select stood

  init();

  async function init() {
    let data;
    try {
      data = await (await fetch("/api/songs")).json();
    } catch {
      // Server unreachable or non-JSON. app.js's own loader writes its error
      // into the native select; replacing that with a dead panel would hide
      // the one message the reader needs. Leave the select alone.
      return;
    }
    if (!data || data.available === false
        || !Array.isArray(data.songs) || !data.songs.length) {
      // No chart library on disk is a STATE, and app.js already puts its
      // reason inside the native select. The select IS the message here; a
      // panel would only bury it.
      return;
    }
    songs = data.songs.map(shape);
    buildFilters(data);
    mountTrigger();
    wire();
    apply();
  }

  // ---- filters: built from the data, never hardcoded ----------------------
  function buildFilters(data) {
    const members = new Map(); // chr id -> display name
    const stypes = new Set();
    const cats = new Set();
    const levels = new Set();
    for (const song of songs) {
      for (const c of song.characters) if (c.id) members.set(c.id, c.name);
      if (song.singerType) stypes.add(song.singerType);
      if (song.category) cats.add(song.category);
      if (song.level != null) levels.add(song.level);
    }

    // A menu whose every row shares one value filters nothing, so it does not
    // earn a place — that is why the thresholds are 2, not 1. Note count and
    // duration got no filter at all for the same reason: they are facts you
    // read off a row, not questions anyone asks of 182 songs. Both still show
    // on every row.
    const showMember = members.size >= 2;
    const showStype = stypes.size >= 2;
    const showCat = cats.size >= 2;
    const showLevel = levels.size >= 2;

    // Member filter as toggle FACE chips (the game's mini icons) instead of a
    // dropdown — prettier, faster to scan, and multi-select (any chosen singer).
    ui.member.textContent = "";
    if (showMember) {
      for (const [id, name] of [...members.entries()].sort((a, b) => a[1].localeCompare(b[1]))) {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "sp-memchip" + (selectedMembers.has(id) ? " on" : "");
        chip.title = name;
        const img = document.createElement("img");
        img.loading = "lazy";
        img.alt = name;
        img.src = "/chricon/" + encodeURIComponent(id);
        img.addEventListener("error", () => { chip.textContent = name.slice(0, 2); });
        chip.appendChild(img);
        chip.addEventListener("click", () => {
          if (selectedMembers.has(id)) { selectedMembers.delete(id); chip.classList.remove("on"); }
          else { selectedMembers.add(id); chip.classList.add("on"); }
          apply();
        });
        ui.member.appendChild(chip);
      }
    }
    if (showStype) fill(ui.stype, [...stypes].sort().map((v) => [v, v]));
    if (showCat) fill(ui.cat, [...cats].sort().map((v) => [v, v]));
    if (showLevel) {
      fill(ui.level, [...levels].sort((a, b) => a - b)
        .map((v) => [String(v), "Lv" + v]));
    }
    // The <label> is the filter's whole visible footprint, so hiding it hides
    // the control and its caption together.
    ui.member.parentElement.hidden = !showMember;
    ui.stype.parentElement.hidden = !showStype;
    ui.cat.parentElement.hidden = !showCat;
    ui.level.parentElement.hidden = !showLevel;

    // Absent metadata is a state with a reason, never three empty menus. The
    // level filter and the search live on chart data and survive either way.
    const metaPresent = songs.some(
      (s) => s.characters.length || s.singerType || s.category);
    if (!metaPresent) {
      ui.nofilters.hidden = false;
      ui.nofilters.textContent = data.filters_available === false
        ? "Member and category filters are unavailable: the master metadata "
          + "(main_Music.json) is not on disk. Search"
          + (showLevel ? " and the level filter" : "") + " still work."
        : "Member and category filters are unavailable: this server does not "
          + "send the filter fields yet — restart it once the song-mode "
          + "backend lands. Search"
          + (showLevel ? " and the level filter" : "") + " still work.";
    }
  }

  function fill(select, pairs) {
    for (const [value, label] of pairs) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      select.appendChild(option);
    }
  }

  // ---- the trigger: shows the CURRENT choice, not "pick a song" -----------
  function mountTrigger() {
    trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "sp-trigger";
    trigger.setAttribute("aria-haspopup", "dialog");
    songSelect.insertAdjacentElement("afterend", trigger);
    // The select stays in the DOM as the value holder — app.js reads its
    // .value and listens for its change, and test_id_contract.py holds that
    // contract — but from here on the trigger is its face.
    songSelect.hidden = true;
    trigger.addEventListener("click", open);
    // Repainting on the select's OWN change (rather than only inside choose)
    // means any writer — this panel, a future script, devtools — keeps the
    // trigger honest about what is actually chosen.
    songSelect.addEventListener("change", paintTrigger);
    paintTrigger();
  }

  function paintTrigger() {
    const id = songSelect.value;
    const song = songs.find((s) => s.id === id);
    const title = document.createElement("b");
    title.className = "sp-t-title";
    const meta = document.createElement("span");
    meta.className = "sp-t-meta";
    if (!id) {
      title.textContent = "Any song (song-agnostic)";
      meta.textContent = "click to search " + songs.length + " charts";
    } else if (song) {
      title.textContent = song.title;
      meta.textContent = metaLine(song) || "click to change";
    } else {
      // A value the loaded list does not know. Shown, never blanked: the
      // select is the authority on what was chosen, not this panel.
      title.textContent = id;
      meta.textContent = "not in the loaded song list";
    }
    trigger.textContent = "";
    trigger.append(title, meta);
  }

  // ---- wiring --------------------------------------------------------------
  function wire() {
    ui.search.addEventListener("input", apply);
    // Member is a face-chip row (its clicks call apply directly); the rest stay
    // <select>s driven by `change`.
    for (const sel of [ui.stype, ui.cat, ui.level]) {
      sel.addEventListener("change", apply);
    }
    ui.clear.addEventListener("click", () => {
      ui.search.value = "";
      for (const sel of [ui.stype, ui.cat, ui.level]) sel.value = "";
      selectedMembers.clear();
      ui.member.querySelectorAll(".sp-memchip.on").forEach((c) => c.classList.remove("on"));
      apply();
      ui.search.focus();
    });
    ui.close.addEventListener("click", () => dlg.close());
    // Click-outside closes. Inside a native <dialog> the backdrop's click
    // target is the dialog element itself; a click on any child arrives with
    // that child as target, so this can never fire on a row.
    dlg.addEventListener("click", (event) => {
      if (event.target === dlg) dlg.close();
    });
    dlg.addEventListener("keydown", onKey);
    // Escape is the platform's (the dialog's own `cancel`), not re-implemented.
  }

  function open() {
    dlg.showModal();
    // The search clears on every open; the FILTERS deliberately survive. A
    // search is a one-shot gesture ("find this one song") and a stale query
    // plus a kept filter reopened onto "0 of 181 match", which reads as a
    // broken library. A filter is a standing preference and stays.
    ui.search.value = "";
    apply();
    // Open on the choice already made, so re-picking starts from where the
    // last pick left off instead of from the top of 182 rows.
    const at = filtered.findIndex((s) => s.id === songSelect.value);
    if (at > 0) {
      active = at;
      paintActive();
    }
    ui.search.focus();
  }

  function onKey(event) {
    // Arrow keys inside a filter <select> must keep their native meaning
    // (they change the select's value); hijacking them there breaks the
    // control they are aimed at.
    if (event.target instanceof HTMLSelectElement) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!filtered.length) return;
      active = event.key === "ArrowDown"
        ? Math.min(active + 1, filtered.length - 1)
        : Math.max(active - 1, 0);
      paintActive();
    } else if (event.key === "Enter" && event.target === ui.search) {
      // Enter in the search box picks the highlighted row. Only there: on a
      // focused row button Enter already clicks it natively, and on the close
      // disc it must keep closing.
      event.preventDefault();
      const song = filtered[active];
      if (song) choose(song.id);
    }
  }

  // ---- the integration surface ---------------------------------------------
  function choose(id) {
    // If the select does not carry an <option> for this id — its own loader
    // failed, or raced this one — add one first: assigning a value with no
    // matching option silently resets a <select> to "", which would un-choose
    // the song without a word. That silent reset is exactly the kind of
    // quiet no-op this page's tests exist to prevent.
    if (id && ![...songSelect.options].some((o) => o.value === id)) {
      const song = songs.find((s) => s.id === id);
      const option = document.createElement("option");
      option.value = id;
      option.textContent = song ? song.title : id;
      songSelect.appendChild(option);
    }
    songSelect.value = id;
    songSelect.dispatchEvent(new Event("change", { bubbles: true }));
    dlg.close();
  }

  // ---- search + filters -----------------------------------------------------
  function apply() {
    const q = ui.search.value.trim().toLowerCase();
    const stype = ui.stype.value;
    const cat = ui.cat.value;
    const level = ui.level.value;
    const matches = songs.filter((song) => {
      if (q && !(song.title.toLowerCase().includes(q)
                 || song.singer.toLowerCase().includes(q)
                 || song.id.includes(q))) return false;
      if (selectedMembers.size && !song.characters.some((c) => selectedMembers.has(c.id))) return false;
      if (stype && song.singerType !== stype) return false;
      if (cat && song.category !== cat) return false;
      if (level && String(song.level) !== level) return false;
      return true;
    });
    const filtering = Boolean(q || selectedMembers.size || stype || cat || level);
    ui.clear.hidden = !filtering;
    filtered = [ANY, ...matches];
    // While filtering, the cursor sits on the first MATCH — type, Enter, done.
    // Unfiltered, it sits on "Any song", which is the current default state.
    active = filtering && matches.length ? 1 : 0;
    ui.count.textContent = matches.length === songs.length
      ? songs.length + " songs"
      : matches.length + " of " + songs.length + " songs match";
    render(matches.length);
  }

  function render(matchCount) {
    ui.list.textContent = "";
    filtered.forEach((song, index) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "sp-row";
      if (index === active) row.classList.add("on");
      if (song.id === songSelect.value) row.classList.add("current");
      const main = document.createElement("span");
      main.className = "sp-r-main";
      const title = document.createElement("b");
      title.className = "sp-r-title";
      const meta = document.createElement("span");
      meta.className = "sp-r-meta";
      if (!song.id) {
        row.classList.add("sp-any");
        title.textContent = "Any song (song-agnostic)";
        meta.textContent =
          "no chart assumed — the ranking stays a direction to build in";
        main.append(title, meta);
        row.append(main);
      } else {
        title.textContent = song.title
          + (song.titled ? "" : " (no title on file)");
        meta.textContent = metaLine(song);
        main.append(title, meta);
        // The cover art, prepended (song_jackets.py renders these): a 404 drops
        // the <img> and the row shows the title alone, the same first-class
        // absence the trigger and card art already honour.
        const jacket = document.createElement("img");
        jacket.className = "sp-r-jacket";
        jacket.loading = "lazy";
        jacket.alt = "";
        jacket.src = "/jacket/" + encodeURIComponent(song.id) + "?w=96";
        jacket.addEventListener("error", () => jacket.remove());
        row.append(jacket, main);
        // Category and singer type ride as chips, when known — absent fields
        // simply produce no chip rather than an empty one.
        const chips = document.createElement("span");
        chips.className = "sp-r-chips";
        for (const tag of [song.category, song.singerType]) {
          if (!tag) continue;
          const chip = document.createElement("i");
          chip.className = "sp-chip";
          chip.textContent = tag;
          chips.appendChild(chip);
        }
        if (chips.childNodes.length) row.appendChild(chips);
      }
      row.addEventListener("click", () => choose(song.id));
      ui.list.appendChild(row);
    });
    if (!matchCount) {
      // Zero matches is the whole list filtered away, not a missing library —
      // said in words, with the way back.
      const empty = document.createElement("p");
      empty.className = "sp-empty";
      empty.textContent = "No song matches that search and those filters. "
        + "The library is still here — clear the filters to get all "
        + songs.length + " songs back.";
      ui.list.appendChild(empty);
    }
  }

  function paintActive() {
    const rows = ui.list.querySelectorAll(".sp-row");
    rows.forEach((row, index) => row.classList.toggle("on", index === active));
    const row = rows[active];
    if (row) row.scrollIntoView({ block: "nearest" });
  }
}
