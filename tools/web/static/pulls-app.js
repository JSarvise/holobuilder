// Gacha page (/pulls): the account's pull history, banners, and luck against the rates.
// Every figure comes from GET /api/gacha (tools/web/gacha_view.py, read-only over the
// append-only store data/gacha/). Card names and faces come from GET /api/collection.
// Nothing is invented here: where the API has no figure, the page says so.
// Sync is MANUAL: POST /api/gacha/sync only on a click; no polling.
import { luckRow } from "/pulls-stats.js";

const $ = s => document.querySelector(s);
const QUOTE = String.fromCharCode(34);   // spelled out: a quote inside a regex trips test_js_syntax
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", [QUOTE]: "&quot;" };
const ESC_RE = new RegExp("[&<>" + QUOTE + "]", "g");
const esc = s => String(s ?? "").replace(ESC_RE, c => ESC[c]);
const F = window.HBFmt;   // hb-format.js: the shared date/number/placeholder rules
const num = n => F.num(n);
const pct1 = x => F.pct(x);
const RAR = ["5★", "4★", "3★"];
// rarity hues: pulls.css --r5/--r4/--r3 (dataviz-validated set; the game has no rarity hue to borrow)
const COL = { "5★": "var(--r5)", "4★": "var(--r4)", "3★": "var(--r3)" };
const STAR = "/chrome/rarity/ui_999_img_card_star.png";
const rint = r => parseInt(r, 10) || 0;

// `last_sync`/`first_seen` are epoch SECONDS (our sync); banner times and draw/acquire
// times are the game's MILLISECONDS. One helper for both.
const secs = t => (typeof t === "number" ? (Math.abs(t) > 1e12 ? t / 1000 : t) : null);
const fmtDate = t => F.day(t);
const FAR_FUTURE = Date.UTC(2100, 0, 1) / 1000;
// The permanent pool arrives with no dates and is_end_time_hidden: say "permanent", not "—".
function period(b) {
  const hasS = typeof b.start_time === "number", hasE = typeof b.end_time === "number";
  const hidden = !!b.is_end_time_hidden;
  if (!hasS && !hasE && !hidden) return null;
  const sOpen = !hasS || b.start_time === 0;
  const eOpen = !hasE || b.end_time === 0 || hidden || secs(b.end_time) >= FAR_FUTURE;
  if (sOpen && eOpen) return "Permanent";
  if (eOpen) return `Since ${fmtDate(b.start_time)}`;
  if (sOpen) return `Until ${fmtDate(b.end_time)}`;
  return F.range(b.start_time, b.end_time);
}
const isOpen = b => b.is_live_last_sync && (typeof b.end_time !== "number" || b.end_time > Date.now() || b.is_end_time_hidden);
const bname = n => String(n || "(no name)").replace(/\s*\n\s*/g, " · ");
// "Take your pick! Sea Bridge Gacha" -> "Sea Bridge Gacha": the pick prefix becomes a tag, the name is said once
const PICK_RE = /^\s*take your pick!?\s*/i;
const shortName = n => bname(n).replace(PICK_RE, "");
const pickLabel = n => (PICK_RE.test(bname(n)) ? "Take your pick" : "");

async function getJSON(url, opts) {
  const r = await fetch(url, opts);
  const body = await r.json().catch(() => null);
  if (!r.ok || !body || body.error) throw new Error((body && (body.error || body.message)) || `${r.status} ${r.statusText}`);
  return body;
}
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("on");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("on"), 3200);
}
function stars(r) {
  const n = rint(r);
  return `<span class="rs" role="img" aria-label="${esc(r)}">${`<img src="${STAR}" alt="">`.repeat(n)}</span>`;
}

// ---- card metadata (names, type) from the collection; absent -> id-derived rarity only
let META = new Map();
function meta(id) {
  const c = META.get(id);
  const m = /^card-\d+-(\d)-/.exec(id || "");
  return {
    id, member: c ? c.member : id, type: c ? c.type : "", rarity: c ? rint(c.rarity) : (m ? +m[1] : 3),
    sub: c ? (c.costume_name && c.costume_name !== "Default" ? c.costume_name : c.title || "") : "",
  };
}
function face(id, w = 260) {
  const m = meta(id);
  return window.HBCard.face({ id, member: m.member, type: m.type, rarity: m.rarity }, { w, role: false });
}

let VIEW = null;
const STATE = { banner: "", rarity: "", show: "" };

// ---------------------------------------------------------------- hero + tiles
function heroBanner(v) {
  const pulled = v.banners.filter(b => b.pulls > 0);
  return pulled.find(isOpen) || v.banners.find(isOpen) || pulled[0] || v.banners[0] || null;
}
function art(b, w) { return b && b.gacha_id && b.art && b.art.banner ? `/gachaart/${encodeURIComponent(b.gacha_id)}/banner?w=${w}` : null; }

// Gacha/ListHistory serves only the newest pulls of the last ~30 days, at
// most 100, and cannot be asked for older ones. A pull that leaves that window before a sync
// sees it is gone for good, so the page says so on every visit, louder as the sync ages.
const HISTORY_DAYS = 30, HISTORY_MAX = 100;
function renderLimit(v) {
  const s = secs(v.last_sync);
  const age = s == null ? null : (Date.now() / 1000 - s) / 86400;
  const level = age == null || age >= HISTORY_DAYS - 5 ? "bad" : age >= 14 ? "warn" : "";
  // calendar days, as the header counts them
  const day0 = t => { const d = new Date(t * 1000); return Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) / 86400000; };
  const cal = s == null ? null : day0(Date.now() / 1000) - day0(s);
  const when = cal == null ? "You have never synced."
    : cal === 0 ? "Last sync today." : cal === 1 ? "Last sync yesterday." : `Last sync ${cal}${F.SP}days ago.`;
  const left = age == null ? null : Math.max(0, Math.floor(HISTORY_DAYS - age));
  const advice = level === "bad"
    ? (left ? `Sync now: pulls from before then leave the game's history in about ${left}${F.SP}day${left === 1 ? "" : "s"}.` : "Sync now: some pulls made right after that sync may already have left the game's history.")
    : level === "warn" ? `Sync within ${left}${F.SP}days so no pull falls out.` : "";
  $("#histlimit").hidden = false;
  $("#histlimit").className = `g-limit${level ? " " + level : ""}`;
  $("#histlimit").innerHTML = `<span class="ic">${level ? "!" : "i"}</span><span><b>The game only keeps your last ${HISTORY_DAYS}${F.SP}days of pulls, up to ${HISTORY_MAX}.</b>
    Each sync saves what is still in that window, and older pulls cannot be asked for again, so anything that leaves it between two syncs is lost.
    Sync at least every couple of weeks, and after any session of more than ${HISTORY_MAX} pulls. ${esc(when)}${advice ? ` <b>${esc(advice)}</b>` : ""}</span>`;
}

