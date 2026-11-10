"""Marathon event-farming valuation: the parts read from the game.

Covers the two primitives taken from the game's data: the wildcard bonus-row matcher
(rows are patterns, unset fields wildcards, all matching rows SUM) and the live stamina table.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from support.event_bonus import event_bonus_permil

from hbuilder.domain.card import Card, Stats
from hbuilder.domain.marathon import (
    MarathonBonusRow,
    MarathonEvent,
    live_stamina_score,
)

STATS = Stats(performance=5000, technique=5000, sense=5000)


def card(card_id: str, *, rarity: str = "5★", member: str = "X") -> Card:
    return Card(
        id=card_id,
        member=member,
        generations=frozenset({"0期生"}),
        type="Cute",
        rarity=rarity,
        costume_name="c",
        stats=STATS,
        skills=(None, None, None),
        costume_skill=None,
    )


def event(**channels) -> MarathonEvent:
    return MarathonEvent(
        name="t",
        rows_by_channel={k: tuple(v) for k, v in channels.items()},
    )


# --- The wildcard matcher --------------------------------------------------


def test_holomem_row_fires_on_any_card_of_that_character():
    ev = event(live=[MarathonBonusRow(permil=300, character_id="chr-00012")])
    hit = card("card-00012-3-nrml-0000-00")
    miss = card("card-00021-5-uniq-0001-00")
    r = event_bonus_permil([hit, miss], ev)
    assert r.per_card_permil == (300, 0)
    assert r.total_permil == 300


def test_card_id_row_fires_only_on_that_card():
    ev = event(live=[MarathonBonusRow(permil=300, card_id="card-00012-5-uniq-0062-00")])
    r = event_bonus_permil([card("card-00012-5-uniq-0062-00"), card("card-00012-3-nrml-0000-00")], ev)
    assert r.per_card_permil == (300, 0)


def test_matching_rows_sum_holomem_plus_member_plus_bloom():
    """Worked example: a 5★ event card at bloom 5 draws holomem+Member+bloom = 900."""
    ev = event(
        live=[
            MarathonBonusRow(permil=300, character_id="chr-00012"),
            MarathonBonusRow(permil=300, card_id="card-00012-5-uniq-0062-00"),
            MarathonBonusRow(permil=300, rarity=5, bloom=5),
        ]
    )
    c = card("card-00012-5-uniq-0062-00", rarity="5★")
    r = event_bonus_permil([c], ev, blooms={c.id: 5})
    assert r.total_permil == 900
    assert r.bloom_rows_skipped == 0


def test_bloom_row_needs_owned_bloom_and_matches_rarity_and_bloom():
    ev = event(live=[MarathonBonusRow(permil=10, rarity=3, bloom=1)])
    c3 = card("card-00001-3-nrml-0000-00", rarity="3★")
    c5 = card("card-00002-5-uniq-0000-00", rarity="5★")
    r = event_bonus_permil([c3, c5], ev, blooms={c3.id: 1, c5.id: 5})
    assert r.per_card_permil == (10, 0)  # rarity 3 at bloom 1 fires; 5★ does not


def test_bloom_row_unmatchable_without_bloom_is_reported_not_silently_zero():
    ev = event(live=[MarathonBonusRow(permil=10, rarity=3, bloom=1)])
    c = card("card-00001-3-nrml-0000-00", rarity="3★")
    r = event_bonus_permil([c], ev)  # no blooms supplied
    assert r.total_permil == 0
    assert r.bloom_rows_skipped == 1


def test_music_row_is_song_gated():
    row = MarathonBonusRow(permil=100, card_id="card-06002-5-uniq-0066-00", music_id="m0524")
    ev = event(music=[row])
    c = card("card-06002-5-uniq-0066-00")
    assert event_bonus_permil([c], ev, channel="live", music_id="m0524").total_permil == 100
    assert event_bonus_permil([c], ev, channel="live", music_id="m0001").total_permil == 0
    # No song in scope -> the music row cannot apply.
    assert event_bonus_permil([c], ev, channel="live").total_permil == 0


def test_channel_selects_which_rows_apply():
    ev = event(
        live=[MarathonBonusRow(permil=300, character_id="chr-00012")],
        minigame=[MarathonBonusRow(permil=200, card_id="card-00012-5-uniq-0062-00")],
    )
    c = card("card-00012-5-uniq-0062-00")
    assert event_bonus_permil([c], ev, channel="live").total_permil == 300
    assert event_bonus_permil([c], ev, channel="minigame").total_permil == 200


def test_unknown_channel_raises():
    with pytest.raises(ValueError):
        event_bonus_permil([card("card-00001-3-nrml-0000-00")], event(), channel="bogus")


# --- Stamina tables --------------------------------------------------------


def test_live_stamina_curve_is_linear_through_three_then_sublinear():
    assert [live_stamina_score(s) for s in (1, 2, 3)] == [8, 16, 24]
    assert live_stamina_score(10) == 56
    # per-stamina rate is maximised at or below the recommend-consumption knee (3)
    assert live_stamina_score(3) / 3 > live_stamina_score(10) / 10


def test_stamina_out_of_range_raises():
    with pytest.raises(ValueError):
        live_stamina_score(11)


def _write(tmp_path: Path, obj) -> Path:
    p = tmp_path / "marathon.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def test_the_multi_room_divisor_follows_the_event_kind():
    # Three exact multi Lives: relay events /75000 (m0131 on the wire, Shallow
    # Shadow), the Point Rally (normal) event /60000 -- PI PIKASO Highlight on normal-008:
    # own 1,237,703, room 8,283,504, E +138 %, 3 boosts, HoloPass, x1.5 Highlight, no length
    # bonus -> 58,392 Event Pt on the result screen
    from hbuilder.domain import marathon as mar

    assert mar.multi_divisor("marathon_event-normal-008-chapter-01") == 60_000
    assert mar.multi_divisor("marathon_event-relay-003-chapter-02") == 75_000
    assert mar.multi_divisor(None) == 75_000
    base = mar.event_points_base(1_237_703, 1380, 8_283_504, divisor=60_000)
    assert base == 811
    assert mar.event_points(None, None, boosts=3, length_permil=1500, holopass=True, base=base) == 58_392
    # the relay vectors keep their exact values
    assert (
        mar.event_points(1_469_887, 1110, boosts=3, length_permil=1310, holopass=True, multi_score=5_143_567)
        == 38_043
    )
