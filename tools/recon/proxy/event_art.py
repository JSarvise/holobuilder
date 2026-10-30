"""The open events' NAME, kind, times and banner art -> dumps/event_meta.json + dumps/event_art/.

Sources:

* `dumps/event_info_live.bin` -- the decrypted `Event/ListEventInfo` response the refresh already
  fetches (`account_snapshot.py --refresh-events`). `EventListEventInfoResponse.EventInfo`
  (`dumps/proto_rpc/merged.proto`) carries `event_id`=1, `type`=2, `name`=3 (this is the
  KIND label, "Point Rally Event" / "Score Challenge Event", the same words the banner prints under
  the logo), `start_time`=4, `end_time`=5, `exchange_end_time`=6 (ms since the epoch),
  `logo_asset_id`=7, `background_asset_id`=8, and `marathon_info`=14 whose `marathon_chapters`=4
  each carry `id`=1, `chapter_number`=2, `end_time`=4, `background_asset_id`=13.
* The event's real TITLE is not on the wire. It is the event POSTER's name in the master:
  `Poster` row `poster-<chapter id>` -> its lang id (field 2) -> `LangPoster_Eng` text (field 1000).
  For normal-001 and normal-003 that text equals the event-story chapter title, and for
  normal-007 it ("Neon Sunshine Festival") is the text drawn in the event logo. Fallback:
  `LangEventStoryChapter_Eng` `la-event_story_chapter-<chapter id>`. Tables come from the
  refresh's own master export (`dumps/holodori/main_<T>.json`, its `level_tables` step), else a
  hololive-dreams-master dump (`data/tables/<T>.json`, HB_MASTER_REPO or the sibling folder).
* Banner art: the Octo catalogue names `img_banner_full_<event id>[_lang-eng]` and the logo asset
  id from the wire (`img_marathon_event_normal_logo-007-01[_lang-eng]`). Fetched with
  `octo_fetch.fetch` (md5-checked, 1 req/s, cached in dumps/octo_fetched), extracted with UnityPy
  on `octo_crypto.decrypt_bundle` -- the `gacha_art.py` pipeline -- and saved as WEBP <= 1280 px:
  `dumps/event_art/<event id>.webp` (banner) and `<event id>_logo.webp`.

NOTHING here fails the refresh. A missing source is a printed line and a null in the JSON; the
script exits 0 unless its own arguments are wrong.

    proxyenv/Scripts/python event_art.py            # every open marathon event
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import atomic_io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

DUMPS = HERE / "dumps"
EVENT_INFO = DUMPS / "event_info_live.bin"
OUT_JSON = DUMPS / "event_meta.json"
ART_DIR = DUMPS / "event_art"
# the refresh's own master export (named fields), then a master-data dump (numbered fields)
MASTER_TABLES = (DUMPS / "holodori",
                 Path(os.environ.get("HB_MASTER_REPO") or HERE.parents[2].parent / "hololive-dreams-master")
                 / "data" / "tables")
FETCHED = DUMPS / "octo_fetched"

# enums.EventType (merged.proto). Only marathons get art: mission events have no banner_full.
EVENT_TYPE = {1: "marathon_normal", 2: "marathon_relay", 3: "mission"}
MARATHON_TYPES = (1, 2)


# --- the wire ------------------------------------------------------------------------------
def _varint(b: bytes, p: int) -> tuple[int, int]:
    r = s = 0
    while True:
        c = b[p]
        p += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, p


def _fields(b: bytes) -> dict[int, list]:
    """flat protobuf -> {field: [values]} (varint -> int, length-delimited -> bytes)."""
    out: dict[int, list] = {}
    p = 0
    while p < len(b):
        tag, p = _varint(b, p)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            v, p = _varint(b, p)
        elif wt == 2:
            n, p = _varint(b, p)
            v = b[p:p + n]
            p += n
        elif wt == 5:
            v, p = int.from_bytes(b[p:p + 4], "little"), p + 4
        elif wt == 1:
            v, p = int.from_bytes(b[p:p + 8], "little"), p + 8
        else:
            raise ValueError(f"wire type {wt} at {p}")
        out.setdefault(fn, []).append(v)
    return out


def _s(rec: dict, fn: int) -> str | None:
    v = rec.get(fn, [None])[0]
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else None


def _i(rec: dict, fn: int) -> int | None:
    v = rec.get(fn, [None])[0]
    return v if isinstance(v, int) else None


def iso(ms: int | None) -> str | None:
    """ms since the epoch -> ISO 8601 UTC, or None."""
    if not ms:
        return None
    return _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc).isoformat()


def parse_event_info(blob: bytes) -> list[dict]:
    """Every `EventInfo` in a decrypted `Event/ListEventInfo` response."""
    events = []
    for raw in _fields(blob).get(1, []):
        if not isinstance(raw, bytes):
            continue
        ev = _fields(raw)
        chapters = []
        for mi in ev.get(14, []):
            for ch_raw in _fields(mi).get(4, []):
                ch = _fields(ch_raw)
                chapters.append({"id": _s(ch, 1), "number": _i(ch, 2),
                                 "ends_at": iso(_i(ch, 4)),
                                 "background_asset_id": _s(ch, 13)})
        etype = _i(ev, 2)
        events.append({"event_id": _s(ev, 1), "type": EVENT_TYPE.get(etype, etype),
                       "kind_label": _s(ev, 3), "starts_at": iso(_i(ev, 4)),
                       "ends_at": iso(_i(ev, 5)), "exchange_ends_at": iso(_i(ev, 6)),
                       "logo_asset_id": _s(ev, 7), "background_asset_id": _s(ev, 8),
                       "chapters": chapters, "_type_code": etype})
    return events


# --- the title (master tables) --------------------------------------------------------------
def _rows(tables, name: str) -> dict[str, dict]:
    """{id: data} of one master table, from the first of `tables` (one folder or several) that
    has it: as the refresh's own export (`main_<T>.json`) or a master dump (`<T>.json`)."""
    for folder in (tables if isinstance(tables, (list, tuple)) else (tables,)):
        for path in (folder / f"main_{name}.json", folder / f"{name}.json"):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rows = doc.get("rows") if isinstance(doc, dict) else doc
            return {r.get("id"): (r.get("data") or {}) for r in rows or [] if isinstance(r, dict)}
    return {}


