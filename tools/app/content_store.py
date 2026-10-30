"""The app's side of the shared content: sync and lazy files.

On the publisher PC every file is local and nothing here ever touches the network: `lazy()`
returns the local path. On an installed app:

  - `sync()` brings every 'sync' file of the server's manifest to its repo-relative path,
    verified by sha256 and written atomically, with a progress callback for the installer
    and the startup screen. It never deletes anything.
  - `lazy(rel)` answers a page's request for a Live2D figure, a chibi or card art: the local
    file if there is one, else the object from api.holobuilder.dev, verified and kept in a
    cache, unlimited by default (cap 0), else capped with the
    oldest out first. `prefetch` fills it in the background (the card art).

Lives in tools/app, not tools/web, because it writes files and tools/web may not
(`tools/web/test_no_writes.py`); the web server only calls it.

Config: state/server_api.json {"url", "token"} (gitignored, written by the app's Settings).
The token never reaches the browser: the page asks the local server, the local server asks
the VPS.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CONF = REPO / "state" / "server_api.json"
STORE = REPO / "tools" / "recon" / "proxy" / "dumps" / ".content"
PUBLISHER_MARK = "tools/recon/proxy/dumps/.publish_hashes.json"   # publish.py's hash cache
DEFAULT_CAP = 0                     # unlimited
TIMEOUT_S = 30
SYNC_THREADS = 8

Fetch = Callable[[str], bytes]   # route under /content/ -> body


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "wb") as f:
        f.write(body)
    os.replace(tmp, path)


def _version() -> str:
    try:
        return (Path(__file__).resolve().parent / "VERSION").read_text("utf8").strip()[:20]
    except OSError:
        return ""


def user_agent() -> str:
    """`holobuilder/<version>` (`dev` in a checkout). The server sits behind Cloudflare, and a
    request with Python's own agent ("Python-urllib", or none from http.client) is one its
    browser checks are built to stop (403 without the zone's configuration rule)."""
    return f"holobuilder/{_version() or 'dev'}"


def version_header() -> dict:
    """`User-Agent` (`user_agent`) and `X-HB-App: <version>` -- what the server's auth notes as
    the user's app on their next request (hb_auth `seen`), so the admin's Users page says
    the app's version, not the installer's 'setup'. Nothing else about
    the PC is sent."""
    v = _version()
    return {"User-Agent": user_agent(), **({"X-HB-App": v} if v else {})}


def install_user_agent() -> None:
    """Every `urllib.request.urlopen` of this process sends `user_agent()` unless the request
    names its own: called once by each process the app starts (launcher, web server)."""
    opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", user_agent())]
    urllib.request.install_opener(opener)


_LIVE2D_LOCK = threading.Lock()


def fetch_live2d_core(dest: Path, url: str) -> bool:
    """True once Live2D's Cubism Core is at `dest`: downloaded the first time from Live2D's own
    CDN (the official SDK samples load it from there), so the holobuilder server never
    redistributes it. The CDN answers Python's default agent with 403.
    Accepted only if it is Live2D's Core (its licence banner), written atomically; any failure
    leaves the leader drawn as the card art."""
    if dest.is_file():
        return True
    with _LIVE2D_LOCK:
        if dest.is_file():
            return True
        try:
            req = urllib.request.Request(url, headers={"User-Agent": user_agent()})
            with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 -- live2d.com
                body = r.read(4_000_000)
        except OSError as exc:
            print(f"  [live2d] Cubism Core not downloaded: {exc}", flush=True)
            return False
        if not body.lstrip().startswith(b"/**") or b"Live2D Cubism Core" not in body[:200]:
            print("  [live2d] the downloaded file is not Live2D's Cubism Core; not kept", flush=True)
            return False
        _atomic_write(dest, body)
        return True


def http_fetch(conf: dict) -> Fetch:
    """GET /content/<route> with the bearer token, one kept-alive HTTPS connection per thread.

    A fresh connection per file (urllib) takes minutes for ~1,150 files / 115 MB -- the TLS
    handshakes, not the bytes. Kept alive and 8 threads wide the bytes are the cost."""
    url = urllib.parse.urlsplit(conf["url"].rstrip("/") + "/content/")
    headers = {"Authorization": f"Bearer {conf['token']}", **version_header()}
    local = threading.local()

    def get(route: str) -> bytes:
        for attempt in (1, 2):
            conn = getattr(local, "conn", None)
            if conn is None:
                cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
                conn = local.conn = cls(url.netloc, timeout=TIMEOUT_S)
            try:
                conn.request("GET", url.path + route, headers=headers)
                r = conn.getresponse()
                body = r.read()
            except (OSError, http.client.HTTPException):
                conn.close()
                local.conn = None
                if attempt == 2:
                    raise
                continue                           # a dropped keep-alive: reconnect once
            if r.status in (401, 403):
                why = {401: "this PC's access key is not known to the server",
                       403: "access is waiting for an admin's approval, or was withdrawn"}[r.status]
                raise OSError(f"holobuilder server: {why}")
            if r.status != 200:
                raise OSError(f"GET {route}: HTTP {r.status}")
            return body
        raise AssertionError("unreachable")
    return get


class ContentStore:
    def __init__(self, repo: Path = REPO, *, fetch: Fetch | None = None,
                 store: Path | None = None, cap: int | None = None, verify: bool = True) -> None:
        self.verify = verify
        self.repo = repo
        self.store = store or (repo / STORE.relative_to(REPO))
        if cap is None and os.environ.get("HB_CONTENT_CACHE_MB", "").strip().isdigit():
            cap = int(os.environ["HB_CONTENT_CACHE_MB"]) * 1024 * 1024    # the app's Settings
        self.cap = DEFAULT_CAP if cap is None else cap
        if fetch is None and CONF.is_file() and repo == REPO:
            fetch = http_fetch(json.loads(CONF.read_text("utf8")))
        self.fetch = fetch
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}
        self._manifest: dict | None = None

    # ------------------------------------------------------------------ manifest
    @property
    def manifest_path(self) -> Path:
        return self.store / "manifest.json"

    def manifest(self) -> dict:
        """The last manifest `sync()` fetched ({} before the first sync)."""
        if self._manifest is None:
            try:
                self._manifest = json.loads(self.manifest_path.read_text("utf8"))
            except (OSError, ValueError):
                self._manifest = {}
        return self._manifest

    def entry(self, rel: str) -> dict | None:
        return (self.manifest().get("files") or {}).get(rel)

    def known(self, rel: str) -> bool:
        """True when the file is local or the server has it: 'can a page ask for it'."""
        return (self.repo / rel).is_file() or self.entry(rel) is not None

    # ------------------------------------------------------------------ sync
    def sync(self, progress: Callable[[dict], None] | None = None) -> dict:
        """Fetch the server's manifest and every 'sync' file that is missing or differs."""
        if self.fetch is None:
            raise RuntimeError(f"no server configured ({CONF})")
        if (self.repo / PUBLISHER_MARK).is_file():
            # this checkout is where the content comes FROM: syncing would only overwrite
            # files that changed since the last publish with their older published copy
            raise RuntimeError("this PC publishes the content (tools/app/publish.py); "
                               "it never syncs it back")
        raw = self.fetch("manifest.json")
        if self.verify:
            # the server is public and not trusted: only the publisher's signature makes the
            # manifest (and through its sha256s, every object) acceptable (signing.py)
            import signing  # noqa: PLC0415
            signing.verify(raw, self.fetch("manifest.sig").decode())
        manifest = json.loads(raw)
        # never back: a manifest older than the one this PC holds (its id is the publish time,
        # YYYYMMDDTHHMMSSZ) is a replay of a past signed one, not new content
        held = self.manifest().get("id")
        if held and manifest.get("id") and str(manifest["id"]) < str(held):
            raise OSError(f"the server's content ({manifest['id']}) is older than this PC's ({held})")
        todo = []
        for rel, f in sorted(manifest["files"].items()):
            if f["kind"] != "sync":
                continue
            p = self.repo / rel
            if p.is_file() and p.stat().st_size == f["size"] and _sha(p.read_bytes()) == f["sha256"]:
                continue
            todo.append((rel, f))
        total = sum(f["size"] for _, f in todo)
        done = 0

        def one(rel: str, f: dict) -> str:
            _atomic_write(self.repo / rel, self._object(f["sha256"]))
            return rel
        with ThreadPoolExecutor(SYNC_THREADS) as pool:
            futures = {pool.submit(one, rel, f): f for rel, f in todo}
            for i, fut in enumerate(as_completed(futures)):
                rel = fut.result()                 # a failed file fails the whole sync
                done += futures[fut]["size"]
                if progress:
                    progress({"file": rel, "i": i + 1, "n": len(todo), "bytes": done,
                              "total": total})
        _atomic_write(self.manifest_path, json.dumps(manifest).encode())
        self._manifest = manifest
        return {"manifest": manifest.get("id"), "files": len(todo), "bytes": total}

    # ------------------------------------------------------------------ lazy
    def lazy(self, rel: str) -> Path | bytes | None:
        """A local Path, the cached object's Path, or None (bytes only after a lost race)."""
        local = self.repo / rel
        if local.is_file():
            return local
        f = self.entry(rel)
        if f is None or self.fetch is None:
            return None
        sha = f["sha256"]
        cached = self.store / "objects" / sha[:2] / sha
        if cached.is_file():
            os.utime(cached)                       # most recently used
            return cached
        with self._lock:
            ev = self._inflight.get(sha)
            mine = ev is None
            if mine:
                ev = self._inflight[sha] = threading.Event()
        if not mine:                               # another request is fetching it
            ev.wait(TIMEOUT_S)
            return cached if cached.is_file() else self.lazy_uncached(sha)
        try:
            body = self._object(sha)
            _atomic_write(cached, body)
            if self.cap > 0:                       # 0 = unlimited: everything seen stays
                self._evict(keep=cached)
            return cached
        except Exception:
            return None
        finally:
            with self._lock:
                self._inflight.pop(sha, None)
            ev.set()

    def prefetch(self, prefixes: tuple[str, ...], *, threads: int = 4,
                 stop: threading.Event | None = None) -> dict:
        """Bring every 'lazy' object under `prefixes` into the cache, in the background, so a
        page never waits for it: the card art Collection shows, so every card's art is there
        when the page opens. Skips what is local or cached; only with an
        unlimited cache, since a capped one would evict what it just fetched."""
        if self.fetch is None or self.cap > 0:
            return {"fetched": 0, "skipped": "no server" if self.fetch is None else "capped cache"}
        todo = [rel for rel, f in sorted((self.manifest().get("files") or {}).items())
                if f["kind"] == "lazy" and rel.startswith(prefixes)
                and not (self.repo / rel).is_file()
                and not (self.store / "objects" / f["sha256"][:2] / f["sha256"]).is_file()]
        done = 0

        def one(rel: str) -> None:
            nonlocal done
            if stop is not None and stop.is_set():
                return
            if isinstance(self.lazy(rel), Path):
                done += 1
        with ThreadPoolExecutor(threads) as pool:
            list(pool.map(one, todo))
        return {"fetched": done, "of": len(todo)}

    def lazy_uncached(self, sha: str) -> bytes | None:
        try:
            return self._object(sha)
        except Exception:
            return None

    def _object(self, sha: str) -> bytes:
        body = self.fetch(f"objects/{sha[:2]}/{sha}")
        if _sha(body) != sha:
            raise ValueError(f"object {sha} arrived corrupt")
        return body

    def _evict(self, keep: Path) -> None:
        """Oldest first until under the cap; never the object just served (two writes can
        share one mtime tick, and evicting what the page is about to read would be absurd)."""
        objs = [p for p in (self.store / "objects").glob("*/*") if p.is_file()]
        total = sum(p.stat().st_size for p in objs)
        for p in sorted(objs, key=lambda p: p.stat().st_mtime):
            if total <= self.cap:
                break
            if p == keep:
                continue
            total -= p.stat().st_size
            p.unlink(missing_ok=True)


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
