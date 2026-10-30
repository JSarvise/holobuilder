"""The parallel across-songs re-rank must return `rank_across_charts`'s answer
EXACTLY — same means, same wins, same per-chart scores, same slot orders, same
tie-breaks, same order.

The recurring defect in this codebase — four instances now — is a value
computed and then dropped, where the absent value is ALSO correct for the
common case, so every test agrees and nothing looks wrong. Moving
`member_order` and `deck_power` across a process boundary is exactly that
shape: a worker that shipped empty orders home would still produce correct
MEANS, the table would still render, and only a player asking "in what order?"
would ever notice. So these tests do not compare summaries — they compare the
full `RankedOnSongs` record, and then additionally assert the fields that
could be empty-and-plausible are non-trivial on the PARALLEL result itself,
so an equality of two accidentally-empty sides cannot pass.

These tests spawn real processes, as `test_parallel_exact.py` does and for the
same reason: Windows `spawn` is the pickle round trip of `Team`, `SongChart`
and `ScoringConfig` that no in-process test can exercise.
"""

from __future__ import annotations

import pytest
from conftest import FIXTURE_CARDS_PATH, make_scoring_config

from hbuilder.domain.loader import load_cards
from hbuilder.domain.song import ChartNote, SongChart
from hbuilder.domain.song_rank import rank_across_charts
from hbuilder.domain.team import Team
from hbuilder.solver import parallel_songs
from hbuilder.solver.parallel_songs import rank_across_charts_parallel


@pytest.fixture(scope="module")
def cards():
    return load_cards(FIXTURE_CARDS_PATH)


@pytest.fixture(scope="module")
def cfg():
    return make_scoring_config()


def _chart(music_id="m0001", scale=1, note_count=39):
    """A tiny chart; `scale` and `note_count` vary the geometry so two charts
    produce genuinely different scores and the merge has something to get
    wrong."""
    return SongChart(
        music_id=music_id,
        difficulty="EXPERT",
        difficulty_level=26,
        live_score_coefficient_permil=4,
        notes=tuple(
            ChartNote(time=float(t), perfect_permil=1000 * scale)
            for t in range(1, note_count + 1)
        ),
        special_cues=(5.0, 10.0, 15.0, 20.0, 25.0),
    )


def _teams(cards, n=3):
    return [
        Team(leader=cards[i], members=tuple(c for j, c in enumerate(cards) if j != i))
        for i in range(n)
    ]


def _charts():
    """Five charts with distinct ids and geometries — more charts than the
    2-3 workers under test, so dynamic dispatch actually interleaves and a
    completion-order merge would have somewhere to go wrong."""
    return [
        _chart("m0001"),
        _chart("m0002", scale=2),
        _chart("m0003", scale=3, note_count=25),
        _chart("m0004", note_count=50),
        _chart("m0005", scale=2, note_count=31),
    ]


# ---------------------------------------------------------------------------
# The claim itself: bit-identical to serial, with real processes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("workers", [2, 3])
def test_the_parallel_ranking_is_bit_identical_to_the_serial_one(
    cards, cfg, workers
):
    teams = _teams(cards)
    charts = _charts()

    serial = rank_across_charts(teams, charts, cfg)
    notices: list[str] = []
    parallel = rank_across_charts_parallel(
        teams, charts, cfg, workers=workers, on_fallback=notices.append
    )

    # The fallback returns the identical answer BY DESIGN, which means it
    # could also silently absorb a broken pool and let this whole file pass
    # without ever spawning a process. So the identity claim is only made
    # about a run that verifiably stayed parallel.
    assert notices == []

    # Frozen-dataclass equality covers the whole record: means, wins, ranks,
    # tie-breaks, and every per-chart score, slot order and deck power.
    assert parallel == serial

    # And the boundary-crossing fields are non-trivial ON THE PARALLEL SIDE,
    # so `parallel == serial` can never be two empty answers agreeing.
    assert parallel.teams
    for row in parallel.teams:
        assert len(row.per_chart) == len(charts)
        for cell in row.per_chart:
            assert len(cell.member_order) == 5
            assert cell.deck_power > 0
            assert cell.expected_score > 0

    # Exact float equality, spelled out for the reader wondering whether
    # something rounded on the way through a pickle.
    assert [row.expected_score for row in parallel.teams] == [
        row.expected_score for row in serial.teams
    ]


def test_two_worker_counts_agree_with_each_other_not_just_with_serial(cards, cfg):
    """Chart rows complete in a nondeterministic order under `imap_unordered`.
    If the merge were order-sensitive, the answer would move between worker
    counts while each run still looked self-consistent."""
    teams = _teams(cards)
    charts = _charts()

    assert rank_across_charts_parallel(
        teams, charts, cfg, workers=2
    ) == rank_across_charts_parallel(teams, charts, cfg, workers=3)


def test_one_worker_takes_the_serial_path_and_returns_the_serial_answer(cards, cfg):
    teams = _teams(cards)
    charts = _charts()

    assert rank_across_charts_parallel(
        teams, charts, cfg, workers=1
    ) == rank_across_charts(teams, charts, cfg)


