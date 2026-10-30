"""Build the chart library `src/` scores against, from caches already on disk.

`domain/song.py` scores a team on a concrete chart; this module builds those
charts, so the per-song scorer has a production input the CLI can reach.

**Nothing new is extracted here.** Two caches already hold everything:

* `dumps/lattice_charts.json` — 704 charts, `{t, w, cues, f}`, built by
  `active_lattice.py --charts`. Times are **seconds** and cues are seconds;
  the beat→second conversion happened in `sus_parse._set_times` upstream.
  `f` lists the indices of the FLICK notes — the one kind whose
  PERFECT weight depends on the client (1050 mobile / 1000 PC).
* `dumps/music_coef.json` — `{chart_name: (difficulty_level, permil)}` from
  master `MusicDifficulty` field 3 and `Music` field 17.

The one real computation is recovering `perfect_permil` from the cache's `w`.
The cache stores the pre-multiplied weight

    w = perfect_permil x (1 + ComboCoefficient(i + 1))

and under the full-perfect premise the combo at note `i` IS `i + 1`, so the
permil divides out exactly. `score_distribution.build_song_chart` does the same
recovery; the arithmetic is identical on purpose, and `--verify` below checks
this file against `score_distribution`'s own reading rather than trusting that.

Usage
-----
    python build_charts.py                       # EXPERT only -> dumps/charts.json
    python build_charts.py --difficulty ALL      # every difficulty
    python build_charts.py --all-out dumps/charts_all.json   # both, in one pass
    python build_charts.py --verify              # round-trip through the loader

EXPERT is the default because the ranking mode is **always Expert**, and
because all four difficulties is 4x the file for charts no mode reads.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DUMPS = os.path.join(HERE, "dumps")
CHART_CACHE = os.path.join(DUMPS, "lattice_charts.json")
MUSIC_CACHE = os.path.join(DUMPS, "music_coef.json")
OUT = os.path.join(DUMPS, "charts.json")

sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "src"))
from hbuilder.domain.song import (  # noqa: E402
    LIVE_COMBO_TIERS,
    ChartLibrary,
    charts_from_mapping,
)

# charts-2: each note is `[time, permil, flick]` — `permil` the PC-column
# base recovered below and `flick` 0/1 from the lattice cache's `"f"` index list, so the
# scorer can weigh a FLICK at 1050 (mobile, the default) or 1000 (PC) at run time
# (`ScoringConfig.platform`). No platform is baked into this file. charts-1 files
# (`[time, permil]`) still load and read as flick-less.
SCHEMA_VERSION = "charts-2"


def combo_coefficient(combo: int) -> float:
    """`LIVE_COMBO_TIERS` read the way `CalcScore` reads it — highest tier
    reached. Imported from the domain, not re-typed, so the two cannot drift."""
    out = 0.0
    for threshold, permil in LIVE_COMBO_TIERS:
        if combo >= threshold:
            out = permil / 1000.0
    return out


MUSIC_MASTER = os.path.join(DUMPS, "holodori", "main_Music.json")

_SINGER_TYPE = {
    "MusicSingerType_MUSIC_SINGER_TYPE_SOLO": "solo",
    "MusicSingerType_MUSIC_SINGER_TYPE_GROUP": "group",
    "MusicSingerType_MUSIC_SINGER_TYPE_ALL": "all",
}


def _casts() -> tuple[dict[str, tuple[list[str], str]], list[str]]:
    """`music_id -> (singers, scope)` from master `Music`, plus what was refused.

    `characterIds` is the song's cast and `musicSingerType` its scope. Over
    182 rows: 128 SOLO, 40 GROUP, 14 ALL.

    ⚠ Two shapes are REPORTED rather than guessed at, because `SongChart`
    refuses both and inventing a reading to get past it would be a silent
    guess:

      * **14 ALL songs carry an EMPTY `characterIds`** — the whole roster is
        implied. `SongChart` requires a non-empty cast whenever a scope is set,
        and materialising "the roster" here would be this tool deciding what the
        roster is. Left absent; the `all` bucket is already pooled by
        `_board_bonus` and is leader-invariant anyway.
      * **4 SOLO songs name TWO singers** — the FUWAMOCO duo, whose "solo" songs
        are the pair's. These ARE populated: the client's
        `CalculateCurrentEffectValue` sums over the song's own singers, filtered
        by `musicSingerType_`, under ONE cap, so a duo `solo` correctly sums both
        members' `solo`-scope nodes and clamps once — exactly what
        `song_score_bonus_for` does. `SongChart` admits a 1- or 2-singer solo.
        ⚠ Not yet checked against a server capture with duo solo nodes bought;
        it rests on the decompiled aggregation. 3+ singers stays a mislabel guard.

    Everything else is populated exactly.
    """
    if not os.path.exists(MUSIC_MASTER):
        return {}, [f"{os.path.basename(MUSIC_MASTER)}: absent, no song casts populated"]

    with open(MUSIC_MASTER, encoding="utf-8") as fh:
        raw = json.load(fh)
    rows = raw if isinstance(raw, list) else (raw.get("rows") or list(raw.values())[0])

    casts: dict[str, tuple[list[str], str]] = {}
    refused: list[str] = []
    for row in rows:
        data = row.get("data") or {}
        scope = _SINGER_TYPE.get(data.get("musicSingerType") or "")
        singers = [str(c) for c in (data.get("characterIds") or [])]
        music_id = str(row.get("id") or "")
        if scope is None:
            refused.append(f"{music_id}: unknown musicSingerType "
                           f"{data.get('musicSingerType')!r}")
            continue
        if not singers:
            refused.append(f"{music_id}: singer_type {scope!r} with an empty cast")
            continue
        if scope == "solo" and not 1 <= len(set(singers)) <= 2:
            refused.append(f"{music_id}: singer_type 'solo' names {len(singers)} "
                           f"singers {singers} (admits 1, or 2 for a duo)")
            continue
        casts[music_id] = (singers, scope)
    # The Music master export can lag a release: a song present only in songs.json (from
    # Master/Get) would get no cast, and its singer's yellow / singer reds / Score Bonus
    # scope would never fire. Fill the songs it lacks from songs.json, under the same checks.
    songs_path = os.path.join(os.path.dirname(os.path.dirname(MUSIC_MASTER)), "songs.json")
    try:
        with open(songs_path, encoding="utf-8") as fh:
            ours = json.load(fh)
    except (OSError, ValueError):
        ours = {}
    ours = ours.get("songs", ours) if isinstance(ours, dict) else ours
    for song in (ours.values() if isinstance(ours, dict) else ours or ()):
        music_id = str((song or {}).get("id") or "")
        if not music_id or music_id in casts:
            continue
        singer = song.get("singer") or {}
        scope = str(singer.get("type") or "").lower() or None
        singers = [str(c) for c in (singer.get("character_ids") or [])]
        if scope in ("solo", "group", "all") and singers and (
                scope != "solo" or 1 <= len(set(singers)) <= 2):
            casts[music_id] = (singers, scope)
    return casts, refused


def build(difficulty: str = "EXPERT") -> dict:
    if not os.path.exists(CHART_CACHE):
        raise SystemExit(
            f"{CHART_CACHE} missing — run `python active_lattice.py --charts 1` "
            f"once to build it (a few minutes), then re-run this."
        )
    if not os.path.exists(MUSIC_CACHE):
        raise SystemExit(f"{MUSIC_CACHE} missing — run score_distribution.py once.")

    with open(CHART_CACHE, encoding="utf-8") as fh:
        cache = json.load(fh)
    with open(MUSIC_CACHE, encoding="utf-8") as fh:
        music = json.load(fh)

    casts, cast_skipped = _casts()

    wanted = difficulty.upper()
    charts: dict[str, dict] = {}
    skipped: list[str] = list(cast_skipped)
    for name in sorted(cache):
        # `chart_m0001_expert` -> ("m0001", "EXPERT")
        parts = name.split("_")
        if len(parts) < 3:
            skipped.append(f"{name}: unparseable name")
            continue
        music_id, chart_difficulty = parts[1], parts[2].upper()
        if wanted != "ALL" and chart_difficulty != wanted:
            continue
        if name not in music:
            # Named, never dropped silently: a chart with no master row would
            # otherwise vanish from the library and take its song with it.
            skipped.append(f"{name}: absent from music_coef.json")
            continue

        raw = cache[name]
        level, permil = music[name]
        if "f" not in raw:
            # A lattice cache from before the flick index list existed: refusing
            # is the honest failure — a flick-less chart would silently score
            # every flick at the PC weight on the mobile default.
            raise SystemExit(
                f"{CHART_CACHE} predates the per-note flick flag ({name} has no 'f'); "
                f"re-run `python build_charts.py --rebuild-caches` (or delete the cache "
                f"and run `active_lattice.py --charts 1`)."
            )
        flick_at = set(int(i) for i in raw["f"])
        notes = [
            [float(time), int(round(weight / (1.0 + combo_coefficient(index + 1)))),
             1 if index in flick_at else 0]
            for index, (time, weight) in enumerate(zip(raw["t"], raw["w"]))
        ]
        if not notes:
            skipped.append(f"{name}: no notes")
            continue
        record = {
            "music_id": music_id,
            "difficulty": chart_difficulty,
            "difficulty_level": int(level),
            "live_score_coefficient_permil": int(permil),
            "notes": notes,
            "special_cues": [float(cue) for cue in raw.get("cues", ())],
        }
        cast = casts.get(music_id)
        if cast is not None:
            record["singers"], record["singer_type"] = cast
        charts[ChartLibrary.key(music_id, chart_difficulty)] = record

    return {
        "schema_version": SCHEMA_VERSION,
        "source": "lattice_charts.json + music_coef.json",
        "difficulty": wanted,
        "skipped": skipped,
        "charts": charts,
    }


def rebuild_caches_if_stale() -> bool:
    """Rebuild the two parsed-chart caches when charts_cdn has outgrown them.

    `active_lattice.build_chart_cache` rebuilds only when its file is absent, so
    twenty new .sus files leave the solver scoring the old set while every counter
    says OK (refresh_all names this; here it is acted on). The four `m9999`
    tutorial difficulties are skipped by design, so the cache is expected to sit
    exactly four below the .sus count. `music_coef.json` is rebuilt from the local
    master (score_distribution.load_music_coefficients) whenever the lattice is,
    since a new song is new in both. Returns True when a rebuild happened."""
    sus_dir = os.path.join(DUMPS, "charts_cdn")
    if not os.path.isdir(sus_dir):
        print("  charts_cdn/ missing -- nothing to rebuild")
        return False
    on_disk = sum(1 for n in os.listdir(sus_dir) if n.endswith(".sus"))
    expected = on_disk - 4
    parsed = -1
    if os.path.exists(CHART_CACHE):
        try:
            with open(CHART_CACHE, encoding="utf-8") as fh:
                cached = json.load(fh)
            parsed = len(cached)
            if any("f" not in rec for rec in cached.values()):
                print("  lattice cache STALE: records lack the per-note flick list 'f'")
                parsed = -1
        except Exception:  # noqa: BLE001 -- unreadable counts as stale
            parsed = -1
    if parsed >= expected:
        print(f"  lattice cache current: {parsed} parsed vs {on_disk} .sus (-4 tutorial)")
        # The two caches are rebuilt together but they can FAIL apart, and then the
        # lattice check alone declares everything current forever: a rebuild that writes
        # the lattice and dies on the master read before music_coef leaves every later run
        # seeing "lattice current", skipping the rebuild, and `build()` stopping with
        # "music_coef.json missing". Rebuild the missing half on its own rather than
        # requiring the lattice to go stale again.
        if not os.path.exists(MUSIC_CACHE):
            print("  music_coef.json MISSING while the lattice is current -- rebuilding it")
            sys.path.insert(0, HERE)
            import score_distribution  # noqa: PLC0415
            coef = score_distribution.load_music_coefficients()
            print(f"  rebuilt: music_coef {len(coef)} rows")
            return True
        return False
    print(f"  lattice cache STALE: {parsed} parsed vs {expected} expected -- rebuilding "
          f"({on_disk} .sus, a few minutes)")
    for path in (CHART_CACHE, MUSIC_CACHE):
        if os.path.exists(path):
            os.replace(path, path + ".stale")
    sys.path.insert(0, HERE)
    import active_lattice  # noqa: PLC0415
    import score_distribution  # noqa: PLC0415
    cache = active_lattice.build_chart_cache()
    with open(CHART_CACHE, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)
    coef = score_distribution.load_music_coefficients()   # rebuilds + writes when absent
    print(f"  rebuilt: lattice {len(cache)} charts, music_coef {len(coef)} rows")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="build_charts.py")
    parser.add_argument("--difficulty", default="EXPERT",
                        help="EASY/NORMAL/HARD/EXPERT, or ALL (default EXPERT)")
    parser.add_argument("--out", default=OUT)
    parser.add_argument("--all-out", default=None,
                        help="ALSO write every difficulty to this path (Path B's absolute "
                             "per-song scorer needs EASY/NORMAL/HARD; the ranking library "
                             "stays EXPERT-only so its digest and size do not move). "
                             "Kept in the same command so the two files cannot drift.")
    parser.add_argument("--verify", action="store_true",
                        help="read the file back through domain.charts_from_mapping")
    parser.add_argument("--rebuild-caches", action="store_true",
                        help="if charts_cdn holds .sus files the lattice cache has not parsed, "
                             "rebuild dumps/lattice_charts.json (active_lattice.build_chart_cache) "
                             "and dumps/music_coef.json (from the local master) first")
    args = parser.parse_args(argv)

    if args.rebuild_caches:
        rebuild_caches_if_stale()

    document = build(args.difficulty)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(document, fh, separators=(",", ":"))

    charts = document["charts"]
    notes = sum(len(c["notes"]) for c in charts.values())
    flicks = sum(1 for c in charts.values() for n in c["notes"] if n[2])
    with_flicks = sum(1 for c in charts.values() if any(n[2] for n in c["notes"]))
    size = os.path.getsize(args.out) / 1e6
    print(f"{len(charts)} charts, {notes:,} notes ({flicks:,} flicks on {with_flicks} charts) "
          f"-> {args.out} ({size:.1f} MB)")
    for line in document["skipped"]:
        print(f"  SKIPPED {line}")

    if args.all_out and args.all_out != args.out:
        every = build("ALL")
        with open(args.all_out, "w", encoding="utf-8") as fh:
            json.dump(every, fh, separators=(",", ":"))
        # The check that keeps the two files honest: the all-difficulty build must
        # reproduce the ranking library EXACTLY on the difficulty it also covers.
        # If it ever does not, one of the two is scoring a different chart.
        shared = {k: v for k, v in every["charts"].items() if k in charts}
        agree = sum(1 for k, v in shared.items() if v == charts[k])
        print(f"{len(every['charts'])} charts (every difficulty) -> {args.all_out} "
              f"({os.path.getsize(args.all_out) / 1e6:.1f} MB); "
              f"{agree}/{len(charts)} identical to {args.out}")
        if agree != len(charts):
            print("  ⚠ the two chart files DISAGREE on a shared chart — do not use either")
            return 1

    if args.verify:
        library = charts_from_mapping(document, source=args.out)
        cues = sum(1 for c in library.charts.values() if len(c.special_cues) == 5)
        print(f"  loader accepts {len(library)} charts; "
              f"{cues} carry the expected 5 SPECIAL cues")
        for key, chart in library.charts.items():
            if len(chart.special_cues) != 5:
                print(f"  ⚠ {key}: {len(chart.special_cues)} cues, not 5")

        # The check that matters: an INDEPENDENT reading of the same caches.
        # `score_distribution.build_song_chart` recovers the permil the same
        # way for its own purposes, so if the two agree the recovery is not
        # merely self-consistent.
        import score_distribution as sd

        cache, music = sd.load_charts(), sd.load_music_coefficients()
        compared = mismatched = 0
        for name in sorted(cache):
            parts = name.split("_")
            if len(parts) < 3 or name not in music:
                continue
            if wanted := document["difficulty"]:
                if wanted != "ALL" and parts[2].upper() != wanted:
                    continue
            reference = sd.build_song_chart(name, cache[name], music)
            mine = library.get(reference.music_id, reference.difficulty)
            compared += 1
            # `score_distribution` reads (time, PC-base permil) and no flick
            # flag, so the flag is compared separately against the cache's own
            # index list rather than against a reader that never had it.
            same_notes = mine is not None and (
                [(n.time, n.perfect_permil) for n in mine.notes]
                == [(n.time, n.perfect_permil) for n in reference.notes]
                and [i for i, n in enumerate(mine.notes) if n.flick]
                == sorted(int(i) for i in cache[name].get("f", ()))
            )
            if mine is None or (
                not same_notes
                or mine.special_cues != reference.special_cues
                or mine.difficulty_level != reference.difficulty_level
                or mine.live_score_coefficient_permil
                != reference.live_score_coefficient_permil
            ):
                mismatched += 1
                print(f"  ⚠ {name}: disagrees with score_distribution's reading")
        print(f"  cross-checked {compared} charts against "
              f"score_distribution.build_song_chart: {mismatched} mismatches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
