"""The `--ceiling` preset and its `all_invited` scope rule.

`--ceiling` reproduces the ceiling toggles of the community tool holodori.best's
Board Optimizer so the two tools can be compared on the same footing: bare boards
(respec), infinite
cubes/hexas, max board points (361) for EVERY holomem, and "all invited" -- the
account-global GREEN/YELLOW branches searched on every board, not just the team's
six. RED/BLUE stay leader/character-scoped, so off-team they are worth exactly 0
and are never searched there.

`_move_in_scope` is that rule as one pure predicate, shared by `cmd_plan` and
`_plan_for_team`'s all-invited pass, so
the two cannot drift. It is tested here directly -- no dumps, no Oracle, no
1-2 minute plan -- because the scope rule is exactly the thing the all-invited
extension turns on and it must not silently regress to team-only.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PROXY = Path(__file__).resolve().parent
_MODULE = _PROXY / "tree_optimise.py"


def _load():
    spec = importlib.util.spec_from_file_location("tree_optimise", _MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["tree_optimise"] = module
    spec.loader.exec_module(module)
    return module


def test_off_team_allows_account_global_green_and_yellow():
    """Off a team, only the account-global branches (G/Y) may be bought.

    This is the whole of 'all invited': a green or yellow node pays from ANY
    board (G-001/Y-004 measured at +176.8/+561.6 on off-team characters), so
    the ceiling search must reach them on every holomem,
    not only the five members plus leader."""
    to = _load()
    for target in ("G-001", "G-006", "Y-004", "Y-018"):
        assert to._move_in_scope(target, on_team=False), (
            f"{target} is account-global and must be searchable off-team"
        )


def test_off_team_excludes_leader_and_character_scoped_red_blue():
    """Off a team, RED (leader-only) and BLUE (that character's own cards only)
    are worth exactly 0, so searching them on 34 boards is pure waste and would
    also pollute the plan with worthless purchases. They must be excluded."""
    to = _load()
    for target in ("R-001", "R-032", "B-001", "B-030"):
        assert not to._move_in_scope(target, on_team=False), (
            f"{target} is character/leader-scoped and worth 0 off-team; it must "
            "not be searched there"
        )


def test_on_team_allows_red_blue_green_and_yellow_only_where_it_pays():
    """On one of the team's own characters red, blue and green are in scope. Yellow
    goes where it pays: in general on the bench (the account-global cap is covered by
    bench points red/blue never use), on a song only on its singers."""
    to = _load()
    for target in ("R-001", "B-001", "G-001"):
        assert to._move_in_scope(target, on_team=True), (
            f"{target} must be in scope on a team character"
        )
    assert not to._move_in_scope("Y-004", on_team=True)
    assert to._move_in_scope("Y-004", on_team=True, song_mode=True, is_singer=True)
    assert not to._move_in_scope("Y-004", on_team=True, song_mode=True, is_singer=False)
