"""Bridge from the score model to the board-plan engine: assemble the real
`SkillTree`, and value each node in Unit-Score terms for one character in one
team, so `board_optimizer.optimize_board` can choose the board to develop.

Two halves, both pure (callers pass the already-decoded dumps, so `src/` holds
no dump paths):

* `skill_tree_from_dumps` joins the three sources `tree_from_mapping` needs —
  `skilltree.json`'s node table, one model's positions, and the point costs from
  master `SkillTreeNode` field 9 (they sum to 453).
* `power_node_values` values every node the way the optimiser needs, a
  linearisation around the team's board-off score across all four channels:
  the stat/power channel (`dUS/dPower x Delta power`, reach set by branch — R ->
  all members while leading, B -> its own card, G -> every card); the additive
  `song_score_bonus` rate; and the three ACTIVE-pool channels, which ride on the
  team's bare active pool (`ScoreBreakdown.active_pool / (1 + support)`) —
  `skill_effect_up` multiplies the whole pool, `activation_rate` / `cooldown`
  raise one member's occupancy (its ~1/5 share). Character-grouping greens are
  valued from skilltree's EXACT per-character `chr` override (value + which
  grouping), reaching the team members in that grouping — which also makes the
  per-character row exact, since every other node's effect is character-constant.
  Still a linearisation (marginal values around the board-off score).
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field, replace
from typing import Callable, Mapping, Sequence

from hbuilder.domain.team import Team, TeamError
from hbuilder.domain.skilltree import SkillTree, tree_from_mapping
from hbuilder.domain.board import (
    parse_effect_id, SONG_ALL_CAP_PERMIL, STAT_NAMES, _GENERATION_BY_GROUPING, _GROUPING_RE,
    board_from_mapping, BoardBase, BoardDigest, BoardModel, BoardNode, CharacterBoard,
)
from hbuilder.domain.skilltree import ROOT_NODE
from hbuilder.domain.card import Card
from hbuilder.domain.board_optimizer import BoardPlan, _available, _cheapest_bundles, optimize_board
from hbuilder.domain.scoring import (
    score_with_breakdown, deck_screen_unit_score, UNIT_SCORE_K,
)
from hbuilder.domain.scoring_config import ScoringConfig


def node_costs_from_master(master_rows: Sequence[Mapping]) -> dict[str, int]:
    """`group_id -> point cost` from master `SkillTreeNode` field 9."""
    costs: dict[str, int] = {}
    for row in master_rows:
        f9 = row.get("data", {}).get("9")
        if f9:
            costs[row["group_id"]] = int(f9[0])
    return costs


def node_effect_ids_from_master(master_rows: Sequence[Mapping]) -> dict[str, list[str]]:
    """`group_id -> [effect id string]` from master `SkillTreeNode` field 6."""
    out: dict[str, list[str]] = {}
    for row in master_rows:
        f6 = row.get("data", {}).get("6")
        if f6:
            out.setdefault(row["group_id"], [])
            for eff in f6:
                if isinstance(eff, str):
                    out[row["group_id"]].append(eff)
    return out


def skill_tree_from_dumps(
    skilltree_json: Mapping, master_rows: Sequence[Mapping], model_id: str
) -> SkillTree:
    """Assemble a `SkillTree` for one geometry model."""
    positions = skilltree_json["models"][model_id]
    costs = node_costs_from_master(master_rows)
    return tree_from_mapping(skilltree_json["nodes"], positions, costs)


def model_for_character(skilltree_json: Mapping, holomem_chr: str) -> str | None:
    """The geometry model this character's board uses."""
    for m in skilltree_json.get("members", []):
        if m.get("chr") == holomem_chr:
            return m.get("model")
    return None


def _grouping_node_value(
    override: Mapping, member_cards, d_us_per_power: float
) -> float:
    """A character-grouping green (G-008/011/021), valued from skilltree's EXACT
    per-character override: it grants +v to every stat of the team members in
    THIS character's own grouping (`chr[chr_id]` carries both the value and which
    grouping). Reaches only members whose generation matches."""
    match = _GROUPING_RE.match(override.get("tgt", ""))
    if not match:
        return 0.0
    generation = _GENERATION_BY_GROUPING.get(match.group("grouping"))
    if generation is None:
        return 0.0
    reached = sum(1 for c in member_cards if generation in c.generations)
    return float(override.get("v", 0)) * len(STAT_NAMES) * reached * d_us_per_power


# The generation greens (G-008/011/021), whose sum a card receives is capped at
# `board.GROUPING_ALL_CAP` over every source board.
GROUPING_GREEN_NODES = frozenset({"G-008", "G-011", "G-021"})
# The skilltree side key the leaf's planner reads: `{chr: room}`, the share of that
# character's generation chain still under the cap given the OTHER boards the
# leaf scores with (`grouping_room`). Valuation only -- nothing that scores reads it.
GROUPING_ROOM_KEY = "_grouping_room"
# and `{grouping: {chr: amount}}`: what the leaf's off-team boards supply
GROUPING_SUPPLY_KEY = "_grouping_supply"
# plan-key suffix of a plan that buys no generation green (`PlanCache.plan_for`)
NO_GROUPING = "no-grouping"
# what a generation green is worth in that variant: a hair below nothing, so a path
# through one loses to an equal path around it (a 0 left the tie to set order, which
# differs per process: the pool and the serial path planned different boards)
NO_GROUPING_VALUE = -1e-6


def grouping_room(skilltree: Mapping, bench_characters: Mapping | None,
                  name_by_chr: Mapping[str, str]) -> dict[str, dict[str, float]]:
    """`{chr: {node: share in [0, 1]}}`: how much of each character's G-008/011/021 chain
    still counts, the cap less what the OTHER boards of her grouping already supply
    (`bench_characters`, the boards the leaf scores the off-team holomems with: the
    account's own, or the developed bench under unlimited cubes, where every chain
    is bought and the cap is full). Team-invariant, so the per-character plan cache
    stays valid: the planner values a chain at its room instead of at full. What the
    TEAMMATES' chains take is `cached_team_plan`'s surplus pass; the score itself is
    exact (`board._close_grouping`)."""
    from .board import GROUPING_ALL_CAP  # noqa: PLC0415

    nodes = skilltree.get("nodes", {})
    # (chr, grouping) -> that character's whole chain INTO that grouping, and which
    # of her nodes feed it (a character's nodes can feed different groupings)
    chain: dict[tuple[str, str], float] = {}
    feeds: dict[tuple[str, str], list[str]] = {}
    for nid in sorted(GROUPING_GREEN_NODES):
        for cid, row in ((nodes.get(nid) or {}).get("chr") or {}).items():
            key = (cid, row.get("tgt", ""))
            chain[key] = chain.get(key, 0.0) + float(row.get("v") or 0)
            feeds.setdefault(key, []).append(nid)
    supply = grouping_supply(skilltree, bench_characters, name_by_chr)
    room: dict[str, dict[str, float]] = {}
    for (cid, tgt), total in chain.items():
        if total <= 0:
            continue
        others = sum(v for c, v in (supply.get(tgt) or {}).items() if c != cid)
        share = max(0.0, min(1.0, (GROUPING_ALL_CAP - others) / total))
        for nid in feeds[(cid, tgt)]:
            room.setdefault(cid, {})[nid] = share
    return room


def grouping_supply(skilltree: Mapping, bench_characters: Mapping | None,
                    name_by_chr: Mapping[str, str]) -> dict[str, dict[str, float]]:
    """`{grouping: {chr: amount}}`: the generation greens each board of
    `bench_characters` gives its grouping, Connect-scaled as the scorer reads them."""
    nodes = skilltree.get("nodes", {})
    chr_by_name = {v: k for k, v in (name_by_chr or {}).items()}
    supply: dict[str, dict[str, float]] = {}       # grouping -> {chr: owned amount}
    for name, rows in (bench_characters or {}).items():
        cid = chr_by_name.get(name)
        if cid is None:
            continue
        for row in rows or ():
            nid = row.get("node") if isinstance(row, Mapping) else None
            if nid in GROUPING_GREEN_NODES:
                over = ((nodes.get(nid) or {}).get("chr") or {}).get(cid)
                if over:
                    per = supply.setdefault(over.get("tgt", ""), {})
                    per[cid] = per.get(cid, 0.0) + float(over.get("v") or 0) * float(row.get("connect_multiplier") or 1.0)
    return supply


def _apply_grouping_room(values: dict[str, float], skilltree: Mapping | None,
                         own_chr: str) -> None:
    room = ((skilltree or {}).get(GROUPING_ROOM_KEY) or {}).get(own_chr)
    if not room:
        return
    for nid, share in room.items():
        if nid in values:
            values[nid] *= share


def _connect_multipliers(
    skilltree: Mapping, model_id: str, node_values: Mapping[str, float]
) -> dict[str, float]:
    """Per-node connect multiplier from OPTIMAL socketing. Each of the board's 4
    sockets sits at the base of a branch; a card placed there multiplies every
    unlocked node in that card's fixed shape (anchored at the socket) by
    `1 + v/1000` (bloom 5 = the higher of the two connect levels). Shapes never
    collide (0 of 6,936 shape pairs), so each socket is independent: it takes the
    card-shape that maximises the VALUE of the nodes it covers -- so the yellow
    socket, whose branch is not developed here, is simply never worth a card."""
    positions = skilltree.get("models", {}).get(model_id, {})
    if not positions:
        return {}
    extents = skilltree.get("extents", {})
    connect = skilltree.get("connect", {})
    card_connect = skilltree.get("card_connect", {})
    pos_to_node = {tuple(p): n for n, p in positions.items()}
    sockets = [n for n in positions if n.startswith("S")]
    # every distinct (shape, boost) a socketable card can provide, at max bloom.
    shapes: set = set()
    for _card, eff_id in card_connect.items():
        eff = connect.get(eff_id)
        if eff and eff.get("area") in extents:
            shapes.add((tuple(map(tuple, extents[eff["area"]])), eff["levels"][-1]["v"]))
    mult: dict[str, float] = {}
    for socket in sockets:
        sx, sy = positions[socket]
        best_gain, best = 0.0, None
        for offsets, boost in shapes:
            gain = 0.0
            for dx, dy in offsets:
                node = pos_to_node.get((sx + dx, sy + dy))
                if node:
                    gain += node_values.get(node, 0.0) * boost / 1000.0
            if gain > best_gain:
                best_gain, best = gain, (offsets, boost)
        if best:
            offsets, boost = best
            for dx, dy in offsets:
                node = pos_to_node.get((sx + dx, sy + dy))
                if node:
                    mult[node] = max(mult.get(node, 1.0), 1.0 + boost / 1000.0)
    return mult


# One node's PRE-PARSED valuation input: `(node_id, chr override or None,
# parsed effects)`. Compiling is a pure function of (node_effect_ids, the
# skilltree node table, own character) — the parse and the override lookup were
# most of `power_node_values`, and they do not depend on the team — so a plan
# cache keeps one compiled table per character and re-runs only
# the arithmetic per team. `power_node_values` compiles on the fly, so both
# paths share ONE arithmetic (`node_values_from_recipes`) and cannot drift.
NodeRecipe = tuple[str, Mapping | None, tuple]


def compile_node_recipes(
    node_effect_ids: Mapping[str, list[str]],
    skilltree_nodes: Mapping | None,
    own_chr: str,
) -> tuple[NodeRecipe, ...]:
    """`(node_id, override, effects)` per node, in `node_effect_ids` order. A
    truthy chr override (a character-grouping green) carries no parsed effects
    — it is valued whole by `_grouping_node_value`; every other node carries
    the effects that parse (an id this channel cannot route is skipped, never
    guessed)."""
    out: list[NodeRecipe] = []
    for node_id, effect_ids in node_effect_ids.items():
        branch = node_id[:1]
        # A character-grouping node's exact effect is per-character; take it from
        # skilltree's chr override (the master rows are every grouping's variant).
        # Only a grouping green's override names a grouping (`tgt`): Fuwawa's 12 solo
        # yellows carry a chr override too (the same effect as the master rows), and
        # read as a grouping they would be worth 0 to her plan and sockets.
        override = (
            skilltree_nodes.get(node_id, {}).get("chr", {}).get(own_chr)
            if skilltree_nodes is not None else None
        )
        if override and "tgt" in override:
            out.append((node_id, override, ()))
            continue
        effs = []
        for eff_id in effect_ids:
            try:
                eff = parse_effect_id(eff_id, branch=branch)
            except Exception:
                continue  # an effect shape this channel does not value
            if eff is None:
                continue
            effs.append(eff)
        out.append((node_id, None, tuple(effs)))
    return tuple(out)


