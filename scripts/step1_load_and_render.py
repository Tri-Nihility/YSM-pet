"""
第一步：引擎 + 场景 + 模型 加载，并渲染出图。

- 引擎：pybullet (DIRECT, 软件渲染)
- 场景：地面 + 光照
- 模型：YSM / 基岩版 geometry（models/main.json），逐骨骼 FK + 逐 cube 重建

FK / UV 规则全部对齐上游 OpenYSM（见 src/ysm/geometry.py 顶部注释）。
坐标系：渲染空间 (Y 上) -> pybullet (Z 上)：(x, y, z) -> (x, z, y)

用法：python scripts/step1_load_and_render.py [core|full]
输出：outputs/step1_preview/step1_{mode}_{view}.png
"""
import os
import sys

import numpy as np
import pybullet as p
import pybullet_data
from scipy.spatial.transform import Rotation as Rot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src.ysm import geometry as G  # noqa: E402

PKG = os.path.join(ROOT, "临时文件（记得整理掉", "苦力怕狐")
MODEL = os.path.join(PKG, "models", "main.json")
OUT = os.path.join(ROOT, "outputs", "step1_preview")
os.makedirs(OUT, exist_ok=True)

TARGET_HEIGHT = 1.8          # 归一化到这个高度（米）
W = H = 900

# 核心骨架（验证 FK 用）：躯干 + 头 + 四肢
CORE_BONES = {
    "UpBody", "UpperBody", "Body", "AllHead", "Head", "Head2",
    "LeftArm", "LeftForeArm", "LeftHand", "RightArm", "RightForeArm", "RightHand",
    "LeftLeg", "LeftLowerLeg", "LeftFoot", "RightLeg", "RightLowerLeg", "RightFoot",
}
# 展示用特效件（背景幕布、道具等），不属于骨架运动
FX_BONES = {"Background1", "curtain", "white_curtain", "MooncakleH",
            "ysmGlowTnt_Right_bone"}


def render(views, target, mode, bodies):
    out = []
    for name, (dist, yaw, pitch) in views.items():
        vm = p.computeViewMatrixFromYawPitchRoll(target, dist, yaw, pitch, 0, upAxisIndex=2)
        pm = p.computeProjectionMatrixFOV(fov=45, aspect=W / H, nearVal=0.05, farVal=100)
        img = p.getCameraImage(W, H, vm, pm)
        rgb = np.reshape(img[2], (H, W, 4))[:, :, :3]
        path = os.path.join(OUT, f"step1_{mode}_{name}.png")
        try:
            import imageio.v2 as imageio
            imageio.imwrite(path, rgb)
        except ImportError:
            from PIL import Image
            Image.fromarray(rgb).save(path)
        center = rgb[H // 2 - 60:H // 2 + 60, W // 2 - 60:W // 2 + 60]
        out.append((name, path, int((center.sum(axis=2) < 720).sum())))
    return out


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "core"   # core / full
    geo = G.load_geometry(MODEL)
    bones = geo["bones"]
    M = G.bone_matrices(bones)
    boxes = G.build_boxes(bones, M)

    if mode == "core":
        boxes = [b for b in boxes if b["bone"] in CORE_BONES]
        tag = f"核心骨架 {len(CORE_BONES)} 根"
    else:
        boxes = [b for b in boxes if b["bone"] not in FX_BONES]
        tag = "全模型(去特效件)"
    print(f"[模型] {os.path.relpath(MODEL, ROOT)}  模式={mode}({tag})")
    print(f"  骨骼 {len(bones)} | 本次渲染 cube {len(boxes)}")

    allv = np.concatenate([np.array([b["pos"] - b["half"], b["pos"] + b["half"]]) for b in boxes])
    lo, hi = allv.min(0), allv.max(0)
    size = hi - lo
    print(f"  包围盒 X={size[0]*16:.1f} Y={size[1]*16:.1f} Z={size[2]*16:.1f} px  "
          f"(Y/X={size[1]/max(size[0], 1e-6):.2f})")
    scale = TARGET_HEIGHT / max(size[1], 1e-6)
    print(f"  缩放 {scale:.4f} → 高 {TARGET_HEIGHT} m")

    # ---- 引擎 & 场景 ----
    p.connect(p.DIRECT)
    p.setAdditionalSearchPath(pybullet_data.getDataPath())
    p.setGravity(0, 0, -9.8)
    ground = p.loadURDF("plane.urdf", [0, 0, 0], globalScaling=8)
    p.changeVisualShape(ground, -1, rgbaColor=[0.30, 0.32, 0.36, 1])
    print(f"[场景] 地面已加载 | 引擎 pybullet DIRECT")

    # ---- 逐 cube 建体 ----
    ctr = np.array([(lo[0] + hi[0]) / 2, lo[1], (lo[2] + hi[2]) / 2])
    for b in boxes:
        half = G.to_pb_vec(b["half"]) * scale
        pos = G.to_pb_vec(b["pos"] - ctr) * scale
        quat = Rot.from_matrix(G.to_pb_rot(b["rot"])).as_quat()
        h = (b["pos"][1] - lo[1]) / max(size[1], 1e-6)      # 按高度着色，便于分辨结构
        col = [0.35 + 0.5 * h, 0.45 + 0.25 * (1 - h), 0.55 + 0.3 * (1 - h), 1.0]
        sh = p.createVisualShape(p.GEOM_BOX, halfExtents=half.tolist(), rgbaColor=col)
        p.createMultiBody(baseMass=0, baseVisualShapeIndex=sh,
                          basePosition=pos.tolist(), baseOrientation=quat.tolist())
    print(f"[加载] 建体 {len(boxes)} 个")

    target = [0, 0, TARGET_HEIGHT * 0.5]
    views = {
        "yaw000": (3.4, 0, 6),
        "yaw090": (3.4, 90, 6),
        "yaw180": (3.4, 180, 6),
        "yaw045": (3.0, 45, 14),
    }
    for name, path, nz in render(views, target, mode, None):
        print(f"[渲染] {name:<8} 画面中心非白像素 {nz:>6} -> {os.path.basename(path)}")

    p.disconnect()
    print(f"\n完成 → {OUT}")


if __name__ == "__main__":
    main()
