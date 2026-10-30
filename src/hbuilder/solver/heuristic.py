"""Gate-aware, multi-seeded beam search over (5 Members + 1
Outfit/Leader) teams — the approximate sibling of `solver/exact.py`.

`solve_heuristic` mirrors `solve_exact`'s signature and return type exactly,
so a caller cannot tell which one it called. `solve_heuristic_labelled`
returns the same teams *plus* the specific failure modes the search actually
observed while producing them (an approximate result must name the specific
way it could be wrong, never a generic "approximate" caveat).

Why exact search is not an option, and therefore why this module exists: a
real 91-card `data/owned.json` is 46,504,458 member sets and 4,231,905,678
arrangements, projected at **31.96 h** (28.8 h once R1 — five distinct
holomem — is credited). `--require x1` only reaches 3.43 h, because
`C(N-1,4) + C(N-1,5) = C(N,5)`: a lock does not shrink the set count at all,
only the arrangement count.

## The shape (after int3rrupt3d's beam search)

1. a one-off **static-potential ordering** of the pool (`_static_potential`);
2. a bounded **beam extended one card per level** (`_run_beam`);
3. **per-feature beam quotas** — one heap per attribute/generation any gate
   trigger in the pool names, applied both to *branching* (`_extend`, see
   `DEFAULT_FEATURE_BRANCH`) and to *selection*, merged back as diversity
   rescues (`_quota_features`, `_select_level`);
4. an **open-condition bonus** crediting a partial team for gates it could
   still satisfy with the slots it has left (`_open_conditions`);
5. **one-card-swap polish** at the end (`_polish`);
6. **multiple seeds**, each a different static-potential context.

int3rrupt3d's 5!-order check and positional SPECIAL weights are deliberately
NOT copied: they serve a modelling assumption this project does not hold.

**What carries the result, and what does not** — an add-one ablation over
six real N=16 pools: the **swap polish** and the **per-feature branch** each
take 2/6 top-1 matches to 6/6.
The **open-condition bonus** and the **per-feature quota** move nothing
measurable, because 62 of 67 real gated slots sit at threshold 2 and their
magnitudes run 6–45, so the bonus is worth ~2.8 % of a partial's value
against a much larger stat spread. They are kept because a future card can
change that, not because they are load-bearing.

## Slot order is inside the OBJECTIVE, not inside the beam — and that is correct

In the client binary,
`LiveSkillUtility::IsSatisfyEffectTarget{Attribute,Character,CharacterGrouping}
Condition` count matching members *in deck order* and return
`count <= targetCount`, so a count-limited passive reaches only the first N
matching members. Separability fails — the count is a property of the whole
prefix, not of the pair `(card, slot)` — so it is a **search**, not a table
lookup, costing about 1.13x on the inner loop.

That search lives in `domain/scoring.py::_best_order_stat_sum`, and this
module's objective function is `score_card_set`, so **every value this beam
compares — every partial at every level, every finalist, every swap
candidate — is already resolved under the attested first-N-in-deck-order
rule**. This beam therefore does **not** search over ordered partial teams,
and does not need to.

**Dedup-by-set is provably lossless because the beam presents every set in ONE
canonical order** — `members = tuple(sorted(slots, key=card.id))` at every level
and in `_partial_value` — so the objective it evaluates is a deterministic
function of the SET, not of the commit order. Recipients are ranked by TOTAL
parameter (effect-independent): when totals are distinct the recipient is unique
and every ordering agrees; on an EXACT total tie the recipient follows slot order
(the server rule, and the ranking's `top_n_by_stat` tie-break too), and the
beam's ascending-id canonicalization pins that tie
deterministically. Either way two beam states holding the same *k* cards in
different commit orders re-canonicalize to the identical member tuple, carry the
identical value and the identical set of reachable completions, so keeping both
buys nothing. Tested, not argued:
`test_the_objective_is_a_function_of_the_canonical_member_set_so_dedup_is_lossless`.

`_State.slots` is carried in commit order but is **never read as an order**:
the dedupe key is `_State.key`, the member set, and nothing compares two
states by their slot sequence. It is kept ordered only so that a per-song
objective can drop in by changing `_partial_value` and the dedupe key alone
rather than rewriting the beam — because dedup-by-set would **not** be
lossless under a per-`(card, slot)` term, which is exactly the case of the
SPECIAL cue. That term is
**structurally absent from `domain/scoring.py`** and belongs to the per-song
entry point (`domain/song.py`).

The top teams' winning slot order is reported
(`HeuristicResult.member_orders`, and `--json`'s `search.member_orders`)
rather than left for the caller to guess.

## What this module does NOT do

It never resolves a gate and never computes a rate itself — the same
one-way `solver/` -> `domain/` seam `exact.py`'s docstring names. The
open-condition bonus reads gates through `domain.gates.aggregate` and
`domain.gates.predicate_from_condition`, the domain's own public API, so
there is no second copy of gate resolution here to drift from the first.
It imports nothing from `json`, `argparse`, `pathlib` or `sys`.
"""

from __future__ import annotations

import heapq
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Sequence

from hbuilder.domain.card import Card, EffectTarget
from hbuilder.domain.gates import aggregate, predicate_from_condition
from hbuilder.domain.scoring import score_card_set, score_with_breakdown
from hbuilder.domain.scoring_config import ScoringConfig
from hbuilder.domain.team import MEMBER_COUNT, Team
from hbuilder.domain.unknown import is_known

# `_heap_key` is imported rather than re-derived on purpose: the heuristic's
# Top-N must break ties by exactly the rule `solve_exact` breaks them by
# (`(-score, leader_id, sorted(member_ids))`), or the two solvers would
# disagree on equal-scoring teams and the validation harness would report that
# disagreement as a search failure. Two copies of a tie-break rule is how that
# happens; one import is how it does not.
from hbuilder.solver.exact import (  # noqa: F401  (RankedTeam is re-exported)
    Constraint,
    Progress,
    RankedTeam,
    SetConstraint,
    _heap_key,
    canonical_pool,
)

DEFAULT_TOP_N: int = 10

# Defaults, checked with `bench/bench_heuristic.py` — 72/72 exact top-1
# agreement with `solve_exact` at N=9..20 and 10.60 s on a real 91-card pool,
# against 31.96 h exhaustive.
#
# They are deliberately GENEROUS. The machinery saturates at `beam_width = 8`
# on real N=16 pools, 12x below this — but that is a measurement from *below*
# the interesting range, and whether 96 is 12x more than N=91 needs or exactly
# what carries it is not known. 10.6 s is not a budget worth optimising
# against that uncertainty.
DEFAULT_BEAM_WIDTH: int = 96
DEFAULT_BRANCH: int = 24
DEFAULT_SEEDS: int = 4
DEFAULT_QUOTA: int = 2
# How many extra extensions per quota feature `_extend` admits on top of the
# `branch` best by static potential. See `_extend`'s docstring for the
# 86.3% miss this closes.
DEFAULT_FEATURE_BRANCH: int = 2
DEFAULT_OPEN_CONDITION_WEIGHT: float = 0.35

