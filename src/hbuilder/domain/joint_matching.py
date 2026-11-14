"""The Connect matching of a team and the whole bench, solved once for the bench and re-solved
per team by changing a few rows: the dynamic Hungarian method.

The account-wide Connect assignment is a maximum-weight matching of socket rows (a board's
socket) to card groups (cards of one shape and permil, a column of that many units). The bench
(every board off the team) is the same for every team of a song, so its matching is solved once
and kept with its dual potentials; a team then changes only its own holomems' rows (their
sockets valued on the team's boards instead of the bench's) and each changed row costs one
augmenting path.

Square form, so that a changed row can be re-solved exactly: every card unit is held at all times,
by a socket row or by one of its group's own UNUSED rows (worth 0 there and on the open column),
and a socket left without a card holds a unit of the OPEN column (capacity: every socket row).
With every column full a row can be taken out and put back with one augmentation and the
matching stays optimal (Mills-Tettey, Stentz and Dias, "The dynamic Hungarian algorithm for the
assignment problem with changing costs", 2007). The augmentation is `board_plan._max_weight_matching`'s
successive-shortest-path step line for line: same scan order, same strict comparisons, the rows
of a column kept in arrival order, the lowest column index winning a tie.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

_INF = math.inf


class JointMatching:
    """A solved matching of `weights` (`n` socket rows x `m` card groups, each `>= 0`) to the
    groups' `capacities`, and its dual potentials. `solve(changes)` returns the matching with
    some rows' weights replaced, re-solved from this one (this one is left as it is).

    Rows `0..n-1` are the sockets; the unused rows follow, one per card unit, group by group.
    Columns `0..m-1` are the groups; column `m` is the open column."""

    __slots__ = ("m", "n", "caps", "cost", "home", "u", "v", "matched", "col_of_row")

    def __init__(self, weights: Sequence[Sequence[float]], capacities: Sequence[int]) -> None:
        m, n = len(capacities), len(weights)
        self.m, self.n = m, n
        self.caps = [*capacities, n]
        self.cost: list[list[float] | None] = [[-w for w in row] + [0.0] for row in weights]
        self.home = [-1] * n
        for j, cap in enumerate(capacities):
            for _ in range(cap):
                self.home.append(j)
                self.cost.append(None)
        rows = len(self.home)
        self.u = [0.0] * rows
        self.v = [0.0] * (m + 1)
        self.matched: list[list[int]] = [[] for _ in range(m + 1)]
        self.col_of_row = [-1] * rows
        # the cards first: every group full of its own unused rows, the open column empty
        for r in range(n, rows):
            self._augment(r)
        for r in range(n):
            self._augment(r)

    def copy(self) -> JointMatching:
        other = object.__new__(JointMatching)
        other.m, other.n, other.caps, other.home = self.m, self.n, self.caps, self.home
        other.cost = list(self.cost)
        other.u = list(self.u)
        other.v = list(self.v)
        other.matched = [list(rows) for rows in self.matched]
        other.col_of_row = list(self.col_of_row)
        return other

    def _row_costs(self, i: int) -> tuple[tuple[int, float], ...] | list[float]:
        """Row `i`'s costs: a socket row's list over every column, an unused row's
        `((its group, 0.0), (the open column, 0.0))` (every other column is infinite)."""
        costs = self.cost[i]
        if costs is None:
            return ((self.home[i], 0.0), (self.m, 0.0))
        return costs

    def _augment(self, r: int) -> None:
        """One successive-shortest-path step from the unassigned row `r` to a column with a
        free unit (`board_plan._max_weight_matching`'s loop body)."""
        m = self.m + 1
        u, v, matched, caps, col_of_row = self.u, self.v, self.matched, self.caps, self.col_of_row
        minv = [_INF] * m
        way_col = [-1] * m
        way_row = [-1] * m
        used = [False] * m
        ur = u[r]
        costs = self._row_costs(r)
        if isinstance(costs, list):
            for j in range(m):
                c = costs[j] - ur - v[j]
                if c < minv[j]:
                    minv[j] = c
                    way_row[j] = r
        else:
            for j, cost in costs:
                c = cost - ur - v[j]
                if c < minv[j]:
                    minv[j] = c
                    way_row[j] = r
        visited: list[int] = []
        while True:
            delta = _INF
            j1 = -1
            for j in range(m):
                if not used[j] and minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            u[r] += delta
            for j in visited:
                v[j] -= delta
                for i in matched[j]:
                    u[i] += delta
            for j in range(m):
                if not used[j]:
                    minv[j] -= delta
            used[j1] = True
            visited.append(j1)
            if len(matched[j1]) < caps[j1]:
                break
            for i in matched[j1]:
                ci = self._row_costs(i)
                ui = u[i]
                if isinstance(ci, list):
                    for j in range(m):
                        if not used[j]:
                            c = ci[j] - ui - v[j]
                            if c < minv[j]:
                                minv[j] = c
                                way_col[j] = j1
                                way_row[j] = i
                else:
                    for j, cost in ci:
                        if not used[j]:
                            c = cost - ui - v[j]
                            if c < minv[j]:
                                minv[j] = c
                                way_col[j] = j1
                                way_row[j] = i
        j = j1
        while True:
            i = way_row[j]
            pj = way_col[j]
            matched[j].append(i)
            col_of_row[i] = j
            if pj == -1:
                break
            matched[pj].remove(i)
            j = pj

    def solve(self, changes: Mapping[int, Sequence[float]]) -> JointMatching:
        """A copy with each socket row of `changes` given new weights and the matching re-solved:
        the changed rows are taken out (in row order), then put back one augmentation each, in
        row order."""
        out = self.copy()
        order = sorted(changes)
        for r in order:
            if not 0 <= r < self.n:
                raise ValueError(f"row {r} is not a socket row")
            col = out.col_of_row[r]
            if col >= 0:
                out.matched[col].remove(r)
                out.col_of_row[r] = -1
            out.cost[r] = [-w for w in changes[r]] + [0.0]
        for r in order:
            out._augment(r)
        return out

    def assignment_after(self, changes: Mapping[int, Sequence[float]]) -> dict[int, int]:
        """`solve(changes).assignment()` (the compiled twin re-solves in a kept buffer)."""
        return self.solve(changes).assignment()

    def assignment(self) -> dict[int, int]:
        """`{socket row: group}` of the rows holding a card at a positive weight (a row on a
        group at weight 0 is as good as open, as `_max_weight_matching` reports it)."""
        out = {}
        for i in range(self.n):
            j = self.col_of_row[i]
            costs = self.cost[i]
            if 0 <= j < self.m and costs is not None and costs[j] < 0:
                out[i] = j
        return out

    def value(self) -> float:
        """The matched weight, summed in row order."""
        total = 0.0
        for i, j in self.assignment().items():
            total += -self.cost[i][j]  # type: ignore[index]
        return total
