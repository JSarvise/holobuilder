"""The swap-refine's EXACT stage across processes — same numbers, fewer seconds.

`board_plan.refine_members` hill-climbs one team's five member slots by the
target metric. Its cost is the exact stage: every shortlisted candidate is
DEVELOPED (`optimize_and_rerank`, ~20 ms) and SCORED on the chart
(`score_song_detailed`, ~50-100 ms with the joint slot-order walk). A round is
5 slots x `shortlist` candidates of that — 100 evaluations at shortlist 20 —
and a wide refine over a dozen leaders plus alternative seeds takes ~3-8 minutes
serially. Those evaluations share no state, so this is
`parallel_songs.py`'s pattern applied to them: `spawn` context, initializer
carries the context once per worker, small tasks dispatched dynamically, and
the parent keeps the decision.

## What a worker holds and what a task is

The initializer receives everything the server's `_develop_one` and
`_song_est_of` closures close over, as plain data: the card catalogue (Card
dataclasses), the scoring config, the chart(s), the skilltree/master dumps,
node effect ids, Connect cards, bench boards, the chr->name map and the
budget rule. Each worker rebuilds the same `tree_for`/`budget_for` the server
builds and caches its trees. A task is one team as ids; it returns the exact
score — the MEAN over the given charts, so one pool serves the song mode (one
chart) and the across mode's sampled-mean metric (16 charts) alike. Boards do
not cross the process boundary: `refine_members` re-develops only the accepted
winner in-process, so the returned `BoardOptimisedTeam` is built by the same
code path as a serial refine.

## Why the answer cannot depend on the pool

Workers compute; the parent decides. `RefinePool.batch_score(teams)` returns
scores in the order of `teams`, and `refine_members` applies the same accept
rule it applies to serial scores. Each score is the same `optimize_and_rerank`
+ `score_song_detailed` on the same inputs in the same interpreter, so it is
the same float. Any failure to stand the pool up, or a worker dying, falls
back to computing the batch serially IN THIS PROCESS with the same functions —
the score is then computed here instead of there, never approximated.
"""

from __future__ import annotations

import dataclasses
import multiprocessing
from typing import Callable, Mapping, Sequence

from hbuilder.domain.board_optimizer import BoardPlan
from hbuilder.domain.board_plan import (
    BoardOptimisedTeam,
    PlanCache,
    SocketPlacement,
    SongCast,
    singer_bench_characters,
    song_cast,
    TeamBoardPlan,
    UNIT_SCORE_K,
    board_model_from_plan,
    cached_team_plan,
    develop_and_refine,
    develop_refined_team,
    model_for_character,
    optimize_and_rerank,
    skill_tree_from_dumps,
)
from hbuilder.domain.card import Card
from hbuilder.domain.scoring import deck_screen_unit_score, score_with_breakdown
from hbuilder.domain.scoring_config import ScoringConfig
from hbuilder.domain.song import SongChart, overall_power_for, score_song_detailed
from hbuilder.domain.team import Team
from hbuilder.solver.parallel import default_workers

__all__ = ["RefinePool", "RefineContext", "board_from_state", "develop_team", "evaluate",
           "new_plan_cache"]


