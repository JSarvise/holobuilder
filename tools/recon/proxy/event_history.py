"""The account's EVENT HISTORY: every marathon event seen, with its result and the tier borders.

The web shows the current event and, from its banner, the earlier ones. Nothing in the game
keeps this history:

* `Event/ListEventInfo` lists an event only until its exchange closes, and `event_meta.json`
  is rewritten by every refresh -- so an event's title, dates and art are ARCHIVED here while
  it is listed;
* the `*RankingGrade` RPCs answer only while the event is in term, and `Marathon/Top` gives
  the account's Event Pt but an empty `ranking_result` -- so its rank and the final borders are
  what the local cutoff reads stored (`dumps/cutoffs/<region>/`, `event_cutoffs.py`);
* for events older than those reads, the ranking-reward EMBLEM the account holds says which
  tier it reached (`emblem-marathon_score_ranking_rank_reward-<chapter>-<grade>`, from
  `User/Get`'s `user_emblem_list`), with no rank, points or dates.

So this merges three local sources into `dumps/event_history.json`, ADDITIVELY: an event once
seen is never removed and a known value is only replaced by a better one (a later read of the
account's rank, a final over a non-final). Each value carries its `source`. No network.

    proxyenv/Scripts/python event_history.py
"""
from __future__ import annotations

import json
import os
import atomic_io
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DUMPS = HERE / "dumps"
OUT = DUMPS / "event_history.json"
EVENT_META = DUMPS / "event_meta.json"
CUTOFFS = DUMPS / "cutoffs"
SNAPSHOT = DUMPS / "account_snapshot_main.json"
# the final borders of events older than the local reads, per region, read by eye off the
# community tool holodori.best's leaderboard page; global, published with the game data.
# Hand-recorded, never fetched by code.
FINALS = DUMPS / "event_finals.json"
# what is PUBLISHED with the game data: this history without anyone's result (publishing one
# account's event_history.json would show its ranks, Event Pt and emblems in every other
# user's Past events). The desktop app builds its own history on top of it, from its own
# account, at every refresh.
ARCHIVE = DUMPS / "event_archive.json"
PERSONAL = ("self", "event_pt", "emblems")


def _public_chapter(ch: dict) -> dict:
    """A chapter without the account's own results -- its song rankings' too."""
    out = {k: v for k, v in ch.items() if k not in PERSONAL}
    if isinstance(out.get("songs"), dict):
        out["songs"] = {m: {k: v for k, v in song.items() if k not in PERSONAL}
                        for m, song in out["songs"].items()}
    return out


def archive_of(doc: dict) -> dict:
    """`doc` with every account's own result removed: titles, dates, tiers, final borders."""
    return {"events": {eid: {**{k: v for k, v in e.items() if k != "chapters"},
                             "chapters": {cid: _public_chapter(ch)
                                          for cid, ch in (e.get("chapters") or {}).items()}}
                       for eid, e in (doc.get("events") or {}).items()}}
CREDENTIAL_REGION_DEFAULT = "us"

GRADES = ("dream", "diamond", "platinum", "gold", "silver", "bronze")   # best first
_EMBLEM = re.compile(r"^emblem-(marathon_score_ranking_rank_reward|total_music_highest_score_reward)"
                     r"-(marathon_event-([a-z]+)-(\d+))-chapter-(\d+)-([a-z]+)$")
EVENT_FIELDS = ("type", "kind_label", "title", "starts_at", "ends_at", "exchange_ends_at", "art")


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def emblem_results(emblems: list[dict]) -> dict[str, dict]:
    """chapter id -> {grade, ranking, event_id} from the ranking-reward emblems held (the best
    grade when several are held for one chapter and ranking)."""
    out: dict[str, dict] = {}
    for e in emblems or []:
        m = _EMBLEM.match(str(e.get("emblem_id") or ""))
        if not m or m.group(6) not in GRADES:
            continue
        ranking = "points" if m.group(1).startswith("marathon") else "total"
        cid = f"{m.group(2)}-chapter-{m.group(5)}"
        key = f"{cid}|{ranking}"
        prev = out.get(key)
        if prev is None or GRADES.index(m.group(6)) < GRADES.index(prev["grade"]):
            out[key] = {"chapter_id": cid, "event_id": m.group(2), "ranking": ranking,
                        "grade": m.group(6), "source": "emblem held (User/Get user_emblem_list)"}
    return out


