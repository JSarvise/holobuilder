"""EXACT per-(song, leader) rows by branch-and-bound on the plan-cache leaf.

The exhaustive route proves a row by scoring every one of the 8,441,052 legal
rosters at ~0.42 ms wall each — an hour per row. This module proves the same
statement in minutes by never scoring most of them: a **valid upper bound** on
the score of every completion of a PARTIAL member set (0..4 chosen) prunes the
subtree when it cannot beat the incumbent, and only the survivors' leaves reach
`parallel_refine.evaluate` — the SAME leaf the exhaustive route used, so the
certified number is the same float.

## The bound is a monotone relaxation of the SAME leaf

`score_song_detailed` computes, for a roster `T = (L, M1..M5)` on chart `C`,

    S(T) = max_π Σ_n ceil( P(T) · K_C · (1 + Y) · w_n · (1 + F(T) + ratio_n(T, π)) )

with `ratio_n = E[max_i mag_i·(1 + SEU + B_n + T_i)·X_i]` over the ACTIVEs live
at note `n` (`song.py`, `scoring._score_up_pool`). Every factor is
non-negative and monotone non-decreasing in every skill/board contribution (no
negative magnitude exists in the catalogue; `ceil` is monotone), so
replacing each factor by something ≥ its value under EVERY completion of the
partial set gives an upper bound of the product and of the sum. The
relaxations, each stated with why it dominates:

* **gates** — a type/generation gate FIRES if the chosen members' count plus
  the number of open slots reaches its threshold (an open slot may be filled by
  a matching card); run-state gates resolve against `cfg.run_state` exactly as
  the leaf does. A fired gate is a superset of skills applied. An ACTIVE's
  conditional override reads `max(base, override)` when its gate COULD fire.
* **stat passives / leader outfit** — a count-`k` target reaches the `k`
  matching cards with the largest value of the stat it raises (≥ the leaf's
  top-`k` by total parameter, ≥ any slot-order recipient set); the open slots
  contribute the largest matching stats among the candidate cards (distinct
  holomems). `self` reaches its owner, team-wide reaches everyone.
* **open slots** — each of the `5-k` open slots is filled, PER FACTOR, by the
  best candidate for that factor (largest stat sum, largest board power, the
  strongest passive, the largest ACTIVE magnitude AND proc chance, the
  strongest SPECIAL, the largest support): a hypothetical card that dominates
  every real completion in every factor at once.
* **board** — a UNION `BoardModel`: every character's node set is the union of
  every plan the cache could assign her in any roster (member plan per card,
  leader / leader+member plan for the leader, the account's bench board),
  each node at the largest Connect multiplier any socket entry could give it.
  A superset of nodes at ≥ multipliers dominates every assembled board
  (`board_model_from_plan`), including the yellow song bonus (capped) and the
  once-per-node `skill_effect_up` union.
* **ACTIVE proc** — real proc chance `tier · (1 + team rate-up + board rate)`
  at its maximum (`p_mode="tier"`), or `p = 1` (`p_mode="one"`, the pack's
  `active_all_fire`); `E[max]` is monotone in every `p` and every magnitude
  (a coupling argument: raising one Bernoulli can only raise the maximum).
* **cooldown** — the ACTIVE lattice depends on the board's cooldown nodes,
  which is not monotone per note; the bound takes the UNION of the note sets
  over every cooldown value the character's plans and sockets can realise.
* **SPECIAL cue** — the joint slot assignment `max_π Σ_s Δ[π(s)][s]` is relaxed
  to `Σ_s max_m Δ[m][s]` (each cue window gets the best SPECIAL);
  in the coupled case (two cue windows over one note) the per-slot deltas
  over-count the maximum, so it stays an upper bound.
* **count-targeted supports** — reach everyone: `T_i ≤ Σ` of every targeted
  support on the team; team-wide supports and the leader outfit support enter
  the boost maximum as they do in the leaf.

A relative slack of `1e-9` is applied to the note constant before the `ceil`
so a different float summation order can never put the bound one unit under
the leaf.

## What this module does NOT do

It does not develop boards (the plan cache is the objective),
it does not import anything outside the stdlib and `hbuilder`, and it never
writes `data/` or `state/`. Stacking models other than additive (stat) and
`E[max]` (ACTIVE) are refused at construction: the bound arithmetic is written
for the shipped models and would silently be wrong for another.
"""

from __future__ import annotations

import bisect
import dataclasses
import itertools
import json
import math
import multiprocessing
import os
import time
from typing import Callable, Iterable, Mapping, Sequence

from hbuilder.domain import board as board_mod
from hbuilder.domain import board_plan as bp
from hbuilder.domain.board import BoardModel, BoardNode, CharacterBoard
from hbuilder.domain.card import Card, EffectTarget
from hbuilder.domain.gates import (
    GateResolution,
    resolve_card_set_gates,
    resolve_leader_costume_skills,
)
from hbuilder.domain.schema import STAT_NAMES, TEAM_WIDE_TARGET_KIND
from hbuilder.domain.scoring import (
    STAT_NAMES_BY_EFFECT_TYPE,
    _bucket_entry,
    _is_score_up_effect,
    _pool_for,
    _resolve_skill,
    _score_active,
)
from hbuilder.domain.scoring_config import ScoringConfig
from hbuilder.domain.song import SCORE_COEFFICIENT, SPECIAL_CUE_COUNT, SongChart
from hbuilder.solver import parallel_refine as pr
from hbuilder.solver.parallel import default_workers

__all__ = [
    "BoardVariant",
    "ChartStructure",
    "RowBound",
    "board_variants",
    "BnBResult",
    "branch_and_bound",
    "legal_rosters",
    "local_search_1swap",
    "union_board_model",
]

BOUND_SLACK = 1e-9


def _chr_of(card: Card) -> str:
    return "chr-" + card.id.split("-")[1]


# ---------------------------------------------------------------------------
# Song structure — the chart's own geometry, precomputed once per chart
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class ChartStructure:
    """What the bound reads off a chart: note times, the per-note weight
    `w_n·(1+combo_n)` (`song.py` `note_weights`), the distinct weight values
    with prefix counts (so a run of notes sums in O(#distinct w) instead of
    O(notes)), the SPECIAL cue times, and the note-index range of every cue
    window for a given duration."""

    times: tuple[float, ...]
    weights: tuple[float, ...]
    distinct_w: tuple[float, ...]
    prefix: tuple[tuple[int, ...], ...]      # prefix[w_i][n] = #notes < n with weight distinct_w[w_i]
    cues: tuple[float, ...]
    song_seconds: float
    music_coefficient: float
    perfect_sum: float

    @classmethod
    def of(cls, chart: SongChart, cfg: ScoringConfig | None = None) -> "ChartStructure":
        """`cfg.platform` (when the config carries one) selects the client's
        PERFECT column exactly as `score_song_detailed` does — numerator and
        divisor alike; a config/chart without the attribute reads the PC base."""
        platform = getattr(cfg, "platform", None) if cfg is not None else None
        if platform is not None and hasattr(chart.notes[0], "perfect_permil_for"):
            permil_of = lambda note: note.perfect_permil_for(platform)  # noqa: E731
            perfect_sum = chart.notes_perfect_coefficient_sum(platform)
        else:
            permil_of = lambda note: note.perfect_permil  # noqa: E731
            perfect_sum = chart.notes_perfect_coefficient_sum()
        times = tuple(note.time for note in chart.notes)
        weights = tuple(
            (permil_of(note) / 1000.0) * (1.0 + chart.combo_coefficient(i + 1))
            for i, note in enumerate(chart.notes))
        distinct = tuple(sorted(set(weights)))
        idx = {w: i for i, w in enumerate(distinct)}
        n = len(times)
        prefix = []
        for wi in range(len(distinct)):
            row = [0] * (n + 1)
            c = 0
            for k in range(n):
                if idx[weights[k]] == wi:
                    c += 1
                row[k + 1] = c
            prefix.append(tuple(row))
        return cls(times=times, weights=weights, distinct_w=distinct, prefix=tuple(prefix),
                   cues=tuple(chart.special_cues), song_seconds=times[-1],
                   music_coefficient=chart.music_coefficient(),
                   perfect_sum=perfect_sum)

    def note_range(self, start: float, end: float) -> tuple[int, int]:
        """`{i : start <= times[i] < end}` as a half-open index range —
        `song.py`'s own bisect predicate."""
        return bisect.bisect_left(self.times, start), bisect.bisect_left(self.times, end)

    def cue_range(self, slot: int, duration: float) -> tuple[int, int]:
        cue = self.cues[slot]
        return self.note_range(cue, cue + duration)

    def active_ranges(self, interval: float, duration: float) -> tuple[tuple[int, int], ...]:
        """`song._active_windows` on the note axis: checks at `k·interval`,
        `k = 1, 2, ...`, windows `[k·I, k·I + duration)`, never clamped."""
        out = []
        k = 1
        while k * interval <= self.song_seconds:
            lo, hi = self.note_range(k * interval, k * interval + duration)
            if lo < hi:
                out.append((lo, hi))
            k += 1
        return tuple(out)

    def sum_ceil(self, a: int, b: int, factor: float) -> float:
        """`Σ_{n in [a,b)} ceil(factor · w_n)`, by distinct weight."""
        total = 0.0
        for wi, w in enumerate(self.distinct_w):
            cnt = self.prefix[wi][b] - self.prefix[wi][a]
            if cnt:
                total += cnt * math.ceil(factor * w)
        return total

    def weighted_coverage(self, ranges: Iterable[tuple[int, int]]) -> float:
        """Note-weight-density of a set of note ranges: `Σ_{covered} w_n / Σ w_n`
        (the note density of an ACTIVE window, weighted as the scorer weights)."""
        total = sum(self.weights)
        covered = 0.0
        for lo, hi in ranges:
            covered += sum(self.weights[lo:hi])
        return covered / total if total else 0.0