def node_values_from_recipes(
    recipes: Sequence[NodeRecipe],
    *,
    is_leader: bool,
    own_card: Card | None,
    member_cards: Sequence[Card],
    bonus_permil: float,
    overall_power: float,
    k_constant: float,
    active_pool_rate: float = 0.0,
    singer_weight: float = 0.0,
    song_scope: str = "",
) -> dict[str, float]:
    """The arithmetic half of `power_node_values` (connect OFF) over compiled
    recipes, in recipe order. Bit-identical to `power_node_values`' own loop:
    the same accumulation order per effect, the same `max` over variants."""
    d_us_per_power = (1.0 + bonus_permil / 1000.0) * k_constant
    d_us_per_bonus = overall_power * k_constant   # per unit of score-bonus rate
    # per unit of the SONG's score rate (a yellow): it multiplies every note
    # (`song.py`, `* (1.0 + board_score_rate)`), i.e. the whole Unit Score
    # P * K * (1 + b), not a summand of the deck screen's Score Bonus. Priced at
    # `d_us_per_bonus` a singer's own yellows would be worth (1 + b) ~ 1.9x too
    # little, so a singing LEADER's board would buy reds instead of her solo yellows
    # and her yellow socket (e.g. Nene leading on m0020: 2,349,358 instead of
    # 2,540,989 re-planned exactly).
    d_us_per_song = overall_power * d_us_per_power
    # (no per-PERMIL constant: every non-stat `BoardEffect.value` is already a
    #  fraction — see the yellow branch below.)
    member_cards = tuple(member_cards)
    own_in_members = own_card is not None and own_card in member_cards
    values: dict[str, float] = {}
    for node_id, override, effs in recipes:
        if override:
            values[node_id] = _grouping_node_value(override, member_cards, d_us_per_power)
            continue
        # master carries one row PER character/grouping variant of the node; only
        # one applies to a given board, so take the MAX over variants, not a sum.
        best = 0.0
        for eff in effs:
            # Which cards this node's effect reaches.
            if eff.scope == "leader":
                reached = member_cards if is_leader else ()
            elif eff.scope == "member":
                # Blues reach the card that SCORES: only a member card, never a
                # leader-only card (the leader slot adds no Member Parameter).
                reached = (own_card,) if own_in_members else ()
            elif eff.scope == "grouping":
                continue  # needs the team's grouping composition — a follow-up
            else:  # "all"/general and account-wide
                reached = member_cards
            row = 0.0
            if eff.kind in ("stat", "rate"):
                stats = STAT_NAMES if eff.stat == "all" else (eff.stat,)
                if eff.kind == "rate":
                    for card in reached:
                        for stat in stats:
                            row += math.ceil(eff.value * card.stat(stat)) * d_us_per_power
                else:
                    # the same addend `len(reached) * len(stats)` times, in the
                    # same order — one product, repeated addition, same float.
                    add = eff.value * d_us_per_power
                    for _ in range(len(reached) * len(stats)):
                        row += add
            elif eff.kind == "song_score_bonus" and reached:
                # Y: `solo`/`group` count only when this holomem sings a song of
                # that scope; `all` is the pooled bucket every score counts.
                #
                # ⚠ a per-unit constant, NOT a per-permil one.
                # `BoardEffect.value` is a FRACTION for every non-stat kind —
                # `board.py` states the rule as "so no caller divides by 1000
                # twice" — and a per-permil constant would divide it a second
                # time, making yellow nodes worth ~1/1000 of their real value.
                # The two siblings on this same rate axis (`skill_effect_up`
                # below, `activation_rate_up`) use the per-unit constant too.
                # `song_scope` (the song's `singer_type`, `SongCast.scope`): the
                # scorer pays ONE scope per song, so a singer's yellows of the other
                # scope are worth 0 (e.g. Kiara's solo yellows on the group song
                # m0198); "" (not known) keeps both.
                # The pooled `all` yellows pay only when the song's scope is not
                # solo/group: a cast's own scope SUPERSEDES that bucket
                # (`song.py`, `song_score_bonus_for`), so on a solo or group song
                # they are worth 0 (otherwise the leaf buys them as leftovers).
                if eff.song_scope in ("solo", "group"):
                    scope_weight = (singer_weight if song_scope in ("", eff.song_scope)
                                    else 0.0)
                else:
                    scope_weight = 0.0 if song_scope in ("solo", "group") else 1.0
                row += eff.value * d_us_per_song * scope_weight
            elif eff.kind == "skill_effect_up" and reached:
                # One team-wide addend that multiplies the whole ACTIVE pool.
                row += active_pool_rate * eff.value * d_us_per_bonus
            elif eff.kind in ("activation_rate_up", "cooldown_shorten"):
                # Raises one member's active occupancy -> about its share of the
                # pool, and only if that member actually fields an active in it
                # (a pure outfit-leader's own active is not in the pool).
                if own_in_members and member_cards:
                    row += (active_pool_rate / len(member_cards)) * eff.value * d_us_per_bonus
            # A `music_skill_tree_character` trigger fires only when this
            # holomem sings the song: worth `singer_weight` of its full value.
            if eff.conditional:
                row *= singer_weight
            best = max(best, row)
        values[node_id] = best
    return values


def power_node_values(
    node_effect_ids: Mapping[str, list[str]],
    *,
    is_leader: bool,
    own_card: Card | None,
    member_cards: Sequence[Card],
    bonus_permil: float,
    overall_power: float,
    k_constant: float,
    active_pool_rate: float = 0.0,
    skilltree: Mapping | None = None,
    model_id: str = "",
    own_chr: str = "",
    singer_weight: float = 0.0,
    song_scope: str = "",
) -> dict[str, float]:
    """Delta Unit Score of buying each node, over all four board channels.

    `Unit Score = Power * (1 + bonus/1000) * K`, so a power add is worth
    `(1 + bonus/1000) * K` per point, a permil of score-rate `Power * K / 1000`,
    and a whole unit of score-BONUS rate `Power * K`. The reach of a node is set
    by its branch: R (leader) reaches all members but only when this character
    leads; B reaches its own card — and only when that card is one of the
    MEMBERS (the leader slot contributes no Member Parameter, `team.py`; a
    leader-only holomem's blues reach nobody); G reaches
    every member.

    `singer_weight` is the SINGER CONTEXT of the plan (the board plan is a
    fixed function of (character, role, budget, singer context)). Every `BoardEffect.conditional` node — R-002 and
    R-044/062/063, the `music_skill_tree_character` passive trigger that fires
    only when this holomem sings the song (`board.py` `parse_effect_id`,
    `conditional_stat_power_for`) — is valued TIMES `singer_weight`: 0.0
    (the default) is the song-agnostic plan where they never fire, 1.0 is the
    plan for a song this holomem sings as leader. The yellow `song_score_bonus`
    nodes follow the same rule by song scope: `solo` / `group` nodes count only
    through `song_score_bonus_for(singers, scope=singer_type)` when this
    holomem is IN the song's cast and the song is of that type, so they carry
    `singer_weight`; an `all`-scope node is the pooled bucket every song-agnostic
    score (`_board_bonus`) already counts — "pooling is exact for a whole-roster
    song" — so it keeps weight 1.0.

    The three ACTIVE-pool channels ride on `active_pool_rate` (the team's ACTIVE
    Score-UP pool, `ScoreBreakdown.active_pool`): `skill_effect_up` multiplies
    the whole pool (`pool * V`), while `activation_rate_up` / `cooldown_shorten`
    raise one member's occupancy, worth about that member's share `pool /
    MEMBER_COUNT * V`. A leader that fields no active (a pure outfit-leader) owns
    no share, so its own-card active channels are valued at zero."""
    recipes = compile_node_recipes(
        node_effect_ids, skilltree["nodes"] if skilltree else None, own_chr)
    values = node_values_from_recipes(
        recipes, is_leader=is_leader, own_card=own_card, member_cards=member_cards,
        bonus_permil=bonus_permil, overall_power=overall_power,
        k_constant=k_constant, active_pool_rate=active_pool_rate,
        singer_weight=singer_weight, song_scope=song_scope,
    )
    _apply_grouping_room(values, skilltree, own_chr)
    # CONNECT: a socketed card multiplies the nodes in its shape; the socket
    # assignment maximises the value it lifts. Doubles a maxed board's worth.
    if skilltree and model_id:
        for node_id, m in _connect_multipliers(skilltree, model_id, values).items():
            if node_id in values:
                values[node_id] *= m
    return values


def bench_node_values(
    node_effect_ids: Mapping[str, list[str]],
    *,
    member_cards: Sequence[Card],
    bonus_permil: float,
    overall_power: float,
    k_constant: float,
    skilltree: Mapping | None = None,
    model_id: str = "",
    own_chr: str = "",
    cast=None,
) -> dict[str, float]:
    """`power_node_values` for a character who is NOT on the team (role
    `bench`): its account-global GREENs reach the
    team — its reds need it to lead, its blues need its card to be a member —
    so those are worth 0. Its song YELLOWS reach the song's score whoever is on
    the team (`bench_yellow_pays`): with `cast` (the song's singers; empty for a
    song with no cast) they keep their value (valued 0, the bench singer's
    yellow socket would lose its card: -2.29 % on every top-16 m0131 team);
    `cast` None keeps the greens only.
    `member_cards` are the cards the greens reach (the team, or the nominal
    roster a plan cache values at). Connect multipliers are then applied to the
    kept values, so a socket is placed for what a bench board really lifts."""
    values = power_node_values(
        node_effect_ids, is_leader=False, own_card=None, member_cards=member_cards,
        bonus_permil=bonus_permil, overall_power=overall_power,
        k_constant=k_constant, active_pool_rate=0.0,
        skilltree={"nodes": skilltree["nodes"]} if skilltree else None,
        model_id="", own_chr=own_chr, singer_weight=0.0 if cast is None else 1.0,
        song_scope=song_scope_of(cast),
    )
    values = {n: (v if n.startswith("G") or (
                  skilltree and bench_yellow_pays(yellow_scope(skilltree, n, own_chr), own_chr, cast))
                  else 0.0)
              for n, v in values.items()}
    _apply_grouping_room(values, skilltree, own_chr)
    if skilltree and model_id:
        for node_id, m in _connect_multipliers(skilltree, model_id, values).items():
            if node_id in values:
                values[node_id] *= m
    return values


@dataclass(frozen=True, slots=True)
class TeamBoardPlan:
    """The board development chosen across a whole team and the Unit-Score gain
    it realises. Each character's board is a SEPARATE point pool funded by that
    character's own level, so the team plan is the sum of six
    independent per-character plans — the leader valued while leading (its red
    board reaches all members), each member valued on its own card."""

    total_gain: float
    per_character: Mapping[str, BoardPlan]
    # `card id -> plan_key(...)` when the plans came from a `PlanCache`
    # (`cached_team_plan`); empty for a per-team plan. Lets
    # `board_model_from_plan(plan_cache=...)` find each character's ASSEMBLED
    # pieces (`PlanCache.assembled_for`) without re-deriving the key.
    plan_keys: Mapping[str, tuple] = field(default_factory=dict)


def optimize_team_boards(
    leader: Card,
    members: Sequence[Card],
    *,
    tree_for: Callable[[Card], SkillTree | None],
    budget_for: Callable[[Card], int],
    node_effect_ids: Mapping[str, list[str]],
    overall_power: float,
    bonus_permil: float,
    k_constant: float,
    active_pool_rate: float = 0.0,
    skilltree: Mapping | None = None,
    player_level: int | None = None,
    singer_weight: float = 0.0,
    singers: frozenset | None = None,
) -> TeamBoardPlan:
    """Optimise every distinct character's board for this team and sum the gain.

    `singer_weight` is the LEADER's singer context (`power_node_values`): 1.0
    when the leader is a credited singer of the song this plan is for, 0.0 for
    the song-agnostic plan. A member is valued as a singer when `singers` (the
    song's cast, chr ids) holds her holomem -- her own solo/group yellows pay on
    her board whoever leads; without it, members are valued at 0.0.

    The phase-B bound (`board_ceiling`) still bounds this: an optimised board is
    a subset of the full board, so `optimised <= full <= ceiling`, and the
    rerank-safe cut holds unchanged — only the top-N candidates ever reach here."""
    members = tuple(members)
    # ONE plan per distinct CHARACTER (the game has one board per holomem),
    # leader first, so a leader whose holomem also fields a member card is
    # valued as the leader (her reds reach the team because she leads) AND
    # for that member card (her blues reach the card that scores) — instead of
    # two plans, the member's overwriting the leader's name-keyed board and
    # dropping every red (e.g. Calli leading with a Calli member card would lose
    # R-044/062/063 and score no singer power on her own song).
    by_chr = {"chr-" + m.id.split("-")[1]: m for m in members}
    seen: set[str] = set()
    plans: dict[str, BoardPlan] = {}
    values_by_chr: dict[str, tuple] = {}   # members holding a generation chain
    total = 0.0
    for card, is_leader in [(leader, True), *[(m, False) for m in members]]:
        chr_id = "chr-" + card.id.split("-")[1]
        if chr_id in seen:
            continue
        seen.add(chr_id)
        tree = tree_for(card)
        if tree is None:
            continue
        values = power_node_values(
            node_effect_ids,
            is_leader=is_leader,
            own_card=by_chr.get(chr_id, card),
            member_cards=members,
            bonus_permil=bonus_permil,
            overall_power=overall_power,
            k_constant=k_constant,
            active_pool_rate=active_pool_rate,
            skilltree=skilltree,
            model_id=(model_for_character(skilltree, "chr-" + card.id.split("-")[1])
                      or "") if skilltree else "",
            own_chr="chr-" + card.id.split("-")[1],
            singer_weight=(singer_weight if is_leader
                           else singer_weight_for(singers or frozenset(), chr_id, False)),
            song_scope=song_scope_of(singers),
        )
        plan = optimize_board(tree, budget_for(card), values, player_level=player_level)
        if not is_leader and chr_id != _chr_of(leader):
            # the grouping cap across the team (`cached_team_plan`'s surplus pass)
            if _grouping_chain(plan.selected, chr_id, skilltree):
                values_by_chr[chr_id] = (card, tree, values)
        plans[card.id] = plan
        total += plan.value
    for card in sorted((values_by_chr[c][0] for c in values_by_chr),
                       key=lambda c: (budget_for(c), c.id)):
        if not _chain_is_surplus(card, plans, members, leader, skilltree):
            continue
        _card, tree, values = values_by_chr[_chr_of(card)]
        values = {n: (NO_GROUPING_VALUE if n in GROUPING_GREEN_NODES else v) for n, v in values.items()}
        plan = optimize_board(tree, budget_for(card), values, player_level=player_level)
        total += plan.value - plans[card.id].value
        plans[card.id] = plan
    return TeamBoardPlan(total_gain=total, per_character=plans)


# ---------------------------------------------------------------------------
# The plan cache: one board plan per (character, role, budget, singer context).
#
# The board plan is a FIXED function of (character,
# role, budget, singer context) — role in {leader, member, bench} (+ the leader
# who ALSO fields a member card, whose blues then reach that card), singer
# context = whether the character, as leader, is a credited singer of the song
# being solved. So a plan is computed ONCE at a NOMINAL team context and reused
# for every team the character appears in with that role/budget/context; the
# per-team exact number comes from RE-SCORING the assembled board
# (`board_model_from_plan`), never from the nominal linearised sum. Consequence:
# the per-song objective becomes a pure function of the roster.
# ---------------------------------------------------------------------------

PLAN_ROLE_LEADER = "leader"                # leads, no card of hers among the members
PLAN_ROLE_LEADER_MEMBER = "leader+member"  # leads AND fields a member card
PLAN_ROLE_MEMBER = "member"
PLAN_ROLE_BENCH = "bench"
PLAN_ROLES = (PLAN_ROLE_LEADER, PLAN_ROLE_LEADER_MEMBER, PLAN_ROLE_MEMBER,
              PLAN_ROLE_BENCH)


def _chr_of(card: Card) -> str:
    return "chr-" + card.id.split("-")[1]


def singer_weight_for(sings, chr_id: str, is_leader: bool) -> float:
    """The singer context of one board: `sings` is either the LEADER's flag (a bool: only
    the leader's board is valued as a singer) or the song's whole cast as a `frozenset` of chr
    ids, which values EVERY board whose holomem sings -- a member singer's solo/group yellows
    and her yellow socket included (otherwise, e.g., Towa as a member on her own solo m0365
    has all 12 yellows bought but the Connect assignment values them 0 and leaves her yellow
    socket empty, -39,254 against the server)."""
    if isinstance(sings, (set, frozenset)):
        return 1.0 if chr_id in sings else 0.0
    return 1.0 if (is_leader and sings) else 0.0


def song_scope_of(sings) -> str:
    """The song's scope a `sings` context carries (`SongCast.scope`), "" when it
    carries none (a leader flag, a plain set, no cast)."""
    return getattr(sings, "scope", "") or ""


def sings_key(sings, chr_id: str, is_leader: bool) -> bool | str:
    """One board's singer context as a plan-cache key part: False when it does not
    sing, else the song's scope (`song_scope_of`) -- a plan for a solo song values
    the solo yellows and one for a group song the group ones, so they are cached
    apart -- or True when the scope is not known."""
    if singer_weight_for(sings, chr_id, is_leader) <= 0:
        return False
    return song_scope_of(sings) or True


