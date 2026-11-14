// The card detail shared by the Collection drawer (/cards) and the Optimizer's card dialog: the
// card's identity, a value row, its skills at a Bloom, its catalogue stats and the current
// event's bonus. Pure functions over a /api/collection card: the caller passes the collection
// document's unit labels and Bloom steps, so this module holds no page state and writes nothing.
// openCardSheet() is the read-only dialog the Optimizer opens on a team's cards.

import { esc, KINDS, KIND_LABEL, sentence, slotAt } from "/cards-skills.js";

const F = window.HBFmt;   // hb-format.js
const fmt = n => (n == null || Number.isNaN(+n) ? F.NA : F.num(Math.round(+n)));
const pctOf = permil => `+${(permil / 10).toFixed(permil % 10 ? 1 : 0)}${F.SP}%`;
const chrOf = id => "chr-" + String(id).split("-")[1];
export const rarOf = c => parseInt(c.rarity, 10) || 3;
export const elIcon = t => `/chrome/element/ui_999_ico_element_${String(t).toLowerCase()}_cl.png`;
export const STATS = [["performance", "Performance"], ["technique", "Technique"], ["sense", "Sense"]];
const total = c => (c.stats ? (c.stats.performance || 0) + (c.stats.technique || 0) + (c.stats.sense || 0) : -1);

/** Largest catalogue value of each stat over these cards (the bars' full width). */
export function statMaxOf(cards) {
  const out = {};
  for (const [k] of STATS) out[k] = Math.max(1, ...[...cards].map(c => (c.stats || {})[k] || 0));
  return out;
}

/** /api/event -> { title, byCard: {id: {live, song}}, byChr: {chr: permil} }, or null. */
export function eventIndex(d) {
  if (!d || d.available === false || !d.live) return null;
  const byCard = {};
  for (const c of [...(d.live.cards || []), ...(d.owned_bonus_cards || [])]) {
    byCard[c.id] = { live: c.live_permil ?? c.permil ?? 0, song: c.song_permil || 0 };
  }
  const byChr = {};
  for (const h of d.live.holomems || []) byChr[h.character_id] = h.permil;
  return { title: (d.event || {}).title || "", byCard, byChr };
}

/** The card's bonus in the event (a holomem Bonus only for a card not owned), or null. */
export function eventOf(card, ev) {
  if (!ev) return null;
  const hit = ev.byCard[card.id];
  if (hit) return { permil: hit.live, song: hit.song, holomemOnly: false };
  const h = ev.byChr[chrOf(card.id)];
  if (h && !card.owned) return { permil: h, song: 0, holomemOnly: true };
  return null;
}

/** Face on the left, stars / element / member / outfit / title / tags on the right. */
export function heroHTML(c, face, tags) {
  return `<div class="dr-hero">
      <div class="dr-face">${face}</div>
      <div class="dr-id">
        <p class="dr-kick"><span class="dr-stars">${"★".repeat(rarOf(c))}</span><img src="${elIcon(c.type)}" alt="">${esc(c.type)}</p>
        <h2 id="dr-title" class="dr-title">${esc(c.member)}</h2>
        <p class="dr-mem">${esc(c.costume_name || "")}</p>
        ${c.title ? `<p class="dr-ctitle">${esc(c.title)}</p>` : ""}
        <div class="dr-tags">${tags}</div>
      </div>
    </div>`;
}

export const genTags = (c, genLabel) => (c.generations || []).map(g => `<span class="dr-tag">${esc(genLabel(g))}</span>`).join("");

export function valueRow(label, val, max, diskVal, pending) {
  const shown = pending ? `<b class="pend">default</b>` : `<b class="num">${val == null ? F.NA : val}</b>`;
  return `<div class="dr-val"><span>${label}</span>${shown}<small>${max != null ? `of ${max}` : ""}${diskVal !== undefined ? ` · on disk ${diskVal ?? F.NA}` : ""}</small></div>`;
}

