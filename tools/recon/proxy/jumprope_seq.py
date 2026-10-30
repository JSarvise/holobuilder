"""Pure-Python reimplementation of hololive Dreams' jump-rope gimmick sequence.

Given the server-provided Int32 `seed` (JumpRopeStartSingleResponse field 1) plus the
JumpRopePhaseGimmick master rows for the mode's group, this reproduces the exact ordered
list of rope loops the client will play -- speed multiplier, active gimmick, reverse /
invisible flags, under-type and the fall-object / steam parameters.

Everything here is a transcription of Vision.JumpRope.GimmickLogic.dll as found in
dumps/v7/GameAssembly_dumped.bin.  Key RVAs are cited at each step.

The sequence is fully determined by the seed ALONE -- no life count, no miss flag, no input
timing feeds it. So the whole run can be precomputed the moment StartSingle answers.
(`SinglePlayJumpRopeModel` uses DynamicSequenceGenerator, the lazy form of the same machine;
the two were cross-checked against each other and are equivalent.)

RNG: `System.Random` -- the .NET/Mono Knuth subtractive generator, seeded via
`new Random(seed)` at JumpRopeModel::.ctor+0x1e2 (RVA 0x5e8b922). Identified byte-for-byte
against the ctor at 0x3b0cbb0 (MSEED 161803398, 56-slot array, inext=0, inextp=21). NOT
UnityEngine.Random and not xoshiro -- there is no XoshiroImpl in the image.

CONFIDENCE. The transcription is read from the binary and its arithmetic cross-checks against the master
tables: phase 1 gives 0.800 and phase 2 gives 0.833, matching JumpRopePhaseGimmick's 800/833
permil, and SPEED_UP yields 0.833*1.2 = 0.9996 exactly as the 1200 permil column says.
But it is NOT VALIDATED END-TO-END against a real run. Doing that needs a capture of a run
scoring >= JumpRopeSetting.playLogUploadScore (250), because only then does the client upload
JumpRopePlayLog with its own SequenceHistory to diff against. Without that, treat the
per-loop output as a well-evidenced reconstruction, not a measurement.

One reading is an inference rather than a transcription: that CalcPhase composed with
GetPhaseGimmickForJumpCount reduces to a flat "largest JumpCountFrom <= jumpCount" lookup.
It is tightly constrained (the alternative readings make MaxByOrThrow throw at low jump
counts) but JumpRopeModel::MasterInitialize, which builds _phaseCount, was not fully
reversed. Re-derive this if a future group's JumpCountFrom values overlap across phases.

Usage:
    python jumprope_seq.py 567114856 [group_id] [n_loops]
"""
from __future__ import annotations

import json
import struct
import subprocess
import sys
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- RNG
# GimmickSequenceGeneratorBase._random is declared System.Random
# (dumps/v7/field_map.tsv line 102046) and JumpRopeModel::.ctor+0x1e2 (RVA 0x5e8b922)
# calls mscorlib!Random::.ctor(int) at 0x3b0cbb0 with the seed, storing it at +0x70.
# The ctor at 0x3b0cbb0 is byte-for-byte the classic .NET Framework / Mono Knuth
# subtractive generator:  MSEED 161803398 (`mov r8d, 0x9a4ec86` @0x3b0cc43),
# int[56] (`mov edx, 0x38` @0x3b0cbf3), SeedArray[55]=mj (`[rax+0xfc]` @0x3b0cc73),
# ii=(21*i)%55 (@0x3b0cc90-0x3b0ccb0), the 4x55 mixing pass with index
# 1+(i+30)%55 (@0x3b0cd30-0x3b0cda6), inext=0 / inextp=21 (@0x3b0cdbd/0x3b0cdc6).

MBIG = 2147483647
MSEED = 161803398


