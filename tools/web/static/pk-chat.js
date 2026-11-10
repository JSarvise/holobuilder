// Park page, the Free Chat pieces: every conversation between holomems the Park can show, where
// it stands, and which ones the account has read. pk-app.js draws the area maps and asks this
// module for the Free Chat layer of each one (site markers), the strip above the maps (the count
// and the holomem filter) and the holomems below them. Data: GET /api/park -> free_chat (the
// export's catalogue joined to the refreshes' read log, hbweb/park.py join_free_chat).
//   read     a refresh carried read marks on it (marks are permanent)
//   pending  the latest refresh that showed its site carried no marks ("Not read")
//   unseen   no refresh has shown its site yet: each login shows only the sites the game
//            picked for the area the account stands in ("Not seen yet")
// A site marker is the game's facility icon with a badge of what is left; hovering it shows the
// faces still to meet, clicking it opens the game's photo of the place over its chats.
const QUOTE = String.fromCharCode(34);
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", [QUOTE]: "&quot;" };
const ESC_RE = new RegExp("[&<>" + QUOTE + "]", "g");
const esc = s => String(s ?? "").replace(ESC_RE, c => ESC[c]);
const byId = id => document.getElementById(id);
const F = window.HBFmt;
export const TALK = "/ui/park/ui_200_ico_free_talk.png";
const TALK_READ = "/ui/park/ui_200_ico_free_talk_2.png";
const WORD = { read: "Read", pending: "Not read", unseen: "Not seen yet" };
const ORDER = { pending: 0, unseen: 1, read: 2 };

let fc = null, areas = {}, who = "", redraw = () => {};
try { who = localStorage.getItem("hb.parkview.who") || ""; } catch (_) { /* private mode */ }

