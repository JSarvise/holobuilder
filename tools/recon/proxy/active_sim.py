# What IS the Active Skill line? A model bake-off against the server's own nine decks.
#
# THE PROBLEM. `SUM magnitude x duration/cooldown x probability` -- the naive long-run duty sum --
# does not reproduce the server's Active Skill figure, and the failure looks structural: three
# decks within 1.6 points of each other on duty return ratios of 0.73, 0.97 and 0.84.
#
# WHAT THIS MODULE DOES. Three things:
#
# 1. NINE DECKS, NOT FIVE, WITH NO NEW CAPTURE. `deck_oracle.json` holds ten evaluations of five
#    compositions. Running `deck_oracle.evaluations()` over ALL the `.mitm` files already in
#    `dumps/` yields 24 evaluations of NINE distinct compositions -- including three pairs that
#    differ by exactly one member, which no five-deck subset contains.
#
# 2. NO ASSUMED SKILL LEVELS. Bloom stages are recorded nowhere on disk, so pinning each card's
#    level from them is unreproducible and unfalsifiable. Here the level is never supplied: every
#    consistent assignment is enumerated (a card in four decks must be at the same level in all
#    four), and a model is judged by the BEST residual any assignment can give it. A model that
#    cannot fit under any assignment is refuted without knowing a single Bloom stage.
#
# 3. AN ORDER TEST THAT NO FREE CONSTANT CAN SURVIVE. Reference duration, saturation curve, global
#    calibration, level assignment -- all of them are monotone transforms of a model's own output,
#    and none can reorder two decks. `--order` asks only whether the model ranks the decks the way
#    the server does. It rules out the entire base-magnitude family in one line.
#
# WHICH QUANTITY IS BEING FITTED, AND WHICH IS NOT. `--fit` fits the deck screen's
# `by_live_active_skill` line: a deterministic server-side function of five cards, and NOT the
# in-run expected value. `--sim` computes the other quantity -- the in-run distribution on a real
# chart -- and fits nothing, so its output is a prediction. The two are never mixed inside one fit.
#
#   proxyenv/Scripts/python active_sim.py --decks    the nine decks, their members and their inputs
#   proxyenv/Scripts/python active_sim.py --order    which models are structurally refuted
#   proxyenv/Scripts/python active_sim.py --fit      the bake-off: every model, best residual
#   proxyenv/Scripts/python active_sim.py --best     the winner in detail, with its level assignment
#   proxyenv/Scripts/python active_sim.py --swap     the three single-member swaps
#   proxyenv/Scripts/python active_sim.py --s17      is a published per-deck duty column self-consistent?
#   proxyenv/Scripts/python active_sim.py --scan-t   the reference duration, scanned
#   proxyenv/Scripts/python active_sim.py --sim      timeline Monte Carlo against a measured 21 % spread
import argparse
import itertools
import json
import math
import os
import random
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

ORACLE = os.path.join(HERE, "dumps", "deck_oracle.json")
BOARD = os.path.join(HERE, "dumps", "board_state.json")
SKILLTREE = os.path.join(HERE, "dumps", "skilltree.json")
CHARTS = os.path.join(HERE, "dumps", "charts_cdn")

ACTIVE_KEY = "by_live_active_skill [Active Skill]"

# The reference duration that makes the SPECIAL support reading a constant across ten captured
# decks (125.8-133.7 s, 6.2% spread). Also `REFERENCE_RUN_SECONDS` in
# src/hbuilder/domain/scoring_config.py. Every finite-window model here defaults to it and
# `--fit` also scans T so the fit never depends on the value being exactly right.
T_REF = 128.8


# --------------------------------------------------------------------------- master data

def active_levels():
    """`card-00012-5` -> {level: {mag, dur, cool, prob, gate, gate_mag}}.

    Straight out of `LiveActiveSkillLevel`, no value carried over from any derived dump.
    Field 4 is the effect id whose numeric suffix is the magnitude in per-mille, 7 the cooldown
    in ms, 8 the activation probability in per-mille, 10 the duration in ms. Field 5 is the
    trigger and field 6 the doubled effect it selects."""
    import master_cache as mc
    out = {}
    for r in mc.rows("LiveActiveSkillLevel"):
        m = re.match(r"live_active_skill-(card-\d{5}-\d)", r["live_active_skill_id"])
        if not m:
            continue
        d = r["data"]
        eff = _text(d.get(4))
        mag = re.search(r"(\d+)$", eff or "")
        if not mag:
            continue
        gate_eff = _text(d.get(6))
        gate_mag = re.search(r"(\d+)$", gate_eff or "")
        out.setdefault(m.group(1), {})[int(r["level"])] = {
            "mag": int(mag.group(1)) / 10.0,          # percent
            "cool": d.get(7, [0])[0] / 1000.0,        # seconds
            "prob": d.get(8, [0])[0] / 1000.0,        # 0..1
            "dur": d.get(10, [0])[0] / 1000.0,        # seconds
            "gate": _text(d.get(5)),
            "gate_mag": int(gate_mag.group(1)) / 10.0 if gate_mag else None,
        }
    return out


def card_attribute():
    """card id -> attribute number, from `Card` field 10 (`rarity_5-attribute_2`).

    Needed because `deck_card_attribute-attribute_N-K` -- 46 of the 132 gates -- is a condition on
    the ASSEMBLED TEAM ("at least K members of attribute N"), so it is resolvable exactly from the
    five member ids and nothing else, so it is evaluated rather than assumed."""
    import master_cache as mc
    out = {}
    for r in mc.rows("Card"):
        m = re.search(r"attribute_(\d+)", _text(r["data"].get(10)) or "")
        if m:
            out[r["id"]] = int(m.group(1))
    return out


