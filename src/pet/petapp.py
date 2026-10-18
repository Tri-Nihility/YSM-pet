"""YSM 桌宠：透明背景窗口 + 实时渲染。

架构
----
    RenderThread（后台）                主界面线程
    ─────────────────────              ─────────────
    IdleAnimator.tick()  → anim         PetWindow.paintEvent
    FastRenderer.render() → RGBA  ──Signal──▶  QImage → 贴图
                                       拖拽 / 右键菜单

* 渲染 ~25-31 ms/帧，放在后台线程，拖拽与菜单不会被卡住。
* 窗口：无边框 + 置顶 + 工具窗口（不进任务栏）+ 逐像素透明。
* 未覆盖处 alpha=0，桌面直接透出来。
"""
import math
import os
import sys
import time

import numpy as np

from PySide6 import QtCore, QtGui, QtWidgets

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (ROOT, os.path.join(ROOT, "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _hide_effects():
    """「清晰模式」是否开启（True = 只画人物本体）。

    对应设置 `show_effects`（默认 False，即默认只画本体）。设置读不到时按
    「开」处理 —— 宁可少画几件特效，也不要让特效糊在人物脸上。
    """
    try:
        from pet import settings as S
        return not bool(S.load().get("show_effects", False))
    except Exception:                                         # noqa: BLE001
        return True


# ============================================================ 待机动画
class IdleAnimator:
    """待机：呼吸 + 头部缓慢摆动 + 手臂微摆。

    注：随机眨眼已移除（2026-10-18，用户要求：程序化呼吸只保留呼吸，
    不要眨眼）—— 原来的实现靠压扁眼球骨骼，部分模型上会露出眼皮/
    眉骨变形，观感不自然。

    动画字典格式（与 FastRenderer.pose_matrices 一致）：
        {bone: {"rot":[rx,ry,rz](度), "pos":[x,y,z](1/16 方块), "scale":[s,s,s]}}
    """

    def __init__(self, model, enabled=True):
        self.names = [b["name"] for b in model.order]
        self.nset = set(self.names)
        self.enabled = enabled
        self.t0 = time.time()
        self.pose = {}                      # 外部摆 pose 叠加层
        # G：待机平滑。_sm 存各通道上一帧的值，_last_t 用于算 dt。
        self._sm = {}
        self._last_t = self.t0
        self._bind_bones()

    # G：各通道的平滑速率（1/s）。越小越"懒"，越大越跟手。
    # 呼吸最慢（本来就该缓），摆头和摆臂稍快一点才有生气。
    RATE_BREATH = 2.2
    RATE_SWAY = 1.6
    RATE_SWING = 1.9

    def _pick(self, *cands):
        for c in cands:
            if c in self.nset:
                return c
        return None

    def _bind_bones(self):
        """把逻辑部位绑到模型里真实存在的骨骼名；缺哪块就整块跳过。"""
        self.b_upper = self._pick("UpperBody", "MUpperBody")
        self.b_head = self._pick("Head", "MHead", "AllHead")
        self.b_root = self._pick("AllBody", "MAllBody")
        self.b_rarm = self._pick("RightArm")
        self.b_larm = self._pick("LeftArm")
        self.b_rfore = self._pick("RightForeArm")
        self.b_lfore = self._pick("LeftForeArm")
        self.b_rleg = self._pick("RightLeg")
        self.b_lleg = self._pick("LeftLeg")

    # ---------------------------------------------------------------- 平滑
    def _smooth(self, key, target, dt, rate, limit=6.0):
        """一阶低通 + 速率限幅，给待机动作"惯性"。

        为什么需要：直接喂正弦，骨骼每个采样点的速度都是解析导数，
        帧率抖动时看起来一顿一顿的；而且多个正弦各自起步相位相同，
        叠加后像机械臂。这里做两件事：
          1) 以 `rate`（1/s）指数逼近目标值 —— 运动有起停缓冲；
          2) 限制单帧最大变化量 `limit`（度/秒，按 60fps 折算）——
             即使某帧 dt 突然变大，也不会一步跳到位。

        rate 越大越跟手；limit 越小越"黏"。
        """
        cur = self._sm.get(key, target)
        if dt <= 1e-6:
            self._sm[key] = cur
            return cur
        alpha = 1.0 - math.exp(-max(0.0, rate) * dt)
        nxt = cur + (target - cur) * alpha
        # 速率限幅：按 60fps 折算每帧最大步长
        max_step = abs(limit) * dt
        d = nxt - cur
        if abs(d) > max_step > 0:
            nxt = cur + math.copysign(max_step, d)
        self._sm[key] = nxt
        return nxt

    # ------------------------------------------------------------------
    def tick(self):
        """返回当前帧的动画字典。

        G（待机晃动优化）：三个动作不再用「各自固定频率的纯正弦」，
        改成 **统一时间基 + 双频叠加 + 速率平滑**：
          - 双频：主频 + 一个 1.7 倍的次频，幅值约 1:0.35，
            避免规整正弦的"节拍感"（1.7 是非整数比，不会周期性对齐）
          - 速率平滑：整体幅值再乘一个缓慢起伏的包络，让动作有"呼吸式"
            的强弱变化，而不是一直以固定幅度晃
          - 每通道过 `_smooth` 低通 + 限速，消除帧率抖动引起的突跳
        """
        anim = {}
        if self.pose:
            for b, v in self.pose.items():
                if b in self.nset:
                    anim[b] = dict(v)
        if not self.enabled:
            return anim

        now = time.time()
        t = now - self.t0
        dt = min(0.1, max(0.0, now - self._last_t))     # 夹住 dt，防首帧/卡顿暴走
        self._last_t = now

        # 双频叠加：主频 sin(w) + 次频 0.35*sin(1.7w)
        def duo(period, phase=0.0):
            w = 2 * math.pi * t / period + phase
            return math.sin(w) + 0.35 * math.sin(1.7 * w)

        # 包络：整体幅度在 0.65~1.0 之间缓变（周期 23s，与各主频不成比例）
        env = 0.65 + 0.35 * (0.5 + 0.5 * math.sin(2 * math.pi * t / 23.0))

        br = duo(4.0) * env * 0.72            # 呼吸：降幅（原为 1.0）
        sway = duo(9.0, 0.4) * env * 0.80     # 头部摆动：降幅 + 错相位
        swing = duo(6.0, 1.1) * env * 0.85    # 手臂摆动：降幅 + 错相位

        # 各通道独立平滑：rate 越小越"懒"，柔化起停
        br = self._smooth("br", br, dt, self.RATE_BREATH)
        sway = self._smooth("sway", sway, dt, self.RATE_SWAY)
        swing = self._smooth("swing", swing, dt, self.RATE_SWING)

        def add(bone, rot=None, pos=None, scale=None):
            if not bone:
                return
            e = anim.setdefault(bone, {})
            if rot is not None:
                e["rot"] = list(rot)
            if pos is not None:
                e["pos"] = list(pos)
            if scale is not None:
                e["scale"] = list(scale)

        # 幅值整体下调（G1：降幅），并做过冲保护
        add(self.b_root, pos=[0.0, 0.32 * br, 0.0])
        add(self.b_upper, rot=[-1.0 * br, 0.0, 0.0])
        add(self.b_head, rot=[-0.62 * br, 2.6 * sway, 0.0])
        add(self.b_rarm, rot=[1.6 * br + 0.72 * swing, 0.0, 0.0])
        add(self.b_larm, rot=[-1.6 * br - 0.72 * swing, 0.0, 0.0])
        add(self.b_rfore, rot=[0.85 * br, 0.0, 0.0])
        add(self.b_lfore, rot=[-0.85 * br, 0.0, 0.0])
        return anim


# ============================================================ 动画轮盘
def _pick_cjk_font():
    """挑一个能显示中文的字体（动画名有中文），找不到返回空串走默认。"""
    from PySide6 import QtGui as _G
    pref = ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑",
            "Noto Sans CJK SC", "Source Han Sans SC", "SimHei",
            "PingFang SC", "WenQuanYi Micro Hei")
    fams = set(_G.QFontDatabase.families())
    for f in pref:
        if f in fams:
            return f
    return ""


class ClipWheel(QtWidgets.QWidget):
    """环形动画轮盘：**复刻原版 YSM 的 AnimationRouletteScreen 布局**。

    上游实现（`reference/.../client/gui/AnimationRouletteScreen.java`）：
    * 内容 = `ModelProperties.getExtraAnimation()`（.ysm 里的 extra_animations，
      作者排好序的那 16 个），**不是** main 组动作；
    * 每页 **8 个**扇区，每格 **45°**（`Math.PI/4`），起始角 0°
      （MC 屏幕 y 向下 → 12 点方向起顺时针在 Qt 里等价于 -90° 起）；
    * 扇区填充：内径 **25**、外径 **115**（相对圆心像素）；
    * hover 判定：半径 **50..100** 且角度落在扇区内；
    * 文字画在半径 **65** 处；
    * `<` `>` 翻页按钮在圆心右侧；`#` 开头的键是子菜单（画 ⚙ 图标）。

    布局：**8 个圆角矩形块，左 4 右 4，纵向环绕桌宠两侧**。
    桌宠本体完全不被遮挡（上游是全屏 GUI，圆心让给桌宠会挡脸，
    所以这里从环形改成两侧分列）。

    每页仍 8 项，`<` `>` 翻页、面包屑、子菜单、⚙ 参数项、返回按钮
    的行为与上游一致；点空白/桌宠 = 停止播放（对应上游圆心的 stop）。
    """

    ITEMS_PER_PAGE = 8
    # ---- 矩形块布局（块分布在桌宠两侧，纵向环绕）----
    BLOCK_W_MIN = 92.0   # 块最小宽度；实际宽度按最长标签自适应
    BLOCK_W_MAX = 168.0  # 块宽上限，防止个别超长名字把窗体撑爆
    BLOCK_H = 26.0       # 块高
    BLOCK_GAP = 8.0      # 同列相邻块的间距
    SIDE_GAP = 10.0      # 块列与桌宠之间的水平留白
    PAD_V = 30.0         # 顶部（面包屑+翻页）/底部（返回）控件行高
    HEAD_H = 34.0        # 顶部控件带高度（面包屑在左、翻页在右）
    BTN = 15.0           # 翻页按钮半高

    def __init__(self, pet, scale=1.6):
        super().__init__(None)
        self.pet = pet
        self.scale = scale                   # 半径缩放，跟着桌宠大小走
        self.hover = -1                      # 当前 hover 的全局索引
        self.current = None                  # 正在播的动画名
        self.page = 0
        self.stack = [""]                    # 菜单导航栈，[""] = 顶层
        self.setWindowTitle("YSM 动画轮盘")
        self.setWindowFlags(QtCore.Qt.FramelessWindowHint
                            | QtCore.Qt.WindowStaysOnTopHint
                            | QtCore.Qt.Tool
                            | QtCore.Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setToolTip("动画轮盘：点扇区播放，再点一次回待机；< > 翻页；"
                        "点 × 或按 Esc 关闭")
        self._font_family = _pick_cjk_font()
        self._build_items()

    # ---------------------------------------------------------- 内容
    def _build_items(self):
        """items = [(kind, name, label), ...]，顺序即轮盘顺序。

        kind ∈ {"clip","submenu","config","return"}。
        顶层把「表情动作 / 本体动作」两组收进轮盘（用户要求：把表情缩合进
        轮盘）；点进子菜单再列出该组下全部可播动画。
        """
        lib = self.pet.clips
        menu = self.stack[-1]
        if menu == "":
            # 顶层：表情动作 + 本体动作 两个入口
            items = []
            if lib is not None and len(lib.by_group("extra")) > 0:
                items.append(("submenu", "__expr__", "表情动作"))
            if lib is not None and len(lib.by_group("main")) > 0:
                items.append(("submenu", "__body__", "本体动作"))
            self.items = items
            self.page = 0
            self.hover = -1
            return
        if menu in ("__expr__", "__body__"):
            grp = "extra" if menu == "__expr__" else "main"
            names = lib.by_group(grp) if lib is not None else []
            self.items = [("clip", n, lib.display_name(n) if lib else n)
                          for n in names]
            self.page = 0
            self.hover = -1
            return
        if lib is not None and hasattr(lib, "roulette"):
            self.items = list(lib.roulette(menu))
        else:
            self.items = []
        self.page = 0
        self.hover = -1

    @property
    def menu_id(self):
        return self.stack[-1]

    def _enter(self, menu_id):
        """进入子菜单。"""
        self.stack.append(menu_id)
        self._build_items()
        self.update()

    def _back(self):
        """返回上一层。"""
        if len(self.stack) > 1:
            self.stack.pop()
            self._build_items()
            self.update()

    def breadcrumb(self):
        """菜单路径，对应上游的 `gui.yes_steve_model.roulette.path`。"""
        lib = self.pet.clips
        alias = {"__expr__": "表情动作", "__body__": "本体动作"}
        parts = ["动画"]
        for mid in self.stack[1:]:
            if mid in alias:
                parts.append(alias[mid])
            else:
                parts.append(lib.submenu_label(mid)
                             if hasattr(lib, "submenu_label") else mid)
        return " > ".join(parts)

    @property
    def pages(self):
        n = len(self.items)
        return max(1, (n + self.ITEMS_PER_PAGE - 1) // self.ITEMS_PER_PAGE)

    def _page_items(self):
        """当前页的 (全局索引, kind, name, label) 列表。"""
        base = self.page * self.ITEMS_PER_PAGE
        out = []
        for i in range(self.ITEMS_PER_PAGE):
            gi = base + i
            if gi < len(self.items):
                out.append((gi,) + tuple(self.items[gi]))
        return out

    # ---------------------------------------------------------- 尺寸
    def refresh_size(self):
        """窗体 = 左列 + 桌宠 + 右列，高度取桌宠与两列块高的较大者。

        块分布在桌宠两侧，桌宠本体完全不被遮挡（上游是全屏 GUI，圆心
        让给桌宠会挡脸，这里改成两侧分列）。
        """
        pw, ph = self.pet.width(), self.pet.height()
        n = min(self.ITEMS_PER_PAGE, max(1, len(self._page_items())))
        col_h = n * (self.BLOCK_H + self.BLOCK_GAP) - self.BLOCK_GAP
        bw = self._block_w()
        w = pw + 2 * (self.SIDE_GAP + bw) + 8
        # 顶部控件带必须能同时放下「面包屑」和「页码+翻页」，否则会叠字
        w = max(w, 300.0)
        h = max(ph + self.HEAD_H, col_h)
        self.setFixedSize(int(w), int(h + self.HEAD_H))
        return self.width()

    # ---------------------------------------------------------- 几何
    def _geom(self):
        """返回桌宠中心在本窗体里的位置。

        顶部留 `HEAD_H` 给「面包屑 + 翻页」，所以桌宠中心相对窗体中心
        整体下移半个 HEAD_H。
        """
        return self.width() / 2.0, (self.height() + self.HEAD_H) / 2.0

    def _block_w(self):
        """块宽按当前页最长标签自适应（名字长短差很大，固定宽会裁字）。"""
        fm = QtGui.QFontMetrics(self.font())
        texts = []
        for _gi, k, _n, label in self._page_items():
            t = ("▸ " if k == "submenu" else "⚙ " if k == "config" else "") + label
            texts.append(t)
        need = max([fm.horizontalAdvance(t) for t in texts] or [0]) + 26
        return max(self.BLOCK_W_MIN, min(self.BLOCK_W_MAX, float(need)))

    def _cols(self):
        """左右两列的 x 范围 + 块尺寸。桌宠左右各贴一列。"""
        pw, _ph = self.pet.width(), self.pet.height()
        cx, _ = self._geom()
        bw, bh = self._block_w(), self.BLOCK_H
        lx = cx - pw / 2.0 - self.SIDE_GAP - bw
        rx = cx + pw / 2.0 + self.SIDE_GAP
        return (lx, rx, bw, bh)

    def _block_rect(self, i):
        """第 i 个块（0..7）的矩形。左侧从上到下 = 0..3，右侧从上到下 = 4..7。"""
        page = self._page_items()
        if i < 0 or i >= len(page):
            return None
        lx, rx, bw, bh = self._cols()
        # 8 格固定 4+4；不足 8 时上半数走左列、其余走右列
        half = (len(page) + 1) // 2
        col = 0 if i < half else 1
        row = i if col == 0 else i - half
        rows = half if col == 0 else len(page) - half
        col_h = rows * (bh + self.BLOCK_GAP) - self.BLOCK_GAP
        top = self.height() / 2.0 - col_h / 2.0
        x = lx if col == 0 else rx
        y = top + row * (bh + self.BLOCK_GAP)
        return QtCore.QRectF(x, y, bw, bh)

    # ---------------------------------------------------------- 控件位置
    def _prect(self):
        """顶部右侧的 `‹ 1/2 ›`：贴窗体右上角，避开左侧面包屑。"""
        h = self.BTN * 2
        y = (self.HEAD_H - h) / 2.0
        right = self.width() - 6
        return (QtCore.QRectF(right - 60, y, 26, h),
                QtCore.QRectF(right - 26, y, 26, h))

    def _back_rect(self):
        """底部返回按钮：只在子菜单里显示，横向居中。"""
        cx, _ = self._geom()
        y = self.height() - 16.0
        return QtCore.QRectF(cx - 44, y - 11, 88, 22)

    def _close_rect(self):
        """左上角 × 关闭按钮（避开面包屑，面包屑已从 x=28 起画）。"""
        s = 18.0
        return QtCore.QRectF(6.0, 6.0, s, s)

    def _slice_at(self, pos):
        """命中测试 → (kind, index)。kind: 'item' | 'prev' | 'next' | 'back' | 'close' | None。"""
        # 关闭按钮（×，左上角）
        if self._close_rect().contains(pos):
            return ("close", -1)
        # 顶部控件行：<  ·  1/2  ·  >
        if self.pages > 1:
            rp, rn = self._prect()
            if rp.contains(pos):
                return ("prev", -1)
            if rn.contains(pos):
                return ("next", -1)

        # 底部返回按钮：子菜单里才有
        if len(self.stack) > 1 and self._back_rect().contains(pos):
            return ("back", -1)

        for i in range(len(self._page_items())):
            r = self._block_rect(i)
            if r is not None and r.contains(pos):
                return ("item", self._page_items()[i][0])
        return (None, -1)

    def mouseMoveEvent(self, e):
        kind, gi = self._slice_at(e.position())
        h = gi if kind == "item" else -1
        if h != self.hover:
            self.hover = h
            self.update()
        if kind in ("prev", "next"):
            self.setToolTip("上一页" if kind == "prev" else "下一页")
        elif kind == "back":
            self.setToolTip("返回上一层")
        elif h >= 0:
            self.setToolTip(self._tip_of(h))
        else:
            self.setToolTip("动画轮盘：点扇区播放，再点一次回待机；< > 翻页；"
                        "点 × 或按 Esc 关闭")

    def _tip_of(self, gi):
        """悬停提示：动画给时长，子菜单/表单给说明。"""
        k, name, label = self.items[gi]
        if k == "clip":
            c = self.pet.clips.get(name)
            return "%s（%s）" % (label, c.duration_text() if c else "?")
        if k == "submenu":
            n = len(self.pet.clips.classify.get(name, {}))
            return "%s ▸ 子菜单（%d 项）" % (label, n)
        if k == "config":
            return "%s ⚙ 参数设置" % label
        return label

    def leaveEvent(self, _e):
        if self.hover != -1:
            self.hover = -1
            self.update()

    def mousePressEvent(self, e):
        if e.button() != QtCore.Qt.LeftButton:
            return
        kind, gi = self._slice_at(e.position())
        if kind == "close":
            self.hide()                       # 关闭键：× 按钮
            return
        if kind == "prev":
            self.page = (self.page - 1) % self.pages
            self.update()
            return
        if kind == "next":
            self.page = (self.page + 1) % self.pages
            self.update()
            return
        if kind == "back":
            self._back()
            return
        if kind == "item":
            k, name, label = self.items[gi]
            if k == "submenu":
                self._enter(name)
            elif k == "return":
                self._back()
            elif k == "config":
                # 参数表单组：桌宠场景下没有配套的 molang 执行环境，
                # 这里只提示，不假装能改（避免给出无效 UI）。
                self.setToolTip("%s：该模型带了参数表单，桌宠端暂不支持" % label)
            else:
                nxt = None if self.current == name else name
                self.current = nxt
                self.pet.play_clip(nxt)
                self.update()
            return
        # 点在块以外（含桌宠本体、空白处）= 停止动画、回到默认待机
        # （原版轮盘圆心是 "stop" 按钮；这里圆心让给桌宠，把"停"放在
        #   「点任何非块区域」上，语义一致，且不会误挡桌宠）
        self.current = None
        self.pet.play_clip(None)
        self.update()

    def keyPressEvent(self, e):
        """关闭键：Esc 收起轮盘（× 按钮是鼠标入口，见 _close_rect）。"""
        if e.key() == QtCore.Qt.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(e)

    # ---------------------------------------------------------- 绘制
    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)

        f = p.font()
        f.setPointSize(9)
        if self._font_family:
            f.setFamily(self._font_family)
        p.setFont(f)

        # ---- 8 个矩形块，左右两列环绕桌宠 ----
        page = self._page_items()
        for i, (gi, k, name, label) in enumerate(page):
            r = self._block_rect(i)
            if r is None:
                continue
            sel = (k == "clip" and name == self.current)
            hov = (gi == self.hover)
            if sel:
                bg, edge = QtGui.QColor(76, 141, 255, 235), QtGui.QColor(150, 190, 255)
            elif hov:
                bg, edge = QtGui.QColor(88, 122, 190, 230), QtGui.QColor(160, 200, 255)
            elif k in ("submenu", "config"):
                bg, edge = QtGui.QColor(64, 56, 30, 225), QtGui.QColor(190, 160, 70)
            else:
                bg, edge = QtGui.QColor(28, 32, 42, 215), QtGui.QColor(90, 108, 140)

            p.setPen(QtGui.QPen(edge, 1.4))
            p.setBrush(bg)
            p.drawRoundedRect(r, 7, 7)

            # 左侧色条：选中/悬停时高亮，方便一眼看出当前在播哪个
            if sel or hov:
                p.setPen(QtCore.Qt.NoPen)
                p.setBrush(QtGui.QColor(160, 200, 255) if sel
                           else QtGui.QColor(120, 160, 220))
                p.drawRoundedRect(QtCore.QRectF(r.left() + 3, r.top() + 5, 3,
                                                r.height() - 10), 1.5, 1.5)

            text = ("▸ " + label) if k == "submenu" else \
                   ("⚙ " + label) if k == "config" else label
            p.setPen(QtGui.QColor(255, 255, 255) if sel
                     else QtGui.QColor(226, 233, 244))
            p.drawText(r.adjusted(10, 0, -6, 0),
                       QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, text)

        # ---- 顶部一行：× 关闭（左） + 面包屑（左，让开 ×） + `< 1/2 >`（右） ----
        f2 = p.font()
        f2.setPointSize(8)
        f2.setBold(False)
        if self._font_family:
            f2.setFamily(self._font_family)
        p.setFont(f2)

        # × 关闭按钮
        cr = self._close_rect()
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(40, 44, 56, 220))
        p.drawRoundedRect(cr, 4, 4)
        p.setPen(QtGui.QColor(232, 238, 248))
        p.setFont(f2)
        p.drawText(cr, QtCore.Qt.AlignCenter, "\u2715")

        brow = self.width() - 74 if self.pages > 1 else self.width() - 8
        p.setPen(QtGui.QColor(196, 210, 234))
        p.drawText(QtCore.QRectF(28, 0, max(40.0, brow - 28), self.HEAD_H),
                   QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                   self.breadcrumb())

        if self.pages > 1:
            rp, rn = self._prect()
            for rect, txt in ((rp, "\u2039"), (rn, "\u203a")):
                p.setPen(QtCore.Qt.NoPen)
                p.setBrush(QtGui.QColor(30, 34, 44, 210))
                p.drawRoundedRect(rect, 5, 5)
                p.setPen(QtGui.QColor(230, 236, 246))
                p.drawText(rect, QtCore.Qt.AlignCenter, txt)
            p.setPen(QtGui.QColor(160, 200, 255))
            p.drawText(QtCore.QRectF(rp.right() + 2, rp.top(),
                                     rn.left() - rp.right() - 4, rp.height()),
                       QtCore.Qt.AlignCenter,
                       "%d/%d" % (self.page + 1, self.pages))

        # ---- 底部：返回按钮（子菜单里才有） ----
        if len(self.stack) > 1:
            br = self._back_rect()
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(QtGui.QColor(30, 34, 44, 210))
            p.drawRoundedRect(br, 5, 5)
            p.setPen(QtGui.QColor(230, 236, 246))
            p.drawText(br, QtCore.Qt.AlignCenter, "\u25c2 \u8fd4\u56de")
        p.end()


# ============================================================ 渲染线程
class RenderThread(QtCore.QThread):
    frame = QtCore.Signal(object)
    stat = QtCore.Signal(float)

    def __init__(self, renderer, animator, W=200, H=300, fps=30, rig=None,
                 gaze=None, clip_player=None, resident=None, yaw=None,
                 pitch=None):
        super().__init__()
        self.r = renderer
        self.animator = animator
        self.rig = rig
        self.gaze = gaze                    # GazeTracker（可为 None）
        self.clip_player = clip_player      # ClipPlayer（可为 None）
        self.resident = resident            # ResidentPlayer（常驻并行层）
        self.W, self.H = W, H
        self.fps = fps
        self._stop = False
        # 朝向：显式传入优先；否则读持久化设置（默认 yaw=90 / pitch=0）
        if yaw is None or pitch is None:
            from pet import settings as _S
            _cfg = _S.load()
        if yaw is None:
            yaw = float(_cfg.get("yaw", 90.0))
        if pitch is None:
            pitch = float(_cfg.get("pitch", 0.0))
        self.yaw, self.pitch = float(yaw), float(pitch)
        self.spin = 0.0                     # deg/s，>0 时自动旋转展示

    def set_size(self, W, H):
        self.W, self.H = int(W), int(H)

    def stop(self):
        self._stop = True
        self.wait(3000)

    def run(self):
        t_prev = time.time()
        while not self._stop:
            t0 = time.time()
            if self.spin:
                self.yaw += self.spin * (t0 - t_prev)
            t_prev = t0
            # 叠加顺序（后写覆盖），对齐原版 YSM 的分层：
            #   1) 自捏待机（呼吸/摆头，垫底兜底）
            #   2) pre_parallel 常驻层（头发/裙摆的基础校正）
            #   3) 主体动画（用户选的 / 模型默认）
            #   4) parallel 常驻层（覆盖，驱动飘动部件）
            #   5) 表情显隐
            #   6) 视线追踪（最高优先级）
            anim = self.animator.tick()
            if self.resident is not None:
                for b, v in self.resident.layer_pre(t0).items():
                    e = anim.setdefault(b, {})
                    e.update(v)
            if self.clip_player is not None:
                for b, v in self.clip_player.tick(t0).items():
                    e = anim.setdefault(b, {})
                    e.update(v)
            if self.resident is not None:
                for b, v in self.resident.layer_post(t0).items():
                    e = anim.setdefault(b, {})
                    e.update(v)
            # 表情是「显隐」层，叠加在待机动画之上（scale=0 隐藏未选中选项）
            if self.rig is not None and getattr(self.rig, "enabled", False):
                for b, v in self.rig.to_anim().items():
                    e = anim.setdefault(b, {})
                    e.update(v)
            # 视线追踪：头 + 眼跟随鼠标，放最后以保证不被别的层覆盖
            if self.gaze is not None:
                g = self.gaze.tick()
                for b, v in self.gaze.to_anim(g).items():
                    e = anim.setdefault(b, {})
                    e.update(v)
            rgba = self.r.render(anim, self.W, self.H,
                                 yaw=self.yaw, pitch=self.pitch)
            # 渲染器复用同一块缓冲，必须拷贝后再跨线程发送
            self.frame.emit(rgba.copy())
            self.stat.emit(time.time() - t0)
            budget = max(0.0, 1.0 / self.fps - (time.time() - t0))
            self.msleep(int(budget * 1000))


# ============================================================ 全局热键（鼠标穿透的逃生舱）
# 为什么必须有这个东西：
#   鼠标穿透一旦开启，桌宠窗口带上 WindowTransparentForInput，**收不到任何
#   鼠标事件** —— 右键菜单点不出来，穿透开关自己把自己锁死了。
#   理论上「托盘图标 → 设置… → 取消勾选」能救回来，但 Win11 默认把托盘
#   图标折叠进"隐藏的图标"里，用户经常找不到。
#   所以再挂一个**系统级热键** Ctrl+Alt+P：无论焦点在哪个程序、托盘在不在，
#   按一下就把穿透切回来。这是最后一道保险，不做可发现性设计。
_HOTKEY_ID = 0xB531                 # 本进程内唯一的注册 id
_WM_HOTKEY = 0x0312


class GlobalHotkey(QtCore.QAbstractNativeEventFilter):
    """Windows 系统级热键（RegisterHotKey + WM_HOTKEY）。

    热键挂在**一个专属的、永不显示的原生窗口**上，而不是桌宠窗口：
    `toggle_click_through()` 里的 `setWindowFlags()` 在 Windows 上会销毁并
    重建原生窗口，挂在桌宠 HWND 上的热键会跟着失效（表现为"开一次穿透
    之后 Ctrl+Alt+P 就没反应了"）。宿主窗口不碰 flags，HWND 恒定。
    """

    def __init__(self, callback, mods=0x0002 | 0x0001, vk=0x50):
        super().__init__()
        self._cb = callback
        self._mods, self._vk = mods, vk   # 默认 Ctrl+Alt+P
        self.ok = False
        self._ctypes = None
        self._host = None
        if sys.platform != "win32":
            return
        try:
            import ctypes
            u = ctypes.windll.user32
            # 显式声明签名：HANDLE 在 64 位下是指针，不声明容易被截断
            u.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                         ctypes.c_uint, ctypes.c_uint]
            u.RegisterHotKey.restype = ctypes.c_bool
            u.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
            u.UnregisterHotKey.restype = ctypes.c_bool
            # 永不 show() 的宿主窗口：winId() 会立刻建出原生 HWND
            self._host = QtWidgets.QWidget()
            self._host.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
            hwnd = int(self._host.winId())
            if hwnd and u.RegisterHotKey(ctypes.c_void_p(hwnd), _HOTKEY_ID,
                                         self._mods, self._vk):
                self._ctypes = ctypes
                self._hwnd = hwnd
                self.ok = True
            elif u.RegisterHotKey(None, _HOTKEY_ID, self._mods, self._vk):
                # 退路：线程级注册（hwnd=0），靠 Qt 把线程消息交给过滤器
                self._ctypes = ctypes
                self._hwnd = 0
                self.ok = True
            else:
                print("[hotkey] Ctrl+Alt+P 注册失败（多半被别的程序占用了）")
        except Exception as e:                                # noqa: BLE001
            print("[hotkey] 注册异常，已跳过：%s" % e)

    def nativeEventFilter(self, etype, message):
        if not self.ok:
            return False, 0
        try:
            if isinstance(etype, QtCore.QByteArray):
                etype = bytes(etype)
            if etype not in (b"windows_generic_MSG",
                             b"windows_dispatcher_MSG"):
                return False, 0
            from ctypes import wintypes
            msg = self._ctypes.cast(
                int(message), self._ctypes.POINTER(wintypes.MSG)).contents
            if msg.message == _WM_HOTKEY:
                self._cb()
                return True, 0
        except Exception:                                     # noqa: BLE001
            pass
        return False, 0

    def release(self):
        if not self.ok:
            return
        try:
            self._ctypes.windll.user32.UnregisterHotKey(
                self._ctypes.c_void_p(self._hwnd) if self._hwnd else None,
                _HOTKEY_ID)
        except Exception:                                     # noqa: BLE001
            pass
        self.ok = False


