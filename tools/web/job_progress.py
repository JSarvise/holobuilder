"""`eta_s` and `best_so_far` on a search job's progress events (critique
A-data §1 P1-5: "progress you can trust"). `/api/run` and `/api/event/run`
wrap their `emit` in a `JobProgress`, which passes every event through and
adds, to each `progress` event:

    elapsed_s     seconds since the job started
    stage_s       seconds since this stage's first event
    eta_s         seconds left IN THIS STAGE, or null when nothing measured
                  says how much is left. From `done/total` (rate so far x
                  what is left), else from a stage projection
                  (`note_projection`, e.g. the exact search's
                  `projected_seconds`) minus the time already spent. Never a
                  guess: a stage with neither keeps `eta_s: null`.
    eta_basis     "rate" | "projection" | null
    best_so_far   the best team seen so far `{cards: [leader, m1..m5], value,
                  metric, stage, provisional: true}`, or null. Read off events
                  that carry a team (`team` + `song_est` / `optimised_us` /
                  `score`, or a ready `best_so_far`). PROVISIONAL: the final
                  `result` is the answer; a later stage may rank differently,
                  and a value of a different metric replaces the held one.

Read-only bookkeeping; nothing here scores anything.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Mapping

# the per-team value keys an event may carry, most specific first
_VALUE_KEYS = (("song_est", "song_est"), ("score", "score"), ("optimised_us", "unit_score"))


class JobProgress:
    def __init__(self, emit: Callable[[dict], Any], clock: Callable[[], float] = time.monotonic):
        self._emit = emit
        self._clock = clock
        self._t0 = clock()
        self._stage_t0: dict[str, float] = {}
        self._totals: dict[str, int] = {}
        self._projected: dict[str, float] = {}
        self.best: dict | None = None

    def note_projection(self, stage: str, *, total: int | None = None,
                        seconds: float | None = None) -> None:
        """A stage's known size (`total`, in the unit of its `evaluated` /
        `done`) and/or projected duration."""
        if total:
            self._totals[stage] = int(total)
        if seconds is not None and seconds > 0:
            self._projected[stage] = float(seconds)

    def _best(self, ev: Mapping, stage: str | None) -> None:
        held = ev.get("best_so_far")
        if isinstance(held, Mapping) and held.get("value") is not None:
            self._offer(dict(held))
            return
        team = ev.get("team")
        if not isinstance(team, (list, tuple)) or not team:
            return
        for key, metric in _VALUE_KEYS:
            value = ev.get(key)
            if isinstance(value, (int, float)):
                self._offer({"cards": list(team), "value": float(value), "metric": metric,
                             "stage": stage})
                return

    def _offer(self, cand: dict) -> None:
        cand["provisional"] = True
        if (self.best is None or cand.get("metric") != self.best.get("metric")
                or cand["value"] > self.best["value"]):
            self.best = cand

    def __call__(self, ev: dict) -> None:
        now = self._clock()
        kind = ev.get("type")
        stage = ev.get("stage")
        if stage is not None and kind in ("stage", "progress"):
            if kind == "stage" and ev.get("state") == "active":
                self._stage_t0[stage] = now
            else:
                self._stage_t0.setdefault(stage, now)
        self._best(ev, stage)
        if kind == "progress":
            ev = dict(ev)
            key = stage or "search"
            if ev.get("sub"):
                key = f"{key}:{ev['sub']}"      # a sub-phase has its own rate
            start = self._stage_t0.setdefault(key, now)
            spent = now - start
            done = ev.get("done") if ev.get("done") is not None else ev.get("evaluated")
            total = ev.get("total") or self._totals.get(key)
            eta = basis = None
            if ev.get("complete"):
                eta, basis = 0.0, "rate"
            elif isinstance(done, (int, float)) and total and done > 0 and spent > 0:
                eta, basis = max(0.0, spent * (total - done) / done), "rate"
            elif key in self._projected:
                eta, basis = max(0.0, self._projected[key] - spent), "projection"
            if total and ev.get("total") is None and ev.get("done") is None:
                ev["total"] = total
            ev.update({"elapsed_s": round(now - self._t0, 1), "stage_s": round(spent, 1),
                       "eta_s": None if eta is None else round(eta, 1), "eta_basis": basis,
                       "best_so_far": dict(self.best) if self.best else None})
        self._emit(ev)
