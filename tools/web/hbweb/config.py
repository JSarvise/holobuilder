"""Every file the web server reads, and the one JSON reader.

Each route reads these as `config.X` when it runs, never as a copy taken at import, so a
test that points one of them at a fixture points every route at it.

The write prohibition: nothing under `tools/web/` may write `data/cards.json` or
`data/owned.json`. Their two constants here only ever reach read sinks
(`hbuilder.inputs.load_inputs`, `read_json`), and `tools/web/test_no_writes.py` walks the
package's AST to prove it. `COLLECTION_PATH` is the one file the package writes (the
collection editor's save, `collection.save_collection`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hbuilder import paths
from hbuilder.inputs import (
    COLLECTION_CAPS_PATH,
    COLLECTION_CURVE_PATH,
    DEFAULT_BOARD_PATH,
    DEFAULT_CHARTS_PATH,
)

REPO_ROOT = paths.REPO
REFRESH_DIR = paths.REFRESH_DIR
DUMPS = paths.DUMPS

# ---------------------------------------------------------------------------
# The catalogue and the account. CARDS_PATH and OWNED_PATH are read-only.
# ---------------------------------------------------------------------------
CARDS_PATH = REPO_ROOT / "data" / "cards.json"
OWNED_PATH = REPO_ROOT / "data" / "owned.json"
# The ONE path anything under `tools/web/` may write. `state/` is not under the write
# prohibition (that names `data/cards.json` and `data/owned.json`), but `test_no_writes.py`
# still pins every filesystem mutation in the package to exactly this name.
COLLECTION_PATH = paths.STATE / "collection.json"
# The per-level skill rungs the solver scores from (`restate_card_skills`), so the
# collection editor shows each skill at the level a card's bloom bought.
LADDER_PATH = DUMPS / "skill_ladder.json"
# The account's captured boards (a repo-relative path, as `load_board_basis` takes it).
BOARD_PATH = DEFAULT_BOARD_PATH
# True ignores the captured board on the routes that would score it.
BOARD_DISABLED = False
# Every holomem's board as `User/Get` has it ({chr: {nodes, slots}}).
BOARD_STATE_PATH = DUMPS / "board_state.json"
# The collection editor's bounds: the level caps per rarity and the per-level multiplier curve.
CURVE_PATH = REPO_ROOT / COLLECTION_CURVE_PATH
CAPS_PATH = REPO_ROOT / COLLECTION_CAPS_PATH
# The board editor's inputs: the node table + the four model geometries, and the master
# rows the point costs (field 9) come from. Absent files give an editor that says so.
SKILLTREE_PATH = DUMPS / "skilltree.json"
NODE_MASTER_PATH = DUMPS / "master_SkillTreeNode.json"
# `{chr-id: unspent skill-tree points}` (`tree_budget.unspent_from_capture`): the editor's
# real budget rather than the 453 of a full board.
TREE_POINTS_PATH = DUMPS / "tree_points.json"
# `cards_enriched.json`: the game's own English outfit-skill text per card.
CARDS_ENRICHED_PATH = DUMPS / "cards_enriched.json"
# The account's items (`user_item_list`) and its snapshot (unspent board points).
INVENTORY_PATH = DUMPS / "inventory.json"
SNAPSHOT_PATH = DUMPS / "account_snapshot_main.json"
# The game's own English item names: the holodori.best export, then the live Master/Get
# dump, which also names every item the game added since that export.
LANG_ITEM_PATH = paths.HOLODORI / "main_LangItem_Eng.json"
LIVE_LANG_ITEM_PATH = paths.MASTER_REPO / "data" / "tables" / "LangItem_Eng.json"
# `CharacterGrouping` fields 100/101 per generation (`gen_groups.py`): the card ribbon is a
# horizontal gradient between them, 100 at the left edge and 101 at the right.
GEN_GROUPS_PATH = DUMPS / "gen_groups.json"

# ---------------------------------------------------------------------------
# Songs. The charts are what a score is computed from; the titles and the Music master are
# display and filter labels only, so a missing label file can never change a number.
# ---------------------------------------------------------------------------
CHARTS_PATH = REPO_ROOT / DEFAULT_CHARTS_PATH
# Every difficulty (`build_charts.py --all-out`): EASY / NORMAL / HARD charts live only
# here; EXPERT is read from charts.json.
CHARTS_ALL_PATH = CHARTS_PATH.with_name("charts_all.json")
SONG_TITLES_PATH = paths.HOLODORI / "main_LangMusic_Eng.json"
# The songs' master rows: singer type, cast, cover/original, playing time.
MUSIC_PATH = paths.HOLODORI / "main_Music.json"

# ---------------------------------------------------------------------------
# Events.
# ---------------------------------------------------------------------------
# The newest open event's bonus rows (every refresh writes them).
EVENT_BONUS_PATH = DUMPS / "marathon_bonus.json"
# The event's name, kind, times and banner (`event_art.py`).
EVENT_META_PATH = DUMPS / "event_meta.json"
EVENT_ART_DIR = DUMPS / "event_art"
# Every event seen + the account's result (`event_history.py`).
EVENT_HISTORY_PATH = DUMPS / "event_history.json"
# The ranking-border store dumps/cutoffs/<region>/<chapter>.jsonl, appended to only by
# `event_cutoffs.py` / `tracker_pull.py`.
CUTOFFS_STORE_DIR = DUMPS / "cutoffs"
CUTOFFS_SYNC_SCRIPT = REFRESH_DIR / "event_cutoffs.py"
CUTOFFS_PULL_SCRIPT = REFRESH_DIR / "tracker_pull.py"
CUTOFFS_GRADE_PATH = paths.HOLODORI / "main_MarathonRankingGrade.json"
CUTOFFS_GRADE_LANG_PATH = paths.HOLODORI / "main_LangMarathonRankingGrade_Eng.json"
# The 24/7 tracker on the VPS, reached over SSH (`tracker_remote.json`) or over HTTPS
# through api.holobuilder.dev (`server_api.json`, which also carries the admin key).
CUTOFFS_REMOTE_CONFIG = paths.STATE / "tracker_remote.json"
SERVER_API_PATH = paths.STATE / "server_api.json"

# ---------------------------------------------------------------------------
# Account-side scripts the routes run as subprocesses (they own their own writes).
# ---------------------------------------------------------------------------
REFRESH_SCRIPT = REFRESH_DIR / "refresh_all.py"
# The gacha store is data/gacha/*.jsonl, appended to only by `gacha_sync.py`; the export
# (`gacha_export.py`) writes its HTML file itself and reports it on a `WROTE` line.
GACHA_STORE_DIR = REPO_ROOT / "data" / "gacha"
GACHA_ART_DIR = GACHA_STORE_DIR / "art"
GACHA_SYNC_SCRIPT = REFRESH_DIR / "gacha_sync.py"
GACHA_EXPORT_SCRIPT = REFRESH_DIR / "gacha_export.py"
# The credential the live Park read spends: checked before the read so its absence is a
# named state rather than the `MissingInput` of `account_snapshot.load_credential`.
ACCOUNT_CREDENTIAL_PATH = paths.STATE / "account_credential.json"
# The Park export (`park_export.py`): the maps and where every named spot lands on them.
PARK_EXPORT_DIR = DUMPS / "park_export"
PARK_SPOTS_PATH = PARK_EXPORT_DIR / "park_spots.json"
# today's pick, written by the account refresh (account_snapshot.write_park_state)
PARK_STATE_PATH = DUMPS / "park_state.json"

# ---------------------------------------------------------------------------
# Pages and assets.
# ---------------------------------------------------------------------------
STATIC_DIR = paths.WEB_DIR / "static"
# The card portraits (`<card-id>.webp`, gitignored), served from where they live;
# `--art-dir` points at another checkout's copy.
DEFAULT_ART_DIR = REPO_ROOT / "tools" / "explorer" / "cards_explorer_art"
# The game's typeface, LINE Seed Sans (Latin subset, ExtraBold: the one weight the card face uses).
FONT_PATH = paths.WEB_DIR / "fonts" / "line-seed-sans-xbd-latin.woff2"
# The node glyphs (`skill_icons.py`).
ICONS_DIR = DUMPS / "skill_icons"
# The card art (`img_card_<family>_<key>.webp`), keyed by card id minus `card-`.
CARD_IMAGES_DIR = DUMPS / "card_images"
# The card's own chrome (`card_images.py`): element badge, generation ribbon, rarity band,
# star, bloom flower.
CHROME_DIR = DUMPS / "card_chrome"
# The game's official UI sprites the page shell and components draw.
UI_OFFICIAL_DIR = DUMPS / "ui_official"
# Per-character round face icons (`member_icons/<chr>.png`).
MEMBER_ICONS_DIR = DUMPS / "member_icons"
# The game's item icons (`img_item_thumb_*`) and `index.json`'s item -> asset map.
ITEM_ICONS_DIR = DUMPS / "item_icons"
# Song jackets (`song_jackets.py`).
JACKETS_DIR = DUMPS / "song_jackets"
# The leaders' Live2D figures (`live2d_build.py`; models/, motions/, expressions/, index.json)
# and the Cubism Core runtime, which each PC downloads from Live2D's own CDN.
LIVE2D_DIR = DUMPS / "live2d"
LIVE2D_CORE = DUMPS / "live2d_runtime" / "live2dcubismcore.min.js"
# The holomems' 3D chibis for the progress runner (`chibi_build.py`).
CHIBI_DIR = DUMPS / "chibi"


def read_json(path: Path) -> Any | None:
    """A JSON file, or `None` when it is absent. Read mode only: `test_no_writes.py`
    names this reader as one of the package's read sinks."""
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
