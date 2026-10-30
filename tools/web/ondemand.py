"""The optimiser ON DEMAND: one song's best teams searched when asked, no cache.

The optimiser answers per request, as fast and as true as it can, with no
hours-long precomputed cache that goes stale; a new card or song is data and
needs no rebuild. This module is that path:

* an `Engine` per (basis, difficulty) keeps a `RefinePool` over EVERY chart of
  that difficulty alive between requests (the pool start and the leaf inputs,
  ~2 s, are paid once; a refresh changes the inputs' key and the next request
  rebuilds it);
* a request runs `joint_search.search` — the leader chosen inside the search —
  on the song's own chart (`chart_indices=(song,)`, the board developed for
  that song), then re-scores the best teams with the activation refinement;
* the answer of the last request with the same song and filters is kept IN
  MEMORY as seeds for the next one (a hint, never the answer: every number
  served is computed in this request). Nothing here writes to disk.

`payload` has the shape of `two_stage_view.payload` (`scope`, `ranking`, `rows`,
...), so the page draws it with the same code; `state` is always "fresh".
"""
from __future__ import annotations

import dataclasses
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Mapping

_PROXY = Path(__file__).resolve().parents[1] / "recon" / "proxy"
if str(_PROXY) not in sys.path:
    sys.path.insert(0, str(_PROXY))

import joint_search  # noqa: E402
import two_stage  # noqa: E402
import two_stage_build as tsb  # noqa: E402

# the teams re-scored with the activation refinement (`two_stage_build.SETTINGS
# ["refine"]`: within 4 % of the best, at most this many; ~0.04 s a team)
REFINE_CAP = 12
REFINE_MARGIN = 0.04
# the served ranking with the activation refinement is the best REFINE_HEAD teams of the
# search, every one refined, in refined order: refining only the best REFINE_CAP within
# REFINE_MARGIN would serve the rest at plain values beside refined ones, so a true #3 could
# sit at #22 and the same team score differently with and without a leader filter
# (refinement gains 0-3.3 %, mean about 0.5 %; plain teams ranked in the 50s-70s can be
# refined top 10)
REFINE_HEAD = 100
RANKING = 50          # teams served, best first (a true top N)
ROWS = 8              # per-leader rows (each leader's best), the secondary view
HINTS = 20            # pairs kept per (song, filters) as the next request's seeds
# In general (no song): the search ranks by the mean over this many charts
# (`two_stage.chart_sample`), then the best GENERAL_RESCORE teams are scored on
# EVERY chart and only those full means are served
GENERAL_SAMPLE = 6
# ... except on a maxed account, where every holomem's singer nodes bought makes a
# team score ~4 % more on a song one of its holomems sings: 6 note-spread charts
# ranked the strong teams worse than chance there and the search never scored the
# true #1
GENERAL_SAMPLE_BY_BASIS = {"maxed": 16}
# The bases whose single-difficulty In general search uses a FITTED sample instead
# (`two_stage.fitted_chart_sample`): SAMPLE_TRAIN random legal teams scored on every
# chart once per engine, the FITTED_SAMPLE
# charts whose mean best tracks each team's full mean to search on, then a hill
# climb (`joint_search.climb`) on the first GENERAL_WIDE of the same selection from
# the search's best CLIMB_START teams -- two tiers, like the 2 / 6 pseudo-maps of the
# community tool holodori.best. On maxed EXPERT this reaches the best known team in
# 4 of 5 runs in about 24-39 s, against 60-87 s with 16 note-spread charts. NOT used
# with a level range (maxed 25-30: 0/5 runs; the note sample 4/5 in 12-20 s) nor on
# the account (same answer, 29 s instead of 12 s). 8 fitted charts put every run
# within 0.08 % of the best known team in 16-19 s; 6 charts leave some runs ~1 % low
GENERAL_FITTED = frozenset({"maxed"})
FITTED_SAMPLE = 8
GENERAL_WIDE = 24
CLIMB_START = 20
CLIMB_POLISH = 10
CLIMB_RESCORE = 100
# independent searches merged In general on the two-tier sample (maxed; each from its own
# seed; the song search and the account keep one). With the settle below, 2 starts reach the
# best known team in 5 of 6 seeds (40-58 s), 1 start in 3 of 6 (26-41 s)
GENERAL_STARTS = 2
# In general, after the full re-score: the climb again from its best SETTLE_FROM teams
# (on the wide sample), and its best SETTLE_RESCORE new teams re-scored on every chart
SETTLE_FROM = 3
SETTLE_RESCORE = 20
# ...and two-card moves: the best this many one-swaps of the climb's best team, paired
SETTLE_PAIR_MOVES = 20
SAMPLE_TRAIN = 300
SAMPLE_SEED = 20261022
GENERAL_RESCORE = 200
# boards planned within the account's own materials: the search's best this many
# teams are re-planned exactly within the wallet and re-scored (`materials_plan.py`)
# 24: the ranking served is the re-planned teams, so this bounds it -- 20 shown plus room
# for a team the wallet cannot plan
REPLAN_CANDIDATES = 24
# ... plus the best this many teams led by a card whose holomem SINGS the song, wherever the
# quick plan put them: the exact plan buys the singer's own song-score nodes, which the quick
# plan values badly (a singer-led team can sit at the leaf's #190 and re-plan +8 % to the
# served #4)
SINGER_CANDIDATES = 4
# ... chosen from this many of the search's best, re-ranked first with the
# activation refinement: the plain leaf orders them badly against the exact plan
# (Spearman about 0.37 on a song's best 96; 0.71 refined, 3 s), since the plan's cooldown
# staircase adds +2.1 % on average and up to +7.5 % depending on the team (the exact top
# 16 can come from as deep as the leaf's #87, the refined #39)
REPLAN_SCREEN = 96
MATERIALS_CANDIDATES = REPLAN_CANDIDATES  # alias kept for callers that use this name


