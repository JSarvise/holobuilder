"""D-00/D-00a/D-00b: a tainted team is ranked inline with a `>=` prefix and
its missing card/field named, never excluded and never shown as a bare
total — skipped skills can only understate (D-03's non-negativity
invariant), so a tainted team's computed score is a lower bound on its
true score, not an estimate. There is no separate insufficient-data bucket
and no threshold selecting between rendering modes (D-00a): one rendering
path for every team.

Directly pins PITFALLS.md's "partially-populated cards confidently ranked"
pitfall: `tests/fixtures/owned.json` holds exactly 6 cards, one of which
(`mio_01`) has an unknown `ACTIVE.magnitude`, so every shipped candidate
team is tainted — the `>=` rendering is load-bearing on the shipped
fixture, not a theoretical edge case.
"""

from __future__ import annotations

from hbuilder.domain.loader import load_cards, load_owned
from hbuilder.domain.scoring import ScoreBreakdown, SkillContribution, score_with_breakdown
from hbuilder.domain.scoring_config import load_scoring_config
from hbuilder.output.table import (
    LOWER_BOUND_MARKER,
    exact_row_count,
    format_provisional_footer,
    format_team_rows,
    format_unscored_mechanics_footer,
)
from hbuilder.solver.exact import RankedTeam, solve_exact

from conftest import DEFAULT_RUN_STATE, FIXTURE_CARDS_PATH, FIXTURE_OWNED_PATH

_PROVISIONAL_TAIL = (
    f"are provisional: {LOWER_BOUND_MARKER} rows may score higher than shown "
    f"and displace rows above them."
)


def _ranked(score: float, leader_id: str, member_ids: list[str]) -> RankedTeam:
    """A bare `RankedTeam` for tests that pin `format_team_rows`/
    `format_provisional_footer`/`exact_row_count` directly against
    hand-built `ScoreBreakdown`s, without going through the real solver.
    `team` is never read by any function under test here."""
    return RankedTeam(score=score, leader_id=leader_id, member_ids=tuple(member_ids), team=None)


def _breakdown(
    total_score: float,
    *,
    incomplete_skills: tuple[str, ...] = (),
    contributions: tuple[SkillContribution, ...] = (),
) -> ScoreBreakdown:
    return ScoreBreakdown(
        total_score=total_score,
        base_stat_sum=total_score,
        effective_stat_sum=total_score,
        combined_stat_rate=0.0,
        combined_score_rate=0.0,
        combined_support_rate=0.0,
        board_score_rate=0.0,
        team_rate_up=0.0,
        contributions=contributions,
        incomplete_skills=incomplete_skills,
        stacking_model_name="additive",
        activation_rate_model_name="pp",
        active_stacking_model_name="max",
        run_state=DEFAULT_RUN_STATE,
    )


def _contribution(
    card_id: str = "c1",
    slot_kind: str = "ACTIVE",
    *,
    gate_fired: bool | None = None,
    gate_dimension: str | None = None,
    gate_value: str | None = None,
    gate_threshold: int | None = None,
    gate_actual_count: int | None = None,
    rider_family: str | None = None,
    rider_effect_type: str | None = None,
    rider_magnitude: float | None = None,
) -> SkillContribution:
    return SkillContribution(
        card_id=card_id,
        slot_kind=slot_kind,
        source="normal",
        family="score_support",
        effect_type="score",
        state="applied",
        pool="score",
        rate=0.1,
        weight=1.0,
        weighted_rate=0.1,
        missing_field=None,
        gate_dimension=gate_dimension,
        gate_value=gate_value,
        gate_threshold=gate_threshold,
        gate_actual_count=gate_actual_count,
        gate_fired=gate_fired,
        rider_family=rider_family,
        rider_effect_type=rider_effect_type,
        rider_magnitude=rider_magnitude,
    )


def _shipped_ranked_and_breakdowns():
    cards = load_cards(FIXTURE_CARDS_PATH)
    owned = load_owned(FIXTURE_OWNED_PATH, cards)
    cfg = load_scoring_config()
    ranked = solve_exact(owned, cfg, top_n=6)
    breakdowns = tuple(score_with_breakdown(candidate.team, cfg) for candidate in ranked)
    return ranked, breakdowns, cfg


