"""tools/local/run_local.py: what setup.cmd, refresh.cmd and start.cmd rely on, without the game,
an account or the network."""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "local"))

import run_local  # noqa: E402


def _cred(folder: Path, name: str = "0123456789abcdef0123456789abcdef", size: int = 258) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"\xef\xbb\xbf" + b"x" * (size - 3))
    return path


def test_the_one_account_file_in_the_game_folder_is_found(tmp_path):
    want = _cred(tmp_path)
    (tmp_path / "Player.log").write_text("log")
    (tmp_path / ("f" * 32)).write_bytes(b"x" * 5000)            # right name, not a credential's size
    assert run_local.find_credential({}, tmp_path) == (want, "the game's own folder")


def test_two_account_files_are_not_guessed_between(tmp_path):
    _cred(tmp_path)
    _cred(tmp_path, "fedcba9876543210fedcba9876543210")
    path, why = run_local.find_credential({}, tmp_path)
    assert path is None and "2 files" in why and "HB_CREDENTIAL_FILE" in why


def test_no_game_folder_says_what_to_do(tmp_path):
    path, why = run_local.find_credential({}, tmp_path / "absent")
    assert path is None and "start the game" in why


def test_HB_CREDENTIAL_FILE_wins_and_must_exist(tmp_path):
    _cred(tmp_path / "game")
    mine = _cred(tmp_path / "elsewhere")
    assert run_local.find_credential({"HB_CREDENTIAL_FILE": str(mine)}, tmp_path / "game") == \
        (mine, "HB_CREDENTIAL_FILE")
    path, why = run_local.find_credential({"HB_CREDENTIAL_FILE": str(tmp_path / "nope")}, tmp_path / "game")
    assert path is None and "names no file" in why


def test_region_comes_from_the_option_then_the_environment_then_this_folder():
    hosts = run_local.REGION_HOSTS
    assert run_local.region_host("jp", {"HB_ACCOUNT_HOST": "x.example"}, {"region": "as"}) == hosts["jp"]
    assert run_local.region_host(None, {"HB_ACCOUNT_HOST": "x.example"}, {"region": "as"}) == "x.example"
    assert run_local.region_host(None, {}, {"region": "as"}) == hosts["as"]
    assert run_local.region_host(None, {}, {}) is None


def test_region_is_the_first_server_that_accepts_the_login():
    tried = []

    def login(host):
        tried.append(host)
        if not host.startswith("jp."):
            raise RuntimeError("login rejected: grpc-status 16 UNAUTHENTICATED")

    assert run_local.detect_region(login) == "jp"
    assert tried == [run_local.REGION_HOSTS[r] for r in ("us", "as", "jp")]


def test_a_login_failure_that_is_not_the_region_is_not_swallowed():
    def login(host):
        raise RuntimeError("login rejected: grpc-status 3 OUTDATED_APP")

    with pytest.raises(RuntimeError, match="OUTDATED_APP"):
        run_local.detect_region(login)


def test_no_region_accepts_the_login():
    def login(host):
        raise RuntimeError("login rejected: grpc-status 16")

    assert run_local.detect_region(login) is None


def test_the_region_found_is_kept_in_this_folder(tmp_path):
    path = tmp_path / "state" / "local.json"
    assert run_local.saved(path) == {}
    run_local.save(path, region="as")
    run_local.save(path, other=1)
    assert run_local.saved(path) == {"region": "as", "other": 1}


def test_the_children_get_the_account_settings(tmp_path):
    env = run_local.child_env(tmp_path / "cred", "as.example", base={"HOLODREAMS_DIR": "D:\\game"})
    assert env["HB_CREDENTIAL_FILE"] == str(tmp_path / "cred")
    assert env["HB_ACCOUNT_HOST"] == "as.example"
    assert env["HOLODREAMS_DIR"] == "D:\\game"                # a folder set by hand is kept
    assert env["PYTHONIOENCODING"] == "utf-8"
    bare = run_local.child_env(None, None, base={"HOLODREAMS_DIR": "D:\\game"})
    assert "HB_CREDENTIAL_FILE" not in bare and "HB_ACCOUNT_HOST" not in bare


def test_only_this_folders_server_counts_as_ours(tmp_path):
    repo = tmp_path / "My Clone"
    mine = f'"C:\\Python\\python.exe" -u "{repo}\\tools\\web\\server.py" --port 8731'
    assert run_local.ours(mine, repo)
    assert run_local.ours(mine.replace("\\", "/").upper(), repo)
    assert not run_local.ours(mine.replace("My Clone", "Other"), repo)
    assert not run_local.ours("", repo)


def test_a_port_held_by_another_program_is_left_alone(monkeypatch):
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen()
    port = held.getsockname()[1]
    try:
        monkeypatch.setattr(run_local, "listeners", lambda p: [{"pid": 1, "name": "other.exe", "cmd": "x"}])
        monkeypatch.setattr(run_local.time, "sleep", lambda s: None)
        killed = []
        monkeypatch.setattr(run_local.subprocess, "run", lambda *a, **k: killed.append(a))
        got = run_local.claim_port(port)
        assert got is not None and got != port and not killed
    finally:
        held.close()


def test_a_free_port_is_used_as_it_is():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert run_local.claim_port(port) == port


def test_refresh_passes_other_options_to_refresh_all(monkeypatch):
    seen = {}
    monkeypatch.setattr(run_local, "refresh", lambda args, extra: seen.update(region=args.region,
                                                                               extra=extra) or 0)
    assert run_local.main(["refresh", "--region", "as", "--skip", "chibi", "--pictures-later"]) == 0
    assert seen == {"region": "as", "extra": ["--skip", "chibi", "--pictures-later"]}


def test_start_and_check_refuse_unknown_options():
    with pytest.raises(SystemExit):
        run_local.main(["start", "--skip", "chibi"])
    with pytest.raises(SystemExit):
        run_local.main(["check", "--whatever"])


def test_the_launchers_call_the_helper_from_their_own_folder():
    for name, sub in (("setup.cmd", "check"), ("refresh.cmd", "refresh"), ("start.cmd", "start")):
        raw = (ROOT / name).read_bytes()
        text = raw.decode("ascii")                         # cmd reads them in the OEM code page
        assert 'cd /d "%~dp0"' in text                    # any clone folder, any working directory
        assert f"tools\\local\\run_local.py {sub}" in text
        assert "goto" not in text.lower() and "\n:" not in text   # no labels: LF-only copies still run
