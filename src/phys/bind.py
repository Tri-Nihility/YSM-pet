"""阶段 4：把 YSM 模型绑定成「九自由度物理骨架」。

两件事：
    1. 精简——从模型的 329 根骨骼里抽出 11 个物理分段 + 9 个可控关节；
    2. 桥接——让「物理关节角 q」与「YSM 骨骼 rotation」一一对应，
       从而使训练出来的姿态可以直接驱动渲染。

坐标约定
--------
模型空间（YSM 烘焙顶点所在）：右手系，单位=方块
    +X = 角色右侧     -Z = 角色正前方     +Y = 上
（由 ATRI 实测：LeftArm 质心 x=-0.244 / RightArm x=+0.244，
  眼睛·眼睑质心 z≈-0.21，脚尖 z≈-0.20）

pybullet 空间：Z-up。转换矩阵 P（det=+1，是旋转不是镜像）
    pb = P @ model      P = [[0,0,-1],[-1,0,0],[0,1,0]]
    px = -mz（前）   py = -mx（左→+Y）   pz = my（上）

角度约定
--------
q（度）= 绕 **+X_model** 的右手正向旋转。绕 +X 正转会把 -Y（下）转向 -Z（前），
所以 q>0 = 肢体向前摆。

但上游 RotationValue 对 X/Y 取负：rot_R([rx,ry,rz]) = Rz(rz)·Ry(-ry)·Rx(-rx)。
要让骨骼实际绕 +X 转 q 度，必须满足 -rx = q，即

    YSM rotation.x = **-q**

这条符号关系是整个桥接的关键，env / 渲染 / GUI 三处共用。
"""
import os
import xml.etree.ElementTree as ET
from xml.dom import minidom

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import numpy as np  # noqa: E402
import yaml  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 模型空间 -> pybullet 空间（Z-up）的旋转矩阵，det = +1
P_MAT = np.array([[0.0, 0.0, -1.0],
                  [-1.0, 0.0, 0.0],
                  [0.0, 1.0, 0.0]])


def to_pb(v):
    """模型空间向量 -> pybullet 空间。支持 (3,) 或 (N,3)。"""
    v = np.asarray(v, float)
    return v @ P_MAT.T if v.ndim == 2 else P_MAT @ v


