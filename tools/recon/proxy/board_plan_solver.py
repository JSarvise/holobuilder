"""Exact Full-plan board optimiser via HiGHS — a drop-in for `optimised_board`.

`optimised_board` shells out to `tree_optimise --plan` (the beam, ~60-90s) and reads
the `{chr: {nodes, slots}}` it dumps. This produces the SAME dict IN-PROCESS by solving
the board as one integer program — provably optimal, sub-second.

The beam is a multi-dimensional knapsack over BOTH each character's points AND the
account's shared cube/hexa wallet, with per-branch scope (RED pays on the leader, BLUE
on a member, GREEN on any board, YELLOW on a singer in song mode). This models all of
that as ONE joint MILP over the six team boards:

* one binary per (character, node); the root forced on, level-gated nodes excluded;
* per-character point budget and per-character connectivity (single-commodity flow);
* the shared cube/hexa wallet as 8 account-wide constraints (dropped when materials
  are infinite);
* objective = the per-node values from `tree_optimise._branch_values`, scoped per
  branch exactly as the beam's `_move_in_scope` does.

Sockets are placed afterwards by `socket_optimise` on the solved board, the same as the
beam path. Server-side only; the solver library is not imported by `src/hbuilder`.
"""
from __future__ import annotations

import itertools
import json
import re

import numpy as np
import tempfile
from pathlib import Path

import tree_optimise as to


# What every plan of one account shares, kept per process (rebuilding the Oracle, re-reading
# the account and re-building the ~48 bench boards' MILP block on every solve costs ~15-24 %
# end to end; kept, the plans are byte-identical):
# `_ACCOUNT` the Oracle and the account's graphs / prices / owned nodes / wallet, `_BLOCKS`
# each bench board's MILP block (columns + rows). Both are keyed on `_account_stamp()`
# and emptied when it moves (a refresh).
_ACCOUNT: dict = {}
_BLOCKS: dict = {}
# every file the Oracle, `_prices` and `balance_from_disk` read
_STAMPED = ("data/cards.json", "data/owned.json", "state/collection.json", "config.json",
            "tools/recon/proxy/dumps/skilltree.json", "tools/recon/proxy/dumps/master_SkillTreeNode.json",
            "tools/recon/proxy/dumps/card_caps.json", "tools/recon/proxy/dumps/charts.json",
            "tools/recon/proxy/dumps/board_state.json", "tools/recon/proxy/dumps/inventory.json",
            "tools/recon/proxy/dumps/card_level_curve.json", "tools/recon/proxy/dumps/account_snapshot_main.json",
            "tools/recon/proxy/dumps/skill_ladder.json", "tools/recon/proxy/dumps/card_params.json",
            "tools/recon/proxy/dumps/master_index.json",
            "tools/recon/proxy/dumps/master_refetched/SkillTreeNode.db")
# the HiGHS model status of each MILP of the LAST `solve_team_board` call in this process:
# a plan any of whose solves stopped on the time limit depends on the machine's load, and
# `materials_plan` never hands such a plan to another search
LAST_STATUSES: list = []


class PlanStopped(Exception):
    """`solve_team_board(should_stop=...)` answered True before a MILP: the plan was
    abandoned (a cancelled search), nothing returned."""


def _account_stamp() -> tuple:
    """What the plan's account inputs were read from: `_STAMPED`'s mtimes, the game's
    own SkillTreeNode copy (`master_cache`'s second candidate) and the newest *.mitm
    (`balance_from_disk`'s fallback)."""
    import os  # noqa: PLC0415
    out = []
    for rel in _STAMPED:
        p = to.REPO / rel
        out.append(p.stat().st_mtime_ns if p.exists() else 0)
    # the game's own SkillTreeNode copy (master_cache's 2nd candidate) and the *.mitm
    # fallback of balance_from_disk
    try:
        import master_cache as _mc  # noqa: PLC0415
        gp = _mc._game_cache_path("SkillTreeNode")
        out.append(os.stat(gp).st_mtime_ns if gp else 0)
    except Exception:  # noqa: BLE001
        out.append(-1)
    out.append(max((p.stat().st_mtime_ns for p in to.DUMPS.glob("*.mitm")), default=0))
    return tuple(out)


def _account_inputs(oracle) -> tuple:
    """`(graphs, prices, owned, balance, wallet_ref, points)` of the account, read as the
    plan always read them (outside the Oracle's `try`: a read failure raises, as before)."""
    graphs = to._graphs(oracle.skilltree)
    prices = to._prices()
    owned = to._owned_nodes()
    balance, _src = to.balance_from_disk()
    wallet_ref, points, _start, _levels = to.respec_budget(owned, prices, balance)
    return graphs, prices, owned, balance, wallet_ref, points


# The board's connectivity without a flow. A single-commodity flow with big-M capacity N
# leaves a 6-7 % root gap on a board valuable on all four
# branches (a Nene-led plan: 24-27 s of MILP), since its LP relaxation reaches deep nodes
# through path nodes at x = 1/N. Instead: every node unreachable from the root is 0; a
# 2-edge-connected component hung off a bridge is entered only through its entry node
# (x[entry] <= x[attach], x[w] <= x[entry]); inside a component, every minimal vertex
# separator S between its entry and v gives x[v] <= sum x[S]. A rooted node set is
# connected iff all of these hold (the bridge tree means a path into a component never
# leaves it), so the feasible boards are the flow's (checked on 60,000 pruned sets and
# 198,784 exhaustive subsets, 0 discrepancies) and the root gap is ~0.23 % (3.7-4.0 s).
# The MILP optimum is the same; among exact ties the solver may return another board.
_CONNECTIVITY: dict = {}


def _reach(adj: dict, start: str, banned: frozenset = frozenset()) -> set:
    seen = {start}
    stack = [start]
    while stack:
        a = stack.pop()
        for b in adj[a]:
            if b not in banned and b not in seen:
                seen.add(b)
                stack.append(b)
    return seen


def _min_separators(cadj: dict, e: str, v: str) -> list[list[str]]:
    """Every minimal e-v vertex separator of the component `cadj`: S = N(D) for a
    connected D holding v (and nothing of N[e]) that is v's whole side of G - S, with e
    on the other."""
    out = set()
    forbidden = {e} | set(cadj[e])
    seen = set()
    stack = [frozenset([v])]
    while stack:
        d = stack.pop()
        if d in seen:
            continue
        seen.add(d)
        s = frozenset(b for a in d for b in cadj[a] if b not in d)
        if e not in s:
            rest = {n: [b for b in cadj[n] if b not in s] for n in cadj if n not in s}
            side = _reach(rest, e)
            if v not in side and all(any(b in side for b in cadj[t]) for t in s) \
                    and _reach(rest, v) == set(d):
                out.add(s)
        for a in d:
            for b in cadj[a]:
                if b not in d and b not in forbidden:
                    stack.append(d | {b})
    return sorted(sorted(s) for s in out)


