"""**The SPECIAL collapse:** a SPECIAL Score UP multiplies the ACTIVE maximum
and is worth exactly nothing without one.

In one paragraph: `CalcScoreUpRatio` computes `ratio = max_ACTIVE v(e) +
max_SPECIAL v(e)` where `v(e) = (1 + SkillEffectUpPermilUp/1000 + B) x
e.ScoreUpByRatio(deck)`. In the client, `LiveActiveSkillEffectData::Create` maps
effect type 5 — the type every `LiveSpecialSkillLevel` row carries — to
`Vision.LiveCore.ScoreUpBoostSkillEffect`, whose vtable slot `+0x1c8`
(`ScoreUpByRatio`) is **not overridden**: it holds the shared base stub whose
four bytes `0f 57 c0 c3` are `xorps xmm0,xmm0 ; ret` — a hard `0.0f`. Only slot
`+0x1d8` (`ScoreUpBoostByRatio`) is overridden. So `max_SPECIAL v(e)` is
structurally zero and the expression collapses to

    ratio = (1 + B) x max_ACTIVE ScoreUpByRatio

with the team's SPECIAL Score UPs living inside `B`.

The captured decks pin the size independently: solving the reference duration
from them under both readings, the multiplier reading gives `T` constant to
**6.2 %**; the additive reading spreads **90.0 %**.

The game's collapse is instantaneous; this module multiplies a time-averaged `B`
by a time-averaged ACTIVE maximum, and the two differ by `Cov(B, A)`. That gap
has two halves, and only one of them is real:

- **Within `B` — real, implemented.** SPECIALs cannot overlap one another: the
  chart has five `#mmm0B` cues, one per deck slot, and across every chart that
  carries them the shortest consecutive cue gap is **9.88 s** against a
  **7.0 s** shortest SPECIAL duration — no adjacent pair can be bridged by the
  shortest SPECIAL and under 1 % by the longest. Those terms carry
  `ActiveTerm.exclusive = True` and the SPECIAL bucket's maximum is the plain
  sum, which fits the deck screen's own Special line far better than an
  independence discount (rms 1.48 pp against 3.92 pp).
- **Between `B` and `A` — about zero, deliberately not applied.** Over the
  captured decks and every chart (note-weighted) `E[SA]/(E[S]E[A])` is
  **1.0747** over the whole song but **0.9976 +/- 0.0087** from `t = 40 s`. All
  of the excess is the ACTIVE cooldown lattice's start-of-song dead zone, which
  `_active_occupancy` models directly — so applying the whole-song figure on
  top would count it twice. `scoring._score_up_pool` carries the full
  derivation.

The ACTIVE uptime is the finite lattice: `_active_occupancy` reads
`_finite_window_uptime(duration, interval)` — the union of the activation
windows at `k x interval` for `k = 1 .. floor(128.8 / interval)` — not the
steady-state duty cycle `min(1, duration / interval)`. For this file's
fixture (`duration == interval == 10 s`) the windows abut and tile the run from
the first check to its end, so uptime is `(128.8 - 10)/128.8 = 118.8/128.8`
rather than 1.0, and ACTIVE occupancy is `0.46 * 118.8/128.8 = 297/700` rather
than 0.46. See `ACTIVE_OCCUPANCY` below. `_special_occupancy` has no lattice,
so every `B` in this file is the plain SPECIAL occupancy.

`test_the_factorisation_carries_the_within_b_covariance_and_not_the_between_
bucket_one` pins the resulting identity to numbers, including a `!=` against the
factorised product with an independence discount inside `B`.

Every number below is derived by hand in its test's own docstring, and every
pinned assertion also asserts against the rival readings (the additive bucket,
the steady-state uptime, the independence discount) — a rule that only asserted
a direction would pass under all of them.
"""

from __future__ import annotations

import dataclasses

import pytest

from hbuilder.domain.gates import resolve_team_gates
from hbuilder.domain.scoring import (
    _bucket_entry,
    _score_up_implements_boost,
    _windows_are_mutually_exclusive,
    score_card_set,
    score_fast,
    score_with_breakdown,
)
from hbuilder.domain.scoring_models import ACTIVE_STACKING_MODELS

