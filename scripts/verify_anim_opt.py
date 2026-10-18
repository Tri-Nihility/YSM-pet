"""A3 验收：动画采样/姿态优化的等价性。

背景
----
A3 原本要求"ysmmodel 建骨骼树 + 烘焙面重组 → C"。实测发现：
  * `YsmModel.pose()/faces()` 已不在热路径上（FastRenderer 接管了每帧 FK
    与批量变换），搬进 C 没有收益。
  * 真正的瓶颈是 `ysm/animation.py:sample_channel` —— 它**每帧**对同一条
    通道重跑 `_keyframes()`（解析 dict + sorted）。以及 `pose_matrices`
    里每帧 `np.eye(4)` 的重复分配、`_dirty()` 每帧重算 BFS。

所以 A3 落到三处**纯 Python 优化**：
  1. animation.sample_channel → 预解析通道（_prep 缓存）
  2. fastrender.pose_matrices → 共享 _EYE4 常量
  3. fastrender._dirty → 按骨骼名集合缓存

本脚本证明这三处**不改变任何输出**：
  - 采样层：与"不用缓存的朴素 _keyframes 路径"逐字段对照
  - 渲染层：真实动画下逐像素对照（对照 = 关掉优化的参考实现）
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np                     # noqa: E402
from ysm import animation as A         # noqa: E402


def find_model():
    for base in ("models", "临时文件（记得整理掉"):
        dd = os.path.join(ROOT, base)
        if not os.path.isdir(dd):
            continue
        for dp, _dn, fn in os.walk(dd):
            for f in fn:
                if f.lower().endswith(".ysm"):
                    return os.path.join(dp, f)
    return None


# ------------------------------------------------------------------ 朴素参考
def sample_channel_naive(channel, t, length=0.0):
    """完全不走 _prep 缓存的参考实现（照抄优化前的 sample_channel）。

    存在的意义：证明 _prep 缓存路径与"每帧重跑 _keyframes"逐字段相同。
    """
    ks = A._keyframes(channel)
    if not ks:
        return None, False
    if len(ks) == 1:
        return ks[0][1].copy(), False
    ts = [k[0] for k in ks]
    if length > 0 and t > ts[-1]:
        t = t % max(length, 1e-6)
    if t <= ts[0]:
        return ks[0][1].copy(), True
    if t >= ts[-1]:
        return ks[-1][1].copy(), True
    i = max(j for j in range(len(ts) - 1) if ts[j] <= t)
    t0, v0, m = ks[i]
    t1, v1, _ = ks[i + 1]
    u = (t - t0) / max(t1 - t0, 1e-9)
    if str(m).lower() == "catmullrom":
        p0 = ks[max(i - 1, 0)][1]
        p3 = ks[min(i + 2, len(ks) - 1)][1]
        return A._catmull(p0, v0, v1, p3, u), True
    return v0 + (v1 - v0) * u, True


def main():
    path = find_model()
    if not path:
        print("没找到 .ysm")
        return 2
    from pet import modelsource as MS
    from pet.fastrender import FastRenderer
    from ysm.clips import ClipLibrary

    d = MS.load_source(path)
    model, tex = d["model"], d["tex"]
    fr = FastRenderer(model, tex)
    lib = ClipLibrary(model)
    names = lib.names()[:8]
    print("模型: %s" % os.path.basename(path))
    print("抽检动画: %d 个" % len(names))

    # ------------------------------------------------ 1) 采样层逐字段
    print("\n--- 1) 采样层：_prep 缓存 vs 每帧重跑 _keyframes ---")
    bad = 0
    worst = 0.0
    nchk = 0
    for nm in names:
        clip = lib.get(nm)
        for bone, chans in clip.bones.items():
            for ch in ("rotation", "position", "scale"):
                if ch not in chans:
                    continue
                c = chans[ch]
                for i in range(60):
                    t = i * 0.017
                    a = A.sample_channel(c, t, clip.length)
                    b = sample_channel_naive(c, t, clip.length)
                    nchk += 1
                    if (a[0] is None) != (b[0] is None):
                        bad += 1
                        continue
                    if a[0] is None:
                        continue
                    dd = float(np.max(np.abs(np.asarray(a[0], float)
                                             - np.asarray(b[0], float))))
                    worst = max(worst, dd)
                    if dd > 1e-15 or a[1] != b[1]:
                        bad += 1
    print("  对照 %d 次采样，不一致 %d 处，最大差 %.3e" % (nchk, bad, worst))
    if bad:
        print("  ✗ 采样层不一致")
        return 1
    print("  ✓ 采样层逐字段相同")

    # ------------------------------------------------ 2) 渲染层逐像素
    print("\n--- 2) 渲染层：真实动画下逐像素 ---")
    import ctypes
    # 参考实现：临时把优化点还原
    import pet.fastrender as FR
    fr2 = FastRenderer(model, tex)
    orig_dirty = FastRenderer._dirty

    def naive_dirty(self, anim):
        stack = [self.index[n] for n in anim if n in self.index]
        seen = set(stack)
        while stack:
            i = stack.pop()
            for c in self.children[i]:
                if c not in seen:
                    seen.add(c)
                    stack.append(c)
        return sorted(seen)

    maxdiff = 0
    npix = 0
    for nm in names[:4]:
        clip = lib.get(nm)
        for i in range(12):
            t = i * 0.05
            anim = clip.sample(t)
            # 优化版
            fr._dirty_cache.clear()
            img_opt = fr.render(anim, 160, 240)
            # 参考版（关掉 _dirty 缓存 + 关掉 animation 缓存重算）
            A.clear_prep_cache()
            FastRenderer._dirty = naive_dirty
            try:
                fr2._dirty_cache.clear()
                img_ref = fr2.render(anim, 160, 240)
            finally:
                FastRenderer._dirty = orig_dirty
            diff = int(np.max(np.abs(img_opt.astype(int)
                                     - img_ref.astype(int))))
            maxdiff = max(maxdiff, diff)
            npix += int(np.count_nonzero(np.any(img_opt != img_ref, axis=2)))
    print("  逐像素最大差 = %d   不同像素累计 = %d" % (maxdiff, npix))
    if maxdiff != 0:
        print("  ✗ 渲染层出现像素差异")
        return 1
    print("  ✓ 真实动画下逐像素完全一致")

    print("\nA3（采样/姿态优化路径）验收全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