def _connectivity(g: dict, nodes: list) -> tuple[list, list, list]:
    """`(unreachable, precedences, separators)` of the board graph `g` over `nodes`:
    nodes forced to 0, `(w, u)` pairs for x[w] <= x[u], and `(v, S)` for
    x[v] <= sum x[S] -- in a fixed order (the rows' order). Kept per (graph, node list)."""
    key = (id(g), tuple(nodes))
    held = _CONNECTIVITY.get(key)
    if held is not None and held[0] is g:
        return held[1]
    root = to.ROOT
    keep = set(nodes)
    adj = {n: sorted(b for b in g.get(n, ()) if b in keep) for n in nodes}
    reach = _reach(adj, root)
    radj = {n: [b for b in adj[n] if b in reach] for n in reach}
    # bridges (iterative Tarjan from the root)
    tin, low, bridges = {root: 0}, {root: 0}, set()
    t = 1
    stack = [(root, None, iter(radj[root]))]
    while stack:
        u, p, it = stack[-1]
        for w in it:
            if w == p:
                continue
            if w in tin:
                low[u] = min(low[u], tin[w])
            else:
                tin[w] = low[w] = t
                t += 1
                stack.append((w, u, iter(radj[w])))
                break
        else:
            stack.pop()
            if p is not None:
                low[p] = min(low[p], low[u])
                if low[u] > tin[p]:
                    bridges.add(frozenset((p, u)))
    # 2-edge-connected components, and the bridge tree from the root's
    comp: dict = {}
    for s0 in sorted(reach):
        if s0 in comp:
            continue
        comp[s0] = s0
        todo = [s0]
        while todo:
            a = todo.pop()
            for b in radj[a]:
                if b not in comp and frozenset((a, b)) not in bridges:
                    comp[b] = s0
                    todo.append(b)
    members: dict = {}
    for n, c in comp.items():
        members.setdefault(c, []).append(n)
    entry, attach = {comp[root]: root}, {}
    order, seen_c = [comp[root]], {comp[root]}
    k = 0
    while k < len(order):
        c = order[k]
        k += 1
        for a in sorted(members[c]):
            for b in radj[a]:
                if frozenset((a, b)) in bridges and comp[b] not in seen_c:
                    seen_c.add(comp[b])
                    order.append(comp[b])
                    entry[comp[b]], attach[comp[b]] = b, a
    unreachable = [n for n in nodes if n not in reach]
    precedences, separators = [], []
    for c in order:
        e, mem = entry[c], sorted(members[c])
        if c in attach:
            precedences.append((e, attach[c]))
        if len(mem) < 2:
            continue
        for w in mem:
            if w != e and e != root:
                precedences.append((w, e))
        cn = set(mem)
        cadj = {n: [b for b in radj[n] if b in cn] for n in cn}
        for v in mem:
            if v == e or e in cadj[v]:
                continue
            for s in _min_separators(cadj, e, v):
                separators.append((v, tuple(s)))
    out = (unreachable, precedences, separators)
    _CONNECTIVITY[key] = (g, out)
    return out


def _scope_value(node: str, chr_id: str, values: dict, *, leader_chr: str,
                 member_chrs: set, song: bool, singers: frozenset) -> float:
    """The node's value ON THIS character's board, mirroring `_move_in_scope`:
    RED only on the leader, BLUE only on a member, GREEN on any board (it stacks),
    YELLOW only on a singer in song mode (bench-only in general mode, so 0 on a team
    board). `_branch_values` already dropped scoreless nodes, so a missing key is 0."""
    v = values.get(node)
    if not v:
        return 0.0
    branch = node[0]
    if branch == "R":
        return v if chr_id == leader_chr else 0.0
    if branch == "B":
        return v if chr_id in member_chrs else 0.0
    if branch == "G":
        return v
    if branch == "Y":
        return v if (song and chr_id in singers) else 0.0
    return 0.0


# objective weight of a secondary target: above any node's score value (the whole
# board is worth a few hundred thousand), so a reachable target is always taken
TARGET_BONUS = 1.0e9
# objective weight of keeping an owned node / not buying a new one: far below any
# score a node adds (the smallest priced node is worth tens), so it only decides ties
TIE_BREAK = 1.0
MIP_REL_GAP = 1.0e-7


# The type-20 limit (`hbuilder.domain.board.GROUPING_ALL_CAP`): the grouping greens
# a card receives, summed over every source board, add at most 900 to each stat.
GROUPING_ALL_CAP = 900


def _grouping_terms(oracle, leader_id: str, member_ids, boards, owned, graphs) -> dict:
    """`{grouping: {"slope", "amounts": {(chr, node): value}}}` for every grouping
    whose generation greens move this team's score.

    `amounts` are the G-008/011/021 values each board of the grouping gives
    (`skilltree.json`'s per-character override); `slope` is the team score per
    point of grouping all-stats, measured on the scorer: the account's boards with
    every chain of that grouping taken off, then one board's whole chain put back
    (below the cap, so the score moves linearly). A grouping no member is in
    measures 0 and is left out. A card in two groupings shares one cap in game;
    here each grouping is capped alone (expected to be rare; the final score is exact)."""
    nodes = oracle.skilltree.get("nodes", {})
    groups: dict = {}
    for cid in boards:
        g = graphs.get(cid) or {}
        for n in sorted(to.PER_CHARACTER_GREEN):
            row = (nodes.get(n, {}).get("chr") or {}).get(cid)
            if not row or n not in g or not row.get("v"):
                continue
            groups.setdefault(row.get("tgt", ""), {})[(cid, n)] = float(row["v"])
    if not groups:
        return {}
    base = {c: set(owned.get(c) or ()) | {to.ROOT} for c in graphs}

    def score(state):
        return oracle.score_team(leader_id, member_ids,
                                 oracle.state_from({c: sorted(v) for c, v in state.items()}))

    out = {}
    for tgt, amounts in sorted(groups.items()):
        stripped = {c: v - {n for (cc, n) in amounts if cc == c} for c, v in base.items()}
        by_board: dict = {}
        for (cid, n), a in amounts.items():
            by_board[cid] = by_board.get(cid, 0.0) + a
        host = max(sorted(by_board), key=lambda c: by_board[c])
        chain = by_board[host]
        trial = dict(stripped)
        trial[host] = stripped[host] | {n for (cc, n) in amounts if cc == host}
        gain = score(trial) - score(stripped)
        if gain > 0 and chain > 0:
            out[tgt] = {"slope": gain / min(chain, GROUPING_ALL_CAP), "amounts": amounts}
    return out