@dataclasses.dataclass(frozen=True)
class RefineContext:
    """Everything one exact evaluation needs, as picklable data. Built once by
    the caller (the web server), shipped once per worker."""

    cards: tuple[Card, ...]
    cfg: ScoringConfig                       # board=None; each team gets its own
    charts: tuple[SongChart, ...]            # 1 chart (song), a sample (across), or
                                             # () -> the developed deck-screen Unit Score
    skilltree: Mapping
    node_master: Sequence[Mapping]
    node_effect_ids: Mapping[str, list[str]]
    connect_cards: Mapping | None
    bench_characters: Mapping | None
    name_by_chr: Mapping[str, str]
    flat_budget: int | None                  # None -> per-character `granted`
    granted: Mapping[str, int]               # chr-id -> real earned points
    # PLAN CACHE. `False` (default): every evaluation develops its own board
    # through `optimize_and_rerank`. `True`: each process holds
    # ONE `PlanCache` (built lazily by `new_plan_cache` from `plan_cache_seed`,
    # a reference team as ids, `PlanCache.from_team`) and the leaf is
    # `cached_team_plan` + `board_model_from_plan(plan_cache=...)` + the chart
    # score — the objective as a pure function of the roster, ~3× cheaper.
    # The seed is REQUIRED with `use_plan_cache` so the nominal context is the
    # caller's choice and the same in every worker, never "whichever team this
    # process happened to see first".
    use_plan_cache: bool = False
    plan_cache_seed: tuple[str, tuple[str, ...]] | None = None
    # A planned search (boards re-planned, not as they are): each song's SINGERS'
    # bench boards are planned for it too (`board_plan.singer_bench_characters`), as
    # the materials re-plan plans them. Needs the plan cache; off for boards as they are.
    plan_singer_bench: bool = False
    # MANY charts, the board REBUILT PER SONG. `False` (default): several charts
    # share ONE board developed with `sings=False` — the plain across objective.
    # `True`: each chart is scored on the board developed for ITS singer
    # context (the leader sings it or not: at most two boards per team), so the
    # mean is exactly the mean of the team's single-song scores — the honest
    # "in general" figure for a player who re-specs the board per song.
    board_per_chart: bool = False
    # ACTIVATION REFINEMENT. `True`: after the develop, the team's
    # activation-frequency / -probability nodes are chosen jointly on the score
    # being computed (`board_plan.develop_and_refine` ->
    # `refine_activation_nodes`): what a shorter cooldown is worth is a matter
    # of where the windows then fall, which no per-node value captures
    # (+0.2..3.1 % on six songs). ~10-100x the cost of a plain evaluation, so it is
    # meant for re-scoring a short list, not for a search.
    refine_activation: bool = False
    # BOARDS AS THEY ARE (the "Optimize boards" switch off).
    # `None` (default): each team's boards are PLANNED (the develop). A
    # `{chr: {"nodes": [...], "slots": {...}}}` state: every team character's
    # board is taken from it as it stands (`board_from_state`), nothing is
    # planned and `refine_activation` has nothing to choose.
    board_state: Mapping | None = None
    # `True`: `board_state` is the ACCOUNT's (`board_state.json`), so its `slots`
    # are the real socketed cards and are taken as they stand. `False`: the
    # sockets are assigned by the develop's matching over the state's nodes (a
    # planned state whose `slots: {}` means "not decided"). Re-assigning on the
    # account's state puts the "as they are" Overall Power off the server by up
    # to 3,450.
    board_state_slots: bool = False
    # BENCH SOCKETS: `True` puts every off-team board's root socket in the
    # develop's Connect matching (`board_plan._bench_socket_rows`), so each card is used
    # once account-wide and the bench's sockets are chosen for the team. ~48 extra rows a
    # team: meant for the short lists (the within-wallet re-plan, the row detail).
    socket_bench: bool = False
    # THE BENCH'S SOCKETS AS THEY ARE: `{chr: {unlocked socket: card or
    # None}}` from the account (`board_state.json`). Without `socket_bench` the cards
    # held on off-team boards are kept out of the team's matching (they keep lifting
    # their own board, already in `bench_characters`, and must not be counted
    # twice); with it every unlocked bench socket joins the matching, so the empty ones
    # can be filled too (`board_plan.board_model_from_plan`).
    bench_sockets: Mapping | None = None


def new_plan_cache(ctx: RefineContext, by_id: Mapping[str, Card]) -> PlanCache:
    """The one `PlanCache` a process uses under `ctx.use_plan_cache`."""
    if ctx.plan_cache_seed is None:
        raise ValueError("RefineContext.use_plan_cache needs plan_cache_seed "
                         "(a reference team as (leader id, member ids))")
    leader_id, member_ids = ctx.plan_cache_seed
    seed = Team(leader=by_id[leader_id], members=tuple(by_id[m] for m in member_ids))
    return PlanCache.from_team(seed, ctx.cfg)


# ---------------------------------------------------------------------------
# Worker side. Module globals set once by the initializer (spawn gives every
# worker a fresh interpreter, exactly as parallel_songs.py relies on).
# ---------------------------------------------------------------------------
_CTX: RefineContext | None = None
_BY_ID: dict[str, Card] = {}
_TREES: dict = {}
_PLAN_CACHE: PlanCache | None = None
# the pool's shared stop mark (`RefinePool.stop_through`): tasks of a generation at or
# below it stop early (a cancelled search's exact plans, `materials_plan.dispatch`)
_STOP = None


def _init_worker(ctx: RefineContext, stop=None) -> None:
    global _CTX, _BY_ID, _TREES, _PLAN_CACHE, _STOP
    _CTX = ctx
    _BY_ID = {c.id: c for c in ctx.cards}
    _TREES = {}
    _PLAN_CACHE = None
    _STOP = stop


