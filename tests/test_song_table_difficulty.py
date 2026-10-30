"""The header must name the difficulty it actually scored (Path B step 7).

`format_song_basis` hard-coded "EXPERT" because EXPERT was the only difficulty
that could be built. With `charts_all.json` on disk `--difficulty EASY` really
scores EASY, and a header that still says EXPERT is the exact failure this
project is organised against: reporting one number while showing another.
"""

from __future__ import annotations

from hbuilder.domain.song_rank import RankedOnSongs
from hbuilder.output.song_table import format_song_basis


def _ranking(difficulty: str, count: int = 3) -> RankedOnSongs:
    return RankedOnSongs(
        teams=(),
        charts=tuple(f"m000{i}" for i in range(1, count + 1)),
        filtered_from=8,
        difficulty=difficulty,
    )


def test_the_multi_chart_header_names_the_difficulty_that_was_scored():
    assert "3 EASY charts" in format_song_basis(_ranking("EASY"))[0]
    assert "EXPERT" not in format_song_basis(_ranking("EASY"))[0]
    assert "3 EXPERT charts" in format_song_basis(_ranking("EXPERT"))[0]


def test_the_single_chart_header_names_the_difficulty_too():
    line = format_song_basis(_ranking("HARD", count=1))[0]
    assert "m0001" in line and "HARD" in line


def test_the_difficulty_defaults_to_expert_for_a_hand_built_ranking():
    """Every existing caller builds `RankedOnSongs` without naming one, and
    EXPERT is what they all scored."""
    assert RankedOnSongs(teams=(), charts=(), filtered_from=0).difficulty == "EXPERT"
