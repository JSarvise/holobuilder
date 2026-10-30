"""Resolve each card's LEVEL and BLOOM from a hand-authored file, with a `max` shorthand.

Why this exists rather than a capture: no per-card record the proxy captures carries these two
numbers, and they cannot be inverted out of the stats. So they are entered by hand, which is also
the project's standing rule for authoritative data.

`max` is not a guess either: it resolves against the game's own ceilings, exported from master
by `tools/recon/proxy/card_caps.py` (CardLevelLimit + CardPotential).

The file is `card_state.json` next to this module:

    {
      "default": "max",                        // "max" | "unknown" | {"level":…, "bloom":…}
      "cards": {
        "card-00031-5-uniq-0025-00": {"level": 27, "bloom": 2},
        "card-00023-5-uniq-0019-00": "max",
        "card-00004-3-nrml-0005-00": "unknown"
      }
    }

Anything omitted falls through to `default`. A partial entry is fine -- `{"bloom": 3}` takes its
level from the default. `limit_break` may be given to cap the level the way the game does; it is
otherwise assumed to be whatever the stated level requires.

Nothing here writes; the file is edited by hand.
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "card_state.json")
CAPS = os.path.join(HERE, "..", "recon", "proxy", "dumps", "card_caps.json")

MAX = "max"
UNKNOWN = "unknown"

# `None` is a legitimate value for both `state` and `caps` -- it means "there is no caps file",
# which is exactly the degraded case that has to keep working. So "argument not supplied" needs
# a distinct sentinel, or passing None would silently load from disk instead.
_UNSET = object()


def load_caps(path=CAPS):
    """Game ceilings, or None when card_caps.py has not been run."""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return None
    return json.load(open(path, encoding="utf-8"))


def load_state(path=STATE):
    """The hand-authored file, or an all-unknown default when it is absent."""
    if not os.path.exists(path):
        return {"default": UNKNOWN, "cards": {}}
    data = json.load(open(path, encoding="utf-8"))
    data.setdefault("default", UNKNOWN)
    # JSON has no comments, so the file uses `//`-prefixed keys for them. Dropping them here
    # keeps a documented file from reporting its own notes as unknown card ids.
    data["cards"] = {k: v for k, v in (data.get("cards") or {}).items()
                     if not k.startswith("//")}
    return data


def _ceilings(caps, rank, attribute):
    """(level_cap_by_limit_break, max_bloom) for a card, or (None, None) without card_caps."""
    if not caps:
        return None, None
    grp = f"rarity_{rank}-attribute_{attribute}"
    levels = caps.get("level_cap_by_limit_break", {}).get(grp)
    pot = caps.get("potential_by_rarity", {}).get(str(rank)) or {}
    return levels, pot.get("max")


def _normalise(entry):
    """'max' / 'unknown' / {...} / None -> a dict, so callers deal with one shape."""
    if entry is None:
        return {}
    if isinstance(entry, str):
        key = entry.strip().lower()
        if key == MAX:
            return {"level": MAX, "bloom": MAX}
        return {"level": None, "bloom": None}
    if isinstance(entry, dict):
        return dict(entry)
    return {}


def resolve_one(entry, default, levels, max_bloom, warn, cid):
    """-> {'lv', 'bloom', 'lvmax', 'bloommax', 'src'} for a single card."""
    e = _normalise(entry)
    d = _normalise(default)
    for k in ("level", "bloom", "limit_break"):
        if k not in e and k in d:
            e[k] = d[k]

    lb = e.get("limit_break")
    if lb is not None and levels:
        try:
            lb = max(0, min(int(lb), len(levels) - 1))
        except (TypeError, ValueError):
            warn(f"{cid}: limit_break no numerico ({e['limit_break']!r}), ignorado")
            lb = None
    lv_cap = levels[lb if lb is not None else -1] if levels else None

    lv = e.get("level")
    if lv == MAX:
        lv = lv_cap
        if lv is None:
            warn(f"{cid}: 'max' de nivel sin card_caps.json -- ejecuta card_caps.py")
    elif lv is not None:
        try:
            lv = int(lv)
        except (TypeError, ValueError):
            warn(f"{cid}: level no numerico ({e['level']!r}), ignorado")
            lv = None
        else:
            if lv < 1:
                warn(f"{cid}: level {lv} < 1, subido a 1")
                lv = 1
            elif lv_cap is not None and lv > lv_cap:
                warn(f"{cid}: level {lv} supera el tope {lv_cap}"
                     + (f" con limit_break {lb}" if lb is not None else " a limit break maximo")
                     + ", recortado")
                lv = lv_cap

    bl = e.get("bloom")
    if bl == MAX:
        bl = max_bloom
        if bl is None:
            warn(f"{cid}: 'max' de bloom sin card_caps.json -- ejecuta card_caps.py")
    elif bl is not None:
        try:
            bl = int(bl)
        except (TypeError, ValueError):
            warn(f"{cid}: bloom no numerico ({e['bloom']!r}), ignorado")
            bl = None
        else:
            if bl < 0:
                warn(f"{cid}: bloom {bl} < 0, subido a 0")
                bl = 0
            elif max_bloom is not None and bl > max_bloom:
                warn(f"{cid}: bloom {bl} supera el maximo {max_bloom}, recortado")
                bl = max_bloom

    if lv is None and bl is None:
        src = UNKNOWN
    elif entry is None:
        src = "default"
    elif isinstance(entry, str) and entry.strip().lower() == MAX:
        src = MAX
    else:
        src = "manual"
    return {"lv": lv, "bloom": bl, "lvmax": lv_cap, "bloommax": max_bloom, "src": src}


def resolve(cards, state=_UNSET, caps=_UNSET):
    """[(id, rarity_rank, attribute)] -> ({id: state}, [warnings]).

    `cards` is a sequence of (id, rarity_rank, attribute) triples so this stays independent of
    whatever record shape the caller happens to hold.
    """
    state = load_state() if state is _UNSET else state
    caps = load_caps() if caps is _UNSET else caps
    warnings = []

    def warn(msg):
        warnings.append(msg)

    known = {cid for cid, _, _ in cards}
    for cid in state["cards"]:
        if cid not in known:
            warn(f"card_state.json nombra una carta que no existe en el dataset: {cid}")

    out = {}
    for cid, rank, attribute in cards:
        levels, max_bloom = _ceilings(caps, rank, attribute)
        if caps and levels is None:
            warn(f"{cid}: sin topes de nivel para rarity_{rank}-attribute_{attribute}")
        out[cid] = resolve_one(state["cards"].get(cid), state["default"],
                               levels, max_bloom, warn, cid)
    return out, warnings


def _selftest():
    """Exercise every branch of the resolver. Exits non-zero on regression.

    Same contract as octo_crypto.py's self-check: run the module and it verifies itself, so a
    refactor that quietly changes clamping or fallthrough is caught rather than shipped.
    """
    caps = {"level_cap_by_limit_break": {"rarity_5-attribute_1": [40, 50, 60, 70, 80],
                                         "rarity_3-attribute_1": [20, 30, 40, 50, 60]},
            "potential_by_rarity": {"5": {"max": 5}, "3": {"max": 5}}}
    C5, C3 = ("card-5", 5, 1), ("card-3", 3, 1)
    fails = []

    def check(label, got, want):
        if got != want:
            fails.append(f"{label}: esperado {want}, obtenido {got}")

    def run(default, cards_map, cards=(C5, C3)):
        st = {"default": default, "cards": cards_map}
        res, warns = resolve(list(cards), st, caps)
        return res, warns

    # `max` resolves to the game's ceiling, per rarity
    r, w = run(MAX, {})
    check("max 5*", (r["card-5"]["lv"], r["card-5"]["bloom"]), (80, 5))
    check("max 3*", (r["card-3"]["lv"], r["card-3"]["bloom"]), (60, 5))
    check("max sin avisos", w, [])

    # `unknown` draws nothing
    r, _ = run(UNKNOWN, {})
    check("unknown", (r["card-5"]["lv"], r["card-5"]["bloom"], r["card-5"]["src"]),
          (None, None, UNKNOWN))

    # an explicit entry beats the default; a PARTIAL one inherits the rest of it
    r, _ = run({"level": 10, "bloom": 1}, {"card-5": {"bloom": 4}})
    check("parcial hereda level", (r["card-5"]["lv"], r["card-5"]["bloom"]), (10, 4))
    check("default se aplica", (r["card-3"]["lv"], r["card-3"]["bloom"]), (10, 1))

    # out of range is clamped, and says so
    r, w = run(UNKNOWN, {"card-3": {"level": 999, "bloom": 9}})
    check("nivel recortado al tope", r["card-3"]["lv"], 60)
    check("bloom recortado al maximo", r["card-3"]["bloom"], 5)
    check("dos avisos de recorte", len([x for x in w if "recortado" in x]), 2)

    # limit_break lowers the ceiling the way the game does
    r, w = run(UNKNOWN, {"card-5": {"level": MAX, "limit_break": 0}})
    check("max con limit_break 0", r["card-5"]["lv"], 40)
    r, w = run(UNKNOWN, {"card-5": {"level": 75, "limit_break": 1}})
    check("nivel recortado por limit_break", r["card-5"]["lv"], 50)

    # a card id that is not in the dataset is reported rather than ignored
    _, w = run(MAX, {"card-noexiste": MAX})
    check("id desconocido avisado", any("no existe" in x for x in w), True)

    # no caps at all: `max` cannot be resolved, and says so instead of inventing a number
    res, w = resolve([C5], {"default": MAX, "cards": {}}, None)
    check("max sin caps -> None", (res["card-5"]["lv"], res["card-5"]["bloom"]), (None, None))
    check("max sin caps avisa", any("card_caps" in x for x in w), True)

    if fails:
        print("card_state self-test FALLA:")
        for f in fails:
            print("  -", f)
        return 1
    print("card_state self-test OK (10 comprobaciones)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
