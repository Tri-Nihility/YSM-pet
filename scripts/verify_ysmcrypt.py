"""A4：C 版解密器 vs Python 原版，逐字段对照。

为什么必须逐字段对照：上游对 CityHash / ChaCha 做了魔改，C 里任何"按标准
算法写"的地方都会悄悄跑偏 —— 而且**不会报错**，只会解出一堆乱码。所以
不能只看"最终能不能解出一个模型"，要把中间量逐层对齐。

对照层次（由浅到深，哪层挂了立刻能定位）：
    1. cityHash64(raw)              —— 纯哈希，无副作用
    2. cityHash64WithSeed(raw, s)   —— 带 seed 变体
    3. 文件签名校验（stored vs calc）—— 真实 .ysm 上的端到端哈希
    4. ChaCha 解密中间流            —— 只做第 1 步的解密结果
    5. MT19937 异或后               —— 第 1+2 步
    6. 跳过 padding 后的密文流      —— 第 1+2+3 步
    7. 最终 zstd 解压结果           —— 完整链路（Python 侧解压）

用法：
    python scripts/verify_ysmcrypt.py            # 用 models/ 下的真实 .ysm
    python scripts/verify_ysmcrypt.py <file.ysm>
"""
import ctypes
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

DLL = os.path.join(ROOT, "native", "ysmcrypt.dll")


def load_dll():
    if not os.path.exists(DLL):
        sys.exit("缺少 %s，先跑 scripts/build_native.py" % DLL)
    d = ctypes.CDLL(DLL)
    d.ysmc_cityhash64.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
    d.ysmc_cityhash64.restype = ctypes.c_uint64
    d.ysmc_cityhash64_seed.argtypes = [ctypes.c_char_p, ctypes.c_size_t,
                                       ctypes.c_uint64]
    d.ysmc_cityhash64_seed.restype = ctypes.c_uint64
    d.ysmc_verify.argtypes = [ctypes.c_char_p, ctypes.c_size_t,
                              ctypes.POINTER(ctypes.c_uint64),
                              ctypes.POINTER(ctypes.c_uint64)]
    d.ysmc_verify.restype = ctypes.c_int
    d.ysmc_decrypt.argtypes = [ctypes.c_char_p, ctypes.c_size_t,
                               ctypes.c_char_p, ctypes.c_char_p,
                               ctypes.c_char_p, ctypes.c_size_t]
    d.ysmc_decrypt.restype = ctypes.c_int
    return d


def find_models():
    """找仓库里可用的 .ysm。"""
    out = []
    for base in ("models", "临时文件（记得整理掉"):
        d = os.path.join(ROOT, base)
        if not os.path.isdir(d):
            continue
        for dp, _dn, fn in os.walk(d):
            for f in fn:
                if f.lower().endswith(".ysm"):
                    out.append(os.path.join(dp, f))
    return out


def check_hash(dll, ysc):
    """层次 1/2：纯哈希随机对照（长度覆盖 0..16 / 17..32 / 33..64 / >64）。"""
    print("--- 层次 1/2: cityHash64 随机对照 ---")
    rnd = random.Random(20261005)
    case = 0
    for n in ([0, 1, 3, 4, 7, 8, 15, 16, 17, 31, 32, 33, 63, 64, 65,
               100, 256, 1000, 4096, 70000]):
        data = bytes(rnd.randrange(256) for _ in range(n)) if n else b""
        p = ctypes.c_char_p(data)
        got = dll.ysmc_cityhash64(p, ctypes.c_size_t(n))
        exp = ysc.city_hash64(data)
        if got != exp:
            print("  ✗ len=%d  hash C=%#x  py=%#x" % (n, got, exp))
            return False
        seed = rnd.randrange(1 << 64)
        got2 = dll.ysmc_cityhash64_seed(p, ctypes.c_size_t(n),
                                        ctypes.c_uint64(seed))
        exp2 = ysc.city_hash64_with_seed(data, seed)
        if got2 != exp2:
            print("  ✗ len=%d seed hash C=%#x  py=%#x" % (n, got2, exp2))
            return False
        case += 1
    print("  ✓ %d 组长度全部一致（含 >64 字节的 weak/交换分支）" % case)
    # 随机长度再压一轮
    for _ in range(200):
        n = rnd.randrange(0, 300)
        data = bytes(rnd.randrange(256) for _ in range(n)) if n else b""
        p = ctypes.c_char_p(data)
        seed = rnd.randrange(1 << 64)
        if dll.ysmc_cityhash64_seed(p, ctypes.c_size_t(n),
                                    ctypes.c_uint64(seed)) \
                != ysc.city_hash64_with_seed(data, seed):
            print("  ✗ 随机 len=%d 不一致" % n)
            return False
    print("  ✓ 追加 200 组随机长度一致")
    return True


