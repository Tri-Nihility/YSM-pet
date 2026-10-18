"""设置页「当前模型」应显示真实模型名，而不是占位符「—」。

回归点：原 `_title_block` 从 `ClipLibrary.source` / `name` 取名字，但
`ClipLibrary` 这两个属性根本不存在 → 一路 None → 永远显示「—」。
正确来源是 `PetWindow.model_key`（模型路径）。
"""
import glob
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.join(os.getcwd(), "src"))

from PySide6 import QtWidgets                              # noqa: E402
from pet import petapp, modelsource, settings as S         # noqa: E402
from pet.fastrender import FastRenderer                    # noqa: E402
from ysm.clips import ClipLibrary                          # noqa: E402

SNAP = S.load()
src = SNAP.get("model")
if not src or not os.path.exists(src):
    cands = glob.glob(os.path.join(os.getcwd(), "临时文件*", "*.ysm"))
    if not cands:
        print("跳过：找不到可用 .ysm 模型")
        sys.exit(0)
    src = cands[0]

app = QtWidgets.QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)
data = modelsource.load_source(src)
fr = FastRenderer(data["model"], data["tex"], hide_bones=data["hide"])
clips = ClipLibrary(data["model"])
anim = petapp.IdleAnimator(data["model"])
w = petapp.PetWindow(fr, anim, clips=clips, model_key=os.path.abspath(src))
w.show()
app.processEvents()

FAIL = []

# 1) 模块级函数
assert petapp._model_label("") == "", "_model_label 空串"
assert petapp._model_label("/a/b/亚托莉（目标）.ysm") == "亚托莉（目标）", \
    "_model_label 应去目录和扩展名"
print("1) _model_label 函数 OK")

# 2) PetWindow.model_label 属性
expect = os.path.splitext(os.path.basename(src))[0]
print("   期望模型名 =", expect, "  实际 =", w.model_label)
if not w.model_label or w.model_label == expect:
    pass
assert w.model_label == expect, "model_label == 文件名去扩展名"
print("2) PetWindow.model_label =", w.model_label, " ✓")

# 3) 设置面板标题块里不应再是「—」
pan = petapp.SettingsPanel(w)
w.panel = pan
pan.show()
app.processEvents()
title_w = pan._title_block()
subs = [c.text() for c in title_w.findChildren(QtWidgets.QLabel)
        if c.objectName() == "subtitle"]
print("   面板副标题:", subs)
if not any("当前模型" in s for s in subs):
    FAIL.append("面板没有「当前模型 · ...」")
elif any(s.strip().endswith("· —") or s.strip().endswith("·") for s in subs):
    FAIL.append("面板仍显示占位符「—」")
elif not any(expect in s for s in subs):
    FAIL.append("面板没显示真实模型名 %r" % expect)
else:
    print("3) 设置面板「当前模型 · %s」✓（不再是占位符）" % expect)

pan.close()
w.thread.stop()
w.close()
S.save(SNAP)
print("\n结果:", "✓ 当前模型显示正常" if not FAIL else "✗ %s" % FAIL)
sys.exit(1 if FAIL else 0)
