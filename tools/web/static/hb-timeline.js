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
  return { P, k, all, sel, main: paceLine(P, sel.mean), avg: paceLine(P, all.mean),
    lo: paceLine(P, sel.lo), hi: paceLine(P, sel.hi) };
}
// one stretch per gap between two cuts (0 to the first cut too): the selection's gain, an
// average run's, their difference, and how much the 10-90 % spread of every run grows there
// (null when the payload carries no band)
function paceSegs({ P, all, sel }) {
  const prev = (a, j) => j ? a[j - 1] : 0;
  const width = j => all.hi[j] != null && all.lo[j] != null ? all.hi[j] - all.lo[j] : null;
  return P.cuts.map((c, j) => {
    const gain = sel.mean[j] == null || (j && sel.mean[j - 1] == null) ? null : sel.mean[j] - prev(sel.mean, j);
    const avg = all.mean[j] - prev(all.mean, j);
    const w0 = j ? width(j - 1) : 0, w1 = width(j);
    return { t0: j ? P.cuts[j - 1].t : 0, t1: c.t, gain, avg, d: gain == null ? null : gain - avg,
      spread: w0 == null || w1 == null ? null : w1 - w0 };
  });
}
const biggest = (segs, f) => segs.reduce((b, s) => f(s) != null && (b == null || Math.abs(f(s)) > Math.abs(f(b))) ? s : b, null);
// the lane's layout in px, as hb.css sets it: .tl-pbox starts PB_TOP below the lane's top and is
// PB_H tall (grade names and value hairlines are placed in px, so they need the plot's height)
const PB_TOP = 30, PB_H = 132, GRADE_GAP = 14, VAL_ROW = 19, VAL_MIN = .06, STRIP_MID = 14, STRIP_H = 12;

