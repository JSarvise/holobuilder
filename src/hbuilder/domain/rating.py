"""Holomem Rating: the per-holomem high-score leaderboard.

The game keeps, per account, the highest score of every (holomem, song, difficulty) a live
was played for (`UserMusicCharacterHighestScore`). Each record is worth a song rating of
`ceil(score / RATING_DENOMINATOR)`. A holomem's rating adds the best difficulty of each of
her songs and keeps her best `AGGREGATION_MUSIC_COUNT` songs; the account's total is the sum
over every holomem. A weekly ranking per holomem orders the players by that rating.

Which songs count for a holomem: a song the master flags `isHighestScoreRatingTarget`, with
her in its cast, that is not an all-member song (those carry no flag). A play is credited to
one holomem of the cast, the leader of the deck, so the optimizer pins the leader.

Stdlib only: the solver package imports none of the runtime packages.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

# Master `Setting`: `music_highest_score_rating_aggregation_music_count`. The denominator
# (`music_highest_score_rating_value_denominator`) is absent from the rendered row; 5000 is
# the value every record of the account agrees with (`highest_score_rating_value`).
RATING_DENOMINATOR: int = 5000
AGGREGATION_MUSIC_COUNT: int = 3

# The order of the difficulties, easiest first (a tie on level keeps the harder one).
DIFFICULTY_ORDER: tuple[str, ...] = ("EASY", "NORMAL", "HARD", "EXPERT", "MASTER")


def song_rating(score: float) -> int:
    """The song rating a run's score is worth: `ceil(score / 5000)`, 0 for no score."""
    if score <= 0:
        return 0
    return math.ceil(score / RATING_DENOMINATOR)


@dataclass(frozen=True)
class RatingSong:
    """What decides whether a song counts for a holomem's rating."""

    music_id: str
    singers: tuple[str, ...]
    singer_type: str  # "solo" / "group" / "all" (the master's MusicSingerType, lower case)
    rating_target: bool  # Music.isHighestScoreRatingTarget
    start_ms: int = 0  # Music.startTime: a song not released yet counts for nobody


def counts_for(song: RatingSong, chr_id: str, now_ms: int | None = None) -> bool:
    """Whether a run of `song` led by `chr_id` adds to `chr_id`'s rating."""
    if not song.rating_target or chr_id not in song.singers:
        return False
    if now_ms is not None and song.start_ms > now_ms:
        return False
    return song.singer_type.lower() in ("solo", "group")


def best_per_song(records: Iterable[tuple[str, str, int]]) -> dict[str, int]:
    """`{music_id: rating}` from `(music_id, difficulty, rating)` records: each song at its
    best difficulty."""
    out: dict[str, int] = {}
    for music_id, _difficulty, rating in records:
        if rating > out.get(music_id, 0):
            out[music_id] = rating
        else:
            out.setdefault(music_id, rating)
    return out


def top_songs(per_song: Mapping[str, int], count: int = AGGREGATION_MUSIC_COUNT) -> list[tuple[str, int]]:
    """The songs the rating adds, best first (a tie keeps the lower music id first)."""
    ranked = sorted(per_song.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:count]


def holomem_rating(per_song: Mapping[str, int]) -> int:
    """A holomem's rating: her best AGGREGATION_MUSIC_COUNT song ratings added."""
    return sum(r for _m, r in top_songs(per_song))


def rating_with(current: Mapping[str, int], new: Mapping[str, int]) -> int:
    """The holomem's rating once the runs in `new` are played: a record only ever rises, so
    each song keeps the higher of its current and its new rating."""
    merged = dict(current)
    for music_id, rating in new.items():
        if rating > merged.get(music_id, 0):
            merged[music_id] = rating
    return holomem_rating(merged)


def gain(current: Mapping[str, int], music_id: str, rating: int) -> int:
    """What one new run of `music_id` at `rating` adds to the holomem's rating."""
    return rating_with(current, {music_id: rating}) - holomem_rating(current)


def needed_score(rating: int) -> int:
    """The lowest score worth `rating`: one point above the previous step."""
    if rating <= 0:
        return 0
    return (rating - 1) * RATING_DENOMINATOR + 1


def pick_chart(levels: Mapping[str, int], max_level: int) -> str | None:
    """The difficulty to play a song at, given `{difficulty: level}` and the highest level
    the player clears: the highest level within reach (the song's coefficient raises a note's
    value with the level), a tie going to the harder difficulty. None when every chart is
    above `max_level`."""
    within = [(lv, DIFFICULTY_ORDER.index(d) if d in DIFFICULTY_ORDER else -1, d) for d, lv in levels.items()]
    within = [w for w in within if w[0] <= max_level]
    if not within:
        return None
    return max(within)[2]


# A rating step must outweigh any difference in the mean score (a few million at most), and
# the mean must still separate teams of the same rating: the search needs a slope between the
# rating's steps, which are 5000 points apart.
MERGED_RATING_SCALE: float = 1e7
MERGED_MEAN_WEIGHT: float = 0.1


def merged_value(
    scores: Sequence[float],
    music_ids: Sequence[str],
    current: Mapping[str, int] | None = None,
) -> float:
    """The objective of one team for several songs at once: the holomem's rating once each
    song is played with it (`rating_with`, from the current records), then the mean score as
    the tie-break between teams of the same rating."""
    if len(scores) != len(music_ids):
        raise ValueError("one score per song")
    if not scores:
        return 0.0
    new: dict[str, int] = {}
    for music_id, score in zip(music_ids, scores):
        r = song_rating(score)
        if r > new.get(music_id, 0):
            new[music_id] = r
    total = rating_with(current or {}, new)
    return MERGED_RATING_SCALE * total + MERGED_MEAN_WEIGHT * (sum(scores) / len(scores))


def merged_rating(value: float) -> int:
    """The rating a `merged_value` carries."""
    return int(value // MERGED_RATING_SCALE)
