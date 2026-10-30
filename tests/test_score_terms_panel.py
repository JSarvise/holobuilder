"""`build_score_terms` — the term-level panel the web renders.

A breakdown panel that does not reconstruct the number it claims to explain is
decoration. So the load-bearing test here is an IDENTITY: the rows the panel
shows, recombined by the formula they are labelled with, must give back
`total_score` exactly. Everything else in this file guards the two things
a renderer will otherwise get wrong:

1. **The total is a HYBRID and is not the game's Unit Score.** The deck
   screen ADDS the board score bonus where `scoring.py` MULTIPLIES it, which
   overstates by 2.02–11.76 % and is team-dependent. So the panel must never
   present itself as the game's panel.
2. **Four of the game's five bonus lines are not separable in this
   model.** The pool is `(1 + B) x max` per bucket, not a sum. Splitting the
   number to match the game would change it. One honest combined row, never
   four fabricated ones.

And `None` is never `0`: a term the model does not cover is absent, not worth nothing.
"""

from __future__ import annotations

import dataclasses

import pytest

from hbuilder.domain.scoring import ScoreBreakdown, score_with_breakdown
from hbuilder.output.json_output import build_score_terms

from conftest import make_card, make_scoring_config, make_team


def _rows(terms: dict, column: str) -> dict[str, dict]:
    return {row["field"]: row for row in terms[column]}


def _breakdown(**overrides) -> ScoreBreakdown:
    """A breakdown with every scalar set to a distinct, hand-checkable value,
    so an identity failure names the term that broke it."""
    base = ScoreBreakdown(
        total_score=0.0,
        base_stat_sum=1000.0,
        effective_stat_sum=1200.0,  # a +20 % stat rate
        combined_stat_rate=0.20,
        combined_score_rate=0.50,
        combined_support_rate=0.0,
        board_score_rate=0.10,
        board_power=300.0,
        team_rate_up=0.0,
        contributions=(),
        incomplete_skills=(),
        stacking_model_name="x",
        activation_rate_model_name="y",
        active_stacking_model_name="z",
        run_state=None,
    )
    resolved = dataclasses.replace(base, **overrides)
    # Keep the object self-consistent: the total is what the formula says.
    total = (
        (resolved.effective_stat_sum + resolved.board_power)
        * (1.0 + resolved.combined_score_rate)
        * (1.0 + resolved.board_score_rate)
    )
    return dataclasses.replace(resolved, total_score=total)


# ==========================================================================
# 1. The identity — the panel must rebuild the number it explains
# ==========================================================================


def test_the_rows_recombine_into_the_total_exactly():
    """(1200 + 300) x 1.50 x 1.10 = 2475.0, by hand.

    The power rows sum to Overall Power, and Overall Power carried through
    both bonus factors is the total. If a future term is added to the score
    and not to this panel, this fails — which is the point: a panel that
    silently omits a term still looks complete."""
    breakdown = _breakdown()
    terms = build_score_terms(breakdown, board_modelled=True)

    assert breakdown.total_score == pytest.approx(2475.0)

    power = _rows(terms, "power")
    summed = sum(row["value"] for row in terms["power"])
    assert summed == pytest.approx(terms["overall_power"])
    assert terms["overall_power"] == pytest.approx(1500.0)

    # 1000 base, +200 from the stat rate, +300 board.
    assert power["card_potential_parameter"]["value"] == pytest.approx(1000.0)
    assert power["up_by_live_passive_skill"]["value"] == pytest.approx(200.0)
    assert power["up_by_skill_tree"]["value"] == pytest.approx(300.0)

    bonus = _rows(terms, "bonus")
    rebuilt = (
        terms["overall_power"]
        * (1.0 + bonus["combined_score_rate"]["value"] / 1000.0)
        * (1.0 + bonus["by_skill_tree"]["value"] / 1000.0)
    )
    assert rebuilt == pytest.approx(breakdown.total_score)
    assert rebuilt == pytest.approx(2475.0)


def test_the_identity_holds_on_a_really_scored_team():
    """The same identity, but against `score_with_breakdown` rather than a
    hand-built object — so it cannot pass by agreeing with a fixture that
    shares its own mistake."""
    team = make_team([make_card(f"card_{i}", member=f"Holomem{i}") for i in range(6)])
    cfg = make_scoring_config()
    breakdown = score_with_breakdown(team, cfg)
    terms = build_score_terms(breakdown, board_modelled=cfg.board is not None)

    assert sum(
        row["value"] for row in terms["power"] if row["value"] is not None
    ) == pytest.approx(terms["overall_power"])

    bonus = _rows(terms, "bonus")
    board_permil = bonus["by_skill_tree"]["value"] or 0.0
    rebuilt = (
        terms["overall_power"]
        * (1.0 + bonus["combined_score_rate"]["value"] / 1000.0)
        * (1.0 + board_permil / 1000.0)
    )
    assert rebuilt == pytest.approx(breakdown.total_score)


