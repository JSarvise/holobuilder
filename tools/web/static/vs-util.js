// Compare page: small shared helpers. No number is computed here; formatting only.
// Copied from opt-util.js (the Optimizer page) and adapted to the .v- prefix, so the two
// pages never depend on each other's files.
export const byId = id => document.getElementById(id);  // not `$`: test_id_contract checks `byId("…")` against index.html only
const QUOTE = String.fromCharCode(34);   // spelled out: a quote inside a regex trips test_js_syntax
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", [QUOTE]: "&quot;" };
const ESC_RE = new RegExp("[&<>" + QUOTE + "]", "g");
export const esc = s => String(s ?? "").replace(ESC_RE, c => ESC[c]);
const NF = new Intl.NumberFormat("en-US");
export const fmt = n => (n == null || Number.isNaN(+n)) ? "—" : NF.format(Math.round(+n));
export const dec = (n, d) => (+n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
export const sgn = (n, d = 1) => (n >= 0 ? "+" : "−") + dec(Math.abs(n), d) + "%";
export const sgnN = n => (n >= 0 ? "+" : "−") + fmt(Math.abs(n));
export const mmss = s => { s = Math.round(s); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
export const thumb = id => `/cardart/thumb/${encodeURIComponent(id)}?w=160`;
export const INK = { Cute: "#F24068", Pure: "#0DC134", Happy: "#FFA712" };
export const LETTERS = ["A", "B", "C", "D"];
export const MAX_TEAMS = 4;

export async function getJSON(url, opts) {
  const r = await fetch(url, opts);
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

/* ---- the explanations behind the (i) buttons ---- */
export const NOTES = {
  route: "Every team is scored under the same assumptions, in pairs: team A with each other team, so team A's figures are the same in every pair. The timeline rebuilds each team's own board and slot order. With Holomem Boards <b>As ranked</b>, each team is scored on the board the Optimizer gave it.",
  state: "<b>As ranked</b>: each team on the board the Optimizer's search gives it on this song, not on one shared plan, so the numbers are the Optimizer's.",
  match: "The Optimizer's figure and this page's figure on the same board should be one number. A difference is shown, never hidden.",
  shared: "<b>From scratch</b>: one board plan is built once and every team is scored on it, so the teams share one basis. The <b>Re-planned for this team</b> line is the same team with a board planned for it alone (a what-if).",
  cached: "The score the Optimizer's search gave this team. The search plans the boards per song on its own, so it can differ from this page's figure. Shown for reference, never mixed into the comparison.",
  expected: "<b>Expected Score</b>: the mean score of a full-perfect run on this chart.",
  across: "<b>Average score</b> over every chart of this difficulty.",
  unitscore: "No song: the teams are compared by their <b>Unit Score</b>, the deck screen's number.",
  max: "<b>Max</b> is the score if every Active Skill fires every time it can.",
  off: "The same team with <b>no Holomem Board</b> at all: what the boards add.",
  unit: "Laid out like the game's own <b>Unit Score Details</b>, in the team's slot order and on its board. The Overall Power lines match the game exactly.",
  model_bonus: "The <b>Score Bonus as the ranking pools it</b>: what the search's Unit Score uses. It is not a line of the deck screen.",
  bonus: "The deck screen's five <b>Score Bonus</b> lines. Their total and the Unit Score are the deck screen's figures. The ranking pools the bonus differently; that figure has its own line and is not the deck screen's.",
  grade: "Grade letters use the game's own thresholds: the Unit Score ladder for the Unit Score, the song's score ladder for a score.",
  run: "Expected, Max and No board for this team. The bars are the exact spread of the run score: with a full-perfect run, the only chance is whether each Active Skill fires. <b>Verified</b> means the spread's own mean gives the Expected Score exactly.",
  timeline: "Active Skill windows as scheduled in this song, with each card's base activation chance, on this team's board and in its slot order. Special Skills fire at the song's five Special cues.",
  coverage: "Derived from the lanes above: the sum of the activation chances of every Active window open at each moment.",
  reset: "Resetting a node costs <b>1,000 Hologold per node</b>; the materials come back.",
  plan: "Nodes to unlock and to reset per holomem on this team's board, with each node's name and cost.",
  materials: "Materials the plan needs, summed over the nodes to unlock, against what your account holds.",
  gates: "Each Outfit or Passive Skill that needs a number of members of one generation or element, and whether the team meets it.",
  skills: "Each skill's own rate × uptime × weight: what it would add on its own, before the pooled maximum. Rows do not sum to the Score Bonus.",
  driver: "The line of the deck screen that moves the most between team A and this team. A guide to why, not an exact split of the score difference.",
};
export const info = key => `<button class="v-info" type="button" data-note="${key}" aria-label="About this" aria-expanded="false">i</button>`;

/* ---- grades: GET /api/ranks (the master's LiveDeckEvaluationRank and LiveScoreEvaluationRank) ---- */
let RANKS = null;
export async function loadRanks() {
  try { const r = await getJSON("/api/ranks"); RANKS = r && r.available !== false ? r : null; } catch (_) { RANKS = null; }
  return RANKS;
}
// [threshold, letter, superscript], highest first; empty until /api/ranks answered (no badge then, never a guess)
const sup = g => g.plus ? (g.plus === 1 ? "+" : g.plus === 2 ? "++" : `+${g.plus}`) : "";
const table = rows => (rows || []).map(g => [g.threshold, g.letter, sup(g)]).sort((a, b) => b[0] - a[0]);
export const unitGrades = () => table(RANKS && RANKS.unit_score);
export function songGrades(musicId) {
  const s = RANKS && RANKS.song; if (!s) return [];
  return table(s.groups[(s.exceptions || {})[musicId] || s.default_group]);
}
const GCOL = { S: "var(--grade-s)", A: "var(--grade-a)", B: "var(--grade-b)", C: "var(--grade-c)", D: "var(--grade-c)" };
export function badge(v, table, cls = "") {
  const g = table.find(([t]) => v >= t); if (!g) return "";
  return `<div class="v-grade ${cls}" style="--g:${GCOL[g[1]] || "var(--grade-c)"}" role="img" aria-label="Grade ${g[1]}${g[2]}"><span>${g[1]}${g[2] ? `<sup>${g[2]}</sup>` : ""}</span></div>`;
}

/* ---- popovers and tooltips ---- */
export function place(el, x, y) {
  el.hidden = false;
  const w = el.offsetWidth, h = el.offsetHeight;
  el.style.left = Math.min(x + 14, innerWidth - w - 12) + "px";
  el.style.top = (y + 16 + h > innerHeight ? y - h - 12 : y + 16) + "px";
}
let popBtn = null;
export function closePop() { byId("pop").hidden = true; if (popBtn) popBtn.setAttribute("aria-expanded", "false"); popBtn = null; }
export function wirePopovers() {
  document.addEventListener("click", e => {
    const nb = e.target.closest("[data-note]");
    if (nb) {
      e.preventDefault(); e.stopPropagation();
      if (popBtn === nb) return closePop();
      closePop(); popBtn = nb; nb.setAttribute("aria-expanded", "true");
      const p = byId("pop"); p.innerHTML = NOTES[nb.dataset.note] || ""; const r = nb.getBoundingClientRect();
      p.hidden = false; p.style.left = Math.min(r.left - 8, innerWidth - p.offsetWidth - 12) + "px";
      p.style.top = (r.bottom + 8 + p.offsetHeight > innerHeight ? r.top - p.offsetHeight - 8 : r.bottom + 8) + "px";
      return;
    }
    if (!e.target.closest("#pop")) closePop();
  }, true);
  document.addEventListener("keydown", e => { if (e.key === "Escape") closePop(); });
  addEventListener("scroll", closePop, { passive: true });
  document.addEventListener("mouseover", e => {
    const t = e.target.closest("[data-tip]"); const tip = byId("tip");
    if (!t) { if (!e.target.closest(".v-tlw")) tip.hidden = true; return; }
    const [a, b, c] = t.dataset.tip.split("|");
    tip.innerHTML = `<b>${a}</b><div><span class="num">${b}</span>${c ? ` · ${c}` : ""}</div>`;
    place(tip, e.clientX, e.clientY);
  });
  document.addEventListener("mousemove", e => { const t = e.target.closest("[data-tip]"); if (t) place(byId("tip"), e.clientX, e.clientY); });
}

/* ---- section reveal ---- */
const io = "IntersectionObserver" in window
  ? new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } }),
    { rootMargin: "0px 0px -40px 0px" }) : null;
