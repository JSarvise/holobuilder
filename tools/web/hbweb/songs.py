"""The song library: `GET /api/songs`, and the song reads the other routes share
(titles and singers, the Music master's rows, note counts, the difficulties on disk).

Labels and filter metadata are display-only: nothing is scored from them, so their
absence costs the page its titles or filters and never a number.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs

import certificate as cert_mod

from hbweb import basis, config

# The picker's filter tokens, from the Music master's own enums. Keyed by the
# suffix after the last underscore, because the wire spells the whole thing out
# (`MusicSingerType_MUSIC_SINGER_TYPE_SOLO`).
#
# ⚠ The wire vocabulary is SOLO / GROUP / ALL (every row of `main_Music.json`)
# -- while the page contract's tokens are
# solo / unit / whole. GROUP->unit and ALL->whole is that bijection, spelled
# out here rather than derived, so a NEW enum value degrades to "" (an honest
# "unknown") instead of leaking a raw enum suffix into a filter menu.
SINGER_TYPES = {"SOLO": "solo", "GROUP": "unit", "ALL": "whole"}
CATEGORIES = {"ORIGINAL": "original", "COVER": "cover"}


def song_labels() -> tuple[dict[str, str], dict[str, str]]:
    """`(titles, singers)` by music id, from the display-only language export.

    One reader for every route that names a song, so two views can never disagree
    about what a song is called. Not cached: the refresh button can rebuild the dump
    under a running server, and a stale title is exactly the kind of lie a cache
    tells. Absent file, two empty maps, and every label falls back to the music id
    -- a visible placeholder rather than a blank."""
    titles: dict[str, str] = {}
    singers: dict[str, str] = {}
    rows = config.read_json(config.SONG_TITLES_PATH) or []
    for row in rows if isinstance(rows, list) else []:
        key = str((row or {}).get("id") or "")
        text = ((row or {}).get("data") or {}).get("text")
        if not text:
            continue
        if key.startswith("la-music_title-"):
            titles[key[len("la-music_title-") :]] = text
        elif key.startswith("la-singer_name-"):
            singers[key[len("la-singer_name-") :]] = text
    return titles, singers


def song_titles() -> dict[str, str]:
    """Just the titles half of `song_labels`."""
    return song_labels()[0]


def music_rows() -> list[dict]:
    """The Music master's rows (`data` of each), `[]` when the file is absent."""
    rows = config.read_json(config.MUSIC_PATH)
    if not isinstance(rows, list):
        return []
    return [r.get("data") or {} for r in rows if isinstance(r, dict)]


def chart_note_counts(difficulty: str) -> dict[str, int] | None:
    """`{music id: note count}` of every chart at `difficulty`; None without the chart file."""
    raw = config.read_json(basis.charts_path(difficulty))
    if not isinstance(raw, dict):
        return None
    out: dict[str, int] = {}
    for chart in (raw.get("charts") or {}).values():
        if str((chart or {}).get("difficulty") or "").upper() == difficulty:
            out[str(chart.get("music_id") or "")] = len(chart.get("notes") or [])
    return out


def difficulties_listing() -> dict[str, Any]:
    """`/api/meta`'s `difficulties`: `{"default": "EXPERT", "available": [...],
    "charts": {difficulty: count}, "missing": {difficulty: [music ids]}}` —
    `missing` lists the songs that have an EXPERT chart but none at that
    difficulty, and is present only when some difficulty lacks a song."""
    present = cert_mod.available_difficulties(config.CHARTS_PATH, config.CHARTS_ALL_PATH)
    out: dict[str, Any] = {
        "default": "EXPERT",
        "available": [d for d in cert_mod.DIFFICULTIES if d in present],
        "charts": {d: len(present[d]) for d in cert_mod.DIFFICULTIES if d in present},
    }
    expert = set(present.get("EXPERT") or ())
    missing = {
        d: sorted(expert - set(ids)) for d, ids in present.items() if d != "EXPERT" and expert - set(ids)
    }
    if missing:
        out["missing"] = missing
    return out


def _music_by_id() -> dict[str, Mapping[str, Any]]:
    """The Music master's rows by music id (`{}` without the file)."""
    music_rows_raw = config.read_json(config.MUSIC_PATH)
    music: dict[str, Mapping[str, Any]] = {}
    for row in music_rows_raw if isinstance(music_rows_raw, list) else []:
        data = (row or {}).get("data") or {}
        music_id = str(data.get("id") or "")
        if music_id:
            music[music_id] = data
    return music


