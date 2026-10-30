// Event page, in the order a player decides:
//   1. the target  - which reward tier, and the gap to it (forecast fans)
//   2. the plan    - the best team's Event Pt per Live on the best songs, then the Lives the gap costs
//   3. the team    - the six cards; only the five members earn Event Bonus, the leader adds nothing
//   4. reference   - the bonus rules and the account's bonus cards, as a sortable table
// Every figure comes from the server (/api/event, /api/event/songs, /api/event/cutoffs,
// POST /api/event/run); this file only lays it out and divides. API: tools/web/EVENT-API.md.
import { score } from "/hb-grade.js";
import { lineChart, wireLine, heatmap } from "/ev-chart.js";

const $ = s => document.querySelector(s);
const QUOTE = String.fromCharCode(34);   // spelled out: a quote inside a regex trips test_js_syntax
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", [QUOTE]: "&quot;" };
const ESC_RE = new RegExp("[&<>" + QUOTE + "]", "g");
const esc = s => String(s ?? "").replace(ESC_RE, c => ESC[c]);
const F = window.HBFmt;   // hb-format.js: one date format, exact numbers, one placeholder
const pct = permil => permil == null ? F.NA : `+${Math.round(permil / 10)}${F.SP}%`;
const fmt = n => n == null || !isFinite(n) ? F.NA : F.num(Math.round(+n));
// Event Pt are printed exact everywhere (no "3.67 M" beside "+1,909,260" in one panel)
const short = fmt;
const ago = s => s == null ? "" : F.ago(Date.now() - s * 1000);
const thumb = id => `/cardart/thumb/${encodeURIComponent(id)}?w=160`;
const FLOOR_PT = 1598;   // the everybody-who-played border (an estimate, EVENT-API.md Cutoffs)

const S = { ev: null, cut: null, songs: [], target: null, kind: null, region: "us", sort: { k: "eb", dir: -1 } };

// a network failure means the app's own local engine is not answering (stopped, or restarting
// after an update): say so instead of the browser's bare "Failed to fetch"
const ENGINE_DOWN = "The app's local engine is not answering (it may be restarting after an update). Close and reopen the app; if it keeps happening, see Troubleshooting in the docs.";
async function reach(url, opts) {
  try { return await fetch(url, opts); } catch (e) {
    if (e && e.name === "AbortError") throw e;
    throw new Error(ENGINE_DOWN);
  }
}
async function getJSON(url) {
  const r = await reach(url);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || body.message || `${r.status} ${r.statusText}`);
  return body;
}
async function postJSON(url, data) {
  const r = await reach(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || body.message || `${r.status}`);
  return body;
}
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("on");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("on"), 2600);
}
const face = (card, opts = {}) => window.HBCard.face({ id: card.id, member: card.member, type: card.type, rarity: card.rarity,
  level: card.level, bloom: card.bloom }, { w: 400, lead: !!opts.lead });
function genOf(chr) {
  const g = ((window.HB_GENS && window.HB_GENS.by) || {})[chr];
  return (g && g[0]) || { c: "#8C8BB8" };
}
const fmtDate = iso => F.date(iso);
function timeLeft(iso) {
  const ms = new Date(iso) - Date.now();
  if (ms <= 0) return null;
  const h = Math.floor(ms / 36e5), d = Math.floor(h / 24);
  return d ? [`${d}${F.SP}d ${h % 24}${F.SP}h`, "left"] : [`${h}${F.SP}h ${Math.floor(ms / 6e4) % 60}${F.SP}min`, "left"];
}
function eventName(key) {
  const m = /marathon_event-([a-z]+)-(\d+)/.exec(key || "");
  return m ? `${m[1][0].toUpperCase()}${m[1].slice(1)} event ${+m[2]}` : key;
}
// the five strongest DIFFERENT holomems among the account's cards: the ceiling of a team's Event Bonus
function bestFive(cards) {
  const seen = new Set(), pick = [];
  for (const c of [...cards].sort((a, b) => b.live_permil - a.live_permil)) {
    if (seen.has(c.chr)) continue;
    seen.add(c.chr); pick.push(c);
    if (pick.length === 5) break;
  }
  return pick;
}

/* ------------------------------------------------------------------ header */
function renderHead(ev) {
  const e = ev.event || {};
  const title = e.title || eventName(e.key) || "Current event";
  document.title = `${title} · Event · hololive builder`;
  $("#evtitle").textContent = title;
  const facts = [e.kind_label || "Event"];
  if (e.chapter != null) facts.push(`Chapter ${+e.chapter || e.chapter}`);
  if (e.starts_at && e.ends_at) facts.push(`${fmtDate(e.starts_at)} to ${fmtDate(e.ends_at)}`);
  if ((ev.open_events || []).length > 1) facts.push(`${ev.open_events.length} events open, showing the newest`);
  $("#evchips").innerHTML = facts.map(f => `<span>${esc(f)}</span>`).join("");
  renderChapters(ev);
  // always shown: it is also the way into the past events, art or not
  $("#banner").innerHTML = e.art ? `<img src="${esc(e.art)}?w=720" alt="">` : `<span class="noart">No banner</span>`;
  renderHeadStats();
}
// the head's stat tiles: time left, then your own rank and Pt once a cutoff read carries them
function renderHeadStats() {
  const e = (S.ev && S.ev.event) || {};
  const left = e.ends_at && timeLeft(e.ends_at);
  const tiles = [];
  if (e.ends_at) tiles.push(`<div class="stat-tile"><div><span class="lbl">${(S.ev && (S.ev.chapters || []).length > 1) ? "Chapter ends in" : "Time left"}</span><b class="n">${esc(left ? left[0] : "Ended")}</b><small>ends ${esc(fmtDate(e.ends_at))}</small></div></div>`);
  // a Spotlight event: its chapters last two days each, the event runs on (both are shown)
  const chs = (S.ev && S.ev.chapters) || [];
  const evEnd = chs.length > 1 ? chs[chs.length - 1].ends_at_ms : null;
  const evLeft = evEnd && timeLeft(new Date(evEnd).toISOString());
  if (evEnd) tiles.push(`<div class="stat-tile"><div><span class="lbl">Event ends in</span><b class="n">${esc(evLeft ? evLeft[0] : "Ended")}</b><small>chapter ${esc(String(e.chapter ? +e.chapter : "?"))} of ${chs.length} · ${esc(fmtDate(evEnd))}</small></div></div>`);
  const me = S.cut && S.cut.self;
  const ps = ptStanding(S.cut);
  if (me && me.rank) {
    const fin = finalSelf(S.cut);
    tiles.push(`<div class="stat-tile"><div><span class="lbl">Your rank (${esc(S.region.toUpperCase())})</span><b class="n">#${fmt(me.rank)}</b><small>${fin ? "final" : `read ${esc(ago((S.cut.now - me.captured_at) / 1000))}${S.cut.ended ? ", before the end" : ""}`}</small></div></div>`);
    if (!ps) tiles.push(`<div class="stat-tile"><div><span class="lbl">Your Event Pt</span><b class="n">${fmt(me.score)}</b><small>${fin ? "final" : "at that read"}</small></div></div>`);
  }
  if (ps) tiles.push(`<div class="stat-tile"><div><span class="lbl">Your final Event Pt</span><b class="n">${fmt(ps.pts)}</b><small>${ps.best ? `inside Top ${fmt(ps.best.rank)}` : "outside every tier"}</small></div></div>`);
  $("#evstats").innerHTML = tiles.join("");
}

