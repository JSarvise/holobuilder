"""The ACTIVE line is `E[max]`, not a sum: the highest Score UP that is live
is the one that counts.

Two independent sources, and they agree:

- The game's `Vision.LiveCore!ActiveSkillEffectDataCollection::CalcScoreUpRatio`
  @ rva `0x5efdb90` folds each effect into one of two accumulators with
  `Math::Max` and returns their sum (`addss xmm7, xmm8`).
- This rule reproduces the deck screen's Active Skill line across captured
  decks to rms about 0.4 pp with no free constant. A summed model misses by up
  to about 14 pp and fails an order test that no free constant can survive.

The two buckets are **ACTIVE and SPECIAL**, discriminated by the plain byte
`LiveSkillEffectData.isSpecialSkill` (`cmp byte [rsp+0x38], 0` at `0x5efddf1`).

**⚠ The SPECIAL bucket does not carry a ratio term at all.** Every SPECIAL
Score UP is a `ScoreUpBoostSkillEffect`, whose `ScoreUpByRatio` is the base
stub returning `0.0f`, so `max_SPECIAL v(e)` is structurally zero and a SPECIAL
*multiplies* the ACTIVE maximum instead of adding to it — see
`tests/test_special_collapse.py`.

Every number here is derived by hand in its test's own docstring, and each
asserts against the summed answer too. Direction-only assertions would pass
under both rules for a single-ACTIVE team, where the two agree exactly.
"""

from __future__ import annotations

import dataclasses

import pytest

from hbuilder.domain.scoring import (
    SCORE_BUCKET_ACTIVE,
    SCORE_BUCKET_SPECIAL,
    score_card_set,
    score_fast,
    score_with_breakdown,
)
from hbuilder.domain.scoring_models import ACTIVE_STACKING_MODELS

from conftest import make_card, make_scoring_config, make_skill, make_team

# conftest's STATS baseline is 300 total per card, so five members are 1500.0.
BASE_STAT_SUM = 1500.0


def _cfg(stacking: str = "max"):
    return dataclasses.replace(
        make_scoring_config(), active_stacking_model=ACTIVE_STACKING_MODELS[stacking]
    )


def _scorer(card_id: str, magnitude: float, *, duration: float = 10.0, interval: float = 10.0):
    """An ACTIVE Score UP. `duration == interval` does not pin uptime at 1.0:
    the activation checks sit at `k * interval` for `k = 1 ..`, never at t = 0,
    so a run opens with one whole cooldown of dead time. At 10 s / 10 s the
    checks are 10, 20, ... 120 and their windows union to `[10, 128.8]`, an
    uptime of `118.8 / 128.8 = 297/322 = 0.922360248447205`.

    Times the Medium tier that is an occupancy of
    `0.46 * 297/322 = 297/700 = 0.4242857142857143` exactly, so the arithmetic
    below stays checkable by hand."""
    return make_card(
        card_id,
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            effect_type="score",
            duration=duration,
            interval=interval,
            probability_tier="Medium",  # 0.46
            magnitude=magnitude,
        ),
    )


def _special_scorer(card_id: str, magnitude: float, *, duration: float = 12.88):
    """A SPECIAL Score UP. At the default `expected_fires_per_run` of 1.0 and
    `REFERENCE_RUN_SECONDS = 128.8`, a 12.88 s duration is an occupancy of
    exactly 0.10, which keeps the arithmetic below checkable by hand."""
    return make_card(
        card_id,
        special=make_skill(
            "SPECIAL",
            family="conditional_score_up",
            effect_type="score",
            duration=duration,
            magnitude=magnitude,
        ),
    )


def _team(*scorers):
    filler = [make_card(f"m{index}") for index in range(len(scorers), 5)]
    return make_team([make_card("outfit"), *scorers, *filler])


