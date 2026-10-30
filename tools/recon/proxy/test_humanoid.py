"""humanoid.py: Mecanim muscles <-> local rotations on an Avatar rebuilt from the skeleton.
A synthetic straight T-pose (Unity space, facing +Z, left = -X, every local rotation identity)
stands in for the chibi rig, so nothing here needs the game's bundles.
    python -m pytest tools/recon/proxy/test_humanoid.py
"""
from __future__ import annotations

import math
import random

import humanoid as H

ID = [0.0, 0.0, 0.0, 1.0]


def _tpose():
    """{node: (local pos, local rot)}, {node: parent}, {human bone: node}."""
    nodes, parent, human = {}, {}, {}

    def add(name, par, pos, bone=None):
        nodes[name], parent[name] = (pos, ID), par
        if bone:
            human[bone] = name

    add("root", None, [0, 0, 0])
    add("hips", "root", [0, 0.57, 0.02], "Hips")
    add("spine", "hips", [0, 0.01, 0], "Spine")
    add("chest", "spine", [0, 0.11, 0], "Chest")
    add("neck", "chest", [0, 0.17, 0], "Neck")
    add("head", "neck", [0, 0.06, 0], "Head")
    for s, side in ((-1, "Left"), (1, "Right")):
        p = side[0]
        add(p + "sh", "chest", [s * 0.04, 0.13, 0], side + "Shoulder")
        add(p + "ua", p + "sh", [s * 0.07, 0, 0], side + "UpperArm")
        add(p + "la", p + "ua", [s * 0.16, 0, 0], side + "LowerArm")
        add(p + "ha", p + "la", [s * 0.13, 0, 0], side + "Hand")
        add(p + "ul", "hips", [s * 0.08, -0.07, 0], side + "UpperLeg")
        add(p + "ll", p + "ul", [0, -0.26, 0], side + "LowerLeg")
        add(p + "ft", p + "ll", [0, -0.19, 0], side + "Foot")
    return nodes, parent, human


def _axes():
    nodes, parent, human = _tpose()
    g = H.globals_of(nodes, parent)
    tpose = {b: g[n] for b, n in human.items()}
    par = {b: parent[n] for b, n in human.items()}
    for b, pn in par.items():
        tpose.setdefault(pn, g[pn])
    return H.build_axes(tpose, par)


def _angle(a, b):
    return math.degrees(2 * math.acos(min(1.0, abs(sum(x * y for x, y in zip(a, b))))))


def test_a_straight_t_pose_reads_unitys_zero_pose_muscles():
    # Unity's muscle zero pose is NOT the T-pose: a T-pose reads arms 0.4 down-up / 0.3
    # front-back, upper legs 0.6 front-back, elbows and knees fully stretched (as read off two
    # Unity-built Avatars)
    axes = _axes()
    m = H.local_to_muscles(axes, {b: ID for b in axes})
    for side in ("Left", "Right"):
        twist, fb, du = m[side + "UpperArm"]
        assert abs(du - 0.4) < 1e-6 and abs(fb - 0.3) < 1e-6 and abs(twist) < 1e-6
        assert abs(m[side + "UpperLeg"][2] - 0.6) < 1e-6
        assert abs(m[side + "LowerArm"][2] - 1.0) < 1e-6
        assert abs(m[side + "LowerLeg"][2] - 1.0) < 1e-6
    assert all(abs(v) < 1e-6 for v in m["Spine"] + m["Chest"] + m["Neck"])


def test_muscles_round_trip_through_local_rotations():
    axes = _axes()
    rng = random.Random(20261022)
    for _ in range(50):
        mus = [rng.uniform(-0.9, 0.9) for _ in range(95)]
        back = H.local_to_muscles(axes, H.muscles_to_local(axes, mus))
        for b, vals in back.items():
            for ax, v in enumerate(vals):
                if v is not None:
                    assert abs(v - mus[H.BONES[b][0][ax]]) < 1e-6, (b, ax)


