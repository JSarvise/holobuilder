"""The canonical cards.json / owned.json contract. Pure, no I/O.

This module is the single source of truth for field names, enums, and
validation rules. Nothing here reads a file; `domain/loader.py` is the only
I/O boundary for canonical data.

Absent-vs-zero encoding (strict-explicit): every schema field listed in
`_CARD_REQUIRED_FIELDS` must be present in every card entry. A missing key
is a validation error naming
the offending card id and field. JSON `null` is the only on-disk encoding
for "unknown / never populated" — see `domain/unknown.py` for the `UNKNOWN`
sentinel that `null` is converted into once a `Card` is constructed.

`condition` is covered by this rule too. `null` means UNKNOWN, exactly like
`duration`/`interval`/`magnitude` — it does not mean "ungated".
`{"kind": "ungated"}` is the affirmative on-disk encoding for a skill with no gate at all, and its `target`/`count`
are structurally absent, never null. This makes the invariant exact across
the whole schema: `absent` means "not applicable to this kind" and `null`
means "UNKNOWN (tainting)" — a third representation for anything would
break that one-meaning-per-encoding property.
"""

from __future__ import annotations

SCHEMA_VERSION = "2"

CARD_TYPES = frozenset({"Cute", "Pure", "Happy"})

# Order matches the game's own model exactly.
GENERATIONS: tuple[str, ...] = (
    "0期生",
    "1期生",
    "2期生",
    "ゲーマーズ",
    "3期生",
    "4期生",
    "5期生",
    "holoX",
    "ID1",
    "ID2",
    "ID3",
    "Myth",
    "Promise",
    "Advent",
    "ReGLOSS",
)

SKILL_SLOT_KINDS: tuple[str, ...] = ("SPECIAL", "ACTIVE", "PASSIVE")
COSTUME_SLOT_KIND = "COSTUME"

PROBABILITY_TIERS: tuple[str, ...] = ("Low", "Medium", "High")
# "run_state" is a third condition kind, distinct from "type" and
# "generation" — it resolves against a play-through snapshot (RunState),
# never against the assembled team. See RUN_STATE_DIMENSIONS below.
CONDITION_KINDS: tuple[str, ...] = ("type", "generation", "run_state")

# The affirmative on-disk encoding for "this skill has no gate at
# all". Deliberately not a fourth CONDITION_KINDS member: that tuple names
# GatePredicate dimensions, and "ungated" is not a dimension — it gets its
# own branch in validate_skill_slot instead of a GatePredicate.
UNGATED_CONDITION_KIND: str = "ungated"

# The effect *target* is a clause structurally
# independent of the gate `condition` — "With 2+ Cute Members, Grants
# Score Support of 6% to 3 Cute Members" carries a gate AND a target, and
# many gateless PASSIVEs carry a target with no gate at all.
# Element-for-element the kinds
# `tools/recon/proxy/skills.py::parse_target` emits.
TARGET_KINDS: tuple[str, ...] = ("self", "all", "type", "generation")

# The affirmative on-disk marker for "this effect reaches the whole
# team" — exactly as UNGATED_CONDITION_KIND is the affirmative marker for
# "this skill has no gate", never inferred from a missing key. A slot
# whose `target` is JSON null is UNKNOWN (untranscribed), never team-wide.
TEAM_WIDE_TARGET_KIND: str = "all"

# team.TEAM_SIZE, hardcoded here rather than imported: schema.py must not
# import team.py (schema.py is the base contract team.py itself builds
# on), so the bound this constant enforces (an effect cannot target more
# cards than exist in a team) is named explicitly instead.
MAX_TARGET_COUNT: int = 6

# The shared affirmative "this slot carries no second effect of this shape"
# marker, used by both `override` (a gated override of the slot's own base
# magnitude) and `rider` (a second, differently-typed effect) — the same
# idiom `UNGATED_CONDITION_KIND` and `TEAM_WIDE_TARGET_KIND` use for their
# own fields, rather than a new encoding. Like its siblings, it is never
# inferred from a missing key.
SECOND_EFFECT_ABSENT_KIND: str = "none"

# `override`'s two possible `kind` values: the shared absent marker, or a
# populated gated override of the slot's own base magnitude.
# The override carries no `family`/`effect_type` of its own — by
# definition it overrides the principal's magnitude, and a second copy of
# the family could only ever disagree with the first.
OVERRIDE_KINDS: tuple[str, ...] = (SECOND_EFFECT_ABSENT_KIND, "override")

# `rider`'s two possible `kind` values: the shared absent marker, or a
# populated second, differently-typed effect, retained rather than dropped.
RIDER_KINDS: tuple[str, ...] = (SECOND_EFFECT_ABSENT_KIND, "rider")

# "Structurally absent" table for `override`: the absent
# marker has no payload at all (no dimension to override, no gate to
# check), so both `magnitude` and `condition` are structurally absent for
# it, never null. A populated override requires both.
NOT_APPLICABLE_OVERRIDE_FIELDS_BY_OVERRIDE_KIND: dict[str, frozenset[str]] = {
    SECOND_EFFECT_ABSENT_KIND: frozenset(
        {"magnitude", "condition", "family", "effect_type"}
    ),
    "override": frozenset(),
}

