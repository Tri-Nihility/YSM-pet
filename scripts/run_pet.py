"""启动 YSM 桌宠（透明背景窗口）。

    python scripts/run_pet.py                          # 用上次记住的模型；没记住就弹向导
    python scripts/run_pet.py --source 亚托莉.ysm       # 指定 .ysm
    python scripts/run_pet.py --source 某文件夹          # 指定文件夹
    python scripts/run_pet.py --source 模型包.zip        # 指定 zip/7z/rar
    python scripts/run_pet.py --scale 2 --fps 60        # 放大 + 高帧率
    python scripts/run_pet.py --gaze                    # 启动即开启视线追踪
    python scripts/run_pet.py --smoke                   # 不开窗口，只渲 3 帧存图自检
    python scripts/run_pet.py --reset                   # 忘掉记住的模型，重新走向导
    python scripts/run_pet.py --self-check --out x.log  # 依赖自检，输出同时写日志
"""
import argparse
import os
import sys
import time

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

# ⚠️ 控制台编码兜底：非中文 Windows 的默认编码是 cp1252 / cp437，里面装不下中文，
#    而本脚本满屏中文提示（加载进度、待机策略…），Python 默认 strict 报错 →
#    一句 print 就能让桌宠启动即崩 UnicodeEncodeError。
#    统一改成 UTF-8 + errors=replace：中文区显示正常，别的区最多显示为乱码，
#    但**绝不崩**。（为什么要这样补：PYTHONUTF8 必须在解释器启动前才生效，
#    在脚本里改 os.environ 已经晚了，只能直接 reconfigure 流。）
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                        # noqa: BLE001
        pass


# ⚠️ windowed 打包（console=False）没有控制台，解释器初始化时拿不到
#    stdout/stderr，sys.stdout / sys.stderr 会是 None —— 此后任何 print()
#    都是 RuntimeError: lost sys.stdout，而桌宠 GUI 路径满屏诊断输出
#    （petapp 启动时就打十几行：模型/面数/取景/待机策略…），不补空流
#    等于双击 exe 立刻闪退。补成「黑洞流」；有 --out 时下面的 tee 会
#    再包一层，把输出写进日志文件。
class _NullStream:
    def write(self, s):
        pass

    def flush(self):
        pass


if sys.stdout is None:
    sys.stdout = _NullStream()
if sys.stderr is None:
    sys.stderr = _NullStream()


# 仅源码模式需要手动补 sys.path（冻结态 PyInstaller 已处理好导入）。
# 注意：不能再像以前那样无条件用 dirname×2 推仓库根 —— onefile 冻结时脚本在
# %TEMP%/_MEIxxxx 里，推出来是临时目录，详见 apppath 模块文档。
if not getattr(sys, "frozen", False):
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _ROOT = os.path.dirname(_HERE)                      # scripts/ → 仓库根
    for _p in (_ROOT, os.path.join(_ROOT, "src")):
        if _p not in sys.path:
            sys.path.insert(0, _p)

from apppath import writable_root


def _install_out_tee():
    """`--out 文件`：把 stdout/stderr 的全部输出 tee 一份到日志文件。

    为什么需要：v0.3.2 起 spec 改成 `console=False`（windowed，去掉黑色
    命令行窗口），windowed 程序没有控制台，`print()` 直接丢弃、CI 也抓不到
    输出。CLI 模式（--help/--self-check/--smoke）靠这个参数留痕：
      * CI 冒烟：`--self-check --out check.log` 再 grep 日志；
      * 用户 double-click 带参数排查时也有据可查。
    必须在 argparse 之前调用（--help 的输出也走 stdout）。
    """
    out_path = None
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--out" and i + 1 < len(argv):
            out_path = argv[i + 1]
        elif a.startswith("--out="):
            out_path = a.split("=", 1)[1]
    if not out_path:
        return

    class _Tee:
        def __init__(self, *streams):
            self._streams = [s for s in streams if s is not None]

        def write(self, s):
            for st in self._streams:
                try:
                    st.write(s)
                except Exception:                            # noqa: BLE001
                    pass

        def flush(self):
            for st in self._streams:
                try:
                    st.flush()
                except Exception:                            # noqa: BLE001
                    pass

    try:
        f = open(out_path, "w", encoding="utf-8", errors="replace")
    except OSError:
        return
    sys.stdout = _Tee(sys.stdout, f)
    sys.stderr = _Tee(sys.stderr, f)


_install_out_tee()


def _crash_log_path():
    return os.path.join(writable_root(), "outputs", "crash.log")


