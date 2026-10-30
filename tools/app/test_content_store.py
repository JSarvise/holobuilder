"""content_store: sync brings the 'sync' files, lazy serves the rest, the cache is capped.
The server is a dict; nothing touches the network."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import content_store as cst  # noqa: E402


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def server(files: dict[str, tuple[str, bytes]]):
    """{rel: (kind, body)} -> (fetch, calls)."""
    manifest = {"id": "m1", "files": {rel: {"sha256": sha(b), "size": len(b), "kind": k}
                                      for rel, (k, b) in files.items()}}
    objects = {f"objects/{sha(b)[:2]}/{sha(b)}": b for _, b in files.values()}
    calls = []

    def fetch(route: str) -> bytes:
        calls.append(route)
        if route == "manifest.json":
            return json.dumps(manifest).encode()
        return objects[route]
    return fetch, calls


FILES = {"d/charts.json": ("sync", b"charts"), "d/icons/a.png": ("sync", b"icon"),
         "d/live2d/m.moc3": ("lazy", b"m" * 100), "d/live2d/n.moc3": ("lazy", b"n" * 100)}


def test_sync_writes_only_sync_files_and_skips_what_matches(tmp_path):
    fetch, calls = server(FILES)
    s = cst.ContentStore(tmp_path, verify=False, fetch=fetch)
    seen = []
    r = s.sync(seen.append)
    assert r == {"manifest": "m1", "files": 2, "bytes": 10}
    assert (tmp_path / "d/charts.json").read_bytes() == b"charts"
    assert not (tmp_path / "d/live2d/m.moc3").exists()          # lazy stays on the server
    assert seen[-1]["i"] == seen[-1]["n"] == 2
    calls.clear()
    assert s.sync()["files"] == 0                               # second run: nothing to fetch
    assert calls == ["manifest.json"]


def test_lazy_prefers_local_then_caches_then_serves_from_cache(tmp_path):
    fetch, calls = server(FILES)
    s = cst.ContentStore(tmp_path, verify=False, fetch=fetch)
    s.sync()
    assert s.lazy("d/charts.json") == tmp_path / "d/charts.json"
    p = s.lazy("d/live2d/m.moc3")
    assert p.read_bytes() == b"m" * 100 and s.store in p.parents
    calls.clear()
    assert s.lazy("d/live2d/m.moc3") == p and calls == []       # second view: from disk
    assert s.lazy("d/nope.moc3") is None
    assert s.known("d/live2d/n.moc3") and not s.known("d/nope.moc3")


def test_cache_cap_evicts_the_oldest_and_zero_is_unlimited(tmp_path):
    fetch, _ = server(FILES)
    s = cst.ContentStore(tmp_path, verify=False, fetch=fetch, cap=150)
    s.sync()
    first = s.lazy("d/live2d/m.moc3")
    second = s.lazy("d/live2d/n.moc3")
    assert second.is_file() and not first.exists()              # 200 B > 150 B cap: oldest out
    s0 = cst.ContentStore(tmp_path / "z", verify=False, fetch=fetch, cap=0)   # the default: no cap
    s0.sync()
    a, b = s0.lazy("d/live2d/m.moc3"), s0.lazy("d/live2d/n.moc3")
    assert a.is_file() and b.is_file()                          # nothing evicted


def test_prefetch_fills_the_cache_once_and_only_when_unlimited(tmp_path):
    fetch, calls = server(FILES)
    s = cst.ContentStore(tmp_path, verify=False, fetch=fetch, cap=0)
    s.sync()
    calls.clear()
    assert s.prefetch(("d/live2d/",)) == {"fetched": 2, "of": 2}
    assert len(calls) == 2
    assert s.prefetch(("d/live2d/",)) == {"fetched": 0, "of": 0}  # already cached: no request
    capped = cst.ContentStore(tmp_path / "c", verify=False, fetch=fetch, cap=150)
    capped.sync()
    assert capped.prefetch(("d/live2d/",))["fetched"] == 0


def test_the_publisher_never_syncs_over_its_own_files(tmp_path):
    fetch, calls = server(FILES)
    mark = tmp_path / cst.PUBLISHER_MARK
    mark.parent.mkdir(parents=True)
    mark.write_text("{}")
    with pytest.raises(RuntimeError, match="publishes"):
        cst.ContentStore(tmp_path, verify=False, fetch=fetch).sync()
    assert calls == []


def test_a_corrupt_object_is_never_written(tmp_path):
    fetch, _ = server(FILES)

    def bad(route: str) -> bytes:
        body = fetch(route)
        return body if route == "manifest.json" else body + b"!"
    s = cst.ContentStore(tmp_path, verify=False, fetch=bad)
    with pytest.raises(ValueError):
        s.sync()
    assert not (tmp_path / "d/charts.json").exists()


def test_an_unsigned_or_altered_manifest_is_refused(tmp_path, monkeypatch):
    """The server is public and untrusted: only the publisher's signature counts (signing.py)."""
    from Crypto.PublicKey import ECC
    import signing
    key = ECC.generate(curve="ed25519")
    pem = tmp_path / "k.pem"
    pem.write_text(key.export_key(format="PEM"))
    monkeypatch.setattr(signing, "RELEASE_PUBKEY", key.public_key().export_key(format="raw").hex())
    fetch, _ = server(FILES)
    body = fetch("manifest.json")
    good = signing.sign(body, pem).encode()

    def serve(sig, manifest=body):
        return lambda route: {"manifest.json": manifest, "manifest.sig": sig}.get(route) or fetch(route)
    assert cst.ContentStore(tmp_path / "a", fetch=serve(good)).sync()["files"] == 2
    with pytest.raises(signing.BadSignature):
        cst.ContentStore(tmp_path / "b", fetch=serve(b"AAAA")).sync()
    with pytest.raises(signing.BadSignature):
        cst.ContentStore(tmp_path / "c", fetch=serve(good, body.replace(b"m1", b"m2"))).sync()
    assert not (tmp_path / "c" / "d" / "charts.json").exists()


def test_an_older_manifest_is_refused(tmp_path):
    """A replay of a past (signed) manifest never takes this PC's content back:
    the manifest id is its publish time."""
    fetch, _ = server(FILES)
    s = cst.ContentStore(tmp_path, verify=False, fetch=fetch)
    s.sync()                                            # holds manifest "m1"
    older = json.dumps({"id": "m0", "files": {}}).encode()
    s2 = cst.ContentStore(tmp_path, verify=False,
                          fetch=lambda r: older if r == "manifest.json" else fetch(r))
    with pytest.raises(OSError, match="older"):
        s2.sync()
    assert (tmp_path / "d" / "charts.json").read_bytes() == b"charts"


def test_every_request_names_the_app():
    """The server sits behind Cloudflare, whose browser checks answer Python's own agent with
    403: every request names holobuilder."""
    assert cst.user_agent().startswith("holobuilder/")
    assert cst.version_header()["User-Agent"] == cst.user_agent()
