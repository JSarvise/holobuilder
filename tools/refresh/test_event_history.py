"""The event history (`event_history.py`): additive merge of the listed events, our own cutoff
reads and the emblems held. No network, no files outside tmp_path."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import event_cutoffs as ec  # noqa: E402
import event_history as eh  # noqa: E402

E7 = "marathon_event-normal-007"
C7 = E7 + "-chapter-01"


def test_emblems_give_the_best_tier_per_chapter_and_ranking():
    got = eh.emblem_results([
        {"emblem_id": "emblem-marathon_score_ranking_rank_reward-marathon_event-normal-001-chapter-01-gold"},
        {"emblem_id": "emblem-marathon_score_ranking_rank_reward-marathon_event-normal-001-chapter-01-platinum"},
        {"emblem_id": "emblem-total_music_highest_score_reward-marathon_event-normal-002-chapter-01-silver"},
        {"emblem_id": "emblem-music_highest_score_ranking_rank_reward-marathon_event-normal-001-chapter-01-m0206-dream"},
        {"emblem_id": "emblem-chr-best_score_rating_00001-1500"},
    ])
    assert got == {
        "marathon_event-normal-001-chapter-01|points": {
            "chapter_id": "marathon_event-normal-001-chapter-01", "event_id": "marathon_event-normal-001",
            "ranking": "points", "grade": "platinum", "source": "emblem held (User/Get user_emblem_list)"},
        "marathon_event-normal-002-chapter-01|total": {
            "chapter_id": "marathon_event-normal-002-chapter-01", "event_id": "marathon_event-normal-002",
            "ranking": "total", "grade": "silver", "source": "emblem held (User/Get user_emblem_list)"},
    }


def _store(tmp_path):
    meta = {"event_id": E7, "chapter_id": C7, "chapter_number": 1, "start_time": 1, "chapter_end_time": 10,
            "reveal_time": 20, "tiers": {"points": [{"end_rank": 5000, "grade_id": "marathon_ranking_grade-platinum"}]}}
    d = tmp_path / "us"
    d.mkdir()
    (d / f"{C7}.meta.json").write_text(json.dumps(meta), "utf-8")
    rows = [{"captured_at": 5, "kind": "points", "final": False, "borders": [{"rank": 5000, "score": 99}],
             "self": {"rank": 1385, "score": 415, "out_of_range": False}},
            {"captured_at": 25, "kind": "points", "final": True, "borders": [{"rank": 5000, "score": 530}],
             "self": {"rank": 2974, "score": 874, "out_of_range": False}},
            {"captured_at": 26, "kind": "points", "final": False, "borders": [], "self": None}]
    (d / f"{C7}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")
    ec.append_points(tmp_path, "us", E7, 30, {C7: 874103})
    return tmp_path


def test_cutoff_results_keep_the_final_rank_borders_and_event_pt(tmp_path):
    got = eh.cutoff_results(_store(tmp_path), "us")[C7]
    assert got["self"]["rank"] == 2974 and got["self"]["final"] is True
    assert got["final_borders"]["borders"] == [{"rank": 5000, "score": 530}]
    assert got["event_pt"]["value"] == 874103
    assert got["tiers"] == [{"rank": 5000, "grade_id": "marathon_ranking_grade-platinum"}]


def test_merge_never_removes_and_a_final_beats_a_later_non_final(tmp_path):
    cut = eh.cutoff_results(_store(tmp_path), "us")
    listed = [{"event_id": E7, "title": "Neon Sunshine Festival", "kind_label": "Point Rally Event",
               "art": {"banner": "x.webp"}, "chapters": [{"id": C7, "number": 1}]}]
    first = eh.merge({}, events=listed, cutoffs=cut, emblems={})
    # the event leaves the list and a later, non-final self read arrives: nothing is lost
    later = {C7: dict(cut[C7], self={"rank": 3100, "score": 900, "captured_at": 99, "final": False})}
    second = eh.merge(first, events=[], cutoffs=later, emblems={})
    e = second["events"][E7]
    assert e["title"] == "Neon Sunshine Festival" and e["art"] == {"banner": "x.webp"}
    assert e["chapters"][C7]["self"]["rank"] == 2974
    # an event known only from an emblem, titled from the master
    em = eh.emblem_results([{"emblem_id": "emblem-marathon_score_ranking_rank_reward-"
                                           "marathon_event-normal-001-chapter-01-gold"}])
    third = eh.merge(second, events=[], cutoffs={}, emblems=em,
                     titles={"marathon_event-normal-001-chapter-01": "First Event"})
    old = third["events"]["marathon_event-normal-001"]
    assert old["title"] == "First Event"
    assert old["chapters"]["marathon_event-normal-001-chapter-01"]["emblems"]["points"]["grade"] == "gold"
    assert E7 in third["events"]


def test_top_points_are_read_from_the_marathon_chapters():
    def f(fn, payload):
        return ec._write_varint((fn << 3) | 2) + ec._write_varint(len(payload)) + payload
    chapter = f(1, C7.encode()) + ec._write_varint((17 << 3) | 0) + ec._write_varint(874103)
    blob = f(1, f(12, chapter))
    assert ec.parse_top_points(blob) == {C7: 874103}


def test_the_published_archive_carries_no_ones_result():
    """The app must not receive the publisher's own history: a friend's Past events would
    show the publisher's ranks, Event Pt and emblems."""
    doc = {"events": {"ev": {"title": "T", "chapters": {"c1": {
        "ranking": "points", "final_borders": {"borders": [{"rank": 500, "score": 9}]},
        "self": {"rank": 2974}, "event_pt": {"value": 874_103}, "emblems": {"points": {"grade": "gold"}}}}}}}
    got = eh.archive_of(doc)
    assert got == {"events": {"ev": {"title": "T", "chapters": {"c1": {
        "ranking": "points", "final_borders": {"borders": [{"rank": 500, "score": 9}]}}}}}}


