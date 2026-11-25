# Map the extracted dumps/cards_enriched.json onto the domain's cards.json record shape
# (src/hbuilder/domain/schema.py). Writes TWO files, never data/cards.json:
#
#   dumps/cards_review.json        LOSSLESS. Every extracted field, domain-named where a
#                                  domain name exists, plus everything the schema cannot yet
#                                  hold. This is the file to re-import from once the schema
#                                  grows; nothing is thrown away here.
#   dumps/cards_import_draft.json  SCHEMA-VALID proposal document ({"schema_version", "cards"},
#                                  the same shape as data/cards.json). Passes validate_card with
#                                  zero errors. Whatever the current schema cannot express is
#                                  dropped HERE and only here, and every drop is counted and
#                                  printed as a loss report -- never silently.
#
# Identity, stats, durations, cooldowns, probability tiers, magnitudes and gates are all
# verified against an independent 50-card hand transcription.
#
# Four shapes the schema represents rather than drops:
#   1. base + conditional magnitude in one slot -- SkillSlot.override:
#      the base magnitude stays `magnitude`, the gated value becomes `override.magnitude`.
#   2. two effect families in one slot         -- SkillSlot.rider:
#      the second, different-family effect becomes `rider.family`/`.effect_type`/`.magnitude`.
#   3. all_parameter_up ("All Stats UP")       -- maps to `("stat_up", "all_stats")`,
#      the fourth stat_up effect_type.
#   4. effect target_max ("... to 2 Cute")     -- SkillSlot.target, the `(kind, value,
#      count)` clause every family carries.
#
# `all` is never a fallback for a PASSIVE target: no real card's PASSIVE resolves to `all`
# (they are self, type-max-2/3 or generation-max-2), so defaulting a missing one to `all` would
# inflate a subset effect into a team-wide one. `_slot` asserts it. ACTIVE and SPECIAL carry no
# target clause at all, and `all` there encodes that absence (a genuine team-wide default).
#
# Usage: python import_to_cards.py [--diff]
import argparse
import difflib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))  # this checkout's hbuilder first
from hbuilder import paths  # noqa: E402

DUMPS = str(paths.DUMPS)
from hbuilder.domain.schema import SCHEMA_VERSION, validate_card  # noqa: E402

CARDS_JSON = str(paths.DATA / "cards.json")

# VERIFIED 50/50 against the SSR transcription's `type` column.
TYPE_BY_ATTRIBUTE = {1: "Cute", 2: "Pure", 3: "Happy"}
# schema.GENERATIONS names, keyed by the Character master's English generation label.
GENERATIONS_BY_MASTER = {
    "hololive 0th Generation": ["0期生"],
    "hololive 1st Generation": ["1期生"],
    "hololive 2nd Generation": ["2期生"],
    "hololive Gamers": ["ゲーマーズ"],
    "hololive 1st Generation / Gamers": ["1期生", "ゲーマーズ"],
    "hololive 3rd Generation": ["3期生"],
    "hololive 4th Generation": ["4期生"],
    "hololive 5th Generation": ["5期生"],
    "hololive holoX": ["holoX"],
    "hololive ID 1st Generation": ["ID1"],
    "hololive ID 2nd Generation": ["ID2"],
    "hololive ID 3rd Generation": ["ID3"],
    "hololive EN Myth": ["Myth"],
    "hololive EN Promise": ["Promise"],
    "hololive EN Advent": ["Advent"],
    "DEV_IS ReGLOSS": ["ReGLOSS"],
}
GENERATION_BY_GROUP = {
    "grp-gen_0": "0期生",
    "grp-gen_1": "1期生",
    "grp-gen_2": "2期生",
    "grp-gamers": "ゲーマーズ",
    "grp-gen_3": "3期生",
    "grp-gen_4": "4期生",
    "grp-gen_5": "5期生",
    "grp-holox": "holoX",
    "grp-indonesia-gen_1": "ID1",
    "grp-indonesia-gen_2": "ID2",
    "grp-indonesia-gen_3": "ID3",
    "grp-myth": "Myth",
    "grp-promise": "Promise",
    "grp-advent": "Advent",
    "grp-regloss": "ReGLOSS",
}
RARITY_BY_CODE = {"nrml": "3★", "cmmn": "4★", "uniq": "5★"}

