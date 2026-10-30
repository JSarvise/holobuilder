# Export the PER-LEVEL CardLevel stat multiplier curve, and each card's level/potential group.
#
# Why this exists: `build_cards_dataset.py` reads the same table but keeps only each group's
# value at its MAXIMUM level (`_cardlevel_maxmult`), because maxed stats were all it needed.
# `src/hbuilder/domain/collection.py` computes
#
#     stat_i = ceil(ratio_i * (level_multiplier / 1000) * bloom_multiplier)
#
# in IEEE-754 binary32 at every step, as `CardStatus::.ctor` does
# (`Vision.Common.Core`, RVA 0x52acff0): the whole chain is `cvtsi2ss`/`divss`/`mulss`/`addss`
# and the single `cvtps2pd` only widens the result for `Math.Ceiling(double)`. See that
# module's docstring for the disassembly and for the 12 (card, level, bloom, stat) points
# where a binary64 evaluation gives a different integer.
#
# at an ARBITRARY level, so it needs the whole curve. Without it a card's level cannot move a
# score, and a collection editor's level control would be decorative.
#
# Reads the master SQLCipher DBs directly via `master_cache.open_table` + `master_db
# .decode_record` -- the same path `build_cards_dataset.py` uses -- NOT `master_cache.rows()`,
# which corrupts any byte >= 0x80 in a binary field.
#
# No network. Writes tools/recon/proxy/dumps/card_level_curve.json and nothing else; it never
# touches data/cards.json or data/owned.json.
#
# Usage: python tools/recon/proxy/card_level_curve.py
import sys, os, json, math, struct


def _f32(value):
    """Round to IEEE-754 binary32, the way an `ss` SSE instruction does."""
    return struct.unpack("<f", struct.pack("<f", value))[0]

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from master_db import decode_record
from master_cache import open_table

DUMPS = os.path.join(HERE, "dumps")
OUT = os.path.join(DUMPS, "card_level_curve.json")

# Field numbers, read off `build_cards_dataset.py`'s own decode of the same tables.
CARDLEVEL_MULT = 3       # CardLevel field 3 -- the stat multiplier at that level
CARDLEVEL_POWERUP = 5    # CardLevel field 5 -- live_deck_power_permyriad_up, the Member
                         # Power-Up ladder. Purely (rarity, level); 27 groups collapse to
                         # 3 ladders whose per-card maxima are 4 / 5 / 8 permyriad.
CARD_LEVEL_GROUP = 6     # Card field 6  -- which CardLevel group this card follows
CARD_POTENTIAL_GROUP = 14  # Card field 14 -- which CardPotential group
CARD_RATIO = (7, 8, 9)   # Card fields 7/8/9 -- performance/technique/sense ratio, permil


def _s(fields, key):
    value = fields.get(key, [None])[0]
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def level_curve(idx):
    """(group_id -> {level: multiplier}, group_id -> {level: powerup_permyriad})."""
    db = open_table("CardLevel", idx)
    curve, powerup = {}, {}
    for row in db.query("SELECT group_id,level,data FROM CardLevel"):
        gid = row["group_id"].decode()
        level = int(row["level"])
        fields = decode_record(row["data"])
        mult = fields.get(CARDLEVEL_MULT, [None])[0]
        if mult is None:
            raise SystemExit(f"CardLevel {gid} level {level} has no field {CARDLEVEL_MULT}")
        curve.setdefault(gid, {})[level] = int(mult)
        powerup.setdefault(gid, {})[level] = int(fields.get(CARDLEVEL_POWERUP, [0])[0])
    db.close()
    return curve, powerup


def card_groups(idx):
    """card id -> {level_group, potential_group, ratio_permil}."""
    db = open_table("Card", idx)
    out = {}
    for row in db.query("SELECT id,data FROM Card"):
        cid = row["id"].decode()
        fields = decode_record(row["data"])
        out[cid] = {
            "level_group": _s(fields, CARD_LEVEL_GROUP),
            "potential_group": _s(fields, CARD_POTENTIAL_GROUP),
            "ratio_permil": [fields.get(k, [None])[0] for k in CARD_RATIO],
        }
    db.close()
    return out


def bloom_permil(idx):
    """potential group_id -> (cumulative permil by bloom 0..max_bloom, max_bloom).

    `CardPotential` rows carry an `upgrade_count` (the bloom step) and a permil `value` on
    effect_type 4. The cumulative array is the running sum by step, which is what reproduces
    `card_caps.json`'s `mult_permil` and the `stats_max.at.bloom_mult` values on disk.

    **The array MUST run to the group's max bloom, not to its last stat step**, and the two are
    different for two of the three rarities. In the real table rarity 3 carries
    effect_type 4 at upgrade_count 2 AND 5, but rarities 4 and 5 carry it only at 2 -- their
    blooms 3, 4 and 5 grant other effect types and no stat permil at all. Truncating at the last
    stat step would give rarity 4/5 an array of length 3, and a 5* at bloom 5 would then index
    off the end and read as "unknown bloom multiplier" rather than as 1.10.
    So `max_bloom` is taken over EVERY effect type and the stat array is padded by repeating its
    last value -- which is what the game does: a bloom that grants no stat effect leaves the
    stat multiplier where it was.
    """
    db = open_table("CardPotential", idx)
    steps = {}
    top_step = {}
    for row in db.query("SELECT group_id,effect_type,data FROM CardPotential"):
        gid = row["group_id"].decode()
        fields = decode_record(row["data"])
        count = int(fields.get(2, [0])[0])
        top_step[gid] = max(top_step.get(gid, 0), count)
        if fields.get(3, [None])[0] != 4:  # effect_type 4 == all-parameter permil
            continue
        steps.setdefault(gid, {})[count] = int(fields.get(4, [0])[0])
    db.close()

    out = {}
    for gid, max_bloom in top_step.items():
        by_step = steps.get(gid, {})
        running = 0
        array = []
        for step in range(max_bloom + 1):
            running += by_step.get(step, 0)
            array.append(running)
        out[gid] = {"mult_permil": array, "max_bloom": max_bloom}
    return out


