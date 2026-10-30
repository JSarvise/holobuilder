"""`--across-songs` stage 2 across processes — bit-identical to the serial path.

Over 90 % of an `--across-songs` run is
stage 2 — re-scoring 150 teams x 181 Expert charts = 27,150 independent
`score_song_detailed` calls at ~3.7 ms each, sharing no state. This module is
`solver/parallel.py`'s pattern applied to that workload: same `spawn` context,
same initializer-carries-the-context shape, same dynamically dispatched small
tasks, same deterministic merge.

## Why the unit of work is a CHART, not a (team, chart) cell

A task must amortise its own IPC. A single cell is ~3.7 ms — 27,150 pickle
round trips of a `TeamSongScore` would spend a real fraction of the win on
transport. One chart x 150 teams is ~0.55 s of scoring per task, 181 tasks:
large enough that dispatch cost vanishes, numerous enough that the pool
load-balances across workers (`imap_unordered(..., chunksize=1)`, exactly as
`parallel.py` does and for the same reason — the largest task is a small
fraction of the total, so no worker is left holding the bag).

## Why the merge is exact, not approximately right

Workers compute; they never aggregate. Each task returns
`(chart_index, score_teams_on_chart(...))` — the SAME function the serial
`rank_across_charts` calls per chart, so there is one scoring path, not two.
The parent stores each row at its chart index and, only when every row is
home, hands the matrix to `assemble_across_charts` — the SAME aggregation the
serial path runs. Completion order therefore cannot reach the answer:

- **means** are summed in chart order, fixed by the index, not by arrival;
- **wins** go to the first team index at each chart's maximum (strict `>`),
  a per-chart pure function of that chart's row;
- **sorting and tie-breaks** happen once, in `assemble_across_charts`.

Each cell is computed by the same interpreter version running the same code on
the same machine, so its float is the same float; the merge adds nothing of
its own. `tests/test_parallel_songs.py` asserts full-record equality against
the serial path — every score, slot order, deck power, win count and rank —
because the recurring defect in this codebase is a value computed and then
dropped somewhere a test never looks. Moving `member_order` and `deck_power`
across a process boundary is exactly that shape, so the test fails if either
arrives empty.

## The fallback is the serial path, not a degraded answer

Any failure to stand the pool up (or a worker dying) lands in
`rank_across_charts` — same inputs, same answer, one process. A deterministic
scoring bug is NOT hidden by this: the serial re-run hits the same exception
and raises it exactly as a no-pool run would have. What the fallback rescues
is spawn-environment failures, which have nothing to say about the answer.
"""

from __future__ import annotations

import multiprocessing
from typing import Callable, Iterable, Sequence

from hbuilder.domain.scoring_config import ScoringConfig
from hbuilder.domain.song import SongChart
from hbuilder.domain.song_rank import (
    RankedOnSongs,
    TeamSongScore,
    assemble_across_charts,
    rank_across_charts,
    score_teams_on_chart,
)
from hbuilder.domain.team import Team
from hbuilder.solver.parallel import default_workers

__all__ = ["rank_across_charts_parallel"]

# Per-worker state, set once by the pool initializer — the same shape as
# `parallel.py`: `spawn` gives each worker a fresh interpreter, and paying the
# teams + charts + config pickle once per worker beats paying it once per
# chart task.
_TEAMS: tuple[Team, ...] = ()
_CHARTS: tuple[SongChart, ...] = ()
_CFG: ScoringConfig | None = None
_BOARDS: tuple | None = None


def _init_worker(
    teams: tuple[Team, ...],
    charts: tuple[SongChart, ...],
    cfg: ScoringConfig,
    boards: tuple | None = None,
) -> None:
    global _TEAMS, _CHARTS, _CFG, _BOARDS
    _TEAMS = teams
    _CHARTS = charts
    _CFG = cfg
    _BOARDS = boards