def plan_key(card: Card, role: str, budget: int, sings: bool | str) -> tuple:
    """The cache key of one board plan: `(chr, role, budget, sings, own_card)`.

    `card` is the card whose stats the plan's BLUE nodes use — the member card
    for `member` / `leader+member`, the leader card for `leader`, any card of
    the character for `bench`. `own_card` is part of the key only for the roles
    whose blues reach a card (a 4-star and a 5-star of the same holomem value
    their blues differently); for `leader` / `bench` it is `""` so every card
    of the character shares the plan. `sings` is this board's OWN singer context:
    meaningful for a leader role (the conditional reds) and for a member (her
    solo/group yellows; `singer_weight_for`); a bench plan ignores it."""
    if role not in PLAN_ROLES:
        raise ValueError(f"unknown plan role {role!r}; expected one of {PLAN_ROLES}")
    uses_blues = role in (PLAN_ROLE_MEMBER, PLAN_ROLE_LEADER_MEMBER)
    leads = role in (PLAN_ROLE_LEADER, PLAN_ROLE_LEADER_MEMBER)
    context = (sings if isinstance(sings, str) and sings else bool(sings))
    # a bench plan keeps its singer context too: an off-team singer's yellows pay on
    # her song (`singer_bench_characters`)
    sung = leads or uses_blues or role == PLAN_ROLE_BENCH
    return (_chr_of(card), role, int(budget), context if sung else False,
            card.id if uses_blues else "")


# `PlanCache._team_boards` is emptied when it reaches this many developed
# boards. A bound on memory only — an entry is a pure function of its key, so
# dropping one costs a rebuild and can never change a score.
_TEAM_BOARDS_MAX = 20_000
_NO_MULTS: frozenset = frozenset()


@dataclass(slots=True)
class PlanCache:
    """Per-(character, role, budget, singer context) board plans, valued at ONE
    nominal team context and reused across teams.

    The NOMINAL CONTEXT is what `power_node_values` linearises around, fixed
    at construction: `nominal_members` (the cards a leader's reds / a bench's
    greens reach — five typical member cards; for a member or leader+member
    plan the character's own card REPLACES one of them so its blues and its
    active-pool share are valued as a member's), `overall_power`,
    `bonus_permil` and `active_pool_rate` (the linearisation point:
    `dUS/dPower`, `dUS/dpermil`, the bare ACTIVE pool). `from_team` derives
    them from one reference team's board-off breakdown.

    `hits` / `misses` count lookups so a caller can verify reuse."""

    nominal_members: tuple[Card, ...]
    overall_power: float
    bonus_permil: float
    active_pool_rate: float = 0.0
    k_constant: float = UNIT_SCORE_K
    player_level: int | None = None
    plans: dict = None  # type: ignore[assignment]
    hits: int = 0
    misses: int = 0
    # The ASSEMBLED, team-invariant pieces of each plan (`AssembledPlan`),
    # keyed like `plans`; filled lazily by `assembled_for`. Rebuilt whenever the
    # (node_effect_ids, skilltree, connect_cards) objects they were derived
    # from change identity — the cache holds those references, so an id cannot
    # be recycled under it.
    assembled: dict = None  # type: ignore[assignment]
    _assembled_src: tuple = ()
    _recipes: dict = None  # type: ignore[assignment]   # own_chr -> recipes
    _connect_groups: tuple = ()  # (shape offsets, permil, sorted card ids) per group
    _bench: tuple = ()  # (bench mapping object, parsed {name: CharacterBoard})
    _bench_digests: tuple = ()  # (the parsed dict above, {name: BoardDigest})
    _bench_base: tuple = ()  # (the digests dict above, BoardBase over it)
    # (plan key, holomem, per-node connect multipliers) -> (CharacterBoard,
    # BoardDigest): a developed board as `_board_model_from_cache` assembles
    # it. Emptied with `assembled`, whose nodes it is built from.
    _team_boards: dict = None  # type: ignore[assignment]
    # plan key -> that character's recipes cut down to `AssembledPlan.needed`.
    # Emptied with `assembled` and `_recipes`, which it is a function of.
    _needed_recipes: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.nominal_members = tuple(self.nominal_members)
        if self.plans is None:
            self.plans = {}
        if self.assembled is None:
            self.assembled = {}
        if self._recipes is None:
            self._recipes = {}
        if self._team_boards is None:
            self._team_boards = {}
        if self._needed_recipes is None:
            self._needed_recipes = {}

    @classmethod
    def from_team(cls, team: Team, cfg: ScoringConfig, *,
                  player_level: int | None = None) -> "PlanCache":
        """A cache whose nominal context is `team`'s board-off standing: its
        five members as the reached roster, its Overall Power / Score Bonus as
        the linearisation point, its bare ACTIVE pool for the pool channels."""
        bd = score_with_breakdown(team, replace(cfg, board=None))
        ds = deck_screen_unit_score(bd)
        return cls(
            nominal_members=tuple(team.members),
            overall_power=ds.overall_power, bonus_permil=ds.score_bonus_permil,
            active_pool_rate=bd.active_pool / (1.0 + bd.combined_support_rate),
            player_level=player_level,
        )

    def _reached_for(self, card: Card, role: str) -> tuple[Card, ...]:
        """The member roster the plan is valued against: the nominal five, with
        this character's own card standing in for one of them when its role
        puts a card of hers among the members."""
        chr_id = _chr_of(card)
        others = tuple(m for m in self.nominal_members if _chr_of(m) != chr_id)
        if role in (PLAN_ROLE_MEMBER, PLAN_ROLE_LEADER_MEMBER):
            return (card, *others[:max(0, len(self.nominal_members) - 1)])
        return others

    def plan_for(
        self, card: Card, role: str, budget: int, sings: bool, *,
        tree: SkillTree, node_effect_ids: Mapping[str, list[str]],
        skilltree: Mapping | None, no_grouping: bool = False,
    ) -> BoardPlan:
        """The cached plan for `plan_key(card, role, budget, sings)`, computed
        at the nominal context on a miss. `no_grouping`: the variant that buys no
        generation green (its grouping is already capped by the rest of the team,
        `cached_team_plan`), keyed apart."""
        key = plan_key(card, role, budget, sings) + ((NO_GROUPING,) if no_grouping else ())
        plan = self.plans.get(key)
        if plan is not None:
            self.hits += 1
            return plan
        self.misses += 1
        chr_id = _chr_of(card)
        model_id = (model_for_character(skilltree, chr_id) or "") if skilltree else ""
        reached = self._reached_for(card, role)
        if role == PLAN_ROLE_BENCH:
            # sings: her song's scope (or True, not known) -- her yellows of it pay
            cast = None if not key[3] else (
                SongCast({chr_id}, key[3]) if isinstance(key[3], str) else frozenset({chr_id}))
            values = bench_node_values(
                node_effect_ids, member_cards=reached,
                bonus_permil=self.bonus_permil, overall_power=self.overall_power,
                k_constant=self.k_constant, skilltree=skilltree,
                model_id=model_id, own_chr=chr_id, cast=cast,
            )
        else:
            values = power_node_values(
                node_effect_ids,
                is_leader=role in (PLAN_ROLE_LEADER, PLAN_ROLE_LEADER_MEMBER),
                own_card=card, member_cards=reached,
                bonus_permil=self.bonus_permil, overall_power=self.overall_power,
                k_constant=self.k_constant, active_pool_rate=self.active_pool_rate,
                skilltree=skilltree, model_id=model_id, own_chr=chr_id,
                singer_weight=1.0 if key[3] else 0.0,
                song_scope=key[3] if isinstance(key[3], str) else "",
            )
        if no_grouping:
            values = {n: (NO_GROUPING_VALUE if n in GROUPING_GREEN_NODES else v) for n, v in values.items()}
        plan = optimize_board(tree, budget, values, player_level=self.player_level)
        self.plans[key] = plan
        return plan

    # -- the assembled cache ------------------------------------------------
    def _sync_sources(self, node_effect_ids, skilltree, connect_cards) -> None:
        src = (node_effect_ids, skilltree, connect_cards)
        if self._assembled_src and all(a is b for a, b in zip(self._assembled_src, src)):
            return
        self._assembled_src = src
        self.assembled = {}
        self._recipes = {}
        self._team_boards = {}
        self._needed_recipes = {}
        groups: dict[tuple, list[str]] = {}
        for card_id, (offsets, permil) in (connect_cards or {}).items():
            groups.setdefault((tuple(tuple(o) for o in offsets), permil), []).append(card_id)
        self._connect_groups = tuple(
            (offsets, permil, tuple(sorted(ids))) for (offsets, permil), ids in groups.items())

    def recipes_for(self, chr_id: str, node_effect_ids, skilltree) -> tuple:
        """`compile_node_recipes` for one character, compiled once."""
        r = self._recipes.get(chr_id)
        if r is None:
            r = compile_node_recipes(
                node_effect_ids, skilltree["nodes"] if skilltree else None, chr_id)
            self._recipes[chr_id] = r
        return r

    def needed_recipes(self, key: tuple, asm: "AssembledPlan", chr_id: str,
                       node_effect_ids, skilltree) -> tuple:
        """`recipes_for(chr_id)` restricted to the nodes plan `key`'s Connect
        assignment reads (`asm.needed`; `None` = every node), in recipe order
        — filtered once per plan instead of once per team."""
        hit = self._needed_recipes.get(key)
        if hit is not None and hit[0] is asm:
            return hit[1]
        recipes = self.recipes_for(chr_id, node_effect_ids, skilltree)
        if asm.needed is not None:
            if asm.needed:
                need = set(asm.needed)
                recipes = tuple(r for r in recipes if r[0] in need)
            else:
                recipes = ()
        self._needed_recipes[key] = (asm, recipes)
        return recipes

    def bench_boards(self, bench_characters) -> dict[str, CharacterBoard]:
        """The account's bench boards parsed ONCE per mapping object — the same
        `board_from_mapping` parse, its `CharacterBoard`s reused (frozen)."""
        if bench_characters is None:
            return {}
        if self._bench and self._bench[0] is bench_characters:
            return self._bench[1]
        parsed = board_from_mapping(
            {"characters": {k: list(v) for k, v in bench_characters.items()}}).characters
        self._bench = (bench_characters, dict(parsed))
        return self._bench[1]

    def bench_digests(self, bench_characters) -> dict[str, BoardDigest]:
        """`bench_boards`' `BoardDigest`s, built ONCE per parse and handed to
        every `BoardModel` assembled over them — the bench is ~50 of a model's
        ~54 boards and identical for every team."""
        boards = self.bench_boards(bench_characters)
        if self._bench_digests and self._bench_digests[0] is boards:
            return self._bench_digests[1]
        digests = {name: BoardDigest(board) for name, board in boards.items()}
        self._bench_digests = (boards, digests)
        return digests

    def bench_base(self, bench_characters) -> BoardBase | None:
        """The bench as a `BoardBase` — its per-card stat terms summed once
        and corrected per team (`BoardModel._terms_over_base`). `None` with no
        bench."""
        if bench_characters is None:
            return None
        digests = self.bench_digests(bench_characters)
        if self._bench_base and self._bench_base[0] is digests:
            return self._bench_base[1]
        base = BoardBase(digests)
        self._bench_base = (digests, base)
        return base

    def team_board(self, key: tuple, holomem: str, asm: "AssembledPlan",
                   cmult: Mapping[str, float]) -> tuple[CharacterBoard, BoardDigest]:
        """One developed board — plan `key`'s nodes (`asm`, which
        `assembled_for` returned for that key) under the Connect multipliers
        the team-wide assignment gave them — and its digest, reused whenever
        another team lands the same plan on the same multipliers. Keyed by
        the character's whole `cmult` (a handful of covered nodes) rather than
        by the multiplier of every node: the board is a function of (plan,
        `cmult`), so equal keys are equal boards, and two `cmult`s that differ
        only outside this plan's nodes merely build the same board twice."""
        cache_key = (key, holomem, frozenset(cmult.items()) if cmult else _NO_MULTS)
        hit = self._team_boards.get(cache_key)
        if hit is not None:
            return hit
        mults = tuple([cmult.get(node_id, 1.0) for node_id, _eff in asm.nodes])
        if len(self._team_boards) >= _TEAM_BOARDS_MAX:
            self._team_boards.clear()
        board = CharacterBoard(
            holomem=holomem,
            nodes=tuple(
                BoardNode(node_id=node_id, effects=(eff,), connect_multiplier=mult)
                for (node_id, eff), mult in zip(asm.nodes, mults)
            ),
        )
        hit = (board, BoardDigest(board))
        self._team_boards[cache_key] = hit
        return hit

    def assembled_for(self, key: tuple, cplan: BoardPlan, chr_id: str, *,
                      skilltree: Mapping, node_effect_ids, connect_cards) -> "AssembledPlan":
        """The team-invariant half of `board_model_from_plan` for one plan:
        its node list with parsed effects (in `cplan.selected` order), and the
        socket entries the team-wide Connect assignment scores — computed once
        per plan key and reused for every team."""
        self._sync_sources(node_effect_ids, skilltree, connect_cards)
        asm = self.assembled.get(key)
        if asm is not None:
            return asm
        nodes: list[tuple[str, object]] = []
        for node_id in cplan.selected:
            if node_id == ROOT_NODE:
                continue
            effstr = _effect_string_for(skilltree, node_id, chr_id, node_effect_ids)
            if effstr is None:
                continue
            nodes.append((node_id, parse_effect_id(effstr, branch=node_id[:1])))
        model_id = model_for_character(skilltree, chr_id) or ""
        positions = skilltree.get("models", {}).get(model_id, {}) if model_id else {}
        entries: list[tuple[str, int, tuple[str, ...], tuple[str, ...]]] = []
        needed: set[str] = set()
        if connect_cards is not None and positions:
            pos_to_node = {tuple(p): n for n, p in positions.items()}
            selected = cplan.selected
            for socket, spos in positions.items():
                if not socket.startswith("S"):
                    continue
                if socket != "S-001" and socket not in selected:
                    continue
                sx, sy = spos
                for offsets, permil, card_ids in self._connect_groups:
                    covered = []
                    for dx, dy in offsets:
                        node = pos_to_node.get((sx + dx, sy + dy))
                        if node and node in selected:
                            covered.append(node)
                    if covered:
                        entries.append((socket, permil, tuple(covered), card_ids))
                        needed.update(covered)
        asm = AssembledPlan(
            nodes=tuple(nodes), model_id=model_id, entries=tuple(entries),
            needed=tuple(sorted(needed)) if connect_cards is not None else None)
        self.assembled[key] = asm
        return asm


@dataclass(frozen=True, slots=True)
class AssembledPlan:
    """One plan's team-invariant assembly (`PlanCache.assembled_for`).

    `nodes` — `(node_id, BoardEffect)` for every bought node with an effect, in
    the plan's own iteration order (the order `board_model_from_plan`
    emits). `entries` — `(socket, permil, covered nodes, card ids)`
    per (eligible socket, distinct connect shape): the shape's covered bought
    nodes in offset order and the account's cards carrying that exact (shape,
    permil), id-sorted; `needed` — the union of covered nodes, i.e. the ONLY
    nodes whose base value the Connect assignment reads (`None` = all, the
    idealised no-`connect_cards` path)."""

    nodes: tuple[tuple[str, object], ...]
    model_id: str
    entries: tuple[tuple[str, int, tuple[str, ...], tuple[str, ...]], ...]
    needed: tuple[str, ...] | None


