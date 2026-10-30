"""`refresh_all`'s staleness probe must notice a card whose LEVEL moved, not
only a card that is missing.

Comparing only the SET of card ids in `state/collection.json` against the set
the account reports is not enough: a card can be in both sets while sitting at
level **1** in the collection and level **70** on the account. The probe would
then be silent, and the solver would score that card far below its real total —
a card that simply could not be recommended, with nothing in any output to say
so.

`account_snapshot_*.json` carries each card's `level`, `bloom` and
`limit_break` verbatim, so this needs no level curve and no derivation: it is a
direct comparison of the two numbers that are supposed to be the same one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_PROXY = Path(__file__).resolve().parents[1] / "tools" / "recon" / "proxy"
sys.path.insert(0, str(_PROXY))

import refresh_all  # noqa: E402


def _write(tmp_path, collection_cards, snapshot_cards):
    collection = tmp_path / "collection.json"
    snapshot = tmp_path / "snapshot.json"
    collection.write_text(json.dumps({"cards": collection_cards}), encoding="utf-8")
    snapshot.write_text(json.dumps({"cards": snapshot_cards}), encoding="utf-8")
    return collection, snapshot


def test_a_card_whose_level_moved_is_reported(tmp_path):
    """The Luna case, exactly."""
    collection, snapshot = _write(
        tmp_path,
        {"card-00028-5-uniq-0023-00": {"level": 1, "bloom": 0, "limit_break": 0}},
        [{"card_id": "card-00028-5-uniq-0023-00", "level": 70, "limit_break": 0}],
    )
    drift = refresh_all.collection_level_drift(collection, snapshot)
    assert len(drift) == 1
    card_id, field, mine, live = drift[0]
    assert card_id == "card-00028-5-uniq-0023-00"
    assert (field, mine, live) == ("level", 1, 70)


def test_bloom_and_limit_break_drift_too(tmp_path):
    collection, snapshot = _write(
        tmp_path,
        {"c1": {"level": 70, "bloom": 0, "limit_break": 1}},
        [{"card_id": "c1", "level": 70, "bloom": 3, "limit_break": 4}],
    )
    fields = {row[1] for row in refresh_all.collection_level_drift(collection, snapshot)}
    assert fields == {"bloom", "limit_break"}


def test_an_agreeing_collection_reports_nothing(tmp_path):
    collection, snapshot = _write(
        tmp_path,
        {"c1": {"level": 70, "bloom": 3, "limit_break": 4}},
        [{"card_id": "c1", "level": 70, "bloom": 3, "limit_break": 4}],
    )
    assert refresh_all.collection_level_drift(collection, snapshot) == []


def test_a_field_the_snapshot_omits_is_not_drift(tmp_path):
    """The snapshot omits `bloom` for a card with none and `limit_break` for a
    card with none. Absent is not evidence of zero, and reporting it as drift
    would make the probe cry wolf on most of the collection — which is how a
    real warning gets ignored."""
    collection, snapshot = _write(
        tmp_path,
        {"c1": {"level": 70, "bloom": 0, "limit_break": 0}},
        [{"card_id": "c1", "level": 70}],
    )
    assert refresh_all.collection_level_drift(collection, snapshot) == []


def test_a_card_only_one_side_knows_is_left_to_the_id_probe(tmp_path):
    """Missing cards are the OTHER half of the report and already covered; this
    one must not double-report them."""
    collection, snapshot = _write(
        tmp_path,
        {"c1": {"level": 70}},
        [{"card_id": "c1", "level": 70}, {"card_id": "c2", "level": 5}],
    )
    assert refresh_all.collection_level_drift(collection, snapshot) == []


def test_a_missing_file_is_no_drift_rather_than_a_crash(tmp_path):
    """The probe runs before the account step on a fresh checkout."""
    assert refresh_all.collection_level_drift(
        tmp_path / "nope.json", tmp_path / "also-nope.json"
    ) == []


# --------------------------------------------------------------------------
# Derived artefacts older than their sources
# --------------------------------------------------------------------------
#
# `account_snapshot.py` refreshes `dumps/board_state.json` and prints "now run
# build_board.py"; if nobody does, `dumps/board.json` goes on describing the
# previous board, and the solver scores cards without their current board terms
# (e.g. a cooldown shorten). No counter notices, because every counter is about
# the file that HAD moved — so a derived file older than its source is reported.


def test_a_derived_file_older_than_its_source_is_reported(tmp_path, monkeypatch):
    import time

    dumps = tmp_path / "dumps"
    dumps.mkdir()
    (dumps / "board.json").write_text("{}", encoding="utf-8")
    time.sleep(0.01)
    src = dumps / "board_state.json"
    src.write_text("{}", encoding="utf-8")
    # two hours ahead of the derived file
    stamp = (dumps / "board.json").stat().st_mtime + 7200
    import os

    os.utime(src, (stamp, stamp))

    monkeypatch.setattr(refresh_all, "DUMPS", dumps)
    drift = refresh_all.derived_artefact_drift()
    assert [(d, s) for d, s, _h in drift] == [("board.json", "board_state.json")]
    assert 1.9 < drift[0][2] < 2.1


def test_a_derived_file_newer_than_its_source_is_silent(tmp_path, monkeypatch):
    import os
    import time

    dumps = tmp_path / "dumps"
    dumps.mkdir()
    (dumps / "board_state.json").write_text("{}", encoding="utf-8")
    time.sleep(0.01)
    derived = dumps / "board.json"
    derived.write_text("{}", encoding="utf-8")
    stamp = (dumps / "board_state.json").stat().st_mtime + 600
    os.utime(derived, (stamp, stamp))

    monkeypatch.setattr(refresh_all, "DUMPS", dumps)
    assert refresh_all.derived_artefact_drift() == []


def test_one_chain_run_of_slack_is_not_drift(tmp_path, monkeypatch):
    """A single chain run writes the source and the derived file seconds apart,
    in that order. Flagging that would fire on every healthy refresh, and a
    warning that always fires is one nobody reads."""
    import os

    dumps = tmp_path / "dumps"
    dumps.mkdir()
    (dumps / "board.json").write_text("{}", encoding="utf-8")
    src = dumps / "board_state.json"
    src.write_text("{}", encoding="utf-8")
    stamp = (dumps / "board.json").stat().st_mtime + 30
    os.utime(src, (stamp, stamp))

    monkeypatch.setattr(refresh_all, "DUMPS", dumps)
    assert refresh_all.derived_artefact_drift() == []
