"""What the desktop app gets from api.holobuilder.dev, and how.

Two kinds, by how the app reads them:

  sync  -- downloaded to disk before the app runs. The solver opens these directly in its hot
           loop (charts, cards, ladder, master exports), and the pages list them (jackets,
           icons), so they cannot arrive on demand.
  lazy  -- fetched the first time a page asks for one, through the app's local server. Live2D
           figures, chibis and card art: ~920 MB in the dumps, ~3 MB per figure viewed.

NOT here, on purpose:
  - the account: account_snapshot_main.json, board_state.json, card_params.json,
    inventory.json, board.json, board_export.json, two_stage/account_* -- the app's own
    account refresh writes them on the PC that holds the credential. The server never sees it.
  - cutoffs/: the 24/7 tracker serves them itself (/tracker/*).
  - research captures, decompilation output, and the 888 MB of song jackets that sit next
    to the 24 node glyphs the board actually draws in skill_icons.py's output.

Paths are relative to the repo root, '/'-separated. Patterns are fnmatch globs where '*' also
crosses '/', so 'dumps/live2d/models/*' takes every file under it.
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path

D = "tools/recon/proxy/dumps/"

SYNC: list[str] = [
    # solver and pages
    D + "charts.json", D + "charts_all.json", D + "cards_enriched.json", D + "songs.json",
    D + "skill_ladder.json", D + "skilltree.json", D + "master_SkillTreeNode.json",
    D + "master_index.json", D + "card_level_curve.json", D + "card_caps.json",
    D + "music_coef.json", D + "lattice_charts.json",
    # the game client's version (publish.py reads the publisher's install): the app's login
    # sends it, having no install to read it from
    D + "client_version.json",
    # the game's card catalogue: a card shipped after the app's release arrives with the data.
    # The publisher's copy is the append-only one; the app's is a
    # mirror of it, and the publisher never syncs (content_store.PUBLISHER_MARK).
    "data/cards.json",
    # the open event (global, not the account's): bonus tables, names, past events
    # the event archive, not event_history.json: that one holds the publisher's own results
    D + "marathon_bonus.json", D + "event_meta.json", D + "event_archive.json",
    D + "event_finals.json",
    D + "event_art/*",
    # the master exports the runtime reads by name
    *(D + f"holodori/main_{t}.json" for t in (
        "Card", "CardLevel", "LangItem_Eng", "LangMarathonRankingGrade_Eng", "LangMusic_Eng",
        "Live", "LiveDeckEvaluationRank", "LiveNote", "LiveScoreEvaluationRank",
        "MarathonRankingGrade", "Music", "Poster", "Setting", "SkillTreeEffectValueLimit",
        "SkillTreeNode")),
    # two-stage screens on the maxed bases (the account basis is built locally)
    D + "two_stage/song_*.json", D + "two_stage/accountmax_*.json",
    # small art every page draws, and the indexes the pages list
    D + "card_chrome/*", D + "ui_official/*", D + "member_icons/*", D + "item_icons/*",
    D + "song_jackets/*", D + "park_export/*",
    D + "skill_icons/index.json", D + "skill_icons/img_skill_icon_*",
    D + "skill_icons/mdl_map*", D + "skill_icons/img_card_vert_*",
    # (not the Cubism Core: each PC downloads it from Live2D itself, server._live2d_core)
    D + "live2d/index.json", D + "chibi/index.json",
]

LAZY: list[str] = [
    D + "live2d/models/*", D + "live2d/motions/*", D + "live2d/expressions/*",
    D + "chibi/models/*",
    D + "card_images/*",
    "tools/explorer/cards_explorer_art/*",
    # the gacha banners' art (global, not the account's): the app cannot extract it, having
    # no game install for the Octo catalogue (without this the Gacha tab shows no banner
    # art). The pulls and banners logged next to it stay the account's own
    "data/gacha/art/*",
]

# Never published, whatever a pattern above matches: the account, and files being written.
EXCLUDE: list[str] = [
    "*.tmp", "*.bak", "*.stale", "*.log", "*.err", "*.out", "*/.*",
    D + "two_stage/account_*",
]


def _match(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(rel, p) for p in patterns)


def kind_of(rel: str) -> str | None:
    """'sync', 'lazy' or None for one repo-relative path."""
    if _match(rel, EXCLUDE):
        return None
    if _match(rel, SYNC):
        return "sync"
    if _match(rel, LAZY):
        return "lazy"
    return None


def _roots(patterns: list[str]) -> set[str]:
    """The fixed directory prefix of each pattern: where to start walking."""
    out = set()
    for p in patterns:
        if "*" not in p:
            out.add(p)                                   # one file: no walk at all
        else:
            out.add(p.split("*", 1)[0].rsplit("/", 1)[0])
    return out


def iter_content(repo: Path):
    """Yield (rel, kind, path) for every file the app gets, sorted by rel."""
    found: dict[str, tuple[str, Path]] = {}
    for root in sorted(_roots(SYNC + LAZY)):
        base = repo / root
        if base.is_file():
            walk = [(str(base.parent), [], [base.name])]
        elif base.is_dir():
            walk = os.walk(base)
        else:
            continue
        for dirpath, _dirs, files in walk:
            for name in files:
                p = Path(dirpath) / name
                rel = p.relative_to(repo).as_posix()
                k = kind_of(rel)
                if k and rel not in found:
                    found[rel] = (k, p)
    for rel in sorted(found):
        yield rel, found[rel][0], found[rel][1]
