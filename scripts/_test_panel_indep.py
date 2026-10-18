import os, sys
sys.path.insert(0, os.path.join(os.getcwd(), "src"))
from PySide6 import QtWidgets, QtCore
from pet import petapp, modelsource
from pet.fastrender import FastRenderer
from ysm.clips import ClipLibrary

from pet import settings as S
src = S.load().get("model")
if not src or not os.path.exists(src):
    # 设置里没模型（或被向导测试改过）→ 退回仓库里的示例模型
    import glob
    cands = glob.glob(os.path.join(os.getcwd(), "临时文件*", "*.ysm"))
    if not cands:
        print("跳过：找不到可用的 .ysm 模型")
        sys.exit(0)
    src = cands[0]
S.update(model=src)
app = QtWidgets.QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)          # 与 run() 一致
data = modelsource.load_source(src)
fr = FastRenderer(data["model"], data["tex"], hide_bones=data["hide"])
clips = ClipLibrary(data["model"])
anim = petapp.IdleAnimator(data["model"])
w = petapp.PetWindow(fr, anim, clips=clips, model_key=os.path.abspath(src))
w.show()
app.processEvents()

print("1) 桌宠可见:", w.isVisible())
print("   面板指针:", w.panel)

p = petapp.SettingsPanel(w)
w.panel = p
p.show()
app.processEvents()
print("2) 打开面板 → 桌宠可见:", w.isVisible(), " 面板可见:", p.isVisible())
print("   面板 flags 带 Qt.Window:", bool(p.windowFlags() & QtCore.Qt.Window))
print("   是独立顶层窗口:", p.isWindow())

p.close()                                     # 模拟点 ×
app.processEvents()
app.processEvents()
print("3) 关闭面板 → 面板可见:", p.isVisible(), " 桌宠可见:", w.isVisible())
print("   桌宠 panel 引用已清:", w.panel is None)
print("   桌宠线程仍在跑:", w.thread.isRunning())
ok = w.isVisible() and w.thread.isRunning() and w.panel is None
print("\n结果:", "✓ 关面板不影响桌宠" if ok else "✗ 桌宠被连带关闭")
w.thread.stop(); w.close()
sys.exit(0 if ok else 1)
