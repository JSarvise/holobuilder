"""Event Points mode for the web server: the newest open event,
which cards and songs carry its bonus, and the account-basis search that ranks
teams by Event Points (or Event Song Score) instead of by score.

Three pieces, each a pure function of what it is handed so the tests can stub
the leaf:

- `event_payload`  — `GET /api/event`: the event's bonus rows grouped for a page,
  and the account's own bonus cards (live / song permil, level, bloom).
- `songs_payload`  — `GET /api/event/songs`: songs ranked by the song-length
  multiplier `L` (team-independent), an ESTIMATE (`LENGTH_FORMULA`).
- `search_event`   — the search behind `POST /api/event/run`:
    A. the two-stage search by score (its best score is `S_max`);
    B. the same search fed the event objective (`two_stage.objective_scorer`);
    C. a bound: a team beating B's best `f*` needs `E > E_min` (Event Points) or
       `P > P_min` (Event Song Score); every legal member set above it is counted
       and, when there are few enough, exact-scored under B's top leaders.
  `run_account_search` wires it to the real account basis (every owned rarity,
  real level / bloom / boards — on an account the best bonus carriers may be 4★/3★),
  or, with `basis: "maxed"` (the page's Maxed account), to the 5★
  catalogue maxed: every event card is a 5★, at bloom 5 the strongest carrier.

Rules (marathon.py): E = live rows summed per card, summed over the
five MEMBERS; P = song rows, MAX over the members; the leader slot adds nothing.
Whether P lifts Event Pt is not known, so Event Points stay on the raw score.

Nothing here writes a file.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import itertools
import json
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
_PROXY = REPO / "tools" / "recon" / "proxy"
for _p in (REPO / "src", _PROXY):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from hbuilder.domain import marathon as mar  # noqa: E402

EVENT_OBJECTIVES = ("event_points", "event_song")
# the pins the Score search takes too (`ondemand.parse_filters`)
FILTER_KEYS = ("leader_member", "leader_card", "no_leader_overlap", "require", "exclude")
DEFAULT_OBJECTIVE = "event_points"
DEFAULT_BOOSTS = 3
DEFAULT_TOP = 10
CERTIFY_LIMIT = 400_000
SEED = 20261013

# --- the song-length multiplier L -------------------------------------------------
# 50 free seconds, then a slope of 10/29 % a second (a game update changed these from
# 90 s and 100/190, and the master's Setting no longer carries the constants). Checked
# against a result screen: Shallow Shadow (123 s, 3 boosts) shows +31 %, which 10/29
# gives. The boost term (300/200/200 permil for 1/2/3 boosts, 0 at 4+) is not in the
# master but the +31 % needs it, so it is presumably still applied server-side. L is
# computed by the server; the client never does.
LENGTH_FREE_SECONDS = 50
LENGTH_SLOPE = (10, 29)            # percent per second, as an exact fraction
LENGTH_BOOST_UP = {1: 300, 2: 200, 3: 200}
LENGTH_FORMULA = ("L = 1000 + 10*ceil((sec - 50) * 10/29 * (1 + up/1000)); L = 1000 when "
                  "sec <= 50; up = 300/200/200 permil for 1/2/3 boosts, 0 at 4+; sec = "
                  "Music.playingSeconds (checked on Shallow Shadow, 123 s: +31 %)")
EP_SCORE_NOTE = ("Event Points use the raw score: whether the song bonus P lifts Event Pt is "
                 "not known (holodori.best assumes it does).")


# --- Highlight ---------------------------------------------------------------------
# The multi Live pick "Highlight" (client enum LiveMultiMusicSelectionType.PICKUP = 3) draws a
# random WHOLE song among every song sung by one of the chapter's Event Bonus holomems (its
# character-only live rows), group songs included, and its song-length multiplier L is the
# length formula's for a HIGHLIGHT_SECONDS song whatever song is drawn: +54 % at 1 boost, +50 % at
# 2-3, +42 % at 4+ (the boost term applies as for any song). Result screens show "Highlight
# Bonus +42%" at 4 and 10 boosts and +50 % at 3; +54 % at 1 comes from a player's report.
# 169 s and 170 s give the same L at every boost count and the bonus is rounded to a whole
# percent before it applies, so the exact base never matters (a flat x1.5 is right only at
# 2-3 boosts). Everything else in Event Pt is as for any song. The draw is taken as UNIFORM
# over the pool (an assumption: the client does not show the weights), so a team's expected
# score is the MEAN of its score over the pool's charts.
HIGHLIGHT = "highlight"
HIGHLIGHT_SECONDS = 170
# Highlight on a sample (the Score search's In general recipe; the default,
# opts["hl_sample"] False turns it off): the search ranks by the mean over HIGHLIGHT_SAMPLE of
# the pool's charts (`two_stage.chart_sample`), then its best HIGHLIGHT_RESCORE teams are scored
# on every chart of the pool and re-ranked. On 4 setups (the page's own options, account
# plain, maxed, boards as they are) the served top 5-10 is identical to the full-pool search,
# and the whole search goes 160 -> 89 s, 62 -> 39 s, 131 -> 80 s, 68 -> 34 s.
HIGHLIGHT_SAMPLE = 6
HIGHLIGHT_RESCORE = 200
HIGHLIGHT_NOTES = (
    "Highlight: a random whole song of the event holomems' songs (a uniform draw is "
    "assumed); each team is ranked by the MEAN of its score over every chart of the pool, "
    "and its length bonus is a 170 s song's whatever song is drawn (+54 % at 1 boost, +50 % at "
    "2-3, +42 % at 4+; seen in game at 3, 4 and 10 boosts, 1 boost from a player's report).",
    "Highlight: no Event Song Score bonus is counted (P = 0, assumed: the pick is not the "
    "event's bonus song set; Event Points use the raw score anyway).",
)


def is_highlight(song) -> bool:
    return str(song or "").strip().lower() == HIGHLIGHT


def event_holomems(event: "mar.MarathonEvent") -> list[str]:
    """The chapter's Event Bonus holomems: its live rows naming a character and no card."""
    out: list[str] = []
    for row in event.rows_for(mar.LIVE_CHANNEL):
        if row.character_id and row.card_id is None and row.character_id not in out:
            out.append(row.character_id)
    return out


def highlight_pool(holomems: Sequence[str], music_rows: Sequence[Mapping],
                   charted: Sequence[str] | set | None = None) -> tuple[list[str], list[str]]:
    """`(pool, skipped)`: every song (`main_Music` rows, `characterIds`) one of `holomems`
    sings, group songs included, sorted by music id. A song with no chart in `charted` (the
    charts on disk at the difficulty) goes to `skipped` instead; `charted` None keeps all."""
    want = set(holomems)
    pool: list[str] = []
    skipped: list[str] = []
    for data in music_rows:
        music_id = str((data or {}).get("id") or "")
        if not music_id or not want & set((data or {}).get("characterIds") or ()):
            continue
        (pool if charted is None or music_id in charted else skipped).append(music_id)
    return sorted(set(pool)), sorted(set(skipped))


def highlight_context(ctx, library, pool_ids: Sequence[str]):
    """`(ctx, pool charts, lost ids)`: the leaf context scoring every chart of the pool and
    only those, a board per chart (a batch with no `chart_indices` is then the MEAN over
    them, `RefinePool.batch_score`). `lost`: pool songs `library` has no chart for."""
    by_music = {c.music_id: c for c in library}
    charts = tuple(by_music[m] for m in pool_ids if m in by_music)
    lost = [m for m in pool_ids if m not in by_music]
    return dataclasses.replace(ctx, charts=charts, board_per_chart=True), charts, lost


class EventRequestError(ValueError):
    """A bad request body — the route answers 400 with this message."""


