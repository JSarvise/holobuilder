"""Bridge from the score model to the board-plan engine: assemble the real
`SkillTree`, and value each node in Unit-Score terms for one character in one
team, so `board_optimizer.optimize_board` can choose the board to develop.

Both halves are pure (callers pass the decoded dumps, so `src/` holds no dump paths).
They live in `board_node_values` and are re-exported here:

* `skill_tree_from_dumps` joins `skilltree.json`'s node table, one model's positions
  and the point costs from master `SkillTreeNode` field 9 (they sum to 453).
* `power_node_values` values every node as a linearisation around the team's
  board-off score, over four channels: stat/power (`dUS/dPower x Delta power`; R
  reaches all members while leading, B its own card, G every card), the additive
  `song_score_bonus` rate, and the ACTIVE pool (`skill_effect_up` scales the team's
  bare pool, `activation_rate` / `cooldown` one member's ~1/5 share of it).
  Character-grouping greens take skilltree's exact per-character `chr` override
  (value and grouping), reaching the team members in that grouping.

The plan cache (`PlanCache`, `cached_team_plan`) fixes one plan per (character, role,
budget, singer context); `board_model_from_plan` assembles plans into a `BoardModel`
and solves the team-wide Connect matching, so every reported score is a re-score of
the real board, never the linearised sum. Re-ranking and the activation-node refinement
built on these plans are in `board_refine`.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from hbuilder.domain.board import (
    SONG_ALL_CAP_PERMIL,
    BoardBase,
    BoardDigest,
    BoardModel,
    BoardNode,
    CharacterBoard,
    board_from_mapping,
    parse_effect_id,
)

# Node values and per-team plans live in `board_node_values`; re-exported so `board_plan.X`
# keeps working for every importer.
from hbuilder.domain.board_node_values import GROUPING_GREEN_NODES as GROUPING_GREEN_NODES
from hbuilder.domain.board_node_values import GROUPING_ROOM_KEY as GROUPING_ROOM_KEY
from hbuilder.domain.board_node_values import GROUPING_SUPPLY_KEY as GROUPING_SUPPLY_KEY
from hbuilder.domain.board_node_values import NO_GROUPING as NO_GROUPING
from hbuilder.domain.board_node_values import NO_GROUPING_VALUE as NO_GROUPING_VALUE
from hbuilder.domain.board_node_values import NodeRecipe as NodeRecipe
from hbuilder.domain.board_node_values import TeamBoardPlan as TeamBoardPlan
from hbuilder.domain.board_node_values import _apply_grouping_room as _apply_grouping_room
from hbuilder.domain.board_node_values import _chain_is_surplus as _chain_is_surplus
from hbuilder.domain.board_node_values import _chr_of as _chr_of
from hbuilder.domain.board_node_values import _connect_multipliers as _connect_multipliers
from hbuilder.domain.board_node_values import _grouping_cap as _grouping_cap
from hbuilder.domain.board_node_values import _grouping_chain as _grouping_chain
from hbuilder.domain.board_node_values import _grouping_node_value as _grouping_node_value
from hbuilder.domain.board_node_values import _grouping_supply as _grouping_supply
from hbuilder.domain.board_node_values import bench_node_values as bench_node_values
from hbuilder.domain.board_node_values import bench_yellow_pays as bench_yellow_pays
from hbuilder.domain.board_node_values import compile_node_recipes as compile_node_recipes
from hbuilder.domain.board_node_values import grouping_room as grouping_room
from hbuilder.domain.board_node_values import grouping_supply as grouping_supply
from hbuilder.domain.board_node_values import model_for_character as model_for_character
from hbuilder.domain.board_node_values import node_costs_from_master as node_costs_from_master
from hbuilder.domain.board_node_values import node_effect_ids_from_master as node_effect_ids_from_master
from hbuilder.domain.board_node_values import node_values_from_recipes as node_values_from_recipes
from hbuilder.domain.board_node_values import optimize_team_boards as optimize_team_boards
from hbuilder.domain.board_node_values import power_node_values as power_node_values
from hbuilder.domain.board_node_values import singer_weight_for as singer_weight_for
from hbuilder.domain.board_node_values import skill_tree_from_dumps as skill_tree_from_dumps
from hbuilder.domain.board_node_values import song_scope_of as song_scope_of
from hbuilder.domain.board_node_values import yellow_scope as yellow_scope
from hbuilder.domain.board_optimizer import BoardPlan, optimize_board
from hbuilder.domain.card import Card
from hbuilder.domain.scoring import (
    UNIT_SCORE_K,
    deck_screen_unit_score,
    score_with_breakdown,
)
from hbuilder.domain.scoring_config import ScoringConfig
from hbuilder.domain.skilltree import ROOT_NODE, SkillTree
from hbuilder.domain.team import Team

if TYPE_CHECKING:
    from hbuilder.domain.song import SongChart


# ---------------------------------------------------------------------------
# The plan cache: one board plan per (character, role, budget, singer context),
# role in {leader, leader+member, member, bench}, singer context = which scope of
# the song being solved the character sings, if any. A plan is computed ONCE at a
# NOMINAL team context and reused for every team with that key; each team's exact
# number comes from RE-SCORING the assembled board (`board_model_from_plan`), so
# the per-song objective is a pure function of the roster.
# ---------------------------------------------------------------------------

PLAN_ROLE_LEADER = "leader"  # leads, no card of hers among the members
PLAN_ROLE_LEADER_MEMBER = "leader+member"  # leads AND fields a member card
PLAN_ROLE_MEMBER = "member"
PLAN_ROLE_BENCH = "bench"
PLAN_ROLES = (PLAN_ROLE_LEADER, PLAN_ROLE_LEADER_MEMBER, PLAN_ROLE_MEMBER, PLAN_ROLE_BENCH)


def sings_key(sings: bool | AbstractSet[str], chr_id: str, is_leader: bool) -> bool | str:
    """One board's singer context as a plan-cache key part: False when it does not
    sing, else the song's scope (`song_scope_of`) -- a plan for a solo song values
    the solo yellows and one for a group song the group ones, so they are cached
    apart -- or True when the scope is not known."""
    if singer_weight_for(sings, chr_id, is_leader) <= 0:
        return False
    return song_scope_of(sings) or True


def plan_key(card: Card, role: str, budget: int, sings: bool | str) -> tuple:
    """The cache key of one board plan: `(chr, role, budget, sings, own_card)`.

    `card` is the card whose stats the plan's BLUE nodes use: the member card for
    `member` / `leader+member`, the leader card for `leader`, any card of the
    character for `bench`. `own_card` is in the key only for the roles whose blues
    reach a card (a 4-star and a 5-star of one holomem value their blues
    differently); for `leader` / `bench` it is `""`, so every card of the character
    shares the plan. `sings` is this board's own singer context (`sings_key`)."""
    if role not in PLAN_ROLES:
        raise ValueError(f"unknown plan role {role!r}; expected one of {PLAN_ROLES}")
    uses_blues = role in (PLAN_ROLE_MEMBER, PLAN_ROLE_LEADER_MEMBER)
    leads = role in (PLAN_ROLE_LEADER, PLAN_ROLE_LEADER_MEMBER)
    context = sings if isinstance(sings, str) and sings else bool(sings)
    # a bench plan keeps its singer context too: an off-team singer's yellows pay on
    # her song (`singer_bench_characters`)
    sung = leads or uses_blues or role == PLAN_ROLE_BENCH
    return (_chr_of(card), role, int(budget), context if sung else False, card.id if uses_blues else "")


# `PlanCache._team_boards` is emptied when it reaches this many developed
# boards. A bound on memory only — an entry is a pure function of its key, so
# dropping one costs a rebuild and can never change a score.
_TEAM_BOARDS_MAX = 20_000
# `PlanCache.assembled` holds at most this many plans: the activation refinement assembles
# one per board it tries (`selected_plan_key`), so a long-lived cache would grow without it.
# Emptying it only costs re-assembling (a plan key names its assembly's content).
_ASSEMBLED_MAX = 50_000
# The first item of `selected_plan_key`'s keys (a `plan_key` starts with a chr id)
SELECTED_PLAN = "selected-nodes"
# How many bench mappings the caches keyed by one are held for at once (`PlanCache`'s parsed
# benches, `_SINGER_BENCH`, `_bench_bought`, `held_bench_cards`), the least recently used
# dropped first. With every board planned a search asks for one bench per singer context
# (`singer_bench_characters`, about 50 over one In general search) and comes back to each
# team after team; a one-entry cache would rebuild the bench (~54 parsed boards, their
# digests and per-card memos) on about half of the teams.
# A bound on memory only: an entry is a pure function of the objects it is keyed by.
_BENCHES_HELD = 128
# `PlanCache._bench_rows` holds at most this many parsed boards (one per rows list)
_BENCH_ROWS_MAX = 4096


def _held(cache: dict, key):
    """`cache[key]` moved to the most recently used end, or None."""
    hit = cache.pop(key, None)
    if hit is not None:
        cache[key] = hit
    return hit


def _hold(cache: dict, key, entry):
    """`entry` stored as the most recently used, the least recently used dropped beyond
    `_BENCHES_HELD`; returns `entry`."""
    cache.pop(key, None)
    while len(cache) >= _BENCHES_HELD:
        del cache[next(iter(cache))]
    cache[key] = entry
    return entry