def is_five_star(card_id: str) -> bool:
    """The id's 3rd segment is the rarity (`card-00001-5-uniq-...`), as server._is_five_star."""
    parts = str(card_id).split("-")
    return len(parts) > 2 and parts[2] == "5"


def replan_pick(ranked: list, sings, n: int = REPLAN_CANDIDATES,
                extra: int = SINGER_CANDIDATES) -> list[int]:
    """Positions in `ranked` (best first) that get the exact board plan: the first `n`, then
    the best `extra` more whose leader sings the song (`sings(item)`), in ranking order."""
    head = list(range(min(n, len(ranked))))
    more = [k for k in range(len(head), len(ranked)) if sings(ranked[k])][:extra]
    return head + more


def candidates(reps, filters: "Filters") -> tuple[list, list]:
    """(member pool, leader candidates) of a search: banned cards out of both; with the 5-star
    switch the MEMBERS are 5-star only while any rarity may lead (an outfit leader is not
    part of the card-pool filter)."""
    excluded = set(filters.exclude)
    usable = [c for c in reps if c.id not in excluded]
    pool = [c for c in usable if not filters.members_five_star or is_five_star(c.id)]
    return pool, usable


class Cancelled(Exception):
    pass


@dataclasses.dataclass
class Filters:
    """What the page lets the user pin. Card ids; empty = no constraint."""
    leader_member: str = ""          # the leader must be this holomem
    leader_card: str = ""            # ... or exactly this card
    no_leader_overlap: bool = False  # the leader's holomem not among the members
    require: tuple[str, ...] = ()    # these cards must be members
    exclude: tuple[str, ...] = ()    # these cards are not used at all
    # "5-star only" (the page's Card pool): the five MEMBERS are 5-star cards; the leader slot is
    # any rarity (an outfit).
    members_five_star: bool = False

    def key(self) -> tuple:
        return (self.leader_member, self.leader_card, self.no_leader_overlap,
                tuple(sorted(self.require)), tuple(sorted(self.exclude)), self.members_five_star)


@dataclasses.dataclass(frozen=True)
class Assumptions:
    """What the answer assumes about the USER'S ACCOUNT, one switch per lever --
    modelled on the community tool holodori.best's optimizer options: the account
    as it is, untouched, by default, every assumption an explicit switch.

    Cards: `level` "account" | an int | "max", `bloom` "account" | an int,
    `sp_training` "account" | 0..4 (holodori.best's "Assumed SP Training": the limit
    break, i.e. each card's level cap by rarity); `keep_higher` = holodori.best's "do
    not delevel / debloom" (a card above an assumption keeps its own).

    Boards: `boards` "as_is" (each character's nodes as captured, nothing
    planned) | "plan" (each team's boards planned from scratch: a full reset,
    points refunded). With "plan": `max_points` (every character at max level,
    361 points), `infinite` (cubes / hexas ignored; off-team idle points on
    green), `all_invited` (a character with no points plans at 361),
    `max_resets` (cap on owned nodes the plan may reset; None = no cap; each
    costs 1,000 Hologold), `no_refund` (a reset returns no materials), `branches`
    (only these of G/B/R/Y are bought or reset; the rest stay as owned) and
    `targets` (`(chr, node, "buy" | "keep")`: bought if the budget allows / kept
    only when owned), `reset_min_gain` (percent of the team's score a reset must
    earn to be made, 0 = resets are free; within the account's materials only). Every one of those but max_points / all_invited / infinite
    is a constraint the search's quick plan does not model: the best
    REPLAN_CANDIDATES teams are then re-planned exactly under them
    (`materials_plan.py`) and ranked on those boards."""
    level: object = "account"
    bloom: object = "account"
    sp_training: object = "account"
    keep_higher: bool = False
    boards: str = "as_is"
    max_points: bool = False
    infinite: bool = False
    all_invited: bool = False
    max_resets: object = None
    no_refund: bool = False
    branches: str = "GBRY"
    targets: tuple = ()
    reset_min_gain: float = 0.0

    @property
    def materials(self) -> bool:
        """Boards planned within the account's own materials."""
        return self.boards == "plan" and not self.infinite

    @property
    def replan(self) -> bool:
        """The best teams are re-planned exactly (a constraint the quick plan lacks)."""
        return self.boards == "plan" and (
            not self.infinite or self.max_resets is not None or self.branches != "GBRY"
            or bool(self.targets))

    def plan_opts(self) -> dict:
        """`board_plan_solver.solve_team_board` keyword options of this plan."""
        targets: dict = {}
        for chr_id, node, mode in self.targets:
            targets.setdefault(chr_id, {})[node] = mode
        return {"char_level": "max" if self.max_points else "current",
                "resources": "infinite" if self.infinite else "inventory",
                "max_resets": self.max_resets, "branches": self.branches,
                "targets": targets, "no_refund": self.no_refund and not self.infinite,
                "all_invited": self.all_invited,
                "reset_min_gain": 0.0 if self.infinite else self.reset_min_gain / 100.0}

    def key(self) -> tuple:
        return (str(self.level), str(self.bloom), str(self.sp_training), self.keep_higher,
                self.boards, self.max_points, self.infinite, self.all_invited,
                str(self.max_resets), self.no_refund, self.branches, self.targets,
                self.reset_min_gain)

    def notes(self) -> list[str]:
        out = []
        if self.replan:
            what = []
            if self.materials:
                what.append("within your cubes and hexas (a reset "
                            + ("refunds nothing" if self.no_refund else "refunds what it takes off")
                            + ")")
            if self.max_resets is not None:
                what.append(f"at most {self.max_resets} resets")
            if self.reset_min_gain and self.materials:
                what.append(f"a reset only where it earns {self.reset_min_gain:g} % of the score")
            if self.branches != "GBRY":
                what.append("only the " + "/".join(self.branches) + " branches changed")
            if self.targets:
                what.append(f"{len(self.targets)} secondary target(s)")
            out.append(f"The best {REPLAN_CANDIDATES} teams of the search (picked from its "
                       f"best {REPLAN_SCREEN} with their activation nodes chosen jointly) were re-planned "
                       + ", ".join(what) + " and are ranked on those boards; the search itself "
                       "did not see these constraints, so a team that only shines without "
                       "them may sit lower.")
        return out

    def describe(self) -> str:
        if self.level == "account" and self.bloom == "account" and self.sp_training == "account":
            cards = "your cards as they are"
        else:
            parts = ([f"level {self.level}"] if self.level != "account" else []) + \
                    ([f"bloom {self.bloom}"] if self.bloom != "account" else []) + \
                    ([f"SP training {self.sp_training}"] if self.sp_training != "account" else [])
            cards = "your cards at " + ", ".join(parts) + \
                    (" (higher ones kept)" if self.keep_higher else "")
        if self.boards == "as_is":
            boards = "your Holomem Boards exactly as they are"
        else:
            extra = []
            if not self.infinite:
                extra.append("within your cubes and hexas"
                             + (" (no refund)" if self.no_refund else ""))
            else:
                extra.append("unlimited cubes and hexas")
            if self.all_invited:
                extra.append("every holomem invited")
            if self.max_resets is not None:
                extra.append(f"at most {self.max_resets} resets")
            if self.reset_min_gain and not self.infinite:
                extra.append(f"each reset earning {self.reset_min_gain:g} %")
            if self.branches != "GBRY":
                extra.append("only " + "/".join(self.branches) + " changed")
            if self.targets:
                extra.append(f"{len(self.targets)} target node(s)")
            boards = ("each team's boards planned from scratch with "
                      + ("every character's max points (361)" if self.max_points
                         else "each character's own points")
                      + ", " + ", ".join(extra))
        return f"{cards}; {boards}"


