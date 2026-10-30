// The collection editor: level, bloom and limit break, per card.
//
// Two rules this module exists to honour, both of them the reason it is thin:
//
// 1. **It never computes a bound of its own.** Every cap, every max bloom and
//    every "which skills are overstated at this bloom" comes from
//    `GET /api/collection`, which reads them out of `hbuilder.domain.collection`.
//    A UI that clamped to its own idea of the cap would let the user type a
//    level the solver then silently changes, and `resolve_card_state` already
//    clamps AND reports — this surfaces that rather than duplicating it.
// 2. **The disk file is the record; `localStorage` is only the working copy.**
//    The CLI cannot read a browser's storage, and the solve is a server-side
//    job, so an edit that never reaches `state/collection.json` has not
//    happened as far as the tool is concerned. Save says so explicitly.

import { cardTile, setGenColors } from "/ranking.js";

const LS_KEY = "hbuilder.collection.draft";

const state = {
  doc: null,       // the document as edited
  cards: [],       // server-resolved view, for bounds and current values
  meta: null,      // fully_scorable_bloom, skill_bloom_step, curve_available
  dirty: false,
};

import { el } from "/dom.js";

function loadDraft() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null; // a corrupt draft is not worth failing the page over
  }
}

function saveDraft() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(state.doc));
  } catch {
    /* quota or private mode — the disk file is the record, not this */
  }
}

function entryFor(cardId) {
  const cards = state.doc.cards || (state.doc.cards = {});
  if (typeof cards[cardId] !== "object" || cards[cardId] === null) {
    cards[cardId] = {};
  }
  return cards[cardId];
}

// Which skill kinds `data/cards.json` overstates at this bloom. Mirrors
// `domain.collection.overstated_skills`, driven by the table the SERVER sent
// rather than by a copy of it here.
function overstated(bloom) {
  if (bloom === null || bloom === undefined) return [];
  const steps = state.meta.skill_bloom_step || {};
  return Object.keys(steps)
    .filter((kind) => bloom < steps[kind])
    .sort((a, b) => steps[a] - steps[b]);
}

// One editable card. The FACE is `cardTile`'s -- the same renderer the ranking
// uses -- and the controls ride in its footer slot. Nothing about the card's
// appearance is decided here, which is the point: two copies of the face is how
// the element colours drifted for months.
// The overstated-skills sentence, in one place because the tile and the modal
// must not be able to disagree about what a bloom costs you.
function overstatedTitle() {
  return (
    `Bloom upgrades a card's skills to full magnitude one step at a time ` +
    `(${Object.entries(state.meta.skill_bloom_step)
      .map(([k, v]) => `${k} at bloom ${v}`)
      .join(", ")}), but data/cards.json always carries the full value. Below ` +
    `those blooms the tool scores the maxed skill, not the weaker one this card ` +
    `has now — its stats are right, these skill magnitudes read high.`
  );
}

// The screen-level version of the same fact. A per-card badge can say WHICH
// card is affected; only a banner can say how many there are, why it happens
// and what the reader is supposed to do about it. Counted off the live
// document, so it moves the moment a bloom is edited.
function refreshOverstatedBanner() {
  const node = document.getElementById("coverstated");
  if (!node) return;
  const affected = state.cards.filter((card) => {
    const entry = (state.doc.cards || {})[card.id] || {};
    const bloom = entry.bloom !== undefined ? entry.bloom : card.bloom;
    return overstated(bloom === null ? null : Number(bloom)).length > 0;
  }).length;
  node.hidden = affected === 0;
  if (!affected) return;
  const steps = Object.entries(state.meta.skill_bloom_step || {})
    .sort((a, b) => a[1] - b[1])
    .map(([kind, step]) => `${kind} at bloom ${step}`)
    .join(", ");
  node.className = "banner warn";
  node.textContent =
    `${affected} of ${state.cards.length} cards sit below the bloom that maxes a ` +
    `skill, so some of their ACTIVE/SPECIAL/PASSIVE are still at Lv 1 — ${steps}. ` +
    `The tool scores them at that real Lv-1 value (matching the game), not the ` +
    `catalogue's maxed one. Open a card to see each skill at its bloom; bloom it ` +
    `to reach Lv 2.`;
}

