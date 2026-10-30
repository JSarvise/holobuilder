// ranking.js — the full N-team ranking.
//
// The server returns N teams for the `top` box; this module renders all of
// them, not just the first.
//
// A RANKING IS A SET OF DIFFERENCES, NOT A COLUMN OF NUMBERS. On a real
// 91-card pool: rank 1 = 459,427.59728535096 and rank 2 =
// 459,425.9071651244 — a gap of **1.7 on 459k**, and the two teams differ
// by exactly one member card. Printed at one decimal in a column those two
// rows are visually identical and the reader learns nothing. So every row
// carries its delta against #1 AND against its own predecessor, a bar
// scaled to the spread of the list, and the cards that came out and went
// in against the rank above it.
//
// AND THE DELTA IS QUOTED AGAINST THE NOISE IT LIVES IN. Four AUTO runs of
// one song with an identical team scored
// 578,424 / 617,974 / 650,951 / 702,485 — the active-skill RNG is worth
// **21.4 %**. A 0.0004 % gap is a real ordering of the model and a
// meaningless ordering of an evening's play, and the page says so rather
// than letting a bold number imply otherwise.

const RNG_SPREAD = 0.214; // observed over 4 AUTO runs of one song, identical team.

// --- which number this ranking is ABOUT --------------------------------------
// ⛔ `sorted_by` is the payload's own statement of the field it ordered the
// teams by: "song_score" when a chart was chosen,
// "score" — or absent, on a server from before the field existed — for the
// song-agnostic run. EVERYTHING that prints or draws a score quantity (the
// headline, the deltas, the %, the gap bars, the noise band) reads the teams
// through the ONE accessor built here. That is what makes "sorted by one
// number, displaying another" structurally impossible rather than something
// every call site must remember: there is no second field to drift to.
//
// ⚠ `score` and `song_score` are different quantities on different scales.
// No caller may subtract one from the other, and none does: `metric.read` is
// the only score read in this file's arithmetic.
const SCORE_METRIC = { field: "score", label: "score", read: (t) => t.score };

export function metricOf(payload) {
  const field = (payload && payload.sorted_by) || "score";
  if (field === "score") return SCORE_METRIC;
  // --across-songs sorts by `across_score` (the MEAN over every chart); the
  // sorted_by name and the team field differ, so map it explicitly.
  if (field === "across_song") {
    return {
      field: "across_score",
      label: "mean per-song score",
      read: (t) => t.across_score,
    };
  }
  return {
    field,
    // "per-song score" is the page's prose name for `song_score`; a field
    // this file has never heard of keeps its own raw name rather than
    // getting a guessed phrase — an unknown quantity should look unknown.
    label: field === "song_score" ? "per-song score" : field.replace(/_/g, " "),
    read: (t) => t[field],
  };
}

// Force "en-US" grouping (1,234.5), never the browser locale: the CLI prints
// en-US and a Spanish-locale browser would otherwise show 1.234,5 for the SAME
// number — the project's named "two numbers that read differently" failure.
export const num = (n, d = 0) =>
  n === null || n === undefined || Number.isNaN(n)
    ? "—"
    : n.toLocaleString("en-US", {
        minimumFractionDigits: d,
        maximumFractionDigits: d,
      });

const nameOf = (cards, id) => (cards[id] ? cards[id].member : id);

import { el } from "/dom.js";
import { certPill, certFold } from "/certpill.js";

// `gen_colors` from the payload: {generation name: {c, c2}}. Set once per
// render so `cardTile` stays a pure function of its arguments plus this.
export let GEN_COLORS = {};
// Display labels only: `cards.json` spells generations mixed (`Advent` beside
// `0期生`), and this maps each to the GAME'S own English name from
// `CharacterGrouping`. The raw value is what stays in `card.generations`,
// because a gate reads `generation:Myth >= 2` against it.
export let GEN_LABELS = {};
export function setGenColors(map, labels) {
  GEN_COLORS = map || {};
  if (labels !== undefined) GEN_LABELS = labels || {};
}

const RANK_OF = (rarity) => {
  const m = /(\d+)/.exec(String(rarity || ""));
  return Math.min(Math.max(m ? +m[1] : 3, 3), 5);
};

// The chrome sprites, served from `dumps/card_chrome/` in place by `/chrome/`.
// A 404 is a first-class outcome: the piece is dropped and the rest of the face
// draws. That is deliberate -- a placeholder element badge or rarity band would
// be a WRONG factual claim about the card, which is worse than an absent one.
const CHROME = {
  el: (type) => `/chrome/element/ui_999_ico_element_${String(type||"").toLowerCase()}_cl.png`,
  rbar: (rank) => `/chrome/rarity/ui_999_thm_card_rarity_r${rank}.png`,
  star: "/chrome/rarity/ui_999_img_card_star.png",
  flower: (on) => `/chrome/bloom/ui_999_img_flower${on ? "" : "_blank"}.png`,
};

const dropOn404 = (img) => {
  img.addEventListener("error", () => img.closest(".drop, .gens, .stars, .bloom, .clip")?.remove()
    || img.remove());
  return img;
};

function sprite(cls, src, alt) {
  const img = el("img", cls);
  img.loading = "lazy";
  img.decoding = "async";
  img.alt = alt || "";
  img.src = src;
  return dropOn404(img);
}

