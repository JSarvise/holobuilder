"""The output-rendering layer: consumes `RankedTeam` / `ScoreBreakdown`
data only, never prints, never imports `argparse` or `pathlib`. Every
formatter follows `cli.format_gates_report`'s "build `list[str]`,
caller prints" idiom.
"""

from __future__ import annotations