def song_results(recs: list[dict], meta: dict) -> dict[str, dict]:
    """music id -> {tiers, self, final_borders} from a chapter's `music:<id>` reads."""
    out: dict[str, dict] = {}
    music_tiers = (meta.get("tiers") or {}).get("music") or {}
    for mid in sorted({r["kind"][6:] for r in recs if str(r.get("kind", "")).startswith("music:")}):
        mine = [r for r in recs if r.get("kind") == f"music:{mid}"]
        own = next((r for r in reversed(mine) if r.get("self")
                    and (r["self"].get("rank") or r["self"].get("out_of_range"))), None)
        fin = next((r for r in reversed(mine) if r.get("final")), None)
        out[mid] = {
            "tiers": [{"rank": t.get("end_rank"), "grade_id": t.get("grade_id")} for t in music_tiers.get(mid) or []],
            "self": ({"rank": own["self"].get("rank"), "score": own["self"].get("score"),
                      "out_of_range": bool(own["self"].get("out_of_range")),
                      "captured_at": own["captured_at"], "final": bool(own.get("final"))} if own else None),
            "final_borders": ({"captured_at": fin["captured_at"], "borders": fin.get("borders") or []}
                              if fin else None),
        }
    return out


def cutoff_results(store: Path, region: str) -> dict[str, dict]:
    """chapter id -> what the local reads say: tiers, the account's last rank, the final borders."""
    out: dict[str, dict] = {}
    d = store / region
    if not d.is_dir():
        return out
    for meta_path in sorted(d.glob("*.meta.json")):
        meta = _load(meta_path, None)
        if not isinstance(meta, dict) or not meta.get("chapter_id"):
            continue
        cid = meta["chapter_id"]
        recs = []
        path = d / f"{cid}.jsonl"
        if path.is_file():
            for line in path.read_text("utf-8").splitlines():
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    continue
        recs.sort(key=lambda r: r.get("captured_at") or 0)
        ranking = "total" if meta.get("points_ranking_disabled") else "points"
        mine = [r for r in recs if r.get("kind") == ranking]
        own = next((r for r in reversed(mine) if r.get("self")
                    and (r["self"].get("rank") or r["self"].get("out_of_range"))), None)
        fin = next((r for r in reversed(mine) if r.get("final")), None)
        out[cid] = {
            "event_id": meta.get("event_id"), "number": meta.get("chapter_number"),
            "ranking": ranking, "region": region,
            "start_time": meta.get("start_time"), "end_time": meta.get("chapter_end_time"),
            "tiers": [{"rank": t.get("end_rank"), "grade_id": t.get("grade_id")}
                      for t in (meta.get("tiers") or {}).get(ranking) or []],
            "self": ({"rank": own["self"].get("rank"), "score": own["self"].get("score"),
                      "out_of_range": bool(own["self"].get("out_of_range")),
                      "captured_at": own["captured_at"], "final": bool(own.get("final")),
                      "source": "own cutoff read (Marathon *RankingGrade self)"} if own else None),
            "final_borders": ({"captured_at": fin["captured_at"], "borders": fin.get("borders") or [],
                               "source": "own cutoff read after the end"} if fin else None),
            # each song ranking of the chapter (one per part of a Spotlight event): its tiers,
            # the account's best read, the final borders
            "songs": song_results(recs, meta),
        }
    for path in sorted(d.glob("*.self_points.jsonl")):
        last = None
        for line in path.read_text("utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if last is None or (rec.get("captured_at") or 0) >= (last.get("captured_at") or 0):
                last = rec
        for cid, pts in ((last or {}).get("chapters") or {}).items():
            row = out.setdefault(cid, {"event_id": last.get("event_id"), "region": region})
            row["event_pt"] = {"value": pts, "captured_at": last.get("captured_at"),
                               "source": "Marathon/Top (the owner's own Event Pt)"}
    return out


def recorded_finals(doc: dict, region: str) -> dict[str, dict]:
    """chapter id -> {event_id, ranking, final_borders} for `region` from `event_finals.json`."""
    out: dict[str, dict] = {}
    source = doc.get("source") or "recorded final borders"
    for eid, ev in (doc.get("events") or {}).items():
        for cid, ch in (ev.get("chapters") or {}).items():
            borders = ((ch.get("regions") or {}).get(region) or {}).get("borders")
            if borders:
                out[cid] = {"event_id": eid, "ranking": ch.get("ranking"),
                            "kind_label": ev.get("kind_label"), "holomem": ch.get("holomem"),
                            "final_borders": {"borders": borders, "source": source,
                                              "read_at": doc.get("read_at"),
                                              "updated": ((ch.get("regions") or {}).get(region) or {}).get("updated")}}
    return out


def past_art(doc: dict, art_dir: Path, ensure=None) -> int:
    """Banner + logo of every past event on record that has none: the Octo catalogue keeps
    `img_banner_full_<event id>` and `img_marathon_event_<kind>_logo-<NNN>-01` for every
    event, listed or not. Fetched by `event_art.ensure_art`, the open events' own path.
    Returns how many events got art."""
    todo = []
    for eid, e in (doc.get("events") or {}).items():
        if (e.get("art") or {}).get("banner"):
            continue
        m = re.match(r"marathon_event-(normal|relay)-(\d{3})$", eid)
        if m:
            todo.append({"event_id": eid, "logo_asset_id": f"img_marathon_event_{m.group(1)}_logo-{m.group(2)}-01"})
    if not todo:
        return 0
    if ensure is None:
        import event_art  # noqa: PLC0415
        ensure = event_art.ensure_art
    got = ensure(todo, art_dir)
    n = 0
    for eid, art in got.items():
        if art.get("banner"):
            doc["events"][eid]["art"] = {k: v for k, v in art.items() if v}
            n += 1
    return n


def _better_self(old: dict | None, new: dict | None) -> dict | None:
    """A final read beats a non-final one; between equals, the later read."""
    if not new:
        return old
    if not old:
        return new
    if bool(new.get("final")) != bool(old.get("final")):
        return new if new.get("final") else old
    return new if (new.get("captured_at") or 0) >= (old.get("captured_at") or 0) else old


def merge(history: dict, *, events: list[dict], cutoffs: dict[str, dict],
          emblems: dict[str, dict], titles: dict[str, str] | None = None,
          finals: dict[str, dict] | None = None) -> dict:
    """The new history: `history` with every source folded in, nothing removed."""
    titles = titles or {}
    out = {"events": {k: dict(v) for k, v in (history.get("events") or {}).items()}}

    def event(eid: str) -> dict:
        e = out["events"].setdefault(eid, {"event_id": eid, "chapters": {}})
        e["chapters"] = {k: dict(v) for k, v in (e.get("chapters") or {}).items()}
        return e

    def chapter(e: dict, cid: str) -> dict:
        return e["chapters"].setdefault(cid, {"chapter_id": cid})

    for ev in events or []:                      # the listed events: archive their facts
        if not ev.get("event_id"):
            continue
        e = event(ev["event_id"])
        for k in EVENT_FIELDS:
            if ev.get(k) not in (None, "", {}, []):
                e[k] = ev[k]
        for c in ev.get("chapters") or []:
            if c.get("id"):
                ch = chapter(e, c["id"])
                for k, v in (("number", c.get("number")), ("ends_at", c.get("ends_at")),
                             ("title", c.get("title"))):
                    if v not in (None, ""):
                        ch[k] = v
    for cid, r in cutoffs.items():               # the local reads: its rank, the final borders
        if not r.get("event_id"):
            continue
        e = event(r["event_id"])
        ch = chapter(e, cid)
        for k in ("number", "ranking", "region", "start_time", "end_time", "tiers"):
            if r.get(k) not in (None, "", []):
                ch[k] = r[k]
        ch["self"] = _better_self(ch.get("self"), r.get("self"))
        if r.get("event_pt") and ((ch.get("event_pt") or {}).get("captured_at") or 0) <= (
                r["event_pt"].get("captured_at") or 0):
            ch["event_pt"] = r["event_pt"]
        if r.get("final_borders"):
            ch["final_borders"] = r["final_borders"]
        for mid, song in (r.get("songs") or {}).items():   # a song ranking: newest read wins
            have = ch.setdefault("songs", {}).setdefault(mid, {})
            for k in ("tiers", "final_borders"):
                if song.get(k):
                    have[k] = song[k]
            have["self"] = _better_self(have.get("self"), song.get("self"))
    for cid, r in (finals or {}).items():        # recorded finals: only where none was read
        ch = chapter(event(r["event_id"]), cid)
        if not (ch.get("final_borders") or {}).get("borders") or ch["final_borders"].get("read_at"):
            ch["final_borders"] = r["final_borders"]
        if r.get("ranking") and not ch.get("ranking"):
            ch["ranking"] = r["ranking"]
        if not ch.get("number") and cid[-2:].isdigit():
            ch["number"] = int(cid[-2:])
        if r.get("holomem") and not ch.get("title"):
            ch["title"] = r["holomem"]
        if r.get("kind_label") and not out["events"][r["event_id"]].get("kind_label"):
            out["events"][r["event_id"]]["kind_label"] = r["kind_label"]
    for key, r in emblems.items():               # the emblems: the tier of older events
        e = event(r["event_id"])
        ch = chapter(e, r["chapter_id"])
        ch.setdefault("emblems", {})[r["ranking"]] = {"grade": r["grade"], "source": r["source"]}
    for eid, e in out["events"].items():         # a title the master knows, for an event never listed
        if not e.get("title"):
            for cid in sorted(e["chapters"]):
                if titles.get(cid):
                    e["title"] = titles[cid]
                    break
    return out


def build() -> tuple[dict, bool]:
    """(the history document, built for the desktop app) from what is on disk -- writes
    nothing. The refresh's `main` writes it; the web server's Past events builds it per request
    (a chapter's final borders, pulled from the tracker every 5 min, must reach every player
    without a refresh)."""
    region = CREDENTIAL_REGION_DEFAULT
    try:
        import account_snapshot as asnap  # noqa: PLC0415
        region = asnap.load_credential("main")[0].split(".", 1)[0] or region
    except (Exception, SystemExit):  # noqa: BLE001 -- US by default; no credential is not fatal
        pass
    meta = _load(EVENT_META, {})
    snap = _load(SNAPSHOT, {})
    emblems = emblem_results(snap.get("emblems") or [])
    cut = cutoff_results(CUTOFFS, region)
    titles = {}
    try:
        import event_art  # noqa: PLC0415
        ids = sorted({r["chapter_id"] for r in emblems.values()} | set(cut))
        titles = event_art.chapter_titles(event_art.MASTER_TABLES, ids)
    except Exception:  # noqa: BLE001
        pass
    # the desktop app starts from the published archive, never from a history it holds:
    # one copied from elsewhere would carry someone else's results
    app = os.environ.get("HB_APP_MODE") == "1"
    base = archive_of(_load(ARCHIVE, {})) if app else _load(OUT, {})
    doc = merge(base, events=meta.get("events") or [], cutoffs=cut,
                emblems=emblems, titles=titles,
                finals=recorded_finals(_load(FINALS, {}), region))
    if app:
        doc["built_for"] = "this account"          # the app's start checks for it
    return doc, app


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        print(__doc__)
        return 2
    try:
        doc, app = build()
    except Exception as exc:  # noqa: BLE001 -- never fail the refresh over the history
        print(f"event history: could not build ({type(exc).__name__}: {exc}) -- skipped")
        return 0
    if not app:                                  # the app has no game catalogue; it gets the files
        try:
            got = past_art(doc, DUMPS / "event_art")
            if got:
                print(f"event history: banner art for {got} past event(s)")
        except Exception as exc:  # noqa: BLE001 -- art is never worth failing the history
            print(f"event history: past banners skipped ({type(exc).__name__}: {exc})")
    atomic_io.write_bytes(OUT, json.dumps(doc, indent=1, ensure_ascii=False).encode("utf-8"))
    if not app:
        atomic_io.write_bytes(ARCHIVE, json.dumps(archive_of(doc), indent=1, ensure_ascii=False).encode("utf-8"))
    for eid in sorted(doc["events"], reverse=True):
        e = doc["events"][eid]
        bits = []
        for cid in sorted(e["chapters"]):
            ch = e["chapters"][cid]
            s = ch.get("self")
            if s:
                where = f"#{s['rank']:,}" if s.get("rank") else "outside every tier"
                bits.append(f"{where}{' (final)' if s.get('final') else ''}")
            if ch.get("event_pt"):
                bits.append(f"{ch['event_pt']['value']:,} Event Pt")
            for rk, em in (ch.get("emblems") or {}).items():
                bits.append(f"{em['grade']} emblem ({rk})")
        print(f"history {eid}: {e.get('title') or '(no title)'} -- {'; '.join(bits) or 'no result on record'}")
    print(f"written {OUT} ({len(doc['events'])} events, emblems "
          f"{'read' if _load(SNAPSHOT, {}).get('emblems') is not None else 'not in the snapshot yet'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
