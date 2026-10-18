"""桌宠素材加载：解析 .ysm（已解密）→ 模型 + 贴图 + 需隐藏骨骼。

流程（对应需求「先读取ysm文档，加载贴图，读取骨骼」）：
    1. ysmbin.deserialize()  —— 读二进制，解出烘焙面 + 动画 + 贴图字节
    2. YsmModel(raw)         —— 建骨骼树（name/parent/pivot/cubes/faces）
    3. 贴图                  —— .ysm 内嵌 PNG 字节，或外部 tex.png
    4. hide_bones            —— 表情/备用部件骨骼，渲染时排除
"""
import io
import os

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _sys_path():
    import sys
    for p in (ROOT, os.path.join(ROOT, "src")):
        if p not in sys.path:
            sys.path.insert(0, p)


def load_model(model_file="亚托莉目标模型.bin", decrypted_dir=None):
    """返回 (raw, YsmModel)。"""
    _sys_path()
    from ysm import ysmbin as B
    from ysm import ysmmodel as YM

    d = decrypted_dir or os.path.join(ROOT, "data", "decrypted")
    path = model_file if os.path.isabs(model_file) else os.path.join(d, model_file)
    with open(path, "rb") as fp:
        raw = B.deserialize(fp.read())
    return raw, YM.YsmModel(raw, "main_model")


def _decode_image(data, w=0, h=0):
    """YSM 内嵌贴图有三种形态（实测）：
        1) 带文件头的压缩图 —— PNG / WebP / JPEG…（现代格式 image_format=2/4 均属此类）；
        2) 裸 RGBA 字节流（亚托莉就是这种，256*256*4=262144，与 outputs/step4/tex.png 逐字节相同）；
        3) 裸 RGB。
    """
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return (np.asarray(img.convert("RGBA"), np.uint8),
                (img.format or "image").lower())
    except Exception:
        pass                            # 不是已知图片格式 → 当裸像素处理
    n = len(data)
    if w > 0 and h > 0 and n == w * h * 4:
        return np.frombuffer(data, np.uint8).reshape(h, w, 4).copy(), "rawRGBA"
    if w > 0 and h > 0 and n == w * h * 3:
        a = np.frombuffer(data, np.uint8).reshape(h, w, 3)
        return np.dstack([a, np.full((h, w), 255, np.uint8)]), "rawRGB"
    # 边长未知时按正方形猜
    side = int(round((n / 4.0) ** 0.5))
    if side * side * 4 == n:
        return np.frombuffer(data, np.uint8).reshape(side, side, 4).copy(), "rawRGBA?"
    raise ValueError("无法识别贴图格式：%d 字节, w=%d h=%d" % (n, w, h))


def load_texture(raw=None, tex_png=None, prefer_embedded=True):
    """贴图来源二选一：.ysm 内嵌（默认）或外部 PNG。返回 (uint8 RGBA, 来源说明)。"""
    if prefer_embedded and raw is not None and getattr(raw, "textures", None):
        props = getattr(raw, "properties", {}) or {}
        name = props.get("default_texture")
        tex = raw.textures.get(name) if name in raw.textures else None
        if tex is None:
            # 跳过箭头之类的非角色贴图
            tex = next((t for k, t in raw.textures.items()
                        if not k.startswith("/")),
                       next(iter(raw.textures.values()), None))
        if tex and tex.get("data"):
            arr, kind = _decode_image(tex["data"], tex.get("width", 0),
                                      tex.get("height", 0))
            return arr, "内嵌:%s(%s)" % (tex.get("name"), kind)
    path = tex_png or os.path.join(ROOT, "outputs", "step4", "tex.png")
    return np.asarray(Image.open(path).convert("RGBA"), np.uint8), path


def load_hide_bones(cfg_path=None):
    _sys_path()
    import yaml
    cfg_path = cfg_path or os.path.join(ROOT, "configs", "joints9.yaml")
    with open(cfg_path, encoding="utf-8") as fp:
        cfg = yaml.safe_load(fp)
    return list(cfg.get("hide_bones", [])), cfg


def load_all(model_file="亚托莉目标模型.bin", tex_png=None):
    """一步到位：返回 dict(model, tex, tex_src, hide, cfg, raw)。"""
    raw, model = load_model(model_file)
    tex, src = load_texture(raw, tex_png)
    hide, cfg = load_hide_bones()
    return {"raw": raw, "model": model, "tex": tex, "tex_src": src,
            "hide": hide, "cfg": cfg}
