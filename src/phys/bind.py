"""坐标约定：模型空间 <-> pybullet 空间。

桌宠**只用**这一个矩阵 `P_MAT`（渲染 / 投影 / 取景全都依赖它）。

坐标约定
--------
模型空间（YSM 烘焙顶点所在）：右手系，单位=方块
    +X = 角色右侧     -Z = 角色正前方     +Y = 上
（由 ATRI 实测：LeftArm 质心 x=-0.244 / RightArm x=+0.244，
  眼睛·眼睑质心 z≈-0.21，脚尖 z≈-0.20）

pybullet 空间：Z-up。转换矩阵 P（det=+1，是旋转不是镜像）
    pb = P @ model      P = [[0,0,-1],[-1,0,0],[0,1,0]]
    px = -mz（前）   py = -mx（左→+Y）   pz = my（上）

⚠️ 历史包袱（已清理）
--------------------
这里原先还有 `Skeleton9` —— 一套「九自由度物理骨架 + URDF + 强化学习训练」
的绑定层，属于被淘汰的训练线。桌宠是纯动画播放器，一条都不用：
不需 URDF、不接物理引擎、不训练。连同 `src/env` `src/train` `src/serve`
`src/export` 一起删掉了。需要的话回滚到 v0.2 之前的历史提交能找到。

同理，渲染/取景以前靠 **pybullet** 算 view/proj 矩阵，现在换成
`src/pet/camera.py`（纯 numpy，数值已对齐）—— pybullet 在 PyPI 上没有
Windows 轮子，留着 CI 就装不上依赖。
"""
import numpy as np

__all__ = ["P_MAT", "to_pb"]

# 模型空间 -> pybullet 空间（Z-up）的旋转矩阵，det = +1
P_MAT = np.array([[0.0, 0.0, -1.0],
                  [-1.0, 0.0, 0.0],
                  [0.0, 1.0, 0.0]])


def to_pb(v):
    """模型空间向量 -> pybullet 空间。支持 (3,) 或 (N,3)。"""
    v = np.asarray(v, float)
    return v @ P_MAT.T if v.ndim == 2 else P_MAT @ v