// One line for a skill, in the data's OWN vocabulary -- the same `family /
// effect_type` the score-breakdown table already prints, so the page says one
// thing in one way. Nothing is paraphrased into game-speak that the data does
// not support: `%` is appended only for `stat_up`, where the game's own text
// ("Grants All Stats UP 15% to all") confirms the unit.
function skillLine(sk) {
  if (!sk) return "none";
  const pretty = (v) => String(v || "").replace(/_/g, " ");
  const parts = [pretty(sk.effect_type)];
  if (typeof sk.magnitude === "number") {
    parts.push(sk.family === "stat_up" ? `+${sk.magnitude}%` : `${sk.magnitude}`);
  } else if (sk.magnitude != null) {
    parts.push(String(sk.magnitude));
  }
  if (sk.target && sk.target !== "none") parts.push(`→ ${pretty(sk.target)}`);
  if (sk.condition && sk.condition !== "ungated") parts.push(`(${pretty(sk.condition)})`);
  return parts.join(" ");
}

// --- the card face ---------------------------------------------------------
// Geometry is NOT designed here. Every value lives in style.css and was ported
// from `tools/explorer/build_explorer.py`'s face CSS, which traces to the
// game's prefab measurements. This function only decides the
// element structure those rules need -- notably a real `.ring` child rather
// than an inset shadow, because an inset shadow paints under an <img> child
// and would vanish behind the portrait.
//
// `role` is not drawn on the card: it is the group box's label, so a
// per-card colour can never be mistaken for the role.
//
// ⚠ `lead` is not decoration. **The leader contributes no stats and no
// SPECIAL/ACTIVE/PASSIVE skill — only its OUTFIT** (the five member power
// values sum to Overall Power exactly on every deck checked). So a leader
// tile printing P/T/S numbers is showing
// a quantity that enters no total anywhere. The leader tile prints what the
// outfit DOES instead.
export function cardTile(card, opts, mark) {
  const lead = !!(opts && opts.lead);
  const tile = el("div", "tile " + (card ? card.type : "") +
    (lead ? " lead" : "") + (mark ? " swapped" : ""));
  const rank = card ? RANK_OF(card.rarity) : 3;

  const face = el("div", "face");
  if (card) {
    // The 512x1024 portrait, recovered for every card via octo_fetch. Always
    // attempted; it adds `.has` on load so
    // the gradient shows until it arrives, and a 404 drops it back to the gradient.
    const img = el("img", "port");
    img.loading = "lazy";
    img.alt = card.member;
    // Ask for a 512-wide render of the native master — crisp even on retina at the
    // face's display size, without pulling the multi-MB full-res texture.
    img.src = "/cardart/vert/" + card.id + "?w=512";
    img.addEventListener("load", () => face.classList.add("has"));
    img.addEventListener("error", () => { img.remove(); face.classList.remove("has"); });
    face.append(img);
  }
  face.append(el("div", "ring"));

  if (card) {
    // element badge, top-left
    face.append(sprite("el drop", CHROME.el(card.type), card.type));

    // generation ribbons, top-right, tinted with the game's own field 100/101
    const gens = el("div", "gens");
    for (const name of card.generations || []) {
      const gen = el("div", "gen");
      const tint = GEN_COLORS[name];
      if (tint) {
        gen.style.setProperty("--gc", tint.c);
        gen.style.setProperty("--gc2", tint.c2 || tint.c);
      }
      gen.append(el("i"), el("b", null, GEN_LABELS[name] || name));
      gens.append(gen);
    }
    if (gens.childElementCount) face.append(gens);

    // rarity band along the bottom, clipped to the rounded corners
    const clip = el("span", "clip");
    clip.append(sprite("rbar", CHROME.rbar(rank)));
    face.append(clip);

    // star column, bottom-right, with the swept glint
    const stars = el("div", "stars");
    stars.style.setProperty("--star", `url(${CHROME.star})`);
    for (let i = 0; i < rank; i++) {
      const cell = el("span");
      cell.style.setProperty("--i", String(rank - 1 - i));
      cell.append(sprite(null, CHROME.star), el("i"));
      stars.append(cell);
    }
    face.append(stars);

    // Lv and bloom. Absent state draws NEITHER badge rather than a made-up
    // number -- `build_explorer.py`'s rule, and the reason is the same: a
    // level the player never stated is not a fact about the account.
    if (card.bloom != null) {
      const bloom = el("div", "bloom" + (card.bloom > 0 ? "" : " off"));
      bloom.append(sprite(null, CHROME.flower(card.bloom > 0)), el("em", null, String(card.bloom)));
      face.append(bloom);
    }
    if (card.level != null) {
      const lvl = el("div", "lvl");
      lvl.append(el("span", null, "Lv"), el("b", null, String(card.level)));
      face.append(lvl);
    }
  }
  if (mark) face.append(el("span", "swap", "NEW"));

  // On a leader the OUTFIT is the subject, so it leads and the character is the
  // subtitle. On a member the card is the subject and the outfit is a detail.
  const primary = lead && card && card.costume_name ? card.costume_name : (card ? card.member : "(unknown card)");
  const secondary = lead ? (card ? card.member : "") : (card ? card.costume_name || "" : "");
  const name = el("div", "nm", primary);
  const gen = el("div", "gn", secondary);
  // `|| []` is load-bearing, not defensive noise: not every payload that draws a
  // card carries `generations`, and without it this line throws
  // `card.generations is undefined` on the ones that do not.
  gen.title = card ? (card.generations || []).join(" · ") : "";

  // `opts.foot` lets a caller supply its own footer node -- the collection
  // editor puts its level/bloom controls there. That is the whole reason this
  // function takes an options object: ONE card renderer with variants, rather
  // than a second copy that drifts.
  let footer;
  if (opts && opts.foot) {
    footer = opts.foot;
  } else if (lead) {
    footer = el("div", "outfit");
    footer.append(el("span", "olab", "Outfit skill"));
    footer.append(el("span", "oeff", card ? skillLine(card.costume_skill) : "—"));
  } else {
    // P / T / S / TOTAL, each under its own badge. The three circles are the
    // game's `Ellipse 1296` node tinted CardStatusPf/Te/Se -- there is no icon
    // sprite for them, so the circle in style.css IS the asset.
    //
    // These are the CARD's own stats, already restated by the collection for
    // its level and bloom (`apply_collection`), and they carry NO team bonus:
    // outfit and passive `stat_up` are `stat_entries` applied across the team
    // in `_effective_stat_sum`, never folded back into a card.
    footer = el("div", "st");
    if (card) {
      const rows = [
        ["pf", "P", card.stats.performance],
        ["te", "T", card.stats.technique],
        ["se", "S", card.stats.sense],
        ["tot", "TOTAL", card.stat_sum],
      ];
      for (const [cls, glyph, value] of rows) {
        const cell = el("div", cls === "tot" ? "tot" : null);
        cell.append(el("span", "sico " + cls, glyph), el("b", null, num(value)));
        footer.append(cell);
      }
    }
  }

  tile.append(face, name, gen, footer);
  tile.title = card ? card.id : "";
  return tile;
}

