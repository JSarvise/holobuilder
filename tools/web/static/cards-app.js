// Collection page (/cards): every card in the catalogue, owned or not, with the level, Bloom
// and Limit Break the account has; filters, sort, search; a detail drawer with the card's
// skills at its Bloom, its stats and the current event's bonus; and the collection
// edits (level / Bloom / Limit Break per card, the account scalars, the default for
// unlisted cards) written ONLY by Save, through the existing POST /api/collection.
// Data: GET /api/collection (cards + bounds), GET /api/event (bonus cards; optional).

import { esc, KINDS, KIND_LABEL, sentence, slotAt, SKILL_FACETS } from "/cards-skills.js";
import { doc, fetchCollection, adopt, discard, valuesOf, setCard, clearCard, entryOf, player, touch, save, fingerprint, isTrial, setTrial } from "/cards-doc.js";

const byId = id => document.getElementById(id);   // not `$`: test_id_contract checks `$("…")` against index.html only
const F = window.HBFmt;   // hb-format.js: exact numbers, one placeholder (PASS-04)
const fmt = n => (n == null || Number.isNaN(+n) ? F.NA : F.num(Math.round(+n)));
const pctOf = permil => `+${(permil / 10).toFixed(permil % 10 ? 1 : 0)}${F.SP}%`;
const chrOf = id => "chr-" + String(id).split("-")[1];
const rarOf = c => parseInt(c.rarity, 10) || 3;
const ELS = ["Cute", "Pure", "Happy"];
const elIcon = t => `/chrome/element/ui_999_ico_element_${String(t).toLowerCase()}_cl.png`;
const STATS = [["performance", "Performance"], ["technique", "Technique"], ["sense", "Sense"]];

const S = {
  cards: [],
  byId: {},
  order: {},          // catalogue index
  event: null,        // { title, byCard: {id: {live, song}}, byChr: {chr: permil} }
  statMax: {},
  view: [],           // filtered + sorted ids
  open: null,         // card id in the drawer
  preview: null,      // bloom being previewed in the drawer's form
  q: "",
  own: "",
  rar: "",
  el: new Set(),
  sel: {},            // facet key -> Set
  sort: "catalogue",
};

let toastT;
function toast(msg) {
  const t = byId("toast"); t.textContent = msg; t.classList.add("on");
  clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove("on"), 3200);
}
const genLabel = g => ((doc.data && doc.data.gen_labels) || {})[g] || g;
const steps = () => (doc.data && doc.data.skill_bloom_step) || {};
const maxLB = c => Math.max(0, (c.level_caps_by_limit_break || []).length - 1);

// Skill kinds still at Lv 1 at this bloom (below the bloom that lifts them to Lv 2).
function lowKinds(card, bloom) {
  if (bloom == null) return [];
  const st = steps();
  return KINDS.filter(k => st[k] != null && bloom < st[k] && ((card.skills || {})[k] || (card.skill_ladder || {})[k]));
}

function eventOf(card) {
  const ev = S.event;
  if (!ev) return null;
  const hit = ev.byCard[card.id];
  if (hit) return { permil: hit.live, song: hit.song, holomemOnly: false };
  const h = ev.byChr[chrOf(card.id)];
  if (h && !card.owned) return { permil: h, song: 0, holomemOnly: true };
  return null;
}

// ---------------------------------------------------------------- facets (panel)
const FACETS = [
  { key: "member", label: "holomem", face: true, pick: c => (c.member ? [c.member] : []) },
  { key: "gen", label: "Unit", pick: c => c.generations || [], text: genLabel },
  ...SKILL_FACETS,
  { key: "flag", label: "Show only", fixed: [["event", "Event Bonus"], ["lv1", "A skill still at Lv 1"], ["edited", "Changed, not saved"]],
    pick: c => {
      const out = [];
      const v = valuesOf(c);
      if (eventOf(c)) out.push("event");
      if (c.owned && lowKinds(c, v.bloom).length) out.push("lv1");
      if (v.changed) out.push("edited");
      return out;
    } },
];
for (const f of FACETS) S.sel[f.key] = new Set();

function passes(c) {
  if (S.own === "owned" && !c.owned) return false;
  if (S.own === "unowned" && c.owned) return false;
  if (S.rar && String(rarOf(c)) !== S.rar) return false;
  if (S.el.size && !S.el.has(c.type)) return false;
  for (const f of FACETS) {
    const set = S.sel[f.key];
    if (set.size && !f.pick(c).some(v => set.has(v))) return false;
  }
  if (S.q) {
    const hay = `${c.member} ${c.costume_name || ""} ${c.title || ""} ${c.id}`.toLowerCase();
    if (!S.q.split(/\s+/).every(w => hay.includes(w))) return false;
  }
  return true;
}