/* ------------------------------------------------------------------ one tier's chart */
function openChart(rank) {
  S.chartRank = rank; S.chartPlayer = null;
  if (!$("#chartdlg").open) $("#chartdlg").showModal();
  renderChart();
}
function renderChart() {
  if (S.chartPlayer) { renderPlayerChart(); return; }
  const c = S.cut, t = c && (c.tiers || []).find(x => x.rank === S.chartRank);
  if (!t) return;
  const view = S.chartView || "line";
  $("#chartview").querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.v === view)));
  $("#charttitle").textContent = `Top ${fmt(t.rank)} cutoff${t.grade_name ? ` (${t.grade_name})` : ""}`;
  // "Show you": on the line, your own reads beside the border; on the heatmap, your own hourly
  // gains instead of the border's (the heatmap is the border as read, yours only on that
  // switch). display, not hidden: .ev-check's display overrides [hidden]
  const youOK = (c.self_series || []).length > 1 || (view === "line" && !!(c.self && c.self.score));
  $("#chartyoulbl").style.display = youOK ? "" : "none";
  const you = youOK && $("#chartyou").checked;
  const n = (t.series || []).length;
  $("#chartnote").textContent = `${n} read${n === 1 ? "" : "s"} of this border${t.prediction && t.prediction.method === "template" ? ", forecast from past events" : t.prediction && t.prediction.method === "pace" ? ", forecast from its pace (no past event yet)" : ""}`;
  const body = $("#chartbody");
  body.style.setProperty("--tc", t.color1 ? `#${t.color1}` : "#8C8BB8");
  body.innerHTML = view === "heat"
    ? heatmap(c, you ? { ...t, series: c.self_series } : t, { you, unit: isPts() ? "Event Pt" : c.kind === "total" ? "Total score" : "Song score" })
    : lineChart(c, t, { you });
  if (view === "line") wireLine(body.querySelector("svg"), $("#charttip"));
  else $("#charttip").hidden = true;
}

