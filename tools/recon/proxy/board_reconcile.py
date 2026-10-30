"""Board-line parity harness: reconcile the model's Holomem Board power line
against the SERVER's per-deck decomposition, over the 29-deck live oracle.

For each oracle deck it:
  1. reconstructs the 5 member cards at LIVE levels (exp->level via the master
     CardLevel curve, read OFFLINE from the game's own octo cache through
     master_cache; live bloom + limit_break from account_snapshot_main.json),
  2. maps the leader (character, costume) to a scoreable leader CARD,
  3. builds the FRESH board (board_state.json -> build_board -> BoardModel),
  4. scores the exact team through the CURRENT scorer
     (score_with_breakdown(team, cfg).board_power),
  5. diffs it against ev["power"]["up_by_skill_tree [Holomem Board]"].

Reads only gitignored data already on disk. Writes only a scratch collection
doc under the OS temp dir. Does not touch tracked src or the oracle.
"""
from __future__ import annotations

import bisect
import dataclasses
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
DUMPS = HERE / "dumps"
REPO = HERE.parents[2]

# apply_collection resolves card_level_curve.json / caps RELATIVE to cwd
# (COLLECTION_CURVE_PATH). Run from the repo root or every card would silently
# score fully-maxed. See tree_optimise.py's "SILENT BASIS SWITCH" note.
os.chdir(REPO)

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "src"))

import build_board  # noqa: E402
import master_cache  # noqa: E402
from hbuilder.cli import apply_collection, build_parser  # noqa: E402
from hbuilder.domain.board import board_from_mapping  # noqa: E402
from hbuilder.domain.loader import load_cards, load_owned  # noqa: E402
from hbuilder.domain.scoring_config import load_scoring_config  # noqa: E402
from hbuilder.domain.scoring import score_with_breakdown  # noqa: E402
from hbuilder.domain.team import Team  # noqa: E402

# Scratch for the derived live collection: the OS temp dir, so the path is
# valid on any machine.
import tempfile  # noqa: E402

SCRATCH = Path(tempfile.gettempdir()) / "hololive-builder-reconcile"
SCRATCH.mkdir(parents=True, exist_ok=True)
LIVE_COLLECTION = SCRATCH / "live_collection.json"

# Whether THIS process has written LIVE_COLLECTION. The file survives across
# processes, and that is a trap: after an account refresh levels a card 1 -> 70,
# `resolve_live_levels` says 70 while a stale doc still scores the card at its
# LEVEL-1 stats, which looks like a model error that does not exist. So
# `build_cards` refuses a doc this process did not write. See its docstring.
_LIVE_COLLECTION_IN_PROCESS = False

SERVER_BOARD_KEY = "up_by_skill_tree [Holomem Board]"


def _load(p: Path):
    return json.loads(Path(p).read_text("utf8"))


def build_exp_level_curve() -> dict[str, list[tuple[int, int]]]:
    """group_id -> sorted [(cumulative_exp, level)] from master CardLevel.

    CardLevel row data fields (as account_snapshot.load_level_curves reads them):
      1=group_id  2=level  3=parameter_base_value  4=cumulative exp  5=power permyriad.
    Level-1 rows omit field 4 (exp 0)."""
    curve: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for row in master_cache.rows("CardLevel"):
        d = row["data"]
        level = (d.get(2) or [0])[0]
        exp = (d.get(4) or [0])[0]
        curve[row["group_id"]].append((int(exp), int(level)))
    for g in curve:
        curve[g].sort()
    return dict(curve)


def resolve_live_levels(snapshot: dict, level_curve: dict) -> dict[str, dict]:
    """card_id -> {level, bloom, limit_break} from the LIVE snapshot.

    level is derived from the card's exp by bisecting its level-group's
    cumulative-exp thresholds. bloom/limit_break are taken verbatim from the
    snapshot. Drift-free vs the oracle: same live login moment."""
    card_meta = _load(DUMPS / "card_level_curve.json")["cards"]
    out: dict[str, dict] = {}
    unresolved: list[str] = []
    for c in snapshot.get("cards", []):
        cid = c.get("card_id")
        if not cid:
            continue
        meta = card_meta.get(cid)
        exp = int(c.get("exp") or 0)
        if meta is None:
            unresolved.append(cid)
            level = 1
        else:
            rows = level_curve.get(meta["level_group"])
            if not rows:
                unresolved.append(cid)
                level = 1
            else:
                i = bisect.bisect_right([e for e, _ in rows], exp) - 1
                level = rows[max(i, 0)][1]
        out[cid] = {
            "level": level,
            "bloom": int(c.get("bloom") or 0),
            "limit_break": int(c.get("limit_break") or 0),
        }
    return out, unresolved