/** Active / Special / Passive at this Bloom (with the Bloom that lifts a Lv 1 skill), then the Outfit Skill. */
export function skillsHTML(c, bloom, { steps, genLabel }) {
  const rows = KINDS.map(k => {
    const { slot, level, step: st } = slotAt(c, k, bloom, steps);
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

export function statsHTML(c, statMax) {
  if (!c.stats) return `<p class="cd-empty">No stats on file for this card (restart the server if it predates this page).</p>`;
  const tot = total(c);
  return `<div class="dr-stats">${STATS.map(([k, lab]) => {
    const val = c.stats[k];
    const w = statMax[k] ? (val / statMax[k]) * 100 : 0;
    return `<div class="dr-stat ${k}"><span>${lab}</span><b class="num">${fmt(val)}</b><i style="--w:${w.toFixed(1)}%"></i></div>`;
  }).join("")}<div class="dr-stat tot"><span>Total</span><b class="num">${fmt(tot)}</b></div></div>
  <p class="dr-fine">The card fully raised (max level, Limit Break and Bloom), as the catalogue carries it. Not this card at its current level.</p>`;
}

export function eventHTML(c, event) {
  if (!event) return "";
  const ev = eventOf(c, event);
  if (!ev) return `<div class="dr-ev none"><span>${esc(event.title || "Current event")}</span><p>No Event Bonus.</p></div>`;
  return `<div class="dr-ev"><span>${esc(event.title || "Current event")}</span>
    <p><b class="eb">${pctOf(ev.permil)}</b> ${ev.holomemOnly ? "holomem Bonus (Bloom adds more once owned)" : "Event Bonus to Event Pt"}${
      ev.song ? ` · <b class="eb">${pctOf(ev.song)}</b> Song Score Bonus on the event song` : ""}</p>
    <a class="cd-linkbtn" href="/event">Open the event</a></div>`;
}

/* ------------------------------------------------------------ the read-only card dialog */
let sheet = null;
const SH = { ids: [], at: 0, draw: null };

function ensureSheet() {
  if (sheet) return sheet;
  sheet = document.createElement("dialog");
  sheet.className = "cs-sheet";
  sheet.tabIndex = -1;   // focus lands on the panel, not on its first button
  sheet.setAttribute("aria-labelledby", "dr-title");
  sheet.innerHTML = `<div class="cs-top">
      <span class="cs-pos" id="cspos"></span>
      <button type="button" class="dr-nav" data-cs="-1" aria-label="Previous card"><i class="ico prev" style="--ico:url(/ui/nav/ui_999_ico_arrow.png)" aria-hidden="true"></i></button>
      <button type="button" class="dr-nav" data-cs="1" aria-label="Next card"><i class="ico" style="--ico:url(/ui/nav/ui_999_ico_arrow.png)" aria-hidden="true"></i></button>
      <button type="button" class="dr-x" data-cs="x" aria-label="Close"><i class="ico" style="--ico:url(/ui/nav/ui_999_ico_cancel.png)" aria-hidden="true"></i></button>
    </div>
    <div class="cs-body" id="csbody"></div>`;
  document.body.appendChild(sheet);
  sheet.addEventListener("click", e => {
    if (e.target === sheet) { sheet.close(); return; }   // the backdrop
    const b = e.target.closest("[data-cs]");
    if (!b) return;
    if (b.dataset.cs === "x") sheet.close(); else stepSheet(+b.dataset.cs);
  });
  sheet.addEventListener("keydown", e => {
    if (e.key === "ArrowRight") { e.preventDefault(); stepSheet(1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); stepSheet(-1); }
  });
  return sheet;
}

function drawSheet() {
  const n = SH.ids.length;
  sheet.querySelector("#cspos").textContent = n > 1 ? `${SH.at + 1} of ${n}` : "";
  sheet.querySelectorAll(".dr-nav").forEach(b => { b.hidden = n < 2; });
  const body = sheet.querySelector("#csbody");
  body.innerHTML = SH.draw(SH.ids[SH.at], SH.at);
  body.scrollTop = 0;
}

function stepSheet(dir) {
  const n = SH.ids.length;
  if (n < 2) return;
  SH.at = (SH.at + dir + n) % n;
  drawSheet();
}

/** Open the dialog on ids[at]; draw(id, k) returns the body's HTML; ←/→ step through ids. */
export function openCardSheet(ids, at, draw) {
  ensureSheet();
  Object.assign(SH, { ids, at: Math.max(0, at), draw });
  drawSheet();
  if (!sheet.open) { sheet.showModal(); sheet.focus(); }
}
