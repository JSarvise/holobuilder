"""Read the game's own chibi bone map (bone name -> Unity HumanBodyBones) out of the binary, and
check `chibi_build.HUMAN` against it. Offline, ~10 s, no disassembler session.

Where the map lives: `VisionActorDefine::.cctor` (RVA 0x5ac7c60, method
map) fills a Dictionary<string, HumanBodyBones> with `Add(dict, name, value)` calls:
`mov r8d, <HumanBodyBones>` (or `xor r8d, r8d` for 0 = Hips) and `mov rdx, [rip + <slot>]`,
where <slot> is the string literal's runtime-metadata slot. The slots hold heap pointers in the
memory dump and zeros in the packed DLL, so a slot is named by RANK: string-literal slots are
laid out in string-literal index order (slot order == literal order, checked on six bone
names), and the literal table (decrypted `global-metadata.dat`, `decrypt_metadata.py`) is
sorted. So the k-th lowest slot of these Add calls is the k-th bone-name literal in index
order. The rank pairing is only accepted when it is CONSISTENT: every pair must give the same
human bone the name says and the count must match -- two independent orders agreeing.

    python chibi_human_map.py            # prints the map, exits 1 on any disagreement

Inputs: dumps/v7/GameAssembly_dumped.bin (the unpacked image, file offset == rva) and the
installed global-metadata.dat. Re-run after a game update: if the cctor moved, pass --rva
(method_map.tsv, `VisionActorDefine .cctor`).
"""
from __future__ import annotations

import argparse
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

DUMP = os.path.join(HERE, "dumps", "v7", "GameAssembly_dumped.bin")
METHOD_MAP = os.path.join(HERE, "dumps", "v7", "method_map.tsv")
CCTOR = ("VisionActorDefine", ".cctor")
BONE_LITERAL = re.compile(rb"jnt_[CLR]_[A-Za-z]+00_0\d")
# UnityEngine.HumanBodyBones (Unity 6, stable since 2017)
HUMAN_BODY_BONES = [
    "Hips", "LeftUpperLeg", "RightUpperLeg", "LeftLowerLeg", "RightLowerLeg", "LeftFoot", "RightFoot",
    "Spine", "Chest", "Neck", "Head", "LeftShoulder", "RightShoulder", "LeftUpperArm", "RightUpperArm",
    "LeftLowerArm", "RightLowerArm", "LeftHand", "RightHand", "LeftToes", "RightToes", "LeftEye",
    "RightEye", "Jaw",
    "LeftThumbProximal", "LeftThumbIntermediate", "LeftThumbDistal",
    "LeftIndexProximal", "LeftIndexIntermediate", "LeftIndexDistal",
    "LeftMiddleProximal", "LeftMiddleIntermediate", "LeftMiddleDistal",
    "LeftRingProximal", "LeftRingIntermediate", "LeftRingDistal",
    "LeftLittleProximal", "LeftLittleIntermediate", "LeftLittleDistal",
    "RightThumbProximal", "RightThumbIntermediate", "RightThumbDistal",
    "RightIndexProximal", "RightIndexIntermediate", "RightIndexDistal",
    "RightMiddleProximal", "RightMiddleIntermediate", "RightMiddleDistal",
    "RightRingProximal", "RightRingIntermediate", "RightRingDistal",
    "RightLittleProximal", "RightLittleIntermediate", "RightLittleDistal",
    "UpperChest",
]


SETUP = ("VisionActorSetupUtility", "SetupAnimation")
TWIST_SETTERS = {"set_upperArmTwist": "UpperArm", "set_lowerArmTwist": "LowerArm",
                 "set_upperLegTwist": "UpperLeg", "set_lowerLegTwist": "LowerLeg"}


def _rva_of(cls: str, method: str) -> list[int]:
    out = []
    for line in open(METHOD_MAP, encoding="utf-8"):
        f = line.rstrip("\n").split("\t")
        if len(f) >= 5 and (f[3], f[4]) == (cls, method):
            out.append(int(f[0], 16))
    return out


