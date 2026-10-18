"""native DLL 的统一加载与开关（C 加速模块的 Python 侧入口）。

设计原则
--------
1. **单点加载**：所有 DLL 在这里找、在这里 CDLL，别处只管调函数。
2. **失败即降级**：DLL 缺失 / 加载失败 / 架构不符 → 返回 None，调用方走
   Python 实现。绝不让"没有 DLL"变成"程序起不来"。
3. **可强制回退**：环境变量 `YSM_NO_NATIVE=1` 关掉全部 native，
   `YSM_NO_NATIVE=ysmcrypt,raster` 只关指定模块。排查问题时用。

为什么用环境变量而不是配置文件：这种开关只在"怀疑 C 和 Python 不一致"
时才临时用，环境变量能一行带上，不用改文件。
"""
import ctypes
import os
import sys

_CACHE = {}


def disabled_modules():
    """被禁用的 native 模块名集合（空集合 = 全开）。"""
    v = os.environ.get("YSM_NO_NATIVE", "").strip()
    if not v:
        return set()
    if v in ("1", "true", "True", "all"):
        return {"*"}
    return {s.strip() for s in v.split(",") if s.strip()}


def _native_root():
    # src/ysm/nativeload.py -> 仓库根
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(here))


def load(name):
    """加载 native/<name>.dll。返回 CDLL 或 None（不可用时静默降级）。

    name 例：'raster'、'ysmcrypt'
    """
    if name in _CACHE:
        return _CACHE[name]
    dis = disabled_modules()
    if "*" in dis or name in dis:
        _CACHE[name] = None
        return None

    dll = os.path.join(_native_root(), "native", name + ".dll")
    if not os.path.exists(dll):
        _CACHE[name] = None
        return None
    try:
        # Windows 需要把 DLL 目录加进搜索路径（静态链接已解决运行库依赖，
        # 这里只是保险，避免同目录有其它 DLL 时找不到）
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(os.path.dirname(dll))
            except Exception:
                pass
        handle = ctypes.CDLL(dll)
    except OSError as e:
        sys.stderr.write("[native] 加载 %s 失败，回退 Python 实现：%s\n"
                         % (name, e))
        _CACHE[name] = None
        return None
    _CACHE[name] = handle
    return handle


def available(name):
    return load(name) is not None


def status():
    """给菜单/诊断用的状态字符串。"""
    out = []
    for n in ("raster", "ysmcrypt"):
        dll = os.path.join(_native_root(), "native", n + ".dll")
        if not os.path.exists(dll):
            out.append("%s: 未编译" % n)
        elif available(n):
            out.append("%s: 启用" % n)
        else:
            out.append("%s: 已禁用/加载失败" % n)
    return "  ".join(out)
