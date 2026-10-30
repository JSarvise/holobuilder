// Park page: today's hidden random symbols, drawn on the game's own
// area maps. Everything is READ from the account (GET /api/park: the server hands the client
// the day's picks in plaintext on User/Get), nothing is predicted. The same rules as
// static/park.js apply:
//   * every area prints ITS OWN roll time; the roll is lazy (first contact after 05:00 JST),
//     so a roll from before the last reset is shown as stale, not smoothed into "today";
//   * fixed chests are one-time pickups: the ones still closed are drawn with the game's own
//     chest icon and counted per area (a new area can bring dozens); the
//     opened ones are an optional, dimmed layer.
// GET /api/park triggers a live login (server TTL 60 s), so it runs on page open and on
// "Read again" only; the layer toggles redraw from the data in hand.
const byId = id => document.getElementById(id);   // not `$`: test_id_contract
const QUOTE = String.fromCharCode(34);
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", [QUOTE]: "&quot;" };
const ESC_RE = new RegExp("[&<>" + QUOTE + "]", "g");
const esc = s => String(s ?? "").replace(ESC_RE, c => ESC[c]);
const HOUR = 3600e3, DAY = 24 * HOUR;
// the game's own area names come with the export (LangArea); these only cover an old export
const AREA = { "area-a01": "Harmony Garden", "area-a02-game": "Play Area · Game Corner",
  "area-a02-casino": "Play Area · Casino", "area-a02-lobby": "Lobby", "area-a03": "Soda Pop Lagoon" };
const areaName = a => a.name && a.name !== a.id ? a.name : AREA[a.id] || a.id;
const CHEST = "/park/chest.png";
const closed = a => (a.treasures || []).filter(t => !t.opened && t.x != null);
const spotNo = s => { const m = String(s || "").match(/(\d+)$/); return m ? String(+m[1]) : ""; };

let data = null, readAt = null;
const L = { opened: false, cands: false };
try { Object.assign(L, JSON.parse(localStorage.getItem("hb.parkview") || "{}")); } catch (_) { /* private mode */ }

// the most recent 05:00 JST reset at or before `now` (JST is UTC+9, no DST)
function lastReset(now) {
  const j = new Date(now + 9 * HOUR);
  let b = Date.UTC(j.getUTCFullYear(), j.getUTCMonth(), j.getUTCDate(), 5, 0, 0) - 9 * HOUR;
  if (b > now) b -= DAY;
  return b;
}
const F = window.HBFmt;   // hb-format.js: the shared date rules
const ago = ms => F.ago(ms);
const when = ms => F.date(ms);
const BOX = 4 / 3;   // every area map sits in the same 4:3 box, so the spot lists start at one height


