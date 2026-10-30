"""Phase A/B prototype — rerank-safe candidate generation for board-heavy runs.

The workflow the frontend wants is: take a large candidate set, apply the
expensive board contribution to each, and re-rank. Doing the expensive board
step for *every* team is the bottleneck, and cutting to "the top-N by the
board-less score" is **not** safe — the board can promote a team from outside
that top-N (a leader with a strong leader-scope board, gate nodes this team
alone satisfies, a Connect-boosted node).

This module makes the cut safe AND cheap using the structure of `board.py`:

* **Additive per node, separable per holomem.** Every board contribution is a
  sum of per-node effects, so the most a holomem's board can add is a
  per-holomem constant — precompute it ONCE (phase A).
* **Unit Score is monotone** in Overall Power and in every score-bonus channel.
  So an optimistic per-holomem ceiling, plugged into the Unit Score formula,
  bounds the team's board-on score from above (phase B). Rank by that bound,
  keep the top-N, run the expensive board pass on only those: nothing outside
  the top-N can beat the exact top-K, because its exact score <= its bound <=
  the N-th bound.

The board enters the score at FOUR structurally different places, and the bound
must cover all four or it is unsound (an early version that missed the ACTIVE
occupancy channels violated on 555/4000 sampled teams):

* **Overall Power**, split because leader-scope scales with team size:
  - member-scope power is a per-holomem constant (`member_power`);
  - leader-scope is a RATE/flat on *every* member, so it is bounded per team as
    `leader_scope_rate x MAX_TEAM_STAT_SUM + leader_scope_flat x MEMBER_COUNT`
    (`leader_scope`), and only the leader's applies.
* **ACTIVE pool** — `skill_effect_up` (multiplies magnitudes) and
  `activation_rate_up` / `cooldown_shorten` (multiply occupancy). Multiplicative
  on the bonus.
* **song_score_bonus** — a separate additive rate.

`bench/bench_board_ceiling.py` asserts the bound is never violated on the
sample, which is how a loose-but-valid bound is verified rather than assumed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from hbuilder.domain.board import BoardModel, STAT_NAMES
from hbuilder.domain.card import Card
from hbuilder.domain.team import MEMBER_COUNT


@dataclass(frozen=True, slots=True)
class HolomemCeiling:
    """The most a single holomem's board can add, over every team context —
    optimistic (every conditional gate assumed satisfied, best Connect)."""

    member_power: float   # Overall-Power addend that does NOT scale with team size
    leader_scope: float   # Overall-Power addend IF this holomem leads (already team-bounded)
    song_rate: float      # additive permil (song_score_bonus)
    effect_up: float      # permil; multiplies the ACTIVE pool
    act_rate: float       # fraction; raises ACTIVE occupancy (probability)
    cooldown: float       # fraction; shortens interval -> raises ACTIVE occupancy (uptime)


def _member_power(model: BoardModel, card: Card) -> float:
    """Board power this holomem brings AS A MEMBER (no leader-scope): the
    unconditional member/green terms (`power_bonus_for` with no leader) plus its
    conditional member-scope stat nodes, assumed to fire."""
    base = model.power_bonus_for((card,), None)
    cond = 0.0
    board = model.board_for(card.member)
    if board is not None:
        for node in board.nodes:
            for raw in node.effects:
                eff = raw.scaled(node.connect_multiplier)
                if not eff.conditional or eff.kind not in ("stat", "rate"):
                    continue
                if eff.scope == "leader":
                    continue  # handled in _leader_scope
                stats = STAT_NAMES if eff.stat == "all" else (eff.stat,)
                for stat in stats:
                    cond += math.ceil(eff.value * card.stat(stat)) if eff.kind == "rate" else eff.value
    return base + cond


def _leader_scope(model: BoardModel, card: Card, max_team_stat_sum: float) -> float:
    """Upper bound on the Overall Power this holomem's LEADER-scope board adds to
    a full team: a rate reaches all members' stats (bounded by the maximum
    possible team stat sum), a flat reaches each member once (x MEMBER_COUNT)."""
    total = 0.0
    board = model.board_for(card.member)
    if board is not None:
        for node in board.nodes:
            for raw in node.effects:
                eff = raw.scaled(node.connect_multiplier)
                if eff.scope != "leader" or eff.kind not in ("stat", "rate"):
                    continue
                span = len(STAT_NAMES) if eff.stat == "all" else 1
                if eff.kind == "rate":
                    total += eff.value * max_team_stat_sum
                else:  # flat, per member
                    total += eff.value * span * MEMBER_COUNT
    return total


def _rate_and_effect(model: BoardModel, holomem: str) -> tuple[float, float]:
    """Max board song-rate and skill-effect-up this holomem can contribute."""
    rate = effect = 0.0
    board = model.board_for(holomem)
    if board is not None:
        for node in board.nodes:
            for raw in node.effects:
                eff = raw.scaled(node.connect_multiplier)
                if eff.kind == "song_score_bonus":
                    rate += eff.value
                elif eff.kind == "skill_effect_up":
                    effect += eff.value
    return rate, effect


def holomem_ceilings(
    model: BoardModel,
    best_card_by_holomem: dict[str, Card],
    max_team_stat_sum: float,
) -> dict[str, HolomemCeiling]:
    """PHASE A — computed ONCE per board model. Uses each holomem's own
    highest-stat card so `rate` nodes are ceilinged, not under-counted."""
    out: dict[str, HolomemCeiling] = {}
    for holomem, card in best_card_by_holomem.items():
        song_rate, effect = _rate_and_effect(model, holomem)
        act_rate, cooldown = model.uptime_for(holomem)
        out[holomem] = HolomemCeiling(
            member_power=_member_power(model, card),
            leader_scope=_leader_scope(model, card, max_team_stat_sum),
            song_rate=song_rate,
            effect_up=effect,
            act_rate=act_rate,
            cooldown=cooldown,
        )
    return out


def unit_score_upper_bound(
    *,
    overall_power_no_board: float,
    score_bonus_rate_no_board: float,
    member_holomems: tuple[str, ...],
    leader_holomem: str,
    ceilings: dict[str, HolomemCeiling],
    k_constant: float,
) -> float:
    """PHASE B — a true upper bound on the team's board-on Unit Score.
    `Unit Score = OverallPower * (1 + ScoreBonus/1000) * K`, monotone in every
    board addition."""
    holomems = [h for h in (set(member_holomems) | {leader_holomem}) if h in ceilings]
    # Overall Power: every member's member-scope board + the single leader's leader-scope.
    power = overall_power_no_board + sum(ceilings[h].member_power for h in holomems)
    power += max((ceilings[h].leader_scope for h in holomems), default=0.0)
    # ACTIVE-pool channels multiply the bonus; scaling the whole no-board bonus
    # over-counts the non-active support terms, which only loosens the bound.
    eff_total = sum(ceilings[h].effect_up for h in holomems)          # permil
    act_max = max((ceilings[h].act_rate for h in holomems), default=0.0)
    cool_max = max((ceilings[h].cooldown for h in holomems), default=0.0)
    song_total = sum(ceilings[h].song_rate for h in holomems)         # permil
    bonus_ub = (
        score_bonus_rate_no_board
        * (1.0 + eff_total / 1000.0) * (1.0 + act_max) * (1.0 + cool_max)
        + song_total
    )
    return power * (1.0 + bonus_ub / 1000.0) * k_constant


def top_n_candidates(bounds: list[tuple[int, float]], n: int) -> list[int]:
    """Indices of the N teams with the highest upper bound — the only teams that
    can enter the exact top-K, so the only ones that need the expensive pass."""
    return [i for i, _ in sorted(bounds, key=lambda t: t[1], reverse=True)[:n]]