# ==========================================================================
# 2. This is not the game's Unit Score, and says so
# ==========================================================================


def test_the_panel_refuses_to_call_itself_the_unit_score():
    """`total_score` is the deck-screen power assembly wearing the in-run
    score stage, so it is neither quantity exactly. The deck screen ADDS the
    board score bonus; the model MULTIPLIES it. Over 10 captures the
    multiplicative form runs 2.02–11.76 % high, and the spread is
    team-dependent, so for the deck-screen quantity it is a wrong formula and
    not a missing constant.

    A hard `False` rather than a missing key: a consumer must not be able to
    default it to true, and a reviewer must be able to grep for it."""
    terms = build_score_terms(_breakdown(), board_modelled=True)
    assert terms["is_unit_score"] is False
    assert terms["total_label"] == "our ranking score"


# ==========================================================================
# 3. One combined bonus row, not four invented ones
# ==========================================================================


def test_the_bonus_column_keeps_the_unsplittable_sources_in_one_row():
    """The game bills Active, Special, Passive and Outfit separately. The model
    has one `combined_score_rate` because the pool is `(1 + B) x max` per bucket,
    and `ScoreBreakdown`'s docstring records that the per-skill rows do not
    sum to it. Four rows here would be four numbers nobody can defend."""
    terms = build_score_terms(_breakdown(), board_modelled=True)
    bonus = _rows(terms, "bonus")

    assert set(bonus) == {"combined_score_rate", "by_skill_tree"}
    combined = bonus["combined_score_rate"]
    assert combined["basis"] == "combined"
    assert "change the number" in combined["note"]

    # The board is separable and is therefore its own row, not folded in.
    assert bonus["by_skill_tree"]["basis"] == "computed"


def test_every_row_carries_its_unit():
    """Power rows are absolute and bonus rows are permil,
    and a renderer that guesses is wrong by three orders of magnitude."""
    terms = build_score_terms(_breakdown(), board_modelled=True)
    assert {row["unit"] for row in terms["power"]} == {"power"}
    assert {row["unit"] for row in terms["bonus"]} == {"permil"}
    assert {row["unit"] for row in terms["outside_total"]} == {"power"}
    for column in ("power", "bonus", "outside_total"):
        for row in terms[column]:
            assert row["basis"] in {"computed", "combined", "folded", "not-modelled"}


# ==========================================================================
# 4. `None` is never `0`
# ==========================================================================


def test_an_unmodelled_board_is_absent_not_zero():
    """`board_power == 0.0` cannot distinguish "the board contributed nothing"
    from "no board was loaded" — only `cfg.board is None` can, which is why
    `board_modelled` is a parameter and not inferred from the value."""
    terms = build_score_terms(
        _breakdown(board_power=0.0, board_score_rate=0.0), board_modelled=False
    )
    assert _rows(terms, "power")["up_by_skill_tree"]["value"] is None
    assert _rows(terms, "power")["up_by_skill_tree"]["basis"] == "not-modelled"
    assert _rows(terms, "bonus")["by_skill_tree"]["value"] is None

    # A board that IS modelled and genuinely contributes nothing is a zero,
    # and must read differently from the above.
    zeroed = build_score_terms(
        _breakdown(board_power=0.0, board_score_rate=0.0), board_modelled=True
    )
    assert _rows(zeroed, "power")["up_by_skill_tree"]["value"] == 0.0
    assert _rows(zeroed, "power")["up_by_skill_tree"]["basis"] == "computed"


def test_the_two_account_terms_are_reported_outside_the_total():
    """Both are ranking-safe to drop, and `total_score` does not include
    them. They are shown because the game's panel shows them, and kept out of
    the arithmetic because the model does not use them — both halves have to
    be visible at once."""
    terms = build_score_terms(
        _breakdown(memory_bonus=3753, member_power_up=460), board_modelled=True
    )
    outside = {row["field"]: row for row in terms["outside_total"]}
    assert outside["up_by_poster_collect"]["value"] == 3753
    assert outside["card_training_evaluation"]["value"] == 460

    # And they are NOT in the total, nor in Overall Power.
    assert terms["overall_power"] == pytest.approx(1500.0)
    assert terms["total_score"] == pytest.approx(2475.0)


def test_an_unsupplied_account_scalar_is_none_not_zero():
    """"Not told us" is not "told us they have none" — the collection model's
    rule, carried into the panel so a player who has typed nothing does not
    see a confident zero."""
    terms = build_score_terms(_breakdown(), board_modelled=True)
    outside = {row["field"]: row for row in terms["outside_total"]}
    assert outside["up_by_poster_collect"]["value"] is None
    assert outside["up_by_poster_collect"]["basis"] == "not-modelled"
    assert outside["card_training_evaluation"]["value"] is None