def special_riders():
    """card id -> {level: (probability_up fraction, special duration s, own trigger)}.

    No card alters an Active cooldown -- that comes only from the board -- but cards DO alter
    activation probability: 60 of the 334 `LiveSpecialSkillLevel` rows carry, in field 5,
    `live_special_skill_effect-live_active_skill_activation_probability_up_permil_up-<permil>` at
    350-550 permil, across 30 distinct cards, two levels each. A taxonomy built from
    `LivePassiveSkillEffect` misses them, because SPECIAL skills do not appear in that table.

    This is the rider `SkillSlot` represents and `src/hbuilder/domain/scoring.py` deliberately
    does not fold. It is a per-CARD term -- different skins of one holomem carry different SPECIAL
    skills -- so probability has two sources with two different keys, the board (per holomem) and
    this rider (per card), while the cooldown reduction is keyed by holomem alone."""
    import master_cache as mc
    out = {}
    for r in mc.rows("LiveSpecialSkillLevel"):
        d = r["data"]
        eff = _text(d.get(5))
        if not eff or "activation_probability" not in eff:
            continue
        cid = re.search(r"card-\S+", _text(d.get(1)) or "")
        m = re.search(r"(\d+)$", eff)
        if not (cid and m):
            continue
        out.setdefault(cid.group(), {})[int(_text(d.get(2)))] = (
            int(m.group(1)) / 1000.0, d.get(6, [0])[0] / 1000.0, _text(d.get(4)))
    return out


def gate_fires(gate, member_ids, attrs, policy):
    """Does this gate hold, for this team, under this policy?

    `deck_card_attribute-attribute_N-K` and `deck_card_character_grouping` are decidable from the
    team alone and are always RESOLVED. `combo_gte` and `life_gte` are run-time states the deck
    screen cannot know; `policy` says what the screen is assumed to do with them ("fire" or
    "skip"), and that assumption is what the bake-off tests."""
    if not gate:
        return False
    m = re.search(r"deck_card_attribute-attribute_(\d+)-(\d+)", gate)
    if m:
        if policy.get("attr", "resolve") == "skip":
            return False
        want, need = int(m.group(1)), int(m.group(2))
        return sum(1 for c in member_ids if attrs.get(c) == want) >= need
    if "deck_card_character_grouping" in gate:
        return policy.get("grouping", "skip") == "fire"
    if "combo_gte" in gate:
        return policy.get("combo", "skip") == "fire"
    if "life_gte" in gate:
        return policy.get("life", "skip") == "fire"
    return False


def card_character():
    """card id -> character id, from `Card` field 2.

    Not inferred from the id shape: the master `Card` table carries the character id in field 2.
    `--decks` prints how many of the 167 rows have the numeric block of the card id equal to the
    numeric block of the character id, so the shape rule is verified against the master field
    rather than trusted."""
    import master_cache as mc
    return {r["id"]: _text(r["data"].get(2)) for r in mc.rows("Card")}


def _text(v):
    if not v:
        return None
    x = v[0]
    return x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x)


# --------------------------------------------------------------------------- board

def board_active_terms(scale=1.0):
    """character -> (probability_up, cooldown_shorten), both as fractions, from unlocked tiles.

    NOT the published "+30% / -12%" ceiling. That figure is the value of a FULLY unlocked blue
    branch on ONE character. What a given member actually carries is the sum of the blue tiles
    SHE has unlocked, which is what this reads.

    `scale` lets a fit test the hypothesis that board values are half what `skilltree.json`
    stores (a level-1 versus level-2 reading). `board_state.json` records node ids without
    levels, so every board-aware fit is run at scale 1.0 and 0.5 and both residuals reported."""
    tree = json.load(open(SKILLTREE, encoding="utf-8"))["nodes"]
    state = json.load(open(BOARD, encoding="utf-8"))
    out = {}
    for chrid, rec in state.items():
        prob = cool = 0.0
        for nid in rec.get("nodes", ()):
            node = tree.get(nid)
            if not node:
                continue
            e = node.get("e") or {}
            if e.get("t") == "Act.Prob":
                prob += e["v"] / 1000.0
            elif e.get("t") == "Act.CD":
                cool += e["v"] / 1000.0
        out[chrid] = (prob * scale, cool * scale)
    return out


def board_from_captures():
    """card id -> (probability_up, cooldown_shorten), as the SERVER states them.

    Strictly better than deriving them from `board_state.json` x `skilltree.json`, and present in
    every `Live/GetDeck` capture. `LiveDeckInGameEffect.live_deck_positions` gives, per
    member, `{6 live_active_skill_effect_up_permil_up, 7 ..._activation_probability_up_permil_up,
    8 ..._cool_time_shorten_permil_up}` -- all three as fixed32 FLOATS, which is why reading them
    as varints yields nonsense. `deck_oracle.py` reads top-level field 2 and stops; this function
    re-derives them from the captures.

    Measured over every capture on disk, fields 7 and 8 are INVARIANT for a card across every deck
    it appears in, so they are per-character board properties. Field 6 is not invariant -- it moves
    with the deck, because it is the leader-scoped Act.Power / support term -- and is not used here.

    Cross-check against the tiles: `card-04001-5` +80 and `card-00036-5` +60 agree EXACTLY with
    summing that character's unlocked blue `Act.Prob` nodes; `card-00031-5` reads 231 against a
    tile sum of 180 and `card-00023-*` 173 against 140. Both gaps are on the two characters whose
    Connect slots are filled, and a Connect bonus multiplies the tiles in its own shape by
    55-220 %, so the server's numbers are the tile sums WITH the connect bonus applied
    and the tile sums alone are an under-count."""
    import re as _re
    import struct
    import glob
    import grpc_decrypt as gd
    from master_db import decode_record
    card_re = _re.compile(rb"card-\d{5}-\d-[a-z]{4}-\d{4}-\d{2}")
    out = {}
    for path in sorted(glob.glob(os.path.join(HERE, "dumps", "*.mitm"))):
        for rpc, _t, ct in gd.iter_grpc(path):
            if "GetDeck" not in rpc and "SaveDeck" not in rpc:
                continue
            pt = gd.decrypt(ct, bytes.fromhex(gd.KEY_HEX))
            for m in card_re.finditer(pt):
                i = m.start()
                pre = pt[i - 4:i]
                if len(pre) != 4 or pre[0] != 0x08 or pre[2] != 0x12:
                    continue
                rec = None
                for span in (140, 110, 80):
                    try:
                        rec = decode_record(pt[i - 4:i + span])
                    except Exception:
                        continue
                    break
                if not rec or not (7 in rec or 8 in rec):
                    continue

                def f32(k):
                    v = rec.get(k, [0])[0]
                    return struct.unpack("<f", int(v).to_bytes(4, "little"))[0] / 1000.0

                prob, cool = f32(7), f32(8)
                cid = m.group().decode()
                if cid in out and out[cid] != (prob, cool):
                    raise RuntimeError(f"{cid} board effect not invariant: "
                                       f"{out[cid]} vs {(prob, cool)}")
                out[cid] = (prob, cool)
    return out


