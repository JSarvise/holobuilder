"""The Holomem Board's `Act.CD` nodes shorten an ACTIVE's cooldown, and they
DIVIDE.

    cooldown_effective = cooldown / (1 + reduction)

Three facts this rests on, none of them assumed here:

- Only the board supplies it: no card changes an Active's cooldown. So the
  modifier is keyed by HOLOMEM, not by card, and reaches every card of that
  holomem.
- Branch B of the tree carries exactly three `Act.CD` nodes at 4.0 % each, so
  0.12 is the ceiling (`tools/recon/proxy/board_active_mods.py`).
- It divides rather than multiplies: a 35 s Active lands on a 31.25 s grid at
  +12 %.

An absent entry means "no board data for this holomem" and yields zero, which
is also the correct baseline for the maxed-account mode.
"""

from __future__ import annotations

import dataclasses

import pytest

from hbuilder.domain.scoring import score_card_set, score_fast

from conftest import make_card, make_scoring_config, make_skill, make_team

BASE_STAT_SUM = 1500.0


def _cfg(reduction_by_member: dict | None = None):
    cfg = make_scoring_config()
    return dataclasses.replace(cfg, active_cooldown_reduction=reduction_by_member or {})


def _team(member_name: str = "Nene"):
    scorer = make_card(
        "member_active",
        member=member_name,
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            effect_type="score",
            duration=8.0,
            interval=20.0,
            probability_tier="Medium",
            magnitude=20.0,
        ),
    )
    return make_team(
        [
            make_card("outfit"),
            scorer,
            make_card("m2"),
            make_card("m3"),
            make_card("m4"),
            make_card("m5"),
        ]
    )


def test_no_board_entry_scores_exactly_as_before():
    """The baseline the other tests move against. The ACTIVE uptime is the
    finite activation lattice over the 128.8 s reference run
    (`scoring._finite_window_uptime`), not the steady-state ratio.

        checks at 20, 40, 60, 80, 100, 120 — six of them, because 7 × 20 = 140
        is past the end of the run, and there is no check at t = 0
        every window whole: the last runs 120 → 128.0, still inside the run
        uptime 48/128.8 = 0.372670... · Medium 0.46 · 20 % = 0.034285714...
        → 1500 × 1.034285714... = 1551.428571..."""
    total = score_fast(_team(), _cfg())
    assert total == pytest.approx(1551.428571, abs=1e-3)
    # 1555.2 is the steady-state answer (uptime 8/20 = 0.40).
    assert total != pytest.approx(1555.2, abs=1e-3)


def test_a_twelve_percent_node_divides_the_cooldown_it_does_not_multiply():
    """The whole point of the finding, as a number — and under the finite
    window the number is not a smooth 12 % of anything.

        divides    : 20 / 1.12 = 17.857142... s
                     checks at 17.86, 35.71, ..., 125.0 — SEVEN of them, where
                     the unreduced lattice had six: 8 × 17.857142... = 142.86
                     is past the run, 7 × 17.857142... = 125.0 is not
                     six whole 8 s windows + (128.8 − 125.0) = 48 + 3.8 = 51.8
                     uptime 51.8/128.8 = 0.402173... → 0.037 → 1555.5
        multiplies : 20 × 0.88 = 17.6 s
                     checks at 17.6, ..., 123.2 — also seven, but the seventh
                     is truncated to 5.6 s rather than 3.8: 48 + 5.6 = 53.6
                     uptime 53.6/128.8 = 0.416149... → 0.038285714...
                     → 1557.428571...

    The two are close, which is exactly why getting it wrong is easy and why
    this test pins the number rather than the direction."""
    baseline = score_fast(_team(), _cfg())
    total = score_fast(_team(), _cfg({"Nene": 0.12}))
    assert total == pytest.approx(1555.5, abs=1e-3)
    assert total != pytest.approx(1557.428571, abs=1e-3)
    # 1561.824 is the steady-state answer and 1562.727 its
    # multiplying twin — both computed from `min(1, duration / interval)`.
    assert total != pytest.approx(1561.824, abs=1e-3)
    assert total != pytest.approx(1562.727, abs=1e-3)

    # The direction survives the finite window; the SIZE of the gain does not.
    # Under the
    # ratio a 12 % shorter cooldown was worth exactly 12 % more uptime, every
    # time, for every skill. Under the lattice the reduction buys ACTIVATIONS —
    # an integer — so here it buys a seventh check, and that seventh window is
    # only 3.8 s of its 8 before the run ends. Nothing but the uptime moves, so
    # the whole contribution scales as the union does: 51.8/48 = 1.079166...,
    # i.e. +7.92 %, not +12 %.
    gain = (total - BASE_STAT_SUM) / (baseline - BASE_STAT_SUM)
    assert gain == pytest.approx(51.8 / 48.0, abs=1e-9)
    assert gain != pytest.approx(1.12, abs=1e-3)


