"""Event Points mode (`event_mode.py` + the `/api/event*` routes).

No network and no writes: the search runs on a stubbed scorer over a synthetic
pool, the routes on a temporary marathon_bonus.json and a stubbed catalogue.

    python -m pytest tools/web/test_event_mode.py
"""
from __future__ import annotations

import itertools
import json
import random
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import event_mode as em
import server as web_server
from hbuilder.domain import marathon as mar

TYPES = ("Cute", "Cool", "Pure", "Happy")


# --- the L estimate (the game's current song-length formula) ------------------------
@pytest.mark.parametrize("seconds,boosts,expected", [
    (123, 3, 1310),                                   # Shallow Shadow +31 %, read off the game
    (166, 2, 1480), (166, 3, 1480),                   # KANADE 1480, read off the game (both formulas)
    (166, 1, 1520), (166, 4, 1400),                   # 1 boost +300 permil; none at 4+
    (50, 3, 1000), (30, 1, 1000),                     # <= 50 s: no bonus
    (51, 4, 1010),                                    # the bonus is whole percents, rounded up
])
def test_length_estimate_matches_the_measured_points(seconds, boosts, expected):
    assert em.length_permil_estimate(seconds, boosts) == expected


def test_the_pre_update_fit_no_longer_applies():
    """Kyun Kyun MIKO (144 s, 2 boosts) gives 1390 under the current formula; the game's
    earlier formula (free 90 s, 100/190 %/s) gave 1350 there and would predict 1210 for
    Shallow Shadow, where the screen shows +31 % (1310)."""
    assert em.length_permil_estimate(144, 2) == 1390
    assert em.length_permil_estimate(123, 3) != 1210


def test_length_estimate_is_none_without_seconds():
    assert em.length_permil_estimate(None, 3) is None


# --- request validation --------------------------------------------------------
def test_body_defaults():
    opts = em.parse_event_body({"song": "m0342"})
    assert opts == {"objective": "event_points", "song": "m0342", "boosts": 3, "holopass": True,
                    "workers": None, "top": 10, "event": None, "certify": False,
                    "assumptions": None, "filters": {}, "platform": "mobile", "auto": False,
                    "basis": "account"}


def test_body_takes_the_maxed_account_basis_without_account_levers():
    # Maxed account applies to Event Points and Event Song Score too
    assert em.parse_event_body({"song": "m0342", "basis": "maxed"})["basis"] == "maxed"
    with pytest.raises(em.EventRequestError):
        em.parse_event_body({"song": "m0342", "basis": "maxed", "assumptions": {"boards": "plan"}})
    with pytest.raises(em.EventRequestError):
        em.parse_event_body({"song": "m0342", "basis": "everything"})


def test_body_carries_how_the_run_is_played():
    # the page's Platform and Auto must reach the event searches, or every run is ranked
    # as manual on mobile
    opts = em.parse_event_body({"song": "m0342", "platform": "PC", "auto": True})
    assert (opts["platform"], opts["auto"]) == ("pc", True)
    assert em.parse_event_body({"song": "m0342", "auto": "yes"})["auto"] is False


def test_body_takes_the_score_searchs_levers_and_pins():
    opts = em.parse_event_body({"song": "m0342", "assumptions": {"level": "max", "boards": "plan"},
                                "leader_member": "Aki Rosenthal", "require": ["c1"], "exclude": []})
    assert opts["assumptions"] == {"level": "max", "boards": "plan"}
    assert opts["filters"] == {"leader_member": "Aki Rosenthal", "require": ["c1"]}


@pytest.mark.parametrize("body", [
    {"song": "m0342", "objective": "score"},
    {"song": "m0342", "objective": "max_points"},
    {"song": "m0342", "boosts": 0},
    {"song": "m0342", "boosts": 11},
    {"song": "m0342", "boosts": "3"},
    {"song": "m0342", "boosts": True},
    {"song": "m0342", "holopass": "yes"},
    {"song": "m0342", "workers": 0},
    {"objective": "event_points"},
    {"song": "m0342", "assumptions": {"boards": "rebuilt"}},
    {"song": "m0342", "assumptions": {"level": 99}},
    {"song": "m0342", "assumptions": "max"},
])
def test_bad_bodies_raise(body):
    with pytest.raises(em.EventRequestError):
        em.parse_event_body(body)


