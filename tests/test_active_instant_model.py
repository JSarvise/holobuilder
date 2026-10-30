"""How do two ACTIVE score-ups that are live at the SAME instant of a real
run combine?

Two hypotheses, and they differ by ~2-3 % of a song's total score:

- **H1 INDEPENDENT** (the default, `ExpectedMaximumActiveSkill`): each live
  ACTIVE rolls its own Bernoulli(p) every check and the run sees
  `E[max m_i X_i]`.
- **H2 EXCLUSIVE-NORMALISED** (`ExclusiveNormalizedActiveSkill`): co-live
  ACTIVEs share ONE roll — `sum m_i p_i / max(1, sum p_i)`. This is the law
  that reproduces the deck screen's Score-Bonus ACTIVE line bit-exactly, read
  as an in-run rule. Note it is NOT uniformly smaller than H1: while
  `sum p <= 1` the effects ADD, because under an exclusive roll a smaller
  skill firing is not evidence a bigger one was suppressed.

H2 is selectable behind a SWITCH that defaults to H1, and these tests pin both
halves of that: the arithmetic of the alternative rule, and — the one that
matters for trust — that leaving the switch alone changes nothing at all.
"""

from __future__ import annotations

import dataclasses

import pytest

from hbuilder.domain.scoring import score_card_set
from hbuilder.domain.scoring_models import (
    ACTIVE_INSTANT_MODELS,
    ACTIVE_STACKING_MODELS,
    ExclusiveNormalizedActiveSkill,
)
from hbuilder.domain.song import ChartNote, SongChart, score_song_detailed

from conftest import make_card, make_scoring_config, make_skill, make_team

EXCLUSIVE = ACTIVE_INSTANT_MODELS["exclusive"]
MAX = ACTIVE_STACKING_MODELS["max"]


def _term(magnitude, occupancy, bucket="ACTIVE"):
    """The `(bucket, magnitude, occupancy, interval, exclusive)` 5-tuple the
    per-song scorer's `_ratio_at` builds: `occupancy` is the skill's activation
    PROBABILITY at this instant, not a fraction of the run."""
    return (bucket, magnitude, occupancy, 0, False)


# ==========================================================================
# 1. The rule itself
# ==========================================================================


def test_a_lone_active_is_worth_magnitude_times_probability_under_both_models():
    """The two hypotheses differ only where windows COINCIDE. One live skill is
    the same number either way — and it has to be, or the switch would move
    every note of every chart rather than the ~40 % where two actives overlap."""
    terms = [_term(0.55, 0.46)]
    assert EXCLUSIVE.combine(terms) == pytest.approx(0.253)
    assert EXCLUSIVE.combine(terms) == pytest.approx(MAX.combine(terms))


def test_two_co_live_actives_ADD_while_their_probabilities_fit_under_one():
    """`(0.55*0.46 + 0.60*0.46) / max(1, 0.92) = 0.5290`.

    This is the counter-intuitive half of H2 and the reason it scores HIGHER
    than H1 rather than lower: with 0.92 of the roll allocated there is still
    slack, so neither skill's activation crowds the other out. H1 walks in
    descending magnitude and discounts the smaller by the chance the bigger
    already fired: `0.60*0.46 + 0.55*0.46*(1-0.46) = 0.276 + 0.13662 = 0.41262`.
    So on this instant H2 is +28 %."""
    terms = [_term(0.55, 0.46), _term(0.60, 0.46)]
    assert EXCLUSIVE.combine(terms) == pytest.approx(0.5290)
    assert MAX.combine(terms) == pytest.approx(0.41262)
    assert EXCLUSIVE.combine(terms) > MAX.combine(terms)


def test_three_co_live_actives_renormalise_once_the_roll_is_fully_allocated():
    """`sum p = 1.38 > 1`, so the instant's value is the probability-weighted
    MEAN of the magnitudes, not their sum: the roll saturates.

    `(0.55 + 0.60 + 0.50) * 0.46 / 1.38 = 0.55` — exactly the mean of the three
    magnitudes, since all three share one probability."""
    terms = [_term(0.55, 0.46), _term(0.60, 0.46), _term(0.50, 0.46)]
    assert EXCLUSIVE.combine(terms) == pytest.approx(0.55)


