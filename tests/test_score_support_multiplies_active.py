"""`score_support` MULTIPLIES the ACTIVE pool rather than adding to it.

The game's `CalcScoreUpRatio`:

    B    = max_ACTIVE ScoreUpBoostByRatio + max_SPECIAL ScoreUpBoostByRatio
    v(e) = (1 + SkillEffectUpPermilUp/1000 + B) x ScoreUpByRatio(e)

So a support multiplies (`mulss xmm6, xmm0`), applies to the ACTIVE pool only
(there is no SPECIAL ratio term to raise), and several supports combine as a
max-of-two-maxima, never through `STACKING_MODELS`.

Every SPECIAL Score UP is itself a `ScoreUpBoostSkillEffect`, so `max_SPECIAL
v(e)` is structurally zero and `B` has nothing in the SPECIAL bucket to
multiply. The boost population `B` is the team's `score_support` slots **plus**
its SPECIAL Score UPs.

Three independent lines of evidence rule out a flat addend in the score pool,
and the last of them is a measurement rather than a reading:

- the game's own effect ids — every SPECIAL is `score_up_EFFECT_up_permil_up`
  and every Score Support outfit is `live_active_skill_EFFECT_up_permil_up`,
  against a plain `score_up_permil_up` on every ACTIVE;
- another community optimiser reaches the same conclusion independently;
- solving the reference duration from ten captured decks under both readings,
  the multiplier reading gives a constant to within 6.2 %, the addend reading
  spreads 90 %.

The tests here pin the *consequences*, each with a number computed by hand in
the test's own docstring rather than copied from the implementation.

Fixtures come from `tests/conftest.py`. `make_team` takes `cards[0]` as the
LEADER — whose own normal slots are never emitted — and `cards[1:6]` as
the five members, so every ACTIVE under test sits at index 1 or later.
"""

from __future__ import annotations

import pytest

from hbuilder.domain.card import EffectTarget
from hbuilder.domain.scoring import score_card_set, score_fast, score_with_breakdown

from conftest import make_card, make_scoring_config, make_skill, make_team

# conftest's STATS baseline is 300 total per card, so five members are 1500.0.
BASE_STAT_SUM = 1500.0

# An ACTIVE's uptime is the FINITE activation lattice over
# `REFERENCE_RUN_SECONDS = 128.8`, not the steady-state duty cycle `min(1, d/i)`.
# For the `duration=10 / interval=10` scorer below the checks sit at 10, 20, ...
# 120 — there is no check at t=0, so the run opens with one whole cooldown of dead
# zone — each window runs straight into the next and the last is truncated by the
# end of the run, so the effect is live from 10 s to 128.8 s:
#
#     u = (128.8 - 10) / 128.8 = 118.8 / 128.8 = 0.922360248447205
#
# The steady-state reading would give `min(1, 10/10) = 1.0`. Every ACTIVE number
# below carries this factor and names it `u`, so each derivation shows where it
# entered; the steady-state answers are asserted against as values to stay away from.
ACTIVE_UPTIME = 118.8 / 128.8
# u * Medium(0.46) * _as_rate(20.0) = 0.08485714285714285 (0.092 steady-state)
ACTIVE_EV = ACTIVE_UPTIME * 0.46 * 0.20


def _active_scorer(card_id: str = "member_active"):
    return make_card(
        card_id,
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            effect_type="score",
            duration=10.0,
            interval=10.0,
            probability_tier="Medium",
            magnitude=20.0,
        ),
    )


def _support_passive(card_id: str, magnitude: float, target: object = None):
    return make_card(
        card_id,
        passive=make_skill(
            "PASSIVE",
            family="score_support",
            effect_type="score",
            magnitude=magnitude,
            target=target,
        ),
    )


def _team(cards: list):
    return make_team(cards)