# ============================================================ 窗口
def _clear_caches():
    """清理桌宠的磁盘缓存，返回清理的项数。

    只动两类**本应用自己生成**的缓存（用户按「清理缓存」按钮显式触发）：
      1. 比例缓存   `%LOCALAPPDATA%\\YSMPet\\ratio_cache.json`
         （窗口尺寸测量结果，清掉后下次加载模型重新测量）
      2. 模型解密缓存 `<项目>/data/cache/` 下的 .bin/.meta
         （.ysm 解密结果，清掉后下次加载重新解密）
    """
    import shutil
    from pet import fastrender as _fr
    from pet import modelsource as _ms
    n = 0
    p = _fr._ratio_cache_path()
    if os.path.isfile(p):
        try:
            os.remove(p)
            n += 1
        except OSError:
            pass
    d = _ms.cache_dir()
    if os.path.isdir(d):
        for name in os.listdir(d):
            fp = os.path.join(d, name)
            if os.path.isfile(fp):
                try:
                    os.remove(fp)
                    n += 1
                except OSError:
                    pass
        try:
            shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass
    # 进程内的预解析缓存（若有）一并清掉
    try:
        from ysm import animation as _anim
        _anim.clear_prep_cache()
        n += 1
    except Exception:
        pass
    return n


