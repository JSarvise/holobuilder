"""Pluggable scoring models: each semantic the game does not state outright is a
`typing.Protocol` with named implementations, never an `if model == ...` branch.

Three name-to-instance registries (`STACKING_MODELS`, `ACTIVATION_RATE_MODELS`,
`TARGET_SELECTION_MODELS`) are the only place a model name from `config.json` becomes a
strategy object; `ACTIVE_STACKING_MODELS` names the ACTIVE pool rule. The module is pure
float math and imports nothing from `domain/`: `TargetSelectionModel` works on
`(card_id, value)` pairs, built by `domain/targets.py`."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class StackingModel(Protocol):
    """Combines same-pool rate contributions (0.10 = +10 %) into one combined rate; the caller
    adds 1.0."""

    name: str

    def combine(self, rates: list[float]) -> float: ...


class ActivationRateModel(Protocol):
    """Applies a team-wide activation-rate-up to a base activation probability. Both arguments
    are fractions in [0, 1]; the percent conversion happens upstream (`scoring._as_rate`)."""

    name: str

    def apply(self, base_prob: float, team_rate_up: float) -> float: ...


# One ACTIVE score-up term, kept decomposed because a maximum cannot be taken after its
# halves are multiplied: `(bucket, magnitude_rate, occupancy, interval, exclusive
# [, duration, probability])`.
#
# - `bucket`: the game's ACTIVE or SPECIAL accumulator (`scoring.SCORE_BUCKET_*`).
# - `magnitude_rate`: the magnitude as a fraction, family weight and any Score Support
#   multiplier included.
# - `occupancy`: the fraction of the run the effect is live, in [0, 1]; the product of
#   the two is the term's summed rate.
# - `interval`: the ACTIVE cooldown as an int, 0 without one.
# - `exclusive`: the window is disjoint from every other exclusive window in its bucket,
#   so no `E[max]` discount applies between them and their time-averaged maximum is the
#   plain sum. False means the windows overlap independently. It describes what the
#   occupancy MEANS: a SPECIAL's run-average occupancy (`scoring._special_occupancy`) is
#   exclusive, because the chart's five cue windows do not overlap
#   (`scoring._windows_are_mutually_exclusive`), while `song.py`'s instantaneous
#   liveness is not, since real overlaps are visible there. So it travels on the term
#   rather than being keyed off `bucket`.
# - `duration`, `probability` (optional): the effect duration in seconds and the
#   resolved activation probability, which a per-instant lattice law needs and the
#   occupancy product has lost.
#
# The shipped combiner reads indices 0..4 and ignores `interval`, so a 5-tuple and a
# 7-tuple combine to the same number.
ActiveTerm = tuple[object, float, float, int, bool] | tuple[object, float, float, int, bool, float, float]


def _term_fields(term: ActiveTerm) -> tuple[object, float, float, int, bool]:
    """Unpack an `ActiveTerm` to `(bucket, magnitude_rate, occupancy, interval, exclusive)`."""
    return term[0], term[1], term[2], term[3], term[4]


def _expected_max_group(triples: list[tuple[float, float, bool]]) -> float:
    """The E[max] walk over one group of `(magnitude_rate, occupancy, exclusive)` triples, in
    descending magnitude (derivation in `ExpectedMaximumActiveSkill`):

        free_i = PROD over bigger INDEPENDENT j of (1 - occ_j)
        used_i = SUM  over bigger EXCLUSIVE   j of occ_j

    `sorted` is stable, so equal magnitudes keep input order and are credited once."""
    free = 1.0
    used = 0.0
    total = 0.0
    for magnitude_rate, occupancy, exclusive in sorted(triples, key=lambda triple: -triple[0]):
        if exclusive:
            # Conditioning on this term being live rules out its bigger exclusive
            # siblings; only the independent ones can still cover it.
            total += magnitude_rate * occupancy * free
            used += occupancy
        else:
            total += magnitude_rate * occupancy * free * (1.0 - used)
            free *= 1.0 - occupancy
    return total


class ActiveStackingModel(Protocol):
    """Combines the team's ACTIVE score-up terms into ONE pool rate. Unlike `StackingModel` it
    works on the whole pool: under a maximum a card's contribution depends on the others.
    Returns a bare rate; the caller adds 1.0."""

    name: str

    def combine(self, terms: list[ActiveTerm]) -> float: ...


class TargetSelectionModel(Protocol):
    """Selects which `count` of `candidates` (`(card_id, value)` pairs) receive a targeted
    effect when more than `count` qualify."""

    name: str

    def select(self, candidates: Sequence[tuple[str, float]], count: int) -> list[str]: ...


class AdditiveStacking:
    name = "additive"

    def combine(self, rates: list[float]) -> float:
        return sum(rates)


class MultiplicativeRateUp:
    """`min(1, base_prob * (1 + team_rate_up))`. Never divides by 100: the input is already a
    fraction, and a second conversion would silently shrink it a hundredfold."""

    name = "multiplicative"

    def apply(self, base_prob: float, team_rate_up: float) -> float:
        return min(1.0, base_prob * (1.0 + team_rate_up))


class ExpectedMaximumActiveSkill:
    """`E[max]`: at any instant only the largest live ACTIVE score-up effect in a bucket counts,
    so the pool is the expected value of that maximum, summed over buckets.

    The client's `ActiveSkillEffectDataCollection::CalcScoreUpRatio` (rva `0x5efdb90`) folds
    each effect into one of two accumulators with `Math::Max` and returns their sum. This is the
    run's rule. The deck screen's Active Skill line is a different figure, computed by the
    server with a normalised sum per second (`deck_panel`), and this class does not model it.

    Within a bucket, term `i` is the maximum when it is live and nothing bigger is. Walking in
    descending magnitude, with `ActiveTerm.exclusive` as the relation between windows:

        free_i = PROD over bigger INDEPENDENT j of (1 - occ_j)
        used_i = SUM  over bigger EXCLUSIVE   j of occ_j

        i independent :  mag_i * occ_i * free_i * (1 - used_i)
        i exclusive   :  mag_i * occ_i * free_i

    An exclusive term needs no `(1 - used_i)` (its exclusive siblings cannot be live with
    it); for an independent term `1 - used_i` is exact, since disjoint events' probabilities
    add. All independent gives the classic `SUM_i mag_i occ_i PROD_{j before i} (1 - occ_j)`
    bit for bit (the extra factor is exactly 1.0); all exclusive gives the plain sum.

    `sorted` is stable, so ties keep input order and `free` multiplies every earlier term
    (a strict `mag_j > mag_i` product would double-count a tie). Determinism and scorer
    parity rest on that plus callers building `terms` in `resolve_team_gates` order.

    `scoring._score_up_pool` calls this twice per team, over the ratio terms and over the
    boost terms (`B`), because the game inlines `CalcScoreUpBoostByRatioBase` into
    `CalcScoreUpRatio`. It answers for the within-bucket maximum only; the covariance between
    `B` and the ACTIVE maximum is discussed in `_score_up_pool`."""

    name = "max"

    def combine(self, terms: list[ActiveTerm]) -> float:
        buckets: dict[object, list[tuple[float, float, bool]]] = {}
        for term in terms:
            # `interval` is discarded: every same-bucket term joins one walk, whatever its
            # cooldown lattice.
            bucket, magnitude_rate, occupancy, _interval, exclusive = _term_fields(term)
            buckets.setdefault(bucket, []).append((magnitude_rate, occupancy, exclusive))
        total = 0.0
        # Buckets in first-appearance order (dict insertion order), so the float sum is
        # reproducible across runs.
        for triples in buckets.values():
            total += _expected_max_group(triples)
        return total


class TopNByBuffedStat:
    """The top `count` candidates by the value they were built with (the base value of the
    stat(s) the effect raises, see `targets.resolve_target_card_ids`), an exact tie broken by
    deck slot order.

    The server keeps slot order on a tie (the earlier slot wins), so the sort is stable on
    `-value` alone, with no `card_id` key; the input order is the deck's slot order
    (`cards`/`team.members`), fixed by every caller. A `(-value, card_id)` key would favour
    the smaller id, which the server contradicts. The deck-screen display uses the same rule
    (`scoring._SLOT_ORDER_TIE_BREAK`)."""

    name = "top_n_by_stat"

    def select(self, candidates: Sequence[tuple[str, float]], count: int) -> list[str]:
        ordered = sorted(candidates, key=lambda candidate: -candidate[1])
        return [card_id for card_id, _stat_value in ordered[:count]]


# The only place a model name string becomes a strategy instance (`scoring_config.py`
# resolves `config.json`'s model choices here). Keys equal each instance's `name`.
STACKING_MODELS: dict[str, StackingModel] = {
    "additive": AdditiveStacking(),
}

ACTIVATION_RATE_MODELS: dict[str, ActivationRateModel] = {
    "multiplicative": MultiplicativeRateUp(),
}

TARGET_SELECTION_MODELS: dict[str, TargetSelectionModel] = {
    "top_n_by_stat": TopNByBuffedStat(),
}

# Not a `config.json` key: the maximum is read from the game's code and checked against
# nine captured decks, so it is not a choice for the user. The registry lets
# `ScoreBreakdown` name the rule that produced a total, and a config key would report a
# defaulted value with `"file"` provenance, which `load_scoring_config` exists to
# prevent.
ACTIVE_STACKING_MODELS: dict[str, ActiveStackingModel] = {
    "max": ExpectedMaximumActiveSkill(),
}

# The shipped rule: the expected maximum of the co-live ACTIVEs.
DEFAULT_ACTIVE_STACKING: ActiveStackingModel = ACTIVE_STACKING_MODELS["max"]


# A registry key and its instance's `name` cannot drift apart.
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
