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

`payload` carries `scope`, `ranking`, `rows`, ...; `state` is always "fresh".
"""

from __future__ import annotations

import dataclasses
import sys
import threading
import time
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from hbuilder import paths

if str(paths.REFRESH_DIR) not in sys.path:
    sys.path.insert(0, str(paths.REFRESH_DIR))
DUMPS = paths.DUMPS

import joint_search  # noqa: E402
import leaf_inputs  # noqa: E402
import two_stage  # noqa: E402

from hbuilder.domain.song import load_charts  # noqa: E402
from hbuilder.solver import members_first  # noqa: E402
from hbuilder.solver.parallel import default_workers  # noqa: E402
from hbuilder.solver.parallel_refine import COUNTS, COUNTS_BOTH, RefinePool, SharedPool  # noqa: E402

if TYPE_CHECKING:
    from hbuilder.domain.card import Card
    from hbuilder.domain.collection import AccountBonuses
    from hbuilder.solver.parallel_refine import RefineContext

# the served ranking with the activation refinement is the best REFINE_HEAD teams of the
# search, every one refined, in refined order (~0.04 s a team): refining only a few teams
# near the best would serve the rest at plain values beside refined ones, so a true #3 could
# sit at #22 and the same team score differently with and without a leader filter
# (refinement gains 0-3.3 %, mean about 0.5 %; plain teams ranked in the 50s-70s can be
# refined top 10)
# ... chosen from the search's best COUNTS_FUNNEL (30 a worker) by a cheap estimate of
# their frequency node counts chosen, not by the plain value (`_count_funnel`)
COUNTS_FUNNEL = 480
REFINE_HEAD = 100
# ... and the refined teams within COUNTS_MARGIN of the best refined one (at most
# COUNTS_HEAD, two per worker) are refined once more trying every combination of the
# members' activation-frequency node counts (refine level 2,
# `board_plan.refine_cooldown_counts`, ~0.5 s a team): a pair of members each dropping a
# frequency node can gain while either drop alone loses, which the move-by-move
# refinement never reaches. The gain measured on 16 top teams of four songs was
# 0-1.26 %, so a team further than the margin below the best cannot take #1 by it.
COUNTS_HEAD = 32
COUNTS_MARGIN = 0.015
# On one song with planned boards the search itself ranks every team by the LENS
# (`parallel_refine.lens_value`): the plain develop and its best single-member frequency count
# drop. The plain value buys every frequency node and ranks some teams up to ~5 % low, so the
# set screen and the leader model never propose them: holodori.best's #1 on m0001 sits 1,754th
# by the plain value and is never scored; ranked by the lens the same search finds it on 3 of
# 3 seeds (0 of 3 plain), and the best known team on m0342, m0552 and m0155 as before. The lens
# values only order: each served row is the refinement's, or its exact plain value
LENS_SEARCH = True
RANKING = 50  # teams served, best first (a true top N)
ROWS = 8  # per-leader rows (each leader's best), the secondary view
HINTS = 20  # pairs kept per (song, filters) as the next request's seeds
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
# In general with boards the leaf plans: the best this many re-scored teams are scored once
# more on every chart with the members' activation-frequency node counts chosen FOR EACH
# CHART (`parallel_refine.ChartCounts`), and they are the ranking served, on that one basis.
# The plain develop buys every frequency node on every song; choosing them per song adds
# 0.1-1.9 % to a top team's mean (median 0.6 %) and reorders the head: the plain #4 becomes
# #1 over every EXPERT chart, the plain #7 over the charts of levels 25-30. As many as a
# search serves (`RANKING`): "One per leader" picks from every row served
GENERAL_COUNTS_HEAD = 50
# ... picked from the best this many by the plain mean, by what the counts add on a
# note-spread sample of GENERAL_COUNTS_SAMPLE charts (`Engine._counts_funnel`): of the 20
# best teams with their counts chosen, some sit as deep as #34 (EXPERT) and #96 (levels
# 25-30) by the plain mean, and all within the funnel's best 26
GENERAL_COUNTS_FUNNEL = 200
GENERAL_COUNTS_SAMPLE = 24
# In general the joint search seeds every leader region (`joint_search.search`'s `tilt_of`):
# an outfit that raises one stat leans its leader's best sets towards the members strong in
# that stat. A card's tilt is its share of the stat around the even third, at the log gain
# of a +100 % boost on a fifth of the set (0.75 / 5), times 3 so the seed leans into the stat;
# the exact leaf judges every seed
OUTFIT_TILT = 3 * 0.75 / 5
OUTFIT_STATS = ("performance", "technique", "sense")
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
# the re-planned teams are planned with the board planner's first plan; the best this many
# are re-planned again with its other candidates (`materials_plan.refine_rows`): the rows the
# Optimizer page lists (at least 20; Versus takes the best 20). A row below them keeps its
# first plan, so it could have risen into them by what a candidate adds (on the golden: up to
# 0.18 % on a song, 1.9 % off a song). Off a song every re-planned team gets them: there that
# lift is the largest (a row planned 23rd on its first plan is served 20th), each candidate a
# task of its own behind the first plans, as on a song -- the same plans as one task per team
# solving them all, and the workers stay busy to the end (the last of 24 long tasks ran on a
# half-idle pool)
SHOWN_ROWS = 20


def is_five_star(card_id: str) -> bool:
    """The id's 3rd segment is the rarity (`card-00001-5-uniq-...`)."""
    parts = str(card_id).split("-")
    return len(parts) > 2 and parts[2] == "5"


def replan_pick(
    ranked: list, sings: Callable[[Any], bool], n: int = REPLAN_CANDIDATES, extra: int = SINGER_CANDIDATES
) -> list[int]:
    """Positions in `ranked` (best first) that get the exact board plan: the first `n`, then
    the best `extra` more whose leader sings the song (`sings(item)`), in ranking order."""
    head = list(range(min(n, len(ranked))))
    more = [k for k in range(len(head), len(ranked)) if sings(ranked[k])][:extra]
    return head + more


def outfit_tilt(pool: Sequence[Card]) -> Callable[[object], tuple[float, ...] | None]:
    """`tilt_of(leader)` for `joint_search.search` over `pool`: an outfit that raises one stat
    (`stat_up` of performance, technique or sense) adds OUTFIT_TILT x (the card's share of
    that stat - 1/3) to each card; any other outfit, None. One tuple per stat, shared by every
    leader whose outfit raises it."""
    by_stat: dict[str, tuple[float, ...]] = {}

    def tilt_of(leader):
        skill = getattr(leader, "costume_skill", None)
        stat = getattr(skill, "effect_type", None)
        if skill is None or getattr(skill, "family", None) != "stat_up" or stat not in OUTFIT_STATS:
            return None
        if stat not in by_stat:
            by_stat[stat] = tuple(
                OUTFIT_TILT * (getattr(c.stats, stat) / c.stats.total() - 1.0 / 3) for c in pool
            )
        return by_stat[stat]

    return tilt_of


