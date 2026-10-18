"""模型自带动画：把 .ysm 里解出的动画接成可播放的 clip。

数据来源
--------
`YsmModel.raw.animations` 是一个 `{组名: {动画名: 动画定义}}` 的字典。
*.ysm 里通常有若干「组」，实测亚托莉模型分 9 组：

    main        —— 本体动作（idle/walk/run/sit/sleep/海边/潜行/爬行/趴下/蹲下…）
    arm         —— 手持物品姿势（hold_mainhand:sword 等）
    slashblade  —— 拔刀剑模组动作
    tac         —— 枪械模组动作
    parcool     —— 跑酷模组动作
    swem/tlm/carryon/extra —— 其他模组

桌宠只关心 **本体动作**，所以默认只用 `main` 组。

动画定义的关键字段
------------------
    length : 动画时长（秒）；`inf` 表示"单帧静态姿势"（比如 idle 就是 0 帧）
    loop   : 1=循环 / 3=单次（death）
    bones  : {骨骼名: {"rotation":[{t,mode,post}...], "position":…, "scale":…}}

采样走 `ysm.animation.sample_channel`（已支持 `{t, mode, post}` 原生键帧）。
"""
import math
import re
import time

from ysm.animation import sample_channel

# 播放模式
ONCE = "once"
LOOP = "loop"
HOLD = "hold"          # 播完停在最后一帧

# 常驻并行层：`main` 组里名字形如 pre_parallel0 / parallel3 的动画。
# 原版 YSM 把它们**始终循环播放**（见 ResidentPlayer）。
_RESIDENT_RE = re.compile(r"^(pre_parallel|parallel)(\d+)$")


class Clip:
    """一个可播放的模型自带动画。"""

    def __init__(self, group, name, raw):
        self.group = group
        self.name = name
        self.raw = raw or {}
        length = self.raw.get("length", 0) or 0
        # inf / 非有限值 → 单帧静态姿势
        try:
            length = float(length)
        except (TypeError, ValueError):
            length = 0.0
        self.static = not math.isfinite(length)
        self.length = 0.0 if self.static else max(length, 1e-3)
        self.bones = self.raw.get("bones") or {}
        loop = self.raw.get("loop", 1)
        self.loop_flag = loop
        # loop==3 是"单次播放"（death 这类），其余当循环
        self.mode = LOOP if loop != 3 else ONCE

    # ------------------------------------------------------------------
    @property
    def n_bones(self):
        return len(self.bones)

    @property
    def playable(self):
        """有没有实际会动的内容。0 骨骼的条目（idle/run/walk 的空壳）不算。"""
        return bool(self.bones)

    def label(self):
        """GUI 显示名：把模组前缀去掉，太长就截断。"""
        n = self.name
        if ":" in n:
            n = n.split(":", 1)[1]
        return n

    def duration_text(self):
        if self.static:
            return "静态"
        return "%.1fs" % self.length

    # ------------------------------------------------------------------
    def sample(self, t):
        """-> {bone: {"rot":[3], "pos":[3], "scale":[3]}}（与 FastRenderer 一致）。"""
        pose = {}
        for bone, chans in self.bones.items():
            entry = {}
            for ch, key in (("rotation", "rot"), ("position", "pos"),
                            ("scale", "scale")):
                if ch in chans:
                    v, _ = sample_channel(chans[ch], t, self.length)
                    # 空键帧列表 → sample_channel 返回 None 保护
                    if v is not None:
                        entry[key] = v
            if entry:
                pose[bone] = entry
        return pose