// --- tables ----------------------------------------------------------------
function table(headers, rows) {
  const t = el("table", "grid");
  const thead = el("thead");
  const htr = el("tr");
  for (const h of headers) htr.append(el("th", null, h));
  thead.append(htr);
  const tbody = el("tbody");
  for (const cells of rows) {
    const tr = el("tr");
    for (const [cls, value] of cells) {
      const td = el("td", cls);
      if (value instanceof Node) td.append(value);
      else td.textContent = value;
      tr.append(td);
    }
    tbody.append(tr);
  }
  t.append(thead, tbody);
  // The table is wrapped rather than made `display:block` itself: a
  // `display:block` <table> wraps thead and tbody in two SEPARATE anonymous
  // table boxes, and the header columns then stop lining up with the body's.
  // Wide content scrolls inside this box; the page body never scrolls
  // sideways.
  const wrap = el("div", "gridwrap");
  wrap.append(t);
  return wrap;
}

// A gate written the way a person would say it out loud. `generation:3期生 ≥ 2`
// is exact and unreadable; "needs 2+ Gen 3 members" is the same fact in the
// reader's language. The raw expression is NOT dropped — it moves to its own
// column beside this one, because the whole value of this tool is that its
// numbers are traceable back to the data.
//
// The generation value is passed through GEN_LABELS, the game's own English
// name for the group, exactly as the card ribbons already do. The raw key is
// what the solver matched on and it stays visible in the `condition` column.
function gatePlain(gate) {
  const noun = gate.dimension === "generation"
    ? `${GEN_LABELS[gate.value] || gate.value} members`
    : gate.dimension === "type"
      ? `${gate.value} members`
      : `${gate.value} (${gate.dimension})`;
  return `needs ${gate.threshold}+ ${noun}`;
}

function gatesTable(team, cards) {
  return table(
    // Order matters: `.grid th:nth-child(4)` and `th:last-child` are the two
    // right-aligned columns in style.css, so the count and the verdict have to
    // sit in exactly those two slots. The traceable raw expression goes
    // between them, left-aligned like the prose column it belongs to.
    ["card", "slot", "what the skill needs", "has", "condition", ""],
    team.gates.map((gate) => {
      const mark = el(
        "span",
        gate.fired ? "fired" : "unfired",
        gate.fired ? "met" : "NOT MET"
      );
      mark.title = gate.fired
        ? "The team satisfies this condition, so the skill pays out."
        : "The team does not satisfy this condition, so this skill pays nothing.";
      return [
        ["k", nameOf(cards, gate.card_id)],
        ["k", gate.slot_kind],
        ["", gatePlain(gate)],
        ["v", `${gate.actual_count} of ${gate.threshold}`],
        ["k raw", `${gate.dimension}:${gate.value} ≥ ${gate.threshold}`],
        ["v", mark],
      ];
    })
  );
}

// The data's vocabulary, said in words, WITH the raw token kept beside it.
//
// `stat_up` / `gate_not_met` are the words the data itself uses, and a bare
// translation would put a second vocabulary on screen. So the page translates
// and keeps the raw term alongside: it reads in English without becoming
// untraceable back to the file. It is the shape `gatesTable` already uses
// (plain phrase, raw expression beside it), extended to the breakdown.
//
// Every phrase is a restatement of what the family NAME says, never a claim
// about a mechanic. The one that goes further is `score_support`, and it is
// measured rather than guessed: the model routes it to a support pool that
// MULTIPLIES the ACTIVE pool (in a cross-deck test the support-multiplier
// reading held to 6.2 % while direct-score spread 90 %).
const EFFECT_PLAIN = {
  "stat_up/performance": "raises Performance",
  "stat_up/technique": "raises Technique",
  "stat_up/sense": "raises Sense",
  "stat_up/all_stats": "raises all three stats",
  "conditional_score_up/score": "raises score",
  "score_support/score": "boosts every Active skill",
  "activation_rate_up/activation_rate": "fires more often",
  "life_recovery/life": "recovers LIFE",
  "judgment_enhancement/judgment": "improves judgment",
};

const STATE_PLAIN = {
  applied: "paid out",
  gate_not_met: "paid nothing",
  data_missing: "not scorable",
};

const STATE_TITLE = {
  applied: "This skill's condition holds, so it contributed to the score.",
  gate_not_met:
    "This skill's condition failed against this team, so it contributed nothing.",
  data_missing:
    "A field this skill needs is untranscribed, so it could not be scored at all "
    + "— this is a data gap, not a zero.",
};