// The footer is one button that states the card's current values and opens the
// dialog, rather than a raw <input> and <select> in every card footer (hundreds
// of unlabelled live controls, with no way to see what a change would do before
// making it).
function rowFor(card) {
  const foot = el("button", "cfoot cfoot-btn");
  foot.type = "button";

  function refresh() {
    const entry = (state.doc.cards || {})[card.id] || {};
    const lvl = entry.level !== undefined ? entry.level : card.level;
    const bloom = entry.bloom !== undefined ? entry.bloom : card.bloom;
    const behind = overstated(bloom === null ? null : Number(bloom));
    foot.textContent = "";
    const line = el("span", "cfoot-vals");
    line.append(
      el("b", "cfoot-lv", lvl === null || lvl === undefined ? "--" : `Lv ${lvl}`),
      el("span", "cfoot-sep", "·"),
      el("span", "cfoot-bl",
        bloom === null || bloom === undefined ? "bloom —" : `bloom ${bloom}`)
    );
    foot.append(line);
    const warn = el("span", "cwarn");
    if (behind.length) {
      // Plain words rather than the model's vocabulary ("overstated"); the
      // banner at the top of the screen carries the mechanism.
      warn.textContent = `${behind.join("/")} at Lv 1`;
      warn.title = overstatedTitle();
      row.classList.add("under");
    } else {
      row.classList.remove("under");
    }
    foot.append(warn);
    // `source` again: a value the user typed and a value the document
    // defaulted to are different claims, and the footer border says which.
    const src = (state.doc.cards || {})[card.id];
    const typed = src && (src.level !== undefined || src.bloom !== undefined);
    row.classList.toggle("cdefault", !typed);
  }

  foot.addEventListener("click", () => openEditor(card, refresh));

  const row = cardTile(card, { foot }, false);
  row.classList.add("ctile");
  row.dataset.id = card.id;
  // `cdefault` -- "this value came from the document default, not from you" --
  // is set inside `refresh` alone, off the LIVE document, not from
  // `card.source` (the server's reading at load time, which goes stale the
  // moment an edit is made). One source of truth, and it is the editable one.
  refresh();
  return row;
}

// --- the level/bloom dialog ------------------------------------------------
// Shaped after the game's own `CardLevelUpConfirmDialog`:
// the current value in `Blk`, a `»` in `Blu2nd`, the value you are moving to
// in `Blu1st`, the field on `BluLight`.
//
// What it deliberately does NOT show is a stat delta. The game's dialog does,
// because the game knows what P/T/S a card has at level 71. `/api/collection`
// reports a card's CURRENT level and its caps, not the curve between them, so
// a "+412 Performance" line here would be a number this page invented. The
// dialog shows what it actually knows: the two values, the bound, and what a
// bloom below the skill step costs you.

// --- the card's skills, in plain words -----------------------------------
// Composed from the maxed magnitudes data/cards.json carries — the same numbers
// the solver reads — the way the ranking's breakdown does: a plain phrase with
// the family/effect kept beside it as the receipt, and no invented unit (only
// `stat_up` gets a `%`, exactly as ranking.js already trusts).
const genLabel = (v) => ((state.meta && state.meta.gen_labels) || {})[v] || v;
const skPretty = (v) => String(v || "").replace(/_/g, " ");
// The game's own effect names. `stat_up` composes generically below.
const STAT_NAME = { performance: "Performance", technique: "Technique",
  sense: "Sense", all_stats: "All Stats" };