def twist_split(span: int = 0x3000) -> dict[str, float]:
    """The HumanDescription twist split the game's actor setup passes to BuildHumanAvatar
    (`VisionActorSetupUtility::SetupAnimation`): for each call to a twist setter, the float last
    put in xmm1 (`movss xmm1, [rip+c]` read from the image, or `xorps xmm1, xmm1` = 0). The
    setters are found by RVA (identical-code folding gives them other names in a decompile)."""
    import capstone  # noqa: PLC0415

    image = open(DUMP, "rb").read()
    setters = {_rva_of("HumanDescription", m)[0]: part for m, part in TWIST_SETTERS.items()}
    rva = _rva_of(*SETUP)[0]
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    out, xmm1 = {}, None
    for ins in md.disasm(image[rva:rva + span], rva):
        ops = ins.operands
        if ins.mnemonic in ("movss", "movd") and ops and ins.reg_name(ops[0].reg) == "xmm1":
            if ops[1].type == capstone.CS_OP_MEM and ins.reg_name(ops[1].mem.base) == "rip":
                xmm1 = struct.unpack_from("<f", image, ins.address + ins.size + ops[1].mem.disp)[0]
            else:
                xmm1 = None
        elif ins.mnemonic in ("xorps", "pxor") and ins.op_str == "xmm1, xmm1":
            xmm1 = 0.0
        elif ins.mnemonic == "call" and ops and ops[0].type == capstone.CS_OP_IMM:
            part = setters.get(ops[0].imm)
            if part is not None and xmm1 is not None and part not in out:
                out[part] = round(xmm1, 6)
    return out


def cctor_rva() -> int:
    for line in open(METHOD_MAP, encoding="utf-8"):
        f = line.rstrip("\n").split("\t")
        if len(f) >= 5 and (f[3], f[4]) == CCTOR:
            return int(f[0], 16)
    raise KeyError("VisionActorDefine .cctor not in method_map.tsv")


def add_calls(image: bytes, rva: int, span: int = 0x2000) -> list[tuple[int, int]]:
    """[(literal slot rva, HumanBodyBones value)] of the Dictionary.Add calls, in code order:
    every call preceded (since the previous call) by `mov rdx, [rip+slot]` and an r8d value."""
    import capstone  # noqa: PLC0415

    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    out, rdx, r8, target = [], None, None, None
    for ins in md.disasm(image[rva:rva + span], rva):
        if ins.mnemonic == "ret":
            break
        ops = ins.operands
        if ins.mnemonic == "mov" and len(ops) == 2 and ops[0].type == capstone.CS_OP_REG:
            reg = ins.reg_name(ops[0].reg)
            if reg == "rdx" and ops[1].type == capstone.CS_OP_MEM and ins.reg_name(ops[1].mem.base) == "rip":
                rdx = ins.address + ins.size + ops[1].mem.disp
            elif reg == "r8d" and ops[1].type == capstone.CS_OP_IMM:
                r8 = ops[1].imm
        elif ins.mnemonic == "xor" and ins.op_str == "r8d, r8d":
            r8 = 0
        elif ins.mnemonic == "call":
            if rdx is not None and r8 is not None:
                if target is None:
                    target = ins.op_str
                if ins.op_str == target:           # the same Add every time
                    out.append((rdx, r8))
            rdx = r8 = None
    return out


def bone_literals(metadata_path: str) -> list[str]:
    """The bone-name string literals, in literal-index order."""
    import decrypt_metadata as dm  # noqa: PLC0415

    raw = open(metadata_path, "rb").read()
    key = dm.KNOWN_KEY
    probe = bytes(raw[i] ^ key[i % dm.L] for i in range(4))
    if struct.unpack_from("<I", probe)[0] != dm.MAGIC:
        key = dm.recover_key(raw)
    key, _ = dm.refine_by_literal_text(raw, key)
    dec = bytes(raw[i] ^ key[i % dm.L] for i in range(len(raw)))
    lo, _ls, ln = struct.unpack_from("<3i", dec, 8)
    do, dsz, _dn = struct.unpack_from("<3i", dec, 20)
    idx = struct.unpack_from(f"<{ln}I", dec, lo) + (dsz,)
    if any(b < a for a, b in zip(idx, idx[1:])):
        raise ValueError("string-literal index table not monotone: metadata not cleanly decrypted")
    lits = [dec[do + idx[i]:do + idx[i + 1]] for i in range(ln)]
    return [l.decode() for l in lits if BONE_LITERAL.fullmatch(l)]