# `hbuilder.domain.board.SONG_ALL_CAP_PERMIL`: a song's yellow score bonus, summed
# over its singers' boards, counts up to 100 permil (`song_score_bonus_for`)
SONG_YELLOW_CAP = 100
_YELLOW_PERMIL = re.compile(r"singer_type-(\d+)-skill_tree_effect_passive")


def _yellow_amounts(oracle, singers, singer_type: str, boards, graphs) -> dict:
    """`{(singer, node): permil}`: what each singer board's Y nodes of `singer_type` give
    a song sung by `singers` (the permil is in the node's effect id)."""
    nodes = oracle.skilltree.get("nodes", {})
    amounts: dict = {}
    for cid in sorted(set(singers) & set(boards)):
        for n in sorted(graphs.get(cid) or ()):
            if n[0] != "Y":
                continue
            eff = (nodes.get(n, {}).get("chr") or {}).get(cid) or nodes.get(n, {}).get("e") or {}
            m = _YELLOW_PERMIL.search(eff.get("t") or "")
            if m and eff.get("tr") == singer_type and int(m.group(1)) > 0:
                amounts[(cid, n)] = float(m.group(1))
    return amounts


def _pool_yellow_terms(oracle, song_pool, boards, graphs) -> list:
    """A Highlight pool's yellows (how much yellow the pool's event units need before
    returns diminish): one
    `{"slope", "amounts"}` per pool song, each capped at SONG_YELLOW_CAP permil like the
    single song's. `song_pool`: `[{"singers", "singer_type", "slope"}]` from the caller,
    `slope` the team score per permil of that song's bonus already weighted by its share
    of the pool (`row_detail.pool_yellow`). A singer's yellow counts on every pool song she
    sings (team or bench), and a song past its cap gains nothing more."""
    out = []
    for song in song_pool or ():
        amounts = _yellow_amounts(oracle, song.get("singers") or (), song.get("singer_type") or "",
                                  boards, graphs)
        if amounts and float(song.get("slope") or 0) > 0:
            out.append({"slope": float(song["slope"]), "amounts": amounts})
    return out


def _yellow_terms(oracle, leader_id: str, member_ids, boards, owned, graphs) -> dict | None:
    """`{"slope", "amounts": {(singer, node): permil}}` for the song's yellow score
    bonus, or None off a song / when it does not move the score.

    `amounts` are the permil each singer board's Y nodes of the song's singer type
    give (in the effect id); `slope` is the team score per permil, measured like
    `_grouping_terms`: every singer's yellow taken off, then the largest board's
    put back (below the cap). Linear per node, a group song with 4 singers at 40
    permil a board would read 160 where the game counts 100 (e.g. m0342)."""
    chart = getattr(oracle, "_chart", None)
    if chart is None or not getattr(chart, "singer_type", ""):
        return None
    amounts = _yellow_amounts(oracle, chart.singers, chart.singer_type, boards, graphs)
    if not amounts:
        return None
    stripped = {c: (set(owned.get(c) or ()) | {to.ROOT}) - {n for (cc, n) in amounts if cc == c}
                for c in graphs}
    by_board: dict = {}
    for (cid, _n), a in amounts.items():
        by_board[cid] = by_board.get(cid, 0.0) + a
    host = max(sorted(by_board), key=lambda c: by_board[c])
    chain = min(by_board[host], SONG_YELLOW_CAP)
    trial = dict(stripped)
    trial[host] = stripped[host] | {n for (cc, n) in amounts if cc == host}

    def score(state):
        return oracle.score_team(leader_id, member_ids,
                                 oracle.state_from({c: sorted(v) for c, v in state.items()}))
    gain = score(trial) - score(stripped)
    if gain <= 0:
        return None
    return {"slope": gain / chain, "amounts": amounts}


# Act-level Gauss-Seidel: at most this many one-board rounds (one table pass + one
# MILP each), stopping when no board's best level beats its current by ACT_MIN_GAIN
ACT_ROUNDS = 4
ACT_MIN_GAIN = 1.0
# joint cooldown vectors re-solved in the MILP after the exhaustive staircase
ACT_JOINT_TOP = 2
# cooldown counts per board kept for the joint enumeration
ACT_JOINT_KEEP = 3


def _act_tables(score_fn, state: dict, act_chars, graphs, act_kind: dict, act_value: dict) -> dict:
    """`{chr: {(cd count, rate sum permil): team score}}` for each member board, the
    other boards as `state` has them. The scorer sees a board's ACTIVE uptime only
    through those two sums (`board.resolve` adds them per card), so one subset per
    reachable sum stands for all. SEPARABLE around the board's current level
    (k0, p0): the cooldown row at p0 and the rate column at k0 are scored exactly
    (~34 calls a board, not ~120), a cell off both is `V(k,p0) + V(k0,p) - V(k0,p0)`."""
    out = {}
    for cid in act_chars:
        g = graphs.get(cid) or {}
        cds = sorted(n for n in g if act_kind.get(n) == "Act.CD")
        prs = sorted(n for n in g if act_kind.get(n) == "Act.Prob")
        if not cds and not prs:
            continue
        reach = {0: ()}
        for n in prs:
            for s, subset in list(reach.items()):
                reach.setdefault(s + act_value[n], subset + (n,))
        have = set(state[cid])
        k0 = sum(1 for n in have if n in cds)
        p0 = sum(act_value[n] for n in have if n in prs)
        if p0 not in reach:
            reach[p0] = tuple(n for n in prs if n in have)
        bare = have - set(cds) - set(prs)
        cur_prs = tuple(n for n in prs if n in have)

        def score(k, subset):
            trial = dict(state)
            trial[cid] = bare | set(cds[:k]) | set(subset)
            return score_fn(trial)
        row = {k: score(k, cur_prs) for k in range(len(cds) + 1)}
        col = {s: (row[k0] if s == p0 else score(k0, subset)) for s, subset in reach.items()}
        out[cid] = {(k, s): row[k] + col[s] - row[k0] for k in row for s in col}
    return out


