"""The ranking medal of every chapter on record, as the game draws it; fetched by the admin
refresh so the server can publish it.

    proxyenv/Scripts/python event_emblems.py

Each ended chapter rewards a medal (an Emblem master row
`emblem-<ranking>-<chapter id>-<grade>`), whose picture is the Octo bundle
`img_emb_<asset id>_lang-eng` (the English plate; the bare one when absent): the event's art, the
chapter's holomem and the tier's name, 1024 x 512. Written as
`dumps/event_art/emblem-<chapter id>-<grade>.webp`, 512 wide, beside the banners -- so the same
/eventart route serves them and the same content rule publishes them to the app. A chapter's
ranking decides the series: `points` -> marathon_score_ranking_rank_reward, `total` (a Score
Challenge) -> total_music_highest_score_reward. Files already on disk are never fetched again.
Never fails the refresh: a medal that cannot be had is reported and skipped.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
DUMPS = HERE / "dumps"
OUT = DUMPS / "event_art"
SERIES = {"points": "marathon_score_ranking_rank_reward", "total": "total_music_highest_score_reward"}
WIDTH = 512


def chapters() -> dict[str, str]:
    """chapter id -> its ranking ('points' | 'total'), from the history and the archive."""
    out: dict[str, str] = {}
    for name in ("event_history.json", "event_archive.json"):
        try:
            doc = json.loads((DUMPS / name).read_text("utf8"))
        except (OSError, ValueError):
            continue
        for e in (doc.get("events") or {}).values():
            for cid, ch in (e.get("chapters") or {}).items():
                out.setdefault(cid, ch.get("ranking") or "points")
    return out


def wanted(chs: dict[str, str], emblem_assets: dict[str, str], grades: list[str]) -> dict[str, str]:
    """out file name -> asset id, for every chapter x grade the master names a medal for."""
    out = {}
    for cid, ranking in sorted(chs.items()):
        series = SERIES.get(ranking, SERIES["points"])
        for g in grades:
            asset = emblem_assets.get(f"emblem-{series}-{cid}-{g}")
            if asset:
                out[f"emblem-{cid}-{g}.webp"] = asset
    return out


def main() -> int:
    from master_cache import rows  # noqa: PLC0415
    grades = [r["id"].split("-", 1)[1] for r in rows("MarathonRankingGrade")]
    assets = {r["id"]: (r["data"].get(3) or [""])[0] for r in rows("Emblem")
              if r["id"].startswith(("emblem-marathon_score", "emblem-total_music"))}
    todo = {f: a for f, a in wanted(chapters(), assets, grades).items() if not (OUT / f).is_file()}
    print(f"event emblems: {len(todo)} to fetch")
    if not todo:
        return 0
    import park_map  # noqa: PLC0415  (the Octo fetch + bundle plumbing)
    OUT.mkdir(parents=True, exist_ok=True)
    done = failed = 0
    for fname, asset in sorted(todo.items()):
        for bundle in (f"img_emb_{asset}_lang-eng", f"img_emb_{asset}"):
            try:
                park_map.ensure([bundle])
                tex = next(o.read() for o in park_map._load_bundle(bundle).objects
                           if o.type.name == "Texture2D")
            except Exception:  # noqa: BLE001 -- try the next name, then report
                continue
            im = tex.image.convert("RGBA")
            im = im.resize((WIDTH, round(im.height * WIDTH / im.width)))
            buf = io.BytesIO()
            im.save(buf, "WEBP", quality=88)
            (OUT / fname).write_bytes(buf.getvalue())
            done += 1
            break
        else:
            failed += 1
            print(f"  no medal picture for {fname[:-5]} ({asset})")
    print(f"event emblems: {done} written, {failed} without a picture -> {OUT}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 -- never fail the refresh over a medal
        print(f"event emblems: skipped ({type(exc).__name__}: {exc})")
        sys.exit(0)
