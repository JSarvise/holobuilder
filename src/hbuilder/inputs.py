"""Reading the inputs every scorer needs: the card catalogue, the owned list, the scoring
config, the account's collection (levels, blooms, limit breaks, posters, power-up) and its
boards, with the paths they live at by default.

`load_inputs` is the one read path for `data/cards.json` and `data/owned.json` (the web's
no-writes guard pins it), `apply_collection` restates the catalogue at the account's (or the
maxed) card state, and `load_board_basis` reads the captured boards.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from hbuilder import paths
from hbuilder.domain.board import BoardModel, load_board
from hbuilder.domain.card import Card, Stats
from hbuilder.domain.collection import (
    FULLY_SCORABLE_BLOOM,
    account_bonuses_from_mapping,
    caps_by_rarity_from_mapping,
    collection_stats,
    derived_member_power_up_permyriad,
    maxed_account_bonuses,
    restate_card_skills,
    skill_ladder_from_mapping,
    stat_basis_from_mapping,
)
from hbuilder.domain.loader import (
    CardProvenance,
    card_provenance_of,
    load_cards,
    load_owned,
)
from hbuilder.domain.schema import SchemaError
from hbuilder.domain.scoring_config import (
    DEFAULT_CONFIG_PATH,
    ScoringConfig,
    load_scoring_config,
)
from hbuilder.domain.team import TeamError

# What a failed `load_inputs` carries (`_LoadFailure`): which input was bad.
EXIT_INPUT_DATA = 3  # cards.json / owned.json missing, unreadable, malformed.
EXIT_CONFIG = 4  # config.json fails validate_scoring_config.


# Where `tools/refresh/build_board.py` writes by convention. Under
# `dumps/`, which is gitignored: a board is captured ACCOUNT state, so this
# file does not exist in a fresh checkout and the board then scores as zero.
DEFAULT_BOARD_PATH = f"{paths.DUMPS_REL}/board.json"


# The chart library the per-song modes score against, built by
# `tools/refresh/build_charts.py`. Same contract as the board: absent is a
# first-class state.
DEFAULT_CHARTS_PATH = f"{paths.DUMPS_REL}/charts.json"


# Where the collection editor writes, and where `--collection` looks by default.
# Nothing in this module writes it; the CLI only ever reads.
DEFAULT_COLLECTION_PATH = "state/collection.json"


# Both gitignored under dumps/: extracted game data.
COLLECTION_CURVE_PATH = f"{paths.DUMPS_REL}/card_level_curve.json"


COLLECTION_CAPS_PATH = f"{paths.DUMPS_REL}/card_caps.json"


# The per-level skill rows, for the bloom down-level. ⚠ NOT `skill_levels.json`
# -- that path may hold a stale file no current tool generates.
COLLECTION_LADDER_PATH = f"{paths.DUMPS_REL}/skill_ladder.json"


# The master's `Poster` table (one row per poster the game has): the maxed basis's
# Memory Bonus input. Written by the refresh's master export.
COLLECTION_POSTER_PATH = f"{paths.DUMPS_REL}/holodori/main_Poster.json"


def master_poster_count(path: Path | str = paths.REPO / COLLECTION_POSTER_PATH) -> int | None:
    """How many posters the game has (rows of the master `Poster` export), or
    `None` when the export is absent or unreadable."""
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(rows, dict):
        rows = rows.get("rows") or rows.get("data") or []
    return len(rows) if isinstance(rows, list) else None


# The master's `CharacterLevel` table (one row per holomem level, with the Holomem Board
# points reaching that level grants): the maxed basis's board budget. Written by the
# refresh's master export. Every playable holomem levels on the one group below.
CHARACTER_LEVEL_PATH = f"{paths.DUMPS_REL}/holodori/main_CharacterLevel.json"
CHARACTER_LEVEL_GROUP = "level-group-1"


def max_level_board_points(
    path: Path | str = paths.REPO / CHARACTER_LEVEL_PATH, group: str = CHARACTER_LEVEL_GROUP
) -> int:
    """The Holomem Board points a holomem holds at the maximum level: the sum of
    `skillTreePointQuantity` over every level of `group` in the master `CharacterLevel`
    export. Levelling is the only source of board points, so this is the most any
    account can spend on one board (the whole tree costs more).

    Raises `MissingInput` when the export is absent, unreadable or has no row of
    `group`: a missing budget must never read as zero points or as the account's own."""
    from hbuilder.errors import MissingInput  # noqa: PLC0415

    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MissingInput(
            f"cannot read {path} ({type(exc).__name__}): the max-level board "
            "points come from the master export the refresh writes"
        ) from None
    if isinstance(rows, dict):
        rows = rows.get("rows") or rows.get("data") or []
    total, levels = 0, 0
    for row in rows if isinstance(rows, list) else ():
        data = (row or {}).get("data") or {}
        if (data.get("groupId") or row.get("group_id")) != group:
            continue
        levels += 1
        total += int(data.get("skillTreePointQuantity") or 0)
    if not levels:
        raise MissingInput(
            f"{path} has no level of {group}: the max-level board points "
            "come from the master export the refresh writes"
        )
    return total


