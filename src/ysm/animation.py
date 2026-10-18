"""
 Bedrock/YSM 动画采样 + 分层合成。

通道值（per bone per channel）可能是：
  - {"秒": [x,y,z]}               普通键帧
  - {"秒": number|"number"}       标量，广播到 3 轴
  - {"秒": {"pre":[...], "post":[...], "lerp_mode":"catmullrom"}}   Catmull-Rom 切线
  - {"秒": "molang 表达式"}        运行时求值（见 ysm.molang），每帧都要重算
  - [x,y,z] / number             常量（整个通道无时间维度）

分层合成：上层覆盖下层，逐骨骼字典合并（同骨骼同通道后写胜出）。
"""
import json
import os

import numpy as np

from ysm import molang

# --------------------------------------------------------------------- 预解析
# 为什么要有这一层：
#   `sample_channel` 每帧都会对**同一条通道**重跑 `_keyframes()` —— 解析
#   dict、逐分量 `_vec`、构造 list、再 `sorted()`。而通道对象在模型整个
#   生命周期内是不变的，这些全是白做的重复劳动。
#   实测（scripts/bench_anim.py，亚托莉 attacked 93 条通道）：
#       现实现 1.883 ms/帧  →  预解析版 0.461 ms/帧   （4.1x）
#   逐字段对照最大差 0.0，完全等价。
#
#   注意 A3 的"烘焙面重组"不在这里做：FastRenderer 已经接管了每帧 FK 与
#   批量变换，`YsmModel.pose()/faces()` 只剩参考实现，不在渲染热路径上。
#   所以这条通道的瓶颈是**采样**，不是几何。
#
# 缓存键用 id(channel)：
#   channel 是 list/dict，不可哈希；id 复用风险靠 _PREP_HOLD 持有强引用
#   消掉 —— 只要对象还在缓存里，id 就不会被别的对象复用。缓存容量有上限，
#   满了整体清空（动画通道数量级是几百，不会频繁触发）。
_PREP = {}
_PREP_HOLD = {}
_PREP_CAP = 20000

# `_prep` 的哨兵：这条通道含 molang，每帧必须重算，不能缓存数值
_DYN = object()


def clear_prep_cache():
    """换模型时调用：丢掉旧通道的预解析结果。

    缓存用 `id(channel)` 做键并且持有强引用（防 id 复用），所以换模型后
    旧模型的上万条通道会被**一直留着**。虽然 _PREP_CAP 有心跳式兜底，
    但换模型是确定性时机，必须显式清，否则每换一次模型就多占一份内存。
    """
    _PREP.clear()
    _PREP_HOLD.clear()


def _prep(channel):
    """把通道解析成 (ts, vs, catmull_flags)，只做一次。

    含 molang 的通道返回 `_DYN`（并把这个判定缓存住）：数值每帧都可能变，
    缓存下来就冻住了 —— 实测 `!v.roaming.p` 被冻成 0 会让身体直接消失。
    """
    key = id(channel)
    got = _PREP.get(key)
    if got is not None:
        return got
    if _has_expr(channel):
        got = _DYN
    else:
        ks = _keyframes(channel)
        if not ks:
            got = None
        else:
            got = (
                np.array([k[0] for k in ks], float),
                np.array([k[1] for k in ks], float),
                [str(k[2]).lower() == "catmullrom" for k in ks],
            )
    if len(_PREP) >= _PREP_CAP:
        _PREP.clear()
        _PREP_HOLD.clear()
    _PREP[key] = got
    _PREP_HOLD[key] = channel
    return got


