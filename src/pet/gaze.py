"""视线追踪：桌宠的头跟随鼠标光标（MC 背包那种「转头看你」的效果）。

目标
----
Minecraft 里把宠物/经验球放进背包 GUI 时，它们的头会自动朝玩家的鼠标方向
转过来。这里复刻同样的观感：**整个头转过去**，眼睛只用来判断方向、
本身不动（MC 皮肤的眼睛是脸上贴图，不是可动部件）。

坐标系
------
输入是桌宠窗口的全局像素坐标，先换算成「相对桌宠中心」的方向：

    dx = 鼠标x - 桌宠中心x        （右为正）
    dy = 鼠标y - 桌宠中心y        （下为正）

**方向符号（2026-10-05 二次实测修正，⚠️ 与旧结论相反）**

  * 绕竖直轴转 `ry`：**ry 增大 → 头朝屏幕「左」转**。
    （旧版注释写的是"ry 为正 → 朝屏幕右"，那是**错的** —— 它来自
    `scripts/_probe_gaze_sign.py` 当时的判据"整幅轮廓的质心 x 位移"，
    而身体/头发/裙摆都不动，把头部信号稀释到只剩 2~7 px，方向被噪声
    定反了。现改用「脸朝向向量 · 屏幕右基向量」的几何判据，见
    `scripts/_test_gaze_dir.py`。）
  * 绕水平轴转 `rx`：**rx 为正 → 低头**，rx 为负 → 抬头。
    所以「光标在屏幕上方」（dy<0）要 **rx < 0**（这条一直是对的）。

**为什么 ry 的符号会反（可自行验算）**

相机在模型方位 ψ=cam_yaw 处、模型正面在相机 `front_yaw` 时正对镜头。
头绕竖直轴转 ry 后，正面朝向（模型空间）

    P(ry) = (sin ry, 0, -cos ry)

而 **屏幕右方向在模型空间**是（`scripts/_probe_camera.py` 的精确解，
yaw=28 时实测 `(-0.469, 0, -0.883)`）

    R = (-sin ψ, 0, -cos ψ)

于是「脸在屏幕上有多右」= `P·R = cos(ry + ψ)`。中位处 `ry = front_yaw - ψ`，
代入得 `ry + ψ = front_yaw ≈ 90°`，`d(P·R)/d(ry) = -sin(front_yaw) < 0`
—— **ry 越大，脸越往屏幕左**。所以「光标在右」必须 **减小** ry。

**正面朝向补偿**

相机默认停在 yaw=28°（3/4 侧视），而**模型正面在相机 yaw=90° 时才是正对
镜头**（见 `scripts/_calib_cam_yaw.py` 的对照图）。也就是说人物的「正面」
离镜头有 `front_yaw - cam_yaw` 度。如果直接把「光标在正中」映射成「头转 0°」，
头就会一直偏着，看着像"没在看光标"。

所以先把头预转 `front_yaw - cam_yaw`，让**光标在正中时光标方向 = 头正对镜头**；
光标左右移动再在这个中位上加/减摆幅。这样「光标在右 → 头看向右侧」是
**对称**的，而不是"往右只能转到正脸、往左直接扭过头"。
"""
import math
import time

DEFAULTS = {
    "max_yaw": 48.0,        # 中位左右的最大摆幅（度）
    "max_pitch": 34.0,      # 头上下最大转角（度）
    # 模型正面朝向（相机 yaw，度）。None = 自动推断（推荐）—— 用「眼骨骼
    # 相对头骨骼的水平偏移」算出来，换模型不用手调；推不出来时退回 90。
    "front_yaw": None,
    "smooth": 7.5,          # 平滑速度（越大越跟手，越小越懒）
    "dead_zone": 3.0,       # 归一化死区（光标离正前方很近时不动）
    "reach_px": 520.0,      # 达到最大转角所需的水平像素距离
    "idle_timeout": 6.0,    # 多久没动鼠标就缓缓回正（秒）, 0=从不回正
}

# 自动推断失败时的兜底正面朝向（相机 yaw，度）。
# 90 = 相机 yaw 转到 90° 时正对模型的脸 —— 实测亚托莉模型如此，也是
# YSM/MC 模型里最常见的朝向。
FRONT_YAW_FALLBACK = 90.0