class ClipLibrary:
    """模型自带动画集合，供 GUI 选播。"""

    # 解析范围：本体动作 + extra 组（extra 才是作者给轮盘挑的那批）
    DEFAULT_GROUPS = ("main", "extra")

    def __init__(self, model, groups=DEFAULT_GROUPS, min_bones=1):
        self.model = model
        self.groups = tuple(groups)
        props = getattr(getattr(model, "raw", None), "properties", None) or {}
        self.animations = getattr(getattr(model, "raw", None), "animations", {}) or {}
        # extra_animations: {动画名: 显示名}，**保持 .ysm 里的原始顺序**
        # —— 这个顺序就是原版 YSM 动画轮盘的扇区顺序（作者排好的）。
        self.labels = dict(props.get("extra_animations") or {})
        # 子菜单：{子菜单id: {动画名: 显示名}}
        # 上游 `ModelProperties.getExtraAnimationClassify()`
        self.classify = {}
        for c in (props.get("extra_animation_classifies") or []):
            if isinstance(c, dict) and c.get("id"):
                self.classify[c["id"]] = dict(c.get("extras") or {})
        # 参数表单组：{id: {name, forms}}
        self.buttons = {}
        for b in (props.get("extra_animation_buttons") or []):
            if isinstance(b, dict) and b.get("id"):
                self.buttons[b["id"]] = b
        self.clips = {}
        for g in self.groups:
            for name, raw in (self.animations.get(g) or {}).items():
                if not isinstance(raw, dict):
                    continue
                c = Clip(g, name, raw)
                if c.playable and c.n_bones >= min_bones:
                    self.clips[name] = c

    # ------------------------------------------------------------------
    def names(self):
        return sorted(self.clips)

    def display_name(self, name):
        """优先用 .ysm 里作者写的显示名，否则用去前缀的动画名。"""
        if name in self.labels:
            return self.labels[name]
        c = self.clips.get(name)
        return c.label() if c is not None else name

    def by_group(self, group):
        """返回该组里所有可播放动画名（按名字排序）。"""
        return sorted(n for n, c in self.clips.items() if c.group == group)

    # ------------------------------------------------------------------
    def roulette(self, menu_id=""):
        """原版 YSM 动画轮盘的内容，按作者在 `.ysm` 里排的顺序。

        上游 `AnimationRouletteScreen` 用 `ModelProperties.getExtraAnimation()`
        （= `extra_animations`），每页 8 个、45° 一格。这里还原它的三种条目：

            key 以 `#` 开头            → 子菜单（跳进 extra_animation_classifies）
            key == `#return`           → 「返回」项（子菜单里才有）
            value 以 `#` 开头          → 打开参数表单组（extra_animation_buttons）
            其余                        → 普通动画，点了就播

        参数 menu_id 为空字符串 = 顶层菜单；否则取 `classify[menu_id]`。

        -> [(kind, name, label), ...]，kind ∈ {"clip","submenu","config","return"}
        """
        table = self.labels if not menu_id else self.classify.get(menu_id, {})
        out = []
        for name, label in table.items():
            key = name if not menu_id else str(name)

            # key 以 # 开头 → 子菜单 / 返回
            if key.startswith("#"):
                if key == "#return":
                    out.append(("return", "#return", "返回"))
                elif key[1:] in self.classify:
                    out.append(("submenu", key[1:], label or key[1:]))
                continue

            c = self.clips.get(key)
            # value 以 # 开头 → 参数表单组
            if isinstance(label, str) and label.startswith("#"):
                gid = label[1:]
                if gid in self.buttons:
                    out.append(("config", gid,
                                self.buttons[gid].get("name") or gid))
                continue

            if c is not None and c.playable:
                out.append(("clip", key, label))

        if out:
            return out
        # 模型没写 extra_animations → 退回「有可播内容的 extra 组动画」
        if not menu_id:
            return [("clip", n, self.display_name(n))
                    for n in self.by_group("extra")]
        return out

    def submenu_label(self, menu_id):
        """子菜单标题：优先用顶层里引用它的那条目显示名。"""
        for name, label in self.labels.items():
            if name == "#" + menu_id and isinstance(label, str):
                return label
        return menu_id

    def preview_clip_name(self):
        """`properties.preview_animation`：模型预览页播放的那个动画（如 'gui'）。

        原版 YSM 的模型预览界面就循环放它；桌宠把它当「默认待机」用最贴近。
        """
        props = getattr(getattr(self.model, "raw", None), "properties", None) or {}
        name = props.get("preview_animation")
        c = self.clips.get(name) if name else None
        return name if (c is not None and c.playable) else None

    def default_clip_name(self):
        """桌宠「常驻」主体动画（对齐原版 YSM）——**没有就返回 None**。

        原版实体**站着不动**时播 `main` 组的状态动画 `idle`。作者常把
        `idle` 写成空壳（`length=inf`、0 骨骼）—— 意思就是"这个状态没有额外
        动画，保持 rest 姿势"，此时**正确的呈现就是 rest 姿势 + 并行层**。

        ⚠️ 不要退回 `preview_animation`。那是**模型预览界面/GUI 里的展示
        姿势**（亚托莉叫 `gui`），会带 root 位移/放大，实测把它当常驻播时
        人物高度从画幅 71% 涨到 100%+ 并被裁切 —— 用户看到的"桌宠过大"。
        它仍然是可播动画，用户能在轮盘/动画列表里自己选。
        """
        for cand in ("idle", "default", "stand"):
            c = self.clips.get(cand)
            if c is not None and c.playable:
                return cand
        return None

    # ------------------------------------------------------------------
    def resident_layers(self):
        """常驻并行层 -> (pre_list, post_list)。

        `.ysm` 的 `main` 组里有两类**特殊**动画：

            pre_parallel0..N   在主体动画**之前**应用（基础校正层）
            parallel0..N       在主体动画**之后**应用（覆盖层）

        作者用它们驱动头发、裙摆、飘带、光环这些「会自己动」的部件。
        **原版 YSM 里这两层始终循环播放**（各层按自己的时长独立循环，
        实测 parallel0=5s / parallel4=4s / pre_parallel4=8s）。

        缺了它们，模型会死死固定在 rest 姿势 —— 用户报的
        "有一些动态的模型会固定"就是这一条。
        """
        pre, post = [], []
        for name, c in self.clips.items():
            m = _RESIDENT_RE.match(name)
            if not m:
                continue
            (pre if m.group(1) == "pre_parallel" else post).append(
                (int(m.group(2)), c))
        pre.sort(key=lambda kv: kv[0])
        post.sort(key=lambda kv: kv[0])
        return [c for _, c in pre], [c for _, c in post]

    def get(self, name):
        return self.clips.get(name)

    def __len__(self):
        return len(self.clips)

    def __contains__(self, name):
        return name in self.clips

    def all_groups(self):
        """所有组名 + 里面可播放的动画数（GUI 可用它做分组展示）。"""
        out = []
        for g, items in self.animations.items():
            n = 0
            for name, raw in (items or {}).items():
                if isinstance(raw, dict) and Clip(g, name, raw).playable:
                    n += 1
            out.append((g, n))
        return out


