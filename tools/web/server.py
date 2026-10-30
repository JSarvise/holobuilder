"""The local Solve -> Result web view: stdlib `http.server` on 127.0.0.1.

The four decisions this file implements:

* **stdlib only, bound to loopback.** No Flask, no FastAPI, no new
  dependency. The page is for local use, and `127.0.0.1` enforces that
  rather than being a default that happens to be safe -- so the bind
  address is a module constant, not a flag.
* **The solve is ~10.7 s, not 1.41 s.** 1.41 s is beam
  width 8 measured in isolation; the shipped path pays four seeds, the
  one-card-swap polish and the failure-label diagnostics on top. That is
  a job, not a spinner: `POST /solve` starts one and returns an id, and
  the page reads real progress off `GET /events/<id>` as Server-Sent
  Events. Progress is the solver's own `progress` callback -- the same
  one the CLI turns into `progress: N arrangements evaluated` -- never a
  fabricated animation.
* **In-process** (brief): the solver is imported, not shelled out to.
  `src/` is on the pythonpath per `pyproject.toml`.
* **The write prohibition, structurally.** Nothing in `tools/web/`
  may write `data/cards.json` or `data/owned.json`. This module never
  opens them at all: it hands two `Path` constants to
  `hbuilder.cli.load_inputs`, the project's single shared read path,
  and `tools/web/test_no_writes.py` walks this file's AST and
  fails if either constant reaches anything but a whitelisted read sink.

Cancellation is the progress callback raising `_Cancelled`. That works
because the beam calls `progress` from inside its own hot loop and
catches nothing broad on the way out (verified against
`solver/heuristic.py` -- it has no bare `except Exception`), so the
exception unwinds the search instead of being swallowed into a plausible
wrong answer.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import functools
import http.server
import importlib.util
import json
import os
import queue
import re
import sys
import threading
import time
import types
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from hbuilder.cli import (  # noqa: E402
    COLLECTION_CAPS_PATH,
    COLLECTION_CURVE_PATH,
    DEFAULT_BOARD_PATH,
    DEFAULT_CHARTS_PATH,
    DEFAULT_SONG_POOL,
    apply_collection,
    load_board_basis,
    _LoadFailure,
    _scoring_config_document,
    format_approximation,
    load_inputs,
    should_use_heuristic,
)
from hbuilder.domain.collection import (  # noqa: E402
    CollectionError,
    FULLY_SCORABLE_BLOOM,
    MEMBER_POWER_UP_CAP_PERMYRIAD,
    MEMORY_BONUS_MAX_POSTERS,
    PLAYER_LEVEL_MAX,
    PLAYER_LEVEL_MIN,
    account_bonuses_from_mapping,
    connect_bonus_level,
    max_member_power_up_permyriad,
    memory_bonus_permil,
    SKILL_BLOOM_STEP,
    caps_by_rarity_from_mapping,
    resolve_card_state,
    stat_basis_from_mapping,
)
from hbuilder.domain.board import BoardError, board_from_mapping  # noqa: E402
from hbuilder.domain.scoring import (  # noqa: E402
    deck_screen_unit_score,
    score_with_breakdown,
)
from hbuilder.domain.scoring_config import (  # noqa: E402
    ACTIVE_UPTIME_MODELS,
    DEFAULT_PLATFORM,
    PLATFORMS,
)
from hbuilder.domain.skilltree import (  # noqa: E402
    ROOT_NODE,
    TOTAL_BOARD_POINTS,
    player_level_requirement,
)
from hbuilder.domain.song import (  # noqa: E402
    SongError,
    charts_from_mapping,
    load_charts,
    score_song_detailed,
    active_coverage,
    timeline,
)
from hbuilder.domain.song_rank import rank_on_chart, score_team_ceiling, score_team_on_chart  # noqa: E402
from hbuilder.solver.parallel_songs import rank_across_charts_parallel  # noqa: E402
from hbuilder.solver.parallel_refine import RefineContext, RefinePool  # noqa: E402
from hbuilder.domain.team import MEMBER_COUNT, Team, TeamError  # noqa: E402
from hbuilder.domain.board_plan import (  # noqa: E402
    BoardOptimisedTeam,
    PlanCache,
    SongCast,
    song_cast,
    TeamBoardPlan,
    develop_refined_team,
    optimize_and_rerank,
    refine_members,
    node_effect_ids_from_master,
    skill_tree_from_dumps,
    model_for_character,
    board_points_from_snapshot,
)
from hbuilder.output.assumptions import format_assumptions  # noqa: E402
from hbuilder.output.explain import format_explain  # noqa: E402
from hbuilder.output.json_output import (  # noqa: E402
    build_payload,
    build_score_terms,
    build_team_object,
)
from hbuilder.solver.estimate import (  # noqa: E402
    Projection,
    ThroughputSchemaError,
    config_hash,
    load_throughput,
    project_arrangements,
    project_duration,
)
from hbuilder.solver.exact import RankedTeam, solve_exact  # noqa: E402
from hbuilder.solver.heuristic import solve_heuristic_labelled  # noqa: E402
from hbuilder.solver.selectors import (  # noqa: E402
    CandidatePool,
    SelectorError,
    Selectors,
    constraint_from,
    resolve_selectors,
    set_constraint_from,
)
import gacha_view  # noqa: E402
import compare_view  # noqa: E402  (POST /api/compare: two hand-picked teams, side by side)
import row_detail  # noqa: E402  (one number set per row: figures, panel, split, law, board rebuild)
import board_values  # noqa: E402  (node values + Connect boosts for /api/board-doc)
import certificate as cert_mod  # noqa: E402  (tools/web/certificate.py: inputs_hash recipe)
import certindex  # noqa: E402  (read-only EXACT/BOUNDED certificate lookup + row objects)
import seed_rosters  # noqa: E402  (read-only seed rosters unioned into the develop set)
import members_first_index  # noqa: E402  (read-only cached members-first enumerations -> seeds)

# How many distinct-leader teams `leader_diverse` develops by board-off rank
# (one best team per leader from a single beam) — EVERY 5-star leader is kept on
# top of this, whatever its board-off rank (see the leader_diverse branch), so
# the develop + per-song/across re-score ranks all ~66 5-star leaders plus the
# best 4-star ones, ~80 teams on the maxed pool.
LEADER_DIVERSE_POOL = 60


def _web_workers() -> int | None:
    """Process count for the web's refine / across pools: `HBUILDER_WEB_WORKERS`
    when set (a measurement next to another batch job must not take every
    core), else the solver's own default (every logical core)."""
    raw = os.environ.get("HBUILDER_WEB_WORKERS", "").strip()
    try:
        return max(1, int(raw)) if raw else None
    except ValueError:
        return None

# ---------------------------------------------------------------------------
# Paths. These two are READ-ONLY and `test_no_writes.py` enforces it by AST.
# ---------------------------------------------------------------------------
CARDS_PATH = REPO_ROOT / "data" / "cards.json"
# The per-level skill rungs the solver scores from (`restate_card_skills`). Read
# here too so the collection editor can show each skill at the level a card's
# bloom bought — the game's own value, not the catalogue's maxed one.
LADDER_PATH = REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "skill_ladder.json"
BOARD_PATH = DEFAULT_BOARD_PATH
BOARD_DISABLED = False
OWNED_PATH = REPO_ROOT / "data" / "owned.json"

# The ONE path anything under `tools/web/` may write, and it is a module
# constant so `test_no_writes.py`'s check 4 can pin every mutator call in this
# package to exactly this name. `state/` is deliberately NOT under the write
# prohibition -- that names exactly `data/cards.json` and `data/owned.json`
# -- but "not prohibited" is not "unchecked", so
# the guard is extended rather than relaxed.
COLLECTION_PATH = REPO_ROOT / "state" / "collection.json"
# Read-only inputs the editor needs to bound its controls: the level caps per
# rarity and the per-level multiplier curve. Both gitignored under `dumps/`.
CURVE_PATH = REPO_ROOT / COLLECTION_CURVE_PATH
CAPS_PATH = REPO_ROOT / COLLECTION_CAPS_PATH
# The board editor's read-only inputs, both gitignored under `dumps/`:
# the node table + the four model geometries, and the master rows the point
# costs (field 9) come from. Absent files degrade to an empty editor that says
# so, never to an error -- the same first-class-absence contract the board and
# the collection already have.
SKILLTREE_PATH = REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "skilltree.json"
NODE_MASTER_PATH = (
    REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "master_SkillTreeNode.json"
)
PROXY_DIR = REPO_ROOT / "tools" / "recon" / "proxy"
# The solve's account BASIS names map to the optimiser's SCENARIO names (same
# mapping the page uses in app.js SCENARIO_BY_BASIS). This is how /api/boardplan
# knows which collection state to plan the Solve's team over.
_SCENARIO_BY_BASIS = {
    "account": "account",
    "maxed": "full-collection-maxed",
    # The maxed-ACCOUNT basis (below): the same catalogue-wide, level-2 Connect
    # scenario for the subprocess planners (precompute.SCENARIO_ALIASES); what
    # differs is the bench and the account bonuses, which live in-process.
    "maxed-account": "full-collection-maxed",
}
# The three card bases a request may name. Anything else is the real account:
# an unrecognised value must not silently become a hypothetical one.
MAXED_ACCOUNT_BASIS = "maxed-account"
BASES = ("account", "maxed", MAXED_ACCOUNT_BASIS)


def _parse_basis(raw: object) -> str:
    """`account` | `maxed` | `maxed-account`; unknown -> `account`."""
    basis = str(raw or "account").lower()
    return basis if basis in BASES else "account"


def _cards_maxed(basis: str) -> bool:
    """Whether the basis scores the MAXED catalogue (every card, level/bloom
    max) — true for `maxed` and `maxed-account`; the two differ only in the
    account around the cards (bench boards + Memory / Power-Up bonuses)."""
    return basis in ("maxed", MAXED_ACCOUNT_BASIS)
# A music id is `m` + digits in every chart on disk; this allowlist is also the
# path-traversal guard for the `song` query param (no dots, no slashes).
_MUSIC_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# The chart library the per-song modes score against, and the song TITLES.
#
# Two files, deliberately, because they are two different kinds of thing. The
# charts are the game's own note maps, rebuilt by `build_charts.py`, and they
# are what a score is computed from. The titles are HUMAN LABELS lifted from
# a language export -- nothing is scored from them, and a missing title
# degrades to the music id rather than hiding the song. Keeping the label source
# separate from the scoring source means a display file can never quietly change
# a number.
CHARTS_PATH = REPO_ROOT / DEFAULT_CHARTS_PATH
# Every difficulty (`build_charts.py --all-out`): EASY / NORMAL / HARD charts
# live only here. EXPERT is read from charts.json.
CHARTS_ALL_PATH = CHARTS_PATH.with_name("charts_all.json")


def _difficulties_listing() -> dict[str, Any]:
    """`/api/meta`'s `difficulties`: `{"default": "EXPERT", "available": [...],
    "charts": {difficulty: count}, "missing": {difficulty: [music ids]}}` —
    `missing` lists the songs that have an EXPERT chart but none at that
    difficulty, and is present only when some difficulty lacks a song."""
    present = cert_mod.available_difficulties(CHARTS_PATH, CHARTS_ALL_PATH)
    out: dict[str, Any] = {
        "default": "EXPERT",
        "available": [d for d in cert_mod.DIFFICULTIES if d in present],
        "charts": {d: len(present[d]) for d in cert_mod.DIFFICULTIES if d in present},
    }
    expert = set(present.get("EXPERT") or ())
    missing = {d: sorted(expert - set(ids)) for d, ids in present.items()
               if d != "EXPERT" and expert - set(ids)}
    if missing:
        out["missing"] = missing
    return out


def _charts_path(difficulty: str = "EXPERT") -> Path:
    """The chart file a run at `difficulty` reads (`certificate.charts_path_for`,
    against this module's paths so a test that moves them moves both)."""
    return cert_mod.charts_path_for(difficulty, charts_path=CHARTS_PATH,
                                    charts_all_path=CHARTS_ALL_PATH)
SONG_TITLES_PATH = (
    REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "holodori"
    / "main_LangMusic_Eng.json"
)
# The songs' own master rows -- singer type, real cast, cover/original,
# playing time. DISPLAY/FILTER metadata only, same discipline as the titles
# above: nothing is scored from this file, so its absence costs the picker
# its filters and never a number. `filters_available` is how the page learns
# which of the two states it is in, rather than being shown empty menus.
MUSIC_PATH = (
    REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "holodori"
    / "main_Music.json"
)
# The node glyphs, pulled by `skill_icons.py`. NOT under `ui_sprites/`.
ICONS_DIR = REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "skill_icons"
# The full card art (`img_card_full_<key>.webp`, 512x256 after `card_images.py
# --render --max-width 512`), keyed by card id minus the `card-` prefix. Only the
# ~60 cards the game has cached have art; a miss is a 404 and the UI falls back to
# the card name — a first-class absence, same contract as `_serve_art`.
CARD_IMAGES_DIR = REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "card_images"
# The card's own chrome, extracted by `card_images.py`:
# element badge, generation ribbon, rarity band, star, bloom flower. The explorer
# inlines these as data URIs; a served page does better to fetch them once and let
# the browser cache them, so they are served from the dump in place.
CHROME_DIR = REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "card_chrome"
# The game's official UI sprites: nav glyphs, grade letters and the RankIcon "+",
# skill plates, panels, board glyphs, item thumbs. The page shell and components
# draw these instead of hand-made icons. Served in place, like `card_chrome/`.
UI_OFFICIAL_DIR = REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "ui_official"
# Per-character round face icons: the game's own `img_chr_icon_mini` (512px webp in
# card_images, once rendered) preferred, else the 256px `member_icons` PNG. Served
# so the socket list can show WHOSE board each pick sits on, at a glance.
MEMBER_ICONS_DIR = REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "member_icons"
# The game's own ITEM icons (beads/crystals/hololium/hologold/lesson-pt/board
# cubes), extracted by the octo-fetch pipeline into `img_item_thumb_*` webps.
# Served at /itemicon/<item_id> for the Resources view; a miss is a 404 and the
# view falls back to a coloured glyph.
ITEM_ICONS_DIR = REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "item_icons"
# Song jacket images, rendered by song_jackets.py. Served at /jacket/<music_id>
# so the picker shows cover art instead of a bare id; absent = 404 and the picker
# falls back to the title alone (same first-class-absence contract as card art).
JACKETS_DIR = REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "song_jackets"

def _bench_characters():
    """`board.json`'s name-keyed `characters` mapping (the account's CURRENT
    boards, connect multipliers resolved) for the develop re-score's bench
    merge, or `None` when the capture is absent — the develop then scores the
    six team boards alone."""
    doc = _read_json(REPO_ROOT / BOARD_PATH) or {}
    return doc.get("characters") or None


def _bench_sockets():
    """What the account's off-team sockets hold (`ondemand.bench_sockets_of` over
    `board_state.json`), so a develop route keeps the team from also socketing a card
    a bench board keeps lifting (`board_plan.held_bench_cards`); None without it."""
    import ondemand  # noqa: PLC0415
    state = _read_json(PROXY_DIR / "dumps" / "board_state.json")
    return ondemand.bench_sockets_of(state) if state else None


# ---------------------------------------------------------------------------
# The MAXED-ACCOUNT basis: "best team on a FULLY MAXED account", on the SAME
# basis the community tool holodori.best answers it, so the numbers are
# comparable. The `maxed` basis maxes the CARDS but keeps the account's captured
# bench boards and account counters (2.76 M vs 3.42 M average song score on the
# same kind of roster). This basis is `maxed` + the reference scenario
# (`precompute_optimisations.py`, bottom):
#   * account bonuses = `maxed_account_bonuses`: Memory posters = the MASTER's
#     poster count, Member Power-Up = the roster ceiling
#     (derived from the roster) — the same two rates holodori.best uses;
#   * bench = `maxed_account_bench`: EVERY holomem's board at the full 453-pt
#     tree, its Connect sockets filled at level 2 from the whole catalogue by
#     the domain's exact assignment, valued at a FIXED nominal team — so the
#     bench is a function of the inputs only, never of the team being scored;
#   * connect pool = `connect_cards_for("maxed-account")` (every card, level 2);
#   * the developed board budget defaults to the flat 453 (the full tree).
# The bench takes a few seconds to build (54 boards, one exact matching), so
# it is cached per process, keyed by the resolved config it was linearised
# at (`cert_mod.cfg_json`) — the same key the certificate hashes.
# ---------------------------------------------------------------------------
# The nominal team the bench sockets are valued at: holodori.best v3.1.1's
# recommended roster (the first entry of tools/web/seed_rosters.json, source
# "holodori-v3.1.1"), FIXED here rather than read from that hand-edited file so
# the bench cannot change under a seed edit. Noel Soleil Kiss (leader + member),
# Matsuri VCS, Flare EGG, Fubuki KR, Hajime BSG.
MAXED_ACCOUNT_NOMINAL_TEAM = (
    "card-00022-5-uniq-0063-00",
    ("card-00007-5-uniq-0008-00", "card-00022-5-uniq-0063-00",
     "card-00021-5-uniq-0017-00", "card-00006-5-uniq-0007-00",
     "card-06005-5-uniq-0061-00"),
)
MAXED_ACCOUNT_BUDGET = 453          # the full tree, every team character
MAXED_ACCOUNT_LABEL = ("maxed account · 54 bench boards @453 · memories 37 · "
                       "power-up 11.58 %")


@dataclass(frozen=True)
class MaxedAccountScenario:
    """Everything the maxed-account basis adds on top of the maxed cards."""
    bonuses: Any                       # AccountBonuses (posters, power_up: the maxed account's)
    bench: dict                        # name-keyed `characters` mapping (board.json shape)
    connect_cards: dict | None         # `connect_cards_for("maxed-account")`
    board_model: Any                   # BoardModel of the bench (the "current" board)
    literal: str                       # the scenario literal the collection line hashes
    bench_sockets: int                 # sockets the exact assignment filled (54 expected)
    report: tuple[str, ...]            # build_board's report + problems (diagnostics)

    def lines(self) -> list[str]:
        b = self.bonuses
        posters = getattr(b, "posters", None)
        pu = getattr(b, "power_up_permyriad", None)
        mem = (", +%.1f %%" % (memory_bonus_permil(posters) / 10)) if posters is not None else ""
        pup = (" = %.2f %%" % (pu / 100)) if pu is not None else ""
        return [
            "BASIS: MAXED ACCOUNT (holodori-comparable) -- every card owned, at "
            "max level and max bloom, on a fully maxed ACCOUNT: not yours",
            f"BASIS: bench = every one of the {len(self.bench)} holomem boards fully "
            f"developed at {MAXED_ACCOUNT_BUDGET} points, {self.bench_sockets} Connect "
            "sockets filled at level 2 (exact assignment, valued at a fixed nominal team)",
            f"BASIS: Memory Bonus at {posters} posters (the master's whole poster table"
            f"{mem}); Member Power-Up at the roster ceiling ({pu} permyriad{pup})",
            f"BASIS: team boards developed at the flat {MAXED_ACCOUNT_BUDGET}-point budget "
            "(the full tree); Connect pool = every card in the game at level 2",
        ]

    def current_by_chr(self, name_by_chr: Mapping[str, str]) -> dict:
        """The `current_by_chr` baseline a developed row's `board_plan_summary`
        diffs against, for THIS basis: every bench node owned, 453 granted —
        so a develop on a maxed account reads as a respec of a full board,
        not as buying the account's missing nodes."""
        out = {}
        for chr_id, name in name_by_chr.items():
            rows = self.bench.get(name)
            if rows is None:
                continue
            out[chr_id] = {"name": name,
                           "owned": sorted(str(r.get("node")) for r in rows if r.get("node")),
                           "granted": MAXED_ACCOUNT_BUDGET}
        return out

    def summary(self) -> dict:
        b = self.bonuses
        return {"basis": MAXED_ACCOUNT_BASIS, "label": MAXED_ACCOUNT_LABEL,
                "bench_boards": len(self.bench), "bench_budget": MAXED_ACCOUNT_BUDGET,
                "bench_sockets": self.bench_sockets,
                "posters": getattr(b, "posters", None),
                "power_up_permyriad": getattr(b, "power_up_permyriad", None),
                "connect_cards": len(self.connect_cards or {}),
                "nominal_team": [MAXED_ACCOUNT_NOMINAL_TEAM[0], *MAXED_ACCOUNT_NOMINAL_TEAM[1]],
                "scenario": self.literal}


_MAXED_ACCOUNT_CACHE: dict[str, MaxedAccountScenario] = {}
_MAXED_ACCOUNT_LOCK = threading.Lock()


class MaxedAccountUnavailable(RuntimeError):
    """The scenario cannot be built (a dump is missing, the recon module will
    not import): a first-class, typed absence the routes turn into a 503."""


def _maxed_account_scenario(cards, cfg, collection_basis, *,
                            precompute=None) -> MaxedAccountScenario:
    """The cached scenario for `cards` (the maxed catalogue) at `cfg` (its
    account bonuses REPLACED by the maxed-account ones before the bench is
    linearised, so the cache key is the config the bench is a function of).
    Raises `MaxedAccountUnavailable` with the reason; never guesses."""
    precompute = precompute or _load_precompute()
    if precompute is None or not hasattr(precompute, "maxed_account_bench"):
        raise MaxedAccountUnavailable(
            "could not import precompute_optimisations.py (maxed_account_bench) — "
            "the maxed-account basis needs the proxyenv interpreter this server "
            "runs under and the gitignored dumps/ (skilltree.json, "
            "master_SkillTreeNode.json, holodori/main_Poster.json).")
    try:
        bonuses = precompute.maxed_account_bonuses(collection_basis)
    except Exception as exc:  # noqa: BLE001 — reported, typed
        raise MaxedAccountUnavailable(f"maxed_account_bonuses: {type(exc).__name__}: {exc}")
    if bonuses is None or precompute.master_poster_count() is None:
        raise MaxedAccountUnavailable(
            "the maxed-account basis needs the master Poster table "
            "(dumps/holodori/main_Poster.json) for its Memory Bonus and the level "
            "curve for the Power-Up ceiling; one of them is absent.")
    cfg_scn = dataclasses.replace(cfg, account=bonuses, board=None)
    key = cert_mod.cfg_json(cfg_scn)
    with _MAXED_ACCOUNT_LOCK:
        hit = _MAXED_ACCOUNT_CACHE.get(key)
        if hit is not None:
            return hit
        skilltree = _read_json(SKILLTREE_PATH)
        node_master = _read_json(NODE_MASTER_PATH)
        if not skilltree or not node_master:
            raise MaxedAccountUnavailable(
                "the maxed-account bench needs skilltree.json + "
                "master_SkillTreeNode.json under the gitignored dumps/; run "
                "tools/recon/proxy/skilltree.py")
        by_id = {c.id: c for c in cards}
        leader, members = MAXED_ACCOUNT_NOMINAL_TEAM
        missing = [c for c in (leader, *members) if c not in by_id]
        if missing:
            raise MaxedAccountUnavailable(
                f"the bench's nominal team names card(s) not in the catalogue: {missing}")
        report: list[str] = []
        try:
            connect_cards = precompute.connect_cards_for(precompute.MAXED_ACCOUNT_SCENARIO)
            bench = precompute.maxed_account_bench(
                cards, cfg_scn, (leader, tuple(members)), skilltree=skilltree,
                node_master=node_master, connect_cards=connect_cards, report=report)
            board_model = board_from_mapping(
                {"characters": {k: list(v) for k, v in bench.items()}},
                source="<maxed-account bench>")
        except Exception as exc:  # noqa: BLE001 — reported, typed
            raise MaxedAccountUnavailable(
                f"maxed_account_bench: {type(exc).__name__}: {exc}")
        sockets = sum(1 for line in report if str(line).strip().startswith("S-00"))
        literal = (f"maxed-account:level=max,bloom=max,posters={bonuses.posters},"
                   f"power_up_permyriad={bonuses.power_up_permyriad},bench=full453+L2sockets")
        scn = MaxedAccountScenario(
            bonuses=bonuses, bench=bench, connect_cards=connect_cards,
            board_model=board_model, literal=literal, bench_sockets=sockets,
            report=tuple(str(x) for x in report))
        _MAXED_ACCOUNT_CACHE[key] = scn
        return scn


def _apply_basis(cards, owned, cfg, basis: str, *, assume_level=None, assume_bloom=None,
                 superseded_by_real_stats: bool = False, precompute=None):
    """The ONE place a request's card basis becomes (cards, collection_basis,
    cfg, scenario): `apply_collection` exactly as the CLI applies it, then —
    on the maxed-account basis only — the account bonuses swapped for the
    scenario's and the scenario built (cached). `scenario` is None on the
    other two bases. `cfg.board` is NOT touched here (each route decides
    which board it scores); the scenario's `board_model` is the maxed-account
    "current" board for the routes that score one."""
    maxed = _cards_maxed(basis)
    cards, collection_basis = apply_collection(
        cards,
        None if maxed else str(COLLECTION_PATH),
        superseded_by_real_stats=superseded_by_real_stats,
        owned_ids=[c.id for c in owned],
        maxed=maxed,
        assume_level=assume_level,
        assume_bloom=assume_bloom,
    )
    if collection_basis.bonuses is not None:
        cfg = dataclasses.replace(cfg, account=collection_basis.bonuses)
    scenario = None
    if basis == MAXED_ACCOUNT_BASIS:
        scenario = _maxed_account_scenario(cards, cfg, collection_basis, precompute=precompute)
        cfg = dataclasses.replace(cfg, account=scenario.bonuses)
    return cards, collection_basis, cfg, scenario


_ITEM_ICON_MAP: dict = {"mtime": None, "map": {}}


def _item_icon_map() -> dict[str, str]:
    """`item_id -> icon asset name` from `item_icons.py`'s index.json (`by_item`,
    master `Item` field 8). Reloaded when the file changes; `{}` when absent — the
    filename conventions in `_serve_itemicon` still apply."""
    path = ITEM_ICONS_DIR / "index.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _ITEM_ICON_MAP["mtime"] != mtime:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            _ITEM_ICON_MAP["map"] = dict(raw.get("by_item") or {})
            _ITEM_ICON_MAP["mtime"] = mtime
        except (OSError, ValueError):
            return _ITEM_ICON_MAP["map"]
    return _ITEM_ICON_MAP["map"]


# In-memory cache of downscaled art. The masters on disk stay at NATIVE resolution
# (lossless), which is the right archive; the page asks for the exact size it shows
# via `?w=`, so it never downloads a 2 MB texture to paint a 56 px thumbnail. Keyed
# by (path, mtime, width) so a re-render invalidates it.
_ART_SCALE_CACHE: dict = {}


def _scaled_art(path, width: int) -> bytes | None:
    """The bytes of `path`, downscaled to `width` px wide (aspect kept) and cached.
    `width <= 0`, an unreadable master, or a master already narrower returns the
    master untouched — a resize is never an upscale and never a failure path."""
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, width)
    cached = _ART_SCALE_CACHE.get(key)
    if cached is not None:
        return cached
    raw = path.read_bytes()
    body = raw
    if width > 0:
        try:
            import io  # noqa: PLC0415
            from PIL import Image  # noqa: PLC0415 — proxyenv ships Pillow
            with Image.open(io.BytesIO(raw)) as im:
                if im.width > width:
                    height = max(1, round(im.height * width / im.width))
                    im = im.convert("RGBA").resize((width, height), Image.LANCZOS)
                    buf = io.BytesIO()
                    im.save(buf, format="WEBP", quality=90, method=4)
                    body = buf.getvalue()
        except Exception:  # noqa: BLE001 — any failure serves the master
            body = raw
    if len(_ART_SCALE_CACHE) > 3000:
        _ART_SCALE_CACHE.clear()
    _ART_SCALE_CACHE[key] = body
    return body
# The game's own typeface. The build ships no `Font` object at all (TextMeshPro
# bakes an SDF atlas), so the face was identified as LINE Seed Sans and is
# fetched from LINE rather than extracted. The card-face CSS names it as its
# FIRST choice; unserved, every glyph on the card would draw in a Century
# Gothic/Trebuchet fallback.
# Latin subset, ExtraBold only, which is the one weight all three uses ask for
# (`.gen b`, `.lvl b|span`, `.bloom em` are each already font-weight:800).
FONT_PATH = REPO_ROOT / "tools" / "explorer" / "fonts" / "line-seed-sans-xbd-latin.woff2"
# `CharacterGrouping` fields 100/101 per generation, extracted by `gen_groups.py`.
# The ribbon is a horizontal gradient between them (`build_explorer.py`, read off a
# game screenshot) -- 100 at the left edge, 101 at the right, and the
# polarity is the field order rather than light-to-dark: on Myth 100 is the PALE one.
GEN_GROUPS_PATH = REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "gen_groups.json"
# `{chr-id: unspent skill-tree points}`, from `tree_budget.unspent_from_capture`
# over a `User/Get` capture. This is what makes the editor's budget REAL rather
# than the 453 of a full board.
TREE_POINTS_PATH = (
    REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "tree_points.json"
)
# The Park view's two halves. `park_spots.json` + four raw PNGs are the
# OFFLINE export (`park_export.py`) -- the map and where every named spot lands
# on it, the parts that never change. The day's picked symbols are a LIVE read
# (`park_treasure.py`), joined on top in `_serve_park`. Both are gitignored
# under dumps/; an absent export is first-class STATE with the command that
# builds it, never a 500.
PARK_EXPORT_DIR = REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "park_export"
PARK_SPOTS_PATH = PARK_EXPORT_DIR / "park_spots.json"
PARK_PROXY_DIR = REPO_ROOT / "tools" / "recon" / "proxy"
# The credential the live park read SPENDS. Checked here so an absent one is a
# named state rather than the `sys.exit()` that `account_snapshot.load_credential`
# raises on a missing file -- SystemExit would otherwise escape as a 500.
ACCOUNT_CREDENTIAL_PATH = REPO_ROOT / "state" / "account_credential.json"

# --- Gacha tracker. The store is data/gacha/*.jsonl, WRITTEN ONLY by
# tools/recon/proxy/gacha_sync.py (append-only) -- this module reads it and serves the
# banner art copied there. `/api/gacha/sync` shells out, same reasoning as `_refresh`.
GACHA_STORE_DIR = REPO_ROOT / "data" / "gacha"
GACHA_ART_DIR = GACHA_STORE_DIR / "art"
GACHA_SYNC_SCRIPT = PARK_PROXY_DIR / "gacha_sync.py"
# The export is written by the RECON side, never here: this package writes no files
# (`test_no_writes.py`). The route shells out and reads back the `WROTE <path>` line.
GACHA_EXPORT_SCRIPT = PARK_PROXY_DIR / "gacha_export.py"
_GACHA_ID_RE = re.compile(r"^gacha-[0-9a-z_-]+$")
# `<kind>` (extension-less, what the page asks for), `<kind>.png` (the committed masters)
# and `<kind>.webp` (what gacha_art saves). The route serves whichever
# file is actually there, so the page never has to know which format a banner was saved in.
_GACHA_ART_KINDS = ("banner", "logo", "icon")
_GACHA_ART_EXTS = ("png", "webp")
# The sync SPENDS the account credential (a login). Two in flight would double-log-in and
# race two appenders on the same append-only store, so the second is refused, not queued:
# the user pressed a button and deserves an answer, not a silent wait behind a 60 s login.
_GACHA_SYNC_LOCK = threading.Lock()
# One account refresh at a time. A page reloaded mid-refresh with the button pressed again
# would start a SECOND refresh_all.py beside the first: two logins, two writers on the same
# dumps (a transient Errno 22 on dumps/inventory.json). The second is refused (409), like
# the gacha sync.
_REFRESH_LOCK = threading.Lock()

# --- Event ranking cutoffs. The store is
# dumps/cutoffs/<region>/<chapter>.jsonl, WRITTEN ONLY by event_cutoffs.py (append-only);
# `cutoffs_view` reads it and predicts. `/api/event/cutoffs/sync` shells out, like the gacha
# sync, and is refused (409) while another is in flight -- it logs in up to three accounts.
CUTOFFS_STORE_DIR = PARK_PROXY_DIR / "dumps" / "cutoffs"
CUTOFFS_SYNC_SCRIPT = PARK_PROXY_DIR / "event_cutoffs.py"
_CUTOFFS_GRADE_PATH = PARK_PROXY_DIR / "dumps" / "holodori" / "main_MarathonRankingGrade.json"
_CUTOFFS_GRADE_LANG_PATH = (PARK_PROXY_DIR / "dumps" / "holodori"
                            / "main_LangMarathonRankingGrade_Eng.json")
_CUTOFFS_SYNC_LOCK = threading.Lock()
# The local poller (ON at start-up, every 5 min; guests only; shares the sync
# lock so one read runs at a time). `tools/web/cutoffs_poller.py` has the policy;
# HBUILDER_CUTOFFS_POLL_MIN=<min> sets the interval and =0 turns it off; the Event page's
# toggle switches it at runtime.
import cutoffs_poller  # noqa: E402

# With the 24/7 tracker on the VPS configured (state/tracker_remote.json,
# tools/recon/proxy/event_tracker.py), the poller PULLS its reads over SSH instead of logging
# the same guest accounts in from here -- two readers on one guest would keep ending each
# other's session. The manual sync button still runs event_cutoffs.py (it reads the account's
# own rank).
CUTOFFS_PULL_SCRIPT = PARK_PROXY_DIR / "tracker_pull.py"
CUTOFFS_REMOTE_CONFIG = REPO_ROOT / "state" / "tracker_remote.json"
# ...or over HTTPS through api.holobuilder.dev/tracker/* (state/server_api.json): the desktop
# app's route, on a PC without the VPS's SSH key.
CUTOFFS_API_CONFIG = REPO_ROOT / "state" / "server_api.json"
def _tracker_configured() -> bool:
    return CUTOFFS_REMOTE_CONFIG.is_file() or CUTOFFS_API_CONFIG.is_file()


_CUTOFFS_POLL_SCRIPT = CUTOFFS_PULL_SCRIPT if _tracker_configured() else CUTOFFS_SYNC_SCRIPT
_CUTOFFS_POLLER = cutoffs_poller.CutoffsPoller(
    cutoffs_poller.ScriptRunner(_CUTOFFS_POLL_SCRIPT, REPO_ROOT), _CUTOFFS_SYNC_LOCK)

# The top 100 is read ON DEMAND, only while someone is looking at it: each
# GET /api/event/top renews a viewer's lease on the VPS tracker for that region (at most once
# every TOP_WATCH_EVERY_S per region) and pulls what it read, in the background; the page asks
# again about every minute while it is open, so it sees the board the lease made.
TOP_WATCH_EVERY_S = 50
_TOP_WATCH_AT: dict[str, float] = {}
_TOP_WATCH_MU = threading.Lock()


def _kick_top_watch(region: str, kind: str | None = None) -> bool:
    """Start one background `tracker_pull.py --watch <region>`; False when not configured or
    one ran for this region less than TOP_WATCH_EVERY_S ago."""
    import subprocess  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    if not _tracker_configured():
        return False
    now = _time.time()
    with _TOP_WATCH_MU:
        key = f"{region}|{kind or ''}"   # a song's board holds its own lease
        if now - _TOP_WATCH_AT.get(key, 0.0) < TOP_WATCH_EVERY_S:
            return False
        _TOP_WATCH_AT[key] = now

    def run() -> None:
        # the pull appends to the store: never alongside the poller's pull or a manual sync
        if not _CUTOFFS_SYNC_LOCK.acquire(timeout=90):
            return
        try:
            subprocess.run([sys.executable, "-u", str(CUTOFFS_PULL_SCRIPT), "--watch", region,
                            *(["--kind", kind] if kind else [])],
                           cwd=str(REPO_ROOT), capture_output=True, timeout=120,
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"  [top-watch] {region}: {type(exc).__name__}: {exc}", flush=True)
        finally:
            _CUTOFFS_SYNC_LOCK.release()

    threading.Thread(target=run, name=f"top-watch-{region}", daemon=True).start()
    return True


def _parse_sync_summary(stdout: str) -> dict | None:
    last = None
    for line in stdout.splitlines():
        if line.startswith("SUMMARY "):
            try:
                last = json.loads(line[len("SUMMARY "):])
            except ValueError:
                pass
    return last


def _parse_wrote(stdout: str) -> str | None:
    """The last `WROTE <path>` line's remainder -- the export's own report of where it
    landed. Last, not first, for the same reason as SUMMARY: a rerun inside one
    subprocess would otherwise hand back a stale path."""
    last = None
    for line in stdout.splitlines():
        if line.startswith("WROTE "):
            last = line[len("WROTE "):].strip() or None
    return last


STATIC_DIR = Path(__file__).resolve().parent / "static"
_PAGE_RE = re.compile(r"/[a-z][a-z-]{0,40}")   # a redesign page: /<name> -> static/<name>.html
# The portraits are 141 webp at 512x1024 under `tools/explorer/`, they
# are gitignored, and they are NOT copied here -- they are served from where
# they already live. A fresh worktree has none; `--art-dir` is the one-line
# change that repoints at a checkout that does, and an absent directory
# degrades to the placeholder gradient rather than an error.
DEFAULT_ART_DIR = REPO_ROOT / "tools" / "explorer" / "cards_explorer_art"

BIND_HOST = "127.0.0.1"  # loopback: the page is served to this machine only.
DEFAULT_PORT = 8731

_ART_NAME_RE = re.compile(r"^card-[0-9A-Za-z_-]+\.webp$")
# Node glyphs. The names come from `skilltree.json`'s own `e.icon`.
#
# ⚠ The HYPHEN is required: three Y-branch assets are suffixed by song scope
# (`…musicskilltreecharacterandmusicsingertype-solo` / `-group` / `-all`) and a
# `[a-z0-9_]+` pattern would 400 every one of them — **18 nodes**, the whole
# score-bonus family, silently glyph-less. Still no `/`, no `.` except the
# literal extension, so no traversal.
_ICON_NAME_RE = re.compile(r"^[a-z0-9_-]+\.png$")
# `<subdir>/<file>.png`, both from the same restricted alphabet. One level only:
# `card_chrome/` is exactly two deep and nothing legitimate needs to go further.
_CHROME_NAME_RE = re.compile(r"^[a-z0-9_]+/[a-z0-9_]+\.png$")
# `GET /ui/<category>/<sprite>.(png|webp)`: one level, the category is a folder of
# `ui_official/` (letters and `_`), the file the game's own sprite name (item thumbs
# carry hyphens). No `.` but the extension, so no traversal; containment re-checked.
# `views/` holds node-tree text dumps, not images, and the extension rule keeps it out.
_UI_NAME_RE = re.compile(r"^[a-z_]+/[a-z0-9_-]+\.(png|webp)$")
# `GET /park/<area>.png` -- the area id is lowercase letters, digits and
# hyphens only (`area-a01`, `area-a02-casino`). No `.` but the literal
# extension, no `/`, no `_`, so there is no traversal surface; the served path
# is re-checked for containment under `PARK_EXPORT_DIR` after resolving anyway.
# `chest.png` is the game's own chest map icon, exported beside the maps.
_PARK_IMAGE_NAME_RE = re.compile(r"^(area-[a-z0-9-]+|chest)\.png$")
# The leaders' Live2D figures (tools/recon/proxy/live2d_build.py writes them; gitignored):
# models/<asset>/..., motions/..., expressions/... and index.json. The Cubism Core runtime
# is Live2D's proprietary file, kept OUT of git and never shipped by this project: each PC
# downloads it once from Live2D's own CDN (`_live2d_core`) and serves it to its local page.
LIVE2D_DIR = REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "live2d"
LIVE2D_CORE = REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "live2d_runtime" / "live2dcubismcore.min.js"
LIVE2D_CORE_URL = "https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js"


def _live2d_core() -> bool:
    """True once the Cubism Core is on this PC: the app's file cache downloads it the first time
    from Live2D's own CDN, so the holobuilder server never redistributes it
    (`content_store.fetch_live2d_core`; this module writes nothing but the collection)."""
    return _content_store.fetch_live2d_core(LIVE2D_CORE, LIVE2D_CORE_URL)


_LIVE2D_PATH_RE = re.compile(
    r"^(models/[0-9]{5}-[a-z]+-[0-9]{4}-[0-9]{2}/[a-z0-9_]+\.(json|moc3|webp|png)"
    r"|motions/live2d_mot_[a-z0-9_-]+\.motion3\.json"
    r"|expressions/live2d_exp_[a-z0-9_-]+\.exp3\.json)$")
_LIVE2D_TYPES = {"json": "application/json", "moc3": "application/octet-stream",
                 "webp": "image/webp", "png": "image/png"}
# The holomems' 3D chibis for the Optimizer's progress runner (tools/recon/proxy/chibi_build.py
# writes them; gitignored): models/<SdCostume id>.glb and index.json
CHIBI_DIR = REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "chibi"
_CHIBI_PATH_RE = re.compile(r"^models/sdcos-[0-9]{5}-[a-z]+-[0-9]{4}-[0-9]{2}\.glb$")

# The installed app keeps the Live2D figures, chibis and card art on api.holobuilder.dev and
# fetches each the first time a page asks (`tools/app/content_store.py`).
# On a PC with every file local, `lazy()` returns it and no
# request leaves the machine. The store writes its cache itself: this module still opens
# nothing for writing (`test_no_writes.py`).
sys.path.insert(0, str(REPO_ROOT / "tools" / "app"))
import content_store as _content_store  # noqa: E402

_CONTENT = _content_store.ContentStore()


def _asset(base: Path, path: Path) -> Path | bytes | None:
    """`path`, which must stay under `base`: the local file, else the server's copy (a cached
    Path, or the bytes when the cache is off), else None. A path escaping `base` is None."""
    try:
        if base.resolve() not in path.resolve().parents:
            return None
    except OSError:
        return None
    if path.is_file():
        return path
    try:
        rel = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return None                       # outside this checkout (another --art-dir): local only
    return _CONTENT.lazy(rel)


def _asset_known(base: Path, path: Path) -> bool:
    """Whether a page may ask for `path`: on disk, or on the server. Never downloads."""
    if path.is_file():
        return True
    try:
        return _CONTENT.entry(path.relative_to(REPO_ROOT).as_posix()) is not None
    except ValueError:
        return False


def _asset_bytes(base: Path, path: Path) -> bytes | None:
    got = _asset(base, path)
    return got.read_bytes() if isinstance(got, Path) else got


# `data/cards.json` carries generation names MIXED: `Advent` and `ReGLOSS` in
# English beside `0期生` and `ゲーマーズ` in Japanese. `gen_groups.json`, extracted
# from `CharacterGrouping`, is uniformly English -- and it is the GAME'S own
# English, not a translation. The two sets are the same 15 groups, so
# this is a bijection and not a guess:
_GEN_ALIAS = {
    "0期生": "Gen 0", "1期生": "Gen 1", "2期生": "Gen 2",
    "3期生": "Gen 3", "4期生": "Gen 4", "5期生": "Gen 5",
    "ゲーマーズ": "GAMERS",
    "ID1": "ID Gen 1", "ID2": "ID Gen 2", "ID3": "ID Gen 3",
    # Advent · Myth · Promise · ReGLOSS · holoX already match by name.
}


# The picker's filter tokens, from the Music master's own enums. Keyed by the
# suffix after the last underscore, because the wire spells the whole thing out
# (`MusicSingerType_MUSIC_SINGER_TYPE_SOLO`).
#
# ⚠ The wire vocabulary is SOLO / GROUP / ALL (every row of `main_Music.json`)
# -- while the page contract's tokens are
# solo / unit / whole. GROUP->unit and ALL->whole is that bijection, spelled
# out here rather than derived, so a NEW enum value degrades to "" (an honest
# "unknown") instead of leaking a raw enum suffix into a filter menu.
_SINGER_TYPES = {"SOLO": "solo", "GROUP": "unit", "ALL": "whole"}
_CATEGORIES = {"ORIGINAL": "original", "COVER": "cover"}


@functools.lru_cache(maxsize=1)
def _gen_labels() -> dict[str, str]:
    """`{name as `cards.json` spells it: the game's English label}`.

    DISPLAY ONLY. `card.generations` keeps its own spelling everywhere else,
    because a gate reads `generation:Myth >= 2` against that value -- relabelling
    the data instead of the label would silently change what a gate matches.
    """
    return dict(_GEN_ALIAS)


@functools.lru_cache(maxsize=1)
def _gen_palette() -> dict[str, dict[str, str]]:
    """`{generation name: {"c": <field 100>, "c2": <field 101>}}`.

    Keyed by the group's display NAME because that is what `_card_view` ships in
    `generations` -- the client never sees a `grp-` id. Absent file, empty map, and
    the ribbon falls back to the card's type tint, which is what the explorer does.
    """
    try:
        raw = json.loads(GEN_GROUPS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, dict[str, str]] = {}
    for group in (raw.get("groups") or {}).values():
        name = group.get("name")
        colour = group.get("color")
        if name and colour:
            out[str(name)] = {"c": str(colour), "c2": str(group.get("color2") or colour)}
    # Also key by the spelling `cards.json` uses, or every Japanese-named
    # generation misses the lookup and silently falls back to the type tint.
    for raw_name, english in _GEN_ALIAS.items():
        if english in out:
            out[raw_name] = out[english]
    return out

# `…_level_up` is a hollow FRAME with "Lv" on its base, not a finished icon —
# the granted skill's own glyph belongs inside it, and left empty the node
# reads as a blank box. Copied from `tools/explorer/build_explorer.py`'s
# `TREE_FRAME_ICONS`, which established this; duplicated rather than imported
# because that file is a standalone generator, not a library.
#
# From the asset: the frame is 128x128 and its enclosed hole spans
# 0.117..0.883 on both axes (76.6 % of the side). The explorer fits the inner
# glyph to 86 % of that hole, i.e. 0.659 of the frame — the fraction
# `board.js` uses.
TREE_FRAME_ICONS = frozenset({"img_skill_icon_tree_livedeckleaderactiveskilllevelup"})


class _Cancelled(Exception):
    """Raised inside the solver's own progress callback to unwind a run."""


@dataclass
class Job:
    job_id: str
    events: "queue.Queue[dict[str, Any]]" = field(default_factory=queue.Queue)
    cancel: threading.Event = field(default_factory=threading.Event)
    started_at: float = field(default_factory=time.monotonic)
    done: bool = False


_JOBS: dict[str, Job] = {}
_JOBS_LOCK = threading.Lock()


class _ParkUnavailable(Exception):
    """A named, first-class reason the live park read cannot be served.

    Absent credential, a park_treasure that will not import, a failed login --
    each carries a `reason` (with the command that fixes it) that `_serve_park`
    renders as `available: false`, never a 500 and never a silent empty list."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ⚠ The park route triggers a LIVE LOGIN, and a login is not free (it spends the
# account credential -- see `_refresh`'s warning). So it is bounded two ways:
#
#  * a short TTL, so a stray refresh or a reopened view within the window reuses
#    the last read instead of firing another login;
#  * a lock the login happens INSIDE, so several tabs opening at once coalesce
#    into ONE login rather than a thundering herd.
#
# The pick is frozen for the whole game-day once rolled (Park/Refresh does not
# re-roll), so a cache hit within the TTL cannot be wrong
# about the DAY -- at worst it is a minute behind on a symbol just collected.
# The server itself NEVER calls this at startup or on page load; the page asks
# for it explicitly (behind a lazy view open).
_PARK_TTL_SECONDS = 60.0
_PARK_CACHE: dict[str, tuple[float, dict]] = {}
_PARK_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# The solve, in-process.
# ---------------------------------------------------------------------------
def _card_states() -> dict[str, Any]:
    """`{card_id: CardState}` for the whole collection, resolved once.

    Same three inputs the board endpoint resolves against — the document's per-card
    entries, its `card_default`, and the per-rarity level caps — so the face and the
    Connect picker cannot disagree about what level a card is at.

    ⚠ These drive the DISPLAY only. `card_default: "max"` is a legitimate statement
    about levels and stats (validated 167/167) and is what the solver already scored
    against; it is **refuted for Connect**, and that rule lives where it belongs, in
    `connect_bonus_level` and the board endpoint. Do not reuse this for Connect.
    """
    document = _read_json(COLLECTION_PATH) or {}
    caps_raw = _read_json(CAPS_PATH)
    caps = caps_by_rarity_from_mapping(caps_raw) if caps_raw else {}
    return {
        "entries": document.get("cards") or {},
        "default": document.get("card_default", "max"),
        "caps": caps,
    }


def _state_of(inputs: dict[str, Any], card: Any) -> Any:
    """One card's resolved `(level, bloom, ...)`, or `None` if it cannot be had."""
    match = re.match(r"(\d+)", str(card.rarity or ""))
    rarity = match.group(1) if match else None
    try:
        return resolve_card_state(
            card.id,
            inputs["entries"].get(card.id),
            inputs["default"],
            inputs["caps"].get(rarity) if rarity else None,
        )
    except Exception:  # a malformed entry must not take the whole solve down
        return None


def _card_view(card: Any, art_dir: Path, state: Any = None) -> dict[str, Any]:
    """Everything the client needs to draw one card face. `generations`
    is sorted for a stable render order -- it is a frozenset on `Card`.

    `level` and `bloom` are what the face prints in the game's own bottom-left
    stack. They are `None` when unresolvable, and the client then draws neither
    badge rather than a made-up number — `build_explorer.py`'s rule, kept.
    """
    return {
        "id": card.id,
        "member": card.member,
        "type": card.type,
        "rarity": card.rarity,
        "generations": sorted(card.generations),
        "costume_name": card.costume_name,
        "stats": {
            "performance": card.stats.performance,
            "technique": card.stats.technique,
            "sense": card.stats.sense,
        },
        "stat_sum": card.stat_sum(),
        "has_art": _asset_known(art_dir, art_dir / f"{card.id}.webp"),
        "level": getattr(state, "level", None),
        "bloom": getattr(state, "bloom", None),
        # The OUTFIT's skill, which is the only thing a leader contributes
        # Shipped for every card because any card can be the leader.
        "costume_skill": _skill_view(card.costume_skill),
        "costume_text": _costume_texts().get(card.id),
    }


_COSTUME_TEXTS: dict = {"mtime": None, "texts": {}}


def _costume_texts() -> dict[str, str]:
    """{card id: the game's own English outfit-skill text} (LangGeneratedLiveLeaderSkill,
    carried by `cards_enriched.json` `descriptions.COSTUME`), re-read when the file moves.
    The page prints this rather than rebuilding a sentence from the parsed skill, which
    can drop a second clause (Choco's Coquette Temptation: "... Score Support Effect of 25%")."""
    path = PROXY_DIR / "dumps" / "cards_enriched.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _COSTUME_TEXTS["mtime"] != mtime:
        doc = _read_json(path) or {}
        rows = doc if isinstance(doc, list) else (doc.get("cards") or list(doc.values()))
        texts = {}
        for row in rows:
            if isinstance(row, dict) and row.get("id"):
                text = ((row.get("descriptions") or {}).get("COSTUME") or "").strip()
                if text:
                    texts[row["id"]] = text
        _COSTUME_TEXTS.update(mtime=mtime, texts=texts)
    return _COSTUME_TEXTS["texts"]


def _skill_view(slot: Any) -> dict[str, Any] | None:
    """The few fields a skill needs to be described in one line, or `None`.

    Deliberately not `dataclasses.asdict`: a slot field can hold an `Unknown`
    sentinel, and the point of that sentinel is that it must reach the reader as
    "unknown" rather than be coerced into a number. Anything not a plain scalar
    is stringified, so an `Unknown` prints as itself.
    """
    if slot is None:
        return None

    def plain(value: object) -> object:
        if value is None or isinstance(value, (int, float, str, bool)):
            return value
        kind = getattr(value, "kind", None)
        return str(kind) if kind is not None else str(value)

    return {
        "family": plain(getattr(slot, "family", None)),
        "effect_type": plain(getattr(slot, "effect_type", None)),
        "magnitude": plain(getattr(slot, "magnitude", None)),
        "target": plain(getattr(slot, "target", None)),
        "condition": plain(getattr(slot, "condition", None)),
    }


def _load_build_board():
    """`build_board.py` is a recon script under `tools/`, not an installed
    package, so it is imported by path — the same way `tests/` exercises it.
    Returns `None` when it cannot be loaded, because an editable board is a
    feature and its absence must degrade rather than break the solve."""
    try:
        spec = importlib.util.spec_from_file_location(
            "build_board",
            REPO_ROOT / "tools" / "recon" / "proxy" / "build_board.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 — any failure means "no editable board"
        return None


def _edited_board_model():
    """`(BoardModel, report lines)` built from the collection's layer C, or
    `None` when nothing has been edited.

    Reuses `build_board.build` rather than re-deriving the mapping: a second
    conversion path is exactly the silent divergence this module's docstring
    warns about, and the editor's schema is `board_state.json`'s shape
    precisely so that reuse is possible.

    Connect levels are forced to the base (`level=1`), NOT derived from the
    collection: `build_board`'s own docstring records that `card_default:
    "max"` is validated for stats and REFUTED for Connect, and an edited board
    must not quietly become an upper bound."""
    document = _read_json(COLLECTION_PATH) or {}
    characters = document.get("characters") or {}
    if not characters:
        return None

    build_board = _load_build_board()
    skilltree = _read_json(SKILLTREE_PATH)
    node_master = _read_json(NODE_MASTER_PATH)
    if build_board is None or not skilltree or not node_master:
        return None

    try:
        board_document, _report, problems = build_board.build(
            characters, skilltree, node_master, level=1
        )
        model = board_from_mapping(board_document, source=str(COLLECTION_PATH))
    except (BoardError, KeyError, TypeError, ValueError) as exc:
        # A broken edit must NOT silently discard the captured board — that
        # would answer a different question without saying so. Keep the
        # capture, and say loudly why the edit was not used.
        return None, [
            f"BOARD: the edited board in {COLLECTION_PATH.name} could not be "
            f"built and was IGNORED -- {exc}",
            "BOARD: falling back to the captured board; the ranking below is "
            "NOT the board you edited",
        ]

    lines = [
        f"BOARD: EDITED -- {len(characters)} character board(s) from "
        f"{COLLECTION_PATH.name}, superseding the capture"
    ]
    lines.extend(f"BOARD: {problem}" for problem in problems[:5])
    return model, lines


def _board_model_from_state(state):
    """A `BoardModel` built from a `{chr: {nodes, slots}}` board state — the shape
    the board-plan optimiser emits — so a request can score/timeline against the
    OPTIMISED board, not the captured one. Reuses `build_board.build` (the one
    conversion path) exactly as `_edited_board_model`; base Connect level (1), since
    the active-uptime the timeline cares about is node-driven, not socket-driven.
    Returns None on any failure, so the caller falls back to the captured board."""
    if not isinstance(state, dict) or not state:
        return None
    build_board = _load_build_board()
    skilltree = _read_json(SKILLTREE_PATH)
    node_master = _read_json(NODE_MASTER_PATH)
    if build_board is None or not skilltree or not node_master:
        return None
    try:
        board_document, _report, _problems = build_board.build(
            state, skilltree, node_master, level=1
        )
        return board_from_mapping(board_document, source="optimised-plan")
    except (BoardError, KeyError, TypeError, ValueError):
        return None


def _song_labels() -> tuple[dict[str, str], dict[str, str]]:
    """`(titles, singers)` by music id, from the display-only language export.

    One reader for `_serve_songs` and the solve's song block, so the two can
    never disagree about what a song is called. Deliberately NOT cached: the
    refresh button can rebuild the dump under a running server, and a stale
    title is exactly the kind of lie a cache tells. Absent file, two empty
    maps, and every label falls back to the music id -- a visible placeholder
    rather than a blank."""
    titles: dict[str, str] = {}
    singers: dict[str, str] = {}
    rows = _read_json(SONG_TITLES_PATH) or []
    for row in rows if isinstance(rows, list) else []:
        key = str((row or {}).get("id") or "")
        text = ((row or {}).get("data") or {}).get("text")
        if not text:
            continue
        if key.startswith("la-music_title-"):
            titles[key[len("la-music_title-") :]] = text
        elif key.startswith("la-singer_name-"):
            singers[key[len("la-singer_name-") :]] = text
    return titles, singers


def _song_titles() -> dict[str, str]:
    """Just the titles half of `_song_labels`, for the solve's song block."""
    return _song_labels()[0]


def run_solve_job(
    job: Job,
    top_n: int,
    art_dir: Path,
    basis: str = "account",
    use_board: bool = True,
    song: str = "",
    pool_filter: str = "all",
    require: tuple[str, ...] = (),
    leader: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
    include_unowned: bool = False,
    force_exact: bool = False,
    estimate: bool = False,
    across_songs: bool = False,
    uptime_model: str | None = None,
    assume_level: object = None,
    assume_bloom: object = None,
    platform: str = DEFAULT_PLATFORM,
    difficulty: str = "EXPERT",
) -> None:
    """The whole CLI `solve` path minus argparse and printing, so the
    numbers the browser sees are the numbers `hbuilder solve` prints.

    Deliberately reuses `load_inputs`, `project_arrangements`,
    `project_duration`, `should_use_heuristic`, `solve_heuristic_labelled`
    / `solve_exact`, `score_with_breakdown` and `build_payload` rather
    than reimplementing any of them: a second scoring path is exactly the
    kind of silent divergence that is this project's most expensive failure
    mode.

    `song` (a music id, e.g. `"m0346"`; `""` means song-agnostic) does NOT
    change how the search runs. Exactly as `cli.py`'s `--song` wires it: the
    song-agnostic search still enumerates and still keeps its top K -- that
    stage is the FILTER -- and the kept teams are re-scored on the real chart
    by `rank_on_chart` (the CLI's own re-ranker, never a second one) and
    re-ordered by expected run score. The payload then says `sorted_by:
    "song_score"`, because a table sorted by one number while displaying
    another is this project's named failure."""
    emit = job.events.put
    t0 = time.monotonic()
    try:
        # Song mode resolves its chart FIRST: a ~10 s search followed by
        # "no such song" would be a diagnostic withheld for ten seconds.
        song = (song or "").strip()
        # --across-songs ranks by the MEAN expected score over every EXPERT chart
        # (with a WINS count); it is mutually exclusive with a single --song, and a
        # single song wins the tie if somehow both are set.
        across_songs = across_songs and not song
        chart = None
        all_charts = None
        if song or across_songs:
            charts_file = _charts_path(difficulty)
            library = load_charts(str(charts_file))
            if library is None:
                emit({
                    "type": "error",
                    "message": (
                        f"{'across-songs' if across_songs else 'song ' + repr(song)}: "
                        f"{charts_file.name} is not on disk (it lives under gitignored "
                        f"dumps/). Build it with tools/recon/proxy/build_charts.py."
                    ),
                })
                return
            if across_songs:
                all_charts = tuple(
                    c for c in (library.get(mid, difficulty) for mid in library.music_ids)
                    if c is not None
                )
                if not all_charts:
                    emit({"type": "error", "message": (
                        f"across-songs: no {difficulty} charts in {charts_file.name}")})
                    return
            else:
                chart = library.get(song, difficulty)
                if chart is None:
                    known = ", ".join(library.music_ids[:8])
                    emit({
                        "type": "error",
                        "message": (
                            f"song: no {difficulty} chart for {song!r} in "
                            f"{charts_file.name} ({len(library.music_ids)} songs "
                            f"available: {known}...)"
                        ),
                    })
                    return

        # How many teams stage 1 hands to the re-ranker. The CLI's own line
        # (`run_solve`: `search_top = max(args.top, args.song_pool)`), with
        # the CLI's own default. DEFAULT_SONG_POOL is 150 on an observed basis
        # (cli.py: the true per-song winner has been observed as deep as
        # stage-1 rank 61, so 150 is ~2.5x the deepest observation) -- do not
        # shrink it here to make the page feel faster; stage 1 is a filter
        # and a dropped winner is silent.
        #
        # ACROSS-songs needs the SAME wide pool: stage 1 filters by the
        # song-agnostic Unit Score, and the mean-per-song winner is a DIFFERENT
        # team (Unit Score and per-song score diverge: a maxed Shirogane Noel
        # team can win the across mean board-off yet be left out of a top_n=20
        # handover by its Unit Score). Handing across only `top_n` teams would
        # be exactly that silent drop.
        wide_pool = chart is not None or across_songs
        search_top = max(top_n, DEFAULT_SONG_POOL) if wide_pool else top_n

        emit({"type": "status", "message": "loading cards"})
        cards, owned, cfg, provenance = load_inputs(CARDS_PATH, OWNED_PATH, False)
        # Optional ACTIVE-uptime override, mirroring the CLI's `--uptime-model`.
        # None inherits config.json's value (steady by default); a validated
        # finite/steady swaps the reported Unit Score model. `replace` preserves
        # every other field, exactly as the board/account replaces below do.
        if uptime_model in ACTIVE_UPTIME_MODELS:
            cfg = dataclasses.replace(cfg, active_uptime_model=uptime_model)
        # Platform (mobile | pc): which LiveNote PERFECT column weighs the notes.
        if platform in PLATFORMS:
            cfg = dataclasses.replace(cfg, platform=platform)

        # The Holomem Board, through the CLI's own loader -- never a second one.
        # Without this the page would serve the boardless answer while the CLI
        # serves the board one, which is the silent divergence the module docstring
        # above exists to prevent. The board can flip the best leader (e.g. from
        # Mori Calliope 5* to Ninomae Ina'nis 4*) and costs ~6x the runtime
        # (10.2 s -> 63.6 s on a 91-card pool), so the state is emitted to the
        # client rather than left implicit.
        # ⛔ The board is scored by DEFAULT and it dominates the answer: it is
        # 29.4-48.7 % of Overall Power, and when only one holomem has
        # leader-scoped red nodes (worth ~29,020) she leads every team with the
        # board and no team without it.
        #
        # Example, same pool, cards maxed:
        #     with board     564,302.8   leader card-04003 (Ina)
        #     without board  486,334.3   leader card-00012 (Subaru), with Choco
        #
        # Both are right; they answer different questions. Hence a per-request
        # toggle rather than a module constant.
        # Board OFF means "optimise as if EVERY unit's board were EMPTY", so the
        # ranking answers the from-scratch question. That excludes the capture AND
        # the EDITED board — otherwise "board off" would still score the boards
        # the account has, which is not the question being asked. So the board is
        # consulted (capture or edit) only when it is ON.
        board_off = BOARD_DISABLED or not use_board
        # The card basis FIRST (it does not touch the board): on the
        # maxed-account basis the "current" board is the scenario's bench —
        # every holomem at the full tree — not the account's capture, so the
        # board block below needs to know which basis it is on.
        cards, collection_basis, cfg, scenario = _apply_basis(
            cards, owned, cfg, basis,
            assume_level=assume_level, assume_bloom=assume_bloom)
        if scenario is not None and not board_off:
            board_model = scenario.board_model
            board_lines = [
                f"BOARD: maxed-account bench -- {len(scenario.bench)} holomem boards at "
                f"the full {MAXED_ACCOUNT_BUDGET} points, {scenario.bench_sockets} Connect "
                "sockets at level 2; NOT your capture"]
        else:
            board_basis = load_board_basis(None if board_off else BOARD_PATH)
            board_model = board_basis.model
            board_lines = list(board_basis.lines())

        # An EDITED board supersedes the capture. `state/collection.json`'s
        # layer C carries the player's own selection -- for planning ("where do
        # my idle points go"), and for characters that appear in no capture at
        # all. Its schema is deliberately `board_state.json`'s shape, so the
        # SAME converter turns it into a board; nothing here re-implements that
        # mapping.
        #
        # Converted in memory and never written: `board.json` is not this
        # package's to write, and `test_no_writes.py`'s fourth guard says so.
        # Gated on the board being ON, so OFF is a true empty-board run. On the
        # maxed-account basis the player's edit is not this account's: skipped.
        edited = None if (board_off or scenario is not None) else _edited_board_model()
        is_edited = False
        if edited is not None:
            edited_model, edited_lines = edited
            if edited_model is not None:
                board_model, board_lines, is_edited = edited_model, edited_lines, True
            else:
                # The edit failed to build. Keep the capture and carry BOTH
                # sets of lines, so the page says which board it actually
                # scored rather than implying the edited one.
                board_lines = board_lines + edited_lines
        # ALWAYS set cfg.board: when OFF, board_model is None, which scores an
        # EMPTY board for every unit rather than whatever load_inputs carried.
        cfg = dataclasses.replace(cfg, board=board_model)
        emit({
            "type": "board",
            "loaded": board_model is not None,
            "edited": is_edited,
            "lines": board_lines,
        })

        # The collection, through the CLI's own `apply_collection` -- never a
        # second one, for the same reason as the board above.
        #
        # ⚠ Without this the page's collection EDITOR values would be ignored
        # by the Solve button: every web solve would score a maxed collection
        # with no account bonuses, while `hbuilder solve` scores the real levels
        # from the same file. A tool that offers an input and then discards it
        # is worse than one that never asked.
        #
        # Applied BEFORE the pool is built, exactly where `cli.py` applies it,
        # so the solver, the projection and the payload all see ONE set of
        # stats. `superseded_by_real_stats` is False because this path always
        # loads mode A (`load_inputs(..., False)` above); a mode-B document
        # would carry measured per-card stats that the collection must never
        # overwrite with computed ones.
        # THREE bases:
        #   account        what can I field TONIGHT -- real levels and blooms
        #   maxed          what is the best team in the game right now (every card, maxed)
        #   maxed-account  the best team on a FULLY MAXED ACCOUNT, comparable with
        #                  holodori.best: maxed cards + every bench board at 453 +
        #                  the master's poster count + the 11.58 % power-up ceiling
        #
        # There is no "my cards, maxed" basis: it is exactly `account` + Assume
        # level Max + Assume bloom Max, which the level/bloom what-if filters
        # express (identical to the decimal).
        # `_apply_basis` (above the board block) already applied the collection
        # / the scenario; the level/bloom what-ifs only bite on the account basis.
        maxed = _cards_maxed(basis)
        emit({
            "type": "collection",
            "maxed": collection_basis.maxed,
            "applied": collection_basis.bonuses is not None,
            "lines": (scenario.lines() if scenario is not None
                      else list(collection_basis.lines())),
            "warnings": list(collection_basis.warnings[:10]),
            **({"scenario": scenario.summary()} if scenario is not None else {}),
        })

        # Search constraints, exactly as `cli.run_solve` builds them from
        # --require/--leader/--exclude/--include-unowned (L1543): lock a card into
        # every team, pin the leader slot, bar a card, or let a selector name an
        # unowned card. Driven by the page.
        selectors = Selectors(
            require=tuple(require),
            leader=tuple(leader),
            exclude=tuple(exclude),
            include_unowned=bool(include_unowned),
        )
        # A maxed account owns everything, so the ownership set IS the
        # catalogue. Widening `owned` is the lever, not `include_unowned` --
        # that flag only admits unowned cards a selector NAMES. Same line as
        # `cli.run_solve`, deliberately.
        pool_source = list(cards if maxed else owned)
        if pool_filter == "5star":
            # The id's 3rd segment is the rarity rank (`card-00001-5-uniq-...`).
            pool_source = [
                c for c in pool_source
                if (str(c.id).split("-") + ["", "", ""])[2] == "5"
            ]
        try:
            pool: CandidatePool = resolve_selectors(cards, pool_source, selectors)
        except SelectorError as exc:
            # A bad id or a require/exclude/leader conflict is the user's, not a
            # crash: report it verbatim (it already names the flag and id) and stop.
            emit({"type": "error", "message": str(exc)})
            return
        if len(pool.cards) < MEMBER_COUNT:
            emit(
                {
                    "type": "error",
                    "message": (
                        f"insufficient pool: {len(pool.cards)} candidate(s) "
                        f"remain, {MEMBER_COUNT} required"
                    ),
                }
            )
            return

        set_count, arrangement_count = project_arrangements(
            len(pool.cards), len(pool.locked_ids), pool.leader_id is not None
        )
        try:
            throughput = load_throughput()
        except (ThroughputSchemaError, json.JSONDecodeError, OSError):
            # Same degradation the CLI chooses: a rotted benchmark side-car
            # must not crash a solve, it only costs the advisory duration.
            throughput = None
        seconds_or_none, duration_note = project_duration(
            arrangement_count,
            throughput,
            config_hash(_scoring_config_document(cfg)),
            set_count=set_count,
        )
        projection = Projection(
            pool_size=len(pool.cards),
            locked_count=len(pool.locked_ids),
            leader_fixed=pool.leader_id is not None,
            set_count=set_count,
            arrangement_count=arrangement_count,
            seconds_or_none=seconds_or_none,
            throughput_warning=duration_note,
        )
        # `force_exact` (the CLI's --exact) overrides the auto-switch to the beam
        # above the threshold — the user demanding the exhaustive answer.
        use_heuristic = should_use_heuristic(projection, force_exact)
        emit(
            {
                "type": "projection",
                "pool_size": projection.pool_size,
                "set_count": projection.set_count,
                "arrangement_count": projection.arrangement_count,
                "projected_seconds": projection.seconds_or_none,
                "solver": "heuristic" if use_heuristic else "exact",
                "owned_count": len(owned),
            }
        )
        # `--estimate`: the projection above is the whole answer — report the cost
        # and stop, never enumerate. The frontend shows it and re-enables Solve.
        if estimate:
            emit({
                "type": "estimate",
                "arrangement_count": projection.arrangement_count,
                "projected_seconds": projection.seconds_or_none,
                "solver": "heuristic" if use_heuristic else "exact",
            })
            emit({"type": "end"})
            return

        def progress(evaluated: int) -> None:
            if job.cancel.is_set():
                raise _Cancelled()
            emit({"type": "progress", "evaluated": evaluated})

        heuristic_result = None
        if use_heuristic:
            heuristic_result = solve_heuristic_labelled(
                pool.cards,
                cfg,
                top_n=search_top,
                progress=progress,
                constraint=constraint_from(pool),
                set_constraint=set_constraint_from(pool),
                locked_ids=pool.locked_ids,
            )
            ranked = heuristic_result.teams
            emit(
                {
                    "type": "progress",
                    "evaluated": heuristic_result.arrangements_scored,
                    "complete": True,
                }
            )
        else:
            ranked = solve_exact(
                pool.cards,
                cfg,
                top_n=search_top,
                progress=progress,
                constraint=constraint_from(pool),
                set_constraint=set_constraint_from(pool),
                locked_ids=pool.locked_ids,
                leader_id=pool.leader_id,
            )
            emit(
                {
                    "type": "progress",
                    "evaluated": projection.arrangement_count,
                    "complete": True,
                }
            )

        # -- song mode: stage 2, the re-rank ------------------------------
        # Through `rank_on_chart`, the CLI's own re-ranker -- `cli.py`'s
        # `_render_song_ranking` goes through the same function, so the
        # expected run score the page shows is the one the CLI prints.
        song_rows = None
        song_block = None
        across_block = None
        if chart is not None:
            handed = len(ranked)
            emit({
                "type": "status",
                "message": f"re-scoring top {handed} teams on {song}",
            })

            def song_progress(scored: int, total: int) -> None:
                if job.cancel.is_set():
                    raise _Cancelled()
                emit({"type": "song_progress", "scored": scored, "total": total})

            ranking = rank_on_chart(
                [candidate.team for candidate in ranked],
                chart,
                cfg,
                filtered_from=handed,
                progress=song_progress,
            )
            # Map each re-ranked row back to the candidate that produced it.
            # (leader, sorted member ids) is how `rank_on_chart` itself keys a
            # team, so the join cannot drift from the thing it joins.
            by_key = {
                (
                    candidate.team.leader.id,
                    tuple(sorted(card.id for card in candidate.team.members)),
                ): candidate
                for candidate in ranked
            }
            ranked = tuple(
                by_key[(row.leader_id, row.member_ids)]
                for row in ranking.teams[:top_n]
            )
            song_rows = list(ranking.teams[: len(ranked)])
            # Title through the same display-only language export
            # `_serve_songs` reads; the id stands in when there is none, and
            # nothing is scored from it either way.
            song_block = {
                "music_id": song,
                "title": _song_titles().get(song) or song,
                "difficulty": difficulty,
                "pool": handed,
            }
        elif across_songs and all_charts:
            # --across-songs: score the top-K on EVERY chart and rank by the MEAN
            # expected score, with a WINS count. workers=1 keeps it in-process
            # (rank_across_charts_parallel runs rank_across_charts directly at 1
            # worker) — no process spawn from this request thread.
            handed = len(ranked)
            emit({"type": "status", "message": (
                f"scoring top {handed} teams across {len(all_charts)} charts")})

            def across_progress(done: int, total: int) -> None:
                if job.cancel.is_set():
                    raise _Cancelled()
                emit({"type": "song_progress", "scored": done, "total": total})

            ranking = rank_across_charts_parallel(
                [candidate.team for candidate in ranked], all_charts, cfg,
                filtered_from=handed, workers=1, progress=across_progress,
            )
            by_key = {
                (c.team.leader.id,
                 tuple(sorted(card.id for card in c.team.members))): c
                for c in ranked
            }
            ranked = tuple(
                by_key[(row.leader_id, row.member_ids)]
                for row in ranking.teams[:top_n]
            )
            song_rows = list(ranking.teams[: len(ranked)])
            across_block = {"chart_count": ranking.chart_count, "pool": handed}
            if difficulty != "EXPERT":
                across_block["difficulty"] = difficulty

        breakdowns = tuple(score_with_breakdown(c.team, cfg) for c in ranked)
        payload = build_payload(
            ranked,
            breakdowns,
            cfg,
            pool.cards,
            {
                "require": [],
                "leader": pool.leader_id,
                "exclude": [],
                "include_unowned": False,
            },
            explain_rank=1 if ranked else None,
            owned_count=len(owned),
            arrangement_count=projection.arrangement_count,
        )

        # ⛔ `sorted_by` is load-bearing and exists for one reason: this
        # project's named failure is a table sorted by one number while
        # displaying another. The page renders whichever field this names as
        # the headline number, so it must name the field the ordering above
        # actually used -- and `score` is never overloaded to mean two
        # different quantities depending on a flag.
        # Per-team mode fields go INTO the team objects. These keys exist ONLY in
        # the song/across modes, which have no CLI `--json` contract to preserve;
        # an agnostic payload carries none of them, byte-for-byte as before.
        if across_block is not None:
            payload["sorted_by"] = "across_song"
            payload["across"] = across_block
            for team_obj, row in zip(payload["teams"], song_rows):
                # The MEAN expected score over every chart, and the WINS count.
                # ⚠ NOT comparable to `score` — different quantities/scales.
                team_obj["across_score"] = row.expected_score
                team_obj["wins"] = row.wins
                # the slot order best on the most charts (one per song exists)
                team_obj["member_order"], team_obj["member_order_share"] =                     row_detail.modal_order(row.per_chart)
        elif song_rows is not None:
            payload["sorted_by"] = "song_score"
            payload["song"] = song_block
            for team_obj, row in zip(payload["teams"], song_rows):
                # `expected_score` and the slot order that achieved it, from
                # the SAME `rank_on_chart` row that decided the ordering.
                # ⚠ NOT comparable to `score` -- different quantities on
                # different scales; the page must never show a delta.
                team_obj["song_score"] = row.expected_score
                team_obj["member_order"] = list(row.member_order)
                # the ceiling (every ACTIVE fires) on the same board and basis,
                # at the row's own slot order
                if chart is not None:
                    team_obj["song_max"] = score_team_ceiling(
                        row.team, chart, cfg, row.member_order)
        else:
            payload["sorted_by"] = "score"

        # Certificates. Song and
        # across responses carry `model_version` + `inputs_hash` and one
        # `certificate` per row. Stage 1 + rank_on_chart is HEURISTIC by
        # construction (best of the N arrangements the beam/exact search kept
        # and re-scored); a row is upgraded to EXACT/BOUNDED only when a
        # certificate on file passes the same-instant-same-basis guard — this
        # path scores the board OFF or AS CAPTURED, so its `budget_kind` is
        # "none"/"captured" and a flat-453 developed certificate can never be
        # borrowed for it. The agnostic payload stays byte-identical (CLI
        # `--json` contract).
        if scenario is not None:
            payload["scenario"] = scenario.summary()
        if song_rows is not None:
            payload.update(_certificate_meta(cfg, basis, scenario=scenario))
            payload["certificate_basis"] = cert_mod.certificate_basis(
                universe=cert_mod.universe_name(basis=basis, pool=pool_filter),
                basis=basis,
                budget_kind="captured" if cfg.board is not None else "none",
                budget=None, uptime_model=cfg.active_uptime_model,
                platform=cfg.platform, difficulty=difficulty)
            _gen = ("beam search (solve_heuristic_labelled)" if heuristic_result is not None
                    else "exhaustive search (solve_exact)")
            _stages = [f"stage 1: {_gen}, top {search_top} by Unit Score",
                       ("stage 2: rank_on_chart on " + song) if chart is not None
                       else f"stage 2: mean over {len(all_charts or ())} charts"]
            _n = (heuristic_result.arrangements_scored if heuristic_result is not None
                  else projection.arrangement_count)
            for team_obj in payload["teams"]:
                hc = certindex.heuristic_certificate(
                    model_version=payload["model_version"],
                    inputs_hash=payload.get("inputs_hash"),
                    basis=payload["certificate_basis"], generator=_gen,
                    stages=_stages, candidates_evaluated=_n,
                    note=("across-songs rows are always HEURISTIC in v2 (no per-chart bound)"
                          if chart is None else None))
                if chart is not None:
                    hc = certindex.attach(
                        hc, song=song, leader=team_obj["leader_id"],
                        member_ids=list(team_obj["member_ids"]),
                        row_score=team_obj.get("song_score"))
                team_obj["certificate"] = hc

        # The `search` block, byte-for-byte the CLI's own (cli.py's
        # `--json` path builds it outside `build_payload` for the
        # diffability reason recorded there: an EXACT run's payload must
        # stay stable, so the key exists only when the beam ran).
        # Reproduced here rather than summarised,
        # because the page's APPROXIMATE banner is built from it and a
        # summary would be a second source of truth for the one caveat the
        # result must never be read without.
        if heuristic_result is not None:
            payload["search"] = {
                "mode": "heuristic",
                "approximate": True,
                "beam_width": heuristic_result.beam_width,
                "branch": heuristic_result.branch,
                "seeds": heuristic_result.seeds,
                "arrangements_scored": heuristic_result.arrangements_scored,
                "partials_scored": heuristic_result.partials_scored,
                "exhaustive_arrangement_count": projection.arrangement_count,
                "exhaustive_projected_seconds": projection.seconds_or_none,
                "failure_modes": sorted(
                    mode.value for mode in heuristic_result.modes()
                ),
                "observations": [
                    {
                        "mode": observation.mode.value,
                        "detail": observation.detail,
                        "level": observation.level,
                        "margin": observation.margin,
                        "seed": observation.seed,
                    }
                    for observation in heuristic_result.observations
                ],
                # A tool that recommends a team without its slot order hands
                # the player the median case.
                "member_orders": {
                    ",".join(member_ids): (
                        list(order) if order is not None else None
                    )
                    for member_ids, order in heuristic_result.member_orders.items()
                },
            }

        # Card metadata for the faces, keyed by id -- the union over ALL
        # ranked teams in `payload["teams"]`, not the whole pool. At top=10
        # that is typically ~10 distinct cards, not 60.
        needed: set[str] = set()
        for team in payload["teams"]:
            needed.add(team["leader_id"])
            needed.update(team["member_ids"])
        by_id = {c.id: c for c in pool.cards}
        state_inputs = _card_states()
        payload["cards"] = {
            cid: _card_view(by_id[cid], art_dir, _state_of(state_inputs, by_id[cid]))
            for cid in sorted(needed)
            if cid in by_id
        }
        # The game's own per-generation ribbon tints, shipped once rather than per
        # card: ~15 groups against up to 60 faces.
        payload["gen_colors"] = _gen_palette()
        payload["gen_labels"] = _gen_labels()
        payload["assumptions"] = list(format_assumptions(cfg))
        payload["explain_lines"] = (
            list(format_explain(1, ranked[0], breakdowns[0])) if ranked else []
        )
        # Per-rank attribution, for EVERY rank -- the page renders N teams,
        # and per-skill attribution is the thing the tool is for.
        #
        # `build_payload` attaches `contributions` to exactly one team, the
        # one `--explain <rank>` names (`json_output.py`: depth is a
        # parameter, format is the module). Widening that would change the
        # CLI's own `teams[]` contract, which `--json` consumers diff
        # against -- so `payload["teams"]` is left byte-identical to the
        # CLI's and the rest goes in a side key built by the SAME
        # `build_team_object` / `format_explain` calls. A second location,
        # never a second computation: no scoring path is duplicated here.
        payload["explain_by_rank"] = [
            {
                "rank": rank,
                # The ROSTER this entry belongs to, so a client that re-orders
                # (develop) or re-composes (refine) teams can find the right
                # breakdown by roster instead of by stage-1 rank number — by
                # rank, a developed #1 would show ANOTHER team's breakdown.
                "roster": [candidate.leader_id, *candidate.member_ids],
                "lines": list(format_explain(rank, candidate, breakdown)),
                "contributions": build_team_object(
                    rank, candidate, breakdown, with_contributions=True
                )["contributions"],
                # The term-level panel, for EVERY rank -- same reasoning as
                # `contributions` above and the same discipline: `teams[]`
                # stays byte-identical to the CLI's, and the widening lives in
                # this side key, built by `json_output`'s own formatter rather
                # than assembled here. `board_modelled` distinguishes "the
                # board contributed nothing" from "no board was loaded", which
                # neither `board_power` nor `board_score_rate` can say alone.
                "terms": build_score_terms(
                    breakdown, board_modelled=cfg.board is not None
                ),
            }
            for rank, (candidate, breakdown) in enumerate(
                zip(ranked, breakdowns), start=1
            )
        ]
        payload["approximation"] = (
            list(format_approximation(heuristic_result, projection))
            if heuristic_result is not None
            else []
        )
        payload["elapsed_ms"] = round((time.monotonic() - t0) * 1000)
        if provenance is not None:
            payload["stats_basis"] = {
                "mode": provenance.mode,
                "basis": provenance.stats_basis,
                "generated_at": provenance.generated_at,
            }
        emit({"type": "result", "payload": payload})
    except _Cancelled:
        emit({"type": "cancelled", "elapsed_ms": round((time.monotonic() - t0) * 1000)})
    except _LoadFailure as exc:
        emit({"type": "error", "message": f"input data error (exit {exc.exit_code})"})
    except SelectorError as exc:
        emit({"type": "error", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 -- a job thread must not die silently
        emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        job.done = True
        emit({"type": "end"})


def run_acquire_job(
    job: Job,
    art_dir: Path,
    use_board: bool = True,
    song: str = "",
    pool_filter: str = "all",
    uptime_model: str | None = None,
    assume_level: object = None,
    assume_bloom: object = None,
    top_cards: int = 15,
    require: tuple[str, ...] = (),
    leader: tuple[str, ...] = (),
    platform: str = DEFAULT_PLATFORM,
    difficulty: str = "EXPERT",
) -> None:
    """Which single card the account does NOT own would raise its best score
    most — the CLI's `--acquire` question, served.

    Method, and what it costs. A per-candidate beam solve would be exact and
    ~minutes; this is the bounded version that stays interactive:

    1. ONE `per_leader_best` beam over the OWNED pool gives ~one seed team per
       leader — the same anti-monoculture seed set the develop ranking uses,
       so a candidate whose best home is under a leader the global top-N
       buries is still measured against that leader's team.
    2. Every candidate is tried in every seed's six roles (leader + 5 member
       slots, Team rules enforced); the metric is the per-song expected score
       when a song is chosen, else the deck-screen Unit Score, both on the
       SAME board/uptime the request asked for. The best mutation is that
       candidate's score; its gain is the delta over the best OWNED seed under
       the identical metric.
    3. Candidates are scored MAXED (level/bloom cap) while the owned cards
       keep the account basis — the question is "what could this card do for
       me if I built it", and `card_default: unknown` would otherwise score
       every candidate at an unresolved level. Stated in the payload.

    Bounded honestly: a candidate whose best team contains NO current seed
    (all five members different) is under-measured; the seed sweep is a lower
    bound on each candidate's true ceiling, never an over-claim."""
    emit = job.events.put
    t0 = time.monotonic()
    try:
        song = (song or "").strip()
        chart = None
        if song:
            charts_file = _charts_path(difficulty)
            library = load_charts(str(charts_file))
            chart = library.get(song, difficulty) if library is not None else None
            if chart is None:
                emit({"type": "error", "message": (
                    f"song: no {difficulty} chart for {song!r} in {charts_file.name}")})
                return

        emit({"type": "status", "message": "loading cards"})
        cards, owned, cfg, _prov = load_inputs(CARDS_PATH, OWNED_PATH, False)
        if uptime_model in ACTIVE_UPTIME_MODELS:
            cfg = dataclasses.replace(cfg, active_uptime_model=uptime_model)
        if platform in PLATFORMS:
            cfg = dataclasses.replace(cfg, platform=platform)

        board_off = BOARD_DISABLED or not use_board
        board_basis = load_board_basis(None if board_off else BOARD_PATH)
        board_model = board_basis.model
        edited = None if board_off else _edited_board_model()
        if edited is not None and edited[0] is not None:
            board_model = edited[0]
        cfg = dataclasses.replace(cfg, board=board_model)

        owned_ids = [card.id for card in owned]
        account_cards, collection_basis = apply_collection(
            cards, str(COLLECTION_PATH), superseded_by_real_stats=False,
            owned_ids=owned_ids, maxed=False,
            assume_level=assume_level, assume_bloom=assume_bloom,
        )
        if collection_basis.bonuses is not None:
            cfg = dataclasses.replace(cfg, account=collection_basis.bonuses)
        # Candidates at their CAP (see docstring point 3) — a second
        # apply_collection pass, maxed, same loader, never a private formula.
        maxed_cards, _ = apply_collection(
            cards, None, superseded_by_real_stats=False,
            owned_ids=owned_ids, maxed=True,
        )
        maxed_by_id = {c.id: c for c in maxed_cards}

        owned_set = set(owned_ids)
        owned_pool = tuple(c for c in account_cards if c.id in owned_set)
        candidates = [maxed_by_id[c.id] for c in cards
                      if c.id not in owned_set and c.id in maxed_by_id]
        if pool_filter == "5star":
            candidates = [
                c for c in candidates
                if (str(c.id).split("-") + ["", "", ""])[2] == "5"
            ]
        if not candidates:
            emit({"type": "error", "message": "no unowned candidate cards"})
            return

        def metric(team: Team) -> float:
            if chart is not None:
                # the ranker's own per-song figure (Overall Power deck power)
                return score_team_on_chart(team, chart, cfg).total_score
            return deck_screen_unit_score(score_with_breakdown(team, cfg)).unit_score

        # Acquire semantics as in holodori.best: up to four of YOUR cards (and/or a
        # leader) may be PINNED, and every seed team honours them — the same
        # Selectors machinery the solve uses, never a second filter.
        try:
            seed_pool = resolve_selectors(
                account_cards, list(owned_pool),
                Selectors(require=tuple(require), leader=tuple(leader),
                          exclude=(), include_unowned=False))
        except SelectorError as exc:
            emit({"type": "error", "message": str(exc)})
            return

        emit({"type": "status", "message": "building the owned seed teams"})
        res = solve_heuristic_labelled(
            seed_pool.cards, cfg, top_n=LEADER_DIVERSE_POOL, per_leader_best=True,
            constraint=constraint_from(seed_pool),
            set_constraint=set_constraint_from(seed_pool),
            locked_ids=seed_pool.locked_ids)
        by_id = {c.id: c for c in account_cards}
        seeds: list[Team] = []
        for rt in res.teams:
            try:
                seeds.append(Team(leader=by_id[rt.leader_id],
                                  members=tuple(by_id[m] for m in rt.member_ids)))
            except (TeamError, KeyError):
                continue
        if not seeds:
            emit({"type": "error", "message": "no owned seed team could be built"})
            return
        baseline_scored = [(metric(t), t) for t in seeds]
        base_score, base_team = max(baseline_scored, key=lambda p: p[0])

        total = len(candidates)
        emit({"type": "projection", "pool_size": len(owned_pool),
              "set_count": total, "arrangement_count": total * len(seeds) * 6,
              "solver": "acquire-sweep", "owned_count": len(owned)})

        rows = []
        for index, cand in enumerate(candidates):
            if job.cancel.is_set():
                raise _Cancelled()
            best_score, best_team = 0.0, None
            pinned = set(require) | set(leader)
            for seed in seeds:
                roster = [seed.leader, *seed.members]
                for role in range(len(roster)):
                    # a PINNED card is the question's premise — the candidate
                    # may never take its slot.
                    if roster[role].id in pinned:
                        continue
                    mutated = list(roster)
                    mutated[role] = cand
                    try:
                        team = Team(leader=mutated[0], members=tuple(mutated[1:]))
                    except TeamError:
                        continue
                    s = metric(team)
                    if s > best_score:
                        best_score, best_team = s, team
            if best_team is not None:
                rows.append((best_score, cand, best_team))
            emit({"type": "progress", "evaluated": index + 1})
        emit({"type": "progress", "evaluated": total, "complete": True})

        rows.sort(key=lambda r: (-r[0], r[1].id))
        rows = rows[:top_cards]

        # The seed sweep is a LOWER bound per candidate. Close it for the
        # candidates actually shown: hill-climb each one's members by the
        # metric itself (`refine_members`, the develop's own machinery — no
        # second optimiser). Develop is a no-op here (acquire scores the
        # requested board as-is), and the guard makes the PINS and the
        # candidate inviolable: a mutation that drops one scores -1 and can
        # never be accepted. The baseline gets the same refine, so a gain is
        # refined-vs-refined, never refined-vs-sloppy.
        emit({"type": "status", "message": "refining the top candidates"})

        def _bare(team: Team) -> BoardOptimisedTeam:
            return BoardOptimisedTeam(
                team=team, board_off_us=0.0, optimised_us=0.0,
                plan=TeamBoardPlan(total_gain=0.0, per_character={}))

        def _refined(team: Team, score: float, must: frozenset) -> tuple:
            def guard(t, _board, _must=must):
                ids = {t.leader.id, *[m.id for m in t.members]}
                return metric(t) if _must <= ids else -1.0
            out, out_score, _swaps = refine_members(
                _bare(team), candidates=list(owned_pool), develop_fn=_bare,
                score_fn=guard, shortlist=6, max_rounds=2)
            return (out.team, out_score) if out_score > score else (team, score)

        base_team, base_score = _refined(
            base_team, base_score, frozenset(require))
        rows = [(s, cand, team) for score, cand, team in rows
                for team, s in [_refined(team, score,
                                         frozenset({cand.id, *require}))]]
        rows.sort(key=lambda r: (-r[0], r[1].id))

        states = _card_states()
        cards_map: dict[str, dict] = {}
        out = []
        for score, cand, team in rows:
            used = [team.leader.id, *[m.id for m in team.members]]
            for cid in (cand.id, *used):
                if cid in cards_map:
                    continue
                card_obj = by_id.get(cid) or maxed_by_id.get(cid)
                if card_obj is not None:
                    cards_map[cid] = _card_view(
                        card_obj, art_dir, _state_of(states, card_obj))
            out.append({
                "card": cand.id,
                "member": cand.member,
                "score": round(score),
                "gain": round(score - base_score),
                "gain_pct": round(100.0 * (score - base_score) / base_score, 2)
                if base_score else 0.0,
                "team": used,
                "leader_member": team.leader.member,
            })
        payload = {
            "mode": "acquire",
            "metric": ("song_score" if chart is not None else "unit_score"),
            "song": song or None,
            "difficulty": difficulty,
            "board": not board_off,
            "baseline_score": round(base_score),
            "baseline_team": [base_team.leader.id,
                              *[m.id for m in base_team.members]],
            "candidate_basis": "each candidate at MAX level/bloom; owned cards on the account basis",
            "rows": out,
            "cards": cards_map,
            "elapsed_ms": round((time.monotonic() - t0) * 1000),
        }
        emit({"type": "result", "payload": payload})
    except _Cancelled:
        emit({"type": "cancelled",
              "elapsed_ms": round((time.monotonic() - t0) * 1000)})
    except _LoadFailure as exc:
        emit({"type": "error", "message": f"input data error (exit {exc.exit_code})"})
    except Exception as exc:  # noqa: BLE001 -- a job thread must not die silently
        emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        job.done = True
        emit({"type": "end"})


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _read_json(path: Path) -> Any | None:
    """A JSON file, or `None` when it is absent — the same first-class-state
    contract `load_board` has. Read mode only; `test_no_writes.py`'s check 3
    is blanket over this package and this is the one reader it sees."""
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _team_gates(team, cfg) -> list:
    """The per-gate projection the ranking renders, for a team the client did not
    solve itself (leader_diverse surfaces teams outside the solve's payload, so it
    has no gates for them). Same shape as json_output.py's `gates`. Gates resolve
    against the assembled team, independent of the board, so the base cfg suffices."""
    bd = score_with_breakdown(team, cfg)
    return [
        {"card_id": c.card_id, "slot_kind": c.slot_kind,
         "dimension": c.gate_dimension, "value": c.gate_value,
         "threshold": c.gate_threshold, "actual_count": c.gate_actual_count,
         "fired": c.gate_fired}
        for c in bd.contributions if c.gate_fired is not None
    ]


def _is_five_star(card) -> bool:
    """The id's 3rd segment is the rarity rank (`card-00001-5-uniq-...`)."""
    return (str(card.id).split("-") + ["", "", ""])[2] == "5"


def _certificate_meta(cfg, basis_name: str, *,
                      scenario: "MaxedAccountScenario | None" = None) -> dict[str, Any]:
    """`model_version` + `inputs_hash` for a response. The
    hash is over the SAME resolved config shape the certificates hash — `cfg` with
    `board=None` (the board a run develops is an OUTPUT; the captured board file
    is hashed as an input by the recipe itself), platform, uptime model and the
    basis' account bonuses. A missing dump makes the hash `null` with the
    reason, never a hash over whatever files happened to exist. On the
    maxed-account basis the collection line is the scenario literal + the
    generated bench's digest (`cert_mod.scenario_hash`, the probes' recipe)."""
    if basis_name == MAXED_ACCOUNT_BASIS and scenario is not None:
        h, per, err = cert_mod.try_scenario_hash(
            dataclasses.replace(cfg, board=None), scenario.bench, scenario.literal)
    else:
        h, per, err = cert_mod.try_inputs_hash(
            dataclasses.replace(cfg, board=None), "maxed" if basis_name == "maxed" else "account")
    meta: dict[str, Any] = {"model_version": cert_mod.MODEL_VERSION, "inputs_hash": h}
    if err:
        meta["inputs_hash_error"] = err
    if per:
        # the recipe's per-file digests (cards / board / charts / skilltree /
        # master_stn / collection / cfg): the members-first index guards by
        # the enumeration's own key built from these + the song's chart
        # digest (`certificate.enumeration_key`), so a new song in
        # charts.json retires no enumeration
        meta["inputs_hash_files"] = dict(per)
    return meta


def _develop_meta(cfg, basis_name: str, scenario, pool_filter: str,
                  flat_budget: int | None, leaf: str | None = None,
                  difficulty: str = "EXPERT") -> dict[str, Any]:
    """The certificate meta every develop response carries (`model_version`,
    `inputs_hash`, `certificate_basis`; `scenario` on the maxed-account
    basis). One helper so the across, song and Unit-Score returns cannot
    disagree about the basis they were computed on. `leaf` names the board
    the rows' headline number was scored on (`certificate_basis.leaf`:
    "plan_cache" = the fixed-plan v2 objective, "develop" = the per-team
    greedy re-develop)."""
    out = _certificate_meta(cfg, basis_name, scenario=scenario)
    out["certificate_basis"] = cert_mod.certificate_basis(
        universe=cert_mod.universe_name(basis=basis_name, pool=pool_filter),
        basis=basis_name,
        budget_kind="flat" if flat_budget is not None else "real",
        budget=flat_budget, uptime_model=cfg.active_uptime_model,
        platform=cfg.platform, leaf=leaf, difficulty=difficulty)
    if scenario is not None:
        out["scenario"] = scenario.summary()
    return out


def _parse_platform(body: Mapping) -> str:
    """The client whose PERFECT column the per-song scorer weighs notes with:
    "mobile" (FLICK 1050, the default) or "pc" (FLICK
    1000). Absent/empty/unknown -> the default, never an error: the toggle is a
    what-if, and every route that scores a chart (/solve, /acquire,
    /api/optimize-rerank, /api/timeline) reads it through this one function so
    the develop and the timeline cannot re-score on a different platform than
    the ranking above them."""
    platform = str(body.get("platform") or "").lower()
    return platform if platform in PLATFORMS else DEFAULT_PLATFORM


def _parse_difficulty(body: Mapping, song: str = "") -> str:
    """The chart difficulty a route scores on (EXPERT is the default; a manual
    player may want an easier chart). Absent / "" ->
    EXPERT. Unlike the platform toggle an unknown value is an ERROR, a 400 —
    silently scoring EXPERT for a typo would answer a question nobody asked.
    A non-EXPERT difficulty must be on disk (charts_all.json) and, with `song`,
    the song must have a chart at it."""
    try:
        return cert_mod.resolve_difficulty(
            body.get("difficulty"), song or None,
            charts_path=CHARTS_PATH, charts_all_path=CHARTS_ALL_PATH)
    except ValueError as exc:
        raise _HttpError(400, {"error": str(exc)}) from None


def _parse_assume(body: Mapping, key: str, lo: int, hi: int) -> object:
    """The level/bloom what-if token: None/""/"account" -> None (no override,
    use the account's own value); "max" -> the card's cap; an int -> that value
    clamped to [lo, hi]. Shared by /solve and /api/optimize-rerank so the develop
    re-score uses the SAME stats the ranking was solved with (otherwise the
    rerank would re-score maxed teams on level-1 stats)."""
    raw = body.get(key)
    if raw is None:
        return None
    token = str(raw).strip().lower()
    if token in ("", "account"):
        return None
    if token == "max":
        return "max"
    try:
        return max(lo, min(hi, int(token)))
    except ValueError:
        return None


def _socket_catalogue(tree: dict, document: Mapping) -> dict[str, Any]:
    """Everything the editor needs to put a card in a socket.

    The Connect level comes from `domain/collection.connect_bonus_level`, which
    already owns the rule (Bloom 5 on a 4*/5*, and a 3* never levels at all).

    ⚠ **`card_default: "max"` does not license a Connect level**, and this is
    the trap: it is validated for STATS (167/167) and **REFUTED for
    Connect** -- `tests/test_board.py::
    test_bloom_5_connect_overshoots_the_acceptance_number` pins the overshoot at
    **+16.31 %** against the game's own +28.73 pp, on an account whose socket
    cards are below Bloom 5. So a
    Bloom that came from the DEFAULT is not account truth: the level falls back
    to 1, exactly as `build_board`'s `--connect-level 1` default does, and the
    card is tagged `basis: "default"` so the picker can say the number is a
    floor rather than a measurement.

    `cells` is sent once per shape rather than per card -- 113 cards share 20
    shapes -- and the card carries only the shape's name."""
    connect, card_connect = tree.get("connect") or {}, tree.get("card_connect") or {}
    if not card_connect:
        return {"cards": [], "extents": {}}

    caps_raw = _read_json(CAPS_PATH)
    caps = caps_by_rarity_from_mapping(caps_raw) if caps_raw else {}
    entries = document.get("cards") or {}
    default = document.get("card_default", "max")

    by_id = {c["id"]: c for c in (_read_json(CARDS_PATH) or {}).get("cards", [])}
    cards: list[dict[str, Any]] = []
    for card_id, key in sorted(card_connect.items()):
        row = connect.get(key)
        if not row:
            continue
        card = by_id.get(card_id, {})
        match = re.match(r"(\d+)", str(card.get("rarity") or ""))
        rarity = match.group(1) if match else None
        state = resolve_card_state(
            card_id, entries.get(card_id), default, caps.get(rarity) if rarity else None
        )
        # Only a Bloom the player actually stated speaks for the account.
        stated = state.source == "manual"
        level = connect_bonus_level(state.bloom, rarity) if stated else None
        levels = row.get("levels") or []
        step = levels[(level or 1) - 1] if levels else {}
        cards.append(
            {
                "id": card_id,
                "member": card.get("member"),
                "rarity": card.get("rarity"),
                "area": row.get("area"),
                "bloom": state.bloom if stated else None,
                "basis": state.source,
                "level": level or 1,
                "permil": step.get("v"),
                # Both levels, so the editor can say what a Bloom would buy
                # rather than only what the card does today.
                "permil_at_max": (levels[-1] or {}).get("v") if levels else None,
            }
        )
    return {"cards": cards, "extents": tree.get("extents") or {}}


def _board_level_gates() -> list[dict[str, int]]:
    """`[{level, nodes}, ...]` — how many board nodes each account level opens.

    DERIVED from `skilltree.json`, never hardcoded, so it tracks the master
    rather than a number someone typed into the page. `level: 0` is the
    ungated remainder, which is a real bucket and not a rounding error: 25 of
    the 152 groups carry no `req` at all (whether those 25 are genuinely
    ungated is not known, so the label the page uses says "no level gate",
    not "available from level 1").

    Empty when the tree is not on disk — the level control still works, it
    just cannot say what a level buys."""
    tree = _read_json(SKILLTREE_PATH)
    if not tree:
        return []
    counts: dict[int, int] = {}
    for raw in (tree.get("nodes") or {}).values():
        need = player_level_requirement((raw or {}).get("req")) or 0
        counts[need] = counts.get(need, 0) + 1
    return [{"level": lvl, "nodes": counts[lvl]} for lvl in sorted(counts)]


# ---------------------------------------------------------------------------
# The park view: today's hidden collectables, joined to the offline export.
# ---------------------------------------------------------------------------
def _load_park_treasure():
    """`park_treasure.py` under `tools/recon/proxy/`, imported BY PATH the same
    way `_load_build_board` imports `build_board.py`.

    It runs under proxyenv, which is this server's interpreter, and its
    module-level code is import-safe: it only defines functions and constants
    and inserts its own directory on `sys.path` (so its `from account_snapshot
    import ...` resolves) -- it does NOT log in at import time. So importing
    beats shelling out: no second process, no JSON round-trip, no second
    interpreter. `park_map.py` shells out only because IT needs system python's
    UnityPy/Pillow, which is not this server's toolchain; `park_treasure.py`
    shares ours. Returns None if it cannot be loaded."""
    try:
        spec = importlib.util.spec_from_file_location(
            "park_treasure", PARK_PROXY_DIR / "park_treasure.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 — any failure means "no live read available"
        return None


_INVENTORY_PATH = PROXY_DIR / "dumps" / "inventory.json"


def _account_currencies() -> list:
    """The upgrade currencies from the account's item capture (`inventory.json`'s
    decoded `user_item_list`). Names and item_ids checked against the game's
    LangItem_Eng master (`la-item-<id>-name`):
    - Beads / Crystals per attribute (Cute/Pure/Happy).
    - The SELECTABLE bundles are their OWN items, matching the game's inventory:
      `attribute_1` = "Beads Bundle" S/M/L, `attribute_2` = "A Jar of Crystals"
      S/M/L (the `_1/_2` picks beads-vs-crystals; the COLOUR is chosen on open,
      so it's not an attribute), and the -10/-100/-300 suffix is the size and the
      count it grants. Their icons don't reverse-map from the item_id, so each
      carries an explicit octo asset `icon` (the bottle art). Zero-held bundles
      are omitted, exactly as the game hides an item you don't hold.
    Every row is tagged with a `group` (beads/crystals/bundle/other) for the view.
    `[]`-free — an absent capture just yields zeros, a first-class 'refresh' state."""
    inv = _read_json(_INVENTORY_PATH) or {}
    items = inv.get("items") or {}

    def q(item_id: str) -> int:
        try:
            return int(items.get(item_id, 0) or 0)
        except (TypeError, ValueError):
            return 0

    rows: list[dict] = []
    attrs = (("attribute_1", "Cute"), ("attribute_2", "Pure"), ("attribute_3", "Happy"))
    for key, label in attrs:  # grade -1 = Beads
        iid = f"item-card_limit_break-{key}-1"
        rows.append({"name": f"{label} Beads", "held": q(iid), "note": "Member Training",
                     "item_id": iid, "group": "beads"})
    for key, label in attrs:  # grade -2 = Crystals
        iid = f"item-card_limit_break-{key}-2"
        rows.append({"name": f"{label} Crystals", "held": q(iid), "note": "Member Training",
                     "item_id": iid, "group": "crystals"})

    # Selectable bundles. box asset is `...-box-<01 beads|02 crystals>-<01 S|02 M|03 L>`.
    sizes = (("10", "S", 10, "01"), ("100", "M", 100, "02"), ("300", "L", 300, "03"))
    bundles = (("attribute_1", "Beads Bundle", "beads", "01"),
               ("attribute_2", "A Jar of Crystals", "crystals", "02"))
    for attr_key, base_name, grants, box_attr in bundles:
        for suffix, size, grant, box_tier in sizes:
            iid = f"item-selectable_reward_box-item-card_limit_break-{attr_key}-{suffix}"
            held = q(iid)
            if not held:
                continue  # the game hides a bundle you don't hold
            rows.append({
                "name": f"{base_name} {size}", "held": held,
                "note": f"grants {grant} {grants}", "item_id": iid, "group": "bundle",
                "icon": f"img_item_thumb_selectable-limit-break-attribute-box-{box_attr}-{box_tier}",
                "grant_each": grant, "grant_kind": grants,
            })

    rows.append({"name": "Hololium", "held": q("item-card_limit_break-cmn-sr"),
                 "note": "Member Training", "item_id": "item-card_limit_break-cmn-sr", "group": "other"})
    rows.append({"name": "Lesson Pt", "held": q("item-card_level_up"),
                 "note": "level up a Member", "item_id": "item-card_level_up", "group": "other"})
    rows.append({"name": "Hologold", "held": q("item-gold"),
                 "note": "various", "item_id": "item-gold", "group": "other"})
    return rows


# The game's own English item names (`la-item-<id>-name` / `-description`), as
# a one-off export of the community tool holodori.best leaves them. Read-only; absent =
# names fall back to None.
_LANG_ITEM_PATH = PROXY_DIR / "dumps" / "holodori" / "main_LangItem_Eng.json"
_SNAPSHOT_PATH = PROXY_DIR / "dumps" / "account_snapshot_main.json"
# The same table from the live Master/Get dump (`hololive-dreams-master`, which
# refresh_all re-dumps): the holodori.best copy is a one-off pull and lacks every item the
# game added since (event badges, park materials, minigame coins would show "Unnamed").
_LIVE_LANG_ITEM_PATH = (Path(os.environ.get("HB_MASTER_REPO") or REPO_ROOT.parent / "hololive-dreams-master")
                        / "data" / "tables" / "LangItem_Eng.json")


def _item_lang() -> dict:
    """`{lang id: English text}` for items: the holodori.best export, then the live master
    dump over it (newer). Rows: holodori `{data: {id, text}}`, live `{id, data: {"1000": text}}`."""
    lang: dict = {}
    for row in _read_json(_LANG_ITEM_PATH) or []:
        data = row.get("data") if isinstance(row, dict) else None
        if isinstance(data, dict) and data.get("id"):
            lang[data["id"]] = data.get("text")
    for row in (_read_json(_LIVE_LANG_ITEM_PATH) or {}).get("rows") or []:
        data = row.get("data") if isinstance(row, dict) else None
        if isinstance(data, dict) and row.get("id") and data.get("1000"):
            lang[row["id"]] = data["1000"]
    return lang


def _account_items() -> dict:
    """EVERY item the account holds (`inventory.json`'s `items`, held > 0), named by
    the game's LangItem_Eng master, plus each holomem's unspent board points
    (`account_snapshot_main.json`'s `board_points`). For the redesigned Inventory page;
    `_account_currencies` stays as it is for the old view. A name the master lacks is
    `None` — the page shows the raw id rather than a guessed name."""
    inv = _read_json(_INVENTORY_PATH) or {}
    lang = _item_lang()
    items = []
    for item_id, held in (inv.get("items") or {}).items():
        try:
            held = int(held or 0)
        except (TypeError, ValueError):
            continue
        if held <= 0:
            continue  # the game hides an item you don't hold
        items.append({"item_id": item_id, "held": held,
                      "name": lang.get(f"la-{item_id}-name"),
                      "description": lang.get(f"la-{item_id}-description")})
    names = {}
    for card in (_read_json(CARDS_PATH) or {}).get("cards", []):
        parts = str(card.get("id", "")).split("-")
        if len(parts) > 1 and card.get("member"):
            names.setdefault("chr-" + parts[1], card["member"])
    points = []
    for row in (_read_json(_SNAPSHOT_PATH) or {}).get("board_points") or []:
        pid = str(row.get("point_id", ""))
        chr_id = pid.split("skill_tree_point-", 1)[-1] if pid.startswith("skill_tree_point-") else None
        if chr_id:
            points.append({"chr": chr_id, "name": names.get(chr_id),
                           "unspent": int(row.get("quantity") or 0)})
    return {"items": items, "holomem_points": points,
            "inventory_captured_at": inv.get("_captured_at"),
            "inventory_source": inv.get("_source")}


def _load_precompute():
    """`precompute_optimisations.py` under `tools/recon/proxy/`, imported BY PATH
    the same way as `park_treasure.py` and `build_board.py`. Its module-level code
    is import-safe (it only imports `board_export`/`socket_optimise`/`tree_optimise`
    and defines functions), and it runs under proxyenv — this server's interpreter.
    We need its `team_board_plan`, which plans the board for one EXACT team (the
    Solve's winner) via `tree_optimise --for-team`. Returns None if unloadable."""
    try:
        spec = importlib.util.spec_from_file_location(
            "precompute_optimisations", PROXY_DIR / "precompute_optimisations.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 — any failure means "no on-demand plan"
        return None


def _account_credential_present() -> bool:
    """The account's credential is where `account_snapshot.load_credential` reads it: the
    desktop app names the game's own file in HB_CREDENTIAL_FILE (app_settings.env), the
    publisher's PC has state/account_credential.json. Checking only the second would make the
    Park page fail in the app, whose account reads every other page fine."""
    env = os.environ.get("HB_CREDENTIAL_FILE", "")
    return bool(env and Path(env).is_file()) or ACCOUNT_CREDENTIAL_PATH.is_file()


def _park_live_state(account: str = "main") -> dict:
    """Today's per-area pick + opened treasures, from a fresh `User/Get`.

    Every failure becomes a `_ParkUnavailable` carrying a reason and the command
    that fixes it. The credential is checked HERE rather than left to
    `account_snapshot.load_credential`, whose `sys.exit()` on a missing file
    would escape as a 500 instead of a first-class state."""
    if not _account_credential_present():
        raise _ParkUnavailable(
            "the live park read needs the account credential: in the app, Settings -> "
            "the game's credential file; on the publisher's PC, state/"
            f"{ACCOUNT_CREDENTIAL_PATH.name} (account_snapshot.py --import-from-capture)."
        )
    park = _load_park_treasure()
    if park is None:
        raise _ParkUnavailable(
            "could not import tools/recon/proxy/park_treasure.py (it needs the "
            "proxyenv interpreter this server runs under)."
        )
    try:
        userdata = park.userdata_live(account)
    except _ParkUnavailable:
        raise
    except SystemExit as exc:  # login/master helpers bail via sys.exit
        raise _ParkUnavailable(f"live park read aborted: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — a network/login failure is a STATE
        raise _ParkUnavailable(
            f"live login failed: {type(exc).__name__}: {exc}"
        ) from exc
    return park.park_state(userdata)


def _cached_park_live_state(account: str = "main") -> dict:
    """`_park_live_state` behind the TTL cache + lock (see `_PARK_TTL_SECONDS`).

    The login happens INSIDE the lock so concurrent requests coalesce into one.
    A `_ParkUnavailable` is NOT cached, so fixing the credential and retrying
    works on the next request rather than waiting out the TTL."""
    now = time.monotonic()
    with _PARK_LOCK:
        cached = _PARK_CACHE.get(account)
        if cached is not None and now - cached[0] < _PARK_TTL_SECONDS:
            return cached[1]
        state = _park_live_state(account)
        _PARK_CACHE[account] = (time.monotonic(), state)
        return state


def _park_export() -> dict | None:
    """The offline export (`park_spots.json`), or None when it is absent.

    A separate seam from the live read on purpose: the export is what a fresh
    checkout lacks, and the test suite stubs this to develop the join against a
    fixture without touching disk."""
    return _read_json(PARK_SPOTS_PATH)


def _resolve_spot(
    mapping: str, mapping_to_spot: Mapping[str, Any], spots: Mapping[str, Any]
) -> tuple[Any, Any, Any]:
    """`(area, spot_name, {x,y})` for a mapping id.

    ⚠ ALWAYS via `mapping_to_spot`, never by synthesising a spot name from the
    mapping id: the mapping suffix is 3-digit (`...a01-082`) while the spot name
    is 2-digit (`reward_random_82`), and areas spell differently inside the two
    (`area-a02-casino` vs `...a02_casino-033`). `(None, None, None)` when the
    mapping is unknown; `(area, spot, None)` when the spot carries no pixel --
    both are REPORTED by the caller, never dropped."""
    m = mapping_to_spot.get(mapping)
    if not m:
        return None, None, None
    area = m.get("area")
    spot = m.get("spot")
    px = spots.get(spot) if spot else None
    return area, spot, px


def _join_park(export: Mapping[str, Any], state: Mapping[str, Any]) -> dict:
    """The export's geometry joined to today's live pick.

    ⚠ Each area carries ITS OWN `rolled_at` (`last_refreshed_time`), never a
    park-wide one. A capture caught the casino a full day behind the
    other two, so a single broadcast timestamp would be a lie on exactly the day
    it matters. An area with no live row gets `rolled_at: null` -- it is NOT
    filled from another area.

    ⚠ A picked symbol that cannot be placed is REPORTED in the area's
    `unresolved` list (and counted at the top), never dropped: a silently
    missing marker is a treasure the player walks past. Every picked id is drawn
    from the candidate pool `mapping_to_spot` holds, so a miss should not happen
    -- this path is the guard that makes it loud if that assumption ever breaks.

    ⚠ Three of the fixed chests (`-008/-018/-014`, in a01-house/-restaurant/
    -theater; 70 chests in all since Soda Pop Lagoon) have NO map asset and no `areas` entry, so they cannot be placed.
    They are skipped explicitly and counted in `unplaceable_treasures` rather
    than falling out of the join unnoticed."""
    export_areas = export.get("areas") or {}
    mapping_to_spot = export.get("mapping_to_spot") or {}
    all_candidates = export.get("candidates") or {}
    all_treasures = export.get("treasures") or {}
    random_state = state.get("random") or {}
    opened = set(state.get("opened_treasures") or [])

    areas_out: list[dict[str, Any]] = []
    unresolved_total = 0
    for area_id in sorted(export_areas):
        ainfo = export_areas[area_id] or {}
        spots = ainfo.get("spots") or {}
        live = random_state.get(area_id)

        # Straight from THIS area's own row -- None when the area has no live
        # state (not entered since the reset, or absent from the blob).
        rolled_at = live.get("last_refreshed_time") if live else None
        picked_ids = list(live.get("picked") or []) if live else []
        collected_ids = set(live.get("collected") or []) if live else set()

        picked_out: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        for mapping in picked_ids:
            m_area, spot, px = _resolve_spot(mapping, mapping_to_spot, spots)
            if px is None:
                unresolved.append({
                    "mapping": mapping,
                    "reason": (
                        "no entry in the export's mapping_to_spot"
                        if m_area is None
                        else f"spot {spot!r} has no pixel in {area_id}"
                    ),
                })
                continue
            picked_out.append({
                "mapping": mapping,
                "spot": spot,
                "x": px.get("x"),
                "y": px.get("y"),
                # `collected` is per-symbol and comes from THIS area's
                # collected list, so it is a strict subset of `picked`.
                "collected": mapping in collected_ids,
            })
        unresolved_total += len(unresolved)

        candidates_out: list[dict[str, Any]] = []
        for mapping in all_candidates.get(area_id) or []:
            _a, spot, px = _resolve_spot(mapping, mapping_to_spot, spots)
            if px is not None:
                candidates_out.append(
                    {"spot": spot, "x": px.get("x"), "y": px.get("y")}
                )

        treasures_out: list[dict[str, Any]] = []
        for tid, tinfo in sorted(all_treasures.items()):
            if (tinfo or {}).get("area") != area_id:
                continue
            spot = (tinfo or {}).get("spot")
            px = spots.get(spot) if spot else None
            treasures_out.append({
                "id": tid,
                "spot": spot,
                "x": (px or {}).get("x"),
                "y": (px or {}).get("y"),
                "opened": tid in opened,
            })

        areas_out.append({
            "id": area_id,
            "name": ainfo.get("name") or area_id,
            "image": f"/park/{area_id}.png",
            "width": ainfo.get("width"),
            "height": ainfo.get("height"),
            "rolled_at": rolled_at,
            "picked": picked_out,
            "unresolved": unresolved,
            "candidates": candidates_out,
            "treasures": treasures_out,
        })

    # The three chests in areas with no map (see docstring) -- counted so a
    # treasure list of 39 is explained rather than mysteriously short.
    unplaceable_treasures = sum(
        1 for t in all_treasures.values()
        if (t or {}).get("area") not in export_areas
    )
    # A live area the export does not know about must not vanish silently.
    unmapped_areas = sorted(a for a in random_state if a not in export_areas)

    return {
        "available": True,
        "note": export.get("note") or "",
        "generated_at": export.get("generated_at"),
        "areas": areas_out,
        "unresolved_count": unresolved_total,
        "unplaceable_treasures": unplaceable_treasures,
        "unmapped_areas": unmapped_areas,
    }



def _board_plan_summary(bt, board_state: dict, *, budget_kind: str, budget_for,
                        current_by_chr: dict, node_prices: dict | None,
                        board_off: int, developed: int, metric: str) -> dict:
    """The per-row board section: ONE board per row —
    the one it was scored on (`board_state`) — diffed against the account's
    CURRENT board from the same board_export the develop budgeted with. `buy`
    = developed − current, `sell` = current − developed (a respec proposal:
    only meaningful under REAL points; a flat/full-tree develop is "adds
    only", so `sell` is empty there and `develop_only` says so), `keep` = both.
    `points_used` = the plan's own spend, `points_max` = the budget the plan
    was costed against, named by kind — never "453 / 97". Materials from the
    node price table for the buys; absent when the master is unreadable."""
    per_character = []
    materials: dict[str, int] = {}
    develop_only = budget_kind != "real"
    for card in (bt.team.leader, *bt.team.members):
        chr_id = "chr-" + card.id.split("-")[1]
        if chr_id in {c["chr"] for c in per_character}:
            continue
        cplan = bt.plan.per_character.get(card.id)
        state = board_state.get(chr_id) or {}
        planned = set(state.get("nodes") or (sorted(cplan.selected) if cplan else []))
        cur = current_by_chr.get(chr_id) or {}
        owned = set(cur.get("owned") or [])
        buy = sorted(planned - owned)
        sell = [] if develop_only else sorted(owned - planned)
        keep = sorted(planned & owned)
        if node_prices:
            for nid in buy:
                for item, qty in (node_prices.get(nid) or {}).items():
                    if item != "points":
                        materials[item] = materials.get(item, 0) + int(qty)
        sockets = [{"slot": slot, "card_id": cid}
                   for slot, cid in sorted((state.get("slots") or {}).items())]
        per_character.append({
            "chr": chr_id, "name": cur.get("name") or card.member,
            "buy": buy, "sell": sell, "keep": keep, "sockets": sockets,
            "points_used": int(cplan.spent) if cplan is not None else 0,
            "points_max": int(budget_for(card)),
            "current_owned": len(owned),
        })
    return {
        "metric": metric, "board_off": board_off, "developed": developed,
        "gain_pct": round(100.0 * (developed - board_off) / board_off, 1) if board_off else 0.0,
        "budget_kind": "full_tree" if budget_kind == "flat" and all(
            c["points_max"] == 453 for c in per_character) else budget_kind,
        "develop_only": develop_only,
        "per_character": per_character,
        "materials": materials if node_prices else None,
    }


def _internal(cfg, optimised) -> dict:
    """The pipeline's live objects for an in-process caller (`/api/run` builds
    the FINAL rosters' breakdown/terms/explain from them); the HTTP wrapper
    strips this key — it is not JSON."""
    return {"cfg": cfg,
            "by_roster": {(bt.team.leader.id, frozenset(m.id for m in bt.team.members)): bt
                          for bt in optimised}}


class _HttpError(Exception):
    """A typed HTTP answer raised from inside `develop_pipeline` (a bad
    request, a missing dump); the route wrapper turns it into JSON."""

    def __init__(self, code: int, obj: dict) -> None:
        super().__init__(obj.get("error") if isinstance(obj, dict) else str(obj))
        self.code = code
        self.obj = obj


# Which deck power the per-song numbers are on, as a request knob beside
# `uptime_model` and `platform`. The DEFAULT (`song_absolute_deck_power=True`)
# is the deck screen's own Overall Power, the quantity the game multiplies.
#
# `"modelled"` asks for the stat-pool basis, and it exists for ONE reason
# worth stating: the parity references against the community tool
# holodori.best are measured on it. That comparison is one of the few
# genuinely NON-CIRCULAR checks this project has — a third-party
# implementation, not this project's own output — so it must keep asking the
# question it was calibrated for. Re-baselining it against the deck-power
# number would turn an independent check into a self-recording.
DECK_POWER_BASES = {"server": True, "modelled": False}


def _auto_premise(cfg, body: Mapping) -> dict:
    """`{}` or the BOTH-HALVES AUTO premise, as kwargs for `dataclasses.replace`.

    `"auto": true` in the request body ranks for an AUTO run. It sets the AUTO
    judgement column AND `run_state.combo = 0`, because (seen in a live trace of
    the scorer) the combo never accumulates on AUTO, so
    a `combo >= N` magnitude override never fires. Setting only the column would
    keep granting magnitudes the run cannot produce and over-value exactly the
    cards that carry one — half a premise is a number for a run that does not
    exist, so the two are set together here and nowhere else.
    """
    if body.get("auto") is not True:
        return {}
    return {
        "song_auto": True,
        "run_state": dataclasses.replace(cfg.run_state, combo=0),
    }


def _deck_power_basis(body: Mapping) -> dict:
    """`{}` (inherit the default) or `{"song_absolute_deck_power": bool}`."""
    choice = str(body.get("deck_power") or "").lower()
    if choice in DECK_POWER_BASES:
        return {"song_absolute_deck_power": DECK_POWER_BASES[choice]}
    return {}


def develop_pipeline(body: Mapping, *, art_dir: Path, emit=None, cancel=None) -> dict:
    """The develop + (refine) + per-song/across/Unit-Score re-rank of a set of
    teams — the body of `POST /api/optimize-rerank`, extracted so `/api/run`
    can drive it as ONE stage of a single job with progress and cancel.
    `body` is the request document; the wrapper
    route is byte-compatible (`test_optimize_rerank_route.py`).

    `emit(event)` receives `{"type": "progress"|"stage"|"fact"|"developed"|
    "notice", ...}` dicts as work proceeds; `cancel` (a threading.Event) is
    checked between teams and between stages and raises `_Cancelled`. Bad
    input raises `_HttpError(code, obj)`; a missing dump `_HttpError(503, ..)`.
    Returns the response document (`ranking`, `cards`, `sorted_by`, ...)."""
    def _emit(ev: dict) -> None:
        if emit is not None:
            emit(ev)

    def _check_cancel() -> None:
        if cancel is not None and cancel.is_set():
            raise _Cancelled()

    # leader_diverse: ignore the posted teams and instead GENERATE the best
    # team for every candidate leader (one solver search each), breaking the
    # Unit-Score monoculture — stage 1's top-N is ~all one leader, so the
    # across-optimal team under a different leader is never generated. This is
    # what lets a board/across-dependent team (e.g. a Shirogane Noel outfit)
    # surface instead of a hundred variants of the single top-Unit-Score leader.
    leader_diverse = bool(body.get("leader_diverse"))
    teams_in = body.get("teams") or []
    if not leader_diverse and (not isinstance(teams_in, list) or not teams_in):
        raise _HttpError(400, {"error": (
            "need a non-empty 'teams' list of [leader, m1..m5] rosters")})
    basis_name = _parse_basis(body.get("basis"))
    # Level/bloom what-if, PARSED THE SAME WAY the /solve handler does — the
    # develop re-score MUST use the same card stats the ranking above was solved
    # with, or it re-ranks maxed teams on their real (often level-1) stats and
    # the developed scores come out below the board-off ones. Only bite on the
    # account basis (maxed already answers at max).
    assume_level = _parse_assume(body, "assume_level", 1, 80)
    assume_bloom = _parse_assume(body, "assume_bloom", 0, 6)
    # ACTIVE-uptime model, same as /solve: the develop re-score MUST use the
    # score model the ranking was solved with, or "Deck screen" (steady) above
    # gets re-ranked with the in-run (finite) model below — a ~15-18% shift on
    # the ACTIVE contribution, silently.
    uptime_model = str(body.get("uptime_model") or "").lower() or None
    if uptime_model not in ACTIVE_UPTIME_MODELS:
        uptime_model = None
    # Same platform (mobile | pc) the ranking was solved with, for the same reason.
    platform = _parse_platform(body)
    # Candidate universe (`pool`: "all" | "5star"), the same lever /solve
    # takes: it bounds the leader_diverse generation AND the refine's swap
    # candidates, so the response's `certificate_basis.universe` names the
    # universe the pipeline actually searched (a 5★-only certificate is
    # only comparable to a 5★-only run).
    pool_filter = str(body.get("pool") or "all").lower()
    if pool_filter not in ("all", "5star"):
        pool_filter = "all"
    # Default: each character's REAL earned points (board_export points.granted),
    # the same source the board plan respects. A flat `budget` overrides with a
    # per-character cap (0-453) — the "if fully levelled" ceiling question.
    flat_budget = None
    if body.get("budget") is not None:
        try:
            flat_budget = max(0, min(453, int(body.get("budget"))))
        except (TypeError, ValueError):
            flat_budget = None
    # The maxed-account basis develops at the full tree by default: "real
    # granted points" are the account's, and a maxed account has them all.
    if flat_budget is None and basis_name == MAXED_ACCOUNT_BASIS:
        flat_budget = MAXED_ACCOUNT_BUDGET
    # Song mode: after developing every team's board, re-order by the
    # PER-SONG score against that developed board (not the deck-panel Unit
    # Score), so the ranking answers "which team scores highest on THIS chart
    # once its board is built". The board is developed for Unit Score (fast
    # Engine-A) as a proxy; the exact per-song-optimal board stays the
    # on-demand /api/boardplan solver.
    # Member swap-refine (song mode only): hill-climb each top team's five
    # member slots by the PER-SONG developed score, leader fixed — the beam
    # picks members by Unit Score and the per-song winner can differ. Opt-in:
    # it costs ~1-3 s per refined team on a desktop PC.
    # `refine`: false | true (shortlist 8, 2 rounds) | "wide" (shortlist 20,
    # 3 rounds — for when the incumbent-board prefilter can miss a swap whose
    # value is in its OWN developed board or in a de-synchronised cooldown).
    refine_raw = body.get("refine", False)
    refine = refine_raw is True or refine_raw == "wide"
    refine_kw = ({"shortlist": 20, "max_rounds": 8} if refine_raw == "wide"
                 else {})
    # Refine LEAF. `plan_cache` (default):
    # the refine's batch scoring runs on the FIXED per-(character, role,
    # budget, singer-context) plans seeded from the run's own #1 developed
    # team — the v2 objective, a pure function of the roster, ~10x cheaper
    # per leaf than developing every candidate's board (~3.7 ms vs ~40 ms).
    # `develop`: the legacy leaf — every candidate develops its
    # own board through optimize_and_rerank.
    # The leaf changes which swaps are tried; the numbers shown come from
    # `objective_leaf` below (under its default the rows and the refine share
    # ONE plan cache, so the refine's accept metric IS the ranking's).
    refine_leaf = str(body.get("refine_leaf") or "plan_cache").lower()
    if refine_leaf not in ("plan_cache", "develop"):
        refine_leaf = "plan_cache"
    # OBJECTIVE LEAF. The
    # number a song / across row is RANKED and SHOWN by. `plan_cache`
    # (default): the v2 objective S(T, C) — the team scored on the FIXED
    # per-(character, role, budget, singer-context) plans of ONE PlanCache
    # seeded for the whole run (the per-leader table's seed when this run's
    # hash + basis match it, else the members-first enumeration's, else the
    # run's #1 developed team), the same leaf the refine and the EXACT
    # certificates score on. The per-team greedy re-develop
    # (`optimize_and_rerank` per team) survives on every row as the LABELLED
    # what-if (`song_est_develop` / `across_score_develop`, its board under
    # `board_state_develop`). `develop`: the legacy rows — headline, board and
    # order from the per-team develop. The develop leaf can exceed the
    # certified fixed-plan float (by 0.2-0.7 % on 8 of 66 certified argmax
    # rows), which the certificate guard rightly refuses as "row > EXACT".
    objective_leaf = str(body.get("objective_leaf") or "plan_cache").lower()
    if objective_leaf not in ("plan_cache", "develop"):
        objective_leaf = "plan_cache"
    # SEED ROSTERS (seed_rosters.py): known-strong rosters UNIONED with the
    # candidates before the develop, so the beam's recall failure (it can
    # miss the best known general roster) cannot hide a
    # roster the run's own evaluator would rank first. `seeds`: "all" (every
    # source) | "file" (only tools/web/seed_rosters.json) | false. Default:
    # "all" when the run generates candidates itself (leader_diverse) or
    # refines, else off — a plain "develop these posted rosters" call keeps
    # its plain contract; the client asks for "file" there.
    seeds_raw = body.get("seeds")
    if seeds_raw is None:
        seeds_scope = "all" if (leader_diverse or refine) else None
    elif seeds_raw is True:
        seeds_scope = "all"
    elif isinstance(seeds_raw, str) and seeds_raw.lower() in ("all", "file"):
        seeds_scope = seeds_raw.lower()
    else:
        seeds_scope = None
    # MEMBERS-FIRST SEEDS (members_first_index.py): on a song-mode run that
    # generates candidates (leader_diverse) or refines, on the 5★ maxed
    # universe, with a cached bare-proxy enumeration on THIS run's
    # inputs_hash — THIS song's own (mode "own"), else any other song's as a
    # REFERENCE (mode "transfer": the member-set ranking transfers across
    # charts: 6/6 EXACT rows of m0139 from the m0334 enumeration at K=500)
    # — every leader's members-first
    # best roster (the best of the top-K / gate-satisfying / own-holomem
    # tiers re-scored under the real leader on THIS song) enters the develop
    # set as a seed. `members_first_k`: K (absent -> 2000 own / 500 transfer,
    # the measured values; 0 disables). The response's `members_first` says
    # what happened, which mode, which sources, and the measured recall on
    # file — the rows stay HEURISTIC unless the certificate guard says EXACT.
    members_first_k: int | None
    if "members_first_k" in body and body.get("members_first_k") is not None:
        try:
            members_first_k = max(0, min(100000, int(body.get("members_first_k"))))
        except (TypeError, ValueError):
            members_first_k = None
    else:
        members_first_k = None
    # Constraints a seed must respect (a seed that violates one is dropped,
    # never forced): Ban / Must-include / leader pin, as `/solve` takes them.
    def _id_list(key):
        raw = body.get(key)
        return tuple(str(x).strip() for x in raw if str(x).strip())             if isinstance(raw, list) else ()
    seed_exclude, seed_require, seed_leader = (
        _id_list("exclude"), _id_list("require"), _id_list("leader"))
    song = str(body.get("song") or "").strip()
    difficulty = _parse_difficulty(body, song)
    charts_file = _charts_path(difficulty)
    chart = None
    if song:
        library = load_charts(str(charts_file))
        chart = library.get(song, difficulty) if library is not None else None
        if chart is None:
            raise _HttpError(400, {"error": (
                f"song {song!r}: no {difficulty} chart in {charts_file.name} "
                f"(build it with tools/recon/proxy/build_charts.py)")})
    # Across-songs mode: re-order by the MEAN per-song score over EVERY chart,
    # scored against each team's developed board — the same quantity the
    # board-off across ranking uses, so develop answers "best team across all
    # songs once boards are built" instead of falling back to Unit Score.
    across = bool(body.get("across")) and not song
    all_charts = []
    if across:
        library = load_charts(str(charts_file))
        all_charts = [c for c in (
            (library.get(m, difficulty) for m in library.music_ids) if library else ())
            if c is not None]
        if not all_charts:
            raise _HttpError(400, {"error": (
                f"across-songs develop needs the chart library ({charts_file.name}) "
                f"on disk; build it with tools/recon/proxy/build_charts.py")})
    skilltree = _read_json(SKILLTREE_PATH)
    node_master = _read_json(NODE_MASTER_PATH)
    if not skilltree or not node_master:
        raise _HttpError(503, {"error": (
            "board optimisation needs skilltree.json + "
            "master_SkillTreeNode.json under the gitignored dumps/; run "
            "tools/recon/proxy/skilltree.py")})
    precompute = _load_precompute()
    if precompute is None or not hasattr(precompute, "board_export"):
        raise _HttpError(503, {"error": (
            "could not import board_export via precompute_optimisations "
            "(needs the proxyenv interpreter this server runs under).")})
    effects = node_effect_ids_from_master(node_master)

    try:
        cards, owned, cfg, _prov = load_inputs(CARDS_PATH, OWNED_PATH, False)
        if uptime_model in ACTIVE_UPTIME_MODELS:
            cfg = dataclasses.replace(cfg, active_uptime_model=uptime_model)
        cfg = dataclasses.replace(cfg, platform=platform, **_deck_power_basis(body))
        cfg = dataclasses.replace(cfg, **_auto_premise(cfg, body))
        # The basis AFTER uptime/platform: the maxed-account bench is
        # linearised at (and cached by) the resolved config.
        try:
            cards, collection_basis, cfg, ma_scenario = _apply_basis(
                cards, owned, cfg, basis_name,
                assume_level=assume_level, assume_bloom=assume_bloom,
                precompute=precompute)
        except MaxedAccountUnavailable as exc:
            raise _HttpError(503, {"error": str(exc)})
        # Real per-character board budgets (points.granted), computed once for
        # every character; used unless a flat cap was given.
        scenario = _SCENARIO_BY_BASIS.get(basis_name, "account")
        export_doc = precompute.board_export.build(gain=False, pool=1, scenario=scenario)
        granted = {
            c["chr"]: (c.get("points") or {}).get("granted")
            for c in export_doc.get("characters", [])
        }
        # the CURRENT board per character (owned nodes, name, granted) — the
        # baseline every row's `board_plan_summary` diffs its developed board
        # against; same instant, same export
        current_by_chr = {
            c["chr"]: {"name": c.get("name") or c.get("member") or c["chr"],
                       "owned": sorted(n["id"] for n in c.get("nodes", [])
                                       if n.get("state") == "owned"),
                       "granted": (c.get("points") or {}).get("granted")}
            for c in export_doc.get("characters", [])
        }
        if ma_scenario is not None:
            # maxed account: the baseline is the scenario's bench (every node
            # owned, 453 granted), not the account's export
            current_by_chr = ma_scenario.current_by_chr(
                {"chr-" + c.id.split("-")[1]: c.member for c in cards})
            granted = {k: MAXED_ACCOUNT_BUDGET for k in current_by_chr}
        try:
            node_prices = precompute.to._prices()
        except Exception:  # noqa: BLE001 — materials stay unknown, never fatal
            node_prices = None
        by_id = {c.id: c for c in cards}

        built: list[Team] = []
        if leader_diverse:
            _emit({"type": "stage", "stage": "leaders", "state": "active",
                   "detail": "one beam, best team per leader"})

            def _leaders_progress(evaluated: int) -> None:
                _check_cancel()
                _emit({"type": "progress", "stage": "leaders", "evaluated": evaluated})

            # ONE beam, then keep the BEST TEAM PER LEADER (per_leader_best) —
            # the global top-N is a monoculture (146/150 one leader observed),
            # but the beam already explores every leader's own best team; this
            # just stops the single strongest outfit from burying them. Same
            # cost as a normal solve (~one beam), vs one search per leader.
            # optimize_and_rerank + the per-song/across re-score below then ranks
            # these ~50 diverse teams by the target metric.
            owned_ids = {c.id for c in owned}
            pool_cards = tuple(cards if _cards_maxed(basis_name)
                               else [c for c in cards if c.id in owned_ids])
            if pool_filter == "5star":
                pool_cards = tuple(c for c in pool_cards if _is_five_star(c))
            # Ask for EVERY leader's best team, then keep the top
            # LEADER_DIVERSE_POOL by board-off score PLUS every 5-star
            # leader. The board-off cut alone drops 19 of the 66 5-star
            # leaders (Towa, Watame, Calli summer, Pekora, Kronii, ...)
            # below thirteen 4-star "Dreamy Drop" leaders (maxed pool),
            # and the develop that follows can reorder them — so the cut
            # must not happen before it.
            # The leader options, inside the generation: a team that breaks
            # them is never built (filtering afterwards would drop a leader whose
            # best team happens to break them, rather than find her best legal one)
            _lm = str(body.get("leader_member") or "").strip()
            _lc = str(body.get("leader_card") or "").strip()
            _no_overlap = bool(body.get("no_leader_overlap"))

            def _leader_constraint(members, leader, _lm=_lm, _lc=_lc, _no=_no_overlap):
                if _lm and leader.member != _lm:
                    return False
                if _lc and leader.id != _lc:
                    return False
                return not (_no and leader.member in {m.member for m in members})

            res = solve_heuristic_labelled(
                pool_cards, cfg, top_n=len(pool_cards), per_leader_best=True,
                progress=_leaders_progress,
                constraint=_leader_constraint if (_lm or _lc or _no_overlap) else None)
            keep = list(res.teams[:LEADER_DIVERSE_POOL])
            keep += [rt for rt in res.teams[LEADER_DIVERSE_POOL:]
                     if by_id[rt.leader_id].rarity.startswith("5")]
            for rt in keep:
                try:
                    built.append(Team(
                        leader=by_id[rt.leader_id],
                        members=tuple(by_id[m] for m in rt.member_ids)))
                except (TeamError, KeyError):
                    continue
        else:
            for roster in teams_in:
                ids = [str(c) for c in (roster or []) if c]
                if len(ids) != MEMBER_COUNT + 1:
                    raise _HttpError(400, {"error": (
                        f"each team needs {MEMBER_COUNT + 1} card ids "
                        f"(leader + {MEMBER_COUNT}); got {len(ids)}")})
                missing = [c for c in ids if c not in by_id]
                if missing:
                    raise _HttpError(400, {"error": f"unknown card id(s): {missing}"})
                try:
                    built.append(Team(
                        leader=by_id[ids[0]],
                        members=tuple(by_id[c] for c in ids[1:]),
                    ))
                except TeamError as exc:
                    raise _HttpError(400, {"error": f"invalid team {ids}: {exc}"})
        # SEEDS: union the known-strong rosters (all six cards in THIS run's
        # pool, constraints honoured, deduped by (leader, member set)) with the
        # beam's / posted teams. They are developed and scored exactly like any
        # other candidate; `provenance` only says where the roster came from.
        provenance_by_key: dict[tuple[str, frozenset[str]], str] = {}
        if seeds_scope is not None:
            _owned_ids = {c.id for c in owned}
            _seed_pool = [c for c in cards
                          if _cards_maxed(basis_name) or c.id in _owned_ids]
            if pool_filter == "5star":
                _seed_pool = [c for c in _seed_pool if _is_five_star(c)]
            _taken = [(t.leader.id, frozenset(m.id for m in t.members)) for t in built]
            _seeds = seed_rosters.filter_seeds(
                seed_rosters.default_index().seeds(seeds_scope),
                pool_ids=(c.id for c in _seed_pool),
                exclude=seed_exclude, require=seed_require, leader=seed_leader,
                taken=_taken)
            for _team, _seed in seed_rosters.build_seed_teams(
                    _seeds, by_id, lambda l, ms: Team(leader=l, members=ms)):
                built.append(_team)
                provenance_by_key[_seed.key] = _seed.provenance
            if provenance_by_key:
                _emit({"type": "fact", "stage": "develop",
                       "text": f"{len(provenance_by_key)} seed roster(s) added"})
        if not built:
            raise _HttpError(400, {"error": "no valid teams to develop"})
        tree_cache: dict[str, object] = {}

        def tree_for(card):
            model = model_for_character(skilltree, "chr-" + card.id.split("-")[1])
            if model is None:
                return None
            if model not in tree_cache:
                tree_cache[model] = skill_tree_from_dumps(
                    skilltree, node_master, model)
            return tree_cache[model]

        def budget_for(card):
            if flat_budget is not None:
                return flat_budget
            g = granted.get("chr-" + card.id.split("-")[1])
            return int(g) if g else 0

        # REAL Connect cards (owned, at their resolved level) so the develop
        # re-score places actual sockets — one card, one socket, team-wide —
        # instead of the idealised best-shape-everywhere multiplier (measured
        # ~+4-5 pp optimistic vs the real assignment). Non-fatal: without the
        # recon data the idealised model still stands.
        try:
            # an assumed bloom lifts the Connect levels too (bloom 5 = level 2)
            connect_cards = precompute.connect_cards_for(scenario, assume_bloom=assume_bloom)
        except Exception:  # noqa: BLE001 — sockets stay idealised
            connect_cards = None

        # The account's CURRENT bench boards (board.json, connect resolved):
        # their GREENs are account-global, so every developed team's score
        # carries them — the Oracle scorer always counted them and the
        # develop re-score ran ~4 % low without them. Team characters
        # override their own bench entries with the developed plan.
        # On the maxed-account basis: the scenario's bench (every holomem at
        # the full tree, level-2 sockets) and its catalogue-wide Connect pool.
        _bench_socks = None     # the maxed bench carries its own sockets
        if ma_scenario is not None:
            bench_characters = ma_scenario.bench
            connect_cards = ma_scenario.connect_cards
        else:
            bench_characters = _bench_characters()
            _bench_socks = _bench_sockets()
            # "Unlimited cubes/hexas" (an option holodori.best also offers): every character off the
            # team spends its idle points on account-wide green, without a wallet
            # (`precompute.developed_bench_characters`, one MILP around the first
            # candidate, cached per (team, points)). Worth +12..16 % at max
            # board points; within a real wallet it moves nothing, so the
            # account's own materials keep the bench as captured.
            if str(body.get("materials") or "") == "infinite" and built:
                _seed = [built[0].leader.id, *[m.id for m in built[0].members]]
                _bkey = (tuple(_seed), flat_budget is not None)
                if _bkey not in precompute._DEVELOPED_BENCH:
                    precompute._DEVELOPED_BENCH[_bkey] = precompute.developed_bench_characters(
                        _seed, max_level=flat_budget is not None, resources="infinite")
                bench_characters = precompute._DEVELOPED_BENCH[_bkey] or bench_characters
        # chr-id -> holomem name for EVERY card, so the per-song re-score can
        # match a chart's chr-id cast against the name-keyed developed board
        # (team AND bench singers). Without it the singer-conditional nodes
        # never fire on the develop path (see board_model_from_plan).
        name_by_chr = {"chr-" + c.id.split("-")[1]: c.member for c in cards}

        # SINGER CONTEXT. The board plan is a function of
        # (character, role, budget, sings): a leader's conditional reds
        # (R-002/044/062/063) and her solo/group yellow song bonus fire only
        # on a song she is credited on, so the plan for THIS song must value
        # them (`board_plan.power_node_values(singer_weight=...)`). Song mode
        # -> per leader from the chart's own cast. Across mode / no song ->
        # False: one board is developed and reused for every chart, so there
        # is no single cast to key on and the song-agnostic plan is what the
        # across objective means.
        _cast = song_cast(chart) if chart is not None else SongCast()

        # the cast is passed whole (`sings=_cast`): every singing board is valued as a
        # singer, a member's yellows and yellow socket included -- not only the leader's

        # MEMBERS-FIRST seeds (see `members_first_k` above). Computed here,
        # after the bench / sockets / cast are resolved, on a RefinePool
        # whose plan cache is seeded exactly as the ENUMERATION's was (the
        # meta's `plan_cache_seed`), so the candidates are re-scored on the
        # same leaf that ranked them. The rosters then develop / refine /
        # rank like any other candidate; a roster the beam or a static seed
        # already produced is not added twice (a seed's provenance is
        # extended, a beam team is counted).
        members_first_meta: dict[str, Any] = {"available": False, "k": members_first_k}
        _mf_enum = None
        if chart is None:
            members_first_meta["reason"] = "not a song-mode run"
        elif difficulty != "EXPERT":
            # every enumeration on file was scored on the EXPERT chart, and its
            # validity key digests that chart: none is this run's own source
            members_first_meta["reason"] = (
                f"members-first enumerations are EXPERT-only; this run is {difficulty}")
        elif not (leader_diverse or refine):
            members_first_meta["reason"] = "run is neither every-leader (leader_diverse) nor refine"
        elif not _cards_maxed(basis_name) or pool_filter != "5star":
            members_first_meta["reason"] = (
                f"basis {basis_name!r} / pool {pool_filter!r} is not the enumerations' "
                f"universe (maxed cards, 5-star pool)")
        elif members_first_k is not None and members_first_k <= 0:
            members_first_meta["reason"] = "members_first_k is 0 (disabled)"
        else:
            _mf_dm = _develop_meta(cfg, basis_name, ma_scenario, pool_filter, flat_budget,
                                 difficulty=difficulty)
            _mf_pool = [c for c in cards if _is_five_star(c)]
            _mf_mode, _mf_enums, _mf_reason = members_first_index.default_index().sources(
                song, _mf_dm.get("inputs_hash"), (c.id for c in _mf_pool), budget=flat_budget,
                inputs_hash_files=_mf_dm.get("inputs_hash_files"),
                model_version=_mf_dm.get("model_version"))
            _mf_enum = _mf_enums[0] if _mf_enums else None
            members_first_meta["song"] = song
            if _mf_enum is None:
                members_first_meta["reason"] = _mf_reason
            else:
                if members_first_k is None:
                    members_first_k = (members_first_index.DEFAULT_K if _mf_mode == "own"
                                       else members_first_index.DEFAULT_K_TRANSFER)
                members_first_meta["k"] = members_first_k
                _mf_leaders = (list(_mf_pool) if leader_diverse
                               else [t.leader for t in built if _is_five_star(t.leader)])
                _seen_l: set[str] = set()
                _mf_leaders = [l for l in _mf_leaders
                               if not (l.id in _seen_l or _seen_l.add(l.id))]
                if seed_leader:
                    _mf_leaders = [l for l in _mf_leaders if l.id in set(seed_leader)]
                _emit({"type": "stage", "stage": "members_first", "state": "active",
                       "total": len(_mf_leaders),
                       "detail": (f"members-first seeds, K={members_first_k}, "
                                  f"{len(_mf_leaders)} leaders, "
                                  + (f"own enumeration of {song}" if _mf_mode == "own" else
                                     "TRANSFER from " + "+".join(e.song for e in _mf_enums)))})
                _mf_seed = _mf_enum.plan_cache_seed
                _mf_ctx = RefineContext(
                    cards=tuple(cards), cfg=dataclasses.replace(cfg, board=None),
                    charts=(chart,), skilltree=skilltree, node_master=node_master,
                    node_effect_ids=effects, connect_cards=connect_cards,
                    bench_characters=bench_characters, name_by_chr=name_by_chr,
                    flat_budget=flat_budget,
                    granted={k: int(v) for k, v in granted.items() if v},
                    use_plan_cache=_mf_seed is not None,
                    plan_cache_seed=_mf_seed)
                _t_mf = time.perf_counter()

                def _mf_progress(pos, total, leader):
                    _check_cancel()
                    _emit({"type": "progress", "stage": "members_first", "done": pos,
                           "total": total, "leader": leader.id})

                with RefinePool(_mf_ctx, workers=_web_workers(),
                                on_fallback=lambda line: print(f"  \u26a0 {line}", flush=True)
                                ) as _mf_rp:
                    def _mf_batch(leader, sets, _rp=_mf_rp):
                        return _rp.batch_score([
                            Team(leader=leader, members=tuple(by_id[m] for m in ms))
                            for ms in sets])
                    _mf_best, _mf_info = members_first_index.default_index().best_for_leaders(
                        song, _mf_dm.get("inputs_hash"), _mf_pool, _mf_leaders,
                        members_first_k, _mf_batch, budget=flat_budget,
                        inputs_hash_files=_mf_dm.get("inputs_hash_files"),
                        model_version=_mf_dm.get("model_version"), progress=_mf_progress)
                members_first_meta.update(_mf_info)
                members_first_k = int(members_first_meta.get("k") or members_first_k)
                _mf_prov = (f"seed:members-first K={members_first_k}"
                            + (" transfer:" + "+".join(members_first_meta.get("source_songs") or [])
                               if members_first_meta.get("mode") == "transfer" else ""))
                _taken_keys = {(t.leader.id, frozenset(m.id for m in t.members)) for t in built}
                _ban, _must = set(seed_exclude), set(seed_require)
                _added = _already_seed = _already_beam = 0
                _mf_rows = []
                for _lid, (_sc, _mids, _nc) in _mf_best.items():
                    _key = (_lid, frozenset(_mids))
                    _mf_rows.append({"leader": _lid, "members": list(_mids),
                                     "score": round(_sc), "candidates": _nc})
                    if _key in provenance_by_key:
                        provenance_by_key[_key] += "+" + _mf_prov[len("seed:"):]
                        _already_seed += 1
                        continue
                    if _key in _taken_keys:
                        _already_beam += 1
                        continue
                    _six = _key[1] | {_lid}
                    if _ban & _six or (_must and not _must <= _six):
                        continue
                    try:
                        built.append(Team(leader=by_id[_lid],
                                          members=tuple(by_id[m] for m in _mids)))
                    except (TeamError, KeyError):
                        continue
                    provenance_by_key[_key] = _mf_prov
                    _taken_keys.add(_key)
                    _added += 1
                members_first_meta.update({
                    "leaders_seeded": _added + _already_seed, "added": _added,
                    "already_seeded": _already_seed, "already_in_beam": _already_beam,
                    "wall_seconds": time.perf_counter() - _t_mf, "rows": _mf_rows,
                    "provenance": _mf_prov})
                _emit({"type": "fact", "stage": "members_first",
                       "text": (f"members-first ({members_first_meta.get('mode')}"
                                + (", from " + "+".join(members_first_meta.get("source_songs") or [])
                                   if members_first_meta.get("mode") == "transfer" else "")
                                + f", K={members_first_k}): {_added} roster(s) added, {_already_seed} "
                                f"already seeded, {_already_beam} already in the beam "
                                f"({members_first_meta['wall_seconds']:.0f}s)")})
                _emit({"type": "stage", "stage": "members_first", "state": "done"})
        # The leader options (as in holodori.best): a chosen leader HOLOMEM (any of
        # her cards — "the best outfit she has"), and "no leader overlap" (no
        # member card of the leader's own holomem). Applied to every candidate
        # before the develop, so the rows, the refine seeds and the final rank
        # only ever hold teams that satisfy them.
        _leader_member = str(body.get("leader_member") or "").strip()

        def _leader_ok(team) -> bool:
            """A refine swap may not break the leader options either."""
            if _leader_member and team.leader.member != _leader_member:
                return False
            if _leader_card and team.leader.id != _leader_card:
                return False
            return not (body.get("no_leader_overlap")
                        and team.leader.member in {m.member for m in team.members})

        _leader_card = str(body.get("leader_card") or "").strip()
        if _leader_member:
            built = [t for t in built if t.leader.member == _leader_member]
        if _leader_card:
            built = [t for t in built if t.leader.id == _leader_card]
        if body.get("no_leader_overlap"):
            built = [t for t in built if t.leader.member not in {m.member for m in t.members}]
        if not built:
            raise _HttpError(400, {"error": "no candidate team satisfies the leader options "
                                            "(leader holomem / no leader overlap)"})
        # ONE team per call, so progress can be reported and a cancel honoured
        # BETWEEN teams; the final sort is `optimize_and_rerank`'s own
        # (optimised_us desc, stable on input order), so the list is byte-
        # identical to one call over `built`.
        _emit({"type": "stage", "stage": "develop", "state": "active",
               "total": len(built),
               "detail": ("flat %d pt" % flat_budget if flat_budget is not None
                          else "real granted points")})
        _check_cancel()
        optimised = []
        for _k, _team in enumerate(built, start=1):
            if chart is not None:
                # SONG MODE: the team's activation nodes chosen jointly on THIS
                # chart's score (`board_plan.develop_refined_team`); a shorter
                # cooldown can add overlap, so no per-node value prices it
                # (+0.2..3.1 % on six songs).
                def _on_chart(board, _team=_team):
                    return score_team_on_chart(
                        _team, chart, dataclasses.replace(cfg, board=board)).total_score
                optimised.append(develop_refined_team(
                    _team, cfg, _on_chart, tree_for=tree_for, budget_for=budget_for,
                    node_effect_ids=effects, skilltree=skilltree,
                    connect_cards=connect_cards, bench_characters=bench_characters,
                    name_by_chr=name_by_chr, sings=_cast,
                    bench_sockets=_bench_socks)[0])
            else:
                optimised.extend(optimize_and_rerank(
                    [_team], cfg, tree_for=tree_for, budget_for=budget_for,
                    node_effect_ids=effects, skilltree=skilltree,
                    connect_cards=connect_cards,
                    bench_characters=bench_characters,
                    name_by_chr=name_by_chr,
                    sings=_cast, bench_sockets=_bench_socks,
                ))
            _emit({"type": "progress", "stage": "develop", "done": _k, "total": len(built),
                   "team": [_team.leader.id, *[m.id for m in _team.members]],
                   "optimised_us": round(optimised[-1].optimised_us),
                   # the song figure of this developed team: what `best_so_far`
                   # (job_progress) streams as the provisional best
                   **({"song_est": round(score_team_on_chart(
                       _team, chart, dataclasses.replace(cfg, board=optimised[-1].board)
                   ).total_score)} if chart is not None else {})})
            _check_cancel()
        optimised.sort(key=lambda r: r.optimised_us, reverse=True)
        # seeded rows, by position in `optimised` (a refine keeps the index;
        # the Unit-Score path re-sorts and remaps below)
        prov_by_index: dict[int, str] = {
            i: provenance_by_key[k] for i, bt in enumerate(optimised)
            if (k := (bt.team.leader.id, frozenset(m.id for m in bt.team.members)))
            in provenance_by_key}
    except (_HttpError, _Cancelled):
        raise
    except Exception as exc:  # noqa: BLE001 — report as JSON, never HTML 500
        import traceback  # noqa: PLC0415

        print(traceback.format_exc(), flush=True)
        raise _HttpError(500, {"error": f"{type(exc).__name__}: {exc}"})
    # Card faces for the returned teams — the SOLVE's payload.cards only carries
    # the cards it displayed, so a leader_diverse team the solve never showed
    # would render as "(unknown card)". Same `_card_view` shape the solve emits;
    # the client merges these into payload.cards before re-rendering.
    _states = _card_states()
    _used = set()
    for bt in optimised:
        _used.add(bt.team.leader.id)
        _used.update(m.id for m in bt.team.members)
    cards_map = {
        cid: _card_view(by_id[cid], art_dir, _state_of(_states, by_id[cid]))
        for cid in _used if cid in by_id
    }

    # Shared by both swap-refine paths (across + song): the candidate pool
    # matches the ranking's card basis, and develop is the same single-team
    # optimize_and_rerank the ranking itself ran (real sockets included).
    cand_pool: list = []
    if refine:
        owned_set = {c.id for c in owned}
        cand_pool = [c for c in cards
                     if _cards_maxed(basis_name) or c.id in owned_set]
        if pool_filter == "5star":
            cand_pool = [c for c in cand_pool if _is_five_star(c)]

        def _develop_one(team):
            # Same inputs as the main develop above (bench GREENs, cast
            # names), or a refined team is scored on a different basis
            # than the one it is compared against.
            return optimize_and_rerank(
                [team], cfg, tree_for=tree_for, budget_for=budget_for,
                node_effect_ids=effects, skilltree=skilltree,
                connect_cards=connect_cards,
                bench_characters=bench_characters,
                name_by_chr=name_by_chr, sings=_cast, bench_sockets=_bench_socks)[0]

        def _refine_pool(charts_for_metric, seed_team=None):
            # The refine's exact stage across processes (parallel_refine):
            # the same develop + per-song score the closures above run,
            # rebuilt in each worker from plain data. Serial fallback inside.
            # `refine_leaf == "plan_cache"`: every worker (and the serial
            # fallback) scores on ONE PlanCache seeded from `seed_team` — the
            # run's #1 developed team, FIXED for the whole run so every
            # candidate of every refined team meets the same objective
            # (~3.7 ms per leaf vs ~40 ms for a develop).
            use_cache = refine_leaf == "plan_cache" and seed_team is not None
            ctx = RefineContext(
                cards=tuple(cards), cfg=dataclasses.replace(cfg, board=None),
                charts=tuple(charts_for_metric), skilltree=skilltree,
                node_master=node_master, node_effect_ids=effects,
                connect_cards=connect_cards, bench_characters=bench_characters,
                name_by_chr=name_by_chr, flat_budget=flat_budget,
                granted={k: int(v) for k, v in granted.items() if v},
                use_plan_cache=use_cache,
                plan_cache_seed=((seed_team.leader.id,
                                  tuple(m.id for m in seed_team.members))
                                 if use_cache else None))
            _emit({"type": "fact", "stage": "refine",
                   "text": ("refine leaf: fixed plan cache seeded from the #1 "
                            "developed team" if use_cache
                            else "refine leaf: develop every candidate's board")})
            return RefinePool(
                ctx, workers=_web_workers(),
                on_fallback=lambda line: print(f"  \u26a0 {line}", flush=True))

        def _leaf_score_fn(pool, develop_score_fn):
            # The refine's INCUMBENT metric must be the same function as its
            # candidates' (batch) metric, or the accept rule compares a
            # developed-board score against a plan-cache one and the first
            # swap is judged on a different basis than the rest. Under
            # `plan_cache` that is the pool's own leaf; under `develop` the
            # closure the ranking scored with (unchanged legacy path).
            if pool.ctx.use_plan_cache:
                return lambda team, _board: pool.batch_score([team])[0]
            return develop_score_fn

        def _merge_refined_faces(bts):
            # refined teams may need faces the solve never displayed.
            for bt in bts:
                for cid in (bt.team.leader.id,
                            *[m.id for m in bt.team.members]):
                    if cid not in cards_map and cid in by_id:
                        cards_map[cid] = _card_view(
                            by_id[cid], art_dir,
                            _state_of(_states, by_id[cid]))

    # THE OBJECTIVE (see `objective_leaf` above). Under `plan_cache` every
    # song / across row is scored on the FIXED plans of ONE PlanCache
    # (`_obj_cache`, seeded ONCE for the run) — `optimize_and_rerank(...,
    # plan_cache=)` runs the same three steps the refine's workers run
    # (`parallel_refine._cached_board`: board-off breakdown, `cached_team_plan`,
    # `board_model_from_plan(plan_cache=)`) and returns the assembled board
    # plus the plan / sockets the row's board section renders. Same floats as
    # the RefinePool leaf. The per-team develop (`optimised`)
    # stays as the labelled what-if.
    objective: dict[str, Any] = {"leaf": objective_leaf}
    _obj_cache: PlanCache | None = None
    _obj_seed_team: Team | None = None

    def _team_from_ids(leader_id, member_ids):
        try:
            return Team(leader=by_id[leader_id],
                        members=tuple(by_id[m] for m in member_ids))
        except (KeyError, TeamError):
            return None

    def _resolve_objective_seed(rank_key):
        """(seed Team, source). Song mode: the per-leader TABLE's seed when
        this run's inputs_hash + basis match it (so rows and certificates are
        the same objective), else the members-first enumeration's (same
        guard, already checked), else — and always in across mode — the
        run's #1 developed team by `rank_key` (the refine's seed before this
        flag existed)."""
        if song and chart is not None:
            _dm0 = _develop_meta(cfg, basis_name, ma_scenario, pool_filter, flat_budget,
                                 difficulty=difficulty)
            found = certindex.default_index().plan_cache_seed_for(
                song, _dm0.get("inputs_hash"), _dm0["certificate_basis"])
            if found is not None:
                t = _team_from_ids(found[0], found[1])
                if t is not None:
                    return t, f"per-leader table ({found[2]})"
            if _mf_enum is not None and _mf_enum.plan_cache_seed is not None:
                t = _team_from_ids(*_mf_enum.plan_cache_seed)
                if t is not None:
                    return t, (f"members-first enumeration of {_mf_enum.song} (same "
                               + ("inputs_hash" if _mf_enum.legacy else
                                  "enumeration_key" if _mf_enum.song == song else "reference_key")
                               + (", transfer reference" if _mf_enum.song != song else "") + ")")
        if optimised:
            best = max(range(len(optimised)), key=rank_key)
            return optimised[best].team, "run #1 developed team"
        return None, None

    def _install_objective(rank_key) -> None:
        nonlocal _obj_cache, _obj_seed_team
        if objective_leaf != "plan_cache":
            return
        _obj_seed_team, source = _resolve_objective_seed(rank_key)
        if _obj_seed_team is None:
            return
        _obj_cache = PlanCache.from_team(_obj_seed_team, dataclasses.replace(cfg, board=None))
        objective.update({
            "plan_cache_seed": {"leader": _obj_seed_team.leader.id,
                                "members": [m.id for m in _obj_seed_team.members]},
            "seed_source": source,
            "meaning": ("rows are scored on the FIXED per-(character, role, budget, "
                        "singer-context) plans of one PlanCache seeded from this team "
                        "(OBJECTIVE-v2 §3, the v2 objective); the per-team greedy "
                        "re-develop is carried as the labelled what-if")})
        _emit({"type": "fact", "stage": "develop",
               "text": f"objective: fixed plan cache seeded from the {source}"})

    def _objective_bt(team: Team) -> BoardOptimisedTeam:
        """`team` on the fixed plan (plan-cache leaf): plan, sockets, board. In
        song mode the team's activation nodes are then chosen jointly on this
        chart's score (`develop_refined_team`; +0.2..3.1 % on six songs)."""
        assert _obj_cache is not None
        if chart is not None:
            def _on_chart(board, _team=team):
                return score_team_on_chart(
                    _team, chart, dataclasses.replace(cfg, board=board)).total_score
            return develop_refined_team(
                team, cfg, _on_chart, tree_for=tree_for, budget_for=budget_for,
                node_effect_ids=effects, skilltree=skilltree,
                connect_cards=connect_cards, bench_characters=bench_characters,
                name_by_chr=name_by_chr, plan_cache=_obj_cache, sings=_cast,
                bench_sockets=_bench_socks)[0]
        return optimize_and_rerank(
            [team], cfg, tree_for=tree_for, budget_for=budget_for,
            node_effect_ids=effects, skilltree=skilltree,
            connect_cards=connect_cards, bench_characters=bench_characters,
            name_by_chr=name_by_chr, plan_cache=_obj_cache, sings=_cast,
            bench_sockets=_bench_socks)[0]

    def _board_state_of(bt) -> dict:
        """{chr: {nodes, slots}} — the developed board as the timeline and the
        board section read it (the plan's selected nodes, the REAL Connect
        placements the re-score used)."""
        slots_by_chr: dict[str, dict[str, str]] = {}
        for p in bt.sockets:
            slots_by_chr.setdefault(p.chr, {})[p.socket] = p.card
        state: dict = {}
        for card in (bt.team.leader, *bt.team.members):
            cplan = bt.plan.per_character.get(card.id)
            if cplan is None:
                continue
            chr_id = "chr-" + card.id.split("-")[1]
            state[chr_id] = {"nodes": sorted(cplan.selected),
                             "slots": slots_by_chr.get(chr_id, {})}
        return state

    def _whatif(obj_value: float, dev_value: float, metric: str) -> dict:
        return {"label": "greedy re-develop for this team",
                "leaf": "develop", metric: round(dev_value),
                "delta_pct": (round(100.0 * (dev_value - obj_value) / obj_value, 2)
                              if obj_value else 0.0)}

    # Across-songs mode: score every developed team on EVERY chart, rank by the
    # MEAN per-song estimate. `wins` = how many charts each team tops AMONG THESE
    # developed teams (the only set developed, so an honest "best of the shown
    # teams per chart", not the full field). This is what makes develop + across
    # answer the across question instead of the deck-panel Unit Score.
    if across:
        # the objective's seed: song-agnostic, so the #1 by developed Unit
        # Score (the plan cache values at sings=False, as the across refine
        # leaf always has — parallel_refine.evaluate)
        _install_objective(lambda i: optimised[i].optimised_us)
        # Swap-refine BY the across metric, paid where it can matter: the
        # hill-climb's accept metric is a 16-chart sampled mean and its
        # candidate prefilter a 4-chart one (deterministic, evenly spread),
        # because the full 181-chart mean per candidate is ~200x the budget.
        # The FINAL ranking below re-scores every (possibly refined) team on
        # ALL charts, so sampling can only affect WHICH swaps were tried,
        # never the numbers shown.
        swaps_by_team: dict[int, list[dict]] = {}
        if refine and optimised:
            def _sampled(n):
                if len(all_charts) <= n:
                    return tuple(all_charts)
                stride = len(all_charts) / n
                return tuple(all_charts[int(i * stride)] for i in range(n))

            def _mean_over(subset):
                def fn(team, board):
                    cfg_x = dataclasses.replace(cfg, board=board)
                    total = 0.0
                    for ch in subset:
                        total += score_team_on_chart(team, ch, cfg_x).total_score
                    return total / len(subset)
                return fn

            exact_fn = _mean_over(_sampled(16))
            pre_fn = _mean_over(_sampled(4))
            order = sorted(
                range(len(optimised)),
                key=lambda i: -exact_fn(optimised[i].team,
                                        optimised[i].board))
            _emit({"type": "stage", "stage": "refine", "state": "active",
                   "total": min(12, len(order))})
            with _refine_pool(_sampled(16), _obj_seed_team if _obj_cache is not None
                              else optimised[order[0]].team) as pool:
                leaf_fn = _leaf_score_fn(pool, exact_fn)
                for pos, i in enumerate(order[:12]):
                    _check_cancel()
                    _emit({"type": "progress", "stage": "refine", "done": pos,
                           "total": min(12, len(order))})
                    refined, _est, swaps = refine_members(
                        optimised[i], candidates=cand_pool,
                        develop_fn=_develop_one, score_fn=leaf_fn,
                        prefilter_fn=pre_fn,
                        batch_score_fn=pool.batch_score,
                        **(refine_kw if pos < 4 else {}))
                    if swaps and _leader_ok(refined.team):
                        optimised[i] = refined
                        swaps_by_team[id(refined.team)] = swaps
            _merge_refined_faces(optimised[i] for i in order[:12])
        n_charts = len(all_charts)
        # ONE scoring path, sharded: the same `rank_across_charts` machinery
        # the solve's across mode uses, each team scored against its OWN
        # developed board (`boards`, aligned 1:1). A serial per-team loop
        # would be ~20 s of a ~29 s request (60 teams x 181 charts); the
        # pool's fallback is the serial
        # same-answer path, so a spawn failure costs time, never the answer.
        # Under the plan-cache objective the RANKED pass scores each team on
        # its FIXED-plan board (`_objective_bt`); the develop boards are scored
        # in a second pass for the what-if mean (`across_score_develop`).
        obj_bts = ([_objective_bt(bt.team) for bt in optimised]
                   if _obj_cache is not None else None)
        scored_bts = obj_bts if obj_bts is not None else optimised
        ranked = rank_across_charts_parallel(
            [bt.team for bt in scored_bts], all_charts,
            dataclasses.replace(cfg, board=None),
            boards=[bt.board for bt in scored_bts],
            workers=_web_workers(),
            on_fallback=lambda line: print(f"  ⚠ {line}", flush=True),
        )
        dev_mean_by_team: dict[int, float] = {}
        if obj_bts is not None:
            ranked_dev = rank_across_charts_parallel(
                [bt.team for bt in optimised], all_charts,
                dataclasses.replace(cfg, board=None),
                boards=[bt.board for bt in optimised],
                workers=_web_workers(),
                on_fallback=lambda line: print(f"  ⚠ {line}", flush=True),
            )
            dev_mean_by_team = {id(r.team): r.expected_score for r in ranked_dev.teams}
        # `assemble_across_charts` builds its rows from the parent's own
        # team objects, so identity maps each ranked row back to its
        # BoardOptimisedTeam (plan/sockets) without inventing a roster key.
        bt_by_team = {id(bt.team): bt for bt in scored_bts}
        dev_bt_by_team = {id(bt.team): bt for bt in optimised}
        prov_by_team = {id(optimised[i].team): p for i, p in prov_by_index.items()}
        rows = []
        seen_rosters: set[tuple] = set()
        for ranked_row in ranked.teams:
            bt = bt_by_team[id(ranked_row.team)]
            leader, members = bt.team.leader, bt.team.members
            # Keyed by (leader, member SET): a flat set of six ids would merge
            # two DIFFERENT teams whose leader and one member swap roles
            # (X leads {Y,..} vs Y leads {X,..}) and drop the second as a
            # "duplicate" (9 of 77 leader_diverse rows, in one run). A refine
            # keeps its leader, so this still catches a refine landing on a
            # listed team.
            roster = (leader.id, frozenset(m.id for m in members))
            if roster in seen_rosters:
                continue  # a refine landed on a team already listed
            seen_rosters.add(roster)
            mean = ranked_row.expected_score
            board_state = _board_state_of(bt)
            whatif: dict = {}
            if obj_bts is not None:
                dev_bt = dev_bt_by_team[id(bt.team)]
                dev_mean = dev_mean_by_team.get(id(bt.team), 0.0)
                dev_state = _board_state_of(dev_bt)
                whatif = {
                    "across_score_develop": round(dev_mean),
                    "points_develop": sum(p.spent for p in dev_bt.plan.per_character.values()),
                    "board_state_develop": dev_state,
                    "board_plan_summary_develop": {**_board_plan_summary(
                        dev_bt, dev_state,
                        budget_kind="flat" if flat_budget is not None else "real",
                        budget_for=budget_for, current_by_chr=current_by_chr,
                        node_prices=node_prices, board_off=round(dev_bt.board_off_us),
                        developed=round(dev_bt.optimised_us), metric="unit_score"),
                        "leaf": "develop"},
                    "develop_whatif": _whatif(mean, dev_mean, "across_score"),
                    "objective_leaf": "plan_cache",
                }
            rows.append((mean, {
                "cards": [leader.id, *[m.id for m in members]],
                "leader": leader.id,
                "leader_id": leader.id,
                "member_ids": [m.id for m in members],
                "leader_member": leader.member,
                "members": [m.member for m in members],
                "gates": _team_gates(bt.team, cfg),
                "board_state": board_state,
                "across_score": round(mean),
                "wins": ranked_row.wins,
                # a team has one best slot order PER SONG: the one best on the
                # most charts, and the share of charts it is best on
                **dict(zip(("member_order", "member_order_share"),
                           row_detail.modal_order(ranked_row.per_chart))),
                "points": sum(p.spent for p in bt.plan.per_character.values()),
                "board_plan_summary": {**_board_plan_summary(
                    bt, board_state, budget_kind="flat" if flat_budget is not None else "real",
                    budget_for=budget_for, current_by_chr=current_by_chr,
                    node_prices=node_prices, board_off=round(bt.board_off_us),
                    developed=round(bt.optimised_us), metric="unit_score"),
                    **({"leaf": "plan_cache"} if obj_bts is not None else {})},
                **whatif,
                **({"refined": swaps_by_team[id(bt.team)]}
                   if id(bt.team) in swaps_by_team else {}),
                **({"provenance": prov_by_team[id(bt.team)]}
                   if id(bt.team) in prov_by_team else {}),
            }))
        rows.sort(key=lambda r: r[0], reverse=True)
        return {"ranking": [r for _, r in rows],
                "basis": basis_name, "across": True,
                "chart_count": n_charts, "difficulty": difficulty, "cards": cards_map,
                "budget": "real" if flat_budget is None else flat_budget,
                "sorted_by": "across_song",
                **_develop_meta(cfg, basis_name, ma_scenario, pool_filter, flat_budget,
                                difficulty=difficulty, leaf="plan_cache" if obj_bts is not None else "develop"),
                "objective": objective,
                "_internal": _internal(cfg, scored_bts)}
    # Song mode: re-score every developed board PER-SONG (estimate = expected
    # procs; max = every ACTIVE fires) and re-order by the estimate. The board
    # was developed for Unit Score, but the RANK is by the on-chart score — the
    # question the user actually asked. `board_off` is the same team scored on
    # the chart with no board, for the gain line.
    if song and chart is not None:
        def _song_est_of(team, board):
            # the ranker's own figure (`song_rank.score_team_on_chart`): Overall
            # Power on the default basis, as the refine leaf and the two-stage
            # rows score — a bare `score_song_detailed` is the modelled stat pool
            return score_team_on_chart(
                team, chart, dataclasses.replace(cfg, board=board)).total_score

        # Swap-refine the teams that will actually be SHOWN (the per-song
        # top-12), by the per-song developed metric itself. Leader fixed, so
        # leader_diverse keeps its one-row-per-leader meaning; a refine that
        # lands on a team already in the list is dropped by the dedupe below.
        swaps_by_index: dict[int, list[dict]] = {}
        # The refine's SEED per team (roster + developed per-song score
        # before any swap), for the row's HEURISTIC certificate.
        seed_by_index: dict[int, tuple[list[str], float]] = {}
        # every team's per-song score on its OWN developed board (the
        # legacy headline; under the plan-cache objective the what-if) —
        # the refine's order and the objective's fallback seed read it
        _pre = {i: _song_est_of(optimised[i].team, optimised[i].board)
                for i in range(len(optimised))}
        _install_objective(lambda i: _pre[i])
        if refine:
            order = sorted(range(len(optimised)), key=lambda i: -_pre[i])
            for i in order[:12]:
                seed_by_index[i] = (
                    [optimised[i].team.leader.id,
                     *[m.id for m in optimised[i].team.members]], _pre[i])
            _emit({"type": "stage", "stage": "refine", "state": "active",
                   "total": min(12, len(order))})
            # the refine's plan cache is THE objective's cache seed when the
            # objective is the plan-cache leaf (one objective for the whole
            # run: refine, rows, certificates), else the legacy #1 developed
            pool = _refine_pool([chart], _obj_seed_team if _obj_cache is not None
                                else optimised[order[0]].team).__enter__()
            try:
                # incumbent + exact metric = the pool's leaf; the CHEAP
                # prefilter stays the incumbent-board per-song proxy.
                leaf_fn = _leaf_score_fn(pool, _song_est_of)
                for pos, i in enumerate(order[:12]):
                    _check_cancel()
                    _emit({"type": "progress", "stage": "refine", "done": pos,
                           "total": min(12, len(order))})
                    # wide's larger shortlist only where it can change the
                    # answer shown first (top-4); the rest keep the normal cost.
                    refined, _est, swaps = refine_members(
                        optimised[i], candidates=cand_pool,
                        develop_fn=_develop_one, score_fn=leaf_fn,
                        prefilter_fn=_song_est_of,
                        batch_score_fn=pool.batch_score,
                        **(refine_kw if pos < 4 else {}))
                    if swaps and _leader_ok(refined.team):
                        optimised[i] = refined
                        swaps_by_index[i] = swaps
                # WIDE: the hill-climb converges to the basin of its ONE seed
                # (the beam's Unit-Score team), and that basin is not always
                # the best — on NIGHTBREAK, Calli PDW's beam seed converges at
                # 3,483,909 while a different seed converges at 3,565,062
                # (+2.3 %, leader-as-member roster).
                # So for the top-4 leaders refine from EXTRA seeds too and keep
                # the best: the top-3 other rosters with this leader swapped
                # in, and the own team with its weakest slot given to the
                # leader card itself (legal: the leader may also be a member).
                if refine_raw == "wide":
                    top_ids = order[:4]
                    donors = [optimised[j].team for j in order[:10]]
                    # an alt seed hunts a different BASIN, not width: normal
                    # shortlist, but enough rounds to converge.
                    alt_kw = {"shortlist": 8, "max_rounds": 6}
                    for i in top_ids:
                        seed_bt = optimised[i]
                        leader = seed_bt.team.leader
                        best_bt = seed_bt
                        best_score = leaf_fn(seed_bt.team, seed_bt.board)
                        best_swaps = swaps_by_index.get(i, [])
                        seeds: list[Team] = []
                        for donor in donors:
                            if donor.leader.id == leader.id:
                                continue
                            try:
                                seeds.append(Team(leader=leader,
                                                  members=donor.members))
                            except TeamError:
                                continue
                        # leader-as-member: replace each slot in turn, keep
                        # the cheap-best variant as one more seed.
                        variants = []
                        for k in range(len(seed_bt.team.members)):
                            ms = list(seed_bt.team.members)
                            ms[k] = leader
                            try:
                                v = Team(leader=leader, members=tuple(ms))
                            except TeamError:
                                continue
                            variants.append((_song_est_of(v, seed_bt.board), v))
                        if variants:
                            seeds.append(max(variants, key=lambda t: t[0])[1])
                        for seed_team in seeds[:6]:
                            try:
                                seed_dev = _develop_one(seed_team)
                            except Exception:  # noqa: BLE001 — a bad seed is skipped
                                continue
                            alt, alt_score, alt_swaps = refine_members(
                                seed_dev, candidates=cand_pool,
                                develop_fn=_develop_one,
                                score_fn=leaf_fn, prefilter_fn=_song_est_of,
                                batch_score_fn=pool.batch_score, **alt_kw)
                            if alt_score > best_score + 1.0 and _leader_ok(alt.team):
                                best_bt, best_score = alt, alt_score
                                best_swaps = [{"slot": -1, "out": None,
                                               "in": None, "gain": 0.0,
                                               "seed": "alt"}] + alt_swaps
                        if best_bt is not seed_bt:
                            optimised[i] = best_bt
                            swaps_by_index[i] = best_swaps
            finally:
                pool.close()
            _merge_refined_faces(optimised[i] for i in swaps_by_index)

        song_rows = []
        seen_rosters: set[tuple] = set()
        # the rows' scored objects: the FIXED-plan board per team under the
        # plan-cache objective (develop kept alongside as the what-if), the
        # develop itself under the legacy leaf
        scored_bts: list = []
        for index, dev_bt in enumerate(optimised):
            bt = _objective_bt(dev_bt.team) if _obj_cache is not None else dev_bt
            scored_bts.append(bt)
            leader, members = bt.team.leader, bt.team.members
            # Keyed by (leader, member SET): a flat set of six ids would merge
            # two DIFFERENT teams whose leader and one member swap roles
            # (X leads {Y,..} vs Y leads {X,..}) and drop the second as a
            # "duplicate" (9 of 77 leader_diverse rows, in one run). A refine
            # keeps its leader, so this still catches a refine landing on a
            # listed team.
            roster = (leader.id, frozenset(m.id for m in members))
            if roster in seen_rosters:
                continue  # a refine landed on a team already listed
            seen_rosters.add(roster)
            cfg_on = dataclasses.replace(cfg, board=bt.board)
            cfg_off = dataclasses.replace(cfg, board=None)
            # ONE number set per row (row_detail.figures): estimate, ceiling,
            # board-off, slot order and deck power through the ranker's own
            # `score_team_on_chart` — the same figures /api/compare,
            # /api/timeline and /api/row-detail return for this board.
            fig = row_detail.figures(bt.team, chart, cfg_on, cfg_off=cfg_off)
            est, mx, off = fig["song_est"], fig["song_max"], fig["song_off"]
            order = fig["member_order"]
            # Visibility only (not a second scorer): active coverage of the
            # song and the share of summed active value the engine's MAX
            # rule discards, read off the SAME timeline windows the page
            # draws — so a #1 that wins on raw power despite a stacked
            # active lattice shows it in the row.
            cover = active_coverage(timeline(
                members, chart, cfg_on,
                tuple(order or [m.id for m in members])))
            # The developed board as {chr: [nodes]} so the caller can regenerate
            # each team's timeline against the SAME cooldowns it was scored on
            # (the fixed plan under the plan-cache objective; the Engine-A
            # per-team develop under the legacy leaf). `slots` carries the
            # REAL Connect placements the re-score used (empty when the recon
            # data was unavailable and the idealised model applied).
            board_state = _board_state_of(bt)
            whatif: dict = {}
            if _obj_cache is not None:
                # the per-team greedy re-develop: its own per-song score and
                # its own board, LABELLED as the what-if
                est_dev = _song_est_of(dev_bt.team, dev_bt.board)
                dev_state = _board_state_of(dev_bt)
                whatif = {
                    "song_est_develop": round(est_dev),
                    "points_develop": sum(p.spent for p in dev_bt.plan.per_character.values()),
                    "board_state_develop": dev_state,
                    "board_plan_summary_develop": {**_board_plan_summary(
                        dev_bt, dev_state,
                        budget_kind="flat" if flat_budget is not None else "real",
                        budget_for=budget_for, current_by_chr=current_by_chr,
                        node_prices=node_prices, board_off=round(off),
                        developed=round(est_dev), metric="song_est"), "leaf": "develop"},
                    "develop_whatif": _whatif(est, est_dev, "song_est"),
                    "objective_leaf": "plan_cache",
                }
            song_rows.append((est, {
                "_index": index,
                "_dist": (bt.team, cfg_on, order or [m.id for m in members], fig["deck_power"], est),
                "cards": [leader.id, *[m.id for m in members]],
                "leader": leader.id,
                "leader_id": leader.id,
                "member_ids": [m.id for m in members],
                "leader_member": leader.member,
                "members": [m.member for m in members],
                "member_order": order or [m.id for m in members],
                "gates": _team_gates(bt.team, cfg),
                "board_state": board_state,
                "song_off": round(off),
                "song_est": round(est),
                "song_max": round(mx),
                "deck_power": round(fig["deck_power"]),
                "active_cover_pct": round(cover["active_cover_pct"], 1),
                "active_overlap_loss_pct": round(cover["active_overlap_loss_pct"], 1),
                "active_dead_start_s": round(cover["active_dead_start_s"], 1),
                "gain_pct": round(100.0 * (est - off) / off, 1) if off else 0.0,
                "points": sum(p.spent for p in bt.plan.per_character.values()),
                "board_plan_summary": {**_board_plan_summary(
                    bt, board_state, budget_kind="flat" if flat_budget is not None else "real",
                    budget_for=budget_for, current_by_chr=current_by_chr,
                    node_prices=node_prices, board_off=round(off), developed=round(est),
                    metric="song_est"),
                    **({"leaf": "plan_cache"} if _obj_cache is not None else {})},
                **whatif,
                **({"refined": swaps_by_index[index]}
                   if index in swaps_by_index else {}),
                **({"provenance": prov_by_index[index]}
                   if index in prov_by_index else {}),
            }))
        song_rows.sort(key=lambda r: r[0], reverse=True)
        # p10/p50/p90 of the exact run-score law on the rows the page shows
        # first (~0.5 s a row), as the two-stage cache's first ORDER_ROWS carry
        for _k, (_e, _r) in enumerate(song_rows):
            _team_d, _cfg_d, _order_d, _power_d, _est_d = _r.pop("_dist")
            if _k < 8:
                try:
                    _r.update(row_detail.quantile_fields({
                        "song_max": _r["song_max"],
                        "distribution": row_detail.distribution(
                            _team_d, chart, _cfg_d, _order_d, _power_d, row_score=_est_d)}))
                except Exception as _exc:  # noqa: BLE001 — the row stands without them
                    _r["quantiles_error"] = f"{type(_exc).__name__}: {_exc}"
        # Certificates: the developed per-song rows are HEURISTIC
        # by construction — best of the teams this pipeline developed (and
        # refined); upgraded to EXACT/BOUNDED only when a certificate on file
        # passes the same-instant-same-basis guard (hash + budget/uptime/
        # platform/universe). Row score = `song_est` (this chart; the fixed
        # plan under the plan-cache objective — the certificates' own leaf —
        # else the per-team develop, which `certificate_basis.leaf` names so
        # the guard reports HEURISTIC, not ERROR, above a certified maximum).
        _leaf = "plan_cache" if _obj_cache is not None else "develop"
        _dm = _develop_meta(cfg, basis_name, ma_scenario, pool_filter, flat_budget,
                            difficulty=difficulty, leaf=_leaf)
        meta = {k: _dm[k] for k in ("model_version", "inputs_hash", "inputs_hash_error")
                if k in _dm}
        cbasis = _dm["certificate_basis"]
        gen = ("beam per_leader_best (one team per leader)" if leader_diverse
               else "posted stage-1 rosters (beam/exact top-K by Unit Score)")
        stages = [f"candidates: {gen}",
                  "develop: Engine-A board per team (optimize_and_rerank, "
                  + ("flat %d pt" % flat_budget if flat_budget is not None
                     else "real granted points") + ")"
                  + (" — carried as the what-if" if _obj_cache is not None else "")]
        if members_first_meta.get("available"):
            stages.insert(1, (f"seeds: members-first K={members_first_meta.get('k')} "
                              f"({members_first_meta.get('leaders_seeded', 0)} leaders from the "
                              + ("cached bare-proxy enumeration of this song"
                                 if members_first_meta.get("mode") == "own" else
                                 "bare-proxy enumeration(s) of "
                                 + "+".join(members_first_meta.get("source_songs") or [])
                                 + " TRANSFERRED to this song")
                              + f", proxy {members_first_meta.get('proxy')})"))
        if refine:
            stages.append("refine: 1-swap hill-climb of the top-12 by developed "
                          "per-song score" + (", wide (shortlist 20 + alt seeds)"
                                              if refine_raw == "wide" else ""))
        if _obj_cache is not None:
            stages.append(f"objective: fixed plan cache seeded from the "
                          f"{objective.get('seed_source')} (plan-cache leaf)")
            stages.append(f"rank: score_song_detailed on {song}, fixed-plan board")
        else:
            stages.append(f"rank: score_song_detailed on {song}, developed board")
        for est, row in song_rows:
            idx = row.pop("_index", None)
            seed = seed_by_index.get(idx) if idx is not None else None
            hc = certindex.heuristic_certificate(
                model_version=meta["model_version"], inputs_hash=meta.get("inputs_hash"),
                basis=cbasis, generator=gen, stages=stages,
                candidates_evaluated=len(optimised),
                seed_team=seed[0] if seed else None,
                seed_score=round(seed[1]) if seed else None,
                extra={"refine_candidate_pool": len(cand_pool)} if refine else None)
            row["certificate"] = certindex.attach(
                hc, song=song, leader=row["leader_id"], member_ids=row["member_ids"],
                row_score=row["song_est"])
        return {"ranking": [r for _, r in song_rows],
                "basis": basis_name, "song": song, "difficulty": difficulty, "cards": cards_map,
                "budget": "real" if flat_budget is None else flat_budget,
                "sorted_by": "song_est", **_dm,
                "members_first": members_first_meta,
                "objective": objective,
                "_internal": _internal(cfg, scored_bts)}
    # Song-agnostic (Unit Score) rows carry the SAME team shape as the song
    # and across rows above (leader_id / member_ids / gates / board_state):
    # the client's leader_diverse branch REBUILDS the ranking from these rows,
    # so without them "best team of every leader" with no song would render
    # "(unknown card)", no members and "0 of 0 conditions met". The developed
    # board_state also drives each team's timeline in the plain (non-leaders)
    # develop.
    #
    # Swap-refine BY the developed Unit Score (any-song mode refines too).
    # Same machinery as song mode: leader fixed,
    # hill-climb the five slots on the developed metric, exact stage across
    # processes (RefinePool with no charts -> deck-screen Unit Score), and
    # under `wide` the extra seeds that escape the beam seed's own basin.
    swaps_by_index: dict[int, list[dict]] = {}
    if refine and optimised:
        def _us_of(team, board):
            return deck_screen_unit_score(score_with_breakdown(
                team, dataclasses.replace(cfg, board=board))).unit_score

        order = sorted(range(len(optimised)),
                       key=lambda i: -optimised[i].optimised_us)
        _emit({"type": "stage", "stage": "refine", "state": "active",
               "total": min(12, len(order))})
        pool = _refine_pool((), optimised[order[0]].team).__enter__()
        try:
            leaf_fn = _leaf_score_fn(pool, _us_of)
            for pos, i in enumerate(order[:12]):
                _check_cancel()
                _emit({"type": "progress", "stage": "refine", "done": pos,
                       "total": min(12, len(order))})
                refined, _est, swaps = refine_members(
                    optimised[i], candidates=cand_pool,
                    develop_fn=_develop_one, score_fn=leaf_fn,
                    prefilter_fn=_us_of,
                    batch_score_fn=pool.batch_score,
                    **(refine_kw if pos < 4 else {}))
                if swaps and _leader_ok(refined.team):
                    optimised[i] = refined
                    swaps_by_index[i] = swaps
            if refine_raw == "wide":
                donors = [optimised[j].team for j in order[:10]]
                alt_kw = {"shortlist": 8, "max_rounds": 6}
                for i in order[:4]:
                    seed_bt = optimised[i]
                    leader = seed_bt.team.leader
                    best_bt = seed_bt
                    best_score = leaf_fn(seed_bt.team, seed_bt.board)
                    best_swaps = swaps_by_index.get(i, [])
                    seeds: list[Team] = []
                    for donor in donors:
                        if donor.leader.id == leader.id:
                            continue
                        try:
                            seeds.append(Team(leader=leader, members=donor.members))
                        except TeamError:
                            continue
                    variants = []
                    for k in range(len(seed_bt.team.members)):
                        ms = list(seed_bt.team.members)
                        ms[k] = leader
                        try:
                            v = Team(leader=leader, members=tuple(ms))
                        except TeamError:
                            continue
                        variants.append((_us_of(v, seed_bt.board), v))
                    if variants:
                        seeds.append(max(variants, key=lambda t: t[0])[1])
                    for seed_team in seeds[:6]:
                        try:
                            seed_dev = _develop_one(seed_team)
                        except Exception:  # noqa: BLE001 — a bad seed is skipped
                            continue
                        alt, alt_score, alt_swaps = refine_members(
                            seed_dev, candidates=cand_pool,
                            develop_fn=_develop_one, score_fn=leaf_fn,
                            prefilter_fn=_us_of,
                            batch_score_fn=pool.batch_score, **alt_kw)
                        if alt_score > best_score + 1.0 and _leader_ok(alt.team):
                            best_bt, best_score = alt, alt_score
                            best_swaps = [{"slot": -1, "out": None, "in": None,
                                           "gain": 0.0, "seed": "alt"}] + alt_swaps
                    if best_bt is not seed_bt:
                        optimised[i] = best_bt
                        swaps_by_index[i] = best_swaps
        finally:
            pool.close()
        _merge_refined_faces(optimised[i] for i in swaps_by_index)
        # the ranking below is in optimised order; a refined team may have
        # moved, so re-sort by the developed Unit Score before emitting.
        paired = sorted(enumerate(optimised), key=lambda p: -p[1].optimised_us)
        swaps_by_index = {new: swaps_by_index[old]
                          for new, (old, _bt) in enumerate(paired)
                          if old in swaps_by_index}
        prov_by_index = {new: prov_by_index[old]
                         for new, (old, _bt) in enumerate(paired)
                         if old in prov_by_index}
        optimised = [bt for _old, bt in paired]

    ranking = []
    seen_rosters: set[tuple] = set()
    for index, bt in enumerate(optimised):
        base, opt = bt.board_off_us, bt.optimised_us
        leader, members = bt.team.leader, bt.team.members
        roster = (leader.id, frozenset(m.id for m in members))
        if roster in seen_rosters:
            continue  # a refine landed on a team already listed
        seen_rosters.add(roster)
        board_state = {}
        slots_by_chr = {}
        for p in bt.sockets:
            slots_by_chr.setdefault(p.chr, {})[p.socket] = p.card
        for card in (leader, *members):
            cplan = bt.plan.per_character.get(card.id)
            if cplan is None:
                continue
            chr_id = "chr-" + card.id.split("-")[1]
            board_state[chr_id] = {
                "nodes": sorted(cplan.selected),
                "slots": slots_by_chr.get(chr_id, {})}
        ranking.append({
            "cards": [leader.id, *[m.id for m in members]],
            "leader": leader.id,
            "leader_id": leader.id,
            "member_ids": [m.id for m in members],
            "leader_member": leader.member,
            "members": [m.member for m in members],
            "gates": _team_gates(bt.team, cfg),
            "board_state": board_state,
            "board_off_us": round(base),
            "optimised_us": round(opt),
            "gain_pct": round(100.0 * (opt - base) / base, 1) if base else 0.0,
            "points": sum(p.spent for p in bt.plan.per_character.values()),
            "board_plan_summary": _board_plan_summary(
                bt, board_state, budget_kind="flat" if flat_budget is not None else "real",
                budget_for=budget_for, current_by_chr=current_by_chr,
                node_prices=node_prices, board_off=round(base), developed=round(opt),
                metric="unit_score"),
            **({"refined": swaps_by_index[index]}
               if index in swaps_by_index else {}),
            **({"provenance": prov_by_index[index]}
               if index in prov_by_index else {}),
        })
    return {"ranking": ranking, "basis": basis_name, "cards": cards_map,
            "budget": "real" if flat_budget is None else flat_budget,
            "sorted_by": "optimised_us",
            # the song-agnostic rows are the per-team develop (no plan-cache
            # objective for the deck-screen Unit Score); say so
            **_develop_meta(cfg, basis_name, ma_scenario, pool_filter, flat_budget,
                            difficulty=difficulty, leaf="develop"),
            "objective": {"leaf": "develop"},
            "_internal": _internal(cfg, optimised)}


# ---------------------------------------------------------------------------
# /api/run — ONE job: search → (every leader) → develop → (refine) → rank.
# Additive: `/solve` + `/api/optimize-rerank` remain available.
# ---------------------------------------------------------------------------
# Default develop funnel: how many stage-1 rosters are developed (and refined)
# before the final rank. On a desktop PC (song m0334, owned cards, real points,
# refine on): develop+refine
# 10.0 s / 22.4 s / 28.1 s for 5 / 15 / 25 (whole run 21.9 / 33.9 / 39.6 s incl.
# the ~11 s search) — the refine only touches the top-12, so 25 costs +6 s over
# 15 and can only add a developed winner the smaller funnel never developed.
DEFAULT_DEVELOP_TOP = 25
RUN_STAGES = ("search", "leaders", "develop", "refine", "rank")


def _parse_run_body(body: Mapping) -> dict:
    """The `/api/run` request: the `/solve` fields plus the explicit develop
    controls (`board`, `budget`, `leaders`, `refine`, `develop_top`). No
    hidden `boardmode` encoding — every field says what it is."""
    top_n = max(1, min(int(body.get("top", 10) or 10), 50))
    basis = _parse_basis(body.get("basis"))
    board = str(body.get("board") or "develop").lower()
    if board not in ("develop", "current", "off"):
        board = "develop"
    budget_raw = body.get("budget", "real")
    if budget_raw in ("full", "full_tree"):
        budget: object = 453
    elif budget_raw in (None, "", "real"):
        budget = "real"
    else:
        try:
            budget = max(0, min(453, int(budget_raw)))
        except (TypeError, ValueError):
            budget = "real"
    leaders = body.get("leaders", False) is True
    if leaders:
        budget = 453          # every-leader mode is a full-tree comparison
    if basis == MAXED_ACCOUNT_BASIS and budget == "real":
        budget = MAXED_ACCOUNT_BUDGET   # a maxed account has every point: full tree
    refine_raw = body.get("refine", False)
    refine = "wide" if refine_raw == "wide" else (refine_raw is True)
    try:
        develop_top = max(1, min(150, int(body.get("develop_top") or DEFAULT_DEVELOP_TOP)))
    except (TypeError, ValueError):
        develop_top = DEFAULT_DEVELOP_TOP
    song = str(body.get("song") or "").strip()
    across = body.get("across_songs", False) is True and not song
    pool = str(body.get("pool") or "all").lower()
    if pool not in ("all", "5star"):
        pool = "all"

    def _ids(key):
        raw = body.get(key)
        return tuple(dict.fromkeys(str(x).strip() for x in raw if str(x).strip())) \
            if isinstance(raw, list) else ()

    uptime_model = str(body.get("uptime_model") or "").lower() or None
    if uptime_model not in ACTIVE_UPTIME_MODELS:
        uptime_model = None
    return {
        "top": top_n, "basis": basis, "board": board, "budget": budget,
        "leaders": leaders, "refine": refine, "develop_top": develop_top,
        "song": song, "across": across, "pool": pool,
        "require": _ids("require"), "leader": _ids("leader"), "exclude": _ids("exclude"),
        "include_unowned": body.get("include_unowned", False) is True,
        "exact": body.get("exact", False) is True,
        "uptime_model": uptime_model,
        "assume_level": _parse_assume(body, "assume_level", 1, 80),
        "assume_bloom": _parse_assume(body, "assume_bloom", 0, 6),
        # seed rosters: None -> pipeline default; "all" | "file" | False
        "seeds": (body["seeds"] if body.get("seeds") in ("all", "file", True, False)
                  else None),
        "refine_leaf": (str(body["refine_leaf"]).lower()
                        if body.get("refine_leaf") in ("plan_cache", "develop") else None),
        # the rows' objective leaf: None -> pipeline default (plan_cache);
        # "develop" pins the legacy per-team develop headline
        "objective_leaf": (str(body["objective_leaf"]).lower()
                           if body.get("objective_leaf") in ("plan_cache", "develop") else None),
        "platform": _parse_platform(body),
        # the chart difficulty every song / across stage scores on (absent ->
        # EXPERT; unknown -> `_HttpError(400)`, which `_start_run` returns)
        "difficulty": _parse_difficulty(body, song),
        # options as in holodori.best, the same fields /api/optimize-rerank reads:
        # "infinite" bench materials (the other characters' idle points on
        # green, no wallet), a leader holomem / leader card, no leader overlap
        "materials": "infinite" if str(body.get("materials") or "") == "infinite" else None,
        "leader_member": str(body.get("leader_member") or "").strip() or None,
        "leader_card": str(body.get("leader_card") or "").strip() or None,
        "no_leader_overlap": body.get("no_leader_overlap") is True,
    }


def run_plan(opts: Mapping) -> list[dict]:
    """The stepper the client draws BEFORE anything runs: one entry per stage
    this request will actually go through, with a rough ETA where measured."""
    develop = opts["board"] == "develop"
    stages = [{"id": "search", "label": "Search",
               "detail": "beam / exact top-K by Unit Score"}]
    if develop and opts["leaders"]:
        stages.append({"id": "leaders", "label": "Every leader",
                       "detail": "one beam, best team per leader"})
    if develop:
        n = opts["develop_top"] if not opts["leaders"] else None
        stages.append({"id": "develop", "label": f"Develop {n}" if n else "Develop",
                       "total": n, "detail": ("full tree (453)" if opts["budget"] == 453
                                              else f"{opts['budget']} pt flat"
                                              if isinstance(opts["budget"], int)
                                              else "real granted points")})
        if opts["refine"]:
            stages.append({"id": "refine", "label": "Refine" + (" (wide)" if opts["refine"] == "wide" else ""),
                           "detail": "1-swap hill-climb, leader fixed"})
    hard = opts.get("difficulty") or "EXPERT"
    suffix = "" if hard == "EXPERT" else f" ({hard})"
    if opts["song"]:
        stages.append({"id": "rank", "label": f"Score on {opts['song']}{suffix}"})
    elif opts["across"]:
        stages.append({"id": "rank", "label": f"Across every chart{suffix}"})
    else:
        stages.append({"id": "rank", "label": "Unit Score"})
    return stages


def _roster_key(leader_id: str, member_ids) -> str:
    """`leader|m1|m2|m3|m4|m5` with the members sorted — ranking.js's own
    `rosterKey`, so the client resolves `payload.explain` by the same string."""
    return "|".join([leader_id, *sorted(member_ids)])


# -- Event Points mode ------------------------------------------------------------
# The newest open event's bonus rows (`dumps/marathon_bonus.json`, written by every
# refresh) and the account-basis search ranked by Event Points / Event Song Score.
# `tools/web/event_mode.py` holds the logic; these are the reads and the job.
EVENT_BONUS_PATH = PROXY_DIR / "dumps" / "marathon_bonus.json"
# The event's name, kind, times and banner (`event_art.py`, a refresh step). Absent ->
# the payload's title/art fields are null; a missing banner at /eventart/ is a 404.
EVENT_META_PATH = PROXY_DIR / "dumps" / "event_meta.json"
EVENT_ART_DIR = PROXY_DIR / "dumps" / "event_art"
# every event seen + the account's result (`event_history.py`, a refresh step)
EVENT_HISTORY_PATH = PROXY_DIR / "dumps" / "event_history.json"
_EVENT_ART_RE = re.compile(r"^[A-Za-z0-9_-]+\.webp$")
EVENT_STAGES = (
    {"id": "build", "label": "Account basis",
     "detail": "every owned card at its real level and bloom, boards rebuilt for the song"},
    {"id": "score_search", "label": "Search by score", "detail": "two-stage search; its best is S_max"},
    {"id": "event_search", "label": "Search by event objective",
     "detail": "the same search ranking by the event objective"},
    {"id": "bound", "label": "Bound", "detail": "every member set that could still win, exact-scored"},
)


def _event_unavailable(exc: Exception) -> dict:
    return {"available": False, "error": (
        f"no current event: {exc}. The refresh writes tools/recon/proxy/dumps/"
        "marathon_bonus.json (Event/ListEventInfo); run tools/recon/proxy/refresh_all.py.")}


def _chr_names(cards) -> dict[str, str]:
    names: dict[str, str] = {}
    for card in cards:
        parts = card.id.split("-")
        if len(parts) >= 2:
            names.setdefault(f"chr-{parts[1]}", card.member)
    return names


def _music_rows() -> list[dict]:
    rows = _read_json(MUSIC_PATH)
    if not isinstance(rows, list):
        return []
    return [r.get("data") or {} for r in rows if isinstance(r, dict)]


def _chart_note_counts(difficulty: str) -> dict[str, int] | None:
    raw = _read_json(_charts_path(difficulty))
    if not isinstance(raw, dict):
        return None
    out: dict[str, int] = {}
    for chart in (raw.get("charts") or {}).values():
        if str((chart or {}).get("difficulty") or "").upper() == difficulty:
            out[str(chart.get("music_id") or "")] = len(chart.get("notes") or [])
    return out


def run_search_job(job: Job, opts: Mapping, *, engine_for=None) -> None:
    """The job behind `POST /api/search` (the optimiser on demand):
    `ondemand.engine(basis, difficulty)` -- started once, kept alive
    -- then one joint search of the song. Events: `plan` -> `progress` (per
    round, with `best_so_far`) -> `result{payload}` -> `end`; or `cancelled` /
    `error` then `end`. `engine_for(basis, difficulty)` replaces the engine
    (tests)."""
    import ondemand  # noqa: PLC0415
    import two_stage_view  # noqa: PLC0415
    from job_progress import JobProgress  # noqa: PLC0415

    emit = JobProgress(job.events.put)
    t0 = time.monotonic()
    try:
        emit({"type": "plan", "stages": [{"id": "build", "label": "Load your account"},
                                         {"id": "search", "label": "Search every leader and team"},
                                         {"id": "refine", "label": "Refine the best teams"}],
              "opts": {k: v for k, v in opts.items() if k not in ("filters", "assumptions")}})
        basis, difficulty = opts["basis"], opts["difficulty"]
        assumptions = opts.get("assumptions")
        # the engine keeps the account loaded between searches and reloads it when a
        # refresh moved its inputs (`ondemand.account_key`): `build` is instant when warm
        emit({"type": "stage", "stage": "build", "state": "active"})
        if engine_for is None:
            levels = opts.get("levels")
            if assumptions is not None:
                eng = ondemand.engine(basis, difficulty, workers=_web_workers(),
                                      assumptions=assumptions, levels=levels,
                                      play=opts.get("play") or ("mobile", False),
                                      current_key=lambda: ondemand.account_key(difficulty))
            else:
                eng = ondemand.engine(
                    basis, difficulty, workers=_web_workers(), levels=levels,
                    play=opts.get("play") or ("mobile", False),
                    current_key=lambda: (two_stage_view.current_keys(basis, difficulty)
                                         or (None, None))[1])
        else:
            eng = engine_for(basis, difficulty)
        emit({"type": "stage", "stage": "build", "state": "done"})
        emit({"type": "stage", "stage": "search", "state": "active"})
        payload = eng.search(opts["song"], opts["filters"], emit=emit, cancel=job.cancel,
                             top=opts.get("top") or ondemand.RANKING)
        emit({"type": "result", "payload": payload,
              "elapsed_ms": round((time.monotonic() - t0) * 1000)})
    except ondemand.Cancelled:
        emit({"type": "cancelled", "elapsed_ms": round((time.monotonic() - t0) * 1000)})
    except KeyError as exc:
        emit({"type": "error", "message": str(exc.args[0] if exc.args else exc)})
    except Exception as exc:  # noqa: BLE001 — every failure is an event, never a dead stream
        import traceback  # noqa: PLC0415

        print(traceback.format_exc(), flush=True)
        emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        job.done = True
        emit({"type": "end"})


def run_event_job(job: Job, opts: Mapping, *, runner=None) -> None:
    """The job behind `POST /api/event/run`. Events: `plan` -> `stage` / `progress`
    (per stage) -> `result{payload}` -> `end`; or `cancelled` / `error` then `end`.
    `runner` replaces `event_mode.run_account_search` (tests)."""
    import event_mode  # noqa: PLC0415
    from job_progress import JobProgress  # noqa: PLC0415

    emit = JobProgress(job.events.put)     # + eta_s / best_so_far on progress events
    t0 = time.monotonic()
    try:
        emit({"type": "plan", "stages": [dict(s) for s in EVENT_STAGES], "opts": dict(opts)})
        _raw, event, _keys = event_mode.load_event_bundle(EVENT_BONUS_PATH, opts.get("event"))
        collection = ((_read_json(COLLECTION_PATH) or {}).get("cards") or {})
        titles, singers = _song_labels()
        seconds = next((d.get("playingSeconds") for d in _music_rows()
                        if d.get("id") == opts["song"]), None)
        song_info = {"title": titles.get(opts["song"]), "singer": singers.get(opts["song"]),
                     "seconds": seconds}
        if event_mode.is_highlight(opts["song"]):
            # the pool is built from every song's singers and the charts on disk (reads)
            song_info = {"title": "Highlight", "music_rows": _music_rows(), "titles": titles,
                         "singers": singers,
                         "charted": set(_chart_note_counts(opts["difficulty"]) or ())}
        run = runner or event_mode.run_account_search
        payload = run(opts, emit=emit, cancel=job.cancel, event=event, collection=collection,
                      song_info=song_info, workers=opts.get("workers") or _web_workers())
        emit({"type": "result", "payload": payload,
              "elapsed_ms": round((time.monotonic() - t0) * 1000)})
    except event_mode.EventCancelled:
        emit({"type": "cancelled", "elapsed_ms": round((time.monotonic() - t0) * 1000)})
    except Exception as exc:  # noqa: BLE001 — every failure is an event, never a dead stream
        import traceback  # noqa: PLC0415

        print(traceback.format_exc(), flush=True)
        emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        job.done = True
        emit({"type": "end"})


def run_job(job: Job, opts: Mapping, art_dir: Path) -> None:
    """The single job behind `POST /api/run`. Events, in order:
    `plan` → `stage`/`progress`/`fact` (per stage) → [`developed`] → `result`
    (with `warnings[]`, `notice[]`, `explain{roster: {...}}`) → `end`; or
    `cancelled{stage}` / `error{stage, message}` then `end`. Nothing is
    drawn from a stage-1 payload when a develop follows: the ONLY `result`
    is the final one. Every `progress` event carries `elapsed_s`, `eta_s` and
    `best_so_far` (`job_progress.JobProgress`)."""
    from job_progress import JobProgress  # noqa: PLC0415

    emit = progress = JobProgress(job.events.put)
    t0 = time.monotonic()
    warnings: list[str] = []
    notices: list[str] = []
    develop = opts["board"] == "develop"
    stage = "search"
    try:
        emit({"type": "plan", "stages": run_plan(opts), "opts": {
            k: (list(v) if isinstance(v, tuple) else v) for k, v in opts.items()}})
        emit({"type": "stage", "stage": "search", "state": "active"})
        # -- stage 1: the /solve job on an inner Job sharing our cancel flag ----
        inner = Job(job_id=job.job_id + ":search", cancel=job.cancel)
        # develop_top rosters must come out of stage 1 (the funnel), and the
        # every-leader path ignores the posted teams, so 1 suffices there
        inner_top = (max(opts["top"], opts["develop_top"]) if develop and not opts["leaders"]
                     else opts["top"])
        # a leader CARD is the solve's own leader pin; a leader holomem and "no
        # leader overlap" filter the candidates (stage 1 keeps a wider funnel)
        leader_pin = opts["leader"] or ((opts["leader_card"],) if opts.get("leader_card") else ())
        _leader_filters = bool(opts.get("leader_member") or opts.get("no_leader_overlap"))
        if _leader_filters:
            inner_top = min(50, max(inner_top, 3 * opts["top"]))
        worker = threading.Thread(
            target=run_solve_job,
            args=(inner, inner_top, art_dir, opts["basis"], opts["board"] == "current",
                  opts["song"], opts["pool"], opts["require"], leader_pin,
                  opts["exclude"], opts["include_unowned"], opts["exact"], False,
                  opts["across"], opts["uptime_model"], opts["assume_level"],
                  opts["assume_bloom"]),
            kwargs={"platform": opts["platform"],
                    "difficulty": opts.get("difficulty") or "EXPERT"},
            name=f"run-search-{job.job_id[:8]}", daemon=True)
        worker.start()
        payload = None
        while True:
            ev = inner.events.get()
            t = ev.get("type")
            if t == "end":
                break
            if t == "result":
                payload = ev["payload"]
            elif t == "error":
                emit({"type": "error", "stage": "search", "message": ev.get("message")})
                return
            elif t == "cancelled":
                emit({"type": "cancelled", "stage": "search",
                      "elapsed_ms": round((time.monotonic() - t0) * 1000)})
                return
            elif t == "progress":
                emit({"type": "progress", "stage": "search", "evaluated": ev.get("evaluated"),
                      "complete": bool(ev.get("complete"))})
            elif t == "song_progress":
                emit({"type": "progress", "stage": "search", "sub": "rank",
                      "done": ev.get("scored"), "total": ev.get("total")})
            elif t == "status":
                emit({"type": "fact", "stage": "search", "text": ev.get("message")})
            elif t == "projection":
                # the exact search's `evaluated` counts arrangements; the
                # heuristic's does not, so only its projected seconds apply
                progress.note_projection(
                    "search", seconds=ev.get("projected_seconds"),
                    total=ev.get("arrangement_count") if ev.get("solver") == "exact" else None)
                emit({"type": "fact", "stage": "search", "projection": ev})
            elif t in ("board", "collection"):
                emit(dict(ev, stage="search"))
                for line in ev.get("warnings") or []:
                    warnings.append(str(line))
            elif t == "estimate":
                emit({"type": "fact", "stage": "search", "estimate": ev})
        worker.join(timeout=5.0)
        if payload is None:
            emit({"type": "error", "stage": "search", "message": "stage 1 produced no result"})
            return
        emit({"type": "stage", "stage": "search", "state": "done",
              "teams": len(payload.get("teams") or [])})
        if job.cancel.is_set():
            emit({"type": "cancelled", "stage": "search",
                  "elapsed_ms": round((time.monotonic() - t0) * 1000)})
            return

        def _leader_ok(t: Mapping) -> bool:
            ids = [t.get("leader_id"), *(t.get("member_ids") or [])]
            members = {(payload.get("cards") or {}).get(c, {}).get("member") for c in ids[1:]}
            lead = (payload.get("cards") or {}).get(ids[0], {}).get("member")
            if opts.get("leader_member") and lead != opts["leader_member"]:
                return False
            return not (opts.get("no_leader_overlap") and lead in members)

        if _leader_filters:
            before = len(payload.get("teams") or [])
            payload["teams"] = [t for t in payload.get("teams") or [] if _leader_ok(t)]
            if len(payload["teams"]) < before:
                notices.append(f"leader options: {before - len(payload['teams'])} of {before} "
                               "stage-1 teams dropped")

        if not develop:
            # board off / as captured: the stage-1 payload IS the answer, with the
            # explain already keyed by roster — copy it under `explain`.
            payload["explain"] = {
                _roster_key(e["roster"][0], e["roster"][1:]): {
                    "lines": e["lines"], "contributions": e["contributions"], "terms": e["terms"]}
                for e in payload.get("explain_by_rank") or [] if e.get("roster")}
            payload["teams"] = payload["teams"][:opts["top"]]
            payload["warnings"] = warnings
            payload["notice"] = notices
            payload["run"] = {"opts": {k: (list(v) if isinstance(v, tuple) else v)
                                       for k, v in opts.items()},
                              "elapsed_ms": round((time.monotonic() - t0) * 1000)}
            emit({"type": "stage", "stage": "rank", "state": "done"})
            emit({"type": "result", "payload": payload})
            return

        # -- stage 2..n: develop (+ every leader, + refine) + rank ---------------
        stage = "develop"
        rosters = [[t["leader_id"], *t["member_ids"]] for t in payload["teams"]
                   if t.get("leader_id") and len(t.get("member_ids") or []) == MEMBER_COUNT]
        rosters = rosters[:opts["develop_top"]]
        dev_body = {
            "basis": opts["basis"], "teams": rosters, "song": opts["song"],
            "across": opts["across"], "refine": opts["refine"],
            "leader_diverse": opts["leaders"], "pool": opts["pool"],
            "assume_level": opts["assume_level"], "assume_bloom": opts["assume_bloom"],
            "uptime_model": opts["uptime_model"], "platform": opts["platform"],
            "difficulty": opts.get("difficulty") or "EXPERT",
            # the seed rosters honour the run's pins (a violating seed is dropped)
            "exclude": list(opts["exclude"]), "require": list(opts["require"]),
            "leader": list(opts["leader"]),
        }
        if opts["budget"] != "real":
            dev_body["budget"] = opts["budget"]
        # `seeds` / `refine_leaf`: passed through when the client said; absent
        # -> the pipeline's own defaults (seeds with every-leader or refine;
        # plan-cache leaf).
        if opts.get("seeds") is not None:
            dev_body["seeds"] = opts["seeds"]
        if opts.get("refine_leaf"):
            dev_body["refine_leaf"] = opts["refine_leaf"]
        if opts.get("objective_leaf"):
            dev_body["objective_leaf"] = opts["objective_leaf"]
        for key in ("materials", "leader_member", "leader_card", "no_leader_overlap"):
            if opts.get(key):
                dev_body[key] = opts[key]
        current_stage = {"id": "develop"}

        def _forward(ev: dict) -> None:
            if ev.get("type") == "stage":
                current_stage["id"] = ev.get("stage") or current_stage["id"]
            emit(ev)

        try:
            doc = develop_pipeline(dev_body, art_dir=art_dir, emit=_forward, cancel=job.cancel)
        except _HttpError as exc:
            emit({"type": "error", "stage": current_stage["id"],
                  "message": exc.obj.get("error") if isinstance(exc.obj, dict) else str(exc)})
            return
        except _Cancelled:
            emit({"type": "cancelled", "stage": current_stage["id"],
                  "elapsed_ms": round((time.monotonic() - t0) * 1000)})
            return
        internal = doc.pop("_internal", {})
        cfg = internal.get("cfg")
        by_roster = internal.get("by_roster") or {}
        for w in doc.get("warnings") or []:
            warnings.append(str(w))
        # what the pipeline could not honour on this request
        if opts["leaders"] and (opts["require"] or opts["leader"] or opts["exclude"]):
            warnings.append("Every-leader mode ignores Must-include / Leader / Ban pins "
                            "(generated from the whole pool)")
        if opts["refine"] and opts["exclude"]:
            warnings.append("Refine does not honour Ban: a banned card may be swapped in")
        if opts["refine"] and opts["require"]:
            warnings.append("Refine does not honour Must-include: a required card may be swapped out")
        emit({"type": "stage", "stage": "develop", "state": "done", "teams": len(doc["ranking"])})
        if opts["refine"]:
            emit({"type": "stage", "stage": "refine", "state": "done",
                  "refined": sum(1 for r in doc["ranking"] if r.get("refined"))})
        emit({"type": "stage", "stage": "rank", "state": "active"})
        stage = "rank"

        # -- the FINAL payload: teams from the developed rows, breakdown/terms/
        #    explain computed for THIS FINAL roster on ITS developed board -----
        rows = doc["ranking"]
        sorted_by = doc.get("sorted_by")
        teams = []
        explain: dict[str, dict] = {}
        for i, r in enumerate(rows[:opts["top"]], start=1):
            t = {
                "rank": i, "leader_id": r["leader_id"], "member_ids": list(r["member_ids"]),
                "gates": r.get("gates") or [], "board_state": r.get("board_state"),
                "developed_points": r.get("points"), "developed_gain": r.get("gain_pct"),
                "is_lower_bound": False, "board_plan_summary": r.get("board_plan_summary"),
            }
            for k in ("member_order", "member_order_share", "deck_power",
                      "refined", "provenance", "certificate", "wins",
                      "song_max", "active_cover_pct", "active_overlap_loss_pct",
                      "active_dead_start_s",
                      # the plan-cache objective's labelled what-if (the
                      # per-team greedy re-develop: its number and its board)
                      "objective_leaf", "develop_whatif", "board_state_develop",
                      "board_plan_summary_develop", "points_develop"):
                if k in r:
                    t[k] = r[k]
            if sorted_by == "song_est":
                t["song_score"] = r["song_est"]
                t["developed_off"] = r.get("song_off")
                if "song_est_develop" in r:
                    t["song_score_develop"] = r["song_est_develop"]
            elif sorted_by == "across_song":
                t["across_score"] = r["across_score"]
                if "across_score_develop" in r:
                    t["across_score_develop"] = r["across_score_develop"]
            else:
                t["score"] = r["optimised_us"]
                t["developed_off"] = r.get("board_off_us")
            teams.append(t)
            bt = by_roster.get((r["leader_id"], frozenset(r["member_ids"])))
            if bt is not None and cfg is not None:
                bd = score_with_breakdown(bt.team, dataclasses.replace(cfg, board=bt.board))
                cand = RankedTeam(score=bd.total_score, leader_id=bt.team.leader.id,
                                  member_ids=tuple(sorted(m.id for m in bt.team.members)),
                                  team=bt.team)
                explain[_roster_key(r["leader_id"], r["member_ids"])] = {
                    "lines": list(format_explain(i, cand, bd)),
                    "contributions": build_team_object(
                        i, cand, bd, with_contributions=True)["contributions"],
                    "terms": build_score_terms(bd, board_modelled=True),
                    "board": "developed",
                }
        payload["teams"] = teams
        payload["cards"] = {**(payload.get("cards") or {}), **(doc.get("cards") or {})}
        payload["sorted_by"] = "song_score" if sorted_by == "song_est" else (
            "across_song" if sorted_by == "across_song" else "score")
        if sorted_by == "across_song":
            payload["across"] = {"chart_count": doc.get("chart_count"), "pool": len(rows)}
        payload["explain"] = explain
        payload["explain_by_rank"] = []          # stage-1 attribution is not this roster's
        payload["explain_lines"] = []
        payload["_developed"] = True
        payload["developed"] = {
            "mode": "leaders" if opts["leaders"] else ("max" if opts["budget"] != "real" else "account"),
            "budget": ("every leader · full tree" if opts["leaders"]
                       else "full tree, from scratch" if opts["budget"] == 453
                       else f"{opts['budget']} pt per character, from scratch"
                       if isinstance(opts["budget"], int) else "my points, from scratch"),
            "developed": len(rows), "funnel": len(rosters), "refine": opts["refine"],
        }
        for k in ("model_version", "inputs_hash", "inputs_hash_error", "certificate_basis",
                  "scenario", "objective", "members_first"):
            if k in doc:
                payload[k] = doc[k]
        # the members-first seeds, labelled honestly in the run header: own
        # enumeration vs TRANSFER from other songs' (HEURISTIC either way —
        # the per-row certificate pill is the only thing that says EXACT)
        _mfm = doc.get("members_first") or {}
        if _mfm.get("available"):
            _rec = _mfm.get("measured_recall") or {}
            notices.append(
                f"members-first seeds ({_mfm.get('leaders_seeded', 0)} leaders, K={_mfm.get('k')}, "
                + ("this song's own enumeration" if _mfm.get("mode") == "own" else
                   "TRANSFER from the enumeration(s) of "
                   + "+".join(_mfm.get("source_songs") or []) + " — HEURISTIC candidates"
                   + (", measured recall: " + "; ".join(f"{k}: {v}" for k, v in _rec.items())
                      if _rec else ", transfer loss not measured for this song"))
                + ")")
        payload["warnings"] = warnings
        payload["notice"] = notices
        payload["run"] = {"opts": {k: (list(v) if isinstance(v, tuple) else v)
                                   for k, v in opts.items()},
                          "elapsed_ms": round((time.monotonic() - t0) * 1000),
                          "searched": inner_top,
                          "developed": len(rows), "refined": sum(1 for r in rows if r.get("refined")),
                          "seeded": sum(1 for r in rows if r.get("provenance"))}
        emit({"type": "stage", "stage": "rank", "state": "done"})
        emit({"type": "result", "payload": payload})
    except _Cancelled:
        emit({"type": "cancelled", "stage": stage,
              "elapsed_ms": round((time.monotonic() - t0) * 1000)})
    except Exception as exc:  # noqa: BLE001 — a job thread must not die silently
        import traceback  # noqa: PLC0415

        print(traceback.format_exc(), flush=True)
        emit({"type": "error", "stage": stage, "message": f"{type(exc).__name__}: {exc}"})
    finally:
        job.done = True
        emit({"type": "end"})


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "hbuilder-web"
    art_dir: Path = DEFAULT_ART_DIR

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # -- helpers ------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _static(self, name: str) -> None:
        path = (STATIC_DIR / name).resolve()
        if not str(path).startswith(str(STATIC_DIR.resolve())) or not path.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".woff2": "font/woff2",
        }.get(path.suffix, "application/octet-stream")
        self._send(200, path.read_bytes(), ctype)

    # -- routes -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/":
            # The redesigned pages are the default; the previous UI
            # stays reachable at /classic.
            self.send_response(302)
            self.send_header("Location", "/optimizer")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        elif path in ("/classic", "/index.html"):
            self._static("index.html")
        elif (
            path.endswith((".js", ".css"))
            and path.count("/") == 1
            and ".." not in path
        ):
            # The page is a set of ES modules (`app.js` imports
            # `ranking.js`), so the route is by extension rather than by an
            # allowlist a new module silently falls off -- that omission is
            # a blank page with a bare console error. Still one level deep,
            # still no `..`, and `_static` re-checks containment under
            # STATIC_DIR after resolving, so this widens the file set by
            # exactly "the other .js/.css beside app.js" and nothing else.
            self._static(path.lstrip("/"))
        elif path == "/api/collection":
            self._serve_collection()
        elif path == "/api/board":
            self._serve_board()
        elif path == "/api/meta":
            self._serve_meta()
        elif path == "/api/songs":
            self._serve_songs()
        elif path == "/api/ranks":
            self._json(200, row_detail.load_ranks(PROXY_DIR / "dumps" / "holodori"))
        elif path == "/api/two-stage":
            self._serve_two_stage()
        elif path == "/api/event":
            self._serve_event()
        elif path == "/api/event/songs":
            self._serve_event_songs()
        elif path == "/api/event/cutoffs":
            self._serve_event_cutoffs()
        elif path == "/api/event/top":
            self._serve_event_top()
        elif path in ("/api/admin/me", "/api/admin/users"):
            self._admin_proxy("GET", "me" if path.endswith("/me") else "admin/users")
        elif path == "/api/event/history":
            self._serve_event_history()
        elif path == "/api/event/cutoffs/poller":
            self._json(200, _CUTOFFS_POLLER.status())
        elif path == "/event":
            self._static("event.html")
        elif path == "/api/park":
            self._serve_park()
        elif path == "/api/resources":
            self._serve_resources()
        elif path == "/api/gacha":
            self._serve_gacha()
        elif path.startswith("/gachaart/"):
            self._serve_gachaart(path[len("/gachaart/"):])
        elif path == "/api/live2d/index":
            self._serve_live2d_index()
        elif path == "/live2d/core.js":
            self._serve_live2d_file(None)
        elif path.startswith("/live2d/"):
            self._serve_live2d_file(path[len("/live2d/"):])
        elif path == "/api/chibi/index":
            self._serve_chibi_index()
        elif path.startswith("/chibi/"):
            self._serve_chibi_file(path[len("/chibi/"):])
        elif path.startswith("/park/"):
            self._serve_park_image(path[len("/park/") :])
        elif path.startswith("/art/"):
            self._serve_art(path[len("/art/") :])
        elif path.startswith("/cardart/"):
            self._serve_cardart(path[len("/cardart/") :])
        elif path.startswith("/jacket/"):
            self._serve_jacket(path[len("/jacket/") :])
        elif path.startswith("/eventart/"):
            self._serve_eventart(path[len("/eventart/") :])
        elif path.startswith("/chricon/"):
            self._serve_chricon(path[len("/chricon/") :])
        elif path.startswith("/itemicon/"):
            self._serve_itemicon(path[len("/itemicon/") :])
        elif path.startswith("/icon/"):
            self._serve_icon(path[len("/icon/") :])
        elif path.startswith("/chrome/"):
            self._serve_chrome(path[len("/chrome/") :])
        elif path.startswith("/ui/"):
            self._serve_ui_sprite(path[len("/ui/") :])
        elif path == "/font/line-seed-sans-xbd-latin.woff2":
            self._serve_font()
        elif (
            path.startswith("/fonts/")
            and path.endswith(".woff2")
            and path.count("/") == 2
            and ".." not in path
        ):
            # The redesign's self-hosted UI faces (Space Grotesk / Public Sans /
            # JetBrains Mono) live under static/fonts/. Same containment guard as
            # the .js/.css route: one dir deep, no `..`, re-checked in `_static`.
            self._static(path.lstrip("/"))
        elif path.startswith("/events/"):
            self._serve_events(path[len("/events/") :])
        elif _PAGE_RE.fullmatch(path) and (STATIC_DIR / f"{path[1:]}.html").is_file():
            # The redesign's pages (`static/<name>.html`, docs/redesign/): one route
            # for all of them, so a new page needs no server change. Lowercase
            # letters and dashes only, one level; checked last, so no API route
            # can be shadowed, and `_static` re-checks containment.
            self._static(f"{path[1:]}.html")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/solve":
            self._start_solve()
        elif path == "/acquire":
            self._start_acquire()
        elif path == "/api/boardplan":
            self._board_plan()
        elif path == "/api/optimize-rerank":
            self._optimize_rerank()
        elif path == "/api/run":
            self._start_run()
        elif path == "/api/event/run":
            self._start_event_run()
        elif path == "/api/event/points":
            self._event_points()
        elif path == "/api/search":
            self._start_search()
        elif path == "/api/board-plan-account":
            self._serve_board_plan_account()
        elif path == "/api/card-states":
            self._serve_card_states()
        elif path == "/api/board-doc":
            self._serve_board_doc()
        elif path == "/api/board-plan-fast":
            self._board_plan_fast()
        elif path == "/api/collection":
            self._save_collection()
        elif path == "/api/admin/user":
            q = parse_qs(urlparse(self.path).query)
            uid, action = (q.get("id") or [""])[0], (q.get("action") or [""])[0]
            if not uid.isdigit() or action not in ("approve", "revoke", "delete"):
                self._json(400, {"error": "id and action (approve | revoke | delete) needed"})
            else:
                self._admin_proxy("POST", f"admin/user?id={uid}&action={action}")
        elif path == "/api/refresh":
            # ⛔ Guarded. An exception escaping a handler makes `http.server`
            # emit an HTML error page, and the page then reports a JSON parse
            # error naming a column number -- which says nothing about what
            # went wrong. A handler that can only fail as JSON is a handler
            # whose failures are readable.
            if not _REFRESH_LOCK.acquire(blocking=False):
                self._json(409, {"error": "a refresh is already running; wait for it to finish "
                                          "(the server console shows its progress)"})
                return
            try:
                self._refresh()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                # the page went away before the reply (a reload): the refresh itself ran
                # to the end; `handle_error` logs the one line
                raise
            except Exception as exc:  # noqa: BLE001
                import traceback  # noqa: PLC0415

                detail = traceback.format_exc()
                print(detail, flush=True)
                self._json(500, {
                    "returncode": -1,
                    "stdout": "",
                    "stderr": f"{type(exc).__name__}: {exc}\n\n{detail}",
                })
            finally:
                _REFRESH_LOCK.release()
        elif path == "/api/row-detail":
            self._row_detail()
        elif path == "/api/timeline":
            self._serve_timeline()
        elif path.startswith("/cancel/"):
            self._cancel(path[len("/cancel/") :])
        elif path == "/api/gacha/sync":
            self._gacha_sync()
        elif path == "/api/event/cutoffs/sync":
            self._event_cutoffs_sync()
        elif path == "/api/event/cutoffs/poller":
            self._event_cutoffs_poller()
        elif path == "/api/gacha/export":
            self._gacha_export()
        elif path == "/api/compare":
            self._compare()
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    # -- the board editor ---------------------------------------------------
    def _node_costs(self) -> dict[str, int]:
        """Point cost per node group, master `SkillTreeNode` field 9.

        The cost does not vary across a group's rows (0 of 152
        disagree), so the first row decides and the 15 multi-row groups need no
        special case here. They still do for EFFECTS, which is `build_board`'s
        problem, not this endpoint's."""
        rows = _read_json(NODE_MASTER_PATH) or []
        costs: dict[str, int] = {}
        for row in rows:
            data = row.get("data") or {}
            group = (data.get("1") or [None])[0]
            if group is not None and group not in costs:
                costs[group] = int((data.get("9") or [0])[0])
        return costs

    def _serve_two_stage(self) -> None:
        """The cached two-stage answer for `?song=<music id>`, or in general
        with no song (`two_stage_view.payload`). Read-only: the cache is built
        by `two_stage_build.py`, which a refresh starts in the background."""
        import two_stage_view  # noqa: PLC0415

        query = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        basis = (query.get("basis") or ["maxed"])[0]
        try:
            difficulty = _parse_difficulty({"difficulty": (query.get("difficulty") or [""])[0]})
        except _HttpError as exc:
            self._json(exc.code, exc.obj)
            return
        if (query.get("scope") or [""])[0] == "singers":
            self._json(200, two_stage_view.singers_payload(basis=basis, difficulty=difficulty))
            return
        try:
            top = max(1, int((query.get("top") or [two_stage_view.RANKING])[0]))
        except ValueError:
            self._json(400, {"error": "top must be a positive integer"})
            return
        self._json(200, two_stage_view.payload((query.get("song") or [""])[0], basis=basis,
                                               difficulty=difficulty, top=top))

    def _serve_songs(self) -> None:
        """The songs the page may rank against, with what each one is.

        The engine has `--song` and `--across-songs`; this route gives the page
        the list to choose from. (`song.timeline()` produces the five lanes, the
        chart's SPECIAL cues and every ACTIVE's cooldown windows for a choice.)

        ⚠ **EXPERT by default, and that is a decision rather than an accident.**
        The ranking mode is Expert, and
        `ChartLibrary.at` records the same reason: a picker that silently mixed
        four difficulties would offer four rows per song that score differently
        with nothing on screen saying why.

        What each song carries is what makes a chart score differently, so the
        picker can be read rather than guessed at: the difficulty level, the
        note count, and the five SPECIAL cue times. The cues are the reason
        slot order matters at all -- cue `i` fires the `i`-th member's SPECIAL
        (checked on 704/704 charts) -- so a song whose cues
        land early is a different question from one whose cues land late.

        Absent files are first-class, never an error: `charts.json` is
        gitignored account-adjacent data and a fresh checkout has none, in which
        case this reports `available: false` with the command that builds it,
        exactly as `_serve_board` does for the skill tree.

        `?difficulty=EASY|NORMAL|HARD|EXPERT` lists that
        difficulty's charts instead (charts_all.json for the three that are not
        EXPERT); absent -> EXPERT; unknown -> 400. Each
        response names the difficulties on disk under `difficulties`."""
        query = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        try:
            difficulty = _parse_difficulty({"difficulty": (query.get("difficulty") or [""])[0]})
        except _HttpError as exc:
            self._json(exc.code, exc.obj)
            return
        charts_file = _charts_path(difficulty)
        raw = _read_json(charts_file)
        if not raw:
            self._json(
                200,
                {
                    "available": False,
                    "reason": (
                        f"{charts_file.name} is not on disk (it lives under "
                        f"gitignored dumps/). Build it with "
                        f"tools/recon/proxy/build_charts.py."
                    ),
                    # No songs, so no filters either -- but the key is always
                    # present, so the page never has to guess its absence.
                    "filters_available": False,
                    "songs": [],
                },
            )
            return

        charts = raw.get("charts") or {}

        # Titles are a nicety and their absence must not cost the feature. The
        # ids stay the value the solver is given either way, so a page rendered
        # without titles asks the SAME question as one rendered with them.
        titles, singers = _song_labels()

        # The filter metadata -- singer type, real cast, cover/original,
        # playing time -- from the game's own Music master. Same first-class
        # absence contract as everything else on this server: no file means
        # `filters_available: false` and every song still ships the keys with
        # empty values, so the page says the FILTERS are unavailable rather
        # than showing empty menus (or KeyError-ing on a fresh checkout).
        music_rows = _read_json(MUSIC_PATH)
        music: dict[str, Mapping[str, Any]] = {}
        for row in music_rows if isinstance(music_rows, list) else []:
            data = (row or {}).get("data") or {}
            music_id = str(data.get("id") or "")
            if music_id:
                music[music_id] = data
        filters_available = bool(music)

        # chr-id -> holomem name, from the catalogue: `card-00015-...` belongs
        # to `chr-00015` and carries `member`. A character no card names keeps
        # its id as the label, so a gap is visible rather than blank.
        names: dict[str, str] = {}
        for card in (_read_json(CARDS_PATH) or {}).get("cards", []):
            parts = str(card.get("id") or "").split("-")
            if len(parts) >= 2 and card.get("member"):
                names.setdefault(f"chr-{parts[1]}", str(card["member"]))

        songs = []
        for key in sorted(charts):
            chart = charts[key] or {}
            if str(chart.get("difficulty") or "").upper() != difficulty:
                continue
            music_id = str(chart.get("music_id") or "")
            cues = list(chart.get("special_cues") or [])
            row = music.get(music_id) or {}
            character_ids = [str(c) for c in (row.get("characterIds") or []) if c]
            songs.append({
                "music_id": music_id,
                "difficulty": difficulty,
                # `title` is the label; `titled` says whether it is the song's
                # real name or the id standing in for one. A page that could
                # not tell them apart would present `m0042` as a song title.
                "title": titles.get(music_id) or music_id,
                "titled": music_id in titles,
                "singer": singers.get(music_id) or "",
                "difficulty_level": chart.get("difficulty_level"),
                "notes": len(chart.get("notes") or []),
                "special_cues": cues,
                "singer_type": _SINGER_TYPES.get(
                    str(row.get("musicSingerType") or "").rpartition("_")[2], ""
                ),
                "category": _CATEGORIES.get(
                    str(row.get("categoryType") or "").rpartition("_")[2], ""
                ),
                "seconds": row.get("playingSeconds"),
                # when the song entered the game (Music.startTime, epoch ms; the
                # launch songs all carry the launch date) -- the picker's sort
                "release": int(row.get("startTime") or 0) or None,
                "character_ids": character_ids,
                "characters": [
                    {"id": chr_id, "name": names.get(chr_id, chr_id)}
                    for chr_id in character_ids
                ],
            })

        self._json(
            200,
            {
                "available": True,
                "difficulty": difficulty,
                "difficulties": list(_difficulties_listing()["available"]),
                "source": raw.get("source") or str(charts_file.name),
                "titles_available": bool(titles),
                "filters_available": filters_available,
                "songs": songs,
            },
        )

    def _serve_resources(self) -> None:
        """Everything the account holds, from the captured on-disk state: the board
        MATERIALS (cubes/hexas spent on nodes + the unspent wallet) and the upgrade
        CURRENCIES (beads/crystals, hololium, lesson points, hologold). Absent is a
        first-class `{available: False}` with the refresh hint — the same contract
        the board/collection views use; nothing here writes or logs a credential."""
        precompute = _load_precompute()
        if precompute is None:
            self._json(200, {"available": False, "reason": (
                "could not import the recon tools (they need the proxyenv "
                "interpreter this server runs under).")})
            return
        to = precompute.to
        try:
            prices = to._prices()
            owned = to._owned_nodes()
            spent, _spent_points = to._spent(owned, prices)
            balance, source = to.balance_from_disk()
            balance = balance or {}
        except Exception as exc:  # noqa: BLE001 — report as a first-class absence
            self._json(200, {"available": False,
                             "reason": f"board/inventory capture unavailable: {exc}"})
            return

        # Iterate CURRENCIES (not the dicts' own keys) so all 8 cube/hexa lines
        # always appear, even a genuinely-zero one _spent drops.
        board_materials = [
            {
                "name": to.PRETTY.get(cur, cur),
                "spent": int(spent.get(cur, 0)),
                "wallet": int(balance.get(cur, 0)),
                "total": int(spent.get(cur, 0)) + int(balance.get(cur, 0)),
                "item_id": cur,
            }
            for cur in to.CURRENCIES
        ]
        doc = {
            "available": True,
            "captured_at": source,
            "board_points_spent": int(spent.get("points", 0)),
            "board_materials": board_materials,
            # The upgrade currencies (beads/crystals, hololium, lesson pt, hologold)
            # are folded in by `_account_currencies` once the item mapping lands.
            "currencies": _account_currencies(),
        }
        doc.update(_account_items())  # every held item + holomem points (Inventory page)
        self._json(200, doc)

    def _serve_park(self) -> None:
        """Today's hidden collectables, joined to the offline export.

        Two halves: the geometry (`park_spots.json`, static) and the day's pick
        (a LIVE `User/Get`). Both absences are first-class states with the
        command that fixes them, exactly as `_serve_board` / `_serve_songs` do
        -- never a 500, never a silent empty list.

        ⚠ This route triggers a live login. The server does not call it on page
        load or at startup; the page requests it (behind a lazy view
        open), and `_cached_park_live_state` bounds the frequency with a TTL +
        lock so a stray refresh cannot hammer the account."""
        export = _park_export()
        if export is None:
            self._json(200, {
                "available": False,
                "reason": (
                    f"{PARK_SPOTS_PATH.name} is not on disk (it lives under "
                    f"gitignored dumps/). Build it with "
                    f"tools/recon/proxy/park_export.py."
                ),
                "areas": [],
            })
            return
        try:
            state = _cached_park_live_state("main")
        except _ParkUnavailable as exc:
            self._json(200, {"available": False, "reason": exc.reason, "areas": []})
            return
        self._json(200, _join_park(export, state))

    def _serve_live2d_index(self) -> None:
        """`GET /api/live2d/index` -> `{available, core, by_card: {card id: asset},
        models: {asset: {model, motion, expression}}}`: which leader cards have a
        converted Live2D figure, so the page never asks for one that is not there."""
        index = _read_json(LIVE2D_DIR / "index.json") or {}
        models = {a: {"model": f"/live2d/{m['model']}", "motion": m.get("motion"),
                      "expression": m.get("expression")}
                  for a, m in (index.get("models") or {}).items()}
        core = bool(models) and _live2d_core()
        self._json(200, {"available": core, "core": core, "by_card": index.get("by_card") or {},
                         "models": models, "written": index.get("written")})

    def _serve_live2d_file(self, rel: str | None) -> None:
        """A converted Live2D file under `LIVE2D_DIR` (or, `rel` None, the Cubism Core).
        Guarded like the image routes: a strict name pattern AND the resolved path must
        stay under the folder; a missing file is a 404, never a substitute."""
        if rel is None:
            _live2d_core()
            path, kind = LIVE2D_CORE, "text/javascript; charset=utf-8"
            base = LIVE2D_CORE.parent
        else:
            m = _LIVE2D_PATH_RE.match(rel)
            if not m:
                self._send(400, b"bad live2d path", "text/plain; charset=utf-8")
                return
            path, base = LIVE2D_DIR / rel, LIVE2D_DIR
            kind = _LIVE2D_TYPES[rel.rsplit(".", 1)[1]]
        body = _asset_bytes(base, path)
        if body is None:
            self._send(404, b"no such live2d file", "text/plain; charset=utf-8")
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _serve_chibi_index(self) -> None:
        """`GET /api/chibi/index` -> `{available, outfits: {SdCostume id: {chr, name, typ,
        model}}}`: the converted 3D chibis, so the page only ever asks for one that exists."""
        index = _read_json(CHIBI_DIR / "index.json") or {}
        outfits = {k: {**{f: v.get(f) for f in ("chr", "name", "typ")}, "model": f"/chibi/{v['model']}"}
                   for k, v in (index.get("outfits") or {}).items()
                   if v.get("model") and _asset_known(CHIBI_DIR, CHIBI_DIR / v["model"])}
        self._json(200, {"available": bool(outfits), "outfits": outfits})

    def _serve_chibi_file(self, rel: str) -> None:
        """A converted chibi `.glb` under `CHIBI_DIR`, guarded like the Live2D files."""
        path = CHIBI_DIR / rel
        if not _CHIBI_PATH_RE.match(rel):
            self._send(400, b"bad chibi path", "text/plain; charset=utf-8")
            return
        body = _asset_bytes(CHIBI_DIR, path)
        if body is None:
            self._send(404, b"no such chibi file", "text/plain; charset=utf-8")
            return
        self.send_response(200)
        self.send_header("Content-Type", "model/gltf-binary")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _serve_park_image(self, name: str) -> None:
        """A RAW area map, from `dumps/park_export/park-<area>.png`.

        Path handling on this route is the kind of thing that is quietly wrong,
        so it is guarded twice, exactly as `_serve_icon` is: the regex forbids
        `..`, `/` and any `.` but the extension, AND the resolved path must
        still live under `PARK_EXPORT_DIR`. A missing file is a 404 (the same
        first-class-absence contract the other image routes keep), never a
        substituted placeholder."""
        if not _PARK_IMAGE_NAME_RE.match(name):
            self._send(400, b"bad park image name", "text/plain; charset=utf-8")
            return
        # `park-` prefix: the URL carries the area id (`area-a01.png`), the file
        # is `park-area-a01.png` -- the export's own filename convention.
        path = PARK_EXPORT_DIR / f"park-{name}"
        if (
            not path.is_file()
            or PARK_EXPORT_DIR.resolve() not in path.resolve().parents
        ):
            self._send(404, b"no park image", "text/plain; charset=utf-8")
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _row_detail(self) -> None:
        """`POST /api/row-detail` — ONE number set for a two-stage / event row,
        re-developed on the leaf that ranked it (`row_detail.two_stage_detail`):
        song figures, slot order, deck power, board state, Overall Power lines,
        the Score Bonus split and, on request, the timeline and the run law.

        Body: `{"song", "difficulty"?, "basis": "account"|"maxed"|"account-max",
        "cards": [leader, m1..m5], "refine"?: bool (default: the cached row's own
        flag; event rows pass false), "member_order"?, "timeline"?: bool,
        "distribution"?: bool, "as_is"?: bool (default true: `song_est_as_is` on the
        account's captured boards + `plan_cost` of the planned boards vs the
        wallet, `row_detail._as_is`), "reset_price"?: bool (a within-wallet row
        with resets: the same team re-planned with fewer, `row_detail.reset_price`;
        a few plans more, so the page asks for it on its own), "pool"?: [music ids] (a
        Highlight row on the WHOLE pool, `row_detail.pool_detail`: one board and one slot
        order for every song; `song` is then any of them)}`. Read-only."""
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            body = {}
        song = str(body.get("song") or "").strip()
        ids = [str(c) for c in (body.get("cards") or []) if c]
        if not song or len(ids) != MEMBER_COUNT + 1:
            self._json(400, {"error": f"need a song and {MEMBER_COUNT + 1} card ids "
                                      "(leader + 5 members)"})
            return
        basis = str(body.get("basis") or "account")
        import two_stage_build as tsb  # noqa: PLC0415
        if basis not in tsb.BASES:
            self._json(400, {"error": f"basis must be one of {sorted(tsb.BASES)}"})
            return
        try:
            difficulty = _parse_difficulty(body, song)
        except _HttpError as exc:
            self._json(exc.code, exc.obj)
            return
        refine = body.get("refine") if isinstance(body.get("refine"), bool) else None
        order = body.get("member_order") if isinstance(body.get("member_order"), list) else None
        assumptions = None
        if isinstance(body.get("assumptions"), dict):
            import ondemand  # noqa: PLC0415
            try:
                assumptions = ondemand.parse_assumptions(body["assumptions"])
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": f"assumptions: {exc}"})
                return
        pool = [str(s) for s in body.get("pool") or [] if s] if isinstance(body.get("pool"), list) else []
        try:
            if pool:
                # a Highlight row on the WHOLE pool: one board, one slot order
                # (`row_detail.pool_detail`); `song` is any pool song (the difficulty's)
                out = row_detail.pool_detail(basis, pool, difficulty, ids[0], ids[1:],
                                             assumptions=assumptions, refine=refine is not False)
                out.update({"available": True, "difficulty": difficulty, "basis": basis,
                            "leader_id": ids[0], "member_ids": ids[1:]})
                self._json(200, out)
                return
            out = row_detail.two_stage_detail(
                basis, song, difficulty, ids[0], ids[1:], refine=refine, assumptions=assumptions,
                want_distribution=body.get("distribution") is True,
                want_timeline=body.get("timeline") is True,
                member_order=[str(c) for c in order] if order else None,
                as_is=body.get("as_is") is not False,
                want_reset_price=body.get("reset_price") is True)
        except KeyError as exc:
            self._json(400, {"error": str(exc)})
            return
        except (TeamError, BoardError, SongError, ValueError) as exc:
            self._json(400, {"error": f"{type(exc).__name__}: {exc}"})
            return
        except SystemExit as exc:      # a builder that cannot find the chart
            self._json(400, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 — report as JSON, never HTML 500
            import traceback  # noqa: PLC0415

            print(traceback.format_exc(), flush=True)
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        out.update({"available": True, "song": song, "difficulty": difficulty, "basis": basis,
                    "leader_id": ids[0], "member_ids": ids[1:]})
        self._json(200, out)

    def _serve_timeline(self) -> None:
        """When each of a team's five members is actually live, on one axis.

        `song.timeline()` produces this -- five lanes, the chart's own SPECIAL
        cues, and every ACTIVE's cooldown lattice. This is the route that hands
        it to the page.

        ⛔ **The slot order is COMPUTED, never taken from the caller.** A chart
        always has five SPECIAL cues and cue `i` fires the `i`-th member's
        SPECIAL, so which member sits in which slot changes the picture AND the
        score. `score_song_detailed` already finds the order that maximises the
        song, so the lanes drawn here are the lanes of the arrangement the
        scorer would actually recommend. Drawing the caller's incoming order
        instead would show a picture of a team nobody was told to field.

        Everything is loaded through the same `load_inputs` / `apply_collection`
        / `load_board_basis` the solve uses. A second definition of what a card
        is worth is this project's named expensive failure mode."""
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            body = {}

        music_id = str(body.get("song") or "")
        wanted = [str(c) for c in (body.get("cards") or []) if c]
        if not music_id or len(wanted) != MEMBER_COUNT + 1:
            self._json(400, {
                "error": (
                    f"need a song and exactly {MEMBER_COUNT + 1} card ids "
                    f"(1 leader + {MEMBER_COUNT} members); "
                    f"got song={music_id!r} and {len(wanted)} card(s)"
                ),
            })
            return

        try:
            difficulty = _parse_difficulty(body, music_id)
        except _HttpError as exc:
            self._json(exc.code, exc.obj)
            return
        charts_file = _charts_path(difficulty)
        raw = _read_json(charts_file)
        chart_doc = ((raw or {}).get("charts") or {}).get(f"{music_id}/{difficulty}")
        if not chart_doc:
            self._json(200, {
                "available": False,
                "reason": f"no {difficulty} chart for {music_id} in {charts_file.name}",
            })
            return

        basis_name = _parse_basis(body.get("basis"))
        use_board = body.get("board", True) is not False
        # `board_state` (the optimiser's {chr:{nodes,slots}}) regenerates the
        # timeline AGAINST THE ROW'S BOARD, rebuilt EXACTLY as the develop
        # assembled it (`row_detail.board_from_state`: the basis' Connect cards,
        # bench boards, cast names and singer context) — not a build_board
        # rebuild at Connect level 1 without the bench, which reads the deck
        # power 7.6 % low. Absent -> the captured board.
        board_state = body.get("board_state") if isinstance(body.get("board_state"), dict) else None
        # The SAME level/bloom what-if, uptime model, platform, deck-power basis
        # and AUTO premise the ranking was solved with.
        assume_level = _parse_assume(body, "assume_level", 1, 80)
        assume_bloom = _parse_assume(body, "assume_bloom", 0, 6)
        uptime_model = str(body.get("uptime_model") or "").lower()
        platform = _parse_platform(body)
        want_distribution = body.get("distribution") is True

        try:
            cards, owned, cfg, _prov = load_inputs(CARDS_PATH, OWNED_PATH, False)
            if uptime_model in ACTIVE_UPTIME_MODELS:
                cfg = dataclasses.replace(cfg, active_uptime_model=uptime_model)
            cfg = dataclasses.replace(cfg, platform=platform, **_deck_power_basis(body))
            cfg = dataclasses.replace(cfg, **_auto_premise(cfg, body))
            try:
                cards, collection_basis, cfg, ma_scenario = _apply_basis(
                    cards, owned, cfg, basis_name,
                    assume_level=assume_level, assume_bloom=assume_bloom)
            except MaxedAccountUnavailable as exc:
                self._json(503, {"error": str(exc)})
                return

            by_id = {c.id: c for c in cards}
            missing = [c for c in wanted if c not in by_id]
            if missing:
                self._json(400, {"error": f"unknown card id(s): {missing}"})
                return
            leader = by_id[wanted[0]]
            members = tuple(by_id[c] for c in wanted[1:])
            try:
                team = Team(leader=leader, members=members)
            except TeamError:
                # the timeline draws any six cards (it always has). The deck
                # screen's Overall Power is only defined for a legal team, so an
                # illegal roster is drawn on the modelled stat pool, labelled.
                team = types.SimpleNamespace(leader=leader, members=members)
                cfg = dataclasses.replace(cfg, song_absolute_deck_power=False)
            chart = charts_from_mapping(
                {"schema_version": raw.get("schema_version"),
                 "charts": {f"{music_id}/{difficulty}": chart_doc}},
                source=str(charts_file.name),
            ).get(music_id, difficulty)

            cfg = dataclasses.replace(cfg, board=None)
            optimised_model = None
            if board_state:
                dev = compare_view._Developer(cfg, cards, basis_name, None, chart, ma_scenario)
                try:
                    optimised_model, _placements = row_detail.board_from_state(
                        team, board_state, cfg=cfg, skilltree=dev.skilltree,
                        node_effect_ids=dev.effects, tree_for=dev.tree_for,
                        connect_cards=dev.connect_cards, bench_characters=dev.bench_characters,
                        name_by_chr=dev.name_by_chr, sings=dev.cast,
                        # a state carrying its sockets keeps them (the row's own board)
                        use_slots=any((v or {}).get("slots") for v in board_state.values()))
                except ValueError as exc:
                    self._json(400, {"error": f"board_state: {exc}"})
                    return
            if ma_scenario is not None:
                # the maxed-account "current" board is the scenario's bench
                captured = None if (BOARD_DISABLED or not use_board) else ma_scenario.board_model
            else:
                captured = load_board_basis(
                    None if (BOARD_DISABLED or not use_board) else BOARD_PATH).model
            board_model = optimised_model if optimised_model is not None else captured
            cfg_board = dataclasses.replace(cfg, board=board_model)

            fig = row_detail.figures(team, chart, cfg_board)
            drawing = timeline(members, chart, cfg_board, tuple(fig["member_order"]))
            law = (row_detail.distribution(team, chart, cfg_board, fig["member_order"],
                                           fig["deck_power"], row_score=fig["song_est"])
                   if want_distribution else None)
        except compare_view.CompareError as exc:
            self._json(exc.code, {"error": exc.message})
            return
        except (_LoadFailure, CollectionError, BoardError, SongError, TeamError) as exc:
            # First-class, like every other bad input on this server: a readable
            # reason beats an HTML error page whose JSON parse error names a
            # column number.
            self._json(200, {"available": False,
                             "reason": f"{type(exc).__name__}: {exc}"})
            return

        requested = body.get("member_order")
        out = {
            "available": True,
            "music_id": music_id,
            "difficulty": difficulty,
            "leader_id": leader.id,
            # the row's figures (row_detail.figures), the same numbers
            # /api/compare and /api/row-detail return for this board
            "total_score": fig["song_est"],
            "song_est": fig["song_est"],
            "max_score": fig["song_max"],
            "song_max": fig["song_max"],
            "deck_power": fig["deck_power"],
            "deck_power_basis": fig["deck_power_basis"],
            "member_order": fig["member_order"],
            "basis": basis_name,
            "board": board_model is not None,
            "optimised_board": optimised_model is not None,
            "board_rebuild": "exact (board_model_from_plan)" if optimised_model is not None else None,
            "timeline": drawing,
        }
        if isinstance(requested, list) and sorted(map(str, requested)) == sorted(fig["member_order"]):
            out["member_order_requested_matches"] = [str(c) for c in requested] == fig["member_order"]
        if law is not None:
            out["distribution"] = law
        self._json(200, out)

    def _serve_board(self) -> None:
        """Everything the board editor needs, in one request.

        The tree is static per install (152 nodes, four model geometries) and
        the selection is per character, so they are sent together rather than
        making the editor stitch three endpoints together on every character
        switch.

        `source` per character is the honest part: `"edited"` means the player
        typed it and it lives in the collection; `"captured"` means it came
        from the game via `board_state.json` and nobody has touched it. A UI
        that showed both identically would let an edit masquerade as an
        observation."""
        tree = _read_json(SKILLTREE_PATH)
        if not tree:
            self._json(
                200,
                {
                    "available": False,
                    "reason": (
                        f"{SKILLTREE_PATH.name} is not on disk (it lives under "
                        f"gitignored dumps/). Run tools/recon/proxy/skilltree.py "
                        f"against a capture to produce it."
                    ),
                },
            )
            return

        costs = self._node_costs()
        document = _read_json(COLLECTION_PATH) or {}

        # The `player` block is hand-edited, so it can be wrong on disk. Read it
        # HERE, once, and report a bad value as first-class state — the same
        # contract the missing-tree branch above uses. Letting the exception
        # escape drops the connection, which the page shows as a dead editor
        # with no reason given.
        try:
            player_level = account_bonuses_from_mapping(document).level
        except CollectionError as exc:
            self._json(
                200,
                {
                    "available": False,
                    "reason": f"{COLLECTION_PATH.name}: {exc}",
                },
            )
            return

        edited = document.get("characters") or {}
        captured = tree.get("unlocked") or {}
        placed = tree.get("placed") or {}

        # The real budget per character, not the 453 of a full board.
        #
        # `budget = cost_of(unlocked nodes) + unspent`,
        # where the unspent balance is read straight off `User/Get` and the
        # level is NOT needed — "nothing has to be typed". The total EARNED is
        # what bounds an edit, and it does not move when the player respends, so
        # it is computed from the CAPTURED node set even while editing.
        #
        # `None` for a character with no capture, because their
        # balance is genuinely unknown. Never 0: that would tell the player they
        # cannot afford anything, which is a different and false claim.
        unspent = _read_json(TREE_POINTS_PATH) or {}
        budgets: dict[str, int | None] = {}
        for chr_id, unlocked in (tree.get("unlocked") or {}).items():
            spent = sum(costs.get(node, 0) for node in (unlocked or []))
            budgets[chr_id] = spent + int(unspent.get(chr_id, 0))

        characters: dict[str, Any] = {}
        for member in tree.get("members", []):
            chr_id = member["chr"]
            if chr_id in edited:
                entry = edited[chr_id]
                characters[chr_id] = {
                    "nodes": list(entry.get("nodes") or []),
                    "slots": dict(entry.get("slots") or {}),
                    "source": "edited",
                    "budget": budgets.get(chr_id),
                }
            elif chr_id in captured:
                characters[chr_id] = {
                    "nodes": list(captured.get(chr_id) or []),
                    "slots": dict(placed.get(chr_id) or {}),
                    "source": "captured",
                    "budget": budgets.get(chr_id),
                }
            else:
                # 28 of 54 characters appear in no capture. That is a real
                # state, not an error, and it is exactly what the editor is
                # for -- so it is sent as an empty board rather than omitted.
                characters[chr_id] = {
                    "nodes": [], "slots": {}, "source": "empty",
                    "budget": budgets.get(chr_id),
                }

        # Which glyph each node draws, resolved against what is ON DISK.
        #
        # `e.icon` is the usual source, but the three per-character grouping
        # nodes (G-008/G-011/G-021) carry `e: null` and keep their icon inside
        # the `chr` block instead — so a naive read leaves them blank. The four
        # sockets genuinely have no glyph and are drawn as sockets.
        #
        # ⚠ Resolution order matters, and getting it wrong is VISIBLE. Ported
        # from `build_explorer.py`'s `glyphURI`, which established all three
        # rules, each easy to get wrong:
        #
        #  1. A "level up" node's own icon is a hollow FRAME; the granted
        #     skill's glyph goes inside it. Left bare it renders as an empty
        #     box (e.g. on R-031).
        #  2. A node that GRANTS a skill draws THAT skill's icon. For the
        #     "addition" family the game ships no tree icon at all, so
        #     `icon_sk` is not a fallback, it is the only correct answer.
        #     R-024 and R-027 are those nodes.
        #  3. The three per-character grouping nodes (G-008/G-011/G-021) carry
        #     `e: null` and keep their icon in the `chr` block.
        available = (
            {path.stem for path in ICONS_DIR.glob("*.png")}
            if ICONS_DIR.is_dir()
            else set()
        )
        missing_icons: set[str] = set()

        def _icon_of(raw: dict) -> tuple[str, str]:
            """`(icon, inner)`. `inner` is non-empty only for a frame."""
            effect = (raw.get("e") or {}) or {}
            name = effect.get("icon") or ""
            inner = effect.get("icon_sk") or ""
            if not name and not inner:
                for value in (raw.get("chr") or {}).values():
                    if isinstance(value, dict) and value.get("icon"):
                        name = value["icon"]
                        break

            if name in TREE_FRAME_ICONS and inner:
                if name in available and inner in available:
                    return name, inner
                # A frame we cannot fill is worse than the bare skill glyph.
                if inner in available:
                    return inner, ""

            for key in (name, inner):
                if key and key in available:
                    return key, ""

            # Nothing resolved. Report the name we WANTED, so the gap is
            # visible instead of a silently blank node.
            if name and name not in available:
                missing_icons.add(name)
            return "", ""

        nodes = {}
        for node_id, raw in (tree.get("nodes") or {}).items():
            icon, inner = _icon_of(raw)
            nodes[node_id] = {
                "br": raw.get("br", ""),
                "cost": costs.get(node_id, 0),
                "req_level": player_level_requirement(raw.get("req")),
                "lv": raw.get("lv", 1),
                "d": ((raw.get("e") or {}) or {}).get("d", "") or "",
                "icon": icon,
                "icon_inner": inner,
            }

        self._json(
            200,
            {
                "available": True,
                "root": ROOT_NODE,
                "total_points": TOTAL_BOARD_POINTS,
                "branches": tree.get("branches") or {},
                "nodes": nodes,
                "models": tree.get("models") or {},
                "members": tree.get("members") or [],
                "characters": characters,
                "sockets": _socket_catalogue(tree, document),
                # Through the domain, not off the raw dict: a level the solver
                # would refuse must not silently gate the editor differently
                # from the way a run would. `None` stays `None` -- board.js
                # reads that as "not told us" and locks nothing.
                "player_level": player_level,
                # Named, not swallowed: the editor says which glyph is absent
                # and what to run, rather than quietly drawing a blank node.
                "unspent_total": sum(int(v) for v in unspent.values()),
                "missing_icons": sorted(missing_icons),
                "icons_available": len(available),
            },
        )

    # -- the collection editor ---------------------------------------------
    def _refresh(self) -> None:
        """Pull the account down from the game and rebuild every derived file.

        ⚠ **This is the one route on this page that WRITES**: the page serves a
        single user's own account, so its refresh button writes where the
        refresh needs to. Everything else stays read-only, and
        `test_no_writes.py`'s structural guard still
        holds inside this file because the writing happens in
        `tools/recon/proxy/refresh_all.py`, in a SEPARATE PROCESS, under its own
        rules — not here.

        Why a subprocess and not an import: `refresh_all.py` already owns the
        data-authority contract this page must not re-implement. It hashes
        `data/owned.json` before and after and fails loudly if it moved, it may
        only APPEND to `data/cards.json`, and it is the single place those rules
        live. Importing it would put a second copy of that judgement in a web
        handler; shelling out keeps exactly one.

        ⚠ It uses `state/account_credential.json`, which IS the account — 255
        opaque bytes, full access, no second factor. A button that triggers it
        means anything that can reach this port can spend that credential. The
        server binds loopback only; that is the whole of the protection and it
        is worth knowing rather than assuming.
        """
        import subprocess  # noqa: PLC0415
        import types  # noqa: PLC0415

        script = REPO_ROOT / "tools" / "recon" / "proxy" / "refresh_all.py"
        if not script.is_file():
            self._json(500, {"error": f"missing {script}"})
            return
        # ⛔ STREAMED, not captured. `capture_output=True` would send every line
        # to a pipe and NOTHING to this server's console -- the operator presses
        # the button, watches the shell stay blank for minutes, and reasonably
        # concludes it is broken.
        #
        # So each line is echoed to the server's own stdout AS IT ARRIVES and
        # also accumulated for the HTTP reply. Both consumers want it and
        # neither is a substitute for the other: the console is where the
        # operator is looking, the reply is what the page renders.
        print(f"\n--- refresh_all.py starting ({script}) ---", flush=True)
        captured: list[str] = []
        try:
            process = subprocess.Popen(  # noqa: S603
                # `--merge-collection`: `state/collection.json` must reflect the
                # account, so the page's refresh button merges it. Without it a
                # refresh moves every derived dump forward and leaves the
                # collection behind -- the solver would go on scoring a card at
                # level 1 that the account has at 70, so it could not be
                # recommended, and nothing in the output would say so.
                #
                # The writer guesses nothing: every value is the account's own,
                # read back from `User/Get`, MERGED (not replaced), with one
                # backup to `dumps/collection_before_import.json`.
                [sys.executable, "-u", str(script), "--merge-collection"]
                # The desktop app refreshes the ACCOUNT only; everything shared comes from
                # api.holobuilder.dev (tools/app/app_settings.py).
                + (["--account-only"] if os.environ.get("HB_APP_MODE") == "1" else []),
                cwd=str(REPO_ROOT),
                # Same three encoding guards as `_run_recon_script`, and for the same
                # reason: a Windows child defaults its stdout to the console codepage
                # (cp1252 here), so a non-ASCII character in a step's output came back
                # as U+FFFD or, decoded strictly, could kill the read.
                env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            self._json(500, {"error": f"could not start refresh_all.py: {exc}"})
            return
        assert process.stdout is not None
        for line in process.stdout:
            try:
                print(f"  [refresh] {line.rstrip()}", flush=True)
            except UnicodeEncodeError:
                # This server's OWN console may be cp1252. Echoing is a convenience;
                # letting it raise would abort the request and lose the exit code.
                print(f"  [refresh] {line.rstrip().encode('ascii', 'replace').decode('ascii')}",
                      flush=True)
            captured.append(line)
        code = process.wait()
        print(f"--- refresh_all.py finished, exit {code} ---\n", flush=True)

        done = types.SimpleNamespace(
            returncode=code, stdout="".join(captured), stderr=""
        )

        # Gacha rides along with the refresh, so a refresh picks up the latest
        # pulls. The tracker's store is only fed by a sync, and Gacha/ListHistory
        # serves at most the newest 100 entries -- every refresh WITHOUT this
        # would be a chance for pulls to age out of that window unrecorded.
        # The sync stays manual in spirit: it runs only when the user presses
        # Refresh (or the Gacha tab's own Sync button); nothing schedules it.
        # Same lock as the button so two appenders can never race; if the button
        # is mid-sync the refresh reports that instead of queueing behind it.
        gacha: dict = {}
        if code == 3:   # refresh_all.EXIT_MAINTENANCE: the game refuses every call, so would this
            gacha = {"error": "the game is in maintenance; pulls not synced this refresh"}
        elif code == 4:   # refresh_all.EXIT_NO_KEYS: the API cannot be read on this PC yet
            gacha = {"error": "the game client's build constants are not on this PC; pulls not synced"}
        elif _GACHA_SYNC_LOCK.acquire(blocking=False):
            try:
                ran = self._run_recon_script(GACHA_SYNC_SCRIPT, "gacha")
            finally:
                _GACHA_SYNC_LOCK.release()
            if ran is None:
                return  # _run_recon_script already answered 500
            gcode, gout = ran
            gacha = {"returncode": gcode, "summary": _parse_sync_summary(gout)}
        else:
            gacha = {"error": "a gacha sync is already running; pulls not synced this refresh"}

        # The DIFF is the product, so stdout is returned whatever the exit code
        # -- a partial refresh that names what moved is more useful than a bare
        # failure, and the code is reported beside it rather than swallowed.
        self._json(200, {
            "returncode": done.returncode,
            "stdout": done.stdout[-20000:],
            "stderr": done.stderr[-4000:],
            "gacha": gacha,
        })

    # -- the gacha tracker ---------------------------------------------------
    def _serve_gacha(self) -> None:
        cards_doc = _read_json(CARDS_PATH) or {}
        cards_by_id = {c["id"]: c for c in (cards_doc.get("cards") or []) if isinstance(c, dict) and "id" in c}
        store = gacha_view.load_store(GACHA_STORE_DIR)
        # The account snapshot ("Refresh account") is optional: without it the view's
        # `tutorial` block is null rather than guessed.
        snapshot = _read_json(PARK_PROXY_DIR / "dumps" / "account_snapshot_main.json")
        view = gacha_view.build_view(store, cards_by_id, GACHA_ART_DIR, snapshot=snapshot)
        # art the app has not downloaded yet but the server holds counts as there
        for b in view.get("banners") or []:
            for k, e in (b.get("art_ext") or {}).items():
                if e is None and b.get("gacha_id"):
                    e = next((x for x in _GACHA_ART_EXTS if _asset_known(
                        GACHA_ART_DIR, GACHA_ART_DIR / b["gacha_id"] / f"{k}.{x}")), None)
                    b["art_ext"][k] = e
                    b.setdefault("art", {})[k] = e is not None
        view["store_dir"] = str(GACHA_STORE_DIR)
        self._json(200, view)

    def _serve_gachaart(self, rest: str) -> None:
        parts = rest.split("/")
        if len(parts) != 2 or not _GACHA_ID_RE.match(parts[0]):
            self._send(400, b"bad gacha art path", "text/plain; charset=utf-8")
            return
        # `banner`, `banner.png` and `banner.webp` are all accepted. The extension-less
        # form is what the page and the export ask for: it is the one request that works
        # whether a banner was saved as a PNG master or as a WEBP (gacha_art saves WEBP).
        kind, _, ext = parts[1].partition(".")
        if kind not in _GACHA_ART_KINDS or (ext and ext not in _GACHA_ART_EXTS):
            self._send(400, b"bad gacha art path", "text/plain; charset=utf-8")
            return
        wanted = [ext] if ext else list(_GACHA_ART_EXTS)
        path = next((p for p in (GACHA_ART_DIR / parts[0] / f"{kind}.{e}" for e in wanted)
                     if p.is_file()), GACHA_ART_DIR / parts[0] / f"{kind}.{wanted[0]}")
        if not path.is_file():
            # the desktop app has no game install to extract it from: the server's copy
            for p in (GACHA_ART_DIR / parts[0] / f"{kind}.{e}" for e in wanted):
                got = _asset(GACHA_ART_DIR, p)
                if isinstance(got, Path):
                    path = got
                    break
                if got:
                    self._send(200, got, "image/png" if got[:4] == b"\x89PNG" else "image/webp")
                    return
        if not path.is_file() or (GACHA_ART_DIR.resolve() not in path.resolve().parents
                                  and _CONTENT.store.resolve() not in path.resolve().parents):
            self._send(404, b"no gacha art", "text/plain; charset=utf-8")
            return
        # `?w=N` downscales (LANCZOS -> WEBP, cached) exactly as `/cardart/` does. The
        # masters are 2048x1024 PNGs of 2-3 MB; a 320 px gallery card that downloads the
        # master and lets the browser shrink it comes out blurred. A width
        # at or above the master's own returns the master, so the mime follows the BYTES
        # (`_scaled_art` only re-encodes when it actually resized), never the request.
        width = self._query_width()
        body = _scaled_art(path, width) if width else None
        if not body:
            body = path.read_bytes()
        if not body:
            self._send(404, b"no gacha art", "text/plain; charset=utf-8")
            return
        ctype = "image/png" if body[:8] == b"\x89PNG\r\n\x1a\n" else "image/webp"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _run_recon_script(self, script: Path, tag: str,
                          args: list[str] | None = None) -> tuple[int, str] | None:
        """Run a recon-side script as a subprocess, streaming its stdout to this
        server's console (see `_refresh` for why streamed rather than captured).

        Returns `(returncode, stdout)`, or `None` having ALREADY answered 500 when the
        script is missing or will not start -- the two failures where there is no exit
        code to report and the caller has nothing left to say.
        """
        import subprocess  # noqa: PLC0415
        if not script.is_file():
            self._json(500, {"error": f"missing {script}"})
            return None
        print(f"\n--- {script.name} starting ---", flush=True)
        captured: list[str] = []
        # PYTHONIOENCODING: the pipe is READ as utf-8 above, but a Windows child
        # defaults its stdout to the console codepage (cp1252 here) -- a `·` in the
        # child's own summary line came back as U+FFFD and, worse, a non-ASCII
        # character in the path it reports would not survive the round trip.
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        try:
            process = subprocess.Popen(  # noqa: S603
                [sys.executable, "-u", str(script), *(args or [])], cwd=str(REPO_ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                encoding="utf-8", errors="replace")
        except OSError as exc:
            self._json(500, {"error": f"could not start {script.name}: {exc}"})
            return None
        assert process.stdout is not None
        for line in process.stdout:
            try:
                print(f"  [{tag}] {line.rstrip()}", flush=True)
            except UnicodeEncodeError:
                # This server's OWN console may be cp1252. Echoing is a convenience;
                # letting it raise would abort the request and lose the exit code.
                print(f"  [{tag}] {line.rstrip().encode('ascii', 'replace').decode('ascii')}",
                      flush=True)
            captured.append(line)
        code = process.wait()
        print(f"--- {script.name} finished, exit {code} ---\n", flush=True)
        return code, "".join(captured)

    # -- event ranking cutoffs --------------------------------------------------------
    def _serve_event_cutoffs(self) -> None:
        """`GET /api/event/cutoffs?region=us[&chapter=<id>][&kind=points]` — the stored
        border series per reward tier plus a prediction of each tier's final. Pure read of
        dumps/cutoffs/ (`cutoffs_view.build_view`). 400 on a bad region, 503
        `{available:false,error}` when nothing has been synced for that region yet."""
        import time as _time  # noqa: PLC0415
        from urllib.parse import parse_qs, urlparse  # noqa: PLC0415

        import cutoffs_view  # noqa: PLC0415

        query = parse_qs(urlparse(self.path).query)
        region = ((query.get("region") or ["us"])[0] or "us").lower()
        if region not in cutoffs_view.REGIONS:
            self._json(400, {"error": f"region must be one of {', '.join(cutoffs_view.REGIONS)}"})
            return
        grades = cutoffs_view.load_grades(_read_json(_CUTOFFS_GRADE_PATH),
                                          _read_json(_CUTOFFS_GRADE_LANG_PATH))
        try:
            view = cutoffs_view.build_view(
                CUTOFFS_STORE_DIR, region, now=int(_time.time() * 1000),
                chapter_id=(query.get("chapter") or [None])[0] or None,
                kind=(query.get("kind") or [None])[0] or None, grades=grades)
        except LookupError as exc:
            self._json(503, {"available": False, "region": region, "error": str(exc)})
            return
        except (OSError, ValueError) as exc:
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(200, view)

    def _admin_proxy(self, method: str, route: str) -> None:
        """The Users page (the admin console):
        api.holobuilder.dev/auth/<route> with THIS PC's key, which the server only honours for
        an admin. Reads state/server_api.json; writes nothing here (approvals live on the VPS)."""
        import urllib.error  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415
        conf = _read_json(REPO_ROOT / "state" / "server_api.json") or {}
        if not conf.get("token"):
            self._json(200, {"admin": False, "error": "no server key on this PC"})
            return
        req = urllib.request.Request(conf["url"].rstrip("/") + "/auth/" + route, method=method,
                                     data=b"" if method == "POST" else None,
                                     headers={"Authorization": f"Bearer {conf['token']}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 -- our own host
                self._json(200, json.loads(r.read()))
        except urllib.error.HTTPError as exc:
            self._json(exc.code, {"error": (json.loads(exc.read() or b"{}") or {}).get("error", str(exc))})
        except OSError as exc:
            self._json(502, {"error": f"server unreachable: {exc}"})

    def _serve_event_top(self) -> None:
        """`GET /api/event/top?region=us[&chapter=<id>][&kind=<k>][&uid=<public id>]` -- the latest top-100
        board of a chapter with each player's last-hour movement, and with `uid` that player's
        series (`top_view.build_top` over dumps/cutoffs/<region>/<chapter>.top.jsonl, which the
        24/7 tracker writes). Pure read. 400 on a bad region, 503 `{available:false}` when the
        tracker has not stored a board for it."""
        import time as _time  # noqa: PLC0415
        from urllib.parse import parse_qs, urlparse  # noqa: PLC0415

        import cutoffs_view  # noqa: PLC0415
        import top_view  # noqa: PLC0415

        query = parse_qs(urlparse(self.path).query)
        region = ((query.get("region") or ["us"])[0] or "us").lower()
        if region not in cutoffs_view.REGIONS:
            self._json(400, {"error": f"region must be one of {', '.join(cutoffs_view.REGIONS)}"})
            return
        uid = (query.get("uid") or [None])[0] or None
        live = _tracker_configured()
        kind = (query.get("kind") or [None])[0] or None
        if kind is not None and not (kind in ("points", "total") or (
                kind.startswith("music:") and kind[6:].replace("_", "").isalnum())):
            self._json(400, {"error": "kind must be points, total or music:<id>"})
            return
        if live and not uid:
            _kick_top_watch(region, kind)
        try:
            view = top_view.build_top(
                CUTOFFS_STORE_DIR, region, now=int(_time.time() * 1000),
                chapter_id=(query.get("chapter") or [None])[0] or None, uid=uid,
                kind=kind)
        except LookupError as exc:
            self._json(503, {"available": False, "region": region, "error": str(exc),
                             "watching": live})
            return
        except (OSError, ValueError) as exc:
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        view["watching"] = live
        self._json(200, view)

    def _serve_event_history(self) -> None:
        """`GET /api/event/history` -- every marathon event on record, newest first, with the
        account's result per chapter and the final borders (`cutoffs_view.history_view` over
        `event_history.build()`, the refresh step's own build, made in memory). Pure read;
        `{events: []}` before the first build."""
        import cutoffs_view  # noqa: PLC0415

        grades = cutoffs_view.load_grades(_read_json(_CUTOFFS_GRADE_PATH),
                                          _read_json(_CUTOFFS_GRADE_LANG_PATH))
        # built per request from what is on disk (the final borders the
        # tracker pull brings in every 5 min must show without a refresh); the file the
        # refresh wrote is the fallback when the build cannot run
        doc = None
        try:
            import event_history  # noqa: PLC0415
            doc = event_history.build()[0]
        except Exception:  # noqa: BLE001 -- the stored history still answers
            doc = None
        if doc is None:
            doc = _read_json(EVENT_HISTORY_PATH) or {}
        self._json(200, {"events": cutoffs_view.history_view(doc, grades, _song_titles())})

    def _event_cutoffs_poller(self) -> None:
        """`POST /api/event/cutoffs/poller {enabled?: bool, interval_min?: int >= 1}` -- the
        page's toggle for the local guest-only poller. Answers its status; 400 on a bad
        interval. Nothing is persisted: a restarted server starts with the poller off."""
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        enabled = body.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            self._json(400, {"error": "enabled must be true or false"})
            return
        try:
            status = _CUTOFFS_POLLER.configure(enabled=enabled,
                                               interval_min=body.get("interval_min"))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, status)

    def _event_cutoffs_sync(self) -> None:
        """`POST /api/event/cutoffs/sync {region?: "all"|"us"|"as"|"jp", self?: bool}` —
        the page's button. Runs `event_cutoffs.py`, which logs in (a guest per region; the
        user's own account for its own region when `self`, default true) and APPENDS one
        read per tier to the store. One at a time: a second caller gets 409."""
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        region = str(body.get("region") or "all").lower()
        if region not in ("all", "us", "as", "jp"):
            self._json(400, {"error": "region must be all, us, as or jp"})
            return
        args = ["--region", region] + (["--self"] if body.get("self", True) else [])
        if not _CUTOFFS_SYNC_LOCK.acquire(blocking=False):
            self._json(409, {"error": "a cutoffs sync is already running"})
            return
        try:
            ran = self._run_recon_script(CUTOFFS_SYNC_SCRIPT, "cutoffs", args)
            if ran is None:
                return
            code, out = ran
            appended = None
            for line in out.splitlines():
                if line.startswith("cutoffs: "):
                    try:
                        appended = int(line.split()[1])
                    except (IndexError, ValueError):
                        appended = None
            failed = [line.split("]", 1)[0].lstrip("[") for line in out.splitlines()
                      if line.startswith("[") and "] FAILED:" in line]
            self._json(200, {"returncode": code, "appended": appended,
                             "failed_regions": failed, "stdout": out[-20000:]})
        finally:
            _CUTOFFS_SYNC_LOCK.release()

    def _gacha_sync(self) -> None:
        """Manual sync -- the page's button. Spends the account credential (a login).

        One at a time (`_GACHA_SYNC_LOCK`): this is a threading server, and a double click
        or a second tab would otherwise log in twice and run two appenders over the same
        store. The second caller is REFUSED (409) rather than queued -- gacha.js prints the
        `error` field, so the user is told why instead of watching a button hang."""
        if not _GACHA_SYNC_LOCK.acquire(blocking=False):
            self._json(409, {"error": "a gacha sync is already running"})
            return
        try:
            ran = self._run_recon_script(GACHA_SYNC_SCRIPT, "gacha")
            if ran is None:
                return
            code, out = ran
            self._json(200, {"returncode": code, "stdout": out[-20000:],
                             "summary": _parse_sync_summary(out)})
        finally:
            _GACHA_SYNC_LOCK.release()

    def _gacha_export(self) -> None:
        """One self-contained HTML file under dumps/. No login: it reads the store."""
        ran = self._run_recon_script(GACHA_EXPORT_SCRIPT, "gacha-export")
        if ran is None:
            return
        code, out = ran
        self._json(200, {"returncode": code, "stdout": out[-20000:], "path": _parse_wrote(out)})

    def _compare(self) -> None:
        """`POST /api/compare` — two hand-picked teams scored side by side under
        one set of assumptions. All logic lives in `compare_view.build_compare`;
        this only parses the JSON and maps its typed errors to status codes."""
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            body = {}
        try:
            self._json(200, compare_view.build_compare(body, art_dir=self.art_dir))
        except compare_view.CompareError as exc:
            self._json(exc.code, {"error": exc.message})
        except Exception as exc:  # noqa: BLE001 — report as JSON, never HTML 500
            import traceback  # noqa: PLC0415

            print(traceback.format_exc(), flush=True)
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _serve_collection(self) -> None:
        """The document on disk plus the bounds the editor needs to build its
        controls: each card's level cap by limit break, its max bloom, and its
        currently-resolved level/bloom.

        The bounds come from `domain/collection`, never recomputed here. A UI
        that clamps to its own idea of the cap would let the player enter a
        level the solver then silently changes -- and `resolve_card_state`
        already clamps AND reports, which is the behaviour to surface rather
        than to duplicate."""
        document = _read_json(COLLECTION_PATH) or {
            "schema_version": "collection-1",
            "card_default": "max",
            "cards": {},
        }
        curve = _read_json(CURVE_PATH)
        caps_raw = _read_json(CAPS_PATH)
        basis = stat_basis_from_mapping(curve) if curve else None
        caps = caps_by_rarity_from_mapping(caps_raw) if caps_raw else None

        cards_doc = _read_json(CARDS_PATH) or {}
        ladder_cards = (_read_json(LADDER_PATH) or {}).get("cards") or {}
        entries = document.get("cards") or {}
        default = document.get("card_default", "max")
        # Which cards the account actually has, so the editor can filter on it.
        # READ ONLY -- `data/owned.json` is authoritative and hand-editable, and
        # this module never writes it (see the module docstring). Absent file =>
        # empty set, and the filter says "0 owned" rather than pretending every
        # card is owned, which would be a claim about the account.
        owned_ids = set((_read_json(OWNED_PATH) or {}).get("owned") or [])
        # cards the player is trying as if owned: in the search pool until the next refresh
        trial_ids = set(document.get("trial") or [])

        cards: list[dict[str, Any]] = []
        for card in cards_doc.get("cards", []):
            card_id = card["id"]
            rarity = None
            if basis is not None and card_id in basis.cards:
                group = basis.cards[card_id][0] or ""
                rarity = group.partition("rarity_")[2].split("-", 1)[0] or None
            entry_caps = (caps or {}).get(rarity) if rarity else None
            state = resolve_card_state(card_id, entries.get(card_id), default, entry_caps)
            cards.append(
                {
                    "id": card_id,
                    "member": card.get("member"),
                    "rarity": card.get("rarity"),
                    # The face fields, so the editor can draw the GAME'S card
                    # through the same `cardTile` the ranking uses. A second
                    # copy of the face would let the element colours drift.
                    "type": card.get("type"),
                    "generations": sorted(card.get("generation") or []),
                    "costume_name": card.get("costume_name"),
                    "title": card.get("title"),
                    "has_art": _asset_known(Handler.art_dir, Handler.art_dir / f"{card_id}.webp"),
                    "owned": card_id in owned_ids,
                    "trial": card_id in trial_ids and card_id not in owned_ids,
                    "level": state.level,
                    "bloom": state.bloom,
                    "limit_break": state.limit_break,
                    "level_cap": state.level_cap,
                    "max_bloom": state.max_bloom,
                    "source": state.source,
                    "overstated": list(state.overstated),
                    "scorable": state.scorable,
                    # The card's skills, straight from data/cards.json (the maxed
                    # LEVEL-2 magnitudes — the same the solver reads). Read-only:
                    # the collection editor renders them so the player can SEE which
                    # skills a low bloom is scoring above their real magnitude.
                    "skills": card.get("skills") or {},
                    "costume_skill": card.get("costume_skill"),
                    "costume_text": _costume_texts().get(card_id),
                    # The catalogue's Performance / Technique / Sense, as
                    # data/cards.json carries them: the MAXED card, not this
                    # card at its level. Read-only, for the Collection page's
                    # card detail, which labels them as maxed.
                    "stats": card.get("stats"),
                    # The SAME per-level rungs `restate_card_skills` scores from, so
                    # the editor shows each skill at the level THIS bloom bought
                    # (level 1 below the step, level 2 at/above) — the game's value.
                    "skill_ladder": ladder_cards.get(card_id) or {},
                    "level_caps_by_limit_break": (
                        list(entry_caps.level_by_limit_break) if entry_caps else []
                    ),
                }
            )

        self._json(
            200,
            {
                "path": str(COLLECTION_PATH),
                "document": document,
                "cards": cards,
                "gen_colors": _gen_palette(),
                "gen_labels": _gen_labels(),
                "curve_available": basis is not None,
                "fully_scorable_bloom": FULLY_SCORABLE_BLOOM,
                "skill_bloom_step": SKILL_BLOOM_STEP,
                # The three account scalars the player sets by hand. Every bound
                # comes from `domain/collection` -- `power_up_reachable` is
                # DERIVED from the current roster and moves when a card ships,
                # so the page must never cache or restate it.
                "player": document.get("player") or {},
                "memory_max_posters": MEMORY_BONUS_MAX_POSTERS,
                "memory_permil_at_max": memory_bonus_permil(MEMORY_BONUS_MAX_POSTERS),
                "power_up_cap_permyriad": MEMBER_POWER_UP_CAP_PERMYRIAD,
                "player_level_min": PLAYER_LEVEL_MIN,
                "player_level_max": PLAYER_LEVEL_MAX,
                "board_level_gates": _board_level_gates(),
                "power_up_reachable_permyriad": max_member_power_up_permyriad(basis),
                # The roster that figure was summed over, sent BESIDE it because
                # the page has to name it in a sentence and the alternative is
                # the page inventing the number, which goes stale the moment a
                # card ships.
                "power_up_reachable_roster_size": (
                    len(basis.cards) if basis is not None else None
                ),
            },
        )

    def _serve_card_states(self) -> None:
        """`POST /api/card-states {ids, assumptions?}` -> `{states: {id: {level, bloom,
        limit_break, owned, trial}}}`: each card as a search SCORED it -- the collection
        through the same `_override_collection_levels` the search's `apply_collection`
        applies for the request's levers (`ondemand.parse_assumptions`), resolved by
        `resolve_card_state`, so with "max level, bloom 5" the result's cards show the maxed
        level and bloom rather than the account's own, and a card not owned still shows one.
        Read-only."""
        from hbuilder.cli import _override_collection_levels  # noqa: PLC0415

        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            body = {}
        ids = [str(i) for i in (body.get("ids") or []) if i][:200]
        document = _read_json(COLLECTION_PATH) or {"cards": {}}
        owned_ids = set((_read_json(OWNED_PATH) or {}).get("owned") or [])
        trial_ids = set(document.get("trial") or [])
        if isinstance(body.get("assumptions"), dict):
            import ondemand  # noqa: PLC0415
            try:
                a = ondemand.parse_assumptions(body["assumptions"])
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": f"assumptions: {exc}"})
                return
            document = _override_collection_levels(
                document, None if a.level == "account" else a.level,
                None if a.bloom == "account" else a.bloom,
                keep_higher_level=a.keep_higher, keep_higher_bloom=a.keep_higher,
                assume_limit_break=None if a.sp_training == "account" else a.sp_training)
        curve, caps_raw = _read_json(CURVE_PATH), _read_json(CAPS_PATH)
        basis = stat_basis_from_mapping(curve) if curve else None
        caps = caps_by_rarity_from_mapping(caps_raw) if caps_raw else None
        entries, default = document.get("cards") or {}, document.get("card_default", "max")
        states = {}
        for card_id in ids:
            rarity = None
            if basis is not None and card_id in basis.cards:
                rarity = (basis.cards[card_id][0] or "").partition("rarity_")[2].split("-", 1)[0] or None
            st = resolve_card_state(card_id, entries.get(card_id), default,
                                    (caps or {}).get(rarity) if rarity else None)
            states[card_id] = {"level": st.level, "bloom": st.bloom, "limit_break": st.limit_break,
                               "owned": card_id in owned_ids,
                               "trial": card_id in trial_ids and card_id not in owned_ids}
        self._json(200, {"states": states})

    def _save_collection(self) -> None:
        """Write the document to `COLLECTION_PATH` and nothing else, ever.

        Validated through `hbuilder.cli.apply_collection` BEFORE it is written:
        a document the solver cannot read is a document that must not reach
        disk, and the same function the CLI uses is the only thing that decides
        whether it can. The response carries what the solver made of it, so the
        page shows the tool's own verdict rather than its own optimism."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            document = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._json(400, {"error": f"malformed JSON: {exc}"})
            return
        if not isinstance(document, dict) or not isinstance(
            document.get("cards", {}), dict
        ):
            self._json(400, {"error": "a collection document must be an object with a 'cards' object"})
            return

        body = json.dumps(document, indent=1, sort_keys=True) + "\n"
        COLLECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
        COLLECTION_PATH.write_text(body, encoding="utf-8")

        # Read it back through the CLI's own loader and report what the SOLVER
        # made of it -- not what this handler believes it wrote.
        try:
            cards, _owned, _cfg, _prov = load_inputs(CARDS_PATH, OWNED_PATH, None)
        except _LoadFailure:
            self._json(200, {"saved": True, "path": str(COLLECTION_PATH)})
            return
        _restated, basis = apply_collection(tuple(cards), str(COLLECTION_PATH))
        self._json(
            200,
            {
                "saved": True,
                "path": str(COLLECTION_PATH),
                "cards_restated": basis.applied,
                "cards_unchanged": basis.unchanged,
                "not_fully_scorable": list(basis.not_scorable),
                "warnings": list(basis.warnings[:20]),
            },
        )

    def _query_width(self) -> int:
        """The `?w=N` width from the request URL (0 if absent/invalid), clamped to
        a sane ceiling so a crafted value cannot ask for a giant re-encode."""
        if "?" not in self.path:
            return 0
        for part in self.path.split("?", 1)[1].split("&"):
            if part.startswith("w="):
                try:
                    return max(0, min(4096, int(part[2:])))
                except ValueError:
                    return 0
        return 0

    def _serve_art(self, name: str) -> None:
        """Served from `tools/explorer/cards_explorer_art/` in place,
        never copied. Some cards may have no portrait; a missing file is a
        404 and the client falls back to
        the type-tinted gradient, which is the same `.face:not(.has)`
        placeholder `build_explorer.py` already uses."""
        if not _ART_NAME_RE.match(name):
            self._send(400, b"bad art name", "text/plain; charset=utf-8")
            return
        body = _asset_bytes(self.art_dir, self.art_dir / name)
        if body is None:
            self._send(404, b"no portrait", "text/plain; charset=utf-8")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/webp")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _serve_jacket(self, rest: str) -> None:
        """A song's jacket from dumps/song_jackets/img_music_jacket_<id>.webp,
        `?w=N` to downscale. Absent is a 404 -- the picker shows the title alone."""
        music_id = rest.split("?", 1)[0].strip("/")
        if not _MUSIC_ID_RE.match(music_id or ""):
            self._send(404, b"bad id", "text/plain; charset=utf-8")
            return
        path = JACKETS_DIR / f"img_music_jacket_{music_id}.webp"
        if not path.is_file() or JACKETS_DIR.resolve() not in path.resolve().parents:
            self._send(404, b"no jacket", "text/plain; charset=utf-8")
            return
        width = self._query_width()
        body = _scaled_art(path, width) if width else path.read_bytes()
        if not body:
            body = path.read_bytes()
        if not body:
            # A truncated master (0 bytes on disk) served 200 with an empty body reads to
            # the browser as a decode error and to the page as a broken image with no
            # cause named. Same guard as /cardart/ and /gachaart/: absent IS a 404.
            self._send(404, b"no jacket", "text/plain; charset=utf-8")
            return
        self._send(200, body, "image/webp")

    def _serve_eventart(self, rest: str) -> None:
        """An event banner / logo from dumps/event_art/<name>.webp (`event_art.py`),
        `?w=N` to downscale. Absent is a 404 -- the page keeps its placeholder."""
        name = rest.split("?", 1)[0].strip("/")
        if not _EVENT_ART_RE.match(name or ""):
            self._send(404, b"bad name", "text/plain; charset=utf-8")
            return
        path = EVENT_ART_DIR / name
        if not path.is_file() or EVENT_ART_DIR.resolve() not in path.resolve().parents:
            self._send(404, b"no event art", "text/plain; charset=utf-8")
            return
        width = self._query_width()
        body = (_scaled_art(path, width) if width else None) or path.read_bytes()
        if not body:
            self._send(404, b"no event art", "text/plain; charset=utf-8")
            return
        self._send(200, body, "image/webp")

    def _serve_cardart(self, rest: str) -> None:
        """Card art from `dumps/card_images/img_card_<family>_<key>.webp`, at NATIVE
        resolution on disk.

        `/cardart/<card-id>` is the FULL art (2048x1024 landscape); a family may be
        named first — `/cardart/vert/<id>` (portrait, the ranking face) or
        `/cardart/thumb/<id>` (square, the tree sockets). `?w=N` downscales to N px
        wide (cached) so the page fetches the size it shows, not the 2 MB master. A
        miss is a 404 and the caller falls back (name / gradient / dim socket)."""
        _FAMILIES = {"full": "img_card_full", "vert": "img_card_vert",
                     "thumb": "img_card_thumb"}
        parts = rest.split("/")
        if len(parts) == 2 and parts[0] in _FAMILIES:
            family, card_id = parts
        else:
            family, card_id = "full", parts[-1]
        if not _ART_NAME_RE.match(card_id + ".webp"):
            self._send(400, b"bad card id", "text/plain; charset=utf-8")
            return
        key = card_id[len("card-"):] if card_id.startswith("card-") else card_id
        got = _asset(CARD_IMAGES_DIR, CARD_IMAGES_DIR / f"{_FAMILIES[family]}_{key}.webp")
        if got is None:
            # `dumps/card_images/` is filled from the game's own on-disk cache and
            # can predate the newest cards; the refresh puts THEIR art in the
            # explorer art dir (`card_art_sync.py`: `<id>.webp` is the same
            # `img_card_vert`, `<id>.sq.webp` the same thumb), so look there too.
            art_dir = getattr(self, "art_dir", DEFAULT_ART_DIR)
            got = _asset(Path(art_dir), Path(art_dir) / (
                f"{card_id}.sq.webp" if family == "thumb" else f"{card_id}.webp"))
            if got is None:
                self._send(404, b"no card art", "text/plain; charset=utf-8")
                return
        # `not body` as well as `is None`: a master in the dump can be a 0-byte truncated
        # write (e.g. `img_card_thumb_00010-4-cmmn-0000-00.webp`). Served as a 200 with an
        # empty body it becomes a broken <img> the page cannot explain;
        # a 404 is the truth and the caller's named fallback is a deliberate path.
        # (bytes = the app's asset cache is off: served unscaled rather than not at all)
        body = _scaled_art(got, self._query_width()) if isinstance(got, Path) else got
        if not body:
            self._send(404, b"no card art", "text/plain; charset=utf-8")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/webp")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _serve_itemicon(self, key: str) -> None:
        """The game's item icon for `/itemicon/<item_id>` (Resources view). Tries the
        item_id filename first, then the octo asset-name form (`img_item_thumb_<id>`
        with `item-` dropped and `_`->`-`). A miss is a 404 — the view falls back to a
        coloured glyph."""
        from urllib.parse import unquote  # noqa: PLC0415

        key = unquote(key)
        # item_ids are [A-Za-z0-9_-] only; a dot or slash is a path-escape attempt.
        if not re.match(r"^[0-9A-Za-z_-]+$", key):
            self._send(400, b"bad item id", "text/plain; charset=utf-8")
            return
        candidates = [ITEM_ICONS_DIR / f"{key}.webp"]
        # The game's own item->asset map (master `Item` field 8, dumped by
        # `item_icons.py` into index.json) first: reward boxes and friends name
        # their icon there, not by the id convention below.
        mapped = _item_icon_map().get(key)
        if mapped:
            candidates.append(ITEM_ICONS_DIR / f"{mapped}.webp")
        if key.startswith("item-"):
            asset = "img_item_thumb_" + key[len("item-") :].replace("_", "-")
            candidates.append(ITEM_ICONS_DIR / f"{asset}.webp")
        for path in candidates:
            if path.is_file() and ITEM_ICONS_DIR.resolve() in path.resolve().parents:
                body = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/webp")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                self.wfile.write(body)
                return
        self._send(404, b"no item icon", "text/plain; charset=utf-8")

    def _serve_chricon(self, chr_id: str) -> None:
        """A character's round face icon (`/chricon/chr-XXXXX`). Prefers the game's
        512px `img_chr_icon_mini_<num>.webp` (once rendered into card_images), else
        the 256px `member_icons/<chr>.png`. A miss is a 404 — the caller shows the
        name alone."""
        if not re.match(r"^chr-[0-9A-Za-z_-]+$", chr_id):
            self._send(400, b"bad chr id", "text/plain; charset=utf-8")
            return
        num = chr_id.split("-", 1)[1]
        mini = _asset_bytes(CARD_IMAGES_DIR, CARD_IMAGES_DIR / f"img_chr_icon_mini_{num}.webp")
        png = MEMBER_ICONS_DIR / f"{chr_id}.png"
        if mini:
            body, ctype = mini, "image/webp"
        elif png.is_file() and MEMBER_ICONS_DIR.resolve() in png.resolve().parents:
            body, ctype = png.read_bytes(), "image/png"
        else:
            self._send(404, b"no chr icon", "text/plain; charset=utf-8")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _serve_icon(self, name: str) -> None:
        """A skill-tree node glyph, from `dumps/skill_icons/` in place.

        These are NOT under `dumps/ui_sprites/` — `skill_icons.py` pulled them
        into their own directory and the mapping from a node's `e.icon` to a
        file is the identity plus `.png`. There are 22 distinct names across
        the 152 nodes; 21 resolve, and exactly one
        (`img_skill_icon_tree_livedeckleaderactiveskilladdition`, used by R-024
        and R-027) is in the decrypted catalogue but is not extracted.

        A missing file is a 404 and the node draws without a glyph — the same
        first-class-absence contract `_serve_art` has. The alternative, a
        placeholder glyph, would put a WRONG symbol on two real nodes."""
        if not _ICON_NAME_RE.match(name):
            self._send(400, b"bad icon name", "text/plain; charset=utf-8")
            return
        path = ICONS_DIR / name
        # Resolve and re-check containment: the regex already forbids `..` and
        # separators, but the check is cheap and the directory is not ours.
        if not path.is_file() or ICONS_DIR.resolve() not in path.resolve().parents:
            self._send(404, b"no icon", "text/plain; charset=utf-8")
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _serve_chrome(self, name: str) -> None:
        """One card-chrome sprite, from `dumps/card_chrome/<kind>/<file>.png`.

        Same first-class-absence contract as `_serve_art` and `_serve_icon`: a missing
        file is a 404 and the face draws without that piece. It must never substitute
        a placeholder, because a wrong element badge or a wrong rarity band is a
        factual claim about the card.
        """
        if not _CHROME_NAME_RE.match(name):
            self._send(400, b"bad chrome name", "text/plain; charset=utf-8")
            return
        path = CHROME_DIR / name
        # Re-check containment after resolving. The regex already forbids `..` and
        # backslashes, but the directory is not ours and the check is free.
        if not path.is_file() or CHROME_DIR.resolve() not in path.resolve().parents:
            self._send(404, b"no chrome", "text/plain; charset=utf-8")
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _serve_ui_sprite(self, name: str) -> None:
        """One official UI sprite, from `dumps/ui_official/<category>/<sprite>`.

        Same first-class-absence contract as `_serve_chrome`: a missing file is a
        404 and the page draws without it. Never a placeholder: a wrong grade
        letter or a wrong skill plate is a factual claim about the game."""
        if not _UI_NAME_RE.match(name):
            self._send(400, b"bad ui sprite name", "text/plain; charset=utf-8")
            return
        path = UI_OFFICIAL_DIR / name
        if not path.is_file() or UI_OFFICIAL_DIR.resolve() not in path.resolve().parents:
            self._send(404, b"no ui sprite", "text/plain; charset=utf-8")
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/webp" if name.endswith(".webp") else "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _serve_font(self) -> None:
        """LINE Seed Sans XBd, the game's own face.

        One fixed path, no name parameter, so there is no traversal surface to
        guard. Same first-class-absence contract as the sprites: if the file was
        never fetched the route 404s and `font-display:swap` leaves the existing
        fallback stack in place. A missing font must degrade, never block paint.
        """
        if not FONT_PATH.is_file():
            self._send(404, b"no font", "text/plain; charset=utf-8")
            return
        body = FONT_PATH.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "font/woff2")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _board_plan(self) -> None:
        """On-demand board plan FOR the team the Solve won.

        Plans the EXACT team the Solve returned — leader + five members — over the
        same basis and song, via `tree_optimise --for-team`, so the plan is for the
        ranking's winner (which depends on the board toggle / basis the Solve ran
        with). Slow (a beam-search subprocess); the page shows a spinner and the
        server is threaded, so other requests are unaffected.

        The board TOGGLE is not an input here: the plan is a `--respec` bare-board
        rebuild for the given team, so it does not read the current board — the
        toggle only decided WHICH team won, which the caller already resolved."""
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            body = {}

        cards = [str(c) for c in (body.get("cards") or []) if c]
        if len(cards) != MEMBER_COUNT + 1:
            self._json(400, {"error": (
                f"need exactly {MEMBER_COUNT + 1} card ids (leader + "
                f"{MEMBER_COUNT} members); got {len(cards)}")})
            return
        basis = _parse_basis(body.get("basis"))
        scenario = _SCENARIO_BY_BASIS.get(basis, "account")
        song = str(body.get("song") or "").strip() or None
        if song is not None and not _MUSIC_ID_RE.match(song):
            self._json(400, {"error": f"bad song id {song!r}"})
            return
        # The two orthogonal resource toggles the affordable plan runs under:
        #   char_level: 'current' (real per-character level -> node points) or 'max'
        #               (every holomem at level 50 = 361 points).
        #   resources : 'inventory' (the cube/hexa wallet I have) or 'infinite'.
        char_level = str(body.get("char_level") or "current").lower()
        if char_level not in ("current", "max"):
            char_level = "current"
        resources = str(body.get("resources") or "inventory").lower()
        if resources not in ("inventory", "infinite"):
            resources = "inventory"
        # engine: 'beam' (the ~60-90s tree_optimise subprocess, default) or 'solver'
        # (the HiGHS in-process optimiser — provably optimal, sub-second).
        engine = str(body.get("engine") or "beam").lower()
        if engine not in ("beam", "solver"):
            engine = "beam"
        # max_resets: how many owned nodes the plan may take off (1,000 Hologold each,
        # read from the game binary); absent = the free rebuild. Only the exact solver honours it.
        try:
            max_resets = None if body.get("max_resets") in (None, "") else max(0, int(body["max_resets"]))
        except (TypeError, ValueError):
            self._json(400, {"error": "max_resets must be a non-negative integer"})
            return

        precompute = _load_precompute()
        if precompute is None:
            self._json(503, {"error": (
                "could not import precompute_optimisations.py (it needs the "
                "proxyenv interpreter this server runs under).")})
            return
        try:
            doc = precompute.team_board_plan(
                scenario=scenario, song=song, for_team=cards,
                char_level=char_level, resources=resources, engine=engine,
                max_resets=max_resets)
        except ModuleNotFoundError as exc:
            # The exact plan needs highspy in proxyenv (a recon-side dependency).
            # Say so, typed, instead of a 500 the page would mislabel as
            # "(~90s)"; /api/meta.solver_available lets
            # the button be disabled before the first click.
            self._json(503, {"error": f"{exc.name or 'highspy'} not installed",
                             "hint": "tools/recon/proxy/proxyenv/Scripts/python -m pip "
                                     "install highspy"})
            return
        except Exception as exc:  # noqa: BLE001 — report as JSON, never an HTML 500
            import traceback  # noqa: PLC0415

            detail = traceback.format_exc()
            print(detail, flush=True)
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(200, doc)

    def _optimize_rerank(self) -> None:
        """The bounded JOINT team+board re-rank: board-optimise the posted top-K
        teams IN-PROCESS and return them re-ranked by the board-optimised Unit
        Score — the same `board_plan.optimize_and_rerank` the CLI's
        `--optimize-board` runs, so the two never drift.

        Fast (greedy path-bundling knapsack, in-process), unlike
        `/api/boardplan`'s `tree_optimise` subprocess — so re-ranking a top-K is
        seconds, not K × ~90 s. It answers the joint-optimum question the greedy
        precompute misses: a team a board-less solve ranked second can win once
        every team's board is developed, because the board is 29–49 % of Overall
        Power and is per-team."""
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            body = {}

        try:
            doc = develop_pipeline(body, art_dir=self.art_dir)
            doc.pop("_internal", None)
        except _HttpError as exc:
            self._json(exc.code, exc.obj)
            return
        except Exception as exc:  # noqa: BLE001 — report as JSON, never HTML 500
            import traceback  # noqa: PLC0415

            print(traceback.format_exc(), flush=True)
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(200, doc)

    def _board_plan_fast(self) -> None:
        """Board plan for one team via the FAST in-process Engine A (board_plan),
        returned in the SAME board-export/v0 shape `boardtree.js` consumes as the
        slow /api/boardplan. Seconds, not ~90s.

        Engine A optimises each character's tree from BARE within that character's
        REAL earned skill-tree points (board_export's `points.granted` — the
        account's actual budget, typically 12-97, far below the 453 full tree) and
        re-scores the assembled board on the exact deck-screen Unit Score. A flat
        `budget` in the body overrides that with a per-character cap (0-453), the
        "if I fully levelled" question. It ALSO runs the team-aware socket optimiser
        against the developed board (socket_optimise + _annotate_sockets), so the
        fast plan shows the recommended Connect cards too. The slow /api/boardplan
        stays the option for respec/keep/max budget presets, cube/hexa affordability
        and per-song tuning."""
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            body = {}
        ids = [str(c) for c in (body.get("cards") or []) if c]
        if len(ids) != MEMBER_COUNT + 1:
            self._json(400, {"error": (
                f"need exactly {MEMBER_COUNT + 1} card ids (leader + "
                f"{MEMBER_COUNT}); got {len(ids)}")})
            return
        basis_name = _parse_basis(body.get("basis"))
        # SINGER CONTEXT: an optional `song` makes the develop plan
        # song-aware — the leader's conditional reds and solo/group yellows are
        # only worth points on a song she is credited on
        # (`board_plan.power_node_values(singer_weight=...)`). Absent (the
        # default) the plan stays song-agnostic.
        # Unknown song id is not fatal here: the fast plan is a Unit-Score
        # answer, so it degrades to the song-agnostic plan rather than 400.
        _song = str(body.get("song") or "").strip()
        _cast: frozenset[str] = frozenset()
        if _song:
            _library = load_charts(str(CHARTS_PATH))
            _chart = _library.get(_song) if _library is not None else None
            if _chart is not None:
                _cast = song_cast(_chart)

        # the cast is passed whole (`sings=_cast`): every singing board is valued as a
        # singer, a member's yellows and yellow socket included -- not only the leader's

        # Default: each character's REAL earned points. A flat `budget` overrides.
        flat_budget = None
        if body.get("budget") is not None:
            try:
                flat_budget = max(0, min(453, int(body.get("budget"))))
            except (TypeError, ValueError):
                flat_budget = None
        if flat_budget is None and basis_name == MAXED_ACCOUNT_BASIS:
            flat_budget = MAXED_ACCOUNT_BUDGET

        skilltree = _read_json(SKILLTREE_PATH)
        node_master = _read_json(NODE_MASTER_PATH)
        if not skilltree or not node_master:
            self._json(503, {"error": (
                "board optimisation needs skilltree.json + "
                "master_SkillTreeNode.json under the gitignored dumps/.")})
            return
        precompute = _load_precompute()
        if precompute is None or not hasattr(precompute, "board_export"):
            self._json(503, {"error": (
                "could not import board_export via precompute_optimisations "
                "(needs the proxyenv interpreter this server runs under).")})
            return
        effects = node_effect_ids_from_master(node_master)

        try:
            cards, owned, cfg, _prov = load_inputs(CARDS_PATH, OWNED_PATH, False)
            try:
                cards, collection_basis, cfg, ma_scenario = _apply_basis(
                    cards, owned, cfg, basis_name, precompute=precompute)
            except MaxedAccountUnavailable as exc:
                self._json(503, {"error": str(exc)})
                return
            by_id = {c.id: c for c in cards}
            missing = [c for c in ids if c not in by_id]
            if missing:
                self._json(400, {"error": f"unknown card id(s): {missing}"})
                return
            try:
                team = Team(leader=by_id[ids[0]],
                            members=tuple(by_id[c] for c in ids[1:]))
            except TeamError as exc:
                self._json(400, {"error": f"invalid team: {exc}"})
                return

            tree_cache: dict[str, object] = {}

            def tree_for(card):
                model = model_for_character(skilltree, "chr-" + card.id.split("-")[1])
                if model is None:
                    return None
                if model not in tree_cache:
                    tree_cache[model] = skill_tree_from_dumps(
                        skilltree, node_master, model)
                return tree_cache[model]

            # Build the geometry/state doc FIRST: it carries each character's REAL
            # board budget (`points.granted`), which the plan must respect — the
            # node costs are meaningless if the budget is a flat 453 nobody has.
            scenario = _SCENARIO_BY_BASIS.get(basis_name, "account")
            doc = precompute.board_export.build(gain=False, pool=1, scenario=scenario)
            granted = {
                c["chr"]: (c.get("points") or {}).get("granted")
                for c in doc.get("characters", [])
            }

            def budget_for(card):
                if flat_budget is not None:
                    return flat_budget
                g = granted.get("chr-" + card.id.split("-")[1])
                return int(g) if g else 0

            # Same REAL Connect basis as the develop ranking, so this doc's
            # board_off/optimised_us cannot drift from the numbers the ranking
            # showed for the same team. Non-fatal -> idealised fallback.
            try:
                connect_cards = precompute.connect_cards_for(scenario)
            except Exception:  # noqa: BLE001 — sockets stay idealised
                connect_cards = None
            bench_characters = _bench_characters()
            _bench_socks = None if ma_scenario is not None else _bench_sockets()
            if ma_scenario is not None:
                connect_cards = ma_scenario.connect_cards
                bench_characters = ma_scenario.bench
            optimised = optimize_and_rerank(
                [team], cfg, tree_for=tree_for, budget_for=budget_for,
                node_effect_ids=effects, skilltree=skilltree,
                connect_cards=connect_cards,
                bench_characters=bench_characters,
                name_by_chr={"chr-" + c.id.split("-")[1]: c.member for c in cards},
                sings=_cast, bench_sockets=_bench_socks,
            )
            bt = optimised[0]

            # Engine A's chosen node set per character, keyed by chr id the way
            # board_export / _apply_plan key characters.
            team_nodes: dict[str, list[str]] = {}
            for card in [team.leader, *team.members]:
                chr_id = "chr-" + card.id.split("-")[1]
                cplan = bt.plan.per_character.get(card.id)
                if cplan is not None:
                    team_nodes[chr_id] = sorted(cplan.selected)
            overlay = {
                chr_id: {"nodes": nodes, "slots": {}}
                for chr_id, nodes in team_nodes.items()
            }

            # Keep only the team's six characters and overlay Engine A's plan as
            # to_buy/to_sell/optimised_nodes (respec diff vs the current board) —
            # the same _apply_plan the slow path uses, the shape boardtree.js paints.
            doc["characters"] = [
                c for c in doc.get("characters", []) if c["chr"] in team_nodes
            ]
            precompute._apply_plan(doc, overlay)
            # The fast Engine-A plan is always costed on REAL points (current level),
            # so the header shows the board's cost against each character's real budget.
            precompute._annotate_plan_budget(doc, char_level="current")

            # Sockets: the team-aware socket optimiser ON the developed board, for
            # THIS team — the same socket picks the full plan makes, so the fast plan
            # is complete (nodes AND Connect cards), not crippled. Via precompute,
            # which owns the temp-file write (the web layer's no-writes allowlist
            # forbids writing here). Non-fatal.
            #
            # `include_current_benches`: the fast plan only develops the six team
            # boards, so — unlike the full plan — its socket search would miss the
            # ~50 bench holomems whose GREEN nodes are account-global. Widen it to the
            # current boards so a Connect card on a bench's free S-001 can socket the
            # green you already own (+8.9k isolated on a real account, ~5s). Those
            # bench boards are NOT rendered (the doc stays the six team boards); the
            # picks appear in the socket LIST, named by `host_name`.
            precompute.annotate_sockets_for(
                doc, overlay, scenario=scenario, song=None, for_team=ids,
                include_current_benches=True)

            doc["for_team"] = ids
            doc["plan_included"] = True
            doc["engine"] = "fast"
            doc["budget"] = "real" if flat_budget is None else flat_budget
            doc["board_off_us"] = round(bt.board_off_us)
            doc["optimised_us"] = round(bt.optimised_us)
        except Exception as exc:  # noqa: BLE001 — report as JSON, never HTML 500
            import traceback  # noqa: PLC0415

            print(traceback.format_exc(), flush=True)
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(200, doc)

    # -- Event Points mode --------------------------------------------------
    def _serve_event(self) -> None:
        """`GET /api/event[?event=<key>]` — the newest open event's bonus rows and
        the account's bonus cards (`event_mode.event_payload`). Pure read; 503
        when the refresh has not written marathon_bonus.json or nothing is open."""
        import event_mode  # noqa: PLC0415
        from urllib.parse import parse_qs, urlparse  # noqa: PLC0415

        query = parse_qs(urlparse(self.path).query)
        key = (query.get("event") or [None])[0] or None
        try:
            raw, event, keys = event_mode.load_event_bundle(EVENT_BONUS_PATH, key)
        except (OSError, ValueError, KeyError, event_mode.mar.SchemaError) as exc:
            # FileNotFoundError, a JSON error, SchemaError (a ValueError) — all 503
            self._json(503, _event_unavailable(exc))
            return
        try:
            cards, owned, _cfg, _prov = load_inputs(CARDS_PATH, OWNED_PATH, False)
        except _LoadFailure as exc:
            self._json(500, {"error": f"could not load the catalogue: {exc}"})
            return
        collection = ((_read_json(COLLECTION_PATH) or {}).get("cards") or {})
        titles, singers = _song_labels()
        try:
            meta = _read_json(EVENT_META_PATH)
        except (OSError, ValueError):  # a torn event_meta.json costs the title, not the page
            meta = None
        self._json(200, event_mode.event_payload(
            raw, event, open_keys=keys, cards_by_id={c.id: c for c in cards},
            owned_ids=[c.id for c in owned], collection=collection, titles=titles,
            chr_names=_chr_names(cards), meta=meta,
            art_dir=EVENT_ART_DIR, singers=singers,
            music_meta={str(d.get("id")): d for d in _music_rows() if d.get("id")}))

    def _serve_event_songs(self) -> None:
        """`GET /api/event/songs[?boosts=3&difficulty=EXPERT]` — every song ranked
        by the song-length multiplier L (an ESTIMATE, team-independent)."""
        import event_mode  # noqa: PLC0415
        from urllib.parse import parse_qs, urlparse  # noqa: PLC0415

        query = parse_qs(urlparse(self.path).query)
        try:
            boosts = int((query.get("boosts") or [event_mode.DEFAULT_BOOSTS])[0])
        except ValueError:
            boosts = -1
        if not 1 <= boosts <= 10:
            self._json(400, {"error": "'boosts' must be an integer 1..10"})
            return
        try:
            difficulty = _parse_difficulty({"difficulty": (query.get("difficulty") or [None])[0]})
        except _HttpError as exc:
            self._json(exc.code, exc.obj)
            return
        try:
            # `event`: one chapter (a Spotlight event's chapters each have their own bonuses)
            _raw, event, _keys = event_mode.load_event_bundle(
                EVENT_BONUS_PATH, (query.get("event") or [None])[0] or None)
        except (OSError, ValueError, KeyError, event_mode.mar.SchemaError):  # the ranking stands without an event
            event = None
        titles, singers = _song_labels()
        self._json(200, event_mode.songs_payload(
            _music_rows(), titles=titles, singers=singers, boosts=boosts, event=event,
            charted=_chart_note_counts(difficulty), difficulty=difficulty))

    def _start_search(self) -> None:
        """`POST /api/search {song (empty = in general), basis?: one of
        two_stage_build.BASES,
        difficulty?, leader_member?, leader_card?, no_leader_overlap?, require?,
        exclude?, top?}` -- the optimiser ON DEMAND (`ondemand.py`): 202
        `{job_id}`; events on `/events/<id>`; cancel with `POST /cancel/<id>`.
        The result payload has the shape of `GET /api/two-stage`. Read-only."""
        import ondemand  # noqa: PLC0415
        import two_stage_build as tsb  # noqa: PLC0415

        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            self._json(400, {"error": "the body is not valid JSON"})
            return
        song = str(body.get("song") or "").strip()
        # no song = in general: the mean over every chart (`ondemand.Engine.search`)
        if song and not all(ch.isalnum() or ch in "_-" for ch in song):
            self._json(400, {"error": "song must be a music id (or empty for in general)"})
            return
        basis = str(body.get("basis") or "account")
        if basis not in tsb.BASES:
            self._json(400, {"error": f"basis must be one of {sorted(tsb.BASES)}"})
            return
        try:
            difficulty = _parse_difficulty(body, song)
        except _HttpError as exc:
            self._json(exc.code, exc.obj)
            return
        try:
            top = max(1, min(200, int(body.get("top") or ondemand.RANKING)))
        except (TypeError, ValueError):
            top = ondemand.RANKING
        # `assumptions` (the account levers, `ondemand.Assumptions`): the
        # account under them; absent, the fixed `basis`
        assumptions = None
        if isinstance(body.get("assumptions"), dict):
            try:
                assumptions = ondemand.parse_assumptions(body["assumptions"])
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": f"assumptions: {exc}"})
                return
            basis = "account"
        # holodori.best-style "any song" level range: In general over every chart of any
        # difficulty whose level is in [lo, hi]
        levels = None
        if body.get("levels") not in (None, "", []):
            try:
                lo, hi = (int(v) for v in body["levels"])
            except (TypeError, ValueError):
                self._json(400, {"error": "levels must be [lowest, highest]"})
                return
            if song or not 1 <= lo <= hi <= 40:
                self._json(400, {"error": "levels need no song (In general) and 1 <= lowest <= highest <= 40"})
                return
            levels = (lo, hi)
        opts = {"song": song, "basis": basis, "difficulty": difficulty, "top": top,
                "filters": ondemand.parse_filters(body), "assumptions": assumptions,
                "levels": levels, "play": ondemand.parse_play(body)}
        job = Job(job_id=uuid.uuid4().hex)
        with _JOBS_LOCK:
            _JOBS[job.job_id] = job
        threading.Thread(target=run_search_job, args=(job, opts),
                         name=f"search-{job.job_id[:8]}", daemon=True).start()
        self._json(202, {"job_id": job.job_id, "opts": {k: v for k, v in opts.items()
                                                          if k not in ("filters", "assumptions")}})

    def _event_points(self) -> None:
        """`POST /api/event/points` -- one team's Event Pt per Live, solo or with the room's
        MULTI SCORE typed in (`event_mode.event_points_for`). Instant: no search."""
        import event_mode  # noqa: PLC0415

        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            self._json(200, event_mode.event_points_for(body))
        except json.JSONDecodeError:
            self._json(400, {"error": "the body is not valid JSON"})
        except event_mode.EventRequestError as exc:
            self._json(400, {"error": str(exc)})

    def _start_event_run(self) -> None:
        """`POST /api/event/run {objective, song, difficulty?, boosts?, holopass?,
        workers?, top?, event?}` — 202 `{job_id, plan, opts}`; events on
        `/events/<id>`; cancel with `POST /cancel/<id>`."""
        import event_mode  # noqa: PLC0415

        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            self._json(400, {"error": "the body is not valid JSON"})
            return
        try:
            opts = event_mode.parse_event_body(body)
            # Highlight is a pool, not one song: the difficulty alone is checked here
            opts["difficulty"] = _parse_difficulty(
                body, "" if event_mode.is_highlight(opts["song"]) else opts["song"])
        except event_mode.EventRequestError as exc:
            self._json(400, {"error": str(exc)})
            return
        except _HttpError as exc:
            self._json(exc.code, exc.obj)
            return
        if not EVENT_BONUS_PATH.is_file():
            self._json(503, _event_unavailable(FileNotFoundError(str(EVENT_BONUS_PATH))))
            return
        job = Job(job_id=uuid.uuid4().hex)
        with _JOBS_LOCK:
            _JOBS[job.job_id] = job
        thread = threading.Thread(target=run_event_job, args=(job, opts),
                                  name=f"event-{job.job_id[:8]}", daemon=True)
        thread.start()
        self._json(202, {"job_id": job.job_id, "plan": [dict(s) for s in EVENT_STAGES],
                         "opts": opts})

    def _serve_meta(self) -> None:
        """The one-line facts the page shows before any run: how many cards the
        account owns of the catalogue, how many boards the capture carries, and
        the two account-level bonuses every score silently includes (memory /
        member power-up) — plus the exhaustive size of the owned pool, so the
        search-space line is the server's own number, not client arithmetic."""
        try:
            cards, owned, _cfg, _prov = load_inputs(CARDS_PATH, OWNED_PATH, False)
            _cards2, basis = apply_collection(
                cards, str(COLLECTION_PATH), superseded_by_real_stats=False,
                owned_ids=[c.id for c in owned], maxed=False,
            )
            bonuses = basis.bonuses
            bs = _read_json(
                REPO_ROOT / "tools" / "recon" / "proxy" / "dumps"
                / "board_state.json") or {}
            captured = sum(1 for v in bs.values() if (v or {}).get("nodes"))
            _sets, arrangements = project_arrangements(len(owned), 0, False)
            snap_path = (REPO_ROOT / "tools" / "recon" / "proxy" / "dumps"
                         / "account_snapshot_main.json")
            try:
                snapshot_mtime = _dt.datetime.fromtimestamp(
                    snap_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            except OSError:
                snapshot_mtime = None
            charts_raw = _read_json(CHARTS_PATH) or {}
            chart_count = len((charts_raw.get("charts") or {}))
            difficulties = _difficulties_listing()
            try:
                import importlib.util  # noqa: PLC0415
                solver_available = importlib.util.find_spec("highspy") is not None
            except Exception:  # noqa: BLE001
                solver_available = False
            self._json(200, {
                "catalogue": len(cards),
                "owned": len(owned),
                # provenance the header strip states in one line
                "snapshot_mtime": snapshot_mtime,
                # a page reloaded mid-refresh shows it running instead of offering a second
                "refresh_running": _REFRESH_LOCK.locked(),
                "chart_count": chart_count,
                # the chart difficulties a run may ask for (`difficulty` on
                # /solve, /acquire, /api/run, /api/optimize-rerank,
                # /api/compare, /api/timeline, ?difficulty= on /api/songs and
                # /api/two-stage): the default, what is on disk, how many
                # charts each holds, and — only when they vary — the EXPERT
                # songs with no chart at a difficulty
                "difficulties": difficulties,
                # whether the exact HiGHS plan can run at all (typed 503 otherwise)
                "solver_available": solver_available,
                "boards_with_nodes": captured,
                "boards_total": len(bs),
                "memory_posters": getattr(bonuses, "posters", None) if bonuses else None,
                "memory_bonus_permil": (bonuses.memory_permil
                                        if bonuses is not None else None),
                "power_up_permyriad": (
                    bonuses.power_up_permyriad
                    if bonuses is not None and bonuses.power_up_permyriad is not None
                    # `player.power_up_permyriad: "auto"` — the derived value the
                    # scoring itself uses, never a hand-typed stale one.
                    else getattr(basis.derived_power_up, "permyriad", None)),
                "owned_arrangements": arrangements,
            })
        except Exception as exc:  # noqa: BLE001 — an info strip must never 500 the page
            # `load_inputs` raises `_LoadFailure(exit code)` from the real error: say that one
            # (otherwise the page shows "_LoadFailure: 3" for "owned: id '...' is not present
            # in the loaded cards")
            if isinstance(exc, _LoadFailure) and exc.__cause__ is not None:
                exc = exc.__cause__
            self._json(200, {"error": f"{type(exc).__name__}: {exc}"})

    def _start_acquire(self) -> None:
        """Start a next-best-card job (`run_acquire_job`) and return its job id;
        progress and the result flow through the same `/events/<id>` channel the
        solve uses — one job plumbing, not two."""
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            body = {}
        use_board = body.get("board", True) is not False
        song = str(body.get("song") or "").strip()
        pool_filter = str(body.get("pool") or "all").lower()
        if pool_filter not in ("all", "5star"):
            pool_filter = "all"
        uptime_model = str(body.get("uptime_model") or "").lower() or None
        if uptime_model not in ACTIVE_UPTIME_MODELS:
            uptime_model = None
        assume_level = _parse_assume(body, "assume_level", 1, 80)
        assume_bloom = _parse_assume(body, "assume_bloom", 0, 6)
        platform = _parse_platform(body)
        try:
            difficulty = _parse_difficulty(body, song)
        except _HttpError as exc:
            self._json(exc.code, exc.obj)
            return

        def _ids(key: str) -> tuple[str, ...]:
            raw = body.get(key)
            if not isinstance(raw, list):
                return ()
            out: list[str] = []
            for item in raw:
                cid = str(item).strip()
                if cid and cid not in out:
                    out.append(cid)
            return tuple(out)

        job = Job(job_id=uuid.uuid4().hex)
        with _JOBS_LOCK:
            _JOBS[job.job_id] = job
        thread = threading.Thread(
            target=run_acquire_job,
            args=(job, self.art_dir, use_board, song, pool_filter,
                  uptime_model, assume_level, assume_bloom, 15,
                  _ids("require"), _ids("leader")),
            kwargs={"platform": platform, "difficulty": difficulty},
            name=f"acquire-{job.job_id[:8]}",
            daemon=True,
        )
        thread.start()
        self._json(200, {"job_id": job.job_id, "mode": "acquire"})

    def _start_solve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}
        top_n = int(body.get("top", 1) or 1)
        top_n = max(1, min(top_n, 50))
        # The account basis. Anything that is not exactly "maxed" is the real
        # account -- an unrecognised value must not silently become the
        # hypothetical one, since the two answer different questions.
        basis = _parse_basis(body.get("basis"))
        # Absent means TRUE: the board has always been scored, and a missing
        # field must not silently change an answer by 78,000 points.
        use_board = body.get("board", True) is not False
        # `""` and absent both mean song-agnostic, exactly as today. Anything
        # else is a music id and the run re-ranks its top K on that chart.
        song = str(body.get("song") or "").strip()
        # Optional candidate-pool filter: "all" (default) or "5star" (consider
        # only 5* cards). A restriction on WHICH cards enter the search, the same
        # lever as the selectors — not a scoring change. Unknown -> "all".
        pool_filter = str(body.get("pool") or "all").lower()
        if pool_filter not in ("all", "5star"):
            pool_filter = "all"

        # Search constraints (--require/--leader/--exclude/--include-unowned):
        # lists of card ids, de-duped and stringified; a bad id or a conflict is
        # caught by resolve_selectors inside the job and reported as an error
        # event, so nothing here validates beyond shape.
        def _id_list(key: str) -> tuple[str, ...]:
            raw = body.get(key)
            if not isinstance(raw, list):
                return ()
            seen: list[str] = []
            for item in raw:
                cid = str(item).strip()
                if cid and cid not in seen:
                    seen.append(cid)
            return tuple(seen)

        require = _id_list("require")
        leader = _id_list("leader")
        exclude = _id_list("exclude")
        include_unowned = body.get("include_unowned", False) is True
        # --exact forces the exhaustive search over the auto-chosen beam; --estimate
        # reports the projected cost and stops without enumerating.
        force_exact = body.get("exact", False) is True
        estimate = body.get("estimate", False) is True
        # --across-songs: rank by the MEAN expected score over every chart (+ wins).
        across_songs = body.get("across_songs", False) is True
        # ACTIVE-uptime model: "steady" (deck-panel, config default) | "finite"
        # (in-run window). Absent/empty/unknown -> None = inherit config.json.
        uptime_model = str(body.get("uptime_model") or "").lower() or None
        if uptime_model not in ACTIVE_UPTIME_MODELS:
            uptime_model = None
        # Level/bloom "what-if" filters: None/"account"/"" = use the account's own
        # values (no override); "max" = the card's cap; an int = that exact value,
        # clamped to a sane range. Only meaningful on the account basis. Shared with
        # /api/optimize-rerank via `_parse_assume` so the two cannot drift.
        assume_level = _parse_assume(body, "assume_level", 1, 80)
        assume_bloom = _parse_assume(body, "assume_bloom", 0, 6)
        # Platform (mobile | pc): the LiveNote PERFECT column — FLICK 1050 vs 1000.
        # Absent -> mobile.
        platform = _parse_platform(body)
        # Chart difficulty: absent -> EXPERT; unknown -> 400.
        try:
            difficulty = _parse_difficulty(body, song)
        except _HttpError as exc:
            self._json(exc.code, exc.obj)
            return

        job = Job(job_id=uuid.uuid4().hex)
        with _JOBS_LOCK:
            _JOBS[job.job_id] = job
        thread = threading.Thread(
            target=run_solve_job,
            args=(job, top_n, self.art_dir, basis, use_board, song, pool_filter,
                  require, leader, exclude, include_unowned, force_exact, estimate,
                  across_songs, uptime_model, assume_level, assume_bloom),
            kwargs={"platform": platform, "difficulty": difficulty},
            name=f"solve-{job.job_id[:8]}",
            daemon=True,
        )
        thread.start()
        self._json(202, {"job_id": job.job_id, "top": top_n, "basis": basis,
                         "board": use_board, "song": song, "pool": pool_filter,
                         "require": list(require), "leader": list(leader),
                         "exclude": list(exclude), "include_unowned": include_unowned,
                         "exact": force_exact, "estimate": estimate,
                         "across_songs": across_songs, "uptime_model": uptime_model,
                         "assume_level": assume_level, "assume_bloom": assume_bloom,
                         "platform": platform, "difficulty": difficulty})

    def _start_run(self) -> None:
        """`POST /api/run` — the ONE job: 202 with
        the job id and the stage plan; events on `/events/<id>`; cancel via
        `POST /cancel/<id>` (checked between stages and between teams)."""
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            body = {}
        try:
            opts = _parse_run_body(body)
        except _HttpError as exc:
            self._json(exc.code, exc.obj)
            return
        job = Job(job_id=uuid.uuid4().hex)
        with _JOBS_LOCK:
            _JOBS[job.job_id] = job
        thread = threading.Thread(target=run_job, args=(job, opts, self.art_dir),
                                  name=f"run-{job.job_id[:8]}", daemon=True)
        thread.start()
        self._json(202, {"job_id": job.job_id, "plan": run_plan(opts),
                         "opts": {k: (list(v) if isinstance(v, tuple) else v)
                                  for k, v in opts.items()}})

    def _serve_board_doc(self) -> None:
        """`POST /api/board-doc {board_state, cards, basis, budget}` — the
        board-export/v0 doc for the six characters of a team with a GIVEN
        developed board overlaid (`_apply_plan`: to_buy / to_sell /
        optimised_nodes / slots) — build + overlay + sockets from the given
        slots, NO optimisation call. Fetched lazily when a row's board section
        is opened. `budget`: "real" (points.granted) | int flat cap —
        `point_budget` is labelled by that kind, never "453 / 97"."""
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            body = {}
        board_state = body.get("board_state")
        if not isinstance(board_state, dict) or not board_state:
            self._json(400, {"error": "need a non-empty 'board_state' {chr: {nodes, slots}}"})
            return
        ids = [str(c) for c in (body.get("cards") or []) if c]
        basis_name = _parse_basis(body.get("basis"))
        flat_budget = None
        if body.get("budget") not in (None, "", "real"):
            try:
                flat_budget = max(0, min(453, int(body.get("budget"))))
            except (TypeError, ValueError):
                flat_budget = None
        if flat_budget is None and basis_name == MAXED_ACCOUNT_BASIS:
            flat_budget = MAXED_ACCOUNT_BUDGET
        precompute = _load_precompute()
        if precompute is None or not hasattr(precompute, "board_export"):
            self._json(503, {"error": (
                "could not import board_export via precompute_optimisations "
                "(needs the proxyenv interpreter this server runs under).")})
            return
        try:
            # the team's sockets are the plan's; one it left empty keeps the account's card
            # when the plan uses that card nowhere (`_settle_slots`)
            state = {c: {"nodes": list((st or {}).get("nodes") or []), "slots": dict((st or {}).get("slots") or {})}
                     for c, st in board_state.items() if isinstance(st, dict)}
            if body.get("complete") and basis_name == "maxed":
                # a maxed account: EVERY holomem's target, shown against the
                # account's real boards so the player sees what to unlock and what it costs.
                # Only what scores for THIS team ("remove the useless nodes"): per holomem the
                # nodes `tsb.maxed_bench_targets(team)` names (greens that reach a team card,
                # per-song yellows, a team singer's conditional reds, socketed Connect cards);
                # a team holomem also its row's planned board -- the row's plan is ONE song's,
                # the union covers the songs she sings (solo AND group yellows); each plus the
                # cheapest path from the account's boards and what the account already holds
                # (a maxed account resets nothing)
                import two_stage_build as tsb  # noqa: PLC0415

                targets = tsb.maxed_bench_targets(ids)
                team = state
                now = precompute.board_export.build(gain=False, pool=1,
                                                    scenario=_SCENARIO_BY_BASIS["maxed"])
                state = {}
                for ch in now.get("characters", []):
                    c = ch["chr"]
                    nodes = {n["id"]: n for n in ch.get("nodes", [])}
                    extra = set((targets.get(c) or {}).get("nodes") or [])
                    if c in team:                       # its sockets are its row's
                        extra = {i for i in extra if not i.startswith("S-")}
                    want = set((team.get(c) or {}).get("nodes") or []) | extra
                    owned = {i for i, n in nodes.items() if n.get("state") == "owned"}
                    reach = set()
                    for i in want:
                        reach |= set((nodes.get(i) or {}).get("unlock_path") or []) | {i}
                    slots = (team.get(c) or targets.get(c) or {}).get("slots") or {}
                    state[c] = {"nodes": sorted(owned | reach), "slots": dict(slots)}
                flat_budget = MAXED_ACCOUNT_BUDGET
            else:
                _settle_slots(state, _read_json(PROXY_DIR / "dumps" / "board_state.json") or {}, set(state),
                              _connect_groups(precompute))
            doc = _board_doc_for(precompute, state, ids, basis_name, flat_budget)
        except Exception as exc:  # noqa: BLE001 — report as JSON, never HTML 500
            import traceback  # noqa: PLC0415

            print(traceback.format_exc(), flush=True)
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(200, doc)

    def _serve_board_plan_account(self) -> None:
        """`POST /api/board-plan-account {cards: [leader, m1..m5], song?, song_pool?, assumptions,
        board_state?}` — the WHOLE account's planned Holomem Boards for a team, as
        holodori.best shows them: every holomem's target board, not only the six.

        The plan is the exact one (`materials_plan.plan_for` -> `board_plan_solver.
        solve_team_board(develop_bench=True)`) under the request's board levers
        (`ondemand.Assumptions.plan_opts`); the team's six boards are the row's own
        (`board_state`, the boards its score was computed on) when given. Answers the
        board-export doc of every character with the plan overlaid (to_buy / to_sell /
        optimised_nodes), `changed` = the holomems whose board moves. Read-only."""
        import materials_plan  # noqa: PLC0415
        import ondemand  # noqa: PLC0415

        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            body = {}
        ids = [str(c) for c in (body.get("cards") or []) if c]
        if len(ids) != 6:
            self._json(400, {"error": "need 'cards': [leader, five members]"})
            return
        try:
            a = ondemand.parse_assumptions(body.get("assumptions"))
        except (TypeError, ValueError) as exc:
            self._json(400, {"error": f"bad assumptions: {exc}"})
            return
        if a.boards != "plan":
            self._json(400, {"error": "boards as they are: there is no plan to show"})
            return
        precompute = _load_precompute()
        if precompute is None or not hasattr(precompute, "board_export"):
            self._json(503, {"error": "could not import board_export via precompute_optimisations"})
            return
        try:
            opts = a.plan_opts()
            if isinstance(body.get("song_pool"), list) and body["song_pool"]:
                # a Highlight row on the whole pool: its plan values the yellows on every
                # pool song (`row_detail.pool_detail`'s `plan_pool`, the same plan)
                opts["song_pool"] = body["song_pool"]
            planned = materials_plan.plan_for(ids[0], ids[1:], body.get("song") or None, opts)
            if planned is None:
                self._json(422, {"error": "the wallet cannot fund a plan for this team"})
                return
            whole = planned[2]
            captured = _read_json(PROXY_DIR / "dumps" / "board_state.json") or {}
            state = {chr_id: {"nodes": list((plan or {}).get("nodes") or []),
                              "slots": dict((captured.get(chr_id) or {}).get("slots") or {})}
                     for chr_id, plan in whole.items()}
            for chr_id, st in (body.get("board_state") or {}).items():
                if isinstance(st, dict):
                    state[chr_id] = {"nodes": list(st.get("nodes") or []), "slots": dict(st.get("slots") or {})}
            # the sockets the plan re-chose: the team's (the row's own `board_state`) and,
            # when the detail re-chose the bench's too, `bench_slots` (`row_detail._bench_slots`)
            chosen = {c for c, st in (body.get("board_state") or {}).items() if isinstance(st, dict)}
            for chr_id, slots in (body.get("bench_slots") or {}).items():
                if isinstance(slots, dict) and chr_id in state:
                    state[chr_id]["slots"] = {str(k): str(v) for k, v in slots.items() if v}
                    chosen.add(chr_id)
            _settle_slots(state, captured, chosen, _connect_groups(precompute))
            flat = 361 if a.max_points else None
            doc = _board_doc_for(precompute, state, ids, "account", flat)
            team = {"chr-" + c.split("-")[1] for c in ids}
            doc["team_chrs"] = sorted(team)
            doc["changed"] = sorted(c["chr"] for c in doc.get("characters", [])
                                    if (c.get("plan") or {}).get("to_buy") or (c.get("plan") or {}).get("to_sell")
                                    or (c.get("plan") or {}).get("socket_changes"))
            doc["assumptions"] = a.describe()
        except Exception as exc:  # noqa: BLE001
            import traceback  # noqa: PLC0415

            print(traceback.format_exc(), flush=True)
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(200, doc)

    def _cancel(self, job_id: str) -> None:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if job is None:
            self._json(404, {"error": "unknown job"})
            return
        job.cancel.set()
        self._json(200, {"job_id": job_id, "cancelling": True})

    def _serve_events(self, job_id: str) -> None:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if job is None:
            self._send(404, b"unknown job", "text/plain; charset=utf-8")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        # ThreadingHTTPServer + one thread per connection is what lets this
        # stream stay open while POST /cancel is still answerable.
        self.end_headers()
        try:
            while True:
                try:
                    event = job.events.get(timeout=5.0)
                except queue.Empty:
                    # A comment frame keeps proxies and the browser's own
                    # idle timeout from closing a stream that is merely
                    # between progress ticks.
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(
                    b"data: " + json.dumps(event).encode("utf-8") + b"\n\n"
                )
                self.wfile.flush()
                if event.get("type") == "end":
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            # The page navigated away or the tab closed. Cancel the run --
            # a solve nobody is watching should not keep a core busy.
            job.cancel.set()
        finally:
            with _JOBS_LOCK:
                _JOBS.pop(job_id, None)


def _settle_slots(state: dict, captured: Mapping, chosen: set, group_of: Mapping | None = None) -> None:
    """Make `state`'s socket map one the account can hold, in place. `chosen`: the boards
    whose sockets the plan re-chose; every other board keeps the account's cards.
    - `group_of` (card -> its Connect shape and permil): the matching names a placement
      by the lowest unused id of an interchangeable group, so a socket already holding
      an EQUIVALENT card would read as a swap that changes nothing. The account's card is
      kept there instead (the plan's id goes where the account's card was planned, if
      anywhere) -- the same multipliers, no move shown;
    - a card the plan placed is taken off any board that kept it (one card, one socket);
    - a socket the plan left empty keeps the account's card when the plan uses that card
      nowhere and still owns the socket: the matching only places a card where it adds
      score, so an empty socket there means "nothing to gain", not "take the card out"
      (no move shown for it). A socket the plan resets loses its card."""
    if group_of:
        where = {card: (c, sock) for c in chosen
                 for sock, card in ((state.get(c) or {}).get("slots") or {}).items()}
        for chr_id in sorted(chosen):
            slots = (state.get(chr_id) or {}).get("slots") or {}
            for sock, card in list(slots.items()):
                now = ((captured.get(chr_id) or {}).get("slots") or {}).get(sock)
                if not now or now == card or group_of.get(now) is None \
                        or group_of.get(now) != group_of.get(card):
                    continue
                if now in where:                       # the plan put the account's card elsewhere
                    oc, osock = where[now]
                    state[oc]["slots"][osock] = card
                    where[card] = (oc, osock)
                else:
                    where.pop(card, None)
                slots[sock] = now
                where[now] = (chr_id, sock)
    used = {card for c in chosen for card in ((state.get(c) or {}).get("slots") or {}).values()}
    for chr_id, entry in state.items():
        if chr_id not in chosen:
            entry["slots"] = {k: v for k, v in (entry.get("slots") or {}).items() if v not in used}
    for chr_id in chosen:
        entry = state.get(chr_id)
        if entry is None:
            continue
        slots = entry.setdefault("slots", {})
        owned = {"S-001", *(entry.get("nodes") or ())}     # the root is free on every board
        for socket, card in ((captured.get(chr_id) or {}).get("slots") or {}).items():
            # only a socket the plan still owns can keep a card (a reset S-004 cannot)
            if card and socket not in slots and card not in used and socket in owned:
                slots[socket] = card
                used.add(card)


_CONNECT_GROUPS: list = [None, 0.0]


def _connect_groups(precompute) -> dict:
    """card id -> (shape, permil) of the account's Connect cards (`connect_cards_for`), the
    key two cards are interchangeable by; re-read at most once a minute."""
    if _CONNECT_GROUPS[0] is None or time.monotonic() - _CONNECT_GROUPS[1] > 60:
        try:
            cards = precompute.connect_cards_for("account") if precompute is not None else {}
        except Exception:  # noqa: BLE001 - without it a socket simply shows the plan's id
            cards = {}
        _CONNECT_GROUPS[0] = {k: (tuple(map(tuple, v[0])), v[1]) for k, v in cards.items()}
        _CONNECT_GROUPS[1] = time.monotonic()
    return _CONNECT_GROUPS[0]


def _socket_changes(doc: dict) -> None:
    """Mark every socket of the doc with what the plan does to it (`change`: "add" an
    empty socket gets a card, "remove" its card comes off, "swap" another card goes in;
    absent = unchanged) and count them per holomem (`plan.socket_changes`), comparing the
    account's card (`placed_card`) with the plan's (`recommended_card`)."""
    for c in doc.get("characters", []):
        n = 0
        for sock in c.get("sockets", []) or []:
            now, then = sock.get("placed_card") or None, sock.get("recommended_card") or None
            change = None if now == then else "add" if not now else "remove" if not then else "swap"
            if change:
                sock["change"] = change
                n += 1
            else:
                sock.pop("change", None)
        if c.get("plan") is not None:
            c["plan"]["socket_changes"] = n


def _board_doc_for(precompute, board_state: dict, ids: list, basis_name: str, flat_budget) -> dict:
    """The board-export/v0 doc of the characters in `board_state` (+ `ids`' own) with that
    state overlaid as the plan (`_apply_plan`: to_buy / to_sell / optimised_nodes), its
    sockets the GIVEN placements, node values annotated. No optimisation call."""
    scenario = _SCENARIO_BY_BASIS.get(basis_name, "account")
    doc = precompute.board_export.build(gain=False, pool=1, scenario=scenario)
    wanted = set(board_state)
    if ids:
        wanted |= {"chr-" + c.split("-")[1] for c in ids if "-" in c}
    doc["characters"] = [c for c in doc.get("characters", []) if c["chr"] in wanted]
    overlay = {chr_id: {"nodes": list((st or {}).get("nodes") or []),
                        "slots": dict((st or {}).get("slots") or {})}
               for chr_id, st in board_state.items()}
    precompute._apply_plan(doc, overlay)
    precompute._annotate_plan_budget(doc, char_level="current")
    if flat_budget is not None:
        for c in doc["characters"]:
            if c.get("plan"):
                c["plan"]["point_budget"] = flat_budget
    # sockets: the GIVEN placements, folded onto each character's socket
    # list the way `_annotate_sockets` does — no optimiser call here
    names = precompute._card_names() if hasattr(precompute, "_card_names") else {}
    for c in doc["characters"]:
        slots = (overlay.get(c["chr"]) or {}).get("slots") or {}
        for sock in c.get("sockets", []) or []:
            if sock.get("placed_card"):
                now = names.get(sock["placed_card"]) or {}
                sock["placed_card_name"] = now.get("name") or sock["placed_card"]
                sock["placed_card_member"] = now.get("member") or ""
            cid = slots.get(sock.get("socket"))
            if cid:
                info = names.get(cid) or {}
                sock["recommended_card"] = cid
                sock["recommended_card_name"] = info.get("name") or cid
                sock["recommended_card_member"] = info.get("member") or ""
    _socket_changes(doc)
    doc["for_team"] = ids
    doc["plan_included"] = True
    # node values + Connect ranges/boosts (the page prints the game's numbers)
    board_values.annotate(doc, _read_json(SKILLTREE_PATH) or {},
                          _read_json(COLLECTION_PATH) or {},
                          maxed=basis_name == MAXED_ACCOUNT_BASIS)
    doc["engine"] = "given"
    doc["budget"] = "real" if flat_budget is None else flat_budget
    doc["budget_kind"] = "real" if flat_budget is None else "flat"
    return doc


class ThreadingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    # ⛔ FALSE, and on Windows this is a correctness fix rather than a
    # preference. `SO_REUSEADDR` means "reuse a socket in TIME_WAIT" on
    # POSIX, but on Windows it means **a second process may HIJACK a port
    # another process is already listening on**. Both bind, both print
    # "serving on ...", and requests go to whichever the stack picks --
    # in practice the older one.
    #
    # Example: a server started the previous day still holds 8731. Every
    # relaunch announces itself successfully and the browser keeps talking to
    # the old process, which has none of the new routes. The page reports
    # "404, your server is older than this page", the operator restarts
    # exactly as told, and nothing changes -- because the process being
    # restarted is never the one answering.
    #
    # With this False, a second launch FAILS on bind and says so. A tool
    # that lies about having started is worse than one that refuses to.
    allow_reuse_address = False

    def handle_error(self, request, client_address) -> None:
        """A browser that drops its connection (a closed tab, a reload, an
        EventSource closed once its search ended -- WinError 10053/10054) is not
        a server fault: one line instead of socketserver's traceback. Anything
        else keeps the full traceback."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            print(f"{client_address[0]} closed the connection ({type(exc).__name__})", flush=True)
            return
        super().handle_error(request, client_address)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tools/web/server.py",
        description="Local Solve -> Result view. Binds 127.0.0.1 only.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--art-dir",
        default=str(DEFAULT_ART_DIR),
        help=(
            "Directory holding <card-id>.webp portraits. Default is "
            "tools/explorer/cards_explorer_art in this checkout; point it at "
            "another checkout's copy rather than copying the files."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # a custom User-Agent on the requests to the holobuilder server: it sits behind
    # Cloudflare, whose browser checks stop Python's default one
    import urllib.request  # noqa: PLC0415

    opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", "holobuilder-web")]
    urllib.request.install_opener(opener)
    # The compiled note walk (tools/web/kernel): bit-identical to the Python path
    # and ~4x faster per song score. `enable()` sets it for this process and, via
    # the environment, for every worker spawned from here; it returns False and
    # changes nothing if there is no compiler. HBUILDER_NO_KERNEL=1 opts out.
    if not os.environ.get("HBUILDER_NO_KERNEL"):
        sys.path.insert(0, str(REPO_ROOT / "tools" / "web" / "kernel"))
        import notewalk  # noqa: PLC0415

        notewalk.enable()
        # ...and the compiled Connect socket assignment, the same contract
        # (`board_plan.CONNECT_KERNEL`): ~280 -> ~40 us per developed team.
        import connect  # noqa: PLC0415

        connect.enable()
    # `apply_collection` (cli.py) resolves the skill ladder, level curve and caps
    # by paths RELATIVE to CWD. Launched from anywhere but the repo root the solver
    # silently scores MAXED skills (e.g. Sora 3* ACTIVE reads 55 (level 2)
    # instead of its real 40 (level 1)). Anchor CWD so the optimiser always loads
    # them, whatever directory the server was started from.
    os.chdir(REPO_ROOT)
    args = build_parser().parse_args(argv)
    Handler.art_dir = Path(args.art_dir).resolve()
    try:
        server = ThreadingServer((BIND_HOST, args.port), Handler)
    except OSError as exc:
        # The message matters more than the traceback: the operator's next
        # move is to find and kill the process holding the port, and on
        # Windows that process may be elevated and refuse an ordinary kill.
        print(
            f"cannot bind {BIND_HOST}:{args.port} -- {exc}\n"
            f"\n"
            f"Something is already listening there. Find it with:\n"
            f"    Get-NetTCPConnection -LocalPort {args.port} -State Listen\n"
            f"then stop that PID (an elevated shell may be needed), or start\n"
            f"this on another port with --port.\n"
            f"\n"
            f"This used to SUCCEED and serve nothing: Windows lets a second\n"
            f"process hijack a listening port when SO_REUSEADDR is set, so\n"
            f"both announced themselves and the older one answered.",
            file=sys.stderr,
        )
        return 2
    art_note = (
        f"{Handler.art_dir}"
        if Handler.art_dir.is_dir()
        else f"{Handler.art_dir} (missing -- faces degrade to gradients)"
    )
    print(f"art: {art_note}", file=sys.stderr)
    print(f"serving on http://{BIND_HOST}:{args.port}/", file=sys.stderr)
    # ON by default: the curves and the heatmap
    # need a dense series; it idles an hour when no chapter is open. HBUILDER_CUTOFFS_POLL_MIN=0
    # turns it off, any other value sets the interval.
    poll_min = os.environ.get("HBUILDER_CUTOFFS_POLL_MIN", str(cutoffs_poller.DEFAULT_INTERVAL_MIN))
    if poll_min.strip().lower() in ("0", "off", "no", ""):
        print("cutoffs poller: off (HBUILDER_CUTOFFS_POLL_MIN=0)", file=sys.stderr)
    else:
        try:
            _CUTOFFS_POLLER.configure(enabled=True, interval_min=poll_min)
            print(f"cutoffs poller: on, every {_CUTOFFS_POLLER.interval_min} min "
                  + ("(pulls the VPS tracker)" if _CUTOFFS_POLL_SCRIPT == CUTOFFS_PULL_SCRIPT
                     else "(guests only)"), file=sys.stderr)
        except ValueError as exc:
            print(f"cutoffs poller: HBUILDER_CUTOFFS_POLL_MIN ignored -- {exc}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _CUTOFFS_POLLER.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
