"""Rank teams by what they would actually SCORE, on one chart or across all of them.

`scoring.py` ranks by a song-agnostic quantity and is right to (`CalcScore`
divides by the chart-wide `NotesPerfectCoefficientSum`, so length genuinely
cancels). `song.py` scores one team on one chart. This joins them, and the join
is a **two-stage** one, because the cost forbids anything else.

## Why two stages, and what it costs

Scoring costs about **12 ms per (team, chart)**. One team over every EXPERT
chart takes about 2 s; 200 teams take minutes. Full enumeration is `C(N,6) x 6`
arrangements — millions — so scoring every candidate on every chart is not
merely slow, it is impossible. So:

1. the song-agnostic solver enumerates everything and keeps the top **K**;
2. those K are scored on the real chart(s) and re-ranked by expected score.

⚠ **Stage 1 is a FILTER, and that is this module's one real weakness.** A team
that would win on real charts but does not make the abstract top K is never
seen, and no amount of care in stage 2 recovers it. `pool_size` is therefore an
argument and not a constant, and `RankedOnSongs.filtered_from` records what it
was, so a report can say what the answer rested on. The check is cheap and
belongs in a report rather than in a docstring: re-run with a larger K and see
whether the winner moves.

## "Expected" is exact here, not a sample

`score_song` takes the **exact expectation** over the ACTIVE activation roll
(`song._ratio_at`) rather than sampling it. The SPECIAL term is deterministic —
the chart says when each cue fires. So the number ranked on is an expectation
with no Monte-Carlo error, and the only stochastic thing left in a real run is
judgement, which the full-perfect premise removes by construction.

## What the aggregate across songs means

`mean_score` is the plain mean of the expected score over the charts, i.e.
*"play each of these songs once, how much do you score in total"* divided by the
count. It is deliberately NOT normalised per song: normalising would answer
"which team is most consistently above average", which is a different question
and hides that some songs are worth far more than others.

Because a mean can hide a team that wins overall while losing most songs,
`wins` counts the charts on which each team is the best of the candidates, and a
report should show both. When the top team by mean is not also the top by wins,
that is a real finding about the roster, not a defect.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Iterable, Sequence

from hbuilder.domain.card import Card
from hbuilder.domain.scoring_config import ScoringConfig
from hbuilder.domain.song import (
    SongChart,
    SongScore,
    overall_power_for,
    score_song_detailed,
)
from hbuilder.domain.team import Team

__all__ = [
    "TeamSongScore",
    "RankedOnSong",
    "RankedOnSongs",
    "rank_on_chart",
    "rank_across_charts",
    "score_teams_on_chart",
    "score_team_on_chart",
    "score_team_ceiling",
    "assemble_across_charts",
]


@dataclass(frozen=True, slots=True)
class TeamSongScore:
    """One team's expected score on one chart, with the slot order that got it.

    `member_order` is never dropped: a tool that
    recommends a team without its slot order hands the player the median case
    instead of the best one — and the SPECIAL cue assignment binds on every
    team, so there is always an order worth reporting."""

    music_id: str
    expected_score: float
    member_order: tuple[str, ...]
    deck_power: float


@dataclass(frozen=True, slots=True)
class RankedOnSong:
    """One team's place in a per-song ranking."""

    team: Team
    leader_id: str
    member_ids: tuple[str, ...]
    expected_score: float
    member_order: tuple[str, ...]
    per_chart: tuple[TeamSongScore, ...] = ()
    wins: int = 0

    @property
    def mean_score(self) -> float:
        return self.expected_score


@dataclass(frozen=True, slots=True)
class RankedOnSongs:
    """A whole ranking, plus what it rested on.

    `filtered_from` is the K of stage 1 and is carried, not implied: the answer
    is only as good as that filter and a reader must be able to see it without
    reading the call site."""

    teams: tuple[RankedOnSong, ...]
    charts: tuple[str, ...]
    filtered_from: int
    # Which difficulty the charts were scored at, carried so the report can
    # STATE it rather than assume it: a run can score any difficulty, and a
    # header saying EXPERT over an EASY run would report one number and
    # compute another. Defaults to EXPERT.
    difficulty: str = "EXPERT"
    # Which PREMISE the scores are on: manual full-perfect, or an AUTO run.
    # Carried for the same reason as `difficulty` — the report states it rather
    # than assuming it, and an AUTO ranking labelled "full-perfect premise"
    # would be one number computed and another reported.
    auto: bool = False

    @property
    def chart_count(self) -> int:
        return len(self.charts)


