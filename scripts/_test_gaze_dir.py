# -*- coding: utf-8 -*-
"""视线方向（左右）判据：光标在屏幕右 → 脸必须转向屏幕右。

为什么需要这个脚本
------------------
`scripts/_probe_gaze_sign.py` 早先用「**整幅轮廓**的质心 x 位移」判断 ry 的
方向，但身体 / 头发 / 裙摆都不跟着头转，头部信号被稀释到只剩 2~7 px，
方向被噪声定反了 —— 于是 `gaze.py` 里写下了"ry 为正 = 朝屏幕右"这个
**错误结论**。用户实测就是「视线绑定的左右反了」。

这里改用**几何判据**，不做任何像素统计：

    脸的朝向（模型空间）  P = R_euler(rot) @ P0
        P0 = (cos front_yaw, 0, -sin front_yaw)      # 从眼骨骼推断，
                                                      # 与 GazeTracker 同源
    转到 pb 空间          dir_pb = P_MAT @ P
    转到相机空间          v_cam  = R_view @ dir_pb
    v_cam[0] = 「脸的朝向在**屏幕右轴**上的分量」
        > 0 → 脸朝屏幕右；< 0 → 脸朝屏幕左；≈ 0 → 正对镜头

相机矩阵与渲染器**同源**（`computeViewMatrixFromYawPitchRoll`），所以这个
判据和真实出图一致；脚本末尾同时出一张对照图供人眼复核。
"""
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np                                   # noqa: E402

from pet.camera import view_matrix                   # noqa: E402
from phys.bind import P_MAT                          # noqa: E402
from pet import settings as S                        # noqa: E402
from pet import modelsource as MS                    # noqa: E402
from pet.fastrender import FastRenderer, _euler_rot  # noqa: E402
from pet.gaze import GazeTracker                     # noqa: E402

W = H = 300
CAM_YAW, CAM_PITCH = 28.0, 10.0


def face_horiz(rot, front_yaw, cam_yaw=CAM_YAW, cam_pitch=CAM_PITCH):
    """rot=[rx,ry,rz](度) 时，脸的朝向在屏幕右轴上的分量。"""
    fy = math.radians(front_yaw)
    P0 = np.array([math.cos(fy), 0.0, -math.sin(fy)])          # 模型空间
    R = _euler_rot(*np.radians(rot))[:3, :3]
    dir_model = R @ P0
    dir_pb = P_MAT @ dir_model
    vm = view_matrix((0, 0, 0), 5.0, cam_yaw, cam_pitch)
    return float((vm[:3, :3] @ dir_pb)[0])


