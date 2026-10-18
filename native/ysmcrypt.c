// ============================================================
// YSM 解密核心（纯 C，A1 阶段：xor/tea 类流密码 + chunk 校验）
//
// 为什么写这个：解密链路里 CityHash64 / ChaCha20 变体 / MT19937-64 全是
// 纯 64 位整数运算，Python 逐字节循环很慢，换成 C 是数量级的下降。
//
// ⚠️⚠️ 本文件是 src/ysm/ysmcrypt.py 的**逐行搬运**，不是"标准算法实现"。
//     上游对 CityHash / ChaCha 做了魔改，任何"照标准写法写"的地方都会
//     和 Python 版不一致：
//       - hashLen16 是**三参**版本，第三参 mul = K2 + n*2（随长度变）
//       - cityHash64 >64 字节分支用 _weak() 且每轮 `x,z = z,x` 交换
//       - 带 seed 版用 (hash64 - K2) 再 hashLen16(..., seed)
//       - _rot 是**右旋**（rot(val, shift) = (val>>>shift)|(val<<(64-shift))）
//     改这里之前先读 Python 版那一行。
//
// 编译：见 scripts/build_native.py
// 调用：ctypes 裸指针，不依赖 Python C API。
//
// ⚠️ 工具链是 MinGW TDM-GCC 4.9.2 + MSVC 编译的 CPython。
//    绝不能用 std:: 容器 / 异常 / RTTI —— 会触发 libstdc++ 在 DllMain
//    初始化期崩溃（WinError 1114）。只用 malloc/free/memcpy + <stdint.h>。
// ============================================================
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define U64 uint64_t
#define U32 uint32_t

static const U64 K0 = 0xE4986A230E5AAA17ULL;
static const U64 K1 = 0x91AF10802CAB25A5ULL;
static const U64 K2 = 0xAF29CE778879D9C7ULL;
static const U64 KMUL = 0xDE0F6EE09BDBAB91ULL;

static const U64 SEED_FILE_VERIFICATION = 0x9E5599DB80C67C29ULL;
static const U64 SEED_KEY_DERIVATION = 0xD017CBBA7B5D3581ULL;
static const U64 SEED_RES_VERIFICATION = 0xA62B1A2C43842BC3ULL;

static U64 ld64(const uint8_t *p) { U64 v; memcpy(&v, p, 8); return v; }
static U32 ld32(const uint8_t *p) { U32 v; memcpy(&v, p, 4); return v; }

// Java Long.reverseBytes
static U64 bswap64(U64 v)
{
    return ((v & 0x00000000000000FFULL) << 56)
         | ((v & 0x000000000000FF00ULL) << 40)
         | ((v & 0x0000000000FF0000ULL) << 24)
         | ((v & 0x00000000FF000000ULL) << 8)
         | ((v & 0x000000FF00000000ULL) >> 8)
         | ((v & 0x0000FF0000000000ULL) >> 24)
         | ((v & 0x00FF000000000000ULL) >> 40)
         | ((v & 0xFF00000000000000ULL) >> 56);
}

// ------------------------------------------------------------ CityHash64

// CityHash.rotate：**右旋**。shift==0 时直接返回（避免 >>64 的 UB）。
static U64 rot(U64 val, int shift)
{
    shift &= 63;
    return shift == 0 ? val : ((val >> shift) | (val << (64 - shift)));
}

static U64 shift_mix(U64 v) { return v ^ (v >> 47); }

// CityHash.hashLen16(u, v, mul) —— 三参版本
static U64 hash_len16(U64 u, U64 v, U64 mul)
{
    U64 a = (u ^ v) * mul;
    a ^= a >> 47;
    U64 b = (v ^ a) * mul;
    b ^= b >> 47;
    return b * mul;
}

// CityHash.hash128to64(low, high) —— 等价于 _hash_len16(u,v) 两参版
static U64 hash128to64(U64 low, U64 high)
{
    U64 t1 = (low ^ high) * KMUL;
    U64 t2 = (shift_mix(t1) ^ low) * KMUL;
    return KMUL * shift_mix(t2);
}

// CityHash.weakHashLen32WithSeeds(w,x,y,z,a,b) -> (u64, u64)
static void weak32(U64 w, U64 x, U64 y, U64 z, U64 a, U64 b,
                   U64 *out0, U64 *out1)
{
    a += w;
    b = rot(b + a + z, 21);
    U64 c = a;
    a += x;
    a += y;
    b += rot(a, 44);
    *out0 = a + z;
    *out1 = b + c;
}

// _weak(buf, off, a, b)：从 buf+off 取 4 个 u64 当 w,x,y,z
static void weak_at(const uint8_t *buf, size_t off, U64 a, U64 b,
                    U64 *out0, U64 *out1)
{
    weak32(ld64(buf + off), ld64(buf + off + 8),
           ld64(buf + off + 16), ld64(buf + off + 24),
           a, b, out0, out1);
}