/* ------------------------------------------------------------------ top 100 */
// The chapter's board as the 24/7 tracker reads it (tools/recon/proxy/event_tracker.py, every
// couple of minutes on the VPS), with each player's last hour, modelled on the community tool
// holodori.best's leaderboard. A row opens that player's own line.
const TOP_COLS = [
  { k: "rank", lab: "#", v: r => r.rank, dir: 1 },
  { k: "name", lab: "Player", v: r => (r.name || "").toLowerCase(), dir: 1 },
  { k: "score", lab: "Event Pt", v: r => r.score, dir: -1 },
  { k: "gain_1h", lab: "Last hour", v: r => r.gain_1h ?? -1, dir: -1 },
  { k: "rank_1h", lab: "Rank, 1 h", v: r => r.rank_1h ?? -999, dir: -1 },
  { k: "pace_h", lab: "Pace / h (3 h)", v: r => r.pace_h ?? -1, dir: -1 },
  { k: "last_gain_at", lab: "Last gain", v: r => r.last_gain_at ?? 0, dir: -1 },
];
// Read ON DEMAND (only while someone is looking): every request renews the
// tracker's lease on this region, so the page asks again every minute while it is visible.
const TOP_REFRESH_MS = 60e3;
async function loadTop() {
  const region = S.region;
  const kind = S.cut && S.cut.kind && S.cut.kind !== "points" ? S.cut.kind : null;
  const r = await fetch(`/api/event/top?region=${region}${S.chapter ? `&chapter=${encodeURIComponent(S.chapter)}` : ""}${kind ? `&kind=${encodeURIComponent(kind)}` : ""}`);
  const body = await r.json().catch(() => ({}));
  if (region !== S.region || kind !== (S.cut && S.cut.kind && S.cut.kind !== "points" ? S.cut.kind : null)) return;   // moved on while in flight
  if (!r.ok || (kind && !(body.rows || []).length)) {
    S.top = null;
    if (kind && r.ok) {   // a song board not read yet
      $("#topsec").hidden = false;
      $("#h-top").textContent = `Top 100 · ${songTitle(kind)}`;
      $("#topnote").textContent = "";
      $("#top").innerHTML = `<tbody><tr><td class="ev-empty">This song's top 100 has not been read yet: the 24/7 tracker reads it while this page is open, so it appears within a few minutes.</td></tr></tbody>`;
      return;
    }
    $("#topsec").hidden = !body.watching;
    if (body.watching) {
      $("#topnote").textContent = "Asking the 24/7 tracker for this board…";
      $("#top").innerHTML = `<tbody><tr><td class="ev-empty">The first read of this region's top 100 arrives in about a minute.</td></tr></tbody>`;
    }
    return;
  }
  S.top = body;
  $("#topsec").hidden = false;
  renderTop();
}
function wireTopRefresh() {
  setInterval(() => { if (!document.hidden) loadTop(); }, TOP_REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) loadTop(); });
}
function renderTop() {
  const d = S.top; if (!d) return;
  const rows = d.rows || [];
  const song = String(d.kind || "").startsWith("music:");
  $("#h-top").textContent = song ? `Top 100 · ${songTitle(d.kind)}` : d.kind === "total" ? "Top 100 · Total Score" : "Top 100";
  TOP_COLS.find(x => x.k === "score").lab = song ? "Best score" : d.kind === "total" ? "Total score" : "Event Pt";
  $("#topnote").textContent = d.captured_at ? `Read ${F.ago(d.captured_at)} by the 24/7 tracker${d.final ? ". Final" : d.watching ? `, every 5${F.SP}min while this page is open` : ""}` : "";
  const sort = S.topSort || { k: "rank", dir: 1 };
  const col = TOP_COLS.find(x => x.k === sort.k) || TOP_COLS[0];
  const sorted = [...rows].sort((a, b) => { const x = col.v(a), y = col.v(b); return (x < y ? -1 : x > y ? 1 : 0) * sort.dir || a.rank - b.rank; });
  const aria = k => sort.k === k ? (sort.dir < 0 ? "descending" : "ascending") : "none";
  const delta = v => v == null ? `<span class="dim">${F.NA}</span>` : v > 0 ? `<span class="up">▲${v}</span>` : v < 0 ? `<span class="down">▼${-v}</span>` : `<span class="dim">=</span>`;
  const n = S.allTop ? sorted.length : 20;
  $("#top").innerHTML = `<thead><tr>${TOP_COLS.map(x => `<th scope="col" aria-sort="${aria(x.k)}"><button type="button" data-tsort="${x.k}">${x.lab}</button></th>`).join("")}</tr></thead>
    <tbody>${sorted.slice(0, n).map(r => `<tr data-uid="${esc(r.uid)}" title="Open ${esc(r.name || r.uid)}'s line">
      <td class="n">${r.rank}</td><td><b>${esc(r.name || F.NA)}</b> <small class="dim">${esc(r.uid || "")}</small></td>
      <td>${fmt(r.score)}</td><td>${r.gain_1h == null ? `<span class="dim">${F.NA}</span>` : r.gain_1h ? `+${fmt(r.gain_1h)}` : `<span class="dim">0</span>`}</td>
      <td>${delta(r.rank_1h)}</td><td class="n">${r.pace_h == null ? F.NA : fmt(r.pace_h)}</td>
      <td class="n">${r.last_gain_at ? F.ago(r.last_gain_at) : `<span class="dim">${d.first_read > d.captured_at - 36e5 ? "none read yet" : "not in the last hour"}</span>`}</td></tr>`).join("")}</tbody>
    ${sorted.length > 20 ? `<tfoot><tr><td colspan="${TOP_COLS.length}"><button type="button" class="btn small" data-alltop>${S.allTop ? "Show the top 20" : `Show all ${sorted.length}`}</button></td></tr></tfoot>` : ""}`;
}
async function openPlayer(uid) {
  const d = S.top; if (!d) return;
  const row = (d.rows || []).find(r => r.uid === uid);
  try {
    const p = await getJSON(`/api/event/top?region=${d.region}&chapter=${encodeURIComponent(d.chapter_id)}${d.kind ? `&kind=${encodeURIComponent(d.kind)}` : ""}&uid=${encodeURIComponent(uid)}`);
    S.chartPlayer = { uid, name: row ? row.name : uid, rank: row ? row.rank : null, series: (p.player || {}).series || [], top: p };
  } catch (e) { toast(e.message); return; }
  if (!$("#chartdlg").open) $("#chartdlg").showModal();
  renderChart();
}
function renderPlayerChart() {
  const P = S.chartPlayer, d = P.top;
  const view = S.chartView || "line";
  $("#chartview").querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.v === view)));
  $("#charttitle").textContent = `${P.name || P.uid}${P.rank ? ` (#${P.rank})` : ""}`;
  $("#chartyoulbl").style.display = "none";   // no "you" on another player's line
  const n = P.series.length;
  $("#chartnote").textContent = `${n} read${n === 1 ? "" : "s"} while in the top 100`;
  const c = { start_time: d.start_time || (P.series[0] || [Date.now()])[0], end_time: d.end_time || d.now, now: d.now, ended: d.final, self: null };
  const t = { rank: P.rank || 0, series: P.series, prediction: null };
  const body = $("#chartbody");
  body.style.setProperty("--tc", "#8C8BB8");
  body.innerHTML = view === "heat" ? heatmap(c, t, { unit: d.kind && d.kind !== "points" ? (String(d.kind).startsWith("music:") ? "Best score" : "Total score") : "Event Pt" }) : lineChart(c, t);
  if (view === "line") wireLine(body.querySelector("svg"), $("#charttip"));
  else $("#charttip").hidden = true;
}
function wireTop() {
  $("#top").addEventListener("click", e => {
    if (e.target.closest("[data-alltop]")) { S.allTop = !S.allTop; renderTop(); return; }
    const b = e.target.closest("[data-tsort]");
    if (b) {
      const k = b.dataset.tsort, col = TOP_COLS.find(x => x.k === k), cur = S.topSort || { k: "rank", dir: 1 };
      S.topSort = { k, dir: cur.k === k ? -cur.dir : col.dir };
      renderTop(); return;
    }
    const tr = e.target.closest("tr[data-uid]");
    if (tr && tr.dataset.uid) openPlayer(tr.dataset.uid);
  });
}

/* ------------------------------------------------------------------ chapters */
// A Spotlight event runs one chapter per holomem, in turn, each with its own Event Bonus
// holomem, bonus cards, event song and ranking: one tab per chapter, the
// running one first selected; a one-chapter event shows no tabs.
function renderChapters(ev) {
  const chs = ev.chapters || [];
  const box = $("#evchapters");
  if (!box) return;
  if (chs.length < 2) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  box.innerHTML = chs.map(c => {
    const who = (/\(([^)]+)\)\s*$/.exec(c.title || "") || [])[1] || `Chapter ${c.number}`;
    const when = c.starts_at_ms ? `${fmtDate(c.starts_at_ms)} – ${fmtDate(c.ends_at_ms)}` : "";
    return `<button type="button" data-chapter="${esc(c.key)}" aria-pressed="${c.key === S.chapter}" title="${esc(when)}">
      ${c.chr ? `<img src="/chricon/${esc(c.chr)}" alt="" onerror="this.remove()">` : `<b>${c.number}</b>`}<span>${esc(who)}</span>${c.now ? `<small>now</small>` : c.ends_at_ms && c.ends_at_ms < Date.now() ? `<small>ended</small>` : `<small>soon</small>`}</button>`;
  }).join("");
}
async function selectChapter(key) {
  if (!key || key === S.chapter) return;
  S.chapter = key;
  try { S.ev = await getJSON(`/api/event?event=${encodeURIComponent(key)}`); renderHead(S.ev); renderRules(S.ev); } catch (e) { toast(e.message); }
  try {
    const songs = (await getJSON(`/api/event/songs?boosts=3&event=${encodeURIComponent(key)}`)).songs || [];
    S.songs = songs.filter(s => s.chart_available !== false && s.length_permil != null);
  } catch (e) { S.songs = []; }
  loadCutoffs().then(loadTop);
  renderMine();
}

