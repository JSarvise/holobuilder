# Read a hololive Dreams .sus chart into a time-ordered note list.
#
# SUS is the open text chart format (Seaurchin's); this game writes a variant of it. Nothing here
# is decryption -- the files are plain UTF-8 and start with `#CREATE_APP_`. See chart_download.py
# for where they come from and chart_cache.py for the handful the client caches by itself.
#
# THE FORMAT, as established against the game's charts (see verify() -- every claim below is checked
# against each chart's own header counts, on every chart, and the checker is the point of this
# module):
#
#   #BPMxx: v          define BPM slot xx
#   #mmm02: v          measures are v beats long FROM mmm ONWARD (default 4 until first stated)
#   #mmm08: <slots>    BPM slot indices placed on the measure's grid
#   #mmm1x: <slots>    short notes.  x = lane
#   #mmm3xy: <slots>   long/slide notes.  x = lane, y = slide identifier (36 concurrent)
#   #mmm5x: <slots>    per-note attribute (flick direction).  x = lane
#
#   <slots> is a run of 2-character cells; the line divides the measure into len/2 equal steps and
#   cell i sits at fraction i/n. A cell whose FIRST character is '0' is empty. Otherwise character
#   1 is the note type and character 2 the note's width.
#
#   Several lines may address the same measure+channel; they merge on a common grid. Counting
#   without merging over-counts -- on m0524_expert: 576 raw channel-1 cells against 554
#   real ones. The merge de-duplicates by the note's FULL identity, not by position: the later
#   line does NOT win, because two lines routinely place different notes at the same position.
#
# THE ONE NON-OBVIOUS RULE. A long/slide START is written TWICE: once on channel 3 and once, at
# the same lane and tick, on channel 1. The channel-1 copy is not a separate note. On
# chart_m0001_easy that is exactly the 5-note gap between 49 raw channel-1 cells and the header's
# NORMAL_NOTE_COUNT of 44 -- and dropping it closes the count exactly.
#
#   python sus_parse.py --verify [dir]      reproduce every header count on every chart
#   python sus_parse.py <file.sus>          summarise one chart
import collections, os, re, sys
from fractions import Fraction

HERE = os.path.dirname(os.path.abspath(__file__))
CHARTS = os.path.join(HERE, "dumps", "charts_cdn")

HEADER = re.compile(r"^#([A-Z_]+)(?:\s+(.*))?$")
BPMDEF = re.compile(r"^#BPM([0-9A-Za-z]{1,2}):\s*(\S+)$")
BODY = re.compile(r"^#(\d{3})([0-9A-Za-z]{2,3}):\s*(\S*)$")

SHORT, LONG, ATTR = "1", "3", "5"


class Note:
    __slots__ = ("beat", "time", "lane", "chan", "kind", "width", "slide", "attr")

    def __init__(self, beat, lane, chan, kind, width, slide):
        self.beat, self.lane, self.chan = beat, lane, chan
        self.kind, self.width, self.slide = kind, width, slide
        self.time, self.attr = None, None

    def __repr__(self):
        return (f"<{self.chan}{self.kind} lane{self.lane} "
                f"beat{float(self.beat):.3f} t{self.time:.3f}s>")


class Chart:
    def __init__(self, header, notes, bpms):
        self.header, self.notes, self.bpms = header, notes, bpms

    def count(self, name):
        """A #..._COUNT header value, as an int (0 when the chart omits it)."""
        return int(self.header.get(name, 0))

    @property
    def duration(self):
        return self.notes[-1].time if self.notes else 0.0


def _b62(c):
    """One case-SENSITIVE base-62 digit: 0-9, then a-z, then A-Z."""
    o = ord(c)
    if 48 <= o <= 57:
        return o - 48          # '0'-'9' ->  0-9
    if 97 <= o <= 122:
        return o - 87          # 'a'-'z' -> 10-35
    if 65 <= o <= 90:
        return o - 29          # 'A'-'Z' -> 36-61
    return 0


