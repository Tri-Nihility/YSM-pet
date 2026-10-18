# 12 YSM 现代二进制格式（format 16~32）读取规格

> 本文档描述 **Yes Steve Model（YSM / openysm）`.ysm` 二进制载荷**在解密、解压之后的**字节读取顺序**。
> 反向来源：本机 mod jar 的 **反编译 Java 源码**（不是猜测、不是字节码推断）。
> 目标读者：要在 `src/ysm/ysmbin.py` 里实现/核对 `fmt >= 16`（modern）解析的人。

---

## 0. 证据链（可复现）

| 项 | 值 |
|---|---|
| 目标 jar（未混淆，首选） | `C:\game\wdsj\.minecraft\versions\自然之需YSM兼容整合（基于Arcomua Cloth 1.21.11 Fabric 260331）\mods\openysm-fabric-2.6.5.9-mc1.21.11.jar`（28,077,905 B，版本 2.6.5.9 / mc1.21.11） |
| 反编译工具 | CFR 0.152 |
| 反编译产物（读取端，权威） | `C:\private\DSH\DSH_1\temp\cfr\src\com\elfmcys\yesstevemodel\resource\YSMBinaryDeserializer.java`（910 行） |
| 反编译产物（写入端，交叉验证） | `…\resource\YSMBinarySerializer.java`（561 行） |
| POJO | `…\resource\pojo\RawYsmModel.java`（含全部内部类） |
| 文件夹格式（对照，另有一套） | `…\resource\YSMFolderDeserializer.java`（1281 行） |
| 原语类（jar 内，另有反编译） | `rip.ysm.security.YSMByteBuf` |
| 入口调用者 | `com.elfmcys.yesstevemodel.model.ServerModelManager$1`（磁盘路径）、`com.elfmcys.yesstevemodel.client.ClientModelManager`（网络/缓存路径） |

**本文所有「类 + 方法 + 行号」中的行号，均指上述 CFR 产物文件内的行号。**

### 0.1 入口：谁在读，format 从哪里来

两条真实入口（javap 反汇编取证）：

1. **磁盘 `.ysm` 文件（正常加载路径）** — `ServerModelManager$1.visitFile`（javap 偏移 154~186）：
   ```
   int cryptoVer = YesModelUtils.getYsmCryptoVersion(rawBytes);   // 偏移 54
   byte[] dec    = YsmCrypt.decryptYsmFile(rawBytes);             // 偏移 154
   d = new YSMBinaryDeserializer(dec);        // ← 1 参构造器：吃掉载荷首个 dword 当 format
   model = d.deserializeKeepOpen();
   d.parseYSMFooter(model);                   // ← footer 由调用方显式解析
   d.close();
   ```
2. **服务端缓存 / 网络同步** — `ClientModelManager.parseAndLoadModel`（javap 偏移 0~43）与 `ServerModelManager`（javap 偏移 192~233）：
   ```
   d = new YSMBinaryDeserializer(data, 32);   // ← 2 参构造器：不读 dword，format 硬编码 32
   d.deserializeKeepOpen();
   reader = d.getReader();
   // 后面自己内联读 footer（不调用 parseYSMFooter）
   ```
   > 注意：这条路径 **不消耗** 载荷里的 format dword；也就是说，服务端缓存 / 网络数据的载荷是被剥掉 4 字节头的，或该路径只用于本插件自己写出的 format=32 文件。

结论：**`.ysm` 文件载荷（解密后）的第一个字段就是 4 字节 LE 的 format dword**，`YSMBinaryDeserializer(byte[])` 只读它一次，之后所有字段按本文档顺序读取。

### 0.2 原语语义（`rip.ysm.security.YSMByteBuf`，字节序强制 LITTLE_ENDIAN）

| 原语 | 实现 | Python 对应 |
|---|---|---|
| `readByte()` | netty `readByte()`，**有符号** byte（-128~127） | `Reader.byte()` |
| `readDword()` | netty `readUnsignedInt()` 返回 **long**（4 B LE 无符号） | `Reader.dword()` |
| `readFloat()` | netty `readFloat()`（LE IEEE-754） | `Reader.float()` |
| `readVarInt()` | LEB128，累加到 **int**（有符号，溢出回绕），每字节 `position += 7`，`position >= 64` 抛 `VarInt too big`，最多 9 字节 | `Reader.varint()` ✅ 语义一致 |
| `readVarLong()` | 同上但累加到 long，错误串 `VarLong too big` | `Reader.varlong()` |
| `readString()` | `len = readVarInt()`；`len == 0` → `""`；否则读 len 字节 UTF-8 | `Reader.string()` ✅ |
| `readByteArray()` | `len = readVarInt()`；`len == 0` → `new byte[0]`；否则读 len 字节 | `Reader.byte_array()` ✅ |
| **bool** | **没有专用方法**，一律 `readVarInt() != 0` | — |

`readVector3D()`（YSMBinaryDeserializer.java:862）＝连续 3 个 `readFloat()`。

---

## 1. 版本分派表（全部 `format` 守卫）

### 1.1 顶层三岔（YSMBinaryDeserializer.deserializeInternal :40-48）

| format | 分支 |
|---|---|
| `< 4` | `deserializeLegacyV1()`（:89-172） |
| `4 … 15` | `deserializeLegacyV15()`（:174-322） |
| `>= 16` | **`deserializeModern()`（:324-373）** ← 本文重点 |

分派之后统一执行（:49-52）：`model.projectiles` 里 `model == null || textures.isEmpty()` 的条目被删除。**modern 不调用 `parseYSMFooter`**，footer 由调用方在 `deserializeKeepOpen()` 之后单独读。

写入端 `YSMBinarySerializer.serialize`（:23-34）：`format >= 16` 才写，否则 `UnsupportedOperationException`；`writeModern`（:59-61）：`format < 26` 也直接抛异常 —— 即**写端只能产出 format ≥ 26 的文件**，所以 format 16~25 的文件只能用读端知识解析，无法用写端交叉验证。

