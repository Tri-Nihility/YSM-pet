"""
YSM / 基岩版 geometry 解析 + FK —— 逐行对齐上游 OpenYSM 2.6.x 的 Java 实现。

上游依据（C:/Users/User/Desktop/YSM/YSM-Liaison/mod/common/src/main/java/com/elfmcys/yesstevemodel/）：
  resource/YSMFolderDeserializer.java      L505-645   JSON 解析 / cube 烘焙 / box-UV
  geckolib3/geo/NativeModelRenderer.java             calculateBoneMatrix（骨骼矩阵）

约定
----
JSON 里的 origin/size/pivot 都是"像素"单位（1 方块 = 16）。
渲染空间 = 方块单位，Y 上，且与 MC/JSON 空间相比 X 取反：

    v_render = (-v_json[0], v_json[1], v_json[2]) / 16

骨骼矩阵（bind pose，无动画）：

    M_bone = M_parent · T(pivot) · Rz(rz)·Ry(-ry)·Rx(-rx) · T(-pivot)

scipy 的外旋 "XYZ" 恰好是 Rz(c)·Ry(b)·Rx(a)，所以 R = from_euler("XYZ", [-rx, -ry, rz])。
"""
import os

# 必须在 import numpy 之前：多线程 OpenBLAS 在内存紧张时会
# "Memory allocation still failed after 10 retries"，直接杀掉进程。
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import json  # noqa: E402

import numpy as np  # noqa: E402
from scipy.spatial.transform import Rotation as Rot  # noqa: E402

# ---------------------------------------------------------------- 基础工具


def to_render(v):
    """MC/JSON(像素) -> 渲染空间(方块)。X 取反。"""
    v = np.asarray(v, float)
    return np.array([-v[0], v[1], v[2]], float) / 16.0


def rot_R(euler_deg):
    """上游 rotateZ(rz)·rotateY(-ry)·rotateX(-rx) == scipy 外旋 XYZ[-rx,-ry,rz]。"""
    e = np.asarray(euler_deg, float)
    return Rot.from_euler("XYZ", [-e[0], -e[1], e[2]], degrees=True).as_matrix()


def _T(t):
    M = np.eye(4)
    M[:3, 3] = t
    return M


def _R(Rm):
    M = np.eye(4)
    M[:3, :3] = Rm
    return M


def about(p, Rm):
    """T(p) · R · T(-p)：绕点 p 旋转。"""
    return _T(p) @ _R(Rm) @ _T(-p)


# ---------------------------------------------------------------- 解析


def load_geometry(path):
    d = json.load(open(path, encoding="utf-8"))
    g = d["minecraft:geometry"][0]
    desc = g.get("description", {})
    return {
        "bones": g["bones"],
        "texture_width": float(desc.get("texture_width", 64)),
        "texture_height": float(desc.get("texture_height", 64)),
        "identifier": desc.get("identifier", ""),
    }


def _ordered(bones):
    """父骨骼排在子骨骼之前（上游按数组顺序假设已满足，这里做一次保险拓扑排序）。"""
    by = {b["name"]: b for b in bones}
    out, seen = [], set()

    def visit(b, stack):
        n = b["name"]
        if n in seen or n in stack:
            return
        stack.add(n)
        par = b.get("parent") or ""
        if par and par in by:
            visit(by[par], stack)
        stack.discard(n)
        seen.add(n)
        out.append(b)

    for b in bones:
        visit(b, set())
    return out


# ---------------------------------------------------------------- FK


