# 07 · .ysm 格式解析器（已完成）

> 代码：`src/ysm/ysmcrypt.py`（解密）、`src/ysm/ysmbin.py`（二进制反序列化）、`src/ysm/ysmmodel.py`（骨骼/姿态）
> 自检：`tests/test_ysmcrypt_primitives.py`
> 端到端：`scripts/step3_parse_ysm.py` → `outputs/step3/<name>/`

## 1. 容器布局（crypto v3）

```
[0, hlen)      文本头  EF BB BF "YSGP\r\n" + Metadata/Tips/Export/SHA-256/Codec Version
[hlen]         0x00 终止符
[hlen+1,+4)    int32 LE = 3            crypto 版本
[hlen+5, -64)  加密载荷
[-64,-32)      key   (32 B)
[-32,-8)       iv    (24 B)
[-8,)          int64 LE 文件签名 = CityHash64WithSeed(data[:-8], SEED_FILE_VERIFICATION)
```

头部明文里有 `<format>` / `<crypto>`，但**真正的 format 要从解密后的载荷首 4 字节读**（上游有注释警告不要写死 32）。

## 2. 解密链（`YsmCrypt.decryptYsmFile`）

1. `modifiedChaChaDecrypt(payload, key, iv, SEED_RES_VERIFICATION)`
   - `hash2 = CityHash64WithSeed(key||iv, seed)`
   - 初始轮数 `10 * (hash2 % 3) + 10`，块大小 `((hash2 & 0x3F) | 0x40) << 6`
   - 每块解完后 `hash64WithSeed(明文块, seed)` → `updateStateYSM()`：改轮数 + 用 lo/hi 异或 state[4..15]
2. `mt19937Xor(..., key||iv, SEED_KEY_DERIVATION)` —— MT19937-64，CityHash 派生种子，64bit keystream
3. `n = (u16[0]) & 0x3FF` → 跳过 `2 + n` 字节 padding
4. `YsmZstd.wash()` → 标准 ZSTD 解压

### wash()：块头混淆还原
块头 3 字节被 YSM 重排：`last` 位不变，blockType 做 `0↔2 / 1↔3` 交换，尺寸字段存的是 `真实size ^ 0xD4E9`。帧头里 `FHD & 0xFB` 会清掉 single-segment 位。

### 关键实现坑（都已踩过）
| 坑 | 说明 |
|---|---|
| `_rot32` 方向 | ChaCha 的 `rotateLeft(i,i2) = (i >>> (-i2)) \| (i << i2)`，Java 移位距离取低 5 位 ⇒ **左旋**。写成 `(v>>s)\|(v<<(32-s))` 是右旋，RFC 8439 向量必挂 |
| `_rot` 方向 | CityHash 的 `rotate` 恰好是**右旋**，与名字直觉相反；别和 ChaCha 的混着改 |
| `hashLen16` 两个版本 | 3 参版 vs 2 参版（= `hash128to64`），`hash64WithSeed` 用的是后者 |
| `Long.reverseBytes` | `_hash_len33to64` 里 4 处，漏了会得到错误 magic |
| zstd 内容长度未知 | 帧头 FCS 标志为 0，`zstandard.decompress()` 会抛错；要用 `stream_reader`（上游 `ZstdUtil.decompress` 也是这么兜底的） |

## 3. 二进制载荷（format ≤ 15 → `deserializeLegacyV15`）

全部小端。原语：`dword`(4B) / `varint`(LEB128) / `float` / `string`(varint 长度 + UTF-8) / `bytearray`。

```
varint skipLen; skip                        # 垃圾头
varint modelCount
  { varint modelId; varint ==1; Geo }       # 1=main 2=arm 3=arrow
varint animationBlobCount
  { varint animId; varint ==1; AnimFile }   # 1..13 -> main/arm/extra/tac/arrow/...
varint controllerCount; { varint unk; CtrlBody }   # format>9
varint textureCount; { str name; bytes; w; h; varint subCount; {spec;bytes;w;h} }
varint soundCount;   { str; bytes }                # format>9
varint avatarCount;  { str; bytes; w; h }
varint modelTableSize;    { varint id; str hash }
varint animationTableSize;{ varint id; str hash }
varint textureTableSize;  { str; str; varint; {varint; str} }
ysm.json（见下）
```

### Geo（parseModels）
```
varint boneCount
  { str parent; varint cubeCount; { varint faceCount; { vec3 normal; 4×(vec3 pos; float u; float v) }; 3×varint }
    str name; 5×varint; vec3 pivot; vec3 rotation }
str identifier; float texH; float texW; float visH; float visW
varint n; n×float; float; float
varint hasInfoJson; [LegacyYSMInfo]
3×varint
```

