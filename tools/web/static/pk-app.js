// Park page: one map per area with everything there is to find on it, all READ from the account
// (GET /api/park: the server hands the client the day's picks in plaintext on User/Get), nothing
// predicted. Layers, each a chip in the toolbar:
//   * Free Chats -- the game's facility icon at every site where holomems chat, with a badge of
//     the conversations left (pk-chat.js draws them, their hover card and their dialog);
//   * today's random symbols -- every area prints ITS OWN roll time; the roll is lazy (first
//     contact after the day's reset), so a roll from before the reset is shown as stale, not
//     smoothed into "today";
//   * chests -- one-time pickups: the ones still closed are drawn with the game's own chest icon
//     and counted per area; the opened ones and every possible symbol spot are optional layers.
// GET /api/park returns what the last account refresh saved (the page logs in only for "Scan all
// areas", off by default: a login from the app ends the game's session); it runs on page open and
// on "Read again", the layer toggles and the holomem filter redraw from the data in hand.
import { setFreeChat, chatStrip, chatMarkers, chatTally, chatMembers, left, TALK } from "/pk-chat.js";

const byId = id => document.getElementById(id);   // not `$`: test_id_contract
const QUOTE = String.fromCharCode(34);
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", [QUOTE]: "&quot;" };
const ESC_RE = new RegExp("[&<>" + QUOTE + "]", "g");
const esc = s => String(s ?? "").replace(ESC_RE, c => ESC[c]);
// the game's own area names come with the export (LangArea)
const areaName = a => a.name && a.name !== a.id ? a.name : a.id;
const CHEST = "/park/chest.png";
const SYMBOL = "/park/symbol.png";   // the game's own random-symbol picture
const MAP_H = 820;   // a map is at most this tall: its width follows from its own aspect
const closed = a => (a.treasures || []).filter(t => !t.opened && t.x != null);
const spotNo = s => { const m = String(s || "").match(/(\d+)$/); return m ? String(+m[1]) : ""; };

let data = null, readAt = null;
const L = { chats: true, symbols: true, chests: true, opened: false, cands: false };
try { Object.assign(L, JSON.parse(localStorage.getItem("hb.parkview") || "{}")); } catch (_) { /* private mode */ }

const F = window.HBFmt;   // hb-format.js: the shared date rules

/* ------------------------------------------------------------------ one area */
const symbolsOut = a => a.picked.filter(p => !p.collected).length;
const chatsLeft = a => (L.chats ? left(chatTally(a.id)) : 0);
// what an area still holds, for the order of the cards and the header tiles
const todo = a => (L.symbols ? symbolsOut(a) : 0) + (L.chests ? closed(a).length : 0) + chatsLeft(a);

