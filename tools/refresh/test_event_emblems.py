"""Which ranking medals the refresh fetches (`event_emblems.wanted`)."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import event_emblems as ee  # noqa: E402

CH = "marathon_event-normal-009-chapter-01"


def test_a_chapter_gets_its_ranking_medal_and_each_song_its_own():
    assets = {
        f"emblem-total_music_highest_score_reward-{CH}-diamond": "a-total",
        f"emblem-music_highest_score_ranking_rank_reward-{CH}-m0394-diamond": "a-song",
        f"emblem-music_highest_score_ranking_rank_reward-{CH}-m0394-nope": "a-bad-grade",
        "emblem-music_highest_score_ranking_rank_reward-marathon_event-normal-077-chapter-01-m0001-gold": "a-other",
    }
    got = ee.wanted({CH: "total"}, assets, ["diamond", "gold"])
    assert got == {f"emblem-{CH}-diamond.webp": "a-total", f"emblem-{CH}-m0394-diamond.webp": "a-song"}
