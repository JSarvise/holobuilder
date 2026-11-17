"""The second environment for the game's asset bundles (.venv-assets): which interpreter runs the
scripts that import UnityPy when .venv runs a Python UnityPy has no wheels for."""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REFRESH = ROOT / "tools" / "refresh"
sys.path.insert(0, str(REFRESH))
sys.path.insert(0, str(ROOT / "tools" / "local"))

import refresh_steps  # noqa: E402
import run_local  # noqa: E402

from hbuilder import paths  # noqa: E402

# Modules whose UnityPy use is optional and handled in place: app_version() asks .venv-assets
# itself when this interpreter cannot import UnityPy.
OPTIONAL = {"account_snapshot"}


def test_this_python_reads_the_assets_when_it_can(tmp_path):
    other = paths.venv_python(tmp_path)
    other.parent.mkdir(parents=True)
    other.write_text("")
    assert paths.assets_python(tmp_path, has_unitypy=True) == sys.executable
    assert paths.assets_python(tmp_path, has_unitypy=False) == str(other)


def test_without_venv_assets_this_python_runs_them_anyway(tmp_path):
    assert paths.assets_python(tmp_path / "none", has_unitypy=False) == sys.executable


def test_the_python_option_runs_every_step(monkeypatch):
    monkeypatch.setattr(paths, "assets_python", lambda: "ASSETS")
    assert refresh_steps._assets_pyexe(argparse.Namespace(python="C:/py/python.exe")) == "C:/py/python.exe"
    assert refresh_steps._assets_pyexe(argparse.Namespace(python="")) == "ASSETS"


def _imports(path: Path) -> set[str]:
    """The top-level names a module imports anywhere (lazy imports too), except under
    `if __name__ == "__main__":`."""
    names: set[str] = set()

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if (
                isinstance(child, ast.If)
                and isinstance(child.test, ast.Compare)
                and isinstance(child.test.left, ast.Name)
                and child.test.left.id == "__name__"
            ):
                continue
            if isinstance(child, ast.Import):
                names.update(a.name.split(".")[0] for a in child.names)
            elif isinstance(child, ast.ImportFrom) and child.module and not child.level:
                names.add(child.module.split(".")[0])
            walk(child)

    walk(ast.parse(path.read_text("utf8")))
    return names


def _unitypy_modules() -> set[str]:
    """The refresh modules that reach UnityPy, directly or through another refresh module."""
    graph = {p.stem: _imports(p) for p in REFRESH.glob("*.py")}
    users = {m for m, names in graph.items() if "UnityPy" in names and m not in OPTIONAL}
    while True:
        more = {m for m, names in graph.items() if names & users and m not in OPTIONAL} - users
        if not more:
            return users
        users |= more


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        python="",
        force=False,
        account="main",
        merge_collection=True,
        account_only=False,
        dry_run_adds=False,
        connect_level="max",
    )


def test_every_step_that_reads_asset_bundles_runs_on_the_assets_python(monkeypatch):
    monkeypatch.setattr(paths, "assets_python", lambda: "ASSETS")
    users = _unitypy_modules()
    assert {"card_art_sync", "live2d_build", "chibi_build", "gacha_art", "park_map"} <= users
    assert "chart_download" not in users  # octo_crypto reads UnityPy only in its self-check
    routed, plain = set(), set()
    for step in refresh_steps.STEPS:
        cmd, _env, _cwd = step.build(_args())
        scripts = [Path(a).stem for a in cmd if a.endswith(".py")]
        if not scripts:
            continue  # the master step runs PowerShell
        (routed if scripts[0] in users else plain).add(step.name)
        expected = "ASSETS" if scripts[0] in users else sys.executable
        assert cmd[0] == expected, (step.name, scripts[0])
    assert {"jackets", "card_art", "live2d", "chibi", "event_art", "park_export"} <= routed
    assert {"account", "charts", "chart_lib", "board"} <= plain


def test_requirements_keep_unitypy_apart_for_the_pythons_it_supports():
    main = (ROOT / "requirements.txt").read_text("utf8")
    assets = (ROOT / "requirements-assets.txt").read_text("utf8")
    assert re.search(r"(?m)^-r requirements-assets\.txt$", main)
    assert not re.search(r"(?im)^unitypy", main)
    pin = re.search(r'(?m)^UnityPy==[\d.]+ ?; python_version < "(\d+)\.(\d+)"$', assets)
    assert pin, assets
    newest = max(run_local.ASSETS_PYTHONS)
    assert (int(pin[1]), int(pin[2])) == (newest[0], newest[1] + 1)
    assert run_local.needs_assets_env((newest[0], newest[1] + 1))
    assert not any(run_local.needs_assets_env(v) for v in run_local.ASSETS_PYTHONS)