def write_live_collection(live_levels: dict[str, dict], base_path=None) -> None:
    """A state/collection.json-shaped doc sourced from the LIVE snapshot, so
    apply_collection restates every card's stats at the oracle's real levels.
    Preserves the hand-entered player block so account bonuses still load.

    ⚠ `base_path` is where the PLAYER block comes from, and it is a separate
    argument because the player block is account state too. Frozen card levels
    with a live `player` block do not match: one extra poster moves the Memory
    Bonus and breaks a frozen comparison such as `test_overall_power_exact`.
    Freeze the cards and the player block together or neither is frozen."""
    global _LIVE_COLLECTION_IN_PROCESS
    base = _load(Path(base_path) if base_path else REPO / "state" / "collection.json")
    doc = {
        "schema_version": base.get("schema_version"),
        "card_default": base.get("card_default"),
        "player": base.get("player"),
        "cards": live_levels,
    }
    LIVE_COLLECTION.write_text(json.dumps(doc, indent=1), "utf8")
    _LIVE_COLLECTION_IN_PROCESS = True


def build_cards(live_levels: dict, base_path=None, roster=None) -> tuple[dict, object, int]:
    """Load the catalogue and restate every card at LIVE levels. Returns
    (by_id, cfg, applied) where cfg carries the account bonuses and (later)
    the board.

    `live_levels` is AUTHORITATIVE. Non-empty means "score exactly these
    levels": the collection doc is (re)written from it HERE, so the handed
    levels and the scored levels can never desync (`base_path` is passed
    through to `write_live_collection` for frozen-basis callers). Empty means
    "score the doc `write_live_collection` wrote EARLIER IN THIS PROCESS" —
    the `test_overall_power_exact` frozen-fixture form — and a doc merely
    found on disk is refused.

    ⚠ `LIVE_COLLECTION` is a temp file that survives across processes. Scoring
    a doc left by another process desyncs any card whose level moved since it
    was written (a card levelled 1 -> 70 scores at its LEVEL-1 stats), so the
    contract is enforced here rather than left to caller convention.
    `tests/test_board_reconcile_basis.py` pins both halves.

    `roster` (card ids), when given, limits the OWNED roster to those cards --
    for frozen-basis callers. Member Power-Up's `"auto"` permyriad is derived
    from every owned card, so the live `data/owned.json` leaks into a frozen
    comparison the moment the refresh appends a card (one new card moves every
    corpus row's power-up and leaves `test_overall_power_exact` with 0
    applicable rows). Pass the frozen collection's card ids."""
    if live_levels:
        write_live_collection(live_levels, base_path)
    elif not _LIVE_COLLECTION_IN_PROCESS:
        raise SystemExit(
            "build_cards({}) with no in-process write_live_collection: "
            f"{LIVE_COLLECTION} was left by an EARLIER process and may carry "
            "stale levels. Call write_live_collection first (or pass the "
            "levels to build_cards)."
        )
    defaults = build_parser().parse_args(["solve"])
    cards = load_cards(REPO / defaults.cards)
    owned = load_owned(REPO / defaults.owned, cards)
    if roster is not None:
        keep = set(roster)
        owned = [c for c in owned if c.id in keep]
    cfg = load_scoring_config(REPO / "config.json")
    cards, basis = apply_collection(
        cards,
        str(LIVE_COLLECTION),
        owned_ids=[c.id for c in owned],
        maxed=False,
    )
    if basis.path and basis.applied == 0 and not basis.maxed:
        raise SystemExit(
            "collection not applied -> cards would score fully-maxed. "
            "Run from repo root so card_level_curve.json resolves."
        )
    if basis.bonuses is not None:
        cfg = dataclasses.replace(cfg, account=basis.bonuses)
    by_id = {c.id: c for c in cards}
    return by_id, cfg, basis.applied


def build_board_model(board_state_path=None, collection_path=None):
    """The `BoardModel` for the account's CURRENT board, or for a frozen one.

    Both paths exist for the same reason: a test that compares CAPTURED server
    rows against the model must drive the model with the account state OF THAT
    MOMENT. `dumps/board_state.json` moves the instant a board point is spent,
    and `state/collection.json` moves on every merge — and the collection is
    read HERE, not just by the caller, because a socketed card's bloom sets the
    node's connect multiplier.

    Freezing the board alone is not enough: the next collection merge moves the
    multipliers underneath it. Same instant, same basis: pass BOTH fixtures, or
    neither.
    """
    state = _load(Path(board_state_path) if board_state_path
                  else DUMPS / "board_state.json")
    document, _report, problems = build_board.build(
        state,
        _load(DUMPS / "skilltree.json"),
        _load(DUMPS / "master_SkillTreeNode.json"),
        level=None,
        collection=_load(Path(collection_path) if collection_path
                         else REPO / "state" / "collection.json"),
        caps_by_rarity=_load(DUMPS / "card_caps.json"),
    )
    if problems:
        raise SystemExit(f"build_board problems: {problems[:3]}")
    return board_from_mapping(document, source="<board_reconcile>")


