"""The song's own cast, and the two board terms that need it.

`SongChart` described a chart as `CalcScore` reads it and stopped there. But two
board quantities are functions of **who sings the song**, not of the chart, and
both were computed and discarded for want of anywhere to put the cast:

1. **The singer-conditional `skill_effect_up`.** A controlled triple of deck
   screens:

   | | board POWER | Active | board BONUS |
   |---|---|---|---|
   | Aki leader, no `R-002`, no song | 6,535 | 413 | absent = 0 |
   | Aki leader, **with** `R-002`, no song | 7,285 | 413 | absent = 0 |
   | Aki leader, with `R-002`, **Aki is the Singer** | 7,285 | 413 | **41 permil** |

   `R-002` reads *"When Aki Rosenthal is included as a singer, Grants Score
   Support Effect of 10.0 % to All."* Buying it moved the bonus line by **zero**
   permil on a songless deck screen, and it fired at **41** the moment the
   song's singer was that holomem. So `BoardEffect.conditional` is right, and
   excluding it is right for the song-agnostic ranking and **wrong** for a named
   chart.

2. **`song_score_bonus` at `solo` and `group` scope.** The game's
   `SkillTreeEffectTargetMusicDialogPresenter::CalculateCurrentEffect
   Value` @`0x67f3670` takes the contributing set from the SONG
   (`music.IsWholeMusic ? get_RegularCharacterListCache : Music::get_Characters`)
   and each singer's effects are filtered by `musicSingerType_` — 3 whole,
   2 unit, 1 solo. `BoardContribution.song_score_bonus_for` implements this and
   needs the singers named. Pooling account-wide instead overstates the bonus
   (e.g. 120 permil where the real solo number is 40) and looks like a cap
   violation.

⚠ **The unresolved reading, stated so nobody later mistakes it for settled.**
`R-002` sits on Aki's board and names Aki. One measurement cannot distinguish
*"the node's own holomem must be a singer"* from *"a character named inside the
effect id must be a singer"* — they coincide on every known node. This module
implements the first, because it is the simpler reading and the only one the
parsed effect id can express. A node whose conditional names someone
OTHER than its owner would separate them; none is known to exist.

By default a chart with no cast carries
`singers = ()`, both terms stay at zero, and the song-agnostic behaviour is
unchanged.
"""

from __future__ import annotations

import dataclasses

import pytest

from hbuilder.domain.board import (
    BoardEffect,
    BoardModel,
    BoardNode,
    CharacterBoard,
)
from hbuilder.domain.song import ChartNote, SongChart, SongError, score_song

from conftest import make_card, make_scoring_config, make_skill

CERTAIN_TIERS = {"Low": 1.0, "Medium": 1.0, "High": 1.0}

AKI_CONDITIONAL_PERMIL = 100.0  # R-002's stated rate; 41 permil is what it BUYS


def cfg(**overrides):
    base = make_scoring_config(probability_tiers=CERTAIN_TIERS)
    return dataclasses.replace(base, **overrides) if overrides else base


def _effect(
    permil: float,
    kind: str,
    scope: str,
    *,
    conditional: bool = False,
    song_scope: str | None = None,
):
    return BoardEffect(
        family=(
            "live_active_skill_effect_up_permil_up"
            if kind == "skill_effect_up"
            else "live_score_bonus_add_permil_up"
        ),
        kind=kind,
        stat="",
        value=permil / 1000.0,
        scope=scope,
        conditional=conditional,
        song_scope=song_scope,
    )


def _board(*by_holomem) -> BoardModel:
    """`_board(("Aki", effect), ("Ina", effect))` -> one node each."""
    return BoardModel(
        characters={
            holomem: CharacterBoard(
                holomem=holomem, nodes=(BoardNode(f"R-{i:03d}", (effect,)),)
            )
            for i, (holomem, effect) in enumerate(by_holomem)
        }
    )


