// hb-rank.js — the ranking ("setlist") list and the search progress block (Night Deck
// components).
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
  // optional "one per leader" filter (first row of each leader, ranks kept); off by default
  const seen = new Set();
  const shown = rows.map((r, i) => [r, i]).filter(([r, i]) => {
    if (!opts.onePerLeader) return true;
    const k = (r.leader && r.leader.id) || i; if (seen.has(k)) return false; seen.add(k); return true; });
  return `<div class="rk-list">${shown.map(([r, i]) => one(r, i)).join("")}</div>`;
}

/** The long-search block: keeps the cached answer in view (the caller renders it below),
 *  shows stage, done/total, elapsed, ETA and the provisional best. p: {stage, done, total,
 *  elapsed_s, eta_s?, best_so_far?, cached_score?}. With no served total the count reads
 *  "n scored" (never "n / 0"); with no count either, nothing. The Cancel button carries data-cancel. */
export function progress(p) {
  const f = s => s == null ? "—" : s >= 60 ? `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s` : `${Math.round(s)}s`;
  const frac = p.total ? Math.min(1, (p.done ?? 0) / p.total) : 0;
  const num = v => Number(v).toLocaleString("en-US");
  const count = p.total ? `${num(p.done ?? 0)} / ${num(p.total)}` : p.done != null ? `${num(p.done)} scored` : "";
  return `<div class="hb-progress" role="status" aria-live="polite">
    <div class="pg-top"><b>${esc(p.stage || "Searching")}</b><span class="n">${count}</span>
      <span class="pg-t">elapsed <b class="n">${f(p.elapsed_s)}</b> · ETA <b class="n">${f(p.eta_s)}</b></span>
      <button class="btn small" data-cancel>Cancel</button></div>
    <div class="pg-bar"><i style="width:${(frac * 100).toFixed(1)}%"></i></div>
    <div class="pg-best">${p.best_so_far != null ? `Provisional best ${score(p.best_so_far)}` : "No provisional best yet"}${p.cached_score != null ? ` <span class="sub">· the cached answer below stays valid until this finishes (${score(p.cached_score)})</span>` : ""}</div>
  </div>`;
}

if (typeof window !== "undefined") window.HBRank = { list, progress, beats };
