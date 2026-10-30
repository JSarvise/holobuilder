"""Unity Mecanim humanoid muscles -> local bone rotations, with the Avatar's axes rebuilt from
the skeleton's build pose the way AvatarBuilder does it. chibi_build.py uses it to turn the
game's humanoid run motion into per-bone keyframes: the game ships its chibis WITHOUT a
humanoid Avatar (it builds one at runtime), so the axes are rebuilt here.

Sources:
  * clip layout -- attr 0-6 MotionT/Q, 7-13 RootT/Q, 14-41 IK goals, 42-136 the 95 muscles in
    HumanTrait.MuscleName order: from AssetRipper HumanoidMuscleType.cs, and confirmed on the
    game's run clip (its constant curves are exactly UpperChest, eyes/jaw and toes, the bones
    this rig lacks).
  * muscle -> rotation (LimitUnproject, halfTan, ZYRoll2Quat, PreQ * zy * conj(PostQ)): as in
    two reimplementations (fluorite.moe rig/humanoid.rs, Ruri.RipperHook
    AvatarMuscleReferential.cs); the tan-half-angle form checked against Unity's zero pose.
  * PreQ / PostQ / m_Sgn from the skeleton (`build_axes`): reproduces two Unity-built Avatars
    (VSeeFaceSDK Vita, TrackLab ViRe) to <= 0.031 deg, every sign exact.
  * hips from RootT / RootQ (`bake`): the fluorite body_fix / Ruri BodyTransform recipe; the
    per-bone centre-of-mass points are Ruri's rule, matching within 0.7 mm.
  * the game's own HumanDescription (VisionActorSetupUtility::SetupAnimation in the client):
    default limits and the twist split in GAME_TWIST are read from it; its skeleton is the
    model's default bone transforms (the prefab's T-pose, inferred from the name); the set of
    mapped bone names comes from the decrypted metadata literals, each name's human bone read
    from its name.

Everything is in UNITY space (left-handed, +Y up, character faces +Z, its left side is -X).
Convert to glTF/three.js only at the very end: p -> (-x, y, z), q -> (x, -y, -z, w).
Quaternions are [x, y, z, w]. stdlib only.
"""
from __future__ import annotations
import math

# ---------- quaternion helpers ----------
def qmul(a, b):
    ax, ay, az, aw = a; bx, by, bz, bw = b
    return [aw*bx + ax*bw + ay*bz - az*by, aw*by - ax*bz + ay*bw + az*bx,
            aw*bz + ax*by - ay*bx + az*bw, aw*bw - ax*bx - ay*by - az*bz]
def qconj(q): return [-q[0], -q[1], -q[2], q[3]]
def qnorm(q):
    l = math.sqrt(sum(c*c for c in q)); return [c/l for c in q]
def qrot(q, v): return qmul(qmul(q, [*v, 0.0]), qconj(q))[:3]
def sub(a, b): return [a[i]-b[i] for i in range(3)]
def vnorm(v):
    l = math.sqrt(sum(c*c for c in v)); return [c/l for c in v]
def cross(a, b): return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]
def quat_from_cols(x, y, z):
    m00, m11, m22 = x[0], y[1], z[2]; tr = m00 + m11 + m22
    if tr > 0:
        s = math.sqrt(tr + 1) * 2; return qnorm([(y[2]-z[1])/s, (z[0]-x[2])/s, (x[1]-y[0])/s, 0.25*s])
    if m00 > m11 and m00 > m22:
        s = math.sqrt(1 + m00 - m11 - m22) * 2; return qnorm([0.25*s, (y[0]+x[1])/s, (z[0]+x[2])/s, (y[2]-z[1])/s])
    if m11 > m22:
        s = math.sqrt(1 + m11 - m00 - m22) * 2; return qnorm([(y[0]+x[1])/s, 0.25*s, (z[1]+y[2])/s, (z[0]-x[2])/s])
    s = math.sqrt(1 + m22 - m00 - m11) * 2; return qnorm([(z[0]+x[2])/s, (z[1]+y[2])/s, 0.25*s, (x[1]-y[0])/s])

