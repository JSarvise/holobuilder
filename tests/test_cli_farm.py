"""The `farm` subcommand — Marathon event-point farming ranking.

Driven against the real CLI as a subprocess, matching the harness shape of the other
tests/test_cli_*.py files (copied env with PYTHONPATH, capture_output, utf-8).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import FIXTURE_CARDS_PATH, FIXTURE_OWNED_PATH

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(args: list[str], env_extra: dict | None = None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "hbuilder.cli", *args],
        capture_output=True, encoding="utf-8", env=env, cwd=REPO_ROOT,
    )


def _marathon(tmp_path: Path, channels) -> Path:
    p = tmp_path / "marathon.json"
    p.write_text(json.dumps({"name": "test-event", "channels": channels}), encoding="utf-8")
    return p


def test_farm_ranks_owned_cards_by_matching_bonus_rows(tmp_path):
    # nene_01 is an owned fixture card; a card_id row gives it +30%.
    marathon = _marathon(tmp_path, {"live": [{"permil": 300, "card_id": "nene_01"}]})
    r = _run([
        "farm",
        "--cards", str(FIXTURE_CARDS_PATH),
        "--owned", str(FIXTURE_OWNED_PATH),
        "--marathon", str(marathon),
        "--no-collection",
    ])
    assert r.returncode == 0, r.stderr
    assert "MARATHON FARMING deck" in r.stdout
    # The matching card is listed with its +30.0% bonus and leads the deck total.
    assert "nene_01" in r.stdout
    assert "+ 30.0%" in r.stdout
    assert "deck total +30.0%" in r.stdout


def test_farm_flat_when_no_card_matches(tmp_path):
    marathon = _marathon(tmp_path, {"live": [{"permil": 300, "card_id": "not_owned_card"}]})
    r = _run([
        "farm",
        "--cards", str(FIXTURE_CARDS_PATH),
        "--owned", str(FIXTURE_OWNED_PATH),
        "--marathon", str(marathon),
        "--no-collection",
    ])
    assert r.returncode == 0, r.stderr
    assert "farming EV is flat here" in r.stdout
    assert "deck total +0.0%" in r.stdout


def test_farm_recommended_doubles_the_points_scalar(tmp_path):
    marathon = _marathon(tmp_path, {"live": [{"permil": 300, "card_id": "nene_01"}]})
    base = _run(["farm", "--cards", str(FIXTURE_CARDS_PATH), "--owned", str(FIXTURE_OWNED_PATH),
                 "--marathon", str(marathon), "--no-collection", "--stamina", "3"])
    boosted = _run(["farm", "--cards", str(FIXTURE_CARDS_PATH), "--owned", str(FIXTURE_OWNED_PATH),
                    "--marathon", str(marathon), "--no-collection", "--stamina", "3",
                    "--recommended"])
    assert "x24 " in base.stdout       # stamina 3 -> provide 24, no recommended
    assert "x48 " in boosted.stdout    # x2.0 recommended
    assert "recommended-deck x2.0" in boosted.stdout


def test_farm_bad_marathon_file_is_input_error(tmp_path):
    bad = tmp_path / "marathon.json"
    bad.write_text(json.dumps({"name": "e", "channels": {"live": [{"permil": 1}]}}),
                   encoding="utf-8")  # row identifies no card
    r = _run(["farm", "--cards", str(FIXTURE_CARDS_PATH), "--owned", str(FIXTURE_OWNED_PATH),
              "--marathon", str(bad), "--no-collection"])
    assert r.returncode == 3, (r.returncode, r.stderr)