def _score_chart(index: int) -> tuple[int, tuple[TeamSongScore, ...]]:
    """One chart's row of the score matrix, tagged with its chart index so the
    parent can file it by position rather than by arrival."""
    assert _CFG is not None  # set by _init_worker before any task is dispatched
    return index, score_teams_on_chart(
        _TEAMS, _CHARTS[index], _CFG, boards=_BOARDS
    )


def rank_across_charts_parallel(
    teams: Sequence[Team],
    charts: Iterable[SongChart],
    cfg: ScoringConfig,
    *,
    boards: Sequence | None = None,
    filtered_from: int | None = None,
    workers: int | None = None,
    progress: Callable[[int, int], None] | None = None,
    on_fallback: Callable[[str], None] | None = None,
) -> RankedOnSongs:
    """`rank_across_charts`'s answer, computed across processes.

    `workers=1` runs `rank_across_charts` directly rather than spinning up a
    pool of one — same answer, and it keeps a single-worker run honest as a
    baseline in a speedup table instead of measuring the pool machinery
    against itself. So does any input too small to shard.

    `progress`, when given, is called with `(evaluations_done, total)` like
    the serial path's — but at CHART granularity: once per completed chart,
    with `completed_charts * len(teams)`. It is monotone and ends at the same
    total; only the cadence differs, because the serial per-evaluation cadence
    cannot cross a process boundary without shipping a message per cell.

    `on_fallback`, when given, is told (as one printable line) why a run fell
    back to the single-process path. The fallback itself is unconditional on
    failure: the answer must not depend on whether the pool stood up.

    `boards`, when given, aligns 1:1 with `teams` — team `i` is scored with its
    OWN board (`score_teams_on_chart`), the develop-across case. It rides to
    each worker once, in the initializer, exactly as the teams do."""
    team_list = tuple(teams)
    board_list = None if boards is None else tuple(boards)
    chart_list = tuple(charts)
    if workers is None:
        workers = default_workers()
    if workers < 1:
        raise ValueError(f"workers must be >= 1, got {workers}")
    if workers == 1 or len(chart_list) < 2 or not team_list:
        return rank_across_charts(
            team_list, chart_list, cfg, boards=board_list,
            filtered_from=filtered_from, progress=progress,
        )

    total = len(chart_list) * len(team_list)
    rows: list[tuple[TeamSongScore, ...] | None] = [None] * len(chart_list)
    try:
        context = multiprocessing.get_context("spawn")
        with context.Pool(
            processes=min(workers, len(chart_list)),
            initializer=_init_worker,
            initargs=(team_list, chart_list, cfg, board_list),
        ) as pool:
            completed = 0
            for index, row in pool.imap_unordered(
                _score_chart, range(len(chart_list)), chunksize=1
            ):
                rows[index] = row
                completed += 1
                if progress is not None:
                    progress(completed * len(team_list), total)
    except Exception as exc:  # spawn/pickle/worker failure — never a new answer
        if on_fallback is not None:
            on_fallback(
                f"parallel song scoring failed ({type(exc).__name__}: {exc}); "
                f"rescoring in a single process"
            )
        return rank_across_charts(
            team_list, chart_list, cfg, boards=board_list,
            filtered_from=filtered_from, progress=progress,
        )

    complete = [row for row in rows if row is not None]
    if len(complete) != len(chart_list):
        # A pool that returns without a row for every chart delivered a
        # partial matrix; aggregating it would be a silently wrong mean.
        if on_fallback is not None:
            on_fallback(
                f"parallel song scoring returned {len(complete)} of "
                f"{len(chart_list)} chart rows; rescoring in a single process"
            )
        return rank_across_charts(
            team_list, chart_list, cfg, boards=board_list,
            filtered_from=filtered_from, progress=progress,
        )

    return assemble_across_charts(
        team_list, chart_list, complete, filtered_from=filtered_from,
        auto=cfg.song_auto,
    )