def stopped(generation: int) -> bool:
    """In a worker: has the parent stopped this generation of tasks?"""
    return _STOP is not None and generation is not None and _STOP.value >= generation


def _worker_plan_cache() -> PlanCache | None:
    """This worker's plan cache, built on first use when the context asks."""
    global _PLAN_CACHE
    assert _CTX is not None
    if not _CTX.use_plan_cache:
        return None
    if _PLAN_CACHE is None:
        _PLAN_CACHE = new_plan_cache(_CTX, _BY_ID)
    return _PLAN_CACHE


def _tree_for(card: Card):
    assert _CTX is not None
    model = model_for_character(_CTX.skilltree, "chr-" + card.id.split("-")[1])
    if model is None:
        return None
    if model not in _TREES:
        _TREES[model] = skill_tree_from_dumps(_CTX.skilltree, _CTX.node_master, model)
    return _TREES[model]


def _budget_for(card: Card) -> int:
    assert _CTX is not None
    if _CTX.flat_budget is not None:
        return _CTX.flat_budget
    g = _CTX.granted.get("chr-" + card.id.split("-")[1])
    return int(g) if g else 0


_CARDS_BY_CHR: dict = {}
_SINGER_CTX: dict = {}


def _with_singer_bench(ctx, by_id, tree_for, budget_for, plan_cache, sings):
    """`ctx` with its singers' bench boards planned for the song (`plan_singer_bench`,
    a `SongCast` with a cast, the plan cache, boards not as they are), else `ctx`."""
    if (not ctx.plan_singer_bench or plan_cache is None or ctx.board_state is not None
            or not isinstance(sings, SongCast) or not sings):
        return ctx
    hit = _CARDS_BY_CHR.get(id(by_id))
    if hit is None or hit[0] is not by_id:
        cards: dict = {}
        for card_id in sorted(by_id):
            cards.setdefault("chr-" + card_id.split("-")[1], by_id[card_id])
        _CARDS_BY_CHR.clear()
        hit = _CARDS_BY_CHR[id(by_id)] = (by_id, cards)
    bench = singer_bench_characters(
        ctx.bench_characters, sings, cache=plan_cache, cards_by_chr=hit[1],
        tree_for=tree_for, budget_for=budget_for, node_effect_ids=ctx.node_effect_ids,
        skilltree=ctx.skilltree, name_by_chr=ctx.name_by_chr)
    if bench is ctx.bench_characters:
        return ctx
    done = _SINGER_CTX.get(id(ctx))
    if done is None or done[0] is not ctx or done[1] is not bench:
        if len(_SINGER_CTX) > 8:
            _SINGER_CTX.clear()
        done = _SINGER_CTX[id(ctx)] = (ctx, bench, dataclasses.replace(ctx, bench_characters=bench))
    return done[2]


