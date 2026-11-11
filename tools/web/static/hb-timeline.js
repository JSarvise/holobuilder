// hb-timeline.js — the skill uptime timeline (docs/redesign/DESIGN-V3.md §5b, Night Deck skin
// in hb.css `.tl*`). Draws the `timeline` object that /api/row-detail ("timeline": true) and
// POST /api/timeline both return (src/hbuilder/domain/song.py::timeline). It derives NOTHING
// the scorer did not: windows, cues, chances and note times are the payload's own.
//
// Rows, one shared time axis:
//   Special cues  the official SP plate at each of the chart's five cues; cue i fires slot i
//   Notes         notes per second (payload `notes_per_second`), where the score is
//   5 lanes       slot order as scored. Special = solid bar in the lane's top band (a fact);
//                 Active = hold-note capsule, outline in the lane colour, % = that check's chance
//   Actives live  expected number of Actives live (sum of the chances, 0.25 s), stacked by slot
//   Score pace    (opts.pace) the score so far of the runs that end in one histogram bar
//   axis          m:ss every 15 s, song end marked
// Hover: a judgement line + readout of who is live at that instant.
// Actives use ONE fill and print their chance (fill strength by chance was unreadable:
// 55% vs 46%); the stacked strip separates slots with a gap, not only a hue (colour-blind).
// The chance is each check's as the scorer rolls it (payload `probability`: the card's tier
// with the team's and the board's Activation Rate UP, and a Special's rider where one is live
// at the check, `rider_from`); a payload without it falls back to the tier alone. A check a
// rider raises is drawn brighter and says whose Special raised it. Windows are as scheduled,
// unclamped.

// Lane colours by slot: the game's difficulty palette + Blu1st (five distinct game hues,
// independent of element: two same-element members must not share a lane colour).
export const LANE = ["#28C3FF", "#4FDB95", "#FFBB54", "#FD93B6", "#9A93FF"];
const UI = "/ui/skill/";
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;" }, QUOTE = String.fromCharCode(34);
const esc = s => String(s ?? "").replace(/[&<>]/g, c => ESC[c]).split(QUOTE).join("&quot;");
// a check's chance as scored (older payloads: the card's tier alone)
const chance = a => a.probability ?? a.probability_base ?? 1;
const boosted = a => !!(a.rider_from && a.rider_from.length);
// a chance as the game holds it: whole permil, truncated (CalcActivationProbabilityPermil),
// printed with one decimal ("76.7%", never the float rounded to "77%"); 1000 permil = certain
const pct = p => p >= 1 ? "100%" : `${(Math.floor(p * 1000 + 1e-6) / 10).toFixed(1)}%`;
const mmss = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const chrOf = id => "chr-" + String(id).split("-")[1];

// ---- Score pace (opts.pace): the score so far of the runs that end in one bar of the run
// histogram (payload `distribution.pace`, song_distribution.score_pace): its mean and its
// 10-90 % band at each Special cue and at the end, exact; between two cues the curve follows
// the Expected score's own shape (`expected_by_second`), so a grade crossed there is "≈".
// opts.pace = { pace, pmf, bar: index | null (null = every run), grades: [{grade, threshold}] }
const PW = 1000, PH = 100;
const thousands = v => Math.round(v).toLocaleString("en-US");
const kilo = v => `${Math.round(v / 1000).toLocaleString("en-US")}k`;
const signedKilo = d => `${d < 0 ? "−" : "+"}${kilo(Math.abs(d))}`;

/** `[[t, score]]` through 0 at t = 0 and `vals` at each cut, shaped like the Expected
 *  score in between; null if a cut has no value (an empty bar). */