// The plain phrase, with the raw `family / effect_type` beside it as the
// traceable secondary. Anything the map has not heard of falls back to the raw
// token ALONE rather than to an invented phrase — an unknown family is
// something to notice, not something to paraphrase.
function effectPlain(family, effectType) {
  return EFFECT_PLAIN[`${family}/${effectType}`] || null;
}

function withRaw(plain, raw) {
  const wrap = el("span");
  if (plain) {
    wrap.append(document.createTextNode(plain + " "));
    wrap.append(el("span", "rawin", raw));
  } else {
    wrap.append(el("span", "rawin", raw));
  }
  return wrap;
}

function contribsTable(contributions, cards) {
  const sorted = [...contributions].sort(
    (a, b) => (b.weighted_rate || 0) - (a.weighted_rate || 0)
  );
  return table(
    ["card", "slot", "what it does", "state", "weighted rate"],
    sorted.map((c) => {
      const chip = el(
        "span",
        "state-" + c.state,
        STATE_PLAIN[c.state] || c.state
      );
      if (STATE_TITLE[c.state]) chip.title = STATE_TITLE[c.state];
      const state = el("span");
      state.append(chip, document.createTextNode(" "), el("span", "rawin", c.state));
      return [
        ["k", nameOf(cards, c.card_id)],
        ["k", c.slot_kind],
        ["", withRaw(effectPlain(c.family, c.effect_type), `${c.family} / ${c.effect_type}`)],
        ["", state],
        [
          "v",
          c.weighted_rate === null || c.weighted_rate === undefined
            ? "—"
            : Number(c.weighted_rate).toFixed(4),
        ],
      ];
    })
  );
}

// --- roster diffs ----------------------------------------------------------
// `Team.leader` is a SEPARATE slot that may name a card which is also a
// member (`domain/team.py`, MEMBER_COUNT = 5), so the leader tile is
// expected to repeat one of the five. For *diff* purposes the roster is the
// set union — a card is "in this team" once, however many slots it fills.
const rosterOf = (team) => new Set([team.leader_id, ...team.member_ids]);

function rosterDiff(previous, current) {
  const a = rosterOf(previous);
  const b = rosterOf(current);
  return {
    out: [...a].filter((id) => !b.has(id)),
    in: [...b].filter((id) => !a.has(id)),
  };
}

// --- per-rank explain lookup ----------------------------------------------
// `build_payload` attaches `contributions` to exactly ONE team — the rank
// `--explain` named (`json_output.py`). The server therefore sends
// the rest under `explain_by_rank`, built by the SAME `build_team_object`
// call, so this is a second *location* and never a second computation.
// Resolved by ROSTER when the team is known: after a develop re-orders the
// teams (or a refine re-composes one), rank numbers no longer name the same
// team, and looking up by rank would show ANOTHER team's breakdown. Rank
// is only a fallback for payloads that were never re-ordered.
const rosterKey = (leader, members) =>
  [leader, ...[...(members || [])].sort()].join("|");
const explainBlock = (payload, rank, team) => {
  const entries = payload.explain_by_rank || [];
  if (team && team.leader_id) {
    const want = rosterKey(team.leader_id, team.member_ids);
    // `/api/run` keys the FINAL rosters' attribution by this same string,
    // computed on each roster's own developed board.
    if (payload.explain && payload.explain[want]) return payload.explain[want];
    const hit = entries.find((entry) =>
      Array.isArray(entry.roster) &&
      rosterKey(entry.roster[0], entry.roster.slice(1)) === want);
    if (hit) return hit;
    // developed/refined payload with no roster match: say nothing rather than
    // show a stranger's numbers
    if (payload._developed || (Array.isArray(team.refined) && team.refined.length)) return null;
  }
  return entries.find((entry) => entry.rank === rank);
};

const contribsFor = (payload, rank, team) => {
  const entry = explainBlock(payload, rank, team);
  return entry ? entry.contributions : null;
};

const termsFor = (payload, rank, team) => {
  const entry = explainBlock(payload, rank, team);
  return entry ? entry.terms : null;
};

// --- the term panel --------------------------------------------------------
// The game's own two-column shape (Power, then Bonus), with only the rows the
// model can defend. Two things this panel must never do:
//
//   1.   It is NOT the game's Unit Score. `total_score` is the deck-screen
//        power assembly wearing the in-run score stage — the deck screen ADDS
//        the board score bonus where the model MULTIPLIES it, worth
//        +2.02..+11.76 % and TEAM-DEPENDENT. The server sends
//        `is_unit_score: false`; the heading is driven off it rather than
//        hardcoded, so the label cannot drift away from the flag.
//
//   2.   The bonus column has ONE combined row, not the game's four. The pool
//        is (1 + B) x max per bucket, not a sum, so splitting it would change
//        the number rather than report it. The row says so on screen — a
//        merged row that does not explain itself reads as a missing feature.
//
// A `null` value is "not modelled" and renders as an em dash. It is never a
// zero: a term not computed and a term worth nothing are different facts.
const BASIS_NOTE = {
  combined: "not separable",
  folded: "folded in",
  "not-modelled": "not modelled",
};

function termRow(row) {
  const value =
    row.value === null || row.value === undefined
      ? "—"
      : row.unit === "permil"
        ? `${num(row.value, 1)} ‰`
        : num(row.value, 0);

  const label = el("span", null, row.label);
  const cell = el("div", "term-label");
  cell.append(label);
  if (BASIS_NOTE[row.basis]) {
    cell.append(el("span", "term-basis", BASIS_NOTE[row.basis]));
  }
  if (row.note) {
    const why = el("span", "term-why", "?");
    why.title = row.note;
    cell.append(why);
  }
  return [
    ["", cell],
    ["v mono", value],
  ];
}

