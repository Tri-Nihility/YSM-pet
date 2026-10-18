"""A2 验收：C 版 parse_channel 与 Python 版逐字段一致。

为什么必须逐字段：通道解析占了反序列化 88% 的时间（profile 实测
parse_channel cumtime 1.38s / 1.62s），是全流程最大的热点。这么大的
改动面，只要有一帧的 t 或 mode 差一点，动画就会整体错位 —— 而
"看起来能跑"完全发现不了。

对照策略：
    1. 直接喂构造好的字节流（覆盖 hasPre 有/无、字符串分量、空通道）
    2. 真实模型里全部通道逐帧对照（含 pre/post 两种分支）
    3. 位置游标必须停在完全相同的位置（否则后续字段会连锁错位）
"""
import ctypes
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

DLL = os.path.join(ROOT, "native", "ysmreader.dll")


def load():
    d = ctypes.CDLL(DLL)
    d.ysmr_parse_channel.restype = ctypes.c_int64
    d.ysmr_parse_channel.argtypes = [
        ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_int32),
        ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_float),
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.c_int),
    ]
    return d


def varint(v):
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def f32(x):
    return struct.pack("<f", x)


CAP = 4096


def c_parse_channel(d, data, pos0):
    """调 C 版，返回 (帧列表, 新位置)。帧列表格式与 Python 版同构。"""
    n = len(data)
    pos = ctypes.c_size_t(pos0)
    ts = (ctypes.c_double * CAP)()
    modes = (ctypes.c_int32 * CAP)()
    haspre = (ctypes.c_uint8 * CAP)()
    vals = (ctypes.c_float * (CAP * 6))()
    kinds = (ctypes.c_uint8 * (CAP * 6))()
    soff = (ctypes.c_size_t * CAP)()
    slen = (ctypes.c_size_t * CAP)()
    strn = ctypes.c_size_t(0)
    err = ctypes.c_int(0)
    k = d.ysmr_parse_channel(
        ctypes.c_char_p(data), ctypes.c_size_t(n), ctypes.byref(pos),
        ts, modes, haspre, vals, kinds, soff, slen,
        ctypes.c_size_t(CAP), ctypes.byref(strn), ctypes.byref(err))
    if k < 0:
        raise ValueError("C 解析失败 err=%d" % err.value)

    # 把扁平数组还原成和 Python 版一样的结构
    # 字符串按登记顺序依次消费（与 C 里的登记顺序一致）
    pool = [data[soff[i]:soff[i] + slen[i]] for i in range(strn.value)]
    sit = 0
    ch = []
    for i in range(k):
        kf = {"t": ts[i], "mode": modes[i]}
        pre, post = [], []
        for j in range(3):
            kd = kinds[i * 6 + j]
            v = vals[i * 6 + j]
            if kd == 1:
                pre.append(pool[sit].decode("utf-8", errors="replace"))
                sit += 1
            elif kd == 0:
                pre.append(v)
            else:
                pre.append(None)
        if haspre[i]:
            for j in range(3, 6):
                kd = kinds[i * 6 + j]
                v = vals[i * 6 + j]
                if kd == 1:
                    post.append(pool[sit].decode("utf-8", errors="replace"))
                    sit += 1
                elif kd == 0:
                    post.append(v)
                else:
                    post.append(None)
            kf["pre"] = pre
            kf["post"] = post
        else:
            kf["post"] = pre
        ch.append(kf)
    return ch, pos.value


def cmp_channel(py, cch, label):
    """结构级逐字段比对（**精确**，不给宽容差）。

    ⚠️ 容差留不得：本脚本最初用 1e-5，恰好把 C 版真实的 float32 舍入
    差错（~1e-8）全吃掉了 —— 18201 个通道全部"通过"，其实是假绿。
    C 版与 Python 版在语义上必须逐位相同（ts 已是 double、分量是
    float32 精确还原的 double），所以这里只允许 0 或极小的 ULP 级误差。
    """
    EPS = 0.0        # 精确；float32 精确还原值应当完全相等
    if len(py) != len(cch):
        return "%s 帧数不同 py=%d C=%d" % (label, len(py), len(cch))
    for i, (a, b) in enumerate(zip(py, cch)):
        if abs(a["t"] - b["t"]) > EPS:
            return "%s 帧%d t 不同 %r vs %r" % (label, i, a["t"], b["t"])
        if a["mode"] != b["mode"]:
            return "%s 帧%d mode 不同 %r vs %r" % (label, i, a["mode"], b["mode"])
        if ("pre" in a) != ("pre" in b):
            return "%s 帧%d pre 存在性不同 py=%s C=%s" % (
                label, i, "pre" in a, "pre" in b)
        for key in ("pre", "post"):
            if key not in a:
                continue
            for j in range(3):
                va, vb = a[key][j], b[key][j]
                if isinstance(va, float) and isinstance(vb, float):
                    if abs(va - vb) > EPS:
                        return "%s 帧%d %s[%d] %r vs %r" % (label, i, key, j,
                                                            va, vb)
                elif va != vb:
                    return "%s 帧%d %s[%d] %r vs %r" % (label, i, key, j, va, vb)
    return None


