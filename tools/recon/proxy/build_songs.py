"""Assemble everything known about every song into one file: dumps/songs.json.

WHY THIS EXISTS. Song data spans ten master tables, three enums, an unlock chain through
`ParkQuestLive`, and four asset families across two directories. A Songs view should not have
to re-derive any of that. This joins it once and writes a single document.

WHAT IT IS NOT. Generated, and regenerable from master data in about a second — so it lands in
`dumps/`, not `data/`. `data/cards.json` and `data/owned.json` remain the hand-edited authorities
and nothing here touches them.

SOURCES (all local; no network):
  Music, LangMusic_Eng, MusicDifficulty, MusicDifficultyChart, MusicMv,
  Character, LangCharacter_Eng, ParkQuestLive
  plus on-disk presence checks for jackets, audio and charts.

  proxyenv/Scripts/python build_songs.py [--out PATH] [--quiet]
"""
import json, os, re, sys, collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import master_cache

DUMPS = os.path.join(HERE, "dumps")
OUT = os.path.join(DUMPS, "songs.json")
JACKETS = os.path.join(DUMPS, "song_jackets")
FETCHED = os.path.join(DUMPS, "octo_fetched")
CHARTS = os.path.join(DUMPS, "charts_cdn")
WAVS = os.path.join(DUMPS, "song_wav")

# Game enums (see holodori_sync.enum_values)
RELEASE = {1: "PRESET", 2: "SHOP", 3: "REWARD"}
CATEGORY = {1: "ORIGINAL", 2: "COVER"}
SINGER = {1: "SOLO", 2: "GROUP", 3: "ALL"}
DIFFICULTY = {1: "EASY", 2: "NORMAL", 3: "HARD", 4: "EXPERT"}


def iso_utc(ms):
    """Epoch milliseconds -> ISO 8601 UTC. Music.start_time is the release moment."""
    if not ms:
        return None
    import datetime
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat()


def rel(path):
    """Repo-relative, forward slashes — these paths get consumed by a web view."""
    return os.path.relpath(path, HERE).replace(os.sep, "/") if os.path.exists(path) else None


def g(data, field, default=None):
    """One value of a protobuf field, or `default`. Keys are ints, not strings."""
    vs = data.get(field) or data.get(str(field)) or []
    return vs[0] if vs else default


def gl(data, field):
    """Every value of a repeated field."""
    return list(data.get(field) or data.get(str(field)) or [])


_CATALOG_AUDIO = {}


def catalog_audio():
    """{music id: [asset names]} for music_short_*, straight from the production catalog.

    Read here rather than via octo_fetch, which imports `requests` at module level and so
    cannot be imported under proxyenv."""
    if _CATALOG_AUDIO:
        return _CATALOG_AUDIO
    from master_db import decode_record
    path = os.path.join(DUMPS, "octocacheevai_prod.dec")
    if not os.path.exists(path):
        return _CATALOG_AUDIO
    with open(path, "rb") as fh:
        top = decode_record(fh.read())
    for blob in list(top.get(2, [])) + list(top.get(3, [])):
        name = decode_record(blob).get(2, [b""])[0].decode()
        if name.startswith("music_short_"):
            _CATALOG_AUDIO.setdefault(name[len("music_short_"):].rsplit(".", 1)[0], []).append(name)
    return _CATALOG_AUDIO


def _lang(table):
    """id -> English text (field 1000). Needs master_cache's Lang table-name fix."""
    return {r["id"]: g(r["data"], 1000, "") for r in master_cache.rows(table)}


def raw_rows(table):
    """Rows with values still as BYTES.

    `master_cache.rows` runs every value through `.decode("utf-8", "replace")`, which is right
    for text but destroys an embedded message: the `.common.Reward` in field 1000 of the reward
    tables is a nested protobuf, and replacement characters make it unparseable. Reward decoding
    therefore has to come through here."""
    from master_db import decode_record
    db = master_cache.open_table(table)
    try:
        return [decode_record(r["data"])
                for r in db.query(f"SELECT * FROM {master_cache.sql_table(db, table)}")]
    finally:
        db.close()


def _reward(blob):
    """`.common.Reward` -> {resource_type, resource_id, quantity}.

    Field 1 is the resource type, 2 the resource id (absent for types that need
    none, e.g. 1001 = the Red Diamond currency), 3 the quantity."""
    from master_db import decode_record
    r = decode_record(blob)
    rid = r.get(2, [None])[0]
    return {"resource_type": r.get(1, [None])[0],
            "resource_id": rid.decode("utf-8", "replace") if isinstance(rid, bytes) else rid,
            "quantity": r.get(3, [None])[0]}


