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

import json
from pathlib import Path
from typing import Protocol, Sequence

# The shipped deck-screen reference density (`NormalizedLatticeActiveSkill`).
# Data, not code: `data/deck_screen_density.json`, a 1-s-bin histogram that sums
# to 1, with its provenance in the file's own header. `load_deck_screen_density`
# is the only reader; the model takes the density by INJECTION (constructor
# argument) and only loads this file lazily when none was injected.
DECK_SCREEN_DENSITY_PATH = Path(__file__).resolve().parents[3] / "data" / "deck_screen_density.json"


# Float guard for `active_line_permil`: 35 of 6017 oracle decks are EXACT integer
# permil in rational arithmetic, and float64 noise pushes 16 of them over the
# next integer under a bare `ceil`. 1e-9 permil is eight orders
# below any real gap and well above float64 noise on a value <= 1000.
_ACTIVE_LINE_CEIL_EPSILON = 1e-9


def active_line_permil(rate: float) -> int:
    """The deck screen's integer [Active Skill] permil from a combiner's
    UNROUNDED rate: **CEIL, not round** (6017/6017 oracle decks in exact
    rational arithmetic; half-up lands 3053, floor 35). Lives here, at the
    boundary where
    the integer line is formed, so the combiners stay exact floats."""
    import math

    return math.ceil(rate * 1000.0 - _ACTIVE_LINE_CEIL_EPSILON)


def load_deck_screen_density(path: Path | str | None = None) -> tuple[float, ...]:
    """The 1-s-bin reference density `rho` from `path` (default: the shipped
    `data/deck_screen_density.json`). The document's `rho` list is returned as a
    tuple of floats; its `provenance` header is for the reader, not this code."""
    doc = json.loads(Path(path or DECK_SCREEN_DENSITY_PATH).read_text("utf8"))
    return tuple(float(x) for x in doc["rho"])


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
# destroyed. They are OPTIONAL trailing elements: `max`/`sum`/`max_lattice` read
# indices 0..4 only, so a 5-tuple (or the legacy 4-tuple) and a 7-tuple combine
# to the same number under them — `tests/test_active_normalized_lattice.py`
# pins that parity. Only `NormalizedLatticeActiveSkill` reads them; a term
# without them (or with `probability` 0) has no lattice and falls back to the
# shipped per-bucket `max` walk there.
ActiveTerm = tuple[object, float, float, int, bool] | tuple[
    object, float, float, int, bool, float, float
]


def _term_lattice(term: ActiveTerm) -> tuple[int, float, float] | None:
    """`(interval, duration, probability)` when the term carries a placeable
    lattice — a 7-tuple with a positive interval, duration and probability —
    else `None`. Reads the same positional slots `_term_fields` does, so the
    legacy shapes are tolerated rather than special-cased."""
    if len(term) < 7:
        return None
    interval, duration, probability = term[3], term[5], term[6]
    if not (interval > 0 and duration > 0.0 and probability > 0.0):
        return None
    return int(interval), float(duration), float(probability)


def _term_fields(term: ActiveTerm) -> tuple[object, float, float, int, bool]:
    """Unpack an `ActiveTerm` to
    `(bucket, magnitude_rate, occupancy, interval, exclusive)`.

    Tolerates the LEGACY 4-tuple `(bucket, magnitude, occupancy, exclusive)`
    without the `interval` element: such a tuple reads `interval` `0`, so it
    falls in the per-bucket "no lattice" group. The domain builders emit
    5-tuples; the hand-built tuples in the combiner unit tests are 4-tuples,
    and both must combine to
    the same number under `max`/`sum`. `bucket`/`magnitude`/`occupancy` are read
    positionally (indices 0..2) precisely so the two shapes share this one
    reader."""
    bucket, magnitude_rate, occupancy = term[0], term[1], term[2]
    if len(term) >= 5:
        return bucket, magnitude_rate, occupancy, term[3], term[4]
    return bucket, magnitude_rate, occupancy, 0, term[3]


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
    for magnitude_rate, occupancy, exclusive in sorted(
        triples, key=lambda triple: -triple[0]
    ):
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


class AdditivePercentagePoints:
    name = "pp"

    def apply(self, base_prob: float, team_rate_up: float) -> float:
        return min(1.0, base_prob + team_rate_up)


class MultiplicativeStacking:
    name = "multiplicative"

    def combine(self, rates: list[float]) -> float:
        total = 1.0
        for rate in rates:
            total *= 1.0 + rate
        return total - 1.0