def test_left_and_right_mirror_each_other():
    # the same muscle values on both sides give mirror-image rotations (x -> -x in Unity space:
    # a quaternion (x, y, z, w) mirrors to (x, -y, -z, w))
    axes = _axes()
    mus = [0.0] * 95
    for i, v in ((39, 0.7), (40, -0.4), (41, 0.3), (48, 0.7), (49, -0.4), (50, 0.3),
                 (21, -0.8), (22, 0.2), (29, -0.8), (30, 0.2), (24, 0.5), (32, 0.5)):
        mus[i] = v
    loc = H.muscles_to_local(axes, mus)
    for bone in ("UpperArm", "UpperLeg", "LowerLeg"):
        lq, rq = loc["Left" + bone], loc["Right" + bone]
        assert _angle([lq[0], -lq[1], -lq[2], lq[3]], rq) < 1e-4, bone


def test_baking_the_build_pose_keeps_the_skeleton_where_it_is():
    nodes, parent, human = _tpose()
    axes = _axes()
    m = H.local_to_muscles(axes, {b: ID for b in axes})
    mus = [0.0] * 95
    for b, vals in m.items():
        for ax, v in enumerate(vals):
            if v is not None:
                mus[H.BONES[b][0][ax]] = v
    g = H.globals_of(nodes, parent)
    com, _q = H.body_of({b: g[n][0] for b, n in human.items()})
    frame = [0.0] * 137
    frame[6] = 1.0                                    # MotionQ identity
    frame[7:10] = [c / com[1] for c in com]           # RootT = the build-pose body / human scale
    frame[10:14] = [0.0, 0.0, 0.0, 1.0]
    frame[42:137] = mus
    out = H.bake(nodes, parent, human, [frame, frame])
    for n, qs in out["rot"].items():
        assert _angle(qs[0], ID) < 1e-3, n
    # in place: the body's x / z are centred on the clip's mean, so the hips move by -CoM.xz
    hp, h0 = out["hips_pos"][0], nodes["hips"][0]
    want = [h0[0] - com[0], h0[1], h0[2] - com[2]]
    assert all(abs(a - b) < 1e-6 for a, b in zip(hp, want)), (hp, want)


def test_the_games_twist_split_moves_forearm_twist_to_the_hand_and_keeps_the_hand():
    # the game's HumanDescription (Ghidra, VisionActorSetupUtility::SetupAnimation): lower arm /
    # lower leg keep NONE of their twist (0), upper arm / leg keep all (1.0). The hand's and foot's
    # GLOBAL orientation must not change; the forearm then carries no twist of its own
    nodes, parent, human = _tpose()
    axes = _axes()
    mus = [0.0] * 95
    for i, v in ((43, 0.6), (52, -0.5), (25, 0.4), (33, 0.7), (42, 0.3), (51, 0.3), (24, 0.5), (32, 0.5)):
        mus[i] = v
    plain = H.muscles_to_local(axes, mus)
    split = H.muscles_to_local(axes, mus, H.GAME_TWIST)

    def glob(loc):
        return H.globals_of(nodes, parent, {human[b]: q for b, q in loc.items() if b != "Hips"})

    gp, gs = glob(plain), glob(split)
    for b in ("LeftHand", "RightHand", "LeftFoot", "RightFoot"):
        assert _angle(gp[human[b]][1], gs[human[b]][1]) < 1e-6, b
    for b in ("LeftLowerArm", "RightLowerArm", "LeftLowerLeg", "RightLowerLeg"):
        assert abs(H.local_to_muscles(axes, {b: split[b]})[b][0]) < 1e-6, b     # no twist left
        assert _angle(plain[b], split[b]) > 1.0, b                              # it did move
    for b in ("LeftUpperArm", "RightUpperArm", "LeftUpperLeg", "RightUpperLeg"):
        assert _angle(plain[b], split[b]) < 1e-9, b                             # split 1.0: untouched
