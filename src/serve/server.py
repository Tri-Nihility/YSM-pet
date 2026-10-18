"""阶段 4：训练/姿态服务（FastAPI + WebSocket）。

    python -m serve.server            # http://127.0.0.1:8765

职责
    * 静态托管 web/dist（TypeScript GUI 的构建产物）
    * REST：姿态读写、表情切换、训练控制、checkpoint 列表
    * WebSocket /ws：~30Hz 推送关节角 + 骨盆位姿（GUI 据此做 GPU 蒙皮）
    * 训练线程：PPO 采样循环，逐物理帧把状态写进共享快照

硬约束：训练全程不加载模型自带动画（Skeleton9 已强制校验）。
"""
import os
import threading
import time

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import numpy as np  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from env.stand_walk import StandWalkEnv  # noqa: E402
from phys.bind import Skeleton9  # noqa: E402
from train.ppo import PPO  # noqa: E402
from ysm import ysmbin as B  # noqa: E402
from ysm import ysmmodel as YM  # noqa: E402
from ysm.expression import ExpressionRig  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WEB_DIST = os.path.join(ROOT, "web", "dist")

app = FastAPI(title="YSM-Motion 9DOF")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ---------------------------------------------------------------- 全局状态
_lock = threading.Lock()
_skel = None
_rig = None


def skeleton():
    global _skel
    if _skel is None:
        raw = B.deserialize(open(os.path.join(
            ROOT, "data", "decrypted", "亚托莉目标模型.bin"), "rb").read())
        _skel = Skeleton9(YM.YsmModel(raw, "main_model"))
    return _skel


def rig():
    global _rig
    if _rig is None:
        _rig = ExpressionRig(skeleton().model)
    return _rig


class Shared:
    """训练线程 <-> HTTP/WS 之间的共享快照。"""

    def __init__(self):
        self.mode = "manual"          # manual | train | policy
        self.angles = None            # 9 关节角（度）
        self.base_pos = [0.0, 0.0, 0.0]
        self.base_quat = [0.0, 0.0, 0.0, 1.0]
        self.episode = 0
        self.step = 0
        self.reward = 0.0
        self.ep_ret = 0.0
        self.contacts = [0, 0]
        self.stage = "stand"
        self.running = False
        self.paused = False
        self.iter = 0
        self.iters = 0
        self.history = []             # [{iter, ret, len, fallen}]
        self.message = ""
        self.fps = 0.0


shared = Shared()


class Trainer(threading.Thread):
    """后台训练 / 策略回放线程。"""

    def __init__(self, stage="stand", iters=60, spu=2048, mode="train",
                 warmstart=None):
        super().__init__(daemon=True)
        self.stage = stage
        self.iters = iters
        self.spu = spu
        self.mode = mode              # train | policy
        self.warmstart = warmstart
        self.stop_flag = False
        self.pause_flag = False

    def run(self):
        env = StandWalkEnv(self.stage, seed=0, skeleton=skeleton())
        agent = PPO(env.obs_dim, env.act_dim)
        if self.warmstart and os.path.exists(self.warmstart):
            agent.load(self.warmstart)
        det = self.mode == "policy"
        ck_dir = os.path.join(ROOT, "outputs", "checkpoints")
        os.makedirs(ck_dir, exist_ok=True)
        try:
            for it in range(1, self.iters + 1):
                if self.stop_flag:
                    break
                while self.pause_flag and not self.stop_flag:
                    time.sleep(0.2)
                if self.mode == "train":
                    buf, eps = agent.collect(
                        env, self.spu,
                        on_episode=self._on_episode(),
                        on_step=lambda r: self._snapshot(env, it, r, 0.0))
                    agent.update(buf)
                    ret = float(np.mean([e["ret"] for e in eps])) if eps else 0.0
                    elen = float(np.mean([e["len"] for e in eps])) if eps else 0.0
                    fal = float(np.mean([bool(e.get("fallen"))
                                         for e in eps])) if eps else 0.0
                    with _lock:
                        shared.iter = it
                        shared.history.append({"iter": it, "ret": round(ret, 2),
                                               "len": round(elen, 1),
                                               "fallen": round(fal, 3)})
                        if len(shared.history) > 4000:
                            shared.history = shared.history[-4000:]
                    agent.save(os.path.join(ck_dir, f"{self.stage}_final.pt"),
                               {"stage": self.stage, "iter": it, "ret": ret})
                else:
                    env.reset()
                    ep_ret, n = 0.0, 0
                    done = False
                    while not done and not self.stop_flag:
                        o = agent.norm.normalize(env._obs())
                        a, _, _ = agent.act(o, deterministic=True)
                        o2, r, done, info = env.step(a)
                        ep_ret += r
                        n += 1
                        self._snapshot(env, it, r, ep_ret)
                        time.sleep(1 / 60)      # 回放按 60Hz 慢放
                    with _lock:
                        shared.episode += 1
        finally:
            env.close()
            with _lock:
                shared.running = False
                shared.mode = "manual"
                shared.message = "训练/回放结束"

    def _on_episode(self, env, it):
        def cb(info):
            with _lock:
                shared.episode += 1
                shared.stage = self.stage
        return cb

    def _snapshot(self, env, it, rew, ep_ret):
        import pybullet as p
        with _lock:
            q = env.current_q()
            shared.angles = [float(x) for x in q]
            pos, quat = p.getBasePositionAndOrientation(
                env.robot, physicsClientId=env.client)
            shared.base_pos = [float(x) for x in pos]
            shared.base_quat = [float(x) for x in quat]
            shared.step = env.steps
            shared.reward = float(rew)
            shared.ep_ret = float(ep_ret)
            shared.iter = it
            shared.contacts = [int(x) for x in env._contacts()]
            shared.mode = self.mode
            shared.stage = self.stage