### 1.2 逐条守卫清单

| # | 类.方法 | 行 | 守卫 | 命中时多读/少读的东西 |
|---|---|---|---|---|
| 1 | `deserializeInternal` | :42 | `format < 4` | → `deserializeLegacyV1` |
| 2 | `deserializeInternal` | :44 | `format <= 15` | → `deserializeLegacyV15` |
| 3 | `deserializeLegacyV1` | :135 | `format < 4` | 贴图循环里多读一个 varint 标志（必须 == 1） |
| 4 | `deserializeLegacyV15` | :213 | `format > 9` | 多一整个「动画控制器段 + 控制器 hash 回填表」 |
| 5 | `deserializeLegacyV15` | :250 | `format > 9` | 多一整个「声音段 + 声音 hash 回填表」 |
| 6 | `deserializeModern` | :329 | `format < 26` | 只有 1 段子实体（`"SubEntity"`）+ 末尾一个**读了丢弃**的 varint |
| 7 | `deserializeModern` | :335 | `format >= 26` | 两段子实体（`"Vehicle"`、`"Projectile"`），无末尾 varint |
| 8 | `parseSubEntity` | :378 | `format <= 26` | 子实体 identifier **从流里读字符串** |
| 9 | `parseSubEntity` | :413 | `format > 26` | 尾部多读 `varint footerFlag + string identifier`（覆盖 :378 的名字） |
| 10 | `parseYSMJson` | :484 | `format <= 15` 且 `isNewVersionYsm != 0` | 多读 1 个 varint（丢弃） |
| 11 | `parseYSMJson` | :508 | `isNewVersionYsm == 0 && format <= 15` | **直接 return**（后面全部不读） |
| 12 | `parseYSMJson` | :523 | `format > 9` | 多读「扩展动画按钮段 + 分类段」 |
| 13 | `parseYSMJson` | :562 | `format > 4` | 多读 `renderLayersFirst`(bool) |
| 14 | `parseYSMJson` | :565 | `format >= 15` | 多读 `allCutout`(bool) + `disablePreviewRotation`(bool) |
| 15 | `parseYSMJson` | :569 | `format > 15` | 多读 `guiNoLighting`(bool) + GUI 前景/背景图 + **头像段** |
| 16 | `parseYSMJson` | :571 | `format >= 32` | 多读 `mergeMultilineExpr`(bool) |
| 17 | `parseYSMJson` | :593 | `format <= 15` | **return**（背景图段不读） |
| 18 | `parseYSMJson` | :596 | 恒（`format > 15`） | 背景图段 |
| 19 | `parseAnimations` | :635 | `format > 9` | 多读每条动画的 `unkInt1/unkInt2/blendWeightMolang/unkInt4` |
| 20 | `parseAnimations` | :669 | `format > 9` | 多读每条动画的「音效事件段」 |
| 21 | `parseAnimationControllers` | :729 | `format <= 15` | 改为读 `legacyUnknownInt`(varint)，name 用 `"legacy_controller_" + i` 合成 |
| 22 | `parseAnimationControllers` | :732-737 | `format > 15` | 读 `name`（仅当 `readName=true`）+ `hash`(string) |
| 23 | `parseAnimationControllerBody` | :779 | `format > 26` | 每个 state 多读「音效名列表段」 |
| 24 | `parseSoundFiles` | :796 | `format > 15` | 每个声音多读 `hash`(string) |
| 25 | `parseYSMFooter` | :71 | `format < 9` | **直接 return**，footer 不读 |
| 26 | `parseYSMFooter` | :74 | `format > 26` | `footer.version` 从流里读 varint；否则取 `format` 本身（**不消耗字节**） |
| 27 | `parseYSMFooter` | :79 | `format >= 24` | 多读 `unkInt2`(varint) |
| 28 | `YSMBinarySerializer.serialize` | :25 | `format >= 16` | 否则抛 `UnsupportedOperationException` |
| 29 | `YSMBinarySerializer.writeModern` | :59 | `format < 26` | 抛 `UnsupportedOperationException`（写端能力边界） |
| 30 | `YSMBinarySerializer.writeSubEntities` | :129 | `format > 26` | 多写 `writeVarInt(1) + writeString(identifier)` |
| 31 | `YSMBinarySerializer.writeAnimationFileContent` | :190 | `format > 9` | 同 #19 |
| 32 | `YSMBinarySerializer.writeAnimationControllerBody` | :308 | `format <= 26` → `continue` | 不写 state 音效（与读端 #23 对齐） |
| 32b | `YSMBinarySerializer.writeAnimationFileContent` | :215 | `format <= 9` → `continue` | 不写动画音效（与读端 #20 对齐） |
| 32c | `YSMBinarySerializer.writeAnimationFileContent` | :190-198 | `format > 9` | 写 `unkInt1/unkInt2/hasBlend/writeMolangValue/unkInt4`（与读端 #19 的 `blendWeightMolangCount` 对应：`hasBlend` 只写 0 或 1，非 0 时再由 `writeMolangValue` 写 `1 字节类型 + 值`） |
| 33 | `YSMBinarySerializer.writeSoundFiles` | :355 | `format > 15` | 同 #24 |

> 读端与写端在 **format > 9 / > 15 / > 26 / >= 32** 这几处分界完全对齐；`>= 24`（#27）只有读端 footer 用到。

---

## 2. 顶层读取顺序（modern，format 16~32）——可直接照抄的伪代码

**前置**：`r` 已停在 format dword 之后（dword 由构造器吃掉）。