def bone_matrices(bones, anim=None, replace_rot=False):
    """返回 {bone_name: 4x4}。

    anim: {bone: {"rot":[x,y,z]deg, "pos":[x,y,z], "scale":[x,y,z]}}
    replace_rot: True 时动画值**覆盖** bind pose 的 rotation；
                 False（默认）时叠加。两种语义都要试，看哪个和上游一致。

    scale 通道真实存在且常 != 1（如 ysmGlowTnt_Right_bone = [3.33,3.11,3.175]），
    还被大量用作**显隐开关**（scale=0 隐藏，见 docs/05 §3b）。
    """
    anim = anim or {}
    M = {}
    for b in _ordered(bones):
        n = b["name"]
        piv = to_render(b.get("pivot", [0, 0, 0]))
        e = np.asarray(b.get("rotation", [0, 0, 0]), float)
        s = np.asarray(b.get("scale", [1.0, 1.0, 1.0]), float)
        a = anim.get(n)
        if a:
            if replace_rot and "rot" in a:
                e = np.asarray(a["rot"], float)
            else:
                e = e + np.asarray(a.get("rot", [0, 0, 0]), float)
            piv = piv + to_render(a.get("pos", [0, 0, 0]))
            s = s * np.asarray(a.get("scale", [1, 1, 1]), float)
        # NativeModelRenderer: translate(+pivot - animT) ... translate(-pivot)
        local = about(piv, rot_R(e)) @ np.diag([s[0], s[1], s[2], 1.0])
        par = b.get("parent") or ""
        M[n] = (M[par] @ local) if par in M else local
    return M


def cube_bake_matrix(cube):
    """cube 的 pivot 旋转烘焙矩阵；无 rotation/pivot 时返回 None。

    上游 L542-546：translate(-cpvt0/16, cpvt1/16, cpvt2/16) · Rz·Ry·Rx · translate(cpvt0/16, -cpvt1/16, -cpvt2/16)
    """
    if "rotation" not in cube and "pivot" not in cube:
        return None
    cp = np.asarray(cube.get("pivot", [0, 0, 0]), float)
    cr = np.asarray(cube.get("rotation", [0, 0, 0]), float)
    a = np.array([-cp[0], cp[1], cp[2]], float) / 16.0
    return about(a, rot_R(cr))


def cube_corners(cube):
    """8 个角点（渲染空间，方块单位）。顺序 = 上游 p1..p8。

    上游 L531-536：cx = -origin[0] - size[0] - inflate, cy = origin[1] - inflate, ...
    """
    o = np.asarray(cube["origin"], float)
    s = np.asarray(cube["size"], float)
    inf = float(cube.get("inflate", 0.0))
    x1 = (-o[0] - s[0] - inf) / 16.0
    x2 = (-o[0] + inf) / 16.0
    y1 = (o[1] - inf) / 16.0
    y2 = (o[1] + s[1] + inf) / 16.0
    z1 = (o[2] - inf) / 16.0
    z2 = (o[2] + s[2] + inf) / 16.0
    return np.array([
        [x1, y1, z1],  # p1
        [x1, y1, z2],  # p2
        [x1, y2, z1],  # p3
        [x1, y2, z2],  # p4
        [x2, y1, z1],  # p5
        [x2, y1, z2],  # p6
        [x2, y2, z1],  # p7
        [x2, y2, z2],  # p8
    ])


def cube_box(cube):
    """(center[3], half[3])：烘焙后的 AABB（渲染空间）。"""
    c = cube_corners(cube)
    bake = cube_bake_matrix(cube)
    if bake is not None:
        c = (np.hstack([c, np.ones((8, 1))]) @ bake.T)[:, :3]
    lo, hi = c.min(0), c.max(0)
    return (lo + hi) / 2, np.maximum((hi - lo) / 2, 1e-5)


# ---------------------------------------------------------------- 面 / UV

# 上游 L626-634 的顶点索引（p1..p8 -> 0..7），全部按外向 CCW
FACE_IDX = {
    "west":  [3, 2, 0, 1],
    "east":  [6, 7, 5, 4],
    "north": [2, 6, 4, 0],
    "south": [7, 3, 1, 5],
    "up":    [3, 7, 6, 2],
    "down":  [0, 4, 5, 1],
}
FACE_KEYS = ("north", "south", "east", "west", "up", "down")
_ALIAS = {"top": "up", "bottom": "down"}


def box_uv_rects(size, uv):
    """box-UV 六面矩形布局 (u, v, w, h)，像素。上游 L570-575。"""
    dx, dy, dz = (float(np.floor(x)) for x in size)
    ux, uy = uv
    return {
        "north": (ux + dz, uy + dz, dx, dy),
        "south": (ux + dz + dx + dz, uy + dz, dx, dy),
        "east":  (ux, uy + dz, dz, dy),
        "west":  (ux + dz + dx, uy + dz, dz, dy),
        "up":    (ux + dz, uy, dx, dz),
        "down":  (ux + dz + dx, uy + dz, dx, -dz),
    }