**顶点已经是烘焙好的模型空间坐标（方块单位）**，UV 已展开，不需要再从 cube/box-UV 推导。pivot/rotation 仍是"像素"单位（÷16）。

### AnimFile（parseAnimations）
```
varint animCount
  { str name; float len(÷20→秒); varint loop        # 1=LOOP 3=HOLD_ON_LAST_FRAME 其他=PLAY_ONCE
    format>9: 2×varint; varint bwCount; {byte type; float|str}; varint
    varint boneCount; { str bone; Ch rot; Ch pos; Ch scale }
    varint timelineCount; { varint n; n×str; float t(÷20) }
    format>9: varint sfxCount; { str; float t } }
Ch: varint kfCount; { float t(÷20); varint mode; 3×(byte; float|str); varint hasPre; [3×(byte;…)] }
```

### ysm.json（parseYSMJson）
sha256 / isNewVersion / metadata(name,tips,license,authors[{name,role,contacts,comment}],links)
/ widthScale,heightScale / extraAnimations / (format>9: buttons, classifies)
/ defaultTexture / previewAnimation / isFree / (format>4: renderLayersFirst)
/ (format≥15: allCutout, disablePreviewRotation)

## 4. 渲染语义（NativeModelRenderer.calculateBoneMatrix）

```
local = M_parent
      · T((pivotX - aTx)/16, (pivotY + aTy)/16, (pivotZ + aTz)/16)
      · Rz(aRz) · Ry(aRy) · Rx(aRx)
      · S(aSx, aSy, aSz)
      · T(-pivot/16)
```

- 无动画时 local = I ⇒ **烘焙顶点已在模型空间**，直接 `M_bone · v` 即可，无需 inverse-bind。
- 动画旋转单位是**弧度**且 X/Y 已取反（`RotationValue.convert(deg, flip)`），等价于我们一贯的 `Rz(rz)·Ry(-ry)·Rx(-rx)`（度）。
- 动画平移等价于 `to_render(position)`（X 取反）。
- `scale` 三轴全 0 ⇒ 该骨骼及其子级**整体隐藏**。

## 5. 关键帧插值（BoneKeyFrameProcessor + EasingType）

- `contiguous = !hasPreData`：无 pre 时 `pre == post`（阶梯）；有 pre 时 pre 是上一段终点。
- 段 i（i≥1）：`[kf[i-1].t, kf[i].t]`，起点 `kf[i-1].post`，终点 `kf[i].pre`，段末瞬时值 `kf[i].post`。
- 段 0（Transition）：`[0, kf[0].t]` 全程取 `kf[0].pre`，末尾跳到 `kf[0].post`。
- 插值模式：`2 = catmullrom`，其余 linear。**选哪个关键帧的模式**：本帧是 CATMULLROM 或 i==0 用本帧，否则用前一帧（`BoneKeyFrameProcessor` L21-25）。
- 超过末尾：保持最后一个 `post`。

## 6. 特殊骨骼命名（上游约定）

| 前缀/名字 | 作用 |
|---|---|
| `ysmGlow*` | 自发光（fullbright），常配合 scale=0 做显隐 |
| `LeftArm` / `RightArm` / `Background` | partMask 1/2/3（分部位渲染、Background 始终渲染） |
| `gun` 等挂点 | 见 docs/06 的 35 组 locator 表 |
| `bk` / `KJ` 等 | 本模型 gui 预览用背景板（只在 gui 动画里被移动/隐藏） |

## 7. 两个样本模型

| | 亚托莉（ATRI） | 爱恩.伊瑞丝.卡列尼娜 |
|---|---|---|
| format | 15 | 15 |
| 骨骼 | 329（220 带网格） | 400（278 带网格） |
| 面 | 3228 | 11082 |
| 贴图 | ATRI 256×256 | IrisH/IrisJ/IrisL 512×512 |
| main 片段 | 53（其中若干为**空占位**：`idle`/`jump`/`climb` 0 骨骼） | 46 |
| 待机 | `hover`(600s) / `focus`(360s) | `idle`(120s) / `hover` |
| width/heightScale | 0.7 | — |

> ⚠️ 训练数据要注意：**有些模型的 `idle` 是空占位**，不能按名字取，要按"骨骼数>0 且长度有限"过滤。

## 8. C 加速与可复现性（A1–A5 结论）

### 8.1 native 层边界（教训）

TDM-GCC 4.9.2（libstdc++）编出的 C++ 不能进 MSVC-Python 进程 —— DllMain 初始化即崩（WinError 1114）。
`native/` 下**一律纯 C ABI**：无 `std::` 容器/异常/RTTI，只用 `malloc/free/memcpy` + `<stdint.h>`。

