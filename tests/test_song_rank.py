"""Ranking teams by what they would actually score, on one chart or across all.

The two-stage design and its cost are in `domain/song_rank.py`'s docstring.
These pin the parts that would be wrong silently:

* the ranking must AGREE with `score_song_detailed` — it is a re-ranker, not a
  second scorer, and a drift between them would be invisible;
* ties must break deterministically, or two runs over the same pool disagree
  about rank 1;
* an across-songs row must NOT carry a single slot order, because the order
  that wins one chart is not the order that wins them all;
* the renderer must never reorder what it is given.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FIXTURE_CARDS_PATH, make_scoring_config

from hbuilder.domain.loader import load_cards
from hbuilder.domain.song import (
    ChartNote,
    SongChart,
    load_charts,
    overall_power_for,
    score_song_detailed,
)
from hbuilder.domain.song_rank import (
    TeamSongScore,
    assemble_across_charts,
    rank_across_charts,
    rank_on_chart,
    score_teams_on_chart,
)
from hbuilder.domain.team import Team
from hbuilder.output.song_table import (
    SONGS_TABLE_COLUMNS,
    SONG_TABLE_COLUMNS,
    format_song_basis,
    format_song_rows,
)

_DUMPS = Path(__file__).resolve().parents[1] / "tools" / "recon" / "proxy" / "dumps"


@pytest.fixture(scope="module")
def cards():
    return load_cards(FIXTURE_CARDS_PATH)


@pytest.fixture(scope="module")
def cfg():
    return make_scoring_config()


def _chart(music_id="m0001", scale=1):
    """A tiny chart. `scale` shifts the note weights so two charts can disagree
    about which team is best, which is what the WINS column exists to show."""
    return SongChart(
        music_id=music_id,
        difficulty="EXPERT",
        difficulty_level=26,
        live_score_coefficient_permil=4,
        notes=tuple(ChartNote(time=float(t), perfect_permil=1000 * scale)
                    for t in range(1, 40)),
        special_cues=(5.0, 10.0, 15.0, 20.0, 25.0),
    )


def _teams(cards, n=3):
    """`n` teams from the 6-card fixture: leader `i`, the other five as members.

    That is the only shape the fixture affords — 6 cards means exactly 6
    distinct (leader, members) teams — and it is enough, because what is under
    test is the ranking, not the enumeration."""
    return [
        Team(leader=cards[i], members=tuple(c for j, c in enumerate(cards) if j != i))
        for i in range(n)
    ]


# ==========================================================================
# 1. It is a re-ranker, not a second scorer
# ==========================================================================


def test_the_ranking_reproduces_score_song_detailed_exactly(cards, cfg):
    """If these ever drift, every per-song answer is wrong and nothing says so."""
    chart = _chart()
    teams = _teams(cards)
    ranking = rank_on_chart(teams, chart, cfg)

    by_leader = {row.leader_id: row for row in ranking.teams}
    for team in teams:
        # Same deck power as `_score_one` supplies — see the note in
        # `test_score_teams_on_chart_matches_the_reference_scorer_row_by_row`.
        reference = score_song_detailed(
            tuple(team.members), (team.leader,), chart, cfg,
            deck_power=overall_power_for(tuple(team.members), team.leader, cfg),
        )[0]
        row = by_leader[team.leader.id]
        assert row.expected_score == reference.total_score
        assert row.member_order == reference.member_order


def test_the_ranking_is_sorted_best_first(cards, cfg):
    ranking = rank_on_chart(_teams(cards), _chart(), cfg)
    scores = [row.expected_score for row in ranking.teams]
    assert scores == sorted(scores, reverse=True)


def test_ties_break_deterministically(cards, cfg):
    """Two runs over one pool must not disagree about rank 1. The tie-break is
    `(leader_id, member_ids)` — the same determinism anchor `canonical_pool`
    gives the solver."""
    teams = _teams(cards)
    chart = _chart()
    first = rank_on_chart(teams, chart, cfg)
    second = rank_on_chart(list(reversed(teams)), chart, cfg)
    assert [r.leader_id for r in first.teams] == [r.leader_id for r in second.teams]


# ==========================================================================
# 2. Across songs
# ==========================================================================


def test_the_mean_is_the_plain_mean_over_the_charts(cards, cfg):
    charts = [_chart("m0001"), _chart("m0002", scale=2)]
    ranking = rank_across_charts(_teams(cards), charts, cfg)
    for row in ranking.teams:
        assert len(row.per_chart) == 2
        expected = sum(c.expected_score for c in row.per_chart) / 2
        assert row.expected_score == pytest.approx(expected)
    assert ranking.chart_count == 2
    assert ranking.charts == ("m0001", "m0002")


def test_wins_counts_charts_and_sums_to_the_chart_count(cards, cfg):
    charts = [_chart("m0001"), _chart("m0002", scale=2), _chart("m0003", scale=3)]
    ranking = rank_across_charts(_teams(cards), charts, cfg)
    assert sum(row.wins for row in ranking.teams) == len(charts)


def test_an_across_songs_row_carries_no_single_slot_order(cards, cfg):
    """The order that wins one chart is not the order that wins them all.
    Reporting one would read as "play this order on everything", which is
    false — `per_chart` carries one per chart instead."""
    ranking = rank_across_charts(_teams(cards), [_chart("m0001"), _chart("m0002")], cfg)
    for row in ranking.teams:
        assert row.member_order == ()
        assert all(c.member_order for c in row.per_chart)


def test_no_charts_is_an_empty_ranking_not_a_crash(cards, cfg):
    ranking = rank_across_charts(_teams(cards), [], cfg)
    assert ranking.teams == () and ranking.chart_count == 0


# ==========================================================================
# 2a. The seam the parallel path shares: per-chart rows + one assembler
# ==========================================================================


def test_score_teams_on_chart_matches_the_reference_scorer_row_by_row(cards, cfg):
    """A row is `score_song_detailed`'s answer in TEAM ORDER — never a ranking.
    This is what a worker ships home, so every field is pinned: a dropped
    `member_order` or `deck_power` would be the recurring
    computed-then-discarded defect crossing a process boundary."""
    chart = _chart()
    teams = _teams(cards)
    rows = score_teams_on_chart(teams, chart, cfg)

    assert len(rows) == len(teams)
    for team, row in zip(teams, rows):
        # The SAME deck power the ranking path supplies.
        # `cfg.song_absolute_deck_power` defaults on, so `_score_one` hands `score_song_detailed` the server's Overall Power;
        # a reference call that let it default would compare two different
        # questions and read as drift.
        reference = score_song_detailed(
            tuple(team.members), (team.leader,), chart, cfg,
            deck_power=overall_power_for(tuple(team.members), team.leader, cfg),
        )[0]
        assert row.music_id == chart.music_id
        assert row.expected_score == reference.total_score
        assert row.member_order == reference.member_order
        assert row.deck_power == reference.deck_power


def test_assembling_serial_rows_reproduces_rank_across_charts_exactly(cards, cfg):
    """`rank_across_charts` = per-chart rows + `assemble_across_charts`, and
    the parallel path composes the SAME two pieces. If this equality ever
    breaks, the two paths have two definitions of the ranking."""
    charts = [_chart("m0001"), _chart("m0002", scale=2), _chart("m0003", scale=3)]
    teams = _teams(cards)

    rows = [score_teams_on_chart(teams, chart, cfg) for chart in charts]
    assembled = assemble_across_charts(teams, charts, rows)
    direct = rank_across_charts(teams, charts, cfg)

    assert assembled == direct


def test_assemble_gives_a_tied_chart_to_the_earliest_team_index(cards, cfg):
    """The per-chart winner is the FIRST index at the maximum (strict `>`).
    Under a completion-order merge a tie could drift to whichever worker
    reported first; the assembler must make it a pure function of the matrix."""
    charts = [_chart("m0001")]
    teams = _teams(cards, n=2)
    tied = [
        (
            TeamSongScore("m0001", 100.0, ("a",) * 5, 1.0),
            TeamSongScore("m0001", 100.0, ("b",) * 5, 1.0),
        )
    ]
    ranking = assemble_across_charts(teams, charts, tied)

    by_leader = {row.leader_id: row for row in ranking.teams}
    assert by_leader[teams[0].leader.id].wins == 1
    assert by_leader[teams[1].leader.id].wins == 0


def test_assemble_refuses_a_matrix_that_does_not_match_its_charts(cards, cfg):
    """A partial matrix aggregated anyway would be a silently wrong mean —
    the exact failure shape the parallel path's fallback exists to prevent."""
    charts = [_chart("m0001"), _chart("m0002")]
    teams = _teams(cards)
    rows = [score_teams_on_chart(teams, charts[0], cfg)]  # one row short

    with pytest.raises(ValueError, match="every chart must have exactly one row"):
        assemble_across_charts(teams, charts, rows)

    short_row = [rows[0][:-1], rows[0][:-1]]  # a team missing from each row
    with pytest.raises(ValueError, match="for 3 team"):
        assemble_across_charts(teams, charts, short_row)


