"""build_view aggregates the JSONL store into what gacha.js draws. Fixture store,
no real account data.

This file writes NO files: `tools/web/test_no_writes.py` walks every `.py` under
`tools/web/` (test files included) and rejects any `mkdir`/`write_bytes`/`write_text`
call, even one aimed at pytest's own `tmp_path`. The art-detection test below uses a
tiny in-memory path-like double instead of touching disk, the same pattern
`test_park_route.py` documents for this package.
"""
from __future__ import annotations
from pathlib import Path
import gacha_view as gv

CARDS = {"card-00001-5-x": {"rarity": "5★"}, "card-00002-3-x": {"rarity": "3★"}}
STORE = {
    "banners": [{"gacha_id": "gacha-a", "name": "A", "type": 1, "start_time": 1700000000,
                 "end_time": 1700900000, "pickup_card_ids": ["card-00001-5-x"], "seen_at": 5, "raw": {}}],
    "pulls": [
        {"draw_time": 1700000000000, "gacha_name": "A", "gacha_id": "gacha-a", "card_id": "card-00001-5-x", "first_seen": 5},
        {"draw_time": 1700000000000, "gacha_name": "A", "gacha_id": "gacha-a", "card_id": "card-00002-3-x", "first_seen": 5},
        {"draw_time": 1700000000000, "gacha_name": "A", "gacha_id": "gacha-a", "card_id": "card-00002-3-x", "first_seen": 5},
        {"draw_time": 1600000000000, "gacha_name": "Old", "gacha_id": None, "card_id": "card-00009-4-x", "first_seen": 5},
    ],
    "rates": [{"gacha_id": "gacha-a", "synced_at": 5, "normal": {"normal": [
        {"rarity": 5, "ppm": 500000, "cards": [{"card_id": "card-00001-5-x", "ppm": 100000, "is_rate_up": 1}]},
        {"rarity": 4, "ppm": 1000000, "cards": []},
        {"rarity": 3, "ppm": 8500000, "cards": []}]}, "card_select": {"error": "x"}}],
    "counters": [{"gacha_id": "gacha-a", "synced_at": 5, "gacha_point": {"gacha_point_id": "gp", "quantity": 7},
                  "buttons": [{"id": "b", "drawn_count": 2, "limit_count": 0}]}],
}


class _FakeArtFile:
    """Stands in for `art_dir / gid / "<k>.<ext>"`; `.is_file()` without touching disk."""

    def __init__(self, present: bool):
        self._present = present

    def is_file(self) -> bool:
        return self._present


class _FakeGachaDir:
    """Stands in for `art_dir / gid`; `.../ f"{k}.png"` never hits disk."""

    def __init__(self, files_present: set[str]):
        self._files_present = files_present

    def __truediv__(self, name: str) -> _FakeArtFile:
        return _FakeArtFile(name in self._files_present)


class _FakeArtDir:
    """Stands in for `art_dir` itself; `art_dir / gid` never hits disk."""

    def __init__(self, files_present: set[str]):
        self._files_present = files_present

    def __truediv__(self, gid: str) -> _FakeGachaDir:
        return _FakeGachaDir(self._files_present)


def test_totals_and_rarity_fallback(tmp_path):
    v = gv.build_view(STORE, CARDS, tmp_path)
    assert v["totals"]["pulls"] == 4
    assert v["totals"]["by_rarity"] == {"5★": 1, "4★": 1, "3★": 2}   # 4★ from the id digit
    assert v["last_sync"] == 5


def test_pulls_since_last_five_star(tmp_path):
    # Banner A's only draw group contains the 5★, so the pity counter sits at 0.
    v = gv.build_view(STORE, CARDS, tmp_path)
    assert v["banners"][0]["pulls_since_last_5"] == 0

    # Two later groups with no 5★: the counter is the pulls drawn AFTER the last
    # group that produced one. Groups are the unit — within a multi-draw the order
    # of the cards is unknown, so a group containing a 5★ resets to 0, never to
    # "the cards after it in the same group".
    store2 = dict(STORE)
    store2["pulls"] = STORE["pulls"] + [
        {"draw_time": 1700000100000, "gacha_name": "A", "gacha_id": "gacha-a",
         "card_id": "card-00002-3-x", "first_seen": 6},
        {"draw_time": 1700000200000, "gacha_name": "A", "gacha_id": "gacha-a",
         "card_id": "card-00002-3-x", "first_seen": 6},
    ]
    v2 = gv.build_view(store2, CARDS, tmp_path)
    assert v2["banners"][0]["pulls_since_last_5"] == 2

    # A banner that never produced a 5★ counts every pull it has.
    store3 = dict(STORE)
    store3["pulls"] = [p for p in STORE["pulls"] if p["card_id"] != "card-00001-5-x"]
    v3 = gv.build_view(store3, CARDS, tmp_path)
    assert v3["banners"][0]["pulls_since_last_5"] == 2


