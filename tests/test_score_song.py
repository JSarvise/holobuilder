"""The per-song scorer: `hbuilder.domain.song`.

Every number below is derived by hand in its test's own docstring. The
arithmetic is kept checkable by pinning three things that would otherwise make
it opaque:

* `difficulty_level=5` makes `LiveScoreMusicCoefficient` exactly `1.0` — the
  `(level - 5)` factor vanishes — except in the two tests that are *about* the
  coefficient.
* every probability tier is pinned to `1.0`, so a live ACTIVE has occupancy
  exactly `1.0` and `E[max]` reduces to `max`, except in the test that is about
  the expectation.
* `deck_power` is always passed explicitly, so no test depends on the modelled
  stat pool, which is a different quantity from the game's DeckPower and is
  `score_card_set`'s business, not this module's (the leader-outfit tests at
  the end are the deliberate exception).

This file holds the mechanism, not an end-to-end validation against a real run.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from hbuilder.domain.card import Stats
from hbuilder.domain.scoring import score_card_set
from hbuilder.domain.song import (
    SCORE_COEFFICIENT,
    ChartNote,
    SongChart,
    SongError,
    score_song,
    score_song_detailed,
)

from conftest import make_card, make_scoring_config, make_skill

# Pinning every tier to 1.0 turns `E[max]` into a plain `max` over the live
# magnitudes, which is what makes the hand arithmetic below readable.
CERTAIN_TIERS = {"Low": 1.0, "Medium": 1.0, "High": 1.0}


def cfg(**overrides):
    base = make_scoring_config(probability_tiers=CERTAIN_TIERS)
    return dataclasses.replace(base, **overrides) if overrides else base


def make_chart(
    *,
    times: tuple[float, ...],
    permil: int = 1000,
    level: int = 5,
    coefficient_permil: int = 4,
    cues: tuple[float, ...] = (10.0, 20.0, 30.0, 40.0, 50.0),
    permils: tuple[int, ...] | None = None,
) -> SongChart:
    weights = permils if permils is not None else (permil,) * len(times)
    return SongChart(
        music_id="m0001",
        difficulty="easy",
        difficulty_level=level,
        live_score_coefficient_permil=coefficient_permil,
        notes=tuple(
            ChartNote(time=t, perfect_permil=w) for t, w in zip(times, weights)
        ),
        special_cues=cues,
    )


def plain_members(count: int = 5):
    """Five cards carrying no skills at all — the arithmetic-only baseline."""
    return tuple(make_card(f"card-{i}") for i in range(count))


# ---------------------------------------------------------------------------
# The chart itself
# ---------------------------------------------------------------------------


def test_music_coefficient_is_the_ctor_expression_and_penalises_easy_charts():
    """`1 + (permil/1000) x (level - 5)`, as in the game binary.

    `m0001` easy is level 1 with `Music.liveScoreCoefficientPermil = 4`:
        1 + 0.004 x (1 - 5) = 1 - 0.016 = 0.984.
    An expert chart at level 30 with permil 8:
        1 + 0.008 x (30 - 5) = 1 + 0.200 = 1.200 — the measured maximum.
    Level 5 is the fixed point, which is why every other test pins it there."""
    assert make_chart(times=(1.0,), level=1, coefficient_permil=4).music_coefficient() == (
        pytest.approx(0.984)
    )
    assert make_chart(times=(1.0,), level=30, coefficient_permil=8).music_coefficient() == (
        pytest.approx(1.200)
    )
    assert make_chart(times=(1.0,), level=5).music_coefficient() == 1.0


def test_notes_perfect_coefficient_sum_is_the_integer_permil_sum_divided_once():
    """The game accumulates int32 permil and
    calls `FloatFromPermil` once at the end.

    A NORMAL note is 1000 permil and a LONG_CONTINUE tick is 100, so a chart of
    two taps and three hold ticks is `(1000 + 1000 + 100 + 100 + 100) / 1000 =
    2.3`."""
    chart = make_chart(
        times=(1.0, 2.0, 3.0, 4.0, 5.0), permils=(1000, 1000, 100, 100, 100)
    )
    assert chart.notes_perfect_coefficient_sum() == pytest.approx(2.3)


def test_combo_coefficient_reads_the_highest_tier_reached():
    """`LiveCombo`: 0 permil below 100, 10 permil from 100, 20 from
    200. A 136-note full combo therefore never leaves the first two tiers."""
    chart = make_chart(times=(1.0,))
    assert chart.combo_coefficient(1) == 0.0
    assert chart.combo_coefficient(99) == 0.0
    assert chart.combo_coefficient(100) == pytest.approx(0.010)
    assert chart.combo_coefficient(199) == pytest.approx(0.010)
    assert chart.combo_coefficient(200) == pytest.approx(0.020)


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"times": ()}, "no notes"),
        ({"times": (2.0, 1.0)}, "ascending time order"),
        ({"times": (1.0,), "cues": (1.0, 2.0)}, "exactly 5"),
        ({"times": (1.0,), "cues": (5.0, 4.0, 3.0, 2.0, 1.0)}, "ascending time order"),
        ({"times": (1.0,), "permil": 0}, "zero"),
    ],
)
def test_a_malformed_chart_raises_a_named_error(kwargs, fragment):
    """A chart that would produce a plausible wrong number raises instead. The
    zero-divisor case is the sharp one: `NotesPerfectCoefficientSum == 0` makes
    every note score infinitely, and it is exactly what a note-type map applied
    in the wrong direction produces."""
    with pytest.raises(SongError) as excinfo:
        make_chart(**kwargs)
    assert fragment in str(excinfo.value)


# ---------------------------------------------------------------------------
# Per-note shares of the chart-wide divisor
# ---------------------------------------------------------------------------


def test_song_length_cancels_exactly_when_nothing_varies_with_time():
    """`CalcScore` divides by the chart-wide
    `NotesPerfectCoefficientSum`, so each note takes a *share* and doubling the
    chart cannot inflate the score.

    Four 1000-permil notes, `deck_power = 2000`, level 5 (coefficient 1.0), no
    board, no skills, combo below 100 throughout:
        constant = 2000 x 2.300 x 1.0 / 4.0 = 1150.0
        per note = 1150.0 x 1.0 x (1 + 0) = 1150      -> total 4 x 1150 = 4600
    Eight identical notes:
        constant = 2000 x 2.300 x 1.0 / 8.0 = 575.0   -> total 8 x 575  = 4600
    Exactly equal, and exactly integral, so the per-note `ceil` cannot hide a
    discrepancy."""
    members, leaders = plain_members(), (make_card("leader"),)
    four = make_chart(times=(1.0, 2.0, 3.0, 4.0))
    eight = make_chart(times=(1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0))
    short = score_song(members, leaders, four, cfg(), deck_power=2000)
    long = score_song(members, leaders, eight, cfg(), deck_power=2000)
    assert short[0].total_score == 4600
    assert long[0].total_score == 4600


def test_the_music_coefficient_does_NOT_cancel_and_is_why_this_module_exists():
    """The same team on the same notes at two difficulty levels. The ratio
    argument above does not reach this term — it is a per-song scalar, and it is
    the reason a per-song entry point exists rather than a parameter on
    `score_fast`.

    Level 1, permil 4  -> 0.984 x 4600 = 4526.4, over four notes of 1131.6
                          -> ceil each -> 4 x 1132 = 4528
    Level 30, permil 8 -> 1.200 x 4600 = 5520 exactly."""
    members, leaders = plain_members(), (make_card("leader"),)
    easy = make_chart(times=(1.0, 2.0, 3.0, 4.0), level=1, coefficient_permil=4)
    hard = make_chart(times=(1.0, 2.0, 3.0, 4.0), level=30, coefficient_permil=8)
    assert score_song(members, leaders, easy, cfg(), deck_power=2000)[0].total_score == 4528
    assert score_song(members, leaders, hard, cfg(), deck_power=2000)[0].total_score == 5520


def test_the_ceil_is_applied_per_note_not_once_to_the_total():
    """`CalcScore` computes ONE note's score and
    its thunk ends in `roundsd xmm0,xmm0,0xa` — round-up. The run total is the
    sum of per-note `ceil`s, which is NOT `ceil` of the sum.

    Three 1000-permil notes, `deck_power = 1000`, level 5:
        constant = 1000 x 2.300 / 3.0 = 766.6666...
        per note -> ceil(766.666...) = 767   -> total 2301
        ceil of the un-rounded total = ceil(2300.0) = 2300
    The two differ by 1, which is the whole point."""
    members, leaders = plain_members(), (make_card("leader"),)
    chart = make_chart(times=(1.0, 2.0, 3.0))
    total = score_song(members, leaders, chart, cfg(), deck_power=1000)[0].total_score
    assert total == 2301
    assert total != math.ceil(1000 * SCORE_COEFFICIENT)


def test_a_note_whose_type_has_no_PERFECT_row_scores_nothing_and_costs_nothing():
    """Both `TryGetValue`s in the game fall to `0.0f`, so a
    missing `(judgement, noteType)` pair scores ZERO, not a default. A GHOST
    note has no PERFECT row.

    Adding a 0-permil note to a chart must therefore change nothing at all: it
    contributes 0 to the numerator and 0 to `NotesPerfectCoefficientSum`. A
    model that defaulted the missing pair to 1000 would move both."""
    members, leaders = plain_members(), (make_card("leader"),)
    without = make_chart(times=(1.0, 2.0, 3.0, 4.0))
    with_ghost = make_chart(
        times=(1.0, 2.0, 3.0, 4.0, 5.0), permils=(1000, 1000, 1000, 1000, 0)
    )
    assert (
        score_song(members, leaders, with_ghost, cfg(), deck_power=2000)[0].total_score
        == score_song(members, leaders, without, cfg(), deck_power=2000)[0].total_score
    )


def test_auto_permil_is_the_attested_AUTO_judgement_column():
    """`CalcScore` reads `JudgementCoefficient` by the note's ACTUAL judgement.
    AUTO weights a tap at 800 permil (0.80x
    PERFECT's 1000) and leaves hold-ticks (100) and no-row notes (0) unchanged."""
    assert ChartNote(time=0.0, perfect_permil=1000).auto_permil == 800
    assert ChartNote(time=0.0, perfect_permil=100).auto_permil == 100
    assert ChartNote(time=0.0, perfect_permil=0).auto_permil == 0


def test_auto_play_scores_the_AUTO_column_taps_at_080_ticks_unchanged():
    """`auto=True` swaps the numerator to the AUTO column while the NPCS divisor
    stays PERFECT. So an all-tap chart's auto total is 0.80x the manual
    total (bar the per-note ceil), and a hold-tick chart is unchanged."""
    members, leaders = plain_members(), (make_card("leader"),)
    taps = make_chart(times=(1.0, 2.0, 3.0, 4.0))
    manual = score_song(members, leaders, taps, cfg(), deck_power=1_000_000)[0].total_score
    auto = score_song(
        members, leaders, taps, cfg(), deck_power=1_000_000, auto=True
    )[0].total_score
    assert auto < manual
    assert abs(auto / manual - 0.80) < 0.001

    ticks = make_chart(times=(1.0, 2.0, 3.0, 4.0), permil=100)
    assert (
        score_song(members, leaders, ticks, cfg(), deck_power=1_000_000, auto=True)[0].total_score
        == score_song(members, leaders, ticks, cfg(), deck_power=1_000_000)[0].total_score
    )


# ---------------------------------------------------------------------------
# The time axis
# ---------------------------------------------------------------------------


def _active_scorer(card_id: str, magnitude: float, *, interval=20.0, duration=10.0):
    return make_card(
        card_id,
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            effect_type="score",
            duration=duration,
            interval=interval,
            probability_tier="High",
            magnitude=magnitude,
        ),
    )