/** `{ who, track }`: the inner HTML of the Score pace label and lane. */
export function paceParts(o, dur) {
  const S = paceSeries(o), { P, k, all, sel, main, avg, lo, hi } = S;
  const pm = o.pmf || {};
  const share = k == null ? null : (P.bars.mass[k] || [])[P.cuts.length - 1];
  const from = k == null ? null : pm.start + k * pm.width, to = from == null ? null : from + pm.width;
  const ok = P.check && P.check.mass != null && P.check.mass < 1e-3;
  const head = `Score pace${ok ? ` <span class="tl-ok" title="The two sweeps, joined at each cue, give back every bar's share of runs (largest gap ${(P.check.mass * 100).toFixed(3)} points of a percent)">Verified</span>` : ""}
    <small>${k == null ? "Average run · click a bar of the Score distribution"
      : `Runs ending <b>${thousands(from)} – ${thousands(to)}</b>${share != null ? ` · ${(share * 100).toFixed(1)}% of runs` : ""}`}</small>`;
  if (!main) return { who: head, track: `<span class="tl-pnone">No run ends in this bar</span>` };
  const n = P.cuts.length, segs = paceSegs(S);
  const cls = d => d < 0 ? "dn" : "up";
  // the strip under the plot: the gap to an average run per stretch (a bar picked), or where
  // every run's spread grows (no bar picked); hidden when the payload has neither
  const f = k == null ? (s => s.spread) : (s => s.d);
  const big = biggest(segs, f);
  const span = s => `${mmss(s.t0)}–${mmss(s.t1)}`;
  let who = head;
  if (k != null) {
    const endD = sel.mean[n - 1] - all.mean[n - 1];
    who += `<small><b class="${cls(endD)}">${signedKilo(endD)}</b> vs average at the end</small>`;
    if (big && big.d) who += `<small>${big.d < 0 ? "Lost" : "Gained"} most ${span(big)} (<b class="${cls(big.d)}">${signedKilo(big.d)}</b>)</small>`;
  } else if (big && big.spread > 0) {
    who += `<small>Strip: where runs diverge · most ${span(big)} (<b>+${kilo(big.spread)}</b>)</small>`;
  }
  const ends = [sel.hi, all.hi].map(a => a[a.length - 1] || 0);
  const top = Math.max(...ends, sel.mean[n - 1] || 0, 1) * 1.04;
  const X = t => (Math.max(0, Math.min(t, dur)) / dur * PW).toFixed(1), Y = v => (PH - v / top * PH).toFixed(1);
  const L = t => (Math.max(0, Math.min(t, dur)) / dur * 100).toFixed(3) + "%", T = v => ((1 - v / top) * 100).toFixed(2) + "%";
  const ypx = v => (1 - v / top) * PB_H;
  const path = pts => pts.map(([t, v], i) => `${i ? "L" : "M"}${X(t)},${Y(v)}`).join("");
  const band = lo && hi ? `<path class="band" d="${path(hi)}${lo.slice().reverse().map(([t, v]) => `L${X(t)},${Y(v)}`).join("")}Z"/>` : "";
  const last = main[main.length - 1];
  const area = `<path class="area" d="${path(main)}L${X(last[0])},${PH}L0,${PH}Z"/>`;
  const ref = k == null ? "" : `<path class="ref" d="${path(avg)}"/>`;
  // the grades in the upper part of the lane: the low ones crowd the start, where the curve is
  const grades = (o.grades || []).filter(g => g.threshold >= top * .3 && g.threshold <= top);
  const gLines = grades.map(g => `<line class="g" x1="0" x2="${PW}" y1="${Y(g.threshold)}" y2="${Y(g.threshold)}"/>`).join("");
  const gInfo = grades.map(g => {
    const t = paceCrossing(main, g.threshold), ta = paceCrossing(avg, g.threshold);
    const tip = t == null ? `${g.grade} (${thousands(g.threshold)}): ${k == null ? "not reached by an average run" : "these runs end below it"}`
      : `${g.grade} (${thousands(g.threshold)}) at ≈${mmss(t)}${k != null && ta != null ? `, an average run at ≈${mmss(ta)}` : ""}`;
    return { g, t, tip, y: ypx(g.threshold) };
  });
  // a grade passed: a bare diamond where (its time in the tooltip and the hover readout)
  const gMarks = gInfo.filter(x => x.t != null).map(x => `<span class="tl-pd" style="left:${L(x.t)};top:${T(x.g.threshold)}" title="${esc(x.tip)}"><i></i></span>`).join("");
  // grade names on their lines at the left edge, at least GRADE_GAP apart: pushed down in
  // threshold order, then back up if the lowest one would leave the plot
  const names = gInfo.slice().sort((a, b) => a.y - b.y);
  for (let i = 1; i < names.length; i++) names[i].y = Math.max(names[i].y, names[i - 1].y + GRADE_GAP);
  for (let i = names.length - 1; i >= 0; i--) names[i].y = Math.min(names[i].y, i + 1 < names.length ? names[i + 1].y - GRADE_GAP : PB_H - 6);
  const gNames = names.map(x => `<span class="tl-pg${x.t != null ? " on" : ""}" style="top:${x.y.toFixed(1)}px" title="${esc(x.tip)}">${esc(x.g.grade)}</span>`).join("");
  const dots = P.cuts.map((c, j) => {
    const v = sel.mean[j]; if (v == null) return "";
    // the gap to an average run, unless there is none to speak of (before the first window)
    const d = k == null || Math.abs(v - all.mean[j]) < 500 ? null : v - all.mean[j];
    const bnd = sel.lo[j] != null && sel.hi[j] != null ? ` ${k == null ? "8 in 10 runs" : "8 in 10 of these runs"} have ${thousands(sel.lo[j])}–${thousands(sel.hi[j])} here.` : "";
    const tip = `${mmss(c.t)} · ${c.label} · ${k == null ? "An average run" : `Runs ending ${thousands(from)}–${thousands(to)}`}: ${thousands(v)} by here on average${d == null ? "" : ` (an average run: ${thousands(all.mean[j])}, ${d < 0 ? "−" : "+"}${thousands(Math.abs(d))})`}.${bnd} Exact: from the same calculation as the bars.`;
    return `<span class="tl-pc${j === n - 1 ? " end" : ""}" style="left:${L(c.t)};top:${T(v)}" title="${esc(tip)}"><i></i></span>`;
  }).join("");
  // the value row: each cue's figure above its dot, the end's first, then the later cues; a
  // figure closer than VAL_MIN of the width to a kept one is dropped
  const kept = [];
  for (let j = n - 1; j >= 0; j--) {
    const v = sel.mean[j], x = Math.min(P.cuts[j].t, dur) / dur;
    if (v == null || kept.some(q => Math.abs(q.x - x) < VAL_MIN)) continue;
    kept.push({ j, v, x });
  }
  const vals = kept.map(({ j, v, x }) => {
    const side = x > .97 ? " r" : x < .03 ? " l" : "", hair = PB_TOP + ypx(v) - 6 - VAL_ROW;
    return `<span class="tl-pv${j === n - 1 ? " end" : ""}${side}" style="left:${L(P.cuts[j].t)}">${kilo(v)}</span>` +
      (hair > 2 ? `<i class="tl-ph" style="left:${L(P.cuts[j].t)};height:${hair.toFixed(1)}px"></i>` : "");
  }).join("");
  // the strip: one block per stretch, the largest labelled
  let strip = "";
  const fs = segs.map(f);
  if (fs.some(v => v != null)) {
    const mx = Math.max(1, ...fs.map(v => Math.abs(v || 0)));
    const blocks = segs.map((s, i) => {
      const v = fs[i]; if (v == null) return "";
      const h = Math.max(1, Math.abs(v) / mx * STRIP_H), c = k == null ? "sp" : cls(v);
      const tip = `${span(s)}: ${k == null ? `the spread of 8 in 10 runs grows ${signedKilo(v)}` : `${signedKilo(s.gain)} here, an average run ${signedKilo(s.avg)} (${signedKilo(v)})`}`;
      return `<span class="tl-pb ${c}" style="left:calc(${L(s.t0)} + 2px);width:calc(${L(s.t1)} - ${L(s.t0)} - 4px);top:${(v < 0 ? STRIP_MID : STRIP_MID - h).toFixed(1)}px;height:${h.toFixed(1)}px" title="${esc(tip)}"></span>`;
    }).join("");
    const bv = big ? f(big) : null;
    const lab = bv ? `<span class="tl-pbl ${k == null ? "sp" : cls(bv)}" style="left:${L((big.t0 + big.t1) / 2)};top:${bv < 0 ? STRIP_MID - 7 : STRIP_MID + 7}px">${signedKilo(bv)}</span>` : "";
    strip = `<div class="tl-pstrip"><i class="z"></i>${blocks}${lab}</div>`;
  }
  const track = `${vals}<div class="tl-pbox"><svg viewBox="0 0 ${PW} ${PH}" preserveAspectRatio="none" aria-hidden="true">${gLines}${band}${area}${ref}<path class="line" d="${path(main)}"/></svg>${gMarks}${dots}${gNames}</div>${strip}`;
  return { who, track };
}
/** Show another bar's pace (`o.bar`) in a rendered timeline. */
export function setPace(el, o) {
  const tl = el && el.querySelector(".tl"); if (!tl || !o || !o.pace) return;
  const who = tl.querySelector(".tl-pace-who"), track = tl.querySelector(".tl-pace");
  if (!who || !track) return;
  const parts = paceParts(o, +tl.dataset.dur || 1);
  // the lane's 15 s grid and cue lines stay; everything the pace draws is replaced together
  const grid = [...track.children].filter(c => c.matches(".tl-g, .tl-c")).map(c => c.outerHTML).join("");
  who.innerHTML = parts.who; track.innerHTML = grid + parts.track;
  el._pace = o;
}

