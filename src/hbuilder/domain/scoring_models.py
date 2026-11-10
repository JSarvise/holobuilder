"""Pluggable-model strategy objects: the three genuinely unverified
semantics — same-family stacking (additive vs. multiplicative),
activation-rate interpretation (+x percentage points vs. x(1+x)), and
which N members receive a targeted buff when more than N qualify — are
each expressed as a `typing.Protocol` with named concrete
implementations, never as an `if model == "additive"` branch scattered
through scoring math.

Every implementation of each Protocol exists here, behind exactly three
name-to-instance registries (`STACKING_MODELS`, `ACTIVATION_RATE_MODELS`,
`TARGET_SELECTION_MODELS`). These registries are the *only* place in the
codebase where a model name string becomes a strategy object —
`scoring.py` never compares a model name for equality, and
`scoring_config.py` resolves `model.stacking` / `model.activation_rate` /
`target_selection.strategy` through exactly these three dicts.

`TargetSelectionModel` is intentionally `Card`-free: it is typed over
plain `(card_id, stat_value)` pairs rather than `Card` objects, so this
module keeps its existing "pure float math, imports nothing from
`domain/`" property. The `Card`-aware resolution walk that builds those
pairs lives in `domain/targets.py`, a sibling of `domain/gates.py`.
"""

from __future__ import annotations

from typing import Protocol


class StackingModel(Protocol):
    """Combines same-pool rate contributions from multiple cards into one
    combined rate. `combine` takes bare rate contributions where 0.10 means
    plus ten percent, and returns the *combined rate* — the caller adds 1.0
    to it. It never returns a multiplier itself, which keeps the
    "one plus the combined rates" phrasing literal."""

    name: str

    def combine(self, rates: list[float]) -> float: ...


class ActivationRateModel(Protocol):
    """Applies a team-wide activation-rate-up modifier to a base activation
    probability. Both `base_prob` and `team_rate_up` are already-converted
    values in the zero-to-one range — `apply` must never divide by 100,
    because the one percent-to-fraction boundary happens upstream in
    `scoring.py`."""

    name: str

    def apply(self, base_prob: float, team_rate_up: float) -> float: ...


# One ACTIVE score-up skill, decomposed for an aggregation that cannot be
# performed after its two halves have been multiplied together:
# `(bucket_key, magnitude_rate, occupancy)`.
#
# `magnitude_rate` is the skill's magnitude as a fraction, already carrying its
# family weight and any Score Support multiplying it. `occupancy` is
# `min(1, duration / interval) * P(activation)` — the fraction of the run the
# effect is live, in `[0, 1]`. Their product is exactly the per-card rate the
# summed model contributes, so `SummedActiveSkills` reproduces it.
#
# `bucket_key` partitions the terms into the game's ACTIVE and SPECIAL
# accumulators. See `scoring.SCORE_BUCKET_ACTIVE` / `SCORE_BUCKET_SPECIAL`, and
# `scoring._score_up_implements_boost` for which of the two `combine` calls
# (ratio or `B`) a given term is handed to.
#
# `exclusive` is the DEPENDENCE STRUCTURE of this term's live window against the
# other terms **in its own bucket**, and it is the difference between two
# expectations of the same maximum:
#
#   False  the window may overlap its bucket-mates' windows, and does so
#          INDEPENDENTLY. `E[max]` discounts each term by the chance a bigger
#          one is already live.
#   True   the window is disjoint from every other `exclusive` window in the
#          bucket -- at most one of them is live at any instant, so no discount
#          applies between them, and the time-average of their maximum is the
#          plain sum of `magnitude x occupancy`.
#
# It is a property of what the OCCUPANCY MEANS, not of the skill. The same
# SPECIAL slot is `exclusive` when its occupancy is the run-average fraction
# `scoring._special_occupancy` computes -- the chart fires it once, in its own
# deck slot's `#mmm0B` window, and those five windows do not overlap
# (`scoring._windows_are_mutually_exclusive` carries the measurement) -- and it
# is NOT exclusive when the occupancy is `song.py`'s instantaneous liveness,
# where the real cue windows sit on a real time axis and an overlap is VISIBLE
# rather than assumed away. That is why the flag travels on the term rather than
# being keyed off `bucket_key`: a corpus-level fact must not overrule ground
# truth in the one module that holds it.
#
# `interval` is the skill's ACTIVE cooldown period as an
# `int`, or `0` when the slot has no interval axis at all (SPECIAL, always-on,
# COSTUME -- structurally absent / `UNKNOWN`). It is the LATTICE the effect rolls
# on. The shipped `max`/`sum` combiners IGNORE it -- they read only
# `(bucket, magnitude, occupancy[, exclusive])` -- so their output is unchanged
# by its presence. Only `ExpectedMaximumActiveSkillByInterval` reads it, to
# partition the E[max] walk by `(bucket, interval)`.
#
# `duration` and `probability` (trailing elements 5 and 6) are
# the skill's effect duration in seconds and its RESOLVED activation probability
# (tier x rate-up). Together with `interval` they are the three numbers the
# per-instant `normalized_lattice` law needs and the occupancy product has
# destroyed. They are OPTIONAL trailing elements: the stacking models read
# indices 0..4 only, so a 5-tuple and a 7-tuple combine to the same number.
ActiveTerm = tuple[object, float, float, int, bool] | tuple[object, float, float, int, bool, float, float]


