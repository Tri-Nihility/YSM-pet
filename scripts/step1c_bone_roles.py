"""
第一步c：生成骨骼角色清单 -> outputs/bone_roles.csv

回答"203 根骨骼到底哪些参与运动模型"：
  - 有无几何 / cube 数 / 树深
  - 被哪些动画文件、哪些片段驱动
  - 是否被 scale=0 隐藏（显隐开关件，如 hat / Mask / Background1）
  - 是否表情 / 毛发尾巴衣摆 / 挂点（启发式命名分组）
  - 是否身体运动核心候选（CORE_BONES）

用法：python scripts/step1c_bone_roles.py
输出：outputs/bone_roles.csv
"""
import csv
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "临时文件（记得整理掉", "苦力怕狐")
MODEL = os.path.join(PKG, "models", "main.json")
ANIM_DIR = os.path.join(PKG, "animations")
OUT = os.path.join(ROOT, "outputs", "bone_roles.csv")
os.makedirs(os.path.dirname(OUT), exist_ok=True)

CORE_BONES = {
    "UpBody", "UpperBody", "Body", "AllHead", "Head", "Head2",
    "LeftArm", "LeftForeArm", "LeftHand", "RightArm", "RightForeArm", "RightHand",
    "LeftLeg", "LeftLowerLeg", "LeftFoot", "RightLeg", "RightLowerLeg", "RightFoot",
}
FX_BONES = {"Background1", "curtain", "white_curtain", "MooncakleH", "MooncakleH2",
            "ysmGlowTnt_Right_bone", "NysmGlowTnt_Right_bone"}

# 启发式分组：这些是"结果"而非"原因"，不该进运动模型
FACE_PAT = re.compile(r"Eye|Eyelid|Eyebrow|Mouth|Expression|weixiao|jingya|Bangs|Ear|ear", re.I)
HAIR_PAT = re.compile(r"Hair|Bangs|Tail|Clothe|Dress|Cloak|Ribbon|DaiMao|hat", re.I)


def frames(ch):
    if isinstance(ch, str):
        try:
            f = float(ch)
            return [(0.0, [f, f, f])]
        except ValueError:
            return []                       # molang 表达式
    if isinstance(ch, (int, float)):
        return [(0.0, [ch, ch, ch])]
    if isinstance(ch, list):
        if len(ch) == 3 and all(isinstance(x, (int, float)) for x in ch):
            return [(0.0, ch)]
        return [(float(i), v) for i, v in enumerate(ch)]
    out = []
    for k, v in ch.items():
        if isinstance(v, (int, float)):
            v = [v, v, v]
        out.append((float(k), v))
    return out


def main():
    geo = json.load(open(MODEL, encoding="utf-8"))["minecraft:geometry"][0]
    bones = geo["bones"]
    depth = {}

    def d(n):
        if n in depth:
            return depth[n]
        b = next((x for x in bones if x["name"] == n), None)
        if b is None or not b.get("parent"):
            depth[n] = 0
        else:
            depth[n] = d(b["parent"]) + 1
        return depth[n]

    # 动画驱动关系
    anim_files = []
    for root, _, fs in os.walk(ANIM_DIR):
        for f in fs:
            if f.endswith(".json"):
                anim_files.append(os.path.join(root, f))
    anim_files.sort()
    driven_by, clips_of = {}, {}
    scale0 = {}
    for path in anim_files:
        rel = os.path.relpath(path, ANIM_DIR)
        data = json.load(open(path, encoding="utf-8"))
        for aname, a in data.get("animations", {}).items():
            for bn, ch in (a.get("bones") or {}).items():
                driven_by.setdefault(bn, set()).add(rel)
                clips_of.setdefault(bn, set()).add(f"{rel}:{aname}")
                if "scale" in ch:
                    fr = frames(ch["scale"])
                    if fr and all(all(x == 0 for x in v) for _, v in fr):
                        scale0.setdefault(bn, set()).add(aname)

    rows = []
    for b in bones:
        n = b["name"]
        ncubes = len(b.get("cubes") or [])
        anim = sorted(driven_by.get(n, []))
        cat = []
        if ncubes == 0:
            cat.append("挂点/组织")
        if n in CORE_BONES:
            cat.append("核心运动")
        elif n in FX_BONES:
            cat.append("特效件")
        elif FACE_PAT.search(n):
            cat.append("表情")
        elif HAIR_PAT.search(n):
            cat.append("毛发衣摆")
        if n in scale0:
            cat.append("显隐开关")
        if not anim and ncubes:
            cat.append("静态装饰")
        rows.append({
            "name": n,
            "parent": b.get("parent", ""),
            "depth": d(n),
            "n_cubes": ncubes,
            "category": "|".join(cat) or "普通",
            "n_clips": len(clips_of.get(n, [])),
            "anim_files": ";".join(anim),
            "hidden_by_scale0": ";".join(sorted(scale0.get(n, []))),
            "in_core_set": "1" if n in CORE_BONES else "",
        })

    with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # 摘要
    from collections import Counter
    cnt = Counter(r["category"].split("|")[0] for r in rows)
    print(f"骨骼 {len(rows)} 根 -> {os.path.relpath(OUT, ROOT)}\n")
    print("分类（按首个标签）:")
    for k, v in cnt.most_common():
        print(f"  {k:<10} {v}")
    core = [r for r in rows if r["in_core_set"]]
    print(f"\n核心运动集 {len(core)} 根，其中被动画驱动的 "
          f"{sum(1 for r in core if r['n_clips'])} 根：")
    print("  " + ", ".join(r["name"] for r in core))
    vis = [r for r in rows if r["hidden_by_scale0"]]
    print(f"\n被 scale=0 隐藏过的骨骼 {len(vis)} 根（显隐开关件）：")
    for r in vis:
        print(f"  {r['name']:<24} {r['hidden_by_scale0']}")


if __name__ == "__main__":
    main()