from conftest import make_card, make_scoring_config, make_skill, make_team

# conftest's STATS baseline is 300 total per card, so five members are 1500.0.
BASE_STAT_SUM = 1500.0

# The ACTIVE uptime is the finite activation lattice over the
# `REFERENCE_RUN_SECONDS = 128.8` s run, not the steady-state duty cycle
# `min(1, duration / interval)`.
# `_active`'s skills carry `duration == interval == 10 s`, so the checks sit at
# 10, 20, ... 120 — twelve of them, and none at `t = 0` — and their windows
# abut exactly, tiling the run from the first check to its end. The union is
# therefore the whole run bar the opening one-cooldown dead zone:
#
#     live   = 128.8 - 10 = 118.8 s
#     uptime = 118.8 / 128.8 = 0.9223602484472050
#
# The steady-state answer would be a flat 1.0 — `min(1, 10/10)` — which is this
# expression's `T -> infinity` limit with the dead zone assumed away.
ACTIVE_UPTIME = (128.8 - 10.0) / 128.8
# Times the Medium tier: 0.46 * 118.8/128.8 = 54.648/128.8 = 297/700 exactly
# = 0.4242857142857143. The steady-state occupancy would be the bare tier, 0.46.
ACTIVE_OCCUPANCY = ACTIVE_UPTIME * 0.46


def _cfg(stacking: str = "max"):
    return dataclasses.replace(
        make_scoring_config(), active_stacking_model=ACTIVE_STACKING_MODELS[stacking]
    )


def _active(card_id: str, magnitude: float):
    """An ACTIVE Score UP. `duration == interval` makes the lattice windows abut,
    so uptime is the run minus one cooldown, `118.8/128.8 = 0.922360...`, and
    occupancy is `ACTIVE_OCCUPANCY = 0.46 * 118.8/128.8 = 0.4242857142857143`.
    Under a steady-state uptime of 1.0 it would be the bare Medium tier 0.46."""
    return make_card(
        card_id,
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            effect_type="score",
            duration=10.0,
            interval=10.0,
            probability_tier="Medium",
            magnitude=magnitude,
        ),
    )


def _special(card_id: str, magnitude: float, *, duration: float = 12.88):
    """A SPECIAL Score UP. At `expected_fires_per_run = 1.0` and
    `REFERENCE_RUN_SECONDS = 128.8`, a 12.88 s duration is an occupancy of
    exactly 0.10."""
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


# --- The headline: a SPECIAL with nothing under it is worth nothing ---------


def test_a_special_score_up_with_no_active_under_it_contributes_exactly_nothing():
    """The claim that changes team building. A single SPECIAL Score UP at
    magnitude 50, occupancy 0.10, and no ACTIVE Score UP anywhere:

        B     = 0.50 * 0.10 = 0.05
        ratio = (1 + 0.05) * max_ACTIVE = 1.05 * 0.0 = 0.0
        1500 * 1.0 = 1500.0

    Exactly the bare stat sum — `==`, not `approx`, because a multiplier on an
    empty maximum must be identically zero rather than merely small.

    The additive-bucket reading puts the SPECIAL in its own bucket maximum and adds it:
    `1500 * 1.05 = 1575.0`."""
    team = _team(_special("s", 50.0))

    assert score_fast(team, _cfg()) == BASE_STAT_SUM
    assert score_fast(team, _cfg()) != pytest.approx(1575.0)  # additive bucket


def test_a_second_special_with_no_active_under_it_also_changes_nothing():
    """Not a coincidence of arity. Two SPECIAL Score UPs at 50 and 40, still no
    ACTIVE: `B = 0.50*0.10 + 0.40*0.10*(1-0.10) = 0.086`, multiplying zero.

    The additive-bucket reading sums the SPECIAL bucket's `E[max]` straight into
    the score: `1500 * 1.086 = 1629.0`. **This is the shape of over-credit that
    would make the solver recommend an all-SPECIAL team.**"""
    team = _team(_special("s1", 50.0), _special("s2", 40.0))

    assert score_fast(team, _cfg()) == BASE_STAT_SUM
    assert score_fast(team, _cfg()) != pytest.approx(1629.0)  # additive bucket


