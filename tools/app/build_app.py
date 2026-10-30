"""Build the desktop app as one self-contained folder.

    proxyenv/Scripts/python tools/app/build_app.py 0.1.0             # build/holobuilder/ + zip
    proxyenv/Scripts/python tools/app/build_app.py 0.1.0 --publish   # ...and put it on the server

    build/holobuilder/
      runtime/   the official embeddable CPython (the exact version proxyenv runs) + the app's
                 packages, pinned to proxyenv's versions -- same interpreter, same speed
      app/       the code: src/, tools/web, tools/app, the recon scripts the refresh runs,
                 data/cards.json, the C kernels' DLLs (gcc -O2, no -march=native: any x64)

Why not PyInstaller: the server and the refresh start their helpers as `[sys.executable, ...]`
(refresh_all, tracker_pull, the search's worker pool). Frozen, sys.executable is the app's exe
and every one of those calls would need rewriting; with a real python.exe they all work as they
are.

NOT in the build: dumps/ (the app syncs it from the server), state/ (this PC's settings and
account), data/owned.json and data/gacha (the account's own records: the app's refresh writes
owned.json; the gacha log is synced by its own button), research notes and captures.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BUILD = REPO / "build"
OUT = BUILD / "holobuilder"
CACHE = REPO / "tools" / "recon" / "proxy" / "dumps" / "build_cache"
PY_VERSION = "3.13.7"
PY_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
REMOTE_APP = "/srv/holobuilder/app"

# Pinned to what proxyenv runs (pip freeze); pywebview brings pythonnet/bottle/...
PACKAGES = ["pycryptodome==3.23.0", "h2==4.3.0", "hpack==4.2.0", "hyperframe==6.1.0",
            "highspy==1.15.1", "pillow==12.3.0", "pywebview==5.4"]

APP_PATHS = ["..\\app\\src", "..\\app\\tools\\web", "..\\app\\tools\\web\\kernel",
             "..\\app\\tools\\recon\\proxy", "..\\app\\tools\\app"]

INCLUDE = ["src/", "tools/web/", "tools/app/", "tools/explorer/fonts/", "data/cards.json",
           "config.json", "pyproject.toml",
           # the account snapshot's protobuf schema (account_snapshot.PROTO_DIR)
           "tools/recon/protobufs/"]
# read at run time out of the research tree (found with an audit-hook trace): the
# certificates behind the optimizer's "Verified" badge and the per-leader tables (certindex.py)
INCLUDE_RE = re.compile(r"^tools/recon/RESEARCH-2026-08-18/.*(certificate_|per_leader_table_)[^/]*\.json$")
# the recon scripts: the top level of tools/recon/proxy only (no dumps, no venv, no captures)
RECON = "tools/recon/proxy/"
EXCLUDE_PARTS = ("/test_", "/tests/", "__pycache__", "/.pytest_cache/", ".old-", ".bak")
# the publisher's own tools: they run on this PC or the VPS, never inside a user's app (the app
# imports none of them)
# gitignored on purpose (never in the public repo) but the app's refresh needs it: always shipped
PRIVATE_SHIPPED = ("tools/recon/proxy/_game_keys.py",)
PUBLISHER_ONLY = {"tools/app/build_app.py", "tools/app/build_setup.py", "tools/app/publish.py",
                  "tools/app/users.py", "tools/recon/proxy/event_tracker.py"}


def app_files() -> list[str]:
    # tracked AND new-but-not-ignored: a build must not silently drop a file that is simply not
    # committed yet (e.g. a new app.css/tour.js)
    tracked = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             cwd=REPO, capture_output=True, check=True).stdout.decode().split("\0")
    out = []
    for rel in filter(None, tracked):
        # top-level scripts of the recon tree's two script folders; probes/ holds modules the
        # server and the solver load BY PATH (ondemand, event_mode, two_stage_build), which an
        # import scan cannot see
        top_recon = any(rel.startswith(d) and "/" not in rel[len(d):] and rel.endswith(".py")
                        for d in (RECON, "tools/recon/probes/"))
        if not (top_recon or any(rel == p or rel.startswith(p) for p in INCLUDE if p.endswith("/"))
                or rel in INCLUDE or INCLUDE_RE.match(rel)):
            continue
        if (any(x in "/" + rel for x in EXCLUDE_PARTS) or rel.split("/")[-1].startswith("test_")
                or rel in PUBLISHER_ONLY):
            continue
        out.append(rel)
    # built, not tracked: the C kernels (portable flags, see tools/web/kernel/*.py)
    out += [p.relative_to(REPO).as_posix() for p in (REPO / "tools/web/kernel").glob("*.dll")]
    out += [rel for rel in PRIVATE_SHIPPED if (REPO / rel).is_file()]
    return sorted(set(out))


def fetch_python() -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    z = CACHE / PY_URL.rsplit("/", 1)[1]
    if not z.is_file():
        print(f"downloading {PY_URL}")
        with urllib.request.urlopen(PY_URL, timeout=120) as r:  # noqa: S310 -- python.org
            z.write_bytes(r.read())
    return z


def build_runtime(dest: Path) -> None:
    with zipfile.ZipFile(fetch_python()) as zf:
        zf.extractall(dest)
    pth = next(dest.glob("python*._pth"))
    # The ._pth keeps the runtime ISOLATED: without it CPython also finds the PC's own Python
    # through the registry and mixes its Lib in. The
    # price: a ._pth means no script directory on sys.path and no PYTHONPATH, and the project's
    # scripts import their neighbours -- so every script directory is listed here instead
    # (checked: no module name appears in two of them).
    pth.write_text("\n".join([next(dest.glob("python*.zip")).name, ".", "Lib\\site-packages",
                              *APP_PATHS, "import site", ""]), "utf8")
    site = dest / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    # proxyenv IS the target platform (CPython 3.13, win_amd64), so a plain --target install
    # picks the same wheels; proxy_tools (a pywebview dependency) ships as an sdist only
    if sys.version_info[:2] != tuple(int(x) for x in PY_VERSION.split(".")[:2]):
        raise SystemExit(f"build with Python {PY_VERSION} (proxyenv): the wheels must match the runtime")
    subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                    "--no-warn-script-location", "--target", str(site), *PACKAGES], check=True)


def build(version: str) -> Path:
    if OUT.exists():
        shutil.rmtree(OUT)
    t0 = time.time()
    build_runtime(OUT / "runtime")
    files = app_files()
    for rel in files:
        dst = OUT / "app" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, dst)
    (OUT / "app" / "tools" / "app" / "VERSION").write_text(version, "utf8")
    # every holomem's face, small, so a first start shows them before any sync (Settings, the
    # app's icon): 128 px webp from the synced member_icons
    from PIL import Image  # noqa: PLC0415
    faces = OUT / "app" / "tools" / "app" / "ui" / "faces"
    faces.mkdir(parents=True, exist_ok=True)
    for png in sorted((REPO / "tools/recon/proxy/dumps/member_icons").glob("chr-*.png")):
        with Image.open(png) as im:
            im = im.convert("RGBA")
            im.thumbnail((128, 128))
            im.save(faces / f"{png.stem}.webp", quality=88, method=6)
    # the shortcut's icon until the app writes the chosen holomem's (Nene by default)
    sys.path.insert(0, str(HERE))
    import launcher  # noqa: PLC0415
    ico = launcher.icon_file("chr-00031")
    if ico:
        shutil.copy2(ico, OUT / "app" / "tools" / "app" / "ui" / "app.ico")
    zpath = BUILD / f"holobuilder-{version}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in sorted(OUT.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                zf.write(p, p.relative_to(OUT).as_posix())
    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"built {OUT} ({len(files)} app files, {size / 1e6:.0f} MB) in {time.time() - t0:.0f} s")
    print(f"zip   {zpath} ({zpath.stat().st_size / 1e6:.0f} MB)")
    return zpath


CHANGELOG = REPO / "tools" / "oss" / "public" / "CHANGELOG.md"
# The oldest holobuilder-setup.exe still fit to install (installer.SETUP_VERSION): raise it when a
# change makes an old exe install wrong; the signed version.json carries it and an older exe
# then points at the new release instead of installing.
SETUP_MIN = "1.0.0"


def changelog(version: str, notes: str) -> None:
    """The public CHANGELOG gets the published version on top, with the same note the app's
    update banner shows: one source, so they never disagree."""
    if not CHANGELOG.is_file() or f"## {version} " in CHANGELOG.read_text("utf8"):
        return
    text = CHANGELOG.read_text("utf8")
    head, _, rest = text.partition("\n## ")
    entry = f"## {version} ({time.strftime('%Y-%m-%d')})\n- {notes or 'Maintenance release.'}\n"
    CHANGELOG.write_text(head.rstrip("\n") + "\n\n" + entry + ("\n## " + rest if rest else ""), "utf8")


def publish(zpath: Path, version: str, min_version: str | None, notes: str) -> None:
    sys.path.insert(0, str(HERE))
    import publish as pub  # noqa: PLC0415
    target = pub.ssh_target()
    sha = hashlib.sha256(zpath.read_bytes()).hexdigest()
    meta = {"latest": version, "min_version": min_version or version, "notes": notes,
            "setup_min": SETUP_MIN,
            "url": f"app/{zpath.name}", "sha256": sha, "size": zpath.stat().st_size,
            "published_at": int(time.time())}
    import signing  # noqa: PLC0415
    meta["sig"] = signing.sign(signing.canonical(meta))
    subprocess.run(["scp", "-q", str(zpath), f"{target}:{REMOTE_APP}/{zpath.name}"], check=True)
    pub.ssh(target, f"cd {REMOTE_APP} && cat > .version.json.tmp && mv .version.json.tmp version.json",
            data=json.dumps(meta, indent=1).encode())
    print(f"published {version} -> {REMOTE_APP}/{zpath.name} (sha256 {sha[:12]}...)")
    changelog(version, notes)



# Updates are mandatory, so a broken release would stop every user: a
# --publish first runs these tests (the app, the pages' JS, the per-song scorer; ~10 s) and,
# after the build, starts the BUILT app's own server and asks for its pages. Either failing
# stops the publish; nothing reaches the server.
GATE_TESTS = ("tools/app", "tools/web/test_js_syntax.py", "tests/test_song_rank.py",
              "tests/test_score_song.py", "tests/test_gates.py")
SMOKE_PAGES = ("/optimizer", "/event", "/hb-shell.js")


def gate_tests() -> None:
    t0 = time.time()
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *GATE_TESTS],
                         cwd=str(REPO), capture_output=True, text=True)
    tail = (run.stdout or "").strip().splitlines()[-1:] or ["(no output)"]
    if run.returncode != 0:
        print(run.stdout[-3000:], run.stderr[-2000:], sep="\n")
        raise SystemExit(f"gate tests FAILED ({tail[0]}): nothing published")
    print(f"tests {tail[0]} ({time.time() - t0:.0f} s)")


def smoke(port: int = 8799, timeout_s: float = 90.0) -> None:
    """Start the built app's server with its own runtime, as the launcher does, and require
    every SMOKE_PAGES page to answer 200."""
    py = OUT / "runtime" / "python.exe"
    log = BUILD / "smoke.log"
    with open(log, "wb") as out:
        proc = subprocess.Popen([str(py), "-u", str(OUT / "app" / "tools" / "web" / "server.py"),
                                 "--port", str(port)], cwd=str(OUT / "app"), stdin=subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            deadline, base = time.time() + timeout_s, f"http://127.0.0.1:{port}"
            while True:
                if proc.poll() is not None:
                    raise SystemExit(f"smoke: the built app's server stopped at start (see {log}): nothing published")
                try:
                    with urllib.request.urlopen(base + SMOKE_PAGES[0], timeout=2):
                        break
                except OSError:
                    if time.time() > deadline:
                        raise SystemExit(f"smoke: no answer in {timeout_s:.0f} s (see {log}): nothing published")
                    time.sleep(0.5)
            for page in SMOKE_PAGES:
                with urllib.request.urlopen(base + page, timeout=10) as r:
                    if r.status != 200:
                        raise SystemExit(f"smoke: {page} answered {r.status}: nothing published")
            print(f"smoke: the built app served {', '.join(SMOKE_PAGES)}")
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("version", help="X.Y.Z")
    ap.add_argument("--publish", action="store_true", help="upload and make it the latest")
    ap.add_argument("--min-version", default=None,
                    help="older apps must update (default: this version only for a breaking change "
                         "-- pass the previous one to make it optional)")
    ap.add_argument("--notes", default="", help="one line shown in the update banner")
    args = ap.parse_args(argv)
    if args.publish:
        gate_tests()
    z = build(args.version)
    if args.publish:
        smoke()
        publish(z, args.version, args.min_version, args.notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