/* ------------------------------------------------------------------ the map with its markers */
function mapHTML(a, big) {
  const P = (v, n) => (v / n * 100).toFixed(3) + "%";
  const at = p => `left:${P(p.x, a.width)};top:${P(p.y, a.height)}`;
  const mk = (p, cls, label, n) => `<span class="pv-mk ${cls}" style="${at(p)}" title="${esc(label)}">${n ? `<b>${n}</b>` : ""}</span>`;
  const chest = (t, open) => `<span class="pv-chest${open ? " opened" : ""}" style="${at(t)}" title="${esc(`Chest ${spotNo(t.spot)}: ${open ? "opened" : "not opened yet"}`)}"><img src="${CHEST}" alt="">${big && !open ? `<b>${spotNo(t.spot)}</b>` : ""}</span>`;
  const out = [];
  if (L.cands) for (const c of a.candidates || []) out.push(mk(c, "cand", `Possible spot ${spotNo(c.spot)}`));
  for (const t of a.treasures || []) if (t.x != null && (!t.opened || L.opened)) out.push(chest(t, t.opened));
  a.picked.forEach((p, k) => out.push(mk(p, p.collected ? "got" : "out", `${p.collected ? "Collected" : "Still out"}: spot ${spotNo(p.spot)}`, big || !p.collected ? k + 1 : "")));
  const fit = big ? "" : a.width / a.height >= BOX ? " wide" : " tall";
  return `<div class="pv-map${big ? " big" : ""}${fit}" style="aspect-ratio:${a.width} / ${a.height}">
    <img src="${esc(a.image)}" alt="Map of ${esc(areaName(a))}" loading="lazy">${out.join("")}</div>`;
}
function areaCard(a, i) {
  const out = a.picked.filter(p => !p.collected).length, n = a.picked.length;
  const shut = closed(a), chests = (a.treasures || []).length;
  const stale = a.rolled_at && a.rolled_at < lastReset(Date.now());
  const pills = [
    n ? (out ? `<span class="pv-pill out"><b class="n">${out}</b> of ${n} still out</span>` : `<span class="pv-pill done">All ${n} collected</span>`) : "",
    shut.length ? `<span class="pv-pill chest"><img src="${CHEST}" alt=""><b class="n">${shut.length}</b> chest${shut.length === 1 ? "" : "s"} to open</span>`
      : chests ? `<span class="pv-pill done">All ${chests} chests opened</span>` : "",
  ].join("");
  const roll = !n ? "" : a.rolled_at
    ? `<span class="pv-roll${stale ? " stale" : ""}" title="${esc(new Date(a.rolled_at).toString())}"><svg class="i s"><use href="#i-clock"/></svg>Symbols rolled ${esc(when(a.rolled_at))} · ${ago(a.rolled_at)}${stale ? ` <em>before today’s reset</em>` : ""}</span>`
    : `<span class="pv-roll">Roll time unknown</span>`;
  const list = n ? `<ol class="pv-list">${a.picked.map((p, k) => `<li class="${p.collected ? "got" : "out"}"><i>${k + 1}</i>Spot ${spotNo(p.spot)}<span>${p.collected ? "Collected" : "Still out"}</span></li>`).join("")}</ol>` : "";
  const chestList = shut.length ? `<div class="pv-chests"><span class="lbl">Chests to open</span>${shut.map(t => `<span class="pv-ct"><img src="${CHEST}" alt="">${spotNo(t.spot)}</span>`).join("")}<span class="pv-cof">${chests - shut.length} of ${chests} opened</span></div>` : "";
  const unres = (a.unresolved || []).length ? `<p class="pv-warn"><svg class="i s"><use href="#i-alert"/></svg>${a.unresolved.length} pick${a.unresolved.length === 1 ? "" : "s"} could not be placed: ${esc(a.unresolved.map(u => u.reason).join("; "))}</p>` : "";
  return `<article class="pv-area${out ? " has-out" : ""}" style="--i:${i}">
    <header><h2>${esc(areaName(a))}</h2><div class="pv-pills">${pills}</div></header>
    ${roll}
    <button class="pv-mapbtn" type="button" data-zoom="${i}" aria-label="Open the ${esc(areaName(a))} map larger">${mapHTML(a, false)}<span class="pv-exp"><svg class="i s"><use href="#i-expand"/></svg></span></button>
    ${list}${chestList}${unres}
  </article>`;
}