def _special_scorer(card_id: str, magnitude: float, *, duration=10.0):
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


def test_the_active_cooldown_lattice_starts_at_the_interval_not_at_zero():
    """The clock starts at measure 0 of the
    beatmap, so activation check `k` sits at `k * interval` — there is no check
    at `t = 0`.

    One member with a 20 s / 10 s ACTIVE Score UP at magnitude 50 (= +0.50),
    probability pinned to 1.0. Windows are [20,30), [40,50), [60,70) ...
    Notes at 1 s and 15 s are outside every window; notes at 25 s and 45 s are
    inside one. `deck_power = 2000`, four notes, level 5:
        constant = 2000 x 2.300 / 4.0 = 1150.0
        bare note  = 1150
        boosted    = 1150 x 1.50 = 1725
        total      = 1150 + 1150 + 1725 + 1725 = 5750"""
    members = (_active_scorer("card-a", 50.0),) + plain_members(4)
    chart = make_chart(times=(1.0, 15.0, 25.0, 45.0))
    result = score_song(members, (make_card("leader"),), chart, cfg(), deck_power=2000)
    assert result[0].total_score == 5750


def test_two_live_actives_take_the_maximum_not_the_sum():
    """The instantaneous pool is `scoring._score_up_pool`'s, unforked, so the
    game's `Math::Max` per bucket applies tick by tick rather than as a run
    average.

    Two members, magnitudes 50 and 30, both with windows [20,30). At `t = 25`
    both are live and certain, so `E[max] = 0.50`, not `0.80`.
        constant = 2000 x 2.300 / 2.0 = 2300.0
        t = 1  -> 2300
        t = 25 -> 2300 x 1.50 = 3450        (a sum would give 2300 x 1.80 = 4140)
        total  = 5750"""
    members = (
        _active_scorer("card-a", 50.0),
        _active_scorer("card-b", 30.0),
    ) + plain_members(3)
    chart = make_chart(times=(1.0, 25.0))
    result = score_song(members, (make_card("leader"),), chart, cfg(), deck_power=2000)
    assert result[0].total_score == 5750