# How `_open_conditions` prices a reachable-but-unfired gate.
#
# **Neither mode is an admissible bound, and that is the point of having both.**
# `"discounted"` (the shipped default) charges
# `(magnitude/100) / (1 + shortfall)`, which UNDER-credits — the gate, if it
# fires, is worth its full delta, so a partial one card from a large payoff is
# still ranked below its true best completion and can still be pruned.
# `"full"` charges `magnitude/100` with no shortfall discount, which is the
# largest credit the gate could ever be worth; it removes the under-credit but
# does not make the key an upper bound either, because the base it multiplies
# is the *partial's own* score rather than a bound over completions.
# `"off"` charges nothing.
#
# The honest statement is that the beam's ranking key is a heuristic in every
# mode. `bench/validate_search.py --open-modes` measures what each mode costs
# against exact search, which is the only question that decides anything.
OPEN_CONDITION_MODES: frozenset[str] = frozenset({"discounted", "full", "off"})
DEFAULT_OPEN_CONDITION_MODE: str = "discounted"
DEFAULT_POLISH_TEAMS: int = 3
DEFAULT_POLISH_ROUNDS: int = 4
DEFAULT_RNG_SEED: int = 0

# A truncation is only *reported* when the cut was a near-tie: the best
# evicted partial scored within this fraction of the worst kept one. A label
# emitted on every run is worthless, and every run with `N > beam_width`
# truncates something; what is worth reporting is that the truncation was
# close enough to have plausibly gone the other way.
TRUNCATION_MARGIN: float = 0.005

# `progress` is called every this many scored arrangements. Smaller than
# `exact.PROGRESS_INTERVAL_TEAMS` because a whole heuristic run scores fewer
# arrangements than one exact progress tick.
PROGRESS_INTERVAL_TEAMS: int = 5_000


class FailureMode(Enum):
    """The specific ways this search can have missed a better team.

    Each member is emitted **only when the search actually observed that
    situation at runtime** — not as a blanket caveat attached to every
    approximate result. `HeuristicResult.observations` is empty when the beam
    never truncated close, never rescued anything, never had to drop a
    partial carrying a reachable multi-member gate, never improved on itself
    by swapping, and every seed agreed; and that emptiness is a real (if
    weak) statement about the run, which a constant label could never make.
    """

    #: At some level the best evicted partial scored within
    #: `TRUNCATION_MARGIN` of the worst kept one. The cut was a near-tie, so
    #: the branch that was dropped could plausibly have led to a better team.
    BEAM_TRUNCATION = "beam_truncation"

    #: A per-feature quota re-admitted a partial that the global top-`width`
    #: had already dropped. That partial's whole subtree exists only because
    #: its attribute/generation had a reserved heap — so any feature *not* in
    #: `_quota_features` had no such protection on this run.
    QUOTA_RESCUE = "quota_rescue"

    #: A partial survived the cut only because of the open-condition bonus:
    #: ranked inside the beam with the bonus, outside it without. The bonus
    #: is a heuristic credit, not a score, so this names a survivor whose
    #: presence rests on an estimate rather than on a computed team score.
    OPEN_CONDITION_RESCUE = "open_condition_rescue"

    #: **The named pairwise failure mode.** A partial that was
    #: evicted carried a gate needing **two or more further matching members**
    #: to fire. Such a gate pays nothing until both arrive, so the partial
    #: looks weak at the level it was cut — "a team whose gate only fires
    #: once two particular members are combined may have been missed" is not
    #: assumed here, it is *detected*, and the observation names the gate.
    PAIRWISE_GATE_MISS = "pairwise_gate_miss"

    #: The one-card-swap polish strictly improved on the beam's own best team.
    #: The beam's ranking was therefore demonstrably not exhaustive, and the
    #: returned answer is only a local optimum under single swaps. Not emitted
    #: when polish found nothing — that is the informative distinction.
    SWAP_LOCAL_OPTIMUM = "swap_local_optimum"

    #: Two seeds' best teams differ. The seeds are independent static-potential
    #: contexts, so disagreement is direct evidence that the answer depends on
    #: where the search started.
    SEED_DISAGREEMENT = "seed_disagreement"

    #: Cards in the pool that **no beam state ever extended into**, so they were
    #: never candidates for any team the search considered. This is the mode the
    #: other five are blind to: `_extend` takes the best `branch` extensions by
    #: static potential, so on a pool larger than roughly `branch + MEMBER_COUNT`
    #: the tail of the potential ordering is unreachable no matter how wide the
    #: beam is. Every other mode reports on a partial the search *built and then
    #: dropped*; this one reports on cards it never built with at all.
    #:
    #: Why this member exists: on a 14-card pool at `branch=6` the beam can miss
    #: the best team by **86.3%** (3,225 against exact's 23,511) while emitting
    #: only `BEAM_TRUNCATION` and `QUOTA_RESCUE` — because the six cards carrying
    #: the gate that made the best team were never reached, nothing carrying that
    #: gate was ever *evicted*, and `PAIRWISE_GATE_MISS` is an eviction
    #: observation. Without this member the labels would be silent in exactly
    #: the case they exist for.
    EXTENSION_HORIZON = "extension_horizon"


@dataclass(frozen=True, slots=True)
class Observation:
    """One runtime sighting of one `FailureMode`.

    `detail` names the concrete thing that was seen — the gate, the feature,
    the card ids — because "a gate may have been missed" is a caveat and
    "`generation>=3` on `card-00031-...` needed two more members and its
    partial was cut at level 3" is a finding.
    """

    mode: FailureMode
    detail: str
    level: int | None = None
    margin: float | None = None
    seed: int | None = None


@dataclass(frozen=True, slots=True)
class HeuristicResult:
    """`solve_heuristic`'s answer, plus everything needed to label it.

    `teams` is exactly what `solve_heuristic` returns and exactly what
    `solve_exact` would return in shape — same `RankedTeam`, same ordering
    rule, same tie-break.
    """

    teams: tuple[RankedTeam, ...]
    observations: tuple[Observation, ...]
    #: The winning deck slot order for each returned team, keyed by that
    #: team's ascending `member_ids`. `None` when no count-limited effect
    #: binds on that team and every ordering scores identically — the honest
    #: report.
    member_orders: dict[tuple[str, ...], tuple[str, ...] | None]
    #: How many `(member set, leader)` arrangements were actually scored.
    #: Compare against `estimate.project_arrangements` for the exact run to
    #: see what the beam bought.
    arrangements_scored: int
    #: How many *partial* evaluations the beam paid for on top of that.
    partials_scored: int
    beam_width: int
    branch: int
    seeds: int

    def modes(self) -> frozenset[FailureMode]:
        return frozenset(observation.mode for observation in self.observations)