def evaluate(ctx: RefineContext, by_id: Mapping[str, Card], tree_for, budget_for,
             team_ids: tuple[str, tuple[str, ...]], *,
             plan_cache: PlanCache | None = None, per_chart: bool = False):
    """ONE exact evaluation — develop the team, score it on the context's
    charts, return the mean. Shared by the worker task and the in-process
    fallback so both compute the identical float.

    `plan_cache`: with a cache the board comes from the FIXED
    per-(character, role, budget, singer context) plans (`cached_team_plan`)
    assembled through `board_model_from_plan(plan_cache=...)`, and the
    deck-screen re-score `optimize_and_rerank` also computes (its ranking
    number, unused here) is skipped. Without one (default) the path is
    `optimize_and_rerank` per team, unchanged.

    SINGER CONTEXT (`sings`). The board plan is a function of
    (character, role, budget, singer context): a leader's conditional reds
    (R-002/044/062/063) and her solo/group yellow song bonus only fire on a song
    she is credited on, so a plan for such a song must value them and a plan for
    any other song must not (`board_plan.power_node_values`). This function
    develops the board ONCE and reuses it for every chart in `ctx.charts`, so the
    flag is only well defined when there is exactly ONE chart — song mode. For
    the sampled ACROSS mean (`len(ctx.charts) > 1`) there is no single cast: the
    leader sings some of the sampled charts and not others, and one board has to
    serve all of them. `sings=False` is used there deliberately — the
    song-agnostic plan, which is what the across objective means (each chart's
    own scorer still applies the conditional terms when the cast matches). A
    per-chart develop is the separate, ~len(charts)× more expensive
    `ctx.board_per_chart` objective. `ctx.charts == ()` (developed deck-screen Unit Score)
    is song-agnostic by definition, also False.

    `per_chart=True` returns each chart's score, in chart order, instead of their
    mean (the mean is their sum, added in that order, over their count: the same
    float). Used to choose the In general search's chart sample
    (`tools/web/ondemand.py`); not for the developed deck-screen score (no charts)."""
    # `team_ids` may carry a third element: indices into `ctx.charts`, the charts
    # THIS evaluation is scored on. One pool holding the whole library can then
    # answer for one song, a sample or all of them without a context per song
    # (`RefinePool.batch_score(chart_indices=)`); absent, it is every chart.
    # A fourth element overrides `ctx.refine_activation` for this evaluation, so
    # one pool can search plain and re-score its short list refined.
    leader_id, member_ids, *selection = team_ids
    if len(selection) > 1 and selection[1] is not None:
        ctx = dataclasses.replace(ctx, refine_activation=bool(selection[1]))
    charts = ctx.charts if not selection or selection[0] is None else         tuple(ctx.charts[i] for i in selection[0])
    team = Team(leader=by_id[leader_id],
                members=tuple(by_id[m] for m in member_ids))
    leader_chr = "chr-" + team.leader.id.split("-")[1]
    powers: dict[int, tuple[object, float]] = {}   # id(board) -> (that board, power)

    def song_score(chart, cfg) -> float:
        """One chart's score on the deck power `cfg` asks for: with
        `song_absolute_deck_power` (the default) the deck screen's own Overall
        Power, Memory Bonus and Member Power-Up included, as `song_rank._score_one`
        does; it is chart-invariant, so it is computed once per board."""
        power = None
        if cfg.song_absolute_deck_power:
            # keyed by identity AND holding the board, so a freed board's id
            # reused by a new one during the refinement can never alias
            hit = powers.get(id(cfg.board))
            if hit is None or hit[0] is not cfg.board:
                hit = powers[id(cfg.board)] = (
                    cfg.board, overall_power_for(tuple(team.members), team.leader, cfg))
            power = hit[1]
        s = score_song_detailed(team.members, (team.leader,), chart, cfg, deck_power=power)
        return s[0].total_score if s else 0.0

    def develop(sings: bool, charts_for=()):
        ctx_d = _with_singer_bench(ctx, by_id, tree_for, budget_for, plan_cache, sings)
        return _develop(ctx_d, sings, charts_for)

    def _develop(ctx, sings, charts_for):
        if ctx.board_state is not None:
            return board_from_state(
                team, ctx.board_state, cfg=ctx.cfg, skilltree=ctx.skilltree,
                node_effect_ids=ctx.node_effect_ids, tree_for=tree_for,
                connect_cards=ctx.connect_cards, bench_characters=ctx.bench_characters,
                name_by_chr=ctx.name_by_chr, sings=sings, use_slots=ctx.board_state_slots,
                socket_bench=ctx.socket_bench, bench_sockets=ctx.bench_sockets)[0]
        if ctx.refine_activation and charts_for:
            def charts_score(board):
                cfg_r = dataclasses.replace(ctx.cfg, board=board)
                total = 0.0
                for chart in charts_for:
                    total += song_score(chart, cfg_r)
                return total
            return develop_and_refine(
                team, ctx.cfg, charts_score, tree_for=tree_for, budget_for=budget_for,
                node_effect_ids=ctx.node_effect_ids, skilltree=ctx.skilltree,
                connect_cards=ctx.connect_cards, bench_characters=ctx.bench_characters,
                name_by_chr=ctx.name_by_chr, plan_cache=plan_cache, sings=sings,
                bench_sockets=ctx.bench_sockets)[0]
        if plan_cache is not None:
            return _cached_board(ctx, team, tree_for, budget_for, plan_cache, sings)
        return optimize_and_rerank(
            [team], ctx.cfg, tree_for=tree_for, budget_for=budget_for,
            node_effect_ids=ctx.node_effect_ids, skilltree=ctx.skilltree,
            connect_cards=ctx.connect_cards, bench_characters=ctx.bench_characters,
            name_by_chr=ctx.name_by_chr, sings=sings, bench_sockets=ctx.bench_sockets)[0].board

    if ctx.board_per_chart and charts:
        # One board per singer context, each chart scored on its own: the same
        # floats, added in chart order, as one single-chart evaluation per chart.
        configs: dict[frozenset, object] = {}
        total = 0.0
        scores: list[float] = []
        # keyed by WHICH of the team's holomems sing: the leader's reds and every
        # singing board's yellows (members included) follow the cast
        team_chrs = frozenset("chr-" + c.id.split("-")[1] for c in (team.leader, *team.members))
        # One chart (a song search): its WHOLE cast, so an off-team singer's yellows are
        # valued and her yellow-socket card held (`board_plan.bench_node_values`,
        # `held_bench_cards`; with only the team's singers an off-team singer's
        # yellow socket would be stripped, e.g. -2.29 % on m0131). Several charts keep the team-only key: one board per cast would
        # develop a board per chart. The cast carries the song's scope (`SongCast`):
        # the scorer pays only that scope's yellows, so it is part of the key.
        def key(c):
            singers = frozenset(c.singers) if len(charts) == 1 else team_chrs & frozenset(c.singers)
            return SongCast(singers, c.singer_type)
        for chart in charts:
            sings = key(chart)
            cfg_c = configs.get((sings, sings.scope))
            if cfg_c is None:
                same = tuple(c for c in charts
                             if (k := key(c)) == sings and k.scope == sings.scope)
                cfg_c = configs[(sings, sings.scope)] = dataclasses.replace(
                    ctx.cfg, board=develop(sings, same))
            s = song_score(chart, cfg_c)
            if per_chart:
                scores.append(s)
            total += s
        return scores if per_chart else total / len(charts)
    # one chart: its whole cast, so a singing MEMBER's yellows and yellow socket are valued
    # too (`board_plan.singer_weight_for`); several charts: song-agnostic, as always
    sings = song_cast(charts[0]) if len(charts) == 1 else False
    board = develop(sings, charts)
    cfg_x = dataclasses.replace(ctx.cfg, board=board)
    if not charts:
        # song-agnostic: the developed deck-screen Unit Score, the same number
        # optimize_and_rerank ranks by (bt.optimised_us) — recomputed here from
        # the board so this stays one code path with the chart metrics.
        return deck_screen_unit_score(score_with_breakdown(team, cfg_x)).unit_score
    total = 0.0
    scores = []
    for chart in charts:
        s = song_score(chart, cfg_x)
        if per_chart:
            scores.append(s)
        total += s
    return scores if per_chart else total / len(charts)


