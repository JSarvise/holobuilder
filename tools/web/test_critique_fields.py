"""The backend fields docs/redesign/critique/A-data.md §8 asks for: the song
cache's GLOBAL `ranking[]` and per-leader `alternatives[]`, the run-score
quantiles on the first rows, `plan_cost` / `song_est_as_is` on a row detail,
and `eta_s` / `best_so_far` on a job's progress events. Synthetic inputs only;
no network, no account files."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
for extra in (HERE, HERE / "kernel", HERE.parents[1] / "src", HERE.parent / "recon" / "proxy"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import event_mode  # noqa: E402
import row_detail  # noqa: E402
import two_stage_view as view  # noqa: E402
from job_progress import JobProgress  # noqa: E402

tsb = view.build


# ---------------------------------------------------------------------------
# the global ranking + alternatives (two_stage_build)
# ---------------------------------------------------------------------------
def _card(i, member=None):
    return SimpleNamespace(id=f"card-{i}", member=member or f"Chr{i}", costume_name=f"C{i}")


def _found_rows():
    """Two leaders: A's 2nd and 3rd sets beat B's best."""
    a, b = _card(100, "LeadA"), _card(101, "LeadB")
    return [
        {"leader": a, "status": "searched", "best": 100.0, "members": (0, 1, 2, 3, 4),
         "ranking": [(100.0, (0, 1, 2, 3, 4)), (99.0, (0, 1, 2, 3, 5)), (98.0, (0, 1, 2, 3, 6)),
                     (10.0, (0, 1, 2, 3, 7))]},
        {"leader": b, "status": "triage", "best": 97.0, "members": (1, 2, 3, 4, 5),
         "ranking": [(97.0, (1, 2, 3, 4, 5)), (50.0, (2, 3, 4, 5, 6))]},
    ], a, b


def test_the_global_ranking_merges_every_leaders_sets_best_first():
    rows, a, b = _found_rows()
    reps = [_card(i) for i in range(8)]
    pool_ids = [c.id for c in reps]
    got = tsb.global_ranking(rows, pool_ids, reps, lambda c: c.member, [b], size=4)
    assert [(r["leader"], r["best"]) for r in got] == [
        ("card-100", 100.0), ("card-100", 99.0), ("card-100", 98.0), ("card-101", 97.0)]
    assert got[1]["members"] == ["card-0", "card-1", "card-2", "card-3", "card-5"]
    assert got[3]["sings"] is True and got[3]["status"] == "triage"
    assert got[0]["member_names"] == ["Chr0", "Chr1", "Chr2", "Chr3", "Chr4"]


def test_alternatives_are_the_next_sets_of_the_same_leader_without_its_own():
    rows, _a, _b = _found_rows()
    reps = [_card(i) for i in range(8)]
    alts = tsb.alternatives(rows[0], [c.id for c in reps], reps, count=2)
    assert [x["best"] for x in alts] == [99.0, 98.0]
    assert all(x["refined"] is False for x in alts)
    assert tsb.alternatives(rows[1], [c.id for c in reps], reps, count=5)[0]["best"] == 50.0


def test_refine_scores_a_team_once_across_the_ranking_and_the_rows(monkeypatch):
    import hbuilder.domain.team as team_mod

    monkeypatch.setattr(team_mod, "Team", lambda leader, members: (leader, members))
    calls = []

    class Pool:
        def batch_score(self, teams, chart_indices=None, refine_activation=False):
            calls.append(len(teams))
            return [1000.0 + k for k in range(len(teams))]

    by_id = {k: k for k in ("L", "a", "b", "c", "d", "e", "f")}
    ranking = [{"leader": "L", "members": list("abcde"), "best": 990.0},
               {"leader": "L", "members": list("abcdf"), "best": 980.0}]
    rows = [{"leader": "L", "members": list("edcba"), "best": 990.0}]
    known: dict = {}
    assert tsb._refine_rows(ranking, (0,), Pool(), by_id, known=known) == 2
    assert tsb._refine_rows(rows, (0,), Pool(), by_id, known=known) == 1
    assert calls == [2]                                # the row reused the ranking's score
    assert rows[0]["best"] == 1000.0 and rows[0]["best_plain"] == 990.0 and rows[0]["refined"]


def test_slot_orders_write_order_and_quantiles_and_develop_a_team_once(monkeypatch):
    import hbuilder.domain.team as team_mod

    monkeypatch.setattr(team_mod, "Team", lambda leader, members: (leader, members))
    seen = []

    def leaf_detail(built, team, *, refine, panel, chart, want_distribution):
        seen.append(team)
        assert want_distribution and panel is False
        return {"member_order": ["e", "d", "c", "b", "a"], "song_max": 120.0,
                "distribution": {"available": True, "p10": 90, "p50": 100, "p90": 110,
                                 "expected_total": 100.5, "verified": True}}

    monkeypatch.setattr(row_detail, "leaf_detail", leaf_detail)
    built = {"by_id": {k: k for k in ("L", "a", "b", "c", "d", "e")}}
    ranking = [{"leader": "L", "members": list("abcde"), "best": 100.5}]
    rows = [{"leader": "L", "members": list("abcde"), "best": 100.5}]
    known: dict = {}
    tsb._slot_orders(ranking, built, None, known=known)
    tsb._slot_orders(rows, built, None, known=known)
    assert len(seen) == 1
    for r in (ranking[0], rows[0]):
        assert r["member_order"] == ["e", "d", "c", "b", "a"]
        assert (r["p10"], r["p50"], r["p90"], r["song_max"]) == (90, 100, 110, 120.0)
        assert r["dist_verified"] is True and r["dist_mean"] == 100.5


def test_quantile_fields_are_empty_when_the_law_is_unavailable():
    assert row_detail.quantile_fields({"distribution": {"available": False}}) == {}
    assert row_detail.quantile_fields({}) == {}


def test_the_cache_version_moved_with_the_shape():
    assert tsb.VERSION == "two-stage-build-4"
    assert tsb.SETTINGS["ranking"]["size"] >= 10 and tsb.SETTINGS["alternatives"] >= 3


def test_search_song_rows_all_carry_a_ranking_led_by_their_own_set():
    pytest.importorskip("surrogate")
    if shutil.which("gcc") is None:
        pytest.skip("no gcc to build the screen")
    import test_two_stage as t2

    cards = t2._pool(24, seed=61)
    score, _sets, _calls = t2._scorer(cards, seed=62)
    leaders = t2._leaders(cards, seed=63)
    of = t2._leader_score(cards, score)
    base = SimpleNamespace(id="bare", member="Nobody", gate=None, flat=1.0, gated=1.0, sings=1.0)
    result = t2.two_stage.search_song(
        cards, leaders, lambda leader, sets: [of(leader, s) for s in sets], base_leader=base,
        gate_of=lambda l: l.gate, initial=800, step=300, triage_top=80, tier=30, max_searched=3)
    assert {r["status"] for r in result["rows"]} == {"searched", "triage"}
    for row in result["rows"]:
        ranking = row["ranking"]
        assert ranking and len(ranking) <= 100
        assert ranking[0] == (row["best"], row["members"])
        assert [v for v, _s in ranking] == sorted((v for v, _s in ranking), reverse=True)


# ---------------------------------------------------------------------------
# /api/two-stage serves them (two_stage_view)
# ---------------------------------------------------------------------------
def _row(i, **extra):
    return {"leader": f"card-{i}", "leader_name": f"L{i}", "status": "searched",
            "best": 100.0 - i, "members": [f"m{k}" for k in range(5)],
            "member_names": [f"N{k}" for k in range(5)], **extra}


def test_the_view_serves_the_global_ranking_alternatives_and_quantiles(tmp_path):
    ranking = [_row(0, p10=90, p50=99, p90=108, song_max=130, dist_mean=100.0,
                    dist_verified=True, refined=True, best_plain=99.5)] + \
              [_row(0, best=99.0 - i) for i in range(5)]
    rows = [_row(0, alternatives=[{"members": ["x"] * 5, "best": 99.0, "refined": False}]),
            _row(1)]
    tsb._write(tmp_path / "song_m0001.json", {"key": "k", "song": "m0001", "rows": rows,
                                              "ranking": ranking})
    keys = lambda: ({"m0001": "k"}, "g")  # noqa: E731
    got = view.payload("m0001", keys=keys, cache_dir=tmp_path)
    assert got["ranking_size"] == 6 and len(got["ranking"]) == 6
    assert [r["leader"] for r in got["ranking"]] == ["card-0"] * 6     # one leader may fill it
    top = got["ranking"][0]
    assert (top["p10"], top["p50"], top["p90"], top["song_max"]) == (90, 99, 108, 130)
    assert top["refined"] is True and top["best_plain"] == 99.5
    assert got["ranking"][1]["p10"] is None
    assert got["rows"][0]["alternatives"][0]["best"] == 99.0
    assert got["rows"][1]["alternatives"] == []
    assert len(view.payload("m0001", keys=keys, cache_dir=tmp_path, top=2)["ranking"]) == 2


def test_an_older_file_has_no_ranking(tmp_path):
    tsb._write(tmp_path / "song_m0001.json", {"key": "k", "song": "m0001", "rows": [_row(0)]})
    got = view.payload("m0001", keys=lambda: ({"m0001": "k"}, "g"), cache_dir=tmp_path)
    assert got["ranking"] is None and got["ranking_size"] is None
    assert got["rows"][0]["p10"] is None and got["rows"][0]["alternatives"] == []


# ---------------------------------------------------------------------------
# plan_cost (row_detail)
# ---------------------------------------------------------------------------
PRICES = {"G-001": {"points": 2, "green": 30}, "G-002": {"points": 3, "green": 40, "greenhex": 1},
          "R-001": {"points": 5, "red": 20}, "Y-001": {"points": 1, "yellow": 5}}


def test_plan_cost_counts_buys_resets_refunds_and_the_shortfall():
    planned = {"chr-1": {"nodes": ["S-001", "G-001", "G-002"]}, "chr-2": {"nodes": ["Y-001"]}}
    current = {"chr-1": ["S-001", "R-001", "G-001"], "chr-2": []}
    got = row_detail.plan_cost(planned, current, PRICES,
                               wallet={"green": 10, "yellow": 100}, hologold=5000,
                               unspent_points={"chr-1": 0, "chr-2": 0}, names={"chr-1": "A"})
    one, two = got["per_character"]
    assert one["buy"] == ["G-002"] and one["reset"] == ["R-001"] and one["name"] == "A"
    assert (one["points_needed"], one["points_refunded"], one["points_shortfall"]) == (3, 5, 0)
    assert two["buy"] == ["Y-001"] and two["points_shortfall"] == 1
    assert got["materials"] == {"green": 40, "greenhex": 1, "yellow": 5}
    assert got["refund"] == {"red": 20}
    assert got["shortfall"] == {"green": 30, "greenhex": 1}
    assert (got["resets"], got["hologold"], got["hologold_shortfall"]) == (1, 1000, 0)
    assert got["affordable"] is False and got["buys"] == 2 and got["unpriced"] == 0


def test_plan_cost_never_reads_an_unmeasured_wallet_as_zero():
    got = row_detail.plan_cost({"chr-1": {"nodes": ["G-001", "X-404"]}}, {}, PRICES)
    assert got["wallet"] is None and got["shortfall"] is None and got["affordable"] is None
    assert got["hologold_wallet"] is None and got["hologold_shortfall"] is None
    assert got["per_character"][0]["points_shortfall"] is None and got["unpriced"] == 1


def test_an_affordable_plan_says_so():
    got = row_detail.plan_cost({"chr-1": {"nodes": ["G-001"]}}, {"chr-1": []}, PRICES,
                               wallet={"green": 30}, hologold=0, unspent_points={"chr-1": 2})
    assert got["shortfall"] == {} and got["affordable"] is True


def test_as_is_reports_an_unreadable_capture_instead_of_guessing(monkeypatch, tmp_path):
    monkeypatch.setattr(row_detail, "CAPTURED_BOARD", tmp_path / "missing.json")
    got = row_detail._as_is(None, None, None, None, None)
    assert got["song_est_as_is"] is None
    assert got["as_is"]["available"] is False and got["plan_cost"]["available"] is False


# ---------------------------------------------------------------------------
# eta_s / best_so_far (job_progress) and the event rows
# ---------------------------------------------------------------------------
class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_eta_comes_from_the_rate_and_the_best_so_far_is_kept():
    out, clock = [], Clock()
    jp = JobProgress(out.append, clock=clock)
    jp({"type": "stage", "stage": "develop", "state": "active", "total": 4})
    clock.t = 2.0
    jp({"type": "progress", "stage": "develop", "done": 1, "total": 4, "team": list("Labcde"),
        "optimised_us": 50, "song_est": 2_000_000})
    clock.t = 4.0
    jp({"type": "progress", "stage": "develop", "done": 2, "total": 4, "team": list("Mabcde"),
        "song_est": 1_900_000})
    first, second = out[1], out[2]
    assert first["eta_s"] == 6.0 and first["eta_basis"] == "rate"
    assert second["eta_s"] == 4.0 and second["elapsed_s"] == 4.0
    assert second["best_so_far"]["cards"] == list("Labcde")
    assert second["best_so_far"]["value"] == 2_000_000 and second["best_so_far"]["metric"] == "song_est"
    assert second["best_so_far"]["provisional"] is True
    assert "eta_s" not in out[0]                     # only progress events are enriched


def test_eta_is_null_without_a_measure_and_uses_a_projection_when_given():
    out, clock = [], Clock()
    jp = JobProgress(out.append, clock=clock)
    clock.t = 1.0
    jp({"type": "progress", "stage": "score_search", "evaluated": 500})
    assert out[-1]["eta_s"] is None and out[-1]["eta_basis"] is None
    assert out[-1]["best_so_far"] is None
    jp.note_projection("search", seconds=10.0)
    jp({"type": "progress", "stage": "search", "evaluated": 10})
    clock.t = 4.0
    jp({"type": "progress", "stage": "search", "evaluated": 20})
    assert out[-1]["eta_s"] == 7.0 and out[-1]["eta_basis"] == "projection"
    jp.note_projection("search", total=100)
    clock.t = 5.0
    jp({"type": "progress", "stage": "search", "evaluated": 25})
    assert out[-1]["eta_basis"] == "rate" and out[-1]["total"] == 100
    assert out[-1]["eta_s"] == 12.0
    jp({"type": "progress", "stage": "search", "evaluated": 100, "complete": True})
    assert out[-1]["eta_s"] == 0.0


def test_a_ready_best_so_far_is_carried_and_a_new_metric_replaces_the_old():
    out = []
    jp = JobProgress(out.append, clock=Clock())
    jp({"type": "progress", "stage": "score_search", "evaluated": 1,
        "best_so_far": {"cards": ["a"], "value": 5.0, "metric": "score"}})
    jp({"type": "progress", "stage": "score_search", "evaluated": 2,
        "best_so_far": {"cards": ["b"], "value": 4.0, "metric": "score"}})
    assert out[-1]["best_so_far"]["cards"] == ["a"]
    jp({"type": "progress", "stage": "develop", "done": 1, "total": 2, "team": ["c"],
        "optimised_us": 3})
    assert out[-1]["best_so_far"]["cards"] == ["c"] and out[-1]["best_so_far"]["metric"] == "unit_score"


def test_event_rows_take_the_quantiles_when_asked():
    law = {"available": True, "p10": 1, "p50": 2, "p90": 3, "expected_total": 2.0, "verified": True}
    row = {"score": 2}

    def fn(leader, members):
        return {"song_est": 2.0, "song_max": 4.0, "distribution": law}

    event_mode.add_row_detail(row, {}, "L", [], detail_fn=fn)
    assert (row["p10"], row["p50"], row["p90"], row["dist_verified"]) == (1, 2, 3, True)
    plain = {"score": 2}
    event_mode.add_row_detail(plain, {}, "L", [], detail_fn=lambda l, m: {"song_est": 2.0})
    assert "p10" not in plain
