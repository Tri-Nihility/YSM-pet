"""
.ysm 二进制载荷反序列化器 —— 逐行移植上游 Java。

上游依据（YSM-Liaison/mod/common/src/main/java/com/elfmcys/yesstevemodel/resource/）：
  YSMBinaryDeserializer.java  deserializeLegacyV15 / parseModels / parseAnimations /
                              parseChannel / parseAnimationController* / parseYSMJson
  rip/ysm/security/YSMByteBuf.java   LITTLE_ENDIAN、readDword/readVarInt/readString/readFloat/readByteArray

字节序：小端（上游注释：“YSM的基於C++，使用小端序”）。
"""
import ctypes
import os
import struct

# 动画类型 -> 逻辑名（YSMFolderDeserializer.getAnimKeyFromType）
ANIM_KEY_FROM_TYPE = {
    1: "main", 2: "arm", 3: "extra", 4: "tac", 5: "arrow",
    6: "carryon", 7: "parcool", 8: "swem", 9: "slashblade", 10: "tlm",
    11: "fp_arm", 12: "immersive_melodies", 13: "irons_spell_books",
}

# 模型类型 -> 槽位（YSMBinaryDeserializer.assignMainModels）
MODEL_SLOT_FROM_TYPE = {1: "mainModel", 2: "armModel", 3: "minecraft:arrow"}


class Reader:
    """等价于 YSMByteBuf（小端）。"""

    __slots__ = ("b", "pos")

    def __init__(self, data: bytes, pos: int = 0):
        self.b = data
        self.pos = pos

    # ---- 原语 ----
    def byte(self) -> int:
        v = self.b[self.pos]
        self.pos += 1
        return v

    def dword(self) -> int:
        v = struct.unpack_from("<I", self.b, self.pos)[0]
        self.pos += 4
        return v

    def float(self) -> float:
        v = struct.unpack_from("<f", self.b, self.pos)[0]
        self.pos += 4
        return v

    def varint(self) -> int:
        """Java readVarInt：累加到 int（按 32 位回绕），返回有符号值。"""
        value = 0
        position = 0
        while True:
            cb = self.b[self.pos]
            self.pos += 1
            value |= (cb & 0x7F) << position
            if (cb & 0x80) == 0:
                break
            position += 7
            if position >= 64:
                raise ValueError("VarInt too big")
        # 与 Java int 一致：按 32 位有符号解释
        value &= 0xFFFFFFFF
        return value - 0x100000000 if value & 0x80000000 else value

    def varlong(self) -> int:
        value = 0
        position = 0
        while True:
            cb = self.b[self.pos]
            self.pos += 1
            value |= (cb & 0x7F) << position
            if (cb & 0x80) == 0:
                break
            position += 7
            if position >= 64:
                raise ValueError("VarLong too big")
        return value

    def string(self) -> str:
        n = self.varint()
        if n == 0:
            return ""
        s = self.b[self.pos:self.pos + n].decode("utf-8", errors="replace")
        self.pos += n
        return s

    def byte_array(self) -> bytes:
        n = self.varint()
        if n == 0:
            return b""
        v = self.b[self.pos:self.pos + n]
        self.pos += n
        return v

    def vec3(self):
        v = struct.unpack_from("<3f", self.b, self.pos)
        self.pos += 12
        return v

    def skip(self, n: int):
        self.pos += n

    def offset(self) -> int:
        return self.pos


class RawYsmModel:
    """RawYsmModel 的 Python 等价（只保留训练/渲染需要的字段）。"""

    def __init__(self, fmt: int):
        self.format_version = fmt
        self.main_model = None       # type 1
        self.arm_model = None        # type 2
        self.projectiles = {}        # identifier -> geo
        self.animations = {}         # animKey -> {animName: anim}
        self.controllers = {}        # name -> {animName: controller}
        self.textures = {}           # name -> {name,data,width,height,sub:[...]}
        self.sounds = {}             # name -> bytes
        self.avatars = []            # [{name,data,width,height}]
        self.metadata = {}
        self.properties = {}
        # 下面三段只有现代格式（format > 15）才有，legacy 保持空
        self.vehicles = {}           # identifier -> 子实体（载具/投射物）
        self.functions = {}          # name -> {hash, data}
        self.languages = {}          # name -> {hash, nodes}
        self.end_offset = -1
        self.footer = {}             # parseYSMFooter 的结果（载荷尾部）
        self.footer_offset = -1      # 尾部 footer 读完的位置


