// Event page: one tier's cutoff as a real chart (modelled on the tier dialog of the community
// tool holodori.best): LINE = the border over the event with axes, grid, the forecast and its band; HEATMAP =
// Event Pt the border gained per hour, one row per event day. Pure rendering from the
// /api/event/cutoffs payload (the tier's `series`, `prediction`); no fetch here.
const F = window.HBFmt;
const QUOTE = String.fromCharCode(34);
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", [QUOTE]: "&quot;" };
const ESC_RE = new RegExp("[&<>" + QUOTE + "]", "g");
const esc = s => String(s ?? "").replace(ESC_RE, c => ESC[c]);
const H = 3600e3;
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const pad = n => String(n).padStart(2, "0");

// a read counts for an instant only if reads bracket it this closely (else the hour is "no data")
const MAX_BRACKET = 70 * 60e3;
const PARTIAL_BRACKET = 20 * 60e3;

export function compact(v) {
  const a = Math.abs(v);
  if (a >= 1e6) return `${(v / 1e6).toFixed(a >= 1e7 ? 0 : 1)}M`;
  if (a >= 1e3) return `${(v / 1e3).toFixed(a >= 1e4 ? 0 : 1)}K`;
  return String(Math.round(v));
}
function niceStep(range, ticks) {
  const raw = range / Math.max(1, ticks), mag = 10 ** Math.floor(Math.log10(raw || 1));
  const n = raw / mag;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
}
// "Day 1 05h": the event's day count and the local hour, like the game's own schedule screens
function dayLabel(t, start) {
  const d = new Date(t), s = new Date(start);
  const day = Math.round((new Date(d.getFullYear(), d.getMonth(), d.getDate()) - new Date(s.getFullYear(), s.getMonth(), s.getDate())) / 864e5) + 1;
  return `Day ${day} ${pad(d.getHours())}h`;
}
// the border's series, anchored at (start, 0): every border starts the event at nothing
export function anchored(c, t) {
  const s = (t.series || []).filter(q => q[0] >= c.start_time);
  return [[c.start_time, 0], ...s];
}
// the border at instant x, or null when no two reads bracket it within MAX_BRACKET
export function valueAt(series, x) {
  for (let i = 1; i < series.length; i++) {
    const [a, va] = series[i - 1], [b, vb] = series[i];
    if (x < a || x > b) continue;
    // a score never falls: two equal reads mean no gain in between, however far apart (exact)
    if (va === vb) return { v: va, partial: false };
    if (b - a > MAX_BRACKET && x !== a && x !== b) return null;
    return { v: b === a ? vb : va + (vb - va) * (x - a) / (b - a), partial: b - a > PARTIAL_BRACKET };
  }
  return null;
}