function termsPanel(terms) {
  const wrap = el("div", "terms");

  wrap.append(el("h4", "term-head", "Power"));
  wrap.append(table(["term", "value"], terms.power.map(termRow)));
  const power = el("p", "term-total");
  power.append(el("span", null, "Overall Power"));
  power.append(el("b", "mono", num(terms.overall_power, 0)));
  wrap.append(power);

  wrap.append(el("h4", "term-head", "Bonus — one multiplied factor each"));
  wrap.append(table(["term", "value"], terms.bonus.map(termRow)));

  const total = el("p", "term-total grand");
  total.append(el("span", null, terms.total_label));
  total.append(el("b", "mono", num(terms.total_score, 1)));
  wrap.append(total);

  // "not the Unit Score", on screen and not only in a comment.
  if (terms.is_unit_score === false) {
    wrap.append(
      el(
        "p",
        "note",
        "Not the game's Unit Score. The deck screen ADDS the board score " +
          "bonus; this multiplies it — a team-dependent +2.0…+11.8 % apart " +
          "(DECK-SCORE.md §3c). Use it to compare teams, not to match the " +
          "number on the deck screen."
      )
    );
  }

  // The deck screen's own additive shape, beside ours. Rendered in a muted
  // style on purpose: the formula is exact but the base is not, so it must
  // not compete visually with the ranking score above it. A number shown as
  // confidently as a correct one is how a ±13 % figure gets quoted.
  const deck = terms.deck_screen;
  if (deck) {
    const row = el("p", "term-total deck");
    row.append(
      el("span", null, "deck-screen shape" + (deck.complete ? "" : " (incomplete)"))
    );
    row.append(el("b", "mono", num(deck.unit_score, 0)));
    wrap.append(row);
    wrap.append(el("p", "note", deck.caveat));
    if (!deck.complete) {
      wrap.append(
        el(
          "p",
          "note",
          "Missing from Overall Power: " +
            deck.missing.join(", ") +
            " — enter them in My collection so this sum is the game's."
        )
      );
    }
  }

  const outside = terms.outside_total || [];
  if (outside.length) {
    wrap.append(el("h4", "term-head", "Shown, but not in the total"));
    wrap.append(table(["term", "value"], outside.map(termRow)));
    wrap.append(
      el(
        "p",
        "note",
        "The game's panel lists these; our ranking drops them, checked to " +
          "never change the order. They are displayed so the omission " +
          "is visible rather than silent."
      )
    );
  }
  return wrap;
}

const explainFor = (payload, rank, team) => {
  const entry = explainBlock(payload, rank, team);
  if (entry && entry.lines && entry.lines.length) return entry.lines;
  if (payload._developed || (team && Array.isArray(team.refined) && team.refined.length)) return null;
  return rank === 1 ? payload.explain_lines : null;
};

