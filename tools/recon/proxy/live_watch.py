"""Read `live_tap.py`'s stream while the game is running, and filter it.

    proxyenv/Scripts/python live_watch.py                    # follow everything
    proxyenv/Scripts/python live_watch.py --path Marathon    # only matching RPCs
    proxyenv/Scripts/python live_watch.py --summary          # counts, not trees
    proxyenv/Scripts/python live_watch.py --since 0          # replay from the start

Separate process from the addon on purpose. mitmproxy owns its own event loop, and
anything that blocks in a hook stalls the game's traffic; the JSONL is the boundary.
The tail is byte-offset based and opens in binary so a half-written final line is
backed over rather than parsed — the same shape as `tools/jumprope/events.py`, which
is the only live-IPC pattern in this repo that has actually been run.

`--summary` is the one to reach for first when hunting an unknown mechanic: it answers
"what did the game just talk to, and how big was the answer" without printing a
thousand-node tree. Find the RPC that moves when you do the thing, THEN open its tree.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT = os.environ.get("LIVE_TAP_EVENTS") or os.path.join(HERE, "dumps", "live_tap.jsonl")


def render(tree, indent: int = 2, depth: int = 0) -> list[str]:
    lines = []
    pad = " " * (indent + depth * 2)
    if isinstance(tree, dict):
        for key, value in tree.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.extend(render(value, indent, depth + 1))
            else:
                lines.append(f"{pad}{key}: {value}")
    elif isinstance(tree, list):
        for i, value in enumerate(tree):
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}[{i}]")
                lines.extend(render(value, indent, depth + 1))
            else:
                lines.append(f"{pad}[{i}] {value}")
    else:
        lines.append(f"{pad}{tree}")
    return lines


def follow(path: str, offset: int, poll: float = 0.4):
    """Yield records as they land, forever. Torn trailing lines are re-read."""
    while True:
        if not os.path.exists(path):
            time.sleep(poll)
            continue
        with open(path, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
        if not chunk:
            time.sleep(poll)
            continue
        *complete, tail = chunk.split(b"\n")
        offset += len(chunk) - len(tail)
        for raw in complete:
            if not raw.strip():
                continue
            try:
                yield json.loads(raw.decode("utf-8"))
            except Exception:
                continue


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", default=DEFAULT)
    ap.add_argument("--path", default="", help="substring filter on the RPC path")
    ap.add_argument("--dir", default="", choices=("", "req", "resp", "non-grpc"))
    ap.add_argument("--summary", action="store_true", help="counts only, no trees")
    ap.add_argument(
        "--since",
        type=int,
        default=-1,
        help="byte offset to start from; -1 (default) means only NEW traffic",
    )
    args = ap.parse_args()

    start = args.since
    if start < 0:
        start = os.path.getsize(args.file) if os.path.exists(args.file) else 0

    print(f"watching {args.file} from byte {start:,}")
    print("(start the game; Ctrl-C to stop)\n")
    seen: Counter[str] = Counter()
    try:
        for rec in follow(args.file, start):
            if args.path and args.path.lower() not in rec.get("path", "").lower():
                continue
            if args.dir and rec.get("dir") != args.dir:
                continue
            key = f"{rec.get('dir')} {rec.get('path')}"
            seen[key] += 1
            if args.summary:
                print(f"{seen[key]:>4}x  {key}   {rec.get('bytes', 0):,} bytes")
                continue
            stamp = time.strftime("%H:%M:%S", time.localtime(rec.get("t", 0)))
            print(f"[{stamp}] {key}  ({rec.get('bytes', 0):,} bytes)")
            if "tree" in rec:
                print("\n".join(render(rec["tree"])))
            print()
    except KeyboardInterrupt:
        print("\n-- what was seen --")
        for key, count in seen.most_common():
            print(f"  {count:>5}  {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
