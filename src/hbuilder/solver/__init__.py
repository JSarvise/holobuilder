"""The search layer over `domain`'s scorers.

- `members_first` — the member-set pool, gate tables and screening the joint
  leader + members search is built on (`tools/web/joint_search.py`).
- `parallel_refine` — scores and refines teams on real charts in a process pool
  (`RefinePool`), the exact per-song leaf the web ranks with.
- `parallel` — the worker-count default.
"""

from __future__ import annotations