编译要点（`scripts/build_native.py` 已固化）：
- `.c` 必须用 **`gcc`** 编译。用 `g++` 编 `.c` 会按 C++ 规则 name mangling（`_crypt` → `_Z11...`），ctypes 找不到符号。
- TDM-GCC 的 gcc 默认 **C89**，`for (int i=...)` 会报错，必须加 `-std=c11`。
- **不要加 `-ffast-math`**：它允许编译器忽略 IEEE 舍入语义，把 `(double)tf / 20.0` 收缩回单精度除法。实测 ysmreader 因此有 2144/18201 处 `t` 值偏 ~1e-8。A4 要求逐字段对照，可复现性优先。

### 8.2 逐字段对照的真正难点：float 宽度

Python 侧 `struct.unpack('<f', ...)` 得到的**不是** float32，而是"float32 值精确表示成的 double"；`/ 20.0` 也是 **double 除法**，结果仍是 double。
所以 C 侧：
- `tf / 20.0f` → float 除法，偏 ≥1 ULP ❌
- `(float)((double)tf / 20.0)` → 最后又多舍入一次，仍偏 1 ULP ❌
- `double *ts` 输出 `(double)tf / 20.0` → **逐位相同** ✅

分量值（`vals`）保持 `float` 数组即可：ctypes 读回 `c_float` 得到的也是 float32 精确表示的 double，天然一致。

对照脚本的容差**必须是 0**。`verify_channel.py` 最初用 `1e-5`，恰好把上述 1e-8 的系统性偏差全吃掉 —— 18201 个通道"全部通过"，实为假绿。

### 8.3 关于「解析搬进 C」的实测结论

| 路径 | 耗时 | 结论 |
|---|---|---|
| 朴素 Python（逐字段调 Reader） | 0.624 s | 基线 |
| Python 内联版（`parse_channel_py`） | **0.388 s** | **快 27%，默认启用** ✅ |
| C 版（`ysmr_parse_channel`） | 0.708 s | 慢 13%，默认关（`YSM_USE_C_CHANNEL=1` 才走） |
| 逐字段 ctypes | 0.119 s vs 0.033 s | **慢 3.3x**，永远别这么用 |

原因：`parse_channel` 的瓶颈是「**构造几万个 Python dict**」，不是"读字节慢"。原语只占全量 6% —— 即便原语全免费，全量最多省 6%。C 版反而多跑两趟搬运（建 8 个 ctypes 大数组 + 拿扁平结果回 Python 组 dict），而 dict 构造两种方案都免不了。
**正确的优化是减少函数调用（内联），不是换语言读字节。**

C 版仍作为「已验证资产」保留：它已与 Python 版逐帧、逐游标一致（18201 个真实通道，EPS=0）。

### 8.4 A3 的真实热点在采样，不在几何

原任务写的是"ysmmodel 建骨骼树 + 烘焙面重组 → C"。实测：
- `YsmModel.pose()/faces()` **已不在渲染热路径**上 —— `FastRenderer` 在构造时一次性建好骨骼索引/父子表，每帧自己做向量化 FK + 批量变换 + 调 `raster.dll` 光栅化。搬进 C 无收益。
- 真正的瓶颈是 `ysm/animation.py:sample_channel`：**每帧对同一条通道重跑 `_keyframes()`**（解析 dict + `_vec` + `sorted`），而通道对象在模型生命周期内不变。

落地的三处纯 Python 优化（`scripts/verify_anim_opt.py` 验收）：

| 位置 | 改动 | 效果 |
|---|---|---|
| `animation.sample_channel` | 加 `_prep()` 预解析缓存（`id(channel)` 为键 + 强引用防复用；`clear_prep_cache()` 在换模型时调用） | 采样 1.883 → 0.679 ms/帧（**2.8x**） |
| `fastrender.pose_matrices` | 共享只读 `_EYE4` 取代循环里的 `np.eye(4)` | `numpy.eye` 73200 → 18900 次 |
| `fastrender._dirty` | 按 `frozenset(anim)` 缓存脏骨骼集（返回已排序 list） | 省掉每帧 BFS + `sorted` |

整帧：**7.21 → 5.80 ms/帧（+24%）**。验收：68580 次采样逐字段差 0；真实动画逐像素差 0。

### 8.5 开关

- `--legacy`（`run_pet.py`）/ `YSM_NO_NATIVE=1`：关掉**全部** native，退回纯 Python 解析器。
- `YSM_NO_NATIVE=raster,ysmcrypt`：只关指定模块。
- `YSM_USE_C_CHANNEL=1`：显式启用 C 版通道解析（做交叉校验用）。