def _term_fields(term: ActiveTerm) -> tuple[object, float, float, int, bool]:
    """Unpack an `ActiveTerm` to
    `(bucket, magnitude_rate, occupancy, interval, exclusive)`."""
    return term[0], term[1], term[2], term[3], term[4]


def _expected_max_group(triples: list[tuple[float, float, bool]]) -> float:
    """The E[max] walk over ONE group of
    `(magnitude_rate, occupancy, exclusive)` triples, in descending magnitude.

    This is the exact within-bucket walk `ExpectedMaximumActiveSkill.combine`
    runs; it is factored out so `ExpectedMaximumActiveSkillByInterval`
    reuses it verbatim rather than reimplementing it. See
    `ExpectedMaximumActiveSkill` for the full derivation of the `free`/`used`
    bookkeeping and the two closed-form limits.

        free_i = PROD over bigger INDEPENDENT j of (1 - occ_j)
        used_i = SUM  over bigger EXCLUSIVE   j of occ_j

    `sorted` is stable, so equal magnitudes keep input order and are credited
    once between them."""
    free = 1.0
    used = 0.0
    total = 0.0
    for magnitude_rate, occupancy, exclusive in sorted(triples, key=lambda triple: -triple[0]):
        if exclusive:
            # Conditioning on this term being live already rules its bigger
            # exclusive siblings out; only the independent ones can still be
            # covering it.
            total += magnitude_rate * occupancy * free
            used += occupancy
        else:
            total += magnitude_rate * occupancy * free * (1.0 - used)
            free *= 1.0 - occupancy
    return total


class ActiveStackingModel(Protocol):
    """Combines the team's ACTIVE score-up skills into ONE pool rate — a
    pool-level operation, unlike `StackingModel`, which combines rates that
    were each already final. The distinction is the whole point: under
    `max` a card's contribution depends on which other cards are present, so
    it cannot be computed per card and added up afterwards.

    Returns a bare rate, like `StackingModel.combine` — the caller adds the
    1.0."""

    name: str

    def combine(self, terms: list[ActiveTerm]) -> float: ...


class TargetSelectionModel(Protocol):
    """Selects which `count` of `candidates` receive a targeted effect when
    more than `count` cards qualify (client logic present in no master
    table). `candidates` is
    `list[tuple[card_id, stat_value]]` — plain data, never a `Card` — so
    this module stays free of a `domain/` import."""

    name: str

    def select(self, candidates: list[tuple[str, int]], count: int) -> list[str]: ...


class AdditiveStacking:
    name = "additive"

    def combine(self, rates: list[float]) -> float:
        return sum(rates)


class MultiplicativeRateUp:
    """`apply` never divides by 100 — the percent-to-fraction conversion
    already happened upstream in `scoring.py`'s single named helper
    (`_as_rate`). Dividing
    again here would not raise; it would silently compound into a value
    roughly two orders of magnitude too small."""

    name = "multiplicative"

    def apply(self, base_prob: float, team_rate_up: float) -> float:
        return min(1.0, base_prob * (1.0 + team_rate_up))


