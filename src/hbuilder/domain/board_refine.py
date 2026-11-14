"""Board refinement on top of the plans: re-ranking teams by their developed board
(`optimize_and_rerank`, `BoardOptimisedTeam`) and the joint choice of the activation-timing
nodes (`refine_activation_nodes`, `refine_cooldown_counts`, `develop_refined_team`).

Split out of `board_plan.py`; imported from here, not re-exported there (it builds on
board_plan)."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace
from typing import Any

from hbuilder.domain.board import BoardModel
from hbuilder.domain.board_node_values import (
    TeamBoardPlan,
    _chr_of,
    optimize_team_boards,
    singer_weight_for,
)
from hbuilder.domain.board_optimizer import (
    BoardPlan,
    _available,
    _cheapest_bundles,
    keep_preferred,
    optimize_board,
)
from hbuilder.domain.board_plan import (
    PlanCache,
    SocketPlacement,
    board_model_from_plan,
    cached_team_plan,
)
from hbuilder.domain.card import Card
from hbuilder.domain.scoring import (
    UNIT_SCORE_K,
    deck_screen_unit_score,
    score_with_breakdown,
)
from hbuilder.domain.scoring_config import ScoringConfig
from hbuilder.domain.skilltree import ROOT_NODE, SkillTree
from hbuilder.domain.team import Team


@dataclass(frozen=True, slots=True)
class BoardOptimisedTeam:
    """One team's board-optimised standing: its board-off Unit Score, the Unit
    Score once its own board is developed (RE-SCORED from the assembled plan, not
    the linearised per-node sum), the plan that gets there, and the assembled
    `BoardModel` itself so a caller can RE-SCORE it against anything else — e.g.
    a per-song chart — without re-developing the board."""

    team: Team
    board_off_us: float
    optimised_us: float
    plan: TeamBoardPlan
    board: BoardModel | None = None
    # Real Connect placements when the caller passed `connect_cards`; empty under
    # the idealised model (which places shapes, not actual cards).
    sockets: tuple[SocketPlacement, ...] = ()


def optimize_and_rerank(
    teams: Sequence[Team],
    cfg: ScoringConfig,
    *,
    tree_for: Callable[[Card], SkillTree | None],
    budget_for: Callable[[Card], int],
    node_effect_ids: Mapping[str, list[str]],
    skilltree: Mapping,
    connect_cards: Mapping[str, tuple[Sequence[Sequence[int]], int]] | None = None,
    bench_characters: Mapping[str, Sequence[Mapping]] | None = None,
    name_by_chr: Mapping[str, str] | None = None,
    plan_cache: PlanCache | None = None,
    sings: bool | AbstractSet[str] | Callable[[Card], bool | AbstractSet[str]] = False,
    bench_sockets: Mapping[str, Mapping[str, str | None]] | None = None,
    player_level: int | None = None,
    prefer: Mapping[str, frozenset[str]] | None = None,
    joint: bool = False,
) -> list[BoardOptimisedTeam]:
    """The bounded JOINT team+board pass: board-optimise every team and RE-RANK by
    the board-optimised Unit Score, highest first. `player_level`: the account's, which
    gates the nodes a plan may buy (None gates nothing; the plan cache carries its own).
    `prefer`: `{chr: nodes the account has}`, kept where it costs nothing
    (`optimize_board`; the plan cache carries its own).

    The board is 29-49 % of Overall Power and is per team, so a team a board-less
    ranking placed second may win once each board is developed. Every caller shares
    this one implementation, so they serve the same re-rank.

    `sings` is the singer context (`singer_weight_for`): a bool or a cast for the
    whole list, or a `Callable[[leader Card], ...]` when the teams have different
    leaders on one song. `plan_cache`, when given, takes every plan from
    `cached_team_plan` instead of re-planning per team (`optimize_team_boards`).
    Either way the ASSEMBLED board is re-scored (`board_model_from_plan`) for the
    exact optimised Unit Score; the plan's `total_gain` is only a linearised sum. `joint`:
    each team's Connect cards dealt together with the bench's (`board_model_from_plan`)."""
    cfg_off = replace(cfg, board=None)
    rows: list[BoardOptimisedTeam] = []
    for team in teams:
        bd = score_with_breakdown(team, cfg_off)
        ds = deck_screen_unit_score(bd)
        # The BARE active-pool rate a new skill_effect_up node multiplies:
        # active_pool carries the support boost, exactly (1 + combined_support_rate).
        bare_pool = bd.active_pool / (1.0 + bd.combined_support_rate)
        # a cast (frozenset) passes through: every singing board valued as a singer
        leader_sings = sings(team.leader) if callable(sings) else sings
        if not isinstance(leader_sings, (set, frozenset)):
            leader_sings = bool(leader_sings)
        if plan_cache is not None:
            plan = cached_team_plan(
                team.leader,
                team.members,
                cache=plan_cache,
                tree_for=tree_for,
                budget_for=budget_for,
                node_effect_ids=node_effect_ids,
                skilltree=skilltree,
                sings=leader_sings,
            )
        else:
            plan = optimize_team_boards(
                team.leader,
                team.members,
                tree_for=tree_for,
                budget_for=budget_for,
                node_effect_ids=node_effect_ids,
                overall_power=ds.overall_power,
                bonus_permil=ds.score_bonus_permil,
                k_constant=UNIT_SCORE_K,
                active_pool_rate=bare_pool,
                skilltree=skilltree,
                singer_weight=singer_weight_for(leader_sings, _chr_of(team.leader), True),
                singers=leader_sings if isinstance(leader_sings, (set, frozenset)) else None,
                player_level=player_level,
                prefer=prefer,
            )
        placements: list[SocketPlacement] = []
        board = board_model_from_plan(
            plan,
            team.leader,
            team.members,
            skilltree,
            node_effect_ids,
            overall_power=ds.overall_power,
            bonus_permil=ds.score_bonus_permil,
            k_constant=UNIT_SCORE_K,
            active_pool_rate=bare_pool,
            connect_cards=connect_cards,
            placements_out=placements,
            bench_characters=bench_characters,
            name_by_chr=name_by_chr,
            sings=leader_sings,
            plan_cache=plan_cache,
            bench_sockets=bench_sockets,
            joint=joint,
        )
        opt_us = deck_screen_unit_score(score_with_breakdown(team, replace(cfg_off, board=board))).unit_score
        rows.append(
            BoardOptimisedTeam(
                team=team,
                board_off_us=ds.unit_score,
                optimised_us=opt_us,
                plan=plan,
                board=board,
                sockets=tuple(placements),
            )
        )
    rows.sort(key=lambda r: r.optimised_us, reverse=True)
    return rows


# The two node families whose value is a matter of TIMING, not of size: the
# ACTIVE's activation frequency (cooldown) and its activation probability.
ACTIVATION_EFFECT_TYPES = frozenset({"Act.CD", "Act.Prob"})


def activation_node_ids(skilltree: Mapping) -> frozenset[str]:
    """Node ids whose effect is an activation-timing family
    (`ACTIVATION_EFFECT_TYPES`), read from the skill-tree dump's `nodes[*].e.t`."""
    return frozenset(
        node_id
        for node_id, meta in (skilltree.get("nodes") or {}).items()
        if ((meta or {}).get("e") or {}).get("t") in ACTIVATION_EFFECT_TYPES
    )


