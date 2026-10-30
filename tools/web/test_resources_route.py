"""`_account_currencies` maps the account's item list to the Resources view's
currency rows — beads/crystals per attribute, the selectable bundles as their own
S/M/L items (Beads Bundle / A Jar of Crystals, matching the game's inventory), and
the loose singles (hololium / lesson pt / hologold).

Synthetic inventory (monkeypatched `_read_json`), so no real-account data is read.
The item_ids and names are the ones in the game's LangItem_Eng master
(`la-item-<id>-name`).
"""
from __future__ import annotations

import server as web_server


def _by_name(rows):
    return {r["name"]: r for r in rows}


def test_account_currencies_maps_items_and_bundles(monkeypatch):
    inv = {"items": {
        "item-card_limit_break-attribute_1-1": 100,   # Cute Beads
        "item-card_limit_break-attribute_3-1": 40,    # Happy Beads
        "item-card_limit_break-attribute_2-2": 50,    # Pure Crystals
        # Bundles are their OWN S/M/L items, shown with their raw held count.
        "item-selectable_reward_box-item-card_limit_break-attribute_1-10": 5,    # Beads Bundle S
        "item-selectable_reward_box-item-card_limit_break-attribute_1-100": 2,   # Beads Bundle M
        "item-selectable_reward_box-item-card_limit_break-attribute_2-10": 3,    # Jar of Crystals S
        "item-card_limit_break-cmn-sr": 7,            # Hololium
        "item-card_level_up": 999,                    # Lesson Pt
        "item-gold": 12345,                           # Hologold
    }}
    monkeypatch.setattr(web_server, "_read_json", lambda _p: inv)
    by = _by_name(web_server._account_currencies())

    assert by["Cute Beads"]["held"] == 100
    assert by["Happy Beads"]["held"] == 40
    assert by["Pure Crystals"]["held"] == 50
    assert by["Cute Beads"]["group"] == "beads"
    assert by["Pure Crystals"]["group"] == "crystals"

    # Each bundle is a distinct item at its RAW count (not a multiplied pool), with
    # the real bottle icon and a "grants N" note. Sizes not held are omitted.
    assert by["Beads Bundle S"]["held"] == 5
    assert by["Beads Bundle M"]["held"] == 2
    assert "Beads Bundle L" not in by                       # none held -> omitted
    assert by["A Jar of Crystals S"]["held"] == 3
    assert by["Beads Bundle S"]["group"] == "bundle"
    assert by["Beads Bundle S"]["note"] == "grants 10 beads"
    assert by["A Jar of Crystals S"]["note"] == "grants 10 crystals"
    # grant_each / grant_kind feed the "open every bundle" hypothetical the view shows.
    assert (by["Beads Bundle S"]["grant_each"], by["Beads Bundle S"]["grant_kind"]) == (10, "beads")
    assert (by["Beads Bundle M"]["grant_each"], by["Beads Bundle M"]["grant_kind"]) == (100, "beads")
    assert (by["A Jar of Crystals S"]["grant_each"], by["A Jar of Crystals S"]["grant_kind"]) == (10, "crystals")
    # Bundle icons are explicit octo asset names (they don't reverse-map from item_id):
    assert by["Beads Bundle S"]["icon"] == \
        "img_item_thumb_selectable-limit-break-attribute-box-01-01"
    assert by["A Jar of Crystals S"]["icon"] == \
        "img_item_thumb_selectable-limit-break-attribute-box-02-01"

    assert by["Hololium"]["held"] == 7
    assert by["Lesson Pt"]["held"] == 999
    assert by["Hologold"]["held"] == 12345
    assert by["Hologold"]["group"] == "other"


def test_account_currencies_absent_inventory_is_zeros_not_a_crash(monkeypatch):
    monkeypatch.setattr(web_server, "_read_json", lambda _p: None)
    rows = web_server._account_currencies()
    by = _by_name(rows)
    # Every loose currency still appears at 0 (a first-class "you hold none"),
    # and no bundle rows show (all zero -> omitted).
    for name in ("Cute Beads", "Pure Crystals", "Hololium", "Lesson Pt", "Hologold"):
        assert by[name]["held"] == 0
    assert not any(r["group"] == "bundle" for r in rows)


