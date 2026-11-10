"""The collection editor: `GET /api/collection` (the document and every card's bounds),
`POST /api/collection` (save) and `POST /api/card-states` (cards as a search scored them).

The save is the ONE write anything under `tools/web/` makes, to `config.COLLECTION_PATH`
only; `test_no_writes.py` pins every filesystem mutation in the package to that name.
The bounds come from `domain/collection`, never recomputed here: a UI that clamps to its
own idea of a cap would let the player enter a level the solver then silently changes.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hbuilder.domain.collection import (
    FULLY_SCORABLE_BLOOM,
    MEMBER_POWER_UP_CAP_PERMYRIAD,
    MEMORY_BONUS_MAX_POSTERS,
    PLAYER_LEVEL_MAX,
    PLAYER_LEVEL_MIN,
    SKILL_BLOOM_STEP,
    caps_by_rarity_from_mapping,
    max_member_power_up_permyriad,
    memory_bonus_permil,
    resolve_card_state,
    stat_basis_from_mapping,
)
from hbuilder.inputs import _LoadFailure, apply_collection, load_inputs
from hbweb import board, config, static

# `data/cards.json` carries generation names MIXED: `Advent` and `ReGLOSS` in
# English beside `0期生` and `ゲーマーズ` in Japanese. `gen_groups.json`, extracted
# from `CharacterGrouping`, is uniformly English -- and it is the GAME'S own
# English, not a translation. The two sets are the same 15 groups, so
# this is a bijection and not a guess:
_GEN_ALIAS = {
    "0期生": "Gen 0",
    "1期生": "Gen 1",
    "2期生": "Gen 2",
    "3期生": "Gen 3",
    "4期生": "Gen 4",
    "5期生": "Gen 5",
    "ゲーマーズ": "GAMERS",
    "ID1": "ID Gen 1",
    "ID2": "ID Gen 2",
    "ID3": "ID Gen 3",
    # Advent · Myth · Promise · ReGLOSS · holoX already match by name.
}


@functools.lru_cache(maxsize=1)
def gen_labels() -> dict[str, str]:
    """`{name as `cards.json` spells it: the game's English label}`.

    DISPLAY ONLY. `card.generations` keeps its own spelling everywhere else,
    because a gate reads `generation:Myth >= 2` against that value -- relabelling
    the data instead of the label would silently change what a gate matches.
    """
    return dict(_GEN_ALIAS)


@functools.lru_cache(maxsize=1)
def gen_palette() -> dict[str, dict[str, str]]:
    """`{generation name: {"c": <field 100>, "c2": <field 101>}}`.

    Keyed by the group's display NAME because that is what the card faces ship in
    `generations` -- the client never sees a `grp-` id. Absent file, empty map, and
    the ribbon falls back to the card's type tint, which is what the explorer does.
    """
    try:
        raw = json.loads(config.GEN_GROUPS_PATH.read_text(encoding="utf-8"))
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


_COSTUME_TEXTS: dict = {"mtime": None, "texts": {}}


def costume_texts() -> dict[str, str]:
    """{card id: the game's own English outfit-skill text} (LangGeneratedLiveLeaderSkill,
    carried by `cards_enriched.json` `descriptions.COSTUME`), re-read when the file moves.
    The page prints this rather than rebuilding a sentence from the parsed skill, which
    can drop a second clause (Choco's Coquette Temptation: "... Score Support Effect of 25%")."""
    path = config.CARDS_ENRICHED_PATH
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _COSTUME_TEXTS["mtime"] != mtime:
        doc = config.read_json(path) or {}
        rows = doc if isinstance(doc, list) else (doc.get("cards") or list(doc.values()))
        texts = {}
        for row in rows:
            if isinstance(row, dict) and row.get("id"):
                text = ((row.get("descriptions") or {}).get("COSTUME") or "").strip()
                if text:
                    texts[row["id"]] = text
        _COSTUME_TEXTS.update(mtime=mtime, texts=texts)
    return _COSTUME_TEXTS["texts"]


def _rarity_of(basis, card_id: str) -> str | None:
    """The rarity digit of a card from the level curve's grouping (`rarity_<n>-...`)."""
    if basis is None or card_id not in basis.cards:
        return None
    group = basis.cards[card_id][0] or ""
    return group.partition("rarity_")[2].split("-", 1)[0] or None


def _editor_card(
    card: Mapping, state, entry_caps, *, owned_ids: set, trial_ids: set, ladder_cards: Mapping, art_dir: Path
) -> dict[str, Any]:
    """One card of the editor: its face, the account's state and its bounds."""
    card_id = card["id"]
    return {
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
        "has_art": static.asset_known(art_dir, art_dir / f"{card_id}.webp"),
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
        "costume_text": costume_texts().get(card_id),
        # The catalogue's Performance / Technique / Sense, as
        # data/cards.json carries them: the MAXED card, not this
        # card at its level. Read-only, for the Collection page's
        # card detail, which labels them as maxed.
        "stats": card.get("stats"),
        # The SAME per-level rungs `restate_card_skills` scores from, so
        # the editor shows each skill at the level THIS bloom bought
        # (level 1 below the step, level 2 at/above) — the game's value.
        "skill_ladder": ladder_cards.get(card_id) or {},
        "level_caps_by_limit_break": (list(entry_caps.level_by_limit_break) if entry_caps else []),
    }


def serve_collection(h) -> None:
    """`GET /api/collection`: the document on disk plus the bounds the editor needs to
    build its controls: each card's level cap by limit break, its max bloom, and its
    currently-resolved level/bloom (`resolve_card_state` clamps AND reports, which is the
    behaviour to surface rather than to duplicate)."""
    document = config.read_json(config.COLLECTION_PATH) or {
        "schema_version": "collection-1",
        "card_default": "max",
        "cards": {},
    }
    curve = config.read_json(config.CURVE_PATH)
    caps_raw = config.read_json(config.CAPS_PATH)
    basis = stat_basis_from_mapping(curve) if curve else None
    caps = caps_by_rarity_from_mapping(caps_raw) if caps_raw else None

    cards_doc = config.read_json(config.CARDS_PATH) or {}
    ladder_cards = (config.read_json(config.LADDER_PATH) or {}).get("cards") or {}
    entries = document.get("cards") or {}
    default = document.get("card_default", "max")
    # Which cards the account actually has, so the editor can filter on it.
    # READ ONLY -- `data/owned.json` is authoritative and hand-editable. Absent file =>
    # empty set, and the filter says "0 owned" rather than pretending every
    # card is owned, which would be a claim about the account.
    owned_ids = set((config.read_json(config.OWNED_PATH) or {}).get("owned") or [])
    # cards the player is trying as if owned: in the search pool until the next refresh
    trial_ids = set(document.get("trial") or [])

    cards: list[dict[str, Any]] = []
    for card in cards_doc.get("cards", []):
        card_id = card["id"]
        rarity = _rarity_of(basis, card_id)
        entry_caps = (caps or {}).get(rarity) if rarity else None
        state = resolve_card_state(card_id, entries.get(card_id), default, entry_caps)
        cards.append(
            _editor_card(
                card,
                state,
                entry_caps,
                owned_ids=owned_ids,
                trial_ids=trial_ids,
                ladder_cards=ladder_cards,
                art_dir=h.art_dir,
            )
        )

    h.reply_json(
        200,
        {
            "path": str(config.COLLECTION_PATH),
            "document": document,
            "cards": cards,
            "gen_colors": gen_palette(),
            "gen_labels": gen_labels(),
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
            "board_level_gates": board.board_level_gates(),
            "power_up_reachable_permyriad": max_member_power_up_permyriad(basis),
            # The roster that figure was summed over, sent BESIDE it because
            # the page has to name it in a sentence and the alternative is
            # the page inventing the number, which goes stale the moment a
            # card ships.
            "power_up_reachable_roster_size": (len(basis.cards) if basis is not None else None),
        },
    )


def serve_card_states(h) -> None:
    """`POST /api/card-states {ids, assumptions?}` -> `{states: {id: {level, bloom,
    limit_break, owned, trial}}}`: each card as a search SCORED it -- the collection
    through the same `_override_collection_levels` the search's `apply_collection`
    applies for the request's levers (`ondemand.parse_assumptions`), resolved by
    `resolve_card_state`, so with "max level, bloom 5" the result's cards show the maxed
    level and bloom rather than the account's own, and a card not owned still shows one.
    Read-only."""
    from hbuilder.inputs import _override_collection_levels  # noqa: PLC0415

    body = h.body_or_empty()
    ids = [str(i) for i in (body.get("ids") or []) if i][:200]
    document = config.read_json(config.COLLECTION_PATH) or {"cards": {}}
    owned_ids = set((config.read_json(config.OWNED_PATH) or {}).get("owned") or [])
    trial_ids = set(document.get("trial") or [])
    if isinstance(body.get("assumptions"), dict):
        import ondemand  # noqa: PLC0415

        try:
            a = ondemand.parse_assumptions(body["assumptions"])
        except (TypeError, ValueError) as exc:
            h.reply_json(400, {"error": f"assumptions: {exc}"})
            return
        document = _override_collection_levels(
            document,
            None if a.level == "account" else a.level,
            None if a.bloom == "account" else a.bloom,
            keep_higher_level=a.keep_higher,
            keep_higher_bloom=a.keep_higher,
            assume_limit_break=None if a.sp_training == "account" else a.sp_training,
        )
    curve, caps_raw = config.read_json(config.CURVE_PATH), config.read_json(config.CAPS_PATH)
    basis = stat_basis_from_mapping(curve) if curve else None
    caps = caps_by_rarity_from_mapping(caps_raw) if caps_raw else None
    entries, default = document.get("cards") or {}, document.get("card_default", "max")
    states = {}
    for card_id in ids:
        rarity = _rarity_of(basis, card_id)
        st = resolve_card_state(
            card_id, entries.get(card_id), default, (caps or {}).get(rarity) if rarity else None
        )
        states[card_id] = {
            "level": st.level,
            "bloom": st.bloom,
            "limit_break": st.limit_break,
            "owned": card_id in owned_ids,
            "trial": card_id in trial_ids and card_id not in owned_ids,
        }
    h.reply_json(200, {"states": states})


def save_collection(h) -> None:
    """`POST /api/collection`: write the document to `COLLECTION_PATH` and nothing else, ever.

    The response carries what the solver made of it (read back through the CLI's own
    loader), so the page shows the tool's own verdict rather than its own optimism."""
    try:
        length = int(h.headers.get("Content-Length") or 0)
        document = json.loads(h.rfile.read(length).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        h.reply_json(400, {"error": f"malformed JSON: {exc}"})
        return
    if not isinstance(document, dict) or not isinstance(document.get("cards", {}), dict):
        h.reply_json(400, {"error": "a collection document must be an object with a 'cards' object"})
        return

    body = json.dumps(document, indent=1, sort_keys=True) + "\n"
    config.COLLECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.COLLECTION_PATH.write_text(body, encoding="utf-8")

    # Read it back through the CLI's own loader and report what the SOLVER
    # made of it -- not what this handler believes it wrote.
    try:
        cards, _owned, _cfg, _prov = load_inputs(config.CARDS_PATH, config.OWNED_PATH)
    except _LoadFailure:
        h.reply_json(200, {"saved": True, "path": str(config.COLLECTION_PATH)})
        return
    _restated, basis = apply_collection(tuple(cards), str(config.COLLECTION_PATH))
    h.reply_json(
        200,
        {
            "saved": True,
            "path": str(config.COLLECTION_PATH),
            "cards_restated": basis.applied,
            "cards_unchanged": basis.unchanged,
            "not_fully_scorable": list(basis.not_scorable),
            "warnings": list(basis.warnings[:20]),
        },
    )
