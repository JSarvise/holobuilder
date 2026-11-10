"""Ranking teams for an AUTO run, which is a DIFFERENT question from ranking
them for manual play — and the difference is not only the note column.

On AUTO the combo never accumulates (seen in a live trace of the game), so a
`SlotOverride` gated on `combo >= N` NEVER fires. Luna's active runs at her
base 50 % for a whole song where the model, on its manual premise, resolves
her to 100 %. Life-gated overrides do fire, because
life is full from the first second.

So an AUTO ranking needs BOTH halves:

1. `auto=True` — the AUTO judgement column, which `score_song_detailed` takes
   and every AUTO ranking path must pass;
2. `run_state.combo = 0` — or the scorer keeps granting combo-gated overrides
   that the run will never see, and over-values exactly the cards that carry
   them.

Half of it is worse than neither: an AUTO score with a manual combo premise is a
number for a run that does not exist.
"""

from __future__ import annotations

import dataclasses

from support.builders import make_card, make_scoring_config, make_skill
from support.song_ranking import rank_on_chart

from hbuilder.domain.card import GateCondition, SlotOverride
from hbuilder.domain.scoring_config import RunState
from hbuilder.domain.song import ChartNote, SongChart, score_song_detailed
from hbuilder.domain.team import Team

CERTAIN = {"Low": 1.0, "Medium": 1.0, "High": 1.0}


def _cfg(**over):
    base = make_scoring_config(probability_tiers=CERTAIN)
    return dataclasses.replace(base, **over) if over else base


def _chart():
    return SongChart(
        music_id="m0001",
        difficulty="easy",
        difficulty_level=5,
        live_score_coefficient_permil=4,
        # a note every 0.25 s: the combo is past 40 by the first check (20 s), as on a
        # real chart -- the leaf reads the combo AT each check (`song._combo_split`)
        notes=tuple(ChartNote(time=0.25 * k, perfect_permil=1000) for k in range(1, 221)),
        special_cues=(10.0, 20.0, 30.0, 40.0, 50.0),
    )


def _combo_gated_scorer(card_id="combo-card"):
    """An ACTIVE whose magnitude DOUBLES at combo >= 40 — the shape the trace
    caught: Luna 50 -> 100, Polka 30 -> 60."""
    return make_card(
        card_id,
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            effect_type="score",
            duration=10.0,
            interval=20.0,
            probability_tier="High",
            magnitude=50.0,
            override=SlotOverride(
                magnitude=100.0,
                condition=GateCondition(kind="run_state", target="combo", count=40, operator="gte"),
            ),
        ),
    )


def _team():
    members = (_combo_gated_scorer(),) + tuple(make_card(f"card-{i}") for i in range(4))
    return Team(leader=make_card("leader"), members=members)


# ==========================================================================
# 1. The flag
# ==========================================================================


def test_the_auto_premise_defaults_off():
    """Every existing per-song number is on the manual premise."""
    assert make_scoring_config().song_auto is False


def test_the_ranking_path_scores_auto_when_asked():
    team, chart = _team(), _chart()
    manual = rank_on_chart([team], chart, _cfg()).teams[0].expected_score
    auto = rank_on_chart([team], chart, _cfg(song_auto=True)).teams[0].expected_score
    reference = score_song_detailed(
        tuple(team.members),
        (team.leader,),
        chart,
        _cfg(song_auto=True),
        deck_power=None,
        auto=True,
    )[0].total_score
    assert auto == reference
    assert auto != manual


def test_the_default_still_scores_the_manual_column():
    team, chart = _team(), _chart()
    ranked = rank_on_chart([team], chart, _cfg()).teams[0].expected_score
    reference = score_song_detailed(tuple(team.members), (team.leader,), chart, _cfg())[0].total_score
    assert ranked == reference


# ==========================================================================
# 2. The half that is easy to forget
# ==========================================================================


def test_a_combo_gated_override_fires_under_the_manual_premise():
    """The baseline: `run_state.combo = 100` means combo >= 40 holds, so the
    card is worth its 100 % override."""
    team, chart = _team(), _chart()
    assert _cfg().run_state.combo >= 40
    with_override = rank_on_chart([team], chart, _cfg()).teams[0].expected_score
    without = (
        rank_on_chart([team], chart, _cfg(run_state=RunState(life=1000, combo=0))).teams[0].expected_score
    )
    assert with_override > without


