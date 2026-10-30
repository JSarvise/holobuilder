"""The sole file-I/O boundary for canonical data (cards.json, owned.json).

No function here writes either file — every function in this module reads
only. `domain/schema.py` owns validation rules; this module wires JSON I/O
to those rules and to `card_from_record`.

Mode A vs mode B. `data/cards.json` holds **maxed** stats,
so a default run answers "what is the best team if every card were
maxed?" — mode A, and it stays the default. A *mode B* document is the
same cards.json shape carrying the account's **real** per-card
stats, plus one extra top-level `provenance` object naming where those
numbers came from and when. `card_provenance` parses that block;
`load_cards_and_provenance` returns it alongside the cards so a caller
can announce which basis it ranked on. A document with no `provenance`
key is mode A — the absence is the default, never an error.

Nothing here builds a mode-B document: `tools/recon/proxy/build_mode_b.py`
does, by hand, and it emits a *new* file. `data/cards.json` and
`data/owned.json` remain read-only to every code path in this package.
"""

from __future__ import annotations

import hashlib
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
MODE_B = "B"
REAL_STATS_BASIS = "real"

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

    Deliberately not optional-field-tolerant: a document that claims to
    carry real stats must say, in the file itself, which mode it is, what
    basis its stats are on, when it was generated, and which two files it
    was generated from. A half-filled provenance block is a `SchemaError`,
    not a shrug — the whole point of the block is that nobody can mistake a
    real-stat ranking for a maxed one, and a block that only half-says so
    fails that job."""

    mode: str
    stats_basis: str
    generated_at: str
    stats_source: str
    skills_source: str
    skills_source_sha256: str
    card_count: int
    description: str

    def is_mode_b(self) -> bool:
        return self.mode == MODE_B

    def banner(self) -> str:
        """The one line the CLI prints so a mode-B run announces itself.
        Composed here from the parsed fields rather than read out of the
        file as free text: a document cannot understate what it is."""
        return (
            f"MODE {self.mode}: ranking on {self.stats_basis.upper()} per-card stats "
            f"from {self.stats_source} (generated {self.generated_at}, "
            f"{self.card_count} cards) — NOT the maxed default."
        )


def card_provenance(doc: object) -> CardProvenance | None:
    """Parse the optional top-level `provenance` block of a cards document.

    Returns `None` when the key is absent — that is mode A (maxed stats
    from the hand-edited `data/cards.json`), the default, and it is not a
    gap. Raises `SchemaError` when the key is present but malformed."""
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
            f"provenance: 'card_count' is {card_count} but the document holds "
            f"{len(cards)} cards",
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


def stale_source_note(provenance: CardProvenance, root: Path) -> str | None:
    """One-line staleness warning, or `None` if the mode-B document is still
    in step with the hand-edited file it copied skills out of.

    A mode-B document duplicates `data/cards.json`'s skills, gates and
    identity at build time, so a later hand edit there — the 54
    untranscribed 3★ `costume_skill`s are the live example — silently makes
    it stale. `skills_source_sha256` is what turns that into a visible
    warning instead of a wrong answer nobody notices. Read-only: hashes the
    named file, never writes it."""
    source = (root / provenance.skills_source).resolve()
    try:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
    except OSError:
        return (
            f"STALENESS UNCHECKED: {provenance.skills_source} (the file this "
            "document copied skills from) could not be read"
        )
    if digest == provenance.skills_source_sha256:
        return None
    return (
        f"STALE: {provenance.skills_source} has changed since this document was "
        f"generated ({provenance.generated_at}); its skills/gates may be out of "
        "date. Rebuild with tools/recon/proxy/build_mode_b.py."
    )


def load_cards(path: Path) -> tuple[Card, ...]:
    """Read, validate, and construct `Card`s from a cards.json-shaped file.

    Raises `SchemaError` on the first validation failure, naming the card
    id and field — including a duplicate id shared by two entries. An
    empty `cards` array is a valid document and returns an empty tuple, not
    a parse error. Returns cards in file order.

    Says nothing about *which stat basis* the document carries — that is
    `card_provenance_of`'s job, deliberately a second function rather than
    a second return value here. `tests/test_exit_codes.py::
    test_the_shared_load_and_guard_is_the_only_load_path` pins `load_cards`
    by name as the CLI's one card-loading call site; folding
    provenance into this signature would rename that call and silently
    void the pin. The cost is one extra read of the same file per run,
    which is nothing next to a solve.
    """
    with open(path, "r", encoding="utf-8") as fh:
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


def card_provenance_of(
    path: Path, *, require_mode_b: bool = False
) -> CardProvenance | None:
    """The stat basis of a cards-shaped document: `None` for the maxed
    default (mode A), a `CardProvenance` for a mode-B real-stat document.

    `require_mode_b=True` makes a document without a mode-B provenance
    block a `SchemaError` rather than a silent mode-A load: it is what
    `--real-stats` uses, so naming the wrong file fails loudly instead of
    quietly ranking on maxed stats under a flag that promised real ones.
    """
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)

    provenance = card_provenance(doc)
    if require_mode_b and (provenance is None or not provenance.is_mode_b()):
        raise SchemaError(
            f"provenance: {path} does not declare mode {MODE_B} stats — it carries "
            "no real-stat provenance block. Generate one with "
            "tools/recon/proxy/build_mode_b.py.",
            card_id=None,
            field=PROVENANCE_KEY,
        )
    return provenance


def load_owned(path: Path, cards: tuple[Card, ...]) -> tuple[Card, ...]:
    """Read owned.json, raise `SchemaError` naming any id repeated within
    `owned.json` or absent from `cards`, and return the owned subset
    ordered by each card's position in `cards` — never by the order ids
    appear in `owned.json` — so two owned files differing only in id order
    resolve to identical tuples. An empty `owned` array returns an empty
    tuple without raising."""
    with open(path, "r", encoding="utf-8") as fh:
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
    non-empty, in `cards` order, field paths in `unknown_fields()`'s own
    deterministic order. A card with no unknown fields is omitted entirely
    — an explicit zero is known data, never a gap."""
    report: list[CardCompleteness] = []
    for card in cards:
        unknown = card.unknown_fields()
        if unknown:
            report.append(CardCompleteness(card_id=card.id, unknown_fields=unknown))
    return tuple(report)
