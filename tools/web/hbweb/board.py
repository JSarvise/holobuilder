"""The board editor's document: `GET /api/board`.

The tree is static per install (152 nodes, four model geometries) and the selection is
per character, so both travel in one response rather than making the editor stitch
three endpoints together on every character switch.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from hbuilder.domain.collection import (
    CollectionError,
    account_bonuses_from_mapping,
    caps_by_rarity_from_mapping,
    connect_bonus_level,
    resolve_card_state,
)
from hbuilder.domain.skilltree import ROOT_NODE, TOTAL_BOARD_POINTS, player_level_requirement

from hbweb import config

# `…_level_up` is a hollow FRAME with "Lv" on its base, not a finished icon —
# the granted skill's own glyph belongs inside it, and left empty the node
# reads as a blank box.
#
# From the asset: the frame is 128x128 and its enclosed hole spans
# 0.117..0.883 on both axes (76.6 % of the side). The explorer fits the inner
# glyph to 86 % of that hole, i.e. 0.659 of the frame — the fraction
# `board.js` uses.
TREE_FRAME_ICONS = frozenset({"img_skill_icon_tree_livedeckleaderactiveskilllevelup"})


def node_costs() -> dict[str, int]:
    """Point cost per node group, master `SkillTreeNode` field 9.

    The cost does not vary across a group's rows (0 of 152
    disagree), so the first row decides and the 15 multi-row groups need no
    special case here. They still do for EFFECTS, which is `build_board`'s
    problem, not this endpoint's."""
    rows = config.read_json(config.NODE_MASTER_PATH) or []
    costs: dict[str, int] = {}
    for row in rows:
        data = row.get("data") or {}
        group = (data.get("1") or [None])[0]
        if group is not None and group not in costs:
            costs[group] = int((data.get("9") or [0])[0])
    return costs


def socket_catalogue(tree: dict, document: Mapping) -> dict[str, Any]:
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

    caps_raw = config.read_json(config.CAPS_PATH)
    caps = caps_by_rarity_from_mapping(caps_raw) if caps_raw else {}
    entries = document.get("cards") or {}
    default = document.get("card_default", "max")

    by_id = {c["id"]: c for c in (config.read_json(config.CARDS_PATH) or {}).get("cards", [])}
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


def board_level_gates() -> list[dict[str, int]]:
    """`[{level, nodes}, ...]` — how many board nodes each account level opens.

    DERIVED from `skilltree.json`, never hardcoded, so it tracks the master
    rather than a number someone typed into the page. `level: 0` is the
    ungated remainder, which is a real bucket and not a rounding error: 25 of
    the 152 groups carry no `req` at all (whether those 25 are genuinely
    ungated is not known, so the label the page uses says "no level gate",
    not "available from level 1").

    Empty when the tree is not on disk — the level control still works, it
    just cannot say what a level buys."""
    tree = config.read_json(config.SKILLTREE_PATH)
    if not tree:
        return []
    counts: dict[int, int] = {}
    for raw in (tree.get("nodes") or {}).values():
        need = player_level_requirement((raw or {}).get("req")) or 0
        counts[need] = counts.get(need, 0) + 1
    return [{"level": lvl, "nodes": counts[lvl]} for lvl in sorted(counts)]


def _characters(tree: Mapping, document: Mapping, costs: Mapping[str, int],
                unspent: Mapping[str, Any]) -> dict[str, Any]:
    """Each member's selection, its `source` and its budget.

    `source` is the honest part: `"edited"` means the player typed it and it lives in
    the collection; `"captured"` means it came from the game via `board_state.json`;
    `"empty"` is a character no capture has (28 of 54), sent as an empty board rather
    than omitted, since that is what the editor is for.

    The budget is `cost_of(unlocked nodes) + unspent`, with the unspent balance read
    off `User/Get`: the total EARNED bounds an edit and does not move when the player
    respends, so it is computed from the CAPTURED node set even while editing. `None`
    for a character with no capture, whose balance is unknown -- never 0, which would
    claim they cannot afford anything."""
    edited = document.get("characters") or {}
    captured = tree.get("unlocked") or {}
    placed = tree.get("placed") or {}
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
            characters[chr_id] = {
                "nodes": [], "slots": {}, "source": "empty",
                "budget": budgets.get(chr_id),
            }
    return characters


def _node_glyphs(tree: Mapping, costs: Mapping[str, int]) -> tuple[dict, set[str], int]:
    """`(nodes, missing_icons, icons_available)`: each node's fields and the glyph it
    draws, resolved against what is ON DISK.

    ⚠ Resolution order matters, and getting it wrong is VISIBLE:

     1. A "level up" node's own icon is a hollow FRAME; the granted
        skill's glyph goes inside it. Left bare it renders as an empty
        box (e.g. on R-031).
     2. A node that GRANTS a skill draws THAT skill's icon. For the
        "addition" family the game ships no tree icon at all, so
        `icon_sk` is not a fallback, it is the only correct answer.
        R-024 and R-027 are those nodes.
     3. The three per-character grouping nodes (G-008/G-011/G-021) carry
        `e: null` and keep their icon in the `chr` block.

    The four sockets have no glyph and are drawn as sockets. A glyph that cannot be
    resolved is reported by the name WANTED, so the gap is visible."""
    available = (
        {path.stem for path in config.ICONS_DIR.glob("*.png")}
        if config.ICONS_DIR.is_dir()
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
    return nodes, missing_icons, len(available)


def serve_board(h) -> None:
    """`GET /api/board`: everything the board editor needs, in one request. Absent files
    and a bad hand-edited `player` block are first-class `available: false` states with
    the reason, never a dropped connection."""
    tree = config.read_json(config.SKILLTREE_PATH)
    if not tree:
        h.reply_json(
            200,
            {
                "available": False,
                "reason": (
                    f"{config.SKILLTREE_PATH.name} is not on disk (it lives under "
                    f"gitignored dumps/). Run tools/refresh/build_skilltree.py "
                    f"against a capture to produce it."
                ),
            },
        )
        return

    costs = node_costs()
    document = config.read_json(config.COLLECTION_PATH) or {}
    try:
        player_level = account_bonuses_from_mapping(document).level
    except CollectionError as exc:
        h.reply_json(
            200,
            {
                "available": False,
                "reason": f"{config.COLLECTION_PATH.name}: {exc}",
            },
        )
        return

    unspent = config.read_json(config.TREE_POINTS_PATH) or {}
    characters = _characters(tree, document, costs, unspent)
    nodes, missing_icons, icons_available = _node_glyphs(tree, costs)
    h.reply_json(
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
            "sockets": socket_catalogue(tree, document),
            # Through the domain, not off the raw dict: a level the solver
            # would refuse must not silently gate the editor differently
            # from the way a run would. `None` stays `None` -- board.js
            # reads that as "not told us" and locks nothing.
            "player_level": player_level,
            # Named, not swallowed: the editor says which glyph is absent
            # and what to run, rather than quietly drawing a blank node.
            "unspent_total": sum(int(v) for v in unspent.values()),
            "missing_icons": sorted(missing_icons),
            "icons_available": icons_available,
        },
    )
