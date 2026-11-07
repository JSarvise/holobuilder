"""Whole-deck event-bonus sums over the product's row matcher.

The product works per card: `marathon.card_bonus_table` and `marathon._card_permil`
give each card's live and song permil, and the searches add them up inside their own
loops (Event Pt rows SUM over the five members, Event Song Score rows take the MAX
card). These functions do the same deck-level arithmetic in one call, so the tests
can pin the matching rules and the two laws on small hand-built decks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from hbuilder.domain.card import Card
from hbuilder.domain.marathon import (
    LIVE_CHANNEL,
    MINIGAME_CHANNEL,
    MUSIC_CHANNEL,
    MarathonEvent,
    _card_permil,
)

CHANNELS: tuple[str, ...] = (LIVE_CHANNEL, MINIGAME_CHANNEL, MUSIC_CHANNEL)


@dataclass(frozen=True, slots=True)
class EventBonusResult:
    total_permil: int
    per_card_permil: tuple[int, ...]
    bloom_rows_skipped: int  # bloom rows that could not be evaluated for want of owned bloom


def event_bonus_permil(
    cards: Iterable[Card],
    event: MarathonEvent,
    *,
    channel: str = LIVE_CHANNEL,
    blooms: Mapping[str, int] | None = None,
    music_id: str | None = None,
) -> EventBonusResult:
    """The deck's total event-bonus permil for one play.

    For each card, SUM the permil of every matching row (rows are wildcard patterns and
    all matches sum); the deck total sums those per-card contributions. `channel` selects
    which bonus groups apply: a live play draws the live rows plus the song-gated music
    rows; a minigame play draws the minigame rows.

    `blooms` maps card.id -> owned bloom, needed only for `{rarity, bloom}` rows; when a bloom
    row cannot be evaluated it is counted in `bloom_rows_skipped` rather than silently missed.
    """
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}; expected one of {CHANNELS}")
    rows = list(event.rows_for(channel))
    # A live play also earns the song-specific "Event Score Up" music rows.
    if channel == LIVE_CHANNEL:
        rows += list(event.rows_for(MUSIC_CHANNEL))

    blooms = blooms or {}
    per_card: list[int] = []
    skipped = 0
    for card in cards:
        card_bloom = blooms.get(card.id)
        card_permil = 0
        for row in rows:
            if row.bloom is not None and card_bloom is None:
                skipped += 1
                continue
            if row.matches(card, bloom=card_bloom, music_id=music_id):
                card_permil += row.permil
        per_card.append(card_permil)
    return EventBonusResult(
        total_permil=sum(per_card),
        per_card_permil=tuple(per_card),
        bloom_rows_skipped=skipped,
    )


def event_point_bonus_permil(members: Iterable[Card], event: MarathonEvent, *,
                             blooms: Mapping[str, int]) -> EventBonusResult:
    """`E` for Event Points: the LIVE rows only, summed per card, summed over the members.
    Pass the five members, never the leader."""
    rows = event.rows_for(LIVE_CHANNEL)
    per_card = []
    skipped = 0
    for card in members:
        bloom = blooms.get(card.id)
        skipped += sum(1 for r in rows if r.bloom is not None and bloom is None)
        per_card.append(_card_permil(card, rows, bloom=bloom, music_id=None))
    return EventBonusResult(total_permil=sum(per_card), per_card_permil=tuple(per_card),
                            bloom_rows_skipped=skipped)


def event_song_bonus_permil(members: Iterable[Card], event: MarathonEvent, *, music_id: str,
                            blooms: Mapping[str, int]) -> int:
    """`P` for Event Song Score: the song-gated rows, summed per card, then the LARGEST card."""
    rows = event.rows_for(MUSIC_CHANNEL)
    return max((_card_permil(c, rows, bloom=blooms.get(c.id), music_id=music_id)
                for c in members), default=0)