def plan_node_order(selected: Iterable[str]) -> list[str]:
    """A plan's nodes in the order its board lists them: by node id, the order of the
    account's own boards as the game reports them. A board's stat rates, uptime and song
    bonus are float sums over its nodes in list order, and a set's order differs from one
    process to the next: assembled in set order, a board would sum to a different last
    bit per process, a point or two of Overall Power after the ceiling."""
    return sorted(selected)


def selected_plan_key(chr_id: str, selected: Iterable[str]) -> tuple:
    """The plan cache key of a board plan the cache did not make (one the activation
    refinement changed): the board and its nodes as the set iterates them, so equal
    keys are equal node sets and equal assemblies."""
    return (SELECTED_PLAN, chr_id, tuple(selected))


_NO_MULTS: frozenset = frozenset()


@dataclass(slots=True)
class PlanCache:
    """Per-(character, role, budget, singer context) board plans, valued at ONE
    nominal team context and reused across teams.

    The nominal context is the linearisation point of `power_node_values`, fixed at
    construction: `nominal_members` (five typical member cards the reds and greens
    reach; for a member or leader+member plan the character's own card replaces one
    of them), `overall_power`, `bonus_permil` and `active_pool_rate` (the bare ACTIVE
    pool). `from_team` derives them from one reference team's board-off breakdown.
    `hits` / `misses` count lookups."""

    nominal_members: tuple[Card, ...]
    overall_power: float
    bonus_permil: float
    active_pool_rate: float = 0.0
    k_constant: float = UNIT_SCORE_K
    player_level: int | None = None
    # `{chr: nodes the account has}` for a plan made from scratch that should keep them
    # where it costs nothing (`optimize_board`'s `prefer`); empty for a search's cache
    prefer: Mapping[str, frozenset[str]] | None = None
    plans: dict = None  # type: ignore[assignment]
    hits: int = 0
    misses: int = 0
    # The ASSEMBLED, team-invariant pieces of each plan (`AssembledPlan`), keyed
    # like `plans`, filled by `assembled_for`; emptied when the (node_effect_ids,
    # skilltree, connect_cards) objects change identity (they are held, so an id
    # cannot be recycled).
    assembled: dict = None  # type: ignore[assignment]
    _assembled_src: tuple = ()
    _recipes: dict = None  # type: ignore[assignment]   # own_chr -> recipes
    _connect_groups: tuple = ()  # (shape offsets, permil, sorted card ids) per group
    # id(bench mapping) -> [the mapping, parsed {name: CharacterBoard}, {name: BoardDigest},
    # BoardBase over them or None until asked]: one entry per bench mapping OBJECT, the
    # mapping kept alive in it so its id cannot be recycled; `_BENCHES_HELD` of them
    _benches: dict = None  # type: ignore[assignment]
    # (holomem, id(rows)) -> (rows, CharacterBoard, BoardDigest): one board parsed once per
    # rows LIST, whichever mappings carry it. Two benches that differ in a few boards (a
    # song's singers planned, `singer_bench_characters`) share every other list, so they
    # share those boards, their digests and the per-card memos the digests hold
    _bench_rows: dict = None  # type: ignore[assignment]
    # (plan key, holomem, per-node connect multipliers) -> (CharacterBoard,
    # BoardDigest): a developed board as `_board_model_from_cache` assembles
    # it. Emptied with `assembled`, whose nodes it is built from.
    _team_boards: dict = None  # type: ignore[assignment]
    # plan key -> that character's recipes cut down to `AssembledPlan.needed`.
    # Emptied with `assembled` and `_recipes`, which it is a function of.
    _needed_recipes: dict = None  # type: ignore[assignment]
    # A store SHARED with other caches of the same nominal context (`get(key)` -> plan or
    # None, `put(key, plan)`), injected by the caller: a plan is a pure function of its key
    # and of that context, so a plan another process already solved is the one this cache
    # would solve. A search's worker processes each hold a cache; without a store each one
    # solves the same plans again (16 workers: a third of a first search's work).
    store: object = None
    store_hits: int = 0

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
        if self._benches is None:
            self._benches = {}
        if self._bench_rows is None:
            self._bench_rows = {}

    @classmethod
    def from_team(cls, team: Team, cfg: ScoringConfig, *, player_level: int | None = None) -> PlanCache:
        """A cache whose nominal context is `team`'s board-off standing: its
        five members as the reached roster, its Overall Power / Score Bonus as
        the linearisation point, its bare ACTIVE pool for the pool channels."""
        bd = score_with_breakdown(team, replace(cfg, board=None))
        ds = deck_screen_unit_score(bd)
        return cls(
            nominal_members=tuple(team.members),
            overall_power=ds.overall_power,
            bonus_permil=ds.score_bonus_permil,
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
            return (card, *others[: max(0, len(self.nominal_members) - 1)])
        return others

    def plan_for(
        self,
        card: Card,
        role: str,
        budget: int,
        sings: bool | str,
        *,
        tree: SkillTree,
        node_effect_ids: Mapping[str, list[str]],
        skilltree: Mapping | None,
        no_grouping: bool = False,
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
        if self.store is not None:
            plan = self.store.get(key)  # type: ignore[attr-defined]
            if plan is not None:
                self.store_hits += 1
                self.plans[key] = plan
                return plan
        chr_id = _chr_of(card)
        model_id = (model_for_character(skilltree, chr_id) or "") if skilltree else ""
        reached = self._reached_for(card, role)
        if role == PLAN_ROLE_BENCH:
            # sings: her song's scope (or True, not known) -- her yellows of it pay
            cast = (
                None
                if not key[3]
                else (SongCast({chr_id}, key[3]) if isinstance(key[3], str) else frozenset({chr_id}))
            )
            values = bench_node_values(
                node_effect_ids,
                member_cards=reached,
                bonus_permil=self.bonus_permil,
                overall_power=self.overall_power,
                k_constant=self.k_constant,
                skilltree=skilltree,
                model_id=model_id,
                own_chr=chr_id,
                cast=cast,
            )
        else:
            values = power_node_values(
                node_effect_ids,
                is_leader=role in (PLAN_ROLE_LEADER, PLAN_ROLE_LEADER_MEMBER),
                own_card=card,
                member_cards=reached,
                bonus_permil=self.bonus_permil,
                overall_power=self.overall_power,
                k_constant=self.k_constant,
                active_pool_rate=self.active_pool_rate,
                skilltree=skilltree,
                model_id=model_id,
                own_chr=chr_id,
                singer_weight=1.0 if key[3] else 0.0,
                song_scope=key[3] if isinstance(key[3], str) else "",
            )
        if no_grouping:
            values = {n: (NO_GROUPING_VALUE if n in GROUPING_GREEN_NODES else v) for n, v in values.items()}
        plan = optimize_board(
            tree,
            budget,
            values,
            player_level=self.player_level,
            prefer=(self.prefer or {}).get(chr_id, frozenset()),
        )
        self.plans[key] = plan
        if self.store is not None:
            self.store.put(key, plan)  # type: ignore[attr-defined]
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
            (offsets, permil, tuple(sorted(ids))) for (offsets, permil), ids in groups.items()
        )

    def recipes_for(
        self, chr_id: str, node_effect_ids: Mapping[str, list[str]], skilltree: Mapping | None
    ) -> tuple:
        """`compile_node_recipes` for one character, compiled once."""
        r = self._recipes.get(chr_id)
        if r is None:
            r = compile_node_recipes(node_effect_ids, skilltree["nodes"] if skilltree else None, chr_id)
            self._recipes[chr_id] = r
        return r

    def needed_recipes(
        self,
        key: tuple,
        asm: AssembledPlan,
        chr_id: str,
        node_effect_ids: Mapping[str, list[str]],
        skilltree: Mapping | None,
    ) -> tuple:
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

    def _bench_entry(self, bench_characters) -> list:
        """`[mapping, boards, digests, base or None]` for one bench mapping object, parsed
        on first sight: each board from its rows list as `board_from_mapping` parses it,
        once per LIST (`_bench_rows`), in the mapping's order."""
        key = id(bench_characters)
        entry = _held(self._benches, key)
        if entry is not None and entry[0] is bench_characters:
            return entry
        rows_held = self._bench_rows
        boards: dict[str, CharacterBoard] = {}
        digests: dict[str, BoardDigest] = {}
        for name, rows in bench_characters.items():
            hit = rows_held.get((name, id(rows)))
            if hit is None or hit[0] is not rows:
                parsed = board_from_mapping({"characters": {name: list(rows)}})
                board = parsed.characters[name]
                # the digest the parse's own model built for this very board
                digest = parsed._digests.get(name)
                if digest is None or digest.board is not board:
                    digest = BoardDigest(board)
                while len(rows_held) >= _BENCH_ROWS_MAX:
                    del rows_held[next(iter(rows_held))]
                hit = rows_held[(name, id(rows))] = (rows, board, digest)
            boards[name], digests[name] = hit[1], hit[2]
        return _hold(self._benches, key, [bench_characters, boards, digests, None])

    def bench_boards(
        self, bench_characters: Mapping[str, Sequence[Mapping]] | None
    ) -> dict[str, CharacterBoard]:
        """The account's bench boards parsed ONCE per mapping object — the same
        `board_from_mapping` parse, its `CharacterBoard`s reused (frozen)."""
        if bench_characters is None:
            return {}
        return self._bench_entry(bench_characters)[1]

    def bench_digests(
        self, bench_characters: Mapping[str, Sequence[Mapping]] | None
    ) -> dict[str, BoardDigest]:
        """`bench_boards`' `BoardDigest`s, built ONCE per parse and handed to
        every `BoardModel` assembled over them — the bench is ~50 of a model's
        ~54 boards and identical for every team."""
        if bench_characters is None:
            return {}
        return self._bench_entry(bench_characters)[2]

    def bench_base(self, bench_characters: Mapping[str, Sequence[Mapping]] | None) -> BoardBase | None:
        """The bench as a `BoardBase` — its per-card stat terms summed once
        and corrected per team (`BoardModel._terms_over_base`). `None` with no
        bench."""
        if bench_characters is None:
            return None
        entry = self._bench_entry(bench_characters)
        if entry[3] is None:
            entry[3] = BoardBase(entry[2])
        return entry[3]

    def team_board(
        self, key: tuple, holomem: str, asm: AssembledPlan, cmult: Mapping[str, float]
    ) -> tuple[CharacterBoard, BoardDigest]:
        """One developed board (plan `key`'s nodes, `asm`, under the Connect multipliers
        the team-wide matching gave them) and its digest, reused whenever another team
        lands the same plan on the same multipliers. Keyed by the character's whole
        `cmult` (a handful of nodes): equal keys are equal boards, and two `cmult`s that
        differ only outside this plan's nodes merely build the same board twice."""
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
                BoardNode(node_id=node_id, effects=(eff,), connect_multiplier=mult)  # type: ignore[arg-type]
                for (node_id, eff), mult in zip(asm.nodes, mults)
            ),
        )
        hit = (board, BoardDigest(board))
        self._team_boards[cache_key] = hit
        return hit

    def assembled_for(
        self,
        key: tuple,
        cplan: BoardPlan,
        chr_id: str,
        *,
        skilltree: Mapping,
        node_effect_ids: Mapping[str, list[str]],
        connect_cards: Mapping[str, tuple[Sequence[Sequence[int]], int]] | None,
    ) -> AssembledPlan:
        """The team-invariant half of `board_model_from_plan` for one plan:
        its node list with parsed effects (in node id order, `plan_node_order`), and
        the socket entries the team-wide Connect assignment scores — computed once
        per plan key and reused for every team."""
        self._sync_sources(node_effect_ids, skilltree, connect_cards)
        asm = self.assembled.get(key)
        if asm is not None:
            return asm
        nodes: list[tuple[str, object]] = []
        for node_id in plan_node_order(cplan.selected):
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
            nodes=tuple(nodes),
            model_id=model_id,
            entries=tuple(entries),
            needed=tuple(sorted(needed)) if connect_cards is not None else None,
        )
        if len(self.assembled) >= _ASSEMBLED_MAX:
            self.assembled.clear()
            self._needed_recipes.clear()
        self.assembled[key] = asm
        return asm


