"""account_snapshot.master_get on OUTDATED_APP: one retry with the current client version, no
guessed versions, and a refusal that says what to do."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import account_snapshot as A  # noqa: E402


def _server(accepts: str, seen: list):
    def call(host, path, body, ident=None, master_version=None):
        import os
        v = os.environ.get("HOLODREAMS_APP_VERSION") or dict(ident or A.GENERIC_HEADERS)["x-app-version"]
        seen.append(v)
        if v == accepts:
            return {}, {"grpc-status": "0"}, b"ok"
        return {}, {"grpc-status": "3", "grpc-message": "OUTDATED_APP"}, b""
    return call


@pytest.fixture(autouse=True)
def _own_version_file(monkeypatch, tmp_path):
    """An accepted retry is recorded in client_version.json: here, never in the real dumps/."""
    monkeypatch.setattr(A, "CLIENT_VERSION_FILE", tmp_path / "client_version.json")


def test_a_stale_sent_version_is_retried_once_with_the_current_one(monkeypatch, tmp_path):
    seen: list = []
    monkeypatch.delenv("HOLODREAMS_APP_VERSION", raising=False)
    monkeypatch.setattr(A, "call", _server("1.2.101", seen))
    monkeypatch.setattr(A, "app_version", lambda: "1.2.101")
    assert A.master_get("h", [("x-app-version", "1.1.1")]) == b"ok"
    assert seen == ["1.1.1", "1.2.101"]
    assert '"1.2.101"' in (tmp_path / "client_version.json").read_text("utf8")


def test_no_newer_version_known_ends_with_what_to_do_and_no_guesses(monkeypatch):
    seen: list = []
    monkeypatch.setenv("HOLODREAMS_APP_VERSION", "1.2.101")
    monkeypatch.setattr(A, "call", _server("1.2.150", seen))
    with pytest.raises(RuntimeError, match="publish.py"):
        A.master_get("h", [("x-app-version", "1.2.101")])
    assert seen == ["1.2.101"]
    assert not hasattr(A, "next_versions")
