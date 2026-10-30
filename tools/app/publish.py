"""Publish the app's shared content from this PC to api.holobuilder.dev.

    proxyenv/Scripts/python tools/app/publish.py            # hash, upload what is new, swap
    proxyenv/Scripts/python tools/app/publish.py --dry-run  # say what it would do, send nothing

The store on the VPS is content-addressed, so a publish only ever ADDS files and the switch to
the new version is one rename:

    /srv/holobuilder/content/objects/<sha[:2]>/<sha>   every file, named by its sha256
    /srv/holobuilder/content/manifests/<id>.json       every manifest ever published
    /srv/holobuilder/content/manifest.json             the current one (written last)

Caddy serves them under /content/ behind the bearer token; objects are immutable, so the app
and the browser can cache them forever. An app that is half-way through a sync keeps reading
the objects its manifest names: nothing it needs is ever overwritten or removed by a publish.

The manifest lists, per repo-relative path: sha256, size and kind ('sync' = downloaded before
the app runs, 'lazy' = fetched when a page asks). What goes in is `content_spec.py`.

Transport: the publisher's SSH key (the same one the tracker deploy uses), a tar stream into
`tar -x` on the VPS -- no rsync on Windows, no server code. Hashes are cached by
(size, mtime) in dumps/.publish_hashes.json, so a re-publish rehashes only what changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import content_spec  # noqa: E402

REMOTE = "/srv/holobuilder/content"
HASH_CACHE = REPO / "tools" / "recon" / "proxy" / "dumps" / ".publish_hashes.json"
REMOTE_CONF = REPO / "state" / "tracker_remote.json"   # {"ssh": "root@<vps>", ...}
MANIFEST_VERSION = 1


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def build_manifest(repo: Path, cache: dict, *, now: float | None = None) -> dict:
    """The manifest for what `content_spec` selects under `repo` (updates `cache` in place)."""
    files = {}
    for rel, kind, path in content_spec.iter_content(repo):
        st = path.stat()
        key = [st.st_size, st.st_mtime_ns]
        hit = cache.get(rel)
        sha = hit["sha256"] if hit and hit.get("key") == key else sha256_file(path)
        cache[rel] = {"key": key, "sha256": sha}
        files[rel] = {"sha256": sha, "size": st.st_size, "kind": kind}
    now = time.time() if now is None else now
    return {
        "version": MANIFEST_VERSION,
        "id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)),
        "created_at": int(now),
        "files": files,
    }


def summary(manifest: dict) -> dict:
    out = {}
    for f in manifest["files"].values():
        k = out.setdefault(f["kind"], {"files": 0, "bytes": 0})
        k["files"] += 1
        k["bytes"] += f["size"]
    return out


def missing_objects(manifest: dict, remote: set[str]) -> dict[str, str]:
    """{sha: repo-relative path} for every object the VPS does not hold yet (one path per sha)."""
    out: dict[str, str] = {}
    for rel, f in manifest["files"].items():
        if f["sha256"] not in remote and f["sha256"] not in out:
            out[f["sha256"]] = rel
    return out


def object_name(sha: str) -> str:
    return f"objects/{sha[:2]}/{sha}"


def write_tar(stream, repo: Path, todo: dict[str, str], progress=None) -> None:
    """Every missing object into an uncompressed tar on `stream`, named objects/<aa>/<sha>.

    Uncompressed: most bytes are webp/png/moc3 that do not shrink, and gzip would make the
    upload CPU-bound on a fast line."""
    with tarfile.open(fileobj=stream, mode="w|") as tar:
        for i, (sha, rel) in enumerate(sorted(todo.items(), key=lambda kv: kv[1])):
            path = repo / rel
            info = tarfile.TarInfo(object_name(sha))
            info.size = path.stat().st_size
            info.mtime = int(time.time())
            info.mode = 0o644
            with open(path, "rb") as f:
                tar.addfile(info, f)
            if progress:
                progress(i + 1, len(todo), info.size)


# ------------------------------------------------------------------ remote side (ssh)

def ssh_target() -> str:
    env = os.environ.get("HB_PUBLISH_SSH")
    if env:
        return env
    return json.loads(REMOTE_CONF.read_text("utf8"))["ssh"]


def ssh(target: str, command: str, *, data: bytes | None = None) -> bytes:
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", target, command], input=data,
                       capture_output=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"ssh {command!r} failed ({r.returncode}): "
                           f"{r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def remote_objects(target: str) -> set[str]:
    out = ssh(target, f"mkdir -p {REMOTE}/objects {REMOTE}/manifests && "
                      f"find {REMOTE}/objects -type f -printf '%f\\n'")
    return {line.strip() for line in out.decode().splitlines() if len(line.strip()) == 64}


def upload(target: str, repo: Path, todo: dict[str, str]) -> None:
    total = sum((repo / rel).stat().st_size for rel in todo.values())
    sent = [0]
    t0 = time.time()

    def progress(i: int, n: int, size: int) -> None:
        sent[0] += size
        if i == n or i % 200 == 0:
            rate = sent[0] / max(time.time() - t0, 1e-3) / 1e6
            print(f"  {i}/{n} files  {sent[0] / 1e6:.0f}/{total / 1e6:.0f} MB  {rate:.1f} MB/s",
                  flush=True)

    proc = subprocess.Popen(["ssh", "-o", "BatchMode=yes", target,
                             f"cd {REMOTE} && tar -xf - && echo extracted"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        write_tar(proc.stdin, repo, todo, progress)
    finally:
        proc.stdin.close()
    out, err = proc.communicate()
    if proc.returncode != 0 or b"extracted" not in out:
        raise RuntimeError(f"upload failed ({proc.returncode}): {err.decode(errors='replace')}")


def swap_manifest(target: str, manifest: dict) -> None:
    body = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    mid = manifest["id"]
    import signing  # noqa: PLC0415 -- the publisher's private key (state/, never in git)
    ssh(target, f"cd {REMOTE} && cat > .manifest.sig.tmp", data=signing.sign(body).encode())
    ssh(target, f"cd {REMOTE} && cat > manifests/{mid}.json && "
                f"cp manifests/{mid}.json .manifest.json.tmp && mv .manifest.sig.tmp manifest.sig && "
                f"mv .manifest.json.tmp manifest.json",
        data=body)


def write_client_version() -> None:
    """dumps/client_version.json from this PC's game install, for the app's login (it has no
    install to read). Left as it is when the install cannot be read: never a guess."""
    sys.path.insert(0, str(REPO / "tools" / "recon" / "proxy"))
    import account_snapshot  # noqa: PLC0415 -- recon-side, only on the publisher
    import atomic_io  # noqa: PLC0415
    v = account_snapshot.app_version()
    if v:
        atomic_io.write_text(account_snapshot.CLIENT_VERSION_FILE,
                             json.dumps({"version": v, "source": "publisher's install"}), "utf8")
        print(f"game client version {v}")
    else:
        print("game client version: install not readable, client_version.json left as it is")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="hash and compare; send nothing")
    args = ap.parse_args(argv)

    write_client_version()
    cache = json.loads(HASH_CACHE.read_text("utf8")) if HASH_CACHE.is_file() else {}
    t0 = time.time()
    manifest = build_manifest(REPO, cache)
    HASH_CACHE.write_text(json.dumps(cache), "utf8")
    for kind, s in sorted(summary(manifest).items()):
        print(f"{kind:5s} {s['files']:6d} files  {s['bytes'] / 1e6:8.1f} MB")
    print(f"hashed in {time.time() - t0:.1f} s on {socket.gethostname()}")

    target = ssh_target()
    todo = missing_objects(manifest, remote_objects(target))
    new_bytes = sum(manifest["files"][rel]["size"] for rel in todo.values())
    print(f"new on the server: {len(todo)} objects, {new_bytes / 1e6:.1f} MB")
    if args.dry_run:
        print("dry run: nothing sent")
        return 0
    if todo:
        upload(target, REPO, todo)
        still = missing_objects(manifest, remote_objects(target))
        if still:
            print(f"ERROR: {len(still)} objects still missing after the upload; manifest NOT "
                  "switched", file=sys.stderr)
            return 1
    swap_manifest(target, manifest)
    print(f"published manifest {manifest['id']} ({len(manifest['files'])} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