def test_banner_cards_counts_pickups_and_expected():
    art_dir = _FakeArtDir({"banner.png"})
    v = gv.build_view(STORE, CARDS, art_dir)
    b = v["banners"][0]
    assert b["pulls"] == 3 and b["has_rates"] is True and b["art"] == {"banner": True, "logo": False, "icon": False}
    assert b["art_ext"] == {"banner": "png", "logo": None, "icon": None}
    cards = {c["card_id"]: c for c in b["cards"]}
    assert cards["card-00002-3-x"]["count"] == 2
    assert cards["card-00001-5-x"]["is_pickup"] is True and cards["card-00001-5-x"]["rate_ppm"] == 100000
    assert abs(b["expected_by_rarity"]["5★"] - 3 * 0.05) < 1e-9
    assert b["counters"] == {"gacha_point": 7, "drawn_count_total": 2}


def test_unidentified_and_timeline(tmp_path):
    v = gv.build_view(STORE, CARDS, tmp_path)
    assert v["unidentified"] == [{"gacha_name": "Old", "pulls": 1, "by_rarity": {"4★": 1},
                                 "cards": [{"card_id": "card-00009-4-x", "count": 1, "rarity": "4★",
                                            "is_pickup": False, "rate_ppm": None}]}]
    days = {t["day"]: t for t in v["timeline"]}
    assert days["2023-11-14"]["pulls"] == 3 and days["2023-11-14"]["five_star"] == 1
    # expected only from banners WITH a table; the unidentified pull contributes nothing
    assert abs(v["expected_by_rarity"]["5★"] - 0.15) < 1e-9


def test_load_store_reads_missing_dir_as_empty(tmp_path):
    s = gv.load_store(tmp_path / "nope")
    assert s == {"pulls": [], "banners": [], "rates": [], "counters": [],
                 "aliases": {}, "aliases_error": None}


# -- Task 11: aliases, kinds, exclusions, 9+1 expected, tutorial ------------------

ALIASES = {
    "_doc": "ignored",
    "Sunny Summer Vacay Gacha": {"gacha_id": "gacha-pickup-normal-260728", "kind": "pickup"},
    "★5 Guaranteed Ticket Gacha": {"gacha_id": "gacha-common-ticket_r5-001", "kind": "ticket"},
}
_NORMAL = {"normal": [
    {"rarity": 5, "ppm": 500000, "cards": []},
    {"rarity": 4, "ppm": 1000000, "cards": []},
    {"rarity": 3, "ppm": 8500000, "cards": []}]}
_FIXED = [{"rarity": 5, "ppm": 10000000, "cards": []}]

STORE_ALIAS = {
    "aliases": ALIASES,
    "banners": STORE["banners"],
    "rates": STORE["rates"],
    "counters": STORE["counters"],
    "pulls": STORE["pulls"] + [
        {"draw_time": 1650000000000, "gacha_name": "★5 Guaranteed Ticket Gacha", "gacha_id": None,
         "card_id": "card-00001-5-x", "first_seen": 5},
        {"draw_time": 1660000000000, "gacha_name": "Sunny Summer Vacay Gacha", "gacha_id": None,
         "card_id": "card-00002-3-x", "first_seen": 5},
    ],
}


def _by_id(view):
    return {b["gacha_id"]: b for b in view["banners"]}


def test_alias_pulls_become_banners_not_unidentified(tmp_path):
    v = gv.build_view(STORE_ALIAS, CARDS, tmp_path)
    names = {u["gacha_name"] for u in v["unidentified"]}
    assert names == {"Old"}                      # only the truly unknown one is left
    bs = _by_id(v)
    tk = bs["gacha-common-ticket_r5-001"]
    assert tk["name"] == "★5 Guaranteed Ticket Gacha" and tk["is_alias"] is True
    assert tk["kind"] == "ticket" and tk["excluded_from_rates"] is True
    assert tk["pulls"] == 1 and tk["has_rates"] is False and tk["by_rarity"] == {"5★": 1}
    su = bs["gacha-pickup-normal-260728"]
    assert su["kind"] == "pickup" and su["excluded_from_rates"] is False and su["is_alias"] is True
    assert bs["gacha-a"]["is_alias"] is False and bs["gacha-a"]["kind"] == "common"


