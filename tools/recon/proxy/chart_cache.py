# Find and read the game's chart files. No download, no decryption, no catalog.
#
# MusicDifficultyChart's field 5 is the md5 of the chart file, and the game's Octo cache is
# content-addressed by md5 -- so that md5 IS the filename inside <install>\octo\v1. The files are
# plain UTF-8 SUS (the open text chart format) and were never obfuscated: do NOT run them through
# octo_crypto, that destroys them. Their raw bytes start with `#CREATE_APP_`.
#
# Only charts the client has actually downloaded are present; it fetches one when you play that
# difficulty, so the cache fills as you play rather than all at once.
#
#   proxyenv/Scripts/python chart_cache.py                 -> coverage report
#   proxyenv/Scripts/python chart_cache.py --copy          -> copy every cached chart to dumps/charts/
#   proxyenv/Scripts/python chart_cache.py --show m0524 4  -> print one chart's header
import os, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import master_cache as mc

CACHE = os.path.expandvars(r"%ProgramFiles(x86)%\Steam\steamapps\common\hololiveDreams\octo\v1")
OUT = os.path.join(HERE, "dumps", "charts")
DIFF = {1: "easy", 2: "normal", 3: "hard", 4: "expert"}


def index():
    """md5 -> (music_id, difficulty, total_notes, flick_notes_unknown_until_read)."""
    return {r["data"][5][0]: (r["music_id"], int(r["difficulty_type"]), r["data"][3][0])
            for r in mc.rows("MusicDifficultyChart")}


def cached(want):
    """md5 -> path, for every wanted chart the client has downloaded."""
    found = {}
    for root, _, files in os.walk(CACHE):
        for f in files:
            if f in want:
                found[f] = os.path.join(root, f)
    return found


def main():
    want = index()
    have = cached(want)
    songs_all = {v[0] for v in want.values()}
    songs_have = {want[m][0] for m in have}
    print(f"{len(have)}/{len(want)} charts cached  ({len(songs_have)}/{len(songs_all)} songs)")

    if "--show" in sys.argv:
        mid = sys.argv[sys.argv.index("--show") + 1]
        d = int(sys.argv[sys.argv.index("--show") + 2])
        for md5, (m, dd, _) in want.items():
            if m == mid and dd == d:
                if md5 not in have:
                    raise SystemExit(f"{mid} {DIFF[d]} is not in the cache — play it first")
                text = open(have[md5], encoding="utf-8", errors="replace").read()
                print("\n".join(l for l in text.splitlines() if l.startswith("#") and " " in l))
                return
        raise SystemExit(f"no such chart: {mid} difficulty {d}")

    if "--copy" in sys.argv:
        os.makedirs(OUT, exist_ok=True)
        for md5, path in have.items():
            m, d, _ = want[md5]
            dst = os.path.join(OUT, f"{m}_{DIFF.get(d, d)}.sus")
            if not open(path, "rb").read(32).startswith((b"#CREATE_APP_", b"#VISION_SUS_FORMAT_VERSION")):
                print(f"  skipped {os.path.basename(dst)}: not plaintext SUS")
                continue
            shutil.copy(path, dst)
        print(f"copied {len(os.listdir(OUT))} charts -> {OUT}")
        return

    by_song = {}
    for md5 in have:
        m, d, _ = want[md5]
        by_song.setdefault(m, []).append(DIFF.get(d, d))
    for m, ds in sorted(by_song.items()):
        print(f"   {m}  {', '.join(sorted(ds))}")


if __name__ == "__main__":
    main()
