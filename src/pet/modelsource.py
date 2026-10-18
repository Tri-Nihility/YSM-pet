"""模型来源解析：把用户选的东西变成 (raw, YsmModel, tex, hide)。

支持四种输入（对应需求「支持文件夹，压缩包，.ysm 文件」）
-------------------------------------------------------
    1. `.ysm` 文件       → ysmcrypt.decrypt_ysm_file → ysmbin.deserialize
    2. `.bin` 文件       → 已是解密后的裸二进制，直接 deserialize
    3. 文件夹            → 递归找里面的 `.ysm` / `.bin`
    4. 压缩包            → `.zip` / `.7z` / `.rar`，解包后再按上面规则找

压缩包后端
----------
    .zip  → 标准库 zipfile
    .7z   → py7zr；没装 py7zr（或解不动）时退回 libarchive：`tar.exe -xf`
    .rar  → UnRAR.exe / unar；都没有时退回 libarchive（能读大多数 rar）

    libarchive 后端优先用 Windows 10 1803+ 自带的 `C:\\Windows\\System32\\tar.exe`，
    它对 7z 的读取支持很好、且不需要任何 pip 依赖；Anaconda 环境里的
    `Library\\bin\\bsdtar.exe` 也在候选里。

解密耗时
--------
亚托莉 560KB 的 .ysm 纯 Python 解密要 ~4s、解析 ~0.7s，首次合计约 7s；
所以解密结果缓存到 `data/cache/<源文件 stem>.bin`，第二次加载直接读缓存
（~1s）。源文件 mtime + size 变了才重新解密。

注意：首次**新写入**缓存文件时偶发被 Windows Defender 实时扫描拖到几十秒
（实测出现过一次 250s），第二次起恢复正常。要规避可把这个目录加入
Defender 排除项，但不影响功能。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 支持的模型文件扩展名
MODEL_EXTS = (".ysm", ".bin")
ARCHIVE_EXTS = (".zip", ".7z", ".rar")
# rar 解包器候选路径（专用工具）
UNRAR_CANDIDATES = (
    r"C:\Program Files\WinRAR\UnRAR.exe",
    r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
    "unrar", "unar",
)
# libarchive 后端：Windows 10 1803+ 自带 tar.exe，能解 7z / zip / tar、能读多数 rar
BSDTAR_CANDIDATES = (
    r"C:\Windows\System32\tar.exe",
    "bsdtar", "tar",
)


def _sys_path():
    for p in (ROOT, os.path.join(ROOT, "src")):
        if p not in sys.path:
            sys.path.insert(0, p)


def cache_dir():
    d = os.path.join(ROOT, "data", "cache")
    os.makedirs(d, exist_ok=True)
    return d


# ============================================================ 压缩包
def _find_exe(cands):
    for c in cands:
        if os.path.isabs(c):
            if os.path.isfile(c):
                return c
        else:
            w = shutil.which(c)
            if w:
                return w
    return None


def _unrar_exe():
    return _find_exe(UNRAR_CANDIDATES)


def _bsdtar_exe():
    return _find_exe(BSDTAR_CANDIDATES)


def _extract_zip(path, dest):
    import zipfile
    with zipfile.ZipFile(path) as z:
        z.extractall(dest)
    return "zipfile"


def _run_archive(exe, args, tag):
    """跑一个解包器子进程，失败抛 RuntimeError（带 stderr 前 300 字）。"""
    r = subprocess.run([exe] + args, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("%s 解包失败(%d)：%s"
                           % (tag, r.returncode,
                              (r.stderr or r.stdout).decode("utf-8", "replace")[:300]))
    return os.path.basename(exe).lower()


def _extract_7z(path, dest):
    """优先 py7zr；没装或解不动时退回 libarchive（tar.exe -xf）。"""
    py7zr = None
    try:
        import py7zr
    except ImportError:
        py7zr = None
    if py7zr is not None:
        try:
            with py7zr.SevenZipFile(path, "r") as z:
                z.extractall(dest)
            return "py7zr"
        except Exception as e:                       # 归档损坏 / 编码不支持
            py7zr_err = "%s: %s" % (type(e).__name__, e)
    else:
        py7zr_err = None
    exe = _bsdtar_exe()
    if exe:
        return _run_archive(exe, ["-xf", path, "-C", dest], "libarchive")
    if py7zr_err:
        raise RuntimeError("py7zr 解压失败，且找不到 libarchive：%s" % py7zr_err)
    raise RuntimeError("解压 .7z 需要 py7zr（pip install py7zr），"
                       "或系统自带的 tar.exe（libarchive）")


def _extract_rar(path, dest):
    exe = _unrar_exe()
    if not exe:
        exe = _bsdtar_exe()                          # libarchive 兜底（读多数 rar）
        if not exe:
            raise RuntimeError(
                "解压 .rar 需要 WinRAR 的 UnRAR.exe、PATH 里的 unrar/unar，"
                "或 libarchive（Windows 自带的 tar.exe）；都没找到。")
    base = os.path.basename(exe).lower()
    if base.startswith("unrar"):
        return _run_archive(exe, ["x", "-o+", "-y", path, dest + os.sep], "UnRAR")
    if base.startswith("unar"):
        return _run_archive(exe, ["-o", dest, "-f", path], "unar")
    return _run_archive(exe, ["-xf", path, "-C", dest], "libarchive")


def extract_archive(path, dest):
    """按扩展名解包到 dest，返回所用后端名。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".zip":
        return _extract_zip(path, dest)
    if ext == ".7z":
        return _extract_7z(path, dest)
    if ext == ".rar":
        return _extract_rar(path, dest)
    raise ValueError("不支持的压缩格式：%s" % ext)


