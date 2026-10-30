"""content_spec + publish: what goes to the server, and that a publish only adds. No network."""
from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import content_spec as cs  # noqa: E402
import publish as pub  # noqa: E402

D = cs.D


def test_kind_of_sorts_sync_lazy_and_never_the_account():
    assert cs.kind_of(D + "charts.json") == "sync"
    assert cs.kind_of(D + "two_stage/song_m0020.json") == "sync"
    assert cs.kind_of(D + "live2d/models/00031-nene-0001-00/x.moc3") == "lazy"
    assert cs.kind_of(D + "chibi/models/sdcos-00031-nene-0001-00.glb") == "lazy"
    # the account never leaves the PC
    for rel in ("account_snapshot_main.json", "board_state.json", "card_params.json",
                "inventory.json", "two_stage/account_song_m0020.json"):
        assert cs.kind_of(D + rel) is None, rel
    assert cs.kind_of("state/account_credential.json") is None
    # skill_icons: the node glyphs, not the song jackets pulled next to them
    assert cs.kind_of(D + "skill_icons/img_skill_icon_tree_01.png") == "sync"
    assert cs.kind_of(D + "skill_icons/img_music_jacket_m0145.png") is None
    # half-written and backup files
    assert cs.kind_of(D + "charts.json.123.tmp") is None
    assert cs.kind_of(D + "live2d/models/a/.hidden") is None


def make_tree(root: Path) -> None:
    for rel, body in {
        D + "charts.json": b"charts",
        D + "board_state.json": b"mine",
        D + "live2d/index.json": b"{}",
        D + "live2d/models/a/model.moc3": b"moc",
        D + "live2d/models/a/tex.webp": b"moc",          # same bytes as model.moc3
        D + "skill_icons/img_music_jacket_m1.png": b"big",
    }.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)


def test_manifest_lists_only_the_spec_with_hashes(tmp_path):
    make_tree(tmp_path)
    m = pub.build_manifest(tmp_path, {}, now=0)
    assert sorted(m["files"]) == [D + "charts.json", D + "live2d/index.json",
                                  D + "live2d/models/a/model.moc3", D + "live2d/models/a/tex.webp"]
    assert m["files"][D + "charts.json"] == {
        "sha256": pub.sha256_file(tmp_path / D / "charts.json"), "size": 6, "kind": "sync"}
    assert pub.summary(m) == {"sync": {"files": 2, "bytes": 8}, "lazy": {"files": 2, "bytes": 6}}
    assert m["id"] == "19700101T000000Z"


def test_hash_cache_skips_unchanged_files(tmp_path, monkeypatch):
    make_tree(tmp_path)
    cache: dict = {}
    pub.build_manifest(tmp_path, cache)
    calls = []
    monkeypatch.setattr(pub, "sha256_file", lambda p: calls.append(p) or "x" * 64)
    pub.build_manifest(tmp_path, cache)
    assert calls == []                                   # nothing changed: nothing rehashed
    (tmp_path / D / "charts.json").write_bytes(b"charts v2")
    pub.build_manifest(tmp_path, cache)
    assert [p.name for p in calls] == ["charts.json"]


def test_only_missing_objects_are_sent_once_each(tmp_path):
    make_tree(tmp_path)
    m = pub.build_manifest(tmp_path, {})
    charts = m["files"][D + "charts.json"]["sha256"]
    todo = pub.missing_objects(m, remote={charts})
    assert charts not in todo
    assert len(todo) == 2                                # index.json + one copy of the moc bytes
    buf = io.BytesIO()
    pub.write_tar(buf, tmp_path, todo)
    names = tarfile.open(fileobj=io.BytesIO(buf.getvalue())).getnames()
    assert sorted(names) == sorted(pub.object_name(s) for s in todo)
    assert all(n.startswith("objects/") and len(n.split("/")[-1]) == 64 for n in names)
