"""
.ysm 原生模型：骨骼层级 + 姿态求值 + 烘焙网格取面。

与上游的对应关系
----------------
上游：YSMClientMapper.buildMesh 把 RawGeometry 转成 GeoModel.bakedBones，
      NativeModelRenderer.calculateBoneMatrix 负责骨骼矩阵：

        local = M_parent
              · T((pivotX - animTx)/16, (pivotY + animTy)/16, (pivotZ + animTz)/16)
              · Rz(animRz) · Ry(animRy) · Rx(animRx)
              · S(animSx, animSy, animSz)
              · T(-pivotX/16, -pivotY/16, -pivotZ/16)

      animR 单位是**弧度**且 X/Y 已取反（RotationValue.convert(deg, flip)，
      flip=true 用于 X/Y），等价于我们熟知的 R = Rz(rz)·Ry(-ry)·Rx(-rx)（度）。
      无动画时 T(p/16)·I·T(-p/16) = I —— 说明**烘焙顶点已经在模型空间**，
      渲染时直接乘骨骼矩阵即可，不需要 inverse-bind。

关键帧语义（YSMClientMapper.parseKeyframes + BoneKeyFrameProcessor）
-------------------------------------------------------------------
  contiguous = !hasPreData：没有 pre 数据时 pre==post（阶梯/连续），
                            有 pre 数据时 pre 是上一段终点、post 是本段终点。
  段 i（i>=1）：[kf[i-1].t, kf[i].t]，起点 = kf[i-1].post，终点 = kf[i].pre，
                段末瞬时值 = kf[i].post。
  段 0       ：[0, kf[0].t]，全程 = kf[0].pre，末尾跳到 kf[0].post（Transition）。
  插值模式   ：2 = catmullrom，其它 = linear。
"""
import os

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import numpy as np  # noqa: E402
from scipy.spatial.transform import Rotation as Rot  # noqa: E402


def rot_R(euler_deg):
    """Rz(rz)·Ry(-ry)·Rx(-rx)，与 NativeModelRenderer 的 rotateZ/Y/X 顺序一致。"""
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


class YsmModel:
    """一份 RawGeometry（main / arm）+ 它的动画。"""

    def __init__(self, raw, geo="main_model"):
        self.raw = raw
        self.geo = getattr(raw, geo)
        self.bones = self.geo["bones"]
        self.by_name = {b["name"]: b for b in self.bones}
        self.order = self._topo()
        self.parent_idx = {b["name"]: (self.by_name[b["parent"]]["name"]
                                       if b["parent"] in self.by_name else None)
                           for b in self.bones}
        self.tw = float(self.geo["texture_width"])
        self.th = float(self.geo["texture_height"])

    # ------------------------------------------------------------ 层级
    def _topo(self):
        out, seen = [], set()

        def visit(b, stack):
            n = b["name"]
            if n in seen or n in stack:
                return
            stack.add(n)
            par = b.get("parent") or ""
            if par and par in self.by_name:
                visit(self.by_name[par], stack)
            stack.discard(n)
            seen.add(n)
            out.append(b)

        for b in self.bones:
            visit(b, set())
        return out

    # ------------------------------------------------------------ 姿态
    def pose(self, anim=None):
        """返回 {bone_name: 4x4}（模型空间 = 上游烘焙顶点所在空间）。

        anim: {bone: {"rot":[x,y,z]度, "pos":[x,y,z]像素, "scale":[x,y,z]}}
        """
        anim = anim or {}
        M, vis = {}, {}
        for b in self.order:
            n = b["name"]
            piv = np.asarray(b["pivot"], float) / 16.0
            rot = np.zeros(3)
            sc = np.ones(3)
            a = anim.get(n)
            if a:
                rot = np.asarray(a.get("rot", [0, 0, 0]), float)
                p = np.asarray(a.get("pos", [0, 0, 0]), float)
                # 上游 translate(pivotX - animTx, pivotY + animTy, pivotZ + animTz)
                piv = piv + np.array([-p[0], p[1], p[2]], float) / 16.0
                sc = np.asarray(a.get("scale", [1, 1, 1]), float)
            local = (_T(piv) @ _R(rot_R(rot))
                     @ np.diag([sc[0], sc[1], sc[2], 1.0]) @ _T(-piv))
            par = b.get("parent") or ""
            base = M.get(par, np.eye(4))
            M[n] = base @ local
            # NativeModelRenderer：scale 三轴全 0 => 骨骼（及子级）隐藏
            vis[n] = (not np.allclose(sc, 0.0)) and (vis.get(par, True) if par else True)
        return M, vis

    # ------------------------------------------------------------ 取面
    def faces(self, anim=None, M=None, vis=None, tex_name=None):
        """返回 [(verts[4,3] 模型空间, uv[4,2] 像素, normal[3])]。"""
        if M is None:
            M, vis = self.pose(anim)
        out = []
        for b in self.order:
            n = b["name"]
            if not vis.get(n, True) or not b["cubes"]:
                continue
            Mn = M[n]
            Rn = Mn[:3, :3]
            for c in b["cubes"]:
                for f in c["faces"]:
                    v = np.asarray(f["pos"], float)
                    nrm = Rn @ np.asarray(f["normal"], float)
                    uv = np.asarray(f["uv"], float) * (self.tw, self.th)
                    vw = (np.hstack([v, np.ones((4, 1))]) @ Mn.T)[:, :3]
                    out.append((vw, uv, nrm))
        return out

    # ------------------------------------------------------------ 统计
    def stats(self):
        nfaces = sum(len(c["faces"]) for b in self.bones for c in b["cubes"])
        return {
            "bones": len(self.bones),
            "bones_with_mesh": sum(1 for b in self.bones if b["cubes"]),
            "cubes": sum(len(b["cubes"]) for b in self.bones),
            "faces": nfaces,
            "texture": (self.tw, self.th),
        }