def test_a_single_active_is_identical_under_both_rules():
    """The two rules can only disagree when two effects can be live at once.
    One ACTIVE at magnitude 40, at the finite-window occupancy
    `uptime 118.8/128.8 = 0.922360248447205` (checks at 10, 20, ... 120, the
    last window running past the end of the run) times the Medium tier 0.46,
    i.e. 297/700 = 0.4242857142857143:

        0.40 * 0.4242857142857143 = 0.1697142857142857

    so 1500 * 1.1697142857142857 = 1754.571428571429 either way — `==`, because
    `E[max]` over one term must reduce to that term exactly, not merely
    closely."""
    team = _team(_scorer("a", 40.0))

    assert score_fast(team, _cfg("max")) == score_fast(team, _cfg("sum"))
    assert score_fast(team, _cfg("max")) == pytest.approx(1754.571428571429)
    # 1776.0 is the answer on the steady-state duty cycle:
    # uptime `min(1, 10/10) = 1.0` and so `0.40 * 0.46 = 0.184`.
    assert score_fast(team, _cfg("max")) != pytest.approx(1776.0)


def test_two_actives_take_the_expected_maximum_not_the_sum():
    """Magnitudes 40 and 30, occupancy 0.4242857142857143 each — on the 10 s /
    10 s lattice the checks are at 10, 20, ... 120 and their windows union to
    `[10, 128.8]`, so uptime is `118.8/128.8 = 0.922360248447205`, times the
    Medium tier 0.46, which is 297/700 exactly.

        E[max] = 0.40 * 0.4242857142857143
               + 0.30 * 0.4242857142857143 * (1 - 0.4242857142857143)
               = 0.1697142857142857 + 0.07328020408163265
               = 0.24299448979591837
        1500 * 1.24299448979591837 = 1864.4917346938776

        summed = 0.1697142857142857 + 0.1272857142857143 = 0.297
        1500 * 1.297 = 1945.5

    81.01 points apart — 4.3 % of the `E[max]` total."""
    team = _team(_scorer("a", 40.0), _scorer("b", 30.0))

    assert score_fast(team, _cfg("max")) == pytest.approx(1864.4917346938776)
    assert score_fast(team, _cfg("sum")) == pytest.approx(1945.5)
    # 1887.78 and 1983.0 are the pair on the steady-state duty
    # cycle's occupancy of 0.46 (pool 0.25852, sum 0.322).
    assert score_fast(team, _cfg("max")) != pytest.approx(1887.78)
    assert score_fast(team, _cfg("sum")) != pytest.approx(1983.0)


def test_the_pool_is_independent_of_the_order_the_actives_appear_in():
    """`E[max]` is a property of the set, so listing the weaker scorer first
    must not change the answer. This is what the descending-magnitude sort
    buys, and it is worth pinning because the walk itself is order-sensitive
    by construction — reading the terms in list order would give, at the
    finite-window occupancy 0.4242857142857143,
    `0.30*0.4242857142857143 + 0.40*0.4242857142857143*0.5757142857142857
    = 0.1272857142857143 + 0.0977069387755102 = 0.22499265306122448`
    here instead of 0.24299448979591837."""
    forward = _team(_scorer("a", 40.0), _scorer("b", 30.0))
    reversed_ = _team(_scorer("b", 30.0), _scorer("a", 40.0))

    assert score_fast(forward, _cfg()) == pytest.approx(1864.4917346938776)
    assert score_fast(reversed_, _cfg()) == pytest.approx(1864.4917346938776)
    # 1887.78 is the answer on the steady-state duty cycle.
    assert score_fast(forward, _cfg()) != pytest.approx(1887.78)
    assert score_fast(reversed_, _cfg()) != pytest.approx(1887.78)
    # And the list-order walk, 1500 * 1.22499265306122448, is not it either.
    assert score_fast(reversed_, _cfg()) != pytest.approx(1837.4889795918368)


