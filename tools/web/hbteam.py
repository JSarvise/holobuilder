"""A team row as a file (`.hbteam`): what the Optimizer showed, and everything needed to replay it.

The page's "Export" button (`POST /api/export-team`) builds one (`document`) and
`tools/refresh/team_file.py` saves it; `team_file.py show` / `replay` read and check one. Plain
JSON, readable by hand.

* `request`: the row as the row detail is asked for it (song, difficulty, basis, the six cards,
  slot order, the search's account levers, platform and AUTO), so the same numbers come back.
* `detail`: what the server computed for that request when the file was made (Expected, Max,
  slot order, Overall Power, every planned board and socket, the run law's percentiles).
* `shown`: the numbers the page showed on the row; `search`: the search that ranked it.
* `account`: the account files the score reads (`ACCOUNT_FILES`), so another install scores
  the row on the exporter's own cards, boards and materials. Card states, boards, materials
  and board points only: no login, no player id, no client profile.
* `app` / `game`: the app and model versions, the game client version and the chart's digest.

`team_file.py replay` re-scores the file on THIS install's code in a throwaway copy of the checkout whose
account files are the file's (the golden harness' sandbox: offline, its own data paths; the
live checkout is never written), then compares the replayed detail with the file's, field by
field. The same numbers mean the row is reproduced; a difference names the field and both
values. `--search` also re-runs the search that ranked the row and says where the row lands;
`--runs N --score S` places a score among the best of N runs on the row's exact run law
(`song_distribution.best_of_n`). Nothing is encrypted or signed: the check is the replay.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))  # this checkout's hbuilder first
from hbuilder import paths  # noqa: E402

FORMAT = "holobuilder-team"
FORMAT_VERSION = 1
SUFFIX = ".hbteam"
FORBIDDEN = set('\\/:*?"<>|')  # characters a file name may not hold (Windows)

# The account files a row's score reads (measured: every file a search, a row detail, its run
# law and timeline open on the account basis), relative to the checkout.
ACCOUNT_FILES = (
    "data/owned.json",
    "state/collection.json",
    f"{paths.DUMPS_REL}/board_state.json",
    f"{paths.DUMPS_REL}/board.json",
    f"{paths.DUMPS_REL}/inventory.json",
    f"{paths.DUMPS_REL}/account_snapshot_main.json",
)
# Of the account snapshot (a whole `User/Get`), only what the score reads: each holomem's
# unspent board points. Decks, missions, emblems and the rest stay out.
SNAPSHOT_KEEP = ("board_points",)
# The row detail's request, as the page sends it.
REQUEST_KEYS = (
    "song",
    "difficulty",
    "basis",
    "cards",
    "member_order",
    "refine",
    "assumptions",
    "platform",
    "auto",
    "pool",
)
# Detail fields a file does not carry: the timeline is drawn from the rest and is large.
DETAIL_SKIP = ("timeline",)
# Fields compared loosely on replay: the planner's own report of how it went.
REPLAY_SKIP = ("leaf",)


# ---------------------------------------------------------------------------------- building
def request_of(body: Mapping) -> dict:
    """The row-detail request inside a page body (`REQUEST_KEYS` that are set)."""
    return {k: body[k] for k in REQUEST_KEYS if body.get(k) is not None}


def account_files(repo: Path = paths.REPO) -> dict:
    """`{relative path: parsed JSON}` of the `ACCOUNT_FILES` this install has (the snapshot
    cut to `SNAPSHOT_KEEP`)."""
    out = {}
    for rel in ACCOUNT_FILES:
        path = repo / rel
        if not path.is_file():
            continue
        doc = json.loads(path.read_text("utf-8"))
        if rel.endswith("account_snapshot_main.json"):
            doc = {k: doc[k] for k in SNAPSHOT_KEEP if k in doc}
        out[rel] = doc
    return out


def app_info(repo: Path = paths.REPO) -> dict:
    """The app's version (the desktop build's `VERSION`, else "dev" and the git commit when
    there is one) and the scoring model's."""
    from hbuilder.domain.model_version import MODEL_VERSION  # noqa: PLC0415

    version_file = repo / "tools" / "app" / "VERSION"
    version = version_file.read_text("utf-8").strip() if version_file.is_file() else "dev"
    commit = None
    if version == "dev":
        try:
            commit = (
                subprocess.run(
                    ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                ).stdout.strip()
                or None
            )
        except (OSError, subprocess.SubprocessError):
            commit = None
    return {"version": version, "commit": commit, "model": MODEL_VERSION}