def cooldown_node_ids(skilltree: Mapping) -> frozenset[str]:
    """Node ids whose effect is the ACTIVE's activation frequency (`Act.CD`, the cooldown
    shortening), read from the skill-tree dump's `nodes[*].e.t`."""
    return frozenset(
        node_id
        for node_id, meta in (skilltree.get("nodes") or {}).items()
        if ((meta or {}).get("e") or {}).get("t") == "Act.CD"
    )


def _with_cooldown_subset(
    tree: SkillTree, selected: frozenset[str], family: frozenset[str], want: frozenset[str], available
) -> frozenset[str] | None:
    """`selected` with its `family` nodes replaced by `want`: the family nodes not wanted are
    dropped, each wanted node is bought with its cheapest path. `None` when the drop leaves
    bought nodes cut off from the root (the board would come apart), or a wanted node
    cannot be reached."""
    # dropped together, so a node and the one hanging from it can both go
    rest = frozenset(selected - ((selected & family) - want))
    if tree.reachable(rest) != rest:
        return None
    sel = set(rest)
    costs = {nid: tree.nodes[nid].cost for nid in tree.nodes}
    for node in sorted(want - frozenset(sel)):
        bundles = _cheapest_bundles(tree, frozenset(sel), available, costs)
        if node not in bundles:
            return None
        sel |= set(bundles[node][1]) | {node}
    return frozenset(sel)