def test_a_second_copy_of_the_same_skill_is_worth_less_than_the_first():
    """The consequence that actually changes team building.

    At the finite-window occupancy 0.4242857142857143 (uptime 118.8/128.8 =
    0.922360248447205 from the checks at 10, 20, ... 120, times the Medium tier
    0.46):

        one copy  : 0.40 * 0.4242857142857143            = 0.1697142857142857
        two copies: 0.1697142857142857
                    + 0.40 * 0.4242857142857143 * (1 - 0.4242857142857143)
                                                        = 0.26742122448979594

    The second copy adds 0.0977069387755102, which is 57.57 % of what the first
    added — not 100 % as a sum would have it. That surviving share is exactly
    `1 - occupancy = 403/700`, so the dead zone leaves the second copy MORE room
    than the steady-state reading's 54 %, not less. Diminishing, never zero:
    the second copy still pays whenever the first happens not to be live."""
    one = score_fast(_team(_scorer("a", 40.0)), _cfg()) - BASE_STAT_SUM
    two = score_fast(_team(_scorer("a", 40.0), _scorer("b", 40.0)), _cfg()) - BASE_STAT_SUM

    assert one == pytest.approx(1500.0 * 0.1697142857142857)
    assert two == pytest.approx(1500.0 * 0.26742122448979594)
    assert (two - one) == pytest.approx(one * 0.5757142857142857)
    # 0.1840 / 0.28336 / 0.54 are the trio on the steady-state
    # duty cycle's occupancy of 0.46.
    assert one != pytest.approx(1500.0 * 0.1840)
    assert two != pytest.approx(1500.0 * 0.28336)
    assert (two - one) != pytest.approx(one * 0.54)


def test_a_certain_active_masks_every_weaker_one_except_over_the_dead_zone():
    """**No ACTIVE is ever live for a whole run, so `free` is never 0 and the
    masking is never total.** A 1.0
    probability tier makes the roll certain, but certainty is not occupancy —
    the first check sits one whole cooldown in, so a 10 s / 10 s skill is dark
    over `[0, 10]` and up over `[10, 128.8]`:

        occupancy = 1.0 * 118.8/128.8 = 297/322 = 0.922360248447205
        free      = 25/322            = 0.07763975155279502

    The weaker skill is masked over 92.24 % of the run and paid over the
    remaining 7.76 % — the dead zone is the only thing it can still win, and it
    does. A sum would credit it in full over the whole run.

        E[max] = 0.40 * 0.922360248447205
               + 0.30 * 0.922360248447205 * 0.07763975155279502
               = 0.368944099378882 + 0.021483546159484588
               = 0.39042764553836656   ; 1500 * 1.39042764553836656 = 2085.64146830755
        summed = 0.40 + 0.30 = 0.70    ; 1500 * 1.70 = 2550.0
    """
    cfg = dataclasses.replace(
        _cfg(), probability_tiers={"Low": 0.37, "Medium": 1.0, "High": 0.55}
    )
    team = _team(_scorer("a", 40.0), _scorer("b", 30.0))

    assert score_fast(team, cfg) == pytest.approx(2085.64146830755)
    # 2100.0 is the answer on the steady-state duty cycle, which reads
    # uptime 1.0, makes occupancy exactly 1.0 and the masking total.
    assert score_fast(team, cfg) != pytest.approx(2100.0)
    assert score_fast(team, cfg) != pytest.approx(2550.0)


def test_a_gate_that_did_not_fire_neither_contributes_nor_masks():
    """A skill that is not `applied` must yield occupancy 0.0 as well as
    magnitude 0.0. Magnitude alone would not be enough: a zero-magnitude term
    with a live occupancy would still consume the walk's remaining
    probability, letting an unfired gate suppress a skill that did fire.

    Here the ungated 30 scores alone, exactly as if the 40 were absent, at the
    finite-window occupancy 0.4242857142857143 (uptime 118.8/128.8 =
    0.922360248447205, checks at 10, 20, ... 120, times the Medium tier 0.46):
    `0.30 * 0.4242857142857143 = 0.1272857142857143` ;
    1500 * 1.1272857142857143 = 1690.9285714285713."""
    from hbuilder.domain.card import GateCondition

    gated = make_card(
        "gated",
        type_="Cute",
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            effect_type="score",
            duration=10.0,
            interval=10.0,
            probability_tier="Medium",
            magnitude=40.0,
            # Six Happy members are impossible in a five-member team.
            condition=GateCondition(kind="type", target="Happy", count=6),
        ),
    )
    team = _team(gated, _scorer("b", 30.0))

    assert score_fast(team, _cfg()) == pytest.approx(1690.9285714285713)
    # 1707.0 is the answer on the steady-state duty cycle.
    assert score_fast(team, _cfg()) != pytest.approx(1707.0)