# ---------------------------------------------------------------- the client version
def _no_unitypy():
    raise ModuleNotFoundError("No module named 'UnityPy'")


@pytest.fixture
def snapshot(monkeypatch, tmp_path):
    import account_snapshot as A

    monkeypatch.delenv("HOLODREAMS_APP_VERSION", raising=False)
    monkeypatch.setattr(A, "_app_version_cache", [])
    monkeypatch.setattr(A, "CLIENT_VERSION_FILE", tmp_path / "client_version.json")
    monkeypatch.setattr(A, "_install_versions", _no_unitypy)
    return A


def test_without_unitypy_the_client_version_is_read_by_venv_assets(snapshot, monkeypatch):
    asked = []
    monkeypatch.setattr(snapshot.paths, "assets_python", lambda: "C:/assets/python.exe")
    monkeypatch.setattr(snapshot, "_install_versions_with", lambda py: asked.append(py) or {"1.2.101"})
    assert snapshot.app_version() == "1.2.101"
    assert asked == ["C:/assets/python.exe"]


def test_without_any_asset_reader_the_published_version_is_used(snapshot, monkeypatch, tmp_path):
    monkeypatch.setattr(snapshot.paths, "assets_python", lambda: sys.executable)
    monkeypatch.setattr(snapshot, "_install_versions_with", lambda py: pytest.fail("asked itself"))
    (tmp_path / "client_version.json").write_text('{"version": "1.2.100"}')
    assert snapshot.app_version() == "1.2.100"


def test_a_failing_asset_reader_keeps_the_header_as_written(snapshot, monkeypatch):
    monkeypatch.setattr(snapshot.paths, "assets_python", lambda: "C:/assets/python.exe")

    def broken(_py):
        raise RuntimeError("exit 1")

    monkeypatch.setattr(snapshot, "_install_versions_with", broken)
    assert snapshot.app_version() is None


def test_the_other_interpreter_answers_with_the_versions_or_fails(monkeypatch):
    import account_snapshot as A

    monkeypatch.setenv("HB_NO_GAME", "1")  # no install: the child raises MissingInput
    with pytest.raises(subprocess.CalledProcessError):
        A._install_versions_with(sys.executable)
    monkeypatch.setattr(
        A.subprocess,
        "run",
        lambda cmd, **kw: type("R", (), {"stdout": 'noise\n["1.2.101"]\n'})(),
    )
    assert A._install_versions_with(sys.executable) == {"1.2.101"}