# game effect -> (domain family, effect_type). The two "Score Support Effect" spellings are
# the same in-game label seen on different slots; `score_up` is the "Score UP" family.
# all_parameter_up ("All Stats UP") is the fourth stat_up effect_type, not a family the
# schema has no room for.
FAMILY_BY_EFFECT = {
    "score_up": ("conditional_score_up", "score"),
    "score_up_effect_up": ("score_support", "score"),
    "live_active_skill_effect_up": ("score_support", "score"),
    "live_active_skill_activation_probability_up": ("activation_rate_up", "activation_rate"),
    "performance_up": ("stat_up", "performance"),
    "technique_up": ("stat_up", "technique"),
    "sense_up": ("stat_up", "sense"),
    "life_recovery": ("life_recovery", "life"),
    "judgement_enhance": ("judgment_enhancement", "judgment"),
    "all_parameter_up": ("stat_up", "all_stats"),
}


def _condition(gate):
    """extracted gate -> domain condition. None means affirmatively ungated."""
    if gate is None:
        return {"kind": "ungated"}
    if gate["kind"] == "run_state":
        cond = {"kind": "run_state", "target": gate["target"], "count": gate["count"]}
        # Carry the comparison direction only when it is the non-default "lte"
        # (LIFE_LTE); a "gte" gate stays operator-less so existing cards.json
        # rows are byte-for-byte unchanged and the domain default applies.
        if gate.get("operator", "gte") != "gte":
            cond["operator"] = gate["operator"]
        return cond
    if gate["kind"] == "type":
        return {
            "kind": "type",
            "target": TYPE_BY_ATTRIBUTE[int(gate["target"].rsplit("_", 1)[1])],
            "count": gate["count"],
        }
    if gate["kind"] == "generation":
        return {"kind": "generation", "target": GENERATION_BY_GROUP[gate["target"]], "count": gate["count"]}
    raise ValueError(f"unmapped gate {gate!r}")


def _target(target, loss, card_id, slot_kind):
    """extracted target ({"kind", "target", "max"} or None) -> domain target
    ({"kind", "value", "count"} or JSON null).

    `target is None` is the extraction's OWN "no clause at all" state -- attested on every one
    of the 167 ACTIVE/SPECIAL slots. This helper only translates a PRESENT clause; a
    caller on ACTIVE/SPECIAL substitutes the authored `{"kind": "all"}` default itself (see
    `_slot`) rather than asking this helper to invent one.

    An extracted `kind == "unknown"` (the extractor's own regex failed to classify the target
    string) is never guessed into a plausible kind: it emits JSON null -- the
    untranscribed state, tainting via the existing lower-bound `>=` machinery -- and is counted
    in the `target_unmapped` diagnostic bucket so it stays visible."""
    if target is None:
        return None
    kind = target["kind"]
    if kind == "self":
        return {"kind": "self"}
    if kind == "all":
        return {"kind": "all"}
    if kind == "type":
        return {
            "kind": "type",
            "value": TYPE_BY_ATTRIBUTE[int(target["target"].rsplit("_", 1)[1])],
            "count": target["max"],
        }
    if kind == "generation":
        return {
            "kind": "generation",
            "value": GENERATION_BY_GROUP[target["target"]],
            "count": target["max"],
        }
    loss["target_unmapped"].append((card_id, slot_kind, target.get("target")))
    return None


