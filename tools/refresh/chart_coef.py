"""The per-chart score coefficients, read from the master, and what changed in them.

Three numbers per chart reach every note's value: the difficulty level
(`MusicDifficulty` field 3), the song's `liveScoreCoefficientPermil` (`Music` field 17) and
the chart's own factor `scoreCoefficientPermyriadMultiply` (`MusicDifficultyChart` field 7,
10000 when the row lacks it). The game can change any of them on a chart that already
exists, with no new .sus file, so the refresh reads them from the master every time and
names each change (`change_lines`).

The master's score tables can also gain a field the scorer does not know about;
`unnamed_field_lines` names every field number the committed schema lacks in them.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA = HERE / "master_schema" / "schema.json"

# `K` on a chart whose master row lacks field 7: x1.
DEFAULT_PERMYRIAD = 10000

# The master tables `CalcScore` and the chart build read.
SCORE_TABLES = ("Music", "MusicDifficulty", "MusicDifficultyChart", "LiveNote", "Setting", "LiveCombo")

# `MusicDifficulty.difficultyType` -> the chart files' difficulty name.
DIFFICULTY = {1: "EASY", 2: "NORMAL", 3: "HARD", 4: "EXPERT"}

Coef = tuple[int, int, int]  # (difficulty_level, live_score_coefficient_permil, K permyriad)


def as_coef(value: Iterable[object]) -> Coef:
    """A `music_coef.json` value: `[level, permil]` (K absent: x1) or `[level, permil, K]`."""
    parts = [int(v) for v in value]  # type: ignore[call-overload]
    if len(parts) == 2:
        return parts[0], parts[1], DEFAULT_PERMYRIAD
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    raise ValueError(f"a chart coefficient is [level, permil] or [level, permil, K], not {parts}")


def chart_label(asset_name: str) -> str:
    """`chart_m0405_expert` -> `m0405 EXPERT`; any other shape is returned unchanged."""
    parts = asset_name.split("_")
    if len(parts) >= 3 and parts[0] == "chart":
        return f"{parts[1]} {parts[2].upper()}"
    return asset_name


def _field(data: Mapping, number: int) -> object:
    value = data.get(number)
    if value is None:
        value = data.get(str(number))
    return value[0] if value else None


def coefficients_from_rows(
    music_rows: Iterable[Mapping], difficulty_rows: Iterable[Mapping], chart_rows: Iterable[Mapping]
) -> dict[str, Coef]:
    """`{chart asset name: (level, permil, K)}` from decoded master rows (`master_cache.rows`)."""
    music = {str(r["id"]): r["data"] for r in music_rows}
    k_of: dict[tuple[str, int], int] = {}
    for r in chart_rows:
        d = r["data"]
        k = _field(d, 7)
        k_of[(str(_field(d, 1)), int(_field(d, 2) or 0))] = int(k) if k is not None else DEFAULT_PERMYRIAD
    out: dict[str, Coef] = {}
    for r in difficulty_rows:
        d = r["data"]
        music_id, difficulty, name = str(_field(d, 1)), int(_field(d, 2) or 0), _field(d, 4)
        if not name or music_id not in music:
            continue
        level, permil = _field(d, 3), _field(music[music_id], 17)
        out[str(name)] = (
            int(level or 0),
            int(permil or 0),
            k_of.get((music_id, difficulty), DEFAULT_PERMYRIAD),
        )
    return out


def read_master() -> dict[str, Coef]:
    """The coefficients of every chart, from the game's local master cache.

    Raises when this PC cannot read the master (no SQLCipher library, no local copy)."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import master_cache as mc  # noqa: PLC0415 -- beside this script

    if not mc.can_decrypt():
        raise RuntimeError("no SQLCipher library on this PC")
    return coefficients_from_rows(
        mc.rows("Music"), mc.rows("MusicDifficulty"), mc.rows("MusicDifficultyChart")
    )


