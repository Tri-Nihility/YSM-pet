// ============================================================
// YSM 二进制读取原语（纯 C，A2 阶段）
//
// 定位：**只做逐字节的原语**，不做结构解析。
// 为什么这样切：ysmbin 的输出是深层嵌套的 Python 对象树，如果 C 直接
// 构造这棵树，桥接开销会吃掉全部收益（见 scripts/bench_ysmbin.py 的
// 结论）。真正慢的是 varint/varlong/string 这类逐字节循环，把这一层
// 换成 C，数据结构仍由 Python 组装，收益最大且改动面最小。
//
// 接口设计：**游标式**。C 侧只维护一个"当前位置"，Python 传指针+长度，
// 每次调用返回读到的值 + 新位置。位置由 Python 持有，C 保持无状态，
// 这样不会有两边状态不同步的问题。
//
// ⚠️ 必须与 src/ysm/ysmbin.py 的 Reader 完全一致，尤其是：
//    - varint 按 32 位回绕后**解释为有符号 int**
//    - varlong 是**无符号**的
//    - string/byte_array 的长度前缀是 varint，n==0 时直接返回空
// 编译：scripts/build_native.py（.c 走 gcc，保证纯 C ABI 不被 mangle）
// ============================================================
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define EXPORT __declspec(dllexport)

// 实现 ysmbin.Reader.varint()：Java readVarInt 语义
// 返回读到的有符号值；out_pos 更新为读完后位置。
// 出错（越界 / VarInt too big）返回 0 并把 *err 置 1。
EXPORT int32_t ysmr_varint(const uint8_t *b, size_t len, size_t *pos,
                           int *err)
{
    uint32_t value = 0;
    int position = 0;
    size_t p = *pos;
    for (;;) {
        if (p >= len) { *err = 1; return 0; }
        uint8_t cb = b[p++];
        value |= (uint32_t)(cb & 0x7F) << position;
        if ((cb & 0x80) == 0) break;
        position += 7;
        if (position >= 64) { *err = 2; return 0; }
    }
    *pos = p;
    // 与 Java int 一致：按 32 位有符号解释
    return (int32_t)value;
}

// 实现 ysmbin.Reader.varlong()：无符号 64 位
EXPORT uint64_t ysmr_varlong(const uint8_t *b, size_t len, size_t *pos,
                             int *err)
{
    uint64_t value = 0;
    int position = 0;
    size_t p = *pos;
    for (;;) {
        if (p >= len) { *err = 1; return 0; }
        uint8_t cb = b[p++];
        value |= (uint64_t)(cb & 0x7F) << position;
        if ((cb & 0x80) == 0) break;
        position += 7;
        if (position >= 64) { *err = 2; return 0; }
    }
    *pos = p;
    return value;
}

// 读 varint 长度前缀的 UTF-8 字符串。
// 不解码 —— 只把 [起始, 长度) 回传，Python 侧用 bytes[pos:pos+n] 解。
// 返回长度 n；*out_start 是字符串数据起点。出错返回 -1。
EXPORT int64_t ysmr_string_span(const uint8_t *b, size_t len, size_t *pos,
                                size_t *out_start, int *err)
{
    int e = 0;
    int32_t n = ysmr_varint(b, len, pos, &e);
    if (e) { *err = e; return -1; }
    if (n < 0) { *err = 3; return -1; }      // 负长度非法
    if (*pos + (size_t)n > len) { *err = 1; return -1; }
    *out_start = *pos;
    *pos += (size_t)n;
    return n;
}

// 批量读 float12（vec3）：一次调用读 count 个 vec3
// out 需 count*3 个 float 空间。成功返回 count，出错返回 -1。
EXPORT int64_t ysmr_vec3_batch(const uint8_t *b, size_t len, size_t *pos,
                               float *out, size_t count, int *err)
{
    size_t need = count * 12;
    if (*pos + need > len) { *err = 1; return -1; }
    memcpy(out, b + *pos, need);
    *pos += need;
    return (int64_t)count;
}

// 批量读 float32 数组
EXPORT int64_t ysmr_float_batch(const uint8_t *b, size_t len, size_t *pos,
                                float *out, size_t count, int *err)
{
    size_t need = count * 4;
    if (*pos + need > len) { *err = 1; return -1; }
    memcpy(out, b + *pos, need);
    *pos += need;
    return (int64_t)count;
}

// ============================================================
// A2 核心：一次调用解完**一个动画通道**
//
// 为什么整通道一次调用，而不是"逐字段调 C"：
// scripts/bench_ysmbin.py 实测，逐字段 ctypes 跨界比纯 Python 还慢 3.3x
// （Python 字节索引太便宜了，ctypes 调用开销反而更大）。所以 C 必须
// **一次吃掉整个循环**，只在最后回传扁平数组。
//
// 上游 parseChannel 语义（务必一致）：
//   n = varint；n==0 → 空通道
//   每帧：t = float/20, mode = varint
//         first = 3 个 typed
//         hasPre = varint；>0 则 first 是 pre，再读 3 个 typed 当 post
//                           否则 first 就是 post
//   typed: 0x01→float, 0x02→varint长度前缀的字符串, 其它→None
//
// 输出（扁平）：
//   ts[i]     时间戳（已 /20）
//   modes[i]  插值模式
//   haspre[i] 0/1
//   vals[]    每帧 6 个 float：pre[3] 然后 post[3]；缺失的位置填 NAN
//   kinds[]   每帧 6 个字节：0=数字, 1=字符串, 2=None
//   字符串统一追加到 strbuf，str_off/str_len 给出每处的切片位置
//
// 返回关键帧数；出错返回 -1，*err 给错误码。
// 调用方需保证输出缓冲足够（活帧数上界 = 剩余字节数，够用）。
// ============================================================
#include <math.h>

