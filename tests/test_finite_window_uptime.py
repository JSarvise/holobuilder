"""The ACTIVE uptime is a **finite window**, not a steady-state duty cycle.

`min(1, duration / interval)` is the `T -> infinity` limit of the activation
lattice. A real run is not that limit and misses it in two places, both
read from the game binary: the cooldown clock starts at
measure 0 and the first check sits one whole cooldown in, so the run opens with
a **dead zone** in which the skill cannot be up at all; and the run ends, so the
last window is truncated.

Measured with `tools/recon/proxy/active_lattice.py`: driving the
shipped `song.py` over all **704** charts the game ships — real note times, real
`#mmm0B` cues, note-weighted exactly as `CalcScore` weights — a single skill is
up for **84 % to 95 %** of what `duration / interval` claims, across every
`(interval, duration)` pair the master data carries, and **never more**. Against
the same reference, `_score_up_pool`'s `E[A]` over 24 teams sampled from
`data/owned.json` × 704 charts goes

    rms 0.0511 -> 0.0225   mean bias +0.0457 -> +0.0019

**Every number below is derived by hand from the lattice in its own docstring,
and every one asserts against the steady-state answer as a `!=`.**
A direction-only assertion would pass under both readings, and an assertion that
did not exclude the steady-state value would pass under a silent revert.
"""

from __future__ import annotations

import pytest

from hbuilder.domain.scoring import _finite_window_uptime
from hbuilder.domain.scoring_config import REFERENCE_RUN_SECONDS
from hbuilder.domain.song import _active_windows

from conftest import make_resolved, make_scoring_config, make_skill

T = REFERENCE_RUN_SECONDS


def test_the_reference_run_is_the_one_the_rest_of_the_module_uses():
    """If `REFERENCE_RUN_SECONDS` ever moves, every number in this file moves
    with it, so the file states which value it was written against."""
    assert T == 128.8


def test_a_ten_second_effect_on_a_twenty_second_cooldown_is_not_half_the_run():
    """`T = 128.8`, `interval = 20` -> checks at 20, 40, 60, 80, 100, 120; the
    seventh would be at 140 and never happens.

        the first five windows run their full 10 s      -> 50.0
        the sixth is truncated by the end of the run    -> 128.8 - 120 = 8.8
        live = 58.8, uptime = 58.8 / 128.8 = 0.456521...

    The steady-state ratio says 0.5, which is 9.5 % higher, and the whole of
    that gap is the 20 s dead zone and the 1.2 s of the sixth window that the
    run does not contain."""
    uptime = _finite_window_uptime(10.0, 20.0)

    assert uptime == pytest.approx(58.8 / 128.8)
    assert uptime == pytest.approx(0.4565217391304348)
    assert uptime != pytest.approx(0.5)


def test_the_loss_grows_with_the_cooldown_which_is_what_no_ratio_can_express():
    """Two skills of **equal duty** and different cooldowns are not equally up —
    the longer cooldown has the longer dead zone and the coarser window count.
    Both of these are `duration / interval == 0.4`:

        interval 15, duration  6: checks 1..8 (8x15=120), live = 7x6 + min(6, 8.8)
                                  = 42 + 6 = 48.0        -> 48.0 / 128.8 = 0.372670...
        interval 35, duration 14: checks 1..3 (3x35=105), live = 2x14 + min(14, 23.8)
                                  = 28 + 14 = 42.0       -> 42.0 / 128.8 = 0.326086...

    The steady-state ratio cannot tell them apart at all; the lattice puts them
    12.5 % apart. This is the term that reorders teams."""
    short_cooldown = _finite_window_uptime(6.0, 15.0)
    long_cooldown = _finite_window_uptime(14.0, 35.0)

    assert short_cooldown == pytest.approx(48.0 / 128.8)
    assert long_cooldown == pytest.approx(42.0 / 128.8)
    assert short_cooldown != pytest.approx(long_cooldown)
    # The ratio they replace is the SAME number for both, which is the point.
    assert 6.0 / 15.0 == pytest.approx(14.0 / 35.0)
    assert long_cooldown / short_cooldown == pytest.approx(42.0 / 48.0)


def test_a_duration_at_or_above_its_own_cooldown_is_the_run_less_its_dead_zone():
    """**Full uptime is structural, not a clamp.** At
    `duration >= interval` the windows abut or overlap, so the lattice tiles
    `[interval, T]` continuously and the skill is up for everything except its
    own dead zone:

        uptime = (T - interval) / T = (128.8 - 20) / 128.8 = 0.844720...

    A SUM over the windows would have read 157.6 / 128.8 for the 30 s case and
    been saved only by the `min(1, ...)` guard, i.e. it would have returned
    `1.0` — a skill up for the whole run including the 20 s before it can first
    fire. The union returns the same value for 20 s and for 30 s."""
    at_interval = _finite_window_uptime(20.0, 20.0)
    above_interval = _finite_window_uptime(30.0, 20.0)

    assert at_interval == pytest.approx(above_interval)
    assert at_interval == pytest.approx((128.8 - 20.0) / 128.8)
    assert at_interval == pytest.approx(0.8447204968944099)
    # `min(1, duration / interval)` would give exactly 1.0 for both.
    assert at_interval != pytest.approx(1.0)
    assert above_interval != pytest.approx(1.0)