# ------------------------------------------------------------------ 几何
def parse_models(r: Reader, fmt: int) -> dict:
    """parseModels：烘焙后的面（顶点 + UV），不再是 cube 定义。"""
    geo = {"bones": [], "identifier": "", "texture_size": None}
    bone_count = r.varint()
    for _ in range(bone_count):
        bone = {"parent": r.string(), "cubes": []}
        cube_count = r.varint()
        for _ in range(cube_count):
            cube = {"faces": [], "unk": None}
            face_count = r.varint()
            for _ in range(face_count):
                normal = r.vec3()
                verts, uvs = [], []
                for _ in range(4):
                    verts.append(r.vec3())
                    uvs.append((r.float(), r.float()))
                cube["faces"].append({"normal": normal, "pos": verts, "uv": uvs})
            cube["unk"] = (r.varint(), r.varint(), r.varint())
            bone["cubes"].append(cube)
        bone["name"] = r.string()
        bone["pad"] = tuple(r.varint() for _ in range(5))
        bone["pivot"] = r.vec3()
        bone["rotation"] = r.vec3()
        geo["bones"].append(bone)

    geo["identifier"] = r.string()
    geo["texture_height"] = r.float()
    geo["texture_width"] = r.float()
    geo["visible_bounds_height"] = r.float()
    geo["visible_bounds_width"] = r.float()
    n = r.varint()
    geo["visible_bounds_offset"] = [r.float() for _ in range(n)]
    geo["unk_float1"] = r.float()
    geo["unk_float2"] = r.float()

    if r.varint() > 0:                     # hasInfoJsonFlag
        geo["legacy_info"] = parse_legacy_ysm_info(r)

    geo["footer_pad"] = (r.varint(), r.varint(), r.varint())
    return geo


def parse_legacy_ysm_info(r: Reader) -> dict:
    info = {"name": r.string(), "tips": r.string()}
    info["extra_animations"] = [r.string() for _ in range(r.varint())]
    info["authors"] = [r.string() for _ in range(r.varint())]
    info["license_type"] = r.string()
    info["is_free"] = r.varint() != 0
    return info


# ------------------------------------------------------------------ 动画
# ---------------------------------------------------------------- channel C 加速
# 通道解析是反序列化的绝对瓶颈：profile 显示 18201 次调用吃掉 1.38s/1.62s
# （85%）。所以把整个通道循环搬进 C（native/ysmreader.c）。
#
# ⚠️ 为什么不是"逐字段调 C"：实测逐字段 ctypes 跨界比纯 Python 还慢 3.3x
#    （见 scripts/bench_ysmbin.py），必须**一次调用吃完整通道**才有收益。
_CH_UNSET = object()
_CH_DLL = _CH_UNSET


def _ch_dll():
    """拿通道解析 DLL；不可用返回 None（自动回退 Python）。"""
    global _CH_DLL
    if _CH_DLL is not _CH_UNSET:
        return _CH_DLL
    from ysm import nativeload
    d = nativeload.load("ysmreader")
    if d is not None:
        try:
            d.ysmr_parse_channel.restype = ctypes.c_int64
            d.ysmr_parse_channel.argtypes = [
                ctypes.c_char_p, ctypes.c_size_t,
                ctypes.POINTER(ctypes.c_size_t),
                ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_int32),
                ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_float),
                ctypes.POINTER(ctypes.c_uint8),
                ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t),
                ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
                ctypes.POINTER(ctypes.c_int),
            ]
        except AttributeError:
            d = None
    _CH_DLL = d
    return _CH_DLL