def chapter_titles(tables: Path, chapter_ids: list[str]) -> dict[str, str]:
    """chapter id -> English title: the Poster's name, else the event-story chapter title."""
    posters = _rows(tables, "Poster")
    poster_lang = _rows(tables, "LangPoster_Eng")
    story_lang = _rows(tables, "LangEventStoryChapter_Eng")
    out = {}
    for cid in chapter_ids:
        # a master dump numbers the fields; the refresh's own export names them
        poster = posters.get(f"poster-{cid}") or {}
        lang_id = poster.get("2") or poster.get("nameLangId")
        lang = poster_lang.get(lang_id) or {} if lang_id else {}
        text = lang.get("1000") or lang.get("text")
        if not text:
            story = story_lang.get(f"la-event_story_chapter-{cid}") or {}
            text = story.get("1000") or story.get("text")
        if text:
            out[cid] = text
    return out


# --- the art (Octo CDN) ---------------------------------------------------------------------
def art_names(event: dict) -> dict[str, list[str]]:
    eid = event["event_id"]
    logo = event.get("logo_asset_id")
    return {"banner": [f"img_banner_full_{eid}_lang-eng", f"img_banner_full_{eid}"],
            "logo": [f"{logo}_lang-eng", logo] if logo else []}


def art_files(event_id: str) -> dict[str, str]:
    return {"banner": f"{event_id}.webp", "logo": f"{event_id}_logo.webp"}


def _default_fetch(names: list[str]) -> None:
    import octo_fetch  # noqa: PLC0415
    octo_fetch.fetch(names)