# ---------- Mecanim tables (from unidot_importer human_trait.gd / AssetRipper MuscleType) ----------
MUSCLE_MIN = [-40,-40,-40,-40,-40,-40,-20,-20,-20,-40,-40,-40,-40,-40,-40,
              -10,-20,-10,-20,-10,-10,
              -90,-60,-60,-80,-90,-50,-30,-50,
              -90,-60,-60,-80,-90,-50,-30,-50,
              -15,-15,-60,-100,-90,-80,-90,-80,-40,
              -15,-15,-60,-100,-90,-80,-90,-80,-40] + \
             [-20,-25,-40,-40, -50,-20,-45,-45, -50,-7.5,-45,-45, -50,-7.5,-45,-45, -50,-20,-45,-45] * 2
MUSCLE_MAX = [40,40,40,40,40,40,20,20,20,40,40,40,40,40,40,
              15,20,15,20,10,10,
              50,60,60,80,90,50,30,50,
              50,60,60,80,90,50,30,50,
              30,15,100,100,90,80,90,80,40,
              30,15,100,100,90,80,90,80,40] + \
             [20,25,35,35, 50,20,45,45, 50,7.5,45,45, 50,7.5,45,45, 50,20,45,45] * 2

R, U, F = [1, 0, 0], [0, 1, 0], [0, 0, 1]
L, D = [-1, 0, 0], [0, -1, 0]
t = lambda deg: math.tan(math.radians(deg))
# name: (muscle index for x,y,z or -1, aim, reference axis r, zero-pose W halftan (left side), m_Sgn (Unity, measured))
#   aim = list of human child bones (first present wins) | fixed vector | 'parent' (reuse parent's X)
BONES = {
 'Hips':       ((-1,-1,-1), ['Spine'], L, (0,0,0), (1,1,1)),
 'Spine':      ((2,1,0),  ['Chest','UpperChest','Neck'], L, (0,0,0), (1,1,1)),
 'Chest':      ((5,4,3),  ['UpperChest','Neck'], L, (0,0,0), (1,1,1)),
 'UpperChest': ((8,7,6),  ['Neck'], L, (0,0,0), (1,1,1)),
 'Neck':       ((11,10,9),['Head'], L, (0,0,0), (1,1,1)),
 'Head':       ((14,13,12), U, L, (0,0,0), (1,1,1)),
 'LeftEye':    ((-1,16,15), F, R, (0,0,0), (1,1,-1)),
 'RightEye':   ((-1,18,17), F, R, (0,0,0), (1,-1,-1)),
 'LeftUpperLeg':  ((23,22,21), ['LeftLowerLeg'], R, (-t(15),0,0), (1,1,1)),
 'RightUpperLeg': ((31,30,29), ['RightLowerLeg'], R, (-t(15),0,0), (-1,-1,1)),
 'LeftLowerLeg':  ((25,-1,24), ['LeftFoot'], R, (t(40),0,0), (1,1,-1)),
 'RightLowerLeg': ((33,-1,32), ['RightFoot'], R, (t(40),0,0), (-1,1,-1)),
 'LeftFoot':   ((-1,27,26), D, R, (0,0,0), (1,1,1)),
 'RightFoot':  ((-1,35,34), D, R, (0,0,0), (1,-1,1)),
 'LeftToes':   ((-1,-1,28), F, R, (0,0,0), (1,1,1)),
 'RightToes':  ((-1,-1,36), F, R, (0,0,0), (1,1,1)),
 'LeftShoulder':  ((-1,38,37), ['LeftUpperArm'], F, (0,0,0), (1,1,-1)),
 'RightShoulder': ((-1,47,46), ['RightUpperArm'], F, (0,0,0), (1,1,1)),
 'LeftUpperArm':  ((41,40,39), ['LeftLowerArm'], F, (0,t(15),t(20)), (1,1,-1)),
 'RightUpperArm': ((50,49,48), ['RightLowerArm'], F, (0,t(15),t(20)), (-1,1,1)),
 'LeftLowerArm':  ((43,-1,42), ['LeftHand'], U, (0,t(40),0), (1,1,-1)),
 'RightLowerArm': ((52,-1,51), ['RightHand'], U, (0,t(40),0), (-1,1,1)),
 'LeftHand':   ((-1,45,44), 'parent', F, (0,0,0), (1,1,-1)),
 'RightHand':  ((-1,54,53), 'parent', F, (0,0,0), (1,1,1)),
}
# fingers: muscles 55 + 20*side + 4*finger + [1 Stretched, Spread, 2 Stretched, 3 Stretched]
_FSGN = {  # measured m_Sgn per (side, finger, phalanx)
 ('Left','Thumb'): [(-1,-1,1),(-1,1,1),(-1,1,1)], ('Right','Thumb'): [(1,-1,-1),(1,1,-1),(1,1,-1)],
 ('Left','Index'): [(-1,-1,-1),(-1,1,-1),(-1,1,-1)], ('Right','Index'): [(1,-1,1),(1,1,1),(1,1,1)],
 ('Left','Middle'):[(-1,-1,-1),(-1,1,-1),(-1,1,-1)], ('Right','Middle'):[(1,-1,1),(1,1,1),(1,1,1)],
 ('Left','Ring'):  [(-1,1,-1),(-1,1,-1),(-1,1,-1)], ('Right','Ring'):  [(1,1,1),(1,1,1),(1,1,1)],
 ('Left','Little'):[(-1,1,-1),(-1,1,-1),(-1,1,-1)], ('Right','Little'):[(1,1,1),(1,1,1),(1,1,1)],
}
for si, side in enumerate(('Left', 'Right')):
    for fi, (fing, spread_w) in enumerate((('Thumb', None), ('Index', .08), ('Middle', .04), ('Ring', -.04), ('Little', -.08))):
        base = 55 + 20*si + 4*fi
        names = [f'{side}{fing}{k}' for k in (1, 2, 3)]
        r = U if fing == 'Thumb' else F
        w = [(0, .125, .125), (0, -.2, 0), (0, -.2, 0)] if fing == 'Thumb' else [(0, spread_w, .3), (0, 0, .33), (0, 0, .33)]
        mus = [(-1, base+1, base), (-1, -1, base+2), (-1, -1, base+3)]
        for k in range(3):
            aim = [names[k+1]] if k < 2 else 'parent'
            BONES[names[k]] = (mus[k], aim, r, w[k], _FSGN[(side, fing)][k])

