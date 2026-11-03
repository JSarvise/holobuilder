"""Count-targeted PASSIVE Score Support reaches only its N targets.

On the game's deck panel, a member PASSIVE `score_support` with
`{kind:type, count:2}` gives +80 to the two highest-parameter Cute members and 0
to the third Cute; the per-card `active_effect_up` is the SUM of the supports
whose target selects that card (plus the board). So a support must not boost an
ACTIVE whose owner it does not target.

The model: each ACTIVE ratio term owned by member `i` is multiplied by
`(1 + eu + B_shared + sum of the count-targeted supports whose resolved target
set contains i)`, where `B_shared` is the unchanged max-per-bucket combine of
the `all`-target supports (member PASSIVE `all`, leader COSTUME, SPECIAL).

Every number here is derived by hand; parity between the three drivers is
pinned bit-identical."""
from __future__ import annotations

import dataclasses

from hbuilder.domain.card import EffectTarget, Stats
from hbuilder.domain.scoring import (
    score_card_set,
    score_fast,
    score_with_breakdown,
)
from hbuilder.domain.song import ChartNote, SongChart, score_song_detailed
from conftest import make_card, make_scoring_config, make_skill, make_team

CERTAIN_TIERS = {"Low": 1.0, "Medium": 1.0, "High": 1.0}


def cfg(**overrides):
    base = make_scoring_config(probability_tiers=CERTAIN_TIERS)
    return dataclasses.replace(base, **overrides) if overrides else base


def _active(magnitude: float):
    """An ACTIVE live across the chart's notes ([20, 50) window)."""
    return make_skill(
        "ACTIVE",
        family="conditional_score_up",
        effect_type="score",
        duration=30.0,
        interval=20.0,
        probability_tier="High",
        magnitude=magnitude,
    )


def _support(magnitude: float, target):
    return make_skill(
        "PASSIVE",
        family="score_support",
        effect_type="score",
        magnitude=magnitude,
        target=target,
    )


def _chart(times=(25.0, 30.0)) -> SongChart:
    return SongChart(
        music_id="m0001",
        difficulty="easy",
        difficulty_level=5,
        live_score_coefficient_permil=4,
        notes=tuple(ChartNote(time=t, perfect_permil=1000) for t in times),
        special_cues=(10.0, 20.0, 30.0, 40.0, 50.0),
    )


def _members(support_target, *, support_on="card-e"):
    """Five members: card-a..card-c Cute, card-d/card-e Cool. card-a carries
    the biggest ACTIVE (50 %), card-d a 30 % ACTIVE; `support_on` carries a
    magnitude-10 PASSIVE score_support with `support_target`. Stats are equal,
    so a count-2 Cute target resolves to the first two Cute in list order
    (card-a, card-b) by the tie-break."""
    cards = {
        "card-a": make_card("card-a", type_="Cute", active=_active(50.0)),
        "card-b": make_card("card-b", type_="Cute"),
        "card-c": make_card("card-c", type_="Cute"),
        "card-d": make_card("card-d", type_="Cool", active=_active(30.0)),
        "card-e": make_card("card-e", type_="Cool"),
    }
    cid = support_on
    base = cards[cid]
    cards[cid] = dataclasses.replace(
        base, skills=(base.skills[0], base.skills[1], _support(10.0, support_target))
    )
    return tuple(cards[k] for k in ("card-a", "card-b", "card-c", "card-d", "card-e"))


LEADER = make_card("card-L", type_="Cute")
ALL = None                                  # structurally team-wide
CUTE_2 = EffectTarget(kind="type", value="Cute", count=2)
COOL_2 = EffectTarget(kind="type", value="Cool", count=2)


def _pool_rate(members) -> float:
    return score_with_breakdown(make_team([LEADER, *members]), cfg()).combined_score_rate


def _occupancy() -> float:
    """The run-average occupancy of `_active`, read off a single-ACTIVE team
    (pool = 0.50 x occ). Both ACTIVEs share duration/interval/tier, so they
    share it. The E[max] walk (magnitudes descending) is then
    `m1 x occ + m2 x occ x (1 - occ)`."""
    solo = (make_card("card-a", type_="Cute", active=_active(50.0)),) + tuple(
        make_card(f"card-{c}", type_="Cool") for c in "bcde"
    )
    return _pool_rate(solo) / 0.5


def _walk(m1: float, m2: float, occ: float) -> float:
    hi, lo = max(m1, m2), min(m1, m2)
    return hi * occ + lo * occ * (1.0 - occ)


def test_all_target_support_boosts_every_active():
    """`all` support: both ACTIVEs (0.50 and 0.30) get x1.10."""
    occ = _occupancy()
    assert abs(_pool_rate(_members(ALL)) - _walk(0.55, 0.33, occ)) < 1e-12


def test_count_2_support_boosts_only_its_two_targets():
    """`{Cute, 2}` reaches card-a and card-b, not card-d: only card-a's
    ACTIVE is boosted. `{Cool, 2}` reaches card-d and card-e: only card-d's.
    Boosting both in either case would give the `all` number."""
    occ = _occupancy()
    assert abs(_pool_rate(_members(CUTE_2)) - _walk(0.55, 0.30, occ)) < 1e-12
    assert abs(_pool_rate(_members(COOL_2)) - _walk(0.50, 0.33, occ)) < 1e-12
    assert _pool_rate(_members(COOL_2)) < _pool_rate(_members(ALL))


