"""Frozen, slotted, hashable value objects for cards. Pure, no I/O.

`skill_slot_from_record` / `card_from_record` assume the record already
passed `domain.schema.validate_card` — they do not re-validate.
"""

from __future__ import annotations

from dataclasses import dataclass

from hbuilder.domain.schema import (
    COSTUME_SLOT_KIND,
    NOT_APPLICABLE_FIELDS_BY_SLOT_KIND,
    SECOND_EFFECT_ABSENT_KIND,
    SKILL_SLOT_KINDS,
    TEAM_WIDE_TARGET_KIND,
    UNGATED_CONDITION_KIND,
)
from hbuilder.domain.unknown import UNKNOWN, is_known

# `condition` is an UNKNOWN-routed slot field like the others — a null
# condition on disk taints the card exactly like a null
# duration/interval/magnitude does, rather than meaning "ungated".
#
# `target` follows `condition`, so an untranscribed target reports as
# `skills.PASSIVE.target` — the same three-state contract.
#
# `override` and `rider` come last, so an untranscribed second-effect
# payload taints the card (and therefore the team-level `>=` marker via
# `team_taint()`) rather than silently collapsing into "there is no second
# effect". Only `override` additionally forces the individual skill's own
# contribution to `data_missing` — it is in `scoring.py`'s
# `_REQUIRED_KNOWN_FIELDS`, which is the only list that drives a
# per-skill `data_missing` state. `rider` is deliberately NOT in that
# list: a rider does not feed the slot's principal contribution, so an
# unknown rider cannot make that contribution wrong either way; the
# card-level taint above is what still surfaces the `>=` marker
# correctly. Do not "fix" this by adding `rider` to
# `_REQUIRED_KNOWN_FIELDS` — that would force every card with an
# unknown-but-being-transcribed rider into a fully-zeroed principal
# contribution for no scoring-correctness reason.
_UNKNOWN_CAPABLE_SLOT_FIELDS: tuple[str, ...] = (
    "duration",
    "interval",
    "probability_tier",
    "magnitude",
    "condition",
    "target",
    "override",
    "rider",
)


@dataclass(frozen=True, slots=True)
class Stats:
    performance: int
    technique: int
    sense: int

    def total(self) -> int:
        return self.performance + self.technique + self.sense


@dataclass(frozen=True, slots=True)
class GateCondition:
    kind: str  # "type" | "generation" | "run_state"
    target: str
    count: int
    # The comparison direction, meaningful only for a `run_state` gate:
    # "gte" (LIFE/COMBO at or ABOVE the threshold, the game's *_GTE triggers)
    # or "lte" (LIFE at or BELOW it — the LIFE_LTE=11 comeback/desperation
    # triggers, thresholds 500/700/950). Defaults to "gte", so a record that
    # omits it reads as an at-or-above gate. `type`/`generation` are
    # set-membership counts and are always "gte"; the schema forbids an
    # operator on them.
    operator: str = "gte"


@dataclass(frozen=True, slots=True)
class SlotOverride:
    """A slot's base magnitude plus a gated override of that same magnitude — e.g.
    matsuri "Sweet Cheeky Stage" ACTIVE: "Score UP 35%. ... With 2+ Pure ->
    Score UP 70%." When the override's own gate fires, `magnitude`
    REPLACES the slot's base `magnitude` (`scoring._effective_magnitude`);
    the two are never summed. The override carries no `family`/
    `effect_type` of its own: by definition it overrides the principal's
    magnitude, and a second copy of the family could only ever disagree
    with the first (see `tests/test_schema.py::
    test_a_slots_override_carries_no_family_or_effect_type_of_its_own`)."""

    magnitude: float
    condition: GateCondition


@dataclass(frozen=True, slots=True)
class SlotRider:
    """A slot's second, differently-typed effect, retained rather than
    dropped — e.g. a 3-star SP carrying both `score_support` and
    `life_recovery`. Represented, but not scored, except for the outfit
    riders in `SCORED_COSTUME_RIDER_FAMILIES`.

    `condition`/`target` follow the identical three-state contract the
    slot's own `condition`/`target` carry — `None` is structurally
    ungated/team-wide, `UNKNOWN` taints, a populated value resolves to a
    `GateCondition`/`EffectTarget` — via the same
    `_condition_from_record`/`_target_from_record` converters, never a
    second copy of either."""

    family: str
    effect_type: str
    magnitude: float
    condition: object  # GateCondition | None | Unknown
    target: object  # EffectTarget | None | Unknown


@dataclass(frozen=True, slots=True)
class EffectTarget:
    """The effect-target clause — "who receives this effect" —
    structurally independent of `GateCondition` ("what unlocks this
    effect"). `value` rather than `target`, because `GateCondition.target`
    already maps to `GatePredicate.value` at the predicate layer, and
    `target.target` is unreadable. `value`/`count` are `None` for a "self"
    or "all" kind (structurally absent on disk — see
    `schema.NOT_APPLICABLE_TARGET_FIELDS_BY_TARGET_KIND`), populated for
    "type"/"generation"."""

    kind: str  # one of schema.TARGET_KINDS
    value: str | None
    count: int | None