def board_from_state(team: Team, board_state: Mapping, *, cfg, skilltree: Mapping,
                     node_effect_ids: Mapping, tree_for, connect_cards=None,
                     bench_characters=None, name_by_chr=None, sings: bool = False,
                     plan_out: list | None = None, use_slots: bool = False,
                     socket_bench: bool = False, bench_sockets=None):
    """`(BoardModel, placements)` for `team` with each character's nodes taken
    from `board_state` (`{chr: {"nodes": [...], "slots": {...}}}`), assembled
    by `board_model_from_plan` exactly as the develop assembles a plan: the
    same linearisation point (the team board-off), Connect cards, bench boards,
    cast names and singer context. A character absent from the state keeps no
    developed board (its bench entry, if any, stands). Raises ValueError on a
    malformed state. `plan_out`, when given, receives the `TeamBoardPlan`.
    `use_slots`: the state is the ACCOUNT's, so each team board keeps the cards
    its `slots` name (`board_plan._fixed_connect_placements`) instead of the
    develop's assignment -- see `RefineContext.board_state_slots`.
    Lives here so the leaf can score boards as they are; `tools/web/row_detail.py`
    re-exports it."""
    if not isinstance(board_state, Mapping):
        raise ValueError("board_state must be an object {chr: {nodes, slots}}")
    per_character: dict[str, BoardPlan] = {}
    fixed_slots: dict[str, dict[str, str]] | None = {} if use_slots else None
    for card in (team.leader, *team.members):
        entry = board_state.get("chr-" + card.id.split("-")[1])
        if entry is None or card.id in per_character:
            continue
        nodes = entry.get("nodes") if isinstance(entry, Mapping) else entry
        if not isinstance(nodes, (list, tuple)):
            raise ValueError(f"board_state[chr-{card.id.split('-')[1]}].nodes must be a list")
        selected = frozenset(str(n) for n in nodes)
        if fixed_slots is not None and isinstance(entry, Mapping):
            fixed_slots["chr-" + card.id.split("-")[1]] = {
                str(k): str(v) for k, v in (entry.get("slots") or {}).items()}
        tree = tree_for(card)
        spent = tree.cost_of(selected - {"S-001"}) if tree is not None else 0
        per_character[card.id] = BoardPlan(selected=selected, spent=int(spent), value=0.0)
    plan = TeamBoardPlan(total_gain=0.0, per_character=per_character)
    if plan_out is not None:
        plan_out.append(plan)
    cfg_off = dataclasses.replace(cfg, board=None)
    bd = score_with_breakdown(team, cfg_off)
    ds = deck_screen_unit_score(bd)
    bare_pool = bd.active_pool / (1.0 + bd.combined_support_rate)
    placements: list[SocketPlacement] = []
    board = board_model_from_plan(
        plan, team.leader, team.members, skilltree, node_effect_ids,
        overall_power=ds.overall_power, bonus_permil=ds.score_bonus_permil,
        k_constant=UNIT_SCORE_K, active_pool_rate=bare_pool,
        connect_cards=connect_cards, placements_out=placements,
        bench_characters=bench_characters, name_by_chr=name_by_chr, sings=sings,
        fixed_slots=fixed_slots, socket_bench=socket_bench, bench_sockets=bench_sockets)
    return board, placements