def verify(curve, groups, blooms, enriched_path):
    """Reproduce every card's `stats_max` from the exported curve, EXACTLY.

    This is the check the curve exists to make possible. `stats_max` was computed independently
    by `build_cards_dataset.py` straight from the master tables; this recomputes it from the
    exported JSON through the same formula `domain/collection.py` uses. Two derivations, one
    comparison -- an off-by-one in the level index, a summed-instead-of-indexed bloom array, or
    a transposed stat triple all fail here on real cards instead of passing quietly.
    """
    if not os.path.exists(enriched_path):
        return None
    rows = json.load(open(enriched_path, encoding="utf-8"))
    if isinstance(rows, dict):
        rows = rows.get("cards", list(rows.values()))

    checked = 0
    bad = []
    for row in rows:
        target = row.get("stats_max")
        cid = row.get("id")
        if not target or cid not in groups:
            continue
        meta = groups[cid]
        lg, pg = meta["level_group"], meta["potential_group"]
        ratio = meta["ratio_permil"]
        if lg not in curve or pg not in blooms or any(r is None for r in ratio):
            continue

        at_level = target["at"]["level"]
        mult = curve[lg].get(at_level)
        if mult is None:
            bad.append((cid, f"no level {at_level} in {lg}"))
            continue
        bloom_mult = _f32(_f32(1.0) + _f32(_f32(blooms[pg]["mult_permil"][-1]) / _f32(1000.0)))

        per_mille = _f32(_f32(mult) / _f32(1000.0))
        # TWO ceilings, byte-for-byte `build_cards_dataset._max_stats`: the
        # level-scaled stat becomes an INTEGER before the bloom multiplier applies.
        # A one-ceiling form shows up as a phantom ±1 "drift" on boundary cards.
        got = tuple(
            math.ceil(_f32(_f32(math.ceil(_f32(_f32(r) * per_mille))) * bloom_mult))
            for r in ratio
        )
        want = (target["performance"], target["technique"], target["sense"])
        checked += 1
        if got != want:
            bad.append((cid, got, want))

    return checked, bad


def main():
    idx = json.load(open(os.path.join(DUMPS, "master_index.json"), encoding="utf-8"))

    curve, powerup = level_curve(idx)
    groups = card_groups(idx)
    blooms = bloom_permil(idx)

    levels = sum(len(v) for v in curve.values())
    print(f"CardLevel: {len(curve)} groups, {levels} (group, level) rows")
    for gid in sorted(curve)[:3]:
        ls = sorted(curve[gid])
        print(f"  {gid}: levels {ls[0]}..{ls[-1]}, mult {curve[gid][ls[0]]}..{curve[gid][ls[-1]]}")
    print(f"Card: {len(groups)} cards")
    print(f"CardPotential: {len(blooms)} groups")
    for gid in sorted(blooms):
        print(f"  {gid}: {blooms[gid]}")

    # Cross-check against card_caps.json, built independently by card_caps.py from the same
    # two tables. Disagreement means one of the two is wrong and both are in use.
    caps_path = os.path.join(DUMPS, "card_caps.json")
    if os.path.exists(caps_path):
        caps = json.load(open(caps_path, encoding="utf-8"))
        pot = caps.get("potential_by_rarity", {})
        for gid, entry in sorted(blooms.items()):
            rarity = gid.rsplit("rarity_", 1)[-1]
            other = pot.get(rarity)
            if other is None:
                print(f"  CROSSCHECK {gid}: absent from card_caps.json")
                continue
            same_arr = list(other.get("mult_permil") or []) == entry["mult_permil"]
            same_max = other.get("max") == entry["max_bloom"]
            print(f"  CROSSCHECK rarity {rarity}: mult_permil {'OK' if same_arr else 'DIFFERS ' + str(other.get('mult_permil'))}"
                  f" | max_bloom {'OK' if same_max else 'DIFFERS ' + str(other.get('max'))}")

    result = verify(curve, groups, blooms, os.path.join(DUMPS, "cards_enriched.json"))
    if result is None:
        print("VERIFY: skipped -- dumps/cards_enriched.json absent")
    else:
        checked, bad = result
        print(f"VERIFY: {checked - len(bad)}/{checked} cards reproduce stats_max exactly")
        for entry in bad[:10]:
            print(f"  MISMATCH {entry}")
        if bad:
            raise SystemExit(f"{len(bad)} cards do not reproduce stats_max -- not writing")

    doc = {
        "_source": "master CardLevel + Card + CardPotential, via master_cache.py (no network)",
        "_formula": "stat_i = ceil(ratio_i * (level_mult[group][level]/1000) * (1 + bloom_permil[group][bloom]/1000)), evaluated in IEEE-754 binary32 at every step and in this operand order -- ATTESTED from CardStatus::.ctor, Vision.Common.Core RVA 0x52acff0",
        "level_mult": {gid: {str(k): v for k, v in sorted(lv.items())} for gid, lv in sorted(curve.items())},
        "power_up_permyriad": {
            gid: {str(k): v for k, v in sorted(lv.items())} for gid, lv in sorted(powerup.items())
        },
        "bloom_permil_by_group": {k: v for k, v in sorted(blooms.items())},
        "cards": {k: groups[k] for k in sorted(groups)},
    }
    os.makedirs(DUMPS, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
