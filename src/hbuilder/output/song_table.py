"""The per-song Top-N table — its OWN output path, deliberately not `table.py`'s.

`format_team_rows` prints `breakdown.total_score`, the song-agnostic ranking
quantity. A per-song ranking sorts by a different number, so reusing that
renderer would produce a table **sorted by one number and showing another** —
the exact class of quietly-wrong output this project exists to prevent. Rather
than teach one renderer two meanings, the song modes get their own, and the two
tables are free to have different columns because they answer different
questions.

Two columns exist here that `table.py` cannot have, and both matter:

* **`ORDER`** — the slot order that achieved the score. A tool that
  recommends a team without its slot order hands the player the
  median case instead of the best one. The SPECIAL cue assignment binds on
  every team, so there is always one to report.
* **`WINS`** (across-songs only) — how many of the charts this team is best on.
  A mean can hide a team that wins overall while losing most songs, so the mean
  is never shown alone.

`SCORE` here is an **absolute predicted run score** under the full-perfect
premise, not a ranking quantity — see `song.score_song`. It is an exact
expectation over the ACTIVE roll, not a sample.
"""

from __future__ import annotations

from typing import Sequence

from hbuilder.domain.song_rank import RankedOnSong, RankedOnSongs

__all__ = [
    "SONG_TABLE_COLUMNS",
    "SONGS_TABLE_COLUMNS",
    "format_song_rows",
    "format_song_basis",
]

SONG_TABLE_COLUMNS: tuple[str, ...] = (
    "RANK", "EXPECTED", "LEADER", "MEMBERS", "ORDER",
)
SONGS_TABLE_COLUMNS: tuple[str, ...] = (
    "RANK", "MEAN", "WINS", "LEADER", "MEMBERS",
)


def _row(rank: int, row: RankedOnSong, across: bool) -> str:
    members = ",".join(row.member_ids)
    if across:
        return (
            f"{rank} {row.expected_score:,.1f} {row.wins} "
            f"{row.leader_id} {members}"
        )
    return (
        f"{rank} {row.expected_score:,.1f} {row.leader_id} {members} "
        f"{'>'.join(row.member_order)}"
    )


def format_song_rows(ranking: RankedOnSongs, *, top: int | None = None) -> list[str]:
    """One header plus one row per team, in the order `ranking` already holds.

    Never reorders — `rank_on_chart`/`rank_across_charts` own the sort, and a
    renderer that sorted too could disagree with the ranking it is printing."""
    across = ranking.chart_count != 1
    columns = SONGS_TABLE_COLUMNS if across else SONG_TABLE_COLUMNS
    lines = [" ".join(columns)]
    rows = ranking.teams if top is None else ranking.teams[:top]
    for rank, row in enumerate(rows, start=1):
        lines.append(_row(rank, row, across))
    return lines


def _premise(ranking: RankedOnSongs) -> str:
    """Which run the score describes. Stated, never assumed: an AUTO ranking
    labelled "full-perfect premise" is one number computed and another
    reported, and the combo half of the AUTO premise makes the two genuinely
    different answers (`ScoringConfig.song_auto`)."""
    return ("AUTO premise (AUTO judgement column, combo=0 so 'combo >= N' "
            "magnitude overrides never fire)" if ranking.auto
            else "full-perfect premise")


def format_song_basis(ranking: RankedOnSongs) -> list[str]:
    """What the ranking rested on, as statements of fact rather than caveats.

    The filter line is the important one and is always printed. Stage 1 is the
    song-agnostic solver, so a team that would win on real charts but misses
    its top K is never seen — no care in stage 2 recovers it, and a reader who
    is not told cannot know to widen K and re-check."""
    lines: list[str] = []
    if ranking.chart_count == 1:
        lines.append(
            f"SONG: expected run score on {ranking.charts[0]} {ranking.difficulty}, "
            f"{_premise(ranking)}, exact expectation over the ACTIVE roll"
        )
    else:
        lines.append(
            f"SONGS: mean expected run score over {ranking.chart_count} "
            f"{ranking.difficulty} charts, {_premise(ranking)}; WINS counts the "
            f"charts a team is best on"
        )
    # The measurement is quoted, not just the warning. A caveat a reader cannot
    # size is a caveat they cannot act on: a top-K of 50 misses the real winner
    # on about 1 song in 30.
    lines.append(
        f"FILTER: these are the top {ranking.filtered_from} teams by the "
        f"song-agnostic ranking, re-scored here; a team outside it is never "
        f"scored. Checked over 181 EXPERT charts at pool 600: the per-chart winner was "
        f"song-agnostic rank 1 on 181/181; the deepest team in any per-chart "
        f"top-5 sat at rank 23, and the top-20 by mean drew from ranks <=47. "
        f"The default keeps >3x margin. Re-run the depth check after ANY "
        f"scorer change — the previous text quoted depths (<=7 on 29 of 30, "
        f"61 on one) measured while the per-song scorer was discarding board "
        f"POWER, so stage 2 disagreed with stage 1 on the leader and winners "
        f"sat deep; that scorer no longer exists"
    )
    if ranking.chart_count > 1 and ranking.teams:
        best_mean = ranking.teams[0]
        best_wins = max(ranking.teams, key=lambda row: row.wins)
        if best_wins is not best_mean and best_wins.wins > best_mean.wins:
            # By RANK, not by leader id: the top teams routinely share a leader
            # and differ only in members, and naming the leader twice reads as
            # a bug rather than as the finding it is.
            rank = ranking.teams.index(best_wins) + 1
            lines.append(
                f"⚠ SPLIT: rank 1 leads on mean but rank {rank} wins more "
                f"charts ({best_wins.wins} vs {best_mean.wins} of "
                f"{ranking.chart_count}) — the mean is carried by which songs "
                f"it wins, not by winning more of them. If you play a spread "
                f"of songs rather than one, rank {rank} is the better team"
            )
    return lines