def test_the_auto_premise_must_zero_the_combo_or_it_over_values_the_card():
    """On AUTO the combo never accumulates and the override never
    fires. Scoring AUTO while still asserting combo >= 40 keeps granting a
    magnitude the run will never produce — this pins that the two halves are
    both needed, so a caller cannot get half an AUTO premise and a number for a
    run that does not exist."""
    team, chart = _team(), _chart()
    half = rank_on_chart([team], chart, _cfg(song_auto=True)).teams[0].expected_score
    whole = (
        rank_on_chart([team], chart, _cfg(song_auto=True, run_state=RunState(life=1000, combo=0)))
        .teams[0]
        .expected_score
    )
    assert whole < half, (
        "zeroing the combo must lower this team's AUTO score — it carries a "
        "combo-gated override the AUTO run cannot satisfy"
    )


def test_the_manual_combo_is_read_at_each_check():
    """A slow start: 10 notes in the first 20 s, then dense. The check at 20 s has 10
    combo behind it (base 50 %), the later ones are past 40 (override 100 %), so the
    manual score sits strictly between "base everywhere" and "override everywhere"
    (the combo is read at each check, as the community tool holodori.best does, not
    granted from the first check)."""
    times = [2.0 * k for k in range(1, 11)] + [20.0 + 0.25 * k for k in range(1, 161)]
    chart = SongChart(
        music_id="m0001",
        difficulty="easy",
        difficulty_level=5,
        live_score_coefficient_permil=4,
        notes=tuple(ChartNote(time=t, perfect_permil=1000) for t in times),
        special_cues=(10.0, 20.0, 30.0, 40.0, 50.0),
    )
    team = _team()
    manual = rank_on_chart([team], chart, _cfg()).teams[0].expected_score
    never = rank_on_chart([team], chart, _cfg(run_state=RunState(life=1000, combo=0))).teams[0].expected_score
    always_card = make_card(
        "combo-card",
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            effect_type="score",
            duration=10.0,
            interval=20.0,
            probability_tier="High",
            magnitude=100.0,
        ),
    )
    always = (
        rank_on_chart([Team(leader=team.leader, members=(always_card,) + team.members[1:])], chart, _cfg())
        .teams[0]
        .expected_score
    )
    assert never < manual < always


# ==========================================================================
# 2. A SPECIAL's combo-gated activation-rate rider
# ==========================================================================


def _rider_team(condition):
    """A member whose SPECIAL raises the team's activation rate at each cue while
    `condition` holds, and a member with an uncertain ACTIVE for it to raise."""
    from hbuilder.domain.card import SlotRider

    host = make_card(
        "rider-host",
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
                condition=condition,
                target=None,
            ),
        ),
    )
    scorer = make_card(
        "scorer",
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            effect_type="score",
            duration=10.0,
            interval=20.0,
            probability_tier="Medium",
            magnitude=50.0,
        ),
    )
    members = (host, scorer) + tuple(make_card(f"card-{i}") for i in range(3))
    return Team(leader=make_card("leader"), members=members)


def test_a_combo_gated_rider_does_not_fire_on_auto():
    """On AUTO the combo never accumulates, so a SPECIAL rider gated on combo >= N
    never fires -- the same premise the combo-gated ACTIVE override follows
    (`song._combo_split`). The rider is read per cue against the chart's notes on the
    manual premise only."""
    half = {"Low": 0.3, "Medium": 0.5, "High": 0.7}
    auto = _cfg(probability_tiers=half, song_auto=True, run_state=RunState(life=1000, combo=0))
    manual = _cfg(probability_tiers=half)
    chart = _chart()
    combo40 = GateCondition(kind="run_state", target="combo", count=40, operator="gte")
    never = GateCondition(kind="run_state", target="combo", count=10_000, operator="gte")
    score = lambda cond, cfg: rank_on_chart([_rider_team(cond)], chart, cfg).teams[0].expected_score  # noqa: E731
    # manual: the cues past 40 notes raise the rate, so the rider is worth something
    assert score(combo40, manual) > score(never, manual)
    # AUTO: worth nothing, exactly as a rider that can never fire
    assert score(combo40, auto) == score(never, auto)


def test_an_auto_note_carries_no_combo_multiplier():
    """On AUTO the combo never accumulates, so no note gets the LiveCombo multiplier:
    every note's weight is its AUTO column alone. Manual play under the full-perfect
    premise counts the combo note by note (the n-th note carries the tier of combo n)."""
    from hbuilder.domain.song import _chart_weights

    chart = _chart()
    auto, _ = _chart_weights(chart, "mobile", True)
    manual, _ = _chart_weights(chart, "mobile", False)
    assert auto == tuple(n.auto_permil / 1000.0 for n in chart.notes)
    assert manual == tuple(
        (n.perfect_permil_for("mobile") / 1000.0) * (1.0 + chart.combo_coefficient(k + 1))
        for k, n in enumerate(chart.notes)
    )
    assert manual[-1] > manual[0]  # the last note carries the highest combo tier