const EFFECT_VERB = {
  "conditional_score_up/score": (m) => `Score UP ${m}%`,
  "score_support/score": (m) => `Grants Score Support Effect of ${m}%`,
  "activation_rate_up/activation_rate": (m) => `Grants Skill Activation UP ${m}%`,
  "life_recovery/life": (m) => `Recovers LIFE ${m}%`,
  "judgment_enhancement/judgment": () => "GOOD or higher becomes PERFECT",
};
function effectVerb(sk) {
  const fn = EFFECT_VERB[`${sk.family}/${sk.effect_type}`];
  if (fn) return fn(sk.magnitude);
  if (sk.family === "stat_up") {
    return `Grants ${STAT_NAME[sk.effect_type] || skPretty(sk.effect_type)} UP ${sk.magnitude}%`;
  }
  return `${skPretty(sk.effect_type)} ${sk.magnitude}`;
}
function elTypeIcon(type) {
  const i = el("img", "sk-typeico");
  i.loading = "lazy";
  i.alt = "";
  i.src = "/chrome/element/ui_999_ico_element_" + String(type).toLowerCase() + "_cl.png";
  i.addEventListener("error", () => i.remove());
  return i;
}
// "N [icon]Type Members" / "N Gen Members" — the game's own group phrasing.
function appendGroup(node, kind, value, count) {
  if (count) node.append(document.createTextNode(count + " "));
  if (kind === "type") {
    node.append(elTypeIcon(value));
    node.append(document.createTextNode(`${value} Type Members`));
  } else if (kind === "generation") {
    node.append(document.createTextNode(`${genLabel(value)} Members`));
  } else {
    node.append(document.createTextNode(skPretty(value || kind)));
  }
}
// Compose the game's sentence for a skill INTO `line` (with inline type icons):
// "With 2 or more [icon]Happy Type Members, Grants Technique UP 7% to 3 [icon]Happy
// Type Members." · "For 6s, Score UP 40%. Every 17s with a Low Probability chance."
function buildSkillSentence(line, sk) {
  const cnd = sk.condition || {};
  if (cnd.kind === "type" || cnd.kind === "generation") {
    line.append(document.createTextNode(`With ${cnd.count || 1} or more `));
    appendGroup(line, cnd.kind, cnd.target || cnd.value, 0);
    line.append(document.createTextNode(", "));
  }
  if (sk.duration) line.append(document.createTextNode(`For ${sk.duration}s, `));
  line.append(el("b", null, effectVerb(sk)));
  const t = sk.target || {};
  if (t.kind === "type" || t.kind === "generation") {
    line.append(document.createTextNode(" to "));
    appendGroup(line, t.kind, t.value, t.count);
  } else if (t.kind === "self") {
    line.append(document.createTextNode(" to self"));
  }
  line.append(document.createTextNode("."));
  const rider = sk.rider || {};
  if (rider.kind === "rider") line.append(document.createTextNode(" " + effectVerb(rider) + "."));
  const ov = sk.override || {};
  if (ov.kind === "override") {
    const oc = ov.condition || {};
    const when = oc.kind === "run_state"
      ? `At ${oc.count || ""} ${skPretty(oc.target || "combo")}`.replace(/\s+/g, " ").trim()
      : "When boosted";
    line.append(document.createTextNode(
      ` ${when}, ${effectVerb({ family: sk.family, effect_type: sk.effect_type, magnitude: ov.magnitude })}.`));
  }
  if (sk.interval) {
    const prob = sk.probability_tier ? ` with a ${sk.probability_tier} Probability chance` : "";
    line.append(document.createTextNode(` Every ${sk.interval}s${prob}.`));
  }
}

const SKILL_ORDER = ["ACTIVE", "SPECIAL", "PASSIVE"];

// Fill #cedit-skills for `card`, and return an updater that (re)marks which
// kinds are "scored maxed" at a given bloom — so the modal's live bloom preview
// can flag them without rebuilding the list.
// The skill slot a card's `kind` actually has at `bloom` — the ladder's rung for
// that level (level 1 below the bloom step, level 2 at/above), the SAME value
// `restate_card_skills` scores. Falls back to cards.json (level 2) if a rung is
// missing, and to level 2 when the bloom is unknown.
function skillAtBloom(card, kind, bloom) {
  const step = (state.meta.skill_bloom_step || {})[kind];
  const level = (bloom === null || bloom === undefined || step === undefined)
    ? 2 : (bloom >= step ? 2 : 1);
  const rungs = (card.skill_ladder || {})[kind] || {};
  const slot = rungs[String(level)] || rungs[level] || (card.skills || {})[kind];
  return { slot, level };
}

