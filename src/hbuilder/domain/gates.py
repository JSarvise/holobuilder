"""Team evaluation stages one and two: aggregate, then resolve gates. Stage three (scoring)
is `scoring.py`; this module computes no scores.

Gate conditions resolve against a `TeamAggregates` snapshot of the assembled member set,
never against a card in isolation, plus a `RunState` snapshot for play-through gates
("with a Combo of 100 or more") that team counts cannot answer. The counts do not depend
on who leads, so `resolve_card_set_gates(cards, run_state)` is the one leader-agnostic
resolution loop and `resolve_team_gates(team, run_state)` wraps it."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from hbuilder.domain.card import (
    Card,
    GateCondition,
    SkillSlot,
    SlotRider,
    scored_costume_rider_slot,
)
from hbuilder.domain.schema import SKILL_SLOT_KINDS
from hbuilder.domain.team import Team
from hbuilder.domain.unknown import is_known


@dataclass(frozen=True, slots=True)
class TeamAggregates:
    type_counts: dict
    generation_counts: dict

    def type_count(self, value: str) -> int:
        return self.type_counts.get(value, 0)

    def generation_count(self, value: str) -> int:
        return self.generation_counts.get(value, 0)


def aggregate(cards: Iterable[Card]) -> TeamAggregates:
    """Type and generation counts over `cards` in one pass; a dual-generation card counts
    once for each of its generations."""
    type_counts: dict[str, int] = {}
    generation_counts: dict[str, int] = {}
    for card in cards:
        type_counts[card.type] = type_counts.get(card.type, 0) + 1
        for generation in card.generations:
            generation_counts[generation] = generation_counts.get(generation, 0) + 1
    return TeamAggregates(type_counts=type_counts, generation_counts=generation_counts)


@dataclass(frozen=True, slots=True)
class RunState:
    """Team-independent play-through state for `run_state` gates; field names match
    `schema.RUN_STATE_DIMENSIONS`.

    Values are numeric, not flags: a gate carries its own threshold (`GatePredicate
    .threshold`) and compares against this value, since cards use more than one threshold
    per dimension (LIFE 600 and 1000). `life` is the assumed LIFE; under perfect play it
    stays at its 1000 maximum."""

    life: int
    combo: int


@dataclass(frozen=True, slots=True)
class GatePredicate:
    dimension: str  # "type" | "generation" | "run_state"
    value: str
    threshold: int
    # "gte" (actual >= threshold) or "lte" (actual <= threshold). Only a run_state gate
    # is ever "lte" (LIFE_LTE=11 comeback triggers).
    operator: str = "gte"

    def resolve(self, agg: TeamAggregates, run_state: RunState) -> GateResolution:
        if self.dimension == "type":
            actual = agg.type_count(self.value)
        elif self.dimension == "generation":
            actual = agg.generation_count(self.value)
        else:
            # The run-state value itself, compared against this predicate's threshold.
            actual = getattr(run_state, self.value)
        # Perfect-play life=1000 satisfies `>= 950` but must fail `<= 950`.
        fired = actual <= self.threshold if self.operator == "lte" else actual >= self.threshold
        return GateResolution(predicate=self, fired=fired, actual_count=actual)


@dataclass(frozen=True, slots=True)
class GateResolution:
    predicate: GatePredicate
    fired: bool
    actual_count: int


def predicate_from_condition(condition: GateCondition) -> GatePredicate:
    """Map an on-disk `condition` to a predicate: `kind` -> `dimension`, `target` -> `value`,
    `count` -> `threshold`."""
    return GatePredicate(
        dimension=condition.kind,
        value=condition.target,
        threshold=condition.count,
        operator=condition.operator,
    )


@dataclass(frozen=True, slots=True)
class ResolvedSkill:
    card_id: str
    # The HOLOMEM, not the card: the Holomem Board is per character and reaches every card
    # of theirs, so scoring needs no card index to find whose board applies.
    member: str
    slot_kind: str
    source: str
    family: str
    skill: SkillSlot
    gate: GateResolution | None
    override_gate: GateResolution | None
    # The rider's own gate (e.g. LIFE>=1000 on an activation_rate_up rider), resolved
    # once per card set like `override_gate`. Defaults to None (no rider, or ungated).
    rider_gate: GateResolution | None = None


@dataclass(frozen=True, slots=True)
class CardSetGates:
    """Leader-agnostic resolution of a card set: one `TeamAggregates` snapshot, each card's
    normal-slot `ResolvedSkill`s in `SKILL_SLOT_KINDS` order (`normal_by_card_id`) and its
    costume PRINCIPAL or `None` (`costume_by_card_id`). An outfit's scored `score_support`
    rider is not here: it applies only to the leader's outfit and is read through
    `resolve_leader_costume_skills`."""

    aggregates: TeamAggregates
    normal_by_card_id: dict
    costume_by_card_id: dict


def _resolve_one_skill(
    card_id: str,
    member: str,
    slot_kind: str,
    source: str,
    skill: SkillSlot,
    agg: TeamAggregates,
    run_state: RunState,
) -> ResolvedSkill:
    """Resolve one skill slot's gates (principal condition, override, rider) against the
    snapshots.

    An `UNKNOWN` condition and an ungated (`None`) one both give `gate = None`, because an
    `UNKNOWN` must never reach `predicate_from_condition`. So `gate = None` here means "no
    gate resolved", not "ungated": an ungated skill contributes in full while an unrecorded
    one must contribute nothing. `scoring._REQUIRED_KNOWN_FIELDS` (which lists `condition`
    first) tells them apart for both scoring paths;
    `tests/test_unknown_condition_is_data_missing.py` pins it. The override and rider gates
    follow the same three-state rule and, like the principal's, depend only on
    `(aggregates, run_state)`, so they are leader-invariant."""
    condition = skill.condition
    gate = None
    if is_known(condition) and condition is not None:
        predicate = predicate_from_condition(condition)  # type: ignore[arg-type]
        gate = predicate.resolve(agg, run_state)

    override = skill.override
    override_gate = None
    if is_known(override) and override is not None:
        override_predicate = predicate_from_condition(override.condition)  # type: ignore[attr-defined]
        override_gate = override_predicate.resolve(agg, run_state)

    # The rider's own condition, three-state like the override's. Only a populated
    # `SlotRider` has one; the absent marker (`rider.kind == "none"`) is not a `SlotRider`.
    rider = skill.rider
    rider_gate = None
    if isinstance(rider, SlotRider):
        rider_condition = rider.condition
        if is_known(rider_condition) and rider_condition is not None:
            rider_gate = predicate_from_condition(rider_condition).resolve(agg, run_state)  # type: ignore[arg-type]

    return ResolvedSkill(
        card_id=card_id,
        member=member,
        slot_kind=slot_kind,
        source=source,
        family=skill.family,
        skill=skill,
        gate=gate,
        override_gate=override_gate,
        rider_gate=rider_gate,
    )


def resolve_card_set_gates(cards: tuple[Card, ...], run_state: RunState) -> CardSetGates:
    """The leader-agnostic resolver, the only resolution loop in this module: aggregate `cards`
    once, then resolve every card's normal slots and costume slot against that snapshot and
    `run_state`. Every costume is resolved, though one at most is used per arrangement: its
    gate does not depend on who leads.

    `source` is `"normal"` or `"costume"`; `slot_kind` is the `SKILL_SLOT_KINDS` member or the
    lowercase `"costume"` (not the uppercase `schema.COSTUME_SLOT_KIND`), which flows into
    `SkillContribution.slot_kind`."""
    agg = aggregate(cards)
    type_counts = agg.type_counts
    generation_counts = agg.generation_counts
    normal_by_card_id: dict = {}
    costume_by_card_id: dict = {}

    for card in cards:
        # Per-card table: the resolved slots depend only on `run_state` and the few counts
        # the card's conditions read (`_gate_probes`), so each reading resolves once.
        table = _card_gate_tables.get(id(card))
        if table is None or table[0] is not card:
            if len(_card_gate_tables) > 100_000:
                _card_gate_tables.clear()
            table = (card, _gate_probes(card), {})
            _card_gate_tables[id(card)] = table
        reading = (
            run_state,
            *[
                (type_counts if dimension == "type" else generation_counts).get(value, 0)
                for dimension, value in table[1]
            ],
        )
        resolved = table[2].get(reading)
        if resolved is None:
            normal_resolved: list[ResolvedSkill] = []
            for slot_kind in SKILL_SLOT_KINDS:
                skill = card.skill(slot_kind)
                if skill is None:
                    continue
                normal_resolved.append(
                    _resolve_one_skill(card.id, card.member, slot_kind, "normal", skill, agg, run_state)
                )
            costume = None
            if card.costume_skill is not None:
                costume = _resolve_one_skill(
                    card.id, card.member, "costume", "costume", card.costume_skill, agg, run_state
                )
            resolved = (tuple(normal_resolved), costume)
            table[2][reading] = resolved
        normal_by_card_id[card.id], costume_by_card_id[card.id] = resolved

    return CardSetGates(
        aggregates=agg,
        normal_by_card_id=normal_by_card_id,
        costume_by_card_id=costume_by_card_id,
    )


def resolve_leader_costume(
    leader: Card, aggregates: TeamAggregates, run_state: RunState
) -> ResolvedSkill | None:
    """The leader's Outfit Skill principal resolved against the MEMBERS' aggregates, or `None`
    without a costume slot. The leader may be outside the member set, and its own card only
    counts if it is one of the five, so a gated outfit cannot satisfy its own condition."""
    if leader.costume_skill is None:
        return None
    return _resolve_one_skill(
        leader.id, leader.member, "costume", "costume", leader.costume_skill, aggregates, run_state
    )


# The `source` of a leader outfit's scored rider. Its `slot_kind` is the principal's
# `"costume"`, so `source` alone tells them apart (also in `score_card_set`'s cache key).
COSTUME_RIDER_SOURCE = "costume_rider"


def resolve_leader_costume_skills(
    leader: Card, aggregates: TeamAggregates, run_state: RunState
) -> tuple[ResolvedSkill, ...]:
    """Every scored effect of the leader's outfit, in emission order: the principal
    (`resolve_leader_costume`), then its `score_support` RIDER (source
    `COSTUME_RIDER_SOURCE`, a COSTUME slot synthesised by `card.scored_costume_rider_slot`);
    empty without an outfit.

    The rider is resolved against the same members' aggregates with its OWN condition (e.g.
    Radiant Summer Heatwave: sense +80 % on 2+ Cute and, separately, Score Support 25 % on
    2+ Cute). Every reader of "the leader's outfit" (`score_card_set`,
    `song.score_song_detailed`, `resolve_team_gates`) iterates this tuple."""
    principal = resolve_leader_costume(leader, aggregates, run_state)
    if principal is None:
        return ()
    rider_slot = scored_costume_rider_slot(leader.costume_skill)  # type: ignore[arg-type]
    if rider_slot is None:
        return (principal,)
    rider = _resolve_one_skill(
        leader.id, leader.member, "costume", COSTUME_RIDER_SOURCE, rider_slot, aggregates, run_state
    )
    return (principal, rider)


def resolve_team_gates(team: Team, run_state: RunState) -> tuple[ResolvedSkill, ...]:
    """A team's resolved skills in scoring order (also the order of
    `ScoreBreakdown.contributions`): the leader's outfit effects, then each member's normal
    slots in member order, `SKILL_SLOT_KINDS` order within a member.

    Gates resolve over the 5 MEMBERS only. The leader's own normal slots are not emitted; if
    its card is also a member they arrive once, through the member loop."""
    card_set_gates = resolve_card_set_gates(team.members, run_state)
    resolved: list[ResolvedSkill] = []
    resolved.extend(resolve_leader_costume_skills(team.leader, card_set_gates.aggregates, run_state))
    for member in team.members:
        resolved.extend(card_set_gates.normal_by_card_id[member.id])
    return tuple(resolved)


# id(card) -> (card, its gate probes, {reading: (normal slots, costume slot)}). The card
# is kept in the entry so a recycled `id()` cannot serve another card's table.
_card_gate_tables: dict[int, tuple] = {}


def _gate_probes(card: Card) -> tuple[tuple[str, str], ...]:
    """Every `(dimension, value)` team count this card's gates read (normal and costume slots:
    principal, override, rider).

    Must list at least what `_resolve_one_skill` reads: a `GateResolution` carries the count,
    so two teams agreeing on every probe and on `run_state` resolve this card to equal
    records. A condition this cannot read is not probed; `_resolve_one_skill` then raises on
    the table miss."""
    probes: list[tuple[str, str]] = []

    def probe(condition: object) -> None:
        if is_known(condition) and condition is not None:
            predicate = predicate_from_condition(condition)  # type: ignore[arg-type]
            if predicate.dimension in ("type", "generation"):
                probes.append((predicate.dimension, predicate.value))

    for skill in (*(card.skill(kind) for kind in SKILL_SLOT_KINDS), card.costume_skill):
        if skill is None:
            continue
        probe(skill.condition)
        override = skill.override
        if is_known(override) and override is not None:
            probe(getattr(override, "condition", None))
        rider = skill.rider
        if isinstance(rider, SlotRider):
            probe(rider.condition)
    return tuple(probes)
