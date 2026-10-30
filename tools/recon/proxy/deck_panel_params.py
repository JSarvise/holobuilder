"""Build `hbuilder.domain.deck_panel.PanelCard`s from the game's own master data.

The domain model (`src/hbuilder/domain/deck_panel.py`) takes plain numbers and
does no I/O on purpose. This is the recon-side adapter that resolves those
numbers for a REAL card: master tables via `master_cache` (offline, from the
game's own local cache), the override condition from `data/cards.json`, and the
bloom level from `state/collection.json`.

Read-only. Nothing here writes `data/cards.json`, `data/owned.json` or
`state/collection.json`.

Two entry points:

  `panel_cards(row)`   — a captured `bonus_extract_db.jsonl` row -> PanelCards.
                         Effect-ups and board buffs come from the row's own
                         `per_card` block (the server reports what it applied),
                         split into outfit / passive / board so the panel can
                         count its non-zero lines.
  `active_params(card_id, members)` — the override-resolved active-skill
                         parameters for one card in one deck, with no deck
                         context beyond the member list.

Accuracy of the resulting panel over 6017 song-less DB rows:
Active 6017/6017 exact, Special 3776/3778 on board-free rows, non-special TOTAL
5831/6017 (96.9 %, every residual +/-1), Unit Score 5828/6017.
"""

from __future__ import annotations

import json
import os
import re
import sys
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
DUMPS = HERE / "dumps"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "src"))

import master_cache as mc  # noqa: E402

from hbuilder.domain.deck_panel import PanelCard  # noqa: E402

_ATTRIBUTE = {"attribute_1": "Cute", "attribute_2": "Pure", "attribute_3": "Happy"}

#: Fallback only. `leader_board_effect_up()` DERIVES the same quantity from the
#: account's own board so it cannot go stale; this is kept because the derivation
#: needs `dumps/board.json`, which a caller may not have built.
#:
#: A frozen snapshot of one character's six unconditional leader-scope
#: score-support nodes (0.037+0.040+0.074+0.040+0.030+0.030 = 251 permil). A
#: snapshot goes incomplete as soon as another leader-scope node is unlocked: an
#: unknown leader-scope effect-up is charged to the PASSIVE line by `panel_cards`,
#: which changes how many panel lines are non-zero and hence the `N - 1` term.
LEADER_BOARD_EFFECT_UP = {"chr-04001": 251}


@lru_cache(maxsize=1)
def _derived_leader_board_effect_up():
    """`chr id -> leader-scope board effect-up permil`, derived from the live board.

    Every score-support effect-up in the game is a leader-scope RED node: the BLUE
    branch grants only activation rate/frequency and stats, and the GREEN branch only
    stats. So summing each character's UNCONDITIONAL leader-scope `skill_effect_up`
    terms reproduces exactly what a hardcoded table would freeze — checked against a
    paired before/after capture of a single node unlock.

    The singer-conditional terms are deliberately NOT included: `BoardModel` keeps them
    in `_conditional_skill_effect_up_terms`, and they contribute nothing to a song-less
    panel. Returns `None` if the board artifacts are absent, so the caller can fall back.
    """
    try:
        from hbuilder.domain.board import load_board
    except Exception:
        return None
    board = load_board(str(DUMPS / "board.json"))
    if board is None:
        return None
    try:
        export = json.loads((DUMPS / "board_export.json").read_text(encoding="utf-8"))
    except OSError:
        return None
    by_name = {c.get("name"): c.get("chr") for c in export.get("characters", ())}
    out = {}
    for holomem, effect in getattr(board, "_skill_effect_up_terms", ()):
        if getattr(effect, "scope", None) != "leader" or getattr(effect, "conditional", False):
            continue
        chr_id = by_name.get(holomem)
        if chr_id:
            out[chr_id] = out.get(chr_id, 0.0) + effect.value * 1000.0
    return {k: int(round(v)) for k, v in out.items()}


