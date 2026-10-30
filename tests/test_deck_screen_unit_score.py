"""The deck screen's Unit Score, against the game's own ten captures.

`total_score` is a HYBRID — the deck-screen power assembly wearing the in-run
score stage. `CalcScore` multiplies the board score bonus as a trailing factor
(read from the binary, and correct for the in-run quantity); the deck screen
ADDS it into one summed Score Bonus. `deck_screen_unit_score` assembles the
additive form so the ranking objective is left alone.

## Why the oracle rows are embedded here

`dumps/` is gitignored (~11 GB), so a test may not read `deck_oracle.json`.
The three aggregate numbers per capture are transcribed below instead — the
same discipline `test_board.py` uses for Ina's 27 red nodes. They are the
game's own output and are ground truth, not chosen fixtures.

## What is proven and what is not

**Proven: the formula.** Given the game's own term values, all three
identities hold on 10/10 — see the three tests below.

**NOT proven, and the tests say so: the base.** No stat source on disk
reproduces the server's Member Parameter line; observed ratios are
0.880 / 1.147 / 0.936 / 0.924 / 0.949, on *both* sides of 1.0. Roughly -12.9 %
to +13.7 %, larger than the shape error this module removes. So a correct
formula here does NOT mean the number matches the player's deck screen, and
`test_the_docstring_says_the_base_is_still_wrong` pins that the code itself
says so.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from hbuilder.domain.scoring import (
    UNIT_SCORE_K,
    ScoreBreakdown,
    deck_screen_unit_score,
)

# --------------------------------------------------------------------------
# The ten `Live/GetDeck` captures, transcribed from `dumps/deck_oracle.json`.
#
# `(overall_power, score_bonus_permil, unit_score)` — all three are the GAME's
# numbers, read off the wire. Five distinct compositions each seen twice, three
# hours apart, which is why the rows pair up.
# --------------------------------------------------------------------------
ORACLE: tuple[tuple[int, int, int], ...] = (
    (134891, 1669, 733492),
    (80768, 650, 271511),
    (100769, 1542, 521875),
    (56322, 597, 183252),
    (55925, 814, 206684),
    (55925, 823, 207710),
    (56322, 605, 184170),
    (100769, 1554, 524339),
    (80768, 658, 272828),
    (134891, 1681, 736790),
)


def _breakdown(**overrides) -> ScoreBreakdown:
    base = ScoreBreakdown(
        total_score=0.0,
        base_stat_sum=1000.0,
        effective_stat_sum=1200.0,
        combined_stat_rate=0.20,
        combined_score_rate=0.50,
        combined_support_rate=0.0,
        board_score_rate=0.10,
        board_power=300.0,
        team_rate_up=0.0,
        contributions=(),
        incomplete_skills=(),
        stacking_model_name="x",
        activation_rate_model_name="y",
        active_stacking_model_name="z",
        run_state=None,
        memory_bonus=50,
        member_power_up=10,
    )
    return dataclasses.replace(base, **overrides)


# ==========================================================================
# 1. The formula, against the game
# ==========================================================================


def test_the_identity_reproduces_every_capture_EXACTLY():
    """`unit_score = ceil( overall_power x (1 + permil/1000) x K )`.

    **Residue exactly 0 on 10/10.** Two pitfalls make it look like +/-1:

    1. Estimating K as `mean(unit / (power x factor))`. Because the game
       CEILINGs, every sample is biased upward by ~1/2 a point, so the mean is
       biased upward by ~1.5 ppm (giving `K = 2.0373445`).
    2. With a K that is too high, `ceil` overshoots, which makes `ceil` look
       like the WORST rounding rule when the real fault is the fitted constant.

    Solved as a feasibility problem instead of a fit: each capture constrains K
    to `(unit-1)/X < K <= unit/X`, and the 10 intervals INTERSECT. A fit has one
    answer whatever the data; an intersection is empty when the shape is wrong,
    which is what makes this evidence."""
    residues = [
        unit - math.ceil(power * (1.0 + permil / 1000.0) * UNIT_SCORE_K)
        for power, permil, unit in ORACLE
    ]
    assert residues == [0] * 10, residues


def test_ceiling_is_the_rule_and_the_others_are_now_refuted():
    """`floor` is uniformly +1 and `round` is 0-or-+1. That asymmetry is the
    signature of a `Ceiling` over a product that is never an exact integer, and
    it is the same rule `CalcScore` ends in (`Math::Ceiling` @`0x75799e0`) — so
    the deck screen and the in-run score agree on rounding.

    ⚠ Read with `test_K_is_a_feasible_interval_not_a_fitted_point`: the rule and
    the constant were only separable together. Testing a rounding rule against a
    constant fitted under a different rule measures the pair, not the rule."""

    def residues(rule):
        return [
            unit - rule(power * (1.0 + permil / 1000.0) * UNIT_SCORE_K)
            for power, permil, unit in ORACLE
        ]

    assert residues(math.ceil) == [0] * 10
    assert residues(math.floor) == [1] * 10  # uniformly short by one
    assert set(residues(lambda x: int(round(x)))) == {0, 1}


def test_K_is_a_feasible_interval_not_a_fitted_point():
    """K is only pinned to ~6.6e-07 relative, and the shipped value is a point
    inside that interval — not a measurement of six-decimal accuracy.

    ⛔ **The mean-fitted `K = 2.0373445` lies OUTSIDE and is refuted**, high by 1.8 ppm.

    ⛔ **K is NOT a constant the game ships.** All 163 `Setting` fields are
    integers and the unit convention is permil/permyriad; `2037` permil and
    `20373` permyriad both fall outside this interval, so no field in the game's
    own units could express K. A whole-image sweep of all 182 MB at every byte
    offset, for float32, float64 and integer encodings, returned zero aligned
    hits, as did all 372 master tables. **K is a server-side computed product**,
    which is why grepping master tables was never going to find it.

    So this constant may move again, and any caller must treat it as provisional
    — that is what this test exists to say."""
    from fractions import Fraction

    lo, hi = Fraction(0), Fraction(10)
    for power, permil, unit in ORACLE:
        x = Fraction(power) * Fraction(1000 + permil, 1000)
        lo = max(lo, Fraction(unit - 1) / x)
        hi = min(hi, Fraction(unit) / x)

    assert lo < hi, "the ceil model is infeasible — the identity's SHAPE is wrong"
    assert lo < Fraction(repr(UNIT_SCORE_K)) <= hi
    assert not (lo < Fraction("2.0373445") <= hi), "the refuted value came back"
    assert float((hi - lo) / lo) < 1e-6


def test_the_multiplicative_shape_is_wrong_for_this_quantity_and_by_how_much():
    """Why a separate function exists at all.

    Using `CalcScore`'s multiplicative shape to reproduce the deck-screen
    number costs the cross term `combined_score_rate x board_score_rate`. The
    board's own share of the permil is what drives it, so the error is
    TEAM-DEPENDENT — which is what makes it a wrong formula rather than a
    missing constant, and therefore not something a single fudge factor could
    absorb.

    Reproduces the error band on the two extreme captures: the
    board carried 215 of 1681 permil on one deck and 56 of 650 on another."""

    def overstatement(board_permil: int, rest_permil: int) -> float:
        additive = 1.0 + (board_permil + rest_permil) / 1000.0
        multiplicative = (1.0 + rest_permil / 1000.0) * (1.0 + board_permil / 1000.0)
        return 100.0 * (multiplicative / additive - 1.0)

    assert overstatement(215, 1466) == pytest.approx(11.76, abs=0.01)
    assert overstatement(56, 594) == pytest.approx(2.02, abs=0.01)

    # The point: the two ends differ by ~9.7 pp, so no constant absorbs it.
    assert overstatement(215, 1466) - overstatement(56, 594) > 9.0


# ==========================================================================
# 2. The assembly, from one of our own breakdowns
# ==========================================================================


def test_the_bonus_lines_are_added_not_multiplied():
    """The one line that distinguishes this from `total_score`.

    Overall Power = 1200 + 300 + 50 + 10 = 1560.
    Score Bonus   = (0.50 + 0.10) x 1000 = 600 permil  <- ADDED.
    Unit Score    = 1560 x 1.600 x 2.03734 = 5085.2006 -> 5086 (CEILED by
    the OUTPUT layer, not here).

    `total_score` on the same breakdown MULTIPLIES: (1200 + 300) x 1.50 x 1.10
    — a different quantity, and deliberately unchanged by this module."""
    result = deck_screen_unit_score(_breakdown())

    assert result.overall_power == pytest.approx(1560.0)
    assert result.score_bonus_permil == pytest.approx(600.0)
    # EXACT float here; the output layer rounds. `domain/scoring.py` may not
    # round at all — `test_scoring_ev.py`'s structural guard — because
    # rounding inside an EV computation is a silent correctness bug.
    assert result.unit_score == pytest.approx(1560.0 * 1.600 * UNIT_SCORE_K)
    assert result.unit_score == pytest.approx(5085.2006, abs=0.001)
    assert round(result.unit_score) == 5085
    assert result.complete is True
    assert result.missing == ()

    # Explicitly: the additive and multiplicative readings differ here, so this
    # test would still pass if someone "simplified" one into the other only if
    # they were equal — they are not.
    multiplicative = 1560.0 * 1.50 * 1.10 * UNIT_SCORE_K
    assert round(multiplicative) != result.unit_score


def test_an_unsupplied_account_scalar_is_named_not_silently_dropped():
    """A sum that omits a term the game includes is not the game's Overall
    Power. `None` means the player has not entered the scalar — never 0 — so
    the term is left out AND named, and `complete` goes false."""
    result = deck_screen_unit_score(_breakdown(memory_bonus=None))
    assert result.overall_power == pytest.approx(1510.0)  # 50 dropped
    assert result.missing == ("up_by_poster_collect",)
    assert result.complete is False

    both = deck_screen_unit_score(
        _breakdown(memory_bonus=None, member_power_up=None)
    )
    assert both.missing == ("up_by_poster_collect", "card_training_evaluation")
    assert both.complete is False

    # A supplied ZERO is not the same thing and must stay in the sum.
    zeroed = deck_screen_unit_score(_breakdown(memory_bonus=0))
    assert zeroed.missing == ()
    assert zeroed.overall_power == pytest.approx(1510.0)


def test_the_ranking_objective_is_untouched():
    """The deck-screen number is a new function, not a change to `total_score`. This pins
    that the ranking number is not recomputed, reinterpreted, or replaced —
    `deck_screen_unit_score` only reads."""
    breakdown = _breakdown(total_score=1234.5)
    before = dataclasses.asdict(breakdown)
    deck_screen_unit_score(breakdown)
    assert dataclasses.asdict(breakdown) == before
    assert breakdown.total_score == 1234.5


# ==========================================================================
# 3. The honesty the number depends on
# ==========================================================================


def test_unit_score_k_is_the_pinned_constant():
    """The Unit Score constant the rest of the suite assumes; a change must be deliberate."""
    assert UNIT_SCORE_K == 2.03734
