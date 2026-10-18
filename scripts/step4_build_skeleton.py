"""阶段 4：构建九自由度物理骨架 + 生成 URDF + 渲染零位姿/随机位姿验证。

产出
    data/processed/skeleton9.json      分段/质量/包围盒摘要
    models/ysm_atri_9dof.urdf          pybullet 用的 URDF（root=pelvis，浮动基）
    outputs/step4/{zero,range}.png     零位姿 / 各关节推到限位两端的预览
"""
import json
import os
import sys

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from ysm import ysmbin as B          # noqa: E402
from ysm import ysmmodel as YM       # noqa: E402
from ysm.render import camera_matrix, render_faces  # noqa: E402
from phys.bind import P_MAT, Skeleton9  # noqa: E402

DEC = os.path.join(ROOT, "data", "decrypted")
OUT = os.path.join(ROOT, "outputs", "step4")
TILE = 300


def load(model_file):
    return B.deserialize(open(os.path.join(DEC, model_file), "rb").read())


def pick_texture(raw, outdir):
    best, best_size = None, -1
    for name, tex in raw.textures.items():
        w, h = tex["width"], tex["height"]
        if w <= 0 or h <= 0 or not tex["data"]:
            continue
        if w * h > best_size:
            best, best_size = name, w * h
    img = Image.frombytes("RGBA", (raw.textures[best]["width"],
                                   raw.textures[best]["height"]),
                          raw.textures[best]["data"])
    img.save(os.path.join(outdir, "tex.png"))
    return np.asarray(img.convert("RGBA")).astype(float), img.size


def tile(model, tex, tw, th, anims, ncol, combine=True, zoom=1.0):
    """anims: [(label, anim)] -> 一行/网格拼图。"""
    M0, _ = model.pose(None)
    allv = np.vstack([np.asarray(f["pos"], float)
                      for b in model.order if b["cubes"] and b["name"] not in set()
                      for c in b["cubes"] for f in c["faces"]])
    ctr = (allv.max(0) + allv.min(0)) / 2
    radius = float(np.linalg.norm(allv - ctr, axis=1).max())
    fov = 40.0
    dist = radius / np.tan(np.radians(fov / 2)) * 1.25 / zoom
    W = H = TILE
    tiles = []
    for label, anim in anims:
        M, vis = model.pose(anim)
        faces = model.faces(M=M, vis=vis)
        Mv = camera_matrix(P_MAT @ ctr, dist, 28, 10, 1.0, fov)
        ff = [((v - ctr) @ P_MAT.T, uv, n @ P_MAT.T) for v, uv, n in faces]
        buf, _ = render_faces(ff, Mv, W, H, tex, tw, th)
        tiles.append((label, buf))
    nrow = (len(tiles) + ncol - 1) // ncol
    sheet = np.full((H * nrow, W * ncol, 3), 255.0)
    for i, (_, buf) in enumerate(tiles):
        r, c = divmod(i, ncol)
        sheet[r * H:(r + 1) * H, c * W:(c + 1) * W] = buf
    return sheet, [t[0] for t in tiles]


def main():
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(os.path.join(ROOT, "data", "processed"), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "models"), exist_ok=True)

    model_file = "亚托莉目标模型.bin"
    raw = load(model_file)
    model = YM.YsmModel(raw, "main_model")
    print("几何:", model.stats())

    sk = Skeleton9(model)
    summary = sk.summary()
    print("\n=== 九自由度骨架 ===")
    print("关节顺序:", summary["joint_order"])
    print("对应骨骼:", summary["joint_bones"])
    print(f"脚底 y={summary['sole_y']}  站立髋高={summary['stand_height_blocks']} 方块")
    print(f"总质量 {summary['total_mass_kg']} kg")
    print("\n分段表:")
    print(f"  {'segment':10s} {'parent':10s} {'root_bone':12s} {'骨骼数':>5s} {'体积':>8s} {'质量kg':>8s}")
    for r in summary["segments"]:
        print(f"  {r['segment']:10s} {str(r['parent']):10s} {r['root_bone']:12s} "
              f"{r['bones']:5d} {r['volume_blk3']:8.3f} {r['mass_kg']:8.3f}")

    with open(os.path.join(ROOT, "data", "processed", "skeleton9.json"), "w",
              encoding="utf-8") as fp:
        json.dump(summary, fp, ensure_ascii=False, indent=2)

    urdf = os.path.join(ROOT, "models", "ysm_atri_9dof.urdf")
    sk.build_urdf(urdf)
    print("\nURDF ->", urdf, os.path.getsize(urdf), "bytes")

    # ---------------- 渲染验证 ----------------
    tex, (tw, th) = pick_texture(raw, OUT)
    hide_anim = {n: {"scale": [0, 0, 0]} for n in sk.hidden if n in sk.by_name}

    zero = sk.joint_to_anim(np.zeros(9))
    zero.update(hide_anim)
    sheet, _ = tile(model, tex, tw, th, [("零位姿", zero)], 1)
    Image.fromarray(sheet.astype(np.uint8)).save(os.path.join(OUT, "zero.png"))

    # 每个关节分别推到两端（其余保持 default），检查桥接方向是否正确
    base = sk.default_pose()
    panels = []
    for i, j in enumerate(sk.joints):
        for tag, val in (("min", j["limits"][0]), ("max", j["limits"][1])):
            q = base.copy()
            q[i] = val
            a = sk.joint_to_anim(q)
            a.update(hide_anim)
            panels.append((f"{j['label']} {tag}{val:+.0f}", a))
    sheet, labels = tile(model, tex, tw, th, panels, 6)
    Image.fromarray(sheet.astype(np.uint8)).save(os.path.join(OUT, "joint_range.png"))
    print("\n限位预览顺序:", labels)

    # 一个「站立初期」的手摆 pose：髋微屈、膝微屈
    q = np.array([18, 18, -25, -25, -10, -10, 12, -12, 0.0])
    a = sk.joint_to_anim(q)
    a.update(hide_anim)
    sheet, _ = tile(model, tex, tw, th, [("手摆 pose", a)], 1)
    Image.fromarray(sheet.astype(np.uint8)).save(os.path.join(OUT, "demo_pose.png"))
    print("\n产出 ->", OUT)


if __name__ == "__main__":
    main()