def game_info(request: Mapping) -> dict:
    """The game client version this install read, and the digest of every chart the row plays
    (a different chart file scores differently)."""
    import leaf_inputs  # noqa: PLC0415

    client = paths.DUMPS / "client_version.json"
    version = json.loads(client.read_text("utf-8")).get("version") if client.is_file() else None
    difficulty = leaf_inputs._difficulty(request.get("difficulty") or "EXPERT")
    songs = [str(s) for s in (request.get("pool") or [request.get("song")]) if s]
    try:
        digests = leaf_inputs.chart_digests(leaf_inputs.charts_path(difficulty), difficulty)
    except (OSError, ValueError, KeyError):
        digests = {}
    return {"client_version": version, "difficulty": difficulty, "charts": {s: digests.get(s) for s in songs}}


def team_of(request: Mapping, files: Mapping) -> list[dict]:
    """The six cards, readable: slot, id, holomem and outfit (the catalogue's), and the card's
    state in the exported account (level, bloom, limit break) when it has one."""
    catalogue = json.loads((paths.REPO / "data" / "cards.json").read_text("utf-8"))
    rows = catalogue.get("cards", catalogue) if isinstance(catalogue, dict) else catalogue
    by_id = {c.get("id"): c for c in rows if isinstance(c, dict)} if isinstance(rows, list) else rows
    states = (files.get("state/collection.json") or {}).get("cards") or {}
    out = []
    for k, cid in enumerate(request.get("cards") or []):
        card = by_id.get(cid) or {}
        out.append(
            {
                "slot": "leader" if k == 0 else f"member {k}",
                "id": cid,
                "holomem": card.get("member"),
                "outfit": card.get("costume_name"),
                "account": states.get(cid),
            }
        )
    return out


def document(
    request: Mapping, detail: Mapping, *, shown: Mapping | None = None, search: Mapping | None = None
) -> dict:
    """The `.hbteam` document of a row: `request` (`request_of`), `detail` (the server's row
    detail for it, with the run law's summary), `shown` (the page's numbers), `search` (the
    search that ranked it)."""
    detail = {k: v for k, v in detail.items() if k not in DETAIL_SKIP}
    if isinstance(detail.get("distribution"), dict):
        detail["distribution"] = {k: v for k, v in detail["distribution"].items() if k != "law"}
    files = account_files()
    now = time.time()
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "about": "A holobuilder team row and the account files its score reads. Replay it with "
        "tools/refresh/team_file.py replay <file>.",
        "created_at": _dt.datetime.fromtimestamp(now, _dt.UTC).isoformat(timespec="seconds"),
        "created_unix": int(now),
        "app": app_info(),
        "game": game_info(request),
        "team": team_of(request, files),
        "request": dict(request),
        "search": dict(search) if search else None,
        "shown": dict(shown) if shown else None,
        "detail": json.loads(json.dumps(detail, default=str)),
        "account": {
            "files": files,
            "note": "the exporter's card states, boards, materials and board points; "
            "no login, player id or client profile",
        },
    }


def file_name(doc: Mapping) -> str:
    """`<song> #<rank> <leader> <date time>.hbteam`, with only characters every file system
    takes."""
    shown = doc.get("shown") or {}
    team = doc.get("team") or [{}]
    song = str(shown.get("song") or doc["request"].get("song") or "team")
    rank = f" #{shown['rank']}" if shown.get("rank") else ""
    leader = f" {team[0].get('holomem')}" if team and team[0].get("holomem") else ""
    stamp = _dt.datetime.fromtimestamp(doc.get("created_unix") or time.time()).strftime("%Y-%m-%d %H%M")
    name = f"{song}{rank}{leader} {stamp}"
    name = "".join(" " if ch in FORBIDDEN or ord(ch) < 32 else ch for ch in name)
    return " ".join(name.split())[:120] + SUFFIX