/** HTML for one timeline. opts.name(cardId)->label, opts.facts(lane)->small text,
 *  opts.face(cardId)->img url (default /chricon/<chr>). */
export function html(T, opts = {}) {
  if (!T || !T.lanes || !T.lanes.length) return `<p class="tl-none">No timeline for this row.</p>`;
  const dur = T.song_seconds || 1;
  const pc = v => (Math.max(0, Math.min(v, dur)) / dur * 100).toFixed(3) + "%";
  const wd = (a, b) => `calc(${pc(b)} - ${pc(a)})`;
  // a window at either end of the song: its chance reads inward, never past the lane's edge
  const edge = a => a.start / dur < .03 ? " edge-l" : Math.min(a.end, dur) / dur > .97 ? " edge-r" : "";
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
        ${ln.active.filter(a => a.start < dur).map(a => `<span class="tl-ac${boosted(a) ? " up" : ""}${edge(a)}" style="left:${pc(a.start)};width:${wd(a.start, a.end)}"${boosted(a) ? ` title="${esc(`${pct(chance(a))} here: raised by the Special of ${a.rider_from.join(" and ")}`)}"` : ""}>${(a.end - a.start) / dur > .035 ? `<em>${pct(chance(a))}</em>` : ""}</span>`).join("")}
      </div>`;
  }).join("");
  const cueRail = (T.special_cues || []).map((s, k) => `<span class="tl-cue" style="left:${pc(s)};--lc:${lanes[k] ? lanes[k].color : "#fff"}" title="Special cue ${k + 1} at ${s.toFixed(1)} s fires slot ${k + 1}">
      <img src="${UI}ui_800_thm_skill_sp_base.png" alt=""><img class="t" src="${UI}ui_800_ico_thm_skill_sp_txt.png" alt="SP"><b>${k + 1}</b></span>`).join("");
  let paceRow = "";
  if (opts.pace && opts.pace.pace) {
    const parts = paceParts(opts.pace, dur);
    paceRow = `<div class="tl-who tl-lab tl-pace-who">${parts.who}</div><div class="tl-track tl-pace">${gridHTML}${parts.track}</div>`;
  }
  // a 15 s mark too close to the song end would print over the end's own label
  const axis = [0, ...grid].filter(s => s === 0 || (dur - s) / dur > .05).map(s => `<span style="left:${pc(s)}">${mmss(s)}</span>`).join("")
    + `<span class="end" style="left:100%">${mmss(dur)}</span>`;
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
  // the selected runs' score by the hovered time, against an average run's, their 8-in-10
  // band and the stretch between two cues the time falls in
  const paceRead = t => {
    const o = el._pace; if (!o || !o.pace) return "";
    const S = paceSeries(o), { k, main, avg, lo, hi } = S, v = paceAt(main, t), a = paceAt(avg, t);
    if (v == null) return "";
    const at = o.pace.cuts.some(c => Math.abs(c.t - t) < .25) ? "" : "≈";
    const gap = k == null || a == null || Math.abs(v - a) < 500 ? "" : ` · <b>${signedKilo(v - a)}</b> vs average`;
    const l = paceAt(lo, t), h = paceAt(hi, t);
    const band = l != null && h != null && h > l ? `<br>8 in 10 ${k == null ? "runs" : "of these"} <b>${kilo(l)}–${kilo(h)}</b>` : "";
    const seg = paceSegs(S).find(s => t <= s.t1) || null;
    const segR = seg && seg.gain != null ? `<br>${mmss(seg.t0)}–${mmss(seg.t1)} gains <b>${signedKilo(seg.gain)}</b>${k == null || seg.d == null ? "" : ` (${signedKilo(seg.d)} vs average)`}` : "";
    // the grades these runs pass in that stretch, with when (and when an average run does)
    const fin = main[main.length - 1][1];
    const crossed = !seg ? [] : (o.grades || []).filter(g => g.threshold >= fin * .3)
      .map(g => ({ g, t: paceCrossing(main, g.threshold), ta: paceCrossing(avg, g.threshold) }))
      .filter(x => x.t != null && x.t > seg.t0 && x.t <= seg.t1);
    const gr = crossed.length ? `<br>${crossed.map(x => `<b>${esc(x.g.grade)}</b> at ≈${mmss(x.t)}${k != null && x.ta != null ? ` (average ≈${mmss(x.ta)})` : ""}`).join(" · ")}` : "";
    return `<div class="f">${k == null ? "An average run" : "These runs"} by here <b>${at}${thousands(v)}</b>${gap}${band}${segR}${gr}</div>`;
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
