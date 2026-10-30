// Holomem Board page: shared helpers and the board's fixed vocabulary.
// Formatting and lookups only; every figure on the page is a server field
// (/api/board, /api/board-doc, /api/boardplan, /api/board-plan-fast, /api/resources).
export const byId = id => document.getElementById(id);  // not `$`: test_id_contract checks `$("…")` against index.html
const QUOTE = String.fromCharCode(34);   // spelled out: a quote inside a regex trips test_js_syntax
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", [QUOTE]: "&quot;" };
const ESC_RE = new RegExp("[&<>" + QUOTE + "]", "g");
export const esc = s => String(s ?? "").replace(ESC_RE, c => ESC[c]);
const NF = new Intl.NumberFormat("en-US");
export const fmt = n => (n == null || Number.isNaN(+n)) ? "—" : NF.format(Math.round(+n));
export const chrOf = id => "chr-" + String(id).split("-")[1];
export const plural = (n, one, many) => `${fmt(n)} ${n === 1 ? one : (many || one + "s")}`;

// Board reset: 1,000 Hologold per node reset (main_Setting skillTreeResetConsumptionGoldQuantity,
// read from the client).
export const RESET_GOLD = 1000;
// Every holomem at level 50 (the "Max Holomem Board Pt" preset the Optimizer also uses).
export const MAX_LEVEL_POINTS = 361;

// The four branches + Connect. Names follow the Optimizer's plan block; `hue` is the
// skilltree palette, `g1/g2` the game's node gradient
// (PaletteStore SkillLeader / SkillMember / SkillSupport1 / SkillSupport2).
// `mat` is the material family a branch's nodes cost.
// `ink` is the same branch as a Night Deck token (hb.css --br-*: the g2 gradient ends), used by
// HTML chrome (chips, dots, rules); the SVG board keeps `hue`/`g1`/`g2`, as the game draws it.
// `sprite` is the game's own node sprite for the branch (/ui/board/).
export const BRANCH = {
  R: { name: "Leader Skill", hue: "#F45E5A", g1: "#FF983D", g2: "#FF386E", mat: "leader", ink: "var(--br-leader)", sprite: "red" },
  B: { name: "Member Skill", hue: "#4373FF", g1: "#3DCBFF", g2: "#3866FF", mat: "card", ink: "var(--br-member)", sprite: "blue" },
  G: { name: "Overall Boost", hue: "#3BC799", g1: "#7EF9B7", g2: "#00CD7B", mat: "allmember", ink: "var(--br-support1)", sprite: "green" },
  Y: { name: "Song & Holowork", hue: "#FFB028", g1: "#FF9E2D", g2: "#FFC941", mat: "content", ink: "var(--br-support2)", sprite: "yellow" },
  S: { name: "Connect", hue: "#E8EAF0", g1: "#F4F5FA", g2: "#B9BED0", mat: null, ink: "var(--connect)", sprite: null },
};
// An official white-silhouette sprite tinted by currentColor (hb.css .ico); no drawn icons.
const ICO = { plus: "grade/ui_999_ico_plus", x: "nav/ui_999_ico_cancel", check: "nav/ui_999_ico_check_l",
  lock: "nav/ui_999_ico_lock", info: "nav/ui_999_ico_question_circle", search: "nav/ui_999_ico_search" };
export const ico = k => `<i class="ico" style="--ico:url(/ui/${ICO[k]}.png)" aria-hidden="true"></i>`;
// The branch's node sprite (lit), the game's own art for a branch marker.
export const branchMark = b => BRANCH[b] && BRANCH[b].sprite
  ? `<img class="hbd-bm" src="/ui/board/ui_710_ico_node_${BRANCH[b].sprite}_lv1.png" alt="" onerror="this.style.visibility='hidden'">`
  : `<i class="hbd-bm con" aria-hidden="true"></i>`;
export const BRANCH_ORDER = ["R", "B", "G", "Y"];
// Who a branch helps (from master data).
export const WHO = {
  R: "Only while this holomem is the Leader. Helps the whole unit.",
  B: "Only this holomem's own cards, while one is in the unit.",
  G: "Always, every team, even when this holomem is not in the unit.",
  Y: "Songs this holomem sings (score bonus) and Holowork rewards.",
  S: "Place a member card here: it boosts the nodes in its range.",
};

