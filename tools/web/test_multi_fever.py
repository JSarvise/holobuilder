"""Multi Live (FEVER) on the web side: a Multi row's split, and the keys a solo run keeps."""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))

import certificate  # noqa: E402
import materials_plan  # noqa: E402
import row_detail  # noqa: E402
from support.builders import CERTAIN_TIERS, make_card, make_scoring_config, make_skill  # noqa: E402

from hbuilder.domain.card import Stats  # noqa: E402
from hbuilder.domain.song import ChartNote, SongChart  # noqa: E402
from hbuilder.domain.song_rank import score_team_at_order, score_team_notes, score_team_on_chart  # noqa: E402
from hbuilder.domain.team import Team  # noqa: E402

# One note before anything is live, one at 55 s inside the fever section, where the fifth
# SPECIAL cue's window sits: the slot order decides how much the fever note scores.
CHART = SongChart(
    music_id="m0001",
    difficulty="EXPERT",
    difficulty_level=5,
    live_score_coefficient_permil=4,
    notes=(ChartNote(time=1.0, perfect_permil=1000), ChartNote(time=55.0, perfect_permil=1000)),
    special_cues=(10.0, 20.0, 30.0, 40.0, 50.0),
    fever=(50.0, 60.0),
)


def _team() -> Team:
    big, small = (
        Stats(performance=900, technique=900, sense=900),
        Stats(performance=10, technique=10, sense=10),
    )
    special = make_skill(
        "SPECIAL", family="conditional_score_up", effect_type="score", duration=10.0, magnitude=1000.0
    )
    members = (
        make_card("card-A", stats=big, special=special),
        make_card("card-B", stats=big),
        make_card("card-C", stats=small),
        make_card("card-D", stats=small),
        make_card("card-E", stats=small),
    )
    return Team(leader=make_card("card-L", stats=small), members=members)


def _cfg(weight: float = 0.0):
    return dataclasses.replace(
        make_scoring_config(probability_tiers=CERTAIN_TIERS),
        song_absolute_deck_power=False,
        fever_weight=weight,
    )


def test_the_score_at_the_best_order_is_the_rankers_score():
    team, cfg = _team(), _cfg()
    best = score_team_on_chart(team, CHART, cfg)
    assert score_team_at_order(team, CHART, cfg, best.member_order) == pytest.approx(best.total_score)


def test_a_multi_row_splits_into_its_own_score_and_the_fever_at_its_own_order():
    team = _team()
    multi = score_team_on_chart(team, CHART, _cfg(1.5))
    split = row_detail.multi_figures(team, CHART, _cfg(1.5), multi.member_order, multi.total_score)
    plain = score_team_at_order(team, CHART, _cfg(), multi.member_order)
    assert split["song_est_solo"] == plain
    # the fever part is the 55 s note's own expected value at that order (per-note ceilings: 1)
    best, notes = score_team_notes(team, CHART, _cfg())
    assert tuple(best.member_order) == tuple(multi.member_order)
    assert split["fever_score"] == pytest.approx(notes[1].expected_value, abs=1.0)
    assert split["multi_score"] == pytest.approx(multi.total_score)  # weight 1.5: the ranked value itself
    assert 0 < split["fever_score"] < plain


def test_a_solo_config_hashes_as_before_and_a_multi_one_differs():
    solo, multi = _cfg(), _cfg(1.5)
    assert "fever_weight" not in json.loads(certificate.cfg_json(solo))
    assert certificate.cfg_json(solo) != certificate.cfg_json(multi)


def test_a_plan_judged_for_a_multi_run_is_held_apart():
    from ondemand import Assumptions

    a = Assumptions()
    leaf = type("Leaf", (), {})()
    leaf.cfg = _cfg()
    solo = materials_plan.judged_opts({}, a, "EXPERT", leaf)
    leaf.cfg = _cfg(1.5)
    multi = materials_plan.judged_opts({}, a, "EXPERT", leaf)
    assert solo["judge"] != multi["judge"]
    assert "1.5" not in solo["judge"]


def test_event_song_score_in_multi_ranks_the_contribution_and_takes_a_highlight_pool():
    import event_mode

    body = {"objective": "event_song", "song": "highlight", "multi": True}
    opts = event_mode.parse_event_body(body)
    assert opts["multi"] is True and event_mode.contribution_mode(opts)
    # without Multi a Highlight pool still ranks by Event Points only
    with pytest.raises(event_mode.EventRequestError):
        event_mode.parse_event_body({**body, "multi": False})
    # Event Points in Multi is not a contribution
    assert not event_mode.contribution_mode(
        event_mode.parse_event_body({**body, "objective": "event_points"})
    )


@pytest.mark.parametrize("divisor", [75_000, 60_000])
def test_event_points_in_multi_rank_the_attested_base_on_the_weighted_score(divisor):
    from hbuilder.domain import marathon as mar

    s, f, others, e = 2_300_000, 540_000, 12_000_000, 350
    c = mar.multi_points_fever_weight(divisor)
    v = s + c * f
    value = mar.event_objective(
        v, objective="event_points", live_permil=e, multi_divisor=divisor, room_rest=others
    )
    k = divisor // 15_000
    exact = (120 * divisor + k * s + (s + 1.5 * f + others)) * (1000 + e) / (divisor * 1000)
    assert value == pytest.approx(exact, rel=1e-12)
    # the served base: `score = V` and the room `V + O` (only the roundings of V and S + 1.5 F differ)
    served = mar.event_points_base(round(v), e, round(v) + others, divisor)
    assert abs(served - mar.event_points_base(s, e, round(s + 1.5 * f) + others, divisor)) <= 1
    # solo is unchanged
    assert mar.event_objective(s, objective="event_points", live_permil=e) == pytest.approx(
        (120 + s / 15000) * (1 + e / 1000)
    )


def test_event_points_in_multi_take_the_room_and_a_highlight_pool():
    import event_mode

    opts = event_mode.parse_event_body({"objective": "event_points", "song": "highlight", "multi": True})
    assert event_mode.multi_points_mode(opts) and not event_mode.contribution_mode(opts)
    assert opts["room_others"] is None
    opts = event_mode.parse_event_body({"song": "m0342", "multi": True, "room_others": 9_000_000})
    assert opts["room_others"] == 9_000_000
    # a solo run carries no room
    assert event_mode.parse_event_body({"song": "m0342", "room_others": 9_000_000})["room_others"] is None
    with pytest.raises(event_mode.EventRequestError):
        event_mode.parse_event_body({"song": "m0342", "multi": True, "room_others": -1})


def test_a_detail_takes_the_searchs_fever_weight():
    from ondemand import parse_play

    assert parse_play({"multi": True, "fever_weight": 0.25}) == ("mobile", False, 0.25)
    assert parse_play({"multi": True, "fever_weight": 9}) == ("mobile", False, 1.5)
    assert parse_play({"fever_weight": 0.25}) == ("mobile", False, 0.0)