class ClipPlayer:
    """在渲染线程里按时间推进一个 clip。

    与 IdleAnimator 的分工：玩家选中的动画**覆盖**待机动作（后写胜出），
    播放结束后自动回到待机。
    """

    def __init__(self):
        self.clip = None
        self.t0 = 0.0
        self.speed = 1.0
        self._done = False

    # ------------------------------------------------------------------
    def play(self, clip, now, speed=1.0):
        self.clip = clip
        self.t0 = now
        self.speed = max(0.05, float(speed))
        self._done = False

    def stop(self):
        self.clip = None
        self._done = False

    @property
    def active(self):
        return self.clip is not None and not self._done

    @property
    def finished(self):
        return self._done

    def progress(self, now):
        if self.clip is None:
            return 0.0
        if self.clip.static or self.clip.length <= 0:
            return 1.0
        t = (now - self.t0) * self.speed
        if self.clip.mode == LOOP:
            return (t % self.clip.length) / self.clip.length
        return min(t / self.clip.length, 1.0)

    # ------------------------------------------------------------------
    def tick(self, now):
        """-> {bone: {...}}；没有在播就返回 {}。"""
        c = self.clip
        if c is None:
            return {}
        if c.static:
            # 静态姿势：一直保持，不标记结束
            return c.sample(0.0)

        el = (now - self.t0) * self.speed
        if c.mode == LOOP:
            t = el % c.length
        else:
            if el >= c.length:
                # ⚠️ 修复「穿模后贴图渲染直接消失」：ONCE 动画播完必须
                #    **立刻交还待机**（返回 {}），不能再继续 sample(t=length)。
                #    原来这里只置 _done 标志、却仍每帧返回末帧姿势 —— 而
                #    extra0~4 / death 这类动画的末帧是「整副骨骼 scale=0
                #    塌缩」的隐藏帧，结果动画一播完桌宠就永久隐形。
                #    与本类 docstring「播放结束后自动回到待机」对齐。
                self._done = True
                return {}
            t = el
        return c.sample(t)


