"""The optimiser on demand: `POST /api/search` and its job (`server.run_search_job`)
and the request parsing of `ondemand.py`. No leaf, no pool, no writes: the job
runs on a stub engine. The search itself is held in `test_joint_search.py`.

    python -m pytest tools/web/test_ondemand.py
"""
from __future__ import annotations

import queue

import ondemand
import server as web_server


def _drain(job) -> list[dict]:
    out = []
    while True:
        try:
            out.append(job.events.get_nowait())
        except queue.Empty:
            return out


class _Stub:
    def __init__(self, raise_=None):
        self.raise_ = raise_
        self.calls = []

    def search(self, song, filters, *, emit, cancel, top):
        self.calls.append((song, filters, top))
        if self.raise_:
            raise self.raise_
        emit({"type": "progress", "stage": "search", "round": 1, "evaluated": 10,
              "best_so_far": {"value": 123.0, "metric": "song_est"}})
        return {"scope": "song", "song": song, "ranking": [], "on_demand": True}


def _opts(**kw):
    base = {"song": "m0342", "basis": "account", "difficulty": "EXPERT", "top": 50,
            "filters": ondemand.Filters()}
    base.update(kw)
    return base


def test_job_emits_plan_progress_result_end():
    stub = _Stub()
    job = web_server.Job(job_id="s1")
    web_server.run_search_job(job, _opts(), engine_for=lambda b, d: stub)
    events = _drain(job)
    assert [e["type"] for e in events] == ["plan", "stage", "stage", "stage", "progress", "result", "end"]
    # the account load is its own stage (the page shows "Loading your account"), then the search
    assert [(e["stage"], e["state"]) for e in events[1:4]] == [
        ("build", "active"), ("build", "done"), ("search", "active")]
    assert events[4]["best_so_far"]["value"] == 123.0          # JobProgress passes it on
    assert events[5]["payload"]["on_demand"] is True
    assert stub.calls[0][0] == "m0342"


def test_job_reports_cancel_and_a_filter_error_as_events():
    job = web_server.Job(job_id="s2")
    web_server.run_search_job(job, _opts(), engine_for=lambda b, d: _Stub(ondemand.Cancelled()))
    assert [e["type"] for e in _drain(job)] == ["plan", "stage", "stage", "stage", "cancelled", "end"]
    job = web_server.Job(job_id="s3")
    web_server.run_search_job(job, _opts(), engine_for=lambda b, d: _Stub(KeyError("no card matches")))
    events = _drain(job)
    assert [e["type"] for e in events] == ["plan", "stage", "stage", "stage", "error", "end"]
    assert events[4]["message"] == "no card matches"


def test_parse_filters_reads_lists_and_comma_strings():
    f = ondemand.parse_filters({"leader_member": "Takane Lui", "require": "a,b", "exclude": ["c"],
                                "no_leader_overlap": 1})
    assert f == ondemand.Filters(leader_member="Takane Lui", require=("a", "b"), exclude=("c",),
                                 no_leader_overlap=True)
    assert ondemand.parse_filters({}) == ondemand.Filters()
    assert f.key() == ondemand.parse_filters({"leader_member": "Takane Lui", "require": ["b", "a"],
                                              "exclude": "c", "no_leader_overlap": True}).key()


# --- the engine on a fake leaf ---------------------------------------------------
import random  # noqa: E402
import shutil  # noqa: E402
import threading  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402


