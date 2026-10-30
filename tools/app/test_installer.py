"""installer: what an update keeps and where a run works. No window, no network, no registry."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import installer as ins  # noqa: E402


def test_an_update_keeps_this_pcs_data_and_nothing_else(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    for rel in ("app/state/app_settings.json", "app/tools/recon/proxy/dumps/charts.json",
                "app/data/owned.json", "app/data/gacha/pulls.jsonl", "app/tools/app/launcher.py"):
        (old / rel).parent.mkdir(parents=True, exist_ok=True)
        (old / rel).write_text("x")
    moves = {src.relative_to(old).as_posix(): dst for src, dst in ins.preserve_moves(old, new)}
    assert set(moves) == {"app/state", "app/tools/recon/proxy/dumps", "app/data/owned.json",
                          "app/data/gacha"}                      # the code is never carried over
    assert moves["app/state"] == new / "app" / "state"


def test_the_folder_is_never_guessed_when_one_is_given(tmp_path, monkeypatch):
    # an update with the registry entry missing must not fall back to the default folder
    assert ins.install_dir(str(tmp_path)) == tmp_path
    monkeypatch.setattr(ins, "registered_dir", lambda: None)
    assert ins.install_dir(None) == ins.DEFAULT_DIR              # not frozen, nothing registered


def test_powershell_quoting_survives_apostrophes():
    assert ins.ps_quote("C:\\Users\\O'Brien\\x") == "'C:\\Users\\O''Brien\\x'"


def test_an_installer_older_than_setup_min_points_at_the_new_one(monkeypatch):
    """The signed version.json's setup_min stops a kept old exe from
    installing; equal or newer, or no setup_min at all (servers before it), installs."""
    monkeypatch.setattr(ins, "SETUP_VERSION", "1.0.0")
    assert ins.setup_outdated({}) is None
    assert ins.setup_outdated({"setup_min": "1.0.0"}) is None
    msg = ins.setup_outdated({"setup_min": "1.1.0"})
    assert msg and "out of date" in msg and ins.SETUP_NAME in msg
