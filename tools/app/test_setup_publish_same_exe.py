"""`build_setup.py --publish` after a release publishes the exe that release's version.json
signed, not a rebuild: PyInstaller output is not byte-identical, so a rebuild would no longer
match the signed `setup_sha256`."""

from __future__ import annotations

import build_setup
import pytest


def test_publish_reuses_the_exe_built_from_these_sources(tmp_path, monkeypatch):
    exe = tmp_path / "holobuilder-setup.exe"
    exe.write_bytes(b"exe")
    stamp = tmp_path / "holobuilder-setup.exe.sources"
    stamp.write_text("digest", "utf8")
    monkeypatch.setattr(build_setup, "EXE", exe)
    monkeypatch.setattr(build_setup, "STAMP", stamp)
    monkeypatch.setattr(build_setup, "sources_digest", lambda: "digest")
    monkeypatch.setattr(build_setup, "build", lambda: pytest.fail("rebuilt"))
    published = []
    monkeypatch.setattr(build_setup, "publish", published.append)
    assert build_setup.main(["--publish"]) == 0
    assert published == [exe]


def test_other_sources_are_rebuilt(tmp_path, monkeypatch):
    exe = tmp_path / "holobuilder-setup.exe"
    exe.write_bytes(b"old")
    stamp = tmp_path / "holobuilder-setup.exe.sources"
    stamp.write_text("old digest", "utf8")
    monkeypatch.setattr(build_setup, "EXE", exe)
    monkeypatch.setattr(build_setup, "STAMP", stamp)
    monkeypatch.setattr(build_setup, "sources_digest", lambda: "new digest")
    built = tmp_path / "new.exe"
    built.write_bytes(b"new")
    monkeypatch.setattr(build_setup, "build", lambda: built)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())  # PyInstaller "present"
    monkeypatch.setattr(build_setup, "sums", lambda files, where: [])
    published = []
    monkeypatch.setattr(build_setup, "publish", published.append)
    assert build_setup.main(["--publish"]) == 0
    assert published == [built]
