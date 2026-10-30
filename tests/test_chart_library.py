"""Loading real charts: the file boundary `domain/song.py` does not have.

`score_song` scores a team on a concrete `SongChart`; these pin the loader that
builds those charts from disk, and the rules that stop a chart library being
wrong in a way a score would hide.

The one that matters is the **key/contents agreement** check. A library is a
lookup, and a chart filed under the wrong key is not a loud failure: `get`
misses it, the song silently drops out of a "best across everything" answer,
and the number that comes back is a mean over 175 songs presented as 176.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hbuilder.domain.song import (
    ChartLibrary,
    ChartNote,
    SongError,
    charts_from_mapping,
    load_charts,
)

_DUMPS = Path(__file__).resolve().parents[1] / "tools" / "recon" / "proxy" / "dumps"


def _chart(music_id="m0001", difficulty="EXPERT", **over):
    row = {
        "music_id": music_id,
        "difficulty": difficulty,
        "difficulty_level": 26,
        "live_score_coefficient_permil": 4,
        "notes": [[1.0, 1000], [2.0, 1000], [3.5, 500]],
        "special_cues": [1.5, 2.5, 3.0, 3.2, 3.4],
    }
    row.update(over)
    return row


def _document(**charts):
    return {
        "schema_version": "charts-1",
        "charts": {ChartLibrary.key(c["music_id"], c["difficulty"]): c
                   for c in charts.values()},
    }


# ==========================================================================
# 1. The happy path
# ==========================================================================


def test_a_chart_round_trips_into_a_scorable_SongChart():
    library = charts_from_mapping(_document(a=_chart()))
    assert len(library) == 1
    chart = library.get("m0001")
    assert chart is not None
    assert chart.difficulty == "EXPERT"
    assert chart.notes == (
        ChartNote(1.0, 1000), ChartNote(2.0, 1000), ChartNote(3.5, 500)
    )
    assert chart.special_cues == (1.5, 2.5, 3.0, 3.2, 3.4)


def test_difficulty_is_case_insensitive_on_the_way_in_and_out():
    """The caches spell it `expert`, the master spells it `EXPERT`, and a
    library that answered only one of them would look empty for no reason."""
    library = charts_from_mapping(_document(a=_chart(difficulty="expert")))
    assert library.get("m0001", "EXPERT") is not None
    assert library.get("m0001", "expert") is not None
    assert library.at("expert") == library.at("EXPERT")


def test_at_returns_only_the_asked_difficulty_in_music_id_order():
    """EXPERT is the default because the ranking mode is always Expert; a mode
    that averaged four difficulties would answer a question nobody asked."""
    library = charts_from_mapping(_document(
        b=_chart("m0002", "EXPERT"),
        a=_chart("m0001", "EXPERT"),
        c=_chart("m0001", "HARD"),
    ))
    assert [c.music_id for c in library.at()] == ["m0001", "m0002"]
    assert [c.music_id for c in library.at("HARD")] == ["m0001"]
    assert library.music_ids == ("m0001", "m0002")


# ==========================================================================
# 2. The failures that would otherwise become a plausible wrong number
# ==========================================================================


def test_a_key_that_disagrees_with_its_contents_is_refused():
    """The quiet one. `get` would miss it, the song would drop out of a
    "best across everything" answer, and the result would be a mean over 175
    charts reported as 176."""
    document = {"schema_version": "charts-1", "charts": {"m9999/EXPERT": _chart()}}
    with pytest.raises(SongError, match="m0001/EXPERT"):
        charts_from_mapping(document)


def test_every_failure_names_the_chart_it_came_from():
    """A library is thousands of numbers from a pipeline. "invalid chart"
    without a key is not a diagnosis, it is a search."""
    with pytest.raises(SongError, match=r"m0001/EXPERT"):
        charts_from_mapping(_document(a=_chart(notes=[[1.0, "not a number"]])))
    with pytest.raises(SongError, match=r"m0001/EXPERT"):
        charts_from_mapping(_document(a=_chart(difficulty_level="twenty-six")))


def test_a_chart_with_no_notes_or_unsorted_notes_is_refused_by_name():
    """`SongChart.__post_init__` already refuses both; the loader must let that
    through with the key attached rather than swallowing it."""
    with pytest.raises(SongError, match="m0001/EXPERT"):
        charts_from_mapping(_document(a=_chart(notes=[])))
    with pytest.raises(SongError, match="m0001/EXPERT"):
        charts_from_mapping(_document(a=_chart(notes=[[5.0, 1000], [1.0, 1000]])))


def test_a_malformed_document_says_what_it_expected():
    with pytest.raises(SongError, match="must be an object"):
        charts_from_mapping([])
    with pytest.raises(SongError, match="missing object key 'charts'"):
        charts_from_mapping({"schema_version": "charts-1"})


def test_an_absent_file_is_None_not_an_error(tmp_path):
    """The same first-class-state contract `load_board` has: no charts is a
    refusal to make at the CLI, not an exception to raise here."""
    assert load_charts(tmp_path / "nope.json") is None


def test_an_unreadable_file_names_the_path(tmp_path):
    path = tmp_path / "charts.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SongError, match="unreadable chart file"):
        load_charts(path)


# ==========================================================================
# 3. Against the real charts
# ==========================================================================


def test_the_real_library_loads_and_every_chart_carries_five_special_cues():
    """`song.SPECIAL_CUE_COUNT`: channel #mmm0B carries exactly five cues on
    every chart except the tutorial song's four.
    The EXPERT library must therefore be 5/5 with no exceptions — if it is not,
    the cue channel was misread and every SPECIAL timing is wrong."""
    path = _DUMPS / "charts.json"
    if not path.exists():
        pytest.skip("dumps/ is gitignored; run build_charts.py to produce charts.json")

    library = load_charts(path)
    assert library is not None and len(library) > 0
    for key, chart in library.charts.items():
        assert len(chart.special_cues) == 5, key
        assert chart.difficulty == "EXPERT", key
        assert chart.notes, key
        assert list(chart.special_cues) == sorted(chart.special_cues), key

    # The library's own key must find every chart it holds.
    for chart in library.charts.values():
        assert library.get(chart.music_id, chart.difficulty) is chart


def test_the_real_library_is_scorable_end_to_end():
    """The point of the whole boundary: a chart off disk goes through
    `score_song` and produces a number, without a test having hand-built it.

    The cards come from the FIXTURE, not from `data/` — SCHM-03's rule, and
    this test does not care which cards they are, only that a real chart can be
    scored by real `Card`s without one being hand-built."""
    path = _DUMPS / "charts.json"
    if not path.exists():
        pytest.skip("dumps/ is gitignored; run build_charts.py to produce charts.json")

    from conftest import FIXTURE_CARDS_PATH, make_scoring_config
    from hbuilder.domain.loader import load_cards
    from hbuilder.domain.song import score_song

    library = load_charts(path)
    cards = load_cards(FIXTURE_CARDS_PATH)
    chart = library.at()[0]

    scores = score_song(tuple(cards[:5]), (cards[5],), chart, make_scoring_config())
    assert len(scores) == 1
    assert scores[0].total_score > 0


# ==========================================================================
# 4. Every difficulty (Path B)
# ==========================================================================
#
# The ranking mode is always Expert, so `dumps/charts.json` is built EXPERT-only
# and `at()` defaults to it. Path B — the REAL per-song score — is asked about
# the difficulty actually played, so it reads the all-difficulty library
# `dumps/charts_all.json` (`build_charts.py --all-out`, every song x 4). These
# pin that the same loader serves it and that `at()` still refuses to average
# across difficulties, which is the failure a four-difficulty file invites.

_DIFFICULTIES = ("EASY", "NORMAL", "HARD", "EXPERT")


def test_one_song_at_four_difficulties_stays_four_separately_addressable_charts():
    library = charts_from_mapping(_document(**{
        d: _chart(difficulty=d, difficulty_level=level, notes=[[1.0, 1000]] * (i + 1))
        for i, (d, level) in enumerate(zip(_DIFFICULTIES, (5, 12, 19, 26)))
    }))
    assert len(library) == 4
    assert library.music_ids == ("m0001",)
    for i, d in enumerate(_DIFFICULTIES):
        chart = library.get("m0001", d)
        assert chart is not None and chart.difficulty == d
        assert len(chart.notes) == i + 1
    # Lower-cased at the call site must still find it: the key is normalised.
    assert library.get("m0001", "easy") is library.get("m0001", "EASY")


def test_at_a_difficulty_never_mixes_difficulties():
    """`at()` is what the across-songs mode iterates. If a four-difficulty file
    made it return four charts per song, "best across every song" would silently
    become "best across every chart" — the same song counted four times, and
    weighted towards whatever difficulty is easiest to score."""
    library = charts_from_mapping(_document(**{
        f"{song}{d}": _chart(music_id=song, difficulty=d)
        for song in ("m0001", "m0002") for d in _DIFFICULTIES
    }))
    assert len(library) == 8
    for d in _DIFFICULTIES:
        at = library.at(d)
        assert len(at) == 2, d
        assert {c.difficulty for c in at} == {d}
        assert [c.music_id for c in at] == ["m0001", "m0002"]


def test_the_real_all_difficulty_library_loads_and_covers_every_song():
    """Every song x 4 difficulties (the tutorial song m9999's four are
    excluded by design), every one carrying five SPECIAL cues, and its EXPERT half identical to the
    ranking library — `build_charts.py --all-out` re-checks that on every build."""
    path = _DUMPS / "charts_all.json"
    if not path.exists():
        pytest.skip("dumps/ is gitignored; run build_charts.py --all-out to produce it")

    library = load_charts(path)
    assert library is not None
    per_difficulty = {d: library.at(d) for d in _DIFFICULTIES}
    counts = {d: len(charts) for d, charts in per_difficulty.items()}
    assert len(set(counts.values())) == 1, f"ragged difficulty coverage: {counts}"
    assert len(library) == sum(counts.values())
    songs = set(library.music_ids)
    for d, charts in per_difficulty.items():
        assert {c.music_id for c in charts} == songs, d
        for chart in charts:
            assert len(chart.special_cues) == 5, (d, chart.music_id)
            assert chart.notes, (d, chart.music_id)

    # An EASY chart is a different chart, not a copy: fewer notes, lower level.
    for song in sorted(songs)[:20]:
        easy, expert = library.get(song, "EASY"), library.get(song, "EXPERT")
        assert easy.difficulty_level <= expert.difficulty_level, song
        assert len(easy.notes) <= len(expert.notes), song


def test_the_all_difficulty_library_agrees_with_the_ranking_library_on_expert():
    """The two files are built by one command precisely so they cannot drift;
    this is the assertion that would catch it if they ever did. Same instant,
    same basis — a Path B score on EXPERT must be scored on the same chart the
    ranking used, note for note."""
    every, expert_only = _DUMPS / "charts_all.json", _DUMPS / "charts.json"
    if not (every.exists() and expert_only.exists()):
        pytest.skip("dumps/ is gitignored; run build_charts.py --all-out")

    all_lib, expert_lib = load_charts(every), load_charts(expert_only)
    ranked = expert_lib.at("EXPERT")
    assert ranked, "the ranking library is empty"
    for chart in ranked:
        mine = all_lib.get(chart.music_id, "EXPERT")
        assert mine == chart, chart.music_id


# ---------------------------------------------------------------------------
# The song's cast
# ---------------------------------------------------------------------------
#
# ⚠ If `charts_from_mapping` stopped reading these two fields, `build_charts.py`
# could write a perfect cast into every row and the loader would drop it on the
# floor, leaving every singer-conditional board term silently inert with
# nothing to show it: a channel computed and thrown away, where the absent
# value is also the correct value for the common case.


def test_the_loader_reads_the_songs_cast():
    library = charts_from_mapping(
        _document(a=_chart(singers=["chr-04003"], singer_type="solo"))
    )
    chart = library.get("m0001")
    assert chart.singers == ("chr-04003",)
    assert chart.singer_type == "solo"


def test_a_chart_with_no_cast_stays_absent_rather_than_empty_scoped():
    """Absent is the honest state for a chart built before casts existed, and
    it is what keeps the singer-conditional terms behaving as "cast unknown"
    rather than as "this song has no singers"."""
    chart = charts_from_mapping(_document(a=_chart())).get("m0001")
    assert chart.singers == ()
    assert chart.singer_type == ""


def test_a_half_filled_cast_is_refused_by_the_loader():
    """`SongChart.__post_init__`'s guards must reach through the loader, so a
    partially-populated file fails loudly instead of scoring on half a cast."""
    with pytest.raises(SongError):
        charts_from_mapping(_document(a=_chart(singer_type="solo")))
    with pytest.raises(SongError):
        charts_from_mapping(_document(a=_chart(singers=["chr-04003"])))
    # A FUWAMOCO duo `solo` is a COHERENT cast (two singers, attested to sum both
    # boards under one cap), so the loader accepts it rather than refusing.
    lib = charts_from_mapping(
        _document(a=_chart(singers=["chr-04016", "chr-04017"], singer_type="solo"))
    )
    assert list(lib.at("EXPERT"))[0].singers == ("chr-04016", "chr-04017")
    # Three singers under `solo`, however, is still an incoherent cast.
    with pytest.raises(SongError):
        charts_from_mapping(
            _document(a=_chart(
                singers=["chr-04016", "chr-04017", "chr-00015"], singer_type="solo"
            ))
        )


def test_the_real_chart_library_carries_the_casts_it_can_represent():
    """Every Expert chart carries a cast (solo — including the FUWAMOCO duo's
    two-singer "solo" songs — plus group) except the ALL-scope songs, whose
    `characterIds` is empty because the whole roster is implied; `_board_bonus`'s pooled `all` bucket
    already covers those. `build_charts.py` REPORTS the ALL songs rather than
    inventing a roster — see its `_casts` docstring.
    """
    path = _DUMPS / "charts.json"
    if not path.exists():
        pytest.skip("dumps/charts.json is gitignored recon data")
    library = load_charts(path)
    charts = list(library.at("EXPERT"))
    with_cast = [c for c in charts if c.singers]
    assert len(with_cast) >= 150, f"only {len(with_cast)} of {len(charts)} carry a cast"
    scopes = {c.singer_type for c in with_cast}
    assert scopes <= {"solo", "group", "all"}
    for chart in with_cast:
        assert chart.singer_type, chart.music_id
        if chart.singer_type == "solo":
            assert 1 <= len(set(chart.singers)) <= 2  # 2 = a duo act (FUWAMOCO)


# ==========================================================================
# 5. Selecting the set of charts a run is ranked on (Path B step 7)
# ==========================================================================
#
# `at()` answers "every song, one difficulty". A player does not play every
# song: they play a handful, and not always on Expert. `select()` is the one
# place that turns "these songs, this difficulty" into charts, so the CLI and
# the web cannot disagree about what a named song resolved to — and so that a
# song the library cannot serve is a NAMED refusal rather than a quietly
# shorter mean. The whole point of `test_a_chart_filed_under_the_wrong_key_is_
# refused` is that a 175-song mean must never be presented as a 176-song one.


def test_select_returns_the_named_songs_at_the_named_difficulty():
    library = charts_from_mapping(_document(**{
        f"{song}{d}": _chart(music_id=song, difficulty=d)
        for song in ("m0001", "m0002", "m0003") for d in ("EASY", "EXPERT")
    }))
    picked = library.select(("m0003", "m0001"), "EASY")
    assert [c.music_id for c in picked] == ["m0001", "m0003"]
    assert {c.difficulty for c in picked} == {"EASY"}


def test_select_with_no_songs_named_is_every_song_at_that_difficulty():
    library = charts_from_mapping(_document(**{
        f"{song}{d}": _chart(music_id=song, difficulty=d)
        for song in ("m0001", "m0002") for d in ("EASY", "EXPERT")
    }))
    assert library.select(None, "EASY") == library.at("EASY")
    assert library.select((), "EXPERT") == library.at("EXPERT")


def test_select_REFUSES_a_song_the_library_cannot_serve():
    """The failure this exists to prevent: a named song silently dropped turns
    "mean over the 5 songs you play" into a mean over 4, with nothing in the
    output to say so. The message names the song AND the difficulty, because
    "m0303 is not here" and "m0303 has no EASY chart" are different problems
    with different fixes (typo vs `build_charts.py --all-out`)."""
    library = charts_from_mapping(_document(
        a=_chart(music_id="m0001", difficulty="EXPERT")
    ))
    with pytest.raises(SongError) as absent:
        library.select(("m0001", "m9999"), "EXPERT")
    assert "m9999" in str(absent.value)

    with pytest.raises(SongError) as difficulty:
        library.select(("m0001",), "EASY")
    assert "m0001" in str(difficulty.value) and "EASY" in str(difficulty.value)


def test_select_names_every_missing_song_at_once():
    """A caller fixing a typo'd list should not have to re-run five times to
    discover five typos."""
    library = charts_from_mapping(_document(
        a=_chart(music_id="m0001", difficulty="EXPERT")
    ))
    with pytest.raises(SongError) as exc:
        library.select(("m0002", "m0003", "m0004"), "EXPERT")
    for missing in ("m0002", "m0003", "m0004"):
        assert missing in str(exc.value)


def test_select_collapses_a_repeated_song_rather_than_double_weighting_it():
    """A mean over a list that names one song twice would weight it double —
    silently, and only for whoever wrote the list that way."""
    library = charts_from_mapping(_document(
        a=_chart(music_id="m0001", difficulty="EXPERT"),
        b=_chart(music_id="m0002", difficulty="EXPERT"),
    ))
    picked = library.select(("m0001", "m0002", "m0001"), "EXPERT")
    assert [c.music_id for c in picked] == ["m0001", "m0002"]


def test_select_is_case_insensitive_about_the_difficulty():
    library = charts_from_mapping(_document(a=_chart(difficulty="EXPERT")))
    assert library.select(("m0001",), "expert") == library.select(("m0001",), "EXPERT")