```python
def deserialize_modern(r, fmt, m):
    # ---------- ① 固定三段（无版本守卫，modern 独有） ----------
    m.sounds    = parseSoundFiles(r, fmt)      # varint 数量 → §3.9
    m.functions = parseFunctionFiles(r)        # varint 数量 → §3.10
    m.languages = parseLanguageFiles(r)        # varint 数量 → §3.11

    # ---------- ② 子实体段 ----------
    if fmt < 26:
        n = r.varint()                         # 子实体总数
        for i in range(n):
            m.vehicles[identifier] = parseSubEntity(r, fmt, "SubEntity", i)
        r.varint()                             # ★ 读了丢弃（J:334，见 §5.1）
    else:
        n = r.varint()                         # 载具总数
        for i in range(n):
            m.vehicles[identifier] = parseSubEntity(r, fmt, "Vehicle", i)
        n = r.varint()                         # 投射物总数
        for i in range(n):
            m.projectiles[identifier] = parseSubEntity(r, fmt, "Projectile", i)

    flag = r.varint()                          # 哨兵，必须 == 1，否则 RuntimeException
    #                                          #   "Expected 1 after SubEntities"

    # ---------- ③ 主实体动画表（type + hash 内联） ----------
    n = r.varint()
    for _ in range(n):
        t = r.varint()                         # animationId（1~13，见 §3.7 映射表）
        h = r.string()                         # 该动画包的 hash
        af = parseAnimations(r, fmt)           # §3.6
        m.animations[ANIM_KEY_FROM_TYPE.get(t, "unknown")] = af
        af.anim_type = t ; af.file_hash = h

    # ---------- ④ 主实体控制器 / 贴图 ----------
    parseAnimationControllers(r, fmt, read_name=True)   # §3.8
    parseTextureFiles(r)                                # §3.12 → m.textures

    # ---------- ⑤ 主实体模型表（modelType + hash 内联） ----------
    n = r.varint()
    for _ in range(n):
        mt = r.varint()                        # 1=主模型 2=手臂模型 3=箭
        h  = r.string()
        geo = parseModels(r)                   # §3.1（无版本差异）
        geo.sha256 = h ; geo.model_type = mt
    assignMainModels(...)                      # 1→main_model 2→arm_model 3→projectiles["minecraft:arrow"]

    # ---------- ⑥ ysm.json ----------
    parseYSMJson(r, fmt)                       # §3.13
```

### 2.1 modern 与 legacy(≤15) 的顶层差异（父 agent 明确要求的回答）

| 差异点 | legacy（4~15） | modern（16~32） |
|---|---|---|
| **开头 skip 块** | **有**：`n = varint(); skipBytes(n)`（:175-176 / :91-92） | **没有**。载荷紧跟 format dword 就是声音段的数量 varint |
| 段顺序 | 模型 → 动画 →（>9 控制器）→（>9 声音）→ 贴图 → 头像 → 3 张 hash 回填表 → ysm.json | 声音 → 函数 → 语言 → 子实体 → 哨兵 → 动画 → 控制器 → 贴图 → 模型 → ysm.json |
| **hash 回填表** | **有 3 张独立表**（模型表、动画表、贴图表；贴图表里还嵌一层子贴图表），位于数据段之后 | **完全没有**。每个条目的 hash 就在条目头部（`hash` 紧跟 `name`/`type`），流式回填 |
| 文件段（functions） | 无 | **有**（哈希 + 字节） |
| 语言段（languages） | 无 | **有**（hash + kv 表） |
| 子实体 | 只有箭头投射物（靠贴图名 `"arrow.png"` / `"/ARROW\"` 特判，模型靠 `modelType == 3`） | **一等公民**：`vehicles` / `projectiles`，各自带完整动画、控制器、base 贴图 + 子贴图、完整几何模型、identifier |
| 贴图条目 | `name, data, width, height`（`imageFormat` 恒为 -1，**不落盘**） | `name, hash, data, width, height, imageFormat, unknownFlag` |
| 声音条目 | `name, data` | `name, hash, data` |
| 动画表条目 | `animId, pad(==1), parseAnimations` | `type, hash, parseAnimations` |
| 模型表条目 | `modelId, pad(==1), parseModels` 放在最前 | `modelType, hash, parseModels` 放在控制器/贴图之后 |
| ysm.json 尾部 | 到 `disablePreviewRotation` 为止 | 再追加 `guiNoLighting / guiForeground / guiBackground / 头像段 / 背景图段`（`>= 32` 还有 `mergeMultilineExpr`） |
| footer | 由 `parseYSMFooter` 显式读（:69-87） | 同 |

---

## 3. 子结构字段序列（逐字段）

类型记号：`vi`=varint（有符号 32 位 LEB128）、`vl`=varlong、`s`=string（varint 长度前缀 UTF-8）、`b[]`=byteArray（varint 长度前缀）、`f`=float(LE)、`y`=byte(有符号)、`bool`=varint!=0。

### 3.1 `parseModels()` → RawGeometry（:421-476，**无任何版本守卫**，legacy/modern 共用）

```
boneCount: vi
for each bone:
    parentName: s
    cubeCount: vi
    for each cube:
        faceCount: vi
        for each face:
            normal: f,f,f
            ×4 (v=0..3):  positions[v]: f,f,f ;  u[v]: f ;  v[v]: f
        unkInt1, unkInt2, unkInt3: vi ×3
    name: s
    unkPad1..unkPad5: vi ×5
    pivot: f,f,f
    rotation: f,f,f
identifier: s
textureHeight: f          ← 注意源码里 Height 先读，Width 后读
textureWidth: f
visibleBoundsHeight: f
visibleBoundsWidth: f
visibleBoundsOffsetSize: vi
visibleBoundsOffset: f × visibleBoundsOffsetSize
unkFloat1: f
unkFloat2: f
hasInfoJsonFlag: vi
if hasInfoJsonFlag > 0:  parseLegacyYSMInfo()   # §3.2
footerPad1, footerPad2, footerPad3: vi ×3
```

> 循环边界来源：`boneCount/cubeCount/faceCount` 各是紧邻的 varint；`visibleBoundsOffset` 的浮点个数由它自己的 varint 决定。

### 3.2 `parseLegacyYSMInfo()`（:609-624，**无版本守卫**）

```
name: s ; tips: s
extraAnimationsCount: vi
    → 每项只 readString() 并丢弃
authorsCount: vi
    → 每项只读 author.name: s
licenseType: s
isFree: bool
```

