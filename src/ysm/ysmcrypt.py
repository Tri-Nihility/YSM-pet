"""
.ysm（crypto v3）解密器 —— 逐行移植上游 Java 实现。

上游依据（Desktop/YSM/YSM-Liaison/mod/common/src/main/java/）：
  rip/ysm/security/YsmCrypt.java        decryptYsmFile / modifiedChaChaDecrypt / mt19937Xor
  rip/ysm/algorithms/XChaCha20.java     XChaCha20 + updateStateYSM + decryptYSM
  rip/ysm/algorithms/ChaCha20Base.java  quarterRound / shuffleState / processBlock
  rip/ysm/algorithms/CityHash.java      CityHash64 (+seed)
  rip/ysm/algorithms/MT19937.java       MT19937-64
  rip/ysm/algorithms/YsmZstd.java       wash()：魔改 ZSTD 块头还原

.ysm 容器布局（crypto 3）
    [0, hlen)        文本头：EF BB BF "YSGP\\r\\n" + Metadata/Tips/Export/SHA-256/Codec Version
    [hlen]           0x00 终止符
    [hlen+1, +4)     int32 LE = 3   (crypto version)
    [hlen+5, -64)    加密载荷
    [-64, -32)       key  (32 B)
    [-32, -8)        iv   (24 B)
    [-8, )           int64 LE 文件签名 = CityHash64WithSeed(data[:-8], SEED_FILE_VERIFICATION)

解密步骤
    1) modifiedChaChaDecrypt(payload, key, iv, SEED_RES_VERIFICATION)
    2) mt19937Xor(..., key||iv, SEED_KEY_DERIVATION)
    3) n = (u16[0]) & 0x3FF  -> 跳过 2+n 字节 padding
    4) YsmZstd.wash() 后按标准 ZSTD 解压
"""
import ctypes
import struct

M32 = 0xFFFFFFFF
M64 = 0xFFFFFFFFFFFFFFFF

SEED_PACKET_VERIFICATION = 0xEE6FA63D570BD77B
SEED_KEY_DERIVATION = 0xD017CBBA7B5D3581
SEED_FILE_VERIFICATION = 0x9E5599DB80C67C29
SEED_RES_VERIFICATION = 0xA62B1A2C43842BC3
SEED_CACHE_DECRYPTION = 0xD1C3D1D13A99752B
SEED_CACHE_VERIFICATION = 0xF346451E53A22261


# --------------------------------------------------------------- CityHash64

_K0 = 0xE4986A230E5AAA17
_K1 = 0x91AF10802CAB25A5
_K2 = 0xAF29CE778879D9C7
_KMUL = 0xDE0F6EE09BDBAB91


def _rot(val, shift):
    """CityHash.rotate(val, shift) = (val >>> shift) | (val << (64 - shift))。

    注意：这是**右旋**（与 ChaCha 的 rotateLeft 方向相反），
    与上游 CityHash.java 的写法逐字一致，别看名字想当然改成左旋。
    """
    shift &= 63
    return val if shift == 0 else ((val >> shift) | (val << (64 - shift))) & M64


def _rot32(val, shift):
    """ChaCha20Base.rotateLeft(i, i2) = (i >>> (-i2)) | (i << i2)。

    Java 的 int 移位距离取低 5 位，故 -i2 & 31 == 32 - i2，
    即 (i >>> (32 - i2)) | (i << i2) —— 这是**左旋**，不是右旋。
    """
    shift &= 31
    return val if shift == 0 else ((val << shift) | (val >> (32 - shift))) & M32


def _shift_mix(val):
    return (val ^ (val >> 47)) & M64


def _fetch64(b, off):
    return int.from_bytes(b[off:off + 8], "little")


def _fetch32(b, off):
    return int.from_bytes(b[off:off + 4], "little")


def _hash_len16(u, v, mul=_KMUL):
    """CityHash.hashLen16(u, v, mul) —— 三参版本。"""
    a = ((u ^ v) * mul) & M64
    a ^= a >> 47
    b = ((v ^ a) * mul) & M64
    b ^= b >> 47
    return (b * mul) & M64