def parse_assumptions(raw: Mapping | None) -> Assumptions:
    """`Assumptions` from a request body's `assumptions` object; ValueError on a bad lever."""
    raw = raw or {}

    def lever(v, lo, hi):
        if v in (None, "", "account"):
            return "account"
        if v == "max":
            return "max"
        n = int(v)
        if not lo <= n <= hi:
            raise ValueError(f"{n} is outside {lo}..{hi}")
        return n
    boards = str(raw.get("boards") or "as_is")
    if boards not in ("as_is", "plan"):
        raise ValueError("boards must be 'as_is' or 'plan'")
    bloom = lever(raw.get("bloom"), 0, 5)
    if bloom == "max":
        bloom = 5
    sp = lever(raw.get("sp_training"), 0, 4)
    if sp == "max":
        sp = 4
    resets = raw.get("max_resets")
    if resets in (None, "", -1, "-1"):
        resets = None
    else:
        resets = int(resets)
        if resets < 0:
            raise ValueError("max_resets must be >= 0 (or -1 / absent for no cap)")
    gain = raw.get("reset_min_gain")
    gain = 0.0 if gain in (None, "") else float(gain)
    if not 0.0 <= gain <= 5.0:
        raise ValueError("reset_min_gain must be a percent in 0..5")
    branches = "".join(b for b in "GBRY" if b in str(raw.get("branches") or "GBRY").upper())
    if not branches:
        raise ValueError("branches must name at least one of G, B, R, Y")
    targets = []
    for t in raw.get("targets") or ():
        chr_id, node, mode = str(t.get("chr") or ""), str(t.get("node") or ""), str(t.get("mode") or "buy")
        if not chr_id.startswith("chr-") or not node or mode not in ("buy", "keep"):
            raise ValueError(f"bad target {t!r}: need chr-..., node, mode buy|keep")
        targets.append((chr_id, node, mode))
    return Assumptions(level=lever(raw.get("level"), 1, 80), bloom=bloom, sp_training=sp,
                       keep_higher=bool(raw.get("keep_higher")), boards=boards,
                       max_points=bool(raw.get("max_points")), infinite=bool(raw.get("infinite")),
                       all_invited=bool(raw.get("all_invited")), max_resets=resets,
                       no_refund=bool(raw.get("no_refund")), branches=branches,
                       targets=tuple(sorted(set(targets))), reset_min_gain=gain)