/* ------------------------------------------------------------------ past events */
// the current event stays the page; its banner opens every event on record
async function openHistory() {
  const dlg = $("#histdlg");
  $("#histbody").innerHTML = `<p class="ev-empty">Reading the event history…</p>`;
  $("#histnote").textContent = "";
  if (!dlg.open) dlg.showModal();
  let doc;
  try { doc = await getJSON("/api/event/history"); }
  catch (e) { $("#histbody").innerHTML = `<p class="ev-empty">The history could not be read: ${esc(e.message)}</p>`; return; }
  const evs = doc.events || [];
  const nowKey = S.ev && S.ev.event && S.ev.event.key;
  $("#histnote").textContent = evs.length ? `${evs.length} event${evs.length === 1 ? "" : "s"} on record` : "";
  $("#histbody").innerHTML = evs.length ? evs.map(e => histRow(e, nowKey)).join("")
    : `<p class="ev-empty">No event on record yet: the refresh starts the history.</p>`;
}
// one chapter: your Event Pt, where you finished, the tier, and every final border with you placed
// among them
const fmtK = v => v >= 1e6 ? `${(v / 1e6).toFixed(2)}${F.SP}M` : v >= 1e3 ? `${Math.round(v / 1e3)}${F.SP}K` : fmt(v);
// one row per chapter, kept compact so it reads at a glance:
// medal | chapter | final rank | Event Pt | tier; the borders and the song ranking fold under it
function histChapter(ch) {
  const g = ch.reached, st = ch.standing, pts = (ch.ranking || "points") === "points";
  const col = (lbl, big, small = "") => `<div class="c"><span class="hl">${lbl}</span><b class="n">${big}</b>${small ? `<small>${small}</small>` : ""}</div>`;
  const na = `<span class="mute">${F.NA}</span>`;
  let rank;
  if (st && st.rank) rank = col("Final rank", `#${fmt(st.rank)}`);
  else if (st && st.best) rank = col("Final rank", st.from_rank > 1 ? `#${fmt(st.from_rank)}–${fmt(st.best)}` : `Top ${fmt(st.best)}`,
    st.how === "the emblem you hold" ? "from your medal" : "from your final Event Pt");
  else if (st && st.outside) rank = col("Final rank", `<span class="mute">Outside</span>`, `below Top ${fmt(st.from_rank - 1)}`);
  else if (ch.rank) rank = col(ch.final ? "Final rank" : "Rank", `#${fmt(ch.rank)}`, ch.final ? "" : `before the end`);
  else if (ch.outside) rank = col("Final rank", `<span class="mute">Outside</span>`, "below every tier");
  else rank = col("Final rank", na, "not on record");
  const pt = col(pts ? "Event Pt" : "Total score", ch.event_pt != null ? fmt(ch.event_pt) : na,
    ch.event_pt == null ? (ch.from_emblem ? "not kept this far back" : "not read") : ch.event_pt_final || !pts ? "" : "so far");
  const tier = g ? `<div class="c"><span class="hl">Tier</span><span class="ev-grade" style="--tc:#${esc(g.color1 || "8C8BB8")}"><i></i>${esc(g.grade_name || g.grade_id)}</span>${!st && !ch.final ? `<small>at the last read</small>` : ""}${st && st.next ? `<small class="short">+${fmt(st.next.need)} short of Top ${fmt(st.next.rank)}</small>` : ""}</div>`
    : col("Tier", `<span class="mute">None</span>`);
  const m = g && (st || ch.final) ? medal(ch.chapter_id, g.grade_id, "ev-medal") : "";   // no medal from a read before the end
  const name = `<div class="who"><b>Chapter ${ch.number || "?"}</b>${ch.title ? `<small>${esc(ch.title)}</small>` : ""}</div>`;

  const scored = (ch.borders || []).filter(b => b.score != null).sort((a, b) => a.rank - b.rank);
  const you = ch.event_pt_final && pts ? ch.event_pt : null;
  const chips = list => list.map(b => `<span class="${g && g.grade_id === b.grade_id ? "mine" : ""}" style="--tc:#${esc(b.color1 || "8C8BB8")}"><i></i><em>Top ${fmt(b.rank)}</em>${fmtK(b.score)}</span>`);
  let strip = "";
  if (scored.length) {
    const items = chips(scored);
    if (you != null) { const at = scored.findIndex(b => you >= b.score); items.splice(at < 0 ? items.length : at, 0, `<span class="you"><i></i>You ${fmtK(you)}</span>`); }
    strip = `<div class="ev-hborders"><span class="lbl">${ch.borders_final ? "Final borders" : "Borders"}${pts ? "" : " · total score"}</span>${items.join("")}</div>`;
  }
  const songs = (ch.songs || []).map(sg => {
    const res = sg.rank ? `#${fmt(sg.rank)} · ${fmtK(sg.score)}` : sg.outside ? "Outside" : F.NA;
    const bs = chips((sg.borders || []).filter(b => b.score != null).sort((a, b) => a.rank - b.rank)).join("");
    return `<div class="ev-hborders"><span class="lbl">Song Score · ${esc(sg.title || sg.music_id)} · <b>${res}</b></span>${bs}</div>`;
  }).join("");
  const more = strip || songs ? `<details class="ev-hdet"><summary>Borders${songs ? " and song ranking" : ""}</summary>${strip}${songs}</details>` : "";
  return `<div class="ev-hline"><div class="md">${m || `<div class="nomd"></div>`}</div>${name}${rank}${pt}${tier}</div>${more}`;
}
function histRow(e, nowKey) {
  const art = e.banner ? `<img src="/eventart/${esc(e.banner)}?w=480" alt="" loading="lazy">` : `<div class="noart"></div>`;
  const when = e.starts_at && e.ends_at ? `${fmtDate(e.starts_at)} to ${fmtDate(e.ends_at)}` : "dates not on record";
  const chs = e.chapters || [];
  const body = chs.map(histChapter).join("") || `<p class="meta">Not started or no chapter on record yet.</p>`;
  return `<article class="ev-hrow${e.event_id === nowKey ? " now" : ""}">
    <header><div class="art">${art}</div><div><h3>${esc(e.title || eventName(e.event_id))}</h3>
      <p class="meta">${esc(e.kind_label || "Event")} · ${esc(when)}${e.event_id === nowKey ? " · current event" : ""}</p></div></header>
    <div class="ev-hchs">${body}</div></article>`;
}