# ---------------------------------------------------------------------------
# Board variants — every board a character can carry, and the per-socket bound
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class BoardVariant:
    """One board a character can carry in some roster: her BENCH board (the
    account's capture, Connect multipliers fixed per node) or one PLAN of the
    cache (member plan per own card; `leader` / `leader+member` for the
    leader), whose multipliers come from the team-wide socket assignment and
    are bounded here PER SOCKET: at most one shape sits at each eligible
    socket, sockets never share a node, so
    `Σ_sockets max_shape lift` dominates every assignment (card uniqueness
    across the team relaxed)."""

    kind: str                                          # "bench" | "plan"
    role: str                                          # "" for bench
    nodes: tuple[tuple[str, object, float], ...]       # (node_id, BoardEffect, fixed multiplier)
    entries: tuple[tuple[str, int, tuple[str, ...]], ...]   # (socket, permil, covered) — plans only

    def bound(self, g: Callable[[object, float], float]) -> float:
        """`Σ_n g(effect_n, mult_n)` for a bench board; for a plan
        `Σ_n g(n, 1) + Σ_sockets max_shape Σ_{n∈shape} (g(n, 1+permil/1000) − g(n, 1))`."""
        if self.kind == "bench":
            return sum(g(eff, mult) for _nid, eff, mult in self.nodes)
        eff_of = {nid: eff for nid, eff, _m in self.nodes}
        base = sum(g(eff, 1.0) for _nid, eff, _m in self.nodes)
        best: dict[str, float] = {}
        for socket, permil, covered in self.entries:
            m = 1.0 + permil / 1000.0
            lift = 0.0
            for nid in covered:
                eff = eff_of.get(nid)
                if eff is not None:
                    lift += g(eff, m) - g(eff, 1.0)
            if lift > best.get(socket, 0.0):
                best[socket] = lift
        return base + sum(best.values())


def board_variants(
    ctx: pr.RefineContext, cache: bp.PlanCache, leader: Card, pool: Sequence[Card], *,
    tree_for: Callable[[Card], object], budget_for: Callable[[Card], int], sings: bool,
) -> tuple[dict[str, list[BoardVariant]], dict[str, list[tuple[float, ...]]]]:
    """Per holomem, every `BoardVariant` `board_model_from_plan` can give her
    for any roster of `pool` under `leader`, and per holomem the realisable
    cooldown-shorten sums of the boards a MEMBER card of hers can carry (one
    tuple per plan, over the cartesian product of each cooldown node's
    possible multipliers)."""
    lead_chr = _chr_of(leader)
    variants: dict[str, list[BoardVariant]] = {}
    cooldowns: dict[str, list[tuple[float, ...]]] = {}
    for name, cb in cache.bench_boards(ctx.bench_characters).items():
        variants.setdefault(name, []).append(BoardVariant(
            kind="bench", role="",
            nodes=tuple((node.node_id, eff, node.connect_multiplier)
                        for node in cb.nodes for eff in node.effects),
            entries=()))
    seen_keys: set[tuple] = set()

    def add_plan(card: Card, role: str, budget: int, sings_flag: bool) -> None:
        tree = tree_for(card)
        if tree is None:
            return
        key = bp.plan_key(card, role, budget, sings_flag)
        if key in seen_keys:
            return
        seen_keys.add(key)
        chr_id = _chr_of(card)
        cplan = cache.plan_for(card, role, budget, sings_flag, tree=tree,
                               node_effect_ids=ctx.node_effect_ids, skilltree=ctx.skilltree)
        asm = cache.assembled_for(key, cplan, chr_id, skilltree=ctx.skilltree,
                                  node_effect_ids=ctx.node_effect_ids,
                                  connect_cards=ctx.connect_cards)
        entries = [(socket, permil, covered) for socket, permil, covered, _cards in asm.entries]
        if ctx.connect_cards is None and asm.model_id:
            # idealised sockets (`_connect_multipliers`): every shape at max
            # bloom may sit at any socket — one entry per (socket, shape)
            positions = ctx.skilltree.get("models", {}).get(asm.model_id, {})
            extents = ctx.skilltree.get("extents", {})
            connect = ctx.skilltree.get("connect", {})
            pos_to_node = {tuple(p): n for n, p in positions.items()}
            selected = {nid for nid, _e in asm.nodes}
            shapes = set()
            for _card_id, eff_id in ctx.skilltree.get("card_connect", {}).items():
                eff = connect.get(eff_id)
                if eff and eff.get("area") in extents:
                    shapes.add((tuple(map(tuple, extents[eff["area"]])), eff["levels"][-1]["v"]))
            for socket, spos in positions.items():
                if not socket.startswith("S"):
                    continue
                sx, sy = spos
                for offsets, boost in shapes:
                    covered = tuple(n for dx, dy in offsets
                                    for n in (pos_to_node.get((sx + dx, sy + dy)),)
                                    if n and n in selected)
                    if covered:
                        entries.append((socket, int(boost), covered))
        mults_by_node: dict[str, set[float]] = {}
        for _socket, permil, covered in entries:
            for node in covered:
                mults_by_node.setdefault(node, {1.0}).add(1.0 + permil / 1000.0)
        variants.setdefault(card.member, []).append(BoardVariant(
            kind="plan", role=role,
            nodes=tuple((nid, eff, 1.0) for nid, eff in asm.nodes),
            entries=tuple(entries)))
        if role != bp.PLAN_ROLE_LEADER:
            # only a board a MEMBER card carries shapes a member's lattice; the
            # leader-only plan is never a member's board
            sums = {0.0}
            for nid, eff in asm.nodes:
                if eff.kind == "cooldown_shorten" and not eff.conditional:
                    choices = [eff.value * m for m in sorted(mults_by_node.get(nid, {1.0}))]
                    sums = {s0 + v for s0 in sums for v in choices}
            cooldowns.setdefault(card.member, []).append(tuple(sorted(sums)))

    for card in pool:
        if _chr_of(card) == lead_chr:
            add_plan(card, bp.PLAN_ROLE_LEADER_MEMBER, budget_for(leader), sings)
        else:
            add_plan(card, bp.PLAN_ROLE_MEMBER, budget_for(card), False)
    add_plan(leader, bp.PLAN_ROLE_LEADER, budget_for(leader), sings)
    return variants, cooldowns


def union_board_model(
    ctx: pr.RefineContext, cache: bp.PlanCache, leader: Card, pool: Sequence[Card], *,
    tree_for: Callable[[Card], object], budget_for: Callable[[Card], int], sings: bool,
) -> BoardModel:
    """A `BoardModel` that dominates every board `board_model_from_plan` can
    assemble for any roster of `pool` under `leader`: per character the UNION
    of her variants' nodes, each at the largest multiplier any entry could give
    it. Coarser than the per-socket `BoardVariant.bound` (every covered node at
    its own maximum at once, ~2× the real board power on the m0334
    incumbent) — kept as the reference model tests compare the variants to."""
    variants, _cd = board_variants(ctx, cache, leader, pool, tree_for=tree_for,
                                   budget_for=budget_for, sings=sings)
    names = dict(ctx.name_by_chr or {})
    for card in (*pool, leader):
        names[_chr_of(card)] = card.member
    chars = {}
    for holomem, vs in variants.items():
        d: dict[tuple, float] = {}
        for v in vs:
            mult_max: dict[str, float] = {}
            for _socket, permil, covered in v.entries:
                for nid in covered:
                    mult_max[nid] = max(mult_max.get(nid, 1.0), 1.0 + permil / 1000.0)
            for nid, eff, m in v.nodes:
                key = (nid, eff)
                d[key] = max(d.get(key, 1.0), m, mult_max.get(nid, 1.0))
        chars[holomem] = CharacterBoard(
            holomem=holomem,
            nodes=tuple(BoardNode(node_id=nid, effects=(eff,), connect_multiplier=m)
                        for (nid, eff), m in d.items()))
    return BoardModel(characters=chars, name_by_chr=names)