def test_account_items_names_every_held_item_and_holomem_points(monkeypatch):
    """`_account_items` (the Inventory page): every held item named by LangItem_Eng,
    zero-held items dropped, a name the master lacks left as None (never guessed),
    and each holomem's unspent board points with the member name from cards.json."""
    files = {
        web_server._INVENTORY_PATH: {"_captured_at": "2026-10-15T13:56:08+0200", "items": {
            "item-gold": 12345, "item-live_auto_play": 7, "item-gacha_ticket-r5-01": 0,
            "item-mini-game-chase-coin": 9}},
        web_server._LANG_ITEM_PATH: [
            {"id": "la-item-gold-name", "data": {"id": "la-item-gold-name", "text": "Hologold"}},
            {"id": "x", "data": {"id": "la-item-live_auto_play-name", "text": "Auto-play Ticket"}},
            {"id": "y", "data": {"id": "la-item-live_auto_play-description", "text": "Enables Auto-Play."}},
            {"id": "z", "data": {"id": "la-item-gacha_ticket-r5-01-name", "text": "Ticket"}}],
        web_server.CARDS_PATH: {"cards": [{"id": "card-00001-3-nrml-0000-00", "member": "Tokino Sora"}]},
        web_server._SNAPSHOT_PATH: {"board_points": [
            {"point_id": "skill_tree_point-chr-00001", "quantity": 65},
            {"point_id": "skill_tree_point-chr-00099", "quantity": 3}]},
    }
    monkeypatch.setattr(web_server, "_read_json", lambda p: files.get(p))
    doc = web_server._account_items()
    by = {r["item_id"]: r for r in doc["items"]}
    assert set(by) == {"item-gold", "item-live_auto_play", "item-mini-game-chase-coin"}
    assert by["item-gold"]["name"] == "Hologold" and by["item-gold"]["held"] == 12345
    assert by["item-live_auto_play"]["description"] == "Enables Auto-Play."
    assert by["item-mini-game-chase-coin"]["name"] is None          # not in the master
    assert doc["inventory_captured_at"] == "2026-10-15T13:56:08+0200"
    pts = {p["chr"]: p for p in doc["holomem_points"]}
    assert pts["chr-00001"] == {"chr": "chr-00001", "name": "Tokino Sora", "unspent": 65}
    assert pts["chr-00099"]["name"] is None


def test_account_items_absent_files_are_empty(monkeypatch):
    monkeypatch.setattr(web_server, "_read_json", lambda _p: None)
    doc = web_server._account_items()
    assert doc["items"] == [] and doc["holomem_points"] == []
    assert doc["inventory_captured_at"] is None


def test_account_items_live_master_names_what_holodori_lacks(monkeypatch):
    """The live Master/Get dump (`LangItem_Eng` in hololive-dreams-master) names the
    items an older one-off export lacks, and wins where both have a row."""
    files = {
        web_server._INVENTORY_PATH: {"items": {"item-gold": 1, "item-mini-game-chase-coin": 9}},
        web_server._LANG_ITEM_PATH: [
            {"id": "a", "data": {"id": "la-item-gold-name", "text": "Old Gold"}}],
        web_server._LIVE_LANG_ITEM_PATH: {"rows": [
            {"id": "la-item-gold-name", "data": {"1": "la-item-gold-name", "1000": "Hologold"}},
            {"id": "la-item-mini-game-chase-coin-name",
             "data": {"1": "la-item-mini-game-chase-coin-name", "1000": "Harmony Garden Coin"}}]},
    }
    monkeypatch.setattr(web_server, "_read_json", lambda p: files.get(p))
    by = {r["item_id"]: r for r in web_server._account_items()["items"]}
    assert by["item-gold"]["name"] == "Hologold"
    assert by["item-mini-game-chase-coin"]["name"] == "Harmony Garden Coin"