@dataclass(frozen=True, slots=True)
class SkillSlot:
    kind: str  # one of SKILL_SLOT_KINDS, or COSTUME_SLOT_KIND
    family: str
    effect_type: str
    duration: object  # int | float | Unknown
    interval: object  # int | float | Unknown
    probability_tier: object  # str | Unknown
    magnitude: object  # int | float | Unknown
    condition: object  # GateCondition | None | Unknown
    target: object  # EffectTarget | None | Unknown
    override: object = None  # SlotOverride | None | Unknown
    rider: object = None  # SlotRider | None | Unknown


# The rider families a leader OUTFIT scores as a second effect. 11 of the
# outfits in `data/cards.json` carry a
# `score_support` rider (effect_type `score`, magnitude 25, target all, gated
# on 2+ of a type — e.g. Mori Calliope "Radiant Summer Heatwave"). It is
# always-on and team-wide like any costume effect, so it is scored exactly as
# a PRINCIPAL costume `score_support` is — via a synthesized COSTUME slot
# (`scored_costume_rider_slot`) that carries the rider's own condition/target
# and is resolved against the five members like the outfit itself
# (`gates.resolve_leader_costume_skills`). Any other rider family on a costume
# stays represented and unscored.
SCORED_COSTUME_RIDER_FAMILIES: tuple[str, ...] = ("score_support",)


def scored_costume_rider_slot(costume: "SkillSlot") -> "SkillSlot | None":
    """The synthesized COSTUME `SkillSlot` for a costume slot's scored rider,
    or `None` when the costume carries no rider of a
    `SCORED_COSTUME_RIDER_FAMILIES` family (absent, `UNKNOWN`, or another
    family). The slot's `condition`/`target` are the RIDER'S own — three-state
    exactly as the rider carries them — and it has no override and no rider of
    its own; `duration`/`interval`/`probability_tier` are copied from the host
    (structurally not-applicable to a COSTUME slot either way)."""
    rider = costume.rider
    if not isinstance(rider, SlotRider) or rider.family not in SCORED_COSTUME_RIDER_FAMILIES:
        return None
    return SkillSlot(
        kind=COSTUME_SLOT_KIND,
        family=rider.family,
        effect_type=rider.effect_type,
        duration=costume.duration,
        interval=costume.interval,
        probability_tier=costume.probability_tier,
        magnitude=rider.magnitude,
        condition=rider.condition,
        target=rider.target,
        override=None,
        rider=None,
    )


@dataclass(frozen=True, slots=True)
class Card:
    id: str
    member: str
    generations: frozenset[str]
    type: str
    rarity: str
    costume_name: str
    stats: Stats
    skills: tuple[SkillSlot | None, ...]
    costume_skill: SkillSlot | None

    def stat_sum(self) -> int:
        return self.stats.total()

    def stat(self, name: str) -> int:
        return getattr(self.stats, name)

    def skill(self, kind: str) -> SkillSlot | None:
        index = SKILL_SLOT_KINDS.index(kind)
        return self.skills[index]

    def unknown_fields(self) -> tuple[str, ...]:
        """Dotted paths (e.g. `skills.ACTIVE.magnitude`) for every field
        currently holding `UNKNOWN`, in a deterministic order (slot kind in
        `SKILL_SLOT_KINDS` order, then `costume_skill`; within a slot, the
        field order of `_UNKNOWN_CAPABLE_SLOT_FIELDS`).

        A field named in `NOT_APPLICABLE_FIELDS_BY_SLOT_KIND[slot.kind]` for
        its slot's kind is never reported — it structurally does not exist
        for that slot kind, so it is not a data gap.
        """
        fields: list[str] = []
        for slot_kind, slot in zip(SKILL_SLOT_KINDS, self.skills):
            if slot is None:
                continue
            fields.extend(_unknown_slot_fields(slot, f"skills.{slot_kind}"))
        if self.costume_skill is not None:
            fields.extend(_unknown_slot_fields(self.costume_skill, "costume_skill"))
        return tuple(fields)


def _coerce_unknown(value: object) -> object:
    return UNKNOWN if value is None else value


def _unknown_slot_fields(slot: SkillSlot, prefix: str) -> list[str]:
    not_applicable = NOT_APPLICABLE_FIELDS_BY_SLOT_KIND.get(slot.kind, frozenset())
    result: list[str] = []
    for field in _UNKNOWN_CAPABLE_SLOT_FIELDS:
        if field in not_applicable:
            continue
        if not is_known(getattr(slot, field)):
            result.append(f"{prefix}.{field}")
    return result