# Import-time guarantee, same style as the condition/target-kind asserts
# above: adding an override kind without deciding its not-applicable
# fields must fail at import, never at load time.
assert set(NOT_APPLICABLE_OVERRIDE_FIELDS_BY_OVERRIDE_KIND) == set(OVERRIDE_KINDS), (
    "NOT_APPLICABLE_OVERRIDE_FIELDS_BY_OVERRIDE_KIND must cover every "
    "OVERRIDE_KINDS member — adding an override kind without deciding its "
    "not-applicable fields must fail at import, never at load time"
)

# Same mechanism for `rider`: the absent marker has no payload at all, so
# every one of its five fields is structurally absent, never null. A
# populated rider requires all five.
NOT_APPLICABLE_RIDER_FIELDS_BY_RIDER_KIND: dict[str, frozenset[str]] = {
    SECOND_EFFECT_ABSENT_KIND: frozenset(
        {"family", "effect_type", "magnitude", "condition", "target"}
    ),
    "rider": frozenset(),
}

# Import-time guarantee, same style as the assert above: adding a rider
# kind without deciding its not-applicable fields must fail at import,
# never at load time.
assert set(NOT_APPLICABLE_RIDER_FIELDS_BY_RIDER_KIND) == set(RIDER_KINDS), (
    "NOT_APPLICABLE_RIDER_FIELDS_BY_RIDER_KIND must cover every "
    "RIDER_KINDS member — adding a rider kind without deciding its "
    "not-applicable fields must fail at import, never at load time"
)

# The only two run-state DIMENSIONS gate resolution may
# reference. Named element-for-element the same as
# `hbuilder.domain.gates.RunState`'s two int fields, so the two declarations
# cannot drift apart silently (pinned by a structural test in
# tests/test_schema.py).
#
# They are numeric, not booleans: a boolean folds the threshold INTO the flag
# name, so it can express exactly one threshold per dimension — and real cards
# carry different LIFE thresholds (600 and 1000) on different skills. The
# threshold is therefore a PER-SKILL parameter carried by the condition's
# `count`, and the run state carries the numeric value it is compared against.
RUN_STATE_DIMENSIONS: tuple[str, ...] = ("life", "combo")

# The comparison operators a run_state gate may carry. "gte" is the default
# (and the only value type/generation gates ever take); "lte" exists for the
# game's LIFE_LTE=11 comeback/desperation triggers (thresholds 500/700/950),
# which fire when LIFE is at or BELOW the threshold — the inverse of "gte".
# Validated only on run_state below; forbidden on every other condition kind,
# so an operator typed on a type/generation/ungated gate is a loud load-time
# failure, not a silently ignored key.
CONDITION_OPERATORS: tuple[str, ...] = ("gte", "lte")

STAT_NAMES: tuple[str, ...] = ("performance", "technique", "sense")

# The six skill families are distinct fields of the model, never
# variants of one generic effect — one slot carries exactly one family.
SKILL_FAMILIES: tuple[str, ...] = (
    "score_support",
    "conditional_score_up",
    "stat_up",
    "activation_rate_up",
    "life_recovery",
    "judgment_enhancement",
)

# Which `effect_type` values each family may carry. `stat_up` maps to the
# three individual stat names — this is what makes a stat-scaling skill
# address one named stat, never the stat sum — plus a fourth
# sentinel, `all_stats`, naming all three at once. Every
# other family maps to a single fixed effect_type naming what it affects.
#
# `all_stats` is the domain's name for the game's own `all_parameter_up`
# ("All Stats UP"), found on many PASSIVE and outfit slots.
# It is deliberately a fourth `effect_type` of the existing `stat_up`
# family, never a seventh `SKILL_FAMILIES` member: a seventh family would
# force an invented default `scoring_config.DEFAULT_CONFIG["family_weights"]`
# entry with no basis to choose it. Not named `"all"`: that literal is
# already a `TARGET_KINDS` member meaning "reaches every card" — reusing it
# here would make `effect_type=all` and `target.kind=all` visually
# identical in `--explain` output and in every grep, while meaning two
# unrelated things.
EFFECT_TYPES_BY_FAMILY: dict[str, frozenset[str]] = {
    "score_support": frozenset({"score"}),
    "conditional_score_up": frozenset({"score"}),
    "stat_up": frozenset({"performance", "technique", "sense", "all_stats"}),
    "activation_rate_up": frozenset({"activation_rate"}),
    "life_recovery": frozenset({"life"}),
    "judgment_enhancement": frozenset({"judgment"}),
}

# Fields that structurally do not exist for a given slot kind. Populating
# one anyway is the most damaging data defect — routing a
# gauge-fired (SPECIAL) or always-on (PASSIVE/COSTUME) skill through the
# ACTIVE formula — caught here at load time rather than at score time.
NOT_APPLICABLE_FIELDS_BY_SLOT_KIND: dict[str, frozenset[str]] = {
    "ACTIVE": frozenset(),
    "SPECIAL": frozenset({"interval", "probability_tier"}),
    "PASSIVE": frozenset({"duration", "interval", "probability_tier"}),
    COSTUME_SLOT_KIND: frozenset({"duration", "interval", "probability_tier"}),
}

# The same "structurally absent" mechanism, generalized to key on
# condition kind as well as slot kind — no new concept. `target` and
# `count` do not exist for an ungated condition: there is no dimension to
# target and no threshold to meet, so both must be structurally absent,
# the same way SPECIAL's `interval`/`probability_tier` are structurally
# absent above. Every real CONDITION_KINDS member has nothing
# not-applicable — this map exists so ungated's exception is expressed in
# the same shape as every other not-applicable rule, not as a special case.
NOT_APPLICABLE_CONDITION_FIELDS_BY_CONDITION_KIND: dict[str, frozenset[str]] = {
    "type": frozenset(),
    "generation": frozenset(),
    "run_state": frozenset(),
    UNGATED_CONDITION_KIND: frozenset({"target", "count"}),
}