def cached_team_plan(
    leader: Card,
    members: Sequence[Card],
    *,
    cache: PlanCache,
    tree_for: Callable[[Card], SkillTree | None],
    budget_for: Callable[[Card], int],
    node_effect_ids: Mapping[str, list[str]],
    skilltree: Mapping | None,
    sings: bool | frozenset = False,
) -> TeamBoardPlan:
    """`optimize_team_boards`'s twin on the plan cache: one plan per distinct
    CHARACTER, the leader's at role `leader` (or `leader+member` when a card of
    hers is among the members, valued on THAT card so her blues reach it), each
    other member's at role `member`; `sings` is the leader's singer context.
    Same `per_character` shape (keyed by the leader/member card id, the leader
    first) so `board_model_from_plan` re-scores it unchanged. `total_gain` is
    the NOMINAL linearised sum — a bookkeeping figure, not this team's number;
    the re-score is."""
    members = tuple(members)
    by_chr = {_chr_of(m): m for m in members}
    lead_chr = _chr_of(leader)
    plans: dict[str, BoardPlan] = {}
    keys: dict[str, tuple] = {}
    total = 0.0
    tree = tree_for(leader)
    if tree is not None:
        if lead_chr in by_chr:
            role, card = PLAN_ROLE_LEADER_MEMBER, by_chr[lead_chr]
        else:
            role, card = PLAN_ROLE_LEADER, leader
        lead_sings = sings_key(sings, lead_chr, True)
        plan = cache.plan_for(card, role, budget_for(leader), lead_sings, tree=tree,
                              node_effect_ids=node_effect_ids, skilltree=skilltree)
        plans[leader.id] = plan
        keys[leader.id] = plan_key(card, role, budget_for(leader), lead_sings)
        total += plan.value
    for m in members:
        if _chr_of(m) == lead_chr:
            continue
        tree = tree_for(m)
        if tree is None:
            continue
        m_sings = sings_key(sings, _chr_of(m), False)
        plan = cache.plan_for(m, PLAN_ROLE_MEMBER, budget_for(m), m_sings, tree=tree,
                              node_effect_ids=node_effect_ids, skilltree=skilltree)
        plans[m.id] = plan
        keys[m.id] = plan_key(m, PLAN_ROLE_MEMBER, budget_for(m), m_sings)
        total += plan.value
    # THE GROUPING CAP ACROSS THE TEAM: the plans above are per character, so three
    # ID Gen 1 members each buy the whole G-008/011/021 chain (1,350; the game counts
    # 900). A member whose chain lies wholly in the surplus adds nothing with it: she
    # is re-planned without it (a cached variant), the fewest-points board first --
    # those points are dearest there (e.g. a Lv40 Iofi would otherwise half-buy her blues)
    for m in sorted((m for m in members if m.id in plans and _chr_of(m) != lead_chr),
                    key=lambda m: (budget_for(m), m.id)):
        if not _chain_is_surplus(m, plans, members, leader, skilltree):
            continue
        tree = tree_for(m)
        m_sings = sings_key(sings, _chr_of(m), False)
        plan = cache.plan_for(m, PLAN_ROLE_MEMBER, budget_for(m), m_sings, tree=tree,
                              node_effect_ids=node_effect_ids, skilltree=skilltree,
                              no_grouping=True)
        total += plan.value - plans[m.id].value
        plans[m.id] = plan
        keys[m.id] = plan_key(m, PLAN_ROLE_MEMBER, budget_for(m), m_sings) + (NO_GROUPING,)
    return TeamBoardPlan(total_gain=total, per_character=plans, plan_keys=keys)


_SINGER_BENCH: dict = {}


def singer_bench_characters(
    bench_characters: Mapping[str, list] | None,
    cast,
    *,
    cache: PlanCache,
    cards_by_chr: Mapping[str, Card],
    tree_for: Callable[[Card], SkillTree | None],
    budget_for: Callable[[Card], int],
    node_effect_ids: Mapping[str, list[str]],
    skilltree: Mapping,
    name_by_chr: Mapping[str, str],
) -> Mapping[str, list] | None:
    """`bench_characters` with each SINGER's board planned for her song: greens plus
    the yellows the song pays (`bench_node_values` with her `SongCast`), at the plan
    cache's nominal context and her points. A singer off the team still pays her song
    yellows (`song_score_bonus_for` sums the cast's boards), and a planned search
    re-plans every board -- the materials re-plan buys her yellows on the bench -- so
    the search's leaf must too: with her board as captured (no yellow bought), m0365's
    leaf ranks every team without Towa ~5 % too low and the materials re-plan never sees
    its best team (2,534,438 against 2,409,940). A singer on the team plans her own board (team boards take
    the name), so this is the same mapping for every team of the song. The captured
    Connect multiplier of a kept node stays; a new node starts at 1.0."""
    if not bench_characters or not cast:
        return bench_characters
    scope = song_scope_of(cast)
    key = (id(bench_characters), id(cache), tuple(sorted(cast)), scope)
    hit = _SINGER_BENCH.get(key)
    if hit is not None and hit[0] is bench_characters and hit[1] is cache:
        return hit[2]
    out = dict(bench_characters)
    for chr_id in sorted(cast):
        card, name = cards_by_chr.get(chr_id), name_by_chr.get(chr_id)
        tree = tree_for(card) if card is not None else None
        if tree is None or not name:
            continue
        plan = cache.plan_for(card, PLAN_ROLE_BENCH, budget_for(card), scope or True, tree=tree,
                              node_effect_ids=node_effect_ids, skilltree=skilltree)
        kept = {e.get("node"): float(e.get("connect_multiplier") or 1.0)
                for e in (bench_characters.get(name) or ())}
        rows = []
        for node in sorted(plan.selected):
            effect = _effect_string_for(skilltree, node, chr_id, node_effect_ids)
            if effect:
                rows.append({"node": node, "effects": [effect],
                             "connect_multiplier": kept.get(node, 1.0)})
        out[name] = rows
    if len(_SINGER_BENCH) > 16:
        _SINGER_BENCH.clear()
    _SINGER_BENCH[key] = (bench_characters, cache, out)
    return out


def _grouping_cap() -> int:
    from .board import GROUPING_ALL_CAP  # noqa: PLC0415
    return GROUPING_ALL_CAP


def _grouping_chain(selected, chr_id: str, skilltree: Mapping | None) -> dict[str, float]:
    """`{grouping: amount}` of the generation greens in `selected` on `chr_id`'s
    board. Per grouping, not one: a character's three nodes can feed different
    groupings (Fubuki's G-008 goes to Gen 1, her G-011 to GAMERS)."""
    nodes = (skilltree or {}).get("nodes", {})
    out: dict[str, float] = {}
    for nid in sorted(GROUPING_GREEN_NODES & set(selected)):
        row = ((nodes.get(nid) or {}).get("chr") or {}).get(chr_id)
        if row and row.get("v"):
            tgt = row.get("tgt", "")
            out[tgt] = out.get(tgt, 0.0) + float(row["v"])
    return out


def _grouping_supply(tgt: str, plans: Mapping, members, leader: Card,
                     skilltree: Mapping | None) -> float:
    """What grouping `tgt` receives from the team's planned boards plus the off-team
    boards (`GROUPING_ROOM_KEY`'s source: the leaf's bench, `grouping_supply`)."""
    cards = {c.id: c for c in (leader, *members)}
    team_chrs = {_chr_of(c) for c in cards.values()}
    total = 0.0
    for card_id, plan in plans.items():
        total += _grouping_chain(plan.selected, _chr_of(cards[card_id]), skilltree).get(tgt, 0.0)
    bench = ((skilltree or {}).get(GROUPING_SUPPLY_KEY) or {}).get(tgt) or {}
    return total + sum(v for c, v in bench.items() if c not in team_chrs)


def _chain_is_surplus(card: Card, plans: Mapping, members, leader: Card,
                      skilltree: Mapping | None) -> bool:
    """Every grouping this card's board feeds would still reach the cap without it."""
    chain = _grouping_chain(plans[card.id].selected, _chr_of(card), skilltree)
    if not chain:
        return False
    cap = _grouping_cap()
    return all(_grouping_supply(tgt, plans, members, leader, skilltree) - amount >= cap
               for tgt, amount in chain.items())


# CharacterLevel field 11, cumulative point grant: the real board
# budget a character of a given level has. The full tree is 453 points.
_POINT_GRANT = ((10, 26), (20, 83), (30, 161), (40, 261), (50, 361))
_FULL_TREE_POINTS = 453


def point_budget_for_level(level: int) -> int:
    """The skill-tree points a character of `level` has earned, interpolated
    between the game's grant milestones and capped at the full tree."""
    if level <= _POINT_GRANT[0][0]:
        return int(round(_POINT_GRANT[0][1] * level / _POINT_GRANT[0][0]))
    for (l0, p0), (l1, p1) in zip(_POINT_GRANT, _POINT_GRANT[1:]):
        if level <= l1:
            return int(round(p0 + (p1 - p0) * (level - l0) / (l1 - l0)))
    # above the last milestone, extrapolate the last slope, capped at the tree.
    (l0, p0), (l1, p1) = _POINT_GRANT[-2], _POINT_GRANT[-1]
    grant = p1 + (p1 - p0) * (level - l1) / (l1 - l0)
    return min(_FULL_TREE_POINTS, int(round(grant)))


def board_points_from_snapshot(snapshot: Mapping) -> dict[str, int]:
    """`chr-id -> UNSPENT skill-tree points` from the account snapshot's
    `board_points` (`user_skill_tree_point_list`), e.g. `{'chr-00001': 26, ...}`.

    A row WITHOUT `quantity` is 0, not unknown: proto3 elides a zero field, so a
    character whose points are all spent arrives as `{'point_id': ...}` alone.
    Skipping those rows would budget such a character 0 points and make the plan
    propose selling her whole board."""
    out: dict[str, int] = {}
    for row in snapshot.get("board_points", []):
        pid = row.get("point_id", "")
        prefix = "skill_tree_point-"
        if not pid.startswith(prefix):
            continue
        q = row.get("quantity")
        out[pid[len(prefix):]] = int(q) if isinstance(q, int) else 0
    return out


def _effect_string_for(skilltree: Mapping, node_id: str, chr_id: str,
                       node_effect_ids: Mapping[str, list[str]]) -> str | None:
    """The effect id string for one node on one character's board: the chr
    override for a grouping green, else the node's single master effect."""
    override = skilltree["nodes"].get(node_id, {}).get("chr", {}).get(chr_id)
    if override and "tgt" in override and "v" in override:
        return (f"skill_tree_effect-all_parameter_up_for_character_grouping-"
                f"{int(override['v'])}-skill_tree_effect_target-{override['tgt']}")
    effs = node_effect_ids.get(node_id, [])
    return effs[0] if effs else None


@dataclass(frozen=True, slots=True)
class SocketPlacement:
    """One real Connect placement the develop re-score decided on: this card,
    on this character's socket, at the multiplier its resolved level grants."""

    chr: str            # "chr-00031"
    socket: str         # "S-001".."S-004"
    card: str           # placed card id
    multiplier: float   # 1 + permil/1000 at the card's resolved level
    value: float        # linearised Unit-Score lift of the placement


# One candidate Connect placement the team-wide assignment chooses among:
# `(gain, chr_id, socket, permil, covered nodes, id-sorted cards carrying this
# exact (shape, permil))` — one per (eligible socket, distinct shape group),
# gain > 0. Both assignment entry points below reduce to this shape.
ConnectTriple = tuple[float, str, str, int, tuple[str, ...], tuple[str, ...]]


def _max_weight_matching(
    weights: Sequence[Sequence[float]],
    capacities: Sequence[int] | None = None,
) -> dict[int, int]:
    """EXACT maximum-weight bipartite matching — the Hungarian (Kuhn-Munkres)
    algorithm in its successive-shortest-path form with dual potentials,
    stdlib only, columns CAPACITATED so cards sharing one (shape, permil) need
    no duplication. `weights[i][j] >= 0` is the value of pairing row `i` with
    column `j`; every row is used at most once and column `j` at most
    `capacities[j]` times (default 1). Rows may stay unmatched (one zero-value
    dummy column of capacity n), so the result is the maximum over ALL
    matchings, not only perfect ones. Returns `{row: column}` for the rows
    matched at positive weight. Deterministic: on equal reduced cost the
    lowest column index wins, so the answer is a pure function of the matrix.
    O(n · (n + m) · m) worst case, n rows × m columns; about 0.05 ms on a
    typical 16 × 20 team instance."""
    n = len(weights)
    if n == 0:
        return {}
    ncol = len(weights[0])
    caps = list(capacities) if capacities is not None else [1] * ncol
    m = ncol + 1                          # + the dummy column (index ncol)
    caps.append(n)
    inf = float("inf")
    cost = [[-w for w in row] + [0.0] for row in weights]   # minimise -weight
    u = [0.0] * n                          # row potentials
    v = [0.0] * m                          # column potentials
    matched: list[list[int]] = [[] for _ in range(m)]   # rows on column j
    col_of_row = [-1] * n
    for r in range(n):
        # Dijkstra from row r over the columns, reduced costs c - u - v >= 0;
        # a column with spare capacity ends the path.
        minv = [inf] * m
        way_col = [-1] * m     # predecessor column on the path (-1 = row r itself)
        way_row = [-1] * m     # the row the path came through
        used = [False] * m
        cr = cost[r]
        ur = u[r]
        for j in range(m):
            c = cr[j] - ur - v[j]
            if c < minv[j]:
                minv[j] = c
                way_row[j] = r
        visited: list[int] = []
        while True:
            delta = inf
            j1 = -1
            for j in range(m):
                if not used[j] and minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            u[r] += delta
            for j in visited:
                v[j] -= delta
                for i in matched[j]:
                    u[i] += delta
            for j in range(m):
                if not used[j]:
                    minv[j] -= delta
            used[j1] = True
            visited.append(j1)
            if len(matched[j1]) < caps[j1]:
                break
            # saturated: continue through EVERY row it holds (0-cost back edges)
            for i in matched[j1]:
                ci = cost[i]
                ui = u[i]
                for j in range(m):
                    if not used[j]:
                        c = ci[j] - ui - v[j]
                        if c < minv[j]:
                            minv[j] = c
                            way_col[j] = j1
                            way_row[j] = i
        j = j1
        while True:                        # augment along the found path
            i = way_row[j]
            pj = way_col[j]
            matched[j].append(i)
            col_of_row[i] = j
            if pj == -1:
                break
            matched[pj].remove(i)
            j = pj
    out: dict[int, int] = {}
    for i in range(n):
        j = col_of_row[i]
        if 0 <= j < ncol and weights[i][j] > 0:
            out[i] = j
    return out


# The tie-break an incumbent placement gets (`incumbent`): its weight is raised by
# this FRACTION of itself, so among matchings of equal value the one keeping the
# account's own cards wins, and no matching worth more than 1e-9 relative more
# can lose to it.
INCUMBENT_TIE = 1e-9


