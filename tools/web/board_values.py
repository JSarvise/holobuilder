"""Node values and Connect boosts for the Holomem Board page (`/api/board-doc`).

`board_export.build` serves each node's effect TEXT with the game's `[value]` /
`[value/10]%` placeholders but not the number, so the page could only drop it.
This module fills the gap from the same decoded master the board is drawn from
(`dumps/skilltree.json`), and adds what the game shows for a Connect: which
nodes a socketed card covers and each covered node's value after the boost.

Sources and confidence:

* Node value — `SkillTreeNode` effect `v` (master data); the per-character
  override when the node carries one (the same pick `board_export._effect_for`
  makes). The yellow per-song family ships `v = 0` and an empty text; its permil
  is the number inside the effect id (`...-5-skill_tree_effect_passive_trigger-…`,
  the same parse `hbuilder.domain.board._EFFECT_RE` makes).
* Connect shape — `SkillTreeConnectEffectExtent` cells, offsets from the socket's
  own cell.
* Connect level — 2 at bloom 5 on a 4*/5* card, else 1
  (`hbuilder.domain.collection.connect_bonus_level`, from master
  `CardPotential`), read from the account's own bloom in `state/collection.json`.
  A card with no recorded bloom is level 1 and labelled `assumed`; the maxed basis
  is level 2 and labelled `maxed`.
* Boosted value — `Ceiling((1 + Σpermil/1000) × base)`, the sources SUMMED, from
  `SkillTreeUtility::GetEffectValueWithConnectEffectsInternal` in the client.
  Computed in exact integer arithmetic so a product
  that is whole in the game (40 × 1.55 = 62) can never ceil up on a float
  residue. Display only: the solver's own scoring path is `hbuilder.domain.board`.

Read-only: nothing here writes a file.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from hbuilder.domain.collection import connect_bonus_level

_ID_VALUE_RE = re.compile(r"^skill_tree_effect-.+?-(\d+)-skill_tree_effect_(?:target|passive_trigger)-")
_HIGHLIGHT_RE = re.compile(r"\[/?highlight\]")
SOCKET_PREFIX = "S-"


def node_effect(meta: Mapping, chr_id: str) -> dict:
    """The effect node `meta` grants `chr_id` (per-character override first)."""
    per = (meta.get("chr") or {}).get(chr_id)
    return dict(per or meta.get("e") or {})


def effect_value(effect: Mapping) -> int | None:
    """The raw master value of an effect: `v`, or the permil inside the effect id
    for the yellow per-song family (whose `v` is 0). None when there is none."""
    value = effect.get("v")
    if value:
        return int(value)
    match = _ID_VALUE_RE.match(str(effect.get("t") or ""))
    if match:
        return int(match.group(1))
    return int(value) if value is not None else None


def value_unit(effect: Mapping) -> str | None:
    """How the game prints the value: "flat" (`[value]`), "permil"
    (`[value/10]%`, and the yellow per-song bonus), or None (no number shown)."""
    text = str(effect.get("d") or "")
    if "[value/10]" in text or "live_score_bonus" in str(effect.get("t") or ""):
        return "permil"
    if "[value]" in text:
        return "flat"
    return None


def boosted(value: int, permil: int) -> int:
    """`Ceiling((1 + permil/1000) × value)` in exact integers (see module doc)."""
    if not permil:
        return value
    return -(-value * (1000 + permil) // 1000)


def format_value(value: int | None, unit: str | None) -> str:
    """The number as the game prints it: `50`, `4%`, `6.2%`."""
    if value is None or unit is None:
        return ""
    if unit == "permil":
        tenths = value / 10
        return (f"{tenths:.1f}".rstrip("0").rstrip(".")) + "%"
    return str(value)


def connect_cards(skilltree: Mapping, collection: Mapping | None, *, maxed: bool) -> dict:
    """Every card that has a Connect effect -> its shape, level and boost."""
    extents = skilltree.get("extents") or {}
    effects = skilltree.get("connect") or {}
    owned = (collection or {}).get("cards") or {}
    out: dict[str, dict] = {}
    for card_id, effect_id in (skilltree.get("card_connect") or {}).items():
        effect = effects.get(effect_id) or {}
        levels = effect.get("levels") or []
        cells = extents.get(effect.get("area"))
        if not levels or cells is None:
            continue
        parts = card_id.split("-")
        rarity = parts[2] if len(parts) > 2 else None
        if maxed:
            level, basis = len(levels), "maxed"
        else:
            entry = owned.get(card_id) or {}
            derived = connect_bonus_level(entry.get("bloom"), rarity)
            level, basis = (derived, "account") if derived is not None else (1, "assumed")
        level = max(1, min(level, len(levels)))
        row = levels[level - 1]
        out[card_id] = {
            "area": effect.get("area"),
            "rare": effect.get("rare"),
            "level": level,
            "level_basis": basis,
            "permil": int(row.get("v") or 0),
            "permil_levels": [int(x.get("v") or 0) for x in levels],
            "text": _HIGHLIGHT_RE.sub("", str(row.get("d") or "")),
            "cells": [list(c) for c in cells],
        }
    return out


def covered_nodes(positions: Mapping[str, list], socket_pos: list, cells: list) -> list[str]:
    """The board nodes inside a Connect shape anchored at `socket_pos`."""
    sx, sy = socket_pos
    hit = {(sx + dx, sy + dy) for dx, dy in cells}
    return sorted(n for n, p in positions.items()
                  if not n.startswith(SOCKET_PREFIX) and (p[0], p[1]) in hit)


def annotate(doc: dict, skilltree: Mapping, collection: Mapping | None, *, maxed: bool = False) -> dict:
    """Add node values and Connect boosts to a board-export/v0 doc, in place.

    Per node: `value`, `value_unit`, `value_text`, and for the sockets as placed
    in the doc: `connect_permil`, `boosted_value`, `boosted_text`, `boosted_by`.
    Per socket: `connect` (the placed card's shape, level, permil and the node
    ids it covers). Top level: `connect_cards` (every Connect card, so a page can
    show any card's range, e.g. a plan's recommended one)."""
    nodes_meta = skilltree.get("nodes") or {}
    models = skilltree.get("models") or {}
    catalogue = connect_cards(skilltree, collection, maxed=maxed)
    doc["connect_cards"] = catalogue
    for ch in doc.get("characters") or []:
        cid = ch.get("chr")
        positions = models.get(ch.get("model")) or {
            n["id"]: n["pos"] for n in ch.get("nodes") or []}
        permil_by_node: dict[str, int] = {}
        source_by_node: dict[str, list[str]] = {}
        for sock in ch.get("sockets") or []:
            card = sock.get("placed_card")
            info = catalogue.get(card) if card else None
            if not info:
                sock["connect"] = None
                continue
            covers = covered_nodes(positions, sock.get("pos") or positions.get(sock["socket"]), info["cells"])
            sock["connect"] = {"card": card, "level": info["level"], "level_basis": info["level_basis"],
                               "permil": info["permil"], "area": info["area"], "text": info["text"],
                               "covers": covers}
            for nid in covers:
                permil_by_node[nid] = permil_by_node.get(nid, 0) + info["permil"]
                source_by_node.setdefault(nid, []).append(sock["socket"])
        for node in ch.get("nodes") or []:
            effect = node_effect(nodes_meta.get(node["id"]) or {}, cid)
            value = effect_value(effect)
            unit = value_unit(effect)
            node["value"] = value if unit else None
            node["value_unit"] = unit
            node["value_text"] = format_value(value, unit)
            permil = permil_by_node.get(node["id"], 0)
            node["connect_permil"] = permil
            node["boosted_by"] = source_by_node.get(node["id"], [])
            if unit and value is not None and permil:
                up = boosted(value, permil)
                node["boosted_value"] = up
                node["boosted_text"] = format_value(up, unit)
            else:
                node["boosted_value"] = node["value"]
                node["boosted_text"] = node["value_text"]
    return doc
