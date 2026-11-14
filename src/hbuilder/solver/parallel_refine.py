"""The exact per-song leaf, across processes: same numbers, fewer seconds.

`evaluate` scores one team the way the web ranks it: the team's boards are
developed for the chart (`optimize_and_rerank`, or the plan cache) and the team
is scored on it (`score_song_detailed`); the result is the MEAN over the given
charts, so one pool serves the one-song and the across-songs searches alike.
`RefinePool` runs that over many teams: `spawn` context, the `RefineContext`
(cards, config, charts, board dumps, budget rule) loaded once per worker, results
in input order (`batch_score`, `batch_score_per_chart`). `SharedPool` lets several
contexts share one set of workers.

The answer cannot depend on the pool: each score is the same develop and score on
the same inputs in the same interpreter, so it is the same float. With one worker,
or when the pool cannot start, the batch is scored serially in this process with
the same functions.
"""

from __future__ import annotations

import atexit
import dataclasses
import multiprocessing
import os
import pickle
import re
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from multiprocessing.reduction import ForkingPickler
from typing import TYPE_CHECKING, Any, NamedTuple

from hbuilder.domain.board_optimizer import BoardPlan
from hbuilder.domain.board_plan import (
    UNIT_SCORE_K,
    PlanCache,
    SocketPlacement,
    SongCast,
    TeamBoardPlan,
    board_model_from_plan,
    cached_team_plan,
    forget_plan_cache,
    model_for_character,
    singer_bench_characters,
    skill_tree_from_dumps,
    song_cast,
)
from hbuilder.domain.board_refine import (
    BoardOptimisedTeam,
    cooldown_count_axes,
    cooldown_node_ids,
    develop_and_refine,
    develop_refined_team,
    optimize_and_rerank,
    plan_values,
    plan_with_counts,
    respend_freed_points,
)
from hbuilder.domain.card import Card
from hbuilder.domain.scoring import deck_screen_unit_score, score_with_breakdown
from hbuilder.domain.scoring_config import ScoringConfig
from hbuilder.domain.song import (
    OverallPower,
    SongChart,
    overall_power_board_key,
    overall_power_for,
    score_song_and_screen,
    score_song_detailed,
    screen_song_cooldown_variants,
)
from hbuilder.domain.song_rank import score_team_ceiling
from hbuilder.domain.team import Team
from hbuilder.solver.parallel import default_workers, one_blas_thread

if TYPE_CHECKING:
    from multiprocessing.pool import Pool

    from hbuilder.domain.board import BoardModel
    from hbuilder.domain.skilltree import SkillTree

# the workers this module starts inherit it (`parallel.one_blas_thread`)
one_blas_thread()

__all__ = [
    "RefinePool",
    "RefineContext",
    "SharedPool",
    "board_from_state",
    "develop_team",
    "evaluate",
    "new_plan_cache",
]


@dataclasses.dataclass(frozen=True)
class RefineContext:
    """Everything one exact evaluation needs, as picklable data. Built once by
    the caller (the web server), shipped once per worker."""

    cards: tuple[Card, ...]
    cfg: ScoringConfig  # board=None; each team gets its own
    charts: tuple[SongChart, ...]  # 1 chart (song), a sample (across), or
    # () -> the developed deck-screen Unit Score
    skilltree: Mapping
    node_master: Sequence[Mapping]
    node_effect_ids: Mapping[str, list[str]]
    connect_cards: Mapping | None
    bench_characters: Mapping | None
    name_by_chr: Mapping[str, str]
    flat_budget: int | None  # None -> per-character `granted`
    granted: Mapping[str, int]  # chr-id -> real earned points
    # PLAN CACHE. `False` (default): every evaluation develops its own board
    # through `optimize_and_rerank`. `True`: each process holds ONE `PlanCache`
    # (`new_plan_cache` from `plan_cache_seed`, a reference team as ids) and the leaf
    # is `cached_team_plan` + `board_model_from_plan(plan_cache=...)` + the chart
    # score: the objective as a pure function of the roster, ~3x cheaper. The seed
    # is required so the nominal context is the same in every worker, never the
    # first team a process happened to see.
    use_plan_cache: bool = False
    plan_cache_seed: tuple[str, tuple[str, ...]] | None = None
    # A planned search (boards re-planned, not as they are): each song's SINGERS'
    # bench boards are planned for it too (`board_plan.singer_bench_characters`), as
    # the materials re-plan plans them. Needs the plan cache; off for boards as they are.
    plan_singer_bench: bool = False
    # MANY charts, the board REBUILT PER SONG. `False` (default): several charts
    # share ONE board developed with `sings=False`, the plain across objective.
    # `True`: each chart is scored on the team's boards developed for ITS singer
    # context: the "in general" figure for a player who re-specs the board per song.
    # With several charts the context is the team's OWN singers of the chart: a singer
    # off the team keeps the board the bench gives her, where a single-song search plans
    # her board for the song too (`plan_singer_bench`). So the mean is not the mean of the
    # team's single-song scores: with the account's boards planned from scratch it is ~4.8 %
    # below it, nearly the same for every team (24 served teams x 231 charts: Spearman
    # 0.984, the same #1 and top 5); on the maxed basis, whose bench boards are complete,
    # 0.01 %. Planning every chart's whole cast would cost ~6-8x the evaluation for the
    # same ranking. A board state (`board_state`, e.g. a re-plan within the account's
    # materials) keeps the same nodes on every chart.
    board_per_chart: bool = False
    # ACTIVATION REFINEMENT. `True`: after the develop, the team's activation
    # nodes are chosen jointly on the score being computed
    # (`board_refine.refine_activation_nodes`). ~10-100x the cost of a plain
    # evaluation: for re-scoring a short list, not for a search. `2`: that, then
    # every combination of the members' frequency node COUNTS
    # (`board_refine.refine_cooldown_counts`, ~1,000 scorings): for a few finalists.
    refine_activation: bool | int = False
    # BOARDS AS THEY ARE (the "Optimize boards" switch off). `None` (default):
    # each team's boards are PLANNED. A `{chr: {"nodes": [...], "slots": {...}}}`
    # state: every team character's board is taken from it as it stands
    # (`board_from_state`) and nothing is planned.
    board_state: Mapping | None = None
    # `True`: `board_state` is the ACCOUNT's (`board_state.json`), so its `slots`
    # are the real socketed cards, taken as they stand (re-assigning them would put
    # the "as they are" Overall Power off the server). `False`: the develop's
    # matching assigns the sockets over the state's nodes (`slots: {}`).
    board_state_slots: bool = False
    # BENCH SOCKETS: `True` puts every off-team board's root socket in the
    # develop's Connect matching (`board_plan._bench_socket_rows`), so each card is used
    # once account-wide. ~48 extra rows a team: for short lists (the within-wallet
    # re-plan, the row detail).
    socket_bench: bool = False
    # EACH CARD IN ONE PLACE, DEALT WITH THE BENCH'S (needs `bench_sockets`): every board
    # assembled for a song has the team's Connect cards and the whole bench's in one matching
    # (`board_plan.board_model_from_plan(joint=True)`: the bench's side solved once per song on
    # the plan cache, the team's rows re-solved per team). Several charts: each chart's board is
    # assembled for its own cast (the plan stays the team's singer context's). One board for
    # several songs (no cast) keeps the bench's cards where they are (`held_bench_cards`).
    joint_connect: bool = False
    # THE BENCH'S SOCKETS AS THEY ARE: `{chr: {unlocked socket: card or None}}`, from
    # `board_state.json` on the account, from the maxed bench's own assignment on the maxed
    # basis (`board_plan.board_model_from_plan`'s `bench_sockets`).
    bench_sockets: Mapping | None = None
    # A folder the plan caches of this context's worker processes share (`PlanStore`),
    # set by `RefinePool` on worker processes, so a board plan is solved once per pool
    # instead of once per worker. None: every cache solves its own.
    plan_store: str | None = None
    # ONE BOARD FOR SEVERAL CHARTS, planned for a cast. With several charts and
    # `board_per_chart` off, the shared board is planned song-agnostic (`sings=False`) unless
    # this names the cast every chart shares (a `SongCast`, e.g. the holomem whose songs a
    # Holomem Rating team plays): then it is planned as for one of her songs, and each chart's
    # scorer still pays its own cast's conditional terms.
    board_cast: frozenset | None = None


class PlanStore:
    """`PlanCache.store` over a folder: one pickled `BoardPlan` per plan key, written
    whole to a private file and moved into place, so a reader sees a complete plan or
    none. A plan is a pure function of its key and of the cache's nominal context, which
    every cache of one `RefineContext` shares, so the first process to solve a plan
    serves it to the others. Any failure to read or write is a miss: the caller then
    solves the plan itself, which is the same plan."""

    def __init__(self, folder: str) -> None:
        self.folder = folder

    def _path(self, key: tuple) -> str:
        import hashlib  # noqa: PLC0415

        return os.path.join(self.folder, hashlib.sha1(repr(key).encode("utf-8")).hexdigest() + ".pkl")

    def get(self, key: tuple) -> BoardPlan | None:
        try:
            with open(self._path(key), "rb") as fh:
                stored_key, plan = pickle.load(fh)
        except (OSError, EOFError, pickle.UnpicklingError, ValueError):
            return None
        return plan if stored_key == key else None

    def put(self, key: tuple, plan: BoardPlan) -> None:
        tmp = None
        try:
            # a private temporary file in the store's folder, moved into place whole
            with tempfile.NamedTemporaryFile("wb", dir=self.folder, suffix=".tmp", delete=False) as fh:
                tmp = fh.name
                pickle.dump((key, plan), fh, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, self._path(key))
        except OSError:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def player_level(cfg: ScoringConfig) -> int | None:
    """The account's player level (`cfg.account`), which gates the board nodes a plan may buy
    (most carry a `cd-player_level-N` unlock); None, when unknown, gates nothing. The maxed
    basis carries the top level."""
    level = getattr(cfg.account, "level", None)
    return level if isinstance(level, int) else None


