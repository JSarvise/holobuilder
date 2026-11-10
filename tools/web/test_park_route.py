"""`GET /api/park` and `GET /park/<area>.png` — today's hidden collectables,
joined to the offline park export.

What is pinned here is the CONTRACT, and specifically the things a
plausible-but-wrong implementation would still pass:

* per-area `rolled_at` really comes from THAT area. On most real days all three
  areas agree, so a route that broadcast one timestamp to all three would look
  perfectly normal on those days and prove nothing. The fixture below is
  deliberately a day where they DIFFER (the casino a day behind, a case seen
  in a real capture);
* `collected` is a strict subset of `picked` and the flags line up;
* every `picked` mapping resolves to a pixel, and an unresolvable one is
  REPORTED rather than dropped — a silently missing marker is a treasure the
  user walks past;
* an absent export or an absent saved pick degrades with a reason and the command
  that fixes it, never a 500 and never a silent empty list;
* the route never logs in: the pick is the one the account refresh saved.

The saved pick is stubbed at the `park.park_saved_state` seam and the export at the
`park.park_export` seam, so the JOIN is exercised without touching the account or
the disk. The one test that would need real files (serving an actual PNG) skips
loudly when `dumps/park_export/` is absent.

⚠ This file writes NO files: `tools/web/test_no_writes.py` walks every `.py`
under `tools/web/` and would reject a `write_text`/`mkdir`/`open(...,"w")` here
too. Fixtures are in-memory dicts injected by monkeypatch, never on disk.
"""

from __future__ import annotations

import json
from pathlib import Path
import urllib.error
import urllib.request

import pytest

from hbweb import config, park


# ---------------------------------------------------------------------------
# Fixtures — an in-memory export and a live state, matching the REAL shapes
# (checked against tools/recon/proxy/dumps/park_export/park_spots.json)
# but small enough to reason about.
#
# Naming follows the real data: spot names are 2-digit
# (`reward_random_04`), mapping-id suffixes are 3-digit (`...a01-004`), area ids
# spell with hyphens (`area-a02-casino`) while mapping ids inside them use an
# underscore (`...a02_casino-033`). A spot name is therefore NEVER synthesised
# from a mapping id — the join always goes through `mapping_to_spot`.
# ---------------------------------------------------------------------------
FIXTURE_EXPORT = {
    "generated_at": "2026-08-09T04:00:00+02:00",
    "k": 1.2,
    "note": "TEST caveat: markers are ~1% uncertain; within a marker's width.",
    "areas": {
        "area-a01": {
            "image": "park-area-a01.png",
            "width": 1551,
            "height": 2048,
            "spots": {
                "reward_random_10": {"x": 731, "y": 1204},
                "reward_random_04": {"x": 100, "y": 200},
                # reward_random_99 is deliberately ABSENT here, so a mapping that
                # points at it is unplaceable and must be reported.
                "reward_treasure_01": {"x": 50, "y": 60},
            },
        },
        # The casino: a day behind the other two in the live state below.
        "area-a02-casino": {
            "image": "park-area-a02-casino.png",
            "width": 2048,
            "height": 2048,
            "spots": {
                "reward_random_01": {"x": 10, "y": 20},
                "reward_random_02": {"x": 30, "y": 40},
            },
        },
        "area-a02-game": {
            "image": "park-area-a02-game.png",
            "width": 2048,
            "height": 2048,
            "spots": {"reward_random_01": {"x": 11, "y": 21}},
        },
        # An area with treasures but NO random state at all — its rolled_at must
        # come back null, not be filled from a neighbour.
        "area-a02-lobby": {
            "image": "park-area-a02-lobby.png",
            "width": 1851,
            "height": 2048,
            "spots": {"reward_treasure_02": {"x": 5, "y": 6}},
        },
    },
    "mapping_to_spot": {
        "target_map-random_symbol-a01-010": {
            "area": "area-a01", "spot": "reward_random_10"},
        "target_map-random_symbol-a01-004": {
            "area": "area-a01", "spot": "reward_random_04"},
        # Points at a spot with no pixel in area-a01 -> "no pixel" unresolved.
        "target_map-random_symbol-a01-099": {
            "area": "area-a01", "spot": "reward_random_99"},
        "target_map-random_symbol-a02_casino-001": {
            "area": "area-a02-casino", "spot": "reward_random_01"},
        "target_map-random_symbol-a02_casino-002": {
            "area": "area-a02-casino", "spot": "reward_random_02"},
        "target_map-random_symbol-a02_game-001": {
            "area": "area-a02-game", "spot": "reward_random_01"},
    },
    "candidates": {
        "area-a01": [
            "target_map-random_symbol-a01-010",
            "target_map-random_symbol-a01-004",
        ],
    },
    "treasures": {
        "park_treasure-area-a01-001": {
            "area": "area-a01", "spot": "reward_treasure_01"},
        "park_treasure-area-a02-lobby-001": {
            "area": "area-a02-lobby", "spot": "reward_treasure_02"},
        # The real export's three unplaceable chests: an area with no map asset
        # and no entry in `areas`. Must be counted, not silently dropped.
        "park_treasure-area-a01-008": {
            "area": "area-a01-house", "spot": "reward_treasure_01"},
    },
}

