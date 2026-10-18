"""阶段 4：表情接口（预留）。

YSM 的表情本质是**显隐**：作者把多种眉/眼/嘴做成并列骨骼组，
用 scale=(0,0,0) 隐藏不需要的。因此接口是「槽位 + 互斥选项」：

    rig = ExpressionRig(model)
    rig.set_slot("mouth", "m11")       # 张嘴
    rig.slots()                        # GUI 用：槽位与可选值
    anim = rig.to_anim()               # -> {bone: {"scale":[0,0,0]}} 叠加进姿态

训练（站立/行走）不使用表情；GUI 摆 pose 与后续动画播放时才启用。
"""
import os

import yaml  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class ExpressionRig:
    def __init__(self, model=None, cfg_path=None):
        cfg_path = cfg_path or os.path.join(ROOT, "configs", "expressions.yaml")
        with open(cfg_path, encoding="utf-8") as fp:
            self.cfg = yaml.safe_load(fp)
        self.model = model
        self.enabled = bool(self.cfg.get("enabled", False))
        self.slots_def = self.cfg["slots"]
        # 校验：所有选项骨骼必须存在于模型
        self._valid = {}
        for slot, sdef in self.slots_def.items():
            opts = {}
            for name, odef in sdef["options"].items():
                bones = [b for b in odef["bones"]
                         if model is None or b in model.by_name]
                if bones:
                    opts[name] = bones
            self._valid[slot] = opts
        self.state = {s: d.get("default") for s, d in self.slots_def.items()}
        self.state = {s: (v if v in self._valid[s] else next(iter(self._valid[s])))
                      for s, v in self.state.items() if self._valid[s]}

    # ------------------------------------------------------------------
    def slots(self):
        """GUI 用：[{slot,label,options,default}]"""
        return [{"slot": s, "label": self.slots_def[s].get("label", s),
                 "default": self.slots_def[s].get("default"),
                 "options": [{"name": n, "bones": b} for n, b in self._valid[s].items()]}
                for s in self.slots_def if self._valid[s]]

    def set_slot(self, slot, option):
        if slot not in self._valid:
            raise KeyError(f"未知表情槽: {slot}")
        if option not in self._valid[slot]:
            raise KeyError(f"槽 {slot} 没有选项 {option}（可选: {list(self._valid[slot])}）")
        self.state[slot] = option

    def get_slot(self, slot):
        return self.state.get(slot)

    # ------------------------------------------------------------------
    def hidden(self):
        """当前应隐藏的骨骼：每个槽里除选中项外的全部骨骼。"""
        out = set()
        for slot, cur in self.state.items():
            for name, bones in self._valid[slot].items():
                if name != cur:
                    out.update(bones)
        return out

    def to_anim(self):
        """叠加进 YSM 动画字典的显隐层。"""
        if not self.enabled:
            return {}
        return {b: {"scale": [0.0, 0.0, 0.0]} for b in self.hidden()
                if self.model is None or b in self.model.by_name}

    # ------------------------------------------------------------------
    def summary(self):
        return {"enabled": self.enabled,
                "state": dict(self.state),
                "slots": self.slots()}