def test_shipped_six_card_collection_is_fully_tainted_and_footer_says_no_rows_are_exact():
    """`mio_01`'s unknown `ACTIVE.magnitude` is in every shipped candidate
    team, so all six rows carry the `>=` marker, `exact_row_count` is 0,
    and the footer is the `No rows are exact.` form (D-00b)."""
    ranked, breakdowns, cfg = _shipped_ranked_and_breakdowns()
    assert len(ranked) == 6
    assert all(breakdown.incomplete_skills for breakdown in breakdowns)

    rows = format_team_rows(ranked, breakdowns, cfg)
    assert len(rows) == 7  # header + 6 teams (D-13)
    for row in rows[1:]:
        # SCORE is the second whitespace-delimited field.
        assert row.split(" ", 2)[1] == LOWER_BOUND_MARKER

    assert exact_row_count(breakdowns) == 0

    footer = format_provisional_footer(breakdowns)
    assert " ".join(footer) == (
        f"No rows are exact. Every row is provisional: {LOWER_BOUND_MARKER} "
        f"rows may score higher than shown and displace rows above them."
    )


def test_shipped_collection_incomplete_cell_names_the_card_and_missing_field():
    """Every tainted row's `INCOMPLETE` cell names the card and the dotted
    field, so the reader knows exactly what to type in."""
    ranked, breakdowns, cfg = _shipped_ranked_and_breakdowns()
    rows = format_team_rows(ranked, breakdowns, cfg)
    for row in rows[1:]:
        assert "mio_01:skills.ACTIVE.magnitude" in row


def test_fully_populated_synthetic_pool_has_no_marker_and_no_footer():
    """No row carries the marker and `format_provisional_footer` returns an
    empty list — the footer is omitted, not emptied to a blank line."""
    ranked = [
        _ranked(300.0, "leader_a", ["m1", "m2", "m3", "m4", "m5"]),
        _ranked(200.0, "leader_b", ["m1", "m2", "m3", "m4", "m6"]),
    ]
    breakdowns = [_breakdown(300.0), _breakdown(200.0)]

    footer = format_provisional_footer(breakdowns)
    assert footer == []

    cfg = load_scoring_config()
    rows = format_team_rows(ranked, breakdowns, cfg)
    for row in rows[1:]:
        assert LOWER_BOUND_MARKER not in row
    assert "are provisional:" not in " ".join(rows)


def test_footer_reads_rows_1_3_are_exact_when_rank_4_is_first_tainted():
    """A synthetic pool where exactly the rank-4 team is the first tainted
    one: the footer reads `Rows 1-3 are exact. Rows 4+ are provisional:
    ...` — the wording from CONTEXT.md, asserted verbatim."""
    breakdowns = [
        _breakdown(600.0),
        _breakdown(500.0),
        _breakdown(400.0),
        _breakdown(300.0, incomplete_skills=("card_d:skills.ACTIVE.magnitude",)),
        _breakdown(200.0, incomplete_skills=("card_e:skills.ACTIVE.magnitude",)),
        _breakdown(100.0, incomplete_skills=("card_f:skills.ACTIVE.magnitude",)),
    ]
    assert exact_row_count(breakdowns) == 3

    footer = format_provisional_footer(breakdowns)
    assert " ".join(footer) == f"Rows 1-3 are exact. Rows 4+ {_PROVISIONAL_TAIL}"


def test_footer_uses_singular_row_1_is_exact_form_when_only_rank_1_is_exact():
    """A synthetic pool where only rank 1 is exact: the footer uses the
    singular `Row 1 is exact.` form."""
    breakdowns = [
        _breakdown(600.0),
        _breakdown(500.0, incomplete_skills=("card_b:skills.ACTIVE.magnitude",)),
        _breakdown(400.0, incomplete_skills=("card_c:skills.ACTIVE.magnitude",)),
    ]
    assert exact_row_count(breakdowns) == 1

    footer = format_provisional_footer(breakdowns)
    assert " ".join(footer) == f"Row 1 is exact. Rows 2+ {_PROVISIONAL_TAIL}"


def test_team_with_no_gated_skills_renders_0_of_0_in_gates_column():
    """A team with no gated skills at all renders `0/0`, never a blank
    cell, so an empty gate set is distinguishable from a rendering failure
    (CLI-02 empty edge)."""
    ranked = [_ranked(100.0, "solo_leader", ["m1", "m2", "m3", "m4", "m5"])]
    breakdowns = [
        _breakdown(100.0, contributions=(_contribution(gate_fired=None),))
    ]
    cfg = load_scoring_config()
    rows = format_team_rows(ranked, breakdowns, cfg)
    assert rows[1].split()[-2] == "0/0"


