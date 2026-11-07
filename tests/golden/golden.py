"""Golden outputs: the web server's answers on a frozen account, compared exactly.

Each search runs in a fresh server (the search engine is cached and seeds the next search from
the previous one, so cases must not share a process). Requests mirror what the pages send; the
answers are canonical JSON with the run-dependent fields (timings, job ids, stamps) removed.

    python tests/golden/golden.py record  --frozen DIR --out DIR/expected
    python tests/golden/golden.py compare --frozen DIR --expected DIR/expected
    python tests/golden/golden.py run     --frozen DIR --out DIR/run_x   [--workers N]
    python tests/golden/golden.py diff    DIR_A DIR_B
    python tests/golden/golden.py audit   --frozen DIR --run DIR/run_x

``--code`` (default: this checkout) is the code under test. The snapshot comes from
``freeze.py``; every run also leaves ``timings.json`` (wall-clock per case and per stage) and
the server's file-access log.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sandbox  # noqa: E402

REPO = Path(__file__).resolve().parents[2]

# --------------------------------------------------------------------------- the sample
SONG = "m0342"                       # an EXPERT chart of the frozen event's song list
EVENT = "marathon_event-normal-008-chapter-01"
PLAN = {"level": "account", "bloom": "account", "keep_higher": False, "sp_training": "account",
        "all_invited": False, "no_refund": False, "branches": "GBRY", "targets": [],
        "boards": "plan", "max_points": False, "infinite": False, "max_resets": None,
        "reset_min_gain": 0.01}
PLAY = {"difficulty": "EXPERT", "platform": "mobile", "auto": False}

# Fields that differ between two runs of the same code on the same input. `plan_optimal` is not
# compared but asserted (`plan_checks`): every exact board plan must have solved to optimality.
STRIP_KEYS = {"seconds", "timings", "written", "elapsed_ms", "elapsed_s", "stage_s", "eta_s",
              "eta_basis", "job_id", "seeded", "polish_seconds", "plan_optimal"}
# the requests whose answers rest on an exact plan within the account's materials
PLAN_ROUTES = ("/api/row-detail", "/api/board-plan-account")


def _search_song_account():
    return {"song": SONG, "basis": "account", **PLAY, "assumptions": PLAN}


def _search_song_maxed():
    return {"song": SONG, "basis": "maxed", **PLAY}


def _search_all_account():
    return {"song": "", "basis": "account", **PLAY, "assumptions": PLAN}


def _search_all_maxed():
    return {"song": "", "basis": "maxed", **PLAY}


def _event_run(song: str, basis: str):
    body = {"objective": "event_points", "song": song, "difficulty": "EXPERT", "boosts": 3,
            "holopass": True, "top": 5, "certify": False, "basis": basis, "event": EVENT,
            "platform": "mobile", "auto": False}
    if basis == "account":
        body["assumptions"] = PLAN
    return body


# A group = one fresh server. The first step is a job; the follow-ups use its top rows the way
# the pages do (row detail -> board plan, Versus compare of the two best teams).
GROUPS = [
    ("search_song_account", "/api/search", _search_song_account, ("detail", "board_plan", "compare", "now")),
    ("search_song_maxed", "/api/search", _search_song_maxed, ("detail", "compare")),
    ("search_all_account", "/api/search", _search_all_account, ("detail_all",)),
    ("search_all_maxed", "/api/search", _search_all_maxed, ()),
    ("event_song_account", "/api/event/run", lambda: _event_run(SONG, "account"), ("points",)),
    ("event_highlight_account", "/api/event/run", lambda: _event_run("highlight", "account"), ()),
    ("event_highlight_maxed", "/api/event/run", lambda: _event_run("highlight", "maxed"), ()),
]

# `--serial`: two searches that plan boards within the account's materials, run with ONE worker
# (the desktop app's "workers = 1"), compared with the same expected answers. One worker plans in
# the server process instead of a worker pool; the numbers must not change. Two cases cover the
# per-song search and the event search; the rest take ~10x longer at one worker and add nothing
# the 16-worker run does not check.
SERIAL_GROUPS = ("search_song_account", "event_song_account")


# --------------------------------------------------------------------------- transport
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post(base: str, route: str, body: dict, timeout: float = 900) -> tuple[int, dict]:
    req = urllib.request.Request(base + route, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", "replace")
        try:
            return err.code, json.loads(raw)
        except ValueError:
            return err.code, {"error_text": raw}


def _get(base: str, route: str) -> dict:
    with _OPENER.open(base + route, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _stream(base: str, job_id: str, t0: float) -> tuple[dict | None, list]:
    """The job's result payload and its (wall time, event) list from the SSE stream."""
    events: list = []
    result = None
    with _OPENER.open(f"{base}/events/{job_id}", timeout=3600) as resp:
        for raw in resp:
            line = raw.decode("utf-8").rstrip("\r\n")
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:].strip())
            now = round(time.perf_counter() - t0, 3)
            kind = ev.get("type")
            if kind in ("stage", "plan", "error", "cancelled", "end"):
                events.append({"t": now, **{k: v for k, v in ev.items() if k != "payload"}})
            if kind == "result":
                result = ev.get("payload")
                events.append({"t": now, "type": "result", "elapsed_ms": ev.get("elapsed_ms")})
            if kind == "end":
                break
    return result, events


