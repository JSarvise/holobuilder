"""24/7 event ranking tracker: the border reads of `event_cutoffs.py` plus the TOP-100 board,
on a schedule, from an always-on host -- with a tiny read-only HTTP API the local app pulls from.

Why: the local poller (`tools/web/cutoffs_poller.py`) reads only while the PC is on, so every
chapter's series has holes and the forecasts learn from fragments. This reads continuously on a
small always-on host and feeds the local app.

What it reads, per region, with that region's GUEST account (`state/ranking_accounts.json`; the
user's own account is never used here):

* every `borders` seconds: the `*RankingGrade` border of each open chapter -- the SAME record
  `event_cutoffs.py` appends (`<region>/<chapter_id>.jsonl`, `.meta.json` beside it), so every
  reader of that store works unchanged;
* the chapter's top-100 board ON DEMAND, only while someone looks at it: every `top_watch` seconds while a viewer holds a lease (`POST /watch`, the
  local page renews it while it is open), else every `top` seconds (0 = never, the default).
  Appended to
  `<region>/<chapter_id>.top.jsonl` as `{captured_at, ..., rows: [[rank, score, public_user_id,
  name], ...]}`. Other players' public ranking rows; kept out of git like the rest of dumps/.

What it does NOT do per tick, because through a paid proxy every byte counts (roughly:
Master/Get ~109 KB, ListEventInfo 27 KB, Grade 6 KB, Top 44.5 KB, nothing gzip-compressed): it keeps ONE HTTP/2 connection per region (`account_snapshot.Channel`), logs in
once and again only when refused or after TOKEN_MAX_AGE, fetches the master version only when
refused for it, and re-reads the event list every EVENTS_EVERY seconds.

Self-healing, modelled on the community tool holodori.best's worker: DATE_CHANGED -> the
guest's daily Home/Login; a refused token -> login again; OUTDATED_MASTER_DATA -> new master
version; OUTDATED_APP -> the next app version is probed (an outdated-but-valid version is refused
with 2004, an unknown one with INVALID_ARGUMENT, so trying X.Y.Z+1, X.Y+1.0, X+1.0.0 finds the
live one); anything else backs the region off (x2, capped at 15 min). `status.json` in the store
says, per region, the last success, the last error and the bytes moved.

The API (`--serve-port`, bound to 127.0.0.1 only -- reach it through `ssh -L`, nothing is opened
to the internet): `GET /status`, `GET /files` (every store file with its size), `GET
/file?path=<rel>&offset=<n>` (the bytes from `offset`: the jsonl files are append-only, so a
puller asks only for what it has not seen). `tracker_pull.py` is the local side.

    HB_API_PROXY=<proxy-host>:<port> HOLODREAMS_APP_VERSION=1.2.1 \
        python event_tracker.py --serve-port 8790
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import atomic_io  # noqa: E402
import event_cutoffs as ec  # noqa: E402

STORE = ec.STORE
REGIONS = ec.REGIONS
RPC_TOP_POINTS = "/rpc.api.Marathon/ListMarathonScoreRankingTop"
RPC_TOP_TOTAL = "/rpc.api.Marathon/ListTotalMusicHighestScoreRankingTop"
# a chapter's song ranking, top 100; merged.proto: MarathonListMusicHighestScoreRankingTopRequest {marathon_chapter_id=1,
# music_id=2}, its result the same {self_rank, self_score, out_of_range, rank_infos=4} as the others
RPC_TOP_MUSIC = "/rpc.api.Marathon/ListMusicHighestScoreRankingTop"
RPC_EVENTS = "/rpc.api.Event/ListEventInfo"

# seconds between reads: the borders of every region every 5 min, the top 100 only under a
# viewer's lease, every 5 min. About 0.1 GB/month of proxy traffic while a chapter is open
# (an estimate: 3 x 6 KB / 5 min + a login a day), plus ~0.5 MB per watched hour of a top 100.
DEFAULT_PLAN = {r: {"borders": 300, "top": 0, "top_watch": 300} for r in ("us", "as", "jp")}
WATCH_MAX_S = 1800           # one lease lasts at most this long; the page renews it
# re-read the event list (chapters, tiers) this often, and at the next start / reveal it already
# lists. The list is ~27 KB a read (3.9 MB a day through the paid proxy at 30 min, a fifth of
# the tracker's traffic) while what it says changes only at its own dates.
EVENTS_EVERY = 3 * 3600
# keep each region's proxied connection alive between reads (account_snapshot.Channel.ping):
# a PING every KEEPALIVE_S, but only while the next read is at most KEEPALIVE_AHEAD away.
KEEPALIVE_S = 20
KEEPALIVE_AHEAD = 600
TOKEN_MAX_AGE = 12 * 3600    # the game's token lives 24 h
IDLE_EVENTS_EVERY = 1800     # nothing open: look again this often
MAX_BACKOFF = 900
# the game in maintenance (grpc-status 3 IN_MAINTENANCE, code 2006, on all three regions): not a fault to back off from but a wait with an end -- look every 5 min so the
# reads resume within minutes of it, instead of up to MAX_BACKOFF later
MAINTENANCE_RETRY = 300
PACE_S = 1.0                 # project etiquette: <= 1 request per second per region


class GameError(RuntimeError):
    def __init__(self, path: str, status: str, message: str, code: str | None):
        super().__init__(f"{path} rejected: grpc-status {status} {message} (code {code})")
        self.status, self.message, self.code = status, message, code


def parse_top(blob: bytes) -> list[list]:
    """`*RankingTopResponse` -> [[rank, score, public_user_id, name], ...] (common/ranking:
    rank_infos=4 {rank=1, score=2, user_info=3 {public_user_id=1, user_profile_info=2 {name=1}}})."""
    top = ec.msg(blob)
    res = ec.msg(top[1][0]) if top.get(1) else {}
    rows = []
    for raw in res.get(4, []):
        r = ec.msg(raw)
        uid = name = None
        if r.get(3):
            u = ec.msg(r[3][0])
            uid = ec._s(u, 1)
            if u.get(2):
                name = ec._s(ec.msg(u[2][0]), 1)
        rows.append([ec._sint(ec._i(r, 1)), ec._sint(ec._i(r, 2)), uid, name])
    rows.sort(key=lambda x: x[0] or 0)
    return rows


def next_versions(v: str) -> list[str]:
    try:
        a, b, c = (int(x) for x in v.split("."))
    except ValueError:
        return []
    return [f"{a}.{b}.{c + 1}", f"{a}.{b + 1}.0", f"{a + 1}.0.0"]


def top_final_kinds(store: Path, region: str, chapter_id: str) -> set[str]:
    """The kinds ('points' / 'total' / 'music:<id>') whose top 100 has a final read stored."""
    path = store / region / f"{chapter_id}.top.jsonl"
    out: set[str] = set()
    if not path.is_file():
        return out
    for ln in path.read_bytes().splitlines():
        if b'"final":true' not in ln and b'"final": true' not in ln:
            continue
        try:
            out.add(json.loads(ln).get("kind") or "points")
        except ValueError:
            continue
    return out


def top_chapters(listed: list[dict], live: list[dict], store: Path, region: str,
                 now_ms: int) -> list[dict]:
    """The chapters whose top 100 is worth reading: the live ones, plus an ended one the server
    still lists whose top 100 lacks a final for some kind -- a song ranking's board, added
    after its chapter ended, is still served until the event leaves its term."""
    seen = {c["chapter_id"] for c in live}
    out = list(live)
    for ch in listed:
        if ch["chapter_id"] in seen or now_ms <= ec.final_after(ch):
            continue
        done = top_final_kinds(store, region, ch["chapter_id"])
        if any(k not in done for k, _r, _m in ec.kinds_for(ch, songs=True)):
            out.append(ch)
    return out


def append_top(store: Path, rec: dict) -> Path:
    d = store / rec["region"]
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{rec['chapter_id']}.top.jsonl"
    with path.open("ab") as fh:
        fh.write((json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))
    return path


def next_boundary(listed: list[dict], now: float) -> float:
    """The next instant (epoch s) at which the live chapter set changes by date alone: a
    listed chapter's start or its final read (ec.final_after). inf when none is ahead."""
    times = [t / 1000 for ch in listed for t in (ch.get("start_time") or 0, ec.final_after(ch))
             if t and t / 1000 > now]
    return min(times, default=float("inf")) + 5