def candidates(reps: Iterable[Card], filters: Filters) -> tuple[list, list]:
    """(member pool, leader candidates) of a search: banned cards out of both; with the 5-star
    switch the MEMBERS are 5-star only while any rarity may lead (an outfit leader is not
    part of the card-pool filter)."""
    excluded = set(filters.exclude)
    usable = [c for c in reps if c.id not in excluded]
    pool = [c for c in usable if not filters.members_five_star or is_five_star(c.id)]
    return pool, usable


class Cancelled(Exception):
    pass


class SearchRounds:
    """`joint_search.search`'s `on_round` for an answer made of `starts` searches run one after
    another (`GENERAL_STARTS`): each round becomes one `progress` event. With more than one
    start the event says which search it belongs to (`start` of `starts`) and its `evaluated`
    counts the teams of the searches before it too: each search numbers its rounds and its
    teams from zero, which on the page read as the same search running again."""

    def __init__(self, emit: Callable[[dict], None] | None, starts: int = 1) -> None:
        self.emit = emit
        self.starts = max(1, int(starts))
        self.start = 1
        self._before = 0  # teams scored by the searches already finished
        self._pairs = 0  # teams scored so far by the one running

    def next_start(self) -> None:
        """The search running has finished and another one begins."""
        self.start += 1
        self._before += self._pairs
        self._pairs = 0

    def __call__(self, record: Mapping) -> None:
        self._pairs = int(record["pairs"])
        if not self.emit:
            return
        event = {
            "type": "progress",
            "stage": "search",
            "round": record["round"],
            "evaluated": self._before + self._pairs,
            "best_so_far": None
            if record["best"] is None
            else {"value": record["best"], "metric": "song_est"},
        }
        if self.starts > 1:
            event["start"], event["starts"] = self.start, self.starts
        self.emit(event)


@dataclasses.dataclass
class Filters:
    """What the page lets the user pin. Card ids; empty = no constraint."""

    leader_member: str = ""  # the leader must be this holomem
    leader_card: str = ""  # ... or exactly this card
    no_leader_overlap: bool = False  # the leader's holomem not among the members
    require: tuple[str, ...] = ()  # these cards must be members
    exclude: tuple[str, ...] = ()  # these cards are not used at all
    # "5-star only" (the page's Card pool): the five MEMBERS are 5-star cards; the leader slot is
    # any rarity (an outfit).
    members_five_star: bool = False

    def key(self) -> tuple:
        return (
            self.leader_member,
            self.leader_card,
            self.no_leader_overlap,
            tuple(sorted(self.require)),
            tuple(sorted(self.exclude)),
            self.members_five_star,
        )


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
    `leaf_inputs.maxed_budget()` points), `infinite` (cubes / hexas ignored; off-team idle
    points on green), `all_invited` (a character with no points plans at that grant),
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
            not self.infinite or self.max_resets is not None or self.branches != "GBRY" or bool(self.targets)
        )

    def plan_opts(self) -> dict:
        """`board_plan_solver.solve_team_board` keyword options of this plan."""
        targets: dict = {}
        for chr_id, node, mode in self.targets:
            targets.setdefault(chr_id, {})[node] = mode
        return {
            "char_level": "max" if self.max_points else "current",
            "resources": "infinite" if self.infinite else "inventory",
            "max_resets": self.max_resets,
            "branches": self.branches,
            "targets": targets,
            "no_refund": self.no_refund and not self.infinite,
            "all_invited": self.all_invited,
            "reset_min_gain": 0.0 if self.infinite else self.reset_min_gain / 100.0,
        }

    def key(self) -> tuple:
        return (
            *self.engine_key(),
            str(self.max_resets),
            self.no_refund,
            self.branches,
            self.targets,
            self.reset_min_gain,
        )

    def engine_key(self) -> tuple:
        """The levers the engine's leaf is built from (`build_inputs`). The plan's other
        options (resets, refund, branches, targets, a reset's minimum gain) only shape the
        re-plan within the materials, which each search runs with its own: one engine
        serves them all, its workers' context identical byte for byte."""
        return (
            str(self.level),
            str(self.bloom),
            str(self.sp_training),
            self.keep_higher,
            self.boards,
            self.max_points,
            self.infinite,
            self.all_invited,
        )

    def notes(self) -> list[str]:
        out = []
        if self.replan:
            what = []
            if self.materials:
                what.append(
                    "within your cubes and hexas (a reset "
                    + ("refunds nothing" if self.no_refund else "refunds what it takes off")
                    + ")"
                )
            if self.max_resets is not None:
                what.append(f"at most {self.max_resets} resets")
            if self.reset_min_gain and self.materials:
                what.append(f"a reset only where it earns {self.reset_min_gain:g} % of the score")
            if self.branches != "GBRY":
                what.append("only the " + "/".join(self.branches) + " branches changed")
            if self.targets:
                what.append(f"{len(self.targets)} secondary target(s)")
            out.append(
                f"The best {REPLAN_CANDIDATES} teams of the search (picked from its "
                f"best {REPLAN_SCREEN} with their activation nodes chosen jointly) were re-planned "
                + ", ".join(what)
                + " and are ranked on those boards; the search itself "
                "did not see these constraints, so a team that only shines without "
                "them may sit lower."
            )
        return out

    def describe(self) -> str:
        if self.level == "account" and self.bloom == "account" and self.sp_training == "account":
            cards = "your cards as they are"
        else:
            parts = (
                ([f"level {self.level}"] if self.level != "account" else [])
                + ([f"bloom {self.bloom}"] if self.bloom != "account" else [])
                + ([f"SP training {self.sp_training}"] if self.sp_training != "account" else [])
            )
            cards = "your cards at " + ", ".join(parts) + (" (higher ones kept)" if self.keep_higher else "")
        if self.boards == "as_is":
            boards = "your Holomem Boards exactly as they are"
        else:
            extra = []
            if not self.infinite:
                extra.append("within your cubes and hexas" + (" (no refund)" if self.no_refund else ""))
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
            boards = (
                "each team's boards planned from scratch with "
                + (
                    f"every character's max points ({leaf_inputs.maxed_budget()})"
                    if self.max_points
                    else "each character's own points"
                )
                + ", "
                + ", ".join(extra)
            )
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
    return Assumptions(
        level=lever(raw.get("level"), 1, 80),
        bloom=bloom,
        sp_training=sp,
        keep_higher=bool(raw.get("keep_higher")),
        boards=boards,
        max_points=bool(raw.get("max_points")),
        infinite=bool(raw.get("infinite")),
        all_invited=bool(raw.get("all_invited")),
        max_resets=resets,
        no_refund=bool(raw.get("no_refund")),
        branches=branches,
        targets=tuple(sorted(set(targets))),
        reset_min_gain=gain,
    )


