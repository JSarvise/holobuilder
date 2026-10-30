"""`ScoringConfig.active_uptime_model` selects which ACTIVE-skill uptime a run
averages: "finite" (the in-run finite-window count, WITH the start-of-song dead
zone) or "steady" (the deck-screen panel's steady-state `min(1, duration/
interval)`, no dead zone).

The deck-screen Score Bonus reproduces the server only under "steady": across
71 saved decks the
Active-line residual moves from median -8.1% ("finite") to +2.3% ("steady"),
because the deck screen is song-independent and so carries no first-cooldown
dead zone to discount. "finite" stays the default so every in-run/`total_score`
number is untouched; the deck-screen driver passes "steady".

The assertions pin the two readings against each other with `!=`, not a
direction-only check, so a silent revert to one value cannot pass under the
other.
"""
from __future__ import annotations

import dataclasses

from hbuilder.domain.scoring import _active_occupancy, _finite_window_uptime

from conftest import make_resolved, make_scoring_config, make_skill

# A long-cooldown active where the dead zone bites: interval (30 s) well above
# duration (8 s), so finite-window < steady-state by more than a rounding wobble.
_DURATION = 8.0
_INTERVAL = 30.0


def _occ(model: str) -> float:
    cfg = make_scoring_config(probability_tiers={"t": 1.0})
    cfg = dataclasses.replace(cfg, active_uptime_model=model)
    skill = make_skill(
        "ACTIVE", duration=_DURATION, interval=_INTERVAL, probability_tier="t"
    )
    resolved = make_resolved("card-x", "ACTIVE", skill, member="Holomem X")
    return _active_occupancy(resolved, cfg, team_rate_up=0.0)


def test_finite_is_the_default_and_is_the_finite_window_count():
    assert make_scoring_config().active_uptime_model == "finite"
    # probability pinned to 1.0, no board / cooldown reduction, so occupancy IS
    # the uptime.
    assert _occ("finite") == _finite_window_uptime(_DURATION, _INTERVAL)


def test_steady_is_the_dead_zone_free_ratio():
    assert _occ("steady") == min(1.0, _DURATION / _INTERVAL)


def test_steady_exceeds_finite_by_the_dead_zone():
    finite = _occ("finite")
    steady = _occ("steady")
    # The deck screen (no dead zone) reads higher than the in-run count, which is
    # the whole reason "finite" undershoots the panel. Pinned as `>` AND `!=` so
    # neither a revert nor an accidental equality passes.
    assert steady > finite
    assert steady != finite
    # And the steady value is exactly the T->infinity limit finite discounts.
    assert steady == min(1.0, _DURATION / _INTERVAL)


def test_an_unknown_model_falls_back_to_finite_not_a_crash():
    cfg = make_scoring_config(probability_tiers={"t": 1.0})
    cfg = dataclasses.replace(cfg, active_uptime_model="not-a-real-model")
    skill = make_skill(
        "ACTIVE", duration=_DURATION, interval=_INTERVAL, probability_tier="t"
    )
    resolved = make_resolved("card-x", "ACTIVE", skill, member="Holomem X")
    # Only the exact string "steady" switches behaviour; anything else is the
    # safe in-run default, never an exception.
    assert _active_occupancy(resolved, cfg, 0.0) == _finite_window_uptime(
        _DURATION, _INTERVAL
    )
