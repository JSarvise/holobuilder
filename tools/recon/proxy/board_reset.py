"""What a board plan's resets cost: Hologold per node taken off a board.

The game charges `Setting.skillTreeResetConsumptionGoldQuantity` (1000) Hologold per node in
the reset list and refunds the node's points and cubes/hexas: the client's reset dialog
multiplies the reset-list count by the setting. A partial reset also removes every node beyond the
one chosen, so the count here (nodes owned now and absent from the plan) is a LOWER bound
on what the game will list. S-001, the free root, is never reset.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = "S-001"
SETTING_PATH = Path(__file__).resolve().parent / "dumps" / "holodori" / "main_Setting.json"
DEFAULT_GOLD_PER_NODE = 1000


def gold_per_node(path: Path = SETTING_PATH) -> int:
    """The game's own price, read from master data; the client's default 1000 when it is absent."""
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        data = rows[0].get("data", rows[0]) if isinstance(rows, list) else rows
        return int(data["skillTreeResetConsumptionGoldQuantity"])
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return DEFAULT_GOLD_PER_NODE


def reset_cost(now: dict, plan: dict, *, gold_per_node: int | None = None) -> dict:
    """`{count, gold, per_character}` for moving from `now` to `plan`.

    Both map a character id to its node ids. A character absent from `plan` keeps its
    board, so it resets nothing."""
    price = DEFAULT_GOLD_PER_NODE if gold_per_node is None else gold_per_node
    per_character: dict[str, list[str]] = {}
    for cid, planned in plan.items():
        keep = set(planned)
        dropped = sorted(n for n in set(now.get(cid) or ()) if n != ROOT and n not in keep)
        if dropped:
            per_character[cid] = dropped
    count = sum(len(v) for v in per_character.values())
    return {"count": count, "gold": count * price, "gold_per_node": price,
            "per_character": per_character}
