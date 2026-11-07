"""The exact law of the run score (`hbuilder.domain.song_distribution`).

The one property that makes it trustworthy: its expectation path, built from
the same terms, reproduces `score_song_detailed`'s own number for the same
team, chart, deck power and slot order — so the law is OF the row's model, not
of a sibling. Plus the distribution's own invariants."""

from __future__ import annotations

import pytest
from support.builders import FIXTURE_CARDS_PATH, make_scoring_config

from hbuilder.domain.loader import load_cards
from hbuilder.domain.song import ChartNote, SongChart, overall_power_for, score_song_detailed
from hbuilder.domain.song_distribution import run_score_distribution


@pytest.fixture(scope="module")
def cards():
    return load_cards(FIXTURE_CARDS_PATH)


@pytest.fixture(scope="module")
def cfg():
    return make_scoring_config()


def _chart():
    return SongChart(
        music_id="m0001", difficulty="EXPERT", difficulty_level=26,
        live_score_coefficient_permil=4,
        notes=tuple(ChartNote(time=0.5 * t, perfect_permil=1000) for t in range(1, 240)),
        special_cues=(10.0, 30.0, 50.0, 70.0, 90.0),
    )


@pytest.mark.parametrize("leader_index", [0, 1, 2])
def test_the_expectation_is_the_scorers_own_number(cards, cfg, leader_index):
    leader = cards[leader_index]
    members = tuple(c for i, c in enumerate(cards) if i != leader_index)[:5]
    chart = _chart()
    power = overall_power_for(members, leader, cfg)
    best = score_song_detailed(members, (leader,), chart, cfg, deck_power=power)[0]
    law = run_score_distribution(members, leader, chart, cfg, best.member_order,
                                 best.deck_power)
    assert law["expected_total"] == best.total_score
    assert law["min"] <= law["p50"] <= law["max"]
    assert law["min"] <= law["mean"] <= law["max"]
    assert abs(sum(law["pmf"]["mass"]) + law["pmf"]["below"] - 1.0) < 1e-9
    # E[ceil] vs ceil(E): the mean differs from the expectation by rounding only
    assert abs(law["mean"] - best.total_score) <= 0.5 * len(chart.notes)
    assert law["exact"] is True
    assert "law" not in law          # the whole law only on request (`keep_law`)


def test_a_bad_order_is_refused(cards, cfg):
    with pytest.raises(ValueError):
        run_score_distribution(tuple(cards[1:6]), cards[0], _chart(), cfg, ["x"] * 5, 1000.0)


def test_the_ceiling_is_at_the_rows_own_slot_order(cards, cfg):
    """`score_team_ceiling` is "every ACTIVE fires" at the order the row plays,
    so it is the top of that row's law. Running `active_all_fire` through the
    scorer instead re-chooses the slot order for the all-fire case — here it
    swaps two members' SPECIAL cues (21,708 vs the row's 21,696) — which would
    put the web's Max above the end of the histogram."""
    import dataclasses

    from hbuilder.domain.song_rank import score_team_ceiling, score_team_on_chart
    from hbuilder.domain.team import Team

    chart = _chart()
    team = Team(leader=cards[0], members=tuple(cards[1:6]))
    row = score_team_on_chart(team, chart, cfg)
    free = score_team_on_chart(team, chart, dataclasses.replace(cfg, active_all_fire=True))
    assert free.member_order != row.member_order  # the case that would go wrong
    law = run_score_distribution(team.members, team.leader, chart, cfg, row.member_order,
                                 row.deck_power)
    assert score_team_ceiling(team, chart, cfg, row.member_order) == law["max"]
    # at the order the free ceiling picked, nothing changes
    assert score_team_ceiling(team, chart, cfg, free.member_order) == free.total_score


@pytest.mark.parametrize("cap", [12, 24])
def test_a_binned_law_keeps_the_exact_extremes_and_an_unbiased_body(cards, cfg, cap):
    """A long chart's law is binned once its support passes `cap`. Binning must not
    drift: the extremes stay exact (Max is the ceiling the row shows), the mean stays
    the exact law's (mass split between grid neighbours, not floored), and every
    percentile stays within one bin of the exact one -- a floor at each merge pulled
    them all down, more the more windows merged (15-29 points on a real chart). The
    mean moves only by the mass held at an extreme, well under half a bin."""
    leader, members = cards[0], tuple(cards[1:6])
    chart = _chart()
    power = overall_power_for(members, leader, cfg)
    best = score_song_detailed(members, (leader,), chart, cfg, deck_power=power)[0]
    exact = run_score_distribution(members, leader, chart, cfg, best.member_order, best.deck_power)
    binned = run_score_distribution(members, leader, chart, cfg, best.member_order, best.deck_power,
                                    cap=cap)
    assert exact["exact"] is True and binned["exact"] is False
    w = binned["bin_width"]
    assert binned["min"] == exact["min"] and binned["max"] == exact["max"]
    assert abs(binned["mean"] - exact["mean"]) <= 0.5 * w
    for q in (1, 5, 10, 50, 90, 95, 99):
        assert abs(binned[f"p{q}"] - exact[f"p{q}"]) <= w, q
    assert abs(sum(binned["pmf"]["mass"]) + binned["pmf"]["below"] - 1.0) < 1e-9


def test_best_of_n_on_a_small_law():
    """Best of n: `P(best <= x) = F(x) ** n`. On a fair coin between 0 and 10, the best
    of two is 10 three times in four (expected 7.5); a score of 10 is beaten by no run,
    reached by one run in two and by a best-of-two session three times in four."""
    from hbuilder.domain.song_distribution import best_of_n

    law = [(0, 0.5), (10, 0.5)]
    one, two = best_of_n(law, 1), best_of_n(law, 2)
    assert one["expected"] == 5.0 and two["expected"] == 7.5
    assert two["p25"] == 0 and two["p50"] == 10
    hit = best_of_n(law, 2, score=10)
    assert hit["run_percentile"] == 0.5 and hit["best_percentile"] == 0.25
    assert hit["chance_to_reach"] == 0.75
    with pytest.raises(ValueError):
        best_of_n(law, 0)


def test_best_of_n_on_a_runs_law(cards, cfg):
    """On a real law: best of one is the law's mean, more runs never expect less, and
    the best of many tends to the law's Max."""
    from hbuilder.domain.song_distribution import best_of_n

    leader, members = cards[0], tuple(cards[1:6])
    chart = _chart()
    power = overall_power_for(members, leader, cfg)
    best = score_song_detailed(members, (leader,), chart, cfg, deck_power=power)[0]
    law = run_score_distribution(members, leader, chart, cfg, best.member_order,
                                 best.deck_power, keep_law=True)
    values = law["law"]
    assert values == sorted(values) and abs(sum(p for _v, p in values) - 1.0) < 1e-9
    assert abs(best_of_n(values, 1)["expected"] - law["mean"]) < 1e-6 * law["mean"]
    expected = [best_of_n(values, n)["expected"] for n in (1, 2, 5, 26, 10_000)]
    assert expected == sorted(expected)
    assert expected[-1] <= law["max"] and law["max"] - expected[-1] < law["max"] - law["mean"]
    at_max = best_of_n(values, 26, score=law["max"])
    assert abs(at_max["run_percentile"] - (1.0 - law["p_max"])) < 1e-12
