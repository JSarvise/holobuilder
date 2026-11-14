"""tools/local/run_local.py: what setup.cmd, refresh.cmd and start.cmd rely on, without the game,
an account or the network."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
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
    (tmp_path / ("f" * 32)).write_bytes(b"x" * 5000)  # right name, not a credential's size
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
    assert run_local.find_credential({"HB_CREDENTIAL_FILE": str(mine)}, tmp_path / "game") == (
        mine,
        "HB_CREDENTIAL_FILE",
    )
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
    assert env["HOLODREAMS_DIR"] == "D:\\game"  # a folder set by hand is kept
    assert env["PYTHONIOENCODING"] == "utf-8"
    bare = run_local.child_env(None, None, base={"HOLODREAMS_DIR": "D:\\game"})
    assert "HB_CREDENTIAL_FILE" not in bare and "HB_ACCOUNT_HOST" not in bare


def test_only_this_folders_server_counts_as_ours(tmp_path):
    repo = tmp_path / "My Clone"
    repo.mkdir()
    assert run_local.ours({"repo": str(repo), "pid": 7}, repo)
    assert not run_local.ours({"repo": str(tmp_path / "Other"), "pid": 7}, repo)
    assert not run_local.ours(None, repo)


def test_a_port_held_by_another_program_is_left_alone(monkeypatch):
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen()
    port = held.getsockname()[1]
    try:
        monkeypatch.setattr(run_local.time, "sleep", lambda s: None)
        killed = []
        monkeypatch.setattr(run_local.os, "kill", lambda *a: killed.append(a))
        monkeypatch.setattr(run_local, "server_on", lambda p: None)  # not a holobuilder server
        got = run_local.claim_port(port)
        assert got is not None and got != port and not killed
        monkeypatch.setattr(run_local, "server_on", lambda p: {"repo": "/elsewhere", "pid": 9})
        got = run_local.claim_port(port)
        assert got is not None and got != port and not killed
    finally:
        held.close()


def test_an_earlier_server_of_this_folder_is_stopped(monkeypatch):
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen()
    port = held.getsockname()[1]
    killed = []

    def kill(pid, sig):
        killed.append((pid, sig))
        held.close()  # the old server lets the port go

    monkeypatch.setattr(run_local, "server_on", lambda p: {"repo": str(run_local.REPO), "pid": 4242})
    monkeypatch.setattr(run_local.os, "kill", kill)
    monkeypatch.setattr(run_local.time, "sleep", lambda s: None)
    assert run_local.claim_port(port) == port
    assert killed == [(4242, run_local.signal.SIGTERM)]


def test_the_server_answer_is_read_from_the_port():
    import http.server
    import json
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({"repo": "/r", "pid": 5}).encode() if self.path == "/api/server" else b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert run_local.server_on(srv.server_address[1]) == {"repo": "/r", "pid": 5}
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_launchers_and_helpers_call_no_powershell():
    for name in ("tools/local/run_local.py", "tools/web/hbweb/router.py", "tools/refresh/refresh_all.py"):
        text = (ROOT / name).read_text("utf8")
        assert "Get-NetTCPConnection" not in text and "taskkill" not in text
        assert '"powershell"' not in text or name.endswith("refresh_all.py")


def test_a_free_port_is_used_as_it_is():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert run_local.claim_port(port) == port


def test_refresh_passes_other_options_to_refresh_all(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        run_local, "refresh", lambda args, extra: seen.update(region=args.region, extra=extra) or 0
    )
    assert run_local.main(["refresh", "--region", "as", "--skip", "chibi", "--pictures-later"]) == 0
    assert seen == {"region": "as", "extra": ["--skip", "chibi", "--pictures-later"]}


def test_start_and_check_refuse_unknown_options():
    with pytest.raises(SystemExit):
        run_local.main(["start", "--skip", "chibi"])
    with pytest.raises(SystemExit):
        run_local.main(["check", "--whatever"])


LAUNCHERS = ("setup", "refresh", "start", "update")


def test_the_launchers_call_the_helper_from_their_own_folder():
    for sub in LAUNCHERS:
        name = f"{sub}.cmd"
        raw = (ROOT / name).read_bytes()
        text = raw.decode("ascii")  # cmd reads them in the OEM code page
        assert 'cd /d "%~dp0"' in text  # any clone folder, any working directory
        assert f"tools\\local\\run_local.py {sub}" in text
        assert "goto" not in text.lower() and "\n:" not in text  # no labels: LF-only copies still run


def test_the_update_launchers_stop_reading_themselves_before_the_pull():
    # a pull can rewrite the launcher that runs it: cmd reads a (...) block whole before running
    # it and bash stops reading at exec, so the lines still to run cannot shift under them
    cmd = (ROOT / "update.cmd").read_bytes().decode("ascii")
    block = cmd[cmd.index('(\r\n  ".venv') :]
    assert "run_local.py update %*" in block and block.rstrip().endswith(")")
    assert "!ERRORLEVEL!" in block and "EnableDelayedExpansion" in cmd  # %ERRORLEVEL% expands too early
    assert "exec .venv/bin/python -u tools/local/run_local.py update" in (ROOT / "update.sh").read_text(
        "ascii"
    )


def test_the_shell_launchers_mirror_the_cmd_ones():
    for sub in LAUNCHERS:
        name = f"{sub}.sh"
        raw = (ROOT / name).read_bytes()
        assert b"\r" not in raw  # bash reads a CR as part of the line
        text = raw.decode("ascii")
        assert text.startswith("#!/usr/bin/env bash\n")
        assert 'cd "$(dirname "$0")"' in text  # any clone folder, any working directory
        assert f"tools/local/run_local.py {sub}" in text
    assert "*.sh text eol=lf" in (ROOT / ".gitattributes").read_text("utf8")


def test_launcher_names_follow_the_platform(monkeypatch):
    monkeypatch.setattr(run_local, "WINDOWS", True)
    assert run_local.launcher("refresh") == "refresh.cmd"
    monkeypatch.setattr(run_local, "WINDOWS", False)
    assert run_local.launcher("refresh") == "./refresh.sh"


def test_shown_hides_the_user_folder_on_both_platforms():
    assert run_local.shown(r"C:\Users\Ana\x", r"C:\Users\ana", windows=True) == r"%USERPROFILE%\x"
    assert run_local.shown("/home/ana/x", "/home/ana", windows=False) == "~/x"
    assert run_local.shown("/home/Ana/x", "/home/ana", windows=False) == "/home/Ana/x"


def test_setup_creates_the_venv_installs_and_checks(monkeypatch, tmp_path):
    venv = tmp_path / ".venv"
    py = run_local.paths.venv_python(venv)
    calls = []

    def fake_run(cmd, **_kw):
        calls.append([str(c) for c in cmd])
        if cmd[1:3] == ["-m", "venv"]:
            py.parent.mkdir(parents=True)
            py.write_text("")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(run_local.paths, "VENV", venv)
    monkeypatch.setattr(run_local.subprocess, "run", fake_run)
    assert run_local.setup() == 0
    assert calls[0][1:] == ["-m", "venv", str(venv)]
    assert calls[1] == [str(py), "-m", "pip", "--version"]
    assert calls[2][:4] == [str(py), "-m", "pip", "install"] and calls[2][-3:] == [
        ".",
        "-r",
        "requirements.txt",
    ]
    assert calls[3][0] == str(py) and calls[3][-1] == "check"


def test_setup_stops_when_the_venv_cannot_be_made(monkeypatch, tmp_path):
    monkeypatch.setattr(run_local.paths, "VENV", tmp_path / ".venv")
    monkeypatch.setattr(run_local.subprocess, "run", lambda cmd, **_kw: type("R", (), {"returncode": 1})())
    assert run_local.setup() == 1


def test_setup_refuses_a_python_older_than_the_minimum(monkeypatch, capsys):
    monkeypatch.setattr(run_local, "MIN_PYTHON", (99, 0))
    monkeypatch.setattr(run_local.subprocess, "run", lambda *a, **k: pytest.fail("ran something"))
    assert run_local.setup() == 1
    assert "is too old: 3.12 or newer is needed" in capsys.readouterr().out


def test_setup_makes_a_venv_from_an_older_python_again(monkeypatch, tmp_path, capsys):
    venv = tmp_path / ".venv"
    py = run_local.paths.venv_python(venv)
    py.parent.mkdir(parents=True)
    py.write_text("")
    calls = []
    monkeypatch.setattr(run_local.paths, "VENV", venv)
    monkeypatch.setattr(run_local, "venv_version", lambda _py: (3, 11))
    monkeypatch.setattr(
        run_local.subprocess,
        "run",
        lambda cmd, **_kw: calls.append([str(c) for c in cmd]) or type("R", (), {"returncode": 0})(),
    )
    assert run_local.setup() == 0
    assert calls[0] == [sys.executable, "-m", "venv", "--clear", str(venv)]
    assert "runs Python 3.11, too old" in capsys.readouterr().out


def test_the_venv_version_is_read_from_its_python():
    assert run_local.venv_version(Path(sys.executable)) == tuple(sys.version_info[:2])
    assert run_local.venv_version(Path(sys.executable).with_name("no-such-python.exe")) is None


@pytest.mark.parametrize("command", [["start"], ["refresh"]])
def test_start_and_refresh_refuse_a_venv_older_than_the_minimum(monkeypatch, capsys, command):
    monkeypatch.setattr(run_local, "MIN_PYTHON", (99, 0))
    monkeypatch.setattr(run_local, "start", lambda args: pytest.fail("started"))
    monkeypatch.setattr(run_local, "refresh", lambda args, extra: pytest.fail("refreshed"))
    assert run_local.main(command) == 1
    assert "holobuilder needs 3.12 or newer" in capsys.readouterr().out


def test_the_minimum_python_is_the_same_everywhere():
    import re

    assert run_local.MIN_PYTHON == (3, 12)
    assert 'requires-python = ">=3.12"' in (ROOT / "pyproject.toml").read_text("utf8")
    for name in ("setup.cmd", "setup.sh"):
        text = (ROOT / name).read_text("utf8")
        assert "sys.version_info < (3, 12)" in text and "3.11" not in text, name
    assert "sys.version_info < (3, 12)" in (ROOT / "tools" / "web" / "server.py").read_text("utf8")
    assert not re.search(r"3\.11", (ROOT / "tools" / "local" / "run_local.py").read_text("utf8"))


def test_venv_python_per_platform(tmp_path):
    from hbuilder import paths

    assert paths.venv_python(tmp_path, "win32") == tmp_path / "Scripts" / "python.exe"
    assert paths.venv_python(tmp_path, "linux") == tmp_path / "bin" / "python"


# ---------------------------------------------------------------- update
CATALOGUE = run_local.CATALOGUE  # the clone's catalogue, relative to its folder


def _card(n: int, name: str = "") -> dict:
    return {"id": f"card-{n:05d}", "name": name or f"card {n}"}


def _catalogue(*cards: dict) -> bytes:
    doc = {"cards": list(cards), "schema_version": 1}
    return (json.dumps(doc, indent=2) + "\n").replace("\n", "\r\n").encode()


def test_only_added_cards_accepts_whole_new_cards_and_nothing_else():
    before = json.loads(_catalogue(_card(1), _card(2)))
    assert run_local.only_added_cards(before, json.loads(_catalogue(_card(1), _card(2), _card(3))))
    assert run_local.only_added_cards(before, before)
    assert not run_local.only_added_cards(before, json.loads(_catalogue(_card(1, "fixed"), _card(2))))
    assert not run_local.only_added_cards(before, json.loads(_catalogue(_card(2), _card(3))))  # one gone
    assert not run_local.only_added_cards(before, {**before, "schema_version": 2})
    assert not run_local.only_added_cards(before, [])


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _commit(repo: Path, files: dict[str, bytes], message: str) -> None:
    for rel, body in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def clone(tmp_path):
    """(GitHub's repository, a clone of it): the clone as a user has it after a refresh added
    card 3 to the catalogue."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    upstream, mine = tmp_path / "github", tmp_path / "mine"
    upstream.mkdir()
    _git(upstream, "init", "-q")
    (upstream / ".gitattributes").write_bytes(b"* -text\n")  # bytes as written, whatever core.autocrlf says
    _commit(upstream, {CATALOGUE: _catalogue(_card(1), _card(2)), "code.py": b"v1\n"}, "0.1")
    _git(tmp_path, "clone", "-q", str(upstream), str(mine))
    (mine / CATALOGUE).write_bytes(_catalogue(_card(1), _card(2), _card(3)))
    return upstream, mine


def _update(mine: Path, no_refresh: bool = False) -> tuple[int, list[str]]:
    steps: list[str] = []

    def run(cmd: list[str]) -> int:
        steps.append(cmd[-1])
        return 0

    code = run_local.update(argparse.Namespace(no_refresh=no_refresh), repo=mine, run=run)
    return code, steps


def test_update_puts_the_refreshs_cards_aside_when_the_new_version_changes_the_catalogue(clone):
    upstream, mine = clone
    newer = _catalogue(_card(1), _card(2, "hand corrected"), _card(3), _card(4))
    _commit(upstream, {CATALOGUE: newer, "code.py": b"v2\n"}, "0.2")
    code, steps = _update(mine)
    assert code == 0 and steps == ["refresh"]
    assert _git(mine, "rev-parse", "HEAD") == _git(upstream, "rev-parse", "HEAD")
    assert (mine / CATALOGUE).read_bytes() == newer  # the refresh adds back what it lacks
    backup = mine / run_local.paths.DUMPS_REL / run_local.CATALOGUE_BACKUP
    assert json.loads(backup.read_bytes())["cards"][-1] == _card(3)


def test_update_keeps_the_refreshs_cards_when_the_new_version_leaves_the_catalogue_alone(clone):
    upstream, mine = clone
    _commit(upstream, {"code.py": b"v2\n"}, "0.2")
    assert _update(mine) == (0, ["refresh"])
    assert (mine / "code.py").read_bytes() == b"v2\n"
    assert json.loads((mine / CATALOGUE).read_bytes())["cards"][-1] == _card(3)
    assert not (mine / run_local.paths.DUMPS_REL / run_local.CATALOGUE_BACKUP).exists()


@pytest.mark.parametrize("edit", ["code", "catalogue"])
def test_update_never_touches_a_change_of_the_users_own(clone, edit):
    upstream, mine = clone
    _commit(upstream, {CATALOGUE: _catalogue(_card(1), _card(2), _card(4)), "code.py": b"v2\n"}, "0.2")
    if edit == "code":
        (mine / "code.py").write_bytes(b"my own change\n")
        mine_before = (mine / "code.py").read_bytes()
    else:
        (mine / CATALOGUE).write_bytes(_catalogue(_card(1, "my fix"), _card(2)))
        mine_before = (mine / CATALOGUE).read_bytes()
    head = _git(mine, "rev-parse", "HEAD")
    assert _update(mine) == (1, [])
    assert _git(mine, "rev-parse", "HEAD") == head
    path = mine / ("code.py" if edit == "code" else CATALOGUE)
    assert path.read_bytes() == mine_before


def test_update_runs_setup_when_the_packages_change_and_refresh_is_optional(clone):
    upstream, mine = clone
    _commit(upstream, {"requirements.txt": b"numpy\n"}, "0.2")
    assert _update(mine, no_refresh=True) == (0, ["setup"])


def test_update_with_nothing_new_still_refreshes(clone):
    _, mine = clone
    assert _update(mine) == (0, ["refresh"])
    assert _update(mine, no_refresh=True) == (0, [])


def test_update_needs_a_git_clone(tmp_path):
    assert _update(tmp_path) == (1, [])


def test_main_routes_update(monkeypatch):
    seen = {}
    monkeypatch.setattr(run_local, "update", lambda args: seen.update(no_refresh=args.no_refresh) or 0)
    assert run_local.main(["update", "--no-refresh"]) == 0 and seen == {"no_refresh": True}
    with pytest.raises(SystemExit):
        run_local.main(["update", "--skip", "chibi"])