function renderHero(v) {
  const hb = heroBanner(v);
  const src = art(hb, 960);
  $("#herobanner").innerHTML = src
    ? `<img src="${esc(src)}" alt=""><figcaption>${isOpen(hb) ? `<span class="tag">Open now</span>` : ""}<b>${esc(shortName(hb.name))}</b></figcaption>`
    : "";
  $("#herobanner").hidden = !src;
  const c = v.comparison || {};
  const five = (v.totals.by_rarity || {})["5★"] || 0;
  const s = secs(v.last_sync);
  // calendar days, never negative (a clock a little behind the sync must not read "-1 d ago")
  const day0 = t => { const d = new Date(t * 1000); return Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) / 86400000; };
  const age = s == null ? null : Math.max(0, day0(Date.now() / 1000) - day0(s));
  void five;
  const chips = [
    s == null ? `<span class="stale">Never synced</span>`
      : `<span class="${age >= 3 ? "stale" : ""}">Last sync ${esc(F.date(s))}${age === 0 ? ", today" : `, ${age === 1 ? "yesterday" : `${age}${F.SP}d ago`}`}</span>`,
    `<span>${v.banners.filter(b => b.pulls > 0).length} banners pulled on</span>`,
  ];
  $("#herochips").innerHTML = chips.join("");
  if (v.aliases_error) {
    $("#aliaserr").hidden = false;
    $("#aliaserr").textContent = `The banner name list could not be read (${v.aliases_error}). Closed banners named there show as pulls without a banner until it is fixed.`;
  }
  void c;
}

function renderTiles(v) {
  const c = v.comparison || {};
  const tot = v.totals.by_rarity || {};
  const featured = v.banners.reduce((a, b) => a + (b.cards || []).filter(x => x.is_pickup).reduce((s, x) => s + x.count, 0), 0);
  const pickupPulls = v.banners.filter(b => b.kind === "pickup").reduce((a, b) => a + b.pulls, 0);
  const parts = [`${num(c.covered_pulls)} compared`];
  if (c.excluded_pulls) parts.push(`${num(c.excluded_pulls)} ticket/tutorial`);
  if (c.uncovered_pulls) parts.push(`${num(c.uncovered_pulls)} without rates`);
  let html = `<div class="stat-tile"><div><span class="lbl">Pulls tracked</span><b class="n">${num(v.totals.pulls)}</b><small>${parts.join(" · ")}</small></div></div>`;
  for (const r of RAR) {
    const got = tot[r] || 0;
    const exp = (c.expected_by_rarity || {})[r];
    html += `<div class="stat-tile g-rt" style="--rc:${COL[r]}"><div><span class="lbl">${stars(r)}</span><b class="n">${num(got)}<em>${v.totals.pulls ? pct1(got / v.totals.pulls) : ""}</em></b>
      <small>${exp == null ? "no rate table to compare" : `${num((c.observed_by_rarity || {})[r] || 0)} of the compared · ${exp.toFixed(1)} expected`}</small></div></div>`;
  }
  html += `<div class="stat-tile"><div><span class="lbl">Featured cards pulled</span><b class="n">${num(featured)}</b><small>on banners that feature them · ${num(pickupPulls)} pickup-banner pulls</small></div></div>`;
  $("#tiles").innerHTML = html;
}

// ---------------------------------------------------------------- banners
function rarityBar(byR, total) {
  if (!total) return `<div class="g-rbar empty"></div>`;
  return `<div class="g-rbar" role="img" aria-label="${RAR.map(r => `${r} ${byR[r] || 0}`).join(", ")}">${RAR.map(r => {
    const n = byR[r] || 0;
    return n ? `<i style="flex:${n};background:${COL[r]}" title="${r}: ${n}"></i>` : "";
  }).join("")}</div>`;
}
function flags(b) {
  const f = [];
  if (isOpen(b)) f.push(`<span class="tag">Open now</span>`);
  if (b.excluded_from_rates) f.push(`<span class="g-flag">${esc(b.kind)} · not in the luck figures</span>`);
  else if (b.is_alias) f.push(`<span class="g-flag">closed · rates never saved</span>`);
  else if (!b.has_rates && b.gacha_id) f.push(`<span class="g-flag">no rate table saved</span>`);
  if (!b.gacha_id) f.push(`<span class="g-flag warn">no banner</span>`);
  return f.join("");
}
function bannerCard(b, i) {
  const src = art(b, 640);
  const exp = b.expected_by_rarity || {};
  const pity = b.pulls > 0 && !b.excluded_from_rates && typeof b.pulls_since_last_5 === "number"
    ? `<span><b class="n">${num(b.pulls_since_last_5)}</b> since last ★5</span>` : "";
  const pts = b.counters && b.counters.gacha_point != null ? `<span><b class="n">${num(b.counters.gacha_point)}</b> gacha points</span>` : "";
  return `<button class="g-bcard${src ? "" : " noart"}" type="button" data-i="${i}">
    <div class="g-bart">${src ? `<img src="${esc(src)}" alt="" loading="lazy">` : ""}</div>
    <div class="g-bmeta">
      ${flags(b) ? `<div class="g-flags">${flags(b)}</div>` : ""}
      <b class="g-bn">${esc(bname(b.name))}</b>
      <small>${esc(period(b) || (b.gacha_id ? "Dates not captured" : "Banner unknown"))}</small>
      <div class="g-bpulls"><b class="n">${num(b.pulls)}</b> pulls ${rarityBar(b.by_rarity || {}, b.pulls)}</div>
      <div class="g-brar">${RAR.map(r => `<span><i style="background:${COL[r]}"></i>${r.replace("★", "")}★ <b class="n">${(b.by_rarity || {})[r] || 0}</b>${exp[r] != null ? `<em class="n">/${exp[r].toFixed(1)}</em>` : ""}</span>`).join("")}</div>
      ${pity || pts ? `<div class="g-bextra">${pity}${pts}</div>` : ""}
    </div></button>`;
}
// unattributed pulls become pseudo-banners so they are visible and openable
function unidBanners(v) {
  return (v.unidentified || []).map(u => {
    const twin = v.banners.find(b => bname(b.name) === bname(u.gacha_name));
    return { name: u.gacha_name, gacha_id: null, pulls: u.pulls, by_rarity: u.by_rarity, expected_by_rarity: {},
      cards: u.cards || [], art: {}, has_rates: false, excluded_from_rates: false, counters: null, pickup_card_ids: [], twin };
  });
}
let BANNERS = [];
function renderBanners(v) {
  const pulled = v.banners.filter(b => b.pulls > 0);
  BANNERS = [...pulled, ...unidBanners(v)];
  $("#banners").innerHTML = pulled.map(bannerCard).join("") || `<p class="g-empty">No pulls tracked yet. Press Sync.</p>`;
  const unidCards = BANNERS.slice(pulled.length);
  $("#unidblock").hidden = !unidCards.length;
  $("#unidbanners").innerHTML = unidCards.map((b, i) => bannerCard(b, pulled.length + i)).join("");
  const open = e => { const c = e.target.closest(".g-bcard"); if (c) openBanner(BANNERS[+c.dataset.i]); };
  $("#banners").onclick = open; $("#unidbanners").onclick = open;
  const unp = v.banners.filter(b => b.pulls === 0);
  $("#unpulled").hidden = !unp.length;
  $("#unpulled").innerHTML = `<h3>Seen at sync, not pulled on</h3><div class="g-unrow">${unp.map(b => {
    const src = art(b, 320);
    return `<div class="g-un">${src ? `<img src="${esc(src)}" alt="" loading="lazy">` : ""}<span><b>${esc(bname(b.name))}</b><small>${esc(period(b) || "")}</small></span></div>`;
  }).join("")}</div>`;
  const unid = (v.unidentified || []).reduce((a, u) => a + u.pulls, 0);
  if (unid) $("#bannernote").innerHTML = `Every banner you pulled on, newest first. <b class="n">${unid}</b> pulls came back without a banner; they are listed last.`;
}

