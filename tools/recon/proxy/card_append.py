"""Append-only additions to `data/cards.json` -- the ONE automated write it allows.

The rule:

    `data/cards.json` is the GAME'S CATALOGUE, not the player's data. An automated
    path MAY ADD a card the game ships and this file lacks. It must NEVER modify or
    remove an existing entry -- those carry hand corrections. Additions are gated on
    `load_cards` accepting the card with no `unknown_fields`; a card that cannot be
    translated cleanly is REPORTED, never guessed. Write BYTES, not text.

Why the rule is shaped that way, so nobody softens it later: if nothing could write the
file, the game could ship a card and the tool would answer "your best team" without it,
and the player would never learn the card existed. But the file also carries hand
corrections that no extraction can reproduce, so the write has to be strictly additive.

What this module guarantees, and how each one is CHECKED rather than intended
-----------------------------------------------------------------------------
1. **The encoder cannot reformat.** Before writing anything, the UNMODIFIED document
   is re-encoded and compared byte-for-byte against the file on disk. If they differ,
   the file is not in this module's canonical form and the write is REFUSED -- because
   a mismatch means the eventual diff would be a whole-file reformat wearing an
   addition's clothes. `Path.write_text`
   re-translates `\\n` on Windows, so hand-produced CRLF becomes `\\r\\r\\n` and a 226-line
   addition renders as an **18,215-line diff**. Hence `write_bytes`.
2. **No existing entry moves.** Pre-existing records are carried over by IDENTITY, never
   rebuilt, and after the write the file is re-read and every pre-existing id compared
   for deep equality. Their relative order is asserted unchanged too.
3. **Nothing enters that cannot be read back.** Each candidate must pass `validate_card`,
   construct through `card_from_record`, and carry no UNKNOWN anywhere in the resulting
   object graph. Anything else is reported by id with its reason and left out.
4. **A failed verification restores the original bytes.** The file is never left in a
   state this module has not re-read and agreed with.

⚠ On gate 3, and it is not a detail: `Card.unknown_fields()` walks the SLOT-level fields
only -- it does not descend into `override` or `rider`. So an untranscribed target INSIDE
a rider passes it silently: a card's `costume_skill.rider.target` can sit as UNKNOWN
under a completeness report that reads "0 cards with unknown fields".
`nested_unknown_paths` covers precisely that blind spot,
and `baseline_unknown_paths` says which nested unknowns are normal -- see their
docstrings for why the check has to be comparative rather than absolute.

Usage:
    proxyenv/Scripts/python card_append.py [--dry-run] [--draft PATH] [--cards PATH]
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
DUMPS = HERE / "dumps"
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))

from hbuilder.domain.card import Card  # noqa: E402
from hbuilder.domain.loader import card_from_record, load_cards  # noqa: E402
from hbuilder.domain.schema import validate_card, validate_document  # noqa: E402
from hbuilder.domain.unknown import is_known  # noqa: E402

CARDS_JSON = REPO / "data" / "cards.json"
DRAFT_JSON = DUMPS / "cards_import_draft.json"


# --------------------------------------------------------------------------
# the canonical encoding -- matched to the file, not chosen
# --------------------------------------------------------------------------


def encode(doc: Any) -> bytes:
    """The exact byte encoding `data/cards.json` is already written in.

    `indent=2, sort_keys=True, ensure_ascii=False`, CRLF line endings, and a
    trailing CRLF. This reproduces the catalogue byte-for-byte (CRLF only, no
    bare LF, no BOM).
    `sort_keys` is not load-bearing for the existing file -- its keys are
    already in sorted order, so both settings reproduce it -- but it IS
    load-bearing for a NEW record, whose keys come out of the importer in
    insertion order. Sorting them is what makes an addition look like every
    entry around it.
    """
    text = json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=True)
    return text.replace("\n", "\r\n").encode("utf-8") + b"\r\n"


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Rejection:
    card_id: str
    reason: str
    detail: str

    def __str__(self) -> str:
        return f"{self.card_id}: {self.reason} -- {self.detail}"


def _nested_unknown_in_slot(slot: Any, prefix: str) -> list[str]:
    """UNKNOWN paths INSIDE a slot's `override` and `rider`, and nowhere else.

    This is deliberately the exact complement of `Card.unknown_fields()`, which
    walks the slot's own fields (honouring NOT_APPLICABLE per slot kind) and
    then stops -- it never descends into `override` or `rider`. The two together
    cover the whole graph with no overlap and no gap.

    `override`/`rider` being UNKNOWN themselves is `unknown_fields()`'s business,
    not this function's; it only looks a level deeper.
    """
    found: list[str] = []
    for attribute in ("override", "rider"):
        nested = getattr(slot, attribute, None)
        if not (dataclasses.is_dataclass(nested) and not isinstance(nested, type)):
            continue
        for f in dataclasses.fields(nested):
            if not is_known(getattr(nested, f.name)):
                found.append(f"{prefix}.{attribute}.{f.name}")
    return found


def nested_unknown_paths(card: Card) -> list[str]:
    """Every untranscribed field `Card.unknown_fields()` cannot see.

    A `None` is NOT unknown here -- the schema's three-state contract makes
    `None` the affirmative "structurally absent" marker (`{"kind": "ungated"}`
    -> `None`), and only the sentinel means untranscribed.
    """
    from hbuilder.domain.card import SKILL_SLOT_KINDS

    found: list[str] = []
    for slot_kind, slot in zip(SKILL_SLOT_KINDS, card.skills):
        if slot is not None and dataclasses.is_dataclass(slot):
            found.extend(_nested_unknown_in_slot(slot, f"skills.{slot_kind}"))
    if card.costume_skill is not None and dataclasses.is_dataclass(card.costume_skill):
        found.extend(_nested_unknown_in_slot(card.costume_skill, "costume_skill"))
    return found


def baseline_unknown_paths(cards: Iterable[Card]) -> frozenset[str]:
    """Every NESTED unknown path the existing catalogue already exhibits.

    The second gate is comparative rather than absolute: **an addition may not
    leave a field untranscribed in a
    position where no existing card leaves one.**

    It has to be comparative because a nested UNKNOWN is not automatically a
    defect. `skills.SPECIAL.rider.target` is null on 30 real cards -- the
    importer emits no target for a rider on ACTIVE/SPECIAL, where the game
    states none -- so rejecting it would reject a third of the roster.
    `costume_skill.rider.target` is null on none of them, because a costume DOES
    state its target, so a null there is a transcription slip. The catalogue
    itself is the discriminator, and it
    stays right as the data grows.

    ⚠ It is only as sharp as the catalogue is diverse: against a nearly empty
    catalogue it degenerates to "no nested unknowns at all". That is the
    conservative direction (over-report, never over-admit), and with the real
    169 cards every slot kind is exhibited many times over.
    """
    return frozenset(path for card in cards for path in nested_unknown_paths(card))


def screen(
    record: Any, index: int = 0, allowed_unknown: frozenset[str] | None = None
) -> tuple[Card | None, Rejection | None]:
    """Admit a candidate record, or reject it with a reason. Never guesses."""
    card_id = record.get("id", "<no id>") if isinstance(record, dict) else "<not an object>"
    errors = validate_card(record, index)
    if errors:
        return None, Rejection(card_id, "schema", f"{len(errors)} error(s); first: {errors[0]}")
    try:
        card = card_from_record(record)
    except Exception as exc:  # noqa: BLE001 -- the reason is the product here
        return None, Rejection(card_id, "construct", f"{type(exc).__name__}: {exc}")

    # Gate 1 -- the rule's own words: `load_cards` accepting it with no `unknown_fields`.
    reported = card.unknown_fields()
    if reported:
        return None, Rejection(
            card_id,
            "untranscribed",
            f"unknown_fields: {', '.join(reported)} -- reported, not guessed",
        )
    # Gate 2 -- and no untranscribed field in a position the catalogue never
    # leaves untranscribed. Skipped when there is no catalogue to compare with.
    if allowed_unknown is not None:
        novel = sorted(set(nested_unknown_paths(card)) - allowed_unknown)
        if novel:
            return None, Rejection(
                card_id,
                "untranscribed",
                f"UNKNOWN at {', '.join(novel)}, which no existing card leaves "
                "untranscribed -- reported, not guessed",
            )
    return card, None


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------


@dataclasses.dataclass
class Plan:
    additions: list[dict]
    rejections: list[Rejection]
    present: int
    draft_total: int

    @property
    def owed(self) -> bool:
        return bool(self.additions) or bool(self.rejections)


def plan_additions(catalogue_doc: dict, draft_doc: dict) -> Plan:
    """What the draft has that the catalogue lacks, screened.

    Only ids ABSENT from the catalogue are ever considered, so a card the
    catalogue already carries is untouchable by construction -- there is no
    code path here that can reach an existing record, not merely a rule
    saying it must not.
    """
    have = {c["id"] for c in catalogue_doc["cards"] if isinstance(c, dict) and "id" in c}
    existing_cards = [card_from_record(c) for c in catalogue_doc["cards"]]
    allowed = baseline_unknown_paths(existing_cards) if existing_cards else None
    additions: list[dict] = []
    rejections: list[Rejection] = []
    seen: set[str] = set()
    for index, record in enumerate(draft_doc.get("cards", [])):
        card_id = record.get("id") if isinstance(record, dict) else None
        if card_id in have:
            continue
        if card_id in seen:
            rejections.append(Rejection(str(card_id), "duplicate", "appears twice in the draft"))
            continue
        seen.add(str(card_id))
        card, rejection = screen(record, index, allowed)
        if rejection is not None:
            rejections.append(rejection)
            continue
        additions.append(record)
    return Plan(
        additions=additions,
        rejections=rejections,
        present=len(have),
        draft_total=len(draft_doc.get("cards", [])),
    )


# --------------------------------------------------------------------------
# applying
# --------------------------------------------------------------------------


class RefusedToWrite(RuntimeError):
    """The write did not happen, and the reason is the message."""


def _merge(existing: list[dict], additions: list[dict]) -> list[dict]:
    """Existing records carried over BY IDENTITY, additions slotted in.

    Sorted-by-id insertion when the file is sorted (it is), so an
    addition produces a small local diff instead of moving everything after
    it. If the file ever stops being sorted, additions go on the end rather
    than reordering anything: preserving the existing order matters more than
    a tidy insertion point.
    """
    ids = [c["id"] for c in existing]
    if ids != sorted(ids):
        return [*existing, *additions]
    merged = list(existing)
    for record in additions:
        position = len(merged)
        for i, current in enumerate(merged):
            if current["id"] > record["id"]:
                position = i
                break
        merged.insert(position, record)
    return merged


@dataclasses.dataclass
class Result:
    added: list[str]
    rejections: list[Rejection]
    before_bytes: int
    after_bytes: int
    wrote: bool


def apply_additions(
    cards_path: Path = CARDS_JSON,
    draft_path: Path = DRAFT_JSON,
    *,
    dry_run: bool = False,
) -> Result:
    raw = cards_path.read_bytes()
    catalogue = json.loads(raw.decode("utf-8"))
    draft = json.loads(draft_path.read_text(encoding="utf-8"))

    # Guarantee 1: the encoder must reproduce THIS file before it is trusted to
    # rewrite it. A mismatch means the addition would arrive as a reformat.
    if encode(catalogue) != raw:
        raise RefusedToWrite(
            f"{cards_path} is not in this module's canonical encoding (indent=2, "
            "sort_keys=True, ensure_ascii=False, CRLF, trailing CRLF), so writing it "
            "would reformat the whole file rather than add to it. Refusing."
        )

    plan = plan_additions(catalogue, draft)
    if not plan.additions:
        return Result([], plan.rejections, len(raw), len(raw), False)

    existing = catalogue["cards"]
    merged = _merge(existing, plan.additions)
    new_doc = {**catalogue, "cards": merged}

    # Guarantee 2, checked before the write: every pre-existing record is the
    # very object that was read, and their relative order has not moved.
    added_ids = {r["id"] for r in plan.additions}
    kept = [c for c in merged if c["id"] not in added_ids]
    if len(kept) != len(existing) or any(a is not b for a, b in zip(kept, existing)):
        raise RefusedToWrite("an existing record was rebuilt or reordered -- refusing")

    out = encode(new_doc)
    if dry_run:
        return Result(sorted(added_ids), plan.rejections, len(raw), len(out), False)

    cards_path.write_bytes(out)
    try:
        verify(cards_path, existing, added_ids)
    except Exception:
        cards_path.write_bytes(raw)  # never leave it in an unverified state
        raise
    return Result(sorted(added_ids), plan.rejections, len(raw), len(out), True)


def verify(cards_path: Path, existing: list[dict], added_ids: set[str]) -> None:
    """Re-read what was written and agree with it, or raise.

    Verifying the artefact rather than the counter: `apply_additions` reporting
    its own success proves nothing, so the file goes back through the real
    loader and every pre-existing record is compared by value.
    """
    written = json.loads(cards_path.read_bytes().decode("utf-8"))
    doc_errors = validate_document(written)
    if doc_errors:
        raise RefusedToWrite(f"the written document does not validate: {doc_errors[0]}")
    by_id = {c["id"]: c for c in written["cards"]}
    for record in existing:
        if by_id.get(record["id"]) != record:
            raise RefusedToWrite(
                f"existing entry {record['id']} changed on write -- restored"
            )
    if not added_ids <= set(by_id):
        raise RefusedToWrite(f"additions missing after write: {sorted(added_ids - set(by_id))}")
    loaded = load_cards(cards_path)
    if len(loaded) != len(existing) + len(added_ids):
        raise RefusedToWrite(
            f"load_cards read {len(loaded)} cards, expected "
            f"{len(existing) + len(added_ids)}"
        )


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def report_lines(result: Result, *, dry_run: bool) -> list[str]:
    lines: list[str] = []
    if result.added:
        verb = "would add" if dry_run else "ADDED"
        lines.append(f"{verb} {len(result.added)} card(s) to data/cards.json:")
        lines += [f"   + {cid}" for cid in result.added]
        lines.append(f"   {result.before_bytes} -> {result.after_bytes} bytes")
    else:
        lines.append("no cards to add -- the catalogue has every card the game ships.")
    for rejection in result.rejections:
        lines.append(f"!! NOT added, {rejection}")
    if result.rejections:
        lines.append(
            "   a card that cannot be translated cleanly is reported, never guessed "
            "-- add it by hand once the schema can hold it."
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(
        description="Add cards the game ships and data/cards.json lacks. "
        "Append-only: never modifies or removes an existing entry.",
    )
    ap.add_argument("--cards", default=str(CARDS_JSON), help="the catalogue to add to")
    ap.add_argument("--draft", default=str(DRAFT_JSON), help="import_to_cards.py's draft")
    ap.add_argument("--dry-run", action="store_true", help="plan and report, write nothing")
    args = ap.parse_args(argv)

    try:
        result = apply_additions(
            Path(args.cards), Path(args.draft), dry_run=args.dry_run
        )
    except RefusedToWrite as exc:
        print(f"REFUSED: {exc}")
        return 1
    for line in report_lines(result, dry_run=args.dry_run):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