def _connect_assignment_exact(
    triples: Sequence[ConnectTriple],
    incumbent: Mapping[tuple[str, str], str] | None = None,
) -> tuple[dict[str, dict[str, float]], list[SocketPlacement]]:
    """The team-wide REAL socketing, solved EXACTLY: one owned card per socket,
    each card at most once (the game's account-wide rule, restricted to this
    team's boards), maximising the summed linearised lift. The values are
    additive per (socket, card) — a card's multiplier reaches only the nodes
    of its own shape anchored at its own socket, and no two sockets of one
    board can reach a common node (on the shipped skilltree: 4 models × 4
    sockets × 17 shapes, 6,936 cross-socket shape pairs, 0 overlaps; even the
    unions of every shape per socket are pairwise disjoint) — so the problem is a maximum-weight bipartite
    matching between (character, socket) rows and card columns, and cards
    with the same (shape, permil) are one column with capacity = their count.

    Deterministic: rows are ordered `(chr, socket)`, columns by their id-sorted
    card tuple, and among the placements the concrete card is chosen in
    value-desc, then `(chr, socket)` order, taking the lowest unused id of the
    column (the same tie-break a highest-lift-first greedy uses).

    `incumbent` (`(chr, socket) -> card id`, the account's own placements): on a
    tie the account's card stays where it is (`INCUMBENT_TIE`). Without it, a card
    lifting greens worth exactly the same on two boards would be moved from one to
    the other -- a move that changes nothing but has to be made by hand -- and with
    the bench in the matching, dozens of such moves would be shown. The placements'
    values stay the true gains."""
    if not triples:
        return {}, []
    row_keys = sorted({(t[1], t[2]) for t in triples})
    row_of = {k: i for i, k in enumerate(row_keys)}
    col_keys = sorted({t[5] for t in triples})
    col_of = {k: j for j, k in enumerate(col_keys)}
    weights = [[0.0] * len(col_keys) for _ in row_keys]
    at: dict[tuple[int, int], ConnectTriple] = {}
    for t in triples:
        gain = t[0]
        if gain <= 0:
            continue
        if incumbent and incumbent.get((t[1], t[2])) in t[5]:
            gain *= 1.0 + INCUMBENT_TIE
        i, j = row_of[(t[1], t[2])], col_of[t[5]]
        # one entry per (socket, group) is the contract; keep the larger if not.
        if gain > weights[i][j]:
            weights[i][j] = gain
            at[(i, j)] = t
    match = _max_weight_matching(weights, [len(k) for k in col_keys])
    return _placements_from_matched([at[(i, j)] for i, j in match.items()],
                                    incumbent=incumbent)


def _placements_from_matched(
    matched: Sequence[ConnectTriple], *, name_cards: bool = True,
    incumbent: Mapping[tuple[str, str], str] | None = None,
) -> tuple[dict[str, dict[str, float]], list[SocketPlacement]]:
    """The matched placements as `(multipliers per character and node, the
    placements)`: largest lift first, each placement taking the first card of
    its group not yet used. The naming half of `_connect_assignment_exact`,
    shared with the compiled matching (`CONNECT_KERNEL`) so the two differ only
    in who solved the matching.

    `name_cards=False` returns the multipliers with an EMPTY placement list —
    for a caller that scores the board and reports no sockets. The multipliers
    do not depend on which card of a group took which socket (a node's is the
    `max` over the placements covering it), so they are the same either way."""
    if not name_cards:
        only_mults: dict[str, dict[str, float]] = {}
        for _gain, chr_id, _socket, permil, covered, _card_ids in matched:
            m = only_mults.setdefault(chr_id, {})
            multiplier = 1.0 + permil / 1000.0
            for node in covered:
                m[node] = max(m.get(node, 1.0), multiplier)
        return only_mults, []
    chosen = sorted(matched, key=lambda t: (-t[0], t[1], t[2]))
    mults: dict[str, dict[str, float]] = {}
    placements: list[SocketPlacement] = []
    # a socket matched to the group its own (`incumbent`) card belongs to keeps that card
    kept: dict[tuple[str, str], str] = {}
    for t in chosen:
        card = (incumbent or {}).get((t[1], t[2]))
        if card in t[5] and card not in kept.values():
            kept[(t[1], t[2])] = card
    used_cards: set[str] = set(kept.values())
    for gain, chr_id, socket, permil, covered, card_ids in chosen:
        card_id = kept.get((chr_id, socket)) or next(c for c in card_ids if c not in used_cards)
        used_cards.add(card_id)
        m = mults.setdefault(chr_id, {})
        for node in covered:
            m[node] = max(m.get(node, 1.0), 1.0 + permil / 1000.0)
        placements.append(SocketPlacement(
            chr=chr_id, socket=socket, card=card_id,
            multiplier=1.0 + permil / 1000.0, value=gain))
    return mults, placements


def _team_connect_assignment(
    per_char: Mapping[str, tuple[Mapping, frozenset, Mapping[str, float]]],
    connect_cards: Mapping[str, tuple[Sequence[Sequence[int]], int]],
    incumbent: Mapping[tuple[str, str], str] | None = None,
) -> tuple[dict[str, dict[str, float]], list[SocketPlacement]]:
    """REAL socketing over the account's cards, team-wide and EXACT
    (`_connect_assignment_exact`): each card and each socket used at most once.
    A socket other than the free `S-001` root must have been BOUGHT by the
    plan. `per_char` is `chr_id -> (model positions, selected nodes, node
    values)`; `connect_cards` is `card id -> (shape offsets, permil)`.

    The geometry -- which selected nodes each (socket, shape group) covers -- does not
    depend on the values, and `_yellow_capped_assignment` asks for it up to 7 times per
    team with only the values scaled: it is kept per (connect cards, board positions,
    selected nodes) object (`_CONNECT_GEOMETRY`). The gains are the same sums in the
    same order, so the triples are identical."""
    held = _CONNECT_GEOMETRY.get(id(connect_cards))
    if held is None or held[0] is not connect_cards:
        groups: dict[tuple, list[str]] = {}
        for card_id, (offsets, permil) in connect_cards.items():
            groups.setdefault((tuple(tuple(o) for o in offsets), permil), []).append(card_id)
        shape_groups = [(offsets, permil, tuple(sorted(ids)))
                        for (offsets, permil), ids in groups.items()]
        if len(_CONNECT_GEOMETRY) > 64:
            _CONNECT_GEOMETRY.clear()
        held = _CONNECT_GEOMETRY[id(connect_cards)] = (connect_cards, shape_groups, {})
    _cards, shape_groups, per_board = held
    triples: list[ConnectTriple] = []
    for chr_id, (positions, selected, values) in per_char.items():
        if not isinstance(selected, frozenset):
            selected = frozenset(selected)
        entry = per_board.get((id(positions), selected))
        if entry is None or entry[0] is not positions:
            pos_to_node = {tuple(p): n for n, p in positions.items()}
            rows = []
            for socket, spos in positions.items():
                if not socket.startswith("S"):
                    continue
                if socket != "S-001" and socket not in selected:
                    continue
                sx, sy = spos
                for offsets, permil, card_ids in shape_groups:
                    covered = []
                    for dx, dy in offsets:
                        node = pos_to_node.get((sx + dx, sy + dy))
                        if node and node in selected:
                            covered.append(node)
                    rows.append((socket, permil, tuple(covered), card_ids))
            if len(per_board) > 4096:
                per_board.clear()
            entry = per_board[(id(positions), selected)] = (positions, rows)
        for socket, permil, covered, card_ids in entry[1]:
            gain = 0.0
            for node in covered:
                gain += values.get(node, 0.0) * permil / 1000.0
            if gain > 0:
                triples.append((gain, chr_id, socket, permil, covered, card_ids))
    return _connect_assignment_exact(triples, incumbent)


# `_team_connect_assignment`'s geometry: id(connect cards) -> (the cards, shape groups,
# {(id(positions), selected): (positions, [(socket, permil, covered, card ids)])})
_CONNECT_GEOMETRY: dict = {}


_GREEN_HOLDERS: dict = {}


def yellow_scope(skilltree: Mapping, node_id: str, chr_id: str) -> str | None:
    """The song scope (`solo` / `group` / `all`) of a yellow song-score-bonus node on
    `chr_id`'s board, or None for any other node."""
    if not node_id.startswith("Y"):
        return None
    node = (skilltree.get("nodes") or {}).get(node_id) or {}
    effect = (node.get("chr") or {}).get(chr_id) or node.get("e") or {}
    if "live_score_bonus" not in str(effect.get("t") or ""):
        return None
    return effect.get("tr")


class SongCast(frozenset):
    """A song's cast (chr ids) that also carries the song's scope (`Chart.singer_type`:
    `solo` / `group` / `all`). It is a `frozenset`, so every `sings` caller reads it as
    the cast; `bench_yellow_pays` reads the scope. The scorer pays ONE scope per song
    (`BoardModel.song_score_bonus_for(singers, scope=singer_type)`), and the cast's size
    does not say which: a `solo` song may name two singers (`Chart` validation). Without
    the scope, Ina's and Kiara's SOLO yellows would fill the cap's room on their GROUP song
    m0198, so their group yellows would be valued 0 and lose their sockets: -1.68 % on
    every top-16 team."""

    def __new__(cls, singers=(), scope: str = ""):
        self = super().__new__(cls, singers)
        self.scope = scope
        return self

    def __reduce__(self):
        return (SongCast, (tuple(self), self.scope))


def song_cast(chart) -> SongCast:
    """The `SongCast` of one chart: its singers' chr ids at its `singer_type`."""
    return SongCast(chart.singers, getattr(chart, "singer_type", "") or "")


def bench_yellow_pays(scope: str | None, chr_id: str, cast) -> bool:
    """Whether an OFF-TEAM board's yellow of `scope` reaches this song's score
    (`BoardModel.song_score_bonus_for`): the song's own cast pays the yellows of the
    song's scope whoever is on the team; a song with no cast (a whole-roster song, or
    the song-agnostic score) pays every board's `all` yellows. A plain set says who
    sings but not the scope, so it accepts either solo or group (a `SongCast` says
    which). `cast` None: not known (a leader flag was passed instead), so no bench
    yellow is counted."""
    if scope is None or cast is None:
        return False
    if not cast:
        return scope == "all"
    if chr_id not in cast:
        return False
    song_scope = getattr(cast, "scope", "")
    return scope == song_scope if song_scope else scope in ("solo", "group")


def bench_sockets_from_state(state: Mapping | None) -> dict[str, dict[str, str | None]]:
    """`{chr: {unlocked socket: card or None}}` from the account's `board_state.json`:
    the root (free on every board), every socket node it bought and every socket that
    holds a card. Every bench mode keeps the captured placements (the developed bench
    re-plans nodes, never sockets), so this is what the off-team sockets hold
    (`RefineContext.bench_sockets`, `held_bench_cards`)."""
    out = {}
    for chr_id, entry in (state or {}).items():
        slots = dict((entry or {}).get("slots") or {})
        unlocked = {"S-001", *slots, *(n for n in (entry or {}).get("nodes") or () if str(n).startswith("S-"))}
        out[chr_id] = {s: slots.get(s) for s in sorted(unlocked)}
    return out


_BENCH_BOUGHT: list = [None, None, None]


def _bench_bought(bench_characters, name_by_chr) -> dict[str, frozenset] | None:
    """`chr -> its bought nodes` from the name-keyed `bench_characters`, or None when
    the names cannot be mapped; the last pair is remembered (the leaf asks per team)."""
    if not bench_characters or not name_by_chr:
        return None
    hit = _BENCH_BOUGHT
    if hit[0] is not bench_characters or hit[1] is not name_by_chr:
        by_name = {name: chr_id for chr_id, name in name_by_chr.items()}
        hit[2] = {by_name[name]: frozenset(e.get("node") for e in rows if e.get("node"))
                  for name, rows in bench_characters.items() if name in by_name}
        hit[0], hit[1] = bench_characters, name_by_chr
    return hit[2]


def held_bench_cards(bench_sockets: Mapping[str, Mapping[str, str | None]] | None,
                     team_chrs, skilltree: Mapping, connect_cards, *,
                     cast=None, bought: Mapping[str, frozenset] | None = None) -> frozenset[str]:
    """The cards an OFF-TEAM board's socket holds (`bench_sockets`, chr -> {socket:
    card}) where they lift a node of that board that reaches the team, already
    multiplied in `bench_characters`: a GREEN, or a YELLOW the song pays (`bench_yellow_pays` with `cast`, the song's singers: the
    scorer sums every board's song yellows, so Miko's yellow socket counts on her song
    with Miko off the team). The team's matching may not use such a card too, or the
    card counts twice (+2.06 % on a Miko solo). A card whose
    shape reaches neither lifts nothing this team sees, so it stays free for the team.
    `bought` (chr -> its bought nodes): only a bought node is multiplied; without it
    every node in the shape counts. The team's own boards are re-assigned, so their
    cards are free as well. The board walk is done once per input set."""
    if not bench_sockets or not connect_cards:
        return frozenset()
    key = (id(bench_sockets), id(connect_cards), id(bought))
    hit = _GREEN_HOLDERS.get(key)
    if hit is None or hit[0] is not bench_sockets or hit[1] is not connect_cards \
            or hit[2] is not bought:
        holders = []
        for chr_id, slots in bench_sockets.items():
            positions = (skilltree.get("models", {}) or {}).get(
                model_for_character(skilltree, chr_id) or "", {})
            pos_to_node = {tuple(p): n for n, p in positions.items()}
            owned = None if bought is None else bought.get(chr_id, frozenset())
            for socket, card in (slots or {}).items():
                entry = connect_cards.get(card) if card else None
                spos = positions.get(socket)
                if entry is None or spos is None:
                    continue
                lifted = [n for dx, dy in entry[0]
                          if (n := str(pos_to_node.get((spos[0] + dx, spos[1] + dy), "")))
                          and (owned is None or n in owned)]
                green = any(n.startswith("G-") for n in lifted)
                scopes = frozenset(s for n in lifted if (s := yellow_scope(skilltree, n, chr_id)))
                if green or scopes:
                    holders.append((chr_id, card, green, scopes))
        if len(_GREEN_HOLDERS) > 8:
            _GREEN_HOLDERS.clear()
        hit = _GREEN_HOLDERS[key] = (bench_sockets, connect_cards, bought, tuple(holders))
    return frozenset(card for chr_id, card, green, scopes in hit[3]
                     if chr_id not in team_chrs
                     and (green or any(bench_yellow_pays(s, chr_id, cast) for s in scopes)))