function luckMini(b) {
  if (!b.gacha_id) return `<p class="g-note">The server returned these pulls without a banner id, so there is no rate table to compare them with.${b.twin ? ` A banner with this name is in the store (${esc(period(b.twin) || "dates unknown")}); add <code>${esc(QUOTE + b.name + QUOTE)}: {${QUOTE}gacha_id${QUOTE}: ${QUOTE}${esc(b.twin.gacha_id)}${QUOTE}, ${QUOTE}kind${QUOTE}: ${QUOTE}${esc(b.twin.kind)}${QUOTE}}</code> to <code>data/gacha/banner_aliases.json</code> to count them there.` : ""}</p>`;
  if (b.excluded_from_rates) return `<p class="g-note">${esc(b.kind)} pulls are tracked but not compared: they were not bought with a rate roll.</p>`;
  if (!b.has_rates) return `<p class="g-note">No rate table was saved for this banner (it closed before a sync captured one), so there is nothing to compare against.</p>`;
  return luckRows(b.by_rarity || {}, b.expected_by_rarity || {}, b.pulls);
}
function openBanner(b) {
  const src = art(b, 1280);
  $("#bd-hero").innerHTML = src ? `<img src="${esc(src)}" alt="">` : "";
  $("#bd-hero").hidden = !src;
  $("#bd-kind").textContent = b.gacha_id ? `${b.kind || ""} banner`.trim() : "Pulls without a banner";
  $("#bd-title").textContent = bname(b.name);
  const chips = [];
  if (period(b)) chips.push(period(b));
  chips.push(`${num(b.pulls)} pulls tracked`);
  if (b.counters && b.counters.gacha_point != null) chips.push(`${num(b.counters.gacha_point)} gacha points`);
  if (b.pulls && !b.excluded_from_rates && b.gacha_id) chips.push(`${num(b.pulls_since_last_5)} pulls since the last ★5`);
  $("#bd-chips").innerHTML = chips.map(c => `<span>${esc(c)}</span>`).join("");

  const pulledCount = new Map((b.cards || []).map(c => [c.card_id, c.count]));
  let html = "";
  if ((b.pickup_card_ids || []).length) {
    html += `<h3 class="g-bd-sec">Featured</h3><div class="card-grid g-cards">${b.pickup_card_ids.map(id => {
      const n = pulledCount.get(id) || 0, m = meta(id);
      return `<div class="g-tile${n ? "" : " missing"}">${face(id)}<div class="g-cap"><b>${esc(m.member)}</b><small>${esc(m.sub)}</small>${n ? `<span class="g-got">Pulled ×${n}</span>` : `<span class="tag muted">Not pulled</span>`}</div></div>`;
    }).join("")}</div>`;
  }
  html += `<h3 class="g-bd-sec">Luck on this banner</h3>${luckMini(b)}`;
  // pulls the server returned WITHOUT an id but under this banner's name: named, never merged
  const twin = b.gacha_id && (VIEW.unidentified || []).find(u => bname(u.gacha_name) === bname(b.name));
  if (twin) {
    const fiveIds = (twin.cards || []).filter(c => c.rarity === "5★").map(c => meta(c.card_id).member);
    html += `<p class="g-alert">${twin.pulls} more pulls carry this banner's name but came back without a banner id${fiveIds.length ? `, including ★5 ${esc(fiveIds.join(", "))}` : ""}. They are not counted on this banner; they appear under "Pulls without a banner".</p>`;
  }
  for (const r of RAR) {
    const cards = (b.cards || []).filter(c => c.rarity === r);
    if (!cards.length) continue;
    cards.sort((a, z) => (z.is_pickup - a.is_pickup) || (z.count - a.count) || meta(a.card_id).member.localeCompare(meta(z.card_id).member));
    html += `<h3 class="g-bd-sec">${stars(r)} <span>${cards.reduce((a, c) => a + c.count, 0)} pulled</span></h3><div class="card-grid g-cards">${cards.map(c => tileHTML(c.card_id, c.count, c.is_pickup, c.rate_ppm)).join("")}</div>`;
  }
  if (!(b.cards || []).length) html += `<p class="g-empty">No cards recorded for this banner yet.</p>`;
  $("#bd-content").innerHTML = html;
  $("#bdlg").showModal();
  $("#bdlg").scrollTop = 0;
}

