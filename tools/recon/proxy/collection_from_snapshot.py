"""Merge a SAVED account snapshot into `state/collection.json` — offline, no credential.

`account_snapshot.py --snapshot --merge-collection` does this too, but only as the tail of a
live `Auth/Login -> User/Get` round trip: it needs the network and the account's real
credential. That is the right tool when the account has moved since the last capture and
the wrong one when it has not — re-authenticating to re-read something already sitting in
`dumps/` spends a credential for nothing and cannot be re-run offline to check the result.

So this is the same merge over a snapshot file that already exists. It imports
`merge_collection` from `account_snapshot.py` rather than reimplementing it: there is exactly one
definition of how a snapshot becomes a collection, and a second copy would be free to drift from
it silently. Everything that function guarantees still holds —

  * `state/collection.json` is MERGED, never overwritten. `cards`, `card_default` and
    `player.posters` move -- the last from the account's own `user_poster_list`
    count, which drives the Memory Bonus; `player.level` and `player.power_up_permyriad` are
    hand-entered, a snapshot has no notion of them, and writing the candidate over the top
    would destroy them.
  * `dumps/collection_before_import.json` is written once, the first time, and never replaced —
    a backup that re-backs-up on every run has overwritten the thing it was protecting.
  * `data/cards.json` and `data/owned.json` are never touched.

⚠ **This writes the user's own account file, as a deliberate exception.** The project's
data-authority rule says no automated path writes `state/collection.json`, so a pipeline cannot
silently replace hand-entered truth with a guess. Nothing here guesses: every value is the
account's own, read back from the game's own `User/Get`. A stale collection does real damage (a
card one level behind, or an owned card missing, can turn a derived `power_up_permyriad` into an
unusable bound). It stays **explicitly invoked**, never a side effect of another command.

Usage:
    proxyenv/Scripts/python collection_from_snapshot.py [--snapshot dumps/account_snapshot_main.json]
    proxyenv/Scripts/python collection_from_snapshot.py --dry-run     # show the diff, write nothing
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from account_snapshot import DUMPS, STATE, merge_collection, resolve_levels  # noqa: E402

DEFAULT_SNAPSHOT = DUMPS / "account_snapshot_main.json"


def _entries_of(snapshot: dict) -> dict[str, dict]:
    """The same (level, bloom, limit_break) projection `merge_collection` writes, so the
    dry-run diff below describes exactly what a real run would do rather than approximating
    it."""
    out: dict[str, dict] = {}
    for card in snapshot.get("cards", []):
        card_id = card.get("card_id")
        if not card_id:
            continue
        entry: dict = {}
        if "level" in card:
            entry["level"] = card["level"]
        entry["bloom"] = int(card.get("bloom") or 0)
        entry["limit_break"] = int(card.get("limit_break") or 0)
        out[card_id] = entry
    return out


def describe(snapshot: dict) -> list[str]:
    """What the merge would change, per card. Printed before writing and by `--dry-run`.

    Verify artefacts, not counters: "97 cards merged" is a claim the tool makes about itself,
    while "Ina'nis level 69 -> 70" is a statement about the file that can be checked afterwards."""
    state = STATE / "collection.json"
    current = json.loads(state.read_text("utf8")) if state.exists() else {}
    before = current.get("cards") or {}
    after = _entries_of(snapshot)

    lines: list[str] = []
    for card_id in sorted(set(before) | set(after)):
        old, new = before.get(card_id), after.get(card_id)
        if old == new:
            continue
        if old is None:
            lines.append(f"  + {card_id}  {new}   (owned, was not recorded)")
        elif new is None:
            lines.append(f"  - {card_id}  {old}   (recorded, not in the snapshot)")
        else:
            moved = ", ".join(
                f"{k} {old.get(k)!r} -> {new.get(k)!r}"
                for k in ("level", "bloom", "limit_break")
                if old.get(k) != new.get(k)
            )
            lines.append(f"  ~ {card_id}  {moved}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--snapshot", default=str(DEFAULT_SNAPSHOT),
        help=f"saved snapshot to merge (default {DEFAULT_SNAPSHOT})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the per-card diff and write nothing",
    )
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    path = Path(args.snapshot)
    if not path.exists():
        print(
            f"no snapshot at {path}.\n"
            f"Take one with:  proxyenv/Scripts/python account_snapshot.py --snapshot",
            file=sys.stderr,
        )
        return 2
    snapshot = json.loads(path.read_text("utf8"))
    cards = snapshot.get("cards")
    if not cards:
        print(f"{path} carries no 'cards' — refusing to merge an empty collection", file=sys.stderr)
        return 2

    print(f"snapshot  : {path}  ({len(cards)} cards)")
    # Annotate each card with its LEVEL, resolved from cumulative exp via the
    # CardLevel curve. The snapshot carries exp, not level, so without this the
    # merge writes only bloom/limit_break and every card scores at an unknown
    # level -> None stats. resolve_levels mutates in place, so describe() and
    # merge_collection() below both see it.
    resolved = resolve_levels(snapshot)
    if resolved:
        print(f"levels    : resolved on {resolved}/{len(cards)} cards from exp")
    else:
        print(
            "levels    : WARNING resolved on 0 cards -- dumps/holodori/"
            "main_Card.json + main_CardLevel.json missing; cards will merge "
            "WITHOUT a level (unknown-level stats). Regenerate them first.",
            file=sys.stderr,
        )
    changes = describe(snapshot)
    if not changes:
        print("no change : state/collection.json already matches this snapshot")
        return 0
    print(f"\nwould change {len(changes)} card(s):")
    for line in changes:
        print(line)

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    merge_collection(snapshot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
