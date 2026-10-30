// The per-song picture: when each of the five members is actually LIVE, as an
// activation chart — one coloured lane per member
// (with their face), an "Active" summary lane, and an "Overlap" heat lane, over
// a shared time axis.
//
// `song.timeline()` produces the data: five lanes, the chart's own SPECIAL cues,
// every ACTIVE's cooldown lattice. The Active and Overlap lanes are DERIVED here
// from those windows (the server sends per-member windows only).
//
// ⛔ **A SPECIAL is a FACT and an ACTIVE is a CHANCE, never drawn alike.** The
// SPECIAL fires at a cue the chart names (solid); an ACTIVE has a per-check
// probability (hatched), drawn inside each member's own colour.
//
// ⚠ The probability is the BASE tier only — a LOWER bound (team rate-up and the
// board's uptime are not folded in). ⚠ Windows are half-open and NOT clamped to
// the song's end; a bar may run past the axis, and the axis marks where the song
// stops.

const STYLE_ID = "tl-style";

// Five distinct member hues (teal / amber / indigo / rose / green). Assigned by lane order; the SAME colour carries the member's face, its
// bars and its share of the Active lane.
const LANE_COLORS = ["#3bb6a6", "#d08a3c", "#7b78e6", "#d05a9e", "#83b23f"];

const CSS = `
.tl { margin: var(--s5, 24px) 0 0; }
.tl-head { display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; margin-bottom:4px; }
.tl-title { font-family:var(--display, sans-serif); font-weight:700; font-size:15px; color:var(--fg); }
.tl-sub { font-family:var(--figure, monospace); font-size:11px; color:var(--dim);
  font-variant-numeric:tabular-nums; }
.tl-note { font-size:11.5px; color:var(--dim); margin:2px 0 12px; max-width:66ch; line-height:1.5; }
.tl-note b { color:var(--fg); font-weight:600; }
.tl-grid { display:grid; grid-template-columns:minmax(8.5rem,auto) 1fr; gap:5px 12px;
  align-items:center; }
.tl-name { display:flex; align-items:center; gap:8px; justify-content:flex-end;
  font-size:12.5px; color:var(--fg); overflow:hidden; white-space:nowrap; }
.tl-name .tl-dot { width:9px; height:9px; border-radius:50%; flex:none; }
.tl-face { width:21px; height:21px; border-radius:50%; object-fit:cover; flex:none;
  background:rgba(127,127,127,.2); }
.tl-name.summary { color:var(--dim); font-family:var(--figure, monospace); font-size:10px;
  letter-spacing:.14em; text-transform:uppercase; }
.tl-lane { position:relative; height:26px; border-radius:5px;
  background:rgba(127,127,127,.12); overflow:hidden; }
.tl-lane.ov { background:rgba(127,127,127,.08); }
/* SPECIAL and ACTIVE ride in SEPARATE bands (top vs bottom of the lane) so the
   fact never paints over the chance and hides its real duration. */
.tl-bar { position:absolute; border-radius:3px; }
.tl-active { opacity:.9; }           /* hatched, member colour applied inline */
.tl-special { box-shadow:0 0 0 1px rgba(0,0,0,.28) inset; }  /* solid, member colour inline */
.tl-seg { position:absolute; top:0; bottom:0; }              /* Active-lane / Overlap slice */
.tl-cue { position:absolute; top:0; bottom:0; width:2px; background:var(--gold, #f5c451); opacity:.9; }
.tl-axis { position:relative; height:16px; margin-top:5px; }
.tl-tick { position:absolute; top:0; font-family:var(--figure, monospace); font-size:10px;
  color:var(--dim); transform:translateX(-50%); font-variant-numeric:tabular-nums; }
.tl-end { position:absolute; top:-2px; bottom:2px; width:1px; background:var(--line, #2a3140); }
.tl-key { display:flex; gap:16px; flex-wrap:wrap; font-size:11px; color:var(--dim); margin-top:10px; }
.tl-key i { display:inline-block; width:20px; height:9px; border-radius:2px; margin-right:5px;
  vertical-align:-1px; }
.tl-empty { font-size:12.5px; color:var(--dim); padding:8px 0; }
`;