def cooldown_count_axes(
    plan: TeamBoardPlan,
    cards_by_id: Mapping[str, Card],
    *,
    tree_for: Callable[[Card], SkillTree | None],
    budget_for: Callable[[Card], int],
    cooldown_nodes: frozenset[str],
    member_ids: frozenset[str],
    player_level: int | None = None,
    drops_only: bool = False,
    memo: dict | None = None,
) -> list[tuple[str, list[tuple[int, frozenset[str]]], int]]:
    """The activation-frequency counts each MEMBER's board can take around `plan`:
    `[(member card id, [(count, the board's nodes at that count)], its count now)]`, the
    counts ascending, for every member with at least two of them. Each count's variant
    is chosen as `refine_cooldown_counts` describes.

    `drops_only`: only the counts reached by dropping frequency nodes the board holds.
    `memo` (a dict the caller keeps): a member's counts are a function of its own board,
    so the plans of one team for several songs work them out once."""
    available_cache: dict[int, object] = {}
    axes: list[tuple[str, list[tuple[int, frozenset[str]]], int]] = []
    for card_id in plan.per_character:
        if card_id not in member_ids:
            continue
        card = cards_by_id.get(card_id)
        tree = tree_for(card) if card is not None else None
        if tree is None:
            continue
        family = frozenset(cooldown_nodes & set(tree.nodes))
        if not family:
            continue
        selected = frozenset(plan.per_character[card_id].selected)
        here = len(selected & family)
        key_m = (card_id, selected, drops_only)
        states = memo.get(key_m) if memo is not None else None
        if states is None:
            budget = budget_for(card)  # type: ignore[arg-type]
            available = available_cache.setdefault(id(tree), _available(tree, player_level))
            best_by_count: dict[int, tuple[tuple, frozenset[str]]] = {}
            nodes = sorted(family)
            for mask in range(1 << len(nodes)):
                want = frozenset(n for i, n in enumerate(nodes) if mask >> i & 1)
                if drops_only and not want <= selected:
                    continue
                sel = _with_cooldown_subset(tree, selected, family, want, available)
                if sel is None:
                    continue
                cost = tree.cost_of(sel - {ROOT_NODE})
                if cost > budget:
                    continue
                key = (len(sel ^ selected), cost, tuple(sorted(sel)))
                count = len(sel & family)
                if count not in best_by_count or key < best_by_count[count][0]:
                    best_by_count[count] = (key, sel)
            # no axis for a member with one count, or whose own board is not among them
            states = (
                [(count, best_by_count[count][1]) for count in sorted(best_by_count)]
                if len(best_by_count) >= 2 and here in best_by_count
                else []
            )
            if memo is not None:
                memo[key_m] = states
        if states:
            axes.append((card_id, states, here))
    return axes


def plan_with_counts(
    plan: TeamBoardPlan,
    axes: Sequence[tuple],
    combo: Sequence[int],
    cards_by_id: Mapping[str, Card],
    tree_for: Callable[[Card], SkillTree | None],
) -> TeamBoardPlan:
    """`plan` with each axis of `cooldown_count_axes` at the state `combo` names (an index
    into that member's counts). It carries no `plan_keys`: it is assembled by its nodes."""
    per = dict(plan.per_character)
    for (card_id, states, _here), k in zip(axes, combo):
        sel = states[k][1]
        old = plan.per_character[card_id]
        if sel != old.selected:
            tree = tree_for(cards_by_id[card_id])
            per[card_id] = BoardPlan(
                selected=sel,
                spent=tree.cost_of(sel - {ROOT_NODE}),  # type: ignore[union-attr]
                value=old.value,
                steps=old.steps,
            )
    return replace(plan, per_character=per, plan_keys={})


