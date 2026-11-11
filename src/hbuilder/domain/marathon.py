"""Marathon events: the bonus rows, Event Points and the event search objectives.

Event points are NOT the deck-screen Unit Score the main optimiser ranks by. They are
driven by **event bonus rows** — per-event server data (holomem / Member-card /
rarity+bloom / song rows) that ADD a percentage to the event score for every card in the
deck that matches. Their matching rule is read from the client code (see
`MarathonBonusRow.matches`), and the stamina→provide-count curve from the master data.

Nothing here is imported by the solver's hot loop; it is a separate objective over the same
`Card` team. Third-party deps: none (stdlib only).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from hbuilder.domain.card import Card
from hbuilder.domain.schema import SchemaError

# --- Master constants (Setting table) ----------------------------------------
#
# Values from the master `Setting` row and the Marathon*RewardUpStaminaEffect tables
# (field numbers: the proto tags of `master_setting.gen.proto` in the dumps). Literals,
# so a master update that changes them is a visible diff, not a silent drift.

# Setting 1118 / 1121 / 1301.
LIVE_STAMINA_MAX: int = 10

# MarathonLiveRewardUpStaminaEffect: stamina consumed -> marathon_score_provide_count.
# Linear ×8 through stamina 3, sub-linear after.
MARATHON_LIVE_STAMINA_SCORE: dict[int, int] = {
    1: 8,
    2: 16,
    3: 24,
    4: 30,
    5: 37,
    6: 42,
    7: 46,
    8: 50,
    9: 53,
    10: 56,
}

# The channels an event's bonus groups are keyed on (marathon_bonus.json `kind`). A live play
# draws live + music (song-gated) rows; a minigame play draws minigame rows.
LIVE_CHANNEL = "live"
MINIGAME_CHANNEL = "minigame"
MUSIC_CHANNEL = "music"


def _chr_id(card: Card) -> str:
    """The holomem id a card belongs to, e.g. 'chr-00001' from 'card-00001-3-nrml-0000-00'."""
    parts = card.id.split("-")
    return f"chr-{parts[1]}" if len(parts) > 1 else card.id


def _rarity_int(card: Card) -> int | None:
    """The wire rarity (literally 3/4/5, the star count) from a '3★'-style rarity string."""
    head = card.rarity[:1]
    return int(head) if head.isdigit() else None


@dataclass(frozen=True, slots=True)
class MarathonBonusRow:
    """One event bonus row — a PATTERN whose unset fields are WILDCARDS (as in the client's
    CardMarathonLiveEventBonusRatioCache::CalculateBonusRatioPermil). A row
    fires on a card when every SET field matches; the permil of every matching row SUMS.

    Exactly one of the identifying shapes is populated per row (seen in the wire data):
      - character_id only              -> any card of that holomem
      - card_id only                   -> that specific card
      - rarity + bloom                 -> every card at that rarity AND owned bloom
      - card_id + music_id (music kind)-> that card, only for that song
    """

    permil: int
    character_id: str | None = None
    card_id: str | None = None
    rarity: int | None = None
    bloom: int | None = None
    music_id: str | None = None
    # cardAttributeType (+0x38 in the client row, 0 = wildcard): "Cute" / "Pure" / "Happy".
    attribute: str | None = None

    def matches(self, card: Card, *, bloom: int | None, music_id: str | None) -> bool:
        # A song-gated (music) row only applies when that song is the one being played.
        if self.music_id is not None and self.music_id != music_id:
            return False
        if self.card_id is not None and card.id != self.card_id:
            return False
        if self.attribute is not None and card.type != self.attribute:
            return False
        if self.character_id is not None and _chr_id(card) != self.character_id:
            return False
        if self.rarity is not None and _rarity_int(card) != self.rarity:
            return False
        if self.bloom is not None:
            # A bloom row needs the card's OWNED bloom (collection state, not on the
            # catalogue Card). Without it the row cannot match.
            if bloom is None or bloom != self.bloom:
                return False
        return True


@dataclass(frozen=True, slots=True)
class MarathonEvent:
    """One event's bonus data, grouped by channel. Per-event data (the game sends it on
    the wire, not in master), loaded from data/marathon.json and never written by an
    automated path."""

    name: str
    rows_by_channel: Mapping[str, tuple[MarathonBonusRow, ...]]

    def rows_for(self, channel: str) -> tuple[MarathonBonusRow, ...]:
        return self.rows_by_channel.get(channel, ())


def live_stamina_score(stamina: int) -> int:
    """marathon_score_provide_count for a LIVE play at `stamina` consumed (master table)."""
    if stamina not in MARATHON_LIVE_STAMINA_SCORE:
        raise ValueError(f"live stamina {stamina} out of range 1..{LIVE_STAMINA_MAX}")
    return MARATHON_LIVE_STAMINA_SCORE[stamina]


# --- The three event objectives -----------------------------------------------
#
# What the game does (client code): the live score carries the Song Bonus (yellow) and the
# leader-sings reds INSIDE each note's product; the event bonus is applied afterwards by the
# server. Event Pt rows SUM across the members (deck screen `Enumerable.Sum` @0x6571a74),
# Event Song Score rows take the MAX card (`Enumerable.Max` @0x658f441). The leader slot adds
# nothing.


def _card_permil(
    card: Card, rows: Iterable[MarathonBonusRow], *, bloom: int | None, music_id: str | None
) -> int:
    """One card's bonus: the sum of every matching row (`CalculateBonusRatioPermil`
    @0x614b5e0). A bloom row needs the owned bloom and compares with == (never >=)."""
    total = 0
    for row in rows:
        if row.bloom is not None and bloom is None:
            continue
        if row.matches(card, bloom=bloom, music_id=music_id):
            total += row.permil
    return total


def _ceil_div(num: int, den: int) -> int:
    return -(-num // den)


# The room's MULTI SCORE divisor by event kind, fitted to multi Live results: a Spotlight
# (relay) event 75000, a Point Rally (normal) event 60000. The normal value may instead be
# "15000 x the players in the room": the results it is fitted to do not tell them apart.
MULTI_DIVISOR = {"relay": 75_000, "normal": 60_000}


def multi_divisor(event_key: str | None) -> int:
    """`MULTI_DIVISOR` of an event key (`marathon_event-normal-008-chapter-01` -> 60000);
    an unknown kind keeps the relay value."""
    kind = next((k for k in MULTI_DIVISOR if f"-{k}-" in f"-{event_key or ''}-"), "relay")
    return MULTI_DIVISOR[kind]


def event_points_base(
    score: int, bonus_permil: int, multi_score: int | None = None, divisor: int = 75_000
) -> int:
    """`ceil((120 + score/15000 + multi/divisor) * (1 + E/1000))` in exact integers
    (`divisor`: `multi_divisor(event)`, 75000 on a relay event, 60000 on a normal one).

    Solo (`multi_score` None or 0): `120 + score/15000`, exact on observed solo runs. Multi:
    the room's MULTI SCORE (as the result screen shows it, your own score included) adds
    `multi/divisor` -- on a relay event 75000 = 15000 x 5 players, exact on observed multi
    Live/Finish results. Floats would turn (120 + 1.5M/15000) * 1.3 into
    286.00000000000006 and ceil it to 287."""
    # x divisor (a multiple of 15000): 120 + score/15000 + multi/divisor, all over divisor
    k = int(divisor) // 15_000
    total = 120 * int(divisor) + k * int(score) + int(multi_score or 0)
    return _ceil_div(total * (1000 + int(bonus_permil)), int(divisor) * 1000)


def event_points(
    score: int | None,
    bonus_permil: int | None,
    *,
    boosts: int,
    length_permil: int,
    holopass: bool,
    base: int | None = None,
    multi_score: int | None = None,
    divisor: int = 75_000,
) -> int:
    """Event Pt for one play: `ceil(base * boost_count * L * (1 + HoloPass))`.

    `boosts` 1..10 picks the provide count (8, 16, ... 56). `length_permil` is the
    song-length multiply the server sends (e.g. 1480): server-only and not known in general,
    so the caller supplies it. `multi_score`: the room's MULTI SCORE for a multi Live
    (`event_points_base`). Exact on observed solo and multi Live/Finish results."""
    if base is None:
        if score is None or bonus_permil is None:
            raise ValueError("event_points needs either `base` or both `score` and `bonus_permil`")
        base = event_points_base(int(score), int(bonus_permil), multi_score, divisor)
    provide = live_stamina_score(boosts)
    return _ceil_div(base * provide * int(length_permil) * (2 if holopass else 1), 1000)


def event_song_score(score: int, song_bonus_permil: int) -> int:
    """The event score of a play: the server's bonus on the WHOLE reported score.
    ceil matches the community tool holodori.best; the server's rounding is not known."""
    return _ceil_div(int(score) * (1000 + int(song_bonus_permil)), 1000)