# LiveEvaluationRankType, as the game defines it
RANK = {1: "D", 2: "C", 3: "B", 4: "A", 5: "S"}


def global_rewards():
    """The four reward tables. They are GLOBAL -- 4 rows each, keyed by threshold, not by song.

    Only the thresholds they are compared against are per-song, and those live in
    `MusicDifficultyChart.max_combo_count_reward_threshold` and the chart note counts."""
    out = {}
    for key, table, label in (
            ("score_rank", "LiveMusicHighestScoreEvaluationRankReward", "evaluation_rank_type"),
            ("play_count", "LiveMusicClearCountReward", "clear_count"),
            ("max_combo", "LiveMusicDifficultyMaxComboCountReward", "difficulty_type"),
            ("full_combo", "LiveMusicFullComboReward", "difficulty_type")):
        rows = []
        for d in raw_rows(table):
            n = d.get(1, [None])[0]
            entry = {label: RANK.get(n, n) if label == "evaluation_rank_type"
                     else DIFFICULTY.get(n, n) if label == "difficulty_type" else n}
            if 1000 in d:
                entry["reward"] = _reward(d[1000][0])
            rows.append(entry)
        out[key] = rows
    return out


def streaming_muted_assets():
    """Asset ids the client mutes in streamer mode (72 rows)."""
    return {g(r["data"], 1) for r in master_cache.rows("AssetStreamingMute")}


def build():
    music = master_cache.rows("Music")
    lm = _lang("LangMusic_Eng")
    lc = _lang("LangCharacter_Eng")
    chars = {g(r["data"], 1): lc.get(g(r["data"], 2, ""), "") for r in master_cache.rows("Character")}

    diffs = collections.defaultdict(list)
    for r in master_cache.rows("MusicDifficulty"):
        diffs[g(r["data"], 1)].append(r["data"])
    charts = {}
    for r in master_cache.rows("MusicDifficultyChart"):
        charts[(g(r["data"], 1), g(r["data"], 2))] = r["data"]

    mvs = collections.defaultdict(list)
    for r in master_cache.rows("MusicMv"):
        mvs[g(r["data"], 1)].append(r["data"])
    quests = collections.defaultdict(list)
    for r in master_cache.rows("ParkQuestLive"):
        quests[g(r["data"], 2)].append(r["data"])
    muted = streaming_muted_assets()

    songs = []
    for r in sorted(music, key=lambda x: g(x["data"], 999) or 0):
        d = r["data"]
        mid = g(d, 1)
        cids = gl(d, 101)
        release = RELEASE.get(g(d, 24), f"UNKNOWN({g(d, 24)})")

        # --- unlock
        unlock = {"type": release,
                  "requirement_text": lm.get(g(d, 26, ""), "") or None,
                  "release_item_quantity": g(d, 25),
                  "quest_ids": [g(q, 1) for q in quests.get(mid, [])] or None}

        # --- difficulties, joined to their chart counts and the .sus on disk
        levels = []
        for dd in sorted(diffs.get(mid, []), key=lambda x: g(x, 2) or 0):
            dt = g(dd, 2)
            asset = g(dd, 4, "")
            ch = charts.get((mid, dt), {})
            sus = os.path.join(CHARTS, asset + ".sus")
            levels.append({
                "difficulty": DIFFICULTY.get(dt, f"UNKNOWN({dt})"),
                "level": g(dd, 3),
                "chart_asset_id": asset,
                "full_combo_note_count": g(ch, 3),
                "normal_note_count": g(ch, 6),
                "chart_hash": g(ch, 5),
                # what the "N combo" reward threshold is measured against, per difficulty
                "max_combo_reward_threshold": g(ch, 4),
                "chart_file": rel(sus),
            })

        # --- assets on disk. `expected` comes from the catalog, so "complete" is a real
        # claim: most songs keep their waveform in a .awb and only some carry it inline,
        # so "has an .acb" would call songs done that have no audio at all.
        expected = catalog_audio().get(mid, [])
        jacket_name = f"img_music_jacket_{g(d, 13, mid)}"
        webp = os.path.join(JACKETS, jacket_name + ".webp")
        acb = os.path.join(FETCHED, f"music_short_{mid}.acb.bundle")
        awb = os.path.join(FETCHED, f"music_short_{mid}.awb.bundle")
        songs.append({
            "id": mid,
            "title": lm.get(g(d, 2, ""), ""),
            "title_ruby": lm.get(g(d, 3, ""), ""),
            "credits": {"lyricist": lm.get(g(d, 7, ""), ""),
                        "composer": lm.get(g(d, 9, ""), ""),
                        "arranger": lm.get(g(d, 11, ""), "")},
            "singer": {
                "type": SINGER.get(g(d, 100), f"UNKNOWN({g(d, 100)})"),
                "display_name": lm.get(g(d, 102, ""), "") or None,
                "character_ids": cids,
                # SINGER_TYPE_ALL leaves character_ids empty on purpose -- it means everyone,
                # not nobody. Callers must not render "no performer".
                "characters": [{"id": c, "name": chars.get(c, "")} for c in cids],
            },
            "length_seconds": g(d, 15),
            "released_at_ms": g(d, 14),
            "released_at_utc": iso_utc(g(d, 14)),
            # the client mutes these in streamer mode -- licensed tracks, mostly
            "streaming_muted": f"music_short_{mid}" in muted,
            "chorus": {"start_ms": g(d, 104), "end_ms": g(d, 105)},
            "category": CATEGORY.get(g(d, 16), f"UNKNOWN({g(d, 16)})"),
            "copyright": g(d, 31),
            "mv_url": g(d, 29),
            "mv_count": len(mvs.get(mid, [])) or 0,
            "live_score_coefficient_permil": g(d, 17),
            "is_tutorial": bool(g(d, 28)),
            "unlock": unlock,
            "difficulties": levels,
            "assets": {
                "jacket_asset_id": jacket_name,
                "jacket_webp": rel(webp),
                "audio_acb": rel(acb),
                "audio_awb": rel(awb),
                "audio_files_expected": expected,
                "audio_complete": bool(expected) and all(
                    os.path.exists(os.path.join(FETCHED, n + ".bundle")) for n in expected),
                "audio_wav": rel(os.path.join(WAVS, mid + ".wav")),
            },
        })
    return songs