def _fake_engine(n_cards: int = 14, n_charts: int = 9):
    rng = random.Random(3)
    reps = [SimpleNamespace(id=f"card-{i:05d}-5-uniq-{i:04d}-00", member=f"Chr{i % 11}",
                            costume_name=f"Outfit{i}", type=rng.choice(["Cute", "Cool"]),
                            generations=frozenset([rng.choice(["A", "B"])]), w=rng.uniform(1, 2))
           for i in range(n_cards)]
    charts = [SimpleNamespace(music_id=f"m{k:04d}", difficulty="EXPERT", singers=(), singer_type=None,
                              notes=[0] * (100 + 10 * k)) for k in range(n_charts)]
    calls = []

    class Pool:
        def batch_score(self, teams, chart_indices=None, refine_activation=None):
            idx = range(n_charts) if chart_indices is None else chart_indices
            calls.append((len(teams), None if chart_indices is None else tuple(chart_indices)))
            out = []
            for t in teams:
                base = sum(m.w for m in t.members) * (1.1 if t.leader.type == "Cute" else 1.0)
                out.append(sum(base * 1000 * (1 + 0.01 * k) for k in idx) / len(idx)
                           + (5 if refine_activation else 0))
            return out

        def batch_score_per_chart(self, teams):
            calls.append((len(teams), "per_chart"))
            return [[sum(m.w for m in t.members) * 1000 * (1 + 0.01 * k) for k in range(n_charts)]
                    for t in teams]

    eng = object.__new__(ondemand.Engine)
    eng.basis, eng.difficulty, eng.assumptions, eng.levels = "account", "EXPERT", None, None
    eng.mf = SimpleNamespace(gate_of=lambda c: None)
    eng.charts, eng.index = charts, {c.music_id: k for k, c in enumerate(charts)}
    eng.reps, eng.by_id = reps, {c.id: c for c in reps}
    eng.pool, eng.lock, eng.hints = Pool(), threading.Lock(), {}
    eng.started_seconds, eng.closed, eng.key = 0.0, False, "k"
    eng._sample, eng._wide = None, ()
    return eng, calls


@pytest.mark.skipif(shutil.which("gcc") is None, reason="no gcc to build the screen")
def test_engine_song_and_general_payloads(monkeypatch):
    from hbuilder.domain import team as team_mod

    monkeypatch.setattr(ondemand, "GENERAL_SAMPLE", 3)
    monkeypatch.setattr(ondemand, "SAMPLE_TRAIN", 40)
    monkeypatch.setattr(ondemand, "GENERAL_WIDE", 5)
    monkeypatch.setattr(ondemand, "FITTED_SAMPLE", 3)
    # the fake cards are not Card instances; the engine only builds and passes teams
    monkeypatch.setattr(team_mod, "Team", lambda leader, members: SimpleNamespace(leader=leader, members=members))
    eng, calls = _fake_engine()
    eng.basis = "maxed"                                           # a GENERAL_FITTED basis
    song = eng.search("m0002")
    assert song["scope"] == "song" and song["ranking"][0]["refined"] is True
    assert all(c[1] == (2,) for c in calls)                       # only that chart
    calls.clear()
    gen = eng.search(None)
    assert gen["scope"] == "general" and gen["song"] is None and gen["charts"] == 9
    assert len(gen["sample"]) == 3
    assert any(c[1] is None for c in calls)                       # the re-score on every chart
    assert any(c[1] not in (None, "per_chart") and len(c[1]) == 5 for c in calls)   # the climb, on the wide sample
    assert tuple(gen_sample := eng._wide)[:3] == eng._sample and len(gen_sample) == 5
    values = [r["best"] for r in gen["ranking"]]
    assert values == sorted(values, reverse=True)
    # the served number is the FULL mean (every chart), not the sample's
    by_id = eng.by_id
    top = gen["ranking"][0]
    team = SimpleNamespace(leader=by_id[top["leader"]], members=[by_id[m] for m in top["members"]])
    assert top["best"] == pytest.approx(eng.pool.batch_score([team])[0])
    # the sample was fitted on random teams scored on every chart, once: a second
    # search on the same engine fits nothing; a new engine fits the same sample
    assert any(c[1] == "per_chart" for c in calls)
    calls.clear()
    eng.search(None)
    assert not any(c[1] == "per_chart" for c in calls)
    again, _again_calls = _fake_engine()
    again.basis = "maxed"
    again.search(None)
    assert again._sample == eng._sample
    # any other basis, or a level range: the note-spread sample, nothing fitted, no climb
    for basis, levels in (("account", None), ("maxed", (25, 30))):
        other, other_calls = _fake_engine()
        other.basis, other.levels = basis, levels
        other.search(None)
        assert not any(c[1] == "per_chart" for c in other_calls)
        assert other._wide == () and len(other._sample) == (3 if basis == "account" else 9)


