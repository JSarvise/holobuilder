// Inventory page (Resources): everything the account holds, from the last refresh.
// Every count comes from GET /api/resources (inventory.json's live User/Get capture,
// the board's own spent tally, account_snapshot's board points); this file only lays
// it out. Names are the game's own LangItem_Eng strings; a name the local table lacks
// is shown as the raw id, never guessed.

const $ = s => document.querySelector(s);
const QUOTE = String.fromCharCode(34);   // spelled out: a quote inside a regex trips test_js_syntax
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", [QUOTE]: "&quot;" };
const ESC_RE = new RegExp("[&<>" + QUOTE + "]", "g");
const esc = s => String(s ?? "").replace(ESC_RE, c => ESC[c]);
const F = window.HBFmt;   // hb-format.js: shared date/number/placeholder rules
const num = n => F.num(n);
const icon = id => `/itemicon/${encodeURIComponent(id)}`;

// The four board branches: cube/hexa item ids, the branch's node family (it holds for every
// node price row), its Night Deck token (hb.css --br-*, the game's PaletteStore gradient
// ends) and the game's own lit node sprite for the branch (/ui/board/).
const BRANCHES = [
  { key: "leader", colour: "Red", hue: "var(--br-leader)", sprite: "red", feeds: "Leader Skill nodes" },
  { key: "card", colour: "Blue", hue: "var(--br-member)", sprite: "blue", feeds: "Member Skill nodes" },
  { key: "allmember", colour: "Green", hue: "var(--br-support1)", sprite: "green", feeds: "Overall Boost nodes" },
  { key: "content", colour: "Yellow", hue: "var(--br-support2)", sprite: "yellow", feeds: "Song and content nodes" },
];
// The game's element inks (hb.css --cute/--pure/--happy), for the beads/crystals columns.
const ATTRS = [
  { n: 1, name: "Cute", ink: "var(--cute)" },
  { n: 2, name: "Pure", ink: "var(--pure)" },
  { n: 3, name: "Happy", ink: "var(--happy)" },
];
// Board reset: 1,000 Hologold per node reset (main_Setting skillTreeResetConsumptionGoldQuantity).
const RESET_GOLD = 1000;

// Ids shown in the dedicated sections above; "Everything else" takes the rest.
const SHOWN_RE = /^item-(skill_tree_|card_level_up$|card_limit_break-|selectable_reward_box-item-card_limit_break-|gold$|card-potential-upgrade)/;
const GROUPS = [
  { name: "Gacha", re: /^item-gacha/ },
  { name: "Lives and boosts", re: /^item-(live_|mini_game_reward_up)/ },
  { name: "Exchange and shops", re: /^item-(exchange|music_release|character_coin|selectable_reward_box_membership)/ },
  { name: "Minigame coins", re: /^item-mini[-_]game/ },
  { name: "Events", re: /^item-event_/ },
  { name: "Park facilities", re: /^item-facility/ },
  { name: "Park quests", re: /^item-(quest|park_)/ },
  { name: "Other", re: /./ },
];

// An item icon. When the server has no art for it (the item is newer than the last
// `item_icons.py` extraction), a deliberate empty slot says so, in the item's colour when it
// has one: never a borrowed icon, which would be a claim about what the item is.
function img(id, cls, colour) {
  return `<img class="${cls}" src="${icon(id)}" alt="" loading="lazy" data-fallback="${esc(colour || "")}">`;
}
function wireFallbacks(root) {
  root.querySelectorAll("img[data-fallback]").forEach(im => im.addEventListener("error", () => {
    const span = document.createElement("span");
    span.className = im.className + " miss";
    span.title = "No icon on disk for this item: re-run item_icons.py after a refresh";
    if (im.dataset.fallback) span.style.setProperty("--mc", im.dataset.fallback);
    span.innerHTML = "<i>no art</i>";
    im.replaceWith(span);
  }, { once: true }));
}

function parseWhen(s) {
  if (!s) return null;
  const m = /(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})([+-]\d{2})(\d{2})?/.exec(s);
  const d = m ? new Date(`${m[1]}${m[2]}:${m[3] || "00"}`) : new Date(s);
  return isNaN(d) ? null : d;
}
function whenText(d) {
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  return { abs: F.date(d), rel: F.ago(d), stale: mins > 24 * 60 };
}

