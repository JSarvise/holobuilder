"""`cbuild.build` on a PC without a compiler (the desktop app on a user's machine): the library
shipped next to its sources is used even when an update wrote a source a few ms after it, and
only a missing library is an error."""

from __future__ import annotations

import ctypes
import importlib
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cbuild  # noqa: E402

KERNELS = ("notewalk", "connect", "surrogate", "cdvar")
# The tests that score real charts skip without the game data (a clone, the public CI), so a
# kernel that no longer compiles would only show on a PC with the data. Here every kernel is
# compiled with the contract's flags wherever there is a gcc; on the CI's Linux, which always has
# one, a missing gcc is a failure rather than a skip.
NEEDS_GCC = sys.platform.startswith("linux") and bool(os.environ.get("CI"))


def test_without_a_compiler_a_shipped_library_is_used_even_if_its_source_is_newer(tmp_path, monkeypatch):
    monkeypatch.setattr(cbuild.shutil, "which", lambda name: None)
    source, target = tmp_path / "k.c", tmp_path / "k.dll"
    target.write_bytes(b"lib")
    source.write_text("int x;", encoding="utf-8")
    os.utime(target, (1_000_000, 1_000_000))
    os.utime(source, (1_000_005, 1_000_005))  # the source extracted after its library
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


@pytest.mark.skipif(shutil.which("gcc") is None and not NEEDS_GCC, reason="no gcc to build the kernels")
@pytest.mark.parametrize("name", KERNELS)
def test_every_kernel_compiles_and_exports_what_its_loader_binds(name, tmp_path):
    module = importlib.import_module(name)
    library = cbuild.build(module.SOURCE, cbuild.library(tmp_path, name), name, force=True)
    lib = ctypes.CDLL(str(library))
    bound = set(re.findall(r"lib\.(hd_\w+)\.(?:restype|argtypes)", Path(module.__file__).read_text("utf-8")))
    assert bound, f"{name}.py binds no kernel function"
    for symbol in sorted(bound):
        assert hasattr(lib, symbol), f"{name}: the compiled library has no {symbol}"