@dataclass(frozen=True, slots=True)
class AssembledPlan:
    """One plan's team-invariant assembly (`PlanCache.assembled_for`).

    `nodes`: `(node_id, BoardEffect)` for every bought node with an effect, in node
    id order (`plan_node_order`, as `board_model_from_plan` emits them). `entries`:
    `(socket, permil, covered nodes, card ids)` per (eligible socket, distinct
    connect shape), the covered bought nodes in offset order and the account's cards
    of that exact (shape, permil), id-sorted. `needed`: the union of covered nodes,
    the only nodes whose base value the Connect matching reads (`None` = all, the
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
    sings: bool | AbstractSet[str] = False,
) -> TeamBoardPlan:
    """`optimize_team_boards`'s twin on the plan cache: one plan per distinct
    CHARACTER, the leader's at role `leader` (or `leader+member` when a card of
    hers is among the members, valued on that card), each other member's at role
    `member`; `sings` is the singer context (`singer_weight_for`). Same
    `per_character` shape, leader first. `total_gain` is the NOMINAL linearised
    sum, a bookkeeping figure; the team's number is the re-score."""
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
        plan = cache.plan_for(
            card,
            role,
            budget_for(leader),
            lead_sings,
            tree=tree,
            node_effect_ids=node_effect_ids,
            skilltree=skilltree,
        )
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
        plan = cache.plan_for(
            m,
            PLAN_ROLE_MEMBER,
            budget_for(m),
            m_sings,
            tree=tree,
            node_effect_ids=node_effect_ids,
            skilltree=skilltree,
        )
        plans[m.id] = plan
        keys[m.id] = plan_key(m, PLAN_ROLE_MEMBER, budget_for(m), m_sings)
        total += plan.value
    # THE GROUPING CAP ACROSS THE TEAM: the plans above are per character, so three
    # members of one generation can each buy the whole G-008/011/021 chain (1,350; the
    # game counts 900). A member whose chain lies wholly in the surplus adds nothing
    # with it and is re-planned without it (a cached variant), the fewest-points board
    # first, where the freed points are dearest.
    for m in sorted(
        (m for m in members if m.id in plans and _chr_of(m) != lead_chr), key=lambda m: (budget_for(m), m.id)
    ):
        if not _chain_is_surplus(m, plans, members, leader, skilltree):
            continue
        tree = tree_for(m)
        m_sings = sings_key(sings, _chr_of(m), False)
        plan = cache.plan_for(
            m,
            PLAN_ROLE_MEMBER,
            budget_for(m),
            m_sings,
            tree=tree,  # type: ignore[arg-type]
            node_effect_ids=node_effect_ids,
            skilltree=skilltree,
            no_grouping=True,
        )
        total += plan.value - plans[m.id].value
        plans[m.id] = plan
        keys[m.id] = plan_key(m, PLAN_ROLE_MEMBER, budget_for(m), m_sings) + (NO_GROUPING,)
    return TeamBoardPlan(total_gain=total, per_character=plans, plan_keys=keys)


_SINGER_BENCH: dict = {}


