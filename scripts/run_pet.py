"""启动 YSM 桌宠（透明背景窗口）。

    python scripts/run_pet.py                          # 用上次记住的模型；没记住就弹向导
    python scripts/run_pet.py --source 亚托莉.ysm       # 指定 .ysm
    python scripts/run_pet.py --source 某文件夹          # 指定文件夹
    python scripts/run_pet.py --source 模型包.zip        # 指定 zip/7z/rar
    python scripts/run_pet.py --scale 2 --fps 60        # 放大 + 高帧率
    python scripts/run_pet.py --gaze                    # 启动即开启视线追踪
    python scripts/run_pet.py --smoke                   # 不开窗口，只渲 3 帧存图自检
    python scripts/run_pet.py --reset                   # 忘掉记住的模型，重新走向导
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))


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
    ap.add_argument("--frames", type=int, default=3)
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
        out = os.path.join(ROOT, "outputs", "pet")
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
    sys.exit(main())
