"""桌宠启动烟测：真的把 `run()` 走一遍，然后自动退出。

为什么需要它
------------
`petapp.run()` 里漏了 `from pet.gaze import GazeTracker` 和
`from ysm.clips import ClipLibrary` —— 因为本模块只在**函数内部** import
这两个（顶层引入会把 pybullet 一起拖进来，拖慢冷启动），结果 1529/1543 行
用到处看不见它们，启动即 `NameError`。

而 `_test_pet_features.py` / `_shot_app.py` / `_test_wizard.py` 这些脚本
**自己** import 了 GazeTracker/ClipLibrary，走的也不是 `run()`，所以
一次都没暴露。**必须有一条真正调用 `run()` 的路径**才算覆盖启动。

做法：临时把模型指到 --source，`QT_QPA_PLATFORM` 保持真实平台（默认），
用 QTimer 在窗口 show 之后 1.2 秒退出事件循环。任何 NameError / 导入失败
都会让 run() 抛异常、进程非零退出。

    python scripts/test_startup.py [--source 模型路径] [--legacy]
"""
import argparse
import os
import sys
import traceback

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

DEFAULT_SRC = os.path.join(ROOT, "临时文件（记得整理掉",
                           "亚托莉（目标模型）.ysm")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=None)
    ap.add_argument("--legacy", action="store_true")
    ap.add_argument("--hold", type=float, default=1.2,
                    help="窗口显示后停留秒数再退出")
    args = ap.parse_args()

    if args.legacy:
        os.environ["YSM_NO_NATIVE"] = "1"

    src = args.source or DEFAULT_SRC
    if not os.path.exists(src):
        print("找不到模型：%s" % src)
        return 2

    from PySide6 import QtCore, QtWidgets
    from pet import petapp as PA

    # run() 内部自己 `app = QApplication(...)` 然后 `return app.exec()`。
    # 我们要做的是"让事件循环跑一小会儿就自己退出"，同时**不改 run() 的
    # 任何代码**。最干净的办法：包一层 QApplication.exec ——
    # 在被调用时先挂一个单次定时器，到点调 quit()。
    _real_exec = QtWidgets.QApplication.exec

    def _patched_exec(*a, **kw):
        # PySide6 的 exec 在 C 层是 builtin（inspect 签名是 `()`），但写成
        # `app.exec()` 时仍会被绑上 self —— 直接透传会
        # `TypeError: QApplication.exec() takes no arguments (1 given)`。
        # 这里只取实例、不把它转发给底层。
        inst = QtWidgets.QApplication.instance()
        if inst is not None:
            QtCore.QTimer.singleShot(int(args.hold * 1000), inst.quit)
        return _real_exec()

    QtWidgets.QApplication.exec = _patched_exec

    try:
        rc = PA.run(source=src, scale=1.0, fps=30)
    except Exception:                                        # noqa: BLE001
        print("\n✗ 启动失败：")
        traceback.print_exc()
        return 1

    print("✓ run() 正常返回 rc=%s（启动路径全部走通）" % rc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