def _model_label(path):
    """把模型来源（.ysm 文件 / 文件夹 / 压缩包）变成界面上显示的名字。

    ⚠️ 面板原来是从 `ClipLibrary` 上找 `source` / `name` 属性 —— 但
    `ClipLibrary.__init__(model, groups, min_bones)` **根本没有这两个属性**
    （它只有动画，不知道自己是哪个模型），于是 `getattr` 一路返回 None，
    「当前模型」永远显示占位符 `—`。

    真正的来源是 `PetWindow.model_key`（= 启动时解析出的模型路径）。
    """
    if not path:
        return ""
    s = str(path).rstrip("\\/")
    if not s:
        return ""
    b = os.path.basename(s)
    if not b:
        return s
    return os.path.splitext(b)[0] or b


class PetWindow(QtWidgets.QWidget):
    """无边框、置顶、逐像素透明的桌宠窗口。"""

    # 窗口高度的基准（scale=1 时）。宽度不硬编码 —— 由**模型渲染出来的
    # 长宽比**推出（见 _size）。340 是"够大但不占地方"的观感尺寸。
    BASE_H = 340
    # 横向留白：人物宽度 × 这个系数 = 窗口宽度。
    # 比例现在是**全动画全局包围盒**（不再是待机姿势），本身就含了最宽的
    # 动作，所以留白系数只需再给一点余量：1.25 实测最宽的动作(extra11)
    # 占宽约 62%，其余 25~40%，左右对称、且不裁切。
    FIG_W_PAD = 1.25
    # 兜底宽高比（拿不到模型跨度时，退回 2:3）
    FALLBACK_ASPECT = 2.0 / 3.0

    def __init__(self, renderer, animator, rig=None, scale=1.0, fps=30,
                 gaze=None, clips=None, model_key=None, parallel=False):
        super().__init__()
        self.rig = rig
        self.gaze = gaze
        self.clips = clips                  # ClipLibrary（可为 None）
        self.clip_player = None
        self.scale = scale
        self.model_key = model_key          # 比例缓存键（模型路径）
        self.model_label = _model_label(model_key)   # 面板上显示的模型名
        self.setWindowTitle("YSM Pet")
        self.setWindowFlags(QtCore.Qt.FramelessWindowHint
                            | QtCore.Qt.WindowStaysOnTopHint
                            | QtCore.Qt.Tool)
        # WA_TranslucentBackground 会让 Qt 在 Windows 上启用分层窗口（逐像素 alpha）。
        # 注意：Qt 设置它时会自动打开 WA_NoSystemBackground，千万别再手动关掉，
        # 否则窗口会退化成不透明背景。
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.renderer = renderer            # FastRenderer（_size 要用它量比例）
        self.animator = animator            # 量比例时推进几帧，看姿态变化

        # ⚠️ 常驻并行层必须**在建线程 / 量尺寸之前**建好，而且 `enabled` 也
        #    要在 `_size()` 之前定下来：
        #    `_size()` → `_aspect()` → `measure_ratio(..., resident=self.resident)`
        #    会看 `resident.available`，开着就按「带并行层」量（慧慧1.2 那种
        #    模型量出来会宽一倍、而且人还是被藏住的）。默认关闭：桌宠没有游戏
        #    内的 molang 变量，这些层算出来的是「隐藏身体」。
        from ysm.clips import ClipPlayer, ResidentPlayer
        if clips is not None:
            self.clip_player = ClipPlayer()
        self.resident = ResidentPlayer(clips)
        self.resident.enabled = bool(parallel)

        W, H = self._size()
        self.resize(W, H)
        self.img = None
        self._drag = None
        self._click_through = False

        if gaze is not None:
            # 视线锚点：先用窗口几何中心占位，_poll_gaze 每帧会用真实脸位置覆盖。
            # 初始朝向与 RenderThread 一致（读持久化设置，不再硬编码 28/10）。
            from pet import settings as _S
            _cfg0 = _S.load()
            _yaw0 = float(_cfg0.get("yaw", 90.0))
            _pitch0 = float(_cfg0.get("pitch", 0.0))
            gaze.set_geometry(
                self._gaze_anchor_for(renderer, gaze, W, H, _yaw0, _pitch0)
                or (W / 2.0, H / 2.0), (W, H))

        self.thread = RenderThread(renderer, animator, W, H, fps, rig=rig,
                                   gaze=gaze, clip_player=self.clip_player,
                                   resident=self.resident)
        self.thread.frame.connect(self.set_frame)
        self.thread.stat.connect(self._on_stat)
        self.thread.start()

        self.panel = None                   # 首次打开时才创建
        self.wheel = None                   # 环形动画轮盘（独立窗口）
        self._tray_set = False
        self._build_menu()
        self._build_tray()
        # 鼠标穿透的逃生舱：系统级热键 Ctrl+Alt+P（详见 GlobalHotkey 注释）
        self._hotkey = GlobalHotkey(self._flip_click_through)
        if self._hotkey.ok:
            QtWidgets.QApplication.instance().installNativeEventFilter(
                self._hotkey)

        # 视线追踪：全局轮询光标（桌宠窗口很小，光标大多在窗口外）
        self._poller = None
        if gaze is not None:
            from pet.gaze import CursorPoller
            self._poller = CursorPoller()
        self._gaze_timer = QtCore.QTimer(self)
        self._gaze_timer.setInterval(40)
        self._gaze_timer.timeout.connect(self._poll_gaze)
        self._gaze_timer.start()

    def _aspect(self):
        """人物**渲染后**的 宽/高 比（渲染器量好的，拿不到就兜底）。

        注意用 `measure_ratio` 而不是 `aspect`：后者是静态顶点的跨度，
        把 A-pose 张开的双臂也算进去了，会让窗口横向留白过多。

        `model_key` 是磁盘缓存的键（模型路径）：同一个模型第二次启动直接
        读缓存，省掉 2~3 s 的全动画扫描。
        """
        r = getattr(self, "renderer", None) or getattr(
            getattr(self, "thread", None), "r", None)
        if r is None:
            return self.FALLBACK_ASPECT
        try:
            a = float(r.measure_ratio(getattr(self, "animator", None),
                                      getattr(self, "clips", None),
                                      cache_key=getattr(self, "model_key", None),
                                      resident=getattr(self, "resident", None)))
        except Exception:                                     # noqa: BLE001
            a = float(getattr(r, "aspect", 0.0) or 0.0)
        if not (0.05 < a < 20.0):
            a = self.FALLBACK_ASPECT
        return a

    def _size(self):
        """按**模型自身的渲染长宽比**算窗口尺寸。

        与 zoom 的分工：
          * `zoom` 决定人物在窗口里**占多高**（相机远近）；
          * 本函数只决定窗口的**形状**（宽:高），让人物左右刚好卡住。

        推导：人物像素尺寸与窗口尺寸成正比（实测同 zoom 下
        200x300→75x170、400x600→149x339，比例恒为 0.44），所以
        "人物占窗高"是个常数、由 zoom 定死；这里只需按人物宽高比
        把窗口宽度收紧即可，不能凭空假设一个占高比例。
        """
        H = max(48, int(round(self.BASE_H * self.scale)))
        r = self._aspect()                       # 人物 宽/高
        # 窗口 宽/高 = 人物宽高比 × 横向留白系数
        W = max(48, int(round(H * r * self.FIG_W_PAD)))
        return W, H

    @staticmethod
    def _gaze_anchor_for(renderer, gaze, W, H, yaw, pitch):
        """脸/眼骨骼在桌宠窗口里的屏幕位置（窗口局部像素，与光标同坐标系）。

        视线中心应锚在**脸**而不是窗口几何中心（≈模型包围盒中心≈胸口）。
        之前锚在 (W/2, H/2)，于是"光标对准脸"时算出的 dx/dy 是相对胸口中心
        的，头会向上/偏着转 —— 用户看到的"像锚在了头颅左上角"。

        这里用渲染器同一套 view/projection 矩阵把骨骼的**可见 cube 质心**
        投影到窗口像素（见 FastRenderer.screen_pos_of_bone，已修 pivot 坑）。
        眼骨骼（如 Eyes）常常只是眼珠方块的父骨骼、自身没有 cube，无法定位
        可见位置 —— 此时退回投影头骨骼的质心（脸）。眼、头都取不到再返回 None。
        """
        if renderer is None or gaze is None:
            return None
        for bn in (gaze.eye, gaze.head):
            if not bn:
                continue
            pos = renderer.screen_pos_of_bone(bn, W, H, yaw, pitch)
            if pos is not None:
                return pos
        return None

    # ---------------------------------------------------------- 换模型
    def change_model(self):
        """弹选模型向导；选中了就热重载（不停桌宠窗口）。"""
        wiz = ModelWizard(self)
        if wiz.exec() != QtWidgets.QDialog.Accepted \
                or getattr(wiz, "result_data", None) is None:
            return
        self.apply_model(wiz.result_data, source=wiz.result_source)

    def apply_model(self, data, source=None):
        """用一批已加载好的模型数据替换当前桌宠（热重载）。

        data 来自 `pet.modelsource.load_source()`：含 model/tex/hide/raw。
        """
        from pet.fastrender import FastRenderer
        from pet.gaze import GazeTracker
        from ysm.clips import ClipLibrary
        from ysm.clips import ClipPlayer

        model, tex, hide = data["model"], data["tex"], data["hide"]
        # 「清晰模式」：把人物本体之外的骨骼并进 hide（特效/立绘/UI 部件）。
        # 放在这里而不是只放 load_source 里 —— 向导、内存数据、热重载三条
        # 路径都要走 apply_model，收口在这里才不会漏。
        hide = set(hide or ())
        if _hide_effects():
            from pet import modelsource as _ms
            hide |= _ms.hero_hide_bones(model)
        hide = sorted(hide)
        # 停旧线程、清旧轮盘
        self.thread.stop()
        if self.wheel is not None:
            self.wheel.close()
            self.wheel = None

        fr = FastRenderer(model, tex, hide_bones=hide)
        fr.lighting = bool(getattr(self.thread.r, "lighting", False))
        fr.ssaa = int(getattr(self.thread.r, "ssaa", 2))

        gaze = GazeTracker(model)
        clips = ClipLibrary(model)
        animator = IdleAnimator(model)
        animator.enabled = bool(getattr(self.thread.animator, "enabled", True))

        self.rig = None                      # 显隐表情已弃用
        self.gaze = gaze
        self.clips = clips
        self.clip_player = ClipPlayer()
        # 常驻并行层跟着换模型重新绑定（否则会拿旧模型的骨骼名去采样）
        if getattr(self, "resident", None) is None:
            from ysm.clips import ResidentPlayer
            self.resident = ResidentPlayer()
        self.resident.set_library(clips)

        # 换了模型 → 屏幕宽高比变了 → 按新模型重算窗口尺寸（否则会沿用
        # 旧模型的比例，出现"人物细长却套着宽窗口"）。
        self.renderer = fr
        self.model_key = source or ""        # 比例缓存键跟着换
        self.model_label = _model_label(source or (data or {}).get("src"))
        W, H = self._size()
        self.resize(W, H)
        # 视线锚点：眼/脸骨骼在窗口里的真实屏幕位置（不再锚在胸口中心）
        self.gaze.set_geometry(
            self._gaze_anchor_for(self.renderer, self.gaze, W, H,
                                  self.thread.yaw, self.thread.pitch)
            or (W / 2.0, H / 2.0), (W, H))
        self.thread = RenderThread(fr, animator, W, H,
                                   self.thread.fps, rig=None, gaze=gaze,
                                   clip_player=self.clip_player,
                                   resident=self.resident,
                                   yaw=self.thread.yaw, pitch=self.thread.pitch)
        self.thread.frame.connect(self.set_frame)
        self.thread.stat.connect(self._on_stat)
        self.thread.start()
        if self.wheel is not None:
            self._place_wheel()

        self._tray_set = False               # 重设托盘图标
        self._rebuild_menus()                # 动画列表要跟着新模型换
        self._sync_menu()

        name = (getattr(data["raw"], "metadata", None) or {}).get("name")
        print("换模型: %s  骨骼=%d  动画=%d"
              % (name or source, len(model.order), len(clips)))
        if source:
            from pet import settings as S
            S.update(model=source)
        return fr

    # ---------------------------------------------------------- 帧
    @QtCore.Slot(object)
    def set_frame(self, rgba):
        h, w = rgba.shape[:2]
        img = QtGui.QImage(rgba.data, w, h, w * 4,
                           QtGui.QImage.Format_RGBA8888).copy()
        self.img = img
        # 托盘图标用第一帧的模型剪影替换占位圆点
        if not self._tray_set:
            self._tray_set = True
            self.tray.setIcon(QtGui.QIcon(
                QtGui.QPixmap.fromImage(img).scaled(
                    64, 64, QtCore.Qt.KeepAspectRatio,
                    QtCore.Qt.SmoothTransformation)))
        self.update()

    @QtCore.Slot(float)
    def _on_stat(self, dt):
        self._last_ms = dt * 1000

    # ---------------------------------------------------------- 视线
    def _poll_gaze(self):
        """全局轮询光标 → 换算成窗口局部坐标 → 喂给 GazeTracker。

        Qt 的 mouseMoveEvent 只在光标进入桌宠窗口时触发，而桌宠只有
        一百多像素宽，光标绝大部分时间在窗口外，因此必须用全局坐标轮询。

        同时把**当前相机 yaw/pitch** 喂给 GazeTracker —— 头骨骼的旋转是
        在模型局部空间施加的，而相机是斜着看的，不做相机补偿会让转头在
        屏幕上"斜着歪"（用户报的"视线跟踪绑定错误"）。见 gaze._aim。
        """
        g = self.gaze
        if g is None or self._poller is None or not g.enabled:
            return
        pos = self._poller.global_pos()
        if pos is None:
            return
        top_left = self.mapToGlobal(QtCore.QPoint(0, 0))
        g.set_cursor(pos[0] - top_left.x(), pos[1] - top_left.y())
        g.set_camera(self.thread.yaw, self.thread.pitch)
        # 视线锚点跟着相机/窗口实时走：把眼骨骼投影到窗口像素，喂给 GazeTracker
        # 当参考中心。这样"光标对准脸"时头正好朝正前方（见 _gaze_anchor_for）。
        anchor = self._gaze_anchor_for(self.renderer, g, self.width(),
                                       self.height(), self.thread.yaw,
                                       self.thread.pitch)
        if anchor is not None:
            g.set_geometry(anchor, (self.width(), self.height()))

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        # Source 模式：先把整块（含 alpha）替换成全透明，清掉上一帧残留
        p.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
        p.fillRect(self.rect(), QtCore.Qt.transparent)
        if self.img is not None:
            # 再用 Source 贴模型，alpha=0 处直接透出桌面
            p.drawImage(0, 0, self.img)
        p.end()

    # ---------------------------------------------------------- 拖拽
    def mousePressEvent(self, e):
        if e.button() == QtCore.Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & QtCore.Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag = None

    def mouseDoubleClickEvent(self, e):
        """双击桌宠 = 打开设置面板（不用右键找菜单）。"""
        self.show_panel()

    def contextMenuEvent(self, e):
        self.menu.exec(e.globalPos())

    # ---------------------------------------------------------- 设置面板
    def show_panel(self):
        # 每次都按当前状态**重建**面板，而不是复用一个旧实例。
        # 因为菜单里也能改抗锯齿/帧率/缩放/朝向/视线等，复用的旧面板里
        # 控件值会与实际不符（滑块停旧位置）。重建一次的成本远低于
        # "两处入口状态漂移"带来的困惑。
        self._drop_panel()
        self.panel = SettingsPanel(self)
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()

    # ---------------------------------------------------------- 托盘
    def _build_tray(self):
        self.tray = QtWidgets.QSystemTrayIcon(self._placeholder_icon(), self)
        tm = QtWidgets.QMenu()
        tm.addAction("设置…").triggered.connect(self.show_panel)
        tm.addSeparator()
        # ⚠️ 托盘菜单**必须**有一个鼠标穿透开关。
        #    穿透开启后桌宠窗口收不到鼠标事件，右键菜单完全点不出来；
        #    设置面板一关，用户就只剩托盘这一条路。托盘图标是独立窗口，
        #    不受穿透影响，所以这里是"退出设置页之后"唯一的图形化出口。
        self.act_tray_thru = tm.addAction("鼠标穿透")
        self.act_tray_thru.setCheckable(True)
        self.act_tray_thru.setToolTip("勾上后桌宠点不到鼠标 / 拖不动\n"
                                      "关闭：再点这里，或按 Ctrl+Alt+P")
        self.act_tray_thru.triggered.connect(self.toggle_click_through)
        tm.addSeparator()
        tm.addAction("退出").triggered.connect(self.quit)
        self.tray.setContextMenu(tm)
        self.tray.activated.connect(
            lambda r: self.show_panel()
            if r in (QtWidgets.QSystemTrayIcon.Trigger,
                     QtWidgets.QSystemTrayIcon.DoubleClick) else None)
        self.tray.setToolTip("YSM 桌宠")
        self.tray.show()

    @staticmethod
    def _placeholder_icon():
        pm = QtGui.QPixmap(64, 64)
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setBrush(QtGui.QColor("#4C8DFF"))
        p.setPen(QtCore.Qt.NoPen)
        p.drawEllipse(4, 4, 56, 56)
        p.end()
        return QtGui.QIcon(pm)

    # ---------------------------------------------------------- 菜单
    def _rebuild_menus(self):
        """换模型后重建右键菜单（动画列表变了）。

        旧 menu 必须显式 deleteLater —— 每次都 new 一个 QMenu(self) 挂到
        父对象上，不释放就会随换模型次数累积。
        """
        old = getattr(self, "menu", None)
        if old is not None:
            old.deleteLater()
            self.menu = None
        self._build_menu()

    def _build_menu(self):
        m = QtWidgets.QMenu(self)
        self.menu = m
        self._fill_menu(m, refs=True)
        # 面板里改过参数后，菜单勾选要跟着实际状态走
        m.aboutToShow.connect(self._sync_menu)
        return m

    def _fill_menu(self, m, refs=True):
        """把全部菜单项填进 `m`。

        refs=True 时把可勾选 action 存到 self.act_*（供 _sync_menu 同步勾选
        状态）；托盘菜单传 refs=False —— 它与右键菜单同时存在，两边都存
        引用会互相覆盖，托盘那份只需要"能点"，不需要勾选同步。
        """
        m.addAction("设置…").triggered.connect(self.show_panel)
        m.addAction("更换模型…").triggered.connect(self.change_model)
        m.addSeparator()

        a_top = m.addAction("置顶")
        a_top.setCheckable(True)
        a_top.setChecked(True)
        a_top.triggered.connect(self.toggle_top)

        a_thru = m.addAction("鼠标穿透")
        a_thru.setCheckable(True)
        a_thru.setToolTip("勾上后桌宠点不到鼠标 / 拖不动 / 右键也点不出来\n"
                          "关闭：托盘图标 → 鼠标穿透，或按 Ctrl+Alt+P")
        a_thru.triggered.connect(self.toggle_click_through)

        m.addSeparator()
        # 模型自带动画轮盘（环绕桌宠，矩形块）
        a_wheel = m.addAction("动画轮盘")
        a_wheel.setCheckable(True)
        a_wheel.setEnabled(self.clips is not None and len(self.clips) > 0)
        a_wheel.triggered.connect(self.toggle_wheel)

        m.addSeparator()
        # 注：光影 / 程序化待机 / 并行动画层 / 自动旋转 / 视线跟踪 已**缩合进
        # 设置面板**（显示 / 动画 / 朝向 / 视线 各页），这里不再单列；表情
        # 动作也收进了动画轮盘（见 ClipWheel）。右键菜单常驻项因此大幅减少。

        # 窗口大小
        zsub = m.addMenu("窗口大小")
        zgrp = QtGui.QActionGroup(zsub)
        for s in (0.6, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0):
            a = zsub.addAction("%gx" % s)
            a.setCheckable(True)
            a.setChecked(abs(s - self.scale) < 1e-6)
            a.triggered.connect(lambda _c, s=s: self.set_scale(s))
            zgrp.addAction(a)

        # 抗锯齿
        aa = m.addMenu("抗锯齿")
        agr = QtGui.QActionGroup(aa)
        for txt, val in (("关", 1), ("2x", 2), ("3x", 3)):
            a = aa.addAction(txt)
            a.setCheckable(True)
            a.setChecked(int(getattr(self.thread.r, "ssaa", 2)) == val)
            a.triggered.connect(
                lambda _c, v=val: setattr(self.thread.r, "ssaa", v))
            agr.addAction(a)

        # 帧率
        fsub = m.addMenu("帧率")
        fgr = QtGui.QActionGroup(fsub)
        for v in (15, 24, 30, 45, 60, 90):
            a = fsub.addAction("%d fps" % v)
            a.setCheckable(True)
            a.setChecked(int(self.thread.fps) == v)
            a.triggered.connect(
                lambda _c, v=v: setattr(self.thread, "fps", v))
            fgr.addAction(a)

        # 朝向复位
        m.addAction("朝向复位").triggered.connect(self._reset_orient)

        m.addSeparator()
        m.addAction("退出").triggered.connect(self.quit)

        if refs:
            self.act_top, self.act_thru = a_top, a_thru
            self.act_wheel = a_wheel
            # 已缩合进设置面板 / 轮盘的项，菜单里不再持有引用
            self.act_light = self.act_idle = None
            self.act_par = None
            self.act_spin = self.act_gaze = None

    def _sync_menu(self):
        th = self.thread
        self.act_top.setChecked(
            bool(self.windowFlags() & QtCore.Qt.WindowStaysOnTopHint))
        self._sync_click_through()
        # 以下项已缩合进设置面板，菜单里不再持有引用（None 即跳过同步）
        if getattr(self, "act_light", None) is not None:
            self.act_light.setChecked(bool(th.r.lighting))
        if getattr(self, "act_idle", None) is not None:
            self.act_idle.setChecked(th.animator.enabled)
        par = getattr(self, "act_par", None)
        if par is not None:
            res = getattr(self, "resident", None)
            par.setEnabled(res is not None and res.available)
            par.setChecked(bool(res is not None and res.enabled
                                and res.available))
        if getattr(self, "act_spin", None) is not None:
            self.act_spin.setChecked(th.spin != 0)
        if getattr(self, "act_gaze", None) is not None:
            self.act_gaze.setChecked(self.gaze is not None
                                     and self.gaze.enabled)
        self.act_wheel.setChecked(self.wheel is not None
                                  and self.wheel.isVisible())

    def _reset_orient(self):
        """朝向复位（与面板里的「复位」一致：读持久化默认，yaw=90/pitch=0）。"""
        from pet import settings as S
        cfg = S.load()
        self.thread.yaw = float(cfg.get("yaw", 90.0))
        self.thread.pitch = float(cfg.get("pitch", 0.0))
        # 面板是「建一次就复用」的，里面的滑块值还停在旧朝向。
        # 直接丢弃旧面板，下次打开会按当前值重建，避免显示与实际不符。
        self._drop_panel()

    def _drop_panel(self):
        """销毁缓存的面板，使下次 show_panel() 按当前状态重建。"""
        if self.panel is not None:
            self.panel.close()
            self.panel.deleteLater()
            self.panel = None

    def set_scale(self, s):
        self.scale = s
        W, H = self._size()
        self.resize(W, H)
        self.thread.set_size(W, H)
        if self.gaze is not None:
            self.gaze.set_geometry(
                self._gaze_anchor_for(self.renderer, self.gaze, W, H,
                                      self.thread.yaw, self.thread.pitch)
                or (W / 2.0, H / 2.0), (W, H))
        if self.wheel is not None:
            self._place_wheel()

    def toggle_top(self, on):
        f = self.windowFlags()
        f = f | QtCore.Qt.WindowStaysOnTopHint if on else \
            f & ~QtCore.Qt.WindowStaysOnTopHint
        self.setWindowFlags(f)
        self.show()

    def toggle_click_through(self, on):
        """开 / 关鼠标穿透（桌宠收不到鼠标事件 → 点不到也拖不动）。

        三个入口都汇到这里：右键菜单、托盘菜单、设置面板复选框，
        外加全局热键 Ctrl+Alt+P。
        开启后**唯一**还能点的就是托盘图标菜单和热键本身 —— 因为穿透只
        作用在桌宠窗口上，托盘图标是独立的。
        """
        on = bool(on)
        self._click_through = on
        f = self.windowFlags()
        if on:
            f |= QtCore.Qt.WindowTransparentForInput
        else:
            f &= ~QtCore.Qt.WindowTransparentForInput
        self.setWindowFlags(f)
        self.show()
        self._sync_click_through()

    def _sync_click_through(self):
        """把穿透状态同步到所有开关上（菜单勾选 / 托盘勾选 / 托盘提示）。"""
        for owner, attr in ((self, "act_thru"),
                            (self, "act_tray_thru")):
            a = getattr(owner, attr, None)
            if a is not None:
                a.setChecked(self._click_through)
        try:
            self.tray.setToolTip(
                "YSM 桌宠（鼠标穿透中：Ctrl+Alt+P 或托盘菜单可关闭）"
                if self._click_through else "YSM 桌宠")
        except Exception:                                     # noqa: BLE001
            pass

    def _flip_click_through(self):
        """全局热键 Ctrl+Alt+P 的落点：翻转当前穿透状态。"""
        self.toggle_click_through(not self._click_through)

    # ---------------------------------------------------------- 待机 / 并行层
    def toggle_idle(self, on):
        """程序化待机（呼吸/摆头）。手动开关后不再自动决定。"""
        self.thread.animator.enabled = bool(on)
        try:
            from pet import settings as S
            S.update(idle_enabled=bool(on), idle_auto=False)
        except Exception:                                     # noqa: BLE001
            pass

    def toggle_parallel(self, on):
        """常驻并行层（原版 YSM 的 pre_parallel / parallel）。

        关掉后头发/裙摆等「会自己动」的部件会固定在 rest 姿势。
        """
        if getattr(self, "resident", None) is None:
            return
        self.resident.enabled = bool(on)
        try:
            from pet import settings as S
            S.update(parallel_enabled=bool(on))
        except Exception:                                     # noqa: BLE001
            pass

    def toggle_effects(self, on):
        """「清晰模式」：只画人物本体（隐藏特效 / 立绘 / 游戏内 UI 部件）。

        hide 集合是在 `FastRenderer` 构造时定下的，所以必须**重载模型**才生效。
        """
        try:
            from pet import settings as S
            S.update(show_effects=not bool(on))
        except Exception:                                     # noqa: BLE001
            pass
        self.reload_model()

    def reload_model(self):
        """按当前设置重新加载当前模型（重建渲染器）。

        用于 hide 集合 / 取景口径变化后热生效（如「清晰模式」开关）。
        """
        src = getattr(self, "model_key", "") or ""
        if not src:
            try:
                from pet import settings as S
                src = S.load().get("model") or ""
            except Exception:                                 # noqa: BLE001
                src = ""
        if not src:
            return
        from pet import modelsource as MS
        try:
            data = MS.load_source(src, hide_effects=_hide_effects())
        except Exception as exc:                              # noqa: BLE001
            print("重载模型失败：%s" % exc)
            return
        self.apply_model(data, source=src)

    # ---------------------------------------------------------- 视线
    def toggle_gaze(self, on):
        if self.gaze is None:
            return
        self.gaze.enabled = bool(on)
        if not on:
            self.gaze.reset()

    # ---------------------------------------------------------- 动画轮盘
    def toggle_wheel(self, on):
        if self.clips is None or len(self.clips) == 0:
            return
        if on:
            if self.wheel is None:
                self.wheel = ClipWheel(self)
                # 轮盘是懒创建的：刚建出来时把「当前正在播的动画」同步进去，
                # 否则高亮态和实际播放的会对不上。
                cur = (self.clip_player.clip.name
                       if self.clip_player is not None
                       and self.clip_player.clip is not None else None)
                self.wheel.current = cur
            self._place_wheel()
            self.wheel.show()
            self.wheel.raise_()
        elif self.wheel is not None:
            self.wheel.hide()

    def _place_wheel(self):
        """把环形轮盘居中贴到桌宠四周（比桌宠大一圈）。"""
        if self.wheel is None:
            return
        w = self.wheel
        w.refresh_size()
        gw = self.frameGeometry()
        w.move(gw.center().x() - w.width() // 2,
               gw.center().y() - w.height() // 2)

    def moveEvent(self, e):
        super().moveEvent(e)
        if self.wheel is not None and self.wheel.isVisible():
            self._place_wheel()

    def play_clip(self, name):
        """播放模型自带动画；None/空 = 回到待机。"""
        if self.clip_player is None:
            return
        if not name:
            self.clip_player.stop()
            return
        c = self.clips.get(name)
        if c is not None:
            self.clip_player.play(c, time.time())

    def quit(self):
        """关闭键：停渲染线程 → 收托盘 → 退出。"""
        try:
            self.tray.hide()
        except Exception:                                    # noqa: BLE001
            pass
        try:
            hk = getattr(self, "_hotkey", None)
            if hk is not None:
                QtWidgets.QApplication.instance().removeNativeEventFilter(hk)
                hk.release()
        except Exception:                                    # noqa: BLE001
            pass
        if self.panel is not None:
            self.panel.close()
        if self.wheel is not None:
            self.wheel.close()
        self.thread.stop()
        QtWidgets.QApplication.quit()

    def closeEvent(self, e):
        try:
            self.tray.hide()
        except Exception:                                    # noqa: BLE001
            pass
        try:
            hk = getattr(self, "_hotkey", None)
            if hk is not None:
                QtWidgets.QApplication.instance().removeNativeEventFilter(hk)
                hk.release()
        except Exception:                                    # noqa: BLE001
            pass
        if self.panel is not None:
            self.panel.close()
        if self.wheel is not None:
            self.wheel.close()
        self.thread.stop()
        super().closeEvent(e)


# ============================================================ 设置面板
# 面板样式：现代深色 + 左侧导航。单独提出来是因为字符串太长，塞在类里
# 会把 __init__ 淹掉；也方便和浅色主题对比调整。
_SETTINGS_QSS = """
    QWidget#root { background: #1a1d23; }
    QWidget { font-size: 13px; color: #d7dbe4; }
    #content { background: #1a1d23; }
    #nav {
        background: #16181d;
        border: none;
        border-right: 1px solid #2c3038;
        outline: none;
        padding: 12px 8px;
    }
    #nav::item {
        color: #9aa4b2;
        border-radius: 7px;
        padding-left: 12px;
        margin: 2px 2px;
    }
    #nav::item:hover { background: #242932; color: #cfd6e0; }
    #nav::item:selected { background: #2f6fe0; color: #ffffff; font-weight: 600; }
    #card {
        background: #23262e;
        border: 1px solid #2f333d;
        border-radius: 10px;
    }
    #cardTitle {
        color: #9fb3d1;
        font-weight: 600;
        font-size: 13px;
    }
    #header { background: transparent; }
    #headerTitle { font-weight: 700; color: #eef1f6; }
    #subtitle { color: #8b95a3; }
    #value { color: #e6e9f0; font-weight: 600; }
    QLabel { background: transparent; }
    QCheckBox { spacing: 8px; background: transparent; }
    QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px;
        border: 1px solid #4a515f; background: #2c3038; }
    QCheckBox::indicator:checked { background: #2f6fe0; border-color: #2f6fe0; }
    QComboBox, QSpinBox, QDoubleSpinBox {
        background: #2c3038; color: #e6e9f0;
        border: 1px solid #454b58; border-radius: 6px;
        padding: 5px 9px; min-height: 22px;
    }
    QComboBox::drop-down { border: none; width: 22px; }
    QComboBox QAbstractItemView {
        background: #2c3038; color: #e6e9f0;
        border: 1px solid #454b58;
        selection-background-color: #2f6fe0;
    }
    QSlider::groove:horizontal {
        height: 5px; background: #3a3f4b; border-radius: 3px;
    }
    QSlider::sub-page:horizontal { background: #2f6fe0; border-radius: 3px; }
    QSlider::handle:horizontal {
        width: 15px; height: 15px; margin: -6px 0;
        border-radius: 8px; background: #6ea8ff;
    }
    QSlider::handle:horizontal:hover { background: #86b8ff; }
    QPushButton {
        background: #333845; color: #e6e9f0;
        border: 1px solid #454b58; border-radius: 6px;
        padding: 7px 16px;
    }
    QPushButton:hover { background: #3d4453; border-color: #566073; }
    QPushButton:pressed { background: #2b303b; }
    QPushButton#danger {
        background: #c9453f; border-color: #c9453f; color: white;
        font-weight: 600;
    }
    QPushButton#danger:hover { background: #d9554f; border-color: #d9554f; }
    QScrollArea { border: none; background: transparent; }
"""


class SettingsPanel(QtWidgets.QWidget):
    """桌宠设置面板：显示 / 朝向 / 动画 / 表情轮盘 / 窗口。

    唤出方式三种：右键菜单「设置…」、双击桌宠、托盘图标单击。
    """

    def __init__(self, pet):
        super().__init__()
        self.pet = pet
        self.setWindowTitle("YSM 桌宠 · 设置")
        # ⚠️ 必须带 Qt.Window！
        # 只写 Qt.WindowStaysOnTopHint 的话，这个 widget 没有 Window 位，
        # Qt 会把它当成**桌宠的子部件**：点面板的 × 会连带把桌宠一起关掉
        # （用户实测："设置窗口和桌宠窗口绑定了"）。加上 Qt.Window 才是
        # 独立顶层窗口，× 只关自己。
        self.setWindowFlags(QtCore.Qt.Window
                            | QtCore.Qt.WindowStaysOnTopHint
                            | QtCore.Qt.WindowCloseButtonHint
                            | QtCore.Qt.WindowMinimizeButtonHint)
        self.setMinimumSize(720, 480)
        self.setObjectName("root")               # QSS 靠它给整窗深色底
        self.setStyleSheet(_SETTINGS_QSS)
        # 明确声明不透明背景：否则 Qt 可能让内容区露默认（浅色）调色板，
        # 只有设了 QSS background 的部件才是深的 —— 那样会出现"半浅半深"。
        self.setAutoFillBackground(True)

        # ---- 整体：左导航栏 + 右侧分页内容 + 底部操作条
        # 原来是一整屏两列堆叠 → 内容一多就得拉长（用户："过窄过长"）。
        # 改成**左侧导航 + 右侧分页**：每页内容都短，窗口恒定大小，
        # 一屏看完，不用滚。
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._nav = QtWidgets.QListWidget()
        self._nav.setObjectName("nav")
        self._nav.setFixedWidth(150)
        self._nav.setFocusPolicy(QtCore.Qt.NoFocus)
        self._nav.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

        content = QtWidgets.QWidget()
        content.setObjectName("content")
        right = QtWidgets.QVBoxLayout(content)
        right.setContentsMargins(20, 16, 20, 14)
        right.setSpacing(14)
        right.addWidget(self._title_block())

        self._stack = QtWidgets.QStackedWidget()
        right.addWidget(self._stack, 1)
        right.addWidget(self._footer())

        root.addWidget(self._nav)
        root.addWidget(content, 1)

        # ---- 组装页面（顺序即导航顺序）
        pages = [
            ("显示", "显示", self._page_display()),
            ("朝向", "朝向", self._page_orient()),
            ("动画", "动画", self._page_anim()),
            ("视线", "视线", self._page_gaze()),
            ("窗口", "窗口", self._page_window()),
        ]
        for _key, name, page in pages:
            if page is None:
                continue
            self._stack.addWidget(page)
            it = QtWidgets.QListWidgetItem(name)
            it.setSizeHint(QtCore.QSize(0, 40))
            self._nav.addItem(it)
        self._nav.currentRowChanged.connect(self._stack.setCurrentIndex)
        self._nav.setCurrentRow(0)

        self.resize(680, 460)

    # ---------------------------------------------------------- 顶部标题
    def _title_block(self):
        w = QtWidgets.QWidget()
        w.setObjectName("header")
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(3)
        t = QtWidgets.QLabel("设置")
        t.setObjectName("headerTitle")
        f = t.font()
        f.setPointSize(f.pointSize() + 5)
        f.setBold(True)
        t.setFont(f)
        v.addWidget(t)

        name = ""
        # 名字来源按可靠性排序：桌宠记着的模型路径 → 设置里记住的模型
        for cand in (getattr(self.pet, "model_label", ""),
                     _model_label(getattr(self.pet, "model_key", ""))):
            if cand:
                name = cand
                break
        if not name:
            try:
                from pet import settings as S
                name = _model_label(S.load().get("model"))
            except Exception:                                 # noqa: BLE001
                pass
        lab = QtWidgets.QLabel("当前模型 · " + (name or "（未命名）"))
        lab.setObjectName("subtitle")
        v.addWidget(lab)
        return w

    # ---------------------------------------------------------- 分组容器
    def _box(self, title):
        """一个设置卡片：标题 + 表单。返回 (卡片 widget, form layout)。"""
        card = QtWidgets.QFrame()
        card.setObjectName("card")
        v = QtWidgets.QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 14)
        v.setSpacing(8)
        if title:
            t = QtWidgets.QLabel(title)
            t.setObjectName("cardTitle")
            v.addWidget(t)
        body = QtWidgets.QWidget()
        f = QtWidgets.QFormLayout(body)
        f.setContentsMargins(0, 0, 0, 0)
        f.setLabelAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        f.setVerticalSpacing(9)
        f.setHorizontalSpacing(14)
        f.setFieldGrowthPolicy(QtWidgets.QFormLayout.ExpandingFieldsGrow)
        v.addWidget(body)
        return card, f

    def _page(self, *cards):
        """把若干卡片竖排成一页（内容少，通常不需要滚动）。"""
        page = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)
        for c in cards:
            v.addWidget(c)
        v.addStretch(1)
        return page

    # ---------------------------------------------------------- 页面：显示
    def _page_display(self):
        p, th = self.pet, self.pet.thread

        card, f = self._box("画面")
        c = QtWidgets.QCheckBox("光影（默认关闭）")
        c.setChecked(bool(th.r.lighting))
        c.toggled.connect(lambda v: setattr(th.r, "lighting", v))
        f.addRow("明暗", c)

        aa = QtWidgets.QComboBox()
        aa.addItems(["关", "2x", "3x"])
        aa.setCurrentText("关" if th.r.ssaa < 2 else "%dx" % int(th.r.ssaa))
        aa.setToolTip("SSAA 超采样：按倍数渲染再下采样，消除轮廓锯齿")
        aa.currentTextChanged.connect(
            lambda s: setattr(th.r, "ssaa", 1 if s == "关" else int(s[0])))
        f.addRow("抗锯齿", aa)

        fps = QtWidgets.QSpinBox()
        fps.setRange(5, 120)
        fps.setValue(th.fps)
        fps.valueChanged.connect(lambda v: setattr(th, "fps", v))
        f.addRow("目标帧率", fps)

        card2, f2 = self._box("窗口大小")
        # 滑块连续调，右边实时显示换算后的像素尺寸。
        z = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        z.setRange(10, 60)                       # 0.5x ~ 3.0x，步进 0.05
        z.setValue(int(round(self.pet.scale * 20)))
        zsz = QtWidgets.QLabel()
        zsz.setObjectName("value")

        def _apply_scale(v, do_resize=True):
            s = max(0.5, v / 20.0)
            if do_resize:
                self.pet.set_scale(s)
            W, H = self.pet._size()
            zsz.setText("%gx · %d × %d px" % (s, W, H))
        z.valueChanged.connect(lambda v: _apply_scale(v))
        _apply_scale(z.value(), do_resize=False)

        zr = QtWidgets.QPushButton("复位")
        zr.setFixedWidth(60)
        zr.clicked.connect(lambda: z.setValue(20))            # 1.0x

        zrow = QtWidgets.QWidget()
        zh = QtWidgets.QHBoxLayout(zrow)
        zh.setContentsMargins(0, 0, 0, 0)
        zh.setSpacing(8)
        zh.addWidget(z, 1)
        zh.addWidget(zsz)
        zh.addWidget(zr)
        f2.addRow("缩放", zrow)
        return self._page(card, card2)

    # ---------------------------------------------------------- 页面：朝向
    def _page_orient(self):
        from pet import settings as S
        th = self.pet.thread
        card, f = self._box("相机朝向")

        yw = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        yw.setRange(-180, 180)
        yw.setValue(int(th.yaw) % 360 if int(th.yaw) < -180 else int(th.yaw))
        yl = QtWidgets.QLabel(str(int(th.yaw)))
        yl.setObjectName("value")
        yw.valueChanged.connect(
            lambda v: (setattr(th, "yaw", float(v)), yl.setText(str(v))))
        yw.sliderReleased.connect(
            lambda: S.update(yaw=float(th.yaw)))
        f.addRow("水平 yaw", self._slider_row(yw, yl))

        pt = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        pt.setRange(-60, 60)
        pt.setValue(int(th.pitch))
        pl = QtWidgets.QLabel(str(int(th.pitch)))
        pl.setObjectName("value")
        pt.valueChanged.connect(
            lambda v: (setattr(th, "pitch", float(v)), pl.setText(str(v))))
        pt.sliderReleased.connect(
            lambda: S.update(pitch=float(th.pitch)))
        f.addRow("俯仰 pitch", self._slider_row(pt, pl))

        def reset():
            _cfg = S.load()
            _y, _p = float(_cfg.get("yaw", 90.0)), float(_cfg.get("pitch", 0.0))
            th.yaw, th.pitch = _y, _p
            yw.setValue(int(_y))
            pt.setValue(int(_p))
            yl.setText(str(int(_y)))
            pl.setText(str(int(_p)))
        rb = QtWidgets.QPushButton("复位到默认视角")
        rb.clicked.connect(reset)
        f.addRow("", rb)

        card2, f2 = self._box("缓存")
        cache_hint = QtWidgets.QLabel(
            "清理比例缓存与模型解密缓存；下次加载模型时会自动重新测量。")
        cache_hint.setWordWrap(True)
        f2.addRow("", cache_hint)

        def _do_clear():
            n = _clear_caches()
            cache_hint.setText("已清理 %d 项缓存。" % n)
        cb = QtWidgets.QPushButton("清理缓存")
        cb.clicked.connect(_do_clear)
        f2.addRow("", cb)

        card2, f2 = self._box("旋转")
        sp = QtWidgets.QCheckBox("启用自动旋转")
        sp.setChecked(th.spin != 0)
        sd = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        sd.setRange(0, 90)
        sd.setValue(int(th.spin) or 25)
        sd.valueChanged.connect(
            lambda v: setattr(th, "spin", float(v) if sp.isChecked() else 0.0))
        sp.toggled.connect(
            lambda v: setattr(th, "spin", float(sd.value()) if v else 0.0))
        f2.addRow("", sp)
        sl = QtWidgets.QLabel(str(int(th.spin) or 25) + " °/s")
        sl.setObjectName("value")
        sd.valueChanged.connect(lambda v: sl.setText("%d °/s" % v))
        f2.addRow("转速", self._slider_row(sd, sl))
        return self._page(card, card2)

    # ---------------------------------------------------------- 页面：动画
    def _page_anim(self):
        card, f = self._box("待机")
        c = QtWidgets.QCheckBox("程序化呼吸 / 摆头（模型无自带动画时的兜底）")
        c.setChecked(self.pet.thread.animator.enabled)
        c.setToolTip("模型自带 idle / parallel 时默认不叠这层，直接放模型自己的动画")
        c.toggled.connect(self.pet.toggle_idle)
        f.addRow("", c)

        res = getattr(self.pet, "resident", None)
        if res is not None and res.available:
            p = QtWidgets.QCheckBox("并行动画层（头发 / 裙摆飘动）")
            p.setChecked(res.enabled)
            p.setToolTip("原版 YSM 的 pre_parallel / parallel 层，共 %d 层：\n%s"
                         % (res.n_layers, "、".join(res.names())))
            p.toggled.connect(self.pet.toggle_parallel)
            f.addRow("", p)

        # 「清晰模式」：只画人物本体。YSM 模型常把特效/立绘/游戏内 UI 拆成
        # 独立根骨骼子树，游戏里靠 molang 按需显示；桌宠没有状态机，全画
        # 出来就叠在人物脸上，所以默认只画本体。
        ce = QtWidgets.QCheckBox("只画人物本体（隐藏特效 / 立绘 / UI 部件）")
        try:
            from pet import settings as _S
            ce.setChecked(not bool(_S.load().get("show_effects", False)))
        except Exception:                                     # noqa: BLE001
            ce.setChecked(True)
        ce.setToolTip("YSM 模型里的人本体之外还打包了特效刀光/法阵、平面立绘、\n"
                      "游戏内 UI（转盘/骰子/背景板）等部件，游戏里由 molang\n"
                      "按状态显示。桌宠没有那套状态机 → 全画出来会叠在人物身上\n"
                      "（「人物、特效叠在一起，看不清」）。\n"
                      "勾上 = 只画人物本体（默认）；取消 = 全部部件照画。\n"
                      "改动会重新加载模型（几秒）。")
        ce.toggled.connect(self.pet.toggle_effects)
        f.addRow("", ce)

        lib = self.pet.clips
        if lib is None or len(lib) == 0:
            return self._page(card)

        card2, f2 = self._box("模型自带动画")
        cb = QtWidgets.QComboBox()
        cb.addItem("（待机）", None)
        for name in lib.names():
            cl = lib.get(name)
            if cl is None or not cl.playable:
                continue
            cb.addItem("%s  ·  %s" % (cl.label(), cl.duration_text()), name)
        cb.setToolTip("来自 .ysm 的 main 组动画；选「待机」回到呼吸动画")

        def on_pick(idx):
            self.pet.play_clip(cb.itemData(idx))
        cb.currentIndexChanged.connect(on_pick)
        f2.addRow("播放", cb)

        w = QtWidgets.QCheckBox("环形轮盘（环绕桌宠）")
        w.setChecked(self.pet.wheel is not None and self.pet.wheel.isVisible())
        w.toggled.connect(self.pet.toggle_wheel)
        f2.addRow("", w)
        return self._page(card, card2)

    # ---------------------------------------------------------- 页面：视线
    def _page_gaze(self):
        gt = self.pet.gaze
        if gt is None or not gt.available:
            card, f = self._box("视线追踪")
            lab = QtWidgets.QLabel("当前模型无头骨骼，不可用")
            lab.setObjectName("subtitle")
            f.addRow("", lab)
            return self._page(card)

        card, f = self._box("视线追踪")
        en = QtWidgets.QCheckBox("启用（头跟随鼠标转头）")
        en.setChecked(bool(gt.enabled))
        en.setToolTip("默认关闭。开启后桌宠会像 MC 背包里那样转头看向光标\n"
                      "注：眼睛只用来算方向，不单独转动（MC 皮肤的眼睛是贴图）")

        def on_tog(v):
            self.pet.toggle_gaze(v)
        en.toggled.connect(on_tog)
        f.addRow("", en)

        sm = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        sm.setRange(1, 20)
        sm.setValue(int(gt.cfg["smooth"]))
        sm.valueChanged.connect(lambda v: gt.cfg.__setitem__("smooth", float(v)))
        sml = QtWidgets.QLabel(str(int(gt.cfg["smooth"])))
        sml.setObjectName("value")
        sm.valueChanged.connect(lambda v: sml.setText(str(v)))
        f.addRow("跟手程度", self._slider_row(sm, sml))

        yaw = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        yaw.setRange(10, 90)
        yaw.setValue(int(gt.cfg["max_yaw"]))
        yaw.valueChanged.connect(
            lambda v: gt.cfg.__setitem__("max_yaw", float(v)))
        yawl = QtWidgets.QLabel("%d°" % int(gt.cfg["max_yaw"]))
        yawl.setObjectName("value")
        yaw.valueChanged.connect(lambda v: yawl.setText("%d°" % v))
        f.addRow("左右摆幅", self._slider_row(yaw, yawl))

        # 正面朝向：模型正面在相机 yaw 多少度时正对镜头。决定「光标在正中」
        # 时头预转多少。默认「自动」——用眼骨骼位置推断（换模型不用手调）。
        auto = gt.auto_front_yaw
        auto_txt = ("自动 · %d°" % int(auto)) if auto is not None else "自动 · 默认 90°"
        fy = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        fy.setRange(0, 359)
        fy.setValue(int(round(gt.front_yaw())))
        fy.setToolTip("模型正面在相机 yaw 多少度时正对镜头。\n"
                      "决定光标在正中时头预转多少。\n"
                      "默认自动推断（眼骨骼位置）；头总偏向一边时手动调。")
        fyl = QtWidgets.QLabel("%d°" % int(round(gt.front_yaw())))
        fyl.setObjectName("value")
        f.addRow("正面朝向", self._slider_row(fy, fyl))
        aut = QtWidgets.QCheckBox("自动（按眼骨骼位置推断，推荐）")
        aut.setChecked(gt.cfg.get("front_yaw") is None)
        aut.setToolTip("勾选后用推断值：%s\n取消勾选才用手动值。" % auto_txt)

        def on_auto(v):
            fy.setEnabled(not v)
            if v:
                gt.cfg["front_yaw"] = None
                fyl.setText(auto_txt)
            else:
                gt.cfg["front_yaw"] = float(fy.value())
                fyl.setText("%d°" % fy.value())
        aut.toggled.connect(on_auto)

        def on_fy(v):
            if aut.isChecked():
                aut.setChecked(False)          # 拖动即转为手动
            gt.cfg["front_yaw"] = float(v)
            fyl.setText("%d°" % v)
        fy.valueChanged.connect(on_fy)
        f.addRow("", aut)
        fy.setEnabled(not aut.isChecked())
        if aut.isChecked():
            fyl.setText(auto_txt)

        card2, f2 = self._box("骨骼绑定")
        hb = QtWidgets.QLabel(str(gt.head or "—"))
        hb.setObjectName("value")
        eb = QtWidgets.QLabel(str(gt.eye or "—"))
        eb.setObjectName("value")
        f2.addRow("头骨骼", hb)
        f2.addRow("眼骨骼", eb)
        note = QtWidgets.QLabel("眼球仅用于计算朝向，不参与变形")
        note.setObjectName("subtitle")
        f2.addRow("", note)
        return self._page(card, card2)

    # ---------------------------------------------------------- 页面：窗口
    def _page_window(self):
        card, f = self._box("窗口行为")
        t = QtWidgets.QCheckBox("置顶显示")
        t.setChecked(bool(self.pet.windowFlags() & QtCore.Qt.WindowStaysOnTopHint))
        t.toggled.connect(self.pet.toggle_top)
        f.addRow("", t)

        c = QtWidgets.QCheckBox("鼠标穿透（点不到桌宠）")
        c.setChecked(self.pet._click_through)
        c.toggled.connect(self.pet.toggle_click_through)
        f.addRow("", c)
        # 开启后桌宠本身收不到鼠标事件，右键菜单也点不出来 ——
        # 必须在这里就把"怎么关"讲清楚，否则用户会以为关不掉了。
        hint = QtWidgets.QLabel(
            "开启后桌宠点不到、拖不动，也无法右键。\n"
            "关闭方式（三种任选）：\n"
            "  · 取消勾选本项\n"
            "  · 托盘图标 → 右键 → 「鼠标穿透」\n"
            "  · 按快捷键 Ctrl+Alt+P")
        hint.setObjectName("subtitle")
        hint.setWordWrap(True)
        f.addRow("", hint)

        card2, f2 = self._box("尺寸说明")
        note = QtWidgets.QLabel(
            "窗口宽高比 = 桌宠所有动作渲染后的最大 x / 最大 y。\n"
            "所以任何动作、任何朝向都不会被窗口裁到。")
        note.setObjectName("subtitle")
        note.setWordWrap(True)
        f2.addRow("", note)
        return self._page(card, card2)

    # ---------------------------------------------------------- 底部操作
    def _footer(self):
        w = QtWidgets.QWidget()
        w.setObjectName("footer")
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        change = QtWidgets.QPushButton("更换模型…")
        change.clicked.connect(self.pet.change_model)
        close = QtWidgets.QPushButton("关闭面板")
        close.clicked.connect(self.hide)
        quitb = QtWidgets.QPushButton("退出桌宠")
        quitb.setObjectName("danger")
        quitb.clicked.connect(self.pet.quit)
        h.addWidget(change)
        h.addWidget(close)
        h.addStretch(1)
        h.addWidget(quitb)
        return w

    @staticmethod
    def _slider_row(slider, label):
        """滑块 + 右对齐数值 合成一行（值不会随拖动跳动改变布局）。"""
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        label.setFixedWidth(52)
        label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        h.addWidget(slider, 1)
        h.addWidget(label)
        return w

    def closeEvent(self, e):
        """面板被 × 关掉时，让桌宠忘掉这个引用。

        注意：**只清引用，绝不关桌宠**。面板现在是独立顶层窗口
        （flags 带 Qt.Window），关它跟关桌宠没有任何关系。
        """
        try:
            if getattr(self.pet, "panel", None) is self:
                self.pet.panel = None
        except Exception:                                     # noqa: BLE001
            pass
        super().closeEvent(e)