def coverage(songs):
    """What fraction of each field actually landed. Printed so gaps cannot hide."""
    n = len(songs)
    have = lambda f: sum(1 for s in songs if f(s))
    rows = [
        ("title", have(lambda s: s["title"])),
        # arranger is empty on every COVER song and present on every ORIGINAL one --
        # that is how the game credits covers, not a gap here.
        ("credits: lyricist", have(lambda s: s["credits"]["lyricist"])),
        ("credits: composer", have(lambda s: s["credits"]["composer"])),
        ("credits: arranger (ORIGINAL only)",
         have(lambda s: s["credits"]["arranger"] or s["category"] == "COVER")),
        ("length_seconds", have(lambda s: s["length_seconds"])),
        ("mv_url", have(lambda s: s["mv_url"])),
        ("copyright", have(lambda s: s["copyright"])),
        ("4 difficulties", have(lambda s: len(s["difficulties"]) == 4)),
        ("every chart .sus on disk", have(lambda s: s["difficulties"]
                                          and all(d["chart_file"] for d in s["difficulties"]))),
        ("note counts on all 4", have(lambda s: s["difficulties"]
                                      and all(d["full_combo_note_count"] for d in s["difficulties"]))),
        ("jacket rendered", have(lambda s: s["assets"]["jacket_webp"])),
        ("audio complete (all its files)", have(lambda s: s["assets"]["audio_complete"])),
        ("audio decoded to wav", have(lambda s: s["assets"]["audio_wav"])),
        ("released_at", have(lambda s: s["released_at_utc"])),
        ("max_combo thresholds on all 4",
         have(lambda s: s["difficulties"] and all(d["max_combo_reward_threshold"]
                                                  for d in s["difficulties"]))),
        ("named singers", have(lambda s: s["singer"]["characters"])),
        ("singer type ALL", have(lambda s: s["singer"]["type"] == "ALL")),
        ("unlock requirement text", have(lambda s: s["unlock"]["requirement_text"])),
        ("quest ids", have(lambda s: s["unlock"]["quest_ids"])),
    ]
    print(f"\ncoverage over {n} songs")
    for label, k in rows:
        flag = "" if k == n else "   <- not all"
        print(f"  {label:28} {k:>3}/{n}{flag}")
    by = collections.Counter(s["unlock"]["type"] for s in songs)
    print(f"  unlock types: {dict(by)}")


def main():
    argv = sys.argv[1:]
    out = OUT
    if "--out" in argv:
        out = argv[argv.index("--out") + 1]
    songs = build()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"rewards": global_rewards(), "songs": songs}, fh,
                  ensure_ascii=False, indent=1)
    print(f"{len(songs)} songs -> {out}  ({os.path.getsize(out) / 1024:.0f} KB)")
    if "--quiet" not in argv:
        coverage(songs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