def refine_cooldown_counts(
    plan: TeamBoardPlan,
    cards_by_id: Mapping[str, Card],
    *,
    tree_for: Callable[[Card], SkillTree | None],
    budget_for: Callable[[Card], int],
    cooldown_nodes: frozenset[str],
    member_ids: frozenset[str],
    score: Callable[[TeamBoardPlan], float],
    start_value: float | None = None,
    player_level: int | None = None,
    cooldown_of: Callable[[TeamBoardPlan], Mapping[str, float]] | None = None,
    screen: Callable[[Sequence[Mapping[str, float]]], Sequence[float]] | None = None,
    verify: int = 4,
) -> tuple[TeamBoardPlan, float, list[str]]:
    """Choose how many activation-frequency (`Act.CD`) nodes each MEMBER keeps, all
    members jointly, by trying every combination on the real score.

    With `cooldown_of` (a plan -> each member card's cooldown shortening on that plan's
    board) and `screen` (a list of {member card: cooldown shortening} -> the score of the
    START board with each, in order), the combinations are first scored by `screen`, which
    re-assembles nothing, and only the best `verify` of them are scored by `score` on
    their own assembled board; the answer is always one `score` measured. Without them
    every combination is scored by `score`.

    A member's frequency nodes all carry the same value, so what moves the score is how
    many of them the member keeps (0 to 3), not which. And the counts act together: a
    shorter cooldown moves every later window of that member, and the score at each
    instant is the strongest live ACTIVE, so dropping one node on each of two strong
    members can gain while either drop alone loses. A move-by-move search
    (`refine_activation_nodes`) stops before such a combination; trying every combination
    of counts (at most 4^5 = 1024 for five members) does not.

    Per member, each count is the variant closest to the member's current board (fewest
    nodes changed, then the cheapest): a dropped node must not have bought nodes hanging
    from it, an added node comes with its cheapest path, and the board stays within the
    member's points. Only the members' boards are tried: a frequency node shortens its
    own holomem's ACTIVE, and the leader slot's own ACTIVE is not in the pool. A
    combination is kept only when the score is strictly higher, so ties keep the start
    and the result is never worse. Returns `(plan, its score, the changes made)`."""
    current = start_value if start_value is not None else score(plan)
    axes = cooldown_count_axes(
        plan,
        cards_by_id,
        tree_for=tree_for,
        budget_for=budget_for,
        cooldown_nodes=cooldown_nodes,
        member_ids=member_ids,
        player_level=player_level,
    )
    if not axes:
        return plan, current, []
    start = tuple(next(i for i, (c, _s) in enumerate(states) if c == here) for _cid, states, here in axes)
    best, best_value = start, current
    ranges = [range(len(states)) for _cid, states, _here in axes]

    def plan_for(combo: tuple[int, ...]) -> TeamBoardPlan:
        return plan_with_counts(plan, axes, combo, cards_by_id, tree_for)

    combos = [combo for combo in itertools.product(*ranges) if combo != start]
    if cooldown_of is not None and screen is not None:
        # each member state's own cooldown, read off a board where only that member moved
        start_cd = cooldown_of(plan)
        state_cd: list[list[float]] = []
        for a, (card_id, states, _here) in enumerate(axes):
            row = []
            for k in range(len(states)):
                if k == start[a]:
                    row.append(start_cd[card_id])
                else:
                    one = tuple(k if b == a else start[b] for b in range(len(axes)))
                    row.append(cooldown_of(plan_for(one))[card_id])
            state_cd.append(row)
        values = screen(
            [
                {card_id: state_cd[a][k] for a, ((card_id, _s, _h), k) in enumerate(zip(axes, combo))}
                for combo in combos
            ]
        )
        # best first, ties in enumeration order; only these are scored on their own board
        screened = sorted(zip(values, combos), key=lambda vc: -vc[0])
        combos = [combo for _v, combo in screened[:verify]]
    for combo in combos:
        value = score(plan_for(combo))
        if value > best_value:
            best, best_value = combo, value
    if best == start:
        return plan, current, []
    changes = [
        f"{card_id} Act.CD {states[s][0]}->{states[b][0]}"
        for (card_id, states, _here), s, b in zip(axes, start, best)
        if s != b
    ]
    return plan_for(best), best_value, changes