PARENT_CHAIN_ORDER = list(BONES)  # parents before children


def build_axes(tpose_global: dict[str, tuple[list, list]], parent_of: dict[str, str]):
    """tpose_global[bone] = (pos, rot) in Animator(root) space; parent_of[bone] = the bone's
    actual parent Transform name (any node; must also be in tpose_global).
    Returns {bone: (preQ, postQ, sgn)} — PreQ is relative to the parent TRANSFORM's rotation,
    PostQ to the bone's own rotation (UnityCsReference AvatarMuscleEditor.DrawMuscleHandle)."""
    out, xdir = {}, {}
    for b in PARENT_CHAIN_ORDER:
        if b not in tpose_global:
            continue
        mus, aim, r, w, sgn = BONES[b]
        pos, rot = tpose_global[b]
        if aim == 'parent':
            X = xdir[next(p for p in _human_ancestors(b) if p in xdir)]
        elif isinstance(aim[0], str):
            child = next((c for c in aim if c in tpose_global), None)
            X = vnorm(sub(tpose_global[child][0], pos))
        else:
            X = aim
        xdir[b] = X
        Y = vnorm(cross(r, X)); Z = cross(X, Y)
        frame = quat_from_cols(X, Y, Z)
        if b.startswith('Right'):
            w = (w[0], -w[1], -w[2])            # mirror across the YZ plane
        W = qnorm([w[0], w[1], w[2], 1.0])
        post = qmul(qconj(rot), frame)
        prot = tpose_global[parent_of[b]][1]
        pre = qmul(qconj(prot), qmul(W, frame))
        out[b] = (pre, post, sgn)
    return out