def _rider_payload(cond_effect, loss, card_id, slot_kind):
    """A slot's conditional effect names a family DIFFERENT from the principal -- represent
    it as a rider rather than dropping it. `magnitude` prefers the extraction's percent-scaled `magnitude_permil`; the two
    real rider families that carry none (`life_recovery`'s fixed 400 LIFE, `judgement_enhance`'s
    valueless 0 flag -- neither ever occurs as a PRINCIPAL effect anywhere in the 167 cards)
    fall back to the raw `value` instead. This never matters for scoring: a rider is
    represented but deliberately never scored, so the differing units between a percent
    rate and a raw game value cause no arithmetic error -- the lossless `cards_review.json`
    is where the raw shape lives if it is ever needed."""
    rider_family, rider_effect_type = FAMILY_BY_EFFECT[cond_effect["effect"]]
    magnitude = (
        cond_effect["magnitude_permil"] / 10
        if cond_effect["magnitude_permil"] is not None
        else float(cond_effect["value"])
    )
    return {
        "kind": "rider",
        "family": rider_family,
        "effect_type": rider_effect_type,
        "magnitude": magnitude,
        "condition": _condition(cond_effect.get("gate")),
        "target": _target(cond_effect.get("target"), loss, card_id, slot_kind),
    }


def _stat_first(principal, second):
    """Whether a slot's two effects swap so the stat effect leads: the second is a stat
    effect and the first is not."""
    return FAMILY_BY_EFFECT[second["effect"]][0] == "stat_up" and FAMILY_BY_EFFECT[principal["effect"]][0] != "stat_up"


def _costume_slot(costume, loss, card_id):
    """The COSTUME slot = the card's outfit / leader skill.

    **Every card has one.** `Card.f23` is blank on every 3★ card because a 3★ wears the
    character's `Default` costume rather than a card-specific one, so resolving the outfit
    through it alone would wrongly report 3★ cards as having none. The Costume row and its
    LiveLeaderSkill exist regardless, and the 3★ one is worth +15.0 % all stats, ungated --
    identical on every 3★. See `build_cards_dataset._costume_by_card`.

    Every costume target is `all` in the source (unlike ACTIVE/SPECIAL, this IS read
    from the source, not an authored default), so `_target` is called on it directly, never
    defaulted."""
    if not costume or not costume.get("skill"):
        return None
    sk = costume["skill"]
    principal, gate = sk["effect"], sk["gate"]
    rider = {"kind": "none"}
    if sk.get("conditional") and sk["conditional"].get("effect"):
        second, second_gate = sk["conditional"]["effect"], sk["conditional"]["gate"]
        if _stat_first(principal, second) and second_gate == gate:
            # the catalogue's order for a two-effect outfit: the stat effect is the slot's
            # own, the other its rider (only a score-support rider is scored on an outfit)
            principal, second = second, principal
        rider = _rider_payload(dict(second, gate=second_gate), loss, card_id, "COSTUME")
    family, effect_type = FAMILY_BY_EFFECT[principal["effect"]]
    return {
        "family": family,
        "effect_type": effect_type,
        "duration": None,
        "interval": None,
        "probability_tier": None,
        "magnitude": principal["magnitude_permil"] / 10,
        "condition": _condition(gate),
        "target": _target(principal.get("target"), loss, card_id, "COSTUME"),
        "override": {"kind": "none"},
        "rider": rider,
    }


