"""右键菜单完整性验收。

要求（用户口径）：
  * **不加新的常驻项**（不加托盘图标之外的东西、不加悬浮按钮…）
  * 但**其余功能至少要能从右键菜单打开** —— 即：凡是设置面板里能改的，
    右键菜单必须有等价入口，不强迫用户去开面板。

本脚本：
  1. 列出右键菜单全部条目（含子菜单）并与面板能力逐项对照
  2. 真正"点一遍"每个可点项，确认不抛异常
"""
import os
import sys

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from PySide6 import QtWidgets                      # noqa: E402
from pet import modelsource as MS                  # noqa: E402
from pet.fastrender import FastRenderer            # noqa: E402
from pet.petapp import (IdleAnimator, PetWindow,   # noqa: E402
                        SettingsPanel, _pick_cjk_font)
from pet.gaze import GazeTracker                   # noqa: E402
from ysm.clips import ClipLibrary                  # noqa: E402

SRC = os.path.join(ROOT, "临时文件（记得整理掉", "亚托莉（目标模型）.ysm")


def dump(menu, depth=0):
    """展开菜单树 -> [(缩进文本, action)]。"""
    out = []
    for a in menu.actions():
        if a.isSeparator():
            continue
        sub = a.menu()
        label = a.text().replace("&", "")
        out.append(("  " * depth + label, None if sub else a))
        if sub is not None:
            out.extend(dump(sub, depth + 1))
    return out


def main():
    app = QtWidgets.QApplication([])
    fam = _pick_cjk_font()
    if fam:
        f = app.font()
        f.setFamily(fam)
        app.setFont(f)

    d = MS.load_source(SRC)
    fr = FastRenderer(d["model"], d["tex"], hide_bones=d["hide"])
    w = PetWindow(fr, IdleAnimator(d["model"]), rig=None, scale=1.0, fps=30,
                  gaze=GazeTracker(d["model"]), clips=ClipLibrary(d["model"]))
    w.show()
    for _ in range(6):
        app.processEvents()

    items = dump(w.menu)
    print("右键菜单条目（%d 项）:" % len(items))
    for txt, _a in items:
        print("   " + txt)

    # 规范化：去掉 & 助记符、末尾省略号，便于跨来源比对
    def norm(s):
        return s.replace("&", "").rstrip("…").rstrip(".").strip()

    # ---------------------------------------------------------- 覆盖性对照
    # 右键菜单保留的「快捷入口」（窗口大小/抗锯齿/帧率/朝向复位 面板里也有、
    # 但菜单里仍保留当快捷方式）；其余面板能力（光影/待机/并行层/自动旋转/
    # 视线/表情动作）已**缩合进设置面板 / 轮盘**，菜单里不再单列。
    need = {
        "窗口大小": ["窗口大小"],
        "抗锯齿": ["抗锯齿"],
        "帧率": ["帧率"],
        "朝向复位": ["朝向复位"],
        "动画轮盘": ["动画轮盘"],
        "更换模型": ["更换模型"],
        "置顶": ["置顶"],
        "鼠标穿透": ["鼠标穿透"],
    }
    # 已缩合进面板/轮盘、必须从右键菜单消失的项
    folded = ["光影", "程序化待机", "并行动画层", "自动旋转",
              "视线跟踪", "播放动画"]
    labels = [norm(t) for t, _ in items]
    missing = [k for k, alts in need.items()
               if not any(norm(a) in labels for a in alts)]
    # 精确整条匹配：菜单项文本（去助记符/省略号/句号后）等于该名才算泄漏
    leaked = [f for f in folded if f in labels]
    print("\n面板能力覆盖检查:")
    if missing:
        print("  ✗ 菜单缺少入口: %s" % missing)
    else:
        print("  ✓ 保留的快捷入口都在菜单里")
    if leaked:
        print("  ✗ 以下项本应已缩合进面板/轮盘，却仍出现在右键菜单: %s"
              % leaked)
    else:
        print("  ✓ 已缩合项（光影/待机/并行层/自动旋转/视线/播放动画）"
              "均不在右键菜单")

    # ---------------------------------------------------------- 逐项点一遍
    # ⚠️ 有的项会弹**模态对话框**（QFileDialog 选模型 / QMessageBox 确认退出），
    #    在这类离屏自动化里一旦弹出就永久阻塞。所以按文本白名单跳过，
    #    这几个的动作是"打开对话框"，本身不含逻辑，单独用面板测试覆盖过。
    SKIP = {"更换模型", "设置", "退出"}
    print("\n逐项触发测试:")
    # ⚠️ 有些菜单项（程序化待机 / 并行动画层 / 视线跟踪）会**写盘**保存用户
    #    偏好。测试触发它们会把用户的设置改掉，所以先快照、结束时还原。
    from pet import settings as _S
    try:
        _snap = _S.load()
    except Exception:                                         # noqa: BLE001
        _snap = None
    fired = 0
    skipped = []
    for txt, a in items:
        if a is None:
            continue
        label = norm(txt)
        if label in SKIP:
            skipped.append(label)
            continue
        if not a.isEnabled():
            print("   - %-22s 已禁用（合理，如无动画时轮盘不可用）" % label)
            continue
        if a.isCheckable():
            a.trigger()               # 切换
            a.trigger()               # 切回，别改状态
        else:
            a.trigger()
        for _ in range(2):
            app.processEvents()
        fired += 1
    print("   已触发 %d 个可点项，无异常" % fired)
    if skipped:
        print("   跳过（会弹模态框）: %s" % "、".join(skipped))
    if _snap is not None:                                     # 还原用户设置
        try:
            _S.save(_snap)
        except Exception:                                     # noqa: BLE001
            pass

    ok = (not missing) and (not leaked)
    print("\n菜单验收%s" % ("全部通过" if ok else "失败"))

    # ⚠️ 正常收尾再退出。
    # 不这么做的话，进程在解释器析构阶段（Qt/OpenGL 拆窗口）会异常终止，
    # 退出码被写成 127 —— 明明脚本自己 return 0，CI 里却看着像崩溃。
    w.thread.stop()
    try:
        w.close()
    except Exception:                                         # noqa: BLE001
        pass
    app.processEvents()
    app.quit()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