# ============================================================ 选模型向导
class ModelWizard(QtWidgets.QDialog):
    """首次启动 / 换模型：选择 .ysm、文件夹、压缩包（zip/7z/rar）。

    程序**不内置任何模型**，所以第一次打开必须由用户给一个来源。
    这个对话框同时承担「换模型」职责（菜单里唤起）。

    交互
    ----
    * 三个按钮分别走「选文件 / 选文件夹 / 选压缩包」，也支持把
      文件/文件夹直接拖进来；
    * 选定后在后台线程里加载（解密+解析可能要几秒），完了再关对话框，
      期间界面不卡；
    * 加载失败把原因显示在状态栏，让用户重选。
    """

    def __init__(self, parent=None, first_run=False):
        super().__init__(parent)
        self.setWindowTitle("选择 YSM 模型")
        self.setWindowFlags(QtCore.Qt.Window
                            | QtCore.Qt.WindowStaysOnTopHint)
        self.setAcceptDrops(True)
        self.result_source = None            # 用户最终选的来源
        self._loading = False

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(18, 18, 18, 18)
        v.setSpacing(12)

        title = QtWidgets.QLabel("选择模型来源" if not first_run
                                 else "首次使用，请先选择 YSM 模型")
        f = title.font()
        f.setPointSize(f.pointSize() + 3)
        f.setBold(True)
        title.setFont(f)
        v.addWidget(title)

        tip = QtWidgets.QLabel(
            "支持：<b>.ysm 文件</b> · <b>文件夹</b>（自动找里面的 .ysm/.bin） · "
            "<b>.zip / .7z / .rar 压缩包</b><br>"
            "也可以把文件或文件夹直接拖进这个窗口。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#666")
        v.addWidget(tip)

        self.drop = QtWidgets.QLabel("把模型拖到这里")
        self.drop.setAlignment(QtCore.Qt.AlignCenter)
        self.drop.setMinimumHeight(96)
        self.drop.setStyleSheet(
            "QLabel{border:2px dashed #9aa4b2;border-radius:8px;"
            "color:#8892a0;background:transparent}")
        v.addWidget(self.drop)

        row = QtWidgets.QHBoxLayout()
        b1 = QtWidgets.QPushButton("选择 .ysm 文件…")
        b1.clicked.connect(self._pick_file)
        b2 = QtWidgets.QPushButton("选择文件夹…")
        b2.clicked.connect(self._pick_dir)
        b3 = QtWidgets.QPushButton("选择压缩包…")
        b3.clicked.connect(self._pick_archive)
        for b in (b1, b2, b3):
            row.addWidget(b)
        v.addLayout(row)

        self.status = QtWidgets.QLabel("等待选择…")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#555")
        v.addWidget(self.status)

        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 0)              # 忙碌指示
        self.bar.hide()
        v.addWidget(self.bar)

        v.addStretch(1)
        h = QtWidgets.QHBoxLayout()
        self.ok = QtWidgets.QPushButton("开始")
        self.ok.setEnabled(False)
        self.ok.setStyleSheet(
            "QPushButton{background:#4C8DFF;color:white;padding:7px 20px;"
            "border:none;border-radius:4px;font-weight:bold}"
            "QPushButton:disabled{background:#B9C4D4}"
            "QPushButton:hover:enabled{background:#5E9BFF}")
        self.ok.clicked.connect(self._start)
        cancel = QtWidgets.QPushButton("取消")
        cancel.clicked.connect(self.reject)
        h.addStretch(1)
        h.addWidget(cancel)
        h.addWidget(self.ok)
        v.addLayout(h)

        self._candidate = None
        self.resize(520, 340)

    # ---------------------------------------------------------- 拖放
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self.drop.setStyleSheet(
                "QLabel{border:2px dashed #4C8DFF;border-radius:8px;"
                "color:#4C8DFF;background:#EEF4FF}")

    def dragLeaveEvent(self, _e):
        self.drop.setStyleSheet(
            "QLabel{border:2px dashed #9aa4b2;border-radius:8px;"
            "color:#8892a0;background:transparent}")

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            self._set_candidate(path)

    # ---------------------------------------------------------- 选择
    def _pick_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择 YSM 模型", "",
            "YSM 模型 (*.ysm *.bin);;所有文件 (*.*)")
        if path:
            self._set_candidate(path)

    def _pick_dir(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "选择模型文件夹", "")
        if path:
            self._set_candidate(path)

    def _pick_archive(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择压缩包", "",
            "压缩包 (*.zip *.7z *.rar);;所有文件 (*.*)")
        if path:
            self._set_candidate(path)

    def _set_candidate(self, path):
        from pet import modelsource as MS
        desc = MS.describe(path)
        self._candidate = path
        name = os.path.basename(path.rstrip("/\\")) or path
        if desc == "不存在":
            self.status.setText("找不到：%s" % path)
            self.ok.setEnabled(False)
            return
        if desc == "不支持":
            self.status.setText("不支持的格式：%s" % path)
            self.ok.setEnabled(False)
            return
        self.status.setText("<b>%s</b><br>%s" % (name, desc))
        self.drop.setText(name)
        self.ok.setEnabled(True)

    # ---------------------------------------------------------- 加载
    def _start(self):
        if self._candidate is None or self._loading:
            return
        self._loading = True
        self.ok.setEnabled(False)
        self.bar.show()
        self.status.setText("正在加载模型（首次解密可能几秒）…")
        QtWidgets.QApplication.processEvents()

        # 同步加载但先让界面刷新（解密本身是 CPU 密集，纯 Python 无法中断）
        from pet import modelsource as MS
        try:
            data = MS.load_source(self._candidate,
                                  progress=self.status.setText,
                                  hide_effects=_hide_effects())
        except Exception as e:                               # noqa: BLE001
            import traceback
            self.bar.hide()
            self._loading = False
            self.ok.setEnabled(True)
            self.status.setText("加载失败：%s" % e)
            print(traceback.format_exc())
            return

        name = (getattr(data["raw"], "metadata", None) or {}).get("name")
        self.result_source = self._candidate
        self.result_data = data
        self.status.setText("加载成功：%s（骨骼 %d）"
                            % (name or os.path.basename(self._candidate),
                               len(data["model"].order)))
        self._loading = False
        self.bar.hide()
        QtCore.QTimer.singleShot(120, self.accept)