function renderSkills(card) {
  const host = document.getElementById("cedit-skills");
  if (!host) return () => {};
  host.textContent = "";
  const gatedRows = {};

  const addGated = (kind, label) => {
    if (!(card.skills || {})[kind] && !(card.skill_ladder || {})[kind]) return;
    const row = el("div", "sk");
    row.dataset.kind = kind;
    const head = el("div", "sk-head");
    head.append(el("span", "sk-kind", label));
    const lv = el("span", "sk-lv");
    lv.hidden = true;
    head.append(lv);
    row.append(head);
    const line = el("p", "sk-line");
    row.append(line);
    const raw = el("span", "sk-raw");
    row.append(raw);
    host.append(row);
    gatedRows[kind] = { row, line, lv, raw };
  };
  for (const kind of SKILL_ORDER) addGated(kind, kind[0] + kind.slice(1).toLowerCase());

  // The outfit skill has no bloom step (LiveLeaderSkill, one row) — rendered once.
  if (card.costume_skill) {
    const row = el("div", "sk");
    const head = el("div", "sk-head");
    head.append(el("span", "sk-kind", "Outfit"));
    row.append(head);
    const line = el("p", "sk-line");
    buildSkillSentence(line, card.costume_skill);
    row.append(line);
    row.append(el("span", "sk-raw",
      `${card.costume_skill.family} / ${card.costume_skill.effect_type}`));
    host.append(row);
  }
  if (!host.children.length) host.append(el("p", "sk-none", "No skill data for this card."));

  // Re-render each gated skill AT the chosen bloom — the whole slot changes
  // between levels (magnitude, and sometimes duration/probability), so this
  // rebuilds the sentence rather than toggling a flag.
  return (bloom) => {
    for (const kind of SKILL_ORDER) {
      const r = gatedRows[kind];
      if (!r) continue;
      const { slot, level } = skillAtBloom(card, kind, bloom);
      if (!slot) continue;
      r.line.textContent = "";
      buildSkillSentence(r.line, slot);
      r.raw.textContent = `${slot.family} / ${slot.effect_type}`;
      const low = level === 1;
      r.row.classList.toggle("lv1", low);
      r.lv.hidden = !low;
      if (low) r.lv.textContent = `Lv 1 · Lv 2 at bloom ${(state.meta.skill_bloom_step || {})[kind]}`;
    }
  };
}

function openEditor(card, onApply) {
  const dlg = document.getElementById("cedit");
  if (!dlg) return;
  const entry = (state.doc.cards || {})[card.id] || {};
  const lvlIn = document.getElementById("cedit-lvl");
  const bloomIn = document.getElementById("cedit-bloom");
  const warn = document.getElementById("cedit-warn");

  document.getElementById("cedit-kicker").textContent =
    [card.rarity, card.type].filter(Boolean).join(" · ");
  document.getElementById("cedit-title").textContent =
    card.member + (card.costume_name ? ` — ${card.costume_name}` : "");

  // The same face the grid and the ranking draw. A second renderer is how the
  // element colours drifted for months; a dialog is not an excuse to start again.
  const holder = document.getElementById("cedit-card");
  holder.textContent = "";
  // An EMPTY foot, not a missing one. `cardTile` falls through to the P/T/S
  // branch when no `foot` is given, and a collection card carries no `.stats`
  // -- the server sends level/bloom/caps, not the stat block -- so `{}` threw
  // "cannot read properties of undefined (reading 'performance')" and the
  // dialog silently never opened. The face is the whole point here; the
  // numbers under it belong to the ranking.
  holder.append(cardTile(card, { foot: el("div", "dlg-cardfoot") }, false));

  document.getElementById("cedit-lvl-before").textContent =
    card.level === null || card.level === undefined ? "--" : `Lv ${card.level}`;
  document.getElementById("cedit-bloom-before").textContent =
    card.bloom === null || card.bloom === undefined ? "—" : `bloom ${card.bloom}`;

  lvlIn.max = card.level_cap ? String(card.level_cap) : "";
  lvlIn.placeholder = card.level_cap ? `max ${card.level_cap}` : "level";
  lvlIn.value = entry.level !== undefined ? entry.level : "";
  document.getElementById("cedit-lvl-hint").textContent =
    card.level_caps_by_limit_break.length
      ? `Caps by limit break: ${card.level_caps_by_limit_break.join(" / ")} ` +
        `— this card is at ${card.limit_break}, so its cap is ${card.level_cap}.`
      : "card_caps.json is not on disk, so 'max' cannot resolve to a number.";

  bloomIn.textContent = "";
  bloomIn.append(new Option("—", ""));
  const maxBloom = card.max_bloom === null ? 5 : card.max_bloom;
  for (let b = 0; b <= maxBloom; b += 1) {
    bloomIn.append(new Option(`bloom ${b}`, String(b)));
  }
  bloomIn.value = entry.bloom !== undefined ? String(entry.bloom) : "";
  document.getElementById("cedit-bloom-hint").textContent =
    `Fully scorable from bloom ${state.meta.fully_scorable_bloom}.`;

  const updateSkills = renderSkills(card);

  // Live: the consequence of the bloom you are about to choose, before you
  // commit it. This is the whole reason the control deserves a dialog.
  function preview() {
    const b = bloomIn.value === "" ? null : Number(bloomIn.value);
    updateSkills(b);
    // The skills list below shows each value at this bloom (a Lv 1 tag marks the
    // ones the bloom has not maxed yet), so no separate warning is needed.
    warn.hidden = true;
    // A field only reads as "the new value" while it differs from the old one.
    const lvlChanged = lvlIn.value !== "" && Number(lvlIn.value) !== card.level;
    lvlIn.classList.toggle("changed", lvlChanged);
    bloomIn.classList.toggle(
      "changed",
      bloomIn.value !== "" && Number(bloomIn.value) !== card.bloom
    );
  }
  preview();

  // ⚠ The commit does NOT hang off the dialog's `close` event, and that is not
  // a style choice. Reading `dlg.returnValue` inside a `close` listener -- the
  // documented way to learn which button ended a <form method=dialog> -- fails
  // in some browsers: `returnValue` is correctly "apply" and the dialog closes,
  // but `close` never fires, so every edit is silently dropped. `editing` plus
  // the buttons' own handlers below has no such dependency.
  editing = { card, onApply, lvlIn, bloomIn, preview };
  dlg.showModal();
}