def new_plan_cache(ctx: RefineContext, by_id: Mapping[str, Card]) -> PlanCache:
    """The one `PlanCache` a process uses under `ctx.use_plan_cache`, on the context's
    shared `plan_store` when it has one. Its plans buy only nodes the account's player
    level unlocks (`player_level`)."""
    if ctx.plan_cache_seed is None:
        raise ValueError(
            "RefineContext.use_plan_cache needs plan_cache_seed (a reference team as (leader id, member ids))"
        )
    leader_id, member_ids = ctx.plan_cache_seed
    seed = Team(leader=by_id[leader_id], members=tuple(by_id[m] for m in member_ids))
    cache = PlanCache.from_team(seed, ctx.cfg, player_level=player_level(ctx.cfg))
    if ctx.plan_store:
        cache.store = PlanStore(ctx.plan_store)
    return cache


# ---------------------------------------------------------------------------
# Worker side. Module globals set once by the initializer (spawn gives every
# worker a fresh interpreter).
# ---------------------------------------------------------------------------
_CTX: RefineContext | None = None
_BY_ID: dict[str, Card] = {}
_TREES: dict = {}
_PLAN_CACHE: PlanCache | None = None
# the pool's shared stop mark (`RefinePool.stop_through`): tasks of a generation at or
# below it stop early (a cancelled search's exact plans, `materials_plan.dispatch`). In a
# `SharedPool` worker it is one mark per context (`_SLOT`, the active context's).
_STOP = None
_SLOT: int | None = None
# A `SharedPool` worker's contexts, least recently used first: `{token: [ctx, by_id,
# trees, plan cache]}`, the active one (`_TOKEN`) in the globals above. A token is
# `(file the context is pickled to, stop slot)`.
_CONTEXTS: dict = {}
_TOKEN: tuple | None = None
# the contexts a worker keeps loaded (an engine's each, with its plan cache)
CONTEXTS_HELD = 3


def _init_worker(ctx: RefineContext | str, stop=None) -> None:
    """A worker's context: the context itself, or the file `RefinePool` pickled it to once."""
    global _CTX, _BY_ID, _TREES, _PLAN_CACHE, _STOP
    if isinstance(ctx, str):
        with open(ctx, "rb") as fh:
            ctx = pickle.load(fh)
    _CTX = ctx  # type: ignore[assignment]
    _BY_ID = {c.id: c for c in ctx.cards}  # type: ignore[union-attr]
    _TREES = {}
    _PLAN_CACHE = None
    _STOP = stop


def _init_shared(stop) -> None:
    """A `SharedPool` worker: no context yet, each task names its own (`_activate`)."""
    global _STOP
    _STOP = stop


def _activate(token: tuple) -> None:
    """Make `token`'s context this worker's (`_CTX`, `_BY_ID`, `_TREES`, `_PLAN_CACHE`),
    loading it from its file the first time; the one it replaces is kept, with its plan
    cache and trees, up to `CONTEXTS_HELD` contexts."""
    global _CTX, _BY_ID, _TREES, _PLAN_CACHE, _TOKEN, _SLOT
    if token == _TOKEN:
        return
    if _TOKEN is not None and _TOKEN in _CONTEXTS:
        _CONTEXTS[_TOKEN] = [_CTX, _BY_ID, _TREES, _PLAN_CACHE]
    held = _CONTEXTS.pop(token, None)
    if held is None:
        with open(token[0], "rb") as fh:
            ctx = pickle.load(fh)
        held = [ctx, {c.id: c for c in ctx.cards}, {}, None]
    _CONTEXTS[token] = held
    while len(_CONTEXTS) > CONTEXTS_HELD:
        dropped = _CONTEXTS.pop(next(iter(_CONTEXTS)))
        forget_context(dropped[0], dropped[3])
    _CTX, _BY_ID, _TREES, _PLAN_CACHE = held
    _TOKEN, _SLOT = token, token[1]


def forget_context(ctx: RefineContext, plan_cache: PlanCache | None) -> None:
    """A context this process no longer keeps: the singer-bench memos' entries that hold it
    or its plan cache go with it (each entry keeps its context alive, and the memos are
    only emptied by newer entries)."""
    for key in [k for k, done in _SINGER_CTX.items() if done[0] is ctx]:
        del _SINGER_CTX[key]
    if plan_cache is not None:
        forget_plan_cache(plan_cache)


def run_in(token: tuple, fn: Callable[[Any], Any], item: Any) -> Any:
    """`fn(item)` in a `SharedPool` worker, on `token`'s context."""
    _activate(token)
    return fn(item)


def stopped(generation: int) -> bool:
    """In a worker: has the parent stopped this generation of tasks?"""
    if _STOP is None or generation is None:
        return False
    if _SLOT is not None:
        return _STOP[_SLOT] >= generation
    return _STOP.value >= generation


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
# (id(context), id(singer bench)) -> (context, bench, the context on that bench), the least
# recently used dropped beyond SINGER_CONTEXTS_HELD (as many as the benches the plan cache
# holds, `board_plan._BENCHES_HELD`)
_SINGER_CTX: dict = {}
SINGER_CONTEXTS_HELD = 128


def _with_singer_bench(ctx, by_id, tree_for, budget_for, plan_cache, sings):
    """`ctx` with its singers' bench boards planned for the song (`plan_singer_bench`,
    a `SongCast` with a cast, the plan cache, boards not as they are), else `ctx`."""
    if (
        not ctx.plan_singer_bench
        or plan_cache is None
        or ctx.board_state is not None
        or not isinstance(sings, SongCast)
        or not sings
    ):
        return ctx
    hit = _CARDS_BY_CHR.get(id(by_id))
    if hit is None or hit[0] is not by_id:
        cards: dict = {}
        for card_id in sorted(by_id):
            cards.setdefault("chr-" + card_id.split("-")[1], by_id[card_id])
        _CARDS_BY_CHR.clear()
        hit = _CARDS_BY_CHR[id(by_id)] = (by_id, cards)
    bench = singer_bench_characters(
        ctx.bench_characters,
        sings,
        cache=plan_cache,
        cards_by_chr=hit[1],
        tree_for=tree_for,
        budget_for=budget_for,
        node_effect_ids=ctx.node_effect_ids,
        skilltree=ctx.skilltree,
        name_by_chr=ctx.name_by_chr,
    )
    if bench is ctx.bench_characters:
        return ctx
    # one copy of the context per (context, bench) pair, both kept alive in the entry: a
    # search goes back and forth between a few dozen singer benches
    key = (id(ctx), id(bench))
    done = _SINGER_CTX.pop(key, None)
    if done is None or done[0] is not ctx or done[1] is not bench:
        while len(_SINGER_CTX) >= SINGER_CONTEXTS_HELD:
            del _SINGER_CTX[next(iter(_SINGER_CTX))]
        done = (ctx, bench, dataclasses.replace(ctx, bench_characters=bench))
    _SINGER_CTX[key] = done
    return done[2]