# Import-time guarantee, same style as the assertions immediately below:
# adding a condition kind without deciding its not-applicable fields must
# fail at import, never at load time.
assert set(NOT_APPLICABLE_CONDITION_FIELDS_BY_CONDITION_KIND) == set(CONDITION_KINDS) | {
    UNGATED_CONDITION_KIND
}, (
    "NOT_APPLICABLE_CONDITION_FIELDS_BY_CONDITION_KIND must cover every "
    "CONDITION_KINDS member plus UNGATED_CONDITION_KIND — adding a "
    "condition kind without deciding its not-applicable fields must fail "
    "at import, never at load time"
)

# The same "structurally absent" mechanism as
# NOT_APPLICABLE_CONDITION_FIELDS_BY_CONDITION_KIND, keyed on target kind.
# "self" and the affirmative "all" marker have no dimension to target and
# no count to select (self is always exactly the source card; "all" is
# always every card) — both `value` and `count` are structurally absent
# for them, never null. "type" and "generation" require both.
NOT_APPLICABLE_TARGET_FIELDS_BY_TARGET_KIND: dict[str, frozenset[str]] = {
    "self": frozenset({"value", "count"}),
    TEAM_WIDE_TARGET_KIND: frozenset({"value", "count"}),
    "type": frozenset(),
    "generation": frozenset(),
}

# Import-time guarantee, same style as the condition-kind assert above:
# adding a target kind without deciding its not-applicable fields must
# fail at import, never at load time.
assert set(NOT_APPLICABLE_TARGET_FIELDS_BY_TARGET_KIND) == set(TARGET_KINDS), (
    "NOT_APPLICABLE_TARGET_FIELDS_BY_TARGET_KIND must cover every "
    "TARGET_KINDS member — adding a target kind without deciding its "
    "not-applicable fields must fail at import, never at load time"
)

# This map constrains a slot's PRINCIPAL family only — the value of
# `record["family"]`, never a slot's `rider.family`. A slot's rider is
# deliberately NOT checked against this map: the map exists solely to keep a
# family's *formula* paired with the slot kind that formula's inputs assume,
# and a rider is never dispatched through the principal's formula. Scoring a
# rider needs its own arithmetic (the rider's slot kind is whatever the
# *principal* slot happens to be, e.g. SPECIAL for the activation-rate rider),
# never `_score_active` reused as-is; this map would not change to permit
# that; a rider-shaped fold is added alongside it instead.
# `validate_skill_slot` makes the scope explicit in code, not just here:
# the local it consults is named `principal_family`, never the bare
# `family` a reader could mistake for "the whole slot's family".
#
# `activation_rate_up` is tied to ACTIVE because its fold discounts it by
# uptime and proc chance — the ACTIVE formula's own inputs. In the game's own
# data it never occurs as a PRINCIPAL effect; it appears only as a RIDER on
# SPECIAL slots, which this map never checks (see above). The ACTIVE-principal
# path this guard protects is exercised only by the synthetic six-card
# fixture `fubuki_01` in `data/cards.json`, which the test suite depends on.
# The guard stays, both for that fixture and as defence in depth.
FAMILY_REQUIRES_SLOT_KIND: dict[str, str] = {
    "activation_rate_up": "ACTIVE",
}

# Import-time guarantee: a renamed family or slot kind must fail at import,
# never silently disable this rule.
assert all(family in SKILL_FAMILIES for family in FAMILY_REQUIRES_SLOT_KIND), (
    "FAMILY_REQUIRES_SLOT_KIND keys must all be members of SKILL_FAMILIES"
)
assert all(kind in SKILL_SLOT_KINDS for kind in FAMILY_REQUIRES_SLOT_KIND.values()), (
    "FAMILY_REQUIRES_SLOT_KIND values must all be members of SKILL_SLOT_KINDS"
)

_SKILL_SLOT_REQUIRED_FIELDS: tuple[str, ...] = (
    "family",
    "effect_type",
    "duration",
    "interval",
    "probability_tier",
    "magnitude",
    "condition",
    "target",
    "override",
    "rider",
)

_SLOT_NULLABLE_NUMERIC_FIELDS: tuple[str, ...] = ("duration", "interval", "magnitude")

_CARD_REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "member",
    "generation",
    "type",
    "rarity",
    "costume_name",
    "stats",
    "skills",
    "costume_skill",
)


class SchemaError(Exception):
    """Raised when on-disk data violates the canonical contract.

    `card_id` and `field` are attached whenever known so a validation
    failure is always attributable to a specific card and field, never a
    silently-skipped entry. `str()` appends both whenever set, in
    addition to whatever the `message` itself already says.
    """

    def __init__(
        self,
        message: str,
        *,
        card_id: str | None = None,
        field: str | None = None,
    ) -> None:
        self.card_id = card_id
        self.field = field
        location_parts = []
        if card_id is not None:
            location_parts.append(f"card={card_id}")
        if field is not None:
            location_parts.append(f"field={field}")
        full_message = message
        if location_parts:
            full_message = f"{message} ({', '.join(location_parts)})"
        super().__init__(full_message)


def _record_id(record: object, index: int) -> str:
    if isinstance(record, dict):
        candidate = record.get("id")
        if isinstance(candidate, str) and candidate:
            return candidate
    return f"<card at index {index}>"