# --- The multiplier, and what it is worth ----------------------------------


def test_a_special_is_worth_a_fraction_of_the_active_maximum_not_its_own_magnitude():
    """ACTIVE 40 at occupancy `0.46 * 118.8/128.8 = 0.4242857142857143` — checks
    at 10, 20, ... 120, abutting, so the run is live from 10 s to its end and only
    the opening cooldown is dead — and SPECIAL 50 at occupancy 0.10.

        max_ACTIVE = 0.40 * 0.4242857142857143 = 118.8/700 = 0.1697142857142857
        B          = 0.50 * 0.10               = 0.05
        ratio      = 1.05 * 118.8/700 = 124.74/700 = 0.1782   (exactly)
        1500 * 1.1782 = 1767.3

    The SPECIAL's whole contribution is `1500 * 0.05 * 0.1697142857... = 8910/700
    = 12.7285714285...` points. The additive-bucket reading gives it
    `1500 * 0.05 = 75.0` — an over-credit of `75.0 / 12.72857142857 = 5.892256x`,
    which is exactly `1 / max_ACTIVE` and is what the ratio assertion below pins.
    On the ten captured decks that factor runs 1.47x to 3.74x
    (deck Active lines 68.6 % down to 26.4 %).

    Under a steady-state uptime of 1.0 the occupancy would be the bare 0.46,
    `max_ACTIVE` 0.184, and these three numbers 1776.0, 1789.8 and 13.8. Each
    is asserted against below."""
    active_only = score_fast(_team(_active("a", 40.0)), _cfg())
    both = score_fast(_team(_active("a", 40.0), _special("s", 50.0)), _cfg())

    max_active = 0.40 * ACTIVE_OCCUPANCY
    assert active_only == pytest.approx(1754.5714285714287)  # 1500 * (1 + 118.8/700)
    assert active_only != pytest.approx(1776.0)  # steady-state uptime
    assert both == pytest.approx(1767.3)  # 1500 * 1.1782
    assert both != pytest.approx(1789.8)  # steady-state uptime
    assert both != pytest.approx(1851.0)  # additive bucket, steady-state uptime
    # The additive bucket at THIS uptime: 1754.571428... + 1500 * 0.05.
    assert both != pytest.approx(1829.5714285714287)

    increment = both - active_only
    assert increment == pytest.approx(12.72857142857143)  # 75 * 118.8/700
    assert increment != pytest.approx(13.8)  # steady-state uptime
    # The property: the additive model's over-credit is 1/max_ACTIVE.
    assert (BASE_STAT_SUM * 0.05) / increment == pytest.approx(1.0 / max_active)
    assert (BASE_STAT_SUM * 0.05) / increment == pytest.approx(5.892255892255892)


def test_the_same_special_is_worth_twice_as_much_under_twice_the_active():
    """The cross-term an additive model structurally cannot express, and the
    reason this is a modelling change rather than a rescaling.

    The same SPECIAL 50 (`B = 0.05`) under an ACTIVE of 80 instead of 40, at the
    finite-window occupancy `0.46 * 118.8/128.8 = 0.4242857142857143`:

        max_ACTIVE = 0.80 * 0.4242857142857143 = 237.6/700 = 0.3394285714285714
        ratio      = 1.05 * 237.6/700 = 249.48/700 = 0.3564   (exactly)
        1500 * 1.3564 = 2034.6
        increment  = 1500 * 0.05 * 237.6/700 = 17820/700 = 25.4571428571...

    — exactly double the 12.7285714285... it was worth under the 40, which is
    the property this test exists for and which the uptime does not affect:
    both increments scale linearly in the ACTIVE magnitude and both carry the
    same occupancy factor. Under a sum the SPECIAL would have been worth 75.0
    in both teams, independent of what it was multiplying.

    A steady-state uptime would make these 2052.0, 2079.6 and 27.6."""
    strong_only = score_fast(_team(_active("a", 80.0)), _cfg())
    strong_both = score_fast(_team(_active("a", 80.0), _special("s", 50.0)), _cfg())

    assert strong_only == pytest.approx(2009.142857142857)  # 1500 * (1 + 237.6/700)
    assert strong_both == pytest.approx(2034.6)  # 1500 * (1 + 1.05 * 237.6/700)
    assert (strong_both - strong_only) == pytest.approx(25.45714285714286)
    assert strong_only != pytest.approx(2052.0)  # steady-state uptime
    assert strong_both != pytest.approx(2079.6)  # steady-state uptime
    assert (strong_both - strong_only) != pytest.approx(27.6)  # steady-state uptime
    # Twice the 12.7285714285... the identical SPECIAL bought under the
    # 40-magnitude ACTIVE — the doubling survives the uptime change intact.
    weak_only = score_fast(_team(_active("a", 40.0)), _cfg())
    weak_both = score_fast(_team(_active("a", 40.0), _special("s", 50.0)), _cfg())
    assert (strong_both - strong_only) == pytest.approx(2.0 * (weak_both - weak_only))