const total = c => (c.stats ? (c.stats.performance || 0) + (c.stats.technique || 0) + (c.stats.sense || 0) : -1);
const SORTS = {
  catalogue: () => 0,
  holomem: (a, b) => chrOf(a.id).localeCompare(chrOf(b.id)) || rarOf(b) - rarOf(a),
  rarity: (a, b) => rarOf(b) - rarOf(a),
  level: (a, b) => (valuesOf(b).level ?? -1) - (valuesOf(a).level ?? -1),
  bloom: (a, b) => (valuesOf(b).bloom ?? -1) - (valuesOf(a).bloom ?? -1),
  lb: (a, b) => (valuesOf(b).limit_break ?? -1) - (valuesOf(a).limit_break ?? -1),
  total: (a, b) => total(b) - total(a),
  performance: (a, b) => ((b.stats || {}).performance ?? -1) - ((a.stats || {}).performance ?? -1),
  technique: (a, b) => ((b.stats || {}).technique ?? -1) - ((a.stats || {}).technique ?? -1),
  sense: (a, b) => ((b.stats || {}).sense ?? -1) - ((a.stats || {}).sense ?? -1),
  event: (a, b) => ((eventOf(b) || {}).permil || 0) - ((eventOf(a) || {}).permil || 0),
};

// ---------------------------------------------------------------- hero
function hero() {
  const cards = S.cards;
  const owned = cards.filter(c => c.owned);
  const ev = S.event;
  const chips = [`<span>${fmt(cards.length)} cards in the game</span>`, `<span>${fmt(owned.length)} owned</span>`];
  if (ev && ev.title) chips.push(`<span><a class="cd-evlink" href="/event">${esc(ev.title)}</a>: ${fmt(Object.keys(ev.byCard).length)} cards with a bonus</span>`);
  byId("cdchips").innerHTML = chips.join("");
  const stat = (b, small) => `<div class="stat-tile cd-stat"><div><span class="lbl">${small}</span><b class="n">${b}</b></div></div>`;
  const byR = r => cards.filter(c => rarOf(c) === r);
  const maxBloom = owned.filter(c => c.bloom != null && c.max_bloom != null && c.bloom >= c.max_bloom).length;
  const maxTrain = owned.filter(c => c.limit_break != null && maxLB(c) && c.limit_break >= maxLB(c)).length;
  byId("cdstats").innerHTML =
    [5, 4, 3].map(r => stat(`${fmt(byR(r).filter(c => c.owned).length)}<i>/${fmt(byR(r).length)}</i>`, `${r}★ owned`)).join("") +
    stat(`${fmt(maxBloom)}<i>/${fmt(owned.length)}</i>`, "at full Bloom") + stat(`${fmt(maxTrain)}<i>/${fmt(owned.length)}</i>`, "at full Limit Break");
}

// ---------------------------------------------------------------- notes above the grid
function notes() {
  const out = [];
  const d = doc.data;
  if (d && d.curve_available === false)
    out.push(`<div class="cd-note warn">dumps/card_level_curve.json is missing, so no level can be resolved. Run <code>python tools/recon/proxy/card_level_curve.py</code>.</div>`);
  if (doc.staleDraft)
    out.push(`<div class="cd-note warn"><b>This browser holds an unsaved draft started before the collection on disk last changed</b> (an account refresh rewrites it). Saving the draft would put its older values back. <button type="button" class="cd-linkbtn" data-act="discard">Discard the draft</button></div>`);
  else if (doc.dirty)
    out.push(`<div class="cd-note">Showing an unsaved draft from this browser, not the file on disk. <button type="button" class="cd-linkbtn" data-act="discard">Discard it</button></div>`);
  const low = S.cards.filter(c => c.owned && lowKinds(c, valuesOf(c).bloom).length).length;
  if (low) {
    const st = Object.entries(steps()).sort((a, b) => a[1] - b[1]).map(([k, v]) => `${KIND_LABEL[k] || k} at Bloom ${v}`).join(", ");
    const on = S.sel.flag.has("lv1");
    out.push(`<div class="cd-note soft">${fmt(low)} owned cards have a skill still at Lv 1. Bloom lifts each to Lv 2: ${esc(st)}. The score uses the Lv 1 value, as the game does.
      <button type="button" class="cd-linkbtn" data-act="lv1">${on ? "Show every card" : "Show them"}</button></div>`);
  }
  byId("cdnotes").innerHTML = out.join("");
}