def test_assumptions_default_is_the_account_untouched_and_levers_parse():
    a = ondemand.parse_assumptions({})
    assert a == ondemand.Assumptions() and a.boards == "as_is" and a.level == "account"
    assert a.describe() == "your cards as they are; your Holomem Boards exactly as they are"
    b = ondemand.parse_assumptions({"level": "60", "bloom": 2, "keep_higher": True, "boards": "plan",
                                    "max_points": True, "infinite": True})
    assert (b.level, b.bloom, b.keep_higher, b.boards, b.max_points, b.infinite) == (60, 2, True, "plan", True, True)
    assert b.notes() == []
    assert ondemand.parse_assumptions({"boards": "plan"}).notes()      # materials not enforced: said
    assert ondemand.parse_assumptions({"level": "max"}).level == "max"
    for bad in ({"boards": "nope"}, {"level": 99}, {"bloom": 7}, {"level": "x"}):
        with pytest.raises(ValueError):
            ondemand.parse_assumptions(bad)
    assert a.key() != b.key()


def test_materials_mode_and_reset_cap():
    a = ondemand.parse_assumptions({"boards": "plan"})
    assert a.materials and "re-planned within your cubes and hexas" in a.notes()[0]
    assert "within your cubes and hexas" in a.describe()
    assert not ondemand.parse_assumptions({"boards": "plan", "infinite": True}).materials
    assert not ondemand.parse_assumptions({}).materials                  # as they are
    assert ondemand.parse_assumptions({"boards": "plan", "max_resets": -1}).max_resets is None
    capped = ondemand.parse_assumptions({"boards": "plan", "max_resets": "5"})
    assert capped.max_resets == 5 and "at most 5 resets" in capped.describe()
    assert capped.key() != a.key()
    with pytest.raises(ValueError):
        ondemand.parse_assumptions({"boards": "plan", "max_resets": -3})


def test_the_remaining_holodori_levers_parse_and_trigger_the_exact_replan():
    a = ondemand.parse_assumptions({"boards": "plan", "infinite": True})
    assert not a.replan                                     # nothing the quick plan misses
    b = ondemand.parse_assumptions({"boards": "plan", "infinite": True, "branches": "gy"})
    assert b.branches == "GY" and b.replan and "only G/Y" in b.describe()
    c = ondemand.parse_assumptions({"boards": "plan", "infinite": True,
                                    "targets": [{"chr": "chr-00036", "node": "R-044", "mode": "keep"},
                                                {"chr": "chr-00036", "node": "G-024"}]})
    assert c.replan and c.targets == (("chr-00036", "G-024", "buy"), ("chr-00036", "R-044", "keep"))
    assert c.plan_opts()["targets"] == {"chr-00036": {"G-024": "buy", "R-044": "keep"}}
    d = ondemand.parse_assumptions({"boards": "plan", "no_refund": True, "all_invited": True,
                                    "sp_training": 2, "level": "max"})
    opts = d.plan_opts()
    assert opts["no_refund"] and opts["all_invited"] and opts["resources"] == "inventory"
    assert d.sp_training == 2 and "SP training 2" in d.describe() and "no refund" in d.describe()
    # no refund means nothing with unlimited materials
    assert not ondemand.parse_assumptions({"boards": "plan", "infinite": True,
                                           "no_refund": True}).plan_opts()["no_refund"]
    for bad in ({"branches": "x"}, {"sp_training": 5}, {"targets": [{"chr": "x", "node": "G-1"}]},
                {"targets": [{"chr": "chr-1", "node": "G-1", "mode": "maybe"}]}):
        with pytest.raises(ValueError):
            ondemand.parse_assumptions(bad)
    assert len({x.key() for x in (a, b, c, d)}) == 4