def evaluate(
    ctx: RefineContext,
    by_id: Mapping[str, Card],
    tree_for: Callable[[Card], SkillTree | None],
    budget_for: Callable[[Card], int],
    team_ids: tuple,
    *,
    plan_cache: PlanCache | None = None,
    per_chart: bool = False,
) -> Any:
    """ONE exact evaluation: develop the team, score it on the context's charts,
    return the mean. Shared by the worker task and the in-process fallback, so both
    compute the identical float.

    `plan_cache`: the board comes from the fixed per-(character, role, budget,
    singer context) plans (`cached_team_plan`) assembled through
    `board_model_from_plan(plan_cache=...)`; without one, `optimize_and_rerank`.

    Singer context: conditional reds and solo/group yellows pay only on a song the
    holomem sings, so with ONE chart the board is planned for its cast. With several
    charts one board serves them all and is planned song-agnostic (`sings=False`;
    each chart's scorer still applies the conditional terms when the cast matches),
    unless `ctx.board_per_chart` develops one board per singer context. No charts
    (the developed deck-screen Unit Score) is song-agnostic too.

    `per_chart=True` returns each chart's score, in chart order, instead of their
    mean (the mean is their sum, added in that order, over their count: the same
    float). Not for the developed deck-screen score (no charts)."""
    # `team_ids` may carry a third element: indices into `ctx.charts`, the charts
    # THIS evaluation is scored on. One pool holding the whole library can then
    # answer for one song, a sample or all of them without a context per song
    # (`RefinePool.batch_score(chart_indices=)`); absent, it is every chart.
    # A fourth element overrides `ctx.refine_activation` for this evaluation, so
    # one pool can search plain and re-score its short list refined.
    # A fifth, true, returns the CEILING instead of the expectation: the run score if
    # every ACTIVE fires, at the slot order the expectation plays
    # (`song_rank.score_team_ceiling`), on the very board the expectation is scored on --
    # the top of the row's run law, for a ranking sorted by Max. A sixth, `(team board
    # state, bench)`, is the board a row re-planned within the materials is served on
    # (`materials_plan.plan_and_score` scores it the same way).
    leader_id, member_ids, *selection = team_ids
    # "screen": the plain develop, then `count_screen`'s cheap estimate of what choosing
    # the members' frequency node counts would add -- an ORDER for the refinement, never
    # a served number
    count_screen = len(selection) > 1 and selection[1] == "screen"
    # "lens": the plain develop's score with the best single-member frequency count drop
    # (`lens_value`), the search's count-aware value; "lens_both": `(plain, lens)`;
    # "screen_both": `(plain, count screen)`.
    # One chart only; orders, never served numbers
    lens = len(selection) > 1 and selection[1] in ("lens", "lens_both", "screen_both")
    # COUNTS: the plain develop of each singer context, and each chart scored with the
    # members' frequency node counts chosen for it and verified on their own board
    # (`ChartCounts`): a served number, the mean over the charts as always. COUNTS_BOTH:
    # `(the plain mean, that mean)`, what choosing the counts adds on these charts
    counts_both = len(selection) > 1 and selection[1] == COUNTS_BOTH
    counts = counts_both or (len(selection) > 1 and selection[1] == COUNTS)
    if len(selection) > 1 and selection[1] == SEARCH:
        ctx = dataclasses.replace(ctx, refine_activation=False, joint_connect=False)
    elif count_screen or lens or counts:
        ctx = dataclasses.replace(ctx, refine_activation=False)
    elif len(selection) > 1 and selection[1] is not None:
        ctx = dataclasses.replace(ctx, refine_activation=refine_level(selection[1]))
    ceiling = len(selection) > 2 and bool(selection[2])
    # "both": the expectation AND the ceiling at its slot order, from the one board, as
    # `(expectation, ceiling)` means (a refinement too dear to develop twice)
    both = len(selection) > 2 and selection[2] == "both"
    if len(selection) > 3 and selection[3] is not None:
        team_state, bench = selection[3]
        ctx = dataclasses.replace(ctx, board_state=team_state, bench_characters=bench, socket_bench=True)
        plan_cache = None
    charts = (
        ctx.charts if not selection or selection[0] is None else tuple(ctx.charts[i] for i in selection[0])
    )
    team = Team(leader=by_id[leader_id], members=tuple(by_id[m] for m in member_ids))
    powers: dict[int, tuple[object, OverallPower]] = {}  # id(board) -> (that board, power)
    # what a board gives the Overall Power -> that power: the team's singer contexts each
    # have a board of their own, and about half of them agree on everything the power
    # reads (`overall_power_board_key`)
    powers_by_content: dict[tuple, OverallPower] = {}
    # the team with no board, the point every one of its boards is assembled at
    # (`_cached_board`): one for all its singer contexts
    board_off_point: list = []
    # the team's plan per singer context, for the charts of several casts that share one
    team_plans: dict = {}
    # the team prepared once per board for all the charts scored on it
    # (`score_song_detailed`'s `team_setup`); one chart has nothing to share
    setups: dict | None = {} if len(charts) > 1 else None

    def power_of(cfg):
        """The deck power `cfg` asks for: with `song_absolute_deck_power` (the default)
        the deck screen's own Overall Power, Memory Bonus and Member Power-Up included,
        as `song_rank._score_one` does; chart-invariant, so computed once per board."""
        if not cfg.song_absolute_deck_power:
            return None
        # keyed by identity AND holding the board, so a freed board's id
        # reused by a new one during the refinement can never alias; a
        # `with_cooldowns` copy shares its source's power (cooldowns never enter it)
        key = getattr(cfg.board, "power_identity", cfg.board)
        hit = powers.get(id(key))
        if hit is None or hit[0] is not key:
            content = (
                None
                if cfg.board is None
                else overall_power_board_key(tuple(team.members), team.leader, cfg.board)
            )
            power = powers_by_content.get(content) if content is not None else None
            if power is None:
                power = overall_power_for(tuple(team.members), team.leader, cfg)
                if content is not None:
                    powers_by_content[content] = power
            hit = powers[id(key)] = (key, power)
        return hit[1]

    def song_score(chart, cfg) -> float:
        """One chart's score on `power_of(cfg)`."""
        s = score_song_detailed(
            team.members, (team.leader,), chart, cfg, deck_power=power_of(cfg), team_setup=setups
        )
        return s[0].total_score if s else 0.0

    def served(chart, cfg) -> float:
        """The number this evaluation returns for one chart: the expectation, or with
        `ceiling` the all-fire score at the expectation's slot order."""
        if not ceiling:
            return song_score(chart, cfg)
        power = None
        if cfg.song_absolute_deck_power:
            power = overall_power_for(tuple(team.members), team.leader, cfg)
        s = score_song_detailed(team.members, (team.leader,), chart, cfg, deck_power=power)
        return score_team_ceiling(team, chart, cfg, s[0].member_order) if s else 0.0

    def develop(sings: bool | AbstractSet[str], charts_for=(), assemble_cast=None):
        ctx_d = _with_singer_bench(ctx, by_id, tree_for, budget_for, plan_cache, sings)
        return _develop(ctx_d, sings, charts_for, assemble_cast)

    def _develop(ctx, sings, charts_for, assemble_cast=None):
        if ctx.board_state is not None:
            return board_from_state(
                team,
                ctx.board_state,
                cfg=ctx.cfg,
                skilltree=ctx.skilltree,
                node_effect_ids=ctx.node_effect_ids,
                tree_for=tree_for,
                connect_cards=ctx.connect_cards,
                bench_characters=ctx.bench_characters,
                name_by_chr=ctx.name_by_chr,
                sings=sings,
                use_slots=ctx.board_state_slots,
                socket_bench=ctx.socket_bench,
                bench_sockets=ctx.bench_sockets,
            )[0]
        if ctx.refine_activation and charts_for:

            def charts_score(board):
                cfg_r = dataclasses.replace(ctx.cfg, board=board)
                total = 0.0
                for chart in charts_for:
                    total += song_score(chart, cfg_r)
                return total

            def charts_score_variants(board, variants):
                # `charts_score(board.with_cooldowns(v))` for every `v`: the same sums, in
                # chart order, from one cooldown-free build per chart
                cfg_r = dataclasses.replace(ctx.cfg, board=board)
                totals = [0.0] * len(variants)
                for chart in charts_for:
                    power = power_of(cfg_r)
                    for i, s in enumerate(
                        screen_song_cooldown_variants(
                            team.members, team.leader, chart, cfg_r, variants, deck_power=power
                        )
                    ):
                        totals[i] += s
                return totals

            return develop_and_refine(
                team,
                ctx.cfg,
                charts_score,
                tree_for=tree_for,
                budget_for=budget_for,
                node_effect_ids=ctx.node_effect_ids,
                skilltree=ctx.skilltree,
                connect_cards=ctx.connect_cards,
                bench_characters=ctx.bench_characters,
                name_by_chr=ctx.name_by_chr,
                plan_cache=plan_cache,
                # an assembly cast plans the team's boards as `sings` does (a team holomem's plan
                # reads only whether she sings) and assembles them for its whole cast
                sings=sings if assemble_cast is None else assemble_cast,
                bench_sockets=ctx.bench_sockets,
                cooldown_counts=refine_level(ctx.refine_activation) >= 2,
                charts_score_variants=charts_score_variants,
                joint_connect=ctx.joint_connect,
            )[0]
        if plan_cache is not None:
            return _cached_board(
                ctx, team, tree_for, budget_for, plan_cache, sings, board_off_point, assemble_cast, team_plans
            )
        return optimize_and_rerank(
            [team],
            ctx.cfg,
            tree_for=tree_for,
            budget_for=budget_for,
            node_effect_ids=ctx.node_effect_ids,
            skilltree=ctx.skilltree,
            connect_cards=ctx.connect_cards,
            bench_characters=ctx.bench_characters,
            name_by_chr=ctx.name_by_chr,
            sings=sings,
            bench_sockets=ctx.bench_sockets,
            player_level=player_level(ctx.cfg),
            joint=ctx.joint_connect,
        )[0].board

    def expectation_and_ceiling(chart, cfg) -> tuple[float, float]:
        """`served` without and with `ceiling`, on the one board `cfg` carries."""
        e = song_score(chart, cfg)
        power = (
            overall_power_for(tuple(team.members), team.leader, cfg) if cfg.song_absolute_deck_power else None
        )
        s = score_song_detailed(team.members, (team.leader,), chart, cfg, deck_power=power)
        return e, (score_team_ceiling(team, chart, cfg, s[0].member_order) if s else 0.0)

    if ctx.board_per_chart and charts:
        # One board per singer context, each chart scored on its own, added in chart order.
        # With one chart these are the floats of a single-chart evaluation; with several,
        # the context is the team's own singers (`key` below), not the whole cast.
        configs: dict[tuple[frozenset, str], ScoringConfig] = {}
        total = 0.0
        scores: list[float] = []
        # keyed by WHICH of the team's holomems sing: the leader's reds and every
        # singing board's yellows (members included) follow the cast
        team_chrs = frozenset("chr-" + c.id.split("-")[1] for c in (team.leader, *team.members))

        # One chart (a song search): its WHOLE cast, so an off-team singer's yellows are
        # valued and her yellow-socket card held (`board_plan.bench_node_values`,
        # `held_bench_cards`). Several charts keep the team-only key: one board per cast
        # would develop a board per chart. The cast carries the song's scope (`SongCast`):
        # the scorer pays only that scope's yellows, so it is part of the key.
        def key(c):
            singers = frozenset(c.singers) if len(charts) == 1 else team_chrs & frozenset(c.singers)
            return SongCast(singers, c.singer_type)

        # the cast a chart's board is ASSEMBLED for: the team's singers' key, or with the bench
        # dealt in (`joint_connect`) the chart's whole cast, whose bench yellows the deal values
        def assembled_for(c):
            return SongCast(frozenset(c.singers), c.singer_type) if ctx.joint_connect else key(c)

        # COUNTS: each planned singer context's `ChartCounts` (boards as they are have no
        # count to choose), and the members' count axes, shared by the contexts whose
        # boards agree
        choosers: dict = {}
        axes_memo: dict = {}
        respend_memo: dict = {}
        plain_total = 0.0
        for chart in charts:
            sings = key(chart)
            cast = assembled_for(chart)
            cfg_c = configs.get((cast, cast.scope))
            if cfg_c is None:
                same = tuple(c for c in charts if (k := assembled_for(c)) == cast and k.scope == cast.scope)
                if counts and ctx.board_state is None:
                    ctx_d = _with_singer_bench(ctx, by_id, tree_for, budget_for, plan_cache, sings)
                    plan, board, assemble = _planned_board(
                        ctx_d,
                        team,
                        tree_for,
                        budget_for,
                        plan_cache,
                        sings,
                        board_off_point,
                        cast,
                        team_plans,
                    )
                    cfg_c = dataclasses.replace(ctx.cfg, board=board)
                    choosers[(cast, cast.scope)] = ChartCounts(
                        team,
                        cfg_c,
                        plan,
                        assemble,
                        tree_for=tree_for,
                        budget_for=budget_for,
                        skilltree=ctx.skilltree,
                        power_of=power_of,
                        axes_memo=axes_memo,
                        team_setup=setups,
                        values_for=_plan_values_or_none(ctx, team, plan, plan_cache),
                        player_level=player_level(ctx.cfg),
                        respend_memo=respend_memo,
                    )
                else:
                    cfg_c = dataclasses.replace(ctx.cfg, board=develop(sings, same, cast))
                configs[(cast, cast.scope)] = cfg_c
            if both:
                e, c = expectation_and_ceiling(chart, cfg_c)
                scores.append(c)
                total += e
                continue
            if lens:
                if len(charts) != 1:
                    raise ValueError("the lens and screen modes score one chart")
                plain, best = lens_value(team, chart, cfg_c, power_of(cfg_c))
                if selection[1] == "lens":
                    return best
                if selection[1] == "lens_both":
                    return plain, best
                return plain, count_screen_value(team, chart, cfg_c, plain, power_of(cfg_c))
            chooser = choosers.get((cast, cast.scope))
            if chooser is not None:
                plain_c, s, *_counts = chooser.choose(chart)
            else:
                plain_c = s = served(chart, cfg_c)
            plain_total += plain_c
            if count_screen:
                s = count_screen_value(team, chart, cfg_c, s, power_of(cfg_c))
            if per_chart:
                scores.append(s)
            total += s
        if both:
            # the ceilings added in chart order from 0, as a `ceiling` evaluation adds them
            return total / len(charts), sum(scores) / len(charts)
        if counts_both:
            return plain_total / len(charts), total / len(charts)
        return scores if per_chart else total / len(charts)
    if counts:
        raise ValueError(
            "the per-chart frequency counts need a board per chart (`RefineContext.board_per_chart`)"
        )
    # one chart: its whole cast, so a singing MEMBER's yellows and yellow socket are valued
    # too (`board_plan.singer_weight_for`); several charts: song-agnostic, or planned for the
    # cast they share (`RefineContext.board_cast`)
    if len(charts) == 1:
        sings = song_cast(charts[0])
    else:
        sings = ctx.board_cast if ctx.board_cast is not None else False
    board = develop(sings, charts)
    cfg_x = dataclasses.replace(ctx.cfg, board=board)
    if not charts:
        # song-agnostic: the developed deck-screen Unit Score, the same number
        # optimize_and_rerank ranks by (bt.optimised_us) — recomputed here from
        # the board so this stays one code path with the chart metrics.
        return deck_screen_unit_score(score_with_breakdown(team, cfg_x)).unit_score
    if both:
        expect = top = 0.0
        for chart in charts:
            e, c = expectation_and_ceiling(chart, cfg_x)
            expect += e
            top += c
        return expect / len(charts), top / len(charts)
    if lens:
        if len(charts) != 1:
            raise ValueError("the lens and screen modes score one chart")
        plain, best = lens_value(team, charts[0], cfg_x, power_of(cfg_x))
        if selection[1] == "lens":
            return best
        if selection[1] == "lens_both":
            return plain, best
        return plain, count_screen_value(team, charts[0], cfg_x, plain, power_of(cfg_x))
    total = 0.0
    scores = []
    for chart in charts:
        s = served(chart, cfg_x)
        if count_screen:
            s = count_screen_value(team, chart, cfg_x, s, power_of(cfg_x))
        if per_chart:
            scores.append(s)
        total += s
    return scores if per_chart else total / len(charts)