trainer = None


def start_trainer(**kw):
    global trainer
    stop_trainer()
    trainer = Trainer(**kw)
    with _lock:
        shared.running = True
        shared.paused = False
        shared.mode = kw.get("mode", "train")
        shared.stage = kw.get("stage", "stand")
        shared.iters = kw.get("iters", 0)
        shared.iter = 0
        shared.history = []
        shared.message = ""
    trainer.start()


def stop_trainer():
    global trainer
    if trainer is not None:
        trainer.stop_flag = True
        trainer.join(timeout=5)
        trainer = None
    with _lock:
        shared.running = False


# ---------------------------------------------------------------- REST
class PoseBody(BaseModel):
    angles: list


class ExprBody(BaseModel):
    slot: str
    option: str


class TrainBody(BaseModel):
    stage: str = "stand"
    iters: int = 60
    stepsPerUpdate: int = 2048
    mode: str = "train"                     # train | policy
    warmstart: str = ""


@app.get("/api/config")
def api_config():
    sk = skeleton()
    return {
        "jointOrder": sk.joint_names,
        "joints": [{"name": j["name"], "label": j["label"], "bone": j["bone"],
                    "limits": j["limits"], "default": j["default"]}
                   for j in sk.joints],
        "standHeight": sk.stand_height(),
        "segments": list(sk.segments.keys()),
        "useNativeAnimation": False,
    }


@app.get("/api/expressions")
def api_expr():
    r = rig()
    return {"enabled": r.enabled, "slots": r.slots(), "state": r.state}


@app.post("/api/expressions")
def api_expr_set(b: ExprBody):
    r = rig()
    try:
        r.set_slot(b.slot, b.option)
    except KeyError as e:
        raise HTTPException(400, str(e))
    return {"state": r.state}


@app.get("/api/pose")
def api_pose():
    with _lock:
        return {"mode": shared.mode, "angles": shared.angles or
                [float(x) for x in skeleton().default_pose()],
                "expressions": rig().state}


@app.post("/api/pose")
def api_pose_set(b: PoseBody):
    if len(b.angles) != 9:
        raise HTTPException(400, "需要 9 个关节角")
    if shared.running:
        raise HTTPException(409, "训练进行中，先停止训练再手动摆 pose")
    q = skeleton().denormalize(
        skeleton().normalize(np.asarray(b.angles, float)))
    with _lock:
        shared.angles = [float(x) for x in q]
        shared.mode = "manual"
    return {"angles": shared.angles}


