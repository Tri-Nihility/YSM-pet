"""A2 决策实验：反序列化到底值不值得搬 C？

先说结论导向：ysmbin 的大头**不是** varint/string 这类原语，而是
「构造几千个 Python dict/list」本身。所以先量化两者占比，再决定要不要迁。

测三档：
    A. 现有 Python Reader 全量反序列化（基线）
    B. 只把 Reader 原语换 C，其余结构组装不动
    C. 纯原语开销：只跑一遍 Reader 读完整流的成本

如果 B 相对 A 提升 < 20%，说明瓶颈在对象组装，搬 C 原语没意义 ——
那就应该转而去砍 Python 对象开销（比如产出扁平数组），而不是继续写 C。
"""
import ctypes
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

DLL = os.path.join(ROOT, "native", "ysmreader.dll")


def load_reader_dll():
    if not os.path.exists(DLL):
        return None
    d = ctypes.CDLL(DLL)
    d.ysmr_varint.argtypes = [ctypes.c_char_p, ctypes.c_size_t,
                              ctypes.POINTER(ctypes.c_size_t),
                              ctypes.POINTER(ctypes.c_int)]
    d.ysmr_varint.restype = ctypes.c_int32
    d.ysmr_varlong.argtypes = d.ysmr_varint.argtypes
    d.ysmr_varlong.restype = ctypes.c_uint64
    d.ysmr_selftest.restype = ctypes.c_int
    return d


def bench(fn, n=3):
    ts = []
    for _ in range(n):
        t = time.time()
        fn()
        ts.append(time.time() - t)
    return min(ts), sum(ts) / len(ts)


def main():
    from ysm import ysmcrypt as C
    from ysm import ysmbin as B

    path = None
    for base in ("models", "临时文件（记得整理掉"):
        d = os.path.join(ROOT, base)
        if not os.path.isdir(d):
            continue
        for dp, _dn, fn in os.walk(d):
            for f in fn:
                if f.lower().endswith(".ysm"):
                    path = os.path.join(dp, f)
                    break
            if path:
                break
        if path:
            break
    if not path:
        sys.exit("找不到 .ysm")

    raw = open(path, "rb").read()
    dec = C.decrypt_ysm_file(raw, use_native=True)
    print("模型: %s   明文 %d B" % (os.path.basename(path), len(dec)))

    d = load_reader_dll()
    if d is None:
        sys.exit("缺少 ysmreader.dll，先跑 scripts/build_native.py")
    print("DLL 自检:", "OK" if d.ysmr_selftest() == 0 else "失败")

    # ---------------- A. 现有全量反序列化
    bmin, bavg = bench(lambda: B.deserialize(dec))
    print("\nA. Python 全量反序列化   min %.3fs  avg %.3fs" % (bmin, bavg))

    # ---------------- C. 纯原语开销：只扫一遍流
    # 用 Python Reader 把所有字节按 varint 扫一遍，纯粹量原语成本
    def scan_py():
        r = B.Reader(dec)
        n = 0
        try:
            while r.pos < len(dec):
                r.varint()
                n += 1
        except Exception:
            pass
        return n

    cmin, cavg = bench(scan_py, n=2)
    print("C. Python 纯 varint 扫描   min %.3fs  avg %.3fs" % (cmin, cavg))

    # C 版原语扫描
    def scan_c():
        pos = ctypes.c_size_t(0)
        err = ctypes.c_int(0)
        n = 0
        L = len(dec)
        ptr = ctypes.c_char_p(dec)
        while pos.value < L:
            d.ysmr_varint(ptr, ctypes.c_size_t(L), ctypes.byref(pos),
                          ctypes.byref(err))
            if err.value:
                break
            n += 1
        return n

    ccmin, ccavg = bench(scan_c, n=2)
    print("C'. C 版原语扫描          min %.3fs  avg %.3fs" % (ccmin, ccavg))
    if cavg > 0:
        print("    原语加速比: %.1fx" % (cavg / ccavg if ccavg else 0))

    # ---------------- 结论
    print("\n--- 结论 ---")
    prim_share = cavg / bavg if bavg else 0
    print("原语(Python) 占全量比例: %.1f%%" % (100 * prim_share))
    potential = (cavg - ccavg) / bavg * 100 if bavg else 0
    print("即便原语全部免费，全量最多省: %.1f%%" % (100 * prim_share))
    print("（C 版原语实得: %.1f%%）" % potential)

    if prim_share < 0.35:
        print("\n→ 原语占比低，瓶颈在 Python 对象组装。")
        print("  搬 C 原语收益有限，应优先考虑扁平化输出结构。")
    else:
        print("\n→ 原语占比高，值得迁 C。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
