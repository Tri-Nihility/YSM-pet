"""
软光栅渲染器（为什么不用 pybullet 画贴图：GEOM_BOX 不支持自定义 UV，
而 Bedrock 用 box-UV，映射规则无法交给它）。

相机矩阵仍由 pybullet 提供（数学部分），光栅化自己实现：
numpy 重心坐标 + z-buffer + 最近邻采样 + alpha 测试 + Lambert 光照。
"""
import numpy as np
import pybullet as p


def camera_matrix(target, dist, yaw, pitch, aspect=1.0, fov=45, near=0.05, far=100):
    """返回 (P @ V)，行主序。target 应投影到画面中心。"""
    vm = np.array(p.computeViewMatrixFromYawPitchRoll(
        target, dist, yaw, pitch, 0, upAxisIndex=2)).reshape(4, 4).T
    pm = np.array(p.computeProjectionMatrixFOV(fov, aspect, near, far)).reshape(4, 4).T
    return pm @ vm


def draw_tri(buf, zbuf, A, B, C, uvA, uvB, uvC, tex, tw, th, shade):
    W, H = buf.shape[1], buf.shape[0]
    minx = max(int(np.floor(min(A[0], B[0], C[0]))), 0)
    maxx = min(int(np.ceil(max(A[0], B[0], C[0]))), W - 1)
    miny = max(int(np.floor(min(A[1], B[1], C[1]))), 0)
    maxy = min(int(np.ceil(max(A[1], B[1], C[1]))), H - 1)
    if minx > maxx or miny > maxy:
        return
    gx = np.arange(minx, maxx + 1) + 0.5
    gy = np.arange(miny, maxy + 1) + 0.5
    X, Y = np.meshgrid(gx, gy)
    v0x, v0y = B[0] - A[0], B[1] - A[1]
    v1x, v1y = C[0] - A[0], C[1] - A[1]
    den = v0x * v1y - v1x * v0y
    if abs(den) < 1e-9:
        return
    px, py = X - A[0], Y - A[1]
    w1 = (px * v1y - v1x * py) / den
    w2 = (v0x * py - px * v0y) / den
    m = (w1 >= 0) & (w2 >= 0) & (w1 + w2 <= 1)
    if not m.any():
        return
    z = A[2] + w1 * (B[2] - A[2]) + w2 * (C[2] - A[2])
    u = uvA[0] + w1 * (uvB[0] - uvA[0]) + w2 * (uvC[0] - uvA[0])
    v = uvA[1] + w1 * (uvB[1] - uvA[1]) + w2 * (uvC[1] - uvA[1])
    ok = m & (z < zbuf[miny:maxy + 1, minx:maxx + 1])
    if not ok.any():
        return
    ui = np.clip(u[ok].astype(int), 0, tw - 1)
    vi = np.clip(v[ok].astype(int), 0, th - 1)
    cols = tex[vi, ui]
    vis = cols[:, 3] >= 100                      # alpha 测试（skin.png 90.9% 透明）
    if not vis.any():
        return
    iy, ix = np.nonzero(ok)
    iy, ix = iy[vis] + miny, ix[vis] + minx
    buf[iy, ix] = cols[vis][:, :3] * shade
    zbuf[iy, ix] = z[ok][vis]


def project(verts, M, W, H):
    """verts[4,3] -> 屏幕坐标 sx, sy, sz"""
    clip = np.hstack([verts, np.ones((len(verts), 1))]) @ M.T
    ndc = clip[:, :3] / clip[:, 3:4]
    sx = (ndc[:, 0] * 0.5 + 0.5) * W
    sy = (1 - (ndc[:, 1] * 0.5 + 0.5)) * H
    return sx, sy, ndc[:, 2]


def render_faces(faces, M, W, H, tex, th, tw, bg=246.0, light=None, cull=True):
    """faces: [(verts[4,3], uv[4,2] 像素, normal[3])]，坐标系须已是 pybullet(Z 上)。"""
    light = light if light is not None else np.array([0.45, -0.6, 0.66])
    light = light / np.linalg.norm(light)
    buf = np.full((H, W, 3), float(bg))
    zbuf = np.full((H, W), np.inf)
    painted = 0
    for verts, uv, n in faces:
        if cull and np.dot(n, -verts[0]) > 0:            # 背面剔除
            continue
        shade = 0.55 + 0.45 * max(float(np.dot(n, light)), 0.0)
        sx, sy, sz = project(verts, M, W, H)
        A = np.array([sx[0], sy[0], sz[0]])
        B = np.array([sx[1], sy[1], sz[1]])
        C = np.array([sx[2], sy[2], sz[2]])
        D = np.array([sx[3], sy[3], sz[3]])
        draw_tri(buf, zbuf, A, B, C, uv[0], uv[1], uv[2], tex, tw, th, shade)
        draw_tri(buf, zbuf, A, C, D, uv[0], uv[2], uv[3], tex, tw, th, shade)
    painted = int((zbuf < np.inf).sum())
    return buf, painted