def board_from_state(
    team: Team,
    board_state: Mapping,
    *,
    cfg: ScoringConfig,
    skilltree: Mapping,
    node_effect_ids: Mapping,
    tree_for: Callable[[Card], SkillTree | None],
    connect_cards: Mapping[str, tuple[Sequence[Sequence[int]], int]] | None = None,
    bench_characters: Mapping[str, Sequence[Mapping]] | None = None,
    name_by_chr: Mapping[str, str] | None = None,
    sings: bool | AbstractSet[str] = False,
    plan_out: list | None = None,
    use_slots: bool = False,
    socket_bench: bool = False,
    bench_sockets: Mapping[str, Mapping[str, str | None]] | None = None,
) -> tuple[BoardModel, list[SocketPlacement]]:
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
                str(k): str(v) for k, v in (entry.get("slots") or {}).items()
            }
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
        fixed_slots=fixed_slots,
        socket_bench=socket_bench,
        bench_sockets=bench_sockets,
    )
    return board, placements


def develop_team(
    ctx: RefineContext,
    team: Team,
    tree_for: Callable[[Card], SkillTree | None],
    budget_for: Callable[[Card], int],
    chart: SongChart | None,
    *,
    plan_cache: PlanCache | None = None,
    refine: bool | int | None = None,
) -> BoardOptimisedTeam:
    """The `BoardOptimisedTeam` (board, plan, sockets) behind ONE leaf
    evaluation of `team` on `chart` — what `evaluate` develops, kept whole so a
    DETAIL view can show the board, the slot order and the panel of the very
    number a row was ranked by (`tools/web/row_detail.py`).

    Same steps as `evaluate`'s `develop`, for one chart: the leader's singer
    context from the chart's cast; the plan cache when one is given; the
    activation refinement when `refine` (default `ctx.refine_activation`), on
    the same per-chart score `evaluate` uses. `tests` pin that the board this
    returns scores to `evaluate`'s float, the song's off-team singers' boards planned
    for it as `evaluate` plans them (`_with_singer_bench`)."""
    refine = ctx.refine_activation if refine is None else refine
    sings = song_cast(chart) if chart is not None else False
    ctx = _with_singer_bench(ctx, {c.id: c for c in ctx.cards}, tree_for, budget_for, plan_cache, sings)
    if ctx.board_state is not None:
        # boards as they are: nothing developed, the same board `evaluate` scores
        plans: list = []
        board, placements = board_from_state(
            team,
            ctx.board_state,
            cfg=ctx.cfg,
            skilltree=ctx.skilltree,
            node_effect_ids=ctx.node_effect_ids,
            tree_for=tree_for,
            connect_cards=ctx.connect_cards,
            bench_characters=ctx.bench_characters,
            name_by_chr=ctx.name_by_chr,
            sings=sings,
            plan_out=plans,
            use_slots=ctx.board_state_slots,
            socket_bench=ctx.socket_bench,
            bench_sockets=ctx.bench_sockets,
        )
        off = deck_screen_unit_score(score_with_breakdown(team, _board_off(ctx.cfg))).unit_score
        on = deck_screen_unit_score(
            score_with_breakdown(team, dataclasses.replace(ctx.cfg, board=board))
        ).unit_score
        return BoardOptimisedTeam(
            team=team,
            board_off_us=off,
            optimised_us=on,
            plan=plans[0],
            board=board,
            sockets=tuple(placements),
        )
    kwargs: dict[str, Any] = dict(
        tree_for=tree_for,
        budget_for=budget_for,
        node_effect_ids=ctx.node_effect_ids,
        skilltree=ctx.skilltree,
        connect_cards=ctx.connect_cards,
        bench_characters=ctx.bench_characters,
        name_by_chr=ctx.name_by_chr,
        plan_cache=plan_cache,
        bench_sockets=ctx.bench_sockets,
        player_level=player_level(ctx.cfg),
    )
    if refine == COUNTS and chart is not None:
        # the frequency node counts chosen on this chart, as `evaluate` chooses them
        # (`ChartCounts`); the plain develop when no count drop scores higher
        plan, board, assemble = _planned_board(ctx, team, tree_for, budget_for, plan_cache, sings)
        cfg_plain = dataclasses.replace(ctx.cfg, board=board)
        powers: dict = {}

        def power_at(cfg_r):
            if not cfg_r.song_absolute_deck_power:
                return None
            hit = powers.get(id(cfg_r.board))
            if hit is None or hit[0] is not cfg_r.board:
                hit = powers[id(cfg_r.board)] = (
                    cfg_r.board,
                    overall_power_for(tuple(team.members), team.leader, cfg_r),
                )
            return hit[1]

        chooser = ChartCounts(
            team,
            cfg_plain,
            plan,
            assemble,
            tree_for=tree_for,
            budget_for=budget_for,
            skilltree=ctx.skilltree,
            power_of=power_at,
            values_for=_plan_values_or_none(ctx, team, plan, plan_cache),
            player_level=player_level(ctx.cfg),
        )
        choice = chooser.choose(chart)
        if choice.counts is not None:
            placements: list[SocketPlacement] = []  # type: ignore[no-redef]
            chosen = chooser.plan_at(choice.counts, choice.respent)
            board = assemble(chosen, placements)
            cfg_off = _board_off(ctx.cfg)
            return BoardOptimisedTeam(
                team=team,
                board_off_us=deck_screen_unit_score(score_with_breakdown(team, cfg_off)).unit_score,
                optimised_us=deck_screen_unit_score(
                    score_with_breakdown(team, dataclasses.replace(cfg_off, board=board))
                ).unit_score,
                plan=chosen,
                board=board,
                sockets=tuple(placements),
            )
        refine = False
    if refine and chart is not None:
        powers: dict[int, tuple] = {}  # type: ignore[no-redef]  # as `evaluate`'s: per board, copies share theirs

        def power_of(cfg_r):
            if not cfg_r.song_absolute_deck_power:
                return None
            key = getattr(cfg_r.board, "power_identity", cfg_r.board)
            hit = powers.get(id(key))
            if hit is None or hit[0] is not key:
                hit = powers[id(key)] = (key, overall_power_for(tuple(team.members), team.leader, cfg_r))
            return hit[1]

        def charts_score(board):
            cfg_r = dataclasses.replace(ctx.cfg, board=board)
            s = score_song_detailed(team.members, (team.leader,), chart, cfg_r, deck_power=power_of(cfg_r))
            return s[0].total_score if s else 0.0

        def charts_score_variants(board, variants):
            cfg_r = dataclasses.replace(ctx.cfg, board=board)
            return screen_song_cooldown_variants(
                team.members, team.leader, chart, cfg_r, variants, deck_power=power_of(cfg_r)
            )

        return develop_refined_team(
            team,
            ctx.cfg,
            charts_score,
            sings=sings,
            cooldown_counts=refine_level(refine) >= 2,
            charts_score_variants=charts_score_variants,
            joint_connect=ctx.joint_connect,
            **kwargs,
        )[0]
    return optimize_and_rerank([team], ctx.cfg, sings=sings, joint=ctx.joint_connect, **kwargs)[0]


COUNT_SCREEN_ROUNDS = 3