def _parse_channel_c(r: Reader):
    """C 版通道解析。任何异常都返回 None 让调用方回退。"""
    d = _ch_dll()
    if d is None:
        return None
    cap = 512
    pos = ctypes.c_size_t(r.pos)
    while True:
        ts = (ctypes.c_double * cap)()
        modes = (ctypes.c_int32 * cap)()
        haspre = (ctypes.c_uint8 * cap)()
        vals = (ctypes.c_float * (cap * 6))()
        kinds = (ctypes.c_uint8 * (cap * 6))()
        soff = (ctypes.c_size_t * cap)()
        slen = (ctypes.c_size_t * cap)()
        strn = ctypes.c_size_t(0)
        err = ctypes.c_int(0)
        n = d.ysmr_parse_channel(
            r.b, ctypes.c_size_t(len(r.b)), ctypes.byref(pos),
            ts, modes, haspre, vals, kinds, soff, slen,
            ctypes.c_size_t(cap), ctypes.byref(strn), ctypes.byref(err))
        if n < 0:
            return None
        if n > cap:
            # 帧数超容量：扩大重试（pos 已被 C 改过，重来一遍）
            cap = n + 16
            pos = ctypes.c_size_t(r.pos)
            continue
        break

    # 字符串按 C 的登记顺序取出
    pool = [r.b[soff[i]:soff[i] + slen[i]] for i in range(strn.value)]
    sit = 0
    ch = []
    for i in range(n):
        kf = {"t": ts[i], "mode": modes[i]}
        first = []
        for j in range(3):
            kd = kinds[i * 6 + j]
            if kd == 1:
                first.append(pool[sit].decode("utf-8", errors="replace"))
                sit += 1
            elif kd == 0:
                first.append(vals[i * 6 + j])
            else:
                first.append(None)
        if haspre[i]:
            post = []
            for j in range(3, 6):
                kd = kinds[i * 6 + j]
                if kd == 1:
                    post.append(pool[sit].decode("utf-8", errors="replace"))
                    sit += 1
                elif kd == 0:
                    post.append(vals[i * 6 + j])
                else:
                    post.append(None)
            kf["pre"] = first
            kf["post"] = post
        else:
            kf["post"] = first
        ch.append(kf)
    r.pos = pos.value
    return ch


def parse_channel(r: Reader) -> list:
    """parseChannel：每个关键帧有 timestamp / 插值模式 / 3 分量（float 或 molang 字符串）。

    注意上游语义：
      hasPreData == False -> 只有 postData（阶梯 / 线性起点）
      hasPreData == True  -> firstData 是 **pre**（上一段终点），后面再读的是 post

    ⚠️ 默认走 **Python 内联版**（parse_channel_py），不是 C 版。
    原因（实测，scripts/bench_ysmbin.py + 交替基准）：
        朴素 Python 0.624s  /  Python 内联版 0.388s  /  C 版 0.708s
    C 版慢的原因不是 C 写得差，而是**多跑了两趟搬运**：每次调用要建 8 个
    ctypes 大数组，拿到扁平结果后还要在 Python 里重新组装成 dict —— 而
    dict 构造这件事两种方案都免不了。逐字段 ctypes 更是慢 3.3x。
    真正的瓶颈是「构造几万个 Python dict/list」，不是"字节读取慢"，
    所以正确地优化是**减少函数调用**（内联），而不是换个语言读字节。

    C 版（native/ysmreader.c）保留为已验证资产：它逐字段对齐过全部
    18201 个真实通道，需要时可显式启用做交叉校验：
        YSM_USE_C_CHANNEL=1
    """
    if os.environ.get("YSM_USE_C_CHANNEL") == "1":
        got = _parse_channel_c(r)
        if got is not None:
            return got
    return parse_channel_py(r)


_UF = struct.unpack_from


