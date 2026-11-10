"""`cbuild.build` on a PC without a compiler (the desktop app on a user's machine): the library
shipped next to its sources is used even when an update wrote a source a few ms after it, and
only a missing library is an error."""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cbuild  # noqa: E402


def test_without_a_compiler_a_shipped_library_is_used_even_if_its_source_is_newer(tmp_path, monkeypatch):
    monkeypatch.setattr(cbuild.shutil, "which", lambda name: None)
    source, target = tmp_path / "k.c", tmp_path / "k.dll"
    target.write_bytes(b"lib")
    source.write_text("int x;", encoding="utf-8")
    os.utime(target, (1_000_000, 1_000_000))
    os.utime(source, (1_000_005, 1_000_005))         # the source extracted after its library
    assert cbuild.build(source, target, "test") == target
    assert cbuild.build(source, target, "test", force=True) == target
    assert target.read_bytes() == b"lib"


def test_without_a_compiler_a_missing_library_is_an_error(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setattr(cbuild.shutil, "which", lambda name: None)
    source = tmp_path / "k.c"
    source.write_text("int x;", encoding="utf-8")
    with pytest.raises(RuntimeError, match="no gcc"):
        cbuild.build(source, tmp_path / "k.dll", "test")