# ---------------------------------------------------------------------------------- reading
def load(path: Path) -> dict:
    """A `.hbteam` document; ValueError when the file is not one this version reads."""
    try:
        doc = json.loads(Path(path).read_text("utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: not a readable JSON file ({exc})") from None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ValueError(f"{path}: not a holobuilder team file")
    if int(doc.get("format_version") or 0) > FORMAT_VERSION:
        raise ValueError(
            f"{path}: format {doc.get('format_version')} is newer than this tool's "
            f"{FORMAT_VERSION}; update holobuilder"
        )
    for key in ("request", "detail", "account"):
        if not isinstance(doc.get(key), dict):
            raise ValueError(f"{path}: missing {key!r}")
    return doc


def _n(x) -> str:
    return f"{x:,.0f}" if isinstance(x, (int, float)) else "—"


def summary(doc: Mapping) -> str:
    """The file in a few lines: premise, team, the numbers shown and served, what it carries."""
    req, det = doc["request"], doc["detail"]
    app, game = doc.get("app") or {}, doc.get("game") or {}
    a = req.get("assumptions")
    play = ("PC" if req.get("platform") == "pc" else "Mobile") + (", AUTO" if req.get("auto") else "")
    lines = [
        f"{doc.get('created_at')} | holobuilder {app.get('version')}"
        + (f" ({app['commit']})" if app.get("commit") else "")
        + f" | model {app.get('model')} | game {game.get('client_version')}",
        f"{req.get('song')} {game.get('difficulty') or req.get('difficulty') or 'EXPERT'} | "
        f"basis {req.get('basis')} | {play}"
        + (f" | Highlight pool of {len(req['pool'])}" if req.get("pool") else ""),
        f"account levers: {json.dumps(a, sort_keys=True) if a else 'none (fixed basis)'}",
        "team:",
    ]
    for c in doc.get("team") or []:
        st = c.get("account") or {}
        state = ", ".join(f"{k} {v}" for k, v in st.items() if k in ("level", "bloom", "limit_break"))
        lines.append(
            f"  {c['slot']:<9} {c.get('holomem') or '?'} / {c.get('outfit') or '?'}"
            f"  [{c['id']}]" + (f"  {state}" if state else "")
        )
    if det.get("member_order"):
        lines.append("slot order: " + " > ".join(det["member_order"]))
    dist = det.get("distribution") or {}
    lines.append(
        f"served: Expected {_n(det.get('song_est'))} | Max {_n(det.get('song_max'))}"
        + (
            f" | p10 {_n(dist.get('p10'))} | p50 {_n(dist.get('p50'))} | p90 {_n(dist.get('p90'))}"
            if dist.get("available")
            else ""
        )
    )
    notes = det.get("notes") or {}
    if notes:
        lines.append(
            f"note by note: {len(notes.get('rows') or [])} notes adding up to "
            f"{_n(notes.get('total'))}"
            + (" (= Expected)" if notes.get("verified") else " (NOT the Expected)")
        )
    shown = doc.get("shown") or {}
    if shown:
        lines.append(
            "shown on the page: "
            + ", ".join(f"{k} {_n(v) if isinstance(v, (int, float)) else v}" for k, v in shown.items())
        )
    files = (doc.get("account") or {}).get("files") or {}
    lines.append(f"account files: {', '.join(Path(f).name for f in files) or 'none'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------- comparing
# Two floats this close are the same number summed in another order: the run law's
# probabilities differ in their last digits between two processes (the order its states are
# added in), while every score, percentile and board is exact.
REL_ROUNDING = 1e-9


def differences(
    want: Any,
    got: Any,
    path: str = "",
    *,
    out: list | None = None,
    limit: int = 60,
    rounding: list | None = None,
) -> list[str]:
    """Every place `got` differs from `want` (`path: want -> got`), over `want`'s fields,
    lists and dicts field by field; `REPLAY_SKIP` fields at the top level are not compared.
    Integers and strings exactly; two floats within `REL_ROUNDING` of each other go to
    `rounding` (their paths) instead."""
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(want, dict):
        if not isinstance(got, dict):
            out.append(f"{path or '.'}: {_short(want)} -> {_short(got)}")
            return out
        for k, v in want.items():
            if not path and k in REPLAY_SKIP:
                continue
            if k not in got:
                out.append(f"{path}.{k}: {_short(v)} -> (absent)")
                continue
            differences(v, got[k], f"{path}.{k}", out=out, limit=limit, rounding=rounding)
        return out
    if isinstance(want, list):
        if not isinstance(got, list) or len(got) != len(want):
            out.append(f"{path}: {_short(want)} -> {_short(got)}")
            return out
        for i, (w, g) in enumerate(zip(want, got)):
            differences(w, g, f"{path}[{i}]", out=out, limit=limit, rounding=rounding)
        return out
    if isinstance(want, float) and isinstance(got, (int, float)) and math.isnan(want) and math.isnan(got):
        return out
    if want != got:
        if (
            isinstance(want, float)
            and isinstance(got, float)
            and not isinstance(got, bool)
            and math.isclose(want, got, rel_tol=REL_ROUNDING, abs_tol=0.0)
        ):
            if rounding is not None:
                rounding.append(path)
            return out
        out.append(f"{path}: {_short(want)} -> {_short(got)}")
    return out


def _short(v: Any) -> str:
    s = json.dumps(v, default=str, sort_keys=True)
    return s if len(s) <= 120 else s[:117] + "..."


# ---------------------------------------------------------------------------------- replaying
def replay_inside(doc_path: Path, *, search: bool, runs: int | None, score: float | None) -> dict:
    """The replay's inner run, in the sandbox (`team_file.replay`): the row detail for the
    file's request (and the search that ranked it, and the best of N runs)."""
    sys.path.insert(0, str(HERE))
    import ondemand  # noqa: PLC0415
    from hbweb import search as search_routes  # noqa: PLC0415

    from hbuilder.domain.song_distribution import best_of_n  # noqa: PLC0415

    doc = load(doc_path)
    body = {**doc["request"], "distribution": True, "timeline": False, "notes": True}
    req = search_routes.row_request(body)
    detail = search_routes.row_figures(req, body, keep_law=bool(runs))
    law = (detail.get("distribution") or {}).pop("law", None)
    result: dict[str, Any] = {
        "detail": json.loads(
            json.dumps({k: v for k, v in detail.items() if k not in DETAIL_SKIP}, default=str)
        )
    }
    if runs:
        result["best_of"] = best_of_n(law, runs, score) if law else None
    if search and doc.get("search"):
        sb = doc["search"]
        a = ondemand.parse_assumptions(sb["assumptions"]) if isinstance(sb.get("assumptions"), dict) else None
        levels = tuple(sb["levels"]) if sb.get("levels") else None
        eng = ondemand.Engine(
            "account" if a is not None else str(sb.get("basis") or "account"),
            req["difficulty"],
            assumptions=a,
            levels=levels,
            play=ondemand.parse_play(sb),
        )
        try:
            payload = eng.search(
                sb.get("song") or None, ondemand.parse_filters(sb), top=int(sb.get("top") or ondemand.RANKING)
            )
        finally:
            eng.close()
        team = sorted(req["ids"][1:])
        rows = payload.get("ranking") or []
        rank = next(
            (
                i + 1
                for i, r in enumerate(rows)
                if r.get("leader") == req["ids"][0] and sorted(r.get("members") or []) == team
            ),
            None,
        )
        result["search"] = {
            "rank": rank,
            "top": [
                {
                    "leader": r.get("leader"),
                    "members": r.get("members"),
                    "best": r.get("best"),
                    "song_max": r.get("song_max"),
                }
                for r in rows[:10]
            ],
        }
    return result


RESULT_MARK = "HBTEAM-RESULT "


def main(argv: list[str] | None = None) -> int:
    """`hbteam.py inside FILE [--search] [--runs N [--score S]]`: `replay_inside`, its result on
    one stdout line after `RESULT_MARK` (the sandbox's run; `team_file.py` reads it)."""
    ap = argparse.ArgumentParser(description="the inner run of tools/refresh/team_file.py replay")
    ap.add_argument("cmd", choices=["inside"])
    ap.add_argument("file", type=Path)
    ap.add_argument("--search", action="store_true")
    ap.add_argument("--runs", type=int, default=None)
    ap.add_argument("--score", type=float, default=None)
    args = ap.parse_args(argv)
    result = replay_inside(args.file, search=args.search, runs=args.runs, score=args.score)
    print(RESULT_MARK + json.dumps(result, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