def parse_channel_py(r: Reader) -> list:
    """通道解析（纯 Python，A5 回退路径 + C 版的对照基准）。

    ⚠️ 这是**热点内联版**，不要"为了可读性"改回调用 r.varint()/r.float()。
    实测（scripts/bench_ysmbin.py）：
        朴素版（逐字段调 Reader 方法）  0.532s
        本版（内联原语 + 复用 unpack_from）0.388s   快 27%，结果逐字段相同
    省下来的全是**函数调用开销** —— 每帧要调 8 次原语，18201 个通道
    累计百万级调用，Python 的函数调用比位运算本身贵得多。

    与 `Reader` 方法的语义必须逐条对齐（见各自 docstring）：
      - varint 按 32 位回绕后解释为**有符号** int
      - typed: 0x01→float, 0x02→varint 长度前缀 UTF-8, 其它→None
      - hasPre>0 时前三个是 pre，再读三个 post；否则前三个就是 post
    """
    b = r.b
    pos = r.pos

    def _vi():
        """内联 varint（有符号语义）。"""
        nonlocal pos
        v = 0
        sh = 0
        while True:
            cb = b[pos]
            pos += 1
            v |= (cb & 0x7F) << sh
            if not (cb & 0x80):
                break
            sh += 7
        return v - 0x100000000 if v & 0x80000000 else v

    def _vu():
        """内联 varint（无符号，用于 hasPre 判定）。"""
        nonlocal pos
        v = 0
        sh = 0
        while True:
            cb = b[pos]
            pos += 1
            v |= (cb & 0x7F) << sh
            if not (cb & 0x80):
                break
            sh += 7
        return v

    def _triple():
        """3 个 typed 分量。"""
        nonlocal pos
        out = []
        for _ in range(3):
            dt = b[pos]
            pos += 1
            if dt == 1:
                out.append(_UF("<f", b, pos)[0])
                pos += 4
            elif dt == 2:
                ln = 0
                sh = 0
                while True:
                    cb = b[pos]
                    pos += 1
                    ln |= (cb & 0x7F) << sh
                    if not (cb & 0x80):
                        break
                    sh += 7
                out.append(b[pos:pos + ln].decode("utf-8", errors="replace"))
                pos += ln
            else:
                out.append(None)
        return out

    n = _vi()
    if n <= 0:
        r.pos = pos
        return []
    ch = []
    ap = ch.append
    for _ in range(n):
        t = _UF("<f", b, pos)[0] / 20.0
        pos += 4
        mode = _vi()
        first = _triple()
        if _vu() > 0:
            ap({"t": t, "mode": mode, "pre": first, "post": _triple()})
        else:
            ap({"t": t, "mode": mode, "post": first})
    r.pos = pos
    return ch


def _typed(r: Reader):
    dt = r.byte()
    if dt == 0x01:
        return r.float()
    if dt == 0x02:
        return r.string()
    return None


def parse_animations(r: Reader, fmt: int) -> dict:
    out = {}
    for _ in range(r.varint()):
        _off = r.offset()
        anim = {"name": r.string(), "length": r.float() / 20.0, "loop": r.varint()}
        if fmt > 9:
            anim["unk1"] = r.varint()
            anim["unk2"] = r.varint()
            anim["blend_weight"] = [_typed(r) for _ in range(r.varint())]
            anim["unk4"] = r.varint()
        bones = {}
        for _ in range(r.varint()):
            bn = r.string()
            bones[bn] = {"rotation": parse_channel(r),
                         "position": parse_channel(r),
                         "scale": parse_channel(r)}
        anim["bones"] = bones
        events = []
        for _ in range(r.varint()):
            ev = {"events": [r.string() for _ in range(r.varint())],
                  "timestamp": r.float() / 20.0}
            events.append(ev)
        anim["timeline"] = events
        if fmt > 9:
            anim["sound_effects"] = [{"name": r.string(), "t": r.float() / 20.0}
                                     for _ in range(r.varint())]
        out[anim["name"]] = anim
        anim["_off"] = _off
    return out


def parse_controller_body(r: Reader, fmt: int) -> dict:
    out = {}
    for _ in range(r.varint()):
        entry = {"name": r.string(), "initial_state": r.string(), "states": []}
        for _ in range(r.varint()):
            st = {"name": r.string()}
            st["animations"] = {r.string(): r.string() for _ in range(r.varint())}
            st["transitions"] = {r.string(): r.string() for _ in range(r.varint())}
            st["on_entry"] = [r.string() for _ in range(r.varint())]
            st["on_exit"] = [r.string() for _ in range(r.varint())]
            if r.varint() != 0:
                st["blend_transition"] = r.float()
            else:
                st["blend_transitions"] = {r.float(): r.float() for _ in range(r.varint())}
            st["blend_via_shortest_path"] = r.varint() != 0
            if fmt > 26:
                st["sound_effects"] = [r.string() for _ in range(r.varint())]
            entry["states"].append(st)
        out[entry["name"]] = entry
    return out