# Three areas, DIVERGING roll times (this is the discriminating part).
_NEW = 1786221788763  # a01 and game rolled here
_OLD = 1786135388000  # casino a full day behind, exactly the measured case
FIXTURE_STATE = {
    "random": {
        "area-a01": {
            # 010 and 004 resolve; 099 has no pixel; the unknown mapping has no
            # mapping_to_spot entry at all. 004 is collected, 010 is not.
            "picked": [
                "target_map-random_symbol-a01-010",
                "target_map-random_symbol-a01-004",
                "target_map-random_symbol-a01-099",
                "target_map-random_symbol-a01-UNKNOWN",
            ],
            "collected": ["target_map-random_symbol-a01-004"],
            "last_refreshed_time": _NEW,
        },
        "area-a02-casino": {
            "picked": [
                "target_map-random_symbol-a02_casino-001",
                "target_map-random_symbol-a02_casino-002",
            ],
            "collected": [],
            "last_refreshed_time": _OLD,
        },
        "area-a02-game": {
            "picked": ["target_map-random_symbol-a02_game-001"],
            "collected": ["target_map-random_symbol-a02_game-001"],
            "last_refreshed_time": _NEW,
        },
    },
    "opened_treasures": ["park_treasure-area-a01-001"],
    "fixed_collected": [],
}


@pytest.fixture
def stub_park(monkeypatch):
    """Inject the in-memory export and live state at the two seams, so the join
    runs with no disk and no login. Returns nothing; tests fetch the route."""
    monkeypatch.setattr(park, "park_export", lambda: FIXTURE_EXPORT)
    monkeypatch.setattr(park, "park_saved_state", lambda: FIXTURE_STATE)


def _park(base: str) -> dict:
    with urllib.request.urlopen(f"{base}/api/park", timeout=30) as response:
        assert response.status == 200
        return json.loads(response.read().decode("utf-8"))