# --- the search on a stubbed leaf -------------------------------------------------
def _pool(n: int = 11, seed: int = 7):
    rng = random.Random(seed)
    return [SimpleNamespace(id=f"card-{i:05d}-5-uniq-0000-00", member=f"Chr{i}",
                            costume_name=f"Outfit{i}", rarity="5★", type=rng.choice(TYPES),
                            generations=frozenset({f"g{i % 3}"}), strength=rng.uniform(0.6, 1.4))
            for i in range(n)]


def _scorer(cards):
    def score(leader, sets):
        lead = 1.0 + 0.05 * (int(leader.id.split("-")[1]) % 4)
        return [round(1_000_000 * lead * sum(cards[i].strength for i in s) / 5.0) for s in sets]
    return score


def _bonus(n: int):
    # live bonus on a few weak-ish cards so the event answer differs from the score answer;
    # song bonus on two cards of different size (MAX, not sum)
    live = {0: 300, 3: 600, 5: 370, 8: 150}
    song = {3: 100, 8: 50}
    return [(live.get(i, 0), song.get(i, 0)) for i in range(n)]


def _brute(cards, leaders, score, bonus, objective):
    best = None
    for leader in leaders:
        sets = [s for s in itertools.combinations(range(len(cards)), 5)
                if len({cards[i].member for i in s}) == 5]
        for s, raw in zip(sets, score(leader, sets)):
            e = sum(bonus[i][0] for i in s)
            p = max(bonus[i][1] for i in s)
            value = mar.event_objective(raw, objective=objective, live_permil=e, song_permil=p)
            if best is None or value > best[0]:
                best = (value, leader.id, s, raw, e, p)
    return best


@pytest.mark.parametrize("engine,options", [
    ("joint", {"initial_sets": 200, "step": 100}),
    ("two_stage", {"initial": 200, "step": 100}),
])
@pytest.mark.parametrize("objective", ["event_points", "event_song"])
def test_search_ranks_by_the_objective_and_finds_the_true_best(objective, engine, options):
    cards = _pool()
    score = _scorer(cards)
    bonus = _bonus(len(cards))
    found = em.search_event(reps=cards, leaders=cards, score=score, bonus=bonus,
                            objective=objective, base_leader=cards[1], top=5,
                            search_options=options, engine=engine)
    rows = found["rows"]
    values = [r["objective"] for r in rows]
    assert values == sorted(values, reverse=True)
    for r in rows:
        e = sum(bonus[i][0] for i in r["members"])          # the leader adds nothing
        p = max(bonus[i][1] for i in r["members"])          # MAX, not a sum
        assert (r["live_permil"], r["song_permil"]) == (e, p)
        assert r["objective"] == pytest.approx(mar.event_objective(
            r["score"], objective=objective, live_permil=e, song_permil=p))
    best = _brute(cards, cards, score, bonus, objective)
    assert rows[0]["objective"] == pytest.approx(best[0])
    assert found["bound"]["certified"] is True
    assert found["bound"]["objective"] == objective
    assert ("e_min" in found["bound"]) == (objective == "event_points")


def test_event_song_takes_the_max_not_the_sum():
    cards = _pool()
    bonus = _bonus(len(cards))
    members = (2, 3, 4, 6, 8)           # song bonus 100 and 50 -> P = 100, not 150
    r = {"leader": cards[0], "members": members, "score": 1_000_000.0,
         "live_permil": sum(bonus[i][0] for i in members),
         "song_permil": max(bonus[i][1] for i in members), "objective": 0.0,
         "source": "event_search"}
    out = em.row_payload(1, r, reps=cards, bonus_of=lambda c: bonus[cards.index(c)],
                         card_state=lambda _i: {}, boosts=3, length_permil=1480, holopass=True)
    assert out["song_permil"] == 100
    assert out["event_song_score"] == mar.event_song_score(1_000_000, 100) == 1_100_000