const chars = () => Object.fromEntries((fc.characters || []).map(c => [c.id, c]));
const short = id => (chars()[id] || {}).short || id;
// a holomem with no icon on disk keeps an empty disc (a transparent pixel over the disc's
// fill) rather than a gap or the browser's broken-image glyph
const BLANK = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
const face = (id, cls = "") => `<img class="pv-face ${cls}" src="/chricon/${esc(id)}" alt="" title="${esc((chars()[id] || {}).name || id)}" loading="lazy" onerror="this.onerror=null;this.src='${BLANK}'">`;
const mine = () => (fc.chats || []).filter(c => !who || c.cast.includes(who));
function tally(rows) {
  const t = { total: rows.length, read: 0, pending: 0, unseen: 0 };
  for (const r of rows) t[r.status] += 1;
  return t;
}
export const left = t => t.pending + t.unseen;
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : /(s|sh|ch)$/.test(w) ? "es" : "s"}`;
const ready = () => !!(fc && fc.available);

function bar(t, cls = "") {
  const pc = n => t.total ? (n / t.total * 100).toFixed(2) + "%" : "0%";
  return `<div class="pv-fc-bar ${cls}" role="img" aria-label="${t.read} read, ${t.pending} not read, ${t.unseen} not seen yet">
    <i class="read" style="width:${pc(t.read)}"></i><i class="pending" style="width:${pc(t.pending)}"></i><i class="unseen" style="width:${pc(t.unseen)}"></i></div>`;
}

/* ---------------------------------------------------------------- the strip above the maps */
export function chatStrip() {
  if (!fc) return "";
  if (!fc.available) return `<p class="pv-warn">${esc(fc.reason || "The Free Chats are not available.")}</p>`;
  const t = tally(mine());
  const c = who ? chars()[who] : null;
  const game = c && c.game_read != null
    ? `<span class="pv-fc-game">The game counts <b class="n">${c.game_read}${c.game_capped ? "+" : ""}</b> read with ${esc(c.short)}${c.game_read > t.read ? ` · ${c.game_read - t.read} of them at sites no refresh has shown yet` : !c.game_capped ? " · every one of them is on the map" : ""}</span>` : "";
  const opts = [...(fc.characters || [])].sort((a, b) => a.short.localeCompare(b.short))
    .map(ch => `<option value="${esc(ch.id)}"${ch.id === who ? " selected" : ""}>${esc(ch.short)}</option>`).join("");
  return `<div class="pv-fc-h">
      <div><h2 class="pv-fc-title"><img src="${TALK}" alt="">Free Chat</h2></div>
      <div class="pv-fc-ctl">
        <div class="pv-fc-score">
          <div class="pv-fc-big"><b class="n">${t.read}</b><span>/ ${t.total} read${c ? ` with ${esc(c.short)}` : ""}</span></div>
          ${bar(t)}
          <ul class="pv-fc-legend">
            <li><i class="read"></i>Read <b class="n">${t.read}</b></li>
            <li><i class="pending"></i>Not read <b class="n">${t.pending}</b></li>
            <li><i class="unseen"></i>Not seen yet <b class="n">${t.unseen}</b></li>
          </ul>${game}
        </div>
        <label class="pv-fc-who"><span class="lbl">Holomem</span>
          <select id="fcwho" aria-label="Show the Free Chats of one holomem"><option value="">All holomems</option>${opts}</select>
          ${who ? `<button class="chip" type="button" id="fcclear" aria-pressed="true">${face(who, "s")}${esc(short(who))} ✕</button>` : ""}
        </label>
      </div>
    </div>`;
}

/* ---------------------------------------------------------------- the layer on an area map */
// the game's generic facility icon is a blank tile: such a site (the Plaza) wears the Free Chat
// bubble instead
const siteIcon = s => s.icon && !/icon-facility\.png$/.test(s.icon) ? s.icon : null;
function siteMarker(s, a) {
  const rows = mine().filter(c => c.site === s.id);
  if (!rows.length) return "";
  const t = tally(rows), n = left(t);
  const P = (v, m) => (v / m * 100).toFixed(3) + "%";
  const state = !n ? "done" : t.pending ? "todo" : "maybe";
  const icon = siteIcon(s);
  const glyph = icon ? `<img class="pv-site-ico" src="${esc(icon)}" alt="">`
    : `<img class="pv-site-ico bubble" src="${TALK}" alt="">`;
  return `<button class="pv-site ${state}" type="button" data-site="${esc(s.id)}" style="left:${P(s.x, a.width)};top:${P(s.y, a.height)}"
      aria-label="${esc(s.name)}: ${n ? `${n} of ${plural(t.total, "Free Chat")} left` : `${t.total === 1 ? "its Free Chat" : `all ${t.total} Free Chats`} read`}${s.inside ? ", inside the building" : ""}">
    ${glyph}<b class="pv-site-n n">${n || "✓"}</b>${s.inside ? `<span class="pv-site-in">Inside</span>` : ""}</button>`;
}
/** The site markers drawn on area `a` (an /api/park area: id, width, height). */
export function chatMarkers(a) {
  if (!ready()) return "";
  return (fc.sites || []).filter(s => s.map === a.id).map(s => siteMarker(s, a)).join("");
}
/** {total, read, pending, unseen} of the Free Chats on area `id` (the holomem filter applies). */
export function chatTally(id) {
  if (!ready()) return tally([]);
  const here = new Set((fc.sites || []).filter(s => s.map === id).map(s => s.id));
  return tally(mine().filter(c => here.has(c.site)));
}
export const freeChatSummary = () => ready() ? tally(fc.chats || []) : null;

/* ---------------------------------------------------------------- hover card + dialog */
function hoverHTML(s) {
  const rows = mine().filter(c => c.site === s.id);
  const t = tally(rows);
  const need = rows.filter(c => c.status !== "read");
  const people = {};
  for (const c of need) for (const p of c.cast) people[p] = (people[p] || 0) + 1;
  const top = Object.entries(people).sort((a, b) => b[1] - a[1] || short(a[0]).localeCompare(short(b[0])));
  const faces = top.slice(0, 14).map(([p, n]) => `<span class="pv-hv-p">${face(p)}<i class="n">${n}</i></span>`).join("");
  return `<b class="pv-hv-t">${esc(s.name)}</b>
    <span class="pv-hv-s">${s.inside ? "Inside the building · " : ""}${left(t) ? `${left(t)} of ${t.total} left` : `All ${t.total} read`}</span>
    ${bar(t, "thin")}
    ${faces ? `<span class="lbl">Still to meet</span><div class="pv-hv-faces">${faces}${top.length > 14 ? `<span class="pv-hv-more">+${top.length - 14}</span>` : ""}</div>` : ""}
    <span class="pv-hv-go">Click for the conversations</span>`;
}
function chatRow(c, shared) {
  const names = c.cast.map(short);
  const label = names.length > 1 ? names.slice(0, -1).join(", ") + " & " + names[names.length - 1] : names[0] || "";
  const when = c.status === "read" && c.read_seen ? `read by ${F.date(Date.parse(c.read_seen))}`
    : c.status === "pending" && c.seen ? `as of ${F.date(Date.parse(c.seen))}` : "";
  const own = c.conditions.filter(k => !shared.includes(k));
  return `<li class="pv-chat ${c.status}">
    <span class="pv-chat-faces">${c.cast.map(p => face(p)).join("")}</span>
    <span class="pv-chat-who">${esc(label)}${own.length ? `<small>Needs ${own.map(esc).join(" · ")}</small>` : ""}</span>
    <span class="pv-chat-st"><img src="${c.status === "read" ? TALK_READ : TALK}" alt="">${WORD[c.status]}${when ? `<small>${esc(when)}</small>` : ""}</span>
  </li>`;
}
function openSite(id) {
  const s = (fc.sites || []).find(x => x.id === id);
  if (!s) return;
  const a = areas[s.map] || {};
  const rows = mine().filter(c => c.site === id).sort((x, y) => ORDER[x.status] - ORDER[y.status] || x.id.localeCompare(y.id));
  const t = tally(rows);
  byId("fcphoto").innerHTML = s.photo ? `<img src="${esc(s.photo)}" alt="${esc(s.name)}">` : "";
  byId("fcphoto").hidden = !s.photo;
  byId("fcsitetitle").innerHTML = `<img src="${esc(siteIcon(s) || TALK)}" alt="">${esc(s.name)}`;
  byId("fcsitesub").textContent = [a.name, s.inside ? "inside the building" : "", who ? `with ${short(who)}` : ""].filter(Boolean).join(" · ");
  // a condition every listed chat has (its grouping's) is said once, not on every row
  const shared = rows.length ? rows[0].conditions.filter(k => rows.every(c => c.conditions.includes(k))) : [];
  const inside = s.inside ? `<p class="pv-fc-inside"><img src="${TALK}" alt="">The game has no map of the inside: these ${plural(t.total, "conversation")} happen inside ${esc(s.name)}; walk in from its door on the ${esc(a.name)} map.</p>` : "";
  byId("fcsitebody").innerHTML = `${inside}
    <div class="pv-fc-dsum">${bar(t)}<span><b class="n">${t.read}</b> read · <b class="n">${t.pending}</b> not read · <b class="n">${t.unseen}</b> not seen yet</span>
      ${shared.length ? `<span class="pv-fc-needs">Unlocks with: ${shared.map(esc).join(" · ")}</span>` : ""}</div>
    <ol class="pv-chats">${rows.map(c => chatRow(c, shared)).join("")}</ol>`;
  byId("fcsite").showModal();
}

/* ---------------------------------------------------------------- the holomems below the maps */
// the holomems in generation order (hb-gens.js: Gen 0, Gen 1, ... as the game lists them);
// one with no generation there goes last, by name
const GEN_ORDER = Object.keys((window.HB_GENS || {}).by || {});
const genRank = id => { const i = GEN_ORDER.indexOf(id); return i < 0 ? GEN_ORDER.length : i; };
export function chatMembers() {
  if (!ready()) return "";
  const list = [...(fc.characters || [])].map(c => ({ c, t: tally((fc.chats || []).filter(x => x.cast.includes(c.id))) }))
    .sort((x, y) => genRank(x.c.id) - genRank(y.c.id) || x.c.short.localeCompare(y.c.short));
  return `<h3 class="pv-fc-sub"><img src="${TALK}" alt="">Free Chats by holomem</h3>
    <div class="pv-fc-mems">${list.map(({ c, t }) => `<button class="pv-mem${c.id === who ? " on" : ""}" type="button" data-who="${esc(c.id)}" aria-pressed="${c.id === who}">
      ${face(c.id)}
      <span class="pv-mem-t"><b>${esc(c.short)}</b><small>${c.game_read != null ? `Game: <b class="n">${c.game_read}${c.game_capped ? "+" : ""}</b> read` : "No achievement"}</small></span>
      <span class="pv-mem-n"><b class="n">${left(t)}</b><small>left of ${t.total}</small></span>
      ${bar(t, "thin")}
    </button>`).join("")}</div>`;
}

/* ---------------------------------------------------------------- state + events */
function setWho(id) {
  who = id || "";
  try { localStorage.setItem("hb.parkview.who", who); } catch (_) { /* private */ }
  redraw();
}
/** Take the /api/park payload; `onChange` redraws the page when the holomem filter changes. */
export function setFreeChat(data, onChange) {
  fc = data && data.free_chat;
  areas = Object.fromEntries((data && data.areas || []).map(a => [a.id, a]));
  if (onChange) redraw = onChange;
  if (ready() && who && !(fc.characters || []).some(c => c.id === who)) who = "";
}

document.addEventListener("change", e => { if (e.target && e.target.id === "fcwho") setWho(e.target.value); });
document.addEventListener("click", e => {
  const s = e.target.closest(".pv-site");
  if (s) { openSite(s.dataset.site); return; }
  const m = e.target.closest("[data-who]");
  if (m) { setWho(m.dataset.who === who ? "" : m.dataset.who); return; }
  if (e.target.closest("#fcclear")) setWho("");
  if (e.target.id === "fcsite") e.target.close();      // a click on the backdrop
});
function hover(e, on) {
  const s = e.target.closest && e.target.closest(".pv-site");
  if (!s || !fc) return;
  const card = s.parentElement.querySelector(".pv-hover");
  if (!card) return;
  if (!on) { card.hidden = true; return; }
  card.innerHTML = hoverHTML((fc.sites || []).find(x => x.id === s.dataset.site));
  card.hidden = false;
  // keep the card inside the map: open it to the side and edge with more room
  const box = s.parentElement.getBoundingClientRect(), r = s.getBoundingClientRect();
  const x = r.left + r.width / 2 - box.left, y = r.top + r.height / 2 - box.top;
  card.style.left = x < box.width / 2 ? `${x + 30}px` : "";
  card.style.right = x < box.width / 2 ? "" : `${box.width - x + 30}px`;
  card.style.top = y < box.height / 2 ? `${Math.max(8, y - 20)}px` : "";
  card.style.bottom = y < box.height / 2 ? "" : `${Math.max(8, box.height - y - 20)}px`;
}
document.addEventListener("mouseover", e => hover(e, true));
document.addEventListener("mouseout", e => hover(e, false));
document.addEventListener("focusin", e => hover(e, true));
document.addEventListener("focusout", e => hover(e, false));