def from_chart_file(doc: object) -> dict[str, Coef]:
    """`{"m0405 EXPERT": (level, permil, K)}` from a `charts.json` / `charts_all.json` document."""
    charts = doc.get("charts") if isinstance(doc, dict) else None
    out: dict[str, Coef] = {}
    for row in (charts or {}).values():
        if not isinstance(row, dict):
            continue
        label = f"{row.get('music_id')} {str(row.get('difficulty', '')).upper()}"
        out[label] = (
            int(row.get("difficulty_level") or 0),
            int(row.get("live_score_coefficient_permil") or 0),
            int(row.get("score_coefficient_permyriad", DEFAULT_PERMYRIAD)),
        )
    return out


def change_lines(before: Mapping[str, Coef], after: Mapping[str, Coef]) -> list[str]:
    """One line per chart whose K, level or permil moved, and per new chart whose K is not x1.

    Keys are chart labels; `music_coef.json` asset names are turned into labels here, so
    both sources print alike. A chart that left the master is not reported: the chart
    library's own probe names lost charts."""
    old = {chart_label(k): v for k, v in before.items()}
    new = {chart_label(k): v for k, v in after.items()}
    lines: list[str] = []
    for label in sorted(new):
        level, permil, k = new[label]
        if label not in old:
            if k != DEFAULT_PERMYRIAD:
                lines.append(f"new chart with K {k}: {label}")
            continue
        old_level, old_permil, old_k = old[label]
        if old_k != k:
            lines.append(f"chart K changed: {label} {old_k} -> {k}")
        if old_level != level:
            lines.append(f"chart level changed: {label} {old_level} -> {level}")
        if old_permil != permil:
            lines.append(f"chart permil changed: {label} {old_permil} -> {permil}")
    return lines


def unnamed_fields(rows_by_table: Mapping[str, Iterable[Mapping]], schema: Mapping) -> dict[str, list[int]]:
    """`{table: [field numbers]}` present in the rows and absent from the schema's message."""
    messages = schema.get("messages", schema)
    out: dict[str, list[int]] = {}
    for table, rows in rows_by_table.items():
        known = {int(f["n"]) for f in messages.get(table, ())}
        seen: set[int] = set()
        for r in rows:
            seen.update(int(n) for n in r["data"])
        extra = sorted(seen - known)
        if extra:
            out[table] = extra
    return out


def unnamed_field_lines(rows_by_table: Mapping[str, Iterable[Mapping]], schema: Mapping) -> list[str]:
    """The refresh summary's warning: one line per score table holding fields the schema lacks."""
    return [
        f"⚠ master {table} has field(s) the schema does not name: {', '.join(map(str, numbers))}"
        for table, numbers in unnamed_fields(rows_by_table, schema).items()
    ]


def score_table_report() -> list[str]:
    """`unnamed_field_lines` over `SCORE_TABLES`, read from the local master cache, or one line
    saying why the check could not run."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    # a report reads the local copies only: no re-download from the CDN on its behalf
    previous = os.environ.get("HB_MASTER_NO_FETCH")
    os.environ["HB_MASTER_NO_FETCH"] = "1"
    try:
        import master_cache as mc  # noqa: PLC0415 -- beside this script

        if not mc.can_decrypt():
            return ["master score tables not checked: no SQLCipher library on this PC"]
        with SCHEMA.open(encoding="utf-8") as fh:
            schema = json.load(fh)
        rows = {table: list(mc.rows(table)) for table in SCORE_TABLES}
    except Exception as exc:  # noqa: BLE001 -- a report line, never a failed refresh
        return [f"master score tables not checked: {type(exc).__name__}: {exc}"]
    finally:
        if previous is None:
            os.environ.pop("HB_MASTER_NO_FETCH", None)
        else:
            os.environ["HB_MASTER_NO_FETCH"] = previous
    lines = unnamed_field_lines(rows, schema)
    return lines or [f"master score tables carry only named fields ({', '.join(SCORE_TABLES)})"]


def write_cache(path: str, coef: Mapping[str, Coef]) -> None:
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import atomic_io  # noqa: PLC0415 -- beside this script

    atomic_io.write_text(path, json.dumps({k: list(v) for k, v in coef.items()}), "utf-8")


def read_cache(path: str) -> dict[str, Coef]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return {k: as_coef(v) for k, v in json.load(fh).items()}