def _status_and_ctype(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", "")


def _area_by_id(payload: dict, area_id: str) -> dict:
    for area in payload["areas"]:
        if area["id"] == area_id:
            return area
    raise AssertionError(f"area {area_id!r} not in payload: "
                         f"{[a['id'] for a in payload['areas']]}")


# ---------------------------------------------------------------------------
# The discriminating tests
# ---------------------------------------------------------------------------
def test_rolled_at_is_each_areas_own_never_broadcast(web_base, stub_park):
    """The one that a broadcast bug would pass on most real days.

    The fixture is a day where the areas DIFFER, so the test can tell "each
    area's own timestamp" from "one timestamp copied to all three". It asserts
    both directions: every present area carries exactly its own stamp, and the
    casino specifically carries its OLDER stamp rather than the newest (which is
    what a `max()`-style broadcast would hand it)."""
    payload = _park(web_base)

    rolled = {a["id"]: a["rolled_at"] for a in payload["areas"]}
    assert rolled["area-a01"] == _NEW
    assert rolled["area-a02-game"] == _NEW
    assert rolled["area-a02-casino"] == _OLD, (
        "the casino must carry ITS OWN (older) roll time; getting _NEW here is "
        "the broadcast bug this fixture exists to catch"
    )
    # An area with no live row must NOT be filled from a neighbour.
    assert rolled["area-a02-lobby"] is None

    # DISCRIMINATION GUARD: if the fixture ever stopped diverging, this test
    # would silently become vacuous. Assert it still discriminates.
    present = [v for v in rolled.values() if v is not None]
    assert len(set(present)) >= 2, (
        "fixture no longer has diverging roll times — this test can no longer "
        "distinguish per-area from broadcast and must be repaired"
    )
    assert _OLD != _NEW


def test_collected_is_a_subset_of_picked_and_flags_line_up(web_base, stub_park):
    """`collected` is per-symbol, and every symbol flagged collected must be one
    of the area's own picks with the flag matching the live collected list."""
    payload = _park(web_base)
    a01 = _area_by_id(payload, "area-a01")

    picked_by_mapping = {p["mapping"]: p for p in a01["picked"]}
    flagged_collected = {m for m, p in picked_by_mapping.items() if p["collected"]}
    live_collected = set(FIXTURE_STATE["random"]["area-a01"]["collected"])

    # Subset: nothing is flagged collected that was not both picked and in the
    # live collected list.
    assert flagged_collected <= set(picked_by_mapping)
    assert flagged_collected == live_collected & set(picked_by_mapping)
    # The specific expectations: 004 collected, 010 not.
    assert picked_by_mapping["target_map-random_symbol-a01-004"]["collected"] is True
    assert picked_by_mapping["target_map-random_symbol-a01-010"]["collected"] is False

    # DISCRIMINATION GUARD: a01 must contain BOTH a collected and a
    # not-collected resolvable pick, or the flag assertions above pass
    # vacuously (e.g. an implementation that hardcodes True or False).
    flags = {p["collected"] for p in a01["picked"]}
    assert flags == {True, False}, (
        "area-a01 must carry both a collected and an uncollected pick, or this "
        "test cannot tell a real flag from a constant"
    )


def test_every_picked_resolves_to_a_pixel_or_is_reported(web_base, stub_park):
    """Resolvable picks land on integer pixels; unresolvable ones appear in
    `unresolved` with a reason and are NOT silently dropped."""
    payload = _park(web_base)
    a01 = _area_by_id(payload, "area-a01")

    # Two of the four a01 picks resolve.
    resolved = {p["mapping"] for p in a01["picked"]}
    assert resolved == {
        "target_map-random_symbol-a01-010",
        "target_map-random_symbol-a01-004",
    }
    for p in a01["picked"]:
        assert isinstance(p["x"], int) and isinstance(p["y"], int)
        assert p["spot"] and p["spot"].startswith("reward_random_")

    # The other two are reported, each with a distinct reason — and are absent
    # from `picked` (reported, not dropped INTO the placed list).
    unresolved = {u["mapping"]: u["reason"] for u in a01["unresolved"]}
    assert set(unresolved) == {
        "target_map-random_symbol-a01-099",       # spot exists, no pixel
        "target_map-random_symbol-a01-UNKNOWN",   # no mapping_to_spot entry
    }
    assert "no entry" in unresolved["target_map-random_symbol-a01-UNKNOWN"]
    assert "no pixel" in unresolved["target_map-random_symbol-a01-099"]
    assert resolved.isdisjoint(unresolved)

    # Counted at the top too, so the page can surface it without walking areas.
    assert payload["unresolved_count"] == 2

    # DISCRIMINATION GUARD: an implementation that dropped everything would also
    # show "no picks placed" — assert resolution genuinely produced placed
    # markers, so a green bar means the join WORKS, not that it failed quietly.
    assert len(a01["picked"]) == 2


def test_candidates_and_treasures_resolve_through_the_export(web_base, stub_park):
    """Candidates carry pixels; treasures carry their opened flag; and the three
    chests in areas with no map are counted, never returned as 39-of-42."""
    payload = _park(web_base)
    a01 = _area_by_id(payload, "area-a01")

    assert {(c["x"], c["y"]) for c in a01["candidates"]} == {(731, 1204), (100, 200)}

    treasures = {t["id"]: t for t in a01["treasures"]}
    assert treasures["park_treasure-area-a01-001"]["opened"] is True
    assert treasures["park_treasure-area-a01-001"]["x"] == 50

    lobby = _area_by_id(payload, "area-a02-lobby")
    lobby_t = {t["id"]: t for t in lobby["treasures"]}
    assert lobby_t["park_treasure-area-a02-lobby-001"]["opened"] is False

    # The unplaceable chest (area-a01-house, no map) is counted, not emitted.
    assert payload["unplaceable_treasures"] == 1
    placed_ids = {t["id"] for a in payload["areas"] for t in a["treasures"]}
    assert "park_treasure-area-a01-008" not in placed_ids


def test_image_urls_and_dimensions_travel_with_each_area(web_base, stub_park):
    payload = _park(web_base)
    assert payload["available"] is True
    assert payload["note"] == FIXTURE_EXPORT["note"]
    a01 = _area_by_id(payload, "area-a01")
    assert a01["image"] == "/park/area-a01.png"
    assert (a01["width"], a01["height"]) == (1551, 2048)


# ---------------------------------------------------------------------------
# First-class absence
# ---------------------------------------------------------------------------
def test_absent_export_degrades_with_a_reason(web_base, monkeypatch):
    """No `park_spots.json` is a STATE, not a 500 — and it names the command
    that builds it."""
    monkeypatch.setattr(park, "park_export", lambda: None)
    payload = _park(web_base)
    assert payload["available"] is False
    assert payload["areas"] == []
    assert "park_export.py" in payload["reason"], (
        "an absent export must name the command that fixes it"
    )


def test_no_saved_pick_asks_for_a_refresh(web_base, monkeypatch):
    """Before the first account refresh there is no saved pick: the page says to
    press Refresh, never a 500 and never an empty park that looks real."""
    monkeypatch.setattr(park, "park_export", lambda: FIXTURE_EXPORT)
    monkeypatch.setattr(park, "park_saved_state", lambda: None)
    payload = _park(web_base)
    assert payload["available"] is False
    assert "Refresh" in payload["reason"]
    # the maps still travel (the Free Chat section draws on them), with nothing picked on them
    assert {a["id"] for a in payload["areas"]} == set(FIXTURE_EXPORT["areas"])
    assert all(not a["picked"] and a["rolled_at"] is None for a in payload["areas"])


def test_the_saved_pick_carries_its_capture_time(web_base, monkeypatch):
    monkeypatch.setattr(park, "park_export", lambda: FIXTURE_EXPORT)
    monkeypatch.setattr(park, "park_saved_state",
                        lambda: {**FIXTURE_STATE, "_captured_at": "2026-11-05T12:00:00+0100"})
    payload = _park(web_base)
    assert payload["available"] is True
    assert payload["captured_at"] == "2026-11-05T12:00:00+0100"


def test_the_park_route_cannot_log_in():
    """A login from the app ends the game's session on the account, so the web side
    holds no path to one: no live read, no `park_treasure` import."""
    source = Path(park.__file__).read_text("utf8")
    for word in ("park_treasure", "userdata_live", "login("):
        assert word not in source, word


def test_park_saved_state_reads_the_refresh_output(monkeypatch, tmp_path):
    saved = tmp_path / "park_state.json"
    monkeypatch.setattr(config, "PARK_STATE_PATH", saved)
    assert park.park_saved_state() is None
    saved.write_bytes(json.dumps(FIXTURE_STATE).encode())  # noqa: test-only file in tmp_path
    assert park.park_saved_state() == FIXTURE_STATE


# ---------------------------------------------------------------------------
# The image route — the path handling is the quietly-wrong surface
# ---------------------------------------------------------------------------
def test_bad_park_image_names_are_rejected_400(web_base):
    """Anything that is not `area-<lower/digit/hyphen>.png` is a 400 before the
    filesystem is touched."""
    for name in ("notanarea.png", "area-a01.jpg", "area-A01.png", "area-a01"):
        status, _ = _status_and_ctype(f"{web_base}/park/{name}")
        assert status == 400, f"{name!r} should be a bad name (got {status})"


def test_park_image_name_regex_forbids_traversal():
    """Unit guard on the regex, since URL normalisation can hide a traversal
    before it reaches the handler: the pattern itself must reject `..`, slashes
    and backslashes, and accept the real area ids."""
    reject = ["area-../secret.png", "area-a01/../x.png", "area-a01\\x.png",
              "../park-area-a01.png", "area-a01.png.exe", "area_a01.png"]
    for name in reject:
        assert not park.PARK_IMAGE_NAME_RE.match(name), name
    for name in ["area-a01.png", "area-a02-casino.png", "area-a02-lobby.png"]:
        assert park.PARK_IMAGE_NAME_RE.match(name), name


def test_missing_park_image_is_404(web_base, monkeypatch, tmp_path):
    """A well-formed name whose file is absent is a 404, not a 500 or a 400 —
    the same first-class-absence contract the other image routes keep."""
    monkeypatch.setattr(config, "PARK_EXPORT_DIR", tmp_path)
    status, _ = _status_and_ctype(f"{web_base}/park/area-a01.png")
    assert status == 404


@pytest.mark.skipif(
    not config.PARK_SPOTS_PATH.is_file(),
    reason="dumps/park_export/ is gitignored",
)
def test_real_png_is_served_when_the_export_is_present(web_base):
    """When the export really is on disk, the map bytes come back as a PNG.
    Skips loudly on a fresh checkout rather than passing vacuously."""
    status, ctype = _status_and_ctype(f"{web_base}/park/area-a01.png")
    assert status == 200
    assert ctype == "image/png"


# ---------------------------------------------------------------------------
# Free Chat: the export's catalogue joined to the refreshes' read log
# ---------------------------------------------------------------------------
FC_EXPORT = {
    "chats": {
        "chat-read": {"site": "facility-a01-house", "x": 1, "y": 2,
                      "cast": ["chr-00001", "chr-00013"], "conditions": ["cd-q"]},
        "chat-pending": {"site": "facility-a01-house", "x": 1, "y": 2,
                         "cast": ["chr-00013", "chr-00002"], "conditions": ["cd-q"]},
        "chat-unseen": {"site": "facility-a01-multiplay", "x": 5, "y": 6,
                        "cast": ["chr-00024", "chr-00001"], "conditions": []},
    },
    "sites": {
        "facility-a01-house": {"name": "holo Home", "map": "area-a01", "x": 1, "y": 2,
                               "inside": None, "photo": "park-site-area-a01-house-00.jpg",
                               "icon_image": "park-icon-house.png"},
        "facility-a01-multiplay": {"name": "Fortune Network", "map": "area-a01", "x": 5, "y": 6,
                                   "inside": "area-a01-multiplay", "photo": None,
                                   "icon_image": None},
    },
    "conditions": {"cd-q": "Main quest 1-2 cleared"},
    "characters": {"chr-00001": {"name": "Tokino Sora", "short": "Sora"},
                   "chr-00002": {"name": "Roboco", "short": "Roboco"},
                   "chr-00013": {"name": "AZKi", "short": "AZKi"},
                   "chr-00024": {"name": "Amane Kanata", "short": "Kanata"}},
}
FC_LOG = {"chats": {
    "chat-read": {"read": ["chr-00001", "chr-00013"], "seen": "2026-10-26T02:37:44+01:00",
                  "read_seen": "2026-08-30T02:29:44+02:00"},
    "chat-pending": {"read": [], "seen": "2026-10-26T02:37:44+01:00", "read_seen": None},
    # a row the catalogue no longer has must not invent a chat
    "gone": {"read": ["chr-00001"], "seen": "2026-08-08T00:54:02+02:00", "read_seen": None}},
    "observations": [{"at": "2026-08-08T00:54:02+02:00", "areas": ["area-a01"]},
                     {"at": "2026-10-26T02:37:44+01:00", "areas": ["area-a01"]}]}


def test_free_chat_status_is_read_pending_or_unseen():
    out = park.join_free_chat(FC_EXPORT, FC_LOG, {"chr-00001": 6, "chr-00013": 10})
    status = {c["id"]: c["status"] for c in out["chats"]}
    assert status == {"chat-read": "read", "chat-pending": "pending", "chat-unseen": "unseen"}
    assert (out["total"], out["read"], out["pending"], out["unseen"]) == (3, 1, 1, 1)
    assert [c for c in out["chats"] if c["id"] == "chat-read"][0]["conditions"] == ["Main quest 1-2 cleared"]
    assert out["first_seen"] == "2026-08-08T00:54:02+02:00" and out["observations"] == 2


def test_free_chat_sites_and_holomems_tally_their_own_chats():
    out = park.join_free_chat(FC_EXPORT, FC_LOG, {"chr-00001": 6, "chr-00013": 10})
    sites = {s["id"]: s for s in out["sites"]}
    house = sites["facility-a01-house"]
    assert (house["total"], house["read"], house["pending"], house["unseen"]) == (2, 1, 1, 0)
    assert house["photo"] == "/park/site-area-a01-house-00.jpg"
    assert house["icon"] == "/park/icon-house.png"
    assert sites["facility-a01-multiplay"]["inside"] is True
    who = {c["id"]: c for c in out["characters"]}
    assert (who["chr-00013"]["total"], who["chr-00013"]["read"]) == (2, 1)
    assert who["chr-00013"]["game_read"] == 10 and who["chr-00013"]["game_capped"] is True
    assert who["chr-00001"]["game_capped"] is False
    # a holomem the game gives no Free Chat achievement
    assert who["chr-00024"]["game_read"] is None


def test_free_chat_without_a_log_is_all_unseen_and_without_an_export_says_why():
    out = park.join_free_chat(FC_EXPORT, None, {})
    assert out["unseen"] == 3 and out["observations"] == 0 and out["first_seen"] is None
    absent = park.join_free_chat(None, FC_LOG, {})
    assert absent["available"] is False and "park_export.py" in absent["reason"]


def test_free_chat_counters_come_from_the_games_achievement():
    snapshot = {"missions": [
        {"mission_id": "mission-achievement-chr-00001-absolute_park_talk_free_unique_count",
         "progress": 6},
        {"mission_id": "mission-achievement-chr-00001-increment_live_play_count-m0032",
         "progress": 32}]}
    assert park.free_chat_counters(snapshot) == {"chr-00001": 6}
    assert park.free_chat_counters(None) == {}


def test_the_park_route_carries_free_chat(web_base, stub_park, monkeypatch):
    monkeypatch.setattr(park, "park_free_chat", lambda: {"available": True, "total": 3})
    assert _park(web_base)["free_chat"] == {"available": True, "total": 3}


def test_site_photos_and_icons_are_servable_names_only():
    for name in ["site-area-a01-house-00.jpg", "icon-house.png", "icon-fishing.png", "symbol.png"]:
        assert park.PARK_IMAGE_NAME_RE.match(name), name
    for name in ["site-area-a01-house-00.png", "icon-house.jpg", "site-../x.jpg",
                 "icon-../x.png", "site-area-a01/../x.jpg", "icon-House.png"]:
        assert not park.PARK_IMAGE_NAME_RE.match(name), name


@pytest.mark.skipif(
    not (config.PARK_EXPORT_DIR / "park-site-area-a01-house-00.jpg").is_file(),
    reason="dumps/park_export/ is gitignored",
)
def test_real_site_photo_is_served_as_jpeg(web_base):
    status, ctype = _status_and_ctype(f"{web_base}/park/site-area-a01-house-00.jpg")
    assert status == 200 and ctype == "image/jpeg"