def _bench_socket_rows(chars, names, team_chrs, skilltree, node_effect_ids, members, *,
                       overall_power, bonus_permil, k_constant, connect_cards, per_char,
                       bench_sockets=None, cast=None):
    """Add every BENCH board's root socket (`S-001`, free on any board) to the Connect
    matching (`per_char`, in place) and return `{chr: (holomem, nodes an S-001 shape can
    reach)}` for the boards added. Keeping the bench's own sockets unoptimised lets a
    card the team takes ALSO stay on its bench socket (8 such on the m0365 Towa team);
    with the bench in the same matching each card is used once account-wide (+0.91 %
    there, 2,458,852 -> 2,481,161). A bench board is valued by what reaches the
    team from it (`bench_node_values`: its GREENs, and the yellows the song pays when
    `cast` is the song's singers). Without `bench_sockets` its other
    sockets are not known (a bench entry carries no socket nodes) and keep the
    account's card; with it (`chr -> {unlocked socket: card}`) EVERY unlocked socket
    of the board joins -- so unlocked-but-empty bench sockets can be filled too -- and
    the cover is the whole board (`None`)."""
    inverse = {name: chr_id for chr_id, name in names.items()}
    shapes = {tuple(tuple(o) for o in offsets) for offsets, _permil in connect_cards.values()}
    rows = {}
    for name, entries in chars.items():
        chr_id = inverse.get(name)
        if chr_id is None or chr_id in team_chrs or not entries:
            continue
        model_id = model_for_character(skilltree, chr_id) or ""
        positions = (skilltree.get("models", {}) or {}).get(model_id, {})
        root = positions.get("S-001")
        if not root:
            continue
        if bench_sockets is not None:
            open_sockets = {"S-001", *(s for s in (bench_sockets.get(chr_id) or {}) if s in positions)}
            cover = None
        else:
            open_sockets = {"S-001"}
            pos_to_node = {tuple(p): n for n, p in positions.items()}
            cover = frozenset(pos_to_node[(root[0] + dx, root[1] + dy)]
                              for shape in shapes for dx, dy in shape
                              if (root[0] + dx, root[1] + dy) in pos_to_node)
        selected = frozenset(e["node"] for e in entries if e.get("node")) | open_sockets
        values = bench_node_values(
            node_effect_ids, member_cards=members, bonus_permil=bonus_permil,
            overall_power=overall_power, k_constant=k_constant,
            skilltree={"nodes": skilltree["nodes"]}, model_id="", own_chr=chr_id, cast=cast)
        # only the open sockets: the matching skips a socket not in `selected` except S-001
        per_char[chr_id] = ({n: p for n, p in positions.items()
                             if not n.startswith("S") or n in open_sockets}, selected, values)
        rows[chr_id] = (name, cover)
    return rows


def _yellow_permil(skilltree: Mapping, node_id: str, chr_id: str, cast,
                   node_effect_ids: Mapping[str, list[str]]) -> float:
    """The song-bonus permil a bought yellow node adds to THIS song (0 for a node the
    song does not pay: another scope, a board outside the cast, not a yellow)."""
    if not bench_yellow_pays(yellow_scope(skilltree, node_id, chr_id), chr_id, cast):
        return 0.0
    effstr = _effect_string_for(skilltree, node_id, chr_id, node_effect_ids)
    try:
        effect = parse_effect_id(effstr, branch="Y") if effstr else None
    except Exception:  # noqa: BLE001 - an effect this channel does not read pays nothing here
        return 0.0
    return effect.value * 1000.0 if effect is not None and effect.kind == "song_score_bonus" else 0.0


_YELLOW_PERMIL_MEMO: dict = {}


def _yellow_permil_memo(skilltree: Mapping, node_id: str, chr_id: str, cast,
                        node_effect_ids: Mapping[str, list[str]]) -> float:
    """`_yellow_permil`, remembered per (inputs, node, board, cast): the leaf's fast
    path asks it for every bought node of every team."""
    key = (id(skilltree), id(node_effect_ids), node_id, chr_id, cast,
           getattr(cast, "scope", None))
    hit = _YELLOW_PERMIL_MEMO.get(key)
    if hit is None:
        if len(_YELLOW_PERMIL_MEMO) > 200_000:
            _YELLOW_PERMIL_MEMO.clear()
        hit = _YELLOW_PERMIL_MEMO[key] = _yellow_permil(skilltree, node_id, chr_id, cast,
                                                       node_effect_ids)
    return hit


def _song_bonus_over_cap(team_rows, bench_characters, names: Mapping[str, str], *, cast,
                         skilltree: Mapping, node_effect_ids) -> bool:
    """Whether a team's paying yellows -- its own bought nodes at their Connect
    multipliers (`team_rows`: `(chr_id, selected nodes, multipliers)`) plus the
    off-team boards' (`bench_characters`) -- exceed `SONG_ALL_CAP_PERMIL`. Below it
    `_yellow_capped_assignment` returns the plain matching, which the plan cache's
    fast path computes too; above it only the capped path is right."""
    total = 0.0
    team = set()
    for chr_id, selected, mults in team_rows:
        team.add(chr_id)
        if cast and chr_id not in cast:
            continue
        for node in selected:
            p = _yellow_permil_memo(skilltree, node, chr_id, cast, node_effect_ids)
            if p:
                total += p * mults.get(node, 1.0)
    inverse = {name: chr_id for chr_id, name in names.items()}
    for name, rows in (bench_characters or {}).items():
        chr_id = inverse.get(name)
        if not chr_id or chr_id in team or (cast and chr_id not in cast):
            continue
        for e in rows:
            if e.get("node"):
                total += _yellow_permil_memo(skilltree, e["node"], chr_id, cast,
                                             node_effect_ids) * float(e.get("connect_multiplier") or 1.0)
    return total > SONG_ALL_CAP_PERMIL + 1e-9


def _yellow_capped_assignment(per_char, connect_cards, *, cast, skilltree, node_effect_ids,
                              fixed_rows=(), rounds: int = 6, incumbent=None):
    """`_team_connect_assignment` under the song bonus's per-song cap
    (`SONG_ALL_CAP_PERMIL`, `BoardModel.song_score_bonus_for`): the linear node values
    price every yellow permil at full worth, but past the cap a yellow adds nothing.
    E.g. on m0342 (a group song) four bench singers' bought yellows already sum 110
    permil, and valuing them linearly sends four cards to their yellow sockets -- worth
    0 -- off the greens: -0.47 % on every top-16 team.

    The room is the cap minus what the bought yellows already pay (`fixed_rows`, the
    boards outside the matching, with their multipliers; the matched boards' own
    yellows unmultiplied). The yellow values are scaled (bisection on the scale, at
    most `rounds` more matchings) and the matching kept is the one worth most with its
    yellow lift CAPPED at the room -- not the last one that fits: scaling only down,
    m0198 (Ina + Kiara, 80 permil bought) drops Ina's yellow card for Kiara's alone,
    leaving 6 permil of room unused, -0.40 % on every team."""
    def paying(rows):
        return {n: p for n in rows if (p := _yellow_permil(skilltree, n, chr_id, cast, node_effect_ids))}
    room = SONG_ALL_CAP_PERMIL
    for chr_id, e in fixed_rows:
        if chr_id and e.get("node"):
            room -= _yellow_permil(skilltree, e["node"], chr_id, cast, node_effect_ids) * float(
                e.get("connect_multiplier") or 1.0)
    yellow = {}
    for chr_id, (_positions, selected, _values) in per_char.items():
        if (rows := paying(selected)):
            yellow[chr_id] = rows
            room -= sum(rows.values())
    result = _team_connect_assignment(per_char, connect_cards, incumbent)
    if not yellow:
        return result
    room = max(room, 0.0)

    def lift_of(res):
        return sum((res[0].get(chr_id, {}).get(n, 1.0) - 1.0) * p
                   for chr_id, rows in yellow.items() for n, p in rows.items())

    def worth(res):
        # the matching's gain on the UNSCALED values, its yellow part capped at the room
        other = paid = 0.0
        for chr_id, (_positions, selected, values) in per_char.items():
            mults, ys = res[0].get(chr_id, {}), yellow.get(chr_id, ())
            for n, m in mults.items():
                if n in selected:
                    if n in ys:
                        paid += (m - 1.0) * values.get(n, 0.0)
                    else:
                        other += (m - 1.0) * values.get(n, 0.0)
        lift = lift_of(res)
        return other + (paid if lift <= room + 1e-9 else paid * room / lift)

    lift = lift_of(result)
    if lift <= room + 1e-9:
        return result
    # ties go to the smaller yellow lift: a card on a yellow worth nothing is a card spent
    best, best_key = result, (worth(result), -lift)
    low, high = 0.0, 1.0
    scale = room / lift
    for _ in range(rounds):
        scaled = {chr_id: (positions, selected,
                           {n: (v * scale if n in yellow.get(chr_id, ()) else v) for n, v in values.items()})
                  for chr_id, (positions, selected, values) in per_char.items()}
        candidate = _team_connect_assignment(scaled, connect_cards, incumbent)
        c_lift = lift_of(candidate)
        key = (worth(candidate), -c_lift)
        if key[0] > best_key[0] + 1e-9 or (key[0] >= best_key[0] - 1e-9 and key[1] > best_key[1]):
            best, best_key = candidate, key
        if c_lift > room + 1e-9:
            high = scale
        else:
            low = scale
        if high - low < 1e-3:
            break
        scale = (low + high) / 2.0
    return best


def _fixed_connect_placements(
    per_char: Mapping[str, tuple[Mapping, frozenset, Mapping[str, float]]],
    connect_cards: Mapping[str, tuple[Sequence[Sequence[int]], int]],
    fixed_slots: Mapping[str, Mapping[str, str]],
) -> tuple[dict[str, dict[str, float]], list[SocketPlacement]]:
    """The Connect multipliers of the sockets AS THEY ARE: `fixed_slots` is
    `chr_id -> {socket: card id}` (the account's own `User/Get` placements), and
    each card covers its own shape anchored at its own socket, at its resolved
    permil from `connect_cards`. Nothing is chosen. A card without a Connect
    effect, or a socket the model does not have, covers nothing. A socket that
    holds a card counts as unlocked (the server reports it so,
    `account_snapshot.refresh_board_state`), so it need not be in `selected`.

    Re-assigning the sockets instead — what the develop does — moves the "as
    they are" Overall Power off the server's `GetDraftDeckInfo` by +35..+3,450,
    with a team-dependent per-member offset, because the matching gives boards
    Connect cards they do not hold."""
    mults: dict[str, dict[str, float]] = {}
    placements: list[SocketPlacement] = []
    for chr_id, (positions, selected, values) in per_char.items():
        pos_to_node = {tuple(p): n for n, p in positions.items()}
        for socket, card_id in sorted((fixed_slots.get(chr_id) or {}).items()):
            entry = connect_cards.get(card_id)
            spos = positions.get(socket)
            if entry is None or spos is None:
                continue
            offsets, permil = entry
            multiplier = 1.0 + permil / 1000.0
            sx, sy = spos
            gain = 0.0
            m = mults.setdefault(chr_id, {})
            for dx, dy in offsets:
                node = pos_to_node.get((sx + dx, sy + dy))
                if node and node in selected:
                    gain += values.get(node, 0.0) * permil / 1000.0
                    m[node] = max(m.get(node, 1.0), multiplier)
            placements.append(SocketPlacement(
                chr=chr_id, socket=socket, card=card_id,
                multiplier=multiplier, value=gain))
    return mults, placements


# One character's input to the team-wide assignment on the plan cache:
# `(chr_id, its AssembledPlan, the recipes of the nodes the assignment reads,
# is_leader, the card, singer_weight)`.
ConnectInput = tuple[str, "AssembledPlan", tuple, bool, Card, float]

# The seam for a COMPILED assignment (`tools/web/kernel/connect.py`), injected
# the way `song.NOTE_WALK_KERNEL` is — this package imports nothing it does not
# ship. Called as `kernel(inputs, members, overall_power=, bonus_permil=,
# k_constant=, active_pool_rate=)`; it returns the MATCHED `ConnectTriple`s, or
# `None` to decline (a plan or a team it cannot express), and then the Python
# below runs. It must never return an approximation.
CONNECT_KERNEL: Callable | None = None
# The same hook for WORKER PROCESSES (a spawned interpreter inherits the
# environment and nothing else): `<dir>/connect.py:assign`, loaded by path on
# first use. A kernel that fails to load is reported once and Python is used.
CONNECT_KERNEL_ENV = "HBUILDER_CONNECT_KERNEL"
_env_connect_kernel: tuple[str, Callable | None] = ("", None)


def _connect_kernel() -> Callable | None:
    """The injected kernel: the module attribute if set, else the one the
    environment names (resolved once per distinct value)."""
    global _env_connect_kernel
    if CONNECT_KERNEL is not None:
        return CONNECT_KERNEL
    spec = os.environ.get(CONNECT_KERNEL_ENV, "")
    if spec == _env_connect_kernel[0]:
        return _env_connect_kernel[1]
    kernel: Callable | None = None
    if spec:
        try:
            import importlib.util

            file_name, _, attribute = spec.rpartition(":")
            module_spec = importlib.util.spec_from_file_location(
                "_hbuilder_connect_kernel", file_name
            )
            module = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(module)
            kernel = getattr(module, attribute)
        except Exception as exc:  # noqa: BLE001 - any failure means "use Python"
            print(
                f"connect kernel {spec!r} could not be loaded ({exc!r}); "
                "the assignment continues on the Python path",
                file=sys.stderr,
            )
    _env_connect_kernel = (spec, kernel)
    return kernel


_HELD_VIEWS: dict = {}


def _held_view(asm: AssembledPlan, held: frozenset) -> AssembledPlan:
    """`asm` with the `held` cards dropped from every entry's card group (an entry
    left with none is dropped), remembered per (plan, held set) so the kernel packs
    each view once."""
    key = (id(asm), held)
    hit = _HELD_VIEWS.get(key)
    if hit is None or hit[0] is not asm:
        entries = tuple((socket, permil, covered, kept)
                        for socket, permil, covered, card_ids in asm.entries
                        if (kept := tuple(c for c in card_ids if c not in held)))
        if len(_HELD_VIEWS) > 50_000:
            _HELD_VIEWS.clear()
        hit = _HELD_VIEWS[key] = (asm, replace(asm, entries=entries))
    return hit[1]


def _cached_connect_assignment(
    inputs: Sequence[ConnectInput], members: tuple[Card, ...], *,
    overall_power: float, bonus_permil: float, k_constant: float,
    active_pool_rate: float, name_cards: bool = True, held: frozenset = frozenset(),
    song_scope: str = "", incumbent: Mapping[tuple[str, str], str] | None = None,
) -> tuple[dict[str, dict[str, float]], list[SocketPlacement]]:
    """The team-wide REAL socketing on the plan cache: the base node values
    (grouping on, connect OFF — only for the nodes the assignment reads; the
    same arithmetic as `power_node_values`, both call
    `node_values_from_recipes`), then `_team_connect_assignment_grouped`.

    With a `CONNECT_KERNEL` the values, the gains and the matching are computed
    there instead; the naming of the result is `_placements_from_matched`
    either way.

    `held` (cards kept on off-team sockets): the cached entries list every owned
    card of a shape group, so the held ones are dropped from each group (a group
    with none left offers nothing). The kernel is handed the same filtered entries
    (`_held_view`, one per plan and held set): with the full groups its answer
    would be thrown away whenever a group is used beyond the cards left -- about
    78 % of the leaf's teams, which would then run the Python path too.

    `incumbent` (the account's own placements, `_connect_assignment_exact`) is read
    only when the cards are NAMED: the kernel has no tie-break, so a named call with
    incumbents runs the Python matching. The search's own calls name nothing and
    keep the kernel."""
    kernel = None if (incumbent and name_cards) else _connect_kernel()
    if kernel is not None:
        matched = kernel(
            [(i[0], _held_view(i[1], held), *i[2:]) if held else i
             for i in inputs if i[1].model_id], members,
            overall_power=overall_power, bonus_permil=bonus_permil,
            k_constant=k_constant, active_pool_rate=active_pool_rate, song_scope=song_scope)
        if matched is not None:
            return _placements_from_matched(matched, name_cards=name_cards)
    per_char = []
    for chr_id, asm, recipes, is_leader, card, singer_weight in inputs:
        if not asm.model_id:
            continue
        base = node_values_from_recipes(
            recipes, is_leader=is_leader, own_card=card, member_cards=members,
            bonus_permil=bonus_permil, overall_power=overall_power,
            k_constant=k_constant, active_pool_rate=active_pool_rate,
            singer_weight=singer_weight, song_scope=song_scope,
        )
        entries = asm.entries
        if held:
            entries = [(socket, permil, covered, kept)
                       for socket, permil, covered, card_ids in entries
                       if (kept := tuple(c for c in card_ids if c not in held))]
        per_char.append((chr_id, entries, base))
    return _team_connect_assignment_grouped(per_char, incumbent if name_cards else None)