OBJECTIVES = ("score", "event_points", "event_song")


def card_bonus_table(
    cards: Iterable[Card], event: MarathonEvent, *, blooms: Mapping[str, int | None], music_id: str | None
) -> tuple[tuple[int, int], ...]:
    """Per card, `(live permil, song permil)`: the two numbers a team's `E` (sum over the
    members) and `P` (max over the members) are made of. A search needs them per card,
    once, so they can be added up inside its loop."""
    live, music = event.rows_for(LIVE_CHANNEL), event.rows_for(MUSIC_CHANNEL)
    return tuple(
        (
            _card_permil(c, live, bloom=blooms.get(c.id), music_id=None),
            _card_permil(c, music, bloom=blooms.get(c.id), music_id=music_id) if music_id else 0,
        )
        for c in cards
    )


def event_objective(score: float, *, objective: str, live_permil: int = 0, song_permil: int = 0) -> float:
    """The value a search ranks teams by — continuous, without the game's ceilings, so ties
    do not hide differences. Report with `event_points_base` / `event_song_score`.

    - `score`: the score itself.
    - `event_points`: `(120 + S/15000)(1 + E/1000)`, `E` = members' live rows summed. Boosts,
      song length and HoloPass multiply every team alike and drop out of the ranking.
    - `event_song`: `S(1 + P/1000)`, `P` = the largest member's song rows.

    Event Points do NOT take `P`: whether the song bonus lifts Event Pt is not known."""
    if objective == "score":
        return float(score)
    if objective == "event_points":
        return (120.0 + float(score) / 15000.0) * (1.0 + live_permil / 1000.0)
    if objective == "event_song":
        return float(score) * (1.0 + song_permil / 1000.0)
    raise ValueError(f"unknown objective {objective!r}; one of {OBJECTIVES}")