static U64 hash_len0to16(const uint8_t *b, size_t n)
{
    if (n >= 8) {
        U64 mul = K2 + n * 2;
        U64 a = ld64(b) + K2;
        U64 bb = ld64(b + n - 8);
        U64 c = rot(bb, 37) * mul + a;
        U64 d = (rot(a, 25) + bb) * mul;
        return hash_len16(c, d, mul);
    }
    if (n >= 4) {
        U64 mul = K2 + n * 2;
        U64 a = ld32(b);
        return hash_len16(n + (a << 3), ld32(b + n - 4), mul);
    }
    if (n > 0) {
        U64 a = b[0];
        U64 bb = b[n >> 1];
        U64 c = b[n - 1];
        U64 y = a + (bb << 8);
        U64 z = n + (c << 2);
        return shift_mix(y * K2 ^ z * K0) * K2;
    }
    return K2;
}

static U64 hash_len17to32(const uint8_t *b, size_t n)
{
    U64 mul = K2 + n * 2;
    U64 a = ld64(b) * K1;
    U64 bb = ld64(b + 8);
    U64 c = ld64(b + n - 8) * mul;
    U64 d = ld64(b + n - 16) * K2;
    U64 u = rot(a + bb, 43) + rot(c, 30) + d;
    U64 v = a + rot(bb + K2, 18) + c;
    return hash_len16(u, v, mul);
}

static U64 hash_len33to64(const uint8_t *b, size_t n)
{
    U64 mul = K2 + n * 2;
    U64 a = ld64(b) * K2;
    U64 bb = ld64(b + 8);
    U64 c = ld64(b + n - 24);
    U64 d = ld64(b + n - 32);
    U64 e = ld64(b + 16) * K2;
    U64 f = ld64(b + 24) * 9;
    U64 g = ld64(b + n - 8);
    U64 h = ld64(b + n - 16) * mul;
    U64 u = rot(a + g, 43) + (rot(bb, 30) + c) * 9;
    U64 v = ((a + g) ^ d) + f + 1;
    U64 w = bswap64((u + v) * mul) + h;
    U64 x = rot(e + f, 42) + c;
    U64 y = (bswap64((v + w) * mul) + g) * mul;
    U64 z = e + f + c;
    a = bswap64((x + z) * mul + y) + bb;
    bb = shift_mix((z + a) * mul + d + h) * mul;
    return bb + x;
}

static U64 city_hash64(const uint8_t *data, size_t n)
{
    if (n <= 16) return hash_len0to16(data, n);
    if (n <= 32) return hash_len17to32(data, n);
    if (n <= 64) return hash_len33to64(data, n);

    U64 x = ld64(data + n - 40);
    U64 y = ld64(data + n - 16) + ld64(data + n - 56);
    U64 z = hash128to64(ld64(data + n - 48) + n, ld64(data + n - 24));
    U64 v0, v1, w0, w1;
    weak_at(data, n - 64, n, z, &v0, &v1);
    weak_at(data, n - 32, y + K1, x, &w0, &w1);
    x = x * K1 + ld64(data);

    size_t length = (n - 1) & ~(size_t)63;
    size_t pos = 0;
    for (;;) {
        x = rot(x + y + v0 + ld64(data + pos + 8), 37) * K1;
        y = rot(y + v1 + ld64(data + pos + 48), 42) * K1;
        x ^= w1;
        y = y + v0 + ld64(data + pos + 40);
        z = rot(z + w0, 33) * K1;
        weak_at(data, pos, v1 * K1, x + w0, &v0, &v1);
        weak_at(data, pos + 32, z + w1, y + ld64(data + pos + 16), &w0, &w1);
        // Python: x, z = z, x
        U64 t = x; x = z; z = t;
        pos += 64;
        length -= 64;
        if (length == 0) break;
    }
    U64 u = hash128to64(v0, w0);
    return hash128to64(u + shift_mix(y) * K1 + z,
                       hash128to64(v1, w1) + x);
}

// cityHash64WithSeed(raw, k2, seed) == hashLen16(hash64 - k2, seed)
static U64 city_hash64_seed(const uint8_t *data, size_t n, U64 seed)
{
    return hash128to64(city_hash64(data, n) - K2, seed);
}

// ------------------------------------------------------------ MT19937-64

typedef struct { U64 mt[312]; int index; } Mt64;

static void mt64_init(Mt64 *m, U64 seed)
{
    m->mt[0] = seed;
    m->index = 312;
    for (int i = 1; i < 312; ++i) {
        U64 prev = m->mt[i - 1];
        m->mt[i] = 6364136223846793005ULL * (prev ^ (prev >> 62)) + (U64)i;
    }
}