@app.post("/api/train/start")
def api_train_start(b: TrainBody):
    if b.mode not in ("train", "policy"):
        raise HTTPException(400, "mode 必须是 train|policy")
    warm = b.warmstart
    if warm and not os.path.isabs(warm):
        warm = os.path.join(ROOT, warm)
    if b.mode == "policy":
        warm = warm or os.path.join(ROOT, "outputs", "checkpoints",
                                    f"{b.stage}_final.pt")
        if not os.path.exists(warm):
            raise HTTPException(404, f"找不到 checkpoint: {warm}")
    start_trainer(stage=b.stage, iters=b.iters, spu=b.stepsPerUpdate,
                  mode=b.mode, warmstart=warm or None)
    return {"ok": True, "mode": b.mode, "stage": b.stage}


@app.post("/api/train/stop")
def api_train_stop():
    stop_trainer()
    return {"ok": True}


@app.post("/api/train/pause")
def api_train_pause():
    with _lock:
        shared.paused = not shared.paused
        p = shared.paused
    if trainer is not None:
        trainer.pause_flag = p
    return {"paused": p}


@app.get("/api/train/status")
def api_status():
    with _lock:
        return {
            "running": shared.running, "paused": shared.paused,
            "mode": shared.mode, "stage": shared.stage,
            "iter": shared.iter, "iters": shared.iters,
            "episode": shared.episode, "step": shared.step,
            "reward": shared.reward, "epRet": shared.ep_ret,
            "history": shared.history[-400:],
            "message": shared.message,
        }


@app.get("/api/checkpoints")
def api_ckpts():
    d = os.path.join(ROOT, "outputs", "checkpoints")
    out = []
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.endswith(".pt"):
                out.append({"name": f, "path": os.path.join("outputs", "checkpoints", f),
                            "size": os.path.getsize(os.path.join(d, f))})
    return out


# ---------------------------------------------------------------- 静态
# 注意：`/assets` 之外，前端还要从 **站点根** 拉两个文件：
#     /model.glb   （Viewport.tsx 的 GLTFLoader）
#     /rig.json    （api.ts 的 rig()）
# 它们就放在 web/dist 下。原来只挂了 /assets，这两个直接 404 —— 表现为
# GUI 能打开、但模型永远加载不出来（three.js 静默失败）。
# `/` 的完整静态挂载放到**文件末尾**（见 STATIC_ROOT_MOUNT），确保
# /api/* 与 /ws 先注册、优先匹配。
if os.path.isdir(WEB_DIST):
    app.mount("/assets",
              StaticFiles(directory=os.path.join(WEB_DIST, "assets")),
              name="assets")


# ---------------------------------------------------------------- WS
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    sk = skeleton()
    last = time.time()
    frames = 0
    fps_t = time.time()
    try:
        while True:
            now = time.time()
            dt = now - last
            if dt < 1 / 30:
                time.sleep(1 / 30 - dt)
                continue
            last = now
            with _lock:
                angles = shared.angles if shared.angles is not None \
                    else [float(x) for x in sk.default_pose()]
                payload = {
                    "t": now,
                    "mode": shared.mode,
                    "stage": shared.stage,
                    "angles": angles,
                    "basePos": shared.base_pos,
                    "baseQuat": shared.base_quat,
                    "episode": shared.episode,
                    "step": shared.step,
                    "reward": shared.reward,
                    "epRet": shared.ep_ret,
                    "contacts": shared.contacts,
                    "running": shared.running,
                    "paused": shared.paused,
                    "iter": shared.iter,
                    "iters": shared.iters,
                    "expressions": rig().state,
                }
            await ws.send_json(payload)
            frames += 1
            if now - fps_t > 2:
                with _lock:
                    shared.fps = frames / (now - fps_t)
                frames, fps_t = 0, now
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


# ---------------------------------------------------------------- 静态根挂载
# 必须放在**所有路由注册之后**：Starlette 的路由按注册顺序匹配，`/` 上的
# StaticFiles 是兜底，先注册会把 /api/* 和 /ws 一起吞掉。
# html=True 让未知路径回落到 index.html（前端是 SPA）。
if os.path.isdir(WEB_DIST):
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True),
              name="static-root")


def main():
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")


if __name__ == "__main__":
    main()