def develop_team(ctx: RefineContext, team: Team, tree_for, budget_for, chart, *,
                 plan_cache: PlanCache | None = None, refine: bool | None = None):
    """The `BoardOptimisedTeam` (board, plan, sockets) behind ONE leaf
    evaluation of `team` on `chart` — what `evaluate` develops, kept whole so a
    DETAIL view can show the board, the slot order and the panel of the very
    number a row was ranked by (`tools/web/row_detail.py`).

    Same steps as `evaluate`'s `develop`, for one chart: the leader's singer
    context from the chart's cast; the plan cache when one is given; the
    activation refinement when `refine` (default `ctx.refine_activation`), on
    the same per-chart score `evaluate` uses. `tests` pin that the board this
    returns scores to `evaluate`'s float."""
    refine = ctx.refine_activation if refine is None else refine
    leader_chr = "chr-" + team.leader.id.split("-")[1]
    sings = song_cast(chart) if chart is not None else False
    if ctx.board_state is not None:
        # boards as they are: nothing developed, the same board `evaluate` scores
        plans: list = []
        board, placements = board_from_state(
            team, ctx.board_state, cfg=ctx.cfg, skilltree=ctx.skilltree,
            node_effect_ids=ctx.node_effect_ids, tree_for=tree_for,
            connect_cards=ctx.connect_cards, bench_characters=ctx.bench_characters,
            name_by_chr=ctx.name_by_chr, sings=sings, plan_out=plans,
            use_slots=ctx.board_state_slots, socket_bench=ctx.socket_bench,
            bench_sockets=ctx.bench_sockets)
        off = deck_screen_unit_score(score_with_breakdown(team, _board_off(ctx.cfg))).unit_score
        on = deck_screen_unit_score(score_with_breakdown(
            team, dataclasses.replace(ctx.cfg, board=board))).unit_score
        return BoardOptimisedTeam(team=team, board_off_us=off, optimised_us=on, plan=plans[0],
                                  board=board, sockets=tuple(placements))
    kwargs = dict(tree_for=tree_for, budget_for=budget_for,
                  node_effect_ids=ctx.node_effect_ids, skilltree=ctx.skilltree,
                  connect_cards=ctx.connect_cards, bench_characters=ctx.bench_characters,
                  name_by_chr=ctx.name_by_chr, plan_cache=plan_cache,
                  bench_sockets=ctx.bench_sockets)
    if refine and chart is not None:
        def charts_score(board):
            cfg_r = dataclasses.replace(ctx.cfg, board=board)
            power = (overall_power_for(tuple(team.members), team.leader, cfg_r)
                     if cfg_r.song_absolute_deck_power else None)
            s = score_song_detailed(team.members, (team.leader,), chart, cfg_r, deck_power=power)
            return s[0].total_score if s else 0.0
        return develop_refined_team(team, ctx.cfg, charts_score, sings=sings, **kwargs)[0]
    return optimize_and_rerank([team], ctx.cfg, sings=sings, **kwargs)[0]