def parse_animation_controllers(r: Reader, fmt: int, read_name: bool) -> list:
    out = []
    for i in range(r.varint()):
        if fmt <= 15:
            f = {"legacy_unk": r.varint(), "name": f"legacy_controller_{i}", "hash": ""}
        else:
            f = {"name": r.string() if read_name else "", "hash": r.string()}
        f["controllers"] = parse_controller_body(r, fmt)
        out.append(f)
    return out


def parse_sound_files(r: Reader, fmt: int) -> dict:
    out = {}
    for _ in range(r.varint()):
        name = r.string()
        h = r.string() if fmt > 15 else ""
        out[name] = {"hash": h, "data": r.byte_array()}
    return out


# ------------------------------------------------------------------ ysm.json
def parse_ysm_json(r: Reader, fmt: int) -> dict:
    props = {"sha256": r.string()}
    meta = {}
    is_new = r.varint()
    if is_new != 0:
        if fmt <= 15:
            r.varint()                       # unknown
        meta["name"] = r.string()
        meta["tips"] = r.string()
        meta["license_type"] = r.string()
        meta["license_description"] = r.string()
        authors = []
        for _ in range(r.varint()):
            a = {"name": r.string(), "role": r.string()}
            a["contacts"] = {r.string(): r.string() for _ in range(r.varint())}
            a["comment"] = r.string()
            authors.append(a)
        meta["authors"] = authors
        meta["links"] = {r.string(): r.string() for _ in range(r.varint())}

    # 上游 line 508：老格式模型（fmt<=15）里 is_new==0 时 ysm.json 到此为止，
    # 后面完全没有内容（小死神 format 9 就是这种，继续读会越界）。
    if is_new == 0 and fmt <= 15:
        return {"properties": props, "metadata": meta}

    props["width_scale"] = r.float()
    props["height_scale"] = r.float()
    props["extra_animations"] = {r.string(): r.string() for _ in range(r.varint())}

    if fmt > 9:
        buttons = []
        for _ in range(r.varint()):
            btn = {"id": r.string(), "name": r.string(), "padding": r.varint(), "forms": []}
            for _ in range(r.varint()):
                form = {"type": r.string(), "title": r.string(),
                        "description": r.string(), "default": r.string(),
                        "step": r.float(), "min": r.float(), "max": r.float()}
                form["labels"] = {r.string(): r.string() for _ in range(r.varint())}
                btn["forms"].append(form)
            buttons.append(btn)
        props["extra_animation_buttons"] = buttons

        classify = []
        for _ in range(r.varint()):
            c = {"id": r.string()}
            c["extras"] = {r.string(): r.string() for _ in range(r.varint())}
            classify.append(c)
        props["extra_animation_classifies"] = classify

    props["default_texture"] = r.string()
    props["preview_animation"] = r.string()
    props["is_free"] = r.varint() != 0
    if fmt > 4:
        props["render_layers_first"] = r.varint() != 0
    if fmt >= 15:
        props["all_cutout"] = r.varint() != 0
        props["disable_preview_rotation"] = r.varint() != 0

    # 现代格式（format > 15）在这一段之后还有一大截；上游 legacy 分支到这里 return
    if fmt > 15:
        props["gui_no_lighting"] = r.varint() != 0
        if fmt >= 32:
            props["merge_multiline_expr"] = r.varint() != 0
        props["gui_foreground"] = r.string()
        props["gui_background"] = r.string()
        avatars = []
        for _ in range(r.varint()):
            avatars.append({"name": r.string(), "data": r.byte_array(),
                            "width": r.varint(), "height": r.varint(),
                            "format": r.varint(), "unknown_flag": r.varint()})
        props["avatars"] = avatars
        authors = meta.get("authors", [])
        for i, av in enumerate(avatars):
            if i < len(authors):
                authors[i]["avatar"] = av["name"]
                authors[i]["avatar_image"] = av
            else:
                meta.setdefault("extra_avatars", []).append(av)
        backgrounds = []
        for _ in range(r.varint()):
            backgrounds.append({"name": r.string(), "data": r.byte_array(),
                                "width": r.varint(), "height": r.varint(),
                                "format": r.varint(), "unknown_flag": r.varint()})
        props["background_images"] = backgrounds
    return {"properties": props, "metadata": meta}