// The open editor's context. Module-level because the dialog's buttons are
// wired ONCE at mount rather than re-bound on every open -- re-binding per
// open is how listeners accumulate 167 deep.
let editing = null;

function commitEditor(mode) {
  if (!editing) return;
  const { card, onApply, lvlIn, bloomIn } = editing;
  const e = entryFor(card.id);
  if (mode === "clear") {
    delete e.level;
    delete e.bloom;
  } else {
    if (lvlIn.value === "") delete e.level;
    else e.level = Number(lvlIn.value);
    if (bloomIn.value === "") delete e.bloom;
    else e.bloom = Number(bloomIn.value);
  }
  // An entry with nothing left in it is not an empty claim, it is no claim:
  // leaving `{}` behind would keep the card off its default.
  if (!Object.keys(e).length) delete state.doc.cards[card.id];
  onApply();
  markDirty();
}

function mountEditorDialog() {
  const dlg = document.getElementById("cedit");
  if (!dlg) return;
  const done = (mode) => {
    commitEditor(mode);
    editing = null;
    if (dlg.open) dlg.close();
  };
  const wire = (id, mode) => {
    const b = document.getElementById(id);
    // `type=button`, so the <form method=dialog> cannot submit underneath us
    // and close the dialog before the handler has run.
    if (b) { b.type = "button"; b.addEventListener("click", () => done(mode)); }
  };
  wire("cedit-apply", "apply");
  wire("cedit-clear", "clear");
  // Cancel, the close disc, Escape and the backdrop all discard. `editing` is
  // dropped so a later Apply cannot commit a card the user walked away from.
  for (const b of dlg.querySelectorAll('button[value="cancel"]')) {
    b.type = "button";
    b.addEventListener("click", () => { editing = null; dlg.close(); });
  }
  dlg.addEventListener("cancel", () => { editing = null; });
  // Live preview, bound once rather than per open.
  const lvl = document.getElementById("cedit-lvl");
  const bloom = document.getElementById("cedit-bloom");
  if (lvl) lvl.addEventListener("input", () => editing && editing.preview());
  if (bloom) bloom.addEventListener("change", () => editing && editing.preview());
}

function markDirty() {
  state.dirty = true;
  saveDraft();
  // An edited bloom can clear (or create) an overstatement, and the banner is
  // the screen's answer to "did that fix it".
  refreshOverstatedBanner();
  const status = document.getElementById("cstatus");
  if (status) {
    status.textContent = "unsaved — nothing on disk has changed yet";
    status.className = "cstatus dirty";
  }
}

