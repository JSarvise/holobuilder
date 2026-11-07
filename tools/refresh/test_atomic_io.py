"""`atomic_io`: whole-file writes that never leave a half-written file behind."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import atomic_io  # noqa: E402


def test_write_text_puts_the_same_bytes_on_disk_as_path_write_text(tmp_path):
    # switching a writer to it must not change the file (line endings included)
    text = '{\n "a": "ñ"\n}\n'
    plain, atomic = tmp_path / "plain.json", tmp_path / "atomic.json"
    plain.write_text(text, "utf8")
    atomic_io.write_text(atomic, text, "utf8")
    assert atomic.read_bytes() == plain.read_bytes()
    atomic_io.write_bytes(atomic, b"\x00\x01")
    assert atomic.read_bytes() == b"\x00\x01"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["atomic.json", "plain.json"]


def test_a_refused_replace_is_retried_and_the_old_file_survives_a_failure(tmp_path, monkeypatch):
    target = tmp_path / "inventory.json"
    target.write_text("old", "utf8")
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) < 3:
            raise OSError(22, "Invalid argument")   # the error a real refresh hit
        real(src, dst)
    monkeypatch.setattr(atomic_io.os, "replace", flaky)
    monkeypatch.setattr(atomic_io.time, "sleep", lambda _s: None)
    atomic_io.write_text(target, "new")
    assert target.read_text("utf8") == "new" and len(calls) == 3

    def never(src, dst):
        raise OSError(22, "Invalid argument")
    monkeypatch.setattr(atomic_io.os, "replace", never)
    with pytest.raises(OSError):
        atomic_io.write_text(target, "newer")
    # the target is the last good file, and no temp file is left beside it
    assert target.read_text("utf8") == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["inventory.json"]