// ---------------------------------------------------------------- toolbar
function buildBar() {
  const rars = [...new Set(S.cards.map(rarOf))].sort((a, b) => a - b);
  byId("cdrar").innerHTML = `<button type="button" data-v="" aria-pressed="true">All</button>` +
    rars.map(r => `<button type="button" data-v="${r}" aria-pressed="false">${r}★</button>`).join("");
  byId("cdel").innerHTML = ELS.map(t => `<button type="button" data-v="${t}" aria-pressed="false" title="${t}" aria-label="${t}"><img src="${elIcon(t)}" alt=""></button>`).join("");
  const seg = (id, key) => byId(id).addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b) return;
    S[key] = b.dataset.v;
    byId(id).querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    render();
  });
  seg("cdown", "own");
  seg("cdrar", "rar");
  byId("cdel").addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b) return;
    const v = b.dataset.v;
    if (S.el.has(v)) S.el.delete(v); else S.el.add(v);
    b.setAttribute("aria-pressed", String(S.el.has(v)));
    render();
  });
  byId("cdq").addEventListener("input", e => { S.q = e.target.value.trim().toLowerCase(); render(); });
  byId("cdsort").addEventListener("change", e => {
    S.sort = e.target.value;
    try { localStorage.setItem("hb.cards.sort", S.sort); } catch (_) { /* per-viewer convenience only */ }
    render();
  });
  try { const s = localStorage.getItem("hb.cards.sort"); if (s && SORTS[s]) { S.sort = s; byId("cdsort").value = s; } } catch (_) { /* default sort */ }
  byId("cdftog").addEventListener("click", () => {
    const p = byId("cdpanel"); p.hidden = !p.hidden;
    byId("cdftog").setAttribute("aria-expanded", String(!p.hidden));
  });
  byId("cdclear").addEventListener("click", clearFilters);
  buildPanel();
}

function buildPanel() {
  const faces = {};
  for (const c of S.cards) if (c.member && !(c.member in faces)) faces[c.member] = chrOf(c.id);
  const groups = FACETS.map(f => {
    let opts;
    if (f.fixed) opts = f.fixed;
    else {
      const seen = [];
      const order = f.key === "member" ? S.cards.slice().sort((a, b) => chrOf(a.id).localeCompare(chrOf(b.id))) : S.cards;
      for (const c of order) for (const v of f.pick(c)) if (v && !seen.includes(v)) seen.push(v);
      if (f.key !== "member" && f.key !== "gen") seen.sort();
      opts = seen.map(v => [v, f.text ? f.text(v) : v]);
    }
    if (!opts.length) return "";
    return `<div class="cd-fgroup${f.face ? " faces" : ""}"><p>${esc(f.label)}</p><div class="cd-chips">${opts.map(([v, lab]) =>
      `<button type="button" class="cd-chip${S.sel[f.key].has(v) ? " on" : ""}" data-f="${esc(f.key)}" data-v="${esc(v)}"${f.face ? ` title="${esc(lab)}"` : ""}>${
        f.face ? `<img src="/chricon/${encodeURIComponent(faces[v])}" alt="" loading="lazy" onerror="this.remove()">` : ""}<span>${esc(lab)}</span></button>`).join("")}</div></div>`;
  });
  byId("cdpanel").innerHTML = groups.join("");
}

function clearFilters() {
  S.q = ""; S.own = ""; S.rar = ""; S.el.clear();
  for (const f of FACETS) S.sel[f.key].clear();
  byId("cdq").value = "";
  for (const id of ["cdown", "cdrar"]) byId(id).querySelectorAll("button").forEach((b, i) => b.setAttribute("aria-pressed", String(i === 0)));
  byId("cdel").querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", "false"));
  byId("cdpanel").querySelectorAll(".cd-chip.on").forEach(b => b.classList.remove("on"));
  render();
}

function panelCount() { return FACETS.reduce((n, f) => n + S.sel[f.key].size, 0); }

// ---------------------------------------------------------------- grid
function lbPips(lb, max) {
  if (!max) return "";
  let s = "";
  for (let i = 0; i < max; i++) s += `<i class="${lb != null && i < lb ? "on" : ""}"></i>`;
  return `<span class="cd-pips" title="Limit Break ${lb ?? F.NA} of ${max}" aria-label="Limit Break ${lb ?? "unknown"} of ${max}">${s}</span>`;
}

// a try counts for the searches only once it is on disk (Save to disk), so the UI says
// whether it has been saved
const trySaved = id => ((doc.disk && doc.disk.trial) || []).includes(id) === isTrial(id);

function faceOf(c, v, w) {
  return window.HBCard.face({ id: c.id, member: c.member, type: c.type, rarity: rarOf(c),
    level: c.owned || v.typed ? v.level : null, bloom: c.owned || v.typed ? v.bloom : null }, { w, role: false });
}

function tile(c) {
  const v = valuesOf(c);
  const ev = eventOf(c);
  const low = c.owned && lowKinds(c, v.bloom).length;
  const meta = c.owned
    ? `${lbPips(v.limit_break, maxLB(c))}${low ? `<span class="cd-lv1" title="${esc(lowKinds(c, v.bloom).map(k => KIND_LABEL[k]).join(", "))} still at Lv 1">Lv 1 skill</span>` : ""}`
    : isTrial(c.id) ? `<span class="cd-try" title="${trySaved(c.id) ? "Tried as if owned: the searches use it until the next account refresh" : "Not saved yet: press Save to disk so the searches use it"}">${trySaved(c.id) ? "Trying" : "Trying · not saved"}</span>`
    : `<span class="cd-no">Not owned</span>`;
  return `<button type="button" class="cd-tile${c.owned || isTrial(c.id) ? "" : " unowned"}${v.changed ? " changed" : ""}${S.open === c.id ? " open" : ""}" data-id="${esc(c.id)}">
    ${faceOf(c, v, 300)}
    ${v.changed ? `<span class="cd-chg" title="Changed in this browser, not saved">Edited</span>` : ""}
    <span class="cd-cap"><b>${esc(c.member)}</b><small>${esc(c.costume_name || c.title || "")}</small></span>
    <span class="cd-meta">${meta}${ev ? `<span class="cd-evb" title="${ev.holomemOnly ? "holomem Bonus" : "Event Bonus"} in ${esc(S.event.title || "the current event")}">${pctOf(ev.permil)}</span>` : ""}</span>
  </button>`;
}

