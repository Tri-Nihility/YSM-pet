# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = [('models', 'models'), ('configs', 'configs'), ('data/processed', 'data/processed')]
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
                 'py7zr', 'multivolumefile', 'texttable', 'pyzstd']
datas += collect_data_files('torch')
datas += collect_data_files('scipy')
binaries += collect_dynamic_libs('torch')
binaries += collect_dynamic_libs('scipy')
binaries += collect_dynamic_libs('numpy')
hiddenimports += collect_submodules('pet')
hiddenimports += collect_submodules('phys')
tmp_ret = collect_all('pybullet')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
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
    excludes=['tkinter', 'IPython', 'jupyter', 'pytest', 'torch.test', 'torch.utils.tensorboard', 'scipy.test', 'numpy.test'],
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
for _rel in ('configs', 'models', _os.path.join('data', 'processed')):
    _src = _os.path.join(SPECPATH, _rel)
    if _os.path.isdir(_src):
        _shutil.copytree(_src, _os.path.join(_APP, _rel), dirs_exist_ok=True)
for _dll in ('raster.dll', 'ysmcrypt.dll', 'ysmreader.dll'):
    _src = _os.path.join(SPECPATH, 'native', _dll)
    if _os.path.isfile(_src):
        _dst = _os.path.join(_APP, 'native', _dll)
        _os.makedirs(_os.path.dirname(_dst), exist_ok=True)
        _shutil.copy2(_src, _dst)
print('[spec] 已把 configs/ models/ data/processed/ native/*.dll 复制到 exe 同级目录: %s' % _APP)