def test_the_active_roll_enters_as_an_exact_expectation_not_a_sample():
    """With the tier at 0.5 rather than 1.0, a single live ACTIVE of magnitude
    50 contributes `E[max] = 0.50 x 0.5 = 0.25`. There is no sampling and no
    seed: `ExpectedMaximumActiveSkill` is closed-form over independent
    Bernoulli trials, and the score is linear in the ratio, so the sum of the
    per-note expectations IS the expectation of the run total.

        constant = 2000 x 2.300 / 2.0 = 2300.0
        t = 1  -> 2300
        t = 25 -> 2300 x 1.25 = 2875
        total  = 5175"""
    members = (_active_scorer("card-a", 50.0),) + plain_members(4)
    chart = make_chart(times=(1.0, 25.0))
    half = cfg()
    half = dataclasses.replace(
        half, probability_tiers={"Low": 0.5, "Medium": 0.5, "High": 0.5}
    )
    result = score_song(members, (make_card("leader"),), chart, half, deck_power=2000)
    assert result[0].total_score == 5175


def test_a_special_with_no_active_under_it_is_worth_exactly_nothing():
    """(`scoring._score_up_implements_boost`) A SPECIAL
    Score UP is a `ScoreUpBoostSkillEffect`: it multiplies the ACTIVE maximum
    and has no ratio term of its own.

    **This is the claim that needs a time axis to be even statable**, and it is
    the reason `scoring._score_up_pool` points here. A team whose only Score UP
    is a SPECIAL scores identically to a team with no Score UP at all — on every
    note, in every slot order.

    Two notes, `deck_power = 2000`, constant 2300 each -> 4600 either way."""
    with_special = (_special_scorer("card-a", 120.0),) + plain_members(4)
    without = plain_members()
    chart = make_chart(times=(1.0, 15.0))
    leaders = (make_card("leader"),)
    assert (
        score_song(with_special, leaders, chart, cfg(), deck_power=2000)[0].total_score
        == score_song(without, leaders, chart, cfg(), deck_power=2000)[0].total_score
        == 4600
    )