export function paceLine(P, vals) {
  const E = P.expected_by_second || [];
  const knots = [{ t: 0, e: 0, v: 0 }, ...P.cuts.map((c, j) => ({ t: c.t, e: c.expected, v: vals[j] }))];
  const pts = [[0, 0]];
  for (let j = 0; j + 1 < knots.length; j++) {
    const a = knots[j], b = knots[j + 1];
    if (b.v == null) return null;
    for (let s = Math.floor(a.t) + 1; s < b.t; s++) {
      const f = b.e > a.e && E[s] != null ? (E[s] - a.e) / (b.e - a.e) : (s - a.t) / (b.t - a.t);
      pts.push([s, a.v + (b.v - a.v) * Math.min(1, Math.max(0, f))]);
    }
    pts.push([b.t, b.v]);
  }
  return pts;
}
/** When `pts` first reaches `score` (linear between two points), or null. */
export function paceCrossing(pts, score) {
  if (!pts) return null;
  for (let i = 1; i < pts.length; i++) {
    const [t0, v0] = pts[i - 1], [t1, v1] = pts[i];
    if (v0 < score && v1 >= score) return t0 + (t1 - t0) * (score - v0) / (v1 - v0);
  }
  return null;
}
function paceAt(pts, t) {
  if (!pts) return null;
  for (let i = 1; i < pts.length; i++) {
    const [t0, v0] = pts[i - 1], [t1, v1] = pts[i];
    if (t <= t1) return t1 > t0 ? v0 + (v1 - v0) * (t - t0) / (t1 - t0) : v1;
  }
  return pts[pts.length - 1][1];
}
// the selection's series: the bar's (or every run's) mean and band, and every run's mean
function paceSeries(o) {
  const P = o.pace, k = o.bar;
  const all = { mean: P.cuts.map(c => c.mean), lo: P.cuts.map(c => c.p10), hi: P.cuts.map(c => c.p90) };
  const sel = k == null ? all : { mean: P.bars.mean[k], lo: P.bars.p10[k], hi: P.bars.p90[k] };
  return { P, k, all, sel, main: paceLine(P, sel.mean), avg: paceLine(P, all.mean) };
}