# ---------------------------------------------------------------------------
# Static potential
# ---------------------------------------------------------------------------


def _skeleton(
    pool: Sequence[Card], exclude_member: str, size: int, order: Sequence[Card]
) -> tuple[Card, ...]:
    """The first `size` cards of `order` with distinct holomem, none of them
    `exclude_member`. Used to build the reference partial a card's static
    potential is measured against."""
    chosen: list[Card] = []
    seen: set[str] = {exclude_member}
    for card in order:
        if card.member in seen:
            continue
        chosen.append(card)
        seen.add(card.member)
        if len(chosen) == size:
            break
    return tuple(chosen)


def _static_potential(
    pool: tuple[Card, ...], cfg: ScoringConfig, context: Sequence[Card]
) -> dict[str, float]:
    """A one-off ordering of the pool: each card scored as the fifth member of
    a fixed four-card reference skeleton, with itself in the outfit slot.

    This is a *context*, not an intrinsic card value, and that is the point —
    it goes through `score_card_set`, so a card whose worth is a gate it
    completes or an outfit it supplies is ranked on what it actually does to
    a team rather than on its raw stat line. Changing the context is what
    makes two seeds genuinely different searches rather than the same search
    with jitter.

    Cost is `len(pool)` single-leader `score_card_set` calls — about 12 ms
    at a real 91-card pool, i.e. free against the beam that follows.
    """
    potential: dict[str, float] = {}
    for card in pool:
        skeleton = _skeleton(pool, card.member, MEMBER_COUNT - 1, context)
        if len(skeleton) < MEMBER_COUNT - 1:
            # Pool too small (or too holomem-collided) to build a reference
            # partial; fall back to the raw stat line rather than raising —
            # `solve_heuristic` must never fail where `solve_exact` succeeds.
            potential[card.id] = float(card.stat_sum())
            continue
        members = tuple(sorted((*skeleton, card), key=lambda item: item.id))
        potential[card.id] = score_card_set(members, (card,), cfg)[0].total_score
    return potential


# ---------------------------------------------------------------------------
# Gate features and the open-condition bonus
# ---------------------------------------------------------------------------


def _card_conditions(card: Card) -> Iterable[object]:
    """Every recorded gate condition on a card's own (non-costume) slots.

    The costume slot is deliberately skipped: a member's costume skill is
    never emitted (`Team.effective_skill_instances`), only the leader's, so
    crediting it to a member partial would credit a term the score cannot
    contain.
    """
    for slot in card.skills:
        if slot is None:
            continue
        condition = slot.condition
        if is_known(condition) and condition is not None:
            yield condition


def _quota_features(pool: tuple[Card, ...]) -> tuple[tuple[str, str], ...]:
    """The `(dimension, value)` pairs that get their own beam heap.

    Exactly the attributes and generations that some **trigger** in this pool
    names — either a gate condition (`GateCondition.kind`/`target`) or a
    count-limited effect target (`EffectTarget.kind`/`value`, the
    slot-order-sensitive one). A feature no trigger names cannot change any
    team's score by being present, so reserving a heap for it would spend
    beam width on diversity that buys nothing.

    Returned sorted, so the quota pass is deterministic.
    """
    features: set[tuple[str, str]] = set()
    for card in pool:
        for condition in _card_conditions(card):
            if condition.kind in ("type", "generation"):
                features.add((condition.kind, condition.target))
        for slot in card.skills:
            if slot is None:
                continue
            target = slot.target
            if isinstance(target, EffectTarget) and target.kind in (
                "type",
                "generation",
            ):
                if target.value is not None:
                    features.add((target.kind, target.value))
    return tuple(sorted(features))


def _has_feature(card: Card, dimension: str, value: str) -> bool:
    if dimension == "type":
        return card.type == value
    return value in card.generations


@dataclass(frozen=True, slots=True)
class OpenCondition:
    """A gate on a partial team that has not fired but still could, given the
    slots the partial has left."""

    card_id: str
    dimension: str
    value: str
    threshold: int
    shortfall: int
    magnitude: float


def _open_conditions(
    members: tuple[Card, ...],
    cfg: ScoringConfig,
    remaining_slots: int,
    mode: str = DEFAULT_OPEN_CONDITION_MODE,
) -> tuple[tuple[OpenCondition, ...], float]:
    """Every gate on `members` that is unfired but reachable within
    `remaining_slots` more members, and the heuristic rate they are worth.

    **This is the term that stops the pairwise-gate failure mode from being
    silent.** A gate reading "with 3+ Cute" pays exactly nothing
    on a two-Cute partial, so the partial that is one card away from a large
    payoff scores identically to one that is three cards away — and the beam
    cuts it. Crediting the reachable-but-unfired gate is what keeps it alive.

    Gate resolution itself is `domain.gates`' job, not this module's:
    the aggregate comes from `gates.aggregate` and the fired/not-fired
    decision from `GatePredicate.resolve`, so this cannot drift from what
    `score_card_set` will decide when the gate does fire.

    Run-state gates (`life`, `combo`) are excluded by construction: no choice
    of members can change them, so they are never "open".

    The rate is `magnitude/100` discounted by `1 / (1 + shortfall)` — a gate
    one card away is worth more to a partial than one two cards away, and a
    gate whose magnitude was never transcribed (`UNKNOWN`) is worth nothing
    rather than a fabricated number.

    **`mode` chooses how the reachable gate is priced, and no mode makes this an
    admissible bound.** See `OPEN_CONDITION_MODES`.

    ## ⚠ Not a bound in either direction, and it costs nothing

    `1 / (1 + shortfall)` is a bound in
    **neither** direction. It *under*-credits, because the true best case is the
    full delta — the gate does fire once the members arrive — so an optimal team
    can still be pruned; and it *over*-credits, because the base it is added to
    is the *partial's own* score rather than a bound over its completions, plus
    a bonus for a gate that base does not satisfy. The ranking key is therefore
    neither achievable nor an upper bound. **Even the undiscounted `"full"` mode
    does not fix that** — the flaw is in the base, not in the discount.

    **What it costs is measured by two harnesses that share no code.**
    `bench/bench_heuristic.py`, same pools and same exact references, credit on
    against `open_condition_weight=0.0`:

    | configuration | credit ON | credit OFF |
    |---|---|---|
    | shipped defaults, N=28/32, 20 pools | **20/20** | **20/20** |
    | width 8, N=28/32, 20 pools | 20/20 | 20/20 |
    | width 4 / 1 seed, N=28/32, 20 pools | 20/20 | 20/20 |
    | width 2 / 1 seed / no quota / no polish, N=20/22, 30 pools | 4/30 | 3/30 |
    | width 1 (greedy), N=20/22, 30 pools | 1/30 | 1/30 |

    `bench/validate_search.py --open-modes` sweeps the same question through the
    three named modes at N = 24…48.

    **At every configuration anyone ships, switching this term off changes
    nothing** — not the hit rate, not the Top-10 overlap (10.00 both ways at the
    shipped width). It buys one team in thirty on a beam crippled to width 2,
    and nothing at all on a greedy one. So the honest status is: the term is
    theoretically unsound, empirically inert, and **should be left alone** —
    replacing a discount that costs nothing with a bound that costs review time
    is not an improvement. Re-measure if the beam is ever narrowed below width 4
    or the pool shape changes materially.
    """
    aggregates = aggregate(members)
    found: list[OpenCondition] = []
    rate = 0.0
    for card in members:
        for slot in card.skills:
            if slot is None:
                continue
            condition = slot.condition
            if not is_known(condition) or condition is None:
                continue
            if condition.kind not in ("type", "generation"):
                continue
            predicate = predicate_from_condition(condition)
            resolution = predicate.resolve(aggregates, cfg.run_state)
            if resolution.fired:
                continue
            shortfall = predicate.threshold - resolution.actual_count
            if shortfall <= 0 or shortfall > remaining_slots:
                continue
            magnitude = slot.magnitude
            if not is_known(magnitude):
                continue
            found.append(
                OpenCondition(
                    card_id=card.id,
                    dimension=predicate.dimension,
                    value=predicate.value,
                    threshold=predicate.threshold,
                    shortfall=shortfall,
                    magnitude=float(magnitude),
                )
            )
            if mode == "off":
                continue
            if mode == "full":
                rate += float(magnitude) / 100.0
            else:
                rate += (float(magnitude) / 100.0) / (1.0 + shortfall)
    return tuple(found), rate