#: The board is TIME-VARYING and `dumps/board.json` is a snapshot of NOW, so the derived
#: table may not be the board a given row was captured under. Applying the current board
#: to older rows invents a board line: in one case it dropped the line-count agreement
#: from 98.53% to 72.08% by giving ~7,900 older rows a board line that did not exist when
#: they were captured.
#:
#: So revisions are kept with the instant they took effect, and a row is resolved by its
#: own `captured_at`. Each boundary sits between the last capture before a board change
#: and the first capture (or board refresh) after it. Rows without a `captured_at` (the
#: whole extract DB) predate every revision here and take the first.
LEADER_BOARD_EFFECT_UP_REVISIONS = (
    ("", {"chr-04001": 251}),
    ("2026-08-30T23:20:00", {"chr-04001": 251, "chr-04016": 20}),
    # Ina's `R-032` unlocked: a SECOND family with a small quota denominator.
    # After the unlock Ina derives exactly 20.
    ("2026-09-08T02:40:00",
     {"chr-04001": 251, "chr-04016": 20, "chr-04003": 20}),
    # Ina's `R-049` ("Score Support Effect 4.0% to all") bought between two capture
    # batches. Board refresh: nodes 479 -> 481, Ina derives exactly 60.
    ("2026-09-09T03:00:00",
     {"chr-04001": 251, "chr-04016": 20, "chr-04003": 60}),
    # Ina's `R-033`+`R-034` AND `R-050`+`R-051` bought: a THIRD board state that
    # preserves the exact per-card Outfit quotas. Two +40 score-support nodes, so
    # 60 -> 140 and E 660 -> 740.
    ("2026-09-09T22:00:00",
     {"chr-04001": 251, "chr-04016": 20, "chr-04003": 140}),
    # Several boards RESPEC'd -- nodes are refundable, so a character can LOSE
    # leader-scope effect-up. Read from the account: chr-04001, chr-04003 and chr-00034
    # lost nodes and the points went to chr-00036 and others. Calliope's 251 and Ina's 140
    # are GONE and Takane Lui (chr-00036) is the score-support leader at 221 permil
    # (5 unconditional leader-scope terms: .037+.040+.074+.040+.030).
    ("2026-09-13T03:00:00",
     {"chr-00036": 221, "chr-04016": 20}),
    # The `random_teams` corpus (847 rows, all captured at 02:28 that day) ran on the
    # board frozen beside it, which derives Lui 260 + Fuwawa 20. No rows lie between the
    # previous revision and this capture, so the boundary only has to precede the capture
    # (the placement is inferred, the values are read from the board).
    ("2026-10-13T00:00:00",
     {"chr-00036": 260, "chr-04016": 20}),
    # After that capture the boards were re-spec'd for an event: Ayunda Risu (chr-03001)
    # gained 260 of leader-scope effect-up and Lui fell 260 -> 200 (derived from the live
    # board). Without this boundary the previous corpus is scored on the newer board and
    # `test_deck_panel`'s oracle falls 96.7 % -> 95.6 % (14 Risu-led rows off by +64..+128).
    ("2026-10-13T03:00:00",
     {"chr-00036": 200, "chr-03001": 260, "chr-04016": 20}),
)


def leader_board_effect_up(leader, captured_at=None):
    """The leader's board effect-up permil AS OF `captured_at`.

    Derived from the live board for the current revision (so a new node cannot be missed
    silently) and from the recorded revisions for anything older.
    """
    table = LEADER_BOARD_EFFECT_UP_REVISIONS[0][1]
    for effective, rev in LEADER_BOARD_EFFECT_UP_REVISIONS:
        if not effective or (captured_at and captured_at >= effective):
            table = rev
    if table is LEADER_BOARD_EFFECT_UP_REVISIONS[-1][1]:
        # the newest revision must agree with what the account's own board derives; if the
        # player unlocks another node, this is where it surfaces instead of going unnoticed
        derived = _derived_leader_board_effect_up()
        if derived is not None:
            return derived.get(leader, 0)
    return table.get(leader, 0)


def _text(data, key):
    value = data.get(key, [None])[0]
    return value.decode() if isinstance(value, bytes) else value


def _trailing_int(text):
    m = re.search(r"(\d+)$", str(text))
    return int(m.group(1)) if m else None


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


CARDS = {c["id"]: c for c in _load_json(REPO / "data" / "cards.json")["cards"]}
#: The account the DB rows were captured on: `bonus_extract_db.collection.json` is the
#: collection as it was at capture time (bonus_extract.py writes it with the DB). Rows carry
#: no card state of their own, so scoring them on today's levels mixes two instants -- the
#: 2026-10-13 corpus matched 847/847 Active lines on its own collection, 688/847 on a later one.
_CAPTURE_COLLECTION = DUMPS / "bonus_extract_db.collection.json"
COLLECTION = _load_json(_CAPTURE_COLLECTION if _CAPTURE_COLLECTION.exists()
                        else REPO / "state" / "collection.json")["cards"]