def _character_names() -> dict[str, str]:
    """chr-id -> holomem name, from the catalogue: `card-00015-...` belongs to
    `chr-00015` and carries `member`. A character no card names keeps its id as the
    label, so a gap is visible rather than blank."""
    names: dict[str, str] = {}
    for card in (config.read_json(config.CARDS_PATH) or {}).get("cards", []):
        parts = str(card.get("id") or "").split("-")
        if len(parts) >= 2 and card.get("member"):
            names.setdefault(f"chr-{parts[1]}", str(card["member"]))
    return names


def _song_row(
    chart: Mapping, difficulty: str, titles: Mapping, singers: Mapping, music: Mapping, names: Mapping
) -> dict[str, Any]:
    """One song of the listing: what makes its chart score differently, and its labels."""
    music_id = str(chart.get("music_id") or "")
    cues = list(chart.get("special_cues") or [])
    row = music.get(music_id) or {}
    character_ids = [str(c) for c in (row.get("characterIds") or []) if c]
    return {
        "music_id": music_id,
        "difficulty": difficulty,
        # `title` is the label; `titled` says whether it is the song's
        # real name or the id standing in for one. A page that could
        # not tell them apart would present `m0042` as a song title.
        "title": titles.get(music_id) or music_id,
        "titled": music_id in titles,
        "singer": singers.get(music_id) or "",
        "difficulty_level": chart.get("difficulty_level"),
        "notes": len(chart.get("notes") or []),
        "special_cues": cues,
        "singer_type": SINGER_TYPES.get(str(row.get("musicSingerType") or "").rpartition("_")[2], ""),
        "category": CATEGORIES.get(str(row.get("categoryType") or "").rpartition("_")[2], ""),
        "seconds": row.get("playingSeconds"),
        # when the song entered the game (Music.startTime, epoch ms; the
        # launch songs all carry the launch date) -- the picker's sort
        "release": int(row.get("startTime") or 0) or None,
        "character_ids": character_ids,
        "characters": [{"id": chr_id, "name": names.get(chr_id, chr_id)} for chr_id in character_ids],
    }


def serve_songs(h) -> None:
    """`GET /api/songs[?difficulty=EASY|NORMAL|HARD|EXPERT]`: the songs the page may rank
    against, with what each one is.

    ⚠ **EXPERT by default, and that is a decision rather than an accident.**
    The ranking mode is Expert, and `ChartLibrary.at` records the same reason: a picker
    that silently mixed four difficulties would offer four rows per song that score
    differently with nothing on screen saying why. Another difficulty lists that
    difficulty's charts (charts_all.json); an unknown one is a 400.

    What each song carries is what makes a chart score differently, so the
    picker can be read rather than guessed at: the difficulty level, the
    note count, and the five SPECIAL cue times. The cues are the reason
    slot order matters at all -- cue `i` fires the `i`-th member's SPECIAL
    (checked on 704/704 charts).

    Absent files are first-class, never an error: without the chart file this reports
    `available: false` with the command that builds it; without the Music master every
    song still ships the filter keys with empty values and `filters_available: false`."""
    query = parse_qs(h.path.split("?", 1)[1]) if "?" in h.path else {}
    try:
        difficulty = basis.parse_difficulty({"difficulty": (query.get("difficulty") or [""])[0]})
    except basis.HttpError as exc:
        h.reply_json(exc.code, exc.obj)
        return
    charts_file = basis.charts_path(difficulty)
    raw = config.read_json(charts_file)
    if not raw:
        h.reply_json(
            200,
            {
                "available": False,
                "reason": (
                    f"{charts_file.name} is not on disk (it lives under "
                    f"gitignored dumps/). Build it with "
                    f"tools/refresh/build_charts.py."
                ),
                # No songs, so no filters either -- but the key is always
                # present, so the page never has to guess its absence.
                "filters_available": False,
                "songs": [],
            },
        )
        return

    charts = raw.get("charts") or {}
    # The ids stay the value the solver is given either way, so a page rendered
    # without titles asks the SAME question as one rendered with them.
    titles, singers = song_labels()
    music = _music_by_id()
    names = _character_names()
    songs = [
        _song_row(charts[key] or {}, difficulty, titles, singers, music, names)
        for key in sorted(charts)
        if str((charts[key] or {}).get("difficulty") or "").upper() == difficulty
    ]

    h.reply_json(
        200,
        {
            "available": True,
            "difficulty": difficulty,
            "difficulties": list(difficulties_listing()["available"]),
            "source": raw.get("source") or str(charts_file.name),
            "titles_available": bool(titles),
            "filters_available": bool(music),
            "songs": songs,
        },
    )