# ---------------------------------------------------------------- setup
def _fake_python(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


@pytest.fixture
def folder(monkeypatch, tmp_path):
    """run_local's .venv and .venv-assets in tmp_path; subprocess.run records each command and
    makes the venv a `-m venv` names."""
    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        cmd = [str(c) for c in cmd]
        calls.append(cmd)
        if cmd[1:3] == ["-m", "venv"]:
            _fake_python(paths.venv_python(Path(cmd[-1])))
        return type("R", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(run_local.paths, "VENV", tmp_path / ".venv")
    monkeypatch.setattr(run_local.paths, "VENV_ASSETS", tmp_path / ".venv-assets")
    monkeypatch.setattr(run_local.subprocess, "run", fake_run)
    monkeypatch.setattr(run_local, "processes_in", lambda folder: [])
    return tmp_path, calls


def _installs(calls) -> list[str]:
    """The folder each `pip install` went into."""
    return [Path(c[0]).parents[1].name for c in calls if c[1:4] == ["-m", "pip", "install"]]


def test_setup_on_315_makes_venv_assets_with_313(folder, monkeypatch):
    tmp_path, calls = folder
    base313 = str(tmp_path / "py313" / "python.exe")
    monkeypatch.setattr(run_local, "this_python", lambda: (3, 15))
    monkeypatch.setattr(run_local, "find_assets_python", lambda: base313)
    monkeypatch.setattr(run_local, "venv_version", lambda py: (3, 13) if "313" in str(py) else None)
    assert run_local.setup() == 0
    assert [base313, "-m", "venv", str(tmp_path / ".venv-assets")] in calls
    assert [sys.executable, "-m", "venv", str(tmp_path / ".venv")] in calls
    assert _installs(calls) == [".venv-assets", ".venv"]
    assert calls[-1][-1] == "check"


def test_setup_on_313_makes_no_venv_assets(folder, monkeypatch):
    tmp_path, calls = folder
    monkeypatch.setattr(run_local, "this_python", lambda: (3, 13))
    monkeypatch.setattr(run_local, "find_assets_python", lambda: pytest.fail("looked for one"))
    assert run_local.setup() == 0
    assert _installs(calls) == [".venv"]
    assert not (tmp_path / ".venv-assets").exists()


def test_setup_on_315_without_an_older_python_warns_and_goes_on(folder, monkeypatch, capsys):
    tmp_path, calls = folder
    monkeypatch.setattr(run_local, "this_python", lambda: (3, 15))
    monkeypatch.setattr(run_local, "find_assets_python", lambda: None)
    assert run_local.setup() == 0  # check() is what names it as blocking
    assert "No Python 3.13, 3.12, 3.14 on this PC" in capsys.readouterr().out
    assert _installs(calls) == [".venv"]


def test_a_313_venv_moves_to_315_and_the_old_one_goes(folder, monkeypatch):
    tmp_path, calls = folder
    old = _fake_python(paths.venv_python(tmp_path / ".venv"))
    old.with_name("marker").write_text("313")
    monkeypatch.setattr(run_local, "this_python", lambda: (3, 15))
    monkeypatch.setattr(run_local, "setup_assets", lambda: True)
    monkeypatch.setattr(run_local, "venv_version", lambda py: (3, 13))
    assert run_local.setup() == 0
    assert [sys.executable, "-m", "venv", str(tmp_path / ".venv")] in calls
    assert not old.with_name("marker").exists()  # a new .venv
    assert not (tmp_path / ".venv-previous").exists()
    assert calls[-1][-1] == "check"


def test_a_failed_move_puts_the_old_venv_back(folder, monkeypatch, capsys):
    tmp_path, calls = folder
    old = _fake_python(paths.venv_python(tmp_path / ".venv"))
    old.with_name("marker").write_text("313")
    monkeypatch.setattr(run_local, "this_python", lambda: (3, 15))
    monkeypatch.setattr(run_local, "setup_assets", lambda: True)
    monkeypatch.setattr(run_local, "venv_version", lambda py: (3, 13))
    monkeypatch.setattr(run_local, "install_packages", lambda py: False)
    assert run_local.setup() == 1
    assert old.with_name("marker").read_text() == "313"
    assert "back as it was (Python 3.13)" in capsys.readouterr().out


def test_a_venv_a_program_runs_from_is_not_moved(folder, monkeypatch, capsys):
    tmp_path, calls = folder
    old = _fake_python(paths.venv_python(tmp_path / ".venv"))
    old.with_name("marker").write_text("313")
    monkeypatch.setattr(run_local, "this_python", lambda: (3, 15))
    monkeypatch.setattr(run_local, "setup_assets", lambda: True)
    monkeypatch.setattr(run_local, "venv_version", lambda py: (3, 13))
    monkeypatch.setattr(run_local, "processes_in", lambda folder: [4242])
    assert run_local.setup() == 1
    assert old.with_name("marker").read_text() == "313"
    assert not (tmp_path / ".venv-previous").exists()
    assert not any(c[1:3] == ["-m", "venv"] for c in calls)
    assert "PID 4242" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform not in ("win32", "linux"), reason="Windows and Linux only")
def test_the_programs_running_from_a_folder_are_found(tmp_path):
    here = Path(sys.executable).parent
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        found = run_local.processes_in(here)
        assert child.pid in found
        assert run_local.processes_in(tmp_path) == []
    finally:
        child.kill()
        child.wait()


def test_a_313_venv_stays_when_315_would_lose_the_asset_reader(folder, monkeypatch, capsys):
    tmp_path, calls = folder
    old = _fake_python(paths.venv_python(tmp_path / ".venv"))
    monkeypatch.setattr(run_local, "this_python", lambda: (3, 15))
    monkeypatch.setattr(run_local, "setup_assets", lambda: False)
    monkeypatch.setattr(run_local, "venv_version", lambda py: (3, 13))
    assert run_local.setup() == 0
    assert not any(c[1:3] == ["-m", "venv"] for c in calls)
    assert _installs(calls) == [".venv"] and calls[-1][0] == str(old)
    assert ".venv stays on Python 3.13" in capsys.readouterr().out


def test_update_runs_setup_from_its_own_venv_without_moving_it(folder, monkeypatch):
    tmp_path, calls = folder
    _fake_python(paths.venv_python(tmp_path / ".venv"))
    _fake_python(paths.venv_python(tmp_path / ".venv-assets"))
    monkeypatch.setattr(run_local, "this_python", lambda: (3, 15))
    monkeypatch.setattr(run_local, "venv_version", lambda py: (3, 13) if "assets" in str(py) else (3, 15))
    assert run_local.setup() == 0
    assert not any(c[1:3] == ["-m", "venv"] for c in calls)
    assert _installs(calls) == [".venv-assets", ".venv"]


def test_venv_assets_on_a_wrong_python_is_made_again(folder, monkeypatch):
    tmp_path, calls = folder
    _fake_python(paths.venv_python(tmp_path / ".venv-assets"))
    base = str(tmp_path / "py313" / "python.exe")
    monkeypatch.setattr(run_local, "find_assets_python", lambda: base)
    monkeypatch.setattr(run_local, "venv_version", lambda py: (3, 13) if "py313" in str(py) else (3, 15))
    monkeypatch.setattr(run_local, "this_python", lambda: (3, 15))
    assert run_local.setup_assets() is True
    assert [base, "-m", "venv", "--clear", str(tmp_path / ".venv-assets")] in calls


def test_the_older_python_is_found_through_the_py_launcher_then_the_path(monkeypatch):
    monkeypatch.setattr(run_local, "WINDOWS", True)
    seen = []

    def fake_run(cmd, **_kw):
        seen.append(cmd[1])
        ok = cmd[1] == "-3.12"
        return type(
            "R", (), {"returncode": 0 if ok else 1, "stdout": "C:/Py312/python.exe\n" if ok else ""}
        )()

    monkeypatch.setattr(run_local.subprocess, "run", fake_run)
    monkeypatch.setattr(run_local, "venv_version", lambda py: (3, 12) if "312" in str(py) else None)
    py_only = {"py": "C:/Windows/py.exe"}
    assert run_local.find_assets_python(which=py_only.get) == "C:/Py312/python.exe"
    assert seen == ["-3.13", "-3.12"]
    monkeypatch.setattr(run_local, "WINDOWS", False)
    on_path = {"python3.14": "/usr/bin/python3.14"}
    monkeypatch.setattr(run_local, "venv_version", lambda py: (3, 14) if "3.14" in str(py) else None)
    assert run_local.find_assets_python(which=on_path.get) == "/usr/bin/python3.14"


def test_check_names_where_unitypy_is(monkeypatch):
    monkeypatch.setattr(run_local.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(run_local.paths, "assets_python", lambda: sys.executable)
    monkeypatch.setattr(run_local, "this_python", lambda: (3, 15))
    ok, _what, _detail, fix = run_local.asset_reader()
    assert ok is False and "install Python 3.13" in fix
    monkeypatch.setattr(run_local.paths, "assets_python", lambda: "C:/x/.venv-assets/python.exe")
    monkeypatch.setattr(run_local.subprocess, "run", lambda cmd, **kw: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(run_local, "venv_version", lambda py: (3, 13))
    ok, _what, detail, _fix = run_local.asset_reader()
    assert ok is True and detail == "UnityPy in .venv-assets (Python 3.13)"


def test_update_tells_an_older_venv_how_to_move_to_315(monkeypatch):
    monkeypatch.setattr(run_local, "this_python", lambda: (3, 13))
    tip = run_local.faster_python_tip(find=lambda: "C:/Py315/python.exe")
    assert "Python 3.15 is installed on this PC" in tip and "setup" in tip
    tip = run_local.faster_python_tip(find=lambda: None)
    assert "Install it (python.org) beside Python 3.13" in tip
    monkeypatch.setattr(run_local, "this_python", lambda: (3, 15))
    assert run_local.faster_python_tip(find=lambda: pytest.fail("looked")) is None


def test_setup_ends_with_the_tip_for_an_older_venv(folder, monkeypatch, capsys):
    monkeypatch.setattr(run_local, "this_python", lambda: (3, 13))
    monkeypatch.setattr(run_local, "faster_python_tip", lambda: "  Python 3.15 is installed on this PC")
    assert run_local.setup() == 0
    assert capsys.readouterr().out.rstrip().endswith("Python 3.15 is installed on this PC")


def test_update_runs_setup_when_the_assets_requirements_change():
    assert "requirements-assets.txt" in run_local.PACKAGES_FILES


# ---------------------------------------------------------------- the web
def test_the_web_gacha_sync_runs_on_the_assets_python(monkeypatch):
    sys.path.insert(0, str(ROOT / "tools" / "web"))
    from hbweb import gacha

    seen = {}
    monkeypatch.setattr(gacha.paths, "assets_python", lambda: "ASSETS")
    monkeypatch.setattr(
        gacha.scripts, "run_recon_script", lambda h, script, tag, **kw: seen.update(kw) or (0, "")
    )
    gacha.sync_after_refresh(object(), 0)
    assert seen == {"python": "ASSETS"}
