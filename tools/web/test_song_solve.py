"""`POST /solve` with `song` — the two-stage re-rank must reach the wire.

The contract: a song does NOT change how the search
runs. Stage 1 is the song-agnostic search, kept as the FILTER exactly as
`cli.py` uses it; stage 2 re-scores the kept teams on the real chart through
`rank_on_chart` — the CLI's own re-ranker — and re-orders. The payload then
carries `sorted_by`, `song`, and per-team `song_score` + `member_order`.

What these tests are FOR is the plausible-but-wrong implementation:

* a run that computed every song score, attached them all, and left the
  song-agnostic ordering in place would look completely normal on screen —
  so the ordering test forces the re-ranker to hand back an order that
  provably DIFFERS from stage 1's, and asserts the payload followed it;
* a `sorted_by` that names a field the ordering did not use is precisely the
  named failure the field exists to prevent, so it is asserted against the
  observed ordering, never against itself;
* a song-agnostic run must be a pure addition — no `song` block, no per-team
  song fields, `top` teams and not `DEFAULT_SONG_POOL` of them.

Fast by construction, like `test_terms_payload.py`: the pool is eight
synthetic cards and the chart is thirty synthetic notes, so a "solve" is
milliseconds rather than the ~7 s of the real account. The chart library is
stubbed at the LOADER seam (`load_charts`), the same pattern that file uses
for `load_inputs` — writing a fixture chart file is not an option, because
`test_no_writes.py`'s check 4 (rightly) forbids every filesystem write in
this package that is not rooted at `COLLECTION_PATH`, tests included.

    python -m pytest tools/web/test_song_solve.py
"""

from __future__ import annotations

import queue
import sys
from pathlib import Path

import pytest

import server as web_server
from hbuilder.domain.card import Stats
from hbuilder.domain.song import charts_from_mapping
from hbuilder.domain.song_rank import RankedOnSong, RankedOnSongs

# `conftest.py`'s card builders, so the pool here is made the same way every
# domain test makes one.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from conftest import make_card, make_scoring_config, make_skill  # noqa: E402

# Deliberately NOT a real music id: if this machine's real language dump were
# consulted despite the monkeypatch, a real id would pick up a real title and
# the title-fallback assertion would stop discriminating.
MUSIC_ID = "m9990"


def _chart_library():
    document = {
        "schema_version": "charts-1",
        "charts": {
            f"{MUSIC_ID}/EXPERT": {
                "music_id": MUSIC_ID,
                "difficulty": "EXPERT",
                "difficulty_level": 26,
                "live_score_coefficient_permil": 4,
                "notes": [[float(t), 1000] for t in range(1, 31)],
                "special_cues": [5.0, 10.0, 15.0, 20.0, 25.0],
            }
        },
    }
    return charts_from_mapping(document, source="test_song_solve")


def _pool(size: int = 8):
    """`size` cards with pairwise-distinct stats AND a per-card costume
    skill, so the ranking is strict.

    The costume skill is load-bearing, not decoration: with skill-less cards
    the agnostic top teams are the SAME six best cards under different
    leaders, and a leader with no costume skill contributes nothing — so the
    top scores all tie and every ordering assertion here goes vacuous (the
    first run of this file caught exactly that, 1925.0 three times over).
    The outfit's skill is the one thing a leader contributes, so a distinct
    magnitude per card makes distinct leaders score distinctly."""
    return tuple(
        make_card(
            f"card_{index}",
            member=f"Holomem{index}",
            stats=Stats(
                performance=100 + 13 * index,
                technique=90 + 7 * index,
                sense=80 + 3 * index,
            ),
            # "COSTUME" (schema.py's COSTUME_SLOT_KIND), not "costume": the
            # scorer dispatches EV functions by the exact kind string and a
            # lowercase one KeyErrors inside the solve.
            costume_skill=make_skill(
                "COSTUME",
                family="stat_up",
                effect_type="all_stats",
                magnitude=5.0 + 3.0 * index,
            ),
        )
        for index in range(size)
    )


def _run(monkeypatch, tmp_path, *, song: str, top: int = 3, library="real"):
    """Run one job through `run_solve_job` and return every event it emitted."""
    pool = _pool()
    config = make_scoring_config()
    monkeypatch.setattr(
        web_server, "load_inputs", lambda *a, **k: (pool, pool, config, None)
    )
    monkeypatch.setattr(web_server, "BOARD_DISABLED", True)
    monkeypatch.setattr(
        web_server, "COLLECTION_PATH", tmp_path / "no-such-collection.json"
    )
    # No titles file, so the song block's title must fall back to the id —
    # asserted below, and deterministic whether or not dumps/ exists here.
    monkeypatch.setattr(
        web_server, "SONG_TITLES_PATH", tmp_path / "no-such-titles.json"
    )
    if library == "real":
        chart_library = _chart_library()
        monkeypatch.setattr(
            web_server, "load_charts", lambda _path: chart_library
        )
    elif library == "absent":
        monkeypatch.setattr(web_server, "load_charts", lambda _path: None)

    job = web_server.Job(job_id="test-song")
    web_server.run_solve_job(
        job, top_n=top, art_dir=Path("does-not-exist"), song=song
    )
    events = []
    while True:
        try:
            events.append(job.events.get_nowait())
        except queue.Empty:
            break
    return events


