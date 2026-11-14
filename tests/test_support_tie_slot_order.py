"""A count-limited Score Support tie is broken by the slot order the walk chooses.

The game pays a `count` k support to the k qualifiers with the strongest ACTIVE and gives an
exact tie to the EARLIER slot (`scoring_models.TopNByBuffedStat`). The slot order is also what
places each SPECIAL on its cue, and the model takes the best of the 120 orders: so when the
cut falls inside a group of equal magnitudes, one order decides both the cues and who is
paid. These tests pin that the score is the best over the 120 orders WITH each order's own
recipients, and that it no longer depends on the order the members are passed in."""

from __future__ import annotations

import itertools

import pytest
from support.builders import CERTAIN_TIERS, make_card, make_scoring_config, make_skill

from hbuilder.domain import song
from hbuilder.domain.card import EffectTarget, Stats
from hbuilder.domain.song import ChartNote, SongChart, score_song_detailed
from hbuilder.domain.unknown import UNKNOWN

CHART = SongChart(
    music_id="m0001",
    difficulty="easy",
    difficulty_level=5,
    live_score_coefficient_permil=4,
    notes=tuple(ChartNote(time=float(t), perfect_permil=1000) for t in range(2, 120, 2)),
    special_cues=(12.0, 31.0, 52.0, 71.0, 93.0),
)


def _active(interval: float, duration: float, magnitude: float = 50.0):
    return make_skill(
        "ACTIVE",
        family="conditional_score_up",
        effect_type="score",
        duration=duration,
        interval=interval,
        probability_tier="High",
        magnitude=magnitude,
    )


def _special(magnitude: float):
    return make_skill(
        "SPECIAL", family="score_support", effect_type="score", duration=8.0, magnitude=magnitude
    )


def _members():
    """card-a and card-b tie at 50 for the one place of card-s's Cute/1 support, with
    different windows; card-a and card-c carry SPECIAL boosts, so the order matters twice."""
    support = make_skill(
        "PASSIVE",
        family="score_support",
        effect_type="score",
        magnitude=60.0,
        target=EffectTarget(kind="type", value="Cute", count=1),
    )
    return (
        make_card("card-a", type_="Cute", active=_active(20.0, 10.0), special=_special(80.0)),
        make_card("card-b", type_="Cute", active=_active(25.0, 12.0)),
        make_card("card-c", type_="Cool", active=_active(30.0, 9.0, 40.0), special=_special(40.0)),
        make_card("card-d", type_="Cool", active=_active(17.0, 6.0, 30.0)),
        make_card("card-s", type_="Cute", passive=support),
    )


LEADER = make_card("card-L", type_="Cool")


def _cfg():
    return make_scoring_config(probability_tiers=CERTAIN_TIERS)


def _score(members) -> float:
    return score_song_detailed(members, (LEADER,), CHART, _cfg())[0].total_score


def _game_optimum(members) -> float:
    """The best over the 120 orders, each walked with the recipients ITS order gives: the
    members passed in slot order p, read at order p (the reference walk's `order_totals`)."""
    best = None
    for p in itertools.permutations(range(5)):
        ordered = tuple(members[i] for i in p)
        totals: dict = {}
        score_song_detailed(ordered, (LEADER,), CHART, _cfg(), order_totals=totals)
        value = totals[(LEADER.id, tuple(c.id for c in ordered))]
        best = value if best is None else max(best, value)
    return best


def test_the_tie_has_two_outcomes_and_no_tie_has_none():
    members = _members()
    held = song._member_setup(members, _cfg())
    outcomes = held[-1]
    assert outcomes is not None and len(outcomes) == 2
    # every order belongs to exactly one outcome
    assert [sum(o[1][i] for o in outcomes) for i in range(120)] == [1] * 120
    # break the tie: no outcome
    a, b, *rest = members
    untied = (a, make_card("card-b", type_="Cute", active=_active(25.0, 12.0, 49.0)), *rest)
    assert song._member_setup(untied, _cfg())[-1] is None


def test_the_score_is_the_game_optimum_over_the_orders_with_their_own_recipients():
    members = _members()
    assert _score(members) == _game_optimum(members)


def test_the_input_order_decides_nothing():
    members = _members()
    values = {_score(tuple(members[i] for i in p)) for p in itertools.permutations(range(5))}
    assert len(values) == 1


def test_an_unknown_active_magnitude_ranks_the_tie_by_total_parameter():
    """With one ACTIVE magnitude unknown, `active_magnitude_by_card` gives no ranking and the
    support's recipients are ranked by total parameter (`targets.resolve_target_card_ids`): the
    tie is looked for on that same ranking. Here card-a and card-b tie on parameter (600) though
    their ACTIVEs differ, and card-s (300) is below the cut."""
    high, low = (
        Stats(performance=200, technique=200, sense=200),
        Stats(performance=100, technique=100, sense=100),
    )
    support = make_skill(
        "PASSIVE",
        family="score_support",
        effect_type="score",
        magnitude=60.0,
        target=EffectTarget(kind="type", value="Cute", count=1),
    )
    members = (
        make_card("card-a", type_="Cute", stats=high, active=_active(20.0, 10.0), special=_special(80.0)),
        make_card("card-b", type_="Cute", stats=high, active=_active(25.0, 12.0, 49.0)),
        make_card("card-c", type_="Cool", active=_active(30.0, 9.0, 40.0), special=_special(40.0)),
        make_card("card-d", type_="Cool", active=_active(17.0, 6.0, UNKNOWN)),
        make_card("card-s", type_="Cute", stats=low, passive=support),
    )
    outcomes = song._member_setup(members, _cfg())[-1]
    assert outcomes is not None and len(outcomes) == 2
    assert [sum(o[1][i] for o in outcomes) for i in range(120)] == [1] * 120
    assert _score(members) == _game_optimum(members)


@pytest.mark.parametrize("with_kernel", [False, True])
def test_kernel_and_python_agree(monkeypatch, with_kernel):
    monkeypatch.setattr(song, "NOTE_WALK_KERNEL", None)
    monkeypatch.delenv(song.NOTE_WALK_KERNEL_ENV, raising=False)
    monkeypatch.setattr(song, "_env_kernel_state", ("", None))
    if with_kernel:
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "web" / "kernel"))
        try:
            import notewalk

            notewalk.load()
        except Exception as exc:  # noqa: BLE001 - no compiler here
            pytest.skip(f"note-walk kernel unavailable: {exc!r}")
        monkeypatch.setattr(song, "NOTE_WALK_KERNEL", notewalk.walk)
    members = _members()
    assert _score(members) == _game_optimum(members)
