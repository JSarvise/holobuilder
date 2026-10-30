"""Package the account state + the measured oracle corpus into a shareable research bundle.

WHY: the deck-screen Score Bonus is reverse-engineered to the point where one residual remains. The
fastest way to close it is to let other people brute-force over the same data, so this writes a
self-contained, English-language folder that can be zipped and shared. Everything in it is either the
game's own catalogue, the account's own state, or measurements taken from that account.

WHAT GOES IN (relevant scoring state only)
  account/collection.json        every owned card with level / bloom / limit-break, plus player level,
                                 poster count and the power-up permyriad
  account/board_state.json       the drawn Holomem Board nodes and connect slots per character
  catalogue/cards.json           the game's card catalogue as this project models it
  catalogue/master_skills.json   the decoded master rows for the skill tables the panel depends on
  measurements/oracle_decks.jsonl  every SaveDeck->GetDeck response: the deck and the server's own
                                 twelve-number Unit Score decomposition. This is the valuable part.
  docs/                          the formulas, and the open problem stated as a maths problem
  README.md                      what all of it is, and what would help

WHAT IS DELIBERATELY LEFT OUT
  Credentials, tokens, device identifiers, the account's user id and display name, deck names, friend
  data, and the raw `account_snapshot_main.json` (which carries deck names). Song audio and charts are
  excluded too: 30 of the 188 songs carry a third-party `Music.copyright` line, and that is someone
  else's call to make, not this project's.

USAGE (from the repo root, proxyenv python)
  proxyenv/Scripts/python tools/recon/proxy/export_research_bundle.py            # -> dumps/research_bundle/
  proxyenv/Scripts/python tools/recon/proxy/export_research_bundle.py --zip      # also writes the .zip
"""
import argparse
import json
import os
import shutil
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
DUMPS = os.path.join(HERE, "dumps")
OUT = os.path.join(DUMPS, "research_bundle")

# The master tables the deck-screen panel is a function of.
SKILL_TABLES = ("LiveActiveSkillLevel", "LivePassiveSkillLevel", "LiveSpecialSkillLevel",
                "LiveLeaderSkill", "Costume")
# Keys never copied out of a deck row: they carry account identity, not scoring state.
DROP_DECK_KEYS = ("deck_name", "name", "user_id", "player_name", "nickname", "friend_code")


def _decode(v):
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else v


def master_skills():
    """The decoded master rows for the skill tables, as plain JSON."""
    import master_cache as mc
    out = {}
    for table in SKILL_TABLES:
        rows = []
        try:
            for r in mc.rows(table):
                row = {k: _decode(v) for k, v in r.items() if k != "data"}
                row["data"] = {str(k): [_decode(x) for x in v] for k, v in r.get("data", {}).items()}
                rows.append(row)
        except Exception as exc:                     # a table missing from the cache is not fatal
            out[table + "__error"] = str(exc)
        out[table] = rows
    return out


def oracle_decks():
    """Every oracle response on disk, with account-identifying fields stripped."""
    rows, seen = [], set()
    for name in ("bonus_extract_db.jsonl", "bonus_recapture_db.jsonl"):
        path = os.path.join(DUMPS, name)
        if not os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            key = (r.get("leader"), r.get("costume"), r.get("music_id"), tuple(r.get("members", [])))
            if key in seen:
                continue
            seen.add(key)
            rows.append({k: v for k, v in r.items()
                         if k not in DROP_DECK_KEYS and not k.startswith("_")})
    return rows