# ============================================================ 找模型
def find_model_files(root, deep=True):
    """在 root 下递归找 .ysm / .bin，返回按名字排序的路径列表。"""
    out = []
    if os.path.isfile(root):
        return [root] if root.lower().endswith(MODEL_EXTS) else []
    for dirpath, dirnames, filenames in os.walk(root):
        # 跳过 mac/系统垃圾目录
        dirnames[:] = [d for d in dirnames
                       if d not in ("__MACOSX", ".git", ".svn")]
        for fn in filenames:
            if fn.lower().endswith(MODEL_EXTS):
                out.append(os.path.join(dirpath, fn))
        if not deep:
            break
    return sorted(out)


def _pick_best(paths):
    """多个候选时优先 .ysm（信息最全），其次按文件大小取大的。"""
    if not paths:
        return None
    ysm = [p for p in paths if p.lower().endswith(".ysm")]
    pool = ysm or paths
    return max(pool, key=lambda p: os.path.getsize(p))


# ============================================================ 解密缓存
def _cache_meta_path(src):
    h = hashlib.md5(os.path.abspath(src).encode("utf-8")).hexdigest()[:12]
    stem = os.path.splitext(os.path.basename(src))[0][:40]
    safe = "".join(c if c.isalnum() or c in "-_（）()" else "_" for c in stem)
    return os.path.join(cache_dir(), "%s_%s" % (safe, h))


def _is_ysm_container(data):
    """判断字节流是不是加密封装（YSGP 容器）。

    容器头是 `EF BB BF "YSGP"`（UTF-8 BOM + 魔数）；裸 `.bin` 开头则是
    小端 int32 的 format_version（如 `0f 00 00 00`），不会以 BOM 开头。

    不能用 `crypto != 0` 判断 —— 裸 `.bin` 里随便一个 0 字节后面读 4 字节
    就可能是非零垃圾值。除魔数外再核对尾部 8 字节的 CityHash64WithSeed
    签名（64 位，误判概率可忽略），双保险。
    """
    if len(data) < 80:
        return False
    head = data[:4]
    if head != b"YSGP" and data[:7] != b"\xef\xbb\xbfYSGP":
        return False
    try:
        _sys_path()
        from ysm import ysmcrypt as C
        c = C.parse_container(data)
    except (ValueError, IndexError, UnicodeDecodeError):
        return False
    return bool(c.get("hash_ok"))