def validate_document(doc: object) -> list[str]:
    """Validate the top-level cards.json envelope.

    Returns a list of human-readable error strings, each formatted as
    `{field}: {detail}` (empty list means valid).
    """
    errors: list[str] = []
    if not isinstance(doc, dict):
        errors.append("root: document must be a JSON object")
        return errors

    if "schema_version" not in doc:
        errors.append("schema_version: missing required key")
    elif doc["schema_version"] != SCHEMA_VERSION:
        errors.append(
            f"schema_version: unsupported value {doc['schema_version']!r}, "
            f"expected {SCHEMA_VERSION!r}"
        )

    if "cards" not in doc:
        errors.append("cards: missing required key")
    elif not isinstance(doc["cards"], list):
        errors.append("cards: must be a JSON array")

    return errors


def validate_owned_document(doc: object) -> list[str]:
    """Validate the top-level owned.json envelope. Returns error strings."""
    errors: list[str] = []
    if not isinstance(doc, dict):
        errors.append("root: document must be a JSON object")
        return errors

    if "schema_version" not in doc:
        errors.append("schema_version: missing required key")
    elif doc["schema_version"] != SCHEMA_VERSION:
        errors.append(
            f"schema_version: unsupported value {doc['schema_version']!r}, "
            f"expected {SCHEMA_VERSION!r}"
        )

    if "owned" not in doc:
        errors.append("owned: missing required key")
    elif not isinstance(doc["owned"], list):
        errors.append("owned: must be a JSON array")
    elif not all(isinstance(entry, str) for entry in doc["owned"]):
        errors.append("owned: every entry must be a string card id")

    return errors


def validate_skill_slot(record: object, card_id: str, slot_kind: str) -> list[str]:
    """Validate one skill-slot (or costume_skill) value against the full
    slot contract.

    `record` may be JSON `null` — the card has no skill in this slot at
    all, which is valid and distinct from a populated slot whose
    individual fields are `null` (unknown). A populated slot must carry
    every field in `_SKILL_SLOT_REQUIRED_FIELDS`; each missing field is
    reported as its own error so an empty object names every field it
    lacks rather than one generic message.

    Returns error strings, each naming `card_id`, `slot_kind` and the
    offending field path (`condition.count` for a nested field). Reports
    only — never clamps, rounds, or normalizes a value.
    """
    if record is None:
        return []
    if not isinstance(record, dict):
        return [
            f"skills.{slot_kind}: card '{card_id}' {slot_kind} slot must be a "
            f"JSON object or null"
        ]

    errors: list[str] = []
    for field in _SKILL_SLOT_REQUIRED_FIELDS:
        if field not in record:
            errors.append(
                f"{field}: card '{card_id}' {slot_kind} slot is missing "
                f"required field '{field}'"
            )

    not_applicable = NOT_APPLICABLE_FIELDS_BY_SLOT_KIND.get(slot_kind, frozenset())

    # This is the slot's PRINCIPAL family — record["family"] — never
    # a rider's family. Named principal_family, not family, so a
    # reader consulting FAMILY_REQUIRES_SLOT_KIND below cannot mistake this
    # for a slot-wide rule (`_validate_rider` deliberately never runs this
    # same check against `rider.family` — see FAMILY_REQUIRES_SLOT_KIND's
    # own comment).
    principal_family = record.get("family")
    if "family" in record and principal_family not in SKILL_FAMILIES:
        errors.append(
            f"family: card '{card_id}' {slot_kind} slot family "
            f"{principal_family!r} is not one of {list(SKILL_FAMILIES)}"
        )

    required_slot_kind = FAMILY_REQUIRES_SLOT_KIND.get(principal_family)
    if required_slot_kind is not None and slot_kind != required_slot_kind:
        errors.append(
            f"family: card '{card_id}' {slot_kind} slot family "
            f"{principal_family!r} is only valid on an {required_slot_kind} "
            f"slot"
        )

    if "effect_type" in record and principal_family in EFFECT_TYPES_BY_FAMILY:
        effect_type = record.get("effect_type")
        accepted = EFFECT_TYPES_BY_FAMILY[principal_family]
        if effect_type not in accepted:
            errors.append(
                f"effect_type: card '{card_id}' {slot_kind} slot effect_type "
                f"{effect_type!r} is not one of {sorted(accepted)} for family "
                f"'{principal_family}'"
            )

    for field in _SLOT_NULLABLE_NUMERIC_FIELDS:
        if field not in record:
            continue
        value = record[field]
        if field in not_applicable:
            if value is not None:
                errors.append(
                    f"{field}: card '{card_id}' {slot_kind} slot must leave "
                    f"'{field}' null — not applicable to a {slot_kind} slot"
                )
            continue
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(
                f"{field}: card '{card_id}' {slot_kind} slot {field} must be "
                f"null or a non-negative number, got {value!r}"
            )
        elif value < 0:
            errors.append(
                f"{field}: card '{card_id}' {slot_kind} slot {field} must be "
                f"non-negative, got {value}"
            )
        elif field == "interval" and value == 0:
            errors.append(
                f"interval: card '{card_id}' {slot_kind} slot interval must "
                f"be greater than 0 when known — Phase 2 divides by it, got 0"
            )

    if "probability_tier" in record:
        tier = record["probability_tier"]
        if "probability_tier" in not_applicable:
            if tier is not None:
                errors.append(
                    f"probability_tier: card '{card_id}' {slot_kind} slot "
                    f"must leave 'probability_tier' null — not applicable to "
                    f"a {slot_kind} slot"
                )
        elif tier is not None and tier not in PROBABILITY_TIERS:
            errors.append(
                f"probability_tier: card '{card_id}' {slot_kind} slot "
                f"probability_tier {tier!r} is not one of "
                f"{list(PROBABILITY_TIERS)}"
            )

    if "condition" in record:
        errors.extend(_validate_condition(record["condition"], card_id, slot_kind))

    if "target" in record:
        errors.extend(_validate_target(record["target"], card_id, slot_kind))

    if "override" in record:
        errors.extend(_validate_override(record["override"], card_id, slot_kind))

    if "rider" in record:
        errors.extend(_validate_rider(record["rider"], card_id, slot_kind))

    return errors