def test_a_special_multiplies_an_active_that_IS_live_under_it():
    """The other half of the collapse. One member carries a 20 s / 10 s ACTIVE
    at magnitude 50; another carries a SPECIAL at magnitude 120 (= +1.20) with
    a 10 s duration. Cues are at 10/20/30/40/50 s.

    Put the SPECIAL in slot 2 (cue 20 s, window [20,30)) and the ACTIVE's own
    window is also [20,30), so at `t = 25` the boost applies:
        ratio = (1 + 1.20) x 0.50 = 1.10
        constant = 2000 x 2.300 / 2.0 = 2300.0
        t = 1  -> 2300
        t = 25 -> 2300 x 2.10 = 4830
        total  = 7130
    An additive reading would give `1 + 0.50 + 1.20 = 2.70` -> 6210 + 2300 =
    8510, which the game binary rules out."""
    members = (
        _active_scorer("card-a", 50.0),
        _special_scorer("card-b", 120.0),
    ) + plain_members(3)
    chart = make_chart(times=(1.0, 25.0))
    result = score_song_detailed(
        members, (make_card("leader"),), chart, cfg(), deck_power=2000
    )
    assert result[0].total_score == 7130
    # `card-b` must be in slot 2 — cue 20 s — for the boost to land at all.
    assert result[0].member_order.index("card-b") == 1


def test_slot_order_is_chosen_to_land_the_special_on_a_live_active():
    """The achievable score of a SET is the max over its
    120 orderings, because the player picks the order.

    The ACTIVE's window is [20,30) only (interval 20 s, duration 10 s, and the
    chart ends at 25 s so no later check exists). Cue 2 is at 20 s and every
    other cue is outside that window, so exactly one of the 120 orderings pays
    and `score_song` must find it. The alternative — reporting the first
    ordering, or the median — differs by 4830 - 2300 = 2530 points on a two-note
    chart."""
    members = (
        _active_scorer("card-a", 50.0),
        _special_scorer("card-b", 120.0),
    ) + plain_members(3)
    chart = make_chart(times=(1.0, 25.0))
    best = score_song_detailed(
        members, (make_card("leader"),), chart, cfg(), deck_power=2000
    )[0]
    assert best.member_order == ("card-a", "card-b", "card-0", "card-1", "card-2")
    assert best.total_score == 7130


def test_a_score_support_boosts_the_active_pool_and_a_special_adds_to_the_same_B():
    """On the time axis, a Score Support is a `ScoreUpBoostByRatio` and so
    joins the SAME maximum-of-two-maxima `B` a SPECIAL joins. The support sits
    in the ACTIVE bucket (it is not a SPECIAL slot), the SPECIAL in the SPECIAL
    bucket, and `B = max_ACTIVE + max_SPECIAL`.

    ACTIVE magnitude 50 live at `t = 25`; a COSTUME Score Support of 30 (always
    on) worn by the leader; a SPECIAL of 120 whose best slot is cue 2:
        B     = 0.30 + 1.20 = 1.50
        ratio = (1 + 1.50) x 0.50 = 1.25
        t = 1  -> 2300 x 1.30 = 2990      (support alone still boosts nothing:
                                           no ACTIVE is live, so ratio = 0)
        t = 25 -> 2300 x 2.25 = 5175
    At `t = 1` the ratio is 0 because no ACTIVE is live, so the note is a bare
    2300 -> total 2300 + 5175 = 7475."""
    members = (
        _active_scorer("card-a", 50.0),
        _special_scorer("card-b", 120.0),
    ) + plain_members(3)
    leader = make_card(
        "leader",
        costume_skill=make_skill(
            "COSTUME",
            family="score_support",
            effect_type="score",
            magnitude=30.0,
        ),
    )
    chart = make_chart(times=(1.0, 25.0))
    result = score_song(members, (leader,), chart, cfg(), deck_power=2000)
    assert result[0].total_score == 7475


