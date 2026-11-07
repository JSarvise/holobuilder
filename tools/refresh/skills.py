# Resolve hololive Dreams card skills from the decrypted skill masters into faithful,
# domain-mappable records. Pairs with master_db.py / build_cards_dataset.py.
#
# Chain: Card master -> passive/active/special skill id -> Live{Active,Passive,Special}SkillLevel
# (per-level BLOB; skills cap at level 2).
#
# Level BLOB fields (verified against a hand transcription of 50 SSR cards):
#   ACTIVE : f4 principal effect, f5 GATE for the conditional effect, f6 conditional effect,
#            f7 cooldown(ms), f8 activation chance(permil), f10 duration(ms)
#   PASSIVE: f3 GATE, f4 effect(+target)
#   SPECIAL: f3 principal effect, f4 GATE for the conditional effect, f5 conditional effect,
#            f6 duration(ms)
#
# Note f7 is the cooldown and f10 the duration (not f7 duration / f8 interval). Cross-check,
# Natsuiro Matsuri 5*: f10=8000 / f4=score_up 600 / f7=23000 / f8=550
# vs the in-game text "8s, Score UP 60%. Every 23s, High probability." (50/50 cards agree on
# duration, cooldown, chance tier, magnitude and conditional magnitude).
#
# TWO DISTINCT NUMBERS, easy to conflate:
#   - the GATE ("with 2+ Cute in the deck") lives in the `live_skill_trigger-*` field and
#     carries its own threshold. This is the domain's `condition`.
#   - the effect TARGET ("... to 3 Cute members") lives in the effect's own
#     `-live_skill_effect_target-<who>-<max>` suffix. `max` is how many members receive the
#     buff, NOT a gate threshold. The domain has no field for it yet; it is carried through
#     as `target.max` so no information is lost.
#
# Attested trigger kinds: life_gte-N, combo_gte-N, deck_card_attribute-attribute_N-M,
# deck_card_character_grouping-grp-X-M. Attested effects: score_up, score_up_effect_up,
# performance/technique/sense_up, all_parameter_up, live_active_skill_effect_up,
# live_active_skill_activation_probability_up (all `_permil_up-<permil>`), plus the two
# valueless forms judgement_enhance-0 and life_recovery-400.
import re

_EFFECT_RE = re.compile(
    r"skill_effect-(?P<body>.+?)"
    r"(?:-live_skill_effect_target-(?P<target>.+))?$"
)
_TRIGGER_RE = re.compile(r"live_skill_trigger-(?P<body>.+)$")

# f8 (activation chance, permil) -> the domain's PROBABILITY_TIERS string.
# 460 == "Medium" and 550 == "High" are attested directly (50-card SSR transcription);
# 370 == "Low" is by elimination -- it is the only other value, it occurs only on 3* cards,
# and the SSR set (4*/5*) contains no "Low".
PROBABILITY_TIER_BY_PERMIL = {370: "Low", 460: "Medium", 550: "High"}


def parse_target(s):
    """'attribute-attribute_2-3'            -> {'kind':'type','target':'attribute_2','max':3}
       'character_grouping-grp-gen_1-2'     -> {'kind':'generation','target':'grp-gen_1','max':2}
       'self'                               -> {'kind':'self','target':None,'max':1}
       'all'                                -> {'kind':'all','target':None,'max':None}"""
    if not s:
        return None
    if s == "self":
        return {"kind": "self", "target": None, "max": 1}
    if s == "all":
        return {"kind": "all", "target": None, "max": None}
    m = re.match(r"^(?P<what>attribute|character_grouping)-(?P<who>.+?)(?:-(?P<max>\d+))?$", s)
    if not m:
        return {"kind": "unknown", "target": s, "max": None}
    kind = "type" if m.group("what") == "attribute" else "generation"
    return {"kind": kind, "target": m.group("who"),
            "max": int(m.group("max")) if m.group("max") else None}


def parse_effect(s):
    """'live_passive_skill_effect-sense_up_permil_up-320-live_skill_effect_target-...-2' ->
       {'effect':'sense_up','magnitude_permil':320,'target':{...},'raw':...}

    Valueless forms (judgement_enhance-0, life_recovery-400) keep their trailing number in
    `value` and leave `magnitude_permil` None -- 400 there is LIFE points, not per-mille."""
    if not s:
        return None
    m = _EFFECT_RE.search(s)
    if not m:
        return None
    body = m.group("body")
    if "_permil_up-" in body:
        effect, mag = body.rsplit("_permil_up-", 1)
        out = {"effect": effect, "magnitude_permil": int(mag), "value": None}
    else:
        effect, value = body.rsplit("-", 1)
        out = {"effect": effect, "magnitude_permil": None,
               "value": int(value) if value.isdigit() else value}
    out["target"] = parse_target(m.group("target"))
    out["raw"] = s
    return out


