"""
第一步(贴图版)：带 skin 贴图的软光栅渲染。

为什么不用 pybullet 画贴图：pybullet 的 GEOM_BOX 不支持自定义 UV 展开，
而 Bedrock 用的是 box-UV 布局，映射规则无法交给 pybullet。
所以：几何/UV 用 src/ysm/geometry.py（对齐上游），相机矩阵仍由 pybullet 提供，光栅化自己实现。

用法：python scripts/step1b_render_textured.py [core|full]
输出：outputs/step1_preview/step1b_{mode}_{view}.png
"""
import os
import sys

import numpy as np
import pybullet as p
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src.ysm import geometry as G  # noqa: E402

PKG = os.path.join(ROOT, "临时文件（记得整理掉", "苦力怕狐")
MODEL = os.path.join(PKG, "models", "main.json")
TEX = os.path.join(PKG, "textures", "skin.png")
OUT = os.path.join(ROOT, "outputs", "step1_preview")
os.makedirs(OUT, exist_ok=True)

W = H = 800
TARGET_HEIGHT = 1.8
CORE_BONES = {"UpBody", "UpperBody", "Body", "AllHead", "Head", "Head2",
              "LeftArm", "LeftForeArm", "LeftHand", "RightArm", "RightForeArm", "RightHand",
              "LeftLeg", "LeftLowerLeg", "LeftFoot", "RightLeg", "RightLowerLeg", "RightFoot"}
FX_BONES = {"Background1", "curtain", "white_curtain", "MooncakleH",
            "ysmGlowTnt_Right_bone"}