def _card_levers(ns, a: Assumptions):
    """`ns` with `a`'s card levers as `leaf_inputs.build_account` reads them."""
    if a.level != "account":
        ns.assume_level = a.level
    if a.bloom != "account":
        ns.assume_bloom = a.bloom
    if a.sp_training != "account":
        ns.assume_limit_break = a.sp_training
    ns.keep_higher_level = ns.keep_higher_bloom = a.keep_higher
    return ns


def account_bonuses(a: Assumptions) -> AccountBonuses | None:
    """The account's Memory / Member Power-Up bonuses under `a`'s card levers: the
    `cfg.account` the leaf of `build_inputs` scores with, without building the leaf.
    Raising every card's level raises the Member Power-Up, so the account's own
    figure is not the one an "Upgraded cards" search uses."""
    _cards, _owned, cfg = leaf_inputs.account_cards_and_cfg(_card_levers(leaf_inputs._namespace(), a))
    return cfg.account  # type: ignore[return-value]


def maxed_bonuses() -> AccountBonuses | None:
    """The Memory / Member Power-Up bonuses of the Maxed account basis: every card in the
    game owned at max level (the roster's power-up ceiling) and every poster the game has,
    as its leaf scores (`apply_collection(maxed=True)`)."""
    from hbuilder.inputs import apply_collection, load_inputs  # noqa: PLC0415

    cards, owned, _cfg, _prov = load_inputs(leaf_inputs.CARDS_PATH, leaf_inputs.OWNED_PATH)
    _cards, basis = apply_collection(cards, None, owned_ids=[c.id for c in owned], maxed=True)
    return basis.bonuses  # type: ignore[return-value]


def build_inputs(
    a: Assumptions,
    difficulty: str,
    song: str = "m0334",
    *,
    split_key: Callable[[Card], Hashable] | None = None,
) -> dict:
    """The leaf inputs of the user's account under `a` (the shape of
    `leaf_inputs.leaf_inputs`) -- shared by the engine, the event search and
    the row detail so a row is re-developed on the very leaf that ranked it.
    `split_key`: `leaf_inputs.build_account`'s (the event search keeps cards with
    different event bonuses apart)."""
    import json as _json  # noqa: PLC0415

    ns = _card_levers(leaf_inputs._namespace(song, difficulty), a)
    if a.boards == "as_is":
        state = _json.loads((DUMPS / "board_state.json").read_text(encoding="utf-8"))
        built = leaf_inputs.build_account(ns, bench="captured", board_state=state, split_key=split_key)
        # the account's boards: its socketed Connect cards as they are, not re-assigned
        return dict(built, ctx=dataclasses.replace(built["ctx"], board_state_slots=True))
    built = leaf_inputs.build_account(
        ns,
        flat_budget=leaf_inputs.maxed_budget() if a.max_points else None,
        bench="infinite" if a.infinite else "captured",
        split_key=split_key,
    )
    if a.all_invited and not a.max_points:
        # a holomem with no board points (not invited) plans at the max-level grant
        ctx = built["ctx"]
        top = leaf_inputs.maxed_budget()
        granted = {c: int(ctx.granted.get(c) or 0) or top for c in ctx.name_by_chr}
        built = dict(built, ctx=dataclasses.replace(ctx, granted=granted))
    # the generation greens' cap in the per-character plans: each chain valued at the
    # room the OTHER boards (this leaf's bench) leave under 900 (`board_plan.grouping_room`)
    from hbuilder.domain import board_plan as _bp  # noqa: PLC0415

    ctx = built["ctx"]
    room = _bp.grouping_room(ctx.skilltree, ctx.bench_characters, ctx.name_by_chr)
    supply = _bp.grouping_supply(ctx.skilltree, ctx.bench_characters, ctx.name_by_chr)
    built = dict(
        built,
        ctx=dataclasses.replace(
            ctx,
            skilltree={**ctx.skilltree, _bp.GROUPING_ROOM_KEY: room, _bp.GROUPING_SUPPLY_KEY: supply},
            # every board is planned, so a song's off-team singers' too (their yellows pay
            # on it; the materials re-plan plans them, `board_plan.singer_bench_characters`)
            plan_singer_bench=True,
            bench_sockets=bench_sockets_of(
                _json.loads((DUMPS / "board_state.json").read_text(encoding="utf-8"))
            ),
        ),
    )
    return built


def bench_sockets_of(state: Mapping) -> dict:
    """`board_plan.bench_sockets_from_state` (kept under this name for its callers)."""
    from hbuilder.domain.board_plan import bench_sockets_from_state  # noqa: PLC0415

    return bench_sockets_from_state(state)