function render() {
  const list = S.cards.filter(passes);
  const cmp = SORTS[S.sort] || SORTS.catalogue;
  list.sort((a, b) => cmp(a, b) || S.order[a.id] - S.order[b.id]);
  S.view = list.map(c => c.id);
  const grid = byId("cdgrid");
  grid.innerHTML = list.length ? list.map(tile).join("")
    : `<p class="cd-empty">${S.cards.length ? "No card matches these filters." : "No cards loaded."}</p>`;
  const n = panelCount();
  byId("cdftxt").textContent = n ? `Filters · ${n}` : "Filters";
  byId("cdftog").classList.toggle("on", n > 0);
  const any = n || S.q || S.own || S.rar || S.el.size;
  byId("cdclear").hidden = !any;
  byId("cdcount").innerHTML = `<b class="num">${fmt(list.length)}</b> of ${fmt(S.cards.length)} cards`;
  notes();
  saveBar();
}

// ---------------------------------------------------------------- drawer
function openCard(id, { push = true } = {}) {
  const c = S.byId[id];
  if (!c) return;
  S.open = id;
  S.preview = null;
  drawDrawer();
  const d = byId("cddrawer");
  d.hidden = false;
  requestAnimationFrame(() => d.classList.add("on"));
  document.body.classList.add("drawer-open");
  byId("cdgrid").querySelectorAll(".cd-tile.open").forEach(t => t.classList.remove("open"));
  const t = byId("cdgrid").querySelector(`.cd-tile[data-id="${CSS.escape(id)}"]`);
  if (t) t.classList.add("open");
  if (push) history.replaceState(null, "", `#${id}`);
}

function closeDrawer() {
  const d = byId("cddrawer");
  d.classList.remove("on");
  d.hidden = true;
  document.body.classList.remove("drawer-open");
  const t = byId("cdgrid").querySelector(".cd-tile.open");
  if (t) { t.classList.remove("open"); t.focus({ preventScroll: true }); }
  S.open = null;
  history.replaceState(null, "", location.pathname);
}

function step(dir) {
  if (!S.open || !S.view.length) return;
  const i = S.view.indexOf(S.open);
  const next = S.view[(i + dir + S.view.length) % S.view.length];
  if (next) {
    openCard(next);
    const t = byId("cdgrid").querySelector(`.cd-tile[data-id="${CSS.escape(next)}"]`);
    if (t) t.scrollIntoView({ block: "nearest" });
  }
}

function skillsHTML(c, bloom) {
  const rows = KINDS.map(k => {
    const { slot, level, step: st } = slotAt(c, k, bloom, steps());
    if (!slot) return "";
    const low = level === 1;
    return `<div class="dr-sk${low ? " low" : ""}"><div class="dr-skh"><span>${KIND_LABEL[k]}</span>
      <em class="${low ? "lv1" : ""}">Lv ${level}${low ? ` · Lv 2 at Bloom ${st}` : ""}</em></div><p>${sentence(slot, genLabel)}</p></div>`;
  });
  if (c.costume_skill)
    rows.push(`<div class="dr-sk outfit"><div class="dr-skh"><span>${KIND_LABEL.OUTFIT}</span><em>when this card leads</em></div><p>${sentence(c.costume_skill, genLabel)}</p></div>`);
  const body = rows.join("");
  return body || `<p class="cd-empty">No skill data for this card.</p>`;
}

function statsHTML(c) {
  if (!c.stats) return `<p class="cd-empty">No stats on file for this card (restart the server if it predates this page).</p>`;
  const tot = total(c);
  return `<div class="dr-stats">${STATS.map(([k, lab]) => {
    const val = c.stats[k];
    const w = S.statMax[k] ? (val / S.statMax[k]) * 100 : 0;
    return `<div class="dr-stat ${k}"><span>${lab}</span><b class="num">${fmt(val)}</b><i style="--w:${w.toFixed(1)}%"></i></div>`;
  }).join("")}<div class="dr-stat tot"><span>Total</span><b class="num">${fmt(tot)}</b></div></div>
  <p class="dr-fine">The card fully raised (max level, Limit Break and Bloom), as the catalogue carries it. Not this card at its current level.</p>`;
}

