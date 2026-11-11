"""The branch-scope rule `_move_in_scope`, which "all invited" planning relies on.

"All invited" searches the account-global GREEN/YELLOW branches on every board, not just
the team's six; RED/BLUE stay leader/character-scoped, so off-team they are worth exactly 0
and are never searched there. Tested directly: no dumps, no Oracle, no plan.
"""

from __future__ import annotations

import tree_optimise as to


def test_off_team_allows_account_global_green_and_yellow():
    """Off a team, only the account-global branches (G/Y) may be bought.

    A green or yellow node pays from ANY board (G-001/Y-004 at +176.8/+561.6 on
    off-team characters), so the search must reach them on every holomem."""
    for target in ("G-001", "G-006", "Y-004", "Y-018"):
        assert to._move_in_scope(target, on_team=False), (
            f"{target} is account-global and must be searchable off-team"
        )


def test_off_team_excludes_leader_and_character_scoped_red_blue():
    """Off a team, RED (leader-only) and BLUE (that character's own cards only)
    are worth exactly 0, so they are excluded."""
    for target in ("R-001", "R-032", "B-001", "B-030"):
        assert not to._move_in_scope(target, on_team=False), (
            f"{target} is character/leader-scoped and worth 0 off-team; it must not be searched there"
        )


def test_on_team_allows_red_blue_green_and_yellow_only_where_it_pays():
    """On one of the team's own characters red, blue and green are in scope. Yellow
    goes where it pays: in general on the bench (the account-global cap is covered by
    bench points red/blue never use), on a song only on its singers."""
    for target in ("R-001", "B-001", "G-001"):
        assert to._move_in_scope(target, on_team=True), f"{target} must be in scope on a team character"
    assert not to._move_in_scope("Y-004", on_team=True)
    assert to._move_in_scope("Y-004", on_team=True, song_mode=True, is_singer=True)
    assert not to._move_in_scope("Y-004", on_team=True, song_mode=True, is_singer=False)