/* ------------------------------------------------------------------ the page */
function render() {
  const out = byId("out");
  if (!data) return;
  if (!data.available) {
    byId("pvtitle").textContent = "Park";
    byId("pvlede").textContent = "Today’s park state could not be read.";
    byId("pvsum").innerHTML = "";
    out.innerHTML = `<div class="pv-error"><b>${esc(data.reason || "Today’s park state is not available right now.")}</b>${data.fix ? `<pre>${esc(data.fix)}</pre>` : ""}</div>`;
    return;
  }
  const areas = data.areas || [];
  const rolling = areas.filter(a => a.picked.length);
  const total = rolling.reduce((s, a) => s + a.picked.length, 0);
  const left = rolling.reduce((s, a) => s + a.picked.filter(p => !p.collected).length, 0);
  const shut = areas.reduce((s, a) => s + closed(a).length, 0);
  const parts = [];
  if (left) parts.push(`${left} of today’s ${total} random symbols still out`);
  else if (total) parts.push(`every one of today’s ${total} random symbols collected`);
  if (shut) parts.push(`${shut} chest${shut === 1 ? "" : "s"} still to open`);
  const lede = parts.join(", and ") || "Nothing to find: no random symbols and every chest opened";
  byId("pvlede").textContent = lede.charAt(0).toUpperCase() + lede.slice(1) + ".";
  // stat tiles: what sends you into the game first, then each area that has something
  const tile = (lbl, n, sub, cls) => `<div class="stat-tile${cls ? " " + cls : ""}"><div><span class="lbl">${lbl}</span><b class="n${n ? " out" : ""}">${n}</b><small>${sub}</small></div></div>`;
  const busy = areas.filter(a => a.picked.length || closed(a).length);
  byId("pvsum").innerHTML = tile("Symbols out", left, `of ${total} today`, "pv-left")
    + tile("Chests to open", shut, "one-time pickups", "pv-left")
    + busy.map(a => {
      const o = a.picked.filter(p => !p.collected).length, c = closed(a).length;
      return tile(esc(areaName(a)), o + c, [a.picked.length ? `${o} symbol${o === 1 ? "" : "s"}` : "", c ? `${c} chest${c === 1 ? "" : "s"}` : ""].filter(Boolean).join(" · ") || "done");
    }).join("");
  // the areas with the most to find first; an area with nothing gets one line, not a map
  const score = a => a.picked.filter(p => !p.collected).length * 100 + closed(a).length;
  const order = areas.map((a, i) => ({ a, i })).sort((x, y) => score(y.a) - score(x.a) || x.i - y.i);
  const quiet = order.filter(({ a }) => !a.picked.length && !closed(a).length);
  byId("quiet").hidden = !quiet.length;
  byId("quiet").textContent = quiet.length ? `Nothing to find in ${quiet.map(({ a }) => areaName(a)).join(", ")}: no symbols today and every chest opened.` : "";
  out.innerHTML = `<div class="pv-grid">${order.filter(({ a }) => a.picked.length || closed(a).length).map(({ a, i }) => areaCard(a, i)).join("")}</div>`;
}
function setLegend() {
  byId("opened").setAttribute("aria-pressed", String(!!L.opened)); byId("cands").setAttribute("aria-pressed", String(!!L.cands));
  document.querySelectorAll("#legend .lay").forEach(li => { li.hidden = !L[li.dataset.layer]; });
}

async function load() {
  const btn = byId("reread"); btn.disabled = true; byId("rereadlabel").textContent = "Reading…";
  try {
    const r = await fetch("/api/park");
    data = await r.json();
  } catch (e) {
    data = { available: false, reason: "Could not reach the builder's server: " + e.message };
  }
  readAt = Date.now();
  byId("readat").textContent = data.available ? `Read from your account ${F.date(readAt)}` : "Not read";
  btn.disabled = false; byId("rereadlabel").textContent = "Read again";
  render();
}

/* "Read again" logs into the account: a second press within 4 s confirms it */
let armed = 0;
byId("reread").addEventListener("click", () => {
  if (Date.now() - armed < 4000) { armed = 0; byId("reread").classList.remove("armed"); load(); return; }
  armed = Date.now(); byId("reread").classList.add("armed"); byId("rereadlabel").textContent = "Press again: logs into your account";
  setTimeout(() => { if (armed && Date.now() - armed >= 4000) { armed = 0; byId("reread").classList.remove("armed"); byId("rereadlabel").textContent = "Read again"; } }, 4100);
});
for (const k of ["opened", "cands"]) byId(k).addEventListener("click", () => {
  L[k] = !L[k]; try { localStorage.setItem("hb.parkview", JSON.stringify(L)); } catch (_) { /* private */ }
  setLegend(); render();
});
document.addEventListener("click", e => {
  const z = e.target.closest("[data-zoom]");
  if (z && data) {
    const a = data.areas[+z.dataset.zoom];
    byId("zoomtitle").textContent = areaName(a);
    const o = a.picked.filter(p => !p.collected).length, c = closed(a).length;
    byId("zoomsub").textContent = [a.picked.length ? `${o} of ${a.picked.length} symbols still out` : "",
      c ? `${c} chest${c === 1 ? "" : "s"} to open` : ""].filter(Boolean).join(" · ") || "Nothing to find";
    byId("zoombody").innerHTML = mapHTML(a, true);
    byId("zoom").showModal(); return;
  }
  const c = e.target.closest("[data-close]"); if (c) c.closest("dialog").close();
});
byId("zoom").addEventListener("click", e => { if (e.target === byId("zoom")) byId("zoom").close(); });
setLegend();
load();