// ---------------------------------------------------------------- featured on open banners
// "Is it worth pulling". What the page shows: ownership and bloom
// from the collection. The score gain of each featured card to the best team is not served yet
// (it needs an acquire run per featured card): the page says so instead of guessing.
// Layout: one block per banner, laid out with auto-fit so two or three blocks share the
// row instead of leaving empty tracks; each block is the banner (art + name once) beside ONE row of cards.
function renderFeatured(v) {
  const open = v.banners.filter(b => isOpen(b) && (b.pickup_card_ids || []).length);
  $("#featblock").hidden = !open.length;
  if (!open.length) return;
  const known = META.size > 0;
  // banners that feature the same cards (a "take your pick" twin of a step-up) share one block
  const groups = new Map();
  for (const b of open) {
    const k = [...b.pickup_card_ids].sort().join(",");
    if (!groups.has(k)) groups.set(k, { ids: b.pickup_card_ids, banners: [] });
    groups.get(k).banners.push(b);
  }
  $("#featnote").textContent = `${groups.size} open banner${groups.size === 1 ? "" : "s"} with featured cards. `
    + (known ? "Owned and Bloom as of the last refresh. " : "The collection could not be read, so ownership is unknown. ")
    + "Score gain for each card: not computed yet.";
  $("#featured").innerHTML = `<div class="g-featgrid">${[...groups.values()].map(({ ids, banners }) => {
    const owned = ids.filter(id => (META.get(id) || {}).owned).length;
    // the plain banner names the block; a "take your pick" twin is a tag that opens its own banner
    const main = banners.find(b => !pickLabel(b.name)) || banners[0];
    const src = art(banners.find(b => art(b, 1)) || main, 640);
    const tags = banners.filter(b => b !== main).map(b =>
      `<button class="tag g-tagbtn" type="button" data-g="${esc(b.gacha_id)}">${esc(pickLabel(b.name) || shortName(b.name))}</button>`).join("");
    return `<article class="g-featrow" style="--n:${ids.length}">
      <div class="g-feathead">
        ${src ? `<button class="g-featart" type="button" data-g="${esc(main.gacha_id)}" aria-label="Open ${esc(shortName(main.name))}"><img src="${esc(src)}" alt="" loading="lazy"></button>` : ""}
        <button class="g-lnk g-featname" type="button" data-g="${esc(main.gacha_id)}">${esc(shortName(main.name))}</button>
        ${pickLabel(main.name) ? `<span class="tag">${esc(pickLabel(main.name))}</span>` : ""}${tags}
        <small>${esc(period(main) || F.NA)}</small>
        ${known ? `<small><b class="n">${owned}</b> of <b class="n">${ids.length}</b> owned</small>` : ""}
      </div>
      <div class="g-featcards">${ids.map(id => {
        const c = META.get(id), m = meta(id);
        const st = !known ? "" : c && c.owned
          ? `<span class="g-got">Owned · Bloom ${F.or(c.bloom)}${c.max_bloom != null ? `/${c.max_bloom}` : ""}</span>`
          : `<span class="g-not">Not owned</span>`;
        return `<div class="g-tile">${face(id)}<div class="g-cap"><b>${esc(m.member)}</b><small>${esc(m.sub)}</small>${st}</div></div>`;
      }).join("")}</div></article>`;
  }).join("")}</div>`;
  $("#featured").onclick = e => { const t = e.target.closest("[data-g]"); if (t) openBanner(BANNERS.find(b => b.gacha_id === t.dataset.g) || v.banners.find(b => b.gacha_id === t.dataset.g)); };
}

// ---------------------------------------------------------------- luck
function verdict(row, r) {
  const worse = Math.round(row.below * 100), better = Math.round(row.above * 100);
  if (r === "3★") return row.k < row.lo ? "fewer than usual" : row.k > row.hi ? "more than usual" : "within the usual range";
  const tail = `${worse} % of players would get fewer, ${better} % more`;
  if (row.pct <= 0.1) return `<b class="bad">unlucky</b> · ${tail}`;
  if (row.pct >= 0.9) return `<b class="good">lucky</b> · ${tail}`;
  return `within the usual range · ${tail}`;
}
function luckRows(obs, exp, n) {
  const rows = RAR.map(r => ({ r, row: luckRow(obs[r] || 0, exp[r], n) })).filter(x => x.row);
  if (!rows.length) return `<p class="g-empty">No expectation to compare against.</p>`;
  return `<div class="g-luck">${rows.map(({ r, row }) => {
    const max = Math.max(row.hi, row.k, Math.ceil(row.expected)) * 1.18 + 1;
    const x = v => `${(100 * v / max).toFixed(2)}%`;
    const tip = `${r}: ${row.k} pulled, ${row.expected.toFixed(1)} expected; 90 % of players with these ${n} pulls get ${row.lo} to ${row.hi}`;
    return `<div class="g-lk" style="--rc:${COL[r]}">
      <div class="g-lk-r">${stars(r)}</div>
      <div class="g-lplot" title="${esc(tip)}">
        <div class="g-lband" style="left:${x(row.lo)};width:calc(${x(row.hi - row.lo)} + 2px)"></div>
        <div class="g-ltick" style="left:${x(row.expected)}"></div>
        <div class="g-ldot" style="left:${x(row.k)}"></div>
        <span class="g-lax n" style="left:0">0</span>${row.lo > 0 ? `<span class="g-lax n" style="left:${x(row.lo)}">${row.lo}</span>` : ""}<span class="g-lax n" style="left:${x(row.hi)}">${row.hi}</span>
      </div>
      <div class="g-lnum"><b class="n">${row.k}</b><span class="n"> / ${row.expected.toFixed(1)} exp.</span>
        <small>${verdict(row, r)}</small>
        <small class="rate">rate ${pct1(row.rate)} (95${F.SP}%: ${pct1(row.rateCI[0])} to ${pct1(row.rateCI[1])}) · stated ${pct1(row.p)}</small></div>
    </div>`;
  }).join("")}</div>`;
}
function renderLuck(v) {
  const c = v.comparison || {};
  $("#lucknote").textContent = c.covered_pulls
    ? `${num(c.covered_pulls)} of ${num(v.totals.pulls)} pulls are compared: those from banners whose rate table was saved, minus ticket and tutorial pulls.`
    : "No banner in the store has a saved rate table yet.";
  let html = luckRows(c.observed_by_rarity || {}, c.expected_by_rarity || {}, c.covered_pulls || 0);
  html += `<div class="g-legend"><span><i class="k-band"></i>90 % of players land here</span><span><i class="k-tick"></i>expected</span><span><i class="k-dot"></i>you</span></div>`;
  $("#luck").innerHTML = html;

  const covered = v.banners.filter(b => b.has_rates && !b.excluded_from_rates && b.pulls > 0);
  const rowsHTML = covered.map(b => {
    const r5 = luckRow((b.by_rarity || {})["5★"] || 0, (b.expected_by_rarity || {})["5★"], b.pulls);
    const cell = r => `<td class="n">${(b.by_rarity || {})[r] || 0}<em>/${((b.expected_by_rarity || {})[r] ?? 0).toFixed(1)}</em></td>`;
    return `<tr><td><button type="button" class="g-lnk" data-g="${esc(b.gacha_id)}">${esc(bname(b.name))}</button></td><td class="n">${num(b.pulls)}</td>${cell("5★")}${cell("4★")}
      <td class="n">${r5 ? `${r5.lo}-${r5.hi}` : F.NA}</td><td class="n">${r5 ? F.pct(r5.above, { d: 0 }) : F.NA}</td></tr>`;
  }).join("");
  const left = v.banners.filter(b => b.pulls > 0 && !(b.has_rates && !b.excluded_from_rates));
  const unid = (v.unidentified || []);
  $("#luckbanners").innerHTML = `<table class="g-ltable"><thead><tr><th>Banner</th><th>Pulls</th><th>★5 / exp.</th><th>★4 / exp.</th><th title="90 % of players with these pulls get this many ★5">★5 range</th><th title="share of players making the same pulls who would get MORE ★5 than you">Would get more ★5</th></tr></thead><tbody>${rowsHTML}</tbody></table>
    ${left.length || unid.length ? `<p class="g-note">Not compared: ${[...left.map(b => `${esc(bname(b.name))} (${b.excluded_from_rates ? esc(b.kind) : "no rate table"}, ${b.pulls})`),
      ...unid.map(u => `${esc(bname(u.gacha_name))} (no banner, ${u.pulls})`)].join(" · ")}.</p>` : ""}
    <p class="g-note">A single banner rarely says anything about luck: the overall figures are the ones to read.</p>`;
  $("#luckbanners").onclick = e => { const t = e.target.closest(".g-lnk"); if (t) openBanner(BANNERS.find(b => b.gacha_id === t.dataset.g)); };
}


