"""Freeze the inputs the golden runs read into one snapshot directory.

The golden runs never read the live files a refresh rewrites: they read this snapshot, taken in
one pass so the collection, the boards and the dumps all describe the same account at the same
instant. The snapshot lives outside the checkout and is never tracked.

Layout of the snapshot (``--out``):

    inputs/                    overlaid on a copy of the code (same relative paths as the repo)
        data/ ...              the catalogue, owned cards, event and gacha data (no gacha art)
        state/collection.json  the account's card states (no credentials, no client profile)
        tools/recon/proxy/dumps/...   every small derived file and the small data folders
    localappdata/AppData/LocalLow/QualiArts, Inc_/hololive-Dreams/<id>/   the master cache
    master_repo/               the sibling master JSON repo (without .git)
    MANIFEST.json              sha256 + size of every frozen file

Usage:
    python tests/golden/freeze.py --source C:/path/to/live/checkout --out C:/path/to/snapshot
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))  # this checkout's hbuilder first
from hbuilder import paths  # noqa: E402

DUMPS_REL = Path("tools/recon/proxy/dumps")

# Folders under dumps/ that hold game assets (pictures, models, raw caches), not inputs the
# optimizer, the board planner or the event views read. Everything else is copied.
DUMPS_SKIP_DIRS = {
    "octo_fetched", "card_assets", "stamps", "skill_icons", "live2d", "card_art", "v7",
    "chibi", "decomp", "ui_sprites", "next_update_assets", "card_images", "live2d_probe",
    "research_bundle", "research_bundle_2026-08-23", "gacha_art_trial", "member_icons_probe",
    "__no_such_cache__",
}
# Top-level dumps files that are captures or backups, never read by the product.
DUMPS_SKIP_SUFFIXES = (".mitm", ".zip", ".obj", ".html", ".stale")
DUMPS_SKIP_MARKERS = (".before_prune", "_stale_", ".bak", "before_refresh", ".pre_")

STATE_FILES = ("collection.json",)          # never the credential or the client profile
DATA_SKIP_DIRS = {"art"}                    # data/gacha/art: banner pictures


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _walk(root: Path, skip_dirs: set[str]):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs and d != "__pycache__")
        for name in sorted(filenames):
            yield Path(dirpath) / name


def _dumps_file_wanted(path: Path, dumps: Path) -> bool:
    rel = path.relative_to(dumps)
    if len(rel.parts) > 1:
        return True
    name = rel.name
    if name.endswith(DUMPS_SKIP_SUFFIXES):
        return False
    return not any(marker in name for marker in DUMPS_SKIP_MARKERS)


def master_cache_dir() -> Path | None:
    """The game's own data folder (`paths.GAME_DATA`: LocalLow on Windows, inside the Proton
    prefix on Linux), None when this PC has none."""
    return paths.GAME_DATA if paths.GAME_DATA.is_dir() else None


def freeze(source: Path, out: Path, master_repo: Path | None) -> dict:
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty; freeze into a new directory")
    inputs = out / "inputs"
    plan: list[tuple[Path, Path]] = []

    for path in _walk(source / "data", DATA_SKIP_DIRS):
        plan.append((path, inputs / path.relative_to(source)))
    for name in STATE_FILES:
        plan.append((source / "state" / name, inputs / "state" / name))
    dumps = source / DUMPS_REL
    for path in _walk(dumps, DUMPS_SKIP_DIRS):
        if _dumps_file_wanted(path, dumps):
            plan.append((path, inputs / DUMPS_REL / path.relative_to(dumps)))

    cache = master_cache_dir()
    if cache is not None:
        target = out / "localappdata" / "AppData" / "LocalLow" / "QualiArts, Inc_" / "hololive-Dreams"
        for path in _walk(cache, {"logs", "Vuplex.WebView"}):
            if path.parent == cache:      # Player.log and friends
                continue
            plan.append((path, target / path.relative_to(cache)))

    if master_repo is not None and master_repo.is_dir():
        for path in _walk(master_repo, {".git"}):
            plan.append((path, out / "master_repo" / path.relative_to(master_repo)))

    started = time.time()
    manifest: dict[str, dict] = {}
    for src, dst in plan:
        _copy(src, dst)
        manifest[dst.relative_to(out).as_posix()] = {"sha256": _sha256(dst), "bytes": dst.stat().st_size}
    meta = {
        "frozen_at_unix": int(started),
        "source": str(source),
        "files": len(manifest),
        "bytes": sum(v["bytes"] for v in manifest.values()),
        "master_cache": str(cache) if cache else None,
        "master_repo": str(master_repo) if master_repo else None,
    }
    (out / "MANIFEST.json").write_text(json.dumps({"meta": meta, "files": manifest}, indent=1,
                                                  sort_keys=True), encoding="utf-8")
    return meta


def verify(out: Path) -> list[str]:
    """Paths whose bytes no longer match the manifest (the snapshot must never change)."""
    doc = json.loads((out / "MANIFEST.json").read_text(encoding="utf-8"))
    bad = []
    for rel, rec in doc["files"].items():
        path = out / rel
        if not path.is_file() or path.stat().st_size != rec["bytes"] or _sha256(path) != rec["sha256"]:
            bad.append(rel)
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", type=Path, help="the live checkout to copy from")
    ap.add_argument("--out", type=Path, required=True, help="a new, empty snapshot directory")
    ap.add_argument("--master-repo", type=Path, default=None,
                    help="the master JSON repo (default: HB_MASTER_REPO or <source>/../hololive-dreams-master)")
    ap.add_argument("--verify", action="store_true", help="check an existing snapshot against its manifest")
    args = ap.parse_args(argv)
    if args.verify:
        bad = verify(args.out)
        print(f"{len(bad)} changed" + "".join(f"\n  {b}" for b in bad[:50]))
        return 1 if bad else 0
    if args.source is None:
        ap.error("--source is required")
    source = args.source.resolve()
    master = args.master_repo or Path(os.environ.get("HB_MASTER_REPO") or source.parent / "hololive-dreams-master")
    meta = freeze(source, args.out.resolve(), master)
    print(json.dumps(meta, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
