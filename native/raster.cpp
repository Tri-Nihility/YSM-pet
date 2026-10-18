// ============================================================
// YSM 软光栅核心（C++ 加速）
//
// 为什么写这个：Python 版 render_faces() 对 3228 个四边形逐面做 numpy 运算，
// 6456 个三角形要 0.8s/帧（约 124us/三角形），实时渲染完全不可能。
// 同样的算法换成 C 只要几毫秒。
//
// 编译：g++ -O3 -shared -static -o raster.dll raster.cpp
// 调用：ctypes，全部参数用 numpy 的裸指针传，不依赖 Python C API。
//
// ⚠️ 本文件必须与 src/ysm/render.py 的算法保持一致：
//    - 重心坐标插值（w1 对应 B 点，w2 对应 C 点）
//    - 采样点取像素中心 +0.5
//    - 最近邻采样 + alpha 测试（tex.a >= 100 才画）
//    - z-buffer 严格小于才写入
//    - 超出纹理范围用 clip 而非 wrap
// ============================================================
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <algorithm>

extern "C" {

// P   : (N, 3, 3)  每个三角形 3 个顶点的 (屏幕x, 屏幕y, ndc z)
// UV  : (N, 3, 2)  每个三角形 3 个顶点的 (u, v)，单位=像素
// SH  : (N,)       每个三角形的 Lambert 明暗系数
// TEX : (TH, TW, 4) RGBA 贴图
// ZB  : (H, W)     float32 深度缓冲，调用前需填 +inf
// OUT : (H, W, 4)  RGBA 输出，调用前需清零（alpha 即为覆盖掩码）
//
// ⚠️ 共面深度打平（tie）的判定
// ---------------------------
// 模型里存在大量**共面重叠**的四边形（相邻方块共享平面、遮挡面与材质面重合），
// 它们的插值 z 在数学上相同。如果只写 `z < ZB`，那么「谁先被遍历到」就决定了
// 该像素最终采到哪个面的颜色 —— 而遍历顺序一旦变化（比如 Python 侧改成
// SoA 布局），整幅图就会有上万像素变化。实测：同一批三角形数据仅仅换顺序，
// 就有 13558 个像素不同、平均差 16.8。
//
// 所以这里把「打平」单独处理：用一个 ZI 缓冲记录当前胜出的三角形序号，
// 当新来的 z 与已记录的 z 在 eps 内视为相等时，**取序号更小者**。
// 这样结果与遍历顺序无关，Python 参考实现与 C++ 全管线能逐像素对齐。
void raster_faces(const double *P, const double *UV, const double *SH, int N,
                  const unsigned char *TEX, int TW, int TH,
                  double *ZB, unsigned char *OUT, int W, int H)
{
    // 每次调用都在内部清零（同时兼作 tie-break 的序号表），
    // 调用方因此不必关心 ZB 的初值。
    const int UNDRAWN = -1;
    int *ZI = (int *)std::malloc(sizeof(int) * (size_t)W * (size_t)H);
    if (!ZI) return;
    for (size_t i = 0; i < (size_t)W * (size_t)H; ++i) {
        ZB[i] = 1e300;
        ZI[i] = UNDRAWN;
    }

    for (int t = 0; t < N; ++t) {
        const double *p = P + (size_t)t * 9;
        const double *u = UV + (size_t)t * 6;
        const double sh = SH[t];

        const double ax = p[0], ay = p[1], az = p[2];
        const double bx = p[3], by = p[4], bz = p[5];
        const double cx = p[6], cy = p[7], cz = p[8];

        const double v0x = bx - ax, v0y = by - ay;
        const double v1x = cx - ax, v1y = cy - ay;
        const double den = v0x * v1y - v1x * v0y;
        if (den > -1e-9 && den < 1e-9) continue;      // 退化三角形
        const double invden = 1.0 / den;

        int minx = (int)std::floor(std::min(ax, std::min(bx, cx)));
        int maxx = (int)std::ceil(std::max(ax, std::max(bx, cx)));
        int miny = (int)std::floor(std::min(ay, std::min(by, cy)));
        int maxy = (int)std::ceil(std::max(ay, std::max(by, cy)));
        if (minx < 0) minx = 0;
        if (miny < 0) miny = 0;
        if (maxx > W - 1) maxx = W - 1;
        if (maxy > H - 1) maxy = H - 1;
        if (minx > maxx || miny > maxy) continue;

        const double ua = u[0], va = u[1];
        const double ub = u[2], vb = u[3];
        const double uc = u[4], vc = u[5];
        const double du1 = ub - ua, dv1 = vb - va;    // 朝 B
        const double du2 = uc - ua, dv2 = vc - va;    // 朝 C
        const double dz1 = bz - az, dz2 = cz - az;

        for (int y = miny; y <= maxy; ++y) {
            const double py = (y + 0.5) - ay;
            const double t1x = py * v1x;              // 预乘，减少内循环运算
            const double t2x = py * v0x;
            const size_t row = (size_t)y * (size_t)W;
            for (int x = minx; x <= maxx; ++x) {
                const double px = (x + 0.5) - ax;
                const double w1 = (px * v1y - t1x) * invden;
                if (w1 < 0.0) continue;
                const double w2 = (t2x - px * v0y) * invden;
                if (w2 < 0.0) continue;
                if (w1 + w2 > 1.0) continue;

                const double z = az + w1 * dz1 + w2 * dz2;
                const size_t idx = row + (size_t)x;
                // 深度测试 + 共面 tie-break：z 明显更近才覆盖；
                // z 相等（eps 内）时序号更小者胜，保证与遍历顺序无关。
                if (z > ZB[idx] + 1e-12) continue;
                if (z > ZB[idx] - 1e-12 && ZI[idx] <= t) continue;

                int ui = (int)(ua + w1 * du1 + w2 * du2);
                int vi = (int)(va + w1 * dv1 + w2 * dv2);
                if (ui < 0) ui = 0; else if (ui >= TW) ui = TW - 1;
                if (vi < 0) vi = 0; else if (vi >= TH) vi = TH - 1;

                const unsigned char *c = TEX + ((size_t)vi * TW + ui) * 4;
                if (c[3] < 100) continue;             // alpha 测试

                ZB[idx] = z;
                ZI[idx] = t;
                unsigned char *o = OUT + idx * 4;
                double r = c[0] * sh, g = c[1] * sh, b = c[2] * sh;
                o[0] = (unsigned char)(r > 255.0 ? 255.0 : r);
                o[1] = (unsigned char)(g > 255.0 ? 255.0 : g);
                o[2] = (unsigned char)(b > 255.0 ? 255.0 : b);
                o[3] = 255;
            }
        }
    }
    std::free(ZI);
}

// ---------------------------------------------------------------- SSAA 下采样
// 为什么放 C++：Python 侧做这件事要先把 (H*SS, W*SS, 4) 转成 float32
// （150x225 用 2x SSAA 就是 300x450x4 = 2.2 MB），再按轴求和，
// 中间还要产生好几个同等大小的临时数组。C++ 里一遍循环直接出结果，
// 不额外分配任何东西，也不产生 GC 压力。
//
// BIG : (H*SS, W*SS, 4)  RGBA，SSAA 倍分辨率的渲染结果
// OUT : (H, W, 4)        RGBA，下采样后的目标分辨率
// 规则：alpha 取算术平均（轮廓由 0/255 二值变软边，这是消锯齿的关键）；
//       RGB 按 alpha 加权平均（半覆盖像素不会被未覆盖处的 0 值拉黑）。
void downsample_rgba(const unsigned char *BIG, int W, int H, int SS,
                     unsigned char *OUT)
{
    const int bw = W * SS;
    const double inv = 1.0 / (double)(SS * SS);

    for (int y = 0; y < H; ++y) {
        for (int x = 0; x < W; ++x) {
            double sr = 0.0, sg = 0.0, sb = 0.0, sw = 0.0;
            for (int dy = 0; dy < SS; ++dy) {
                const unsigned char *row =
                    BIG + ((size_t)((y * SS + dy) * bw + x * SS)) * 4;
                for (int dx = 0; dx < SS; ++dx) {
                    const unsigned char *c = row + dx * 4;
                    const double a = (double)c[3];
                    sr += (double)c[0] * a;
                    sg += (double)c[1] * a;
                    sb += (double)c[2] * a;
                    sw += a;
                }
            }
            unsigned char *o = OUT + ((size_t)y * (size_t)W + x) * 4;
            if (sw > 0.0) {
                const double r = sr / sw, g = sg / sw, b = sb / sw;
                o[0] = (unsigned char)(r > 255.0 ? 255.0 : r);
                o[1] = (unsigned char)(g > 255.0 ? 255.0 : g);
                o[2] = (unsigned char)(b > 255.0 ? 255.0 : b);
            } else {
                o[0] = o[1] = o[2] = 0;
            }
            o[3] = (unsigned char)(sw * inv + 0.5);
        }
    }
}

// ============================================================
// 全管线渲染器
//
// 为什么要这个：把 FK 之后的每一步（顶点变换 → 法线变换 → 背面剔除 →
// 投影 → 拆三角 → 光栅 → SSAA 下采样）都放进 C++，中间结果全部复用
// 上下文里的缓冲。Python 侧原本每一步都要新建 numpy 数组
// （3180 面：Vh 407KB、matmul 中间结果 407KB、clip 407KB、三角 458KB…），
// 每帧几 MB 的临时分配；现在这些数组**根本不存在**。
//
// Python 侧只负责：FK（329 根骨骼，很便宜）+ 用 pybullet 算 view/proj。
// 每帧传给 C++ 的只有 Mcur(42KB) + vis(329B) + 两个 4x4 矩阵。
//
// ⚠️ 约定的矩阵布局：view/proj/M 全部**行主序**，按列向量变换
//    clip = M * v，即 out[i] = sum_k M[i*4+k] * v[k]
// ============================================================

struct YsmCtx {
    int nf;
    int *fbone;                 // (nf,)      面所属骨骼
    double *fvtx;               // (nf,4,3)   面顶点（模型空间，已 /16）
    double *fuv;                // (nf,4,2)   UV，**单位=像素**（已乘 tw/th）
    double *fnrm;               // (nf,3)     面法线
    double ctr[3];              // 取景中心（模型空间）
    const unsigned char *tex;   // 贴图 RGBA，由 Python 持有
    int tw, th;
    // ---- 工作缓冲（按需增长，永不回传给 Python）
    int capBW, capBH, capTri;
    unsigned char *big;
    double *zb;
    double *triP;               // (ntri,9)   三角形屏幕坐标 (x,y,z)*3
    double *triU;               // (ntri,6)   三角形 UV
    double *triS;               // (ntri,)    明暗系数
    int lastNtri;               // 上次 ysm_render 实际写入的三角形数（调试用）
};

// 调试：取回上次 ysm_render 实际写入光栅器的三角形数据
int ysm_dump_last(void *h, double *oP, double *oU, double *oS)
{
    YsmCtx *c = (YsmCtx *)h;
    const int n = c->lastNtri;
    if (oP) std::memcpy(oP, c->triP, sizeof(double) * (size_t)n * 9);
    if (oU) std::memcpy(oU, c->triU, sizeof(double) * (size_t)n * 6);
    if (oS) std::memcpy(oS, c->triS, sizeof(double) * (size_t)n);
    return n;
}

static void mat4mul(const double *A, const double *B, double *C) {
    for (int i = 0; i < 4; ++i)
        for (int j = 0; j < 4; ++j) {
            double s = 0.0;
            for (int k = 0; k < 4; ++k) s += A[i * 4 + k] * B[k * 4 + j];
            C[i * 4 + j] = s;
        }
}

void *ysm_new(int nf, const int *fbone, const double *fvtx,
              const double *fuv, const double *fnrm,
              const double *ctr,
              const unsigned char *tex, int tw, int th)
{
    YsmCtx *c = (YsmCtx *)std::malloc(sizeof(YsmCtx));
    if (!c) return 0;
    c->nf = nf;
    c->fbone = (int *)std::malloc(sizeof(int) * nf);
    c->fvtx = (double *)std::malloc(sizeof(double) * (size_t)nf * 12);
    c->fuv = (double *)std::malloc(sizeof(double) * (size_t)nf * 8);
    c->fnrm = (double *)std::malloc(sizeof(double) * (size_t)nf * 3);
    if (!c->fbone || !c->fvtx || !c->fuv || !c->fnrm) {
        std::free(c->fbone); std::free(c->fvtx);
        std::free(c->fuv); std::free(c->fnrm);
        std::free(c);
        return 0;
    }
    std::memcpy(c->fbone, fbone, sizeof(int) * nf);
    std::memcpy(c->fvtx, fvtx, sizeof(double) * (size_t)nf * 12);
    std::memcpy(c->fuv, fuv, sizeof(double) * (size_t)nf * 8);
    std::memcpy(c->fnrm, fnrm, sizeof(double) * (size_t)nf * 3);
    c->ctr[0] = ctr[0]; c->ctr[1] = ctr[1]; c->ctr[2] = ctr[2];
    c->tex = tex; c->tw = tw; c->th = th;
    // 贴图由 Python 的 numpy 数组持有，C++ 只借用指针，不负责释放
    c->capBW = c->capBH = 0;
    c->big = 0; c->zb = 0;
    c->capTri = 0;
    c->triP = 0; c->triU = 0; c->triS = 0;
    c->lastNtri = 0;
    return c;
}

void ysm_free(void *h) {
    YsmCtx *c = (YsmCtx *)h;
    if (!c) return;
    std::free(c->fbone); std::free(c->fvtx);
    std::free(c->fuv); std::free(c->fnrm);
    std::free(c->big); std::free(c->zb);
    std::free(c->triP); std::free(c->triU); std::free(c->triS);
    std::free(c);
}


// M   : (nb,16)  每根骨骼的世界矩阵（行主序）
// vis : (nb,)    骨骼可见性（0 隐藏）
void ysm_render(void *h, const double *M, const unsigned char *vis,
                const double *view, const double *proj,
                int W, int H, int ss,
                int lighting, const double *light,
                double amb, double hemiW, double diffW,
                unsigned char *out)
{
    YsmCtx *c = (YsmCtx *)h;
    const int bw = W * ss, bh = H * ss;
    const size_t npix = (size_t)bw * (size_t)bh;

    // ---- 按需（重新）分配工作缓冲
    if (bw != c->capBW || bh != c->capBH) {
        std::free(c->big); std::free(c->zb);
        c->big = (unsigned char *)std::malloc(npix * 4);
        c->zb = (double *)std::malloc(sizeof(double) * npix);
        c->capBW = bw; c->capBH = bh;
    }
    const int maxtri = c->nf * 2;
    if (c->capTri < maxtri) {
        std::free(c->triP); std::free(c->triU); std::free(c->triS);
        c->triP = (double *)std::malloc(sizeof(double) * (size_t)maxtri * 9);
        c->triU = (double *)std::malloc(sizeof(double) * (size_t)maxtri * 6);
        c->triS = (double *)std::malloc(sizeof(double) * maxtri);
        c->capTri = maxtri;
    }
    std::memset(c->big, 0, npix * 4);
    for (size_t i = 0; i < npix; ++i) c->zb[i] = 1e300;

    double MV[16];
    mat4mul(proj, view, MV);          // clip = proj * view * v

    const double *L = light;
    int ntri = 0;

    for (int f = 0; f < c->nf; ++f) {
        const int b = c->fbone[f];
        if (!vis[b]) continue;
        const double *m = M + (size_t)b * 16;

        // ---- 顶点：局部 → 世界 → 减取景中心 → pb 空间
        //      P_MAT = [[0,0,-1],[-1,0,0],[0,1,0]]，pb = P @ model
        //      pb_x = -mz ; pb_y = -mx ; pb_z = my
        double sx[4], sy[4], sz[4];
        for (int k = 0; k < 4; ++k) {
            const double *p = c->fvtx + (size_t)(f * 4 + k) * 3;
            const double x = p[0], y = p[1], z = p[2];
            const double wx = m[0] * x + m[1] * y + m[2] * z + m[3];
            const double wy = m[4] * x + m[5] * y + m[6] * z + m[7];
            const double wz = m[8] * x + m[9] * y + m[10] * z + m[11];
            const double dx = wx - c->ctr[0];
            const double dy = wy - c->ctr[1];
            const double dz = wz - c->ctr[2];
            sx[k] = -dz; sy[k] = -dx; sz[k] = dy;
        }

        // ---- 法线（只转旋转部分）→ pb 空间
        const double *n0 = c->fnrm + (size_t)f * 3;
        const double nx0 = m[0] * n0[0] + m[1] * n0[1] + m[2] * n0[2];
        const double ny0 = m[4] * n0[0] + m[5] * n0[1] + m[6] * n0[2];
        const double nz0 = m[8] * n0[0] + m[9] * n0[1] + m[10] * n0[2];
        const double pnx = -nz0, pny = -nx0, pnz = ny0;

        // ---- 背面剔除：保留 dot(Np, V0) >= 0
        if (pnx * sx[0] + pny * sy[0] + pnz * sz[0] < 0.0) continue;

        // ---- 明暗
        double sh = 1.0;
        if (lighting) {
            const double hemi = 0.5 + 0.5 * pnz;         // pb +Z = 上
            double d = pnx * L[0] + pny * L[1] + pnz * L[2];
            if (d < 0.0) d = 0.0;
            sh = amb + hemiW * hemi + diffW * d;
        }

        // ---- 投影 + 近平面裁剪
        // ⚠️ 近平面裁剪（修复「穿模后贴图消失/乱掉」）：
        //    pybullet 透视投影的 clip.w = -z_view，近平面 near=0.05 对应
        //    cw = 0.05。模型某部分穿到相机近平面之内（cw<near）时，
        //    透视除法 iw=1/cw 变负/无穷，投影出全屏退化三角形，整帧被毁。
        //    这里在**裁剪空间**先把四边形按 cw>=NEAR 裁掉，只保留近平面
        //    之前的可见部分再透视除法。正常视角无顶点跨近平面 → 裁剪是
        //    no-op，四边形仍拆 (0,1,2)+(0,2,3)，结果与原来逐像素一致。
        const double NEAR = 0.05;
        double ccx[4], ccy[4], ccz[4], ccw[4], cu[4], cv[4];
        for (int k = 0; k < 4; ++k) {
            const double x = sx[k], y = sy[k], z = sz[k];
            ccw[k] = MV[12] * x + MV[13] * y + MV[14] * z + MV[15];
            ccx[k] = MV[0] * x + MV[1] * y + MV[2] * z + MV[3];
            ccy[k] = MV[4] * x + MV[5] * y + MV[6] * z + MV[7];
            ccz[k] = MV[8] * x + MV[9] * y + MV[10] * z + MV[11];
            cu[k] = c->fuv[(size_t)f * 8 + k * 2 + 0];
            cv[k] = c->fuv[(size_t)f * 8 + k * 2 + 1];
        }
        // Sutherland-Hodgman：四边形(顺序 0-1-2-3)按半平面 cw>=NEAR 裁剪
        double CX[8], CY[8], CZ[8], CW[8], U2[8], V2[8];
        int np = 0;
        for (int k = 0; k < 4; ++k) {
            int k2 = (k + 1) & 3;
            double dA = ccw[k] - NEAR, dB = ccw[k2] - NEAR;
            if (dA >= 0.0) {
                CX[np] = ccx[k]; CY[np] = ccy[k]; CZ[np] = ccz[k];
                CW[np] = ccw[k]; U2[np] = cu[k]; V2[np] = cv[k]; ++np;
            }
            if ((dA >= 0.0) != (dB >= 0.0)) {
                double t = dA / (dA - dB);
                CX[np] = ccx[k] + t * (ccx[k2] - ccx[k]);
                CY[np] = ccy[k] + t * (ccy[k2] - ccy[k]);
                CZ[np] = ccz[k] + t * (ccz[k2] - ccz[k]);
                CW[np] = ccw[k] + t * (ccw[k2] - ccw[k]);
                U2[np] = cu[k] + t * (cu[k2] - cu[k]);
                V2[np] = cv[k] + t * (cv[k2] - cv[k]);
                ++np;
            }
        }
        if (np < 3) continue;
        // 投影裁剪后的多边形 + fan 三角化
        double PX[8], PY[8], PZ[8];
        for (int i = 0; i < np; ++i) {
            const double iw = 1.0 / CW[i];
            PX[i] = (CX[i] * iw * 0.5 + 0.5) * bw;
            PY[i] = (1.0 - (CY[i] * iw * 0.5 + 0.5)) * bh;
            PZ[i] = CZ[i] * iw;
        }
        for (int i = 1; i < np - 1; ++i) {
            double *tp = c->triP + (size_t)ntri * 9;
            double *tu = c->triU + (size_t)ntri * 6;
            const int vi[3] = {0, i, i + 1};
            for (int j = 0; j < 3; ++j) {
                const int v = vi[j];
                tp[j * 3 + 0] = PX[v];
                tp[j * 3 + 1] = PY[v];
                tp[j * 3 + 2] = PZ[v];
                tu[j * 2 + 0] = U2[v];
                tu[j * 2 + 1] = V2[v];
            }
            c->triS[ntri] = sh;
            ++ntri;
        }
    }

    raster_faces(c->triP, c->triU, c->triS, ntri,
                 c->tex, c->tw, c->th, c->zb, c->big, bw, bh);
    c->lastNtri = ntri;

    if (ss > 1)
        downsample_rgba(c->big, W, H, ss, out);
    else
        std::memcpy(out, c->big, (size_t)W * (size_t)H * 4);
}

}  // extern "C"
