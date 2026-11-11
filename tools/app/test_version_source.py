"""The app's version has one source, `src/hbuilder/VERSION`: pyproject.toml reads it
(setuptools' dynamic version) and the build takes it as the default and only accepted value."""

from __future__ import annotations

import re
import tomllib

import build_app
import pytest

from hbuilder import paths


def test_pyproject_reads_the_version_file():
    doc = tomllib.loads((paths.REPO / "pyproject.toml").read_text("utf-8"))
    assert "version" not in doc["project"]
    assert doc["project"]["dynamic"] == ["version"]
    assert doc["tool"]["setuptools"]["dynamic"]["version"] == {"file": "src/hbuilder/VERSION"}
    assert re.fullmatch(r"\d+\.\d+\.\d+", build_app.source_version())


def test_the_build_refuses_another_version(monkeypatch):
    monkeypatch.setattr(build_app, "build", lambda version: pytest.fail("built"))
    with pytest.raises(SystemExit):
        build_app.main(["0.0.1"])