class Skeleton9:
    """九自由度骨架：配置 + 模型 -> 分段归属 / 质量 / URDF / 姿态桥接。"""

    def __init__(self, model, cfg_path=None):
        """
        model: ysmmodel.YsmModel 实例
        """
        self.model = model
        cfg_path = cfg_path or os.path.join(ROOT, "configs", "joints9.yaml")
        with open(cfg_path, encoding="utf-8") as fp:
            self.cfg = yaml.safe_load(fp)

        if self.cfg.get("use_native_animation", False):
            raise RuntimeError("joints9.yaml: use_native_animation 必须为 false —— 本方案禁止使用模型自带动作")

        self.joints = self.cfg["joints"]
        self.joint_names = [j["name"] for j in self.joints]
        self.segments = self.cfg["segments"]
        self.hide = set(self.cfg.get("hide_bones", ()))

        self.by_name = model.by_name
        self.parent = {b["name"]: (b.get("parent") or "") for b in model.bones}
        self.children = {}
        for b in model.bones:
            self.children.setdefault(self.parent[b["name"]], []).append(b["name"])

        # 段根骨骼 -> 段名
        self.seg_root = {s["root_bone"]: name for name, s in self.segments.items()}
        self._check()
        self._assign()
        self.masses = self._masses()

    # ------------------------------------------------------------ 校验
    def _check(self):
        missing = [j["bone"] for j in self.joints if j["bone"] not in self.by_name]
        if missing:
            raise KeyError(f"模型里找不到关节骨骼: {missing}")
        for j in self.joints:
            real = np.asarray(self.by_name[j["bone"]]["pivot"], float) / 16.0
            want = np.asarray(j["pivot"], float)
            if not np.allclose(real, want, atol=1e-3):
                print(f"  [warn] {j['bone']} 枢轴 {np.round(real,4)} != 配置 {want}，以模型为准")
                j["pivot"] = [float(x) for x in real]
        for name, s in self.segments.items():
            if s["root_bone"] not in self.by_name:
                raise KeyError(f"模型里找不到分段根骨骼: {s['root_bone']} ({name})")

    # ------------------------------------------------------------ 分段归属
    def _assign(self):
        """每根骨骼归到「最近的段根祖先」；被 hide 的骨骼及其子树整体丢弃。"""
        root_bone = self.cfg["root"]["bone"]
        self.hidden = set()

        def mark_hidden(n):
            stack = [n]
            while stack:
                x = stack.pop()
                self.hidden.add(x)
                stack.extend(self.children.get(x, []))

        for h in self.hide:
            if h in self.by_name:
                mark_hidden(h)

        self.seg_of = {}
        for b in self.model.order:
            n = b["name"]
            cur = n
            while cur and cur not in self.seg_root:
                cur = self.parent.get(cur, "")
            self.seg_of[n] = self.seg_root.get(cur, self.cfg["root"]["segment"])
        # 骨盆还额外收下 DownBody / Leg 等挂点（它们挂在 AllBody 下，本就归属 pelvis）
        return self.seg_of

    def segment_bones(self, seg):
        return [n for n, s in self.seg_of.items()
                if s == seg and n not in self.hidden]

    # ------------------------------------------------------------ 几何 / 质量
    def _volumes(self):
        """每段占有的方块体积之和（方块=轴对齐盒，故 AABB 即为精确体积）。"""
        vol = {s: 0.0 for s in self.segments}
        box = {s: [] for s in self.segments}
        for b in self.model.order:
            n = b["name"]
            if n in self.hidden or n not in self.seg_of:
                continue
            seg = self.seg_of[n]
            for c in b["cubes"]:
                pts = np.vstack([np.asarray(f["pos"], float) for f in c["faces"]])
                lo, hi = pts.min(0), pts.max(0)
                d = np.maximum(hi - lo, 0.0)
                v = float(d[0] * d[1] * d[2])
                if v <= 0:
                    continue
                vol[seg] += v
                box[seg].append(np.vstack([lo, hi]))
        return vol, box

    def segment_bounds(self, seg):
        """该段所有方块的模型空间 AABB -> (lo, hi)。无网格返回 None。"""
        pts = []
        for n in self.segment_bones(seg):
            for c in self.by_name[n]["cubes"]:
                for f in c["faces"]:
                    pts.append(np.asarray(f["pos"], float))
        if not pts:
            return None
        pts = np.vstack(pts)
        return pts.min(0), pts.max(0)

    def _masses(self):
        """两种模式：按体段质量占比表（默认）或按方块体积自动分配（对照用）。"""
        if not hasattr(self, "volumes"):
            self.volumes, _ = self._volumes()
        vol = self.volumes
        mc = self.cfg["mass"]
        total_kg = float(mc["total_kg"])

        if mc.get("auto_from_volume", False):
            cap = float(mc["max_segment_share"]) * total_kg
            tot = sum(vol.values()) or 1.0
            m = {s: min(total_kg * vol[s] / tot, cap) for s in self.segments}
            ssum = sum(m.values())
            if ssum > 0:                       # 限幅后重新归一化到总体重
                m = {s: v * total_kg / ssum for s, v in m.items()}
        else:
            ovr = {k: float(v) for k, v in (mc["override"] or {}).items()}
            missing = [s for s in self.segments if s not in ovr]
            if missing:
                raise KeyError(f"mass.override 缺少分段: {missing}")
            m = dict(ovr)
        return {s: float(m[s]) for s in self.segments}

    def sole_height(self):
        """本体最低点（模型空间 y），用于确定初始离地高度。"""
        lo = None
        for n in ("LeftFoot", "RightFoot"):
            b = self.by_name.get(n)
            if not b or not b["cubes"]:
                continue
            pts = np.vstack([np.asarray(f["pos"], float)
                             for c in b["cubes"] for f in c["faces"]])
            lo = pts.min(0)[1] if lo is None else min(lo, pts.min(0)[1])
        return lo

    def stand_height(self):
        """站立参考髋高 = 髋枢轴 y - 脚底 y。"""
        hip = np.asarray(self.by_name[self.cfg["root"]["bone"]]["pivot"], float)[1] / 16.0
        return hip - self.sole_height()

    # ------------------------------------------------------------ 姿态桥接
    def limits(self):
        return np.array([j["limits"] for j in self.joints], float)

    def default_pose(self):
        return np.array([j["default"] for j in self.joints], float)

    def joint_torques(self):
        return np.array([j["torque_limit"] for j in self.joints], float)

    def normalize(self, q_deg):
        """度 -> [-1,1]（相对限位区间中点）。"""
        lim = self.limits()
        mid = (lim[:, 0] + lim[:, 1]) / 2
        half = (lim[:, 1] - lim[:, 0]) / 2
        return (np.asarray(q_deg, float) - mid) / half

    def denormalize(self, a):
        """[-1,1] -> 度，并裁剪到限位内。"""
        lim = self.limits()
        mid = (lim[:, 0] + lim[:, 1]) / 2
        half = (lim[:, 1] - lim[:, 0]) / 2
        return np.clip(np.asarray(a, float) * half + mid, lim[:, 0], lim[:, 1])

    def joint_to_anim(self, q_deg, extra_hidden=()):
        """9 个关节角（度）-> YSM 动画字典 {bone: {rot, scale}}。

        核心符号：**rotation.x = -q**（见模块文档）。
        extra_hidden 用于叠加表情系统的隐藏骨骼。
        """
        anim = {}
        for j, q in zip(self.joints, np.asarray(q_deg, float).ravel()):
            if abs(q) < 1e-9:
                continue
            # 轴只有 X 分量被用到（本方案全部为绕 X 的单轴关节）
            rot = [0.0, 0.0, 0.0]
            ax = np.asarray(j["axis"], float)
            s = -float(q)                       # ← 关键的符号翻转
            if abs(ax[0]) > 0.5:
                rot[0] = s * np.sign(ax[0])
            if abs(ax[1]) > 0.5:
                rot[1] = s * np.sign(ax[1])
            if abs(ax[2]) > 0.5:
                rot[2] = -s * np.sign(ax[2])    # Z 在上游不取负，故这里要再翻回来
            anim[j["bone"]] = {"rot": rot}
        for n in self.hidden | set(extra_hidden):
            if n in self.by_name:
                anim.setdefault(n, {})["scale"] = [0.0, 0.0, 0.0]
        return anim

    # ------------------------------------------------------------ URDF
    def _pivot(self, bone):
        return np.asarray(self.by_name[bone]["pivot"], float) / 16.0

    def build_urdf(self, out_path):
        """生成 URDF。root link = pelvis（pybullet 里用 useFixedBase=False 变浮动基）。"""
        root_seg = self.cfg["root"]["segment"]
        root_piv = self._pivot(self.cfg["root"]["bone"])

        def rel(child_bone, parent_seg):
            """child 枢轴相对 parent 段根枢轴的位移 -> pybullet 坐标。"""
            return to_pb(self._pivot(child_bone) - self._pivot(self.segments[parent_seg]["root_bone"]))

        robot = ET.Element("robot", name=f"ysm_{self.cfg['model']}_9dof")

        def box_inertia(parent, mass, size):
            ix = mass / 12.0 * (size[1] ** 2 + size[2] ** 2)
            iy = mass / 12.0 * (size[0] ** 2 + size[2] ** 2)
            iz = mass / 12.0 * (size[0] ** 2 + size[1] ** 2)
            ET.SubElement(parent, "mass", value=f"{mass:.6f}")
            ET.SubElement(parent, "inertia", ixx=f"{ix:.8f}", ixy="0", ixz="0",
                          iyy=f"{iy:.8f}", iyz="0", izz=f"{iz:.8f}")

        def add_collision(link, c, size):
            col = ET.SubElement(link, "collision")
            ET.SubElement(col, "origin",
                          xyz=f"{c[0]:.6f} {c[1]:.6f} {c[2]:.6f}", rpy="0 0 0")
            ET.SubElement(ET.SubElement(col, "geometry"), "box",
                          size=f"{size[0]:.6f} {size[1]:.6f} {size[2]:.6f}")

        def local_box(seg, lo=None, hi=None):
            """段 AABB -> (局部中心, 局部尺寸)，原点是该段根枢轴。"""
            if lo is None:
                b = self.segment_bounds(seg)
                if b is None:
                    return None
                lo, hi = b
            lo = to_pb(np.asarray(lo, float) - self._pivot(self.segments[seg]["root_bone"]))
            hi = to_pb(np.asarray(hi, float) - self._pivot(self.segments[seg]["root_bone"]))
            l, h = np.minimum(lo, hi), np.maximum(lo, hi)
            size = np.maximum(h - l, 1e-3)
            return (l + h) / 2, size

        # ---- links
        for seg, s in self.segments.items():
            link = ET.SubElement(robot, "link", name=seg)
            mass = max(float(self.masses[seg]), 1e-4)
            lb = local_box(seg)
            if lb is None:
                box_inertia(ET.SubElement(link, "inertial"), mass, (0.05, 0.05, 0.05))
            else:
                c, size = lb
                ine = ET.SubElement(link, "inertial")
                ET.SubElement(ine, "origin",
                              xyz=f"{c[0]:.6f} {c[1]:.6f} {c[2]:.6f}", rpy="0 0 0")
                box_inertia(ine, mass, size)
                add_collision(link, c, size)

        # 小腿补一个脚掌碰撞盒（脚不在 9 关节内，但要提供支撑面）
        for att in self.cfg["fixed_attachments"]:
            bone, seg = att["bone"], att["parent"]
            nb = self.segment_bounds(seg)
            pts = []
            stack = [bone]
            while stack:
                n = stack.pop()
                b = self.by_name.get(n)
                if not b:
                    continue
                for c in b["cubes"]:
                    for f in c["faces"]:
                        pts.append(np.asarray(f["pos"], float))
                stack.extend(self.children.get(n, []))
            if not pts:
                continue
            pts = np.vstack(pts)
            link = [e for e in robot.findall("link") if e.get("name") == seg][0]
            add_collision(link, *local_box(seg, pts.min(0), pts.max(0)))

        def add_joint(name, parent, child, xyz, jtype="fixed", axis=(0, -1, 0),
                      lo=0.0, hi=0.0, torque=1000.0):
            j = ET.SubElement(robot, "joint", name=name, type=jtype)
            ET.SubElement(j, "origin", xyz=f"{xyz[0]:.6f} {xyz[1]:.6f} {xyz[2]:.6f}",
                          rpy="0 0 0")
            ET.SubElement(j, "parent", link=parent)
            ET.SubElement(j, "child", link=child)
            if jtype != "fixed":
                ET.SubElement(j, "axis", xyz=f"{axis[0]} {axis[1]} {axis[2]}")
                ET.SubElement(j, "limit", lower=f"{np.radians(lo):.6f}",
                              upper=f"{np.radians(hi):.6f}",
                              effort=f"{torque:.1f}", velocity="12.0")
                ET.SubElement(j, "dynamics", damping="0.05", friction="0.02")

        # 固定：pelvis -> torso
        add_joint("torso_fixed", root_seg, "torso", rel("UpperBody", root_seg))

        for j in self.joints:
            seg = j["segment"]
            parent_seg = self.segments[seg]["parent"]
            add_joint(j["name"], parent_seg, seg, rel(j["bone"], parent_seg),
                      "revolute", lo=j["limits"][0], hi=j["limits"][1],
                      torque=j["torque_limit"])

        xml = minidom.parseString(ET.tostring(robot, encoding="unicode")).toprettyxml("  ")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fp:
            fp.write(xml)
        return out_path

    # ------------------------------------------------------------ 摘要
    def summary(self):
        vol = getattr(self, "volumes", None) or self._volumes()[0]
        rows = []
        for seg in self.segments:
            lb = self.segment_bounds(seg)
            rows.append({
                "segment": seg,
                "parent": self.segments[seg]["parent"],
                "root_bone": self.segments[seg]["root_bone"],
                "bones": len(self.segment_bones(seg)),
                "volume_blk3": round(vol[seg], 4),
                "mass_kg": round(float(self.masses[seg]), 3),
                "aabb": [list(np.round(lb[0], 3)), list(np.round(lb[1], 3))] if lb else None,
            })
        return {
            "joint_order": self.joint_names,
            "joint_bones": [j["bone"] for j in self.joints],
            "stand_height_blocks": round(float(self.stand_height()), 4),
            "sole_y": round(float(self.sole_height()), 4),
            "total_mass_kg": round(float(sum(self.masses.values())), 3),
            "segments": rows,
            "use_native_animation": False,
        }
