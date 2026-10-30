"""`python -m hbuilder` entry point (D-10). Delegates to `hbuilder.cli:main`
— the same function `[project.scripts]` in `pyproject.toml` wires the
`hbuilder` console script to, so the module form and the installed form
never diverge.
"""

from __future__ import annotations

from hbuilder.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