/* ------------------------------------------------------------------ line */
export function lineChart(c, t, opts = {}) {
  const W = 1000, HT = 380, L = 70, R = 28, T = 18, B = 42;
  const series = anchored(c, t);
  const p = t.prediction && t.prediction.method !== "final" ? t.prediction : null;
  const band = (p && p.band) || [], fc = (p && p.points) || [];
  const me = opts.you && c.self && c.self.score && c.self.captured_at >= c.start_time ? c.self : null;
  const mine = opts.you && (c.self_series || []).length > 1 ? c.self_series : null;   // your own line
  const ys = [...series.map(q => q[1]), ...band.map(b => b[2]), ...fc.map(q => q[1]), me ? me.score : 0,
    ...(mine || []).map(q => q[1])];
  const step = niceStep(Math.max(1, ...ys), 5), ymax = Math.ceil(Math.max(1, ...ys) * 1.04 / step) * step;
  const x = ms => L + (W - L - R) * Math.min(1, Math.max(0, (ms - c.start_time) / (c.end_time - c.start_time)));
  const y = v => HT - B - (HT - T - B) * (v / ymax);
  const P = q => `${x(q[0]).toFixed(1)},${y(q[1]).toFixed(1)}`;
  const hours = (c.end_time - c.start_time) / H;
  const xs = [2, 3, 4, 6, 8, 10, 12, 24].find(h => hours / h <= 8) || 24;
  let grid = "";
  for (let v = 0; v <= ymax + 1; v += step) {
    grid += `<line class="g" x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/><text class="yl" x="${L - 10}" y="${(y(v) + 4).toFixed(1)}">${compact(v)}</text>`;
  }
  for (let tt = c.start_time; tt <= c.end_time + 1; tt += xs * H) {
    grid += `<line class="g" x1="${x(tt).toFixed(1)}" x2="${x(tt).toFixed(1)}" y1="${T}" y2="${HT - B}"/><text class="xl" x="${x(tt).toFixed(1)}" y="${HT - B + 22}">${dayLabel(tt, c.start_time)}</text>`;
  }
  const area = band.length ? `<polygon class="band" points="${band.map(b => P([b[0], b[2]])).join(" ")} ${[...band].reverse().map(b => P([b[0], b[1]])).join(" ")}"/>` : "";
  const edges = band.length ? `<polyline class="bedge" points="${band.map(b => P([b[0], b[2]])).join(" ")}"/><polyline class="bedge" points="${band.map(b => P([b[0], b[1]])).join(" ")}"/>` : "";
  const fline = fc.length ? `<polyline class="fc" points="${fc.map(P).join(" ")}"/>` : "";
  const line = `<polyline class="ln" points="${series.map(P).join(" ")}"/>`;
  const dots = series.length <= 60 ? series.slice(1).map(q => `<circle class="dt" cx="${x(q[0]).toFixed(1)}" cy="${y(q[1]).toFixed(1)}" r="3.5"/>`).join("") : "";
  const now = !c.ended && c.now < c.end_time ? `<line class="nowl" x1="${x(c.now).toFixed(1)}" x2="${x(c.now).toFixed(1)}" y1="${T}" y2="${HT - B}"/><text class="nowt" x="${(x(c.now) + 6).toFixed(1)}" y="${T + 12}">now</text>` : "";
  const end = fc.length ? `<text class="endt" x="${(W - R - 4).toFixed(1)}" y="${(y(fc[fc.length - 1][1]) - 10).toFixed(1)}">${compact(p.final)} ±${compact(p.plus_minus || 0)}</text>` : "";
  const you = (mine ? `<polyline class="youln" points="${[[c.start_time, 0], ...mine].map(P).join(" ")}"/>` : "") + (me ? `<circle class="you" cx="${x(me.captured_at).toFixed(1)}" cy="${y(me.score).toFixed(1)}" r="5"/><text class="yout" x="${(x(me.captured_at) + 8).toFixed(1)}" y="${(y(me.score) - 8).toFixed(1)}">You #${F.num(me.rank)}</text>` : "");
  // hover targets: every read and every forecast step, nearest by time
  const pts = [...series.slice(1).map(q => ({ t: q[0], v: q[1], k: "read" })), ...fc.slice(1).map(q => ({ t: q[0], v: q[1], k: "forecast" }))];
  return `<svg class="evc-line" viewBox="0 0 ${W} ${HT}" role="img" aria-label="Top ${F.num(t.rank)} border over the event${p ? ", with its forecast" : ""}"
      data-pts='${esc(JSON.stringify(pts))}' data-x0="${c.start_time}" data-x1="${c.end_time}" data-l="${L}" data-r="${R}" data-w="${W}" data-ymax="${ymax}" data-t="${T}" data-b="${B}" data-h="${HT}">
    ${grid}${area}${edges}${fline}${line}${dots}${now}${end}${you}
    <line class="hov" x1="0" x2="0" y1="${T}" y2="${HT - B}" visibility="hidden"/><circle class="hovd" r="5" cx="0" cy="0" visibility="hidden"/>
  </svg>`;
}
// the tooltip of a line chart (called once after it is in the DOM)
export function wireLine(svg, tip) {
  if (!svg) return;
  const d = svg.dataset, pts = JSON.parse(d.pts || "[]");
  const x0 = +d.x0, x1 = +d.x1, L = +d.l, R = +d.r, W = +d.w, ymax = +d.ymax, T = +d.t, B = +d.b, HT = +d.h;
  const X = ms => L + (W - L - R) * (ms - x0) / (x1 - x0), Y = v => HT - B - (HT - T - B) * (v / ymax);
  const hov = svg.querySelector(".hov"), dot = svg.querySelector(".hovd");
  svg.addEventListener("mousemove", e => {
    if (!pts.length) return;
    const r = svg.getBoundingClientRect(), vx = (e.clientX - r.left) / r.width * W;
    const ms = x0 + (vx - L) / (W - L - R) * (x1 - x0);
    // past the last read the forecast is a curve, not 24 points: follow the pointer (in the
    // reads' own 5-minute steps) instead of jumping ~1.5 h to the nearest forecast step
    const fcs = pts.filter(p => p.k === "forecast"), lastRead = pts.filter(p => p.k === "read").pop();
    let q;
    if (fcs.length && lastRead && ms > lastRead.t) {
      const tt = Math.min(fcs[fcs.length - 1].t, Math.round(ms / 300e3) * 300e3);
      const seg = [lastRead, ...fcs];
      const i = Math.max(1, seg.findIndex(p => p.t >= tt));
      const a = seg[i - 1], b = seg[i] || a;
      q = { t: tt, v: b.t === a.t ? b.v : a.v + (b.v - a.v) * (tt - a.t) / (b.t - a.t), k: "forecast" };
    } else {
      q = pts.reduce((a, b) => Math.abs(b.t - ms) < Math.abs(a.t - ms) ? b : a);
    }
    hov.setAttribute("x1", X(q.t)); hov.setAttribute("x2", X(q.t)); hov.setAttribute("visibility", "visible");
    dot.setAttribute("cx", X(q.t)); dot.setAttribute("cy", Y(q.v)); dot.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.innerHTML = `<b>${F.num(Math.round(q.v))}</b><span>${q.k === "forecast" ? "forecast, " : ""}${esc(F.date(q.t))}</span>`;
    const tx = Math.min(r.width - 170, Math.max(0, X(q.t) / W * r.width + 12));
    tip.style.left = `${tx}px`; tip.style.top = `${Math.max(0, Y(q.v) / HT * r.height - 44)}px`;
  });
  svg.addEventListener("mouseleave", () => { tip.hidden = true; hov.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); });
}

