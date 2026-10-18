"""桌宠新功能无头自检：视线追踪 + 模型自带动画 + 环形轮盘。

用 Qt offscreen 平台跑，不弹窗口。验证：
    1. GazeTracker 方向映射正确（右/左/上/下 → 头旋转符号对）
    2. eye_parts 全部一起转
    3. ClipLibrary 解出的动画能采样出非单位矩阵
    4. ClipWheel 尺寸/命中测试可用
    5. 叠加顺序：视线层不被待机层覆盖
"""
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np                                            # noqa: E402
from PySide6 import QtWidgets, QtCore, QtGui                   # noqa: E402


def main():
    # 模型来源走 `modelsource`（与 run_pet.py 同一条路径），吃 .ysm / 文件夹 /
    # zip·7z·rar；不传就读上次记住的模型。以前这里用 `loader.load_all()`，
    # 它默认去 data/decrypted/*.bin —— 那是 gitignore 掉的本机产物，
    # 别人 clone 下来必然 FileNotFoundError。
    src = sys.argv[1] if len(sys.argv) > 1 else None
    if not src:
        from pet import settings as S
        src = S.load().get("model")
    if not src:
        print("用法: python scripts/_test_pet_features.py "
              "<模型来源 .ysm/文件夹/压缩包>")
        return 2

    from pet import modelsource as MS
    from pet.gaze import GazeTracker
    from ysm.clips import ClipLibrary

    d = MS.load_source(src, progress=None)
    model = d["model"]
    print("骨骼=%d" % len(model.order))

    # ---------------------------------------------------------- 1 视线方向
    g = GazeTracker(model)
    print("头=%s 眼=%s 驱动=%s"
          % (g.head, g.eye, list(g.eye_driven)))
    g.enabled = True
    g.set_geometry((100, 150), (200, 300))      # 中心(100,150)

    def probe(px, py, label):
        g._cur_yaw = g._cur_pitch = 0.0
        g._last_t = 0.0
        g.set_cursor(px, py)
        # 直接算目标角，绕开平滑
        g._last_move = __import__("time").time()
        yaw, pitch = g._aim()
        a = g.to_anim({"yaw": yaw, "pitch": pitch})
        hr = a.get(g.head, {}).get("rot", [0, 0, 0])
        print("  %-10s yaw=%+7.2f pitch=%+7.2f  头rot=%s  层数=%d"
              % (label, yaw, pitch,
                 ["%+.1f" % v for v in hr], len(a)))
        return yaw, pitch, a

    print("方向映射（模型空间 +X 右 / +Y 上）：")
    y_r, p_r, a_r = probe(600, 150, "光标在右")
    y_l, p_l, a_l = probe(-400, 150, "光标在左")
    y_u, p_u, a_u = probe(100, -200, "光标在上")
    y_d, p_d, a_d = probe(100, 500, "光标在下")

    # 符号由**几何实测**定（scripts/_test_gaze_dir.py）：
    #   ry 越大 → 头越朝屏幕**左**转。推导：脸的屏幕水平分量 P·R = cos(ry+ψ)，
    #   中位处 ry+ψ = front_yaw≈90°，d/dry = -sin(front_yaw) < 0。
    #   ⚠️ 旧注释写的"ry 为正 = 朝屏幕右"是错的，来自 _probe_gaze_sign.py
    #   早期用「整幅轮廓质心」做判据（信号仅 2~7px，被身体稀释后方向定反）。
    #   rx 越大 → 头越低头。
    #
    # 2026-10-05：引入「正面朝向补偿」后，yaw 不再是"围绕 0"，而是围绕
    # 中位 neutral（= front_yaw - cam_yaw，光标在正中时头正对镜头）。
    # 所以判据改成「相对于中位的摆幅」：光标在右 → yaw-neutral < 0。
    neutral = g.neutral_yaw()
    print("  中位（正对镜头）neutral=%+.2f°  自动推断 front_yaw=%s"
          % (neutral,
             ("%.1f°" % g.auto_front_yaw) if g.auto_front_yaw is not None
             else "None(用兜底)"))
    # 正面朝向应能自动推断出来（用眼骨骼位置），且本模型实测 ≈89.4°。
    assert g.auto_front_yaw is not None, "正面朝向自动推断失败"
    assert abs(g.auto_front_yaw - 90.0) < 15.0, \
        "自动推断的 front_yaw 偏离预期：%.1f" % g.auto_front_yaw
    assert abs(neutral - 62.0) < 8.0, \
        "相机 yaw=28 时中位应≈62°（正对镜头），实际 %.1f" % neutral
    assert g.yaw_side_sign() < 0, \
        "front_yaw≈90 的模型方向系数应为 -1（光标在右 → ry 减小）"
    assert y_r - neutral < 0 and y_l - neutral > 0, \
        "左右摆幅符号错（光标在右应当减小 ry）"
    assert p_u < 0 and p_d > 0, "上下 pitch 符号错"
    assert abs((y_r - neutral) + (y_l - neutral)) < 1e-6, "左右摆幅不对称"
    print("  → 摆幅左右对称、上下符号正确 ✓"
          "（光标右→右转 Δ%+.1f°，光标左→左转 Δ%+.1f°；"
          "光标上→抬头 pitch<0，光标下→低头 pitch>0）"
          % (y_r - neutral, y_l - neutral))

    # ---------------------------------------------------------- 2 转向绑定
    # 正确行为（2026-10-05，用户口径"眼睛只用来算朝哪边，本身别转"）：
    #   * 只写头骨骼（AllHead）一条；
    #   * **眼骨骼完全不写**——MC 皮肤的眼睛是脸上贴图，转眼会让眼珠滑出眼窝；
    #   * 眼骨骼的一切子孙（眼球块/眉毛/眼睑）自然也不会被写到。
    # 旧实现（更早）：把 24 个带 "eye" 的子孙各写一遍 rot，
    # pose_matrices 父子累积 → 旋转叠加 2~6 层，五官错位。
    assert g.head in a_r, "头骨骼没被驱动"
    assert g.eye not in a_r, "眼骨骼被转了（应只转头、不转眼）"
    desc = [n for n in g.names if g._is_descendant(n, g.eye)]
    leaked = [n for n in desc if n in a_r]
    assert not leaked, "眼骨骼的后代被写了 rot（会五官错位）: %s" % leaked[:6]
    assert len(a_r) == 1 and g.head in a_r, \
        "转向层应只有头骨骼一条，实际 %s" % list(a_r)
    print("  → 只写头骨骼 %s 一条；眼睛（含 %d 个眼部件）完全不动 ✓"
          % (g.head, len(desc)))

    # ---------------------------------------------------------- 3 动画库
    lib = ClipLibrary(model)
    print("自带动画: %d 个" % len(lib))
    moving = 0
    for name in lib.names():
        c = lib.get(name)
        pose = c.sample(0.0 if c.static else c.length * 0.5)
        if pose:
            moving += 1
    print("  可采样出姿势的动画: %d" % moving)
    assert moving >= 30, "可播放动画太少: %d" % moving
    # 抽一个非静态动画验证采样值非全零
    for name in lib.names():
        c = lib.get(name)
        if not c.static and c.n_bones > 20:
            pose = c.sample(c.length * 0.5)
            nz = sum(1 for b, ch in pose.items()
                     if "rot" in ch and any(abs(x) > 1e-6 for x in ch["rot"]))
            print("  样例 %s: %d 骨骼, 其中 %d 根有转角" % (name, len(pose), nz))
            break

    # ---------------------------------------------------------- 4 轮盘 + 窗口
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from pet.fastrender import FastRenderer
    from pet.petapp import IdleAnimator, PetWindow

    fr = FastRenderer(model, d["tex"], hide_bones=d["hide"])
    an = IdleAnimator(model)
    w = PetWindow(fr, an, rig=None, gaze=g, clips=lib, scale=1.5, fps=30)
    app.processEvents()

    # 强制建轮盘
    w.toggle_wheel(True)
    app.processEvents()
    wheel = w.wheel
    assert wheel is not None and wheel.isVisible(), "轮盘没显示"
    print("轮盘: %dx%d  桌宠 %dx%d"
          % (wheel.width(), wheel.height(), w.width(), w.height()))

    # 顶层：表情动作 / 本体动作 两个入口（缩合进轮盘）
    top = [(k, n, l) for k, n, l in wheel.items]
    print("  顶层项: %s" % top)
    top_names = {n for k, n, l in top}
    assert top_names & {"__expr__", "__body__"}, \
        "轮盘顶层应含 表情动作/本体动作 入口，实际 %s" % top
    assert all(k == "submenu" for k, n, l in top), "顶层项都应是子菜单入口"

    # 进入「表情动作」子菜单（没有表情动作则退回本体动作）
    grp_id = "__expr__" if "__expr__" in top_names else "__body__"
    wheel._enter(grp_id)
    app.processEvents()
    assert wheel.menu_id == grp_id, "没进入子菜单"
    grp = "extra" if grp_id == "__expr__" else "main"
    expect = lib.by_group(grp)
    print("  %s: %d 个（%s 组）" % (
        "表情动作" if grp == "extra" else "本体动作", len(wheel.items), grp))
    assert len(wheel.items) == len(expect), \
        "子菜单项数(%d) != %s 组(%d)" % (len(wheel.items), grp, len(expect))
    assert all(k == "clip" for k, n, l in wheel.items), "子菜单项应是可播动画"

    # 命中测试：每个块的中心该命中自己，块与块之间不重叠；
    # 桌宠本体所在的中列不命中任何块（块分布在两侧，不挡桌宠）
    cx, cy = wheel._geom()
    bad = []
    page_items = wheel._page_items()
    for i in range(len(page_items)):
        r = wheel._block_rect(i)
        assert r is not None, "第%d块没有矩形" % i
        h = wheel._slice_at(r.center())
        if h != ("item", page_items[i][0]):
            bad.append("第%d块中心命中=%s" % (i, h))
        # 块必须完整落在窗体里
        if r.left() < 0 or r.top() < 0 or \
           r.right() > wheel.width() or r.bottom() > wheel.height():
            bad.append("第%d块出界 %s" % (i, r))
    # 中列（桌宠身上）必须无块覆盖
    mid_hit = wheel._slice_at(QtCore.QPointF(cx, cy))
    print("  命中: 块=%s  桌宠中心=%s"
          % ("✓" if not bad else "✗ " + "; ".join(bad), mid_hit))
    assert not bad, "块命中测试失败: %s" % bad
    assert mid_hit[0] is None, "桌宠中心不该被块覆盖（块要分布两侧）"

    # 左右分列：前一半在桌宠左侧，后一半在右侧
    half = (len(page_items) + 1) // 2
    if len(page_items) >= 2:
        assert wheel._block_rect(0).right() <= cx - wheel.pet.width() / 2.0, \
            "第0块没在桌宠左侧"
        assert wheel._block_rect(half).left() >= cx + wheel.pet.width() / 2.0, \
            "右列首块没在桌宠右侧"
    print("  分列: 左列 %d 块 / 右列 %d 块，均不压桌宠 ✓"
          % (half, len(page_items) - half))

    # 按像素确认「画出来的块」和「能点中的块」完全同一位置
    img = QtGui.QImage(wheel.width(), wheel.height(),
                       QtGui.QImage.Format_ARGB32)
    img.fill(QtCore.Qt.transparent)
    pp = QtGui.QPainter(img)
    pp.setRenderHint(QtGui.QPainter.Antialiasing)
    pp.setPen(QtCore.Qt.NoPen)
    for i in range(len(page_items)):
        rr = wheel._block_rect(i)
        pp.setBrush(QtGui.QColor((i + 1) * 28, 0, 0, 255))
        pp.drawRoundedRect(rr, 7, 7)
    pp.end()
    for i in range(len(page_items)):
        rr = wheel._block_rect(i)
        col = img.pixelColor(int(rr.center().x()), int(rr.center().y()))
        exp_r = (i + 1) * 28
        if col.alpha() < 200 or abs(col.red() - exp_r) > 12:
            bad.append("第%d块 采样色=%s 期望红≈%d" % (i, col.name(), exp_r))
    print("  绘制/命中一致性: %s"
          % ("✓ 块颜色与位置吻合" if not bad else "✗ " + "; ".join(bad)))
    assert not bad, "块画出来的位置与命中不一致: %s" % bad

    # 点第 0 块 → 播第 0 个动画
    wheel.current = None
    wheel.mousePressEvent(_FakeEv(wheel._block_rect(0).center()))
    app.processEvents()
    assert w.clip_player.clip is not None, "点击后 clip_player 没播"
    assert w.clip_player.clip.name == wheel.items[0][1], "播的不是第0项"
    print("  点击第0片 → 播放 %s (%s) ✓"
          % (w.clip_player.clip.name, lib.display_name(w.clip_player.clip.name)))

    # 翻页（按钮在顶部一行，用 _prect 拿位置）
    if wheel.pages > 1:
        p0 = wheel.page
        _rp, rn = wheel._prect()
        wheel.mousePressEvent(_FakeEv(rn.center()))
        app.processEvents()
        assert wheel.page == (p0 + 1) % wheel.pages, "下一页没生效"
        print("  翻页 %d → %d ✓（共 %d 页）" % (p0, wheel.page, wheel.pages))

    # 返回顶层
    wheel._back()
    app.processEvents()
    assert wheel.menu_id == "", "没返回顶层"

    # ---------------------------------------------------------- 5 叠加顺序
    from pet.petapp import RenderThread
    th = w.thread
    assert th.gaze is g and th.clip_player is w.clip_player
    # 模拟一帧的叠加逻辑（与 run() 内一致）
    anim = an.tick()
    for b, v in w.clip_player.tick(__import__("time").time()).items():
        anim.setdefault(b, {}).update(v)
    gg = g.tick()
    for b, v in g.to_anim(gg).items():
        anim.setdefault(b, {}).update(v)
    head_rot = anim.get(g.head, {}).get("rot")
    print("叠加后 头=%s" % head_rot)
    # 视线是最后一层，头的 rot 必须等于视线层给出的值
    gz = g.to_anim(gg)
    if g.head in gz:
        assert head_rot == gz[g.head]["rot"], "视线层被覆盖了"
    print("  → 视线层优先级最高 ✓")

    w.quit()
    print("\n全部通过")


class _FakeEv:
    def __init__(self, pos, button=None):
        self._p = pos
        self._b = button

    def position(self):
        return self._p

    def button(self):
        return self._b if self._b is not None else QtCore.Qt.LeftButton


if __name__ == "__main__":
    main()