def test_row_numbers_match_marathon_and_the_leader_does_not_count():
    cards = _pool()
    bonus = _bonus(len(cards))
    members = (1, 2, 4, 6, 7)
    leader = cards[3]                   # the strongest live bonus, as leader: worth nothing
    raw = 1_234_567.4
    e = sum(bonus[i][0] for i in members)
    r = {"leader": leader, "members": members, "score": raw, "live_permil": e,
         "song_permil": 0, "objective": 0.0, "source": "event_search"}
    out = em.row_payload(1, r, reps=cards, bonus_of=lambda c: bonus[cards.index(c)],
                         card_state=lambda _i: {"level": 60, "bloom": 2}, boosts=3,
                         length_permil=1480, holopass=True)
    assert out["live_permil"] == e == 0
    assert out["leader"]["live_permil"] == 600 and out["leader"]["counts"] is False
    assert all(m["counts"] for m in out["members"])
    assert out["event_points"] == mar.event_points(1_234_567, e, boosts=3, length_permil=1480,
                                                   holopass=True)
    assert out["event_points_base"] == mar.event_points_base(1_234_567, e)
    for key in ("id", "member", "costume_name", "chr", "type", "rarity", "level", "bloom",
                "owned", "live_permil", "song_permil"):
        assert key in out["leader"] and key in out["members"][0]
    assert out["leader"]["rarity"] == 5 and out["leader"]["chr"] == "chr-00003"


def test_sets_above_counts_only_legal_sets_over_the_threshold():
    hol = ["A", "B", "C", "D", "E", "F", "A"]
    values = [400, 400, 0, 0, 0, 0, 400]
    sets, overflow = em.sets_above(values, hol, 500, use_max=False, limit=1000)
    assert not overflow
    assert all(sum(values[i] for i in s) > 500 for s in sets)
    assert all(len({hol[i] for i in s}) == 5 for s in sets)
    assert len(sets) == 2 * 4            # (0 or 6) with 1, plus 3 of the 4 plain cards
    assert em.sets_above(values, hol, -1, use_max=False, limit=10) == ([], True)


# --- the job ------------------------------------------------------------------------
def _drain(job):
    events = []
    while True:
        ev = job.events.get(timeout=10)
        events.append(ev)
        if ev["type"] == "end":
            return events


GROUP = "marathon_event-normal-009-chapter-01"
EVENT_RAW = {
    "current_group_ids": [f"live_marathon_score_bonus-{GROUP}", f"music_score_bonus-{GROUP}"],
    "live_fetched_at": 1.0,
    "groups": [
        {"group_id": f"live_marathon_score_bonus-{GROUP}", "kind": "live", "rows": [
            {"character_id": "chr-00001", "permil": 300},
            {"card_id": "card-00001-5-uniq-0001-00", "permil": 300},
            {"rarity": 4, "bloom": 2, "permil": 70}]},
        {"group_id": f"music_score_bonus-{GROUP}", "kind": "music", "rows": [
            {"music_id": "m0001", "card_id": "card-00001-5-uniq-0001-00", "permil": 100}]},
    ]}


def _in_memory_event(monkeypatch):
    """No file on disk (test_no_writes forbids writing one): the bundle loader is
    replaced by the in-memory parse of EVENT_RAW, and the path made to look present."""
    monkeypatch.setattr(em, "load_event_bundle",
                        lambda _path, key=None: em.bundle_from_raw(EVENT_RAW, key))
    monkeypatch.setattr(web_server, "EVENT_BONUS_PATH", Path(__file__))


def test_bundle_picks_the_newest_open_event():
    raw = dict(EVENT_RAW, current_group_ids=EVENT_RAW["current_group_ids"]
               + ["live_marathon_score_bonus-marathon_event-normal-002-chapter-01"])
    _raw, event, keys = em.bundle_from_raw(raw)
    assert keys == ["marathon_event-normal-002-chapter-01", GROUP]
    assert event.name == GROUP and len(event.rows_for("live")) == 3
    with pytest.raises(mar.SchemaError):
        em.bundle_from_raw({"current_group_ids": [], "groups": []})


