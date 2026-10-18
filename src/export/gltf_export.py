"""阶段 4：把 YSM 模型导出为带蒙皮的 GLB，供 TypeScript GUI 用 three.js 渲染。

为什么可以这样导出
------------------
上游已确认：烘焙顶点就在模型空间，骨骼矩阵 rest = 单位阵（无 inverse-bind 需求）。
导出成 glTF 时把「骨骼 = 挂在枢轴上的 node」：

    node.translation = pivot_child - pivot_parent      （rest 与动画都不变）
    node.rotation    = 动画四元数                       （rest = 单位）
    node.scale       = 1
    IBM_i            = T(-pivot_i)                      （rest 全局变换的逆）

则蒙皮链条  v_world = Σ w_i · G_i(t) · IBM_i · v  与上游 FK
    v_world = M_i(t) · v （M 的 rest = I）
在「node 局部 = T(Δ)·R·S」这一标准 TRS 顺序下**逐项等价**（已在模块文档推过）。

每个带网格的骨骼拆成一个 primitive（刚性权重：本骨骼 1.0），GUI 显隐表情
只需对 primitive.visible 取反，与 YSM 的 scale=0 显隐完全对应。
"""
import json
import math
import os
import struct

import numpy as np
from PIL import Image  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _pivot(model, bone):
    return np.asarray(model.by_name[bone]["pivot"], float) / 16.0