# ------------------------------------------------------------------ 主流程
def deserialize_legacy_v15(r: Reader, fmt: int, m: RawYsmModel):
    skip = r.varint()
    r.skip(skip)

    temp_models = []
    for _ in range(r.varint()):
        model_id = r.varint()
        pad = r.varint()
        if pad != 1:
            raise ValueError(f"Expected 1, got {pad} at 0x{r.offset():x}")
        geo = parse_models(r, fmt)
        geo["model_type"] = model_id
        temp_models.append(geo)

    for geo in temp_models:
        t = geo["model_type"]
        if t == 1:
            m.main_model = geo
        elif t == 2:
            m.arm_model = geo
        elif t == 3:
            m.projectiles["minecraft:arrow"] = geo
        else:
            raise ValueError(f"Unknown model type: {t}")

    temp_anims = {}
    for _ in range(r.varint()):
        anim_id = r.varint()
        pad = r.varint()
        if pad != 1:
            raise ValueError(f"Expected 1, got {pad} at 0x{r.offset():x}")
        anim_file = parse_animations(r, fmt)
        key = ANIM_KEY_FROM_TYPE.get(anim_id, "unknown")
        if anim_id == 5:
            m.projectiles.setdefault("minecraft:arrow", {})
        else:
            m.animations[key] = anim_file
        temp_anims[anim_id] = anim_file

    if fmt > 9:
        ctrls = parse_animation_controllers(r, fmt, False)
        m.controllers = ctrls
        for _ in range(r.varint()):
            cname = r.string()
            chash = r.string()
            for f in ctrls:
                if f["name"] == cname:
                    f["hash"] = chash
                    break

    for _ in range(r.varint()):
        tex = {"name": r.string(), "data": r.byte_array(),
               "width": r.varint(), "height": r.varint(), "image_format": -1,
               "sub": []}
        for _ in range(r.varint()):
            tex["sub"].append({"specular_type": r.varint(), "data": r.byte_array(),
                               "width": r.varint(), "height": r.varint(),
                               "image_format": -1})
        if tex["name"] == "/ARROW\\" and "minecraft:arrow" in m.projectiles:
            pass  # 上游放进 projectile；这里统一留在 textures 里
        m.textures[tex["name"]] = tex

    if fmt > 9:
        m.sounds = parse_sound_files(r, fmt)
        for _ in range(r.varint()):
            sname = r.string()
            shash = r.string()
            if sname in m.sounds:
                m.sounds[sname]["hash"] = shash

    avatars = []
    for _ in range(r.varint()):
        avatars.append({"name": r.string(), "data": r.byte_array(),
                        "width": r.varint(), "height": r.varint(), "format": -1})
    m.avatars = avatars

    # Hash 回填表
    for _ in range(r.varint()):
        mid = r.varint()
        mh = r.string()
        for geo in temp_models:
            if geo["model_type"] == mid:
                geo["sha256"] = mh

    for _ in range(r.varint()):
        aid = r.varint()
        ah = r.string()
        if aid in temp_anims:
            for a in temp_anims[aid].values():
                a["file_hash"] = ah

    for _ in range(r.varint()):
        tname = r.string()
        thash = r.string()
        tex = m.textures.get(tname)
        if tex:
            tex["hash"] = thash
        for _ in range(r.varint()):
            st = r.varint()
            sh = r.string()
            if tex:
                for s in tex["sub"]:
                    if s["specular_type"] == st:
                        s["hash"] = sh
                        break

    j = parse_ysm_json(r, fmt)
    m.properties = j["properties"]
    m.metadata = j["metadata"]

    for i, av in enumerate(avatars):
        authors = m.metadata.get("authors", [])
        if i < len(authors):
            authors[i]["avatar"] = av["name"]
            authors[i]["avatar_image"] = av
        else:
            m.metadata.setdefault("extra_avatars", []).append(av)