def _default_extract(bundle_path: Path, name: str):
    import gacha_art  # noqa: PLC0415 -- the same UnityPy + octo_crypto extraction
    return gacha_art._default_extract(bundle_path, name)


def _save_webp(img, dst: Path) -> None:
    import gacha_art  # noqa: PLC0415 -- <= 1280 px wide, WEBP q85
    gacha_art._save_webp(img, dst)


def ensure_art(events: list[dict], art_dir: Path, fetch=None, extract=None,
               fetched: Path = FETCHED) -> dict[str, dict[str, str | None]]:
    """event id -> {kind: file name or None}. Only kinds missing on disk are fetched."""
    fetch = fetch or _default_fetch
    extract = extract or _default_extract
    result: dict[str, dict[str, str | None]] = {}
    for ev in events:
        eid = ev["event_id"]
        names, files = art_names(ev), art_files(eid)
        got: dict[str, str | None] = {}
        todo = [k for k in names if names[k] and not (art_dir / files[k]).is_file()]
        if todo:
            try:
                fetch([n for k in todo for n in names[k]])
            except (Exception, SystemExit) as exc:  # noqa: BLE001 -- reported, never fatal
                print(f"event art {eid}: fetch failed: {type(exc).__name__}: {exc}")
        for kind in names:
            if (art_dir / files[kind]).is_file():
                got[kind] = files[kind]
                continue
            got[kind] = None
            for n in names[kind]:
                try:
                    img = extract(fetched / f"{n}.bundle", n)
                except Exception as exc:  # noqa: BLE001 -- one bad bundle must not stop the rest
                    print(f"event art {eid}: {n}: {type(exc).__name__}: {exc}")
                    img = None
                if img is None:
                    continue
                _save_webp(img, art_dir / files[kind])
                got[kind] = files[kind]
                break
        result[eid] = got
    return result


# --- the file -------------------------------------------------------------------------------
def build(blob: bytes, tables: Path, art_dir: Path, *, fetch=None, extract=None,
          fetched: Path = FETCHED, fetched_at: float | None = None) -> dict:
    events = [e for e in parse_event_info(blob) if e.get("_type_code") in MARATHON_TYPES
              and e.get("event_id")]
    titles = chapter_titles(tables, [c["id"] for e in events for c in e["chapters"] if c["id"]])
    art = ensure_art(events, art_dir, fetch=fetch, extract=extract, fetched=fetched)
    out = []
    for e in events:
        e = {k: v for k, v in e.items() if not k.startswith("_")}
        for c in e["chapters"]:
            c["title"] = titles.get(c["id"])
        first = next((c for c in sorted(e["chapters"], key=lambda c: c["number"] or 0)
                      if c["title"]), None)
        e["title"] = first["title"] if first else None
        e["art"] = art.get(e["event_id"], {})
        out.append(e)
    return {"source": "Event/ListEventInfo + master Poster/LangPoster_Eng + Octo banner_full",
            "fetched_at": fetched_at, "events": out}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        print(__doc__)
        return 2
    try:
        blob = EVENT_INFO.read_bytes()
    except OSError:
        print(f"event art: no {EVENT_INFO.name} (the account step fetches it) -- skipped")
        return 0
    try:
        doc = build(blob, MASTER_TABLES, ART_DIR, fetched_at=EVENT_INFO.stat().st_mtime)
    except Exception as exc:  # noqa: BLE001 -- never fail the refresh over art
        print(f"event art: could not build ({type(exc).__name__}: {exc}) -- skipped")
        return 0
    atomic_io.write_text(OUT_JSON, json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    for e in doc["events"]:
        missing = [k for k, v in e["art"].items() if not v]
        print(f"event {e['event_id']}: {e['title'] or '(NO TITLE in the master)'} -- "
              f"{e['kind_label']}, {e['starts_at']} .. {e['ends_at']}; art "
              f"{'ok' if not missing else 'MISSING ' + ', '.join(missing)}")
    print(f"written {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
