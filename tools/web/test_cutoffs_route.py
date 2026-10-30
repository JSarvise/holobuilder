"""Event cutoffs: the prediction model (`cutoffs_view.predict`) and the two routes,
`GET /api/event/cutoffs` and `POST /api/event/cutoffs/sync`.

No network and NO files written (`test_no_writes.py` walks this file too): the store is an
in-memory dict swapped in for `cutoffs_view.list_chapters` / `read_chapter`, and the sync's
subprocess is replaced by a stub of `Handler._run_recon_script`. The disk round trip (append
-> read back) is tested on the recon side, `tools/recon/proxy/test_event_cutoffs.py`.
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

import cutoffs_view as cv
import server as web_server

H = 3_600_000
START = 1_000 * H
END = START + 50 * H


# ------------------------------------------------------------------ predict (pure)
def test_no_reads_no_prediction():
    assert cv.predict([], start=START, end=END, now=START + H) is None


def test_single_read_is_average_pace_with_no_band():
    p = cv.predict([(START + 10 * H, 100_000)], start=START, end=END, now=START + 10 * H)
    assert p["method"] == "pace"
    assert p["final"] == 500_000            # 10 k/h kept for the remaining 40 h
    assert p["band"] == [] and p["confidence"] == "low"
    assert p["points"][0] == [START + 10 * H, 100_000]
    assert p["points"][-1] == [END, 500_000]


def test_two_paces_give_mean_and_band():
    # average pace 100k/10h = 10k/h; recent pace (h4 -> h10) = (100k-20k)/6h
    series = [(START + 4 * H, 20_000), (START + 10 * H, 100_000)]
    p = cv.predict(series, start=START, end=END, now=START + 10 * H)
    recent = 100_000 + 80_000 / 6 * 40
    avg = 500_000
    assert p["low"] == avg and p["high"] == round(recent)
    assert p["final"] == round((avg + recent) / 2)
    assert p["band"] and p["band"][-1] == [END, avg, round(recent)]
    assert p["confidence"] == "rough"


def test_recent_pace_ignores_reads_older_than_the_window_or_too_close():
    series = [(START + 1 * H, 5_000), (START + 40 * H - 600_000, 390_000), (START + 40 * H, 400_000)]
    p = cv.predict(series, start=START, end=END, now=START + 40 * H)
    # h1 is outside the 12 h window and the other read is 10 min back: only the average pace
    assert p["inputs"]["rate_recent_per_hour"] is None and p["band"] == []


def test_after_the_end_the_last_read_is_final():
    p = cv.predict([(END, 777)], start=START, end=END, now=END + H)
    assert p["method"] == "final" and p["final"] == 777


def test_template_scales_from_past_curves():
    # two past chapters: 25 % and 40 % of the final was in at the half-way point
    t1 = ([0.0, 0.5, 1.0], [0.0, 0.25, 1.0])
    t2 = ([0.0, 0.5, 1.0], [0.0, 0.40, 1.0])
    p = cv.predict([(START + 25 * H, 100_000)], start=START, end=END, now=START + 25 * H,
                   templates=[t1, t2])
    assert p["method"] == "template"
    assert p["low"] == 250_000 and p["high"] == 400_000
    assert p["final"] == 325_000 and p["points"][-1] == [END, 325_000]
    assert p["band"][-1] == [END, 250_000, 400_000]


def test_normalised_curve_needs_a_finished_series():
    s = [(START + 10 * H, 50), (END, 200)]
    assert cv.normalised_curve(s, START, END) == ([0.0, 0.2, 1.0], [0.0, 0.25, 1.0])
    assert cv.normalised_curve([(START + 10 * H, 50)], START, END) is None


def test_a_curve_first_read_late_is_no_template():
    """A chapter first read at 73 % of its length can fit a = 2.28 where the full record gives
    1.04, forecasting Top 500 at 40.3 M against 3.9 M -- so a late-starting curve is no template."""
    assert cv.normalised_curve([(START + 37 * H, 80), (END, 100)], START, END) is None


def test_load_grades_joins_master_and_lang():
    g = cv.load_grades(
        [{"id": "g-x", "data": {"id": "g-x", "nameLangId": "la-g-x", "color1": "A", "color2": "B"}}],
        [{"id": "la-g-x", "data": {"id": "la-g-x", "text": "Dream"}}])
    assert g == {"g-x": {"name": "Dream", "color1": "A", "color2": "B"}}


# ------------------------------------------------------------------ in-memory store
META = {"chapter_id": "ch-1", "event_id": "ev", "event_name": "Point Rally", "start_time": START,
        "end_time": END, "chapter_end_time": END, "reveal_time": END + 9 * H,
        "tiers": {"points": [{"end_rank": 500, "grade_id": "g-dream"},
                             {"end_rank": 50000, "grade_id": "g-bronze"}],
                  "total": [], "music": {}}}
RECS = [
    {"captured_at": START + 4 * H, "region": "us", "chapter_id": "ch-1", "kind": "points",
     "borders": [{"rank": 500, "score": 20_000}], "self": None},
    {"captured_at": START + 10 * H, "region": "us", "chapter_id": "ch-1", "kind": "points",
     "borders": [{"rank": 500, "score": 100_000}], "self": {"rank": 1385, "score": 4_000,
                                                            "out_of_range": False}},
]


@pytest.fixture
def store(monkeypatch):
    data = {"us": ([META], {"ch-1": RECS})}

    def list_chapters(_store, region):
        return list(data.get(region, ([], {}))[0])

    def read_chapter(_store, region, cid):
        metas, recs = data.get(region, ([], {}))
        return next((m for m in metas if m["chapter_id"] == cid), None), list(recs.get(cid, []))

    monkeypatch.setattr(cv, "list_chapters", list_chapters)
    monkeypatch.setattr(cv, "read_chapter", read_chapter)


def test_build_view_shape(store):
    v = cv.build_view(web_server.CUTOFFS_STORE_DIR, "us", now=START + 11 * H,
                      grades={"g-dream": {"name": "Dream", "color1": "1", "color2": "2"}})
    assert v["available"] and v["chapter_id"] == "ch-1" and v["kind"] == "points"
    assert v["regions"] == {"us": True, "as": False, "jp": False}
    assert v["snapshots"] == 2 and v["self"]["rank"] == 1385
    dream, bronze = v["tiers"]
    assert dream["grade_name"] == "Dream" and dream["score"] == 100_000
    assert dream["previous"]["score"] == 20_000 and dream["rate_per_hour"] == 13_333
    assert dream["prediction"]["method"] == "pace"
    # a tier the server returned no border for (fewer players than the rank) stays empty
    assert bronze["score"] is None and bronze["filled"] is False and bronze["prediction"] is None


def test_build_view_without_data_raises_lookup(store):
    with pytest.raises(LookupError):
        cv.build_view(web_server.CUTOFFS_STORE_DIR, "jp", now=START)


# ------------------------------------------------------------------ routes
def _free_port():
    with socket.socket() as s:
        s.bind((web_server.BIND_HOST, 0))
        return s.getsockname()[1]


@pytest.fixture
def web_base():
    port = _free_port()
    srv = web_server.ThreadingServer((web_server.BIND_HOST, port), web_server.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://{web_server.BIND_HOST}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def _req(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_cutoffs_route_200(web_base, store):
    st, body = _req(web_base + "/api/event/cutoffs?region=us")
    assert st == 200 and body["tiers"][0]["rank"] == 500


def test_cutoffs_route_503_when_nothing_synced(web_base, store):
    st, body = _req(web_base + "/api/event/cutoffs?region=as")
    assert st == 503 and body["available"] is False and "sync" in body["error"]


def test_cutoffs_route_400_on_bad_region(web_base):
    st, body = _req(web_base + "/api/event/cutoffs?region=eu")
    assert st == 400


def test_sync_route_passes_region_and_self(web_base, monkeypatch):
    seen = {}

    def fake(self, script, tag, args=None):
        seen.update(script=script, tag=tag, args=args)
        return 0, ("[jp] FAILED: RuntimeError: grpc-status 3 DATE_CHANGED\n"
                   "cutoffs: 2 series point(s) appended, 1 region(s) failed\n")

    monkeypatch.setattr(web_server.Handler, "_run_recon_script", fake)
    st, body = _req(web_base + "/api/event/cutoffs/sync", {"region": "us"})
    assert st == 200 and body["appended"] == 2 and body["failed_regions"] == ["jp"]
    assert seen["script"] == web_server.CUTOFFS_SYNC_SCRIPT
    assert seen["args"] == ["--region", "us", "--self"]
    st, _ = _req(web_base + "/api/event/cutoffs/sync", {"region": "all", "self": False})
    assert st == 200 and seen["args"] == ["--region", "all"]


def test_sync_route_rejects_bad_region_and_concurrent_runs(web_base, monkeypatch):
    st, _ = _req(web_base + "/api/event/cutoffs/sync", {"region": "eu"})
    assert st == 400
    assert web_server._CUTOFFS_SYNC_LOCK.acquire(blocking=False)
    try:
        st, body = _req(web_base + "/api/event/cutoffs/sync", {})
        assert st == 409 and "already running" in body["error"]
    finally:
        web_server._CUTOFFS_SYNC_LOCK.release()


# ------------------------------------------------------------------ the optional poller
import cutoffs_poller as cp  # noqa: E402


def test_poller_never_uses_the_owner_account():
    assert "--self" not in cp.build_args()
    assert cp.build_args() == ["--region", "all"]


def test_poller_parse_output_and_interval_floor():
    out = ("[jp] FAILED: RuntimeError: x\n"
           "cutoffs: 2 series point(s) appended, 1 region(s) failed\n")
    assert cp.parse_output(out) == (2, ["jp"])
    assert cp.clamp_interval("5") == 5
    for bad in (0, -3, "x", None):
        with pytest.raises(ValueError):
            cp.clamp_interval(bad)


def _poller(results, lock=None):
    seq = iter(results)

    def runner():
        r = next(seq)
        if isinstance(r, Exception):
            raise r
        return r
    return cp.CutoffsPoller(runner, lock or threading.Lock(), interval_min=5,
                            clock=lambda: 100.0)


def test_poller_backs_off_on_total_failure_and_resets_on_success():
    fail = (1, "[us] FAILED: x\n[as] FAILED: x\n[jp] FAILED: x\n"
               "cutoffs: 0 series point(s) appended, 3 region(s) failed\n")
    ok = (0, "cutoffs: 3 series point(s) appended, 0 region(s) failed\n")
    p = _poller([fail, fail, RuntimeError("boom"), fail, fail, ok])
    delays = []
    for _ in range(6):
        p.run_cycle()
        delays.append(p.next_delay_min())
    assert delays == [10, 20, 40, 60, 60, 5]
    assert p.last_error is None and p.failures == 0 and p.cycles == 6


def test_poller_partial_failure_keeps_the_interval_and_idle_waits_an_hour():
    partial = (0, "[jp] FAILED: x\ncutoffs: 2 series point(s) appended, 1 region(s) failed\n")
    idle = (0, "[us] no marathon chapter is open\n"
               "cutoffs: 0 series point(s) appended, 0 region(s) failed\n")
    p = _poller([partial, idle])
    p.run_cycle()
    assert p.next_delay_min() == 5 and "jp" in p.last_error
    p.run_cycle()
    assert p.next_delay_min() == cp.IDLE_INTERVAL_MIN


def test_poller_skips_while_a_manual_sync_holds_the_lock():
    lock = threading.Lock()
    p = _poller([], lock=lock)
    assert lock.acquire(blocking=False)
    try:
        assert p.run_cycle() == {"skipped": True} and p.skipped == 1 and p.cycles == 0
    finally:
        lock.release()


def test_poller_thread_runs_and_stops_cleanly():
    ran = threading.Event()

    def runner():
        ran.set()
        return 0, "cutoffs: 1 series point(s) appended, 0 region(s) failed\n"
    p = cp.CutoffsPoller(runner, threading.Lock(), interval_min=5)
    assert p.status()["enabled"] is False          # off by default
    st = p.configure(enabled=True)
    assert st["enabled"] and st["requests_per_hour"] == 144
    assert ran.wait(5)
    p.stop()
    assert p.status()["running"] is False and p.status()["next_run"] is None


@pytest.fixture
def fake_poller(monkeypatch):
    calls = []

    def runner():
        calls.append(1)
        return 0, "cutoffs: 1 series point(s) appended, 0 region(s) failed\n"
    p = cp.CutoffsPoller(runner, threading.Lock())
    monkeypatch.setattr(web_server, "_CUTOFFS_POLLER", p)
    yield p
    p.stop()


def test_poller_routes(web_base, fake_poller):
    st, body = _req(web_base + "/api/event/cutoffs/poller")
    assert st == 200 and body["enabled"] is False and body["interval_min"] == 5
    st, body = _req(web_base + "/api/event/cutoffs/poller", {"interval_min": 0})
    assert st == 400
    st, body = _req(web_base + "/api/event/cutoffs/poller", {"enabled": "yes"})
    assert st == 400
    st, body = _req(web_base + "/api/event/cutoffs/poller", {"enabled": True, "interval_min": 2})
    assert st == 200 and body["enabled"] is True and body["interval_min"] == 2
    st, body = _req(web_base + "/api/event/cutoffs/poller", {"enabled": False})
    assert st == 200 and body["enabled"] is False


def test_fit_exponent_reads_a_curve_as_a_power():
    assert cv.fit_exponent([0.0, 0.5, 1.0], [0.0, 0.25, 1.0]) == pytest.approx(2.0)
    assert cv.fit_exponent([0.0, 1.0], [0.0, 1.0]) is None


def test_template_path_bends_and_one_chapter_still_has_a_band():
    """The forecast must not be a straight line. One past chapter with an end sprint
    (a = 1.2) gives a convex path ending on its final, and a band from zero now to at least
    +-BAND_FLOOR at the end (one chapter cannot bound itself)."""
    x = 0.25
    tpl = ([0.0, x, 1.0], [0.0, x ** 1.2, 1.0])
    p = cv.predict([(START + int(50 * H * x), 100_000)], start=START, end=END,
                   now=START + int(50 * H * x), templates=[tpl])
    assert p["method"] == "template"
    final = p["final"]
    assert final == round(100_000 / x ** 1.2)
    ys = [q[1] for q in p["points"]]
    steps = [b - a for a, b in zip(ys, ys[1:])]
    assert all(b > a for a, b in zip(steps, steps[1:])), "convex: each step gains more"
    lo, hi = p["band"][0][1], p["band"][0][2]
    assert lo == hi == 100_000
    assert p["band"][-1][1] == pytest.approx(final * (1 - cv.BAND_FLOOR), abs=1)
    assert p["band"][-1][2] == pytest.approx(final * (1 + cv.BAND_FLOOR), abs=1)


def test_a_relay_learns_from_relays_when_it_can(monkeypatch):
    def meta(cid, eid, t0):
        return {"chapter_id": cid, "event_id": eid, "start_time": t0, "chapter_end_time": t0 + 10 * H}
    metas = [meta("relay-b", "marathon_event-relay-002", 300 * H),
             meta("normal-a", "marathon_event-normal-001", 200 * H),
             meta("relay-a", "marathon_event-relay-001", 100 * H)]
    recs = {m["chapter_id"]: [{"kind": "points", "captured_at": m["start_time"] + 2 * H,
                               "borders": [{"rank": 500, "score": 10 if "relay" in m["chapter_id"] else 30}]},
                              {"kind": "points", "captured_at": m["chapter_end_time"],
                               "borders": [{"rank": 500, "score": 100}]}] for m in metas}
    monkeypatch.setattr(cv, "list_chapters", lambda store, region: metas)
    monkeypatch.setattr(cv, "read_chapter", lambda store, region, cid: (None, recs[cid]))
    got = cv.templates_for(None, "us", "points", 500, "relay-b")
    assert got == [([0.0, 0.2, 1.0], [0.0, 0.1, 1.0])]
    assert cv.event_kind("marathon_event-relay-003") == "relay"


def test_history_view_reads_the_tier_from_the_rank_then_the_emblem():
    doc = {"events": {
        "marathon_event-normal-007": {"title": "Neon", "starts_at": "2026-10-15T03:00:00+00:00", "chapters": {
            "c7": {"number": 1, "ranking": "points",
                   "tiers": [{"rank": 1000, "grade_id": "g-diamond"}, {"rank": 5000, "grade_id": "g-platinum"}],
                   "self": {"rank": 2974, "final": True, "captured_at": 9},
                   "final_borders": {"borders": [{"rank": 1000, "score": 2_927_346}, {"rank": 5000, "score": 530_352}]},
                   "event_pt": {"value": 874_103}}}},
        "marathon_event-normal-001": {"chapters": {"c1": {"emblems": {"points": {"grade": "gold"}}}}},
        "marathon_event-normal-006": {"starts_at": "2026-10-07T03:00:00+00:00", "chapters": {
            "c6": {"ranking": "total", "tiers": [{"rank": 500, "grade_id": "g-dream"}],
                   "self": {"rank": None, "out_of_range": True, "final": True}}}},
    }}
    got = cv.history_view(doc, {"g-platinum": {"name": "Platinum", "color1": "A1EFDC"}})
    assert [e["event_id"] for e in got] == ["marathon_event-normal-007", "marathon_event-normal-006",
                                            "marathon_event-normal-001"]
    c7 = got[0]["chapters"][0]
    assert c7["rank"] == 2974 and c7["reached"]["grade_name"] == "Platinum" and c7["event_pt"] == 874_103
    assert [b["score"] for b in c7["borders"]] == [2_927_346, 530_352] and c7["borders_final"]
    assert got[1]["chapters"][0]["outside"] and got[1]["chapters"][0]["reached"] is None
    old = got[2]["chapters"][0]
    assert old["from_emblem"] and old["reached"]["grade_id"] == "marathon_ranking_grade-gold"


def test_a_final_event_pt_is_placed_among_the_final_borders():
    import cutoffs_view as cv
    b = [{"rank": 500, "score": 4000}, {"rank": 1000, "score": 2000}, {"rank": 5000, "score": 400}]
    st = cv.standing_from_points(1357, b)
    assert st["best"] == 5000 and st["from_rank"] == 1001 and st["next"] == {"rank": 1000, "need": 643}
    assert cv.standing_from_points(5000, b)["best"] == 500 and cv.standing_from_points(5000, b)["next"] is None
    low = cv.standing_from_points(100, b)
    assert low["outside"] and low["best"] is None and low["from_rank"] == 5001
    assert cv.standing_from_points(2000, b)["best"] == 1000        # equal to a border: reached
    assert cv.standing_from_points(None, b) is None


def test_history_places_the_owner_by_final_pt_and_by_emblem():
    import cutoffs_view as cv
    tiers = [{"rank": 500, "grade_id": "g-dream"}, {"rank": 1000, "grade_id": "g-diamond"},
             {"rank": 5000, "grade_id": "g-platinum"}]
    fb = {"borders": [{"rank": 500, "score": 4000}, {"rank": 1000, "score": 2000}, {"rank": 5000, "score": 400}]}
    doc = {"events": {
        "new": {"chapters": {"c": {"number": 1, "ranking": "points", "tiers": tiers, "end_time": 100,
                                   "self": {"rank": 800, "final": False, "captured_at": 50},
                                   "event_pt": {"value": 1357, "captured_at": 200}, "final_borders": fb}}},
        "old": {"chapters": {"c": {"number": 1, "ranking": "points", "final_borders": fb,
                                   "emblems": {"points": {"grade": "diamond"}}}}}}}
    for e in doc["events"].values():          # the emblem names grades by their short id
        for c in e["chapters"].values():
            c.get("tiers") and None
    doc["events"]["old"]["chapters"]["c"]["emblems"]["points"]["grade"] = "x"
    view = {e["event_id"]: e for e in cv.history_view(doc)}
    new = view["new"]["chapters"][0]
    assert new["event_pt_final"] and new["standing"]["best"] == 5000      # not the mid-event #800
    assert new["reached"]["grade_id"] == "g-platinum"
    # the emblem's grade id must be one the ladder knows to be placed
    doc["events"]["old"]["chapters"]["c"]["emblems"]["points"]["grade"] = "diamond"
    tiers2 = [dict(t, grade_id="marathon_ranking_grade-" + t["grade_id"][2:]) for t in tiers]
    doc["events"]["new"]["chapters"]["c"]["tiers"] = tiers2
    old = {e["event_id"]: e for e in cv.history_view(doc)}["old"]["chapters"][0]
    assert old["standing"]["best"] == 1000 and old["standing"]["from_rank"] == 501
    assert old["standing"]["score_range"] == [2000, 4000]


def test_own_series_ends_at_the_end_with_the_value_read_after_it():
    # a score cannot move after the end: a read after it is the value at the end
    of_kind = [{"captured_at": 1_000, "self": {"score": 50}},
               {"captured_at": 1_030, "self": {"score": 50}},     # same refresh, seconds apart
               {"captured_at": 500_000, "self": {"score": 900}},
               {"captured_at": 9_000_000, "self": {"score": 1_300}},
               {"captured_at": 900, "self": {"score": None}}]
    s = cv.own_series("no-store", "us", None, None, of_kind, "points", start=0, end=1_000_000)
    assert s == [[1_000, 50], [500_000, 900], [1_000_000, 1_300]]
