"""Sync the account's gacha data into data/gacha/ (append-only). Manual only: the user
presses the button on the web tool; nothing schedules this.

Wire facts + safety: see gacha_wire.py. Store rules: see gacha_store.py.

    proxyenv/Scripts/python gacha_sync.py            # full sync incl. banner art
    proxyenv/Scripts/python gacha_sync.py --no-art

Last stdout line is `SUMMARY {json}` with the line-count deltas RE-READ FROM DISK.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import account_snapshot as snap  # noqa: E402
import gacha_store as gs  # noqa: E402
import gacha_wire as gw  # noqa: E402

gw.widen_whitelist()

# Gacha/ListHistory returns at most this many entries (the newest), and only the last ~30 days
# (the window's start moving a day a day). It takes no request, so
# pulls older than the window can only ever enter the store from a sync that ran while they
# were still inside it -- which is why a long-unsynced account can lose history.
HISTORY_CAP = 100
HISTORY_DAYS = 30


def history_gap(server_hist: list[dict], stored: list[dict], last_sync: int | None = None,
                synced_at: int | None = None) -> bool:
    """True when pulls may have been lost between syncs: the server's oldest entry is newer
    than everything the store holds AND the window could have dropped something -- it came
    back FULL (the 100 cap), or the last sync (`last_sync`, epoch s) is older than the 30-day
    window at `synced_at`. Otherwise a window that starts past the store only means nothing
    was pulled in between. An empty store has nothing to have a gap against."""
    if not stored or not server_hist:
        return False
    oldest_server = min(h["draw_time"] for h in server_hist)
    newest_stored = max(p["draw_time"] for p in stored)
    if oldest_server <= newest_stored:
        return False
    full = len(server_hist) >= HISTORY_CAP
    stale = last_sync is not None and synced_at is not None and synced_at - last_sync > HISTORY_DAYS * 86400
    return full or stale


def _counters(banners: list[dict], synced_at: int) -> list[dict]:
    rows = []
    for b in banners:
        rows.append({"gacha_id": b.get("gacha_id"), "synced_at": synced_at,
                     "gacha_point": b.get("gacha_point"),
                     "buttons": [{"id": x.get("gacha_button_id"), "drawn_count": x.get("drawn_count"),
                                  "limit_count": x.get("limit_count")}
                                 for x in b.get("gacha_buttons", [])]})
    return rows


def alias_gacha_ids(path: Path) -> list[str]:
    """The ids named by the hand-maintained banner_aliases.json (absent -> []). READ ONLY.

    Hand-maintained means hand-breakable: a half-typed JSON file is REPORTED and treated as
    empty, never allowed to abort a sync that has already written the store."""
    if not path.is_file():
        return []
    try:
        doc = json.loads(path.read_bytes().decode("utf8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        print(f"banner_aliases.json could not be read: {type(exc).__name__}: {exc}")
        return []
    if not isinstance(doc, dict):
        print("banner_aliases.json could not be read: top level is not an object")
        return []
    return [v["gacha_id"] for k, v in doc.items()
            if k != "_doc" and isinstance(v, dict) and v.get("gacha_id")]


def apply_sync(store_dir: Path, synced_at: int, list_resp: dict, hist_resp: dict,
               rates: dict[str, dict]) -> dict:
    files = {k: store_dir / n for k, n in
             (("pulls", gs.PULLS), ("banners", gs.BANNERS), ("rates", gs.RATES), ("counters", gs.COUNTERS))}
    before = {k: gs.count_lines(p) for k, p in files.items()}
    live = gw.live_banners(list_resp)
    gs.append_jsonl(files["banners"], gs.banner_rows(live, gs.read_jsonl(files["banners"]), synced_at))
    stored_pulls = gs.read_jsonl(files["pulls"])
    last_sync = max((r.get("synced_at") or 0 for r in gs.read_jsonl(files["counters"])), default=0) or None
    gap = history_gap(hist_resp.get("histories", []), stored_pulls, last_sync, synced_at)
    gs.append_jsonl(files["pulls"], gs.new_pulls(hist_resp.get("histories", []),
                                                 stored_pulls, live, synced_at))
    gs.append_jsonl(files["rates"], [{"gacha_id": gid, "synced_at": synced_at, **tables}
                                     for gid, tables in rates.items()])
    gs.append_jsonl(files["counters"], _counters(live, synced_at))
    snap_dir = store_dir / gs.SNAPSHOTS
    snap_dir.mkdir(parents=True, exist_ok=True)
    with open(snap_dir / f"{synced_at}.json", "ab") as fh:  # fresh name per sync; append mode by rule
        fh.write(json.dumps({"synced_at": synced_at, "list": list_resp, "history": hist_resp},
                            ensure_ascii=False, sort_keys=True).encode("utf8"))
    after = {k: gs.count_lines(p) for k, p in files.items()}
    out = {k: after[k] - before[k] for k in files}
    out["possible_gap"] = gap
    if gap:
        print(f"⚠ POSSIBLE GAP: the server's history (the last {HISTORY_DAYS} days, at most "
              f"{HISTORY_CAP} pulls) starts after everything stored -- pulls drawn between the "
              "last sync and that window are not recoverable from the wire.")
    return out


def fetch_all(host, token, ident, mver) -> tuple[dict, dict, dict]:
    pt, err = gw.fire(host, gw.LIST, b"", token, ident, mver)
    if pt is None:
        raise SystemExit(f"Gacha/List failed: {err}")
    list_resp = gw.decode(pt, gw.LIST_RESP)
    pt, err = gw.fire(host, gw.HISTORY, b"", token, ident, mver)
    if pt is None:
        raise SystemExit(f"Gacha/ListHistory failed: {err}")
    hist_resp = gw.decode(pt, gw.HIST_RESP)
    rates: dict[str, dict] = {}
    for b in gw.live_banners(list_resp):
        gid = b["gacha_id"]
        req = gw.gacha_id_request(gid)
        row = {}
        for tag, path in (("normal", gw.NORMAL_PROB), ("card_select", gw.SELECT_PROB)):
            pt, err = gw.fire(host, path, req, token, ident, mver)
            row[tag] = gw.decode(pt, gw.PROB_RESP) if pt is not None else {"error": err}
            time.sleep(0.25)
        rates[gid] = row
    return list_resp, hist_resp, rates


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-art", action="store_true")
    ap.add_argument("--store", default=str(gs.STORE_DIR))
    args = ap.parse_args()
    store = Path(args.store)
    synced_at = int(time.time())

    host, cred = snap.load_credential("main")
    ident = snap.client_headers("main")
    try:
        mver = snap.fetch_master_version(host, ident)
    except snap.GameMaintenance:
        print("GAME IN MAINTENANCE -- gacha not synced; sync again once it ends")
        sys.exit(3)
    token = snap.login(host, cred, ident, mver)
    print(f"login OK ({host})")
    list_resp, hist_resp, rates = fetch_all(host, token, ident, mver)
    live = gw.live_banners(list_resp)
    print(f"live banners: {[b['gacha_id'] for b in live]}")
    print(f"history entries from server: {len(hist_resp.get('histories', []))}")
    summary = apply_sync(store, synced_at, list_resp, hist_resp, rates)
    summary["synced_at"] = synced_at
    # ART IS NEVER FATAL. The store is already written by here; the art is a convenience on
    # top of it, and a missing Octo catalogue or an unreadable bundle must not cost the user
    # the summary of the sync that just succeeded. Any failure is NAMED in `art_error`.
    if not args.no_art:
        try:
            import gacha_art  # noqa: PLC0415
            art_dir = store / gs.ART
            summary["art"] = gacha_art.ensure_art([b["gacha_id"] for b in live], art_dir)
            # Closed banners: the server refuses their rate tables but the Octo catalogue still
            # ships their art. banner_aliases.json is HAND-maintained -- read only, never written.
            alias_ids = alias_gacha_ids(store / gs.ALIASES)
            if alias_ids:
                summary["art_aliases"] = gacha_art.ensure_art(alias_ids, art_dir)
        except Exception as exc:  # noqa: BLE001 -- reported in the summary, never fatal
            summary["art_error"] = f"{type(exc).__name__}: {exc}"
            print(f"art step failed: {summary['art_error']}")
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
