"""The app's account-only refresh on a PC without the game.
No network: the gRPC call is a fake."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "recon" / "proxy"))
import account_snapshot as A  # noqa: E402
import game_paths  # noqa: E402
import refresh_all as R  # noqa: E402

import pytest  # noqa: E402


def test_account_only_runs_the_account_steps_without_the_event_call():
    assert set(R.ACCOUNT_STEPS) <= set(R.STEP_NAMES)
    ns = argparse.Namespace(python="py", account="main", merge_collection=True, account_only=True)
    cmd, _, _ = R.cmd_account(ns)
    assert "--refresh-events" not in cmd and "--merge-collection" in cmd
    ns.account_only = False
    assert "--refresh-events" in R.cmd_account(ns)[0]       # the publisher's refresh keeps it


def test_the_app_refresh_reads_its_own_event_rank_and_only_its_own_region():
    assert "event_cutoffs" in R.ACCOUNT_STEPS
    ns = argparse.Namespace(python="py", account_only=True)
    cmd = R.cmd_event_cutoffs(ns)[0]
    assert cmd[-4:] == ["--self", "--songs", "--region", "own"]
    ns.account_only = False
    assert "--region" not in R.cmd_event_cutoffs(ns)[0]       # the publisher reads every region


def test_no_game_means_no_install_lookup(monkeypatch):
    monkeypatch.setenv("HB_NO_GAME", "1")
    with pytest.raises(SystemExit, match="HB_NO_GAME"):
        game_paths.game_root()


def test_app_version_falls_back_to_the_published_file(tmp_path, monkeypatch):
    monkeypatch.delenv("HOLODREAMS_APP_VERSION", raising=False)
    monkeypatch.setenv("HB_NO_GAME", "1")
    monkeypatch.setattr(A, "_app_version_cache", [])
    monkeypatch.setattr(A, "CLIENT_VERSION_FILE", tmp_path / "client_version.json")
    assert A.app_version() is None
    monkeypatch.setattr(A, "_app_version_cache", [])
    (tmp_path / "client_version.json").write_text(json.dumps({"version": "1.2.1"}))
    assert A.app_version() == "1.2.1"
    monkeypatch.setattr(A, "_app_version_cache", [])
    (tmp_path / "client_version.json").write_text(json.dumps({"version": "not a version"}))
    assert A.app_version() is None


def test_outdated_app_tries_the_base_version_before_the_next_ones(tmp_path, monkeypatch):
    """The server answers profile 1.1.1 -> OUTDATED_APP, 1.2.1 accepted, 1.2.2 -> INVALID."""
    monkeypatch.delenv("HOLODREAMS_APP_VERSION", raising=False)
    monkeypatch.setattr(A, "_app_version_cache", [None])          # no install, nothing published
    monkeypatch.setattr(A, "CLIENT_VERSION_FILE", tmp_path / "client_version.json")
    tried = []

    def call(host, path, body, ident):
        v = A.os.environ.get("HOLODREAMS_APP_VERSION") or dict(ident)["x-app-version"]
        tried.append(v)
        if v == "1.2.1":
            return {}, {"grpc-status": "0"}, b""
        msg = "OUTDATED_APP" if v < "1.2.1" else "INVALID_ARGUMENT"
        return {}, {"grpc-status": "3", "grpc-message": msg}, b""
    monkeypatch.setattr(A, "call", call)
    monkeypatch.setattr(A, "unframe", lambda data: [])
    ident = [("x-app-version", "1.1.1")]
    try:
        A.fetch_master_version("us.example", ident)
    except Exception:                                             # noqa: BLE001 -- empty body
        pass
    assert tried[:2] == ["1.1.1", "1.2.1"]
    assert json.loads((tmp_path / "client_version.json").read_text())["version"] == "1.2.1"
    A.os.environ.pop("HOLODREAMS_APP_VERSION", None)


def test_a_new_collection_derives_the_member_power_up(tmp_path, monkeypatch):
    """A first install has no power_up_permyriad, and without one there is no Member
    Power-Up Bonus (Unit Score 1,773,326 vs the game's 1,839,420). Absent -> "auto"; a
    value already there is kept."""
    monkeypatch.setattr(A, "STATE", tmp_path)
    monkeypatch.setattr(A, "DUMPS", tmp_path / "dumps")
    snap = {"cards": [{"card_id": "card-00001-5-uniq-0001-00", "level": 80, "bloom": 1}], "posters": [1, 2]}
    A.merge_collection(snap)
    doc = json.loads((tmp_path / "collection.json").read_text("utf8"))
    assert doc["player"] == {"posters": 2, "power_up_permyriad": "auto"}
    doc["player"]["power_up_permyriad"] = 39
    (tmp_path / "collection.json").write_text(json.dumps(doc), "utf8")
    A.merge_collection(snap)
    assert json.loads((tmp_path / "collection.json").read_text("utf8"))["player"]["power_up_permyriad"] == 39


def test_the_refresh_clears_the_cards_tried_as_owned(tmp_path, monkeypatch):
    """A card tried as owned in Collection ends at the next refresh."""
    monkeypatch.setattr(A, "STATE", tmp_path)
    monkeypatch.setattr(A, "DUMPS", tmp_path / "dumps")
    (tmp_path / "collection.json").write_text(json.dumps({"cards": {}, "trial": ["card-x"]}), "utf8")
    A.merge_collection({"cards": [{"card_id": "card-00001-5-uniq-0001-00", "level": 80, "bloom": 1}]})
    assert "trial" not in json.loads((tmp_path / "collection.json").read_text("utf8"))