def _ceil_div(num: int, den: int) -> int:
    return -(-num // den)


def length_permil_estimate(seconds: float | int | None, boosts: int) -> int | None:
    """`L` in permil for a song of `seconds` played with `boosts` (`LENGTH_FORMULA`)."""
    if seconds is None:
        return None
    sec = int(seconds)
    if sec <= LENGTH_FREE_SECONDS:
        return 1000
    up = LENGTH_BOOST_UP.get(int(boosts), 0)
    num, den = LENGTH_SLOPE
    pct = _ceil_div((sec - LENGTH_FREE_SECONDS) * num * (1000 + up), den * 1000)
    return 1000 + 10 * pct


# --- request parsing ----------------------------------------------------------
def parse_event_body(body: Mapping) -> dict:
    """`{objective, song, boosts, holopass, workers, top, event}` or EventRequestError.
    `difficulty` is resolved by the server's own parser (same 400 as every route)."""
    if not isinstance(body, Mapping):
        raise EventRequestError("the body must be a JSON object")
    objective = body.get("objective", DEFAULT_OBJECTIVE)
    if objective in (None, ""):
        objective = DEFAULT_OBJECTIVE
    if objective not in EVENT_OBJECTIVES:
        raise EventRequestError(f"unknown objective {objective!r}; one of {list(EVENT_OBJECTIVES)}")
    song = str(body.get("song") or "").strip()
    if not song:
        raise EventRequestError("'song' (a music id such as m0342, or 'highlight') is required")
    if is_highlight(song):
        # a random song of a pool: an Event Song Score has no song to belong to
        song = HIGHLIGHT
        if objective != "event_points":
            raise EventRequestError("Highlight ranks by Event Points only: Event Song Score "
                                    "belongs to one song")
    boosts = body.get("boosts", DEFAULT_BOOSTS)
    if boosts is None:
        boosts = DEFAULT_BOOSTS
    if isinstance(boosts, bool) or not isinstance(boosts, int) or not 1 <= boosts <= 10:
        raise EventRequestError(f"'boosts' must be an integer 1..10, got {boosts!r}")
    holopass = body.get("holopass", True)
    if holopass is None:
        holopass = True
    if not isinstance(holopass, bool):
        raise EventRequestError(f"'holopass' must be true or false, got {holopass!r}")
    workers = body.get("workers")
    if workers is not None and (isinstance(workers, bool) or not isinstance(workers, int)
                                or workers < 1):
        raise EventRequestError(f"'workers' must be a positive integer, got {workers!r}")
    top = body.get("top", DEFAULT_TOP)
    if isinstance(top, bool) or not isinstance(top, int):
        raise EventRequestError(f"'top' must be an integer, got {top!r}")
    event = body.get("event")
    if event is not None and not isinstance(event, str):
        raise EventRequestError("'event' must be an event key string")
    certify = body.get("certify", False)
    if not isinstance(certify, bool):
        raise EventRequestError(f"'certify' must be true or false, got {certify!r}")
    # the Score search's levers (`ondemand.Assumptions`, `ondemand.Filters`), checked
    # here and kept as plain JSON (the job echoes its opts); absent -> the account
    # basis this route always used
    assumptions = body.get("assumptions")
    if assumptions is not None:
        import ondemand  # noqa: PLC0415

        if not isinstance(assumptions, Mapping):
            raise EventRequestError("'assumptions' must be an object")
        try:
            ondemand.parse_assumptions(assumptions)
        except (TypeError, ValueError) as exc:
            raise EventRequestError(f"bad assumptions: {exc}") from exc
        assumptions = dict(assumptions)
    # "maxed": the page's Maxed account (every card in the game at max level and bloom, every
    # holomem at max level with a complete board) -- the account levers do not apply to it
    basis = str(body.get("basis") or "account")
    if basis not in ("account", "maxed"):
        raise EventRequestError(f"'basis' must be 'account' or 'maxed', got {basis!r}")
    if basis == "maxed" and assumptions is not None:
        raise EventRequestError("'assumptions' are the account's levers: not with basis 'maxed'")
    filters = {k: body.get(k) for k in FILTER_KEYS if body.get(k) not in (None, "", [], False)}
    return {"objective": objective, "song": song, "boosts": boosts, "holopass": holopass,
            "basis": basis,
            "workers": workers, "top": max(1, min(50, top)), "event": event or None,
            "certify": certify, "assumptions": assumptions, "filters": filters,
            # how the run is played (`ondemand.parse_play`): the page's Platform and Auto
            "platform": "pc" if str(body.get("platform") or "").lower() == "pc" else "mobile",
            "auto": body.get("auto") is True}


# --- GET /api/event -------------------------------------------------------------
def _chr_of(card_id: str) -> str:
    parts = str(card_id).split("-")
    return f"chr-{parts[1]}" if len(parts) > 1 else str(card_id)


def card_label(card) -> str:
    return f"{card.member} / {card.costume_name} ({card.rarity})"


def _rarity_int(rarity) -> int | None:
    head = str(rarity or "")[:1]
    return int(head) if head.isdigit() else None


def card_object(card_id: str, card, *, state: Mapping | None, owned: bool, live_permil: int,
                song_permil: int, **extra) -> dict:
    """THE card shape every route here returns (the page draws a card face from it)."""
    state = state or {}
    out = {"id": card_id, "member": getattr(card, "member", None),
           "costume_name": getattr(card, "costume_name", None), "chr": _chr_of(card_id),
           "type": getattr(card, "type", None), "rarity": _rarity_int(getattr(card, "rarity", None)),
           "level": state.get("level"), "bloom": state.get("bloom"),
           "limit_break": state.get("limit_break"), "owned": owned,
           "live_permil": live_permil, "song_permil": song_permil,
           "name": card_label(card) if card is not None else card_id}
    out.update(extra)
    return out


# --- the event's name, kind, times and banner (event_art.py -> dumps/event_meta.json) ----
EVENT_META_KEYS = ("title", "kind_label", "starts_at", "ends_at", "art", "logo")


def event_meta_fields(meta: Mapping | None, key: str, art_dir: Path | None) -> dict:
    """`{title, kind_label, starts_at, ends_at, art, logo}` for the event chapter `key`.

    Every value is null when `meta` has no entry for the event. `art` / `logo` are URLs under
    `/eventart/` and only when the file is on disk in `art_dir` (a read, never a write)."""
    out: dict = dict.fromkeys(EVENT_META_KEYS)
    entry = chapter = None
    for ev in (meta or {}).get("events") or []:
        chapters = ev.get("chapters") or []
        hit = next((c for c in chapters if c.get("id") == key), None)
        if hit is not None or (ev.get("event_id") and key.startswith(ev["event_id"] + "-")):
            entry, chapter = ev, hit
            break
    if entry is None:
        return out
    out["title"] = (chapter or {}).get("title") or entry.get("title")
    out["kind_label"] = entry.get("kind_label")
    out["starts_at"] = entry.get("starts_at")
    out["ends_at"] = (chapter or {}).get("ends_at") or entry.get("ends_at")
    for field, kind in (("art", "banner"), ("logo", "logo")):
        name = (entry.get("art") or {}).get(kind)
        if (name and art_dir is not None and "/" not in name and "\\" not in name
                and (Path(art_dir) / name).is_file()):
            out[field] = f"/eventart/{name}"
    return out


def event_payload(raw: Mapping, event: "mar.MarathonEvent", *, open_keys: Sequence[str],
                  cards_by_id: Mapping[str, Any], owned_ids: Sequence[str],
                  collection: Mapping[str, Mapping], titles: Mapping[str, str],
                  chr_names: Mapping[str, str], meta: Mapping | None = None,
                  art_dir: Path | None = None, singers: Mapping[str, str] | None = None,
                  music_meta: Mapping[str, Mapping] | None = None) -> dict:
    """The current event's bonus rows, grouped for a page, and the account's bonus cards.
    `meta` / `art_dir`: `dumps/event_meta.json` + `dumps/event_art/` (`event_art.py`), which
    fill the event's title, kind, times and banner; absent, those stay null. `singers` /
    `music_meta` (music id -> its `main_Music` row): each event song's singers and whether
    it is a group or a solo song, for `songs`."""
    owned = set(owned_ids)
    blooms = {k: (v or {}).get("bloom") for k, v in collection.items()}
    live_rows = event.rows_for(mar.LIVE_CHANNEL)
    music = event.rows_for(mar.MUSIC_CHANNEL)
    song_ids: list[str] = []
    for row in music:
        if row.music_id and row.music_id not in song_ids:
            song_ids.append(row.music_id)
    # every song of the chapter, as the server lists them (`marathon_bonus.py`'s
    # `event_songs`): a song without a song-score bonus row still earns Event Pt
    listed = list((raw.get("event_songs") or {}).get(event.name) or ())
    listed += [m for m in song_ids if m not in listed]

    def permils(card_id: str) -> tuple[int, dict[str, int]]:
        card = cards_by_id.get(card_id)
        if card is None:
            return 0, {}
        bloom = blooms.get(card_id) if card_id in owned else None
        live = mar._card_permil(card, live_rows, bloom=bloom, music_id=None)
        songs = {m: p for m in song_ids
                 if (p := mar._card_permil(card, music, bloom=bloom, music_id=m))}
        return live, songs

    def card_brief(card_id: str, **extra) -> dict:
        live, songs = permils(card_id)
        return card_object(card_id, cards_by_id.get(card_id),
                           state=collection.get(card_id) if card_id in owned else None,
                           owned=card_id in owned, live_permil=live,
                           song_permil=max(songs.values(), default=0),
                           song_permil_by_music=songs, **extra)

    holomems, bonus_cards, attributes = [], [], []
    rarity_bloom: dict[str, dict[str, int]] = {}
    for row in event.rows_for(mar.LIVE_CHANNEL):
        if row.card_id is not None:
            bonus_cards.append(card_brief(row.card_id, permil=row.permil))
        elif row.character_id is not None:
            holomems.append({"character_id": row.character_id,
                             "name": chr_names.get(row.character_id, row.character_id),
                             "permil": row.permil})
        elif row.rarity is not None:
            rarity_bloom.setdefault(str(row.rarity), {})[str(row.bloom)] = row.permil
        elif row.attribute is not None:
            attributes.append({"attribute": row.attribute, "permil": row.permil})
    music_rows = []
    for row in music:
        entry = {"music_id": row.music_id,
                 "title": titles.get(row.music_id or "", row.music_id), "permil": row.permil}
        if row.card_id is not None:
            entry["card"] = card_brief(row.card_id)
        elif row.character_id is not None:
            entry.update(character_id=row.character_id,
                         name=chr_names.get(row.character_id, row.character_id))
        music_rows.append(entry)

    mine = []
    for card_id in owned_ids:
        if card_id not in cards_by_id:
            continue
        entry = card_brief(card_id)
        if entry["live_permil"] or entry["song_permil"]:
            mine.append(entry)
    mine.sort(key=lambda r: (-r["live_permil"], -r["song_permil"], r["id"]))
    key = event.name
    return {
        "available": True,
        "event": {"key": key, **event_meta_fields(meta, key, art_dir),
                  "chapter": key.rsplit("chapter-", 1)[1] if "chapter-" in key else None},
        "open_events": list(open_keys),
        # the chapters of THIS event (a Spotlight event has one per holomem, run in turn):
        # the page's chapter tabs, each with its window and whether it is running now
        "chapters": event_chapters(meta, key, open_keys, raw),
        "fetched_at": raw.get("live_fetched_at"),
        "live": {"holomems": holomems, "cards": bonus_cards,
                 "rarity_bloom": rarity_bloom, "attributes": attributes},
        "music": music_rows,
        "event_songs": listed,
        # each event song: its singers, group / solo, and whether a card lifts its score
        "songs": [{"music_id": m, "title": titles.get(m, m), "singer": (singers or {}).get(m, ""),
                   "singer_type": singer_type((music_meta or {}).get(m)),
                   "song_bonus": m in song_ids} for m in listed],
        "owned_bonus_cards": mine,
        "rules": {"E": "live rows summed per card, summed over the 5 members (leader adds nothing)",
                  "P": "song rows of the played song, MAX over the 5 members",
                  "event_points": EP_SCORE_NOTE},
    }


def singer_type(music_row: Mapping | None) -> str | None:
    """`main_Music`'s `musicSingerType` as `solo` / `group` / ... (None when unknown)."""
    raw = str((music_row or {}).get("musicSingerType") or "")
    return raw.rsplit("_TYPE_", 1)[-1].lower() if "_TYPE_" in raw else None


# --- GET /api/event/songs -------------------------------------------------------
def songs_payload(music_rows: Sequence[Mapping], *, titles: Mapping[str, str], boosts: int,
                  event: "mar.MarathonEvent | None", charted: Mapping[str, int] | None,
                  singers: Mapping[str, str] | None = None, difficulty: str = "EXPERT") -> dict:
    """Songs ranked by the L estimate (team-independent), longest bonus first."""
    event_songs: dict[str, list[str]] = {}
    if event is not None:
        for row in event.rows_for(mar.MUSIC_CHANNEL):
            if row.music_id:
                event_songs.setdefault(row.music_id, []).append(row.card_id or row.character_id)
    songs = []
    for data in music_rows:
        music_id = str(data.get("id") or "")
        if not music_id:
            continue
        seconds = data.get("playingSeconds")
        length = length_permil_estimate(seconds, boosts)
        remarks = []
        if music_id in event_songs:
            remarks.append(f"event song: Event Song Score bonus for {len(event_songs[music_id])} card(s)")
        if seconds is not None and int(seconds) <= LENGTH_FREE_SECONDS:
            remarks.append(f"<= {LENGTH_FREE_SECONDS} s: no length bonus")
        if charted is not None and music_id not in charted:
            remarks.append("no chart on disk at this difficulty: cannot be searched")
        songs.append({"music_id": music_id, "title": titles.get(music_id, music_id),
                      "singer": (singers or {}).get(music_id, ""),
                      "notes": None if charted is None else charted.get(music_id),
                      "seconds": seconds, "length_permil": length,
                      "event_song": music_id in event_songs,
                      "event_song_cards": event_songs.get(music_id, []),
                      "chart_available": None if charted is None else music_id in charted,
                      "note": "; ".join(remarks)})
    songs.sort(key=lambda s: (-(s["length_permil"] or 0), -(s["seconds"] or 0), s["music_id"]))
    for rank, song in enumerate(songs, 1):
        song["rank"] = rank
    return {"boosts": boosts, "difficulty": difficulty, "length_multiplier_estimated": True,
            "formula": LENGTH_FORMULA,
            "note": ("Ranked by L alone; real Event Pt also depends on the team's score, but "
                     "weakly (the 120 floor dominates, EVENT-MODES.md §8c)."),
            "songs": songs}


# --- the search ------------------------------------------------------------------
class EventCancelled(Exception):
    """Raised from inside the scorer when the job's cancel flag is set."""


def _aggregate(bonus: Sequence[tuple[int, int]], members: Sequence[int]) -> tuple[int, int]:
    return (sum(bonus[i][0] for i in members), max(bonus[i][1] for i in members))


def sets_above(values: Sequence[int], hol: Sequence[str], threshold: float, *, use_max: bool,
               limit: int) -> tuple[list[tuple[int, ...]], bool]:
    """Every legal member set (5 distinct holomems) whose sum (or max) of `values`
    exceeds `threshold`; `(sets, overflowed)` — overflowed when more than `limit`.
    Carriers (value > 0) first, then filled with plain cards. A set made only of
    plain cards has value 0, so it qualifies only when `threshold < 0`, which this
    reports as an overflow (every legal set would qualify)."""
    if threshold < 0:
        return [], True
    n = len(values)
    carriers = [i for i in range(n) if values[i] > 0]
    plain = [i for i in range(n) if values[i] <= 0]
    above: list[tuple[int, ...]] = []
    for k in range(5, 0, -1):
        for core in itertools.combinations(carriers, k):
            agg = max(values[i] for i in core) if use_max else sum(values[i] for i in core)
            if agg <= threshold:
                continue
            names = {hol[i] for i in core}
            if len(names) != k:
                continue
            for fill in itertools.combinations(plain, 5 - k):
                if len(names | {hol[i] for i in fill}) == 5:
                    above.append(tuple(sorted(core + fill)))
                    if len(above) > limit:
                        return above, True
    return above, False


# singer-led rows search_event adds below its head (2 x `ondemand.SINGER_CANDIDATES`)
SINGER_ROWS = 8


def search_event(*, reps: Sequence, leaders: Sequence, score: Callable, bonus: Sequence[tuple[int, int]],
                 objective: str, base_leader, always: Sequence = (), gate_of=None,
                 top: int = DEFAULT_TOP, certify_limit: int = CERTIFY_LIMIT, seed: int = SEED,
                 on_stage: Callable[[str, str, dict], None] | None = None,
                 search_options: Mapping | None = None, engine: str = "joint",
                 certify: bool = True) -> dict:
    """A / B / C on one song. `score(leader, sets) -> raw scores` (cancel by raising
    from it); `bonus[i] = (live permil, song permil)` of pool card i. Returns the
    candidate rows (leader, members, raw score, E, P, objective, source) best
    first, and the bound block.

    `engine` "joint" (the default: the leader chosen inside one
    search, `joint_search.search_song`, ~5 s a search on the account) or
    "two_stage" (the base search + leader triage + per-leader searches).
    `certify` False skips C's exact check of every set above the threshold (the
    threshold is still reported; on m0342 the check is 37,350 sets, 61 s)."""
    import two_stage  # noqa: PLC0415

    if objective not in EVENT_OBJECTIVES:
        raise ValueError(f"unknown objective {objective!r}")
    stage = on_stage or (lambda *_a: None)
    hol = tuple(c.member for c in reps)
    if engine == "joint":
        import joint_search  # noqa: PLC0415

        def run_search(scorer):
            # no forced singers: the leader model finds them (on 5 songs: same best team,
            # 1/3 of the evaluations)
            return joint_search.search_song(reps, leaders, scorer, gate_of=gate_of, seed=seed,
                                            **dict(search_options or {}))
    elif engine == "two_stage":
        common = dict(base_leader=base_leader, always=list(always), gate_of=gate_of, margin=0.04,
                      triage_top=300, tier=100, seed=seed, **dict(search_options or {}))

        def run_search(scorer):
            return two_stage.search_song(reps, leaders, scorer, **common)
    else:
        raise ValueError(f"unknown engine {engine!r}")

    def value_of(raw: float, members) -> tuple[int, int, float]:
        e, p = _aggregate(bonus, members)
        return e, p, mar.event_objective(raw, objective=objective, live_permil=e, song_permil=p)

    raw_cache: dict[tuple[str, tuple[int, ...]], float] = {}

    def raw_score(leader, members) -> float:
        key = (leader.id, tuple(members))
        if key not in raw_cache:
            raw_cache[key] = float(score(leader, [tuple(members)])[0])
        return raw_cache[key]

    def row(leader, members, raw, source, status=None) -> dict:
        e, p, value = value_of(raw, members)
        return {"leader": leader, "members": tuple(members), "score": raw, "live_permil": e,
                "song_permil": p, "objective": value, "source": source, "status": status}

    def recording(leader, sets):
        sets = list(sets)
        values = score(leader, sets)
        for members, value in zip(sets, values):
            raw_cache.setdefault((leader.id, tuple(members)), float(value))
        return values

    # A: by score (S_max for the bound, and the naive re-sort as candidates). Only
    # when certifying: S_max is what the bound needs, and B already ranks by the
    # objective itself.
    a_rows: list[dict] = []
    a = None
    if certify:
        t0 = time.perf_counter()
        stage("score_search", "active", {})
        a = run_search(score)
        for r in a["rows"]:
            for value, members in (r.get("ranking") or [(r["best"], r["members"])]):
                raw_cache.setdefault((r["leader"].id, tuple(members)), float(value))
                a_rows.append(row(r["leader"], members, float(value), "score_search", r["status"]))
        a_rows.sort(key=lambda r: -r["objective"])
        stage("score_search", "done", {"best_score": max(r["score"] for r in a_rows),
                                       "seconds": time.perf_counter() - t0,
                                       "exact_evals": a["stats"]["exact_evals"]})

    # B: the objective inside the search
    t0 = time.perf_counter()
    stage("event_search", "active", {})
    wrapped = two_stage.objective_scorer(recording, bonus, objective)
    b = run_search(wrapped)
    if engine == "joint":
        # the true top teams across leaders (a leader may hold several)
        flat = sorted(((value, r["position"], r["leader"], members) for r in b["rows"]
                       for value, members in r["ranking"]), key=lambda t: (-t[0], t[1], t[3]))
        picked = [(leader, members, r_status) for _v, _p, leader, members, r_status in
                  ((v, p, l, m, "searched") for v, p, l, m in flat[:max(top, 8)])]
        # the singers' best teams below that head too, so the exact re-plan can reach
        # them (`ondemand.replan_pick`): the quick plan ranks a singer-led team low and
        # the re-plan lifts it most (the singer extras of the event re-plan can only come
        # from these rows)
        singing = {id(c) for c in always}
        picked += [(l, m, "searched") for _v, _p, l, m in flat[max(top, 8):]
                   if id(l) in singing][:SINGER_ROWS]
    else:
        picked = [(r["leader"], r["members"], r["status"]) for r in b["rows"][:max(top, 8)]]
    b_rows = [row(leader, members, raw_score(leader, members), "event_search", status)
              for leader, members, status in picked]
    b_rows.sort(key=lambda r: -r["objective"])
    best_score = max([r["score"] for r in a_rows] + [r["score"] for r in b_rows])
    stage("event_search", "done", {"seconds": time.perf_counter() - t0,
                                   "exact_evals": b["stats"]["exact_evals"],
                                   "leaders_searched": b["stats"]["searched"]})

    # C: the bound
    t0 = time.perf_counter()
    stage("bound", "active", {})
    best = b_rows[0] if not a_rows or b_rows[0]["objective"] >= a_rows[0]["objective"] else a_rows[0]
    s_max = max(best_score, best["score"])
    f_star = best["objective"]
    if objective == "event_points":
        threshold = 1000.0 * f_star / (120.0 + s_max / 15000.0) - 1000.0
        values = [b_[0] for b_ in bonus]
        name = "e_min"
    else:
        threshold = 1000.0 * f_star / s_max - 1000.0 if s_max > 0 else -1.0
        values = [b_[1] for b_ in bonus]
        name = "p_min"
    # the pins hold in the bound as in the search: a set without a required card, or a
    # leader whose holomem is among the members under No leader overlap, is no answer
    # (otherwise such a set could be served #1 and marked "Checked")
    require = tuple((search_options or {}).get("require") or ())
    allowed = (search_options or {}).get("allowed")
    lpos = {id(l): k for k, l in enumerate(leaders)}
    if certify:
        above, overflow = sets_above(values, hol, threshold, use_max=objective == "event_song",
                                     limit=certify_limit)
        if require:
            above = [s for s in above if all(i in s for i in require)]
    else:
        above, overflow = [], False
    bound_rows = []
    cand_leaders = {r["leader"].id: r["leader"] for r in b_rows[:8]}
    cand_leaders.setdefault(best["leader"].id, best["leader"])
    if not overflow and above:
        topc = None
        for leader in cand_leaders.values():
            mine = (above if allowed is None or id(leader) not in lpos
                    else [s for s in above if allowed(lpos[id(leader)], s)])
            for start in range(0, len(mine), 20_000):
                chunk = mine[start:start + 20_000]
                for members, value in zip(chunk, wrapped(leader, chunk)):
                    if topc is None or value > topc[0]:
                        topc = (value, leader, members)
        if topc is not None:
            bound_rows.append(row(topc[1], topc[2], raw_score(topc[1], topc[2]), "bound"))
    certified = certify and not overflow
    bound = {
        "requested": certify,
        "objective": objective, name: threshold, "threshold": threshold,
        "f_star": f_star, "s_max": s_max, "sets_above": len(above) if certify and not overflow else None,
        "overflow": overflow, "certify_limit": certify_limit,
        "leaders_checked": sorted(cand_leaders) if not overflow and above else [],
        "improved": bool(bound_rows and bound_rows[0]["objective"] > f_star),
        "certified": certified,
        "assumptions": [
            f"S_max = {s_max:,.0f}, the best score the score search found, is the true best "
            "score on this song (a searched, not proven, maximum)",
            "the best team's leader is one of the leaders exact-scored above the bound "
            "(the event search's top leaders and the best team's own)",
        ],
        "seconds": time.perf_counter() - t0,
    }
    stage("bound", "done", {"sets_above": bound["sets_above"], "certified": certified})

    # merge: one row per (leader, members), best objective first
    merged: dict[tuple[str, tuple[int, ...]], dict] = {}
    for r in b_rows + bound_rows + a_rows[:top]:
        key = (r["leader"].id, r["members"])
        if key not in merged:
            merged[key] = r
    rows = sorted(merged.values(), key=lambda r: (-r["objective"], -r["score"]))
    return {"rows": rows, "best_score": best_score, "bound": bound,
            "stats": {**({"score_search": a["stats"]} if a else {}), "event_search": b["stats"]}}


def event_points_for(body: Mapping) -> dict:
    """`POST /api/event/points {score, live_permil, boosts, length_permil, holopass,
    multi_score?}` -> `{event_points, event_points_base, multi}`: one team's Event Pt per
    Live, solo or -- with the room's MULTI SCORE the player types in -- multi
    (`marathon.event_points_base`, checked on 2 multi Lives). EventRequestError on a bad
    field."""
    if not isinstance(body, Mapping):
        raise EventRequestError("the body must be a JSON object")

    def whole(name, lo, hi=None, default=None):
        v = body.get(name, default)
        if isinstance(v, bool) or not isinstance(v, int) or v < lo or (hi is not None and v > hi):
            raise EventRequestError(f"'{name}' must be an integer >= {lo}"
                                    + (f" and <= {hi}" if hi is not None else "") + f", got {v!r}")
        return v
    score = whole("score", 0)
    live = whole("live_permil", 0)
    boosts = whole("boosts", 1, 10, DEFAULT_BOOSTS)
    length = whole("length_permil", 1000)
    multi = body.get("multi_score")
    multi = None if multi in (None, "", 0) else whole("multi_score", 0)
    holopass = body.get("holopass", True)
    if not isinstance(holopass, bool):
        raise EventRequestError(f"'holopass' must be true or false, got {holopass!r}")
    # the room term's divisor follows the event's kind (`marathon.multi_divisor`): the event
    # the page shows, else the server's current one
    divisor = mar.multi_divisor(body.get("event") or _current_event_key())
    return {"event_points": mar.event_points(score, live, boosts=boosts, length_permil=length,
                                             holopass=holopass, multi_score=multi, divisor=divisor),
            "event_points_base": mar.event_points_base(score, live, multi, divisor),
            "multi": multi is not None, "multi_divisor": divisor}


def _current_event_key() -> str | None:
    """The chapter open now at the last refresh (`bundle_from_raw`'s choice), or None."""
    try:
        path = Path(__file__).resolve().parents[1] / "recon" / "proxy" / "dumps" / "marathon_bonus.json"
        return load_event_bundle(path)[1].name
    except Exception:  # noqa: BLE001 - no event file: the relay default
        return None


def row_payload(rank: int, r: Mapping, *, reps: Sequence, bonus_of: Callable,
                card_state: Callable[[str], Mapping], boosts: int, length_permil: int | None,
                holopass: bool) -> dict:
    """One ranked team, as the page reads it."""
    def card(c, counts: bool) -> dict:
        live, song = bonus_of(c)
        return card_object(c.id, c, state=card_state(c.id), owned=True, live_permil=live,
                           song_permil=song, counts=counts)

    raw = round(r["score"])
    e, p = r["live_permil"], r["song_permil"]
    ep = (mar.event_points(raw, e, boosts=boosts, length_permil=length_permil, holopass=holopass)
          if length_permil is not None else None)
    return {"rank": rank, "leader": card(r["leader"], False),
            "members": [card(reps[i], True) for i in r["members"]],
            "score": raw, "live_permil": e, "song_permil": p, "objective": r["objective"],
            "event_points_base": mar.event_points_base(raw, e),
            "event_points": ep, "event_song_score": mar.event_song_score(raw, p),
            "source": r["source"], "status": r.get("status"), "refined": bool(r.get("refined"))}


# rows that also get the run-score quantiles (p10/p50/p90), as the two-stage cache's ORDER_ROWS
QUANTILE_ROWS = 1
# rows re-developed inside the job (the rest on demand by the page)
DETAIL_ROWS = 3


def add_row_detail(row: dict, built: Mapping, leader, members: Sequence, *,
                   detail_fn: Callable | None = None, with_distribution: bool = False,
                   refine: bool = False) -> None:
    """Fill a row with its slot order, deck power, ceiling, board state and Unit
    Score panel (`row_detail.leaf_detail`, the event leaf; with its activation nodes
    re-chosen when the row was `refine`d). A failure leaves the row as it was and says
    why (`detail_error`)."""
    try:
        if detail_fn is None:
            import row_detail  # noqa: PLC0415
            from hbuilder.domain.team import Team  # noqa: PLC0415

            got = row_detail.leaf_detail(built, Team(leader=leader, members=tuple(members)),
                                         refine=refine, want_distribution=with_distribution)
        else:
            got = detail_fn(leader, members)
    except Exception as exc:  # noqa: BLE001 — a detail is optional, the row is not
        row["detail_error"] = f"{type(exc).__name__}: {exc}"
        return
    panel = got.get("panel") or {}
    split = got.get("score_bonus") or {}
    row.update({
        "member_order": got.get("member_order"),
        "deck_power": got.get("deck_power"),
        "song_max": got.get("song_max"),
        "board_state": got.get("board_state"),
        "bench_slots": got.get("bench_slots"),
        "unit_score": split.get("unit_score") if split.get("unit_score") is not None
        else panel.get("unit_score"),
        "overall_power": panel.get("overall_power"),
        "score_bonus": split or None,
        "detail_matches": (round(got["song_est"]) == row.get("score")
                           if got.get("song_est") is not None else None),
    })
    if got.get("distribution"):
        # p10/p50/p90 / dist_mean / dist_verified of the exact run-score law (~0.5 s a row)
        import row_detail  # noqa: PLC0415

        row.update({k: v for k, v in row_detail.quantile_fields(got).items() if k != "song_max"})


# --- the real account basis -------------------------------------------------------
def _load(name: str, rel: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# the rows re-planned when a board constraint applies (the Score search's
# `ondemand.REPLAN_CANDIDATES`)
REPLAN_CANDIDATES = 24   # `ondemand.REPLAN_CANDIDATES` (20 rows shown)
# the rows the re-plan picks its REPLAN_CANDIDATES from (`ondemand.REPLAN_SCREEN`)
REPLAN_SCREEN = 96


def _screen_rows(rows, reps, bonus, objective, pool):
    """The best REPLAN_SCREEN rows re-ranked by the event objective on their score
    with the activation nodes chosen jointly (each keeps the better of the two), so
    the REPLAN_CANDIDATES the exact plan gets are the ones its staircase favours
    (the Score search's `_screen_for_replan`). Only the order changes: the served
    numbers are the re-planned ones."""
    from hbuilder.domain.team import Team  # noqa: PLC0415

    head = rows[:REPLAN_SCREEN]
    teams = [Team(leader=r["leader"], members=tuple(reps[i] for i in r["members"])) for r in head]
    refined = pool.batch_score(teams, refine_activation=True)
    keyed = []
    for r, v in zip(head, refined):
        e, p = _aggregate(bonus, r["members"])
        s = max(float(r["score"]), float(v))
        keyed.append((mar.event_objective(s, objective=objective, live_permil=e, song_permil=p), r))
    keyed.sort(key=lambda t: -t[0])
    return [r for _o, r in keyed] + list(rows[REPLAN_SCREEN:])


def _refine_rows(rows, reps, bonus, objective, pool, n):
    """The best `n` rows re-scored with the activation nodes chosen jointly for the song
    (`refine_activation`, as the Score search refines its best teams), each keeping the
    better of its two scores, and re-ranked by the event objective. A refined row is
    marked `refined` so its detail is computed on the same leaf."""
    from hbuilder.domain.team import Team  # noqa: PLC0415

    head = rows[:n]
    teams = [Team(leader=r["leader"], members=tuple(reps[i] for i in r["members"])) for r in head]
    refined = pool.batch_score(teams, refine_activation=True)
    out = []
    for r, v in zip(head, refined):
        if float(v) > float(r["score"]):
            e, p = _aggregate(bonus, r["members"])
            r = {**r, "score": float(v), "refined": True,
                 "objective": mar.event_objective(float(v), objective=objective, live_permil=e, song_permil=p)}
        out.append(r)
    out.sort(key=lambda r: -float(r["objective"]))
    return out + list(rows[n:])


def _replan_rows(rows, reps, bonus, objective, song, assumptions, pool, check, progress=None,
                 singers=frozenset(), cancel=None):
    """The best REPLAN_CANDIDATES rows re-planned exactly under `assumptions`
    (`materials_plan.plan_and_score` in the pool's workers, `materials_plan.dispatch`:
    a set `cancel` stops the plans), re-scored on that board and re-sorted by the
    objective. Returns `(rows, [why a row could not be planned])`; a row that cannot be
    planned is dropped (the account cannot reach its board)."""
    import materials_plan  # noqa: PLC0415

    import ondemand as _od  # noqa: PLC0415

    # the best rows, plus the singer-led ones the quick plan undervalues (`ondemand.replan_pick`)
    cands = [rows[k] for k in _od.replan_pick(rows, lambda r: _chr_of(r["leader"].id) in singers,
                                              n=REPLAN_CANDIDATES)]
    # each with the plan this process already holds for it (`materials_plan.carried_plan`)
    items = [(k, r["leader"].id, tuple(reps[i].id for i in r["members"]), song, None,
              assumptions.plan_opts(), materials_plan.carried_plan(
                  r["leader"].id, tuple(reps[i].id for i in r["members"]), song,
                  assumptions.plan_opts()))
             for k, r in enumerate(cands)]
    # the singers' plans (the longest) first, as `ondemand._within_materials` does
    items.sort(key=lambda it: _chr_of(it[1]) not in singers)
    item_of = {it[0]: it for it in items}        # by index, not position (sorted above)
    workers = getattr(pool, "_pool", None)
    if workers is None:
        raise RuntimeError("re-planning the boards needs the worker pool")
    got: dict[int, float] = {}
    failed: list[str] = []
    for done, (index, value, _state, reason, held) in enumerate(
            materials_plan.dispatch(pool, items, cancel), 1):
        check()
        if progress:
            progress(done, len(items))
        if value is None:
            failed.append(f"{cands[index]['leader'].member}: {reason}")
        else:
            got[index] = float(value)
            # the row's detail opens on this same plan: held here, it is not solved again
            materials_plan.remember(*item_of[index][1:4], item_of[index][5], held)
    check()                 # `dispatch` returns early on a cancel: raise it here
    out = []
    for k, r in enumerate(cands):
        if k in got:
            e, p = _aggregate(bonus, r["members"])
            out.append(dict(r, score=got[k], live_permil=e, song_permil=p, source="replanned",
                            objective=mar.event_objective(got[k], objective=objective,
                                                          live_permil=e, song_permil=p)))
    if not out:
        raise KeyError("no team could be planned under these board options: " + "; ".join(failed[:3]))
    out.sort(key=lambda r: -r["objective"])
    return out, failed


def materials_plan_for(row, reps, song, assumptions):
    """`(team board state, bench)` of a re-planned row (for its detail), or None."""
    import materials_plan  # noqa: PLC0415

    got = materials_plan.plan_for(row["leader"].id, [reps[i].id for i in row["members"]],
                                  song, assumptions.plan_opts())
    return None if got is None else (got[0], got[1])


class _AtBloom(dict):
    """A `blooms` mapping that reads every card at one bloom (the maxed basis)."""

    def __init__(self, bloom: int) -> None:
        super().__init__()
        self.bloom = bloom

    def get(self, _key, _default=None):
        return self.bloom


def run_account_search(opts: Mapping, *, emit: Callable[[dict], None], cancel: threading.Event,
                       event: "mar.MarathonEvent", collection: Mapping[str, Mapping],
                       song_info: Mapping, workers: int | None = None) -> dict:
    """The job body: build the account basis for the song, run `search_event` on a
    RefinePool, and return the result payload. Raises EventCancelled on cancel."""
    import two_stage  # noqa: PLC0415
    import two_stage_build as tsb  # noqa: PLC0415

    song, difficulty, objective = opts["song"], opts["difficulty"], opts["objective"]
    highlight = is_highlight(song)
    pool_ids: list[str] = []
    pool_skipped: list[str] = []
    if highlight:
        # the pool of the chapter's holomems (`highlight_pool`); `song_info` carries the
        # music rows and the charts on disk at this difficulty (the server's reads)
        pool_ids, pool_skipped = highlight_pool(event_holomems(event), song_info.get("music_rows") or (),
                                                song_info.get("charted"))
        if not pool_ids:
            raise KeyError("Highlight: no song of the event holomems has a chart at "
                           f"{difficulty} on disk")
    # the song the leaf is built on (Highlight: the pool's first, its charts swapped in below)
    build_song = pool_ids[0] if highlight else song
    blooms = {k: (v or {}).get("bloom") for k, v in collection.items()}
    cache: dict[str, tuple[int, int]] = {}

    def bonus_of(card) -> tuple[int, int]:
        if card.id not in cache:
            # Highlight: no song-score bonus (P = 0, HIGHLIGHT_NOTES)
            cache[card.id] = mar.card_bonus_table([card], event, blooms=blooms,
                                                  music_id=None if highlight else song)[0]
        return cache[card.id]

    def check():
        if cancel.is_set():
            raise EventCancelled()

    t_start = time.perf_counter()
    emit({"type": "stage", "stage": "build", "state": "active"})
    import ondemand  # noqa: PLC0415

    # the Score search's levers: the account as it is by default, each assumption a
    # switch (`ondemand.Assumptions`); no `assumptions` -> the basis this route
    # always used (each team's boards planned on its real points, bench as captured)
    raw_a = opts.get("assumptions")
    assumptions = ondemand.parse_assumptions(raw_a) if raw_a is not None else None
    if assumptions is not None and assumptions.bloom != "account":
        # an assumed bloom reaches the Event Bonus too, not only the score: a bonus row
        # gated on bloom matches the card's bloom (at bloom 5, 73 of 117 owned cards on
        # one account have another live bonus than at their real bloom). The same
        # override the search's cards and /api/card-states use; `bonus_of` reads it.
        from hbuilder.cli import _override_collection_levels  # noqa: PLC0415

        assumed = _override_collection_levels({"cards": dict(collection)}, None, assumptions.bloom,
                                              keep_higher_bloom=assumptions.keep_higher)
        blooms = {k: (v or {}).get("bloom") for k, v in (assumed.get("cards") or {}).items()}
    maxed = opts.get("basis") == "maxed"
    if maxed:
        # the maxed basis (as the Score page's Maxed account): every 5-star card of the game at
        # max level and BLOOM 5 -- so every card's Event Bonus is read at bloom 5 -- and every
        # holomem's complete board. Its pool keeps one card per feature set; a card collapsed
        # into another that carries a different Event Bonus is put back, as `split_key` does
        blooms = _AtBloom(5)
        mfp = tsb._load("_mfp_ondemand", "tools/recon/probes/members_first_2026_08_19.py")
        built = dict(tsb.leaf_inputs("maxed", difficulty, build_song, mfp=mfp), mfp=mfp)
        by_id = built["by_id"]
        back = [by_id[d] for d, rep in built.get("dominated") or ()
                if d in by_id and rep in by_id and bonus_of(by_id[d]) != bonus_of(by_id[rep])]
        built["reps"] = sorted([*built["reps"], *back], key=lambda c: c.id)
    elif assumptions is not None:
        built = ondemand.build_inputs(assumptions, difficulty, build_song, split_key=bonus_of)
    else:
        acct = _load("_two_stage_account_event", "tools/recon/probes/two_stage_account.py")
        built = acct.build(tsb._namespace(build_song, difficulty), bench="captured", split_key=bonus_of)
    check()
    # scored for how the run is played: Auto = the AUTO column AND no combo (`ondemand.with_play`)
    built = dict(built, ctx=ondemand.with_play(built["ctx"], (opts.get("platform") or "mobile",
                                                              bool(opts.get("auto")))))
    proxy, chart = built["proxy"], built["chart"]
    mf = built["mfp"].mf
    singers = tuple(chart.singers or ())
    pool_charts: tuple = ()
    if highlight:
        # every chart of the pool in the leaf's context, a board per chart: a batch with
        # no chart_indices scores the MEAN over them (`RefinePool.batch_score`), as the
        # Score search's In general does (`ondemand.Engine`)
        library = built["ex"].load_charts(str(tsb.charts_path(difficulty))).at(difficulty)
        ctx, pool_charts, lost = highlight_context(built["ctx"], library, pool_ids)
        pool_skipped = sorted(set(pool_skipped) | set(lost))
        if not pool_charts:
            raise KeyError(f"Highlight: no chart of the pool loads at {difficulty}")
        built = dict(built, ctx=ctx)
        singers = tuple(dict.fromkeys(s for c in pool_charts for s in (c.singers or ())))
    # Highlight on a sample: the search's charts (indices into the pool), None = every chart
    hl_sample = None
    if highlight and opts.get("hl_sample", True) and len(pool_charts) > HIGHLIGHT_SAMPLE:
        at = {id(c): k for k, c in enumerate(pool_charts)}
        hl_sample = tuple(at[id(c)] for c in two_stage.chart_sample(pool_charts, HIGHLIGHT_SAMPLE))
    # the pins (`ondemand.Filters`): banned cards leave the pool, the leader filters
    # narrow the leaders, required cards are forced in, no-overlap is a legality rule
    filters = ondemand.parse_filters(opts.get("filters") or {})
    excluded = set(filters.exclude)
    reps = [c for c in built["reps"] if c.id not in excluded]
    hol = tuple(c.member for c in reps)
    position = {c.id: k for k, c in enumerate(reps)}
    missing = [c for c in filters.require if c not in position]
    if missing:
        raise KeyError(f"required cards not in this pool: {missing}")
    leaders = [c for c in reps
               if (not filters.leader_card or c.id == filters.leader_card)
               and (not filters.leader_member or c.member == filters.leader_member)]
    if not leaders:
        raise KeyError("no card in the pool matches the leader filter")
    search_options: dict = {}
    if filters.require:
        search_options["require"] = [position[c] for c in filters.require]
    if filters.no_leader_overlap:
        def allowed(li, s, leaders=leaders, pool=reps):
            return all(pool[i].member != leaders[li].member for i in s)
        search_options["allowed"] = allowed
    singing = [c for c in leaders if _chr_of(c.id) in singers]
    bonus = [bonus_of(c) for c in reps]
    emit({"type": "stage", "stage": "build", "state": "done",
          "pool": len(reps), "carriers": sum(b[0] > 0 for b in bonus),
          "song_carriers": sum(b[1] > 0 for b in bonus),
          "notes": (round(sum(len(c.notes) for c in pool_charts) / len(pool_charts))
                    if highlight else len(chart.notes)),
          **({"charts": len(pool_charts)} if highlight else {}),
          "seconds": round(time.perf_counter() - t_start, 1)})

    from hbuilder.solver.parallel_refine import RefinePool  # noqa: PLC0415

    evaluated = [0]
    best: list[dict | None] = [None]
    last = [time.monotonic()]
    current = ["score_search"]

    def on_stage(name: str, state: str, info: dict) -> None:
        current[0] = name
        emit(dict({"type": "stage", "stage": name, "state": state}, **info))

    with RefinePool(built["ctx"], workers=workers,
                    on_fallback=lambda line: print(f"  ! {line}", flush=True)) as pool:
        sampled = [hl_sample is not None]

        def batch(teams):
            return pool.batch_score(teams, chart_indices=hl_sample if sampled[0] else None)

        def score(leader, sets):
            check()
            sets = list(sets)
            out = mf.score_sets(leader, reps, sets, batch)
            evaluated[0] += len(sets)
            for value, members in zip(out, sets):
                if best[0] is None or float(value) > best[0]["value"]:
                    best[0] = {"cards": [leader.id, *[reps[i].id for i in members]],
                               "value": float(value), "metric": "score", "stage": current[0]}
            if time.monotonic() - last[0] >= 1.0:
                last[0] = time.monotonic()
                # `best_so_far`: the best RAW score scored so far (provisional; the
                # event objective re-ranks it) -- job_progress adds eta_s / elapsed
                emit({"type": "progress", "stage": current[0], "evaluated": evaluated[0],
                      "best_so_far": dict(best[0]) if best[0] else None})
            check()
            return out

        score(proxy, two_stage.random_legal_sets(hol, 64, random.Random(0)))
        # Highlight: the exact plan within materials is per song (one board for one chart),
        # so it is not run; the bound's certification neither (HIGHLIGHT notes)
        replan = assumptions is not None and assumptions.replan and not highlight
        found = search_event(reps=reps, leaders=leaders, score=score, bonus=bonus,
                             objective=objective, base_leader=proxy, always=singing,
                             gate_of=mf.gate_of,
                             top=(max(opts["top"], REPLAN_SCREEN) if replan
                                  else HIGHLIGHT_RESCORE if sampled[0] else opts["top"]),
                             on_stage=on_stage,
                             certify=bool(opts.get("certify")) and not replan and not highlight,
                             search_options=search_options)
        if sampled[0]:
            # the sample only chose the candidates: each is scored on every chart of the
            # pool, and that full mean is what is ranked and served
            on_stage("rescore", "active", {"teams": len(found["rows"])})
            sampled[0] = False
            by_leader: dict = {}
            for r in found["rows"]:
                by_leader.setdefault(id(r["leader"]), []).append(r)
            for group in by_leader.values():
                check()
                full = score(group[0]["leader"], [tuple(r["members"]) for r in group])
                for r, raw in zip(group, full):
                    r["score"] = float(raw)
                    r["objective"] = mar.event_objective(float(raw), objective=objective,
                                                         live_permil=r["live_permil"],
                                                         song_permil=r["song_permil"])
            found["rows"].sort(key=lambda r: -r["objective"])
            # then the rows the unsampled search hands on (`search_event`): the head, and
            # the singer-led rows below it -- the refine below takes REFINE_HEAD of them
            head = max(opts["top"], 8)
            sing = {id(c) for c in singing}
            found["rows"] = found["rows"][:head] + [r for r in found["rows"][head:]
                                                    if id(r["leader"]) in sing][:SINGER_ROWS]
            on_stage("rescore", "done", {})
        planned: dict[int, dict] = {}
        replan_failed: list[str] = []
        if replan:
            # a board constraint the quick plan lacks (own materials, reset cap,
            # branches, targets): the best rows re-planned exactly under it, re-scored
            # on that board, re-ranked by the event objective (the Score search's
            # `_within_materials`, on the event's own leaf)
            on_stage("replan", "active", {})
            found["rows"] = _screen_rows(found["rows"], reps, bonus, objective, pool)
            check()
            found["rows"], replan_failed = _replan_rows(
                found["rows"], reps, bonus, objective, song, assumptions, pool, check,
                progress=lambda k, n: emit({"type": "progress", "stage": "replan", "done": k, "total": n}),
                singers=frozenset(singers), cancel=cancel)
            found["bound"] = None
            for r in found["rows"][:DETAIL_ROWS]:
                whole = materials_plan_for(r, reps, song, assumptions)
                if whole is not None:
                    planned[id(r)] = whole
            on_stage("replan", "done", {"failed": len(replan_failed)})
        # `bound` is always a dict (search_event builds it with certify off too): only
        # a CERTIFIED answer keeps the leaf its bound was proved on. Testing the dict
        # itself would skip this refine on every event search (it is worth +0.86 % on
        # m0020's #1, the Score goal's #1 under the same options)
        elif (not (assumptions is not None and assumptions.boards == "as_is")
              and not (found.get("bound") or {}).get("certified")):
            # boards planned without a wallet limit: the activation nodes are still chosen
            # for THIS song (the Score search does the same, `ondemand.REFINE_CAP`); boards
            # as they are have nothing to choose, and a certified answer keeps the leaf its
            # bound was proved on
            import ondemand as _od  # noqa: PLC0415

            on_stage("refine", "active", {})
            found["rows"] = _refine_rows(found["rows"], reps, bonus, objective, pool,
                                         max(opts["top"], _od.REFINE_HEAD))
            check()
            on_stage("refine", "done", {})

    seconds = None if highlight else song_info.get("seconds")
    length = length_permil_estimate(HIGHLIGHT_SECONDS if highlight else seconds, opts["boosts"])
    def card_state(card_id: str) -> Mapping:
        return collection.get(card_id) or {}

    rows = [row_payload(k, r, reps=reps, bonus_of=bonus_of, card_state=card_state,
                        boosts=opts["boosts"], length_permil=length, holopass=opts["holopass"])
            for k, r in enumerate(found["rows"][:opts["top"]], 1)]
    # ONE number set per row (row_detail): each shown team re-developed on the
    # leaf that scored it (plain, no activation refinement — the event leaf),
    # for its slot order, board, deck power and Unit Score panel.
    # only the first DETAIL_ROWS here (~3-4 s a row, one process); the page asks
    # /api/row-detail for any other row it opens
    # Highlight: a row's detail (slot order, board, Unit Score panel) is one chart's: none
    n_detail = 0 if highlight else min(len(rows), DETAIL_ROWS)
    emit({"type": "stage", "stage": "detail", "state": "active", "total": n_detail})
    for k, (row, r) in enumerate(zip(rows[:n_detail], found["rows"][:n_detail])):
        check()
        on = built
        if id(r) in planned:
            # the row's own re-planned boards, as the Score search's detail does
            on = dict(built, ctx=dataclasses.replace(built["ctx"], board_state=planned[id(r)][0],
                                                     bench_characters=planned[id(r)][1],
                                                     socket_bench=True))
        add_row_detail(row, on, r["leader"], [reps[i] for i in r["members"]],
                       with_distribution=k < QUANTILE_ROWS, refine=bool(r.get("refined")))
        emit({"type": "progress", "stage": "detail", "done": k + 1, "total": n_detail})
    emit({"type": "stage", "stage": "detail", "state": "done"})
    titles = song_info.get("titles") or {}
    highlight_block = None
    highlight_notes: list[str] = []
    if highlight:
        highlight_block = {
            "holomems": event_holomems(event), "length_permil": length,
            "pool": [{"music_id": c.music_id, "title": titles.get(c.music_id, c.music_id),
                      "singer": (song_info.get("singers") or {}).get(c.music_id, ""),
                      "notes": len(c.notes)} for c in pool_charts],
            "skipped": [{"music_id": m, "title": titles.get(m, m)} for m in pool_skipped],
            "draw": "uniform (assumed)", "score": "mean over the pool's charts"}
        highlight_notes = list(HIGHLIGHT_NOTES) + [
            f"Highlight pool: {len(pool_charts)} songs at {difficulty}"
            + ("; skipped (no chart at this difficulty on disk): "
               + ", ".join(titles.get(m, m) for m in pool_skipped) if pool_skipped else "") + ".",
            "Highlight: a row's detail (slot order, board, Unit Score panel, skill uptime) is the "
            "team on one pool song, chosen in the row; the score at the top is the mean over the pool."]
        if hl_sample is not None:
            highlight_notes.append(
                f"Highlight: the search ranked by the mean over {len(hl_sample)} songs of the pool, "
                f"then its best {HIGHLIGHT_RESCORE} teams were scored on all {len(pool_charts)}; the "
                "ranking shown is that full mean (checked: the same top teams as searching every "
                "song, in about half the time).")
        if assumptions is not None and assumptions.replan:
            highlight_notes.append("Highlight: the exact board plan within your materials is per "
                                   "song and was NOT run; boards are the quick plan, per chart.")
        if opts.get("certify"):
            highlight_notes.append("Highlight: the bound's certification is per song and was not run.")
    return {
        "event": event.name, "objective": objective, "song": song,
        "highlight": highlight_block,
        "song_info": ({"music_id": HIGHLIGHT, "title": "Highlight", "seconds": None, "singer": "",
                       "notes": round(sum(len(c.notes) for c in pool_charts) / len(pool_charts)),
                       "charts": len(pool_charts)} if highlight else
                      {"music_id": song, "title": song_info.get("title") or song,
                       "seconds": seconds, "singer": song_info.get("singer") or "",
                       "notes": len(chart.notes)}),
        "difficulty": difficulty,
        "boosts": opts["boosts"], "holopass": opts["holopass"],
        "basis": ("Maxed account: every five-star card in the game at max level and bloom 5, "
                  "every holomem at max level with its Holomem Board complete" if maxed
                  else assumptions.describe() if assumptions is not None
                  else "account: every owned card at its real level and bloom, real board points"),
        "assumption_notes": (assumptions.notes() if assumptions is not None else [])
        + [f"Could not be re-planned: {line}" for line in replan_failed[:5]],
        "pool": len(reps), "dominated": len(built.get("dominated") or []),
        "seconds_song": seconds, "length_permil": length,
        "length_multiplier_estimated": not highlight,
        "length_formula": (f"Highlight: {LENGTH_FORMULA.split(';')[0]} with sec = {HIGHLIGHT_SECONDS}"
                           if highlight else LENGTH_FORMULA),
        "notes": highlight_notes + [EP_SCORE_NOTE,
                  "Event Pt per play = ceil(ceil((120 + score/15000 [+ room MULTI SCORE/60000 on a "
                  "Point Rally event, /75000 on a Spotlight one, in multi])(1 + E/1000)) x boost count "
                  "x L x (2 with HoloPass)); boost counts 8,16,24,30,37,42,46,50,53,56 (from "
                  "game results; the multi term from 3 exact Lives, its divisor by event kind assumed)."],
        "rows": rows, "best_score": found["best_score"], "bound": found["bound"],
        "stats": {k: {kk: vv for kk, vv in v.items() if isinstance(vv, (int, float, str))}
                  for k, v in found["stats"].items()},
        "elapsed_s": round(time.perf_counter() - t_start, 1),
    }


def chapter_windows(meta: Mapping | None) -> dict[str, tuple[int, int]]:
    """`{chapter id: (start_ms, end_ms)}` from `event_meta.json`: a chapter starts when the
    one before it ends (the first at the event's start) -- a Spotlight event's chapters run
    one after the other, each with its own holomem, bonuses, song and ranking."""
    import datetime as _dt  # noqa: PLC0415

    def ms(iso):
        try:
            return int(_dt.datetime.fromisoformat(str(iso)).timestamp() * 1000)
        except (TypeError, ValueError):
            return None
    out: dict[str, tuple[int, int]] = {}
    for ev in (meta or {}).get("events") or []:
        start = ms(ev.get("starts_at"))
        for ch in sorted(ev.get("chapters") or [], key=lambda c: c.get("number") or 0):
            end = ms(ch.get("ends_at"))
            if ch.get("id") and start is not None and end is not None:
                out[ch["id"]] = (start, end)
                start = end + 1000
    return out


def event_chapters(meta: Mapping | None, key: str, open_keys: Sequence[str],
                   raw: Mapping | None = None) -> list[dict]:
    """`[{key, number, title, chr, starts_at_ms, ends_at_ms, now, open}]` for the chapters of
    the event `key` belongs to, in order (`chapter_windows` gives the windows). `chr` is the
    chapter's Event Bonus holomem (its live group's character-only row) -- the face the page
    shows on the chapter's tab."""
    face: dict[str, str] = {}
    for group in (raw or {}).get("groups") or []:
        if group.get("kind") != "live":
            continue
        for row in group.get("rows") or []:
            if row.get("character_id") and not row.get("card_id"):
                face.setdefault(mar._event_key(group.get("group_id", "")), row["character_id"])
    import time as _time  # noqa: PLC0415

    event_id = key.rsplit("-chapter-", 1)[0]
    windows = chapter_windows(meta)
    now = int(_time.time() * 1000)
    out = []
    for ev in (meta or {}).get("events") or []:
        if ev.get("event_id") != event_id:
            continue
        for ch in sorted(ev.get("chapters") or [], key=lambda c: c.get("number") or 0):
            w = windows.get(ch.get("id"))
            out.append({"key": ch.get("id"), "number": ch.get("number"), "title": ch.get("title"),
                        "chr": face.get(ch.get("id")),
                        "starts_at_ms": w[0] if w else None, "ends_at_ms": w[1] if w else None,
                        "now": bool(w and w[0] <= now <= w[1]), "open": ch.get("id") in open_keys})
    return out


def bundle_from_raw(raw: Mapping, event_key: str | None = None, *, windows=None,
                    now_ms: int | None = None):
    """`(raw, MarathonEvent, open keys)` from marathon_bonus.json's parsed content —
    the same selection as `marathon.load_current_event` (newest open chapter by
    default), without the file. Raises `SchemaError` when nothing is open."""
    keys = sorted({mar._event_key(g) for g in raw.get("current_group_ids") or []})
    if not keys:
        raise mar.SchemaError("no event is open at the last refresh (current_group_ids is empty)")
    chosen = event_key or mar.pick_chapter(keys, windows, now_ms)
    if chosen not in keys:
        raise mar.SchemaError(f"event {chosen!r} is not open; open: {keys}")
    rows: dict[str, list] = {}
    for group in raw.get("groups") or []:
        if mar._event_key(group.get("group_id", "")) != chosen:
            continue
        channel = mar._KIND_CHANNEL.get(group.get("kind"))
        if channel is None:
            continue
        for row in group.get("rows") or []:
            rows.setdefault(channel, []).append(mar.MarathonBonusRow(
                permil=int(row["permil"]), character_id=row.get("character_id"),
                card_id=row.get("card_id"), rarity=row.get("rarity"), bloom=row.get("bloom"),
                music_id=row.get("music_id"), attribute=row.get("attribute")))
    event = mar.MarathonEvent(name=chosen, rows_by_channel={k: tuple(v) for k, v in rows.items()})
    return raw, event, keys


def load_event_bundle(path: Path, event_key: str | None = None):
    """`(raw json, MarathonEvent, open keys)` or raises FileNotFoundError / SchemaError.
    Without `event_key`, the chapter open NOW (`event_meta.json` beside it gives the windows)."""
    import time as _time  # noqa: PLC0415

    meta_path = Path(path).parent / "event_meta.json"
    try:
        windows = chapter_windows(json.loads(meta_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        windows = {}
    return bundle_from_raw(json.loads(Path(path).read_text(encoding="utf-8")), event_key,
                           windows=windows, now_ms=int(_time.time() * 1000))
