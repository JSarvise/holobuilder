"""The timeline's Active chance is the one the scorer rolls.

Each Active window's `probability` must be read off the SAME terms `score_song_detailed`
scores: the ACTIVE term's own probability (the team's and the board's Activation Rate UP
folded in) and, inside a SPECIAL rider's window, the boosted roll. A drawing with the card's
tier alone said 55 % where the score used 77-78 %, and 100 % inside a rider's window.
"""

from __future__ import annotations

import dataclasses

from support.builders import make_card, make_scoring_config, make_skill

from hbuilder.domain.card import SlotRider
from hbuilder.domain.song import ChartNote, SongChart, score_song_detailed, timeline
from hbuilder.domain.song_distribution import _terms

HALF = {"Low": 0.3, "Medium": 0.5, "High": 0.7}


def _chart():
    return SongChart(
        music_id="m0001",
        difficulty="easy",
        difficulty_level=5,
        live_score_coefficient_permil=4,
        notes=tuple(ChartNote(time=0.25 * k, perfect_permil=1000) for k in range(1, 241)),
        special_cues=(10.0, 20.0, 30.0, 40.0, 50.0),
    )


def _team():
    host = make_card(
        "rider-host",
        member="Host",
        special=make_skill(
            "SPECIAL",
            family="score_support",
            effect_type="score",
            duration=10.0,
            magnitude=100.0,
            probability_tier=None,
            rider=SlotRider(
                family="activation_rate_up",
                effect_type="activation_rate",
                magnitude=50.0,
                condition=None,
                target=None,
            ),
        ),
    )
    scorers = tuple(
        make_card(
            f"scorer-{i}",
            member=f"Scorer{i}",
            active=make_skill(
                "ACTIVE",
                family="conditional_score_up",
                effect_type="score",
                duration=6.0,
                interval=9.0 + i,
                probability_tier="Medium",
                magnitude=40.0 + 5 * i,
            ),
        )
        for i in range(3)
    )
    members = (host, *scorers, make_card("plain", member="Plain"))
    return make_card("leader", member="Leader"), members


def test_each_window_carries_the_scorers_own_chance():
    leader, members = _team()
    chart = _chart()
    cfg = dataclasses.replace(make_scoring_config(), probability_tiers=HALF)
    order = score_song_detailed(members, (leader,), chart, cfg)[0].member_order
    tl = timeline(members, chart, cfg, tuple(order))

    terms = _terms(members, leader, chart, cfg)[0]
    index = {card.id: i for i, card in enumerate(members)}
    slot = {cid: s for s, cid in enumerate(order)}
    raised = 0
    for lane in tl["lanes"]:
        own = [t for t in terms if t.slot_cover is None and t.windows and t.owner == lane["card_id"]]
        for w in lane["active"]:
            window = (w["start"], w["end"])
            base = next(t for t in own if window in t.windows)
            miss = 1.0 - base.occupancy
            for r in terms:
                if r.slot_cover is None or r.owner != lane["card_id"]:
                    continue
                host = members[r.special_member]
                assert index[host.id] == r.special_member
                if r.slot_cover[slot[host.id]] == window:
                    miss *= 1.0 - r.occupancy
            assert w["probability"] == 1.0 - miss
            assert w["probability"] >= w["probability_base"]
            if w["rider_from"]:
                raised += 1
                assert w["rider_from"] == ["Host"] and w["probability"] > base.occupancy
    assert raised > 0, "the case must put a check inside the rider's window"
