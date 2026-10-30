"""Export the level and bloom CEILINGS a card can reach -> dumps/card_caps.json.

Written to answer "what does `max` mean for this card?", which is the only thing the explorer
needs in order to render a card at full state without inventing a number.

Two master tables carry it, and between them they reproduce the `bloom_mult` values already
sitting in `cards_enriched.json`, which is the cross-check that they are being read right:

  CardLevelLimit  keyed `rarity_<R>-attribute_<A>` x limit_break_count 0..4, field 3 = the LEVEL
                  CAP at that many limit breaks.  3*: 20/30/40/50/60   4*: 30/40/50/60/70
                  5*: 40/50/60/70/80 -- so the cap at LB4 is 60/70/80, exactly the three values
                  `stats_max.at.level` takes across all 167 cards.

  CardPotential   keyed `card_potential_grp-rarity_<R>` x upgrade_count 1..5, field 3 = an
                  effect type and field 4 its value. Effect type 4 is the stat multiplier in
                  PERMIL, and summing it reproduces `stats_max.at.bloom_mult`:
                      3* -> +50 +100 = +150 permil = 1.15
                      4* -> +100            = 1.10
                      5* -> +100            = 1.10
                  The other effect types (1, 2, 3, 5) also cost an upgrade step but do not move
                  the stat multiplier; they are carried through as `effects` rather than
                  guessed at, because nothing here needs to know what they are.

"Bloom" is the UI's word for what the data calls potential: the thumbnail node is
`CardPotentialUpgradeCountIcon` and it draws `ui_999_img_flower`.

    proxyenv/Scripts/python card_caps.py
"""
import json, os, sys, collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from master_cache import rows  # noqa: E402

OUT = os.path.join(HERE, "dumps", "card_caps.json")
POTENTIAL_STAT_EFFECT = 4      # the effect type whose value is the stat multiplier, in permil


def _one(rec, field, default=None):
    """Master fields are lists even when they hold a single value.

    `decode_record` keys by INTEGER field number. Only the JSON dump on the CLI makes them look
    like strings, so `data["3"]` silently misses and every cap comes out None.
    """
    vals = rec.get(field)
    return vals[0] if vals else default


def level_caps():
    """{'rarity_3-attribute_1': [20, 30, 40, 50, 60], ...} indexed by limit_break_count."""
    by_group = collections.defaultdict(dict)
    for r in rows("CardLevelLimit"):
        grp = r["group_id"]
        lb = int(r["limit_break_count"])
        cap = _one(r["data"], 3)
        if cap is None:
            continue
        by_group[grp][lb] = int(cap)
    out = {}
    for grp, caps in by_group.items():
        out[grp] = [caps[lb] for lb in sorted(caps)]
    return out


def potential():
    """{'3': {'max': 5, 'mult_permil': [0, 0, 50, ...], 'effects': [...]}, ...} by rarity."""
    by_rarity = collections.defaultdict(dict)
    for r in rows("CardPotential"):
        grp = r["group_id"]                       # card_potential_grp-rarity_5
        rarity = grp.rsplit("_", 1)[-1]
        n = int(r["upgrade_count"])
        by_rarity[rarity][n] = (int(_one(r["data"], 3, 0)), int(_one(r["data"], 4, 0)))
    out = {}
    for rarity, steps in by_rarity.items():
        order = sorted(steps)
        # index 0 = "no bloom", so the running multiplier starts at 0 added permil
        running, mult = 0, [0]
        effects = []
        for n in order:
            etype, val = steps[n]
            if etype == POTENTIAL_STAT_EFFECT:
                running += val
            mult.append(running)
            effects.append({"upgrade_count": n, "effect_type": etype, "value": val})
        out[rarity] = {"max": max(order), "mult_permil": mult, "effects": effects}
    return out


def main():
    caps = {
        "_source": "master CardLevelLimit + CardPotential, via master_cache.py (no network)",
        "level_cap_by_limit_break": level_caps(),
        "potential_by_rarity": potential(),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(caps, fh, ensure_ascii=False, indent=1, sort_keys=True)

    lv, pot = caps["level_cap_by_limit_break"], caps["potential_by_rarity"]
    print(f"{OUT}")
    print(f"  {len(lv)} grupos de limite de nivel, {len(pot)} rarezas con potencial")
    for rarity in sorted(pot):
        grp = f"rarity_{rarity}-attribute_1"
        p = pot[rarity]
        print(f"  {rarity}*  nivel {lv.get(grp)}  bloom max {p['max']}"
              f"  mult x{1 + p['mult_permil'][-1] / 1000:.2f}")


if __name__ == "__main__":
    main()