def main():
    cfg = S.load()
    src = sys.argv[1] if len(sys.argv) > 1 else cfg.get("model")
    data = MS.load_source(src, progress=None)
    model, tex, hide = data["model"], data["tex"], data["hide"]
    fr = FastRenderer(model, tex, hide_bones=hide)
    fr.zoom = float(cfg.get("zoom", 1.42))
    fr.ssaa = 3
    fr.lighting = False

    g = GazeTracker(model)
    g.enabled = True
    g.cfg["max_yaw"] = float(cfg.get("gaze_max_yaw", 48.0))
    g.set_camera(CAM_YAW, CAM_PITCH)
    g.set_geometry((W / 2.0, H / 2.0), (W, H))

    fy = g.front_yaw()
    print("头骨骼=%s  眼骨骼=%s" % (g.head, g.eye))
    print("自动推断正面朝向 front_yaw=%.1f°  cam_yaw=%.1f°  中位 neutral=%.1f°"
          % (fy, CAM_YAW, g.neutral_yaw()))
    print("方向系数 yaw_side_sign=%+.0f" % g.yaw_side_sign())

    # 光标在正中时，脸应当正对镜头（屏幕水平分量 ≈ 0）
    g.set_cursor(W / 2.0, H / 2.0)
    yaw_c, pitch_c = g._aim()
    h_c = face_horiz([pitch_c, yaw_c, 0.0], fy)
    print("\n光标正中   ry=%+7.2f  →  脸的屏幕水平分量 %+.4f  (期望 ≈ 0)"
          % (yaw_c, h_c))

    cases = [
        ("光标在右", (W / 2.0 + g.cfg["reach_px"], H / 2.0)),
        ("光标在左", (W / 2.0 - g.cfg["reach_px"], H / 2.0)),
        ("光标在上", (W / 2.0, H / 2.0 - 400)),
        ("光标在下", (W / 2.0, H / 2.0 + 400)),
    ]

    FAIL = []
    res = {}
    print()
    for label, cur in cases:
        g.set_cursor(*cur)
        yaw, pitch = g._aim()
        h = face_horiz([pitch, yaw, 0.0], fy)
        res[label] = (yaw, pitch, h)
        print("%-8s ry=%+7.2f rx=%+7.2f  →  脸的屏幕水平分量 %+.4f"
              % (label, yaw, pitch, h))

    # ---- 判定
    print("\n判定（横向）")
    EPS = 0.05          # 满摆幅下应远超这个量级
    if res["光标在右"][2] <= EPS:
        FAIL.append("光标在右时脸没有朝屏幕右（%+.4f）" % res["光标在右"][2])
    else:
        print("  ✓ 光标在右 → 脸朝屏幕右  (%+.4f)" % res["光标在右"][2])
    if res["光标在左"][2] >= -EPS:
        FAIL.append("光标在左时脸没有朝屏幕左（%+.4f）" % res["光标在左"][2])
    else:
        print("  ✓ 光标在左 → 脸朝屏幕左  (%+.4f)" % res["光标在左"][2])
    if abs(h_c) > 0.10:
        FAIL.append("光标在正中时脸没正对镜头（%+.4f）" % h_c)
    else:
        print("  ✓ 光标正中 → 正对镜头    (%+.4f)" % h_c)

    # 左右摆幅对称（不能一边大一边小）
    lr = abs(res["光标在右"][2]) - abs(res["光标在左"][2])
    if abs(lr) > 0.05:
        FAIL.append("左右摆幅不对称（差 %+.4f）" % lr)
    else:
        print("  ✓ 左右摆幅对称            (差 %+.4f)" % lr)

    print("\n判定（纵向）")
    up, dn = res["光标在上"][1], res["光标在下"][1]
    if not (up < 0 < dn):
        FAIL.append("纵向反了：光标在上 rx=%+.1f，光标在下 rx=%+.1f" % (up, dn))
    else:
        print("  ✓ 光标在上 → 抬头 (rx=%+.1f) / 光标在下 → 低头 (rx=%+.1f)"
              % (up, dn))

    # ---- 出图供人眼复核
    try:
        from PIL import Image, ImageDraw, ImageFont
        OUT = os.path.join(ROOT, "outputs", "gaze_dir")
        os.makedirs(OUT, exist_ok=True)
        tiles = []
        for label, cur in [("cursor:RIGHT", cases[0][1]),
                           ("cursor:CENTER", (W / 2.0, H / 2.0)),
                           ("cursor:LEFT", cases[1][1])]:
            g.set_cursor(*cur)
            yaw, pitch = g._aim()
            anim = g.to_anim({"yaw": yaw, "pitch": pitch})
            rgba = fr.render(anim, W, H, yaw=CAM_YAW, pitch=CAM_PITCH)
            a = rgba[..., 3:4].astype(np.float32) / 255.0
            rgb = (rgba[..., :3].astype(np.float32) * a
                   + 240 * (1 - a)).astype(np.uint8)
            m = np.any(np.abs(rgb.astype(int) - 240) > 12, axis=-1)
            ys, xs = np.nonzero(m)
            y0, y1 = ys.min(), ys.max()
            crop = Image.fromarray(
                rgb[max(0, y0 - 4):y0 + int((y1 - y0 + 1) * 0.5),
                    max(0, xs.min() - 4):min(W, xs.max() + 4)])
            SC = 3
            tiles.append((label, crop.resize(
                (crop.width * SC, crop.height * SC), Image.LANCZOS)))
        gap = 8
        tw = sum(t[1].width for t in tiles) + gap * (len(tiles) - 1)
        th = max(t[1].height for t in tiles)
        canvas = Image.new("RGB", (tw, th + 26), (240, 240, 240))
        d = ImageDraw.Draw(canvas)
        fnt = ImageFont.load_default()
        x = 0
        for label, im in tiles:
            d.text((x + 4, 6), label, fill=(0, 0, 0), font=fnt)
            canvas.paste(im, (x, 26))
            x += im.width + gap
        pth = os.path.join(OUT, "gaze_dir.png")
        canvas.save(pth)
        print("\n对照图（左 → 中 → 右，人眼复核）:", pth)
    except Exception as e:                                # noqa: BLE001
        print("出图跳过：%s" % e)

    print("\n结果:", "✓ 视线左右方向正确" if not FAIL else "✗ %s" % FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