class NetRandom:
    """Exact System.Random (.NET Framework / Unity mscorlib) reimplementation."""

    __slots__ = ("_seed_array", "_inext", "_inextp")

    def __init__(self, seed: int) -> None:
        seed = ((seed + 2**31) % 2**32) - 2**31  # coerce to Int32
        sa = [0] * 56
        subtraction = 2147483647 if seed == -2147483648 else abs(seed)
        mj = MSEED - subtraction
        sa[55] = mj
        mk = 1
        for i in range(1, 55):
            ii = (21 * i) % 55
            sa[ii] = mk
            mk = mj - mk
            if mk < 0:
                mk += MBIG
            mj = sa[ii]
        for _ in range(1, 5):
            for i in range(1, 56):
                sa[i] -= sa[1 + (i + 30) % 55]
                if sa[i] < 0:
                    sa[i] += MBIG
        self._seed_array = sa
        self._inext = 0
        self._inextp = 21

    # RVA 0x3b0c6e0  mscorlib!Random::InternalSample
    def _internal_sample(self) -> int:
        loc_inext = self._inext + 1
        if loc_inext >= 56:
            loc_inext = 1
        loc_inextp = self._inextp + 1
        if loc_inextp >= 56:
            loc_inextp = 1
        ret = self._seed_array[loc_inext] - self._seed_array[loc_inextp]
        if ret == MBIG:
            ret -= 1
        if ret < 0:
            ret += MBIG
        self._seed_array[loc_inext] = ret
        self._inext = loc_inext
        self._inextp = loc_inextp
        return ret

    # RVA 0x3b0caa0  mscorlib!Random::Sample
    def sample(self) -> float:
        return self._internal_sample() * (1.0 / MBIG)

    # RVA 0x3b0c890  mscorlib!Random::Next(int,int)
    def next(self, min_value: int, max_value: int) -> int:
        if min_value > max_value:
            raise ValueError("minValue > maxValue")
        rng = max_value - min_value
        if rng <= MBIG:
            return int(self.sample() * rng) + min_value
        # large-range path (never taken by the jump-rope code)
        d = self._get_sample_for_large_range()
        return int(d * rng) + min_value

    def _get_sample_for_large_range(self) -> float:
        result = self._internal_sample()
        if self._internal_sample() % 2 == 0:
            result = -result
        d = result
        d += 2147483646.0
        d /= 4294967293.0
        return d

    # RVA 0x166ea0  mscorlib!Random::NextDouble  ->  Sample()
    def next_double(self) -> float:
        return self.sample()