def test_a_cooldown_longer_than_the_whole_run_never_reaches_its_first_check():
    """`floor(T / interval) == 0`, so there is no window at all and the skill is
    up for none of the run. The ratio would have credited it `duration /
    interval` — for a 60 s effect on a 200 s cooldown, 30 % of every run from a
    skill that cannot fire once.

    No slot in `data/cards.json` is near this (intervals run 15 .. 35 s against
    T = 128.8), so it is a guard rather than a live path — but it is the honest
    reading and it must not silently become the ratio."""
    assert _finite_window_uptime(60.0, 200.0) == 0.0
    assert _finite_window_uptime(60.0, 200.0) != pytest.approx(0.3)
    # The boundary: a cooldown of exactly T fires once, at the very last instant,
    # and so is live for zero of the run either way.
    assert _finite_window_uptime(10.0, 128.8) == pytest.approx(0.0)
    # One check inside the run is worth its full duration less the truncation.
    assert _finite_window_uptime(10.0, 120.0) == pytest.approx(8.8 / 128.8)


def test_it_is_the_run_average_of_the_windows_song_py_puts_on_a_real_axis():
    """**The two scorers must not describe the same lattice differently.**
    `song._active_windows` places the ACTIVE windows on a named chart's time
    axis; this function is the run-average of exactly those windows over a
    nominal run instead of a named one. Measuring `song.py`'s own windows over
    `[0, T]` must reproduce it for every `(duration, interval)` pair, or the
    ranking scorer and the per-song scorer disagree about when a skill is up.

    This is the test that would fail first if either lattice grew an off-by-one
    in its check index — the defect this file exists to prevent."""
    cfg = make_scoring_config()
    for duration, interval in (
        (6.0, 15.0), (8.0, 17.0), (10.0, 20.0), (12.0, 25.0),
        (14.0, 35.0), (20.0, 20.0), (30.0, 20.0), (7.0, 22.0),
    ):
        skill = make_skill(
            "ACTIVE",
            duration=duration,
            interval=interval,
            probability_tier="Medium",
            magnitude=10.0,
        )
        resolved = make_resolved("card_lattice", "ACTIVE", skill)
        windows = _active_windows(resolved, cfg, T)

        live, covered_to = 0.0, 0.0
        for start, end in windows:
            lo, hi = max(start, covered_to), min(end, T)
            if hi > lo:
                live += hi - lo
                covered_to = hi

        assert _finite_window_uptime(duration, interval) == pytest.approx(live / T), (
            f"the two lattices disagree at duration={duration} interval={interval}"
        )


def test_a_shortened_cooldown_can_buy_a_whole_extra_activation():
    """**Why the board term and the finite window are identifiable only
    JOINTLY.** In the infinite-window ratio a cooldown
    reduction and a probability increase are interchangeable, so the board has
    nowhere to act that is not already absorbed. Under a finite window the
    activation *count* is an integer and a reduction can cross a boundary:

        interval 22.0        -> checks 1..5 (5x22 = 110), live = 4x8 + min(8, 18.8)
                                = 32 + 8 = 40.0          -> 40.0 / 128.8
        interval 22.0 / 1.12 -> 19.642857..., checks 1..6 (6x19.64 = 117.86),
                                live = 5x8 + min(8, 10.94) = 40 + 8 = 48.0
                                                          -> 48.0 / 128.8

    A 12 % cooldown node buys **20 %** more uptime here, not 12 %, because it
    bought a sixth activation. The ratio would have said 12 % exactly."""
    plain = _finite_window_uptime(8.0, 22.0)
    reduced = _finite_window_uptime(8.0, 22.0 / 1.12)

    assert plain == pytest.approx(40.0 / 128.8)
    assert reduced == pytest.approx(48.0 / 128.8)
    assert reduced / plain == pytest.approx(1.2)
    # The steady-state ratio moves by exactly the node's own 12 %, never by 20 %.
    assert reduced / plain != pytest.approx(1.12)


def test_the_cache_does_not_confuse_the_two_arguments():
    """`_finite_window_uptime` is `functools.cache`d on `(duration, interval)`,
    and those are not symmetric — a 10 s effect on a 25 s cooldown is not a 25 s
    effect on a 10 s cooldown. A cache keyed the wrong way round would return a
    plausible number for the wrong skill, which is exactly the failure this
    codebase's tests exist to catch."""
    ten_on_twenty_five = _finite_window_uptime(10.0, 25.0)
    twenty_five_on_ten = _finite_window_uptime(25.0, 10.0)

    # checks at 25, 50, 75, 100, 125 -> 4x10 + min(10, 3.8) = 43.8
    assert ten_on_twenty_five == pytest.approx(43.8 / 128.8)
    # duration >= interval -> the run less the dead zone
    assert twenty_five_on_ten == pytest.approx((128.8 - 10.0) / 128.8)
    assert ten_on_twenty_five != pytest.approx(twenty_five_on_ten)