// ---------------------------------------------------------------- how the rates work
// a stated rate as the game would print it: 85 %, 0.5 %, 0.333 % (trailing zeros of the decimals only)
const pcts = x => { const t = (+x).toFixed(+x >= 10 ? 0 : 3); return `${t.includes(".") ? t.replace(/0+$/, "").replace(/[.]$/, "") : t}${F.SP}%`; };
function split(rates) {
  const parts = RAR.filter(r => rates[r] != null);
  return `<div class="g-split">${parts.map(r => `<i style="width:${rates[r]}%;background:${COL[r]}"></i>`).join("")}</div>
    <div class="g-splitk">${parts.map(r => `<span><i style="background:${COL[r]}"></i>${r} ${pcts(rates[r])}</span>`).join("")}</div>`;
}
function renderRates(v) {
  const withFacts = v.banners.filter(b => b.rate_facts);
  if (!withFacts.length) { $("#ratesblock").hidden = true; return; }
  $("#ratesblock").hidden = false;
  const normal = withFacts.find(b => b.rate_facts.table === "normal" && b.kind !== "fixed") || withFacts[0];
  const nf = normal.rate_facts;
  const pickup = withFacts.find(b => b.kind === "pickup" && b.rate_facts.table === "normal" && b.rate_facts.featured.length);
  const guaranteed = withFacts.find(b => (b.rate_facts.slot10["5★"] || 0) >= 100);
  const selects = withFacts.filter(b => b.rate_facts.table === "card_select");
  const unpicked = selects.filter(b => b.rate_facts.picked === false);
  const cost = (() => {   // the 1-pull button's price, from the banner as the game sent it
    const b = withFacts.find(x => x.cost_per_pull);
    return b ? b.cost_per_pull : null;
  })();
  const facts = [
    `<div class="g-fact"><span class="g-flbl">Every pull</span>${split(nf.rates)}<p>The same odds on every banner the game has served: ${pcts(nf.rates["5★"])} ★5, ${pcts(nf.rates["4★"])} ★4, ${pcts(nf.rates["3★"])} ★3.</p></div>`,
    `<div class="g-fact"><span class="g-flbl">10-pull</span>${split(nf.slot10)}<p>The 10th card comes from its own table: ★4 or better, with the same ${pcts(nf.slot10["5★"] || 0)} chance of ★5.${guaranteed ? ` On a guaranteed banner (${esc(bname(guaranteed.name))}) that card is ★5 every time.` : ""}</p></div>`,
    pickup ? `<div class="g-fact"><span class="g-flbl">Featured members</span><b class="big n">${pcts(pickup.rate_facts.featured_total)}</b><p>of the ${pcts(pickup.rate_facts.rates["5★"])} ★5 goes to the featured members (${pickup.rate_facts.featured.length} on ${esc(bname(pickup.name))}, ${pcts(pickup.rate_facts.featured[0].pct)} each). Every other ★5 is about ${pcts(pickup.rate_facts.other_5_each)}.</p></div>` : "",
    selects.length ? `<div class="g-fact${unpicked.length ? " warn" : ""}"><span class="g-flbl">Take your pick!</span><b class="big n">${pcts(5)} vs ${pcts(4)}</b><p>Choose your member before pulling: the chosen one gets ${pcts(1)} of its own and ★5 is ${pcts(5)}. With none chosen that ${pcts(1)} is simply missing: ★5 drops to ${pcts(4)}.${unpicked.length ? ` ${unpicked.length} of your ${selects.length} saved Take your pick tables were read with no member chosen.` : ""}</p></div>` : "",
  ].join("");
  const open = withFacts.filter(b => b.is_live_last_sync && b.rate_facts.featured.length);
  const tab = b => {
    const f = b.rate_facts;
    const pick = f.table === "card_select"
      ? `<span class="g-pick ${f.picked ? "yes" : "no"}">${f.picked ? "A member is chosen: ★5 is " + pcts(f.rates["5★"]) : "No member chosen yet: ★5 is " + pcts(f.rates["5★"]) + ". Choose one to get " + pcts(5)}</span>` : "";
    return `<article class="g-rtab"><h4>${esc(bname(b.name))}</h4><span class="meta">${esc(period(b))}${cost ? ` · ${num(cost)} per pull` : ""}</span>${split(f.rates)}
      <div class="g-feat">${f.featured.map(c => { const m = meta(c.card_id);
        return `<div><img src="/cardart/vert/${esc(c.card_id)}?w=96" alt="" loading="lazy"><span><b>${pcts(c.pct)}</b>${esc(m.member)}</span></div>`; }).join("")}</div>${pick}</article>`;
  };
  $("#rates").innerHTML = `<div class="g-facts4">${facts}</div>${open.length ? `<div class="g-ratetabs">${open.map(tab).join("")}</div>` : ""}`;
}

