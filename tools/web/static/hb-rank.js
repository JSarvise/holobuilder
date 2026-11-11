// hb-rank.js — the ranking ("setlist") list (a Night Deck component).
//
// Every row shows ONE score set (the row's own: `best` or `song_est`, never a cached figure
// beside a detail figure), its p10–p90 run spread on ONE axis shared by all rows, and, when
// the served pmfs exist, "beats #1 in x% of runs". Fields that are not served yet
// (p10/p50/p90/max per cached row, rows[].alternatives[], pmf) degrade to absent, never to a
// guess. The list is the GLOBAL top N: several rows may share a leader and
// differ only in members; "diff vs #1" outlines the swapped cards so those rows read at a glance.
import { score } from "/hb-grade.js";

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;" }, QUOTE = String.fromCharCode(34);
const esc = s => String(s ?? "").replace(/[&<>]/g, c => ESC[c]).split(QUOTE).join("&quot;");
const pct = v => (v * 100).toFixed(v < .1 && v > 0 ? 1 : 0) + "%";

/** P(X > Y) for two independent served pmfs ({start, width, mass[]}), ties split.
 *  Each bin's mass is spread uniformly over its width; bins of equal width only. */
export function beats(a, b) {
  if (!a || !b || !a.mass || !b.mass || a.width !== b.width) return null;
  const cdfB = [], tot = b.mass.reduce((x, y) => x + y, 0) || 1; let c = 0;
  for (const m of b.mass) { c += m / tot; cdfB.push(c); }
  const at = x => { const k = Math.floor((x - b.start) / b.width); return k < 0 ? 0 : k >= cdfB.length ? 1 : (k ? cdfB[k - 1] : 0) + (cdfB[k] - (k ? cdfB[k - 1] : 0)) * ((x - b.start) / b.width - k); };
  const totA = a.mass.reduce((x, y) => x + y, 0) || 1; let p = 0;
  a.mass.forEach((m, k) => { const mid = a.start + (k + .5) * a.width; p += m / totA * at(mid); });
  return p;
}

/** rows: [{score, p10?, p50?, p90?, max?, pmf?, leader:{id,name,outfit}, cards:[6 ids], sings?, alternatives?}]
 *  opts: {selected, diff (outline cards not in #1), thumb(id)->url, onePerLeader (default false),
 *  format(value)->html (default: the 8-digit score; Event Pt pass a plain number)} */
export function list(rows, opts = {}) {
  if (!rows || !rows.length) return `<p class="sub">No ranked teams yet.</p>`;
  const thumb = opts.thumb || (id => `/cardart/vert/${encodeURIComponent(id)}?w=96`);
  const lo = Math.min(...rows.map(r => r.p10 ?? r.score)), hi = Math.max(...rows.map(r => r.max ?? r.p90 ?? r.score));
  const X = v => ((v - lo) / Math.max(1, hi - lo) * 100).toFixed(2) + "%";
  const top = rows[0], topSet = new Set(top.cards || []);
  const one = (r, i) => {
    const sel = opts.selected === i;
    const b = i && r.pmf && top.pmf ? beats(r.pmf, top.pmf) : null;
    const delta = i ? `${((r.score / top.score - 1) * 100).toFixed(1)}% vs #1` : "best";
    const spread = r.p10 != null && r.p90 != null
      ? `<span class="rk-spread" title="p10 ${r.p10.toLocaleString("en-US")} – p90 ${r.p90.toLocaleString("en-US")}"><i class="iqr" style="left:${X(r.p10)};width:calc(${X(r.p90)} - ${X(r.p10)})"></i>${r.p50 != null ? `<i class="med" style="left:${X(r.p50)}"></i>` : ""}<i class="exp" style="left:${X(r.score)}"></i>${r.max != null ? `<i class="max" style="left:${X(r.max)}"></i>` : ""}</span>`
      : `<span class="rk-spread none" title="spread not served for this row"><i class="exp" style="left:${X(r.score)}"></i></span>`;
    const cards = (r.cards || []).map((id, k) => `<span class="th${k === 0 ? " lead" : ""}${opts.diff && i && !topSet.has(id) ? " swapped" : ""}"><img src="${thumb(id)}" alt="" loading="lazy"></span>`).join("");
    return `<button class="lrow rk-row" data-i="${i}" aria-selected="${sel}">
      <span class="rk-n">${r.rank ?? i + 1}</span>
      <span class="rk-main"><span class="rk-lead"><b>${esc(r.leader && r.leader.name)}</b> <small>${esc(r.leader && r.leader.outfit)}</small>${r.sings ? '<span class="tag">SINGS</span>' : ""}</span>
        <span class="thumbs">${cards}</span>${spread}</span>
      <span class="sc">${opts.format ? opts.format(r.score) : score(r.score)}<small>${delta}${b != null ? ` · beats #1 in ${pct(b)} of runs` : ""}</small></span></button>`;
  };
  return `<div class="rk-list">${shownRows(rows, opts).map(([r, i]) => one(r, i)).join("")}</div>`;
}

/** The [row, index] pairs `list` draws: the optional "one per leader" filter (first row of each
 *  leader, ranks kept; off by default), then at most `opts.limit` of them (the "Teams to show"),
 *  so one per leader picks from every row before the cut. */
export function shownRows(rows, opts = {}) {
  const seen = new Set();
  return (rows || []).map((r, i) => [r, i]).filter(([r, i]) => {
    if (!opts.onePerLeader) return true;
    const k = (r.leader && r.leader.id) || i; if (seen.has(k)) return false; seen.add(k); return true;
  }).slice(0, opts.limit ?? Infinity);
}

if (typeof window !== "undefined") window.HBRank = { list, beats };