# --------------------------------------------------------------------------- decks

def evaluations(only=None):
    """Every deck evaluation in every capture on disk, not just the five in `deck_oracle.json`.

    It needs no new capture. `deck_oracle.json` holds ten evaluations of five compositions.
    Running `deck_oracle.evaluations()` over ALL nine `.mitm` files in `dumps/` instead of one
    live capture yields 24 evaluations of **nine distinct compositions** -- including deck
    675/644, which is absent from `deck_oracle.json` entirely.

    Nothing here writes to `dumps/deck_oracle.json`; the harvest is recomputed each run (~1 s)."""
    import glob
    import deck_oracle as do
    out = []
    for path in sorted(glob.glob(os.path.join(HERE, "dumps", "*.mitm"))):
        if only and only not in path:
            continue
        try:
            evs = do.evaluations(path)
        except Exception:
            continue
        for e in evs:
            e["src"] = os.path.basename(path)
            out.append(e)
    return out


def decks(only=None):
    """The distinct deck compositions, with the server's Active line.

    Compositions are keyed by the SET of five cards, not the slot order. Two captures in
    `deck_capture.mitm` hold the same five cards in different slots and both read 675 per-mille,
    which is a finding in its own right: the Active line carries no positional weighting, unlike
    the SPECIAL line the community optimizer fits positional weights to.

    Repeats are checked rather than assumed. Composition 686 appears in four different captures
    over one day and reads 686 every time, while its Holomem Board line moves (203 -> 215) as
    tiles are unlocked. Composition 318 reads 318 in captures two days apart. So over the whole
    capture window the Active line is STABLE: no skill level changed, and board progress did not
    move it."""
    seen = {}
    for i, dk in enumerate(evaluations(only)):
        ids = frozenset(v["card"] for v in (dk.get("cards") or {}).values())
        if len(ids) != 5:
            continue
        seen.setdefault(ids, []).append(dk)
    out = []
    for ids, obs in seen.items():
        acts = {o["bonus"].get(ACTIVE_KEY) for o in obs}
        out.append({
            "cards": sorted(ids),
            "active": obs[0]["bonus"].get(ACTIVE_KEY) / 10.0,      # percent
            "n_obs": len(obs),
            "srcs": sorted({o["src"] for o in obs}),
            "active_agree": len(acts) == 1,
            "tree": [o["bonus"].get("by_skill_tree [Holomem Board]") for o in obs],
            "outfit": [o["bonus"].get("by_live_leader_skill [Outfit Skill]") for o in obs],
            "special": obs[0]["bonus"].get("by_live_special_skill [Special Skill]"),
        })
    out.sort(key=lambda d: d["active"])
    for n, d in enumerate(out):
        d["name"] = "ABCDEFGHIJKLMNOP"[n]
    return out


def _key(card_id):
    return "-".join(card_id.split("-")[:3])


# --------------------------------------------------------------------------- models
#
# Every model is `SUM over the five members of one per-card term`, except the `max` stacking
# family, which is the community optimizer's "only the highest overlapping Active Score UP
# counts" written as an expectation over independent skills.

class Term(tuple):
    """(magnitude, occupancy, probability, effective cooldown, duration) for one member.

    The first two are all the `sum` and `max` models read. The last three are what `maxgrid`
    needs: it integrates over the real timeline instead of assuming a skill's up-time is spread
    uniformly and independently."""
    __slots__ = ()

    def __new__(cls, mag, occ, prob, cool, dur):
        return tuple.__new__(cls, (mag, occ, prob, cool, dur))

    mag = property(lambda self: self[0])
    occ = property(lambda self: self[1])
    prob = property(lambda self: self[2])
    cool = property(lambda self: self[3])
    dur = property(lambda self: self[4])