# `(cfg, cfg with board=None)` for the last config seen: the board-off copy is
# the same for every team of a run, and building it was ~9 us of each leaf.
_BOARD_OFF: tuple = ()


def _board_off(cfg):
    global _BOARD_OFF
    if not _BOARD_OFF or _BOARD_OFF[0] is not cfg:
        _BOARD_OFF = (cfg, dataclasses.replace(cfg, board=None))
    return _BOARD_OFF[1]


def _cached_board(ctx: RefineContext, team: Team, tree_for, budget_for,
                  plan_cache: PlanCache, sings: bool):
    """The team's developed board on the plan cache — the same three steps
    `optimize_and_rerank(plan_cache=...)` takes (board-off breakdown for the
    linearisation point, `cached_team_plan`, `board_model_from_plan`), minus
    its final deck-screen re-score."""
    cfg_off = _board_off(ctx.cfg)
    bd = score_with_breakdown(team, cfg_off)
    ds = deck_screen_unit_score(bd)
    bare_pool = bd.active_pool / (1.0 + bd.combined_support_rate)
    plan = cached_team_plan(
        team.leader, team.members, cache=plan_cache, tree_for=tree_for,
        budget_for=budget_for, node_effect_ids=ctx.node_effect_ids,
        skilltree=ctx.skilltree, sings=sings)
    return board_model_from_plan(
        plan, team.leader, team.members, ctx.skilltree, ctx.node_effect_ids,
        overall_power=ds.overall_power, bonus_permil=ds.score_bonus_permil,
        k_constant=UNIT_SCORE_K, active_pool_rate=bare_pool,
        connect_cards=ctx.connect_cards, bench_characters=ctx.bench_characters,
        name_by_chr=ctx.name_by_chr, sings=sings, plan_cache=plan_cache,
        bench_sockets=ctx.bench_sockets)


