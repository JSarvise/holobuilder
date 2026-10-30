"""`--explain`'s full per-skill attribution renderer, and
the per-run assumptions-plus-completeness header.

`format_explain` must list every `SkillContribution` in
`Team.effective_skill_instances()` order — never reordered by
contribution size — and must reproduce `output/table.py`'s `>=`
lower-bound marker verbatim for a tainted team, since a lower bound must
never be presented as an exact score in *any* output mode.

`format_assumptions` must report every unverified constant with
the literal word "unverified" on every defaulted value, and
`format_completeness` must report the collection's data
completeness in every output mode, including the boring all-known case.
"""

from __future__ import annotations

import json
from pathlib import Path

from hbuilder.domain.loader import load_cards, load_owned
from hbuilder.domain.scoring import score_with_breakdown
from hbuilder.domain.scoring_config import load_scoring_config
from hbuilder.output.assumptions import format_assumptions, format_completeness
from hbuilder.output.explain import format_explain
from hbuilder.output.table import LOWER_BOUND_MARKER
from hbuilder.solver.exact import solve_exact

from conftest import FIXTURE_CARDS_PATH, FIXTURE_OWNED_PATH, make_card


def _shipped_rank_one():
    """The rank-1 `RankedTeam` and its `ScoreBreakdown` from the real
    shipped fixture — used instead of a synthetic team because the
    fixture already carries every state `format_explain` must render:
    an applied gate, a gate that did not fire, a `data_missing` skill
    (`mio_01`), and a `weight == 0.0` family (WR-02's pinned
    `activation_rate_up`)."""
    cards = load_cards(FIXTURE_CARDS_PATH)
    owned = load_owned(FIXTURE_OWNED_PATH, cards)
    cfg = load_scoring_config()
    ranked = solve_exact(owned, cfg, top_n=1)
    breakdown = score_with_breakdown(ranked[0].team, cfg)
    return ranked[0], breakdown


def test_format_explain_orders_contributions_by_effective_skill_instances():
    """`format_explain` lists contributions in `Team
    .effective_skill_instances()` order for the shipped team, asserted
    against `Team.effective_skill_instances()` directly rather than a
    hardcoded list."""
    ranked_team, breakdown = _shipped_rank_one()
    instances = ranked_team.team.effective_skill_instances()
    assert len(breakdown.contributions) == len(instances)
    for contribution, instance in zip(breakdown.contributions, instances):
        assert contribution.card_id == instance.card_id
        assert contribution.slot_kind == instance.slot_kind
        assert contribution.source == instance.source

    lines = format_explain(1, ranked_team, breakdown)
    # The five header lines precede exactly one block per contribution, in
    # the same order breakdown.contributions already carries them.
    contribution_lines = lines[5 : 5 + len(breakdown.contributions)]
    for line, contribution in zip(contribution_lines, breakdown.contributions):
        assert line.startswith(f"{contribution.card_id}/{contribution.slot_kind} ")


def test_gated_skill_block_names_threshold_and_actual_count():
    """A gated skill's block names the threshold *and* the actual team
    count, not just fired/not-fired."""
    ranked_team, breakdown = _shipped_rank_one()
    lines = format_explain(1, ranked_team, breakdown)

    gated = next(
        contribution
        for contribution in breakdown.contributions
        if contribution.gate_dimension is not None
    )
    line = next(
        line
        for line in lines
        if line.startswith(f"{gated.card_id}/{gated.slot_kind} ")
    )
    assert (
        f"gate={gated.gate_dimension} {gated.gate_value} >= {gated.gate_threshold} "
        f"actual={gated.gate_actual_count} fired={gated.gate_fired}"
    ) in line


def test_data_missing_skill_names_missing_field_and_tainted_team_carries_marker():
    """A `data_missing` skill's block names the missing field, and the
    explain output for a tainted team carries the same `>=` marker the
    table uses (a lower bound is never shown as exact, in any mode)."""
    ranked_team, breakdown = _shipped_rank_one()
    assert breakdown.incomplete_skills  # the shipped team is always tainted
    lines = format_explain(1, ranked_team, breakdown)

    missing = next(
        contribution
        for contribution in breakdown.contributions
        if contribution.state == "data_missing"
    )
    line = next(
        line
        for line in lines
        if line.startswith(f"{missing.card_id}/{missing.slot_kind} ")
    )
    assert f"missing_field={missing.missing_field}" in line

    closing_line = lines[-1]
    assert LOWER_BOUND_MARKER in closing_line
    for field in breakdown.incomplete_skills:
        assert field in closing_line


