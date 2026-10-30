"""`gacha_export.render_html` -- the one-file HTML export.

The contract this file pins is what "self-contained" means: every image is a
`data:` URI (so the page renders with the network off), no `src`/`href` points at
`http(s)://`, every string that came from the store is HTML-escaped, and the charts
are inline SVG rather than a script that draws them. The fixture is in memory; the
only thing written is the tmp_path art file the render is asked to embed.
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path

import pytest
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gacha_export as ge  # noqa: E402

VIEW = {"last_sync": 5, "totals": {"pulls": 3, "by_rarity": {"5★": 1, "3★": 2}},
        "expected_by_rarity": {"5★": 0.15, "4★": 0.3, "3★": 2.55},
        # The SERVER's comparison block: the export reads this, never a second
        # copy of the arithmetic. covered + excluded + uncovered == totals.pulls.
        "comparison": {"covered_pulls": 3, "observed_by_rarity": {"5★": 1, "3★": 2},
                       "expected_by_rarity": {"5★": 0.15, "4★": 0.3, "3★": 2.55},
                       "excluded_pulls": 0, "uncovered_pulls": 0, "excluded": []},
        "tutorial": None,
        "banners": [{"gacha_id": "gacha-a", "name": "A <b>", "type": 1, "start_time": 1, "end_time": 2,
                     "pickup_card_ids": [], "art": {"banner": True, "logo": False, "icon": False}, "pulls": 3,
                     "by_rarity": {"5★": 1, "3★": 2}, "expected_by_rarity": {"5★": 0.15}, "has_rates": True,
                     "first_seen": 5, "is_live_last_sync": True, "counters": {"gacha_point": 3, "drawn_count_total": 0},
                     "cards": [{"card_id": "card-00001-5-x", "count": 1, "rarity": "5★", "is_pickup": True, "rate_ppm": 100000},
                               {"card_id": "card-00002-3-x", "count": 2, "rarity": "3★", "is_pickup": False, "rate_ppm": None}]}],
        "unidentified": [], "timeline": [{"day": "2023-11-14", "pulls": 3, "five_star": 1}]}


def test_render_is_self_contained_and_escaped(tmp_path):
    (tmp_path / "gacha-a").mkdir(); (tmp_path / "gacha-a" / "banner.png").write_bytes(b"\x89PNGfake")
    html = ge.render_html(VIEW, tmp_path, {"card-00001-5-x": b"RIFFfake"})
    assert "data:image/png;base64," in html and "data:image/webp;base64," in html
    assert not re.search(r"(src|href)=[\"']https?://", html)
    assert "A &lt;b&gt;" in html and "×2" in html
    assert "<svg" in html


def test_missing_thumb_falls_back_to_text_and_no_broken_uri(tmp_path):
    html = ge.render_html(VIEW, tmp_path, {})
    # No art on disk and no thumbs supplied: nothing is embedded, and the card that
    # has no image is named instead of being dropped.
    assert "data:image/webp;base64," not in html
    assert "card-00002-3-x" in html
    assert not re.search(r"(src|href)=[\"']https?://", html)


def test_covered_denominator_is_stated(tmp_path):
    html = ge.render_html(VIEW, tmp_path, {})
    assert "compared: 3 pulls from banners with a saved rate table" in html


def test_the_comparison_comes_from_the_server_not_the_banner_rows():
    """The tell: a banner row that disagrees with the server's block must not move the
    numbers. Here the banner claims 3 pulls with rates, the server says 1 was excluded --
    the export must print the server's 2, or the exclusion rule silently stops applying."""
    view = dict(VIEW)
    view["comparison"] = {"covered_pulls": 2, "observed_by_rarity": {"5★": 1, "3★": 1},
                          "expected_by_rarity": {"5★": 0.1}, "excluded_pulls": 1,
                          "uncovered_pulls": 0,
                          "excluded": [{"gacha_name": "★5 Ticket", "kind": "ticket", "pulls": 1}]}
    cov = ge.comparison(view)
    assert cov["covered_pulls"] == 2 and cov["observed"] == {"5★": 1, "3★": 1}


