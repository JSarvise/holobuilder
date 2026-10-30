"""Put a chart, a team's skill clocks and the chart's own SPECIAL triggers on ONE timeline.

`sus_parse.py` reads the notes. This adds the three channels it deliberately ignores, because
they are not notes:

    #mmm0B:  FIVE per chart, values 01..05 -- the SPECIAL trigger for team slot 1..5.
             702 charts x 5 = 3 510 chart-defined Special trigger timestamps, which matches
             the count another optimiser reports and cross-checks this reading.
    #mmm0C:  a start/end pair.
    #mmm0D:  a start/end pair.

0C and 0D are recorded positionally and NOT interpreted here -- naming them would be a guess,
and a guess must never read like a measurement.

ACTIVE skill clocks: the clock starts at **measure 0 of the
beatmap**, not at the first note, so the k-th activation check is at `k * cooldown`. Cooldown
reduction (Holomem Board only, 12 % ceiling) DIVIDES: `cooldown / (1 + reduction)`.

    proxyenv/Scripts/python chart_timeline.py <music_id> <difficulty> [--out out.json]
"""
import json
import os
import re
import sys
from fractions import Fraction

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import master_cache as mc  # noqa: E402
import sus_parse as sp  # noqa: E402

EVENT_CHANNELS = ("0B", "0C", "0D")
EVENT_RE = re.compile(r"^#(\d{3})(0[BCD]):\s*(\S*)$")

# A captured deck the score model was validated against, in slot order.
DEFAULT_TEAM = [
    "card-00031-5-uniq-0025-00",
    "card-04001-5-uniq-0042-00",
    "card-04003-5-uniq-0044-00",
    "card-00036-5-uniq-0029-00",
    "card-06005-5-uniq-0061-00",
]


def measure_starts(text, upto):
    """Beat at which each measure begins, honouring `#mmm02` length declarations."""
    lengths = {}
    for raw in text.splitlines():
        m = sp.BODY.match(raw.strip())
        if m and m.group(2) == "02":
            lengths[int(m.group(1))] = Fraction(m.group(3))
    starts, beat = {}, Fraction(0)
    for measure in range(upto + 1):
        starts[measure] = beat
        beat += sp._length_of(measure, lengths)
    return starts, lengths


def events(text, chart):
    """[(channel, measure, beat, time, value)] for the three non-note channels."""
    last = max(int(m.group(1)) for m in
               (sp.BODY.match(r.strip()) for r in text.splitlines()) if m)
    starts, lengths = measure_starts(text, last)
    raw = []
    for line in text.splitlines():
        m = EVENT_RE.match(line.strip())
        if not m:
            continue
        measure, chan, data = int(m.group(1)), m.group(2), m.group(3)
        cells = [data[i:i + 2] for i in range(0, len(data), 2)]
        for i, cell in enumerate(cells):
            if not cell or cell == "00":
                continue
            frac = Fraction(i, len(cells))
            beat = starts[measure] + frac * sp._length_of(measure, lengths)
            raw.append([chan, measure, beat, cell])
    holder = [sp.Note(beat, 0, "1", "1", 1, None) for _, _, beat, _ in raw]
    sp._set_times(holder, chart.bpms)
    return [
        {"channel": chan, "measure": measure, "beat": float(beat),
         "time": round(note.time, 4), "value": cell}
        for (chan, measure, beat, cell), note in zip(raw, holder)
    ]


def team_skills(card_ids, level=2):
    """Each card's ACTIVE and SPECIAL at `level`, straight from the master tables."""
    cards = {r["id"]: r["data"] for r in mc.rows("Card")}
    active, special = {}, {}
    for r in mc.rows("LiveActiveSkillLevel"):
        d = r["data"]
        active.setdefault(d[1][0], {})[d[2][0]] = d
    for r in mc.rows("LiveSpecialSkillLevel"):
        d = r["data"]
        special.setdefault(d[1][0], {})[d[2][0]] = d
    # Names come from `data/cards.json`, which is the authoritative hand-authored
    # file — read only, never written.
    names = {}
    cards_path = os.path.join(HERE, "..", "..", "..", "data", "cards.json")
    with open(os.path.abspath(cards_path), encoding="utf-8") as fh:
        doc = json.load(fh)
    for card in (doc["cards"] if isinstance(doc, dict) and "cards" in doc else doc):
        names[card["id"]] = (card.get("member"), card.get("title"), card.get("type"))

    def permil(effect):
        return float(effect.rsplit("-", 1)[-1]) / 1000 if effect else 0.0

    out = []
    for slot, cid in enumerate(card_ids, start=1):
        row = cards[cid]
        a = active[row[12][0]][level]
        s = special[row[13][0]][level]
        member, title, ctype = names.get(cid, (cid, "", ""))
        out.append({
            "slot": slot,
            "card_id": cid,
            "member": member,
            "title": title,
            "type": ctype,
            "active": {
                "base_magnitude": permil(a[4][0]),
                "conditional_magnitude": permil(a[6][0]) if 6 in a else None,
                "condition": a[5][0] if 5 in a else None,
                "cooldown": a[7][0] / 1000,
                "duration": a[10][0] / 1000,
                "probability": a[8][0] / 1000,
            },
            "special": {
                "magnitude": permil(s[3][0]),
                "duration": s[6][0] / 1000,
            },
        })
    return out