def test_gates_column_counts_fired_over_total_gated_contributions():
    """A team with two gated skills, one fired and one not, renders
    `1/2` — gated-but-not-fired still counts toward the denominator."""
    ranked = [_ranked(100.0, "leader_x", ["m1", "m2", "m3", "m4", "m5"])]
    breakdowns = [
        _breakdown(
            100.0,
            contributions=(
                _contribution(
                    "c1",
                    gate_fired=True,
                    gate_dimension="type",
                    gate_value="Cute",
                    gate_threshold=2,
                    gate_actual_count=2,
                ),
                _contribution(
                    "c2",
                    gate_fired=False,
                    gate_dimension="generation",
                    gate_value="1期生",
                    gate_threshold=3,
                    gate_actual_count=1,
                ),
                _contribution("c3", gate_fired=None),
            ),
        )
    ]
    cfg = load_scoring_config()
    rows = format_team_rows(ranked, breakdowns, cfg)
    assert rows[1].split()[-2] == "1/2"


def test_equal_scores_render_as_two_separate_rows_ordered_and_deterministic():
    """Two teams with exactly equal scores both appear as their own rows
    (CLI-02 adjacency) in the order `ranked` already carries them (this
    function never reorders or merges), and a second identical call
    returns the identical `list[str]` (CLI-02 ordering/determinism)."""
    ranked = [
        _ranked(500.0, "aaa_leader", ["m1", "m2", "m3", "m4", "m5"]),
        _ranked(500.0, "bbb_leader", ["m1", "m2", "m3", "m4", "m6"]),
    ]
    breakdowns = [_breakdown(500.0), _breakdown(500.0)]
    cfg = load_scoring_config()

    first_call = format_team_rows(ranked, breakdowns, cfg)
    second_call = format_team_rows(ranked, breakdowns, cfg)

    assert len(first_call) == 3  # header + 2 distinct rows, never merged
    assert "aaa_leader" in first_call[1]
    assert "bbb_leader" in first_call[2]
    assert first_call == second_call


def test_table_renders_exactly_one_row_per_team_plus_one_header_line():
    """D-13: the compact table renders exactly one row per team by
    default — no more, no less."""
    ranked, breakdowns, cfg = _shipped_ranked_and_breakdowns()
    rows = format_team_rows(ranked, breakdowns, cfg)
    assert len(rows) == len(ranked) + 1


# --- Mechanic 4b (Phase 03.1 plan 06): format_unscored_mechanics_footer ----


def test_a_rider_carrying_breakdown_set_produces_exactly_one_unscored_mechanics_footer_line():
    breakdowns = [
        _breakdown(300.0, contributions=(_contribution("c1", rider_family="activation_rate_up"),)),
        _breakdown(200.0, contributions=(_contribution("c2"),)),
    ]
    footer = format_unscored_mechanics_footer(breakdowns)
    assert len(footer) == 1
    assert "1" in footer[0]
    assert "UNDERESTIMATE" in footer[0]


def test_a_rider_free_breakdown_set_produces_no_unscored_mechanics_footer_line():
    breakdowns = [_breakdown(300.0, contributions=(_contribution("c1"),)), _breakdown(200.0)]
    assert format_unscored_mechanics_footer(breakdowns) == []


def test_a_rider_alone_does_not_put_the_lower_bound_marker_on_a_team_row():
    """D-30: a rider is present data deliberately not scored, never missing
    data -- `format_team_rows` must not prefix a rider-only row's score
    with LOWER_BOUND_MARKER, which is driven exclusively by
    `breakdown.incomplete_skills` (team_taint), never by rider presence."""
    ranked = [_ranked(300.0, "leader_a", ["m1", "m2", "m3", "m4", "m5"])]
    breakdowns = [
        _breakdown(
            300.0,
            contributions=(_contribution("c1", rider_family="activation_rate_up"),),
        )
    ]
    rows = format_team_rows(ranked, breakdowns, cfg=None)
    assert LOWER_BOUND_MARKER not in rows[1]
