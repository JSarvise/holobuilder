"""refresh_all stops before any step when the game is in maintenance."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import account_snapshot  # noqa: E402
import refresh_all  # noqa: E402


def test_maintenance_runs_no_step_and_says_so(monkeypatch, capsys):
    monkeypatch.setattr(account_snapshot, "in_maintenance", lambda account="main": True)
    ran = []
    monkeypatch.setattr(refresh_all, "_run", lambda *a, **k: ran.append(a) or (0, ""))
    monkeypatch.setattr(sys, "argv", ["refresh_all.py"])
    assert refresh_all.main() == refresh_all.EXIT_MAINTENANCE
    assert ran == []
    assert "GAME IN MAINTENANCE" in capsys.readouterr().out


def test_in_maintenance_reads_the_master_get_refusal(monkeypatch):
    monkeypatch.setattr(account_snapshot, "load_credential", lambda a: ("h", b"x"))
    monkeypatch.setattr(account_snapshot, "client_headers", lambda a: [])

    def refused(host, ident):
        raise account_snapshot.GameMaintenance("Master/Get rejected: grpc-status 3 IN_MAINTENANCE")
    monkeypatch.setattr(account_snapshot, "fetch_master_version", refused)
    assert account_snapshot.in_maintenance() is True
    monkeypatch.setattr(account_snapshot, "fetch_master_version", lambda h, i: "v1")
    assert account_snapshot.in_maintenance() is False
    monkeypatch.setattr(account_snapshot, "load_credential", lambda a: sys.exit("no credential"))
    assert account_snapshot.in_maintenance() is None