def draw_tri(buf, zbuf, A, B, C, uvA, uvB, uvC, tex, tw, th, shade):
    minx = max(int(np.floor(min(A[0], B[0], C[0]))), 0)
    maxx = min(int(np.ceil(max(A[0], B[0], C[0]))), W - 1)
    miny = max(int(np.floor(min(A[1], B[1], C[1]))), 0)
    maxy = min(int(np.ceil(max(A[1], B[1], C[1]))), H - 1)
    if minx > maxx or miny > maxy:
        return
    gx = np.arange(minx, maxx + 1) + 0.5
    gy = np.arange(miny, maxy + 1) + 0.5
    X, Y = np.meshgrid(gx, gy)
    v0x, v0y = B[0] - A[0], B[1] - A[1]
    v1x, v1y = C[0] - A[0], C[1] - A[1]
    den = v0x * v1y - v1x * v0y
    if abs(den) < 1e-9:
        return
    px, py = X - A[0], Y - A[1]
    w1 = (px * v1y - v1x * py) / den
    w2 = (v0x * py - px * v0y) / den
    m = (w1 >= 0) & (w2 >= 0) & (w1 + w2 <= 1)
    if not m.any():
        return
    z = A[2] + w1 * (B[2] - A[2]) + w2 * (C[2] - A[2])
    u = uvA[0] + w1 * (uvB[0] - uvA[0]) + w2 * (uvC[0] - uvA[0])
    v = uvA[1] + w1 * (uvB[1] - uvA[1]) + w2 * (uvC[1] - uvA[1])
    ok = m & (z < zbuf[miny:maxy + 1, minx:maxx + 1])
    if not ok.any():
        return
    ui = np.clip(u[ok].astype(int), 0, tw - 1)
    vi = np.clip(v[ok].astype(int), 0, th - 1)
    cols = tex[vi, ui]
    vis = cols[:, 3] >= 100
    if not vis.any():
        return
    iy, ix = np.nonzero(ok)
    iy, ix = iy[vis] + miny, ix[vis] + minx
    buf[iy, ix] = cols[vis][:, :3] * shade
    zbuf[iy, ix] = z[ok][vis]


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "core"
    tex_img = Image.open(TEX).convert("RGBA")
    tex = np.asarray(tex_img).astype(np.float32)
    th, tw = tex.shape[:2]

    geo = G.load_geometry(MODEL)
    bones = geo["bones"]
    M = G.bone_matrices(bones)
    if mode == "core":
        keep = lambda n: n in CORE_BONES          # noqa: E731
        tag = f"核心骨架 {len(CORE_BONES)} 根"
    else:
        keep = lambda n: n not in FX_BONES        # noqa: E731
        tag = "全模型(去特效件)"
    # build_faces 不保留 bone 名，这里重跑一次带筛选
    faces = []
    for b in bones:
        n = b["name"]
        if n not in M or not b.get("cubes"):
            continue
        if not keep(n):
            continue
        Mn = M[n]
        Rn = Mn[:3, :3]
        for c in b["cubes"]:
            for _, verts, uv in G.cube_faces(c, geo["texture_width"], geo["texture_height"]):
                v = (np.hstack([verts, np.ones((4, 1))]) @ Mn.T)[:, :3]
                nrm = np.cross(verts[1] - verts[0], verts[2] - verts[0])
                nrm = Rn @ (nrm / (np.linalg.norm(nrm) + 1e-9))
                faces.append((v, uv, nrm))

    print(f"[贴图] {os.path.relpath(TEX, ROOT)} {tw}x{th}")
    print(f"[模型] 模式={mode}({tag}) | 面数 {len(faces)}")

    # ---- 归一化：居中 + 缩放到 1.8m（渲染空间 Y 上）----
    allv = np.concatenate([f[0] for f in faces])
    lo, hi = allv.min(0), allv.max(0)
    ctr = np.array([(lo[0] + hi[0]) / 2, lo[1], (lo[2] + hi[2]) / 2])
    scale = TARGET_HEIGHT / max(hi[1] - lo[1], 1e-6)
    # 渲染空间(Y上) -> pybullet(Z上)
    faces = [(((v - ctr) * scale) @ G.P_MAT.T, uv, G.to_pb_vec(n)) for v, uv, n in faces]
    print(f"[缩放] {scale:.4f} → 高 {TARGET_HEIGHT}m")

    # ---- 相机矩阵由 pybullet 提供 ----
    p.connect(p.DIRECT)
    target = [0, 0, TARGET_HEIGHT * 0.5]
    views = {"yaw000": (3.2, 0, 5), "yaw180": (3.2, 180, 5),
             "yaw090": (3.2, 90, 5), "yaw045": (2.9, 45, 12)}
    mats = {}
    for name, (dist, yaw, pitch) in views.items():
        vm = np.array(p.computeViewMatrixFromYawPitchRoll(
            target, dist, yaw, pitch, 0, upAxisIndex=2)).reshape(4, 4).T
        pm = np.array(p.computeProjectionMatrixFOV(45, W / H, 0.05, 100)).reshape(4, 4).T
        mats[name] = pm @ vm
    p.disconnect()

    clip = mats["yaw000"] @ np.array([target[0], target[1], target[2], 1.0])
    print(f"[相机] target 投影 NDC = {np.round(clip[:3] / clip[3], 3)}（应接近 0,0）")

    LIGHT = np.array([0.45, -0.6, 0.66])
    LIGHT = LIGHT / np.linalg.norm(LIGHT)

    for name, Mvp in mats.items():
        buf = np.full((H, W, 3), 246.0)
        zbuf = np.full((H, W), np.inf)
        painted = 0
        for verts, uv, n in faces:
            if np.dot(n, -verts[0]) > 0:        # 背面剔除
                continue
            shade = 0.55 + 0.45 * max(float(np.dot(n, LIGHT)), 0.0)
            clip = np.hstack([verts, np.ones((4, 1))]) @ Mvp.T
            ndc = clip[:, :3] / clip[:, 3:4]
            sx = (ndc[:, 0] * 0.5 + 0.5) * W
            sy = (1 - (ndc[:, 1] * 0.5 + 0.5)) * H
            sz = ndc[:, 2]
            A = np.array([sx[0], sy[0], sz[0]])
            B = np.array([sx[1], sy[1], sz[1]])
            C = np.array([sx[2], sy[2], sz[2]])
            D = np.array([sx[3], sy[3], sz[3]])
            draw_tri(buf, zbuf, A, B, C, uv[0], uv[1], uv[2], tex, tw, th, shade)
            draw_tri(buf, zbuf, A, C, D, uv[0], uv[2], uv[3], tex, tw, th, shade)
        path = os.path.join(OUT, f"step1b_{mode}_{name}.png")
        Image.fromarray(buf.astype(np.uint8)).save(path)
        painted = int((zbuf < np.inf).sum())
        print(f"[渲染] {name:<8} 面 {len(faces):>5} 着色像素 {painted:>7} -> {os.path.basename(path)}")

    print(f"\n完成 → {OUT}")


if __name__ == "__main__":
    main()