def test_only_active_score_up_skills_enter_the_pool():
    """A PASSIVE Score UP is not an active-skill effect: it is always on, it
    is not what `CalcScoreUpRatio` maximises, and it must keep adding. The
    PASSIVE term is untouched by the finite window — it has no interval and no
    activation lattice — so only the ACTIVE half moves. A PASSIVE at 25 % and an
    ACTIVE at 40, the latter at occupancy 0.4242857142857143 (uptime
    118.8/128.8 = 0.922360248447205 times the Medium tier 0.46):

        0.25  +  0.40 * 0.4242857142857143  =  0.25 + 0.1697142857142857
              =  0.4197142857142857
        1500 * 1.4197142857142857 = 2129.5714285714284

    If the PASSIVE were swept into the maximum the answer would be
    `0.25 + 0.1697142857142857 * 0.75`-shaped and strictly smaller."""
    passive_scorer = make_card(
        "passive_scorer",
        passive=make_skill(
            "PASSIVE", family="conditional_score_up", effect_type="score", magnitude=25.0
        ),
    )
    team = _team(_scorer("a", 40.0), passive_scorer)

    assert score_fast(team, _cfg()) == pytest.approx(2129.5714285714284)
    # 2151.0 is the answer on the steady-state duty cycle.
    assert score_fast(team, _cfg()) != pytest.approx(2151.0)


def test_a_special_multiplies_the_active_maximum_rather_than_adding_to_it():
    """**The SPECIAL collapse.** An ACTIVE Score UP at magnitude 40 and a
    SPECIAL Score UP at magnitude 50:

        ACTIVE  occupancy = 118.8/128.8 * 0.46           = 0.4242857142857143
        SPECIAL occupancy = 1.0 fires * 12.88 / 128.8    = 0.10

    The finite window only affects the ACTIVE half: `_special_occupancy` reads
    a fire count and a duration, never an interval, so there is no lattice to
    walk.

    The SPECIAL is a `ScoreUpBoostSkillEffect`, so it joins `B`, not the ratio
    (`scoring._score_up_implements_boost`):

        B     = max_ACTIVE boost + max_SPECIAL boost = 0.0 + 0.50 * 0.10 = 0.05
        ratio = (1 + 0.05) * max_ACTIVE ScoreUpByRatio
              = 1.05 * 0.40 * 0.4242857142857143
              = 0.42 * 297/700 = 124.74/700 = 0.1782 exactly
        1500 * 1.1782 = 1767.3

    The additive-bucket model, re-derived on this same finite-window occupancy,
    adds the two maxima instead: `0.1697142857142857 + 0.05 =
    0.21971428571428572` for 1829.5714285714287 — **62.27 points, 3.5 %, too
    high**, and it over-credits the SPECIAL by a factor of
    `1 / max_ACTIVE = 1 / 0.1697142857142857 = 5.89`. On real decks that
    over-credit runs 1.5x to 3.7x. (1851.0 is that same additive answer on the
    steady-state uptime.)"""
    team = _team(_scorer("a", 40.0), _special_scorer("s", 50.0))
    total = score_fast(team, _cfg())

    assert total == pytest.approx(1767.3)
    # 1789.8 is the answer on the steady-state duty cycle.
    assert total != pytest.approx(1789.8)
    assert total != pytest.approx(1851.0)  # additive buckets, steady-state uptime
    assert total != pytest.approx(1829.5714285714287)  # additive buckets, this uptime