/* ------------------------------------------------------------------ 1. target */
const tierOK = t => t.filled && t.score > FLOOR_PT;
function defaultTarget(c) {
  const ok = (c.tiers || []).filter(tierOK);
  const me = c.self && c.self.rank;
  if (me) {   // the tightest tier you are not inside yet; if you are inside them all, the top one
    const above = ok.filter(t => t.rank < me).sort((a, b) => b.rank - a.rank)[0];
    const inside = ok.filter(t => t.rank >= me).sort((a, b) => a.rank - b.rank)[0];
    return (above || inside || ok[0] || {}).rank ?? null;
  }
  return (ok[Math.floor(ok.length / 2)] || {}).rank ?? null;
}
const finalOf = t => t.prediction ? t.prediction.final : t.score;
function gapOf(c, t) {   // Pt you still need to reach this tier's forecast border (and its range)
  const mine = (c.self && c.self.score) || 0, p = t.prediction;
  const f = finalOf(t);
  return { mid: f - mine, low: (p && p.low != null ? p.low : f) - mine, high: (p && p.high != null ? p.high : f) - mine };
}
// the event is over: the account's last read is its FINAL standing only if made after the end
// (an earlier read would judge an ended event on a read from part-way through)
const finalSelf = c => !!(c.ended && c.self && c.self.score && c.self.captured_at >= c.end_time);
// read after the end and the board did not have you: outside every tier (a song ranking you
// never placed in answers this way)
const finalOut = c => !!(c.ended && c.self && c.self.out_of_range && c.self.captured_at >= c.end_time);
const rankName = c => String(c.kind || "").startsWith("music:") ? `Song Score, ${songTitle(c.kind)}` : c.kind === "total" ? "Total Score" : "";
// no rank read after the end, but the Event Pt was (Marathon/Top answers through the tally):
// place it among the borders -- the final ones when read, else the last ones before the end
// (it shows where you ended up according to your points)
function ptStanding(c) {
  const op = c && c.own_points;
  if (!c || c.kind && c.kind !== "points" || !c.ended || finalSelf(c) || !op || op.captured_at < c.end_time) return null;
  const ts = (c.tiers || []).filter(t => finalOf(t) != null).sort((a, b) => a.rank - b.rank);
  const best = ts.find(t => op.value >= finalOf(t)) || null;
  const above = best ? ts.filter(t => t.rank < best.rank).pop() : ts[ts.length - 1];
  const fin = ts.length && ts.every(t => t.prediction && t.prediction.method === "final");
  return { pts: op.value, best, from: best ? (above ? above.rank + 1 : 1) : (above ? above.rank + 1 : null), fin };
}
const tierReached = c => {
  const me = c.self && c.self.rank;
  return me ? (c.tiers || []).filter(t => t.rank >= me).sort((a, b) => a.rank - b.rank)[0] || null : null;
};
// the chapter's ranking medal as the game draws it (event_emblems.py -> /eventart/emblem-<chapter>-<grade>.webp)
const medal = (chapterId, gradeId, cls = "ev-medal") => chapterId && gradeId
  ? `<img class="${cls}" src="/eventart/emblem-${esc(chapterId)}-${esc(String(gradeId).replace(/^marathon_ranking_grade-/, ""))}.webp" alt="" onerror="this.remove()">` : "";
// where you ended an ended chapter: {pts, tier (the tier you finished in, or null), exact rank or null}
function finish(c) {
  if (finalSelf(c)) return { pts: c.self.score, tier: tierReached(c), rank: c.self.rank };
  const ps = ptStanding(c);
  return ps ? { pts: ps.pts, tier: ps.best, rank: null, from: ps.from } : null;
}

function fan(c, t, pace) {
  const W = 640, H = 96, PX = 6, PY = 8;
  const p = t.prediction;
  const band = (p && p.band) || [];
  const ys = [t.score, ...(t.series || []).map(q => q[1]), ...band.map(b => b[2]), ...((p && p.points) || []).map(q => q[1])];
  // the tier sets the scale, plus where you are now -- not your projected END, which on a low
  // tier would flatten the border's curve into the floor. Your line is clipped at the top edge
  // instead, and so is your dot when you are far above the tier.
  const tierMax = Math.max(1, ...ys);
  if (pace && pace.now <= tierMax * 1.5) ys.push(pace.now);
  const ymax = Math.max(1, ...ys) * 1.06;
  const x = ms => PX + (W - 2 * PX) * Math.min(1, Math.max(0, (ms - c.start_time) / (c.end_time - c.start_time)));
  const y = v => H - PY - (H - 2 * PY) * (v / ymax);
  const pts = a => a.map(q => `${x(q[0]).toFixed(1)},${y(q[1]).toFixed(1)}`).join(" ");
  const area = band.length ? `<polygon class="fan" points="${band.map(b => `${x(b[0]).toFixed(1)},${y(b[2]).toFixed(1)}`).join(" ")} ${[...band].reverse().map(b => `${x(b[0]).toFixed(1)},${y(b[1]).toFixed(1)}`).join(" ")}"/>` : "";
  const mid = p && p.points && p.points.length ? `<polyline class="mid" points="${pts(p.points)}"/>` : "";
  const anch = [[c.start_time, 0], ...(t.series || []).filter(q => q[0] >= c.start_time)];   // every border starts at 0
  const past = anch.length > 1 ? `<polyline class="past" points="${pts(anch)}"/>` : "";
  const dots = (t.series || []).length > 12 ? "" : (t.series || []).map(q => `<line class="dot" x1="${x(q[0]).toFixed(1)}" y1="${y(q[1]).toFixed(1)}" x2="${x(q[0]).toFixed(1)}" y2="${y(q[1]).toFixed(1)}"/>`).join("");
  const youEnd = !pace ? null : pace.end <= ymax ? [c.end_time, pace.end]
    : [c.start_time + (c.end_time - c.start_time) * ymax / pace.end, ymax];
  const you = pace ? `<line class="you" x1="${x(c.start_time)}" y1="${y(0)}" x2="${x(youEnd[0]).toFixed(1)}" y2="${y(youEnd[1]).toFixed(1)}"/>
      <line class="youdot" x1="${x(pace.at).toFixed(1)}" y1="${y(Math.min(pace.now, ymax)).toFixed(1)}" x2="${x(pace.at).toFixed(1)}" y2="${y(Math.min(pace.now, ymax)).toFixed(1)}"/>` : "";
  const nowX = x(c.now).toFixed(1);
  return `<svg class="ev-fan" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Top ${fmt(t.rank)}: border so far as dots, forecast range to the end of the event${pace ? ", and your pace as a line" : ""}">
    <line class="base" x1="${PX}" x2="${W - PX}" y1="${H - PY}" y2="${H - PY}"/>
    <line class="now" x1="${nowX}" x2="${nowX}" y1="${PY}" y2="${H - PY}"/>
    ${area}${mid}${past}${you}${dots}</svg>`;
}