def _plan_values_or_none(ctx: RefineContext, team: Team, plan: TeamBoardPlan, plan_cache: PlanCache | None):
    """`board_refine.plan_values` of a plan the cache made (the node values each board was
    planned with), for spending again the points a dropped frequency node frees; None
    without the cache."""
    if plan_cache is None or not plan.plan_keys:
        return None
    cards = {c.id: c for c in (team.leader, *team.members)}
    return plan_values(plan, cards, plan_cache, ctx.node_effect_ids, ctx.skilltree)


def _cooldown_counts(team: Team, board) -> tuple[dict, dict]:
    """`({holomem: cooldown shortening per frequency node}, {holomem: its node count})` of
    the members holding frequency (`cooldown_shorten`) nodes on `board`; a member's count
    is its nodes, each taken at the board's mean value per node."""
    unit, have = {}, {}
    for holomem in (m.member for m in team.members):
        nodes = getattr(board.characters.get(holomem), "nodes", ()) or ()
        count = sum(1 for node in nodes for e in node.effects if e.kind == "cooldown_shorten")
        if count:
            unit[holomem] = board.uptime_for(holomem)[1] / count
            have[holomem] = count
    return unit, have


def lens_value(
    team: Team, chart: SongChart, cfg: ScoringConfig, power: OverallPower | None
) -> tuple[float, float]:
    """`(plain, lens)`: the exact score of the developed board (`cfg.board`), and the best
    of it and its every single-member frequency count drop (each member's every lower count,
    estimated by `song.screen_song_cooldown_variants`), from one set-up of the team on the
    chart. The search ranks by the lens: the plain board buys every frequency node, which
    ranks some teams up to ~5 % low and keeps them from ever being refined (a team two
    swaps from the plain #1 that wants nine nodes dropped). An order, never served."""
    board = cfg.board
    unit, have = _cooldown_counts(team, board) if board is not None else ({}, {})
    if not have:
        s = score_song_detailed(team.members, (team.leader,), chart, cfg, deck_power=power)
        plain = s[0].total_score if s else 0.0
        return plain, plain
    full = {x: unit[x] * have[x] for x in have}
    variants = [{**full, h: unit[h] * c} for h in have for c in range(have[h])]
    plain_score, values = score_song_and_screen(
        team.members, team.leader, chart, cfg, variants, deck_power=power
    )
    plain = plain_score.total_score
    return plain, max([plain, *values])


def count_screen_value(
    team: Team, chart: SongChart, cfg: ScoringConfig, plain: float, power: OverallPower | None
) -> float:
    """A cheap estimate of the team's score with its members' frequency node counts chosen:
    from the developed board (`cfg.board`), up to COUNT_SCREEN_ROUNDS rounds of the best
    single-member count drop, each round every member's every lower count scored at once
    (`song.screen_song_cooldown_variants`, estimated, no board re-assembled). A member's count is its
    `cooldown_shorten` nodes on the board, each taken at the board's mean value per node.
    For ORDERING the refinement only: the refinement scores what it keeps on real boards.
    Returns at least `plain`."""
    board = cfg.board
    if board is None:
        return plain
    unit, have = _cooldown_counts(team, board)
    if not have:
        return plain
    current, best = dict(have), plain
    for _round in range(COUNT_SCREEN_ROUNDS):
        moves = [(h, c) for h in have for c in range(current[h]) if c != current[h]]
        if not moves:
            break
        variants = [{**{x: unit[x] * current[x] for x in have}, h: unit[h] * c} for h, c in moves]
        values = screen_song_cooldown_variants(
            team.members, team.leader, chart, cfg, variants, deck_power=power
        )
        top = max(range(len(values)), key=lambda i: (values[i], -i))
        if values[top] <= best:
            break
        best = values[top]
        current[moves[top][0]] = moves[top][1]
    return best


def refine_level(refine: bool | int | None) -> int:
    """A refinement flag as its level: 0 none, 1 the activation nodes move by move
    (`refine_activation_nodes`), 2 that and every combination of the members'
    activation-frequency node counts (`refine_cooldown_counts`). `True` is 1; a row's
    `refined` field carries the level its value was served at."""
    if refine is None or refine is False:
        return 0
    if refine is True:
        return 1
    level = int(refine)
    if level not in (0, 1, 2):
        raise ValueError(f"refine level must be 0, 1 or 2, got {refine!r}")
    return level


# `(cfg, cfg with board=None)` for the last config seen: the board-off copy is
# the same for every team of a run, and building it costs ~9 us per leaf.
_BOARD_OFF: tuple = ()


def _board_off(cfg):
    global _BOARD_OFF
    if not _BOARD_OFF or _BOARD_OFF[0] is not cfg:
        _BOARD_OFF = (cfg, dataclasses.replace(cfg, board=None))
    return _BOARD_OFF[1]


def _cached_board(
    ctx: RefineContext,
    team: Team,
    tree_for,
    budget_for,
    plan_cache: PlanCache,
    sings: bool,
    off_point: list | None = None,
    assemble_cast=None,
    plans: dict | None = None,
):
    """The team's developed board on the plan cache — the same three steps
    `optimize_and_rerank(plan_cache=...)` takes (board-off breakdown for the
    linearisation point, `cached_team_plan`, `board_model_from_plan`), minus
    its final deck-screen re-score.

    `off_point`: a list the caller keeps for ONE team. The linearisation point is the
    team scored with no board, the same for every singer context the team's boards are
    developed for, so it is computed for the first and read back for the others."""
    return _planned_board(
        ctx, team, tree_for, budget_for, plan_cache, sings, off_point, assemble_cast, plans
    )[1]


def _planned_board(
    ctx: RefineContext,
    team: Team,
    tree_for,
    budget_for,
    plan_cache: PlanCache | None,
    sings,
    off_point: list | None = None,
    assemble_cast=None,
    plans: dict | None = None,
):
    """`(plan, board, assemble)` of the team's plain develop for `sings`: its
    `TeamBoardPlan`, the board assembled from it, and `assemble(another plan of the team[,
    placements out])`, which assembles that plan the same way (same linearisation point,
    Connect cards, bench and singer context) -- a plan with some nodes moved is taken by its
    nodes. `assemble_cast` (a `SongCast`): the boards are assembled for that cast instead
    of `sings` (planned for `sings`), as `RefineContext.joint_connect` assembles each chart
    of several. With `joint_connect` every board's Connect cards are dealt together with the
    bench's. On the plan cache the plan is the cached one (`_cached_board`'s steps); without
    one, `optimize_and_rerank`'s. `off_point`: `_cached_board`'s. `plans`: a dict the caller
    keeps for ONE team, the plan per singer context (charts of several casts that share one
    are assembled from one plan)."""
    cfg_off = _board_off(ctx.cfg)
    if off_point and off_point[0] is cfg_off:
        _cfg, overall_power, bonus_permil, bare_pool = off_point
    else:
        bd = score_with_breakdown(team, cfg_off)
        ds = deck_screen_unit_score(bd)
        overall_power, bonus_permil = ds.overall_power, ds.score_bonus_permil
        bare_pool = bd.active_pool / (1.0 + bd.combined_support_rate)
        if off_point is not None:
            off_point[:] = [cfg_off, overall_power, bonus_permil, bare_pool]

    cast = sings if assemble_cast is None else assemble_cast

    def assemble(plan, placements: list | None = None):
        return board_model_from_plan(
            plan,
            team.leader,
            team.members,
            ctx.skilltree,
            ctx.node_effect_ids,
            overall_power=overall_power,
            bonus_permil=bonus_permil,
            k_constant=UNIT_SCORE_K,
            active_pool_rate=bare_pool,
            connect_cards=ctx.connect_cards,
            placements_out=placements,
            bench_characters=ctx.bench_characters,
            name_by_chr=ctx.name_by_chr,
            sings=cast,
            plan_cache=plan_cache,
            bench_sockets=ctx.bench_sockets,
            joint=ctx.joint_connect,
        )

    if plan_cache is None:
        developed = optimize_and_rerank(
            [team],
            ctx.cfg,
            tree_for=tree_for,
            budget_for=budget_for,
            node_effect_ids=ctx.node_effect_ids,
            skilltree=ctx.skilltree,
            connect_cards=ctx.connect_cards,
            bench_characters=ctx.bench_characters,
            name_by_chr=ctx.name_by_chr,
            sings=sings,
            bench_sockets=ctx.bench_sockets,
            player_level=player_level(ctx.cfg),
            joint=ctx.joint_connect,
        )[0]
        if assemble_cast is not None:
            return developed.plan, assemble(developed.plan), assemble
        return developed.plan, developed.board, assemble
    plan_key = (sings, getattr(sings, "scope", None))
    plan = plans.get(plan_key) if plans is not None else None
    if plan is None:
        plan = cached_team_plan(
            team.leader,
            team.members,
            cache=plan_cache,
            tree_for=tree_for,
            budget_for=budget_for,
            node_effect_ids=ctx.node_effect_ids,
            skilltree=ctx.skilltree,
            sings=sings,
        )
        if plans is not None:
            plans[plan_key] = plan
    return plan, assemble(plan), assemble


# the node ids of the activation-frequency family of the last skill tree dump seen
_COOLDOWN_NODES: tuple = ()


def _cooldown_nodes(skilltree) -> frozenset:
    global _COOLDOWN_NODES
    if not _COOLDOWN_NODES or _COOLDOWN_NODES[0] is not skilltree:
        _COOLDOWN_NODES = (skilltree, cooldown_node_ids(skilltree))
    return _COOLDOWN_NODES[1]


# a row's `refined` when its frequency node counts were chosen chart by chart
# (`ChartCounts`), and the refinement flag that asks for it; COUNTS_BOTH asks for
# `(plain mean, that mean)`
COUNTS = "counts"
COUNTS_BOTH = "counts_both"
# SEARCH: the plain develop with the bench's Connect cards where they are
# (`RefineContext.joint_connect` off for this evaluation): one board per singer context of
# the team instead of one per chart cast, for the values a search over several charts only
# ORDERS by; the rows it serves are scored with the deal
SEARCH = "search"


