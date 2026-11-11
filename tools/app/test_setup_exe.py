"""setup_exe: the installer copy next to app/ follows each release's own (version.json's signed
`setup_sha256`), so an update never runs the first install's installer forever."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import setup_exe  # noqa: E402

NEW = b"MZ new installer"


def _meta(data: bytes = NEW) -> dict:
    return {"setup_url": "app/holobuilder-setup.exe", "setup_sha256": hashlib.sha256(data).hexdigest()}


def test_an_older_copy_is_replaced_by_the_releases_installer(tmp_path):
    target = tmp_path / setup_exe.NAME
    target.write_bytes(b"MZ first install")
    asked: list[str] = []
    got = setup_exe.bring_current(tmp_path, _meta(), lambda route: asked.append(route) or NEW)
    assert got == "replaced" and target.read_bytes() == NEW and asked == ["app/holobuilder-setup.exe"]
    # the old copy (the running exe, during an update) was moved aside, then cleared by a later run
    aside = list(tmp_path.glob(f".{setup_exe.NAME}.*.old"))
    assert len(aside) == 1 and aside[0].read_bytes() == b"MZ first install"
    setup_exe.remove_aside(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [setup_exe.NAME]


def test_a_current_copy_is_not_downloaded_again(tmp_path):
    (tmp_path / setup_exe.NAME).write_bytes(NEW)
    assert setup_exe.bring_current(tmp_path, _meta(), lambda route: pytest.fail("downloaded")) == "current"


def test_a_damaged_download_keeps_the_copy_in_place(tmp_path):
    target = tmp_path / setup_exe.NAME
    target.write_bytes(b"MZ first install")
    with pytest.raises(ValueError, match="sha256"):
        setup_exe.bring_current(tmp_path, _meta(), lambda route: b"MZ tampered")
    assert target.read_bytes() == b"MZ first install"
    assert sorted(p.name for p in tmp_path.iterdir()) == [setup_exe.NAME]


def test_a_release_naming_no_installer_keeps_a_copy_of_the_running_one(tmp_path):
    running = tmp_path / "Downloads" / setup_exe.NAME
    running.parent.mkdir()
    running.write_bytes(b"MZ downloaded")
    folder = tmp_path / "holobuilder"
    folder.mkdir()
    assert (
        setup_exe.bring_current(folder, {}, lambda route: pytest.fail("downloaded"), running=running)
        == "copied"
    )
    assert (folder / setup_exe.NAME).read_bytes() == b"MZ downloaded"
    # the installed copy running itself (a reinstall from it) is not copied onto itself
    installed = folder / setup_exe.NAME
    assert setup_exe.bring_current(folder, {}, lambda route: b"", running=installed) == "kept"
    assert setup_exe.bring_current(folder, {}, lambda route: b"") == "kept"  # not frozen


def test_the_release_names_an_installer_built_from_its_own_sources(tmp_path, monkeypatch):
    import build_app
    import build_setup

    exe, stamp = tmp_path / setup_exe.NAME, tmp_path / (setup_exe.NAME + ".sources")
    monkeypatch.setattr(build_setup, "EXE", exe)
    monkeypatch.setattr(build_setup, "STAMP", stamp)
    with pytest.raises(SystemExit, match="build_setup.py first"):
        build_app.setup_exe_for_release()  # no exe
    exe.write_bytes(NEW)
    stamp.write_text("an older build\n", "utf8")
    with pytest.raises(SystemExit, match="build_setup.py first"):
        build_app.setup_exe_for_release()
    stamp.write_text(build_setup.sources_digest() + "\n", "utf8")
    assert build_app.setup_exe_for_release() == exe
