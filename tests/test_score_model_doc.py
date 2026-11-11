"""docs/score-model.md cites the code as `file.py`, `name`: every cited name is defined in the
file the document names, so the write-up cannot drift from the code unnoticed."""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "score-model.md"
DOMAIN = REPO / "src" / "hbuilder" / "domain"
CITATION = re.compile(r"`([\w/]+\.py)`((?:,\s*`[\w.]+`)+)")


def _defined(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_bytes())):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _source(cited: str) -> Path:
    """A bare file name is under src/hbuilder/domain/ (the document says so); a path is from the root."""
    return REPO / cited if "/" in cited else DOMAIN / cited


def test_every_cited_name_is_defined_in_the_cited_file():
    text = DOC.read_text(encoding="utf-8")
    missing = []
    for m in CITATION.finditer(text):
        path = _source(m.group(1))
        assert path.is_file(), f"docs/score-model.md cites {m.group(1)}, which does not exist"
        names = _defined(path)
        for name in re.findall(r"`([\w.]+)`", m.group(2)):
            if name.split(".")[0] not in names:
                missing.append(f"{m.group(1)}: {name} (line {text[: m.start()].count(chr(10)) + 1})")
    assert not missing, "docs/score-model.md cites names that are not where it says: " + "; ".join(missing)
