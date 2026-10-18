"""桌宠设置持久化：记住上次用的模型 + 用户偏好。

存到 `~/.workbuddy/ysm_pet/settings.json`（用户级，不占项目目录）。
不内置任何模型路径 —— 首次运行 `model` 为空，由向导让用户选。
"""
import json
import os

_DEFAULT = {
    "model": "",              # 上次加载的模型来源（.ysm/文件夹/压缩包）
    "scale": 1.0,
    "fps": 30,
    "lighting": False,
    "ssaa": 2,
    # 相机后退倍率：越大 → 人越小。
    # 1.42 配合 BASE_H=340：窗口宽度现在由**全动画全局包围盒比例**推出
    # （亚托莉 ≈0.87 × 340 × 1.25 ≈ 370px），人物占窗高约 70%。
    #   1.20→85%  1.30→79%  1.42→72%  1.50→68%  1.75→57%  2.05→49%
    "zoom": 1.42,
    # 相机朝向（用户要求默认 yaw=90 / pitch=0，正对模型侧面→正面）。
    # 现在**持久化**：面板滑块松手 / 右键菜单「朝向复位」都会写回这里，
    # 重启桌宠恢复上次朝向；「复位」回到这两个默认值。
    "yaw": 90.0,
    "pitch": 0.0,
    "gaze_enabled": False,    # 默认关闭，需用户手动开启
    "gaze_smooth": 7.5,
    # 中位左右的最大摆幅（度）。注意：这是**围绕"正对镜头"中位**的摆幅，
    # 不是"围绕模型静止朝向"的摆幅 —— 见 gaze._aim 的「正面朝向补偿」。
    # 48 配合补偿可让"光标在右 → 头看右 48°"，对称且不过猛。
    "gaze_max_yaw": 48.0,
    # 模型正面朝向（相机 yaw 多少度时正对镜头）。它决定"光标在正中"时头
    # 应该预转多少（`front_yaw - 当前相机 yaw`）。
    #   null = 自动推断（推荐）：用「眼骨骼相对头骨骼的水平偏移」算，
    #          换模型不用手调；推不出来时退回 90。
    #   数字 = 手动指定。若头总是偏向一边，按 ±90 试。
    "gaze_front_yaw": None,
    "idle_enabled": True,
    # True = 按「模型有没有自带动画」自动决定是否叠程序化呼吸。
    # 有自带动画（idle/preview 或 parallel 层）时用模型自己的，对齐原版
    # YSM；只有啥都没有的模型才用程序化待机兜底。用户在菜单里手动开关过
    # 之后会被置为 False（尊重用户选择）。
    "idle_auto": True,
    # 常驻并行层：原版 YSM 的 pre_parallel / parallel。游戏里它们由
    # 「玩家状态 molang」驱动（护甲值/耐久/箭矢/背景板/HUD 图层…），
    # 桌宠里没有这些变量，整层套上去会把身体藏掉（慧慧1.2：整层应用后
    # 只剩约 3.6 单位高的一小团）。所以默认关闭，需要的用户再手动开。
    "parallel_enabled": False,
    # 「清晰模式」：只画人物本体，藏掉模型里的特效/立绘/游戏内 UI/备用部件。
    # YSM 模型常把这些拆成独立根骨骼子树（慧慧1.2 的 texiao/DAZ/MZ2 等
    # 合计 42% 的面；斯卡蒂的 MUI/BeiJing/MZhuanPan/MTouzi 是游戏内控件），
    # 游戏里由 molang 按需显示 —— 桌宠没有状态机，全画出来就叠在人物脸上。
    # False（默认）= 清晰模式；True = 全部部件照画（想还原游戏画面的用户开）。
    "show_effects": False,
    "top": True,
    "wheel_pad": 96,
    # 设置结构版本。改了默认值语义时 +1，老配置会被迁移一次。
    # v2：窗口改为按模型长宽比计算。
    # v3：比例改为「全动画全局包围盒」；zoom 统一收口到 1.42。
    # v4：视线改为「正面朝向补偿 + 对称摆幅」，max_yaw 62 收口到 48。
    # v5：常驻并行层默认关闭（桌宠没有游戏内 molang 变量，开着会把身体
    #     整块藏掉）；老配置里是 true 的一律刷成 false。
    # v6：新增「清晰模式」`show_effects=False`（只画人物本体）。新键在老配置
    #     里不存在 → 直接吃新默认值，不需要 remap 规则；+1 是为了让比例缓存
    #     等按下标/版本号缓存的东西作废。
    "cfg_version": 6,
}

# 老配置迁移：把"上一代默认值"刷成新的。只在值**正好等于老默认**时才刷新
# —— 那说明用户没动过它，是被旧版本写进去的。用户自己调过的值不动。
_MIGRATE = {
    # v2：老默认 scale=2.0（上轮实验遗留）/ zoom=1.75（上轮为"人太大"调远）,
    #     现在窗口按模型收紧，这两个值都会让画面偏小、留白偏多，收回新默认。
    2: {
        "scale": {2.0: 1.0},
        "zoom": {1.75: 1.38},
    },
    # v3：比例改全动画包围盒后窗口会变宽；把 v2 留下的 zoom 1.38 收口到
    #     当前默认 1.42（两者观感接近，1.42 人物略小一点、留白更透气）。
    3: {
        "zoom": {1.38: 1.42},
    },
    # v4：视线追踪改为「正面朝向补偿 + 对称摆幅」。老的 max_yaw=62 是
    #     "围绕静止朝向"的摆幅（且被 cos(cam_yaw) 削过一次），语义已变，
    #     收口到新的 48（围绕"正对镜头"中位的摆幅）。
    4: {
        "gaze_max_yaw": {62.0: 48.0},
    },
    # v5：常驻并行层（pre_parallel/parallel）默认值 True → False。游戏里这些
    #     层由玩家状态 molang 驱动，桌宠里同一条表达式算出来的是「隐藏」，
    #     结果身体被整块藏掉（慧慧1.2 只剩一个头）。老配置只要还正好是旧的
    #     默认 true，就说明用户没主动开过，刷成 false。
    5: {
        "parallel_enabled": {True: False},
    },
}


def _path():
    base = os.path.join(os.path.expanduser("~"), ".workbuddy", "ysm_pet")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "settings.json")


def load():
    """读设置，缺失项用默认值补齐（含一次性的老配置迁移）。"""
    cfg = dict(_DEFAULT)
    p = _path()
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as fp:
                data = json.load(fp)
            if isinstance(data, dict):
                for k in _DEFAULT:
                    if k in data:
                        cfg[k] = data[k]
                _migrate(cfg)
        except (OSError, ValueError):
            pass
    return cfg


def _migrate(cfg):
    """把旧版本留下的值刷新到新语义（就地改 cfg）。"""
    ver = int(cfg.get("cfg_version", 1) or 1)
    if ver >= _DEFAULT["cfg_version"]:
        return
    for _v, rules in sorted(_MIGRATE.items()):
        if ver >= _v:
            continue
        for key, remap in rules.items():
            if cfg.get(key) in remap:
                cfg[key] = remap[cfg[key]]
    cfg["cfg_version"] = _DEFAULT["cfg_version"]


def save(cfg):
    """只写已知键，原子替换防写坏。"""
    out = {k: cfg[k] for k in _DEFAULT if k in cfg}
    p = _path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return p


def update(**kw):
    """原地改几项并保存。"""
    cfg = load()
    cfg.update(kw)
    save(cfg)
    return cfg