# ---------------------------------------------------------------------------
# The beam
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _State:
    """One ordered partial deck.

    `slots` is in deck order — the order the cards were committed to slots by
    the extension loop. `key` is the member SET, which is what states are
    deduplicated by (see the module docstring: lossless, because the
    objective at every level is already a max over that level's orderings).
    `value` is the open-condition-bonused partial value the beam ranks by;
    `base` is the same number without the bonus, kept so a rescue can be
    detected rather than assumed.
    """

    slots: tuple[Card, ...]
    key: frozenset[str]
    value: float
    base: float
    opens: tuple[OpenCondition, ...]


@dataclass(slots=True)
class _Counters:
    """Per-run counters, and the per-run memo tables that ride with them.

    The memos live here rather than in a module-level `lru_cache` for one
    reason: `cfg`, `constraint` and `set_constraint` are fixed for a run but
    vary BETWEEN runs, and none of them is hashable. A `_Counters` instance is
    created once per solve and threaded to exactly the two functions that do
    the scoring, so its lifetime IS the scope over which the keys below are
    sufficient. A module-level cache would silently serve one run's answers to
    the next.

    On a real 97-card pool the beam re-scores the same
    `(member-set, probe-leader)` 1.57x over and the same finalist set 2.56x
    over. Memoising both takes `solve` from 9.6 s to 6.0 s, x1.60, with the
    top-20 bit-identical.
    """

    partials: int = 0
    arrangements: int = 0
    # `(member ids, probe leader id) -> value`. Sound because `_partial_value`
    # is a pure function of those two plus `cfg`, which is run-fixed.
    partial_memo: dict[tuple, float] = field(default_factory=dict)
    # `(member ids, group identity) -> ranked teams`. The group has to be in
    # the key: two groups admit different leader sets, so the same member set
    # scores differently under each. `RankedTeam` is frozen and no caller
    # mutates the returned list (they extend their own), so the list is shared
    # rather than copied.
    set_memo: dict[tuple, list] = field(default_factory=dict)
    partial_memo_hits: int = 0
    set_memo_hits: int = 0


def _partial_value(
    members: tuple[Card, ...],
    probe_leader: Card,
    cfg: ScoringConfig,
    counters: _Counters,
) -> float:
    """A partial deck's value: `score_card_set` over the prefix, with one
    fixed probe leader.

    The prefix is handed to the domain scorer unchanged, so the partial is
    evaluated under exactly the rules the finished team will be — including
    gate resolution against the *partial's own* aggregates (a "3+ Cute" gate
    genuinely does not fire on a two-Cute prefix, which is the truth the
    open-condition bonus then compensates for) and the attested
    first-N-in-deck-order recipient rule over the prefix's own orderings.

    The probe leader is fixed for the whole run so that two partials at the
    same level are compared on their members, never on a leader that varies
    between them. It is only a probe: the real leader vector is applied at
    level 5, where the arrangement is actually scored.
    """
    key = (tuple(card.id for card in members), probe_leader.id)
    cached = counters.partial_memo.get(key)
    if cached is not None:
        counters.partial_memo_hits += 1
        return cached
    # ⚠ `partials` counts SCORINGS, not calls -- it is reported as
    # `partials_scored`, and counting cache hits as work would make the memo
    # invisible in the very number that measures it.
    counters.partials += 1
    value = score_card_set(members, (probe_leader,), cfg)[0].total_score
    counters.partial_memo[key] = value
    return value


def _extend(
    state: _State,
    candidates: tuple[Card, ...],
    branch: int,
    features: tuple[tuple[str, str], ...],
    feature_branch: int,
) -> Iterable[tuple[Card, ...]]:
    """The `branch` best extensions of one ordered partial by static potential,
    **plus up to `feature_branch` per quota feature**, honouring R1 (a holomem
    may not repeat among the members).

    Yields the child's ordered slots — the parent's order with the new card
    appended at the next slot.

    **Why the second term exists.** Taking only the top `branch` by static
    potential makes the reachable card set roughly `branch + MEMBER_COUNT`
    however wide the beam is, and static potential is a *stat-led* ranking: a
    card whose whole worth is a gate that needs three more of its own attribute
    scores badly as the fifth member of a reference skeleton, because the gate
    does not fire there either. So the cards that make the pairwise-gate
    failure mode real are precisely the cards this ordering puts last.

    The per-feature quota in `_select_level` cannot repair that: it re-admits
    partials the global cut dropped, and a partial that was never *built* was
    never dropped. On a 14-card pool where a `4+ Cute` gate worth
    +400% sits on six low-stat Cute cards: at `branch=6` the beam returned
    3,225 against exact's 23,511 — an **86.3% miss** — and every Cute card was
    unreachable. With `feature_branch=2` the same configuration returns
    23,511, exact.

    So the quota is applied to *branching* as well as to selection. It costs at
    most `len(features) * feature_branch` extra children per state, and
    `_quota_features` is bounded by the attributes and generations some trigger
    in the pool actually names — **14** on a real 91-card collection, not the
    100+ a per-card ordering would suggest.
    """
    used_ids = state.key
    used_members = {card.member for card in state.slots}
    emitted: set[str] = set()
    taken = 0
    for card in candidates:
        if card.id in used_ids or card.member in used_members:
            continue
        emitted.add(card.id)
        yield (*state.slots, card)
        taken += 1
        if taken >= branch:
            break

    if feature_branch <= 0:
        return
    for dimension, value in features:
        taken = 0
        for card in candidates:
            if taken >= feature_branch:
                break
            if card.id in used_ids or card.id in emitted:
                continue
            if card.member in used_members:
                continue
            if not _has_feature(card, dimension, value):
                continue
            emitted.add(card.id)
            yield (*state.slots, card)
            taken += 1