### 3.3 `parseChannel(List<RawKeyframe>)`（:683-723）

```
keyframeCount: vi
if keyframeCount == 0: return          # ★ 不读任何东西
for each keyframe:
    timestamp: f  (Java 里 /20.0 转秒；Python 同)
    interpolationMode: vi
    # ① 三元组 A（typed）
    for j in 0..2:
        datatype: y                    # ★ 是 1 字节 byte，不是 varint
        if datatype == 1: value = f
        elif datatype == 2: value = s
        else: 该分量留 null，**不消耗任何字节**
    hasPreData: vi > 0
    if hasPreData:
        # ② 三元组 B（typed，同上规则）
        for j in 0..2: datatype: y ; 1→f / 2→s / 其它→null
        kf.preData  = 三元组 A
        kf.postData = 三元组 B
    else:
        kf.postData = 三元组 A
```

### 3.4 `parseAnimations()` → RawAnimationFile（:626-681）

```
animationCount: vi
for each anim:
    name: s
    length: f   (÷20.0)
    loopMode: vi
    if fmt > 9:
        unkInt1: vi ; unkInt2: vi
        blendWeightMolangCount: vi
        for each: datatype: y ; 1→f ; 2→s ; 其它→不读
        unkInt4: vi
    boneCount: vi
        for each: boneName: s ; parseChannel(rotation) ; parseChannel(position) ; parseChannel(scale)
    timelineEventGroupsCount: vi
        for each group: timelineEventsCount: vi ; 每项 event: s ; timestamp: f (÷20.0)
    if fmt > 9:
        soundEffectsCount: vi
        for each: effectName: s ; timestamp: f (÷20.0)
    animations.put(name, anim)
```
> 注意 `blendWeight` 在 Java 里是**单值字段**，循环里被反复覆盖（最后一个赢）；写端 `writeMolangValue` 也只写一个 —— Python 实现成 list 会与 Java 不同（见 §4.3）。

### 3.5 `parseAnimationControllers(List, readName)`（:725-741）

```
controllerCount: vi
for i, each file:
    if fmt <= 15:  legacyUnknownInt: vi ; name = "legacy_controller_" + i
    else:          if readName: name: s ;  hash: s
    parseAnimationControllerBody(file.controllers)   # §3.8 前身
```
`readName` 的两种取值：主实体传 `true`（:358），子实体传 `false`（:386 / legacy :214），legacy 走 `false`。

### 3.6 `parseAnimationControllerBody(Map)`（:743-789）＝ §3.8

### 3.7 动画类型 → key 映射（`YSMFolderDeserializer.getAnimKeyFromType`，:1042-1059）

| type | key | type | key |
|---|---|---|---|
| 1 | `main` | 8 | `swem` |
| 2 | `arm` | 9 | `slashblade` |
| 3 | `extra` | 10 | `tlm` |
| 4 | `tac` | 11 | `fp_arm` |
| 5 | `arrow` | 12 | `immersive_melodies` |
| 6 | `carryon` | 13 | `irons_spell_books` |
| 7 | `parcool` | 其它 | `unknown` |

（与 `ysmbin.py:16-20` 的 `ANIM_KEY_FROM_TYPE` **逐项一致**。`MODEL_SLOT_FROM_TYPE` 只在 legacy 路径用。）

### 3.8 `parseAnimationControllerBody(Map)`（:743-789）

```
animationCount: vi
for each entry:
    animationName: s
    initialState: s
    statesCount: vi
    for each state:
        name: s
        animationsSize: vi      → 每项 (s, s)
        transitionsSize: vi     → 每项 (s, s)
        onEntryCount: vi        → 每项 s
        onExitCount: vi         → 每项 s
        if vi != 0:  blendTransitionValue: f
        else:        blendTransitionsCount: vi → 每项 (f, f)
        blendViaShortestPath: bool
        if fmt > 26: soundEffectsCount: vi → 每项 s
    targetMap.put(animationName, entry)
```

### 3.9 `parseSoundFiles()`（:791-802）

```
soundCount: vi
for each: soundName: s ; if fmt > 15: hash: s ; data: b[]
```

### 3.10 `parseFunctionFiles()`（:804-812，**无版本守卫**）

```
functionCount: vi
for each: functionName: s ; hash: s ; data: b[]
```

### 3.11 `parseLanguageFiles()`（:814-826，**无版本守卫**）

```
languageCount: vi
for each: languageName: s ; hash: s ; nodesCount: vi → 每项 (s, s)
```

### 3.12 `parseTextureFiles(Map)`（:828-854，modern 主实体贴图）

```
textureCount: vi
for each tex:
    name: s ; hash: s ; data: b[]
    width: vi ; height: vi ; imageFormat: vi ; unknownFlag: vi
    subTextureSize: vi
    for each sub:
        specularType: vi
        specHash: s ; specData: b[]        # = parseSpecialImage()，:856-860
        width: vi ; height: vi ; imageFormat: vi ; unknownFlag: vi
    targetMap.put(name, tex)
```

### 3.13 `parseSubEntity(Map, categoryName, index)`（:375-419）

```
identifier: (fmt <= 26) ? s : categoryName + "_" + index
animationCount: vi
for each: hash: s ; parseAnimations()  → animationFiles.put(categoryName, af) ; af.fileHash = hash
parseAnimationControllers(controllers, readName=false)        # §3.5
# base 贴图（不是 parseTextureFiles，是 parseSpecialImage + 散字段）
hash: s ; data: b[]                                           # parseSpecialImage()
width: vi ; height: vi ; imageFormat: vi ; unknownFlag: vi
name = "base_texture_" + index
subTextureSize: vi
for each: specularType: vi ; hash: s ; data: b[] ; width: vi ; height: vi ; imageFormat: vi ; unknownFlag: vi
modelHash: s
parseModels()  （§3.1） ; model.sha256 = modelHash
if fmt > 26:  footerFlag: vi ; identifier: s     ← 覆盖开头的 identifier
targetMap.put(identifier, subEntity)
```
> 注意 base 贴图的子贴图挂在 **base 贴图对象**上；`subTextures` 是 base 的字段。