static void mt64_twist(Mt64 *m)
{
    for (int i = 0; i < 312; ++i) {
        U64 x = (m->mt[i] & 0xFFFFFFFF80000000ULL)
              | (m->mt[(i + 1) % 312] & 0x7FFFFFFFULL);
        U64 xa = x >> 1;
        if (x & 1) xa ^= 0xB5026F5AA96619E9ULL;
        m->mt[i] = m->mt[(i + 156) % 312] ^ xa;
    }
    m->index = 0;
}

static U64 mt64_next(Mt64 *m)
{
    if (m->index >= 312) mt64_twist(m);
    U64 y = m->mt[m->index++];
    y ^= (y >> 29) & 0x5555555555555555ULL;
    y = y ^ ((y << 17) & 0x71D67FFFEDA60000ULL);
    y = y ^ ((y << 37) & 0xFFF7EEE000000000ULL);
    y ^= y >> 43;
    return y;
}

// ------------------------------------------------------------ ChaCha20 变体

// ⚠️ 这是上游的 **XChaCha20**，不是普通 ChaCha20，而且被魔改过：
//   1) 构造时先跑一轮 HChaCha20：state = SIGMA+key+nonce[0..4]，
//      shuffle 后取 x[0..3],x[12..15] 当子密钥；正式 state =
//      SIGMA + subkey + [0,0,nonce[4],nonce[5]]
//   2) 轮数不是固定 20，由 update_state_ysm 按 chunk 动态改：10*(h%3)+10
//   3) 解密按 chunk 走，每个 chunk 之间用 chunk 的 CityHash 搅动 state
//      （xor 到 state[4..15]，偶数下标用 lo、奇数用 hi）
// 少任何一条，解出来都是乱码 —— 而且不会有任何报错。
typedef struct {
    U32 state[16];    // 当前 state（会被 update_state_ysm 改）
    U32 block[16];    // 当前 keystream 块
    int rounds;
    int idx;          // 已消费到 block 的第几字节
} Chacha;

// ChaCha20Base.rotateLeft(i, i2) = (i >>> (-i2)) | (i << i2) → 左旋
static U32 rot32(U32 v, int s)
{
    s &= 31;
    return s == 0 ? v : ((v << s) | (v >> (32 - s)));
}

static void quarter(U32 *s, int a, int b, int c, int d)
{
    s[a] += s[b]; s[d] = rot32(s[d] ^ s[a], 16);
    s[c] += s[d]; s[b] = rot32(s[b] ^ s[c], 12);
    s[a] += s[b]; s[d] = rot32(s[d] ^ s[a], 8);
    s[c] += s[d]; s[b] = rot32(s[b] ^ s[c], 7);
}

// 对应 Python _shuffle(x, rounds)：每轮 4 次 column + 4 次 diagonal quarter
static void shuffle(U32 *x, int rounds)
{
    for (int r = 0; r < rounds; r += 2) {
        quarter(x, 0, 4, 8, 12);
        quarter(x, 1, 5, 9, 13);
        quarter(x, 2, 6, 10, 14);
        quarter(x, 3, 7, 11, 15);
        quarter(x, 0, 5, 10, 15);
        quarter(x, 1, 6, 11, 12);
        quarter(x, 2, 7, 8, 13);
        quarter(x, 3, 4, 9, 14);
    }
}

// XChaCha20.__init__：HChaCha20 派生 subkey 后构造正式 state
static void chacha_init(Chacha *c, const uint8_t *key, const uint8_t *nonce,
                        int rounds)
{
    static const uint8_t SIGMA[16] = {
        'e','x','p','a','n','d',' ','3','2','-','b','y','t','e',' ','k'
    };
    U32 x[16];
    for (int i = 0; i < 4; ++i) x[i] = ld32(SIGMA + i * 4);
    for (int i = 0; i < 8; ++i) x[4 + i] = ld32(key + i * 4);
    // nonce[0..4] 进 state[12..15]
    for (int i = 0; i < 4; ++i) x[12 + i] = ld32(nonce + i * 4);
    shuffle(x, rounds);
    U32 sub[8] = { x[0], x[1], x[2], x[3], x[12], x[13], x[14], x[15] };

    for (int i = 0; i < 4; ++i) c->state[i] = ld32(SIGMA + i * 4);
    for (int i = 0; i < 8; ++i) c->state[4 + i] = sub[i];
    c->state[12] = 0;
    c->state[13] = 0;
    c->state[14] = ld32(nonce + 16);   // nonce[4]
    c->state[15] = ld32(nonce + 20);   // nonce[5]
    c->rounds = rounds;
    c->idx = 64;    // 强制生成第一块
}