def _select_level(
    children: list[_State],
    width: int,
    quota: int,
    features: tuple[tuple[str, str], ...],
    level: int,
    seed_index: int,
    observations: list[Observation],
) -> list[_State]:
    """The level's survivors: the global top `width` by bonused value, plus
    per-feature quota rescues, with every eviction decision that could have
    hidden a better team recorded as it happens.

    Four things are observed here, and each only when it actually occurs:

    - `BEAM_TRUNCATION` when the cut was a near-tie (`TRUNCATION_MARGIN`);
    - `QUOTA_RESCUE` when a feature heap re-admitted something the global cut
      had dropped;
    - `OPEN_CONDITION_RESCUE` when a survivor is inside the cut on `value` but
      would be outside it on `base` — i.e. it is alive on an estimate;
    - `PAIRWISE_GATE_MISS` when something evicted carried a gate still needing
      two or more matching members.
    """
    if len(children) <= width:
        return sorted(children, key=lambda state: -state.value)

    by_value = sorted(children, key=lambda state: -state.value)
    kept = by_value[:width]
    evicted = by_value[width:]
    kept_keys = {state.key for state in kept}

    worst_kept = kept[-1].value
    best_evicted = evicted[0].value
    if worst_kept > 0.0:
        margin = (worst_kept - best_evicted) / worst_kept
        if margin < TRUNCATION_MARGIN:
            observations.append(
                Observation(
                    mode=FailureMode.BEAM_TRUNCATION,
                    detail=(
                        f"level {level}: {len(evicted)} partials cut; the best "
                        f"of them scored {best_evicted:.4f} against the worst "
                        f"survivor's {worst_kept:.4f}"
                    ),
                    level=level,
                    margin=margin,
                    seed=seed_index,
                )
            )

    # Open-condition rescues: who is inside the cut on `value` but would be
    # outside it on `base`.
    base_cut = sorted(children, key=lambda state: -state.base)[:width]
    base_keys = {state.key for state in base_cut}
    for state in kept:
        if state.key in base_keys or not state.opens:
            continue
        gates = ", ".join(
            f"{open_.dimension}:{open_.value}>={open_.threshold} "
            f"(short {open_.shortfall}) on {open_.card_id}"
            for open_ in state.opens[:3]
        )
        observations.append(
            Observation(
                mode=FailureMode.OPEN_CONDITION_RESCUE,
                detail=(
                    f"level {level}: partial {sorted(state.key)} is inside the "
                    f"beam only with the open-condition bonus ({gates})"
                ),
                level=level,
                seed=seed_index,
            )
        )

    # Per-feature quotas, merged back as diversity rescues.
    rescues: list[_State] = []
    for dimension, value in features:
        matching = [
            state
            for state in by_value
            if state.key not in kept_keys
            and any(_has_feature(card, dimension, value) for card in state.slots)
        ]
        for state in matching[:quota]:
            rescues.append(state)
            kept_keys.add(state.key)
            observations.append(
                Observation(
                    mode=FailureMode.QUOTA_RESCUE,
                    detail=(
                        f"level {level}: {dimension}:{value} quota re-admitted "
                        f"{sorted(state.key)}, which the global cut dropped "
                        f"({state.value:.4f} against a cut at {worst_kept:.4f})"
                    ),
                    level=level,
                    margin=(worst_kept - state.value) / worst_kept
                    if worst_kept > 0.0
                    else None,
                    seed=seed_index,
                )
            )

    # The named failure mode: something with a two-or-more-member gate on it
    # was dropped. Reported once per distinct gate, not once per partial.
    reported: set[tuple[str, str, int]] = set()
    for state in evicted:
        if state.key in kept_keys:
            continue
        for open_ in state.opens:
            if open_.shortfall < 2:
                continue
            gate_key = (open_.dimension, open_.value, open_.threshold)
            if gate_key in reported:
                continue
            reported.add(gate_key)
            observations.append(
                Observation(
                    mode=FailureMode.PAIRWISE_GATE_MISS,
                    detail=(
                        f"level {level}: dropped a partial carrying "
                        f"{open_.dimension}:{open_.value}>={open_.threshold} on "
                        f"{open_.card_id}, still {open_.shortfall} members short "
                        f"— that gate pays nothing until all {open_.shortfall} "
                        f"arrive, so the partial was cut on a score that does "
                        f"not include it"
                    ),
                    level=level,
                    seed=seed_index,
                )
            )

    return kept + rescues


def _run_beam(
    seed_members: tuple[Card, ...],
    extension_pool: tuple[Card, ...],
    probe_leader: Card,
    cfg: ScoringConfig,
    *,
    width: int,
    branch: int,
    feature_branch: int,
    quota: int,
    open_weight: float,
    features: tuple[tuple[str, str], ...],
    seed_index: int,
    observations: list[Observation],
    counters: _Counters,
    reached: set[str],
    open_mode: str = DEFAULT_OPEN_CONDITION_MODE,
    trace: list[dict] | None = None,
) -> list[frozenset[str]]:
    """One beam run: from `seed_members` (which may be empty) up to five
    members, returning the surviving member sets.

    `extension_pool` is already ordered by descending static potential, so
    `_extend`'s "best `branch` extensions" is a prefix walk rather than a
    sort per state.

    `reached` accumulates every card id this run ever built a state with, so
    the caller can name the cards the search never considered at all
    (`FailureMode.EXTENSION_HORIZON`) rather than infer them from the
    parameters.

    `trace`, when supplied, receives one record per level holding the member
    keys the level BUILT and the keys it KEPT. It exists so the validation
    harness (`bench/validate_search.py`) can answer *where* an optimum was
    lost — built-then-evicted is a different defect from never-built, and a
    bare hit rate cannot tell them apart. It is pure observation: nothing in
    this function reads it back, so a traced run and an untraced run take
    identical decisions and return identical answers
    (`test_a_traced_run_returns_exactly_what_an_untraced_run_returns`).
    """
    if len(seed_members) > MEMBER_COUNT:
        return []
    if len({card.member for card in seed_members}) != len(seed_members):
        return []  # the locks themselves violate R1

    remaining = MEMBER_COUNT - len(seed_members)
    seed_opens, seed_rate = _open_conditions(seed_members, cfg, remaining, open_mode)
    if seed_members:
        base = _partial_value(
            tuple(sorted(seed_members, key=lambda card: card.id)),
            probe_leader,
            cfg,
            counters,
        )
    else:
        base = 0.0
    beam = [
        _State(
            slots=seed_members,
            key=frozenset(card.id for card in seed_members),
            value=base * (1.0 + open_weight * seed_rate),
            base=base,
            opens=seed_opens,
        )
    ]

    for level in range(len(seed_members) + 1, MEMBER_COUNT + 1):
        remaining = MEMBER_COUNT - level
        children: dict[frozenset[str], _State] = {}
        for state in beam:
            for slots in _extend(
                state, extension_pool, branch, features, feature_branch
            ):
                reached.add(slots[-1].id)
                key = frozenset(card.id for card in slots)
                if key in children:
                    continue
                members = tuple(sorted(slots, key=lambda card: card.id))
                base = _partial_value(members, probe_leader, cfg, counters)
                opens, rate = _open_conditions(members, cfg, remaining, open_mode)
                children[key] = _State(
                    slots=slots,
                    key=key,
                    value=base * (1.0 + open_weight * rate),
                    base=base,
                    opens=opens,
                )
        if not children:
            return []
        beam = _select_level(
            list(children.values()),
            width,
            quota,
            features,
            level,
            seed_index,
            observations,
        )
        if trace is not None:
            trace.append(
                {
                    "seed": seed_index,
                    "level": level,
                    "built": frozenset(children),
                    "kept": frozenset(state.key for state in beam),
                }
            )

    return [state.key for state in beam]