def singer_bench_characters(
    bench_characters: Mapping[str, list] | None,
    cast: AbstractSet[str] | None,
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
    re-plans every board, so the leaf must too: with her board as captured (no yellow
    bought) every team without her ranks ~5 % too low on her solo song. A singer on the
    team plans her own board (team boards take the name), so this is the same mapping
    for every team of the song. A kept node keeps its captured Connect multiplier; a
    new node starts at 1.0."""
    if not bench_characters or not cast:
        return bench_characters
    scope = song_scope_of(cast)
    key = (id(bench_characters), id(cache), tuple(sorted(cast)), scope)
    hit = _held(_SINGER_BENCH, key)
    if hit is not None and hit[0] is bench_characters and hit[1] is cache:
        return hit[2]
    out = dict(bench_characters)
    for chr_id in sorted(cast):
        card, name = cards_by_chr.get(chr_id), name_by_chr.get(chr_id)
        tree = tree_for(card) if card is not None else None
        if tree is None or not name:
            continue
        plan = cache.plan_for(
            card,  # type: ignore[arg-type]
            PLAN_ROLE_BENCH,
            budget_for(card),  # type: ignore[arg-type]
            scope or True,
            tree=tree,
            node_effect_ids=node_effect_ids,
            skilltree=skilltree,
        )
        kept = {
            e.get("node"): float(e.get("connect_multiplier") or 1.0)
            for e in (bench_characters.get(name) or ())
        }
        rows = []
        for node in sorted(plan.selected):
            effect = _effect_string_for(skilltree, node, chr_id, node_effect_ids)
            if effect:
                rows.append({"node": node, "effects": [effect], "connect_multiplier": kept.get(node, 1.0)})
        out[name] = rows
    _hold(_SINGER_BENCH, key, (bench_characters, cache, out))
    return out


def forget_plan_cache(cache: PlanCache) -> None:
    """Drop the singer benches made on `cache` (each `_SINGER_BENCH` entry holds its cache):
    for a caller that discards a plan cache, so the memo does not keep it alive."""
    for key in [k for k, hit in _SINGER_BENCH.items() if hit[1] is cache]:
        del _SINGER_BENCH[key]


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
        out[pid[len(prefix) :]] = int(q) if isinstance(q, int) else 0
    return out


def _effect_string_for(
    skilltree: Mapping, node_id: str, chr_id: str, node_effect_ids: Mapping[str, list[str]]
) -> str | None:
    """The effect id string for one node on one character's board: the chr
    override for a grouping green, else the node's single master effect."""
    override = skilltree["nodes"].get(node_id, {}).get("chr", {}).get(chr_id)
    if override and "tgt" in override and "v" in override:
        return (
            f"skill_tree_effect-all_parameter_up_for_character_grouping-"
            f"{int(override['v'])}-skill_tree_effect_target-{override['tgt']}"
        )
    effs = node_effect_ids.get(node_id, [])
    return effs[0] if effs else None


@dataclass(frozen=True, slots=True)
class SocketPlacement:
    """One real Connect placement the develop re-score decided on: this card,
    on this character's socket, at the multiplier its resolved level grants."""

    chr: str  # "chr-00031"
    socket: str  # "S-001".."S-004"
    card: str  # placed card id
    multiplier: float  # 1 + permil/1000 at the card's resolved level
    value: float  # linearised Unit-Score lift of the placement


# One candidate Connect placement the team-wide assignment chooses among:
# `(gain, chr_id, socket, permil, covered nodes, id-sorted cards carrying this
# exact (shape, permil))` — one per (eligible socket, distinct shape group),
# gain > 0. Both assignment entry points below reduce to this shape.
ConnectTriple = tuple[float, str, str, int, tuple[str, ...], tuple[str, ...]]


def _max_weight_matching(
    weights: Sequence[Sequence[float]],
    capacities: Sequence[int] | None = None,
) -> dict[int, int]:
    """EXACT maximum-weight bipartite matching: the Hungarian (Kuhn-Munkres)
    algorithm in its successive-shortest-path form with dual potentials, columns
    CAPACITATED so cards sharing one (shape, permil) need no duplication.

    `weights[i][j] >= 0` is the value of pairing row `i` with column `j`; every row
    is used at most once and column `j` at most `capacities[j]` times (default 1).
    Rows may stay unmatched (a zero-value dummy column of capacity n), so the result
    is the maximum over ALL matchings. Returns `{row: column}` for the rows matched
    at positive weight. Deterministic: on equal reduced cost the lowest column
    index wins. O(n (n + m) m) worst case, about 0.05 ms on a 16 x 20 team."""
    n = len(weights)
    if n == 0:
        return {}
    ncol = len(weights[0])
    caps = list(capacities) if capacities is not None else [1] * ncol
    m = ncol + 1  # + the dummy column (index ncol)
    caps.append(n)
    inf = float("inf")
    cost = [[-w for w in row] + [0.0] for row in weights]  # minimise -weight
    u = [0.0] * n  # row potentials
    v = [0.0] * m  # column potentials
    matched: list[list[int]] = [[] for _ in range(m)]  # rows on column j
    col_of_row = [-1] * n
    for r in range(n):
        # Dijkstra from row r over the columns, reduced costs c - u - v >= 0;
        # a column with spare capacity ends the path.
        minv = [inf] * m
        way_col = [-1] * m  # predecessor column on the path (-1 = row r itself)
        way_row = [-1] * m  # the row the path came through
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
        while True:  # augment along the found path
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
    additive per (socket, card): a card's multiplier reaches only the nodes of its
    own shape anchored at its own socket, and no two sockets of one board reach a
    common node (4 models x 4 sockets x 17 shapes: 0 overlaps in 6,936 cross-socket
    shape pairs). So this is a maximum-weight bipartite matching between
    (character, socket) rows and card columns; cards with the same (shape, permil)
    are one column with capacity = their count.

    Deterministic: rows are ordered `(chr, socket)`, columns by their id-sorted
    card tuple, and among the placements the concrete card is chosen in
    value-desc, then `(chr, socket)` order, taking the lowest unused id of the
    column (the same tie-break a highest-lift-first greedy uses).

    `incumbent` (`(chr, socket) -> card id`, the account's own placements): on a
    tie the account's card stays where it is (`INCUMBENT_TIE`), so a card lifting
    greens worth the same on two boards is not moved for nothing (with the bench in
    the matching, dozens of such moves would be shown). The placements' values stay
    the true gains."""
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
    return _placements_from_matched([at[(i, j)] for i, j in match.items()], incumbent=incumbent)


def _placements_from_matched(
    matched: Sequence[ConnectTriple],
    *,
    name_cards: bool = True,
    incumbent: Mapping[tuple[str, str], str] | None = None,
) -> tuple[dict[str, dict[str, float]], list[SocketPlacement]]:
    """The matched placements as `(multipliers per character and node, the
    placements)`: largest lift first, each placement taking the first card of its
    group not yet used. The naming half of `_connect_assignment_exact`, shared with
    the compiled matching (`CONNECT_KERNEL`).

    `name_cards=False` returns the multipliers with an EMPTY placement list, for a
    caller that reports no sockets; the multipliers do not depend on which card of a
    group took which socket, so they are the same either way."""
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
        placements.append(
            SocketPlacement(
                chr=chr_id, socket=socket, card=card_id, multiplier=1.0 + permil / 1000.0, value=gain
            )
        )
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

    The geometry (which selected nodes each (socket, shape group) covers) does not
    depend on the values, and `_yellow_capped_assignment` asks for it up to 7 times per
    team with only the values scaled, so it is kept per (connect cards, board positions,
    selected nodes) object (`_CONNECT_GEOMETRY`). The gains are the same sums in the
    same order either way."""
    held = _CONNECT_GEOMETRY.get(id(connect_cards))
    if held is None or held[0] is not connect_cards:
        groups: dict[tuple, list[str]] = {}
        for card_id, (offsets, permil) in connect_cards.items():
            groups.setdefault((tuple(tuple(o) for o in offsets), permil), []).append(card_id)
        shape_groups = [(offsets, permil, tuple(sorted(ids))) for (offsets, permil), ids in groups.items()]
        if len(_CONNECT_GEOMETRY) > 64:
            _CONNECT_GEOMETRY.clear()
        held = _CONNECT_GEOMETRY[id(connect_cards)] = (connect_cards, shape_groups, {})
    _cards, shape_groups, per_board = held
    triples: list[ConnectTriple] = []
    for chr_id, (positions, selected, values) in per_char.items():
        if not isinstance(selected, frozenset):
            selected = frozenset(selected)
        entry = per_board.get((id(positions), selected))  # type: ignore[call-overload]
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
            entry = per_board[(id(positions), selected)] = (positions, rows)  # type: ignore[index, assignment]
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


class SongCast(frozenset):
    """A song's cast (chr ids) that also carries the song's scope (`Chart.singer_type`:
    `solo` / `group` / `all`). It is a `frozenset`, so every `sings` caller reads it as
    the cast; `bench_yellow_pays` reads the scope. The scorer pays ONE scope per song
    (`BoardModel.song_score_bonus_for(singers, scope=singer_type)`), and the cast's size
    does not say which: a `solo` song may name two singers (`Chart` validation). Without
    the scope, the singers' SOLO yellows would fill the cap's room on a GROUP song and
    their group yellows would lose their sockets."""

    scope: str

    def __new__(cls, singers: Iterable[str] = (), scope: str = "") -> SongCast:
        self = super().__new__(cls, singers)
        self.scope = scope
        return self

    def __reduce__(self):
        return (SongCast, (tuple(self), self.scope))


def song_cast(chart: SongChart) -> SongCast:
    """The `SongCast` of one chart: its singers' chr ids at its `singer_type`."""
    return SongCast(chart.singers, getattr(chart, "singer_type", "") or "")


def bench_sockets_from_state(state: Mapping | None) -> dict[str, dict[str, str | None]]:
    """`{chr: {unlocked socket: card or None}}` from the account's `board_state.json`:
    the root (free on every board), every socket node it bought and every socket that
    holds a card. Every bench mode keeps the captured placements (the developed bench
    re-plans nodes, never sockets), so this is what the off-team sockets hold
    (`RefineContext.bench_sockets`, `held_bench_cards`)."""
    out = {}
    for chr_id, entry in (state or {}).items():
        slots = dict((entry or {}).get("slots") or {})
        unlocked = {
            "S-001",
            *slots,
            *(n for n in (entry or {}).get("nodes") or () if str(n).startswith("S-")),
        }
        out[chr_id] = {s: slots.get(s) for s in sorted(unlocked)}
    return out


_BENCH_BOUGHT: dict = {}


def _bench_bought(bench_characters, name_by_chr) -> dict[str, frozenset] | None:
    """`chr -> its bought nodes` from the name-keyed `bench_characters`, or None when
    the names cannot be mapped; remembered per pair of objects (the leaf asks per team,
    and `held_bench_cards` remembers its walk by the dict returned here)."""
    if not bench_characters or not name_by_chr:
        return None
    key = (id(bench_characters), id(name_by_chr))
    hit = _held(_BENCH_BOUGHT, key)
    if hit is None or hit[0] is not bench_characters or hit[1] is not name_by_chr:
        by_name = {name: chr_id for chr_id, name in name_by_chr.items()}
        hit = _hold(
            _BENCH_BOUGHT,
            key,
            (
                bench_characters,
                name_by_chr,
                {
                    by_name[name]: frozenset(e.get("node") for e in rows if e.get("node"))
                    for name, rows in bench_characters.items()
                    if name in by_name
                },
            ),
        )
    return hit[2]


def held_bench_cards(
    bench_sockets: Mapping[str, Mapping[str, str | None]] | None,
    team_chrs: AbstractSet[str],
    skilltree: Mapping,
    connect_cards: Mapping[str, tuple[Sequence[Sequence[int]], int]] | None,
    *,
    cast: AbstractSet[str] | None = None,
    bought: Mapping[str, frozenset] | None = None,
) -> frozenset[str]:
    """The cards an OFF-TEAM board's socket holds (`bench_sockets`, chr -> {socket:
    card}) where they lift a node of that board that reaches the team, already
    multiplied in `bench_characters`: a GREEN, or a YELLOW the song pays
    (`bench_yellow_pays` with `cast`, the song's singers: the scorer sums every board's
    song yellows, so an off-team singer's yellow socket counts on her song). The team's
    matching may not use such a card too, or the card counts twice. A card whose shape
    reaches neither lifts nothing this team sees, so it stays free for the team.
    `bought` (chr -> its bought nodes): only a bought node is multiplied; without it
    every node in the shape counts. The team's own boards are re-assigned, so their
    cards are free as well. The board walk is done once per input set."""
    if not bench_sockets or not connect_cards:
        return frozenset()
    key = (id(bench_sockets), id(connect_cards), id(bought))
    hit = _held(_GREEN_HOLDERS, key)
    if hit is None or hit[0] is not bench_sockets or hit[1] is not connect_cards or hit[2] is not bought:
        holders = []
        for chr_id, slots in bench_sockets.items():
            positions = (skilltree.get("models", {}) or {}).get(
                model_for_character(skilltree, chr_id) or "", {}
            )
            pos_to_node = {tuple(p): n for n, p in positions.items()}
            owned = None if bought is None else bought.get(chr_id, frozenset())
            for socket, card in (slots or {}).items():
                entry = connect_cards.get(card) if card else None
                spos = positions.get(socket)
                if entry is None or spos is None:
                    continue
                lifted = [
                    n
                    for dx, dy in entry[0]
                    if (n := str(pos_to_node.get((spos[0] + dx, spos[1] + dy), "")))
                    and (owned is None or n in owned)
                ]
                green = any(n.startswith("G-") for n in lifted)
                scopes = frozenset(s for n in lifted if (s := yellow_scope(skilltree, n, chr_id)))
                if green or scopes:
                    holders.append((chr_id, card, green, scopes))
        hit = _hold(_GREEN_HOLDERS, key, (bench_sockets, connect_cards, bought, tuple(holders)))
    return frozenset(
        card
        for chr_id, card, green, scopes in hit[3]
        if chr_id not in team_chrs and (green or any(bench_yellow_pays(s, chr_id, cast) for s in scopes))
    )


def _bench_socket_rows(
    chars,
    names,
    team_chrs,
    skilltree,
    node_effect_ids,
    members,
    *,
    overall_power,
    bonus_permil,
    k_constant,
    connect_cards,
    per_char,
    bench_sockets=None,
    cast=None,
):
    """Add every BENCH board's root socket (`S-001`, free on any board) to the Connect
    matching (`per_char`, in place) and return `{chr: (holomem, nodes an S-001 shape can
    reach)}` for the boards added. With the bench in the same matching each card is
    used once account-wide; left out, a card the team takes would also stay on its
    bench socket. A bench board is valued by what reaches the team from it
    (`bench_node_values`: its GREENs, and the yellows the song pays when `cast` is the
    song's singers). Without `bench_sockets` its other sockets are not known (a bench
    entry carries no socket nodes) and keep the account's card; with it
    (`chr -> {unlocked socket: card}`) EVERY unlocked socket of the board joins, so
    unlocked-but-empty bench sockets can be filled too, and the cover is the whole
    board (`None`)."""
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
            cover = frozenset(
                pos_to_node[(root[0] + dx, root[1] + dy)]
                for shape in shapes
                for dx, dy in shape
                if (root[0] + dx, root[1] + dy) in pos_to_node
            )
        selected = frozenset(e["node"] for e in entries if e.get("node")) | open_sockets
        values = bench_node_values(
            node_effect_ids,
            member_cards=members,
            bonus_permil=bonus_permil,
            overall_power=overall_power,
            k_constant=k_constant,
            skilltree={"nodes": skilltree["nodes"]},
            model_id="",
            own_chr=chr_id,
            cast=cast,
        )
        # only the open sockets: the matching skips a socket not in `selected` except S-001
        per_char[chr_id] = (
            {n: p for n, p in positions.items() if not n.startswith("S") or n in open_sockets},
            selected,
            values,
        )
        rows[chr_id] = (name, cover)
    return rows


def _yellow_permil(
    skilltree: Mapping, node_id: str, chr_id: str, cast, node_effect_ids: Mapping[str, list[str]]
) -> float:
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


def _yellow_permil_memo(
    skilltree: Mapping, node_id: str, chr_id: str, cast, node_effect_ids: Mapping[str, list[str]]
) -> float:
    """`_yellow_permil`, remembered per (inputs, node, board, cast): the leaf's fast
    path asks it for every bought node of every team."""
    key = (id(skilltree), id(node_effect_ids), node_id, chr_id, cast, getattr(cast, "scope", None))
    hit = _YELLOW_PERMIL_MEMO.get(key)
    if hit is None:
        if len(_YELLOW_PERMIL_MEMO) > 200_000:
            _YELLOW_PERMIL_MEMO.clear()
        hit = _YELLOW_PERMIL_MEMO[key] = _yellow_permil(skilltree, node_id, chr_id, cast, node_effect_ids)
    return hit


def _song_bonus_over_cap(
    team_rows, bench_characters, names: Mapping[str, str], *, cast, skilltree: Mapping, node_effect_ids
) -> bool:
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
        for node in plan_node_order(selected):
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
                total += _yellow_permil_memo(skilltree, e["node"], chr_id, cast, node_effect_ids) * float(
                    e.get("connect_multiplier") or 1.0
                )
    return total > SONG_ALL_CAP_PERMIL + 1e-9


# `_yellow_capped_assignment`'s matchings after its first one, at most (the bisection's rounds)
CAPPED_ROUNDS = 6


def _yellow_capped_assignment(
    per_char,
    connect_cards,
    *,
    cast,
    skilltree,
    node_effect_ids,
    fixed_rows=(),
    rounds: int = CAPPED_ROUNDS,
    incumbent=None,
):
    """`_team_connect_assignment` under the song bonus's per-song cap
    (`SONG_ALL_CAP_PERMIL`, `BoardModel.song_score_bonus_for`): the linear node values
    price every yellow permil at full worth, but past the cap a yellow adds nothing
    (when the bench singers' bought yellows already fill it, a linear matching sends
    cards to yellow sockets worth 0, off the greens).

    The room is the cap minus what the bought yellows already pay (`fixed_rows`, the
    boards outside the matching, with their multipliers; the matched boards' own
    yellows unmultiplied). The yellow values are scaled (bisection on the scale, at
    most `rounds` more matchings) and the matching kept is the one worth most with its
    yellow lift CAPPED at the room, not the last one that fits (which can drop a
    yellow card and leave part of the room unused)."""

    def paying(rows):
        return {n: p for n in rows if (p := _yellow_permil(skilltree, n, chr_id, cast, node_effect_ids))}

    room: float = SONG_ALL_CAP_PERMIL
    for chr_id, e in fixed_rows:
        if chr_id and e.get("node"):
            room -= _yellow_permil(skilltree, e["node"], chr_id, cast, node_effect_ids) * float(
                e.get("connect_multiplier") or 1.0
            )
    yellow = {}
    for chr_id, (_positions, selected, _values) in per_char.items():
        if rows := paying(selected):
            yellow[chr_id] = rows
            room -= sum(rows.values())
    result = _team_connect_assignment(per_char, connect_cards, incumbent)
    if not yellow:
        return result
    room = max(room, 0.0)

    def lift_of(res):
        return sum(
            (res[0].get(chr_id, {}).get(n, 1.0) - 1.0) * p
            for chr_id, rows in yellow.items()
            for n, p in rows.items()
        )

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
        scaled = {
            chr_id: (
                positions,
                selected,
                {n: (v * scale if n in yellow.get(chr_id, ()) else v) for n, v in values.items()},
            )
            for chr_id, (positions, selected, values) in per_char.items()
        }
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

    Re-assigning the sockets (what the develop does) would move the "as they are"
    Overall Power off the server's `GetDraftDeckInfo` (by +35..+3,450), because the
    matching gives boards Connect cards they do not hold."""
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
            placements.append(
                SocketPlacement(chr=chr_id, socket=socket, card=card_id, multiplier=multiplier, value=gain)
            )
    return mults, placements


# One character's input to the team-wide assignment on the plan cache:
# `(chr_id, its AssembledPlan, the recipes of the nodes the assignment reads,
# is_leader, the card, singer_weight)`.
ConnectInput = tuple[str, "AssembledPlan", tuple, bool, Card, float]

# The seam for a COMPILED assignment (`tools/web/kernel/connect.py`), injected
# the way `song.NOTE_WALK_KERNEL` is: this package imports nothing it does not
# ship. Called as `kernel(inputs, members, overall_power=, bonus_permil=,
# k_constant=, active_pool_rate=)`; it returns the MATCHED `ConnectTriple`s, or
# `None` to decline (then the Python below runs). It must never return an
# approximation.
CONNECT_KERNEL: Callable | None = None
# The same hook for WORKER PROCESSES (a spawned interpreter inherits the
# environment and nothing else): `<dir>/connect.py:assign`, loaded by path on
# first use. A kernel that fails to load is reported once and Python is used.
CONNECT_KERNEL_ENV = "HBUILDER_CONNECT_KERNEL"

# The seam for the COMPILED board assembly (`tools/web/kernel/connect.py`'s
# `assemble`), injected the same way: on the plan cache with the song's cast known,
# the team-wide Connect matching under the song bonus's cap exactly as
# `board_model_from_plan` runs it in Python. Called as `kernel(inputs, members,
# overall_power=, bonus_permil=, k_constant=, active_pool_rate=, song_scope=,
# yellows=, bench=, cap=, rounds=, incumbent=, tie_factor=, tie_first=, checks=)`;
# it returns the MATCHED `ConnectTriple`s, or `None` to decline. It must never
# return an approximation.
BOARD_KERNEL: Callable | None = None
BOARD_KERNEL_ENV = "HBUILDER_BOARD_KERNEL"

# env variable -> (its value, the kernel it names or None), and file -> its module: the
# seams that name one file share one module and its packing caches
_ENV_KERNELS: dict[str, tuple[str, Callable | None]] = {}
_KERNEL_MODULES: dict[str, object] = {}


def _env_kernel(env: str) -> Callable | None:
    """The kernel the environment variable `env` names (`<file>:<function>`), loaded by
    path on first use and resolved once per distinct value; reported once, and None,
    when it cannot be loaded."""
    spec = os.environ.get(env, "")
    hit = _ENV_KERNELS.get(env)
    if hit is not None and hit[0] == spec:
        return hit[1]
    kernel: Callable | None = None
    if spec:
        try:
            import importlib.util

            file_name, _, attribute = spec.rpartition(":")
            module = _KERNEL_MODULES.get(file_name)
            if module is None:
                module_spec = importlib.util.spec_from_file_location(
                    "_hbuilder_kernel_" + os.path.splitext(os.path.basename(file_name))[0], file_name
                )
                module = importlib.util.module_from_spec(module_spec)  # type: ignore[arg-type]
                module_spec.loader.exec_module(module)  # type: ignore[union-attr]
                _KERNEL_MODULES[file_name] = module
            kernel = getattr(module, attribute)
        except Exception as exc:  # noqa: BLE001 - any failure means "use Python"
            print(
                f"kernel {spec!r} could not be loaded ({exc!r}); this step continues on the Python path",
                file=sys.stderr,
            )
    _ENV_KERNELS[env] = (spec, kernel)
    return kernel


def _connect_kernel() -> Callable | None:
    """The injected Connect kernel: the module attribute if set, else the one the
    environment names."""
    return CONNECT_KERNEL if CONNECT_KERNEL is not None else _env_kernel(CONNECT_KERNEL_ENV)


def _board_kernel() -> Callable | None:
    """The injected board assembly kernel, found like `_connect_kernel`."""
    return BOARD_KERNEL if BOARD_KERNEL is not None else _env_kernel(BOARD_KERNEL_ENV)


_HELD_VIEWS: dict = {}


def _held_view(asm: AssembledPlan, held: frozenset) -> AssembledPlan:
    """`asm` with the `held` cards dropped from every entry's card group (an entry
    left with none is dropped), remembered per (plan, held set) so the kernel packs
    each view once."""
    key = (id(asm), held)
    hit = _HELD_VIEWS.get(key)
    if hit is None or hit[0] is not asm:
        entries = tuple(
            (socket, permil, covered, kept)
            for socket, permil, covered, card_ids in asm.entries
            if (kept := tuple(c for c in card_ids if c not in held))
        )
        if len(_HELD_VIEWS) > 50_000:
            _HELD_VIEWS.clear()
        hit = _HELD_VIEWS[key] = (asm, replace(asm, entries=entries))
    return hit[1]


def _cached_connect_assignment(
    inputs: Sequence[ConnectInput],
    members: tuple[Card, ...],
    *,
    overall_power: float,
    bonus_permil: float,
    k_constant: float,
    active_pool_rate: float,
    name_cards: bool = True,
    held: frozenset = frozenset(),
    song_scope: str = "",
    incumbent: Mapping[tuple[str, str], str] | None = None,
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
    would be thrown away whenever a group is used beyond the cards left (about
    78 % of the leaf's teams, which would then run the Python path too).

    `incumbent` (the account's own placements, `_connect_assignment_exact`) is read
    only when the cards are NAMED: the kernel has no tie-break, so a named call with
    incumbents runs the Python matching. The search's own calls name nothing and
    keep the kernel."""
    kernel = None if (incumbent and name_cards) else _connect_kernel()
    if kernel is not None:
        matched = kernel(
            [(i[0], _held_view(i[1], held), *i[2:]) if held else i for i in inputs if i[1].model_id],
            members,
            overall_power=overall_power,
            bonus_permil=bonus_permil,
            k_constant=k_constant,
            active_pool_rate=active_pool_rate,
            song_scope=song_scope,
        )
        if matched is not None:
            return _placements_from_matched(matched, name_cards=name_cards)
    per_char = []
    for chr_id, asm, recipes, is_leader, card, singer_weight in inputs:
        if not asm.model_id:
            continue
        base = node_values_from_recipes(
            recipes,
            is_leader=is_leader,
            own_card=card,
            member_cards=members,
            bonus_permil=bonus_permil,
            overall_power=overall_power,
            k_constant=k_constant,
            active_pool_rate=active_pool_rate,
            singer_weight=singer_weight,
            song_scope=song_scope,
        )
        entries = asm.entries
        if held:
            entries = [  # type: ignore[assignment]
                (socket, permil, covered, kept)
                for socket, permil, covered, card_ids in entries
                if (kept := tuple(c for c in card_ids if c not in held))
            ]
        per_char.append((chr_id, entries, base))
    return _team_connect_assignment_grouped(per_char, incumbent if name_cards else None)


def _team_connect_assignment_grouped(
    per_char: Sequence[
        tuple[str, Sequence[tuple[str, int, tuple[str, ...], tuple[str, ...]]], Mapping[str, float]]
    ],
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
    sings: bool | AbstractSet[str] = False,
    plan_cache: PlanCache | None = None,
    fixed_slots: Mapping[str, Mapping[str, str]] | None = None,
    socket_bench: bool = False,
    bench_sockets: Mapping[str, Mapping[str, str | None]] | None = None,
) -> BoardModel:
    """Turn a plan into a real `BoardModel` so it can be RE-SCORED exactly: the
    per-node valuation is a linearisation, and only scoring the assembled board
    captures the multiplicative compounding of power and bonus.

    Connect: with `connect_cards` (`card id -> (shape offsets, permil at the card's
    resolved level)`, the account's Connect cards) the multipliers come from a REAL
    team-wide assignment solved exactly (`_connect_assignment_exact`: one card, one
    socket, sockets beyond the free root only if bought), reported through
    `placements_out`. Without it every socket takes the best shape in the game at
    max bloom (an optimistic upper bound). Node selection values under that
    idealised model either way; only the re-score and the reported sockets change.

    `bench_characters`: the account's current boards for characters outside this
    team (`board.json`'s name-keyed `characters`, Connect multipliers resolved).
    Their GREENs are account-global, so every team's score carries them. A
    character this team develops takes the PLAN's board; its bench entry is
    replaced, never merged.

    `name_by_chr` (chr id -> holomem name) lets the per-song scorer match a chart's
    chr-id cast against this name-keyed board; the team's own cards are always
    mapped, and a caller with the whole roster widens it. Without it every
    singer-conditional term (R-044/062/063, R-002, the solo/group yellows) is zero.

    `sings` is the singer context the planner took (`singer_weight_for`): the socket
    placement re-values the nodes (`base` below), and without it a singing leader's
    conditional reds would be valued 0 and the matching would move her sockets off
    the branch that carries them. Default `False` is the song-agnostic valuation.

    `plan_cache`, with a plan whose `plan_keys` it filled (`cached_team_plan`),
    takes the FAST path: nodes, parsed effects and socket coverage come from
    `PlanCache.assembled_for`, base values only for the nodes the Connect matching
    reads, and the `BoardModel` is built directly. Same floats as the plain path
    (`tests/test_fast_leaf.py`).

    `fixed_slots` (`chr_id -> {socket: card id}`, with `connect_cards`): the team's
    sockets are taken AS THEY ARE instead of assigned (`_fixed_connect_placements`).
    It disables the plan-cache fast path, which only knows the assignment.

    `bench_sockets` (`chr_id -> {unlocked socket: card id or None}`, from
    `board_state.json`) says what the OFF-TEAM boards' sockets hold. A card on a
    bench socket keeps lifting that board (its multiplier is already in
    `bench_characters`), so the team's matching may not use it too. Without
    `socket_bench` those cards leave the team's pool (`held_bench_cards`); with it
    EVERY unlocked bench socket joins the matching (`_bench_socket_rows`), so empty
    bench sockets can be filled too. `socket_bench` disables the plan-cache fast
    path (its bench boards are shared across teams)."""
    members = tuple(members)
    if fixed_slots is not None or socket_bench:
        plan_cache = None
    names: dict[str, str] = {"chr-" + card.id.split("-")[1]: card.member for card in (*members, leader)}
    if name_by_chr:
        names = {**dict(name_by_chr), **names}
    team_chrs = {"chr-" + card.id.split("-")[1] for card in (*members, leader)}
    # the song's singers when known (a frozenset); a bare leader flag says nothing
    # about the bench, so no bench yellow is counted then
    cast = sings if isinstance(sings, (set, frozenset)) else None
    held = (
        frozenset()
        if socket_bench or fixed_slots is not None
        else held_bench_cards(
            bench_sockets,
            team_chrs,
            skilltree,
            connect_cards,
            cast=cast,
            bought=_bench_bought(bench_characters, name_by_chr),
        )
    )
    # the account's own placements: on a tie a card stays where it is
    incumbent = {
        (chr_id, socket): card
        for chr_id, slots in (bench_sockets or {}).items()
        for socket, card in (slots or {}).items()
        if card
    }
    if (
        plan_cache is not None
        and cast is not None
        and connect_cards is not None
        and bench_characters is not None
        and (kernel := _board_kernel()) is not None
    ):
        # the compiled assembly: the same model as the two paths below, or None to run them
        model = _board_model_by_kernel(
            kernel,
            plan,
            leader,
            members,
            skilltree,
            node_effect_ids,
            names,
            overall_power=overall_power,
            bonus_permil=bonus_permil,
            k_constant=k_constant,
            active_pool_rate=active_pool_rate,
            connect_cards=connect_cards,
            placements_out=placements_out,
            bench_characters=bench_characters,
            name_by_chr=name_by_chr,
            sings=sings,
            cache=plan_cache,
            cast=cast,
            held=held,
            incumbent=incumbent,
        )
        if model is not None:
            return model
    if (
        plan_cache is not None
        and plan.plan_keys
        and not (
            connect_cards is not None
            and cast is not None
            and _song_bonus_over_cap(
                _team_yellow_rows(plan, leader, members),
                bench_characters,
                names,
                cast=cast,
                skilltree=skilltree,
                node_effect_ids=node_effect_ids,
            )
        )
    ):
        # (over the cap with every multiplier at 1 -- Connect multipliers are >= 1, so the
        # fast path's own check after its matching would say so too: skip that matching)
        fast = _board_model_from_cache(
            plan,
            leader,
            members,
            skilltree,
            node_effect_ids,
            names,
            overall_power=overall_power,
            bonus_permil=bonus_permil,
            k_constant=k_constant,
            active_pool_rate=active_pool_rate,
            connect_cards=connect_cards,
            placements_out=placements_out,
            bench_characters=bench_characters,
            sings=sings,
            cache=plan_cache,
            held=held,
            incumbent=incumbent,
        )
        if fast is not None:
            return fast
        # the song bonus is over its cap: only the capped matching below is right
    connect_all = connect_cards  # the plan cache's assembly key (`assembled_for`)
    if held and connect_cards is not None:
        connect_cards = {k: v for k, v in connect_cards.items() if k not in held}
    chars: dict[str, list] = {name: list(nodes) for name, nodes in (bench_characters or {}).items()}
    seen: set[str] = set()
    # (card, chr_id, model_id, cplan, base) in build order; chars assembled after
    # the team-wide assignment is known. ONE entry per CHARACTER (one board per
    # holomem), leader first: a holomem who leads and also fields a member card is
    # valued in both roles at once, as `optimize_team_boards` plans her. Keyed per
    # card, the member entry would replace the leader's and her reds would read 0.
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
        own = member_by_chr.get(chr_id, card) if is_leader else card
        if plan_cache is not None:
            # `power_node_values` below with no grouping room and no Connect step: its
            # recipes, compiled once per character by the plan cache
            plan_cache._sync_sources(node_effect_ids, skilltree, connect_all)
            base = node_values_from_recipes(
                plan_cache.recipes_for(chr_id, node_effect_ids, skilltree),
                is_leader=is_leader,
                own_card=own,
                member_cards=members,
                bonus_permil=bonus_permil,
                overall_power=overall_power,
                k_constant=k_constant,
                active_pool_rate=active_pool_rate,
                singer_weight=singer_weight_for(sings, chr_id, is_leader),
                song_scope=song_scope_of(sings),
            )
        else:
            base = power_node_values(
                node_effect_ids,
                is_leader=is_leader,
                own_card=own,
                member_cards=members,
                bonus_permil=bonus_permil,
                overall_power=overall_power,
                k_constant=k_constant,
                active_pool_rate=active_pool_rate,
                skilltree={"nodes": skilltree["nodes"]},
                model_id="",
                own_chr=chr_id,
                singer_weight=singer_weight_for(sings, chr_id, is_leader),
                song_scope=song_scope_of(sings),
            )
        ctx.append((card, chr_id, model_id, cplan, base))
    if connect_cards is not None:
        per_char = {
            chr_id: (skilltree.get("models", {}).get(model_id, {}), cplan.selected, base)  # type: ignore[attr-defined]
            for _card, chr_id, model_id, cplan, base in ctx
            if model_id
        }
        bench_rows: dict[str, tuple[str, frozenset]] = {}
        if fixed_slots is None and socket_bench and bench_characters:
            bench_rows = _bench_socket_rows(
                chars,
                names,
                set(per_char),
                skilltree,
                node_effect_ids,
                members,
                overall_power=overall_power,
                bonus_permil=bonus_permil,
                k_constant=k_constant,
                connect_cards=connect_cards,
                per_char=per_char,
                bench_sockets=bench_sockets,
                cast=cast,
            )
        if fixed_slots is not None:
            mults_by_chr, placements = _fixed_connect_placements(per_char, connect_cards, fixed_slots)
        elif cast is not None:
            inverse = {name: chr_id for chr_id, name in names.items()}
            fixed = [
                (inverse.get(name), e)
                for name, rows in chars.items()
                if inverse.get(name) not in per_char
                for e in rows
            ]
            mults_by_chr, placements = _yellow_capped_assignment(
                per_char,
                connect_cards,
                cast=cast,
                skilltree=skilltree,
                node_effect_ids=node_effect_ids,
                fixed_rows=fixed,
                incumbent=incumbent,
            )
        else:
            mults_by_chr, placements = _team_connect_assignment(per_char, connect_cards, incumbent)
        # the bench boards' sockets re-assigned in the same matching: drop what the
        # account's cards did there (`cover`: the nodes the re-assigned root's shapes can
        # reach; None = every socket of the board was re-assigned) and apply the new ones
        for chr_id, (name, cover) in bench_rows.items():
            cmult = mults_by_chr.get(chr_id, {})
            chars[name] = [
                dict(
                    e,
                    connect_multiplier=cmult.get(e["node"], 1.0)
                    if cover is None or e.get("node") in cover
                    else e.get("connect_multiplier", 1.0),
                )
                for e in chars[name]
            ]
        if placements_out is not None:
            placements_out.extend(placements)
    else:
        mults_by_chr = {
            chr_id: (_connect_multipliers(skilltree, model_id, base) if model_id else {})
            for _card, chr_id, model_id, _cplan, base in ctx
        }
    # with the plan cache, a team board whose plan it holds is built from its assembly
    # (`team_board`, as on the fast path) instead of from these lists
    cached = (
        plan_cache is not None
        and bench_characters is not None
        and bool(plan.plan_keys)
        and all(card.id in plan.plan_keys for card, *_ in ctx)
    )
    for card, chr_id, _model_id, cplan, _base in ctx:  # type: ignore[assignment]
        if cached:
            break
        cmult = mults_by_chr.get(chr_id, {})
        nodes = []
        for node_id in plan_node_order(cplan.selected):  # type: ignore[union-attr]
            if node_id == ROOT_NODE:
                continue
            effstr = _effect_string_for(skilltree, node_id, chr_id, node_effect_ids)
            if effstr is None:
                continue
            nodes.append(
                {"node": node_id, "effects": [effstr], "connect_multiplier": cmult.get(node_id, 1.0)}
            )
        chars[card.member] = nodes
    if plan_cache is not None and bench_characters is not None:
        # the plain path with a plan cache at hand (the song bonus over its cap, or a
        # refined plan): the ~50 unchanged bench boards, their digests and `BoardBase`
        # come from the cache as on the fast path and only the team's boards are
        # parsed (the same `CharacterBoard`s and scores, ~5 s less per request)
        boards = dict(plan_cache.bench_boards(bench_characters))
        digests = dict(plan_cache.bench_digests(bench_characters))
        for card, chr_id, _model_id, cplan, _base in ctx:  # type: ignore[assignment]
            if cached:
                key = plan.plan_keys[card.id]
                asm = plan_cache.assembled_for(
                    key,
                    cplan,  # type: ignore[arg-type]
                    chr_id,
                    skilltree=skilltree,
                    node_effect_ids=node_effect_ids,
                    connect_cards=connect_all,
                )
                boards[card.member], digests[card.member] = plan_cache.team_board(
                    key, card.member, asm, mults_by_chr.get(chr_id, {})
                )
                continue
            boards[card.member] = board_from_mapping(
                {"characters": {card.member: chars[card.member]}}
            ).characters[card.member]
            digests.pop(card.member, None)
        return BoardModel(
            characters=boards,
            name_by_chr=dict(names),
            _digests=digests,
            _base=plan_cache.bench_base(bench_characters),
        )
    return board_from_mapping({"characters": chars, "name_by_chr": names})


def _team_yellow_rows(plan: TeamBoardPlan, leader: Card, members: Sequence[Card]) -> list:
    """`_song_bonus_over_cap`'s team rows with every Connect multiplier at 1: one row per
    character, leader first, as both paths of `board_model_from_plan` build them."""
    rows, seen = [], set()  # type: ignore[var-annotated]
    for card in (leader, *members):
        chr_id = "chr-" + card.id.split("-")[1]
        cplan = plan.per_character.get(card.id)
        if chr_id in seen or cplan is None:
            continue
        seen.add(chr_id)
        rows.append((chr_id, cplan.selected, {}))
    return rows


def _board_model_from_cache(
    plan: TeamBoardPlan,
    leader: Card,
    members: tuple[Card, ...],
    skilltree: Mapping,
    node_effect_ids: Mapping[str, list[str]],
    names: dict[str, str],
    *,
    overall_power: float,
    bonus_permil: float,
    k_constant: float,
    active_pool_rate: float,
    connect_cards,
    placements_out,
    bench_characters,
    sings: bool | AbstractSet[str],
    cache: PlanCache,
    held: frozenset = frozenset(),
    incumbent=None,
) -> BoardModel | None:
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
            plan.plan_keys[card.id],
            cplan,
            chr_id,
            skilltree=skilltree,
            node_effect_ids=node_effect_ids,
            connect_cards=connect_cards,
        )
        recipes = cache.needed_recipes(plan.plan_keys[card.id], asm, chr_id, node_effect_ids, skilltree)
        ctx.append((card, chr_id, asm))
        inputs.append(
            (
                chr_id,
                asm,
                recipes,
                is_leader,
                member_by_chr.get(chr_id, card) if is_leader else card,
                singer_weight_for(sings, chr_id, is_leader),
            )
        )
    if connect_cards is not None:
        mults_by_chr, placements = _cached_connect_assignment(
            inputs,
            members,
            overall_power=overall_power,
            bonus_permil=bonus_permil,
            k_constant=k_constant,
            active_pool_rate=active_pool_rate,
            name_cards=placements_out is not None,
            held=held,
            song_scope=song_scope_of(sings),
            incumbent=incumbent,
        )
        cast = sings if isinstance(sings, (set, frozenset)) else None
        if cast is not None and _song_bonus_over_cap(
            [
                (chr_id, plan.per_character[card.id].selected, mults_by_chr.get(chr_id, {}))
                for card, chr_id, _asm in ctx
            ],
            bench_characters,
            names,
            cast=cast,
            skilltree=skilltree,
            node_effect_ids=node_effect_ids,
        ):
            return None
        if placements_out is not None:
            placements_out.extend(placements)
    else:
        mults_by_chr = {}
        for chr_id, asm, recipes, is_leader, card, singer_weight in inputs:
            # base node values (grouping on, connect OFF) to place the sockets
            base = node_values_from_recipes(
                recipes,
                is_leader=is_leader,
                own_card=card,
                member_cards=members,
                bonus_permil=bonus_permil,
                overall_power=overall_power,
                k_constant=k_constant,
                active_pool_rate=active_pool_rate,
                singer_weight=singer_weight,
                song_scope=song_scope_of(sings),
            )
            mults_by_chr[chr_id] = _connect_multipliers(skilltree, asm.model_id, base) if asm.model_id else {}
    chars: dict[str, CharacterBoard] = dict(cache.bench_boards(bench_characters))
    digests: dict[str, BoardDigest] = dict(cache.bench_digests(bench_characters))
    for card, chr_id, asm in ctx:
        chars[card.member], digests[card.member] = cache.team_board(
            plan.plan_keys[card.id], card.member, asm, mults_by_chr.get(chr_id, {})
        )
    return BoardModel(
        characters=chars, name_by_chr=dict(names), _digests=digests, _base=cache.bench_base(bench_characters)
    )