def _hash128to64(low, high):
    """CityHash.hashLen16(u, v)（两参）== hash128to64：kMul*ShiftMix(kMul*(ShiftMix((l^h)*kMul)^l))"""
    t1 = ((low ^ high) * _KMUL) & M64
    t2 = ((_shift_mix(t1) ^ low) * _KMUL) & M64
    return (_KMUL * _shift_mix(t2)) & M64


def _weak32(w, x, y, z, a, b):
    a = (a + w) & M64
    b = _rot((b + a + z) & M64, 21)
    c = a
    a = (a + x + y) & M64
    b = (b + _rot(a, 44)) & M64
    return ((a + z) & M64, (b + c) & M64)


def _weak(buf, off, a, b):
    return _weak32(_fetch64(buf, off), _fetch64(buf, off + 8),
                   _fetch64(buf, off + 16), _fetch64(buf, off + 24), a, b)


def _hash_len0to16(b):
    n = len(b)
    if n >= 8:
        mul = (_K2 + n * 2) & M64
        a = (_fetch64(b, 0) + _K2) & M64
        bb = _fetch64(b, n - 8)
        c = (_rot(bb, 37) * mul + a) & M64
        d = ((_rot(a, 25) + bb) * mul) & M64
        return _hash_len16(c, d, mul)
    if n >= 4:
        mul = (_K2 + n * 2) & M64
        a = _fetch32(b, 0)
        return _hash_len16((n + (a << 3)) & M64, _fetch32(b, n - 4), mul)
    if n > 0:
        a = b[0]
        bb = b[n >> 1]
        c = b[n - 1]
        y = (a + (bb << 8)) & M64
        z = (n + (c << 2)) & M64
        return (_shift_mix((y * _K2 ^ z * _K0) & M64) * _K2) & M64
    return _K2


def _hash_len17to32(b):
    n = len(b)
    mul = (_K2 + n * 2) & M64
    a = (_fetch64(b, 0) * _K1) & M64
    bb = _fetch64(b, 8)
    c = (_fetch64(b, n - 8) * mul) & M64
    d = (_fetch64(b, n - 16) * _K2) & M64
    u = (_rot((a + bb) & M64, 43) + _rot(c, 30) + d) & M64
    v = (a + _rot((bb + _K2) & M64, 18) + c) & M64
    return _hash_len16(u, v, mul)


def _hash_len33to64(b):
    n = len(b)
    mul = (_K2 + n * 2) & M64
    a = (_fetch64(b, 0) * _K2) & M64
    bb = _fetch64(b, 8)
    c = _fetch64(b, n - 24)
    d = _fetch64(b, n - 32)
    e = (_fetch64(b, 16) * _K2) & M64
    f = (_fetch64(b, 24) * 9) & M64
    g = _fetch64(b, n - 8)
    h = (_fetch64(b, n - 16) * mul) & M64
    u = (_rot((a + g) & M64, 43) + (_rot(bb, 30) + c) * 9) & M64
    v = (((a + g) ^ d) + f + 1) & M64
    w = (_bswap(((u + v) * mul) & M64) + h) & M64
    x = (_rot((e + f) & M64, 42) + c) & M64
    y = ((_bswap(((v + w) * mul) & M64) + g) * mul) & M64
    z = (e + f + c) & M64
    a = (_bswap(((x + z) * mul + y) & M64) + bb) & M64
    bb = (_shift_mix(((z + a) * mul + d + h) & M64) * mul) & M64
    return (bb + x) & M64


def _bswap(v):
    """Java Long.reverseBytes"""
    return int.from_bytes(v.to_bytes(8, "little"), "big")


def city_hash64(data):
    n = len(data)
    if n <= 32:
        return _hash_len0to16(data) if n <= 16 else _hash_len17to32(data)
    if n <= 64:
        return _hash_len33to64(data)

    x = _fetch64(data, n - 40)
    y = (_fetch64(data, n - 16) + _fetch64(data, n - 56)) & M64
    z = _hash128to64((_fetch64(data, n - 48) + n) & M64, _fetch64(data, n - 24))
    v = _weak(data, n - 64, n, z)
    w = _weak(data, n - 32, (y + _K1) & M64, x)
    x = (x * _K1 + _fetch64(data, 0)) & M64

    length = (n - 1) & ~63
    pos = 0
    while True:
        x = _rot((x + y + v[0] + _fetch64(data, pos + 8)) & M64, 37) * _K1 & M64
        y = _rot((y + v[1] + _fetch64(data, pos + 48)) & M64, 42) * _K1 & M64
        x ^= w[1]
        y = (y + v[0] + _fetch64(data, pos + 40)) & M64
        z = _rot((z + w[0]) & M64, 33) * _K1 & M64
        v = _weak(data, pos, (v[1] * _K1) & M64, (x + w[0]) & M64)
        w = _weak(data, pos + 32, (z + w[1]) & M64, (y + _fetch64(data, pos + 16)) & M64)
        x, z = z, x
        pos += 64
        length -= 64
        if length == 0:
            break
    u = _hash128to64(v[0], w[0])
    return _hash128to64((u + _shift_mix(y) * _K1 + z) & M64,
                        (_hash128to64(v[1], w[1]) + x) & M64)