function heroStats(data, byId) {
  const gold = byId["item-gold"]?.held;
  const lesson = byId["item-card_level_up"]?.held;
  const unspent = (data.holomem_points || []).reduce((a, p) => a + (p.unspent || 0), 0);
  const stats = [
    ["item-gold", "Hologold", gold],
    ["item-card_level_up", "Lesson Pt", lesson],
  ];
  // stat tiles (hb.css): no box of their own, split by a 1 px rule
  const tile = (art, label, v) => `<div class="stat-tile">${art}<div><span class="lbl">${label}</span><span class="n">${num(v)}</span></div></div>`;
  let html = stats.map(([id, label, v]) => tile(img(id, "si"), label, v)).join("");
  html += tile("", "Board Pt invested", data.board_points_spent);
  if ((data.holomem_points || []).length) {
    const holding = data.holomem_points.filter(p => p.unspent > 0).length;
    html += `<div class="stat-tile"><div><span class="lbl">Board Pt unspent</span><span class="n">${num(unspent)}</span>
      <small><a class="inv-lnk" href="/holoboard">${holding} of ${data.holomem_points.length} holomems, on the Holomem Board</a></small></div></div>`;
  }
  $("#invstats").innerHTML = html;
}

function chips(data) {
  const parts = [];
  const d = parseWhen(data.inventory_captured_at) || parseWhen(data.captured_at);
  if (d) {
    const w = whenText(d);
    parts.push(`<span class="${w.stale ? "inv-stale" : ""}" title="${esc([data.captured_at, data.inventory_source].filter(Boolean).join(" · "))}">Read from your account ${esc(w.abs)}, ${esc(w.rel)}</span>`);
  } else if (data.captured_at) {
    parts.push(`<span>Read from your account</span>`);
  }
  parts.push(`<span>${num((data.items || []).length)} items held</span>`);
  $("#invchips").innerHTML = parts.join("");
}

function branches(data) {
  const by = {};
  for (const m of data.board_materials || []) by[m.item_id] = m;
  $("#branches").innerHTML = BRANCHES.map(b => {
    const rows = [1, 2].map(tier => {
      const id = `item-skill_tree_${b.key}-${tier}`;
      const m = by[id] || { name: `${b.colour} ${tier === 1 ? "Cube" : "Hexa"}`, spent: 0, wallet: 0, total: 0 };
      const total = m.total || 0;
      const sp = total ? (m.spent / total) * 100 : 0;
      return `<div class="mat">
        ${img(id, "mi", b.hue)}
        <div class="mt">
          <div class="mline"><span class="mn">${esc(m.name)}</span><b class="num">${num(m.wallet)}</b></div>
          <div class="mbar" role="img" aria-label="${num(m.spent)} invested, ${num(m.wallet)} in the bag"><i style="width:${sp.toFixed(1)}%"></i></div>
          <div class="mleg"><span>invested <b class="num">${num(m.spent)}</b></span><span>total <b class="num">${num(total)}</b></span></div>
        </div>
      </div>`;
    }).join("");
    return `<article class="branch" style="--bh:${b.hue}">
      <header><img class="bm" src="/ui/board/ui_710_ico_node_${b.sprite}_lv1.png" alt="" onerror="this.style.visibility='hidden'"><div><h3>${b.colour}</h3><p>feeds ${b.feeds}</p></div></header>
      ${rows}
    </article>`;
  }).join("");
}

function reset(byId) {
  const gold = byId["item-gold"]?.held;
  if (typeof gold !== "number") { $("#reset").hidden = true; return; }
  $("#reset").innerHTML = `${img("item-gold", "ri")}
    <p>Resetting a node costs <b class="num">${num(RESET_GOLD)}</b> Hologold, and its Cubes and Hexas come back.
    Your <b class="num">${num(gold)}</b> Hologold covers <b class="num">${num(Math.floor(gold / RESET_GOLD))}</b> node resets.</p>`;
}