def refine_activation_nodes(
    plan: TeamBoardPlan,
    cards_by_id: Mapping[str, Card],
    *,
    tree_for: Callable[[Card], SkillTree | None],
    budget_for: Callable[[Card], int],
    activation_nodes: frozenset[str],
    score: Callable[[TeamBoardPlan], float],
    player_level: int | None = None,
    max_moves: int = 40,
) -> tuple[TeamBoardPlan, float, list[str]]:
    """Choose the team's activation nodes JOINTLY, on the real score.

    What a shorter cooldown (or a higher activation chance) is worth depends on
    where that member's windows then fall against the other members' windows and
    the chart's cues: the active Score UP is a per-tick MAX, so an extra activation
    can land on top of another and buy nothing, or shift a window off a cue. No
    per-node value captures that, and the greedy planner over-buys them (dropping
    some raises the song score by 0.2-3.1 % on the songs tried).

    Coordinate descent over single moves, first improvement kept: for each
    character in turn, DROP an activation node the rest of its board does not
    hang from, or ADD one (its cheapest path) that the character's points still
    cover. `score(plan)` is the caller's real objective (a song score on the board
    assembled from `plan`); a move is kept only when it raises it, so the result
    is never worse than the start. Freed points stay unspent.

    The returned plan carries no `plan_keys`: it is not one of the plan cache's
    plans (`board_model_from_plan` assembles it by its nodes). Returns
    `(plan, its score, the moves made)`."""
    best = score(plan)
    moves: list[str] = []
    if plan.plan_keys:
        plan = replace(plan, plan_keys={})
    improved = True
    while improved and len(moves) < max_moves:
        improved = False
        for card_id in list(plan.per_character):
            card = cards_by_id.get(card_id)
            tree = tree_for(card) if card is not None else None
            if tree is None:
                continue
            current = plan.per_character[card_id]
            selected = frozenset(current.selected)
            budget = budget_for(card)  # type: ignore[arg-type]
            spent = tree.cost_of(selected - {ROOT_NODE})
            candidates: list[tuple[str, frozenset[str]]] = []
            for node in sorted(selected & activation_nodes):
                rest = selected - {node}
                if tree.reachable(rest) == rest:
                    candidates.append((f"-{node}", rest))
            costs = {nid: tree.nodes[nid].cost for nid in tree.nodes}
            bundles = _cheapest_bundles(tree, selected, _available(tree, player_level), costs)
            for node in sorted(activation_nodes & set(bundles)):
                added_cost, added = bundles[node]
                if spent + added_cost <= budget:
                    candidates.append((f"+{node}", selected | added))
            for label, nodes in candidates:
                trial = replace(
                    plan,
                    per_character={
                        **plan.per_character,
                        card_id: BoardPlan(
                            selected=nodes,
                            spent=tree.cost_of(nodes - {ROOT_NODE}),
                            value=current.value,
                            steps=current.steps,
                        ),
                    },
                )
                value = score(trial)
                if value > best:
                    plan, best = trial, value
                    moves.append(f"{card_id} {label}")
                    improved = True
                    break
            if len(moves) >= max_moves:
                break
    return plan, best, moves


