"""快速渲染器：向量化 FK + 批量变换 + C++ 光栅化。

慢在哪（实测，200x300、3228 个四边形）
--------------------------------------
* `model.faces()` 逐面 numpy 小矩阵运算         ~0.21 s
* `render_faces()` 逐面调 draw_tri（6456 三角） ~0.80 s
合计 ~1 s/帧，实时无望。

本模块的做法
------------
1. **rest 世界矩阵预计算**：rest 时 local = T(piv)·T(-piv) = I，
   所以未参与动画的骨骼 `M[n] == M[parent]`。每帧只重算「动画骨骼 + 其子孙」，
   通常只有 30~80 根，而不是 329 根。
2. **批量变换**：所有面的顶点/法线用一次 `np.matmul` 批处理，不再逐面循环。
3. **C++ 光栅化**：`native/raster.dll`（ctypes），算法与 `ysm/render.py` 严格一致。
4. **透明背景**：输出 RGBA，未覆盖处 alpha=0，直接给桌宠的透明窗口用。

输出：uint8 (H, W, 4) RGBA（非预乘）。
"""
import ctypes
import os

import numpy as np

from phys.bind import P_MAT

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DLL = os.path.join(ROOT, "native", "raster.dll")

# 光照（pybullet 空间：+X = 角色前方, +Y = 左, +Z = 上）
# 旧光向 [0.45,-0.60,0.66] 是「上方偏后」，导致脸发暗、且过半的面被 clamp 到同一个
# 最暗值 → 明暗呈二值跳变。现改为：主光从「上 + 前 + 左」打来（脸亮、顶面亮），
# 再叠半球环境光（按法线朝上程度插值），背光面落到 0.60 而不是死黑。
_LIGHT = np.array([0.50, 0.40, 0.77], float)
_LIGHT /= np.linalg.norm(_LIGHT)

# shade = AMB + HEMI_W * (0.5 + 0.5*n_up) + DIFF_W * max(dot(n, L), 0)
# 取值区间 [0.60, 1.00]：下 0.60 / 后·右 0.71 / 左 0.78 / 前 0.80 / 上 0.96
_AMB, _HEMI_W, _DIFF_W = 0.60, 0.22, 0.18


# ---------------------------------------------------------------- 比例缓存
# `measure_ratio` 要遍历**全部动画**（几十个 × 4 帧），实测 2~20 s。
# 但模型不变时结果恒定，所以按「模型指纹」落盘：下次启动直接读，秒开。
# 存的是 JSON（人类可读，出问题能一眼看出），放用户缓存目录而不是仓库里。
#
# ⚠️ `_RATIO_CACHE_VER` 必须随**量法**变化而 +1，否则用户机器上会一直用
#    旧量法算出来的比例（"缓存有问题"）。
#      v2：改为「所有动作渲染后的全局包围盒」，并且**把 pre_parallel /
#          parallel 常驻层也计入** —— 头发飘出去的那一下才是最宽的时刻，
#          不算进去窗口就会裁到飘起来的头发。
#      v3：两个修正 ——
#          ① 探针改用**更大的 zoom**（相机拉远），保证任何动作都不出画。
#             之前用 zoom=1.42 时，超宽动作会被钳到画幅上，方幅 → 比例被
#             钳成 ≈1.0 → **量出来的是假比例**（用户："缓存的数据有问题"）。
#          ② 居中校正**不再平移 pctr**。pctr 是相机绕转的枢轴，
#             把它的水平分量推离模型纵轴 → 改「朝向」时模型会绕着那根
#             外来的竖轴甩（用户："以莫名其妙的外纵轴旋转"）。
#      v4：取景改用「核心顶点团」（`_frame_core`）—— 远端特效面不再把
#          包围球撑大。量法与 v3 不同，旧缓存必须失效，否则窗口尺寸
#          仍然按旧量法的结果来（慧慧1.2 就会继续是个窄长方形空白窗）。
#      v5：相机距离改用「模型高度的一半」（`self.frame_r`）而不是包围球
#          半径 —— 球半径把张开的手臂/头发也算进去，人物只占窗高 ~25%，
#          与 zoom「人物占窗高 ≈ 1/zoom」的本意不符。出画判定随之变化，
#          故 v4 缓存也要作废。
#      v6：常驻并行层（pre_parallel / parallel）改为**默认关闭**（桌宠没有
#          游戏内 molang 变量，这些层算出来是「隐藏身体」；慧慧1.2 只剩一
#          个头）。量比例时 `resident.available` 随开关变化，量出来的宽高比
#          会差一倍，故 v5 缓存必须作废。
#      v7：“清晰模式”（只画人物本体）改为**默认开** —— hide 集合变了，
#          模型包围盒随之变小（慧慧1.2 少了 42% 的面 / 法阵与立绘），
#          量出来的宽高比不同，故 v6 缓存必须作废。
_RATIO_CACHE_VER = 7


def _ratio_cache_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "YSMPet")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return None
    return os.path.join(d, "ratio_cache.json")


def _load_ratio_cache(key):
    """按 key 取缓存，返回 (ratio, dx, dy) 或 None。

    dx/dy 是「相对 pctr_base 的偏移」—— 见 `_ratio_base_pctr`，因为
    pctr 本身每换一个模型就重算，不能把绝对值写进缓存。
    """
    if not key:
        return None
    p = _ratio_cache_path()
    if not p or not os.path.exists(p):
        return None
    try:
        import json
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        e = data.get(str(key))
        if not e:
            return None
        # 量法版本不一致 → 当作没缓存（强制按新量法重测）
        if int(e.get("v", 0)) != _RATIO_CACHE_VER:
            return None
        return (float(e["ratio"]), float(e.get("dx", 0.0)),
                float(e.get("dy", 0.0)))
    except (OSError, ValueError, KeyError, TypeError):
        return None                                  # 缓存坏了就当没有