def test_the_factorisation_carries_the_within_b_covariance_and_not_the_between_bucket_one():
    """**The identity, restated after the overlap half landed.** This replaces
    `test_the_factorisation_is_exact_and_is_the_approximation`, which pinned the
    same product with `E[B]` computed under an independence assumption the chart
    refutes — and which, carrying only ONE SPECIAL, could not have seen the
    change even if it had been wrong, because a bucket of one has no overlap to
    model. Two SPECIALs, so the rule is discriminated rather than merely
    exercised.

    The score is `(1 + E[B]) x E[max_ACTIVE]`, and each factor carries its
    own dependence structure:

        E[B]           = 0.50 * 0.10 + 0.40 * 0.10  = 0.09      <- DISJOINT sum,
                         because the two SPECIALs fire at two different `#mmm0B`
                         cues and the shortest gap over every chart
                         (9.88 s) exceeds the longest SPECIAL that could bridge
                         it on all but a few adjacent pairs.
        E[max_ACTIVE]  = 0.40 * A + 0.30 * A * (1 - A) = 0.2429944897959184,
                         where A = 0.46 * 118.8/128.8 = 0.4242857142857143 is
                         the finite-window occupancy: checks at 10, 20, ... 120,
                         abutting, so the run is live from its first check to
                         its end and only the opening cooldown is dead.
                         <- still the INDEPENDENCE walk: two ACTIVEs roll on
                         their own cooldown lattices and collide constantly.
        ratio          = 1.09 * 0.2429944897959184 = 0.264863993877551
        1500 * 1.264863993877551 = 1897.2959908163266

    Only the ACTIVE factor carries the finite-window uptime. `E[B]` is a
    SPECIAL-bucket quantity and `_special_occupancy` has no lattice.

    `==` against the hand-built product, never `approx`: the implementation
    computes it as literally that product, so any difference is a change of rule
    and not of rounding.

    **The between-bucket covariance is absent and that is a decision, not
    an omission.** `Cov(B, A)` is about zero: `E[SA]/(E[S]E[A]) = 0.9976 +/-
    0.0087` in steady state over the captured decks and every chart; the
    whole-song 1.0747 is the ACTIVE lattice's start-of-song dead zone. Applying
    it would double-count that dead zone, which is modelled explicitly by
    `_finite_window_uptime` rather than assumed away by a duty cycle. Anyone
    adding one has to break the `!=` below."""
    team = _team(
        _active("a", 40.0), _active("b", 30.0), _special("s1", 50.0), _special("s2", 40.0)
    )
    total = score_fast(team, _cfg())

    expected_b = 0.50 * 0.10 + 0.40 * 0.10
    expected_active_max = (
        0.40 * ACTIVE_OCCUPANCY
        + 0.30 * ACTIVE_OCCUPANCY * (1.0 - ACTIVE_OCCUPANCY)
    )
    assert expected_active_max == pytest.approx(0.2429944897959184)
    assert total == BASE_STAT_SUM * (1.0 + (1.0 + expected_b) * expected_active_max)
    assert total == pytest.approx(1897.2959908163266)

    # The steady-state uptime, with `E[max_ACTIVE]` at 0.25852.
    assert total != pytest.approx(1922.6802)
    assert expected_active_max != pytest.approx(0.25852)
    # The independence factorisation: `B` discounted for a SPECIAL-on-SPECIAL
    # overlap the chart says cannot happen, `0.50*0.10 + 0.40*0.10*(1-0.10)`
    # = 0.086. That reads 1895.838... at this uptime and 1921.12908 at the
    # steady-state one; neither may be produced.
    assert total != pytest.approx(BASE_STAT_SUM * (1.0 + 1.086 * expected_active_max))
    assert total != pytest.approx(1921.12908)
    # And the additive model, which adds the SPECIAL bucket to the ratio
    # instead of multiplying by it — 1993.4917... here, 1500 * 1.34452 at the
    # steady-state uptime.
    assert total != pytest.approx(
        BASE_STAT_SUM * (1.0 + expected_active_max + 0.086)
    )
    assert total != pytest.approx(BASE_STAT_SUM * 1.34452)
    # A between-bucket covariance factor of the measured whole-song 1.0747 would
    # land here. It is deliberately NOT applied.
    assert total != pytest.approx(
        BASE_STAT_SUM * (1.0 + (1.0 + 1.0747 * expected_b) * expected_active_max)
    )


