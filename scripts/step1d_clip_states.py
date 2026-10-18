"""
第一步d：把模型里每一段动画归类到「常态 / 动作 / 额外渲染 / 界面」
并输出到 outputs/clip_states.csv。

分类依据是上游源码：
  common/.../client/animation/AnimationRegister.java      -> main 层的 19 个位移状态
  common/.../geckolib3/core/controller/controllers/PlayerAnimationController.java -> 控制器管线
  client/model/processor/ParallelProcessor.java           -> parallel / pre_parallel 槽位

用法：python scripts/step1d_clip_states.py
"""
import csv
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "临时文件（记得整理掉", "苦力怕狐")
ANIM_DIR = os.path.join(PKG, "animations")
OUT = os.path.join(ROOT, "outputs", "clip_states.csv")
os.makedirs(os.path.dirname(OUT), exist_ok=True)

# AnimationRegister.java L17-43 的 19 项，按优先级从高到低
MAIN_STATES = {
    "death": "死亡", "riptide": "三叉戟旋冲", "sleep": "睡觉", "swim": "游泳",
    "climb": "爬动", "climbing": "攀爬静止", "ladder_up": "上梯", "ladder_stillness": "梯上静止",
    "ladder_down": "下梯", "fly": "飞行", "elytra_fly": "鞘翅", "swim_stand": "水中站立",
    "attacked": "受击", "jump": "跳跃", "sneak": "潜行移动", "sneaking": "潜行",
    "run": "奔跑", "walk": "行走", "idle": "常态站立",
}
MAIN_ORDER = list(MAIN_STATES)


def frames(ch):
    if isinstance(ch, str):
        return [(0.0, [ch, ch, ch])]
    if isinstance(ch, (int, float)):
        return [(0.0, [ch, ch, ch])]
    if isinstance(ch, list):
        if len(ch) == 3 and all(isinstance(x, (int, float)) for x in ch):
            return [(0.0, ch)]
        return [(float(i), v) for i, v in enumerate(ch)]
    out = []
    for k, v in ch.items():
        out.append((float(k), v if isinstance(v, list) else [v, v, v]))
    return out


def classify(name, rel):
    """返回 (类别, 所属层, 说明)"""
    tail = name.split(":")[-1].replace("molang&", "")

    if rel.startswith("vehicle"):
        return ("载具", "vehicle", "坐骑模型动画")
    if rel.startswith("tlm"):
        return ("额外动作", "TLM 互动", "坐下/下棋/野餐/弹琴等")
    if rel.startswith("extra") or name in ("开关兜帽(按钮)", "月饼"):
        return ("额外动作", "extra 轮盘", "玩家轮盘主动触发")
    if name in ("关兜帽", "开兜帽", "head:default") or "parallel:" in name \
            or name.startswith("pre_parallel") or re.match(r"^parallel\d*$", name):
        return ("额外渲染", "parallel/pre_parallel", "显隐切换 / 条件叠加层")
    if name in ("sit", "ride", "ride_pig", "boat", "has_ride", "forward"):
        return ("骑乘姿态", "vehicle/pose", "坐姿 / 骑猪 / 乘船")
    if tail in MAIN_STATES or name in MAIN_STATES:
        return ("位移动作", "main", MAIN_STATES.get(tail) or MAIN_STATES[name])
    if name.startswith("new_idle") or tail.startswith("new_idle"):
        return ("常态变体", "post_main", "idle 轮盘随机插播")
    if name.startswith("gui") or name.startswith("preview"):
        return ("界面预览", "gui_hover/gui_focus", "模型选择界面")
    if name.startswith(("hold_", "swing", "use_", "arm:", "molang&arm:", "carryon", "carry_")):
        return ("动作叠加", "hold/swing/use", "持物 / 挥击 / 使用")
    if name.startswith("tac") or tail.startswith("tac:"):
        return ("动作叠加", "fire(TAC)", "枪械模组持枪动作")
    if name.startswith(("extra", "molang&extra", "pre_parallel")) or "extra" in name:
        return ("额外动作", "extra 轮盘", "玩家主动触发")
    if name.startswith("sleep") or "sleep" in tail:
        return ("额外动作", "extra 轮盘", "睡姿变体")
    if name in ("attack_empty", "attack1", "attack2", "attack3", "fishing", "not_fishing"):
        return ("动作叠加", "post_swing", "攻击/钓鱼占位")
    if name.startswith("vehicle") or "$" in name:
        return ("额外动作", "vehicle", "载具 / 第三方整合")
    return ("未归类", "?", "")


def main():
    files = []
    for root, _, fs in os.walk(ANIM_DIR):
        for f in fs:
            if f.endswith(".json"):
                files.append(os.path.join(root, f))
    files.sort()

    rows = []
    for path in files:
        rel = os.path.relpath(path, ANIM_DIR)
        data = json.load(open(path, encoding="utf-8"))
        for aname, a in data.get("animations", {}).items():
            bones = a.get("bones") or {}
            chans, nkeys, molang = {}, 0, 0
            for bn, ch in bones.items():
                for c, v in ch.items():
                    chans[c] = chans.get(c, 0) + 1
                    fr = frames(v)
                    nkeys = max(nkeys, len(fr))
                    if isinstance(v, dict):
                        for _, val in fr:
                            if isinstance(val, list) and any(isinstance(x, str) for x in val):
                                molang += 1
                    elif isinstance(v, str):
                        molang += 1
            cat, layer, note = classify(aname, rel)
            rows.append({
                "clip": aname,
                "file": rel,
                "category": cat,
                "layer": layer,
                "note": note,
                "length_s": a.get("animation_length", 0),
                "loop": str(a.get("loop", "")),
                "n_bones": len(bones),
                "n_keys": nkeys,
                "channels": ";".join(sorted(chans)),
                "has_molang": "1" if molang else "",
            })

    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    from collections import Counter
    print(f"共 {len(rows)} 段动画 -> {os.path.relpath(OUT, ROOT)}\n")
    for cat, n in Counter(r["category"] for r in rows).most_common():
        sub = Counter(r["note"] or r["layer"] for r in rows if r["category"] == cat)
        print(f"[{cat}] {n} 段")
        for k, v in sub.most_common():
            print(f"    {k:<22} {v}")

    miss = sorted(MAIN_STATES.keys() - {r["clip"] for r in rows})
    print(f"\nmain 层定义但本模型缺失的位移状态: {miss}")
    have = [(k, MAIN_STATES[k]) for k in MAIN_ORDER if k in {r['clip'] for r in rows}]
    print(f"main 层本模型已实现 {len(have)}/{len(MAIN_ORDER)}:")
    print("    " + ", ".join(f"{k}({v})" for k, v in have))
    print(f"\n含 molang 关键帧的片段 {sum(1 for r in rows if r['has_molang'])} 段")


if __name__ == "__main__":
    main()
