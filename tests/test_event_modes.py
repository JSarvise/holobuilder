"""Event objectives: Event Points, Event Song Score, Holomem Score Rating.

Event Pt base and both ceilings match real solo and multi Live/Finish results
exactly. The event-bonus matcher rules follow the client
(`CalculateBonusRatioPermil` @0x614b5e0): wildcard rows, bloom compared with ==,
the leader excluded, Event Pt rows SUMMED across cards, Event Song Score rows
MAXED across cards (`Enumerable.Max` @0x658f441).
"""
from __future__ import annotations

import pytest

from hbuilder.domain.card import Card, Stats
from hbuilder.domain.marathon import (
    MarathonBonusRow,
    MarathonEvent,
    event_point_bonus_permil,
    event_points,
    event_points_base,
    event_song_bonus_permil,
    event_song_score,
    holomem_rating_total,
    holomem_song_rating,
)

STATS = Stats(performance=5000, technique=5000, sense=5000)


def card(card_id: str, *, rarity: str = "5★", type_: str = "Cute") -> Card:
    return Card(id=card_id, member="X", generations=frozenset({"0期生"}), type=type_,
                rarity=rarity, costume_name="c", stats=STATS, skills=(None, None, None),
                costume_skill=None)


def event(**channels) -> MarathonEvent:
    return MarathonEvent(name="t", rows_by_channel={k: tuple(v) for k, v in channels.items()})


# --- Event Pt arithmetic ------------------------------------------------------------

@pytest.mark.parametrize("score,bonus,base", [(544_758, 830, 287), (554_165, 830, 288),
                                               (844_515, 150, 203)])
def test_event_point_base_is_the_ceiling_of_120_plus_score_over_15000(score, bonus, base):
    assert event_points_base(score, bonus) == base


@pytest.mark.parametrize("score,bonus,points", [(544_758, 830, 12_399), (554_165, 830, 12_442),
                                                 (844_515, 150, 8_770)])
def test_event_points_reproduce_the_owners_three_solo_runs(score, bonus, points):
    # m0323 EXPERT, 2 boosts (x16), HoloPass on (x2), length multiplier 1.35
    assert event_points(score, bonus, boosts=2, length_permil=1350, holopass=True) == points


def test_event_points_reproduce_the_multi_live_on_the_wire():
    # base 490 from the server; 2 boosts, length multiply 1480, HoloPass
    assert event_points(None, None, boosts=2, length_permil=1480, holopass=True,
                        base=490) == 23_207


@pytest.mark.parametrize("score, multi, bonus, boosts, length, points", [
    (1_208_510, 7_078_605, 660, 2, 1480, 23_207),    # m0131 EXPERT, the wire capture
    (1_469_887, 5_143_567, 1110, 3, 1310, 38_043),   # Shallow Shadow EXPERT, result screens
])
def test_multi_event_points_add_the_room_score_over_75000(score, multi, bonus, boosts, length,
                                                          points):
    """Checked on two multi Lives: `120 + score/15000 + multi/75000`,
    multi = the room's MULTI SCORE with your own score in it. A `/60000`
    divisor gives 40,307 on the second (the game: 38,043)."""
    assert event_points(score, bonus, boosts=boosts, length_permil=length, holopass=True,
                        multi_score=multi) == points
    assert event_points_base(score, bonus, 0) == event_points_base(score, bonus)   # solo


def test_event_point_base_uses_exact_integers_not_floats():
    # (120 + 1.5M/15000) * 1.3 is 286.00000000000006 in IEEE double; the game's ceil is 286
    assert event_points_base(1_500_000, 300) == 286


def test_boosts_outside_the_table_are_refused():
    with pytest.raises(ValueError):
        event_points(1_000_000, 0, boosts=11, length_permil=1000, holopass=False)


# --- Event Pt bonus E: rows summed per card, leader excluded ------------------------

def test_a_bloom5_bonus_holomem_event_card_draws_all_three_rows():
    c = card("card-00012-5-uniq-0001-00")
    ev = event(live=[MarathonBonusRow(permil=300, character_id="chr-00012"),
                     MarathonBonusRow(permil=300, card_id=c.id),
                     MarathonBonusRow(permil=300, rarity=5, bloom=5)])
    assert event_point_bonus_permil([c], ev, blooms={c.id: 5}).total_permil == 900


def test_two_cards_of_a_bonus_holomem_each_draw_the_holomem_row():
    a, b = card("card-00012-5-uniq-0001-00"), card("card-00012-4-cmmn-0000-00", rarity="4★")
    ev = event(live=[MarathonBonusRow(permil=300, character_id="chr-00012")])
    assert event_point_bonus_permil([a, b], ev, blooms={}).total_permil == 600


def test_a_bloom_row_fires_only_at_exactly_that_bloom():
    c = card("card-00020-5-uniq-0003-00")
    ev = event(live=[MarathonBonusRow(permil=300, rarity=5, bloom=5)])
    assert event_point_bonus_permil([c], ev, blooms={c.id: 4}).total_permil == 0
    assert event_point_bonus_permil([c], ev, blooms={c.id: 0}).total_permil == 0


def test_an_attribute_row_matches_only_that_attribute():
    cute, pure = card("card-00001-5-uniq-0001-00"), card("card-00002-5-uniq-0002-00", type_="Pure")
    ev = event(live=[MarathonBonusRow(permil=200, attribute="Pure")])
    assert event_point_bonus_permil([cute, pure], ev, blooms={}).per_card_permil == (0, 200)