def test_saturation_makes_a_fourth_weak_skill_actively_HARMFUL():
    """The sharp edge of the exclusive law, and the reason it is a real
    hypothesis and not a smoothing: once the roll is saturated, adding a WEAK
    co-live skill DILUTES the mean. Three 0.60s at p=0.46 give 0.60; a fourth
    at 0.10 drags the instant down to `(3*0.60 + 0.10)*0.46/1.84 = 0.475`.

    Under H1 a weak skill can never hurt (`E[max]` is monotone), so any future
    trace that shows a weak card LOWERING a real score is evidence for H2."""
    strong = [_term(0.60, 0.46)] * 3
    assert EXCLUSIVE.combine(strong) == pytest.approx(0.60)
    assert EXCLUSIVE.combine(strong + [_term(0.10, 0.46)]) == pytest.approx(0.475)
    assert MAX.combine(strong + [_term(0.10, 0.46)]) >= MAX.combine(strong)


def test_an_empty_instant_is_exactly_zero():
    assert EXCLUSIVE.combine([]) == 0.0


def test_a_suppressed_skill_contributes_nothing_and_dilutes_nothing():
    """A gate that did not fire arrives with occupancy 0 (`_term_for`). It must
    not join the denominator — otherwise a skill whose condition FAILED would
    still crowd out the ones that fired."""
    live = [_term(0.55, 0.46)]
    assert EXCLUSIVE.combine(live + [_term(0.9, 0.0)]) == pytest.approx(
        EXCLUSIVE.combine(live)
    )


# ==========================================================================
# 2. Buckets stay separate
# ==========================================================================


def test_the_special_bucket_is_never_normalised_against_the_active_one():
    """ACTIVE and SPECIAL are the client's two `Math::Max` partitions
    (`LiveSkillEffectData.isSpecialSkill`) and the game ADDS the two maxima. A
    SPECIAL that is live must not appear in an ACTIVE roll's denominator: the
    SPECIAL did not roll, it fired on the chart's cue."""
    active = [_term(0.55, 0.46)]
    special = [_term(0.30, 1.0, bucket="SPECIAL")]
    assert EXCLUSIVE.combine(active + special) == pytest.approx(
        EXCLUSIVE.combine(active) + EXCLUSIVE.combine(special)
    )


def test_a_certain_effect_alone_in_its_bucket_keeps_its_full_magnitude():
    """`max(1, sum p)` — the clamp at 1 — is what stops a single certain effect
    being divided by its own probability."""
    assert EXCLUSIVE.combine([_term(0.30, 1.0, bucket="SPECIAL")]) == pytest.approx(0.30)


# ==========================================================================
# 3. The registry contract the rest of the codebase relies on
# ==========================================================================


def test_the_instant_registry_maps_the_name_to_the_instance():
    """The key is the HYPOTHESIS's name, which is why this registry does not
    carry the `key == model.name` assertion the other four do: `independent` is
    deliberately the shipped `max` instance under a name that says what it
    MEANS in this context, rather than a fifth combiner."""
    assert isinstance(ACTIVE_INSTANT_MODELS["exclusive"], ExclusiveNormalizedActiveSkill)
    assert ACTIVE_INSTANT_MODELS["exclusive"].name == "exclusive"


def test_independent_resolves_to_the_shipped_max_walk_so_both_names_exist():
    """`independent` is not a second implementation of `max` — it IS `max`. A
    caller that names the hypothesis gets the shipped instance, so there is
    exactly one E[max] walk in the codebase."""
    assert ACTIVE_INSTANT_MODELS["independent"] is ACTIVE_STACKING_MODELS["max"]


# ==========================================================================
# 4. The switch, wired through a real per-song score
# ==========================================================================


def _active_scorer(card_id: str, magnitude: float, *, interval=20.0, duration=10.0):
    """One card whose only skill is an ACTIVE Score UP — the same shape
    `tests/test_score_song.py` uses, so the two files describe one model."""
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


def _chart(times):
    return SongChart(
        music_id="m0001",
        difficulty="easy",
        difficulty_level=5,          # music coefficient exactly 1.0
        live_score_coefficient_permil=4,
        notes=tuple(ChartNote(time=t, perfect_permil=1000) for t in times),
        special_cues=(10.0, 20.0, 30.0, 40.0, 50.0),
    )