// The filter predicates, one per control. Kept as data so `render` reads as a
// single `every(...)` rather than a stack of ifs, and so adding a facet later
// is one entry instead of another branch.
// Multi-select facets: a card
// passes a facet if it matches ANY chosen chip (OR within a facet, AND across
// facets). `pick` returns the card's value(s) for the facet; chips are built
// from the roster so a facet can never list a value no card has. Generation is
// kept RAW (`0期生`, `Myth`) — the key a gate reads — and only LABELLED in English.
const chrOf = (c) => "chr-" + String(c.id).split("-")[1];
const FACETS = [
  { key: "owned", label: "Ownership",
    fixed: [["owned", "Owned"], ["unowned", "Not owned"]],
    pick: (c) => [c.owned ? "owned" : "unowned"] },
  { key: "member", label: "Holomem", face: true,
    pick: (c) => (c.member ? [c.member] : []) },
  { key: "type", label: "Attribute",
    // The game's own element icon (the same sprite the card face wears) instead
    // of the words Cute/Pure/Happy — prettier and unmistakable.
    icon: (v) => "/chrome/element/ui_999_ico_element_" + String(v).toLowerCase() + "_cl.png",
    pick: (c) => (c.type ? [c.type] : []) },
  { key: "rarity", label: "Rarity",
    sort: (a, b) => (parseInt(a, 10) || 0) - (parseInt(b, 10) || 0),
    pick: (c) => (c.rarity ? [c.rarity] : []) },
  { key: "gen", label: "Generation", pick: (c) => c.generations || [] },
];

// key -> Set of chosen values. Module-level so build and render share one source.
const SEL = Object.fromEntries(FACETS.map((f) => [f.key, new Set()]));
const selCount = () => FACETS.reduce((n, f) => n + SEL[f.key].size, 0);
const passesFacets = (c) => FACETS.every((f) => {
  const set = SEL[f.key];
  return !set.size || f.pick(c).some((v) => set.has(v));
});

// The compact filter panel: one chip group per facet, built from the roster.
// The Holomem group carries each character's face (the game's mini icon).
function buildFilterUI() {
  const panel = document.getElementById("cfpanel");
  if (!panel) return;
  const labels = (state.meta && state.meta.gen_labels) || {};
  panel.textContent = "";

  const faceFor = {};
  for (const c of state.cards)
    if (c.member && !(c.member in faceFor)) faceFor[c.member] = chrOf(c);

  for (const f of FACETS) {
    let opts;
    if (f.fixed) {
      opts = f.fixed;
    } else {
      const seen = new Set();
      for (const c of state.cards) for (const v of f.pick(c)) if (v) seen.add(v);
      opts = [...seen].sort(f.sort || undefined)
        .map((v) => [v, f.key === "gen" ? (labels[v] || v) : v]);
    }
    if (!opts.length) continue;

    const group = el("div", "cfgroup");
    group.append(el("p", "cfgroup-lab", f.label));
    const chips = el("div", "cfchips");
    for (const [val, lab] of opts) {
      const chip = el("button", "cfchip" + (SEL[f.key].has(val) ? " on" : ""));
      chip.type = "button";
      if (f.face && faceFor[val]) {
        const img = el("img", "cfchip-face");
        img.loading = "lazy";
        img.alt = "";
        img.src = "/chricon/" + encodeURIComponent(faceFor[val]);
        img.addEventListener("error", () => img.remove());
        chip.append(img);
      }
      if (f.icon) {
        const img = el("img", "cfchip-ico");
        img.loading = "lazy";
        img.alt = "";
        img.src = f.icon(val);
        img.addEventListener("error", () => img.remove());
        chip.append(img);
      }
      chip.append(document.createTextNode(lab));
      chip.addEventListener("click", () => {
        if (SEL[f.key].has(val)) { SEL[f.key].delete(val); chip.classList.remove("on"); }
        else { SEL[f.key].add(val); chip.classList.add("on"); }
        render();
        syncFilterChrome();
      });
      chips.append(chip);
    }
    group.append(chips);
    panel.append(group);
  }
}

// "Filters · N" count and the Clear affordance, in one place.
function syncFilterChrome() {
  const n = selCount();
  const toggle = document.getElementById("cftoggle");
  if (toggle) toggle.textContent = n ? `Filters · ${n}` : "Filters";
  const reset = document.getElementById("cfreset");
  const q = (document.getElementById("cfilter").value || "");
  if (reset) reset.hidden = !n && !q;
}