// ---------------------------------------------------------------- luck charts
function renderCurve(v) {
  const pts = v.curve || [];
  if (!pts.length) { $("#curve").innerHTML = `<p class="g-empty">No compared pulls yet.</p>`; return; }
  const W = 760, H = 300, L = 40, R = 70, T = 16, B = H - 30;
  const n = pts[pts.length - 1][0];
  const top = Math.max(...pts.map(p => Math.max(p[1], p[2] + 1.645 * Math.sqrt(p[3])))) + 1;
  const x = k => L + (W - L - R) * k / n, y = c => B - (B - T) * c / top;
  const band = pts.map(p => [p[0], Math.max(0, p[2] - 1.645 * Math.sqrt(p[3])), p[2] + 1.645 * Math.sqrt(p[3])]);
  let s = `<svg viewBox="0 0 ${W} ${H}" class="cchart" role="img" aria-label="★5 obtained against the expected count, pull by pull">`;
  const step = Math.max(1, Math.round(top / 4));
  for (let c = 0; c <= top; c += step) s += `<line x1="${L}" x2="${W - R}" y1="${y(c)}" y2="${y(c)}" class="dg"/><text x="${L - 8}" y="${y(c) + 4}" text-anchor="end" class="dax">${c}</text>`;
  for (const k of [0, Math.round(n / 4), Math.round(n / 2), Math.round(3 * n / 4), n]) s += `<text x="${x(k)}" y="${H - 8}" text-anchor="middle" class="dax">${k}</text>`;
  s += `<path class="band" d="M${band.map(b => `${x(b[0])},${y(b[2])}`).join("L")}L${band.slice().reverse().map(b => `${x(b[0])},${y(b[1])}`).join("L")}Z"/>`;
  s += `<path class="exp" d="M${x(0)},${y(0)}L${pts.map(p => `${x(p[0])},${y(p[2])}`).join("L")}"/>`;
  let d = `M${x(0)},${y(0)}`, prev = 0;
  for (const p of pts) { d += `L${x(p[0])},${y(prev)}L${x(p[0])},${y(p[1])}`; prev = p[1]; }
  s += `<path class="obs" d="${d}"/>`;
  const last = pts[pts.length - 1];
  s += `<circle cx="${x(last[0])}" cy="${y(last[1])}" r="6" class="end"/><text x="${x(last[0]) + 10}" y="${y(last[1]) + 4}" class="lab">${last[1]} you</text>`;
  s += `<text x="${x(last[0]) + 10}" y="${y(last[2]) + (last[2] > last[1] ? -6 : 16)}" class="dax">${last[2].toFixed(1)} exp.</text></svg>`;
  const sd = Math.sqrt(last[3]), z = (last[1] - last[2]) / (sd || 1);
  const word = z <= -1.645 ? "below the usual range" : z >= 1.645 ? "above the usual range" : "inside the usual range";
  $("#curvenote").textContent = `${num(n)} compared pulls`;
  $("#curve").innerHTML = `<div class="g-legend"><span><i class="k-line"></i>your ★5 so far</span><span><i class="k-dash"></i>expected from each banner's rates</span><span><i class="k-band"></i>90 % of players</span></div>${s}
    <p class="g-note">${last[1]} ★5 against ${last[2].toFixed(1)} expected: ${word}. The line steps up at each ★5; the shaded band is where 90 % of players making the same pulls would be at every point.</p>`;
}
function renderGaps(v) {
  const g = (v.gaps || {}).gaps || [];
  if (!g.length) { $("#gaps").innerHTML = `<p class="g-empty">No ★5 yet.</p>`; return; }
  const p5 = 0.05, size = 10, nb = 8;   // bins of 10 pulls; the last is "70 or more"
  const bins = new Array(nb).fill(0);
  for (const x of g) bins[Math.min(nb - 1, Math.floor((x - 1) / size))]++;
  // a gap of k pulls is geometric: P(gap in [a, b]) = (1-p)^(a-1) - (1-p)^b
  const expBin = i => g.length * (Math.pow(1 - p5, i * size) - (i === nb - 1 ? 0 : Math.pow(1 - p5, (i + 1) * size)));
  const W = 460, H = 260, L = 30, R = 8, T = 16, B = H - 34;
  const top = Math.max(...bins, ...bins.map((_, i) => expBin(i))) + 1;
  const bw = (W - L - R) / nb, y = c => B - (B - T) * c / top;
  let s = `<svg viewBox="0 0 ${W} ${H}" class="cchart" role="img" aria-label="How many pulls each ★5 took, against the expected spread">`;
  for (const c of [0, Math.round(top / 2), Math.floor(top)]) s += `<line x1="${L}" x2="${W - R}" y1="${y(c)}" y2="${y(c)}" class="dg"/><text x="${L - 6}" y="${y(c) + 4}" text-anchor="end" class="dax">${c}</text>`;
  bins.forEach((c, i) => {
    const x0 = L + i * bw;
    s += `<g><title>${i === nb - 1 ? `${i * size + 1}+` : `${i * size + 1}-${(i + 1) * size}`} pulls: ${c} time${c === 1 ? "" : "s"} (${expBin(i).toFixed(1)} expected)</title>`;
    if (c) s += `<rect x="${x0 + 4}" y="${y(c)}" width="${bw - 8}" height="${B - y(c)}" rx="4" class="gbar"/>`;
    s += `<circle cx="${x0 + bw / 2}" cy="${y(expBin(i))}" r="4" class="gexpd"/></g>`;
    s += `<text x="${x0 + bw / 2}" y="${H - 18}" text-anchor="middle" class="dax">${i === nb - 1 ? `${i * size + 1}+` : `${i * size + 1}-${(i + 1) * size}`}</text>`;
  });
  s += `<path class="gexp" d="M${bins.map((_, i) => `${L + i * bw + bw / 2},${y(expBin(i))}`).join("L")}"/></svg>`;
  const mean = g.reduce((a, x) => a + x, 0) / g.length;
  $("#gapnote").textContent = `${g.length} ★5 draws`;
  $("#gaps").innerHTML = `<div class="g-legend"><span><i class="k-line"></i>how often it took that many pulls</span><span><i class="k-dash"></i>expected at ${pcts(5)}</span></div>${s}
    <div class="g-stat3"><div><small>Average</small><b class="n">${mean.toFixed(1)}</b><small>pulls per ★5 (≈ ${Math.round(1 / p5)} expected)</small></div>
      <div><small>Longest</small><b class="n">${Math.max(...g)}</b><small>pulls</small></div>
      <div><small>Since the last ★5</small><b class="n">${(v.gaps || {}).open || 0}</b><small>pulls</small></div></div>
    <p class="g-note">Counted over every pull in order, tickets included. Cards inside one multi-pull share a time, so a 10-pull with a ★5 closes the stretch with all ten.</p>`;
}