def _payload(events):
    results = [e for e in events if e.get("type") == "result"]
    assert results, f"no result event; got {[e.get('type') for e in events]}"
    return results[-1]["payload"]


# ---------------------------------------------------------------------------
# The one that matters: the table is ORDERED by song_score
# ---------------------------------------------------------------------------


def test_song_mode_orders_by_the_rerankers_verdict_not_stage_1s(
    monkeypatch, tmp_path
):
    """A run that computed song scores and kept the agnostic ordering would
    look completely normal — every field present, every number real. So the
    re-ranker here is a stub whose verdict is the exact REVERSE of stage 1's
    order (last team handed over scores highest), and the payload must follow
    the stub, not the stage-1 order it was handed."""
    seen: dict = {}

    def contrarian_rank(teams, chart, cfg, *, filtered_from=None, progress=None):
        rows = [
            RankedOnSong(
                team=team,
                leader_id=team.leader.id,
                member_ids=tuple(sorted(card.id for card in team.members)),
                # Monotone INCREASING in stage-1 rank: the team the agnostic
                # search liked least scores the most on this "chart".
                expected_score=1000.0 * (index + 1),
                # And a slot order that is provably not sorted(member_ids),
                # so propagation is distinguishable from recomputation.
                member_order=tuple(card.id for card in reversed(team.members)),
            )
            for index, team in enumerate(teams)
        ]
        seen["input_keys"] = [(r.leader_id, r.member_ids) for r in rows]
        rows.sort(key=lambda row: -row.expected_score)
        seen["output_keys"] = [(r.leader_id, r.member_ids) for r in rows]
        seen["rows_by_key"] = {(r.leader_id, r.member_ids): r for r in rows}
        return RankedOnSongs(
            teams=tuple(rows),
            charts=(chart.music_id,),
            filtered_from=len(teams) if filtered_from is None else filtered_from,
        )

    monkeypatch.setattr(web_server, "rank_on_chart", contrarian_rank)
    payload = _payload(_run(monkeypatch, tmp_path, song=MUSIC_ID, top=3))

    # NEGATIVE CONTROL: the stub's verdict must actually differ from the
    # order it was handed, or everything below passes for the wrong
    # implementation too. With >= 2 teams the reverse always differs.
    assert seen["output_keys"] != seen["input_keys"], (
        "the contrarian stub failed to produce a different order — this "
        "fixture no longer discriminates and every assertion below is vacuous"
    )

    got = [
        (team["leader_id"], tuple(sorted(team["member_ids"])))
        for team in payload["teams"]
    ]
    assert got == seen["output_keys"][: len(got)], (
        "the payload's team order is not the re-ranker's order — a run that "
        "attaches song_score but keeps the song-agnostic ordering is exactly "
        "the silent failure this test exists to catch"
    )

    assert payload["sorted_by"] == "song_score"
    scores = [team["song_score"] for team in payload["teams"]]
    assert scores == sorted(scores, reverse=True)

    # member_order is the re-ranker's row, propagated — not recomputed, not
    # tidied into sorted order.
    for team in payload["teams"]:
        key = (team["leader_id"], tuple(sorted(team["member_ids"])))
        assert team["member_order"] == list(seen["rows_by_key"][key].member_order)


# ---------------------------------------------------------------------------
# Through the REAL re-ranker: shapes, K, and the song block
# ---------------------------------------------------------------------------