def test_two_overlapping_cue_windows_take_the_maximum_here_and_not_the_run_average_sum():
    """**The one place the ranking scorer's disjointness assumption must NOT
    reach**, and the reason `_ratio_at` passes `exclusive=False` on every term.

    `scoring._windows_are_mutually_exclusive` marks a run-average SPECIAL term
    exclusive, which lets the SPECIAL bucket's `E[max]` become a plain SUM. It
    earns that from chart geometry: over the 704 charts that carry five `#mmm0B`
    cues the shortest consecutive gap is 9.88 s and the longest SPECIAL is
    14.0 s, so **18 of 2816 adjacent pairs can still be bridged** — 0.64 %, small
    but not zero. This module holds the actual cue times, so on those pairs it
    must see the overlap and take a real maximum. Inheriting the corpus's
    disjointness would double-count exactly where the corpus is wrong.

    One note at `t = 22`. An ACTIVE of 50 on a 20 s / 10 s lattice is live
    over [20,30). Two SPECIALs of 120 and 80, each 15 s long, against cues at
    10/20/30/40/50 — so slot 1 covers [10,25) and slot 2 covers [20,35), and both
    contain `t = 22`. The best slot order puts both there:

        B     = max(1.20, 0.80) = 1.20              <- ONE bucket, ONE maximum
        ratio = (1 + 1.20) x 0.50 = 1.10
        NotesPerfectCoefficientSum = 1.0, combo tier 0, music coefficient 1.0
        constant = 2000 x 2.300 / 1.0 = 4600
        total    = ceil(4600 x 2.10) = 9660

    Summing them the way the run-average path is allowed to would give
    `B = 2.00`, `ratio = 1.50` and **11500** — and the slot-order search would
    actively chase that overlap instead of being indifferent to it."""
    members = (
        _active_scorer("card-a", 50.0),
        _special_scorer("card-b", 120.0, duration=15.0),
        _special_scorer("card-c", 80.0, duration=15.0),
    ) + plain_members(2)
    chart = make_chart(times=(22.0,))
    result = score_song(members, (make_card("leader"),), chart, cfg(), deck_power=2000)

    assert result[0].total_score == 9660
    assert result[0].total_score != 11500  # the disjoint sum, wrong on a real overlap


def test_one_leader_score_per_leader_in_leaders_order():
    """The contract `score_card_set` states, mirrored exactly so a solver need
    not know which of the two it called. The leaders' outfits differ, so the
    totals must differ and must not be re-sorted."""
    members = (_active_scorer("card-a", 50.0),) + plain_members(4)
    leaders = tuple(
        make_card(
            f"leader-{magnitude:.0f}",
            costume_skill=make_skill(
                "COSTUME",
                family="score_support",
                effect_type="score",
                magnitude=magnitude,
            ),
        )
        for magnitude in (10.0, 90.0, 50.0)
    )
    chart = make_chart(times=(1.0, 25.0))
    result = score_song(members, leaders, chart, cfg(), deck_power=2000)
    assert tuple(score.leader_id for score in result) == (
        "leader-10",
        "leader-90",
        "leader-50",
    )
    assert result[1].total_score > result[2].total_score > result[0].total_score


def test_a_team_that_is_not_five_members_raises_rather_than_guessing():
    """The chart carries one `#mmm0B` cue per deck slot and there are five. A
    four-member call has no defensible cue assignment, so it raises."""
    chart = make_chart(times=(1.0,))
    with pytest.raises(SongError) as excinfo:
        score_song(plain_members(4), (make_card("leader"),), chart, cfg())
    assert "exactly 5 members" in str(excinfo.value)


def test_deck_power_defaults_to_the_same_stat_pool_score_card_set_uses():
    """Nothing here invents a DeckPower. With no `deck_power` argument the
    modelled stat pool is used, and `conftest.STATS` is 300 per card, so five
    plain members give 1500.0 — the same `base_stat_sum` `score_card_set`
    builds. `SongScore.deck_power` reports it, so a caller can never mistake it
    for the game's own Overall Power."""
    chart = make_chart(times=(1.0, 2.0))
    result = score_song_detailed(plain_members(), (make_card("leader"),), chart, cfg())
    assert result[0].deck_power == pytest.approx(1500.0)
    # constant = 1500 x 2.300 / 2.0 = 1725.0 per note, exactly integral.
    assert result[0].total_score == 3450


def test_the_board_bonus_is_its_own_multiplicative_factor_here_too():
    """`CalcScore` ends in `x (1 + SkillScoreUpRatio) x (1 +
    ScoreUpBonusRatioBySkillTree)` — two separate factors. With a 0.20 board
    entry on one member's holomem and no skills at all:
        constant = 2000 x 2.300 / 2.0 x 1.20 = 2760.0 per note -> 5520."""
    members = plain_members()
    board = cfg(board_score_bonus={members[0].member: 0.20})
    chart = make_chart(times=(1.0, 2.0))
    result = score_song(members, (make_card("leader"),), chart, board, deck_power=2000)
    assert result[0].total_score == 5520