function render() {
  const list = document.getElementById("clist");
  list.textContent = "";
  const filter = (document.getElementById("cfilter").value || "").toLowerCase();
  const shown = state.cards.filter(
    (c) =>
      (!filter ||
        (c.member || "").toLowerCase().includes(filter) ||
        c.id.toLowerCase().includes(filter)) &&
      passesFacets(c)
  );
  const reset = document.getElementById("cfreset");
  if (reset) reset.hidden = !selCount() && !filter;
  for (const card of shown) list.append(rowFor(card));
  // A filter that matches nothing must say so. An empty grid under an active
  // filter is indistinguishable from a panel that failed to load -- which is
  // exactly the silent-empty-panel failure this page shipped once already.
  if (!shown.length) {
    const none = el("p", "cnone");
    none.textContent = state.cards.length
      ? "No card matches these filters."
      : "No cards loaded.";
    list.append(none);
  }
  // The completeness line says WHY an answer may be wrong before the reader
  // reaches it: how many cards are still on the document default rather than
  // on something the user typed.
  const typed = state.cards.filter((c) => c.source === "manual").length;
  const bits = [`${shown.length} of ${state.cards.length} cards`];
  if (typed !== state.cards.length) {
    bits.push(`${typed} typed, ${state.cards.length - typed} on the "` +
      `${state.doc.card_default ?? "max"}" default`);
  }
  document.getElementById("ccount").textContent = bits.join(" · ");
  // Counted over the WHOLE collection, not the filtered view: the banner is a
  // statement about the document, and a filter must not make a problem look
  // solved.
  refreshOverstatedBanner();
}

// The three account scalars. No bound is invented here: `memory_max_posters`,
// `power_up_cap_permyriad` and `player_level_max` are the game's, and
// `power_up_reachable_permyriad` is DERIVED from the current roster by the
// server and moves when a card ships. `board_level_gates` likewise comes off
// skilltree.json, so what a level is said to unlock is the master's answer.
function mountAccount() {
  const posters = document.getElementById("cposters");
  const powerup = document.getElementById("cpowerup");
  const level = document.getElementById("clevel");
  const meta = state.meta;

  posters.max = String(meta.memory_max_posters);
  // Power-Up is typed as the PERCENT you read off the Members list (e.g. 9.74),
  // not the internal permyriad — the cap is 50 %.
  powerup.max = String((meta.power_up_cap_permyriad || 5000) / 100);
  if (meta.player_level_min != null) level.min = String(meta.player_level_min);
  if (meta.player_level_max != null) level.max = String(meta.player_level_max);

  const player = state.doc.player || (state.doc.player = {});
  // An untouched slider must read as "not told us", not as 0 — so the input is
  // only given a value when the document actually carries one.
  if (player.posters !== undefined) posters.value = String(player.posters);
  if (player.power_up_permyriad !== undefined) {
    powerup.value = String(player.power_up_permyriad / 100);
  }
  if (player.level !== undefined) level.value = String(player.level);

  function permil(t) {
    return t === 0 ? 0 : Math.min(2 * t, t + 30);
  }
  function refresh() {
    const p = state.doc.player || {};
    const hasPosters = p.posters !== undefined;
    const hasPower = p.power_up_permyriad !== undefined;
    document.getElementById("cmemory").textContent = hasPosters
      ? `Memory Bonus ${(permil(p.posters) / 10).toFixed(1)} % of Member Parameter`
      : "not set — reported as absent, not as zero";
    const reach = meta.power_up_reachable_permyriad;
    // The roster size comes off the same response as the figure it describes,
    // never a literal: it moves when the game ships a card.
    const rosterSize = meta.power_up_reachable_roster_size;
    const roster = rosterSize != null ? `${rosterSize}-card roster` : "roster";
    document.getElementById("cpowerhint").textContent = !hasPower
      ? "not set — reported as absent, not as zero"
      : reach != null && p.power_up_permyriad > reach
        ? `above the ${(reach / 100).toFixed(2)} % the current ${roster} can reach`
        : reach != null
          ? `the ${roster} reaches ${(reach / 100).toFixed(2)} % today; cap is 50 %`
          : "";

    // What a level actually buys, counted off the tree the editor draws — not
    // a claim about score. An unset level is stated as gating NOTHING, because
    // that is what board.js then does, and a page that implied otherwise would
    // be describing a lock the editor is not applying.
    const hasLevel = p.level !== undefined;
    const gates = meta.board_level_gates || [];
    const total = gates.reduce((n, g) => n + g.nodes, 0);
    const open = gates.reduce((n, g) => (g.level <= (p.level || 0) ? n + g.nodes : n), 0);
    const next = gates.find((g) => g.level > (p.level || 0));
    document.getElementById("clevelhint").textContent = !hasLevel
      ? "not set — the board editor gates nothing"
      : !total
        ? "skilltree.json is not on disk, so nothing can be gated"
        : `${open} of ${total} board nodes available` +
          (next ? ` — next ${next.nodes} at level ${next.level}` : " — every gate met");
  }

  posters.addEventListener("input", () => {
    (state.doc.player || (state.doc.player = {})).posters = Number(posters.value);
    refresh();
    markDirty();
  });
  powerup.addEventListener("input", () => {
    // Typed as a percent; stored as permyriad (9.74 % -> 974).
    const pct = powerup.value === "" ? undefined : Math.round(Number(powerup.value) * 100);
    const pl = state.doc.player || (state.doc.player = {});
    if (pct === undefined) delete pl.power_up_permyriad; else pl.power_up_permyriad = pct;
    refresh();
    markDirty();
  });
  level.addEventListener("input", () => {
    (state.doc.player || (state.doc.player = {})).level = Number(level.value);
    refresh();
    markDirty();
  });
  refresh();
}