def cube_faces(cube, tw, th):
    """返回 [(face, verts[4,3] 渲染空间(已烘焙), uv[4,2] 像素)]。

    cube 没有 uv 时返回 []（几何仍可用 cube_box 渲染）。
    """
    if "uv" not in cube:
        return []
    mirror = bool(cube.get("mirror", False))
    corners = cube_corners(cube)
    bake = cube_bake_matrix(cube)
    if bake is not None:
        corners = (np.hstack([corners, np.ones((8, 1))]) @ bake.T)[:, :3]

    uvspec = cube["uv"]
    rects = None
    if not isinstance(uvspec, dict):
        rects = box_uv_rects(cube["size"], uvspec)

    out = []
    for face in FACE_KEYS:
        if isinstance(uvspec, dict):
            # 上游 L557-558：mirror 时 east/west 的 UV 互相取对方的键
            key = face
            if mirror and face == "east":
                key = "west"
            elif mirror and face == "west":
                key = "east"
            node = uvspec.get(key) or uvspec.get(_ALIAS.get(key, key))
            if node is None:
                continue
            u_, v_ = node["uv"]
            su, sv = node.get("uv_size", [0, 0])
        else:
            u_, v_, su, sv = rects[face]

        u0, u1 = u_ / tw, (u_ + su) / tw
        v0, v1 = v_ / th, (v_ + sv) / th
        if not mirror:          # 上游 L605-607：mirror == false 时才交换
            u0, u1 = u1, u0
        uv4 = np.array([[u0, v0], [u1, v0], [u1, v1], [u0, v1]], float) * (tw, th)
        out.append((face, corners[FACE_IDX[face]].copy(), uv4))
    return out


# ---------------------------------------------------------------- 汇总


def build_boxes(bones, M):
    """[{bone, pos, half, rot}]。rot 已含缩放归一化，half 已按骨缩放放大。"""
    out = []
    for b in bones:
        n = b["name"]
        if n not in M or not b.get("cubes"):
            continue
        Mn = M[n]
        Rs = Mn[:3, :3]
        sc = np.linalg.norm(Rs, axis=0)          # 各轴缩放因子
        # 旋转 = 去掉缩放的方向矩阵
        Rdir = Rs / np.where(np.abs(sc) < 1e-9, 1.0, sc)[None, :]
        for c in b["cubes"]:
            center, half = cube_box(c)
            bake = cube_bake_matrix(c)
            Rw = Rdir @ (bake[:3, :3] if bake is not None else np.eye(3))
            pos = (Mn @ np.array([center[0], center[1], center[2], 1.0]))[:3]
            out.append({"bone": n, "pos": pos, "half": half * sc, "rot": Rw})
    return out


def build_faces(bones, M, tw, th):
    """[(verts[4,3] 世界渲染空间, uv[4,2] 像素, normal[3])]。"""
    out = []
    for b in bones:
        n = b["name"]
        if n not in M or not b.get("cubes"):
            continue
        Mn = M[n]
        Rn = Mn[:3, :3]
        for c in b["cubes"]:
            for _, verts, uv in cube_faces(c, tw, th):
                v = (np.hstack([verts, np.ones((4, 1))]) @ Mn.T)[:, :3]
                nrm = np.cross(verts[1] - verts[0], verts[2] - verts[0])
                nrm = Rn @ (nrm / (np.linalg.norm(nrm) + 1e-9))
                out.append((v, uv, nrm))
    return out


# ---------------------------------------------------------------- 坐标 -> pybullet

# 渲染空间(Y 上) -> pybullet(Z 上)：(x, y, z) -> (x, z, y)
P_MAT = np.array([[1.0, 0, 0], [0, 0, 1.0], [0, 1.0, 0]])


def to_pb_vec(v):
    return P_MAT @ np.asarray(v, float)


def to_pb_rot(Rm):
    return P_MAT @ Rm @ P_MAT.T