# ---------------------------------------------------------------------------
# Per-card relaxed skill data
# ---------------------------------------------------------------------------
def _forced(gate: GateResolution | None) -> GateResolution | None:
    """A gate resolution with every type/generation predicate FIRED (run-state
    gates keep the leaf's own answer against `cfg.run_state`)."""
    if gate is None or gate.fired:
        return gate
    if gate.predicate.dimension in ("type", "generation"):
        return GateResolution(predicate=gate.predicate, fired=True,
                              actual_count=max(gate.actual_count, gate.predicate.threshold))
    return gate


def _pred(gate: GateResolution | None):
    """`(dimension, value, threshold)` for a type/generation gate the bound
    must count, `None` for no gate / a run-state gate (already resolved)."""
    if gate is None or gate.predicate.dimension not in ("type", "generation"):
        return None
    return (gate.predicate.dimension, gate.predicate.value, gate.predicate.threshold)


@dataclasses.dataclass(frozen=True)
class _StatEntry:
    rate: float                       # rate * family weight (override applied per gate state)
    rate_base: float                  # rate with the base magnitude (when the override might not fire)
    stats: tuple[str, ...]
    target: object                    # None | EffectTarget
    gate: tuple | None                # counted type/generation gate
    override_gate: tuple | None
    owner: str


@dataclasses.dataclass(frozen=True)
class _ActiveEntry:
    mag: float                        # weight * override magnitude rate (or base if no override)
    mag_base: float
    tier: float
    interval: float
    duration: float
    gate: tuple | None
    override_gate: tuple | None
    ranges: tuple[tuple[int, int], ...]   # union over realisable cooldowns


@dataclasses.dataclass(frozen=True)
class _SpecialEntry:
    mag: float
    duration: float
    gate: tuple | None
    ranges_by_slot: tuple[tuple[int, int], ...]


@dataclasses.dataclass(frozen=True)
class _SupportEntry:
    mag: float
    targeted: bool
    gate: tuple | None


@dataclasses.dataclass(frozen=True)
class _CardData:
    card: Card
    holomem: str
    ctype: str
    gens: frozenset[str]
    stats: tuple[float, float, float]
    base_sum: float
    board_power: float
    stat_entries: tuple[_StatEntry, ...]
    actives: tuple[_ActiveEntry, ...]
    specials: tuple[_SpecialEntry, ...]
    supports: tuple[_SupportEntry, ...]
    score_rates: float                # F: summed non-score-up score-pool rates
    rate_up: float                    # activation_rate_up EV folded into team_rate_up
    coverage: float                   # note-weighted ACTIVE coverage (structure)