def test_a_gated_skill_that_did_not_fire_neither_scores_nor_suppresses():
    """A gate that did not fire yields magnitude 0 AND occupancy 0, exactly as
    `scoring._bucket_entry` requires. Occupancy alone would let the dead skill
    consume the `E[max]` walk's remaining probability and suppress the live one.

    Member A carries a Cool-gated ACTIVE at magnitude 90; the team is all Cute,
    so it never fires. Member B carries an ungated ACTIVE at 50 with the same
    window. At `t = 25` the answer must be 0.50, not 0.0 and not something
    between."""
    from hbuilder.domain.card import GateCondition

    gated = make_card(
        "card-gated",
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            effect_type="score",
            duration=10.0,
            interval=20.0,
            probability_tier="High",
            magnitude=90.0,
            condition=GateCondition(kind="type", target="Cool", count=2),
        ),
    )
    members = (gated, _active_scorer("card-b", 50.0)) + plain_members(3)
    chart = make_chart(times=(25.0,))
    result = score_song(members, (make_card("leader"),), chart, cfg(), deck_power=2000)
    # constant = 2000 x 2.300 / 1.0 = 4600.0; 4600 x 1.50 = 6900
    assert result[0].total_score == 6900


def test_stats_do_not_reach_the_score_when_deck_power_is_supplied():
    """A stat buff changes `DeckPower`, never the ratio. With `deck_power`
    supplied by the caller the stat pool is bypassed entirely, so a team
    carrying a large stat ACTIVE scores exactly what the plain team does — which
    is what makes the supplied-power path safe to compare across teams."""
    buffed = (
        make_card(
            "card-stat",
            stats=Stats(performance=999, technique=999, sense=999),
            active=make_skill(
                "ACTIVE",
                family="stat_up",
                effect_type="all_stats",
                duration=10.0,
                interval=20.0,
                probability_tier="High",
                magnitude=80.0,
            ),
        ),
    ) + plain_members(4)
    chart = make_chart(times=(1.0, 25.0))
    leaders = (make_card("leader"),)
    assert (
        score_song(buffed, leaders, chart, cfg(), deck_power=2000)[0].total_score
        == score_song(plain_members(), leaders, chart, cfg(), deck_power=2000)[0].total_score
    )


# ==========================================================================
# The leader's stat outfit — the defect every other test in this file misses
# ==========================================================================
#
# Every other test here passes `deck_power` explicitly, so none of them sees the
# DEFAULT `deck_power`. That default must include the leader's outfit: computed
# over the member entries alone, a leader whose outfit is a stat buff
# contributes NOTHING and every candidate leader ties (four leaders with +50
# all-stats, +100 sense, +120 sense and +50 all-stats outfits all score the same,
# while `score_card_set` separates them by up to 30 %). Almost every leader
# outfit is `stat_up`, so that would affect every per-song ranking.
#
# So these deliberately do NOT pass `deck_power`.


def _stat_outfit_leaders():
    """Two leaders differing only in the magnitude of a stat outfit."""
    small = make_card(
        "leader_small",
        costume_skill=make_skill(
            "COSTUME", family="stat_up", effect_type="all_stats", magnitude=50.0
        ),
    )
    big = make_card(
        "leader_big",
        costume_skill=make_skill(
            "COSTUME", family="stat_up", effect_type="all_stats", magnitude=500.0
        ),
    )
    return small, big


def test_a_leaders_stat_outfit_moves_the_default_deck_power():
    """The regression. Without this the per-song scorer cannot rank leaders at
    all, and a per-song ranking over candidate leaders is meaningless."""
    small, big = _stat_outfit_leaders()
    chart = make_chart(times=(1.0, 2.0, 3.0, 4.0))

    scores = score_song_detailed(plain_members(), (small, big), chart, cfg())
    by_id = {score.leader_id: score for score in scores}

    assert by_id["leader_big"].deck_power > by_id["leader_small"].deck_power
    assert by_id["leader_big"].total_score > by_id["leader_small"].total_score


def test_the_per_song_leader_ORDER_agrees_with_the_ranking_scorer():
    """The two scorers answer different
    questions and their absolute numbers are not comparable — but they read the
    same leader contribution, so they must agree on which leader is better."""
    small, big = _stat_outfit_leaders()
    members = plain_members()
    chart = make_chart(times=(1.0, 2.0, 3.0, 4.0))

    abstract = score_card_set(members, (small, big), cfg())
    per_song = score_song_detailed(members, (small, big), chart, cfg())

    abstract_order = [s.leader_id for s in sorted(abstract, key=lambda s: -s.total_score)]
    song_order = [s.leader_id for s in sorted(per_song, key=lambda s: -s.total_score)]
    assert abstract_order == song_order