function tile(it, opts = {}) {
  const named = !!it.name;
  const name = named ? esc(it.name) : "Unnamed item";
  // an item the name table lacks: said plainly, its game id kept in the tooltip (one truncated line on screen)
  const desc = it.description || it.note || (named ? "" : it.item_id);
  return `<div class="it${named ? "" : " unnamed"}" title="${esc(named ? desc : `${it.item_id}: no English name for this item yet`)}">
    <div class="art">${img(it.icon || it.item_id, "ii", opts.colour)}</div>
    <div class="itx"><span class="in">${name}</span><b class="num">${num(it.held)}</b>${desc ? `<small>${esc(desc)}</small>` : ""}</div>
  </div>`;
}

function growth(data, byId) {
  const cur = data.currencies || [];
  const held = id => byId[id]?.held ?? cur.find(c => c.item_id === id)?.held ?? 0;
  const single = ["item-card_level_up", "item-card_limit_break-cmn-sr", "item-card-potential-upgrade-cmn"]
    .map(id => byId[id] || cur.find(c => c.item_id === id))
    .filter(Boolean);
  const attrTable = `<div class="attrs">
    <div class="ah"></div>${ATTRS.map(a => `<div class="ah" style="--ac:${a.ink}"><i></i>${a.name}</div>`).join("")}
    ${[["Beads", 1], ["Crystals", 2]].map(([label, g]) => `<div class="al">${label}</div>` + ATTRS.map(a => {
      const id = `item-card_limit_break-attribute_${a.n}-${g}`;
      return `<div class="ac">${img(id, "ai", a.ink)}<b class="num">${num(held(id))}</b></div>`;
    }).join("")).join("")}
  </div>`;
  const bundles = cur.filter(c => c.group === "bundle").map(b => ({ ...b, description: b.note }));
  $("#growth").innerHTML = `
    <div class="inv-sub"><h3>Beads and Crystals <small>Member Training, by attribute</small></h3>${attrTable}</div>
    <div class="inv-sub"><h3>Items</h3><div class="its">${single.map(i => tile(i)).join("")}</div></div>
    ${bundles.length ? `<div class="inv-sub wide"><h3>Bundles</h3><div class="its four">${bundles.map(b => tile(b)).join("")}</div><p class="inv-hint" id="bundlehint" hidden></p></div>` : ""}`;
  let beads = 0, crystals = 0;
  for (const b of bundles) {
    const g = (b.grant_each || 0) * (b.held || 0);
    if (b.grant_kind === "crystals") crystals += g; else beads += g;
  }
  if (beads || crystals) {
    const parts = [];
    if (beads) parts.push(`<b class="num">+${num(beads)}</b> Beads`);
    if (crystals) parts.push(`<b class="num">+${num(crystals)}</b> Crystals`);
    const hint = $("#bundlehint");
    hint.innerHTML = `Opening every bundle would add ${parts.join(" and ")}, of whichever attribute you pick.`;
    hint.hidden = false;
  }
}

function rest(data) {
  const others = (data.items || []).filter(it => !SHOWN_RE.test(it.item_id));
  if (!others.length) { $("#rest").innerHTML = `<p class="empty">Nothing else held.</p>`; return; }
  const buckets = GROUPS.map(g => ({ ...g, items: [] }));
  for (const it of others) buckets.find(b => b.re.test(it.item_id)).items.push(it);
  $("#rest").innerHTML = buckets.filter(b => b.items.length).map(b => `
    <div class="grp"><h3>${b.name}<small class="num">${b.items.length}</small></h3>
      <div class="its four">${b.items.sort((x, y) => y.held - x.held).map(i => tile(i)).join("")}</div></div>`).join("");
}

function unavailable(reason) {
  $("#invchips").innerHTML = `<span>No account capture</span>`;
  $("#invbody").innerHTML = `<div class="inv-sec"><div class="error">${esc(reason || "No account capture to read.")}
    Refresh the account and reload this page.</div></div>`;
}

async function main() {
  let data;
  try {
    const r = await fetch("/api/resources");
    data = await r.json();
  } catch (e) {
    unavailable(`Could not reach the builder's server (${e}).`);
    return;
  }
  if (!data || data.available === false) { unavailable(data && data.reason); return; }
  const byId = {};
  for (const it of data.items || []) byId[it.item_id] = it;
  chips(data);
  heroStats(data, byId);
  branches(data);
  reset(byId);
  growth(data, byId);
  rest(data);
  wireFallbacks(document.body);
}
main();