def test_recorded_finals_fill_only_what_we_did_not_read():
    finals = eh.recorded_finals({"source": "holodori.best", "read_at": "2026-10-25", "events": {"ev": {"chapters": {
        "c1": {"ranking": "total", "regions": {"us": {"borders": [{"rank": 500, "score": 7}]}}},
        "c2": {"ranking": "points", "regions": {"us": {"borders": [{"rank": 500, "score": 8}]}}}}}}}, "us")
    own = {"c2": {"event_id": "ev", "final_borders": {"borders": [{"rank": 500, "score": 99}], "source": "own"}}}
    doc = eh.merge({}, events=[], cutoffs=own, emblems={}, finals=finals)
    ch = doc["events"]["ev"]["chapters"]
    assert ch["c1"]["final_borders"]["borders"][0]["score"] == 7 and ch["c1"]["ranking"] == "total"
    assert ch["c2"]["final_borders"]["borders"][0]["score"] == 99        # our own read wins


def test_past_events_get_their_banner_from_the_catalogue_names(tmp_path):
    """Past events' banners are downloaded."""
    asked = []

    def ensure(events, art_dir):
        asked.extend(events)
        return {e["event_id"]: {"banner": f"{e['event_id']}.webp", "logo": None} for e in events}
    doc = {"events": {"marathon_event-relay-002": {"chapters": {}},
                      "marathon_event-normal-007": {"art": {"banner": "x.webp"}, "chapters": {}}}}
    assert eh.past_art(doc, tmp_path, ensure) == 1
    assert asked == [{"event_id": "marathon_event-relay-002",
                      "logo_asset_id": "img_marathon_event_relay_logo-002-01"}]
    assert doc["events"]["marathon_event-relay-002"]["art"] == {"banner": "marathon_event-relay-002.webp"}


def test_the_archive_drops_the_owner_s_song_results_too():
    doc = {"events": {"e": {"chapters": {"c": {"self": {"rank": 1}, "event_pt": {"value": 5},
                                               "songs": {"m1": {"self": {"rank": 9}, "final_borders": {"borders": []}}}}}}}}
    pub = eh.archive_of(doc)["events"]["e"]["chapters"]["c"]
    assert "self" not in pub and "event_pt" not in pub
    assert pub["songs"]["m1"] == {"final_borders": {"borders": []}}