def _chart(singers=(), singer_type="", times=(25.0, 30.0)) -> SongChart:
    return SongChart(
        music_id="m0001",
        difficulty="easy",
        difficulty_level=5,
        live_score_coefficient_permil=4,
        notes=tuple(ChartNote(time=t, perfect_permil=1000) for t in times),
        special_cues=(10.0, 20.0, 30.0, 40.0, 50.0),
        singers=singers,
        singer_type=singer_type,
    )


def _scorer(card_id: str, magnitude: float, member: str):
    """An ACTIVE live across both of `_chart`'s notes — window `[20, 50)`.

    ⚠ The cooldown starts at 0 with no check at t=0, so the first activation is
    at `t = interval`. Notes before that measure the plain baseline."""
    return make_card(
        card_id,
        member=member,
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            effect_type="score",
            duration=30.0,
            interval=20.0,
            probability_tier="High",
            magnitude=magnitude,
        ),
    )


def _members():
    return (_scorer("card-a", 50.0, "Member A"),) + tuple(
        make_card(f"card-{i}", member=f"Member {i}") for i in range(1, 5)
    )


# ---------------------------------------------------------------------------
# The chart carries a cast, and refuses an incoherent one
# ---------------------------------------------------------------------------


def test_a_chart_defaults_to_no_cast_and_that_changes_nothing():
    chart = _chart()
    assert chart.singers == ()
    assert chart.singer_type == ""


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"singers": ("Aki",), "singer_type": "duet"}, "singer_type"),
        ({"singers": (), "singer_type": "solo"}, "names no singers"),
        ({"singers": ("Aki", "Ina", "Miko"), "singer_type": "solo"}, "distinct singers"),
    ],
)
def test_an_incoherent_cast_raises_rather_than_scoring_a_plausible_number(
    kwargs, fragment
):
    """A `solo` scope with THREE+ singers would silently pool their boards and cap
    at a number that looks reasonable — which is exactly the failure that made 120
    permil look like a cap violation. It raises instead. A genuine DUO (two
    singers, e.g. FUWAMOCO) is admitted — that is the attested duo-solo case, not
    an incoherent cast — and is exercised in test_board.py."""
    with pytest.raises(SongError) as excinfo:
        _chart(**kwargs)
    assert fragment in str(excinfo.value)


# ---------------------------------------------------------------------------
# 1. The singer-conditional `skill_effect_up`
# ---------------------------------------------------------------------------


def test_a_conditional_fires_when_its_own_holomem_sings():
    """The deck-screen measurement, reproduced in the scorer's arithmetic.

        constant = 2000 x 2.300 x 1.0 / 2.0 = 2300.0 per note

    Member A's ACTIVE is live across both notes at 50 %. Aki leads and her board
    carries a 100-permil CONDITIONAL leader-scope `skill_effect_up`.

    With no cast the term is excluded and each note scores
        ceil(2300.0 x 1.50) = 3450        -> 6900
    With Aki as the credited singer it enters `CalcScoreUpRatio`'s parenthesis
        0.50 x (1 + 0.100) = 0.55
        ceil(2300.0 x 1.55) = 3565        -> 7130
    """
    members = _members()
    leader = make_card("leader", member="Aki")
    board = _board(
        ("Aki", _effect(AKI_CONDITIONAL_PERMIL, "skill_effect_up", "leader",
                        conditional=True))
    )

    songless = score_song(members, (leader,), _chart(), cfg(board=board),
                          deck_power=2000)
    sung = score_song(
        members, (leader,), _chart(singers=("Aki",), singer_type="solo"),
        cfg(board=board), deck_power=2000,
    )

    assert songless[0].total_score == 6900
    assert sung[0].total_score == 7130


def test_a_conditional_stays_silent_when_someone_else_sings():
    """The other half of the same measurement: the conditional contributed
    exactly zero until its own holomem was the credited Singer."""
    members = _members()
    leader = make_card("leader", member="Aki")
    board = _board(
        ("Aki", _effect(AKI_CONDITIONAL_PERMIL, "skill_effect_up", "leader",
                        conditional=True))
    )
    chart = _chart(singers=("Ina",), singer_type="solo")

    result = score_song(members, (leader,), chart, cfg(board=board),
                        deck_power=2000)
    assert result[0].total_score == 6900