// The game's English node text, with [character] filled and [value] / [value/10]% filled
// with `val` (the number as the game prints it: "120", "6.2%"). With no `val` the
// placeholder is dropped, never invented.
export function effectText(effect, name, val) {
  const t = String(effect || "").replace(/\[character\]/g, name || "this holomem");
  return (val ? t.replace(/\[value(\/10)?\]%?/g, val) : t.replace(/\s*\[value(\/10)?\]%?/g, ""))
    .replace(/\s+\./g, ".")
    .trim();
}
// One-line node title: the effect's first sentence without "Grants".
export function effectShort(effect, name, val) {
  const t = effectText(effect, name, val).split("\n").pop();
  return t.replace(/^Grants /, "").replace(/\.$/, "");
}
// What a node is for, by its game text: score, life, or rewards (no score value).
export function nodeKind(n) {
  const e = n.effect || "";
  if (n.branch === "S") return "connect";
  if (/Per-song score bonus/.test(e)) return "songbonus";
  if (/Holowork|holowork|Rewards obtained|Drop Rate|EXP UP/.test(e)) return "reward";
  if (/LIFE/.test(e)) return "life";
  return "score";
}
export const isGreenAll = n => n.branch === "G" && / to All\.$/.test(n.effect || "");
export const groupingOf = n => {
  if (n.branch !== "G") return null;
  const m = / to (.+)\.$/.exec(n.effect || "");
  return m && m[1] !== "All" && /All Stats UP/.test(n.effect) ? m[1] : null;
};
export const songBonusType = n => {
  const m = /Per-song score bonus on (any|group|solo) songs/.exec(n.effect || "");
  return m ? m[1] : null;
};

/* ------------------------------------------------------------------ values and Connect */
// Node values and the Connect catalogue are server fields (/api/board-doc, built by
// tools/web/board_values.py from master SkillTreeNode + SkillTreeConnectEffect(Extent), the
// Connect level from the account's bloom). The one piece of arithmetic done here is the
// game's own boost, so a plan's recommended card can be shown as well:
//   value = Ceiling((1 + sum of permil / 1000) x base)   (read from the client)
// in exact integers, the same as board_values.boosted (tested there).
export const boosted = (v, permil) => (permil ? Math.floor((v * (1000 + permil) + 999) / 1000) : v);
export function fmtValue(v, unit) {
  if (v == null || !unit) return "";
  if (unit === "permil") return (v / 10).toFixed(1).replace(/\.0$/, "") + "%";
  return String(v);
}
// The game's connect colour (palette Blu2nd: SkillTreeCell ConnectArrow + boosted-cell base).
export const CONNECT_HUE = "#5FDCF0";

/** A node's base value as the board-doc serves it: {value, unit}, both null when none. */
export function nodeValue(chr, id) {
  const c = D.chars.get(chr);
  const n = c && (c.nodes || []).find(x => x.id === id);
  return n ? { value: n.value ?? null, unit: n.value_unit || null } : { value: null, unit: null };
}

/** What the Connect cards on `ch` do in the board shown. `card(sock)` names the card a
 *  socket holds in this view (placed now, or the plan's). Returns
 *  {bySocket: socket -> {card, info, covers[]}, permil: node -> summed permil,
 *   src: node -> [socket], cells: socket -> [[x, y]] (the whole shape, empty cells too)}. */
export function connectView(ch, card) {
  const cat = (D.doc && D.doc.connect_cards) || {};
  const at = new Map((ch.nodes || []).filter(n => n.branch !== "S").map(n => [`${n.pos[0]},${n.pos[1]}`, n.id]));
  const out = { bySocket: new Map(), permil: new Map(), src: new Map(), cells: new Map() };
  for (const s of ch.sockets || []) {
    const id = card(s);
    const info = id ? cat[id] : null;
    if (!info) continue;
    const cells = info.cells.map(([dx, dy]) => [s.pos[0] + dx, s.pos[1] + dy]);
    const covers = cells.map(([x, y]) => at.get(`${x},${y}`)).filter(Boolean);
    out.bySocket.set(s.socket, { card: id, info, covers });
    out.cells.set(s.socket, cells);
    for (const nid of covers) {
      out.permil.set(nid, (out.permil.get(nid) || 0) + info.permil);
      out.src.set(nid, [...(out.src.get(nid) || []), s.socket]);
    }
  }
  return out;
}