function renderCutoffs() {
  const c = S.cut;
  if (!c) return;
  $("#cutage").textContent = c.captured_at
    ? `Read ${ago(c.age_s)}, ${c.snapshots} read${c.snapshots === 1 ? "" : "s"}, ${Math.round((c.elapsed_fraction || 0) * 100)}% of the event gone` : "";
  if (!c.snapshots) {   // a ranking with no read yet (a song ranking before its first one)
    const what = rankName(c) || "This ranking";
    $("#tiers").innerHTML = `<p class="ev-empty">${esc(what)}: not read yet. ${c.ended
      ? "Its final borders are read when the chapter's results come out."
      : "The tracker reads it every 5 minutes; the first read appears here."}</p>`;
    $("#evgap").innerHTML = "";
    return;
  }
  if (S.target == null || !(c.tiers || []).some(t => t.rank === S.target)) S.target = defaultTarget(c);
  const pace = null;   // the player's own line lives in the tier chart ("Show you"), not the mini fan
  $("#tiers").innerHTML = (c.tiers || []).map(t => {
    const g1 = t.color1 ? `#${t.color1}` : "#8C8BB8";
    const name = `<div class="tn"><i style="--tc:${g1}"></i><b>Top ${fmt(t.rank)}</b><small>${esc(t.grade_name || "")}</small></div>`;
    if (!tierOK(t)) {
      const why = !t.filled ? `Fewer than ${fmt(t.rank)} players so far: anyone who plays is inside.`
        : "At the floor: everyone who played one Live has this. No forecast yet.";
      return `<div class="ev-tier quiet" role="listitem">${name}<div class="cur"><span class="lbl">Border now</span><b class="n">${t.filled ? fmt(t.score) : F.NA}</b></div><p class="why">${why}</p></div>`;
    }
    const p = t.prediction, gp = gapOf(c, t);
    const sel = t.rank === S.target;
    const range = p && p.low != null && p.method !== "final" && !c.ended ? `<small>${short(p.low)} to ${short(p.high)}</small>` : "";
    let verdict;
    if (c.ended) {
      const fin = finish(c);
      if (!fin && finalOut(c)) verdict = `<b class="n one">Outside</b><small>you are not on this board</small>`;
      else if (!fin) verdict = `<b class="n mute">${F.NA}</b><small>${c.self && c.self.score ? "your last read is from before the end" : "your rank was not read"}</small>`;
      else if (fin.tier && fin.tier.rank === t.rank) verdict = `<b class="n ok one">You finished here</b><small>${fin.rank ? `#${fmt(fin.rank)}` : `#${fmt(fin.from)}–${fmt(t.rank)}`} · +${short(fin.pts - finalOf(t))} over it</small>`;
      else if (fin.pts >= finalOf(t)) verdict = `<b class="n one">Cleared by +${short(fin.pts - finalOf(t))}</b>`;
      else verdict = `<b class="n one">Outside by ${short(finalOf(t) - fin.pts)}</b>`;
    } else if (!c.self || !c.self.score) verdict = `<b class="n">${short(finalOf(t))}</b><small>${unit()} to finish inside</small>`;
    else if (gp.high <= 0) verdict = `<b class="n ok">Inside</b><small>you are above the whole fan</small>`;
    else verdict = `<b class="n">+${short(Math.max(0, gp.mid))}</b><small>${gp.low <= 0 ? "inside the low end" : `range +${short(gp.low)} to +${short(gp.high)}`}</small>`;
    return `<button type="button" class="ev-tier" role="listitem" aria-pressed="${sel}" data-rank="${t.rank}" style="--tc:${g1}">
        ${name}
        <div class="cur"><span class="lbl">${c.ended ? "Last border read" : "Border now"}</span><b class="n">${fmt(t.score)}</b><small>${c.ended ? esc(fmtDate(t.captured_at)) : t.rate_per_hour != null ? `+${fmt(t.rate_per_hour)} ${unit()}/h` : "one read, no pace yet"}</small></div>
        ${fan(c, t, pace)}
        <div class="fc"><span class="lbl">${c.ended || (p && p.method === "final") ? "Final" : "Forecast"}</span><b class="n">${fmt(finalOf(t))}</b>${range}</div>
        <div class="gap"><span class="lbl">${c.ended ? "Your result" : "You need"}</span>${verdict}</div>
      </button>`;
  }).join("") || `<p class="ev-empty">No tiers in this read.</p>`;
  renderGap();
  renderHeadStats();
}
function renderGap() {
  const c = S.cut;
  const t = c && (c.tiers || []).find(x => x.rank === S.target);
  if (!c || !t) { $("#evgap").innerHTML = `<p class="ev-empty">No cutoffs read for this region yet.</p>`; return; }
  if (c.ended) {
    const won = finalSelf(c) ? tierReached(c) : null;
    const ps = ptStanding(c);
    const head = !finalSelf(c) && ps
      ? `<b class="n">${ps.best ? (ps.from > 1 ? `#${fmt(ps.from)}–${fmt(ps.best.rank)}` : `Top ${fmt(ps.best.rank)}`) : `Below Top ${fmt((ps.from || 1) - 1)}`}</b><small>${ps.best ? `inside Top ${fmt(ps.best.rank)}${ps.best.grade_name ? ` ${esc(ps.best.grade_name)}` : ""}` : "outside every tier"}: your final ${fmt(ps.pts)} Event Pt against the ${ps.fin ? "final" : "last read"} borders${ps.fin ? "" : " (the final ones are not out yet)"}; your exact rank is read once the chapter's results are out</small>`
      : finalSelf(c)
      ? `<b class="n">#${fmt(c.self.rank)}</b><small>${won ? `Top ${fmt(won.rank)}${won.grade_name ? ` ${esc(won.grade_name)}` : ""}` : "outside every tier"}: final, read ${esc(fmtDate(c.self.captured_at))}</small>`
      : finalOut(c)
      ? `<b class="n">Outside</b><small>${rankName(c) ? `${esc(rankName(c))}: ` : ""}you finished outside every tier; final, read ${esc(fmtDate(c.self.captured_at))}</small>`
      : `<b class="n mute">${F.NA}</b><small>The event has ended and your rank was not read after its end${c.self && c.self.rank ? ` (last read #${fmt(c.self.rank)}, ${esc(fmtDate(c.self.captured_at))})` : ""}.</small>`;
    const fin = finish(c);
    const m = fin && fin.tier ? medal(c.chapter_id, fin.tier.grade_id, "ev-medal big") : "";
    $("#evgap").innerHTML = (m ? `<div class="ev-medalbox">${m}<div><span class="ev-mlbl">Your medal</span><b class="n">${esc(fin.tier.grade_name || "")}</b><small>${fin.rank ? `#${fmt(fin.rank)}, final` : `#${fmt(fin.from)}–${fmt(fin.tier.rank)} from your final Event Pt${ptStanding(c) && !ptStanding(c).fin ? " (final borders not out yet)" : ""}`}</small></div></div>` : `<div class="stat-tile prime"><div><span class="lbl">Event ended</span>${head}</div></div>`)
      + (!finalSelf(c) && ps ? `<div class="stat-tile"><div><span class="lbl">Final Event Pt</span><b class="n">${fmt(ps.pts)}</b><small>read ${esc(fmtDate(c.own_points.captured_at))}, after the end</small></div></div>` : "")
      + (finalSelf(c) ? `<div class="stat-tile"><div><span class="lbl">Final Event Pt</span><b class="n">${fmt(c.self.score)}</b><small>${won ? `Top ${fmt(won.rank)} border ${fmt(finalOf(won))}` : "outside every tier"}</small></div></div>` : "");
    return;
  }
  const gp = gapOf(c, t);
  const me = c.self && c.self.rank ? "" : `<div class="stat-tile"><div><span class="lbl">You</span><b class="n mute">${F.NA}</b><small>your rank is not read in this region</small></div></div>`;
  const head = gp.high <= 0
    ? `<b class="n ok">Inside</b><small>Top ${fmt(t.rank)} forecast ${fmt(finalOf(t))}: you are above its whole range</small>`
    : `<b class="n">+${fmt(Math.max(0, gp.mid))}</b><small>${unit()} to hold Top ${fmt(t.rank)}: its border is forecast at ${fmt(finalOf(t))}${gp.low !== gp.high ? ` (${short(finalOf(t) + gp.low - gp.mid)} to ${short(finalOf(t) + gp.high - gp.mid)})` : ""}</small>`;
  $("#evgap").innerHTML = `<div class="stat-tile prime"><div><span class="lbl">Gap to target</span>${head}</div></div>${me}`;
}

// which ranking the page shows: Event Points (a
// Score Challenge: Total Score) or Song Score, one per song of the chapter -- kind points / total /
// music:<id> of the same /api/event/cutoffs
const isPts = () => ((S.cut && S.cut.kind) || "points") === "points";
const unit = () => isPts() ? "Pt" : "score";
const songTitle = k => { const mid = String(k).slice(6), s = (S.songs || []).find(x => x.music_id === mid); return s ? s.title : mid; };
function renderKinds() {
  const ks = (S.cut && S.cut.kinds) || [], cur = (S.cut && S.cut.kind) || "points";
  const main = ks.filter(k => !String(k).startsWith("music:")), songs = ks.filter(k => String(k).startsWith("music:"));
  const btn = (v, label, on) => `<button type="button" data-v="${esc(v)}" aria-pressed="${on}">${esc(label)}</button>`;
  const seg = main.map(k => btn(k, k === "total" ? "Total Score" : "Event Points", cur === k))
    .concat(songs.length ? [btn(songs.includes(cur) ? cur : songs[0], "Song Score", cur.startsWith("music:"))] : []);
  $("#rkind").innerHTML = seg.length > 1 ? seg.join("") : "";
  const row = $("#rsongs");
  row.hidden = !(cur.startsWith("music:") && songs.length > 1);
  row.innerHTML = row.hidden ? "" : songs.map(k => `<button type="button" class="ev-songpick" data-v="${esc(k)}" aria-pressed="${k === cur}">
      <img src="/jacket/${esc(k.slice(6))}" alt="" loading="lazy"><span>${esc(songTitle(k))}</span></button>`).join("");
}
async function loadCutoffs() {
  const q = `region=${S.region}${S.chapter ? `&chapter=${encodeURIComponent(S.chapter)}` : ""}`;
  try {
    let c = await getJSON(`/api/event/cutoffs?${q}${S.kind ? `&kind=${encodeURIComponent(S.kind)}` : ""}`);
    if (S.kind && !(c.kinds || []).includes(S.kind)) { S.kind = null; c = await getJSON(`/api/event/cutoffs?${q}`); }
    S.cut = c; renderKinds(); renderCutoffs();
  }
  catch (e) {
    S.cut = null; $("#cutage").textContent = ""; $("#rkind").innerHTML = ""; $("#rsongs").hidden = true;
    $("#tiers").innerHTML = "";
    renderGap();
  }
}
function wireCutoffs() {
  const seg = $("#region");
  seg.addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b) return;
    S.region = b.dataset.v; S.target = null;
    seg.querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    loadCutoffs().then(loadTop);
  });
  const pickKind = e => {
    const b = e.target.closest("button[data-v]"); if (!b || b.dataset.v === (S.cut && S.cut.kind)) return;
    S.kind = b.dataset.v; S.target = null; loadCutoffs().then(loadTop);
  };
  $("#rkind").addEventListener("click", pickKind);
  $("#rsongs").addEventListener("click", pickKind);
  $("#tiers").addEventListener("click", e => {
    const b = e.target.closest("button.ev-tier"); if (!b) return;
    S.target = +b.dataset.rank; renderCutoffs();
    openChart(S.target);   // a tier opens its chart
  });
  $("#chartview").addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b) return;
    S.chartView = b.dataset.v; renderChart();
  });
  $("#chartyou").onchange = () => renderChart();
  $("#chartclose").onclick = () => $("#chartdlg").close();
  $("#chartdlg").addEventListener("click", e => { if (e.target === e.currentTarget) e.currentTarget.close(); });
}

