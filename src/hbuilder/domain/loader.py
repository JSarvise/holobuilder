"""The sole file-I/O boundary for canonical data (cards.json, owned.json).

Read only: `domain/schema.py` owns the validation rules; this module wires JSON
I/O to them and to `card_from_record`.

Mode A vs mode B. `data/cards.json` holds **maxed** stats, so a default run
answers "what is the best team if every card were maxed?" (mode A, the default).
A *mode B* document has the same shape with the account's **real** per-card
stats, plus a top-level `provenance` object naming where those numbers came from
and when (`card_provenance`). No `provenance` key means mode A, never an error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from hbuilder.domain.card import Card, card_from_record
from hbuilder.domain.schema import (
    SchemaError,
    validate_card,
    validate_document,
    validate_owned_document,
)


def _split_error(error: str) -> tuple[str, str]:
    """Split a validate_* error string into (field, message). Every
    validate_* error is formatted as `{field}: {detail}`, so this split is
    exact, not a heuristic."""
    field, _, _ = error.partition(": ")
    return field, error


PROVENANCE_KEY = "provenance"

_PROVENANCE_STRING_FIELDS: tuple[str, ...] = (
    "mode",
    "stats_basis",
    "generated_at",
    "stats_source",
    "skills_source",
    "skills_source_sha256",
)


@dataclass(frozen=True, slots=True)
class CardProvenance:
    """Where a non-default card document's stats came from.

    Every field is required: a document claiming real stats must say which mode
    it is, what basis its stats are on, when it was generated and from which two
    files, so nobody can mistake a real-stat ranking for a maxed one. A
    half-filled block is a `SchemaError`."""

    mode: str
    stats_basis: str
    generated_at: str
    stats_source: str
    skills_source: str
    skills_source_sha256: str
    card_count: int
    description: str


def card_provenance(doc: object) -> CardProvenance | None:
    """Parse the optional top-level `provenance` block of a cards document.

    Returns `None` when the key is absent (mode A, the default). Raises
    `SchemaError` when the key is present but malformed."""
    if not isinstance(doc, dict) or PROVENANCE_KEY not in doc:
        return None

    block = doc[PROVENANCE_KEY]
    if not isinstance(block, dict):
        raise SchemaError(
            "provenance: must be a JSON object",
            card_id=None,
            field=PROVENANCE_KEY,
        )

    for field in _PROVENANCE_STRING_FIELDS:
        value = block.get(field)
        if not isinstance(value, str) or not value:
            raise SchemaError(
                f"provenance: '{field}' must be a non-empty string",
                card_id=None,
                field=PROVENANCE_KEY,
            )
    card_count = block.get("card_count")
    if not isinstance(card_count, int) or isinstance(card_count, bool):
        raise SchemaError(
            "provenance: 'card_count' must be an integer",
            card_id=None,
            field=PROVENANCE_KEY,
        )
    cards = doc.get("cards")
    if isinstance(cards, list) and len(cards) != card_count:
        raise SchemaError(
            f"provenance: 'card_count' is {card_count} but the document holds {len(cards)} cards",
            card_id=None,
            field=PROVENANCE_KEY,
        )

    return CardProvenance(
        mode=block["mode"],
        stats_basis=block["stats_basis"],
        generated_at=block["generated_at"],
        stats_source=block["stats_source"],
        skills_source=block["skills_source"],
        skills_source_sha256=block["skills_source_sha256"],
        card_count=card_count,
        description=str(block.get("description", "")),
    )


def load_cards(path: Path) -> tuple[Card, ...]:
    """Read, validate, and construct `Card`s from a cards.json-shaped file.

    Raises `SchemaError` on the first validation failure, naming the card
    id and field — including a duplicate id shared by two entries. An
    empty `cards` array is a valid document and returns an empty tuple, not
    a parse error. Returns cards in file order.

    The stat basis is `card_provenance_of`'s, a second function rather than a
    second return value: `tests/test_exit_codes.py::
    test_the_shared_load_and_guard_is_the_only_load_path` pins `load_cards` by
    name as the one card-loading call site. The cost is one extra read per run.
    """
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)

    doc_errors = validate_document(doc)
    if doc_errors:
        field, message = _split_error(doc_errors[0])
        raise SchemaError(message, card_id=None, field=field)

    cards: list[Card] = []
    seen_ids: set[str] = set()
    for index, record in enumerate(doc["cards"]):
        card_errors = validate_card(record, index)
        if card_errors:
            card_id = record.get("id") if isinstance(record, dict) else None
            field, message = _split_error(card_errors[0])
            raise SchemaError(message, card_id=card_id, field=field)

        card_id = record["id"]
        if card_id in seen_ids:
            raise SchemaError(
                f"id: card id '{card_id}' appears more than once in cards.json",
                card_id=card_id,
                field="id",
            )
        seen_ids.add(card_id)
        cards.append(card_from_record(record))

    return tuple(cards)


def card_provenance_of(path: Path) -> CardProvenance | None:
    """The stat basis of a cards-shaped document: `None` for the maxed
    default (mode A), a `CardProvenance` for a mode-B real-stat document."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    return card_provenance(doc)


def load_owned(path: Path, cards: tuple[Card, ...]) -> tuple[Card, ...]:
    """Read owned.json and return the owned subset in `cards` order (never the
    file's order, so two owned files differing only in id order give identical
    tuples). Raises `SchemaError` naming any id repeated in `owned.json` or absent
    from `cards`."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)

    doc_errors = validate_owned_document(doc)
    if doc_errors:
        field, message = _split_error(doc_errors[0])
        raise SchemaError(message, card_id=None, field=field)

    known_ids = {card.id for card in cards}
    seen_ids: set[str] = set()
    for owned_id in doc["owned"]:
        if owned_id in seen_ids:
            raise SchemaError(
                f"owned: id '{owned_id}' appears more than once in owned.json",
                card_id=owned_id,
                field="owned",
            )
        seen_ids.add(owned_id)
        if owned_id not in known_ids:
            raise SchemaError(
                f"owned: id '{owned_id}' is not present in the loaded cards",
                card_id=owned_id,
                field="owned",
            )

    return tuple(card for card in cards if card.id in seen_ids)


def index_by_id(cards: tuple[Card, ...]) -> dict[str, Card]:
    return {card.id: card for card in cards}


@dataclass(frozen=True, slots=True)
class CardCompleteness:
    card_id: str
    unknown_fields: tuple[str, ...]


def completeness_report(cards: tuple[Card, ...]) -> tuple[CardCompleteness, ...]:
    """One `CardCompleteness` per card whose `Card.unknown_fields()` is
    non-empty, in `cards` order. A card with no unknown fields is omitted (an
    explicit zero is known data, never a gap)."""
    report: list[CardCompleteness] = []
    for card in cards:
        unknown = card.unknown_fields()
        if unknown:
            report.append(CardCompleteness(card_id=card.id, unknown_fields=unknown))
    return tuple(report)