def _validate_condition(
    condition: object, card_id: str, slot_kind: str, *, field: str = "condition"
) -> list[str]:
    """The `condition` (gate) validation rule, extracted so the override's
    own condition and the rider's own condition can reuse this exact rule
    rather than a second/third copy of it — the `_REQUIRED_KNOWN_FIELDS`
    comment in `scoring.py` names writing the same rule twice as the
    failure mode this avoids.

    `field` is the dotted path this call reports under: `"condition"` for
    the slot's own gate (the default), `"override.condition"` for the
    override's nested gate, `"rider.condition"` for the rider's nested
    gate — one rule, three callers, each naming its own field path."""
    errors: list[str] = []
    if condition is None:
        # null is UNKNOWN, not "ungated" — the transcriber has
        # not yet recorded whether this skill is gated at all. Valid,
        # and deliberately runs no sub-field validation: an unknown
        # condition taints the card via Card.unknown_fields() later,
        # it is never scrutinized as if it were a populated gate.
        pass
    elif not isinstance(condition, dict):
        errors.append(
            f"{field}: card '{card_id}' {slot_kind} slot {field} "
            f"must be a JSON object or null"
        )
    else:
        kind = condition.get("kind")
        allowed_kinds = (*CONDITION_KINDS, UNGATED_CONDITION_KIND)
        if "kind" not in condition or kind not in allowed_kinds:
            errors.append(
                f"{field}.kind: card '{card_id}' {slot_kind} slot "
                f"{field}.kind {kind!r} is not one of "
                f"{list(allowed_kinds)}"
            )
        elif kind == UNGATED_CONDITION_KIND:
            # Real card text puts the gate in the second
            # sentence of the skill description — "For 9s, Score UP
            # 50%. ... When LIFE is 600 or higher, Score UP 100%."
            # Transcribing sentence one and stopping is the single
            # most likely data-entry error in this project; requiring
            # target/count to be structurally absent here converts
            # that error from a silent overstatement into a schema
            # failure the moment someone types a target/count anyway.
            not_applicable = NOT_APPLICABLE_CONDITION_FIELDS_BY_CONDITION_KIND[kind]
            if "target" in not_applicable and "target" in condition:
                errors.append(
                    f"{field}.target: card '{card_id}' {slot_kind} "
                    f"slot {field}.target must be omitted for an "
                    f"ungated condition — there is no dimension to "
                    f"target"
                )
            if "count" in not_applicable and "count" in condition:
                errors.append(
                    f"{field}.count: card '{card_id}' {slot_kind} "
                    f"slot {field}.count must be omitted for an "
                    f"ungated condition — there is no threshold to meet"
                )
        else:
            target = condition.get("target")
            if "target" not in condition or not isinstance(target, str) or not target:
                errors.append(
                    f"{field}.target: card '{card_id}' {slot_kind} "
                    f"slot {field}.target must be a non-empty string"
                )
            elif kind == "type" and target not in CARD_TYPES:
                errors.append(
                    f"{field}.target: card '{card_id}' {slot_kind} "
                    f"slot {field}.target {target!r} is not a known "
                    f"type"
                )
            elif kind == "generation" and target not in GENERATIONS:
                errors.append(
                    f"{field}.target: card '{card_id}' {slot_kind} "
                    f"slot {field}.target {target!r} is not a known "
                    f"generation"
                )
            elif kind == "run_state" and target not in RUN_STATE_DIMENSIONS:
                errors.append(
                    f"{field}.target: card '{card_id}' {slot_kind} "
                    f"slot {field}.target {target!r} is not a known "
                    f"run-state dimension"
                )

            count = condition.get("count")
            if "count" not in condition:
                errors.append(
                    f"{field}.count: card '{card_id}' {slot_kind} slot "
                    f"{field}.count is missing"
                )
            elif isinstance(count, bool) or not isinstance(count, int) or count < 1:
                errors.append(
                    f"{field}.count: card '{card_id}' {slot_kind} slot "
                    f"{field}.count must be an integer >= 1, got "
                    f"{count!r}"
                )
            # NO `count != 1` pin for run_state: `count` is the real numeric
            # threshold the run-state dimension is compared against
            # ("LIFE >= 600" is count=600), so pinning it to 1 would make
            # every real run-state gate unrepresentable. The generic
            # `count >= 1` check above still applies.

            # `operator` (gte|lte) is optional and meaningful ONLY on a
            # run_state gate — LIFE_LTE=11 fires at or below its threshold,
            # the inverse of the default gte. On a type/generation gate it
            # is structurally meaningless (set-membership counts are always
            # gte), so its mere presence there is a load-time failure rather
            # than a silently ignored key — the same "populated-where-absent"
            # discipline as NOT_APPLICABLE_CONDITION_FIELDS_BY_CONDITION_KIND.
            if "operator" in condition:
                operator = condition.get("operator")
                if kind != "run_state":
                    errors.append(
                        f"{field}.operator: card '{card_id}' {slot_kind} "
                        f"slot {field}.operator must be omitted for a "
                        f"{kind!r} condition — only a run_state gate has a "
                        f"comparison direction (type/generation counts are "
                        f"always gte)"
                    )
                elif operator not in CONDITION_OPERATORS:
                    errors.append(
                        f"{field}.operator: card '{card_id}' {slot_kind} "
                        f"slot {field}.operator {operator!r} is not one of "
                        f"{list(CONDITION_OPERATORS)}"
                    )

    return errors