# ---------------------------------------------------------------- 现代格式（16+）
# 上游：YSMBinaryDeserializer.deserializeModern()（openysm 2.6.5 反编译源码）
# 与 legacy(<=15) 的区别是**顶层顺序完全不同**：
#   legacy：skip 块 → 模型 → 动画 → 控制器 → 贴图 → 声音 → 头像 → hash 回填 → ysm.json
#   modern：声音 → 函数 → 语言 → 子实体 → 动画 → 控制器 → 贴图 → 模型 → ysm.json
# （hash 直接跟在各自条目后面，不再有单独的 hash 回填表）
def parse_texture_files(r: Reader, fmt: int) -> dict:
    """parseTextureFiles：现代贴图段（hash 在 data 之前，显式 image_format）。"""
    out = {}
    for _ in range(r.varint()):
        tex = {"name": r.string(), "hash": r.string(), "data": r.byte_array(),
               "width": r.varint(), "height": r.varint(),
               "image_format": r.varint(), "unknown_flag": r.varint(), "sub": []}
        for _ in range(r.varint()):
            tex["sub"].append({"specular_type": r.varint(), "hash": r.string(),
                               "data": r.byte_array(), "width": r.varint(),
                               "height": r.varint(), "image_format": r.varint(),
                               "unknown_flag": r.varint()})
        out[tex["name"]] = tex
    return out


def parse_function_files(r: Reader) -> dict:
    """parseFunctionFiles：{名字: {hash, data}}。"""
    out = {}
    for _ in range(r.varint()):
        name = r.string()
        h = r.string()
        out[name] = {"hash": h, "data": r.byte_array()}
    return out


def parse_language_files(r: Reader) -> dict:
    """parseLanguageFiles：{语言: {hash, nodes:{key: value}}}。"""
    out = {}
    for _ in range(r.varint()):
        name = r.string()
        h = r.string()
        nodes = {r.string(): r.string() for _ in range(r.varint())}
        out[name] = {"hash": h, "nodes": nodes}
    return out


def parse_sub_entity(r: Reader, fmt: int, category: str, index: int) -> dict:
    """parseSubEntity：载具 / 投射物（自带动画、控制器、贴图和子模型）。"""
    sub = {"identifier": r.string() if fmt <= 26 else "%s_%d" % (category, index),
           "animations": {}, "controllers": [], "textures": {}, "model": None}
    for _ in range(r.varint()):
        h = r.string()
        anim_file = parse_animations(r, fmt)
        for a in anim_file.values():
            a["file_hash"] = h
        sub["animations"][category] = anim_file
    sub["controllers"] = parse_animation_controllers(r, fmt, False)
    base = {"name": "base_texture_%d" % index, "hash": r.string(),
            "data": r.byte_array(), "width": r.varint(), "height": r.varint(),
            "image_format": r.varint(), "unknown_flag": r.varint(), "sub": []}
    for _ in range(r.varint()):
        base["sub"].append({"specular_type": r.varint(), "hash": r.string(),
                            "data": r.byte_array(), "width": r.varint(),
                            "height": r.varint(), "image_format": r.varint(),
                            "unknown_flag": r.varint()})
    sub["textures"][base["name"]] = base
    model_hash = r.string()
    geo = parse_models(r, fmt)
    geo["sha256"] = model_hash
    sub["model"] = geo
    if fmt > 26:
        r.varint()                        # footerFlag（未使用）
        sub["identifier"] = r.string()
    return sub


def assign_main_models(m: RawYsmModel, temp_models: list) -> None:
    """assignMainModels：type 1→主模型，2→手臂模型，3→箭的投射物。"""
    for geo in temp_models:
        t = geo.get("model_type")
        if t == 1:
            m.main_model = geo
        elif t == 2:
            m.arm_model = geo
        elif t == 3:
            m.projectiles["minecraft:arrow"] = geo
        else:
            raise ValueError("Unknown model type: %r" % (t,))