def test_excluded_pulls_are_named_not_just_counted(tmp_path):
    view = dict(VIEW)
    view["comparison"] = dict(VIEW["comparison"], covered_pulls=2, excluded_pulls=1,
                              excluded=[{"gacha_name": "Ticket <x>", "kind": "ticket", "pulls": 1}])
    html = ge.render_html(view, tmp_path, {})
    assert "excluded: ticket/tutorial pulls (1)" in html
    assert "Ticket &lt;x&gt; (ticket, 1)" in html


def test_owned_before_tracking_is_shown_and_the_tutorial_card_is_marked(tmp_path):
    view = dict(VIEW)
    view["tutorial"] = {"owned_not_tracked": [
        {"card_id": "card-00031-5-uniq-0025-00", "rarity": "5★",
         "acquired": 1785000000000, "is_tutorial": True},
        {"card_id": "card-00040-3-nrml-0000-00", "rarity": "3★",
         "acquired": 1786000000000, "is_tutorial": False}]}
    html = ge.render_html(view, tmp_path, {})
    assert "Owned before tracking started" in html
    assert "card-00031-5-uniq-0025-00" in html and "tutorial · not counted" in html
    assert html.count("tutorial · not counted") == 1   # only the tutorial one is marked


def test_no_snapshot_says_so_rather_than_showing_an_empty_list(tmp_path):
    html = ge.render_html(VIEW, tmp_path, {})   # VIEW["tutorial"] is None
    assert "needs the account snapshot" in html


def test_the_star_sprite_is_embedded_once(tmp_path):
    """The rarity row is printed a few hundred times; the PNG must be in the file ONCE
    (a CSS background), or the export grows by ~4 KB per star drawn."""
    if not ge.STAR_PNG.is_file():
        pytest.skip("star sprite not in this checkout's dumps")
    html = ge.render_html(VIEW, tmp_path, {})
    # Twice: once for the CSS class the HTML rows use, once for the SVG <image> in the
    # chart, which cannot reference a CSS class.
    assert html.count("data:image/png;base64,") == 2
    assert '<span class="stars">' in html


def test_unidentified_pulls_are_shown(tmp_path):
    view = dict(VIEW)
    view["unidentified"] = [{"gacha_name": "ticket <x>", "pulls": 2,
                             "by_rarity": {"3★": 2},
                             "cards": [{"card_id": "card-00003-3-x", "count": 2, "rarity": "3★",
                                        "is_pickup": False, "rate_ppm": None}]}]
    html = ge.render_html(view, tmp_path, {})
    assert "ticket &lt;x&gt;" in html and "<script" not in html.lower()


def test_hero_is_downscaled_to_webp(tmp_path):
    """The size bound. A real banner PNG (2833x1417, ~2.8 MB) costs its own length x4/3
    in base64; the export must re-encode it, so the WHOLE page comes out smaller than
    that one image alone would have been.

    The fixture is the repo's own committed banner -- a synthetic gradient is a
    pathological case for PNG-vs-WEBP and would test the codec, not this code.
    """
    pytest.importorskip("PIL.Image")
    src = HERE.parents[2] / "data" / "gacha" / "art" / "gacha-pickup-normal-260807" / "banner.png"
    if not src.is_file():
        pytest.skip("committed banner art not present")
    (tmp_path / "gacha-a").mkdir()
    png = tmp_path / "gacha-a" / "banner.png"
    png.write_bytes(src.read_bytes())
    raw_b64 = 4 * len(png.read_bytes()) // 3

    html = ge.render_html(VIEW, tmp_path, {})
    assert "data:image/webp;base64," in html
    # The only PNGs left are the star sprite's two embeds (CSS + the chart's <defs>);
    # the HERO must not be one, or the size bound this test exists for is gone.
    assert html.count("data:image/png;base64,") <= 2
    assert len(html) < raw_b64
    data, mime = ge._hero_bytes(png)
    assert mime == "image/webp" and len(data) < len(png.read_bytes())


def test_hero_falls_back_to_the_raw_png_when_it_cannot_be_decoded(tmp_path):
    (tmp_path / "gacha-a").mkdir()
    png = tmp_path / "gacha-a" / "banner.png"
    png.write_bytes(b"\x89PNGfake")
    assert ge._hero_bytes(png) == (b"\x89PNGfake", "image/png")