def city_hash64_with_seed(data, seed):
    """hash64WithSeeds(raw, k2, seed) == hashLen16(hash64 - k2, seed)"""
    return _hash128to64((city_hash64(data) - _K2) & M64, seed & M64)


# --------------------------------------------------------------- MT19937-64

class MT19937:
    """标准 mt19937_64（与 Java MT19937 一致）。"""

    _N, _M, _R = 312, 156, 31
    _A = 0xB5026F5AA96619E9
    _U, _S, _T, _L = 29, 17, 37, 43
    _D = 0x5555555555555555
    _B = 0x71D67FFFEDA60000
    _C = 0xFFF7EEE000000000
    _F = 6364136223846793005
    _LOWER = 0x7FFFFFFF
    _UPPER = 0xFFFFFFFF80000000

    def __init__(self, seed):
        self.mt = [0] * self._N
        self.mt[0] = seed & M64
        self.index = self._N
        for i in range(1, self._N):
            prev = self.mt[i - 1]
            self.mt[i] = (self._F * (prev ^ (prev >> 62)) + i) & M64

    def _twist(self):
        n, m = self._N, self._M
        for i in range(n):
            x = (self.mt[i] & self._UPPER) | (self.mt[(i + 1) % n] & self._LOWER)
            xa = x >> 1
            if x & 1:
                xa ^= self._A
            self.mt[i] = self.mt[(i + m) % n] ^ xa
        self.index = 0

    def next_u64(self):
        if self.index >= self._N:
            self._twist()
        y = self.mt[self.index]
        self.index += 1
        y ^= (y >> self._U) & self._D
        y = (y ^ ((y << self._S) & self._B)) & M64
        y = (y ^ ((y << self._T) & self._C)) & M64
        y ^= y >> self._L
        return y


def mt19937_xor(data, keyiv, seed):
    """YsmCrypt.mt19937Xor：CityHash 派生种子，64bit keystream 逐字节异或。"""
    mt = MT19937(city_hash64_with_seed(keyiv, seed))
    out = bytearray(len(data))
    i = 0
    while i < len(data):
        rnd = mt.next_u64()
        chunk = rnd.to_bytes(8, "little")
        for j in range(8):
            if i >= len(data):
                break
            out[i] = data[i] ^ chunk[j]
            i += 1
    return bytes(out)


# ----------------------------------------------------------------- XChaCha20

_SIGMA = struct.unpack("<4I", b"expand 32-byte k")


def _quarter(x, a, b, c, d):
    x[a] = (x[a] + x[b]) & M32
    x[d] = _rot32(x[d] ^ x[a], 16)
    x[c] = (x[c] + x[d]) & M32
    x[b] = _rot32(x[b] ^ x[c], 12)
    x[a] = (x[a] + x[b]) & M32
    x[d] = _rot32(x[d] ^ x[a], 8)
    x[c] = (x[c] + x[d]) & M32
    x[b] = _rot32(x[b] ^ x[c], 7)


