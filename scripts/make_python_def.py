"""从 python313.dll 的 PE 导出表生成 MinGW 可用的导入库。

为什么需要这个
--------------
本机 Python 是 MSVC 编译的，只提供 MSVC 短格式导入库 `python313.lib`，
MinGW 的 ld 读不了它（链接时报一片 `undefined reference to __imp_Py*`）。
Dev-C++ 5.11 自带的 MinGW64 **没有 gendef.exe**，所以这里直接用 Python
解析 PE 导出目录，产出 `.def` 再交给 `dlltool.exe` 生成 `libpython313.a`。

用法
----
    python scripts/make_python_def.py            # 生成 native/python313.def
    python scripts/make_python_def.py --mklib    # 顺带调用 dlltool 生成 .a
"""
import argparse
import os
import struct
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_dll():
    import sysconfig
    base = sysconfig.get_config_var("BINDIR") or os.path.dirname(sys.executable)
    for cand in (os.path.join(base, "python%d%d.dll" % sys.version_info[:2]),):
        if os.path.exists(cand):
            return cand
    # venv 没有 dll，回退到 base_prefix
    base = sysconfig.get_config_var("base_prefix") or sys.base_prefix
    cand = os.path.join(base, "python%d%d.dll" % sys.version_info[:2])
    return cand if os.path.exists(cand) else None


def rva_to_off(sections, rva):
    for va, vsz, praw, rawsz in sections:
        if va <= rva < va + max(vsz, rawsz):
            return praw + (rva - va)
    return None


def parse_exports(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] != b"MZ":
        raise SystemExit("不是 PE 文件: %s" % path)
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise SystemExit("PE 签名不对")

    nsec, = struct.unpack_from("<H", data, pe + 6)
    opt_size, = struct.unpack_from("<H", data, pe + 20)
    opt = pe + 24
    magic, = struct.unpack_from("<H", data, opt)
    dd_off = opt + (112 if magic == 0x20B else 96)
    exp_rva, exp_sz = struct.unpack_from("<II", data, dd_off)

    secs = []
    s = opt + opt_size
    for _ in range(nsec):
        name = data[s:s + 8].rstrip(b"\0")
        vsz, va, rawsz, praw = struct.unpack_from("<IIII", data, s + 8)
        secs.append((va, vsz, praw, rawsz))
        s += 40

    if not exp_rva:
        raise SystemExit("没有导出表")
    e = rva_to_off(secs, exp_rva)
    (_char, _ts, _mj, _mn, _name, base,
     nfun, nnam, afun, anam, aord) = struct.unpack_from("<IIHHIIIIIII", data, e)

    off_names = rva_to_off(secs, anam)
    names = []
    for i in range(nnam):
        r, = struct.unpack_from("<I", data, off_names + i * 4)
        o = rva_to_off(secs, r)
        end = data.index(b"\0", o)
        names.append(data[o:end].decode("ascii", "replace"))
    return sorted(set(names)), nfun


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mklib", action="store_true", help="调用 dlltool 生成 .a")
    ap.add_argument("--dlltool", default=r"C:\Program Files (x86)\Dev-Cpp\MinGW64\bin\dlltool.exe")
    args = ap.parse_args()

    dll = find_dll()
    if not dll:
        raise SystemExit("找不到 python3xx.dll")
    names, nfun = parse_exports(dll)
    print("DLL : %s" % dll)
    print("导出: %d 个名字 / %d 个序号" % (len(names), nfun))

    nat = os.path.join(ROOT, "native")
    os.makedirs(nat, exist_ok=True)
    defp = os.path.join(nat, "python313.def")
    with open(defp, "w", newline="\n") as f:
        f.write("LIBRARY %s\nEXPORTS\n" % os.path.basename(dll))
        for n in names:
            f.write("  %s\n" % n)
    print("写出: %s" % defp)

    if args.mklib:
        if not os.path.exists(args.dlltool):
            raise SystemExit("找不到 dlltool: %s" % args.dlltool)
        libp = os.path.join(nat, "libpython313.a")
        cmd = [args.dlltool, "--input-def", defp, "--dllname",
               os.path.basename(dll), "--output-lib", libp]
        print("执行:", " ".join(cmd))
        subprocess.run(cmd, check=True)
        print("生成: %s (%d B)" % (libp, os.path.getsize(libp)))


if __name__ == "__main__":
    main()
