// hb-timeline.js — the skill uptime timeline (docs/redesign/DESIGN-V3.md §5b, Night Deck skin
// in hb.css `.tl*`). Draws the `timeline` object that /api/row-detail ("timeline": true) and
// POST /api/timeline both return (src/hbuilder/domain/song.py::timeline). It derives NOTHING
// the scorer did not: windows, cues, chances and note times are the payload's own.
//
// Rows, one shared time axis:
//   Special cues  the official SP plate at each of the chart's five cues; cue i fires slot i
//   Notes         notes per second (payload `notes_per_second`), where the score is
//   5 lanes       slot order as scored. Special = solid bar in the lane's top band (a fact);
//                 Active = hold-note capsule, outline in the lane colour, fill = base chance
//   Actives live  expected number of Actives live (sum of base chances, 0.25 s), stacked by slot
//   axis          m:ss every 15 s, song end marked
// Hover: a judgement line + readout of who is live at that instant.
// Actives use ONE fill and print their chance (fill strength by chance was unreadable:
// 55% vs 46%); the stacked strip separates slots with a gap, not only a hue (colour-blind).
// Honesty: the chance is the BASE tier (a lower bound; team rate-up / board uptime are not
// folded in) and windows are as scheduled, unclamped. Both are said in the block's tooltip.

// Lane colours by slot: the game's difficulty palette + Blu1st (five distinct game hues,
// independent of element: two same-element members must not share a lane colour).
export const LANE = ["#28C3FF", "#4FDB95", "#FFBB54", "#FD93B6", "#9A93FF"];
const UI = "/ui/skill/";
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;" }, QUOTE = String.fromCharCode(34);
const esc = s => String(s ?? "").replace(/[&<>]/g, c => ESC[c]).split(QUOTE).join("&quot;");
const mmss = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const chrOf = id => "chr-" + String(id).split("-")[1];

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
    const parts = lanes.map(ln => ln.active.reduce((b, a) => b + (t >= a.start && t < a.end ? (a.probability_base ?? 1) : 0), 0));
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
    const p = ln.active[0] ? Math.round((ln.active[0].probability_base ?? 1) * 100) : null;
    const facts = opts.facts ? opts.facts(ln) : `${p == null ? "no Active" : `Active ${p}%`} · ${ln.active.length}×`;
    return `<div class="tl-who" style="--lc:${ln.color}"><span class="tl-slot">${ln.slot}</span>
        <span class="tl-face"><img src="${face(ln.card_id)}" alt="" loading="lazy"></span>
        <span class="tl-nm"><b>${esc(name(ln.card_id) || ln.member)}</b><small>${esc(facts)}</small></span></div>
      <div class="tl-track" style="--lc:${ln.color}">${gridHTML}
        ${ln.special.map(s => `<span class="tl-sp" style="left:${pc(s.start)};width:${wd(s.start, s.end)}"><img src="${UI}ui_800_thm_skill_sp_base.png" alt=""></span>`).join("")}
        ${ln.active.filter(a => a.start < dur).map(a => `<span class="tl-ac" style="left:${pc(a.start)};width:${wd(a.start, a.end)}">${(a.end - a.start) / dur > .035 ? `<em>${Math.round((a.probability_base ?? 1) * 100)}%</em>` : ""}</span>`).join("")}
      </div>`;
  }).join("");
  const cueRail = (T.special_cues || []).map((s, k) => `<span class="tl-cue" style="left:${pc(s)};--lc:${lanes[k] ? lanes[k].color : "#fff"}" title="Special cue ${k + 1} at ${s.toFixed(1)} s fires slot ${k + 1}">
      <img src="${UI}ui_800_thm_skill_sp_base.png" alt=""><img class="t" src="${UI}ui_800_ico_thm_skill_sp_txt.png" alt="SP"><b>${k + 1}</b></span>`).join("");
  const axis = [0, ...grid].map(s => `<span style="left:${pc(s)}">${mmss(s)}</span>`).join("") + `<span class="end" style="left:100%">${mmss(dur)}</span>`;
  return `<div class="tl" data-dur="${dur}" title="Active fill = the base chance (a lower bound: team rate-up and board uptime are not folded in). Windows are as the scorer schedules them, not clamped to the song end.">
    <div class="tl-who tl-lab">Special cues</div><div class="tl-rail">${cueRail}</div>
    ${notesRow}
    ${leaderRow}${laneHTML}
    <div class="tl-who tl-lab">Actives live<small>expected · mean ${mean.toFixed(2)} · peak ${peak.toFixed(2)}</small></div>
    <div class="tl-track tl-live">${gridHTML}<svg viewBox="0 0 ${W} ${LH}" preserveAspectRatio="none" aria-hidden="true">${areas}
      <line x1="0" x2="${W}" y1="${meanY}" y2="${meanY}" class="mean"/></svg>
      <span class="tl-y" style="top:0">${ymax}</span><span class="tl-y" style="bottom:0">0</span></div>
    ${weightedRow}
    <div></div><div class="tl-axis">${axis}</div>
    <div class="tl-over" aria-hidden="true"><div class="tl-pin" hidden></div><div class="tl-read" hidden></div></div>
  </div>
  <div class="tl-key"><span><i class="k-sp"></i>Special Skill: certain, fires on its cue</span>
    <span><i class="k-ac"></i>Active Skill: may fire, % = base chance</span>${nd ? '<span><i class="k-nt"></i>Notes per second</span>' : ""}</div>`;
}