### 3.14 `parseYSMJson()`（:478-607）——逐字段 + 全部守卫

```
properties.sha256: s
isNewVersionYsm: vi
if isNewVersionYsm != 0:
    if fmt <= 15: vi（读后丢弃）
    metadata.name: s ; tips: s ; licenseType: s ; licenseDescription: s
    authorsCount: vi
    for each author:
        name: s ; role: s
        contactsCount: vi → 每项 (s, s)
        comment: s
    linksCount: vi → 每项 (s, s)
if isNewVersionYsm == 0 and fmt <= 15: RETURN          # ★ 提前返回
properties.widthScale: f ; heightScale: f
extraAnimationsCount: vi → 每项 (s, s)（异常包 RuntimeException）
if fmt > 9:
    extraAnimationButtonsCount: vi
    for each button:
        id: s ; name: s ; vi（读后丢弃）
        configurationFormsCount: vi
        for each form:
            type: s ; title: s ; description: s ; defaultValue: s
            step: f ; min: f ; max: f
            labelsSize: vi → 每项 (s, s)
    extraAnimationClassifyCount: vi
    for each classify: id: s ; classificationExtrasCount: vi → 每项 (s, s)
properties.defaultTexture: s
previewAnimation: s
isFree: bool
if fmt > 4:  renderLayersFirst: bool
if fmt >= 15: allCutout: bool ; disablePreviewRotation: bool
if fmt > 15:
    guiNoLighting: bool
    if fmt >= 32: mergeMultilineExpr: bool
    guiForeground: s ; guiBackground: s
    avatarsCount: vi
    for i, each avatar:
        name: s ; data: b[] ; width: vi ; height: vi ; format: vi ; unknownFlag: vi
        if i < len(metadata.authors): authors[i].avatar = name ; authors[i].avatarImage = avatar
        else: metadata.extraAvatars.add(avatar)
if fmt <= 15: RETURN                                  # ★ 第二个提前返回
backgroundImagesCount: vi
for each bg: name: s ; data: b[] ; width: vi ; height: vi ; format: vi ; unknownFlag: vi
```

### 3.15 `parseYSMFooter(RawYsmModel)`（:69-87）——**不会被 deserialize 自动调用**

```
if fmt < 9: return
footer.version = (fmt > 26) ? vi : fmt      # fmt<=26 时**不消耗字节**
footer.unkInt1: vi
footer.rand: s
footer.time: vl                             # ← 唯一一个 varlong
footer.extra: s
if fmt >= 24: footer.unkInt2: vi
（整段 try/catch，异常只打印 "ERROR" + 栈，不抛出）
```

写端 `writeFooter`（YSMBinarySerializer.java:36-53）：
```
if footer == null or footer.version == 65535: writeVarInt(65535) ; writeVarInt(0) ; writeVarLong(0) ; return
writeVarInt(version != 0 ? version : 65535)
writeVarInt(unkInt1)
if unkInt1 != 0: writeString(rand)
writeVarLong(time)
if unkInt1 != 0: writeString(extra) ; writeVarInt(unkInt2)
```
> 读端无条件读 rand/extra/unkInt2，写端用 `unkInt1 != 0` 门控；且写端总是写 version，读端只在 `fmt > 26` 时读 version。**两端不对称**（§5.2）。

另外，`ClientModelManager.parseAndLoadModel`（javap 偏移 23~123）内联了**第三套** footer 读取逻辑，条件与**写端**一致：
```
version = vi ; unkInt1 = vi
if unkInt1 != 0: rand = s
time = vl
if unkInt1 != 0: extra = s ; unkInt2 = vi
```
即：**footer 有两种读法** —— 磁盘路径（`parseYSMFooter`，按 format 分支）与缓存/网络路径（按 `unkInt1` 门控，与写端自洽）。

### 3.16 `assignMainModels(List)`（:866-887）

```
switch modelType: 1 → mainEntity.mainModel
                  2 → mainEntity.armModel
                  3 → projectiles["minecraft:arrow"] = {model: geo}
                  default → RuntimeException("Unknown model type: " + modelType)
```

---

## 4. 与 Python 现状（`C:\private\YSM-Motion\src\ysm\ysmbin.py`）的对照

> 行号取自当前工作副本（847 行）。函数名稳定，行号若漂移请以函数名为准。

### 4.1 函数级映射（已实现的现代路径）

| Python | 行 | Java 对应 | 状态 |
|---|---|---|---|
| `Reader.byte/dword/float/varint/varlong/string/byte_array/vec3/skip/offset` | :31-106 | `YSMByteBuf` 全部原语 | ✅ 语义一致 |
| `parse_models` | :133 | `parseModels` :421 | ✅（含 `has_info_json → parse_legacy_ysm_info`、footer_pad 三元组） |
| `parse_legacy_ysm_info` | :175 | `parseLegacyYSMInfo` :609 | ✅ |
| `parse_channel` / `parse_channel_py` / `_parse_channel_c` | :287 / :317 / :221 | `parseChannel` :683 | ✅（C 版仅 `YSM_USE_C_CHANNEL=1` 时启用） |
| `_typed` | :408 | 内联的 `datatype` 分支 | ✅ |
| `parse_animations` | :417 | `parseAnimations` :626 | ✅（`fmt > 9` 两处守卫齐） |
| `parse_controller_body` | :448 | `parseAnimationControllerBody` :743 | ✅（`fmt > 26` state 音效齐） |
| `parse_animation_controllers` | :470 | `parseAnimationControllers` :725 | ✅（`fmt <= 15` 分支齐） |
| `parse_sound_files` | :482 | `parseSoundFiles` :791 | ✅（`fmt > 15` hash 齐） |
| `parse_ysm_json` | :492 | `parseYSMJson` :478 | ⚠ 见 §4.3 —— 缺 1 处提前 return |
| `deserialize_legacy_v15` | :580 | `deserializeLegacyV15` :174 | ✅（legacy 路径） |
| `parse_texture_files` | :705 | `parseTextureFiles` :828 | ✅ |
| `parse_function_files` | :721 | `parseFunctionFiles` :804 | ✅ |
| `parse_language_files` | :731 | `parseLanguageFiles` :814 | ✅ |
| `parse_sub_entity` | :742 | `parseSubEntity` :375 | ✅（`fmt <= 26` / `fmt > 26` 两条都写了） |
| `assign_main_models` | :772 | `assignMainModels` :866 | ✅ |
| `deserialize_modern` | :786 | `deserializeModern` :324 | ✅（含 `fmt < 26` 那个「读了丢弃」的 varint，:766 附近注释也写了） |
| `deserialize` | :837 | `deserializeInternal` :40 + 两个构造器 | ⚠ 见 §4.3 |