def test_a_score_support_skill_is_routed_to_the_support_pool_never_the_score_pool():
    team = _team(
        [
            make_card("outfit"),
            _active_scorer(),
            _support_passive("member_support", 50.0),
            make_card("m3"),
            make_card("m4"),
            make_card("m5"),
        ]
    )
    breakdown = score_with_breakdown(team, make_scoring_config())
    support = [c for c in breakdown.contributions if c.family == "score_support"]

    assert support, "the team must actually carry a score_support skill"
    assert all(c.pool == "support" for c in support)
    assert all(c.pool != "support" for c in breakdown.contributions if c.family != "score_support")


def test_support_multiplies_the_active_contribution_instead_of_adding_to_it():
    """One ACTIVE at `u * 0.46 * 0.20` and one team-wide 50 % support.

        u      = 118.8 / 128.8 = 0.922360248447205
        ACTIVE = u * 0.46 * 0.20 = 0.08485714285714285

    Multiplier reading : 1500 * (1 + 0.08485714... * 1.50) = 1690.9285714285713
    Addend reading     : 1500 * (1 + 0.08485714... + 0.50) = 2377.285714285714

    Under the steady-state uptime reading the ACTIVE would be a flat 0.092 and
    the two answers 1707.0 and 2388.0."""
    team = _team(
        [
            make_card("outfit"),
            _active_scorer(),
            _support_passive("member_support", 50.0),
            make_card("m3"),
            make_card("m4"),
            make_card("m5"),
        ]
    )
    total = score_fast(team, make_scoring_config())

    assert total == pytest.approx(1690.9285714285713)
    assert total != pytest.approx(2388.0)  # the addend reading, on the steady-state uptime
    assert total != pytest.approx(1707.0)  # the steady-state uptime answer


def test_support_with_no_active_in_the_team_contributes_exactly_nothing():
    """The structural claim, and the one most worth pinning: a Score Support
    skill raises the ACTIVE skill's effect, so with no ACTIVE effect in play it
    has nothing to raise. A team of pure support scores its bare stat sum."""
    team = _team(
        [
            make_card("outfit"),
            _support_passive("member_support_a", 50.0),
            _support_passive("member_support_b", 70.0),
            make_card("m3"),
            make_card("m4"),
            make_card("m5"),
        ]
    )
    assert score_fast(team, make_scoring_config()) == pytest.approx(BASE_STAT_SUM)


def test_two_supports_in_one_bucket_do_not_stack_only_the_larger_counts():
    """Score Support does not stack. The game's `B` term is `max over ACTIVE + max over SPECIAL` of
    `ScoreUpBoostByRatio` — the same two-bucket max-then-add rule as the score
    pool itself, because `CalcScoreUpBoostByRatioBase` is inlined verbatim into
    `CalcScoreUpRatio`. It never passes through `STACKING_MODELS`.

    Two PASSIVE supports at 10 % and 14 % both sit in the ACTIVE bucket at
    occupancy 1.0 — a PASSIVE is always on, so no uptime term touches `B` —
    while the ACTIVE they raise carries the finite-window `u = 118.8 / 128.8`:

        B      = max(0.10, 0.14) = 0.14
        ACTIVE = 0.10488 * u     = 0.09673714285714285
        1500 * 1.09673714285714285 = 1645.1057142857144

    An additive stack would sum the two to 0.24, giving 1657.8342857142857 on
    this uptime and 1671.12 on the steady-state uptime. A second copy of a support is worth nothing while the
    larger one is up — the same exclusivity rule the score pool follows, and for
    the same reason."""
    team = _team(
        [
            make_card("outfit"),
            _active_scorer(),
            _support_passive("member_support_a", 10.0),
            _support_passive("member_support_b", 14.0),
            make_card("m4"),
            make_card("m5"),
        ]
    )
    assert score_fast(team, make_scoring_config()) == pytest.approx(1645.1057142857144)
    assert score_fast(team, make_scoring_config()) != pytest.approx(1671.12)
    # the additive stack on this uptime, and the steady-state uptime answer
    assert score_fast(team, make_scoring_config()) != pytest.approx(1657.8342857142857)
    assert score_fast(team, make_scoring_config()) != pytest.approx(1657.32)