def _team_connect_assignment_grouped(
    per_char: Sequence[tuple[str, Sequence[tuple[str, int, tuple[str, ...], tuple[str, ...]]],
                             Mapping[str, float]]],
    incumbent: Mapping[tuple[str, str], str] | None = None,
) -> tuple[dict[str, dict[str, float]], list[SocketPlacement]]:
    """`_team_connect_assignment` over PRE-COMPUTED socket entries (the plan
    cache's `AssembledPlan.entries`: one per (eligible socket, distinct shape
    group), `(socket, permil, covered, id-sorted cards)`) — the same triples,
    the same exact solve, the same result. `per_char` is `(chr_id, entries,
    values)`."""
    triples: list[ConnectTriple] = []
    for chr_id, entries, values in per_char:
        for socket, permil, covered, card_ids in entries:
            gain = 0.0
            for node in covered:
                gain += values.get(node, 0.0) * permil / 1000.0
            if gain > 0:
                triples.append((gain, chr_id, socket, permil, covered, card_ids))
    return _connect_assignment_exact(triples, incumbent)


def board_model_from_plan(
    plan: TeamBoardPlan,
    leader: Card,
    members: Sequence[Card],
    skilltree: Mapping,
    node_effect_ids: Mapping[str, list[str]],
    *,
    overall_power: float,
    bonus_permil: float,
    k_constant: float,
    active_pool_rate: float,
    connect_cards: Mapping[str, tuple[Sequence[Sequence[int]], int]] | None = None,
    placements_out: list[SocketPlacement] | None = None,
    bench_characters: Mapping[str, Sequence[Mapping]] | None = None,
    name_by_chr: Mapping[str, str] | None = None,
    sings: bool = False,
    plan_cache: PlanCache | None = None,
    fixed_slots: Mapping[str, Mapping[str, str]] | None = None,
    socket_bench: bool = False,
    bench_sockets: Mapping[str, Mapping[str, str | None]] | None = None,
) -> BoardModel:
    """Turn a plan into a real `BoardModel` so it can be RE-SCORED exactly. The
    per-node valuation is a linearisation (it sums per-node Unit-Score deltas);
    scoring the assembled board captures the true multiplicative compounding of
    power and bonus, which is what makes the reported gain exact rather than the
    ~+75% the linearised sum reports against the exact +99%.

    Connect: with `connect_cards` (`card id -> (shape offsets, permil at the
    card's resolved level)`, the account's actual Connect cards) the multipliers
    come from a REAL team-wide assignment, solved EXACTLY as a maximum-weight
    matching (`_connect_assignment_exact`) — one card, one socket, sockets
    beyond the free root only if bought — reported through `placements_out`.
    Without it, the idealised model applies: every socket takes the best shape
    in the game at max bloom (an optimistic upper bound, ~+4-5 pp above the real
    assignment on a real account). Node SELECTION
    (`optimize_team_boards`) still values under the idealised model either way;
    only the re-score and the reported sockets change.

    `bench_characters`, when given, is the account's CURRENT board for
    characters outside this team (`board.json`'s name-keyed `characters`
    mapping, connect multipliers already resolved). Their GREEN nodes are
    account-global, so any team's score carries them (leaving them out reads
    ~4 % low).
    A character this team develops takes the PLAN's board; its bench entry,
    if any, is REPLACED, never merged.

    `name_by_chr` (chr-id -> holomem name) is what lets the per-song scorer
    match a chart's chr-id CAST against this name-keyed board. The team's own
    cards always contribute their mapping; a caller with the whole roster
    (bench singers whose yellow nodes count from outside the team) widens it.
    Without it every singer-conditional term — R-044/062/063 "+stat% to all
    when this holomem sings", R-002, the yellow solo/group song bonus — is
    silently ZERO on the develop path, and the song-scope bonus then replaces
    the pooled bucket with 0, so a leader scores LOWER on their own song than on
    a stranger's.

    `sings` is the LEADER's singer context, the same flag the planner takes
    (`power_node_values.singer_weight`). It matters here because the socket
    placement re-values the nodes (`base` below) to decide which Connect socket
    is worth which shape: without it the leader's singer-conditional reds are
    valued 0 even on a plan that bought them BECAUSE she sings, and the team-wide
    connect assignment then moves the sockets off the branch that carries them
    (e.g. Calli's S-001 valued 18,870 instead of 92,154 -> -1.41 % on m0334).
    Default `False` is the song-agnostic valuation.

    `plan_cache`, when given together with a plan whose `plan_keys` it filled
    (`cached_team_plan`), takes the FAST path: the node list,
    parsed effects and socket coverage come from `PlanCache.assembled_for`,
    the base values are computed only for the nodes the Connect assignment
    reads, the assignment runs per distinct shape, and the `BoardModel` is
    built directly. Same floats as the path below — proven by
    `tests/test_fast_leaf.py` — only fewer seconds.

    `fixed_slots` (`chr_id -> {socket: card id}`, with `connect_cards`): the
    team's sockets are taken AS THEY ARE instead of assigned
    (`_fixed_connect_placements`) — the account's real boards. It disables the
    plan-cache fast path, which only knows the assignment.

    `bench_sockets` (`chr_id -> {unlocked socket: card id or None}`, the account's
    own placements, `board_state.json`) says what the OFF-TEAM boards' sockets hold.
    A card placed on a bench socket keeps lifting that board (its multiplier is already
    in `bench_characters`), so the team's matching may not use it too, or the card
    counts twice. Without `socket_bench` the cards that lift a bench GREEN are
    taken out of the team's pool (`held_bench_cards`); with it EVERY unlocked bench
    socket joins the matching
    (`_bench_socket_rows`), so unlocked-but-empty bench sockets can be filled
    too. `socket_bench` disables the plan-cache fast path (its bench boards are
    shared across teams)."""
    members = tuple(members)
    if fixed_slots is not None or socket_bench:
        plan_cache = None
    names: dict[str, str] = {
        "chr-" + card.id.split("-")[1]: card.member for card in (*members, leader)
    }
    if name_by_chr:
        names = {**dict(name_by_chr), **names}
    team_chrs = {"chr-" + card.id.split("-")[1] for card in (*members, leader)}
    # the song's singers when known (a frozenset); a bare leader flag says nothing
    # about the bench, so no bench yellow is counted then
    cast = sings if isinstance(sings, (set, frozenset)) else None
    held = (frozenset() if socket_bench or fixed_slots is not None
            else held_bench_cards(bench_sockets, team_chrs, skilltree, connect_cards, cast=cast,
                                  bought=_bench_bought(bench_characters, name_by_chr)))
    # the account's own placements: on a tie a card stays where it is
    incumbent = {(chr_id, socket): card for chr_id, slots in (bench_sockets or {}).items()
                 for socket, card in (slots or {}).items() if card}
    if plan_cache is not None and plan.plan_keys:
        fast = _board_model_from_cache(
            plan, leader, members, skilltree, node_effect_ids, names,
            overall_power=overall_power, bonus_permil=bonus_permil,
            k_constant=k_constant, active_pool_rate=active_pool_rate,
            connect_cards=connect_cards, placements_out=placements_out,
            bench_characters=bench_characters, sings=sings, cache=plan_cache, held=held,
            incumbent=incumbent)
        if fast is not None:
            return fast
        # the song bonus is over its cap: only the capped matching below is right
    if held and connect_cards is not None:
        connect_cards = {k: v for k, v in connect_cards.items() if k not in held}
    chars: dict[str, list] = {
        name: list(nodes) for name, nodes in (bench_characters or {}).items()
    }
    seen: set[str] = set()
    # (card, chr_id, model_id, cplan, base) in build order; chars assembled after
    # the team-wide assignment is known. ONE entry per CHARACTER (one board per
    # holomem), leader first: a holomem who leads and also fields a member card is
    # valued in both roles at once -- her reds reach the members, her blues her member
    # card -- as `optimize_team_boards` plans her. Keyed per card, the member entry
    # would replace the leader's in the matching and her reds would read 0 (up to
    # -4.07 % on such teams).
    member_by_chr = {"chr-" + m.id.split("-")[1]: m for m in members}
    ctx: list[tuple[Card, str, str, object, dict[str, float]]] = []
    for card, is_leader in [(leader, True), *[(m, False) for m in members]]:
        chr_id = "chr-" + card.id.split("-")[1]
        if chr_id in seen:
            continue
        cplan = plan.per_character.get(card.id)
        if cplan is None:
            continue
        seen.add(chr_id)
        model_id = model_for_character(skilltree, chr_id) or ""
        # base node values (grouping on, connect OFF) to place the sockets.
        base = power_node_values(
            node_effect_ids, is_leader=is_leader,
            own_card=member_by_chr.get(chr_id, card) if is_leader else card, member_cards=members,
            bonus_permil=bonus_permil, overall_power=overall_power,
            k_constant=k_constant, active_pool_rate=active_pool_rate,
            skilltree={"nodes": skilltree["nodes"]}, model_id="", own_chr=chr_id,
            singer_weight=singer_weight_for(sings, chr_id, is_leader),
            song_scope=song_scope_of(sings),
        )
        ctx.append((card, chr_id, model_id, cplan, base))
    if connect_cards is not None:
        per_char = {
            chr_id: (skilltree.get("models", {}).get(model_id, {}),
                     cplan.selected, base)
            for _card, chr_id, model_id, cplan, base in ctx if model_id
        }
        bench_rows: dict[str, tuple[str, frozenset]] = {}
        if fixed_slots is None and socket_bench and bench_characters:
            bench_rows = _bench_socket_rows(
                chars, names, set(per_char), skilltree, node_effect_ids, members,
                overall_power=overall_power, bonus_permil=bonus_permil, k_constant=k_constant,
                connect_cards=connect_cards, per_char=per_char, bench_sockets=bench_sockets,
                cast=cast)
        if fixed_slots is not None:
            mults_by_chr, placements = _fixed_connect_placements(
                per_char, connect_cards, fixed_slots)
        elif cast is not None:
            inverse = {name: chr_id for chr_id, name in names.items()}
            fixed = [(inverse.get(name), e) for name, rows in chars.items()
                     if inverse.get(name) not in per_char for e in rows]
            mults_by_chr, placements = _yellow_capped_assignment(
                per_char, connect_cards, cast=cast, skilltree=skilltree,
                node_effect_ids=node_effect_ids, fixed_rows=fixed, incumbent=incumbent)
        else:
            mults_by_chr, placements = _team_connect_assignment(per_char, connect_cards, incumbent)
        # the bench boards' sockets re-assigned in the same matching: drop what the
        # account's cards did there (`cover`: the nodes the re-assigned root's shapes can
        # reach; None = every socket of the board was re-assigned) and apply the new ones
        for chr_id, (name, cover) in bench_rows.items():
            cmult = mults_by_chr.get(chr_id, {})
            chars[name] = [
                dict(e, connect_multiplier=cmult.get(e["node"], 1.0)
                     if cover is None or e.get("node") in cover else e.get("connect_multiplier", 1.0))
                for e in chars[name]]
        if placements_out is not None:
            placements_out.extend(placements)
    else:
        mults_by_chr = {
            chr_id: (_connect_multipliers(skilltree, model_id, base)
                     if model_id else {})
            for _card, chr_id, model_id, _cplan, base in ctx
        }
    for card, chr_id, _model_id, cplan, _base in ctx:
        cmult = mults_by_chr.get(chr_id, {})
        nodes = []
        for node_id in cplan.selected:
            if node_id == ROOT_NODE:
                continue
            effstr = _effect_string_for(skilltree, node_id, chr_id, node_effect_ids)
            if effstr is None:
                continue
            nodes.append({"node": node_id, "effects": [effstr],
                          "connect_multiplier": cmult.get(node_id, 1.0)})
        chars[card.member] = nodes
    if plan_cache is not None and bench_characters is not None:
        # the plain path with a plan cache at hand (the song bonus over its cap, or a
        # refined plan): the ~50 unchanged bench boards, their digests and `BoardBase`
        # come from the cache as on the fast path, and only the team's boards are
        # parsed -- the same `CharacterBoard`s, the same scores, ~5 s less per request
        boards = dict(plan_cache.bench_boards(bench_characters))
        digests = dict(plan_cache.bench_digests(bench_characters))
        for card, _chr_id, _model_id, _cplan, _base in ctx:
            boards[card.member] = board_from_mapping(
                {"characters": {card.member: chars[card.member]}}).characters[card.member]
            digests.pop(card.member, None)
        return BoardModel(characters=boards, name_by_chr=dict(names), _digests=digests,
                          _base=plan_cache.bench_base(bench_characters))
    return board_from_mapping({"characters": chars, "name_by_chr": names})