def test_job_emits_plan_result_end(tmp_path, monkeypatch):
    _in_memory_event(monkeypatch)
    monkeypatch.setattr(web_server, "COLLECTION_PATH", tmp_path / "none.json")
    seen = {}

    def runner(opts, *, emit, cancel, event, collection, song_info, workers):
        seen.update(event=event.name, song_info=song_info)
        emit({"type": "stage", "stage": "build", "state": "done"})
        return {"rows": [], "objective": opts["objective"]}

    job = web_server.Job(job_id="t1")
    web_server.run_event_job(job, {"objective": "event_points", "song": "m0001", "boosts": 3,
                                   "holopass": True, "top": 5, "difficulty": "EXPERT",
                                   "workers": 1, "event": None}, runner=runner)
    types = [e["type"] for e in _drain(job)]
    assert types == ["plan", "stage", "result", "end"]
    assert seen["event"] == "marathon_event-normal-009-chapter-01"


def test_job_reports_a_cancel(monkeypatch):
    _in_memory_event(monkeypatch)

    def runner(opts, **_kw):
        raise em.EventCancelled()

    job = web_server.Job(job_id="t2")
    web_server.run_event_job(job, {"song": "m0001", "objective": "event_points", "event": None},
                             runner=runner)
    assert [e["type"] for e in _drain(job)] == ["plan", "cancelled", "end"]