def _slot(kind, sk, loss, card_id):
    """One schema-valid skill slot, or None when the slot itself is absent.

    Every field NOT_APPLICABLE to the slot kind is present and null, as the strict-explicit
    encoding requires -- absent keys are a validation error, not a shorthand."""
    if not sk:
        return None
    principal, conditional = sk["effect"], sk.get("conditional")
    override, rider = {"kind": "none"}, {"kind": "none"}
    gate = sk.get("gate") if kind in ("ACTIVE", "PASSIVE") else None
    additional = sk.get("additional") if kind == "PASSIVE" else None
    if additional and additional.get("effect"):
        # a PASSIVE's second effect (f5/f6) with its own gate and target: the stat effect
        # leads (the recipients rule a stat passive uses), the other is its rider
        second, second_gate = additional["effect"], additional["gate"]
        if _stat_first(principal, second):
            principal, second, gate, second_gate = second, principal, second_gate, gate
        rider = _rider_payload(dict(second, gate=second_gate), loss, card_id, kind)

    if conditional and conditional["effect"]:
        cond_effect = conditional["effect"]
        if cond_effect["effect"] == principal["effect"]:
            # Override: the base magnitude REPLACES nothing -- it stays the
            # slot's own `magnitude`, the slot's own gate stays `condition`, and the gated
            # value becomes an explicit override, never collapsed into the principal.
            #
            # `magnitude_permil` is None for a "valueless" effect form (`judgement_enhance-0`,
            # `life_recovery-400`; see `_rider_payload`), which no card has as a PRINCIPAL
            # effect. The same fallback as `_rider_payload` keeps a future one from raising
            # a bare `TypeError`.
            override = {
                "kind": "override",
                "magnitude": (
                    cond_effect["magnitude_permil"] / 10
                    if cond_effect["magnitude_permil"] is not None
                    else float(cond_effect["value"])
                ),
                "condition": _condition(conditional["gate"]),
            }
        else:
            # Rider: a second, DIFFERENT family in the same slot (SPECIAL
            # carries activation-rate / judgement / life-recovery riders).
            rider = _rider_payload(dict(cond_effect, gate=conditional["gate"]), loss, card_id, kind)

    family, effect_type = FAMILY_BY_EFFECT[principal["effect"]]  # every effect maps; never None

    raw_target = principal.get("target")
    if kind == "PASSIVE":
        target = _target(raw_target, loss, card_id, kind)
        # `all` is never a fallback for a PASSIVE -- enforced, not assumed. A real
        # PASSIVE target never resolves to `all` across all 167 cards; if one ever did, writing
        # it silently would fabricate the single value this family of effect never exhibits.
        if isinstance(target, dict) and target.get("kind") == "all":
            raise ValueError(
                f"D-32 violation: card {card_id!r} PASSIVE target resolved to 'all', which "
                f"never occurs in the real 167-card extraction -- refusing to fabricate the "
                f"one value the data never exhibits"
            )
    else:
        # ACTIVE/SPECIAL carry no target clause in the extraction at all (raw_target is
        # always None) -- `all` here is this importer's encoding of that absence, a genuine team-wide
        # default, never something read from the source.
        target = {"kind": "all"} if raw_target is None else _target(raw_target, loss, card_id, kind)

    # ACTIVE (f3) and PASSIVE (f3) carry the principal effect's gate; SPECIAL has none
    condition = _condition(gate) if gate else {"kind": "ungated"}

    slot = {
        "family": family,
        "effect_type": effect_type,
        "duration": (sk["duration_ms"] / 1000) if kind in ("ACTIVE", "SPECIAL") else None,
        "interval": (sk["cooldown_ms"] / 1000) if kind == "ACTIVE" else None,
        "probability_tier": sk["probability_tier"] if kind == "ACTIVE" else None,
        # on-disk magnitude is percentage points (scoring._as_rate divides by 100)
        "magnitude": principal["magnitude_permil"] / 10
        if principal["magnitude_permil"] is not None
        else None,
        "condition": condition,
        "target": target,
        "override": override,
        "rider": rider,
    }
    return slot


# A gate in the game's own skill text: "With 2 or more Cute Type Members", "When LIFE is 600
# or higher", "With a Combo of 40 or more". "GOOD or higher becomes PERFECT" is the judgement
# effect, not a gate.
_GATE_PHRASE = re.compile(r"\b(?:or more|or higher|or lower|or less|or above|or below)\b", re.I)
_NOT_A_GATE = re.compile(r"\bGOOD or higher\b")


