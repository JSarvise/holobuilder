"""`--explain <rank>` renders full per-skill attribution for a
single ranked team — every skill's state, gate dimension, target,
threshold, actual team count, rate, weight and weighted rate, in
`Team.effective_skill_instances()` order (never reordered by contribution
size). Consumes a `RankedTeam` and its `ScoreBreakdown`
only, following `output/table.py`'s "build `list[str]`, caller prints"
shape — never prints, never imports `argparse`/`pathlib`/`sys`.

The `>=` lower-bound line `output/table.py` renders for a tainted team is
reproduced here verbatim rather than dropped: a lower bound must never be
presented as an exact score in *any* output mode, and `--explain` is one
such mode.
"""

from __future__ import annotations

from hbuilder.domain.scoring import ScoreBreakdown, SkillContribution
from hbuilder.output.table import LOWER_BOUND_MARKER
from hbuilder.solver.exact import RankedTeam


def _format_contribution(contribution: SkillContribution) -> str:
    """One line naming every attribution field of a single
    `SkillContribution`: `card_id`, `slot_kind`, `source`, `family`,
    `effect_type`, `state`, `pool`, `rate`, `weight`, `weighted_rate`.

    A gated skill additionally names its gate's `dimension`, `value`,
    `threshold`, `actual_count` and `fired` — so the reader sees which
    gates fired *and* the actual team count that fired them, never only
    the boolean. A `data_missing` skill additionally names
    `missing_field`. A `weight == 0.0` skill says so explicitly, so a
    family contributing zero by a configured assumption (e.g.
    `life_recovery` at its shipped default) reads differently from a
    family contributing zero because its gate did not fire.

    A resolved `pool == "stat"` contribution
    additionally names its `target_kind` and the reached card ids
    (`target_card_ids`), placed after `pool=` and before `rate=` so the
    line reads "what it is, who it reached, what it was worth" — never
    rendered when `target_kind is None` (a score-pool row, a pool=="none"
    row, or a stat row that never resolved a target at all).

    A slot carrying an override
    additionally names `override_fired`, placed right after
    `weighted_rate=` so a reader can tell a value that came from a fired
    override apart from a value that was always the base — never
    rendered when `override_fired is None` (the slot carries no
    override).

    A slot carrying a rider
    additionally names the rider's family, effect_type and magnitude, and
    states the DIRECTION of the resulting error — "represented, not
    scored — this row is an UNDERESTIMATE" — never merely the fact that a
    rider exists. Placed after the `override_fired=` segment (if any) and
    before the `weight == 0.0` note, so the line reads value-then-caveat;
    never rendered when `contribution.rider_family is None` (the slot
    carries no rider)."""
    parts = [
        f"{contribution.card_id}/{contribution.slot_kind}",
        f"source={contribution.source}",
        f"family={contribution.family}",
        f"effect_type={contribution.effect_type}",
        f"state={contribution.state}",
        f"pool={contribution.pool}",
    ]
    if contribution.target_kind is not None:
        parts.append(
            f"target={contribution.target_kind} "
            f"reached={','.join(contribution.target_card_ids)}"
        )
    parts.append(f"rate={contribution.rate!r}")
    parts.append(f"weight={contribution.weight!r}")
    parts.append(f"weighted_rate={contribution.weighted_rate!r}")
    if contribution.override_fired is not None:
        parts.append(f"override_fired={contribution.override_fired!r}")
    if contribution.rider_family is not None:
        parts.append(
            f"rider={contribution.rider_family}/{contribution.rider_effect_type} "
            f"magnitude={contribution.rider_magnitude!r} represented, not "
            f"scored - this row is an UNDERESTIMATE"
        )
    if contribution.weight == 0.0:
        parts.append(
            "note=weight is 0.0 by configured assumption, not because a "
            "gate did not fire"
        )
    if contribution.state == "data_missing":
        parts.append(f"missing_field={contribution.missing_field}")
    if contribution.gate_dimension is not None:
        parts.append(
            f"gate={contribution.gate_dimension} {contribution.gate_value} "
            f">= {contribution.gate_threshold} "
            f"actual={contribution.gate_actual_count} "
            f"fired={contribution.gate_fired}"
        )
    return " ".join(parts)


def format_explain(rank: int, ranked_team: RankedTeam, breakdown: ScoreBreakdown) -> list[str]:
    """Full per-skill attribution for one ranked team.

    Emits a header naming `rank`, the leader and the five members, then
    the team totals, then `team_rate_up`. `base_stat_sum` and
    `effective_stat_sum` are reported independently, NOT as
    `effective_stat_sum = base_stat_sum * (1 + combined_stat_rate)`:
    `effective_stat_sum` is a per-`(card,
    stat_name)` accumulation (`card.stat(stat_name) * (1 +
    combine(rates naming that stat that target that card))`, summed over
    every card and every stat; see `scoring.py::_score_team`), so it
    cannot in general be reproduced from `base_stat_sum` and
    `combined_stat_rate` by hand. `combined_stat_rate` is printed
    alongside `EFFECTIVE_STAT_SUM` only because it remains a useful
    team-wide summary (the flat `combine()` of every weighted stat rate,
    ignoring target) — it is reported for context, not as
    `effective_stat_sum`'s multiplier. `total = effective_stat_sum * (1 +
    combined_score_rate) * (1 + board_score_rate)` still holds, and both
    multipliers are printed on the `TOTAL` line so it can be checked by
    hand — the board is a SEPARATE factor (as in the client's `CalcScore`), so
    printing one summed Score Bonus would misreport the arithmetic that
    actually ran. One block follows per entry of
    `breakdown.contributions` — which is already in
    `Team.effective_skill_instances()` order (leader normal slots,
    leader costume, then members in order) and is never reordered here
    by contribution size.

    Closes with the same `>=` lower-bound line `output/table.py` uses
    when `breakdown.incomplete_skills` is non-empty, naming every
    incomplete field — the marker must not be dropped just because the
    output surface changed."""
    lines: list[str] = [
        f"RANK {rank}: LEADER {ranked_team.leader_id} MEMBERS "
        f"{','.join(ranked_team.member_ids)}"
    ]
    lines.append(f"BASE_STAT_SUM: {breakdown.base_stat_sum:,.1f}")
    lines.append(
        f"EFFECTIVE_STAT_SUM: {breakdown.effective_stat_sum:,.1f} "
        f"(combined_stat_rate={breakdown.combined_stat_rate!r} — team-wide "
        f"summary only, no longer effective_stat_sum's multiplier since "
        f"D-26; see per-card/per-stat contributions below)"
    )
    lines.append(
        f"TOTAL: {breakdown.total_score:,.1f} "
        f"(combined_score_rate={breakdown.combined_score_rate!r} "
        f"board_score_rate={breakdown.board_score_rate!r})"
    )
    lines.append(f"TEAM_RATE_UP: {breakdown.team_rate_up!r}")

    for contribution in breakdown.contributions:
        lines.append(_format_contribution(contribution))

    if breakdown.incomplete_skills:
        lines.append(
            f"{LOWER_BOUND_MARKER} {breakdown.total_score:,.1f} is a LOWER "
            f"BOUND — missing: {' '.join(breakdown.incomplete_skills)}"
        )

    return lines