def _slots(data):
    """Split a data field into slots, expanding the run-length marker.

    A cell is normally 2 characters. The exporter also emits `0x` followed by a TWO-CHARACTER
    count, meaning that many empty slots in a row -- a 4-character token. Reading it as two
    ordinary cells silently corrupts every position on the line: measure 017 of m0524_expert
    then divides into 5 steps instead of 38, and `0x03330x04` into 5 instead of 3+1+4 = 8.

    The count is case-SENSITIVE BASE-62, not base-36. Base-36 folds 'A' onto 'a', so every
    run longer than 35 slots would expand short and the notes after it land at the wrong
    times. It is a tempting misreading because base-36 is the largest radix under which the
    *totals* still come out whole; only base-62 reproduces LONG_CONTINUE, NORMAL, FLICK,
    LONG_RELAY and FULL_COMBO on every chart."""
    out, i = [], 0
    while i + 1 < len(data):
        if data[i:i + 2] == "0x" and i + 4 <= len(data):
            out.extend([None] * (_b62(data[i + 2]) * 62 + _b62(data[i + 3])))
            i += 4
        else:
            out.append(data[i:i + 2])
            i += 2
    return out


def _cells(data):
    """(fraction within the measure, cell) for every non-empty slot."""
    slots = _slots(data)
    n = len(slots)
    for i, cell in enumerate(slots):
        if cell and cell[0] != "0":     # '0' in the type position means no note
            yield Fraction(i, n), cell


def parse(text):
    header, bpmdef, lengths = {}, {}, {}
    # (measure, channel) -> {(fraction, cell)}. A SET, not a fraction->cell map: two lines may
    # place DIFFERENT notes at the same position in the same measure and channel, and letting the
    # later line win destroys one of them. De-duplicating by the note's full identity keeps both
    # and drops only exact repeats, which the dense multi-line encoding produces in quantity.
    # Worth 6 charts on LONG_CONTINUE. UntitledCharts/sonolus-level-converters de-dups on
    # (tick, lane, type, width) for the same stated reason.
    grid = collections.defaultdict(set)
    bpmgrid = collections.defaultdict(dict)

    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("#"):
            continue
        m = BPMDEF.match(line)
        if m:
            bpmdef[m.group(1).lstrip("0") or "0"] = float(m.group(2))
            continue
        m = BODY.match(line)
        if m:
            measure, chan, data = int(m.group(1)), m.group(2), m.group(3)
            if chan == "02":
                lengths[measure] = Fraction(data)
            elif chan == "08":
                slots = _slots(data)
                for i, cell in enumerate(slots):
                    if cell and cell != "00":
                        bpmgrid[measure][Fraction(i, len(slots))] = cell.lstrip("0") or "0"
            elif chan[0] in (SHORT, LONG, ATTR):
                grid[(measure, chan)].update(_cells(data))   # exact repeats collapse, rest kept
            continue
        m = HEADER.match(line)
        if m and m.group(2) is not None:
            header[m.group(1)] = m.group(2).strip()

    return _assemble(header, grid, bpmgrid, bpmdef, lengths)


def _length_of(measure, lengths):
    """A measure's length in beats. `#mmm02` PERSISTS until the next declaration.

    A 3/4 chart declares `#00002: 3` once, at measure 0, and says nothing again -- so defaulting
    undeclared measures to 4 stretches every bar after the first by a third, and any hold crossing
    a bar line comes out too long (every chart in 3-beat measures would miss LONG_CONTINUE)."""
    if measure in lengths:
        return lengths[measure]
    prior = [m for m in lengths if m < measure]
    return lengths[max(prior)] if prior else Fraction(4)