/* ------------------------------------------------------------------ heatmap */
// the event's local hours as rows of days: each cell the Pt the border gained in that hour,
// computed only where reads bracket both ends of the hour (else "no data")
export function heatCells(c, t) {
  const series = anchored(c, t), last = series[series.length - 1][0];
  const s0 = new Date(c.start_time); s0.setMinutes(0, 0, 0);
  const rows = [];
  for (let h0 = s0.getTime(); h0 < c.end_time; h0 += H) {
    const d = new Date(h0), key = `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
    let row = rows[rows.length - 1];
    if (!row || row.key !== key) { row = { key, label: `${DAYS[d.getDay()]} ${d.getDate()}`, cells: Array(24).fill(null) }; rows.push(row); }
    const a = Math.max(h0, c.start_time), b = Math.min(h0 + H, c.end_time);
    let cell = { state: "none", t0: a, t1: b };
    if (a >= c.now) cell.state = "future";
    else if (b > c.now && !c.ended) cell.state = "running";       // the hour in progress
    else if (a >= last) cell.state = "none";
    else {
      const va = valueAt(series, a), vb = valueAt(series, Math.min(b, last));
      if (va && vb && b <= last) cell = { state: "ok", t0: a, t1: b, v: Math.max(0, vb.v - va.v), partial: va.partial || vb.partial || (b - a) < H };
    }
    row.cells[d.getHours()] = cell;
  }
  return rows;
}
const STOPS = [[0, [243, 233, 242]], [0.5, [93, 183, 232]], [1, [123, 91, 214]]];
function colour(f) {
  let i = 1; while (i < STOPS.length - 1 && f > STOPS[i][0]) i++;
  const [fa, ca] = STOPS[i - 1], [fb, cb] = STOPS[i], k = Math.min(1, Math.max(0, (f - fa) / (fb - fa)));
  return `rgb(${ca.map((v, j) => Math.round(v + (cb[j] - v) * k)).join(" ")})`;
}
export function heatmap(c, t, opts = {}) {
  const rows = heatCells(c, t);
  const unit = `${opts.you ? "Your " : ""}${opts.unit || "Event Pt"} / hour`;
  const top = Math.max(0, ...rows.flatMap(r => r.cells.filter(x => x && x.state === "ok").map(x => x.v))), max = Math.max(1, top);
  const any = rows.some(r => r.cells.some(x => x && x.state === "ok"));
  const head = `<span></span>${Array.from({ length: 24 }, (_, h) => `<span class="hh">${h}</span>`).join("")}`;
  const body = rows.map(r => `<span class="dl">${esc(r.label)}</span>` + r.cells.map((x, h) => {
    if (!x) return `<span class="hc out" title="outside the event"></span>`;
    const when = `${r.label} ${pad(h)}:00-${pad((h + 1) % 24)}:00`;
    if (x.state === "ok") return `<span class="hc${x.partial ? " part" : ""}" style="background:${colour(x.v / max)}" title="${esc(when)}: +${F.num(Math.round(x.v))}${x.partial ? " (interpolated across a gap)" : ""}"></span>`;
    if (x.state === "future") return `<span class="hc fut" title="${esc(when)}: not yet"></span>`;
    if (x.state === "running") return `<span class="hc run" title="${esc(when)}: this hour, still running"></span>`;
    return `<span class="hc nod" title="${esc(when)}: no reads around this hour"></span>`;
  }).join("")).join("");
  return `<div class="evc-heat"><div class="evc-grid">${head}${body}</div>
    <div class="evc-legend"><span>+0</span><i style="background:linear-gradient(90deg,${colour(0)},${colour(0.5)},${colour(1)})"></i><span>${F.num(Math.round(top))}</span><span>${esc(unit)}</span></div>
    ${any ? "" : opts.you ? `<p class="evc-note">Your score has no hour with reads at both ends yet: your own rank is read on each refresh.</p>` : `<p class="evc-note">No hour has reads at both ends yet: the tracker reads the border every few minutes, and this fills in as it does.</p>`}
    <p class="evc-note">Hover a cell for that hour's gain. Crossed = no reads around that hour; a dot = interpolated across a gap. Times are your local time.</p></div>`;
}