function eventHTML(c) {
  const ev = eventOf(c);
  if (!S.event) return "";
  if (!ev) return `<div class="dr-ev none"><span>${esc(S.event.title || "Current event")}</span><p>No Event Bonus.</p></div>`;
  return `<div class="dr-ev"><span>${esc(S.event.title || "Current event")}</span>
    <p><b class="eb">${pctOf(ev.permil)}</b> ${ev.holomemOnly ? "holomem Bonus (Bloom adds more once owned)" : "Event Bonus to Event Pt"}${
      ev.song ? ` · <b class="eb">${pctOf(ev.song)}</b> Song Score Bonus on the event song` : ""}</p>
    <a class="cd-linkbtn" href="/event">Open the event</a></div>`;
}

function valueRow(label, val, max, diskVal, pending) {
  const shown = pending ? `<b class="pend">default</b>` : `<b class="num">${val == null ? F.NA : val}</b>`;
  return `<div class="dr-val"><span>${label}</span>${shown}<small>${max != null ? `of ${max}` : ""}${diskVal !== undefined ? ` · on disk ${diskVal ?? F.NA}` : ""}</small></div>`;
}

function drawDrawer() {
  const c = S.byId[S.open];
  if (!c) return;
  const v = valuesOf(c);
  const e = entryOf(c.id) || {};
  const r = rarOf(c);
  const bloomShown = S.preview !== null ? S.preview : v.bloom;
  const caps = c.level_caps_by_limit_break || [];
  const gens = (c.generations || []).map(g => `<span class="dr-tag">${esc(genLabel(g))}</span>`).join("");
  const showVals = c.owned || v.typed;
  const capFor = lb => (lb != null && caps[lb] != null ? caps[lb] : c.level_cap);
  const diff = f => (v.changed && c[f] !== v[f] ? c[f] : undefined);

  byId("drbody").innerHTML = `
    <div class="dr-hero">
      <div class="dr-face">${faceOf(c, { ...v, bloom: bloomShown }, 600)}</div>
      <div class="dr-id">
        <p class="dr-kick"><span class="dr-stars">${"★".repeat(r)}</span><img src="${elIcon(c.type)}" alt="">${esc(c.type)}</p>
        <h2 id="dr-title" class="dr-title">${esc(c.member)}</h2>
        <p class="dr-mem">${esc(c.costume_name || "")}</p>
        ${c.title ? `<p class="dr-ctitle">${esc(c.title)}</p>` : ""}
        <div class="dr-tags">${gens}<span class="dr-tag ${c.owned || isTrial(c.id) ? "own" : "no"}">${c.owned ? "Owned" : isTrial(c.id) ? "Trying as owned" : "Not owned"}</span></div>
      </div>
    </div>

    <section class="dr-sec"><h3>${c.owned ? "On your account" : "Not owned"}</h3>
      ${showVals ? `<div class="dr-vals">
        ${valueRow("Level", v.level, capFor(v.limit_break), diff("level"), v.pending && e.level === undefined && c.level != null)}
        ${valueRow("Bloom", v.bloom, c.max_bloom, diff("bloom"), v.pending && e.bloom === undefined && c.bloom != null)}
        ${valueRow("Limit Break", v.limit_break, maxLB(c) || null, diff("limit_break"), v.pending && e.limit_break === undefined && c.limit_break != null)}
      </div>` : `<p class="dr-fine">The account does not have this card.</p>`}
      ${!c.owned && !trySaved(c.id) ? `<div class="dr-trysave" role="status"><p><b>${isTrial(c.id) ? "Not in the searches yet." : "Still in the searches until you save."}</b>
        ${isTrial(c.id) ? "The Optimizer and the event search only use it once it is saved." : "Save to take it out of the searches."}</p>
        <button type="button" class="btn primary" id="drtrysave">Save to disk</button></div>` : ""}
      ${c.owned ? "" : `<div class="dr-try"><p class="dr-fine">${isTrial(c.id)
        ? "The optimiser and the event search use this card as if you had it, at the values above. The next account refresh ends the try (your account does not have it)."
        : "Try it as if you had it: the optimiser and the event search will use it, at its max values unless you change them below. The next account refresh ends the try."}</p>
        <button type="button" class="btn small${isTrial(c.id) ? "" : " primary"}" id="drtry">${isTrial(c.id) ? "Stop trying" : "Try as owned"}</button></div>`}
      ${v.changed ? `<p class="dr-fine chg">Changed in this browser. Not on disk until you press Save.</p>` : ""}
    </section>

    <section class="dr-sec"><h3>Skills${bloomShown != null ? ` at Bloom ${bloomShown}` : ""}</h3><div id="drskills">${skillsHTML(c, bloomShown)}</div></section>
    <section class="dr-sec"><h3>Stats</h3>${statsHTML(c)}</section>
    ${S.event ? `<section class="dr-sec"><h3>Event</h3>${eventHTML(c)}</section>` : ""}

    <section class="dr-sec dr-edit"><h3>Change values</h3>
      <div class="dr-form">
        <label><span>Level</span><input id="drlvl" type="number" min="1" step="1" inputmode="numeric" placeholder="${c.level_cap ? `1 to ${capFor(e.limit_break ?? c.limit_break)}` : "level"}" value="${e.level ?? ""}"></label>
        <label><span>Bloom</span><select id="drbloom"><option value="">Default</option>${
          Array.from({ length: (c.max_bloom ?? 5) + 1 }, (_, b) => `<option value="${b}"${e.bloom === b ? " selected" : ""}>${b}</option>`).join("")}</select></label>
        <label><span>Limit Break</span><select id="drlb"${caps.length ? "" : " disabled"}><option value="">Default</option>${
          caps.map((cap, i) => `<option value="${i}"${e.limit_break === i ? " selected" : ""}>${i} · Lv cap ${cap}</option>`).join("")}</select></label>
      </div>
      <p class="dr-fine" id="drhint">${caps.length ? `Level cap by Limit Break: ${caps.join(" / ")}.` : "card_caps.json is not on disk, so the level cap is unknown."} Fully scored from Bloom ${esc(doc.data.fully_scorable_bloom)}. Default = the collection's rule for cards it does not name.</p>
      <p class="dr-err" id="drerr" hidden></p>
      <div class="dr-acts">
        <button type="button" class="btn primary small" id="drapply">Apply</button>
        <button type="button" class="btn small" id="drdefault"${Object.keys(e).length ? "" : " disabled"}>Use the default</button>
      </div>
    </section>`;
  wireForm(c);
}