class CountsChoice(NamedTuple):
    """`ChartCounts.choose`'s answer for one chart: the plain board's score, the score served,
    the counts it is served at (None: the plain board's), and whether on the board with the
    freed points spent again."""

    plain: float
    served: float
    counts: tuple | None
    respent: bool


class ChartCounts:
    """One team on one developed board: each chart's score with the members'
    activation-frequency node counts chosen FOR THAT CHART.

    The plain develop buys every frequency node, and what a shorter cooldown is worth is a
    matter of where the windows then fall on the chart, so the best counts differ from
    song to song. Per chart: up to COUNT_SCREEN_ROUNDS rounds of the best single-member
    count drop, every member's every lower count estimated at once on the plain board
    (`song.screen_song_cooldown_variants`); then the counts chosen are VERIFIED: their own
    board is assembled (once per combination, kept) and the chart scored on it exactly.
    The value is that exact score or the plain one, whichever is higher, so it is always a
    score measured on a real board and never below the plain develop's.

    A member's counts are the ones its board reaches by dropping frequency nodes that
    nothing hangs from (`board_refine.cooldown_count_axes`). With `values_for` (the node
    values each board was planned with, `board_refine.plan_values`) the points a dropped
    node frees are spent again (`board_refine.respend_freed_points`, the counts kept), and
    that board is scored too: the chart is served on whichever of the two scores higher.
    Without it, or when nothing is left to buy (the maxed budget), they stay unspent."""

    def __init__(
        self,
        team: Team,
        cfg: ScoringConfig,
        plan: TeamBoardPlan,
        assemble: Callable[[TeamBoardPlan], BoardModel | None],
        *,
        tree_for: Callable[[Card], SkillTree | None],
        budget_for: Callable[[Card], int],
        skilltree: Mapping | None,
        power_of: Callable[[ScoringConfig], OverallPower | None],
        axes_memo: dict | None = None,
        team_setup: dict | None = None,
        values_for: Callable[[str], Mapping[str, float] | None] | None = None,
        player_level: int | None = None,
        respend_memo: dict | None = None,
    ) -> None:
        self.team, self.cfg, self.plan = team, cfg, plan  # `cfg.board`: the plain board
        self._assemble, self._tree_for, self._budget_for = assemble, tree_for, budget_for
        self._power_of, self._setup = power_of, team_setup
        self._values_for, self._player_level = values_for, player_level
        self._frozen = _cooldown_nodes(skilltree)
        self._respend_memo = respend_memo if respend_memo is not None else {}
        self._cards = {c.id: c for c in (team.leader, *team.members)}
        members = {c.id: c for c in team.members}
        board = cfg.board
        self.axes = (
            []
            if board is None
            else cooldown_count_axes(
                plan,
                self._cards,
                tree_for=tree_for,
                budget_for=budget_for,
                cooldown_nodes=_cooldown_nodes(skilltree),
                member_ids=frozenset(members),
                drops_only=True,
                memo=axes_memo,
            )
        )
        self._holomems = [members[card_id].member for card_id, _states, _here in self.axes]
        # each member's cooldown shortening as its board has it, and per frequency node
        self._own = [board.uptime_for(h)[1] for h in self._holomems]  # type: ignore[union-attr]
        self._unit = [own / here for own, (_cid, _states, here) in zip(self._own, self.axes)]
        self.start = tuple(
            next(k for k, (count, _sel) in enumerate(states) if count == here)
            for _cid, states, here in self.axes
        )
        # (counts chosen, freed points spent) -> the config on their own board
        self._boards: dict = {}

    def _cooldowns(self, combo) -> dict:
        """`{holomem: cooldown shortening}` of the plain board with the members at `combo`."""
        return {
            h: (own if k == k0 else unit * states[k][0])
            for h, own, unit, (_cid, states, _here), k, k0 in zip(
                self._holomems, self._own, self._unit, self.axes, combo, self.start
            )
        }

    def _respent(self, combo: tuple[int, ...]) -> tuple[TeamBoardPlan, bool]:
        """`(the plan with the members at combo and the freed points spent again, whether
        anything was bought)`."""
        plan = plan_with_counts(self.plan, self.axes, combo, self._cards, self._tree_for)
        if self._values_for is None:
            return plan, False
        grown, bought = respend_freed_points(
            self.plan,
            plan,
            self._cards,
            tree_for=self._tree_for,
            budget_for=self._budget_for,
            values_for=self._values_for,
            frozen=self._frozen,
            player_level=self._player_level,
            memo=self._respend_memo,
        )
        return grown, bool(bought)

    def plan_at(self, combo: tuple[int, ...], respent: bool = False) -> TeamBoardPlan:
        """The plan with the members at `combo`; `respent`: with the points the dropped
        nodes free spent again (the same nodes when there is nothing to buy)."""
        if respent:
            return self._respent(combo)[0]
        return plan_with_counts(self.plan, self.axes, combo, self._cards, self._tree_for)

    def config_at(self, combo: tuple[int, ...], respent: bool = False) -> ScoringConfig | None:
        """The config whose board is `plan_at(combo, respent)`, assembled; None when it
        cannot be. When spending the freed points buys nothing it is `config_at(combo)`
        itself."""
        key = (combo, respent)
        if key not in self._boards:
            if respent:
                plan, bought = self._respent(combo)
                if not bought:
                    self._boards[key] = self.config_at(combo)
                    return self._boards[key]
            else:
                plan = self.plan_at(combo)
            board = self._assemble(plan)
            self._boards[key] = None if board is None else dataclasses.replace(self.cfg, board=board)
        return self._boards[key]

    def _exact(self, chart, cfg) -> float:
        s = score_song_detailed(
            self.team.members,
            (self.team.leader,),
            chart,
            cfg,
            deck_power=self._power_of(cfg),
            team_setup=self._setup,
        )
        return s[0].total_score if s else 0.0

    def choose(self, chart: SongChart) -> CountsChoice:
        """The counts and the board `chart` is served on (`CountsChoice`); the counts are None
        (and `respent` False) when the plain board's are kept. The board served is
        `config_at(counts, respent)`."""
        return CountsChoice(*self._choose_counts(chart))

    def _choose_counts(self, chart: SongChart) -> tuple[float, float, tuple | None, bool]:
        """`(plain score, score served, the counts it is served at, whether on the board
        with the freed points spent again)` on `chart`, the bench's cards where they are."""
        team, cfg = self.team, self.cfg
        if not self.axes:
            plain = self._exact(chart, cfg)
            return plain, plain, None, False
        power = self._power_of(cfg)
        current = list(self.start)
        plain = best = 0.0
        for round_ in range(COUNT_SCREEN_ROUNDS):
            moves = [(a, k) for a in range(len(self.axes)) for k in range(current[a])]
            if not moves and round_:
                break
            variants = [
                self._cooldowns([k if b == a else current[b] for b in range(len(current))]) for a, k in moves
            ]
            if round_ == 0:
                # the plain board's exact score and the first round's estimates, one set-up
                score, values = score_song_and_screen(
                    team.members, team.leader, chart, cfg, variants, deck_power=power, team_setup=self._setup
                )
                plain = best = score.total_score
            else:
                values = screen_song_cooldown_variants(
                    team.members, team.leader, chart, cfg, variants, deck_power=power, team_setup=self._setup
                )
            if not values:
                break
            top = max(range(len(values)), key=lambda i: (values[i], -i))
            if values[top] <= best:
                break
            best = values[top]
            current[moves[top][0]] = moves[top][1]
        combo = tuple(current)
        if combo == self.start:
            return plain, plain, None, False
        cfg_v = self.config_at(combo)
        exact = self._exact(chart, cfg_v) if cfg_v is not None else plain
        respent = False
        cfg_r = self.config_at(combo, True)
        if cfg_r is not cfg_v and cfg_r is not None:
            exact_r = self._exact(chart, cfg_r)
            if exact_r > exact:
                exact, respent = exact_r, True
        return (plain, exact, combo, respent) if exact > plain else (plain, plain, None, False)


