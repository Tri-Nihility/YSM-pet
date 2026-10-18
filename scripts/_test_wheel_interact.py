"""动画轮盘交互验收（真实 windows 平台，逐动作出图）。

验证矩形块轮盘的完整交互链：
    1. 未悬停 → hover = -1
    2. 悬停第 3 块 → hover 变成该项的全局索引
    3. 点击 → 该动画开始播放，块变"选中"色
    4. 再点同一块 → 取消播放，回默认待机
    5. 点翻页 → 第 0 块内容换成下一页首项
    6. 点击非块区域（桌宠身上/空白）→ 停止播放

产物：outputs/pet/wheel_step*.png，便于肉眼复核。
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from PySide6 import QtWidgets, QtCore                         # noqa: E402

OUT = os.path.join(ROOT, "outputs", "pet")


class _Ev:
    def __init__(self, pos):
        self._p = pos

    def position(self):
        return self._p

    def button(self):
        return QtCore.Qt.LeftButton


def shot(w, name):
    QtWidgets.QApplication.processEvents()
    for _ in range(3):
        QtWidgets.QApplication.processEvents()
    path = os.path.join(OUT, name)
    w.grab().save(path)
    print("  saved %s  %dx%d" % (name, w.width(), w.height()))


def main():
    os.makedirs(OUT, exist_ok=True)
    from pet import modelsource as MS
    from pet.gaze import GazeTracker
    from ysm.clips import ClipLibrary

    # 走 modelsource（与 run_pet.py 同一条路径），别用 `loader.load_all()`
    # —— 它默认读 data/decrypted/*.bin，那是 gitignore 掉的本机产物。
    src = sys.argv[1] if len(sys.argv) > 1 else None
    if not src:
        from pet import settings as _S
        src = _S.load().get("model")
    if not src:
        print("用法: python scripts/_test_wheel_interact.py "
              "<模型来源 .ysm/文件夹/压缩包>")
        return 2
    d = MS.load_source(src, progress=None)
    model = d["model"]

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from pet.fastrender import FastRenderer
    from pet.petapp import IdleAnimator, PetWindow

    fr = FastRenderer(model, d["tex"], hide_bones=d["hide"])
    an = IdleAnimator(model)
    g = GazeTracker(model)
    lib = ClipLibrary(model)
    w = PetWindow(fr, an, rig=None, gaze=g, clips=lib, scale=1.5, fps=30)
    app.processEvents()

    w.toggle_wheel(True)
    app.processEvents()
    wheel = w.wheel
    assert wheel is not None and wheel.isVisible()
    wheel.raise_()
    app.processEvents()

    print("轮盘顶层: %s" % [(k, n, l) for k, n, l in wheel.items])
    # 顶层是 表情动作/本体动作 入口，先进一个子菜单再交互
    top_names = {n for k, n, l in wheel.items}
    grp_id = "__expr__" if "__expr__" in top_names else "__body__"
    wheel._enter(grp_id)
    app.processEvents()
    print("轮盘 %dx%d  项=%d  页=%d" % (wheel.width(), wheel.height(),
                                        len(wheel.items), wheel.pages))

    # ---------------------------------------------------------- 1 悬停
    r0 = wheel._block_rect(0)
    assert r0 is not None
    wheel.mouseMoveEvent(_Ev(r0.center()))
    app.processEvents()
    gi0 = wheel._page_items()[0][0]
    print("1 悬停第0块 → hover=%d（期望 %d）" % (wheel.hover, gi0))
    assert wheel.hover == gi0, "hover 没跟着鼠标走"
    shot(wheel, "wheel_step1_hover.png")

    # ---------------------------------------------------------- 2 播放
    wheel.mousePressEvent(_Ev(r0.center()))
    app.processEvents()
    name0 = wheel._page_items()[0][2]
    assert w.clip_player.clip is not None, "点了没播"
    print("2 点击第0块 → 播放 %s（期望 %s）current=%s"
          % (w.clip_player.clip.name, name0, wheel.current))
    assert w.clip_player.clip.name == name0 and wheel.current == name0
    shot(wheel, "wheel_step2_playing.png")

    # ---------------------------------------------------------- 3 再点取消
    wheel.mousePressEvent(_Ev(r0.center()))
    app.processEvents()
    print("3 再点同一块 → current=%s clip=%s（期望都为空）"
          % (wheel.current, w.clip_player.clip))
    assert wheel.current is None and w.clip_player.clip is None, "没回到待机"
    shot(wheel, "wheel_step3_stopped.png")

    # ---------------------------------------------------------- 4 翻页（仅多页时）
    if wheel.pages > 1 and len(wheel._page_items()) > 1:
        first_before = wheel._page_items()[0][2]
        _rp, rn = wheel._prect()
        wheel.mousePressEvent(_Ev(rn.center()))
        app.processEvents()
        first_after = wheel._page_items()[0][2]
        print("4 翻页 → %d/%d，第0块 %s → %s"
              % (wheel.page + 1, wheel.pages, first_before, first_after))
        assert wheel.page == 1 and first_after != first_before, "翻页没换内容"
        shot(wheel, "wheel_step4_page2.png")
        wheel.mousePressEvent(_Ev(_rp.center()))      # 翻回第 1 页
        app.processEvents()

    # ---------------------------------------------------------- 5 点空白停播
    # 先点一个块进入播放态（多页已翻回第1页；单页直接点第0块）
    bsel = wheel._block_rect(1) if len(wheel._page_items()) > 1 else r0
    wheel.mousePressEvent(_Ev(bsel.center()))
    app.processEvents()
    assert w.clip_player.clip is not None, "准备阶段没播上"
    cx, cy = wheel._geom()
    wheel.mousePressEvent(_Ev(QtCore.QPointF(cx, cy)))   # 桌宠正中
    app.processEvents()
    print("5 点桌宠正中 → current=%s clip=%s（期望都为空）"
          % (wheel.current, w.clip_player.clip))
    assert wheel.current is None and w.clip_player.clip is None, "点空白没停"
    shot(wheel, "wheel_step5_blank_stop.png")

    # ---------------------------------------------------------- 6 出界检查
    bad = []
    for i in range(len(wheel._page_items())):
        r = wheel._block_rect(i)
        if r.left() < 0 or r.top() < 0 or \
           r.right() > wheel.width() or r.bottom() > wheel.height():
            bad.append(i)
    print("6 出界块: %s（期望空）" % (bad or "无"))
    assert not bad, "有块跑出窗体: %s" % bad

    # ---------------------------------------------------------- 7 关闭键（× 按钮）
    cr = wheel._close_rect()
    wheel.mousePressEvent(_Ev(cr.center()))
    app.processEvents()
    assert not wheel.isVisible(), "点 × 没关闭轮盘"
    print("7 点 × 关闭轮盘 ✓")
    shot(w, "wheel_step7_closed.png")

    w.quit()
    print("\n交互验收全部通过")


if __name__ == "__main__":
    main()
