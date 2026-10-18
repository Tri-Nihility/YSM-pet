"""
第二步：按上游的层序合成「常态」姿态并渲染成接触表（contact sheet）。

层序对齐 `docs/06` §1 / §3：
    pre_parallel0..7（显隐归零，静态 t=0）
  → head:default（默认显隐：把面具等恢复显示）
  → main 层：<idle>（时会 foot dynamics，逐个时刻采样）
  → post_main：<new_idle_empty>（idle 轮盘的空档）
  → parallel_7：关兜帽 / 开兜帽（按 v.hat 二选一）

用法：python scripts/step2_render_states.py [replace|add]
输出：outputs/step2_states/state_*.png + states_sheet.png
"""
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src.ysm import animation as A  # noqa: E402
from src.ysm import geometry as G  # noqa: E402
from src.ysm import render as Rd  # noqa: E402
import pybullet as p  # noqa: E402

PKG = os.path.join(ROOT, "临时文件（记得整理掉", "苦力怕狐")
MODEL = os.path.join(PKG, "models", "main.json")
TEX = os.path.join(PKG, "textures", "skin.png")
ANIM = os.path.join(PKG, "animations", "main.animation.json")
OUT = os.path.join(ROOT, "outputs", "step2_states")
os.makedirs(OUT, exist_ok=True)

TW, TH = 480, 480
TARGET_HEIGHT = 1.8


def render_pose(bones, geo, tex, tw, th, pose, view=(3.2, 0, 6), W=TW, H=TH,
                replace_rot=False):
    M = G.bone_matrices(bones, pose, replace_rot=replace_rot)
    faces = G.build_faces(bones, M, geo["texture_width"], geo["texture_height"])
    # 中心 + 缩放（渲染空间 Y 上 -> pybullet Z 上）
    allv = np.concatenate([f[0] for f in faces])
    lo, hi = allv.min(0), allv.max(0)
    ctr = np.array([(lo[0] + hi[0]) / 2, lo[1], (lo[2] + hi[2]) / 2])
    scale = TARGET_HEIGHT / max(hi[1] - lo[1], 1e-6)
    faces = [(((v - ctr) * scale) @ G.P_MAT.T, uv, G.to_pb_vec(n)) for v, uv, n in faces]
    dist, yaw, pitch = view
    target = [0, 0, TARGET_HEIGHT * 0.5]
    Mvp = Rd.camera_matrix(target, dist, yaw, pitch, aspect=W / H)
    buf, painted = Rd.render_faces(faces, Mvp, W, H, tex, th, tw)
    return buf, painted


def main():
    rot_mode = sys.argv[1] if len(sys.argv) > 1 else "add"    # 实测 add 正确，replace 手臂缩进身体
    geo = G.load_geometry(MODEL)
    bones = geo["bones"]
    tex = np.asarray(Image.open(TEX).convert("RGBA")).astype(np.float32)
    th, tw = tex.shape[:2]
    clips = A.load_clips(ANIM)
    print(f"[资源] main.animation.json 片段 {len(clips)} | 贴图 {tw}x{th}")

    # ---- 层 ----
    pre = [clips[f"pre_parallel{i}"] for i in range(8) if f"pre_parallel{i}" in clips]
    base_vis = [clips["head:default"]] if "head:default" in clips else []
    print(f"[层] pre_parallel {len(pre)} 段 | head:default {len(base_vis)}")

    def build(clip_name, t, hood=None):
        layers = [A.sample_clip(c, 0.0) for c in pre]
        layers += [A.sample_clip(c, 0.0) for c in base_vis]
        layers.append(A.sample_clip(clips[clip_name], t))
        if hood and hood in clips:
            layers.append(A.sample_clip(clips[hood], 0.0))
        return A.compose(layers)

    jobs = []
    idle_len = clips["idle"]["length"]
    for frac in (0.0, 0.25, 0.5, 0.75):
        t = idle_len * frac
        jobs.append((f"idle t={t:.1f}s", "idle", t, "开兜帽"))
    if "new_idle_1" in clips:
        jobs.append((f"new_idle_1 t={clips['new_idle_1']['length']*0.5:.1f}s",
                     "new_idle_1", clips["new_idle_1"]["length"] * 0.5, "开兜帽"))
    jobs.append(("idle + 关兜帽", "idle", 0.0, "关兜帽"))
    jobs.append(("idle（无额外层）", "idle", 0.0, None))

    p.connect(p.DIRECT)
    tiles = []
    for label, clip, t, hood in jobs:
        pose = build(clip, t, hood)
        n_mol = A.has_molang(clips[clip])
        buf, painted = render_pose(bones, geo, tex, tw, th, pose,
                                   replace_rot=(rot_mode == "replace"))
        img = Image.fromarray(buf.astype(np.uint8))
        path = os.path.join(OUT, f"state_{len(tiles):02d}.png")
        img.save(path)
        print(f"[渲染] {label:<22} 骨骼pose {len(pose):>3} | molang帧 {n_mol:>3} "
              f"| 着色 {painted:>6} -> {os.path.basename(path)}")
        tiles.append((label, img))
    p.disconnect()

    # ---- 接触表：4 列 x N 行 ----
    cols = 3
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * TW, rows * (TH + 26)), (32, 34, 38))
    d = ImageDraw.Draw(sheet)
    for i, (label, img) in enumerate(tiles):
        x, y = (i % cols) * TW, (i // cols) * (TH + 26)
        sheet.paste(img, (x, y))
        d.text((x + 8, y + TH + 6), label, fill=(220, 224, 230))
    sheet_path = os.path.join(OUT, "states_sheet.png")
    sheet.save(sheet_path)
    print(f"\n接触表 -> {sheet_path}")


if __name__ == "__main__":
    main()
