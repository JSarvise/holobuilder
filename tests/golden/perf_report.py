"""Per-stage wall-clock table from golden runs' ``timings.json`` (Markdown on stdout).

Each run directory gives one column; stage times come from the server's own stage events
(time from the request to each stage starting, as the client sees it) and its stage timers.

    python tests/golden/perf_report.py DIR/expected DIR/run_same16 ...
    python tests/golden/perf_report.py --median RUN_1 RUN_2 RUN_3

``--median``: the same runs repeated (three, on an otherwise idle machine); one table of the
median of each figure: the searches with the server's stage timers, the event searches with
their stage events, and every follow-up request.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

SEARCH_STAGES = ("search", "refine", "replan", "engine_start")
EVENT_STAGES = ("event_search", "replan", "rescore", "refine", "detail")


def stages(case: dict) -> dict[str, float]:
    """`{stage: seconds}` from each stage's active -> done events; time between stages that
    report no stage of their own is summed as `other`."""
    events = [e for e in case.get("events") or [] if e.get("type") == "stage" and e.get("stage")]
    out: dict[str, float] = {}
    started: dict[str, float] = {}
    for e in events:
        if e.get("state") == "active":
            started[e["stage"]] = e["t"]
        elif e.get("state") == "done" and e["stage"] in started:
            out[e["stage"]] = round(out.get(e["stage"], 0.0) + e["t"] - started.pop(e["stage"]), 2)
    end = next((e["t"] for e in case.get("events") or [] if e.get("type") == "result"), None)
    if end is not None and out:
        other = round(end - sum(out.values()), 2)
        if other > 0.05:
            out["other"] = other
    return out


def _median(values: list) -> str:
    values = [v for v in values if v is not None]
    return f"{statistics.median(values):.1f}" if values else "-"


def median_tables(runs: list[dict]) -> None:
    """Markdown: the median over `runs` of every case's wall time and stages."""
    names = list(dict.fromkeys(c for r in runs for c in r["cases"]))

    def col(case, pick):
        return [pick(r["cases"][case]) for r in runs if case in r["cases"]]

    searches = [c for c in names if "." not in c and col(c, lambda x: x.get("server_timings"))[0]]
    events = [c for c in names if "." not in c and c not in searches]
    requests = [c for c in names if "." in c]
    print(f"Median of {len(runs)} runs (workers {runs[0]['workers'] or 'all'}).\n")
    print("| Case | Wall | " + " | ".join(SEARCH_STAGES) + " |")
    print("|---|---:|" + "---:|" * len(SEARCH_STAGES))
    for c in searches:
        cells = [_median(col(c, lambda x: x["wall_s"]))]
        cells += [
            _median(col(c, lambda x, s=s: (x.get("server_timings") or {}).get(s))) for s in SEARCH_STAGES
        ]
        print(f"| {c} | " + " | ".join(cells) + " |")
    print("\n| Case | Wall | " + " | ".join(EVENT_STAGES) + " |")
    print("|---|---:|" + "---:|" * len(EVENT_STAGES))
    for c in events:
        cells = [_median(col(c, lambda x: x["wall_s"]))]
        cells += [_median(col(c, lambda x, s=s: stages(x).get(s))) for s in EVENT_STAGES]
        print(f"| {c} | " + " | ".join(cells) + " |")
    print("\n| Request | Wall |")
    print("|---|---:|")
    for c in requests:
        values = col(c, lambda x: x["wall_s"])
        print(f"| {c} | {statistics.median(values):.2f} |")


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--median":
        median_tables([json.loads((Path(a) / "timings.json").read_text("utf-8")) for a in argv[1:]])
        return 0
    runs = [(Path(a).name, json.loads((Path(a) / "timings.json").read_text("utf-8"))) for a in argv]
    names = list(dict.fromkeys(c for _, r in runs for c in r["cases"]))
    head = "| case | " + " | ".join(f"{n} (workers {r['workers'] or 'all'})" for n, r in runs) + " |"
    print(head)
    print("|" + "---|" * (len(runs) + 1))
    for c in names:
        cells = []
        for _, r in runs:
            case = r["cases"].get(c)
            if not case:
                cells.append("")
                continue
            parts = [f"**{case['wall_s']:.1f} s**"]
            st = stages(case)
            if st:
                parts.append(", ".join(f"{k} {v:.1f}" for k, v in st.items()))
            srv = case.get("server_timings")
            if srv:
                parts.append("server: " + ", ".join(f"{k} {v}" for k, v in srv.items()))
            if case.get("server_boot_s") is not None:
                parts.append(f"boot {case['server_boot_s']:.1f}")
            cells.append("<br>".join(parts))
        print(f"| {c} | " + " | ".join(cells) + " |")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