def _shuffle(x, rounds):
    for _ in range(rounds // 2):
        _quarter(x, 0, 4, 8, 12)
        _quarter(x, 1, 5, 9, 13)
        _quarter(x, 2, 6, 10, 14)
        _quarter(x, 3, 7, 11, 15)
        _quarter(x, 0, 5, 10, 15)
        _quarter(x, 1, 6, 11, 12)
        _quarter(x, 2, 7, 8, 13)
        _quarter(x, 3, 4, 9, 14)


class XChaCha20:
    """上游 XChaCha20（HChaCha20 子密钥 + 可变轮数 + YSM 状态搅动）。"""

    def __init__(self, key: bytes, nonce: bytes, rounds: int):
        assert len(key) == 32 and len(nonce) == 24
        k = struct.unpack("<8I", key)
        n = struct.unpack("<6I", nonce)
        x = list(_SIGMA) + list(k) + [n[0], n[1], n[2], n[3]]
        _shuffle(x, rounds)
        subkey = [x[0], x[1], x[2], x[3], x[12], x[13], x[14], x[15]]
        self.state = list(_SIGMA) + subkey + [0, 0, n[4], n[5]]
        self.rounds = rounds

    def _block(self):
        w = list(self.state)
        _shuffle(w, self.rounds)
        out = b"".join(((w[i] + self.state[i]) & M32).to_bytes(4, "little") for i in range(16))
        self.state[12] = (self.state[12] + 1) & M32
        if self.state[12] == 0:
            self.state[13] = (self.state[13] + 1) & M32
        return out

    def process(self, data: bytes) -> bytes:
        out = bytearray(len(data))
        pos = 0
        while pos < len(data):
            ks = self._block()
            n = min(64, len(data) - pos)
            for i in range(n):
                out[pos + i] = data[pos + i] ^ ks[i]
            pos += n
        return bytes(out)

    def update_state_ysm(self, h: int) -> int:
        """XChaCha20.updateStateYSM：按 CityHash 结果改轮数并搅动状态。"""
        self.rounds = 10 * (h % 3) + 10
        lo = h & M32
        hi = (h >> 32) & M32
        for i in range(4, 16):
            self.state[i] ^= lo if i % 2 == 0 else hi
        return ((h & 0x3F) | 0x40) << 6


def modified_chacha_decrypt(data: bytes, key: bytes, iv: bytes, seed: int) -> bytes:
    keyiv = key + iv
    h2 = city_hash64_with_seed(keyiv, seed)
    next_size = ((h2 & 0x3F) | 0x40) << 6
    ctx = XChaCha20(key, iv, 10 * (h2 % 3) + 10)
    out = bytearray(len(data))
    ptr = 0
    while ptr < len(data):
        n = min(next_size, len(data) - ptr)
        chunk = ctx.process(data[ptr:ptr + n])
        out[ptr:ptr + n] = chunk
        ptr += n
        if ptr < len(data):
            next_size = ctx.update_state_ysm(city_hash64_with_seed(chunk, seed))
    return bytes(out)


# -------------------------------------------------------------- YSM 魔改 ZSTD

def _zstd_frame_header_size(fhd: int) -> int:
    size = 1
    single_segment = (fhd >> 5) & 1
    dict_bits = fhd & 3
    dict_size = {1: 1, 2: 2, 3: 4}.get(dict_bits, 0)
    fcs_bits = (fhd >> 6) & 3
    if fcs_bits == 0:
        fcs = 1 if single_segment else 0
    elif fcs_bits == 1:
        fcs = 2
    elif fcs_bits == 2:
        fcs = 4
    else:
        fcs = 8
    return size + (0 if single_segment else 1) + dict_size + fcs


_YSM2STD = {0: 2, 1: 1, 2: 3, 3: 0}


def ysm_zstd_wash(data: bytearray) -> bytes:
    """YsmZstd.wash：把 YSM 混淆过的 ZSTD 块头还原成标准格式。"""
    if len(data) < 5:
        raise ValueError("data too short")
    magic = int.from_bytes(data[0:4], "little")
    if magic != 0xFD2FB528:
        raise ValueError(f"not a zstd frame: {magic:#x}")
    fhd = data[4]
    data[4] = fhd & 0xFB
    offset = 4 + _zstd_frame_header_size(fhd)
    while offset + 3 <= len(data):
        b0, b1, b2 = data[offset], data[offset + 1], data[offset + 2]
        last = (b0 >> 7) & 1
        bt_ysm = (b0 >> 5) & 3
        raw_size = ((b0 & 0x1F) << 16) | b1 | (b2 << 8)
        c_size = raw_size ^ 0xD4E9
        bt_std = _YSM2STD[bt_ysm]
        hdr = last | (bt_std << 1) | (c_size << 3)
        data[offset:offset + 3] = hdr.to_bytes(3, "little")
        offset += 3 + (1 if bt_std == 1 else c_size)
        if last == 1:
            break
    return bytes(data)


# -------------------------------------------------------------------- 容器

def parse_container(file_data: bytes) -> dict:
    """解析 .ysm 容器：文本头 + 各段偏移。"""
    hlen = file_data.find(b"\x00")
    if hlen < 0:
        raise ValueError("no header terminator")
    header_text = file_data[:hlen].decode("utf-8-sig", errors="replace")
    tail = len(file_data) - 64
    crypto = int.from_bytes(file_data[hlen + 1:hlen + 5], "little")
    key = file_data[tail:tail + 32]
    iv = file_data[tail + 32:tail + 56]
    stored_hash = int.from_bytes(file_data[tail + 56:], "little")
    calc_hash = city_hash64_with_seed(file_data[:-8], SEED_FILE_VERIFICATION)
    return {
        "header_text": header_text,
        "crypto": crypto,
        "key": key,
        "iv": iv,
        "stored_hash": stored_hash,
        "calc_hash": calc_hash,
        "hash_ok": stored_hash == calc_hash,
        "payload": file_data[hlen + 5:tail],
    }


def decrypt_stream(data: bytes, key: bytes, iv: bytes) -> bytes:
    """纯 Python：解密到「去 padding 后的密文流」，不做 zstd 解压。

    这是和 C 版 `ysmc_decrypt` 对齐的边界，供 verify 脚本逐字节对照。
    """
    dec = modified_chacha_decrypt(data, key, iv, SEED_RES_VERIFICATION)
    xored = mt19937_xor(dec, key + iv, SEED_KEY_DERIVATION)
    n = (xored[0] | (xored[1] << 8)) & 0x3FF
    return xored[2 + n:]


def _load_crypt_dll():
    """拿 native 解密 DLL 并绑好签名。不可用返回 None。"""
    from ysm import nativeload
    d = nativeload.load("ysmcrypt")
    if d is None:
        return None
    try:
        d.ysmc_decrypt.restype = ctypes.c_int
        d.ysmc_decrypt.argtypes = [
            ctypes.c_char_p, ctypes.c_size_t,      # payload, plen
            ctypes.c_char_p, ctypes.c_char_p,      # key, iv
            ctypes.c_char_p, ctypes.c_size_t,      # out, out_cap
        ]
    except AttributeError:
        return None
    return d


def decrypt_ysm_file(file_data: bytes, use_native: bool = True) -> bytes:
    """YsmCrypt.decryptYsmFile 的完整移植，返回解压后的明文字节。

    解密与哈希走 C（见 native/ysmcrypt.c），zstd 解压仍用 Python 的
    `zstandard`（成熟库没必要重写）。C 不可用时自动回退纯 Python。

    强制回退：环境变量 `YSM_NO_NATIVE=1`，或 `use_native=False`。
    """
    c = parse_container(file_data)
    if not c["hash_ok"]:
        raise ValueError(f"file hash mismatch: stored={c['stored_hash']:#x} calc={c['calc_hash']:#x}")
    if c["crypto"] != 3:
        raise ValueError(f"crypto version {c['crypto']} != 3")

    payload, key, iv = c["payload"], c["key"], c["iv"]
    stream = None
    if use_native:
        d = _load_crypt_dll()
        if d is not None:
            buf = ctypes.create_string_buffer(len(payload))
            n = d.ysmc_decrypt(
                ctypes.c_char_p(payload), ctypes.c_size_t(len(payload)),
                ctypes.c_char_p(key), ctypes.c_char_p(iv),
                buf, ctypes.c_size_t(len(payload)))
            if n >= 0:
                stream = buf.raw[:n]
    if stream is None:
        stream = decrypt_stream(payload, key, iv)
    return ysm_zstd_wash_and_decompress(bytearray(stream))


def ysm_zstd_wash_and_decompress(data: bytearray) -> bytes:
    """对应 ZstdUtil.decompress：先试一次性解压，内容长度未知时退回流式解压。"""
    import io
    import zstandard
    washed = ysm_zstd_wash(data)
    dctx = zstandard.ZstdDecompressor()
    try:
        size = dctx.frame_content_size(washed)
    except Exception:
        size = -1
    if size and size >= 0:
        return dctx.decompress(washed)
    with dctx.stream_reader(io.BytesIO(washed)) as rd:
        return rd.read()