def test_reset_min_gain_is_a_percent_lever_passed_to_the_plan_as_a_fraction():
    """A reset must earn this share of the team's score (the cost of a reset); within
    the account's materials only, never with unlimited cubes."""
    a = ondemand.parse_assumptions({"boards": "plan", "reset_min_gain": "0.05"})
    assert a.reset_min_gain == 0.05
    assert a.plan_opts()["reset_min_gain"] == 0.0005
    assert "0.05 %" in a.describe()
    assert a.key() != ondemand.parse_assumptions({"boards": "plan"}).key()
    inf = ondemand.parse_assumptions({"boards": "plan", "infinite": True, "reset_min_gain": 0.2})
    assert inf.plan_opts()["reset_min_gain"] == 0.0
    assert ondemand.parse_assumptions({}).reset_min_gain == 0.0
    with pytest.raises(ValueError):
        ondemand.parse_assumptions({"boards": "plan", "reset_min_gain": 9})



def test_five_star_pool_limits_the_members_not_the_leader():
    """'Only 5*' applies to the five members; an outfit leader of any rarity
    stays a candidate. The on-demand search must honour the switch."""
    from types import SimpleNamespace as C
    reps = [C(id="card-00001-5-uniq-0001-00"), C(id="card-00002-4-uniq-0002-00"),
            C(id="card-00003-3-uniq-0003-00"), C(id="card-00004-5-uniq-0004-00")]
    f = ondemand.parse_filters({"pool": "5star", "exclude": ["card-00004-5-uniq-0004-00"]})
    assert f.members_five_star and f.key() != ondemand.parse_filters({}).key()
    pool, leaders = ondemand.candidates(reps, f)
    assert [c.id for c in pool] == ["card-00001-5-uniq-0001-00"]
    assert [c.id for c in leaders] == ["card-00001-5-uniq-0001-00", "card-00002-4-uniq-0002-00",
                                       "card-00003-3-uniq-0003-00"]
    pool, leaders = ondemand.candidates(reps, ondemand.parse_filters({}))
    assert len(pool) == len(leaders) == 4


def test_song_board_bonus_reports_yellow_per_singer_capped_and_the_singer_reds():
    """The page shows what the boards add on THIS song -- yellow per singer
    against the 100-permil cap, and the red singer-conditional terms."""
    from types import SimpleNamespace as NS

    import row_detail

    class Board:
        def song_score_bonus_by_singer(self, singers, *, scope):
            assert scope == "group"
            return {"A": 0.075, "B": 0.040}
        def conditional_skill_effect_up_for(self, members, leader, singers):
            return 0.041 if leader == "A" else 0.0
        def conditional_stat_power_for(self, members, leader, singers):
            return 1234.4 if leader == "A" else 0.0

    team = NS(leader=NS(member="A"), members=())
    got = row_detail.song_board_bonus(team, NS(board=Board()), NS(singers=("chr-1", "chr-2"),
                                                                   singer_type="group"))
    assert got["yellow_permil"] == 100 and got["yellow_uncapped_permil"] == 115
    assert got["yellow_by_singer"] == [{"holomem": "A", "permil": 75}, {"holomem": "B", "permil": 40}]
    assert got["red_support_permil"] == 41 and got["red_power"] == 1234 and got["leader_sings"]
    assert row_detail.song_board_bonus(team, NS(board=Board()), None) is None


def test_the_engine_key_moves_when_a_refresh_moves_the_boards_or_the_wallet(tmp_path, monkeypatch):
    # the scoring key does not hash board_state.json (sockets, nodes) or inventory.json
    # (materials); a refresh that moved only those must still rebuild the engine
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    (dumps / "board_state.json").write_text('{"chr-00001": {"nodes": []}}')
    (dumps / "inventory.json").write_text('{"items": {}}')
    monkeypatch.setattr(ondemand, "_PROXY", tmp_path)
    monkeypatch.setattr(ondemand.tsb, "today_keys", lambda basis, difficulty: ({}, "scoring-key"))
    k0 = ondemand.account_key("EXPERT")
    assert k0 and ondemand.account_key("EXPERT") == k0                    # stable
    (dumps / "board_state.json").write_text('{"chr-00001": {"nodes": [], "slots": {"S-001": "card-1"}}}')
    k1 = ondemand.account_key("EXPERT")
    (dumps / "inventory.json").write_text('{"items": {"cube": 5}}')
    k2 = ondemand.account_key("EXPERT")
    assert len({k0, k1, k2}) == 3