async function load() {
  const res = await fetch("/api/collection");
  if (!res.ok) throw new Error(`GET /api/collection -> ${res.status}`);
  const data = await res.json();
  state.cards = data.cards;
  state.meta = data;

  // A draft in `localStorage` wins over the disk file on load — it is by
  // definition newer, and losing an unsaved edit to a page reload is the one
  // thing a working copy exists to prevent. The banner says which is showing.
  const draft = loadDraft();
  state.doc = draft || data.document;
  const status = document.getElementById("cstatus");
  if (draft) {
    status.textContent = "showing an unsaved draft from this browser";
    status.className = "cstatus dirty";
  } else {
    status.textContent = `loaded ${data.path}`;
    status.className = "cstatus";
  }
  if (!data.curve_available) {
    document.getElementById("cwarnbanner").hidden = false;
  }
  document.getElementById("cdefault").value =
    typeof state.doc.card_default === "string" ? state.doc.card_default : "custom";
  mountAccount();
  setGenColors(state.meta && state.meta.gen_colors, state.meta && state.meta.gen_labels);
  buildFilterUI();
  render();
  syncFilterChrome();
}

async function save() {
  const status = document.getElementById("cstatus");
  status.textContent = "saving…";
  status.className = "cstatus";
  const res = await fetch("/api/collection", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(state.doc),
  });
  const data = await res.json();
  if (!res.ok) {
    status.textContent = `not saved: ${data.error || res.status}`;
    status.className = "cstatus err";
    return;
  }
  state.dirty = false;
  localStorage.removeItem(LS_KEY);
  // Report the SOLVER's verdict, not this page's optimism: the server read the
  // file back through the same loader `hbuilder solve` uses.
  const bits = [`saved to ${data.path}`];
  if (data.cards_restated !== undefined) {
    bits.push(`${data.cards_restated} card(s) restated`);
  }
  if (data.not_fully_scorable && data.not_fully_scorable.length) {
    bits.push(
      `${data.not_fully_scorable.length} below bloom ${state.meta.fully_scorable_bloom}`
    );
  }
  status.textContent = bits.join(" · ");
  status.className = "cstatus ok";
}

export function mountCollection() {
  mountEditorDialog();
  document.getElementById("csave").addEventListener("click", () => {
    save().catch((e) => {
      const status = document.getElementById("cstatus");
      status.textContent = `not saved: ${e}`;
      status.className = "cstatus err";
    });
  });
  document.getElementById("cfilter").addEventListener("input", () => { render(); syncFilterChrome(); });
  const cftoggle = document.getElementById("cftoggle");
  if (cftoggle) cftoggle.addEventListener("click", () => {
    const panel = document.getElementById("cfpanel");
    if (panel) panel.hidden = !panel.hidden;
  });
  const reset = document.getElementById("cfreset");
  if (reset) {
    reset.addEventListener("click", () => {
      for (const f of FACETS) SEL[f.key].clear();
      document.querySelectorAll("#cfpanel .cfchip.on").forEach((c) => c.classList.remove("on"));
      document.getElementById("cfilter").value = "";
      render();
      syncFilterChrome();
    });
  }
  document.getElementById("cdefault").addEventListener("change", (ev) => {
    state.doc.card_default = ev.target.value;
    markDirty();
    render();
  });
  document.getElementById("cwrap").addEventListener(
    "toggle",
    function once() {
      if (!this.open || state.cards.length) return;
      load().catch((e) => {
        document.getElementById("cstatus").textContent = String(e);
        document.getElementById("cstatus").className = "cstatus err";
      });
    }
  );
}