// --- one rank --------------------------------------------------------------
// Every score-shaped number in this block — the headline, both deltas, the %,
// the gap bar — comes from `metric.read`. `team.score` appears below exactly
// once, in the song-mode reference line that NAMES it, and never in a
// subtraction against the metric.
function rankBlock(payload, team, best, previous, spread, metric) {
  const cards = payload.cards || {};
  const songMode = metric.field !== "score";
  const value = metric.read(team);
  const bestValue = metric.read(best);
  const deltaBest = value - bestValue;
  const deltaPrev = previous ? value - metric.read(previous) : null;
  const relBest = bestValue ? Math.abs(deltaBest) / bestValue : 0;
  const changed = previous ? rosterDiff(previous, team) : null;
  const swappedIn = changed ? new Set(changed.in) : new Set();

  const block = el("details", "rank");
  block.open = team.rank === 1;

  // -- the summary line: rank, score, both deltas, the bar, the swap ------
  const summary = el("summary");
  summary.append(el("span", "rk", "#" + team.rank));
  summary.append(el("b", "sc mono", num(value, 1)));
  // --across-songs: how many charts this team tops. A team can lead by MEAN yet
  // win few charts, so the count is shown next to the mean it is ordered by.
  if (typeof team.wins === "number") {
    summary.append(
      el("span", "wins", `${team.wins} win${team.wins === 1 ? "" : "s"}`)
    );
  }

  const deltas = el("span", "dl mono");
  if (team.rank === 1) {
    deltas.append(el("span", "d0", "best"));
  } else {
    const d1 = el("span", "dneg", num(deltaBest, 1) + " vs #1");
    // `metric.label` is "score" in the agnostic run.
    d1.title =
      `${num(Math.abs(deltaBest), 4)} below rank 1 on a ${metric.label} of ` +
      `${num(bestValue, 1)} — ${(relBest * 100).toFixed(4)} %`;
    deltas.append(d1);
    // For rank 2 the predecessor IS rank 1, so a second column would print
    // the same number twice. Shown only where it says something new.
    if (deltaPrev !== null && previous.rank !== 1) {
      deltas.append(
        el("span", "dprev", num(deltaPrev, 1) + " vs #" + previous.rank)
      );
    }
    deltas.append(
      el(
        "span",
        relBest < RNG_SPREAD ? "within" : "outside",
        (relBest * 100).toFixed(4) + " %"
      )
    );
  }
  summary.append(deltas);

  // The bar is scaled to the spread of THIS list, not to the score, because
  // the whole list sits inside ~0.8 % of the score — a bar scaled to the
  // score is the same useless column the number was.
  const bar = el("span", "gapbar");
  const fill = el("i");
  fill.style.width =
    (spread > 0 ? (Math.abs(deltaBest) / spread) * 100 : 0).toFixed(2) + "%";
  bar.append(fill);
  bar.title = `gap to #1, scaled to this ranking's own ${num(spread, 1)} spread`;
  summary.append(bar);

  // Not a bare `gates 3/4`: that names a concept the page never defines and
  // gives no hint that the missing one COSTS this team a skill.
  const gatesFired = team.gates.filter((g) => g.fired).length;
  const missed = team.gates.length - gatesFired;
  const gt = el(
    "span",
    "gt" + (missed ? " gt-miss" : ""),
    `${gatesFired} of ${team.gates.length} conditions met`
  );
  gt.title = missed
    ? `${missed} skill${missed === 1 ? "" : "s"} on this team need a condition ` +
      `the team does not satisfy, so ${missed === 1 ? "it pays" : "they pay"} ` +
      `nothing. Open the row to see which.`
    : "Every skill condition on this team is satisfied.";
  summary.append(gt);

  // Song mode, developed: how much of the song has an ACTIVE live and how much
  // of the summed active value the engine's MAX rule throws away when actives
  // stack. Read off the same timeline the row draws — a #1 that wins on raw
  // power despite a piled-up lattice says so here instead of hiding it.
  if (typeof team.active_cover_pct === "number") {
    const loss = team.active_overlap_loss_pct || 0;
    const up = el(
      "span",
      "uptime" + (loss >= 25 ? " uptime-warn" : ""),
      `active ${team.active_cover_pct.toFixed(0)} % · overlap −${loss.toFixed(0)} %`
    );
    up.title =
      `${team.active_cover_pct.toFixed(1)} % of the song has at least one ACTIVE ` +
      `window live (first one at ${(team.active_dead_start_s || 0).toFixed(1)} s — ` +
      `no ACTIVE fires before its own first interval). ` +
      `When two actives overlap the run pays only the strongest (E[max], the ` +
      `binary's rule): ${loss.toFixed(1)} % of the summed active value is lost that ` +
      `way on this team. Display only — the score above already prices it in.`;
    summary.append(up);
  }

  // Swap-refined: the develop's hill-climb replaced members of the team the
  // solve posted. Say so on the row, with the swaps in the tooltip.
  if (Array.isArray(team.refined) && team.refined.length) {
    const swaps = team.refined.filter((w) => w && w.out && w.in);
    const nameOf = (id) => (cards[id] && cards[id].member) || id;
    const rf = el("span", "refined", `refined ×${swaps.length}`);
    rf.title =
      swaps.map((w) => `${nameOf(w.out)} → ${nameOf(w.in)} (+${num(w.gain, 0)})`).join("\n") +
      (team.refined.some((w) => w && w.seed === "alt")
        ? "\n(from an alternative starting roster — wide refine)" : "");
    summary.append(rf);
  }

  // Seeded: the roster came from the seed sources (tools/web/seed_rosters.json
  // or a recon artefact), not the beam — developed and scored like any other
  // candidate; the tag only says where it came from.
  if (typeof team.provenance === "string" && team.provenance.startsWith("seed:")) {
    const sd = el("span", "seeded", "seeded");
    sd.title = `Seed roster (${team.provenance.slice(5)}) — unioned with the beam's ` +
      `candidates, developed and ranked the same way.`;
    summary.append(sd);
  }

  // Certificate status: EXACT / BOUNDED / HEURISTIC / ERROR, decided
  // by the server's same-instant-same-basis guard; the fold-out is in the body.
  const cpill = certPill(team.certificate);
  if (cpill) summary.append(cpill);

  if (changed && (changed.out.length || changed.in.length)) {
    const swap = el("span", "swapline");
    for (const id of changed.out)
      swap.append(el("span", "outc", "− " + nameOf(cards, id)));
    for (const id of changed.in)
      swap.append(el("span", "inc", "+ " + nameOf(cards, id)));
    swap.title = `roster change against rank ${previous.rank}`;
    summary.append(swap);
  } else if (changed) {
    summary.append(el("span", "samecards", "same cards, different slots"));
  }
  if (team.is_lower_bound) summary.append(el("span", "tag lb", "≥ lower bound"));
  block.append(summary);

  // -- the body ------------------------------------------------------------
  const body = el("div", "rbody");

  // The plan-cache objective's labelled what-if: the
  // headline above is the team on the FIXED plan; the per-team greedy
  // re-develop is a different board and a different number — shown second,
  // named, and only where it differs. Never the number the row is ranked by.
  const whatif = team.develop_whatif;
  const whatifValue = typeof team.song_score_develop === "number" ? team.song_score_develop
    : typeof team.across_score_develop === "number" ? team.across_score_develop : null;
  if (whatif && whatifValue !== null && Math.round(whatifValue) !== Math.round(value)) {
    const pct = typeof whatif.delta_pct === "number" ? whatif.delta_pct : 0;
    const wi = el("p", "note whatif",
      `greedy re-develop what-if: ${num(whatifValue, 1)} (${pct >= 0 ? "+" : ""}${pct.toFixed(2)} %) — ` +
      `this team with its OWN greedy board instead of the fixed plan the ranking scores on. ` +
      `A what-if, not the objective: the row is ranked and certified by the number above.`);
    body.append(wi);
  }

  if (songMode && typeof team.score === "number") {
    // The stage-1 quantity, kept and LABELLED — the contract keeps `score` in
    // song mode precisely so it can be shown for reference. Never a delta
    // against the per-song number above: the two are different quantities on
    // different scales, and a subtraction would be
    // a meaningless number wearing four confident decimals.
    const ref = el(
      "p",
      "note",
      `Song-agnostic score, for reference: ${num(team.score, 1)} — the ` +
        `quantity stage 1 filtered on, on a different scale from the ` +
        `per-song score above. No delta between the two means anything.`
    );
    // The roster group's floating h4 label sits at top:-10px and eats the
    // note's last line without this clearance.
    ref.style.margin = "0 0 18px";
    body.append(ref);
  }

  // Two boxes, not six chips. The role is a property of the GROUP -- one
  // leader, five members -- so it belongs on the box. Drawing it per card in
  // the card's type colour would render two members pink and two
  // aquamarine and read as if the colour meant something about the role.
  const roster = el("div", "roster");

  const leadBox = el("div", "rgroup lead");
  leadBox.append(el("h4", null, "Leader"));
  const leadGrid = el("div", "team");
  leadGrid.append(cardTile(cards[team.leader_id], { lead: true }, false));
  leadBox.append(leadGrid);

  const memBox = el("div", "rgroup mem");
  // In song mode the arrangement IS part of the answer: cue i on the chart
  // fires slot i's SPECIAL, so the same five cards in a different order score
  // differently, and recommending a team without its slot order hands the
  // player the median case instead of the best one. The
  // tiles are drawn in `member_order` — the order that achieved the ranked
  // score — each wearing its slot number. A song-agnostic payload carries no
  // `member_order` and keeps the unordered roster.
  const ordered =
    songMode && Array.isArray(team.member_order) && team.member_order.length
      ? team.member_order
      : null;
  memBox.append(el("h4", null, ordered ? "Members — in slot order" : "Members"));
  const memGrid = el("div", "team");
  (ordered || team.member_ids).forEach((id, index) => {
    const tile = cardTile(cards[id], null, swappedIn.has(id));
    if (ordered) tile.prepend(el("div", "slotno", `slot ${index + 1}`));
    memGrid.append(tile);
  });
  memBox.append(memGrid);
  if (ordered) {
    memBox.append(el(
      "p",
      "note",
      "Field them in this left-to-right order — it is the arrangement that " +
        "achieved the score above. SPECIAL cue i on the chart fires slot i's " +
        "SPECIAL, so the order is part of the answer, not a detail."
    ));
  }

  roster.append(leadBox, memBox);
  body.append(roster);

  if (team.is_lower_bound && (team.incomplete_fields || []).length) {
    body.append(
      el(
        "p",
        "note",
        "≥ lower bound — unknown fields: " + team.incomplete_fields.join(", ")
      )
    );
  }

  const cols = el("div", "cols");
  const gcol = el("section");
  gcol.append(el("h3", null, "Gates — what each skill needs from the team"));
  // The word "gate" appears on every row of this page; one sentence defines
  // it, in front of the table that uses it.
  gcol.append(
    el(
      "p",
      "lede sm",
      "A gate is a condition a card's skill demands of the finished team — " +
        "“2 or more Cute members”, for instance. It is resolved against the " +
        "team actually assembled, not assumed. A skill whose gate is NOT MET " +
        "pays nothing at all."
    )
  );
  gcol.append(gatesTable(team, cards));
  cols.append(gcol);

  // Per-skill attribution is the product's actual value, so it is drawn for
  // every rank, not only the one the payload explains.
  const contributions = (payload._developed ? null : team.contributions)
    || contribsFor(payload, team.rank, team);
  const ccol = el("section");
  // The breakdown and the term panel below are built by the SONG-AGNOSTIC
  // scoring (stage 1); in song mode their headings say so, because a panel
  // titled "how this number is built" under a per-song headline would be
  // explaining a different number than the one it sits beneath.
  ccol.append(el(
    "h3",
    null,
    "Score breakdown — one row per skill" +
      (songMode ? " (song-agnostic run, not the per-song score)" : "")
  ));
  ccol.append(
    el(
      "p",
      "lede sm",
      "Every skill this team brings, biggest contributor first. Each row says " +
        "what the skill does in words, with the data's own term beside it in " +
        "small type so any number here can be traced back to the file it came " +
        "from. “paid nothing” means the condition above failed; “not scorable” " +
        "means a field it needs is untranscribed, which is a data gap and not a " +
        "zero. Weighted rate is that skill's share of the score, not a " +
        "percentage of anything."
    )
  );
  if (contributions && contributions.length) {
    ccol.append(contribsTable(contributions, cards));
  } else {
    ccol.append(el("p", "note", payload._developed
      ? "per-skill attribution is computed by the stage-1 solve; this roster was " +
        "re-composed or re-ordered by the develop, so none is shown rather than another team's"
      : "no attribution in this payload"));
  }
  cols.append(ccol);
  body.append(cols);

  // The term panel, on every rank and folded — it is the "why is this number
  // what it is" view, and a reader comparing two teams needs it on both.
  const terms = termsFor(payload, team.rank, team);
  if (terms) {
    const wrap = el("details", "explain terms-fold");
    wrap.append(el(
      "summary",
      null,
      songMode
        ? "Score terms — how the SONG-AGNOSTIC score is built (not the per-song number)"
        : "Score terms — how this number is built"
    ));
    wrap.append(termsPanel(terms));
    body.append(wrap);
  }

  const cfold = certFold(team.certificate);
  if (cfold) body.append(cfold);

  const lines = explainFor(payload, team.rank, team);
  if (lines && lines.length) {
    const wrap = el("details", "explain");
    wrap.append(
      el("summary", null,
        `The raw working, line by line (CLI: --explain ${team.rank})`)
    );
    wrap.append(el("pre", "mono", lines.join("\n")));
    body.append(wrap);
  }

  block.append(body);
  return block;
}