# --- Interaction with Score Support, which shares `B` -----------------------


def test_a_special_and_a_score_support_add_inside_b_because_they_sit_in_two_buckets():
    """Both are `ScoreUpBoostSkillEffect`s, so both are in `B` — but `B` is
    `max_ACTIVE + max_SPECIAL`, and a PASSIVE support is not special. A 50 %
    PASSIVE support, an ACTIVE Score UP at 20, a SPECIAL Score UP at 50:

        B     = 0.50 (support, occupancy 1.0) + 0.50 * 0.10 (SPECIAL) = 0.55
        ratio = 1.55 * 0.20 * 0.4242857142857143
              = 1.55 * 59.4/700 = 92.07/700 = 0.1315285714285714
        1500 * 1.1315285714285714 = 1697.2928571428572

    where 0.4242857142857143 is `0.46 * 118.8/128.8` — the ACTIVE's finite-window
    occupancy. The support's own occupancy is 1.0 and stays 1.0: a PASSIVE is live
    for the whole run and has no activation lattice to be cut by.

    Had the SPECIAL competed with the support in ONE bucket, `B` would be 0.50
    and the total `1500 * (1 + 1.50 * 59.4/700) = 1690.9285714285713`. Under a
    steady-state uptime the two would read 1713.9 and 1707.0."""
    support = make_card(
        "sup",
        passive=make_skill(
            "PASSIVE", family="score_support", effect_type="score", magnitude=50.0
        ),
    )
    team = _team(_active("a", 20.0), _special("s", 50.0), support)

    assert score_fast(team, _cfg()) == pytest.approx(1697.2928571428572)
    # One shared bucket, at this uptime and at the steady-state one.
    assert score_fast(team, _cfg()) != pytest.approx(1690.9285714285713)
    assert score_fast(team, _cfg()) != pytest.approx(1707.0)
    assert score_fast(team, _cfg()) != pytest.approx(1713.9)  # steady-state uptime