# --- The open event's bonus groups (marathon_bonus.json) ----------------------

_KIND_CHANNEL = {"live": LIVE_CHANNEL, "minigame": MINIGAME_CHANNEL, "music": MUSIC_CHANNEL}


def _event_key(group_id: str) -> str:
    """'live_marathon_score_bonus-marathon_event-normal-007-chapter-01' -> 'marathon_event-normal-007-chapter-01'."""
    at = group_id.find("marathon_event-")
    return group_id[at:] if at >= 0 else group_id


def pick_chapter(
    keys: Iterable[str], windows: Mapping[str, Sequence[int]] | None = None, now_ms: int | None = None
) -> str:
    """The default chapter among the open `keys`: the one whose `(start_ms, end_ms)` window
    holds `now_ms`, else the latest that has started, else the last key. A Spotlight (relay)
    event lists ALL its chapters at once (e.g. four chapters, one holomem each, two days
    apiece), so "the newest key" would pick a future chapter instead of the one being played."""
    keys = sorted(keys)
    if not keys:
        raise SchemaError("no event chapter to pick from")
    if windows and now_ms is not None:
        live = [k for k in keys if k in windows and windows[k][0] <= now_ms <= windows[k][1]]
        if live:
            return live[-1]
        started = [k for k in keys if k in windows and windows[k][0] <= now_ms]
        if started:
            return max(started, key=lambda k: windows[k][0])
    return keys[-1]