def _chunksize(count: int, workers: int) -> int:
    """How many teams one worker takes per dispatch. At ~0.5 ms a leaf, the queue
    round-trip per team is a large share of the work (batching gives x3.5 with 16
    workers on a 72k-team batch). Small batches stay at 1 so a short, expensive batch
    is still spread over every worker; a score is a pure function of its team, so how
    the batch is cut cannot change a result."""
    return max(1, min(64, count // (max(1, workers) * 8)))


def _task(item: tuple[int, tuple[str, tuple[str, ...]]]) -> tuple[int, float]:
    index, team_ids = item
    assert _CTX is not None
    return index, evaluate(_CTX, _BY_ID, _tree_for, _budget_for, team_ids, plan_cache=_worker_plan_cache())


def _task_per_chart(item):
    index, team_ids = item
    assert _CTX is not None
    return index, evaluate(
        _CTX, _BY_ID, _tree_for, _budget_for, team_ids, plan_cache=_worker_plan_cache(), per_chart=True
    )


def _ctx_task(item):
    """`_task` in a `SharedPool` worker: `(token, index, team ids)`."""
    token, *rest = item
    _activate(token)
    return _task(tuple(rest))


def _ctx_task_per_chart(item):
    """`_task_per_chart` in a `SharedPool` worker: `(token, index, team ids)`."""
    token, *rest = item
    _activate(token)
    return _task_per_chart(tuple(rest))


# ---------------------------------------------------------------------------
# Parent side.
# ---------------------------------------------------------------------------
# --------------------------------------------------------------------------- temp files
# A pool's temp files (the pickled context its workers read, the shared plan store) carry the
# pid of the process that made them. `RefinePool.close` removes them; exit removes those of a
# pool never closed (atexit); and a process that died before either leaves them to the next
# process's sweep (`sweep_stale_temp`), which removes every one whose process is gone. Each
# context file holds the account's cards (~6 MB): left behind, they pile up by the gigabyte.
_TEMP_NAME = re.compile(r"^(hbctx|hbplans)-(\d+)-")
# names from before the pid was in them (`hbctx_*`, `hbplans_*`): no process can still need one
# this old
LEGACY_TEMP_AGE_S = 2 * 86400
_OWNED_TEMP: set[str] = set()
_TEMP_LOCK = threading.Lock()
_SWEEP_STARTED = False


def _temp_prefix(kind: str) -> str:
    return f"{kind}-{os.getpid()}-"


def _remove_path(path: str) -> bool:
    """Remove a temp file or folder; False when it is still there (a worker holding it)."""
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            return False
    return not os.path.exists(path)


def _own_temp(path: str) -> str:
    with _TEMP_LOCK:
        _OWNED_TEMP.add(path)
    return path


def _drop_temp(path: str) -> None:
    """Remove one of this process's temp files; one that cannot go yet is left to exit."""
    if _remove_path(path):
        with _TEMP_LOCK:
            _OWNED_TEMP.discard(path)


@atexit.register
def _remove_owned_temp() -> None:
    with _TEMP_LOCK:
        owned = list(_OWNED_TEMP)
        _OWNED_TEMP.clear()
    for path in owned:
        _remove_path(path)


def _new_context_file(ctx: object) -> str:
    """`ctx` pickled ONCE to a temp file the workers read (an initializer argument would be
    pickled per worker). Also starts the sweep of earlier processes' leftovers."""
    sweep_stale_temp_once()
    with tempfile.NamedTemporaryFile(
        "wb", prefix=_temp_prefix("hbctx"), suffix=".pickle", delete=False
    ) as fh:
        _own_temp(fh.name)
        fh.write(ForkingPickler.dumps(ctx))
    return fh.name


def _new_store_dir() -> str:
    """A fresh folder for a context's shared plan store."""
    return _own_temp(tempfile.mkdtemp(prefix=_temp_prefix("hbplans")))


def pid_alive(pid: int) -> bool:
    """Whether a process with this pid is running (an exited one is not)."""
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        import ctypes  # noqa: PLC0415

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists, not ours
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # signal 0 on POSIX: only asks whether the process exists
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sweep_stale_temp(folder: str | None = None, *, now: float | None = None) -> list[str]:
    """Remove the pool temp files in `folder` (default: the system temp folder) that no
    running process owns: a name whose pid no process holds, or a name from before the pid
    was in it once older than LEGACY_TEMP_AGE_S. Answers the paths removed."""
    folder = folder or tempfile.gettempdir()
    now = time.time() if now is None else now
    alive: dict[int, bool] = {}
    removed: list[str] = []
    try:
        names = os.listdir(folder)
    except OSError:
        return removed
    for name in names:
        path = os.path.join(folder, name)
        match = _TEMP_NAME.match(name)
        if match:
            pid = int(match.group(2))
            if pid not in alive:
                alive[pid] = pid_alive(pid)
            stale = not alive[pid]
        elif name.startswith(("hbctx_", "hbplans_")):
            try:
                stale = now - os.path.getmtime(path) > LEGACY_TEMP_AGE_S
            except OSError:
                continue
        else:
            continue
        if stale and _remove_path(path):
            removed.append(path)
    return removed


def sweep_stale_temp_once() -> None:
    """`sweep_stale_temp` in a background thread, once per process (the server's start, or
    the first pool)."""
    global _SWEEP_STARTED
    with _TEMP_LOCK:
        if _SWEEP_STARTED:
            return
        _SWEEP_STARTED = True
    threading.Thread(target=sweep_stale_temp, name="temp-sweep", daemon=True).start()


# ----------------------------------------------------------------- waiting on the workers
class WorkerDied(RuntimeError):
    """A worker process ended while its tasks were out: their answers will never come."""


# how often a wait on the workers looks whether one of them has ended (seconds)
WORKER_WATCH_S = 1.0


def worker_pids(pool: Any) -> frozenset[int]:
    """The pids of a multiprocessing pool's worker processes. The pool replaces a worker that
    ends, and none ends on its own here (no `maxtasksperchild`): a pid that leaves this set
    is a worker that died (killed, out of memory, a crash in native code)."""
    procs = getattr(pool, "_pool", None)
    if not isinstance(procs, list):  # not a multiprocessing pool (a thread pool, a test's)
        return frozenset()
    return frozenset(p.pid for p in list(procs))


def _run_chunk(job: tuple[Callable, list]) -> list:
    """In a worker: one chunk of a batch, `task` on each of its items, as the pool's own
    chunking (`mapstar`) runs one."""
    task, items = job
    return [task(item) for item in items]


def _gather(pool: Any, task: Callable, tasks: list, chunksize: int, out: list) -> None:
    """Fill `out[index]` from `task` on each of `tasks`, in the pool, as the answers arrive.

    The batch goes out in chunks of `chunksize` cut here: the pool's own chunking answers
    through a generator, which cannot wait with a timeout. Raises `WorkerDied` when a worker
    ends meanwhile: its chunk's answer never comes, and a plain iteration would wait for it
    forever. A task that raised raises here too."""
    pids = worker_pids(pool)
    size = max(1, chunksize)
    chunks = [(task, tasks[at : at + size]) for at in range(0, len(tasks), size)]
    answers = pool.imap_unordered(_run_chunk, chunks, chunksize=1)
    while True:
        try:
            done = answers.next(timeout=WORKER_WATCH_S)
        except StopIteration:
            return
        except multiprocessing.TimeoutError:
            if worker_pids(pool) != pids:
                raise WorkerDied("a worker process ended while it held tasks of this batch") from None
            continue
        for index, value in done:
            out[index] = value


class SharedPool:
    """ONE set of worker processes for several contexts (`RefinePool(shared=...)`): each
    pool built on it pickles its own context to a file, and a worker loads a context the
    first time a task of it arrives, keeping the `CONTEXTS_HELD` last ones (each with its
    plan cache). Two engines then share the workers and the per-process caches (the board
    planner's Oracle, the exact plans) instead of holding a worker each per core.

    Each context stops its own tasks (`RefinePool.stop_through`): one stop mark per pool in
    a shared array, so cancelling one search never stops another's. Generations are counted
    across the pools, so a mark is never reused for an older batch."""

    SLOTS = 64

    def __init__(self, workers: int | None = None) -> None:
        import threading  # noqa: PLC0415

        self.workers = default_workers() if workers is None else max(1, workers)
        self._pool: Pool | None = None
        self._stop = None
        self._lock = threading.Lock()
        self._generation = 0
        self._free = list(range(self.SLOTS))
        self.failed: str | None = None

    def pool(self) -> Pool | None:
        """The worker processes, started on first use; None when they cannot start."""
        with self._lock:
            if self._pool is None and self.failed is None and self.workers > 1:
                try:
                    context = multiprocessing.get_context("spawn")
                    self._stop = context.RawArray("q", self.SLOTS)
                    self._pool = context.Pool(
                        processes=self.workers, initializer=_init_shared, initargs=(self._stop,)
                    )
                except Exception as exc:  # noqa: BLE001 — never a new answer
                    self._pool = None
                    self.failed = f"{type(exc).__name__}: {exc}"
            return self._pool

    def acquire(self) -> int | None:
        """A stop slot for a new context, marked at the current generation (a slot used
        before must not let an old batch of it run on); None when all are taken."""
        with self._lock:
            if not self._free or self._stop is None:
                return None
            slot = self._free.pop(0)
            self._stop[slot] = self._generation
            return slot

    def release(self, slot: int | None) -> None:
        if slot is not None:
            with self._lock:
                if self._stop is not None:
                    self._stop[slot] = self._generation
                self._free.append(slot)

    def new_generation(self) -> int:
        with self._lock:
            self._generation += 1
            return self._generation

    def stop_through(self, slot: int, generation: int) -> None:
        with self._lock:
            if self._stop is not None and self._stop[slot] < generation:
                self._stop[slot] = generation

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                try:
                    self._pool.terminate()
                    self._pool.join()
                finally:
                    self._pool = None


class RefinePool:
    """A process pool that scores developed teams. Use as a context manager;
    `batch_score(teams)` returns one float per team, in order. `workers=1` (or a
    pool that fails to start) computes the same floats in-process. `shared`: run on
    a `SharedPool`'s workers instead of starting its own (closing it leaves them)."""

    def __init__(
        self,
        ctx: RefineContext,
        *,
        workers: int | None = None,
        on_fallback: Callable[[str], None] | None = None,
        shared: SharedPool | None = None,
    ) -> None:
        self.ctx = ctx
        self.shared = shared
        if shared is not None:
            workers = shared.workers
        self.workers = default_workers() if workers is None else max(1, workers)
        self.on_fallback = on_fallback
        # what went wrong with the workers since the last `take_warnings` (a search reports it)
        self.warnings: list[str] = []
        self._pool: Pool | None = None
        self._by_id = {c.id: c for c in ctx.cards}
        self._trees: dict = {}
        self._plan_cache: PlanCache | None = None
        self._stop = None  # shared with the workers (`_init_worker`)
        self._ctx_file: str | None = None
        self._store_dir: str | None = None  # the workers' shared plan store (`PlanStore`)
        self._generation = 0
        self._token: tuple | None = None  # on a SharedPool: (context file, stop slot)

    def new_generation(self) -> int:
        """A number for the next batch of stoppable tasks (`stop_through`)."""
        if self._token is not None:
            return self.shared.new_generation()  # type: ignore[union-attr]
        self._generation += 1
        return self._generation

    def stop_through(self, generation: int) -> None:
        """Tell every worker to stop the tasks of `generation` and earlier: a task polls
        `stopped(its generation)` (no file, no message: one shared integer)."""
        if self._token is not None:
            self.shared.stop_through(self._token[1], generation)  # type: ignore[union-attr]
            return
        if self._stop is not None and self._stop.value < generation:
            self._stop.value = generation

    def submit(
        self,
        fn: Callable[[Any], Any],
        item: Any,
        *,
        callback: Callable[[Any], object] | None,
        error_callback: Callable[[BaseException], object] | None,
    ) -> None:
        """`fn(item)` on a worker, asynchronously (`materials_plan.dispatch`): on this
        pool's context, which a shared worker activates first."""
        if self._token is not None:
            self._pool.apply_async(  # type: ignore[union-attr]
                run_in, (self._token, fn, item), callback=callback, error_callback=error_callback
            )
        else:
            self._pool.apply_async(fn, (item,), callback=callback, error_callback=error_callback)  # type: ignore[union-attr]

    def _items(self, items: list, task, ctx_task) -> tuple:
        """`(task, task items)` for `imap_unordered`: on a shared pool each item names
        this pool's context."""
        if self._token is not None:
            return ctx_task, [(self._token, k, it) for k, it in enumerate(items)]
        return task, list(enumerate(items))

    # in-process equivalents of the worker's cached helpers
    def _tree_for(self, card: Card):
        model = model_for_character(self.ctx.skilltree, "chr-" + card.id.split("-")[1])
        if model is None:
            return None
        if model not in self._trees:
            self._trees[model] = skill_tree_from_dumps(self.ctx.skilltree, self.ctx.node_master, model)
        return self._trees[model]

    def _budget_for(self, card: Card) -> int:
        if self.ctx.flat_budget is not None:
            return self.ctx.flat_budget
        g = self.ctx.granted.get("chr-" + card.id.split("-")[1])
        return int(g) if g else 0

    def _with_store(self) -> None:
        """Give the context a fresh shared plan store folder, before it is pickled for
        the workers (a plan cache of the context is only used through the workers)."""
        if isinstance(self.ctx, RefineContext) and self.ctx.use_plan_cache and self._store_dir is None:
            try:
                self._store_dir = _new_store_dir()
            except OSError:
                return
            self.ctx = dataclasses.replace(self.ctx, plan_store=self._store_dir)

    def __enter__(self) -> RefinePool:
        if self.shared is not None:
            workers = self.shared.pool() if self.workers > 1 else None
            if workers is not None:
                self._with_store()
                try:
                    self._ctx_file = _new_context_file(self.ctx)
                    slot = self.shared.acquire()
                    if slot is not None:
                        self._token, self._pool = (self._ctx_file, slot), workers
                except Exception:  # noqa: BLE001 — never a new answer
                    self._token = self._pool = None
            if self._pool is None and self.workers > 1 and self.on_fallback:
                self.on_fallback("the shared refine pool is not available; refining in a single process")
            return self
        if self.workers > 1:
            self._with_store()
            try:
                context = multiprocessing.get_context("spawn")
                self._stop = context.RawValue("q", 0)
                # the context (~6 MB with a difficulty's charts) pickled ONCE, to a file each
                # worker reads: as an initializer argument it would be pickled per worker and
                # each start would wait for that worker to read it (16 workers: ~6.7 s before
                # the pool exists, against ~0.3 s)
                self._ctx_file = _new_context_file(self.ctx)
                self._pool = context.Pool(
                    processes=self.workers, initializer=_init_worker, initargs=(self._ctx_file, self._stop)
                )
            except Exception as exc:  # noqa: BLE001 — never a new answer
                self._pool = None
                if self.on_fallback:
                    self.on_fallback(
                        f"refine pool failed to start ({type(exc).__name__}: "
                        f"{exc}); refining in a single process"
                    )
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        forget_context(self.ctx, self._plan_cache)
        if self._token is not None:
            # the workers are the shared pool's: only this context's mark and file go
            self.shared.release(self._token[1])  # type: ignore[union-attr]
            self._token = None
            self._pool = None
        if self.shared is not None:
            self._pool = None
        if self._pool is not None:
            try:
                self._pool.terminate()
                self._pool.join()
            finally:
                self._pool = None
        if self._ctx_file is not None:
            _drop_temp(self._ctx_file)  # one a worker still holds goes at exit
            self._ctx_file = None
        if self._store_dir is not None:
            _drop_temp(self._store_dir)
            self._store_dir = None

    def _serial(self, items: list[tuple[str, tuple[str, ...]]]) -> list[float]:
        if self.ctx.use_plan_cache and self._plan_cache is None:
            self._plan_cache = new_plan_cache(self.ctx, self._by_id)
        return [
            evaluate(self.ctx, self._by_id, self._tree_for, self._budget_for, t, plan_cache=self._plan_cache)
            for t in items
        ]

    def batch_score(
        self,
        teams: Sequence[Team],
        chart_indices: Sequence[int] | None = None,
        refine_activation: bool | int | None = None,
        ceiling: bool = False,
    ) -> list[float]:
        """One float per team, in order. `chart_indices` restricts THIS batch
        to those of the context's charts (see `evaluate`); `None` is all.
        `refine_activation` overrides the context's flag (a `refine_level`) for this batch.
        `ceiling`: each team's run score if every ACTIVE fires (on the plain develop, or the
        level `refine_activation` names), the value a ranking by Max searches on."""
        if ceiling:
            chosen = None if chart_indices is None else tuple(chart_indices)
            return self._run(
                [
                    (t.leader.id, tuple(m.id for m in t.members), chosen, refine_activation, True)
                    for t in teams
                ]
            )
        if refine_activation is not None:
            chosen = None if chart_indices is None else tuple(chart_indices)
            items: list[tuple] = [
                (t.leader.id, tuple(m.id for m in t.members), chosen, refine_activation) for t in teams
            ]
        elif chart_indices is None:
            items = [(t.leader.id, tuple(m.id for m in t.members)) for t in teams]
        else:
            chosen = tuple(chart_indices)
            items = [(t.leader.id, tuple(m.id for m in t.members), chosen) for t in teams]
        return self._run(items)

    def batch_score_and_ceiling(
        self, teams: Sequence[Team], chart_indices: Sequence[int], refine_activation: bool | int
    ) -> list[tuple[float, float]]:
        """`(expectation, ceiling)` per team, in order: `batch_score`'s float and
        `batch_ceiling`'s, from ONE develop of each team's board."""
        chosen = tuple(chart_indices)
        return [
            (float(e), float(c))
            for e, c in self._run(
                [
                    (t.leader.id, tuple(m.id for m in t.members), chosen, refine_activation, "both")
                    for t in teams
                ]
            )
        ]

    def batch_ceiling(
        self,
        teams: Sequence[Team],
        chart_indices: Sequence[int],
        refine: Sequence[bool | int],
        planned: Sequence[tuple | None],
    ) -> list[float]:
        """Each team's run score if every ACTIVE fires, at the slot order its expectation
        plays, on the board its expectation is served on: developed with the activation
        refinement at level `refine[k]` (`refine_level`), or `planned[k]` = `(team board state, bench)` for a
        team re-planned within the materials. One float per team, in order."""
        chosen = tuple(chart_indices)
        return self._run(
            [
                (t.leader.id, tuple(m.id for m in t.members), chosen, refine_level(r), True, p)
                for t, r, p in zip(teams, refine, planned)
            ]
        )

    def _warn(self, line: str) -> None:
        """Report a worker failure: kept for the search's answer (`take_warnings`), and to
        `on_fallback` or the console."""
        self.warnings.append(line)
        if self.on_fallback:
            self.on_fallback(line)
        else:
            print(f"  ! {line}", file=sys.stderr, flush=True)

    def take_warnings(self) -> list[str]:
        """The worker failures since the last call (each once)."""
        taken, self.warnings = self.warnings, []
        return taken

    def _gather_or_warn(self, task: Callable, tasks: list, out: list) -> None:
        """`_gather` into `out`. A worker that died or a task that raised leaves the items
        without an answer None, for the caller to score in this process, and is reported. The
        pool is kept for the next batch: it has replaced a dead worker already, and a task's
        own error is the task's (the in-process run raises it again)."""
        try:
            _gather(self._pool, task, tasks, _chunksize(len(tasks), self.workers), out)
        except Exception as exc:  # noqa: BLE001 — reported; the batch finishes in this process
            missing = sum(v is None for v in out)
            self._warn(
                f"the search's worker processes failed mid-batch ({type(exc).__name__}: {exc}); "
                f"{missing} of {len(out)} teams of this batch were scored in a single process"
            )

    def _run(self, items: list) -> list[Any]:
        """`evaluate` on each item (`_task`'s shape), in the workers; in order."""
        if not items:
            return []
        if self._pool is None:
            return self._serial(items)
        out: list[float | None] = [None] * len(items)
        task, tasks = self._items(items, _task, _ctx_task)
        self._gather_or_warn(task, tasks, out)
        if any(v is None for v in out):
            missing = [items[i] for i, v in enumerate(out) if v is None]
            fill = iter(self._serial(missing))
            out = [v if v is not None else next(fill) for v in out]
        # a "both" item answers `(expectation, ceiling)`
        return [v if isinstance(v, tuple) else float(v) for v in out]  # type: ignore[arg-type]

    def batch_score_per_chart(
        self,
        teams: Sequence[Team],
        chart_indices: Sequence[int] | None = None,
        *,
        ceiling: bool = False,
        planned: Sequence[tuple | None] | None = None,
        refine_activation: str | None = None,
    ) -> list[list[float]]:
        """Each team's score on EVERY chart of the context (or on `chart_indices`), in chart
        order (their mean is `batch_score(teams)`'s float). One list per team, in order.
        `ceiling`: each chart's all-fire score instead. `planned[k]`: the `(team board state,
        bench)` team k is scored on (a re-plan within the materials), None to develop it.
        `refine_activation`: `batch_score`'s, None or SEARCH."""
        if chart_indices is None and not ceiling and planned is None and refine_activation is None:
            items: list[tuple] = [(t.leader.id, tuple(m.id for m in t.members)) for t in teams]
        else:
            chosen = None if chart_indices is None else tuple(chart_indices)
            boards = list(planned) if planned is not None else [None] * len(teams)
            items = [
                (t.leader.id, tuple(m.id for m in t.members), chosen, refine_activation, ceiling, board)
                for t, board in zip(teams, boards)
            ]
        if not items:
            return []
        out: list = [None] * len(items)
        if self._pool is not None:
            task, tasks = self._items(items, _task_per_chart, _ctx_task_per_chart)
            self._gather_or_warn(task, tasks, out)
        missing = [k for k, v in enumerate(out) if v is None]
        if missing and self.ctx.use_plan_cache and self._plan_cache is None:
            self._plan_cache = new_plan_cache(self.ctx, self._by_id)
        for k in missing:
            out[k] = evaluate(
                self.ctx,
                self._by_id,
                self._tree_for,
                self._budget_for,
                items[k],
                plan_cache=self._plan_cache,
                per_chart=True,
            )
        return [[float(s) for s in v] for v in out]