// --- the whole ranking -----------------------------------------------------
export function renderRanking(container, payload) {
  container.replaceChildren();
  // The generation ribbon tints, shipped once per payload rather than per card.
  setGenColors(payload.gen_colors, payload.gen_labels);
  const teams = payload.teams || [];
  if (!teams.length) return null;
  const metric = metricOf(payload);
  const best = teams[0];
  const spread = metric.read(best) - metric.read(teams[teams.length - 1]);

  // The sentence for the reader who scrolled straight to the table: which
  // quantity every row below is, in words, at the top of the ranking itself.
  // Song mode only — the agnostic run is already described by the page's
  // static copy, and `replaceChildren()` above means an agnostic re-run
  // cannot inherit a stale per-song claim.
  if (metric.field !== "score") {
    const head = el("p", "banner");
    head.id = "rankquantity";
    const song = payload.song || {};
    head.textContent =
      `Ranked by ${metric.label} — the expected score for ` +
      `${song.title || song.music_id || "the chosen chart"} ` +
      `(${song.difficulty || "EXPERT"}), cue by cue on that chart. ` +
      `Stage 1 searched song-agnostically` +
      (song.pool != null ? ` and handed over ${song.pool} teams` : "") +
      `; those teams were re-scored on the chart and re-ordered. The ` +
      `song-agnostic score stays inside each row, labelled, for reference — ` +
      `a different scale, never subtracted from this one.`;
    container.append(head);
  }

  teams.forEach((team, index) => {
    container.append(
      rankBlock(payload, team, best, index ? teams[index - 1] : null, spread, metric)
    );
  });
  return { count: teams.length, spread, best, field: metric.field, label: metric.label };
}