def _save_ratio_cache(key, ratio, dx=0.0, dy=0.0):
    """把量好的比例写进缓存（失败静默 —— 缓存只是加速，不该影响启动）。"""
    if not key:
        return
    p = _ratio_cache_path()
    if not p:
        return
    try:
        import json
        data = {}
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    data = json.load(fh) or {}
            except (OSError, ValueError):
                data = {}
        data[str(key)] = {"v": _RATIO_CACHE_VER,
                          "ratio": float(ratio),
                          "dx": float(dx), "dy": float(dy)}
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, p)                           # 原子写，防半截文件
    except (OSError, ValueError, TypeError):
        pass

# 只读的 4x4 单位阵。`pose_matrices` 每帧要给所有「未参与动画」的骨骼
# 取 local=I，原来写 `np.eye(4)` 会**每根骨骼每帧分配一个新数组**
# （profile 实测 numpy.eye 73200 次/200 帧）。这里换成共享常量：
# 它只参与 `Mcur[par] @ local` 的右乘，不会被就地修改。
_EYE4 = np.eye(4)
_EYE4.flags.writeable = False


# ------------------------------------------------------------------ 4x4 工具
def _T(t):
    m = np.eye(4)
    m[:3, 3] = t
    return m


def _euler_rot(rx, ry, rz):
    """与上游一致：R = Rz(rz)·Ry(-ry)·Rx(-rx)，角度为弧度。"""
    cx, sx = np.cos(-rx), np.sin(-rx)
    cy, sy = np.cos(-ry), np.sin(-ry)
    cz, sz = np.cos(rz), np.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    m = np.eye(4)
    m[:3, :3] = Rz @ Ry @ Rx
    return m


def _frame_core(vis_v, gap=3.0, trim=2.5):
    """取景用的「核心顶点团」：去掉明显离群的远端特效面。

    很多模型会把刀光/法阵这类特效面（`ysmGlow_*` / `BeiJing*` /
    `ExtraTeXiao*`）放在离身体很远的地方，它们平时看不见，却会把包围球
    撑大好几倍 —— 相机随之后退，人物缩成几个像素，表现就是
    **窗口在、画面上什么都没有**（慧慧1.2：radius 21.1 vs 身体 1.8×2.5）。

    判据：以顶点中位数为心算距离 d；若 `p99 > gap × p75`（确实存在一坨
    远端顶点）就只保留 `d <= trim × p75`。健康模型（p99 ≈ 2~3×p75）不动。

    返回 `(取景顶点, 被裁掉的比例)`。
    """
    n = len(vis_v)
    if n == 0:
        return vis_v, 0.0
    med = np.median(vis_v, axis=0)
    d = np.linalg.norm(vis_v - med, axis=1)
    q75, q99 = (float(x) for x in np.percentile(d, [75, 99]))
    if q75 > 1e-6 and q99 > gap * q75:
        keep = d <= trim * q75
        if int(keep.sum()) >= max(64, int(0.3 * n)):
            return vis_v[keep], float(1.0 - keep.mean())
    return vis_v, 0.0


