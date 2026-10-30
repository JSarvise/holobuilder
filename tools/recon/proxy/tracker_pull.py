"""Pull the 24/7 event tracker's reads (`event_tracker.py`, on the VPS) into the local store.

The tracker's API listens on the VPS's 127.0.0.1 only; this opens an SSH tunnel to it (the
operator's key, `ssh -L`), asks `/files`, and fetches each append-only file from the offset it last
reached. Records are MERGED into `dumps/cutoffs/<region>/` -- the store every local reader
already uses -- skipping any whose (captured_at, kind) is already there, so the local poller's
own reads and the tracker's interleave without duplicates. A chapter's `.meta.json` is taken
when the tracker's is newer (`fetched_at`). The tracker's `status.json` lands as
`dumps/cutoffs/tracker_status.json` for the Event page.

Config: `state/tracker_remote.json` = `{"ssh": "root@<host>", "port": 8790}` (gitignored with the
rest of state/). Prints `cutoffs: N appended` like event_cutoffs.py, so the server's poller
(`tools/web/cutoffs_poller.py`) can run either script.

    proxyenv/Scripts/python tracker_pull.py
    proxyenv/Scripts/python tracker_pull.py --watch us   (a viewer: read us's top 100 densely)
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import atomic_io  # noqa: E402

REPO = HERE.parents[2]
CONFIG = REPO / "state" / "tracker_remote.json"
STORE = HERE / "dumps" / "cutoffs"
STATE_FILE = ".pull_state.json"
WATCH_S = 300   # a viewer's lease; the page renews it about every minute while it is open


def load_config(path: Path = CONFIG) -> dict | None:
    try:
        cfg = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return cfg if isinstance(cfg, dict) and cfg.get("ssh") else None


API_CONFIG = REPO / "state" / "server_api.json"   # {"url", "token"}: the desktop app's server


def load_api_config(path: Path = API_CONFIG) -> dict | None:
    try:
        cfg = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return cfg if isinstance(cfg, dict) and cfg.get("url") and cfg.get("token") else None


class Https:
    """The same API through api.holobuilder.dev/tracker/* (Caddy, bearer token): the desktop
    app's route, on a PC with no SSH key for the VPS."""

    def __init__(self, url: str, token: str):
        self.base = url.rstrip("/") + "/tracker"
        # A custom User-Agent: the server is behind Cloudflare, whose browser checks stop Python's
        self.headers = {"Authorization": f"Bearer {token}", "User-Agent": "holobuilder-tracker-pull"}

    def get(self, route: str, **q) -> bytes:
        url = self.base + route + ("?" + urllib.parse.urlencode(q) if q else "")
        req = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 (our own https host)
            return r.read()

    def post(self, route: str, **q) -> bytes:
        req = urllib.request.Request(self.base + route + "?" + urllib.parse.urlencode(q),
                                     data=b"", method="POST", headers=self.headers)
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
            return r.read()

    def close(self) -> None:
        pass


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Tunnel:
    def __init__(self, ssh: str, remote_port: int):
        self.local = _free_port()
        self.proc = subprocess.Popen(  # noqa: S603
            ["ssh", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
             "-o", "ConnectTimeout=15", "-N",
             "-L", f"127.0.0.1:{self.local}:127.0.0.1:{remote_port}", ssh],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.time() + 25
        while time.time() < deadline:
            if self.proc.poll() is not None:
                err = (self.proc.stderr.read() or b"").decode("utf-8", "replace").strip()
                raise RuntimeError(f"ssh tunnel failed: {err or self.proc.returncode}")
            try:
                socket.create_connection(("127.0.0.1", self.local), timeout=1).close()
                return
            except OSError:
                time.sleep(0.3)
        self.close()
        raise RuntimeError("ssh tunnel did not come up in 25 s")

    def get(self, route: str, **q) -> bytes:
        url = f"http://127.0.0.1:{self.local}{route}"
        if q:
            url += "?" + urllib.parse.urlencode(q)
        with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 (local tunnel)
            return r.read()

    def post(self, route: str, **q) -> bytes:
        url = f"http://127.0.0.1:{self.local}{route}?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (local tunnel)
            return r.read()

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def _key(rec: dict) -> tuple:
    return (rec.get("captured_at"), rec.get("kind"))


def merge_lines(local: Path, lines: list[bytes]) -> int:
    """Append the records of `lines` whose (captured_at, kind) `local` lacks. Returns the count."""
    have: set = set()
    if local.is_file():
        for ln in local.read_bytes().splitlines():
            try:
                have.add(_key(json.loads(ln)))
            except ValueError:
                continue
    new = []
    for ln in lines:
        try:
            k = _key(json.loads(ln))
        except ValueError:
            continue
        if k not in have:
            have.add(k)
            new.append(ln.rstrip(b"\r\n") + b"\n")
    if new:
        local.parent.mkdir(parents=True, exist_ok=True)
        with local.open("ab") as fh:
            fh.write(b"".join(new))
    return len(new)


def merge_meta(local: Path, blob: bytes) -> bool:
    try:
        remote = json.loads(blob)
    except ValueError:
        return False
    try:
        mine = json.loads(local.read_text("utf-8"))
    except (OSError, ValueError):
        mine = None
    if mine and int(mine.get("fetched_at") or 0) >= int(remote.get("fetched_at") or 0):
        return False
    local.parent.mkdir(parents=True, exist_ok=True)
    atomic_io.write_bytes(local, blob)
    return True


def pull(get, store: Path = STORE) -> dict:
    """One pass over the tracker's files with `get(route, **query) -> bytes`."""
    state_path = store / STATE_FILE
    try:
        offsets = json.loads(state_path.read_text("utf-8"))
    except (OSError, ValueError):
        offsets = {}
    files = json.loads(get("/files"))["files"]
    appended = metas = 0
    for f in files:
        rel, size = f["path"], int(f["size"])
        if rel == "status.json":
            atomic_io.write_bytes(store / "tracker_status.json", get("/file", path=rel))
            continue
        if "/" not in rel:
            continue
        local = store / rel
        if rel.endswith(".meta.json"):
            if offsets.get(rel) != f.get("mtime"):
                metas += merge_meta(local, get("/file", path=rel))
                offsets[rel] = f.get("mtime")
            continue
        if not rel.endswith(".jsonl"):
            continue
        off = int(offsets.get(rel, 0))
        if size < off:       # replaced on the remote: start over (the merge skips duplicates)
            off = 0
        if size == off:
            continue
        data = get("/file", path=rel, offset=off)
        end = data.rfind(b"\n") + 1        # only whole lines; a torn tail waits for next time
        if end:
            appended += merge_lines(local, data[:end].splitlines())
            offsets[rel] = off + end
    store.mkdir(parents=True, exist_ok=True)
    atomic_io.write_bytes(state_path, json.dumps(offsets, indent=0, sort_keys=True).encode())
    return {"appended": appended, "metas": metas, "files": len(files)}


def top_reads(status: dict, region: str) -> int:
    return next((int(r.get("top_reads") or 0) for r in status.get("regions", [])
                 if r.get("region") == region), 0)


def watch(tunnel: "Tunnel", region: str, seconds: int = WATCH_S, wait_s: float = 25,
          kind: str | None = None) -> bool:
    """Hold (or renew) a viewer's lease on `region`'s top 100, then wait for the read it triggers
    when the lease is new -- so the pull right after brings a fresh board. True when one landed."""
    before = top_reads(json.loads(tunnel.get("/status")), region)
    q = {"region": region, "seconds": seconds, **({"kind": kind} if kind else {})}
    after = top_reads(json.loads(tunnel.post("/watch", **q)), region)
    deadline = time.time() + wait_s
    while after == before and time.time() < deadline:
        time.sleep(2)
        after = top_reads(json.loads(tunnel.get("/status")), region)
    return after != before


def main() -> int:
    region = None
    if "--watch" in sys.argv:
        i = sys.argv.index("--watch")
        region = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
        if region not in ("us", "as", "jp"):
            print("[tracker] FAILED: --watch needs us, as or jp")
            return 2
    # --kind music:<id>: the lease covers that song's board alone (the main board otherwise)
    kind = sys.argv[sys.argv.index("--kind") + 1] if "--kind" in sys.argv[:-1] else None
    cfg, api = load_config(), load_api_config()
    if not cfg and not api:
        print(f"[tracker] FAILED: no {CONFIG.relative_to(REPO)} (ssh target) "
              f"and no {API_CONFIG.relative_to(REPO)} (https)")
        return 2
    try:
        # SSH where it is configured (the operator's PC), HTTPS otherwise (the desktop app)
        tunnel = (Tunnel(cfg["ssh"], int(cfg.get("port") or 8790)) if cfg
                  else Https(api["url"], api["token"]))
    except RuntimeError as exc:
        print(f"[tracker] FAILED: {exc}")
        return 1
    try:
        if region:
            fresh = watch(tunnel, region, kind=kind)
            print(f"[{region}] top 100{f' {kind}' if kind else ''} watched"
                  + ("" if fresh else " (no new read yet)"))
        res = pull(tunnel.get)
        status = json.loads(tunnel.get("/status"))
    except Exception as exc:  # noqa: BLE001
        print(f"[tracker] FAILED: {type(exc).__name__}: {exc}")
        return 1
    finally:
        tunnel.close()
    for r in status.get("regions", []):
        ok = (f"ok {int(time.time()) - int(r['ok_at'])}s ago" if r.get("ok_at")
              else "no good read since it started")
        print(f"[{r['region']}] tracker {ok}, reads {r.get('reads')}, "
              f"top {r.get('top_reads')}" + (f", last error: {r['error']}" if r.get("error") else ""))
    print(f"cutoffs: {res['appended']} appended ({res['metas']} meta, {res['files']} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