class ExpectedMaximumActiveSkill:
    """`E[max]`: at any instant only the largest ACTIVE score-up effect that is
    live counts, so the pool is the expected value of that maximum.

    From the client: `Vision.LiveCore!ActiveSkillEffectDataCollection::CalcScoreUpRatio`
    @ rva `0x5efdb90` keeps two accumulators, folds each effect into one of
    them with `Math::Max` on a virtual predicate, and returns their sum
    (`addss xmm7, xmm8`). Two buckets, MAX within each bucket, then the two
    maxima added.

    Independently, this rule reproduces the deck screen's Active Skill line
    across nine captured decks to rms 0.38 pp with no free constant. A free global
    multiplier prefers 1.01 and buys 0.14 pp, so the model reproduces the
    absolute magnitude and not merely the shape.

    Within one bucket, term `i` is the maximum exactly when it is live and
    nothing bigger is, so `E[max] = SUM_i mag_i * P(i live AND nothing bigger
    live)` walking the bucket's terms in descending magnitude. What
    `P(nothing bigger live)` is depends on how those windows relate, and
    `ActiveTerm`'s `exclusive` flag is that relation:

        free_i = PROD over bigger INDEPENDENT j of (1 - occ_j)
        used_i = SUM  over bigger EXCLUSIVE   j of occ_j

        i independent :  mag_i * occ_i * free_i * (1 - used_i)
        i exclusive   :  mag_i * occ_i * free_i

    An exclusive term needs no `(1 - used_i)`: its bigger exclusive siblings
    cannot be live at the same instant it is, so conditioning on `i` live
    already excludes them. An independent term does need it, and `1 - used_i` is
    exact rather than approximate because a mutually exclusive family's
    "somebody is live" probability is the plain SUM of its members'.

    **Both limits are the familiar closed forms, which is why this is a strict
    generalisation and not a new rule:**

    - every term independent -> `used_i` stays `0.0`, the `(1 - used_i)` factor
      is a multiply by exactly `1.0`, and the expression is the classic
      `SUM_i mag_i occ_i PROD_{j before i} (1 - occ_j)` **bit for bit**, which
      keeps scorer parity intact.
    - every term exclusive -> `free_i` stays `1.0` and the expression collapses
      to `SUM_i mag_i occ_i`, the plain sum. Which is correct: over disjoint
      windows the time-average of a maximum IS the sum of the pieces, because
      the pieces never compete.

    `sorted` is stable, so equal magnitudes keep their input order and are
    credited exactly once between them — which is why the running `free`
    multiplies in every earlier term rather than only the strictly-larger ones.
    The strict `PROD_{j: mag_j > mag_i}` phrasing is the tie-free special
    case and double-counts a tie.

    Determinism and `score_card_set`/`score_fast` bit-parity both rest on that stability plus the callers building
    `terms` in `resolve_team_gates` order — the same invariant
    the rest of `scoring.py` already relies on.

    The bucket partition, from the client, is `LiveSkillEffectData
    .isSpecialSkill` — the two buckets are ACTIVE and SPECIAL
    (`scoring.SCORE_BUCKET_ACTIVE` / `SCORE_BUCKET_SPECIAL`). This model does
    not care which axis it is; it maximises within each key it is handed and
    adds the results, exactly as `addss xmm7, xmm8` does.

    ⚠ **This model is called twice per team and the two calls carry different
    populations.** `scoring._score_up_pool` calls it once over the ratio terms
    (ACTIVE-bucket only, after the SPECIAL collapse) and once over the boost
    terms (`B`, both buckets). That is not a redundancy: the game inlines
    `CalcScoreUpBoostByRatioBase` verbatim into `CalcScoreUpRatio`, so one rule
    applied twice is the faithful implementation. Which population a term joins
    is `scoring._score_up_implements_boost`'s decision, never this class's.

    ⚠ **This is `E[max]` within ONE bucket, and it is still not `E[(1 + B) x A]`.**
    `scoring._score_up_pool` multiplies this function's answer over the boost
    population by its answer over the ratio population, which is exact only if
    `B(t)` and `A(t)` are uncorrelated in time. That second, BETWEEN-bucket
    covariance is measured and dismissed in `_score_up_pool`'s closing
    paragraph; this class answers only for the within-bucket one."""

    name = "max"

    def combine(self, terms: list[ActiveTerm]) -> float:
        buckets: dict[object, list[tuple[float, float, bool]]] = {}
        for term in terms:
            # The `interval` element is READ AND DISCARDED here on purpose: this
            # model pools every same-bucket ACTIVE term into ONE independent walk
            # regardless of lattice, which is the shipped, ranking-default rule.
            bucket, magnitude_rate, occupancy, _interval, exclusive = _term_fields(term)
            buckets.setdefault(bucket, []).append((magnitude_rate, occupancy, exclusive))
        total = 0.0
        # dict preserves insertion order, so bucket order is first-appearance
        # order — fixed, not hash-dependent, which is what keeps the float
        # accumulation reproducible across runs.
        for triples in buckets.values():
            total += _expected_max_group(triples)
        return total