**结论：现代格式的移植在函数层面已经齐了**（这是本次调查最重要的对照结论）。

### 4.2 legacy 功能对照（缺失项）

| Java | Python | 差距 |
|---|---|---|
| `deserializeLegacyV1` :89 | 无（`deserialize` 对 fmt<4 抛 `NotImplementedError`） | 未移植（任务范围外） |
| `parseYSMFooter` :69 | **无** | 缺 footer 解析（`varlong time`、`fmt>=24` 的 `unkInt2`、`fmt>26` 的 version 分支） |
| `deserializeInternal` 尾部的 projectiles 清理 :49-52 | 无 | 缺 `model==null 或 textures 为空则删除` 的收尾 |

### 4.3 已发现的语义偏差（**建议核对/修正**，本文档不改代码）

1. **`parse_ysm_json` 缺 `isNewVersionYsm == 0 && fmt <= 15` 的提前 return**（Java :508）。
   Python（:495-543）在 `isNewVersionYsm == 0` 且 `fmt <= 15` 时会继续读 `widthScale/heightScale/...`，而 Java 直接返回 → **legacy 旧包（老 ysm.json，`isNewVersionYsm == 0`、format 4~15）会错位**。现代格式（fmt>15）不受影响。
2. **背景图段的守卫方式不同但结果相同**：Java 用 `if (format <= 15) return;`（:593）再无条件读背景图；Python 把背景图放在 `if fmt > 15:` 块内（:545-569）。对 `fmt >= 16` 两者等价。
3. **`blendWeight` 语义**：Java 是单值字段，`format > 9` 的循环里**反复覆盖（最后一个生效）**；Python（:425）收集成 `[_typed(r) for _ in range(...)]` 列表。两者消耗的字节数相同，但取值不同。
4. **`deserialize(data, fmt)` 的头部语义与 Java 不一一对应**（ysmbin.py:837-842）：显式传 `fmt` 时 Python **仍然吃掉**载荷首个 dword；Java 的 `YSMBinaryDeserializer(byte[], int)` 是**不吃 dword** 的（:33-38）。Python 的「显式 fmt 路径」= Java 的「1 参构造器 + 强制覆盖 format」语义，而**不是** Java 的 2 参构造器语义。若要与 Java 2 参路径对齐，需要在传 fmt 时**不**消费 dword。
   另外 Python 允许 `1 <= fmt <= 64`，Java 无此校验。
5. **`deserialize()` 不调用 footer**：与 Java 一致（Java 也由调用方显式调用 `parseYSMFooter`），但 Python 连 `parseYSMFooter` 都没有 → 文件尾部（`rand/time/extra`）目前被忽略。
6. Python 的动画表回填（:809-811）把 `anim_type` / `file_hash` 写在**每个 anim dict** 上；Java 写在 `RawAnimationFile` 上（一个动画包一个字段）。结构性差异，不影响字节序。

---

## 5. 不确定项清单（明确标注「不确定」）

### 5.1 `deserializeModern` 里 `format < 26` 独占的那个「读了丢弃」的 varint —— **不确定其语义**
- 证据（源码）：`YSMBinaryDeserializer.java:329-334`
  ```java
  if (this.format < 26) {
      int subEntityTotalCount = this.reader.readVarInt();
      for (i = 0; i < subEntityTotalCount; ++i) this.parseSubEntity(this.model.vehicles, "SubEntity", i);
      i = this.reader.readVarInt();          // ← 赋值给共用循环变量 i，之后未被使用
  } else { … }
  ```
- 证据（字节码，早期 javap）：该分支内对应偏移只有 4 条指令 `aload_0 / getfield reader / invokevirtual readVarInt / istore_2` + `goto`，**没有第二个循环**，所以确实是「读一个 varint 然后丢弃」，不是 CFR 漏还原的循环。
- 不确定点：它可能是某个已废弃的「投射物计数」，也可能是「子实体段长度」。**但字节层面必须照读**，否则后面 `Expected 1 after SubEntities` 的哨兵 varint 会错位。
- 无法交叉验证：写端 `writeModern` 对 `format < 26` 直接抛异常（:59-61），所以没有对应的写逻辑。

### 5.2 footer 两套读法 —— **不确定哪个是「真格式」**
- 读法 A `parseYSMFooter`（:69-87）：`version` 仅 `fmt > 26` 才从流里读；`rand/extra/unkInt2` **无条件**读；`unkInt2` 仅 `fmt >= 24`。
- 读法 B `ClientModelManager.parseAndLoadModel`（javap 偏移 23~123）：`version` 总是读；`rand/extra/unkInt2` 由 `unkInt1 != 0` 门控；没有 `fmt` 相关分支。
- 写端 `writeFooter`（:36-53）与读法 B 自洽。
- 判断：**磁盘文件路径走的是读法 A**（`ServerModelManager$1.visitFile` javap 偏移 181 调用 `parseYSMFooter`），因此对 `.ysm` 文件应以读法 A 为准；读法 B 用于服务端缓存/网络载荷。两者是否对同一批字节等价，取决于 `unkInt1` 与 `fmt` 的实际取值，**不保证等价**。
- 附带的确定性结论：这两个字段不对称在 format 16~26 区间会直接导致**字节错位**（写端写了 version，读法 A 不读）——若真有 format 16~26 的文件，读法 A 会解错 footer。**不确定**这类文件是否真实存在（写端只产出 ≥26）。

