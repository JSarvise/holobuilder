"""Save, read and replay `.hbteam` team files (the format: `tools/web/hbteam.py`).

    python tools/refresh/team_file.py show   TEAM.hbteam
    python tools/refresh/team_file.py replay TEAM.hbteam [--search] [--runs 26 --score 2993069] [--keep]

`show` prints what the file holds. `replay` re-scores it on THIS checkout's code with the file's
account, in a throwaway copy of the checkout (the golden harness' sandbox: offline, its own data
paths; the live checkout is never written), and compares the replayed row detail with the file's
field by field: the same numbers mean the row is reproduced, a difference names the field and
both values. `--search` re-runs the search that ranked the row and says where it lands;
`--runs N --score S` places a score among the best of N runs on the row's exact run law. Exit
status 0 when reproduced, 1 when it differs, 2 when the file is not one.

The writing half of the format (`save`, `replay`'s sandbox) lives here, outside `tools/web/`,
which writes nothing but the collection (`tools/web/test_no_writes.py`).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))  # this checkout's hbuilder first
from hbuilder import paths  # noqa: E402

sys.path.insert(0, str(paths.WEB_DIR))
import hbteam  # noqa: E402


def downloads_dir() -> Path:
    """Where an export is saved: the user's Downloads folder, else the home folder."""
    down = Path.home() / "Downloads"
    return down if down.is_dir() else Path.home()