def _human_ancestors(b):
    if b.endswith('3'): return [b[:-1] + '2']
    if b.endswith('Hand'): return [b.replace('Hand', 'LowerArm')]
    return []


def half_tan(a):
    return math.tan(max(-1.5, min(1.5, 0.5 * a)))   # clamp as in Mecanim halfTan


# The game's HumanDescription twist split for its chibis (read from the client's
# VisionActorSetupUtility::SetupAnimation, RVA 0x5acf950, which feeds AvatarBuilder.
# BuildHumanAvatar: upperArmTwist 1.0, lowerArmTwist 0, upperLegTwist 1.0, lowerLegTwist 0,
# arm/legStretch 0.05, feetSpacing 0, no translation DoF; limits useDefaultValues). Unity's
# defaults are 0.5. `twist[bone]` is the share of its own Twist muscle the bone keeps; the rest
# goes to its child, whose global orientation does not change (Ruri.RipperHook's twist solve).
GAME_TWIST = {"UpperArm": 1.0, "LowerArm": 0.0, "UpperLeg": 1.0, "LowerLeg": 0.0}
_TWIST_CHILD = {"LowerArm": "Hand", "UpperArm": "LowerArm", "LowerLeg": "Foot", "UpperLeg": "LowerLeg"}


def _zy_local(pre, post, sgn, mus, muscles95, twist_scale=1.0):
    h = []
    for ax in range(3):
        m = mus[ax]
        if m < 0:
            h.append(0.0); continue
        v = muscles95[m]
        ang = math.radians(v * (MUSCLE_MAX[m] if v >= 0 else -MUSCLE_MIN[m]))  # LimitUnproject
        if ax == 0:
            ang *= twist_scale
        h.append(half_tan(sgn[ax] * ang))
    x, y, z = h
    zy = qnorm([x, y + x*z, z - x*y, 1.0])                # ZYRoll2Quat = swing(0,y,z) * twist(x)
    return qnorm(qmul(qmul(pre, zy), qconj(post)))


def muscles_to_local(axes, muscles95, twist=None):
    """muscles95: 95 floats in HumanTrait.MuscleName order (clip attribute 42+i).
    Returns {bone: local rotation (relative to its parent Transform)}. `twist`: the
    HumanDescription twist split ({"LowerArm": 0.0, ...}, see GAME_TWIST); None keeps every
    twist on its own bone (a split of 1.0 everywhere)."""
    out = {}
    for b, (pre, post, sgn) in axes.items():
        out[b] = _zy_local(pre, post, sgn, BONES[b][0], muscles95)
    for side in ("Left", "Right"):
        # child first, as Ruri: LowerArm->Hand before UpperArm->LowerArm (legs alike)
        for part in ("LowerArm", "UpperArm", "LowerLeg", "UpperLeg"):
            f = (twist or {}).get(part, 1.0)
            b, c = side + part, side + _TWIST_CHILD[part]
            if f == 1.0 or b not in axes or c not in out:
                continue
            pre, post, sgn = axes[b]
            new = _zy_local(pre, post, sgn, BONES[b][0], muscles95, twist_scale=f)
            delta = qmul(qconj(out[b]), new)
            out[b] = new
            out[c] = qnorm(qmul(qconj(delta), out[c]))
    return out


