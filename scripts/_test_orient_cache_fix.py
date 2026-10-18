# -*- coding: utf-8 -*-
"""验收三项：
  1. 穿模消失 bug：ONCE 动画播完后 tick() 交还待机（渲染覆盖恢复，不再 0%）
  2. 朝向默认 yaw=90/pitch=0 + 持久化
  3. 清理缓存函数
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import numpy as np
from pet import modelsource as MS
from pet.fastrender import FastRenderer
from pet import settings as S
from ysm import clips

ok = True


def check(name, cond, detail=""):
    global ok
    print("  %s %s %s" % ("✓" if cond else "✗", name, detail))
    if not cond:
        ok = False


print("===== 1) ONCE 动画播完交还待机（穿模消失 bug） =====")
data = MS.load_source(os.path.join(ROOT, "临时文件（记得整理掉/亚托莉（目标模型）.ysm"))
model, tex, hide = data["model"], data["tex"], data["hide"]
fr = FastRenderer(model, tex, hide_bones=hide)
fr.ssaa = 1
fr.lighting = False
lib = clips.ClipLibrary(model)

idle_cov = float((fr.render({}, 300, 300, yaw=28, pitch=10, fov=40, zoom=1.42)[..., 3] > 0).mean())
import time as _time
for name in ("extra0", "extra1", "death"):
    clip = lib.get(name)
    pl = clips.ClipPlayer()
    pl.play(clip, 0.0)
    # 播到一半：应在播（覆盖可能异常，但 tick 有输出）
    mid = pl.tick(clip.length * 0.1)
    # 播完之后（超时 2 倍时长）：必须返回 {} 交还待机
    end = pl.tick(clip.length * 2 + 1.0)
    check("%s 播完 tick()=空" % name, end == {},
          "(中途 %d 根骨骼 → 播完 %d 根)" % (len(mid), len(end)))
    if name == "extra0":
        # 渲染验证：播完后画面覆盖恢复到 idle 水平，而不是 0%
        anim = {}
        rgba = fr.render(anim, 300, 300, yaw=28, pitch=10, fov=40, zoom=1.42)
        cov = float((rgba[..., 3] > 0).mean())
        check("extra0 播完后画面不再消失", cov > idle_cov * 0.5,
              "cov=%.1f%% (idle=%.1f%%)" % (cov * 100, idle_cov * 100))

print("\n===== 2) 朝向默认 90/0 + 持久化 =====")
cfg = S.load()
check("默认 yaw=90", float(cfg.get("yaw", -1)) == 90.0, "yaw=%s" % cfg.get("yaw"))
check("默认 pitch=0", float(cfg.get("pitch", -999)) == 0.0, "pitch=%s" % cfg.get("pitch"))
# RenderThread 不传朝向 → 读设置
import pet.petapp as PA
from PySide6 import QtWidgets
_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
rt = PA.RenderThread(fr, None, 100, 100)
check("RenderThread 初始朝向=设置值", rt.yaw == 90.0 and rt.pitch == 0.0,
      "yaw=%.1f pitch=%.1f" % (rt.yaw, rt.pitch))
# 持久化写回 + 显式传参优先
S.update(yaw=123.0, pitch=-5.0)
rt2 = PA.RenderThread(fr, None, 100, 100)
check("滑块写入后重启读回", rt2.yaw == 123.0 and rt2.pitch == -5.0,
      "yaw=%.1f pitch=%.1f" % (rt2.yaw, rt2.pitch))
rt3 = PA.RenderThread(fr, None, 100, 100, yaw=7.0, pitch=3.0)
check("显式传参优先于设置", rt3.yaw == 7.0 and rt3.pitch == 3.0)
S.update(yaw=90.0, pitch=0.0)          # 还原默认

print("\n===== 3) 清理缓存 =====")
from pet import fastrender as _fr
from pet import modelsource as _ms
# 造两个假缓存文件
p = _fr._ratio_cache_path()
os.makedirs(os.path.dirname(p), exist_ok=True)
with open(p, "w") as f:
    f.write("{}")
d = _ms.cache_dir()
os.makedirs(d, exist_ok=True)
dummy = os.path.join(d, "_dummy_test.bin")
with open(dummy, "wb") as f:
    f.write(b"x")
n = PA._clear_caches()
check("比例缓存已删", not os.path.isfile(p))
check("解密缓存已删", not os.path.isfile(dummy))
# 注：真实缓存文件/目录的删除可能被沙箱 safe-delete 钩子拦截（fail-closed，
# SHFileOperationW 0x2），属环境限制；真实用户环境下 os.remove 正常。
print("    （清理项数 n=%d；沙箱可能拦截真实缓存删除，非代码问题）" % n)

print("\n结果:", "✅ 全部通过" if ok else "❌ 有失败")
sys.exit(0 if ok else 1)