def test_zero_weight_skill_reads_differently_from_gate_not_met_skill():
    """A skill whose family weight is `0.0` (a configured assumption)
    reads differently from a skill whose gate simply did not fire — both
    contribute zero, but for structurally different, distinguishable
    reasons."""
    ranked_team, breakdown = _shipped_rank_one()
    lines = format_explain(1, ranked_team, breakdown)

    zero_weight = next(
        contribution for contribution in breakdown.contributions if contribution.weight == 0.0
    )
    zero_weight_line = next(
        line
        for line in lines
        if line.startswith(f"{zero_weight.card_id}/{zero_weight.slot_kind} ")
    )
    assert "configured assumption" in zero_weight_line

    # A gate_not_met skill that is NOT also zero-weight: the two reasons for
    # contributing nothing must be distinguishable, so a contribution that is
    # both at once cannot tell them apart. Under the five-member model the
    # first gate_not_met contribution happens to be zero-weight too, which is
    # why this selects rather than takes the first.
    not_fired = next(
        contribution
        for contribution in breakdown.contributions
        if contribution.state == "gate_not_met" and contribution.weight != 0.0
    )
    not_fired_line = next(
        line
        for line in lines
        if line.startswith(f"{not_fired.card_id}/{not_fired.slot_kind} ")
    )
    assert "configured assumption" not in not_fired_line
    assert "fired=False" in not_fired_line


def test_targeted_stat_up_contribution_explain_line_names_target_kind_and_reached_card_ids():
    """A targeted stat_up contribution's explain line names
    both the target kind and the reached card ids -- the reader must be
    able to tell a targeted effect that reached two cards from one that
    reached the whole team without cross-referencing anything else."""
    ranked_team, breakdown = _shipped_rank_one()
    lines = format_explain(1, ranked_team, breakdown)

    targeted = next(
        c
        for c in breakdown.contributions
        if c.family == "stat_up" and c.target_kind is not None
    )
    line = next(
        line for line in lines if line.startswith(f"{targeted.card_id}/{targeted.slot_kind} ")
    )
    assert f"target={targeted.target_kind}" in line
    assert ",".join(targeted.target_card_ids) in line


def test_score_pool_contribution_explain_line_carries_no_target_segment():
    """A score-pool contribution never joins the stat pool at all, so its
    explain line must carry no `target=` segment -- the segment's absence
    is itself informative, never an empty placeholder."""
    ranked_team, breakdown = _shipped_rank_one()
    lines = format_explain(1, ranked_team, breakdown)

    score_pool_contribution = next(c for c in breakdown.contributions if c.pool == "score")
    line = next(
        line
        for line in lines
        if line.startswith(f"{score_pool_contribution.card_id}/{score_pool_contribution.slot_kind} ")
    )
    assert "target=" not in line


def test_format_assumptions_marks_file_and_default_provenance(tmp_path):
    """`format_assumptions` marks a default-sourced key `(unverified
    default)` and a file-sourced key `(config.json)`, exercised by
    writing a temporary `config.json` that sets exactly one top-level
    key."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"model": {"stacking": "additive", "activation_rate": "pp"}}),
        encoding="utf-8",
    )
    cfg = load_scoring_config(config_path)
    lines = format_assumptions(cfg)

    model_lines = [line for line in lines if line.startswith("MODEL.")]
    assert model_lines
    assert all("(config.json)" in line for line in model_lines)

    tier_lines = [line for line in lines if line.startswith("PROBABILITY_TIERS:")]
    assert tier_lines
    assert all("(unverified default)" in line for line in tier_lines)


def test_format_assumptions_reports_config_json_even_when_byte_identical_to_default(
    tmp_path,
):
    """Adjacency edge: a `config.json` whose value for a key is
    byte-identical to the shipped default is still reported
    `(config.json)`, because provenance records where the value came
    from, not whether it differs."""
    config_path = tmp_path / "config.json"
    # additive/multiplicative is DEFAULT_CONFIG["model"]'s own value —
    # byte-identical. This test asserts byte-identity WITH THE DEFAULT, so it
    # has to track the default.
    config_path.write_text(
        json.dumps(
            {"model": {"stacking": "additive", "activation_rate": "multiplicative"}}
        ),
        encoding="utf-8",
    )
    default_cfg = load_scoring_config(Path("__no_such_config_file__.json"))
    file_cfg = load_scoring_config(config_path)

    assert default_cfg.stacking_model.name == file_cfg.stacking_model.name
    assert default_cfg.activation_rate_model.name == file_cfg.activation_rate_model.name

    default_lines = format_assumptions(default_cfg)
    file_lines = format_assumptions(file_cfg)
    default_model_line = next(line for line in default_lines if line.startswith("MODEL.STACKING"))
    file_model_line = next(line for line in file_lines if line.startswith("MODEL.STACKING"))
    assert "(unverified default)" in default_model_line
    assert "(config.json)" in file_model_line


def test_format_completeness_reports_zero_unknown_fields_for_fully_populated_collection():
    """Empty edge: a fully populated synthetic collection still
    emits the completeness line, reporting zero unknown fields rather
    than omitting the line."""
    cards = (make_card("synth_full_a"), make_card("synth_full_b"))
    lines = format_completeness(cards)
    assert len(lines) == 1
    assert "0 unknown field" in lines[0]
    assert "2/2" in lines[0]


def test_format_assumptions_is_deterministic_across_repeated_calls():
    """Ordering edge: two calls to `format_assumptions` with the
    same config return identical `list[str]`."""
    cfg = load_scoring_config()
    assert format_assumptions(cfg) == format_assumptions(cfg)
