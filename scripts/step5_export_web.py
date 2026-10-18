"""阶段 4：导出 GLB + 骨架清单，供 TypeScript GUI 使用。

产出
    web/public/model.glb            带蒙皮模型（rest = 零位姿）
    web/public/rig.json             9 关节定义 + 表情槽位 + 隐藏骨骼表
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from PIL import Image  # noqa: E402

from env.stand_walk import load_yaml  # noqa: E402
from export.gltf_export import export_glb  # noqa: E402
from phys.bind import Skeleton9  # noqa: E402
from ysm import ysmbin as B  # noqa: E402
from ysm import ysmmodel as YM  # noqa: E402
from ysm.expression import ExpressionRig  # noqa: E402

OUT = os.path.join(ROOT, "web", "public")


def main():
    os.makedirs(OUT, exist_ok=True)
    raw = B.deserialize(open(os.path.join(ROOT, "data", "decrypted",
                                          "亚托莉目标模型.bin"), "rb").read())
    model = YM.YsmModel(raw, "main_model")
    sk = Skeleton9(model)
    rig = ExpressionRig(model)

    # 贴图（取最大那张）
    best, bs = None, -1
    tmp = os.path.join(ROOT, "outputs", "step4", "tex.png")
    for name, tex in raw.textures.items():
        w, h = tex["width"], tex["height"]
        if w * h > bs and tex["data"]:
            best, bs = (name, tex), w * h
    name, tex = best
    img = Image.frombytes("RGBA", (tex["width"], tex["height"]), tex["data"])
    img.save(tmp)
    print("贴图:", name, (tex["width"], tex["height"]))

    glb = os.path.join(OUT, "model.glb")
    export_glb(model, tmp, glb, hide_bones=sk.cfg["hide_bones"])
    print("GLB:", glb, os.path.getsize(glb), "bytes",
          "primitives =", sum(1 for b in model.order
                              if b["cubes"] and b["name"] not in set(sk.cfg["hide_bones"])))

    # rig 清单
    rig_cfg = load_yaml("expressions.yaml")
    rig_doc = {
        "model": sk.cfg["model"],
        "jointOrder": sk.joint_names,
        "joints": [
            {"name": j["name"], "label": j["label"], "bone": j["bone"],
             "limits": j["limits"], "default": j["default"]}
            for j in sk.joints
        ],
        "segments": list(sk.segments.keys()),
        "expressions": {
            "enabled": bool(rig_cfg.get("enabled", False)),
            "slots": rig.slots(),
        },
        "hiddenBones": sorted(sk.hidden),
    }
    with open(os.path.join(OUT, "rig.json"), "w", encoding="utf-8") as fp:
        json.dump(rig_doc, fp, ensure_ascii=False, indent=2)
    print("rig ->", os.path.join(OUT, "rig.json"))


if __name__ == "__main__":
    main()
