"""Node values and per-team board plans: the real `SkillTree` from the dumps, each node's
value in Unit-Score terms for one character in one team (`power_node_values`,
`bench_node_values`, the compiled `NodeRecipe`s), the Gen/grouping cap's room and supply,
and `optimize_team_boards`, which plans every board of one team.

Split out of `board_plan.py`, which re-exports every name here."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field

from hbuilder.domain.board import (
    _GENERATION_BY_GROUPING,
    _GROUPING_RE,
    STAT_NAMES,
    parse_effect_id,
)
from hbuilder.domain.board_optimizer import BoardPlan, optimize_board
from hbuilder.domain.card import Card
from hbuilder.domain.skilltree import SkillTree, tree_from_mapping


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


def _grouping_node_value(override: Mapping, member_cards, d_us_per_power: float) -> float:
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
# through one loses to an equal path around it (at 0 the tie falls to set order, which
# differs per process, so the pool and the serial path would plan different boards)
NO_GROUPING_VALUE = -1e-6


def grouping_room(
    skilltree: Mapping, bench_characters: Mapping | None, name_by_chr: Mapping[str, str]
) -> dict[str, dict[str, float]]:
    """`{chr: {node: share in [0, 1]}}`: how much of each character's G-008/011/021 chain
    still counts, the cap less what the OTHER boards of her grouping already supply
    (`bench_characters`, the boards the leaf scores the off-team holomems with).
    Team-invariant, so the per-character plan cache stays valid: the planner values a
    chain at its room instead of at full. What the teammates' chains take is
    `cached_team_plan`'s surplus pass; the score itself is exact
    (`board._close_grouping`)."""
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


def grouping_supply(
    skilltree: Mapping, bench_characters: Mapping | None, name_by_chr: Mapping[str, str]
) -> dict[str, dict[str, float]]:
    """`{grouping: {chr: amount}}`: the generation greens each board of
    `bench_characters` gives its grouping, Connect-scaled as the scorer reads them."""
    nodes = skilltree.get("nodes", {})
    chr_by_name = {v: k for k, v in (name_by_chr or {}).items()}
    supply: dict[str, dict[str, float]] = {}  # grouping -> {chr: owned amount}
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
                    per[cid] = per.get(cid, 0.0) + float(over.get("v") or 0) * float(
                        row.get("connect_multiplier") or 1.0
                    )
    return supply


def _apply_grouping_room(values: dict[str, float], skilltree: Mapping | None, own_chr: str) -> None:
    room = ((skilltree or {}).get(GROUPING_ROOM_KEY) or {}).get(own_chr)
    if not room:
        return
    for nid, share in room.items():
        if nid in values:
            values[nid] *= share


def _connect_multipliers(
    skilltree: Mapping, model_id: str, node_values: Mapping[str, float]
) -> dict[str, float]:
    """Per-node connect multiplier from OPTIMAL socketing (the idealised model). Each
    of the board's 4 sockets sits at the base of a branch; a card placed there
    multiplies every unlocked node in its fixed shape (anchored at the socket) by
    `1 + v/1000` (bloom 5 = the higher of the two connect levels). Shapes of
    different sockets never overlap, so each socket independently takes the shape
    that maximises the value of the nodes it covers."""
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


# One node's PRE-PARSED valuation input: `(node_id, chr override or None, parsed
# effects)`, a pure function of (node_effect_ids, the skilltree node table, own
# character). The parse and the override lookup dominate `power_node_values` and do
# not depend on the team, so a plan cache compiles once per character and re-runs
# only the arithmetic per team. Both paths share `node_values_from_recipes`.
NodeRecipe = tuple[str, Mapping | None, tuple]


def compile_node_recipes(
    node_effect_ids: Mapping[str, list[str]],
    skilltree_nodes: Mapping | None,
    own_chr: str,
) -> tuple[NodeRecipe, ...]:
    """`(node_id, override, effects)` per node, in `node_effect_ids` order. A
    character-grouping green carries its chr override and no parsed effects (it is
    valued whole by `_grouping_node_value`); every other node carries the effects
    that parse (an id this channel cannot route is skipped, never guessed)."""
    out: list[NodeRecipe] = []
    for node_id, effect_ids in node_effect_ids.items():
        branch = node_id[:1]
        # A character-grouping node's exact effect is per-character: skilltree's chr
        # override (the master rows are every grouping's variant). Only a grouping
        # green's override names a grouping (`tgt`); other nodes can carry a chr
        # override with the master's own effect and are parsed as usual (read as a
        # grouping they would be worth 0).
        override = (
            skilltree_nodes.get(node_id, {}).get("chr", {}).get(own_chr)
            if skilltree_nodes is not None
            else None
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
    recipes, in recipe order."""
    d_us_per_power = (1.0 + bonus_permil / 1000.0) * k_constant
    d_us_per_bonus = overall_power * k_constant  # per unit of score-bonus rate
    # per unit of the SONG's score rate (a yellow): it multiplies every note
    # (`song.py`, `* (1.0 + board_score_rate)`), i.e. the whole Unit Score
    # P * K * (1 + b), not a summand of the deck screen's Score Bonus. Priced at
    # `d_us_per_bonus` a singer's own yellows would be worth (1 + b) ~ 1.9x too
    # little, and a singing leader's board would buy reds instead of her solo
    # yellows and her yellow socket (-7.5 % on a re-planned song score).
    d_us_per_song = overall_power * d_us_per_power
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
                reached = (own_card,) if own_in_members else ()  # type: ignore[assignment]
            elif eff.scope == "grouping":
                continue  # needs the team's grouping composition: not valued here
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
                    # `len(reached) * len(stats)` repeated additions, not one product:
                    # a product rounds differently
                    add = eff.value * d_us_per_power
                    for _ in range(len(reached) * len(stats)):
                        row += add
            elif eff.kind == "song_score_bonus" and reached:
                # Y: `solo`/`group` count only when this holomem sings a song of
                # that scope; `all` is the pooled bucket every score counts.
                # A per-UNIT constant, not per-permil: `BoardEffect.value` is already
                # a fraction for every non-stat kind (`board.py`), and dividing again
                # would value yellows ~1/1000 too low (as for `skill_effect_up` and
                # `activation_rate_up`).
                # The scorer pays ONE scope per song (`song_scope`, the song's
                # `singer_type`): a singer's yellows of the other scope are worth 0;
                # "" (not known) keeps both. The pooled `all` yellows pay only when the
                # song is neither solo nor group: a cast's own scope supersedes that
                # bucket (`song.py`, `song_score_bonus_for`).
                if eff.song_scope in ("solo", "group"):
                    scope_weight = singer_weight if song_scope in ("", eff.song_scope) else 0.0
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
    `(1 + bonus/1000) * K` per point and a whole unit of score-bonus rate
    `Power * K`. Reach by branch: R reaches all members, only while this character
    leads; B reaches its own card, only when that card is a MEMBER (the leader slot
    adds no Member Parameter, `team.py`); G reaches every member.

    `singer_weight` is the plan's singer context. Every `BoardEffect.conditional`
    node (R-002, R-044/062/063: the `music_skill_tree_character` trigger that fires
    only when this holomem sings the song) is valued times `singer_weight`: 0.0 (the
    default) is the song-agnostic plan, 1.0 a song this holomem sings. `solo` /
    `group` yellows count only when this holomem is in the cast of a song of that
    type, so they carry `singer_weight` too; an `all` yellow is the pooled bucket
    every song-agnostic score counts and keeps weight 1.0.

    The ACTIVE-pool channels ride on `active_pool_rate` (the team's bare ACTIVE
    Score-UP pool): `skill_effect_up` scales the whole pool (`pool * V`),
    `activation_rate_up` / `cooldown_shorten` one member's share (`pool /
    MEMBER_COUNT * V`). A leader that fields no active (a pure outfit-leader) owns
    no share, so its own-card active channels are worth zero."""
    recipes = compile_node_recipes(node_effect_ids, skilltree["nodes"] if skilltree else None, own_chr)
    values = node_values_from_recipes(
        recipes,
        is_leader=is_leader,
        own_card=own_card,
        member_cards=member_cards,
        bonus_permil=bonus_permil,
        overall_power=overall_power,
        k_constant=k_constant,
        active_pool_rate=active_pool_rate,
        singer_weight=singer_weight,
        song_scope=song_scope,
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
    cast: AbstractSet[str] | None = None,
) -> dict[str, float]:
    """`power_node_values` for a character who is NOT on the team (role `bench`).

    Its account-global GREENs reach the team; its reds (need it to lead) and blues
    (need its card among the members) are worth 0. Its song YELLOWS reach the song's
    score whoever is on the team (`bench_yellow_pays`), so with `cast` (the song's
    singers; empty for a song with no cast) they keep their value, and the bench
    singer's yellow socket keeps its card; `cast` None keeps the greens only.
    `member_cards` are the cards the greens reach (the team, or a plan cache's
    nominal roster). Connect multipliers are applied to the kept values, so a socket
    is placed for what a bench board really lifts."""
    values = power_node_values(
        node_effect_ids,
        is_leader=False,
        own_card=None,
        member_cards=member_cards,
        bonus_permil=bonus_permil,
        overall_power=overall_power,
        k_constant=k_constant,
        active_pool_rate=0.0,
        skilltree={"nodes": skilltree["nodes"]} if skilltree else None,
        model_id="",
        own_chr=own_chr,
        singer_weight=0.0 if cast is None else 1.0,
        song_scope=song_scope_of(cast),
    )
    values = {
        n: (
            v
            if n.startswith("G")
            or (skilltree and bench_yellow_pays(yellow_scope(skilltree, n, own_chr), own_chr, cast))
            else 0.0
        )
        for n, v in values.items()
    }
    _apply_grouping_room(values, skilltree, own_chr)
    if skilltree and model_id:
        for node_id, m in _connect_multipliers(skilltree, model_id, values).items():
            if node_id in values:
                values[node_id] *= m
    return values


@dataclass(frozen=True, slots=True)
class TeamBoardPlan:
    """The board development chosen across a whole team and its Unit-Score gain.
    Each character's board is a separate point pool funded by her own level, so the
    team plan is six independent per-character plans: the leader valued while
    leading (her reds reach all members), each member on its own card."""

    total_gain: float
    per_character: Mapping[str, BoardPlan]
    # `card id -> plan_key(...)` when the plans came from a `PlanCache`
    # (`cached_team_plan`), empty otherwise: lets `board_model_from_plan` find each
    # character's assembled pieces (`PlanCache.assembled_for`).
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
    singers: AbstractSet[str] | None = None,
    prefer: Mapping[str, frozenset[str]] | None = None,
) -> TeamBoardPlan:
    """Optimise every distinct character's board for this team and sum the gain.
    `prefer`: `{chr: nodes the account has}`, kept where it costs nothing (`optimize_board`).

    `singer_weight` is the LEADER's singer context (`power_node_values`): 1.0
    when the leader is a credited singer of the song this plan is for, 0.0 for
    the song-agnostic plan. A member is valued as a singer when `singers` (the
    song's cast, chr ids) holds her holomem -- her own solo/group yellows pay on
    her board whoever leads; without it, members are valued at 0.0."""
    members = tuple(members)
    # ONE plan per distinct CHARACTER (one board per holomem), leader first: a
    # leader whose holomem also fields a member card is valued as the leader (her
    # reds reach the team) AND for that member card (her blues reach the scoring
    # card). Two plans would overwrite the name-keyed board and drop every red.
    by_chr = {"chr-" + m.id.split("-")[1]: m for m in members}
    seen: set[str] = set()
    plans: dict[str, BoardPlan] = {}
    values_by_chr: dict[str, tuple] = {}  # members holding a generation chain
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
            model_id=(model_for_character(skilltree, "chr-" + card.id.split("-")[1]) or "")
            if skilltree
            else "",
            own_chr="chr-" + card.id.split("-")[1],
            singer_weight=(
                singer_weight if is_leader else singer_weight_for(singers or frozenset(), chr_id, False)
            ),
            song_scope=song_scope_of(singers),
        )
        plan = optimize_board(
            tree,
            budget_for(card),
            values,
            player_level=player_level,
            prefer=(prefer or {}).get(chr_id, frozenset()),
        )
        if not is_leader and chr_id != _chr_of(leader):
            # the grouping cap across the team (`cached_team_plan`'s surplus pass)
            if _grouping_chain(plan.selected, chr_id, skilltree):
                values_by_chr[chr_id] = (card, tree, values)
        plans[card.id] = plan
        total += plan.value
    for card in sorted((values_by_chr[c][0] for c in values_by_chr), key=lambda c: (budget_for(c), c.id)):
        if not _chain_is_surplus(card, plans, members, leader, skilltree):
            continue
        _card, tree, values = values_by_chr[_chr_of(card)]
        values = {n: (NO_GROUPING_VALUE if n in GROUPING_GREEN_NODES else v) for n, v in values.items()}
        plan = optimize_board(
            tree,
            budget_for(card),
            values,
            player_level=player_level,
            prefer=(prefer or {}).get(_chr_of(card), frozenset()),
        )
        total += plan.value - plans[card.id].value
        plans[card.id] = plan
    return TeamBoardPlan(total_gain=total, per_character=plans)


def _chr_of(card: Card) -> str:
    return "chr-" + card.id.split("-")[1]


def singer_weight_for(sings: bool | AbstractSet[str], chr_id: str, is_leader: bool) -> float:
    """The singer context of one board: `sings` is either the LEADER's flag (a bool: only
    the leader's board is valued as a singer) or the song's whole cast as a `frozenset` of chr
    ids, which values EVERY board whose holomem sings -- a member singer's solo/group yellows
    and her yellow socket included (valued 0, the Connect matching would leave a singing
    member's yellow socket empty)."""
    if isinstance(sings, (set, frozenset)):
        return 1.0 if chr_id in sings else 0.0
    return 1.0 if (is_leader and sings) else 0.0


def song_scope_of(sings: bool | AbstractSet[str] | None) -> str:
    """The song's scope a `sings` context carries (`SongCast.scope`), "" when it
    carries none (a leader flag, a plain set, no cast)."""
    return getattr(sings, "scope", "") or ""


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


def _grouping_supply(tgt: str, plans: Mapping, members, leader: Card, skilltree: Mapping | None) -> float:
    """What grouping `tgt` receives from the team's planned boards plus the off-team
    boards (`GROUPING_ROOM_KEY`'s source: the leaf's bench, `grouping_supply`)."""
    cards = {c.id: c for c in (leader, *members)}
    team_chrs = {_chr_of(c) for c in cards.values()}
    total = 0.0
    for card_id, plan in plans.items():
        total += _grouping_chain(plan.selected, _chr_of(cards[card_id]), skilltree).get(tgt, 0.0)
    bench = ((skilltree or {}).get(GROUPING_SUPPLY_KEY) or {}).get(tgt) or {}
    return total + sum(v for c, v in bench.items() if c not in team_chrs)


def _chain_is_surplus(card: Card, plans: Mapping, members, leader: Card, skilltree: Mapping | None) -> bool:
    """Every grouping this card's board feeds would still reach the cap without it."""
    chain = _grouping_chain(plans[card.id].selected, _chr_of(card), skilltree)
    if not chain:
        return False
    cap = _grouping_cap()
    return all(
        _grouping_supply(tgt, plans, members, leader, skilltree) - amount >= cap
        for tgt, amount in chain.items()
    )


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


def bench_yellow_pays(scope: str | None, chr_id: str, cast: AbstractSet[str] | None) -> bool:
    """Whether an OFF-TEAM board's yellow of `scope` reaches this song's score
    (`BoardModel.song_score_bonus_for`): the song's own cast pays the yellows of the
    song's scope whoever is on the team; a song with no cast (a whole-roster song, or
    the song-agnostic score) pays every board's `all` yellows. An empty `SongCast` that
    names a solo or group scope is not such a song: it is a song whose singers it leaves
    out (several charts keyed by the team's own singers, none of whom sings this one),
    which pays its own scope only, so no bench yellow is counted. A plain set says who
    sings but not the scope, so it accepts either solo or group (a `SongCast` says
    which). `cast` None: not known (a leader flag was passed instead), so no bench
    yellow is counted."""
    if scope is None or cast is None:
        return False
    if not cast:
        return scope == "all" and getattr(cast, "scope", "") in ("", "all")
    if chr_id not in cast:
        return False
    song_scope = getattr(cast, "scope", "")
    return scope == song_scope if song_scope else scope in ("solo", "group")
