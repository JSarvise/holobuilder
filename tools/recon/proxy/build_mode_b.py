# Build the MODE B collection file: the account's REAL per-card stats, joined onto
# data/cards.json's hand-authored skills / gates / identity.
#
# WHY THIS EXISTS
#   data/cards.json holds MAXED stats, so every ranking the solver has ever produced
#   answers "what is the best team if every card were maxed?" -- a question nobody asked.
#   dumps/card_params.json IS the account's real collection: one row per card straight off the
#   decrypted `rpc.api.Card/GetParameters` response, verified against the in-game card
#   screen (tools/recon/proxy/parse_card_params.py header). Nothing needs deriving.
#
# WHAT IT IS NOT
#   It is NOT an automated path into data/cards.json or data/owned.json. Both stay
#   authoritative and hand-edited; this script refuses to write to either (see _guard_output).
#   It is a recon tool the operator runs BY HAND. The product only ever READS the file
#   it emits (`hbuilder solve --real-stats <path>`).
#
# OUTPUT (default): tools/recon/proxy/dumps/cards.real.json
#   dumps/ is gitignored, which is where account-derived data belongs -- it is the user's
#   own account state, not a hand-authored project asset, and it must never be committed.
#   Shape: a normal cards.json document (schema_version + cards[]) plus one extra top-level
#   `provenance` object. Same shape means the existing loader/validator path is reused
#   rather than forked; the `provenance` block is what makes it impossible to mistake the
#   file for the maxed default, and hbuilder refuses to treat a file without it as mode B.
#
# THE JOIN IS VERIFIED, NOT ASSUMED
#   Every card_params row must match a data/cards.json id exactly. An unmatched row is
#   reported by id and makes this script exit non-zero -- it is never dropped silently.
#
# STAT FIELD ORDER: stat3=Performance, stat4=Technique, stat5=Sense, confirmed in
#   parse_card_params.py against the in-game card screen. This script re-derives that
#   mapping independently (`mapping_report`): one card sits at one level and one bloom
#   stage, so its three real/maxed ratios should agree; under a wrong permutation they
#   scatter. The canonical mapping must win, and it wins by a wide margin (about 9x in
#   mean spread over the nearest rival).
#
# Usage:
#   python tools/recon/proxy/build_mode_b.py                       # defaults, from repo root
#   python tools/recon/proxy/build_mode_b.py -o some/other.json --check-only
import argparse
import hashlib
import itertools
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CARDS = REPO_ROOT / "data" / "cards.json"
DEFAULT_PARAMS = REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "card_params.json"
DEFAULT_OUT = REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "cards.real.json"

# Never writable by any code path in this repository, by the project's data-authority rule.
PROTECTED = ("data/cards.json", "data/owned.json")

# card_params.json field -> Stats field. See header.
STAT_BY_PARAM_FIELD = {"stat3": "performance", "stat4": "technique", "stat5": "sense"}

