"""C++ 全管线 vs Python 参考实现 一致性验证。

用法：
    python scripts/verify_cpp.py                 # 全档位矩阵
    python scripts/verify_cpp.py --probe         # 附带法线/光照探针
"""
import argparse
import ctypes
import os
import sys

import numpy as np

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from pet import loader as L                                  # noqa: E402
from pet.fastrender import FastRenderer, _LIGHT, _AMB, _HEMI_W, _DIFF_W  # noqa: E402
from pet.petapp import IdleAnimator                          # noqa: E402


def cmp_frame(a, b, tag):
    """比较两帧 RGBA，返回 (max差, mean差, 不同像素数, 覆盖像素数)。"""
    ca = a[..., 3] > 0
    cb = b[..., 3] > 0
    cov = int((ca | cb).sum())
    if cov == 0:
        print("  %-26s 两帧全空" % tag)
        return 0, 0.0, 0, 0
    # 只在任一被覆盖的像素上比较（背景全 0 不该算差）
    m = ca | cb
    da = a[m].astype(np.int16)
    db = b[m].astype(np.int16)
    diff = np.abs(da - db)
    mx = int(diff.max())
    mean = float(diff.mean())
    nz = int((diff.max(axis=1) > 0).sum())
    print("  %-26s 覆盖=%5d  max差=%3d  mean差=%.4f  不同像素=%d"
          % (tag, cov, mx, mean, nz))
    return mx, mean, nz, cov


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="亚托莉目标模型.bin")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--rest", action="store_true", help="用 rest 姿态（不叠加动画）")
    ap.add_argument("--frames", type=int, default=3)
    args = ap.parse_args()

    d = L.load_all(args.model, None)
    model, tex, hide = d["model"], d["tex"], d["hide"]
    print("模型 %s  骨骼=%d  面数待定" % (args.model, len(model.order)))

    fr = FastRenderer(model, tex, hide_bones=hide)
    an = IdleAnimator(model)

    # ---------- 探针：法线 / 光照
    if args.probe:
        dll = fr.dll
        dll.ysm_probe2.restype = None
        dll.ysm_probe2.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.POINTER(ctypes.c_double),
            ctypes.c_double, ctypes.c_double, ctypes.c_double,
            ctypes.POINTER(ctypes.c_double)]
        fr._ensure_ctx()
        anim = None if args.rest else an.tick()
        Mcur, vis = fr.pose_matrices(anim)
        v8 = np.ascontiguousarray(vis, np.uint8)
        Lv = np.ascontiguousarray(_LIGHT, np.float64)
        o8 = np.zeros(8, np.float64)
        dll.ysm_probe2(ctypes.c_void_p(fr._ctx),
                       Mcur.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
                       v8.ctypes.data_as(ctypes.POINTER(ctypes.c_ubyte)),
                       Lv.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
                       ctypes.c_double(_AMB), ctypes.c_double(_HEMI_W),
                       ctypes.c_double(_DIFF_W),
                       o8.ctypes.data_as(ctypes.POINTER(ctypes.c_double)))
        print("\n[C++ 探针] 保留面=%d  pnz=[%.3f,%.3f]  d(未clamp)=[%.3f,%.3f]"
              % (o8[0], o8[1], o8[2], o8[3], o8[4]))
        print("           shade=[%.4f,%.4f]   dot(Np,V0)最小=%.4f"
              % (o8[5], o8[6], o8[7]))

        # Python 侧同样的统计
        keep = vis[fr.fb]
        Mf = Mcur[fr.fb][keep]
        Nw = np.einsum("fij,fj->fi", Mf[:, :3, :3], fr.fn[keep])
        from phys.bind import P_MAT
        Np = Nw @ P_MAT.T
        d_raw = Np @ _LIGHT
        hemi = 0.5 + 0.5 * Np[:, 2]
        sh_py = _AMB + _HEMI_W * hemi + _DIFF_W * np.clip(d_raw, 0, None)
        print("[Python  ] 面数=%d  pnz=[%.3f,%.3f]  d(未clamp)=[%.3f,%.3f]"
              % (len(Np), Np[:, 2].min(), Np[:, 2].max(),
                 d_raw.min(), d_raw.max()))
        print("           shade=[%.4f,%.4f]"
              % (sh_py.min(), sh_py.max()))

    # ---------- 一致性矩阵
    mat = [
        (1, False), (1, True),
        (2, False), (2, True),
        (3, False), (3, True),
    ]
    print("\n一致性矩阵（C++ 全管线 vs Python 参考）")
    worst = 0
    for ss, lit in mat:
        fr.ssaa = ss
        fr.lighting = lit
        for i in range(args.frames):
            anim = None if args.rest else an.tick()
            fr.use_cpp_pipeline = True
            a = fr.render(anim, 200, 300).copy()
            fr.use_cpp_pipeline = False
            b = fr.render(anim, 200, 300).copy()
            mx, _, _, _ = cmp_frame(a, b, "ssaa=%d lighting=%s f%d"
                                    % (ss, lit, i))
            worst = max(worst, mx)
    fr.use_cpp_pipeline = True
    print("\n最大逐像素差 = %d  →  %s" % (worst, "✅ 一致" if worst <= 1 else "❌ 有偏差"))
    return 0 if worst <= 1 else 1


if __name__ == "__main__":
    sys.exit(main())