# ---------------------------------------------------------------------------
# Finalising: real leader vectors, the Top-N heap, and the swap polish
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Group:
    """One lock family: the members
    every candidate in this group must already contain, the cards its free
    slots may be drawn from, and the leaders it admits.

    Family A is every lock as a member, with the whole pool admissible as the
    leader. Family B defers exactly one lock to the outfit slot, and then
    **exactly one** leader is admissible — the deferred card itself, since no
    other card can supply that lock. Mirrors `exact._iter_member_sets` plus
    `exact.iter_candidates`'s leader narrowing.
    """

    seed_members: tuple[Card, ...]
    extension_pool: tuple[Card, ...]
    leader_indices: tuple[int, ...]


def _groups(pool: tuple[Card, ...], locked_ids: frozenset[str]) -> tuple[_Group, ...]:
    all_indices = tuple(range(len(pool)))
    if not locked_ids:
        return (
            _Group(
                seed_members=(),
                extension_pool=pool,
                leader_indices=all_indices,
            ),
        )

    locked = tuple(card for card in pool if card.id in locked_ids)
    unlocked = tuple(card for card in pool if card.id not in locked_ids)
    if len(locked) != len(locked_ids):
        return ()  # a lock is not in this pool at all
    index_by_id = {card.id: position for position, card in enumerate(pool)}

    groups: list[_Group] = []
    if len(locked) <= MEMBER_COUNT:
        groups.append(
            _Group(
                seed_members=locked,
                extension_pool=unlocked,
                leader_indices=all_indices,
            )
        )
    for skipped, deferred in enumerate(locked):
        kept = locked[:skipped] + locked[skipped + 1 :]
        if len(kept) > MEMBER_COUNT:
            continue
        groups.append(
            _Group(
                seed_members=kept,
                extension_pool=unlocked,
                leader_indices=(index_by_id[deferred.id],),
            )
        )
    return tuple(groups)


def _score_set(
    members: tuple[Card, ...],
    group: _Group,
    pool: tuple[Card, ...],
    cfg: ScoringConfig,
    constraint: Constraint | None,
    set_constraint: SetConstraint | None,
    counters: _Counters,
    progress: Progress | None,
) -> list[RankedTeam]:
    """Score one completed member set against its group's admissible leaders,
    applying `set_constraint` and `constraint` exactly where `exact.py`
    applies them (set-level before scoring, leader-level before the leader
    vector is handed to `score_card_set`)."""
    if len({card.member for card in members}) != MEMBER_COUNT:
        return []
    if set_constraint is not None and not set_constraint(members):
        return []

    leader_indices = group.leader_indices
    if constraint is not None:
        leader_indices = tuple(
            position
            for position in leader_indices
            if constraint(members, pool[position])
        )
    if not leader_indices:
        return []

    leader_cards = (
        pool
        if len(leader_indices) == len(pool)
        else tuple(pool[position] for position in leader_indices)
    )
    member_ids = tuple(card.id for card in members)
    # ⚠ Only the SCORING is memoised, not the whole function. `arrangements`
    # and the `progress` callback live in the loop below, and short-circuiting
    # past them would make the run report fewer arrangements than it ranked --
    # a diagnostic that silently disagrees with the answer. `leader_indices` is
    # the right half of the key precisely because it is computed AFTER the
    # constraint filter, so it already encodes both the group and the
    # constraint; `pool` and `cfg` are run-fixed, and `_Counters` is per-run.
    score_key = (member_ids, leader_indices)
    leader_scores = counters.set_memo.get(score_key)
    if leader_scores is None:
        leader_scores = score_card_set(members, leader_cards, cfg)
        counters.set_memo[score_key] = leader_scores
    else:
        counters.set_memo_hits += 1
    results: list[RankedTeam] = []
    for slot, leader in enumerate(leader_cards):
        results.append(
            RankedTeam(
                score=leader_scores[slot].total_score,
                leader_id=leader.id,
                member_ids=member_ids,
                team=Team(leader=leader, members=members),
            )
        )
        counters.arrangements += 1
        if progress is not None and counters.arrangements % PROGRESS_INTERVAL_TEAMS == 0:
            progress(counters.arrangements)
    return results