def active_windows(skill, song_seconds, reduction=0.0):
    """Every activation CHECK and the window it opens if the roll succeeds.

    The clock starts at measure 0, so check k sits at `k * cooldown`. Duration is always
    below the cooldown (duration/cooldown never reaches 1 in the game data), so a window
    can never overlap the next check of the same skill.

    ⚠ The end is deliberately **not** clamped to `song_seconds`: every consumer treats a
    window as half-open `[check, end)`, and `song_seconds` is `max(note.time)` — the timestamp
    of the chart's LAST note. Clamping would set `end == song_seconds` for any skill still live
    at the end of the chart, and `t < end` would then judge that last note **outside** the
    window that in fact covers it. One silently dropped note per such window, and it is the
    note carrying the highest combo multiplier: worth 0.4 % of the level-1 prediction on
    `m0001` easy. A window running past the end of the chart simply covers no further notes,
    so leaving it long costs nothing. `src/hbuilder/domain/song.py::_active_windows` follows
    the same rule."""
    cooldown = skill["cooldown"] / (1.0 + reduction)
    out, k = [], 1
    while k * cooldown <= song_seconds:
        start = k * cooldown
        out.append({"check": round(start, 3),
                    "end": round(start + skill["duration"], 3)})
        k += 1
    return out


def resolve_exclusive(lanes):
    """Only the strongest effect applies where several overlap.

    Takes `[(slot, magnitude, [(start, end), ...])]` and returns the winning slot on every
    interval, by cutting at every boundary and taking the largest magnitude live there. Ties go
    to the lower slot, so the output is deterministic."""
    edges = sorted({t for _s, _m, ws in lanes for w in ws for t in w})
    out = []
    for lo, hi in zip(edges, edges[1:]):
        mid = (lo + hi) / 2
        live = [(m, s) for s, m, ws in lanes if any(a <= mid < b for a, b in ws)]
        if not live:
            continue
        magnitude, slot = max(live, key=lambda ms: (ms[0], -ms[1]))
        contested = len(live) > 1
        # Merge only when the NEIGHBOURING slice agrees on both the winner and
        # whether it was contested — collapsing a contested slice into an
        # uncontested one would mislabel the very thing this chart is drawn to
        # show.
        if (out and out[-1]["slot"] == slot and out[-1]["contested"] == contested
                and abs(out[-1]["end"] - lo) < 1e-9):
            out[-1]["end"] = round(hi, 3)
            continue
        out.append({"start": round(lo, 3), "end": round(hi, 3),
                    "slot": slot, "magnitude": magnitude,
                    "contested": len(live) > 1})
    return out


def main():
    music, difficulty = sys.argv[1], sys.argv[2]
    path = os.path.join(HERE, "dumps", "charts_cdn", f"chart_{music}_{difficulty}.sus")
    text = open(path, encoding="utf-8").read()
    chart = sp.parse(text)
    scored = sp.classify(chart)
    song = max(n.time for _k, n in scored)

    team = team_skills(DEFAULT_TEAM)

    # `--board` applies each character's OWN Holomem Board timing nodes. They are
    # per-character, so they do not merely lengthen uptime — they shift each
    # skill's checks off every other skill's, which under the strongest-only rule
    # is a team-level effect no per-card metric can see.
    if "--board" in sys.argv:
        from board_active_mods import modifiers
        cards = {r["id"]: r["data"] for r in mc.rows("Card")}
        mods = modifiers([cards[e["card_id"]][2][0] for e in team])
        for entry in team:
            chr_id = cards[entry["card_id"]][2][0]
            m = mods[chr_id]
            entry["board"] = {"character": chr_id, "captured": m["captured"],
                              "cooldown_reduction": m["cooldown"],
                              "probability_bonus": m["probability"]}
            entry["active"]["cooldown_effective"] = round(
                entry["active"]["cooldown"] / (1.0 + m["cooldown"]), 4)
    evts = events(text, chart)
    specials = sorted([e for e in evts if e["channel"] == "0B"], key=lambda e: e["time"])

    for entry in team:
        entry["active"]["windows"] = active_windows(
            entry["active"], song,
            reduction=entry.get("board", {}).get("cooldown_reduction", 0.0))
    for entry, ev in zip(team, specials):
        entry["special"]["fires_at"] = ev["time"]
        entry["special"]["ends_at"] = round(ev["time"] + entry["special"]["duration"], 3)

    def lanes_for(key):
        return [
            (e["slot"], e["active"][key] or e["active"]["base_magnitude"],
             [(w["check"], w["end"]) for w in e["active"]["windows"]])
            for e in team
        ]

    # Two readings, both shown rather than one picked. `base` is what the deck
    # screen displays (the panel does NOT apply the conditional upgrade even
    # when its condition holds). `upgraded`
    # is what an in-run expected value should use where the condition does hold
    # — for this team every condition does: 3 Cute and 2 Pure among the five,
    # life above 600 in normal play, and combo past 40 after the opening bars.
    lanes = lanes_for("base_magnitude")
    lanes_up = lanes_for("conditional_magnitude")

    out = {
        "music_id": music,
        "difficulty": difficulty,
        "title": "Shiny Smily Story (2022 ver.)" if music == "m0001" else music,
        "header": dict(chart.header),
        "bpm": chart.bpms[0][1] if chart.bpms else None,
        "song_seconds": round(song, 3),
        "notes": [{"t": round(n.time, 4), "kind": k} for k, n in scored],
        "note_tally": dict(sp.tally(chart)),
        "events": evts,
        "team": team,
        "effective": resolve_exclusive(lanes),
        "effective_upgraded": resolve_exclusive(lanes_up),
    }
    dest = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
    text_out = json.dumps(out, ensure_ascii=False, indent=1)
    if dest:
        open(dest, "w", encoding="utf-8").write(text_out)
        print(f"wrote {dest}  ({len(out['notes'])} notes, {song:.1f}s, "
              f"{len(specials)} special triggers)")
    else:
        print(text_out)


if __name__ == "__main__":
    main()
