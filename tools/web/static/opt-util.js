// Optimizer page: small shared helpers. No number is computed here; formatting only.
export const byId = id => document.getElementById(id);  // not `$`: test_id_contract checks `byId("…")` against index.html only
const QUOTE = String.fromCharCode(34);   // spelled out: a quote inside a regex trips test_js_syntax
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", [QUOTE]: "&quot;" };
const ESC_RE = new RegExp("[&<>" + QUOTE + "]", "g");
export const esc = s => String(s ?? "").replace(ESC_RE, c => ESC[c]);
const NF = new Intl.NumberFormat("en-US");
export const fmt = n => (n == null || Number.isNaN(+n)) ? "—" : NF.format(Math.round(+n));
export const dec = (n, d) => (+n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
export const sgn = (n, d = 1) => (n >= 0 ? "+" : "−") + dec(Math.abs(n), d) + "%";
export const pct = permil => permil == null ? "—" : `+${dec(permil / 10, permil % 10 ? 1 : 0)}%`;
// dates in the app's one style, "15 Oct 19:04" (HBFmt.date, /hb-format.js); the local
// fallback only covers a page loaded without that script
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export const when = t => {
  if (window.HBFmt && window.HBFmt.date) return window.HBFmt.date(t);
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(String(t || ""));
  return m ? `${+m[3]} ${MON[+m[2] - 1]} ${m[4]}:${m[5]}` : "—";
};
export const mmss = s => { s = Math.round(s); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
export const thumb = id => `/cardart/thumb/${encodeURIComponent(id)}?w=160`;
export const INK = { Cute: "#F24068", Pure: "#0DC134", Happy: "#FFA712" };
export const chrOf = id => "chr-" + String(id).split("-")[1];
export const AC = [8, 16, 24, 30, 37, 42, 46, 50, 53, 56];   // Live Boost Event Pt multiplier (marathon.live_stamina_score)

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

/* ---- the explanations that live behind the (i) buttons ---- */
export const NOTES = {
  expected: "<b>Expected Score</b> is the server's mean run score for this team, with its Holomem Boards planned for this song (the objective the search ranks by).",
  expected_cur: "<b>Score</b> with your Holomem Boards exactly as they are on your account.",
  across: "<b>Average score</b> over every chart of this difficulty, with the Holomem Boards planned for the team.",
  unitscore: "No song chosen and no average: the team is ranked by its <b>Unit Score</b>.",
  max: "<b>Max</b> is the score if every Active Skill fires every time it can.",
  now: "<b>Now</b> is the same team with your Holomem Boards exactly as they are on your account. On the Every song view it is the average over every chart.",
  cached: "This team comes from the <b>cached search</b> (rebuilt by every Refresh). Its score is on that search's basis; press Find best team to search live.",
  unit: "Laid out like the game's own <b>Unit Score Details</b>, on the row's own Holomem Boards and slot order: the detail rebuilds exactly the board the row was scored on. Overall Power lines are exact against the game.",
  bonus: "The deck screen's five <b>Score Bonus</b> lines, in the row's slot order. Their total and the Unit Score above are the deck screen's figures. The ranking model pools the bonus differently (combined rate + board rate); that pooled figure is in the next note and is not the deck screen's.",
  model_bonus: "The <b>ranking model's pooled Score Bonus</b>: what the search's Unit Score term uses. It is not a line of the deck screen.",
  grade: "Grade letters use the game's own thresholds, read from the game's master data (the Unit Score ladder, and the song's own ladder for a score).",
  run: "Expected, Max and Now are the server's numbers for this team. The bars are the exact law of the run score: with a full-perfect run, the only randomness is whether each Active Skill fires. <b>Verified</b> means the law's own mean reproduces the row's Expected Score exactly.",
  timeline: "Active Skill windows as scheduled in this song, in the row's slot order and on its board, with the card's base activation chance. Special Skills fire at the song's five Special cues.",
  coverage: "Derived from the lanes above: the sum of the activation chances of every Active window open at each moment.",
  reset: "Resetting a node costs <b>1,000 Hologold per node</b> (the game's own price); the materials come back.",
  reset_price: "The same team re-planned within your cubes and hexas with fewer resets: none at all, or only the resets that each earn 0.2 % / 0.05 % of the score. Every plan is scored like the row, so the last column is what those extra resets buy.",
  plan: "Nodes to unlock and to reset per holomem, from the row's own board; node names and costs are the game's.",
  match: "The row and its detail are scored by two routes. When both give the same figure they share one board and one slot order. A difference is shown, never hidden: a stale cache, or a row developed with unlimited materials (the detail rebuilds the board on your real bench).",
  materials: "Materials the plan needs, summed over the server's node costs of the nodes to unlock, against what your account holds plus what the plan's resets give back (a reset refunds the node's materials; not with \"Don't refund resets\").",
  ev_points: "<b>Event Pt per Live</b> = ceil(ceil((120 + score/15000)(1 + E/1000)) × Live Boost × L × 2 with HoloPass). L, the song-length multiplier, is an estimate; the server computes it in game.",
  ev_song: "<b>Event Song Score</b> = ceil(score × (1 + P/1000)), P the largest Song Score Bonus among the five members.",
};
export const info = key => `<button class="o-info" type="button" data-note="${key}" aria-label="About this" aria-expanded="false">i</button>`;

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
  return `<div class="o-grade ${cls}" style="--g:${GCOL[g[1]] || "var(--grade-c)"}" role="img" aria-label="Grade ${g[1]}${g[2]}"><span>${g[1]}${g[2] ? `<sup>${g[2]}</sup>` : ""}</span></div>`;
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
    if (!t) { if (!e.target.closest(".o-tlw")) tip.hidden = true; return; }
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
  root.querySelectorAll(".o-reveal:not(.in)").forEach(el => io ? io.observe(el) : el.classList.add("in"));
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

/* ---- the last answer survives leaving the page: sessionStorage, per browser
   tab, stamped with when the account was read so a refresh in between drops it ---- */
export function stash(name, ctx, value) {
  try { sessionStorage.setItem("hb.optimizer." + name, JSON.stringify({ read: (ctx.D.meta || {}).snapshot_mtime ?? null, value })); }
  catch (_) { /* too large or private mode: the answer just is not kept */ }
}
export function unstash(name, ctx) {
  try {
    const s = JSON.parse(sessionStorage.getItem("hb.optimizer." + name) || "null");
    return s && s.read === ((ctx.D.meta || {}).snapshot_mtime ?? null) ? s.value : null;
  } catch (_) { return null; }
}

// Each result card AS THE SEARCH SCORED IT (with "max level, bloom 5" a card must show the
// maxed level and bloom, not the account's own, and a card not owned must still show one):
// POST /api/card-states applies the search's levers to the collection the way the scorer does.
const STATES = new Map();
let statesKey = "";
export function withState(card) {
  const s = card && STATES.get(card.id);
  return s ? { ...card, level: s.level, bloom: s.bloom, limit_break: s.limit_break, trial: s.trial } : card;
}
// resolves true when the states changed (the caller redraws once); the same request twice is a no-op
export async function loadStates(ids, assumptions) {
  const uniq = [...new Set(ids.filter(Boolean))].sort();
  const key = JSON.stringify([assumptions || null, uniq]);
  if (key === statesKey) return false;
  statesKey = key;
  try {
    const d = await postJSON("/api/card-states", { ids: uniq, ...(assumptions ? { assumptions } : {}) });
    STATES.clear();
    for (const [k, v] of Object.entries(d.states || {})) STATES.set(k, v);
    return true;
  } catch (_) { return false; }
}
