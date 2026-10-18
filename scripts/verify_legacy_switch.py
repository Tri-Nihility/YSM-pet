"""A5 验收：--legacy 开关可用，且两条路径产出的模型逐字段一致。

验的是"回退是真的能回退，而且回退后结果一样"：
    1. C 路径与 legacy 路径解出的裸二进制逐字节相同
    2. 解析出的骨骼数 / 立方体数 / 动画组数相同
    3. 关掉 DLL（重命名）时，自动降级不报错
    4. 给出 C vs Python 的耗时对比

用法：python scripts/verify_legacy_switch.py [model.ysm]
"""
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))


def find_model():
    for base in ("models", "临时文件（记得整理掉"):
        d = os.path.join(ROOT, base)
        if not os.path.isdir(d):
            continue
        for dp, _dn, fn in os.walk(d):
            for f in fn:
                if f.lower().endswith(".ysm"):
                    return os.path.join(dp, f)
    return None


def main():
    from pet import modelsource as MS
    from ysm import ysmcrypt as YC
    from ysm import ysmbin as YB

    path = sys.argv[1] if len(sys.argv) > 1 else find_model()
    if not path:
        sys.exit("找不到 .ysm 模型，请传入路径")
    print("模型:", os.path.basename(path))

    raw_file = open(path, "rb").read()

    # ---------------------------------------------------------- 1 解密层
    t0 = time.time()
    a = YC.decrypt_ysm_file(raw_file, use_native=True)
    t_c = time.time() - t0
    t0 = time.time()
    b = YC.decrypt_ysm_file(raw_file, use_native=False)
    t_py = time.time() - t0
    print("解密: C %.2fs / Python %.2fs  (%.1fx)"
          % (t_c, t_py, (t_py / t_c) if t_c > 0 else 0))
    assert a == b, "C 与 Python 解密结果不一致（长度 %d vs %d）" % (len(a), len(b))
    print("  ✓ 解密结果逐字节一致（%d B）" % len(a))

    # ---------------------------------------------------------- 2 解析层
    ra, rb = YB.deserialize(a), YB.deserialize(b)

    def shape(r):
        mm = r.main_model or {}
        bones = mm.get("bones", [])
        cubes = sum(len(bn.get("cubes", [])) for bn in bones)
        faces = sum(len(c.get("faces", {}))
                    for bn in bones for c in bn.get("cubes", []))
        return (len(bones), cubes, faces, len(r.animations),
                len(r.textures), r.format_version)

    na, nb = shape(ra), shape(rb)
    print("解析: C 路径  骨骼=%d 立方体=%d 面=%d 动画组=%d 贴图=%d fmt=%d" % na)
    print("      legacy  骨骼=%d 立方体=%d 面=%d 动画组=%d 贴图=%d fmt=%d" % nb)
    assert na == nb, "解析结果字段数不一致"
    print("  ✓ 骨骼/立方体/面/动画组/贴图数一致")

    # 骨骼名列表逐项相同（顺序也要一样）
    ba = [x.get("name") for x in (ra.main_model or {}).get("bones", [])]
    bb = [x.get("name") for x in (rb.main_model or {}).get("bones", [])]
    assert ba == bb, "骨骼名列表不一致"
    print("  ✓ 骨骼名列表（含顺序）一致")

    # ---------------------------------------------------------- 3 关掉 DLL 自动降级
    dll = os.path.join(ROOT, "native", "ysmcrypt.dll")
    bak = dll + ".bak"
    if os.path.exists(dll):
        shutil.move(dll, bak)
    try:
        # 清掉加载缓存，模拟"从一开始就没有 DLL"
        from ysm import nativeload
        nativeload._CACHE.pop("ysmcrypt", None)
        c = YC.decrypt_ysm_file(raw_file, use_native=True)
        assert c == a, "DLL 缺失时降级结果不一致"
        print("  ✓ DLL 缺失时自动降级到 Python，结果一致")
    finally:
        if os.path.exists(bak):
            shutil.move(bak, dll)
        from ysm import nativeload
        nativeload._CACHE.pop("ysmcrypt", None)

    # ---------------------------------------------------------- 4 环境变量开关
    os.environ["YSM_NO_NATIVE"] = "1"
    from ysm import nativeload
    nativeload._CACHE.clear()
    assert not nativeload.available("ysmcrypt"), "YSM_NO_NATIVE=1 没生效"
    print("  ✓ YSM_NO_NATIVE=1 能关掉 native")
    del os.environ["YSM_NO_NATIVE"]
    nativeload._CACHE.clear()
    assert nativeload.available("ysmcrypt"), "恢复后 native 没回来"
    print("  ✓ 取消该变量后 native 恢复")

    # ---------------------------------------------------------- 5 load_source 两条路径
    os.environ["YSM_NO_NATIVE"] = "1"
    nativeload._CACHE.clear()
    dl = MS.load_source(path)
    del os.environ["YSM_NO_NATIVE"]
    nativeload._CACHE.clear()
    dc = MS.load_source(path)
    print("load_source: legacy 骨骼=%d / native 骨骼=%d"
          % (len(dl["model"].order), len(dc["model"].order)))
    assert dl["model"].order == dc["model"].order, "两条路径骨骼表不一致"
    assert dl["tex"].shape == dc["tex"].shape, "贴图尺寸不一致"
    print("  ✓ load_source 两条路径骨骼表与贴图一致")

    print("\nA5 验收全部通过")


if __name__ == "__main__":
    main()