def check_real_file(dll, ysc, path):
    """层次 3~7：真实 .ysm 上的完整链路。"""
    name = os.path.basename(path)
    print("\n--- %s ---" % name)
    raw = open(path, "rb").read()
    print("  文件 %d B" % len(raw))

    # 层次 3：签名校验
    stored = ctypes.c_uint64(0)
    calc = ctypes.c_uint64(0)
    okc = dll.ysmc_verify(ctypes.c_char_p(raw), ctypes.c_size_t(len(raw)),
                          ctypes.byref(stored), ctypes.byref(calc))
    pyc = ysc.parse_container(raw)
    okpy = pyc["hash_ok"]
    print("  签名 C: ok=%d stored=%#x calc=%#x" % (okc, stored.value, calc.value))
    print("  签名 py: ok=%d stored=%#x calc=%#x"
          % (okpy, pyc["stored_hash"], pyc["calc_hash"]))
    if (bool(okc) != bool(okpy)) or calc.value != pyc["calc_hash"]:
        print("  ✗ 签名校验层不一致")
        return False
    print("  ✓ 签名校验层一致")

    # 层次 4：ChaCha 解密中间流
    payload = pyc["payload"]
    key, iv = pyc["key"], pyc["iv"]
    exp_chacha = ysc.modified_chacha_decrypt(
        payload, key, iv, ysc.SEED_RES_VERIFICATION)
    out = ctypes.create_string_buffer(len(payload))
    n = dll.ysmc_decrypt(ctypes.c_char_p(payload), ctypes.c_size_t(len(payload)),
                         ctypes.c_char_p(key), ctypes.c_char_p(iv),
                         out, ctypes.c_size_t(len(payload)))
    if n < 0:
        print("  ✗ C 解密返回 %d" % n)
        return False
    py_decrypted = ysc.decrypt_ysm_file(raw)
    print("  C 返回密文流 %d B" % n)

    # 层次 5+6：C 的结果应当是"ChaCha→MT xor→跳 padding"
    # 用 Python 分步算出来对
    xored = ysc.mt19937_xor(exp_chacha, key + iv, ysc.SEED_KEY_DERIVATION)
    nn = (xored[0] | (xored[1] << 8)) & 0x3FF
    exp_stream = xored[2 + nn:]
    c_stream = out.raw[:n]
    if len(c_stream) != len(exp_stream):
        print("  ✗ 密文流长度 C=%d py=%d" % (len(c_stream), len(exp_stream)))
        return False
    diff = sum(1 for a, b in zip(c_stream, exp_stream) if a != b)
    print("  密文流长度 %d，不一致字节 %d" % (len(c_stream), diff))
    if diff:
        first = next(i for i, (a, b) in enumerate(zip(c_stream, exp_stream))
                     if a != b)
        print("  ✗ 首个不一致位置 %d: C=%#x py=%#x"
              % (first, c_stream[first], exp_stream[first]))
        return False
    print("  ✓ ChaCha + MT19937 + padding 三层逐字节一致")

    # 层次 7：最终结果（Python 侧 zstd 解压 C 的密文流）
    got = ysc.ysm_zstd_wash_and_decompress(bytearray(c_stream))
    print("  最终明文 C 路径 %d B / py 路径 %d B" % (len(got), len(py_decrypted)))
    if got != py_decrypted:
        print("  ✗ 最终明文不一致")
        return False
    print("  ✓ 最终明文逐字节一致")
    return True


def main():
    dll = load_dll()
    from ysm import ysmcrypt as ysc

    ok = check_hash(dll, ysc)
    if not ok:
        sys.exit("哈希层就挂了，后面的不用测")

    paths = sys.argv[1:] or find_models()
    if not paths:
        print("\n没找到 .ysm（models/ 为空），跳过真实文件对照")
    else:
        for p in paths[:3]:
            if not check_real_file(dll, ysc, p):
                sys.exit("真实文件对照失败: %s" % p)

    print("\n全部对照通过")


if __name__ == "__main__":
    main()