def build_inputs(a: Assumptions, difficulty: str, song: str = "m0334", *,
                 split_key=None) -> dict:
    """The leaf inputs of the user's account under `a` (the shape of
    `two_stage_build.leaf_inputs`) -- shared by the engine, the event search and
    the row detail so a row is re-developed on the very leaf that ranked it.
    `split_key`: `two_stage_account.build`'s (the event search keeps cards with
    different event bonuses apart)."""
    import json as _json  # noqa: PLC0415

    acct = tsb._account()
    ns = tsb._namespace(song, difficulty)
    if a.level != "account":
        ns.assume_level = a.level
    if a.bloom != "account":
        ns.assume_bloom = a.bloom
    if a.sp_training != "account":
        ns.assume_limit_break = a.sp_training
    ns.keep_higher_level = ns.keep_higher_bloom = a.keep_higher
    if a.boards == "as_is":
        state = _json.loads((_PROXY / "dumps" / "board_state.json").read_text(encoding="utf-8"))
        built = acct.build(ns, bench="captured", board_state=state, split_key=split_key)
        # the account's boards: its socketed Connect cards as they are, not re-assigned
        return dict(built, ctx=dataclasses.replace(built["ctx"], board_state_slots=True))
    built = acct.build(ns, flat_budget=acct.MAX_LEVEL_POINTS if a.max_points else None,
                       bench="infinite" if a.infinite else "captured", split_key=split_key)
    if a.all_invited and not a.max_points:
        # a holomem with no board points (not invited) plans at the max-level grant
        ctx = built["ctx"]
        granted = {c: int(ctx.granted.get(c) or 0) or acct.MAX_LEVEL_POINTS
                   for c in ctx.name_by_chr}
        built = dict(built, ctx=dataclasses.replace(ctx, granted=granted))
    # the generation greens' cap in the per-character plans: each chain valued at the
    # room the OTHER boards (this leaf's bench) leave under 900 (`board_plan.grouping_room`)
    from hbuilder.domain import board_plan as _bp  # noqa: PLC0415
    ctx = built["ctx"]
    room = _bp.grouping_room(ctx.skilltree, ctx.bench_characters, ctx.name_by_chr)
    supply = _bp.grouping_supply(ctx.skilltree, ctx.bench_characters, ctx.name_by_chr)
    built = dict(built, ctx=dataclasses.replace(
        ctx, skilltree={**ctx.skilltree, _bp.GROUPING_ROOM_KEY: room,
                        _bp.GROUPING_SUPPLY_KEY: supply},
        # every board is planned, so a song's off-team singers' too (their yellows pay
        # on it; the materials re-plan plans them, `board_plan.singer_bench_characters`)
        plan_singer_bench=True,
        bench_sockets=bench_sockets_of(
            _json.loads((_PROXY / "dumps" / "board_state.json").read_text(encoding="utf-8")))))
    return built


def bench_sockets_of(state: Mapping) -> dict:
    """`board_plan.bench_sockets_from_state` (kept under this name for its callers)."""
    from hbuilder.domain.board_plan import bench_sockets_from_state  # noqa: PLC0415
    return bench_sockets_from_state(state)


