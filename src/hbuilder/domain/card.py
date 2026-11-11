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

# The slot fields that may hold UNKNOWN, in report order. A null `condition` or
# `target` taints the card like a null duration/magnitude (it never means
# "ungated" / "team-wide"), and so does an untranscribed `override` or `rider`
# (and with it the team-level `>=` marker via `team_taint()`).
# Only `override` also zeroes the skill's own contribution (`scoring.py`'s
# `_REQUIRED_KNOWN_FIELDS`): a rider does not feed the slot's principal
# contribution, so an unknown rider cannot make it wrong, and listing `rider`
# there would zero every such card's principal for nothing.
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
    # The comparison direction, meaningful only for a `run_state` gate: "gte"
    # (at or ABOVE the threshold, the game's *_GTE triggers) or "lte" (LIFE at or
    # BELOW it, the LIFE_LTE=11 triggers). `type`/`generation` gates are always "gte".
    operator: str = "gte"


@dataclass(frozen=True, slots=True)
class SlotOverride:
    """A slot's base magnitude plus a gated override of that same magnitude — e.g.
    matsuri "Sweet Cheeky Stage" ACTIVE: "Score UP 35%. ... With 2+ Pure ->
    Score UP 70%." When the override's own gate fires, the scorers read
    `max(override magnitude, base magnitude)` (`scoring._effective_magnitude`):
    the game holds both and takes the larger, so the two are never summed. It carries no `family`/`effect_type` of its own
    (`schema.OVERRIDE_KINDS`)."""

    magnitude: float
    condition: GateCondition


@dataclass(frozen=True, slots=True)
class SlotRider:
    """A slot's second, differently-typed effect, retained rather than
    dropped — e.g. a 3-star SP carrying both `score_support` and
    `life_recovery`. Represented, but not scored, except for the outfit
    riders in `SCORED_COSTUME_RIDER_FAMILIES`.

    `condition`/`target` follow the slot's own three-state contract: `None` is
    ungated/team-wide, `UNKNOWN` taints, a populated value is a
    `GateCondition`/`EffectTarget`."""

    family: str
    effect_type: str
    magnitude: float
    condition: object  # GateCondition | None | Unknown
    target: object  # EffectTarget | None | Unknown


@dataclass(frozen=True, slots=True)
class EffectTarget:
    """The effect-target clause, "who receives this effect", independent of
    `GateCondition` ("what unlocks it"). `value` (not `target`, as
    `GateCondition.target` maps to `GatePredicate.value`) and `count` are `None`
    for a "self" or "all" kind, populated for "type"/"generation"."""

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


# The rider families a leader OUTFIT scores as a second effect. Some outfits carry
# a `score_support` rider (e.g. Mori Calliope "Radiant Summer Heatwave": 25, target
# all, gated on 2+ of a type). Always-on and team-wide like any costume effect, it
# is scored as a PRINCIPAL costume `score_support` is, via a synthesized COSTUME
# slot (`scored_costume_rider_slot`) resolved against the five members
# (`gates.resolve_leader_costume_skills`). Other rider families stay unscored.
SCORED_COSTUME_RIDER_FAMILIES: tuple[str, ...] = ("score_support",)


def scored_costume_rider_slot(costume: SkillSlot) -> SkillSlot | None:
    """The synthesized COSTUME `SkillSlot` for a costume slot's scored rider, or
    `None` when the costume carries no rider of a `SCORED_COSTUME_RIDER_FAMILIES`
    family. Its `condition`/`target` are the rider's own; it has no override or
    rider; `duration`/`interval`/`probability_tier` are copied from the host (not
    applicable to a COSTUME slot either way)."""
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

        A field not applicable to the slot's kind
        (`NOT_APPLICABLE_FIELDS_BY_SLOT_KIND`) is never reported.
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
    """The three-state condition contract: JSON `null` -> `UNKNOWN` (taints via
    `Card.unknown_fields()`); `{"kind": "ungated"}` -> `None`; any other dict ->
    a `GateCondition`."""
    if record is None:
        return UNKNOWN
    if record.get("kind") == UNGATED_CONDITION_KIND:
        return None
    return GateCondition(
        kind=record["kind"],
        target=record["target"],
        count=record["count"],
        # only a run_state gate may carry it (the schema forbids it elsewhere)
        operator=record.get("operator", "gte"),
    )


def _target_from_record(record: dict | None) -> object:
    """The three-state target contract: JSON `null` -> `UNKNOWN`;
    `{"kind": "all"}` -> `None` (team-wide); any other dict -> an `EffectTarget`."""
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
    """The three-state override contract: JSON `null` -> `UNKNOWN`;
    `{"kind": "none"}` -> `None`; any other dict -> a `SlotOverride` (validation
    guarantees its condition is a real, gated `GateCondition`)."""
    if record is None:
        return UNKNOWN
    if record.get("kind") == SECOND_EFFECT_ABSENT_KIND:
        return None
    return SlotOverride(
        magnitude=record["magnitude"],
        condition=_condition_from_record(record["condition"]),  # type: ignore[arg-type]
    )


def _rider_from_record(record: dict | None) -> object:
    """The three-state rider contract: JSON `null` -> `UNKNOWN`;
    `{"kind": "none"}` -> `None`; any other dict -> a `SlotRider`."""
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

    `record` may be `None` (JSON `null`): the card has no skill in this slot,
    unlike a populated slot whose individual fields are `null` (`UNKNOWN`).
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
    skills = tuple(skill_slot_from_record(record["skills"][kind], kind) for kind in SKILL_SLOT_KINDS)
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