# --------------------------------------------------------------------------- normalisation
def _strip(key: str, value) -> bool:
    # `seconds` is also a song's length (an int); only the float one is a run time
    if key == "seconds":
        return isinstance(value, float)
    return key in STRIP_KEYS


def normalise(doc):
    if isinstance(doc, dict):
        return {k: normalise(v) for k, v in doc.items() if not _strip(k, v)}
    if isinstance(doc, list):
        return [normalise(v) for v in doc]
    return doc


def _plan_flags(doc) -> list:
    if isinstance(doc, dict):
        return ([doc["plan_optimal"]] if "plan_optimal" in doc else []) + \
            [f for k, v in doc.items() for f in _plan_flags(v)]
    if isinstance(doc, list):
        return [f for v in doc for f in _plan_flags(v)]
    return []


def plan_checks(name: str, req: dict, docs: list) -> list[str]:
    """A case whose request plans boards within the materials: every row of the job and every
    plan-based follow-up carries `plan_optimal`, and no plan anywhere stopped on the time limit
    (its numbers would depend on the machine's load)."""
    # Highlight rows keep the quick plan per chart: the exact plan is per song and is not run
    planned = ((req.get("assumptions") or {}).get("boards") == "plan"
               and req.get("song") != "highlight")
    bad = []
    for case, doc, _s in docs:
        flags = _plan_flags(doc)
        if any(f is not True for f in flags):
            bad.append(f"{case}: a plan did not solve to optimality {flags}")
        if not planned:
            continue
        if case == name:
            rows = _rows(doc.get("result") or {})
            if not rows or any(r.get("plan_optimal") is not True for r in rows):
                bad.append(f"{case}: rows without plan_optimal true")
        elif doc.get("route") in PLAN_ROUTES and not flags:
            bad.append(f"{case}: no plan_optimal in the answer")
    return bad


def _canon(doc) -> str:
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, indent=1) + "\n"


# --------------------------------------------------------------------------- follow-ups
def _rows(payload: dict) -> list[dict]:
    # the Optimizer search answers `ranking`, the event search `rows`
    return list((payload or {}).get("ranking") or (payload or {}).get("rows") or [])


def _id(card) -> str:
    # the Optimizer's rows carry card ids, the event search's rows whole card records
    return card["id"] if isinstance(card, dict) else card


def _cards(row: dict) -> list[str]:
    return [_id(row["leader"]), *(_id(c) for c in (row.get("member_order") or row["members"]))]


