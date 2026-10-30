"""The three-stage team evaluation, stages one and two: aggregate, then
resolve. Stage three (scoring) lives in `scoring.py` — this module computes
no scores.

This is the architectural seam the whole project rests on: gate conditions
must be resolved against a `TeamAggregates` snapshot built from the fully
assembled six-card team, never against cards in isolation.

`RunState` is a second, team-independent snapshot alongside
`TeamAggregates` — it answers run-state (play-through) gates such as "with
a Combo of 100 or more" that `TeamAggregates` structurally cannot answer,
since they are properties of the play-through, not the assembled team.
`GatePredicate.resolve` and `resolve_team_gates` take both snapshots. This
does not relax the no-`Card` invariant: `RunState` is not a `Card` and is
not per-card — gates still never resolve against a card in isolation.

The card-set view is primary, and the team view is a thin wrapper over
it. `TeamAggregates` is a function of the
6-card *set*, not the arrangement — type and generation counts do not
depend on who leads — so `resolve_card_set_gates(cards, run_state)` is the
one leader-agnostic resolution loop in this module, and
`resolve_team_gates(team, run_state)` delegates to it rather than
re-implementing pass 1. This does not relax the invariant above: gates
still resolve only against a `TeamAggregates` snapshot built from the fully
assembled six-card set, never against a card in isolation — only the noun
(`Team` vs. `tuple[Card, ...]`) the resolution loop is expressed against
differs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

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
    """One O(6) pass over the assembled team's cards: one Type counter
    increment and one counter increment per entry in `card.generations` —
    the dual-generation case falls out of iterating the frozenset and gets
    no branch of its own."""
    type_counts: dict[str, int] = {}
    generation_counts: dict[str, int] = {}
    for card in cards:
        type_counts[card.type] = type_counts.get(card.type, 0) + 1
        for generation in card.generations:
            generation_counts[generation] = generation_counts.get(generation, 0) + 1
    return TeamAggregates(type_counts=type_counts, generation_counts=generation_counts)


@dataclass(frozen=True, slots=True)
class RunState:
    """A team-independent snapshot of play-through state, read
    alongside `TeamAggregates` when resolving a `run_state` gate condition.
    Field names match `hbuilder.domain.schema.RUN_STATE_DIMENSIONS` element
    for element.

    These are NUMERIC values, not flags. A run-state gate carries its own
    threshold in `GatePredicate.threshold` (from the condition's `count`),
    and resolution compares this snapshot's value against it — exactly the
    way a type or generation count is compared. Two different LIFE
    thresholds are attested on real cards (600 and 1000) on different
    skills, which a per-dimension boolean cannot express at all.

    `life` is the LIFE value assumed during the run. Under the project's
    perfect-play premise LIFE never leaves its 1000 maximum, so every
    attested LIFE gate fires; the value is still carried numerically so a
    threshold above 1000 would be expressible without a schema change."""

    life: int
    combo: int


@dataclass(frozen=True, slots=True)
class GatePredicate:
    dimension: str  # "type" | "generation" | "run_state"
    value: str
    threshold: int
    # "gte" (actual >= threshold) or "lte" (actual <= threshold). Only a
    # run_state gate is ever "lte" — LIFE_LTE=11 comeback/desperation triggers;
    # type/generation counts are always "gte". The default keeps every
    # non-run_state gate at `>=`.
    operator: str = "gte"

    def resolve(self, agg: TeamAggregates, run_state: RunState) -> "GateResolution":
        if self.dimension == "type":
            actual = agg.type_count(self.value)
        elif self.dimension == "generation":
            actual = agg.generation_count(self.value)
        else:
            # The run-state dimension's numeric value, compared against this
            # predicate's own threshold. No boolean coercion: `1 if fired else
            # 0` would fold the threshold into the flag name and could express
            # only one threshold per dimension.
            actual = getattr(run_state, self.value)
        # `operator` distinguishes LIFE_GTE (at or above) from LIFE_LTE (at or
        # below). Hardcoding `>=` here would silently invert the LTE triggers —
        # perfect-play life=1000 satisfies `>= 950` but must FAIL `<= 950`.
        fired = actual <= self.threshold if self.operator == "lte" else actual >= self.threshold
        return GateResolution(predicate=self, fired=fired, actual_count=actual)


@dataclass(frozen=True, slots=True)
class GateResolution:
    predicate: GatePredicate
    fired: bool
    actual_count: int


def predicate_from_condition(condition: GateCondition) -> GatePredicate:
    """The single mapping from an on-disk `condition` object to a
    predicate: `kind` becomes `dimension`, `target` becomes `value`,
    `count` becomes `threshold`."""
    return GatePredicate(
        dimension=condition.kind,
        value=condition.target,
        threshold=condition.count,
        operator=condition.operator,
    )


@dataclass(frozen=True, slots=True)
class ResolvedSkill:
    card_id: str
    # The HOLOMEM, not the card. The Holomem Board is per-character, so a
    # board modifier belongs to the member and reaches every card of theirs.
    # Carried here rather than looked up later so scoring never has to hold a
    # card index just to find out whose board applies.
    member: str
    slot_kind: str
    source: str
    family: str
    skill: SkillSlot
    gate: GateResolution | None
    override_gate: GateResolution | None
    # A slot's rider carries its OWN gate (e.g. LIFE>=1000 on an
    # activation_rate_up rider), resolved here the same once-per-6-card-set way
    # `override_gate` is, so scoring's rider fold reads a resolved gate rather
    # than re-deriving one. Defaulted to None so the ungated/rider-less case and
    # the sole other constructor (`tests/conftest.py`) need not pass it.
    rider_gate: GateResolution | None = None


@dataclass(frozen=True, slots=True)
class CardSetGates:
    """The leader-agnostic result of resolving every skill instance in a
    6-card set: one `TeamAggregates` snapshot, plus every
    card's resolved normal-slot skills and its resolved costume skill (if
    any), keyed by card id. `normal_by_card_id[card_id]` is that card's
    non-costume `ResolvedSkill`s in `SKILL_SLOT_KINDS` order;
    `costume_by_card_id[card_id]` is that card's resolved costume
    `ResolvedSkill` — the PRINCIPAL only — or `None` when the card has no
    costume skill. The outfit's scored `score_support` rider
    is NOT in this map: it exists only for the LEADER's outfit, and every
    consumer reads it through `resolve_leader_costume_skills`."""

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
    """The single per-skill resolution step, shared by every call site in
    `resolve_card_set_gates` for both a normal slot and a costume slot.

    `skill.condition` is three-state. An `UNKNOWN` condition and a
    structurally `None` (ungated) condition both resolve to `gate = None`
    here, because an `UNKNOWN` condition must never be handed to
    `predicate_from_condition` — hence the `is_known` check first. That
    makes `gate = None` **deliberately ambiguous** at this layer: it means
    "no gate fired against this skill", not "this skill has no gate".

    The two are *not* the same thing, and collapsing them would overstate
    scores: an ungated skill contributes at full value,
    while an unrecorded one must contribute nothing. Disambiguating them
    is therefore not this function's job but `domain/scoring.py`'s, and it
    happens at exactly one place there — `_REQUIRED_KNOWN_FIELDS`, which
    lists `condition` first. `_resolve_skill` consults it via
    `_first_missing_field` and `_fold_activation_rate_up` via
    `_has_all_known_fields`, so both the per-skill and the team-rate-fold
    legs are covered by that single list.

    This paragraph names a specific mechanism rather than promising a
    downstream catch in the abstract, because an abstract promise can point
    at a catch that does not exist. Removing `condition` from that list fails
    `tests/test_unknown_condition_is_data_missing.py`.

    An override's own gate is resolved
    here too, once per 6-card set, for the identical reason the
    principal's is — it depends only on `(aggregates, run_state)` and is
    leader-invariant, never per-leader. An `UNKNOWN` override must never
    reach `predicate_from_condition` (hence the `is_known` check below,
    mirroring the principal condition's own check), and a structurally
    absent (`None`) override resolves no gate at all — there is nothing to
    resolve against."""
    condition = skill.condition
    gate = None
    if is_known(condition) and condition is not None:
        predicate = predicate_from_condition(condition)
        gate = predicate.resolve(agg, run_state)

    override = skill.override
    override_gate = None
    if is_known(override) and override is not None:
        override_predicate = predicate_from_condition(override.condition)
        override_gate = override_predicate.resolve(agg, run_state)

    # Resolve the rider's OWN condition, exactly as the override's is
    # resolved above — three-state (an `UNKNOWN` rider condition must never reach
    # `predicate_from_condition`, a structurally `None` one is ungated/team-wide
    # and resolves no gate). Only a populated `SlotRider` has a condition to
    # resolve; the shared absent marker (`rider.kind == "none"`) parses to a
    # non-`SlotRider` value and is skipped.
    rider = skill.rider
    rider_gate = None
    if isinstance(rider, SlotRider):
        rider_condition = rider.condition
        if is_known(rider_condition) and rider_condition is not None:
            rider_gate = predicate_from_condition(rider_condition).resolve(agg, run_state)

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
    """The primary, leader-agnostic resolver — the only
    resolution loop in this module. Aggregates once over the full 6-card
    set (`aggregate` is leader-agnostic), then
    resolves every card's normal skill slots and its costume slot against
    that single `TeamAggregates` snapshot plus `run_state`.

    Every card's costume slot is resolved here even though at most one of
    them will be used by any given arrangement: gate resolution depends
    only on `(aggregates, run_state)`, so a costume skill's gate outcome is
    identical no matter which card leads, and resolving all six once is
    strictly cheaper than resolving one six times.

    The `ResolvedSkill` records built here carry exactly the `card_id` /
    `slot_kind` / `source` / `family` / `skill` / `gate` values
    `Team.effective_skill_instances()` would have produced for the same
    skill: `source` is `"normal"` for a normal slot and `"costume"` for a
    costume slot, and `slot_kind` is the `SKILL_SLOT_KINDS` member for a
    normal slot and the lowercase `"costume"` string
    `Team.effective_skill_instances()` emits for a costume slot (not
    `schema.COSTUME_SLOT_KIND`, which is uppercase). Getting `slot_kind`
    wrong here silently changes `SkillContribution.slot_kind` in every
    downstream breakdown."""
    agg = aggregate(cards)
    type_counts = agg.type_counts
    generation_counts = agg.generation_counts
    normal_by_card_id: dict = {}
    costume_by_card_id: dict = {}

    for card in cards:
        # The per-card gate table: this card's resolved slots are a function of
        # `run_state` and of the handful of counts its own conditions read
        # (`_gate_probes`), so they are resolved once per distinct reading and
        # the same frozen `ResolvedSkill`s are handed back afterwards.
        table = _card_gate_tables.get(id(card))
        if table is None or table[0] is not card:
            if len(_card_gate_tables) > 100_000:
                _card_gate_tables.clear()
            table = (card, _gate_probes(card), {})
            _card_gate_tables[id(card)] = table
        reading = (run_state, *[
            (type_counts if dimension == "type" else generation_counts).get(value, 0)
            for dimension, value in table[1]
        ])
        resolved = table[2].get(reading)
        if resolved is None:
            normal_resolved: list[ResolvedSkill] = []
            for slot_kind in SKILL_SLOT_KINDS:
                skill = card.skill(slot_kind)
                if skill is None:
                    continue
                normal_resolved.append(
                    _resolve_one_skill(
                        card.id, card.member, slot_kind, "normal", skill, agg, run_state
                    )
                )
            costume = None
            if card.costume_skill is not None:
                costume = _resolve_one_skill(
                    card.id, card.member, "costume", "costume", card.costume_skill,
                    agg, run_state
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
    """Resolve the leader's Outfit Skill against the MEMBERS' aggregates, or
    `None` if it has no costume slot.

    Separate from `resolve_card_set_gates`'s `costume_by_card_id` on purpose:
    that map only covers cards in the set it was given, and the leader may be a
    card outside the member set entirely. Passing the
    members' aggregates is what makes a gated outfit unable to satisfy its own
    condition — the outfit's own card is not in those counts unless it is
    also one of the five.

    Shared by `resolve_team_gates` (single team) and `scoring.score_card_set`
    (batch) so the two can never resolve an outfit gate differently."""
    if leader.costume_skill is None:
        return None
    return _resolve_one_skill(
        leader.id, leader.member, "costume", "costume", leader.costume_skill,
        aggregates, run_state
    )


# The `source` a leader outfit's scored RIDER carries. Its
# `slot_kind` is the same lowercase `"costume"` the principal carries (it is
# the outfit slot), so the two are told apart by `source` alone — which is
# also the third element of `score_card_set`'s per-skill cache key.
COSTUME_RIDER_SOURCE = "costume_rider"


def resolve_leader_costume_skills(
    leader: Card, aggregates: TeamAggregates, run_state: RunState
) -> tuple[ResolvedSkill, ...]:
    """Every scored effect of the leader's Outfit, in emission order: the
    principal (`resolve_leader_costume`), then the outfit's
    `score_support` RIDER as a second `ResolvedSkill` (source
    `COSTUME_RIDER_SOURCE`, a synthesized COSTUME `score_support` slot from
    `card.scored_costume_rider_slot`), or nothing at all for a leader with no
    outfit slot.

    The rider is resolved through the SAME `_resolve_one_skill` against the
    SAME members' aggregates as the principal: its gate is the rider's
    OWN condition — e.g. Radiant Summer Heatwave fires sense +80 % on 2+ Cute
    and, separately, Score Support 25 % on 2+ Cute — and, like the outfit's own
    gate, a Cute leader outside the five does not count towards it. This is
    the mirror of `Team.effective_skill_instances()`, which emits the same
    principal-then-rider pair, so `resolve_team_gates` stays in lockstep with
    it.

    Every consumer that reads "the leader's outfit" (`score_card_set`,
    `song.score_song_detailed`, `exact_row.RowBound`, `resolve_team_gates`)
    iterates this tuple. `resolve_leader_costume` keeps returning the principal
    alone for callers that only ever wanted it."""
    principal = resolve_leader_costume(leader, aggregates, run_state)
    if principal is None:
        return ()
    rider_slot = scored_costume_rider_slot(leader.costume_skill)
    if rider_slot is None:
        return (principal,)
    rider = _resolve_one_skill(
        leader.id, leader.member, "costume", COSTUME_RIDER_SOURCE, rider_slot,
        aggregates, run_state
    )
    return (principal, rider)


def resolve_team_gates(team: Team, run_state: RunState) -> tuple[ResolvedSkill, ...]:
    """Thin wrapper: delegates to `resolve_card_set_gates` for the
    member-set resolution, then assembles the leader-specific result in
    `Team.effective_skill_instances()`'s exact order — the leader's costume
    entry when present, then each member's normal slots in member order. That
    reproduces the emission order exactly, so `ScoreBreakdown.contributions`
    ordering is unaffected. Holds no resolution loop of its own
    — the only resolution loop in this module lives in
    `resolve_card_set_gates`.

    **Gates resolve over the 5 MEMBERS only**: the leader
    contributes nothing to the type/generation counts, and its own outfit gate
    is resolved against those member aggregates — so a gated outfit can never
    satisfy its own condition. The leader's normal SPECIAL/ACTIVE/PASSIVE
    slots are not emitted at all; if its card is also a member, they
    arrive through the member loop below, exactly once."""
    card_set_gates = resolve_card_set_gates(team.members, run_state)
    resolved: list[ResolvedSkill] = []
    resolved.extend(
        resolve_leader_costume_skills(team.leader, card_set_gates.aggregates, run_state)
    )
    for member in team.members:
        resolved.extend(card_set_gates.normal_by_card_id[member.id])
    return tuple(resolved)


# id(card) -> (card, its gate probes, {reading: (normal slots, costume slot)}).
# Keyed by object identity with the card kept alive in the entry, so a recycled
# `id()` can never serve another card's table.
_card_gate_tables: dict[int, tuple] = {}


def _gate_probes(card: Card) -> tuple[tuple[str, str], ...]:
    """Every `(dimension, value)` team count this card's gates can read — the
    type and generation counts `GatePredicate.resolve` looks up for its normal
    slots and its costume slot (principal condition, override, rider).

    This mirrors `_resolve_one_skill`'s three lookups and must list AT LEAST
    what it reads: a `GateResolution` carries the count itself, so two teams
    that agree on every probe (and on `run_state`, which covers the third
    dimension) resolve this card to equal records. A condition this function
    cannot read is simply not probed; `_resolve_one_skill` then meets it on the
    table miss and raises."""
    probes: list[tuple[str, str]] = []

    def probe(condition: object) -> None:
        if is_known(condition) and condition is not None:
            predicate = predicate_from_condition(condition)
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
