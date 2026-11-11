"""The scorer's result types: the per-skill attribution (`SkillContribution`), the full
`ScoreBreakdown`, and the deck screen's additive Unit Score built from it
(`DeckScreenScore`, `deck_screen_unit_score`, `UNIT_SCORE_K`).

Split out of `scoring.py`, which re-exports every name here."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from hbuilder.domain.gates import RunState
from hbuilder.domain.scoring_config import SLOT_ORDER_RULES


@dataclass(frozen=True, slots=True)
class SkillContribution:
    """One skill instance's contribution to a team's score, in the vocabulary of
    `ResolvedSkill`/`GateResolution`/`GatePredicate`.

    - `state`: `"applied"` (a real, possibly zero, contribution), `"gate_not_met"` or
      `"data_missing"`; the last two are both zero for different reasons and never conflated.
    - `gate_*`: from the skill's `GateResolution`/`GatePredicate` when gated, all `None` when
      ungated. A count equal to the threshold is `applied`; one short is `gate_not_met`, with
      both count and threshold reported.
    - `weight` is the `family_weights` value applied and `weighted_rate = rate * weight`, so a
      zero by configured weight (e.g. `life_recovery` at 0.0) reads differently from a zero
      by gate.
    - `target_kind`/`target_card_ids`: the cards a stat or support contribution reached, from
      the same resolution the score used, so they cannot disagree with it. `None` for rows
      that resolved no target (other pools, or not `applied`). `target_kind` is
      `schema.TEAM_WIDE_TARGET_KIND` for a team-wide target, never `None`; `target_card_ids`
      is sorted so identical inputs serialise identically.
    - `override_fired`: `True`/`False` when the slot has an override whose gate did/did not
      fire, `None` when it has none, so a reader can tell which magnitude `rate` used.
    - `rider_family`/`rider_effect_type`/`rider_magnitude`: the slot's second effect, for
      attribution only; a rider's value never appears in `rate`/`weight`/`weighted_rate`."""

    card_id: str
    slot_kind: str
    source: str
    family: str
    effect_type: str
    state: str
    pool: str
    rate: float
    weight: float
    weighted_rate: float
    missing_field: str | None
    gate_dimension: str | None
    gate_value: str | None
    gate_threshold: int | None
    gate_actual_count: int | None
    gate_fired: bool | None
    target_kind: str | None = None
    target_card_ids: tuple[str, ...] | None = None
    override_fired: bool | None = None
    rider_family: str | None = None
    rider_effect_type: str | None = None
    rider_magnitude: float | None = None


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """The full attribution result:
    `total_score = (effective_stat_sum + board_power) * (1 + combined_score_rate)
    * (1 + board_score_rate)`, each stage inspectable.

    `contributions` are in `resolve_team_gates` order, never reordered by value;
    `incomplete_skills` is `team_taint(team)`.

    `board_score_rate` is the board's own trailing factor in `CalcScore`, never part of a
    summed Score Bonus; 0.0 means either no board bonus or no board modelled (`cfg.board`
    tells them apart).

    `combined_support_rate` is the game's `B`, the live multiplier on the ACTIVE Score UP
    maximum. It includes the team's SPECIAL Score UPs (they are `ScoreUpBoostSkillEffect`s),
    not only `score_support` rows.

    The Score UP rows do not sum to the score pool: each row's `weighted_rate` is what the
    skill would contribute alone (`magnitude x occupancy x weight`, before `B`), while the
    pool is `(1 + B) x max_ACTIVE` of the boosted values, and a SPECIAL row buys a share of
    the ACTIVE maximum. A contribution is not separable when only the highest live effect is
    paid; `combined_score_rate` is the number actually used.

    The model names and `run_state` make every total traceable to its interpretations."""

    total_score: float
    base_stat_sum: float
    effective_stat_sum: float
    combined_stat_rate: float
    combined_score_rate: float
    combined_support_rate: float
    board_score_rate: float
    team_rate_up: float
    contributions: tuple[SkillContribution, ...]
    incomplete_skills: tuple[str, ...]
    stacking_model_name: str
    activation_rate_model_name: str
    active_stacking_model_name: str
    run_state: RunState
    # The ACTIVE Score UP pool (`_score_up_pool`'s result), which the board's active-pool
    # channels (skill_effect_up, activation_rate, cooldown) multiply; exposed so a
    # board-plan valuer can price those nodes. 0.0 on the batch path.
    active_pool: float = 0.0
    # The slot-order rule that produced `effective_stat_sum`.
    slot_order_rule: str = SLOT_ORDER_RULES[0]
    # The member ids in the deck slot order the rule chose, `None` when it chose none
    # (always under `"free"`): reporting an order the model never chose would read as a
    # recommendation.
    member_order: tuple[str, ...] | None = None

    # The two terms the game's Unit Score Details panel lists and the ranking omits (both
    # ranking-safe), never summed into `total_score`. `None` means the account scalar was
    # not supplied, `0` that it is zero (`domain/collection.AccountBonuses`).
    memory_bonus: int | None = None
    member_power_up: int | None = None

    # Overall Power (deck-screen display) stat block: Member Parameter + Outfit + Passive
    # with the server's per-(card, attribute) float32 ceilings
    # (`_overall_power_stat_subtotals`), not the float64 `effective_stat_sum` the ranking
    # uses. `None` where not built; `deck_screen_unit_score` then falls back to
    # `effective_stat_sum`.
    overall_power_stat_sum: float | None = None
    # The same stat block split by source line, `{slot_kind: total}`: the deck
    # screen's Outfit Skill (`COSTUME`) and Passive Skill (`PASSIVE`) lines, each paid its
    # own per-(card, stat) ceilings. DISPLAY only; `None` where not built.
    overall_power_stat_lines: Mapping[str, float] | None = None
    # Each member's Member Power-Up base (MemberParameter + Outfit + Passive +
    # Holomem Board), in `members` order: what a song's singer-conditional board
    # power is added to before the power-up's per-member ceilings are paid
    # again (`song.song_overall_power`). `None` where not built.
    power_up_bases: tuple[float, ...] | None = None

    # The Holomem Board's power half, a summand of Overall Power (`_board_bonus`), roughly
    # 30-50 % of Overall Power. As with `board_score_rate`, 0.0 does not distinguish "no
    # power" from "no board modelled"; `cfg.board is None` does.
    board_power: float = 0.0