def _score_one(
    team: Team, chart: SongChart, cfg: ScoringConfig
) -> SongScore:
    """`score_song_detailed` for a team whose leader is already fixed.

    A `Team` is (leader, members); the per-song scorer takes members and a
    tuple of candidate leaders, so this passes the one leader and takes the one
    answer. Kept in a function so the leader convention is written once — and
    it is also where `cfg.song_absolute_deck_power` is read, because EVERY
    per-song ranking path (one chart, across charts, serial, parallel) funnels
    through here. Putting the choice in one place beats threading a `deck_power`
    argument through four signatures that could drift apart.

    ⚠ The Overall Power is recomputed per (team, CHART) rather than cached per
    team, and it is chart-invariant, so an across-charts run pays it 187 times
    instead of once. `overall_power_for` costs about 0.21 ms against
    `score_song_detailed`'s 2.5 ms, i.e. **+8 %** — about 2 s on a
    187-chart x 50-team run. That is the price of correctness in the
    per-team-board case (`score_teams_on_chart` replaces the board per team, and
    Overall Power carries the board's power half, so a cache keyed by team alone
    would be WRONG there). Cache by (team, board) if it ever matters."""
    deck_power = (
        overall_power_for(tuple(team.members), team.leader, cfg)
        if cfg.song_absolute_deck_power
        else None
    )
    scores = score_song_detailed(
        tuple(team.members), (team.leader,), chart, cfg, deck_power=deck_power,
        # The AUTO judgement column. `cfg.song_auto`'s own comment records the
        # half of the premise that is easy to forget (combo).
        auto=cfg.song_auto,
    )
    return scores[0]


def score_team_on_chart(
    team: Team, chart: SongChart, cfg: ScoringConfig
) -> SongScore:
    """The ONE per-song figure for a (leader, members) team on `cfg`'s board.

    Public name for `_score_one`, for callers outside the ranking paths (the web
    detail routes). It reads `cfg.song_absolute_deck_power` and `cfg.song_auto`
    exactly as every ranking path does, so a detail view asking "what does this
    row score" gets the number the row was ranked by — not a sibling computed on
    the modelled stat pool. Calling `score_song_detailed` bare instead puts the
    same team on the same board about 1 % below the ranked row."""
    return _score_one(team, chart, cfg)


def score_team_orders(
    team: Team, chart: SongChart, cfg: ScoringConfig
) -> dict[tuple[str, ...], float]:
    """The team's total on `chart` at EVERY slot order, `{member_order: total}` -- the
    same call `score_team_on_chart` makes (`_score_one`'s deck power and AUTO column),
    its 120 orders recorded by the scorer's reference walk (`score_song_detailed`,
    `order_totals`). Its max is `score_team_on_chart`'s total. ~100x the cost of one
    score: for choosing ONE order across several songs, not for a search."""
    deck_power = (
        overall_power_for(tuple(team.members), team.leader, cfg)
        if cfg.song_absolute_deck_power
        else None
    )
    got: dict = {}
    score_song_detailed(tuple(team.members), (team.leader,), chart, cfg,
                        deck_power=deck_power, auto=cfg.song_auto, order_totals=got)
    return {order: total for (_leader, order), total in got.items()}


def score_team_ceiling(
    team: Team, chart: SongChart, cfg: ScoringConfig, member_order: Sequence[str]
) -> float:
    """The run score if every ACTIVE fires, AT `member_order` — the order the
    row plays, so this is the top of that row's own run-score law.

    `score_team_on_chart` with `active_all_fire` alone is not that: it picks the
    slot order best for the all-fire case, which can differ from the row's
    (SPECIAL cues land on other members) and so report a ceiling the deck as
    shown cannot reach. When the free order is the row's, its total is returned as
    is; otherwise the all-fire law at the row's order (one state, linear in
    the notes) gives the figure — the same terms, `verified` against the scorer."""
    from hbuilder.domain.song_distribution import run_score_distribution  # noqa: PLC0415

    cfg_max = replace(cfg, active_all_fire=True)
    free = _score_one(team, chart, cfg_max)
    if tuple(free.member_order) == tuple(member_order):
        return free.total_score
    law = run_score_distribution(tuple(team.members), team.leader, chart, cfg_max,
                                 member_order, free.deck_power)
    return law["expected_total"]


