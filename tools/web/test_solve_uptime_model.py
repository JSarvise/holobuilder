"""`POST /solve` `uptime_model` — the ACTIVE-uptime toggle reaches the scorer.

The web's "Score model" control mirrors the CLI's
`--uptime-model`: the browser sends `uptime_model: steady|finite`, `run_solve_job`
applies it via `dataclasses.replace(cfg, active_uptime_model=...)`, and anything
else inherits config.json. These drive `run_solve_job` directly (as
test_solve_constraints.py does) over a pool whose ACTIVE has a DEAD ZONE
(duration 5 < interval 30), so the finite window — which counts the dead time —
scores strictly below the steady ratio, proving the toggle changed the model.
"""
from __future__ import annotations

import queue
import sys
from pathlib import Path

import server as web_server
from hbuilder.domain.card import Stats

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from conftest import make_card, make_scoring_config, make_skill  # noqa: E402


def _pool(size: int = 6):
    # A DEAD-ZONE active on every card: duration 5 well below interval 30, so
    # finite (window count) < steady (min(1, duration/interval)).
    return tuple(
        make_card(
            f"card_{index}",
            member=f"Holomem{index}",
            stats=Stats(
                performance=100 + 13 * index,
                technique=90 + 7 * index,
                sense=80 + 3 * index,
            ),
            active=make_skill(
                "ACTIVE", family="conditional_score_up", effect_type="score",
                duration=5.0, interval=30.0, probability_tier="High", magnitude=60.0,
            ),
        )
        for index in range(size)
    )


def _top_score(monkeypatch, tmp_path, uptime_model):
    pool = _pool()
    config = make_scoring_config()  # default active_uptime_model == "finite"
    monkeypatch.setattr(
        web_server, "load_inputs", lambda *a, **k: (pool, pool, config, None)
    )
    monkeypatch.setattr(web_server, "BOARD_DISABLED", True)
    monkeypatch.setattr(
        web_server, "COLLECTION_PATH", tmp_path / "no-such-collection.json"
    )
    job = web_server.Job(job_id=f"test-uptime-{uptime_model}")
    web_server.run_solve_job(
        job, top_n=3, art_dir=Path("does-not-exist"), song="",
        uptime_model=uptime_model,
    )
    events = []
    while True:
        try:
            events.append(job.events.get_nowait())
        except queue.Empty:
            break
    results = [e for e in events if e.get("type") == "result"]
    assert results, f"no result event; got {[e.get('type') for e in events]}"
    return results[-1]["payload"]["teams"][0]["score"]


def test_steady_scores_above_finite_through_the_web_toggle(monkeypatch, tmp_path):
    steady = _top_score(monkeypatch, tmp_path, "steady")
    finite = _top_score(monkeypatch, tmp_path, "finite")
    assert steady > finite, (
        f"steady {steady} should exceed finite {finite} on a dead-zone active — "
        "the web toggle did not reach the scorer"
    )


def test_absent_or_unknown_uptime_model_inherits_the_config_default(monkeypatch, tmp_path):
    """None (or an unknown value the server maps to None) must not override the
    config; make_scoring_config's default is finite, so it matches an explicit
    finite run."""
    inherited = _top_score(monkeypatch, tmp_path, None)
    finite = _top_score(monkeypatch, tmp_path, "finite")
    assert inherited == finite