def solve_team_board(*, scenario: str, song: str | None, pool: int,
                     for_team: list[str] | None,
                     char_level: str = "current",
                     resources: str = "inventory",
                     develop_bench: bool = True,
                     include_sockets: bool = False,
                     uptime_model: str | None = "finite",
                     max_resets: int | None = None,
                     branches: str = "GBRY",
                     targets: dict | None = None,
                     no_refund: bool = False,
                     all_invited: bool = False,
                     reset_min_gain: float = 0.0,
                     act_search: bool = True,
                     song_pool: list | None = None,
                     should_stop=None) -> dict | None:
    """The `{chr: {nodes, slots}}` optimal board for one team, solved exactly.

    Same contract/toggles as `precompute_optimisations.optimised_board`. `slots` is
    left empty by default — the caller's pipeline (`annotate_sockets_for`) places the
    Connect cards, exactly as the beam path does; pass `include_sockets=True` to fill
    them here instead. Returns None on any failure (unloadable oracle, infeasible),
    so the caller can fall back to the beam.

    `max_resets`: how many currently-owned nodes the plan may take off the planned
    boards. Each costs 1,000 Hologold in game (`board_reset`); 0 means the plan
    only adds nodes. None keeps the free rebuild this planner always made.

    Board options matching the community tool holodori.best:
    `branches` -- only nodes of these branches (G/B/R/Y; sockets always) are bought or
    reset; every other node stays exactly as owned. `targets` -- `{chr: {node: "buy" |
    "keep"}}`: a "buy" target is bought if the budget allows (a dominant bonus in the
    objective); a "keep" target is never bought, only kept when owned. `no_refund` -- a
    reset returns nothing: the wallet is the inventory alone. `all_invited` -- a
    character with no board points (not invited) plans at the max-level 361.

    `reset_min_gain` -- what a reset must EARN, as a fraction of the team's score on
    the account's own boards (0.0002 = 0.02 %): each owned node the plan takes off
    costs that much in the objective, so a reset happens only when what it frees
    buys more. A count cap alone (`max_resets`) lets through resets that gain next to nothing and blocks ones that gain a lot.

    `act_search` -- False stops after the first MILP (Act levels priced on the
    account's own boards): no cooldown staircase, no one-board rounds. One solve
    instead of up to ~7 plus ~250 scorings, for a screen over many teams.

    `should_stop` -- called before each MILP; True raises `PlanStopped` (a cancelled
    search stops its plans within one solve instead of finishing them).

    `song_pool` -- a Highlight pool's songs `[{"singers", "singer_type", "slope"}]`
    (`_pool_yellow_terms`): the yellows are valued on EVERY pool song, one capped term
    each, in place of `song`'s own; `song` still sets every other singer term."""
    import highspy  # noqa: PLC0415 — server-side solver
    LAST_STATUSES.clear()

    char_level = char_level if char_level in ("current", "max") else "current"
    resources = resources if resources in ("inventory", "infinite") else "inventory"
    if scenario == "full-collection-maxed":
        char_level = "max"

    try:
        # `uptime_model`: the in-run model the search and the page score with, not
        # `config.json`'s deck-screen `steady` (see `tree_optimise.Oracle`).
        stamp = _account_stamp()
        akey = (pool, song, scenario, uptime_model, stamp)
        oracle = (_ACCOUNT.get(akey) or (None,))[0]
        if oracle is None:
            if any(k[4] != stamp for k in _ACCOUNT):
                _BLOCKS.clear()     # a bench block holds the account's points / owned nodes
            _ACCOUNT.clear()        # one Oracle per process: the song of the last plan
            oracle = to.Oracle(pool_size=pool, song=song, charts_path=None, scenario=scenario,
                               uptime_model=uptime_model)
        # prime() enumerates the pool (~10s) only to pick _teams[0]; a fixed team
        # doesn't need it — _branch_values/score_team run off the collection setup.
        if not (for_team and len(for_team) == to.MEMBER_COUNT + 1):
            oracle.prime()
    except Exception:  # noqa: BLE001
        return None
    if for_team and len(for_team) == to.MEMBER_COUNT + 1:
        leader_id, member_ids = for_team[0], tuple(for_team[1:])
    else:
        leader_id, member_ids = oracle._teams[0]

    held = _ACCOUNT.get(akey)
    if held is None:
        held = _ACCOUNT[akey] = (oracle, *_account_inputs(oracle))
    _o, graphs, prices, owned, balance, _wr, _pt = held
    singers = frozenset(oracle._chart.singers) if oracle._chart else frozenset()
    values = to._branch_values(oracle, graphs, prices, leader_id, member_ids,
                               singer_chars=singers)
    # Act.CD / Act.Prob are priced by the per-board level tables, never per node
    act_kind = {n: (m.get("e") or {}).get("t") for n, m in oracle.skilltree.get("nodes", {}).items()
                if (m.get("e") or {}).get("t") in ("Act.CD", "Act.Prob")}
    act_value = {n: int((oracle.skilltree["nodes"][n].get("e") or {}).get("v") or 0) for n in act_kind}
    values = {n: v for n, v in values.items() if n not in act_kind}

    # Budgets: a --respec bare rebuild. Points per character follow char_level; the
    # material wallet is the refund-inclusive balance (inventory) or unbounded (infinite).
    wallet_ref, points = dict(_wr), dict(_pt)
    if char_level == "max":
        points = {c: 361 for c in points}  # level-50 grant, as cmd_plan's --at-max-level
    if all_invited:
        points = {**{c: 361 for c in graphs}, **{c: p for c, p in points.items() if int(p) > 0}}
    if no_refund:
        wallet_ref = {cur: int((balance or {}).get(cur, 0)) for cur in to.CURRENCIES}
    branches = "".join(sorted(set(str(branches or "GBRY").upper()) & set("GBRY"))) or "GBRY"
    targets = {c: dict(v) for c, v in (targets or {}).items()}

    def node_ok(cid: str, node: str) -> bool:
        """May the plan BUY `node` on `cid`'s board? (branch filter, keep-only targets)"""
        if node[0] not in "GBRY":
            return True
        if node[0] not in branches:
            return False
        return targets.get(cid, {}).get(node) != "keep"
    # `branches` is normalised SORTED ("BGRY" = all four); comparing it with "GBRY"
    # would make every plan "constrained"
    constrained = branches != "BGRY" or bool(targets)
    owned_sets = {c: set(v or ()) for c, v in owned.items()}

    def charged_free(cid: str, node: str) -> bool:
        """Kept at no material cost: with `no_refund` an owned node is already paid
        for (the wallet then holds the inventory only, nothing refunded)."""
        return no_refund and node in owned_sets.get(cid, ())
    wallet = None if resources == "infinite" else {
        cur: int((wallet_ref or {}).get(cur, 0)) for cur in to.CURRENCIES}

    leader_chr = "chr-" + leader_id.split("-")[1]
    member_chrs = {"chr-" + m.split("-")[1] for m in member_ids}
    # sorted: the MILP's variable order (and so which of several optimal plans
    # HiGHS returns) must not follow the per-process string hash seed
    team_chars = [leader_chr] + sorted(c for c in member_chrs if c != leader_chr)
    song_mode = bool(song)

    # Bench boards: every OTHER holomem with board points. Their GREEN is account-global
    # (it stacks onto the team score from any board), so developing it with idle points is
    # free score (about +8.3k on a typical team). RED/BLUE pay 0 off
    # the team (`_scope_value`), so a bench board only ever buys green (and, in song mode,
    # a singer's yellow). Skipped when develop_bench is off (team-only, faster).
    bench_chars: list = []
    if develop_bench:
        bench_chars = [c for c in graphs
                       if c not in team_chars and int(points.get(c, 0)) > 0]
    all_chars = team_chars + bench_chars
    grouping_terms = _grouping_terms(oracle, leader_id, member_ids, all_chars, owned,
                                     graphs) if all_chars else {}
    # the song's yellow, one capped variable over its singers' boards (team or bench)
    if song_pool:
        yellow_list = _pool_yellow_terms(oracle, song_pool, all_chars, graphs) if all_chars else []
    else:
        one = _yellow_terms(oracle, leader_id, member_ids, all_chars, owned,
                            graphs) if (song_mode and all_chars) else None
        yellow_list = [one] if one else []
    yellow_keys = {k for term in yellow_list for k in term["amounts"]}
    reset_penalty = 0.0
    if reset_min_gain and reset_min_gain > 0:
        reset_penalty = float(reset_min_gain) * oracle.score_team(
            leader_id, member_ids, oracle.state_from(
                {c: sorted(set(owned.get(c) or ()) | {to.ROOT}) for c in graphs}))

    def _solve(context_values: dict, act_tables: dict | None = None,
               warm: dict | None = None) -> dict | None:
        """One MILP over the boards: `values` scoped per branch, plus
        `context_values[(character, node)]` for the nodes valued in context."""
        if should_stop is not None and should_stop():
            raise PlanStopped("the search was cancelled")
        h = highspy.Highs()
        h.setOptionValue("output_flag", False)
        # one thread: these MILPs gain nothing from HiGHS's pool (e.g. a plan's 8 solves:
        # 25.5 s default, 25.6 s on one thread) and up to 16 plans
        # run at once in the engine's workers, each with its own pool of half the cores
        h.setOptionValue("threads", 1)
        h.setOptionValue("time_limit", 60.0)
        # the default 0.01 % gap is ~180 score units on a ~1.8M objective: far above
        # TIE_BREAK, so the tie-break needs a gap below it (1e-7 = ~0.2 units). A
        # zero gap costs 15-18 s a solve on some teams (e.g. m0198) for nothing
        h.setOptionValue("mip_rel_gap", MIP_REL_GAP)

        def qsum(terms):
            # the same idxs / vals / constant as `e = e + t` (which copies the whole
            # expression on each term: O(n^2)), accumulated in place
            e = None
            for t in terms:
                if e is None:
                    e = 0 + t          # the original first step: a fresh expression
                else:
                    e += t
            return 0 if e is None else e

        # binaries added as [0, 1] columns and made integer in ONE changeColsIntegrality
        # call before the solve (highspy's addBinary calls changeColIntegrality per
        # column: ~33 us each)
        _int_cols: list = []
        _scratch_ints: dict = {}

        def addBinary():
            v = h.addVariable(0, 1)
            _int_cols.append(v.index)
            return v

        def _apply_int(hb):
            idx = _scratch_ints.pop(id(hb), [])
            if idx:
                hb.changeColsIntegrality(len(idx), np.array(idx, dtype=np.int32),
                                         np.full(len(idx), highspy.HighsVarType.kInteger, dtype=np.uint8))

        x: dict = {}
        per_char_nodes: dict = {}
        bench_set = set(bench_chars)

        def _board_into(hb, cid, g, nodes_out):
            """One board's columns and rows (its binaries, root, frozen branches, point
            budget and connectivity), built into `hb` -- a scratch model whose block
            is then copied into the plan's model below."""
            h = hb
            x = {}

            def addBinary():
                v = hb.addVariable(0, 1)
                _scratch_ints.setdefault(id(hb), []).append(v.index)
                return v
            # A bench board only ever buys GREEN (account-global; red/blue pay 0 there) and,
            # in SONG mode, a bench SINGER's YELLOW. Restrict it to those branches + the
            # structural/socket spine, so the joint MILP over ~54 boards stays small (a few
            # dozen relevant nodes per bench board, not all ~150). Pruning in BOTH modes is
            # what keeps the song plan fast — an unpruned bench is ~40 boards × 150 nodes.
            if cid in bench_set:
                keep_branches = ("G", "S", "Y") if song_mode else ("G", "S")
                nodes = [n for n in g if n[0] in keep_branches]
            else:
                nodes = list(g.keys())
            if to.ROOT not in nodes:      # green frame must keep the root to connect
                nodes.append(to.ROOT)
            # a pruned bench board must still be able to KEEP what it owns: its reds and
            # blues pay nothing for this team, but taking them off is a reset (1,000
            # Hologold, the node lost for every other team) that only the tie-break /
            # `reset_min_gain` may decide -- and a reset budget or a frozen branch would
            # be infeasible without them
            nodes.extend(n for n in (owned.get(cid) or ()) if n in g and n not in nodes)
            if constrained:
                # buy targets are candidates on any board of the team
                nodes.extend(n for n in targets.get(cid, {}) if n in g and n not in nodes)
            nodes_out.extend(nodes)
            for n in nodes:
                x[(cid, n)] = addBinary()
            root = to.ROOT
            h.addConstr(x[(cid, root)] == 1)
            if constrained:
                have = set(owned.get(cid) or ())
                for n in nodes:
                    if n == root or n[0] not in "GBRY":
                        continue
                    if n[0] not in branches:
                        # a frozen branch: exactly as owned
                        h.addConstr(x[(cid, n)] == (1 if n in have else 0))
                    elif targets.get(cid, {}).get(n) == "keep" and n not in have:
                        h.addConstr(x[(cid, n)] == 0)
            # per-character point budget (root free)
            budget = int(points.get(cid, 0))
            h.addConstr(qsum(prices.get(n, {}).get("points", 0) * x[(cid, n)]
                             for n in nodes if n != root) <= budget)
            # per-character connectivity over the edges among the KEPT nodes (a pruned
            # bench board keeps only its green frame): `_connectivity`'s bridge precedences
            # and minimal vertex separators -- no flow variables
            unreachable, precedences, separators = _connectivity(g, nodes)
            for n in unreachable:
                h.addConstr(x[(cid, n)] == 0)
            for w, u in precedences:
                h.addConstr(x[(cid, w)] - x[(cid, u)] <= 0)
            for v, sep in separators:
                h.addConstr(x[(cid, v)] - qsum(x[(cid, t)] for t in sep) <= 0)

            return x

        # each board's block (columns + rows) is built by `_board_into` into a scratch
        # model, read back and appended in one addCols / addRows; a BENCH board's block is
        # the same in every solve of every plan of this account, so it is kept per process
        # (`_BLOCKS`). Same columns, rows and order as building in place: the LP and the
        # solution are byte-identical (checked on 30 MILPs and 56 full plans)
        for cid in all_chars:
            g = graphs.get(cid)
            if not g:
                continue
            bkey = (cid, song_mode, cid in bench_set, int(points.get(cid, 0)), constrained, branches,
                    tuple(sorted(targets.get(cid, {}).items())), tuple(owned.get(cid) or ()), akey[4])
            blk = _BLOCKS.get(bkey) if cid in bench_set else None
            if blk is None:
                sh = highspy.Highs()
                sh.setOptionValue("output_flag", False)
                nodes_b: list = []
                xs = _board_into(sh, cid, g, nodes_b)
                _apply_int(sh)
                lp = sh.getLp()
                m = lp.a_matrix_
                assert "Rowwise" in str(m.format_), m.format_
                blk = (list(nodes_b), {k[1]: v.index for k, v in xs.items()},
                       np.array(lp.col_lower_), np.array(lp.col_upper_),
                       np.array([int(t) for t in lp.integrality_], dtype=np.uint8),
                       np.array(lp.row_lower_), np.array(lp.row_upper_),
                       np.array(m.start_, dtype=np.int32), np.array(m.index_, dtype=np.int32),
                       np.array(m.value_))
                if cid in bench_set:
                    _BLOCKS[bkey] = blk
            nodes_b, xloc, cl, cu, integ, rl, ru, st, ix, va = blk
            per_char_nodes[cid] = list(nodes_b)
            base = h.getNumCol()
            nc = len(cl)
            h.addCols(nc, np.zeros(nc), cl, cu, 0, np.zeros(nc + 1, dtype=np.int32),
                      np.empty(0, dtype=np.int32), np.empty(0))
            ints = np.nonzero(integ)[0]
            _int_cols.extend((ints + base).tolist())
            for n, li in xloc.items():
                x[(cid, n)] = highspy.highs_var(base + li, h)
            nr = len(rl)
            h.addRows(nr, rl, ru, len(va), st[:nr], ix + base, va)

        # reset budget: a node owned now and left out of the plan is one reset
        if max_resets is not None:
            dropped = [1 - x[(cid, n)] for cid in per_char_nodes
                       for n in (owned.get(cid) or ()) if n != to.ROOT and (cid, n) in x]
            if dropped:
                h.addConstr(qsum(dropped) <= int(max_resets))

        # shared cube/hexa wallet: one account-wide constraint per currency. With
        # `no_refund` the wallet is the inventory alone, so only a NEW buy draws on it:
        # an owned node kept costs nothing again (it was charged every owned node,
        # which made a no-refund plan look unaffordable for keeping its own board)
        if wallet is not None:
            for cur in to.CURRENCIES:
                h.addConstr(qsum(prices.get(n, {}).get(cur, 0) * x[(cid, n)]
                                 for cid in per_char_nodes for n in per_char_nodes[cid]
                                 if prices.get(n, {}).get(cur, 0) and not charged_free(cid, n))
                            <= wallet[cur])

        # GENERATION GREENS (G-008/011/021): what a grouping's members receive is the
        # sum over every board of that grouping -- team or bench -- capped at
        # GROUPING_ALL_CAP, so they are one capped variable per grouping, worth the
        # grouping's measured slope, not a value per node (a 3rd ID chain is worth 0)
        capped_terms = []
        for gterm in grouping_terms.values():
            supply = [(a, x[key]) for key, a in gterm["amounts"].items() if key in x]
            if not supply:
                continue
            c = h.addVariable(0, GROUPING_ALL_CAP)
            h.addConstr(c - qsum(a * xv for a, xv in supply) <= 0)
            capped_terms.append(gterm["slope"] * c)
        # the song's YELLOW the same way: the singers' boards add up, the game counts
        # SONG_YELLOW_CAP permil of it (a 4th GAMERS board's 40 permil is worth 0)
        # (a Highlight pool: one such term per pool song, `_pool_yellow_terms`)
        for yterm in yellow_list:
            supply = [(a, x[key]) for key, a in yterm["amounts"].items() if key in x]
            if supply:
                c = h.addVariable(0, SONG_YELLOW_CAP)
                h.addConstr(c - qsum(a * xv for a, xv in supply) <= 0)
                capped_terms.append(yterm["slope"] * c)

        # ACTIVE UPTIME (Act.CD / Act.Prob): a card's score depends on its board's
        # cooldown COUNT and activation-rate SUM only, and not monotonically (a
        # shorter cooldown can add overlap), so each member board chooses ONE level
        # (k, p) from its exactly-scored table (`_act_tables`), tied to the nodes
        # bought -- points, cubes and paths traded against every other node exactly.
        for cid, table in (act_tables or {}).items():
            if cid not in per_char_nodes:
                continue
            cd_x = [x[(cid, n)] for n in per_char_nodes[cid] if act_kind.get(n) == "Act.CD"]
            pr_x = [(act_value[n], x[(cid, n)]) for n in per_char_nodes[cid]
                    if act_kind.get(n) == "Act.Prob"]
            z = {lv: addBinary() for lv in table}
            h.addConstr(qsum(z.values()) == 1)
            h.addConstr(qsum(cd_x) - qsum(lv[0] * zv for lv, zv in z.items()) == 0)
            h.addConstr(qsum(v * xv for v, xv in pr_x) - qsum(lv[1] * zv for lv, zv in z.items()) == 0)
            ref = min(table.values())
            capped_terms.append(qsum((table[lv] - ref) * zv for lv, zv in z.items()))

        # objective: scoped per-node value across the six boards
        # a target (buy, or keep when owned) outweighs any score a node can add, so
        # it is taken whenever the points / wallet / connectivity allow it
        bonus = {(c, n): TARGET_BONUS for c, nodes in targets.items() for n, mode in nodes.items()
                 if mode == "buy" or n in set(owned.get(c) or ())}
        # TIE-BREAK: keep what is owned, buy nothing that adds nothing. Without it a
        # zero-value node is a free coin-flip: owned nodes that score 0 on this song
        # could be reset while freeing nothing the plan needs, yet each reset costs
        # 1,000 Hologold and the node's value on every other song.
        owned_set = {(c, n) for c, ns in owned.items() for n in ns}
        keep_bonus = TIE_BREAK + reset_penalty
        # setObjective, not maximize(): highspy's maximize() SOLVES, and the run()
        # below would then solve the whole MILP a second time
        h.setObjective(qsum(
            ((0.0 if (cid, n) in yellow_keys else
              _scope_value(n, cid, values, leader_chr=leader_chr, member_chrs=member_chrs,
                           song=song_mode, singers=singers))
         + context_values.get((cid, n), 0.0) + bonus.get((cid, n), 0.0)
         + (keep_bonus if (cid, n) in owned_set else -TIE_BREAK)) * x[(cid, n)]
            for cid in per_char_nodes for n in per_char_nodes[cid] if n != to.ROOT)
            + qsum(capped_terms), highspy.ObjSense.kMaximize)
        if _int_cols:
            h.changeColsIntegrality(len(_int_cols), np.array(_int_cols, dtype=np.int32),
                                    np.full(len(_int_cols), highspy.HighsVarType.kInteger, dtype=np.uint8))
        if warm:
            # the bench as the previous plan left it: a later solve only re-trades the
            # team's boards around one Act level, and the ~50 bench boards' flow
            # variables are most of its time (e.g. m0198: 8 s -> 1.3 s a solve)
            # -- except a bench red or blue it holds, free to come OFF: worth nothing to
            # the team, it is the bench's material reserve, and frozen it can leave a
            # member's Act level unfundable while a bench board sits on a whole blue
            # branch (the plan then resets team nodes instead and scores lower)
            for (cid, n), var in x.items():
                if cid in bench_set:
                    v = 1.0 if n in (warm.get(cid) or {}).get("nodes", ()) else 0.0
                    h.changeColBounds(var.index, 0.0 if (v and n[0] in "RB") else v, v)
            # the previous plan as a starting incumbent: most solves here differ from
            # the last one by one board's Act level (HiGHS drops it if infeasible)
            idx = [x[key].index for key in x]
            val = [1.0 if key[1] in (warm.get(key[0]) or {}).get("nodes", ()) else 0.0
                   for key in x]
            h.setSolution(len(idx), np.array(idx, dtype=np.int32), np.array(val, dtype=np.float64))
        h.run()
        LAST_STATUSES.append(h.getModelStatus().name)
        if "kOptimal" not in h.getModelStatus().name and "kTimeLimit" not in h.getModelStatus().name:
            return None
        sol = h.getSolution().col_value
        board = {cid: {"nodes": sorted(n for n in per_char_nodes[cid]
                                       if sol[x[(cid, n)].index] > 0.5),
                       "slots": {}}
                 for cid in per_char_nodes}
        return board

    def team_score(node_dict):
        return oracle.score_team(leader_id, member_ids,
                                 oracle.state_from({c: sorted(n) for c, n in node_dict.items()}))

    # ACTIVE UPTIME, block-coordinate: each member board's level table is scored
    # with the other boards as the current plan leaves them, the MILP re-solved on
    # those tables, until no board changes level (the cards interact through the
    # per-instant max). Replaces the per-node context pass, which priced each Act
    # node alone, on top of its isolated value (counted twice), and could not see
    # that a level is reached by several nodes together.
    def full_state(board_):
        return {cid: set(board_[cid]["nodes"]) if cid in board_
                else set(owned.get(cid) or [to.ROOT]) | {to.ROOT} for cid in graphs}

    def levels(state_):
        return {cid: (sum(1 for n in state_[cid] if act_kind.get(n) == "Act.CD"),
                      sum(act_value[n] for n in state_[cid] if act_kind.get(n) == "Act.Prob"))
                for cid in act_chars}

    act_chars = sorted(c for c in member_chrs if c in graphs)
    # round 0: every board free, tables on the account's own boards
    tables = _act_tables(team_score, {c: set(owned.get(c) or [to.ROOT]) | {to.ROOT}
                                      for c in graphs}, act_chars, graphs, act_kind, act_value)
    board = _solve({}, tables)
    best = team_score(full_state(board)) if board is not None else None
    # THE STAIRCASE, exhaustively: every joint cooldown count of the member boards
    # (4^5 = 1,024 on a full team) scored exactly on the round-0 plan, rate sums as
    # planned; the best ACT_JOINT_TOP vectors are then pinned in the MILP (each board
    # free on its rate sum only) so points and cubes are re-traded around them.
    # Unlike trying subsets of a few frontier nodes on the points LEFT OVER, this sees
    # every count, paths and point trades included.
    if board is not None and act_search:
        state0 = full_state(board)
        now0 = levels(state0)
        ranked = []
        cds_of = {c: sorted(n for n in graphs[c] if act_kind.get(n) == "Act.CD") for c in act_chars}
        # each board's ACT_JOINT_KEEP best counts on its own (round-0 table, others
        # as planned): 3^5 = 243 joint vectors instead of 4^5
        options = []
        for c in act_chars:
            tb = tables.get(c) or {}
            by_k = {}
            for (k, s), v in tb.items():
                if s == now0[c][1]:
                    by_k[k] = v
            ks = sorted(by_k, key=lambda k: -by_k[k])[:ACT_JOINT_KEEP] or [now0[c][0]]
            options.append(sorted(set(ks) | {now0[c][0]}))
        for kvec in itertools.product(*options):
            trial = dict(state0)
            for c, k in zip(act_chars, kvec):
                trial[c] = (state0[c] - set(cds_of[c])) | set(cds_of[c][:k])
            ranked.append((team_score(trial), kvec))
        ranked.sort(reverse=True)
        for _v, kvec in ranked[:ACT_JOINT_TOP]:
            if all(now0[c][0] == k for c, k in zip(act_chars, kvec)):
                continue          # the plan already has it
            pinned = {c: {lv: v for lv, v in tables[c].items() if lv[0] == k}
                      for c, k in zip(act_chars, kvec) if c in tables}
            if any(not tb for tb in pinned.values()):
                continue
            solved_ = _solve({}, pinned, warm=board)
            if solved_ is None:
                continue
            value = team_score(full_state(solved_))
            if value > best:
                board, best = solved_, value
    # then one board at a time (Gauss-Seidel): freeing them all at once on tables
    # measured in the old context oscillates (e.g. m0342: one board flipped 3 -> 0
    # cooldown nodes, -26k), because the cards share the per-instant max
    # A board whose step does not pay is set aside and the NEXT best board tried, on the
    # same tables (the plan did not move), rather than ending the loop, so one board
    # the tables over-rate cannot stop every other one
    tabu: set = set()
    tables = None
    improved = attempts = 0
    while (board is not None and act_search and improved < ACT_ROUNDS
           and attempts < ACT_ROUNDS + len(act_chars)):
        attempts += 1
        if tables is None:
            state_ = full_state(board)
            now = levels(state_)
            tables = _act_tables(team_score, state_, act_chars, graphs, act_kind, act_value)
            all_gains = {c: max(tb.values()) - tb[now[c]] for c, tb in tables.items() if now[c] in tb}
        gains = {c: g for c, g in all_gains.items() if c not in tabu}
        if not gains:
            break
        free = max(sorted(gains), key=lambda c: gains[c])
        if gains[free] <= ACT_MIN_GAIN:
            break
        step = {c: (tb if c == free else {now[c]: tb[now[c]]}) for c, tb in tables.items()
                if c == free or now[c] in tb}
        solved_ = _solve({}, step, warm=board)
        value = None if solved_ is None else team_score(full_state(solved_))
        if value is None or value <= best:
            tabu.add(free)
            continue
        board, best = solved_, value
        improved += 1
        tabu.clear()
        tables = None
    if board is None:
        return None

    # THE IN-CONTEXT FILL, as on the beam path (`tree_optimise.fill_boards`).
    # `_branch_values` leaves out, on purpose, the nodes whose value is not
    # additive — the ACTIVE-uptime family (Act.CD / Act.Prob: B-013, B-020, B-031, ...)
    # and the per-character generation greens (G-008/011/021) — because an isolated
    # marginal misprices them; the beam then buys them with the LEFTOVER points,
    # each re-scored on the board being built. Without this pass the MILP never buys
    # any of them: every member board stops at the same points whatever its budget,
    # and the plan scores several % below a greedy plan. Same call, same order as
    # `cmd_plan`.
    spent_points = {cid: sum(prices.get(n, {}).get("points", 0) for n in board[cid]["nodes"]
                             if n != to.ROOT) for cid in board}
    # team-only unless the bench is developed too (`develop_bench`)
    left_points = {cid: int(points.get(cid, 0)) - spent_points.get(cid, 0) for cid in points
                   if develop_bench or cid in team_chars}
    if wallet is None:
        left_wallet = {cur: 10 ** 12 for cur in to.CURRENCIES}
    else:
        left_wallet = {cur: wallet[cur] - sum(prices.get(n, {}).get(cur, 0)
                                              for cid in board for n in board[cid]["nodes"]
                                              if not charged_free(cid, n))
                       for cur in to.CURRENCIES}
    state = {cid: set(board.get(cid, {}).get("nodes") or [to.ROOT]) for cid in graphs}
    scope = to._payable_sockets(oracle.skilltree, leader_chr, list(member_chrs),
                                song_mode=song_mode, singer_chars=singers)
    # The contextual nodes FIRST, on the leftover points: `fill_boards` runs its
    # additive phase before its contextual one, and with the MILP having already
    # bought every additive node worth its points, the additive phase would spend the
    # leftovers on low-value greens and leave the Act.CD nodes nothing (e.g. m0198:
    # one board kept 1 point and stayed 4.7 % below the greedy plan). An empty
    # `values` makes the first call contextual-only; the second spends what is left
    # on the additive values, with no contextual pass.
    state, left_wallet, left_points, _n = to.fill_boards(
        graphs, prices, state, left_wallet, left_points, {},
        leader_chr, list(member_chrs), song_mode=song_mode, singer_chars=singers,
        interactive_nodes=to._interactive_node_ids(oracle.skilltree), score_fn=team_score,
        socket_scope=scope, node_ok=node_ok if constrained else None)
    # the capped yellow is the MILP's: a per-node value here would re-buy past the cap
    fill_values = {n: v for n, v in values.items()
                   if n not in {k[1] for k in yellow_keys}}
    filled, _w, _p, _n = to.fill_boards(
        graphs, prices, state, left_wallet, left_points, fill_values,
        leader_chr, list(member_chrs), song_mode=song_mode, singer_chars=singers,
        socket_scope=scope, node_ok=node_ok if constrained else None)
    for cid, nodes in filled.items():
        if cid in board or set(nodes) != {to.ROOT}:
            board.setdefault(cid, {"nodes": [], "slots": {}})["nodes"] = sorted(nodes)

    # sockets: place the best Connect cards on the solved board, same as the beam path
    if not include_sockets:
        return board
    try:
        import socket_optimise as so  # noqa: PLC0415
        tmp = Path(tempfile.gettempdir()) / f"hbsolve_sock_{leader_id}.json"
        tmp.write_text(json.dumps(board), encoding="utf-8")
        try:
            res = so.optimise(pool=pool, board_state_path=tmp, scenario=scenario,
                              song=song, leader_id=leader_id, member_ids=member_ids)
            for p in res.get("placements", []):
                cid = p.get("chr")
                if cid in board:
                    board[cid]["slots"][p["socket"]] = p["card"]
        finally:
            tmp.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 — sockets are a best-effort add-on
        pass

    return board
