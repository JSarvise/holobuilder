// The Optimizer's card dialog: a click (or Enter / Space) on any of a team's six cards opens the
// Collection's card detail (hb-carddetail.js) for it, read-only. Next to the account's own values
// it shows the level, Bloom and Limit Break this view drew the card with (the search's levers,
// e.g. a maxed account), and the skills at that Bloom. ←/→ step through the team's cards.

import { esc } from "/opt-util.js";
import { heroHTML, genTags, skillsHTML, statsHTML, eventHTML, statMaxOf, eventIndex, openCardSheet } from "/hb-carddetail.js";

const F = window.HBFmt;
let ctx = null;
let statMax = null;
const genLabel = g => ((ctx.D.coll || {}).gen_labels || {})[g] || g;

// the six cards of the clicked card's team: the nearest ancestor holding six distinct cards
function teamOf(el) {
  for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
    const ids = [...new Set([...n.querySelectorAll("[data-card]")].map(x => x.dataset.card).filter(Boolean))];
    if (ids.length >= 6) return { root: n, ids };
  }
  return { root: el.parentElement || document.body, ids: [el.dataset.card] };
}

const numAt = (el, k) => (el && el.dataset[k] != null && el.dataset[k] !== "" ? +el.dataset[k] : null);

function body(root, id, k) {
  const c = ctx.D.cards.get(id) || { id, member: id };
  const el = root.querySelector(`[data-card="${CSS.escape(id)}"]`);
  const sc = { level: numAt(el, "lv"), bloom: numAt(el, "bl"), limit_break: numAt(el, "lb") };
  const own = c.owned ? { level: c.level, bloom: c.bloom, limit_break: c.limit_break } : {};
  const bloom = sc.bloom ?? own.bloom ?? null;
  const caps = c.level_caps_by_limit_break || [];
  const capFor = lb => (lb != null && caps[lb] != null ? caps[lb] : c.level_cap);
  const maxLB = Math.max(0, caps.length - 1) || null;
  const cell = (v, max, up) => `<span><b class="num${up ? " up" : ""}">${v ?? F.NA}</b>${v != null && max != null ? ` <small>of ${max}</small>` : ""}</span>`;
  const row = (label, f, max) => `<span>${label}</span>${cell(sc[f], max(sc), c.owned && sc[f] != null && sc[f] !== own[f])}${cell(own[f], max(own), false)}`;
  const face = window.HBCard.face({ id: c.id, member: c.member, type: c.type, rarity: c.rarity,
    level: sc.level ?? own.level ?? null, bloom }, { w: 600, role: false });
  const tags = `${genTags(c, genLabel)}${k === 0 ? `<span class="dr-tag">Leader</span>` : ""}<span class="dr-tag ${c.owned ? "own" : "no"}">${c.owned ? "Owned" : "Not owned"}</span>`;
  const ev = eventIndex(ctx.D.event);
  statMax = statMax || statMaxOf(ctx.D.cards.values());
  return `${heroHTML(c, face, tags)}
    <div class="cs-main">
      <section class="dr-sec"><h3>Level, Bloom and Limit Break</h3>
        <div class="cs-lv"><span></span><span class="h">As scored</span><span class="h">${c.owned ? "On your account" : "Not owned"}</span>
          ${row("Level", "level", v => capFor(v.limit_break))}
          ${row("Bloom", "bloom", () => c.max_bloom)}
          ${row("Limit Break", "limit_break", () => maxLB)}
        </div>
        <p class="dr-fine">As scored: the values this result used for the card. They differ from the account's when the search raised them (blue).</p>
      </section>
      <section class="dr-sec"><h3>Skills${bloom != null ? ` at Bloom ${bloom}` : ""}</h3>${skillsHTML(c, bloom, { steps: (ctx.D.coll || {}).skill_bloom_step || {}, genLabel })}</section>
      <section class="dr-sec"><h3>Stats</h3>${statsHTML(c, statMax)}</section>
      ${ev ? `<section class="dr-sec"><h3>Event</h3>${eventHTML(c, ev)}</section>` : ""}
    </div>
    <div class="cs-foot"><a class="cd-linkbtn" href="/cards#${esc(encodeURIComponent(id))}">Open in Collection</a></div>`;
}

function open(el) {
  const { root, ids } = teamOf(el);
  openCardSheet(ids, ids.indexOf(el.dataset.card), (id, k) => body(root, id, k));
}

const hit = e => {
  const el = e.target.closest && e.target.closest("[data-card]");
  return el && el.dataset.card && ctx.out().contains(el) && !e.target.closest("button, a, input, select, label") ? el : null;
};

export function wireCardDialog(c) {
  ctx = c;
  document.addEventListener("click", e => { const el = hit(e); if (el) open(el); });
  document.addEventListener("keydown", e => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const el = hit(e);
    if (el && e.target === el) { e.preventDefault(); open(el); }
  });
}