def by_name(name: str) -> str | None:
    """The human bone a chibi joint's NAME says (the independent check)."""
    m = re.fullmatch(r"jnt_([CLR])_([A-Za-z]+)00_0(\d)", name)
    if not m:
        return None
    side = {"L": "Left", "R": "Right", "C": ""}[m.group(1)]
    part, k = m.group(2), int(m.group(3))
    centre = {("hips", 0): "Hips", ("spine", 0): "Spine", ("spine", 1): "Chest", ("neck", 0): "Neck",
              ("head", 0): "Head"}
    if not side:
        return centre.get((part, k))
    limb = {"shoulder": "Shoulder", "upperArm": "UpperArm", "foreArm": "LowerArm", "hand": "Hand",
            "thigh": "UpperLeg", "leg": "LowerLeg", "foot": "Foot", "eye": "Eye"}
    if part in limb and k == 0:
        return side + limb[part]
    finger = {"fingerThumb": "Thumb", "fingerIndex": "Index", "fingerMiddle": "Middle",
              "fingerRing": "Ring", "fingerPinky": "Little"}
    if part in finger:
        return side + finger[part] + ("Proximal", "Intermediate", "Distal")[k]
    return None


def read_map(metadata_path: str, rva: int | None = None) -> dict[str, str]:
    """{joint name: HumanBodyBones name}, the game's own, or ValueError when the rank pairing
    and the names disagree."""
    image = open(DUMP, "rb").read()
    calls = add_calls(image, rva if rva is not None else cctor_rva())
    names = [n for n in bone_literals(metadata_path) if by_name(n)]      # the hair anchor drops out
    slots = sorted(calls)
    if len(slots) != len(names):
        raise ValueError(f"{len(slots)} Add calls for {len(names)} bone-name literals")
    out, bad = {}, []
    for (slot, value), name in zip(slots, names):
        bone = HUMAN_BODY_BONES[value]
        out[name] = bone
        if bone != by_name(name):
            bad.append(f"{name}: code says {bone}, name says {by_name(name)}")
    if bad:
        raise ValueError("rank pairing disagrees with the names: " + "; ".join(bad))
    return out


def main(argv=None) -> int:
    import game_paths  # noqa: PLC0415

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--metadata", default=os.path.join(game_paths.game_root(), "hololive-Dreams_Data",
                                                        "il2cpp_data", "Metadata", "global-metadata.dat"))
    ap.add_argument("--rva", type=lambda s: int(s, 16), default=None)
    a = ap.parse_args(argv)
    try:
        game = read_map(a.metadata, a.rva)
    except ValueError as e:
        print(f"FAIL: {e}")
        return 1
    import chibi_build  # noqa: PLC0415

    # humanoid.py names the finger joints 1/2/3 (HumanTrait style); Unity's enum Proximal/...
    unity = lambda b: re.sub(r"([A-Za-z]+?)([123])$",
                             lambda m: m.group(1) + ("Proximal", "Intermediate", "Distal")[int(m.group(2)) - 1], b)
    ours = {n: unity(b) for b, n in chibi_build.HUMAN.items()}
    for name, bone in sorted(game.items(), key=lambda kv: HUMAN_BODY_BONES.index(kv[1])):
        mark = "" if ours.get(name) == bone else ("  (not animated here)" if name not in ours else "  MISMATCH")
        print(f"  {bone:24} {name}{mark}")
    import humanoid  # noqa: PLC0415

    twist = twist_split()
    print(f"twist split read from the game: {twist}  (humanoid.GAME_TWIST {humanoid.GAME_TWIST})")
    if twist != humanoid.GAME_TWIST:
        print("MISMATCH: humanoid.GAME_TWIST differs from the game's")
        return 1
    mismatched = [n for n in game if n in ours and ours[n] != game[n]]
    missing = [n for n in ours if n not in game]
    print(f"{len(game)} bones in the game's map; chibi_build.HUMAN: {len(ours)} "
          f"({len(mismatched)} mismatched, {len(missing)} not in the game's map)")
    return 1 if mismatched or missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
