"""编译 native/raster.cpp -> native/raster.dll（供 ctypes 调用）。

    python scripts/build_native.py            # 自动找 g++
    python scripts/build_native.py --force    # 强制重编

本机事实：系统没有 VS / clang / mingw 在 PATH 里，
但 Dev-C++ 5.11 自带 MinGW64（TDM-GCC 4.9.2）。找不到时脚本会列出常见位置逐一试。
产物必须**静态链接 libgcc/libstdc++**，否则 Python 加载 DLL 时会找不到这两个运行库。
"""
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NATIVE = os.path.join(ROOT, "native")
SRC = os.path.join(NATIVE, "raster.cpp")
DLL = os.path.join(NATIVE, "raster.dll")

# 所有 native 产物：(源文件, 产物, 必须存在的导出符号)
# 新增模块时在这里加一行即可，`--only` 可按产物名单独编译。
TARGETS = [
    ("raster.cpp", "raster.dll", "raster_faces"),
    ("ysmcrypt.c", "ysmcrypt.dll", "ysmc_decrypt"),
    ("ysmreader.c", "ysmreader.dll", "ysmr_varint"),
]

# 候选编译器（按优先级）。Dev-C++ 5.11 的默认位置排最前。
CANDIDATES = [
    r"C:\Program Files (x86)\Dev-Cpp\MinGW64\bin\g++.exe",
    r"C:\Program Files\Dev-Cpp\MinGW64\bin\g++.exe",
    r"C:\Program Files (x86)\Dev-Cpp\MinGW32\bin\g++.exe",
    r"C:\mingw64\bin\g++.exe",
    r"C:\msys64\mingw64\bin\g++.exe",
    r"C:\TDM-GCC-64\bin\g++.exe",
]


def find_gxx():
    for p in CANDIDATES:
        if os.path.exists(p):
            return p
    which = shutil.which("g++") or shutil.which("g++.exe")
    if which:
        return which
    return None


def find_gcc(gxx=None):
    """找同名目录下的 gcc（编译 .c 用，保证纯 C ABI 不被 mangle）。"""
    if gxx:
        c = os.path.join(os.path.dirname(gxx), "gcc.exe")
        if os.path.exists(c):
            return c
    for p in CANDIDATES:
        c = os.path.join(os.path.dirname(p), "gcc.exe")
        if os.path.exists(c):
            return c
    return shutil.which("gcc") or shutil.which("gcc.exe")


def build_one(gxx, src_name, dll_name, symbol, force=False):
    src = os.path.join(NATIVE, src_name)
    dll = os.path.join(NATIVE, dll_name)
    if not os.path.exists(src):
        print("跳过（源文件不存在）:", src)
        return True
    if os.path.exists(dll) and not force:
        if os.path.getmtime(dll) >= os.path.getmtime(src):
            print("已是最新:", dll)
            return True
    # ⚠️ .c 文件必须用 gcc 编译：用 g++ 会把 C 当 C++，导出符号会被
    #    name mangling（ysmc_decrypt → _Z11ysmc_decryptPKmyyPhm），
    #    ctypes 就找不到。gcc 编 C 是纯 C ABI，符号原名导出。
    cc = gxx
    if src_name.endswith(".c"):
        gcc = os.path.join(os.path.dirname(gxx), "gcc.exe")
        if os.path.exists(gcc):
            cc = gcc
    extra = ["-std=c11"] if src_name.endswith(".c") else []
    # ⚠️ 不要加 -ffast-math：它允许编译器忽略 IEEE 舍入语义，把
    #    `(float)((double)tf / 20.0)` 收缩回单精度除法，导致 C 版与
    #    Python 版在 float32 边界上出现系统性偏差（实测 ysmreader 有
    #    2144/18201 处 t 值差 ~1e-8）。A4 要求「逐字段与 Python 对照」，
    #    可复现性优先于那点浮点吞吐。
    cmd = [cc] + extra + [
           "-O3", "-funroll-loops", "-shared",
           "-static-libgcc", "-static-libstdc++", "-static",
           "-o", dll, src]
    print("编译:", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout or "")
        sys.stderr.write(r.stderr or "")
        return False
    print("产物:", dll, os.path.getsize(dll), "B")
    # 冒烟：加载并确认导出符号存在
    import ctypes
    d = ctypes.CDLL(dll)
    if not hasattr(d, symbol):
        sys.stderr.write("DLL 里没有 %s 符号\n" % symbol)
        return False
    print("冒烟通过：%s 找到 %s" % (dll_name, symbol))
    return True


def build(gxx, force=False, only=""):
    ok = True
    for src_name, dll_name, symbol in TARGETS:
        if only and only not in (src_name, dll_name):
            continue
        print("---", dll_name, "---")
        if not build_one(gxx, src_name, dll_name, symbol, force):
            ok = False
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gxx", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", default="",
                    help="只编译指定产物，如 raster.dll 或 ysmcrypt.dll")
    args = ap.parse_args()

    gxx = args.gxx or find_gxx()
    if not gxx:
        sys.exit("找不到 g++。已尝试：\n  " + "\n  ".join(CANDIDATES)
                 + "\n可用 --gxx 显式指定。")
    print("编译器:", gxx)
    try:
        v = subprocess.run([gxx, "--version"], capture_output=True, text=True)
        print("版本:", (v.stdout or v.stderr).splitlines()[0])
    except Exception as e:
        print("版本查询失败:", e)
    ok = build(gxx, args.force, args.only)
    if not ok:
        sys.exit("编译失败")
    print("全部产物编译完成")


if __name__ == "__main__":
    main()