def rank_on_chart(
    teams: Sequence[Team],
    chart: SongChart,
    cfg: ScoringConfig,
    *,
    filtered_from: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> RankedOnSongs:
    """Rank `teams` by expected score on ONE chart, best first.

    Ties break on `(leader_id, member_ids)` so the order is a pure function of
    the input — the same determinism anchor `canonical_pool` gives the solver.
    Without it two runs over the same pool could disagree on rank 1."""
    rows: list[RankedOnSong] = []
    for index, team in enumerate(teams):
        score = _score_one(team, chart, cfg)
        member_ids = tuple(sorted(card.id for card in team.members))
        rows.append(
            RankedOnSong(
                team=team,
                leader_id=team.leader.id,
                member_ids=member_ids,
                expected_score=score.total_score,
                member_order=score.member_order,
                per_chart=(
                    TeamSongScore(
                        music_id=chart.music_id,
                        expected_score=score.total_score,
                        member_order=score.member_order,
                        deck_power=score.deck_power,
                    ),
                ),
            )
        )
        if progress is not None:
            progress(index + 1, len(teams))

    rows.sort(key=lambda row: (-row.expected_score, row.leader_id, row.member_ids))
    return RankedOnSongs(
        teams=tuple(rows),
        charts=(chart.music_id,),
        difficulty=chart.difficulty.upper(),
        auto=cfg.song_auto,
        filtered_from=len(teams) if filtered_from is None else filtered_from,
    )


def _check_boards(teams: Sequence[Team], boards) -> None:
    """`boards`, when given, must align 1:1 with `teams` — `boards[i]` is the
    board TEAM i is scored against. A silent zip would score teams on the wrong
    board the first time a caller filters one list and not the other."""
    if boards is not None and len(boards) != len(teams):
        raise ValueError(
            f"boards must align with teams: {len(teams)} team(s) but "
            f"{len(boards)} board(s)"
        )


def score_teams_on_chart(
    teams: Sequence[Team],
    chart: SongChart,
    cfg: ScoringConfig,
    *,
    boards: Sequence | None = None,
    progress: Callable[[int], None] | None = None,
) -> tuple[TeamSongScore, ...]:
    """Every team's expected score on ONE chart, **in team order**.

    This is the unit of work `rank_across_charts` iterates and the parallel
    path (`solver/parallel_songs.py`) ships to a worker. Both call THIS
    function, so there is one scoring path, not a serial one and a parallel
    re-implementation that could drift — the same single-enumeration argument
    `solver/parallel.py` makes for the exact search.

    The row order is the caller's team order, never a ranking: `wins` and the
    tie-breaks downstream are defined over team indices, so reordering here
    would silently change who wins a tied chart.

    `boards`, when given, aligns 1:1 with `teams`: team `i` is scored with
    `cfg`'s board replaced by `boards[i]` (`None` = no board for that team) —
    the develop-across case, where every team carries its OWN developed board.

    `progress`, when given, is called with the number of teams scored so far
    on this chart (1-based)."""
    _check_boards(teams, boards)
    rows: list[TeamSongScore] = []
    for index, team in enumerate(teams):
        team_cfg = cfg if boards is None else replace(cfg, board=boards[index])
        score = _score_one(team, chart, team_cfg)
        rows.append(
            TeamSongScore(
                music_id=chart.music_id,
                expected_score=score.total_score,
                member_order=score.member_order,
                deck_power=score.deck_power,
            )
        )
        if progress is not None:
            progress(index + 1)
    return tuple(rows)


def assemble_across_charts(
    teams: Sequence[Team],
    charts: Sequence[SongChart],
    rows: Sequence[Sequence[TeamSongScore]],
    *,
    filtered_from: int | None = None,
    auto: bool = False,
) -> RankedOnSongs:
    """The across-charts ranking, assembled from per-chart score rows.

    `rows[c][t]` is team `t`'s score on chart `c` — `score_teams_on_chart`'s
    output, one row per chart, **in chart order**. Split out from
    `rank_across_charts` so the parallel path can reuse the aggregation
    verbatim: means are summed in chart order and the per-chart winner is the
    FIRST index at the maximum (strict `>`), so the result is a pure function
    of the matrix and cannot depend on which worker finished first.

    `wins` counts the charts on which each team beats every other candidate.
    It is reported alongside the mean because a mean can hide a team that wins
    overall while losing most songs — when the two disagree, that is a finding
    about the roster and not a defect."""
    if len(rows) != len(charts):
        raise ValueError(
            f"assemble_across_charts: {len(charts)} chart(s) but {len(rows)} "
            f"score row(s); every chart must have exactly one row"
        )
    if not charts:
        return RankedOnSongs(
            teams=(), charts=(),
            filtered_from=len(teams) if filtered_from is None else filtered_from,
        )
    for chart, row in zip(charts, rows):
        if len(row) != len(teams):
            raise ValueError(
                f"assemble_across_charts: chart {chart.music_id} carries "
                f"{len(row)} score(s) for {len(teams)} team(s)"
            )

    wins = [0] * len(teams)
    for row in rows:
        best_index, best_score = -1, float("-inf")
        for index, cell in enumerate(row):
            if cell.expected_score > best_score:
                best_index, best_score = index, cell.expected_score
        if best_index >= 0:
            wins[best_index] += 1

    ranked: list[RankedOnSong] = []
    for index, team in enumerate(teams):
        scores = [row[index] for row in rows]
        mean = sum(cell.expected_score for cell in scores) / len(scores)
        ranked.append(
            RankedOnSong(
                team=team,
                leader_id=team.leader.id,
                member_ids=tuple(sorted(card.id for card in team.members)),
                expected_score=mean,
                # The order that wins the FIRST chart is not the order that wins
                # them all, so an across-songs row deliberately carries no single
                # slot order: `per_chart` holds one per chart and the caller must
                # say which song it is quoting. Reporting one here would read as
                # "play this order on everything", which is false.
                member_order=(),
                per_chart=tuple(scores),
                wins=wins[index],
            )
        )

    ranked.sort(key=lambda row: (-row.expected_score, row.leader_id, row.member_ids))
    return RankedOnSongs(
        teams=tuple(ranked),
        charts=tuple(chart.music_id for chart in charts),
        difficulty=charts[0].difficulty.upper() if charts else "EXPERT",
        auto=auto,
        filtered_from=len(teams) if filtered_from is None else filtered_from,
    )


def rank_across_charts(
    teams: Sequence[Team],
    charts: Iterable[SongChart],
    cfg: ScoringConfig,
    *,
    boards: Sequence | None = None,
    filtered_from: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> RankedOnSongs:
    """Rank `teams` by MEAN expected score across `charts`, best first.

    A composition of the two pieces above — `score_teams_on_chart` per chart,
    then `assemble_across_charts` — and deliberately nothing else, so that the
    serial path and `solver/parallel_songs.py` differ only in WHO scores each
    chart, never in what is scored or how it is merged. `boards`, when given,
    aligns 1:1 with `teams` (see `score_teams_on_chart`)."""
    _check_boards(teams, boards)
    chart_list = tuple(charts)
    if not chart_list:
        return RankedOnSongs(
            teams=(), charts=(),
            filtered_from=len(teams) if filtered_from is None else filtered_from,
        )

    total = len(chart_list) * len(teams)
    rows: list[tuple[TeamSongScore, ...]] = []
    done = 0
    for chart in chart_list:
        per_team_progress = (
            None
            if progress is None
            else lambda scored, base=done: progress(base + scored, total)
        )
        rows.append(
            score_teams_on_chart(teams, chart, cfg, boards=boards,
                                 progress=per_team_progress)
        )
        done += len(teams)

    return assemble_across_charts(
        teams, chart_list, rows, filtered_from=filtered_from, auto=cfg.song_auto
    )
