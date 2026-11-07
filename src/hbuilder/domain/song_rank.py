"""Score one team on one chart: the per-song figure every web route reports.

`score_team_on_chart` is the ONE per-song number (the same call the ranking
leaf makes: `cfg.song_absolute_deck_power` and `cfg.song_auto` are read here),
`score_team_orders` its value at every slot order, and `score_team_ceiling` the
run score if every ACTIVE fires at a given order. "Expected" is exact: the
ACTIVE roll is an exact expectation (`song._ratio_at`), and the SPECIAL term is
deterministic.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from hbuilder.domain.scoring_config import ScoringConfig
from hbuilder.domain.song import (
    SongChart,
    SongScore,
    overall_power_for,
    score_song_detailed,
)
from hbuilder.domain.team import Team

__all__ = [
    "score_team_on_chart",
    "score_team_orders",
    "score_team_ceiling",
]


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
