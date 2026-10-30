"""Marathon event-farming valuation.

Event points are NOT the deck-screen Unit Score the main optimiser ranks by. They are
dominated by two things a stat-max deck ignores:

  1. **Event bonus rows** — per-event server data (holomem / Member-card / rarity+bloom /
     song rows) that ADD a percentage to the event score for every card in the deck that
     matches. This is the deck lever that actually re-orders farming decks, and its matching
     rule is read from the client code (see `event_bonus_permil`).
  2. A flat ×2.0 for using the recommended deck, and a stamina→points curve — both
     deck-INDEPENDENT scalars (they scale the absolute yield but never re-order two decks),
     so they belong to absolute-points reporting, not the ranking objective.

The bonus-row matcher and the master stamina/multiplier constants are read from the client
and master data. The absolute-points COMPOSITION (multiply order, base score) is inferred,
not read, and deliberately NOT part of the ranking objective — `points_scalar` exposes the
deck-independent multiplier for reporting, clearly separated from it.

Nothing here is imported by the solver's hot loop; it is a separate objective over the same
`Card` team. Third-party deps: none (stdlib only).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from hbuilder.domain.card import Card
from hbuilder.domain.schema import SchemaError

# --- Master constants (Setting table) ----------------------------------------
#
# Each is a single value read from the account's own `Master/Get` Setting row and the
# Marathon*RewardUpStaminaEffect tables. Field numbers are the proto tags in
# tools/recon/proxy/dumps/holodori/master_setting.gen.proto. Recorded as literals with their
# provenance so a master update that changes them is a visible diff, not a silent drift.

# Setting field 2402 = 2000 permil -> ×2.0 for playing the game's recommended deck.
MARATHON_RECOMMEND_DECK_MULTIPLIER: float = 2.0
# Setting 1118 / 1121 / 1301.
LIVE_STAMINA_MAX: int = 10
LIVE_STAMINA_RECOMMEND_CONSUMPTION: int = 3  # the efficiency knee (linear ×8/stamina through 3)
MINIGAME_STAMINA_MAX: int = 100

# MarathonLiveRewardUpStaminaEffect: stamina consumed -> marathon_score_provide_count.
# Linear ×8 through stamina 3, sub-linear after.
MARATHON_LIVE_STAMINA_SCORE: dict[int, int] = {
    1: 8, 2: 16, 3: 24, 4: 30, 5: 37, 6: 42, 7: 46, 8: 50, 9: 53, 10: 56,
}
# MarathonMiniGameRewardUpStaminaEffect: {mini_game_type: {stamina: provide_count}}.
# The per-type curves differ; only the shipped types are present.
MARATHON_MINIGAME_STAMINA_SCORE: dict[int, dict[int, int]] = {
    1: {1: 6, 2: 12, 3: 17, 4: 22, 5: 26, 6: 30},
    3: {1: 6, 2: 11, 3: 15},
}

# The channels an event's bonus groups are keyed on (marathon_bonus.json `kind`). A live play
# draws live + music (song-gated) rows; a minigame play draws minigame rows.
LIVE_CHANNEL = "live"
MINIGAME_CHANNEL = "minigame"
MUSIC_CHANNEL = "music"
CHANNELS: tuple[str, ...] = (LIVE_CHANNEL, MINIGAME_CHANNEL, MUSIC_CHANNEL)


def _chr_id(card: Card) -> str:
    """The holomem id a card belongs to, e.g. 'chr-00001' from 'card-00001-3-nrml-0000-00'.
    Mirrors the derivation the CLI already uses (`'chr-' + card.id.split('-')[1]`)."""
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
            # catalogue Card). Without it the row is unmatchable — reported by
            # `event_bonus_permil`'s `bloom_rows_skipped`, never silently treated as 0-match.
            if bloom is None or bloom != self.bloom:
                return False
        return True


@dataclass(frozen=True, slots=True)
class MarathonEvent:
    """One event's bonus data, grouped by channel. Hand-maintained per-event data (the game
    does not ship it in master; it arrives on the wire per event), loaded from data/marathon.json
    — never written by an automated path, same provenance discipline as config.json."""

    name: str
    rows_by_channel: Mapping[str, tuple[MarathonBonusRow, ...]]

    def rows_for(self, channel: str) -> tuple[MarathonBonusRow, ...]:
        return self.rows_by_channel.get(channel, ())


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
    """The deck's total event-bonus permil for one play — the farming ranking key.

    For each card, SUM the permil of every matching row (rows are wildcard patterns and
    all matches sum); the deck total sums those per-card contributions. Higher = more event
    points. `channel` selects which bonus groups apply: a live play draws the live rows plus
    the song-gated music rows; a minigame play draws the minigame rows.

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