def _detail_body(req: dict, row: dict, song: str) -> dict:
    body = {"song": song, "difficulty": req["difficulty"], "basis": req["basis"], "cards": _cards(row),
            "refine": row.get("refined")}
    if row.get("member_order"):
        body["member_order"] = [_id(c) for c in row["member_order"]]
    if "assumptions" in req:
        body["assumptions"] = req["assumptions"]
    return body


def _follow_ups(base: str, name: str, req: dict, payload: dict, kinds, out: list) -> None:
    rows = _rows(payload)

    def call(label: str, route: str, body: dict):
        t = time.perf_counter()
        status, doc = _post(base, route, body)
        out.append((f"{name}.{label}", {"route": route, "request": body, "status": status,
                                        "response": doc}, round(time.perf_counter() - t, 3)))
        return doc

    if "detail" in kinds and rows:
        b = _detail_body(req, rows[0], req["song"])
        detail = call("row_detail_timeline", "/api/row-detail", {**b, "timeline": True})
        call("row_detail_distribution", "/api/row-detail", {**b, "distribution": True})
        if "board_plan" in kinds and isinstance(detail, dict) and detail.get("board_state") is not None:
            call("board_plan_account", "/api/board-plan-account",
                 {"cards": _cards(rows[0]), "song": req["song"], "assumptions": req["assumptions"],
                  "board_state": detail.get("board_state"), "bench_slots": detail.get("bench_slots")})
    if "detail_all" in kinds and rows:
        songs = _get(base, f"/api/songs?difficulty={req['difficulty']}")
        listed = [s for s in songs.get("songs") or []
                  if s.get("notes") is not None or s.get("difficulty_level") is not None]
        first = listed[0].get("music_id") if listed else None
        if first:
            b = _detail_body(req, rows[0], first)
            b.pop("member_order", None)
            call("row_detail_first_song", "/api/row-detail", {**b, "timeline": True})
    if "compare" in kinds and len(rows) >= 2:
        teams = [{"cards": _cards(r), "overrides": [None] * 6} for r in rows[:2]]
        call("versus_compare", "/api/compare",
             {"teams": teams, "basis": req["basis"], "board": "develop", "difficulty": req["difficulty"],
              "platform": "mobile", "uptime_model": "finite", "auto": False, "song": req["song"],
              "distribution": True})
        for i, r in enumerate(rows[:2]):
            call(f"versus_timeline_{i}", "/api/timeline",
                 {"song": req["song"], "cards": _cards(r), "board": True, "basis": req["basis"],
                  "difficulty": req["difficulty"], "platform": "mobile", "uptime_model": "finite",
                  "auto": False})
    if "now" in kinds and rows:
        call("optimizer_now_compare", "/api/compare",
             {"basis": "account", "song": req["song"], "across": False, "platform": "mobile",
              "uptime_model": "finite", "auto": False, "assume_level": None, "assume_bloom": None,
              "difficulty": req["difficulty"], "board": "captured",
              "teams": [{"cards": _cards(rows[0])}, {"cards": _cards(rows[0])}]})
    if "points" in kinds and rows:
        best = rows[0]
        for label, extra in (("event_points", {}), ("event_points_multi", {"multi_score": 9_000_000})):
            call(label, "/api/event/points",
                 {"score": int(best["score"]), "live_permil": int(best["live_permil"]), "event": EVENT,
                  "boosts": req["boosts"], "length_permil": int(payload["length_permil"]),
                  "holopass": req["holopass"], **extra})
        # the event page's row detail and board plan for its best row
        b = _detail_body(req, best, req["song"])
        call("row_detail", "/api/row-detail", {**b, "timeline": True, "distribution": False})
        if "assumptions" in req and best.get("board_state") is not None:
            call("board_plan_account", "/api/board-plan-account",
                 {"cards": _cards(best), "song": req["song"], "assumptions": req["assumptions"],
                  "board_state": best.get("board_state"), "bench_slots": best.get("bench_slots")})