/** Render into `el` and wire the hover readout. Returns the element. */
export function render(el, T, opts = {}) {
  el.innerHTML = html(T, opts);
  const tl = el.querySelector(".tl"); if (!tl) return el;
  const over = tl.querySelector(".tl-over"), pin = tl.querySelector(".tl-pin"), read = tl.querySelector(".tl-read");
  const dur = T.song_seconds || 1, lanes = T.lanes.map((ln, k) => ({ ...ln, color: LANE[k % LANE.length] }));
  const name = opts.name || (id => id), nd = T.notes_per_second || [];
  const show = t => {
    const live = lanes.flatMap(ln => ln.active.filter(a => t >= a.start && t < a.end).map(a => ({ ln, a })));
    const sp = lanes.flatMap(ln => ln.special.filter(s => t >= s.start && t < s.end).map(s => ({ ln, s })));
    const exp = live.reduce((a, x) => a + (x.a.probability_base ?? 1), 0);
    const x = t / dur * 100;
    pin.hidden = read.hidden = false; pin.style.left = x + "%";
    read.style.left = x + "%"; read.classList.toggle("flip", x > 70);
    read.innerHTML = `<div class="h"><b>${mmss(t)}</b>${nd.length ? `<span>${nd[Math.floor(t)] || 0} notes/s</span>` : ""}</div>` +
      sp.map(({ ln }) => `<div class="r"><i style="background:${ln.color}"></i>${esc(name(ln.card_id) || ln.member)}<em class="sp">Special · certain</em></div>`).join("") +
      live.map(({ ln, a }) => `<div class="r"><i style="background:${ln.color};opacity:${.35 + .65 * (a.probability_base ?? 1)}"></i>${esc(name(ln.card_id) || ln.member)}<em>Active · ${Math.round((a.probability_base ?? 1) * 100)}%</em></div>`).join("") +
      (sp.length || live.length ? "" : `<div class="r">No skill window open</div>`) +
      `<div class="f">Expected Actives live <b>${exp.toFixed(2)}</b></div>`;
  };
  tl.addEventListener("mousemove", e => {
    const r = over.getBoundingClientRect(); if (e.clientX < r.left || e.clientX > r.right) { pin.hidden = read.hidden = true; return; }
    show((e.clientX - r.left) / r.width * dur);
  });
  tl.addEventListener("mouseleave", () => { if (opts.pin != null) show(opts.pin); else pin.hidden = read.hidden = true; });
  if (opts.pin != null) show(opts.pin);
  return el;
}

if (typeof window !== "undefined") window.HBTimeline = { html, render, LANE };