class TopNByBuffedStat:
    """Ranks `candidates` by the value they were built with, descending — the
    base value of the stat(s) the effect raises (`targets.resolve_target_card_ids`;
    690/690 against the server) — and
    returns the first `count` card ids, breaking an EXACT tie by DECK SLOT ORDER — the order
    `candidates` were built in, which `targets.resolve_target_card_ids` fixes to
    `cards`/`team.members` order.

    **The server keeps deck slot order on an exact total tie** (earlier
    slot wins). The sort is therefore STABLE on `-stat_value` alone — no `card_id`
    secondary key — so tied candidates keep the input (slot) order they arrived in,
    matching the server. This is the same rule the deck-screen Overall Power
    DISPLAY uses (`scoring._SLOT_ORDER_TIE_BREAK`, 100 % bit-exact over
    `bonus_extract_db.jsonl`); the RANKING objective uses it too, so
    `total_score` agrees with the server on the rare tie decks.

    Determinism rests on the input order being deterministic slot order —
    `Team` guarantees it, and every caller (`score_fast`, `score_card_set`, the
    beam's canonical sorted-by-id member tuples) presents members in a fixed order —
    so a `sorted` that is stable (Python's is, Timsort) reproduces the same
    selection across runs. A `(-stat_value, card_id)` key would break the tie
    toward the lexicographically-smaller id instead of the slot-earlier member,
    which the server contradicts."""

    name = "top_n_by_stat"

    def select(self, candidates: list[tuple[str, int]], count: int) -> list[str]:
        ordered = sorted(candidates, key=lambda candidate: -candidate[1])
        return [card_id for card_id, _stat_value in ordered[:count]]


# The only place in the codebase where a model name string becomes a
# strategy instance. `scoring.py` and `scoring_config.py` both resolve
# model choices through exactly these two dicts — never through an
# `if name == "additive"` branch. Keys must be the same strings the
# `--model` and activation-rate CLI flags carry.
STACKING_MODELS: dict[str, StackingModel] = {
    "additive": AdditiveStacking(),
}

ACTIVATION_RATE_MODELS: dict[str, ActivationRateModel] = {
    "multiplicative": MultiplicativeRateUp(),
}

TARGET_SELECTION_MODELS: dict[str, TargetSelectionModel] = {
    "top_n_by_stat": TopNByBuffedStat(),
}

# Deliberately NOT a `config.json` key, for the same reason as
# `REFERENCE_RUN_SECONDS`: sum-versus-max is not one of the "genuinely
# unverified semantics" the other three registries exist for. It is read
# from the game's own code and checked against nine captured decks, so the
# user is not asked to pick. The registry exists so the refuted
# alternative stays runnable under test (`dataclasses.replace(cfg,
# active_stacking_model=ACTIVE_STACKING_MODELS["sum"])`) and so
# `ScoreBreakdown` can name which rule produced a total, not so a ranking can
# be tuned. Adding it to `config.json` would also report a defaulted value
# with `"file"` provenance for every existing config, which is exactly the
# failure `load_scoring_config` is written to prevent.
ACTIVE_STACKING_MODELS: dict[str, ActiveStackingModel] = {
    "max": ExpectedMaximumActiveSkill(),
}

# The shipped rule: the expected maximum of the co-live ACTIVEs.
DEFAULT_ACTIVE_STACKING: ActiveStackingModel = ACTIVE_STACKING_MODELS["max"]


# Import-time guarantee: a rename of any registry's key or its instance's
# `name` attribute cannot silently desynchronize the two.
assert all(key == model.name for key, model in STACKING_MODELS.items()), (
    "STACKING_MODELS key must equal the mapped instance's own name attribute"
)
assert all(key == model.name for key, model in ACTIVATION_RATE_MODELS.items()), (
    "ACTIVATION_RATE_MODELS key must equal the mapped instance's own name attribute"
)
assert all(key == model.name for key, model in TARGET_SELECTION_MODELS.items()), (
    "TARGET_SELECTION_MODELS key must equal the mapped instance's own name attribute"
)
assert all(key == model.name for key, model in ACTIVE_STACKING_MODELS.items()), (
    "ACTIVE_STACKING_MODELS key must equal the mapped instance's own name attribute"
)
assert DEFAULT_ACTIVE_STACKING is ACTIVE_STACKING_MODELS[DEFAULT_ACTIVE_STACKING.name], (
    "DEFAULT_ACTIVE_STACKING must be one of the registered instances, so "
    "ScoreBreakdown's reported model name always resolves back to it"
)