# ------------------------------------------------------------------ 采样
_MOLANG = object()


def _as_vec(vals):
    """3 分量 -> (np.array, 是否含 molang)。molang 串按 0 处理并标记。"""
    out, has = np.zeros(3), False
    for i, v in enumerate(vals):
        if isinstance(v, str):
            has = True
        else:
            out[i] = float(v or 0.0)
    return out, has


def _catmull(p0, p1, p2, p3, u):
    """标准 Catmull-Rom（与上游 CatmullRomKeyFrame 一致）。"""
    u2, u3 = u * u, u * u * u
    return 0.5 * ((2 * p1) + (-p0 + p2) * u
                  + (2 * p0 - 5 * p1 + 4 * p2 - p3) * u2
                  + (-p0 + 3 * p1 - 3 * p2 + p3) * u3)


def sample_channel(ch, t):
    """采样一条 .ysm 通道（rotation/position/scale），返回 (vec3, has_molang)。"""
    if not ch:
        return None
    kfs = sorted(ch, key=lambda k: k["t"])
    # 上游 RawBoneKeyFrame.init：contiguous(!hasPreData) 时 postValue = preValue
    def pre(k):
        return k["pre"] if "pre" in k else k["post"]

    if t < kfs[0]["t"]:                       # Transition：段首常量
        return _as_vec(pre(kfs[0]))
    for i in range(1, len(kfs)):
        a, b = kfs[i - 1], kfs[i]
        if t < b["t"]:
            begin, _ = _as_vec(a["post"])
            end, hm = _as_vec(pre(b))
            dt = b["t"] - a["t"]
            u = 0.0 if dt <= 1e-9 else (t - a["t"]) / dt
            # BoneKeyFrameProcessor：本帧是 CATMULLROM 或 i==0 时用本帧模式，否则用前一帧
            mode = b["mode"] if (b["mode"] == 2) else a["mode"]
            if mode == 2:
                left = kfs[max(0, i - 2)]
                right = kfs[min(len(kfs) - 1, i + 1)]
                lp, _ = _as_vec(left["post"])
                rp, _ = _as_vec(pre(right))
                return _catmull(lp, begin, end, rp, u), hm
            return begin + (end - begin) * u, hm
    return _as_vec(kfs[-1]["post"])            # 超过末尾：保持最后值


def sample_clip(clip, t):
    """采样一个 .ysm 动画片段 -> {bone: {rot,pos,scale}}。

    注意：**这里不做 bind pose 叠加**。因为烘焙顶点已含 bind pose，
    骨骼矩阵在 rest 时是单位阵，动画值就是最终的骨变换。
    """
    anim = {}
    for bone, chans in clip.get("bones", {}).items():
        e = {}
        for key, name in (("rotation", "rot"), ("position", "pos"), ("scale", "scale")):
            got = sample_channel(chans.get(key), t)
            if got is None:
                continue
            v, hm = got
            if name == "scale" and not chans.get(key):
                v = np.ones(3)
            e[name] = [float(x) for x in v]
            if hm:
                e.setdefault("_molang", []).append(name)
        if e:
            anim[bone] = e
    return anim


def compose(*layers):
    """多层叠加：后面的层覆盖前面的（同骨同通道）。"""
    out = {}
    for layer in layers:
        for bone, e in layer.items():
            out.setdefault(bone, {}).update(e)
    return out