def test_the_serial_progress_cadence_is_per_evaluation_and_reaches_the_total(
    cards, cfg
):
    """`rank_across_charts` promises `(done, total)` per evaluation; the
    parallel path promises the same endpoints at chart cadence. The serial
    contract is pinned here so a change to it is a decision, not a drift."""
    charts = [_chart("m0001"), _chart("m0002", scale=2)]
    teams = _teams(cards)
    seen: list[tuple[int, int]] = []

    rank_across_charts(teams, charts, cfg, progress=lambda d, t: seen.append((d, t)))

    total = len(charts) * len(teams)
    assert seen == [(done, total) for done in range(1, total + 1)]


# ==========================================================================
# 3. The renderer
# ==========================================================================


def test_the_renderer_never_reorders_and_uses_the_right_columns(cards, cfg):
    one = rank_on_chart(_teams(cards), _chart(), cfg)
    lines = format_song_rows(one)
    assert lines[0] == " ".join(SONG_TABLE_COLUMNS)
    assert [line.split()[2] for line in lines[1:]] == [
        row.leader_id for row in one.teams
    ]

    across = rank_across_charts(_teams(cards), [_chart("m0001"), _chart("m0002")], cfg)
    lines = format_song_rows(across)
    assert lines[0] == " ".join(SONGS_TABLE_COLUMNS)
    assert [line.split()[3] for line in lines[1:]] == [
        row.leader_id for row in across.teams
    ]