class ResidentPlayer:
    """常驻并行动画层（对齐原版 YSM 的 `pre_parallel` / `parallel`）。

    原版 YSM 除了「主体状态动画」（idle / walk / …）之外，还**一直**叠加
    两层并行层：

        pre_parallel*   主体**之前**（基础校正）
        主体动画（idle / 用户选的动作）
        parallel*       主体**之后**（覆盖）

    它们驱动头发、裙摆、飘带、光环这些「会自己动」的部件。桌宠之前只播
    单个主体动画，所以那些部件**固定在 rest 姿势**（用户报的"有一些动态的
    模型会固定"），而某些部件停在错的基准上又显得"绑定反了"。

    关键：每一层按**自己的时长**独立循环 —— parallel0=5s、parallel4=4s、
    pre_parallel4=8s，各不相同，所以不能共用一条时间轴。
    """

    def __init__(self, clips=None):
        self.pre = []
        self.post = []
        self.enabled = True
        self.t0 = time.time()
        if clips is not None:
            self.set_library(clips)

    def set_library(self, clips):
        """换模型时重新绑定（并重置计时，避免换模型瞬间跳姿势）。"""
        if clips is None:
            self.pre, self.post = [], []
        else:
            self.pre, self.post = clips.resident_layers()
        self.t0 = time.time()

    # ------------------------------------------------------------------
    # ⚠️「模型有没有并行层」和「并行层开没开」是**两件事**，必须分开：
    #    曾经这里只有 `available = enabled and (pre or post)`，而桌宠默认
    #    `enabled=False` —— 于是设置面板里 `if res.available:` 这个判据
    #    **永远为假，复选框根本画不出来**，用户就再也没法把并行层打开了。
    #    这就是用户报的「并行层动画功能消失」。
    @property
    def available(self):
        """模型**有没有**并行层（**与开关无关**）—— UI 用它决定要不要画开关。"""
        return bool(self.pre or self.post)

    @property
    def active(self):
        """并行层**是否真的在生效**（开关打开 且 有层）—— 量比例时按这个判。"""
        return bool(self.enabled and (self.pre or self.post))

    @property
    def n_layers(self):
        return len(self.pre) + len(self.post)

    def names(self):
        return [c.name for c in self.pre] + [c.name for c in self.post]

    # ------------------------------------------------------------------
    def _sample(self, group, el):
        out = {}
        for c in group:
            t = 0.0 if c.static else (el % c.length)
            try:
                pose = c.sample(t)
            except Exception:                                # noqa: BLE001
                continue
            for b, v in pose.items():
                out.setdefault(b, {}).update(v)
        return out

    def layer_pre(self, now):
        """pre_parallel 层（主体动画之前应用）。"""
        if not self.enabled:
            return {}
        return self._sample(self.pre, now - self.t0)

    def layer_post(self, now):
        """parallel 层（主体动画之后应用，覆盖前者）。"""
        if not self.enabled:
            return {}
        return self._sample(self.post, now - self.t0)

    # ------------------------------------------------------------------
    # 离线采样（不依赖实时钟）：供「量窗口尺寸」这类用途在固定时间点上取姿势。
    def pre_at(self, el):
        return self._sample(self.pre, el)

    def post_at(self, el):
        return self._sample(self.post, el)

    def at(self, el):
        """pre + post 合成（后写覆盖）。"""
        out = self._sample(self.pre, el)
        for b, v in self._sample(self.post, el).items():
            out.setdefault(b, {}).update(v)
        return out

    def span(self):
        """所有层的时长范围 [0, max_len]，用于离线扫时间点。"""
        lens = [c.length for c in (self.pre + self.post) if not c.static]
        return max(lens) if lens else 0.0
