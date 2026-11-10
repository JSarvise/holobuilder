"""The Inventory page: `GET /api/resources`, everything the account holds, from the
captured on-disk state. Absent captures are a first-class `{available: False}` with the
refresh hint; nothing here writes or logs a credential.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hbweb import basis, config

if TYPE_CHECKING:
    from hbweb.router import Handler


def account_currencies() -> list:
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
    An absent capture yields zeros, a first-class 'refresh' state."""
    inv = config.read_json(config.INVENTORY_PATH) or {}
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
        rows.append(
            {
                "name": f"{label} Beads",
                "held": q(iid),
                "note": "Member Training",
                "item_id": iid,
                "group": "beads",
            }
        )
    for key, label in attrs:  # grade -2 = Crystals
        iid = f"item-card_limit_break-{key}-2"
        rows.append(
            {
                "name": f"{label} Crystals",
                "held": q(iid),
                "note": "Member Training",
                "item_id": iid,
                "group": "crystals",
            }
        )

    # Selectable bundles. box asset is `...-box-<01 beads|02 crystals>-<01 S|02 M|03 L>`.
    sizes = (("10", "S", 10, "01"), ("100", "M", 100, "02"), ("300", "L", 300, "03"))
    bundles = (
        ("attribute_1", "Beads Bundle", "beads", "01"),
        ("attribute_2", "A Jar of Crystals", "crystals", "02"),
    )
    for attr_key, base_name, grants, box_attr in bundles:
        for suffix, size, grant, box_tier in sizes:
            iid = f"item-selectable_reward_box-item-card_limit_break-{attr_key}-{suffix}"
            held = q(iid)
            if not held:
                continue  # the game hides a bundle you don't hold
            rows.append(
                {
                    "name": f"{base_name} {size}",
                    "held": held,
                    "note": f"grants {grant} {grants}",
                    "item_id": iid,
                    "group": "bundle",
                    "icon": f"img_item_thumb_selectable-limit-break-attribute-box-{box_attr}-{box_tier}",
                    "grant_each": grant,
                    "grant_kind": grants,
                }
            )

    rows.append(
        {
            "name": "Hololium",
            "held": q("item-card_limit_break-cmn-sr"),
            "note": "Member Training",
            "item_id": "item-card_limit_break-cmn-sr",
            "group": "other",
        }
    )
    rows.append(
        {
            "name": "Lesson Pt",
            "held": q("item-card_level_up"),
            "note": "level up a Member",
            "item_id": "item-card_level_up",
            "group": "other",
        }
    )
    rows.append(
        {
            "name": "Hologold",
            "held": q("item-gold"),
            "note": "various",
            "item_id": "item-gold",
            "group": "other",
        }
    )
    return rows


def item_lang() -> dict:
    """`{lang id: English text}` for items: the holodori.best export, then the live master
    dump over it (newer). Rows: holodori `{data: {id, text}}`, live `{id, data: {"1000": text}}`."""
    lang: dict = {}
    for row in config.read_json(config.LANG_ITEM_PATH) or []:
        data = row.get("data") if isinstance(row, dict) else None
        if isinstance(data, dict) and data.get("id"):
            lang[data["id"]] = data.get("text")
    for row in (config.read_json(config.LIVE_LANG_ITEM_PATH) or {}).get("rows") or []:
        data = row.get("data") if isinstance(row, dict) else None
        if isinstance(data, dict) and row.get("id") and data.get("1000"):
            lang[row["id"]] = data["1000"]
    return lang


def account_items() -> dict:
    """EVERY item the account holds (`inventory.json`'s `items`, held > 0), named by
    the game's LangItem_Eng master, plus each holomem's unspent board points
    (`account_snapshot_main.json`'s `board_points`). A name the master lacks is
    `None` — the page shows the raw id rather than a guessed name."""
    inv = config.read_json(config.INVENTORY_PATH) or {}
    lang = item_lang()
    items = []
    for item_id, held in (inv.get("items") or {}).items():
        try:
            held = int(held or 0)
        except (TypeError, ValueError):
            continue
        if held <= 0:
            continue  # the game hides an item you don't hold
        items.append(
            {
                "item_id": item_id,
                "held": held,
                "name": lang.get(f"la-{item_id}-name"),
                "description": lang.get(f"la-{item_id}-description"),
            }
        )
    names: dict[str, Any] = {}
    for card in (config.read_json(config.CARDS_PATH) or {}).get("cards", []):
        parts = str(card.get("id", "")).split("-")
        if len(parts) > 1 and card.get("member"):
            names.setdefault("chr-" + parts[1], card["member"])
    points = []
    for row in (config.read_json(config.SNAPSHOT_PATH) or {}).get("board_points") or []:
        pid = str(row.get("point_id", ""))
        chr_id = pid.split("skill_tree_point-", 1)[-1] if pid.startswith("skill_tree_point-") else None
        if chr_id:
            points.append(
                {"chr": chr_id, "name": names.get(chr_id), "unspent": int(row.get("quantity") or 0)}
            )
    return {
        "items": items,
        "holomem_points": points,
        "inventory_captured_at": inv.get("_captured_at"),
        "inventory_source": inv.get("_source"),
    }


def serve_resources(h: Handler) -> None:
    """`GET /api/resources`: the board MATERIALS (cubes/hexas spent on nodes + the unspent
    wallet), the upgrade CURRENCIES (beads/crystals, hololium, lesson points, hologold),
    and every held item with the holomems' unspent board points."""
    precompute = basis.load_precompute()
    if precompute is None:
        h.reply_json(
            200,
            {
                "available": False,
                "reason": (
                    "could not import the recon tools (they need the packages of "
                    "requirements.txt; setup.cmd or setup.sh installs them)."
                ),
            },
        )
        return
    to = precompute.to
    try:
        prices = to._prices()
        owned = to._owned_nodes()
        spent, _spent_points = to._spent(owned, prices)
        balance, source = to.balance_from_disk()
        balance = balance or {}
    except Exception as exc:  # noqa: BLE001 — report as a first-class absence
        h.reply_json(200, {"available": False, "reason": f"board/inventory capture unavailable: {exc}"})
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
        "currencies": account_currencies(),
    }
    doc.update(account_items())  # every held item + holomem points (Inventory page)
    h.reply_json(200, doc)