def _board_model_from_cache(
    plan: TeamBoardPlan, leader: Card, members: tuple[Card, ...], skilltree: Mapping,
    node_effect_ids: Mapping[str, list[str]], names: dict[str, str], *,
    overall_power: float, bonus_permil: float, k_constant: float,
    active_pool_rate: float, connect_cards, placements_out, bench_characters,
    sings: bool, cache: PlanCache, held: frozenset = frozenset(), incumbent=None,
) -> BoardModel:
    """`board_model_from_plan`'s fast path — see its docstring. `held`: cards on the
    off-team boards' sockets, kept out of the team's matching (`held_bench_cards`).
    None when the team's paying yellows exceed the song cap: the matching here does not
    apply it (`_yellow_capped_assignment` does), so the caller takes the plain path."""
    seen: set[str] = set()
    ctx: list[tuple[Card, str, AssembledPlan]] = []
    inputs: list[ConnectInput] = []
    # one entry per character, the leader valued with her member card's blues too
    # (the plain path's rule, see there)
    member_by_chr = {"chr-" + m.id.split("-")[1]: m for m in members}
    for card, is_leader in [(leader, True), *[(m, False) for m in members]]:
        chr_id = "chr-" + card.id.split("-")[1]
        if chr_id in seen:
            continue
        cplan = plan.per_character.get(card.id)
        if cplan is None:
            continue
        seen.add(chr_id)
        asm = cache.assembled_for(
            plan.plan_keys[card.id], cplan, chr_id, skilltree=skilltree,
            node_effect_ids=node_effect_ids, connect_cards=connect_cards)
        recipes = cache.needed_recipes(
            plan.plan_keys[card.id], asm, chr_id, node_effect_ids, skilltree)
        ctx.append((card, chr_id, asm))
        inputs.append((chr_id, asm, recipes, is_leader,
                       member_by_chr.get(chr_id, card) if is_leader else card,
                       singer_weight_for(sings, chr_id, is_leader)))
    if connect_cards is not None:
        mults_by_chr, placements = _cached_connect_assignment(
            inputs, members, overall_power=overall_power, bonus_permil=bonus_permil,
            k_constant=k_constant, active_pool_rate=active_pool_rate,
            name_cards=placements_out is not None, held=held, song_scope=song_scope_of(sings),
            incumbent=incumbent)
        cast = sings if isinstance(sings, (set, frozenset)) else None
        if cast is not None and _song_bonus_over_cap(
                [(chr_id, plan.per_character[card.id].selected, mults_by_chr.get(chr_id, {}))
                 for card, chr_id, _asm in ctx],
                bench_characters, names, cast=cast, skilltree=skilltree,
                node_effect_ids=node_effect_ids):
            return None
        if placements_out is not None:
            placements_out.extend(placements)
    else:
        mults_by_chr = {}
        for chr_id, asm, recipes, is_leader, card, singer_weight in inputs:
            # base node values (grouping on, connect OFF) to place the sockets
            base = node_values_from_recipes(
                recipes, is_leader=is_leader, own_card=card, member_cards=members,
                bonus_permil=bonus_permil, overall_power=overall_power,
                k_constant=k_constant, active_pool_rate=active_pool_rate,
                singer_weight=singer_weight, song_scope=song_scope_of(sings),
            )
            mults_by_chr[chr_id] = (
                _connect_multipliers(skilltree, asm.model_id, base)
                if asm.model_id else {})
    chars: dict[str, CharacterBoard] = dict(cache.bench_boards(bench_characters))
    digests: dict[str, BoardDigest] = dict(cache.bench_digests(bench_characters))
    for card, chr_id, asm in ctx:
        chars[card.member], digests[card.member] = cache.team_board(
            plan.plan_keys[card.id], card.member, asm, mults_by_chr.get(chr_id, {}))
    return BoardModel(characters=chars, name_by_chr=dict(names), _digests=digests,
                      _base=cache.bench_base(bench_characters))


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
    sings: bool | Callable[[Card], bool] = False,
    bench_sockets: Mapping[str, Mapping[str, str | None]] | None = None,
) -> list[BoardOptimisedTeam]:
    """The bounded JOINT team+board pass: board-optimise every team and RE-RANK by
    the board-optimised Unit Score.

    `sings` is the leader's singer context (`power_node_values.singer_weight`):
    a bool for the whole list, or `Callable[[leader Card], bool]` when the
    teams have different leaders on one song. `plan_cache`, when given, takes
    every plan from `cached_team_plan` (fixed per (character, role, budget,
    singer context) at the cache's nominal context) instead of re-planning per
    team; the default `None` keeps the per-team `optimize_team_boards` path
    unchanged.

    A team a board-less ranking placed second may win once each team's board is
    developed — the board is 29-49 % of Overall Power and is PER-team, so
    rank-then-show-#1's-plan can miss the joint optimum. This is exactly what the
    CLI's `--optimize-board` does; it lives here, not in the CLI, so the web serves
    the SAME re-rank rather than a second implementation free to drift from it.

    Each team is scored board-off for its base, its board optimised IN-PROCESS
    (`optimize_team_boards` — the fast greedy knapsack, NOT the slow `tree_optimise`
    subprocess, so re-ranking the top-K is seconds not minutes), then the ASSEMBLED
    board is re-scored (`board_model_from_plan`) for the EXACT optimised Unit Score:
    the plan's `total_gain` is a linearised sum, while scoring the real board
    captures the power × bonus compounding. Sorted by optimised Unit Score,
    highest first — the caller reads that order as the ranking."""
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
                team.leader, team.members, cache=plan_cache, tree_for=tree_for,
                budget_for=budget_for, node_effect_ids=node_effect_ids,
                skilltree=skilltree, sings=leader_sings,
            )
        else:
            plan = optimize_team_boards(
                team.leader, team.members, tree_for=tree_for, budget_for=budget_for,
                node_effect_ids=node_effect_ids, overall_power=ds.overall_power,
                bonus_permil=ds.score_bonus_permil, k_constant=UNIT_SCORE_K,
                active_pool_rate=bare_pool, skilltree=skilltree,
                singer_weight=singer_weight_for(leader_sings, _chr_of(team.leader), True),
                singers=leader_sings if isinstance(leader_sings, (set, frozenset)) else None,
            )
        placements: list[SocketPlacement] = []
        board = board_model_from_plan(
            plan, team.leader, team.members, skilltree, node_effect_ids,
            overall_power=ds.overall_power, bonus_permil=ds.score_bonus_permil,
            k_constant=UNIT_SCORE_K, active_pool_rate=bare_pool,
            connect_cards=connect_cards, placements_out=placements,
            bench_characters=bench_characters, name_by_chr=name_by_chr,
            sings=leader_sings, plan_cache=plan_cache, bench_sockets=bench_sockets,
        )
        opt_us = deck_screen_unit_score(
            score_with_breakdown(team, replace(cfg_off, board=board))
        ).unit_score
        rows.append(
            BoardOptimisedTeam(
                team=team, board_off_us=ds.unit_score, optimised_us=opt_us,
                plan=plan, board=board, sockets=tuple(placements),
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
        node_id for node_id, meta in (skilltree.get("nodes") or {}).items()
        if ((meta or {}).get("e") or {}).get("t") in ACTIVATION_EFFECT_TYPES
    )


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
    the chart's cues — the active Score UP is a per-tick MAX, so an extra
    activation can land on top of another and buy nothing, or shift a window off
    a cue. No per-node value captures that, and the greedy planner over-buys them:
    on six songs x two bases, dropping some of them raises the song score by
    0.2-3.1 %.

    Coordinate descent over single moves, first improvement kept: for each
    character in turn, DROP an activation node the rest of its board does not
    hang from, or ADD one (its cheapest path) that the character's points still
    cover. `score(plan)` is the caller's real objective (a song score on the board
    assembled from `plan`); a move is kept only when it raises it, so the result
    is never worse than the start. Freed points stay unspent.

    The returned plan carries no `plan_keys`: it is not the plan cache's
    assembled plan, so assemble its board WITHOUT `plan_cache`. Returns
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
            budget = budget_for(card)
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
                trial = replace(plan, per_character={
                    **plan.per_character,
                    card_id: BoardPlan(selected=nodes, spent=tree.cost_of(nodes - {ROOT_NODE}),
                                       value=current.value, steps=current.steps)})
                value = score(trial)
                if value > best:
                    plan, best = trial, value
                    moves.append(f"{card_id} {label}")
                    improved = True
                    break
            if len(moves) >= max_moves:
                break
    return plan, best, moves


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
    sings: bool = False,
    player_level: int | None = None,
    bench_sockets: Mapping[str, Mapping[str, str | None]] | None = None,
) -> tuple[BoardOptimisedTeam, float, list[str]]:
    """`optimize_and_rerank`'s develop for ONE team, then
    `refine_activation_nodes` on `charts_score(board)` — the caller's real
    objective, e.g. the song score on one chart or the sum over a few. The board
    is re-assembled exactly as `optimize_and_rerank` assembles it (same
    linearisation point, Connect cards, bench, singer context), never from the
    plan cache once the plan has been refined. Returns the same
    `BoardOptimisedTeam` `optimize_and_rerank` would (its Unit Score and real
    socket placements recomputed on the refined board), `charts_score` of that
    board, and the moves made."""
    cfg_off = replace(cfg, board=None)
    bd = score_with_breakdown(team, cfg_off)
    ds = deck_screen_unit_score(bd)
    bare_pool = bd.active_pool / (1.0 + bd.combined_support_rate)
    start = optimize_and_rerank(
        [team], cfg, tree_for=tree_for, budget_for=budget_for, node_effect_ids=node_effect_ids,
        skilltree=skilltree, connect_cards=connect_cards, bench_characters=bench_characters,
        name_by_chr=name_by_chr, plan_cache=plan_cache, sings=sings,
        bench_sockets=bench_sockets)[0]

    def assemble(plan: TeamBoardPlan, placements: list | None = None) -> BoardModel | None:
        if plan is start.plan:
            if placements is not None:
                placements.extend(start.sockets)
            return start.board
        return board_model_from_plan(
            plan, team.leader, team.members, skilltree, node_effect_ids,
            overall_power=ds.overall_power, bonus_permil=ds.score_bonus_permil,
            k_constant=UNIT_SCORE_K, active_pool_rate=bare_pool,
            connect_cards=connect_cards, placements_out=placements,
            bench_characters=bench_characters, name_by_chr=name_by_chr, sings=sings,
            bench_sockets=bench_sockets,
            # a refined plan carries no `plan_keys` (`refine_activation_nodes`), so this
            # never takes the cache's fast path: it only reuses the parsed bench
            plan_cache=plan_cache)

    cards = {c.id: c for c in (team.leader, *team.members)}
    plan, value, moves = refine_activation_nodes(
        start.plan, cards, tree_for=tree_for, budget_for=budget_for,
        activation_nodes=activation_node_ids(skilltree),
        score=lambda p: charts_score(assemble(p)), player_level=player_level)
    if not moves:              # unchanged: keep the start exactly (its plan-cache board)
        return start, value, moves
    placements: list[SocketPlacement] = []
    board = assemble(plan, placements)
    opt_us = deck_screen_unit_score(score_with_breakdown(team, replace(cfg_off, board=board))).unit_score
    return (BoardOptimisedTeam(team=team, board_off_us=ds.unit_score, optimised_us=opt_us,
                               plan=plan, board=board, sockets=tuple(placements)),
            value, moves)


def develop_and_refine(team: Team, cfg: ScoringConfig, charts_score: Callable[[BoardModel], float],
                       **kwargs) -> tuple[BoardModel | None, TeamBoardPlan, float, list[str]]:
    """`develop_refined_team` as `(board, plan, charts_score(board), moves)`."""
    bt, value, moves = develop_refined_team(team, cfg, charts_score, **kwargs)
    return bt.board, bt.plan, value, moves


def refine_members(
    seed: BoardOptimisedTeam,
    *,
    candidates: Sequence[Card],
    develop_fn: Callable[[Team], BoardOptimisedTeam],
    score_fn: Callable[[Team, BoardModel | None], float],
    prefilter_fn: Callable[[Team, BoardModel | None], float] | None = None,
    batch_score_fn: Callable[[Sequence[Team]], Sequence[float]] | None = None,
    shortlist: int = 8,
    max_rounds: int = 2,
    min_gain: float = 1.0,
) -> tuple[BoardOptimisedTeam, float, list[dict]]:
    """Hill-climb the five MEMBER slots of one developed team by the TARGET
    metric, leader fixed. The beam picks each leader's members by Unit Score,
    but the per-song winner can differ (e.g. Noel's per-song members are not her
    Unit-Score members).

    Two-stage per slot, so the full metric is paid only where it can matter:
    every candidate is first scored on the SEED'S OWN developed board without
    re-developing (`score_fn(team, incumbent board)` — 1 ms, rank agreement
    with the full metric Spearman ~+0.82..0.86, top-5 overlap 3-4/5), then only the `shortlist` best are
    re-developed (`develop_fn`, ~20 ms each) and scored exactly. The best
    exact improvement across all five slots is accepted per round (steepest
    ascent); rounds stop when no swap beats the incumbent by `min_gain`.

    `develop_fn(team)` returns the team's `BoardOptimisedTeam` (board + real
    sockets); `score_fn(team, board)` is the target metric — per-song estimate,
    across-sample mean, whatever the caller ranks by. Injected so this stays
    pure orchestration: the server wires the real ones, a test wires stubs.

    `prefilter_fn`, when given, replaces `score_fn` in the CHEAP stage only —
    the across case, where even one `score_fn` (a mean over sampled charts) is
    too dear to pay per candidate; a coarser sample orders the shortlist and
    the real metric still makes every accept/reject decision. Ranking noise in
    the prefilter costs recall (a good candidate can miss the shortlist),
    never correctness.

    `batch_score_fn(teams)`, when given, computes the EXACT stage for a whole
    round's shortlists at once — `score_fn(team, develop_fn(team).board)` for
    each, in the same order — which is what a process pool parallelises
    (`solver/parallel_refine.py`). The accepted winner is then re-developed
    in-process through `develop_fn` so the returned `BoardOptimisedTeam`
    carries its board and sockets. Same accept/reject decisions, same numbers,
    fewer wall-clock seconds; the default is the serial equivalent.

    Returns `(refined, refined_score, swaps)`; `swaps` records each accepted
    change as `{"slot", "out", "in", "gain"}`. No improvement -> the seed
    itself, its score, and `[]`."""
    cheap_fn = prefilter_fn if prefilter_fn is not None else score_fn
    current = seed
    current_score = score_fn(seed.team, seed.board)
    swaps: list[dict] = []
    for _round in range(max_rounds):
        # every slot's shortlist first, then ONE exact stage over all of them
        pending: list[tuple[int, Card, Team]] = []
        for slot in range(len(current.team.members)):
            incumbent = current.team.members[slot]
            scored: list[tuple[float, Card, Team]] = []
            for cand in candidates:
                if cand.id == incumbent.id:
                    continue
                members = list(current.team.members)
                members[slot] = cand
                try:
                    mutated = Team(leader=current.team.leader,
                                   members=tuple(members))
                except TeamError:
                    continue
                scored.append((cheap_fn(mutated, current.board), cand, mutated))
            scored.sort(key=lambda s: -s[0])
            pending.extend((slot, cand, mutated)
                           for _keep, cand, mutated in scored[:shortlist])
        built: dict[int, BoardOptimisedTeam] = {}
        if batch_score_fn is not None:
            exacts = list(batch_score_fn([m for _s, _c, m in pending]))
        else:
            exacts = []
            for k, (_s, _c, m) in enumerate(pending):
                built[k] = develop_fn(m)
                exacts.append(score_fn(m, built[k].board))
        best: tuple[float, int, Card, Team, int] | None = None
        for k, ((slot, cand, mutated), exact) in enumerate(zip(pending, exacts)):
            if exact > current_score + min_gain and (
                best is None or exact > best[0]
            ):
                best = (exact, slot, cand, mutated, k)
        if best is None:
            break
        exact, slot, cand, mutated, k = best
        # serial: the winner was developed in the exact stage; batched: the
        # workers returned scores only, so develop the ONE winner here.
        developed = built.get(k) or develop_fn(mutated)
        swaps.append({"slot": slot, "out": current.team.members[slot].id,
                      "in": cand.id, "gain": exact - current_score})
        current, current_score = developed, exact
    return current, current_score, swaps