def local_to_muscles(axes, local):
    """Inverse (ToAxes), for self-checks: a straight T-pose should give Arm Down-Up 0.4,
    Arm Front-Back 0.3, Upper Leg Front-Back 0.6, Forearm/Lower Leg Stretch 1.0, spine 0."""
    res = {}
    for b, q in local.items():
        pre, post, sgn = axes[b]
        zy = qmul(qmul(qconj(pre), q), post)
        if zy[3] < 0: zy = [-c for c in zy]
        x, Y, Z = (zy[i] / zy[3] for i in range(3))
        y = (Y - x*Z) / (1 + x*x); z = (Z + x*Y) / (1 + x*x)
        vals = []
        for ax, tv in enumerate((x, y, z)):
            m = BONES[b][0][ax]
            ang = math.degrees(2 * math.atan(tv)) * sgn[ax]
            vals.append(None if m < 0 else ang / (MUSCLE_MAX[m] if ang >= 0 else -MUSCLE_MIN[m]))
        res[b] = vals
    return res


# ---------- the body: centre of mass + orientation (RootT / RootQ) ----------
# Unity's default m_HumanBoneMass; bones the rig lacks (UpperChest, Toes, eyes) simply drop
# out of the weighted mean (how Unity renormalises there is not known)
MASS = {'Hips': .145455, 'LeftUpperLeg': .121212, 'RightUpperLeg': .121212,
        'LeftLowerLeg': .048485, 'RightLowerLeg': .048485, 'LeftFoot': .009697, 'RightFoot': .009697,
        'Spine': .030303, 'Chest': .145455, 'UpperChest': .145455, 'Neck': .012121, 'Head': .048485,
        'LeftShoulder': .006061, 'RightShoulder': .006061, 'LeftUpperArm': .024242, 'RightUpperArm': .024242,
        'LeftLowerArm': .018182, 'RightLowerArm': .018182, 'LeftHand': .006061, 'RightHand': .006061,
        'LeftToes': .002424, 'RightToes': .002424}
_CHILD = {'Spine': ['Chest', 'UpperChest', 'Neck'], 'Chest': ['UpperChest', 'Neck'], 'UpperChest': ['Neck'],
          'Neck': ['Head'], 'LeftUpperLeg': ['LeftLowerLeg'], 'RightUpperLeg': ['RightLowerLeg'],
          'LeftLowerLeg': ['LeftFoot'], 'RightLowerLeg': ['RightFoot'], 'LeftFoot': ['LeftToes'],
          'RightFoot': ['RightToes'], 'LeftShoulder': ['LeftUpperArm'], 'RightShoulder': ['RightUpperArm'],
          'LeftUpperArm': ['LeftLowerArm'], 'RightUpperArm': ['RightLowerArm'],
          'LeftLowerArm': ['LeftHand'], 'RightLowerArm': ['RightHand']}


def body_of(pos: dict) -> tuple[list, list]:
    """(centre of mass, orientation) of a pose: pos = {human bone: global position}."""
    num, den = [0.0, 0.0, 0.0], 0.0
    for b, m in MASS.items():
        if b not in pos:
            continue
        if b == 'Hips':
            pts = [pos[k] for k in ('LeftUpperLeg', 'RightUpperLeg', 'Spine') if k in pos]
            c = [sum(p[i] for p in pts) / len(pts) for i in range(3)]
        elif b == 'UpperChest':
            pts = [pos[k] for k in ('UpperChest', 'Neck', 'LeftShoulder', 'RightShoulder') if k in pos]
            c = [sum(p[i] for p in pts) / len(pts) for i in range(3)]
        else:
            ch = next((k for k in _CHILD.get(b, []) if k in pos), None)
            c = pos[b] if ch is None else [(pos[b][i] + pos[ch][i]) / 2 for i in range(3)]
        num = [num[i] + m * c[i] for i in range(3)]
        den += m
    com = [v / den for v in num]
    LUA, RUA, LUL, RUL = (pos[k] for k in ('LeftUpperArm', 'RightUpperArm', 'LeftUpperLeg', 'RightUpperLeg'))
    up = vnorm([LUA[i] + RUA[i] - LUL[i] - RUL[i] for i in range(3)])
    right = [RUL[i] - LUL[i] + RUA[i] - LUA[i] for i in range(3)]
    fwd = vnorm(cross(right, up))
    right = cross(up, fwd)
    return com, quat_from_cols(right, up, fwd)


