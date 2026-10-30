"""Event ranking CUTOFFS: read each reward tier's border score, per region, into a local series.

What it does
------------
For each region (US / AS / JP) it logs in with that region's read-only GUEST account
(`state/ranking_accounts.json`, never the user's own account for a foreign region) and makes four
calls, one per second:

    Master/Get -> Auth/Login -> Event/ListEventInfo -> Marathon/List*RankingGrade

`ListEventInfo` names the open marathon chapters and their reward tiers (`end_rank` + grade id:
500 Dream / 1000 Diamond / 5000 Platinum / 10000 Gold / 25000 Silver / 50000 Bronze on
`marathon_event-normal-007`). The `*Grade` RPC returns ONE `rank_info` per tier boundary -- the
player sitting exactly on the border -- plus the caller's own rank. That is the cutoff.

Each read is APPENDED as one JSON line to
`dumps/cutoffs/<region>/<chapter_id>.jsonl` (gitignored), and the chapter's metadata (times,
tiers) is written beside it as `<chapter_id>.meta.json`. Only rank and score are kept: no
names, no user ids, no profiles -- a cutoff needs none of them.

`--self` additionally reads the USER's own rank in their region with their own account
(`Grade` carries `self_rank` / `self_score`). Off by default: a headless login on the
main account can end a running game session.

DATE_CHANGED: a GUEST whose last home login is from an earlier game day is refused; the
fetcher then does what the client does -- one `Home/Login` (guest-only,
`account_snapshot.guest_home_login`) -- and retries. The user's own account is never sent
Home/Login: with `--self`, a DATE_CHANGED on it falls back to the region's guest for the
borders (the user's rank is then skipped until they open the game that day).

Not a poller itself. It runs once per invocation: the web page's `POST /api/event/cutoffs/sync`
runs it on a click, and the optional local poller (`tools/web/cutoffs_poller.py`,
off by default) runs it -- guests only, never `--self` -- every few minutes while on.

    proxyenv/Scripts/python event_cutoffs.py                 all regions, guests only
    proxyenv/Scripts/python event_cutoffs.py --region us --self
    proxyenv/Scripts/python event_cutoffs.py --songs         also per-song borders
"""
from __future__ import annotations

import argparse
import json
import atomic_io
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

STORE = HERE / "dumps" / "cutoffs"
REGIONS = ("us", "as", "jp")
PACE_S = 1.0  # project etiquette: <= 1 request per second, sequential

RPC_POINTS = "/rpc.api.Marathon/ListMarathonScoreRankingGrade"
RPC_TOTAL = "/rpc.api.Marathon/ListTotalMusicHighestScoreRankingGrade"
RPC_MUSIC = "/rpc.api.Marathon/ListMusicHighestScoreRankingGrade"
RPC_TOP = "/rpc.api.Marathon/Top"


# ------------------------------------------------------------------ protobuf (pure)
def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while i < len(b):
        r |= (b[i] & 0x7F) << s
        s += 7
        i += 1
        if not (b[i - 1] & 0x80):
            return r, i
        if s > 70:
            break
    raise ValueError("bad varint")


def _write_varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def msg(b: bytes) -> dict[int, list]:
    """One protobuf message as {field_no: [values]} (varints as int, length-delimited as bytes)."""
    out: dict[int, list] = {}
    i = 0
    while i < len(b):
        tag, i = _read_varint(b, i)
        fn, wt = tag >> 3, tag & 7
        if fn == 0:
            raise ValueError("field 0")
        if wt == 0:
            v, i = _read_varint(b, i)
        elif wt == 2:
            ln, i = _read_varint(b, i)
            if i + ln > len(b):
                raise ValueError("overrun")
            v, i = b[i:i + ln], i + ln
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"wire type {wt}")
        out.setdefault(fn, []).append(v)
    return out


def _s(d: dict, fn: int) -> str | None:
    v = d.get(fn)
    return v[0].decode("utf-8", "replace") if v else None


def _i(d: dict, fn: int) -> int | None:
    v = d.get(fn)
    return int(v[0]) if v else None


def _sint(v: int | None) -> int | None:
    """int32/int64 varints are two's complement on the wire (a -1 arrives as 2**64-1)."""
    if v is None:
        return None
    return v - (1 << 64) if v >= 1 << 63 else v