def test_two_specials_share_one_bucket_inside_b_but_do_not_discount_each_other():
    """The partition is by kind, not one bucket per skill — and it still holds
    after the collapse, one level down, inside `B`. What this pins is the
    dependence structure inside that shared bucket.

    An ACTIVE Score UP at 40 (occupancy 0.4242857142857143 — uptime
    118.8/128.8 = 0.922360248447205 times the Medium tier 0.46) under two
    SPECIAL Score UPs at 50
    and 40, occupancy 0.10 each. Both SPECIALs sit in ONE bucket — a second
    bucket would let them each take their own maximum — but within it they are
    `ActiveTerm.exclusive`, because the chart fires them at two different
    `#mmm0B` cues and the shortest consecutive cue gap over every shipped chart
    (9.88 s) exceeds the shortest SPECIAL duration in the game (7.0 s):

        B     = 0.50 * 0.10  +  0.40 * 0.10  =  0.09
        ratio = 1.09 * 0.40 * 0.4242857142857143
              = 1.09 * 0.1697142857142857 = 0.18498857142857142
        1500 * 1.18498857142857142 = 1777.4828571428573

    The exclusivity being pinned lives entirely inside `B`, and `B` is unchanged
    at 0.09: the finite window moved the ACTIVE factor it multiplies, nothing
    else. The independence walk inside `B` would give `0.05 + 0.04*0.9 = 0.086`
    and so `1.086 * 0.1697142857142857 = 0.1843097142857143` for
    1776.4645714285714 — that is the reading this test excludes; 1799.736 is
    the same reading on the steady-state occupancy. Neither is the additive-bucket
    `1500 * (1 + 0.184 + 0.09) = 1911.0`, which the SPECIAL collapse
    excludes."""
    team = _team(
        _scorer("a", 40.0), _special_scorer("s1", 50.0), _special_scorer("s2", 40.0)
    )

    assert score_fast(team, _cfg()) == pytest.approx(1777.4828571428573)
    # 1800.84 is the answer on the steady-state duty cycle.
    assert score_fast(team, _cfg()) != pytest.approx(1800.84)
    assert score_fast(team, _cfg()) != pytest.approx(1776.4645714285714)  # independence walk
    assert score_fast(team, _cfg()) != pytest.approx(1799.736)  # ditto, steady-state uptime
    assert score_fast(team, _cfg()) != pytest.approx(1911.0)  # additive buckets


def test_the_bucket_keys_are_the_two_the_game_partitions_by():
    assert SCORE_BUCKET_ACTIVE == "ACTIVE"
    assert SCORE_BUCKET_SPECIAL == "SPECIAL"


def test_two_buckets_take_two_maxima_and_add_them_at_the_model_level():
    """The game's two-bucket structure, exercised directly against the model object
    because no caller can currently produce a second bucket. Four terms, two
    per bucket, magnitudes 40/30 in each:

        per bucket : 0.40*0.46 + 0.30*0.46*(1-0.46) = 0.25852
        two buckets: 0.51704

    which is exactly twice the one-bucket answer for the same magnitudes.
    Filing all four under ONE bucket instead gives a single walk over
    0.40 / 0.40 / 0.30 / 0.30 at occupancy 0.46:

        0.184 + 0.09936 + 0.30*0.46*0.2916 + 0.30*0.46*0.157464
              = 0.34533083

    — strictly smaller, because one bucket can only ever have one maximum.
    That gap is exactly what the partition is worth.

    Every term here is `exclusive=False`, so this is the plain independence
    walk: the `exclusive` flag adds a dimension without moving any answer that
    does not ask for it."""
    model = ACTIVE_STACKING_MODELS["max"]
    terms = [
        ("x", 0.40, 0.46, False),
        ("y", 0.40, 0.46, False),
        ("x", 0.30, 0.46, False),
        ("y", 0.30, 0.46, False),
    ]

    assert model.combine(terms) == pytest.approx(0.51704)
    one_bucket = model.combine(
        [(SCORE_BUCKET_ACTIVE, m, o, x) for _b, m, o, x in terms]
    )
    assert one_bucket == pytest.approx(0.34533083)
    assert one_bucket < model.combine(terms)


