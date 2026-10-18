# -*- coding: utf-8 -*-
"""验证「朝向」（相机 yaw）是**绕模型纵轴**旋转，而不是绕一根外来的竖轴。

判据：把 yaw 扫一圈，模型在画面里的**轮廓中心**应当基本不动（原地自转）。
如果枢轴（`pctr`）被推离模型纵轴，轮廓中心会随 yaw 大幅漂移 ——
用户报的"以莫名其妙的外纵轴旋转"。

最后故意把 pctr 水平分量推偏，展示这个指标确实能抓到该 bug。
"""
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np                                  # noqa: E402

from pet import settings as S                        # noqa: E402
from pet import modelsource as MS                    # noqa: E402
from pet.fastrender import FastRenderer              # noqa: E402
from ysm.clips import ClipLibrary, ResidentPlayer    # noqa: E402

BASE_H, PAD = 340, 1.25


def sweep(fr, W, H, label):
    cx, cy = [], []
    for yaw in range(0, 360, 30):
        rgba = fr.render({}, W, H, yaw=float(yaw), pitch=10.0)
        ys, xs = np.nonzero(rgba[..., 3])
        if len(xs) < 8:
            continue
        cx.append(float(xs.mean()))
        cy.append(float(ys.mean()))
    cx = np.array(cx)
    cy = np.array(cy)
    dx = cx.max() - cx.min()
    dy = cy.max() - cy.min()
    print("  %-22s 轮廓中心漂移  横向 %.1f px (%.1f%% 窗宽)  纵向 %.1f px"
          % (label, dx, 100 * dx / W, dy))
    return dx, dy


def main():
    cfg = S.load()
    d = MS.load_source(cfg.get("model"), progress=None)
    model, tex, hide = d["model"], d["tex"], d["hide"]
    lib = ClipLibrary(model)
    res = ResidentPlayer(lib)

    fr = FastRenderer(model, tex, hide_bones=hide)
    fr.zoom = float(cfg.get("zoom", 1.42))
    fr.ssaa = 1
    r = fr.measure_ratio(None, lib, cache_key=None, resident=res)
    W = max(48, int(round(BASE_H * r * PAD)))
    H = BASE_H
    print("比例=%.4f 窗口=%dx%d pctr=%s"
          % (r, W, H, [round(float(v), 4) for v in fr.pctr]))

    print("\n[正确] 枢轴在模型纵轴上：")
    ok_dx, ok_dy = sweep(fr, W, H, "pctr 水平=0")

    print("\n[模拟旧 bug] 把 pctr 水平分量推偏 0.3 pb：")
    saved = fr.pctr.copy()
    fr.pctr = np.array([saved[0] + 0.3, saved[1] - 0.3, saved[2]], float)
    bad_dx, bad_dy = sweep(fr, W, H, "pctr 水平偏移 0.3")
    fr.pctr = saved

    print("\n判定：正确时应 < 8% 窗宽；有偏移时明显更大。")
    ok = ok_dx < 0.08 * W
    print("结果:", "通过 ✓" if ok and bad_dx > ok_dx * 1.5 else "失败 ✗",
          "（正确 %.1f px vs 偏移 %.1f px）" % (ok_dx, bad_dx))
    return 0 if (ok and bad_dx > ok_dx * 1.5) else 1


if __name__ == "__main__":
    raise SystemExit(main())