def _chunksize(count: int, workers: int) -> int:
    """How many teams one worker takes per dispatch. One at a time was right
    when a leaf cost milliseconds; at ~0.5 ms the queue round-trip per team is
    a large share of the work (batching gives x3.5 with 16 workers on a
    72k-team batch). Small batches stay at 1 so a short, expensive batch is
    still spread over every worker; a score is a pure function of its team, so
    how the batch is cut cannot change a result."""
    return max(1, min(64, count // (max(1, workers) * 8)))


def _task(item: tuple[int, tuple[str, tuple[str, ...]]]) -> tuple[int, float]:
    index, team_ids = item
    assert _CTX is not None
    return index, evaluate(_CTX, _BY_ID, _tree_for, _budget_for, team_ids,
                           plan_cache=_worker_plan_cache())


def _task_per_chart(item):
    index, team_ids = item
    assert _CTX is not None
    return index, evaluate(_CTX, _BY_ID, _tree_for, _budget_for, team_ids,
                           plan_cache=_worker_plan_cache(), per_chart=True)


# ---------------------------------------------------------------------------
# Parent side.
# ---------------------------------------------------------------------------
class RefinePool:
    """A process pool that scores developed teams for `refine_members`'s
    `batch_score_fn`. Use as a context manager; `batch_score(teams)` returns
    one float per team, in order. `workers=1` (or a pool that fails to start)
    computes the same floats in-process."""

    def __init__(
        self,
        ctx: RefineContext,
        *,
        workers: int | None = None,
        on_fallback: Callable[[str], None] | None = None,
    ) -> None:
        self.ctx = ctx
        self.workers = default_workers() if workers is None else max(1, workers)
        self.on_fallback = on_fallback
        self._pool = None
        self._by_id = {c.id: c for c in ctx.cards}
        self._trees: dict = {}
        self._plan_cache: PlanCache | None = None
        self._stop = None           # shared with the workers (`_init_worker`)
        self._generation = 0

    def new_generation(self) -> int:
        """A number for the next batch of stoppable tasks (`stop_through`)."""
        self._generation += 1
        return self._generation

    def stop_through(self, generation: int) -> None:
        """Tell every worker to stop the tasks of `generation` and earlier: a task polls
        `stopped(its generation)` (no file, no message: one shared integer)."""
        if self._stop is not None and self._stop.value < generation:
            self._stop.value = generation

    # in-process equivalents of the worker's cached helpers
    def _tree_for(self, card: Card):
        model = model_for_character(self.ctx.skilltree, "chr-" + card.id.split("-")[1])
        if model is None:
            return None
        if model not in self._trees:
            self._trees[model] = skill_tree_from_dumps(
                self.ctx.skilltree, self.ctx.node_master, model)
        return self._trees[model]

    def _budget_for(self, card: Card) -> int:
        if self.ctx.flat_budget is not None:
            return self.ctx.flat_budget
        g = self.ctx.granted.get("chr-" + card.id.split("-")[1])
        return int(g) if g else 0

    def __enter__(self) -> "RefinePool":
        if self.workers > 1:
            try:
                context = multiprocessing.get_context("spawn")
                self._stop = context.RawValue("q", 0)
                self._pool = context.Pool(
                    processes=self.workers, initializer=_init_worker,
                    initargs=(self.ctx, self._stop))
            except Exception as exc:  # noqa: BLE001 — never a new answer
                self._pool = None
                if self.on_fallback:
                    self.on_fallback(
                        f"refine pool failed to start ({type(exc).__name__}: "
                        f"{exc}); refining in a single process")
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        if self._pool is not None:
            try:
                self._pool.terminate()
                self._pool.join()
            finally:
                self._pool = None

    def _serial(self, items: list[tuple[str, tuple[str, ...]]]) -> list[float]:
        if self.ctx.use_plan_cache and self._plan_cache is None:
            self._plan_cache = new_plan_cache(self.ctx, self._by_id)
        return [evaluate(self.ctx, self._by_id, self._tree_for, self._budget_for, t,
                         plan_cache=self._plan_cache)
                for t in items]

    def batch_score(self, teams: Sequence[Team],
                    chart_indices: Sequence[int] | None = None,
                    refine_activation: bool | None = None) -> list[float]:
        """One float per team, in order. `chart_indices` restricts THIS batch
        to those of the context's charts (see `evaluate`); `None` is all.
        `refine_activation` overrides the context's flag for this batch."""
        if refine_activation is not None:
            chosen = None if chart_indices is None else tuple(chart_indices)
            items = [(t.leader.id, tuple(m.id for m in t.members), chosen, refine_activation)
                     for t in teams]
        elif chart_indices is None:
            items = [(t.leader.id, tuple(m.id for m in t.members)) for t in teams]
        else:
            chosen = tuple(chart_indices)
            items = [(t.leader.id, tuple(m.id for m in t.members), chosen) for t in teams]
        if not items:
            return []
        if self._pool is None:
            return self._serial(items)
        out: list[float | None] = [None] * len(items)
        try:
            for index, score in self._pool.imap_unordered(
                _task, list(enumerate(items)),
                chunksize=_chunksize(len(items), self.workers)
            ):
                out[index] = score
        except Exception as exc:  # noqa: BLE001 — a dead worker is not an answer
            if self.on_fallback:
                self.on_fallback(
                    f"refine pool failed mid-batch ({type(exc).__name__}: {exc}); "
                    f"finishing this batch in a single process")
            self.close()
            return self._serial(items)
        if any(v is None for v in out):
            missing = [items[i] for i, v in enumerate(out) if v is None]
            fill = iter(self._serial(missing))
            out = [v if v is not None else next(fill) for v in out]
        return [float(v) for v in out]  # type: ignore[arg-type]

    def batch_score_per_chart(self, teams: Sequence[Team]) -> list[list[float]]:
        """Each team's score on EVERY chart of the context, in chart order (their
        mean is `batch_score(teams)`'s float). One list per team, in order."""
        items = [(t.leader.id, tuple(m.id for m in t.members)) for t in teams]
        if not items:
            return []
        if self._pool is not None:
            out: list = [None] * len(items)
            try:
                for index, scores in self._pool.imap_unordered(
                    _task_per_chart, list(enumerate(items)),
                    chunksize=_chunksize(len(items), self.workers)
                ):
                    out[index] = scores
                if all(v is not None for v in out):
                    return [[float(s) for s in v] for v in out]
            except Exception as exc:  # noqa: BLE001 — a dead worker is not an answer
                if self.on_fallback:
                    self.on_fallback(f"refine pool failed mid-batch ({type(exc).__name__}: {exc}); "
                                     f"finishing this batch in a single process")
                self.close()
        if self.ctx.use_plan_cache and self._plan_cache is None:
            self._plan_cache = new_plan_cache(self.ctx, self._by_id)
        return [[float(s) for s in evaluate(self.ctx, self._by_id, self._tree_for, self._budget_for,
                                            t, plan_cache=self._plan_cache, per_chart=True)]
                for t in items]
