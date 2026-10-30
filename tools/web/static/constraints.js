import { cardTile } from "/ranking.js";

// Search constraints for the Solve — lock a leader, force-include, or ban cards.
// The backend (`resolve_selectors`, the CLI's own --require/--leader/--exclude
// resolver) supports this; this surfaces it: a small chip picker over
// /api/collection, exposing a live `getConstraints()` the Solve body reads.

let CARDS = [];              // [{id, member, costume_name, owned}]
let byId = new Map();
const state = {
  leader: null,             // one card id or null
  require: new Set(),       // must appear in every team
  exclude: new Set(),       // barred from every team
  includeUnowned: false,    // let a named card that is unowned widen the pool
};
const rerenders = [];        // per-field chip re-render callbacks
// EVERY view of the shared state re-renders on any change — the drawer's
// chips and the step-1 pin slots are two views of ONE `require` set, and a
// mutation from either must repaint both or they silently disagree.
function rerenderAll() { for (const fn of rerenders) fn(); }

function label(c) {
  return (c.member || c.id) + (c.costume_name ? ` — ${c.costume_name}` : "");
}

export function getConstraints() {
  return {
    leader: state.leader ? [state.leader] : [],
    require: [...state.require],
    exclude: [...state.exclude],
    include_unowned: state.includeUnowned,
  };
}

export function constraintsSummary() {
  const n = (state.leader ? 1 : 0) + state.require.size + state.exclude.size;
  return n ? `${n}` : "";
}

async function loadCards() {
  try {
    const r = await fetch("/api/collection");
    if (!r.ok) return;
    const doc = await r.json();
    CARDS = (doc.cards || []).map((c) => ({ ...c, owned: !!c.owned }));
    byId = new Map(CARDS.map((c) => [c.id, c]));
  } catch (_) {
    CARDS = [];
  }
}

function chip(id, onRemove) {
  const el = document.createElement("span");
  el.className = "cstr-chip";
  const c = byId.get(id);
  el.textContent = c ? label(c) : id;
  if (c && !c.owned) el.classList.add("unowned");
  const x = document.createElement("button");
  x.type = "button";
  x.className = "cstr-x";
  x.textContent = "×";
  x.setAttribute("aria-label", "remove");
  x.addEventListener("click", onRemove);
  el.append(x);
  return el;
}

// One labelled field: a search box with a filtered dropdown that adds chips.
// `get()` returns the current selection as an array; `add`/`remove` mutate the
// shared state and re-render.
function field(labelText, { multi, get, add, remove }) {
  const row = document.createElement("div");
  row.className = "cstr-field";
  const lab = document.createElement("label");
  lab.className = "cstr-label";
  lab.textContent = labelText;
  const chips = document.createElement("div");
  chips.className = "cstr-chips";
  const search = document.createElement("input");
  search.type = "search";
  search.className = "cstr-search";
  search.placeholder = multi ? "type a card name…" : "type a card name (one)…";
  const drop = document.createElement("div");
  drop.className = "cstr-drop";
  drop.hidden = true;
  row.append(lab, chips, search, drop);

  function renderChips() {
    chips.textContent = "";
    for (const id of get()) chips.append(chip(id, () => { remove(id); rerenderAll(); }));
  }
  rerenders.push(renderChips);

  function closeDrop() { drop.hidden = true; drop.textContent = ""; }

  function renderDrop() {
    const q = search.value.trim().toLowerCase();
    drop.textContent = "";
    if (!q) { closeDrop(); return; }
    const chosen = new Set(get());
    const hits = CARDS.filter((c) => {
      if (chosen.has(c.id)) return false;
      const hay = `${label(c)} ${c.id}`.toLowerCase();
      return hay.includes(q);
    }).slice(0, 10);
    if (!hits.length) { closeDrop(); return; }
    for (const c of hits) {
      const opt = document.createElement("button");
      opt.type = "button";
      opt.className = "cstr-opt";
      opt.textContent = label(c) + (c.owned ? "" : "  (unowned)");
      if (!c.owned) opt.classList.add("unowned");
      opt.addEventListener("click", () => {
        add(c.id);
        rerenderAll();
        search.value = "";
        closeDrop();
        search.focus();
      });
      drop.append(opt);
    }
    drop.hidden = false;
  }

  search.addEventListener("input", renderDrop);
  search.addEventListener("focus", renderDrop);
  search.addEventListener("blur", () => setTimeout(closeDrop, 150));
  renderChips();
  return row;
}