def _polish(
    best: RankedTeam,
    group: _Group,
    pool: tuple[Card, ...],
    cfg: ScoringConfig,
    constraint: Constraint | None,
    set_constraint: SetConstraint | None,
    counters: _Counters,
    progress: Progress | None,
    rounds: int,
) -> tuple[list[RankedTeam], float]:
    """One-card-swap hill climbing from `best`'s member set.

    Every candidate set is scored by the same `_score_set` the beam's
    finalists went through, so a swap can never "improve" a team by being
    measured differently. Locked members (`group.seed_members`) are never
    swapped out — a lock is a constraint, not a preference.

    Returns every arrangement it scored (they join the same Top-N heap) and
    the total score gain over the input, which is `0.0` when the beam's own
    answer was already 1-swap-locally-optimal.
    """
    locked = {card.id for card in group.seed_members}
    current = best
    produced: list[RankedTeam] = []
    gain = 0.0
    for _ in range(rounds):
        members = current.team.members
        improved: RankedTeam | None = None
        for position, outgoing in enumerate(members):
            if outgoing.id in locked:
                continue
            for incoming in group.extension_pool:
                if incoming.id == outgoing.id:
                    continue
                if any(
                    card.member == incoming.member
                    for index, card in enumerate(members)
                    if index != position
                ):
                    continue
                candidate = tuple(
                    sorted(
                        (*members[:position], incoming, *members[position + 1 :]),
                        key=lambda card: card.id,
                    )
                )
                for ranked in _score_set(
                    candidate,
                    group,
                    pool,
                    cfg,
                    constraint,
                    set_constraint,
                    counters,
                    progress,
                ):
                    produced.append(ranked)
                    if improved is None or ranked.score > improved.score:
                        improved = ranked
        if improved is None or improved.score <= current.score:
            break
        gain += improved.score - current.score
        current = improved
    return produced, gain


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def solve_heuristic_labelled(
    cards: Iterable[Card],
    cfg: ScoringConfig,
    *,
    top_n: int = DEFAULT_TOP_N,
    progress: Progress | None = None,
    constraint: Constraint | None = None,
    set_constraint: SetConstraint | None = None,
    locked_ids: frozenset[str] = frozenset(),
    beam_width: int = DEFAULT_BEAM_WIDTH,
    branch: int = DEFAULT_BRANCH,
    feature_branch: int = DEFAULT_FEATURE_BRANCH,
    seeds: int = DEFAULT_SEEDS,
    quota: int = DEFAULT_QUOTA,
    open_condition_weight: float = DEFAULT_OPEN_CONDITION_WEIGHT,
    open_condition_mode: str = DEFAULT_OPEN_CONDITION_MODE,
    polish_teams: int = DEFAULT_POLISH_TEAMS,
    polish_rounds: int = DEFAULT_POLISH_ROUNDS,
    rng_seed: int = DEFAULT_RNG_SEED,
    per_leader_best: bool = False,
    trace: list[dict] | None = None,
) -> HeuristicResult:
    """The labelled entry point: the same Top-N `solve_exact` would return in
    shape, plus every `FailureMode` this run actually observed.

    Deterministic for a fixed `rng_seed`: the only randomness is
    which reference skeletons the seeds' static potentials are measured
    against, and that is drawn from `random.Random(rng_seed)`.
    """
    if open_condition_mode not in OPEN_CONDITION_MODES:
        raise ValueError(
            f"open_condition_mode must be one of "
            f"{sorted(OPEN_CONDITION_MODES)}, got {open_condition_mode!r}"
        )
    pool = canonical_pool(cards)
    if len(pool) < MEMBER_COUNT:
        return HeuristicResult(
            teams=(),
            observations=(),
            member_orders={},
            arrangements_scored=0,
            partials_scored=0,
            beam_width=beam_width,
            branch=branch,
            seeds=seeds,
        )

    index_of = {card.id: position for position, card in enumerate(pool)}
    features = _quota_features(pool)
    groups = _groups(pool, locked_ids)
    counters = _Counters()
    observations: list[Observation] = []

    by_stat = sorted(pool, key=lambda card: (-card.stat_sum(), card.id))
    rng = random.Random(rng_seed)
    contexts: list[tuple[Card, ...]] = [tuple(by_stat)]
    upper = min(len(pool), max(MEMBER_COUNT, beam_width // 4))
    for _ in range(max(0, seeds - 1)):
        shuffled = list(by_stat[:upper])
        rng.shuffle(shuffled)
        contexts.append(tuple(shuffled) + tuple(by_stat[upper:]))

    # Keyed by the ARRANGEMENT, not appended blindly. Every seed runs its own
    # beam over the same pool and the seeds overlap heavily — plus `_polish`
    # re-scores sets the beam already produced — so the same
    # `(leader_id, member_ids)` pair is scored many times over. `nlargest` over
    # a list holding it `s` times returns it `s` times, and the Top-10 shipped
    # to the user degenerates into one team repeated: on a 10-card pool with
    # 4 seeds, the top ten rows would be three distinct arrangements.
    # `solve_exact` yields each arrangement exactly once, so the two solvers
    # would disagree on Top-N for a pool the beam had searched EXHAUSTIVELY.
    # The score is a pure function of the arrangement, so collapsing on the key
    # cannot lose a better duplicate.
    candidates: dict[tuple[str, tuple[str, ...]], RankedTeam] = {}
    seed_bests: list[RankedTeam] = []
    # Every card id any beam state was ever built with, across every seed and
    # every lock family. Its complement in the pool is `EXTENSION_HORIZON`.
    reached: set[str] = set()

    for seed_index, context in enumerate(contexts):
        potential = _static_potential(pool, cfg, context)
        ordered = tuple(
            sorted(pool, key=lambda card: (-potential[card.id], card.id))
        )
        probe_leader = ordered[0]
        seed_candidates: list[RankedTeam] = []
        for group in groups:
            extension_pool = tuple(
                card
                for card in ordered
                if card.id in {item.id for item in group.extension_pool}
            )
            finalists = _run_beam(
                group.seed_members,
                extension_pool,
                probe_leader,
                cfg,
                width=beam_width,
                branch=branch,
                feature_branch=feature_branch,
                quota=quota,
                open_weight=open_condition_weight,
                features=features,
                seed_index=seed_index,
                observations=observations,
                counters=counters,
                reached=reached,
                open_mode=open_condition_mode,
                trace=trace,
            )
            for member_key in finalists:
                members = tuple(
                    card for card in pool if card.id in member_key
                )
                seed_candidates.extend(
                    _score_set(
                        members,
                        group,
                        pool,
                        cfg,
                        constraint,
                        set_constraint,
                        counters,
                        progress,
                    )
                )
        for ranked_team in seed_candidates:
            candidates[(ranked_team.leader_id, ranked_team.member_ids)] = ranked_team
        if seed_candidates:
            seed_bests.append(
                max(
                    seed_candidates,
                    key=lambda candidate: _heap_key(candidate, index_of),
                )
            )

    # The horizon: pool cards no beam state was ever built with. Computed from
    # what the run actually did, not from `branch` and `len(pool)` — a card can
    # be reached at one level by one seed and not another, and only the union
    # over the whole run is the honest statement. Locked cards are excluded:
    # they are in every candidate by construction, never "extended into".
    locked_seed_ids = {
        card.id for group in groups for card in group.seed_members
    }
    unreached = sorted(
        card.id for card in pool if card.id not in reached and card.id not in locked_seed_ids
    )
    if unreached:
        observations.append(
            Observation(
                mode=FailureMode.EXTENSION_HORIZON,
                # ⚠ This must NOT claim "no team containing one of them was
                # considered". `reached` is fed only inside `_run_beam`, and
                # `_polish` runs AFTER this is computed, swapping from the whole
                # pool — so an unreached card can still reach the answer, and
                # routinely does. Over 66 validation runs the TRUE horizon —
                # cards in zero fully-scored member sets — was **0 every time**,
                # and with polish disabled it equals this number exactly. The
                # mechanism is real (squeezed to branch=4, the exact optimum
                # needed an unreached card on 12 of 20 pools) but the polish
                # repairs it: 11 of those 12 correct with polish, 0 without.
                detail=(
                    f"{len(unreached)} of {len(pool)} pool cards were never "
                    f"extended into by any beam state; they can still enter "
                    f"through the one-swap polish, which is measured to be "
                    f"what repairs this: "
                    f"{', '.join(unreached[:8])}"
                    + (f" (+{len(unreached) - 8} more)" if len(unreached) > 8 else "")
                ),
                margin=len(unreached) / len(pool),
            )
        )
        # The pairwise-gate failure mode again, in its harshest form: an
        # unreached card carrying a gate that needs two or more matching
        # members is a team that "only fires once two particular members are
        # combined" AND was never built. `PAIRWISE_GATE_MISS` on its own only
        # covers the partials the beam built and then evicted, which is silent
        # on exactly the runs that miss hardest.
        unreached_cards = {card.id: card for card in pool if card.id in set(unreached)}
        named: set[tuple[str, str, int]] = set()
        for card_id in unreached:
            for condition in _card_conditions(unreached_cards[card_id]):
                if condition.kind not in ("type", "generation"):
                    continue
                if condition.count < 2:
                    continue
                gate_key = (condition.kind, condition.target, condition.count)
                if gate_key in named:
                    continue
                named.add(gate_key)
                observations.append(
                    Observation(
                        mode=FailureMode.PAIRWISE_GATE_MISS,
                        detail=(
                            f"{card_id} carries "
                            f"{condition.kind}:{condition.target}>={condition.count} "
                            f"and was never reached by the search — a team whose "
                            f"gate only fires once {condition.count} "
                            f"{condition.target} members are combined cannot have "
                            f"been considered with this card in it"
                        ),
                    )
                )

    if len(seed_bests) > 1:
        distinct = {
            (candidate.leader_id, candidate.member_ids) for candidate in seed_bests
        }
        if len(distinct) > 1:
            spread = max(candidate.score for candidate in seed_bests) - min(
                candidate.score for candidate in seed_bests
            )
            observations.append(
                Observation(
                    mode=FailureMode.SEED_DISAGREEMENT,
                    detail=(
                        f"{len(distinct)} of {len(seed_bests)} seeds produced "
                        f"different best teams; their scores span {spread:.4f}"
                    ),
                    margin=spread,
                )
            )

    if not candidates:
        return HeuristicResult(
            teams=(),
            observations=tuple(observations),
            member_orders={},
            arrangements_scored=counters.arrangements,
            partials_scored=counters.partials,
            beam_width=beam_width,
            branch=branch,
            seeds=seeds,
        )

    # Polish the best few distinct member sets, one card at a time.
    ranked = heapq.nlargest(
        max(top_n, polish_teams),
        candidates.values(),
        key=lambda candidate: _heap_key(candidate, index_of),
    )
    polished: list[RankedTeam] = []
    seen_sets: set[tuple[str, ...]] = set()
    group_by_leader = {
        position: group for group in groups for position in group.leader_indices
    }
    for candidate in ranked:
        if len(seen_sets) >= polish_teams:
            break
        if candidate.member_ids in seen_sets:
            continue
        seen_sets.add(candidate.member_ids)
        group = group_by_leader.get(index_of[candidate.leader_id], groups[0])
        produced, gain = _polish(
            candidate,
            group,
            pool,
            cfg,
            constraint,
            set_constraint,
            counters,
            progress,
            polish_rounds,
        )
        polished.extend(produced)
        if gain > 0.0:
            observations.append(
                Observation(
                    mode=FailureMode.SWAP_LOCAL_OPTIMUM,
                    detail=(
                        f"one-card-swap polish improved {sorted(candidate.member_ids)} "
                        f"by {gain:.4f} — the beam's own ranking was not exhaustive, "
                        f"so the returned team is only locally optimal under single "
                        f"swaps"
                    ),
                    margin=gain,
                )
            )
    for ranked_team in polished:
        candidates[(ranked_team.leader_id, ranked_team.member_ids)] = ranked_team

    if per_leader_best:
        # LEADER-DIVERSE output: the best team for each distinct leader, then the
        # top_n leaders by that best. The global top-N by score is dominated by the
        # single highest-value outfit (146/150 one leader on a maxed pool),
        # which buries every conditional outfit (e.g. a Performance-135%-w/-2-Gen-3
        # leader) that only wins on its own kind of team. Those teams ARE already in
        # `candidates` — `_score_set` picked that leader for the member sets where it
        # wins — so collapsing to the best per leader surfaces them in ONE beam
        # instead of one search per leader.
        best_by_leader: dict[str, RankedTeam] = {}
        for candidate in candidates.values():
            current = best_by_leader.get(candidate.leader_id)
            if current is None or _heap_key(candidate, index_of) > _heap_key(
                current, index_of
            ):
                best_by_leader[candidate.leader_id] = candidate
        teams = tuple(
            heapq.nlargest(
                top_n,
                best_by_leader.values(),
                key=lambda candidate: _heap_key(candidate, index_of),
            )
        )
    else:
        teams = tuple(
            heapq.nlargest(
                top_n,
                candidates.values(),
                key=lambda candidate: _heap_key(candidate, index_of),
            )
        )

    member_orders: dict[tuple[str, ...], tuple[str, ...] | None] = {}
    for team in teams:
        if team.member_ids in member_orders:
            continue
        member_orders[team.member_ids] = score_with_breakdown(
            team.team, cfg
        ).member_order

    return HeuristicResult(
        teams=teams,
        observations=tuple(observations),
        member_orders=member_orders,
        arrangements_scored=counters.arrangements,
        partials_scored=counters.partials,
        beam_width=beam_width,
        branch=branch,
        seeds=seeds,
    )


def solve_heuristic(
    cards: Iterable[Card],
    cfg: ScoringConfig,
    *,
    top_n: int = DEFAULT_TOP_N,
    progress: Progress | None = None,
    constraint: Constraint | None = None,
    set_constraint: SetConstraint | None = None,
    locked_ids: frozenset[str] = frozenset(),
    **beam_options: object,
) -> tuple[RankedTeam, ...]:
    """Drop-in for `solve_exact`: same parameters, same `tuple[RankedTeam, ...]`
    ordered by the same tie-break key, so a caller cannot tell which it called.

    A caller that wants the run *labelled* — and any caller presenting these
    teams to a human should — calls `solve_heuristic_labelled` instead and
    renders `HeuristicResult.observations`. This function exists so the
    exact/heuristic switch is a one-line change at the call site and so the
    validation harness can put the two solvers behind one signature.
    """
    return solve_heuristic_labelled(
        cards,
        cfg,
        top_n=top_n,
        progress=progress,
        constraint=constraint,
        set_constraint=set_constraint,
        locked_ids=locked_ids,
        **beam_options,  # type: ignore[arg-type]
    ).teams
