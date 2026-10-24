"""资源目录 / 可写目录定位：源码、onedir 冻结、onefile 冻结三态统一。

背景：各模块原来用 `dirname×N(abspath(__file__))` 硬推仓库根，三种环境下
推导结果并不一致——
    源码模式:     src/pet/x.py              → ×3 = 仓库根              ✓
    onedir 冻结:  <exe>/_internal/pet/x.pyc → ×3 = exe 目录            ✓
                  （spec 里为此要把 configs/native 复制一份到 exe 同级）
    onefile 冻结: <%TEMP%/_MEIxxxx>/pet/x.pyc → ×3 = %TEMP%            ✗
                  → configs/native 找不到（native 静默降级成 Python 渲染、
                    joints9.yaml 直接 FileNotFoundError），且 data/cache
                    会落到 %TEMP%/data/cache，PyInstaller 只清理 _MEIPASS，
                    那堆缓存永远留在临时目录里。

统一规则（v0.3.2 起打包改为 onefile 单文件）：
    resource_root() —— 只读资源（configs/、native/*.dll）：冻结态一律
        sys._MEIPASS（datas 的落脚点：onedir 是 _internal/，onefile 是
        临时解压目录，两者都在）。
    writable_root() —— 运行时可写（data/cache、outputs/）：冻结态优先
        exe 同级目录（缓存和输出跟着程序走，便携语义不变）；exe 目录不可写
        （只读媒体、Program Files）时退回 _MEIPASS。
    source_sys_path() —— 仅源码模式需要手动注入的 sys.path（冻结态
        PyInstaller 已处理好导入，乱注入反而可能掩盖打包问题）。
"""
import os
import sys
import tempfile

FROZEN = bool(getattr(sys, "frozen", False))


def _source_root():
    # 本文件位于 src/apppath.py → 上两层 = 仓库根（与 src/pet/x.py 的 ×3 等价）
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_root():
    """只读资源根目录（configs/、native/）。"""
    if FROZEN:
        return sys._MEIPASS
    return _source_root()


def _writable(d):
    try:
        fd, p = tempfile.mkstemp(dir=d)
        os.close(fd)
        os.unlink(p)
        return True
    except OSError:
        return False


def writable_root():
    """运行时可写根目录（data/cache、outputs 等）。"""
    if FROZEN:
        try:
            exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        except Exception:                                # noqa: BLE001
            exe_dir = ""
        if exe_dir and _writable(exe_dir):
            return exe_dir
        return sys._MEIPASS
    return _source_root()


def source_sys_path():
    """仅源码模式需要的 sys.path 注入项（冻结态返回空列表）。"""
    if FROZEN:
        return []
    return [_source_root(), os.path.join(_source_root(), "src")]