def test_the_independence_walk_is_the_exclusive_free_special_case_bit_for_bit():
    """`ActiveTerm.exclusive` is a strict GENERALISATION: with every term
    independent the `(1 - used)` factor is a multiply by exactly `1.0` and the
    walk is the classic
    `SUM_i mag_i occ_i PROD_{j before i} (1 - occ_j)`. `==` and never `approx`,
    because a change in the last bit here would be a change of rule and would
    break driver parity and every pinned number."""
    model = ACTIVE_STACKING_MODELS["max"]
    pairs = [(0.40, 0.46), (0.30, 0.46), (0.22, 0.31), (0.55, 0.19)]

    classic = 0.0
    free = 1.0
    for magnitude, occupancy in sorted(pairs, key=lambda pair: -pair[0]):
        classic += magnitude * occupancy * free
        free *= 1.0 - occupancy

    assert model.combine([("A", m, o, False) for m, o in pairs]) == classic


def test_an_all_exclusive_bucket_is_the_plain_sum_and_the_summed_model_agrees():
    """The other limit. Over windows that never overlap, the time-average of a
    maximum IS the sum of the pieces — nothing ever competes — so the `max`
    model and the `sum` model return the same float:

        0.50*0.10 + 0.40*0.10 + 0.30*0.10 = 0.12

    against the independence walk's
    `0.05 + 0.04*0.9 + 0.03*0.81 = 0.1103`, a **-8.08 %** discount for
    collisions the chart forbids."""
    max_model = ACTIVE_STACKING_MODELS["max"]
    sum_model = ACTIVE_STACKING_MODELS["sum"]
    terms = [("S", 0.50, 0.10, True), ("S", 0.40, 0.10, True), ("S", 0.30, 0.10, True)]

    assert max_model.combine(terms) == pytest.approx(0.12)
    assert max_model.combine(terms) == pytest.approx(sum_model.combine(terms))
    independent = [(b, m, o, False) for b, m, o, _x in terms]
    assert max_model.combine(independent) == pytest.approx(0.1103)
    assert max_model.combine(terms) != pytest.approx(0.1103)


def test_a_bucket_mixing_exclusive_and_independent_terms_is_exact():
    """The general case, which no caller produces and which the formula
    has to get right anyway. One bucket, three terms:

        E1  magnitude 0.50, occupancy 0.10, exclusive
        I   magnitude 0.45, occupancy 0.30, independent
        E2  magnitude 0.40, occupancy 0.10, exclusive

    `E[max] = SUM_i mag_i * P(i is the maximum)`, and each probability is
    written out from the events rather than from the implementation:

        P(E1 max) = P(E1 live)                        = 0.10
        P(I  max) = P(I live) * P(E1 not live)        = 0.30 * 0.90 = 0.27
        P(E2 max) = P(E2 live) * P(I not live)        = 0.10 * 0.70 = 0.07
                    (E1 needs no factor: it cannot be live while E2 is)

        E[max] = 0.50*0.10 + 0.45*0.27 + 0.40*0.07 = 0.05 + 0.1215 + 0.028
               = 0.1995

    The independent cross-check: those probabilities plus `P(nothing live)` must
    be 1, and `P(nothing live) = (1 - 0.10 - 0.10) * (1 - 0.30) = 0.56`, which
    with `0.10 + 0.27 + 0.07 = 0.44` it is."""
    model = ACTIVE_STACKING_MODELS["max"]
    terms = [("S", 0.50, 0.10, True), ("S", 0.45, 0.30, False), ("S", 0.40, 0.10, True)]

    assert model.combine(terms) == pytest.approx(0.1995)
    assert 0.10 + 0.27 + 0.07 + (1.0 - 0.10 - 0.10) * (1.0 - 0.30) == pytest.approx(1.0)
    # Treating all three as independent gives 0.50*0.10 + 0.45*0.30*0.90 + 0.40*0.10*0.90*0.70 = 0.1967.
    all_independent = [(b, m, o, False) for b, m, o, _x in terms]
    assert model.combine(all_independent) == pytest.approx(0.1967)
    assert model.combine(terms) != pytest.approx(0.1967)