CAVEATS = [
    "These are the owner's REAL stats as captured. They are NOT maxed and NOT predictable: "
    "the published materialisation formula reproduces 0 of 91 of them (tools/recon/MODEL.md). "
    "Mode B therefore cannot answer 'what if I levelled this card?'.",
    "Only the owner's 91 owned cards are present. The other 76 cards in data/cards.json have "
    "no measured real stats, so --include-unowned cannot widen the pool in mode B.",
    "The stats carry no deck context: rpc.api.Card/GetParameters returns one row per owned "
    "card with no team supplied, so team-dependent CardParameterUpBy{LivePassiveSkill, "
    "LiveLeaderSkill} cannot be baked in and the solver's own stat_up stage is not "
    "double-counting them. See tools/recon/MODE-B.md.",
    "Skills, gates, generation, type and rarity are copied verbatim from data/cards.json at "
    "build time. skills_source_sha256 pins that file; hbuilder warns if it has since changed.",
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _recorded_path(path: Path) -> str:
    """How a source path is named inside the emitted provenance block:
    repo-relative POSIX when the file is inside the repo, absolute otherwise.

    Not a cosmetic choice -- hbuilder resolves `skills_source` against its own
    working directory to check whether the hand-edited file has changed since
    this document was built. Recording a fixed string instead of the path
    actually read would make that check compare the wrong two files."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def _read_json(path: Path) -> object:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _guard_output(out_path: Path) -> None:
    """Refuse to emit over an authoritative hand-edited file. Compares resolved
    paths, so `data/../data/cards.json` and an absolute path are both caught."""
    resolved = out_path.resolve()
    for relative in PROTECTED:
        if resolved == (REPO_ROOT / relative).resolve():
            raise SystemExit(
                f"refusing to write {relative}: it is authoritative and hand-edited "
                f"(.claude/CLAUDE.md). Pick another -o path."
            )


def join(cards_doc: dict, params: list[dict]) -> tuple[list[dict], list[str]]:
    """Merge real stats onto the hand-authored card records.

    Returns `(records, unmatched_param_ids)`. `records` is in data/cards.json
    order (not card_params order) so the emitted document diffs cleanly against
    the maxed one. `unmatched_param_ids` names every card_params row with no
    cards.json record -- the caller treats a non-empty list as a failure rather
    than dropping rows.
    """
    by_id = {record["id"]: record for record in cards_doc["cards"]}
    params_by_id = {row["id"]: row for row in params}
    unmatched = sorted(cid for cid in params_by_id if cid not in by_id)

    records: list[dict] = []
    for record in cards_doc["cards"]:
        row = params_by_id.get(record["id"])
        if row is None:
            continue
        merged = dict(record)
        merged["stats"] = {
            stat: row[field] for field, stat in STAT_BY_PARAM_FIELD.items()
        }
        records.append(merged)

    return records, unmatched


def mapping_report(cards_doc: dict, params: list[dict]) -> list[tuple[tuple[str, ...], float]]:
    """Re-derive the stat3/4/5 -> performance/technique/sense mapping from the data
    instead of trusting the header comment.

    One card sits at one level and one bloom stage, so all three of its stats scale
    off the maxed values by a similar factor; only the per-stat potential-upgrade
    allocation perturbs them. Under a *wrong* permutation the three ratios scatter
    wildly. Returns `[(permutation, mean ratio spread), ...]` sorted best first --
    the caller asserts the canonical mapping is the winner.
    """
    by_id = {record["id"]: record for record in cards_doc["cards"]}
    names = ("performance", "technique", "sense")
    scored: list[tuple[tuple[str, ...], float]] = []
    for permutation in itertools.permutations(names):
        total = 0.0
        count = 0
        for row in params:
            record = by_id.get(row["id"])
            if record is None:
                continue
            maxed = record["stats"]
            real = dict(zip(permutation, (row["stat3"], row["stat4"], row["stat5"])))
            ratios = [real[name] / maxed[name] for name in names if maxed.get(name)]
            if len(ratios) == 3:
                total += max(ratios) - min(ratios)
                count += 1
        scored.append((permutation, total / count if count else float("inf")))
    return sorted(scored, key=lambda entry: entry[1])


def build_document(cards_path: Path, params_path: Path) -> tuple[dict, list[str]]:
    cards_doc = _read_json(cards_path)
    params = _read_json(params_path)
    records, unmatched = join(cards_doc, params)

    document = {
        "schema_version": cards_doc["schema_version"],
        "provenance": {
            "mode": "B",
            "stats_basis": "real",
            "description": (
                "The owner's REAL per-card stats, captured from their own account. "
                "NOT the maxed hypothetical that data/cards.json holds."
            ),
            "generated_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "generated_by": "tools/recon/proxy/build_mode_b.py",
            "stats_source": _recorded_path(params_path),
            "stats_source_rpc": "rpc.api.Card/GetParameters (decrypted capture)",
            "stats_source_sha256": _sha256(params_path),
            "skills_source": _recorded_path(cards_path),
            "skills_source_sha256": _sha256(cards_path),
            "card_count": len(records),
            "stat_rows_in_source": len(params),
            "cards_json_total": len(cards_doc["cards"]),
            "unmatched_stat_ids": unmatched,
            "stat_field_mapping": {
                field: stat for field, stat in STAT_BY_PARAM_FIELD.items()
            },
            "caveats": CAVEATS,
        },
        "cards": records,
    }
    return document, unmatched


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Emit the mode-B (real-stat) collection file. Never writes "
        "data/cards.json or data/owned.json."
    )
    parser.add_argument("--cards", default=str(DEFAULT_CARDS), help="Path to cards.json")
    parser.add_argument(
        "--params", default=str(DEFAULT_PARAMS), help="Path to card_params.json"
    )
    parser.add_argument("-o", "--out", default=str(DEFAULT_OUT), help="Output path")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Verify the join and print the report without writing anything",
    )
    args = parser.parse_args(argv)

    cards_path = Path(args.cards)
    params_path = Path(args.params)
    out_path = Path(args.out)
    _guard_output(out_path)

    document, unmatched = build_document(cards_path, params_path)
    provenance = document["provenance"]

    print(
        f"join: {provenance['card_count']} matched of "
        f"{provenance['stat_rows_in_source']} stat rows "
        f"({provenance['cards_json_total']} cards in {cards_path})"
    )
    if unmatched:
        print(f"UNMATCHED stat rows ({len(unmatched)}): " + ", ".join(unmatched))

    ranking = mapping_report(_read_json(cards_path), _read_json(params_path))
    (best, best_spread), (_, runner_up_spread) = ranking[0], ranking[1]
    canonical = tuple(STAT_BY_PARAM_FIELD[field] for field in ("stat3", "stat4", "stat5"))
    print(
        f"stat mapping: best={'/'.join(best)} spread={best_spread:.4f} "
        f"(runner-up {runner_up_spread:.4f}); canonical={'/'.join(canonical)}"
    )
    if best != canonical:
        print(
            "REFUSING to emit: the data does not support the stat3/4/5 mapping this "
            "tool applies. Re-verify against the in-game card screen.",
            file=sys.stderr,
        )
        return 1

    missing_costume = [r["id"] for r in document["cards"] if r.get("costume_skill") is None]
    print(
        f"gap: {len(missing_costume)} of {provenance['card_count']} merged cards have no "
        "costume_skill transcribed in data/cards.json (LiveLeaderSkill has one row per "
        "card, so those outfit skills exist in game -- transcribe by hand, never automate)"
    )

    if unmatched:
        print("REFUSING to emit: every stat row must join. Fix the join first.", file=sys.stderr)
        return 1

    if args.check_only:
        print("--check-only: nothing written")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {out_path} ({provenance['card_count']} cards, mode B, real stats)")
    print(f"  use: python -m hbuilder solve --real-stats {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