def _assemble(header, grid, bpmgrid, bpmdef, lengths):
    last = max([m for m, _ in grid] + list(bpmgrid) + list(lengths) + [0])
    starts = {}
    beat = Fraction(0)
    for i in range(last + 2):
        starts[i] = beat
        beat += _length_of(i, lengths)

    # BPM changes as (beat, bpm), in order
    bpms = []
    for measure in sorted(bpmgrid):
        for frac in sorted(bpmgrid[measure]):
            at = starts[measure] + frac * _length_of(measure, lengths)
            bpms.append((at, bpmdef.get(bpmgrid[measure][frac], 120.0)))
    if not bpms or bpms[0][0] != 0:
        bpms.insert(0, (Fraction(0), bpmdef.get("1", float(header.get("BASEBPM", 120)))))

    notes = []
    for (measure, chan), cells in grid.items():
        length = _length_of(measure, lengths)
        lane = int(chan[1], 36)
        slide = chan[2] if len(chan) > 2 else None
        for frac, cell in sorted(cells):
            notes.append(Note(starts[measure] + frac * length, lane, chan[0],
                              cell[0], cell[1], slide))
    notes.sort(key=lambda n: (n.beat, n.lane, n.chan))
    _set_times(notes, bpms)
    _attach_attributes(notes)
    return Chart(header, notes, bpms)


def _set_times(notes, bpms):
    """Walk the BPM changes once, converting beats to seconds."""
    segs, t = [], 0.0
    for i, (at, bpm) in enumerate(bpms):
        segs.append((at, t, bpm))
        if i + 1 < len(bpms):
            t += float(bpms[i + 1][0] - at) * 60.0 / bpm
    for note in notes:
        at, t0, bpm = next(s for s in reversed(segs) if s[0] <= note.beat)
        note.time = t0 + float(note.beat - at) * 60.0 / bpm


def _attach_attributes(notes):
    """Fold channel-5 cells onto the note they decorate, and drop them from the note list.

    Every note at the position gets it. Making the marker consumable once (so co-located twins
    share it) fits `m0094_expert` exactly but breaks 48 other charts on `LONG_END`, whether the
    scope is the whole position or identical kinds only; `m0009_expert` is the counter-example,
    whose co-located ends must BOTH be flicks. Whatever separates m0094 from m0009 is not
    co-location."""
    attrs = {}
    for note in notes:
        if note.chan == ATTR:
            attrs[(note.beat, note.lane)] = note.kind
    notes[:] = [n for n in notes if n.chan != ATTR]
    for note in notes:
        note.attr = attrs.get((note.beat, note.lane))


FLICK_ATTR = "1"           # channel-5 attribute that makes a note a flick
HOLD_TICK = Fraction(1, 2)  # a hold scores a "continue" note every half beat


def holds(chart):
    """(start, end) pairs, per slide identifier, one open slide at a time.

    The exporter uses very few identifiers -- most charts have just two -- so slides that overlap
    in time routinely share one, and the identifier alone does not say which end closes which
    start. Four rules against a 708-chart corpus's own LONG_CONTINUE_NOTE_COUNT:

        one open at a time  658/708      LIFO queue        657/708
        pair within a lane  656/708      FIFO queue        655/708

    They agree to within three charts, which is itself the finding: the residual is NOT a pairing
    problem. Holding one slide open and letting a new start supersede an unclosed one wins, and is
    also the rule that cannot manufacture a hold spanning half the song."""
    by_slide = collections.defaultdict(list)
    for n in chart.notes:
        if n.chan == LONG:
            by_slide[n.slide].append(n)
    out = []
    for slide in sorted(by_slide, key=lambda s: (s is None, s)):
        open_at = None
        # ends sort before starts at the same beat, so a slide closing exactly as another opens
        # pairs with its own start rather than stealing the new one
        for n in sorted(by_slide[slide], key=lambda n: (n.beat, n.kind != "2")):
            if n.kind == "1":
                open_at = n
            elif n.kind == "2" and open_at is not None:
                out.append((open_at, n))
                open_at = None
    return out