// _block()：shuffle 后加到 state，然后 counter[12],[13] 自增
static void chacha_block(Chacha *c)
{
    U32 w[16];
    for (int i = 0; i < 16; ++i) w[i] = c->state[i];
    shuffle(w, c->rounds);
    for (int i = 0; i < 16; ++i) c->block[i] = w[i] + c->state[i];
    c->state[12]++;
    if (c->state[12] == 0) c->state[13]++;
    c->idx = 0;
}

// updateStateYSM(h)：改轮数 + 把 h 的高低 32 位 xor 进 state[4..15]
static int chacha_state_ysm(Chacha *c, U64 h)
{
    c->rounds = 10 * (int)(h % 3) + 10;
    U32 lo = (U32)(h & 0xFFFFFFFFULL);
    U32 hi = (U32)((h >> 32) & 0xFFFFFFFFULL);
    for (int i = 4; i < 16; ++i)
        c->state[i] ^= (i % 2 == 0) ? lo : hi;
    return (int)(((h & 0x3F) | 0x40) << 6);
}

// ctx.process(data)：从当前位置连续异或出 n 字节
static void chacha_process(Chacha *c, const uint8_t *in, uint8_t *out, size_t n)
{
    size_t pos = 0;
    while (pos < n) {
        if (c->idx >= 64) chacha_block(c);
        out[pos] = in[pos]
                 ^ (uint8_t)(c->block[c->idx >> 2] >> ((c->idx & 3) * 8));
        c->idx++;
        pos++;
    }
}

// ------------------------------------------------------------ 导出接口

int ysmc_verify(const uint8_t *data, size_t len,
                U64 *stored_out, U64 *calc_out)
{
    if (len < 8) return 0;
    size_t tail = len - 64;
    U64 stored = ld64(data + tail + 56);
    U64 calc = city_hash64_seed(data, len - 8, SEED_FILE_VERIFICATION);
    if (stored_out) *stored_out = stored;
    if (calc_out) *calc_out = calc;
    return (int)(stored == calc);
}

U64 ysmc_cityhash64(const uint8_t *data, size_t len)
{
    return city_hash64(data, len);
}

U64 ysmc_cityhash64_seed(const uint8_t *data, size_t len, U64 seed)
{
    return city_hash64_seed(data, len, seed);
}

// 解密 payload（不含 zstd 解压）。
// out 必须 >= plen 字节。返回解出的密文流长度，失败 -1。
int ysmc_decrypt(const uint8_t *payload, size_t plen,
                 const uint8_t *key, const uint8_t *iv,
                 uint8_t *out, size_t out_cap)
{
    if (out_cap < plen) return -1;
    if (plen == 0) return 0;

    // --- 1) XChaCha20 解密（按 chunk，chunk 间搅动 state）---
    {
        uint8_t keyiv[32 + 24];
        memcpy(keyiv, key, 32);
        memcpy(keyiv + 32, iv, 24);

        U64 h2 = city_hash64_seed(keyiv, sizeof(keyiv), SEED_RES_VERIFICATION);
        size_t next_size = (size_t)(((h2 & 0x3F) | 0x40) << 6);
        Chacha c;
        memset(&c, 0, sizeof(c));
        chacha_init(&c, key, iv, 10 * (int)(h2 % 3) + 10);

        size_t ptr = 0;
        while (ptr < plen) {
            size_t n = next_size < (plen - ptr) ? next_size : (plen - ptr);
            chacha_process(&c, payload + ptr, out + ptr, n);
            ptr += n;
            if (ptr < plen) {
                U64 hh = city_hash64_seed(out + ptr - n, n,
                                          SEED_RES_VERIFICATION);
                next_size = (size_t)chacha_state_ysm(&c, hh);
            }
        }
    }

    // --- 2) MT19937-64 XOR，种子 = city_hash64_seed(key||iv, ...) ---
    {
        uint8_t kv[32 + 24];
        memcpy(kv, key, 32);
        memcpy(kv + 32, iv, 24);
        Mt64 mt;
        mt64_init(&mt, city_hash64_seed(kv, sizeof(kv), SEED_KEY_DERIVATION));
        size_t i = 0;
        while (i < plen) {
            U64 rnd = mt64_next(&mt);
            for (int j = 0; j < 8 && i < plen; ++j, ++i)
                out[i] ^= (uint8_t)(rnd >> (j * 8));
        }
    }

    // --- 3) 跳过 2 + n 字节 ---
    if (plen < 2) return -1;
    size_t n = (size_t)((out[0] | ((U64)out[1] << 8)) & 0x3FF);
    size_t skip = 2 + n;
    if (skip >= plen) return -1;
    memmove(out, out + skip, plen - skip);
    return (int)(plen - skip);
}