/** `{ who, track }`: the inner HTML of the Score pace label and lane. */
export function paceParts(o, dur) {
  const { P, k, all, sel, main, avg } = paceSeries(o);
  const pm = o.pmf || {};
  const share = k == null ? null : (P.bars.mass[k] || [])[P.cuts.length - 1];
  const from = k == null ? null : pm.start + k * pm.width, to = from == null ? null : from + pm.width;
  const ok = P.check && P.check.mass != null && P.check.mass < 1e-3;
  const who = `Score pace${ok ? ` <span class="tl-ok" title="The two sweeps, joined at each cue, give back every bar's share of runs (largest gap ${(P.check.mass * 100).toFixed(3)} points of a percent)">Verified</span>` : ""}
    <small>${k == null ? "Average run · click a bar of the Score distribution"
      : `Runs ending ${thousands(from)} – ${thousands(to)}${share != null ? ` · ${(share * 100).toFixed(1)}% of runs` : ""}`}</small>`;
  if (!main) return { who, track: `<span class="tl-pnone">No run ends in this bar</span>` };
  const ends = [sel.hi, all.hi].map(a => a[a.length - 1] || 0);
  const top = Math.max(...ends, 1) * 1.04;
  const X = t => (Math.max(0, Math.min(t, dur)) / dur * PW).toFixed(1), Y = v => (PH - v / top * PH).toFixed(1);
  const L = t => (Math.max(0, Math.min(t, dur)) / dur * 100).toFixed(3) + "%", T = v => ((1 - v / top) * 100).toFixed(2) + "%";
  const path = pts => pts.map(([t, v], i) => `${i ? "L" : "M"}${X(t)},${Y(v)}`).join("");
  const lo = paceLine(P, sel.lo), hi = paceLine(P, sel.hi);
  const band = lo && hi ? `<path class="band" d="${path(hi)}${lo.slice().reverse().map(([t, v]) => `L${X(t)},${Y(v)}`).join("")}Z"/>` : "";
  const last = main[main.length - 1];
  const area = `<path class="area" d="${path(main)}L${X(last[0])},${PH}L0,${PH}Z"/>`;
  const ref = k == null ? "" : `<path class="ref" d="${path(avg)}"/>`;
  // the grades these runs pass, with when (≈: between two cues the shape is the Expected's)
  // the grades in the upper part of the lane: the low ones crowd the start, where the curve is
  const grades = (o.grades || []).filter(g => g.threshold >= top * .3 && g.threshold <= top);
  const gLines = grades.map(g => `<line class="g" x1="0" x2="${PW}" y1="${Y(g.threshold)}" y2="${Y(g.threshold)}"/>`).join("");
  const gMarks = grades.map(g => {
    const t = paceCrossing(main, g.threshold), ta = paceCrossing(avg, g.threshold);
    const tip = t == null ? `Grade ${g.grade} at ${thousands(g.threshold)}: ${k == null ? "not reached by an average run" : "these runs end below it"}`
      : `Grade ${g.grade} at ${thousands(g.threshold)}: ${k == null ? "an average run reaches it" : "these runs reach it"} at ≈${mmss(t)}${k != null && ta != null ? ` (an average run at ${mmss(ta)})` : ""}`;
    // a grade passed: a diamond where, named with when; one not passed: its name at the end
    return t == null ? `<span class="tl-pg" style="top:${T(g.threshold)}" title="${esc(tip)}">${esc(g.grade)}</span>`
      : `<span class="tl-pd" style="left:${L(t)};top:${T(g.threshold)}" title="${esc(tip)}"><i></i><b>${esc(g.grade)} ≈${mmss(t)}</b></span>`;
  }).join("");
  const dots = P.cuts.map((c, j) => {
    const v = sel.mean[j]; if (v == null) return "";
    // the gap to an average run, unless there is none to speak of (before the first window)
    const d = k == null || Math.abs(v - all.mean[j]) < 500 ? null : v - all.mean[j];
    const band = sel.lo[j] != null && sel.hi[j] != null ? ` ${k == null ? "8 in 10 runs" : "8 in 10 of these runs"} have ${thousands(sel.lo[j])}–${thousands(sel.hi[j])} here.` : "";
    const tip = `${mmss(c.t)} · ${c.label} · ${k == null ? "An average run" : `Runs ending ${thousands(from)}–${thousands(to)}`}: ${thousands(v)} by here on average${d == null ? "" : ` (an average run: ${thousands(all.mean[j])}, ${d < 0 ? "−" : "+"}${thousands(Math.abs(d))})`}.${band} Exact: from the same calculation as the bars.`;
    return `<span class="tl-pc${j === P.cuts.length - 1 ? " end" : ""}" style="left:${L(c.t)};top:${T(v)}" title="${esc(tip)}"><i></i><b>${kilo(v)}</b>${d == null ? "" : `<em class="${d < 0 ? "dn" : "up"}">${signedKilo(d)}</em>`}</span>`;
  }).join("");
  const track = `<div class="tl-pbox"><svg viewBox="0 0 ${PW} ${PH}" preserveAspectRatio="none" aria-hidden="true">${gLines}${band}${area}${ref}<path class="line" d="${path(main)}"/></svg>${gMarks}${dots}</div>`;
  return { who, track };
}
/** Show another bar's pace (`o.bar`) in a rendered timeline. */
export function setPace(el, o) {
  const tl = el && el.querySelector(".tl"); if (!tl || !o || !o.pace) return;
  const who = tl.querySelector(".tl-pace-who"), track = tl.querySelector(".tl-pace");
  if (!who || !track) return;
  const parts = paceParts(o, +tl.dataset.dur || 1);
  who.innerHTML = parts.who; track.innerHTML = parts.track;
  el._pace = o;
}

/** HTML for one timeline. opts.name(cardId)->label, opts.facts(lane)->small text,
 *  opts.face(cardId)->img url (default /chricon/<chr>). */