def _chr_of(card_id: str) -> str:
    """chr-id encoded in a card id: card-00001-4-... -> chr-00001."""
    return "chr-" + card_id.split("-")[1]


def resolve_leader_card(leader_char: str, member_cards, by_id):
    """Map leader (character,costume) -> a scoreable leader CARD.

    board_power reads ONLY leader.member (the display name of the red
    leader-scope holomem who leads); the costume drives Outfit Skill, a DIFFERENT
    line not scored here. The oracle's leader is a chr-id; a card's chr-id is
    the second field of its card id, and card.member is the display name the
    board is keyed by. So any card whose chr-id == leader_char is EXACT for the
    board line. Prefer the member card of that character already in the deck (the
    in-game leader is one of the 5); else fall back to any catalogue card."""
    for c in member_cards:
        if _chr_of(c.id) == leader_char:
            return c, "member-slot"
    for c in by_id.values():
        if _chr_of(c.id) == leader_char:
            return c, "catalogue-fallback"
    return None, "UNRESOLVED"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    oracle = _load(DUMPS / "deck_oracle_live.json")
    snapshot = _load(DUMPS / "account_snapshot_main.json")

    level_curve = build_exp_level_curve()
    live_levels, unresolved = resolve_live_levels(snapshot, level_curve)
    write_live_collection(live_levels)
    by_id, cfg, applied = build_cards(live_levels)
    board = build_board_model()
    cfg = dataclasses.replace(cfg, board=board)

    print(f"# cards restated from live snapshot: {applied} "
          f"(unresolved exp->level: {len(unresolved)})", file=sys.stderr)

    rows = []
    skipped = []
    for idx, ev in enumerate(oracle):
        slots = ev["cards"]
        member_ids = [slots[f"slot{i}"]["card"] for i in range(1, 6)]
        missing = [m for m in member_ids if m not in by_id]
        if missing:
            skipped.append((idx, f"members not in catalogue: {missing}"))
            continue
        member_cards = [by_id[m] for m in member_ids]
        leader_char = ev["leader"]["character"]
        leader_card, how = resolve_leader_card(leader_char, member_cards, by_id)
        if leader_card is None:
            skipped.append((idx, f"leader char {leader_char} unresolvable"))
            continue
        try:
            team = Team(leader=leader_card, members=tuple(member_cards))
        except Exception as e:  # noqa: BLE001
            skipped.append((idx, f"Team build failed: {e}"))
            continue
        bd = score_with_breakdown(team, cfg)
        our = bd.board_power
        server = ev["power"][SERVER_BOARD_KEY]
        resid = our - server
        pct = (resid / server * 100.0) if server else float("nan")
        rows.append({
            "idx": idx,
            "leader_char": leader_char,
            "leader_card": leader_card.id,
            "how": how,
            "server": server,
            "our": our,
            "resid": resid,
            "pct": pct,
            "members": member_ids,
        })

    print(json.dumps({"rows": rows, "skipped": skipped,
                      "unresolved_levels": unresolved}, indent=1))

    # console summary
    print("\n idx  leader_char  leader_card                    how              "
          "server      our     resid   resid%", file=sys.stderr)
    for r in rows:
        print(f" {r['idx']:>3}  {r['leader_char']:<11}  {r['leader_card']:<28}  "
              f"{r['how']:<16} {r['server']:>7}  {r['our']:>9.2f}  "
              f"{r['resid']:>8.2f}  {r['pct']:>7.3f}%", file=sys.stderr)
    if rows:
        resids = [r["resid"] for r in rows]
        pcts = [r["pct"] for r in rows]
        n = len(resids)
        mean = sum(resids) / n
        med = sorted(resids)[n // 2]
        meanp = sum(pcts) / n
        medp = sorted(pcts)[n // 2]
        worst = max(rows, key=lambda r: abs(r["pct"]))
        print(f"\n decks scored: {n}  skipped: {len(skipped)}", file=sys.stderr)
        print(f" residual  mean {mean:.3f}  median {med:.3f}  "
              f"range [{min(resids):.2f}, {max(resids):.2f}]", file=sys.stderr)
        print(f" residual% mean {meanp:.4f}%  median {medp:.4f}%  "
              f"worst deck idx {worst['idx']} {worst['pct']:.4f}% "
              f"(server {worst['server']}, our {worst['our']:.2f})", file=sys.stderr)
        exact = sum(1 for r in resids if abs(r) < 0.5)
        print(f" exact (|resid|<0.5): {exact}/{n}", file=sys.stderr)
    for idx, why in skipped:
        print(f" SKIP deck {idx}: {why}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
