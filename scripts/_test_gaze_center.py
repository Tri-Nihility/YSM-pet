# -*- coding: utf-8 -*-
"""视线中心锚点验收（2026-10-09 用户："视线中心没绑定对，你绑成头颅左上角了"）。

之前 GazeTracker 的参考中心是窗口几何中心 (W/2, H/2) = 模型包围盒中心 ≈
胸口高度，于是"光标对准脸"时算出的 dx/dy 是相对胸口中心，头会向上/偏着转，
看着像锚在了头颅左上角。

修复：用渲染器同一套 view/projection 矩阵把**眼骨骼**（没有就用头骨骼）的
rest 世界平移投影到窗口像素，作为视线参考中心。

本脚本验收：
  1. 投影出的锚点确实在脸的位置（屏幕 y 明显小于窗口中心，且落在模型上）
  2. 光标对准锚点 → 头正对镜头（yaw≈中位、pitch≈0）
  3. 光标在胸口（窗口中心）→ 头自然朝下（pitch>0），而非旧 bug 的朝上
  4. 旧锚点 (W/2,H/2) 在"光标对准脸"时会算出 pitch<0（头朝上），证实 bug
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np                                   # noqa: E402

from pet import settings as S                        # noqa: E402
from pet import modelsource as MS                     # noqa: E402
from pet.fastrender import FastRenderer               # noqa: E402
from pet.gaze import GazeTracker                      # noqa: E402


def main():
    cfg = S.load()
    src = sys.argv[1] if len(sys.argv) > 1 else cfg.get("model")
    data = MS.load_source(src, progress=None)
    model, tex, hide = data["model"], data["tex"], data["hide"]
    fr = FastRenderer(model, tex, hide_bones=hide)
    fr.zoom = float(cfg.get("zoom", 1.42))
    fr.ssaa = 3

    g = GazeTracker(model)
    g.enabled = True
    CAM_YAW, CAM_PITCH = 28.0, 10.0
    g.set_camera(CAM_YAW, CAM_PITCH)

    print("头骨骼=%s  眼骨骼=%s" % (g.head, g.eye))

    W = H = 300
    bn = g.eye or g.head
    assert bn is not None, "模型无头/眼骨骼，无法做视线锚点"

    # ---- 1) 投影锚点
    # 眼骨骼可能只是眼珠方块的父骨骼、自身没有 cube（如 Eyes）→ 投影返回 None，
    # 这时退回头骨骼质心（与 petapp._gaze_anchor_for 一致）。
    anchor = fr.screen_pos_of_bone(bn, W, H, CAM_YAW, CAM_PITCH)
    if anchor is None and g.head is not None:
        print("  （眼骨骼 %s 无 cube，退化用头骨骼 %s 质心）" % (bn, g.head))
        bn = g.head
        anchor = fr.screen_pos_of_bone(bn, W, H, CAM_YAW, CAM_PITCH)
    assert anchor is not None, "眼/头骨骼都取不到可见质心，无法做视线锚点"
    print("\n[1] 投影锚点（%s）: sx=%.1f sy=%.1f  （窗口中心 %.1f,%.1f）"
          % (bn, anchor[0], anchor[1], W / 2.0, H / 2.0))
    FAIL = []
    if anchor[1] >= H / 2.0:
        FAIL.append("锚点 y=%.1f 不在脸（应明显小于窗口中心 %.1f）"
                    % (anchor[1], H / 2.0))
    else:
        print("  ✓ 锚点在脸（屏幕上方），不在胸口中心")

    # 锚点必须真的落在模型上：渲染静止姿势，确认模型 alpha 包围盒覆盖锚点
    rgba = fr.render({}, W, H, yaw=CAM_YAW, pitch=CAM_PITCH)
    ys, xs = np.nonzero(rgba[..., 3])
    assert len(xs) > 50, "渲染不出模型"
    top_y, bot_y = ys.min(), ys.max()
    left_x, right_x = xs.min(), xs.max()
    print("  模型 alpha 包围盒: x[%d,%d] y[%d,%d]"
          % (left_x, right_x, top_y, bot_y))
    if not (left_x - 4 <= anchor[0] <= right_x + 4
            and top_y - 4 <= anchor[1] <= bot_y + 4):
        FAIL.append("锚点不在模型包围盒内: (%d,%d)" % anchor)
    else:
        print("  ✓ 锚点落在模型包围盒内（确实是脸上某点）")
    # 脸应该在模型上半部（头顶往下约 45% 内）
    face_band = top_y + (bot_y - top_y) * 0.45
    if anchor[1] > face_band:
        FAIL.append("锚点偏下（y=%.1f > 上半部阈值 %.1f），像锚在胸口"
                    % (anchor[1], face_band))
    else:
        print("  ✓ 锚点在模型上半部（脸区域）")

    # ---- 2) 光标对准脸 → 正对镜头
    g.set_geometry(anchor, (W, H))
    g.set_cursor(*anchor)
    yaw_f, pitch_f = g._aim()
    neutral = g.neutral_yaw()
    print("\n[2] 光标对准脸: yaw=%.2f pitch=%.2f  （中位 %.2f）"
          % (yaw_f, pitch_f, neutral))
    if abs(yaw_f - neutral) > 1.0 or abs(pitch_f) > 1.0:
        FAIL.append("光标对准脸时头没正对镜头（yaw偏%.2f pitch偏%.2f）"
                    % (yaw_f - neutral, pitch_f))
    else:
        print("  ✓ 光标对准脸 → 头正对镜头（yaw≈中位 pitch≈0）")

    # ---- 3) 光标在胸口（窗口中心）→ 自然朝下
    g.set_cursor(W / 2.0, H / 2.0)
    yaw_c, pitch_c = g._aim()
    print("\n[3] 光标在窗口中心(胸口): yaw=%.2f pitch=%.2f"
          % (yaw_c, pitch_c))
    if not (pitch_c > 0):
        FAIL.append("光标在胸口时头没朝下（pitch=%.2f）" % pitch_c)
    else:
        print("  ✓ 光标在胸口 → 头自然朝下（pitch>0）")

    # ---- 4) 反证旧 bug：旧锚点 (W/2,H/2) 下，光标对准脸会算出 pitch<0
    g.set_geometry((W / 2.0, H / 2.0), (W, H))
    g.set_cursor(*anchor)
    yaw_old, pitch_old = g._aim()
    print("\n[4] 旧锚点(窗口中心)下光标对准脸: pitch=%.2f" % pitch_old)
    if pitch_old >= 0:
        FAIL.append("反证失败：旧锚点下光标对准脸却没朝上（pitch=%.2f），"
                    "无法确认旧 bug" % pitch_old)
    else:
        print("  ✓ 旧锚点下光标对准脸会算出 pitch<0（头朝上）—— 正是用户"
              "看到的『像锚在头颅左上角』，现已修复")

    print("\n结果:", "✓ 视线中心锚点正确（绑在脸上）" if not FAIL
          else "✗ %s" % FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