def _gates_in(slot):
    """The gated conditions a translated slot carries: its own, its override's, its rider's."""
    if not slot:
        return 0
    parts = [slot.get("condition")]
    if (slot.get("override") or {}).get("kind") == "override":
        parts.append(slot["override"].get("condition"))
    if (slot.get("rider") or {}).get("kind") == "rider":
        parts.append(slot["rider"].get("condition"))
    return sum(1 for c in parts if (c or {}).get("kind", "ungated") != "ungated")


def untranslated(card, record):
    """Why `record` (the draft of the enriched `card`) is not a faithful translation, or [].

    Two checks that read the game, not this translator: a populated level-row field no
    resolver reads (`skills.unread`), and the game's own English skill text naming a
    different number of gates than the slot carries. A card with either is reported and
    never added to the catalogue (`card_append`)."""
    reasons = []
    for kind, levels in (card.get("skills_by_level") or {}).items():
        for level, sk in (levels or {}).items():
            if sk and sk.get("unread"):
                reasons.append(f"{kind} level {level}: unread master fields {sk['unread']}")
    costume = (card.get("costume") or {}).get("skill") or {}
    if costume.get("unread"):
        reasons.append(f"COSTUME: unread master fields {costume['unread']}")
    texts = card.get("descriptions") or {}
    slots = {**(record.get("skills") or {}), "COSTUME": record.get("costume_skill")}
    for kind, slot in slots.items():
        text = texts.get(kind)
        if not text or slot is None:
            continue
        said = len(_GATE_PHRASE.findall(_NOT_A_GATE.sub("", text)))
        if said != _gates_in(slot):
            reasons.append(f"{kind}: the game's text names {said} gate(s), the slot carries {_gates_in(slot)}")
    return reasons


def to_records(cards):
    loss = {k: [] for k in ("costume_name_unknown", "target_unmapped")}
    valid, review = [], []
    for c in cards:
        sm = c["stats_max"]
        costume = c.get("costume")
        # title and costume name are the game's own English strings (LangCard_Eng /
        # LangCostume_Eng), not a transcription. 3★ cards ship no card-specific costume, hence no name.
        title = c.get("title") or ""
        costume_name = (costume or {}).get("name") or ""
        if not costume_name:
            loss["costume_name_unknown"].append(c["id"])
        record = {
            "id": c["id"],
            "member": c["member"],
            "generation": GENERATIONS_BY_MASTER[c["generation"]],
            "type": TYPE_BY_ATTRIBUTE[c["attribute"]],
            "rarity": RARITY_BY_CODE[c["rarity"]],
            "title": title,
            "costume_name": costume_name,
            "stats": {k: sm[k] for k in ("performance", "technique", "sense")} if sm else None,
            "skills": {k: _slot(k, c["skills"][k], loss, c["id"]) for k in ("SPECIAL", "ACTIVE", "PASSIVE")},
            "costume_skill": _costume_slot(costume, loss, c["id"]),
        }
        valid.append(record)
        review.append(
            {
                **record,
                "_extracted": {
                    "stats_current_grpc": c["stats"],
                    "stat_ratio_permil": c["stat_ratio_permil"],
                    "stats_max_at": sm["at"] if sm else None,
                    "name_key": c["name_key"],
                    "descriptions": c.get("descriptions"),
                    "chr_id": c["chr_id"],
                    "rarity_code": c["rarity"],
                    "attribute": c["attribute"],
                    "costume": c.get("costume"),
                    "skills": c["skills"],
                },
            }
        )
    return valid, review, loss


SKILL_LADDER_SCHEMA_VERSION = 1