function mapHTML(a) {
  const P = (v, n) => (v / n * 100).toFixed(3) + "%";
  const at = p => `left:${P(p.x, a.width)};top:${P(p.y, a.height)}`;
  const mk = (p, cls, label, n) => `<span class="pv-mk ${cls}" style="${at(p)}" title="${esc(label)}">${n ? `<b>${n}</b>` : ""}</span>`;
  const chest = (t, open) => `<span class="pv-chest${open ? " opened" : ""}" style="${at(t)}" title="${esc(`Chest ${spotNo(t.spot)}: ${open ? "opened" : "not opened yet"}`)}"><img src="${CHEST}" alt="">${!open ? `<b>${spotNo(t.spot)}</b>` : ""}</span>`;
  const out = [];
  if (L.cands) for (const c of a.candidates || []) out.push(mk(c, "cand", `Possible spot ${spotNo(c.spot)}`));
  if (L.chests || L.opened) for (const t of a.treasures || []) {
    if (t.x == null) continue;
    if (t.opened ? L.opened : L.chests) out.push(chest(t, t.opened));
  }
  // a symbol is the game's star orb with its number on a tab: no round badge, which on these
  // maps means a Free Chat count
  if (L.symbols) a.picked.forEach((p, k) => out.push(`<span class="pv-sym ${p.collected ? "got" : "out"}" style="${at(p)}" title="${esc(`${p.collected ? "Collected" : "Still out"}: spot ${spotNo(p.spot)}`)}"><img src="${SYMBOL}" alt=""><b>${k + 1}</b></span>`));
  if (L.chats) out.push(chatMarkers(a));
  return `<div class="pv-map pv-big pv-canvas" style="aspect-ratio:${a.width} / ${a.height};width:min(100%, ${Math.round(MAP_H * a.width / a.height)}px)">
    <img src="${esc(a.image)}" alt="Map of ${esc(areaName(a))}" loading="lazy">${out.join("")}<div class="pv-hover" hidden></div></div>`;
}
function areaCard(a, i) {
  const out = symbolsOut(a), n = a.picked.length;
  const shut = closed(a), chests = (a.treasures || []).length;
  const ct = chatTally(a.id);
  // the start of the game day comes from the server (the master's resetTimingHour)
  const stale = a.rolled_at && data.game_day_start && a.rolled_at < data.game_day_start;
  const pills = [
    L.chats && ct.total ? (left(ct) ? `<span class="pv-pill chat"><img src="${TALK}" alt=""><b class="n">${left(ct)}</b> of ${ct.total} chats left</span>`
      : `<span class="pv-pill done">All ${ct.total} chats read</span>`) : "",
    L.symbols && n ? (out ? `<span class="pv-pill sym"><img src="${SYMBOL}" alt=""><b class="n">${out}</b> of ${n} symbols out</span>` : `<span class="pv-pill done">All ${n} symbols collected</span>`) : "",
    L.chests && shut.length ? `<span class="pv-pill chest"><img src="${CHEST}" alt=""><b class="n">${shut.length}</b> chest${shut.length === 1 ? "" : "s"} to open</span>`
      : L.chests && chests ? `<span class="pv-pill done">All ${chests} chests opened</span>` : "",
  ].join("");
  const roll = !(L.symbols && n) ? "" : a.rolled_at
    ? `<span class="pv-roll${stale ? " stale" : ""}" title="${esc(new Date(a.rolled_at).toString())}"><svg class="i s"><use href="#i-clock"/></svg>Symbols rolled ${esc(F.date(a.rolled_at))} · ${F.ago(a.rolled_at)}${stale ? ` <em>before today’s reset</em>` : ""}</span>`
    : `<span class="pv-roll">Roll time unknown</span>`;
  const list = L.symbols && n ? `<ol class="pv-list">${a.picked.map((p, k) => `<li class="${p.collected ? "got" : "out"}"><span class="pv-sym-i"><img src="${SYMBOL}" alt=""><b>${k + 1}</b></span>Spot ${spotNo(p.spot)}<span>${p.collected ? "Collected" : "Still out"}</span></li>`).join("")}</ol>` : "";
  const chestList = L.chests && shut.length ? `<div class="pv-chests"><span class="lbl">Chests to open</span>${shut.map(t => `<span class="pv-ct"><img src="${CHEST}" alt="">${spotNo(t.spot)}</span>`).join("")}<span class="pv-cof">${chests - shut.length} of ${chests} opened</span></div>` : "";
  const unres = (a.unresolved || []).length ? `<p class="pv-warn"><svg class="i s"><use href="#i-alert"/></svg>${a.unresolved.length} pick${a.unresolved.length === 1 ? "" : "s"} could not be placed: ${esc(a.unresolved.map(u => u.reason).join("; "))}</p>` : "";
  return `<article class="pv-area${todo(a) ? " has-out" : ""}" style="--i:${i}">
    <header><h2>${esc(areaName(a))}</h2><div class="pv-pills">${pills}</div></header>
    ${roll}
    ${mapHTML(a)}
    ${list}${chestList}${unres}
  </article>`;
}

/* ------------------------------------------------------------------ the page */
function render() {
  const out = byId("out");
  if (!data) return;
  setFreeChat(data, render);
  setLegend();
  byId("fcstrip").innerHTML = L.chats ? chatStrip() : "";
  byId("fcmems").innerHTML = L.chats ? chatMembers() : "";
  const areas = data.areas || [];
  if (!data.available) byId("pvsum").innerHTML = "";
  const problem = data.available ? "" : `<div class="pv-error"><b>${esc(data.reason || "Today’s park state is not available right now.")}</b>${data.fix ? `<pre>${esc(data.fix)}</pre>` : ""}</div>`;
  if (data.available) {
    const rolling = areas.filter(a => a.picked.length);
    const total = rolling.reduce((s, a) => s + a.picked.length, 0);
    const leftSym = rolling.reduce((s, a) => s + symbolsOut(a), 0);
    const shut = areas.reduce((s, a) => s + closed(a).length, 0);
    // stat tiles: the symbols and chests; the Free Chats have their own section below
    const tile = (lbl, n, sub) => `<div class="stat-tile pv-left"><div><span class="lbl">${lbl}</span><b class="n${n ? " out" : ""}">${n}</b><small>${sub}</small></div></div>`;
    byId("pvsum").innerHTML = tile("Symbols out", leftSym, `of ${total} today`)
      + tile("Chests to open", shut, "one-time pickups");
  }
  // the areas with the most to find first; an area with nothing gets one line, not a map
  const order = areas.map((a, i) => ({ a, i })).sort((x, y) => todo(y.a) - todo(x.a) || x.i - y.i);
  const quiet = data.available ? order.filter(({ a }) => !todo(a)) : [];
  byId("quiet").hidden = !quiet.length;
  byId("quiet").textContent = quiet.length ? `Nothing to find in ${quiet.map(({ a }) => areaName(a)).join(", ")}.` : "";
  const cards = order.filter(({ a }) => todo(a)).map(({ a, i }) => areaCard(a, i)).join("");
  out.innerHTML = problem + (cards ? `<div class="pv-grid">${cards}</div>` : "");
}
function setLegend() {
  for (const k of Object.keys(L)) { const b = byId(`lay-${k}`); if (b) b.setAttribute("aria-pressed", String(!!L[k])); }
}

