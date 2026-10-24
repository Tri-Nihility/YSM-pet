# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

# ⚠️ 这里列的目录必须**真的存在且非空**：git 不跟踪空目录，若某天被清空，
#    CI 检出后它就不复存在，PyInstaller 会直接报
#   `ERROR: Unable to find ... when adding binary and data files.` 并失败。
#   （v0.3.0 踩过：`models/` 里的 URDF 属于训练线，删掉后目录变空，就挂在这里。）
#   data/processed 只是个 .gitkeep 占位（代码零引用），不进产物 ——
#   v0.3.2 起是 onefile 单文件：整个 Release 就是一个 YSM-pet.exe。
datas = [('configs', 'configs')]
# 原生加速 DLL：冻结后落在 sys._MEIPASS/native/（各模块经
# src/apppath.py 的 resource_root() 定位，源码 / onedir / onefile 三态统一）。
datas += [('native/raster.dll', 'native'),
          ('native/ysmcrypt.dll', 'native'),
          ('native/ysmreader.dll', 'native')]
binaries = []
# numpy.f2py / numpy.testing / unittest 必须显式带上（曾经被 excludes 掉，导致
# dist 里一启动就 ModuleNotFoundError: No module named 'numpy.f2py'）：
#   scipy/_external/array_api_compat/_internal.py:clone_module 在导入时遍历
#   dir(numpy) 并对每个名字 getattr，而 numpy.__getattr__ 会真的去
#   import numpy.f2py / numpy.testing，numpy.testing 又 import unittest。
#   任一缺失 → scipy.spatial → scipy.sparse → scipy._lib._array_api 整条链崩。
hiddenimports = ['pet.petapp', 'pet.modelsource', 'pet.settings',
                 'apppath',
                 'numpy.f2py', 'numpy.testing', 'unittest',
                 # .7z 解包（pet/modelsource.py 里是函数内 import）：
                 # 打包环境**必须装了 py7zr**，否则它只是静默降级到
                 # libarchive（Windows 自带 tar.exe），不够稳。
                 # 注意：py7zr≥1.1 的 zstd 支持走 `compression.zstd`(3.14+)
                 # 或 `backports.zstd`，**不是** pyzstd（v0.3.0 的 spec 列过
                 # pyzstd，那个包根本不在依赖链里，纯属死条目）。
                 'py7zr', 'multivolumefile', 'texttable']
# 加密格式 .ysm 的 zstd 解压（ysmcrypt.py 函数内 import zstandard）：
# v0.3.0 漏了它 → 用户一加载加密模型就 ModuleNotFoundError。
# collect_all 把包目录 + backend_c/_cffi 两个 C 扩展一起收进来。
tmp_ret = collect_all('zstandard')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# py7zr 解 zstd 压缩的 7z 用（可选；没装只是不支持该压缩法，不崩）。
# requirements.txt 已列 backports.zstd，正常情况下能收到。
try:
    tmp_ret = collect_all('backports.zstd')
    datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
except Exception:
    pass
# ⚠️ 打包依赖必须与 requirements.txt 对齐。**不要**把 torch / pybullet 拉进来：
#    * torch 是训练线遗产，桌宠一行都不用，带上会让产物从 ~200MB 膨胀到 ~2GB；
#    * pybullet 只为两个矩阵函数服务，已被 `src/pet/camera.py`（纯 numpy）取代，
#      而且它在 PyPI 上没有 Windows 轮子 —— 放进 spec 会导致 CI 直接装不上依赖。
datas += collect_data_files('scipy')
binaries += collect_dynamic_libs('scipy')
binaries += collect_dynamic_libs('numpy')
hiddenimports += collect_submodules('pet')
hiddenimports += collect_submodules('phys')
hiddenimports += collect_submodules('ysm')
tmp_ret = collect_all('yaml')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('tqdm')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('PIL')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['scripts/run_pet.py'],
    pathex=['src'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 不要在这里 exclude unittest / numpy.f2py：理由见文件上方 hiddenimports 的注释。
    excludes=['tkinter', 'IPython', 'jupyter', 'pytest', 'scipy.test', 'numpy.test',
              'torch', 'pybullet'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# ---------------------------------------------------------------- 打包形态
# v0.3.2 起：onefile 单文件 + windowed（无黑色命令行窗口）。
#
# console=False：桌宠是纯 GUI 程序，以前 console=True 是为了让
#   --help/--self-check/--smoke 能看到输出；现在这些 CLI 模式的输出统一走
#   run_pet.py 的 `--out 日志文件` tee（windowed 没有控制台，print 会丢），
#   CI 冒烟同步改成读日志。崩溃也留痕：run_pet.py 装了 excepthook，
#   写 outputs/crash.log 并弹 MessageBox。
#
# onefile：a.binaries / a.datas 直接进 exe（不再走 COLLECT 的 onedir），
#   启动时解压到 %TEMP%/_MEIxxxx 再运行。代价是每次启动多几秒；
#   UPX 在这里关掉 —— onefile 的 CArchive 被 UPX 压过之后启动还要多一道
#   解压，且 UPX 壳是杀软误报的经典特征，单文件分发得不偿失。
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    exclude_binaries=False,
    name='YSM-pet',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
# 没有 COLLECT —— onefile 产物就是 dist/YSM-pet.exe 一个文件。
# 也没有「资源补到 exe 同级」的收尾复制了：各模块经 src/apppath.py 的
# resource_root() 统一从 sys._MEIPASS 找 configs/ 和 native/*.dll，
# onedir（_internal）和 onefile（临时解压目录）两种冻结态都覆盖。
#
# ⚠️ 下面的 print 必须是 ASCII：spec 是在 PyInstaller 进程里 exec 的，输出会打到
#    当前控制台编码。GitHub Actions 的 windows runner 默认是 cp1252，
#    中文一 print 就是 UnicodeEncodeError: 'charmap' codec can't encode
#    —— v0.3.0 第一次 CI 就栽在这里（包其实已经打好了，是最后的提示信息炸的）。
_msg = '[spec] onefile windowed build -> %s' % os.path.join(DISTPATH, 'YSM-pet.exe')
try:
    print(_msg)
except UnicodeEncodeError:                       # 兜底：绝不因打印失败整个构建
    pass
