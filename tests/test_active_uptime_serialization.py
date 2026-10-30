"""The ACTIVE-uptime model must appear where the config is SERIALISED, not only
where it is scored.

This covers what `test_active_uptime_model.py` does not: `_scoring_config_document`
(the doc `config_hash` compares against a benchmark) and `format_assumptions` (the
per-run header) must both include `active_uptime`. Otherwise a finite run and a
steady run of the same pool would carry the SAME config hash, and the assumptions
header would not tell the reader which uptime model produced the numbers.
"""

from __future__ import annotations

import dataclasses

from hbuilder.cli import _scoring_config_document
from hbuilder.output.assumptions import format_assumptions
from hbuilder.solver.estimate import config_hash

from conftest import make_scoring_config


def test_scoring_config_document_carries_the_active_uptime_model():
    cfg = make_scoring_config()
    doc = _scoring_config_document(cfg)
    assert doc["model"]["active_uptime"] == cfg.active_uptime_model


def test_config_hash_distinguishes_finite_from_steady():
    """The whole point: a benchmark measured under one model must not silently be
    reused for the other, because they report different Unit Scores."""
    finite = dataclasses.replace(make_scoring_config(), active_uptime_model="finite")
    steady = dataclasses.replace(make_scoring_config(), active_uptime_model="steady")
    assert config_hash(_scoring_config_document(finite)) != config_hash(
        _scoring_config_document(steady)
    )


def test_format_assumptions_reports_the_active_uptime_model():
    for model in ("finite", "steady"):
        cfg = dataclasses.replace(make_scoring_config(), active_uptime_model=model)
        lines = format_assumptions(cfg)
        assert any(line.startswith(f"MODEL.ACTIVE_UPTIME: {model}") for line in lines), (
            f"no MODEL.ACTIVE_UPTIME line for {model!r}: {lines}"
        )