class RowBound:
    """The valid upper bound for one (song, leader, pool) row — see the module
    docstring. `ub(idx)` bounds every completion of the partial member set
    `idx` (pool indices, distinct holomems, 0..5 cards); at 5 cards it bounds
    the leaf itself and is what the validation compares against `evaluate`."""

    def __init__(
        self, ctx: pr.RefineContext, cache: bp.PlanCache, leader: Card,
        pool: Sequence[Card], chart: SongChart, *,
        tree_for: Callable[[Card], object], budget_for: Callable[[Card], int],
        sings: bool, p_mode: str = "tier",
    ) -> None:
        cfg = ctx.cfg
        if getattr(cfg.stacking_model, "name", "") != "additive":
            raise ValueError("RowBound is written for additive stat/rate stacking "
                             f"(got {getattr(cfg.stacking_model, 'name', cfg.stacking_model)!r})")
        if getattr(cfg.active_stacking_model, "name", "") != "max":
            raise ValueError("RowBound is written for the E[max] ACTIVE model "
                             f"(got {getattr(cfg.active_stacking_model, 'name', '?')!r})")
        if p_mode not in ("tier", "one"):
            raise ValueError("p_mode must be 'tier' or 'one'")
        self.p_mode = p_mode
        self.cfg = cfg
        self.leader = leader
        self.pool = tuple(pool)
        self.chart = chart
        self.structure = ChartStructure.of(chart, cfg)
        st = self.structure
        self.sings = sings
        self.leader_holomem = leader.member
        singers = tuple(chart.singers)

        # -- board variants and their per-socket bounds ------------------------
        self.variants, cooldowns = board_variants(
            ctx, cache, leader, self.pool, tree_for=tree_for, budget_for=budget_for, sings=sings)
        h_L = self.leader_holomem
        names = dict(ctx.name_by_chr or {})
        for card in (*self.pool, leader):
            names[_chr_of(card)] = card.member
        cast = {names.get(sg, sg) for sg in singers}
        scope = chart.singer_type or "all"
        # yellow song bonus: the singers' boards, capped per cast (`song_score_bonus_for`)
        y = 0.0
        for holomem in cast:
            y += max((v.bound(lambda e, m: _scaled_value(e, m) if (
                e.kind == "song_score_bonus" and (e.song_scope or "all") == scope) else 0.0)
                for v in self.variants.get(holomem, ())), default=0.0)
        if not singers:
            y = sum(max((v.bound(lambda e, m: _scaled_value(e, m) if (
                e.kind == "song_score_bonus" and (e.song_scope or "all") == "all") else 0.0)
                for v in vs), default=0.0) for vs in self.variants.values())
        self.y_hat = min(y, board_mod.SONG_ALL_CAP_PERMIL / 1000.0)
        # skill_effect_up: once per node for the team; every node that could
        # reach any pool card under this leader (unconditional) + the cast's
        # conditional ones — a constant of the row
        seu = 0.0
        cseu = 0.0
        for holomem, vs in self.variants.items():
            def g_seu(e, m, holomem=holomem, cond=False):
                if e.kind != "skill_effect_up" or e.conditional != cond:
                    return 0.0
                if not any(board_mod._reaches_one(e, holomem, h_L, c) for c in self.pool):
                    return 0.0
                return _scaled_value(e, m)
            seu += max((v.bound(g_seu) for v in vs), default=0.0)
            if holomem in cast:
                cseu += max((v.bound(lambda e, m, h=holomem: g_seu(e, m, h, True)) for v in vs),
                            default=0.0)
        self.seu_hat = seu + cseu
        # activation-rate and cooldown boards per holomem (member-scope nodes on
        # her own board, `uptime_for`): the largest a member card of hers can carry
        self.board_rate: dict[str, float] = {}
        for holomem, vs in self.variants.items():
            self.board_rate[holomem] = max(
                (v.bound(lambda e, m: _scaled_value(e, m) if (
                    e.kind == "activation_rate_up" and not e.conditional) else 0.0)
                 for v in vs if v.role != bp.PLAN_ROLE_LEADER), default=0.0)
        rate_board = _UptimeBoard(self.board_rate, cooldowns)
        cfg_u = dataclasses.replace(cfg, board=rate_board)

        # -- per-card relaxed data ------------------------------------------
        cards: list[_CardData] = []
        for card in self.pool:
            gates = resolve_card_set_gates((card,), cfg.run_state)
            stat_entries: list[_StatEntry] = []
            actives: list[_ActiveEntry] = []
            specials: list[_SpecialEntry] = []
            supports: list[_SupportEntry] = []
            score_rates = 0.0
            rate_up = 0.0
            for resolved in gates.normal_by_card_id[card.id]:
                fr = dataclasses.replace(resolved, gate=_forced(resolved.gate),
                                         override_gate=_forced(resolved.override_gate))
                fr_base = dataclasses.replace(fr, override_gate=None)
                pool_kind = _pool_for(fr)
                if fr.family == "activation_rate_up":
                    rate, state, _ = _resolve_skill(fr, cfg_u, 0.0)
                    if state == "applied":
                        rate_up += _score_active(fr, cfg_u, 0.0)
                    continue
                if pool_kind == "none":
                    continue
                rate, state, _ = _resolve_skill(fr, cfg, 0.0)
                if state != "applied":
                    continue
                rate_base = _resolve_skill(fr_base, cfg, 0.0)[0]
                weight = cfg.family_weights[fr.family]
                gate_p = _pred(resolved.gate)
                ov_p = _pred(resolved.override_gate) if fr.skill.override is not None else None
                kind = fr.skill.kind
                if pool_kind == "stat":
                    stat_entries.append(_StatEntry(
                        rate=rate * weight, rate_base=rate_base * weight,
                        stats=STAT_NAMES_BY_EFFECT_TYPE[fr.skill.effect_type],
                        target=fr.skill.target, gate=gate_p, override_gate=ov_p, owner=card.id))
                elif kind == "SPECIAL" and (pool_kind == "support" or _is_score_up_effect(fr)):
                    # a SPECIAL is a BOOST live in its slot's cue window
                    # (`song._term_for`): per-slot delta, never always-on
                    mag = _bucket_entry(fr, cfg, 0.0, weight, "applied")[1]
                    duration = float(fr.skill.duration)
                    specials.append(_SpecialEntry(
                        mag=mag, duration=duration, gate=gate_p,
                        ranges_by_slot=tuple(st.cue_range(s, duration)
                                             for s in range(SPECIAL_CUE_COUNT))))
                elif pool_kind == "support":
                    # PASSIVE / (COSTUME) supports are live for the whole run; an
                    # ACTIVE-kind support would be windowed with a proc chance —
                    # counted here as always-on, which only widens the bound
                    mag = _bucket_entry(fr, cfg, 0.0, weight, "applied")[1]
                    target = fr.skill.target
                    targeted = isinstance(target, EffectTarget) and target.kind != TEAM_WIDE_TARGET_KIND
                    supports.append(_SupportEntry(mag=mag, targeted=targeted, gate=gate_p))
                elif _is_score_up_effect(fr):
                    mag = _bucket_entry(fr, cfg, 0.0, weight, "applied")[1]
                    mag_base = _bucket_entry(fr_base, cfg, 0.0, weight, "applied")[1]
                    if kind == "ACTIVE":
                        interval = float(fr.skill.interval)
                        duration = float(fr.skill.duration)
                        hand = cfg.active_cooldown_reduction.get(card.member, 0.0)
                        cds = set()
                        for tup in cooldowns.get(card.member, [(0.0,)]):
                            cds.update(tup)
                        ranges: set[tuple[int, int]] = set()
                        for cd in sorted(cds):
                            ranges.update(st.active_ranges(interval / (1.0 + hand + cd), duration))
                        actives.append(_ActiveEntry(
                            mag=mag, mag_base=mag_base,
                            tier=cfg.probability_tiers[fr.skill.probability_tier],
                            interval=interval, duration=duration,
                            gate=gate_p, override_gate=ov_p, ranges=_merge_ranges(ranges)))
                    # (`_is_score_up_effect` admits only ACTIVE / SPECIAL; SPECIAL was routed above)
                else:
                    score_rates += rate * weight
            stats = tuple(float(card.stat(s)) for s in STAT_NAMES)
            bpow = 0.0   # board power lives in `bench_power` / `plan_delta` (roster-dependent)
            all_ranges = set()
            for a in actives:
                all_ranges.update(a.ranges)
            cards.append(_CardData(
                card=card, holomem=card.member, ctype=card.type,
                gens=frozenset(card.generations), stats=stats, base_sum=sum(stats),
                board_power=bpow,
                stat_entries=tuple(stat_entries), actives=tuple(actives),
                specials=tuple(specials), supports=tuple(supports),
                score_rates=score_rates, rate_up=rate_up,
                coverage=st.weighted_coverage(_merge_ranges(all_ranges))))
        self.cards = tuple(cards)
        # -- board power: the account's bench boards reach every card in every
        # roster; a character's PLAN replaces her bench only when she is on the
        # team (the leader always is). `bench_power[c]` = Σ_X bench(X→c);
        # `plan_delta[X][c]` = max(0, best plan bound(X→c) − bench(X→c)).
        self.bench_power: list[float] = []
        self.plan_delta: dict[str, list[float]] = {}
        self.delta_sorted: list[list[tuple[float, str]]] = []
        for ci, cd in enumerate(self.cards):
            bench_total = 0.0
            per_x: list[tuple[float, str]] = []
            for holomem, vs in self.variants.items():
                bench_v = [v for v in vs if v.kind == "bench"]
                plan_v = [v for v in vs if v.kind == "plan"]
                bench_val = self._variant_power(bench_v[0], cd.card, holomem, cast) if bench_v else 0.0
                bench_total += bench_val
                if plan_v:
                    best = max(self._variant_power(v, cd.card, holomem, cast) for v in plan_v)
                    d = max(0.0, best - bench_val)
                else:
                    d = 0.0
                self.plan_delta.setdefault(holomem, [0.0] * len(self.cards))[ci] = d
                if d > 0.0:
                    per_x.append((d, holomem))
            per_x.sort(key=lambda t: -t[0])
            self.bench_power.append(bench_total)
            self.delta_sorted.append(per_x)
        for holomem in self.variants:
            self.plan_delta.setdefault(holomem, [0.0] * len(self.cards))

        # -- leader costume ---------------------------------------------------
        self.outfit_stat: _StatEntry | None = None
        # The principal AND the outfit's scored `score_support`
        # rider (`gates.resolve_leader_costume_skills`), each with its own gate
        # predicate — a forced-fired rider support enters the `b_pass`/`t_hat`
        # maximum below exactly like a principal one, so the bound stays valid.
        # (Resolved against the leader's OWN one-card aggregates only to build
        # the record; every gate is re-decided per prefix by `_fires`.)
        self.outfit_supports: tuple[_SupportEntry, ...] = ()
        self.outfit_score_rate = 0.0
        leader_agg = resolve_card_set_gates((leader,), cfg.run_state).aggregates
        outfit_supports: list[_SupportEntry] = []
        for costume in resolve_leader_costume_skills(leader, leader_agg, cfg.run_state):
            fr = dataclasses.replace(costume, gate=_forced(costume.gate),
                                     override_gate=_forced(costume.override_gate))
            pool_kind = _pool_for(fr)
            if pool_kind != "none":
                rate, state, _ = _resolve_skill(fr, cfg, 0.0)
                if state == "applied":
                    weight = cfg.family_weights[fr.family]
                    gate_p = _pred(costume.gate)
                    if pool_kind == "stat":
                        rate_base = _resolve_skill(dataclasses.replace(fr, override_gate=None), cfg, 0.0)[0]
                        self.outfit_stat = _StatEntry(
                            rate=rate * weight, rate_base=rate_base * weight,
                            stats=STAT_NAMES_BY_EFFECT_TYPE[fr.skill.effect_type],
                            target=fr.skill.target, gate=gate_p,
                            override_gate=_pred(costume.override_gate) if fr.skill.override is not None else None,
                            owner=leader.id)
                    elif pool_kind == "support":
                        mag = _bucket_entry(fr, cfg, 0.0, weight, "applied")[1]
                        target = fr.skill.target
                        targeted = isinstance(target, EffectTarget) and target.kind != TEAM_WIDE_TARGET_KIND
                        outfit_supports.append(_SupportEntry(mag=mag, targeted=targeted, gate=gate_p))
                    elif pool_kind == "score" and not _is_score_up_effect(fr):
                        self.outfit_score_rate = rate * weight
                    # a COSTUME score-up effect would be a ratio term the leaf also
                    # cannot carry (`_is_score_up_effect` is ACTIVE/SPECIAL only)
        self.outfit_supports = tuple(outfit_supports)

        # -- sorted candidate tables (value desc, with holomem) ---------------
        n = len(self.cards)
        self._by_base = sorted(range(n), key=lambda i: -self.cards[i].base_sum)
        self._by_stat = [sorted(range(n), key=lambda i, s=s: -self.cards[i].stats[s])
                         for s in range(len(STAT_NAMES))]
        self._by_score_rate = sorted(range(n), key=lambda i: -self.cards[i].score_rates)
        self._by_rate_up = sorted(range(n), key=lambda i: -self.cards[i].rate_up)
        self._by_tsupport = sorted(range(n), key=lambda i: -_max_support(self.cards[i], True))
        self._by_wsupport = sorted(range(n), key=lambda i: -_max_support(self.cards[i], False))
        # matching tables: (dimension, value) -> per stat, indices sorted by that stat desc
        keys = set()
        for c in self.cards:
            keys.add(("type", c.ctype))
            for g in c.gens:
                keys.add(("generation", g))
        self._match: dict[tuple, list[list[int]]] = {}
        for dim, value in keys:
            members = [i for i, c in enumerate(self.cards)
                       if (c.ctype == value if dim == "type" else value in c.gens)]
            self._match[(dim, value)] = [
                sorted(members, key=lambda i, s=s: -self.cards[i].stats[s])
                for s in range(len(STAT_NAMES))]
        # ACTIVE hypothetical: the largest magnitude, largest tier, union of ranges
        self.hyp_mag = max((a.mag for c in self.cards for a in c.actives), default=0.0)
        self.hyp_tier = max((a.tier for c in self.cards for a in c.actives), default=0.0)
        self.hyp_board_rate = max(self.board_rate.values(), default=0.0)
        hyp_ranges = set()
        for c in self.cards:
            for a in c.actives:
                hyp_ranges.update(a.ranges)
        self.hyp_ranges = _merge_ranges(hyp_ranges)
        # SPECIAL hypotheticals: the Pareto front of (magnitude, duration) over
        # the pool — every real SPECIAL is dominated in BOTH by some front point
        # (a shorter, stronger SPECIAL does not dominate a longer one), and the
        # per-slot delta is monotone in both, so `max` over the front dominates
        # `max` over the candidates
        pairs = sorted({(sp.mag, sp.duration) for c in self.cards for sp in c.specials},
                       key=lambda t: (-t[0], -t[1]))
        front: list[tuple[float, float]] = []
        best_dur = -1.0
        for mag, dur in pairs:
            if dur > best_dur:
                front.append((mag, dur))
                best_dur = dur
        self.hyp_specials = tuple(
            (mag, tuple(st.cue_range(s, dur) for s in range(SPECIAL_CUE_COUNT)))
            for mag, dur in front)
        self.k_chart = SCORE_COEFFICIENT * st.music_coefficient / st.perfect_sum * (1.0 + self.y_hat)
        self._emax_cache: dict[tuple, float] = {}
        self._run_items: dict[tuple[int, int], tuple[tuple[float, int], ...]] = {}
        # per-card ACTIVE overlap (note-weighted) — song structure for ordering
        self._overlap_cache: dict[tuple[int, int], float] = {}
        self.stats_ = {"bound_evals": 0}

    def _variant_power(self, v: BoardVariant, card: Card, holomem: str, cast: set[str]) -> float:
        """Upper bound of `card`'s share of `power_bonus_for` (+ the
        singer-conditional `conditional_stat_power_for` when the node's holomem
        sings) from ONE character's board variant: flat effects ceiled per node
        as `BoardEffect.scaled` does, rate effects summed then ceiled against
        the card's stat — one ceil per (variant, stat, conditional), which is
        ≥ the leaf's single ceil per (card, stat) over every character."""
        h_L = self.leader_holomem
        contrib = 0.0
        for cond in (False, True):
            if cond and holomem not in cast:
                continue
            for s in STAT_NAMES:
                stat = float(card.stat(s))

                def g_flat(e, m, s=s, cond=cond):
                    if e.kind != "stat" or e.conditional != cond:
                        return 0.0
                    if e.stat != "all" and e.stat != s:
                        return 0.0
                    if not board_mod._reaches_one(e, holomem, h_L, card):
                        return 0.0
                    return _scaled_value(e, m)

                def g_rate(e, m, s=s, cond=cond):
                    if e.kind != "rate" or e.conditional != cond:
                        return 0.0
                    if e.stat != "all" and e.stat != s:
                        return 0.0
                    if not board_mod._reaches_one(e, holomem, h_L, card):
                        return 0.0
                    return _scaled_value(e, m)
                contrib += v.bound(g_flat)
                rate = v.bound(g_rate)
                if rate > 0.0:
                    contrib += math.ceil(rate * stat)
        return contrib

    def _board_power_of(self, i: int, chars_team: frozenset[str] | set[str], open_slots: int) -> float:
        """Board power reaching pool card `i`: bench boards + the deltas of the
        team's characters + the `open_slots` largest deltas of any other
        character (a new member's plan reaches this card too)."""
        v = self.bench_power[i]
        for h in chars_team:
            row = self.plan_delta.get(h)
            if row is not None:
                v += row[i]
        if open_slots:
            n = 0
            for d, h in self.delta_sorted[i]:
                if h in chars_team:
                    continue
                v += d
                n += 1
                if n == open_slots:
                    break
        return v

    # -- structure helpers -------------------------------------------------
    def overlap(self, i: int, j: int) -> float:
        """Note-weighted fraction of the chart where card i's and card j's
        ACTIVE windows are BOTH live (the synchronisation the E[max] discounts)."""
        key = (min(i, j), max(i, j))
        v = self._overlap_cache.get(key)
        if v is None:
            ri = _merge_ranges({r for a in self.cards[i].actives for r in a.ranges})
            rj = _merge_ranges({r for a in self.cards[j].actives for r in a.ranges})
            both = []
            for lo, hi in ri:
                for lo2, hi2 in rj:
                    a, b = max(lo, lo2), min(hi, hi2)
                    if a < b:
                        both.append((a, b))
            v = self.structure.weighted_coverage(both)
            self._overlap_cache[key] = v
        return v

    # -- the bound ------------------------------------------------------------
    def _top(self, order: Sequence[int], value: Callable[[int], float], k: int,
             excluded: frozenset[str]) -> float:
        """Sum of the `k` largest values over `order` (value-desc) with distinct
        holomems not in `excluded`."""
        if k <= 0:
            return 0.0
        total = 0.0
        seen: set[str] = set()
        for i in order:
            h = self.cards[i].holomem
            if h in excluded or h in seen:
                continue
            seen.add(h)
            total += value(i)
            if len(seen) == k:
                break
        return total

    def _top_values(self, order: Sequence[int], value: Callable[[int], float], k: int,
                    excluded: frozenset[str]) -> list[float]:
        out: list[float] = []
        seen: set[str] = set()
        for i in order:
            h = self.cards[i].holomem
            if h in excluded or h in seen:
                continue
            seen.add(h)
            out.append(value(i))
            if len(out) == k:
                break
        return out

    def _fires(self, gate: tuple | None, counts: Mapping[tuple, int], open_slots: int) -> tuple[bool, bool]:
        """`(could fire, must fire)` for a counted gate under the partial set."""
        if gate is None:
            return True, True
        dim, value, thr = gate
        c = counts.get((dim, value), 0)
        return c + open_slots >= thr, c >= thr

    def _recipient_stat_sum(self, entry: _StatEntry, chosen: Sequence[int], open_slots: int,
                            excluded: frozenset[str], stat_of_chosen: Sequence[tuple]) -> float:
        """Upper bound of `Σ_{recipients} Σ_{stats} stat` for one stat entry."""
        target = entry.target
        total = 0.0
        if target is None or (isinstance(target, EffectTarget) and target.kind == TEAM_WIDE_TARGET_KIND):
            for s_name in entry.stats:
                s = STAT_NAMES.index(s_name)
                total += sum(st[s] for st in stat_of_chosen)
                total += self._top(self._by_stat[s], lambda i, s=s: self.cards[i].stats[s],
                                   open_slots, excluded)
            return total
        if not isinstance(target, EffectTarget):
            return 0.0
        if target.kind == "self":
            owner = entry.owner
            for i in chosen:
                if self.cards[i].card.id == owner:
                    return sum(self.cards[i].stats[STAT_NAMES.index(s)] for s in entry.stats)
            # owner is a candidate (open-slot passive): its own stats
            for c in self.cards:
                if c.card.id == owner:
                    return sum(c.stats[STAT_NAMES.index(s)] for s in entry.stats)
            return 0.0
        dim = target.kind
        value = target.value
        count = target.count or 0
        table = self._match.get((dim, value))
        for s_name in entry.stats:
            s = STAT_NAMES.index(s_name)
            vals = [st[s] for i, st in zip(chosen, stat_of_chosen)
                    if (self.cards[i].ctype == value if dim == "type" else value in self.cards[i].gens)]
            if table is not None and open_slots:
                vals.extend(self._top_values(table[s], lambda i, s=s: self.cards[i].stats[s],
                                             open_slots, excluded))
            vals.sort(reverse=True)
            total += sum(vals[:count] if count else vals)
        return total

    def _stat_entry_value(self, entry: _StatEntry, counts, open_slots, chosen, excluded,
                          stat_of_chosen) -> float:
        could, _must = self._fires(entry.gate, counts, open_slots)
        if not could:
            return 0.0
        rate = entry.rate
        if entry.override_gate is not None:
            oc, om = self._fires(entry.override_gate, counts, open_slots)
            if not oc:
                rate = entry.rate_base
            elif not om:
                rate = max(entry.rate, entry.rate_base)
        if rate <= 0.0:
            return 0.0
        return rate * self._recipient_stat_sum(entry, chosen, open_slots, excluded, stat_of_chosen)

    def ub(self, idx: Sequence[int], *, incumbent: float | None = None) -> float:
        """Upper bound on `S(T)` over every legal roster `T` whose members
        include the pool cards `idx` (distinct holomems). `incumbent` is unused
        by the arithmetic (kept for a future early-exit) — the value returned
        is the same with or without it."""
        self.stats_["bound_evals"] += 1
        chosen = tuple(idx)
        k = len(chosen)
        open_slots = SPECIAL_CUE_COUNT - k
        if open_slots < 0:
            raise ValueError("more than five members")
        cds = [self.cards[i] for i in chosen]
        excluded = frozenset(c.holomem for c in cds)
        if len(excluded) != k:
            raise ValueError("partial set repeats a holomem")
        counts: dict[tuple, int] = {}
        for c in cds:
            counts[("type", c.ctype)] = counts.get(("type", c.ctype), 0) + 1
            for g in c.gens:
                counts[("generation", g)] = counts.get(("generation", g), 0) + 1
        stat_of_chosen = [c.stats for c in cds]
        st = self.structure

        # ---- power ------------------------------------------------------------
        power = sum(c.base_sum for c in cds)
        power += self._top(self._by_base, lambda i: self.cards[i].base_sum, open_slots, excluded)
        # board: bench boards + the plans of the characters on the team (chosen +
        # leader + the open slots' best deltas), per recipient card
        chars_team = excluded | {self.leader_holomem}
        for i in chosen:
            power += self._board_power_of(i, chars_team, open_slots)
        if open_slots:
            best_new: dict[str, float] = {}
            for i, c in enumerate(self.cards):
                if c.holomem in excluded:
                    continue
                v = self._board_power_of(i, chars_team, open_slots)
                if v > best_new.get(c.holomem, 0.0):
                    best_new[c.holomem] = v
            power += sum(sorted(best_new.values(), reverse=True)[:open_slots])
        for c in cds:
            for e in c.stat_entries:
                power += self._stat_entry_value(e, counts, open_slots, chosen, excluded, stat_of_chosen)
        if open_slots:
            # each open slot's passive: the strongest candidate passives (distinct holomems)
            best: dict[str, float] = {}
            for i, c in enumerate(self.cards):
                if c.holomem in excluded or not c.stat_entries:
                    continue
                v = 0.0
                for e in c.stat_entries:
                    v += self._stat_entry_value(e, counts, open_slots, chosen, excluded, stat_of_chosen)
                if v > best.get(c.holomem, 0.0):
                    best[c.holomem] = v
            power += sum(sorted(best.values(), reverse=True)[:open_slots])
        if self.outfit_stat is not None:
            power += self._stat_entry_value(self.outfit_stat, counts, open_slots, chosen, excluded,
                                            stat_of_chosen)

        # ---- F (summed score-pool rates), rate-up, supports, SEU ---------------
        f_rate = sum(c.score_rates for c in cds) + self.outfit_score_rate
        f_rate += self._top(self._by_score_rate, lambda i: self.cards[i].score_rates, open_slots, excluded)
        rate_up = sum(c.rate_up for c in cds)
        rate_up += self._top(self._by_rate_up, lambda i: self.cards[i].rate_up, open_slots, excluded)
        t_hat = 0.0          # targeted supports, all reaching everyone
        b_pass = 0.0         # team-wide always-on supports: a maximum
        for c in cds:
            for sp in c.supports:
                if not self._fires(sp.gate, counts, open_slots)[0]:
                    continue
                if sp.targeted:
                    t_hat += sp.mag
                else:
                    b_pass = max(b_pass, sp.mag)
        if open_slots:
            t_hat += self._top(self._by_tsupport, lambda i: _max_support(self.cards[i], True),
                               open_slots, excluded)
            b_pass = max(b_pass, max(self._top_values(
                self._by_wsupport, lambda i: _max_support(self.cards[i], False), 1, excluded),
                default=0.0))
        for osp in self.outfit_supports:
            if not self._fires(osp.gate, counts, open_slots)[0]:
                continue
            if osp.targeted:
                t_hat += osp.mag
            else:
                b_pass = max(b_pass, osp.mag)
        mult0 = 1.0 + self.seu_hat + b_pass + t_hat

        # ---- ACTIVE terms and the note sweep ----------------------------------
        terms: list[tuple[float, float, tuple[tuple[int, int], ...]]] = []
        for c in cds:
            board_rate = self.board_rate.get(c.holomem, 0.0)
            for a in c.actives:
                could, _must = self._fires(a.gate, counts, open_slots)
                if not could:
                    continue
                mag = a.mag
                if a.override_gate is not None:
                    oc, om = self._fires(a.override_gate, counts, open_slots)
                    if not oc:
                        mag = a.mag_base
                    elif not om:
                        mag = max(a.mag, a.mag_base)
                if self.p_mode == "one":
                    p = 1.0
                else:
                    p = self.cfg.activation_rate_model.apply(a.tier, rate_up + board_rate)
                    if self.cfg.active_all_fire:
                        p = max(1.0, p)
                if mag > 0.0 and p > 0.0:
                    terms.append((mag, p, a.ranges))
        for _ in range(open_slots):
            if self.hyp_mag > 0.0:
                if self.p_mode == "one":
                    p = 1.0
                else:
                    p = self.cfg.activation_rate_model.apply(self.hyp_tier, rate_up + self.hyp_board_rate)
                    if self.cfg.active_all_fire:
                        p = max(1.0, p)
                terms.append((self.hyp_mag, p, self.hyp_ranges))
        # SPECIAL candidates: (mag, ranges by slot)
        sp_cands: list[tuple[float, tuple[tuple[int, int], ...]]] = []
        for c in cds:
            for sp in c.specials:
                if self._fires(sp.gate, counts, open_slots)[0] and sp.mag > 0.0:
                    sp_cands.append((sp.mag, sp.ranges_by_slot))
        if open_slots:
            sp_cands.extend(self.hyp_specials)

        # sweep: ACTIVE window edges (+bit / -bit) and every SPECIAL cue-window
        # edge (0) in note-index order; between two edges the live set is
        # constant, so E[max] is looked up once per run and the run's notes are
        # summed by distinct weight (`_sum_ceil`)
        n_notes = len(st.times)
        events: list[tuple[int, int]] = [(n_notes, 0)]
        for ti, (_mag, _p, ranges) in enumerate(terms):
            bit = 1 << ti
            for lo, hi in ranges:
                events.append((lo, bit))
                events.append((hi, -bit))
        for _mag, rbs in sp_cands:
            for lo, hi in rbs:
                if lo < hi:
                    events.append((lo, 0))
                    events.append((hi, 0))
        events.sort()
        constant = power * self.k_chart * (1.0 + BOUND_SLACK)
        base_mult = 1.0 + f_rate
        runs: list[tuple[int, int, float, float]] = []
        base_total = 0.0
        local: dict[int, float] = {}
        emax_cache = self._emax_cache
        sum_ceil = self._sum_ceil
        pos = 0
        live = 0
        i = 0
        n_ev = len(events)
        while i < n_ev:
            p_ = events[i][0]
            if p_ > pos:
                e = local.get(live)
                if e is None:
                    key = tuple(sorted(((terms[t][0], terms[t][1]) for t in range(len(terms))
                                        if live >> t & 1), reverse=True))
                    e = emax_cache.get(key)
                    if e is None:
                        e = self._emax(key)
                        emax_cache[key] = e
                    local[live] = e
                rb = sum_ceil(pos, p_, constant * (base_mult + mult0 * e))
                runs.append((pos, p_, e, rb))
                base_total += rb
                pos = p_
            while i < n_ev and events[i][0] == p_:
                d = events[i][1]
                if d > 0:
                    live |= d
                elif d < 0:
                    live &= ~(-d)
                i += 1
        # SPECIAL per-slot maxima
        special_total = 0.0
        for slot in range(SPECIAL_CUE_COUNT):
            best = 0.0
            for mag, rbs in sp_cands:
                lo, hi = rbs[slot]
                if lo >= hi:
                    continue
                d = 0.0
                for a, b, e, rb in runs:
                    if b <= lo:
                        continue
                    if a >= hi:
                        break
                    if e <= 0.0:
                        continue
                    d += sum_ceil(a, b, constant * (base_mult + (mult0 + mag) * e)) - rb
                if d > best:
                    best = d
            special_total += best
        return base_total + special_total

    def _sum_ceil(self, a: int, b: int, factor: float) -> float:
        """`Σ_{n in [a,b)} ceil(factor · w_n)` over the run's distinct weights;
        the run's `(w, count)` list is memoised — runs recur across sets
        because their edges are the pool cards' own windows."""
        items = self._run_items.get((a, b))
        if items is None:
            st = self.structure
            items = tuple((w, st.prefix[wi][b] - st.prefix[wi][a])
                          for wi, w in enumerate(st.distinct_w)
                          if st.prefix[wi][b] - st.prefix[wi][a])
            self._run_items[(a, b)] = items
        total = 0.0
        for w, cnt in items:
            total += cnt * math.ceil(factor * w)
        return total

    def _emax(self, key: tuple[tuple[float, float], ...]) -> float:
        if not key:
            return 0.0
        return self.cfg.active_stacking_model.combine(
            [("ACTIVE", mag, p, False) for mag, p in key])