class Engine:
    """The leaf of one (basis, difficulty[, assumptions]), alive between requests.
    With `assumptions` the basis is the user's account under them (`build_inputs`)."""

    play: tuple[str, bool] = ("mobile", False)  # (platform, auto): how the run is played

    def __init__(
        self,
        basis: str,
        difficulty: str,
        workers: int | None = None,
        assumptions: Assumptions | None = None,
        levels: tuple[int, int] | None = None,
        play: tuple[str, bool] = ("mobile", False),
    ) -> None:
        self.basis = basis
        self.play = tuple(play)  # type: ignore[assignment]
        self.assumptions = assumptions
        self.difficulty = leaf_inputs._difficulty(difficulty)
        # holodori.best's "any song" level range: every chart of ANY difficulty whose level
        # is in it (In general only; `difficulty` then only names the leaf's nominal chart)
        self.levels = tuple(levels) if levels else None
        t0 = time.perf_counter()
        built = (
            build_inputs(assumptions, self.difficulty)
            if assumptions is not None
            else leaf_inputs.leaf_inputs(basis, self.difficulty)
        )
        self.mf = members_first
        path = leaf_inputs.charts_path(self.difficulty)
        if self.levels:
            lo, hi = self.levels
            library = load_charts(str(leaf_inputs.charts_path("HARD")))  # charts_all.json
            self.charts = tuple(
                c
                for _k, c in sorted(library.charts.items())  # type: ignore[union-attr]
                if lo <= c.difficulty_level <= hi
            )
            if not self.charts:
                raise KeyError(f"no chart has a level in {lo}..{hi}")
            self.index = {}
        else:
            self.charts = load_charts(str(path)).at(self.difficulty)  # type: ignore[union-attr]
            self.index = {chart.music_id: k for k, chart in enumerate(self.charts)}
        digests = leaf_inputs.chart_digests(path, self.difficulty)
        if assumptions is not None:
            # any input file moving (a refresh) changes the account basis' key
            self.key = account_key(self.difficulty)
        else:
            _songs, self.key = leaf_inputs.keys_for(built["reference_key"], digests, basis, self.difficulty)
        self.built = built
        self.reps = list(built["reps"])
        self.by_id = built["by_id"]
        ctx = dataclasses.replace(
            with_play(built["ctx"], self.play), charts=tuple(self.charts), board_per_chart=True
        )
        self.workers = workers or default_workers()
        # the server's one set of workers (`shared_pool`): an engine adds its context to
        # them instead of a worker per core of its own
        self.pool = RefinePool(ctx, workers=self.workers, shared=shared_pool(self.workers))
        self.pool.__enter__()
        self.lock = threading.Lock()
        self.hints: dict[tuple, list[tuple[str, tuple[str, ...]]]] = {}
        self.started_seconds = time.perf_counter() - t0
        self.closed = False
        self._sample: tuple[int, ...] | None = None
        self._wide: tuple[int, ...] = ()
        self._counts_charts: tuple[int, ...] | None = None  # `_counts_funnel`'s sample

    def _within_materials(self, a, ranking, leaders, pool, song, chart_indices, cancel, progress=None):
        """The best REPLAN_CANDIDATES distinct teams re-planned within the
        wallet and re-scored on those boards (`materials_plan.plan_and_score`, in
        the engine's workers). Returns `(ranking of the re-planned teams, best
        first, [why a team could not be planned], {(leader index, members): whether
        that team's plan solved to optimality}, {(leader index, members): (team board
        state, bench) it is served on})`."""
        import materials_plan  # noqa: PLC0415

        opts = materials_plan.judged_opts(a.plan_opts(), a, self.difficulty, self.pool.ctx, self.levels)
        # every team on the planner's first plan; its other candidates below, for the rows
        # shown on a song and for every row off a song (`SHOWN_ROWS`)
        first = materials_plan.first_opts(opts)
        singers = set(self.charts[self.index[song]].singers or ()) if song in self.index else set()
        cands = [
            ranking[k]
            for k in replan_pick(ranking, lambda r: "chr-" + leaders[r[1]].id.split("-")[1] in singers)
        ]
        # each with the plan this process already holds for it (`carried_plan`): a filter
        # or option toggle re-plans mostly the teams the previous search planned
        items = [
            (
                k,
                leaders[li].id,
                tuple(pool[i].id for i in s),
                song,
                chart_indices,
                first,
                materials_plan.carried_plan(leaders[li].id, tuple(pool[i].id for i in s), song, first),
            )
            for k, (_v, li, s) in enumerate(cands)
        ]
        # the longest plans first, so they do not start in the last wave: a singing
        # leader's plan runs ~2x the solves (and about twice the time); the order changes
        # nothing else (by index)
        items.sort(key=lambda it: it[1].split("-")[1] not in {c.split("-")[1] for c in singers})
        # by its own index, never by position: the list is not in index order
        item_of = {it[0]: it for it in items}
        got: dict[int, float] = {}
        optimal: dict = {}
        held_of: dict = {}
        failed: list[str] = []

        def on_plan(out):
            index, value, _state, reason, held = out
            if value is None:
                failed.append(f"{leaders[cands[index][1]].member}: {reason}")
            else:
                got[index] = value
                held_of[index] = held
                optimal[cands[index][1:]] = None if held is None else held[2] is False
                materials_plan.remember(*item_of[index][1:4], item_of[index][5], held)

        # the rows shown (the best SHOWN_ROWS on a song, all of them off a song) re-planned
        # with the planner's other candidates (only ever higher), each row's as soon as it
        # is sure to be shown
        refined = materials_plan.plan_and_refine(
            self.pool,
            items,
            opts=opts,
            shown=SHOWN_ROWS if song else len(items),
            rank=lambda k, value: (-value, cands[k][1], cands[k][2]),
            cancel=cancel,
            progress=progress,
            on_plan=on_plan,
        )  # a real count: the page's ETA is a rate
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        for index in got:
            # the plan each row is served on, under this search's judge: the row's detail
            # opens on it, and a request with other levers plans its own
            if index in refined:
                got[index], _state, held_of[index] = refined[index]
                optimal[cands[index][1:]] = held_of[index][2] is False
            materials_plan.remember(*item_of[index][1:4], opts, held_of[index])
        out = sorted(
            ((got[k], li, s) for k, (_v, li, s) in enumerate(cands) if k in got),
            key=lambda r: (-r[0], r[1], r[2]),
        )
        if not out:
            raise KeyError("no team could be planned within your materials: " + "; ".join(failed[:3]))
        boards = {
            cands[k][1:]: (held_of[k][1][0], held_of[k][1][1])
            for k in got
            if held_of.get(k) is not None and len(held_of[k]) > 1 and held_of[k][1] is not None
        }
        return out, failed, optimal, boards

    def _ceilings(
        self,
        served,
        leaders,
        pool,
        chart_indices,
        on_refined,
        planned_boards,
        cancel,
        replan: bool,
        known: Mapping | None = None,
    ):
        """Each served row's `song_max`: the run score if every ACTIVE fires, at the slot
        order its expectation plays, on the board its expectation is served on (refined,
        re-planned within the materials, as they are, or the plain develop) -- the top of
        the row's run law, what the detail shows as Max and the page sorts by Max. One pass
        in the workers; a row whose board cannot be rebuilt keeps None. `known`: the ceilings
        already measured on the board a row is served on (its level-2 refinement), kept."""
        from hbuilder.domain.team import Team  # noqa: PLC0415

        lpos = {c.id: k for k, c in enumerate(leaders)}
        position = {c.id: k for k, c in enumerate(pool)}
        keys, teams, refine, planned = [], [], [], []
        for row in served:
            li = lpos.get(row["leader"])
            if li is None or not all(m in position for m in row["members"]):
                continue
            s = tuple(sorted(position[m] for m in row["members"]))
            if known and (li, s) in known:
                row["song_max"] = float(known[(li, s)])
                continue
            if replan:
                board = planned_boards.get((li, s))
                if board is None:
                    continue
            else:
                board = None
            keys.append(row)
            teams.append(Team(leader=leaders[li], members=tuple(pool[i] for i in s)))
            refine.append(on_refined.get((li, s), 0))
            planned.append(board)
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        for row, value in zip(keys, self.pool.batch_ceiling(teams, chart_indices, refine, planned)):
            row["song_max"] = float(value)

    def _count_funnel(self, ranking, leaders, pool, chart_indices, lens: bool = False):
        """The REFINE_HEAD teams to refine, chosen from the search's best COUNTS_FUNNEL by a
        cheap estimate of their frequency node counts chosen (`parallel_refine.
        count_screen_value`, ~5 ms a team) rather than by the plain value, which buys every
        frequency node and so ranks some teams up to ~5 % low. Each keeps its plain value.
        `lens`: the ranking's values are the search's lens (`LENS_SEARCH`), so each team's
        plain value is scored here too, with its count screen, in one develop."""
        from hbuilder.domain.team import Team  # noqa: PLC0415

        funnel = list(ranking[:COUNTS_FUNNEL])
        if lens:
            teams = [Team(leader=leaders[li], members=tuple(pool[i] for i in s)) for _v, li, s in funnel]
            both: list[tuple[float, float]] = self.pool.batch_score(  # type: ignore[assignment]
                teams,
                chart_indices=chart_indices,
                refine_activation="screen_both",  # type: ignore[arg-type]
            )
            order = sorted(range(len(funnel)), key=lambda k: (-max(float(both[k][1]), float(both[k][0])), k))
            return [(float(both[k][0]), funnel[k][1], funnel[k][2]) for k in order[:REFINE_HEAD]]
        if len(funnel) <= REFINE_HEAD:
            return funnel
        teams = [Team(leader=leaders[li], members=tuple(pool[i] for i in s)) for _v, li, s in funnel]
        screened = self.pool.batch_score(
            teams,
            chart_indices=chart_indices,
            refine_activation="screen",  # type: ignore[arg-type]
        )
        order = sorted(range(len(funnel)), key=lambda k: (-max(float(screened[k]), float(funnel[k][0])), k))
        return [funnel[k] for k in order[:REFINE_HEAD]]

    def _counts_funnel(self, ranking, leaders, pool):
        """The GENERAL_COUNTS_HEAD re-scored teams (In general) that get their frequency
        node counts chosen on every chart, in `ranking`'s order: from its best
        GENERAL_COUNTS_FUNNEL, the ones whose full plain mean times what the counts add on
        GENERAL_COUNTS_SAMPLE note-spread charts is highest. The plain mean alone orders
        them badly (a team's gain runs from 0.1 to 1.9 %). Each keeps its plain value."""
        from hbuilder.domain.team import Team  # noqa: PLC0415

        funnel = list(ranking[:GENERAL_COUNTS_FUNNEL])
        if len(funnel) <= GENERAL_COUNTS_HEAD or len(self.charts) <= GENERAL_COUNTS_SAMPLE:
            return funnel[:GENERAL_COUNTS_HEAD]
        if self._counts_charts is None:
            by_obj = {id(c): k for k, c in enumerate(self.charts)}
            self._counts_charts = tuple(
                by_obj[id(c)] for c in two_stage.chart_sample(self.charts, GENERAL_COUNTS_SAMPLE)
            )
        teams = [Team(leader=leaders[li], members=tuple(pool[i] for i in s)) for _v, li, s in funnel]
        both = self.pool.batch_score(teams, chart_indices=self._counts_charts, refine_activation=COUNTS_BOTH)
        estimate = [
            float(v) * (float(c) / float(p) if p else 1.0) for (v, _li, _s), (p, c) in zip(funnel, both)
        ]
        best = sorted(range(len(funnel)), key=lambda k: (-estimate[k], k))[:GENERAL_COUNTS_HEAD]
        return [funnel[k] for k in sorted(best)]

    def _count_cooldowns(self, chosen, teams, refined, chart_indices):
        """The refined teams within COUNTS_MARGIN of the best (at most COUNTS_HEAD) refined
        once more at level 2 (every combination of the members' frequency node counts).
        Returns the values, the level-2 ones where they are higher, each team's level, and
        `{chosen index: ceiling}` for the teams served at level 2 (measured on that board)."""
        refined = list(refined)
        levels = [1] * len(refined)
        ceilings: dict[int, float] = {}
        if not refined or not COUNTS_HEAD:
            return refined, levels, ceilings
        values = [max(float(v), float(plain)) for (plain, _li, _s), v in zip(chosen, refined)]
        floor = max(values) * (1.0 - COUNTS_MARGIN)
        near = sorted((k for k, v in enumerate(values) if v >= floor), key=lambda k: (-values[k], k))[
            :COUNTS_HEAD
        ]
        counted = self.pool.batch_score_and_ceiling([teams[k] for k in near], chart_indices, 2)
        for k, (v, top) in zip(near, counted):
            if float(v) > float(refined[k]):
                refined[k] = v
                levels[k] = 2
                ceilings[k] = top
        return refined, levels, ceilings

    def _screen_for_replan(self, ranking, leaders, pool, chart_indices):
        """The search's best REPLAN_SCREEN teams re-ranked by their score with the
        activation nodes chosen jointly (each keeps the better of the two), so the
        REPLAN_CANDIDATES the exact plan gets are the ones its staircase favours.
        Only the order changes: the served numbers are the re-planned ones."""
        from hbuilder.domain.team import Team  # noqa: PLC0415

        head = ranking[:REPLAN_SCREEN]
        teams = [Team(leader=leaders[li], members=tuple(pool[i] for i in s)) for _v, li, s in head]
        refined = self.pool.batch_score(teams, chart_indices=chart_indices, refine_activation=True)
        head = sorted(
            ((max(float(v), float(r)), li, s) for (v, li, s), r in zip(head, refined)),
            key=lambda r: (-r[0], r[1], r[2]),
        )
        return head + list(ranking[REPLAN_SCREEN:])

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.pool.__exit__(None, None, None)

    # ------------------------------------------------------------------ sample
    def general_sample(
        self, *, emit: Callable[[dict], None] | None = None, cancel: threading.Event | None = None
    ) -> tuple[int, ...]:
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
        teams = sorted(
            (
                Team(leader=rng.choice(self.reps), members=tuple(self.reps[i] for i in s))
                for s in two_stage.random_legal_sets(holomems, SAMPLE_TRAIN, rng)
            ),
            key=lambda t: t.leader.id,
        )
        scores: list[list[float]] = []
        step = max(1, len(teams) // 6)
        for at in range(0, len(teams), step):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            with self.lock:
                scores += self.pool.batch_score_per_chart(teams[at : at + step])
            if emit:
                emit(
                    {
                        "type": "progress",
                        "stage": "search",
                        "sub": "sample",
                        "done": len(scores),
                        "total": len(teams),
                    }
                )
        wide = tuple(two_stage.fitted_chart_sample(scores, max(GENERAL_WIDE, FITTED_SAMPLE)))
        self._wide, self._sample = wide, wide[:FITTED_SAMPLE]
        return self._sample

    # ------------------------------------------------------------------ search
    def search(
        self,
        song: str | None,
        filters: Filters | None = None,
        *,
        emit: Callable[[dict], None] | None = None,
        cancel: threading.Event | None = None,
        top: int = RANKING,
        seed: int = 20261016,
        assumptions: Assumptions | None = None,
    ) -> dict:
        """`assumptions`: this search's (default the engine's); they may differ from the
        engine's only in what `Assumptions.engine_key` leaves out."""
        a = self.assumptions if assumptions is None else assumptions
        if (a is None) != (self.assumptions is None) or (
            a is not None and a.engine_key() != self.assumptions.engine_key()  # type: ignore[union-attr]
        ):
            raise ValueError("these assumptions are scored on another engine")
        replan = a is not None and a.replan
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
            raise KeyError(
                f"required cards not in this pool: {missing}"
                + (" (Card pool is 5-star only)" if filters.members_five_star else "")
            )
        require = [position[c] for c in filters.require]
        leaders = [
            c
            for c in usable
            if (not filters.leader_card or c.id == filters.leader_card)
            and (not filters.leader_member or c.member == filters.leader_member)
        ]
        if not leaders:
            raise KeyError("no card in the pool matches the leader filter")
        allowed = None
        if filters.no_leader_overlap:

            def allowed(li, s, leaders=leaders, pool=pool):
                return all(pool[i].member != leaders[li].member for i in s)

        lpos = {c.id: k for k, c in enumerate(leaders)}
        # the plan's options too: a search seeded by another option's teams would not be
        # the one that option gets on an engine of its own
        hint_key = (song, filters.key(), a.key() if a is not None else None)
        # the song's singers lead with every proposed set (`joint_search`'s `always`): the
        # screen learns each set under its best leader, so a singer whose teams rank a few
        # % lower would be scored on a handful of sets and her own best sets never proposed
        # under her (a singer-filtered search can find a team above the unfiltered #1)
        sing = set((chart.singers or ()) if chart is not None else ())
        always = [k for k, c in enumerate(leaders) if "chr-" + c.id.split("-")[1] in sing]
        seeds = [
            (lpos[l], tuple(sorted(position[m] for m in ms)))
            for l, ms in self.hints.get(hint_key, ())
            if l in lpos and all(m in position for m in ms)
        ]

        from hbuilder.domain.team import Team  # noqa: PLC0415

        # boards as they are (nothing to plan) or re-planned within the materials after the
        # search: no frequency count to choose on the board the search develops
        as_is = a is not None and (a.boards == "as_is" or a.replan)
        lens = LENS_SEARCH and not general and not as_is

        def score_pairs(pairs):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            teams = [Team(leader=leaders[li], members=tuple(pool[i] for i in s)) for li, s in pairs]
            return self.pool.batch_score(
                teams, chart_indices=chart_indices, refine_activation="lens" if lens else None
            )

        # In general on the two-tier (fitted) sample the answer merges GENERAL_STARTS searches
        starts = GENERAL_STARTS if general and len(self._wide) > len(chart_indices) else 1
        on_round = SearchRounds(emit, starts)

        # every leader region is seeded (`outfit_tilt`), In general and on one song alike: on
        # one song the gate repair also missed whole regions (Lui / Ocean Elegance's holoX set
        # on m0155, +1.0 %; Okayu / Salty Breeze Runway's GAMERS set on m0342)
        tilt_of = outfit_tilt(pool)
        t0 = time.perf_counter()
        with self.lock:
            found = joint_search.search(
                pool,
                leaders,
                score_pairs,
                gate_of=self.mf.gate_of,
                require=require,
                allowed=allowed,
                seeds=seeds,
                always=always,
                seed=seed,
                on_round=on_round,
                tilt_of=tilt_of,
            )
            ranking = found["ranking"]
            if starts > 1:
                # In general on the two-tier (fitted) sample: further independent starts, merged. One search settles in
                # one region of teams; the best can sit four members away and never be
                # proposed: separate seeds can settle on different sets even where the
                # sample ranks them the same way as every chart does, so it is the reach
                merged = {(li, s): v for v, li, s in ranking}
                extra: Any
                for extra in range(1, starts):
                    on_round.next_start()
                    more = joint_search.search(
                        pool,
                        leaders,
                        score_pairs,
                        gate_of=self.mf.gate_of,
                        require=require,
                        allowed=allowed,
                        seed=seed + 104729 * extra,
                        on_round=on_round,
                        tilt_of=tilt_of,
                    )
                    merged.update({(li, s): v for v, li, s in more["ranking"]})
                ranking = sorted(
                    ((v, li, s) for (li, s), v in merged.items()), key=lambda r: (-r[0], r[1], r[2])
                )
            search_seconds = time.perf_counter() - t0
            if not ranking:
                raise KeyError("no legal team satisfies the filters")
            if emit:
                emit({"type": "progress", "stage": "refine", "evaluated": 0})
            if general:
                # the climb on the wide sample, from the search's best teams; its
                # best go to the full re-score first, then the search's own order
                wide = tuple(self._wide)
                climbed: dict = {"ranking": [], "stats": {"evals": 0}}
                if len(wide) > len(chart_indices):

                    def score_wide(pairs):
                        if cancel is not None and cancel.is_set():
                            raise Cancelled()
                        teams = [
                            Team(leader=leaders[li], members=tuple(pool[i] for i in s)) for li, s in pairs
                        ]
                        return self.pool.batch_score(teams, chart_indices=wide)

                    climbed = joint_search.climb(
                        pool,
                        leaders,
                        score_wide,
                        [(li, s) for _v, li, s in ranking[:CLIMB_START]],
                        polish=CLIMB_POLISH,
                        require=require,
                        allowed=allowed,
                    )
                # the climb's best CLIMB_RESCORE AND the search's own best GENERAL_RESCORE:
                # a climb scores hundreds of swaps, and letting them fill the re-score
                # would push the search's #1 out (a repeat seeded with the first answer
                # could serve a lower team)
                order = list(
                    dict.fromkeys(
                        [(li, s) for _v, li, s in climbed["ranking"][:CLIMB_RESCORE]]
                        + [(li, s) for _v, li, s in ranking[:GENERAL_RESCORE]]
                    )
                )
                # every chart: the samples only chose the candidates; the served
                # numbers are the full means (plain leaf, board rebuilt per chart)
                cands = [(None, li, s) for li, s in order]
                teams = [Team(leader=leaders[li], members=tuple(pool[i] for i in s)) for _v, li, s in cands]
                full = self.pool.batch_score(teams, chart_indices=None)
                sample_rank = {(li, s): k for k, (_v, li, s) in enumerate(cands, 1)}
                ranking = sorted(
                    ((float(v), li, s) for (_p, li, s), v in zip(cands, full)),
                    key=lambda r: (-r[0], r[1], r[2]),
                )
                if len(wide) > len(chart_indices) and SETTLE_FROM:
                    # SETTLE: the climb swapped the best teams BY THE WIDE SAMPLE, which
                    # does not order them as every chart does -- the full re-score's #1
                    # may never have had its swaps tried (a team ONE swap away can score
                    # higher on every chart and on both samples). So the
                    # climb again from the full re-score's best, and its best on every chart
                    settled = joint_search.climb(
                        pool,
                        leaders,
                        score_wide,
                        [(li, s) for _v, li, s in ranking[:SETTLE_FROM]],
                        neighbours=SETTLE_FROM,
                        polish=SETTLE_FROM,
                        require=require,
                        allowed=allowed,
                        pair_moves=SETTLE_PAIR_MOVES,
                    )
                    have = {(li, s) for _v, li, s in ranking}
                    extra = [(li, s) for _v, li, s in settled["ranking"] if (li, s) not in have][
                        :SETTLE_RESCORE
                    ]
                    if extra:
                        values = self.pool.batch_score(
                            [Team(leader=leaders[li], members=tuple(pool[i] for i in s)) for li, s in extra],
                            chart_indices=None,
                        )
                        for k, ((li, s), v) in enumerate(zip(extra, values), len(cands) + 1):
                            sample_rank[(li, s)] = k
                            ranking.append((float(v), li, s))
                        ranking.sort(key=lambda r: (-r[0], r[1], r[2]))
                chosen, refined, levels, counted_ceilings = [], [], [], {}
                if not as_is and GENERAL_COUNTS_HEAD:
                    # the best teams again on every chart, each chart with the members'
                    # frequency node counts chosen for it (`parallel_refine.ChartCounts`)
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    chosen = self._counts_funnel(ranking, leaders, pool)
                    refined = self.pool.batch_score(
                        [Team(leader=leaders[li], members=tuple(pool[i] for i in s)) for _v, li, s in chosen],
                        chart_indices=None,
                        refine_activation=COUNTS,  # type: ignore[arg-type]
                    )
                    levels = [COUNTS] * len(chosen)
            else:
                # the refinement: the best teams with their activation nodes chosen jointly; no
                # activation refinement on a board that is not the one served (`as_is` above)
                chosen = [] if as_is else self._count_funnel(ranking, leaders, pool, chart_indices, lens=lens)
                teams = [Team(leader=leaders[li], members=tuple(pool[i] for i in s)) for _v, li, s in chosen]
                refined = (
                    self.pool.batch_score(teams, chart_indices=chart_indices, refine_activation=True)
                    if teams
                    else []
                )
                refined, levels, counted_ceilings = self._count_cooldowns(
                    chosen, teams, refined, chart_indices
                )
                if replan:
                    ranking = self._screen_for_replan(ranking, leaders, pool, chart_indices)
        t_refine = time.perf_counter() - t0 - search_seconds
        better = {(li, s): max(float(v), plain) for (plain, li, s), v in zip(chosen, refined)}
        # the rows served on their REFINED board, with the refinement level the value was
        # served at (a refinement that found nothing better leaves the row on the plain
        # develop's)
        on_refined = {
            (li, s): levels[k]
            for k, ((plain, li, s), v) in enumerate(zip(chosen, refined))
            if float(v) >= plain
        }
        materials_failed: list[str] = []
        plan_optimal: dict | None = None  # set when the boards were re-planned
        planned_boards: dict = {}  # (leader index, set) -> its re-planned board
        t_replan = time.perf_counter()
        if replan:
            if emit:
                emit({"type": "progress", "stage": "refine", "evaluated": 0, "sub": "materials"})
            ranking, materials_failed, plan_optimal, planned_boards = self._within_materials(
                a,
                ranking,
                leaders,
                pool,
                None if general else song,
                None if general else chart_indices,
                cancel,
                progress=(
                    lambda k, n: emit(
                        {"type": "progress", "stage": "refine", "sub": "materials", "done": k, "total": n}
                    )
                )
                if emit
                else None,
            )
            better = {}
        t_replan = time.perf_counter() - t_replan
        singers = set((chart.singers or ()) if chart is not None else ())

        def team(value, li, s):
            leader = leaders[li]
            members = [pool[i] for i in s]
            ref = better.get((li, s))
            row = {
                "leader": leader.id,
                "leader_name": f"{leader.member} / {leader.costume_name}",
                "sings": "chr-" + leader.id.split("-")[1] in singers,
                "status": "searched",
                "best": ref if ref is not None else value,
                "best_plain": value if ref is not None else None,
                "members": [c.id for c in members],
                "member_names": [c.member for c in members],
                "member_order": None,
                # the refinement level the value was served at: True (1) moves the
                # activation nodes one at a time, 2 also tried every frequency count;
                # In general, COUNTS: the frequency counts chosen chart by chart
                "refined": (
                    (True if on_refined.get((li, s), 1) == 1 else on_refined[(li, s)])
                    if ref is not None
                    else False
                ),
                "p10": None,
                "p50": None,
                "p90": None,
                "song_max": None,
                "dist_mean": None,
                "dist_verified": None,
            }
            if plan_optimal is not None:
                # False: a MILP of this team's plan stopped on the solver's time limit
                row["plan_optimal"] = plan_optimal.get((li, s))
            return row

        # refined: only the refined head, so every served value is on the same basis
        served = [team(v, li, s) for v, li, s in (chosen if better else ranking)]
        served.sort(key=lambda t: -t["best"])
        served = served[: max(top, RANKING)]
        t_ceiling = time.perf_counter()
        if not general and served:
            # the level-2 rows' ceilings were measured with their expectation (one develop)
            known = {
                (li, s): counted_ceilings[k]
                for k, (_plain, li, s) in enumerate(chosen)
                if k in counted_ceilings and on_refined.get((li, s)) == 2
            }
            self._ceilings(
                served, leaders, pool, chart_indices, on_refined, planned_boards, cancel, replan, known
            )
        t_ceiling = time.perf_counter() - t_ceiling
        # each leader's best team, with its next sets as alternatives
        by_leader: dict[str, list[dict]]
        rows, by_leader = [], {}
        for t in served:
            by_leader.setdefault(t["leader"], []).append(t)
        for ts in by_leader.values():
            head = dict(ts[0])
            head["alternatives"] = [
                {
                    "members": t["members"],
                    "member_names": t["member_names"],
                    "best": t["best_plain"] or t["best"],
                    "refined": False,
                    **({"plan_optimal": t["plan_optimal"]} if "plan_optimal" in t else {}),
                }
                for t in ts[1:5]
            ]
            rows.append(head)
        rows.sort(key=lambda t: -t["best"])
        self.hints[hint_key] = [
            (leaders[li].id, tuple(pool[i].id for i in s)) for _v, li, s in ranking[:HINTS]
        ]
        stats = found["stats"]
        extra = {}
        # In general with the frequency counts chosen per chart for the rows served
        counted = general and bool(better)
        if general:
            first = (lpos[served[0]["leader"]], tuple(sorted(position[m] for m in served[0]["members"])))
            extra = {
                "charts": len(self.charts),
                "levels": list(self.levels) if self.levels else None,
                "sample": [f"{c.music_id}/{c.difficulty}" if self.levels else c.music_id for c in sample],
                "rescored": len(ranking),
                # where the served #1 stood in the sample's order (1 = the sample agreed)
                "best_sample_rank": sample_rank.get(first),
            }
        per_song = "the board rebuilt per song" + (
            " and its activation-frequency nodes chosen per song" if counted else ""
        )
        return {
            **extra,
            "scope": "general" if general else "song",
            "song": song or None,
            "certified": False,
            "state": "fresh",
            "basis_id": self.basis,
            "basis": (a.describe() if a is not None else leaf_inputs.basis_text(self.basis)),
            "assumptions": dataclasses.asdict(a) if a else None,
            "notes": a.notes() if a else [],
            "difficulty": self.difficulty,
            "written": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "objective": (
                (
                    f"mean score over every chart of level {self.levels[0]}-{self.levels[1]} "
                    f"({len(self.charts)} charts, any difficulty), {per_song}"
                )
                if general and self.levels
                else f"mean score over every {self.difficulty} chart, {per_song}"
                if general
                else f"expected score on this song's {self.difficulty} chart"
            ),
            "method": (
                "joint leader + members search on a chart sample, run for this request "
                "(no cache); the best teams re-scored on every chart"
                + (
                    f", the best {GENERAL_COUNTS_HEAD} of them with each song's "
                    "activation-frequency nodes chosen"
                    if counted
                    else ""
                )
                if general
                else "joint leader + members search, run for this request "
                "(no cache); the best teams re-scored with the activation refinement"
            ),
            "on_demand": True,
            "play": {"platform": self.play[0], "auto": self.play[1]},
            "singers": sorted(singers),
            "singer_type": getattr(chart, "singer_type", None),
            "leaders": len(leaders),
            "searched_leaders": len(leaders),
            "legal_sets": stats.get("legal_sets"),
            "exact_evals": stats.get("exact_evals"),
            "seconds": round(time.perf_counter() - t0, 2),
            # `replan`: the exact plans within the materials, most of a search that has
            # them (about two thirds of the time); `refine` stops before them
            "timings": {
                "search": round(search_seconds, 2),
                "refine": round(t_refine, 2),
                "replan": round(t_replan, 2),
                "ceiling": round(t_ceiling, 2),
                "engine_start": round(self.started_seconds, 2),
            },
            "seeded": len(seeds),
            "filters": dataclasses.asdict(filters),
            "materials_failed": materials_failed,
            "build": {"running": False},
            "rows": rows[:ROWS],
            "ranking": served[:top],
            "ranking_size": len(served),
        }


_ENGINES: dict[tuple, Engine] = {}
_ENGINES_LOCK = threading.Lock()
# live engines kept at once (each holds a worker per core); the least recently
# used one is closed when another is needed
MAX_ENGINES = 2

_SHARED: dict[int, SharedPool] = {}
_SHARED_LOCK = threading.Lock()


def shared_pool(workers: int | None) -> SharedPool | None:
    """The server's worker processes for `workers` (None for one: in-process), started on
    first use and kept: every engine and the event search run their contexts on them
    (`parallel_refine.SharedPool`), so two engines cost one worker per core, not two."""
    if workers is None or workers <= 1:
        return None
    with _SHARED_LOCK:
        held = _SHARED.get(workers)
        if held is None:
            held = _SHARED[workers] = SharedPool(workers)
        return held


# account files a refresh rewrites that the scoring key (`leaf_inputs.today_keys`) does not hash:
# the boards as they stand -- socketed cards and nodes (`board_state.json`, read for the
# bench sockets and "boards as they are") -- and the materials (`inventory.json`, the
# wallet the re-plan spends). Without them a refresh that only moved a socket or the
# wallet would leave the engine on the old account.
ACCOUNT_STATE_FILES = ("board_state.json", "inventory.json")


_BASIS_KEYS: dict[str, tuple[float, str | None]] = {}
# the key re-reads and digests the input files (~0.5 s): recomputed at most this often
_BASIS_KEY_TTL = 20.0


def basis_key(basis: str, difficulty: str) -> str | None:
    """Today's key of `basis`'s inputs (`leaf_inputs.today_keys`, the in-general key), or None
    when they cannot be read: an engine is rebuilt when it moves."""
    now = time.monotonic()
    slot = f"{basis}/{difficulty}"
    held = _BASIS_KEYS.get(slot)
    if held is not None and now - held[0] < _BASIS_KEY_TTL:
        return held[1]
    try:
        key = leaf_inputs.today_keys(basis, difficulty)[1]
    except Exception:  # noqa: BLE001 - "cannot vouch" keeps the engine in hand
        return None
    _BASIS_KEYS[slot] = (now, key)
    return key


def account_key(difficulty: str) -> str | None:
    """Today's key of the account's inputs (None when unreadable)."""
    try:
        base = leaf_inputs.today_keys("account", difficulty)[1]
    except Exception:  # noqa: BLE001 - "cannot vouch" keeps the engine in hand
        return None
    import hashlib  # noqa: PLC0415

    h = hashlib.sha256(base.encode("utf-8"))
    for name in ACCOUNT_STATE_FILES:
        try:
            h.update(name.encode() + b"=" + hashlib.sha256((DUMPS / name).read_bytes()).digest())
        except OSError:
            h.update(name.encode() + b"=missing")
    return h.hexdigest()


def engine(
    basis: str,
    difficulty: str,
    *,
    workers: int | None = None,
    current_key: Callable[[], str | None] | None = None,
    assumptions: Assumptions | None = None,
    levels: tuple[int, int] | None = None,
    play: tuple[str, bool] = ("mobile", False),
) -> Engine:
    """The live engine of (basis, difficulty[, assumptions]), rebuilt when the
    inputs' key moved (`current_key()` -> today's key, or None when it cannot
    be read: then the engine in hand is kept)."""
    difficulty = leaf_inputs._difficulty(difficulty)
    # the plan's options that shape only the re-plan share one engine (`engine_key`)
    slot = (
        basis,
        difficulty,
        assumptions.engine_key() if assumptions is not None else None,
        tuple(levels) if levels else None,
        tuple(play),
    )
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
            held = Engine(
                basis, difficulty, workers=workers, assumptions=assumptions, levels=levels, play=play
            )
        _ENGINES[slot] = held  # re-inserted last: most recently used
        return held


PLATFORMS = ("mobile", "pc")


def parse_play(body: Mapping) -> tuple[str, bool]:
    """`(platform, auto)` of a request: how the run is played. `platform` picks the
    LiveNote PERFECT column; `auto` an AUTO run."""
    platform = str(body.get("platform") or "").lower()
    return (platform if platform in PLATFORMS else "mobile", body.get("auto") is True)


def with_play(ctx: RefineContext, play: tuple[str, bool]) -> RefineContext:
    """`ctx` (a `RefineContext`) scored for `play`. AUTO sets BOTH halves, as
    `hbweb.basis.auto_premise` does for the other routes: the AUTO judgement column (a
    note scores like a GREAT, ~80 % of manual) AND `run_state.combo = 0`,
    since on AUTO the combo never accumulates and a `combo >= N` override never fires.
    The on-demand and event searches must honour the page's Auto toggle, or every AUTO
    request is ranked as a manual run."""
    platform, auto = play
    cfg = dataclasses.replace(ctx.cfg, platform=platform)
    if auto:
        cfg = dataclasses.replace(cfg, song_auto=True, run_state=dataclasses.replace(cfg.run_state, combo=0))
    return dataclasses.replace(ctx, cfg=cfg)


def parse_filters(body: Mapping) -> Filters:
    def ids(v) -> tuple[str, ...]:
        if isinstance(v, str):
            v = [x for x in v.split(",") if x]
        return tuple(str(x) for x in (v or []) if x)

    return Filters(
        leader_member=str(body.get("leader_member") or ""),
        leader_card=str(body.get("leader_card") or ""),
        no_leader_overlap=bool(body.get("no_leader_overlap")),
        require=ids(body.get("require")),
        exclude=ids(body.get("exclude")),
        members_five_star=str(body.get("pool") or "").lower() == "5star",
    )
