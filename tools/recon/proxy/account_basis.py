"""ONE place to ask "what is the account's scoring state RIGHT NOW", and to tell
whether a stored capture still shares it.

WHY THIS EXISTS. A capture corpus spans several account states: a per-character
board buff map derived from the WHOLE corpus can list a character as granting
`probability_up = 150` after her board was re-planned and grants nothing.
`deck_panel_params.panel_cards` is safe because it reads each capture's own
`per_card` block, but anything derived across captures needs every measurement
and the current account to share one basis, which cannot hold while every tool
derives the account state its own way.

So: derive the scoring-relevant account state from the files `refresh_all.py`
regenerates, in one module, and give callers a fingerprint they can compare.

  `board_buffs()`      per character, the permil the board grants to the active
                       skill: probability-up, cooldown-shorten, effect-up. Parsed
                       from `dumps/board.json` (`basis: actual`, written by
                       `build_board.py` from the account's own drawn nodes).
  `fingerprint()`      a short hash over that state plus the collection's
                       level/bloom/limit-break, so two runs can be compared.
  `classify_rows()`    for a capture corpus, mark each row `current` or `stale` by
                       comparing the buffs the SERVER reported in its `per_card`
                       against the buffs the account grants now. A row that
                       disagrees was captured under a different board and must not
                       be pooled with fresh ones.

Read-only: it reads dumps and `state/collection.json` and writes nothing. It is a
recon-side tool; nothing under `src/hbuilder/` may import it.

Run it directly for a report:
    proxyenv/Scripts/python tools/recon/proxy/account_basis.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
DUMPS = HERE / "dumps"

#: the three board effects that reach the Active-skill lattice, and the field each
#: one lands in on a `per_card` block
EFFECT_FIELDS = {
    "live_active_skill_activation_probability_up_permil_up": "active_prob_up",
    "live_active_skill_cool_time_shorten_permil_up": "active_cool_shorten",
    "live_active_skill_effect_up_permil_up": "active_effect_up",
}
_EFFECT_RE = re.compile(r"skill_tree_effect-([a-z_]+)-(\d+)-")


def _board_path() -> Path:
    return DUMPS / "board.json"


def board_buffs() -> dict[str, dict[str, int]]:
    """Per character NAME -> {field: permil} for the three lattice-reaching effects.

    `sum(value * connect_multiplier)` per effect. That the multiplier applies is
    measured, not assumed: on the five characters whose board reaches the lattice
    it reproduces the server's own `per_card` exactly on both effects --
    Marine 300 raw -> 396, Luna 140 -> 236 and shorten 40 -> 40, Nene 300 -> 396,
    Lui 270 -> 366 and 40 -> 40, Ina'nis 300 -> 390 and 120 -> 120. The two sides
    are independent (drawn nodes vs the server's scoring), so this is a
    non-circular check. `validate()` re-runs it over the whole corpus.

    ⚠ **This total sums GATED and UNGATED nodes together.**
    Mori Calliope reports `active_effect_up = 491`, but the server puts **251** in
    `per_card` on every one of 382 measured Calliope card-slots and never 491 or
    240: her node `R-002` (100 raw x 2.4 connect = 240) sits behind a
    `skill_tree_effect_passive_trigger-music` condition. `classify_rows`
    deliberately never compares `active_effect_up`, and the two lattice buffs
    have no gated nodes in practice -- but do not use this number to predict
    what the server will apply without separating the gated nodes first.
    """
    data = json.loads(_board_path().read_text(encoding="utf-8"))
    out: dict[str, dict[str, int]] = {}
    for name, nodes in data.get("characters", {}).items():
        acc: dict[str, float] = {}
        for node in nodes:
            mult = float(node.get("connect_multiplier", 1.0) or 1.0)
            for effect in node.get("effects", []):
                m = _EFFECT_RE.match(effect)
                if not m:
                    continue
                field = EFFECT_FIELDS.get(m.group(1))
                if field:
                    acc[field] = acc.get(field, 0.0) + int(m.group(2)) * mult
        rounded = {k: int(round(v)) for k, v in acc.items() if round(v)}
        if rounded:
            out[name] = rounded
    return out


def name_by_chr() -> dict[str, str]:
    data = json.loads(_board_path().read_text(encoding="utf-8"))
    return data.get("name_by_chr", {})


def buffs_by_chr() -> dict[str, dict[str, int]]:
    """The same map keyed by `chr-XXXXX`, which is what callers actually hold."""
    by_name = board_buffs()
    return {c: by_name[n] for c, n in name_by_chr().items() if n in by_name}


def fingerprint() -> str:
    """A short hash of everything that can change a Score-Bonus measurement."""
    parts = [json.dumps(buffs_by_chr(), sort_keys=True)]
    coll = REPO / "state" / "collection.json"
    if coll.exists():
        data = json.loads(coll.read_text(encoding="utf-8"))
        cards = data.get("cards", {})
        if isinstance(cards, dict):
            items = sorted(
                (k, v.get("level"), v.get("bloom"), v.get("limit_break"))
                for k, v in cards.items() if isinstance(v, dict)
            )
            parts.append(json.dumps(items, sort_keys=True))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def row_buffs(row) -> dict[str, dict[str, int]]:
    """What the SERVER applied in this capture, keyed by `chr-XXXXX`."""
    out: dict[str, dict[str, int]] = {}
    for pc in row.get("per_card", []):
        chr_id = "chr-" + str(pc.get("card_id", "")).split("-")[1]
        got = {}
        for field in ("active_prob_up", "active_cool_shorten"):
            v = pc.get(field)
            if v:
                got[field] = int(v)
        out[chr_id] = got
    return out


def classify_rows(rows, current=None):
    """Split rows into (current, stale, unknown) against the account's board now.

    `active_effect_up` is deliberately NOT compared: it carries the leader
    costume's contribution too, so it cannot be attributed to the board from a
    capture alone. The two lattice buffs come from the board and nothing else.
    """
    if current is None:
        current = buffs_by_chr()
    fresh, stale, unknown = [], [], []
    for row in rows:
        if not row.get("per_card"):
            unknown.append(row)
            continue
        disagrees = False
        for chr_id, got in row_buffs(row).items():
            want = {k: v for k, v in current.get(chr_id, {}).items()
                    if k in ("active_prob_up", "active_cool_shorten") and v}
            if got != want:
                disagrees = True
                break
        (stale if disagrees else fresh).append(row)
    return fresh, stale, unknown


def validate(paths=None):
    """Check the derived buffs against the server's own answers, per capture.

    This is a non-circular acceptance test:
    `board.json` comes from the account's drawn nodes, `per_card` comes from the
    server's scoring, and they are independent.
    """
    paths = paths or [DUMPS / "bonus_recapture_db.jsonl", DUMPS / "bonus_extract_db.jsonl"]
    rows = []
    for p in paths:
        if p.exists():
            with p.open(encoding="utf-8") as fh:
                rows += [json.loads(line) for line in fh]
    fresh, stale, unknown = classify_rows(rows)
    return rows, fresh, stale, unknown


def main():
    buffs = buffs_by_chr()
    names = name_by_chr()
    print(f"account scoring basis  fingerprint {fingerprint()}")
    print(f"board file {_board_path()}  "
          f"mtime {__import__('time').strftime('%Y-%m-%dT%H:%M:%S', __import__('time').localtime(os.path.getmtime(_board_path())))}\n")
    lattice = {c: b for c, b in buffs.items()
               if b.get("active_prob_up") or b.get("active_cool_shorten")}
    print(f"characters whose board reaches the Active lattice: {len(lattice)}")
    for c, b in sorted(lattice.items()):
        print(f"   {c}  {names.get(c, ''):<20} prob-up {b.get('active_prob_up', 0):>4}"
              f"   shorten {b.get('active_cool_shorten', 0):>4}")

    rows, fresh, stale, unknown = validate()
    print(f"\ncapture corpus against that basis: {len(rows)} rows")
    print(f"   current {len(fresh)}   STALE {len(stale)}   no per_card {len(unknown)}")
    if stale:
        import collections
        tags = collections.Counter(r.get("tag") or "(untagged)" for r in stale)
        print("   stale rows by tag:")
        for t, n in tags.most_common(8):
            print(f"      {t:<32}{n}")


if __name__ == "__main__":
    main()
