# -*- coding: utf-8 -*-
"""验证比例缓存「写→读」往返一致：
  1) 用新量法实测一次 → 得到 ratio 与居中后的 pctr（基准 + dx）
  2) 新建渲染器，走缓存路径 → ratio 必须相等，pctr 必须与实测结果一致
     （回归用户报的「第二次启动人物不在正中」—— 根因是 _ratio_base_pctr
      只在实测分支被赋值，缓存命中时是 None，dx/dy 永远应用不上）
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np                                  # noqa: E402

from pet import settings as S                        # noqa: E402
from pet import modelsource as MS                    # noqa: E402
from pet.fastrender import FastRenderer, _ratio_cache_path  # noqa: E402
from ysm.clips import ClipLibrary, ResidentPlayer    # noqa: E402


def main():
    cfg = S.load()
    src = sys.argv[1] if len(sys.argv) > 1 else cfg.get("model")
    data = MS.load_source(src, progress=None)
    model, tex, hide = data["model"], data["tex"], data["hide"]
    clips = ClipLibrary(model)
    res = ResidentPlayer(clips)

    key = "TESTKEY::" + src
    p = _ratio_cache_path()
    # 先把测试键清掉（覆写为空 JSON，不用 safe-delete）
    import json
    if p and os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        d.pop(str(key), None)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)

    # ---- 1) 实测 ----
    fr_a = FastRenderer(model, tex, hide_bones=hide)
    r_a = fr_a.measure_ratio(None, clips, cache_key=key, resident=res)
    pctr_a = np.array(fr_a.pctr, float)
    print("[实测] ratio=%.5f  pctr=%s" % (r_a, pctr_a))

    # ---- 2) 新建渲染器走缓存 ----
    fr_b = FastRenderer(model, tex, hide_bones=hide)
    r_b = fr_b.measure_ratio(None, clips, cache_key=key, resident=res)
    pctr_b = np.array(fr_b.pctr, float)
    print("[缓存] ratio=%.5f  pctr=%s" % (r_b, pctr_b))

    ok = True
    if abs(r_a - r_b) > 1e-9:
        print("  ✗ 比例不一致：%.6f vs %.6f" % (r_a, r_b))
        ok = False
    if not np.allclose(pctr_a, pctr_b, atol=1e-6):
        print("  ✗ 居中偏移没复现：%s vs %s" % (pctr_a, pctr_b))
        ok = False

    # 清理测试键
    if p and os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        d.pop(str(key), None)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)

    print("结果:", "通过 ✓" if ok else "失败 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
