# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

# ⚠️ 这里列的目录必须**真的存在且非空**：git 不跟踪空目录，若某天被清空，
#    CI 检出后它就不复存在，PyInstaller 会直接报
#   `ERROR: Unable to find ... when adding binary and data files.` 并失败。
#   （v0.3.0 踩过：`models/` 里的 URDF 属于训练线，删掉后目录变空，就挂在这里。）
#   data/processed 只是个 .gitkeep 占位（代码零引用），不进产物——
#   发布包只包含程序本体：exe + _internal + configs + native。
datas = [('configs', 'configs')]
# 原生加速 DLL：进 _internal/native（冻结后 ROOT 的两种解析结果都覆盖，
# 见文件末尾的「补齐 exe 同级资源」步骤）。
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

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='YSM-pet',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='YSM-pet',
)

# ---------------------------------------------------------------- 资源补齐
# 冻结后各模块的 ROOT 是「os.path.dirname 三连」（src/<pkg>/<mod>.py → 仓库根）。
# 在 PyInstaller 里 __file__ = <_MEIPASS>/<pkg>/<mod>.pyc，所以 ROOT 会解析到
# **exe 所在目录**（dist/YSM-pet/），而不是 _internal/；而 COLLECT 只会把
# datas 放进 _internal/。实测只放 _internal 时启动即
#   FileNotFoundError: '...\dist\YSM-pet\configs\joints9.yaml'
# 所以这里把同一批资源再放一份到 exe 同级目录：两处都有，无论 ROOT 怎么解析
# 都能找到（native/*.dll 同理，native 缺失只会静默降级成 Python 渲染）。
import os as _os
import shutil as _shutil

_APP = _os.path.join(DISTPATH, 'YSM-pet')
# 只补程序运行必需的资源：configs（joints9.yaml）+ native 三个 DLL。
# 不放 data/（空占位）、models/、docs/ 等任何非程序内容。
for _rel in ('configs',):
    _src = _os.path.join(SPECPATH, _rel)
    if _os.path.isdir(_src):
        _shutil.copytree(_src, _os.path.join(_APP, _rel), dirs_exist_ok=True)
for _dll in ('raster.dll', 'ysmcrypt.dll', 'ysmreader.dll'):
    _src = _os.path.join(SPECPATH, 'native', _dll)
    if _os.path.isfile(_src):
        _dst = _os.path.join(_APP, 'native', _dll)
        _os.makedirs(_os.path.dirname(_dst), exist_ok=True)
        _shutil.copy2(_src, _dst)
# ⚠️ 这一行必须是 ASCII：spec 是在 PyInstaller 进程里 exec 的，输出会打到
#    当前控制台编码。GitHub Actions 的 windows runner 默认是 cp1252，
#    中文一 print 就是 UnicodeEncodeError: 'charmap' codec can't encode
#    —— v0.3.0 第一次 CI 就栽在这里（包其实已经打好了，是最后的提示信息炸的）。
_msg = '[spec] copied configs/ native/*.dll next to the exe: %s' % _APP
try:
    print(_msg)
except UnicodeEncodeError:                       # 兜底：绝不因打印失败整个构建
    pass