def deserialize_modern(r: Reader, fmt: int, m: RawYsmModel) -> None:
    """deserializeModern（format 16+）。"""
    m.sounds = parse_sound_files(r, fmt)
    m.functions = parse_function_files(r)
    m.languages = parse_language_files(r)

    if fmt < 26:
        for i in range(r.varint()):
            sub = parse_sub_entity(r, fmt, "SubEntity", i)
            m.vehicles[sub["identifier"]] = sub
        r.varint()                        # 上游这里多读一个 varint（未使用）
    else:
        for i in range(r.varint()):
            sub = parse_sub_entity(r, fmt, "Vehicle", i)
            m.vehicles[sub["identifier"]] = sub
        for i in range(r.varint()):
            sub = parse_sub_entity(r, fmt, "Projectile", i)
            m.projectiles[sub["identifier"]] = sub

    flag = r.varint()
    if flag != 1:
        raise ValueError("Expected 1 after SubEntities, got %d at 0x%x"
                         % (flag, r.offset()))

    for _ in range(r.varint()):
        t = r.varint()
        h = r.string()
        anim_file = parse_animations(r, fmt)
        m.animations[ANIM_KEY_FROM_TYPE.get(t, "unknown")] = anim_file
        for a in anim_file.values():
            a["anim_type"] = t
            a["file_hash"] = h

    m.controllers = parse_animation_controllers(r, fmt, True)
    m.textures = parse_texture_files(r, fmt)

    temp_models = []
    for _ in range(r.varint()):
        model_type = r.varint()
        h = r.string()
        geo = parse_models(r, fmt)
        geo["sha256"] = h
        geo["model_type"] = model_type
        temp_models.append(geo)
    assign_main_models(m, temp_models)

    j = parse_ysm_json(r, fmt)
    m.properties = j["properties"]
    m.metadata = j["metadata"]


def parse_footer(r: Reader, fmt: int) -> dict:
    """parseYSMFooter（上游 :69-87，磁盘路径在 deserialize 之后单独调一次）。

    version 只有 fmt>26 才落盘，否则等于 fmt 本身；fmt>=24 多一个 unk_int2。
    读完这个 footer 后指针应当正好落在文件末尾（“残留=0”是解析正确性的硬证据）。
    """
    if fmt < 9:
        return {}
    f = {"version": r.varint() if fmt > 26 else fmt,
         "unk_int1": r.varint(),
         "rand": r.string(),
         "time": r.varlong(),
         "extra": r.string()}
    if fmt >= 24:
        f["unk_int2"] = r.varint()
    return f


def deserialize(data: bytes, fmt: int = None) -> RawYsmModel:
    r = Reader(data)
    if fmt is None:
        fmt = r.dword()
    else:
        # 上游两种构造器：1 参（载荷首个 dword 就是 format）与 2 参（format 由调用方给、
        # **不吃** dword）。本项目缓存里的 .bin 两种形态都可能，所以只在 dword 恰好等于
        # 已知 format 时才吃掉它，否则原样退回。
        head = r.offset()
        if r.dword() != fmt:
            r.pos = head
    if fmt < 1 or fmt > 64:
        raise ValueError(f"Unsupported YSM binary format: {fmt}")
    m = RawYsmModel(fmt)
    if fmt < 4:
        raise NotImplementedError(f"legacy v1 (format {fmt}) 未移植")
    if fmt <= 15:
        deserialize_legacy_v15(r, fmt, m)
    else:
        deserialize_modern(r, fmt, m)
    # 上游 deserializeInternal 收尾：扔掉没有模型或没有贴图的投射物（:49-52）
    for k in [k for k, s in m.projectiles.items()
              if s.get("model") is None or not s.get("textures")]:
        del m.projectiles[k]
    m.end_offset = r.offset()
    # 载荷尾部：上游 ClientModelManager 在读完之后单独调 parseYSMFooter
    try:
        m.footer = parse_footer(r, fmt)
    except Exception:                      # noqa: BLE001  上游也是 try/catch 吞掉
        m.footer = {}
    m.footer_offset = r.offset()
    return m
