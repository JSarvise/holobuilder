"""`GET /api/songs` — the list the page ranks a song against.

The engine has `--song` and `--across-songs`; this route is what lets the page
ask for either. `song.timeline()` builds the five lanes, the chart's SPECIAL
cues and every ACTIVE's cooldown windows; this route lets the page choose a song.

What is pinned here is the CONTRACT, not the account's data: the route must answer
200 whether or not `charts.json` exists, must say which of the two it is, and
must never present a music id as though it were a song's title. The
data-dependent assertions skip on a fresh checkout, where `dumps/` is absent —
the same shape `test_routes.py` and `test_collection.py` use.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.request

import pytest

import server as web_server


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def web_base():
    port = _free_port()
    server = web_server.ThreadingServer(
        (web_server.BIND_HOST, port), web_server.Handler
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{web_server.BIND_HOST}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _songs(base: str) -> dict:
    with urllib.request.urlopen(f"{base}/api/songs", timeout=30) as response:
        assert response.status == 200
        return json.loads(response.read().decode("utf-8"))


def test_the_route_answers_200_with_or_without_the_chart_library(web_base):
    """Absent data is a STATE, not an error.

    `charts.json` is gitignored, so a fresh checkout has none. A 404 or a 500
    there would read to the page as a broken server rather than as "nothing to
    choose from yet", which is the distinction every other route on this server
    already keeps."""
    payload = _songs(web_base)
    assert "available" in payload
    assert isinstance(payload.get("songs"), list)
    if not payload["available"]:
        assert payload.get("reason"), "an unavailable list must say why"
        assert "build_charts.py" in payload["reason"], (
            "and must name the command that fixes it"
        )
        assert payload["songs"] == []


@pytest.mark.skipif(
    not web_server.CHARTS_PATH.is_file(), reason="dumps/ is gitignored"
)
def test_every_song_carries_what_makes_its_chart_score_differently(web_base):
    """A picker you cannot read is a picker you guess at.

    The five SPECIAL cues are the reason slot order binds at all — cue `i`
    fires the `i`-th member's SPECIAL — so a song is not identified by its name
    alone."""
    payload = _songs(web_base)
    assert payload["available"] and payload["songs"]

    for song in payload["songs"]:
        assert song["difficulty"] == "EXPERT", (
            "mixing difficulties would offer four rows per song that score "
            f"differently with nothing saying why; got {song['difficulty']}"
        )
        assert song["music_id"], "every row must name the chart the solver gets"
        assert song["notes"] > 0
        assert len(song["special_cues"]) == 5, (
            f"{song['music_id']} must carry one SPECIAL cue per deck slot; "
            f"got {len(song['special_cues'])}"
        )
        assert song["special_cues"] == sorted(song["special_cues"]), (
            f"{song['music_id']}: cues must be in ascending time — the slot "
            "binding is by cue ORDER"
        )


@pytest.mark.skipif(
    not web_server.CHARTS_PATH.is_file(), reason="dumps/ is gitignored"
)
def test_a_song_without_a_real_title_is_never_dressed_up_as_having_one(web_base):
    """`titled` is the whole point of the field.

    The titles come from a display-only language export. When one is missing the
    label falls back to the music id, and the page has to be able to tell those
    apart — otherwise it renders `m0042` in the position of a song's name and
    the reader has no way to know it is a placeholder."""
    payload = _songs(web_base)
    for song in payload["songs"]:
        assert song["title"], "every row needs a label"
        if not song["titled"]:
            assert song["title"] == song["music_id"], (
                "an untitled song's label must be its id verbatim, so the "
                f"placeholder is recognisable; got {song['title']!r}"
            )


@pytest.mark.skipif(
    not web_server.CHARTS_PATH.is_file(), reason="dumps/ is gitignored"
)
def test_the_ids_are_unique_so_a_choice_names_exactly_one_chart(web_base):
    payload = _songs(web_base)
    ids = [song["music_id"] for song in payload["songs"]]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, (
        f"a music id appearing twice makes the selection ambiguous: {duplicates}"
    )


# ---------------------------------------------------------------------------
# The filter metadata (main_Music.json joined to cards.json)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not web_server.CHARTS_PATH.is_file() or not web_server.MUSIC_PATH.is_file(),
    reason="dumps/ is gitignored",
)
def test_every_song_carries_the_filter_metadata_in_contract_tokens(web_base):
    """The picker filters on these, so they must be the CONTRACT's tokens and
    never the wire's raw enums (`MusicSingerType_MUSIC_SINGER_TYPE_GROUP` in a
    filter menu is a bug report waiting to be filed)."""
    payload = _songs(web_base)
    assert payload["filters_available"] is True
    for song in payload["songs"]:
        assert song["singer_type"] in {"solo", "unit", "whole", ""}, (
            f"{song['music_id']}: raw enum leaked -> {song['singer_type']!r}"
        )
        assert song["category"] in {"original", "cover", ""}, (
            f"{song['music_id']}: raw enum leaked -> {song['category']!r}"
        )
        assert song["seconds"] is None or (
            isinstance(song["seconds"], int) and song["seconds"] > 0
        )
        assert isinstance(song["character_ids"], list)
        assert [c["id"] for c in song["characters"]] == song["character_ids"], (
            f"{song['music_id']}: characters[] must be character_ids[] with "
            f"names attached, in the same order"
        )
        for character in song["characters"]:
            # The contract: the name falls back to the ID when no card names
            # that character -- a gap must be visible, never blank.
            assert character["name"], (
                f"{song['music_id']}: character {character['id']} has a "
                f"blank name; the fallback is the id itself"
            )

    # NEGATIVE CONTROLS -- a mapping that returned "" for everything would
    # sail through the membership checks above and the filters would be
    # three empty menus. The real data holds all three singer types and both
    # categories, so their absence here is a
    # broken join, not a data quirk.
    songs = payload["songs"]
    for token in ("solo", "unit", "whole"):
        assert any(s["singer_type"] == token for s in songs), (
            f"no song mapped to singer_type={token!r} -- the enum mapping or "
            f"the main_Music.json join is broken (vacuous-pass control)"
        )
    for token in ("original", "cover"):
        assert any(s["category"] == token for s in songs), (
            f"no song mapped to category={token!r} (vacuous-pass control)"
        )
    assert any(s["seconds"] for s in songs), "no song carries playingSeconds"
    assert any(s["characters"] for s in songs), "no song carries its cast"
    # the picker sorts by release: epoch ms, and more than the launch date alone
    assert all(s["release"] is None or s["release"] > 1_600_000_000_000 for s in songs)
    assert len({s["release"] for s in songs if s["release"]}) > 1, "every song on one date"


@pytest.mark.skipif(
    not web_server.CHARTS_PATH.is_file() or not web_server.MUSIC_PATH.is_file(),
    reason="dumps/ is gitignored",
)
def test_the_cast_resolves_to_holomem_names_not_just_ids(web_base):
    """`chr-00015` -> the member name off a `card-00015-...` row. If every
    character 'resolved' to its own id the fallback would be masquerading as
    the join -- so at least some must differ from their id."""
    payload = _songs(web_base)
    named = [
        character
        for song in payload["songs"]
        for character in song["characters"]
        if character["name"] != character["id"]
    ]
    assert named, (
        "no character resolved to a member name -- the cards.json join did "
        "not run and every 'name' is the id fallback"
    )


def test_absent_music_master_degrades_to_filters_available_false(
    web_base, monkeypatch, tmp_path
):
    """`main_Music.json` is gitignored, so a fresh checkout has none. The song
    LIST must survive that; only the filters degrade, and the payload says so
    (`filters_available: false`) instead of shipping empty menus."""
    real_music = web_server.MUSIC_PATH
    before = _songs(web_base)

    monkeypatch.setattr(
        web_server, "MUSIC_PATH", tmp_path / "no-such-main_Music.json"
    )
    after = _songs(web_base)

    assert after["filters_available"] is False
    # The list itself must not degrade with the filters.
    assert after["available"] == before["available"]
    assert [s["music_id"] for s in after["songs"]] == [
        s["music_id"] for s in before["songs"]
    ], "losing the music master must not add or drop a single song"
    for song in after["songs"]:
        # The keys stay, with honest empties -- the page must never KeyError
        # on a fresh checkout.
        assert song["singer_type"] == ""
        assert song["category"] == ""
        assert song["seconds"] is None
        assert song["character_ids"] == []
        assert song["characters"] == []

    # NEGATIVE CONTROL: the patch must have actually changed something, or
    # everything above passed vacuously (both fetches on the degraded path).
    if not real_music.is_file():
        pytest.skip(
            "NEGATIVE CONTROL UNAVAILABLE: main_Music.json is absent on this "
            "machine, so both fetches above exercised the same degraded path "
            "-- the degradation assertions held, but nothing here proved the "
            "non-degraded path differs"
        )
    assert before["filters_available"] is True, (
        "main_Music.json is on disk, yet the un-patched fetch already "
        "reported filters unavailable -- the patch flipped nothing"
    )
    if before["songs"]:
        assert any(s["singer_type"] for s in before["songs"]), (
            "un-patched fetch carried no singer_type at all -- the "
            "degradation test cannot discriminate"
        )
