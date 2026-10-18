"""
步骤 3：.ysm 端到端解析 + 渲染验证。

    .ysm  --[ysmcrypt]--> 明文 --[ysmbin]--> RawYsmModel --[ysmmodel]--> 渲染

产出
    data/decrypted/<name>.bin          解密明文
    outputs/step3/<name>/<tex>.png     贴图（RGBA）
    outputs/step3/<name>/bind.png      bind pose 三视图
    outputs/step3/<name>/poses.png     若干常态/动作姿态
    outputs/step3/<name>/model.json    结构摘要
"""
import json
import os
import sys

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from ysm import ysmbin as B          # noqa: E402
from ysm import ysmcrypt as Y        # noqa: E402
from ysm import ysmmodel as YM       # noqa: E402
from ysm.render import camera_matrix, render_faces  # noqa: E402

TMP = os.path.join(ROOT, "临时文件（记得整理掉")
OUT = os.path.join(ROOT, "outputs", "step3")
DEC = os.path.join(ROOT, "data", "decrypted")

W = H = 520
P_MAT = np.array([[1.0, 0, 0], [0, 0, 1.0], [0, 1.0, 0]])   # 模型空间(Y上) -> pybullet(Z上)


def load(name, ysm_file):
    src = os.path.join(TMP, ysm_file)
    binp = os.path.join(DEC, name + ".bin")
    if not os.path.exists(binp):
        os.makedirs(DEC, exist_ok=True)
        data = open(src, "rb").read()
        c = Y.parse_container(data)
        print(f"  crypto={c['crypto']} hash_ok={c['hash_ok']}")
        open(binp, "wb").write(Y.decrypt_ysm_file(data))
    return B.deserialize(open(binp, "rb").read())


def dump_textures(raw, outdir):
    os.makedirs(outdir, exist_ok=True)
    out = {}
    for tname, tex in raw.textures.items():
        w, h = tex["width"], tex["height"]
        if w <= 0 or h <= 0 or not tex["data"]:
            continue
        img = Image.frombytes("RGBA", (w, h), tex["data"])
        safe = tname.replace("/", "_").replace("\\", "_")
        p = os.path.join(outdir, f"tex_{safe}.png")
        img.save(p)
        out[tname] = {"path": p, "size": (w, h),
                      "sub": [s["specular_type"] for s in tex["sub"]]}
    return out


def render(model, tex, anims, outdir, tag, hide=()):
    """anims: [(label, anim_dict_or_None)]，拼成一行。hide: 要剔除的骨骼名。"""
    bind = [f for f in model.faces(None) if True]
    allv = np.vstack([np.asarray(f["pos"], float)
                      for b in model.order if b["cubes"] and b["name"] not in hide
                      for c in b["cubes"] for f in c["faces"]])
    ctr = (allv.max(0) + allv.min(0)) / 2
    radius = float(np.linalg.norm(allv - ctr, axis=1).max())
    fov = 40.0
    dist = radius / np.tan(np.radians(fov / 2)) * 1.28
    yaw, pitch = 25, 8

    tiles = []
    for label, anim in anims:
        M, vis = model.pose(anim)
        for h in hide:
            vis[h] = False
        faces = model.faces(M=M, vis=vis)
        Mv = camera_matrix(P_MAT @ ctr, dist, yaw, pitch, 1.0, fov)
        ff = [((v - ctr) @ P_MAT.T, uv, n @ P_MAT.T) for v, uv, n in faces]
        buf, painted = render_faces(ff, Mv, W, H, tex, tex.shape[0], tex.shape[1])
        tiles.append((label, buf, painted))
    cols = len(tiles)
    sheet = np.full((H, W * cols, 3), 255.0)
    for i, (_, b, _) in enumerate(tiles):
        sheet[:, i * W:(i + 1) * W] = b
    Image.fromarray(sheet.astype(np.uint8)).save(os.path.join(outdir, tag + ".png"))
    return [(l, p) for l, _, p in tiles]


def main():
    os.makedirs(OUT, exist_ok=True)
    targets = [("atri", "亚托莉（目标模型）.ysm"),
               ("irene", "爱恩.伊瑞丝.卡列尼娜.ysm")]

    for name, f in targets:
        print(f"\n=== {name} : {f} ===")
        raw = load(name, f)
        outdir = os.path.join(OUT, name)
        model = YM.YsmModel(raw, "main_model")
        st = model.stats()
        print("  几何:", st)
        print("  动画组:", {k: len(v) for k, v in raw.animations.items()})
        print("  控制器:", len(raw.controllers))

        texs = dump_textures(raw, outdir)
        print("  贴图:", {k: v["size"] for k, v in texs.items()})

        # 主贴图（跳过箭矢 32x32）
        main_tex_name = max(texs, key=lambda k: texs[k]["size"][0] * texs[k]["size"][1])
        tex = np.asarray(Image.open(texs[main_tex_name]["path"]).convert("RGBA")).astype(float)
        tw, th = texs[main_tex_name]["size"]
        print(f"  使用贴图: {main_tex_name} {tw}x{th}")

        main_anims = raw.animations.get("main", {})
        anims = [("bind (无动画)", None)]
        # 优先常用状态；跳过空占位片段（0 骨骼，作者留空）
        preferred = ("idle", "hover", "focus", "walk", "run", "jump", "sneak", "swim", "gui")
        for key in preferred:
            clip = main_anims.get(key)
            if not clip or not clip.get("bones"):
                continue
            anims.append((f"{key}@0", YM.sample_clip(clip, 0.0)))
            if key in ("walk", "run", "swim") and clip.get("length", 0) not in (0,):
                import math
                if math.isfinite(clip["length"]) and clip["length"] > 0:
                    anims.append((f"{key}@半",
                                  YM.sample_clip(clip, clip["length"] / 2)))
        anims = anims[:10]
        # bk/KJ/bone = gui 预览用背景板；ysmGlow* = 自发光特效件（多数动画里被 scale=0 隐藏）
        info = render(model, tex, anims, outdir, "poses",
                      hide=("bk", "KJ", "bone", "Background",
                            "ysmGlowDD6", "ysmGlowbackground"))
        print("  渲染:", [(l, p) for l, p in info])

        summary = {
            "format": raw.format_version,
            "geometry": st,
            "end_offset": raw.end_offset,
            "animations": {k: sorted(v.keys()) for k, v in raw.animations.items()},
            "controllers": [c["name"] for c in raw.controllers],
            "textures": {k: {"size": v["size"], "sub": v["sub"]} for k, v in texs.items()},
            "properties": raw.properties,
            "metadata": {k: v for k, v in raw.metadata.items() if k != "authors"},
            "authors": [a.get("name") for a in raw.metadata.get("authors", [])],
        }
        with open(os.path.join(outdir, "model.json"), "w", encoding="utf-8") as fp:
            json.dump(summary, fp, ensure_ascii=False, indent=2)
        print("  摘要 ->", os.path.join(outdir, "model.json"))


if __name__ == "__main__":
    main()