class Region:
    def __init__(self, region: str, plan: dict, store: Path, asnap):
        self.region, self.plan, self.store, self.A = region, plan, store, asnap
        self.host, self.credential = asnap.load_credential(region)
        if not self.host.startswith(region + ".") or region == "main":
            raise RuntimeError(f"guest account {region!r} lives on {self.host}")
        self.ident = asnap.client_headers(region)
        self.chan = asnap.Channel(self.host, guest_account=region)
        self.mv = self.token = None
        self.token_at = 0.0
        self.chapters: list[dict] = []
        self.listed: list[dict] = []
        self.events_at = 0.0
        self.next_boundary = float("inf")
        self.next_border = self.next_top = 0.0
        self.watch_until = 0.0       # the main board's lease (Event Pt / Total)
        # a song board's lease, per kind music:<id>: read only while someone has that song's
        # tab open (the same savings as the main board), never because the Event Pt board is
        # watched
        self.song_watch: dict[str, float] = {}
        self.wake = 0.0
        self.backoff = 0
        self.stat = {"region": region, "host": self.host, "ok_at": None, "error": None,
                     "error_at": None, "reads": 0, "top_reads": 0, "logins": 0,
                     "bytes_in": 0, "pings": 0, "ping_fails": 0, "chapters": []}

    # -- transport -------------------------------------------------------------------------
    def rpc(self, path: str, body: bytes = b"", *, auth: bool = True, mv: bool = True) -> bytes:
        """One call; `auth` sends the token, `mv` the master version (Master/Get sends neither,
        Auth/Login only the version -- as the client does)."""
        A = self.A
        rh, tr, data = self.chan.call(path, A.frame(A.seal(body) if body else b""),
                                      token=self.token if auth else None, ident=self.ident,
                                      master_version=self.mv if mv else None)
        time.sleep(PACE_S)
        self.stat["bytes_in"] += len(data)
        st = tr.get("grpc-status", rh.get("grpc-status"))
        if st not in (None, "0"):
            raise GameError(path, st, tr.get("grpc-message", rh.get("grpc-message", ""))[:160],
                            tr.get("x-app-error-code", rh.get("x-app-error-code")))
        for m in A.unframe(data):
            if m:
                return A.unseal(m)
        if auth and path != "/rpc.api.Home/Login":
            # status 0 with an empty body: the token was not accepted
            raise GameError(path, "0", "empty body (token refused)", "AUTH")
        return b""

    def login(self) -> None:
        if not self.mv:
            self.mv = self._master()
        blob = self.rpc("/rpc.api.Auth/Login",
                        self.A.pb_bytes_field(1, self.credential), auth=False)
        token = None
        for fn, wt, v in self.A.fields(blob):
            if fn == 1 and wt == 2:
                token = v.decode()
                break
        if not token:
            raise RuntimeError("no game_auth_token in login response")
        self.token, self.token_at = token, time.time()
        self.stat["logins"] += 1

    def _master(self) -> str:
        blob = self.rpc("/rpc.api.Master/Get", auth=False, mv=False)
        for fn, wt, v in self.A.fields(blob):
            if fn == 1 and wt == 2:
                return v.decode()
        raise RuntimeError("no version in Master/Get")

    def call(self, path: str, body: bytes = b"") -> bytes:
        """An authenticated read that heals what can be healed, once each."""
        healed: set[str] = set()
        while True:
            if not self.token or time.time() - self.token_at > TOKEN_MAX_AGE:
                self.login()
            try:
                return self.rpc(path, body)
            except GameError as exc:
                text = f"{exc.message} {exc.code}"
                if "DATE_CHANGED" in text and "date" not in healed:
                    healed.add("date")
                    self.rpc("/rpc.api.Home/Login")
                elif (exc.status == "16" or exc.code == "AUTH") and "auth" not in healed:
                    healed.add("auth")
                    self.token = None
                elif ("2001" in text or "OUTDATED_MASTER" in text) and "master" not in healed:
                    healed.add("master")
                    self.mv = self.token = None
                elif ("2004" in text or "OUTDATED_APP" in text) and "app" not in healed:
                    healed.add("app")
                    self.bump_app_version()
                    self.mv = self.token = None
                else:
                    raise

    def bump_app_version(self) -> None:
        cur = os.environ.get("HOLODREAMS_APP_VERSION") or "1.2.1"
        for cand in next_versions(cur):
            os.environ["HOLODREAMS_APP_VERSION"] = cand
            try:
                self.rpc("/rpc.api.Master/Get", auth=False, mv=False)
            except GameError as exc:
                if "IN_MAINTENANCE" in exc.message:
                    os.environ["HOLODREAMS_APP_VERSION"] = cur
                    raise  # no version is accepted in maintenance: climb after it
                if "2004" in f"{exc.message} {exc.code}" or "OUTDATED_APP" in exc.message:
                    cur = cand  # valid but outdated too: keep climbing
                    continue
                continue  # unknown version: try the next shape
            print(f"[{self.region}] app version -> {cand}", flush=True)
            _save_appver(self.store, cand)
            return
        os.environ["HOLODREAMS_APP_VERSION"] = cur

    # -- schedule --------------------------------------------------------------------------
    def refresh_events(self, now: float) -> None:
        blob = self.call(RPC_EVENTS)
        listed = ec.parse_event_info(blob)
        self.listed = listed
        self.chapters = ec.live_chapters(listed, int(now * 1000), store=self.store,
                                         region=self.region, songs=True)
        self.events_at = now
        self.next_boundary = next_boundary(listed, now)
        for ch in self.chapters:
            ec.write_meta(self.store, self.region, ch, int(now * 1000))
        self.stat["chapters"] = [c["chapter_id"] for c in self.chapters]

    def tick(self, now: float) -> None:
        if now < self.wake:
            return
        try:
            if now - self.events_at > EVENTS_EVERY or not self.events_at or now >= self.next_boundary:
                self.refresh_events(now)
            else:
                # no read: which listed chapters are live (or still owe their final) changes
                # with the clock and the store, both local
                self.chapters = ec.live_chapters(self.listed, int(now * 1000), store=self.store,
                                                 region=self.region, songs=True)
            self.top_chs = top_chapters(self.listed, self.chapters, self.store, self.region,
                                        int(now * 1000))
            if not self.chapters and not (self.top_chs and self.watched(now)):
                self.wake = now + IDLE_EVENTS_EVERY
                self.events_at = 0.0
                self._ok(now)
                return
            if now >= self.next_border:
                self.read_borders()
                self.next_border = now + self.plan["borders"]
            every = self.top_every(now)
            if every and now >= self.next_top:
                self.read_top()
                self.next_top = now + every
            self._ok(now)
            self.wake = min(self.next_border, self.next_top if every else 1e18,
                            self.watched_until() if self.watched(now) else 1e18)
        except Exception as exc:  # noqa: BLE001 -- a region failing must never stop the others
            if "IN_MAINTENANCE" in str(exc):
                self.wake = now + MAINTENANCE_RETRY
                self.chan.close()
                if not self.stat.get("maintenance"):
                    print(f"[{self.region}] game in maintenance -- looking again every "
                          f"{MAINTENANCE_RETRY}s", flush=True)
                self.stat["maintenance"] = True
                self.stat["error"] = "IN_MAINTENANCE"
                self.stat["error_at"] = int(now)
                return
            self.backoff = min(MAX_BACKOFF, max(60, self.backoff * 2))
            self.wake = now + self.backoff
            self.chan.close()
            self.stat["error"] = f"{type(exc).__name__}: {exc}"[:300]
            self.stat["error_at"] = int(now)
            print(f"[{self.region}] {self.stat['error']} -- retry in {self.backoff}s", flush=True)
            if not isinstance(exc, (GameError, OSError, ConnectionError)):
                traceback.print_exc()

    def keepalive(self, now: float) -> None:
        """PING the open connection when it has been quiet KEEPALIVE_S and a read is due soon;
        an idle region (no chapter open, next look in 30 min) lets it close."""
        ch = self.chan
        if ch.sock is None or now - ch.used_at < KEEPALIVE_S or self.wake - now > KEEPALIVE_AHEAD:
            return
        if ch.ping():
            self.stat["pings"] += 1
        else:
            self.stat["ping_fails"] += 1

    def watched_until(self) -> float:
        return max([self.watch_until, *getattr(self, "song_watch", {}).values()])

    def watched(self, now: float) -> bool:
        return now < self.watched_until()

    def kind_watched(self, kind: str, now: float) -> bool:
        """Is this board leased? A song board only by its own lease; the main board by the
        region's lease, or (no lease system: `top` > 0) always."""
        if kind.startswith("music:"):
            return now < getattr(self, "song_watch", {}).get(kind, 0.0)
        return now < self.watch_until or bool(self.plan.get("top"))

    def top_every(self, now: float) -> int:
        """Seconds between top-100 reads now: dense while watched, else the background rate."""
        if self.watched(now):
            return int(self.plan.get("top_watch") or 120)
        return int(self.plan.get("top") or 0)

    def watch(self, now: float, seconds: float, kind: str | None = None) -> None:
        """A viewer's lease: read the top 100 densely until it runs out (renewable). `kind`
        music:<id> leases that song's board alone; otherwise the main board."""
        seconds = max(0.0, min(float(seconds), WATCH_MAX_S))
        if kind and kind.startswith("music:"):
            leases = getattr(self, "song_watch", {})
            first = now >= leases.get(kind, 0.0)
            self.song_watch = {k: t for k, t in leases.items() if t > now}
            self.song_watch[kind] = max(self.song_watch.get(kind, 0.0), now + seconds)
            self.stat["songs_watched"] = sorted(self.song_watch)
        else:
            first = now >= self.watch_until
            self.watch_until = max(self.watch_until, now + seconds)
        self.stat["watched_until"] = int(self.watched_until())
        dense = now + int(self.plan.get("top_watch") or 120)
        if first or self.next_top > dense:
            self.next_top = now          # a new viewer gets a fresh board now, not in 30 min
        self.wake = min(self.wake, now)

    def _ok(self, now: float) -> None:
        self.backoff = 0
        self.stat["maintenance"] = False
        self.stat["ok_at"] = int(now)

    def read_borders(self) -> None:
        refresh = False
        for ch in self.chapters:
            # the song ranking of each chapter too: one more read per chapter;
            # after the reveal only the kinds whose final is not stored yet
            done = (ec.final_kinds(self.store, self.region, ch["chapter_id"])
                    if time.time() * 1000 > ec.final_after(ch) else set())
            for kind, rpc, music_id in ec.kinds_for(ch, songs=True):
                if kind in done:
                    continue
                try:
                    res = ec.parse_ranking_result(self.call(rpc, ec.request_body(ch["chapter_id"], music_id)))
                except GameError as exc:
                    if "EVENT_OUT_OF_TERM" in exc.message:
                        refresh = True
                        continue
                    raise
                at = int(time.time() * 1000)
                rec = ec.make_record(region=self.region, ch=ch, kind=kind, account="guest",
                                     captured_at=at, result=res, keep_self=False)
                ec.append_record(self.store, rec)
                self.stat["reads"] += 1
                if rec["final"]:
                    refresh = True
        if refresh:
            self.events_at = 0.0  # a chapter ended or got its final: re-plan on the next tick

    def read_top(self) -> None:
        for ch in getattr(self, "top_chs", None) or self.chapters:
            # after the reveal, only the kinds whose final board is not stored yet
            done = (top_final_kinds(self.store, self.region, ch["chapter_id"])
                    if int(time.time() * 1000) >= ec.final_after(ch) else set())
            for kind, rpc, music_id in ec.kinds_for(ch, songs=True):
                if kind in done or not self.kind_watched(kind, time.time()):
                    continue
                top_rpc = (RPC_TOP_POINTS if kind == "points" else RPC_TOP_TOTAL if kind == "total"
                           else RPC_TOP_MUSIC)
                try:
                    rows = parse_top(self.call(top_rpc, ec.request_body(ch["chapter_id"], music_id)))
                except GameError as exc:
                    if "EVENT_OUT_OF_TERM" in exc.message:
                        continue
                    raise
                at = int(time.time() * 1000)
                append_top(self.store, {
                    "captured_at": at, "final": at >= ec.final_after(ch) > 0,
                    "region": self.region, "event_id": ch["event_id"],
                    "chapter_id": ch["chapter_id"], "kind": kind, "rows": rows})
                self.stat["top_reads"] += 1