function wireForm(c) {
  const lvl = byId("drlvl"), bloom = byId("drbloom"), lb = byId("drlb"), err = byId("drerr"), apply = byId("drapply");
  const caps = c.level_caps_by_limit_break || [];
  function check() {
    const lbv = lb.value === "" ? (valuesOf(c).limit_break ?? null) : Number(lb.value);
    const cap = lbv != null && caps[lbv] != null ? caps[lbv] : c.level_cap;
    lvl.max = cap ? String(cap) : "";
    let msg = "";
    if (lvl.value !== "") {
      const n = Number(lvl.value);
      if (!Number.isInteger(n) || n < 1) msg = "Level must be a whole number from 1.";
      else if (cap && n > cap) msg = `Level ${n} is above the cap of ${cap} at Limit Break ${lbv}. Raise the Limit Break first.`;
    }
    err.hidden = !msg; err.textContent = msg; apply.disabled = !!msg;
  }
  lvl.addEventListener("input", check);
  lb.addEventListener("change", check);
  bloom.addEventListener("change", () => {
    S.preview = bloom.value === "" ? null : Number(bloom.value);
    const b = S.preview !== null ? S.preview : valuesOf(c).bloom;
    byId("drskills").innerHTML = skillsHTML(c, b);
    const h = byId("drskills").closest(".dr-sec").querySelector("h3");
    h.textContent = b != null ? `Skills at Bloom ${b}` : "Skills";
  });
  apply.addEventListener("click", () => {
    check();
    if (apply.disabled) return;
    setCard(c.id, { level: lvl.value, bloom: bloom.value, limit_break: lb.value });
    afterEdit(`${c.member} · ${c.costume_name || ""}: changed (not saved yet)`);
  });
  const tryBtn = byId("drtry");
  if (tryBtn) tryBtn.addEventListener("click", () => {
    const on = !isTrial(c.id), top = maxLB(c), caps = c.level_caps_by_limit_break || [];
    setTrial(c, on, { level: caps[top] ?? c.level_cap, bloom: c.max_bloom ?? 5, limit_break: caps.length ? top : null });
    afterEdit(`${c.member} · ${c.costume_name || ""}: ${on ? "tried as owned" : "no longer tried"} — press Save to disk`);
  });
  const trySave = byId("drtrysave");
  if (trySave) trySave.addEventListener("click", () => doSave());
  byId("drdefault").addEventListener("click", () => {
    clearCard(c.id);
    afterEdit(`${c.member} · ${c.costume_name || ""}: back to the default (not saved yet)`);
  });
  check();
}

function afterEdit(msg) {
  S.preview = null;
  render();
  if (S.open) openCard(S.open, { push: false });
  if (msg) toast(msg);
}

