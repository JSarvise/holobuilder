"""The `.hbteam` team file (hbteam.py, `POST /api/export-team`).

What must hold: the file carries the row detail's own request and numbers and the account files
the score reads, and nothing else of the account (the snapshot cut to its board points); it is
saved without ever overwriting a file; a replayed detail is compared field by field. The route
case uses the maxed basis and SKIPS without the gitignored dumps; the replay itself (a sandbox
copy of the checkout, minutes) is run by hand, not here."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

import hbteam
import leaf_inputs  # noqa: F401  (puts tools/refresh, where team_file lives, on the path)
import team_file
from test_row_detail import SONG, _TEAM, _req, _skip_without_dumps


def test_differences_name_every_changed_field():
    want = {"song_est": 100.0, "member_order": ["a", "b"], "board_state": {"chr-1": {"nodes": ["G-001"]}},
            "leaf": "ignored", "distribution": {"p50": 90.0}}
    assert hbteam.differences(want, json.loads(json.dumps(want))) == []
    got = {**want, "song_est": 101.0, "member_order": ["b", "a"], "leaf": "other",
           "board_state": {"chr-1": {"nodes": ["G-001", "G-002"]}}, "distribution": {}}
    diffs = hbteam.differences(want, got)
    assert any(d.startswith(".song_est: 100.0 -> 101.0") for d in diffs)
    assert any(d.startswith(".member_order[0]") for d in diffs)
    assert any(d.startswith(".board_state.chr-1.nodes") for d in diffs)
    assert any(d.startswith(".distribution.p50") and "(absent)" in d for d in diffs)
    assert not any(d.startswith(".leaf") for d in diffs)          # the planner's own wording
    rounding: list = []
    near = {**want, "distribution": {"p50": 90.0 * (1 + 1e-15)}}
    assert hbteam.differences(want, near, rounding=rounding) == []    # the same sum, another order
    assert rounding == [".distribution.p50"]


def test_account_files_carry_only_what_the_score_reads(tmp_path):
    repo = tmp_path
    snapshot = {"board_points": [{"point_id": "skill_tree_point-chr-00001", "quantity": 3}],
                "decks": [1, 2], "missions": [3], "emblems": [4]}
    for rel in hbteam.ACCOUNT_FILES:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(json.dumps(snapshot if rel.endswith("account_snapshot_main.json")
                                           else {"file": rel}), "utf-8")
    (repo / "state" / "credential.json").write_text('{"secret": 1}', "utf-8")
    files = hbteam.account_files(repo)
    assert set(files) == set(hbteam.ACCOUNT_FILES)
    snap = next(v for k, v in files.items() if k.endswith("account_snapshot_main.json"))
    assert snap == {"board_points": snapshot["board_points"]}
    assert "credential" not in json.dumps(files)


def test_save_never_overwrites_and_names_the_row(tmp_path):
    notes = {"columns": ["note", "expected_value"], "rows": [[1, 10], [2, 12]]}
    doc = {"request": {"song": "m0342"}, "shown": {"song": 'A: "song"/b', "rank": 3},
           "team": [{"holomem": "Usada Pekora"}], "created_unix": 1793707200,
           "detail": {"notes": notes}}
    first, first_csv = team_file.save(doc, tmp_path)
    second, second_csv = team_file.save(doc, tmp_path)
    assert first != second and first.is_file() and second.is_file()
    assert first.suffix == hbteam.SUFFIX and "#3 Usada Pekora" in first.name
    assert not set(first.name) & hbteam.FORBIDDEN
    assert json.loads(first.read_text("utf-8")) == doc
    assert first_csv.stem == first.stem and second_csv.stem == second.stem
    assert first_csv.read_text("utf-8").splitlines() == ["note,expected_value", "1,10", "2,12"]
    bare, no_csv = team_file.save({k: v for k, v in doc.items() if k != "detail"}, tmp_path)
    assert bare.is_file() and no_csv is None


def test_load_refuses_other_files(tmp_path):
    bad = tmp_path / "x.hbteam"
    bad.write_text('{"format": "something-else"}', "utf-8")
    with pytest.raises(ValueError):
        hbteam.load(bad)
    newer = tmp_path / "y.hbteam"
    newer.write_text(json.dumps({"format": hbteam.FORMAT, "format_version": hbteam.FORMAT_VERSION + 1,
                                 "request": {}, "detail": {}, "account": {}}), "utf-8")
    with pytest.raises(ValueError, match="newer"):
        hbteam.load(newer)


def test_export_route_writes_the_rows_own_detail(web_base, tmp_path, monkeypatch):
    """The file holds the request, the server's detail for it (the same as `/api/row-detail`),
    the page's numbers and the account files; `summary` reads it back."""
    monkeypatch.setattr(team_file, "downloads_dir", lambda: tmp_path)
    body = {"song": SONG, "basis": "maxed", "cards": _TEAM, "auto": True}
    status, detail = _req(web_base + "/api/row-detail", {**body, "distribution": True})
    _skip_without_dumps(status, detail)
    assert status == 200, detail
    status, saved = _req(web_base + "/api/export-team", {**body, "shown": {"rank": 1, "expected": 1.0}})
    assert status == 200, saved
    path = Path(saved["path"])
    assert path.parent == tmp_path and path.suffix == hbteam.SUFFIX
    doc = hbteam.load(path)
    assert doc["request"] == body
    assert doc["detail"]["song_est"] == detail["song_est"]           # the AUTO row, not manual
    assert doc["detail"]["distribution"]["p50"] == detail["distribution"]["p50"]
    assert "law" not in doc["detail"]["distribution"] and "timeline" not in doc["detail"]
    assert [c["id"] for c in doc["team"]] == _TEAM
    assert set(doc["account"]["files"]) <= set(hbteam.ACCOUNT_FILES)
    assert doc["shown"] == {"rank": 1, "expected": 1.0}
    assert "Expected" in hbteam.summary(doc)
    # the note-by-note breakdown: the scorer's own terms, adding up to the Expected exactly,
    # in the file and in the CSV beside it
    notes = doc["detail"]["notes"]
    assert notes["verified"] is True and notes["total"] == detail["song_est"]
    col = notes["columns"].index("expected_value")
    csv_path = path.with_suffix(".csv")
    assert saved["csv"] == csv_path.name
    rows = list(csv.reader(csv_path.read_text("utf-8").splitlines()))
    assert rows[0] == notes["columns"] and len(rows) - 1 == len(notes["rows"])
    assert sum(int(r[col]) for r in rows[1:]) == detail["song_est"]
    assert all(int(r[notes["columns"].index("combo")]) == 0 for r in rows[1:])   # AUTO: no combo