def decrypt_cached(src, progress=None, legacy=False):
    """解密 .ysm → 裸二进制 bytes，带磁盘缓存。

    legacy=True 时强制走纯 Python 解密（A5 的回退开关）。
    """
    _sys_path()
    from ysm import ysmcrypt as C

    with open(src, "rb") as fp:
        data = fp.read()

    if data[:8] == b"\x89PNG\r\n\x1a\n":
        raise ValueError("这是 PNG，不是 .ysm 模型")

    # 已经是解密后的裸二进制 → 直接返回
    if not _is_ysm_container(data):
        return data

    st = os.stat(src)
    base = _cache_meta_path(src)
    binp, metap = base + ".bin", base + ".json"
    if os.path.isfile(binp) and os.path.isfile(metap):
        try:
            with open(metap, encoding="utf-8") as fp:
                meta = json.load(fp)
            if (meta.get("mtime") == st.st_mtime
                    and meta.get("size") == st.st_size):
                with open(binp, "rb") as fp:
                    return fp.read()
        except (OSError, ValueError):
            pass

    if progress:
        progress("解密中（首次约需数秒）…")
    t = time.time()
    dec = C.decrypt_ysm_file(data, use_native=not legacy)
    with open(binp, "wb") as fp:
        fp.write(dec)
    with open(metap, "w", encoding="utf-8") as fp:
        json.dump({"mtime": st.st_mtime, "size": st.st_size,
                   "src": os.path.abspath(src),
                   "took": round(time.time() - t, 2)}, fp)
    return dec


# ============================================================ 人物本体
def hero_subtree(model):
    """找出「人物本体」所在的根骨骼子树。

    YSM 模型（尤其 .ysm 现代格式）常常把人本体之外的东西拆成**独立的根骨骼
    子树**：特效刀光/法阵（慧慧1.2 的 `texiao` 5642 面）、平面立绘（`DAZ`
    2016 面、`MZ2` 1926 面）、游戏内 UI（斯卡蒂/铃兰的 `MUI`/`BeiJing`/
    `MZhuanPan`/`MTouzi` = 麦克风面板 / 背景板 / 转盘 / 骰子）、备用部件。
    它们在游戏里由 molang 状态按需显示，桌宠里没有那套状态机 → 全都会画
    出来叠在人物身上（用户原话「多数模型部件都渲染了，人物、特效叠在一起，
    看不清」）。

    判据：根骨骼里「含名为 `Head` 的骨骼」的那个子树；没有 `Head` 就退回
    「骨骼最多的根子树」。返回 `(根骨骼名, 本体骨骼名集合)`。
    """
    order = model.order
    names = [b["name"] for b in order]
    if not names:
        return None, set()
    index = {}
    for i, n in enumerate(names):
        index.setdefault(n, i)
    parent = [-1] * len(names)
    kids = {}
    for i, b in enumerate(order):
        p = b.get("parent") or ""
        if p and p in index and index[p] != i:
            parent[i] = index[p]
            kids.setdefault(index[p], []).append(i)
    sub = {}
    for r in [i for i in range(len(names)) if parent[i] < 0]:
        stack, acc = [r], []
        while stack:
            j = stack.pop()
            acc.append(j)
            stack.extend(kids.get(j, ()))
        sub[r] = acc
    if not sub:
        return None, set()
    hero = None
    for r in sorted(sub):
        if any(names[j] == "Head" for j in sub[r]):
            hero = r
            break
    if hero is None:
        hero = max(sub, key=lambda r: len(sub[r]))
    return names[hero], {names[j] for j in sub[hero]}


def hero_hide_bones(model):
    """「清晰模式」要排除的骨骼名 = 人物本体之外的全部骨骼。

    模特本体（`hero_subtree`）找不到时返回空集合 —— 宁可不裁剪，也不能把
    整个模型都藏掉。
    """
    _, keep = hero_subtree(model)
    if not keep:
        return set()
    return {b["name"] for b in model.order if b["name"] not in keep}