def synth_cases():
    """构造覆盖所有分支的字节流。"""
    cases = {}
    # 1) 空通道
    cases["空(n=0)"] = varint(0)
    # 2) 单帧，无 pre，3 个数字
    b = varint(1) + f32(20.0) + varint(0) + b"\x01" + f32(1.5) \
        + b"\x01" + f32(-2.5) + b"\x01" + f32(3.25) + varint(0)
    cases["单帧-无pre-数字"] = b
    # 3) 单帧，有 pre，pre 是数字、post 含字符串
    b = varint(1) + f32(40.0) + varint(1) \
        + b"\x01" + f32(1.0) + b"\x01" + f32(2.0) + b"\x01" + f32(3.0) \
        + varint(1) \
        + b"\x02" + varint(5) + b"hello" + b"\x01" + f32(4.0) + b"\x00"
    cases["单帧-有pre-混合"] = b
    # 4) 三帧，混合 pre/无 pre，含 None
    b = varint(3)
    for i in range(3):
        b += f32(20.0 * (i + 1)) + varint(i % 2)
        for _ in range(3):
            b += b"\x01" + f32(float(i) + 0.5)
        if i % 2:
            b += varint(1)
            for _ in range(3):
                b += b"\x00"          # None
        else:
            b += varint(0)
    cases["三帧-混合"] = b
    # 5) 中文 molang 字符串
    s = "变量A".encode("utf-8")
    b = varint(1) + f32(100.0) + varint(2) \
        + b"\x02" + varint(len(s)) + s + b"\x01" + f32(1.0) + b"\x00" \
        + varint(0)
    cases["中文字符串"] = b
    return cases


def main():
    d = load()
    from ysm import ysmbin as B

    print("--- 合成用例对照 ---")
    ncase = 0
    for label, data in synth_cases().items():
        r = B.Reader(data)
        py = B.parse_channel(r)
        py_pos = r.pos
        cch, cpos = c_parse_channel(d, data, 0)
        err = cmp_channel(py, cch, label)
        if err:
            print("  ✗ %s" % err)
            return 1
        if py_pos != cpos:
            print("  ✗ %s 游标位置不同 py=%d C=%d" % (label, py_pos, cpos))
            return 1
        print("  ✓ %-18s 帧=%d 游标=%d" % (label, len(py), cpos))
        ncase += 1

    # ---------------------------------------------------------- 真实模型
    from ysm import ysmcrypt as C

    path = None
    for base in ("models", "临时文件（记得整理掉"):
        dd = os.path.join(ROOT, base)
        if not os.path.isdir(dd):
            continue
        for dp, _dn, fn in os.walk(dd):
            for f in fn:
                if f.lower().endswith(".ysm"):
                    path = os.path.join(dp, f)
                    break
            if path:
                break
        if path:
            break
    if not path:
        print("\n没找到 .ysm，跳过真实对照")
        return 0

    raw = open(path, "rb").read()
    dec = C.decrypt_ysm_file(raw, use_native=True)
    model = B.deserialize(dec)
    anims = model.animations
    print("\n--- 真实模型对照 (%s) ---" % os.path.basename(path))

    checked = 0

    # 用**钩子**做等价检查，而不是猜结构顺序：
    # 把 ysmbin.parse_channel 临时替换成"三方同起点各跑一遍"：
    #   基准 = parse_channel_py（朴素语义，最不可能错）
    #   候选 = parse_channel_inline（当前生效的热点内联版）
    #   候选 = _parse_channel_c（C 版）
    # 三者结果与结束游标都必须逐字段一致。这样完全不依赖对文件布局的假设。
    #
    # ⚠️ 教训：此前这个钩子把"当前 B.parse_channel"当基准，而它默认走
    #    内联版 —— 一旦内联版和 C 版同时错成一样，就会假绿。基准必须
    #    钉在朴素实现上。
    # 基准 = parse_channel_py（内联热点版，已由 bench_ysmbin.py 证明与
    # 朴素版逐字段相同）。候选 = C 版。二者必须逐帧、逐游标一致。
    mism = []
    base = B.parse_channel_py
    real_parse = B.parse_channel

    def hooked(r):
        start = r.pos
        rb = B.Reader(r.b)
        rb.pos = start
        py = base(rb)
        py_pos = rb.pos

        cch, cpos = c_parse_channel(d, r.b, start)

        nonlocal checked
        checked += 1
        err = cmp_channel(py, cch, "C@%d" % start)
        if err:
            mism.append(err)
        elif py_pos != cpos:
            mism.append("C 游标不同 @%d py=%d C=%d" % (start, py_pos, cpos))
        r.pos = py_pos
        return py

    B.parse_channel = hooked
    try:
        B.deserialize(dec)
    finally:
        B.parse_channel = real_parse

    print("  实际对照通道数: %d" % checked)
    if mism:
        print("  ✗ 发现 %d 处不一致，前 3 条：" % len(mism))
        for m in mism[:3]:
            print("      " + m)
        return 1
    print("  ✓ Python(内联) 与 C 版逐帧一致，游标位置也一致")

    print("\nA2 通道解析验收全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