### 5.3 format 16~25 的真实样本 —— **不确定是否存在**
写端 `writeModern` 在 `format < 26` 抛 `UnsupportedOperationException`，2.6.5.9 只能写出 format ≥ 26。因此本文档里 format 16~25 的路径**只来自读端源码**（`fmt > 15`、`fmt <= 26` 等分支），**没有实测文件验证**。重点目标 format 26 与 32 都在可写范围内。

### 5.4 字段语义未知（顺序确定、含义不确定）
`unkInt1/2/3`（cube）、`unkPad1..5`（bone）、`unkFloat1/2`、`footerPad1..3`、`unknownFlag`（贴图/头像）、`imageFormat` 的具体枚举、`unkInt1/2`（动画）、`unkInt4`、`controller file.hash` 的用途 —— 源码里就没有更进一步的命名信息。本文档只保证**读取顺序与类型**。

### 5.5 容器层（加密/压缩）**未逐字节逆向**
本次只取证到调用点：`YesModelUtils.getYsmCryptoVersion(byte[])`、`YsmCrypt.decryptYsmFile(byte[])`、`YsmCrypt.read(byte[], byte[])`、`YsmCrypt.encryptYsmFile(byte[])`。`.ysm` 文件在解密/解压**之前**的字节布局（魔数、版本、CRC、压缩算法）**本次未分析**，不属于「二进制载荷读取顺序」，故不在本文档范围内。若需要，下一步应 dump `rip/ysm/security/YsmCrypt` 的 `decryptYsmFile`（javap 中位于 :825-983）与 `rip/ysm/legacy/YesModelUtils.getYsmCryptoVersion`。

### 5.6 `YSMByteBuf.readVarInt` 的溢出行为
`position >= 64` 才抛异常（最多 9 字节），而累加目标是 **int**：第 5 字节起就会溢出回绕成负数。Python 的 `varint()` 做了 32 位回绕（与 Java int 一致）——**这点是确定一致的**；但真实文件里是否出现 >5 字节的 varint **未知**。

### 5.7 legacy 的 `skip` 块含义 —— **不确定**
legacy 载荷开头 `n = varint(); skipBytes(n)` 跳过的字节是什么（注释名 `unknownNeedSkipBytes`）源码未说明；`YSMByteBuf.skipGarbageHeader()` 是**另一回事**（只用于网络包 `handlePacket03`/`handlePacket05`，javap 偏移 363/1 与 882/1），modern 载荷里**没有**对应调用。

---

## 6. 移植到 Python 的工作量评估

**好消息：核心已经写完了。** `ysmbin.py` 当前已经包含 `deserialize_modern`（:786）及现代路径所需的一切子函数（§4.1 全 ✅），顶层顺序、`fmt < 26` 的丢弃 varint、`parseSubEntity` 的两套 identifier、`fmt > 26` 的 state 音效、`fmt > 15` 的贴图/头像、`fmt >= 32` 的 `mergeMultilineExpr` 都已覆盖。

剩余待补（按价值排序）：

| # | 工作 | 位置 | 量级 |
|---|---|---|---|
| 1 | `parse_ysm_json` 补 `if isNewVersionYsm == 0 and fmt <= 15: return` | `parse_ysm_json` :495 之后 | ~3 行 |
| 2 | 新增 `parse_footer(r, fmt, m)` 对应 `parseYSMFooter`（§3.15 读法 A） | 新函数 + `deserialize` 尾部可选调用 | ~12 行 |
| 3 | `deserialize(data, fmt)` 的 dword 语义对齐 Java 2 参构造器（传 fmt 时不吃 dword） | `deserialize` :837-842 | ~2 行 + 调用方核对 |
| 4 | projectiles 收尾清理（`model is None or not textures` → 删除） | `deserialize` 尾部 | ~3 行 |
| 5 | `deserializeLegacyV1`（format < 4） | 新函数 | ~80 行（低优先级，现代格式任务范围外） |
| 6 | `blendWeight` 语义是否要与 Java 一致（单值 vs 列表） | `parse_animations` :425 | 决策问题，0~3 行 |

**总计：现代格式约 20 行以内即可与 Java 读端 1:1 对齐**（第 1/3/4 项），footer 是唯一新增的结构（第 2 项）。

---

## 7. 实施与实测记录（2026-10-05）

### 7.1 本次改动（`src/ysm/ysmbin.py`、`src/pet/loader.py`）

| 项 | 内容 |
|---|---|
| §6 第 1 项 | `parse_ysm_json` 存下 `is_new`，补 `if is_new == 0 and fmt <= 15: return`（修 legacy 旧包错位真 bug，`format 9` 的小死神就是靠这条才解开） |
| §6 第 2 项 | 新增 `parse_footer(r, fmt)`（`version`（仅 fmt>26 落盘）/`unk_int1`/`rand`/`time`(varlong)/`extra`/(fmt>=24)`unk_int2`）；`deserialize` 尾部调用并记录 `m.footer`、`m.footer_offset` |
| §6 第 3 项 | `deserialize(data, fmt)` 传 fmt 时改为「读出来的 dword 恰好等于 fmt 才吃掉，否则把指针退回」，兼容 1 参/2 参两种构造器语义 |
| §6 第 4 项 | `deserialize` 尾部按 `:49-52` 清掉没有模型或没有贴图的投射物 |
| 现代路径 | `deserialize_modern` + `parse_texture_files/parse_function_files/parse_language_files/parse_sub_entity/assign_main_models` 本次补齐并接进 `deserialize()` 的 `else` 分支（原先 `fmt >= 16` 直接 `raise NotImplementedError`） |
| 贴图 | `pet/loader.py:_decode_image` 改为先用 PIL 试解（返回 `(arr, 格式名)`），失败才退回裸 RGBA/RGB；顺带修掉原 PNG 分支只返回单值、被 `arr, kind =` 解包即崩的潜在 bug |

