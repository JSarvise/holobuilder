"""Golden for the refresh: the offline rebuilds, byte for byte, on the frozen snapshot.

Runs ``refresh_all.py --offline`` inside a sandbox (code under test + frozen inputs) and hashes
every file under ``data/``, ``state/`` and ``tools/recon/proxy/dumps/`` afterwards. Two runs of
the same code must give the same hashes; a refactor must give the same hashes as the baseline.

Only the steps that read nothing but the snapshot run: the ones that read the game install's
own asset cache (catalogue, icons, UI sprites) follow the installed client, not the snapshot.

    python tests/golden/refresh_offline.py run  --frozen DIR --out DIR/refresh_x.json
    python tests/golden/refresh_offline.py diff A.json B.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sandbox  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
STEPS = (
    "event_bonus",
    "cards",
    "level_curve",
    "caps",
    "import",
    "add_cards",
    "event_history",
    "songs",
    "chart_lib",
    "node_master",
    "skilltree",
    "board",
    "board_export",
)
HASHED = ("data", "state", "tools/recon/proxy/dumps")
# What those steps write. They are deleted first, so each one has to be rebuilt (a step that
# stopped writing would otherwise leave the frozen copy in place and still match). Additive
# files (event_history.json) and the append-only catalogue stay: their steps read them.
DERIVED = tuple(
    "tools/recon/proxy/dumps/" + n
    for n in (
        "marathon_bonus.json",
        "cards_enriched.json",
        "card_level_curve.json",
        "card_caps.json",
        "cards_review.json",
        "cards_import_draft.json",
        "skill_ladder.json",
        "songs.json",
        "lattice_charts.json",
        "music_coef.json",
        "charts.json",
        "charts_all.json",
        "master_SkillTreeNode.json",
        "master_SkillTreeEffect.json",
        "skilltree.json",
        "board.json",
        "board_export.json",
    )
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hashes(root: Path) -> dict[str, str]:
    out = {}
    for top in HASHED:
        for dirpath, dirnames, files in os.walk(root / top):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for name in sorted(files):
                p = Path(dirpath) / name
                out[p.relative_to(root).as_posix()] = _sha(p)
    return out


def run(code: Path, frozen: Path, out: Path) -> dict:
    box = sandbox.build(code, frozen)
    try:
        before = hashes(box)
        for rel in DERIVED:
            (box / rel).unlink()
        audit = out.with_suffix(".audit.tsv")
        for stale in audit.parent.glob(audit.name + ".*"):
            stale.unlink()
        env = sandbox.env(box, frozen, audit_log=audit, workers=1)
        t0 = time.perf_counter()
        proc = subprocess.run(
            [
                sandbox.python_exe(),
                "-u",
                str(box / "tools/refresh/refresh_all.py"),
                "--offline",
                "--only",
                ",".join(STEPS),
            ],
            cwd=box,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        seconds = round(time.perf_counter() - t0, 1)
        out.with_suffix(".log").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr, encoding="utf-8")
        after = hashes(box)
    finally:
        sandbox.remove(box)
    changed = sorted(k for k in after if before.get(k) != after[k])
    not_rebuilt = sorted(rel for rel in DERIVED if rel not in after)
    doc = {
        "returncode": proc.returncode,
        "seconds": seconds,
        "steps": list(STEPS),
        "changed": changed,
        "not_rebuilt": not_rebuilt,
        "hashes": after,
    }
    out.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    return doc


def diff(a: dict, b: dict) -> list[str]:
    """Files whose bytes differ between two runs. A file only one side has that its own run left
    untouched is an input that checkout tracks (e.g. new gacha art), not a refresh result."""
    ha, hb = a["hashes"], b["hashes"]
    bad = []
    for k in sorted(set(ha) | set(hb)):
        if ha.get(k) == hb.get(k):
            continue
        if k not in ha and k not in b["changed"] or k not in hb and k not in a["changed"]:
            continue
        bad.append(f"{k}: {'only in A' if k not in hb else 'only in B' if k not in ha else 'bytes differ'}")
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--frozen", type=Path, required=True)
    p.add_argument("--code", type=Path, default=REPO)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--against", type=Path, help="a baseline hashes file to compare with")
    p = sub.add_parser("diff")
    p.add_argument("a", type=Path)
    p.add_argument("b", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "diff":
        bad = diff(json.loads(args.a.read_text("utf-8")), json.loads(args.b.read_text("utf-8")))
        print("identical" if not bad else "\n".join(bad))
        return 1 if bad else 0
    doc = run(args.code, args.frozen, args.out)
    print(
        f"refresh --offline: rc={doc['returncode']} in {doc['seconds']}s, "
        f"{len(DERIVED) - len(doc['not_rebuilt'])}/{len(DERIVED)} outputs rebuilt, "
        f"{len(doc['changed'])} differ from the frozen copy"
        + (f"; NOT REBUILT: {doc['not_rebuilt']}" if doc["not_rebuilt"] else "")
    )
    if args.against:
        bad = diff(json.loads(args.against.read_text("utf-8")), doc)
        print("REFRESH GOLDEN: identical" if not bad else "REFRESH GOLDEN: DIFFERS\n" + "\n".join(bad))
        return 1 if bad else 0
    return 0 if doc["returncode"] == 0 and not doc["not_rebuilt"] else 1


if __name__ == "__main__":
    sys.exit(main())