// The one sentence that keeps every delta above honest. It is built from
// the ranking's own numbers; the 21.4 % it compares against is observed and
// fixed (four AUTO runs of one song, identical team).
// The noise band, drawn to scale.
//
// This is the one graphic on the page, and it exists because the sentence
// below it is the most important thing this tool has to say and the least
// likely to be read: the ENTIRE spread between the ranked teams is a fraction
// of the run-to-run variance of a single play. Written out it is two decimals
// buried in a paragraph. Drawn, it is one look.
//
// Both quantities are already in the payload — the ranking's own relative
// spread, and RNG_SPREAD, which is observed (four AUTO runs of one song with
// an identical team, 578,424 / 617,974 / 650,951 / 702,485).
// Nothing here is estimated and nothing is scaled to look better than it is:
// the marker's width is `relSpread / RNG_SPREAD` of the band, full stop.
// ⚠ Both take the metric the TABLE is sorted by. A band computed over `score`
// under a table ordered by `song_score` would describe a different ranking
// than the one on screen — the exact drift `sorted_by` exists to prevent. The
// default keeps a caller that passes no metric on the agnostic quantity.
export function separabilityBand(teams, metric = SCORE_METRIC) {
  if (!teams || teams.length < 2) return null;
  const best = metric.read(teams[0]);
  const spread = best - metric.read(teams[teams.length - 1]);
  const relSpread = best ? spread / best : 0;
  const frac = RNG_SPREAD ? relSpread / RNG_SPREAD : 0;

  const wrap = el("figure", "nband");
  const head = el("figcaption", "nband-cap");
  head.append(
    el("b", "nband-n", `${(relSpread * 100).toFixed(4)} %`),
    el("span", null, " — the whole ranking's spread, against the "),
    el("b", "nband-r", `${(RNG_SPREAD * 100).toFixed(1)} %`),
    el("span", null, " the active-skill RNG is worth on one song")
  );

  const track = el("div", "nband-track");
  const fill = el("i", "nband-fill");
  // A spread of 0.83 % against a 21.4 % band is 3.9 % of the track. Below a
  // hairline it stops being visible at all, so it is floored at 2px — the
  // number beside it carries the precision, and a mark you cannot see would
  // understate rather than inform.
  fill.style.width = `max(2px, ${(Math.min(1, frac) * 100).toFixed(3)}%)`;
  track.append(fill);
  if (frac > 1) track.classList.add("over");

  const scale = el("div", "nband-scale");
  scale.append(el("span", null, "0 %"),
               el("span", null, `${(RNG_SPREAD * 100).toFixed(1)} %`));

  wrap.append(head, track, scale);
  wrap.title =
    "The bar is the measured run-to-run variance of one song played with an " +
    "identical team; the filled mark is how much of it this entire ranking " +
    "spans. Both are drawn to the same scale.";
  return wrap;
}

// One concise sentence; the citation and the four-run numbers live in the
// tooltip the caller sets on the element. The band graphic above carries the
// visual, so the prose is the takeaway, not the evidence dump.
export function separabilityNote(teams, metric = SCORE_METRIC) {
  if (!teams || teams.length < 2) return "";
  const best = metric.read(teams[0]);
  const gap = best - metric.read(teams[1]);
  const spread = best - metric.read(teams[teams.length - 1]);
  const rel = best ? gap / best : 0;
  const relSpread = best ? spread / best : 0;
  const inside = relSpread < RNG_SPREAD;
  return (
    `#1 beats #2 by ${num(gap, 1)} (${(rel * 100).toFixed(2)} %); the ranking spans ` +
    `${(relSpread * 100).toFixed(2)} % — ` +
    (inside
      ? `inside the 21.4 % the active-skill RNG is worth on one song, so no single ` +
        `clear separates these teams.`
      : `part of it exceeds the 21.4 % active-skill RNG band, so it is separable in play.`)
  );
}

// The evidence, for the tooltip — kept out of the always-on text.
export function separabilityDetail() {
  return "Four AUTO runs of one song with an identical " +
    "team scored 578,424 / 617,974 / 650,951 / 702,485 — the active-skill RNG is " +
    "worth 21.4 %. A ranking that fits inside that band orders the model, not an " +
    "evening's play.";
}