def test_the_reduction_is_keyed_by_holomem_not_by_card_id():
    """A board belongs to the character. Keying by card id would silently miss
    every other skin of the same holomem."""
    by_member = score_fast(_team("Nene"), _cfg({"Nene": 0.12}))
    by_card_id = score_fast(_team("Nene"), _cfg({"member_active": 0.12}))

    assert by_member == pytest.approx(1555.5, abs=1e-3)
    assert by_card_id == pytest.approx(1551.428571, abs=1e-3)
    # The steady-state pair, kept so a revert is a failure.
    assert by_member != pytest.approx(1561.824, abs=1e-3)
    assert by_card_id != pytest.approx(1555.2, abs=1e-3)


def test_a_reduction_on_a_holomem_not_in_the_team_changes_nothing():
    absent = score_fast(_team("Nene"), _cfg({"Calliope": 0.12}))

    assert absent == pytest.approx(1551.428571, abs=1e-3)
    # 1555.2 is the steady-state baseline.
    assert absent != pytest.approx(1555.2, abs=1e-3)


def test_a_duration_at_least_its_cooldown_is_live_for_all_but_its_dead_zone():
    """Under the finite window the `min(1.0, ...)` clamp does not bind at all —
    a union of windows inside `[0, T]` cannot measure more than `T` — and what
    governs instead is the DEAD ZONE, the one whole cooldown before the first
    activation check. With `duration >= interval` the windows abut from that
    first check onward and tile the rest of the run, so

        uptime = (T − interval) / T,   T = REFERENCE_RUN_SECONDS = 128.8

    no board entry : (128.8 − 20) / 128.8 = 108.8/128.8 = 0.844720...
                     · Medium 0.46 · 20 % = 0.077714285...
                     → 1500 × 1.077714285... = 1616.571428...
    12 % node      : interval 20 / 1.12 = 17.857142..., and the dead zone is
                     exactly that interval, so it shrinks with it
                     (128.8 − 17.857142...) / 128.8 = 0.861357...
                     · 0.46 · 20 % = 0.079244897...
                     → 1500 × 1.079244897... = 1618.867346...

    So the cooldown axis bites here, which under the clamp it could not: a
    shorter cooldown is a shorter dead zone. Uptime APPROACHES 1 as the cooldown
    approaches zero and reaches it for no cooldown any card carries — `1.0` is
    the `T → ∞` limit, not this fixture's answer."""
    scorer = make_card(
        "member_active",
        member="Nene",
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            effect_type="score",
            duration=20.0,
            interval=20.0,
            probability_tier="Medium",
            magnitude=20.0,
        ),
    )
    team = make_team(
        [make_card("outfit"), scorer] + [make_card(f"m{i}") for i in range(2, 6)]
    )
    without_node = score_fast(team, _cfg())
    with_node = score_fast(team, _cfg({"Nene": 0.12}))

    assert without_node == pytest.approx(1616.571428, abs=1e-3)
    assert with_node == pytest.approx(1618.867346, abs=1e-3)
    # 1638.0 is the steady-state answer for BOTH — uptime clamped to 1.0,
    # which flattens the cooldown axis entirely.
    assert without_node != pytest.approx(1638.0, abs=1e-3)
    assert with_node != pytest.approx(1638.0, abs=1e-3)


def test_batch_and_single_paths_stay_bit_identical_with_a_board_reduction():
    """Driver parity holds with a board cooldown — `==`, never `approx`."""
    cfg = _cfg({"Nene": 0.08})
    outfit = make_card("outfit")
    scorer = make_card(
        "member_active",
        member="Nene",
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            effect_type="score",
            duration=8.0,
            interval=20.0,
            probability_tier="Medium",
            magnitude=20.0,
        ),
    )
    members = (scorer, make_card("m2"), make_card("m3"), make_card("m4"), make_card("m5"))

    batch = score_card_set(members, (outfit,), cfg)
    single = score_fast(make_team([outfit, *members]), cfg)

    assert batch[0].total_score == single