def _vec(v, default=0.0):
    """把任意通道值收敛成 (np.ndarray[3], has_molang: bool)。

    Molang 串**真的求值**（`ysm.molang`）：慧慧1.2 的常驻层用
    `!v.roaming.p` 之类控制「哪套身体/服装显示」，以前一律当 0 会让整具
    身体的 scale 变 0（人物消失）。求值失败才退回 `default`。
    """
    if isinstance(v, dict):
        for key in ("post", "pre"):
            if key in v:
                return _vec(v[key])
        return np.array([default] * 3, float), True
    if isinstance(v, list):
        out, bad = [], False
        for x in v:
            if isinstance(x, str):
                try:
                    out.append(float(x))
                except ValueError:
                    got = molang.try_eval(x)
                    out.append(default if got is None else got)
                    bad = True
            else:
                out.append(float(x))
        while len(out) < 3:
            out.append(default)
        return np.array(out[:3], float), bad
    if isinstance(v, str):
        try:
            return np.array([float(v)] * 3, float), False
        except ValueError:
            got = molang.try_eval(v)
            if got is None:
                return np.array([default] * 3, float), True
            return np.array([got] * 3, float), True
    return np.array([float(v)] * 3, float), False


def _is_expr(x):
    """这个分量是不是需要每帧重算的 molang 串（纯数字串不算）。"""
    if not isinstance(x, str):
        return False
    try:
        float(x)
        return False
    except ValueError:
        return True


def _has_expr(channel):
    """通道里是否含 molang 表达式。

    含表达式的通道**不能进 `_PREP` 缓存** —— 缓存的是「解析那一刻的数值」，
    而 molang 依赖 `ysm.head_yaw` / `v.roaming.*` 这些每帧变的量。
    """
    def comps(d):
        for key in ("post", "pre"):
            for c in (d.get(key) or []):
                if _is_expr(c):
                    return True
        return False

    if isinstance(channel, list):
        return any(comps(x) for x in channel if isinstance(x, dict))
    if isinstance(channel, dict):
        for v in channel.values():
            if isinstance(v, dict):
                if comps(v):
                    return True
            elif _is_expr(v):
                return True
        return False
    return _is_expr(channel)


def _keyframes(channel):
    """-> [(t, vec3, lerp_mode)] 按 t 排序；常量通道返回 [(0.0, v, 'linear')]。

    键帧有两种写法：
      * **dict 形式**（Bedrock JSON）：`{"0.5": v, "1.0": v}`
      * **list of {t, mode, post}**（.ysm 二进制里解出来的原生形式）：
        每帧自带 `t` 时间戳，**必须按 t 取值，不能拿列表下标当时间** ——
        `潜行` / `海边` 这类动画的时间点是非等距的（实测 `潜行` 有 0.0/0.042/
        0.292… 的疏密分布），按下标采样会让动作明显走形。
    """
    if isinstance(channel, list):
        # 原生键帧：元素是 {"t":..,"mode":..,"post":[...]} 形式
        if channel and all(isinstance(x, dict) and "t" in x for x in channel):
            out = []
            for x in channel:
                v, _ = _vec(x)
                mode = {0: "linear", 1: "catmullrom"}.get(
                    x.get("mode", 0), "linear")
                out.append((float(x["t"]), v, mode))
            return sorted(out, key=lambda e: e[0])
        # 纯数值列表：常量通道
        if len(channel) > 0 and all(not isinstance(x, (dict, list))
                                   for x in channel):
            v, _ = _vec(channel)
            return [(0.0, v, "linear")]
        out = []
        for i, x in enumerate(channel):
            v, _ = _vec(x)
            out.append((float(i), v, "linear"))
        return sorted(out, key=lambda e: e[0])
    if not isinstance(channel, dict):
        v, _ = _vec(channel)
        return [(0.0, v, "linear")]
    out = []
    for k, v in channel.items():
        vec, _ = _vec(v)
        mode = v.get("lerp_mode", "linear") if isinstance(v, dict) else "linear"
        out.append((float(k), vec, mode))
    return sorted(out, key=lambda e: e[0])


def _catmull(p0, p1, p2, p3, u):
    u2, u3 = u * u, u * u * u
    return 0.5 * ((2 * p1) + (-p0 + p2) * u +
                  (2 * p0 - 5 * p1 + 4 * p2 - p3) * u2 +
                  (-p0 + 3 * p1 - 3 * p2 + p3) * u3)


