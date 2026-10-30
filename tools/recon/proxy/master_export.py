"""Dump a master table to `dumps/master_<Table>.json` — offline, from the game's own cache.

Several tools read a master table as a plain JSON file rather than opening the SQLCipher database
themselves: `build_board.py` and `tools/web/server.py` both want `dumps/master_SkillTreeNode.json`
(field 1 = group, 6 = effect, 3 = the characters the row applies to, 9 = point cost). This is
the command that produces them, so a lost `dumps/` can be rebuilt. `master_cache.rows()` returns exactly the shape
those consumers expect (`{"id": ..., "data": {field: [values]}}`, field keys stringified by JSON),
so this adds a file name and a check, not a second reading of the master.

⚠ `rows()` decodes every byte value as UTF-8 with `errors="replace"`, so a table with genuinely
binary fields comes back lossy — use `open_table` + `decode_record` when the bytes matter. It is the right call HERE because the
tables this is used for carry ids, ints and ASCII group names, and because the consumers
themselves read the stringified form. Do not reach for it to export a table full of blobs.

Usage:
    proxyenv/Scripts/python master_export.py SkillTreeNode
    proxyenv/Scripts/python master_export.py SkillTreeNode SkillTreeEffect
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import master_cache as mc  # noqa: E402

DUMPS = HERE / "dumps"


def export(table: str) -> Path:
    records = mc.rows(table)
    if not records:
        raise SystemExit(f"{table}: 0 rows — refusing to write an empty export")
    DUMPS.mkdir(exist_ok=True)
    out = DUMPS / f"master_{table}.json"
    out.write_text(json.dumps(records, ensure_ascii=False), "utf8")
    print(f"{table}: {len(records)} rows -> {out}")
    return out


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    tables = sys.argv[1:]
    if not tables:
        print(__doc__.split("Usage:")[1].strip(), file=sys.stderr)
        return 2
    for table in tables:
        export(table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