export function html(T, opts = {}) {
  if (!T || !T.lanes || !T.lanes.length) return `<p class="tl-none">No timeline for this row.</p>`;
  const dur = T.song_seconds || 1;
  const pc = v => (Math.max(0, Math.min(v, dur)) / dur * 100).toFixed(3) + "%";
  const wd = (a, b) => `calc(${pc(b)} - ${pc(a)})`;
  const lanes = T.lanes.map((ln, k) => ({ ...ln, color: LANE[k % LANE.length] }));
  const name = opts.name || (id => id), face = opts.face || (id => `/chricon/${encodeURIComponent(chrOf(id))}`);
  const grid = []; for (let s = 15; s < dur; s += 15) grid.push(s);
  const gridHTML = grid.map(s => `<i class="tl-g" style="left:${pc(s)}"></i>`).join("") +
    (T.special_cues || []).map(s => `<i class="tl-c" style="left:${pc(s)}"></i>`).join("");
  // notes per second
  const nd = T.notes_per_second || null, W = 1000, NH = 30;
  let notesRow = "";
  if (nd && nd.length) {
    const nmax = Math.max(1, ...nd);
    const bars = nd.map((n, s) => s < dur && n ? `<rect x="${(s / dur * W).toFixed(2)}" y="${(NH - n / nmax * NH).toFixed(2)}" width="${(W / dur * .82).toFixed(2)}" height="${(n / nmax * NH).toFixed(2)}"/>` : "").join("");
    notesRow = `<div class="tl-who tl-lab">Notes<small>per second · peak ${nmax}</small></div>
      <div class="tl-track tl-notes">${gridHTML}<svg viewBox="0 0 ${W} ${NH}" preserveAspectRatio="none" aria-hidden="true">${bars}</svg></div>`;
  }
  // expected Actives live, stacked by slot
  const step = .25, LH = 56, samples = []; let peak = 0, sum = 0, cnt = 0;
  for (let t = 0; t <= dur; t += step) {
    const parts = lanes.map(ln => ln.active.reduce((b, a) => b + (t >= a.start && t < a.end ? chance(a) : 0), 0));
    const tot = parts.reduce((a, b) => a + b, 0); peak = Math.max(peak, tot); sum += tot; cnt++; samples.push([t, parts]);
  }
  const ymax = Math.max(2, Math.ceil(peak)), mean = cnt ? sum / cnt : 0;
  const areas = lanes.map((ln, k) => {
    const top = [], bot = [];
    for (const [t, parts] of samples) {
      const lo = parts.slice(0, k).reduce((a, b) => a + b, 0), hi = lo + parts[k], x = (t / dur * W).toFixed(1);
      top.push(`${x},${(LH - hi / ymax * LH).toFixed(1)}`); bot.push(`${x},${(LH - lo / ymax * LH).toFixed(1)}`);
    }
    return `<polygon fill="${ln.color}" stroke="var(--sheet)" stroke-width="1.2" vector-effect="non-scaling-stroke" points="${top.join(" ")} ${bot.reverse().join(" ")}"/>`;
  }).join("");
  const meanY = (LH - mean / ymax * LH).toFixed(1);
  // notes-weighted: expected Actives live x notes in that second = where an Active is worth most
  let weightedRow = "";
  if (nd && nd.length) {
    const wv = nd.map((n, s) => n * samples.filter(([t]) => t >= s && t < s + 1).reduce((a, [, parts]) => a + parts.reduce((x, y) => x + y, 0), 0) / 4);
    const wmax = Math.max(1e-9, ...wv);
    weightedRow = `<div class="tl-who tl-lab">Actives × notes<small>where an Active is worth most</small></div>
      <div class="tl-track tl-notes tl-weighted">${gridHTML}<svg viewBox="0 0 ${W} ${NH}" preserveAspectRatio="none" aria-hidden="true">${wv.map((v, s) => s < dur && v ? `<rect x="${(s / dur * W).toFixed(2)}" y="${(NH - v / wmax * NH).toFixed(2)}" width="${(W / dur * .82).toFixed(2)}" height="${(v / wmax * NH).toFixed(2)}"/>` : "").join("")}</svg></div>`;
  }
  // the leader's skill is not a window: it is on for the whole song (opts.leader = {name, text})
  const leaderRow = opts.leader ? `<div class="tl-who"><span class="tl-slot tl-lead">L</span><span class="tl-nm"><b>${esc(opts.leader.name)}</b><small>${esc(opts.leader.text || "Leader Skill")}</small></span></div>
      <div class="tl-track"><span class="tl-always">Leader Skill · always on</span></div>` : "";
  const laneHTML = lanes.map(ln => {
    // the lane's usual chance (outside any Special window) and its highest under a rider
    const plain = ln.active.filter(a => !boosted(a)).map(chance), up = ln.active.filter(boosted).map(chance);
    const p = (plain.length ? Math.min(...plain) : ln.active[0] ? chance(ln.active[0]) : null);
    const facts = opts.facts ? opts.facts(ln) : (p == null ? "no Active"
      : `Active ${pct(p)}${up.length ? ` (SP ${pct(Math.max(...up))})` : ""} · ${ln.active.length}×`);
    return `<div class="tl-who" style="--lc:${ln.color}"><span class="tl-slot">${ln.slot}</span>
        <span class="tl-face"><img src="${face(ln.card_id)}" alt="" loading="lazy"></span>
        <span class="tl-nm"><b>${esc(name(ln.card_id) || ln.member)}</b><small>${esc(facts)}</small></span></div>
      <div class="tl-track" style="--lc:${ln.color}">${gridHTML}
        ${ln.special.map(s => `<span class="tl-sp" style="left:${pc(s.start)};width:${wd(s.start, s.end)}"><img src="${UI}ui_800_thm_skill_sp_base.png" alt=""></span>`).join("")}
        ${ln.active.filter(a => a.start < dur).map(a => `<span class="tl-ac${boosted(a) ? " up" : ""}" style="left:${pc(a.start)};width:${wd(a.start, a.end)}"${boosted(a) ? ` title="${esc(`${pct(chance(a))} here: raised by the Special of ${a.rider_from.join(" and ")}`)}"` : ""}>${(a.end - a.start) / dur > .035 ? `<em>${pct(chance(a))}</em>` : ""}</span>`).join("")}
      </div>`;
  }).join("");
  const cueRail = (T.special_cues || []).map((s, k) => `<span class="tl-cue" style="left:${pc(s)};--lc:${lanes[k] ? lanes[k].color : "#fff"}" title="Special cue ${k + 1} at ${s.toFixed(1)} s fires slot ${k + 1}">
      <img src="${UI}ui_800_thm_skill_sp_base.png" alt=""><img class="t" src="${UI}ui_800_ico_thm_skill_sp_txt.png" alt="SP"><b>${k + 1}</b></span>`).join("");
  let paceRow = "";
  if (opts.pace && opts.pace.pace) {
    const parts = paceParts(opts.pace, dur);
    paceRow = `<div class="tl-who tl-lab tl-pace-who">${parts.who}</div><div class="tl-track tl-pace">${gridHTML}${parts.track}</div>`;
  }
  const axis = [0, ...grid].map(s => `<span style="left:${pc(s)}">${mmss(s)}</span>`).join("") + `<span class="end" style="left:100%">${mmss(dur)}</span>`;
  return `<div class="tl" data-dur="${dur}" title="Active % = that check's chance as the score rolls it: the card's base chance raised by the team's and the Holomem Board's Activation Rate UP, and by a Special's Activation Rate UP while it is live (brighter windows). Windows are as the scorer schedules them, not clamped to the song end.">
    <div class="tl-who tl-lab">Special cues</div><div class="tl-rail">${cueRail}</div>
    ${notesRow}
    ${leaderRow}${laneHTML}
    <div class="tl-who tl-lab">Actives live<small>expected · mean ${mean.toFixed(2)} · peak ${peak.toFixed(2)}</small></div>
    <div class="tl-track tl-live">${gridHTML}<svg viewBox="0 0 ${W} ${LH}" preserveAspectRatio="none" aria-hidden="true">${areas}
      <line x1="0" x2="${W}" y1="${meanY}" y2="${meanY}" class="mean"/></svg>
      <span class="tl-y" style="top:0">${ymax}</span><span class="tl-y" style="bottom:0">0</span></div>
    ${weightedRow}
    ${paceRow}
    <div></div><div class="tl-axis">${axis}</div>
    <div class="tl-over" aria-hidden="true"><div class="tl-pin" hidden></div><div class="tl-read" hidden></div></div>
  </div>
  <div class="tl-key"><span><i class="k-sp"></i>Special Skill: certain, fires on its cue</span>
    <span><i class="k-ac"></i>Active Skill: may fire, % = its chance with the board's Activation Rate UP</span><span><i class="k-ac up"></i>raised by a Special's Activation Rate UP</span>${nd ? '<span><i class="k-nt"></i>Notes per second</span>' : ""}</div>`;
}