def export_glb(model, tex_path, out_path, hide_bones=(), tex_name="model.png"):
    """model: ysmmodel.YsmModel；tex_path: RGBA PNG。"""
    tw, th = int(model.tw), int(model.th)

    # ------------------------------------------------ 骨骼 node 层级
    names = [b["name"] for b in model.order]           # 拓扑序
    node_of = {n: i for i, n in enumerate(names)}
    nodes = []
    roots = []
    for b in model.order:
        n = b["name"]
        piv = _pivot(model, n)
        par = b.get("parent") or ""
        trans = piv if par not in node_of else piv - _pivot(model, par)
        entry = {
            "name": n,
            "translation": [round(float(x), 6) for x in trans],
        }
        kids = [node_of[c["name"]] for c in model.bones
                if (c.get("parent") or "") == n]
        if kids:
            entry["children"] = kids
        if par not in node_of:
            roots.append(node_of[n])
        nodes.append(entry)

    # ------------------------------------------------ 顶点（按骨骼分组）
    hide = set(hide_bones)
    groups = {}                                        # bone -> dict of arrays
    for b in model.order:
        n = b["name"]
        if n in hide or not b["cubes"]:
            continue
        pos, uv, nrm = [], [], []
        for c in b["cubes"]:
            for f in c["faces"]:
                for v, t in zip(f["pos"], f["uv"]):
                    pos.append(v)
                    uv.append([t[0] * tw / tw, t[1] * th / th])  # 归一化（数值上与原 uv 相同）
                nrm.append(f["normal"])
        groups[n] = {
            "pos": np.asarray(pos, np.float32),
            "uv": np.asarray(uv, np.float32),
            "nrm": np.asarray(nrm, np.float32),
        }

    # ------------------------------------------------ buffer 布局
    bin_data = bytearray()
    buffer_views = []
    accessors = []

    def add_view(data, target=None):
        while len(bin_data) % 4:
            bin_data.append(0)
        off = len(bin_data)
        bin_data.extend(data)
        v = {"buffer": 0, "byteOffset": off, "byteLength": len(data)}
        if target is not None:
            v["target"] = target
        buffer_views.append(v)
        return len(buffer_views) - 1

    # 贴图
    img = open(tex_path, "rb").read()
    img_view = add_view(img)

    prim_bone = []
    meshes = []
    for bone, g in groups.items():
        n_v = len(g["pos"])
        if n_v == 0:
            continue
        mn = g["pos"].min(0)
        mx = g["pos"].max(0)
        # pos
        pv = add_view(g["pos"].tobytes(), 34962)
        accessors.append({"bufferView": pv, "componentType": 5126, "count": n_v,
                          "type": "VEC3", "min": [float(x) for x in mn],
                          "max": [float(x) for x in mx]})
        pi = len(accessors) - 1
        # nrm
        nv = add_view(g["nrm"].tobytes(), 34962)
        accessors.append({"bufferView": nv, "componentType": 5126, "count": n_v,
                          "type": "VEC3"})
        ni = len(accessors) - 1
        # uv
        uv = add_view(g["uv"].astype(np.float32).tobytes(), 34962)
        accessors.append({"bufferView": uv, "componentType": 5126, "count": n_v,
                          "type": "VEC2"})
        ui = len(accessors) - 1
        # joints (ushort4，骨骼数可超 255) + weights (float4)
        jt = np.zeros((n_v, 4), np.uint16)
        jt[:, 0] = node_of[bone]
        jv = add_view(jt.tobytes(), 34962)
        accessors.append({"bufferView": jv, "componentType": 5123, "count": n_v,
                          "type": "VEC4"})
        ji = len(accessors) - 1
        wt = np.zeros((n_v, 4), np.float32)
        wt[:, 0] = 1.0
        wv = add_view(wt.tobytes(), 34962)
        accessors.append({"bufferView": wv, "componentType": 5126, "count": n_v,
                          "type": "VEC4"})
        wi = len(accessors) - 1
        # indices (两个三角)
        idx = []
        for f in range(n_v // 4):
            b0 = f * 4
            idx += [b0, b0 + 1, b0 + 2, b0, b0 + 2, b0 + 3]
        iv = add_view(np.asarray(idx, np.uint32).tobytes(), 34963)
        accessors.append({"bufferView": iv, "componentType": 5125,
                          "count": len(idx), "type": "SCALAR"})
        ii = len(accessors) - 1

        # 每个骨骼一个 mesh（node.extras.bone 供 GUI 按骨骼显隐表情）
        meshes.append({
            "name": f"body_{bone}",
            "primitives": [{
                "attributes": {"POSITION": pi, "NORMAL": ni, "TEXCOORD_0": ui,
                               "JOINTS_0": ji, "WEIGHTS_0": wi},
                "indices": ii,
                "material": 0,
                "mode": 4,
            }],
        })
        prim_bone.append(bone)

    # ------------------------------------------------ IBM（单位逆 = T(-piv)）
    ibm = np.zeros((len(names), 16), np.float32)
    for n in names:
        piv = _pivot(model, n)
        m = np.eye(4, dtype=np.float32)
        m[:3, 3] = -piv
        ibm[node_of[n]] = m.reshape(-1)
    iview = add_view(ibm.tobytes())
    accessors.append({"bufferView": iview, "componentType": 5126,
                      "count": len(names), "type": "MAT4"})
    ibm_i = len(accessors) - 1

    # 每个 mesh 挂一个 node，extras.bone = 骨骼名（GUI 表情显隐用）
    base_node = len(nodes)
    for k, bone in enumerate(prim_bone):
        nodes.append({"name": f"mesh_{bone}", "skin": 0, "mesh": k,
                      "extras": {"bone": bone}})
    scene_root = len(nodes)
    nodes.append({"name": "YSM_Root",
                  "children": roots + list(range(base_node, len(nodes) - 1))})

    gltf = {
        "asset": {"version": "2.0",
                  "generator": "YSM-Motion step4 gltf exporter"},
        "scene": 0,
        "scenes": [{"nodes": [scene_root]}],
        "nodes": nodes,
        "meshes": meshes,
        "skins": [{
            "joints": [node_of[n] for n in names],
            "inverseBindMatrices": ibm_i,
        }],
        "materials": [{
            "name": "YSM_Main",
            "pbrMetallicRoughness": {
                "baseColorTexture": {"index": 0},
                "metallicFactor": 0.0,
                "roughnessFactor": 0.9,
            },
            "alphaMode": "MASK",
            "alphaCutoff": 0.05,
            "doubleSided": True,
        }],
        "textures": [{"sampler": 0, "source": 0}],
        "images": [{"bufferView": img_view, "mimeType": "image/png"}],
        "samplers": [{"magFilter": 9729, "minFilter": 9987,
                      "wrapS": 33071, "wrapT": 33071}],
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(bin_data)}],
    }

    # ------------------------------------------------ 打包 GLB
    js = json.dumps(gltf, separators=(",", ":")).encode()
    while len(js) % 4:
        js += b" "
    while len(bin_data) % 4:
        bin_data.append(0)
    total = 12 + 8 + len(js) + 8 + len(bin_data)
    with open(out_path, "wb") as fp:
        fp.write(struct.pack("<III", 0x46546C67, 2, total))
        fp.write(struct.pack("<II", len(js), 0x4E4F534A))
        fp.write(js)
        fp.write(struct.pack("<II", len(bin_data), 0x004E4942))
        fp.write(bin_data)
    return out_path