def request_body(chapter_id: str, music_id: str | None = None) -> bytes:
    """`{marathon_chapter_id = 1[, music_id = 2]}` -- the Grade requests' only fields."""
    def field(fn: int, s: str) -> bytes:
        raw = s.encode()
        return _write_varint((fn << 3) | 2) + _write_varint(len(raw)) + raw
    return field(1, chapter_id) + (field(2, music_id) if music_id else b"")


def parse_event_info(blob: bytes) -> list[dict]:
    """`EventListEventInfoResponse` -> one dict per MARATHON chapter.

    Non-marathon events (missions) carry no `marathon_info` and are skipped."""
    chapters = []
    for raw in msg(blob).get(1, []):
        ev = msg(raw)
        if 14 not in ev:
            continue
        info = msg(ev[14][0])
        for craw in info.get(4, []):
            ch = msg(craw)

            def tiers(fn: int, rank_fn: int, grade_fn: int, music_fn: int | None = None):
                rows = []
                for rr in ch.get(fn, []):
                    r = msg(rr)
                    row = {"end_rank": _i(r, rank_fn), "grade_id": _s(r, grade_fn)}
                    if music_fn:
                        row["music_id"] = _s(r, music_fn)
                    rows.append(row)
                return sorted(rows, key=lambda r: (r.get("music_id") or "", r["end_rank"] or 0))

            music_rows = tiers(17, 3, 5, 2)
            music: dict[str, list] = {}
            for row in music_rows:
                music.setdefault(row.pop("music_id") or "?", []).append(row)
            chapters.append({
                "event_id": _s(ev, 1),
                "event_name": _s(ev, 3),
                "event_type": _i(ev, 2),
                "start_time": _i(ev, 4),
                "end_time": _i(ev, 5),
                "chapter_id": _s(ch, 1),
                "chapter_number": _i(ch, 2),
                "chapter_end_time": _i(ch, 4),
                "aggregation_start_time": _i(info, 2),
                "reveal_time": _i(ch, 15) or _i(info, 3),
                "points_ranking_disabled": bool(_i(info, 5)),
                "tiers": {
                    "points": tiers(16, 2, 4),
                    "total": tiers(18, 2, 4),
                    "music": music,
                },
            })
    # A Spotlight (relay) event's chapters run one after another -- each starts when the one
    # before it ends -- but the wire gives only the event's start: so a
    # chapter's own start is its predecessor's end, and a chapter not yet begun is not read
    prev_end: dict[str, int] = {}
    for ch in sorted(chapters, key=lambda c: (c["event_id"] or "", c["chapter_number"] or 0)):
        before = prev_end.get(ch["event_id"])
        if before:
            ch["start_time"] = before + 1000
        if ch.get("chapter_end_time"):
            prev_end[ch["event_id"]] = ch["chapter_end_time"]
    return chapters


def parse_ranking_result(blob: bytes) -> dict:
    """`*RankingGradeResponse` -> {self_rank, self_score, self_out_of_range, borders[]}.

    All three Grade responses wrap the same shape at field 1 (`MarathonScoreRankingResult`
    / `...TotalMusic...` / `...Music...`: 1 self_rank, 2 self_score, 3 is_self_rank_out_of_range,
    4 rank_infos{1 rank, 2 score, 3 user_info}). user_info is deliberately NOT read."""
    top = msg(blob)
    res = msg(top[1][0]) if top.get(1) else {}
    borders = []
    for rraw in res.get(4, []):
        r = msg(rraw)
        borders.append({"rank": _sint(_i(r, 1)), "score": _sint(_i(r, 2))})
    borders.sort(key=lambda b: b["rank"] or 0)
    return {
        "self_rank": _sint(_i(res, 1)),
        "self_score": _sint(_i(res, 2)),
        "self_out_of_range": bool(_i(res, 3)),
        "borders": borders,
    }


def final_after(ch: dict) -> int:
    """From this instant a read of the chapter is its FINAL standing: the reveal (the
    ranking is frozen and checked), else the chapter's end."""
    return int(ch.get("reveal_time") or ch.get("chapter_end_time") or ch.get("end_time") or 0)