def test_targeted_boost_can_change_which_active_is_the_maximum():
    """card-a 40 % un-targeted vs card-d 38 % targeted by {Cool,2} at +10 %:
    0.38 x 1.10 = 0.418 > 0.40 -> the targeted member's ACTIVE leads the walk."""
    members = list(_members(COOL_2))
    a = members[0]
    members[0] = dataclasses.replace(a, skills=(None, _active(40.0), None))
    d = members[3]
    members[3] = dataclasses.replace(d, skills=(None, _active(38.0), d.skills[2]))
    occ = _occupancy()
    assert abs(_pool_rate(tuple(members)) - _walk(0.418, 0.40, occ)) < 1e-12


def test_two_targeted_supports_on_one_member_sum():
    """Two `{Cool,2}` supports (10 % each) both reach card-d: the panel's
    per-card effect_up is a SUM (+80 +80), so card-d's 30 % becomes 0.36."""
    members = list(_members(COOL_2, support_on="card-e"))
    d = members[3]
    members[3] = dataclasses.replace(d, skills=(None, _active(30.0), _support(10.0, COOL_2)))
    a = members[0]
    members[0] = dataclasses.replace(a, skills=(None, _active(34.0), None))
    occ = _occupancy()
    assert abs(_pool_rate(tuple(members)) - _walk(0.36, 0.34, occ)) < 1e-12


def test_three_driver_parity_with_targeted_support():
    """`score_with_breakdown`, `score_fast`, `score_card_set`
    bit-identical with a count-targeted support in play."""
    for target in (ALL, CUTE_2, COOL_2):
        members = _members(target)
        team = make_team([LEADER, *members])
        c = cfg()
        full = score_with_breakdown(team, c).total_score
        fast = score_fast(team, c)
        batch = score_card_set(members, (LEADER,), c)[0].total_score
        assert full == fast == batch, (target, full, fast, batch)


def test_song_driver_agrees_with_deck_screen_ratio():
    """`score_song_detailed` on two notes at [25, 30) where both ACTIVEs are
    live with p = 1: the per-note pool must equal the ranking driver's
    `score_up_pool` (0.50 for {Cool,2}, 0.55 for {Cute,2} / all). Level 5,
    deck_power 2000 -> constant 2300 per note; combo < 100."""
    for target, pool in ((COOL_2, 0.50), (CUTE_2, 0.55), (ALL, 0.55)):
        members = _members(target)
        (result,) = score_song_detailed(
            members, (LEADER,), _chart(), cfg(), deck_power=2000.0
        )
        expected_note = 2300.0 * (1.0 + pool)
        assert result.total_score == 2 * expected_note, (target, result.total_score)


# A count-limited Score Support goes to the qualifying members whose ACTIVE is the
# strongest (its magnitude at the card's own skill level, a fired override competing
# with it), not the ones with the most parameter; a tie goes to the earlier slot.
# Read off the server's per-card `active_effect_up`, the recipients follow this rule on
# every deck measured, including the ones where ranking by total parameter disagrees.
BIG = Stats(performance=9000, technique=9000, sense=9000)


def _strongest_active_members(order=("card-a", "card-b", "card-c", "card-d", "card-e")):
    """card-a carries the most parameter but the weakest Cute ACTIVE (30 %); card-b
    (40 %) and card-c (49 %) have the default parameter. A {Cute, 2} support on card-e."""
    cards = {
        "card-a": make_card("card-a", type_="Cute", stats=BIG, active=_active(30.0)),
        "card-b": make_card("card-b", type_="Cute", active=_active(40.0)),
        "card-c": make_card("card-c", type_="Cute", active=_active(49.0)),
        "card-d": make_card("card-d", type_="Cool"),
        "card-e": make_card("card-e", type_="Cool", passive=_support(10.0, CUTE_2)),
    }
    return tuple(cards[k] for k in order)


def _support_recipients(members) -> tuple[str, ...]:
    bd = score_with_breakdown(make_team([LEADER, *members]), cfg())
    (row,) = [c for c in bd.contributions if c.family == "score_support"]
    return row.target_card_ids


def test_a_count_limited_support_goes_to_the_strongest_actives_not_the_most_parameter():
    assert _support_recipients(_strongest_active_members()) == ("card-b", "card-c")


def test_a_tie_in_active_magnitude_goes_to_the_earlier_slot():
    def tied(order):
        members = list(_strongest_active_members(order))
        k = [m.id for m in members].index("card-b")
        members[k] = dataclasses.replace(members[k], skills=(None, _active(49.0), None))
        members[0] = dataclasses.replace(members[0], skills=(None, _active(50.0), None))
        return tuple(members)
    # card-a at 50 % is paid; card-b and card-c tie at 49 % for the second place
    assert _support_recipients(tied(("card-a", "card-b", "card-c", "card-d", "card-e"))) == ("card-a", "card-b")
    assert _support_recipients(tied(("card-a", "card-c", "card-b", "card-d", "card-e"))) == ("card-a", "card-c")


def test_every_driver_pays_the_strongest_actives():
    """The deck-screen, fast, card-set and song drivers agree on the recipients:
    with p = 1 on both notes the song's pool is the larger boosted ACTIVE,
    0.49 x 1.10 (card-c paid): 2300 x 1.539 = 3539.7 a note, 3540 as scored; paying
    card-a and card-b instead leaves card-c's 0.49 on top, 2 x 3427 = 6854."""
    members = _strongest_active_members()
    team = make_team([LEADER, *members])
    c = cfg()
    full = score_with_breakdown(team, c).total_score
    assert full == score_fast(team, c) == score_card_set(members, (LEADER,), c)[0].total_score
    (result,) = score_song_detailed(members, (LEADER,), _chart(), c, deck_power=2000.0)
    assert result.total_score == 2 * 3540.0