def test_materials_plan_rereads_a_file_a_refresh_rewrote(tmp_path, monkeypatch):
    import os

    import materials_plan

    dumps = tmp_path / "dumps"
    dumps.mkdir()
    path = dumps / "board_state.json"
    path.write_text('{"a": 1}')
    monkeypatch.setattr(materials_plan, "_PROXY", tmp_path)
    monkeypatch.setattr(materials_plan, "_FILES", {})
    assert materials_plan._read("board_state.json") == {"a": 1}
    path.write_text('{"a": 2}')
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))   # a later write
    assert materials_plan._read("board_state.json") == {"a": 2}


def test_with_play_sets_both_halves_of_auto():
    # AUTO = the AUTO judgement column AND no combo, together (`server._auto_premise`'s
    # rule): the column alone would keep granting combo-gated overrides AUTO never earns
    import dataclasses
    from pathlib import Path

    from hbuilder.domain.scoring_config import load_scoring_config

    cfg = load_scoring_config(Path(__file__).resolve().parents[2] / "config.json")
    ctx = dataclasses.make_dataclass("Ctx", [("cfg", object)])(cfg)
    auto = ondemand.with_play(ctx, ("pc", True)).cfg
    assert (auto.platform, auto.song_auto, auto.run_state.combo) == ("pc", True, 0)
    manual = ondemand.with_play(ctx, ("mobile", False)).cfg
    assert (manual.platform, manual.song_auto, manual.run_state.combo) == ("mobile", False, cfg.run_state.combo)
    assert ondemand.parse_play({"platform": "PC", "auto": True}) == ("pc", True)
    assert ondemand.parse_play({}) == ("mobile", False)


def test_replan_takes_the_head_and_the_best_singer_led_teams_below_it():
    # the quick plan undervalues a singer's team (a singer's best can sit at the leaf's #190
    # and re-plan to #4): her best teams get the exact plan wherever they stand
    ranked = [("lui", k) for k in range(30)]
    ranked[27], ranked[29] = ("nene", 27), ("nene", 29)
    picked = ondemand.replan_pick(ranked, lambda r: r[0] == "nene", n=24, extra=4)
    assert picked == list(range(24)) + [27, 29]
    assert ondemand.replan_pick(ranked[:5], lambda r: False, n=24) == [0, 1, 2, 3, 4]


def test_a_replanned_team_is_remembered_under_its_own_name(monkeypatch):
    # the singers' plans are sent first, so remembering each answer by its POSITION in
    # the reordered list would hold a team's plan under another team's name, and opening
    # that row would show a board that is not its own
    import materials_plan

    class Card(SimpleNamespace):
        pass
    leaders = [Card(id=f"card-0000{k}-5-a-0000-00", member=f"m{k}") for k in range(1, 4)]
    leaders[2] = Card(id="card-00031-5-a-0000-00", member="Nene")         # the singer
    pool = [Card(id=f"card-0100{k}-5-a-0000-00", member=f"p{k}") for k in range(6)]
    ranking = [(3.0 - k / 10, li, (0, 1, 2, 3, 4)) for k, li in enumerate((0, 1, 2))]

    class FakeWorkers:
        _processes = 2

        def apply_async(self, _fn, args, callback, error_callback):
            k, leader_id, members, _song, _ci, _opts, _carried, _generation = args[0]
            callback((k, 1.0, None, None, ("plan of", leader_id, members)))
    held = []
    monkeypatch.setattr(materials_plan, "remember",
                        lambda leader_id, members, _song, _opts, entry: held.append(
                            ((leader_id, members), entry[1:])))
    engine = SimpleNamespace(
        assumptions=SimpleNamespace(plan_opts=lambda: {}),
        charts=[SimpleNamespace(singers=("chr-00031",))], index={"m0020": 0},
        pool=SimpleNamespace(_pool=FakeWorkers(), new_generation=lambda: 1,
                             stop_through=lambda _g: None))
    out, failed = ondemand.Engine._within_materials(engine, ranking, leaders, pool, "m0020", (0,), None)
    assert failed == [] and len(out) == 3
    assert held and all(team == entry for team, entry in held)