// ---------------------------------------------------------------- account values
function acct() {
  const d = doc.data;
  const p = player();
  const pu = p.power_up_permyriad;
  const auto = typeof pu === "string" && pu.trim().toLowerCase() === "auto";
  const def = doc.work.card_default;
  const defOpts = ["max", "unknown"];
  if (typeof def === "string" && !defOpts.includes(def)) defOpts.push(def);
  byId("cdacct").innerHTML = `
    <label class="cd-field"><span>Posters collected</span>
      <input id="acposters" type="number" min="0" max="${esc(d.memory_max_posters)}" step="1" placeholder="—" value="${p.posters ?? ""}">
      <small id="acpostersh"></small></label>
    <div class="cd-field"><span>Member Power-Up %</span>
      <div class="cd-inrow"><input id="acpower" type="number" min="0" max="${(d.power_up_cap_permyriad || 5000) / 100}" step="0.01" placeholder="—"
        value="${!auto && typeof pu === "number" ? pu / 100 : ""}"${auto ? " disabled" : ""}>
      <label class="cd-check"><input id="acauto" type="checkbox"${auto ? " checked" : ""}> Auto</label></div>
      <small id="acpowerh"></small></div>
    <label class="cd-field"><span>Account level</span>
      <input id="aclevel" type="number" min="${d.player_level_min ?? 1}" max="${d.player_level_max ?? ""}" step="1" placeholder="—" value="${p.level ?? ""}">
      <small id="aclevelh"></small></label>
    <label class="cd-field"><span>Cards the collection does not name</span>
      <select id="acdefault">${defOpts.map(o => `<option value="${esc(o)}"${o === def ? " selected" : ""}>${o === "max" ? "Score them maxed" : o === "unknown" ? "Unknown (not scored as maxed)" : esc(o)}</option>`).join("")}</select>
      <small>Written as <code>card_default</code>. An account refresh names every owned card.</small></label>`;
  acctHints();
  const bind = (id, fn) => byId(id).addEventListener("input", () => { fn(byId(id)); touch(); acctHints(); render(); });
  bind("acposters", el => { if (el.value === "") delete player().posters; else player().posters = Number(el.value); });
  bind("aclevel", el => { if (el.value === "") delete player().level; else player().level = Number(el.value); });
  bind("acpower", el => {
    // Typed as the percent the Members list shows (9.74), stored as permyriad (974).
    if (el.value === "") delete player().power_up_permyriad; else player().power_up_permyriad = Math.round(Number(el.value) * 100);
  });
  byId("acauto").addEventListener("change", e => {
    const inp = byId("acpower");
    if (e.target.checked) { player().power_up_permyriad = "auto"; inp.disabled = true; inp.value = ""; }
    else { delete player().power_up_permyriad; inp.disabled = false; }
    touch(); acctHints(); render();
  });
  byId("acdefault").addEventListener("change", e => { doc.work.card_default = e.target.value; touch(); render(); });
}

function acctHints() {
  const d = doc.data;
  const p = player();
  // Memory Bonus by posters: min(2 x posters, posters + 30) permil.
  const permil = t => (t === 0 ? 0 : Math.min(2 * t, t + 30));
  byId("acpostersh").textContent = p.posters !== undefined
    ? `Memory Bonus ${(permil(p.posters) / 10).toFixed(1)}% of Member Parameter (max ${d.memory_max_posters} posters)`
    : "Not set: read as absent, not as zero.";
  const reach = d.power_up_reachable_permyriad;
  const roster = d.power_up_reachable_roster_size != null ? `${d.power_up_reachable_roster_size}-card roster` : "roster";
  const pu = p.power_up_permyriad;
  byId("acpowerh").textContent = typeof pu === "string"
    ? "Auto: the solver derives it from your own cards."
    : pu === undefined ? "Not set: read as absent, not as zero."
      : reach != null && pu > reach ? `Above the ${(reach / 100).toFixed(2)}% the current ${roster} can reach.`
        : reach != null ? `The ${roster} reaches ${(reach / 100).toFixed(2)}% today; the cap is ${((d.power_up_cap_permyriad || 5000) / 100)}%.` : "";
  const gates = d.board_level_gates || [];
  const all = gates.reduce((n, g) => n + g.nodes, 0);
  const lv = p.level || 0;
  const open = gates.reduce((n, g) => (g.level <= lv ? n + g.nodes : n), 0);
  const next = gates.find(g => g.level > lv);
  byId("aclevelh").textContent = p.level === undefined ? "Not set: the board editor gates nothing."
    : !all ? "skilltree.json is not on disk, so nothing can be gated."
      : `${fmt(open)} of ${fmt(all)} Holomem Board nodes open${next ? `; ${next.nodes} more at level ${next.level}` : "; every gate met"}.`;
}

// ---------------------------------------------------------------- save bar
function saveBar() {
  const bar = byId("cdsave");
  bar.hidden = !doc.dirty;
  document.body.classList.toggle("has-save", doc.dirty);
  if (!doc.dirty) return;
  const changed = S.cards.filter(c => valuesOf(c).changed).length;
  const disk = doc.disk || {};
  const acctChanged = fingerprint(disk.player || {}) !== fingerprint(doc.work.player || {}) || disk.card_default !== doc.work.card_default;
  const bits = [];
  if (changed) bits.push(`${changed} card${changed === 1 ? "" : "s"}`);
  if (acctChanged) bits.push("account values");
  const tries = fingerprint(disk.trial || []) !== fingerprint(doc.work.trial || []);
  byId("cdsavemsg").innerHTML = doc.staleDraft
    ? `<b>Draft older than the file on disk.</b> Changed: ${esc(bits.join(" and ") || "values")}. Saving replaces the newer file.`
    : `<b>Unsaved:</b> ${esc(bits.join(" and ") || "changes")}. Nothing on disk has changed yet.${tries ? " <b>Cards tried as owned reach the searches only after Save to disk.</b>" : ""}`;
  bar.classList.toggle("stale", doc.staleDraft);
}