def test_top_truncates_without_reordering(cards, cfg):
    ranking = rank_on_chart(_teams(cards), _chart(), cfg)
    assert len(format_song_rows(ranking, top=2)) == 3  # header + 2


def test_the_basis_always_states_the_stage_one_filter(cards, cfg):
    """A reader who is not told that stage 1 filtered cannot know to widen K
    and re-check. It is a statement of fact, not a caveat, so it is always
    printed."""
    ranking = rank_on_chart(_teams(cards), _chart(), cfg, filtered_from=50)
    basis = format_song_basis(ranking)
    assert any("top 50" in line for line in basis)
    assert any("m0001" in line for line in basis)


# ==========================================================================
# 4. Against the real charts
# ==========================================================================


def test_a_real_chart_ranks_end_to_end(cards, cfg):
    path = _DUMPS / "charts.json"
    if not path.exists():
        pytest.skip("dumps/ is gitignored; run build_charts.py to produce charts.json")

    library = load_charts(path)
    ranking = rank_on_chart(_teams(cards), library.at()[0], cfg)
    assert len(ranking.teams) == 3
    assert all(row.expected_score > 0 for row in ranking.teams)
    assert all(len(row.member_order) == 5 for row in ranking.teams)
    assert format_song_rows(ranking)[0] == " ".join(SONG_TABLE_COLUMNS)


# ==========================================================================
# The public per-song figure the web detail routes use
# ==========================================================================


def test_score_team_on_chart_is_the_rankers_own_number(cards, cfg):
    """The detail routes (compare / timeline / row detail) must show the number
    the row was ranked by: Overall Power when `song_absolute_deck_power`, the
    modelled stat pool otherwise — never a bare `score_song_detailed` call,
    which is always the modelled pool."""
    import dataclasses

    from hbuilder.domain.song_rank import score_team_on_chart

    chart = _chart()
    team = _teams(cards, 1)[0]
    ranked = rank_on_chart([team], chart, cfg).teams[0]
    got = score_team_on_chart(team, chart, cfg)
    assert got.total_score == ranked.expected_score
    assert got.member_order == ranked.member_order
    assert got.deck_power == overall_power_for(tuple(team.members), team.leader, cfg)

    modelled_cfg = dataclasses.replace(cfg, song_absolute_deck_power=False)
    bare = score_song_detailed(tuple(team.members), (team.leader,), chart, modelled_cfg)[0]
    assert score_team_on_chart(team, chart, modelled_cfg).total_score == bare.total_score


def test_score_team_orders_is_every_order_of_the_same_scorer(cards, cfg):
    """`score_team_orders` (Highlight's one order for a whole pool): the 120
    slot orders' totals from the scorer's reference walk. Its best is the ranker's own
    number and order, bit for bit, and asking for the totals changes no score."""
    from hbuilder.domain.song_rank import score_team_on_chart, score_team_orders

    charts = [_chart(), _chart("m0002", scale=2)]
    path = _DUMPS / "charts.json"
    if path.exists():
        charts += list(load_charts(path).at()[:3])
    for chart in charts:
        for team in _teams(cards):
            got = score_team_on_chart(team, chart, cfg)
            totals = score_team_orders(team, chart, cfg)
            assert len(totals) == 120
            best = max(totals.values())
            assert best == got.total_score
            assert next(o for o, v in totals.items() if v == best) == tuple(got.member_order)
            plain = score_song_detailed(tuple(team.members), (team.leader,), chart, cfg)[0]
            seen: dict = {}
            recorded = score_song_detailed(tuple(team.members), (team.leader,), chart, cfg,
                                           order_totals=seen)[0]
            assert recorded == plain and len(seen) == 120
