"""相机矩阵：纯 numpy 复刻 pybullet 的两个函数，**去掉 pybullet 依赖**。

为什么要自己写
--------------
桌宠只用到 pybullet 的两个**纯数学**函数：

    computeViewMatrixFromYawPitchRoll(...)
    computeProjectionMatrixFOV(...)

为此拖进一个 ~100 MB 的物理引擎非常不划算，而且更致命的是：

    pybullet **在 PyPI 上没有 Windows 轮子**，`pip install pybullet` 会回退到
    源码构建并直接失败 —— 于是 CI / 别人 clone 仓库根本装不上依赖。

所以这里用 numpy 把它们原样实现一遍。

数值约定（**已对着本机 pybullet 逐项标定**，勿擅自改动）
------------------------------------------------------
两函数都返回标准 OpenGL 矩阵（glFrustum / gluLookAt 那套），
pb 返回的是**列主序 16 元**，调用方一贯的写法是

    np.array(p.computeXxx(...)).reshape(4, 4).T      # -> 行主序 4x4

本模块**直接返回行主序 4x4**，省掉那次 reshape/T，调用方也更好读。

视图（由 yaw/pitch/distance 定相机位置，**且 yaw/pitch 用角度制**）：

    eye = target + d · [ sin(yaw)·cos(pitch),
                        -cos(yaw)·cos(pitch),
                        -sin(pitch) ]                 (upAxisIndex=2 / Z-up)

    forward = normalize(target - eye)
    right   = normalize(forward × worldUp)
    up      = normalize(right  × forward)

    V[:3,:3] = [right, up, -forward] 逐行； V[:3,3] = -R @ eye

⚠️ **roll 一律传 0。** 这不是偷懒 —— 实测 pybullet 的 roll **会把相机位置本身
也转走**（yaw=37/pitch=21/dist=5 时 eye 从 [2.81,-3.73,-1.79] 漂到 roll=90
的 [1.38,-4.81, 0.00]），它走的是「ZYX 欧拉链先转偏移再 lookAt」的语义，
不是常见的「绕视线滚」。

    这与本模块的「绕视线旋转」实现**从 roll ≠ 0 起就不一致**（重力方向上恰好
    相等，误差 1e-8，是因为那种姿态下两者退化为同一回事）。

桌宠三处调用全部传 roll=0（`_render_cpp` / `_render_raw` / `project_point`），
实况参数下与 pybullet 逐元素一致（≈1e-7，即 pb 内部 float32 的舍入）。若将来真的要用 roll，请**先补标定再放开**——所以这里直接
拒绝非零 roll，宁可响亮报错，也不要悄悄算错。

投影（标准 OpenGL 透视，同样**角度制** fov）：

    t = 1 / tan(fov/2)
    P = [[ t/aspect, 0,  0,                        0                      ],
         [ 0,        t,  0,                        0                      ],
         [ 0,        0, -(far+near)/(far-near),   -2·far·near/(far-near)  ],
         [ 0,        0, -1,                        0                      ]]

标定结果：投影矩阵与 pybullet 一致到 3e-7（那是 pb 内部 float32 的舍入，
本模块用 float64，精度反而更高）；视图矩阵在 **roll=0** 下逐元素一致。

⚠️ 别把它当 pybullet 的通用等价物：这里**只**实现了 upAxisIndex=2 这一档，
其余分支（upAxisIndex=0/1 的 Y-up / X-up 约定）没实现也不需要。
"""
import numpy as np

__all__ = ["view_matrix", "proj_matrix"]

_WORLD_UP = np.array([0.0, 0.0, 1.0])     # upAxisIndex = 2 → Z-up


def view_matrix(target, distance, yaw, pitch, roll=0.0, up_axis=2):
    """pybullet `computeViewMatrixFromYawPitchRoll` 的行主序 4×4 版本。

    target  : 相机看向的点（一般是包围盒中心的投影 `pctr`）
    distance: 相机到 target 的距离
    yaw/pitch: **角度**（不是弧度）
    roll    : 必须为 0，理由见模块文档——pybullet 的 roll 语义与常见约定不同，
              且从未被标定过。传非 0 会抛错而不是悄悄算错。
    """
    if up_axis != 2:
        raise ValueError("本模块只实现 upAxisIndex=2（Z-up），桌宠用不到别的：%r"
                         % (up_axis,))
    if abs(float(roll)) > 1e-9:
        raise ValueError(
            "view_matrix 只支持 roll=0（见模块文档：pybullet 的 roll 会把相机"
            "位置一起转走，本模块未复刻该语义，且未经标定）。收到 roll=%r"
            % (roll,))
    tgt = np.asarray(target, float).reshape(3)
    d = float(distance)
    yw, pt = np.radians([float(yaw), float(pitch)])

    # 相机位置：pb 的欧拉约定（见模块文档）
    eye = tgt + d * np.array([np.sin(yw) * np.cos(pt),
                              -np.cos(yw) * np.cos(pt),
                              -np.sin(pt)])

    fwd = tgt - eye
    n = np.linalg.norm(fwd)
    if n < 1e-12:                                   # 退化：距离 0
        fwd = np.array([0.0, 0.0, -1.0])
    else:
        fwd /= n

    right0 = np.cross(fwd, _WORLD_UP)
    if np.linalg.norm(right0) < 1e-9:
        # pitch 打到 ±90°：视线与世界 up 共线 → 换一个参考轴，避免叉积为 0
        ref = np.array([1.0, 0.0, 0.0])
        right0 = np.cross(fwd, ref)
        if np.linalg.norm(right0) < 1e-9:
            right0 = np.cross(fwd, np.array([0.0, 1.0, 0.0]))
    right0 /= np.linalg.norm(right0)
    up0 = np.cross(right0, fwd)
    up0 /= np.linalg.norm(up0)

    V = np.eye(4)
    V[0, :3] = right0
    V[1, :3] = up0
    V[2, :3] = -fwd
    V[:3, 3] = -V[:3, :3] @ eye
    return V


def proj_matrix(fov, aspect, near=0.05, far=100.0):
    """pybullet `computeProjectionMatrixFOV` 的行主序 4×4 版本（fov 为**角度**）。"""
    t = 1.0 / np.tan(np.radians(float(fov)) / 2.0)
    a = float(aspect)
    nf = 1.0 / (float(far) - float(near))
    P = np.zeros((4, 4))
    P[0, 0] = t / a
    P[1, 1] = t
    P[2, 2] = -(float(far) + float(near)) * nf
    P[2, 3] = -2.0 * float(far) * float(near) * nf
    P[3, 2] = -1.0
    return P