class Engine:
    """The leaf of one (basis, difficulty[, assumptions]), alive between requests.
    With `assumptions` the basis is the user's account under them (`build_inputs`)."""

    play: tuple[str, bool] = ("mobile", False)     # (platform, auto): how the run is played

    def __init__(self, basis: str, difficulty: str, workers: int | None = None,
                 assumptions: Assumptions | None = None,
                 levels: tuple[int, int] | None = None,
                 play: tuple[str, bool] = ("mobile", False)) -> None:
        self.basis = basis
        self.play = tuple(play)
        self.assumptions = assumptions
        self.difficulty = tsb._difficulty(difficulty)
        # holodori.best's "any song" level range: every chart of ANY difficulty whose level
        # is in it (In general only; `difficulty` then only names the leaf's nominal chart)
        self.levels = tuple(levels) if levels else None
        t0 = time.perf_counter()
        built = (build_inputs(assumptions, self.difficulty) if assumptions is not None
                 else tsb.leaf_inputs(basis, self.difficulty))
        ex, mfp = built["ex"], built.get("mfp")
        if mfp is None:
            mfp = tsb._load("_mfp_ondemand", "tools/recon/probes/members_first_2026_08_19.py")
        self.mf = mfp.mf
        path = tsb.charts_path(self.difficulty)
        if self.levels:
            lo, hi = self.levels
            library = ex.load_charts(str(tsb.charts_path("HARD")))      # charts_all.json
            self.charts = tuple(c for _k, c in sorted(library.charts.items())
                                if lo <= c.difficulty_level <= hi)
            if not self.charts:
                raise KeyError(f"no chart has a level in {lo}..{hi}")
            self.index = {}
        else:
            self.charts = ex.load_charts(str(path)).at(self.difficulty)
            self.index = {chart.music_id: k for k, chart in enumerate(self.charts)}
        digests = tsb.chart_digests(path, self.difficulty)
        if assumptions is not None:
            # any input file moving (a refresh) changes the account basis' key
            self.key = account_key(self.difficulty)
        else:
            _songs, self.key = tsb.keys_for(built["reference_key"], digests, basis, self.difficulty)
        self.built = built
        self.reps = list(built["reps"])
        self.by_id = built["by_id"]
        ctx = dataclasses.replace(with_play(built["ctx"], self.play), charts=tuple(self.charts),
                                  board_per_chart=True)
        self.workers = workers or mfp.default_workers()
        self.pool = mfp.pr.RefinePool(ctx, workers=self.workers)
        self.pool.__enter__()
        self.lock = threading.Lock()
        self.hints: dict[tuple, list[tuple[str, tuple[str, ...]]]] = {}
        self.started_seconds = time.perf_counter() - t0
        self.closed = False
        self._sample: tuple[int, ...] | None = None
        self._wide: tuple[int, ...] = ()

    def _within_materials(self, ranking, leaders, pool, song, chart_indices, cancel, progress=None):
        """The best MATERIALS_CANDIDATES distinct teams re-planned within the
        wallet and re-scored on those boards (`materials_plan.plan_and_score`, in
        the engine's workers). Returns `(ranking of the re-planned teams, best
        first, [why a team could not be planned])`."""
        import materials_plan  # noqa: PLC0415

        a = self.assumptions
        singers = set(self.charts[self.index[song]].singers or ()) if song in self.index else set()
        cands = [ranking[k] for k in replan_pick(
            ranking, lambda r: "chr-" + leaders[r[1]].id.split("-")[1] in singers)]
        # each with the plan this process already holds for it (`carried_plan`): a filter
        # or option toggle re-plans mostly the teams the previous search planned
        items = [(k, leaders[li].id, tuple(pool[i].id for i in s), song, chart_indices,
                  a.plan_opts(), materials_plan.carried_plan(
                      leaders[li].id, tuple(pool[i].id for i in s), song, a.plan_opts()))
                 for k, (_v, li, s) in enumerate(cands)]
        # the longest plans first, so they do not start in the last wave: a singing
        # leader's plan runs ~2x the solves (and about twice the time); the order changes
        # nothing else (by index)
        items.sort(key=lambda it: it[1].split("-")[1] not in {c.split("-")[1] for c in singers})
        # by its own index, never by position: the list is not in index order
        item_of = {it[0]: it for it in items}
        workers = getattr(self.pool, "_pool", None)
        if workers is None:
            raise RuntimeError("planning within materials needs the worker pool")
        got: dict[int, float] = {}
        failed: list[str] = []
        for done, (index, value, _state, reason, held) in enumerate(
                materials_plan.dispatch(self.pool, items, cancel), 1):
            if progress:
                progress(done, len(items))          # a real count: the page's ETA is a rate
            if value is None:
                failed.append(f"{leaders[cands[index][1]].member}: {reason}")
            else:
                got[index] = value
                materials_plan.remember(*item_of[index][1:4], item_of[index][5], held)
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        out = sorted(((got[k], li, s) for k, (_v, li, s) in enumerate(cands) if k in got),
                     key=lambda r: (-r[0], r[1], r[2]))
        if not out:
            raise KeyError("no team could be planned within your materials: "
                           + "; ".join(failed[:3]))
        return out, failed

    def _screen_for_replan(self, ranking, leaders, pool, chart_indices):
        """The search's best REPLAN_SCREEN teams re-ranked by their score with the
        activation nodes chosen jointly (each keeps the better of the two), so the
        REPLAN_CANDIDATES the exact plan gets are the ones its staircase favours.
        Only the order changes: the served numbers are the re-planned ones."""
        from hbuilder.domain.team import Team  # noqa: PLC0415

        head = ranking[:REPLAN_SCREEN]
        teams = [Team(leader=leaders[li], members=tuple(pool[i] for i in s)) for _v, li, s in head]
        refined = self.pool.batch_score(teams, chart_indices=chart_indices, refine_activation=True)
        head = sorted(((max(float(v), float(r)), li, s) for (v, li, s), r in zip(head, refined)),
                      key=lambda r: (-r[0], r[1], r[2]))
        return head + list(ranking[REPLAN_SCREEN:])

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.pool.__exit__(None, None, None)

    # ------------------------------------------------------------------ sample
    def general_sample(self, *, emit: Callable[[dict], None] | None = None,
                       cancel: threading.Event | None = None) -> tuple[int, ...]:
        """The In general search's chart sample (indices into `self.charts`).
        A GENERAL_FITTED basis with one difficulty: the first FITTED_SAMPLE of the
        GENERAL_WIDE fitted charts (all kept in `self._wide`, for the climb), fitted
        once per engine (`two_stage.fitted_chart_sample` on SAMPLE_TRAIN random teams
        scored on every chart, ~8 s on 16 workers; kept for the engine's life, which
        a refresh ends). Otherwise the note-spread `two_stage.chart_sample` and no
        climb."""
        if self._sample is not None:
            return self._sample
        if self.basis not in GENERAL_FITTED or self.levels:
            by_obj = {id(c): k for k, c in enumerate(self.charts)}
            count = GENERAL_SAMPLE_BY_BASIS.get(self.basis, GENERAL_SAMPLE)
            self._sample = tuple(by_obj[id(c)] for c in two_stage.chart_sample(self.charts, count))
            self._wide = ()
            return self._sample
        import random  # noqa: PLC0415

        from hbuilder.domain.team import Team  # noqa: PLC0415

        rng = random.Random(SAMPLE_SEED)
        holomems = [c.member for c in self.reps]
        teams = sorted((Team(leader=rng.choice(self.reps), members=tuple(self.reps[i] for i in s))
                        for s in two_stage.random_legal_sets(holomems, SAMPLE_TRAIN, rng)),
                       key=lambda t: t.leader.id)
        scores: list[list[float]] = []
        step = max(1, len(teams) // 6)
        for at in range(0, len(teams), step):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            with self.lock:
                scores += self.pool.batch_score_per_chart(teams[at:at + step])
            if emit:
                emit({"type": "progress", "stage": "search", "sub": "sample",
                      "done": len(scores), "total": len(teams)})
        wide = tuple(two_stage.fitted_chart_sample(scores, max(GENERAL_WIDE, FITTED_SAMPLE)))
        self._wide, self._sample = wide, wide[:FITTED_SAMPLE]
        return self._sample

    # ------------------------------------------------------------------ search
    def search(self, song: str | None, filters: Filters | None = None, *,
               emit: Callable[[dict], None] | None = None,
               cancel: threading.Event | None = None,
               top: int = RANKING, seed: int = 20261016) -> dict:
        filters = filters or Filters()
        general = not song
        if general:
            chart = None
            chart_indices = self.general_sample(emit=emit, cancel=cancel)
            sample = [self.charts[k] for k in chart_indices]
        else:
            if song not in self.index:
                raise KeyError(f"no {self.difficulty} chart for {song!r}")
            chart = self.charts[self.index[song]]
            chart_indices = (self.index[song],)
        pool, usable = candidates(self.reps, filters)
        position = {c.id: k for k, c in enumerate(pool)}
        missing = [c for c in filters.require if c not in position]
        if missing:
            raise KeyError(f"required cards not in this pool: {missing}"
                           + (" (Card pool is 5-star only)" if filters.members_five_star else ""))
        require = [position[c] for c in filters.require]
        leaders = [c for c in usable
                   if (not filters.leader_card or c.id == filters.leader_card)
                   and (not filters.leader_member or c.member == filters.leader_member)]
        if not leaders:
            raise KeyError("no card in the pool matches the leader filter")
        allowed = None
        if filters.no_leader_overlap:
            def allowed(li, s, leaders=leaders, pool=pool):
                return all(pool[i].member != leaders[li].member for i in s)
        lpos = {c.id: k for k, c in enumerate(leaders)}
        hint_key = (song, filters.key())
        # the song's singers lead with every proposed set (`joint_search`'s `always`): the
        # screen learns each set under its best leader, so a singer whose teams rank a few
        # % lower would be scored on a handful of sets and her own best sets never proposed
        # under her (a singer-filtered search can find a team above the unfiltered #1)
        sing = set((chart.singers or ()) if chart is not None else ())
        always = [k for k, c in enumerate(leaders) if "chr-" + c.id.split("-")[1] in sing]
        seeds = [(lpos[l], tuple(sorted(position[m] for m in ms)))
                 for l, ms in self.hints.get(hint_key, ())
                 if l in lpos and all(m in position for m in ms)]

        from hbuilder.domain.team import Team  # noqa: PLC0415

        def score_pairs(pairs):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            teams = [Team(leader=leaders[li], members=tuple(pool[i] for i in s)) for li, s in pairs]
            return self.pool.batch_score(teams, chart_indices=chart_indices)

        def on_round(record):
            if emit:
                emit({"type": "progress", "stage": "search", "round": record["round"],
                      "evaluated": record["pairs"], "best_so_far": None if record["best"] is None
                      else {"value": record["best"], "metric": "song_est"}})

        t0 = time.perf_counter()
        with self.lock:
            found = joint_search.search(pool, leaders, score_pairs, gate_of=self.mf.gate_of,
                                        require=require, allowed=allowed, seeds=seeds,
                                        always=always, seed=seed, on_round=on_round)
            ranking = found["ranking"]
            if general and len(self._wide) > len(chart_indices):
                # In general on the two-tier (fitted) sample: further independent starts, merged. One search settles in
                # one region of teams; the best can sit four members away and never be
                # proposed: separate seeds can settle on different sets even where the
                # sample ranks them the same way as every chart does, so it is the reach
                merged = {(li, s): v for v, li, s in ranking}
                for extra in range(1, GENERAL_STARTS):
                    more = joint_search.search(pool, leaders, score_pairs, gate_of=self.mf.gate_of,
                                               require=require, allowed=allowed,
                                               seed=seed + 104729 * extra, on_round=on_round)
                    merged.update({(li, s): v for v, li, s in more["ranking"]})
                ranking = sorted(((v, li, s) for (li, s), v in merged.items()),
                                 key=lambda r: (-r[0], r[1], r[2]))
            search_seconds = time.perf_counter() - t0
            if not ranking:
                raise KeyError("no legal team satisfies the filters")
            if emit:
                emit({"type": "progress", "stage": "refine", "evaluated": 0})
            if general:
                # the climb on the wide sample, from the search's best teams; its
                # best go to the full re-score first, then the search's own order
                wide = tuple(self._wide)
                climbed = {"ranking": [], "stats": {"evals": 0}}
                if len(wide) > len(chart_indices):
                    def score_wide(pairs):
                        if cancel is not None and cancel.is_set():
                            raise Cancelled()
                        teams = [Team(leader=leaders[li], members=tuple(pool[i] for i in s))
                                 for li, s in pairs]
                        return self.pool.batch_score(teams, chart_indices=wide)
                    climbed = joint_search.climb(
                        pool, leaders, score_wide, [(li, s) for _v, li, s in ranking[:CLIMB_START]],
                        polish=CLIMB_POLISH, require=require, allowed=allowed)
                # the climb's best CLIMB_RESCORE AND the search's own best GENERAL_RESCORE:
                # a climb scores hundreds of swaps, and letting them fill the re-score
                # would push the search's #1 out (a repeat seeded with the first answer
                # could serve a lower team)
                order = list(dict.fromkeys([(li, s) for _v, li, s in climbed["ranking"][:CLIMB_RESCORE]]
                                           + [(li, s) for _v, li, s in ranking[:GENERAL_RESCORE]]))
                # every chart: the samples only chose the candidates; the served
                # numbers are the full means (plain leaf, board rebuilt per chart)
                cands = [(None, li, s) for li, s in order]
                teams = [Team(leader=leaders[li], members=tuple(pool[i] for i in s))
                         for _v, li, s in cands]
                full = self.pool.batch_score(teams, chart_indices=None)
                sample_rank = {(li, s): k for k, (_v, li, s) in enumerate(cands, 1)}
                ranking = sorted(((float(v), li, s) for (_p, li, s), v in zip(cands, full)),
                                 key=lambda r: (-r[0], r[1], r[2]))
                if len(wide) > len(chart_indices) and SETTLE_FROM:
                    # SETTLE: the climb swapped the best teams BY THE WIDE SAMPLE, which
                    # does not order them as every chart does -- the full re-score's #1
                    # may never have had its swaps tried (a team ONE swap away can score
                    # higher on every chart and on both samples). So the
                    # climb again from the full re-score's best, and its best on every chart
                    settled = joint_search.climb(
                        pool, leaders, score_wide, [(li, s) for _v, li, s in ranking[:SETTLE_FROM]],
                        neighbours=SETTLE_FROM, polish=SETTLE_FROM, require=require, allowed=allowed,
                        pair_moves=SETTLE_PAIR_MOVES)
                    have = {(li, s) for _v, li, s in ranking}
                    extra = [(li, s) for _v, li, s in settled["ranking"]
                             if (li, s) not in have][:SETTLE_RESCORE]
                    if extra:
                        values = self.pool.batch_score(
                            [Team(leader=leaders[li], members=tuple(pool[i] for i in s))
                             for li, s in extra], chart_indices=None)
                        for k, ((li, s), v) in enumerate(zip(extra, values), len(cands) + 1):
                            sample_rank[(li, s)] = k
                            ranking.append((float(v), li, s))
                        ranking.sort(key=lambda r: (-r[0], r[1], r[2]))
                chosen, refined = [], []
            else:
                # the refinement: the best teams with their activation nodes chosen jointly
                as_is = self.assumptions is not None and (self.assumptions.boards == "as_is"
                                                          or self.assumptions.replan)
                # boards as they are (nothing to plan) or re-planned within the materials
                # below: no activation refinement on a board that is not the one served
                chosen = [] if as_is else list(ranking[:REFINE_HEAD])
                teams = [Team(leader=leaders[li], members=tuple(pool[i] for i in s))
                         for _v, li, s in chosen]
                refined = self.pool.batch_score(teams, chart_indices=chart_indices,
                                                refine_activation=True) if teams else []
                if self.assumptions is not None and self.assumptions.replan:
                    ranking = self._screen_for_replan(ranking, leaders, pool, chart_indices)
        t_refine = time.perf_counter() - t0 - search_seconds
        better = {(li, s): max(float(v), plain) for (plain, li, s), v in zip(chosen, refined)}
        materials_failed: list[str] = []
        t_replan = time.perf_counter()
        if self.assumptions is not None and self.assumptions.replan:
            if emit:
                emit({"type": "progress", "stage": "refine", "evaluated": 0, "sub": "materials"})
            ranking, materials_failed = self._within_materials(
                ranking, leaders, pool, None if general else song,
                None if general else chart_indices, cancel,
                progress=(lambda k, n: emit({"type": "progress", "stage": "refine", "sub": "materials",
                                             "done": k, "total": n})) if emit else None)
            better = {}
        t_replan = time.perf_counter() - t_replan
        singers = set((chart.singers or ()) if chart is not None else ())

        def team(value, li, s):
            leader = leaders[li]
            members = [pool[i] for i in s]
            ref = better.get((li, s))
            return {"leader": leader.id, "leader_name": f"{leader.member} / {leader.costume_name}",
                    "sings": "chr-" + leader.id.split("-")[1] in singers, "status": "searched",
                    "best": ref if ref is not None else value,
                    "best_plain": value if ref is not None else None,
                    "members": [c.id for c in members], "member_names": [c.member for c in members],
                    "member_order": None, "refined": ref is not None,
                    "p10": None, "p50": None, "p90": None, "song_max": None,
                    "dist_mean": None, "dist_verified": None}

        # refined: only the refined head, so every served value is on the same basis
        served = [team(v, li, s) for v, li, s in (chosen if better else ranking)]
        served.sort(key=lambda t: -t["best"])
        served = served[:max(top, RANKING)]
        # each leader's best team, with its next sets as alternatives
        rows, by_leader = [], {}
        for t in served:
            by_leader.setdefault(t["leader"], []).append(t)
        for lid, ts in by_leader.items():
            head = dict(ts[0])
            head["alternatives"] = [{"members": t["members"], "member_names": t["member_names"],
                                     "best": t["best_plain"] or t["best"], "refined": False}
                                    for t in ts[1:5]]
            rows.append(head)
        rows.sort(key=lambda t: -t["best"])
        self.hints[hint_key] = [(leaders[li].id, tuple(pool[i].id for i in s))
                                for _v, li, s in ranking[:HINTS]]
        stats = found["stats"]
        extra = {}
        if general:
            extra = {"charts": len(self.charts), "levels": list(self.levels) if self.levels else None,
                     "sample": [f"{c.music_id}/{c.difficulty}" if self.levels else c.music_id
                                for c in sample],
                     "rescored": len(ranking),
                     # where the served #1 stood in the sample's order (1 = the sample agreed)
                     "best_sample_rank": sample_rank.get((ranking[0][1], ranking[0][2]))}
        return {
            **extra,
            "scope": "general" if general else "song", "song": song or None,
            "certified": False, "state": "fresh",
            "basis_id": self.basis,
            "basis": (self.assumptions.describe() if self.assumptions is not None
                      else tsb.BASIS_TEXT[self.basis]),
            "assumptions": dataclasses.asdict(self.assumptions) if self.assumptions else None,
            "notes": self.assumptions.notes() if self.assumptions else [],
            "difficulty": self.difficulty, "written": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "objective": ((f"mean score over every chart of level {self.levels[0]}-{self.levels[1]} "
                           f"({len(self.charts)} charts, any difficulty), the board rebuilt per song")
                          if general and self.levels else
                          f"mean score over every {self.difficulty} chart, the board rebuilt per song"
                          if general else f"expected score on this song's {self.difficulty} chart"),
            "method": ("joint leader + members search on a chart sample, run for this request "
                       "(no cache); the best teams re-scored on every chart" if general else
                       "joint leader + members search, run for this request "
                       "(no cache); the best teams re-scored with the activation refinement"),
            "on_demand": True,
            "play": {"platform": self.play[0], "auto": self.play[1]},
            "singers": sorted(singers), "singer_type": getattr(chart, "singer_type", None),
            "leaders": len(leaders), "searched_leaders": len(leaders),
            "legal_sets": stats.get("legal_sets"), "exact_evals": stats.get("exact_evals"),
            "seconds": round(time.perf_counter() - t0, 2),
            # `replan`: the exact plans within the materials, most of a search that has
            # them (about two thirds of the time); `refine` stops before them
            "timings": {"search": round(search_seconds, 2), "refine": round(t_refine, 2),
                        "replan": round(t_replan, 2),
                        "engine_start": round(self.started_seconds, 2)},
            "seeded": len(seeds), "filters": dataclasses.asdict(filters),
            "materials_failed": materials_failed,
            "build": {"running": False},
            "rows": rows[:ROWS],
            "ranking": served[:top], "ranking_size": len(served),
        }


_ENGINES: dict[tuple, Engine] = {}
_ENGINES_LOCK = threading.Lock()
# live engines kept at once (each holds a worker per core); the least recently
# used one is closed when another is needed
MAX_ENGINES = 2


# account files a refresh rewrites that the scoring key (`tsb.today_keys`) does not hash:
# the boards as they stand -- socketed cards and nodes (`board_state.json`, read for the
# bench sockets and "boards as they are") -- and the materials (`inventory.json`, the
# wallet the re-plan spends). Without them a refresh that only moved a socket or the
# wallet would leave the engine on the old account.
ACCOUNT_STATE_FILES = ("board_state.json", "inventory.json")


def account_key(difficulty: str) -> str | None:
    """Today's key of the account's inputs (None when unreadable)."""
    try:
        base = tsb.today_keys("account", difficulty)[1]
    except Exception:  # noqa: BLE001 - "cannot vouch" keeps the engine in hand
        return None
    import hashlib  # noqa: PLC0415

    h = hashlib.sha256(base.encode("utf-8"))
    for name in ACCOUNT_STATE_FILES:
        try:
            h.update(name.encode() + b"=" + hashlib.sha256((_PROXY / "dumps" / name).read_bytes()).digest())
        except OSError:
            h.update(name.encode() + b"=missing")
    return h.hexdigest()


def engine(basis: str, difficulty: str, *, workers: int | None = None,
           current_key: Callable[[], str | None] | None = None,
           assumptions: Assumptions | None = None,
           levels: tuple[int, int] | None = None,
           play: tuple[str, bool] = ("mobile", False)) -> Engine:
    """The live engine of (basis, difficulty[, assumptions]), rebuilt when the
    inputs' key moved (`current_key()` -> today's key, or None when it cannot
    be read: then the engine in hand is kept)."""
    difficulty = tsb._difficulty(difficulty)
    slot = (basis, difficulty, assumptions.key() if assumptions is not None else None,
            tuple(levels) if levels else None, tuple(play))
    with _ENGINES_LOCK:
        held = _ENGINES.pop(slot, None)
        if held is not None and current_key is not None:
            today = current_key()
            if today is not None and today != held.key:
                held.close()
                held = None
        if held is None or held.closed:
            while len(_ENGINES) >= MAX_ENGINES:
                oldest = next(iter(_ENGINES))
                _ENGINES.pop(oldest).close()
            held = Engine(basis, difficulty, workers=workers, assumptions=assumptions,
                          levels=levels, play=play)
        _ENGINES[slot] = held          # re-inserted last: most recently used
        return held


def shutdown() -> None:
    with _ENGINES_LOCK:
        for e in _ENGINES.values():
            e.close()
        _ENGINES.clear()


PLATFORMS = ("mobile", "pc")


def parse_play(body: Mapping) -> tuple[str, bool]:
    """`(platform, auto)` of a request: how the run is played. `platform` picks the
    LiveNote PERFECT column; `auto` an AUTO run."""
    platform = str(body.get("platform") or "").lower()
    return (platform if platform in PLATFORMS else "mobile", body.get("auto") is True)


def with_play(ctx, play: tuple[str, bool]):
    """`ctx` (a `RefineContext`) scored for `play`. AUTO sets BOTH halves, as
    `server._auto_premise` does for the older routes: the AUTO judgement column (a
    note scores like a GREAT, ~80 % of manual) AND `run_state.combo = 0`,
    since on AUTO the combo never accumulates and a `combo >= N` override never fires.
    The on-demand and event searches must honour the page's Auto toggle, or every AUTO
    request is ranked as a manual run."""
    platform, auto = play
    cfg = dataclasses.replace(ctx.cfg, platform=platform)
    if auto:
        cfg = dataclasses.replace(cfg, song_auto=True,
                                  run_state=dataclasses.replace(cfg.run_state, combo=0))
    return dataclasses.replace(ctx, cfg=cfg)


def parse_filters(body: Mapping) -> Filters:
    def ids(v) -> tuple[str, ...]:
        if isinstance(v, str):
            v = [x for x in v.split(",") if x]
        return tuple(str(x) for x in (v or []) if x)
    return Filters(leader_member=str(body.get("leader_member") or ""),
                   leader_card=str(body.get("leader_card") or ""),
                   no_leader_overlap=bool(body.get("no_leader_overlap")),
                   require=ids(body.get("require")), exclude=ids(body.get("exclude")),
                   members_five_star=str(body.get("pool") or "").lower() == "5star")