def live_stamina_score(stamina: int) -> int:
    """marathon_score_provide_count for a LIVE play at `stamina` consumed (master table)."""
    if stamina not in MARATHON_LIVE_STAMINA_SCORE:
        raise ValueError(
            f"live stamina {stamina} out of range 1..{LIVE_STAMINA_MAX}"
        )
    return MARATHON_LIVE_STAMINA_SCORE[stamina]


def minigame_stamina_score(mini_game_type: int, stamina: int) -> int:
    """marathon_score_provide_count for a MINIGAME play (master per-type table)."""
    table = MARATHON_MINIGAME_STAMINA_SCORE.get(mini_game_type)
    if table is None:
        raise ValueError(
            f"no stamina curve for mini_game_type {mini_game_type}; "
            f"known: {sorted(MARATHON_MINIGAME_STAMINA_SCORE)}"
        )
    if stamina not in table:
        raise ValueError(f"minigame stamina {stamina} out of range for type {mini_game_type}")
    return table[stamina]


def points_scalar(*, stamina: int, recommended: bool, channel: str = LIVE_CHANNEL,
                  mini_game_type: int | None = None) -> float:
    """The DECK-INDEPENDENT multiplier on absolute event points: the stamina provide-count ×
    the recommended-deck ×2.0. Deliberately separate from `event_bonus_permil` because it does
    NOT re-order decks — it only scales the absolute yield. The exact composition with the base
    score is inferred, not confirmed by a live capture, so this is a reporting aid, not the
    ranking objective. Use it to turn a permil comparison into approximate absolute points, in
    "× base score" units."""
    if channel == MINIGAME_CHANNEL:
        provide = minigame_stamina_score(mini_game_type or 1, stamina)
    else:
        provide = live_stamina_score(stamina)
    return provide * (MARATHON_RECOMMEND_DECK_MULTIPLIER if recommended else 1.0)


# --- The three event objectives -----------------------------------------------
#
# What the game does, term by term (from the client code): the live score is computed in-live with the
# Song Bonus (yellow) and the leader-sings reds INSIDE each note's product; the event bonus is
# never applied in-live — the server applies it afterwards. Event Pt rows SUM across the
# members (deck screen `Enumerable.Sum` @0x6571a74), Event Song Score rows take the MAX card
# (`Enumerable.Max` @0x658f441). The leader slot adds nothing.


def _card_permil(card: Card, rows: Iterable[MarathonBonusRow], *, bloom: int | None,
                 music_id: str | None) -> int:
    """One card's bonus: the sum of every matching row (`CalculateBonusRatioPermil`
    @0x614b5e0). A bloom row needs the owned bloom and compares with == (never >=)."""
    total = 0
    for row in rows:
        if row.bloom is not None and bloom is None:
            continue
        if row.matches(card, bloom=bloom, music_id=music_id):
            total += row.permil
    return total


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