def _save_appver(store: Path, v: str) -> None:
    store.mkdir(parents=True, exist_ok=True)
    atomic_io.write_bytes(store / "appver.txt", v.encode())


def _load_appver(store: Path) -> None:
    p = store / "appver.txt"
    if p.is_file() and not os.environ.get("HOLODREAMS_APP_VERSION_PINNED"):
        v = p.read_text().strip()
        if v:
            os.environ["HOLODREAMS_APP_VERSION"] = v


# ---------------------------------------------------------------------------------- API
def make_handler(store: Path, status: callable, watch: callable = None):
    root = store.resolve()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/status":
                return self._send(200, json.dumps(status()).encode())
            if u.path == "/files":
                files = []
                for p in sorted(root.rglob("*")):
                    if p.is_file() and p.suffix in (".jsonl", ".json"):
                        st = p.stat()
                        files.append({"path": p.relative_to(root).as_posix(), "size": st.st_size,
                                      "mtime": int(st.st_mtime)})
                return self._send(200, json.dumps({"files": files}).encode())
            if u.path == "/file":
                rel = (q.get("path") or [""])[0]
                p = (root / rel).resolve()
                if root not in p.parents or not p.is_file():
                    return self._send(404, b'{"error":"no such file"}')
                off = int((q.get("offset") or ["0"])[0])
                with p.open("rb") as fh:
                    fh.seek(off)
                    data = fh.read(8 << 20)
                return self._send(200, data, "application/octet-stream")
            return self._send(404, b'{"error":"not found"}')

        def do_POST(self):  # noqa: N802
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/watch" and watch is not None:
                region = (q.get("region") or [""])[0]
                try:
                    secs = float((q.get("seconds") or ["600"])[0])
                except ValueError:
                    return self._send(400, b'{"error":"seconds"}')
                kind = (q.get("kind") or [None])[0] or None
                if kind is not None and not (kind in ("points", "total") or (
                        kind.startswith("music:") and kind[6:].replace("_", "").isalnum())):
                    return self._send(400, b'{"error":"kind"}')
                if not watch(region, secs, kind):
                    return self._send(404, b'{"error":"no such region"}')
                return self._send(200, json.dumps(status()).encode())
            return self._send(404, b'{"error":"not found"}')

    return H


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--regions", default=",".join(REGIONS))
    ap.add_argument("--plan", help="JSON {region: {borders: s, top: s}} overriding DEFAULT_PLAN")
    ap.add_argument("--store", type=Path, default=STORE)
    ap.add_argument("--serve-port", type=int, default=0)
    ap.add_argument("--once", action="store_true", help="one tick per region, then exit")
    args = ap.parse_args()

    import account_snapshot as A  # noqa: PLC0415
    plan = dict(DEFAULT_PLAN)
    if args.plan:
        plan.update(json.loads(args.plan))
    _load_appver(args.store)
    regions = []
    for r in args.regions.split(","):
        r = r.strip()
        if r:
            regions.append(Region(r, plan.get(r, {"borders": 300, "top": 0, "top_watch": 120}),
                                  args.store, A))
    started = int(time.time())

    def status() -> dict:
        return {"started": started, "now": int(time.time()), "proxy": bool(A.API_PROXY),
                "app_version": os.environ.get("HOLODREAMS_APP_VERSION"), "plan": plan,
                "regions": [g.stat for g in regions]}

    woke = threading.Event()

    def watch(region: str, seconds: float, kind: str | None = None) -> bool:
        g = next((g for g in regions if g.region == region), None)
        if g is None:
            return False
        g.watch(time.time(), seconds, kind)
        woke.set()
        return True

    if args.serve_port:
        srv = ThreadingHTTPServer(("127.0.0.1", args.serve_port),
                                  make_handler(args.store, status, watch))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"[api] 127.0.0.1:{args.serve_port}", flush=True)

    while True:
        for g in regions:
            g.tick(time.time())
        for g in regions:
            g.keepalive(time.time())
        try:
            atomic_io.write_bytes(args.store / "status.json",
                                  json.dumps(status(), indent=1).encode())
        except OSError:
            pass
        if args.once:
            return 0
        nxt = min(g.wake for g in regions)
        woke.wait(max(1.0, min(KEEPALIVE_S / 2, nxt - time.time())))
        woke.clear()


if __name__ == "__main__":
    sys.exit(main())