def classify(chart):
    """Every scored note, as (kind, note). Kinds are the game's own header categories.

    Derived by fitting all header counts across every chart, not assumed:
      * every slide note is duplicated on channel 1 at the same lane and tick; the channel-1
        copy is not a separate note. On chart_m0001_easy that is exactly the 5-note gap between
        49 raw channel-1 cells and the header's NORMAL_NOTE_COUNT of 44.
      * a flick is a note carrying channel-5 attribute 1. On a slide end that makes it the
        header's LONG_FLICK_END rather than LONG_END.
      * channel 3 type 5 is an invisible control point and scores nothing.
      * LONG_CONTINUE notes are not written down at all: a hold scores one on every half beat of
        the SONG's grid that falls strictly inside it -- absolute, not measured from the hold's
        own start. On a 708-chart corpus the absolute grid reproduces
        LONG_CONTINUE_NOTE_COUNT on 655, a start-relative grid on only 574."""
    slide_at = {(n.beat, n.lane) for n in chart.notes if n.chan == LONG}
    out = []
    for n in chart.notes:
        if n.chan == SHORT:
            if (n.beat, n.lane) in slide_at:
                continue                                    # duplicate of the slide note
            out.append(("FLICK" if n.attr == FLICK_ATTR else "NORMAL", n))
        elif n.chan == LONG:
            if n.kind == "1":
                out.append(("LONG_START", n))
            elif n.kind == "2":
                out.append(("LONG_FLICK_END" if n.attr == FLICK_ATTR else "LONG_END", n))
            elif n.kind == "3":
                out.append(("LONG_RELAY", n))
            # type 4/5 are control points: they shape the slide but score nothing

    for start, end in holds(chart):
        beat = (start.beat // HOLD_TICK + 1) * HOLD_TICK     # next half beat of the song grid
        while beat < end.beat:
            tick = Note(beat, start.lane, LONG, "c", start.width, start.slide)
            out.append(("LONG_CONTINUE", tick))
            beat += HOLD_TICK
    _set_times([n for _, n in out], chart.bpms)
    out.sort(key=lambda kn: (kn[1].beat, kn[1].lane))
    return out


def tally(chart):
    """Scored notes per header category."""
    return collections.Counter(kind for kind, _ in classify(chart))


CATEGORIES = ("NORMAL", "FLICK", "LONG_START", "LONG_END", "LONG_FLICK_END",
              "LONG_RELAY", "LONG_CONTINUE")


def verify(paths):
    """Reproduce each chart's own header counts from its body. The only real check available."""
    agree, rows = collections.Counter(), []
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            chart = parse(fh.read())
        t = tally(chart)
        checks = {c + "_NOTE_COUNT": t[c] for c in CATEGORIES}
        # Against FULL_COMBO, not TOTAL. `MusicDifficultyChart.full_combo_note_count` is the
        # game's own name for field 3, and TOTAL = FULL_COMBO + GHOST holds on every chart --
        # ghosts are not combo-bearing and are not among CATEGORIES, so TOTAL is the wrong
        # target.
        checks["FULL_COMBO_NOTE_COUNT"] = sum(t[c] for c in CATEGORIES)
        for key, got in checks.items():
            ok = got == chart.count(key)
            agree[(key, ok)] += 1
            if not ok:
                rows.append((os.path.basename(path), key, got, chart.count(key)))
    return agree, rows


def headers_only(path):
    """The #KEY VALUE block, without parsing the body. Cheap, and enough for note counts."""
    out = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("#") and " " in line and ":" not in line.split(" ")[0]:
                key, _, val = line[1:].partition(" ")
                out[key] = val.strip()
            elif ":" in line and line.startswith("#0"):
                break
    return out


def flick_report(src):
    """Per-chart flick counts -- the PC/mobile delta driver.

    LiveNote holds two score multipliers per (note type, judgement). They are equal on every
    row except note type 2 at the two best judgements, where they read 1050 against 1000. So
    exactly one note type scores 5% differently between the two, and a chart's exposure to that
    is its count of those notes. #FLICK_NOTE_COUNT is read straight from the chart header and is
    cross-checked against the master DB, so the counts below are exact.

    What is NOT settled here: which header category note type 2 IS."""
    rows = []
    for fn in sorted(os.listdir(src)):
        if not fn.endswith(".sus"):
            continue
        h = headers_only(os.path.join(src, fn))
        total = int(h.get("TOTAL_NOTE_COUNT", 0))
        flicks = int(h.get("FLICK_NOTE_COUNT", 0)) + int(h.get("LONG_FLICK_END_NOTE_COUNT", 0))
        rows.append((flicks / total if total else 0.0, flicks, total, h.get("MUSIC_ID", "?"),
                     h.get("SCORE_LEVEL", "?"), fn))
    rows.sort(reverse=True)
    print(f"{len(rows)} charts, ranked by share of notes that score differently\n")
    print(f"{'chart':30} {'lvl':>4} {'flick':>6} {'total':>6} {'share':>7}")
    for share, flicks, total, _, lvl, fn in rows[:25]:
        print(f"{fn[:30]:30} {lvl:>4} {flicks:6} {total:6} {share:6.1%}")
    zero = sum(1 for r in rows if not r[1])
    print(f"\n  {zero} charts have no such notes at all -- identical on both platforms")
    print(f"  median share {sorted(r[0] for r in rows)[len(rows) // 2]:.1%}, "
          f"max {rows[0][0]:.1%} ({rows[0][5]})")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--flicks" in sys.argv:
        return flick_report(args[0] if args else CHARTS)
    if "--verify" in sys.argv:
        src = args[0] if args else CHARTS
        paths = sorted(os.path.join(src, f) for f in os.listdir(src) if f.endswith(".sus"))
        agree, rows = verify(paths)
        print(f"{len(paths)} charts")
        worst = 0
        for key in sorted({k for k, _ in agree}):
            ok, bad = agree[(key, True)], agree[(key, False)]
            worst = max(worst, bad)
            print(f"  {key:26} {ok:4}/{ok + bad} agree" + ("" if not bad else f"   {bad} DISAGREE"))
        for name, key, got, want in rows[:15]:
            print(f"    {name:28} {key:26} got {got}, header says {want}")
        return sys.exit(1 if worst else 0)

    if not args:
        sys.exit(__doc__ or "usage: sus_parse.py <file.sus> | --verify [dir]")
    with open(args[0], encoding="utf-8", errors="replace") as fh:
        chart = parse(fh.read())
    # `classify()` IS the set of playable notes: it drops the channel-1 duplicate of every
    # slide start and the type-4/5 control points, and adds the LONG_CONTINUE ticks a hold
    # scores but the file never writes down. Its length is therefore the chart's own
    # FULL_COMBO_NOTE_COUNT, which is why that is the number printed beside it: a count
    # nobody can check would hide a parsing error.
    scored = classify(chart)
    t = collections.Counter(kind for kind, _ in scored)
    full_combo = sum(t[c] for c in CATEGORIES)
    stated = chart.count("FULL_COMBO_NOTE_COUNT")
    verdict = "agrees" if full_combo == stated else f"DISAGREES with header {stated}"
    print(f"{chart.header.get('MUSIC_ID')}  level {chart.header.get('SCORE_LEVEL')}  "
          f"{chart.duration:.1f}s  {full_combo} scored notes ({verdict})")
    print(f"  bpm changes: {[(float(b), v) for b, v in chart.bpms]}")
    print(f"  tally: {dict(sorted(t.items()))}")
    print("  header counts:")
    for k, v in chart.header.items():
        if k.endswith("_COUNT"):
            print(f"    {k:28} {v}")
    return sys.exit(0 if full_combo == stated else 1)


if __name__ == "__main__":
    main()