def _condition_from_record(record: dict | None) -> object:
    """The three-state condition contract: JSON `null` -> `UNKNOWN` (the
    transcriber has not recorded whether this skill is gated, so it taints
    via `Card.unknown_fields()`); `{"kind": "ungated"}` -> `None`
    (structurally ungated — an affirmative marker typed by the transcriber,
    no predicate to build); any other populated dict -> a `GateCondition`."""
    if record is None:
        return UNKNOWN
    if record.get("kind") == UNGATED_CONDITION_KIND:
        return None
    return GateCondition(
        kind=record["kind"],
        target=record["target"],
        count=record["count"],
        # Often absent, and structurally forbidden by the schema on
        # type/generation gates, so the "gte" default is the only value those
        # ever take. Only a run_state gate may carry it.
        operator=record.get("operator", "gte"),
    )


def _target_from_record(record: dict | None) -> object:
    """The three-state target contract, the same shape
    `_condition_from_record` uses: JSON `null` -> `UNKNOWN` (the
    transcriber has not recorded who this effect reaches, so it taints via
    `Card.unknown_fields()`); `{"kind": "all"}` -> `None` (structurally
    team-wide — an affirmative marker typed by the transcriber, no subset
    to select); any other populated dict -> an `EffectTarget`."""
    if record is None:
        return UNKNOWN
    if record.get("kind") == TEAM_WIDE_TARGET_KIND:
        return None
    return EffectTarget(
        kind=record["kind"],
        value=record.get("value"),
        count=record.get("count"),
    )


def _override_from_record(record: dict | None) -> object:
    """Mirrors `_condition_from_record`'s three-state contract:
    JSON `null` -> `UNKNOWN` (the transcriber has
    not recorded whether this slot carries an override at all, so it
    taints via `Card.unknown_fields()`); `{"kind": "none"}` -> `None`
    (structurally "no override" — an affirmative marker typed by the
    transcriber, no second payload); any other populated dict -> a
    `SlotOverride`, whose nested `condition` goes through
    `_condition_from_record` — validation already guarantees a populated
    override's condition is a real, gated `GateCondition`, never `None`/
    `UNKNOWN`."""
    if record is None:
        return UNKNOWN
    if record.get("kind") == SECOND_EFFECT_ABSENT_KIND:
        return None
    return SlotOverride(
        magnitude=record["magnitude"],
        condition=_condition_from_record(record["condition"]),
    )


def _rider_from_record(record: dict | None) -> object:
    """Mirrors `_condition_from_record`'s three-state contract:
    JSON `null` -> `UNKNOWN`; `{"kind": "none"}`
    -> `None`; any other populated dict -> a `SlotRider`, whose nested
    `condition`/`target` go through the
    `_condition_from_record`/`_target_from_record` three-state
    converters."""
    if record is None:
        return UNKNOWN
    if record.get("kind") == SECOND_EFFECT_ABSENT_KIND:
        return None
    return SlotRider(
        family=record["family"],
        effect_type=record["effect_type"],
        magnitude=record["magnitude"],
        condition=_condition_from_record(record.get("condition")),
        target=_target_from_record(record.get("target")),
    )


def skill_slot_from_record(record: dict | None, kind: str) -> SkillSlot | None:
    """Build a `SkillSlot` from an already-validated skill-slot record.

    `record` may be `None` (JSON `null`) meaning the card has no skill in
    this slot at all — that is distinct from a populated slot whose
    `duration`/`interval`/`probability_tier`/`magnitude` are individually
    `null`, which become the `UNKNOWN` sentinel.
    """
    if record is None:
        return None
    return SkillSlot(
        kind=kind,
        family=record["family"],
        effect_type=record["effect_type"],
        duration=_coerce_unknown(record["duration"]),
        interval=_coerce_unknown(record["interval"]),
        probability_tier=_coerce_unknown(record["probability_tier"]),
        magnitude=_coerce_unknown(record["magnitude"]),
        condition=_condition_from_record(record.get("condition")),
        target=_target_from_record(record.get("target")),
        override=_override_from_record(record.get("override")),
        rider=_rider_from_record(record.get("rider")),
    )


def card_from_record(record: dict) -> Card:
    """Build a `Card` from an already-validated cards.json record."""
    skills = tuple(
        skill_slot_from_record(record["skills"][kind], kind) for kind in SKILL_SLOT_KINDS
    )
    costume_skill = skill_slot_from_record(record["costume_skill"], COSTUME_SLOT_KIND)
    stats_record = record["stats"]
    stats = Stats(
        performance=stats_record["performance"],
        technique=stats_record["technique"],
        sense=stats_record["sense"],
    )
    return Card(
        id=record["id"],
        member=record["member"],
        generations=frozenset(record["generation"]),
        type=record["type"],
        rarity=record["rarity"],
        costume_name=record["costume_name"],
        stats=stats,
        skills=skills,
        costume_skill=costume_skill,
    )