# Which load step was in flight when a load-and-guard failure was
# raised, so the shared guard can route to EXIT_INPUT_DATA or EXIT_CONFIG
# without a second load path anywhere in this module.
_STEP_CARDS = "cards"
_STEP_OWNED = "owned"
_STEP_CONFIG = "config"


class _LoadFailure(Exception):
    """Internal control-flow exception carrying the exit code
    `load_inputs`'s caller should return. Never escapes this module."""

    def __init__(self, exit_code: int) -> None:
        super().__init__(exit_code)
        self.exit_code = exit_code


@dataclasses.dataclass(frozen=True, slots=True)
class BoardBasis:
    """Which of the two board states a run is in, and the numbers that prove
    it. Reported rather than inferred: the Holomem Board is roughly 30-50 % of
    Overall Power, so "board loaded" and "board absent" are two different
    answers to the same question and a reader must never have to guess which
    one they are holding."""

    path: str | None
    model: BoardModel | None
    characters: int
    nodes: int

    def lines(self) -> list[str]:
        if self.model is None:
            if self.path is None:
                return ["BOARD: disabled -- the Holomem Board scores as ZERO (29.4-48.7 % of Overall Power)"]
            return [
                f"BOARD: none ({self.path} absent) -- the Holomem Board scores "
                "as ZERO (29.4-48.7 % of Overall Power); "
                "build it with tools/refresh/build_board.py"
            ]
        return [
            f"BOARD: loaded {self.path} -- {self.characters} character board(s), "
            f"{self.nodes} unlocked node(s) SCORED"
        ]

    def payload(self) -> dict:
        return {
            "loaded": self.model is not None,
            "path": self.path,
            "characters": self.characters,
            "nodes": self.nodes,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class CollectionBasis:
    """Which of the two collection states a run is in, and what it changed.

    Reported for the same reason `BoardBasis` is: **every card in
    `data/cards.json` carries its FULLY-MAXED stats**, so a ranking without a
    collection assumes a maxed collection. A run scored against the account's
    real levels is a different answer to the same question and must say so."""

    bonuses: object | None
    path: str | None
    applied: int
    unchanged: int
    not_scorable: tuple[str, ...]
    warnings: tuple[str, ...]
    # How many cards had a skill slot replaced by the level their bloom bought,
    # and how many wanted one and had no exported row (`not_scorable` above
    # names these cards).
    skills_restated: int = 0
    skills_unavailable: tuple[str, ...] = ()
    # `P` computed from the account's own owned cards, so the hand-typed
    # `power_up_permyriad` can be contradicted instead of trusted.
    derived_power_up: object | None = None
    # Scoring a fully-maxed account instead of the real one — a different
    # question, deliberately available, and never the silent default.
    maxed: bool = False

    @property
    def loaded(self) -> bool:
        return self.applied > 0

    def lines(self) -> list[str]:
        if self.maxed:
            power_up = getattr(self.bonuses, "power_up_permyriad", None)
            posters = getattr(self.bonuses, "posters", None)
            return [
                "BASIS: MAXED ACCOUNT -- every card owned, at max level and max "
                "bloom. This is NOT your account; it answers what a team would be "
                "worth maxed, which is the only basis on which an unowned card "
                "can be scored at all",
                "BASIS: max bloom means every skill is at level 2, so no skill is "
                "down-levelled and the skill level of ACTIVE/SPECIAL cannot "
                "affect this answer either way",
                (
                    f"BASIS: Memory Bonus at {posters} posters (every poster the game has)"
                    if posters is not None
                    else f"BASIS: ⚠ Memory Bonus NOT counted -- no {COLLECTION_POSTER_PATH} "
                    "to say how many posters the game has"
                )
                + "; Member "
                f"Power-Up at the roster ceiling "
                f"({power_up if power_up is not None else 'unknown -- no card_level_curve.json'})",
                "BASIS: ⚠ the Holomem Board is still YOUR REAL BOARD, not a maxed "
                "one. What a maxed board should buy depends on the song, the cast "
                "and the event, and is not decided -- so it is left real rather "
                "than invented. Turn boards off to exclude it entirely",
            ]
        if self.path is None:
            return [
                "COLLECTION: not applied -- every card scores at "
                "MAX level and bloom, which is what data/cards.json holds"
            ]
        if not self.loaded:
            return [
                f"COLLECTION: none ({self.path} absent or unresolvable) -- every "
                f"card scores at MAX level and bloom, which is what "
                f"data/cards.json holds"
            ]
        lines = [
            f"COLLECTION: loaded {self.path} -- {self.applied} card(s) restated "
            f"from your levels, {self.unchanged} left at data/cards.json's value"
        ]
        if self.skills_restated:
            lines.append(
                f"COLLECTION: {self.skills_restated} card(s) had a skill dropped "
                f"to the level their bloom actually bought -- data/cards.json "
                f"carries the level-2 row for every card"
            )
        if self.skills_unavailable:
            lines.append(
                f"COLLECTION: {len(self.skills_unavailable)} card(s) below their "
                f"bloom step have NO exported level-1 row and stay OVERSTATED -- "
                f"run tools/refresh/import_to_cards.py: "
                f"{', '.join(self.skills_unavailable[:5])}"
                + (" ..." if len(self.skills_unavailable) > 5 else "")
            )
        elif self.not_scorable:
            lines.append(
                f"COLLECTION: {len(self.not_scorable)} card(s) below bloom "
                f"{FULLY_SCORABLE_BLOOM} -- their stats are correct, but "
                f"data/cards.json's level-2 SPECIAL/PASSIVE magnitudes OVERSTATE "
                f"them: {', '.join(self.not_scorable[:5])}" + (" ..." if len(self.not_scorable) > 5 else "")
            )
        lines.extend(self._power_up_lines())
        return lines

    def _power_up_lines(self) -> list[str]:
        """What the account's own cards say `power_up_permyriad` should be.

        Reported, never applied. `state/collection.json` is the user's file and
        no automated path here writes it -- so the run states the contradiction
        and the number to type, and scores what the file says."""
        # ASCII only. `main()` reconfigures stdout to UTF-8, but these lines are
        # also read by tools/web and by anything piping stderr, and a report
        # that raises UnicodeEncodeError on a Windows console is a report that
        # does not arrive.
        derived = self.derived_power_up
        if derived is None:
            return []
        typed = getattr(self.bonuses, "power_up_permyriad", None)
        if getattr(self.bonuses, "power_up_permyriad_is_auto", False):
            if derived.exact:
                return [
                    f"COLLECTION: power_up_permyriad = {derived.permyriad}, DERIVED "
                    f"from your {derived.counted} owned cards (player."
                    f'power_up_permyriad is "auto" -- it cannot go stale)'
                ]
            return [
                f'COLLECTION: power_up_permyriad is "auto" but NOT derivable -- '
                f"{len(derived.skipped)} owned card(s) have no recorded level, so "
                f"the answer is only bounded to {derived.permyriad}..{derived.upper}. "
                f"The term is left unset rather than guessed; refresh with "
                f"tools/refresh/collection_from_snapshot.py"
            ]
        if derived.exact:
            says = f"derive {derived.permyriad}"
            fix = str(derived.permyriad)
        else:
            says = (
                f"derive {derived.permyriad}..{derived.upper} "
                f"({len(derived.skipped)} owned card(s) have no recorded level, "
                f"so this is a bound, not a figure)"
            )
            fix = f"{derived.permyriad} and record the {len(derived.skipped)} missing level(s)"

        if typed is None:
            return [
                f"COLLECTION: your {len(derived.skipped) + derived.counted} owned "
                f"cards {says}; player.power_up_permyriad is unset"
            ]
        if not derived.disagrees_with(typed):
            return []
        return [
            f"COLLECTION: !! player.power_up_permyriad is {typed}, but your "
            f"owned cards {says}"
            + (
                f". The Member Power-Up Bonus is being scored at "
                f"{typed / derived.upper:.0f}x its largest possible size"
                if derived.upper and typed > derived.upper
                else ""
            ),
            f"COLLECTION: state/collection.json is YOURS -- nothing here writes "
            f"it. Set player.power_up_permyriad to {fix}",
        ]

    def payload(self) -> dict:
        return {
            "loaded": self.loaded,
            "path": self.path,
            "cards_restated": self.applied,
            "cards_unchanged": self.unchanged,
            "maxed": self.maxed,
            "not_fully_scorable": list(self.not_scorable),
            "skills_restated": self.skills_restated,
            "skills_unavailable": list(self.skills_unavailable),
        }


def _load_json_if_present(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _override_collection_levels(
    document: dict,
    assume_level: object,
    assume_bloom: object,
    keep_higher_level: bool = False,
    keep_higher_bloom: bool = False,
    assume_limit_break: object = None,
) -> dict:
    """`document` with every card entry — and `card_default` — forced to the
    assumed level and/or bloom: the "what if all my cards were at level X / bloom
    Y" filter. Only the assumed field is touched (assume the OTHER stays the
    account's). A level assumption also sets `limit_break` to `"max"`, so a real
    card's low limit break does not silently clamp the hypothetical
    (`resolve_card_state` resolves `"max"` to the rarity's top limit break).

    `keep_higher_level` / `keep_higher_bloom` ("do not delevel / debloom", as in
    the community tool holodori.best): a card already ABOVE a numeric assumption keeps its
    own value (and its own limit break) instead of being lowered to it.

    `assume_limit_break` ("Assumed SP Training", 0..4): every card's
    limit break -- its level CAP by rarity (`card_caps.json`) -- set to that, after
    the level assumption (so "level max" means the cap of that limit break); with
    `keep_higher_level` a card already above it keeps its own."""

    def _higher(own: object, assumed: object) -> bool:
        return (
            isinstance(own, int) and not isinstance(own, bool) and isinstance(assumed, int) and own > assumed
        )

    def _apply(entry: object) -> dict:
        out = dict(entry) if isinstance(entry, dict) else {}
        if assume_level is not None and not (keep_higher_level and _higher(out.get("level"), assume_level)):
            out["level"] = assume_level
            out["limit_break"] = "max"
        if assume_bloom is not None and not (keep_higher_bloom and _higher(out.get("bloom"), assume_bloom)):
            out["bloom"] = assume_bloom
        if assume_limit_break is not None and not (
            keep_higher_level
            and _higher(entry.get("limit_break") if isinstance(entry, dict) else None, assume_limit_break)
        ):
            out["limit_break"] = assume_limit_break
        return out

    document = dict(document)
    entries = document.get("cards")
    if isinstance(entries, dict):
        document["cards"] = {cid: _apply(entry) for cid, entry in entries.items()}
    # card_default too, so an owned card with no explicit entry also takes it.
    document["card_default"] = _apply(document.get("card_default"))
    return document


def apply_collection(
    cards: tuple[Card, ...],
    path: str | None,
    *,
    owned_ids: Sequence[str] | None = None,
    maxed: bool = False,
    assume_level: object = None,
    assume_bloom: object = None,
    keep_higher_level: bool = False,
    keep_higher_bloom: bool = False,
    assume_limit_break: object = None,
) -> tuple[tuple[Card, ...], CollectionBasis]:
    """Restate every card's stats from the collection at `path`.

    `path is None` means no collection. An absent file is NOT an error and
    never changes the exit code — the same first-class-state contract
    `load_board` has. So is an absent `card_level_curve.json`: without the
    per-level multiplier there is nothing to restate, every card keeps
    `data/cards.json`'s value, and the run says so.

    A card the collection cannot restate keeps `data/cards.json`'s stats, which
    are the authoritative ones. Nothing here writes any file."""
    # The maxed basis: answer "what would this team be on a maxed account"
    # instead of "what is it on mine". The cards need no work — `data/cards.json`
    # IS the maxed state — so this is the collection deliberately not applied,
    # plus the account-level ceilings.
    if maxed:
        curve_raw = _load_json_if_present(paths.REPO / COLLECTION_CURVE_PATH)
        basis = stat_basis_from_mapping(curve_raw) if curve_raw else None
        return cards, CollectionBasis(
            bonuses=maxed_account_bonuses(basis, master_poster_count()),
            path=None,
            applied=0,
            unchanged=len(cards),
            not_scorable=(),
            warnings=(),
            maxed=True,
        )

    if path is None:
        return cards, CollectionBasis(None, None, 0, len(cards), (), ())

    document = _load_json_if_present(Path(path))
    # The level/bloom "what-if" filter: force every card to an assumed level
    # and/or bloom before the stats are restated. Applied only on the collection
    # (account) path — the maxed basis above already IS max, so it never gets here.
    if document is not None and (
        assume_level is not None or assume_bloom is not None or assume_limit_break is not None
    ):
        document = _override_collection_levels(
            document, assume_level, assume_bloom, keep_higher_level, keep_higher_bloom, assume_limit_break
        )
    curve_raw = _load_json_if_present(paths.REPO / COLLECTION_CURVE_PATH)
    caps_raw = _load_json_if_present(paths.REPO / COLLECTION_CAPS_PATH)
    ladder_raw = _load_json_if_present(paths.REPO / COLLECTION_LADDER_PATH)
    basis = stat_basis_from_mapping(curve_raw) if curve_raw else None
    caps = caps_by_rarity_from_mapping(caps_raw) if caps_raw else None
    ladder = skill_ladder_from_mapping(ladder_raw)

    stats, report = collection_stats([card.id for card in cards], document, caps, basis)
    # The `player` block rides along on the same document. It reaches
    # `ScoreBreakdown` only -- both terms are ranking-safe to drop, so they are
    # reported and never folded into a score.
    bonuses = account_bonuses_from_mapping(document)

    # Two restatements, and they are independent. STATS come from the level and
    # bloom through the curve; SKILL LEVELS come from the bloom alone, through
    # the exported ladder. A card can get one and not the other -- a card with
    # no curve entry still has a bloom, and a card at full bloom still has
    # stats to restate -- so neither is gated on the other.
    restated: list[Card] = []
    skills_restated = 0
    skills_unavailable: list[str] = []
    for card in cards:
        if card.id in stats:
            card = dataclasses.replace(
                card,
                stats=Stats(
                    performance=stats[card.id][0],
                    technique=stats[card.id][1],
                    sense=stats[card.id][2],
                ),
            )
        state = report.states.get(card.id)
        down_levelled, notes = restate_card_skills(
            card, state.bloom if state else None, ladder.get(card.id, {})
        )
        if down_levelled is not card:
            skills_restated += 1
        if any("no level-" in note for note in notes):
            skills_unavailable.append(card.id)
        restated.append(down_levelled)

    # `power_up_permyriad` is hand-typed and easy to get wrong (e.g. `951` for a
    # true `39`). It is derivable from the account's own cards, so derive it and
    # let the run say when the file disagrees. Nothing here writes
    # `state/collection.json`: the correction is the user's to make.
    derived_power_up = derived_member_power_up_permyriad(basis, report.states, owned_ids or ())
    # `"auto"` in the document means "compute it", and this is where it is
    # computed. Only an EXACT derivation may fill it: with an owned card whose
    # level is not recorded the answer is a bound, and scoring the lower end of
    # a bound as though it were the figure is the kind of plausible number this
    # project refuses to invent. A partial derivation leaves the term unset and
    # `lines()` says why.
    if (
        getattr(bonuses, "power_up_permyriad_is_auto", False)
        and derived_power_up is not None
        and derived_power_up.exact
    ):
        bonuses = dataclasses.replace(bonuses, power_up_permyriad=derived_power_up.permyriad)

    return tuple(restated), CollectionBasis(
        bonuses=bonuses,
        path=path,
        applied=len(report.applied),
        unchanged=len(report.unchanged),
        not_scorable=report.not_scorable,
        warnings=report.warnings,
        skills_restated=skills_restated,
        skills_unavailable=tuple(skills_unavailable),
        derived_power_up=derived_power_up,
    )


def load_board_basis(path: str | None) -> BoardBasis:
    """The board file at `path`, or the explicit "no board" state.

    `path is None` means no board. An absent file is NOT an error and
    never changes the exit code: `load_board` returns `None`, `cfg.board`
    stays `None`, `scoring._board_bonus` stays the identity, and the run
    scores without a board. A file that
    exists but does not parse IS an error — a board that is silently zero
    looks exactly like a board that is correctly zero, which is the whole
    failure mode `board.BoardError` exists to prevent."""
    if path is None:
        return BoardBasis(path=None, model=None, characters=0, nodes=0)
    model = load_board(path)
    if model is None:
        return BoardBasis(path=path, model=None, characters=0, nodes=0)
    return BoardBasis(
        path=path,
        model=model,
        characters=len(model.characters),
        nodes=sum(len(board.nodes) for board in model.characters.values()),
    )


def load_inputs(
    cards_path: Path, owned_path: Path
) -> tuple[tuple[Card, ...], tuple[Card, ...], ScoringConfig, CardProvenance | None]:
    """The single shared load-and-guard function: every
    `cards.json` / `owned.json` / `config.json` load goes through here,
    never through a second load path. Never calls `json.load` itself —
    every load delegates to `load_cards`, `card_provenance_of`,
    `load_owned` and `load_scoring_config`.

    Returns the loaded card document's `CardProvenance` as a fourth
    element — `None` for the maxed default (mode A), populated for a
    mode-B real-stat document.

    Raises `_LoadFailure` with the exit code the caller should return:
    `EXIT_INPUT_DATA` for a `load_cards`/`card_provenance_of`/`load_owned` failure,
    `EXIT_CONFIG` for a `load_scoring_config` failure — decided by
    the `current_step` tracking local, which also picks the printed
    message's `current_input`."""
    current_input: str = str(cards_path)
    current_step = _STEP_CARDS
    try:
        cards = load_cards(cards_path)
        # Second read of the same file, on purpose: see `load_cards`'s own
        # docstring — an AST test identifies the shared load path by the
        # name `load_cards`, so the stat basis is a separate call rather than
        # a wider signature. Inside this same guard, so a malformed
        # provenance block exits as an input-data error like any other bad
        # card file.
        provenance = card_provenance_of(cards_path)
        current_input = str(owned_path)
        current_step = _STEP_OWNED
        owned = load_owned(owned_path, cards)
        current_input = str(DEFAULT_CONFIG_PATH)
        current_step = _STEP_CONFIG
        cfg = load_scoring_config()
    except (SchemaError, TeamError) as exc:
        print(str(exc), file=sys.stderr)
        raise _LoadFailure(EXIT_CONFIG if current_step == _STEP_CONFIG else EXIT_INPUT_DATA) from exc
    except (OSError, json.JSONDecodeError) as exc:
        print(f"{current_input}: {exc}", file=sys.stderr)
        raise _LoadFailure(EXIT_CONFIG if current_step == _STEP_CONFIG else EXIT_INPUT_DATA) from exc

    return cards, owned, cfg, provenance


def default_paths():
    """The default input paths, relative to the repo root: `cards`, `owned`, `collection`,
    `board`, `charts`, `skilltree`, `master_nodes`."""
    from types import SimpleNamespace  # noqa: PLC0415

    return SimpleNamespace(
        cards="data/cards.json",
        owned="data/owned.json",
        collection=DEFAULT_COLLECTION_PATH,
        board=DEFAULT_BOARD_PATH,
        charts=DEFAULT_CHARTS_PATH,
        skilltree=f"{paths.DUMPS_REL}/skilltree.json",
        master_nodes=f"{paths.DUMPS_REL}/master_SkillTreeNode.json",
    )