def _validate_target(
    target: object, card_id: str, slot_kind: str, *, field: str = "target"
) -> list[str]:
    """The effect-target validation rule, a sibling of
    `_validate_condition` rather than a variant of it — `condition` and
    `target` are structurally independent clauses (a slot can carry a
    gate AND a target, a target with no gate at all, or (rarely) neither).

    `None` (JSON null) is valid and runs no sub-field validation — the
    transcriber has not recorded who this effect reaches, and it taints the
    card via `Card.unknown_fields()` later, exactly like an untranscribed
    `condition`. `{"kind": "all"}` is the affirmative "reaches the whole
    team" marker, never inferred from a missing key.

    `field` is the dotted path this call reports under: `"target"` for the
    slot's own target (the default), `"rider.target"` for the rider's
    nested target — one rule, two callers."""
    errors: list[str] = []
    if target is None:
        pass
    elif not isinstance(target, dict):
        errors.append(
            f"{field}: card '{card_id}' {slot_kind} slot {field} must be a "
            f"JSON object or null"
        )
    else:
        kind = target.get("kind")
        if "kind" not in target or kind not in TARGET_KINDS:
            errors.append(
                f"{field}.kind: card '{card_id}' {slot_kind} slot "
                f"{field}.kind {kind!r} is not one of {list(TARGET_KINDS)}"
            )
        else:
            not_applicable = NOT_APPLICABLE_TARGET_FIELDS_BY_TARGET_KIND[kind]
            if not_applicable:
                # "self" / "all": no dimension to target, no count to
                # select — present-anyway is a loud schema failure, the
                # same discipline applied to an ungated condition.
                if "value" in not_applicable and "value" in target:
                    errors.append(
                        f"{field}.value: card '{card_id}' {slot_kind} slot "
                        f"{field}.value must be omitted for a {kind!r} "
                        f"target — there is no dimension to target"
                    )
                if "count" in not_applicable and "count" in target:
                    errors.append(
                        f"{field}.count: card '{card_id}' {slot_kind} slot "
                        f"{field}.count must be omitted for a {kind!r} "
                        f"target — there is no count to select"
                    )
            else:
                value = target.get("value")
                if "value" not in target or not isinstance(value, str) or not value:
                    errors.append(
                        f"{field}.value: card '{card_id}' {slot_kind} slot "
                        f"{field}.value must be a non-empty string"
                    )
                elif kind == "type" and value not in CARD_TYPES:
                    errors.append(
                        f"{field}.value: card '{card_id}' {slot_kind} slot "
                        f"{field}.value {value!r} is not a known type"
                    )
                elif kind == "generation" and value not in GENERATIONS:
                    errors.append(
                        f"{field}.value: card '{card_id}' {slot_kind} slot "
                        f"{field}.value {value!r} is not a known generation"
                    )

                count = target.get("count")
                if "count" not in target:
                    errors.append(
                        f"{field}.count: card '{card_id}' {slot_kind} slot "
                        f"{field}.count is missing"
                    )
                elif (
                    isinstance(count, bool)
                    or not isinstance(count, int)
                    or count < 1
                    or count > MAX_TARGET_COUNT
                ):
                    errors.append(
                        f"{field}.count: card '{card_id}' {slot_kind} slot "
                        f"{field}.count must be an integer in the range "
                        f"1..{MAX_TARGET_COUNT}, got {count!r}"
                    )

    return errors