class FastRenderer:
    """model: ysmmodel.YsmModel；tex: uint8 (th, tw, 4)。"""

    def __init__(self, model, tex, hide_bones=(), dll_path=DLL):
        self.model = model
        self.tex = np.ascontiguousarray(np.asarray(tex, np.uint8))
        self.th, self.tw = self.tex.shape[0], self.tex.shape[1]
        self.hide = set(hide_bones or ())
        self.dll = self._load_dll(dll_path)

        # ---- 骨骼索引 / 拓扑
        self.names = [b["name"] for b in model.order]
        self.index = {n: i for i, n in enumerate(self.names)}
        nb = len(self.names)
        self.parent = np.full(nb, -1, np.int32)
        self.children = [[] for _ in range(nb)]
        for i, b in enumerate(model.order):
            p = b.get("parent") or ""
            if p and p in self.index:
                pi = self.index[p]
                self.parent[i] = pi
                self.children[pi].append(i)
        self._dirty_cache = {}
        # 上游 translate(pivotX - animTx, pivotY + animTy, pivotZ + animTz)
        self.pivot = np.array([np.asarray(b["pivot"], float) / 16.0
                               for b in model.order], float)

        # ---- rest 世界矩阵（未动画时直接用）
        # rest 时 local 恒为单位阵，所以 M0[i] == M0[parent]，根为 I。
        self.M0 = np.zeros((nb, 4, 4), float)
        for i in range(nb):
            p = self.parent[i]
            if p >= 0:
                self.M0[i] = self.M0[p]
            else:
                self.M0[i] = _EYE4
        self.Mcur = self.M0.copy()

        # ---- 预抽取所有面（局部坐标）
        fv, fu, fnl, fb = [], [], [], []
        for b in model.order:
            n = b["name"]
            if not b["cubes"] or n in self.hide:
                continue
            bi = self.index[n]
            for c in b["cubes"]:
                for f in c["faces"]:
                    fv.append(np.asarray(f["pos"], float))
                    fu.append(np.asarray(f["uv"], float) * (self.tw, self.th))
                    fnl.append(np.asarray(f["normal"], float))
                    fb.append(bi)
        self.fl = np.asarray(fv, float)                       # (Nf,4,3)
        self.fuv = np.asarray(fu, float)                      # (Nf,4,2) 像素
        self.fn = np.asarray(fnl, float)                      # (Nf,3)
        self.fb = np.asarray(fb, np.int32)                    # (Nf,)
        self.nf = len(self.fb)
        # 齐次顶点（预拼好，省掉每帧 concat）
        self.fh = np.concatenate([self.fl, np.ones((self.nf, 4, 1))], -1)

        # ---- 取景：只看可见骨骼，避免背景板撑大包围球
        #      ⚠️ 还要防「远端特效团」：慧慧1.2 有 ~12% 的顶点
        #      （ysmGlow_dg1_a8x 刀光面）落在 z≈-27 处，离身体十几倍远，
        #      直接拿全体顶点算包围球会把 radius 从 ~2 撑到 21 →
        #      相机后退 10 倍 → 人物只占窗口 1%（症状：窗口在、没内容）。
        #      见 `_frame_core`。
        vis_v = self.fl.reshape(-1, 3)
        core_v, self.frame_trim = _frame_core(vis_v)
        self.ctr = (core_v.max(0) + core_v.min(0)) / 2
        self.radius = float(np.linalg.norm(core_v - self.ctr, axis=1).max())
        # 相机目标：顶点已按 self.ctr 居中，再转到 pb 空间后的包围盒中心。
        # 用它做目标可以让模型在画面里水平居中（否则 yaw 旋转后会偏向一侧）。
        lo = (core_v - self.ctr) @ P_MAT.T
        self.pctr = (lo.max(0) + lo.min(0)) / 2
        # 取景半径用「模型高度的一半」，不是「离中心最远的顶点距离」：
        # 后者把横向的张角（张开的手臂、头发、裙摆、法阵）也算进半径，
        # 相机于是比需要的远 2~3 倍 —— 人物只占窗高 ~25%，而 zoom 的本意
        # 是「人物占窗高 ≈ 1/zoom」（1.42 → ~70%，见 self.zoom 注释）。
        # pb 空间 Z 就是屏幕竖直（P_MAT: pb_z = my），故取 lo[:, 2] 的半跨度；
        # 横向不会溢出：窗口宽度已经按同一个渲染宽高比 ×FIG_W_PAD 收紧。
        self.frame_r = float((lo[:, 2].max() - lo[:, 2].min()) / 2.0)
        if self.frame_r < 1e-3:                       # 退化模型兜底
            self.frame_r = self.radius

        # ---- 模型自身的包围盒（用于"按模型算窗口尺寸"）。
        #      注意：这不是世界包围盒的 3 个轴，而是**pb 空间**（相机空间）
        #      里的横向/纵向跨度 —— 相机是斜 45° 看的，直接用世界 X/Y/Z
        #      宽度会算歪。pb_x 是屏幕水平、pb_y 是屏幕垂直（见 raster.cpp
        #      的注释：pb_x=-mz, pb_y=-mx, pb_z=my）。
        self.bbox_lo = lo.min(0)
        self.bbox_hi = lo.max(0)
        self.screen_w = float(self.bbox_hi[0] - self.bbox_lo[0])   # 屏幕横向跨度
        self.screen_h = float(self.bbox_hi[1] - self.bbox_lo[1])   # 屏幕纵向跨度
        # 窗口宽高比：人物横向跨度 / 纵向跨度（>0，异常时退回 2:3）
        self.aspect = (self.screen_w / self.screen_h
                       if self.screen_h > 1e-6 else 2.0 / 3.0)
        # 真实渲染长宽比（懒测量，见 measure_ratio）
        self._ratio = None
        # measure_ratio 校正 pctr 前的**基准值**。缓存里存的是相对它的
        # 偏移量，所以必须留一份原值。
        #
        # ⚠️ 必须在这里就初始化成「静态 pctr」而不是留 None：缓存命中时
        #    measure_ratio 直接 return，**根本不会走到重新赋基准那一步**，
        #    留 None 就会导致缓存里的 dx/dy 永远应用不上 —— 表现为
        #    "第二次启动人物不在窗口正中"（首启准、次启偏）。
        self._ratio_base_pctr = np.array(self.pctr, float)

        # ---- 平涂开关：False 时不做任何明暗，直接输出贴图原色
        self.lighting = False

        # ---- 全管线 C++ 上下文（懒建）。True 时顶点变换/剔除/投影/
        #      光栅/下采样全在 C++ 内完成，中间结果不再回传 Python。
        self.use_cpp_pipeline = True
        self._ctx = None
        self._tex_ref = None        # 必须留住贴图，C++ 只借用指针

        # ---- 抗锯齿（SSAA）：>1 时以 ssaa 倍分辨率渲染再下采样。
        #      桌宠是透明背景，最刺眼的是**轮廓**锯齿，而 SSAA 下采样会把
        #      alpha 从二值 0/255 变成软边，正好解决这一点；顺带也平滑了
        #      最近邻纹理采样的内部锯齿。代价是渲染像素数 ×ssaa²。
        self.ssaa = 2

        # ---- 取景远近（相机后退倍率，越大越小只）
        #      由 settings 的 "zoom" 覆盖；外部（RenderThread）不传 zoom 时用它。
        #      1.38 与 pet.settings 的默认值保持一致（人物占窗高约 73%）。
        self.zoom = 1.42

        # ---- 输出缓冲：按 key 分开缓存，这样 SSAA 的大缓冲和目标
        #      缓冲可以并存，不会互相挤掉导致每帧重新分配。
        self._bufs = {}

    # ------------------------------------------------------------ dll
    @staticmethod
    def _load_dll(path):
        if not os.path.exists(path):
            return None
        # 尊重统一的 native 开关（`YSM_NO_NATIVE=1` / `--legacy`）。
        # 原来这里直接 ctypes.CDLL 加载 raster.dll，**完全绕过了 nativeload**，
        # 结果 `--legacy` 只退回了 Python 解析器，光栅化仍在跑 C++ ——
        # 与 A5 承诺的"出问题时能退回 Python"不符（实测 `C++光栅=是` 就是这个）。
        from ysm import nativeload
        off = nativeload.disabled_modules()
        if "raster" in off or "*" in off:
            return None
        try:
            d = ctypes.CDLL(path)
            d.raster_faces.restype = None
            d.downsample_rgba.restype = None
            d.downsample_rgba.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte),    # BIG
                ctypes.c_int, ctypes.c_int,        # W H（目标尺寸）
                ctypes.c_int,                      # SS
                ctypes.POINTER(ctypes.c_ubyte),    # OUT
            ]
            d.ysm_new.restype = ctypes.c_void_p
            d.ysm_new.argtypes = [
                ctypes.c_int,                      # nf
                ctypes.POINTER(ctypes.c_int),      # fbone
                ctypes.POINTER(ctypes.c_double),   # fvtx
                ctypes.POINTER(ctypes.c_double),   # fuv
                ctypes.POINTER(ctypes.c_double),   # fnrm
                ctypes.POINTER(ctypes.c_double),   # ctr
                ctypes.POINTER(ctypes.c_ubyte),    # tex
                ctypes.c_int, ctypes.c_int]        # tw th
            d.ysm_free.restype = None
            d.ysm_free.argtypes = [ctypes.c_void_p]
            # 校验用：取回上次 ysm_render 实际写入光栅器的三角形数据
            d.ysm_dump_last.restype = ctypes.c_int
            d.ysm_dump_last.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_double),   # P (ntri,9)
                ctypes.POINTER(ctypes.c_double),   # U (ntri,6)
                ctypes.POINTER(ctypes.c_double)]   # S (ntri,)
            d.ysm_render.restype = None
            d.ysm_render.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_double),   # M (nb,16)
                ctypes.POINTER(ctypes.c_ubyte),    # vis (nb,)
                ctypes.POINTER(ctypes.c_double),   # view 16
                ctypes.POINTER(ctypes.c_double),   # proj 16
                ctypes.c_int, ctypes.c_int, ctypes.c_int,   # W H ss
                ctypes.c_int,                      # lighting
                ctypes.POINTER(ctypes.c_double),   # light 3
                ctypes.c_double, ctypes.c_double, ctypes.c_double,  # amb hemiW diffW
                ctypes.POINTER(ctypes.c_ubyte)]    # out
            d.raster_faces.argtypes = [
                ctypes.POINTER(ctypes.c_double),   # P
                ctypes.POINTER(ctypes.c_double),   # UV
                ctypes.POINTER(ctypes.c_double),   # SH
                ctypes.c_int,                      # N
                ctypes.POINTER(ctypes.c_ubyte),    # TEX
                ctypes.c_int, ctypes.c_int,        # TW TH
                ctypes.POINTER(ctypes.c_double),   # ZB
                ctypes.POINTER(ctypes.c_ubyte),    # OUT
                ctypes.c_int, ctypes.c_int,        # W H
            ]
            return d
        except OSError:
            return None

    # ------------------------------------------------------------ 姿态
    def _dirty(self, anim):
        """动画骨骼 + 全部子孙（**按骨骼名集合缓存，返回已排序 list**）。

        同一个 clip 的动画骨骼集合在整段播放里是固定的，但原来每帧都要
        BFS 一遍子孙再 `sorted()`。实测这是 pure-Python 开销里靠前的一项。
        缓存键用 frozenset(动画骨骼名)，命中率接近 100%（同一个 clip 每帧
        传入的键完全相同）。
        """
        key = frozenset(anim)
        got = self._dirty_cache.get(key)
        if got is None:
            stack = [self.index[n] for n in anim if n in self.index]
            seen = set(stack)
            while stack:
                i = stack.pop()
                for c in self.children[i]:
                    if c not in seen:
                        seen.add(c)
                        stack.append(c)
            got = sorted(seen)
            self._dirty_cache[key] = got
        return got

    def pose_matrices(self, anim):
        """返回 (Nb,4,4) 世界矩阵 + (Nb,) 可见布尔。anim 可为空。"""
        Mcur = self.Mcur
        Mcur[:] = self.M0
        scale0 = {}
        for n, a in (anim or {}).items():
            if n not in self.index:
                continue
            i = self.index[n]
            if a.get("scale") is not None and np.allclose(a["scale"], 0.0):
                scale0[i] = True
        if not anim:
            return Mcur, np.ones(len(self.names), bool)

        for i in self._dirty(anim):
            n = self.names[i]
            a = anim.get(n)
            piv = self.pivot[i]
            if a:
                rot = np.asarray(a.get("rot", [0, 0, 0]), float)
                pos = np.asarray(a.get("pos", [0, 0, 0]), float)
                sc = np.asarray(a.get("scale", [1, 1, 1]), float)
                p = piv + np.array([-pos[0], pos[1], pos[2]], float) / 16.0
                local = (_T(p) @ _euler_rot(*np.radians(rot))
                         @ np.diag([sc[0], sc[1], sc[2], 1.0]) @ _T(-piv))
            else:
                local = _EYE4
            par = self.parent[i]
            Mcur[i] = Mcur[par] @ local if par >= 0 else local

        # 可见性：自身或祖先被 scale=0 则隐藏（上游 NativeModelRenderer 语义）
        vis = np.ones(len(self.names), bool)
        for i in range(len(self.names)):
            if i in scale0:
                vis[i] = False
            p = self.parent[i]
            if p >= 0 and not vis[p]:
                vis[i] = False
        return Mcur, vis

    # ------------------------------------------------------------ 投影
    def project_point(self, world, W, H, yaw, pitch, fov=40.0):
        """把模型空间世界坐标 (x,y,z) 投影到窗口像素 (sx, sy)。

        与 `_render_raw` / `_render_cpp` 渲染出图用的是**同一套** view /
        projection 矩阵（`computeViewMatrixFromYawPitchRoll` + `pctr` 作相机
        目标，顶点先减 `ctr` 再 `@ P_MAT.T`），所以这里算出来的屏幕位置
        和真实画出来的脸/眼**逐像素一致**。视线锚点（见 petapp 的
        `_gaze_anchor_for`）就是用它把眼骨骼投影到窗口像素，让"光标对准脸"
        时头正好朝正前方，而不是锚在窗口几何中心（≈胸口）。
        """
        import pybullet as p
        Vw = np.asarray(world, float)
        Vp = (Vw - self.ctr) @ P_MAT.T
        dist = self.frame_r / np.tan(np.radians(fov / 2.0)) * self.zoom
        vm = np.array(p.computeViewMatrixFromYawPitchRoll(
            tuple(self.pctr), dist, yaw, pitch, 0,
            upAxisIndex=2), float).reshape(4, 4).T
        pm = np.array(p.computeProjectionMatrixFOV(
            fov, float(W) / float(H), 0.05, 100), float).reshape(4, 4).T
        Mv = pm @ vm
        Vq = np.concatenate([Vp, [1.0]])
        clip = Vq @ Mv.T
        ndc = clip[:3] / clip[3]
        sx = (ndc[0] * 0.5 + 0.5) * W
        sy = (1.0 - (ndc[1] * 0.5 + 0.5)) * H
        return float(sx), float(sy)

    def screen_pos_of_bone(self, bone_name, W, H, yaw, pitch, fov=40.0):
        """某骨骼在**静止姿势**下的可见位置投影到的窗口像素。

        关键：锚点必须投影骨骼**可见 cube 的几何质心**，而不是它的 pivot
        （rest 世界矩阵平移 `M0[idx][:3,3]`）。YSM 里 pivot 是动画旋转轴，
        常落在模型原点（脚底）——例如亚托莉 AllHead/Head/Eyes 的 pivot 都在
        脚底（屏幕 y≈252），但 Head 的 cube 质心在脸（屏幕 y≈84）。若直接投影
        pivot，视线中心会锚在胸口而不是脸，于是"光标对准脸"时头向上/偏着转，
        看起来像"绑在了头颅左上角"。

        这里取该骨骼全部 cube 顶点的模型空间坐标（`M0 @ 局部顶点`，与
        `_render_raw` 投影用的是同一套 world 坐标），取均值作为可见质心，
        再用 `project_point` 投影（与真实渲染逐像素一致）。没有 cube 的骨骼
        （纯父骨骼，如 Eyes 只是眼珠方块的父）无法定位可见位置，返回 None，
        由调用方（petapp 的 `_gaze_anchor_for`）退回头骨骼。

        用 rest 矩阵 `M0` 避开与渲染线程抢 `Mcur`；待机动画让头小幅摆动，
        rest 与实时差几像素，而视线锚点对误差容忍很大（reach_px=520），够准。
        """
        if not bone_name or bone_name not in self.index:
            return None
        idx = self.index[bone_name]
        b = self.model.order[idx]
        pts = [pos for c in (b.get("cubes") or [])
               for f in c.get("faces", []) for pos in f.get("pos", [])]
        if not pts:
            return None
        arr = np.asarray(pts, float)
        hom = np.concatenate([arr, np.ones((len(arr), 1), float)], -1)
        world = (hom @ self.M0[idx].T)[:, :3].mean(0)
        return self.project_point(world, W, H, yaw, pitch, fov)

    # ------------------------------------------------------------ 渲染
    def render(self, anim=None, W=200, H=300, yaw=28.0, pitch=10.0,
               fov=40.0, zoom=None, cull=True):
        """返回 uint8 RGBA (H, W, 4)，未覆盖处 alpha=0。

        `zoom` 是相机后退倍率：**越大 → 相机越远 → 人物越小**。
        默认取 `self.zoom`（见 __init__，默认 1.38）。

        历史：原来硬编码 1.30，实测人物占窗高 **78%**（150x225 窗口下
        76x176），顶到上下边缘、没有留白，待机晃动时还会被裁到头顶。
        1.75 时占 57%，既留了余量又不显小。

        self.ssaa > 1 时走 SSAA：先以 ssaa 倍分辨率渲染，再下采样回来。
        """
        if zoom is None:
            zoom = self.zoom
        ss = int(self.ssaa) if self.ssaa > 1 else 1
        if self.use_cpp_pipeline and cull and self._ensure_ctx():
            return self._render_cpp(anim, W, H, ss, yaw, pitch, fov, zoom)
        if ss > 1:
            big = self._render_raw(anim, W * ss, H * ss, yaw, pitch,
                                   fov, zoom, cull)
            return self._downsample(big, ss, W, H)
        return self._render_raw(anim, W, H, yaw, pitch, fov, zoom, cull)

    # ------------------------------------------------------------ 量尺寸
    def measure_ratio(self, animator=None, clips=None, W=260, H=260,
                      yaw=28.0, pitch=10.0, frames=4, recenter=True,
                      cache_key=None, resident=None):
        """量出**人物渲染后的 宽/高 比**，并顺便把画面调到人物居中。

        需求：窗口要按「桌宠所有动作渲染出来的最大 x / 最大 y」来定，
        因为不同动作、不同朝向的 x/y 比例并不固定（实测 0.42 ~ 2.06）。

        ⚠️ 关键坑（之前一直返回 0.6667 就是这个）：
          不能直接在**目标窗口尺寸**上量。窗口本来就窄，宽动作一渲染就
          顶到画面左右边缘 → 包围盒 = 整个画幅 → 「所有动作的并集」恒等于
          画幅 → 比例永远等于 画幅宽/高。**量尺寸的取景必须比人物大**。

        ⚠️ 第二个坑（2026-10-05 用户报"缓存有问题"）：量的时候**必须把
          pre_parallel / parallel 常驻层一起算进去**。并行层才是让头发/
          裙摆飘出去的那一层，只量主体动画会漏掉「头发甩到最开」的瞬间，
          窗口就会裁到飘起来的头发。所以这里用 `resident` 一起合成。

        ⚠️ 第三个坑：**探针相机必须够远**（`probe_zoom = max(zoom, 3.2)`）。
          投影的 pb 视野只由 zoom 决定、与分辨率无关，"把方幅调大"没用。
          一出画，包围盒就被钳到画幅边缘 —— 方幅下比例必然趋近 1.0，
          量出来的就是**假比例**。比例是尺度无关的，拉远不影响结果。

        ⚠️ 第四个坑（用户报"以莫名其妙的外纵轴旋转"）：**绝不平移 pctr**。
          `pctr` 是相机绕转的枢轴，必须留在模型纵轴上；把居中偏移加到它的
          水平分量（pb_x/pb_y 是地面平面轴）会让改「朝向」时模型绕着外来
          竖轴甩。模型本来就由 `ctr` 居中，不需要再平移相机。

        现在的做法：
          1. 用一块**方形大方幅**（默认 260×260，ssaa=1、相机拉到
             `probe_zoom`）去渲染每个动作，每个动画均匀采 `frames` 个时间点，
             并在同一时间点上叠加常驻并行层（真实运行时就是这么叠的）；
          2. 累计 alpha>0 像素的**全局包围盒**（所有动作所有帧的并集）；
          3. 把像素包围盒换算回 pb 空间：
                span_v = 2*dist*tan(fov/2)          （该方幅纵向覆盖多少 pb）
                width_pb  = (gx1-gx0+1) / W * span_h
                height_pb = (gy1-gy0+1) / H * span_v
             比例 = width_pb / height_pb。**换算与方幅尺寸无关**，所以
             方幅取多大都不影响结果，只影响精度/是否裁切。

        性能：56 动画 × 4 帧 = 224 次 260×260 渲染，ssaa=1 实测 ~2.5 s；
        结果按 `cache_key`（模型路径）落盘，下次启动 <0.1 s。
        缓存带量法版本号（`_RATIO_CACHE_VER`），量法一变旧缓存自动失效。

        为什么不用 `self.aspect`：那是**静态顶点**的 pb 跨度，把 A-pose 张开
        的双臂算进去了（亚托莉 0.796），远大于实际渲染的横向跨度。
        """
        if self._ratio is not None:
            return self._ratio

        # 先查磁盘缓存（换模型会换 key，互不干扰）。
        cached = _load_ratio_cache(cache_key) if cache_key else None
        if cached is not None:
            self._ratio = cached[0]
            return self._ratio

        # ---- 1) 探针方幅渲染（关 SSAA，纯测边界，快 4 倍）
        old_ssaa = self.ssaa
        old_zoom = self.zoom
        # ⚠️ 探针必须用**更大的 zoom**（相机拉远）让任何动作都完整落在画内。
        #    投影的 pb 视野只由 zoom 决定、与分辨率无关，所以「把方幅调大」
        #    是不能解决出画的。一出画，包围盒就被钳到画幅边缘，
        #    方幅下比例必然趋近 1.0 —— 量出来的就是假比例。
        #    比例本身是**尺度无关**的，所以拉远不影响结果，只保证不钳。
        probe_zoom = max(float(old_zoom), 3.2)
        self.ssaa = 1
        self.zoom = probe_zoom
        try:
            # 像素 → pb 空间的换算（视野高度 = 2*d*tan(fov/2)）
            dist = self.frame_r / np.tan(np.radians(40.0 / 2)) * probe_zoom
            span_v = 2.0 * dist * np.tan(np.radians(40.0 / 2))
            span_h = span_v * (float(W) / float(H))

            gx0 = gy0 = 10 ** 9
            gx1 = gy1 = -1
            n_scanned = 0
            n_clipped = 0
            has_res = (resident is not None
                       and getattr(resident, "available", False))

            def scan(base_pose, t):
                nonlocal gx0, gy0, gx1, gy1, n_scanned, n_clipped
                anim = dict(base_pose) if base_pose else {}
                if has_res:
                    # 真实运行时的叠加：pre → 主体 → post
                    for b, v in resident.pre_at(t).items():
                        anim.setdefault(b, {}).update(v)
                    for b, v in resident.post_at(t).items():
                        anim.setdefault(b, {}).update(v)
                rgba = self.render(anim, W, H, yaw=yaw, pitch=pitch)
                ys, xs = np.nonzero(rgba[..., 3])
                if len(xs) < 8:
                    return
                if (xs.min() <= 0 or xs.max() >= W - 1
                        or ys.min() <= 0 or ys.max() >= H - 1):
                    n_clipped += 1
                gx0 = min(gx0, int(xs.min()))
                gx1 = max(gx1, int(xs.max()))
                gy0 = min(gy0, int(ys.min()))
                gy1 = max(gy1, int(ys.max()))
                n_scanned += 1

            if clips is not None:
                for name in clips.names():
                    clip = clips.get(name)
                    if clip is None or not clip.playable:
                        continue
                    dur = float(getattr(clip, "length", 0.0) or 0.0)
                    for i in range(frames):
                        t = (dur * i / max(1, frames - 1)) if dur > 0 else 0.0
                        try:
                            pose = clip.sample(t)
                        except Exception:                      # noqa: BLE001
                            pose = None
                        scan(pose, t)

            # 只要模型有并行层，就再单独按并行层的时长扫一遍 —— 主体动画
            # 的采样时刻未必撞上「头发甩最开」的那一下。
            if has_res:
                span = float(getattr(resident, "span", lambda: 0.0)() or 0.0)
                if span > 1e-3:
                    for i in range(max(frames, 6)):
                        scan({}, span * i / max(1, max(frames, 6) - 1))

            if n_scanned == 0 and animator is not None:
                for _ in range(max(frames * 4, 24)):
                    scan(animator.tick(), 0.0)
        finally:
            self.ssaa = old_ssaa
            self.zoom = old_zoom

        if n_clipped:
            # 还有动作出画 → 比例被低估。报出来，别静默给个假值。
            print("[measure_ratio] 警告：%d/%d 帧出画（probe_zoom=%.1f），"
                  "比例可能偏小" % (n_clipped, n_scanned, probe_zoom))

        if n_scanned == 0:
            r = self.aspect if 0.05 < self.aspect < 20.0 else 2.0 / 3.0
            self._ratio = r
            return r

        # ---- 2) 像素包围盒 → pb 空间的真实宽高
        width_pb = float(gx1 - gx0 + 1) / float(W) * span_h
        height_pb = float(gy1 - gy0 + 1) / float(H) * span_v
        r = width_pb / height_pb if height_pb > 1e-6 else self.aspect
        if not (0.05 < r < 20.0):
            r = self.aspect if 0.05 < self.aspect < 20.0 else 2.0 / 3.0
        self._ratio = r

        # ---- 3) ⚠️ 绝不平移 pctr！
        # `pctr` 是相机绕转的枢轴。之前为了"把人物摆到窗口正中"把全局包围盒
        # 中心的偏移加到了 pctr 的水平分量（pb_x/pb_y 是**地面平面**轴！）——
        # 枢轴被推离模型纵轴后，一改「朝向」（相机 yaw）模型就绕着那根外来的
        # 竖轴**甩**，用户原话："以莫名其妙的外纵轴旋转"。
        # 正确做法：枢轴始终留在模型纵轴上。模型本来就由 `ctr`（模型包围盒
        # 中心）居中，不需要再平移相机。
        dx = dy = 0.0
        if cache_key:
            _save_ratio_cache(cache_key, r, dx, dy)
        return r

    # ------------------------------------------------------------ C++ 全管线
    def _ensure_ctx(self):
        """把面数据一次性上传给 C++ 并缓存上下文（只做一次）。"""
        if self._ctx:
            return True
        if self.dll is None or not hasattr(self.dll, "ysm_new"):
            return False
        fb = np.ascontiguousarray(self.fb, np.int32)
        fv = np.ascontiguousarray(self.fl.reshape(-1, 3), np.float64)
        fu = np.ascontiguousarray(self.fuv.reshape(-1, 2), np.float64)
        fn = np.ascontiguousarray(self.fn, np.float64)
        ct = np.ascontiguousarray(self.ctr, np.float64)
        # C++ 内部会复制这些数组，但贴图是**借用指针**，必须一直留住
        self._keep = (fb, fv, fu, fn, ct)
        self._tex_ref = self.tex
        self._ctx = self.dll.ysm_new(
            ctypes.c_int(self.nf),
            fb.ctypes.data_as(ctypes.POINTER(ctypes.c_int)),
            fv.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            fu.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            fn.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            ct.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            self.tex.ctypes.data_as(ctypes.POINTER(ctypes.c_ubyte)),
            ctypes.c_int(self.tw), ctypes.c_int(self.th))
        return bool(self._ctx)

    def _render_cpp(self, anim, W, H, ss, yaw, pitch, fov, zoom):
        """FK + 相机矩阵仍在 Python（很便宜），其余全在 C++ 内完成。"""
        import pybullet as p
        Mcur, vis = self.pose_matrices(anim)
        dist = self.frame_r / np.tan(np.radians(fov / 2)) * zoom
        vm = np.ascontiguousarray(
            np.array(p.computeViewMatrixFromYawPitchRoll(
                tuple(self.pctr), dist, yaw, pitch, 0,
                upAxisIndex=2)).reshape(4, 4).T, np.float64)
        pm = np.ascontiguousarray(
            np.array(p.computeProjectionMatrixFOV(
                fov, W / H, 0.05, 100)).reshape(4, 4).T, np.float64)
        out, _ = self._buf("out", W, H)
        v8 = np.ascontiguousarray(vis, np.uint8)
        self._vis_ref = v8
        self.dll.ysm_render(
            ctypes.c_void_p(self._ctx),
            Mcur.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            v8.ctypes.data_as(ctypes.POINTER(ctypes.c_ubyte)),
            vm.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            pm.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            ctypes.c_int(W), ctypes.c_int(H), ctypes.c_int(ss),
            ctypes.c_int(1 if self.lighting else 0),
            _LIGHT.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            ctypes.c_double(_AMB), ctypes.c_double(_HEMI_W),
            ctypes.c_double(_DIFF_W),
            out.ctypes.data_as(ctypes.POINTER(ctypes.c_ubyte)))
        return out

    def __del__(self):
        try:
            if self._ctx and self.dll is not None:
                self.dll.ysm_free(ctypes.c_void_p(self._ctx))
        except Exception:                                     # noqa: BLE001
            pass

    def _render_raw(self, anim, W, H, yaw, pitch, fov, zoom, cull):
        """返回 uint8 RGBA (H, W, 4)，未覆盖处 alpha=0。"""
        import pybullet as p
        Mcur, vis = self.pose_matrices(anim)

        keep = vis[self.fb]
        Mf = Mcur[self.fb][keep]                       # (Nf',4,4)
        if Mf.shape[0] == 0:
            return np.zeros((H, W, 4), np.uint8)

        # 批量变换：世界坐标（模型空间）
        Vh = self.fh[keep]                             # (Nf',4,4)
        Vw = np.matmul(Vh, Mf.transpose(0, 2, 1))[:, :, :3]
        Rf = Mf[:, :3, :3]
        Nw = np.einsum("fij,fj->fi", Rf, self.fn[keep])

        # → pybullet 空间（Z-up）
        Vp = (Vw - self.ctr) @ P_MAT.T
        Np = Nw @ P_MAT.T

        # 背面剔除（与 render.py 一致的判据）
        if cull:
            d = np.einsum("fi,fi->f", Np, -Vp[:, 0])
            m = d <= 0
            if not m.any():
                return np.zeros((H, W, 4), np.uint8)
            Vp, Np = Vp[m], Np[m]
        else:
            m = slice(None)
        UVk = self.fuv[keep][m]

        # 明暗：默认平涂（shade 全 1，直接输出贴图原色）。
        # 需要立体感时把 self.lighting 打开，走半球环境光 + 主光（见文件头 _LIGHT 注释）。
        if self.lighting:
            hemi = 0.5 + 0.5 * Np[:, 2]             # +Z(pb) = 上
            diff = np.clip(Np @ _LIGHT, 0, None)
            shade = _AMB + _HEMI_W * hemi + _DIFF_W * diff
        else:
            shade = np.ones(Np.shape[0])

        # 投影
        # 注意：顶点已经减去 self.ctr 居中（Vp = (Vw-ctr) @ P_MAT.T），
        # 所以相机目标必须是原点；再传 P_MAT@ctr 会双重偏移，
        # 导致模型被推到画面右下方且被放大（此前所有 fast 渲染图都偏近的根因）。
        dist = self.frame_r / np.tan(np.radians(fov / 2)) * zoom
        vm = np.array(p.computeViewMatrixFromYawPitchRoll(
            tuple(self.pctr), dist, yaw, pitch, 0, upAxisIndex=2)).reshape(4, 4).T
        pm = np.array(p.computeProjectionMatrixFOV(
            fov, W / H, 0.05, 100)).reshape(4, 4).T
        Mv = pm @ vm
        Vq = np.concatenate([Vp, np.ones((Vp.shape[0], 4, 1))], -1)
        clip = np.matmul(Vq, Mv.T)
        ndc = clip[:, :, :3] / clip[:, :, 3:4]
        sx = (ndc[:, :, 0] * 0.5 + 0.5) * W
        sy = (1 - (ndc[:, :, 1] * 0.5 + 0.5)) * H
        sz = ndc[:, :, 2]

        # 四边形 -> 两个三角形 (0,1,2) (0,2,3)
        #
        # ⚠️ 顺序必须与 native/raster.cpp 的 ysm_render 完全一致（AoS：
        #    面0三角A、面0三角B、面1三角A、面1三角B…），不能写成「所有 A 在前、
        #    所有 B 在后」的 SoA 布局。
        #
        # 原因：模型里有大量**共面重叠**的四边形（相邻方块共享同一个平面、
        #    材质面与遮挡面重合）。raster_faces 的深度测试是严格小于
        #    `!(z < ZB[idx])`，所以共面时**先写入者胜**。两种布局会改变这些
        #    像素最终采到哪个面的颜色 —— 实测同一批三角形数据仅换顺序，
        #    就有 13558 个像素不同、平均差 16.8。保持 AoS 才能与 C++ 逐像素一致。
        Nf = sx.shape[0]
        P = np.empty((Nf * 2, 3, 3), np.float64)
        U = np.empty((Nf * 2, 3, 2), np.float64)
        S = np.empty(Nf * 2, np.float64)
        A = np.stack([sx, sy, sz], -1)                    # (Nf,4,3)
        P[0::2] = A[:, [0, 1, 2]]
        P[1::2] = A[:, [0, 2, 3]]
        U[0::2] = UVk[:, [0, 1, 2]]
        U[1::2] = UVk[:, [0, 2, 3]]
        S[0::2] = shade
        S[1::2] = shade

        out, zb = self._buf("raw", W, H)
        if self.dll is None:
            return self._fallback(P, U, S, W, H, out, zb)
        self.dll.raster_faces(
            P.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            U.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            S.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            ctypes.c_int(Nf * 2),
            self.tex.ctypes.data_as(ctypes.POINTER(ctypes.c_ubyte)),
            ctypes.c_int(self.tw), ctypes.c_int(self.th),
            zb.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            out.ctypes.data_as(ctypes.POINTER(ctypes.c_ubyte)),
            ctypes.c_int(W), ctypes.c_int(H))
        return out

    def _buf(self, key, W, H):
        """取（并清空）一块 (H,W,4) 输出缓冲 + z-buffer，按 key 分开缓存。"""
        got = self._bufs.get(key)
        if got is None or got[0].shape[:2] != (H, W):
            got = (np.zeros((H, W, 4), np.uint8),
                   np.full((H, W), np.inf, np.float64))
            self._bufs[key] = got
        out, zb = got
        out[:] = 0
        zb[:] = np.inf
        return out, zb

    def _downsample(self, big, ss, W, H):
        """SSAA 下采样 ss×ss 块。

        * alpha 取算术平均 → 轮廓从 0/255 二值变成软边（这是消锯齿的关键）
        * RGB 按 alpha 加权平均 → 半覆盖像素不会被未覆盖处的 0 值拉黑
        """
        out, _ = self._buf("out", W, H)
        if self.dll is not None:
            # 走 C++：一遍循环出结果，不产生任何临时数组
            self.dll.downsample_rgba(
                big.ctypes.data_as(ctypes.POINTER(ctypes.c_ubyte)),
                ctypes.c_int(W), ctypes.c_int(H), ctypes.c_int(ss),
                out.ctypes.data_as(ctypes.POINTER(ctypes.c_ubyte)))
            return out

        # 没有 DLL 时的 numpy 回退
        b = big.reshape(H, ss, W, ss, 4).astype(np.float32)
        w = b[..., 3]                                   # (H,ss,W,ss)
        wsum = w.sum(axis=(1, 3))                       # (H,W)
        num = (b[..., :3] * w[..., None]).sum(axis=(1, 3))
        den = np.where(wsum > 0.0, wsum, 1.0)
        out[..., :3] = np.clip(num / den[..., None], 0, 255).astype(np.uint8)
        out[..., 3] = (wsum / float(ss * ss)).astype(np.uint8)
        return out

    def _fallback(self, P, U, S, W, H, out, zbuf=None):
        """没有 DLL 时用纯 Python 光栅（慢，但保证能跑）。"""
        if zbuf is None:
            zbuf = self._buf("raw", W, H)[1]
        tex = self.tex
        for t in range(P.shape[0]):
            p = P[t]
            u = U[t]
            sh = float(S[t])
            A, B, C = p[0].astype(float), p[1].astype(float), p[2].astype(float)
            v0x, v0y = B[0] - A[0], B[1] - A[1]
            v1x, v1y = C[0] - A[0], C[1] - A[1]
            den = v0x * v1y - v1x * v0y
            if abs(den) < 1e-9:
                continue
            minx = max(int(np.floor(min(A[0], B[0], C[0]))), 0)
            maxx = min(int(np.ceil(max(A[0], B[0], C[0]))), W - 1)
            miny = max(int(np.floor(min(A[1], B[1], C[1]))), 0)
            maxy = min(int(np.ceil(max(A[1], B[1], C[1]))), H - 1)
            if minx > maxx or miny > maxy:
                continue
            gx = np.arange(minx, maxx + 1) + 0.5
            gy = np.arange(miny, maxy + 1) + 0.5
            X, Y = np.meshgrid(gx, gy)
            px, py = X - A[0], Y - A[1]
            w1 = (px * v1y - v1x * py) / den
            w2 = (v0x * py - px * v0y) / den
            m = (w1 >= 0) & (w2 >= 0) & (w1 + w2 <= 1)
            if not m.any():
                continue
            z = A[2] + w1 * (B[2] - A[2]) + w2 * (C[2] - A[2])
            ok = m & (z < zbuf[miny:maxy + 1, minx:maxx + 1])
            if not ok.any():
                continue
            uu = u[0, 0] + w1 * (u[1, 0] - u[0, 0]) + w2 * (u[2, 0] - u[0, 0])
            vv = u[0, 1] + w1 * (u[1, 1] - u[0, 1]) + w2 * (u[2, 1] - u[0, 1])
            ui = np.clip(uu[ok].astype(int), 0, self.tw - 1)
            vi = np.clip(vv[ok].astype(int), 0, self.th - 1)
            cols = tex[vi, ui]
            vs = cols[:, 3] >= 100
            if not vs.any():
                continue
            iy, ix = np.nonzero(ok)
            iy, ix = iy[vs] + miny, ix[vs] + minx
            cc = cols[vs][:, :3] * sh
            out[iy, ix, :3] = np.clip(cc, 0, 255)
            out[iy, ix, 3] = 255
            zbuf[iy, ix] = z[ok][vs]
        return out