def final_kinds(store: Path, region: str, chapter_id: str, *, with_self: bool = False) -> set[str]:
    """The ranking kinds ('points', 'total', 'music:<id>') a final read is stored for."""
    path = store / region / f"{chapter_id}.jsonl"
    out: set[str] = set()
    if not path.is_file():
        return out
    for line in path.read_text("utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("final") and (not with_self or rec.get("self")):
            out.add(rec.get("kind") or "points")
    return out


def has_final(store: Path, region: str, chapter_id: str, *, with_self: bool = False) -> bool:
    """A read flagged `final` is already stored for this chapter -- with `with_self`, one that
    also carries the user's own rank (the tracker's guest reads never do)."""
    path = store / region / f"{chapter_id}.jsonl"
    if not path.is_file():
        return False
    for line in path.read_text("utf-8").splitlines():
        try:
            rec = json.loads(line)
            if rec.get("final") and (not with_self or rec.get("self")):
                return True
        except ValueError:
            continue
    return False


def parse_top_points(blob: bytes) -> dict[str, int]:
    """`MarathonTopResponse` -> {chapter id: the caller's own Event Pt} (marathon=1
    -> marathon_chapters=12 -> {id=1, score=17}). Its `ranking_result` (field 2) comes back empty
    even for an ended event, so the rank stays the Grade RPCs' job."""
    top = msg(blob)
    m = msg(top[1][0]) if top.get(1) else {}
    out = {}
    for raw in m.get(12, []):
        c = msg(raw)
        cid, score = _s(c, 1), _sint(_i(c, 17))
        if cid and score is not None:
            out[cid] = score
    return out


def append_points(store: Path, region: str, event_id: str, captured_at: int, points: dict) -> Path:
    """The user's own Event Pt per chapter, appended to `<region>/<event_id>.self_points.jsonl`."""
    d = store / region
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{event_id}.self_points.jsonl"
    with path.open("ab") as fh:
        fh.write((json.dumps({"captured_at": captured_at, "event_id": event_id, "chapters": points},
                             separators=(",", ":")) + "\n").encode("utf-8"))
    return path


def live_chapters(chapters: list[dict], now_ms: int, *, store: Path | None = None,
                  region: str | None = None, need_self: bool = False,
                  songs: bool = False) -> list[dict]:
    """Chapters worth reading now: started, and not yet past the final-ranking reveal --
    plus, with `store`, an ENDED chapter the server still lists (until its exchange
    closes) whose final standing is not stored yet: read ONCE more for it.

    The finals are what a later event's prediction learns from. The Grade RPCs still answer
    for an ended chapter days after its end, so a read missed while nothing was polling is
    recovered here.

    `need_self` (the user's own read): an ended chapter is read again until a final read
    carrying THEIR rank is stored -- the tracker's final borders alone do not end it, or the
    page would show a mid-chapter rank as the result.

    `songs`: an ended chapter is also read again until every one of its kinds (points / total /
    music:<id>) has a final read -- the points finals alone do not end it, or a tracker that
    already holds the points finals would never read the song finals."""
    out = []
    for ch in chapters:
        start = ch.get("start_time") or 0
        stop = final_after(ch)
        if start <= now_ms <= stop:
            out.append(ch)
        elif now_ms > stop and store is not None and region is not None:
            done = final_kinds(store, region, ch["chapter_id"], with_self=need_self)
            if not done or (songs and any(k not in done for k, _r, _m in kinds_for(ch, True))):
                out.append(ch)
    return out


def kinds_for(ch: dict, songs: bool) -> list[tuple[str, str, str | None]]:
    """(kind, rpc, music_id) to read for one chapter. A Score Challenge chapter disables the
    points ranking (`is_marathon_score_ranking_disable`) and ranks the TOTAL of song scores."""
    out: list[tuple[str, str, str | None]] = []
    if ch["tiers"]["points"] and not ch.get("points_ranking_disabled"):
        out.append(("points", RPC_POINTS, None))
    if ch["tiers"]["total"]:
        out.append(("total", RPC_TOTAL, None))
    if songs:
        for music_id in sorted(ch["tiers"]["music"]):
            out.append((f"music:{music_id}", RPC_MUSIC, music_id))
    return out


def make_record(*, region: str, ch: dict, kind: str, account: str, captured_at: int,
                result: dict, keep_self: bool) -> dict:
    return {
        "captured_at": captured_at,
        # read after the reveal (else the chapter's end): the chapter's final standing
        "final": captured_at >= final_after(ch) > 0,
        "region": region,
        "event_id": ch["event_id"],
        "chapter_id": ch["chapter_id"],
        "kind": kind,
        "account": account,
        "borders": result["borders"],
        "self": ({"rank": result["self_rank"], "score": result["self_score"],
                  "out_of_range": result["self_out_of_range"]} if keep_self else None),
    }


def append_record(store: Path, rec: dict) -> Path:
    d = store / rec["region"]
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{rec['chapter_id']}.jsonl"
    with path.open("ab") as fh:  # bytes: no newline translation on Windows
        fh.write((json.dumps(rec, separators=(",", ":")) + "\n").encode("utf-8"))
    return path


def write_meta(store: Path, region: str, ch: dict, fetched_at: int) -> Path:
    d = store / region
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{ch['chapter_id']}.meta.json"
    atomic_io.write_bytes(path, json.dumps(dict(ch, region=region, fetched_at=fetched_at),
                                indent=1, ensure_ascii=False).encode("utf-8"))
    return path


# ------------------------------------------------------------------ network
def needs_daily_login(exc: BaseException, account: str) -> bool:
    """A DATE_CHANGED refusal on a GUEST account: the one case the fetcher answers with the
    client's daily `Home/Login`. The user's own account ('main') never gets it -- the user's own
    game does that login when they open it."""
    return account != "main" and "DATE_CHANGED" in str(exc)


def _grpc(asnap, host: str, path: str, body: bytes, token: str, ident, mv: str) -> bytes:
    rh, tr, data = asnap.call(host, path, asnap.frame(asnap.seal(body) if body else b""),
                              token=token, ident=ident, master_version=mv)
    st = tr.get("grpc-status", rh.get("grpc-status"))
    if st not in (None, "0"):
        raise RuntimeError(f"{path} rejected: grpc-status {st} {tr.get('grpc-message', '')}")
    for m in asnap.unframe(data):
        if m:
            return asnap.unseal(m)
    return b""


def out_of_term(exc: BaseException) -> bool:
    """The server will not rank this chapter right now (ended and not yet revealed, or not
    yet open): a skip for that chapter, not a failure of the region."""
    return "EVENT_OUT_OF_TERM" in str(exc)


def owner_fallback(exc: BaseException, account: str) -> bool:
    """The user's own account refused DATE_CHANGED: fall back to that region's guest."""
    return account == "main" and "DATE_CHANGED" in str(exc)


def fetch_region(region: str, *, account: str, songs: bool, keep_self: bool,
                 store: Path = STORE) -> list[dict]:
    import account_snapshot as asnap  # noqa: PLC0415  (network deps only when fetching)

    host, credential = asnap.load_credential(account)
    if not host.startswith(region + "."):
        raise RuntimeError(f"account {account!r} lives on {host}, not region {region!r}")
    ident = asnap.client_headers(account)
    mv = asnap.fetch_master_version(host, ident)
    time.sleep(PACE_S)
    token = asnap.login(host, credential, ident, mv)
    time.sleep(PACE_S)
    try:
        blob = _grpc(asnap, host, "/rpc.api.Event/ListEventInfo", b"", token, ident, mv)
    except RuntimeError as exc:
        if not needs_daily_login(exc, account):
            raise
        # The guest's last home login is from an earlier game day:
        # do what the client does on DATE_CHANGED -- its daily Home/Login -- once, then retry.
        print(f"[{region}] DATE_CHANGED: guest daily Home/Login, then retry")
        time.sleep(PACE_S)
        asnap.guest_home_login(account, host, token, ident, mv)
        time.sleep(PACE_S)
        blob = _grpc(asnap, host, "/rpc.api.Event/ListEventInfo", b"", token, ident, mv)
    now = int(time.time() * 1000)
    listed = parse_event_info(blob)
    if keep_self:
        # the user's own Event Pt of every event still listed (until its exchange closes), for
        # the event history: once the event leaves the list nothing serves it again
        for eid in sorted({ch["event_id"] for ch in listed if ch.get("event_id")}):
            time.sleep(PACE_S)
            try:
                pts = parse_top_points(_grpc(asnap, host, RPC_TOP, request_body(eid), token, ident, mv))
            except RuntimeError as exc:
                print(f"[{region}] {eid} Top: {exc}")
                continue
            if pts:
                append_points(store, region, eid, int(time.time() * 1000), pts)
                print(f"[{region}] {eid} your Event Pt: "
                      + ", ".join(f"{c.rsplit('-', 1)[-1]}={v:,}" for c, v in sorted(pts.items())))
    chapters = live_chapters(listed, now, store=store, region=region, need_self=keep_self,
                             songs=songs)
    if not chapters:
        print(f"[{region}] no marathon chapter is open or awaiting its reveal")
    records = []
    for ch in chapters:
        write_meta(store, region, ch, now)
        done = final_kinds(store, region, ch["chapter_id"], with_self=keep_self) if now > final_after(ch) else set()
        for kind, rpc, music_id in kinds_for(ch, songs):
            if kind in done:
                continue
            time.sleep(PACE_S)
            try:
                res = parse_ranking_result(_grpc(asnap, host, rpc, request_body(
                    ch["chapter_id"], music_id), token, ident, mv))
            except RuntimeError as exc:
                if not out_of_term(exc):
                    raise
                # A relay chapter past its chapter_end_time is
                # refused until its reveal. Skip it; the region's other chapters still read.
                print(f"[{region}] {ch['chapter_id']} {kind}: not readable now (EVENT_OUT_OF_TERM)")
                continue
            rec = make_record(region=region, ch=ch, kind=kind, account="main" if account == "main"
                              else "guest", captured_at=int(time.time() * 1000), result=res,
                              keep_self=keep_self)
            path = append_record(store, rec)
            line = "  ".join(f"T{b['rank']}={b['score']:,}" for b in rec["borders"])
            print(f"[{region}] {ch['chapter_id']} {kind}: {line or '(no borders)'}"
                  + (f"  | you: #{res['self_rank']} {res['self_score']:,}" if keep_self
                     and res["self_rank"] else ""))
            records.append(rec)
            print(f"[{region}]   appended -> {path.relative_to(HERE) if path.is_relative_to(HERE) else path}")
    return records


def asnap_state() -> Path:
    """state/ (the guest accounts live there), from account_snapshot -- imported only here."""
    import account_snapshot as asnap  # noqa: PLC0415
    return asnap.STATE


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--region", choices=REGIONS + ("all", "own"), default="all",
                    help="'own' = only the owner's own region, read with his account (--self): "
                         "the desktop app's refresh, which has no guest accounts")
    ap.add_argument("--self", dest="self_", action="store_true",
                    help="read the owner's own region with the owner's account (own rank)")
    ap.add_argument("--songs", action="store_true", help="also read per-song borders")
    args = ap.parse_args()

    if args.region == "own" and not args.self_:
        ap.error("--region own needs --self")
    own_region = None
    if args.self_:
        import account_snapshot as asnap  # noqa: PLC0415
        own_region = asnap.load_credential("main")[0].split(".", 1)[0]
    regions = REGIONS if args.region == "all" else (own_region,) if args.region == "own"         else (args.region,)
    failures = 0
    total = 0
    for i, region in enumerate(regions):
        if i:
            time.sleep(PACE_S)
        account = "main" if region == own_region else region
        if account != "main" and not (asnap_state() / "ranking_accounts.json").is_file():
            # A fork or the desktop app has no guest accounts: the other regions are skipped,
            # not failed (docs/self-hosting.md explains the optional guest file).
            print(f"[{region}] skipped: no guest account for this region (state/ranking_accounts.json)")
            continue
        try:
            try:
                total += len(fetch_region(region, account=account, songs=args.songs,
                                          keep_self=(account == "main")))
            except RuntimeError as exc:
                if not owner_fallback(exc, account):
                    raise
                # The user has not opened the game this game day; their account is never sent
                # Home/Login. The borders are the same for everyone: read them with the guest.
                # The app ships no guest accounts -- there the 24/7 tracker's borders reach it
                # anyway, so the read ends here instead of failing the step.
                if not (asnap_state() / "ranking_accounts.json").is_file():
                    print(f"[{region}] your account was refused DATE_CHANGED (the game was not opened"
                          f" today): own rank skipped; open the game once today")
                    continue
                print(f"[{region}] your account was refused DATE_CHANGED (the game was not opened"
                      f" today): borders via the {region} guest, own rank skipped")
                time.sleep(PACE_S)
                total += len(fetch_region(region, account=region, songs=args.songs,
                                          keep_self=False))
        except Exception as exc:  # noqa: BLE001  one region failing must not hide the others
            failures += 1
            print(f"[{region}] FAILED: {type(exc).__name__}: {exc}")
    print(f"cutoffs: {total} series point(s) appended, {failures} region(s) failed")
    return 1 if failures and not total else 0


if __name__ == "__main__":
    sys.exit(main())
