"""`tools/recon/proxy/chart_timeline.active_windows` must not clamp a window
to the end of the song.

**The bug this pins.** `song_seconds` is `max(note.time)` — the timestamp of the
chart's *last* note, not a moment after it. Every consumer treats an activation
window as half-open `[check, end)`. Clamping `end` to `song_seconds` therefore
made `end == song_seconds` for any skill still live when the chart ends, and
`t < end` then judged that last note **outside** the window that covers it. One
silently dropped note per such window, and it is the note carrying the highest
combo multiplier (`SCORE-SONG.md` §6 measures the loss at 0.4 % of the level-1
prediction on `m0001` easy).

`src/hbuilder/domain/song.py::_active_windows` never clamped and documents why;
these assertions pin that the recon tool now agrees with it, and each one also
asserts against the **previous, buggy** answer so a re-introduced `min(...)`
fails rather than passes quietly.

The numbers are the real `m0001` easy timeline for `chart_timeline.DEFAULT_TEAM`
(`python tools/recon/proxy/chart_timeline.py m0001 easy`), transcribed as
literals so the test does not depend on the gitignored `dumps/`.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROXY_DIR = REPO_ROOT / "tools" / "recon" / "proxy"
if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

import chart_timeline  # noqa: E402

# `m0001` easy, from the chart itself: `song_seconds` is `round(max(n.time), 3)`
# and the last scored note sits at 98.6503 s — i.e. *after* the rounded
# `song_seconds`, which is a second, independent reason the clamp lost it.
M0001_EASY_SONG_SECONDS = 98.65
M0001_EASY_LAST_NOTE = 98.6503

# Slot 3 of `chart_timeline.DEFAULT_TEAM` (Ninomae Ina'nis) — the one member of
# that team whose ACTIVE is still live when the chart ends.
INA_COOLDOWN = 32.0
INA_DURATION = 14.0


def _skill(cooldown: float, duration: float) -> dict:
    return {"cooldown": cooldown, "duration": duration}


def test_the_last_window_runs_past_the_end_of_the_song() -> None:
    windows = chart_timeline.active_windows(
        _skill(INA_COOLDOWN, INA_DURATION), M0001_EASY_SONG_SECONDS
    )

    # The lattice itself is untouched by the fix: checks at k * cooldown for
    # every k with k * cooldown <= song_seconds. Three, not four (128.0 > 98.65).
    assert [w["check"] for w in windows] == [32.0, 64.0, 96.0]

    assert windows[-1]["end"] == 110.0            # 96.0 + 14.0, unclamped
    assert windows[-1]["end"] != M0001_EASY_SONG_SECONDS   # the previous answer


def test_the_last_note_is_inside_the_last_window() -> None:
    windows = chart_timeline.active_windows(
        _skill(INA_COOLDOWN, INA_DURATION), M0001_EASY_SONG_SECONDS
    )
    last = windows[-1]

    # Half-open, exactly as `resolve_exclusive` and `special_alignment`
    # evaluate it.
    assert last["check"] <= M0001_EASY_LAST_NOTE < last["end"]

    # ...and what the clamp used to answer, spelled out so the regression is
    # named rather than merely absent.
    clamped_end = min(last["check"] + INA_DURATION, M0001_EASY_SONG_SECONDS)
    assert clamped_end == 98.65
    assert not (last["check"] <= M0001_EASY_LAST_NOTE < clamped_end)


def test_a_window_wholly_inside_the_song_is_unchanged() -> None:
    """The fix must move only the windows the clamp actually bit."""
    windows = chart_timeline.active_windows(
        _skill(INA_COOLDOWN, INA_DURATION), M0001_EASY_SONG_SECONDS
    )
    assert windows[0] == {"check": 32.0, "end": 46.0}
    assert windows[1] == {"check": 64.0, "end": 78.0}


def test_cooldown_reduction_still_divides() -> None:
    """D-33: `cooldown / (1 + reduction)`, not `cooldown * (1 - reduction)`.

    Slot 1 of `DEFAULT_TEAM` under its captured 4 % board node: 26.0 / 1.04 =
    25.0 exactly, so the lattice moves from 26/52/78 to 25/50/75."""
    windows = chart_timeline.active_windows(
        _skill(26.0, 9.0), M0001_EASY_SONG_SECONDS, reduction=0.04
    )
    assert [w["check"] for w in windows] == [25.0, 50.0, 75.0]
    assert [w["end"] for w in windows] == [34.0, 59.0, 84.0]