def test_filtered_from_survives_the_parallel_path(cards, cfg):
    """It is the stage-1 K the report rests on; a parallel run that lost it
    would print a basis line describing a different filter."""
    ranking = rank_across_charts_parallel(
        _teams(cards), _charts(), cfg, workers=2, filtered_from=150
    )
    assert ranking.filtered_from == 150


# ---------------------------------------------------------------------------
# The degraded paths: small inputs, spawn failure, bad arguments
# ---------------------------------------------------------------------------


def test_a_single_chart_is_not_sharded_and_still_answers(cards, cfg):
    teams = _teams(cards)
    charts = [_chart("m0001")]

    assert rank_across_charts_parallel(
        teams, charts, cfg, workers=4
    ) == rank_across_charts(teams, charts, cfg)


def test_no_charts_is_an_empty_ranking_not_a_crash(cards, cfg):
    ranking = rank_across_charts_parallel(_teams(cards), [], cfg, workers=4)
    assert ranking.teams == () and ranking.chart_count == 0


def test_a_spawn_failure_falls_back_to_the_identical_serial_answer(
    cards, cfg, monkeypatch
):
    """Requirement: a machine where the pool cannot stand up must still get
    the same answer. The failure is injected at `get_context`, before any
    scoring, so everything the fallback returns was computed serially."""
    teams = _teams(cards)
    charts = _charts()
    notices: list[str] = []

    def refuse(method):
        raise OSError("spawn is not available on this machine")

    monkeypatch.setattr(
        parallel_songs.multiprocessing, "get_context", refuse
    )
    ranking = rank_across_charts_parallel(
        teams, charts, cfg, workers=4, on_fallback=notices.append
    )

    assert ranking == rank_across_charts(teams, charts, cfg)
    assert len(notices) == 1
    assert "single process" in notices[0]


def test_zero_or_negative_workers_is_refused_by_name(cards, cfg):
    with pytest.raises(ValueError, match="workers must be >= 1"):
        rank_across_charts_parallel(_teams(cards), _charts(), cfg, workers=0)


def test_the_progress_callback_is_monotone_and_reaches_the_serial_total(cards, cfg):
    """Documented as CHART cadence with the serial path's endpoints: monotone,
    same total. A caller wiring it to a bar labelled 'evaluations' stays
    truthful about the endpoints even though the steps are coarser."""
    teams = _teams(cards)
    charts = _charts()
    seen: list[tuple[int, int]] = []

    rank_across_charts_parallel(
        teams, charts, cfg, workers=2,
        progress=lambda done, total: seen.append((done, total)),
    )

    total = len(charts) * len(teams)
    assert [done for done, _ in seen] == [
        (index + 1) * len(teams) for index in range(len(charts))
    ]
    assert all(t == total for _, t in seen)
    assert seen[-1][0] == total


# ---------------------------------------------------------------------------
# Per-team boards: the develop-across re-rank scores every team against ITS OWN
# developed board. `boards[i]` applies to `teams[i]` and nothing else, and the
# parallel answer must still be bit-identical to the serial one.
# ---------------------------------------------------------------------------


def _board(member_name):
    from hbuilder.domain.board import board_from_mapping
    return board_from_mapping({"characters": {member_name: [{
        "node": "B-001",
        "effects": ["skill_tree_effect-sense_up-500-skill_tree_effect_target"
                    "-skill_tree_character-always"],
    }]}})


def test_a_board_applies_to_its_own_team_index_and_no_other(cards, cfg):
    teams = _teams(cards)
    charts = _charts()
    board = _board(teams[1].members[0].member)
    boards = [None, board, None]

    base = rank_across_charts(teams, charts, cfg)
    with_boards = rank_across_charts(teams, charts, cfg, boards=boards)

    def cells(ranking, team):
        row = next(r for r in ranking.teams if r.team == team)
        return [c.expected_score for c in row.per_chart]

    # only team 1 was scored against the board; the other two are bit-identical.
    assert cells(with_boards, teams[0]) == cells(base, teams[0])
    assert cells(with_boards, teams[2]) == cells(base, teams[2])
    assert cells(with_boards, teams[1]) != cells(base, teams[1])


def test_boards_survive_the_parallel_path_bit_identically(cards, cfg):
    teams = _teams(cards)
    charts = _charts()
    boards = [_board(teams[0].members[0].member), None,
              _board(teams[2].members[1].member)]

    serial = rank_across_charts(teams, charts, cfg, boards=boards)
    notices: list[str] = []
    parallel = rank_across_charts_parallel(
        teams, charts, cfg, boards=boards, workers=2,
        on_fallback=notices.append)
    assert notices == []
    assert parallel == serial


def test_misaligned_boards_are_refused_by_name(cards, cfg):
    teams = _teams(cards)
    with pytest.raises(ValueError, match="boards"):
        rank_across_charts(teams, _charts(), cfg, boards=[None])
