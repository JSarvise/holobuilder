"""Compact Top-N table, one row per team, and no more. Consumes
`RankedTeam` and each team's own `ScoreBreakdown`
— never prints, never imports `argparse` or `pathlib` — following
`cli.format_gates_report`'s "build `list[str]`, caller prints" shape.

A tainted team is never bucketed separately
and never excluded. Because every effect family is non-negative, a tainted team's computed total is a **lower bound** on
its true score, never an estimate — skipped skills can only understate.
It is ranked inline with every other team, its score cell prefixed with
`LOWER_BOUND_MARKER`, and its `INCOMPLETE` cell naming the card and
missing field. `format_provisional_footer` then states, as a computed
fact rather than a caveat, which rows above the topmost tainted row are
provably correctly placed.
"""

from __future__ import annotations

from typing import Sequence

from hbuilder.domain.scoring import ScoreBreakdown
from hbuilder.domain.scoring_config import ScoringConfig
from hbuilder.solver.exact import RankedTeam

# Rendered as the single character U+2265 ("greater than or equal
# to"), not the two-character ASCII ">=" — `sys.stdout.reconfigure(
# encoding="utf-8")` in `cli.main` makes that safe on Windows.
LOWER_BOUND_MARKER: str = "≥"

# Column set: RANK/SCORE/LEADER/MEMBERS, GATES (fired/total) and
# INCOMPLETE (the named missing fields, or "-").
TABLE_COLUMNS: tuple[str, ...] = ("RANK", "SCORE", "LEADER", "MEMBERS", "GATES", "INCOMPLETE")


def format_team_rows(
    ranked: Sequence[RankedTeam],
    breakdowns: Sequence[ScoreBreakdown],
    cfg: ScoringConfig,
) -> list[str]:
    """One header line plus exactly one row per team in `ranked`,
    in the order `ranked` is already sorted — this function never
    reorders. `breakdowns[i]` must be `score_with_breakdown(ranked[i]
    .team, cfg)`; the caller (the CLI) re-scores the already-selected
    Top-N this way, since the solver's hot loop only ever computes the
    bare `float` `RankedTeam.score`.

    `SCORE` reads `breakdown.incomplete_skills` for the `>=` marker
    rather than recomputing taint (`team_taint` is never called a second
    time here). A team with no gated skills at all renders
    `GATES` as `0/0`, never a blank cell, so an empty gate set stays
    distinguishable from a rendering failure. An
    empty `INCOMPLETE` cell renders `-`.

    `cfg` is unused and kept in the signature so a display option can be
    read from it later without changing this call site."""
    del cfg  # reserved for a display option, unused

    lines: list[str] = [" ".join(TABLE_COLUMNS)]
    for rank, (candidate, breakdown) in enumerate(zip(ranked, breakdowns), start=1):
        if breakdown.incomplete_skills:
            score_text = f"{LOWER_BOUND_MARKER} {breakdown.total_score:,.1f}"
        else:
            # Two leading spaces keep the column visually aligned with the
            # two-character-wide "≥ " prefix used on a tainted row.
            score_text = f"  {breakdown.total_score:,.1f}"

        gated = [
            contribution
            for contribution in breakdown.contributions
            if contribution.gate_fired is not None
        ]
        fired = sum(1 for contribution in gated if contribution.gate_fired)
        gates_text = f"{fired}/{len(gated)}"

        incomplete_text = (
            " ".join(breakdown.incomplete_skills) if breakdown.incomplete_skills else "-"
        )

        lines.append(
            f"{rank} {score_text} {candidate.leader_id} "
            f"{','.join(candidate.member_ids)} {gates_text} {incomplete_text}"
        )
    return lines


def exact_row_count(breakdowns: Sequence[ScoreBreakdown]) -> int:
    """The number of ranked rows above the topmost tainted row — the
    exactness boundary, computed with one pass over `breakdowns`
    rather than asserted. When no row is tainted, every row is exact
    (`len(breakdowns)`); when the very first row is tainted, this is `0`
    — the shipped-fixture case, since `mio_01` is in every candidate
    team drawn from `data/owned.json`."""
    for index, breakdown in enumerate(breakdowns):
        if breakdown.incomplete_skills:
            return index
    return len(breakdowns)


def format_provisional_footer(breakdowns: Sequence[ScoreBreakdown]) -> list[str]:
    """States, as a computed fact, exactly which rows are provably
    correctly placed. Untainted-vs-untainted comparisons are exact; any
    comparison involving a tainted (`>=`) team is provisional, because its
    true score could exceed anything ranked below it.

    Returns an empty list when no ranked team is tainted at all — the
    caveat is omitted **entirely**, not softened to a blank line, because
    a caveat that is always present is one nobody reads, and its absence
    then carries information too.

    Two alternatives are deliberately not used:
      - Marking every row a tainted score *could* displace: absent a
        finite upper bound on a tainted team's true score, that is
        nearly every row whenever any taint exists at all, which teaches
        the reader to ignore the marking exactly when it matters most.
      - Marking tainted rows inline with no footer at all: the inference
        "everything from the first `>=` row down is provisional" is
        obvious to whoever specified the notation and non-obvious to
        whoever reads the table six weeks later.

    Returned as two lines, never joined into one — the exactness
    claim and the provisional claim are two separate, independently true
    statements."""
    if not any(breakdown.incomplete_skills for breakdown in breakdowns):
        return []

    boundary = exact_row_count(breakdowns)
    provisional_clause = (
        f"are provisional: {LOWER_BOUND_MARKER} rows may score higher than "
        f"shown and displace rows above them."
    )

    if boundary == 0:
        return [
            "No rows are exact.",
            f"Every row is provisional: {LOWER_BOUND_MARKER} rows may score "
            f"higher than shown and displace rows above them.",
        ]
    if boundary == 1:
        exact_clause = "Row 1 is exact."
    else:
        exact_clause = f"Rows 1-{boundary} are exact."
    return [
        exact_clause,
        f"Rows {boundary + 1}+ {provisional_clause}",
    ]


def format_unscored_mechanics_footer(breakdowns: Sequence[ScoreBreakdown]) -> list[str]:
    """A SEPARATE footer from
    `format_provisional_footer`, naming how many contributions across
    `breakdowns` carry a rider (`contribution.rider_family is not None`)
    and stating that those rows are underestimates because a second effect
    on those slots is represented but deliberately not scored.

    Returns an empty list when no contribution carries a rider at all —
    the same "no footer when nothing is provisional" discipline
    `format_provisional_footer` already follows.

    Deliberately a second, separate footer rather than reusing
    `LOWER_BOUND_MARKER`/the `>=` marker: that marker means "no value is
    known for this card" (missing data, `team_taint`); this footer
    means "a value is known and is not scored" (present
    data, a modelling decision). Overloading the `>=` marker to also cover
    a rider would make cards with riders look like transcription gaps."""
    count = sum(
        1
        for breakdown in breakdowns
        for contribution in breakdown.contributions
        if contribution.rider_family is not None
    )
    if count == 0:
        return []
    return [
        f"{count} row(s) carry a second, unscored effect (a rider) and are "
        f"UNDERESTIMATES: represented but not scored in this version."
    ]
