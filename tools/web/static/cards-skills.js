// Collection page: a card's skills in the game's own phrasing, and the skill facets the
// filter offers. Nothing here computes a number: every magnitude, duration and interval is
// the one /api/collection serves (data/cards.json + the bloom ladder the solver scores from).

const QUOTE = String.fromCharCode(34);   // spelled out: a quote inside a regex trips test_js_syntax
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", [QUOTE]: "&quot;" };
const ESC_RE = new RegExp("[&<>" + QUOTE + "]", "g");
export const esc = s => String(s ?? "").replace(ESC_RE, c => ESC[c]);

export const KINDS = ["ACTIVE", "SPECIAL", "PASSIVE"];
export const KIND_LABEL = { ACTIVE: "Active Skill", SPECIAL: "Special Skill", PASSIVE: "Passive Skill", OUTFIT: "Outfit Skill" };

const STAT_NAME = { performance: "Performance", technique: "Technique", sense: "Sense", all_stats: "All Stats" };
const typeIcon = t => `<img class="sk-ico" src="/chrome/element/ui_999_ico_element_${esc(String(t).toLowerCase())}_cl.png" alt="" onerror="this.remove()">`;
const num = m => (m == null ? "?" : String(+m === Math.round(+m) ? Math.round(+m) : +m));

// The effect, as the game words it ("Score UP 55%", "Grants Technique UP 14%").
export function effectText(sk) {
  const m = num(sk.magnitude);
  switch (`${sk.family}/${sk.effect_type}`) {
    case "conditional_score_up/score": return `Score UP ${m}%`;
    case "score_support/score": return `Grants Score Support Effect of ${m}%`;
    case "activation_rate_up/activation_rate": return `Grants Skill Activation UP ${m}%`;
    case "life_recovery/life": return `Recovers LIFE ${m}%`;
    case "judgment_enhancement/judgment": return "GOOD or higher becomes PERFECT";
  }
  if (sk.family === "stat_up") return `Grants ${STAT_NAME[sk.effect_type] || sk.effect_type} UP ${m}%`;
  return `${String(sk.effect_type || sk.family).replace(/_/g, " ")} ${m}`;
}

// "2 [icon]Happy Type Members" / "2 Gen 0 Members" — the game's group phrasing.
function group(kind, value, count, genLabel) {
  const n = count ? `${count} ` : "";
  if (kind === "type") return `${n}${typeIcon(value)}${esc(value)} Type Members`;
  if (kind === "generation") return `${n}${esc(genLabel(value))} Members`;
  return esc(String(value || kind).replace(/_/g, " "));
}

function condText(c, genLabel) {
  if (!c) return "";
  if (c.kind === "type" || c.kind === "generation") return `With ${c.count || 1} or more ${group(c.kind, c.target || c.value, 0, genLabel)}`;
  if (c.kind === "run_state") return `At ${c.count} ${esc(c.target === "life" ? "LIFE" : c.target)}`;
  return "";
}

// One skill slot as an HTML sentence:
// "With 2 or more [icon]Happy Type Members, Grants Technique UP 14% to 3 [icon]Happy Type Members."
export function sentence(sk, genLabel) {
  if (!sk) return "";
  const parts = [];
  const cond = condText(sk.condition, genLabel);
  let s = cond ? `${cond}, ` : "";
  if (sk.duration) s += `For ${num(sk.duration)}s, `;
  s += `<b>${esc(effectText(sk))}</b>`;
  const t = sk.target || {};
  if (t.kind === "type" || t.kind === "generation") s += ` to ${group(t.kind, t.value, t.count, genLabel)}`;
  else if (t.kind === "self") s += " to self";
  parts.push(s + ".");
  const rider = sk.rider || {};
  if (rider.kind === "rider") parts.push(`<b>${esc(effectText(rider))}</b>.`);
  const ov = sk.override || {};
  if (ov.kind === "override") {
    const when = condText(ov.condition, genLabel) || "When boosted";
    parts.push(`${when}, <b>${esc(effectText({ family: sk.family, effect_type: sk.effect_type, magnitude: ov.magnitude }))}</b> instead.`);
  }
  if (sk.interval) parts.push(`Every ${num(sk.interval)}s${sk.probability_tier ? ` with a ${esc(sk.probability_tier)} Probability chance` : ""}.`);
  return parts.join(" ");
}

// The slot a card's `kind` has at `bloom`: the ladder's rung (Lv 1 below the bloom step,
// Lv 2 at or above) — the same value restate_card_skills scores. Unknown bloom → Lv 2.
export function slotAt(card, kind, bloom, steps) {
  const step = (steps || {})[kind];
  const level = bloom == null || step == null ? 2 : (bloom >= step ? 2 : 1);
  const rungs = (card.skill_ladder || {})[kind] || {};
  return { slot: rungs[String(level)] || (card.skills || {})[kind] || null, level, step };
}

// ---- skill facets for the filter: the game's effect names, one tag per effect ----
const RIDER = { activation_rate_up: "Skill Activation UP", judgment_enhancement: "GOOD becomes PERFECT", life_recovery: "Recovers LIFE" };
function statTag(sk) {
  if (sk.family === "stat_up") return `${STAT_NAME[sk.effect_type] || sk.effect_type} UP`;
  if (sk.family === "score_support") return "Score Support";
  return effectText(sk).replace(/ [\d.]+%$/, "");
}
export const SKILL_FACETS = [
  { key: "sk_active", label: "Active Skill", pick: c => {
    const a = (c.skills || {}).ACTIVE; if (!a) return [];
    const ov = a.override || {};
    if (ov.kind !== "override") return ["Score UP"];
    const k = (ov.condition || {}).kind;
    return [k === "run_state" ? `Score UP, more at ${(ov.condition.target === "life" ? "LIFE" : ov.condition.target)}` : "Score UP, more with members"];
  } },
  { key: "sk_special", label: "Special Skill", pick: c => {
    const s = (c.skills || {}).SPECIAL; if (!s) return [];
    const r = s.rider || {};
    return [r.kind === "rider" ? (RIDER[r.family] || r.family) : "Score Support only"];
  } },
  { key: "sk_passive", label: "Passive Skill", pick: c => {
    const p = (c.skills || {}).PASSIVE; return p ? [statTag(p)] : [];
  } },
  { key: "sk_outfit", label: "Outfit Skill", pick: c => (c.costume_skill ? [statTag(c.costume_skill)] : []) },
];
