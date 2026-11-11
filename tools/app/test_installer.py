"""installer: what an update keeps and where a run works. No window, no network, no registry."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import installer as ins  # noqa: E402


def test_an_update_keeps_this_pcs_data_and_nothing_else(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    for rel in (
        "app/state/app_settings.json",
        "app/tools/recon/proxy/dumps/charts.json",
        "app/data/owned.json",
        "app/data/gacha/pulls.jsonl",
        "app/tools/app/launcher.py",
    ):
        (old / rel).parent.mkdir(parents=True, exist_ok=True)
        (old / rel).write_text("x")
    moves = {src.relative_to(old).as_posix(): dst for src, dst in ins.preserve_moves(old, new)}
    assert set(moves) == {
        "app/state",
        "app/tools/recon/proxy/dumps",
        "app/data/owned.json",
        "app/data/gacha",
    }  # the code is never carried over
    assert moves["app/state"] == new / "app" / "state"


def test_the_folder_is_never_guessed_when_one_is_given(tmp_path, monkeypatch):
    # an update with the registry entry missing must not fall back to the default folder
    assert ins.install_dir(str(tmp_path)) == tmp_path
    monkeypatch.setattr(ins, "registered_dir", lambda: None)
    assert ins.install_dir(None) == ins.DEFAULT_DIR  # not frozen, nothing registered


def test_powershell_quoting_survives_apostrophes():
    assert ins.ps_quote("C:\\Users\\O'Brien\\x") == "'C:\\Users\\O''Brien\\x'"


def test_an_installer_older_than_setup_min_points_at_the_new_one(monkeypatch):
    """The signed version.json's setup_min stops a kept old exe from
    installing; equal or newer, or no setup_min at all (servers before it), installs."""
    monkeypatch.setattr(ins, "SETUP_VERSION", "1.0.0")
    assert ins.setup_outdated({}) is None
    assert ins.setup_outdated({"setup_min": "1.0.0"}) is None
    msg = ins.setup_outdated({"setup_min": "1.1.0"})
    assert msg and "out of date" in msg and ins.SETUP_NAME in msg


# --- the uninstall removes only what an install put there, and no shell parses a path


def _install(d: Path, *, created: bool = True, extra: tuple[str, ...] = ()) -> None:
    for rel in (
        "app/tools/app/launcher.py",
        "app/state/app_settings.json",
        "runtime/pythonw.exe",
        ins.SETUP_NAME,
    ):
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text("x")
    ins.write_marker(d, created)
    for rel in extra:
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text("theirs")


def _uninstall(d: Path, monkeypatch) -> tuple[list, list]:
    calls: list = []
    events: list = []
    monkeypatch.setenv("HB_SETUP_NO_REGISTRY", "1")
    monkeypatch.setenv("HB_SETUP_SHORTCUTS", str(d.parent / "shortcuts"))
    monkeypatch.setattr(ins.time, "sleep", lambda s: None)
    monkeypatch.setattr(ins.subprocess, "run", lambda *a, **k: calls.append(("run", a, k)))
    monkeypatch.setattr(ins.subprocess, "Popen", lambda *a, **k: calls.append(("Popen", a, k)))
    api = ins.Api("uninstall", None, str(d))
    monkeypatch.setattr(api, "_emit", lambda **ev: events.append(ev))
    api._uninstall()
    return calls, events


def test_an_uninstall_leaves_everything_it_did_not_install(tmp_path, monkeypatch):
    d = tmp_path / "holobuilder"
    _install(d, created=False, extra=("notes.txt", "src/main.py"))
    calls, events = _uninstall(d, monkeypatch)
    assert {"step": "removed"} in events
    assert sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()) == [
        ins.SETUP_NAME,  # running: removed after this process exits
        "notes.txt",
        "src/main.py",
    ]
    assert all(k.get("shell") is not True for _kind, _a, k in calls)
    (kind, (cmd,), _k) = calls[-1]
    assert kind == "Popen" and cmd[0] == "powershell" and "rmdir" not in cmd[-1] and "-Recurse" not in cmd[-1]
    assert ins.ps_quote(str(d / ins.SETUP_NAME)) in cmd[-1]
    assert ins.ps_quote(str(d)) not in cmd[-1].replace(
        ins.ps_quote(str(d / ins.SETUP_NAME)), ""
    )  # not created


def test_a_folder_the_install_created_goes_only_when_empty(tmp_path, monkeypatch):
    d = tmp_path / "holobuilder"
    _install(d, created=True)
    calls, _events = _uninstall(d, monkeypatch)
    script = calls[-1][1][0][-1]
    assert f"Get-ChildItem -LiteralPath {ins.ps_quote(str(d))}" in script  # the emptiness check first
    assert sorted(p.name for p in d.iterdir()) == [ins.SETUP_NAME]


def test_a_folder_that_is_not_an_install_is_left_alone(tmp_path, monkeypatch):
    d = tmp_path / "holobuilder"  # e.g. a clone of the repository: no marker, no installed launcher
    for rel in ("app/readme.txt", "runtime/x", "src/hbuilder/__init__.py"):
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text("theirs")
    calls, events = _uninstall(d, monkeypatch)
    assert calls == [] and events[-1]["step"] == "error" and "nothing was removed" in events[-1]["message"]
    assert len([p for p in d.rglob("*") if p.is_file()]) == 3


def test_an_install_made_before_the_marker_is_still_recognised(tmp_path):
    d = tmp_path / "holobuilder"
    (d / "app" / "tools" / "app").mkdir(parents=True)
    (d / "app" / "tools" / "app" / "launcher.py").write_text("x")
    assert ins.is_install(d) and ins.created_dir(d)


def test_an_install_refuses_a_folder_that_holds_someone_elses_files(tmp_path):
    d = tmp_path / "holobuilder"
    assert ins.install_target_problem(d) is None  # new
    d.mkdir()
    assert ins.install_target_problem(d) is None  # empty
    (d / "notes.txt").write_text("theirs")
    assert "not empty" in (ins.install_target_problem(d) or "")
    ins.write_marker(d, False)
    assert ins.install_target_problem(d) is None  # an install of its own: a reinstall
    ins.write_marker(d, True)
    assert ins.created_dir(d) is False  # a reinstall keeps the first install's answer


def test_the_installer_never_hands_a_command_line_to_a_shell():
    import ast

    tree = ast.parse((Path(ins.__file__)).read_text("utf8"))
    shells = [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.keyword)
        and n.arg == "shell"
        and not (isinstance(n.value, ast.Constant) and n.value.value is False)
    ]
    assert shells == []