def test_comparison_excludes_ticket_pulls(tmp_path):
    v = gv.build_view(STORE_ALIAS, CARDS, tmp_path)
    c = v["comparison"]
    assert c["covered_pulls"] == 3                       # only gacha-a has rates and is not excluded
    assert c["observed_by_rarity"] == {"5★": 1, "3★": 2}
    assert abs(c["expected_by_rarity"]["5★"] - 0.15) < 1e-9
    assert c["excluded_pulls"] == 1
    assert c["excluded"] == [{"gacha_name": "★5 Guaranteed Ticket Gacha", "kind": "ticket", "pulls": 1}]
    assert v["expected_by_rarity"] == c["expected_by_rarity"]   # back-compat alias
    # every pull is accounted for: the rate-less alias banner (1) + the still-unidentified one (1)
    assert c["uncovered_pulls"] == 2
    assert c["covered_pulls"] + c["excluded_pulls"] + c["uncovered_pulls"] == v["totals"]["pulls"]


def test_alias_kind_is_not_inherited_by_a_namesake_with_another_id(tmp_path):
    """A LIVE banner whose name collides with an alias key pointing elsewhere keeps its own
    kind -- inheriting `ticket` from a namesake would silently drop its pulls from the
    comparison."""
    store = dict(STORE_ALIAS)
    store["banners"] = [{"gacha_id": "gacha-pickup-normal-260901", "name": "★5 Guaranteed Ticket Gacha",
                         "type": 1, "start_time": 1700000000, "end_time": 1700900000,
                         "pickup_card_ids": [], "seen_at": 5, "raw": {}}]
    store["rates"] = [{"gacha_id": "gacha-pickup-normal-260901", "synced_at": 5,
                       "normal": {"normal": _NORMAL["normal"]}, "card_select": {}}]
    store["pulls"] = [{"draw_time": 1700000000000, "gacha_name": "★5 Guaranteed Ticket Gacha",
                       "gacha_id": "gacha-pickup-normal-260901", "card_id": "card-00001-5-x",
                       "first_seen": 5}]
    v = gv.build_view(store, CARDS, tmp_path)
    b = _by_id(v)["gacha-pickup-normal-260901"]
    assert b["kind"] == "pickup" and b["excluded_from_rates"] is False and b["is_alias"] is False
    assert v["comparison"]["covered_pulls"] == 1 and v["comparison"]["excluded_pulls"] == 0


def _ten_pull_store(n, fixed):
    return {
        "aliases": {},
        "banners": [{"gacha_id": "gacha-fixed-260807", "name": "F", "type": 1, "start_time": 1,
                     "end_time": 2, "pickup_card_ids": [], "seen_at": 5, "raw": {}}],
        "counters": [],
        "rates": [{"gacha_id": "gacha-fixed-260807", "synced_at": 5,
                   "normal": {"normal": _NORMAL["normal"], "fixed": fixed}, "card_select": {}}],
        "pulls": [{"draw_time": 1700000000000, "gacha_name": "F", "gacha_id": "gacha-fixed-260807",
                   "card_id": "card-00002-3-x", "first_seen": 5} for _ in range(n)],
    }


def test_ten_pull_expected_uses_the_fixed_slot_table(tmp_path):
    v = gv.build_view(_ten_pull_store(10, _FIXED), CARDS, tmp_path)
    b = v["banners"][0]
    assert b["kind"] == "fixed" and b["excluded_from_rates"] is False
    assert abs(b["expected_by_rarity"]["5★"] - 1.45) < 1e-9    # 9 x 5% + 1 x 100%
    assert abs(b["expected_by_rarity"]["3★"] - 9 * 0.85) < 1e-9


def test_three_pull_group_uses_normal_only(tmp_path):
    v = gv.build_view(_ten_pull_store(3, _FIXED), CARDS, tmp_path)
    assert abs(v["banners"][0]["expected_by_rarity"]["5★"] - 0.15) < 1e-9