def _load_active():
    out = {}
    for row in mc.rows("LiveActiveSkillLevel"):
        m = re.match(r"live_active_skill-(card-.+)$", row["live_active_skill_id"])
        if not m:
            continue
        d = row["data"]
        override = _text(d, 6)
        out[(m.group(1), str(row["level"]))] = dict(
            magnitude_permil=_trailing_int(_text(d, 4)) or 0,
            override_permil=_trailing_int(override) if override else None,
            cooldown_s=round(d.get(7, [0])[0] / 1000),
            probability_permil=d.get(8, [0])[0],
            duration_s=round(d.get(10, [0])[0] / 1000),
        )
    return out


def _load_special():
    out = {}
    for row in mc.rows("LiveSpecialSkillLevel"):
        m = re.match(r"live_special_skill-(card-.+)$", row["live_special_skill_id"])
        if not m:
            continue
        d = row["data"]
        rider = _text(d, 5) or ""
        out[(m.group(1), str(row["level"]))] = dict(
            magnitude_permil=_trailing_int(_text(d, 3)) or 0,
            duration_s=round(d.get(6, [0])[0] / 1000),
            rider_permil=(
                _trailing_int(rider) or 0
                if "activation_probability" in rider
                else 0
            ),
        )
    return out


def _load_leader():
    """Costume id -> the leader skill's active-skill effect-up, with its gate."""
    skills = {}
    for row in mc.rows("LiveLeaderSkill"):
        m = re.match(r"live_leader_skill-(card-.+)$", row["id"])
        if not m:
            continue
        d = row["data"]
        effects = []
        for effect_key, trigger_key in ((3, 2), (5, 4)):
            raw = _text(d, effect_key)
            if not raw:
                continue
            mm = re.match(
                r"live_leader_skill_effect-(\w+?)-(\d+)-live_skill_effect_target-(.+)$",
                raw,
            )
            if mm:
                effects.append(
                    dict(kind=mm.group(1), permil=int(mm.group(2)), trigger=_text(d, trigger_key))
                )
        skills[m.group(1)] = effects
    by_costume = {}
    for row in mc.rows("Costume"):
        leader = _text(row["data"], 4)
        if leader:
            by_costume[row["id"]] = skills.get(
                leader.replace("live_leader_skill-", ""), []
            )
    return by_costume


ACTIVE = _load_active()
SPECIAL = _load_special()
LEADER_BY_COSTUME = _load_leader()


def _generations(card_id):
    g = CARDS[card_id].get("generation")
    if isinstance(g, list):
        return tuple(g)
    return (g,) if g else ()


def skill_levels(card_id, *, bloom=None):
    """(active level, special level). Active 2 iff bloom >= 1, special 2 iff
    bloom >= 3 (bloom >= 1 for the special does not fit the server's figures)."""
    if bloom is None:
        bloom = COLLECTION[card_id]["bloom"]
    return ("2" if bloom >= 1 else "1", "2" if bloom >= 3 else "1")


def active_params(card_id, members, *, bloom=None):
    """Override-resolved active-skill parameters for `card_id` in this deck.

    A run-state trigger (life/combo) takes the override outright; a
    deck-composition trigger (type/generation) takes it iff the count of
    matching members INCLUDING the card itself meets the trigger count.
    """
    level, _ = skill_levels(card_id, bloom=bloom)
    rec = dict(ACTIVE[(card_id, level)])
    override = CARDS[card_id]["skills"].get("ACTIVE", {}).get("override")
    if override and rec["override_permil"] is not None:
        condition = override["condition"]
        kind = condition.get("kind")
        if kind == "run_state":
            rec["magnitude_permil"] = rec["override_permil"]
        elif kind in ("type", "generation"):
            if kind == "type":
                n = sum(1 for m in members if CARDS[m].get("type") == condition["target"])
            else:
                n = sum(1 for m in members if condition["target"] in _generations(m))
            if n >= condition["count"]:
                rec["magnitude_permil"] = rec["override_permil"]
    return rec


def special_params(card_id, members=None, *, bloom=None):
    """The special's parameters for `card_id`, with its rider gated by the deck.

    A special's activation-rate rider can carry its OWN trigger (master field 4, e.g.
    `deck_card_character_grouping-grp-holox-2`): the rider lands only when the deck
    meets it. Several 5-star cards gate theirs on 2+ members of a generation. On
    card-00036-5-uniq-0078-00 (2+ holoX), paying the rider unconditionally overstates
    the SPECIAL line by 7 to 10 permil on 9 of its 13 board-free decks, and those 9
    are every SPECIAL miss in 486 rows.

    The gate is read from data/cards.json's translated rider condition, counted over
    the five members INCLUDING the card itself -- the same convention `active_params`
    uses for an override. A run-state gate (life/combo) keeps the rider.
    `members=None` skips the gate (no deck to count over).
    """
    _, level = skill_levels(card_id, bloom=bloom)
    rec = SPECIAL[(card_id, level)]
    if members is None or not rec["rider_permil"]:
        return rec
    rider = CARDS[card_id]["skills"].get("SPECIAL", {}).get("rider") or {}
    condition = rider.get("condition") or {}
    kind = condition.get("kind")
    if kind == "type":
        n = sum(1 for m in members if CARDS[m].get("type") == condition["target"])
    elif kind == "generation":
        n = sum(1 for m in members if condition["target"] in _generations(m))
    else:
        return rec
    if n >= condition["count"]:
        return rec
    return dict(rec, rider_permil=0)