def _ceil_div(num: int, den: int) -> int:
    return -(-num // den)


# The room's MULTI SCORE divisor by event kind (inferred from exact multi Live results):
# a Spotlight (relay) event 75000, a Point Rally (normal) event 60000. The player count of the
# normal-event room it was inferred from is not known, so "15000 x the players" is not ruled
# out: a 5-player normal-event Live would settle it.
MULTI_DIVISOR = {"relay": 75_000, "normal": 60_000}


def multi_divisor(event_key: str | None) -> int:
    """`MULTI_DIVISOR` of an event key (`marathon_event-normal-008-chapter-01` -> 60000);
    an unknown kind keeps the relay value."""
    kind = next((k for k in MULTI_DIVISOR if f"-{k}-" in f"-{event_key or ''}-"), "relay")
    return MULTI_DIVISOR[kind]


def event_points_base(score: int, bonus_permil: int, multi_score: int | None = None,
                      divisor: int = 75_000) -> int:
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


def event_points(score: int | None, bonus_permil: int | None, *, boosts: int,
                 length_permil: int, holopass: bool, base: int | None = None,
                 multi_score: int | None = None, divisor: int = 75_000) -> int:
    """Event Pt for one play: `ceil(base * boost_count * L * (1 + HoloPass))`.

    `boosts` 1..10 picks the provide count (8, 16, ... 56). `length_permil` is the
    song-length multiply the server sends (e.g. 1480): server-only and not known in general,
    so the caller supplies it. `multi_score`: the room's MULTI SCORE for a multi Live
    (`event_points_base`). Exact on observed solo and multi Live/Finish results."""
    if base is None:
        base = event_points_base(int(score), int(bonus_permil), multi_score, divisor)
    provide = live_stamina_score(boosts)
    return _ceil_div(base * provide * int(length_permil) * (2 if holopass else 1), 1000)


def event_song_score(score: int, song_bonus_permil: int) -> int:
    """The event score of a play: the server's bonus on the WHOLE reported score.
    ceil matches the community tool holodori.best; the server's rounding is not known."""
    return _ceil_div(int(score) * (1000 + int(song_bonus_permil)), 1000)


OBJECTIVES = ("score", "event_points", "event_song")


def card_bonus_table(cards: Iterable[Card], event: MarathonEvent, *, blooms: Mapping[str, int],
                     music_id: str | None) -> tuple[tuple[int, int], ...]:
    """Per card, `(live permil, song permil)`: the two numbers a team's `E` (sum over the
    members) and `P` (max over the members) are made of. A search needs them per card,
    once, so they can be added up inside its loop."""
    live, music = event.rows_for(LIVE_CHANNEL), event.rows_for(MUSIC_CHANNEL)
    return tuple((_card_permil(c, live, bloom=blooms.get(c.id), music_id=None),
                  _card_permil(c, music, bloom=blooms.get(c.id), music_id=music_id)
                  if music_id else 0)
                 for c in cards)


def event_objective(score: float, *, objective: str, live_permil: int = 0,
                    song_permil: int = 0) -> float:
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


HOLOMEM_RATING_DENOMINATOR = 5000  # master data; not eligible on PC/Steam


def holomem_song_rating(best_score: int) -> int:
    """A song's contribution to a holomem's Score Rating: best score / 5000.
    ceil as above; the server's rounding is not known."""
    return _ceil_div(int(best_score), HOLOMEM_RATING_DENOMINATOR)


def holomem_rating_total(song_ratings: Iterable[int]) -> int:
    """The holomem's rating: the three best song ratings, summed (client @0x571b340)."""
    return sum(sorted(song_ratings, reverse=True)[:3])


# --- Loader (data/marathon.json) --------------------------------------------

DEFAULT_MARATHON_PATH = Path(__file__).resolve().parents[3] / "data" / "marathon.json"

_ROW_FIELDS = {"permil", "character_id", "card_id", "rarity", "bloom", "music_id", "attribute"}


def _row_from_record(record: object, *, channel: str, index: int) -> MarathonBonusRow:
    field = f"{channel}[{index}]"
    if not isinstance(record, dict):
        raise SchemaError("bonus row must be a JSON object", field=field)
    extra = set(record) - _ROW_FIELDS
    if extra:
        raise SchemaError(f"unknown bonus-row field(s): {sorted(extra)}", field=field)
    permil = record.get("permil")
    if not isinstance(permil, int) or isinstance(permil, bool):
        raise SchemaError("bonus row 'permil' must be an integer", field=f"{field}.permil")
    rarity = record.get("rarity")
    bloom = record.get("bloom")
    if (rarity is None) != (bloom is None):
        raise SchemaError(
            "'rarity' and 'bloom' must be set together (a bloom row needs both)",
            field=field,
        )
    if not any(record.get(k) is not None for k in ("character_id", "card_id", "rarity", "attribute")):
        raise SchemaError(
            "bonus row identifies no card (needs character_id, card_id, rarity+bloom or attribute)",
            field=field,
        )
    return MarathonBonusRow(
        permil=permil,
        character_id=record.get("character_id"),
        card_id=record.get("card_id"),
        rarity=rarity,
        bloom=bloom,
        music_id=record.get("music_id"),
        attribute=record.get("attribute"),
    )


def load_marathon_event(path: Path = DEFAULT_MARATHON_PATH) -> MarathonEvent:
    """Load data/marathon.json -> MarathonEvent. Shape:
        {"name": "...", "channels": {"live": [row, ...], "minigame": [...], "music": [...]}}
    where each row is {permil, and one of character_id | card_id | rarity+bloom | +music_id}.
    Raises SchemaError naming the offending field, never silently skipping a row."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SchemaError(f"marathon event file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SchemaError(f"marathon event file is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SchemaError("marathon event file must be a JSON object")
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise SchemaError("marathon event 'name' must be a non-empty string", field="name")
    channels = data.get("channels")
    if not isinstance(channels, dict):
        raise SchemaError("marathon event 'channels' must be a JSON object", field="channels")
    unknown = set(channels) - set(CHANNELS)
    if unknown:
        raise SchemaError(f"unknown channel(s): {sorted(unknown)}", field="channels")
    rows_by_channel: dict[str, tuple[MarathonBonusRow, ...]] = {}
    for channel, rows in channels.items():
        if not isinstance(rows, list):
            raise SchemaError(f"channel '{channel}' must be a list of rows", field=channel)
        rows_by_channel[channel] = tuple(
            _row_from_record(r, channel=channel, index=i) for i, r in enumerate(rows)
        )
    return MarathonEvent(name=name, rows_by_channel=rows_by_channel)


# --- The CURRENT event, straight from the refresh --------------------------------

DEFAULT_EVENT_BONUS_PATH = (Path(__file__).resolve().parents[3] / "tools" / "recon" / "proxy"
                            / "dumps" / "marathon_bonus.json")
_KIND_CHANNEL = {"live": LIVE_CHANNEL, "minigame": MINIGAME_CHANNEL, "music": MUSIC_CHANNEL}


def _event_key(group_id: str) -> str:
    """'live_marathon_score_bonus-marathon_event-normal-007-chapter-01' -> 'marathon_event-normal-007-chapter-01'."""
    at = group_id.find("marathon_event-")
    return group_id[at:] if at >= 0 else group_id


def pick_chapter(keys, windows=None, now_ms: int | None = None) -> str:
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


def open_events(path: Path = DEFAULT_EVENT_BONUS_PATH) -> list[str]:
    """The event chapters the server listed as open at the last refresh, newest last."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return sorted({_event_key(g) for g in data.get("current_group_ids") or []})


def load_current_event(path: Path = DEFAULT_EVENT_BONUS_PATH, *, event: str | None = None) -> MarathonEvent:
    """The bonus tables of an open event, read from the refresh's marathon_bonus.json
    (`Event/ListEventInfo`, fetched on every refresh). `event` picks one chapter key; by default
    the newest open one. Raises SchemaError when nothing is open."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    keys = sorted({_event_key(g) for g in data.get("current_group_ids") or []})
    if not keys:
        raise SchemaError("no event is open at the last refresh (current_group_ids is empty)")
    chosen = event or keys[-1]
    if chosen not in keys:
        raise SchemaError(f"event {chosen!r} is not open; open: {keys}")
    rows_by_channel: dict[str, list[MarathonBonusRow]] = {}
    for group in data.get("groups") or []:
        if _event_key(group.get("group_id", "")) != chosen:
            continue
        channel = _KIND_CHANNEL.get(group.get("kind"))
        if channel is None:
            continue
        for row in group.get("rows") or []:
            rows_by_channel.setdefault(channel, []).append(MarathonBonusRow(
                permil=int(row["permil"]), character_id=row.get("character_id"),
                card_id=row.get("card_id"), rarity=row.get("rarity"), bloom=row.get("bloom"),
                music_id=row.get("music_id"), attribute=row.get("attribute")))
    return MarathonEvent(name=chosen, rows_by_channel={k: tuple(v) for k, v in rows_by_channel.items()})