export function reveal(root = document) {
  root.querySelectorAll(".v-reveal:not(.in)").forEach(el => io ? io.observe(el) : el.classList.add("in"));
}

/* ---- the leader's Outfit Skill in plain words ---- */
const STAT = { all_stats: "all parameters", sense: "Sense", technique: "Technique", performance: "Performance" };
export function outfitText(sk, gameText) {
  // the game's own sentence (/api/collection costume_text) when served; else one line built from the parsed skill
  if (gameText) return gameText;
  if (!sk) return "";
  let t = sk.family === "stat_up" && STAT[sk.effect_type]
    ? `+${dec(sk.magnitude, Number.isInteger(sk.magnitude) ? 0 : 1)}% to ${STAT[sk.effect_type]}` : `${sk.family} · ${sk.effect_type}`;
  const c = sk.condition || {};
  if (c.kind === "generation" || c.kind === "type") t += ` with ${c.count} ${c.target} members`;
  else if (c.kind && c.kind !== "ungated") t += ` (condition: ${c.kind})`;
  return t;
}

/* ---- one card shape for the face: rarity as an int ---- */
export function normCard(c) {
  const r = typeof c.rarity === "number" ? c.rarity : parseInt(String(c.rarity || "3"), 10) || 3;
  return { ...c, rarity: r };
}
