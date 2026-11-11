"""The canonical cards.json / owned.json contract. Pure, no I/O.

The single source of truth for field names, enums and validation rules;
`domain/loader.py` is the only I/O boundary for canonical data.

Every field in `_CARD_REQUIRED_FIELDS` must be present in every card entry (a
missing key is an error naming the card and field). One meaning per encoding
across the whole schema: an ABSENT key means "not applicable to this kind", JSON
`null` means UNKNOWN (tainting; `domain/unknown.py`'s `UNKNOWN` sentinel once a
`Card` is built), and "nothing here" is an affirmative marker, e.g.
`{"kind": "ungated"}` for a skill with no gate (whose `target`/`count` are absent,
never null). So a `null` `condition` is unknown, never "ungated".
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
# "run_state" resolves against a play-through snapshot (RunState), never against
# the assembled team. See RUN_STATE_DIMENSIONS below.
CONDITION_KINDS: tuple[str, ...] = ("type", "generation", "run_state")

# The affirmative on-disk encoding for "this skill has no gate at all". Not a
# CONDITION_KINDS member: that tuple names GatePredicate dimensions, and "ungated"
# is not one; it gets its own branch in validation instead.
UNGATED_CONDITION_KIND: str = "ungated"

# The effect *target* is independent of the gate `condition`: "With 2+ Cute
# Members, Grants Score Support of 6% to 3 Cute Members" carries both, and many
# PASSIVEs carry a target with no gate. The kinds `tools/refresh/skills.py::
# parse_target` emits.
TARGET_KINDS: tuple[str, ...] = ("self", "all", "type", "generation")

# The affirmative marker for "this effect reaches the whole team", never inferred
# from a missing key. A `target` of JSON null is UNKNOWN, never team-wide.
TEAM_WIDE_TARGET_KIND: str = "all"

# The team size (leader + 5 members): an effect cannot target more cards. Not
# imported from team.py, which builds on this module.
MAX_TARGET_COUNT: int = 6

# The affirmative "this slot carries no second effect of this shape" marker, for
# both `override` (a gated override of the slot's own base magnitude) and `rider`
# (a second, differently-typed effect). Never inferred from a missing key.
SECOND_EFFECT_ABSENT_KIND: str = "none"

# `override`'s two `kind` values: the absent marker, or a gated override of the
# slot's own base magnitude. It carries no `family`/`effect_type` of its own: it
# overrides the principal's magnitude, and a second copy could only disagree.
OVERRIDE_KINDS: tuple[str, ...] = (SECOND_EFFECT_ABSENT_KIND, "override")

# `rider`'s two `kind` values: the absent marker, or a second, differently-typed
# effect.
RIDER_KINDS: tuple[str, ...] = (SECOND_EFFECT_ABSENT_KIND, "rider")

# The absent override marker has no payload, so its fields are absent, never
# null. A populated override requires `magnitude` and `condition`.
NOT_APPLICABLE_OVERRIDE_FIELDS_BY_OVERRIDE_KIND: dict[str, frozenset[str]] = {
    SECOND_EFFECT_ABSENT_KIND: frozenset({"magnitude", "condition", "family", "effect_type"}),
    "override": frozenset(),
}

# Adding a kind without deciding its not-applicable fields fails at import.
assert set(NOT_APPLICABLE_OVERRIDE_FIELDS_BY_OVERRIDE_KIND) == set(OVERRIDE_KINDS), (
    "NOT_APPLICABLE_OVERRIDE_FIELDS_BY_OVERRIDE_KIND must cover every "
    "OVERRIDE_KINDS member — adding an override kind without deciding its "
    "not-applicable fields must fail at import, never at load time"
)

# The same for `rider`: a populated rider requires all five fields.
NOT_APPLICABLE_RIDER_FIELDS_BY_RIDER_KIND: dict[str, frozenset[str]] = {
    SECOND_EFFECT_ABSENT_KIND: frozenset({"family", "effect_type", "magnitude", "condition", "target"}),
    "rider": frozenset(),
}

assert set(NOT_APPLICABLE_RIDER_FIELDS_BY_RIDER_KIND) == set(RIDER_KINDS), (
    "NOT_APPLICABLE_RIDER_FIELDS_BY_RIDER_KIND must cover every "
    "RIDER_KINDS member — adding a rider kind without deciding its "
    "not-applicable fields must fail at import, never at load time"
)

# The run-state DIMENSIONS gate resolution may reference, named as
# `hbuilder.domain.gates.RunState`'s two int fields (pinned by tests/test_schema.py).
# Numeric, not booleans: real cards carry different LIFE thresholds (600 and 1000),
# so the threshold is a per-skill parameter (the condition's `count`) and the run
# state carries the value it is compared against.
RUN_STATE_DIMENSIONS: tuple[str, ...] = ("life", "combo")

# The comparison operators a run_state gate may carry. "gte" is the default;
# "lte" is for the game's LIFE_LTE=11 triggers (thresholds 500/700/950), which fire
# when LIFE is at or BELOW the threshold. Forbidden on every other condition kind,
# so a stray operator fails at load instead of being ignored.
CONDITION_OPERATORS: tuple[str, ...] = ("gte", "lte")

STAT_NAMES: tuple[str, ...] = ("performance", "technique", "sense")

# The six skill families; one slot carries exactly one family.
SKILL_FAMILIES: tuple[str, ...] = (
    "score_support",
    "conditional_score_up",
    "stat_up",
    "activation_rate_up",
    "life_recovery",
    "judgment_enhancement",
)

# Which `effect_type` values each family may carry. `stat_up` names one stat
# (never the stat sum), or `all_stats`: the game's `all_parameter_up` ("All Stats
# UP"), on many PASSIVE and outfit slots. It is an effect_type of `stat_up`, not a
# seventh family (which would need an invented `family_weights` default), and not
# named "all", which is the `TARGET_KINDS` "every card" marker.
EFFECT_TYPES_BY_FAMILY: dict[str, frozenset[str]] = {
    "score_support": frozenset({"score"}),
    "conditional_score_up": frozenset({"score"}),
    "stat_up": frozenset({"performance", "technique", "sense", "all_stats"}),
    "activation_rate_up": frozenset({"activation_rate"}),
    "life_recovery": frozenset({"life"}),
    "judgment_enhancement": frozenset({"judgment"}),
}

# Fields that do not exist for a slot kind. Populating one would route a
# gauge-fired (SPECIAL) or always-on (PASSIVE/COSTUME) skill through the ACTIVE
# formula, so it fails at load time.
NOT_APPLICABLE_FIELDS_BY_SLOT_KIND: dict[str, frozenset[str]] = {
    "ACTIVE": frozenset(),
    "SPECIAL": frozenset({"interval", "probability_tier"}),
    "PASSIVE": frozenset({"duration", "interval", "probability_tier"}),
    COSTUME_SLOT_KIND: frozenset({"duration", "interval", "probability_tier"}),
}

# The same rule keyed on condition kind: an ungated condition has no dimension to
# target and no threshold, so its `target` and `count` must be absent.
NOT_APPLICABLE_CONDITION_FIELDS_BY_CONDITION_KIND: dict[str, frozenset[str]] = {
    "type": frozenset(),
    "generation": frozenset(),
    "run_state": frozenset(),
    UNGATED_CONDITION_KIND: frozenset({"target", "count"}),
}

assert set(NOT_APPLICABLE_CONDITION_FIELDS_BY_CONDITION_KIND) == set(CONDITION_KINDS) | {
    UNGATED_CONDITION_KIND
}, (
    "NOT_APPLICABLE_CONDITION_FIELDS_BY_CONDITION_KIND must cover every "
    "CONDITION_KINDS member plus UNGATED_CONDITION_KIND — adding a "
    "condition kind without deciding its not-applicable fields must fail "
    "at import, never at load time"
)

# The same rule keyed on target kind: "self" (the source card) and "all" (every
# card) have no `value` or `count`; "type" and "generation" require both.
NOT_APPLICABLE_TARGET_FIELDS_BY_TARGET_KIND: dict[str, frozenset[str]] = {
    "self": frozenset({"value", "count"}),
    TEAM_WIDE_TARGET_KIND: frozenset({"value", "count"}),
    "type": frozenset(),
    "generation": frozenset(),
}

assert set(NOT_APPLICABLE_TARGET_FIELDS_BY_TARGET_KIND) == set(TARGET_KINDS), (
    "NOT_APPLICABLE_TARGET_FIELDS_BY_TARGET_KIND must cover every "
    "TARGET_KINDS member — adding a target kind without deciding its "
    "not-applicable fields must fail at import, never at load time"
)

# Keeps a family's formula paired with the slot kind its inputs assume. It
# constrains a slot's PRINCIPAL family only (`record["family"]`), never
# `rider.family`: a rider is never dispatched through the principal's formula and
# is scored by its own fold.
#
# `activation_rate_up` is tied to ACTIVE because its fold discounts it by uptime
# and proc chance, the ACTIVE formula's inputs. In the game's data it occurs only
# as a RIDER on SPECIAL slots; the ACTIVE-principal path is exercised by the
# synthetic fixture `fubuki_01` in `data/cards.json`, which the tests depend on.
FAMILY_REQUIRES_SLOT_KIND: dict[str, str] = {
    "activation_rate_up": "ACTIVE",
}

# A renamed family or slot kind fails at import instead of disabling this rule.
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

    `card_id` and `field` are attached whenever known, and `str()` appends them
    to the message.
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
            f"schema_version: unsupported value {doc['schema_version']!r}, expected {SCHEMA_VERSION!r}"
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
            f"schema_version: unsupported value {doc['schema_version']!r}, expected {SCHEMA_VERSION!r}"
        )

    if "owned" not in doc:
        errors.append("owned: missing required key")
    elif not isinstance(doc["owned"], list):
        errors.append("owned: must be a JSON array")
    elif not all(isinstance(entry, str) for entry in doc["owned"]):
        errors.append("owned: every entry must be a string card id")

    return errors


def validate_skill_slot(record: object, card_id: str, slot_kind: str) -> list[str]:
    """Validate one skill-slot (or costume_skill) value against the slot contract.

    `record` may be JSON `null`: the card has no skill in this slot, which is
    distinct from a populated slot whose fields are `null` (unknown). A populated
    slot must carry every field in `_SKILL_SLOT_REQUIRED_FIELDS`; each missing one
    is its own error.

    Returns error strings naming `card_id`, `slot_kind` and the field path
    (`condition.count` for a nested field). Reports only: never clamps, rounds or
    normalizes a value.
    """
    if record is None:
        return []
    if not isinstance(record, dict):
        return [f"skills.{slot_kind}: card '{card_id}' {slot_kind} slot must be a JSON object or null"]

    errors: list[str] = []
    for field in _SKILL_SLOT_REQUIRED_FIELDS:
        if field not in record:
            errors.append(f"{field}: card '{card_id}' {slot_kind} slot is missing required field '{field}'")

    not_applicable = NOT_APPLICABLE_FIELDS_BY_SLOT_KIND.get(slot_kind, frozenset())

    # the slot's PRINCIPAL family, never a rider's (FAMILY_REQUIRES_SLOT_KIND)
    principal_family = record.get("family")
    if "family" in record and principal_family not in SKILL_FAMILIES:
        errors.append(
            f"family: card '{card_id}' {slot_kind} slot family "
            f"{principal_family!r} is not one of {list(SKILL_FAMILIES)}"
        )

    required_slot_kind = FAMILY_REQUIRES_SLOT_KIND.get(principal_family)  # type: ignore[arg-type]
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
                f"{field}: card '{card_id}' {slot_kind} slot {field} must be non-negative, got {value}"
            )
        elif field == "interval" and value == 0:
            errors.append(
                f"interval: card '{card_id}' {slot_kind} slot interval must "
                f"be greater than 0 when known — the scorer divides by it, got 0"
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
    """The `condition` (gate) validation rule, shared by the slot, its override and
    its rider. `field` is the dotted path errors are reported under (`"condition"`,
    `"override.condition"` or `"rider.condition"`)."""
    errors: list[str] = []
    if condition is None:
        # null is UNKNOWN, not "ungated": valid, and not validated further; it
        # taints the card via Card.unknown_fields()
        pass
    elif not isinstance(condition, dict):
        errors.append(f"{field}: card '{card_id}' {slot_kind} slot {field} must be a JSON object or null")
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
            # Card text often puts the gate in the second sentence ("For 9s, Score
            # UP 50%. ... When LIFE is 600 or higher, Score UP 100%."); requiring
            # target/count to be absent turns a gate typed as "ungated" plus a
            # target/count into a load error instead of a silent overstatement.
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
                errors.append(f"{field}.count: card '{card_id}' {slot_kind} slot {field}.count is missing")
            elif isinstance(count, bool) or not isinstance(count, int) or count < 1:
                errors.append(
                    f"{field}.count: card '{card_id}' {slot_kind} slot "
                    f"{field}.count must be an integer >= 1, got "
                    f"{count!r}"
                )
            # no `count != 1` pin for run_state: `count` is the numeric threshold
            # ("LIFE >= 600" is count=600); the generic `count >= 1` still applies

            # `operator` (gte|lte) is optional and meaningful only on a run_state
            # gate (CONDITION_OPERATORS); anywhere else its presence is an error
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


def _validate_target(target: object, card_id: str, slot_kind: str, *, field: str = "target") -> list[str]:
    """The effect-target validation rule (independent of the gate: a slot can carry
    either, both or neither).

    `None` (JSON null) is valid and not validated further: it is unknown and taints
    the card via `Card.unknown_fields()`. `field` is the dotted path errors are
    reported under (`"target"` or `"rider.target"`)."""
    errors: list[str] = []
    if target is None:
        pass
    elif not isinstance(target, dict):
        errors.append(f"{field}: card '{card_id}' {slot_kind} slot {field} must be a JSON object or null")
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
                # "self" / "all": no dimension to target, no count to select
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
                        f"{field}.count: card '{card_id}' {slot_kind} slot {field}.count is missing"
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
    """The `override` validation rule: unknown (`None`, taints) / absent
    (`{"kind": "none"}`) / populated, for a slot's optional gated override of its
    own base magnitude (no `family`/`effect_type`, `OVERRIDE_KINDS`).

    A populated override's `condition` must be present and gated: `null` and
    `{"kind": "ungated"}` are rejected, because an override with no gate is simply
    a different base magnitude."""
    errors: list[str] = []
    if override is None:
        pass
    elif not isinstance(override, dict):
        errors.append(f"override: card '{card_id}' {slot_kind} slot override must be a JSON object or null")
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
                # a "none" override rejects a stray `family`/`effect_type` too
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
                # no `family`/`effect_type` of its own (OVERRIDE_KINDS)
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
                        f"override.magnitude: card '{card_id}' {slot_kind} slot override.magnitude is missing"
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
                        f"override.condition: card '{card_id}' {slot_kind} slot override.condition is missing"
                    )
                else:
                    condition = override["condition"]
                    if condition is None or (
                        isinstance(condition, dict) and condition.get("kind") == UNGATED_CONDITION_KIND
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
    """The `rider` validation rule: a slot's optional second, differently-typed
    effect. Same three-state contract as `override`; a populated
    `{"kind": "rider", ...}` carries its own `family`/`effect_type`/`magnitude`/
    `condition`/`target`. `FAMILY_REQUIRES_SLOT_KIND` does not apply to a rider."""
    errors: list[str] = []
    if rider is None:
        pass
    elif not isinstance(rider, dict):
        errors.append(f"rider: card '{card_id}' {slot_kind} slot rider must be a JSON object or null")
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
                        f"rider.effect_type: card '{card_id}' {slot_kind} slot rider.effect_type is missing"
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
                        f"rider.magnitude: card '{card_id}' {slot_kind} slot rider.magnitude is missing"
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
                        f"rider.condition: card '{card_id}' {slot_kind} slot rider.condition is missing"
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
                    errors.append(f"rider.target: card '{card_id}' {slot_kind} slot rider.target is missing")
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
            errors.append(f"{field}: card '{card_id}' is missing required field '{field}'")

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
                f"type: card '{card_id}' type {record['type']!r} is not one of {sorted(CARD_TYPES)}"
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
                    errors.append(f"stats.{stat_name}: card '{card_id}' stats is missing '{stat_name}'")
                elif not isinstance(stats[stat_name], int) or isinstance(stats[stat_name], bool):
                    errors.append(f"stats.{stat_name}: card '{card_id}' stats.{stat_name} must be an int")

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
                errors.extend(validate_skill_slot(skills[slot_kind], card_id, slot_kind))

    if "costume_skill" in record:
        errors.extend(validate_skill_slot(record["costume_skill"], card_id, COSTUME_SLOT_KIND))

    return errors