def uptime_fraction(dur, cool, kind, T):
    """The fraction of the reference window this skill is up, GIVEN it rolls successfully.

    `ratio`  dur/cool -- the long-run duty cycle, what the current `_score_active` uses.
    `quant`  the finite window done honestly: activations at k*cool for k = 1..floor(T/cool)
             (the clock starts at measure 0 and the FIRST proc is one cooldown in, not at
             t=0), each contributing min(dur, T - k*cool) so the last one is truncated
             by the end of the run.
    `quantx` the same count but with no truncation, i.e. n*dur/T.

    `quant` and `quantx` are the finite-window candidate: the activation COUNT is an integer, so
    two decks with equal duty and different cooldowns land on different totals. A smooth duty
    model cannot produce that at all."""
    if kind == "ratio":
        return min(1.0, dur / cool)
    n = int(T // cool)
    if kind == "quantx":
        return min(1.0, n * dur / T)
    total = 0.0
    for k in range(1, n + 1):
        total += min(dur, T - k * cool)
    return min(1.0, total / T)


def per_card(row, cfg, board, fired=False, rate_up=0.0):
    """(effective magnitude, effective uptime x probability) for one member."""
    mag = row["mag"]
    if row["gate_mag"] is not None and (fired or cfg.get("gate") == "fired"):
        mag = row["gate_mag"]
    prob_up, cool_dn = board
    cool = row["cool"] / (1.0 + cool_dn)               # the cooldown reduction DIVIDES
    prob = row["prob"] * (1.0 + prob_up + rate_up) if cfg["prob_up"] == "mul" \
        else row["prob"] + prob_up + rate_up
    prob = min(1.0, prob)
    up = uptime_fraction(row["dur"], cool, cfg["uptime"], cfg["T"])
    if cfg["prob"] == "none":
        prob = 1.0
    return Term(mag, up * prob, prob, cool, row["dur"])


def team_rate_up(d, cfg, riders, attrs, level_of=None):
    """The team-wide activation-probability uplift the members' SPECIAL riders contribute.

    `off`  ignore it.
    `avg`  the rider is live only while its SPECIAL is up, so its
           time-average over the reference window is
               expected_fires_per_run (= 1) x duration / T x magnitude.
    `full` the rider at full magnitude for the whole run -- an upper bound, present so the
           bracket is visible rather than assumed away.

    A rider can carry its own trigger (life_gte / combo_gte / character_grouping) in field 4;
    that is resolved by the same policy as an ACTIVE gate."""
    mode = cfg.get("rider", "off")
    if mode == "off":
        return 0.0
    total = 0.0
    for c in d["cards"]:
        r = riders.get(c)
        if not r:
            continue
        lv = (level_of or {}).get(c, 1)
        mag, dur, trig = r.get(lv) or r[1]
        if trig and not gate_fires(trig, d["cards"], attrs, cfg.get("policy") or {}):
            continue
        total += mag if mode == "full" else mag * dur / cfg["T"]
    return total


_BOARD_CACHE = {}


def _boards(cfg):
    """The board term a model wants, keyed by character (tiles) or by card (server)."""
    if not cfg.get("board"):
        return {}
    key = (cfg.get("board_src", "tiles"), cfg.get("board_scale", 1.0))
    if key not in _BOARD_CACHE:
        if key[0] == "server":
            _BOARD_CACHE[key] = {c: (p * key[1], k * key[1])
                                 for c, (p, k) in board_from_captures().items()}
        else:
            _BOARD_CACHE[key] = board_active_terms(key[1])
    return _BOARD_CACHE[key]


def deck_terms(d, cfg, levels, chars, attrs, boards, riders=None):
    """{(card, level): (magnitude, uptime x probability)} for one deck's five members.

    Gate resolution happens HERE, per deck, because `deck_card_attribute` depends on who else is
    in the team. The level assignment does not change which gates fire -- both levels of a slot
    carry the same trigger -- so this can be precomputed once per (deck, model)."""
    out = {}
    rate = team_rate_up(d, cfg, riders or {}, attrs)
    for c in d["cards"]:
        row1 = levels[_key(c)][1]
        fired = gate_fires(row1["gate"], d["cards"], attrs, cfg.get("policy") or {})
        for lv in (1, 2):
            key = c if cfg.get("board_src") == "server" else chars.get(c)
            out[(c, lv)] = per_card(levels[_key(c)][lv], cfg,
                                    boards.get(key, (0.0, 0.0)), fired, rate)
    return out


def deck_gates(d, cfg, levels, attrs):
    """Which members' gates fire in this deck, for reporting."""
    return [c for c in d["cards"]
            if gate_fires(levels[_key(c)][1]["gate"], d["cards"], attrs, cfg.get("policy") or {})]


# --------------------------------------------------------------------------- the fit

def _combine(terms, stack, T=T_REF, dt=0.01):
    if stack == "sum":
        return sum(t.mag * t.occ for t in terms)
    if stack == "max":
        # Only the highest-magnitude skill that is currently up counts. Treating each skill's
        # up-time as spread uniformly and independently over the run,
        #     E[max] = SUM_i mag_i u_i PROD_{j<i} (1 - u_j),  i sorted by magnitude descending.
        out, free = 0.0, 1.0
        for t in sorted(terms, key=lambda t: -t.mag):
            out += t.mag * t.occ * free
            free *= (1.0 - t.occ)
        return out
    return _maxgrid(tuple(sorted(terms, key=lambda t: -t.mag)), T)


_GRID_CACHE = {}


def _maxgrid(ts, T):
    """E[max] with no uniform-spread approximation.

    The skills do not share a clock loosely: every one of them starts counting at measure 0
    and fires on its own fixed period, so which pairs can overlap is DETERMINISTIC
    and the phases are not free. Since every card in the data has duration < cooldown, at most one
    window of a given skill covers any instant, so at time t skill i is up with probability p_i if
    t falls inside one of its windows and 0 otherwise, independently across skills. Then

        E[max] = (1/T) INTEGRAL SUM_i mag_i a_i(t) PROD_{j: mag_j > mag_i} (1 - a_j(t)) dt

    which is exact given that the activation rolls are independent."""
    hit = _GRID_CACHE.get((ts, T))
    if hit is not None:
        return hit
    # The integrand is piecewise constant, so integrate over exact segments rather than sampling:
    # the breakpoints are the window edges, about 60 of them for a five-member deck.
    edges = {0.0, T}
    for x in ts:
        k = 1
        while k * x.cool < T:
            edges.add(k * x.cool)
            edges.add(min(k * x.cool + x.dur, T))
            k += 1
    cut = sorted(edges)
    total = 0.0
    for lo, hi in zip(cut, cut[1:]):
        mid, acc, free = (lo + hi) / 2.0, 0.0, 1.0
        for x in ts:
            a = x.prob if (mid >= x.cool and (mid % x.cool) < x.dur) else 0.0
            acc += x.mag * a * free
            free *= (1.0 - a)
        total += acc * (hi - lo)
    out = total / T
    _GRID_CACHE[(ts, T)] = out
    return out


def fit(models, dks, levels, chars, attrs, riders, verbose=True, targets=None):
    """Best achievable residual per model over ALL consistent skill-level assignments.

    The level of a card is a property of the account, so a card appearing in four decks
    must be at the same level in all four. Enumerating the assignments rather than supplying them
    is what turns "this model fits if you pick the right Bloom stages" into a falsifiable
    statement -- and it needs no Bloom data, which is recorded nowhere on disk.

    SEARCH. 25 distinct cards would be 33 M assignments. But 14 of them appear in exactly one
    deck, and a deck-local card's level cannot affect any other deck, so the search factorises
    exactly: enumerate the 2^11 assignments of the SHARED cards, then per deck pick the best of
    at most 2^4 local assignments independently. No approximation, ~300 k evaluations."""
    cards = sorted({c for d in dks for c in d["cards"]})
    count = {c: sum(1 for d in dks if c in d["cards"]) for c in cards}
    shared = [c for c in cards if count[c] > 1]
    sidx = {c: i for i, c in enumerate(shared)}
    obs = targets or {d["name"]: d["active"] for d in dks}
    results = []
    for name, cfg in models:
        boards = _boards(cfg)
        term = {d["name"]: deck_terms(d, cfg, levels, chars, attrs, boards, riders) for d in dks}
        locals_ = {d["name"]: [c for c in d["cards"] if count[c] == 1] for d in dks}
        best = None
        for bits in range(1 << len(shared)):
            resid, picks = [], {}
            for d in dks:
                td = term[d["name"]]
                fixed = [td[(c, 2 if (bits >> sidx[c]) & 1 else 1)]
                         for c in d["cards"] if count[c] > 1]
                loc = locals_[d["name"]]
                bestd = None
                for lb in range(1 << len(loc)):
                    ts = fixed + [td[(c, 2 if (lb >> j) & 1 else 1)]
                                  for j, c in enumerate(loc)]
                    r = _combine(ts, cfg["stack"], cfg["T"]) - obs[d["name"]]
                    if bestd is None or abs(r) < abs(bestd[0]):
                        bestd = (r, lb)
                resid.append(bestd[0])
                picks[d["name"]] = bestd[1]
            worst = max(abs(r) for r in resid)
            if best is None or worst < best[0]:
                best = (worst, bits, list(resid), dict(picks))
        results.append((name, cfg, best, shared, sidx, locals_))
        if verbose:
            worst, bits, resid, picks = best
            rms = math.sqrt(sum(r * r for r in resid) / len(resid))
            print(f"{name:32} worst {worst:6.2f}  rms {rms:5.2f}   "
                  + " ".join(f"{d['name']}{r:+6.2f}" for d, r in zip(dks, resid)))
    return results


# --------------------------------------------------------------------------- reports

def report_decks(dks, levels, chars, attrs, riders):
    boards = board_active_terms(1.0)
    n_ok = n_tot = 0
    for cid, ch in chars.items():
        if ch:
            n_tot += 1
            n_ok += cid.split("-")[1] == ch.split("-")[1]
    print(f"card -> character: `Card` field 2 is ATTESTED for {n_tot} cards; the numeric block "
          f"of the card id equals the character's on {n_ok}/{n_tot}.\n")
    for d in dks:
        b = [chars.get(c) for c in d["cards"]]
        nb = sum(1 for c in b if boards.get(c, (0, 0))[0] or boards.get(c, (0, 0))[1])
        print(f"=== deck {d['name']}   server Active {d['active']:5.1f}%   seen {d['n_obs']}x in "
              f"{len(d['srcs'])} capture(s), all agreeing: {d['active_agree']}"
              f"   board line {d['tree']}   outfit line {d['outfit']}"
              f"   members with blue Active tiles: {nb}/5")
        for c, ch in zip(d["cards"], b):
            r1, r2 = levels[_key(c)][1], levels[_key(c)][2]
            pu, cd = boards.get(ch, (0.0, 0.0))
            print(f"    {c:26} {ch:10} "
                  f"mag {r1['mag']:5.1f}/{r2['mag']:5.1f}  cool {r1['cool']:5.1f}  "
                  f"prob {r1['prob']:.2f}  dur {r1['dur']:4.1f}/{r2['dur']:4.1f}  "
                  f"board +{pu * 100:4.1f}%p -{cd * 100:4.1f}%cd  "
                  f"gate {r1['gate'] or '-'}")
        print()


def report_order(models, dks, levels, chars, attrs, riders):
    """The assumption-free test: does the model even ORDER the decks the way the server does?

    Every free parameter of the model -- the reference duration, a saturation
    curve, a global calibration constant, which Bloom stage each card sits at -- is either a
    positive scale or a monotone increasing transform of the model's own output. NONE of them can
    reorder two decks. So:

        if pred(X) > pred(Y) for EVERY level assignment, while the server says X < Y,
        then no monotone calibration of this model can ever fit both decks.

    Level 2 dominates level 1 on every card (magnitude never falls, duration never falls, cooldown
    and probability never change), so a deck's whole reachable prediction range is exactly
    [all members at level 1, all members at level 2] -- the extremes are computable, not searched.

    A model with zero violations is not thereby correct; it is merely still alive. A model with
    violations is dead in a way that no constant can revive."""
    print("order violations -- pairs the model ranks the opposite way from the server, "
          "under EVERY level assignment\n")
    for name, cfg in models:
        boards = _boards(cfg)
        rng = {}
        for d in dks:
            td = deck_terms(d, cfg, levels, chars, attrs, boards, riders)
            lo = _combine([td[(c, 1)] for c in d["cards"]], cfg["stack"], cfg["T"])
            hi = _combine([td[(c, 2)] for c in d["cards"]], cfg["stack"], cfg["T"])
            rng[d["name"]] = (min(lo, hi), max(lo, hi))
        bad = []
        for x in dks:
            for y in dks:
                if x["active"] < y["active"] and rng[x["name"]][0] > rng[y["name"]][1]:
                    bad.append((x, y))
        tag = "ALIVE" if not bad else f"{len(bad)} violation(s)"
        print(f"  {name:32} {tag}")
        for x, y in bad:
            print(f"        server {x['name']} {x['active']:5.1f} < {y['name']} {y['active']:5.1f}"
                  f"   but model {x['name']} in [{rng[x['name']][0]:.1f},{rng[x['name']][1]:.1f}]"
                  f" > {y['name']} in [{rng[y['name']][0]:.1f},{rng[y['name']][1]:.1f}]")
    print()


def report_swap(dks, levels, chars, attrs, riders):
    """The two decks that differ by exactly one card -- the sharpest datum in the capture.

    Decks D and E share four members. Whatever the Active line is, the difference between them is
    ONE card's contribution minus another's, with every other term identical and every unknown
    (leader, board, reference duration, stacking rule) cancelling if the line is additive."""
    pairs = [(a, b) for a in dks for b in dks
             if len(set(a["cards"]) & set(b["cards"])) == 4 and a["active"] < b["active"]]
    for a, b in pairs:
        gone = (set(a["cards"]) - set(b["cards"])).pop()
        came = (set(b["cards"]) - set(a["cards"])).pop()
        print(f"deck {a['name']} ({a['active']:.1f}%) -> deck {b['name']} ({b['active']:.1f}%): "
              f"drop {gone}, add {came}   observed delta {b['active'] - a['active']:+.1f} pp")
        best = -1e9
        for cfg_name, cfg in MODELS:
            if cfg["stack"] != "sum":
                continue
            boards = _boards(cfg)
            ta = deck_terms(a, cfg, levels, chars, attrs, boards, riders)
            tb = deck_terms(b, cfg, levels, chars, attrs, boards, riders)
            span = []
            for lg in (1, 2):
                for lc in (1, 2):
                    g, c2 = ta[(gone, lg)], tb[(came, lc)]
                    span.append(c2.mag * c2.occ - g.mag * g.occ)
            print(f"    {cfg_name:32} delta in [{min(span):+6.2f}, {max(span):+6.2f}] "
                  f"over the four level combinations")
            best = max(best, max(span))
        print(f"    -> the largest delta ANY additive model here can produce is {best:+.2f} pp "
              f"against an observed {b['active'] - a['active']:+.1f} pp\n")


def report_s17(dks, levels, chars, attrs, riders):
    """Is a published per-deck duty column reachable by ONE level assignment?

    The column (36.3 / 37.5 / 37.9 / 65.5 / 85.0) was obtained by pinning each card's skill level
    from Bloom states that are recorded nowhere on disk, so it cannot be re-derived -- but it can
    be TESTED, because the five decks share cards and a skill level is a property of the card,
    not of the deck. If no single consistent assignment reproduces all five numbers, the table is
    internally inconsistent."""
    quoted = {26.4: 36.3, 36.5: 37.5, 31.8: 37.9, 68.6: 65.5, 67.1: 85.0}
    sub = [d for d in dks if d["active"] in quoted]
    targets = {d["name"]: quoted[d["active"]] for d in sub}
    res = fit([("section 17 duty column", dict(MODEL_NAIVE))], sub, levels, chars, attrs,
              riders, verbose=False, targets=targets)
    worst, bits, resid, picks = res[0][2]
    print("Is section 17's own duty column reachable by ONE consistent level assignment?")
    for d, r in zip(sub, resid):
        print(f"    deck {d['name']}  server {d['active']:5.1f}   "
              f"section 17 quotes duty {targets[d['name']]:5.1f}   "
              f"closest consistent assignment {targets[d['name']] + r:6.2f}   miss {r:+5.2f}")
    print(f"    worst miss {worst:.2f} duty points\n")


def report_best(name, dks, levels, chars, attrs, riders, tol=1.0, quiet=False):
    """Everything about one model: its level assignment, how unique that is, and its gates.

    A fit with 25 free binary parameters over 9 observations can flatter itself, so this reports
    the things that decide whether it is real: how many DISTINCT consistent level assignments
    reach the tolerance (if the answer is "almost all of them", the fit is unconstrained), what
    the winning assignment actually says about the account's Bloom progress (checkable against
    known Bloom stages of five of these cards), and which gates the model had to fire to get
    there."""
    cfg = dict(next(c for n, c in MODELS if n == name))
    boards = _boards(cfg)
    cards = sorted({c for d in dks for c in d["cards"]})
    count = {c: sum(1 for d in dks if c in d["cards"]) for c in cards}
    shared = [c for c in cards if count[c] > 1]
    sidx = {c: i for i, c in enumerate(shared)}
    term = {d["name"]: deck_terms(d, cfg, levels, chars, attrs, boards, riders) for d in dks}
    locals_ = {d["name"]: [c for c in d["cards"] if count[c] == 1] for d in dks}

    hits, best = 0, None
    for bits in range(1 << len(shared)):
        resid, lv = [], {}
        for d in dks:
            td, loc = term[d["name"]], locals_[d["name"]]
            for c in d["cards"]:
                if count[c] > 1:
                    lv[c] = 2 if (bits >> sidx[c]) & 1 else 1
            bestd = None
            fixed = [td[(c, lv[c])] for c in d["cards"] if count[c] > 1]
            for lb in range(1 << len(loc)):
                ts = fixed + [td[(c, 2 if (lb >> j) & 1 else 1)] for j, c in enumerate(loc)]
                r = _combine(ts, cfg["stack"], cfg["T"]) - d["active"]
                if bestd is None or abs(r) < abs(bestd[0]):
                    bestd = (r, lb)
            for j, c in enumerate(loc):
                lv[c] = 2 if (bestd[1] >> j) & 1 else 1
            resid.append(bestd[0])
        worst = max(abs(r) for r in resid)
        if worst <= tol:
            hits += 1
        if best is None or worst < best[0]:
            best = (worst, dict(lv), list(resid))
    worst, lv, resid = best
    if quiet:
        return lv
    rms = math.sqrt(sum(r * r for r in resid) / len(resid))
    print(f"=== {name}")
    print(f"    worst |resid| {worst:.2f} pp, rms {rms:.2f} pp over {len(dks)} decks")
    print(f"    {hits} of {1 << len(shared)} shared-card level assignments reach "
          f"|resid| <= {tol} pp on every deck")
    for d, r in zip(dks, resid):
        fired = deck_gates(d, cfg, levels, attrs)
        print(f"    deck {d['name']}  server {d['active']:5.1f}  model {d['active'] + r:6.2f}  "
              f"{r:+5.2f}   gates fired {len(fired)}/5")
    print("    skill level per card in the winning assignment "
          "(2 = Bloom >= 1, GAME-DATA-FINDINGS section 4):")
    for c in cards:
        print(f"        {c:28} level {lv[c]}   (in {count[c]} deck(s))")
    print()
    return lv


# --------------------------------------------------------------------------- timeline sim

def simulate(chart_path, terms, stack="max", runs=4000, seed=0, other_bonus=0.0):
    """Monte Carlo of one clear on a real chart: roll every skill on its own cooldown grid.

    THE POINT. `--fit` fits the deck screen's PREVIEW. This computes the other quantity -- the
    in-run distribution -- on the real note timeline from `sus_parse`, and it fits nothing, so it
    is a prediction rather than a fit. The run-to-run spread measures 21.4 % over four AUTO runs
    of `m0001` easy with one fixed team; a model of the activation process has to
    reproduce a band of that size from the same inputs or it is not describing the same process.

    Each note is scored `1 + other_bonus + multiplier(t)/100` where the multiplier is the sum, or
    the maximum, of the skills up at that instant. Notes are weighted equally: the `LiveNote`
    per-type weights change the absolute score but very nearly cancel in the ratio between two
    runs of the SAME chart, and the ratio is what the 21 % band measures."""
    import sus_parse
    chart = sus_parse.parse(open(chart_path, encoding="utf-8", errors="replace").read())
    times = [n.time for _, n in sus_parse.classify(chart)]
    end = chart.duration
    rng = random.Random(seed)
    out = []
    for _ in range(runs):
        wins = []
        for x in terms:
            k = 1
            while k * x.cool <= end:
                if rng.random() < x.prob:
                    wins.append((k * x.cool, k * x.cool + x.dur, x.mag))
                k += 1
        total = 0.0
        for t in times:
            live = [m for a, b, m in wins if a <= t < b]
            mult = 0.0 if not live else (sum(live) if stack == "sum" else max(live))
            total += 1.0 + other_bonus + mult / 100.0
        out.append(total)
    out.sort()
    return {"n": runs, "notes": len(times), "duration": end,
            "mean": sum(out) / runs, "min": out[0], "max": out[-1],
            "spread_full": out[-1] / out[0] - 1.0,
            "spread_4draw": _expected_4draw_spread(out, rng),
            "p_ge_21_4": _p_spread_at_least(out, rng, 0.214)}


def _p_spread_at_least(scores, rng, target, trials=40000):
    """P(four draws span at least `target`), so a single four-run observation can be TESTED.

    The measured 21.4 % is one realisation of a random variable, not a constant. Comparing it to
    a model's mean four-draw spread and calling the gap a discrepancy would be a mistake; what the
    observation can do is exclude a model whose four-draw spread almost never reaches it."""
    n, hits = len(scores), 0
    for _ in range(trials):
        s = [scores[rng.randrange(n)] for _ in range(4)]
        hits += (max(s) / min(s) - 1.0) >= target
    return hits / trials


def _expected_4draw_spread(scores, rng, trials=20000):
    """Expected max/min - 1 over FOUR draws, which is what the four-run observation measures.

    Quoting the full-distribution range against a four-run observation would overstate the model's
    spread: four draws from a wide distribution rarely hit both tails."""
    n, tot = len(scores), 0.0
    for _ in range(trials):
        s = [scores[rng.randrange(n)] for _ in range(4)]
        tot += max(s) / min(s) - 1.0
    return tot / trials


# --------------------------------------------------------------------------- model space

# CANONICAL FORM. A controlled SaveDeck design (dumps/_active_calib.json = all 21 five-card subsets
# of a 7-card ungated pool, with exact server Active integers) settles the model form; the
# `max`/`window` bases below are kept for comparison:
#   1. AGGREGATION is a per-note MAX, bucketed (CalcScoreUpRatio @0x5ee3910):
#      ratio = MAX_normal(term) + MAX_special(term), term = (1+permil/1000+B)*ScoreUpByRatio, and for a
#      normal effect ScoreUpByRatio = the card's own stored magnitude (Value/1000, per-card constant).
#      For an all-normal deck this is a pure per-note TOP-1 MAX over active cards -> stack="max" is the
#      right family (active_sim's E[max] is its probabilistic approximation). No reference chart fits
#      (all 728 real + synthetic + the continuous limit were tried) -- the residual is not chart
#      timing but the per-card OCCUPANCY: server occ != master p*dur/cool (duty RMS 8-22 vs a free
#      per-card occ fit 3.3-5.6). The open problem is the occupancy law, not the aggregation.
#   2. OCCUPANCY is duty-from-activation dur/cool (uptime="ratio"): cooldown timed from ACTIVATION,
#      not the markov p*dur/(cool+p*dur) (markov RMS 46 -> duty 41 closed form, ~17 per-note over a
#      real chart). uptime="ratio" already encodes this.
#   3. The panel is ORDER-INVARIANT (same 5 cards in 4 slot orderings -> identical server Active
#      691), so there is NO slot-index activation stagger to model; and limit_count is NULL on all
#      337 rows, so there is NO per-skill activation cap. The value is a deterministic EXPECTED
#      value over a fixed reference chart.
# WHY NOT BIT-EXACT: the expected per-note sum is LINEAR in occupancy and would fit exactly; it
# floors at ~1% because simultaneous-active notes are discounted below the sum -- a per-note overlap
# that needs the hidden reference chart's exact discrete note timing (not obtainable client-side;
# the integer is READ via GetDeck/SaveDeck). These corrections cut the closed-form error +13-16% ->
# ~1%. NOTE: this is the RECON model for the deck-panel line, NOT src/hbuilder's in-play score.
MODEL_NAIVE = {"uptime": "ratio", "prob": "mul", "prob_up": "mul", "policy": {},
               "stack": "sum", "board": False, "T": T_REF}

# GATE POLICY. 132 of the 337 `LiveActiveSkillLevel` rows carry a trigger in field 5 that selects
# a DOUBLED magnitude in field 6. Four kinds exist:
#
#   deck_card_attribute-attribute_N-K   46   "at least K members of attribute N" -- decidable from
#                                            the team alone, so it is always RESOLVED here, never
#                                            assumed.
#   deck_card_character_grouping         2   likewise decidable in principle; policy-controlled
#                                            here because the grouping table is not wired in.
#   combo_gte-N                         44   run-time state
#   life_gte-N                          40   run-time state
#
# The deck screen cannot know a run-time state, so what it does with `combo_gte`/`life_gte` is a
# modelling choice, and which choice the server actually makes is exactly what the bake-off is
# for. `skip` = show the base magnitude; `fire` = show the doubled one.
POLICIES = [
    ("BASE (no gate ever)", {"attr": "skip", "combo": "skip", "life": "skip"}),
    ("attr only", {"combo": "skip", "life": "skip"}),
    ("attr+combo", {"combo": "fire", "life": "skip"}),
    ("attr+life", {"combo": "skip", "life": "fire"}),
    ("all gates", {"combo": "fire", "life": "fire", "grouping": "fire"}),
]


def _m(**kw):
    cfg = dict(MODEL_NAIVE)
    cfg.update(kw)
    return cfg


BASES = [
    ("grid-max", {"stack": "maxgrid"}),
    ("grid-max+rider", {"stack": "maxgrid", "rider": "avg"}),
    ("grid-max+board", {"stack": "maxgrid", "board": True, "board_scale": 1.0}),
    ("naive+max+rider", {"stack": "max", "rider": "avg"}),
    ("naive+max+board", {"stack": "max", "board": True, "board_scale": 1.0}),
    ("naive+max+SRVboard", {"stack": "max", "board": True, "board_src": "server",
                            "board_scale": 1.0}),
    ("window+max+SRVboard", {"uptime": "quant", "stack": "max", "board": True,
                             "board_src": "server", "board_scale": 1.0}),
    ("naive+max+SRVboard+rdr", {"stack": "max", "board": True, "board_src": "server",
                                "board_scale": 1.0, "rider": "avg"}),
    ("naive+max+board/2", {"stack": "max", "board": True, "board_scale": 0.5}),
    ("naive+max+board+rider", {"stack": "max", "board": True, "board_scale": 1.0,
                               "rider": "avg"}),
    ("naive+max+rider(full)", {"stack": "max", "rider": "full"}),
    ("naive duty+rider", {"rider": "avg"}),
    ("window+max+rider", {"uptime": "quant", "stack": "max", "rider": "avg"}),
    ("naive duty", {}),
    ("naive+board", {"board": True, "board_scale": 1.0}),
    ("naive+board/2", {"board": True, "board_scale": 0.5}),
    ("naive+max", {"stack": "max"}),
    ("window", {"uptime": "quant"}),
    ("window(no trunc)", {"uptime": "quantx"}),
    ("window+board", {"uptime": "quant", "board": True, "board_scale": 1.0}),
    ("window+board/2", {"uptime": "quant", "board": True, "board_scale": 0.5}),
    ("window+max", {"uptime": "quant", "stack": "max"}),
]

MODELS = [(f"{bn} / {pn}", _m(policy=pk, **bk)) for bn, bk in BASES for pn, pk in POLICIES]

# The canonical model: per-note MAX (stack="max", the form CalcScoreUpRatio implements) + duty
# occupancy + all gates fire (the panel fires life/combo) + real server board floats. The dominant
# residual is the per-card OCCUPANCY (server occupancy != master p*dur/cool), not the aggregation or
# a reference chart. Best-effort form; the bit-exact integer is READ, never computed.
MODEL_CANONICAL = _m(stack="max", uptime="ratio", board=True, board_src="server", board_scale=1.0,
                     policy={"combo": "fire", "life": "fire", "grouping": "fire"})


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--decks", action="store_true")
    ap.add_argument("--fit", action="store_true")
    ap.add_argument("--swap", action="store_true")
    ap.add_argument("--order", action="store_true")
    ap.add_argument("--best", metavar="MODEL",
                    default=None, nargs="?", const="naive+max / all gates")
    ap.add_argument("--s17", action="store_true")
    ap.add_argument("--scan-t", action="store_true", help="scan the reference duration")
    ap.add_argument("--sim", nargs="?", const=os.path.join(CHARTS, "chart_m0001_easy.sus"))
    ap.add_argument("--runs", type=int, default=2000)
    args = ap.parse_args()

    levels, chars, attrs = active_levels(), card_character(), card_attribute()
    riders = special_riders()
    dks = decks()

    if args.decks:
        report_decks(dks, levels, chars, attrs, riders)
    if args.order:
        report_order(MODELS, dks, levels, chars, attrs, riders)
    if args.best:
        report_best(args.best, dks, levels, chars, attrs, riders)
    if args.swap:
        report_swap(dks, levels, chars, attrs, riders)
    if args.s17:
        report_s17(dks, levels, chars, attrs, riders)
    if args.fit:
        print(f"five decks, {len({c for d in dks for c in d['cards']})} distinct cards, "
              f"{1 << len({c for d in dks for c in d['cards']})} level assignments per model\n")
        fit(MODELS, dks, levels, chars, attrs, riders)
    if args.scan_t:
        # T only bites where the activation COUNT is quantised, so it is scanned inside the
        # winning family (max stacking, all gates) rather than the refuted additive one.
        pol = dict(next(c for n, c in MODELS if n == "naive+max / all gates"))["policy"]
        for T in [60, 80, 90, 100, 110, 120, 128.8, 140, 160, 180, 210, 240, 300]:
            ms = [(f"window+max      T={T}", _m(uptime="quant", stack="max", policy=pol, T=T)),
                  (f"window+max+rdr  T={T}", _m(uptime="quant", stack="max", policy=pol, T=T,
                                                rider="avg"))]
            fit(ms, dks, levels, chars, attrs, riders)
    if args.sim:
        # The measured team is the 686 deck (preset 03) on m0001 easy. Its non-Active Score Bonus
        # is 168.1 - 68.6 = 99.5 pp, which sets the baseline every note is multiplied by.
        deck = max(dks, key=lambda d: d["active"])
        cfg = dict(next(c for n, c in MODELS if n == "naive+max / all gates"))
        boards = {}
        td = deck_terms(deck, cfg, levels, chars, attrs, boards, riders)
        best = report_best("naive+max / all gates", dks, levels, chars, attrs, riders,
                           quiet=True)
        terms = [td[(c, best[c])] for c in deck["cards"]]
        for stack in ("max", "sum"):
            r = simulate(args.sim, terms, stack=stack, runs=args.runs, other_bonus=0.995)
            print(f"{os.path.basename(args.sim)}  deck {deck['name']} ({deck['active']}%)  "
                  f"stacking {stack}: {r['notes']} notes over {r['duration']:.1f} s, "
                  f"{r['n']} simulated runs")
            print(f"    full range max/min - 1 = {r['spread_full']:.1%};  "
                  f"expected over FOUR draws = {r['spread_4draw']:.1%};  "
                  f"P(four draws span >= 21.4 %) = {r['p_ge_21_4']:.1%}  "
                  f"(section 15 observed exactly 21.4 % on four AUTO runs)")
    if not any((args.decks, args.fit, args.swap, args.s17, args.scan_t, args.sim,
                args.order, args.best)):
        ap.print_help()


if __name__ == "__main__":
    main()
