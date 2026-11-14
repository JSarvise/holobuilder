"""A Holomem Rating team for every song on ONE board (`ondemand.Engine.search_merged` with a
`board_cast`): a row's detail -- `/api/row-detail` with the song the page shows and the
search's own `shared_board` -- is the row's number on every chart, Expected and Max. The board
is the one the search scored every chart on, planned within the wallet for one song and
judged on all of them, or developed for the cast; never a board of the shown song's own.

Real data (the account's cards, boards and wallet, `charts_all.json`); SKIPS when it is
absent. Read-only (test_no_writes.py)."""

from __future__ import annotations

import pytest
from hbweb import search as routes  # first: it puts the web's modules on the path

HOLOMEM = "chr-00031"  # Momosuzu Nene: solo and group songs, so the cast has no common scope
LEADER = "card-00031-5-uniq-0105-00"
MEMBERS = (
    "card-00031-5-uniq-0105-00",
    "card-00034-5-uniq-0027-00",
    "card-03001-5-uniq-0033-00",
    "card-03005-5-uniq-0037-00",
    "card-03006-5-uniq-0038-00",
)
# "My cards, upgraded" at level max, boards planned within the wallet (the page's default)
LEVERS = {
    "level": "max",
    "bloom": "account",
    "keep_higher": True,
    "sp_training": "4",
    "all_invited": False,
    "no_refund": False,
    "branches": "GBRY",
    "targets": [],
    "boards": "plan",
    "max_points": False,
    "infinite": False,
    "max_resets": None,
    "reset_min_gain": 0,
}


def _charts() -> tuple[tuple[str, ...], list[str]]:
    """A group song of hers, then a solo one (EXPERT): the board is planned for the first."""
    import leaf_inputs

    try:
        library = leaf_inputs.chart_library(leaf_inputs.charts_path("HARD")).charts
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"charts_all.json absent: {exc}")
    sung = sorted(k for k, c in library.items() if k.endswith("/EXPERT") and HOLOMEM in (c.singers or ()))
    group = [k for k in sung if library[k].singer_type == "group"][:1]
    solo = [k for k in sung if library[k].singer_type == "solo"][:1]
    if not group or not solo:
        pytest.skip("no group and solo EXPERT chart of hers on disk")
    keys = (*group, *solo)
    return keys, [library[k].singer_type for k in keys]


@pytest.mark.parametrize("infinite", [False, True], ids=["planned-in-wallet", "developed"])
def test_a_shared_board_rows_detail_is_the_row_on_every_chart(infinite):
    import ondemand
    import rating_mode

    keys, types = _charts()
    levers = {**LEVERS, "infinite": infinite}
    a = ondemand.parse_assumptions(levers)
    assert a.replan is not infinite
    cast = rating_mode.one_board_cast(HOLOMEM, types)
    try:
        eng = ondemand.Engine(
            "account",
            rating_mode.ENGINE_DIFFICULTY,
            workers=1,
            assumptions=a,
            chart_keys=keys,
            board_cast=cast,
        )
    except Exception as exc:  # noqa: BLE001 - no account files / charts on this install
        pytest.skip(f"account inputs absent: {exc}")
    try:
        try:
            # the team alone in the pool: the search is quick, the board and its scoring are not
            others = tuple(c.id for c in eng.reps if c.id not in {LEADER, *MEMBERS})
            doc = eng.search_merged(
                ondemand.Filters(leader_card=LEADER, require=MEMBERS, exclude=others),
                objective="expected",
                top=1,
                assumptions=a,
            )
        except KeyError as exc:
            pytest.skip(f"the team is not in this account's pool: {exc}")
    finally:
        eng.close()
    assert doc["shared_board"] == {"charts": list(keys), "cast": [HOLOMEM], "scope": ""}
    row = doc["rows"][0]
    cards = [row["leader"], *row["members"]]
    for c in row["charts"]:
        music, difficulty = c["key"].split("/")
        # the body the page sends for this song (opt-rating.js -> opt-score.js `after`)
        body = {
            "song": music,
            "difficulty": difficulty,
            "basis": "account-levelmax",
            "cards": cards,
            "refine": False,
            "assumptions": levers,
            "platform": "mobile",
            "auto": False,
            "shared_board": doc["shared_board"],
            "as_is": False,
        }
        got = routes.row_figures(routes.row_request(body), body)
        assert (got["song_est"], got["song_max"]) == (c["expected"], c["max"]), c["key"]
        assert got["plan_song"] == (keys[0].split("/")[0] if not infinite else None)


def test_a_shared_board_names_only_its_own_charts():
    body = {
        "song": "m0099",
        "difficulty": "EXPERT",
        "cards": [LEADER, *MEMBERS],
        "shared_board": {"charts": "m0099/EXPERT", "cast": [HOLOMEM]},
    }
    with pytest.raises(Exception, match="shared_board"):
        routes.row_request(body)
