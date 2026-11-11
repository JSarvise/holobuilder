"""Request parsing shared by every route, and the three card bases a request may name.

A basis says which collection a team is scored over: `account` (the cards as the account
has them), `maxed` (every card at max level and bloom, the account's own boards and
bonuses), `maxed-account` (the maxed cards on a fully maxed account, the reference
scenario below). `apply_basis` is the one place a request's basis becomes cards + config.

Two tables use the word `maxed`. Here (the routes that score a given team: compare, timeline,
board documents) `maxed` keeps the account's own captured bench boards, and `maxed-account` is
the fully maxed account. In `leaf_inputs.BASES` (the search and `/api/row-detail`) `maxed` has
every bench holomem's board developed too (`leaf_inputs._with_maxed_bench`). So a row the Optimizer found on its
"maxed" basis is re-scored by `/api/row-detail` on the maxed account, while a follow-up sent
here with `basis: "maxed"` (the row's "Now" through `/api/compare`) scores the same maxed cards
on the account's captured boards.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import os
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import certificate as cert_mod

from hbuilder.domain.board import board_from_mapping
from hbuilder.domain.scoring_config import DEFAULT_PLATFORM, PLATFORMS
from hbuilder.domain.skilltree import TOTAL_BOARD_POINTS
from hbuilder.errors import MissingInput
from hbuilder.inputs import apply_collection, max_level_board_points
from hbweb import config

if TYPE_CHECKING:
    from types import ModuleType

    from hbuilder.domain.card import Card
    from hbuilder.domain.scoring_config import ScoringConfig
    from hbuilder.inputs import CollectionBasis


def web_workers() -> int | None:
    """Process count for the web's refine / across pools: `HBUILDER_WEB_WORKERS`
    when set (a measurement next to another batch job must not take every
    core), else the solver's own default (every logical core)."""
    raw = os.environ.get("HBUILDER_WEB_WORKERS", "").strip()
    try:
        return max(1, int(raw)) if raw else None
    except ValueError:
        return None