def test_permanent_banner_reads_permanent_not_an_empty_range():
    # The game sends 0 (or a sentinel far past any schedule) for the pools that never
    # close. `— → —` said nothing at all; these are the four cases, stated.
    assert ge.period({"start_time": 0, "end_time": 0}) == "permanent"
    assert ge.period({"start_time": 0, "end_time": 4102444800000}) == "permanent"
    assert ge.period({"start_time": 1754524800000}) == "since 2025-08-07"
    assert ge.period({"start_time": 0, "end_time": 1754524800000}) == "until 2025-08-07"
    assert ge.period({"start_time": 1754524800000, "end_time": 1755129600000}) == \
        "2025-08-07 → 2025-08-14"
    # The live wire sends the permanent pool as two nulls + is_end_time_hidden.
    assert ge.period({"is_end_time_hidden": 1}) == "permanent"
    assert ge.period({"start_time": 1754524800000, "is_end_time_hidden": 1}) == "since 2025-08-07"
    # Two nulls and NO flag is a closed banner whose dates were never captured -- unknown,
    # and relabelling it "permanent" would be a claim the data does not support.
    assert ge.period({}) is None


def test_the_last_timeline_label_never_collides_with_the_one_before_it():
    # 22 days: step=4, so the periodic tick lands on day 21 -- ONE slot (~31 px) before
    # the final day's label. That pair would overprint, so the periodic one is
    # dropped and the last is right-aligned to the plot edge.
    days = [f"2026-08-{d:02d}" for d in range(1, 23)]
    view = {"timeline": [{"day": d, "pulls": 1, "five_star": 0} for d in days]}
    svg = ge.timeline_chart(view)
    labels = re.findall(r'text-anchor="(\w+)" class="ax">(\d\d-\d\d)</text>', svg)
    xs = [float(x) for x in re.findall(r'<text x="([\d.]+)" y="188"', svg)]
    assert labels[-1] == ("end", "08-22")                 # the last day, right-aligned
    assert "08-21" not in [t for _, t in labels]          # the colliding tick is gone
    assert all(xs[i + 1] - xs[i] >= 42.0 for i in range(len(xs) - 1))


def _webp_bytes(w=8, h=4) -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h)).save(buf, format="WEBP", quality=85)
    return buf.getvalue()


def test_hero_picks_the_webp_when_that_is_what_is_on_disk(tmp_path):
    """New banners are saved as `.webp` (gacha_art.MAX_W); older masters stay `.png`. The
    export follows `art_ext`, and falls back to trying both when the view predates it."""
    pytest.importorskip("PIL.Image")
    (tmp_path / "gacha-a").mkdir()
    (tmp_path / "gacha-a" / "banner.webp").write_bytes(_webp_bytes())

    view = json.loads(json.dumps(VIEW))
    view["banners"][0]["art_ext"] = {"banner": "webp", "logo": None, "icon": None}
    assert "class=\"hero\"" in ge.banner_block(view["banners"][0], tmp_path, {})

    # No art_ext at all (an older view model): both extensions are tried, so it still finds it.
    assert "class=\"hero\"" in ge.banner_block(VIEW["banners"][0], tmp_path, {})


def test_hero_prefers_the_png_master_when_both_are_present(tmp_path):
    pytest.importorskip("PIL.Image")
    (tmp_path / "gacha-a").mkdir()
    (tmp_path / "gacha-a" / "banner.png").write_bytes(b"\x89PNGfake")
    (tmp_path / "gacha-a" / "banner.webp").write_bytes(_webp_bytes())
    b = dict(VIEW["banners"][0], art_ext={"banner": "png", "logo": None, "icon": None})
    # the png here is undecodable on purpose, so the raw-fallback mime names which file won
    assert "data:image/png;base64," in ge.banner_block(b, tmp_path, {})


def test_aliases_error_is_shown_on_the_page(tmp_path):
    """The tab's twin: if the one hand-maintained file did not parse, every alias silently
    stopped working and the export has to say so rather than just show fewer banners."""
    view = dict(VIEW, aliases_error="JSONDecodeError: Expecting value")
    html = ge.render_html(view, tmp_path, {})
    assert "banner_aliases.json could not be read: JSONDecodeError" in html
    assert "banner_aliases.json" not in ge.render_html(VIEW, tmp_path, {}).split("<h2>")[0]