class MultiplicativeRateUp:
    """`apply` never divides by 100 — the percent-to-fraction conversion
    already happened upstream in `scoring.py`'s single named helper
    (`_as_rate`). Dividing
    again here would not raise; it would silently compound into a value
    roughly two orders of magnitude too small."""

    name = "multiplicative"

    def apply(self, base_prob: float, team_rate_up: float) -> float:
        return min(1.0, base_prob * (1.0 + team_rate_up))


class SummedActiveSkills:
    """The duty-sum: every ACTIVE score-up skill contributes independently
    and the contributions add.

    **Refuted**, and kept only so it stays runnable and under test. An order
    test refutes it that no free constant can survive — reference duration, saturation curve, global
    calibration and skill level are all positive scales or monotone increasing
    transforms of the model's own output, and none of them can reorder two
    decks, yet this model orders two captured deck pairs the wrong way round.
    Against nine captured decks it misses by up to 14.23 pp of Score Bonus
    (rms 6.18) where `ExpectedMaximumActiveSkill` misses by 0.82 (rms 0.49).

    Ignores `bucket_key` entirely, which is not an omission: a sum over a
    partition equals the sum over the whole, so the partition is precisely the
    thing summation cannot see. It ignores `exclusive` for the same reason --
    a sum has no maximum whose dependence structure could matter. Note the
    consequence, which `tests/test_special_collapse.py` pins: over a bucket whose
    terms are ALL exclusive, this refuted model and `ExpectedMaximumActiveSkill`
    agree exactly, because the time-average of a maximum over disjoint windows
    IS the sum. The two only diverge where windows can overlap."""

    name = "sum"

    def combine(self, terms: list[ActiveTerm]) -> float:
        total = 0.0
        for term in terms:
            # `magnitude_rate` and `occupancy` are indices 1 and 2 in BOTH the
            # 5-tuple and the legacy 4-tuple; the `interval` element is
            # invisible to a sum, exactly as `bucket` and `exclusive` are.
            total += term[1] * term[2]
        return total


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
    `terms` in `Team.effective_skill_instances()` order — the same invariant
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
            # `_term_fields` also accepts the legacy 4-tuple the unit tests
            # build.
            bucket, magnitude_rate, occupancy, _interval, exclusive = _term_fields(term)
            buckets.setdefault(bucket, []).append(
                (magnitude_rate, occupancy, exclusive)
            )
        total = 0.0
        # dict preserves insertion order, so bucket order is first-appearance
        # order — fixed, not hash-dependent, which is what keeps the float
        # accumulation reproducible across runs.
        for triples in buckets.values():
            total += _expected_max_group(triples)
        return total


