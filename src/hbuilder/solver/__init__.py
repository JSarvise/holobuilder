"""The pure-compute search layer: exhaustive (1 Leader + 5 Members)
enumeration and bounded-heap Top-N selection over `domain.scoring`'s
`score_fast`. Never imports `json`, `argparse`, `pathlib`, or `sys` (no I/O
in the compute layer) and never resolves a gate or computes a rate itself
— the only domain call is `score_fast(team, cfg)`.

Two solvers, one signature:

- `exact.solve_exact` — exhaustive, and the reference truth. It takes about
  32 h on a real 91-card pool, so it is a small-pool tool.
- `heuristic.solve_heuristic` — gate-aware, multi-seeded beam search with
  per-feature quotas, an open-condition bonus and one-card-swap polish. Same
  parameters, same `tuple[RankedTeam, ...]`, so switching between them is a
  one-line change at the call site.
  `heuristic.solve_heuristic_labelled` returns the same teams plus the
  specific `FailureMode`s the search **observed at runtime**, which is what a
  presented approximate answer must be labelled with.
"""

from __future__ import annotations
