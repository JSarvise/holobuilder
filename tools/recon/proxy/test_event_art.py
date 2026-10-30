"""event_art.py: the open event's title, kind, times and banner (no network)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import event_art as ea  # noqa: E402


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _f(fn: int, v) -> bytes:
    if isinstance(v, int):
        return _varint(fn << 3) + _varint(v)
    if isinstance(v, str):
        v = v.encode()
    return _varint(fn << 3 | 2) + _varint(len(v)) + v


def _blob() -> bytes:
    chapter = _f(1, "marathon_event-normal-009-chapter-01") + _f(2, 1) + _f(4, 1792234799000) \
        + _f(13, "img_marathon_event_normal_top-009-01")
    marathon = _f(1, "exchange_booth_group-x") + _f(4, chapter)
    event = (_f(1, "marathon_event-normal-009") + _f(2, 1) + _f(3, "Point Rally Event")
             + _f(4, 1792033200000) + _f(5, 1792234799000) + _f(6, 1792753199000)
             + _f(7, "img_marathon_event_normal_logo-009-01") + _f(14, marathon))
    mission = _f(1, "event_mission-x") + _f(2, 3) + _f(3, "Event Mission")
    return _f(1, event) + _f(1, mission)


def _tables(tmp: Path, *, poster: bool = True, story: bool = False) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    cid = "marathon_event-normal-009-chapter-01"
    if poster:
        (tmp / "Poster.json").write_text(json.dumps({"rows": [
            {"id": f"poster-{cid}", "data": {"1": f"poster-{cid}", "2": f"la-poster-{cid}"}}]}))
        (tmp / "LangPoster_Eng.json").write_text(json.dumps({"rows": [
            {"id": f"la-poster-{cid}", "data": {"1000": "Neon Test Festival"}}]}))
    if story:
        (tmp / "LangEventStoryChapter_Eng.json").write_text(json.dumps({"rows": [
            {"id": f"la-event_story_chapter-{cid}", "data": {"1000": "Story Title"}}]}))
    return tmp


def test_the_wire_gives_kind_times_and_asset_ids():
    events = ea.parse_event_info(_blob())
    assert [e["event_id"] for e in events] == ["marathon_event-normal-009", "event_mission-x"]
    e = events[0]
    assert e["kind_label"] == "Point Rally Event" and e["type"] == "marathon_normal"
    assert e["starts_at"] == "2026-10-15T03:00:00+00:00"
    assert e["ends_at"] == "2026-10-17T10:59:59+00:00"
    assert e["logo_asset_id"] == "img_marathon_event_normal_logo-009-01"
    assert e["chapters"][0]["id"] == "marathon_event-normal-009-chapter-01"


def test_title_is_the_poster_name_else_the_story_chapter(tmp_path):
    cid = "marathon_event-normal-009-chapter-01"
    assert ea.chapter_titles(_tables(tmp_path / "a"), [cid]) == {cid: "Neon Test Festival"}
    assert ea.chapter_titles(_tables(tmp_path / "b", poster=False, story=True), [cid]) == {
        cid: "Story Title"}
    assert ea.chapter_titles(tmp_path / "none", [cid]) == {}


class _Img:
    width, height = 10, 5


def test_build_fills_title_and_art_and_skips_mission_events(tmp_path, monkeypatch):
    saved = []
    monkeypatch.setattr(ea, "_save_webp", lambda img, dst: (dst.parent.mkdir(exist_ok=True),
                                                            dst.write_bytes(b"x"),
                                                            saved.append(dst.name)))
    asked = []
    doc = ea.build(_blob(), _tables(tmp_path / "t"), tmp_path / "art",
                   fetch=asked.extend, extract=lambda path, name: _Img())
    assert [e["event_id"] for e in doc["events"]] == ["marathon_event-normal-009"]
    e = doc["events"][0]
    assert e["title"] == "Neon Test Festival"
    assert e["art"] == {"banner": "marathon_event-normal-009.webp",
                        "logo": "marathon_event-normal-009_logo.webp"}
    assert "img_banner_full_marathon_event-normal-009_lang-eng" in asked
    # a second run finds both files and asks the CDN for nothing
    asked.clear()
    ea.build(_blob(), _tables(tmp_path / "t"), tmp_path / "art", fetch=asked.extend,
             extract=lambda path, name: None)
    assert asked == []


def test_nothing_found_is_null_not_a_failure(tmp_path):
    def boom(names):
        raise SystemExit("no catalogue")
    doc = ea.build(_blob(), tmp_path / "no_tables", tmp_path / "art", fetch=boom,
                   extract=lambda path, name: None)
    e = doc["events"][0]
    assert e["title"] is None and e["art"] == {"banner": None, "logo": None}
    assert e["kind_label"] == "Point Rally Event"


def test_main_without_the_event_file_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ea, "EVENT_INFO", tmp_path / "missing.bin")
    assert ea.main([]) == 0
    assert "skipped" in capsys.readouterr().out


def test_the_refresh_runs_event_art_after_its_inputs():
    import refresh_all as ra
    order = [s.name for s in ra.STEPS]
    assert order.index("account") < order.index("event_art")
    assert order.index("catalog") < order.index("event_art")
    assert order.index("master") < order.index("event_art")


def test_title_from_the_refreshs_own_export_before_a_master_dump(tmp_path):
    """The refresh's own export (`main_<T>.json`) names the fields where a master dump numbers
    them; it is read first, and the dump is the fallback."""
    cid = "marathon_event-normal-009-chapter-01"
    own = tmp_path / "holodori"
    own.mkdir()
    (own / "main_Poster.json").write_text(json.dumps([
        {"id": f"poster-{cid}", "data": {"id": f"poster-{cid}", "nameLangId": f"la-poster-{cid}"}}]))
    (own / "main_LangPoster_Eng.json").write_text(json.dumps([
        {"id": f"la-poster-{cid}", "data": {"id": f"la-poster-{cid}", "text": "Own Export Title"}}]))
    dump = _tables(tmp_path / "dump")
    assert ea.chapter_titles((own, dump), [cid]) == {cid: "Own Export Title"}
    assert ea.chapter_titles((tmp_path / "absent", dump), [cid]) == {cid: "Neon Test Festival"}
