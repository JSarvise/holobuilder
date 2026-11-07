"""octo_fetch takes a bundle from the game's own cache when its md5 matches the catalogue's,
and goes to the CDN only for what the cache lacks (on a typical refresh most bundles, about
920 of 1,028, are already on disk byte-identical)."""
from __future__ import annotations

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import octo_fetch  # noqa: E402


def test_local_cache_first_cdn_only_for_the_rest(tmp_path, monkeypatch):
    good, partial = b"UnityFS\0good bundle", b"UnityFS\0trunc"
    md5 = {n: hashlib.md5(b).hexdigest() for n, b in (("a", good), ("b", good + b"!"), ("c", b"x"))}
    cache = tmp_path / "cache" / "ab"
    cache.mkdir(parents=True)
    (cache / md5["a"]).write_bytes(good)
    (cache / md5["b"]).write_bytes(partial)          # named right, wrong bytes: not trusted
    index = {n: (f"tok-{n}", m) for n, m in md5.items()}
    monkeypatch.setattr(octo_fetch, "catalog", lambda: ("https://cdn/{o}", index, "test"))
    monkeypatch.setattr(octo_fetch, "local_cache_index",
                        lambda: {p.name: str(p) for p in cache.iterdir()})
    monkeypatch.setattr(octo_fetch, "OUT", str(tmp_path / "out"))
    monkeypatch.setattr(octo_fetch, "DELAY", 0)
    asked = []

    class Resp:
        status_code = 200

        def __init__(self, url):
            self.content = {"https://cdn/tok-b": good + b"!", "https://cdn/tok-c": b"x"}[url]

    def get(url, **_):
        asked.append(url)
        return Resp(url)

    monkeypatch.setattr(octo_fetch.requests, "get", get)
    octo_fetch.fetch(["a", "b", "c"])
    assert asked == ["https://cdn/tok-b", "https://cdn/tok-c"]
    assert (tmp_path / "out" / "a.bundle").read_bytes() == good
    assert (tmp_path / "out" / "b.bundle").read_bytes() == good + b"!"


def test_no_install_means_no_local_cache(monkeypatch):
    monkeypatch.setenv("HB_NO_GAME", "1")
    assert octo_fetch.local_cache_index() == {}