def test_empty_fixed_table_falls_back_to_normal_only(tmp_path):
    v = gv.build_view(_ten_pull_store(10, []), CARDS, tmp_path)
    assert abs(v["banners"][0]["expected_by_rarity"]["5★"] - 0.5) < 1e-9


SNAPSHOT = {"cards": [
    {"card_id": "card-00031-5-uniq", "acquired": 1000000},          # earliest -> tutorial
    {"card_id": "card-00002-3-x", "acquired": 1000000},             # tracked in pulls -> absent
    {"card_id": "card-00050-4-y", "acquired": 1300000},             # +300s -> still tutorial
    {"card_id": "card-00060-3-z", "acquired": 9000000},             # far later
]}


def test_tutorial_block_lists_owned_cards_never_pulled(tmp_path):
    v = gv.build_view(STORE_ALIAS, CARDS, tmp_path, snapshot=SNAPSHOT)
    rows = v["tutorial"]["owned_not_tracked"]
    assert [r["card_id"] for r in rows] == ["card-00031-5-uniq", "card-00050-4-y", "card-00060-3-z"]
    assert [r["is_tutorial"] for r in rows] == [True, True, False]
    assert rows[0]["rarity"] == "5★" and rows[0]["acquired"] == 1000000


def test_null_acquired_card_does_not_move_the_tutorial_origin(tmp_path):
    """One `acquired: null` must not collapse the origin to 0 -- that would un-flag the real
    first batch and flag the null card instead. Unknown stays unknown (`is_tutorial: None`)."""
    snap = {"cards": SNAPSHOT["cards"] + [{"card_id": "card-00070-4-nul", "acquired": None}]}
    rows = {r["card_id"]: r for r in gv.build_view(STORE_ALIAS, CARDS, tmp_path,
                                                   snapshot=snap)["tutorial"]["owned_not_tracked"]}
    assert rows["card-00031-5-uniq"]["is_tutorial"] is True     # still the first batch
    assert rows["card-00050-4-y"]["is_tutorial"] is True
    assert rows["card-00060-3-z"]["is_tutorial"] is False
    assert rows["card-00070-4-nul"]["acquired"] is None
    assert rows["card-00070-4-nul"]["is_tutorial"] is None


def test_tutorial_is_none_without_a_snapshot(tmp_path):
    assert gv.build_view(STORE_ALIAS, CARDS, tmp_path)["tutorial"] is None


class _FakeAliasPath:
    """Stands in for `store_dir / banner_aliases.json`. This package writes NO files, so a
    malformed aliases file is handed to the reader as TEXT rather than put on disk."""

    def __init__(self, text: str | None, exc: Exception | None = None):
        self._text, self._exc = text, exc

    def is_file(self) -> bool:
        return self._text is not None or self._exc is not None

    def read_text(self, encoding: str = "utf8") -> str:
        if self._exc is not None:
            raise self._exc
        return self._text


def test_parse_aliases_drops_the_doc_key_and_names_a_broken_file():
    doc, err = gv.parse_aliases('{"_doc": "notes", "Old": {"gacha_id": "gacha-old"}}')
    assert doc == {"Old": {"gacha_id": "gacha-old"}} and err is None
    doc, err = gv.parse_aliases('{"Old": {"gacha_id"')
    assert doc == {} and "JSONDecodeError" in err
    assert gv.parse_aliases('["not", "an", "object"]') == ({}, "top level is not an object")


def test_read_aliases_reports_a_malformed_or_unreadable_file():
    assert gv._read_aliases(_FakeAliasPath(None)) == ({}, None)     # absent is not an error
    doc, err = gv._read_aliases(_FakeAliasPath('{"Old": {'))
    assert doc == {} and err and "JSONDecodeError" in err
    doc, err = gv._read_aliases(_FakeAliasPath(None, exc=OSError("denied")))
    assert doc == {} and err == "OSError: denied"


def test_aliases_error_reaches_the_view(tmp_path):
    """The tab draws this as a g-flag note: every alias silently stopped working, and the
    user has to be told rather than shown fewer banners."""
    store = dict(STORE, aliases_error="JSONDecodeError: Expecting value: line 1 column 10")
    v = gv.build_view(store, CARDS, tmp_path)
    assert v["aliases_error"] == "JSONDecodeError: Expecting value: line 1 column 10"
    assert gv.build_view(STORE, CARDS, tmp_path)["aliases_error"] is None