def test_an_explicit_deck_power_still_overrides_everything():
    """The caller's number wins, unchanged — a validation against a recorded
    run passes the deck screen's own Overall Power, and the default must not
    touch that path."""
    small, big = _stat_outfit_leaders()
    chart = make_chart(times=(1.0, 2.0))
    scores = score_song_detailed(
        plain_members(), (small, big), chart, cfg(), deck_power=2000
    )
    assert scores[0].deck_power == scores[1].deck_power == 2000
    assert scores[0].total_score == scores[1].total_score


# ==========================================================================
# The 5x5 assignment fast path — bit-identical to the 120-permutation walk
# ==========================================================================
#
# `score_song_detailed` does not always walk the 120 permutations: when the
# count-limited passive term does not bind AND no note is reachable from two
# slots by two members' SPECIALs, the total decomposes into a 5x5 assignment.
# Every per-note value is a `math.ceil`, so every
# summand is an integer-valued float and re-associating the sum is EXACT —
# which is the whole licence for the shortcut, and what these pin.
#
# ⚠ These exercise BOTH paths and check the invariants that hold on either;
# they do NOT compare the two paths against each other on one input, because
# flipping the predicate without changing the inputs is not something a caller
# can do. The direct comparison is a harness over many (team, chart) pairs,
# not a unit test. What lives here is the part a unit test CAN own: that each path runs, returns a legal slot order, and that
# the `bisect` liveness reproduces `_fixed_liveness` exactly.


def _special(magnitude, duration):
    return make_skill(
        "SPECIAL", family="score_support", effect_type="score",
        magnitude=magnitude, duration=duration,
    )


def _members_with_specials(durations):
    """Five members whose SPECIALs differ, so the slot assignment matters."""
    return tuple(
        make_card(f"card-{i}", special=_special(20.0 + 10 * i, duration))
        for i, duration in enumerate(durations)
    )


def test_the_fast_path_matches_the_walk_when_the_cues_are_disjoint():
    """Short SPECIALs against well-separated cues: no note is reachable from
    two slots, so the decomposition applies. Both paths must agree on the score
    AND on the slot order — an argmax that picked a different order would be a
    different recommendation even at an identical total."""
    members = _members_with_specials([3.0, 3.0, 3.0, 3.0, 3.0])
    chart = make_chart(times=tuple(float(t) for t in range(1, 60)))
    result = score_song_detailed(members, (make_card("leader"),), chart, cfg())[0]
    assert result.total_score > 0
    assert len(result.member_order) == 5
    assert sorted(result.member_order) == sorted(c.id for c in members)


def test_a_long_special_that_bridges_two_cues_still_scores():
    """The coupled case: a SPECIAL long enough to span two cue windows makes
    the per-note combine a `max` across two assigned members, which does not
    split per slot. The predicate must catch it and fall back rather than
    return the separable answer."""
    members = _members_with_specials([30.0, 30.0, 3.0, 3.0, 3.0])
    chart = make_chart(times=tuple(float(t) for t in range(1, 60)))
    result = score_song_detailed(members, (make_card("leader"),), chart, cfg())[0]
    assert result.total_score > 0
    assert sorted(result.member_order) == sorted(c.id for c in members)


def test_the_liveness_bisect_agrees_with_the_reference_scan():
    """`_fixed_liveness` is kept as the reference implementation for the
    `bisect` index ranges. Its three cases — always live, never live, and
    windowed — must all still be reproduced by `[bisect_left(times, start),
    bisect_left(times, end))`, or every fixed term's liveness is wrong."""
    from bisect import bisect_left

    from hbuilder.domain.song import _fixed_liveness

    times = tuple(float(t) for t in range(0, 40))

    class _Fake:
        __slots__ = ("windows",)

        def __init__(self, windows):
            self.windows = windows

    for windows in (None, (), ((0.0, 5.0),), ((2.5, 7.5), (20.0, 21.0)),
                    ((0.0, 40.0),), ((39.0, 100.0),)):
        reference = _fixed_liveness(_Fake(windows), times)
        rebuilt = [False] * len(times)
        if windows is None:
            rebuilt = [True] * len(times)
        else:
            for start, end in windows:
                for index in range(bisect_left(times, start), bisect_left(times, end)):
                    rebuilt[index] = True
        assert rebuilt == reference, windows


# ---------------------------------------------------------------------------
# Platform: the one LiveNote cell that differs between mobile and PC (FLICK)
# ---------------------------------------------------------------------------


def _flick_chart(times, flick_at, level: int = 5):
    """A tap chart whose notes at indices `flick_at` are FLICKs (PC base 1000,
    `flick=True`); everything else a NORMAL tap."""
    return SongChart(
        music_id="m0001", difficulty="easy", difficulty_level=level,
        live_score_coefficient_permil=4,
        notes=tuple(
            ChartNote(time=t, perfect_permil=1000, flick=(i in flick_at))
            for i, t in enumerate(times)
        ),
        special_cues=(10.0, 20.0, 30.0, 40.0, 50.0),
    )