// ---------------------------------------------------------------- history
function fillDays(rows) {
  if (!rows.length) return [];
  const at = new Map(rows.map(r => [r.day, r]));
  const first = Date.parse(rows[0].day + "T00:00:00Z"), last = Date.parse(rows[rows.length - 1].day + "T00:00:00Z");
  const out = [];
  for (let t = first; t <= last && out.length < 4000; t += 86400000) {
    const k = new Date(t).toISOString().slice(0, 10), r = at.get(k);
    out.push({ day: k, pulls: r ? r.pulls : 0, five_star: r ? r.five_star : 0 });
  }
  return out;
}
function renderDays(v) {
  const rows = fillDays(v.timeline || []);
  if (!rows.length) { $("#days").innerHTML = `<p class="g-empty">No dated pulls yet.</p>`; return; }
  const W = 1100, H = 190, L = 34, R = 8, T = 22, B = H - 28;
  const max = Math.max(1, ...rows.map(r => r.pulls));
  const slot = (W - L - R) / rows.length, bw = Math.max(2, Math.min(18, slot - 2));
  const y = val => B - (B - T) * val / max;
  const step = Math.max(1, Math.ceil(rows.length / 9));
  let s = `<svg viewBox="0 0 ${W} ${H}" class="dchart" role="img" aria-label="Pulls per day; days with a ★5 are marked">`;
  for (const val of [0, Math.round(max / 2), max]) {
    s += `<line x1="${L}" x2="${W - R}" y1="${y(val)}" y2="${y(val)}" class="dg"/><text x="${L - 8}" y="${y(val) + 4}" text-anchor="end" class="dax">${val}</text>`;
  }
  rows.forEach((r, i) => {
    const cx = L + i * slot + slot / 2;
    const tip = `${fmtDate(r.day)}: ${r.pulls} pull${r.pulls === 1 ? "" : "s"}${r.five_star ? ` · ${r.five_star} ★5` : ""}`;
    s += `<g class="dcol"><title>${esc(tip)}</title><rect x="${L + i * slot}" y="${T - 14}" width="${slot}" height="${B - T + 14}" class="dhit"/>`;
    if (r.pulls) { const h = Math.max(3, B - y(r.pulls)); s += `<rect x="${cx - bw / 2}" y="${B - h}" width="${bw}" height="${h}" rx="3" class="dbar"/>`; }
    if (r.five_star) {
      const dy = (r.pulls ? y(r.pulls) : B) - 9;
      s += `<circle cx="${cx}" cy="${dy}" r="5" class="d5"/>`;
      if (r.five_star > 1) s += `<text x="${cx}" y="${dy - 9}" text-anchor="middle" class="dval">×${r.five_star}</text>`;
    }
    s += `</g>`;
    if (i % step === 0 || i === rows.length - 1) {
      if (i === rows.length - 1 || rows.length - 1 - i >= step / 2)
        s += `<text x="${cx}" y="${H - 8}" text-anchor="middle" class="dax">${esc(F.day(r.day))}</text>`;
    }
  });
  s += `</svg>`;
  const five = rows.reduce((a, r) => a + r.five_star, 0);
  $("#days").innerHTML = `<div class="g-legend"><span><i class="k-bar"></i>pulls that day</span><span><i class="k-dot"></i>a ★5 came out (×n when several)</span></div>${s}`;
  $("#daysnote").textContent = `${rows.length} days, ${fmtDate(rows[0].day)} to ${fmtDate(rows[rows.length - 1].day)} · ${num(rows.reduce((a, r) => a + r.pulls, 0))} pulls · ${five} ★5 · days in UTC`;
}