def notes_csv(notes: Mapping) -> str:
    """A row's note-by-note breakdown (`row_detail.note_breakdown`, the file's
    `detail.notes`) as CSV: a header row, then one row per note."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(notes["columns"])
    writer.writerows(notes["rows"])
    return buf.getvalue()


def save(doc: Mapping, folder: Path | None = None) -> tuple[Path, Path | None]:
    """Write `doc` as a `.hbteam` file in `folder` (default `downloads_dir()`), and its
    note-by-note breakdown beside it as `<same name>.csv` when it has one; never over
    another file (`name (2).hbteam` and so on). Returns both paths."""
    folder = downloads_dir() if folder is None else folder
    folder.mkdir(parents=True, exist_ok=True)
    base = hbteam.file_name(doc)[: -len(hbteam.SUFFIX)]
    n = 1
    while True:
        stem = base if n == 1 else f"{base} ({n})"
        path, csv_path = folder / (stem + hbteam.SUFFIX), folder / (stem + ".csv")
        if not path.exists() and not csv_path.exists():
            break
        n += 1
    with open(path, "xb") as fh:
        fh.write(json.dumps(doc, indent=1, ensure_ascii=False).encode("utf-8"))
    notes = (doc.get("detail") or {}).get("notes")
    if not notes:
        return path, None
    with open(csv_path, "xb") as fh:
        fh.write(notes_csv(notes).encode("utf-8"))
    return path, csv_path


def _sandbox_modules():
    golden = paths.REPO / "tests" / "golden"
    if not (golden / "sandbox.py").is_file():
        raise SystemExit(
            "replay needs a source checkout (tests/golden/sandbox.py): the desktop app does not ship it"
        )
    sys.path.insert(0, str(golden))
    import freeze  # noqa: PLC0415
    import sandbox  # noqa: PLC0415

    return freeze, sandbox


def _write_json(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(doc, indent=1, ensure_ascii=False).encode("utf-8"))


def replay(
    doc_path: Path,
    *,
    search: bool = False,
    runs: int | None = None,
    score: float | None = None,
    keep: bool = False,
    log=print,
) -> dict:
    """Replay a `.hbteam` on this checkout's code with the file's account (module docstring).
    Returns the inner run's result plus `differences` (file detail vs replayed detail)."""
    doc = hbteam.load(doc_path)
    freeze, sandbox = _sandbox_modules()
    work = Path(tempfile.mkdtemp(prefix="hbteam_"))
    box = None
    try:
        t0 = time.perf_counter()
        inputs = work / "frozen" / "inputs"
        plan = freeze.inputs_plan(paths.REPO, inputs)
        for src, dst in plan:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        files = doc["account"].get("files") or {}
        for rel in hbteam.ACCOUNT_FILES:
            target = inputs / rel
            if rel in files:
                _write_json(target, files[rel])
            elif target.exists():
                target.unlink()  # not the exporter's: this install's would leak in
        box = sandbox.build(paths.REPO, work / "frozen")
        log(
            f"sandbox ready in {time.perf_counter() - t0:.0f} s ({len(plan)} data files, "
            f"{len(files)} account files from the file)"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HB_", "HBUILDER_"))}
        env.update(
            {
                "PYTHONPATH": str(box / "tests" / "golden" / "site"),
                "PYTHONUTF8": "1",
                "HB_GOLDEN_OFFLINE": "1",  # never the game's servers
                "HB_GOLDEN_CLOCK": str(float(doc.get("created_unix") or time.time())),
                "HB_MASTER_REPO": str(paths.MASTER_REPO),
                "HB_MASTER_NO_FETCH": "1",
                "HB_CREDENTIAL_FILE": str(box / "state" / "no_credential.json"),
                "OPENBLAS_NUM_THREADS": os.environ.get("OPENBLAS_NUM_THREADS", "1"),
            }
        )
        if os.environ.get("HBUILDER_WORKERS"):
            env["HBUILDER_WORKERS"] = os.environ["HBUILDER_WORKERS"]
        cmd = [
            sys.executable,
            str(box / "tools" / "web" / "hbteam.py"),
            "inside",
            str(Path(doc_path).resolve()),
        ]
        if search:
            cmd.append("--search")
        if runs:
            cmd += ["--runs", str(runs)] + (["--score", str(score)] if score is not None else [])
        t1 = time.perf_counter()
        run = subprocess.run(cmd, cwd=box, env=env, capture_output=True, text=True)
        line = next(
            (ln for ln in reversed(run.stdout.splitlines()) if ln.startswith(hbteam.RESULT_MARK)), None
        )
        if run.returncode != 0 or line is None:
            raise RuntimeError(f"the replay failed ({run.returncode}):\n{run.stderr[-4000:]}")
        got = json.loads(line[len(hbteam.RESULT_MARK) :])
        got["seconds"] = round(time.perf_counter() - t1, 1)
        rounding: list = []
        got["differences"] = hbteam.differences(doc["detail"], got["detail"], rounding=rounding)
        got["rounding"] = rounding
        return got
    finally:
        if keep:
            log(f"kept: {work}" + (f" and {box}" if box else ""))
        else:
            if box is not None:
                sandbox.remove(box)
            shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("show", help="print what the file holds")
    p.add_argument("file", type=Path)
    p = sub.add_parser("replay", help="re-score the file on this install with its account")
    p.add_argument("file", type=Path)
    p.add_argument("--search", action="store_true", help="also re-run the search that ranked it")
    p.add_argument("--runs", type=int, default=None, help="best of N runs on the row's run law")
    p.add_argument("--score", type=float, default=None, help="a score to place among those runs")
    p.add_argument("--keep", action="store_true", help="keep the sandbox (to look inside)")
    p.add_argument("--json", type=Path, default=None, help="also write the replay's result here")
    args = ap.parse_args(argv)

    try:
        doc = hbteam.load(args.file)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(hbteam.summary(doc))
    if args.cmd == "show":
        return 0
    if args.score is not None and not args.runs:
        ap.error("--score needs --runs")
    got = replay(args.file, search=args.search, runs=args.runs, score=args.score, keep=args.keep)
    if args.json:
        _write_json(args.json, got)
    det = got["detail"]
    print(
        f"\nreplayed in {got['seconds']} s on holobuilder {hbteam.app_info().get('version')} "
        f"({hbteam.app_info().get('commit') or ''}): Expected {hbteam._n(det.get('song_est'))} | "
        f"Max {hbteam._n(det.get('song_max'))}"
    )
    here = hbteam.game_info(doc["request"])
    there = doc.get("game") or {}
    if here.get("charts") != there.get("charts") or here.get("client_version") != there.get("client_version"):
        print(
            f"note: the game data differs (file: client {there.get('client_version')}, "
            f"charts {there.get('charts')}; here: client {here.get('client_version')}, "
            f"charts {here.get('charts')})"
        )
    if (doc.get("app") or {}).get("model") != hbteam.app_info().get("model"):
        print(
            f"note: the scoring model differs (file {doc['app'].get('model')}, here "
            f"{hbteam.app_info().get('model')})"
        )
    diffs = got["differences"]
    if not diffs:
        print(
            "REPRODUCED: every field of the file's detail is the same"
            + (
                f" ({len(got['rounding'])} probabilities differ only in their last digits, "
                "the order their sums were added in)"
                if got.get("rounding")
                else ""
            )
        )
    else:
        print(f"DIFFERS in {len(diffs)} field(s)" + (" (first 60)" if len(diffs) >= 60 else "") + ":")
        for d in diffs:
            print("  " + d)
    if got.get("search") is not None:
        s = got["search"]
        print(
            f"\nsearch replayed: the row is #{s['rank']}"
            if s["rank"]
            else "\nsearch replayed: the row is not in the ranking"
        )
        for i, r in enumerate(s["top"], 1):
            print(
                f"  #{i} {hbteam._n(r.get('best'))}  {r.get('leader')} + {', '.join(r.get('members') or [])}"
            )
    if got.get("best_of"):
        b = got["best_of"]
        print(
            f"\nbest of {b['n']}: expected {hbteam._n(b['expected'])} | p50 {hbteam._n(b.get('p50'))} | "
            f"p90 {hbteam._n(b.get('p90'))} | Max {hbteam._n(b.get('max'))}"
        )
        if "run_percentile" in b:
            print(
                f"  {hbteam._n(args.score)}: above {b['run_percentile']:.1%} of single runs, above "
                f"{b['best_percentile']:.1%} of best-of-{b['n']} sessions; "
                f"{b['chance_to_reach']:.1%} chance that {b['n']} runs reach it"
            )
    return 0 if not diffs else 1


if __name__ == "__main__":
    sys.exit(main())