# ============================================================ 启动
def run(model=None, tex=None, hide=(), rig=None, scale=1.0, fps=30,
        argv=None, lighting=False, source=None):
    """启动桌宠。

    模型来源解析顺序（对应「程序不内置模型」）：
        1. 显式传入的 `source`（.ysm / 文件夹 / 压缩包）
        2. `model`+`tex`+`hide`（调用方已加载好的内存数据，向后兼容）
        3. 上次记住的 `settings["model"]`
        4. 都没有 → 弹**首次启动向导**让用户选，取消就退出
    """
    from pet.fastrender import FastRenderer
    from pet import modelsource as MS
    from pet import settings as S

    app = QtWidgets.QApplication(argv or sys.argv)
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")

    # ⚠️ 关键：不要让"最后一个窗口关闭"触发退出。
    # 桌宠窗口带 Qt.Tool 位（不进任务栏、不抢焦点），Qt 对 Tool 窗口的
    # "可见窗口集合"判定很敏感 —— 关掉设置面板的那一瞬间，Qt 会认为
    # 可见顶层窗口为 0，于是自动 quit()，**桌宠跟着一起没了**
    # （用户实测："一关设置，桌宠就跟着关了"）。
    # 关掉这个默认行为后，只有显式 `quit()`（菜单「退出」）才真的退出。
    app.setQuitOnLastWindowClosed(False)

    # 动画名/界面文案含中文，统一挑一个带 CJK 字形的字体
    fam = _pick_cjk_font()
    if fam:
        f = app.font()
        f.setFamily(fam)
        app.setFont(f)

    cfg = S.load()
    if scale == 1.0:
        scale = float(cfg.get("scale", 1.0))
    if fps == 30:
        fps = int(cfg.get("fps", 30))
    if not lighting:
        lighting = bool(cfg.get("lighting", False))

    # ---------------------------------------------------------- 解决模型来源
    data = None
    src = None
    if source:
        src = source
    elif model is None and not cfg.get("model"):
        src = None                            # 触发向导
    elif model is None:
        src = cfg["model"]

    if src:
        try:
            print("加载模型：%s" % src)
            data = MS.load_source(src, tex_png=tex,
                                  hide_effects=_hide_effects())
        except Exception as e:                               # noqa: BLE001
            print("加载上次的模型失败（%s），改让用户重选" % e)
            data = None

    if data is None and model is not None:
        # 调用方直接给了内存数据（run_pet.py 的旧路径）
        data = {"raw": None, "model": model, "tex": tex, "hide": hide,
                "tex_src": "外部", "src": None, "src_kind": "memory"}

    # 首次启动 / 上次模型失效 → 向导
    first = not bool(cfg.get("model")) if model is None else False
    if data is None:
        wiz = ModelWizard(first_run=first)
        if wiz.exec() != QtWidgets.QDialog.Accepted \
                or getattr(wiz, "result_data", None) is None:
            print("未选择模型，退出。")
            return 1
        data = wiz.result_data
        src = wiz.result_source
        S.update(model=src)

    model, tex, hide = data["model"], data["tex"], data["hide"]

    # ---------------------------------------------------------- 渲染器
    fr = FastRenderer(model, tex, hide_bones=hide)
    fr.lighting = bool(lighting)
    fr.ssaa = int(cfg.get("ssaa", 2))
    fr.zoom = float(cfg.get("zoom", 1.42))
    print("面数=%d  C++光栅=%s  取景半径=%.3f  取景倍率=%.2f"
          % (fr.nf, "是" if fr.dll else "否(纯Python回退)", fr.radius,
             fr.zoom))

    # 视线追踪：默认关闭（用户需在菜单/面板里手动开启）
    # ⚠️ 必须在这里显式导入。本模块只在函数内部 import `pet.gaze`
    #    （顶层引入会连带把 numpy/scipy 的大型 clique 拉进模块作用域，
    #     拖慢桌宠冷启动），所以下面用到的 GazeTracker **不会**从模块作用域
    #    看到 —— 漏了这一行就是启动即崩 `NameError: name 'GazeTracker' is not defined`。
    #    各种测试脚本自己 `from pet.gaze import GazeTracker`，所以它们测不出来。
    from pet.gaze import GazeTracker
    gaze = GazeTracker(model)
    gaze.enabled = bool(cfg.get("gaze_enabled", False))
    gaze.cfg["smooth"] = float(cfg.get("gaze_smooth", 7.5))
    gaze.cfg["max_yaw"] = float(cfg.get("gaze_max_yaw", 48.0))
    # 「正面朝向补偿」：模型正面在相机 yaw=多少度时正对镜头。
    # 默认 None = 自动推断（用眼骨骼位置算，见 gaze._detect_front_yaw）。
    _fy = cfg.get("gaze_front_yaw")
    gaze.cfg["front_yaw"] = float(_fy) if _fy is not None else None
    print("视线追踪: 头=%s（只转头；眼骨骼=%s 仅用于算方向，不转）"
          % (gaze.head, gaze.eye))

    # 模型自带动画（main + extra 组）
    # 同 GazeTracker：本模块只在函数内 import，用到处必须显式导入。
    from ysm.clips import ClipLibrary
    clips = ClipLibrary(model)
    # 「常驻默认动画」= main 组的 idle（原版实体站着时播的那个）；模型把
    # idle 写成空壳时退回 preview_animation。
    dclip = clips.default_clip_name()
    pre_layers, post_layers = clips.resident_layers()
    print("自带动画: %d 个可播放  轮盘 %d 项  默认动画=%s  并行层=%d"
          % (len(clips), len(clips.roulette()), dclip,
             len(pre_layers) + len(post_layers)))

    animator = IdleAnimator(model)
    # 并行层开关（下面决定待机策略、以及传给 PetWindow 时都要用同一个值）
    _par_on = bool(cfg.get("parallel_enabled", False))
    # 模型自带动画时优先用它自己的（对齐原版 YSM）；只有啥都没有的模型才
    # 用程序化呼吸兜底。用户在菜单/面板里手动开关过就不再自动决定。
    if bool(cfg.get("idle_auto", True)):
        # 只有「真正在播的模型自带动画」才顶掉程序化呼吸：
        #   * 默认待机动画（idle / preview）—— 一直播；
        #   * 并行层 —— **只有开关打开时它才在播**。开关关着时这些模型其实
        #     没有任何动画在跑（它们的 idle 常是空壳 rest 姿势），必须退回
        #     程序化呼吸，否则桌宠就是一具不动的静帧 —— 这正是"并行层动画
        #     功能消失"看起来最直观的样子。
        _has_own = bool(dclip) or (_par_on and bool(pre_layers or post_layers))
        animator.enabled = not _has_own
        print("待机: %s（%s）"
              % ("程序化呼吸" if animator.enabled else "模型自带动画",
                 "没有动画在播" if animator.enabled else
                 "模型自带 idle" if dclip else "常驻并行层"))
    else:
        animator.enabled = bool(cfg.get("idle_enabled", True))
    # 比例缓存键：模型来源路径（首次向导选的 src 或设置里存的）。换了模型
    # 键就变，缓存互不干扰；量一次 2~3 s，之后启动直接读。
    model_key = src or cfg.get("model") or ""
    w = PetWindow(fr, animator, rig=None, scale=scale, fps=fps,
                  gaze=gaze, clips=clips, model_key=model_key,
                  parallel=_par_on)

    # 默认待机 = 模型的常驻默认动画（原版 YSM 实体站着时播的那个）
    if dclip:
        w.play_clip(dclip)
        if w.wheel is not None:
            w.wheel.current = dclip

    # 默认放到屏幕右下角偏上
    scr = QtWidgets.QApplication.primaryScreen().availableGeometry()
    w.move(scr.right() - w.width() - 60, scr.bottom() - w.height() - 90)
    w.show()

    # 退出前回写设置（记住模型 + 偏好）
    def _save():
        try:
            S.update(model=src,
                     scale=w.scale, fps=w.thread.fps,
                     lighting=bool(w.thread.r.lighting),
                     ssaa=int(w.thread.r.ssaa),
                     zoom=float(w.thread.r.zoom),
                     gaze_enabled=bool(gaze.enabled),
                     gaze_smooth=float(gaze.cfg["smooth"]),
                     gaze_max_yaw=float(gaze.cfg["max_yaw"]),
                     gaze_front_yaw=(None if gaze.cfg.get("front_yaw") is None
                                     else float(gaze.cfg["front_yaw"])),
                     idle_enabled=bool(w.thread.animator.enabled))
        except Exception:                                    # noqa: BLE001
            pass
    app.aboutToQuit.connect(_save)

    return app.exec()