def to_skill_ladder(cards):
    """`{card_id: {kind: {level: slot}}}` in the DOMAIN's slot vocabulary.

    Same `_slot` mapper as the catalogue draft, so level 2 of this ladder is
    byte-identical to `data/cards.json`'s own skill block -- which is the point.
    That identity is the self-check (`tests/test_skill_ladder.py`): if the two
    ever disagree, one is wrong and the tests say so before any score is
    computed.

    `costume_skill` is deliberately absent. `LiveLeaderSkill` has one row per
    card and no level column at all -- bloom buys nothing on the outfit, so a
    ladder rung for it would be an invented mechanic.
    """
    loss = {k: [] for k in ("costume_name_unknown", "target_unmapped")}
    ladder, missing = {}, []
    for c in cards:
        by_level = c.get("skills_by_level")
        if not by_level:
            missing.append(c["id"])
            continue
        rungs = {}
        for kind in ("SPECIAL", "ACTIVE", "PASSIVE"):
            levels = by_level.get(kind) or {}
            slots = {lv: _slot(kind, sk, loss, c["id"]) for lv, sk in levels.items()}
            slots = {lv: s for lv, s in slots.items() if s is not None}
            if slots:
                rungs[kind] = slots
        ladder[c["id"]] = rungs
    return {"schema_version": SKILL_LADDER_SCHEMA_VERSION, "cards": ladder}, missing


def _check_ladder_against_catalogue(ladder_doc):
    """Level 2 of the ladder MUST reproduce `data/cards.json`'s skills exactly.

    Reads `data/cards.json`, never writes it. Returns the disagreements, which
    the caller prints -- a silent pass here would be the pipeline reporting its
    own success, which proves nothing."""
    if not os.path.exists(CARDS_JSON):
        return None
    with open(CARDS_JSON, encoding="utf-8") as fh:
        catalogue = json.load(fh)["cards"]
    bad = []
    for record in catalogue:
        rungs = ladder_doc["cards"].get(record["id"])
        if rungs is None:
            bad.append(f"{record['id']}: absent from the ladder")
            continue
        for kind, slot in record["skills"].items():
            exported = (rungs.get(kind) or {}).get("2")
            if slot != exported:
                bad.append(f"{record['id']} {kind}: level 2 differs from data/cards.json")
    return bad


def _write_merge_diff(draft_doc):
    """The merge into data/cards.json is PROPOSED as a reviewable unified diff,
    never applied. Reads
    data/cards.json (if present); writes only dumps/cards_merge_proposal.diff."""
    if os.path.exists(CARDS_JSON):
        with open(CARDS_JSON, encoding="utf-8") as fh:
            existing_doc = json.load(fh)
    else:
        existing_doc = {"schema_version": SCHEMA_VERSION, "cards": []}
    existing_text = json.dumps(existing_doc, indent=2, ensure_ascii=False, sort_keys=True).splitlines(
        keepends=True
    )
    draft_text = json.dumps(draft_doc, indent=2, ensure_ascii=False, sort_keys=True).splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            existing_text,
            draft_text,
            fromfile="data/cards.json",
            tofile="dumps/cards_import_draft.json",
        )
    )
    diff_path = os.path.join(DUMPS, "cards_merge_proposal.diff")
    with open(diff_path, "w", encoding="utf-8") as fh:
        fh.writelines(diff_lines)
    print(
        f"\n{len(diff_lines)} diff lines -> dumps/cards_merge_proposal.diff "
        f"(reviewable proposal; data/cards.json was read, never written)"
    )