// the server's poller keeps the store current (on at server start); the page re-reads it every minute
setInterval(() => { if (!document.hidden) loadCutoffs(); }, 60_000);

/* ------------------------------------------------------------------ 4. reference */
function renderRules(ev) {
  const live = ev.live || {};
  $("#mems").innerHTML = (live.holomems || []).map(m => `<span class="ev-mem"><i style="--gc:${genOf(m.character_id).c}"></i>${esc(m.name)} <b class="eb">${pct(m.permil)}</b></span>`).join("")
    || `<p class="ev-empty">No holomem bonus in this event.</p>`;
  renderBloom(live.rarity_bloom || {});
  const cards = live.cards || [];
  const own = cards.filter(c => c.owned), miss = cards.filter(c => !c.owned);
  const tile = c => `<div class="ev-ec${c.owned ? "" : " missing"}"><img src="${thumb(c.id)}" alt="" loading="lazy">
      <span><b>${esc(c.member)}</b><small>${esc(c.costume_name || "")}, ${c.rarity}★</small></span><b class="eb">${pct(c.live_permil ?? c.permil)}</b></div>`;
  $("#evcards").innerHTML = !cards.length ? `<p class="ev-empty">No event cards.</p>`
    : `${own.length ? `<div class="ev-ecs">${own.map(tile).join("")}</div>` : `<p class="ev-note">You own none of the ${cards.length} event cards.</p>`}
       ${miss.length ? `<details class="ev-more"><summary>Show the ${miss.length} you do not own</summary><div class="ev-ecs">${miss.map(tile).join("")}</div></details>` : ""}`;
  const bySong = new Map();
  for (const row of ev.music || []) {
    if (!bySong.has(row.music_id)) bySong.set(row.music_id, { music_id: row.music_id, title: row.title, cards: [] });
    if (row.card) bySong.get(row.music_id).cards.push({ ...row.card, song_permil: row.permil });
  }
  // every song of the event (`ev.songs`, with group / solo), its bonus cards from the rows; an
  // older payload without `songs` lists the songs that have bonus rows
  const music = (ev.songs && ev.songs.length ? ev.songs : [...bySong.values()])
    .map(s => ({ ...s, cards: (bySong.get(s.music_id) || { cards: [] }).cards }));
  // the game takes the LARGEST bonus among the five members (never a sum), and only an
  // event card of the row counts: the leader (an outfit) and the singers' other cards do not
  const songRule = m => {
    if (!m.cards.length) return "No Song Score Bonus on this song. It still earns Event Pt.";
    const top = Math.max(...m.cards.map(c => +c.song_permil || 0));
    const owned = m.cards.filter(c => c.owned).length;
    const how = m.cards.length > 1
      ? `${pct(top)} Event Song Score when any one of your five members is one of these cards. One is enough: more do not stack.`
      : `${pct(top)} Event Song Score when this card is one of your five members.`;
    return `${how} The leader and the singers' other cards do not count. You own ${owned} of ${m.cards.length}.`;
  };
  const kind = m => m.singer_type === "group" ? "Group song" : m.singer_type === "solo" ? "Solo song" : "";
  $("#musicpanel").hidden = !music.length;
  $("#music").innerHTML = music.map(m => `<div class="ev-music"><img src="/jacket/${esc(m.music_id)}" alt="">
      <div><b>${esc(m.title || m.music_id)}</b><small>${esc([kind(m), m.singer].filter(Boolean).join(" · "))}</small><small>${esc(songRule(m))}</small></div>
      <div class="ev-mems">${m.cards.map(c => `<span class="ev-mem${c.owned ? "" : " missing"}">${esc(c.member)} <b class="eb">${pct(c.song_permil)}</b></span>`).join("")}</div></div>`).join("");
}
function renderBloom(table) {
  const rar = Object.keys(table).sort();
  if (!rar.length) { $("#bloomt").innerHTML = `<tr><td class="ev-empty">No bloom bonus.</td></tr>`; return; }
  const blooms = [1, 2, 3, 4, 5];
  const max = Math.max(...rar.flatMap(r => blooms.map(b => +(table[r][b] || 0))));
  $("#bloomt").innerHTML = `<thead><tr><th scope="col">Rarity</th>${blooms.map(b => `<th scope="col">Bloom ${b}</th>`).join("")}</tr></thead><tbody>` +
    rar.map(r => `<tr><th scope="row">${r}★</th>${blooms.map(b => {
      const v = table[r][b]; return `<td class="n${+v === max ? " max" : ""}">${v ? pct(v) : F.NA}</td>`;
    }).join("")}</tr>`).join("") + "</tbody>";
}
const COLS = [
  { k: "card", lab: "Card", v: c => `${c.member} ${c.costume_name}` },
  { k: "rarity", lab: "Rarity", v: c => c.rarity },
  { k: "level", lab: "Level", v: c => c.level ?? -1 },
  { k: "bloom", lab: "Bloom", v: c => c.bloom ?? 0 },
  { k: "lb", lab: "Limit Break", v: c => c.limit_break ?? 0 },
  { k: "eb", lab: "Event Bonus", v: c => c.live_permil || 0 },
];
function renderMine() {
  const owned = (S.ev && S.ev.owned_bonus_cards) || [];
  const five = bestFive(owned);
  $("#minenote").innerHTML = owned.length ? `${owned.length} cards. Best five members together: <b class="eb">${pct(five.reduce((t, c) => t + c.live_permil, 0))}</b>` : "";
  if (!owned.length) { $("#mine").innerHTML = `<tbody><tr><td class="ev-empty">None of your cards carries a bonus in this event.</td></tr></tbody>`; return; }
  const col = COLS.find(x => x.k === S.sort.k) || COLS[5];
  const rows = [...owned].sort((a, b) => { const x = col.v(a), y = col.v(b); return (x < y ? -1 : x > y ? 1 : 0) * S.sort.dir || b.live_permil - a.live_permil; });
  const aria = k => S.sort.k === k ? (S.sort.dir < 0 ? "descending" : "ascending") : "none";
  $("#mine").innerHTML = `<thead><tr>${COLS.map(x => `<th scope="col" aria-sort="${aria(x.k)}"><button type="button" data-sort="${x.k}">${x.lab}</button></th>`).join("")}</tr></thead>
    <tbody>${rows.slice(0, S.allMine ? rows.length : 12).map(c => {
      return `<tr><td><span class="ev-who"><img src="${thumb(c.id)}" alt="" loading="lazy"><span><b>${esc(c.member)}</b><small>${esc(c.costume_name || "")}</small></span></span></td>
        <td class="n">${c.rarity}★</td><td class="n">${c.level ?? F.NA}</td><td class="n">${c.bloom ?? 0}</td><td class="n">${c.limit_break ?? 0}</td>
        <td><b class="eb">${pct(c.live_permil)}</b></td></tr>`;
    }).join("")}</tbody>${rows.length > 12 ? `<tfoot><tr><td colspan="${COLS.length}"><button type="button" class="btn small" data-all>${S.allMine ? "Show the top 12" : `Show all ${rows.length}`}</button></td></tr></tfoot>` : ""}`;
}
function wireMine() {
  $("#mine").addEventListener("click", e => {
    if (e.target.closest("[data-all]")) { S.allMine = !S.allMine; renderMine(); return; }
    const b = e.target.closest("[data-sort]"); if (!b) return;
    const k = b.dataset.sort;
    S.sort = { k, dir: S.sort.k === k ? -S.sort.dir : (k === "card" ? 1 : -1) };
    renderMine();
  });
}

