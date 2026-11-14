"""The refresh's step table: every step's name, what it writes, whether it needs the
network, and the argv that runs it (`Step`, `STEPS`, the `cmd_*` builders and the master
tables they name). `refresh_all.py` runs the table, in the order `STEPS` lists it."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))  # this checkout's hbuilder first
from hbuilder import paths  # noqa: E402

HERE = paths.REFRESH_DIR
DUMPS = paths.DUMPS
MASTER_REPO = paths.MASTER_REPO


@dataclass
class Step:
    name: str
    what: str
    network: bool
    probes: list[str]
    build: Callable[[argparse.Namespace], tuple[list[str], dict[str, str], Path]]
    # A soft step's failure is printed as a WARN and never fails the refresh: a
    # convenience artefact (an icon) must not turn the daily command red.
    soft: bool = False


def _pyexe(args: argparse.Namespace) -> str:
    return args.python or sys.executable


def cmd_master(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    script = MASTER_REPO / "scripts" / "run-local.ps1"
    cmd = [
        "powershell",
        "-ExecutionPolicy",
        "Bypass",
        "-NoProfile",
        "-File",
        str(script),
    ]
    env = {"FORCE_RUN": "1"} if args.force else {}
    return cmd, env, MASTER_REPO


def cmd_master_index(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    # No `--account`: Master/Get declares `disabled_game_auth_token`, so the script takes no
    # credential (and argparse would reject the flag).
    return [_pyexe(args), str(HERE / "master_index_live.py")], {}, HERE


def cmd_event_bonus(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "marathon_bonus.py")], {}, HERE


def cmd_inventory(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "inventory_snapshot.py")], {}, HERE


def cmd_holomem_rating(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "holomem_rating.py")], {}, HERE


def cmd_account(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    cmd = [
        _pyexe(args),
        str(HERE / "account_snapshot.py"),
        "--account",
        args.account,
        "--snapshot",
        "--refresh-card-params",
        "--refresh-board-state",
        "--diff",
    ]
    # The desktop app gets the open event from the publisher, and
    # ListEventInfo is the call a not-yet-reset game day refuses (DATE_CHANGED).
    if not getattr(args, "account_only", False):
        cmd.insert(-1, "--refresh-events")
    if args.merge_collection:
        cmd.append("--merge-collection")
    return cmd, {}, HERE


def cmd_cards(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "build_cards_dataset.py")], {}, HERE


def cmd_level_curve(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "card_level_curve.py")], {}, HERE


def cmd_caps(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "card_caps.py")], {}, HERE


# The master tables the runtime reads as dumps/holodori/main_<T>.json, written from the local
# master by master_json.py (the directory name only reflects the export format it mirrors).
# Writing them locally means they never lag the game.
LEVEL_TABLES = ("Card", "CardLevel", "LiveNote", "SkillTreeNode", "CharacterLevel")


# the open event's title (event_art.py), written with the level tables so that the FIRST refresh's
# event_art step has them: song_meta runs after it
EVENT_TITLE_TABLES = ("Poster", "LangPoster_Eng", "LangEventStoryChapter_Eng")


SONG_META_TABLES = (
    "Music",
    "LangMusic_Eng",
    "Poster",
    # read by the web/app
    "Setting",
    "LangItem_Eng",
    "LangMarathonRankingGrade_Eng",
    "MarathonRankingGrade",
    "LiveDeckEvaluationRank",
    "LiveScoreEvaluationRank",
    "SkillTreeEffectValueLimit",
    "Character",
    "CharacterGrouping",
    "SdCostume",
)


def cmd_level_tables(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "master_json.py"), *LEVEL_TABLES, *EVENT_TITLE_TABLES], {}, HERE


def cmd_import(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "import_to_cards.py")], {}, HERE


def cmd_add_cards(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    cmd = [_pyexe(args), str(HERE / "card_append.py")]
    if args.dry_run_adds:
        cmd.append("--dry-run")
    return cmd, {}, HERE


def cmd_catalog(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "octo_catalog.py")], {}, HERE


def cmd_charts(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "chart_download.py")], {}, HERE


def cmd_songs(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "build_songs.py")], {}, HERE


def cmd_song_meta(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    # Casts (Music), titles (LangMusic_Eng) and Poster, whose rows `maxed_account_bonuses`
    # counts as every poster the game has shipped.
    return [_pyexe(args), str(HERE / "master_json.py"), *SONG_META_TABLES], {}, HERE


def cmd_park_moves(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "park_scan.py"), "--write-moves"], {}, HERE


def cmd_chart_lib(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return (
        [
            _pyexe(args),
            str(HERE / "build_charts.py"),
            "--rebuild-caches",
            "--all-out",
            str(DUMPS / "charts_all.json"),
            "--verify",
        ],
        {},
        HERE,
    )


def cmd_jackets(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "song_jackets.py"), "--sync"], {}, HERE


def cmd_card_art(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "card_art_sync.py")], {}, HERE


def cmd_event_art(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    # Exits 0 whatever is missing: a banner the CDN will not serve is a printed line
    # and a null title/art on the Event page, never a failed refresh.
    return [_pyexe(args), str(HERE / "event_art.py")], {}, HERE


def cmd_event_cutoffs(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    # Every region's tier borders (guests) plus the account's own rank in its region. An ended
    # chapter the server still lists is read once more for its finals.
    own = ["--region", "own"] if getattr(args, "account_only", False) else []
    return [_pyexe(args), str(HERE / "event_cutoffs.py"), "--self", "--songs", *own], {}, HERE


def cmd_event_emblems(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    # after event_history: it names the chapters on record (each one's exact medal)
    return [_pyexe(args), str(HERE / "event_emblems.py")], {}, HERE


def cmd_event_history(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    # Offline: folds event_meta (archived before the event leaves the list), the cutoff store
    # (the account's rank, Event Pt, final borders) and the emblems held into event_history.json.
    return [_pyexe(args), str(HERE / "event_history.py")], {}, HERE


def cmd_item_icons(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    # Reads the client's own Octo cache; downloads nothing.
    return [_pyexe(args), str(HERE / "item_icons.py")], {}, HERE


def cmd_ui_assets(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "ui_assets.py")], {}, HERE


def cmd_node_master(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "master_export.py"), "SkillTreeNode", "SkillTreeEffect"], {}, HERE


def cmd_skilltree(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "build_skilltree.py")], {}, HERE


def cmd_member_icons(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "member_icons.py")], {}, HERE


def cmd_skill_icons(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "skill_icons.py")], {}, HERE


def cmd_park_export(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "park_export.py")], {}, HERE


def cmd_board(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    cmd = [
        _pyexe(args),
        str(HERE / "build_board.py"),
        "--connect-level",
        args.connect_level,
    ]
    return cmd, {}, HERE


def cmd_board_export(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "board_export.py")], {}, HERE


def cmd_live2d(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    # The leaders' Live2D figures: every outfit of the catalogue not converted yet, owned or
    # not ("Maxed account" and the pulls page lead with any card). Built models are skipped;
    # a new outfit is ~4.5 MB.
    return [_pyexe(args), str(HERE / "live2d_build.py"), "--all"], {}, HERE


def cmd_chibi(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    # The Optimizer's progress-bar runners: only the SdCostume outfits with no model yet.
    return [_pyexe(args), str(HERE / "chibi_build.py"), "--new"], {}, HERE


# What the desktop app refreshes (`--account-only`): the steps that read the account itself,
# and the two boards built from its board state. Everything else is shared content the
# publisher's full refresh builds and tools/app/publish.py serves.
# event_cutoffs reads only the account's own region there (`--region own`): the app has no guest
# accounts, and the borders come from the tracker; what only this account can read is its own
# rank and Event Pt (without this step the app's Event page shows no points after a refresh).
ACCOUNT_STEPS = (
    "account",
    "inventory",
    "holomem_rating",
    "board",
    "board_export",
    "event_cutoffs",
    "event_history",
)


# Steps that only bring pictures: nothing the optimizer scores reads them, and the pages draw
# without them (a missing picture is a 404). On a first refresh they are most of the time
# (chibis ~50 min and card art / jackets / Live2D from the CDN without a game cache), so
# `--pictures-later` runs everything else first and these in the background afterwards.
PICTURE_STEPS = ("card_art", "event_emblems", "jackets", "live2d", "chibi", "park_export")


STEPS: list[Step] = [
    Step(
        "master",
        "hololive-dreams-master dump (fetch + decrypt + commit + push)",
        network=True,
        probes=["master_tables"],
        build=cmd_master,
    ),
    Step(
        "master_index",
        "dumps/master_index.json from a live Master/Get",
        network=True,
        probes=["master_index"],
        build=cmd_master_index,
    ),
    # The account step turns each card's exp into a level through Card + CardLevel. A card
    # newer than those tables would merge with no level, and `power_up_permyriad: "auto"`
    # sums every owned card's level, so it would move every deck's Member Power-Up. After
    # master_index, so a table the game just changed is read at its new seed.
    Step(
        "level_tables",
        "Card + CardLevel (exp -> level) + LiveNote + SkillTreeNode from our master",
        network=True,
        probes=[],
        build=cmd_level_tables,
    ),
    Step(
        "account",
        "Auth/Login -> User/Get -> card_params.json + board_state.json",
        network=True,
        probes=["snapshot", "card_params", "board_state", "collection"],
        build=cmd_account,
    ),
    Step(
        "event_bonus",
        "dumps/marathon_bonus.json -- the open event's bonus tables (Event/ListEventInfo)",
        network=False,
        probes=["event_bonus"],
        build=cmd_event_bonus,
    ),
    Step(
        "inventory",
        "dumps/inventory.json -- every item balance, incl. the 8 board currencies",
        network=True,
        probes=["inventory"],
        build=cmd_inventory,
    ),
    # The Optimizer's Holomem Rating goal reads it (your records, songs and rank); nothing
    # else does, so a failure is a warning, never a failed refresh.
    Step(
        "holomem_rating",
        "dumps/holomem_rating.json -- songs owned, highest-score records, rank per holomem",
        network=True,
        probes=[],
        build=cmd_holomem_rating,
        soft=True,
    ),
    Step(
        "cards",
        "dumps/cards_enriched.json from the masters + card_params",
        network=False,
        probes=["cards_enriched"],
        build=cmd_cards,
    ),
    # Two more files read off the masters. If they fall behind the game, newer
    # cards cannot be restated at their real level and the account basis scores
    # them at their MAXED stats -- a wrong ranking, with nothing saying so.
    Step(
        "level_curve",
        "dumps/card_level_curve.json -- per-level stat multipliers and each card's group",
        network=False,
        probes=[],
        build=cmd_level_curve,
    ),
    Step(
        "caps",
        "dumps/card_caps.json -- level / bloom / limit-break caps by rarity",
        network=False,
        probes=[],
        build=cmd_caps,
    ),
    Step(
        "import",
        "dumps/cards_import_draft.json -- the schema-valid proposal",
        network=False,
        probes=["cards_json"],
        build=cmd_import,
    ),
    Step(
        "add_cards",
        "APPEND-ONLY: add cards the game ships and data/cards.json lacks",
        network=False,
        probes=["cards_json"],
        build=cmd_add_cards,
    ),
    # `catalog` precedes everything that asks the Octo CDN for an asset by name: otherwise
    # new cards' art is looked up in the previous catalogue ("NOT IN CAT").
    Step(
        "catalog",
        "dumps/octocacheevai_prod.dec from the client's own install (no network)",
        network=False,
        probes=[],
        build=cmd_catalog,
    ),
    # Item/resource thumbs for the Resources view. Needs `catalog` (md5 -> asset
    # name). Local only, and soft: a missing cache or a bad bundle is a WARN line.
    Step(
        "item_icons",
        "dumps/item_icons/ -- item + resource thumbs from the local Octo cache "
        "(no network; never fails the refresh)",
        network=False,
        probes=[],
        build=cmd_item_icons,
        soft=True,
    ),
    # The card chrome and the official UI sprites the pages draw (ui_assets.json says which);
    # after item_icons, whose thumbs it copies. Local install only, soft.
    Step(
        "ui_assets",
        "dumps/card_chrome/ + dumps/ui_official/ -- the game's own UI sprites the pages draw "
        "(local install, no network; never fails the refresh)",
        network=False,
        probes=[],
        build=cmd_ui_assets,
        soft=True,
    ),
    Step(
        "card_art",
        "portrait + square WEBP for cards the art dir lacks (Octo CDN by name, 1 req/s)",
        network=True,
        probes=["card_art"],
        build=cmd_card_art,
    ),
    # The open events' title (master Poster name), kind, times and banner/logo art.
    # Needs `account` (event_info_live.bin), `master` (the Poster tables) and
    # `catalog` (asset names) before it.
    Step(
        "event_art",
        "dumps/event_meta.json + dumps/event_art/ -- event title, kind, times, banner "
        "(Octo CDN by name, 1 req/s; never fails the refresh)",
        network=True,
        probes=[],
        build=cmd_event_art,
    ),
    Step(
        "event_cutoffs",
        "dumps/cutoffs/<region>/ -- every region's tier borders + the account's own rank "
        "(appended; an ended chapter read once more for its finals; never fails the refresh)",
        network=True,
        probes=[],
        build=cmd_event_cutoffs,
        soft=True,
    ),
    Step(
        "event_history",
        "dumps/event_history.json -- every event seen, the account's result and the final borders "
        "(additive: an event once seen is never dropped; offline, never fails the refresh)",
        network=False,
        probes=[],
        build=cmd_event_history,
        soft=True,
    ),
    Step(
        "event_emblems",
        "dumps/event_art/emblem-<chapter>-<grade>.webp -- every chapter's ranking medal as the game "
        "draws it (Octo CDN, new ones only; published with the event art; never fails the refresh)",
        network=True,
        probes=[],
        build=cmd_event_emblems,
        soft=True,
    ),
    # The song chain is three steps because each can go stale silently: a stale catalogue
    # does not name a new song's charts, so they are not downloaded, so songs.json keeps the
    # old count.
    Step(
        "charts",
        "download .sus charts the catalogue names and disk lacks (1 req/s, md5-verified)",
        network=True,
        probes=[],
        build=cmd_charts,
    ),
    Step(
        "songs",
        "dumps/songs.json from the masters + the charts on disk",
        network=False,
        probes=["songs"],
        build=cmd_songs,
    ),
    # After songs.json a new song still needs its cast + title (without a cast every singer
    # node is inert on it), the parsed-chart caches the solver scores from, and its jacket.
    Step(
        "song_meta",
        "Music (casts) + LangMusic_Eng (titles) + the other runtime tables from our master",
        network=True,
        probes=["song_casts", "song_titles"],
        build=cmd_song_meta,
    ),
    # The Park scan's quest data (park_scan.py --write-moves): the master's AREA_MOVE quest steps
    # and its areas, with the hashes of the tables they come from; the scan stops when the live
    # master no longer carries them. In park_export/, so the app gets it with the maps. Soft.
    Step(
        "park_moves",
        "dumps/park_export/park_moves.json -- the Park scan's quest data, from our master",
        network=True,
        probes=[],
        build=cmd_park_moves,
        soft=True,
    ),
    Step(
        "chart_lib",
        "lattice/music_coef caches if stale, then dumps/charts.json + "
        "dumps/charts_all.json (every difficulty, --verify)",
        network=False,
        probes=["chart_library"],
        build=cmd_chart_lib,
    ),
    Step(
        "jackets",
        "song jackets the catalogue names and disk lacks (Octo CDN, 1 req/s) + render",
        network=True,
        probes=["jackets"],
        build=cmd_jackets,
    ),
    # The node table and the Connect effects build_board resolves sockets against (a stale
    # file has no effect for a newer socketed card and `board` fails). Read by build_board,
    # tree_optimise, the CLI and the web server.
    Step(
        "node_master",
        "dumps/master_SkillTreeNode.json + master_SkillTreeEffect.json from the masters",
        network=False,
        probes=[],
        build=cmd_node_master,
    ),
    Step(
        "skilltree",
        "dumps/skilltree.json (board graph + card Connect effects) from the masters",
        network=False,
        probes=[],
        build=cmd_skilltree,
    ),
    # The web's pictures (holomem faces, board glyphs), so a clean checkout gets them without a
    # hand run. Local Octo cache, no network; soft, like item_icons.
    Step(
        "member_icons",
        "dumps/member_icons/ -- the round holomem faces (/chricon, the board export) from the "
        "local Octo cache (never fails the refresh)",
        network=False,
        probes=[],
        build=cmd_member_icons,
        soft=True,
    ),
    Step(
        "skill_icons",
        "dumps/skill_icons/ -- the board node glyphs (/icon) from the local Octo cache, plus "
        "what octo_fetch pulled (never fails the refresh)",
        network=False,
        probes=[],
        build=cmd_skill_icons,
        soft=True,
    ),
    Step(
        "board",
        "dumps/board.json from board_state.json",
        network=False,
        probes=["board"],
        build=cmd_board,
    ),
    Step(
        "board_export",
        "dumps/board_export.json -- the web frontend's board (nodes, costs, "
        "sockets, points.granted). MUST follow `board`.",
        network=False,
        probes=["board_export"],
        build=cmd_board_export,
    ),
    Step(
        "live2d",
        "dumps/live2d/ -- the leaders' Live2D figures (every new outfit of the game)",
        network=True,
        probes=[],
        build=cmd_live2d,
    ),
    Step(
        "chibi",
        "dumps/chibi/ -- the Optimizer's running chibis (new SD outfits of the game only)",
        network=True,
        probes=[],
        build=cmd_chibi,
    ),
    # Parkview's raw maps + every spot's pixel position (park_export.py). The map bundles come
    # from the Octo CDN once, then the export is offline. Soft.
    Step(
        "park_export",
        "dumps/park_export/ -- Parkview's maps and spot positions (Octo CDN once; never fails the refresh)",
        network=True,
        probes=[],
        build=cmd_park_export,
        soft=True,
    ),
]


STEP_NAMES = [s.name for s in STEPS]