export async function mountConstraints(host) {
  if (!host) return;
  await loadCards();
  host.textContent = "";

  const wrap = document.createElement("div");
  wrap.className = "cstr-wrap";

  wrap.append(
    field("Leader (pin the outfit slot)", {
      multi: false,
      get: () => (state.leader ? [state.leader] : []),
      add: (id) => { state.leader = id; },       // single: replace
      remove: () => { state.leader = null; },
    }),
    field("Must include", {
      multi: true,
      get: () => [...state.require],
      add: (id) => state.require.add(id),
      remove: (id) => state.require.delete(id),
    }),
    field("Ban", {
      multi: true,
      get: () => [...state.exclude],
      add: (id) => state.exclude.add(id),
      remove: (id) => state.exclude.delete(id),
    }),
  );

  const unownedRow = document.createElement("label");
  unownedRow.className = "cstr-unowned";
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.addEventListener("change", () => { state.includeUnowned = cb.checked; });
  const cbText = document.createElement("span");
  cbText.textContent = "Let a card named above widen the pool even if I don't own it";
  unownedRow.append(cb, cbText);
  wrap.append(unownedRow);

  host.append(wrap);
}

// The step-1 pin strip: four card slots, a VIEW of the same
// `state.require` the drawer edits. An empty slot opens an inline search over
// OWNED cards; a filled slot shows the face and removes on ×.
export async function mountPinSlots(host) {
  if (!host) return;
  if (!CARDS.length) await loadCards();
  host.textContent = "";
  const grid = document.createElement("div");
  grid.className = "pin-grid";
  host.append(grid);

  // The game's own card chooser: a modal grid of game-view tiles (cardTile —
  // portrait + element badge + gen ribbon + rarity band + stars + Lv/bloom,
  // all from the game's extracted chrome), searchable by name. `clearId`
  // names the pin the picker was opened from, so "Clear slot" can empty it.
  function tileFor(c) {
    // cardTile's default footer wants per-card stats the collection payload
    // does not carry; supply the game-view name band instead (title + member).
    const foot = document.createElement("div");
    foot.className = "pick-foot";
    const t = document.createElement("b");
    t.textContent = c.title || c.costume_name || c.id;
    const m = document.createElement("span");
    m.textContent = c.member || "";
    foot.append(t, m);
    return cardTile(c, { foot });
  }

  function openPicker(clearId) {
    const overlay = document.createElement("div");
    overlay.className = "picker-overlay";
    const box = document.createElement("div");
    box.className = "picker-box";
    const head = document.createElement("div");
    head.className = "picker-head";
    const title = document.createElement("b");
    title.textContent = "Choose a card";
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "picker-clear";
    clear.textContent = "Clear slot";
    clear.hidden = !clearId;
    clear.addEventListener("click", () => {
      state.require.delete(clearId); rerenderAll(); overlay.remove();
    });
    const close = document.createElement("button");
    close.type = "button";
    close.className = "picker-close";
    close.textContent = "×";
    close.addEventListener("click", () => overlay.remove());
    head.append(title, clear, close);
    const search = document.createElement("input");
    search.type = "search";
    search.className = "picker-search";
    search.placeholder = "search by name…";
    const gridBox = document.createElement("div");
    gridBox.className = "picker-grid";
    box.append(head, search, gridBox);
    overlay.append(box);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
    function paint() {
      const q = search.value.trim().toLowerCase();
      gridBox.textContent = "";
      const pool = CARDS.filter((c) => c.owned && !state.require.has(c.id) &&
        (!q || (`${label(c)} ${c.title || ""} ${c.id}`.toLowerCase().includes(q))));
      for (const c of pool) {
        const cell = document.createElement("button");
        cell.type = "button";
        cell.className = "picker-cell";
        cell.append(tileFor(c));
        cell.addEventListener("click", () => {
          if (clearId) state.require.delete(clearId);
          state.require.add(c.id); rerenderAll(); overlay.remove();
        });
        gridBox.append(cell);
      }
    }
    search.addEventListener("input", paint);
    paint();
    document.body.append(overlay);
    search.focus();
  }

  function render() {
    grid.textContent = "";
    const pins = [...state.require].slice(0, 4);
    for (let i = 0; i < 4; i++) {
      const id = pins[i];
      const slot = document.createElement("div");
      slot.className = "pin-slot" + (id ? " filled" : "");
      if (id) {
        const c = byId.get(id);
        const open = document.createElement("button");
        open.type = "button";
        open.className = "pin-open";
        if (c) open.append(tileFor(c));
        else open.textContent = id;
        open.addEventListener("click", () => openPicker(id));
        const x = document.createElement("button");
        x.type = "button";
        x.className = "pin-x";
        x.textContent = "×";
        x.setAttribute("aria-label", "remove pin");
        x.addEventListener("click", () => { state.require.delete(id); rerenderAll(); });
        slot.append(open, x);
      } else {
        const plus = document.createElement("button");
        plus.type = "button";
        plus.className = "pin-plus";
        plus.textContent = "+";
        plus.setAttribute("aria-label", "pin an owned card");
        plus.addEventListener("click", () => openPicker(null));
        slot.append(plus);
      }
      grid.append(slot);
    }
  }
  rerenders.push(render);
  render();
}

// Clear every constraint (used when the page resets).
export function resetConstraints() {
  state.leader = null;
  state.require.clear();
  state.exclude.clear();
  state.includeUnowned = false;
  for (const fn of rerenders) fn();
}