#define KF_VAL 6           // 每帧 6 个分量
#define KIND_NUM 0
#define KIND_STR 1
#define KIND_NONE 2

// 读一个 typed 值。返回 kind；数字写 *fval，字符串写 *soff/*slen。
static int read_typed(const uint8_t *b, size_t len, size_t *pos,
                      float *fval, size_t *soff, size_t *slen, int *err)
{
    if (*pos >= len) { *err = 1; return KIND_NONE; }
    uint8_t dt = b[*pos];
    (*pos)++;
    if (dt == 0x01) {
        if (*pos + 4 > len) { *err = 1; return KIND_NONE; }
        float f;
        memcpy(&f, b + *pos, 4);
        *pos += 4;
        *fval = f;
        return KIND_NUM;
    }
    if (dt == 0x02) {
        size_t st = 0;
        int e = 0;
        int64_t n = ysmr_string_span(b, len, pos, &st, &e);
        if (e) { *err = e; return KIND_NONE; }
        *soff = st;
        *slen = (size_t)n;
        return KIND_STR;
    }
    return KIND_NONE;
}

EXPORT int64_t ysmr_parse_channel(
    const uint8_t *b, size_t len, size_t *pos,
    double *ts, int32_t *modes, uint8_t *haspre, float *vals,
    uint8_t *kinds,
    size_t *str_off, size_t *str_len, size_t str_cap, size_t *str_n,
    int *err)
{
    int e = 0;
    int64_t n = ysmr_varint(b, len, pos, &e);
    if (e) { *err = e; return -1; }
    if (n <= 0) return 0;

    size_t ns = *str_n;
    for (int64_t i = 0; i < n; ++i) {
        // t
        if (*pos + 4 > len) { *err = 1; return -1; }
        float tf;
        memcpy(&tf, b + *pos, 4);
        *pos += 4;
        // ⚠️ ts 必须是 double，且以 double 除法算，才能与 Python 逐位相同。
        // Python 侧是 `struct.unpack('<f', b, pos)[0] / 20.0`：
        //   unpack 得到的是 float32 值（精确表示为 double），
        //   再以 **double** 除 20.0，结果**仍是 double** 存进 dict。
        // 所以 C 侧也必须返回 double：
        //   - 写 `tf / 20.0f`            → float 除法，偏 1 ULP 以上
        //   - 写 `(float)((double)tf/20.0)` → 最后又多舍入一次，仍偏 1 ULP
        //   - 写 `ts[i] = (double)tf / 20.0`（ts 为 double）→ 逐位相同 ✅
        ts[i] = (double)tf / 20.0;
        // mode
        modes[i] = ysmr_varint(b, len, pos, &e);
        if (e) { *err = e; return -1; }

        // first 三个（字符串位置在循环内直接登记到 str_off/str_len，
        // 不需要单独缓存 —— 有 pre 时这三个分量本身就是最终结果）
        for (int j = 0; j < 3; ++j) {
            float fv = NAN;
            size_t so = 0, sl = 0;
            int k = read_typed(b, len, pos, &fv, &so, &sl, &e);
            if (e) { *err = e; return -1; }
            kinds[i * KF_VAL + j] = (uint8_t)k;
            if (k == KIND_NUM) {
                vals[i * KF_VAL + j] = fv;
            } else if (k == KIND_STR) {
                vals[i * KF_VAL + j] = NAN;
                if (ns < str_cap) { str_off[ns] = so; str_len[ns] = sl; ns++; }
            } else {
                vals[i * KF_VAL + j] = NAN;
            }
            (void)so; (void)sl;
        }

        int hp = ysmr_varint(b, len, pos, &e);
        if (e) { *err = e; return -1; }
        haspre[i] = (uint8_t)(hp > 0 ? 1 : 0);

        if (hp > 0) {
            // first 是 pre，再读 3 个当 post
            for (int j = 0; j < 3; ++j) {
                float fv = NAN;
                size_t so = 0, sl = 0;
                int k = read_typed(b, len, pos, &fv, &so, &sl, &e);
                if (e) { *err = e; return -1; }
                kinds[i * KF_VAL + 3 + j] = (uint8_t)k;
                if (k == KIND_NUM) {
                    vals[i * KF_VAL + 3 + j] = fv;
                } else {
                    vals[i * KF_VAL + 3 + j] = NAN;
                    if (k == KIND_STR && ns < str_cap) {
                        str_off[ns] = so; str_len[ns] = sl; ns++;
                    }
                }
            }
        } else {
            // first 就是 post，pre 位留空
            for (int j = 0; j < 3; ++j) {
                kinds[i * KF_VAL + 3 + j] = KIND_NONE;
                vals[i * KF_VAL + 3 + j] = NAN;
            }
        }
    }
    *str_n = ns;
    return n;
}

// 自检：DLL 可用性探针
EXPORT int ysmr_selftest(void)
{
    // varint 往返：0xAC 0x02 -> 300
    uint8_t buf[4] = { 0xAC, 0x02, 0xFF, 0x01 };
    size_t pos = 0;
    int err = 0;
    int32_t a = ysmr_varint(buf, 4, &pos, &err);
    int32_t bv = ysmr_varint(buf, 4, &pos, &err);
    // 300 和 255，且 pos 应到 4
    if (a != 300 || bv != 255 || pos != 4 || err) return 1;
    return 0;
}
