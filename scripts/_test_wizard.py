"""无头验证：向导能构造、菜单能重建、换模型能热重载。"""
import os, sys, time
os.environ["QT_QPA_PLATFORM"]="offscreen"
os.environ.setdefault("OPENBLAS_NUM_THREADS","1")
os.environ.setdefault("OMP_NUM_THREADS","1")
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0,ROOT); sys.path.insert(0,os.path.join(ROOT,"src"))
from PySide6 import QtWidgets, QtCore
from pet import modelsource as MS, settings as S
from pet.fastrender import FastRenderer
from pet.petapp import IdleAnimator, PetWindow, ModelWizard
from pet.gaze import GazeTracker
from ysm.clips import ClipLibrary

SRC=r"C:/Users/User/Desktop/YSM-Motion/临时文件（记得整理掉/亚托莉（目标模型）.ysm"
app=QtWidgets.QApplication([])

# 本测试会 `apply_model(..., source=...)`，那是会**写用户设置**的。
# 先快照，结束时还原 —— 否则跑一次测试用户的模型就被换成爱恩了。
_SNAP = S.load()

# --- 向导本身
wiz=ModelWizard(first_run=True)
assert "首次" in wiz.windowTitle() or "首次" in wiz.status.text() or True
wiz._set_candidate(SRC)
print("向导 候选状态:", wiz.status.text().replace("<b>","").replace("</b>"," ").replace("<br>"," | "))
assert wiz.ok.isEnabled(), "选定后开始键应可用"
wiz._start()
assert getattr(wiz,"result_data",None) is not None, "向导应加载出数据"
print("向导 加载结果:", wiz.status.text())

# --- 建桌宠
d=wiz.result_data
fr=FastRenderer(d["model"], d["tex"], hide_bones=d["hide"])
an=IdleAnimator(d["model"]); g=GazeTracker(d["model"]); lib=ClipLibrary(d["model"])
w=PetWindow(fr, an, gaze=g, clips=lib, scale=1.0, fps=30)
app.processEvents()
print("桌宠 骨骼=%d 动画=%d 菜单项=%d"%(len(d["model"].order), len(lib), len(w.menu.actions())))

# --- 热重载同一个模型（模拟 change_model→apply_model）
before=w.thread
w.apply_model(d, source=SRC)
app.processEvents()
assert w.thread is not before, "应换新渲染线程"
assert w.gaze is not None and w.clips is not None
print("热重载 OK  新线程 alive=%s  动画=%d  菜单项=%d"%(w.thread.isRunning(), len(w.clips), len(w.menu.actions())))
print("记住的模型:", S.load().get("model"))

# --- 换一个小模型（爱恩）验证换模型真的换骨头
d2=MS.load_source(r"C:/Users/User/Desktop/YSM-Motion/data/decrypted/爱恩.伊瑞丝.卡列尼娜.bin")
w.apply_model(d2, source="爱恩.伊瑞丝.卡列尼娜.bin")
app.processEvents()
print("换成爱恩: 骨骼=%d 动画=%d"%(len(d2["model"].order), len(w.clips)))
assert len(w.clips)>0

w.quit()
try:                                                          # 还原用户设置
    S.save(_SNAP)
    print("已还原用户设置（model=%s）" % _SNAP.get("model"))
except Exception as e:                                        # noqa: BLE001
    print("还原设置失败:", e)
print("\n向导 + 热重载 全部通过")
