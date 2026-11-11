"""Two SPECIAL riders live on one ACTIVE check: the game rolls it with the LARGER rider
(`CalcActivationProbabilityPermil` takes the SPECIALs' maximum), never with both combined."""

from __future__ import annotations

from hbuilder.domain.song import (
    _dominated_riders,
    _riders_share_a_check,
    _strongest_riders,
    _Term,
)

BASE = 0.46  # a Medium ACTIVE, no board rate


def _boosted(rider: float) -> float:
    # the game's roll: tier x (1 + rate), capped at 1 (`MultiplicativeRateUp`)
    return min(1.0, BASE * (1.0 + rider))


def _active() -> _Term:
    return _Term("active", 0.6, BASE, False, ((10.0, 18.0),), None, 0.0, active_base=BASE)


def _rider(host: int, rider: float, cover) -> _Term:
    q = (_boosted(rider) - BASE) / (1.0 - BASE)
    return _Term("active", 0.6, q, False, None, host, 0.0, slot_cover=cover, rider_base=0)


def _chance(live: list[_Term]) -> float:
    miss = 1.0
    for t in live:
        miss *= 1.0 - t.occupancy
    return 1.0 - miss


def test_two_live_riders_roll_with_the_larger_one():
    window = (10.0, 18.0)
    live = [_active(), _rider(1, 0.35, (window, None)), _rider(2, 0.45, (None, window))]
    kept = _strongest_riders(live)
    assert [t.special_member for t in kept] == [None, 2]
    # an example: 46 % with riders of 350 and 450 rolls at 0.46 x 1.45 = 66.7 %
    assert abs(_chance(kept) - 0.667) < 1e-9
    assert abs(_chance(live) - 0.7663) < 1e-3  # what combining both would give


def test_one_rider_is_kept_as_it_is():
    live = [_active(), _rider(1, 0.35, ((10.0, 18.0), None))]
    assert _strongest_riders(live) == live
    assert abs(_chance(live) - _boosted(0.35)) < 1e-12


def test_in_a_fixed_order_the_weaker_rider_on_the_same_window_counts_nothing():
    window = (10.0, 18.0)
    terms = [_active(), _rider(1, 0.35, (window, None)), _rider(2, 0.45, (None, window))]
    # member 1 in slot 0 and member 2 in slot 1: both cover the window
    assert _dominated_riders(terms, {1: 0, 2: 1}) == {1}
    # swapped: neither covers it, nothing is dominated
    assert _dominated_riders(terms, {1: 1, 2: 0}) == set()


def test_the_compiled_walk_is_skipped_only_when_two_riders_can_share_a_check():
    window = (10.0, 18.0)
    sharing = [_active(), _rider(1, 0.35, (window, None)), _rider(2, 0.45, (None, window))]
    apart = [_active(), _rider(1, 0.35, (window, None)), _rider(2, 0.45, (None, (30.0, 38.0)))]
    assert _riders_share_a_check(sharing)
    assert not _riders_share_a_check(apart)
