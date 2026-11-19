"""A past event on the Event page (`GET /api/event/past`, `event_mode.past_event_payload`):
its record and archived rules, apart from the open event the Optimizer reads.

    python -m pytest tools/web/test_event_past.py
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import event_mode as em
import pytest
from hbweb import config, event

from hbuilder.domain import marathon as mar

PAST = "marathon_event-normal-008"
CH = PAST + "-chapter-01"
CARD = "card-00007-5-uniq-0096-00"

ENTRY = {
    "event_id": PAST,
    "title": "Catch the Halloween Fever!",
    "kind_label": "Point Rally Event",
    "starts_at": "2026-10-29T03:00:00+00:00",
    "ends_at": "2026-11-04T10:59:59+00:00",
    "art": {"banner": "marathon_event-normal-008.webp"},
    "chapters": {
        CH: {
            "chapter_id": CH,
            "number": 1,
            "title": "Catch the Halloween Fever!",
            "ends_at": "2026-11-04T10:59:59+00:00",
        }
    },
}
ARCHIVE = {
    "chapters": {
        CH: {
            "fetched_at": 1793666147.1,
            "groups": [
                {
                    "group_id": "live_marathon_score_bonus-" + CH,
                    "kind": "live",
                    "rows": [
                        {"character_id": "chr-00007", "permil": 300},
                        {"card_id": CARD, "permil": 300},
                    ],
                },
                {"group_id": "music_score_bonus-" + CH, "kind": "music", "rows": []},
            ],
            "event_songs": ["m0369"],
        }
    }
}


def _card():
    return SimpleNamespace(
        id=CARD, member="Seven", costume_name="X", rarity="5★", type="Cute", generations=frozenset()
    )


def _payload(archive=ARCHIVE, chapter=None):
    return em.past_event_payload(
        ENTRY,
        archive,
        chapter,
        cards_by_id={CARD: _card()},
        owned_ids=[CARD],
        collection={CARD: {"level": 80, "bloom": 5}},
        titles={"m0369": "A song"},
        chr_names={"chr-00007": "Seven"},
    )


def test_a_past_event_shows_its_archived_rules():
    doc = _payload()
    assert doc["past"] is True and doc["rules_on_record"] is True
    assert doc["event"]["key"] == CH and doc["event"]["title"] == "Catch the Halloween Fever!"
    assert doc["live"]["holomems"] == [{"character_id": "chr-00007", "name": "Seven", "permil": 300}]
    assert [c["id"] for c in doc["live"]["cards"]] == [CARD]
    assert [s["music_id"] for s in doc["songs"]] == ["m0369"]
    # today's collection against an old event's rules is not that event's result
    assert doc["owned_bonus_cards"] == [] and doc["open_events"] == []
    assert [(c["key"], c["chr"], c["now"], c["open"]) for c in doc["chapters"]] == [
        (CH, "chr-00007", False, False)
    ]


def test_a_past_event_without_archived_rules_says_so():
    doc = _payload(archive=None)
    assert doc["rules_on_record"] is False and doc["live"]["holomems"] == []
    assert doc["event"]["key"] == CH and doc["event"]["kind_label"] == "Point Rally Event"


def test_a_chapter_the_event_does_not_have_is_refused():
    with pytest.raises(LookupError):
        _payload(chapter=PAST + "-chapter-09")


def test_the_open_event_never_reads_the_archive():
    # the Optimizer's and /api/event's loader: a closed chapter stays refused whatever is archived
    raw = {
        "current_group_ids": ["live_marathon_score_bonus-marathon_event-normal-009-chapter-01"],
        "groups": [],
    }
    with pytest.raises(mar.SchemaError):
        em.bundle_from_raw(raw, CH)


# --- HTTP ---------------------------------------------------------------------------
def _get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture
def past_inputs(monkeypatch, tmp_path):
    archive = tmp_path / "marathon_bonus_archive.json"
    archive.write_text(json.dumps(ARCHIVE), encoding="utf-8")
    monkeypatch.setattr(config, "EVENT_BONUS_ARCHIVE_PATH", archive)
    monkeypatch.setattr(event, "_history_doc", lambda: {"events": {PAST: ENTRY}})
    monkeypatch.setattr(event, "load_inputs", lambda *a, **k: ((_card(),), (_card(),), None, None))
    monkeypatch.setattr(config, "COLLECTION_PATH", tmp_path / "none.json")
    monkeypatch.setattr(config, "EVENT_BONUS_PATH", tmp_path / "no_open_event.json")
    return archive


def test_past_route(web_base, past_inputs):
    status, doc = _get(web_base + f"/api/event/past?event={PAST}")
    assert status == 200 and doc["past"] is True and doc["rules_on_record"] is True
    assert doc["live"]["cards"][0]["id"] == CARD
    status, doc = _get(web_base + f"/api/event/past?event={PAST}&chapter={PAST}-chapter-02")
    assert status == 404
    status, doc = _get(web_base + "/api/event/past?event=marathon_event-normal-077")
    assert status == 404 and "not on record" in doc["error"]
    status, doc = _get(web_base + "/api/event/past")
    assert status == 400


def test_a_past_event_leaves_the_open_event_alone(web_base, past_inputs):
    before = past_inputs.read_bytes()
    status, _doc = _get(web_base + f"/api/event/past?event={PAST}")
    assert status == 200
    assert past_inputs.read_bytes() == before  # a read, never a write
    # the open event (here: none) is still what /api/event answers; the archive does not fill it
    status, doc = _get(web_base + "/api/event")
    assert status == 503 and doc["available"] is False
    status, doc = _get(web_base + f"/api/event?event={CH}")
    assert status == 503


def test_a_past_board_asks_the_tracker_for_no_lease(web_base, monkeypatch, tmp_path):
    kicked = []
    monkeypatch.setattr(event, "tracker_configured", lambda: True)
    monkeypatch.setattr(event, "kick_top_watch", lambda region, kind=None: kicked.append(region) or True)
    monkeypatch.setattr(config, "CUTOFFS_STORE_DIR", tmp_path)  # nothing stored: 503 either way
    status, doc = _get(web_base + f"/api/event/top?region=us&chapter={CH}&past=1")
    assert status == 503 and doc["watching"] is False and kicked == []
    status, doc = _get(web_base + "/api/event/top?region=us")
    assert status == 503 and doc["watching"] is True and kicked == ["us"]


@pytest.mark.parametrize(
    "route", ["/api/event/cutoffs?region=us&at=soon", "/api/event/top?region=us&at=1.5e12"]
)
def test_a_bad_at_is_refused(web_base, route):
    status, doc = _get(web_base + route)
    assert status == 400 and "at" in doc["error"]
