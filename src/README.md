# src 模块导航

YSM-pet 是**桌宠**项目：读 YSM 模型的 `.ysm`，把它当桌面宠物渲染出来。
没有训练、没有物理仿真 —— 那一整套（动捕数据管线 / 运动模型 / PPO 训练 /
GLTF 导出 / web 控制台）已在 v0.3.0 里整体删除。

| 目录 / 文件              | 一句话职责                                          |
| ------------------------ | --------------------------------------------------- |
| `pet/petapp.py`          | 桌宠窗口、渲染线程、右键菜单、设置面板、动画轮盘     |
| `pet/fastrender.py`      | 快速渲染器：向量化 FK + 相机矩阵 + C++ 光栅化       |
| `pet/camera.py`          | 相机矩阵（复刻 pybullet 的两个函数，纯 numpy）       |
| `pet/gaze.py`            | 视线追踪：头 / 眼跟随鼠标                            |
| `pet/modelsource.py`     | 模型来源解析：`.ysm` / 文件夹 / zip·7z·rar 压缩包     |
| `pet/loader.py`          | 素材加载：`.ysm` → 骨骼树 + 贴图 + 需隐藏骨骼         |
| `pet/settings.py`        | 设置持久化（记住模型与偏好），含老配置迁移            |
| `ysm/ysmcrypt.py`        | `.ysm` 解密（A5 专有算法）                           |
| `ysm/ysmbin.py`          | 二进制反序列化：烘焙面、动画、贴图字节               |
| `ysm/ysmmodel.py`        | 骨骼树构建（name / parent / pivot / cubes / faces）  |
| `ysm/animation.py`       | 动画通道采样（含 molang 表达式求值）                 |
| `ysm/molang.py`          | molang 表达式求值器                                  |
| `ysm/clips.py`           | 模型自带动画 → 可播放 clip；常驻并行层、ClipPlayer    |
| `ysm/nativeload.py`      | 加载 `native/*.dll`（失败自动回退纯 Python）         |
| `phys/bind.py`           | 坐标约定：模型空间 ↔ pybullet 空间的 `P_MAT`         |


## 渲染链路

```
.ysm ──ysmcrypt+ysmbin──> raw ──ysmmodel──> 骨骼树
                                              │
                    clips.ClipLibrary ────────┤─> 动画字典 {bone:{rot,pos,scale}}
                    petapp.IdleAnimator ──────┤
                    pet.gaze.GazeTracker ─────┘
                                              │
                              fastrender.FastRenderer
                              （FK → native/raster.dll 光栅化）
                                              │
                                      RGBA → 透明窗口
```

叠加顺序（后写覆盖）对齐原版 YSM 的分层：

```
待机（呼吸/摆头）→ pre_parallel → 主体动画 → parallel → 表情显隐 → 视线追踪
```