# --- HTTP ---------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def web_base():
    port = _free_port()
    server = web_server.ThreadingServer((web_server.BIND_HOST, port), web_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{web_server.BIND_HOST}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _post(url: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_event_points_route_takes_the_room_multi_score(web_base):
    """The page's Multi Live box: the player types the room's MULTI SCORE and reads the
    Event Pt (checked against real multi Lives, `marathon.event_points_base`)."""
    # a relay event's Live (Shallow Shadow, relay-003): the room term's divisor is 75000 there
    play = {"score": 1_469_887, "live_permil": 1110, "boosts": 3, "length_permil": 1310,
            "holopass": True, "event": "marathon_event-relay-003-chapter-02"}
    status, doc = _post(web_base + "/api/event/points", {**play, "multi_score": 5_143_567})
    assert status == 200 and doc["event_points"] == 38_043 and doc["multi"] is True
    status, solo = _post(web_base + "/api/event/points", play)
    assert status == 200 and solo["multi"] is False and solo["event_points"] < 38_043
    status, doc = _post(web_base + "/api/event/points", {**play, "multi_score": -5})
    assert status == 400 and "multi_score" in doc["error"]


def test_event_route_is_503_without_the_bonus_file(web_base, tmp_path, monkeypatch):
    monkeypatch.setattr(web_server, "EVENT_BONUS_PATH", tmp_path / "missing.json")
    status, doc = _get(web_base + "/api/event")
    assert status == 503 and doc["available"] is False and "marathon_bonus.json" in doc["error"]
    status, doc = _post(web_base + "/api/event/run", {"song": "m0342"})
    assert status == 503


def test_event_route_shape(web_base, tmp_path, monkeypatch):
    _in_memory_event(monkeypatch)
    monkeypatch.setattr(web_server, "EVENT_META_PATH", tmp_path / "no_meta.json")
    card = lambda cid, member, rarity: SimpleNamespace(  # noqa: E731
        id=cid, member=member, costume_name="X", rarity=rarity, type="Cute",
        generations=frozenset())
    cards = (card("card-00001-5-uniq-0001-00", "One", "5★"),
             card("card-00002-4-cmmn-0000-00", "Two", "4★"),
             card("card-00003-3-nrml-0000-00", "Three", "3★"))
    monkeypatch.setattr(web_server, "load_inputs", lambda *a, **k: (cards, cards, None, None))
    collection = {"cards": {
        "card-00001-5-uniq-0001-00": {"level": 80, "bloom": 1},
        "card-00002-4-cmmn-0000-00": {"level": 60, "bloom": 2},
        "card-00003-3-nrml-0000-00": {"level": 40, "bloom": 2}}}
    real_read = web_server._read_json
    monkeypatch.setattr(web_server, "_read_json", lambda path: collection
                        if path == web_server.COLLECTION_PATH else real_read(path))
    status, doc = _get(web_base + "/api/event")
    assert status == 200
    assert doc["event"] == {"key": "marathon_event-normal-009-chapter-01", "title": None,
                            "kind_label": None, "starts_at": None, "ends_at": None,
                            "art": None, "logo": None, "chapter": "01"}
    assert doc["live"]["holomems"] == [{"character_id": "chr-00001", "name": "One", "permil": 300}]
    assert [c["id"] for c in doc["live"]["cards"]] == ["card-00001-5-uniq-0001-00"]
    assert doc["live"]["rarity_bloom"] == {"4": {"2": 70}}
    assert doc["music"][0]["music_id"] == "m0001" and doc["music"][0]["permil"] == 100
    mine = doc["owned_bonus_cards"]
    # One: holomem 300 + card 300; Two: 4★ bloom 2 -> 70; Three: 3★ has no row
    assert [(c["id"], c["live_permil"], c["song_permil"]) for c in mine] == [
        ("card-00001-5-uniq-0001-00", 600, 100), ("card-00002-4-cmmn-0000-00", 70, 0)]
    assert mine[0]["level"] == 80 and mine[0]["rarity"] == 5 and mine[0]["owned"] is True
    assert mine[0]["chr"] == "chr-00001" and mine[0]["type"] == "Cute"


@pytest.mark.parametrize("body,needle", [
    ({"song": "m0342", "objective": "nope"}, "objective"),
    ({"song": "m0342", "boosts": 0}, "boosts"),
    ({"song": "m0342", "boosts": 11}, "boosts"),
    ({"song": "m0342", "difficulty": "insane"}, "difficulty"),
    ({"boosts": 3}, "song"),
])
def test_event_run_rejects_bad_bodies(web_base, body, needle):
    status, doc = _post(web_base + "/api/event/run", body)
    assert status == 400, doc
    assert needle in doc["error"]


def test_event_songs_route(web_base):
    status, doc = _get(web_base + "/api/event/songs?boosts=0")
    assert status == 400
    status, doc = _get(web_base + "/api/event/songs?difficulty=insane")
    assert status == 400
    status, doc = _get(web_base + "/api/event/songs?boosts=3")
    assert status == 200 and doc["length_multiplier_estimated"] is True
    lengths = [s["length_permil"] or 0 for s in doc["songs"]]
    assert lengths == sorted(lengths, reverse=True)


def test_event_page_is_served(web_base):
    if not (web_server.STATIC_DIR / "event.html").is_file():
        pytest.skip("static/event.html is not on disk yet")
    with urllib.request.urlopen(web_base + "/event", timeout=30) as response:
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/html")


# --- the event's title, kind, times and banner (event_art.py -> event_meta.json) ----------
META = {"events": [{
    "event_id": "marathon_event-normal-009", "kind_label": "Point Rally Event",
    "starts_at": "2026-10-15T03:00:00+00:00", "ends_at": "2026-10-17T10:59:59+00:00",
    "title": "Neon Sunshine Festival",
    "chapters": [{"id": GROUP, "number": 1, "ends_at": "2026-10-17T10:59:59+00:00",
                  "title": "Neon Sunshine Festival"}],
    "art": {"banner": "test_event_mode.py", "logo": "missing_logo.webp"}}]}


def test_event_meta_fields_fill_from_the_meta_file():
    here = Path(__file__).resolve().parent
    got = em.event_meta_fields(META, GROUP, here)
    assert got == {"title": "Neon Sunshine Festival", "kind_label": "Point Rally Event",
                   "starts_at": "2026-10-15T03:00:00+00:00",
                   "ends_at": "2026-10-17T10:59:59+00:00",
                   # a file on disk -> a URL; a file that is not -> null
                   "art": "/eventart/test_event_mode.py", "logo": None}


def test_event_meta_fields_are_null_without_a_source():
    nulls = dict.fromkeys(em.EVENT_META_KEYS)
    assert em.event_meta_fields(None, GROUP, None) == nulls
    assert em.event_meta_fields({"events": []}, GROUP, None) == nulls
    other = {"events": [dict(META["events"][0], event_id="marathon_event-normal-010",
                             chapters=[])]}
    assert em.event_meta_fields(other, GROUP, None) == nulls
    # title and times survive without an art dir; art does not
    got = em.event_meta_fields(META, GROUP, None)
    assert got["title"] == "Neon Sunshine Festival" and got["art"] is None


def _get_raw(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_eventart_route_404s_on_missing_and_bad_names(web_base, tmp_path, monkeypatch):
    monkeypatch.setattr(web_server, "EVENT_ART_DIR", tmp_path)
    assert _get_raw(web_base + "/eventart/marathon_event-normal-009.webp")[0] == 404
    assert _get_raw(web_base + "/eventart/..%2Fsecret.webp")[0] == 404
    assert _get_raw(web_base + "/eventart/x.png")[0] == 404


def test_eventart_route_serves_the_refreshed_banner(web_base):
    files = sorted(web_server.EVENT_ART_DIR.glob("*.webp")) if web_server.EVENT_ART_DIR.is_dir() else []
    if not files:
        pytest.skip("no event art on disk (event_art.py has not run)")
    status, body = _get_raw(web_base + "/eventart/" + files[0].name)
    assert status == 200 and body[:4] == b"RIFF"
    status, small = _get_raw(web_base + "/eventart/" + files[0].name + "?w=200")
    assert status == 200 and small[:4] == b"RIFF"


def test_a_spotlight_event_defaults_to_the_chapter_open_now():
    """A relay event (relay-003) lists all four chapters at once; the default is the one
    whose window holds now (chapter i starts when chapter i-1 ends), not the newest key."""
    from hbuilder.domain import marathon as mar
    meta = {"events": [{"starts_at": "2026-10-19T11:00:00+00:00", "chapters": [
        {"id": "e-chapter-02", "number": 2, "ends_at": "2026-10-23T10:59:59+00:00"},
        {"id": "e-chapter-01", "number": 1, "ends_at": "2026-10-21T10:59:59+00:00"}]}]}
    w = em.chapter_windows(meta)
    assert w["e-chapter-01"][1] + 1000 == w["e-chapter-02"][0]
    keys = ["e-chapter-01", "e-chapter-02"]
    day1 = w["e-chapter-01"][0] + 3600_000
    assert mar.pick_chapter(keys, w, day1) == "e-chapter-01"
    assert mar.pick_chapter(keys, w, w["e-chapter-02"][0] + 1) == "e-chapter-02"
    assert mar.pick_chapter(keys, w, w["e-chapter-02"][1] + 10**9) == "e-chapter-02"   # ended: the latest
    assert mar.pick_chapter(keys) == "e-chapter-02"                                    # no times: as before


# --- the pre-replan screen (the Score search's `_screen_for_replan`) ----------
def test_screen_rows_reranks_the_head_by_the_objective_on_the_refined_score(monkeypatch):
    import hbuilder.domain.team as team_mod
    monkeypatch.setattr(team_mod, "Team", lambda leader, members: (leader, members))

    reps = [SimpleNamespace(id=f"c{i}") for i in range(10)]
    bonus = [(100, 0)] * 5 + [(300, 0)] * 5   # members 5..9 carry more event bonus
    leader = SimpleNamespace(id="L")
    rows = [dict(leader=leader, members=(0, 1, 2, 3, 4), score=2_000_000.0),
            dict(leader=leader, members=(5, 6, 7, 8, 9), score=1_900_000.0),
            dict(leader=leader, members=(0, 1, 2, 3, 5), score=1_800_000.0)]
    # the screen's activation refine lifts the third row; it never lowers one
    refined = {(0, 1, 2, 3, 4): 1_000_000.0, (5, 6, 7, 8, 9): 1_900_000.0,
               (0, 1, 2, 3, 5): 2_600_000.0}

    class Pool:
        def batch_score(self, teams, refine_activation=None):
            assert refine_activation is True
            return [refined[tuple(int(m.id[1:]) for m in t[1])] for t in teams]

    monkeypatch.setattr(em, "REPLAN_SCREEN", 2)
    out = em._screen_rows(rows, reps, bonus, "event_points", Pool())
    # head (rows 0, 1) re-ranked: row 1's bonus beats row 0's score; row 2 stays outside
    assert [r["members"] for r in out] == [(5, 6, 7, 8, 9), (0, 1, 2, 3, 4), (0, 1, 2, 3, 5)]
    assert out[1]["score"] == 2_000_000.0   # numbers untouched: only the order changes


def test_refined_rows_keep_the_better_score_and_rerank_by_the_objective(monkeypatch):
    import hbuilder.domain.team as team_mod
    monkeypatch.setattr(team_mod, "Team", lambda leader, members: (leader, members))
    # boards planned with no wallet limit: the best rows get the activation refinement for
    # the song (as the Score search does); a row keeps the better of its two scores, is
    # marked `refined` when the refinement won, and the head is re-ranked by the objective
    reps = [SimpleNamespace(id=f"c{i}") for i in range(10)]
    bonus = [(0, 0)] * 10                                       # objective = the score itself
    leader = SimpleNamespace(id="L")
    rows = [dict(leader=leader, members=(0, 1, 2, 3, 4), score=2_000_000.0, objective=2_000_000.0),
            dict(leader=leader, members=(5, 6, 7, 8, 9), score=1_900_000.0, objective=1_900_000.0),
            dict(leader=leader, members=(0, 1, 2, 3, 5), score=1_800_000.0, objective=1_800_000.0)]
    refined = {(0, 1, 2, 3, 4): 1_950_000.0, (5, 6, 7, 8, 9): 2_100_000.0}

    class Pool:
        def batch_score(self, teams, refine_activation=None):
            assert refine_activation is True
            return [refined[tuple(int(m.id[1:]) for m in t[1])] for t in teams]

    out = em._refine_rows(rows, reps, bonus, "event_song", Pool(), 2)
    assert [r["members"] for r in out] == [(5, 6, 7, 8, 9), (0, 1, 2, 3, 4), (0, 1, 2, 3, 5)]
    assert out[0]["score"] == 2_100_000.0 and out[0]["refined"] is True
    assert out[1]["score"] == 2_000_000.0 and not out[1].get("refined")   # never lowered
    assert out[2] is rows[2]                                              # outside the head


@pytest.mark.parametrize("objective", ["event_points", "event_song"])
def test_the_certified_bound_keeps_the_pins(objective):
    # the bound must respect the pins: otherwise its best -- without the required card,
    # or with the leader's holomem among the members -- would be served #1 and marked
    # "Checked"
    cards = _pool()
    score = _scorer(cards)
    bonus = _bonus(len(cards))
    free = em.search_event(reps=cards, leaders=cards, score=score, bonus=bonus, objective=objective,
                           base_leader=cards[1], top=5, engine="joint",
                           search_options={"initial_sets": 200, "step": 100})
    pin = next(i for i in range(len(cards)) if i not in free["rows"][0]["members"])

    def allowed(li, s):
        return all(cards[j].member != cards[li].member for j in s)
    found = em.search_event(reps=cards, leaders=cards, score=score, bonus=bonus, objective=objective,
                            base_leader=cards[1], top=5, engine="joint",
                            search_options={"initial_sets": 200, "step": 100, "require": [pin],
                                            "allowed": allowed})
    assert found["bound"]["certified"] is True
    for r in found["rows"]:
        assert pin in r["members"]
        assert all(cards[j].member != r["leader"].member for j in r["members"])