def test_the_breakdown_names_the_rule_and_its_active_rows_no_longer_sum_to_the_pool():
    """Each ACTIVE row reports what that skill would
    contribute alone, at the finite-window occupancy 0.4242857142857143
    (uptime 118.8/128.8 = 0.922360248447205 — checks at 10, 20, ... 120, the
    last window running past the end of the run — times the Medium tier 0.46):
    0.1697142857142857 and 0.1272857142857143. So the rows sum to
    `0.7 * 297/700 = 0.297` while the pool that was actually used is
    `0.1697142857142857 + 0.30 * 0.4242857142857143 * 0.5757142857142857
    = 0.24299448979591837`. The discrepancy is the mechanic, and
    `active_stacking_model_name` is what tells a reader which rule produced the
    total."""
    breakdown = score_with_breakdown(_team(_scorer("a", 40.0), _scorer("b", 30.0)), _cfg())
    active_rows = [c for c in breakdown.contributions if c.slot_kind == "ACTIVE"]

    assert breakdown.active_stacking_model_name == "max"
    assert sum(c.weighted_rate for c in active_rows) == pytest.approx(0.297)
    assert breakdown.combined_score_rate == pytest.approx(0.24299448979591837)
    # 0.322 and 0.25852 are the pair on the steady-state duty
    # cycle's occupancy of 0.46.
    assert sum(c.weighted_rate for c in active_rows) != pytest.approx(0.322)
    assert breakdown.combined_score_rate != pytest.approx(0.25852)


def test_batch_and_single_paths_stay_bit_identical_under_the_maximum():
    """`score_card_set` caches magnitude and occupancy
    separately and rebuilds the pool per leader; that must cost nothing in the
    last bit, so this is `==` and never `approx`."""
    outfit = make_card("outfit")
    members = (
        _scorer("a", 40.0),
        _scorer("b", 30.0),
        _scorer("c", 30.0, duration=6.0, interval=24.0),
        make_card("m4"),
        make_card("m5"),
    )
    cfg = _cfg()

    batch = score_card_set(members, (outfit,), cfg)
    single = score_fast(make_team([outfit, *members]), cfg)

    assert len(batch) == 1
    assert batch[0].total_score == single


def test_equal_magnitudes_are_credited_once_between_them_not_twice():
    """The tie case, which a strict `PROD_{j: mag_j > mag_i}` phrasing
    gets wrong. Two ACTIVEs at the same magnitude 40, each at
    the finite-window occupancy 0.4242857142857143 (uptime 118.8/128.8 =
    0.922360248447205 times the Medium tier 0.46), so each alone is
    `0.40 * 0.4242857142857143 = 0.1697142857142857`:

        implemented (stable walk) : 0.1697142857142857
                                    + 0.1697142857142857 * (1 - 0.4242857142857143)
                                                              = 0.26742122448979594
        strict-inequality reading : 0.1697142857142857 * 2    = 0.3394285714285714

    The second double-counts: at any instant when both are live there is one
    maximum, not two."""
    team = _team(_scorer("a", 40.0), _scorer("b", 40.0))

    assert score_fast(team, _cfg()) == pytest.approx(1500.0 * 1.26742122448979594)
    # 1.28336 is the answer on the steady-state duty cycle.
    assert score_fast(team, _cfg()) != pytest.approx(1500.0 * 1.28336)
    assert score_fast(team, _cfg()) != pytest.approx(1500.0 * 1.3394285714285714)
    assert score_fast(team, _cfg()) != pytest.approx(1500.0 * 1.368)  # ditto, steady-state