def main():
    # Windows' default cp1252 console encoding raises on this file's own
    # non-ASCII characters (the ★ rarity glyph) -- reconfigure stdout to
    # UTF-8, as the other refresh tools do, rather than let this tool's
    # loss report crash on Windows.
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Map dumps/cards_enriched.json onto the domain's cards.json schema. "
        "Writes only to dumps/ -- never data/cards.json."
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Also write dumps/cards_merge_proposal.diff, a unified diff of data/cards.json "
        "against the schema-valid draft (reads data/cards.json, never writes it).",
    )
    args = parser.parse_args()

    with open(os.path.join(DUMPS, "cards_enriched.json"), encoding="utf-8") as fh:
        cards = json.load(fh)
    valid, review, loss = to_records(cards)
    draft_doc = {"schema_version": SCHEMA_VERSION, "cards": valid}
    # the cards this translator cannot render faithfully: `card_append` adds none of them
    by_id = {c["id"]: c for c in cards}
    problems = {r["id"]: why for r in valid if (why := untranslated(by_id[r["id"]], r))}
    with open(os.path.join(DUMPS, "cards_untranslated.json"), "w", encoding="utf-8") as fh:
        json.dump(problems, fh, indent=2, ensure_ascii=False, sort_keys=True)
    for card_id, why in sorted(problems.items()):
        print(f"  ! {card_id}: " + "; ".join(why))

    errors = [e for i, r in enumerate(valid) for e in validate_card(r, i)]
    with open(os.path.join(DUMPS, "cards_import_draft.json"), "w", encoding="utf-8") as fh:
        json.dump(draft_doc, fh, indent=2, ensure_ascii=False)
    with open(os.path.join(DUMPS, "cards_review.json"), "w", encoding="utf-8") as fh:
        json.dump(review, fh, indent=2, ensure_ascii=False)

    print(f"{len(valid)} cards -> dumps/cards_import_draft.json (schema-valid proposal)")
    print(f"{len(review)} cards -> dumps/cards_review.json (lossless)")
    print(f"validate_card errors: {len(errors)}")
    for e in errors[:20]:
        print("   ", e)
    print("\nLOSS REPORT (the four gaps phase 03.1 closed -- kept as the proof; each is now")
    print("            structurally represented rather than measured, so this always reads 0):")
    print("  gap 1  base magnitude dropped, gated value kept : 0 slots (closed: override, plan 05)")
    print("  gap 2  second family in one slot dropped        : 0 slots (closed: rider, plan 05/06)")
    print("  gap 3  all_parameter_up slot dropped entirely   : 0 slots (closed: all_stats, plan 04)")
    print("  gap 4  effect target_max dropped                : 0 slots (closed: target, plan 01/03)")
    print(
        f"  --     target unparseable/unmapped              : "
        f"{len(loss['target_unmapped'])} slots (D-32/T-03.1-25 diagnostic)"
    )
    print(
        f"  --     costume_name empty                       : "
        f"{len(loss['costume_name_unknown'])} cards (expected 0 -- every card has an outfit)"
    )
    with_costume = sum(1 for r in valid if r["costume_skill"])
    print(f"  --     costume_skill populated                  : {with_costume}/{len(valid)} cards")

    # The bloom down-level ladder. NOT dumps/skill_levels.json -- that path
    # may hold an unrelated, stale file with no generator in this repository.
    ladder_doc, ladder_missing = to_skill_ladder(cards)
    with open(os.path.join(DUMPS, "skill_ladder.json"), "w", encoding="utf-8") as fh:
        json.dump(ladder_doc, fh, indent=2, ensure_ascii=False, sort_keys=True)
    rungs = sum(len(k) for c in ladder_doc["cards"].values() for k in c.values())
    print(
        f"\n{len(ladder_doc['cards'])} cards, {rungs} slot-levels -> "
        f"dumps/skill_ladder.json (bloom down-level)"
    )
    if ladder_missing:
        print(
            f"  ⚠ {len(ladder_missing)} cards carry no skills_by_level -- re-run "
            f"build_cards_dataset.py: {ladder_missing[:3]}"
        )
        errors = errors + ladder_missing
    disagreements = _check_ladder_against_catalogue(ladder_doc)
    if disagreements is None:
        print("  self-check SKIPPED: no data/cards.json to check level 2 against")
    else:
        print(f"  self-check: level 2 vs data/cards.json -- {len(disagreements)} disagreements (expected 0)")
        for d in disagreements[:10]:
            print("   ", d)
        errors = errors + disagreements

    if args.diff:
        _write_merge_diff(draft_doc)

    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