def respend_freed_points(
    start: TeamBoardPlan,
    plan: TeamBoardPlan,
    cards_by_id: Mapping[str, Card],
    *,
    tree_for: Callable[[Card], SkillTree | None],
    budget_for: Callable[[Card], int],
    values_for: Callable[[str], Mapping[str, float] | None],
    frozen: frozenset[str],
    player_level: int | None = None,
    memo: dict | None = None,
) -> tuple[TeamBoardPlan, list[str]]:
    """`plan` (a refinement of `start`) with the points its moves freed spent again.

    Dropping a frequency node frees its points, and on a board the plan could not fill
    (the account's levels) they can buy what the plan left out for want of points. Each
    board the refinement changed is grown by the greedy planner (`optimize_board`) from
    itself (`owned`), with the values its plan was made with (`values_for(card id)`, None:
    left as it is); the `frozen` nodes it does not hold are never bought, not even as a
    path, so the frequency counts the refinement chose stay. A board with every node of
    value already bought (the maxed budget) buys nothing.

    `memo` (a dict the caller keeps): a board's growth is a function of its own nodes and
    of its plan key, so the boards one team tries for several charts grow once. Returns `(plan, one change per
    board grown)`; `plan` itself when nothing is bought. The caller scores the result: a
    linear value is not the score."""
    per = dict(plan.per_character)
    changes: list[str] = []
    for card_id, p in plan.per_character.items():
        p0 = start.per_character.get(card_id)
        if p0 is None or p0.selected == p.selected:
            continue
        # the values follow the start's plan key (role, singer context)
        key = (card_id, p.selected, start.plan_keys.get(card_id))
        grown = memo.get(key) if memo is not None else None
        if grown is None:
            grown = p
            card = cards_by_id.get(card_id)
            tree = tree_for(card) if card is not None else None
            values = values_for(card_id) if tree is not None else None
            # nothing of value left to buy (every board on the maxed budget): no bundle can gain
            if values is not None and any(
                v > 0.0 for n, v in values.items() if n not in p.selected and n not in frozen
            ):
                budget = budget_for(card)  # type: ignore[arg-type]
                if tree.cost_of(p.selected - {ROOT_NODE}) < budget:  # type: ignore[union-attr]
                    bought = optimize_board(
                        tree,  # type: ignore[arg-type]
                        budget,
                        values,
                        player_level=player_level,
                        owned=frozenset(p.selected),
                        forbid=frozen - p.selected,
                    )
                    if bought.selected != p.selected:
                        grown = BoardPlan(
                            selected=bought.selected,
                            spent=bought.spent,
                            value=p.value,
                            steps=(*p.steps, *bought.steps),
                        )
            if memo is not None:
                memo[key] = grown
        if grown is not p:
            per[card_id] = grown
            changes.append(f"{card_id} re-spent +{' +'.join(sorted(grown.selected - p.selected))}")
    if not changes:
        return plan, []
    return replace(plan, per_character=per, plan_keys={}), changes


def plan_values(
    plan: TeamBoardPlan,
    cards_by_id: Mapping[str, Card],
    plan_cache: PlanCache,
    node_effect_ids: Mapping[str, list[str]],
    skilltree: Mapping | None,
) -> Callable[[str], Mapping[str, float] | None]:
    """`card id -> the node values its board in `plan` (a plan the cache made, with its
    `plan_keys`) was planned with`, None for a board the cache did not plan."""

    def values_for(card_id: str) -> Mapping[str, float] | None:
        key = plan.plan_keys.get(card_id)
        if key is None:
            return None
        # a member / leader+member key names its own card; a leader key names none
        card = cards_by_id.get(key[4]) if key[4] else cards_by_id.get(card_id)
        if card is None:
            return None
        return plan_cache.node_values_for_key(key, card, node_effect_ids=node_effect_ids, skilltree=skilltree)

    return values_for


def _keep_preferred_plan(
    plan: TeamBoardPlan,
    cards: Mapping[str, Card],
    prefer: Mapping[str, frozenset[str]],
    *,
    tree_for: Callable[[Card], SkillTree | None],
    budget_for: Callable[[Card], int],
    player_level: int | None,
) -> TeamBoardPlan:
    """`plan` with each board's unspent points keeping the account's own nodes
    (`keep_preferred`); `plan` itself when no board gains one."""
    per = dict(plan.per_character)
    changed = False
    for card_id, p in plan.per_character.items():
        card = cards.get(card_id)
        if card is None:
            continue
        own = prefer.get(_chr_of(card))
        tree = tree_for(card) if own else None
        if not own or tree is None:
            continue
        selected, spent, added = keep_preferred(
            tree, p.selected, p.spent, budget_for(card), own, player_level=player_level
        )
        if added:
            per[card_id] = BoardPlan(selected=selected, spent=spent, value=p.value, steps=(*p.steps, *added))
            changed = True
    return TeamBoardPlan(total_gain=plan.total_gain, per_character=per) if changed else plan