function tileHTML(id, count, pickup, ratePpm, extra) {
  const m = meta(id);
  const tip = `${m.member}${m.sub ? " · " + m.sub : ""}${ratePpm != null ? ` · rate ${(ratePpm / 1e5).toFixed(3)} %` : ""}`;
  // the count badge goes INSIDE the face (bottom-left, where the level would sit: no level is drawn here)
  const f = face(id), badge = count > 1 ? `<span class="g-cnt n">×${count}</span>` : "";
  const at = f.lastIndexOf("</div></div>");
  return `<div class="g-tile" title="${esc(tip)}">${at < 0 ? f + badge : f.slice(0, at) + badge + f.slice(at)}
    <div class="g-cap"><b>${esc(m.member)}</b><small>${esc(m.sub)}</small>${pickup ? `<span class="g-feat">Featured</span>` : ""}${extra || ""}</div></div>`;
}
function allEntries(v) {
  const out = [];
  v.banners.forEach(b => (b.cards || []).forEach(c => out.push({ ...c, key: "b:" + b.gacha_id, banner: bname(b.name) })));
  (v.unidentified || []).forEach(u => (u.cards || []).forEach(c => out.push({ ...c, key: "u:" + u.gacha_name, banner: bname(u.gacha_name) + " (no banner)" })));
  return out;
}
function renderResults() {
  const v = VIEW;
  const entries = allEntries(v);
  const total = new Map();
  for (const e of entries) total.set(e.card_id, (total.get(e.card_id) || 0) + e.count);
  let list;
  if (STATE.banner) list = entries.filter(e => e.key === STATE.banner).map(e => ({ ...e, banners: [e.banner] }));
  else {
    const by = new Map();
    for (const e of entries) {
      const x = by.get(e.card_id) || { ...e, count: 0, banners: [], is_pickup: false };
      x.count += e.count; x.banners.push(e.banner); x.is_pickup = x.is_pickup || e.is_pickup;
      by.set(e.card_id, x);
    }
    list = [...by.values()];
  }
  if (STATE.rarity) list = list.filter(e => e.rarity === STATE.rarity);
  if (STATE.show === "pickup") list = list.filter(e => e.is_pickup);
  if (STATE.show === "dupe") list = list.filter(e => total.get(e.card_id) > 1);
  list.sort((a, z) => rint(z.rarity) - rint(a.rarity) || (z.is_pickup - a.is_pickup) || z.count - a.count || meta(a.card_id).member.localeCompare(meta(z.card_id).member));
  const pulls = list.reduce((a, e) => a + e.count, 0);
  $("#fcount").textContent = `${list.length} cards · ${pulls} pulls`;
  $("#results").innerHTML = list.map(e => {
    const t = total.get(e.card_id);
    const where = e.banners.length > 1 ? `${e.banners.length} banners` : e.banners[0];
    const extra = `<small class="g-where">${esc(where)}${STATE.banner && t > e.count ? ` · ×${t} in all` : ""}</small>`;
    return tileHTML(e.card_id, e.count, e.is_pickup, e.rate_ppm, extra);
  }).join("") || `<p class="g-empty">No card matches these filters.</p>`;
}
function renderHistory(v) {
  renderDays(v);
  const opts = [...v.banners.filter(b => b.pulls > 0).map(b => [`b:${b.gacha_id}`, `${bname(b.name)} (${b.pulls})`]),
    ...(v.unidentified || []).map(u => [`u:${u.gacha_name}`, `${bname(u.gacha_name)}, no banner (${u.pulls})`])];
  $("#fbanner").innerHTML = `<option value="">All banners (${num(v.totals.pulls)})</option>` + opts.map(([k, l]) => `<option value="${esc(k)}">${esc(l)}</option>`).join("");
  $("#fbanner").value = opts.some(o => o[0] === STATE.banner) ? STATE.banner : "";
  STATE.banner = $("#fbanner").value;
  $("#histnote").textContent = "What came out, per card. The store keeps each draw's time, but this view is served per day and per card, not draw by draw.";
  renderResults();
}

function renderBefore(v) {
  const t = v.tutorial;
  if (!t) {
    $("#beforenote").textContent = "Needs the account snapshot (refresh the account). Nothing here is guessed from the pull history.";
    $("#before").innerHTML = ""; return;
  }
  const rows = t.owned_not_tracked || [];
  const tut = rows.filter(r => r.is_tutorial).length;
  $("#beforenote").textContent = rows.length
    ? `${rows.length} owned cards never appear in the tracked pulls: acquired before tracking began, or from a source the history does not report.${tut ? ` ${tut} came from the guaranteed tutorial pull.` : ""}`
    : "Every owned card appears in the tracked pulls.";
  rows.sort((a, z) => rint(z.rarity) - rint(a.rarity) || (a.acquired || 0) - (z.acquired || 0));
  $("#before").innerHTML = rows.map(r => tileHTML(r.card_id, 1, false, null,
    `<small class="g-where">Acquired ${esc(fmtDate(r.acquired))}</small>${r.is_tutorial ? `<span class="tag muted">Tutorial</span>` : ""}`)).join("");
}

// ---------------------------------------------------------------- sync / export
async function doSync() {
  const btn = $("#sync"), st = $("#syncstatus");
  btn.disabled = true; btn.classList.add("busy");
  st.textContent = "Syncing: signing in and reading the pull history and rate tables…";
  try {
    const j = await getJSON("/api/gacha/sync", { method: "POST" });
    const s = j.summary;
    st.textContent = s
      ? `Sync done · +${s.pulls} pulls · +${s.banners} banners · ${s.rates} rate tables${s.possible_gap ? " · possible gap: the game keeps only the last 30 days (up to 100 pulls) and its history starts after everything saved" : ""}`
      : `Sync finished (exit ${j.returncode}) without a summary; see the server console.`;
    await load();
  } catch (e) {
    st.textContent = "Sync failed: " + e.message;
  } finally { btn.disabled = false; btn.classList.remove("busy"); }
}
async function doExport() {
  const btn = $("#export"), st = $("#syncstatus");
  btn.disabled = true; st.textContent = "Exporting…";
  try {
    const j = await getJSON("/api/gacha/export", { method: "POST" });
    st.textContent = j.path ? `Wrote ${j.path}` : `Export finished (exit ${j.returncode}); see the server console.`;
  } catch (e) { st.textContent = "Export failed: " + e.message; }
  finally { btn.disabled = false; }
}

// ---------------------------------------------------------------- wiring
function seg(id, key) {
  $(id).addEventListener("click", e => {
    const b = e.target.closest("button[data-v]"); if (!b) return;
    STATE[key] = b.dataset.v;
    $(id).querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    renderResults();
  });
}
async function load() {
  const [g, col] = await Promise.allSettled([getJSON("/api/gacha"), getJSON("/api/collection")]);
  if (col.status === "fulfilled") META = new Map((col.value.cards || []).map(c => [c.id, c]));
  if (g.status !== "fulfilled") { $("#herochips").innerHTML = `<span class="stale">Could not load the history: ${esc(g.reason.message)}</span>`; return; }
  VIEW = g.value;
  VIEW.banners = VIEW.banners || [];
  renderLimit(VIEW); renderHero(VIEW); renderTiles(VIEW); renderBanners(VIEW); renderFeatured(VIEW); renderRates(VIEW); renderCurve(VIEW); renderGaps(VIEW); renderLuck(VIEW); renderHistory(VIEW); renderBefore(VIEW);
}
$("#sync").addEventListener("click", doSync);
$("#export").addEventListener("click", doExport);
$("#fbanner").addEventListener("change", e => { STATE.banner = e.target.value; renderResults(); });
seg("#frarity", "rarity"); seg("#fshow", "show");
$("#bd-close").addEventListener("click", () => $("#bdlg").close());
$("#bdlg").addEventListener("click", e => { if (e.target === $("#bdlg")) $("#bdlg").close(); });
load().catch(e => toast(e.message));