def test_a_count_targeted_support_boosts_only_its_recipients():
    """A count-targeted
    `score_support` reaches ONLY its `count` recipients. The deck panel's
    per-card `active_effect_up` is the sum of the supports feeding THAT card:
    a member PASSIVE `{kind: type, count: 2}` on a three-Cute deck showed
    +80/+80/0 — the third Cute got nothing. So targeting acts per recipient,
    inside the ACTIVE line as `(1 + SEU + B_i)`, not on a collection-wide scalar.

    Two ACTIVE scorers, one Cute and one Happy, and a support aimed at a single
    Cute card. ONLY the Cute scorer is raised:

        B_cute = 0.50 -> magnitude 0.20 * 1.50 = 0.30
        B_happy = 0    -> magnitude 0.20
        occupancy (both) = u * 0.46 = 0.42428571428571427, u = 118.8 / 128.8

        E[max] = 0.30 * 0.42428571...  +  0.20 * 0.42428571... * (1 - 0.42428571...)
               = 0.12728571428571428   +  0.04885346938775510
               = 0.17613918367346938
        1500 * 1.17613918367346938 = 1764.208775510204

    A collection-wide reading — `B` as one scalar, so a support that reaches
    ANYBODY boosts EVERYBODY — would give 1800.8488775510205 on this uptime
    (both magnitudes 0.30) and 1818.78 on the steady-state uptime; the
    per-recipient reading gives 1781.52 there. The deck panel is a
    server-computed observation of where targeting acts, so it decides. All ten `score_support`
    slots in `data/cards.json` target `{"kind": "all"}`, so no real card in the
    catalogue distinguishes the two readings — this pins the rule the code
    follows on the synthetic case that does."""
    cute_scorer = make_card(
        "member_cute",
        type_="Cute",
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            effect_type="score",
            duration=10.0,
            interval=10.0,
            probability_tier="Medium",
            magnitude=20.0,
        ),
    )
    happy_scorer = make_card(
        "member_happy",
        type_="Happy",
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            effect_type="score",
            duration=10.0,
            interval=10.0,
            probability_tier="Medium",
            magnitude=20.0,
        ),
    )
    supporter = _support_passive(
        "member_support",
        50.0,
        target=EffectTarget(kind="type", value="Cute", count=1),
    )
    team = _team(
        [
            make_card("outfit"),
            cute_scorer,
            happy_scorer,
            supporter,
            make_card("m4", type_="Happy"),
            make_card("m5", type_="Happy"),
        ]
    )
    assert score_fast(team, make_scoring_config()) == pytest.approx(1764.208775510204)
    # the collection-wide reading on this uptime, and both steady-state
    # uptime answers
    assert score_fast(team, make_scoring_config()) != pytest.approx(1800.8488775510205)
    assert score_fast(team, make_scoring_config()) != pytest.approx(1818.78)
    assert score_fast(team, make_scoring_config()) != pytest.approx(1781.52)