def develop_refined_team(
    team: Team,
    cfg: ScoringConfig,
    charts_score: Callable[[BoardModel], float],
    *,
    tree_for: Callable[[Card], SkillTree | None],
    budget_for: Callable[[Card], int],
    node_effect_ids: Mapping[str, list[str]],
    skilltree: Mapping,
    connect_cards: Mapping[str, tuple[Sequence[Sequence[int]], int]] | None = None,
    bench_characters: Mapping[str, Sequence[Mapping]] | None = None,
    name_by_chr: Mapping[str, str] | None = None,
    plan_cache: PlanCache | None = None,
    sings: bool | AbstractSet[str] = False,
    player_level: int | None = None,
    bench_sockets: Mapping[str, Mapping[str, str | None]] | None = None,
    cooldown_counts: bool = False,
    joint_connect: bool = False,
    charts_score_variants: Callable[[BoardModel, Sequence[Mapping[str, float]]], Sequence[float]]
    | None = None,
    prefer: Mapping[str, frozenset[str]] | None = None,
) -> tuple[BoardOptimisedTeam, float, list[str]]:
    """`optimize_and_rerank`'s develop for ONE team, then `refine_activation_nodes`
    on `charts_score(board)` (the caller's real objective, e.g. the song score on one
    chart or the sum over a few) and, with `cooldown_counts`, `refine_cooldown_counts`
    (up to ~1,000 scorings, so for a few finalists). `joint_connect` (with `bench_sockets` and
    a cast): every board it assembles has its Connect cards dealt together with the bench's
    (`board_model_from_plan(joint=True)`). On the plan cache, the points the
    refinement freed are then spent again with the values the boards were planned with
    (`respend_freed_points`, the frequency counts kept), when that scores higher. `prefer`:
    `{chr: nodes the account has}`: the develop keeps them where it costs nothing
    (`optimize_board`), and the points the refinement leaves keep them too when that scores
    no lower (`keep_preferred`).

    `charts_score_variants(board, [{holomem: cooldown shortening}, ...])`, when given,
    is `charts_score(board.with_cooldowns(v))` for every `v` at once
    (`song.score_song_cooldown_variants`); it only makes that screen cheaper. The
    board is re-assembled exactly as `optimize_and_rerank` assembles it (same
    linearisation point, Connect cards, bench, singer context). Returns the
    `BoardOptimisedTeam` of the refined board, `charts_score` of it, and the moves."""
    cfg_off = replace(cfg, board=None)
    bd = score_with_breakdown(team, cfg_off)
    ds = deck_screen_unit_score(bd)
    bare_pool = bd.active_pool / (1.0 + bd.combined_support_rate)
    start = optimize_and_rerank(
        [team],
        cfg,
        tree_for=tree_for,
        budget_for=budget_for,
        node_effect_ids=node_effect_ids,
        skilltree=skilltree,
        connect_cards=connect_cards,
        bench_characters=bench_characters,
        name_by_chr=name_by_chr,
        plan_cache=plan_cache,
        sings=sings,
        bench_sockets=bench_sockets,
        prefer=prefer,
        joint=joint_connect,
    )[0]

    def assemble(plan: TeamBoardPlan, placements: list | None = None) -> BoardModel | None:
        if plan is start.plan:
            if placements is not None:
                placements.extend(start.sockets)
            return start.board
        return board_model_from_plan(
            plan,
            team.leader,
            team.members,
            skilltree,
            node_effect_ids,
            overall_power=ds.overall_power,
            bonus_permil=ds.score_bonus_permil,
            k_constant=UNIT_SCORE_K,
            active_pool_rate=bare_pool,
            connect_cards=connect_cards,
            placements_out=placements,
            bench_characters=bench_characters,
            name_by_chr=name_by_chr,
            sings=sings,
            bench_sockets=bench_sockets,
            # a refined plan carries no `plan_keys` (`refine_activation_nodes`): the
            # compiled assembly takes it by its nodes, the Python's plain path otherwise
            plan_cache=plan_cache,
            joint=joint_connect,
        )

    cards = {c.id: c for c in (team.leader, *team.members)}
    plan, value, moves = refine_activation_nodes(
        start.plan,
        cards,
        tree_for=tree_for,
        budget_for=budget_for,
        activation_nodes=activation_node_ids(skilltree),
        score=lambda p: charts_score(assemble(p)),  # type: ignore[arg-type]
        player_level=player_level,
    )
    if cooldown_counts:
        members_by_id = {c.id: c for c in team.members}
        # the board `value` was measured on: the start's own when nothing moved
        base = start.board if not moves else assemble(plan)

        def cooldown_of(p: TeamBoardPlan) -> dict[str, float]:
            board = base if p is plan else assemble(p)
            return {
                cid: board.uptime_for(c.member)[1] if board is not None else 0.0
                for cid, c in members_by_id.items()
            }

        def screen(variants: Sequence[Mapping[str, float]]) -> list[float]:
            by_holomem = [{members_by_id[cid].member: cd for cid, cd in v.items()} for v in variants]
            if charts_score_variants is not None:
                return list(charts_score_variants(base, by_holomem))  # type: ignore[arg-type]
            return [charts_score(base.with_cooldowns(v)) for v in by_holomem]  # type: ignore[union-attr]

        plan, value, changes = refine_cooldown_counts(
            plan,
            cards,
            tree_for=tree_for,
            budget_for=budget_for,
            cooldown_nodes=cooldown_node_ids(skilltree),
            member_ids=frozenset(members_by_id),
            score=lambda p: charts_score(assemble(p)),  # type: ignore[arg-type]
            start_value=value,
            player_level=player_level,
            cooldown_of=cooldown_of if base is not None else None,
            screen=screen if base is not None else None,
        )
        moves = moves + changes
    if moves and plan_cache is not None and start.plan.plan_keys:
        respent, bought = respend_freed_points(
            start.plan,
            plan,
            cards,
            tree_for=tree_for,
            budget_for=budget_for,
            values_for=plan_values(start.plan, cards, plan_cache, node_effect_ids, skilltree),
            frozen=cooldown_node_ids(skilltree),
            player_level=player_level,
        )
        if bought:
            respent_value = charts_score(assemble(respent))  # type: ignore[arg-type]
            if respent_value > value:
                plan, value = respent, respent_value
                moves = [*moves, *bought]
    if prefer:
        # the points the refinement leaves keep the account's own nodes too, but not its
        # activation nodes: the refinement chose those on the real score and takes one off
        # when the team scores more without it (a shorter cooldown can fire the ACTIVE off
        # its best lattice); and only where the board then scores no lower
        activation = activation_node_ids(skilltree)
        kept = _keep_preferred_plan(
            plan,
            cards,
            {c: own - activation for c, own in prefer.items()},
            tree_for=tree_for,
            budget_for=budget_for,
            player_level=player_level,
        )
        if kept is not plan:
            kept_value = charts_score(assemble(kept))  # type: ignore[arg-type]
            if kept_value >= value:
                plan, value = kept, kept_value
                moves = [*moves, "kept the account's own nodes with the points left"]
    if not moves:  # unchanged: keep the start exactly (its plan-cache board)
        return start, value, moves
    placements: list[SocketPlacement] = []
    board = assemble(plan, placements)
    opt_us = deck_screen_unit_score(score_with_breakdown(team, replace(cfg_off, board=board))).unit_score
    return (
        BoardOptimisedTeam(
            team=team,
            board_off_us=ds.unit_score,
            optimised_us=opt_us,
            plan=plan,
            board=board,
            sockets=tuple(placements),
        ),
        value,
        moves,
    )


def develop_and_refine(
    team: Team, cfg: ScoringConfig, charts_score: Callable[[BoardModel], float], **kwargs: Any
) -> tuple[BoardModel | None, TeamBoardPlan, float, list[str]]:
    """`develop_refined_team` as `(board, plan, charts_score(board), moves)`."""
    bt, value, moves = develop_refined_team(team, cfg, charts_score, **kwargs)
    return bt.board, bt.plan, value, moves