def test_the_real_reranker_round_trips_and_K_is_the_clis_own(
    monkeypatch, tmp_path
):
    events = _run(monkeypatch, tmp_path, song=MUSIC_ID, top=3)
    payload = _payload(events)

    # The song block, exactly the contract's four keys. `pool` is K — how
    # many teams stage 1 handed over — and it must be the CLI's own
    # DEFAULT_SONG_POOL (see cli.py: the true winner has been seen as deep
    # as stage-1 rank 61, hence 150), not a number this page made up. The
    # 8-card fixture offers more candidate teams than that (the projection
    # event reports 448 arrangements), so the search does not cap K here; if
    # the CLI ever retunes its default past what this fixture can offer,
    # this equality fails loudly and the fixture grows a card.
    assert payload["song"] == {
        "music_id": MUSIC_ID,
        "title": MUSIC_ID,  # no titles file -> the id stands in, visibly
        "difficulty": "EXPERT",
        "pool": web_server.DEFAULT_SONG_POOL,
    }
    assert payload["sorted_by"] == "song_score"
    assert len(payload["teams"]) == 3, "top trims AFTER the re-rank"

    scores = [team["song_score"] for team in payload["teams"]]
    assert scores == sorted(scores, reverse=True)
    # NEGATIVE CONTROL: distinct stats per card must yield distinct song
    # scores; if they tie, the descending assertion above proved nothing.
    if len(set(scores)) < 2:
        pytest.skip(
            "NEGATIVE CONTROL LOST: every song_score tied, so the ordering "
            "assertion was vacuous — vary the fixture stats further"
        )

    for team in payload["teams"]:
        # The agnostic score is KEPT beside the song score (the page needs
        # both, and they are different quantities on different scales).
        assert "score" in team
        assert team["song_score"] > 0
        # The slot order that achieved song_score: the same five members,
        # arranged — a permutation of member_ids, never a sixth card.
        assert sorted(team["member_order"]) == sorted(team["member_ids"])

    # Stage 2 reports progress off the re-ranker's own callback, sized to K.
    song_progress = [e for e in events if e.get("type") == "song_progress"]
    assert song_progress, "no song_progress events — the re-rank ran silently"
    assert song_progress[-1]["total"] == payload["song"]["pool"]


# ---------------------------------------------------------------------------
# Song-agnostic runs are a pure addition
# ---------------------------------------------------------------------------


def test_a_song_agnostic_run_is_byte_for_byte_todays_payload(
    monkeypatch, tmp_path
):
    """`""` means song-agnostic, exactly as an absent field does. No `song`
    block, no per-team song fields, `top` teams and not DEFAULT_SONG_POOL of
    them — a regression here silently moves every existing answer."""
    payload = _payload(_run(monkeypatch, tmp_path, song="", top=3))

    assert payload["sorted_by"] == "score"
    assert "song" not in payload
    # `top`, not `search_top`: an agnostic run that quietly kept 150 teams
    # would ship 50x the payload and a page full of teams nobody asked for.
    assert len(payload["teams"]) == 3
    for team in payload["teams"]:
        assert "song_score" not in team
        assert "member_order" not in team

    # ... and the ordering it claims is the ordering it used.
    scores = [team["score"] for team in payload["teams"]]
    assert scores == sorted(scores, reverse=True)
    assert [team["rank"] for team in payload["teams"]] == [1, 2, 3]
    # NEGATIVE CONTROL for the key-absence assertions above: the same pool
    # in song mode DOES carry both keys — so a key rename (songScore, say)
    # cannot make both modes pass by both lacking the checked spelling.
    song_payload = _payload(_run(monkeypatch, tmp_path, song=MUSIC_ID, top=3))
    assert all("song_score" in team for team in song_payload["teams"])
    assert all("member_order" in team for team in song_payload["teams"])
    # NEGATIVE CONTROL for the score ordering: agnostic scores are strict
    # (distinct stats), or the descending check was vacuous.
    assert len(set(scores)) == len(scores), (
        "NEGATIVE CONTROL LOST: agnostic scores tied; the ordering "
        "assertion proved nothing"
    )


# ---------------------------------------------------------------------------
# Failure is fast, first-class, and names the fix
# ---------------------------------------------------------------------------


def test_an_unknown_song_fails_before_the_search_runs(monkeypatch, tmp_path):
    events = _run(monkeypatch, tmp_path, song="m9999")
    errors = [e for e in events if e.get("type") == "error"]
    assert errors and "m9999" in errors[0]["message"], (
        f"expected an error naming the unknown song; got {events}"
    )
    assert not any(e.get("type") == "result" for e in events)
    # Fail FAST: a ~7 s search followed by "no such song" is a diagnostic
    # withheld for seven seconds. No projection means no search started.
    assert not any(e.get("type") == "projection" for e in events)


def test_an_absent_chart_library_names_the_command_that_builds_it(
    monkeypatch, tmp_path
):
    events = _run(monkeypatch, tmp_path, song=MUSIC_ID, library="absent")
    errors = [e for e in events if e.get("type") == "error"]
    assert errors, f"expected an error event; got {events}"
    assert "build_charts.py" in errors[0]["message"], (
        "an absent chart library must name the command that fixes it, "
        "never just refuse"
    )
    assert not any(e.get("type") == "result" for e in events)