def _excepthook(et, ev, tb):
    """windowed 打包下用户眼前一黑什么都看不见 —— 崩溃必须留痕 + 弹窗。"""
    import traceback
    try:
        p = _crash_log_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as fp:
            fp.write("---- %s ----\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            traceback.print_exception(et, ev, tb, file=fp)
    except Exception:                                        # noqa: BLE001
        pass
    if sys.stderr is not None:                # 有控制台时也打一份（源码模式）
        try:
            traceback.print_exception(et, ev, tb)
        except Exception:                                    # noqa: BLE001
            pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0,
            "YSM-pet 启动失败。\n详情已写入：\n%s\n"
            "（看不到该文件的话，请把本窗口截图反馈）" % _crash_log_path(),
            "YSM-pet", 0x10)
    except Exception:                                        # noqa: BLE001
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", "--model", dest="source", default=None,
                    help="模型来源：.ysm 文件 / 文件夹 / .zip/.7z/.rar 压缩包")
    ap.add_argument("--tex", default=None, help="外部贴图 PNG（默认用内嵌）")
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--lighting", action="store_true",
                    help="开启明暗（默认平涂：直接输出贴图原色）")
    ap.add_argument("--gaze", action="store_true",
                    help="启动即开启视线追踪（默认关闭）")
    ap.add_argument("--reset", action="store_true",
                    help="清掉记住的模型来源，重新走向导")
    ap.add_argument("--legacy", action="store_true",
                    help="强制用纯 Python 解析/解密（A5 回退开关，排查问题时用）")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--self-check", action="store_true",
                    help="依赖自检：import 全部关键模块后退出，不启动桌宠"
                         "（CI 冒烟用；漏打包的模块在这里现形）")
    ap.add_argument("--frames", type=int, default=3)
    ap.add_argument("--out", default=None, metavar="日志文件",
                    help="把命令行输出同时写到指定文件。打包版是 windowed"
                         "（无黑色控制台），CI 冒烟和问题排查靠它留痕，"
                         "如 --self-check --out check.log")
    ap.add_argument("--all-parts", dest="all_parts", action="store_true",
                    help="冒烟时连特效/立绘/UI 部件一起画（默认「清晰模式」"
                         "只画人物本体）")
    args = ap.parse_args()

    # 也可用环境变量传：YSM_NO_NATIVE=1（= --legacy），或 YSM_NO_NATIVE=raster
    if args.legacy:
        os.environ["YSM_NO_NATIVE"] = "1"

    from pet import settings as S

    if args.reset:
        S.update(model="")
        print("已清掉记住的模型，下次启动会弹向导。")

    if args.gaze:
        S.update(gaze_enabled=True)

    # ---------------------------------------------------------- 依赖自检
    # v0.3.0 事故：zstandard 没进打包产物，加密模型一加载就
    # ModuleNotFoundError，而 --help 冒烟根本碰不到这条路径。
    # 这个模式把「函数内 import」的关键模块全部真 import 一遍，
    # 漏打包在 CI 阶段就炸，不会拖到用户机器上。
    if args.self_check:
        import importlib
        mods = [
            "numpy", "scipy", "scipy.spatial", "PIL", "PIL.Image",
            "yaml", "tqdm", "py7zr",
            "zstandard",                    # 加密 .ysm 的 zstd 解压（事故主角）
            "pet.settings", "pet.modelsource", "pet.fastrender", "pet.petapp",
            "ysm.ysmcrypt", "ysm.ysmbin", "ysm.nativeload",
        ]
        bad = []
        for m in mods:
            try:
                importlib.import_module(m)
            except Exception as e:         # noqa: BLE001
                bad.append((m, e))
        for m, e in bad:
            print("  ✗ %-18s %s: %s" % (m, type(e).__name__, e))
        if bad:
            print("自检失败：%d 个模块 import 不了（打包漏东西了）" % len(bad))
            return 1
        print("自检通过：%d 个关键模块全部可 import" % len(mods))
        # 软提示：py7zr 解 zstd 压缩的 7z 用的可选后端，缺了不崩
        try:
            importlib.import_module("backports.zstd")
        except ImportError:
            print("  （提示）缺 backports.zstd：zstd 压缩的 7z 模型包将打不开")
        return 0

    # ---------------------------------------------------------- 冒烟自检
    if args.smoke:
        from pet import modelsource as MS
        from pet.fastrender import FastRenderer
        from pet.petapp import IdleAnimator

        src = args.source or S.load().get("model")
        if not src:
            print("冒烟模式需要 --source 指定模型（或先正常启动一次让它记住）")
            return 2
        t0 = time.time()
        d = MS.load_source(src, tex_png=args.tex, legacy=args.legacy,
                          hide_effects=not args.all_parts)
        model, tex, hide = d["model"], d["tex"], d["hide"]
        print("加载: %s  骨骼=%d  贴图=%s (%dx%d)  隐藏骨骼=%d  [%.1fs]"
              % (os.path.basename(str(d["src"])), len(model.order),
                 d["tex_src"], tex.shape[1], tex.shape[0], len(hide),
                 time.time() - t0))

        fr = FastRenderer(model, tex, hide_bones=hide)
        fr.lighting = args.lighting
        an = IdleAnimator(model)
        out = os.path.join(writable_root(), "outputs", "pet")
        os.makedirs(out, exist_ok=True)
        from PIL import Image
        W, H = int(200 * args.scale), int(300 * args.scale)
        for i in range(args.frames):
            t = time.time()
            rgba = fr.render(an.tick(), W, H)
            dt = (time.time() - t) * 1000
            nz = int((rgba[..., 3] > 0).sum())
            print("  帧%d  %6.1f ms  覆盖像素 %d (%.1f%%)"
                  % (i, dt, nz, nz / (W * H) * 100))
            Image.fromarray(rgba).save(os.path.join(out, "smoke_%d.png" % i))
            bg = Image.new("RGB", (W, H), (240, 240, 245))
            bg.paste(Image.fromarray(rgba), (0, 0), Image.fromarray(rgba))
            bg.save(os.path.join(out, "smoke_%d_bg.png" % i))
        print("冒烟图:", out)
        return 0

    # ---------------------------------------------------------- 正常启动
    from pet.petapp import run
    return run(scale=args.scale, fps=args.fps, lighting=args.lighting,
               source=args.source)


if __name__ == "__main__":
    sys.excepthook = _excepthook
    sys.exit(main())