### 7.2 实测：16 个模型全部解析通过，且**尾部残留 = 0**

`data/cache/*.bin` 与 `data/decrypted/*.bin` 全量跑（脚本 `C:\private\DSH\DSH_1\temp\ysm_all_test.py`）。
「尾残留 = 0」表示从 format dword 一路读到 footer 最后一字节**没有一个字节错位**，这是比「不报错」强得多的正确性证据（`体残留` 就是 footer 的长度）。

| 模型 | fmt | 体残留(=footer 长) | 尾残留 | footer.version/unk_int2 | 动画段/动画 | 控制器 | 贴图 | 声音 | 子实体 |
|---|---|---|---|---|---|---|---|---|---|
| atri_* ×4、亚托莉（目标模型） | 15 | 24 | 0 | 15 / — | 9 / 263 | 1 | 2 | 0 | 0 |
| 亚托莉目标模型 | 15 | 24 | 0 | 15 / — | 9 / 263 | 1 | 2 | 0 | 0 |
| 爱恩.伊瑞丝.卡列尼娜 ×2 | 15 | 24 | 0 | 15 / — | 8 / 235 | 0 | 4 | 0 | 0 |
| 草神 | 15 | 24 | 0 | 15 / — | 4 / 32 | 1 | 1 | 0 | 0 |
| 疾旋鼬 | 4 | 0 | 0 | 无 footer（fmt<9） | 4 / 136 | 0 | 2 | 0 | 0 |
| 小死神_特莉波卡 | 9 | 24 | 0 | 9 / — | 3 / 85 | 0 | 2 | 0 | 0 |
| **芙宁娜v3_14中文配音** | **26** | 25 | 0 | 26 / 0 | 9 / 394 | 1 | 1 | 40 | 2 |
| 斯卡蒂②25_12_19 | 28 | 26 | 0 | 28 / 0 | 9 / 562 | 2 | 1 | 35 | 3 |
| 铃兰2025_12_19 | 28 | 26 | 0 | 28 / 0 | 10 / 423 | 3 | 1 | 4 | 0 |
| 明日方舟_Mon3tr | 32 | 26 | 0 | 32 / 0 | 5 / 61 | 1 | 1 | 0 | 0 |
| 苏沐星 | 32 | 26 | 0 | 32 / 0 | 11 / 351 | 2 | 1 | 2 | 0 |

覆盖 format **4 / 9 / 15 / 26 / 28 / 32** 六个版本分支（`fmt>=24` 的 `unk_int2`、`fmt>26` 的 footer version 都实测命中）。

### 7.3 实测：现代贴图是压缩图（legacy 是裸像素）

| 模型 | image_format | 数据 | 实测格式 |
|---|---|---|---|
| 芙宁娜（fmt 26） | 4 | 1024×1024，65,404 B | **WebP**（`RIFF` 头） |
| 斯卡蒂（fmt 28）/ 铃兰（fmt 28）/ 苏沐星（fmt 32） | 2 | 512×512 | PNG |
| 明日方舟_Mon3tr（fmt 32） | 2 | 512×512，36,647 B | PNG |
| atri / 小死神 / 疾旋鼬（fmt ≤ 15） | -1（不落盘） | 256×256 = 262,144 B | 裸 RGBA |

→ `_decode_image` 必须走 PIL 嗅探；`image_format` 的具体枚举含义仍未确认（2=PNG、4=WebP 只是这两类样本的观察值），**不要**按它分支。

### 7.4 实测：端到端渲染（`scripts/run_pet.py --smoke --frames 2`）

| 输入 | 骨骼 | 贴图 | 帧耗时 | 覆盖像素 |
|---|---|---|---|---|
| `data/cache/芙宁娜v3_14中文配音_356edde1b021.bin`（fmt 26） | 573 | 内嵌 texture(webp) 1024×1024 | 33.8 / 23.2 ms | 8776 (14.6%) |
| `data/cache/明日方舟_Mon3tr_105f5fb07bf6.bin`（fmt 32） | 258 | 内嵌 texture(png) 512×512 | 12.3 / 6.7 ms | 4596 (7.7%) |
| `data/cache/小死神_特莉波卡_e34a38036713.bin`（fmt 9） | 129 | 偏粉(rawRGBA) 256×256 | 19.9 / 9.8 ms | 10302 (17.2%) |
| `C:\Users\WO\Downloads\斯卡蒂②25_12_19.ysm` | 1277 | 内嵌 texture(png) 512×512 | 28.5 / 20.3 ms | 9055 (15.1%) |
| `C:\Users\WO\Downloads\铃兰2025_12_19.ysm` | 1552 | 内嵌 texture(png) 512×512 | 23.3 / 17.0 ms | 5735 (9.6%) |
| `C:\Users\WO\Downloads\苏沐星.ysm` | 269 | 内嵌 default(png) 256×256 | 13.4 / 8.4 ms | 8455 (14.1%) |
| `C:\Users\WO\Downloads\疾旋鼬.ysm` | 146 | 内嵌 chillte1(rawRGBA) 128×128 | 12.5 / 7.6 ms | 9217 (15.4%) |

全部 `exit=0` 并写出 `outputs/pet/smoke_*.png`；`.ysm` 容器（`YSGP` 头 + 加密区）走 `ysmcrypt` 解密后再交给现代解析器，同样通过。

### 7.5 仍未做

- `deserializeLegacyV1`（fmt < 4，§6 第 5 项）仍抛 `NotImplementedError`；本机没有 fmt<4 的样本。
- `blendWeight` 单值/列表语义未对齐（§5 不确定项 5 相关），目前不影响渲染。
- fmt 16~25 无真实样本（写端只产出 ≥26），这些分支只有读端源码支撑。