/* ------------------------------------------------------------------ boot */
async function init() {
  wireCutoffs(); wireMine(); wireTop(); wireTopRefresh();
  $("#banner").onclick = openHistory;
  $("#histopen").onclick = openHistory;
  if (location.hash === "#history") openHistory();   // a link straight to the past events
  $("#evchapters").addEventListener("click", e => { const b = e.target.closest("[data-chapter]"); if (b) selectChapter(b.dataset.chapter); });
  $("#histclose").onclick = () => $("#histdlg").close();
  $("#histdlg").addEventListener("click", e => { if (e.target === e.currentTarget) e.currentTarget.close(); });
  try { S.ev = await getJSON("/api/event"); S.chapter = S.ev.event && S.ev.event.key; renderHead(S.ev); renderRules(S.ev); }
  catch (e) { $("#evtitle").textContent = "No event data"; toast(e.message); }
  const cut = loadCutoffs();   // after the event: a Spotlight event's cutoffs are per chapter
  loadTop();
  try {
    const songs = (await getJSON(`/api/event/songs?boosts=3${S.chapter ? `&event=${encodeURIComponent(S.chapter)}` : ""}`)).songs || [];
    S.songs = songs.filter(s => s.chart_available !== false && s.length_permil != null);
  } catch (e) { S.songs = []; }
  await cut;
  renderMine();
}
init();
