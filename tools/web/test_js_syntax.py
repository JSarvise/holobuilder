"""No `.js` on this page may carry a string literal broken across lines.

⛔ **This shipped, twice in one day, and it takes the whole page down.** A
single `"…` left open by an escape that became a real newline is a
`SyntaxError`, the module never evaluates, and EVERY button stops working —
including the ones that had nothing to do with the change. The symptom is a
blank page with one line in the browser console, which is a long way from the
edit that caused it.

The check is deliberately NOT `node --check`. Node happens to be installed on
this machine; a guard that silently skips where it is absent is a guard that
protects nobody on the day it matters. This is pure stdlib and always runs.

What it enforces is exactly the rule JavaScript has: a `'` or `"` string may
not span a line break. Template literals (`` ` ``) may, and are tracked so they
are not mistaken for one. Comments and escapes are handled because a naive
quote count fires on `// don't` and on `\\"`, and a check that cries wolf gets
deleted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent / "static"


def _unterminated(source: str) -> list[tuple[int, str]]:
    """Line numbers whose `'`/`"` string is still open at the newline."""
    offenders: list[tuple[int, str]] = []
    in_template = False
    in_block_comment = False
    for number, line in enumerate(source.splitlines(), 1):
        index = 0
        quote: str | None = None
        while index < len(line):
            char = line[index]
            if in_block_comment:
                if line.startswith("*/", index):
                    in_block_comment = False
                    index += 2
                    continue
                index += 1
                continue
            if quote is None and in_template:
                # Inside a backtick string; only its closing tick matters here.
                if char == "\\":
                    index += 2
                    continue
                if char == "`":
                    in_template = False
                index += 1
                continue
            if quote is not None:
                if char == "\\":
                    index += 2
                    continue
                if char == quote:
                    quote = None
                index += 1
                continue
            if line.startswith("//", index):
                break
            if line.startswith("/*", index):
                in_block_comment = True
                index += 2
                continue
            if char == "`":
                in_template = True
            elif char in "'\"":
                quote = char
            index += 1
        if quote is not None:
            offenders.append((number, line.rstrip()))
    return offenders


@pytest.mark.parametrize(
    "path", sorted(STATIC.glob("*.js")), ids=lambda p: p.name
)
def test_no_string_literal_spans_a_line_break(path: Path) -> None:
    offenders = _unterminated(path.read_text(encoding="utf-8"))
    assert not offenders, (
        f"{path.name}: string literal left open at end of line — the module "
        f"will not parse and every control on the page stops working:\n"
        + "\n".join(f"  line {n}: {text}" for n, text in offenders)
    )


def test_the_check_catches_the_bug_it_was_written_for() -> None:
    """The negative control. A guard nobody has seen fail is a guard nobody
    knows works — and this one was written from a real defect, so the real
    defect is the fixture."""
    broken = 'const x = "Refresh finished.\n" + y;'
    assert _unterminated(broken), "the check missed a genuinely broken literal"

    fine = 'const x = "Refresh finished.\\n" + y;  // don\'t fire on this\n'
    assert not _unterminated(fine), "the check fired on valid source"

    template = "const x = `line one\nline two`;\n"
    assert not _unterminated(template), "template literals may span lines"