class ExpectedMaximumActiveSkillByInterval:
    """`E[max]` partitioned by cooldown lattice: the shipped `E[max]` walk within
    each `(bucket, interval)` group, and the group results SUMMED.

    ⛔ **Refuted — do not ship; kept only as a documented dead end.**
    The sum-by-interval-group model below holds ONLY at low occupancy. At
    3/4/5-card density the server computes ONE GLOBAL E[max] over the union of
    all active windows, not additive interval groups: a *different*-interval
    card is ALSO discounted (4×int17 + 1×int30 → server 221, this model's
    group-sum predicts 254), and the active bonus SATURATES at
    ceil(interval/duration). On real decks this model OVERSHOOTS (+245.9 vs
    +13.0 median Score-Bonus residual over 76 decks) because pure interval-ADD
    grows unbounded where the server saturates. `DEFAULT_ACTIVE_STACKING`
    stays `max` (the corpus-average independent E[max]). The `interval` element
    on `ActiveTerm` is harmless (the shipped `max`/`sum` combiners ignore it).
    The text below records the low-density observation the model rests on.

    On 230 board-free oracle decks at low density, the server's Active E[max]
    obeys a two-level law:

    - ACTIVE score-ups of DIFFERENT `interval` **ADD** (plain sum of
      `magnitude * occupancy`, within ~1 permil). They roll on unsynchronised
      cooldown lattices, so at a random instant the chance the larger is live is
      not what suppresses the smaller — the two contribute as two separate
      maxima.
    - ACTIVE score-ups of the SAME `interval` share one lattice, overlap, and
      take the ordinary within-group E[max] discount.

    The shipped `ExpectedMaximumActiveSkill` pools ALL same-bucket ACTIVE terms
    into one independent free-product walk, which applies the same-lattice
    discount to different-lattice pairs too — its largest systematic error.
    Measured example: two actives int17 and int33, server 138 permil ≈ the 139
    permil sum of the two solos; the shipped model gives 112 (~29 permil low).

    Implementation is exactly `ExpectedMaximumActiveSkill`'s, with a finer
    partition key: `(bucket, interval)` instead of `bucket`. It reuses
    `_expected_max_group` verbatim — the within-group walk is NOT reimplemented —
    and sums the groups, so different intervals add and same intervals discount.
    A term with `interval` 0/None (SPECIAL, always-on) forms the per-bucket
    "no lattice" group, i.e. every such term in a bucket shares one group and
    behaves exactly as under `max`. The client's two-bucket structure survives:
    ACTIVE and SPECIAL still never share a group because `bucket` is part of the
    key.

    ⚠ **KNOWN UNMODELED RESIDUAL — within-group saturation.** When several
    same-interval effects pile onto one lattice the server's E[max] does not grow
    without bound; it saturates / truncates at roughly **2.1–2.7 window-widths**,
    and the exact point is PHASE-dependent (where the cooldown windows sit
    relative to each other and the run). This model does NOT apply a saturation
    constant: the value is not bit-exact, it is measured only as a band, and
    hardcoding it would trade a known undershoot on cross-interval pairs (which
    this class fixes) for a fresh, unvalidated error on same-interval stacks.
    Cross-interval ADD is modelled, within-group saturation is left to the
    ordinary `_expected_max_group` discount and flagged here.

    Opt-in only. `DEFAULT_ACTIVE_STACKING` stays `max` (ranking uses the
    corpus-average independent E[max]); this variant is selected explicitly via
    `dataclasses.replace(cfg, active_stacking_model=ACTIVE_STACKING_MODELS[
    "max_lattice"])`."""

    name = "max_lattice"

    def combine(self, terms: list[ActiveTerm]) -> float:
        groups: dict[tuple[object, int], list[tuple[float, float, bool]]] = {}
        for term in terms:
            bucket, magnitude_rate, occupancy, interval, exclusive = _term_fields(term)
            groups.setdefault((bucket, interval), []).append(
                (magnitude_rate, occupancy, exclusive)
            )
        total = 0.0
        # Insertion-ordered dict -> reproducible float accumulation, as in
        # `ExpectedMaximumActiveSkill`. Different-interval groups ADD (the
        # measured law); within each group the shipped walk discounts.
        for triples in groups.values():
            total += _expected_max_group(triples)
        return total


