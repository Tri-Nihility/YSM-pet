"""阶段 4：带重力的站立/行走训练环境（pybullet）。

设计要点
--------
* 根 = pelvis，浮动基（useFixedBase=False），重力 -9.81 m/s²。
* 9 个可控关节按 configs/joints9.yaml 的顺序排列；动作 a∈[-1,1]⁹
  经 Skeleton9.denormalize 映射回关节角（度），再用 URDF 的限位弧度驱动。
* ⛔ 全程不加载/不采样模型自带动画（use_native_animation=false 是硬约束）。
* 课程：stage="stand"（学会站立）→ stage="walk"（学会走路）。
* 观测/奖励见 configs/phys.yaml。
"""
import math
import os

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import numpy as np  # noqa: E402
import pybullet as p  # noqa: E402
import yaml  # noqa: E402

from phys.bind import P_MAT, Skeleton9, to_pb  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
URDF = os.path.join(ROOT, "models", "ysm_atri_9dof.urdf")


def load_yaml(name):
    with open(os.path.join(ROOT, "configs", name), encoding="utf-8") as fp:
        return yaml.safe_load(fp)


class StandWalkEnv:
    """九自由度物理环境。render=True 时弹 pybullet GUI 窗口。"""

    def __init__(self, stage="stand", render=False, seed=0, skeleton=None):
        self.stage = stage
        self.cfg = load_yaml("phys.yaml")
        self.render_mode = render
        self.rng = np.random.default_rng(seed)
        self.sk = skeleton or self._build_skeleton()

        cur = {c["name"]: c for c in self.cfg["curriculum"]}
        if stage not in cur:
            raise KeyError(f"未知课程阶段: {stage}")
        self.ep = cur[stage]

        self.gravity = float(self.cfg["gravity"])
        self.sim_hz = int(self.cfg["sim_hz"])
        self.frame_skip = int(self.cfg["frame_skip"])
        self.control_dt = self.frame_skip / self.sim_hz
        self.max_steps = int(round(self.ep["episode_seconds"] * self.cfg["control_hz"]))
        self.act = self.cfg["actuator"]
        self.dr = self.cfg["domain_randomization"]

        self.client = p.connect(p.GUI if render else p.DIRECT)
        p.setPhysicsEngineParameter(fixedTimeStep=1.0 / self.sim_hz, numSolverIterations=60,
                                    physicsClientId=self.client)
        p.setGravity(0, 0, self.gravity, physicsClientId=self.client)

        # 地面
        pg = p.createCollisionShape(p.GEOM_PLANE, physicsClientId=self.client)
        self.ground = p.createMultiBody(0, pg, physicsClientId=self.client)
        p.changeDynamics(self.ground, -1, lateralFriction=self.cfg["ground"]["friction"],
                         restitution=self.cfg["ground"]["restitution"],
                         physicsClientId=self.client)

        # 机器人
        self.robot = p.loadURDF(URDF, useFixedBase=False, physicsClientId=self.client)
        n = p.getNumJoints(self.robot, physicsClientId=self.client)
        info = [p.getJointInfo(self.robot, i, physicsClientId=self.client) for i in range(n)]
        self.rev = [i for i in range(n) if info[i][2] == p.JOINT_REVOLUTE]
        self.jname = [info[i][1].decode() for i in self.rev]
        assert sorted(self.jname) == sorted(self.sk.joint_names)
        self.jidx = {nm: k for k, nm in zip(self.rev, self.jname)}
        # 段名 -> link index（碰撞检测用）；pelvis 是根，pybullet 里是 -1
        self.link_of = {info[i][12].decode(): i for i in range(n)}
        self.link_of[self.sk.cfg["root"]["segment"]] = -1
        self.foot_links = [self.link_of[s] for s in ("shinL", "shinR")]

        # 视角（GUI 用）
        if render:
            h = self.sk.stand_height()
            p.resetDebugVisualizerCamera(2.6, -12, -60, [0, 0, h * 0.6],
                                         physicsClientId=self.client)

        # 观测布局
        self._nq = len(self.rev)
        self.obs_dim = 1 + 3 + 3 + 3 + self._nq + self._nq + 2 + self._nq \
            + (1 if stage == "walk" else 0)
        self.act_dim = self._nq
        self.torque_limit = self.sk.joint_torques()
        self.default_q = self.sk.default_pose()
        self.stand_h = float(self.sk.stand_height())
        self.reset()

    # ------------------------------------------------------------------
    def _build_skeleton(self):
        from ysm import ysmbin as B
        from ysm import ysmmodel as YM
        raw = B.deserialize(open(os.path.join(ROOT, "data", "decrypted",
                                              "亚托莉目标模型.bin"), "rb").read())
        return Skeleton9(YM.YsmModel(raw, "main_model"))

    # ------------------------------------------------------------------
    def seed(self, s):
        self.rng = np.random.default_rng(s)

    # ------------------------------------------------------------------
    def reset(self):
        # 域随机化
        ms = self.dr["mass_scale"] if self.dr["enabled"] else (1, 1)
        scale = self.rng.uniform(*ms)
        for seg, m in self.sk.masses.items():
            p.changeDynamics(self.robot, self.link_of[seg], mass=max(m * scale, 1e-3),
                             physicsClientId=self.client)
        fr = self.dr["friction"] if self.dr["enabled"] else (0.9, 0.9)
        p.changeDynamics(self.ground, -1, lateralFriction=self.rng.uniform(*fr),
                         physicsClientId=self.client)

        # 初始姿态：default + 抖动，摆出「半蹲准备」
        jit = self.ep["init_pose_jitter"]
        q0 = self.default_q + self.rng.uniform(-jit, jit, self._nq) * \
            (self.sk.limits()[:, 1] - self.sk.limits()[:, 0]) / 2
        q0 = self.sk.denormalize(self.sk.normalize(q0))

        # 摆到初始高度（脚底贴地），直接按关节角算一次 FK
        base = to_pb(np.asarray(self.sk.by_name[self.sk.cfg["root"]["bone"]]["pivot"],
                                float) / 16.0) + np.array([0, 0, -self.sk.sole_height()])
        base[2] += 0.02                                   # 留 2cm 落地余量
        p.resetBasePositionAndOrientation(self.robot, base, [0, 0, 0, 1],
                                          physicsClientId=self.client)
        for k, nm in zip(self.rev, self.jname):
            qi = self.sk.joint_names.index(nm)
            p.resetJointState(self.robot, k, math.radians(q0[qi]), 0.0,
                              physicsClientId=self.client)
        for _ in range(int(self.sim_hz * 0.25)):          # 0.25s 自由落定
            p.stepSimulation(physicsClientId=self.client)

        # 初始扰动
        if self.dr["enabled"]:
            v = self.rng.uniform(-1, 1, 3) * float(self.dr["initial_push"])
            p.resetBaseVelocity(self.robot, [v[0], v[1], 0], [0, 0, 0],
                                physicsClientId=self.client)

        self.steps = 0
        self.prev_action = np.zeros(self.act_dim)
        self.last_contacts = self._contacts()
        self.contact_flips = 0
        return self._obs()

    # ------------------------------------------------------------------
    def _contacts(self):
        cps = p.getContactPoints(self.robot, self.ground, physicsClientId=self.client)
        feet = set(cp[3] for cp in cps)
        return np.array([1.0 if ln in feet else 0.0 for ln in self.foot_links])

    def _obs(self):
        st = p.getBasePositionAndOrientation(self.robot, physicsClientId=self.client)
        pos, quat = np.array(st[0]), np.array(st[1])
        R = np.array(p.getMatrixFromQuaternion(quat)).reshape(3, 3)

        vel = p.getBaseVelocity(self.robot, physicsClientId=self.client)
        lv, av = np.array(vel[0]), np.array(vel[1])

        # 躯干 +Y（模型上方向）在世界系
        up = np.array([0, 0, 1.0])                        # pb 里躯干局部 +Z 即世界 +Z
        torso_up = R @ np.array([0, 0, 1.0])

        q = np.zeros(self._nq)
        dq = np.zeros(self._nq)
        for k, nm in enumerate(self.jname):
            js = p.getJointState(self.robot, self.rev[k], physicsClientId=self.client)
            q[k] = math.degrees(js[0])
            dq[k] = js[1]

        parts = [pos[2] / self.stand_h,
                 (R.T @ lv) / 2.0,
                 (R.T @ av) / 5.0,
                 torso_up,
                 self.sk.normalize(q),
                 np.clip(dq / float(self.act["max_velocity"]), -3, 3),
                 self._contacts(),
                 self.prev_action]
        if self.stage == "walk":
            parts.append(np.array([self.ep["target_speed"]]))
        return np.concatenate([np.asarray(x, float).ravel() for x in parts])

    # ------------------------------------------------------------------
    def _reward(self, obs, action):
        r_cfg = self.ep["rewards"]
        pos = p.getBasePositionAndOrientation(self.robot, physicsClientId=self.client)
        quat = np.array(pos[1])
        R = np.array(p.getMatrixFromQuaternion(quat)).reshape(3, 3)
        up = R @ np.array([0, 0, 1.0])
        tilt = math.degrees(math.acos(np.clip(up[2], -1, 1)))
        height = pos[0][2] / self.stand_h
        vel = p.getBaseVelocity(self.robot, physicsClientId=self.client)[0][0]

        r = r_cfg["alive"]
        r += r_cfg["height"] * np.clip(height / self.ep["target_height_ratio"], 0, 1)
        r += r_cfg["upright"] * max(0.0, up[2])
        if self.stage == "stand":
            # 重心压在双脚支撑面内：脚中心与骨盆水平距离
            com = self._pelvis_xz()
            feet = self._feet_center_xz()
            r += r_cfg["com_over_feet"] * math.exp(-((com[0] - feet[0]) ** 2 +
                                                     (com[1] - feet[1]) ** 2) * 30)
        else:
            r += r_cfg["forward_vel"] * min(vel / self.ep["target_speed"], 1.2)
            contacts = self._contacts()
            if contacts.sum() == 1 and self.last_contacts.sum() == 1 \
                    and not np.array_equal(contacts, self.last_contacts):
                self.contact_flips += 1
                r += r_cfg["contact_alternation"]
            self.last_contacts = contacts
        r += r_cfg["joint_speed"] * float(np.mean(np.abs(action)))
        r += r_cfg["action_smooth"] * float(np.mean(np.abs(action - self.prev_action)))
        return r

    def _pelvis_xz(self):
        pos = p.getBasePositionAndOrientation(self.robot, physicsClientId=self.client)[0]
        return pos[0], pos[1]

    def _feet_center_xz(self):
        xs, ys = [], []
        for ln in self.foot_links:
            st = p.getLinkState(self.robot, ln, physicsClientId=self.client)
            xs.append(st[4][0])
            ys.append(st[4][1])
        return float(np.mean(xs)), float(np.mean(ys))

    def _fallen(self, obs):
        return (obs[0] < self.ep["target_height_ratio"]
                or self._tilt_deg() > self.ep["max_tilt_deg"])

    def _tilt_deg(self):
        quat = p.getBasePositionAndOrientation(self.robot,
                                               physicsClientId=self.client)[1]
        R = np.array(p.getMatrixFromQuaternion(quat)).reshape(3, 3)
        up = R @ np.array([0, 0, 1.0])
        return math.degrees(math.acos(np.clip(up[2], -1, 1)))

    # ------------------------------------------------------------------
    def current_q(self):
        q = np.zeros(self._nq)
        for k in range(self._nq):
            js = p.getJointState(self.robot, self.rev[k], physicsClientId=self.client)
            q[k] = math.degrees(js[0])
        return q

    def step(self, action):
        action = np.clip(np.asarray(action, float), -1, 1)
        q_target = self.sk.denormalize(action * float(self.act["action_scale"]))
        gains = float(self.act["position_gain"])
        vgains = float(self.act["velocity_gain"])
        p.setJointMotorControlArray(
            self.robot, self.rev, p.POSITION_CONTROL,
            targetPositions=np.radians(q_target),
            forces=self.torque_limit,
            positionGains=[gains] * self._nq,
            velocityGains=[vgains] * self._nq,
            physicsClientId=self.client)
        for _ in range(self.frame_skip):
            p.stepSimulation(physicsClientId=self.client)

        self.steps += 1
        self.prev_action = action
        obs = self._obs()
        reward = self._reward(obs, action)
        done = False
        info = {}
        if self._fallen(obs):
            done = True
            reward += self.ep["rewards"]["fallen_penalty"]
            info["fallen"] = True
        elif self.steps >= self.max_steps:
            done = True
            info["survived"] = True
            if self.stage == "walk":
                vel = p.getBaseVelocity(self.robot, physicsClientId=self.client)[0][0]
                info["distance"] = float(self._pelvis_xz()[0])
                info["speed"] = float(vel)
        return obs, float(reward), done, info

    def close(self):
        p.disconnect(physicsClientId=self.client)