export function matFamily(itemId) {
  const m = /^item-skill_tree_([a-z]+)-(\d)$/.exec(itemId || "");
  return m ? { fam: m[1], tier: +m[2] } : null;
}
const FAM_BRANCH = { leader: "R", card: "B", allmember: "G", content: "Y" };
export const matBranch = itemId => FAM_BRANCH[(matFamily(itemId) || {}).fam] || null;
export const MAT_ORDER = ["leader", "card", "allmember", "content"];
export function matSort(a, b) {
  const fa = matFamily(a) || { fam: "", tier: 9 }, fb = matFamily(b) || { fam: "", tier: 9 };
  return MAT_ORDER.indexOf(fa.fam) - MAT_ORDER.indexOf(fb.fam) || fa.tier - fb.tier || a.localeCompare(b);
}

// a network failure means the app's own local engine is not answering (stopped, or restarting
// after an update): say so instead of the browser's bare "Failed to fetch"
const ENGINE_DOWN = "The app's local engine is not answering (it may be restarting after an update). Close and reopen the app; if it keeps happening, see Troubleshooting in the docs.";
async function reach(url, opts) {
  try { return await fetch(url, opts); } catch (e) {
    if (e && e.name === "AbortError") throw e;
    throw new Error(ENGINE_DOWN);
  }
}
export async function getJSON(url, opts) {
  const r = await reach(url, opts);
  const raw = await r.text();
  let body;
  try { body = raw ? JSON.parse(raw) : {}; } catch (_) {
    throw new Error(`HTTP ${r.status}: the server did not answer JSON (${raw.slice(0, 120)})`);
  }
  if (!r.ok) throw new Error(body.error || body.message || `HTTP ${r.status}`);
  return body;
}
export const postJSON = (url, body, signal) => getJSON(url, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal });

let toastTimer;
export function toast(msg) {
  const t = byId("toast"); t.textContent = msg; t.classList.add("on");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("on"), 2600);
}

export function parseWhen(s) {
  if (!s) return null;
  const m = /(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})([+-]\d{2})(\d{2})?/.exec(s);
  const d = m ? new Date(`${m[1]}${m[2]}:${m[3] || "00"}`) : new Date(s);
  return isNaN(d) ? null : d;
}
export function ago(d) {
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  if (mins < 48 * 60) return `${Math.round(mins / 60)} h ago`;
  return `${Math.round(mins / 1440)} days ago`;
}

// The shared page state: filled by hbd-app.js, read by the board and the plan.
export const D = {
  board: null,     // GET /api/board
  doc: null,       // POST /api/board-doc over every holomem's board as it is
  res: null,       // GET /api/resources
  coll: null,      // GET /api/collection
  songs: [],       // GET /api/songs
  chars: new Map(),  // chr -> board-doc character
  plan: null,      // the last plan doc (board-export/v0 with plan per character)
  planMeta: null,  // {team, song, engine, ...} the plan was asked with
};
export const nameOf = chr => (D.chars.get(chr) || {}).name || chr;
export const genOf = chr => (((window.HB_GENS && window.HB_GENS.by) || {})[chr] || []).filter(g => g.reg);
export const wallet = id => {
  const m = ((D.res && D.res.board_materials) || []).find(x => x.item_id === id);
  return m ? m.wallet : null;
};
export const matName = id => {
  const m = ((D.res && D.res.board_materials) || []).find(x => x.item_id === id);
  if (m) return m.name;
  const f = matFamily(id);
  if (!f) return id;
  const col = { leader: "Red", card: "Blue", allmember: "Green", content: "Yellow" }[f.fam];
  return `${col} ${f.tier === 1 ? "Cube" : "Hexa"}`;
};
export const goldHeld = () => {
  const g = ((D.res && D.res.currencies) || []).find(c => c.item_id === "item-gold");
  return g ? g.held : null;
};
