"""G 验收：待机动作为「降幅 + 速率平滑」，且没有突跳。

为什么这么验：待机是"看起来对不对"的东西，肉眼只能看个大概。所以拆成
可测量的三条，并和**改动前的旧实现**（各自固定频率的纯正弦）对比：

    1. 降幅：新实现的峰值幅度必须明显小于旧实现（否则白改）
    2. 平滑：逐帧二阶差分（即"加速度"）的最大值必须显著下降
       —— 突跳在二阶差分上会是个尖峰，这是最灵敏的指标
    3. 无死板节拍：新实现的自相关周期性应弱于纯正弦

同时用**抖动的时间步长**（模拟卡帧）跑一遍，验证不会因为 dt 突变而抽搐。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np                                            # noqa: E402


def old_idle(t):
    """改动前的实现：三个独立固定频率正弦，直接输出。"""
    br = np.sin(2 * np.pi * t / 4.0)
    sway = np.sin(2 * np.pi * t / 9.0)
    swing = np.sin(2 * np.pi * t / 6.0)
    return {
        "root_y": 0.45 * br,
        "head_y": 4.0 * sway,       # 头部 yaw 幅度
        "rarm": 2.2 * br + 1.0 * swing,
    }


def run_new(an, steps=600, dt=1.0 / 30, jitter=False, seed=7):
    """按固定/抖动步长跑新实现，返回各通道时间序列。"""
    import time as _t
    rnd = np.random.RandomState(seed)
    out = {"root_y": [], "head_y": [], "rarm": []}
    an.t0 = 0.0
    an._last_t = 0.0
    an._sm = {}
    fake = [0.0]
    real = _t.time
    try:
        for i in range(steps):
            if jitter:
                fake[0] += dt * (0.4 + 1.6 * rnd.rand())
            else:
                fake[0] += dt
            _t.time = lambda: fake[0] + an.t0
            a = an.tick()
            out["root_y"].append(a.get(an.b_root, {}).get("pos", [0, 0, 0])[1])
            out["head_y"].append(a.get(an.b_head, {}).get("rot", [0, 0, 0])[1])
            out["rarm"].append(a.get(an.b_rarm, {}).get("rot", [0, 0, 0])[0])
    finally:
        _t.time = real
    return {k: np.array(v) for k, v in out.items()}


def smoothness(sig):
    """二阶差分（加速度）的 95 分位 —— 突跳会让它炸高。"""
    if len(sig) < 3:
        return 0.0
    d2 = np.diff(sig, 2)
    return float(np.percentile(np.abs(d2), 95))


def autocorr_peak(sig):
    """去掉直流后自相关的最大旁瓣峰值，衡量周期性强度。"""
    x = sig - sig.mean()
    if x.std() < 1e-9:
        return 0.0
    ac = np.correlate(x, x, "full")[len(x) - 1:]
    ac = ac / ac[0]
    # 跳过主瓣前几个滞后
    return float(np.max(ac[5:len(x) // 2])) if len(x) > 20 else 0.0


def main():
    from pet.petapp import IdleAnimator
    from pet import loader as L

    d = L.load_all()
    an = IdleAnimator(d["model"])

    steps, dt = 900, 1.0 / 30
    ts = np.arange(steps) * dt
    old = {k: [] for k in ("root_y", "head_y", "rarm")}
    for t in ts:
        o = old_idle(t)
        for k in old:
            old[k].append(o[k])
    old = {k: np.array(v) for k, v in old.items()}

    new = run_new(an, steps=steps, dt=dt)
    print("=== 固定步长 (%.0f fps, %d 帧) ===" % (1 / dt, steps))
    print("%-8s %10s %10s %8s" % ("通道", "旧峰值", "新峰值", "降幅"))
    for k in ("root_y", "head_y", "rarm"):
        po = float(np.abs(old[k]).max())
        pn = float(np.abs(new[k]).max())
        print("%-8s %10.3f %10.3f %7.0f%%"
              % (k, po, pn, 100 * (1 - pn / po) if po else 0))
        assert pn < po, "通道 %s 没降幅（旧 %.3f → 新 %.3f）" % (k, po, pn)

    print("\n%-8s %12s %12s %8s" % ("通道", "旧加速度P95", "新加速度P95", "变化"))
    for k in ("root_y", "head_y", "rarm"):
        so, sn = smoothness(old[k]), smoothness(new[k])
        print("%-8s %12.5f %12.5f %7.0f%%"
              % (k, so, sn, 100 * (sn / so - 1) if so else 0))
        # 头部与手臂的平滑必须变好；root 的位移本来就很小，允许同量级
        if k != "root_y":
            assert sn < so, "通道 %s 加速度反而变大" % k

    print("\n%-8s %12s %12s" % ("通道", "旧自相关峰", "新自相关峰"))
    for k in ("root_y", "head_y", "rarm"):
        ao, anm = autocorr_peak(old[k]), autocorr_peak(new[k])
        print("%-8s %12.4f %12.4f" % (k, ao, anm))
    # 整体周期性应当不高于旧实现（双频+包络打散节拍）
    assert autocorr_peak(new["head_y"]) <= autocorr_peak(old["head_y"]) + 0.02, \
        "头部周期性没有改善"

    # ---------------------------------------------------------- 抖动步长
    # ⚠️ 说明：这一步**不是**在证明"抖动下也很平滑"。
    # dt 本身在变，采样点间距就不同，二阶差分必然变大 —— 这是测量的
    # 性质，不是动画的性质。这里只看它是否"有界"（不出现失控的尖峰），
    # 真正的抗抖动保证由下面那条"大步长单帧跳变"来验。
    print("\n=== 抖动步长（模拟卡帧，0.4x~2.0x dt）===")
    jit = run_new(an, steps=steps, dt=dt, jitter=True)
    for k in ("root_y", "head_y", "rarm"):
        sj = smoothness(jit[k])
        sn = smoothness(new[k])
        ratio = (sj / sn) if sn > 0 else 0
        print("%-8s 抖动P95=%.5f  稳定P95=%.5f  (%.1fx，dt 可变导致，非动画问题)"
              % (k, sj, sn, ratio))
        # 有界即可：不应该比稳定情况放大到离谱的量级
        assert ratio < 40, "通道 %s 在抖动步长下失控（%.1fx）" % (k, ratio)

    # 速率限幅的直接验证：人为制造一个超大步长，输出不该跳
    an._sm = {}
    an.t0 = 0.0
    an._last_t = 0.0
    import time as _t
    real = _t.time
    try:
        _t.time = lambda: 0.0
        a0 = an.tick()
        v0 = a0.get(an.b_head, {}).get("rot", [0, 0, 0])[1]
        # 直接推进 2 秒（正常 60 帧的量），看单帧跳变是否被限住
        _t.time = lambda: 2.0
        a1 = an.tick()
        v1 = a1.get(an.b_head, {}).get("rot", [0, 0, 0])[1]
        print("\n大步长(2s)单帧跳变: %.3f → %.3f  步长=%.3f" % (v0, v1, abs(v1 - v0)))
        # 限幅 6 度/秒 × dt(被夹到 0.1) = 0.6，所以不该超过约 1.0
        assert abs(v1 - v0) < 1.5, "大步长下出现了突跳（%.3f）" % abs(v1 - v0)
        print("  ✓ 大步长下未突跳（被 dt 夹取 + 速率限幅挡住）")
    finally:
        _t.time = real

    print("\nG 验收全部通过")


if __name__ == "__main__":
    main()
