# -*- coding: utf-8 -*-
"""端到端验收：光标在屏幕最左 / 最右 / 最上 / 最下时，
真实走 GazeTracker._aim → to_anim 得到头骨骼 rot，渲染出来对照。

判定：光标在右 → 头应转向**屏幕右**；光标在上 → 头应**抬起**。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np                                  # noqa: E402
from PIL import Image, ImageDraw, ImageFont          # noqa: E402

from pet import settings as S                        # noqa: E402
from pet import modelsource as MS                    # noqa: E402
from pet.fastrender import FastRenderer              # noqa: E402
from pet.gaze import GazeTracker                     # noqa: E402

OUT = os.path.join(ROOT, "outputs", "gaze_sign")
os.makedirs(OUT, exist_ok=True)
font = ImageFont.load_default()


def main():
    cfg = S.load()
    src = sys.argv[1] if len(sys.argv) > 1 else cfg.get("model")
    data = MS.load_source(src, progress=None)
    model, tex, hide = data["model"], data["tex"], data["hide"]
    fr = FastRenderer(model, tex, hide_bones=hide)
    fr.zoom = float(cfg.get("zoom", 1.42))
    fr.ssaa = 3
    fr.lighting = False

    W = H = 300
    YAW, PITCH = 28.0, 10.0

    g = GazeTracker(model)
    g.enabled = True
    g.cfg["max_yaw"] = float(cfg.get("gaze_max_yaw", 48.0))
    g.set_camera(YAW, PITCH)
    g.set_geometry((W / 2.0, H / 2.0), (W, H))
    print("头骨骼=%s  眼骨骼=%s" % (g.head, g.eye))

    cases = [
        ("cursor:RIGHT", (W * 1.9, H / 2.0)),
        ("cursor:LEFT", (-W * 0.9, H / 2.0)),
        ("cursor:UP", (W / 2.0, -H * 0.9)),
        ("cursor:DOWN", (W / 2.0, H * 1.9)),
        ("cursor:CENTER", (W / 2.0, H / 2.0)),
    ]

    imgs = []
    got = {}
    for label, cur in cases:
        g.set_cursor(*cur)
        g._last_move = 1e18                    # 关掉空闲回正
        yaw, pitch = g._aim()
        got[label] = (yaw, pitch)
        anim = g.to_anim({"yaw": yaw, "pitch": pitch})
        rgba = fr.render(anim, W, H, yaw=YAW, pitch=PITCH)
        a = rgba[..., 3:4].astype(np.float32) / 255.0
        rgb = (rgba[..., :3].astype(np.float32) * a
               + 240 * (1 - a)).astype(np.uint8)
        imgs.append((label, rgb))
        print("  %-14s -> yaw=%+7.2f  pitch=%+7.2f  anim=%s"
              % (label, yaw, pitch,
                 {k: [round(x, 1) for x in v["rot"]] for k, v in anim.items()}))

    # 判据：yaw 是**绝对角**（含"正对镜头"的中位 61.4°），不能只看符号。
    # 且 ry 增大 = 脸朝屏幕**左**（见 _test_gaze_dir.py 的几何推导），
    # 所以「光标在右」必须让 yaw **小于**中位。
    neutral = g.neutral_yaw()
    yr, yl = got["cursor:RIGHT"][0], got["cursor:LEFT"][0]
    pu, pd = got["cursor:UP"][1], got["cursor:DOWN"][1]
    ok = (yr < neutral < yl) and pu < 0 < pd
    print("标注：中位=%.1f°  光标在右 → yaw 必须<中位；光标在上 → pitch 必须<0"
          % neutral)
    print("判定：%s" % ("✓ 左右/上下方向正确"
                       if ok else "✗ 方向不对 yr=%.1f yl=%.1f" % (yr, yl)))

    # 拼图（裁头，放大；标签用 ASCII 以免 PIL 默认字体画不出中文）
    SC = 3
    tiles = []
    for label, rgb in imgs:
        m = np.any(np.abs(rgb.astype(int) - 240) > 12, axis=-1)
        ys, xs = np.nonzero(m)
        y0, y1 = ys.min(), ys.max()
        hh = y1 - y0 + 1
        crop = Image.fromarray(rgb[max(0, y0 - 4):y0 + int(hh * 0.46),
                                   max(0, xs.min() - 4):min(W, xs.max() + 4)])
        tiles.append((label, crop.resize((crop.width * SC, crop.height * SC),
                                         Image.LANCZOS)))
    gap = 8
    tw = sum(t[1].width for t in tiles) + gap * (len(tiles) - 1)
    th = max(t[1].height for t in tiles)
    canvas = Image.new("RGB", (tw, th + 26), (240, 240, 240))
    d = ImageDraw.Draw(canvas)
    x = 0
    for label, im in tiles:
        d.text((x + 4, 6), label, fill=(0, 0, 0), font=font)
        canvas.paste(im, (x, 26))
        x += im.width + gap
    p = os.path.join(OUT, "gaze_e2e.png")
    canvas.save(p)
    print("已保存:", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