def test_an_outfit_score_support_multiplies_every_members_active():
    """The leader's outfit is a Score Support. Two
    identical scorers, a team-wide 60 % outfit support. Both magnitudes are
    raised, so the ordering does not change and the second copy is discounted
    by the first's occupancy — the concrete reason a second high-magnitude
    ACTIVE is worth much less than the first:

        occupancy (both) = u * 0.46 = 0.42428571428571427, u = 118.8 / 128.8
        magnitude (both) = 0.20 * 1.60 = 0.32

        E[max] = 0.32 * 0.42428571...  +  0.32 * 0.42428571... * (1 - 0.42428571...)
               = 0.13577142857142857   +  0.07816555102040816
               = 0.21393697959183675
        1500 * 1.21393697959183675 = 1820.9054693877551

    Under the summed pool this is (0.08485714... + 0.08485714...) * 1.60 ->
    1907.3142857142857; on the steady-state uptime the summed pool would be
    (0.092 + 0.092) * 1.60 = 0.2944 -> 1941.6 and this test's own answer
    1840.032."""
    outfit = make_card(
        "outfit_support",
        costume_skill=make_skill(
            "COSTUME", family="score_support", effect_type="score", magnitude=60.0
        ),
    )
    team = _team(
        [
            outfit,
            _active_scorer("member_active_a"),
            _active_scorer("member_active_b"),
            make_card("m3"),
            make_card("m4"),
            make_card("m5"),
        ]
    )
    assert score_fast(team, make_scoring_config()) == pytest.approx(1820.9054693877551)
    assert score_fast(team, make_scoring_config()) != pytest.approx(1941.6)
    # the summed pool on this uptime, and the steady-state uptime answer
    assert score_fast(team, make_scoring_config()) != pytest.approx(1907.3142857142857)
    assert score_fast(team, make_scoring_config()) != pytest.approx(1840.032)


def test_batch_and_single_paths_stay_bit_identical_on_a_supported_team():
    """`score_card_set` and `score_fast` share
    `_apply_support`, so the two paths must agree BIT-identically, not merely
    closely — `==`, never `pytest.approx`."""
    outfit = make_card(
        "outfit_support",
        costume_skill=make_skill(
            "COSTUME", family="score_support", effect_type="score", magnitude=60.0
        ),
    )
    members = (
        _active_scorer("member_active_a"),
        _support_passive("member_support", 25.0),
        _active_scorer("member_active_b"),
        make_card("m4"),
        make_card("m5"),
    )
    cfg = make_scoring_config()

    batch = score_card_set(members, (outfit,), cfg)
    single = score_fast(make_team([outfit, *members]), cfg)

    assert len(batch) == 1
    assert batch[0].total_score == single


def _special_scorer(card_id: str, magnitude: float, *, duration: float = 12.88):
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


def test_a_support_and_a_special_both_enter_b_and_add_across_the_two_buckets():
    """**The SPECIAL collapse.** A SPECIAL
    Score UP is a `ScoreUpBoostSkillEffect` exactly as a Score Support is, so it
    joins `B` rather than the ratio — and the two live in *different* buckets,
    so they add rather than competing. A 50 % PASSIVE support, one ACTIVE Score
    UP at 20 and one SPECIAL Score UP at 50:

        B     = max_ACTIVE boost + max_SPECIAL boost
              = 0.50 (support, occupancy 1.0)  +  0.50 * 0.10 (the SPECIAL)
              = 0.55
        ratio = 1.55 * 0.20 * (u * 0.46) = 0.31 * 0.42428571428571427
              = 0.13152857142857144
        1500 * 1.13152857142857144 = 1697.2928571428572

    The finite-window uptime touches only the ACTIVE's own occupancy: `B` does
    not move, because a PASSIVE support is always on and `_special_occupancy`
    reads `fires * duration / 128.8`.

    Two alternative readings this rules out. Putting the SPECIAL in the ratio and
    having the support multiply it there gives
    `0.20*1.50*(u*0.46) + 0.50*1.50*0.10`, total 1803.4285714285713 (1819.5 on
    the steady-state uptime). Raising only the ACTIVE bucket and leaving the
    SPECIAL a bare 0.05 gives 1765.9285714285713 (1782.0 on the steady-state
    uptime). The collapsed answer is below both, because the SPECIAL's 0.05 buys
    5 % of a 0.08485714... ACTIVE instead of 0.05 of the score."""
    team = _team(
        [
            make_card("outfit"),
            _active_scorer(),
            _special_scorer("member_special", 50.0),
            _support_passive("member_support", 50.0),
            make_card("m4"),
            make_card("m5"),
        ]
    )
    total = score_fast(team, make_scoring_config())

    assert total == pytest.approx(1697.2928571428572)
    assert total != pytest.approx(1819.5)  # SPECIAL-in-the-ratio reading
    assert total != pytest.approx(1782.0)  # ACTIVE-bucket-only reading
    # the same two readings recomputed on the finite-window uptime, then this
    # test's own steady-state uptime answer
    assert total != pytest.approx(1803.4285714285713)
    assert total != pytest.approx(1765.9285714285713)
    assert total != pytest.approx(1713.9)