class NormalizedLatticeActiveSkill:
    """The deck screen's Score-Bonus ACTIVE line as a per-instant NORMALISED SUM
    over the cooldown lattices, integrated against a reference note density.

    Bit-exact on 6017/6017 SaveDeck->GetDeck oracle decks (train 4778/4778,
    held-out 1239/1239) in exact rational arithmetic, the server's ACTIVE line
    obeys

        windows_i = [k*cool_i, k*cool_i + dur_i),  k >= 1     (all lattices share origin 0,
                                                              cool/dur integer seconds)
        live at integer t  iff  k*cool_i <= t < k*cool_i + dur_i
        value(t)  = SUM_{i live at t} m_i P_i / max(1, SUM_{i live at t} P_i)
        Active    = CEIL( SUM_{t=1..200} value(t) / 200 x 1000 )

    with `m_i` the override-resolved magnitude (a rate here: 0.40 for 40 %),
    `P_i` the skill's activation probability (Low/Medium/High = 0.37/0.46/0.55,
    skill level 2 iff bloom >= 1 — both resolved upstream by the domain). The
    density `rho` is FLAT 1/200 over integer seconds 1..200 (bin 0 empty;
    `data/deck_screen_density.json`) — kept as injectable data rather than a
    constant so a test can weight single bins. This class returns the UNROUNDED
    rate; the CEIL belongs to the boundary where the integer permil line is
    formed (`active_line_permil`), not to the combiner. Sampling once per
    integer second is exactly the 1-s-bin walk below: bin `t` is live iff
    `k*cool <= t < k*cool + dur`.

    What the law says, in words: at any instant the effects that are live share
    the line in proportion to their probability mass, and the line SATURATES once
    that mass reaches 1 — two same-lattice 0.5-probability cards add, a third
    turns the sum into a probability-weighted mean. Different lattices interact
    only on the bins where they actually overlap. This is the measured law that
    `ExpectedMaximumActiveSkillByInterval` (`max_lattice`, refuted) approximates
    as additive interval groups, and that the shipped `max` walk approximates as
    one corpus-average independent E[max].

    Term shape. Reads the 7-tuple `ActiveTerm`'s `(interval, duration,
    probability)` via `_term_lattice`; `occupancy` (index 2) is NOT read for a
    lattice term — the law places the windows itself. A term with no placeable
    lattice (SPECIAL / always-on / a legacy 4- or 5-tuple / probability 0, i.e. a
    suppressed skill) is combined by the shipped per-bucket `_expected_max_group`
    walk exactly as `max` does, and that part is ADDED to the lattice part. So
    the `B` (Score Support) population, which has no lattice, is bit-identical to
    `max`, and the SPECIAL bucket keeps the client's structure. Lattice terms are
    partitioned by `bucket` too (ACTIVE and SPECIAL never share a normalisation).

    ⚠ Scope. (a) The lattice period used is the term's `interval`
    element — the master cooldown as `int` — so a board `Act.CD` cooldown
    reduction is NOT applied to the lattice here (it is not known how the
    server's panel places a shortened lattice; every oracle deck carried none). (b) `probability`
    is the domain's resolved value, so a team activation-rate-up DOES move it;
    the oracle decks the law was fitted on carried none. (c) Magnitudes arrive
    already multiplied by the support multiplier `(1 + B)` in `_score_up_pool`,
    as under `max`.

    Opt-in only: `DEFAULT_ACTIVE_STACKING` stays `max` (the ranking objective is
    untouched). Select it on the deck-screen reconcile path via
    `dataclasses.replace(cfg, active_stacking_model=ACTIVE_STACKING_MODELS[
    "normalized_lattice"])`. The
    density is injectable (`density=`) so tests pass a synthetic one; the
    registry instance loads the shipped file lazily on first use."""

    name = "normalized_lattice"

    def __init__(self, density: Sequence[float] | None = None) -> None:
        self._density: tuple[float, ...] | None = (
            None if density is None else tuple(float(x) for x in density)
        )

    @property
    def density(self) -> tuple[float, ...]:
        if self._density is None:
            self._density = load_deck_screen_density()
        return self._density

    def combine(self, terms: list[ActiveTerm]) -> float:
        rho = self.density
        bins = len(rho)
        lattice: dict[object, list[tuple[float, int, float, float]]] = {}
        plain: dict[object, list[tuple[float, float, bool]]] = {}
        for term in terms:
            bucket, magnitude_rate, occupancy, _interval, exclusive = _term_fields(term)
            placed = _term_lattice(term)
            if placed is None:
                plain.setdefault(bucket, []).append((magnitude_rate, occupancy, exclusive))
            else:
                interval, duration, probability = placed
                lattice.setdefault(bucket, []).append(
                    (magnitude_rate, interval, duration, probability)
                )
        total = 0.0
        # Insertion-ordered dicts -> reproducible float accumulation.
        for group in lattice.values():
            mass = [0.0] * bins  # SUM P_i over skills live in the bin
            value = [0.0] * bins  # SUM m_i P_i over the same
            for magnitude_rate, interval, duration, probability in group:
                weighted = magnitude_rate * probability
                k = 1
                while k * interval < bins:
                    start = k * interval
                    end = min(bins, int(k * interval + duration))
                    for b in range(start, end):
                        mass[b] += probability
                        value[b] += weighted
                    k += 1
            for b in range(bins):
                if mass[b] > 0.0:
                    total += rho[b] * value[b] / max(1.0, mass[b])
        for triples in plain.values():
            total += _expected_max_group(triples)
        return total