# The account BASIS names a request sends, mapped to the board planner's SCENARIO names
# (which collection state a team's boards are planned over).
SCENARIO_BY_BASIS = {
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


def parse_basis(raw: object) -> str:
    """`account` | `maxed` | `maxed-account`; unknown -> `account`."""
    basis = str(raw or "account").lower()
    return basis if basis in BASES else "account"


def parse_budget(raw: Any) -> int | None:
    """A request's flat board budget: None for absent / "" / "real" (each holomem's own
    points) or a value that is not a number, "max" for what a max-level holomem holds
    (`maxed_account_budget`), an int clamped to 0..the whole tree's cost."""
    if raw is None or str(raw).strip().lower() in ("", "real"):
        return None
    if str(raw).strip().lower() == "max":
        return maxed_account_budget()
    try:
        return max(0, min(TOTAL_BOARD_POINTS, int(raw)))
    except (TypeError, ValueError):
        return None


def cards_maxed(basis: str) -> bool:
    """Whether the basis scores the MAXED catalogue (every card, level/bloom
    max) — true for `maxed` and `maxed-account`; the two differ only in the
    account around the cards (bench boards + Memory / Power-Up bonuses)."""
    return basis in ("maxed", MAXED_ACCOUNT_BASIS)


def charts_path(difficulty: str = "EXPERT") -> Path:
    """The chart file a run at `difficulty` reads (`certificate.charts_path_for`,
    against `config`'s paths so a test that moves them moves both)."""
    return cert_mod.charts_path_for(
        difficulty, charts_path=config.CHARTS_PATH, charts_all_path=config.CHARTS_ALL_PATH
    )


# ---------------------------------------------------------------------------
# The MAXED-ACCOUNT basis: "best team on a FULLY MAXED account", on the same basis the
# community tool holodori.best answers it, so the numbers are comparable. The `maxed` basis
# maxes the CARDS but keeps the account's captured bench boards and account counters (2.76 M
# vs 3.42 M average song score on the same kind of roster). This basis is `maxed` + the
# reference scenario (`precompute_optimisations.py`, bottom):
#   * account bonuses = `maxed_account_bonuses`: Memory posters = the MASTER's poster count,
#     Member Power-Up = the roster ceiling — the same two rates holodori.best uses;
#   * bench = `maxed_account_bench`: EVERY holomem at max level, her points
#     (`max_level_board_points`) on what of her board reaches a team from the bench, its
#     Connect sockets filled at level 2 from the whole catalogue by the domain's exact
#     assignment, valued at a FIXED nominal team — so the bench never depends on the team
#     being scored;
#   * connect pool = `connect_cards_for("maxed-account")` (every card, level 2);
#   * the developed board budget defaults to the same max-level points
#     (`maxed_account_budget`): no account holds more, the whole tree costs more.
# The bench takes a few seconds to build (54 boards, one exact matching), so it is cached per
# process, keyed by the resolved config it is linearised at (`cert_mod.cfg_json`) — the same
# key the certificate hashes.
# ---------------------------------------------------------------------------
# The nominal team the bench sockets are valued at: holodori.best's recommended roster, fixed
# here so the bench is a function of the inputs only. Noel Soleil Kiss (leader + member),
# Matsuri VCS, Flare EGG, Fubuki KR, Hajime BSG.
MAXED_ACCOUNT_NOMINAL_TEAM = (
    "card-00022-5-uniq-0063-00",
    (
        "card-00007-5-uniq-0008-00",
        "card-00022-5-uniq-0063-00",
        "card-00021-5-uniq-0017-00",
        "card-00006-5-uniq-0007-00",
        "card-06005-5-uniq-0061-00",
    ),
)


def maxed_account_budget() -> int:
    """The board points of every holomem on a maxed account, bench and team alike: what a
    max-level holomem holds (`max_level_board_points`, the master's `CharacterLevel`).
    Raises `MaxedAccountUnavailable` when the master export is absent."""
    try:
        return max_level_board_points()
    except MissingInput as exc:
        raise MaxedAccountUnavailable(str(exc)) from None


def maxed_account_label(points: int) -> str:
    return f"maxed account · 54 bench boards at max level ({points} Pt) · every poster · power-up ceiling"


@dataclass(frozen=True)
class MaxedAccountScenario:
    """Everything the maxed-account basis adds on top of the maxed cards."""

    bonuses: Any  # AccountBonuses (posters, power_up: the maxed account's)
    bench: dict  # name-keyed `characters` mapping (board.json shape)
    connect_cards: dict | None  # `connect_cards_for("maxed-account")`
    board_model: Any  # BoardModel of the bench (the "current" board)
    literal: str  # the scenario literal the collection line hashes
    bench_sockets: int  # sockets the exact assignment filled (54 expected)
    report: tuple[str, ...]  # build_board's report + problems (diagnostics)
    points: int  # every holomem's board points (`maxed_account_budget`)

    def current_by_chr(self, name_by_chr: Mapping[str, str]) -> dict:
        """The `current_by_chr` baseline a developed row's `board_plan_summary`
        diffs against, for THIS basis: every bench node owned, the max-level
        points granted — so a develop on a maxed account reads as a respec of a
        max-level board, not as buying the account's missing nodes."""
        out = {}
        for chr_id, name in name_by_chr.items():
            rows = self.bench.get(name)
            if rows is None:
                continue
            out[chr_id] = {
                "name": name,
                "owned": sorted(str(r.get("node")) for r in rows if r.get("node")),
                "granted": self.points,
            }
        return out

    def summary(self) -> dict:
        b = self.bonuses
        return {
            "basis": MAXED_ACCOUNT_BASIS,
            "label": maxed_account_label(self.points),
            "bench_boards": len(self.bench),
            "bench_budget": self.points,
            "bench_sockets": self.bench_sockets,
            "posters": getattr(b, "posters", None),
            "power_up_permyriad": getattr(b, "power_up_permyriad", None),
            "connect_cards": len(self.connect_cards or {}),
            "nominal_team": [MAXED_ACCOUNT_NOMINAL_TEAM[0], *MAXED_ACCOUNT_NOMINAL_TEAM[1]],
            "scenario": self.literal,
        }


_MAXED_ACCOUNT_CACHE: dict[str, MaxedAccountScenario] = {}
_MAXED_ACCOUNT_LOCK = threading.Lock()


class MaxedAccountUnavailable(RuntimeError):
    """The scenario cannot be built (a dump is missing, the recon module will
    not import): a first-class, typed absence the routes turn into a 503."""


def load_precompute() -> ModuleType | None:
    """`precompute_optimisations.py` under `tools/refresh/`, imported by path. Its
    module-level code is import-safe (it only imports `board_export`/`socket_optimise`/
    `tree_optimise` and defines functions). Returns None if unloadable."""
    try:
        spec = importlib.util.spec_from_file_location(
            "precompute_optimisations", config.REFRESH_DIR / "precompute_optimisations.py"
        )
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module
    except Exception:  # noqa: BLE001 — any failure means "no on-demand plan"
        return None


def maxed_account_scenario(
    cards: tuple[Card, ...],
    cfg: ScoringConfig,
    collection_basis: CollectionBasis,
    *,
    precompute: ModuleType | None = None,
) -> MaxedAccountScenario:
    """The cached scenario for `cards` (the maxed catalogue) at `cfg` (its
    account bonuses REPLACED by the maxed-account ones before the bench is
    linearised, so the cache key is the config the bench is a function of).
    Raises `MaxedAccountUnavailable` with the reason; never guesses."""
    precompute = precompute or load_precompute()
    if precompute is None or not hasattr(precompute, "maxed_account_bench"):
        raise MaxedAccountUnavailable(
            "could not import precompute_optimisations.py (maxed_account_bench) — "
            "the maxed-account basis needs the packages of requirements.txt "
            "(setup.cmd or setup.sh installs them) and the gitignored dumps/ (skilltree.json, "
            "master_SkillTreeNode.json, holodori/main_Poster.json)."
        )
    try:
        bonuses = precompute.maxed_account_bonuses(collection_basis)
    except Exception as exc:  # noqa: BLE001 — reported, typed
        raise MaxedAccountUnavailable(f"maxed_account_bonuses: {type(exc).__name__}: {exc}") from exc
    if bonuses is None or precompute.master_poster_count() is None:
        raise MaxedAccountUnavailable(
            "the maxed-account basis needs the master Poster table "
            "(dumps/holodori/main_Poster.json) for its Memory Bonus and the level "
            "curve for the Power-Up ceiling; one of them is absent."
        )
    cfg_scn = dataclasses.replace(cfg, account=bonuses, board=None)
    # the bench is a function of the config and of the max-level points (a game update
    # may move them)
    key = f"{cert_mod.cfg_json(cfg_scn)}\npoints={maxed_account_budget()}"
    with _MAXED_ACCOUNT_LOCK:
        hit = _MAXED_ACCOUNT_CACHE.get(key)
        if hit is not None:
            return hit
        scn = _build_maxed_account_scenario(cards, cfg_scn, bonuses, precompute)
        _MAXED_ACCOUNT_CACHE[key] = scn
        return scn


def _build_maxed_account_scenario(cards, cfg_scn, bonuses, precompute) -> MaxedAccountScenario:
    """The scenario's bench: every holomem at max level, sockets filled at the nominal team."""
    skilltree = config.read_json(config.SKILLTREE_PATH)
    node_master = config.read_json(config.NODE_MASTER_PATH)
    if not skilltree or not node_master:
        raise MaxedAccountUnavailable(
            "the maxed-account bench needs skilltree.json + "
            "master_SkillTreeNode.json under the gitignored dumps/; run "
            "tools/refresh/build_skilltree.py"
        )
    by_id = {c.id: c for c in cards}
    leader, members = MAXED_ACCOUNT_NOMINAL_TEAM
    missing = [c for c in (leader, *members) if c not in by_id]
    if missing:
        raise MaxedAccountUnavailable(
            f"the bench's nominal team names card(s) not in the catalogue: {missing}"
        )
    points = maxed_account_budget()
    report: list[str] = []
    try:
        connect_cards = precompute.connect_cards_for(precompute.MAXED_ACCOUNT_SCENARIO)
        bench = precompute.maxed_account_bench(
            cards,
            cfg_scn,
            (leader, tuple(members)),
            skilltree=skilltree,
            node_master=node_master,
            connect_cards=connect_cards,
            points=points,
            report=report,
        )
        board_model = board_from_mapping(
            {"characters": {k: list(v) for k, v in bench.items()}}, source="<maxed-account bench>"
        )
    except Exception as exc:  # noqa: BLE001 — reported, typed
        raise MaxedAccountUnavailable(f"maxed_account_bench: {type(exc).__name__}: {exc}") from exc
    sockets = sum(1 for line in report if str(line).strip().startswith("S-00"))
    literal = (
        f"maxed-account:level=max,bloom=max,posters={bonuses.posters},"
        f"power_up_permyriad={bonuses.power_up_permyriad},bench=maxlevel{points}+L2sockets"
    )
    return MaxedAccountScenario(
        bonuses=bonuses,
        bench=bench,
        connect_cards=connect_cards,
        board_model=board_model,
        literal=literal,
        bench_sockets=sockets,
        report=tuple(str(x) for x in report),
        points=points,
    )


def apply_basis(
    cards: tuple[Card, ...],
    owned: Iterable[Card],
    cfg: ScoringConfig,
    basis: str,
    *,
    assume_level: object = None,
    assume_bloom: object = None,
    precompute: ModuleType | None = None,
) -> tuple[tuple[Card, ...], CollectionBasis, ScoringConfig, MaxedAccountScenario | None]:
    """The ONE place a request's card basis becomes (cards, collection_basis,
    cfg, scenario): `apply_collection` exactly as the CLI applies it, then —
    on the maxed-account basis only — the account bonuses swapped for the
    scenario's and the scenario built (cached). `scenario` is None on the
    other two bases. `cfg.board` is NOT touched here (each route decides
    which board it scores); the scenario's `board_model` is the maxed-account
    "current" board for the routes that score one."""
    maxed = cards_maxed(basis)
    cards, collection_basis = apply_collection(
        cards,
        None if maxed else str(config.COLLECTION_PATH),
        owned_ids=[c.id for c in owned],
        maxed=maxed,
        assume_level=assume_level,
        assume_bloom=assume_bloom,
    )
    if collection_basis.bonuses is not None:
        cfg = dataclasses.replace(cfg, account=collection_basis.bonuses)
    scenario = None
    if basis == MAXED_ACCOUNT_BASIS:
        scenario = maxed_account_scenario(cards, cfg, collection_basis, precompute=precompute)
        cfg = dataclasses.replace(cfg, account=scenario.bonuses)
    return cards, collection_basis, cfg, scenario


def certificate_meta(
    cfg: ScoringConfig, basis_name: str, *, scenario: MaxedAccountScenario | None = None
) -> dict[str, Any]:
    """`model_version` + `inputs_hash` for a response. The
    hash is over the SAME resolved config shape the certificates hash — `cfg` with
    `board=None` (the board a run develops is an OUTPUT; the captured board file
    is hashed as an input by the recipe itself), platform, uptime model and the
    basis' account bonuses. A missing dump makes the hash `null` with the
    reason, never a hash over whatever files happened to exist. On the
    maxed-account basis the collection line is the scenario literal + the
    generated bench's digest (`cert_mod.scenario_hash`, the offline certificates' recipe)."""
    if basis_name == MAXED_ACCOUNT_BASIS and scenario is not None:
        h, per, err = cert_mod.try_scenario_hash(
            dataclasses.replace(cfg, board=None), scenario.bench, scenario.literal
        )
    else:
        h, per, err = cert_mod.try_inputs_hash(
            dataclasses.replace(cfg, board=None), "maxed" if basis_name == "maxed" else "account"
        )
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


# ---------------------------------------------------------------------------
# Request parsers: every route that takes one of these tokens reads it here, so a
# follow-up request scores on the same terms as the ranking it came from.
# ---------------------------------------------------------------------------
class HttpError(Exception):
    """A typed HTTP answer raised from inside a request's work (a bad request, a
    missing dump); the route turns it into JSON."""

    def __init__(self, code: int, obj: dict) -> None:
        super().__init__(obj.get("error") if isinstance(obj, dict) else str(obj))
        self.code = code
        self.obj = obj


def parse_platform(body: Mapping) -> str:
    """The client whose PERFECT column the per-song scorer weighs notes with:
    "mobile" (FLICK 1050, the default) or "pc" (FLICK
    1000). Absent/empty/unknown -> the default, never an error: the toggle is a
    what-if, and every route that scores a chart reads it through this one function
    so no follow-up can re-score on a different platform than the ranking above it."""
    platform = str(body.get("platform") or "").lower()
    return platform if platform in PLATFORMS else DEFAULT_PLATFORM


def parse_difficulty(body: Mapping, song: str = "") -> str:
    """The chart difficulty a route scores on (EXPERT is the default; a manual
    player may want an easier chart). Absent / "" ->
    EXPERT. Unlike the platform toggle an unknown value is an ERROR, a 400 —
    silently scoring EXPERT for a typo would answer a question nobody asked.
    A non-EXPERT difficulty must be on disk (charts_all.json) and, with `song`,
    the song must have a chart at it."""
    try:
        return cert_mod.resolve_difficulty(
            body.get("difficulty"),
            song or None,
            charts_path=config.CHARTS_PATH,
            charts_all_path=config.CHARTS_ALL_PATH,
        )
    except ValueError as exc:
        raise HttpError(400, {"error": str(exc)}) from None


def parse_assume(body: Mapping, key: str, lo: int, hi: int) -> object:
    """The level/bloom what-if token: None/""/"account" -> None (no override,
    use the account's own value); "max" -> the card's cap; an int -> that value
    clamped to [lo, hi]."""
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


# Which deck power the per-song numbers are on, a request knob beside `uptime_model` and
# `platform`. The DEFAULT (`song_absolute_deck_power=True`) is the deck screen's own Overall
# Power, the quantity the game multiplies. `"modelled"` asks for the stat-pool basis: the
# parity references against the community tool holodori.best are measured on it, one of the
# few NON-CIRCULAR checks available (a third-party implementation), so it keeps asking the
# question it was calibrated for instead of being re-baselined on this project's own number.
DECK_POWER_BASES = {"server": True, "modelled": False}


def auto_premise(cfg: ScoringConfig, body: Mapping) -> dict:
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


def deck_power_basis(body: Mapping) -> dict:
    """`{}` (inherit the default) or `{"song_absolute_deck_power": bool}`."""
    choice = str(body.get("deck_power") or "").lower()
    if choice in DECK_POWER_BASES:
        return {"song_absolute_deck_power": DECK_POWER_BASES[choice]}
    return {}