def test_an_active_support_and_a_special_support_add_because_they_are_two_buckets():
    """`B` is `max_ACTIVE + max_SPECIAL`, so a support carried on a SPECIAL
    slot adds to one carried on a PASSIVE (which is not special, hence ACTIVE
    bucket) rather than losing to it:

        B = max(0.20) + max(0.30) = 0.50
        ACTIVE Score UP = 0.20 * 1.50 * (u * 0.46) = 0.30 * 0.42428571428571427
                        = 0.12728571428571428
        1500 * 1.12728571428571428 = 1690.9285714285713

    Two supports in the SAME bucket would give B = 0.30 and 1665.4714285714285
    (1638.0 on the steady-state uptime, where this test's own answer would be
    1707.0)."""
    special_support = make_card(
        "member_special_support",
        special=make_skill(
            "SPECIAL",
            family="score_support",
            effect_type="score",
            duration=128.8,  # occupancy exactly 1.0, so B reads its magnitude
            magnitude=30.0,
        ),
    )
    team = _team(
        [
            make_card("outfit"),
            _active_scorer(),
            _support_passive("member_support", 20.0),
            special_support,
            make_card("m4"),
            make_card("m5"),
        ]
    )
    total = score_fast(team, make_scoring_config())

    assert total == pytest.approx(1690.9285714285713)
    assert total != pytest.approx(1638.0)
    # one bucket on this uptime, and the steady-state uptime answer
    assert total != pytest.approx(1665.4714285714285)
    assert total != pytest.approx(1707.0)


def test_a_support_that_reaches_nobody_never_enters_the_collection():
    """An effect that is never created cannot boost anything: the game
    restricts a support at insertion time, which is exactly this. A support gated to a type no member carries resolves
    to an empty target set and contributes no `B` at all, leaving the bare
    ACTIVE `u * 0.46 * 0.20 = 0.08485714285714285` and
    1500 * 1.08485714285714285 = 1627.2857142857142.

    On the steady-state uptime the bare ACTIVE would be 0.092 and this
    1638.0."""
    supporter = _support_passive(
        "member_support",
        50.0,
        target=EffectTarget(kind="type", value="Cool", count=1),
    )
    team = _team(
        [
            make_card("outfit"),
            _active_scorer(),
            supporter,
            make_card("m3"),
            make_card("m4"),
            make_card("m5"),
        ]
    )
    assert score_fast(team, make_scoring_config()) == pytest.approx(1627.2857142857142)
    # the steady-state uptime answer
    assert score_fast(team, make_scoring_config()) != pytest.approx(1638.0)


def test_batch_and_single_paths_stay_bit_identical_across_both_buckets():
    """`score_card_set` caches a
    `(bucket, magnitude, occupancy)` term for supports as well as Score UPs,
    and rebuilds `B` per leader. `==`, never `approx`."""
    outfit = make_card(
        "outfit_support",
        costume_skill=make_skill(
            "COSTUME", family="score_support", effect_type="score", magnitude=60.0
        ),
    )
    members = (
        _active_scorer("member_active_a"),
        _special_scorer("member_special", 50.0),
        _support_passive("member_support", 25.0),
        _active_scorer("member_active_b"),
        make_card("m5"),
    )
    cfg = make_scoring_config()

    batch = score_card_set(members, (outfit,), cfg)
    single = score_fast(make_team([outfit, *members]), cfg)

    assert len(batch) == 1
    assert batch[0].total_score == single