# ---------- a skeleton: local transforms -> globals ----------
def globals_of(nodes: dict, parent: dict, local_rot: dict | None = None, local_pos: dict | None = None) -> dict:
    """nodes[name] = (local pos, local rot) of the build pose; parent[name] = parent name or None.
    Overrides by name. Returns {name: (global pos, global rot)} (unit scale assumed)."""
    out: dict = {}

    def g(n):
        if n in out:
            return out[n]
        p, q = nodes[n]
        if local_rot and n in local_rot:
            q = local_rot[n]
        if local_pos and n in local_pos:
            p = local_pos[n]
        par = parent.get(n)
        if par is None:
            out[n] = (list(p), list(q))
        else:
            pp, pq = g(par)
            out[n] = ([pp[i] + v for i, v in enumerate(qrot(pq, p))], qnorm(qmul(pq, q)))
        return out[n]

    for n in nodes:
        g(n)
    return out


def bake(nodes: dict, parent: dict, human: dict, frames: list, twist=GAME_TWIST) -> dict:
    """The humanoid clip as local keyframes of the model's own nodes, in Unity space.
      nodes, parent : the skeleton's build pose (see `globals_of`)
      human         : {human bone name: node name}, e.g. {'Hips': 'jnt_C_hips00_00', ...}
      frames        : per frame, the clip's 137 humanoid values (attribute order)
    Returns {"rot": {node: [local quat per frame]}, "hips_pos": [local pos per frame]}.
    In place: the body's x / z are centred on their mean over the clip."""
    g0 = globals_of(nodes, parent)
    tpose = {b: g0[n] for b, n in human.items()}
    parent_h = {}
    for b, n in human.items():
        pn = parent.get(n)
        parent_h[b] = pn
        tpose.setdefault(pn, g0[pn])
    axes = build_axes(tpose, parent_h)
    com0, q0 = body_of({b: g0[n][0] for b, n in human.items()})
    s = com0[1]                                    # human scale = build-pose CoM height
    hips = human['Hips']
    H0p, H0q = g0[hips]
    mid = [sum(f[7 + i] for f in frames) / len(frames) for i in range(3)]
    out_rot = {human[b]: [] for b in axes if b != 'Hips'}
    out_rot[hips] = []
    hips_pos = []
    par = parent.get(hips)
    pp, pq = g0[par] if par else ([0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0])
    for f in frames:
        loc = muscles_to_local(axes, f[42:137], twist)
        lr = {human[b]: q for b, q in loc.items() if b != 'Hips'}
        g = globals_of(nodes, parent, lr)          # every bone posed, the hips still at H0
        com, qb = body_of({b: g[n][0] for b, n in human.items()})
        # hips = Body(t) * inv(B * body_fix) * H0, body_fix = (0, conj(q0))
        bt = [(f[7] - mid[0]) * s, f[8] * s, (f[9] - mid[2]) * s]
        R = qmul(qnorm(f[10:14]), qconj(qmul(qb, qconj(q0))))
        hq = qnorm(qmul(R, H0q))
        hp = [bt[i] + v for i, v in enumerate(qrot(R, sub(H0p, com)))]
        out_rot[hips].append(qnorm(qmul(qconj(pq), hq)))
        hips_pos.append(qrot(qconj(pq), sub(hp, pp)))
        for n, q in lr.items():
            out_rot[n].append(q)
    return {"rot": out_rot, "hips_pos": hips_pos}
