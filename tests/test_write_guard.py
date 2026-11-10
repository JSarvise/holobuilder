"""The session's write guard (tests/support/write_guard.py): the real state/ and dumps/ cannot be
written; reading them and writing anywhere else still work."""
from __future__ import annotations

import os

import pytest

from hbuilder import paths


@pytest.mark.parametrize("folder", [paths.STATE, paths.DUMPS])
def test_writes_into_the_real_folders_are_refused(folder):
    target = folder / "__write_guard_probe__.json"
    with pytest.raises(PermissionError, match="real"):
        target.write_text("{}", "utf8")
    with pytest.raises(PermissionError, match="real"):
        open(target, "ab").close()
    with pytest.raises(PermissionError, match="real"):
        os.close(os.open(target, os.O_WRONLY | os.O_CREAT))
    with pytest.raises(PermissionError, match="real"):
        (folder / "__write_guard_new_dir__").mkdir()
    assert not target.exists()


def test_replace_onto_and_delete_of_a_real_file_are_refused(tmp_path):
    src = tmp_path / "x.json"
    src.write_text("{}", "utf8")
    with pytest.raises(PermissionError, match="real"):
        os.replace(src, paths.DUMPS / "__write_guard_probe__.json")
    with pytest.raises(PermissionError, match="real"):
        os.remove(paths.STATE / "__write_guard_probe__.json")


def test_reading_and_tmp_path_stay_free(tmp_path):
    (tmp_path / "ok.json").write_text("{}", "utf8")
    assert (tmp_path / "ok.json").read_text("utf8") == "{}"
    for folder in (paths.STATE, paths.DUMPS):           # a clean clone has neither yet
        if folder.is_dir():
            folder.mkdir(exist_ok=True)                 # an existing real folder is fine
            next(folder.iterdir(), None)
