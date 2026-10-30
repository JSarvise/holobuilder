"""`owned.json` resolution and data completeness reporting (SCHM-05,
SCHM-06). Covers `load_cards`'s duplicate-id/empty-array behavior,
`load_owned`'s duplicate/unknown-id rejection and order-independent
resolution, and `completeness_report`'s explicit-unknown-vs-explicit-zero
distinction against the shipped fixture (now at tests/fixtures/)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hbuilder.domain.loader import completeness_report, load_cards, load_owned
from hbuilder.domain.schema import SCHEMA_VERSION, SchemaError

from conftest import FIXTURE_CARDS_PATH, FIXTURE_OWNED_PATH

CARDS_PATH = FIXTURE_CARDS_PATH
OWNED_PATH = FIXTURE_OWNED_PATH
REPO_ROOT = Path(__file__).resolve().parent.parent


def _minimal_card(card_id: str) -> dict:
    """A minimal cards.json record satisfying every required field, with no
    skills — just enough to pass `validate_card`."""
    return {
        "id": card_id,
        "member": "Test Member",
        "generation": ["5期生"],
        "type": "Happy",
        "rarity": "SSR",
        "costume_name": "Test Costume",
        "stats": {"performance": 1, "technique": 1, "sense": 1},
        "skills": {"SPECIAL": None, "ACTIVE": None, "PASSIVE": None},
        "costume_skill": None,
    }


def _write_json(path: Path, doc: dict) -> Path:
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def cards():
    return load_cards(CARDS_PATH)


# --- load_cards: duplicate ids and empty collections -----------------------


def test_duplicate_card_id_raises_schema_error_naming_id(tmp_path):
    doc = {
        "schema_version": SCHEMA_VERSION,
        "cards": [_minimal_card("dup_01"), _minimal_card("dup_01")],
    }
    path = _write_json(tmp_path / "cards.json", doc)
    with pytest.raises(SchemaError) as excinfo:
        load_cards(path)
    assert "dup_01" in str(excinfo.value)


def test_empty_cards_array_returns_empty_tuple_without_raising(tmp_path):
    doc = {"schema_version": SCHEMA_VERSION, "cards": []}
    path = _write_json(tmp_path / "cards.json", doc)
    assert load_cards(path) == ()


def test_completeness_report_over_empty_collection_returns_empty_tuple(tmp_path):
    doc = {"schema_version": SCHEMA_VERSION, "cards": []}
    path = _write_json(tmp_path / "cards.json", doc)
    assert completeness_report(load_cards(path)) == ()


# --- load_owned: duplicates, unknown ids, empties, order independence ------


def test_duplicate_owned_id_raises_schema_error_naming_id(tmp_path, cards):
    doc = {"schema_version": SCHEMA_VERSION, "owned": ["nene_01", "nene_01"]}
    path = _write_json(tmp_path / "owned.json", doc)
    with pytest.raises(SchemaError) as excinfo:
        load_owned(path, cards)
    assert "nene_01" in str(excinfo.value)


def test_unknown_owned_id_raises_schema_error_naming_id(tmp_path, cards):
    doc = {"schema_version": SCHEMA_VERSION, "owned": ["does_not_exist"]}
    path = _write_json(tmp_path / "owned.json", doc)
    with pytest.raises(SchemaError) as excinfo:
        load_owned(path, cards)
    assert "does_not_exist" in str(excinfo.value)


def test_empty_owned_array_returns_empty_tuple_without_raising(tmp_path, cards):
    doc = {"schema_version": SCHEMA_VERSION, "owned": []}
    path = _write_json(tmp_path / "owned.json", doc)
    assert load_owned(path, cards) == ()


def test_load_owned_returns_cards_json_order_regardless_of_owned_json_order(
    tmp_path, cards
):
    ids = [card.id for card in cards]

    forward_doc = {"schema_version": SCHEMA_VERSION, "owned": ids}
    forward_path = _write_json(tmp_path / "owned_forward.json", forward_doc)

    reverse_doc = {"schema_version": SCHEMA_VERSION, "owned": list(reversed(ids))}
    reverse_path = _write_json(tmp_path / "owned_reverse.json", reverse_doc)

    forward_result = load_owned(forward_path, cards)
    reverse_result = load_owned(reverse_path, cards)

    assert forward_result == reverse_result
    assert [card.id for card in forward_result] == ids


# --- completeness_report against the shipped fixture ------------------------


def test_completeness_report_names_only_mio_01_with_active_magnitude(cards):
    report = completeness_report(cards)
    by_id = {entry.card_id: entry for entry in report}
    assert set(by_id) == {"mio_01"}
    assert by_id["mio_01"].unknown_fields == ("skills.ACTIVE.magnitude",)


def test_completeness_report_omits_lamy_01_whose_only_zero_is_explicit(cards):
    report = completeness_report(cards)
    ids = {entry.card_id for entry in report}
    assert "lamy_01" not in ids


def test_completeness_report_omits_cards_with_no_unknown_fields(cards):
    report = completeness_report(cards)
    covered_ids = {entry.card_id for entry in report}
    for card in cards:
        if card.id not in covered_ids:
            assert card.unknown_fields() == ()


# --- CLI: OWNED and COMPLETENESS lines --------------------------------------


def test_cli_prints_owned_and_completeness_lines():
    # D-09: the standalone gate-report script folded into `hbuilder
    # gates`; this now runs through hbuilder.cli's `gates` subcommand
    # instead of a standalone module.
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hbuilder.cli",
            "gates",
            "--cards",
            str(CARDS_PATH),
            "--owned",
            str(OWNED_PATH),
            "--leader",
            "nene_01",
            "--members",
            "fubuki_01,mio_01,matsuri_01,polka_01,lamy_01",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "OWNED: 6/6" in result.stdout
    assert "COMPLETENESS: 5/6" in result.stdout
    assert "mio_01.skills.ACTIVE.magnitude" in result.stdout
    assert "lamy_01" not in result.stdout.split("COMPLETENESS:")[1]


def test_cli_reports_missing_owned_file_on_stderr_with_exit_code_3(tmp_path):
    # D-24b: this failure class is reassigned from exit 2 to exit 3
    # (EXIT_INPUT_DATA) as part of folding the standalone gate-report
    # script into `hbuilder gates` behind the shared load-and-guard — 2
    # is now reserved for argparse's own usage errors alone.
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    missing_owned = tmp_path / "does_not_exist.json"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hbuilder.cli",
            "gates",
            "--cards",
            str(CARDS_PATH),
            "--owned",
            str(missing_owned),
            "--leader",
            "nene_01",
            "--members",
            "fubuki_01,mio_01,matsuri_01,polka_01,lamy_01",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )

    assert result.returncode == 3
    assert result.stdout == ""
    assert str(missing_owned) in result.stderr
