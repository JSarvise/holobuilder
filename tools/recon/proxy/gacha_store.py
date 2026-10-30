"""Append-only JSONL store for the gacha tracker (`data/gacha/`, versioned in git).

JSONL in the repo, never SQLite: the repo is the backup.

Every writer here APPENDS. Nothing truncates, rewrites or deletes -- `test_gacha_store.py`
walks this file's AST and fails on any other open() mode or on unlink/rename/write_text.
Bytes are written with `\n` (Windows `write_text` would re-translate newlines).

A multi-draw gives every card the same `draw_time` (epoch ms) and a card drawn twice appears as two identical lines. Dedupe is
therefore a multiset comparison inside each (draw_time, gacha_name) group.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
STORE_DIR = REPO_ROOT / "data" / "gacha"
PULLS = "pulls.jsonl"
BANNERS = "banners.jsonl"
RATES = "rates.jsonl"
COUNTERS = "counters.jsonl"
SNAPSHOTS = "snapshots"
ART = "art"
ALIASES = "banner_aliases.json"   # the ONE hand-maintained file here; nothing in this repo writes it

BANNER_WATCH = ("name", "end_time", "pickup_card_ids")


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    with open(path, "rb") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line.decode("utf8")))
    return out


def append_jsonl(path: Path, rows: list[dict]) -> int:
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)
    with open(path, "ab") as fh:
        fh.write(payload.encode("utf8"))
    return len(rows)


def count_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    with open(path, "rb") as fh:
        return sum(1 for line in fh if line.strip())


def resolve_gacha_id(name: str, live_banners: list[dict]) -> str | None:
    for b in live_banners:
        if b.get("name") == name:
            return b.get("gacha_id")
    return None


def new_pulls(server_hist: list[dict], stored: list[dict], live_banners: list[dict],
              synced_at: int) -> list[dict]:
    have: dict[tuple, Counter] = {}
    for r in stored:
        have.setdefault((r["draw_time"], r["gacha_name"]), Counter())[r["card_id"]] += 1
    want: dict[tuple, Counter] = {}
    for h in server_hist:
        want.setdefault((h["draw_time"], h["gacha_name"]), Counter())[h["card_id"]] += 1
    rows: list[dict] = []
    for (dt, name), cnt in sorted(want.items()):
        missing = cnt - have.get((dt, name), Counter())
        gid = resolve_gacha_id(name, live_banners)
        for card_id, n in sorted(missing.items()):
            rows.extend({"draw_time": dt, "gacha_name": name, "gacha_id": gid,
                         "card_id": card_id, "first_seen": synced_at} for _ in range(n))
    return rows


def _banner_row(b: dict, synced_at: int) -> dict:
    return {"gacha_id": b.get("gacha_id"), "name": b.get("name"), "type": b.get("type"),
            "start_time": b.get("start_time"), "end_time": b.get("end_time"),
            "pickup_card_ids": list(b.get("pickup_card_ids") or []),
            "icon_asset_id": b.get("icon_asset_id"), "seen_at": synced_at, "raw": b}


def banner_rows(live_banners: list[dict], stored: list[dict], synced_at: int) -> list[dict]:
    latest: dict[str, dict] = {}
    for r in stored:  # file order == time order; last line wins
        latest[r["gacha_id"]] = r
    rows = []
    for b in live_banners:
        row = _banner_row(b, synced_at)
        prev = latest.get(row["gacha_id"])
        if prev is None or any(prev.get(k) != row.get(k) for k in BANNER_WATCH):
            rows.append(row)
    return rows