function ensureStyle() {
  let style = document.getElementById(STYLE_ID);
  if (!style) {
    style = document.createElement("style");
    style.id = STYLE_ID;
    document.head.appendChild(style);
  }
  style.textContent = CSS;  // keep it current across a redesign
}

const pct = (value, span) => `${Math.max(0, Math.min(100, (value / span) * 100))}%`;
const chrOf = (cardId) => "chr-" + String(cardId).split("-")[1];
// A hatched fill of a member's colour — the "might happen" ACTIVE window.
const hatch = (c) =>
  `repeating-linear-gradient(135deg, ${c} 0 5px, ${c}44 5px 10px)`;

function bar(startSeconds, endSeconds, span, className, title, style) {
  const el = document.createElement("div");
  el.className = `tl-bar ${className}`;
  el.style.left = pct(startSeconds, span);
  const width = Math.max(0.4, ((endSeconds - startSeconds) / span) * 100);
  el.style.width = `${Math.min(width, 100)}%`;
  Object.assign(el.style, style || {});
  if (title) el.title = title;
  return el;
}

// `data` is /api/timeline's payload; `cards` maps id -> the ranking's card, for
// the member face (via its chr id) and a name fallback.
export function renderTimeline(host, data, cards) {
  ensureStyle();
  host.textContent = "";
  if (!data || !data.available) {
    const why = document.createElement("p");
    why.className = "tl-empty";
    why.textContent = data && data.reason ? `No timeline: ${data.reason}` : "No timeline for this song.";
    host.appendChild(why);
    return;
  }

  const t = data.timeline;
  let span = t.song_seconds || 1;
  for (const lane of t.lanes) {
    for (const w of lane.active) span = Math.max(span, w.end);
    for (const w of lane.special) span = Math.max(span, w.end);
  }

  const head = document.createElement("div");
  head.className = "tl-head";
  head.append(
    Object.assign(document.createElement("span"), {
      className: "tl-title", textContent: "When each member is live",
    }),
    Object.assign(document.createElement("span"), {
      className: "tl-sub",
      textContent: `${t.music_id} ${t.difficulty} · ${t.song_seconds.toFixed(1)}s · ${t.notes} notes`,
    }),
  );
  host.appendChild(head);

  const note = document.createElement("p");
  note.className = "tl-note";
  note.innerHTML =
    "Slot order is the one that scores highest on this chart, not the order you sent — " +
    "cue <em>i</em> fires slot <em>i</em>'s SPECIAL. In each row the <b>solid</b> SPECIAL " +
    "rides the top band and the <b>hatched</b> ACTIVE the bottom, so neither hides the " +
    "other's real duration. Solid will happen (the chart names the cue); hatched might " +
    "(the ACTIVE % is a lower bound).";
  host.appendChild(note);

  const grid = document.createElement("div");
  grid.className = "tl-grid";

  // Each window as {start, end, kind, magnitude, prob} per lane, plus its colour.
  const laneColor = (i) => LANE_COLORS[i % LANE_COLORS.length];

  t.lanes.forEach((lane, i) => {
    const color = laneColor(i);
    const name = document.createElement("div");
    name.className = "tl-name";
    const face = document.createElement("img");
    face.className = "tl-face";
    face.loading = "lazy";
    face.alt = "";
    face.src = "/chricon/" + encodeURIComponent(chrOf(lane.card_id));
    face.addEventListener("error", () => {
      const dot = document.createElement("span");
      dot.className = "tl-dot";
      dot.style.background = color;
      face.replaceWith(dot);
    });
    const label = document.createElement("span");
    label.textContent = lane.member || (cards && cards[lane.card_id] && cards[lane.card_id].member) || lane.card_id;
    name.append(face, label);
    name.title = `slot ${lane.slot} · ${lane.card_id}`;
    grid.appendChild(name);

    const track = document.createElement("div");
    track.className = "tl-lane";
    for (const w of lane.active) {
      const chance = w.probability_base == null
        ? "chance not stated" : `≥ ${(w.probability_base * 100).toFixed(0)}% per check`;
      track.appendChild(bar(w.start, w.end, span, "tl-active",
        `ACTIVE ${w.start.toFixed(1)}–${w.end.toFixed(1)}s · magnitude ${w.magnitude} · ${chance}`,
        { background: hatch(color), bottom: "3px", height: "9px" }));
    }
    for (const w of lane.special) {
      track.appendChild(bar(w.start, w.end, span, "tl-special",
        `SPECIAL ${w.start.toFixed(1)}–${w.end.toFixed(1)}s · magnitude ${w.magnitude} · fires on the chart's cue`,
        { background: color, top: "3px", height: "9px" }));
    }
    if (lane.cue != null) {
      const cue = document.createElement("div");
      cue.className = "tl-cue";
      cue.style.left = pct(lane.cue, span);
      cue.title = `SPECIAL cue at ${lane.cue.toFixed(2)}s`;
      track.appendChild(cue);
    }
    grid.appendChild(track);
  });

  // --- Active lane: every member's windows, in their own colour, so you see
  // WHO is live when (overlaps blend). ---
  grid.appendChild(Object.assign(document.createElement("div"),
    { className: "tl-name summary", textContent: "Active" }));
  const activeTrack = document.createElement("div");
  activeTrack.className = "tl-lane";
  t.lanes.forEach((lane, i) => {
    const color = laneColor(i);
    for (const w of [...lane.active, ...lane.special]) {
      const seg = bar(w.start, w.end, span, "tl-seg", "", { background: color, opacity: ".7" });
      activeTrack.appendChild(seg);
    }
  });
  grid.appendChild(activeTrack);

  // --- Overlap lane: how many members are live at once, sampled across the
  // song and shaded (darker = more stacked). ---
  grid.appendChild(Object.assign(document.createElement("div"),
    { className: "tl-name summary", textContent: "Overlap" }));
  const ovTrack = document.createElement("div");
  ovTrack.className = "tl-lane ov";
  const windows = [];
  for (const lane of t.lanes) for (const w of [...lane.active, ...lane.special]) windows.push(w);
  const SLICES = 160;
  const step = span / SLICES;
  let run = null;  // coalesce equal-count neighbours into one segment
  const flush = (endIndex) => {
    if (!run) return;
    if (run.count > 0) {
      const seg = document.createElement("div");
      seg.className = "tl-seg";
      seg.style.left = pct(run.start * step, span);
      seg.style.width = `${((endIndex - run.start) / SLICES) * 100}%`;
      // 1 member -> faint, 5 -> near-solid; a cool grey so it reads as heat.
      const a = Math.min(0.85, 0.14 + run.count * 0.16);
      seg.style.background = `rgba(150,160,190,${a.toFixed(3)})`;
      seg.title = `${run.count} member${run.count === 1 ? "" : "s"} live`;
      ovTrack.appendChild(seg);
    }
    run = null;
  };
  for (let s = 0; s < SLICES; s += 1) {
    const t0 = s * step;
    let count = 0;
    for (const w of windows) if (w.start <= t0 && t0 < w.end) count += 1;
    if (!run || run.count !== count) { flush(s); run = { start: s, count }; }
  }
  flush(SLICES);
  grid.appendChild(ovTrack);

  host.appendChild(grid);

  // The axis: ticks every 15 s, plus the song's own end (the reading that
  // matters when a window runs past it).
  const axisRow = document.createElement("div");
  axisRow.className = "tl-grid";
  axisRow.appendChild(document.createElement("div"));
  const axis = document.createElement("div");
  axis.className = "tl-axis";
  for (let s = 0; s <= span; s += 15) {
    const tick = document.createElement("span");
    tick.className = "tl-tick";
    tick.style.left = pct(s, span);
    tick.textContent = `${s}s`;
    axis.appendChild(tick);
  }
  const end = document.createElement("div");
  end.className = "tl-end";
  end.style.left = pct(t.song_seconds, span);
  end.title = `the song ends at ${t.song_seconds.toFixed(1)}s`;
  axis.appendChild(end);
  axisRow.appendChild(axis);
  host.appendChild(axisRow);

  const key = document.createElement("div");
  key.className = "tl-key";
  const solid = document.createElement("span");
  solid.innerHTML = "<i style='background:#7b78e6'></i>";
  solid.appendChild(document.createTextNode("SPECIAL — solid, will fire on the chart's cue"));
  const hatched = document.createElement("span");
  hatched.innerHTML = `<i style="background:${hatch("#7b78e6")}"></i>`;
  hatched.appendChild(document.createTextNode("ACTIVE — hatched, may fire; the % is a lower bound"));
  key.append(solid, hatched);
  host.appendChild(key);
}
