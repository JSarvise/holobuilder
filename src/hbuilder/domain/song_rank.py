"""Score one team on one chart: the per-song figure every web route reports.

`score_team_on_chart` is the per-song number (the same call the ranking leaf makes),
`score_team_orders` its value at every slot order, `score_team_notes` its note-by-note
breakdown and `score_team_ceiling` the run score if every ACTIVE fires at a given order.
"Expected" is exact: the ACTIVE roll is an exact expectation (`song._ratio_at`) and the
SPECIAL term is deterministic.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

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
    "score_team_notes",
    "score_team_orders",
    "score_team_ceiling",
]


def _score_one(team: Team, chart: SongChart, cfg: ScoringConfig) -> SongScore:
    """`score_song_detailed` for a team whose leader is already fixed.

    Every per-song ranking path (one chart, across charts, serial, parallel) funnels
    through here, so this is the one place that reads `cfg.song_absolute_deck_power`.

    The Overall Power is recomputed per (team, chart) although it does not depend on the
    chart (about +8 % per score): `score_teams_on_chart` replaces the board per team and
    Overall Power includes the board's power, so a cache keyed by team alone would be
    wrong. Cache by (team, board) if it ever matters."""
    deck_power = (
        overall_power_for(tuple(team.members), team.leader, cfg) if cfg.song_absolute_deck_power else None
    )
    scores = score_song_detailed(
        tuple(team.members),
        (team.leader,),
        chart,
        cfg,
        deck_power=deck_power,
        # the AUTO judgement column (`cfg.song_auto` explains the combo half)
        auto=cfg.song_auto,
    )
    return scores[0]


def score_team_on_chart(team: Team, chart: SongChart, cfg: ScoringConfig) -> SongScore:
    """The per-song figure for a (leader, members) team on `cfg`'s board.

    Public name for `_score_one`, for callers outside the ranking paths (the web detail
    routes), so a detail view gets the number the row was ranked by. Calling
    `score_song_detailed` bare (on the modelled stat pool) scores about 1 % lower."""
    return _score_one(team, chart, cfg)


def score_team_notes(team: Team, chart: SongChart, cfg: ScoringConfig) -> tuple[SongScore, list]:
    """`score_team_on_chart` and its note-by-note breakdown at the best slot order
    (`song.NoteRow`s, `score_song_detailed(note_rows=)`): the same deck power and AUTO
    premise, so the rows' `expected_value`s add up to the row's `total_score` exactly. It
    walks the 120 orders in Python: for an export, not a search."""
    deck_power = (
        overall_power_for(tuple(team.members), team.leader, cfg) if cfg.song_absolute_deck_power else None
    )
    rows: dict = {}
    (score,) = score_song_detailed(
        tuple(team.members),
        (team.leader,),
        chart,
        cfg,
        deck_power=deck_power,
        auto=cfg.song_auto,
        note_rows=rows,
    )
    return score, rows[team.leader.id]


def score_team_orders(team: Team, chart: SongChart, cfg: ScoringConfig) -> dict[tuple[str, ...], float]:
    """The team's total on `chart` at every slot order, `{member_order: total}`, on the
    same terms as `score_team_on_chart` (whose total is the max). About 100x the cost of
    one score: for choosing one order across several songs, not for a search."""
    deck_power = (
        overall_power_for(tuple(team.members), team.leader, cfg) if cfg.song_absolute_deck_power else None
    )
    got: dict = {}
    score_song_detailed(
        tuple(team.members),
        (team.leader,),
        chart,
        cfg,
        deck_power=deck_power,
        auto=cfg.song_auto,
        order_totals=got,
    )
    return {order: total for (_leader, order), total in got.items()}


def score_team_ceiling(
    team: Team, chart: SongChart, cfg: ScoringConfig, member_order: Sequence[str]
) -> float:
    """The run score if every ACTIVE fires, at `member_order` (the order the row plays):
    the top of that row's own run-score law.

    `score_team_on_chart` with `active_all_fire` alone picks the order best for the
    all-fire case, which can differ from the row's (SPECIAL cues land on other members)
    and so report a ceiling the row cannot reach; then the all-fire law at the row's
    order gives the figure."""
    from hbuilder.domain.song_distribution import run_score_distribution  # noqa: PLC0415

    cfg_max = replace(cfg, active_all_fire=True)
    free = _score_one(team, chart, cfg_max)
    if tuple(free.member_order) == tuple(member_order):
        return free.total_score
    law = run_score_distribution(
        tuple(team.members), team.leader, chart, cfg_max, member_order, free.deck_power
    )
    return law["expected_total"]
