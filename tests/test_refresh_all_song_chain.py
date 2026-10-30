"""`refresh_all.py`'s song chain, and the staleness it is there to catch.

When the game ships new songs, every artefact derived about songs can stay
silent about them, in a chain where each link's failure looks like success:

* a stale Octo catalogue on disk does not NAME their charts;
* `chart_download.py` can only fetch assets the catalogue names, so it fetches
  nothing and reports every known chart verified;
* `songs.json` has no chart to point at, so it reports the old song count and means it;
* and `active_lattice.build_chart_cache` rebuilds only when its cache file is
  ABSENT, so even once the charts land the solver would go on scoring the old
  set — producing a number computed over content that no longer matches the disk.

That last one is the dangerous one, because it is the only link that stays
silent once the rest are in order. `chart_cache_state` exists to name it, and a
guard nobody has seen fail has not been tested. Both outcomes are driven here.

The four-chart allowance is not a fudge: the `m9999` tutorial difficulties carry
two SPECIAL cues instead of five and `build_chart_cache` skips them by design, so
the cache is expected to sit exactly four BELOW the file count, never equal to it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROXY_DIR = REPO_ROOT / "tools" / "recon" / "proxy"
if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

import refresh_all  # noqa: E402


def _dumps(tmp_path, monkeypatch, *, sus: int, parsed: int | None):
    dumps = tmp_path / "dumps"
    (dumps / "charts_cdn").mkdir(parents=True)
    for i in range(sus):
        (dumps / "charts_cdn" / f"chart_m{i:04d}_expert.sus").write_text("x")
    if parsed is not None:
        (dumps / "lattice_charts.json").write_text(
            json.dumps({f"chart_m{i:04d}_expert": {} for i in range(parsed)})
        )
    monkeypatch.setattr(refresh_all, "DUMPS", dumps)
    return dumps


def test_a_cache_that_has_caught_up_reports_current(tmp_path, monkeypatch):
    _dumps(tmp_path, monkeypatch, sus=728, parsed=724)  # 728 - 4 tutorials

    lines = refresh_all.chart_cache_state()

    assert any("728 .sus files" in line for line in lines)
    assert any("current" in line for line in lines)
    assert not any("STALE" in line for line in lines)


def test_a_cache_left_behind_by_new_charts_is_reported_STALE(tmp_path, monkeypatch):
    """The stale-cache shape: twenty charts land, the cache is not rebuilt."""
    _dumps(tmp_path, monkeypatch, sus=728, parsed=704)

    lines = refresh_all.chart_cache_state()

    stale = [line for line in lines if "STALE" in line]
    assert stale, "twenty new charts left the parsed cache behind and nothing said so"
    assert "704 parsed against 724 expected" in stale[0]
    assert "scoring the OLD chart set" in stale[0]
    # Naming the fix matters: this is the one link with no automatic rebuild.
    assert "active_lattice.py" in stale[0]


def test_a_missing_cache_is_reported_rather_than_read_as_empty(tmp_path, monkeypatch):
    _dumps(tmp_path, monkeypatch, sus=10, parsed=None)

    lines = refresh_all.chart_cache_state()

    assert any("MISSING" in line for line in lines)


def test_the_song_chain_is_ordered_so_each_step_can_feed_the_next():
    """catalog names the charts -> charts downloads them -> songs points at them.

    Out of order, each step succeeds against the state the previous one has not
    produced yet, which is precisely how new songs stay invisible.
    """
    order = [s.name for s in refresh_all.STEPS]
    assert order.index("catalog") < order.index("charts") < order.index("songs")

    steps = {s.name: s for s in refresh_all.STEPS}
    # The catalogue comes from the client's own install, not the network — the
    # distinction is what lets `--offline` still refresh it.
    assert steps["catalog"].network is False
    assert steps["songs"].network is False
    # The download is the one genuinely networked step in the chain.
    assert steps["charts"].network is True


def test_the_catalogue_is_refreshed_before_anything_fetches_an_asset_by_name():
    """card_art, charts and jackets all look a name up in the Octo catalogue.

    If card_art ran before the catalogue, a game update's new cards would fail as
    not-in-catalogue against the previous update's catalogue, while the songs
    fetched fine a few steps later.
    """
    order = [s.name for s in refresh_all.STEPS]
    for consumer in ("card_art", "charts", "jackets"):
        assert order.index("catalog") < order.index(consumer), consumer


def test_the_connect_table_is_rebuilt_before_the_board_that_resolves_against_it():
    """build_board resolves each socketed card in skilltree.json's card_connect.

    If nothing rebuilt that file, it would stay at an old card list and the
    first newer card socketed would make `board` fail outright.
    """
    order = [s.name for s in refresh_all.STEPS]
    assert order.index("skilltree") < order.index("board") < order.index("board_export")
    assert {s.name: s for s in refresh_all.STEPS}["skilltree"].network is False


def test_the_level_curve_and_caps_are_rebuilt_once_the_new_cards_are_known():
    """collection.py restates a card at its real level through card_level_curve.json.

    If nothing rebuilt it, newly shipped cards would have no curve entry and the
    account basis would leave them at their maxed stats.
    """
    order = [s.name for s in refresh_all.STEPS]
    assert order.index("master_index") < order.index("level_curve")
    assert order.index("master_index") < order.index("caps")
    steps = {s.name: s for s in refresh_all.STEPS}
    assert steps["level_curve"].network is False and steps["caps"].network is False


def test_the_exp_to_level_tables_are_fetched_before_the_account_is_read():
    """The account step turns exp into a level through the master's Card + CardLevel.

    Tables fetched once and never refreshed do not know newer cards, which then
    merge into the collection with no level at all -- and the auto Member
    Power-Up, a sum over every owned card's level, is then wrong on every deck.
    """
    order = [s.name for s in refresh_all.STEPS]
    assert order.index("level_tables") < order.index("account")


def test_item_icons_is_a_soft_local_step_after_the_catalogue() -> None:
    """A new item's icon arrives with the refresh that brings the item (otherwise
    /inventory 404s until item_icons.py is run by hand). It reads the client's own cache
    (no network) and must never fail the refresh."""
    order = [s.name for s in refresh_all.STEPS]
    assert order.index("catalog") < order.index("item_icons")
    step = {s.name: s for s in refresh_all.STEPS}["item_icons"]
    assert step.network is False and step.soft is True
    # soft = never fails the refresh: the icons, the event cutoffs + history, and the
    # web's pictures
    assert sorted(s.name for s in refresh_all.STEPS if s.soft) == [
        "event_cutoffs", "event_emblems", "event_history", "item_icons", "member_icons", "park_export",
        "skill_icons", "ui_assets"]


def test_web_pictures_come_with_the_refresh() -> None:
    """A full refresh on a clean checkout must produce the holomem faces, board glyphs and park
    maps, not leave them to hand runs of their extractors. The faces must exist before
    board_export, which references them, and the faces need skilltree.json's member list."""
    order = [s.name for s in refresh_all.STEPS]
    assert order.index("skilltree") < order.index("member_icons") < order.index("board_export")
    assert order.index("node_master") < order.index("skill_icons")
    step = {s.name: s for s in refresh_all.STEPS}
    assert step["member_icons"].network is False and step["skill_icons"].network is False
    assert "park_export" not in refresh_all.ACCOUNT_STEPS


def test_ui_sprites_come_after_the_item_thumbs_they_copy() -> None:
    order = [s.name for s in refresh_all.STEPS]
    assert order.index("item_icons") < order.index("ui_assets")
    assert {s.name: s for s in refresh_all.STEPS}["ui_assets"].network is False


def test_pictures_later_defers_only_picture_steps() -> None:
    """`--pictures-later` moves to the background only steps nothing scored reads."""
    assert set(refresh_all.PICTURE_STEPS) <= set(refresh_all.STEP_NAMES)
    assert not set(refresh_all.PICTURE_STEPS) & set(refresh_all.ACCOUNT_STEPS)
    assert not {"charts", "songs", "chart_lib", "account", "board", "cards"} & set(refresh_all.PICTURE_STEPS)
