"""`"auto": true` in a request body ranks for an AUTO run — BOTH halves of it.

On AUTO the combo never accumulates (seen in a live trace of the scorer), so a
`combo >= N` magnitude override never fires.
Setting only the AUTO judgement column would keep granting magnitudes the run
cannot produce and over-value exactly the cards that carry one. Half a premise
is a number for a run that does not exist, so the two are set together in one
place and this pins that they stay together.

    python -m pytest tools/web/test_auto_premise_route.py
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import server as srv
from hbuilder.domain.scoring_config import RunState

REPO = Path(__file__).resolve().parents[2]


class _Cfg:
    """Just enough of a `ScoringConfig` for the helper: it reads `run_state`."""

    run_state = RunState(life=1000, combo=100)


def test_no_auto_flag_leaves_the_premise_alone():
    assert srv._auto_premise(_Cfg(), {}) == {}
    assert srv._auto_premise(_Cfg(), {"auto": False}) == {}


def test_a_non_boolean_truthy_value_is_NOT_taken_as_auto():
    """`is True`, not truthiness: a stray `"auto": "false"` from a form post is
    a string and would otherwise silently switch the premise."""
    assert srv._auto_premise(_Cfg(), {"auto": "false"}) == {}
    assert srv._auto_premise(_Cfg(), {"auto": 1}) == {}


def test_auto_sets_both_halves_together():
    got = srv._auto_premise(_Cfg(), {"auto": True})
    assert got["song_auto"] is True
    assert got["run_state"].combo == 0, (
        "the combo half is the one that is easy to forget — without it the "
        "scorer keeps granting combo-gated overrides an AUTO run never sees"
    )
    assert got["run_state"].life == _Cfg.run_state.life, "only combo moves"


def test_the_song_pipeline_applies_it():
    """A structural check on the source: the pipeline builds its cfg in a
    subprocess-free path this test cannot drive without an account, so what is
    checked is that the helper is actually called where the cfg is assembled."""
    body = (REPO / "tools" / "web" / "server.py").read_text(encoding="utf-8")
    section = body.split("def develop_pipeline", 1)[1].split("\ndef ", 1)[0]
    assert "_auto_premise(cfg, body)" in section


def test_compare_carries_the_flag_through_its_assumptions():
    view = (REPO / "tools" / "web" / "compare_view.py").read_text(encoding="utf-8")
    assert '"auto": body.get("auto") is True' in view
    assert "srv._auto_premise(cfg" in view