def _cfg(**overrides):
    """Probability 0.5 per tier, so `sum p` crosses 1 at the third co-live
    skill and both regimes of the exclusive law are reachable by hand."""
    base = make_scoring_config(probability_tiers={"Low": 0.5, "Medium": 0.5, "High": 0.5})
    return dataclasses.replace(base, **overrides) if overrides else base


def test_the_default_config_names_no_instant_model():
    """The guarantee that makes this safe to ship: `active_instant_model`
    defaults to `None`, meaning "use `active_stacking_model`" — the SAME object
    through the SAME call, so there is no second code path to drift."""
    assert make_scoring_config().active_instant_model is None


def test_naming_the_independent_model_explicitly_is_bit_identical_to_the_default():
    """`==`, not `approx`: selecting H1 by name must reproduce the default
    score to the last bit, or the switch itself has changed the answer."""
    members = (_active_scorer("card-a", 50.0), _active_scorer("card-b", 60.0)) + tuple(
        make_card(f"card-{i}") for i in range(3)
    )
    chart = _chart((1.0, 21.0, 25.0, 45.0))
    default = score_song_detailed(
        members, (make_card("leader"),), chart, _cfg(), deck_power=2000
    )[0]
    named = score_song_detailed(
        members,
        (make_card("leader"),),
        chart,
        _cfg(active_instant_model=ACTIVE_INSTANT_MODELS["independent"]),
        deck_power=2000,
    )[0]
    assert named.total_score == default.total_score


def test_exclusive_scores_higher_where_two_active_windows_coincide():
    """Both actives run interval 20 s / duration 10 s, so both are live over
    [20,30) — the co-live instant H1 and H2 disagree about — and neither is live
    at t = 1.

    At a note inside the shared window, with p = 0.5 each:
        H1  E[max] = 0.60*0.5 + 0.50*0.5*(1-0.5) = 0.425
        H2  (0.60 + 0.50)*0.5 / max(1, 1.0)      = 0.550
    At t = 1 no ACTIVE is live, so both models score the bare note identically.
    The whole difference therefore lands on the in-window notes."""
    members = (_active_scorer("card-a", 50.0), _active_scorer("card-b", 60.0)) + tuple(
        make_card(f"card-{i}") for i in range(3)
    )
    chart = _chart((1.0, 21.0, 25.0))
    independent = score_song_detailed(
        members, (make_card("leader"),), chart, _cfg(), deck_power=2000
    )[0]
    exclusive = score_song_detailed(
        members,
        (make_card("leader"),),
        chart,
        _cfg(active_instant_model=EXCLUSIVE),
        deck_power=2000,
    )[0]
    assert exclusive.total_score > independent.total_score


def test_exclusive_leaves_a_song_with_one_active_exactly_where_it_was():
    """The switch must be inert where nothing coincides — one live ACTIVE is
    `m*p` under both laws. If this ever fails, the exclusive model is reaching
    terms it has no claim on (the Score Support pool, or the SPECIAL bucket)."""
    members = (_active_scorer("card-a", 50.0),) + tuple(
        make_card(f"card-{i}") for i in range(4)
    )
    chart = _chart((1.0, 21.0, 25.0, 45.0))
    independent = score_song_detailed(
        members, (make_card("leader"),), chart, _cfg(), deck_power=2000
    )[0]
    exclusive = score_song_detailed(
        members,
        (make_card("leader"),),
        chart,
        _cfg(active_instant_model=EXCLUSIVE),
        deck_power=2000,
    )[0]
    assert exclusive.total_score == independent.total_score


def test_the_instant_model_never_reaches_the_run_average_ranking_scorer():
    """The hazard this switch is shaped to avoid. `total_score` is the RANKING
    objective and its occupancies mean "fraction of the run live", not "the
    probability this skill is up right now" — the exclusive law is meaningless
    against them. Selecting it must therefore leave `score_card_set` untouched.
    """
    members = (_active_scorer("card-a", 50.0), _active_scorer("card-b", 60.0)) + tuple(
        make_card(f"card-{i}") for i in range(3)
    )
    leaders = (make_card("leader"),)
    plain = score_card_set(members, leaders, _cfg())
    switched = score_card_set(members, leaders, _cfg(active_instant_model=EXCLUSIVE))
    assert [s.total_score for s in switched] == [s.total_score for s in plain]