def _validate_override(override: object, card_id: str, slot_kind: str) -> list[str]:
    """The `override` validation rule: the three-state contract
    (unknown / absent / populated) applied to a slot's optional gated
    override of its own base magnitude.

    `None` (JSON null) is valid and runs no sub-field validation — the
    transcriber has not recorded whether this slot carries an override at
    all, and it taints the card via `Card.unknown_fields()` later, exactly
    like an untranscribed `condition`/`target`. `{"kind": "none"}` is the
    affirmative "this slot carries no override" marker, never inferred
    from a missing key.

    The override deliberately carries no `family`/`effect_type` of its
    own: by definition it overrides the principal's magnitude, and a
    second copy of the family could only ever disagree with the first.
    Its nested `condition` delegates to `_validate_condition` (one rule,
    three callers) — but a populated override's condition must itself be
    both present and gated: `null` (unknown) and `{"kind": "ungated"}`
    are both rejected here, because an override with no gate at all is
    not an override, it is simply a different base magnitude."""
    errors: list[str] = []
    if override is None:
        pass
    elif not isinstance(override, dict):
        errors.append(
            f"override: card '{card_id}' {slot_kind} slot override must be "
            f"a JSON object or null"
        )
    else:
        kind = override.get("kind")
        if "kind" not in override or kind not in OVERRIDE_KINDS:
            errors.append(
                f"override.kind: card '{card_id}' {slot_kind} slot "
                f"override.kind {kind!r} is not one of {list(OVERRIDE_KINDS)}"
            )
        else:
            not_applicable = NOT_APPLICABLE_OVERRIDE_FIELDS_BY_OVERRIDE_KIND[kind]
            if not_applicable:
                # Mirrors `_validate_rider`'s not-applicable loop: a "none"-kind
                # override rejects a stray `family`/`effect_type` key exactly like
                # the populated ("override") branch below already rejects them on
                # a populated override — a hand-authored `{"kind": "none",
                # "family": ...}` must not validate clean just because the
                # payload is empty.
                for override_field in (
                    "magnitude",
                    "condition",
                    "family",
                    "effect_type",
                ):
                    if override_field in not_applicable and override_field in override:
                        errors.append(
                            f"override.{override_field}: card '{card_id}' "
                            f"{slot_kind} slot override.{override_field} must "
                            f"be omitted for a {kind!r} override — there is "
                            f"no second payload"
                        )
            else:
                # A populated override deliberately carries no `family`/
                # `effect_type` of its own:
                # by definition it overrides the principal's magnitude,
                # and a second copy of the family could only ever
                # disagree with the first.
                for stray_field in ("family", "effect_type"):
                    if stray_field in override:
                        errors.append(
                            f"override.{stray_field}: card '{card_id}' "
                            f"{slot_kind} slot override.{stray_field} must "
                            f"be omitted — an override carries no family "
                            f"or effect_type of its own, it overrides only "
                            f"the principal's magnitude"
                        )

                magnitude = override.get("magnitude")
                if "magnitude" not in override:
                    errors.append(
                        f"override.magnitude: card '{card_id}' {slot_kind} "
                        f"slot override.magnitude is missing"
                    )
                elif (
                    magnitude is None
                    or isinstance(magnitude, bool)
                    or not isinstance(magnitude, (int, float))
                ):
                    errors.append(
                        f"override.magnitude: card '{card_id}' {slot_kind} "
                        f"slot override.magnitude must be a non-negative "
                        f"number, got {magnitude!r} — an override whose "
                        f"magnitude is unknown is an untranscribed "
                        f"override, not a partially-known one; the whole "
                        f"'override' field must be null instead"
                    )
                elif magnitude < 0:
                    errors.append(
                        f"override.magnitude: card '{card_id}' {slot_kind} "
                        f"slot override.magnitude must be non-negative, "
                        f"got {magnitude}"
                    )

                if "condition" not in override:
                    errors.append(
                        f"override.condition: card '{card_id}' {slot_kind} "
                        f"slot override.condition is missing"
                    )
                else:
                    condition = override["condition"]
                    if condition is None or (
                        isinstance(condition, dict)
                        and condition.get("kind") == UNGATED_CONDITION_KIND
                    ):
                        errors.append(
                            f"override.condition: card '{card_id}' "
                            f"{slot_kind} slot override.condition must be a "
                            f"populated, gated condition — an override "
                            f"with no gate at all is not an override, it "
                            f"is a different base magnitude"
                        )
                    else:
                        errors.extend(
                            _validate_condition(
                                condition,
                                card_id,
                                slot_kind,
                                field="override.condition",
                            )
                        )

    return errors


def _validate_rider(rider: object, card_id: str, slot_kind: str) -> list[str]:
    """The `rider` validation rule: a slot's optional second,
    differently-typed effect, retained rather than dropped.

    Same three-state contract as `override`: `None` taints, `{"kind":
    "none"}` is the affirmative "no rider" marker, and a populated
    `{"kind": "rider", ...}` carries its own `family`/`effect_type`/
    `magnitude`/`condition`/`target`, the last two delegating to the
    existing `_validate_condition`/`_validate_target` helpers.

    Deliberately does NOT apply `FAMILY_REQUIRES_SLOT_KIND` to the rider's
    family here — see that map's own comment for why: it
    constrains a slot's principal family only, by design, not as a gap
    still to be closed."""
    errors: list[str] = []
    if rider is None:
        pass
    elif not isinstance(rider, dict):
        errors.append(
            f"rider: card '{card_id}' {slot_kind} slot rider must be a "
            f"JSON object or null"
        )
    else:
        kind = rider.get("kind")
        if "kind" not in rider or kind not in RIDER_KINDS:
            errors.append(
                f"rider.kind: card '{card_id}' {slot_kind} slot rider.kind "
                f"{kind!r} is not one of {list(RIDER_KINDS)}"
            )
        else:
            not_applicable = NOT_APPLICABLE_RIDER_FIELDS_BY_RIDER_KIND[kind]
            if not_applicable:
                for rider_field in (
                    "family",
                    "effect_type",
                    "magnitude",
                    "condition",
                    "target",
                ):
                    if rider_field in not_applicable and rider_field in rider:
                        errors.append(
                            f"rider.{rider_field}: card '{card_id}' "
                            f"{slot_kind} slot rider.{rider_field} must be "
                            f"omitted for a {kind!r} rider — there is no "
                            f"second payload"
                        )
            else:
                family = rider.get("family")
                if "family" not in rider or family not in SKILL_FAMILIES:
                    errors.append(
                        f"rider.family: card '{card_id}' {slot_kind} slot "
                        f"rider.family {family!r} is not one of "
                        f"{list(SKILL_FAMILIES)}"
                    )

                effect_type = rider.get("effect_type")
                if "effect_type" not in rider:
                    errors.append(
                        f"rider.effect_type: card '{card_id}' {slot_kind} "
                        f"slot rider.effect_type is missing"
                    )
                elif family in EFFECT_TYPES_BY_FAMILY:
                    accepted = EFFECT_TYPES_BY_FAMILY[family]
                    if effect_type not in accepted:
                        errors.append(
                            f"rider.effect_type: card '{card_id}' "
                            f"{slot_kind} slot rider.effect_type "
                            f"{effect_type!r} is not one of "
                            f"{sorted(accepted)} for family {family!r}"
                        )

                magnitude = rider.get("magnitude")
                if "magnitude" not in rider:
                    errors.append(
                        f"rider.magnitude: card '{card_id}' {slot_kind} "
                        f"slot rider.magnitude is missing"
                    )
                elif (
                    magnitude is None
                    or isinstance(magnitude, bool)
                    or not isinstance(magnitude, (int, float))
                ):
                    errors.append(
                        f"rider.magnitude: card '{card_id}' {slot_kind} "
                        f"slot rider.magnitude must be a non-negative "
                        f"number, got {magnitude!r}"
                    )
                elif magnitude < 0:
                    errors.append(
                        f"rider.magnitude: card '{card_id}' {slot_kind} "
                        f"slot rider.magnitude must be non-negative, got "
                        f"{magnitude}"
                    )

                if "condition" not in rider:
                    errors.append(
                        f"rider.condition: card '{card_id}' {slot_kind} "
                        f"slot rider.condition is missing"
                    )
                else:
                    errors.extend(
                        _validate_condition(
                            rider["condition"],
                            card_id,
                            slot_kind,
                            field="rider.condition",
                        )
                    )

                if "target" not in rider:
                    errors.append(
                        f"rider.target: card '{card_id}' {slot_kind} slot "
                        f"rider.target is missing"
                    )
                else:
                    errors.extend(
                        _validate_target(
                            rider["target"],
                            card_id,
                            slot_kind,
                            field="rider.target",
                        )
                    )

    return errors