_PAYING_YELLOWS: dict = {}


def _paying_yellows(
    selected: frozenset, chr_id: str, cast, skilltree: Mapping, node_effect_ids
) -> tuple[tuple[tuple[str, float], ...], float]:
    """One board's paying yellows on this song as `(rows, their sum)`: the `(node,
    permil)` of `_yellow_capped_assignment`'s `paying(selected)` in node id order
    (`plan_node_order`), and their sum as it takes it. Remembered per (set, board, cast)."""
    key = (id(selected), chr_id, cast, getattr(cast, "scope", None), id(skilltree), id(node_effect_ids))
    hit = _PAYING_YELLOWS.get(key)
    if hit is not None and hit[0] is selected and hit[1] is skilltree and hit[2] is node_effect_ids:
        return hit[3]
    rows = tuple(
        (n, p)
        for n in plan_node_order(selected)
        if (p := _yellow_permil(skilltree, n, chr_id, cast, node_effect_ids))
    )
    value = (rows, sum(p for _n, p in rows))
    if len(_PAYING_YELLOWS) > 50_000:
        _PAYING_YELLOWS.clear()
    _PAYING_YELLOWS[key] = (selected, skilltree, node_effect_ids, value)
    return value


_BENCH_YELLOWS: dict = {}


def _bench_yellow_rows(
    bench_characters: Mapping, name_by_chr: Mapping[str, str], cast, skilltree: Mapping, node_effect_ids
) -> tuple[tuple[str, float], ...]:
    """The off-team boards' paying yellows as `(chr id, permil x Connect multiplier)` in
    the bench's order, the rows worth 0 left out: what `_song_bonus_over_cap` and
    `_yellow_capped_assignment`'s room read from `bench_characters` (both skip the rows of
    a board on the team). Remembered per (bench, roster, cast)."""
    key = (
        id(bench_characters),
        id(name_by_chr),
        cast,
        getattr(cast, "scope", None),
        id(skilltree),
        id(node_effect_ids),
    )
    hit = _BENCH_YELLOWS.get(key)
    if (
        hit is not None
        and hit[0] is bench_characters
        and hit[1] is name_by_chr
        and hit[2] is skilltree
        and hit[3] is node_effect_ids
    ):
        return hit[4]
    inverse = {name: chr_id for chr_id, name in name_by_chr.items()}
    rows = []
    for name, entries in bench_characters.items():
        chr_id = inverse.get(name)
        if not chr_id:
            continue
        for e in entries:
            if e.get("node"):
                value = _yellow_permil_memo(skilltree, e["node"], chr_id, cast, node_effect_ids) * float(
                    e.get("connect_multiplier") or 1.0
                )
                if value:
                    rows.append((chr_id, value))
    if len(_BENCH_YELLOWS) > 512:
        _BENCH_YELLOWS.clear()
    hit = _BENCH_YELLOWS[key] = (bench_characters, name_by_chr, skilltree, node_effect_ids, tuple(rows))
    return hit[4]