def parse_gate(s):
    """'live_skill_trigger-life_gte-600'                        -> run_state/life/600
       'live_skill_trigger-combo_gte-40'                        -> run_state/combo/40
       'live_skill_trigger-deck_card_attribute-attribute_2-2'   -> type/attribute_2/2
       'live_skill_trigger-deck_card_character_grouping-grp-gen_0-2' -> generation/grp-gen_0/2

    Returns a domain-shaped condition (schema.CONDITION_KINDS) or None when the skill has no
    gate at all -- which is the affirmative "ungated", not "unknown"."""
    if not s:
        return None
    m = _TRIGGER_RE.search(s)
    if not m:
        return {"kind": "unknown", "target": s, "count": None, "raw": s}
    body = m.group("body")
    rs = re.match(r"^(?P<dim>life|combo)_(?P<op>gte|lte)-(?P<n>\d+)$", body)
    if rs:
        # LIFE_LTE=11 (comeback/desperation, thresholds 500/700/950) fires at
        # or BELOW its threshold — the inverse of the *_GTE triggers. The
        # operator must reach the domain, or the resolver's default `>=` would
        # invert it. combo has
        # no LTE trigger in the master today, but a combo_lte would parse
        # harmlessly the same way.
        return {"kind": "run_state", "target": rs.group("dim"),
                "count": int(rs.group("n")), "operator": rs.group("op"), "raw": s}
    dk = re.match(r"^deck_card_(?P<what>attribute|character_grouping)-(?P<who>.+)-(?P<n>\d+)$",
                  body)
    if dk:
        kind = "type" if dk.group("what") == "attribute" else "generation"
        return {"kind": kind, "target": dk.group("who"), "count": int(dk.group("n")), "raw": s}
    return {"kind": "unknown", "target": body, "count": None, "raw": s}


def _s(f, k):
    v = f.get(k, [None])[0]
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else v


def _n(f, k):
    return f.get(k, [None])[0]


def resolve_active(f):
    """f7 cooldown / f8 chance / f10 duration; f5 gates the f6 conditional effect."""
    permil = _n(f, 8)
    return {
        "kind": "ACTIVE",
        "effect": parse_effect(_s(f, 4)),
        "conditional": {"gate": parse_gate(_s(f, 5)), "effect": parse_effect(_s(f, 6))}
        if _s(f, 6) else None,
        "duration_ms": _n(f, 10),
        "cooldown_ms": _n(f, 7),
        "activation_permil": permil,
        "probability_tier": PROBABILITY_TIER_BY_PERMIL.get(permil),
        "level": _n(f, 2),
    }


def resolve_passive(f):
    """f3 is the deck gate; f4 is the effect plus who it buffs."""
    return {
        "kind": "PASSIVE",
        "gate": parse_gate(_s(f, 3)),
        "effect": parse_effect(_s(f, 4)),
        "level": _n(f, 2),
    }


def resolve_leader(f):
    """LiveLeaderSkill (the COSTUME / outfit skill -- the game's leader slot). Same gate+conditional shape as SPECIAL, one slot earlier:
    f2 gate for the principal effect, f3 principal effect, f4 gate + f5 conditional effect.
    Every leader effect targets `all`; 49 of 167 carry a gate, and 102 are all_parameter_up."""
    return {
        "kind": "COSTUME",
        "gate": parse_gate(_s(f, 2)),
        "effect": parse_effect(_s(f, 3)),
        "conditional": {"gate": parse_gate(_s(f, 4)), "effect": parse_effect(_s(f, 5))}
        if _s(f, 5) else None,
    }


def resolve_special(f):
    """f6 duration; f4 gates the f5 conditional effect (which may be a different family:
    judgement_enhance / life_recovery / activation-probability up)."""
    return {
        "kind": "SPECIAL",
        "effect": parse_effect(_s(f, 3)),
        "conditional": {"gate": parse_gate(_s(f, 4)), "effect": parse_effect(_s(f, 5))}
        if _s(f, 5) else None,
        "duration_ms": _n(f, 6),
        "level": _n(f, 2),
    }
