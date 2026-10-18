# YSM-pet

把 **YSM（Yes Steve Model）模型**变成 Windows 桌宠的小项目：透明无边框、永远置顶的
小人贴在桌面上，播模型自带的动画，头能跟着鼠标光标转，右键弹出动画轮盘。

- 不需要 GPU，也不需要联网 —— C++ 光栅化路径下单帧约 10–25 ms（CPU 即可）；
- 不需要 Python 也能跑 —— `YSM-pet.spec` 一条命令打出免安装的单文件夹版；
- 模型版权归作者，**本仓库不含任何模型文件**，自己准备 `.ysm` 即可。

> 📋 完整开发日志（12 篇，含每个坑的取证过程）在 `docs/`，其中
> `docs/10-桌宠项目.md` 是主日志，`docs/11-打包版修复与重打包说明.md` 讲打包。

---

## 功能

| 功能 | 说明 |
|---|---|
| 模型加载 | `.ysm` / 文件夹 / `.zip` / `.7z` / `.rar`；解密读取走自带原生 DLL |
| 动画播放 | 模型自带 main 组动作 + 原版 YSM 的 `pre_parallel`/`parallel` 常驻层（默认关，开着会把身体藏掉） |
| 动画轮盘 | 右键菜单，复刻上游 `AnimationRouletteScreen` 布局（每页 8 项、45° 扇区） |
| 清晰模式（默认开） | 只画人物本体，藏掉特效 / 立绘 / 游戏内 UI 部件（YSM 模型常把这些拆成独立根骨骼） |
| 视线追踪（默认关） | 头跟随鼠标光标。**准确度问题已修复**，在「设置 → 视线」里手动开，或启动加 `--gaze` |
| 程序化待机 | 呼吸 + 头部缓慢摆动 + 手臂微摆（模型没有自带动画时的兜底）。**眨眼已移除** |
| 交互 | 拖拽移动、滚轮缩放、右键菜单、`Ctrl+Alt+P` 鼠标穿透逃生舱、托盘图标 |
| 朝向相机 | 默认 yaw 90° / pitch 0°（正对模型侧面→正面），松手即持久化，重启恢复 |
| 打包 | `YSM-pet.spec` → PyInstaller onedir，含 `_internal` 整包拷贝即用 |

## 快速开始

```bat
rem 方式一：双击（自动在本机找一个装了依赖的 Python）
run_pet.bat

rem 方式二：手动指定解释器
python scripts/run_pet.py                 :: 用上次记住的模型；没记住就弹向导
python scripts/run_pet.py --source 模型.ysm
python scripts/run_pet.py --gaze          :: 启动即开启视线追踪
python scripts/run_pet.py --smoke         :: 不开窗口，只渲 3 帧存图自检
python scripts/run_pet.py --all-parts     :: 冒烟时连特效/立绘一起画（对照用）
```

依赖（源码运行才需要；打包版不需要）：

```
PySide6  numpy  scipy  pyyaml  tqdm  Pillow  pybullet
```

`pybullet` 只用来算视图/投影矩阵（纯矩阵数学），PyPI 无 Windows 轮子，
走 conda-forge：`conda install -y --override-channels -c conda-forge pybullet`。

`run_pet.bat --list` 可以列出本机所有 Python 各自缺什么。

## 桌宠操作

| 操作 | 效果 |
|---|---|
| 左键拖拽 | 移动位置 |
| 滚轮 | 缩放 |
| 右键 | 菜单：置顶 / 穿透 / 轮盘 / 朝向复位 / 设置 / 退出 |
| `Ctrl+Alt+P` | 鼠标穿透逃生舱（穿透开启后窗口收不到鼠标，用热键救回来） |
| 设置面板 | 相机（朝向/缩放/SSAA/光影）、动画（待机/并行层/清晰模式）、视线、窗口 |

## 打包（免安装版）

```bat
python -m PyInstaller YSM-pet.spec --noconfirm
rem 产物：dist\YSM-pet\YSM-pet.exe（+ _internal\），整文件夹拷贝即用
```

spec 已处理三个历史上的坑：`numpy.f2py`/`unittest` 不能进 `excludes`
（否则 scipy 导入链崩）、`configs`/`models`/`data/processed`/`native` 要同时
放 `_internal` 和 exe 同级（模块的 ROOT 解析两种都可能）、`.7z` 需要
`py7zr`。详见 `docs/11`。

原生 DLL 重建：`python scripts/build_native.py`（`raster.cpp` 光栅化 /
`ysmcrypt.c` + `ysmreader.c` 解密读取）。

## 目录结构

```
configs/          相机/骨架/物理/表情配置
data/processed/   skeleton9.json（骨架绑定结果；raw/interim/decrypted 不入库）
models/           ysm_atri_9dof.urdf（9 自由度骨架）
native/           raster.cpp / ysmcrypt.c / ysmreader.c + 预编译 DLL
src/
  pet/            桌宠本体：petapp（窗口/菜单/面板）、fastrender（渲染）、
                   gaze（视线追踪）、modelsource（模型加载）、settings（持久化）
  ysm/            .ysm 解析、几何、动画 clip、加密读取
  phys/ env/ train/ export/ serve/   历史训练线遗产，桌宠运行不经过
scripts/          run_pet.py（入口）、launch_pet.ps1（run_pet.bat 的实现）、
                  build_native.py、step1–5 数据管线、_test_*.py 回归
web/              TypeScript 模型查看器（Vite，step5 导出目标）
tests/            加密原语测试
docs/             01–12 开发日志（10 = 桌宠主日志，11 = 打包，12 = 二进制格式规格）
```

## 已知限制

- `.rar` 只有 libarchive（Windows 自带 `tar.exe`）兜底，加密 / 固实 rar 不保证；
- 「哪个根骨骼子树是人本体」用 `Head` 判定，没有 `Head` 的模型退回「骨骼最多的根」；
- 特效/立绘的显隐是**静态**的（没有实现 molang 状态机）；
- 训练线（站立策略）历史上未达标，与桌宠运行无关，保留在 `src/phys`、`src/env` 等目录。

## 致谢

模型格式与行为对齐上游 **Yes Steve Model**（Minecraft 模组）；动画分层
（`pre_parallel`/`parallel`）、轮盘布局、视线追踪的「转头看你」都复刻自它。