# `Unit Score = ceil( Overall Power x (1 + Score Bonus) x K )`.
#
# K is a feasible interval, not a fit: each captured deck screen constrains it to
# `(unit-1)/X < K <= unit/X`, and the intersection over every capture (exact rational
# arithmetic, two independent capture sets) is
#
#     K in [2.037339908411 , 2.037340032136]      relative width 6.1e-08
#
# with a residue of exactly 0 on every observation. Other shapes (floor, round,
# MultiplyPermil-first, ceiling-before-bonus, per-line summation, multiplicative bonus)
# leave the intersection empty for any K. A `mean(unit / (power x factor))` estimate is
# biased upward by about half a point per sample, because the game takes the ceiling.
#
# 2.03734 is a point inside the interval, kept fixed so it does not move each time the
# interval narrows (the deck-screen oracle uses the same value). Being a round 5-decimal
# number is not identification: a band this wide contains one with probability ~1.2 %.
# K is not a constant the client ships (no `Setting` field in permil/permyriad, no
# float or integer encoding anywhere in the client image or master data, matches it), so
# it is a server-side computed product of unknown factorisation.
UNIT_SCORE_K: float = 2.03734


@dataclass(frozen=True, slots=True)
class DeckScreenScore:
    """The deck screen's own Unit Score, in the game's ADDITIVE shape: for display beside a
    ranking, never as a ranking objective.

    `CalcScore` (which `total_score` follows) multiplies the board's score bonus as its own
    factor; the deck screen adds it into one summed Score Bonus. The multiplicative form
    would cost the cross term `combined_score_rate x board_score_rate`, +2 % to +12 % and
    team-dependent on captured decks.

    The formula is exact: given the game's term values, the six power terms sum to
    `overall_power`, the five bonus terms to `score_bonus_permil`, and
    `overall_power x (1 + permil/1000) x K` reproduces `unit_score` (`UNIT_SCORE_K`). The base
    may not be: when the stat sources on disk do not reproduce the server's Member Parameter
    line, the number will not match the screen, and a caller must say so.

    `missing` names any Overall Power summand the account has not supplied (the two account
    scalars); `complete` is the one-look version. `unit_score` is the exact float; the game
    shows an integer, and that conversion belongs to the output layer
    (`output/json_output.py`)."""

    unit_score: float
    overall_power: float
    score_bonus_permil: float
    missing: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing


def deck_screen_unit_score(breakdown: ScoreBreakdown, *, k: float = UNIT_SCORE_K) -> DeckScreenScore:
    """Assemble `breakdown` into the deck screen's additive Unit Score: Overall Power is the
    stat block plus board power plus the two account scalars, and the Score Bonus is the
    plain sum of the bonus lines, board included."""
    missing: list[str] = []
    # The stat block is the FLOAT32-ceiling Overall Power sum when the breakdown
    # carries it (`_overall_power_stat_subtotals`); older/hand-built breakdowns
    # leave it `None` and fall back to the float64 `effective_stat_sum`.
    stat_block = (
        breakdown.effective_stat_sum
        if breakdown.overall_power_stat_sum is None
        else breakdown.overall_power_stat_sum
    )
    overall_power = stat_block + breakdown.board_power
    if breakdown.memory_bonus is None:
        missing.append("up_by_poster_collect")
    else:
        overall_power += breakdown.memory_bonus
    if breakdown.member_power_up is None:
        missing.append("card_training_evaluation")
    else:
        overall_power += breakdown.member_power_up

    # ADDITIVE — this is the line that differs from `total_score`.
    permil = (breakdown.combined_score_rate + breakdown.board_score_rate) * 1000.0

    return DeckScreenScore(
        unit_score=overall_power * (1.0 + permil / 1000.0) * k,
        overall_power=overall_power,
        score_bonus_permil=permil,
        missing=tuple(missing),
    )