# ============================================================ 主入口
def load_source(source, tex_png=None, deep=True, progress=None, legacy=False,
                hide_effects=True):
    """从任意来源加载模型。

    source: `.ysm` / `.bin` / 文件夹 / `.zip` / `.7z` / `.rar` 的路径
    legacy: True 时强制纯 Python 解密（A5 回退开关）
    hide_effects: True（默认）= 桌宠「清晰模式」，只画人物本体，把特效/
        立绘/游戏内 UI/备用部件并进 hide（见 `hero_hide_bones`）。
        要做「全部部件」的对照渲染（如 probe）时传 False。
    返回 dict 同 loader.load_all()：raw/model/tex/tex_src/hide/cfg/src_kind
    """
    _sys_path()
    from ysm import ysmbin as B
    from ysm import ysmmodel as YM
    from ysm import animation as _anim
    from pet import loader as L

    if not os.path.exists(source):
        raise FileNotFoundError("找不到：%s" % source)

    # 换模型了：旧的预解析缓存（按 id 索引且持有强引用）必须丢掉，
    # 否则每换一次模型就多留一份旧通道，内存只增不减。
    _anim.clear_prep_cache()

    tmp = None
    kind = "file"
    target = None

    if os.path.isdir(source):
        kind = "folder"
        target = _pick_best(find_model_files(source, deep))
        if target is None:
            raise ValueError("文件夹里没有 .ysm / .bin：%s" % source)
    elif os.path.splitext(source)[1].lower() in ARCHIVE_EXTS:
        kind = os.path.splitext(source)[1].lower().lstrip(".")
        tmp = tempfile.mkdtemp(prefix="ysm_")
        if progress:
            progress("解压 %s…" % os.path.basename(source))
        backend = extract_archive(source, tmp)
        if progress:
            progress("解压完成（%s）" % backend)
        target = _pick_best(find_model_files(tmp, deep))
        if target is None:
            shutil.rmtree(tmp, ignore_errors=True)
            raise ValueError("压缩包里没有 .ysm / .bin：%s" % source)
    else:
        target = source
        if not source.lower().endswith(MODEL_EXTS):
            raise ValueError("不认识的模型文件：%s" % source)

    try:
        if target.lower().endswith(".ysm"):
            data = decrypt_cached(target, progress=progress, legacy=legacy)
            kind += "+ysm"
        else:
            with open(target, "rb") as fp:
                data = fp.read()
            # 兜底：有时用户把 .ysm 改名成 .bin，用签名校验识别
            if _is_ysm_container(data):
                _sys_path()
                from ysm import ysmcrypt as C
                if progress:
                    progress("解密中（首次约需数秒）…")
                data = C.decrypt_ysm_file(data, use_native=not legacy)

        if progress:
            progress("解析模型…")
        raw = B.deserialize(data)
        model = YM.YsmModel(raw, "main_model")

        tex, tex_src = L.load_texture(raw, tex_png)
        hide, cfg = L.load_hide_bones()
        if hide_effects:
            extra = hero_hide_bones(model)
            if extra:
                hide = sorted(set(hide) | extra)
        return {"raw": raw, "model": model, "tex": tex, "tex_src": tex_src,
                "hide": hide, "cfg": cfg, "src": target, "src_kind": kind}
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def describe(path):
    """给 GUI 用：一句话说明这个来源是什么、能不能加载。"""
    if not os.path.exists(path):
        return "不存在"
    if os.path.isdir(path):
        n = len(find_model_files(path))
        return "文件夹（%d 个模型）" % n
    ext = os.path.splitext(path)[1].lower()
    size = os.path.getsize(path)
    if ext in ARCHIVE_EXTS:
        return "%s 压缩包（%.1f MB）" % (ext.lstrip(".").upper(), size / 1e6)
    if ext in MODEL_EXTS:
        return "%s 模型（%.1f MB）" % (ext.lstrip(".").upper(), size / 1e6)
    return "不支持"