def test_two_specials_do_not_compete_inside_b_because_the_chart_fires_them_apart():
    """**The overlap half, at the smallest scale that shows it.** Within the
    SPECIAL bucket `B` is still an `E[max]` — but the maximum is taken over
    windows the chart makes DISJOINT, and the time-average of a maximum over
    disjoint windows is the plain sum of the pieces. ACTIVE 40 under SPECIALs
    50 and 40:

        B     = 0.50 * 0.10 + 0.40 * 0.10  =  0.09
        ratio = 1.09 * 0.40 * 0.4242857142857143
              = 1.09 * 118.8/700 = 129.492/700 = 0.1849885714285714
        1500 * 1.1849885714285714 = 1777.4828571428573

    `B` is the plain SPECIAL occupancy sum, 0.09 — `_special_occupancy` has no
    lattice. The ACTIVE under it carries the finite-window occupancy
    `0.46 * 118.8/128.8 = 0.4242857142857143` rather than the flat 0.46 of a
    steady-state duty cycle, because the checks at 10, 20, ... 120 leave the
    opening cooldown dead.

    The disjointness holds over every chart the game ships: the charts carry
    five ascending `#mmm0B` cues whose consecutive gaps have a minimum of
    **9.88 s**, against SPECIAL durations of 7.0 .. 14.0 s over every card. No
    gap can be bridged by the shortest SPECIAL and under 1 % by the longest.
    Non-adjacent cues cannot overlap at all: two consecutive gaps sum to at
    least 19.76 s.

    The rival reading is the independence discount
    `B = 0.50*0.10 + 0.40*0.10*(1 - 0.10) = 0.086`, for a collision the chart
    forbids; it is asserted as `!=`, which is exactly the assertion a silent
    regression has to fail. At a steady-state occupancy of 0.46 the two
    readings would be 1799.736 (independent) and 1800.84 (disjoint)."""
    team = _team(_active("a", 40.0), _special("s1", 50.0), _special("s2", 40.0))

    assert score_fast(team, _cfg()) == pytest.approx(1777.4828571428573)
    # The independence walk inside the SPECIAL bucket, `B = 0.086`, at this
    # uptime: 1500 * (1 + 1.086 * 118.8/700) = 1776.4645714285716.
    assert score_fast(team, _cfg()) != pytest.approx(1776.4645714285716)
    # The same two readings under a steady-state uptime.
    assert score_fast(team, _cfg()) != pytest.approx(1799.736)
    assert score_fast(team, _cfg()) != pytest.approx(1800.84)


def test_the_independence_discount_the_chart_removes_is_the_measured_ten_to_seventeen_percent():
    """What the overlap half is worth, sized on a fixture rather than only in a
    document. Five SPECIALs at magnitude 50 and occupancy 0.10 — one per deck
    slot, one per `#mmm0B` cue, the real team shape:

        disjoint (chart)    5 * 0.50 * 0.10                    = 0.25
        independent         0.05 * (1 + 0.9 + 0.81 + 0.729 + 0.6561)
                                                               = 0.204755

    a **-18.10 %** understatement, close to the -10.6 % .. -17.3 % the
    discount costs on real captured decks (their SPECIALs differ in magnitude,
    which softens it). `B` is read off the breakdown rather than inferred from
    a total, because `combined_support_rate` IS `B`."""
    team = _team(*(_special(f"s{index}", 50.0) for index in range(5)))
    breakdown = score_with_breakdown(team, _cfg())

    assert breakdown.combined_support_rate == pytest.approx(0.25)
    independent = 0.05 * (1.0 + 0.9 + 0.9**2 + 0.9**3 + 0.9**4)
    assert independent == pytest.approx(0.204755)  # independence discount
    assert breakdown.combined_support_rate != pytest.approx(independent)
    assert (independent - 0.25) / 0.25 == pytest.approx(-0.18098)


# --- Structure, reporting and parity ---------------------------------------


def test_the_boost_predicate_routes_special_score_ups_and_only_those():
    """The one decision the collapse is implemented as. Reads real
    `ResolvedSkill`s off a real team rather than hand-building them, so a change
    to how `resolve_team_gates` labels a slot cannot silently bypass it."""
    team = _team(_active("a", 40.0), _special("s", 50.0))
    resolved = resolve_team_gates(team, _cfg().run_state)
    by_kind = {r.skill.kind: r for r in resolved if r.skill.effect_type == "score"}

    assert set(by_kind) == {"ACTIVE", "SPECIAL"}, "the fixture must carry both kinds"
    assert _score_up_implements_boost(by_kind["SPECIAL"]) is True
    assert _score_up_implements_boost(by_kind["ACTIVE"]) is False


