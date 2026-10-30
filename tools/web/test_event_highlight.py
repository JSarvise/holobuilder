"""Highlight on the Event goal: a random whole song of the chapter's
Event Bonus holomems, a 170 s song's length bonus, ranked on the MEAN score
over the pool's charts. The pool, the request, the leaf context and the Event Pt."""
from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest

import event_mode as em
import server as web_server
from hbuilder.domain import marathon as mar


def _event():
    rows = (
        mar.MarathonBonusRow(permil=200, character_id="chr-00007"),
        mar.MarathonBonusRow(permil=200, character_id="chr-03007"),
        # a card row and a rarity row name no holomem of the pool
        mar.MarathonBonusRow(permil=500, character_id="chr-09999", card_id="card-09999-5-x"),
        mar.MarathonBonusRow(permil=100, rarity=5, bloom=5),
    )
    return mar.MarathonEvent(name="normal-008-chapter-1", rows_by_channel={mar.LIVE_CHANNEL: rows})


MUSIC = [
    {"id": "m0001", "characterIds": ["chr-00007"]},                  # solo of a holomem
    {"id": "m0002", "characterIds": ["chr-01000", "chr-03007"]},     # group with one of them
    {"id": "m0003", "characterIds": ["chr-01000"]},                  # nobody of the event
    {"id": "m0004", "characterIds": ["chr-09999"]},                  # a CARD row's holomem only
    {"id": "m0005", "characterIds": ["chr-00007"]},                  # no chart on disk
    {"id": "m0006"},                                                 # no singers listed
]


def test_event_holomems_are_the_character_only_live_rows():
    assert em.event_holomems(_event()) == ["chr-00007", "chr-03007"]


def test_pool_takes_every_song_a_holomem_sings_and_nothing_else():
    pool, skipped = em.highlight_pool(["chr-00007", "chr-03007"], MUSIC,
                                      charted={"m0001", "m0002", "m0003", "m0004"})
    assert pool == ["m0001", "m0002"]          # the group song is in; m0003/m0004/m0006 out
    assert skipped == ["m0005"]                # a pool song without a chart is named


def test_pool_without_chart_listing_keeps_every_pool_song():
    pool, skipped = em.highlight_pool(["chr-00007"], MUSIC, charted=None)
    assert pool == ["m0001", "m0005"] and skipped == []


def test_pool_of_the_real_open_event_is_the_holomems_songs():
    """On the refreshed dumps: every pool song has an event holomem among its singers and
    every song with one is in the pool (or skipped for want of a chart)."""
    try:
        _raw, event, _k = em.load_event_bundle(web_server.EVENT_BONUS_PATH)
    except (OSError, ValueError, mar.SchemaError):
        pytest.skip("no event on disk")
    music = web_server._music_rows()
    charted = set(web_server._chart_note_counts("EXPERT") or ())
    hol = set(em.event_holomems(event))
    pool, skipped = em.highlight_pool(sorted(hol), music, charted)
    assert pool, "the open chapter's holomems sing no charted song"
    sings = {d["id"] for d in music if hol & set(d.get("characterIds") or ())}
    assert set(pool) | set(skipped) == sings
    assert set(pool) <= charted and not set(skipped) & charted


@dataclasses.dataclass(frozen=True)
class _Ctx:
    charts: tuple = ()
    board_per_chart: bool = False


def test_the_leaf_scores_exactly_the_pool_charts_a_board_per_chart():
    library = [SimpleNamespace(music_id=m) for m in ("m0001", "m0002", "m0003")]
    ctx, charts, lost = em.highlight_context(_Ctx(charts=("solo",)), library, ["m0002", "m0001", "m0009"])
    assert [c.music_id for c in charts] == ["m0002", "m0001"]
    assert ctx.charts == charts and ctx.board_per_chart is True
    assert lost == ["m0009"]                     # the non-pool m0003 never enters


@pytest.mark.parametrize("song", ["highlight", "Highlight", " HIGHLIGHT "])
def test_body_takes_highlight(song):
    opts = em.parse_event_body({"song": song})
    assert opts["song"] == em.HIGHLIGHT and opts["objective"] == "event_points"


def test_body_refuses_event_song_score_with_highlight():
    with pytest.raises(em.EventRequestError, match="Event Points only"):
        em.parse_event_body({"song": "highlight", "objective": "event_song"})


def test_highlight_bonus_is_a_170_s_songs_length_bonus():
    # result screens: "Highlight Bonus +42%" at 4 and 10 boosts, +50 % at 3;
    # +54 % at 1 boost is a player's report. 169 s gives the same at every boost count.
    for sec in (em.HIGHLIGHT_SECONDS, 169):
        assert [em.length_permil_estimate(sec, b) for b in (1, 2, 3, 4, 10)] == [1540, 1500, 1500, 1420, 1420]


def test_highlight_event_points_use_the_highlight_bonus_not_the_song_length():
    card = SimpleNamespace(id="card-00007-5-x", member="A", costume_name="c", type="t", rarity="5")
    reps = [card] * 6
    r = {"leader": card, "members": (0, 1, 2, 3, 4), "score": 1_840_400.4, "live_permil": 1380,
         "song_permil": 0, "objective": 0.0, "source": "event_search"}
    row = em.row_payload(1, r, reps=reps, bonus_of=lambda c: (276, 0), card_state=lambda _i: {},
                         boosts=3, length_permil=em.length_permil_estimate(em.HIGHLIGHT_SECONDS, 3), holopass=True)
    assert row["event_points"] == mar.event_points(1_840_400, 1380, boosts=3, length_permil=1500,
                                                   holopass=True)
    assert row["event_points"] != mar.event_points(1_840_400, 1380, boosts=3, length_permil=1480,
                                                   holopass=True)


def test_mean_over_the_pool_ranks_the_teams():
    """The objective is f(mean score): a team strong on one pool song but weak on the
    rest loses to a steadier team with the same E -- what the search maximises."""
    per_chart = {"steady": [1_500_000, 1_500_000, 1_500_000], "spiky": [2_400_000, 1_000_000, 900_000]}

    def value(name):
        mean = sum(per_chart[name]) / len(per_chart[name])
        return mar.event_objective(mean, objective="event_points", live_permil=1000, song_permil=0)
    assert value("steady") > value("spiky")


def test_event_run_route_takes_highlight_and_refuses_its_song_score(monkeypatch):
    """The route parses Highlight without asking for a chart of a song called 'highlight'."""
    body = {"song": "highlight", "objective": "event_points", "difficulty": "HARD"}
    opts = em.parse_event_body(body)
    assert web_server._parse_difficulty(body, "") == "HARD"
    assert json.dumps(opts)                                  # the job echoes its opts