class GazeTracker:
    """把光标位置转成头/眼的旋转量。线程安全（只读写几个 float）。"""

    HEAD_BONES = ("AllHead", "MHead", "Head")
    EYE_BONES = ("Eyes", "Eyes2", "Eye_main")

    def __init__(self, model, cfg=None):
        self.cfg = dict(DEFAULTS)
        if cfg:
            self.cfg.update(cfg)
        names = [b["name"] for b in model.order]
        self.names = names
        self.nset = set(names)
        self._order = list(model.order)
        # 名字 → parent 名，用于判断"谁是别人的祖先"
        self.parent = {b["name"]: b.get("parent") for b in model.order}

        self.enabled = False
        # 光标（窗口局部坐标，None = 尚未收到）
        self.cursor = None
        self.pet_center = None      # 桌宠中心（窗口局部坐标）
        self.pet_size = None
        self._last_move = 0.0
        # 相机朝向（度）。由桌宠在 set_camera 里持续喂入；默认与
        # pet.settings 的默认视角一致，避免没喂时算出歪方向。
        self.cam_yaw = 28.0
        self.cam_pitch = 10.0

        # 平滑后的当前角度（度）
        self._cur_yaw = 0.0
        self._cur_pitch = 0.0
        self._target_yaw = 0.0
        self._target_pitch = 0.0
        self._last_t = time.time()

        # 绑定实际存在的骨骼
        self.head = self._pick(self.HEAD_BONES)
        self.eye = self._pick(self.EYE_BONES)
        # 自动推断的正面朝向（相机 yaw，度）；None = 推不出来
        self.auto_front_yaw = self._detect_front_yaw()

    # ------------------------------------------------------------------
    def _detect_front_yaw(self):
        """用眼骨骼推断「相机 yaw 取多少度时正对模型的脸」。

        原理：眼睛长在脸的前方，所以从**头骨骼 pivot** 指向**眼骨骼 pivot**
        的水平向量就指向模型正面。已知相机 yaw=ψ 时相机落在模型方向
        `(cos ψ, 0, -sin ψ)`（见 scripts/_probe_camera.py 的精确解），
        令它等于正面方向 `(hx, 0, hz)` 即得 `ψ = atan2(-hz, hx)`。

        实测（亚托莉）：正面 = 模型 -Z，推断出 89.4° ≈ 兜底值 90°，吻合。
        换模型若"头总是偏向一边"，就是这个值没推准 —— 可在设置面板里手调。
        """
        if self.head is None:
            return None
        piv = {b["name"]: (float(b["pivot"][0]) / 16.0,
                           float(b["pivot"][2]) / 16.0)      # 只留 (mx, mz)
               for b in self._order}
        hp = piv.get(self.head)
        if hp is None:
            return None
        ax = az = 0.0
        n = 0
        for name in self.names:
            if "eye" not in name.lower() or name == self.head:
                continue
            # 只认头骨骼的后代（避免别的部位偶尔带 "eye" 干扰）
            if not self._is_descendant(name, self.head):
                continue
            p = piv.get(name)
            if p is None:
                continue
            hx, hz = p[0] - hp[0], p[1] - hp[1]
            ln = math.hypot(hx, hz)
            if ln < 1e-4:
                continue
            ax += hx / ln                                     # 单位向量平均
            az += hz / ln
            n += 1
        ln = math.hypot(ax, az)
        if n == 0 or ln < 1e-3:
            return None
        hx, hz = ax / ln, az / ln
        # 相机 yaw=ψ 时落在模型方向 (cos ψ, 0, -sin ψ)；令它等于正面方向
        # (hx, 0, hz) → cos ψ = hx, sin ψ = -hz → ψ = atan2(-hz, hx)。
        return math.degrees(math.atan2(-hz, hx)) % 360.0

    # ------------------------------------------------------------------
    def _pick(self, cands):
        for c in cands:
            if c in self.nset:
                return c
        return None

    def _is_descendant(self, name, ancestor):
        """name 是否在 ancestor 的子树里（模型骨骼是树，只有一条父链）。"""
        if ancestor is None or name == ancestor:
            return False
        n = self.parent.get(name)
        seen = set()
        while n is not None and n not in seen:
            if n == ancestor:
                return True
            seen.add(n)
            n = self.parent.get(n)
        return False

    @property
    def eye_driven(self):
        """眼骨骼集合 —— **现在恒为空**，保留只为兼容旧调用与自检。

        历史：曾用 `"eye" in n.lower()` 把 24 个后代（眼球块/眉毛/眼睑）
        都塞进来各写一遍 rot，而 `pose_matrices` 是父子累积
        （`Mcur[i] = Mcur[parent] @ local`），结果旋转叠加 2~6 层、
        五官错位（用户报的"面部和眼睛与其他五官绑定错误"）。

        2026-10-05 用户进一步明确：**眼睛只用来算朝哪边，本身不转**
        （MC 皮肤的眼睛是脸上贴图，不是可动部件）。所以转向层只写头骨骼，
        眼骨骼一个都不写 —— 这里返回空元组。
        """
        return ()

    @property
    def available(self):
        """模型有没有可转的头或眼。"""
        return self.head is not None or self.eye is not None

    # ------------------------------------------------------------------
    def set_cursor(self, x, y):
        """窗口局部坐标（像素）。"""
        self.cursor = (float(x), float(y))
        self._last_move = time.time()

    def set_geometry(self, center, size):
        """桌宠中心与尺寸（窗口局部坐标）。"""
        self.pet_center = (float(center[0]), float(center[1]))
        self.pet_size = (float(size[0]), float(size[1]))

    def set_camera(self, yaw, pitch=0.0):
        """当前相机朝向（度）。

        **这是"绑定正确"的关键。** 头骨骼的 rot 是在**模型局部空间**里
        施加的（绕模型自己的竖直轴），而相机是绕着模型转的（默认 yaw=28°
        的 3/4 侧视）。要让"光标在正中 → 头正对镜头"，必须知道模型的
        正面朝向相机 yaw 多少度时才正对镜头（`front_yaw`，默认 90°）。
        """
        self.cam_yaw = float(yaw)
        self.cam_pitch = float(pitch)

    def reset(self):
        self.cursor = None
        self._target_yaw = self._target_pitch = 0.0

    # ------------------------------------------------------------------
    def front_yaw(self):
        """模型正面朝向（相机 yaw，度）：设置里显式指定 > 自动推断 > 兜底 90。"""
        v = self.cfg.get("front_yaw")
        if v is not None:
            return float(v)
        if self.auto_front_yaw is not None:
            return float(self.auto_front_yaw)
        return FRONT_YAW_FALLBACK

    def neutral_yaw(self):
        """「正面朝向补偿」：让头正对镜头所需的绕竖直轴转角（度）。

        相机在 `cam_yaw`、模型正面在相机 `front_yaw` 时正对镜头，所以
        人物的正面离镜头 `front_yaw - cam_yaw` 度，先把头预转这么多，
        头就正对镜头（光标在正中时应当如此）。

        实测校验（scripts/_probe_camera.py + _shot_head_rot.py）：
          cam_yaw=28、front_yaw=90 → neutral=62°，渲染出来正好是正脸。
        """
        return self.front_yaw() - self.cam_yaw

    def yaw_side_sign(self):
        """把「光标在屏幕右侧」映射成 ry 的**增减方向**（-1 或 +1）。

        推导（见文件头「为什么 ry 的符号会反」）：
            脸的屏幕水平分量  P·R = cos(ry + ψ)
            中位处 ry+ψ = front_yaw
            d(P·R)/d(ry) = -sin(front_yaw)

        `front_yaw ∈ (0°,180°)` 时该导数为负 → **ry 增大 = 脸朝屏幕左**，
        所以「光标在右」要取 **-1**。绝大多数 YSM 模型正面朝 -Z，
        `front_yaw ≈ 90°`，都落在这一支。

        只有正面朝 +Z 的模型（front_yaw 接近 270°）才需要 +1 —— 这种模型
        等于"背对镜头站着"，保留这一支是为了不写死。
        """
        return -1.0 if math.sin(math.radians(self.front_yaw())) >= 0.0 else 1.0

    # ------------------------------------------------------------------
    def _aim(self):
        """把光标位置换算成**模型局部空间**的目标角度（度）。

        两个部分相加：

          1) **中位**：`front_yaw - cam_yaw` —— 光标在正中时头正对镜头；
          2) **摆幅**：屏幕水平偏移 `nx` × `max_yaw` × `yaw_side_sign()`。
             光标在屏幕右侧 → 头朝屏幕右转（`P·R > 0`）；
             光标在上方（`ny<0`）→ 抬头（`rx<0`）。

        ⚠️ 两处历史坑，都别再踩：
          * 曾经既没有中位补偿、又用 `nx·cos(cam_yaw)` 当水平量 → 28° 侧视下
            「光标在右」只能把头顶到正脸、「光标在左」直接扭到后脑勺；
          * 修好中位后**符号仍然反着**（`+ nx`）—— 因为 `ry` 增大其实是朝
            屏幕**左**（见 `yaw_side_sign()` 的推导与
            `scripts/_test_gaze_dir.py` 的几何判据）。用户看到的就是
            "左右反了"。
        """
        c = self.cfg
        if self.cursor is None or self.pet_center is None:
            return 0.0, 0.0
        dx = self.cursor[0] - self.pet_center[0]
        dy = self.cursor[1] - self.pet_center[1]

        # 归一化到 [-1,1]（水平按 reach_px，垂直按桌宠半高）
        reach = max(c["reach_px"], 1.0)
        nx = max(-1.0, min(1.0, dx / reach))
        half_h = max(self.pet_size[1] * 0.5, 1.0) if self.pet_size else reach
        ny = max(-1.0, min(1.0, dy / (half_h * 1.6)))

        mag = math.hypot(nx, ny)
        if mag < c["dead_zone"] / 100.0:
            # 光标正对前方：回到「正对镜头」而不是 0（0 是 3/4 侧视的静止朝向）
            return self.neutral_yaw(), 0.0

        yaw = self.neutral_yaw() + self.yaw_side_sign() * nx * c["max_yaw"]
        pitch = ny * c["max_pitch"]
        return yaw, pitch

    def tick(self):
        """推进平滑，返回 {"yaw":度, "pitch":度}。"""
        now = time.time()
        dt = max(1e-3, min(now - self._last_t, 0.1))
        self._last_t = now

        if self.enabled:
            # 超时未动 → 缓缓回正
            tmo = self.cfg["idle_timeout"]
            if tmo > 0 and (now - self._last_move) > tmo:
                self._target_yaw = self._target_pitch = 0.0
            else:
                self._target_yaw, self._target_pitch = self._aim()
        else:
            self._target_yaw = self._target_pitch = 0.0

        k = 1.0 - math.exp(-self.cfg["smooth"] * dt)
        self._cur_yaw += (self._target_yaw - self._cur_yaw) * k
        self._cur_pitch += (self._target_pitch - self._cur_pitch) * k

        if abs(self._cur_yaw) < 0.01:
            self._cur_yaw = 0.0
        if abs(self._cur_pitch) < 0.01:
            self._cur_pitch = 0.0
        return {"yaw": self._cur_yaw, "pitch": self._cur_pitch}

    # ------------------------------------------------------------------
    def to_anim(self, gaze=None):
        """把角度转成动画字典（叠加层，后写覆盖待机动画）。

        **只转头，不转眼睛。**（用户 2026-10-05 明确要求）

        眼睛在这里只用于**判断朝向**（决定头往哪边转、转多少），
        本身不写任何 rot —— 因为 MC 皮肤的"眼睛"是画在脸上的贴图色块，
        不是可动部件。硬转眼骨骼会让眼球/眉毛/眼睑一起位移，看着像
        "眼珠滑出眼窝"，也不是原版那种"转头看你"的观感。
        原版行为就是**整个头转过去**，眼睛始终长在脸上跟着头走。
        """
        if not self.enabled or not self.available:
            return {}
        g = gaze if gaze is not None else {"yaw": self._cur_yaw,
                                          "pitch": self._cur_pitch}
        yaw, pitch = g["yaw"], g["pitch"]
        if yaw == 0.0 and pitch == 0.0:
            return {}

        # 注意：**不能**再乘任何削弱系数。`yaw` 里含「正面朝向补偿」这一
        # 常数项，一乘系数中位就偏了（原来那个 1-share/2 是给"眼球分担
        # 一部分转角"用的，眼球不动之后它只会让头转不够）。
        out = {}
        if self.head is not None:
            out[self.head] = {"rot": [pitch, yaw, 0.0]}
        return out


class CursorPoller:
    """兜底光标轮询：Qt 只在鼠标进入窗口时给事件，桌宠窗口很小，
    光标大部分时间在外面。所以需要全局轮询（Windows GetCursorPos）。
    若拿不到全局坐标，退化为「鼠标在窗口上时才有反应」。
    """

    def __init__(self):
        self._get = None
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            pt = wintypes.POINT()

            def get_pos():
                if user32.GetCursorPos(ctypes.byref(pt)):
                    return int(pt.x), int(pt.y)
                return None
            self._get = get_pos
        except Exception:                                    # noqa: BLE001
            self._get = None

    @property
    def available(self):
        return self._get is not None

    def global_pos(self):
        return self._get() if self._get else None