def test_the_breakdown_reports_the_special_inside_b_not_inside_the_score_pool():
    """The SPECIAL's own row still reports what it would
    contribute alone (`0.50 * 0.10 = 0.05`), because that is what a
    `weighted_rate` has always meant — but the number that was actually used
    puts it in `combined_support_rate`:

        combined_support_rate = 0.05
        combined_score_rate   = 1.05 * 0.40 * 0.4242857142857143
                              = 1.05 * 118.8/700 = 124.74/700 = 0.1782

    The SPECIAL's own `weighted_rate` is 0.05 — `_special_occupancy` has no
    lattice — while the ACTIVE maximum it multiplies is 118.8/700 =
    0.1697142857142857 with the finite-window uptime (0.184 at steady state).

    A reader who added the score rows would get `0.1697142857... + 0.05 =
    0.2197142857...` and be wrong by the whole collapse (0.234 at steady
    state). `combined_support_rate` is where to look."""
    breakdown = score_with_breakdown(_team(_active("a", 40.0), _special("s", 50.0)), _cfg())
    special_rows = [c for c in breakdown.contributions if c.slot_kind == "SPECIAL"]

    assert len(special_rows) == 1
    assert special_rows[0].pool == "score"
    assert special_rows[0].weighted_rate == pytest.approx(0.05)
    assert breakdown.combined_support_rate == pytest.approx(0.05)
    assert breakdown.combined_score_rate == pytest.approx(0.1782)
    assert breakdown.combined_score_rate != pytest.approx(0.1932)  # steady-state uptime
    # The added score rows, at this uptime and at the steady-state one.
    assert breakdown.combined_score_rate != pytest.approx(0.21971428571428575)
    assert breakdown.combined_score_rate != pytest.approx(0.234)


def test_batch_and_single_paths_stay_bit_identical_under_the_collapse():
    """`score_card_set` re-derives the boost/ratio routing per
    leader from its cached `(bucket, magnitude, occupancy)` terms; that must cost
    nothing in the last bit, so this is `==` and never `approx`. The member set
    deliberately mixes both populations and both buckets."""
    outfit = make_card(
        "outfit_support",
        costume_skill=make_skill(
            "COSTUME", family="score_support", effect_type="score", magnitude=60.0
        ),
    )
    members = (
        _active("a", 40.0),
        _special("s1", 50.0),
        _active("b", 30.0),
        _special("s2", 40.0),
        make_card("m5"),
    )
    cfg = _cfg()

    batch = score_card_set(members, (outfit,), cfg)
    single = score_fast(make_team([outfit, *members]), cfg)

    assert len(batch) == 1
    assert batch[0].total_score == single