def test_a_conditional_still_needs_its_ordinary_scope_to_reach_the_team():
    """Being a singer is a SECOND condition, not a replacement for the first.
    Aki's node is leader-scoped, so under a different leader it pays nothing
    even on Aki's own song."""
    members = _members()
    board = _board(
        ("Aki", _effect(AKI_CONDITIONAL_PERMIL, "skill_effect_up", "leader",
                        conditional=True))
    )
    chart = _chart(singers=("Aki",), singer_type="solo")

    result = score_song(
        members, (make_card("leader", member="Nene"),), chart,
        cfg(board=board), deck_power=2000,
    )
    assert result[0].total_score == 6900


def test_the_unconditional_term_is_unaffected_by_the_cast():
    """A non-conditional node pays the same whether or not its holomem sings —
    otherwise the cast would be silently gating a term that does not depend on it."""
    members = _members()
    leader = make_card("leader", member="Ina")
    board = _board(("Ina", _effect(173.0, "skill_effect_up", "leader")))

    without_cast = score_song(members, (leader,), _chart(), cfg(board=board),
                              deck_power=2000)
    with_cast = score_song(
        members, (leader,), _chart(singers=("Nene",), singer_type="solo"),
        cfg(board=board), deck_power=2000,
    )
    assert without_cast[0].total_score == with_cast[0].total_score == 7298


# ---------------------------------------------------------------------------
# 2. `song_score_bonus` at solo / group scope
# ---------------------------------------------------------------------------


def test_a_solo_song_takes_only_its_own_singers_board():
    """Two characters each hold a 40-permil `solo`
    Y-tile. On Aki's solo song only Aki's contributes.

    No skills at all, so the only factor is `(1 + ScoreUpBonusRatioBySkillTree)`:
        constant = 2000 x 2.300 / 2.0 = 2300.0
        per note = ceil(2300.0 x 1.040) = 2392      -> 4784
    Pooling both boards would give 1.080 -> 2484 -> 4968, which is the
    over-count this scope exists to prevent."""
    plain = tuple(make_card(f"card-{i}", member=f"Member {i}") for i in range(5))
    board = _board(
        ("Aki", _effect(40.0, "song_score_bonus", "account", song_scope="solo")),
        ("Ina", _effect(40.0, "song_score_bonus", "account", song_scope="solo")),
    )
    chart = _chart(singers=("Aki",), singer_type="solo")

    result = score_song(plain, (make_card("leader"),), chart, cfg(board=board),
                        deck_power=2000)
    assert result[0].total_score == 4784


def test_a_group_song_sums_its_units_boards():
    """`group` takes the song's unit. Two singers, 40 permil each:
        per note = ceil(2300.0 x 1.080) = 2484      -> 4968"""
    plain = tuple(make_card(f"card-{i}", member=f"Member {i}") for i in range(5))
    board = _board(
        ("Aki", _effect(40.0, "song_score_bonus", "account", song_scope="group")),
        ("Ina", _effect(40.0, "song_score_bonus", "account", song_scope="group")),
    )
    chart = _chart(singers=("Aki", "Ina"), singer_type="group")

    result = score_song(plain, (make_card("leader"),), chart, cfg(board=board),
                        deck_power=2000)
    assert result[0].total_score == 4968


def test_a_chart_with_no_cast_falls_back_to_the_pooled_all_scope():
    """The compatibility guarantee. Every existing caller passes no cast, and
    must keep getting `score_bonus_rate()` — the pooled `all` bucket."""
    plain = tuple(make_card(f"card-{i}", member=f"Member {i}") for i in range(5))
    board = _board(
        ("Aki", _effect(40.0, "song_score_bonus", "account", song_scope="all")),
    )

    result = score_song(plain, (make_card("leader"),), _chart(), cfg(board=board),
                        deck_power=2000)
    # constant 2300.0 x 1.040 -> 2392 per note
    assert result[0].total_score == 4784