def test_event_point_bonus_ignores_the_song_rows():
    c = card("card-00012-5-uniq-0001-00")
    ev = event(live=[MarathonBonusRow(permil=300, character_id="chr-00012")],
               music=[MarathonBonusRow(permil=500, card_id=c.id, music_id="m0001")])
    assert event_point_bonus_permil([c], ev, blooms={}).total_permil == 300


# --- Event Song Score: max over cards of the per-card sum ---------------------------

def test_event_song_bonus_is_the_largest_card_not_the_sum():
    a, b = card("card-00012-5-uniq-0001-00"), card("card-00013-5-uniq-0002-00")
    ev = event(music=[MarathonBonusRow(permil=100, card_id=a.id, music_id="m0001"),
                      MarathonBonusRow(permil=150, card_id=b.id, music_id="m0001")])
    assert event_song_bonus_permil([a, b], ev, music_id="m0001", blooms={}) == 150


def test_event_song_bonus_is_song_gated():
    a = card("card-00012-5-uniq-0001-00")
    ev = event(music=[MarathonBonusRow(permil=100, card_id=a.id, music_id="m0001")])
    assert event_song_bonus_permil([a], ev, music_id="m0002", blooms={}) == 0


def test_event_song_score_applies_the_bonus_to_the_whole_score():
    # rounding is a ceil, as community tool holodori.best does; the server's rounding is not known
    assert event_song_score(1_208_510, 100) == 1_329_361


# --- Holomem Score Rating ----------------------------------------------------------

def test_holomem_song_rating_divides_by_5000():
    assert holomem_song_rating(1_208_510) == 242   # ceil (server rounding not known)


def test_holomem_rating_sums_the_three_best_songs():
    assert holomem_rating_total([242, 230, 228, 200]) == 700


# --- The current event, from the refresh's marathon_bonus.json ----------------------

def test_the_newest_open_event_is_loaded_with_its_three_channels(tmp_path):
    import json

    from hbuilder.domain.marathon import load_current_event, open_events

    doc = {"current_group_ids": [
        "live_marathon_score_bonus-marathon_event-normal-006-chapter-01",
        "live_marathon_score_bonus-marathon_event-normal-007-chapter-01",
        "music_score_bonus-marathon_event-normal-007-chapter-01"],
        "groups": [
        {"group_id": "live_marathon_score_bonus-marathon_event-normal-006-chapter-01", "kind": "live",
         "rows": [{"character_id": "chr-00001", "permil": 300}]},
        {"group_id": "live_marathon_score_bonus-marathon_event-normal-007-chapter-01", "kind": "live",
         "rows": [{"character_id": "chr-00023", "permil": 300},
                  {"rarity": 5, "bloom": 5, "permil": 300}]},
        {"group_id": "music_score_bonus-marathon_event-normal-007-chapter-01", "kind": "music",
         "rows": [{"card_id": "card-00023-5-uniq-0085-00", "music_id": "m0354", "permil": 100}]}]}
    path = tmp_path / "marathon_bonus.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert open_events(path) == ["marathon_event-normal-006-chapter-01",
                                 "marathon_event-normal-007-chapter-01"]
    ev = load_current_event(path)
    assert ev.name == "marathon_event-normal-007-chapter-01"
    assert len(ev.rows_for("live")) == 2 and len(ev.rows_for("music")) == 1
    older = load_current_event(path, event="marathon_event-normal-006-chapter-01")
    assert older.rows_for("live")[0].character_id == "chr-00001"


# --- The search objective ---------------------------------------------------------

def test_event_points_objective_ranks_a_bonus_team_above_a_higher_score():
    from hbuilder.domain.marathon import event_objective
    plain = event_objective(1_500_000, objective="event_points", live_permil=0)
    bonus = event_objective(1_200_000, objective="event_points", live_permil=600)
    assert bonus > plain


def test_event_objective_is_continuous_and_agrees_with_the_reported_base():
    from hbuilder.domain.marathon import event_objective
    value = event_objective(544_758, objective="event_points", live_permil=830)
    assert value == pytest.approx((120 + 544_758 / 15000) * 1.83)
    assert event_points_base(544_758, 830) == 287 == -(-int(value * 1e9) // int(1e9))


def test_event_song_objective_and_score_objective():
    from hbuilder.domain.marathon import event_objective
    assert event_objective(1_000_000, objective="event_song", song_permil=100) == pytest.approx(1_100_000)
    assert event_objective(1_000_000, objective="score", live_permil=900) == 1_000_000


def test_unknown_objective_is_refused():
    from hbuilder.domain.marathon import event_objective
    with pytest.raises(ValueError):
        event_objective(1, objective="holomem")


def test_card_bonus_table_gives_live_and_song_permil_per_card():
    from hbuilder.domain.marathon import card_bonus_table
    a, b = card("card-00023-5-uniq-0085-00"), card("card-00001-5-uniq-0001-00")
    ev = event(live=[MarathonBonusRow(permil=300, character_id="chr-00023"),
                     MarathonBonusRow(permil=300, rarity=5, bloom=5)],
               music=[MarathonBonusRow(permil=100, card_id=a.id, music_id="m0354")])
    table = card_bonus_table([a, b], ev, blooms={a.id: 5, b.id: 3}, music_id="m0354")
    assert table == ((600, 100), (0, 0))
    assert card_bonus_table([a], ev, blooms={a.id: 5}, music_id=None) == ((600, 0),)