def validate_card(record: object, index: int) -> list[str]:
    """Validate one card record.

    Returns human-readable error strings, each formatted as `{field}:
    {detail}` — the field name is always the text before the first ': ',
    and `{detail}` always names the offending card id.
    """
    if not isinstance(record, dict):
        return [f"root: card at index {index} must be a JSON object"]

    card_id = _record_id(record, index)
    errors: list[str] = []

    for field in _CARD_REQUIRED_FIELDS:
        if field not in record:
            errors.append(
                f"{field}: card '{card_id}' is missing required field '{field}'"
            )

    if "id" in record:
        if not isinstance(record["id"], str) or not record["id"]:
            errors.append(f"id: card '{card_id}' id must be a non-empty string")

    if "member" in record:
        if not isinstance(record["member"], str) or not record["member"]:
            errors.append(f"member: card '{card_id}' member must be a non-empty string")

    if "generation" in record:
        generation = record["generation"]
        if isinstance(generation, str):
            errors.append(
                f"generation: card '{card_id}' generation must be a JSON array, "
                f"not a bare string — got {generation!r}"
            )
        elif not isinstance(generation, list):
            errors.append(
                f"generation: card '{card_id}' generation must be a JSON array, "
                f"got {type(generation).__name__}"
            )
        elif len(generation) == 0:
            errors.append(
                f"generation: card '{card_id}' generation must contain at least "
                f"one generation name, got an empty array"
            )
        else:
            for entry in generation:
                if not isinstance(entry, str) or entry not in GENERATIONS:
                    errors.append(
                        f"generation: card '{card_id}' generation entry "
                        f"{entry!r} is not a known generation name"
                    )
                    break

    if "type" in record:
        if record["type"] not in CARD_TYPES:
            errors.append(
                f"type: card '{card_id}' type {record['type']!r} is not one of "
                f"{sorted(CARD_TYPES)}"
            )

    if "rarity" in record:
        if not isinstance(record["rarity"], str):
            errors.append(f"rarity: card '{card_id}' rarity must be a string")

    if "costume_name" in record:
        if not isinstance(record["costume_name"], str):
            errors.append(f"costume_name: card '{card_id}' costume_name must be a string")

    if "stats" in record:
        stats = record["stats"]
        if not isinstance(stats, dict):
            errors.append(f"stats: card '{card_id}' stats must be a JSON object")
        else:
            for stat_name in STAT_NAMES:
                if stat_name not in stats:
                    errors.append(
                        f"stats.{stat_name}: card '{card_id}' stats is missing "
                        f"'{stat_name}'"
                    )
                elif not isinstance(stats[stat_name], int) or isinstance(
                    stats[stat_name], bool
                ):
                    errors.append(
                        f"stats.{stat_name}: card '{card_id}' stats.{stat_name} "
                        f"must be an int"
                    )

    if "skills" in record:
        skills = record["skills"]
        if not isinstance(skills, dict):
            errors.append(f"skills: card '{card_id}' skills must be a JSON object")
        elif set(skills.keys()) != set(SKILL_SLOT_KINDS):
            errors.append(
                f"skills: card '{card_id}' skills keys must be exactly "
                f"{list(SKILL_SLOT_KINDS)}, got {sorted(skills.keys())}"
            )
        else:
            for slot_kind in SKILL_SLOT_KINDS:
                errors.extend(
                    validate_skill_slot(skills[slot_kind], card_id, slot_kind)
                )

    if "costume_skill" in record:
        errors.extend(
            validate_skill_slot(record["costume_skill"], card_id, COSTUME_SLOT_KIND)
        )

    return errors