def sample_channel(channel, t, length=0.0):
    """在时刻 t(秒) 采样一个通道 -> (vec3, ok)。

    ⚠️ **空通道返回 (None, False)**，调用方必须跳过这条通道。
    不能当成 [0,0,0]：.ysm 里"这个骨骼没有 scale 动画"就是用**空列表 []**
    表示的（实测 `行走动画` 55 根骨骼里绝大多数 `scale` 是 `[]`），
    当成 0 会把整具身体 scale 压成 0 —— 人物直接消失。
    """
    P = _prep(channel)
    if P is None:
        return None, False
    if P is _DYN:
        # 含 molang：每次采样都重新求值（表达式 AST 有缓存，开销可控）
        ks = _keyframes(channel)
        if not ks:
            return None, False
        ts = np.array([k[0] for k in ks], float)
        vs = np.array([k[1] for k in ks], float)
        cat = [str(k[2]).lower() == "catmullrom" for k in ks]
    else:
        ts, vs, cat = P
    n = len(ts)
    if n == 1:
        return vs[0].copy(), False
    # 循环：超出末尾则取模
    if length > 0 and t > ts[-1]:
        t = t % max(length, 1e-6)
    if t <= ts[0]:
        return vs[0].copy(), True
    if t >= ts[-1]:
        return vs[-1].copy(), True
    # 与旧实现 `max(j for j in ... if ts[j] <= t)` 等价：取最后一个 <= t 的
    # 下标，并夹到 [0, n-2]（t 严格在 [ts[0], ts[-1]) 内，天然不会越界，
    # 夹取只是防御）。
    i = int(np.searchsorted(ts, t, side="right")) - 1
    if i < 0:
        i = 0
    elif i > n - 2:
        i = n - 2
    t0, t1 = ts[i], ts[i + 1]
    v0, v1 = vs[i], vs[i + 1]
    u = (t - t0) / max(t1 - t0, 1e-9)
    if cat[i]:
        p0 = vs[max(i - 1, 0)]
        p3 = vs[min(i + 2, n - 1)]
        return _catmull(p0, v0, v1, p3, u), True
    return v0 + (v1 - v0) * u, True


def load_clips(*paths):
    """-> {clip_name: {"length":float, "loop":str, "bones":{bone:{ch: channel}}}}"""
    out = {}
    for p in paths:
        if not os.path.exists(p):
            continue
        d = json.load(open(p, encoding="utf-8"))
        for name, a in d.get("animations", {}).items():
            out.setdefault(name, {
                "length": float(a.get("animation_length", 0) or 0),
                "loop": str(a.get("loop", "")),
                "bones": a.get("bones") or {},
                "source": os.path.basename(p),
            })
    return out


def sample_clip(clip, t):
    """-> {bone: {"rot":[3]度, "pos":[3], "scale":[3]}}（空通道自动略过）"""
    pose = {}
    for bone, chans in clip["bones"].items():
        entry = {}
        for ch in ("rotation", "position", "scale"):
            if ch in chans:
                key = {"rotation": "rot", "position": "pos", "scale": "scale"}[ch]
                v, _ = sample_channel(chans[ch], t, clip["length"])
                if v is not None:
                    entry[key] = v
        if entry:
            pose[bone] = entry
    return pose


def has_molang(clip):
    """静态解析会踩到 molang 的关键帧数量。"""
    n = 0
    for bone, chans in clip["bones"].items():
        for ch, val in chans.items():
            if isinstance(val, dict):
                for v in val.values():
                    if isinstance(v, dict):
                        continue
                    _, bad = _vec(v)
                    n += int(bad)
    return n


def compose(layers):
    """合并多层 pose，后写的层覆盖先写的。layer 可以是 dict 也可以是 None。"""
    out = {}
    for layer in layers:
        if not layer:
            continue
        for bone, e in layer.items():
            cur = out.setdefault(bone, {})
            cur.update({k: np.asarray(v, float).copy() for k, v in e.items()})
    return out