/** Render into `el` and wire the hover readout. Returns the element. */
export function render(el, T, opts = {}) {
  el.innerHTML = html(T, opts);
  el._pace = opts.pace && opts.pace.pace ? opts.pace : null;
  const tl = el.querySelector(".tl"); if (!tl) return el;
  const over = tl.querySelector(".tl-over"), pin = tl.querySelector(".tl-pin"), read = tl.querySelector(".tl-read");
  const dur = T.song_seconds || 1, lanes = T.lanes.map((ln, k) => ({ ...ln, color: LANE[k % LANE.length] }));
  const name = opts.name || (id => id), nd = T.notes_per_second || [];
  const show = t => {
    const live = lanes.flatMap(ln => ln.active.filter(a => t >= a.start && t < a.end).map(a => ({ ln, a })));
    const sp = lanes.flatMap(ln => ln.special.filter(s => t >= s.start && t < s.end).map(s => ({ ln, s })));
    const exp = live.reduce((a, x) => a + chance(x.a), 0);
    const x = t / dur * 100;
    pin.hidden = read.hidden = false; pin.style.left = x + "%";
    read.style.left = x + "%"; read.classList.toggle("flip", x > 70);
    read.innerHTML = `<div class="h"><b>${mmss(t)}</b>${nd.length ? `<span>${nd[Math.floor(t)] || 0} notes/s</span>` : ""}</div>` +
      sp.map(({ ln }) => `<div class="r"><i style="background:${ln.color}"></i>${esc(name(ln.card_id) || ln.member)}<em class="sp">Special · certain</em></div>`).join("") +
      live.map(({ ln, a }) => `<div class="r"><i style="background:${ln.color};opacity:${.35 + .65 * chance(a)}"></i>${esc(name(ln.card_id) || ln.member)}<em>Active · ${pct(chance(a))}${boosted(a) ? ` · Special of ${esc(a.rider_from.join(" + "))}` : ""}</em></div>`).join("") +
      (sp.length || live.length ? "" : `<div class="r">No skill window open</div>`) +
      `<div class="f">Expected Actives live <b>${exp.toFixed(2)}</b></div>` + paceRead(t);
  };
  // the selected runs' score by the hovered time, against an average run's
  const paceRead = t => {
    const o = el._pace; if (!o || !o.pace) return "";
    const { k, main, avg } = paceSeries(o), v = paceAt(main, t), a = paceAt(avg, t);
    if (v == null) return "";
    const at = o.pace.cuts.some(c => Math.abs(c.t - t) < .25) ? "" : "≈";
    return `<div class="f">${k == null ? "An average run" : "These runs"} by here <b>${at}${thousands(v)}</b>${k == null || a == null ? "" : ` · average ${at}${thousands(a)}`}</div>`;
  };
  tl.addEventListener("mousemove", e => {
    const r = over.getBoundingClientRect(); if (e.clientX < r.left || e.clientX > r.right) { pin.hidden = read.hidden = true; return; }
    show((e.clientX - r.left) / r.width * dur);
  });
  tl.addEventListener("mouseleave", () => { if (opts.pin != null) show(opts.pin); else pin.hidden = read.hidden = true; });
  if (opts.pin != null) show(opts.pin);
  return el;
}

if (typeof window !== "undefined") window.HBTimeline = { html, render, setPace, LANE };