async function doSave() {
  const btn = byId("cdsavebtn");
  btn.disabled = true;
  try {
    // Guard: the file may have moved since this page loaded (a refresh merges the account into it).
    const now = await fetchCollection();
    const moved = fingerprint(now.document || {}) !== doc.base;
    if ((moved || doc.staleDraft) && !window.confirm(
      "state/collection.json changed after this draft was started (an account refresh rewrites it).\n\n" +
      "Saving writes the draft over it, including any older values the draft still holds. Save anyway?")) return;
    const res = await save();
    const bits = [`Saved to ${res.path || "state/collection.json"}`];
    if (res.cards_restated !== undefined) bits.push(`${res.cards_restated} cards restated`);
    if (res.not_fully_scorable && res.not_fully_scorable.length) bits.push(`${res.not_fully_scorable.length} below Bloom ${doc.data.fully_scorable_bloom}`);
    if (res.warnings && res.warnings.length) bits.push(`${res.warnings.length} warnings`);
    adopt(await fetchCollection(), { keepDraft: false });
    ingest();
    acct();
    render();
    if (S.open) openCard(S.open, { push: false });
    toast(bits.join(" · "));
  } catch (e) {
    toast(`Not saved: ${e.message || e}`);
  } finally {
    btn.disabled = false;
  }
}

function doDiscard() {
  discard();
  acct();
  render();
  if (S.open) openCard(S.open, { push: false });
  toast("Draft discarded: showing the file on disk.");
}

// ---------------------------------------------------------------- load
function ingest() {
  S.cards = doc.data.cards || [];
  S.byId = {}; S.order = {};
  S.cards.forEach((c, i) => { S.byId[c.id] = c; S.order[c.id] = i; });
  S.statMax = {};
  for (const [k] of STATS) S.statMax[k] = Math.max(1, ...S.cards.map(c => (c.stats || {})[k] || 0));
}

async function loadEvent() {
  try {
    const r = await fetch("/api/event");
    if (!r.ok) return;
    const d = await r.json();
    if (!d || d.available === false || !d.live) return;
    const byCard = {};
    for (const c of [...(d.live.cards || []), ...(d.owned_bonus_cards || [])]) {
      byCard[c.id] = { live: c.live_permil ?? c.permil ?? 0, song: c.song_permil || 0 };
    }
    const byChr = {};
    for (const h of d.live.holomems || []) byChr[h.character_id] = h.permil;
    S.event = { title: (d.event || {}).title || "", byCard, byChr };
  } catch (_) { /* no event: the page works without it */ }
}

async function main() {
  buildEvents();
  let data;
  try {
    [data] = await Promise.all([fetchCollection(), loadEvent()]);
  } catch (e) {
    byId("cdchips").innerHTML = `<span>Could not load the collection</span>`;
    byId("cdgrid").innerHTML = `<div class="error">${esc(e.message || e)}</div>`;
    return;
  }
  adopt(data);
  ingest();
  hero();
  buildBar();
  acct();
  render();
  const want = decodeURIComponent(location.hash.slice(1));
  if (want && S.byId[want]) openCard(want, { push: false });
}

function buildEvents() {
  byId("cdgrid").addEventListener("click", e => {
    const t = e.target.closest(".cd-tile");
    if (t) openCard(t.dataset.id);
  });
  byId("cdpanel").addEventListener("click", e => {
    const b = e.target.closest(".cd-chip"); if (!b) return;
    const set = S.sel[b.dataset.f];
    if (set.has(b.dataset.v)) set.delete(b.dataset.v); else set.add(b.dataset.v);
    b.classList.toggle("on", set.has(b.dataset.v));
    render();
  });
  byId("cdnotes").addEventListener("click", e => {
    const b = e.target.closest("[data-act]"); if (!b) return;
    if (b.dataset.act === "discard") doDiscard();
    if (b.dataset.act === "lv1") {
      if (S.sel.flag.has("lv1")) S.sel.flag.delete("lv1"); else S.sel.flag.add("lv1");
      buildPanel();
      render();
    }
  });
  byId("drclose").addEventListener("click", closeDrawer);
  byId("drprev").addEventListener("click", () => step(-1));
  byId("drnext").addEventListener("click", () => step(1));
  byId("cdsavebtn").addEventListener("click", doSave);
  byId("cddiscard").addEventListener("click", doDiscard);
  document.addEventListener("keydown", e => {
    if (!S.open) return;
    const typing = /^(INPUT|SELECT|TEXTAREA)$/.test((e.target && e.target.tagName) || "");
    if (e.key === "Escape") { e.preventDefault(); closeDrawer(); }
    else if (!typing && e.key === "ArrowRight") { e.preventDefault(); step(1); }
    else if (!typing && e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
  });
}

main();