def test_the_refuted_summed_model_now_agrees_inside_b_and_still_disagrees_on_the_actives():
    """`SummedActiveSkills` stays runnable so the rival sum model is
    assertable with one `dataclasses.replace` — and the disjoint SPECIAL
    windows decide WHERE the two models differ, which is a result rather than a
    coincidence.

    **They agree on any all-SPECIAL bucket**, because the disjoint
    time-average of a maximum IS the sum. Two SPECIALs, 50 and 40:

        max  : 0.50*0.10 + 0.40*0.10 (no discount, disjoint) = 0.09
        sum  : 0.50*0.10 + 0.40*0.10                         = 0.09

    That is not the sum model being vindicated; it is `E[max]` and `sum`
    being the same function on mutually exclusive events.

    **They still disagree wherever windows really can overlap** — the ACTIVE
    bucket, where two skills roll on their own cooldown lattices. With
    `A = 0.46 * 118.8/128.8 = 0.4242857142857143 = 297/700`:

        max  : 0.40*A + 0.30*A*(1 - A) = 0.2429944897959184
        sum  : 0.40*A + 0.30*A         = 0.7 * 297/700 = 0.297   (exactly)
        with B = 0.05:  1.05 * 0.2429944897959184 = 0.2551442142857143
                                                 vs  1.05 * 0.297 = 0.31185
        1500 * 1.2551442142857143 = 1882.7163214285715
                                  vs  1500 * 1.31185 = 1967.775

    The finite-window uptime scales both models by the same factor and does
    not touch which of them the `exclusive` flag makes agree: the agreement is
    on the SPECIAL bucket, which has no lattice. Under a steady-state uptime
    these two totals would read 1907.169 and 2007.15."""
    one_each = _team(_active("a", 40.0), _special("s", 50.0))
    assert score_fast(one_each, _cfg("sum")) == pytest.approx(1767.3)
    assert score_fast(one_each, _cfg("max")) == pytest.approx(1767.3)
    assert score_fast(one_each, _cfg("max")) != pytest.approx(1789.8)  # steady-state uptime

    two_specials = _team(_active("a", 40.0), _special("s1", 50.0), _special("s2", 40.0))
    assert score_fast(two_specials, _cfg("sum")) == pytest.approx(1777.4828571428573)
    assert score_fast(two_specials, _cfg("max")) == pytest.approx(1777.4828571428573)
    # The independence walk inside the SPECIAL bucket would give 1776.4645714...
    # here; at a steady-state uptime it gives 1799.736 against a disjoint
    # 1800.84, and `max` must match neither.
    assert score_fast(two_specials, _cfg("max")) != pytest.approx(1776.4645714285716)
    assert score_fast(two_specials, _cfg("max")) != pytest.approx(1799.736)
    assert score_fast(two_specials, _cfg("max")) != pytest.approx(1800.84)  # steady-state uptime

    two_actives = _team(_active("a", 40.0), _active("b", 30.0), _special("s", 50.0))
    assert score_fast(two_actives, _cfg("max")) == pytest.approx(1882.7163214285715)
    assert score_fast(two_actives, _cfg("sum")) == pytest.approx(1967.775)
    assert score_fast(two_actives, _cfg("max")) != pytest.approx(1907.169)  # steady-state uptime
    assert score_fast(two_actives, _cfg("sum")) != pytest.approx(2007.15)  # steady-state uptime


def test_the_exclusivity_predicate_marks_special_windows_and_only_those():
    """The one decision the overlap half is implemented as, read off real
    `ResolvedSkill`s rather than hand-built ones, exactly as
    `test_the_boost_predicate_routes_special_score_ups_and_only_those` does for
    the structural half. A SPECIAL fires once, at its own deck slot's `#mmm0B`
    cue; an ACTIVE rolls on a cooldown lattice against a probability and two of
    them collide constantly."""
    team = _team(_active("a", 40.0), _special("s", 50.0))
    resolved = resolve_team_gates(team, _cfg().run_state)
    by_kind = {r.skill.kind: r for r in resolved if r.skill.effect_type == "score"}

    assert set(by_kind) == {"ACTIVE", "SPECIAL"}, "the fixture must carry both kinds"
    assert _windows_are_mutually_exclusive(by_kind["SPECIAL"]) is True
    assert _windows_are_mutually_exclusive(by_kind["ACTIVE"]) is False


def test_the_term_a_gate_suppressed_carries_its_exclusivity_anyway():
    """A `gate_not_met` SPECIAL is zeroed in magnitude AND occupancy, but it must
    still be labelled exclusive: the flag decides how its bucket-mates are
    discounted, and a suppressed SPECIAL silently becoming an INDEPENDENT zero
    would put a `(1 - 0.0)` discount back into a bucket that should have none.
    The numbers happen to coincide at zero occupancy, so this asserts the
    label."""
    team = _team(_active("a", 40.0), _special("s", 50.0))
    resolved = resolve_team_gates(team, _cfg().run_state)
    special = next(r for r in resolved if r.skill.kind == "SPECIAL")
    cfg = _cfg()

    applied = _bucket_entry(special, cfg, 0.0, 1.0, "applied")
    suppressed = _bucket_entry(special, cfg, 0.0, 1.0, "gate_not_met")

    # The 5th element is `ActiveTerm.interval`: `0` for a SPECIAL, which has no
    # cooldown lattice.
    assert applied == ("SPECIAL", 0.50, pytest.approx(0.10), 0, True)
    assert suppressed == ("SPECIAL", 0.0, 0.0, 0, True)