# ------------------------------------------------------------------ float helpers
def f32(x: float) -> float:
    """Round a Python float to IEEE-754 binary32, as the game's `float` arithmetic does."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


def bits_to_f32(v: int) -> float:
    return struct.unpack("<f", struct.pack("<I", v & 0xFFFFFFFF))[0]


# ------------------------------------------------------------------- master data
# Field offsets from dumps/v7/field_map.tsv lines 60335-60355; the proto field
# numbers in master_cache.py's JSON map 1:1 onto them in ascending order.
#   1 groupId            8  cautionWaitJumpCountMin    15 minSteamDensity   (float bits)
#   2 phaseType          9  cautionWaitJumpCountMax    16 maxSteamDensity   (float bits)
#   3 jumpCountFrom     10  cautionActiveJumpCountMin  17 minFallObjectSize (float bits)
#   4 gimmickTypes      11  cautionActiveJumpCountMax  18 maxFallObjectSize (float bits)
#   5 speedCoefPermil   12  gimmickActiveJumpCountMin
#   6 speedUpPermil     13  gimmickActiveJumpCountMax
#   7 speedDownPermil   14  invisibleGimmickType

NONE, SPEED_UP, SPEED_DOWN, REVERSE, INVISIBLE = 0, 1, 2, 3, 4
SPEED_UP_INV, SPEED_DOWN_INV, REVERSE_INV = 5, 6, 7

GIMMICK_NAME = {
    0: "NONE", 1: "SPEED_UP", 2: "SPEED_DOWN", 3: "REVERSE", 4: "INVISIBLE",
    5: "SPEED_UP+INV", 6: "SPEED_DOWN+INV", 7: "REVERSE+INV",
}


@dataclass
class PhaseGimmickRow:
    phase_type: int
    jump_count_from: int
    gimmick_types: tuple                       # ordered, as declared in master
    speed_permil: int
    speed_up_permil: int
    speed_down_permil: int
    caution_wait_min: int                      # -> GetCoolTime
    caution_wait_max: int
    caution_active_min: int                    # -> GetAppearTime
    caution_active_max: int
    gimmick_active_min: int                    # -> GetActiveTime
    gimmick_active_max: int
    invisible_gimmick_type: int
    min_steam: float
    max_steam: float
    min_fall: float
    max_fall: float


def _g(data: dict, key: str, default=0):
    v = data.get(key)
    return v[0] if v else default


def parse_rows(json_lines) -> list:
    rows = []
    for ln in json_lines:
        ln = ln.strip()
        if not ln:
            continue
        obj = json.loads(ln)
        d = obj["data"]
        raw_types = _g(d, "4", "")
        # packed repeated enum: one byte per value (all values < 128)
        types = tuple(ord(c) for c in raw_types) if isinstance(raw_types, str) else tuple(raw_types)
        rows.append((obj["group_id"], PhaseGimmickRow(
            phase_type=_g(d, "2"),
            jump_count_from=_g(d, "3"),
            gimmick_types=types,
            speed_permil=_g(d, "5"),
            speed_up_permil=_g(d, "6"),
            speed_down_permil=_g(d, "7"),
            caution_wait_min=_g(d, "8"),
            caution_wait_max=_g(d, "9"),
            caution_active_min=_g(d, "10"),
            caution_active_max=_g(d, "11"),
            gimmick_active_min=_g(d, "12"),
            gimmick_active_max=_g(d, "13"),
            invisible_gimmick_type=_g(d, "14"),
            min_steam=bits_to_f32(_g(d, "15")),
            max_steam=bits_to_f32(_g(d, "16")),
            min_fall=bits_to_f32(_g(d, "17")),
            max_fall=bits_to_f32(_g(d, "18")),
        )))
    return rows


def load_group(group_id: str) -> list:
    out = subprocess.run(
        [sys.executable, "master_cache.py", "JumpRopePhaseGimmick"],
        capture_output=True, text=True, encoding="utf-8", cwd=".",
    ).stdout.splitlines()
    rows = [r for gid, r in parse_rows(out) if gid == group_id]
    rows.sort(key=lambda r: r.jump_count_from)
    if not rows:
        raise SystemExit(f"no JumpRopePhaseGimmick rows for group {group_id!r}")
    return rows


# ------------------------------------------------------------------- the generator
@dataclass
class Loop:
    index: int                    # jump / rotation count, 0-based
    speed: float                  # OneLoopSequenceParam.speed  (multiplier)
    enable_gimmick: int           # currently-active gimmick type
    next_gimmick: int             # gimmick being telegraphed
    is_reverse: bool
    under: int                    # UnderType 0/1/2
    is_invisible: bool
    invisible_gimmick_type: int
    fall_object_size: float
    steam_density: float
    mesh_type: int
    fall_effect_type: int
    remain: int                   # currentGimmickRemainCount (counts down to 0)
    phase_type: int = 0


class SequenceGenerator:
    """GimmickSequenceGeneratorBase + PrecomputedSequenceGenerator, RVAs 0x5e9e...-0x5ea3..."""

    def __init__(self, seed: int, rows: list, max_jump_count: int = 999):
        self.rnd = NetRandom(seed)
        self.rows = rows
        self.max_jump_count = max_jump_count
        self.prev_fall_effect = 0        # _previousFallEffectType, +0x30

    # RVA 0x5e9f9e0 GetPhaseGimmickForJumpCount, composed with 0x5e9ee30 CalcPhase.
    # CalcPhase picks the first phase whose _phaseCount bound exceeds jumpCount (else the
    # max phase); GetPhaseGimmickForJumpCount then takes, inside that phase, the row with
    # the largest JumpCountFrom <= jumpCount.  Because every group's JumpCountFrom values
    # are globally monotone and partition the jump range by phase, that composition is
    # exactly the flat lookup below (an inference; see the module docstring).
    def row_for(self, jump_count: int) -> PhaseGimmickRow:
        best = self.rows[0]
        for r in self.rows:
            if r.jump_count_from <= jump_count:
                best = r
            else:
                break
        return best

    # RVA 0x5e9f920 GetCoolTime
    def get_cool_time(self, jc: int, gimmick: int) -> int:
        r = self.row_for(jc)
        lo, hi = r.caution_wait_min, r.caution_wait_max
        v = self.rnd.next(lo, hi + 1) if lo < hi else hi
        if gimmick in (REVERSE, REVERSE_INV):
            v = max(v, 1)
        return v

    # RVA 0x5e9f840 GetAppearTime
    def get_appear_time(self, jc: int, gimmick: int) -> int:
        r = self.row_for(jc)
        lo, hi = r.caution_active_min, r.caution_active_max
        v = self.rnd.next(lo, hi + 1) if lo < hi else hi
        if gimmick in (REVERSE, REVERSE_INV):
            v = max(v, 2)
        elif gimmick == NONE:
            v = max(v, 1)
        return v

    # RVA 0x5e9f760 GetActiveTime
    def get_active_time(self, jc: int, gimmick: int) -> int:
        if gimmick == NONE:
            return 0
        r = self.row_for(jc)
        lo, hi = r.gimmick_active_min, r.gimmick_active_max
        v = self.rnd.next(lo, hi + 1) if lo < hi else hi
        if gimmick in (REVERSE, REVERSE_INV):
            v = max(v, 1)
        return v

    # RVA 0x5e9ff20 SelectGimmick(jumpCount, excludeType)
    def select_gimmick(self, jc: int, exclude: int) -> int:
        types = self.row_for(jc).gimmick_types
        if not types:
            return NONE
        if exclude != NONE and len(types) > 1:
            n = sum(1 for t in types if t != exclude)
            if n > 0:
                r = self.rnd.next(0, n)
                for t in types:
                    if t == exclude:
                        continue
                    if r == 0:
                        return t
                    r -= 1
        return types[self.rnd.next(0, len(types))]

    # RVA 0x5e9fe80 SelectFallEffectType (also inlined at 0x5e9f54a)
    def select_fall_effect(self) -> int:
        prev = self.prev_fall_effect
        if prev == 0:
            v = self.rnd.next(1, 4)
            self.prev_fall_effect = v
            return v
        r = self.rnd.next(0, 2)
        t = 1
        while t < 4:
            if t != prev:
                if r == 0:
                    self.prev_fall_effect = t
                    return t
                r -= 1
            t += 1
        return prev

    # RVA 0x5e9f380 CreateSequenceParam
    def create_param(self, jc, gimmick, next_gimmick, remain, is_reversing, under,
                     sel_row) -> Loop:
        row = self.row_for(jc)
        base = f32(f32(row.speed_permil) / 1000.0)
        if gimmick in (SPEED_UP, SPEED_UP_INV):
            coef = f32(f32(row.speed_up_permil) / 1000.0)
        elif gimmick in (SPEED_DOWN, SPEED_DOWN_INV):
            coef = f32(f32(row.speed_down_permil) / 1000.0)
        else:
            coef = 1.0
        is_invisible = gimmick >= INVISIBLE
        row2 = sel_row if sel_row is not None else row

        if is_invisible:
            d = self.rnd.next_double()
            fall = f32(d * f32(row2.max_fall - row2.min_fall) + row2.min_fall)
            d = self.rnd.next_double()
            steam = f32(d * f32(row2.max_steam - row2.min_steam) + row2.min_steam)
            mesh = self.rnd.next(1, 4)
            fx = self.select_fall_effect()
        else:
            fall = steam = 0.0
            mesh = fx = 0

        return Loop(
            index=jc, speed=f32(coef * base), enable_gimmick=gimmick,
            next_gimmick=next_gimmick, is_reverse=bool(is_reversing), under=under,
            is_invisible=is_invisible, invisible_gimmick_type=row2.invisible_gimmick_type,
            fall_object_size=fall, steam_density=steam, mesh_type=mesh,
            fall_effect_type=fx, remain=remain, phase_type=row.phase_type,
        )

    # RVA 0x5ea2900 CalcOneLoopSequenceParams -- the whole run, precomputed.
    def generate(self, count: int | None = None) -> list:
        limit = self.max_jump_count if count is None else min(count, self.max_jump_count)
        out: list = []

        g = NONE            # _currentGimmickType
        ng = NONE           # _nextGimmickType
        prev_g = NONE       # _previousGimmickType (the SelectGimmick exclusion)
        is_rev = False      # _isReversing
        under = 0           # _currentUnder
        sel_row = None      # _selectionPhaseGimmick
        skip_cooldown = False   # `bl` at 0x5ea2b4a

        def emit(n):
            for i in range(n - 1, -1, -1):
                if len(out) >= limit:
                    return
                out.append(self.create_param(len(out), g, ng, i, is_rev, under, sel_row))

        while len(out) < limit:
            if not skip_cooldown:
                emit(self.get_cool_time(len(out), g))     # Default state
                if len(out) >= limit:
                    break
            skip_cooldown = False

            ng = self.select_gimmick(len(out), prev_g)     # TransitionFromCooldown
            sel_row = self.row_for(len(out))
            appear = self.get_appear_time(len(out), ng)
            if ng in (REVERSE, REVERSE_INV):
                emit(appear - 1)                           # Warning state
                is_rev = not is_rev
                under = 1 if is_rev else 2
                emit(1)
            else:
                emit(appear)
            if len(out) >= limit:
                break

            g = ng                                         # Active state
            active = self.get_active_time(len(out), g)
            if g in (REVERSE, REVERSE_INV):
                emit(active - 1)
                under = 0
                emit(1)
                cool1 = self.get_cool_time(len(out), g)    # Cooldown state
                emit(cool1)
                cool2 = self.get_cool_time(len(out), g)    # ResetToDefault
                prev_g, g, ng, sel_row = g, NONE, NONE, None
                emit(cool2)
                skip_cooldown = True
            else:
                emit(active)
                prev_g, g, ng, sel_row = g, NONE, NONE, None
        return out


    # RVA 0x5ea2e90 PrecomputedSequenceGenerator::GenerateNext(index)
    # Past the end of the precomputed list the generator cycles the tail:
    #   loops[_loopStartIndex + (index - _loopStartIndex) % (len - _loopStartIndex)]
    # _loopStartIndex is the first list index that exceeds the last phase's largest
    # JumpCountFrom (0x5ea2b5f-0x5ea2b87), i.e. 201 for score_attack_001.
    @staticmethod
    def generate_next(loops: list, index: int, loop_start_index: int):
        if index < len(loops):
            return loops[index]
        span = len(loops) - loop_start_index
        return loops[loop_start_index + (index - loop_start_index) % span]


# ------------------------------------------------------------------------- driver
def main() -> None:
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 567114856
    group = sys.argv[2] if len(sys.argv) > 2 else "jump_rope_phase_gimmick-jump_rope-score_attack_001"
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 40

    rows = load_group(group)
    gen = SequenceGenerator(seed, rows, max_jump_count=999)
    loops = gen.generate(n)

    print(f"seed={seed}  group={group}  rows={len(rows)}")
    print(f"{'idx':>4} {'ph':>2} {'speed':>7} {'active':>14} {'next':>14} "
          f"{'rev':>4} {'und':>3} {'inv':>3} {'rem':>3}  fall/steam/mesh/fx")
    for lp in loops:
        extra = ""
        if lp.is_invisible:
            extra = (f"  {lp.fall_object_size:.4f} {lp.steam_density:.4f} "
                     f"m{lp.mesh_type} fx{lp.fall_effect_type} it{lp.invisible_gimmick_type}")
        print(f"{lp.index:>4} {lp.phase_type:>2} {lp.speed:>7.4f} "
              f"{GIMMICK_NAME[lp.enable_gimmick]:>14} {GIMMICK_NAME[lp.next_gimmick]:>14} "
              f"{'Y' if lp.is_reverse else '.':>4} {lp.under:>3} "
              f"{'Y' if lp.is_invisible else '.':>3} {lp.remain:>3}{extra}")


if __name__ == "__main__":
    main()
