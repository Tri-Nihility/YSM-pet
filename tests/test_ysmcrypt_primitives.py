"""
ysmcrypt 原语自检。

向量来源（均取自上游源码自带 / 公开标准）：
  1. CityHash   —— CityHash.java main() 内嵌向量（128B，seed 0xD1C3D1D13A99752B）
  2. ChaCha20   —— RFC 8439 §2.4.2（标准 ChaCha20，20 轮，12B nonce）
  3. HChaCha20  —— draft-irtf-cfrg-xchacha §2.2.1（16B nonce 派生子密钥）
  4. XChaCha20  —— draft-irtf-cfrg-xchacha §A.3.2 全链路（HChaCha20 + ChaCha20 组合）
"""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ysm import ysmcrypt as Y  # noqa: E402


def _fmt(v):
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, int):
        return hex(v)
    return str(v)


def check(name, got, exp):
    ok = got == exp
    print(f"[{'OK ' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"        got {_fmt(got)}")
        print(f"        exp {_fmt(exp)}")
    return ok


# ---------------------------------------------------------------- 1. CityHash
CITY_IN = bytes([
    0x01, 0x7B, 0x88, 0x21, 0x12, 0x63, 0x9A, 0xEE,
    0x6A, 0xBD, 0xED, 0xAD, 0xA2, 0xBD, 0xFC, 0xF1,
    0x9B, 0xE2, 0xD6, 0xF8, 0xC7, 0x8F, 0x0A, 0xE5,
    0x05, 0x0A, 0x6B, 0xE1, 0x0D, 0xD4, 0xEC, 0xE5,
    0x29, 0xAE, 0x35, 0x3C, 0x54, 0xE8, 0x8F, 0xF4,
    0x5C, 0x80, 0xCD, 0x1B, 0x0F, 0xD3, 0x76, 0xFE,
    0x7B, 0xC2, 0x41, 0x65, 0x52, 0xA9, 0x48, 0xFE,
    0xC0, 0x46, 0x09, 0x00, 0x32, 0x02, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x58, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x5F, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0xB0, 0xB5, 0x43, 0xD5, 0xFC, 0x7F, 0x00, 0x00,
    0x7F, 0x41, 0x5B, 0xB1, 0x14, 0x03, 0xC3, 0x7D,
    0x5E, 0x73, 0x40, 0xEE, 0x00, 0x01, 0x81, 0x91,
    0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
])


def test_cityhash():
    return check("CityHash64WithSeed(上游内嵌向量)",
                 Y.city_hash64_with_seed(CITY_IN, 0xD1C3D1D13A99752B),
                 0xCC6F56E6E94ABA81)


# --------------------------------------------------------------- 2. ChaCha20
def chacha20_block_raw(key: bytes, counter: int, nonce: bytes, rounds: int = 20) -> bytes:
    """按 RFC 8439 布局组状态：sigma | key(8) | counter | nonce(3)，不做 HChaCha。"""
    k = struct.unpack("<8I", key)
    n = struct.unpack("<3I", nonce)
    init = list(Y._SIGMA) + list(k) + [counter & Y.M32] + list(n)
    w = list(init)
    Y._shuffle(w, rounds)
    return b"".join(((w[i] + init[i]) & Y.M32).to_bytes(4, "little") for i in range(16))


def chacha20_xor(data: bytes, key: bytes, counter: int, nonce: bytes, rounds: int = 20) -> bytes:
    out = bytearray()
    for i in range(0, len(data), 64):
        ks = chacha20_block_raw(key, counter + i // 64, nonce, rounds)
        blk = data[i:i + 64]
        out += bytes(a ^ b for a, b in zip(blk, ks))
    return bytes(out)


SUNSCREEN = (b"Ladies and Gentlemen of the class of '99: If I could offer you "
             b"only one tip for the future, sunscreen would be it.")


def test_chacha20_rfc8439():
    key = bytes(range(0x00, 0x20))
    nonce = bytes.fromhex("000000000000004a00000000")   # 注意：不是 ...09...
    exp = bytes.fromhex(
        "6e2e359a2568f98041ba0728dd0d6981"
        "e97e7aec1d4360c20a27afccfd9fae0b"
        "f91b65c5524733ab8f593dabcd62b357"
        "1639d624e65152ab8f530c359f0861d8"
        "07ca0dbf500d6a6156a38e088a22b65e"
        "52bc514d16ccf806818ce91ab7793736"
        "5af90bbf74a35be6b40b8eedf2785e42"
        "874d")
    return check("ChaCha20 RFC8439 §2.4.2", chacha20_xor(SUNSCREEN, key, 1, nonce), exp)


# -------------------------------------------------------------- 3. HChaCha20
def test_hchacha20():
    """draft-irtf-cfrg-xchacha §2.2.1 —— 校验 hChaCha20 子密钥派生。"""
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
    nonce = bytes.fromhex("000000090000004a0000000031415927")
    exp = bytes.fromhex("82413b4227b27bfed30e42508a877d73a0f9e4d58a74a853c12ec41326d3ecdc")
    k = struct.unpack("<8I", key)
    n = struct.unpack("<4I", nonce)
    x = list(Y._SIGMA) + list(k) + list(n)
    Y._shuffle(x, 20)
    sub = [x[0], x[1], x[2], x[3], x[12], x[13], x[14], x[15]]
    got = b"".join(w.to_bytes(4, "little") for w in sub)
    return check("HChaCha20 (XChaCha draft §2.2.1)", got, exp)


# ------------------------------------------------------------- 4. XChaCha20
DHOLE = (
    b'The dhole (pronounced "dole") is also known as the Asiatic wild dog, red dog, '
    b'and whistling dog. It is about the size of a German shepherd but looks more '
    b'like a long-legged fox. This highly elusive and skilled jumper is classified '
    b'with wolves, coyotes, jackals, and foxes in the taxonomic family Canidae.'
)


def test_xchacha20_draft():
    """draft-irtf-cfrg-xchacha §A.3.2 全链路：上游 XChaCha20(key, 24B nonce, 20轮)。"""
    key = bytes.fromhex("808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f")
    iv = bytes.fromhex("404142434445464748494a4b4c4d4e4f5051525354555658")
    ctx = Y.XChaCha20(key, iv, 20)
    got = ctx.process(DHOLE)
    ref = _xchacha_reference(key, iv)
    ok = check("XChaCha20 vs cryptography 组合参考", got, ref)
    print("        (DHOLE 前 32 字节密文:", got[:32].hex(), ")")
    return ok


def _xchacha_reference(key: bytes, iv: bytes) -> bytes:
    """用 cryptography 的 ChaCha20 组合出 XChaCha20 参考：subkey + 00 00 00 00 || iv[16:24]。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    k = struct.unpack("<8I", key)
    n = struct.unpack("<6I", iv)
    x = list(Y._SIGMA) + list(k) + [n[0], n[1], n[2], n[3]]
    Y._shuffle(x, 20)
    sub = b"".join(w.to_bytes(4, "little")
                   for w in [x[0], x[1], x[2], x[3], x[12], x[13], x[14], x[15]])
    # ChaCha20 的 16B IV = LE32(counter=0) || nonce(12) = 00 00 00 00 || 00 00 00 00 || iv[16:24]
    iv16 = (0).to_bytes(4, "little") + b"\x00\x00\x00\x00" + iv[16:24]
    c = Cipher(algorithms.ChaCha20(sub, iv16), mode=None).encryptor()
    return c.update(DHOLE)


def test_mt19937():
    """mt19937_64(5489) 前 3 个输出（信息性，无外部参考；由端到端解密间接验证）。"""
    mt = Y.MT19937(5489)
    got = [mt.next_u64() for _ in range(3)]
    print("   [info] mt19937-64(5489)[:3] =", [hex(v) for v in got])
    return True


if __name__ == "__main__":
    ok = True
    ok &= test_cityhash()
    ok &= test_chacha20_rfc8439()
    ok &= test_hchacha20()
    ok &= test_xchacha20_draft()
    ok &= test_mt19937()
    print("\n=== ALL PASS ===" if ok else "\n=== HAS FAILURE ===")
    sys.exit(0 if ok else 1)