class ExclusiveNormalizedActiveSkill:
    """The EXCLUSIVE co-firing hypothesis (H2) for ONE INSTANT of a real run:
    co-live ACTIVEs share a single roll rather than each throwing their own.

        value = SUM_i m_i p_i / max(1, SUM_i p_i)          per bucket

    where `p_i` is the skill's activation probability AT THIS INSTANT — which is
    exactly what `song._ratio_at` puts in the `occupancy` slot, and is NOT what
    the run-average ranking scorer puts there. **This model is only meaningful
    against instantaneous terms**, which is why it is registered in
    `ACTIVE_INSTANT_MODELS` and reached through `ScoringConfig
    .active_instant_model` rather than through `active_stacking_model`: the
    latter is also consulted by `score_card_set`, where `occupancy` means
    "fraction of the run live" and this arithmetic would be nonsense.

    The rule itself is not a guess: it is the law measured bit-exact
    (6017/6017 oracle decks) on the deck screen's Score-Bonus ACTIVE line, where
    the server evaluates a deck against a synthetic 200-second timeline
    (`NormalizedLatticeActiveSkill`, same formula with the lattice placed by the
    model instead of by a chart). It is a HYPOTHESIS for the in-run scorer,
    which by default uses the client's `MathF.Max` over independent rolls that
    `ExpectedMaximumActiveSkill` implements; the deck-screen line may be a
    closed-form PROXY the server shows for a stochastic run. The two differ by
    **+1.9 .. +2.7 % of total score** on real charts, so this stays opt-in and
    `active_instant_model` defaults to `None`.

    Two properties worth knowing before reading a score produced by it:

    - It is NOT uniformly below `E[max]`. While `SUM p <= 1` the effects **add**,
      because under a shared roll a smaller skill firing is not evidence that a
      bigger one was suppressed. That is where the +2-3 % comes from.
    - It is **not monotone**. Past saturation the instant is a probability-
      weighted MEAN, so a weak co-live skill DILUTES a strong one. `E[max]` can
      never lose value from an extra skill; this can. Any future trace showing a
      weak card lowering a real score is evidence for this hypothesis.

    Buckets are kept separate exactly as `ExpectedMaximumActiveSkill` keeps
    them (`LiveSkillEffectData.isSpecialSkill`, from the client): a SPECIAL fires on the
    chart's cue and never joins an ACTIVE roll's denominator. The `max(1, .)`
    clamp is what stops a lone certain effect being divided by its own
    probability. A term with `occupancy` 0 — a gate that did not fire — adds
    nothing to either the numerator or the denominator, so a failed condition
    cannot crowd out a skill that did fire."""

    name = "exclusive"

    def combine(self, terms: list[ActiveTerm]) -> float:
        # `(numerator, denominator)` per bucket. Insertion-ordered dict, as in
        # every other combiner here, so the float accumulation is reproducible.
        buckets: dict[object, list[float]] = {}
        for term in terms:
            bucket, magnitude_rate, occupancy, _interval, _exclusive = _term_fields(term)
            slot = buckets.setdefault(bucket, [0.0, 0.0])
            slot[0] += magnitude_rate * occupancy
            slot[1] += occupancy
        total = 0.0
        for numerator, mass in buckets.values():
            if mass > 0.0:
                total += numerator / max(1.0, mass)
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
    "multiplicative": MultiplicativeStacking(),
}

ACTIVATION_RATE_MODELS: dict[str, ActivationRateModel] = {
    "pp": AdditivePercentagePoints(),
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
    "sum": SummedActiveSkills(),
    "max": ExpectedMaximumActiveSkill(),
    "max_lattice": ExpectedMaximumActiveSkillByInterval(),
    "normalized_lattice": NormalizedLatticeActiveSkill(),
}

# The shipped rule. `"max"` because `"sum"` is refuted (see both classes'
# docstrings), not because it is newer.
DEFAULT_ACTIVE_STACKING: ActiveStackingModel = ACTIVE_STACKING_MODELS["max"]

# How co-live ACTIVEs combine AT ONE INSTANT of a real run. A SEPARATE registry from `ACTIVE_STACKING_MODELS` on purpose, even
# though the values share a type: `active_stacking_model` is consulted by the
# run-average ranking scorer too, where `occupancy` means a fraction of the run
# and the exclusive law does not apply. Only `song._ratio_at` reads this one.
#
# `"independent"` is the SHIPPED INSTANCE, not a second implementation of it, so
# naming the hypothesis explicitly and leaving the default alone are the same
# call on the same object — which is what makes "the default is bit-identical"
# a structural fact rather than a measurement.
ACTIVE_INSTANT_MODELS: dict[str, ActiveStackingModel] = {
    "independent": ACTIVE_STACKING_MODELS["max"],
    "exclusive": ExclusiveNormalizedActiveSkill(),
}

# Import-time guarantee: a rename of any registry's key or its instance's
# `name` attribute cannot silently desynchronize the two.
assert all(key == model.name for key, model in STACKING_MODELS.items()), (
    "STACKING_MODELS key must equal the mapped instance's own name attribute"
)
assert all(key == model.name for key, model in ACTIVATION_RATE_MODELS.items()), (
    "ACTIVATION_RATE_MODELS key must equal the mapped instance's own name "
    "attribute"
)
assert all(key == model.name for key, model in TARGET_SELECTION_MODELS.items()), (
    "TARGET_SELECTION_MODELS key must equal the mapped instance's own name "
    "attribute"
)
assert all(key == model.name for key, model in ACTIVE_STACKING_MODELS.items()), (
    "ACTIVE_STACKING_MODELS key must equal the mapped instance's own name "
    "attribute"
)
assert DEFAULT_ACTIVE_STACKING is ACTIVE_STACKING_MODELS[DEFAULT_ACTIVE_STACKING.name], (
    "DEFAULT_ACTIVE_STACKING must be one of the registered instances, so "
    "ScoreBreakdown's reported model name always resolves back to it"
)