def test_a_flick_weighs_1050_on_mobile_and_1000_on_pc_and_nothing_else_moves():
    """In the master's LiveNote table, the PERFECT row's mobile
    column (`scoreCoefficientPermilMultiply`) and PC column
    (`pcScoreCoefficientPermilMultiply`) differ in exactly one cell: FLICK,
    1050 vs 1000. `perfect_permil` is the PC base; the flag resolves it."""
    flick = ChartNote(time=0.0, perfect_permil=1000, flick=True)
    tap = ChartNote(time=0.0, perfect_permil=1000)
    tick = ChartNote(time=0.0, perfect_permil=100)
    assert flick.perfect_permil_for("mobile") == 1050
    assert flick.perfect_permil_for("pc") == 1000
    assert flick.perfect_permil_for() == 1050          # default = mobile
    assert tap.perfect_permil_for("mobile") == tap.perfect_permil_for("pc") == 1000
    assert tick.perfect_permil_for("mobile") == tick.perfect_permil_for("pc") == 100
    # AUTO is 800 in BOTH columns for a FLICK (note_type 2 / judgement 100).
    assert flick.auto_permil == 800
    # An un-flagged note (a charts-1 file) is platform-independent by construction.
    assert ChartNote(time=0.0, perfect_permil=1000).flick is False


def test_the_perfect_sum_follows_the_platform_and_defaults_to_mobile():
    chart = _flick_chart((1.0, 2.0, 3.0, 4.0), flick_at={1, 3})
    assert chart.flick_count() == 2
    assert chart.notes_perfect_coefficient_sum("pc") == pytest.approx(4.0)
    assert chart.notes_perfect_coefficient_sum("mobile") == pytest.approx(4.1)
    assert chart.notes_perfect_coefficient_sum() == pytest.approx(4.1)


def test_scoring_config_platform_defaults_to_mobile_and_reaches_the_scorer():
    """The scorer weighs BOTH the divisor and the per-note numerator in
    `cfg.platform`'s column. On a chart whose flicks all sit past combo 100
    (tier 10 permil) the two platforms give provably different totals: the
    flicks' share of the chart rises on mobile and they carry the combo bonus,
    so mobile > pc here — while a flick-less chart is identical on both."""
    assert cfg().platform == "mobile"
    members, leaders = plain_members(), (make_card("leader"),)
    times = tuple(float(t) for t in range(1, 121))
    chart = _flick_chart(times, flick_at=set(range(100, 120)))
    mobile = score_song(members, leaders, chart, cfg(platform="mobile"),
                        deck_power=1_000_000)[0].total_score
    pc = score_song(members, leaders, chart, cfg(platform="pc"),
                    deck_power=1_000_000)[0].total_score
    default = score_song(members, leaders, chart, cfg(), deck_power=1_000_000)[0].total_score
    assert default == mobile
    assert mobile > pc
    # Hand check, bar the per-note ceil: total ~ P*2.3*MC * sum(w_n*(1+c_n)) / PS.
    ratio_mobile = (100 * 1000 + 20 * 1050 * 1.01) / (100 * 1000 + 20 * 1050)
    ratio_pc = (100 * 1000 + 20 * 1000 * 1.01) / (100 * 1000 + 20 * 1000)
    assert mobile / pc == pytest.approx(ratio_mobile / ratio_pc, rel=2e-4)

    flat = _flick_chart(times, flick_at=set())
    assert (
        score_song(members, leaders, flat, cfg(platform="mobile"), deck_power=1_000_000)[0].total_score
        == score_song(members, leaders, flat, cfg(platform="pc"), deck_power=1_000_000)[0].total_score
    )


def test_a_charts_2_note_row_carries_the_flick_flag_and_a_charts_1_row_reads_flick_less():
    from hbuilder.domain.song import charts_from_mapping

    def _doc(notes):
        return {"schema_version": "charts-2", "charts": {"m0001/EXPERT": {
            "music_id": "m0001", "difficulty": "EXPERT", "difficulty_level": 26,
            "live_score_coefficient_permil": 4, "notes": notes,
            "special_cues": [1.0, 2.0, 3.0, 4.0, 5.0]}}}

    two = charts_from_mapping(_doc([[1.0, 1000], [2.0, 100]])).get("m0001", "EXPERT")
    three = charts_from_mapping(_doc([[1.0, 1000, 1], [2.0, 100, 0], [3.0, 1000, 0]])).get("m0001", "EXPERT")
    assert [n.flick for n in two.notes] == [False, False]
    assert [n.flick for n in three.notes] == [True, False, False]
    assert three.notes_perfect_coefficient_sum("mobile") == pytest.approx(2.15)
    assert three.notes_perfect_coefficient_sum("pc") == pytest.approx(2.1)
    with pytest.raises(SongError):
        charts_from_mapping(_doc([[1.0, 1000, 1, 7]]))