def leader_effect_up(costume_id, members):
    """The leader costume's active-skill effect-up, 0 when its gate is unmet.

    Gated costumes carry `deck_card_attribute-attribute_N-K`: the effect-up
    lands only when at least K of the five members hold that type.
    """
    permil = 0
    for effect in LEADER_BY_COSTUME.get(costume_id, []):
        if effect["kind"] != "live_active_skill_effect_up_permil_up":
            continue
        trigger = effect["trigger"]
        if not trigger:
            permil = effect["permil"]
            continue
        m = re.match(
            r"live_skill_trigger-deck_card_attribute-(attribute_\d)-(\d)", trigger
        )
        if m is None:
            continue
        n = sum(1 for c in members if CARDS[c].get("type") == _ATTRIBUTE[m.group(1)])
        if n >= int(m.group(2)):
            permil = effect["permil"]
    return permil


def panel_cards(row):
    """`bonus_extract_db.jsonl` row -> the deck's `PanelCard`s.

    The row's `per_card.active_effect_up` is the TOTAL effect-up the server
    applied to that card; the leader costume's share and the leader-scope board
    node's share are derived here and the remainder is the passive share. That
    split only decides how many panel lines are non-zero (the `N - 1` term) and
    how the pool is attributed — the magnitude boost uses the total either way.

    Skill levels come from the row's OWN `card_state` bloom when it has one, and
    only fall back to the current `collection.json` when it does not. Deriving a
    level for an older capture from a newer bloom is a provenance error, and it
    is measurable: using the row's own bloom takes the SPECIAL line from 99.449%
    to 99.950% on the 22,153 rows that carry `card_state`, with zero
    regressions, collapsing the whole +1..+5 residual tail. Rows without
    `card_state` — the entire extract DB and 13,007 older recapture rows — keep
    a same-shaped but longer +1..+9 tail and CANNOT be repaired from anything on
    disk.
    """
    members = row["members"]
    outfit = leader_effect_up(row["costume"], members)
    board_effect = leader_board_effect_up(row["leader"], row.get("captured_at"))
    card_state = row.get("card_state") or {}
    out = []
    for card_id, per_card in zip(members, row["per_card"]):
        bloom = (card_state.get(card_id) or {}).get("bloom")
        act = active_params(card_id, members, bloom=bloom)
        spec = special_params(card_id, members, bloom=bloom)
        # `round`, not `int`: these arrive as fixed32 floats off the wire. Truncation turns a
        # 739.9999 into 739 and breaks a whole panel line silently. Every word seen so far
        # is an exact integer permil, so this changes no current row -- it is a guard.
        total_effect = round(per_card.get("active_effect_up") or 0)
        out.append(
            PanelCard(
                magnitude_permil=act["magnitude_permil"],
                probability_permil=act["probability_permil"],
                cooldown_s=act["cooldown_s"],
                duration_s=act["duration_s"],
                outfit_effect_up_permil=outfit,
                passive_effect_up_permil=max(0, total_effect - outfit - board_effect),
                board_effect_up_permil=board_effect,
                board_probability_up_permil=round(per_card.get("active_prob_up") or 0),
                board_cooldown_shorten_permil=round(
                    per_card.get("active_cool_shorten") or 0
                ),
                special_magnitude_permil=spec["magnitude_permil"],
                special_duration_s=spec["duration_s"],
                special_rate_rider_permil=spec["rider_permil"],
            )
        )
    return out


def iter_db_rows(path=None, *, include_songs=False):
    """Yield `(row, panel_cards)` for every usable capture in the panel DB."""
    path = Path(path or DUMPS / "bonus_extract_db.jsonl")
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            row = json.loads(raw)
            if row.get("music_id") and not include_songs:
                continue
            try:
                yield row, panel_cards(row)
            except KeyError:
                continue


if __name__ == "__main__":
    os.chdir(REPO)
    n = sum(1 for _ in iter_db_rows())
    print(f"{n} usable rows in {DUMPS / 'bonus_extract_db.jsonl'}")