README = """# hololive DREAMS — deck-screen Score Bonus research bundle

Everything here was produced from one player's own account and the game's own catalogue, to reverse-engineer
the **"Unit Score Details"** panel that the deck screen shows. The panel is server-computed, so each saved
deck is a fully-labelled example: the inputs (leader + costume + five members) and the game's own answer
(every power line, every bonus line, per-member power shares, per-card stats and activation floats).

## What is already solved (bit-exact, verified in exact rational arithmetic)

    windows of card i:  [k*cool_i, k*cool_i + dur_i),  k >= 1, sampled at integer seconds t = 1..200
    v(t)   = SUM_{i live at t}  mag_i * P_i / max(1000, SUM_{i live at t} P_i)      [permil]
    Active = ceil( SUM_{t=1..200} v(t) / 200 )                                       6017 / 6017 exact

    Special   = ceil( Active * SUM_i smag_i * (1 + R_i/2000) * sdur_i / 120000 )     100% (board-free)
    UnitScore = ceil( OverallPower * (1000 + ScoreBonus) * 203734 / 100000000 )      6098 / 6098 exact

`mag_i` is the HIGHEST listed magnitude (an override is resolved against the actual deck), `P_i` is the
activation probability in permil (Low 370 / Medium 460 / High 550) read from the master, and skill level 2
applies from bloom >= 1 (special skills from bloom >= 3). The panel ignores board activation-rate and
cooldown buffs on the Active line — they are booked under the Holomem Board line.

## What is NOT solved — the reason this bundle exists

Effect-ups (leader costume, passives, board) multiply each card's magnitude before the same sum:

    mag'_i = ceil( mag_i * (1000 + E_i) / 1000 )        E_i = total effect-up on card i, permil
    Active + Outfit + Passive + Board = ceil(tot) + max(N - 1, 0)      N = number of non-zero lines

That is right on **96.9%** of decks. On the rest the server returns **exactly one permil more**, and we
cannot derive when. It is deterministic, order-invariant across slots, and identical for cards with
identical parameters. The decisive measurement:

    deck A = X + Y1   X:  magnitude 600, Medium, cooldown 18, duration 7   (73 live seconds)
    deck B = X + Y2   Y1: magnitude 550, Medium, cooldown 22, duration 7   (59 live seconds)
                      Y2: magnitude 550, Medium, cooldown 28, duration 9   (59 live seconds)
                      leader costume: active-skill effect-up +50%  =>  f = 1.5
    both decks' per-second value profiles are IDENTICAL as multisets:
        52 seconds at 414,  38 at 379.5,  21 at 793.5     (sum 52612.5, A = 175.375, both Active = 176)
    server Outfit:  89  (deck A)   vs   88  (deck B)

Because the multiset of per-second values is the same, **every** model of the form "walk the timeline second
by second, combine whoever is live, average, round" returns the same number for both decks. The server does
not. So the value depends on the temporal **arrangement** of the windows, not only on who is live how often.

Families already tested and refuted against the corpus, each with a measured count: per-second, per-card,
per-block and per-activation-window rounding in any unit; a non-flat weighting over the 200 seconds (with
ceil, floor, or a floor baseline); free per-card effective magnitudes (an LP fits the Active line with zero
slack and is infeasible for the boosted line); no-stack activation rules; duration or probability boosts;
float32/float64 accumulation; ~4.8 million re-implementations of the time sum; any linear "extra" over 24
deck features; union-component, lcm/super-period, gap-variance and coincidence terms; grouped rounding per
merged live run; quantised running accumulators (provably order-invariant); banker's rounding.

**If you find the rule, that is the missing piece — everything else in the panel is bit-exact.**

## The data

| path | what |
|---|---|
| `measurements/oracle_decks.jsonl` | one row per deck: the members, and the server's own panel decomposition (`power`, `bonus`, `member_power`, `per_card`). This is the ground truth to brute-force against. |
| `account/collection.json` | the owned cards with level / bloom / limit-break, plus player level, poster count and power-up permyriad — everything needed to reproduce each card's stats and skill level. |
| `account/board_state.json` | the drawn Holomem Board nodes and connect slots per character. |
| `catalogue/cards.json` | the card catalogue as this project models it (type, generation, skills, conditions, overrides). |
| `catalogue/master_skills.json` | decoded master rows for the skill tables the panel depends on. In `LiveActiveSkillLevel`, field 4 is the effect (magnitude in permil), 5 the trigger, 6 the override effect, 7 the cooldown in ms, 8 the activation probability in permil, 10 the duration in ms. |
| `docs/` | the full formula reference and the open problem stated as a mathematics problem. |

`bonus.active / outfit / passive / board / special` are the five panel lines in permil; they always sum to
`score_bonus_permil`, and `unit_score = ceil(overall_power * (1000 + score_bonus_permil) * 203734 / 1e8)`.

## Provenance and limits

Produced by `tools/recon/proxy/export_research_bundle.py`. Credentials, tokens, device identifiers, the
account's user id and display name, and deck names are never included. Song audio and charts are excluded:
30 of the 188 songs carry a third-party copyright line, and republishing those is not this project's call.
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--zip", action="store_true", help="also write <out>.zip")
    a = ap.parse_args()

    for sub in ("account", "catalogue", "measurements", "docs"):
        os.makedirs(os.path.join(a.out, sub), exist_ok=True)

    # ---- account state (no identity, only scoring state) ----
    col = json.load(open(os.path.join(REPO, "state", "collection.json"), encoding="utf-8"))
    col.pop("_note", None)
    json.dump(col, open(os.path.join(a.out, "account", "collection.json"), "w", encoding="utf-8"),
              indent=1, ensure_ascii=False)
    board_src = os.path.join(DUMPS, "board_state.json")
    if os.path.exists(board_src):
        shutil.copyfile(board_src, os.path.join(a.out, "account", "board_state.json"))

    # ---- catalogue ----
    shutil.copyfile(os.path.join(REPO, "data", "cards.json"),
                    os.path.join(a.out, "catalogue", "cards.json"))
    json.dump(master_skills(), open(os.path.join(a.out, "catalogue", "master_skills.json"), "w",
                                    encoding="utf-8"), indent=1, ensure_ascii=False, default=str)

    # ---- measurements ----
    rows = oracle_decks()
    with open(os.path.join(a.out, "measurements", "oracle_decks.jsonl"), "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ---- docs ----
    for doc in ("DECK-SCREEN-PANEL.md", "SCORE-BONUS-BITEXACT.md", "IDENTIFICATION-METHOD.md"):
        src = os.path.join(os.path.dirname(HERE), doc)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(a.out, "docs", doc))
    open(os.path.join(a.out, "README.md"), "w", encoding="utf-8").write(README)
    open(os.path.join(a.out, "BUNDLE.json"), "w", encoding="utf-8").write(json.dumps(
        {"generated": time.strftime("%Y-%m-%d"), "oracle_decks": len(rows),
         "owned_cards": len(col.get("cards", {}))}, indent=1))

    print(f"bundle -> {a.out}")
    print(f"  oracle decks : {len(rows)}")
    print(f"  owned cards  : {len(col.get('cards', {}))}")
    if a.zip:
        zp = a.out.rstrip("\\/") + ".zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(a.out):
                for f in files:
                    p = os.path.join(root, f)
                    z.write(p, os.path.relpath(p, a.out))
        print(f"  zip          : {zp} ({os.path.getsize(zp)/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