def _covers(ranges: Sequence[tuple[int, int]], n: int) -> bool:
    for lo, hi in ranges:
        if lo <= n < hi:
            return True
        if lo > n:
            return False
    return False


def _merge_ranges(ranges: Iterable[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    out: list[list[int]] = []
    for lo, hi in sorted(ranges):
        if lo >= hi:
            continue
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return tuple((a, b) for a, b in out)


def _max_support(c: _CardData, targeted: bool) -> float:
    return max((s.mag for s in c.supports if s.targeted == targeted), default=0.0)


def _scaled_value(effect, multiplier: float) -> float:
    """`BoardEffect.scaled(multiplier).value` without allocating: ceiled for
    `stat` when a Connect multiplier applies, the plain product otherwise."""
    if multiplier == 1.0:
        return effect.value
    value = effect.value * multiplier
    if effect.kind == "stat":
        return math.ceil(value)
    return value


class _UptimeBoard:
    """The one `cfg.board` reader `_score_active` needs for the rate-up fold:
    `uptime_for(holomem) -> (activation_rate_up, cooldown_shorten)`, at the
    largest rate and the largest cooldown any member board can carry."""

    def __init__(self, rate: Mapping[str, float], cooldowns: Mapping[str, list]) -> None:
        self._rate = dict(rate)
        self._cd = {h: max(max(t) for t in ts) for h, ts in cooldowns.items() if ts}

    def uptime_for(self, holomem: str) -> tuple[float, float]:
        return self._rate.get(holomem, 0.0), self._cd.get(holomem, 0.0)


# ---------------------------------------------------------------------------
# Enumeration helpers
# ---------------------------------------------------------------------------
def legal_rosters(holomems: Sequence[str], k: int = SPECIAL_CUE_COUNT):
    """Lexicographic `k`-combinations of pool indices with distinct holomems —
    the exhaustive route's own order."""
    n = len(holomems)
    for c in itertools.combinations(range(n), k):
        if len({holomems[i] for i in c}) == k:
            yield c


def local_search_1swap(
    holomems: Sequence[str], start: Sequence[int],
    batch_score: Callable[[Sequence[tuple[int, ...]]], Sequence[float]], *,
    start_score: float | None = None, max_passes: int = 20,
) -> tuple[float, tuple[int, ...], int, int]:
    """Best-improvement 1-swap hill-climb over legal rosters (5 pool indices,
    distinct holomems), the HEURISTIC incumbent a branch-and-bound row starts
    from. Each pass scores every legal single replacement of the current
    roster in ONE `batch_score` call (≤ 5·n rosters) and moves to the best;
    stops at a local optimum or after `max_passes`. Returns
    `(score, roster, passes, rosters_scored)`; the roster is sorted."""
    cur = tuple(sorted(start))
    k = len(cur)
    passes = scored = 0
    if start_score is None:
        start_score = batch_score([cur])[0]
        scored += 1
    best = float(start_score)
    n = len(holomems)
    while passes < max_passes:
        passes += 1
        cands: list[tuple[int, ...]] = []
        for slot in range(k):
            others = cur[:slot] + cur[slot + 1:]
            used = {holomems[i] for i in others}
            for j in range(n):
                if j in cur or holomems[j] in used:
                    continue
                cands.append(tuple(sorted(others + (j,))))
        if not cands:
            break
        scores = batch_score(cands)
        scored += len(cands)
        i_best = max(range(len(cands)), key=lambda i: scores[i])
        if scores[i_best] <= best:
            break
        best, cur = float(scores[i_best]), cands[i_best]
    return best, cur, passes, scored


def _children(prefix: tuple[int, ...], holomems: Sequence[str]) -> list[int]:
    """Next pool indices that extend `prefix` (index > last, holomem unused)."""
    used = {holomems[i] for i in prefix}
    start = prefix[-1] + 1 if prefix else 0
    return [j for j in range(start, len(holomems)) if holomems[j] not in used]


# ---------------------------------------------------------------------------
# Branch and bound
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class BnBResult:
    status: str                              # EXACT | BOUNDED
    best_score: float
    best_idx: tuple[int, ...] | None
    leaves_evaluated: int
    nodes_pruned: int                        # internal nodes cut (their leaves never scored)
    leaves_pruned: int                       # rosters excluded by those cuts
    leaves_bound_pruned: int                 # rosters cut by their own 5-card bound
    bound_evals: int
    seconds: float
    upper_bound: float | None                # BOUNDED: max bound over the unexplored remainder
    top: list[tuple[float, tuple[int, ...]]]
    prefixes_done: int
    prefixes_total: int


def _count_completions(prefix: tuple[int, ...], holomems: Sequence[str]) -> int:
    """How many legal 5-rosters extend `prefix`: choose `5-k` more cards with
    index above the last chosen and pairwise-distinct, unused holomems — the
    elementary symmetric polynomial `e_{5-k}` of the per-holomem card counts
    among the eligible candidates."""
    need = SPECIAL_CUE_COUNT - len(prefix)
    if need == 0:
        return 1
    mult: dict[str, int] = {}
    for j in _children(prefix, holomems):
        mult[holomems[j]] = mult.get(holomems[j], 0) + 1
    e = [1] + [0] * need
    for m in mult.values():
        for r in range(need, 0, -1):
            e[r] += e[r - 1] * m
    return e[need]


class _Search:
    """One prefix's depth-first search: bounds every child, prunes those that
    cannot beat the running incumbent, and at the leaf level bounds each
    roster (`ub` at 5 cards, ~0.3 ms) before scoring it with the leaf
    (~3.7 ms) — a roster-level filter, applied only where the partial-set
    bounds could not decide."""

    def __init__(self, bound: RowBound, holomems: Sequence[str], incumbent: float,
                 leaf_fn: Callable[[tuple[int, ...]], float], order: str = "bound",
                 top_k: int = 20) -> None:
        self.bound = bound
        self.holomems = holomems
        self.incumbent = incumbent
        self.leaf_fn = leaf_fn
        self.order = order
        self.top_k = top_k
        self.best: tuple[float, tuple[int, ...] | None] = (float("-inf"), None)
        self.top: list[tuple[float, tuple[int, ...]]] = []
        self.leaves = 0
        self.fsum = 0.0
        self.bound_evals = 0
        self.nodes_pruned = 0
        self.leaves_pruned = 0
        self.leaves_bound_pruned = 0

    def run(self, prefix: tuple[int, ...]) -> None:
        kids = _children(prefix, self.holomems)
        depth = len(prefix) + 1
        if depth == SPECIAL_CUE_COUNT:
            for j in kids:
                leaf = prefix + (j,)
                ub = self.bound.ub(leaf)
                self.bound_evals += 1
                if ub <= self.incumbent:
                    self.leaves_bound_pruned += 1
                    continue
                score = self.leaf_fn(leaf)
                self.leaves += 1
                self.fsum += score
                if score > self.best[0]:
                    self.best = (score, leaf)
                if score > self.incumbent:
                    self.incumbent = score
                self.top.append((score, leaf))
                if len(self.top) > 4 * self.top_k:
                    self.top.sort(key=lambda t: -t[0])
                    del self.top[self.top_k:]
            return
        scored = []
        for j in kids:
            node = prefix + (j,)
            ub = self.bound.ub(node)
            self.bound_evals += 1
            if ub <= self.incumbent:
                self.nodes_pruned += 1
                self.leaves_pruned += _count_completions(node, self.holomems)
                continue
            scored.append((ub, node))
        if self.order == "bound":
            scored.sort(key=lambda t: -t[0])
        elif self.order == "coverage":
            scored.sort(key=lambda t: -self.bound.cards[t[1][-1]].coverage)
        elif self.order == "structure":
            # song structure: prefer the child whose ACTIVE windows cover the
            # most note weight NOT already covered by the chosen members'
            # windows (E[max] discounts synchronised actives)
            b = self.bound
            scored.sort(key=lambda t: -(b.cards[t[1][-1]].coverage
                                        - max((b.overlap(t[1][-1], i) for i in prefix), default=0.0)))
        # "index": the enumeration order, unsorted
        for ub, node in scored:
            if ub <= self.incumbent:      # the incumbent may have moved since
                self.nodes_pruned += 1
                self.leaves_pruned += _count_completions(node, self.holomems)
                continue
            self.run(node)


# --- worker side -------------------------------------------------------------
_BOUND: RowBound | None = None
_HOLOMEMS: tuple[str, ...] = ()
_LEADER_ID: str = ""
_POOL_IDS: tuple[str, ...] = ()


def _init_bnb_worker(ctx: pr.RefineContext, bound: RowBound, leader_id: str,
                     pool_ids: tuple[str, ...]) -> None:
    global _BOUND, _HOLOMEMS, _LEADER_ID, _POOL_IDS
    pr._init_worker(ctx)
    _BOUND = bound
    _HOLOMEMS = tuple(c.holomem for c in bound.cards)
    _LEADER_ID = leader_id
    _POOL_IDS = pool_ids


def _evaluate_leaf(idx: tuple[int, ...]) -> float:
    assert pr._CTX is not None
    ids = (_LEADER_ID, tuple(_POOL_IDS[i] for i in idx))
    return pr.evaluate(pr._CTX, pr._BY_ID, pr._tree_for, pr._budget_for, ids,
                       plan_cache=pr._worker_plan_cache())


def _prefix_task(args: tuple, leaf_fn: Callable[[tuple[int, ...]], float] | None = None) -> dict:
    """One prefix: DFS with the bound, leaves bounded then scored, returning
    the local best, the counts and the top-K."""
    prefix, incumbent, top_k, order = args
    assert _BOUND is not None
    t0 = time.perf_counter()
    search = _Search(_BOUND, _HOLOMEMS, incumbent, leaf_fn or _evaluate_leaf, order, top_k)
    search.run(tuple(prefix))
    top = sorted(search.top, key=lambda t: -t[0])[:top_k]
    return {"prefix": list(prefix), "best": search.best[0],
            "best_idx": (list(search.best[1]) if search.best[1] else None),
            "leaves": search.leaves, "fsum": search.fsum,
            "top": [[sc, list(i)] for sc, i in top],
            "seconds": time.perf_counter() - t0,
            "bound_evals": search.bound_evals, "nodes_pruned": search.nodes_pruned,
            "leaves_pruned": search.leaves_pruned,
            "leaves_bound_pruned": search.leaves_bound_pruned}


def branch_and_bound(
    ctx: pr.RefineContext, bound: RowBound, leader: Card, pool: Sequence[Card], *,
    incumbent_score: float, incumbent_idx: tuple[int, ...] | None = None,
    workers: int | None = None, top_k: int = 20, order: str = "bound",
    resume_path: str | os.PathLike | None = None,
    resume_sink: Callable[[dict], None] | None = None, deadline: float | None = None,
    log: Callable[[str], None] | None = None, prefix_depth: int = 2,
    leaf_fn: Callable[[tuple[int, ...]], float] | None = None,
    max_prefixes: int | None = None,
) -> BnBResult:
    """Prove (or bound) the row: leader fixed, members = 5-sets of distinct
    holomems from `pool`. Depth-1/2 prefixes are bounded in the parent; each
    surviving depth-`prefix_depth` prefix is one worker task (`_prefix_task`).
    `resume_path` (JSONL, one line per finished prefix — READ here) plus
    `resume_sink` (called with each finished prefix's row; the CALLER writes
    it — nothing under `hbuilder` opens a file for writing) make the
    run resumable; `deadline` (seconds) stops between prefixes with status BOUNDED
    and the max bound over the unexplored prefixes as `upper_bound`. `leaf_fn`
    (in-process runs only, `workers=1`) replaces the plan-cache leaf — for
    tests on synthetic pools; with a pool it must be `None`."""
    if leaf_fn is not None and (workers or 1) > 1:
        raise ValueError("leaf_fn is for in-process runs (workers=1)")
    say = log or (lambda s: None)
    holomems = tuple(c.member for c in pool)
    pool_ids = tuple(c.id for c in pool)
    workers = default_workers() if workers is None else max(1, workers)
    t0 = time.perf_counter()
    incumbent = float(incumbent_score)
    best_idx = tuple(incumbent_idx) if incumbent_idx is not None else None
    stats = {"bound_evals": 0, "nodes_pruned": 0, "leaves_pruned": 0, "leaves_bound_pruned": 0}

    # parent-side: depth-1 and depth-2 prefixes
    prefixes: list[tuple[float, tuple[int, ...]]] = []
    frontier: list[tuple[int, ...]] = [()]
    for depth in range(1, prefix_depth + 1):
        nxt: list[tuple[float, tuple[int, ...]]] = []
        for pre in frontier:
            for j in _children(pre, holomems):
                node = pre + (j,)
                ub = bound.ub(node)
                stats["bound_evals"] += 1
                if ub <= incumbent:
                    stats["nodes_pruned"] += 1
                    stats["leaves_pruned"] += _count_completions(node, holomems)
                    continue
                nxt.append((ub, node))
        nxt.sort(key=lambda t: -t[0])
        frontier = [n for _u, n in nxt]
        prefixes = nxt
        say(f"depth {depth}: {len(nxt)} prefixes survive of "
            f"{len(nxt) + (stats['nodes_pruned'])} bounded so far; "
            f"{stats['leaves_pruned']:,} rosters pruned; {time.perf_counter() - t0:.1f}s")
    n_prefixes = len(prefixes)

    done: dict[tuple[int, ...], dict] = {}
    if resume_path is not None and os.path.isfile(resume_path):
        with open(resume_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    row = json.loads(line)
                    if row.get("kind") == "prefix":
                        done[tuple(row["prefix"])] = row

    leaves_evaluated = 0
    top: list[tuple[float, tuple[int, ...]]] = []
    for row in done.values():
        leaves_evaluated += row["leaves"]
        stats["bound_evals"] += row["bound_evals"]
        stats["nodes_pruned"] += row["nodes_pruned"]
        stats["leaves_pruned"] += row["leaves_pruned"]
        stats["leaves_bound_pruned"] += row.get("leaves_bound_pruned", 0)
        for s, i in row["top"]:
            top.append((s, tuple(i)))
        if row["best_idx"] is not None and row["best"] > incumbent:
            incumbent, best_idx = row["best"], tuple(row["best_idx"])
    all_todo = [(ub, p) for ub, p in prefixes if p not in done]
    todo = all_todo if max_prefixes is None else all_todo[:max_prefixes]
    status = "EXACT"
    unexplored_ub: float | None = None

    def _task_iter():
        for ub, p in todo:
            yield (p, incumbent, top_k, order)

    def _absorb(row: dict) -> None:
        nonlocal incumbent, best_idx, leaves_evaluated
        p = tuple(row["prefix"])
        done[p] = row
        leaves_evaluated += row["leaves"]
        stats["bound_evals"] += row["bound_evals"]
        stats["nodes_pruned"] += row["nodes_pruned"]
        stats["leaves_pruned"] += row["leaves_pruned"]
        stats["leaves_bound_pruned"] += row.get("leaves_bound_pruned", 0)
        for s, i in row["top"]:
            top.append((s, tuple(i)))
        if row["best_idx"] is not None and row["best"] > incumbent:
            incumbent, best_idx = row["best"], tuple(row["best_idx"])
        if resume_sink is not None:
            resume_sink({"kind": "prefix", **row})

    n_done_run = 0
    if workers > 1 and todo:
        mp = multiprocessing.get_context("spawn")
        with mp.Pool(processes=workers, initializer=_init_bnb_worker,
                     initargs=(ctx, bound, leader.id, pool_ids)) as p_:
            for row in p_.imap_unordered(_prefix_task, _task_iter(), chunksize=1):
                _absorb(row)
                n_done_run += 1
                if n_done_run % 25 == 0 or n_done_run == len(todo):
                    say(f"prefix {n_done_run}/{len(todo)}: leaves {leaves_evaluated:,} "
                        f"pruned {stats['leaves_pruned']:,} best {incumbent:,.0f} "
                        f"{time.perf_counter() - t0:.1f}s")
                if deadline is not None and time.perf_counter() - t0 > deadline:
                    status = "BOUNDED"
                    p_.terminate()
                    break
    elif todo:
        _init_bnb_worker(ctx, bound, leader.id, pool_ids)
        for ub, p in todo:
            row = _prefix_task((p, incumbent, top_k, order), leaf_fn=leaf_fn)
            _absorb(row)
            n_done_run += 1
            if deadline is not None and time.perf_counter() - t0 > deadline:
                status = "BOUNDED"
                break
    remaining = [(u, q) for u, q in all_todo if q not in done]
    if remaining:
        status = "BOUNDED"
        unexplored_ub = max(u for u, _q in remaining)
    top.sort(key=lambda t: (-t[0], t[1]))
    seen = set()
    uniq = []
    for s, i in top:
        if i not in seen:
            seen.add(i)
            uniq.append((s, i))
    return BnBResult(
        status=status, best_score=incumbent, best_idx=best_idx,
        leaves_evaluated=leaves_evaluated, nodes_pruned=stats["nodes_pruned"],
        leaves_pruned=stats["leaves_pruned"], leaves_bound_pruned=stats["leaves_bound_pruned"],
        bound_evals=stats["bound_evals"],
        seconds=time.perf_counter() - t0, upper_bound=unexplored_ub,
        top=uniq[:top_k], prefixes_done=len(done), prefixes_total=n_prefixes)