# --------------------------------------------------------------------------- a run
def run(code: Path, frozen: Path, out: Path, workers: int | None, only: set[str] | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    timings: dict = {"workers": workers, "cases": {}}
    box = sandbox.build(code, frozen)
    try:
        for name, route, make, kinds in GROUPS:
            if only and name not in only:
                continue
            audit = out / f"{name}.audit.tsv"
            for stale in out.glob(audit.name + ".*"):
                stale.unlink()
            env = sandbox.env(box, frozen, audit_log=audit, workers=workers)
            t_start = time.perf_counter()
            server = sandbox.Server(box, env, sandbox.python_exe(), out / f"{name}.server.log")
            boot = round(time.perf_counter() - t_start, 3)
            try:
                req = make()
                t0 = time.perf_counter()
                status, started = _post(server.base, route, req)
                if status != 202 or "job_id" not in started:
                    raise RuntimeError(f"{name}: {route} answered {status}: {started}")
                payload, events = _stream(server.base, started["job_id"], t0)
                wall = round(time.perf_counter() - t0, 3)
                docs = [(name, {"route": route, "request": req, "result": payload,
                                "plan": started.get("plan")}, wall)]
                _follow_ups(server.base, name, req, payload or {}, kinds, docs)
            finally:
                server.stop()
            missing = missing_inputs(box, audit)
            if missing:
                (out / f"{name}.missing.txt").write_text("\n".join(missing) + "\n", encoding="utf-8")
            for case, doc, seconds in docs:
                (out / f"{case}.json").write_text(_canon(normalise(doc)), encoding="utf-8")
                timings["cases"][case] = {"wall_s": seconds}
            timings["cases"][name].update({
                "server_boot_s": boot, "events": events,
                # the server's own wall-clock stage timers (stripped from the compared output)
                "server_timings": (payload or {}).get("timings"),
                "server_seconds": (payload or {}).get("seconds")})
            failed = [c for c, d, _ in docs[1:] if not 200 <= d.get("status", 0) < 300]
            failed += plan_checks(name, req, docs)
            if payload is None:     # the job ended with an error event (its message is in events)
                failed.insert(0, name + " (job: " + "; ".join(
                    str(e.get("message")) for e in events if e.get("type") == "error") + ")")
            timings["failed_requests"] = timings.get("failed_requests", []) + failed
            print(f"{name}: {wall:.1f}s (+{len(docs) - 1} follow-ups)"
                  + (f"  FAILED REQUESTS: {failed}" if failed else ""), flush=True)
    finally:
        sandbox.remove(box)
    (out / "timings.json").write_text(json.dumps(timings, indent=1), encoding="utf-8")
    return timings


def diff(a: Path, b: Path, groups: set[str] | None = None) -> list[str]:
    """Case files that differ (or exist on one side only); `groups` limits it to those groups'
    files (a run with `--only`)."""
    names = {p.name for p in a.glob("*.json") if p.name != "timings.json"}
    names |= {p.name for p in b.glob("*.json") if p.name != "timings.json"}
    if groups:
        names = {n for n in names if n.split(".")[0] in groups}
    bad = []
    for n in sorted(names):
        pa, pb = a / n, b / n
        if not pa.is_file() or not pb.is_file():
            bad.append(f"{n}: only in {'B' if not pa.is_file() else 'A'}")
        else:
            # normalised again on load, so a stricter normaliser applies to a stored golden too
            x = normalise(json.loads(pa.read_text("utf-8")))
            y = normalise(json.loads(pb.read_text("utf-8")))
            if x != y:
                bad.append(f"{n}: {first_difference(x, y)}")
    return bad


def first_difference(x, y, path: str = "$") -> str:
    if type(x) is not type(y):
        return f"{path}: {type(x).__name__} != {type(y).__name__}"
    if isinstance(x, dict):
        for k in sorted(set(x) | set(y)):
            if k not in x or k not in y:
                return f"{path}.{k}: missing on one side"
            if x[k] != y[k]:
                return first_difference(x[k], y[k], f"{path}.{k}")
    elif isinstance(x, list):
        if len(x) != len(y):
            return f"{path}: length {len(x)} != {len(y)}"
        for i, (u, v) in enumerate(zip(x, y)):
            if u != v:
                return first_difference(u, v, f"{path}[{i}]")
    elif x != y:
        return f"{path}: {str(x)[:80]!r} != {str(y)[:80]!r}"
    return f"{path}: equal?"


# The identity hashes of the inputs a Compare answer reports. A change to the scoring config's
# fields (not its values) moves them while every number stays the same.
FINGERPRINT_KEYS = ("inputs_hash", "inputs_hash_short", "inputs_hash_files")


def _without_fingerprints(doc):
    if isinstance(doc, dict):
        return {k: _without_fingerprints(v) for k, v in doc.items() if k not in FINGERPRINT_KEYS}
    if isinstance(doc, list):
        return [_without_fingerprints(v) for v in doc]
    return doc


def _graft_fingerprints(old, new):
    """`old` with every fingerprint value taken from `new`, nothing else touched."""
    if isinstance(old, dict) and isinstance(new, dict):
        return {k: (new[k] if k in FINGERPRINT_KEYS and k in new else
                    _graft_fingerprints(v, new.get(k))) for k, v in old.items()}
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        return [_graft_fingerprints(u, v) for u, v in zip(old, new)]
    return old


def refingerprint(expected: Path, run_dir: Path) -> list[str]:
    """Re-record ONLY the fingerprint fields of `expected` from `run_dir`, after proving that
    every other field of every case is identical. Refuses (changes nothing) otherwise. The
    expected directory is copied to `<expected>.before_refingerprint` first."""
    import shutil  # noqa: PLC0415

    names = sorted(p.name for p in expected.glob("*.json") if p.name != "timings.json")
    if names != sorted(p.name for p in run_dir.glob("*.json") if p.name != "timings.json"):
        raise SystemExit("the run and the golden do not hold the same cases")
    changed, bad = [], []
    for n in names:
        a = normalise(json.loads((expected / n).read_text("utf-8")))
        b = normalise(json.loads((run_dir / n).read_text("utf-8")))
        if _without_fingerprints(a) != _without_fingerprints(b):
            bad.append(f"{n}: {first_difference(_without_fingerprints(a), _without_fingerprints(b))}")
        elif a != b:
            changed.append(n)
    if bad:
        raise SystemExit("not only fingerprints differ; nothing re-recorded:\n" + "\n".join(bad))
    backup = expected.with_name(expected.name + ".before_refingerprint")
    if changed and not backup.exists():
        shutil.copytree(expected, backup)
    for n in changed:
        old = json.loads((expected / n).read_text("utf-8"))
        new = normalise(json.loads((run_dir / n).read_text("utf-8")))
        (expected / n).write_text(_canon(_graft_fingerprints(old, new)), encoding="utf-8")
    return changed


def missing_inputs(box: Path, audit_log: Path) -> list[str]:
    """Files a run tried to read inside the sandbox that do not exist there: an input the
    snapshot lacks (or a probe the code makes on purpose -- read the list, do not assume)."""
    box_s = str(box).lower()
    seen = set()
    lines = [line for log in sorted(audit_log.parent.glob(audit_log.name + ".*"))
             for line in log.read_text("utf-8", "replace").splitlines()]
    for line in lines:
        parts = line.split("\t")
        if len(parts) >= 4 and parts[1] == "open" and "r" in parts[2] and parts[3].lower().startswith(box_s):
            if not Path(parts[3]).exists() and "__pycache__" not in parts[3]:
                seen.add(Path(parts[3][len(box_s):].lstrip("\\/")).as_posix())
    return sorted(seen)


def audit(frozen: Path, run_dir: Path) -> dict:
    """What a finished run read outside the sandbox and the snapshot, wrote into the snapshot,
    or tried on the network. (Inputs missing from the sandbox are listed during the run, in
    `<case>.missing.txt`, while the sandbox still exists.)"""
    frozen_s = str(frozen.resolve()).lower()
    report = {"blocked": set(), "wrote_snapshot": set(), "outside": set()}
    for log in run_dir.glob("*.audit.tsv.*"):
        for line in log.read_text("utf-8", "replace").splitlines():
            parts = line.split("	")
            if len(parts) < 4 or parts[1] == "start":
                continue
            _pid, kind, mode, path = parts[:4]
            low = path.lower()
            if kind == "BLOCKED":
                report["blocked"].add(path)
            elif low.startswith(frozen_s):
                if kind == "open" and any(c in mode for c in "wax+"):
                    report["wrote_snapshot"].add(path)
            elif (Path(path).is_absolute() and "hbgold_" not in low and low != "nul"
                  and not any(seg in low for seg in ("python", "site-packages", r"\windows" + "\\",
                                                     r"\temp" + "\\"))):
                report["outside"].add(path)
    return {k: sorted(v) for k, v in report.items()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("run", "record", "compare"):
        p = sub.add_parser(cmd)
        p.add_argument("--frozen", type=Path, required=True)
        p.add_argument("--code", type=Path, default=REPO)
        p.add_argument("--workers", type=int, default=None)
        p.add_argument("--only", default="")
        p.add_argument("--out", type=Path)
        p.add_argument("--expected", type=Path)
        if cmd != "record":
            p.add_argument("--serial", action="store_true",
                           help="one worker, the planned-board searches only (SERIAL_GROUPS)")
    p = sub.add_parser("diff")
    p.add_argument("a", type=Path)
    p.add_argument("b", type=Path)
    p = sub.add_parser("refingerprint", help="re-record only the input fingerprints from a run")
    p.add_argument("--frozen", type=Path, required=True)
    p.add_argument("--run", type=Path, required=True)
    p = sub.add_parser("audit")
    p.add_argument("--frozen", type=Path, required=True)
    p.add_argument("--run", type=Path, required=True)
    args = ap.parse_args(argv)

    if args.cmd == "diff":
        bad = diff(args.a, args.b)
        print("identical" if not bad else "\n".join(bad))
        return 1 if bad else 0
    if args.cmd == "refingerprint":
        changed = refingerprint(args.frozen / "expected", args.run)
        print(f"fingerprints re-recorded in {len(changed)} case(s): {changed}")
        return 0
    if args.cmd == "audit":
        print(json.dumps(audit(args.frozen, args.run), indent=1))
        return 0
    only = {s for s in args.only.split(",") if s} or None
    if getattr(args, "serial", False):
        args.workers = 1
        only = only or set(SERIAL_GROUPS)
    if args.cmd == "record":
        out = args.out or args.frozen / "expected"
        if out.exists() and any(out.glob("*.json")):
            raise SystemExit(f"{out} already holds a golden; never re-record over it")
        t = run(args.code, args.frozen, out, args.workers, only)
        if t.get("failed_requests"):
            raise SystemExit(f"not a golden: requests failed {t['failed_requests']}; delete {out} and fix")
        return 0
    out = args.out or args.frozen / f"run_{time.strftime('%Y%m%d_%H%M%S')}"
    t = run(args.code, args.frozen, out, args.workers, only)
    if args.cmd == "compare":
        bad = diff(args.expected or args.frozen / "expected", out, only)
        bad += [f"request failed: {f}" for f in t.get("failed_requests") or []]
        print("GOLDEN: identical" if not bad else "GOLDEN: DIFFERS\n" + "\n".join(bad))
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