async function load() {
  const btn = byId("reread"); btn.disabled = true; byId("rereadlabel").textContent = "Reading…";
  try {
    const r = await fetch("/api/park");
    data = await r.json();
  } catch (e) {
    data = { available: false, reason: "Could not reach the builder's server: " + e.message, areas: [] };
  }
  readAt = Date.now();
  const at = data.captured_at ? Date.parse(data.captured_at) : NaN;
  byId("readat").textContent = !data.available ? "Not read"
    : `From your last Refresh ${F.date(Number.isNaN(at) ? readAt : at)}`;
  btn.disabled = false; byId("rereadlabel").textContent = "Read again";
  render();
  renderScan(data.scan);
}

/* ------------------------------------------------------------------ Scan all areas */
// POST /api/park/scan runs tools/refresh/park_scan.py: it logs in, enters every area that has
// Free Chats (buildings too) and goes back to the account's own; off unless turned on in Settings.
// data.scan says whether it is on, whether today's scan is used, and what the last one did.
let scanning = false;
function toast(msg) {
  const t = byId("toast");
  t.textContent = msg; t.classList.add("on");
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("on"), 5000);
}
const hhmm = ms => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
function scanSummary(last) {
  const areas = (last.areas || []).length, fresh = (last.areas || []).reduce((s, a) => s + (a.new_read || 0), 0);
  return `${areas} area${areas === 1 ? "" : "s"} read${fresh ? `, ${fresh} newly heard chat${fresh === 1 ? "" : "s"}` : ""}`;
}
function renderScan(scan) {
  const b = byId("scan"), meta = byId("scanmeta");
  if (!scan) { b.hidden = meta.hidden = true; return; }
  b.hidden = meta.hidden = false;
  meta.classList.remove("bad");
  if (scanning) return;
  const last = scan.last;
  const lastAt = last && last.finished_at ? Date.parse(last.finished_at) : NaN;
  if (!scan.enabled) {
    b.disabled = true;
    b.title = "Scan all areas is off. Turn it on in Settings.";
    meta.textContent = "Off · turn it on in Settings";
  } else if (scan.done_today) {
    b.disabled = true;
    b.title = "Once per game day";
    meta.textContent = `Scanned today, ${scanSummary(last)} · next from ${hhmm(scan.next_at)}`;
  } else {
    b.disabled = false;
    b.title = "Enter every area with Free Chats and come back";
    if (last && last.stopped && !Number.isNaN(lastAt)) {
      meta.textContent = `Last try ${F.date(lastAt)} stopped: ${last.stopped}`;
      meta.classList.add("bad");
    } else meta.textContent = last && !Number.isNaN(lastAt) ? `Last scan ${F.date(lastAt)}, ${scanSummary(last)}` : "Reads every area’s Free Chats in one go";
  }
}
byId("scan").addEventListener("click", () => byId("scandlg").showModal());
byId("scango").addEventListener("click", async () => {
  byId("scandlg").close();
  const b = byId("scan"), meta = byId("scanmeta");
  scanning = true; b.disabled = true; b.classList.add("busy");
  byId("scanlabel").textContent = "Scanning…";
  meta.classList.remove("bad");
  meta.textContent = "Entering each area and coming back: about a minute";
  let msg;
  try {
    const r = await fetch("/api/park/scan", { method: "POST" });
    const out = await r.json();
    const last = out.last;
    if (!r.ok) msg = out.error || `the scan was refused (${r.status})`;
    else if (out.returncode === 0 && last) msg = `Scan done: ${scanSummary(last)}, back where you were.`;
    else if (last && last.stopped) msg = `The scan stopped: ${last.stopped}`;
    else msg = ((out.stdout || "").trim().split("\n").pop() || "the scan did not start").replace(/^not started:\s*/, "Not started: ");
  } catch (e) {
    msg = "Could not reach the builder's server: " + e.message;
  }
  scanning = false; b.classList.remove("busy");
  byId("scanlabel").textContent = "Scan all areas";
  toast(msg);
  await load();
  if (!/^Scan done/.test(msg)) { meta.textContent = msg; meta.classList.add("bad"); }
});

/* "Read again" re-reads what the last Refresh saved; it never logs in */
byId("reread").addEventListener("click", load);
document.addEventListener("click", e => {
  const b = e.target.closest("[data-lay]");
  if (b) {
    L[b.dataset.lay] = !L[b.dataset.lay];
    try { localStorage.setItem("hb.parkview", JSON.stringify(L)); } catch (_) { /* private */ }
    render();
    return;
  }
  const c = e.target.closest("[data-close]"); if (c) c.closest("dialog").close();
});
load();
