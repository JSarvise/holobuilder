"""`card_append.py` is the ONE automated path allowed to write `data/cards.json`.

It exists because forbidding every automated write has a worse failure mode:
the game could ship a card, the tool would answer "your best team" without it,
and the user would never learn the card existed. So the rule is append-only --
a new card may be added, an existing entry is never modified or removed -- and
this module implements it.

Everything here tests a REFUSAL or a byte, because "it added the card" is not the
claim that matters -- "it added the card and touched nothing else, in a file
whose formatting it reproduced exactly" is.

`tools/recon/proxy/` is outside `src/hbuilder/`, so it is imported via an
explicit `sys.path` insert, the same way `test_import_to_cards_passive_target_guard`
does it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROXY_DIR = REPO_ROOT / "tools" / "recon" / "proxy"
if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

import card_append  # noqa: E402

from hbuilder.cli import build_parser  # noqa: E402

# The catalogue path comes from the CLI's own declared default, never a literal:
# `tests/test_fixture_isolation.py` forbids a hardcoded `data/` path anywhere in
# `tests/` so the user's real files can live there without coupling tests to
# their contents. These tests are about that file's INVIOLABILITY rather than its
# contents, which is the same exemption `test_mode_b` takes, the same way.
CARDS_JSON = REPO_ROOT / build_parser().parse_args(["solve"]).cards
DRAFT_JSON = PROXY_DIR / "dumps" / "cards_import_draft.json"


# --------------------------------------------------------------------------
# fixtures: the smallest schema-valid card, and a two-card catalogue
# --------------------------------------------------------------------------


def _record(card_id: str, **overrides) -> dict:
    record = {
        "id": card_id,
        "member": "Test Member",
        "generation": ["5期生"],
        "type": "Cute",
        "rarity": "5★",
        "title": "A Title",
        "costume_name": "A Costume",
        "stats": {"performance": 100, "technique": 100, "sense": 100},
        "skills": {"SPECIAL": None, "ACTIVE": None, "PASSIVE": None},
        "costume_skill": None,
    }
    record.update(overrides)
    return record


def _catalogue(*ids: str) -> dict:
    return {"schema_version": "2", "cards": [_record(i) for i in sorted(ids)]}


def _write(path: Path, doc: dict) -> Path:
    path.write_bytes(card_append.encode(doc))
    return path


# --------------------------------------------------------------------------
# the encoding
# --------------------------------------------------------------------------


def test_encode_emits_crlf_and_a_trailing_newline_as_bytes():
    out = card_append.encode(_catalogue("card-a"))
    assert b"\r\n" in out
    assert out.count(b"\n") == out.count(b"\r\n"), "a bare LF means the file got mangled"
    assert out.endswith(b"\r\n")
    assert not out.endswith(b"\r\n\r\n")


def test_encode_reproduces_the_real_catalogue_byte_for_byte():
    """The reason the whole module can be trusted with this file.

    If the encoder cannot reproduce the file it is about to rewrite, then every
    addition arrives as a whole-file reformat; `Path.write_text`, for one, re-translates
    `\\n` on Windows and turns a 226-line addition into an 18,215-line diff.
    """
    raw = CARDS_JSON.read_bytes()
    assert card_append.encode(json.loads(raw.decode("utf-8"))) == raw


# --------------------------------------------------------------------------
# the refusals
# --------------------------------------------------------------------------


def test_a_file_not_in_the_canonical_encoding_is_refused_and_left_alone(tmp_path):
    cards = tmp_path / "cards.json"
    # LF, no trailing newline -- i.e. anything but this module's own encoding.
    cards.write_bytes(json.dumps(_catalogue("card-a"), indent=2, sort_keys=True).encode())
    before = cards.read_bytes()
    draft = _write(tmp_path / "draft.json", _catalogue("card-a", "card-b"))

    with pytest.raises(card_append.RefusedToWrite, match="canonical encoding"):
        card_append.apply_additions(cards, draft)
    assert cards.read_bytes() == before


def test_an_existing_entry_is_never_modified_even_when_the_draft_disagrees(tmp_path):
    """The rule's hard half. The draft is machine-derived; the catalogue carries
    hand corrections. Where they disagree about a card the catalogue already has,
    the catalogue wins and nothing is written."""
    cards = _write(tmp_path / "cards.json", _catalogue("card-a", "card-b"))
    before = cards.read_bytes()
    tampered = _catalogue("card-a", "card-b")
    for record in tampered["cards"]:
        record["title"] = "TAMPERED"
        record["stats"]["performance"] = 1
    draft = _write(tmp_path / "draft.json", tampered)

    result = card_append.apply_additions(cards, draft)

    assert result.added == []
    assert result.wrote is False
    assert cards.read_bytes() == before


def test_a_card_missing_from_the_draft_is_never_removed(tmp_path):
    cards = _write(tmp_path / "cards.json", _catalogue("card-a", "card-b"))
    draft = _write(tmp_path / "draft.json", _catalogue("card-a"))

    card_append.apply_additions(cards, draft)

    kept = {c["id"] for c in json.loads(cards.read_bytes().decode("utf-8"))["cards"]}
    assert kept == {"card-a", "card-b"}


def test_a_schema_invalid_candidate_is_reported_by_id_and_not_added(tmp_path):
    cards = _write(tmp_path / "cards.json", _catalogue("card-a"))
    broken = _record("card-b")
    broken["type"] = "NotAType"
    draft = _write(tmp_path / "draft.json", {"schema_version": "2", "cards": [broken]})

    result = card_append.apply_additions(cards, draft)

    assert result.added == []
    assert [r.card_id for r in result.rejections] == ["card-b"]
    assert result.rejections[0].reason == "schema"


# --------------------------------------------------------------------------
# the untranscribed gate -- and why it is comparative
# --------------------------------------------------------------------------


def test_an_untranscribed_slot_field_is_reported_not_guessed(tmp_path):
    """Gate 1, in the rule's own words: `load_cards` accepting it with no
    `unknown_fields`. A null `condition` is the three-state contract's
    "the transcriber has not said whether this is gated"."""
    cards = _write(tmp_path / "cards.json", _catalogue("card-a"))
    candidate = _record("card-b")
    candidate["skills"]["PASSIVE"] = {
        "family": "stat_up",
        "effect_type": "performance",
        "duration": None,
        "interval": None,
        "probability_tier": None,
        "magnitude": 10.0,
        "condition": None,  # <- untranscribed, not "ungated"
        "target": {"kind": "self"},
        "override": {"kind": "none"},
        "rider": {"kind": "none"},
    }
    draft = _write(tmp_path / "draft.json", {"schema_version": "2", "cards": [candidate]})

    result = card_append.apply_additions(cards, draft)

    assert result.added == []
    assert result.rejections[0].reason == "untranscribed"
    assert "skills.PASSIVE.condition" in result.rejections[0].detail


def test_a_not_applicable_field_is_not_treated_as_untranscribed(tmp_path):
    """The gate must not reject every card the game ships.

    A SPECIAL slot has no `interval` and no `probability_tier`; a COSTUME has no
    `duration`. Those coerce to the UNKNOWN sentinel on the constructed object,
    and a gate that counted them refuses everything. `Card.unknown_fields()`
    already excludes NOT_APPLICABLE fields per slot kind, and this pins that the
    module honours that rather than out-strictening it.
    """
    cards = _write(tmp_path / "cards.json", _catalogue("card-a"))
    candidate = _record("card-b")
    candidate["skills"]["SPECIAL"] = {
        "family": "conditional_score_up",
        "effect_type": "score",
        "duration": 10.0,
        "interval": None,          # NOT_APPLICABLE to SPECIAL
        "probability_tier": None,  # NOT_APPLICABLE to SPECIAL
        "magnitude": 20.0,
        "condition": {"kind": "ungated"},
        "target": {"kind": "all"},
        "override": {"kind": "none"},
        "rider": {"kind": "none"},
    }
    draft = _write(tmp_path / "draft.json", {"schema_version": "2", "cards": [candidate]})

    result = card_append.apply_additions(cards, draft)

    assert result.rejections == []
    assert result.added == ["card-b"]


def test_an_untranscribed_field_inside_a_rider_is_caught_by_the_comparative_gate(tmp_path):
    """Gate 2, and the reason it exists.

    `Card.unknown_fields()` walks slot-level fields only -- it never descends
    into `override` or `rider`. So `card-00015-5-uniq-0067-00`'s
    `costume_skill.rider.target`, hand-transcribed as JSON null where the master
    says `all`, passed a completeness report that read "0 cards with unknown
    fields". The comparative gate catches it: an addition may not leave a field
    untranscribed in a position where no existing card leaves one.
    """
    slot = {
        "family": "stat_up",
        "effect_type": "performance",
        "duration": None,
        "interval": None,
        "probability_tier": None,
        "magnitude": 80.0,
        "condition": {"kind": "ungated"},
        "target": {"kind": "all"},
        "override": {"kind": "none"},
        "rider": {
            "kind": "rider",
            "family": "score_support",
            "effect_type": "score",
            "magnitude": 25.0,
            "condition": {"kind": "ungated"},
            "target": {"kind": "all"},
        },
    }
    clean = _record("card-a", costume_skill=json.loads(json.dumps(slot)))
    cards = _write(tmp_path / "cards.json", {"schema_version": "2", "cards": [clean]})

    tainted = _record("card-b", costume_skill=json.loads(json.dumps(slot)))
    tainted["costume_skill"]["rider"]["target"] = None
    draft = _write(tmp_path / "draft.json", {"schema_version": "2", "cards": [tainted]})

    result = card_append.apply_additions(cards, draft)

    assert result.added == []
    assert result.rejections[0].reason == "untranscribed"
    assert "costume_skill.rider.target" in result.rejections[0].detail


# --------------------------------------------------------------------------
# the addition itself
# --------------------------------------------------------------------------


def test_an_addition_is_inserted_in_sorted_position_and_reads_back(tmp_path):
    cards = _write(tmp_path / "cards.json", _catalogue("card-a", "card-c"))
    draft = _write(tmp_path / "draft.json", _catalogue("card-a", "card-b", "card-c"))

    result = card_append.apply_additions(cards, draft)

    assert result.added == ["card-b"]
    assert result.wrote is True
    ids = [c["id"] for c in json.loads(cards.read_bytes().decode("utf-8"))["cards"]]
    assert ids == ["card-a", "card-b", "card-c"]


def test_dry_run_reports_the_addition_and_writes_nothing(tmp_path):
    cards = _write(tmp_path / "cards.json", _catalogue("card-a"))
    before = cards.read_bytes()
    draft = _write(tmp_path / "draft.json", _catalogue("card-a", "card-b"))

    result = card_append.apply_additions(cards, draft, dry_run=True)

    assert result.added == ["card-b"]
    assert result.wrote is False
    assert result.after_bytes > result.before_bytes
    assert cards.read_bytes() == before


# --------------------------------------------------------------------------
# against the real data
# --------------------------------------------------------------------------


@pytest.mark.skipif(not DRAFT_JSON.exists(), reason="dumps/ is gitignored recon data")
def test_the_automated_add_reproduces_the_hand_merge_byte_for_byte(tmp_path):
    """The claim worth making about this module, checked against ground truth.

    The Miko/Suisei event pair was added to `data/cards.json` by hand.
    Delete exactly those two from the real catalogue, let the module put them
    back from the draft, and the result must be the real file — byte for byte.
    """
    real = CARDS_JSON.read_bytes()
    doc = json.loads(real.decode("utf-8"))
    event = ["card-00015-5-uniq-0067-00", "card-00018-5-uniq-0068-00"]
    doc["cards"] = [c for c in doc["cards"] if c["id"] not in event]
    assert len(doc["cards"]) == len(json.loads(real.decode("utf-8"))["cards"]) - 2

    work = _write(tmp_path / "cards.json", doc)
    result = card_append.apply_additions(work, DRAFT_JSON)

    assert result.added == event
    assert work.read_bytes() == real


@pytest.mark.skipif(not DRAFT_JSON.exists(), reason="dumps/ is gitignored recon data")
def test_the_real_catalogue_needs_no_additions_and_is_not_written(tmp_path):
    before = CARDS_JSON.read_bytes()
    result = card_append.apply_additions(CARDS_JSON, DRAFT_JSON, dry_run=True)
    assert result.added == []
    assert result.rejections == []
    assert CARDS_JSON.read_bytes() == before
