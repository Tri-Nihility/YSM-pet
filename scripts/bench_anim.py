"""A3 决策实验：动画采样到底该优化哪里。

背景
----
`ysmmodel`（建骨骼树 + 取面）在当前热路径上**已被 FastRenderer 接管**：
它在 __init__ 里一次性建好骨骼索引/父子表，每帧自己做向量化 FK + 批量
变换，`YsmModel.pose()/faces()` 只剩参考实现，不在渲染循环里。

profile 实测（120 帧，亚托莉 56 个动画）：
    animation.py:_vec          0.329s    ← 3 分量收敛，最热
    animation.py:_keyframes    0.154s    ← 每帧重排 + 重建，纯浪费
    animation.py:sample_channel 0.941s cumtime  ← 占整帧 48%
    fastrender.pose_matrices   0.398s

所以 A3 真正该做的是**预解析通道**，而不是把 ysmmodel 搬进 C。

本脚本量化：预解析后每帧采样能快多少。
"""
import os
import sys
import time

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


def prep(channel):
    """把通道预解析成 (ts ndarray, vs ndarray[N,3], modes list, constant?)。

    与 _keyframes 语义完全一致，只是**只做一次**。
    """
    ks = A._keyframes(channel)
    if not ks:
        return None
    ts = np.array([k[0] for k in ks], float)
    vs = np.array([k[1] for k in ks], float)
    modes = [str(k[2]).lower() == "catmullrom" for k in ks]
    return ts, vs, modes


def sample_prepped(P, t, length=0.0):
    """与 A.sample_channel 逐条对齐的快速版。"""
    if P is None:
        return None
    ts, vs, cat = P
    n = len(ts)
    if n == 1:
        return vs[0].copy()
    if ts[-1] > 0 and length > 0 and t > ts[-1]:
        t = t % max(length, 1e-6)
    if t <= ts[0]:
        return vs[0].copy()
    if t >= ts[-1]:
        return vs[-1].copy()
    # 上界二分
    i = int(np.searchsorted(ts, t, side="right")) - 1
    if i < 0:
        i = 0
    if i > n - 2:
        i = n - 2
    t0, t1 = ts[i], ts[i + 1]
    u = (t - t0) / max(t1 - t0, 1e-9)
    v0, v1 = vs[i], vs[i + 1]
    if cat[i]:
        # 直接复用官方 _catmull，避免手抄公式出错
        p0 = vs[max(i - 1, 0)]
        p3 = vs[min(i + 2, n - 1)]
        return A._catmull(p0, v0, v1, p3, u)
    return v0 + (v1 - v0) * u


def main():
    path = find_model()
    if not path:
        print("没找到 .ysm")
        return 2
    from pet import modelsource as MS
    from ysm.clips import ClipLibrary

    d = MS.load_source(path)
    model = d["model"]
    lib = ClipLibrary(model)
    name = lib.names()[0]
    clip = lib.get(name)
    print("模型: %s" % os.path.basename(path))
    print("动画: %s  骨骼=%d" % (name, len(clip.bones)))

    # 收集该 clip 的全部通道
    chans = []
    for bone, ch in clip.bones.items():
        for k in ("rotation", "position", "scale"):
            if k in ch:
                chans.append(ch[k])
    print("通道数: %d" % len(chans))

    NT = 300

    # --- 基线：现实现（每帧重新 _keyframes） ---
    for i in range(20):
        clip.sample(i * 0.01)
    t0 = time.perf_counter()
    for i in range(NT):
        clip.sample(i * 0.01)
    t1 = time.perf_counter()
    base_ms = (t1 - t0) / NT * 1000

    # --- 优化：预解析一次 ---
    prepped = [(c, prep(c)) for c in chans]
    for i in range(20):
        for _c, P in prepped:
            sample_prepped(P, i * 0.01, clip.length)
    t0 = time.perf_counter()
    for i in range(NT):
        for _c, P in prepped:
            sample_prepped(P, i * 0.01, clip.length)
    t1 = time.perf_counter()
    fast_ms = (t1 - t0) / NT * 1000

    print("\n每帧采样耗时：")
    print("  现实现（每帧 _keyframes） : %.3f ms" % base_ms)
    print("  预解析版                 : %.3f ms" % fast_ms)
    print("  加速                     : %.1fx" % (base_ms / max(fast_ms, 1e-9)))

    # --- 逐字段校验：预解析版必须与现实现完全一致 ---
    bad = 0
    worst = 0.0
    for i in range(200):
        t = i * 0.013
        for c, P in prepped:
            a = A.sample_channel(c, t, clip.length)[0]
            b = sample_prepped(P, t, clip.length)
            if a is None and b is None:
                continue
            if (a is None) != (b is None):
                bad += 1
                continue
            dd = float(np.max(np.abs(np.asarray(a, float) - np.asarray(b, float))))
            worst = max(worst, dd)
            if dd > 1e-12:
                bad += 1
    print("\n逐字段校验: 不一致 %d 处，最大差 %.3e" % (bad, worst))
    if bad:
        print("  ✗ 预解析版与现实现不一致")
        return 1
    print("  ✓ 预解析版与现实现逐字段相同")
    return 0


if __name__ == "__main__":
    sys.exit(main())