def _board_model_by_kernel(
    kernel: Callable,
    plan: TeamBoardPlan,
    leader: Card,
    members: tuple[Card, ...],
    skilltree: Mapping,
    node_effect_ids: Mapping[str, list[str]],
    names: dict[str, str],
    *,
    overall_power: float,
    bonus_permil: float,
    k_constant: float,
    active_pool_rate: float,
    connect_cards,
    placements_out,
    bench_characters,
    name_by_chr,
    sings,
    cache: PlanCache,
    cast,
    held: frozenset,
    incumbent: Mapping,
) -> BoardModel | None:
    """`board_model_from_plan` with the Connect matching from `BOARD_KERNEL`: the plan
    cache's team boards under the multipliers it chose, over the cached bench (the model
    either Python path builds), or None to run the Python: a call the kernel does not
    express (a board without a model, a team name the roster maps elsewhere) or one it
    declines. A plan the cache did not make (the activation refinement's: no `plan_keys`)
    is assembled by its nodes (`selected_plan_key`) and matched as the plain path does:
    straight to `_yellow_capped_assignment`, with no cap checks before.

    It follows the Python exactly: the cards `held` on off-team sockets are out of every
    group (`_held_view`), and the account's placements (`incumbent`) win ties in every
    matching of the capped path, but in the first matching only when the cards are named
    (`tie_first`), as the plan cache's matching applies them."""
    if not name_by_chr or any(
        name_by_chr.get("chr-" + card.id.split("-")[1]) != card.member for card in (leader, *members)
    ):
        return None
    name_cards = placements_out is not None
    seen: set[str] = set()
    ctx: list[tuple[Card, str, AssembledPlan, tuple]] = []
    inputs: list[ConnectInput] = []
    yellows = []
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
        # a plan the cache did not make (the activation refinement's) is assembled by its nodes
        key = plan.plan_keys.get(card.id) or selected_plan_key(chr_id, cplan.selected)
        asm = cache.assembled_for(
            key,
            cplan,
            chr_id,
            skilltree=skilltree,
            node_effect_ids=node_effect_ids,
            connect_cards=connect_cards,
        )
        if not asm.model_id:
            return None
        recipes = cache.needed_recipes(key, asm, chr_id, node_effect_ids, skilltree)
        ctx.append((card, chr_id, asm, key))
        inputs.append(
            (
                chr_id,
                _held_view(asm, held) if held else asm,
                recipes,
                is_leader,
                member_by_chr.get(chr_id, card) if is_leader else card,
                singer_weight_for(sings, chr_id, is_leader),
            )
        )
        yellows.append(_paying_yellows(cplan.selected, chr_id, cast, skilltree, node_effect_ids))
    matched = kernel(
        inputs,
        members,
        overall_power=overall_power,
        bonus_permil=bonus_permil,
        k_constant=k_constant,
        active_pool_rate=active_pool_rate,
        song_scope=song_scope_of(sings),
        yellows=yellows,
        bench=_bench_yellow_rows(bench_characters, name_by_chr, cast, skilltree, node_effect_ids),
        cap=SONG_ALL_CAP_PERMIL,
        rounds=CAPPED_ROUNDS,
        incumbent=incumbent or None,
        tie_factor=1.0 + INCUMBENT_TIE,
        tie_first=name_cards,
        # the Python flow the plan takes: the fast path's checks for a plan the cache made,
        # the plain path (`_yellow_capped_assignment` at once) for one it did not
        checks=bool(plan.plan_keys),
    )
    if matched is None:
        return None
    mults_by_chr, placements = _placements_from_matched(matched, name_cards=name_cards, incumbent=incumbent)
    if placements_out is not None:
        placements_out.extend(placements)
    chars: dict[str, CharacterBoard] = dict(cache.bench_boards(bench_characters))
    digests: dict[str, BoardDigest] = dict(cache.bench_digests(bench_characters))
    for card, chr_id, asm, key in ctx:
        chars[card.member], digests[card.member] = cache.team_board(
            key, card.member, asm, mults_by_chr.get(chr_id, {})
        )
    return BoardModel(
        characters=chars, name_by_chr=dict(names), _digests=digests, _base=cache.bench_base(bench_characters)
    )
